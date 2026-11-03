"""Link-cable transport: wire two NGPC machines together over the serial channel.

The NGPC link cable is TLCS-900 serial channel 0, driven by the SNK BIOS COM
routines. From the emulator's side the cable is nothing but a byte pipe: each
machine transmits bytes (drained with ``NativeMachine.serial_read_tx``) and
receives bytes (fed with ``NativeMachine.serial_write_rx``). Because the two
consoles each run their own copy of the game and share *only* those bytes, there
is no shared simulation to keep deterministic -- no rollback, no lockstep. A link
is just a reliable, ordered byte relay that honours the RTS flow-control line.

This module provides that relay in two shapes behind one tiny interface:

* :class:`InProcessLink` -- two machines in this process (two players on one PC).
* :class:`TcpLink`       -- one local machine + a TCP socket to a remote peer
                            (LAN / online). TCP because the cable never drops or
                            reorders bytes, which is exactly TCP's guarantee.
* :class:`FlycastLink`   -- one local machine + a DREAMCAST in the Flycast emulator,
                            through Flycast's "BattleCable" null-modem (UDP).

Both are pumped once per emulated frame, AFTER stepping the machine(s):

    link.pump()                      # in-process: moves bytes both directions
    # or, per machine, for the TCP case:
    link.pump()                      # sends local TX, delivers received RX

Enable the hardware path on each machine first (``machine.serial_set_enabled(
True)``); disabled, the serial registers stay inert and the cable reads as
unplugged -- the pre-link behaviour.
"""

from __future__ import annotations

import os
import socket
from typing import Protocol

from core.link_debug import deliver_injected


class _SerialMachine(Protocol):
    """The slice of NativeMachine a link needs (see core/native.py)."""

    def serial_read_tx(self, max_bytes: int = ...) -> bytes: ...
    def serial_write_rx(self, data: bytes) -> None: ...
    def serial_rts(self) -> bool: ...
    def serial_set_cts(self, high: bool) -> None: ...
    def serial_set_enabled(self, on: bool) -> None: ...


# One TLCS-900 SC0 byte at 19200 bps is ~3200 CPU cycles; a frame is ~102 000,
# so ~32 bytes cross per frame at most. Drain generously past that so a burst is
# never left stranded in the FIFO for a frame.
_DRAIN_CHUNK = 256


def declare_peer(machine: "_SerialMachine", present: bool) -> None:
    """Tell the console whether there is a CONSOLE at the other end of the cable.

    ⛔ THE BUG THIS ENDS, MEASURED (tools/link_online_probe.py, two consoles of
    SNK Gals' Fighters, 400 frames):

        online (TcpLink)          0xB1 = 0x06   bit2 = 1   -> "aucun pair"
        local  (PlayPage relay)   0xB1 = 0x02   bit2 = 0   -> pair vu

    0xB1 bit2 is the cable-DETECT line the games gate their versus mode on, and since
    it was moved onto the peer's RTS (specs/LINK_CABLE.md §1, silicon 2026-08-19) the
    core only clears it once something has SPOKEN FOR THE PEER -- `serial_cts_seen`.
    Two places do: `InProcessLink._relay` and `PlayPage._pump_link`, both of which
    have the other console in the same process and read its RTS pin directly. The
    ONLINE transports have no such pin to read and nobody ever spoke for the peer, so
    every online console read "no cable" for ever. That is Card Fighters' Clash never
    leaving "EITHER PLAYER MUST PUSH A", Gals' Fighters / Puyo Pop / Magical Drop not
    seeing a second player, and The Last Blade's LINK ERROR -- one cause, not five.

    ⚡ WHAT IS ASSERTED HERE, AND WHY IT IS HONEST. The transports are built AFTER the
    pairing completes, and an online session can only be opened from a console that is
    already running the cartridge -- so "a peer is attached" and "the peer is a console
    running a game" are the same statement on this path. The silicon distinction the
    detect line exists to make (a peer that is merely powered, sitting in its BIOS,
    must NOT register) is unreachable online: there is no way to join a room from the
    BIOS. What the wire cannot tell us is the moment-to-moment RTS, so this asserts
    the steady state -- peer present and ready -- and drops it the moment the peer goes.

    ⚠️ NOT A RETURN TO THE OLD MODEL. The old one derived the bit from "the host
    attached a cable", so it read "peer present" on a lone console with a cable armed
    and no one at the far end (`B1=0x02` where silicon reads `0x07`). This is driven by
    an actual paired peer, and `present=False` on loss makes the cut visible to the
    game -- which is how Match of the Millennium raises its own LINK ERROR.
    """
    try:
        machine.serial_set_cts(not present)
    except Exception:                       # noqa: BLE001
        # A monitor's stand-in machine, or a core too old to have the pin. A link that
        # cannot say this must still carry bytes.
        pass


# ⚡ HOW OFTEN TWO CONSOLES IN ONE PROCESS MUST BE INTERLEAVED, in instructions.
#
# Running one console's whole frame while the other stands still puts a frame of
# latency on the answer, in one direction, always -- and that is enough to lose Card
# Fighters' Clash's VS handshake (its packet reader gives up when the next byte is not
# already in the BIOS ring). Both places that own two consoles use this: the shell's
# local 2-player cable (PlayPage.LINK_SLICE) and mirror netplay
# (core.netplay.run_two_consoles_interleaved).
#
# ⚠️ THIS NUMBER IS A CORRECTNESS FIGURE, NOT A TUNING KNOB -- The Last Blade needs it
# no coarser than 400 (2000 already fails it). See PlayPage.LINK_SLICE for that table.
CABLE_SLICE = 400

# A frame is a few thousand instructions, so this is a runaway backstop with a wide
# margin, not a target: whatever happens, the caller finishes the frame the plain way.
_MAX_SLICES = 256


def host_relay_forced() -> bool:
    """True when the player asked for the PREVIOUS arrangement: a slice of instructions
    each, relayed from Python.

    ⚡ AN A/B SWITCH, AND IT EXISTS FOR A REASON. Moving the pair into the core changed
    how two cabled consoles are scheduled, and the games that care about that are exactly
    the ones whose failures are hard to reproduce headlessly -- a probe ROM that transmits
    non-stop cannot tell the two schedulings apart at all. So when a link game misbehaves,
    the first question is "does the old path still do it?", and answering it must not take
    a rebuild:

        set NGPCRAFT_HOST_RELAY=1

    Answering that question is the whole point; leave it unset for normal play."""
    return os.environ.get("NGPCRAFT_HOST_RELAY", "") not in ("", "0")


def run_two_consoles_interleaved(first, second, link) -> None:
    """Advance both consoles by one frame, a slice at a time, relaying between slices.

    `first` runs first within each slice round -- callers that must agree bit for bit
    across two PCs (mirror netplay) have to pass the SAME console first on both.

    Machines without the sliced `run` interface (test doubles) fall back to a whole
    frame each, which is what this code did everywhere before.

    ⚡ `link.observer_a`, when set, is a LinkMonitor that only COUNTS -- the debugger's
    tap on a mirror cable. It is read around the frame, never consulted to pick the
    path below.

    ⛔ WHY NOT `monitor_a`. A monitor sends the pair down the host relay, and in mirror
    play only ONE PC has the debugger open: that PC steps the cable differently from
    the other, and the two drift. Measured on the probe ROM, four consoles: the RAM
    parted company on the very frame the tap went in, checksum desync 90 frames later
    -- and the debugger attaches its tap just by refreshing the Link tab. Found by
    Blueteemo (Tixul/Ngpcraft_emulator PR #26), whose fix refused the match instead.
    """
    observer = getattr(link, "observer_a", None)
    if observer is None:
        _advance_pair(first, second, link)
        return
    ab, ba = getattr(link, "bytes_ab", 0), getattr(link, "bytes_ba", 0)
    _advance_pair(first, second, link)
    observer.on_counted(getattr(link, "bytes_ab", 0) - ab,
                        getattr(link, "bytes_ba", 0) - ba)


def _advance_pair(first, second, link) -> None:
    if not hasattr(first, "run") or not hasattr(second, "run"):
        first.run_frames(1)
        link.pump()
        second.run_frames(1)
        link.pump()
        return

    # ⚡ THE PAIR IS THE CORE'S JOB. Everything below this is the arrangement it
    # replaces: a slice of INSTRUCTIONS each, relayed from here in between. The core
    # paces the pair on the CABLE's own byte time instead and relays the moment either
    # console reports a byte moved, so the two can never be more than one quantum of
    # emulated time apart. See ngpc_run_linked and LINK_NETPLAY_STUDY.md L3.
    #
    # ⛔ AND IT SETTLES A DIVERGENCE RATHER THAN INHERITING ONE. This function's relay
    # (InProcessLink._relay) refuses to push at a console holding RTS high; the shell's
    # local two-player path and the Android front end both push UNCONDITIONALLY, on the
    # grounds that serial_tick is the real gate and that holding back here can strand a
    # handshake byte. Project memory records that as unsettled and says to measure. The
    # core relays unconditionally, so mirror sessions now use the same rule as the two
    # paths that have been played on real games -- measured on the probe ROM as the same
    # byte counts either way, with the two consoles ending perfectly in step.
    #
    # ⛔ A MONITOR KEEPS THE OLD PATH. It records, delays, drops and injects bytes from
    # Python; a relay inside the core cannot call it.
    if (not host_relay_forced()
            and getattr(link, "monitor_a", None) is None
            and getattr(link, "monitor_b", None) is None
            and hasattr(first, "serial_state") and hasattr(second, "serial_state")):
        from core import native

        a_before = first.serial_state().wire_count
        b_before = second.serial_state().wire_count
        native.run_linked(first, second, 1)
        # The relay's own byte counters are what the UI and several tests read; the core
        # is now the thing that moves bytes, so they come from its count of what actually
        # finished shifting out. Leaving them behind would freeze a number that is
        # supposed to say whether the cable is alive.
        link.bytes_ab += first.serial_state().wire_count - a_before
        link.bytes_ba += second.serial_state().wire_count - b_before
        return

    pair = (first, second)
    starts = [m.run(0, record=False)[0].frame_count for m in pair]
    done = [False, False]
    for _ in range(_MAX_SLICES):
        for i, m in enumerate(pair):
            if done[i]:
                continue
            summ, _ = m.run(CABLE_SLICE, record=False)
            if summ.executed == 0 or summ.frame_count != starts[i]:
                done[i] = True          # this console's frame is finished
        link.pump()
        if all(done):
            return
    for i, m in enumerate(pair):        # never leave a frame half-run
        if not done[i]:
            m.run_frames(1)
    link.pump()


class InProcessLink:
    """Cable between two machines living in the same process.

    ``pump()`` drains each machine's transmit FIFO and feeds it to the other,
    honouring the receiver's RTS: a machine holding RTS high (COMOFFRTS, "not
    ready") does not get bytes pushed at it, so they wait in the sender's FIFO --
    the same back-pressure the real handshake provides. Call once per frame,
    after stepping both machines.
    """

    def __init__(self, machine_a: _SerialMachine, machine_b: _SerialMachine, *,
                 monitor_a=None, monitor_b=None):
        self.a = machine_a
        self.b = machine_b
        # Optional core.link_debug.LinkMonitor per console: watches the bytes
        # (and, if the user asked for it, delays or drops them). None = a perfect
        # wire nobody is looking at, which is the normal case.
        self.monitor_a = monitor_a
        self.monitor_b = monitor_b
        # A count-only tap, for a cable that must not be touched (mirror play; see
        # run_two_consoles_interleaved). Read by that function, not by pump().
        self.observer_a = None
        self.a.serial_set_enabled(True)
        self.b.serial_set_enabled(True)
        self.bytes_ab = 0
        self.bytes_ba = 0

    @staticmethod
    def _relay(src: _SerialMachine, dst: _SerialMachine,
               tx_monitor=None, rx_monitor=None) -> int:
        if not dst.serial_rts():        # receiver is holding the sender off
            return 0
        buf = bytearray()
        while True:
            data = src.serial_read_tx(_DRAIN_CHUNK)
            if not data:
                break
            buf += data
        # One monitor call per pump, even for an empty drain: a monitor holding
        # bytes back for latency releases them on the call, so skipping it when
        # nothing new was sent would strand them until the next byte moved.
        out = tx_monitor.on_tx(bytes(buf)) if tx_monitor is not None else bytes(buf)
        if not out:
            return 0
        dst.serial_write_rx(out)
        if rx_monitor is not None:
            rx_monitor.on_rx(out)
        return len(out)

    def pump(self) -> None:
        # Cross-wire the hardware handshake: each console's CTS0 pin is the OTHER
        # console's RTS line (datasheet 3.11: RTS is any GPIO -> the peer's CTS0).
        # A machine ready to receive (RTS low) pulls the peer's CTS0 low, letting the
        # peer's CTSE-gated transmitter send. Without this the transmitter was never
        # held and a game that leans on the handshake (Card Fighters' Clash) could not
        # keep the two consoles in step. serial_rts() is True when RTS is LOW.
        self.a.serial_set_cts(not self.b.serial_rts())
        self.b.serial_set_cts(not self.a.serial_rts())
        self.bytes_ab += self._relay(self.a, self.b, self.monitor_a, self.monitor_b)
        self.bytes_ba += self._relay(self.b, self.a, self.monitor_b, self.monitor_a)
        # Bytes the debugger typed in by hand reach the console they were aimed
        # at, peer or no peer (core.link_debug.deliver_injected).
        deliver_injected(self.a, self.monitor_a)
        deliver_injected(self.b, self.monitor_b)

    def disconnect(self) -> None:
        self.a.serial_set_enabled(False)
        self.b.serial_set_enabled(False)


# The first record a MIRROR-mode emulator sends (core/netplay.py: _HDR "<BI" with
# _T_HELLO = 1 and frame 0, then a 2-byte length and the JSON hello). Spelled out here
# rather than imported: netplay imports this module.
_MIRROR_HELLO_HEAD = b"\x01\x00\x00\x00\x00"


def _is_mirror_hello(data: bytes) -> bool:
    head = len(_MIRROR_HELLO_HEAD)
    return data.startswith(_MIRROR_HELLO_HEAD) and data[head + 2:head + 4] == b'{"'


class TcpLink:
    """Cable between one local machine and a remote peer over TCP.

    Symmetric: run one on each end (one side ``listen()``, the other
    ``connect()``); both then call ``pump()`` once per frame. Local transmit
    bytes go out on the socket; bytes arriving on the socket are delivered to the
    local machine's receive FIFO when its RTS allows. The socket is non-blocking;
    ``pump()`` never stalls the emulation.
    """

    def __init__(self, machine: _SerialMachine, sock: socket.socket, *, monitor=None):
        self.machine = machine
        self.sock = sock
        self.sock.setblocking(False)
        # Optional core.link_debug.LinkMonitor: watches both directions and can
        # impair the outgoing one (latency/loss) to rehearse a bad connection.
        self.monitor = monitor
        self.machine.serial_set_enabled(True)
        declare_peer(self.machine, True)
        self._rx = bytearray()
        self._out = bytearray()     # written but not yet accepted by the kernel
        self.bytes_out = 0
        self.bytes_in = 0
        # ⚡ WHY THE PEER WENT AWAY, once it has -- and None while the cable is good.
        #
        # ⛔ THE CRASH THIS ENDS. "Sometimes, some games lost the connection between
        # users playing in online mode and then, the emulator crashes." pump() caught
        # only BlockingIOError/InterruptedError, which are the "try again" cases. A peer
        # that GOES is a different family -- ConnectionResetError, BrokenPipeError,
        # ConnectionAbortedError -- and those escaped. MEASURED, both ways a peer can
        # vanish (a clean FIN and an RST from a kill/NAT drop): ConnectionResetError out
        # of the FIRST pump after the peer left, every time.
        #
        # That pump runs inside PlayPage._tick, which is a QTimer slot, and PyQt answers
        # an unhandled Python exception in a slot with qFatal(). MEASURED: the process
        # dies with 0xC0000409 and prints no traceback -- which is exactly why this reads
        # as "the emulator crashes" rather than as an error. (Same mechanism the root
        # conftest documents for the test runner.)
        #
        # So the link records the loss instead of raising it, and stops touching a dead
        # socket. The owner polls this and can say so; the emulation is never disturbed.
        self.lost: str | None = None
        self._sniffed = False       # the first bytes were checked (see _mirror_hello)
        self._sniff_pumps = 0

    # --- connection helpers -------------------------------------------------
    @classmethod
    def listen(cls, machine: _SerialMachine, host: str, port: int) -> "TcpLink":
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind((host, port))
        srv.listen(1)
        conn, _ = srv.accept()          # blocks until the peer connects
        srv.close()
        conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        return cls(machine, conn)

    @classmethod
    def connect(cls, machine: _SerialMachine, host: str, port: int) -> "TcpLink":
        conn = socket.create_connection((host, port))
        conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        return cls(machine, conn)

    def _lose(self, why: str) -> None:
        """The peer is gone. Record it once and let go of the socket."""
        if self.lost is None:
            self.lost = why or "peer closed"
            from core import netlog
            netlog.log("cable_lost", why=self.lost, bytes_in=self.bytes_in,
                       bytes_out=self.bytes_out)
        # The console must SEE the cut. Dropping the detect line is what a yanked cable
        # does on silicon, and it is how a game raises its own link error instead of
        # waiting for ever on a peer that has gone. (The shell also detaches the link,
        # which disables the channel; this stands on its own so a transport that is
        # pumped by anything else behaves the same.)
        declare_peer(self.machine, False)
        try:
            self.sock.close()
        except OSError:
            pass

    # --- per-frame pump -----------------------------------------------------
    def pump(self) -> None:
        if self.lost is not None:
            return                      # a dead cable carries nothing; do not touch it
        # 1) local TX -> socket. Drained into one buffer so the monitor sees one
        # handover per pump (and can release bytes it is holding back for latency
        # even on a pump where the game sent nothing).
        buf = bytearray()
        while True:
            data = self.machine.serial_read_tx(_DRAIN_CHUNK)
            if not data:
                break
            buf += data
        outgoing = self.monitor.on_tx(bytes(buf)) if self.monitor is not None else bytes(buf)
        self._out += outgoing
        if self._out:
            # ⛔ NOT `sendall` ON A NON-BLOCKING SOCKET. It raises BlockingIOError the
            # moment the kernel buffer fills and does NOT say how much of the buffer it
            # already handed over -- so the old code dropped the whole write, mid-stream,
            # and a link cable that loses bytes is a game that desyncs or hangs waiting
            # for a packet that was never sent. (core/lobby.py fixed exactly this on the
            # lobby socket; the direct host/join socket still had it.)
            #
            # `send` reports what it took. Keep the rest and offer it again next pump --
            # the frame is relayed several times now (PlayPage._run_one_frame), so a full
            # buffer costs a fraction of a frame, not a byte.
            try:
                sent = self.sock.send(self._out)
                self.bytes_out += sent
                del self._out[:sent]
            except (BlockingIOError, InterruptedError):
                pass                # buffer full: nothing taken, nothing lost, try again
            except OSError as e:
                # ...whereas this family IS the peer going away. Note it and stop.
                self._lose(str(e))
                return

        # 2) socket -> local buffer
        try:
            while True:
                chunk = self.sock.recv(4096)
                if not chunk:
                    # A clean FIN. This used to just leave the loop, so a peer that
                    # quit tidily was indistinguishable from one with nothing to say
                    # and the game sat there waiting for a console that had gone.
                    self._lose("peer closed the connection")
                    break
                self._rx.extend(chunk)
        except (BlockingIOError, InterruptedError):
            pass
        except OSError as e:
            self._lose(str(e))

        # 2b) THE OTHER PLAYER CHOSE MIRROR. Its first words are a mirror hello, not a
        # game's serial bytes; fed to the console they are noise, and the mirror side
        # then hangs up on us -- which is all this side could ever report. Recognise
        # the hello and say which mode the other player is in.
        if not self._sniffed and self._rx:
            self._sniff_pumps += 1
            if (len(self._rx) < len(_MIRROR_HELLO_HEAD) + 4
                    and self._rx[0] == _MIRROR_HELLO_HEAD[0] and self._sniff_pumps < 30):
                return                  # too short to tell yet: decide next pump (bounded:
                                        # a game whose first byte IS 0x01 still gets it)
            self._sniffed = True
            if _is_mirror_hello(bytes(self._rx)):
                self._rx.clear()
                self._lose("peer_mirror_mode")
                return

        # 3) buffer -> local RX FIFO. Push unconditionally: the core's serial_tick
        # is the flow-control gate (it only PRESENTS a byte to the CPU once our RTS
        # is low), so delivering early just queues it like a real cable. Gating here
        # on RTS could strand a handshake byte and read as "no cable".
        if self._rx:
            self.machine.serial_write_rx(bytes(self._rx))
            self.bytes_in += len(self._rx)
            if self.monitor is not None:
                self.monitor.on_rx(bytes(self._rx))
            self._rx.clear()

        # 4) anything the debugger typed in by hand, as if the peer had sent it.
        deliver_injected(self.machine, self.monitor)

    def disconnect(self) -> None:
        self.machine.serial_set_enabled(False)
        try:
            self.sock.close()
        except OSError:
            pass


# ---------------------------------------------------------------------------
# NGPC <-> DREAMCAST: the Flycast "BattleCable" null modem
# ---------------------------------------------------------------------------
# The NGPC <-> Dreamcast cable plugs the console's serial channel 0 into the
# Dreamcast's SCIF port. Flycast emulates that port and carries it over UDP
# (core/network/null_modem.h, Flycast 2.x), with the most trivial framing there is:
#
#     b"D" + <byte>     one data byte          (2-byte datagram)
#     b"B"              a break condition      (1-byte datagram)
#
# no handshake, no sequence number, no checksum. Flycast binds `[network] LocalPort`
# (default 37391) and sends to `[network] server = host:port`; with `server` empty it
# BROADCASTS to port 37391 and adopts whoever answers first. Its SCIF reports CTS
# always active, so no flow-control line travels either way -- which is why the
# detect line is asserted here as soon as the link exists (see `declare_peer`).
#
# Launch Flycast with, for instance:
#     flycast.exe -config "network:BattleCable=yes,network:server=127.0.0.1:42500" game.chd
# and attach a FlycastLink bound on 42500, sending to 127.0.0.1:37391.
#
# UDP on localhost does not lose or reorder datagrams in practice; across a real
# network it can, and Flycast's own NGPC-less use (two Dreamcasts) has the same
# exposure -- this transport adds nothing to it and hides nothing from it.

FLYCAST_DEFAULT_PORT = 37391        # Flycast's [network] LocalPort default
FLYCAST_NGPC_PORT = 42500           # where this side listens (Flycast's `server`)


class FlycastLink:
    """Cable between one local machine and a Dreamcast running in Flycast.

    Same interface as :class:`TcpLink` (``pump()`` / ``disconnect()`` /
    ``bytes_out`` / ``bytes_in`` / ``lost`` / ``monitor``), so the shell attaches
    it with ``attach_net_link`` like any online cable.
    """

    def __init__(self, machine: _SerialMachine, *, peer: tuple[str, int] = ("127.0.0.1", FLYCAST_DEFAULT_PORT),
                 local_port: int = FLYCAST_NGPC_PORT, bind_host: str = "0.0.0.0", monitor=None):
        self.machine = machine
        self.peer = peer
        self.monitor = monitor
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind((bind_host, local_port))
        self.sock.setblocking(False)
        self.machine.serial_set_enabled(True)
        declare_peer(self.machine, True)
        self.bytes_out = 0
        self.bytes_in = 0
        self.breaks_in = 0          # the NGPC has no break line: counted, then dropped
        self.lost: str | None = None
        self.heard_from: tuple[str, int] | None = None

    def pump(self) -> None:
        if self.lost is not None:
            return
        # 1) local TX -> one datagram per byte, exactly what Flycast's NullModemPipe reads.
        buf = bytearray()
        while True:
            data = self.machine.serial_read_tx(_DRAIN_CHUNK)
            if not data:
                break
            buf += data
        outgoing = self.monitor.on_tx(bytes(buf)) if self.monitor is not None else bytes(buf)
        try:
            for b in outgoing:
                self.sock.sendto(bytes((0x44, b)), self.peer)       # b"D" + byte
                self.bytes_out += 1
        except (BlockingIOError, InterruptedError):
            pass        # localhost UDP buffer full: practically never; the byte is lost
        except ConnectionResetError:
            pass        # Windows: ICMP "port unreachable" from a Flycast not started yet
        except OSError as e:
            self._lose(str(e))
            return
        # 2) datagrams -> RX bytes
        rx = bytearray()
        while True:
            try:
                pkt, addr = self.sock.recvfrom(64)
            except (BlockingIOError, InterruptedError):
                break
            except ConnectionResetError:
                continue    # same ICMP echo of an earlier send; the socket is fine
            except OSError as e:
                self._lose(str(e))
                return
            if self.heard_from is None:
                self.heard_from = addr
            if len(pkt) == 2 and pkt[0] == 0x44:
                rx.append(pkt[1])
            elif pkt == b"B":
                self.breaks_in += 1
        # 3) RX -> the console's receive FIFO, unconditionally (serial_tick is the gate).
        if rx:
            self.machine.serial_write_rx(bytes(rx))
            self.bytes_in += len(rx)
            if self.monitor is not None:
                self.monitor.on_rx(bytes(rx))
        deliver_injected(self.machine, self.monitor)

    def _lose(self, why: str) -> None:
        if self.lost is None:
            self.lost = why or "flycast link closed"
        declare_peer(self.machine, False)
        try:
            self.sock.close()
        except OSError:
            pass

    def disconnect(self) -> None:
        self.machine.serial_set_enabled(False)
        try:
            self.sock.close()
        except OSError:
            pass
