"""Link cable (serial channel 0) — the emulated cable is a byte pipe.

Two layers are covered:

* the :class:`core.link.InProcessLink` relay logic, with fake machines (always
  runs, no ROM/BIOS needed);
* the full hardware path end to end: two real machines running the retail BIOS
  COM routines, wired by an InProcessLink, must each receive the byte the OTHER
  transmitted. This needs the real BIOS (COMINIT lives in it) and the compiled
  probe ROM, so it SKIPS when either is absent.

The probe ROM (tests/roms/link_probe.ngc, source link_probe_main.c) sends its raw
controller byte (port 0xB0, injected per-machine here) every loop and records the
last byte it received plus a running total, at these globals (from its .map):

    g_last_rx  @ 0x400A   (u8)
    g_rx_total @ 0x400C   (u16, little-endian)
    g_tx_count @ 0x400E   (u16)
"""

from __future__ import annotations

from pathlib import Path

import pytest

from core.link import InProcessLink

REPO = Path(__file__).resolve().parent.parent
BIOS = REPO / "bios.bin"
ROM = REPO / "tests" / "roms" / "link_probe.ngc"

G_LAST_RX = 0x400A
G_RX_TOTAL = 0x400C
G_TX_COUNT = 0x400E


# --------------------------------------------------------------------------- #
# 1. Relay logic — fake machines, no core needed.
# --------------------------------------------------------------------------- #
class FakeSerial:
    """A minimal machine exposing the serial slice InProcessLink uses."""

    def __init__(self, rts: bool = True):
        self.tx = bytearray()      # bytes this machine "transmitted"
        self.rx = bytearray()      # bytes delivered to this machine
        self._rts = rts
        self.enabled = False

    def serial_set_enabled(self, on: bool) -> None:
        self.enabled = on

    def serial_read_tx(self, max_bytes: int = 256) -> bytes:
        out = bytes(self.tx[:max_bytes])
        del self.tx[: len(out)]
        return out

    def serial_write_rx(self, data: bytes) -> None:
        self.rx.extend(data)

    def serial_rts(self) -> bool:
        return self._rts

    def serial_set_cts(self, high: bool) -> None:
        # CTS0 handshake input, driven by the peer's RTS. Recorded so a test can
        # assert the bridge cross-wires it; the byte relay itself is unaffected.
        self.cts_high = high


def test_inprocess_link_relays_both_directions():
    a, b = FakeSerial(), FakeSerial()
    link = InProcessLink(a, b)
    assert a.enabled and b.enabled            # constructor arms the hardware path

    a.tx.extend(b"\x11")
    b.tx.extend(b"\x22")
    link.pump()

    assert bytes(b.rx) == b"\x11"             # A's byte reached B
    assert bytes(a.rx) == b"\x22"             # B's byte reached A
    assert link.bytes_ab == 1 and link.bytes_ba == 1


def test_inprocess_link_honours_rts_backpressure():
    """A receiver holding RTS high must not be fed; the bytes wait in the sender."""
    a = FakeSerial()
    b = FakeSerial(rts=False)                 # B is NOT ready to receive
    link = InProcessLink(a, b)

    a.tx.extend(b"\x5A")
    link.pump()

    assert bytes(b.rx) == b""                 # nothing pushed at B
    assert bytes(a.tx) == b"\x5A"             # still queued in A (back-pressure)

    b._rts = True                             # B lowers RTS (COMONRTS)
    link.pump()
    assert bytes(b.rx) == b"\x5A"             # now it flows


# --------------------------------------------------------------------------- #
# 2. Full hardware path — real BIOS COM routines, two machines.
# --------------------------------------------------------------------------- #
requires_rom = pytest.mark.skipif(
    not (BIOS.exists() and ROM.exists()),
    reason="needs the retail bios.bin (gitignored) and the probe ROM",
)


@requires_rom
def test_link_cable_bidirectional_hardware_path():
    from core.native_session import NativeSession

    def rd8(m, addr):
        return m.read(addr, 1)[0]

    def rd16(m, addr):
        d = m.read(addr, 2)
        return d[0] | (d[1] << 8)

    pad_a, pad_b = 0x11, 0x22
    a = NativeSession(ROM, bios_path=BIOS, autosave=False)
    b = NativeSession(ROM, bios_path=BIOS, autosave=False)
    link = InProcessLink(a.machine, b.machine)

    for _ in range(400):
        a.machine.write(0x00B0, bytes([pad_a]))
        b.machine.write(0x00B0, bytes([pad_b]))
        a.run_frames(1)
        b.run_frames(1)
        link.pump()

    # Each console received the OTHER's controller byte, continuously.
    assert rd8(a.machine, G_LAST_RX) == pad_b
    assert rd8(b.machine, G_LAST_RX) == pad_a
    assert rd16(a.machine, G_RX_TOTAL) > 100
    assert rd16(b.machine, G_RX_TOTAL) > 100
    assert link.bytes_ab > 100 and link.bytes_ba > 100


@requires_rom
def test_cts_going_high_mid_byte_does_not_swallow_that_byte():
    """⚡ CTS0 gates the START of a byte. It cannot pause one already going out.

    TMP95C061 datasheet 3.11: "when the CTS0 pin goes high, AFTER COMPLETION OF THE
    CURRENT DATA SEND, data send is halted", and Note 1 of fig 3.11(16) again: "if
    the CTS signal rises during transmission, the NEXT data is not sent after the
    completion of the current transmission". A shift register that has begun cannot
    be stopped.

    The core used to re-test CTS every tick and freeze the in-flight byte's
    countdown, so a peer pulsing its RTS high for a fraction of a frame stalled a
    byte hardware would have finished -- measured at 6001 held ticks inside one frame
    of a Card Fighters' Clash VS exchange, dragging a 26-byte packet across three
    frames instead of two.

    So: start a byte, raise CTS while it is on the wire, and it must still arrive.
    The byte AFTER it is the one that waits.
    """
    from core.native_session import NativeSession

    # The probe ROM's own traffic is the source of bytes: a host poke of SC0BUF is
    # storage, not the CPU action that loads the shift register.
    s = NativeSession(ROM, bios_path=BIOS, autosave=False)
    m = s.machine
    m.serial_set_enabled(True)
    m.serial_set_cts(False)                 # peer ready
    m.write(0x00B0, bytes([0x11]))          # something for the probe to send
    for _ in range(90):                     # let it boot and start talking
        m.run_frames(1)
        m.serial_read_tx()
    assert m.serial_state().ctse == 1, "COMINIT should have enabled the handshake"

    # Catch a byte in mid-flight...
    for _ in range(20000):
        m.run(20, record=False)
        if m.serial_state().tx_busy == 1:
            break
    assert m.serial_state().tx_busy == 1, "the probe never put a byte on the wire"

    # ...raise CTS on it, and it must still land. A byte-time is 3200 cycles, so
    # ~1200 instructions is comfortably more than one.
    sent = m.serial_state().wire_count
    m.serial_set_cts(True)
    m.run(1200, record=False)
    assert m.serial_state().wire_count == sent + 1, (
        "a byte already shifting must complete -- CTS gates the NEXT one")

    # CONTROL: with CTS still high, the wire now goes quiet. Without this the test
    # would pass just as well against a core with no handshake at all.
    sent = m.serial_state().wire_count
    for _ in range(4):
        m.run_frames(1)
    assert m.serial_state().wire_count == sent, "CTS high must hold bytes not yet started"

    m.serial_set_cts(False)
    for _ in range(4):
        m.run_frames(1)
    assert m.serial_state().wire_count > sent, "...and release them once the peer is ready"


@requires_rom
def test_tcp_link_relays_over_socket():
    """The online path: two consoles wired by core.link.TcpLink over a real socket
    pair each receive the other's transmitted controller byte."""
    import socket
    from core.native_session import NativeSession
    from core.link import TcpLink

    sock_a, sock_b = socket.socketpair()
    a = NativeSession(ROM, bios_path=BIOS, autosave=False)
    b = NativeSession(ROM, bios_path=BIOS, autosave=False)
    link_a = TcpLink(a.machine, sock_a)
    link_b = TcpLink(b.machine, sock_b)
    try:
        for _ in range(400):
            a.machine.write(0x00B0, bytes([0x11]))
            b.machine.write(0x00B0, bytes([0x22]))
            a.run_frames(1)
            b.run_frames(1)
            link_a.pump()
            link_b.pump()

        assert a.machine.read(0x400A, 1)[0] == 0x22   # A got B's byte over TCP
        assert b.machine.read(0x400A, 1)[0] == 0x11
        assert link_a.bytes_out > 50 and link_b.bytes_out > 50
        assert link_a.bytes_in > 50 and link_b.bytes_in > 50
    finally:
        link_a.disconnect()
        link_b.disconnect()


# --------------------------------------------------------------------------- #
# 3. The same cable, with NO real BIOS — the clean-room HLE image drives it.
# --------------------------------------------------------------------------- #
HLE_IMAGE = REPO / "hle_bios" / "bios_hle.bin"

requires_hle = pytest.mark.skipif(
    not (HLE_IMAGE.exists() and ROM.exists()),
    reason="needs hle_bios/bios_hle.bin and the probe ROM",
)


@requires_hle
def test_link_cable_works_without_a_real_bios():
    """Two consoles on the HLE image must exchange bytes exactly as on the retail
    BIOS. The COM vectors used to be "cable idle" stubs, so nothing was ever handed
    to SC0BUF and NOT ONE BYTE went on the wire — while the game's own TX counter
    kept climbing, which is why a counter inside the game proves nothing.

    This needs the whole chain: COMINIT arming INTES0 and lowering IFF, the rings at
    0x6C80/0x6CC0, the RX count at 0x6D01 the probe reads itself, and the two serial
    ISRs — which are CROSS-WIRED versus their SDK names (0x18 receives, 0x19
    transmits). Wire them by name instead and each console receives its OWN byte:
    the assertions below are on the OTHER console's byte precisely so that a
    self-loopback fails the test.
    """
    from core.native_session import NativeSession

    a = NativeSession(ROM, bios_path=HLE_IMAGE, autosave=False)
    b = NativeSession(ROM, bios_path=HLE_IMAGE, autosave=False)
    link = InProcessLink(a.machine, b.machine)

    for _ in range(400):
        a.machine.write(0x00B0, bytes([0x11]))
        b.machine.write(0x00B0, bytes([0x22]))
        a.run_frames(1)
        b.run_frames(1)
        link.pump()

    def rd16(m, addr):
        d = m.read(addr, 2)
        return d[0] | (d[1] << 8)

    assert a.machine.read(G_LAST_RX, 1)[0] == 0x22, "A did not receive B's byte"
    assert b.machine.read(G_LAST_RX, 1)[0] == 0x11, "B did not receive A's byte"
    assert rd16(a.machine, G_RX_TOTAL) > 100
    assert rd16(b.machine, G_RX_TOTAL) > 100
    assert link.bytes_ab > 100 and link.bytes_ba > 100, "nothing reached the wire"


@requires_hle
def test_hle_link_is_not_a_loopback():
    """Enabled, but with no peer bridging its TX: it must receive nothing."""
    from core.native_session import NativeSession

    a = NativeSession(ROM, bios_path=HLE_IMAGE, autosave=False)
    a.machine.serial_set_enabled(True)
    for _ in range(200):
        a.machine.write(0x00B0, bytes([0x33]))
        a.run_frames(1)

    d = a.machine.read(G_RX_TOTAL, 2)
    assert (d[0] | (d[1] << 8)) == 0, "the console received its own transmission"


@requires_rom
def test_link_cable_disabled_is_unplugged():
    """With no peer wired in, a machine must never receive its own transmission
    (the self-loopback the cross-wired serial vectors originally caused)."""
    from core.native_session import NativeSession

    def rd16(m, addr):
        d = m.read(addr, 2)
        return d[0] | (d[1] << 8)

    a = NativeSession(ROM, bios_path=BIOS, autosave=False)
    a.machine.serial_set_enabled(True)        # enabled, but nothing bridges its TX
    for _ in range(200):
        a.machine.write(0x00B0, bytes([0x33]))
        a.run_frames(1)

    assert rd16(a.machine, G_RX_TOTAL) == 0    # received nothing — no loopback


def _bare_rom(size: int = 0x10000) -> bytes:
    """A cartridge that is only a valid header -- enough to build a machine."""
    rom = bytearray(b"\xFF" * size)
    rom[0:28] = b" LICENSED BY SNK CORPORATION"
    rom[0x1C:0x20] = (0x200040).to_bytes(4, "little")
    rom[0x23] = 0x10
    rom[0x40:0x44] = bytes(4)
    return bytes(rom)


def test_sc0buf_reads_the_receive_buffer_not_the_byte_we_transmitted():
    """⚡ SC0BUF is TWO registers on one address: a write loads the TRANSMIT buffer,
    a read returns the RECEIVE buffer. The CPU cannot read back what it sent.

    ⛔ THE BUG THIS ENDS. The core returned the received byte only while the
    "new data" flag was set, and fell back to the I/O page afterwards -- where a
    TRANSMITTED byte was sitting. A receive handler that touches SC0BUF more than
    once for one byte (the retail BIOS's COM ISR does) therefore stored THE LAST
    BYTE WE SENT in place of the byte that arrived.

    MEASURED end to end on Card Fighters' Clash, two consoles, NORMAL MATCH: 532
    bytes queued, 532 read, 532 appended to player 2's BIOS ring -- nothing lost,
    nothing duplicated -- and byte 508 arrived as 0xA5 where 0x00 was sent. That one
    wrong byte failed the packet's checksum (`cp H,A` at 0x24260B), player 2 dropped
    the packet in silence and never answered, and both consoles waited for each other
    for ever on CHOOSE FIRST PLAYER. Phase-dependent, hence "it hangs on the first
    try and works on the second".
    """
    from core import native

    m = native.NativeMachine(_bare_rom())
    m.serial_set_enabled(True)
    m.write(0x0000B2, bytes([0x00]))      # our RTS low: ready to receive
    m.write(0x000050, bytes([0xA5]))      # a byte we transmitted, left in the I/O page
    m.serial_write_rx(bytes([0x00]))      # ...and a DIFFERENT byte arrives
    for _ in range(200):
        m.run(64, record=False)
        if m.serial_state().rx_pending:
            break
    assert m.serial_state().rx_pending == 1, "the core never presented the byte"

    assert m.read(0x50, 1)[0] == 0x00, "the first read must return what arrived"
    assert m.read(0x50, 1)[0] == 0x00, (
        "the receive buffer holds its byte -- a second read must not hand back "
        "the byte we transmitted")
    # A HOST read (this one) leaves the 'new data' flag to the game: only the CPU
    # consumes it. See test_host_read_of_sc0buf_does_not_consume_the_received_byte.
    assert m.serial_state().rx_pending == 1
    assert m.serial_state().rx_read_count == 0


def test_host_read_of_sc0buf_does_not_consume_the_received_byte():
    """⛔ The debugger, the rewind history, run-ahead and libretro's serialize all read
    memory from address 0, through SC0BUF. That read used to clear the "new data" flag
    the GAME was about to read.

    MEASURED 2026-10-24, mirror netplay: the rewind history captures only the local
    console, so each PC ate received bytes on a different console -- Dynamite Slugger
    and Neo Geo Cup '98 Plus desynced the moment their link started and never linked.
    """
    from core import native

    m = native.NativeMachine(_bare_rom())
    m.serial_set_enabled(True)
    m.write(0x0000B2, bytes([0x00]))
    m.serial_write_rx(bytes([0x5A]))
    for _ in range(200):
        m.run(64, record=False)
        if m.serial_state().rx_pending:
            break
    assert m.serial_state().rx_pending == 1, "the core never presented the byte"
    assert m.read(0, 0x100)[0x50] == 0x5A
    assert m.serial_state().rx_pending == 1, "a host read took the byte from the game"
    assert m.serial_state().rx_read_count == 0


# ------------------------------------------------------------------------------
# ⚠️ WHY THERE IS NO RUNTIME TEST FOR THE BYTE TIME (and please do not re-attempt
# it the same three ways).
#
# `Machine::serial_byte_cycles()` computes one byte-time from SC0MOD and BR0CR
# instead of returning the old constant (TMP95C061 datasheet 3.11; the derivation is
# written above `kSerialByteCycles` in machine.hpp, and in specs/LINK_CABLE.md 2.2).
# A condemning test would show BR0CR = 0x15 pacing four times slower than 0x05.
# Three environments were tried and none can time it:
#
#   * host-driven transmit -- `machine.write(0x50, ...)` does NOT arm a send; it
#     bypasses io_action_write. Only a real ROM can transmit.
#   * receive pacing on a synthetic cartridge -- with 0xFF filler the CPU decodes
#     junk that READS SC0BUF (eating the queued byte) and then wedges; with NOP
#     filler it wedges anyway after ~280 cycles, which is less than ONE byte-time.
#     No cycles run, so serial_tick is never called.
#   * receive pacing on the real BIOS + probe ROM -- works, but the probe ROM drives
#     the link itself and consumes the queued bytes, so the gaps are contaminated and
#     the slow (0x15) case starves before a second byte is presented.
#
# What IS verified: the arithmetic yields exactly 3200 for SC0MOD=0x69 / BR0CR=0x05
# (the values the BIOS writes for every cartridge, measured on ten), and the whole
# link sweep is byte-for-byte identical before and after the change. A real test
# wants a purpose-built probe ROM that programs BR0CR and reports its own timing --
# the same ROM that would finally validate the constant on silicon.
# ------------------------------------------------------------------------------
