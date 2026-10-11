"""Client for the NgpCraft link lobby + relay server (server/lobby_server.py).

`LobbyClient` owns a background socket thread and speaks the length-prefixed
frame protocol. The Qt side calls create()/refresh()/join() and receives lobby
updates via signals; serial bytes are exchanged through thread-safe buffers that
`LobbyLink.pump()` moves in and out of the emulated console each frame.

Wire protocol (mirrors the server): header = 1 byte TYPE + 4 bytes big-endian
LENGTH, then payload. TYPE 1 = JSON control, TYPE 2 = serial relay bytes.
"""

from __future__ import annotations

import json
import queue
import select
import socket
import threading
from collections import deque

from PyQt6.QtCore import QObject, pyqtSignal

from core import link
from core.link_debug import deliver_injected

FRAME_CONTROL = 1
FRAME_RELAY = 2
_HDR = 5
MAX_RECEIVE_FRAME = 1 << 20


class _Closed(Exception):
    """The socket loop is done, and why. Raised rather than returned so that every
    exit -- a dead socket, a closed one, a server hang-up -- lands on the SAME
    teardown. The old code broke out of the loop and fell through to the cleanup,
    which worked for every path it had thought of and skipped it for the one it
    had not."""


def _frame(ftype: int, payload: bytes) -> bytes:
    return bytes([ftype]) + len(payload).to_bytes(4, "big") + payload


class LobbyClient(QObject):
    """A connection to the lobby server. Signals are emitted from the socket
    thread; Qt delivers them to the main thread via queued connections."""

    connected = pyqtSignal()
    disconnected = pyqtSignal(str)     # reason
    game_list = pyqtSignal(list)       # [{"room","name","game","creator","private"}]
    created = pyqtSignal(str)          # room code
    joined = pyqtSignal(dict)          # {"role","peer","game"?,"name"?}
    peer_left = pyqtSignal()
    error = pyqtSignal(str)

    def __init__(self, host: str, port: int, pseudo: str):
        super().__init__()
        self._host = host
        self._port = port
        self._pseudo = pseudo[:24]
        self._sock: socket.socket | None = None
        self._out: queue.Queue[bytes] = queue.Queue()
        # Bytes handed to us that the socket has not taken yet -- queued here or held
        # in the pump's own buffer. The cable mode never needs this (one byte a frame),
        # but the MIRROR trades a whole cartridge through the same relay, and a sender
        # with no idea how far behind it is queues megabytes a second into a link that
        # cannot take them. Read through LobbyPipe.pending.
        self._owed = 0
        self._owed_lock = threading.Lock()
        self._rx_serial: deque[int] = deque()      # received serial bytes
        self._rx_serial_lock = threading.Lock()
        self._running = False
        # A close during an in-progress connect must not be undone when it finishes.
        self._closing = threading.Event()
        self._thread: threading.Thread | None = None
        # ⚡ THE WAKE-UP PIPE -- this is what keeps an online game at full speed.
        # The socket thread waits in select(); without a second thing to wait ON, a
        # byte queued from the Qt thread just after it went to sleep sat there for a
        # whole select timeout. That is not a rounding error: Windows quantises a
        # select timeout to the ~15.6 ms scheduler tick, so a 20 ms wait becomes ~31 ms,
        # each way, on top of the real network. Measured on loopback (no network at
        # all), one relayed byte took a median of 31 ms to cross -- 62 ms per exchange
        # that the wire never charged us for. A link game exchanges a byte per frame
        # and BLOCKS on the answer, so the game (and only the game -- the sound driver
        # runs free, which is why the audio still sounds right) runs at a fraction of
        # its speed. send_serial() now writes one byte here, select() returns at once,
        # and the frame leaves in microseconds.
        self._wake_r, self._wake_w = socket.socketpair()
        self._wake_w.setblocking(False)            # never stall the Qt thread
        self._wake_r.setblocking(False)

    # ---- lifecycle ---------------------------------------------------------
    def start(self) -> None:
        if self._closing.is_set() or self._thread is not None:
            return
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def close(self) -> None:
        self._closing.set()
        self._running = False
        self._wake()                       # so the thread leaves select() now
        try:
            if self._sock is not None:
                self._sock.close()
        except OSError:
            pass
        if self._thread is None:
            # A user can dismiss the dialog without ever starting the worker.
            for s in (self._wake_r, self._wake_w):
                try:
                    s.close()
                except OSError:
                    pass

    def _wake(self) -> None:
        """Nudge the socket thread out of select(). Called from the Qt thread."""
        try:
            self._wake_w.send(b"\x01")
        except OSError:
            pass                           # full (thread is behind) or closed: harmless

    # ---- control API (called from the Qt thread) ---------------------------
    @property
    def owed(self) -> int:
        """Bytes queued for the peer that have not reached the socket yet."""
        with self._owed_lock:
            return self._owed

    def _enqueue(self, frame: bytes) -> None:
        with self._owed_lock:
            self._owed += len(frame)
        self._out.put(frame)
        self._wake()

    def _send_control(self, obj: dict) -> None:
        self._enqueue(_frame(FRAME_CONTROL, json.dumps(obj).encode("utf-8")))

    def create(self, name: str, game: str, public: bool, password: str = "",
               mode: str = "cable", delay: int = 0) -> None:
        """Open a room. `mode` is which link it is for -- "cable" (the console's own
        serial bytes) or "mirror" (core/netplay session records). The relay carries
        both the same way, but the two clients must agree on what the bytes mean, so
        the room advertises it and the joiner follows. `delay` is the mirror's input
        delay: it has to be identical on both PCs, so the host's is the one that counts.
        """
        self._send_control({"op": "create", "pseudo": self._pseudo, "name": name,
                            "game": game, "public": public, "password": password,
                            "mode": mode, "delay": int(delay)})

    def refresh(self) -> None:
        self._send_control({"op": "list"})

    def join(self, room: str, password: str = "") -> None:
        self._send_control({"op": "join", "pseudo": self._pseudo,
                            "room": room, "password": password})

    def leave(self) -> None:
        self._send_control({"op": "leave"})

    # ---- serial relay (called from the Qt tick via LobbyLink) --------------
    def send_serial(self, data: bytes) -> None:
        if data:
            self._enqueue(_frame(FRAME_RELAY, data))

    def read_serial(self) -> bytes:
        with self._rx_serial_lock:
            if not self._rx_serial:
                return b""
            out = bytes(self._rx_serial)
            self._rx_serial.clear()
            return out

    # ---- socket thread -----------------------------------------------------
    def _run(self) -> None:
        reason = "closed"
        try:
            if self._closing.is_set():
                raise _Closed("closed")
            self._sock = socket.create_connection((self._host, self._port), timeout=15)
            if self._closing.is_set():
                raise _Closed("closed")
            self._sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            self._sock.setblocking(False)
            self._running = True
            if self._closing.is_set():
                raise _Closed("closed")
            self._send_control({"op": "hello", "pseudo": self._pseudo})
            self.connected.emit()
            self._pump(bytearray(), bytearray())
        except _Closed as e:
            reason = str(e)
        except Exception as e:
            # Malformed payloads or unexpected callbacks must still notify Qt.
            reason = f"client socket loop error: {type(e).__name__}: {e}"
        finally:
            self._running = False
            if self._sock is not None:
                try:
                    self._sock.close()
                except OSError:
                    pass
            for s in (self._wake_r, self._wake_w):
                try:
                    s.close()
                except OSError:
                    pass
            self.disconnected.emit(reason)

    def _pump(self, buf: bytearray, pending: bytearray) -> None:
        while self._running:
            # 1) send anything queued
            try:
                while True:
                    pending += self._out.get_nowait()
            except queue.Empty:
                pass
            if pending:
                try:
                    # NOT sendall(): this socket is non-blocking, where sendall's
                    # behaviour is undefined -- it raises BlockingIOError the moment
                    # the kernel buffer is full, and BlockingIOError IS an OSError, so
                    # a momentarily-full buffer used to be read as "the link dropped"
                    # AND ate the bytes it had already taken off the queue. send()
                    # reports what it took; the rest waits here for the next pass.
                    sent = self._sock.send(pending)
                    del pending[:sent]
                    with self._owed_lock:
                        self._owed = max(0, self._owed - sent)
                except (BlockingIOError, InterruptedError):
                    pass
                except OSError as e:
                    raise _Closed(str(e)) from e
            # 2) wait for: something to read, room to write (only while we owe bytes),
            # or the Qt thread waking us because it just queued a frame.
            try:
                r, _, _ = select.select(
                    [self._sock, self._wake_r], [self._sock] if pending else [],
                    [], 0.02)
            except (OSError, ValueError) as e:
                # ⚠️ ValueError, NOT only OSError. `close()` runs on the Qt thread and
                # can shut the socket between the `while self._running` test above and
                # this call. A closed socket's fileno() is -1, and select REJECTS a
                # negative descriptor as a bad ARGUMENT -- a ValueError, which this
                # only caught as OSError. The thread then died on the way out, so the
                # wake socketpair leaked and `disconnected` was never emitted: the UI
                # sat waiting for a signal that could no longer come.
                raise _Closed("closed") from e
            if self._wake_r in r:
                try:
                    self._wake_r.recv(4096)        # drain the nudges; the queue is
                except OSError:                    # what actually carries the data
                    pass
            if self._sock in r:
                try:
                    chunk = self._sock.recv(8192)
                except (BlockingIOError, InterruptedError):
                    continue
                except OSError as e:
                    raise _Closed(str(e)) from e
                if not chunk:
                    raise _Closed("server closed")
                buf.extend(chunk)
                self._drain_frames(buf)

    def _drain_frames(self, buf: bytearray) -> None:
        while len(buf) >= _HDR:
            ftype = buf[0]
            length = int.from_bytes(buf[1:5], "big")
            if length > MAX_RECEIVE_FRAME:
                raise _Closed("lobby frame too large")
            if len(buf) < _HDR + length:
                return
            payload = bytes(buf[_HDR:_HDR + length])
            del buf[:_HDR + length]
            if ftype == FRAME_RELAY:
                with self._rx_serial_lock:
                    self._rx_serial.extend(payload)
            elif ftype == FRAME_CONTROL:
                self._dispatch(payload)

    def _dispatch(self, payload: bytes) -> None:
        try:
            obj = json.loads(payload.decode("utf-8"))
        except Exception:
            return
        if not isinstance(obj, dict):
            return
        op = obj.get("op")
        if op in ("joined", "peer_left"):
            with self._rx_serial_lock:
                self._rx_serial.clear()
        if op == "list":
            self.game_list.emit(obj.get("games", []))
        elif op == "created":
            self.created.emit(str(obj.get("room", "")))
        elif op == "joined":
            self.joined.emit(obj)
        elif op == "peer_left":
            self.peer_left.emit()
        elif op == "error":
            self.error.emit(str(obj.get("msg", "error")))


class LobbyPipe:
    """The lobby relay as a core.netplay Pipe -- so MIRROR play can use it too.

    The relay was built for the cable mode and carries the console's serial bytes; it
    does not care what the bytes are. Mirror play needs exactly the same thing (an
    ordered byte pipe to the other PC) for its cartridge trade and its session records,
    so it gets the room, the NAT traversal and the pairing for free instead of asking
    the players for an IP address.

    `lost` is the one thing core.netplay asks for beyond send/recv, and it is why this
    is not just a pair of lambdas: the lobby loses a peer through Qt signals, not
    through a socket error, and a mirror session that is never told sits at "waiting
    for the other player" for ever.
    """

    def __init__(self, client: LobbyClient) -> None:
        self.client = client
        self.lost: str | None = None
        client.peer_left.connect(lambda: self._lose("peer left the room"))
        client.disconnected.connect(lambda why: self._lose(str(why)))

    def _lose(self, why: str) -> None:
        if self.lost is None:
            self.lost = why or "disconnected"

    @property
    def pending(self) -> int:
        """How far behind the relay is, in bytes still owed to the peer.

        ⚡ WHY A LOBBY PIPE NEEDS THIS AT ALL. The cable mode puts a byte or two a
        frame through here and can never get ahead of the wire. The cartridge trade
        offers megabytes a second, and without a way to ask "how far behind are you?"
        it just queues them all: the client's queue grows to hold a whole compressed
        cartridge on top of the two copies the trade already has, and the progress on
        screen counts bytes that have not left the PC. `core.netplay` reads this and
        stops cutting chunks until the relay catches up.
        """
        return self.client.owed

    def send(self, data: bytes) -> None:
        if data and self.lost is None:
            self.client.send_serial(data)

    def recv(self) -> bytes:
        return b"" if self.lost is not None else self.client.read_serial()

    def close(self) -> None:
        try:
            self.client.leave()
            self.client.close()
        except Exception:  # noqa: BLE001 -- tearing down; nothing left to salvage
            pass


class LobbyLink:
    """Serial transport over the lobby relay. Same interface as core.link.TcpLink
    (pump / disconnect / bytes_out / bytes_in) so PlayPage.attach_net_link reuses
    the same code path."""

    def __init__(self, machine, client: LobbyClient, *, monitor=None):
        self.machine = machine
        self.client = client
        # Optional core.link_debug.LinkMonitor -- the same tap the local and LAN
        # links carry, so the debugger's Link tab reads a lobby game too.
        self.monitor = monitor
        self.bytes_out = 0
        self.bytes_in = 0
        self.machine.serial_set_enabled(True)
        # A CONSOLE is at the other end, and the game has to be able to SEE it: 0xB1
        # bit2 only clears once something has spoken for the peer. The lobby is the
        # other online transport and had the same hole as TcpLink -- see
        # core.link.declare_peer for the measurement and for why asserting it here is
        # honest. Losing the peer goes through `disconnect()`, which disables the
        # channel outright -- the detect line reads "no cable" from that alone.
        link.declare_peer(self.machine, True)

    def pump(self) -> None:
        tx = self.machine.serial_read_tx(256)
        # Call the monitor even on an empty drain: it may be holding bytes back
        # for a simulated latency, and this is where they are released.
        out = self.monitor.on_tx(tx) if self.monitor is not None else tx
        if out:
            self.client.send_serial(out)
            self.bytes_out += len(out)
        rx = self.client.read_serial()
        if rx:
            self.machine.serial_write_rx(rx)
            self.bytes_in += len(rx)
            if self.monitor is not None:
                self.monitor.on_rx(rx)
        deliver_injected(self.machine, self.monitor)

    def disconnect(self) -> None:
        try:
            self.machine.serial_set_enabled(False)
        except Exception:
            pass
        self.client.leave()
        self.client.close()
