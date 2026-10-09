"""Mirror netplay -- exchange the BUTTONS, not the cable (core/netplay.py).

Two layers:

* the session logic against a pipe made of two lists (always runs, no ROM needed);
* the whole thing for real: FOUR consoles, two mirror sessions wired to each other
  exactly as two PCs would be, running the link probe ROM. That one needs the retail
  BIOS and the probe ROM, so it skips when either is absent.

The property the real test pins is the one the mode exists for: **the game's speed
does not depend on the network delay**, and the two PCs stay bit-identical.
"""

from __future__ import annotations

import os
import random
import socket
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from core import lobby  # noqa: E402
from core import netplay  # noqa: E402
from core.netplay import Handshake, ListPipe, MirrorSession, SocketPipe  # noqa: E402

# ⚡ IMPORTED AT MODULE LEVEL ON PURPOSE, not inside the test. The root conftest
# sandboxes the coin cell by patching `ngpc_shell._SYSTEM_RAM`, and it can only do
# that for a module already in sys.modules when the fixture runs -- importing the
# shell inside the test body means `stop()` writes the PLAYER'S system.ram instead.
shell = pytest.importorskip("ngpc_shell")
from PyQt6.QtWidgets import QApplication  # noqa: E402


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])

REPO = Path(__file__).resolve().parent.parent
BIOS = REPO / "bios.bin"
ROM = REPO / "tests" / "roms" / "link_probe.ngc"


@pytest.fixture(scope="module")
def sh(app):
    """ONE Shell, reused by every test here.

    ⚠️ NOT tidiness: a Shell that is never disposed of stays subscribed to the settings
    object, and the UI suite deletes settings keys between its own tests -- so every
    leaked Shell re-applies its settings on every one of those. Eight of them turned a
    100-second suite into a SEVEN-MINUTE one, with the cost landing on somebody else's
    test (`test_both_windows_can_be_made_small`, 283 s), which is the worst place for
    it to show up. One shell, started and stopped per test.
    """
    w = shell.Shell()
    w._settings.setValue("paths/bios", str(BIOS))
    w.play._frames_due = lambda: 1
    yield w
    _wipe(w)


def _wipe(w) -> None:
    """Put the shared shell back the way the next test expects to find it."""
    if w._net_status is not None:
        w._net_status.stop(); w._net_status = None
    w._end_mirror_bringup()
    w._mirror_pending = None
    w.play.detach_mirror()
    w.play.stop()


@pytest.fixture(autouse=True)
def _between_tests(request):
    """One Shell for the module, but no state carried from one test to the next."""
    yield
    w = request.node.funcargs.get("sh")
    if w is not None:
        _wipe(w)



def _bring_up(sh, far_sock, far_image: bytes, cap: int = 4000, announce=None,
              console=None):
    """Complete the cartridge trade so the shell ends up with a live session.

    `Shell._begin_mirror` no longer hands one over on the spot: the two PCs trade
    cartridge images first (that is what lets them be DIFFERENT, and different is what
    two players' saves always are). The far end here is a plain CartExchange built from
    the shell's own fingerprint -- same PC, same settings, so it matches by
    construction rather than by a copy of the values that could drift.

    ⚠️ `far_image` is the LOADED cartridge image (`session._rom`), not the file on disk:
    a session pads the image up to the flash chip's capacity and applies the save, and
    the hello announces a hash of THAT. Sending the raw file is refused as a corrupt
    transfer -- the guard doing its job, and it cost one debugging round to see it.
    """
    from core.netplay import CartExchange, SocketPipe, _image_hash

    far_hs = sh._mirror_handshake(host=False)
    # A real far end announces ITS OWN cartridge -- that is what makes two different
    # ones possible. `announce` overrides it to fake a transfer that arrives different
    # from what was promised.
    far_hs.rom_hash = announce or _image_hash(far_image)
    if console is not None:
        # The far player's own console settings, which THIS side must build its mirror
        # of them from -- they are announced, not agreed on.
        far_hs.console = dict(console)
    far = CartExchange(SocketPipe(far_sock), far_image, far_hs)
    for _ in range(cap):
        if sh.play._mirror is not None or sh._mirror_boot is None:
            break
        sh._pump_mirror_bringup()
        far.pump()
    return far



class _Trickle:
    """A socket that only ever accepts `cap` bytes per send.

    Not a caricature: that is exactly what a real non-blocking socket does once its
    send buffer is full, and `SocketPipe` is built around it -- it keeps the remainder
    and promises to try again. The bug this doubles for is that nobody ever asked it
    to try again. A socketpair on loopback hides the whole thing, because the reader
    drains it as fast as the writer fills it and the buffer is never full.
    """

    def __init__(self, cap: int = 4096) -> None:
        self.inbox = bytearray()
        self.peer: "_Trickle | None" = None
        self.cap = cap

    @staticmethod
    def pair(cap: int = 4096) -> tuple["_Trickle", "_Trickle"]:
        a, b = _Trickle(cap), _Trickle(cap)
        a.peer, b.peer = b, a
        return a, b

    def setblocking(self, flag) -> None:
        pass

    def send(self, data) -> int:
        n = min(self.cap, len(data))
        if n:
            self.peer.inbox += bytes(data[:n])
        return n

    def recv(self, n: int) -> bytes:
        if not self.inbox:
            raise BlockingIOError
        out = bytes(self.inbox[:n])
        del self.inbox[:n]
        return out

    def close(self) -> None:
        pass


def test_the_cartridge_trade_finishes_over_a_wire_that_takes_its_time():
    """⛔ THE HANG THE PLAYER HIT ON A LAN: "trading cartridge" that never ends.

    The trade cut its image into chunks and handed each one to the pipe, then stopped
    talking to the pipe altogether once the last chunk was queued. But a socket does
    not take a 32 KiB chunk whole when its buffer is full -- it takes what fits and
    KEEPS the rest until it is called again, and it was never called again. Measured
    with 2 MiB images on a real socketpair with a slow reader: one side reported
    `done`, progress 100 %, with **1 934 258 bytes still sitting in its own buffer**,
    while the other stayed frozen at 7.8 % for ever. And it could not recover later:
    the session that follows only writes when it schedules a new input, and a session
    waiting for the peer schedules none.

    Both directions must complete on a wire that accepts a few kilobytes at a time.
    """
    a_sock, b_sock = _Trickle.pair(cap=4096)
    # ⚠️ Incompressible on purpose: the trade sends the image through zlib, and a
    # cartridge-shaped repeating pattern collapses to a few hundred bytes that fit in
    # any buffer -- a test that proves nothing at all.
    image_a = random.Random(1).randbytes(96 * 1024)
    image_b = random.Random(2).randbytes(96 * 1024)
    kw = dict(bios_hash="B", core_version="C", delay=3)
    a = netplay.CartExchange(SocketPipe(a_sock), image_a,
                             Handshake(rom_hash=netplay._image_hash(image_a),
                                       host=True, **kw))
    b = netplay.CartExchange(SocketPipe(b_sock), image_b,
                             Handshake(rom_hash=netplay._image_hash(image_b),
                                       host=False, **kw))
    for _ in range(4000):
        for x in (a, b):
            if not (x.done or x.failed):
                x.pump()
        if a.done and b.done:
            break
    assert (a.failed, b.failed) == (None, None)
    assert a.done and b.done, (
        f"the trade never finished: {a.progress} / {b.progress}, "
        f"stranded {a.pipe.pending} / {b.pipe.pending} bytes")
    assert a.peer_image == image_b and b.peer_image == image_a
    # And "sent" on screen meant sent: nothing was left holding.
    assert a.pipe.pending == 0 and b.pipe.pending == 0


def test_a_side_is_not_finished_while_it_still_owes_the_peer_bytes():
    """⛔ WHAT "DONE" MUST MEAN ON AN ASYMMETRIC LINK -- an upload slower than the
    download, which is most home connections.

    This side can have the peer's whole cartridge in hand long before its own has
    left. Finishing on "I queued everything" makes the shell stop pumping the trade
    and hand the transport to the session -- and the peer, still missing the tail,
    sits at a percentage that never moves. The trade is only over when both
    directions are.
    """
    a_sock, b_sock = _Trickle.pair()
    a_sock.cap = 64                # our uplink: a trickle
    b_sock.cap = 1 << 20           # their uplink: everything at once
    image_a = random.Random(4).randbytes(48 * 1024)
    image_b = random.Random(5).randbytes(48 * 1024)
    kw = dict(bios_hash="B", core_version="C", delay=3)
    a = netplay.CartExchange(SocketPipe(a_sock), image_a,
                             Handshake(rom_hash=netplay._image_hash(image_a),
                                       host=True, **kw))
    b = netplay.CartExchange(SocketPipe(b_sock), image_b,
                             Handshake(rom_hash=netplay._image_hash(image_b),
                                       host=False, **kw))
    for _ in range(30):
        a.pump(); b.pump()
    assert a.peer_image is not None, "the fast direction did not even arrive"
    assert a.pipe.pending > 0, "the slow direction is not behind: nothing is pinned"
    assert not a.done, "it called the trade over with the peer's tail still here"

    # ...and, pumped like the shell pumps it (a side that says done is let go of), it
    # still completes in both directions.
    for _ in range(20000):
        for x in (a, b):
            if not (x.done or x.failed):
                x.pump()
        if a.done and b.done:
            break
    assert a.done and b.done and (a.failed, b.failed) == (None, None)
    assert b.peer_image == image_a


def _trade(delay_a: int, delay_b: int, *, announce_a=None):
    """Two cartridge trades, cross-wired, run to a standstill. Returns both."""
    pa, pb = ListPipe.pair()
    image_a, image_b = b"CART-A" * 500, b"CART-B" * 500
    kw = dict(bios_hash="B", core_version="C")
    a = netplay.CartExchange(pa, image_a, Handshake(
        rom_hash=announce_a or netplay._image_hash(image_a), host=True,
        delay=delay_a, **kw))
    b = netplay.CartExchange(pb, image_b, Handshake(
        rom_hash=netplay._image_hash(image_b), host=False, delay=delay_b, **kw))
    for _ in range(200):
        for x in (a, b):
            if not (x.done or x.failed):
                x.pump()
    return a, b


def test_the_joiner_adopts_the_host_input_delay_instead_of_being_refused():
    """⛔ THE REFUSAL THAT LOOKED LIKE A NETWORK FAULT.

    Direct-IP hosting asked BOTH players to type an input delay, and the two numbers
    had to match exactly or the session was refused -- two people typing the same
    number is not something to build a mode on. Worse, a refusal is a hang-up, and a
    TCP socket hung up with unread data sends a RESET, so the player whose settings
    were fine saw `[WinError 10054]` and nothing else. The lobby already had the
    joiner follow the host; the trade is where BOTH transports can.

    Nothing has been simulated yet at this point, so there is nothing to be wrong
    about -- see the session-level test below for where it stops being reconcilable.
    """
    a, b = _trade(delay_a=7, delay_b=2)
    assert (a.failed, b.failed) == (None, None), "a mismatch was still refused"
    assert a.done and b.done
    assert b.hs.delay == 7, "the joiner did not take the host's delay"
    assert a.hs.delay == 7, "the host moved off its own number"


def test_a_running_session_refuses_a_delay_it_cannot_adopt_any_more():
    """The other half of the rule. Once a session exists the delay is baked into
    frames already played: adopting one would silently make the two PCs play two
    different matches, which is the exact failure this mode is built to avoid. So the
    trade reconciles and the session refuses -- and a joiner that somehow failed to
    adopt is caught here, loudly, by the session's own hello."""
    a, b = _trade(delay_a=7, delay_b=2)
    assert b.hs.delay == 7
    b.hs.delay = 2                          # a joiner that did not really adopt
    sa = MirrorSession(FakeMachine(), FakeMachine(), FakeLink(), a.pipe, a.hs)
    sb = MirrorSession(FakeMachine(), FakeMachine(), FakeLink(), b.pipe, b.hs)
    sb.step(0)
    assert sa.step(0) == "rejected"
    assert sa.rejected == "input_delay"


def test_a_refusal_reaches_the_other_player_instead_of_a_bare_reset():
    """⛔ WHERE THE REASON LIVES AND WHERE THE PLAYER IS LOOKING FOR IT.

    Only ONE side can notice some refusals. Here A announces a hash its own cartridge
    does not have, so it is B -- the RECEIVER -- that catches it, and it catches it at
    the END of the transfer, by which time A has everything it needs and has already
    moved on to its session. Without a word from B, A waits for a player who has
    gone, and then reports B's hang-up as `[WinError 10054]`: a true statement about a
    socket that explains nothing.

    So the goodbye has to survive the handover: it lands in what the trade read past
    its own end (`leftover`), which is exactly what is handed to the session as
    `prime` -- the same path the opening inputs of a match take.
    """
    a, b = _trade(delay_a=3, delay_b=3, announce_a="0123456789abcdef")
    assert b.failed == "cartridge_transfer_mismatch", "the guard did not fire"
    assert a.done and a.failed is None, "A had no way to know yet -- that is the point"

    session = MirrorSession(FakeMachine(), FakeMachine(), FakeLink(), a.pipe, a.hs,
                            a.leftover)
    assert session.step(0) == "rejected"
    assert session.rejected == "peer_refused: cartridge_transfer_mismatch", (
        f"the other player was left with {session.rejected!r}")


def test_a_socket_error_number_is_translated_before_a_player_sees_it():
    """10054 is true and useless. Keep it -- it is what a search engine takes -- but
    lead with what happened."""
    assert netplay.plain_network_error(
        "[WinError 10054] Une connexion existante a du etre fermee"
    ).startswith("peer_closed")
    assert "10054" in netplay.plain_network_error("[WinError 10054] whatever")
    assert netplay.plain_network_error("something else") == "something else"


def test_a_progress_bar_does_not_reach_100_percent_before_the_bytes_leave():
    """The bar used to count bytes handed to the socket, not bytes the socket took, so
    the side that hung showed a confident 100 % -- the reading that sent the player
    looking at their network instead of at this."""
    sock, peer = _Trickle.pair(cap=1024)
    image = random.Random(3).randbytes(64 * 1024)      # incompressible: see above
    x = netplay.CartExchange(
        SocketPipe(sock), image,
        Handshake(rom_hash=netplay._image_hash(image), bios_hash="B",
                  core_version="C", delay=3, host=True))
    for _ in range(20):
        x.pump()
    assert x.pipe.pending > 0, "the double is not holding anything back"
    assert x.progress[0] < 1.0


class FakeMachine:
    """Records what it was told to do; enough for the session's own logic."""

    def __init__(self, tag: int = 0) -> None:
        self.pads: list[int] = []
        self.frames = 0
        self.tag = tag

    def write(self, addr: int, data: bytes) -> None:
        if addr == 0x00B0:
            self.pads.append(data[0])

    def run_frames(self, n: int = 1):
        self.frames += n

    def read(self, addr: int, n: int) -> bytes:
        return bytes([self.tag]) * n


class FakeLink:
    def __init__(self) -> None:
        self.pumps = 0

    def pump(self) -> None:
        self.pumps += 1


def _pair(delay: int = 3, delay_pumps: int = 0, *, tags=(1, 1), hs_kw=None):
    pa, pb = ListPipe.pair(delay_pumps)
    kw = dict(rom_hash="R", bios_hash="B", core_version="C", delay=delay)
    kw.update(hs_kw or {})
    a = MirrorSession(FakeMachine(tags[0]), FakeMachine(tags[0]), FakeLink(), pa,
                      Handshake(host=True, **kw))
    b = MirrorSession(FakeMachine(tags[1]), FakeMachine(tags[1]), FakeLink(), pb,
                      Handshake(host=False, **kw))
    return a, b


def test_both_sides_play_the_same_two_input_streams():
    """The whole model rests on this: each PC simulates BOTH consoles, so both PCs
    must feed them identical buttons or the two copies drift apart."""
    a, b = _pair(delay=2)
    for i in range(40):
        assert a.step(i) == "ran"
        assert b.step(0x80 | i) == "ran"
    # a's local console got a's buttons; a's mirror got b's -- and vice versa, with the
    # same delay on both, so the two PCs played the same pair of streams.
    assert a.local.pads == b.peer.pads
    assert a.peer.pads == b.local.pads
    # ...and the first `delay` frames are the agreed neutral padding, not player input.
    assert a.local.pads[:2] == [0, 0]
    assert a.local.pads[2:5] == [0, 1, 2]


def test_a_frame_whose_input_has_not_arrived_stalls_instead_of_guessing():
    """Delay-based netplay waits; it never invents a button. A guess would desync the
    two PCs silently, which is the one failure this whole design is built to avoid."""
    delay = 1
    a, b = _pair(delay=delay)
    for _ in range(3):
        a.step(0x10); b.step(0x20)
    b.pipe.peer = None                      # b's side of the wire goes quiet
    for _ in range(delay):                  # the inputs already in flight still play
        a.step(0x10)
    frames = a.local.frames
    outcomes = {a.step(0x10) for _ in range(5)}
    assert outcomes == {"waiting"}, "it made a button up rather than wait"
    assert a.local.frames == frames, "it ran a frame on an input it did not have"
    assert a.stalls >= 5                     # the ping, visible as a number


def test_a_waiting_session_keeps_pushing_the_wire():
    """⛔ THE DEADLOCK BETWEEN TWO POLITE SIDES.

    A session writes to the wire only when it schedules a NEW input, and a session
    that is waiting for the peer schedules none -- its frame number is not moving. So
    a byte the socket had refused stayed here for exactly as long as the peer was
    waiting for it, and if both sides were waiting, neither ever spoke again. It is
    the same fault as the cartridge trade's, one layer up, and it is why that trade
    could not fix itself by starting the session anyway.

    Here the wire refuses everything, the session stalls, and then the wire opens: the
    bytes must leave WITHOUT a new input being scheduled to carry them.
    """
    sock, far = _Trickle.pair(cap=0)            # a wire that takes nothing at all
    pipe = SocketPipe(sock)
    s = MirrorSession(FakeMachine(), FakeMachine(), FakeLink(), pipe,
                      Handshake(rom_hash="R", bios_hash="B", core_version="C",
                                delay=0, host=True))
    assert s.step(0x11) == "waiting"            # delay 0: frame 0 needs the peer's input
    assert pipe.pending > 0 and far.inbox == bytearray()
    sock.cap = 4096                             # the buffer drains, the wire is open
    for _ in range(5):
        assert s.step(0x11) == "waiting"        # still nothing new to schedule
    assert far.inbox, "the session sat on its bytes for as long as it was waiting"
    assert pipe.pending == 0


def test_a_slow_wire_costs_frames_of_delay_but_not_speed():
    """Hold every packet back for several pumps: the session still runs a frame per
    step once the pipeline is primed. That is the point of the mode -- latency is
    spent on input delay, not on the game's speed."""
    a, b = _pair(delay=6, delay_pumps=4)
    ran = 0
    for i in range(60):
        ra, rb = a.step(i & 0x3F), b.step(i & 0x3F)
        ran += (ra == "ran") + (rb == "ran")
    assert ran >= 100, f"a delayed wire cost speed: {ran}/120 frames ran"


def _play(a, b, ticks):
    ran = []
    for i in range(ticks):
        ran.append((a.step(i & 0x3F) == "ran") + (b.step((i * 7) & 0x3F) == "ran"))
    return ran


def _same_streams(a, b) -> bool:
    n1 = min(len(a.local.pads), len(b.peer.pads))
    n2 = min(len(a.peer.pads), len(b.local.pads))
    return (a.local.pads[:n1] == b.peer.pads[:n1]
            and a.peer.pads[:n2] == b.local.pads[:n2])


class _Clock:
    """Simulated milliseconds, so a 'line' can be 80 ms long without the test taking it."""

    def __init__(self) -> None:
        self.ms = 0.0

    def now(self) -> int:
        return int(self.ms) & 0xFFFFFFFF


class _TimedPipe:
    """A TCP-like wire on the simulated clock: in order, `lat` +- `jit` ms one way."""

    def __init__(self, clock: _Clock, lat: float, jit: float, rng) -> None:
        self.clock, self.lat, self.jit, self.rng = clock, lat, jit, rng
        self.peer: "_TimedPipe | None" = None
        self.q: list[tuple[float, bytes]] = []
        self.last = 0.0

    @staticmethod
    def pair(clock, lat, jit, seed=1):
        rng = random.Random(seed)
        a, b = _TimedPipe(clock, lat, jit, rng), _TimedPipe(clock, lat, jit, rng)
        a.peer, b.peer = b, a
        return a, b

    def send(self, data: bytes) -> None:
        if data and self.peer is not None:
            t = max(self.peer.last,
                    self.clock.ms + self.lat + self.rng.uniform(-self.jit, self.jit))
            self.peer.last = t
            self.peer.q.append((t, bytes(data)))

    def recv(self) -> bytes:
        out = bytearray()
        while self.q and self.q[0][0] <= self.clock.ms:
            out += self.q.pop(0)[1]
        return bytes(out)


def _two_pcs(monkeypatch, *, lat, jit, tick_a=1000 / 60, tick_b=1000 / 60, secs=60,
             delay=3, seed=1, sessions=None):
    """Two sessions on their OWN tick rates (no two PCs tick at exactly 60 Hz).

    Pass `sessions` (from an earlier call) to keep playing them on a new line."""
    import heapq
    if sessions is None:
        clock = _Clock()
        monkeypatch.setattr(MirrorSession, "_now_ms", staticmethod(clock.now))
        pa, pb = _TimedPipe.pair(clock, lat, jit, seed)
        kw = dict(rom_hash="R", bios_hash="B", core_version="C", delay=delay)
        a = MirrorSession(FakeMachine(), FakeMachine(), FakeLink(), pa,
                          Handshake(host=True, **kw))
        b = MirrorSession(FakeMachine(), FakeMachine(), FakeLink(), pb,
                          Handshake(host=False, **kw))
    else:
        a, b = sessions
        clock = a.pipe.clock
        for pipe in (a.pipe, b.pipe):
            pipe.lat, pipe.jit = lat, jit
    start = clock.ms
    events, i = [(start, 0), (start + 3.0, 1)], 0
    rng = random.Random(seed + 1)
    while events:
        t, who = heapq.heappop(events)
        if t > start + secs * 1000:
            break
        clock.ms = t
        i += 1
        (a, b)[who].step((i * (3 + who)) & 0x3F)
        heapq.heappush(events, (t + (tick_a, tick_b)[who] + rng.uniform(-2, 2), who))
    return a, b, secs * 1000 / max(tick_a, tick_b)


def test_the_round_trip_on_screen_is_the_line_not_the_checksum_period(monkeypatch):
    """⛔ IT READ UP TO A SECOND TOO HIGH. The peer sent our timestamp back with its
    NEXT checksum, a second later, and that wait was counted as network: 1030 ms on
    an 80 ms line. The echo now comes back advanced by the time it was held."""
    a, b, _ = _two_pcs(monkeypatch, lat=40, jit=0, secs=20)
    for s in (a, b):
        assert s.rtt_ms is not None
        assert 75 <= s.rtt_ms <= 120, f"{s.rtt_ms} ms measured on an 80 ms line"


def test_a_line_longer_than_the_delay_grows_the_delay_instead_of_crawling(monkeypatch):
    """⛔ "WORKS ON THE LAN, DEAD ONLINE". The delay was fixed at the handshake (3
    frames, ~50 ms), so a longer line made EVERY frame wait -- the cable mode's own
    failure, in the mode built to end it. It now follows the measured round trip."""
    a, b, most = _two_pcs(monkeypatch, lat=60, jit=20, secs=60)
    assert min(a.frames_run, b.frames_run) >= 0.95 * most, (
        f"still crawling: {a.frames_run}/{b.frames_run} of {most:.0f}")
    assert a.delay > 3 and b.delay > 3, "the delay never followed the line"
    assert _same_streams(a, b), "a changed delay made the two PCs play different buttons"


def test_the_faster_pc_does_not_lag_itself_for_the_clock_drift(monkeypatch):
    """⛔ THE FIRST VERSION RAISED ON STALLS, and two PCs never tick at the same rate:
    the faster one stalls on a beat whatever its delay, so it climbed to 15-18 frames
    of lag on its own buttons for nothing. Both must end up near what the line needs."""
    a, b, most = _two_pcs(monkeypatch, lat=40, jit=15, tick_a=16.60, tick_b=16.70, secs=90)
    assert abs(a.delay - b.delay) <= 1, f"delays {a.delay}/{b.delay}: one PC lags itself"
    assert max(a.delay, b.delay) <= 7
    assert min(a.frames_run, b.frames_run) >= 0.98 * most
    assert _same_streams(a, b)


def test_a_raised_delay_leaves_no_frame_without_an_input():
    """Raising the delay jumps the scheduling point forward. The frames in between must
    get an input AND be sent, or the peer waits for them for ever."""
    a, b = _pair(delay=3)
    _play(a, b, 100)
    a.delay += 4                                 # what the adaptation does, in one go
    ran = _play(a, b, 100)
    assert sum(ran[-50:]) == 100, "a gap in the stream froze the session"
    assert _same_streams(a, b)


def test_the_delay_comes_back_down_but_never_below_the_agreed_one(monkeypatch):
    """A bad patch raises it; once the line is short again it steps back down, one
    frame per calm period -- and never under what the two players agreed."""
    a, b, _ = _two_pcs(monkeypatch, lat=150, jit=0, secs=20)    # a bad patch...
    high = min(a.delay, b.delay)
    assert high >= 8, f"a 300 ms round trip only raised the delay to {high}"
    a, b, _ = _two_pcs(monkeypatch, lat=5, jit=0, secs=150, sessions=(a, b))  # ...then calm
    assert a.delay < high and b.delay < high, "a quiet line kept the extra lag"
    assert a.delay >= 3 and b.delay >= 3, "it went under what both players agreed"
    assert _same_streams(a, b), "lowering the delay changed the buttons played"


def test_a_good_line_keeps_the_agreed_delay(monkeypatch):
    """A LAN must not pay a single extra frame for this."""
    a, b, _ = _two_pcs(monkeypatch, lat=3, jit=1, secs=30)
    assert a.delay == 3 and b.delay == 3


def test_two_different_cartridges_are_allowed():
    """⚡ THE POINT OF TRADING THE IMAGES. Requiring the same cartridge meant requiring
    the same SAVE -- a save lives inside the image -- which two players almost never
    have. It also barred SNK-versus-Capcom in Card Fighters' Clash, which is the reason
    that game has a link at all."""
    pa, pb = ListPipe.pair()
    common = dict(bios_hash="B", core_version="C", delay=2)
    a = MirrorSession(FakeMachine(), FakeMachine(), FakeLink(), pa,
                      Handshake(rom_hash="ROM-A", host=True, **common))
    b = MirrorSession(FakeMachine(), FakeMachine(), FakeLink(), pb,
                      Handshake(rom_hash="ROM-B", **common))
    b.step(0)
    assert a.step(0) == "ran"
    assert a.rejected is None
    assert a.hs.peer_rom_hash == "ROM-B", "the peer's cartridge must still be announced"


@pytest.mark.parametrize("field,expected", [
    ("bios_hash", "bios"), ("core_version", "core_version")])
def test_a_different_bios_or_core_is_refused_too(field, expected):
    pa, pb = ListPipe.pair()
    common = dict(rom_hash="R", bios_hash="B", core_version="C", delay=2)
    other = dict(common); other[field] = "OTHER"
    a = MirrorSession(FakeMachine(), FakeMachine(), FakeLink(), pa,
                      Handshake(host=True, **common))
    b = MirrorSession(FakeMachine(), FakeMachine(), FakeLink(), pb, Handshake(**other))
    b.step(0)
    assert a.step(0) == "rejected"
    assert a.rejected == expected


def test_a_desync_is_noticed_and_named():
    """Detected, not repaired -- but never silent. Two sides whose consoles hold
    different bytes must not keep playing as if they agreed."""
    a, b = _pair(delay=1, tags=(1, 2))       # their consoles' memory differs on purpose
    for _ in range(netplay.CHECK_EVERY + 4):
        a.step(0); b.step(0)
    assert a.desync_at is not None
    assert b.desync_at is not None


def test_matching_consoles_never_report_a_desync():
    """The control group. Without it, a checksum that always mismatched would look
    exactly like a working detector."""
    a, b = _pair(delay=1, tags=(7, 7))
    for _ in range(netplay.CHECK_EVERY * 3):
        a.step(0); b.step(0)
    assert a.desync_at is None and b.desync_at is None


@pytest.mark.skipif(not (BIOS.exists() and ROM.exists()),
                    reason="needs the retail bios.bin (gitignored) and the probe ROM")
def test_the_mirrors_own_cable_is_stepped_like_a_cable():
    """⚡ The mirror owns BOTH consoles, so it owns the same trap the local 2P cable had.

    It used to run `first.run_frames(1); pump; second.run_frames(1); pump` -- one
    console frozen for the whole of the other's frame, which is a frame of latency on
    every answer, in one direction. That is what mirror play exists to avoid: its cable
    is local and should carry NO latency at all. Card Fighters' Clash is the measure --
    through a mirror of its VS match, the whole-frame schedule dies at 118/102 bytes
    (its "LINK ERROR"), the interleaved one reaches 610/617 and plays.

    So: a frame of a real mirror must pump the cable many times, not twice.
    """
    from core import native
    from core.link import InProcessLink

    rom, bios = ROM.read_bytes(), BIOS.read_bytes()

    class Counting(InProcessLink):
        pumps = 0

        def pump(self):
            Counting.pumps += 1
            super().pump()

    local = native.NativeMachine(rom, bios=bios)
    peer = native.NativeMachine(rom, bios=bios)
    try:
        local.reset(bios_handoff=True)
        peer.reset(bios_handoff=True)
        Counting.pumps = 0
        pa, _pb = ListPipe.pair()
        FRAMES = 10
        # The input delay pre-fills that many frames of both streams, so this session
        # can run on its own without a peer answering.
        sess = MirrorSession(local, peer, Counting(local, peer), pa,
                             Handshake(rom_hash="r", bios_hash="b", core_version="c",
                                       delay=FRAMES + 2, host=True))
        relays_before = local.link_relay_count()
        for _ in range(FRAMES):
            assert sess.step(0) == "ran"
        # ⚡ COUNTED WHERE THE RELAYING NOW HAPPENS. This used to count InProcessLink.pump
        # calls, which was right while the host owned the relay. The core owns it now
        # (ngpc_run_linked), so that counter reads ZERO for a real pair -- an instrument
        # that can no longer fire, and it would have read as "the cable is never stepped"
        # rather than as "you are counting the wrong thing". The requirement is unchanged.
        real = Counting.pumps + (local.link_relay_count() - relays_before)

        # CONTROL: the same session over machines with no sliced interface takes the
        # whole-frame path -- two pumps a frame. Without it, "many pumps" could just
        # mean "many frames".
        Counting.pumps = 0
        fake = MirrorSession(FakeMachine(), FakeMachine(), Counting(local, peer),
                             ListPipe.pair()[0],
                             Handshake(rom_hash="r", bios_hash="b", core_version="c",
                                       delay=FRAMES + 2, host=True))
        for _ in range(FRAMES):
            assert fake.step(0) == "ran"
        assert Counting.pumps == FRAMES * 2, "the fallback should pump twice a frame"
        assert real > FRAMES * 5, (
            f"a mirrored frame pumped the cable {real / FRAMES:.1f} times on average; "
            "a whole frame per console is the latency CFC's VS handshake cannot survive")
    finally:
        local.close(); peer.close()


# --------------------------------------------------------------------------- #
# The real thing: four consoles, two PCs' worth of session, one probe ROM.
# --------------------------------------------------------------------------- #
@pytest.mark.skipif(not (BIOS.exists() and ROM.exists()),
                    reason="needs the retail bios.bin (gitignored) and the probe ROM")
@pytest.mark.parametrize("wire_delay", [0, 6])
def test_two_mirrored_pcs_stay_identical_and_keep_full_speed(wire_delay):
    """Two PCs, each running BOTH consoles, exchanging only controller bytes.

    The probe ROM sends its own pad and records the other's, so "the cable works" is
    readable straight out of memory (0x400A = last byte received). What this pins:
      * every frame ran -- a six-pump wire delay costs input lag, not speed;
      * the two PCs' consoles are byte-identical, which is what makes the mirror sound.
    """
    from core import native
    from core.link import InProcessLink

    rom, bios = ROM.read_bytes(), BIOS.read_bytes()

    def one_pc(pipe, host):
        local = native.NativeMachine(rom, bios=bios)
        peer = native.NativeMachine(rom, bios=bios)
        local.reset(bios_handoff=True)
        peer.reset(bios_handoff=True)
        link = InProcessLink(local, peer)
        hs = Handshake(rom_hash="r", bios_hash="b", core_version="c",
                       delay=4, host=host)
        return MirrorSession(local, peer, link, pipe, hs)

    pa, pb = ListPipe.pair(wire_delay)
    pc1, pc2 = one_pc(pa, True), one_pc(pb, False)
    try:
        ran = 0
        for i in range(240):
            # player 1 holds RIGHT, player 2 holds UP -- distinct bits, so a console
            # that received the wrong stream is visible rather than plausible.
            ran += (pc1.step(0x08) == "ran") + (pc2.step(0x01) == "ran")
        assert ran == 480, f"the wire cost frames: {ran}/480 ran"

        # PC1's own console and PC2's mirror of it are the same console, twice.
        assert pc1.local.read(0x4000, 0x2C00) == pc2.peer.read(0x4000, 0x2C00)
        assert pc1.peer.read(0x4000, 0x2C00) == pc2.local.read(0x4000, 0x2C00)
        assert pc1.checksum() == pc2.checksum()
        assert pc1.desync_at is None and pc2.desync_at is None

        # and the emulated cable really carried the other player's buttons
        assert pc1.local.read(0x400A, 1)[0] == 0x01     # player 1 heard player 2's UP
        assert pc1.peer.read(0x400A, 1)[0] == 0x08      # player 2 heard player 1's RIGHT
    finally:
        for s in (pc1, pc2):
            s.local.close(); s.peer.close()


@pytest.mark.skipif(not (BIOS.exists() and ROM.exists()),
                    reason="needs the retail bios.bin (gitignored) and the probe ROM")
def test_the_shell_plays_a_mirror_match_over_a_real_socket(sh, app):
    """The product path: a real Shell in mirror mode, over a real socket.

    ⚡ ONLY ONE Shell here, with the other PC standing in as a plain MirrorSession.
    Two Shells in one process reproduced the match fine on their own but took the
    whole suite down with qFatal (0xC0000409, no traceback) when they ran after
    everything else -- and a test that destabilises the suite is worse than the
    coverage it buys. One shell still exercises every line this mode added to the
    product: _begin_mirror, attach_mirror, _step_mirror and the frame loop.
    """
    import ngpc_settings as cfg
    from core import native
    from core.link import InProcessLink
    from core.native_session import NativeSession
    from core.netplay import Handshake, MirrorSession, SocketPipe

    s1, s2 = socket.socketpair()
    far_a = far_b = None
    try:
        sh.play.start(ROM)
        sh.play.held = 0x08                      # this player holds RIGHT
        sh._begin_mirror(SocketPipe(s1), host=True)
        _bring_up(sh, s2, sh.play.session._rom)
        assert sh.play.link_mode() == "mirror"

        # The other PC, built the way _begin_mirror builds its pair: same cartridge
        # image, same clock, cable in before either boots, player 1's console first.
        near = sh.play._mirror
        # ⚡ Built through NativeSession with the SAME arguments _begin_mirror uses,
        # and given the same wait-state tuning. Hand-rolled NativeMachines desynced
        # inside two frames -- which is the design's own warning, working.
        def far_console():
            return NativeSession(ROM, rom_bytes=sh.play.session._rom, autosave=False,
                                 bios_path=BIOS, save_to_rom=False, sidecar=False,
                                 flash_size=sh.play.session.machine.flash_capacity())
        far_a, far_b = far_console(), far_console()
        far_local, far_peer = far_a.machine, far_b.machine
        clock = native.RtcState(1, *shell.Shell.MIRROR_CLOCK)
        for m in (far_local, far_peer):
            if cfg.cart_wait_states(sh._settings):
                # ⚠️ THIS PAIR IS A REPLICA OF `Shell._begin_mirror`, so a knob added
                # there and not here quietly stops it being one.
                #
                # ✅ AND THAT WARNING PAID OFF, 2026-08-21. This block used to spell out
                # four setters by hand; the shell moved to `set_timing_silicon`, which
                # arms eight things at once, and the two PCs ended up running DIFFERENT
                # machines. They desynced inside two frames and the test said so --
                # loudly, not silently, which is better than the comment feared.
                #
                # ⇒ Call the same one thing the shell calls. Never re-spell it.
                m.set_timing_silicon(cfg.CART_FETCH_WAIT, cfg.CART_BIOS_WAIT)
            m.set_rtc(clock)
            m.serial_set_enabled(True)
            m.reset(bios_handoff=True)
        far = MirrorSession(far_local, far_peer, InProcessLink(far_peer, far_local),
                            SocketPipe(s2),
                            Handshake(rom_hash=near.hs.rom_hash,
                                      bios_hash=near.hs.bios_hash,
                                      core_version=near.hs.core_version,
                                      delay=near.delay, host=False))

        for _ in range(400):
            sh.play._tick()
            far.step(0x01)                       # the other player holds UP

        assert near.rejected is None and far.rejected is None
        assert near.frames_run > 300, f"the shell barely ran: {near.frames_run}"
        assert near.desync_at is None and far.desync_at is None
        # each console heard the OTHER player's controller byte, on both PCs
        assert sh.play.machine.read(0x400A, 1)[0] == 0x01
        assert near.peer.read(0x400A, 1)[0] == 0x08
        assert far.local.read(0x400A, 1)[0] == 0x08
        # ...and the two PCs hold the same pair of consoles
        assert near.checksum() == far.checksum()
    finally:
        # ⚡ Timers before objects: a Shell left holding a running QTimer that fires
        # into a half-collected page takes the process with it (qFatal, no traceback).
        # Sockets first: on Windows a socketpair is two loopback TCP sockets, and one
        # left open is a ResourceWarning that outlives the test.
        for s in (s1, s2):
            try: s.close()
            except OSError: pass
        for sess in (far_a, far_b):
            if sess is not None:
                sess.close()


def test_two_different_input_delays_are_refused():
    """The delay decides WHICH FRAME an input is played on, so two different numbers
    are two different matches. Refused up front rather than discovered as drift."""
    pa, pb = ListPipe.pair()
    common = dict(rom_hash="R", bios_hash="B", core_version="C")
    a = MirrorSession(FakeMachine(), FakeMachine(), FakeLink(), pa,
                      Handshake(host=True, delay=3, **common))
    b = MirrorSession(FakeMachine(), FakeMachine(), FakeLink(), pb,
                      Handshake(delay=6, **common))
    b.step(0)
    assert a.step(0) == "rejected"
    assert a.rejected == "input_delay"


@pytest.mark.skipif(not (BIOS.exists() and ROM.exists()),
                    reason="needs the retail bios.bin (gitignored) and the probe ROM")
def test_the_shell_ends_the_session_when_the_peer_goes(sh, app):
    """The shell must READ that flag, not just have it available: a lost peer ends the
    session instead of waiting for an input that can never arrive."""
    s1, s2 = socket.socketpair()
    ended = []
    try:
        sh.play.start(ROM)
        sh._begin_mirror(SocketPipe(s1), host=True)
        _bring_up(sh, s2, sh.play.session._rom)
        sh.play.mirror_ended.connect(ended.append)
        sh.play._mirror.pipe.lost = "peer closed the connection"
        sh.play._tick()
        assert ended, "the shell kept waiting for a console that had gone"
        assert sh.play._mirror is None, "the dead session was not torn down"
    finally:
        if sh._net_status is not None:
            sh._net_status.stop(); sh._net_status = None
        for s in (s1, s2):
            try: s.close()
            except OSError: pass
        sh.play.stop()


def test_the_session_notices_the_peer_going_away():
    """A peer that vanishes must not look like a slow one. Without this the session
    waits for an input that will never arrive, for ever, saying "waiting for the other
    player" -- the same unreadable failure the cable mode had to fix."""
    class DeadPipe:
        lost = "peer closed the connection"
        def send(self, data): pass
        def recv(self): return b""

    assert getattr(DeadPipe(), "lost", None), "the shell reads `lost` off the pipe"
    # and the real pipe raises that flag rather than an exception (see SocketPipe):
    s1, s2 = socket.socketpair()
    pipe = netplay.SocketPipe(s1)
    try:
        s2.close()
        for _ in range(20):                  # a few frames' worth of pumping
            pipe.send(b"x")
            pipe.recv()
            if pipe.lost:
                break
        assert pipe.lost, "a departed peer was never noticed"
    finally:
        for s in (s1, s2):
            try: s.close()
            except OSError: pass


@pytest.mark.skipif(not (BIOS.exists() and ROM.exists()),
                    reason="needs the retail bios.bin (gitignored) and the probe ROM")
def test_stopping_the_game_lets_go_of_the_mirror(sh, app):
    """The mirror owns a SECOND console and a socket. Left attached they outlive the
    game that opened them, and the next `start()` would step a session whose local
    machine has been closed -- so `stop()` must let go of all of it."""
    s1, _s2 = socket.socketpair()
    try:
        sh.play.start(ROM)
        sh._begin_mirror(SocketPipe(s1), host=True)
        _bring_up(sh, _s2, sh.play.session._rom)
        peer = sh.play._mirror_peer_session
        assert peer is not None and sh.play.link_mode() == "mirror"

        sh.play.stop()
        assert sh.play._mirror is None, "the session outlived the game"
        assert sh.play._mirror_peer_session is None, "the second console was left open"

        # ...and a NEW session is not born condemned by the previous one's verdict: a
        # stale `_mirror_desynced` would end it on its first frame, for somebody else's
        # desync.
        sh.play._mirror_desynced = True
        sh.play._mirror_waits = 99
        sh.play.start(ROM)
        s3, s4 = socket.socketpair()
        try:
            sh._begin_mirror(SocketPipe(s3), host=True)
            _bring_up(sh, s4, sh.play.session._rom)
            assert sh.play._mirror_desynced is False, "it inherited the last verdict"
            assert sh.play._mirror_waits == 0
        finally:
            for s in (s3, s4):
                try: s.close()
                except OSError: pass
    finally:
        if sh._net_status is not None:
            sh._net_status.stop(); sh._net_status = None
        for s in (s1, _s2):
            try: s.close()
            except OSError: pass
        sh.play.stop()


@pytest.mark.skipif(not (BIOS.exists() and ROM.exists()),
                    reason="needs the retail bios.bin (gitignored) and the probe ROM")
def test_mirror_play_refuses_what_would_rewind_this_console_only(sh, app):
    """A savestate, a rewind or a reset applied HERE and nowhere else puts the two PCs
    in different states. The checksum would notice a second later and end the match --
    a rotten way to answer a keypress. So the keypress is refused."""
    s1, s2 = socket.socketpair()
    try:
        sh.play.start(ROM)
        for _ in range(20):
            sh.play._tick()
        sh._begin_mirror(SocketPipe(s1), host=True)
        _bring_up(sh, s2, sh.play.session._rom)
        before = sh.play.machine.read(0x4000, 0x1000)

        sh.play.load_state(0)                    # would restore only this console
        sh.play.start_rewind()                   # ...and so would this
        sh.play._do_reset()                      # ...and this reboots only this one
        assert sh.play._rewinding is False, "rewind started inside a mirror match"
        assert sh.play.machine.read(0x4000, 0x1000) == before, "the console was rewound"
        assert sh.play._mirror is not None, "the session was torn down instead of the key"

        # save_state is refused too: a file the player could load back mid-match is the
        # same trap one step later.
        path = sh.play._state_path(0)
        stamp = path.stat().st_mtime_ns if path is not None and path.is_file() else None
        sh.play.save_state(0)
        if path is not None and path.is_file():
            assert path.stat().st_mtime_ns == stamp, "a state was written mid-match"
    finally:
        if sh._net_status is not None:
            sh._net_status.stop(); sh._net_status = None
        for s in (s1, s2):
            try: s.close()
            except OSError: pass
        sh.play.stop()


@pytest.mark.skipif(not (BIOS.exists() and ROM.exists()),
                    reason="needs the retail bios.bin (gitignored) and the probe ROM")
def test_a_lobby_room_starts_the_link_it_advertised(sh, app):
    """⚡ MIRROR PLAY THROUGH THE LOBBY -- the mode the players can actually reach.

    Mirror play used to be direct-address only (an IP, a port, and whatever it takes
    to be reachable), while the lobby -- rooms, pairing, NAT traversal -- only ever
    opened the cable. That is backwards for Card Fighters' Clash, which is the game
    that NEEDS the mirror: its VS handshake gives up on latency the cable cannot avoid.

    The relay carries opaque bytes, so the same room works for either; what the joiner
    must not do is guess. So a room that says "mirror" starts the cartridge trade, and
    one that says nothing is a cable room -- which is what every room made by an older
    client says.
    """
    from core.lobby import LobbyPipe

    def client():
        # Never started: no thread, no socket. What is exercised here is the routing
        # and the pipe, and both only need the object's signals and queues.
        return lobby.LobbyClient("127.0.0.1", 1, "tester")

    sh.play.start(ROM)
    c = client()
    try:
        sh._on_lobby_linked(c, {"mode": "mirror", "role": "host", "delay": 5})
        assert sh._mirror_boot is not None, "a mirror room did not start the trade"
        assert sh.play._net_link is None, "it opened the cable as well"
        assert sh._mirror_boot.hs.delay == 5, "the room's input delay was ignored"
        assert sh._mirror_boot.hs.host is True
        sh._end_mirror_bringup()
    finally:
        c.close()

    # CONTROL: a room with no mode is the cable, and takes the other path entirely.
    c2 = client()
    try:
        sh._on_lobby_linked(c2, {"role": "guest"})
        assert sh._mirror_boot is None, "a cable room started a mirror"
        assert sh.play._net_link is not None, "a cable room did not open the cable"
        assert sh.play.link_mode() == "lobby"
    finally:
        sh._end_net_link()
        c2.close()

    # ...and the pipe reports a peer that leaves. The lobby loses one through a Qt
    # signal, not a socket error, so a session never told sits at "waiting" for ever.
    c3 = client()
    try:
        pipe = LobbyPipe(c3)
        assert pipe.lost is None
        c3.peer_left.emit()
        assert pipe.lost, "a peer leaving the room went unnoticed"
        assert pipe.recv() == b"", "a dead pipe still handed bytes over"
    finally:
        c3.close()


@pytest.mark.skipif(not (BIOS.exists() and ROM.exists()),
                    reason="needs the retail bios.bin (gitignored) and the probe ROM")
def test_the_two_online_modes_are_exclusive(sh, app):
    """They drive the same console two different ways -- the cable relays its serial
    FIFO, the mirror owns the pad byte and runs a second console here. Both at once is
    two relays over one FIFO, so the second one asked for is refused."""
    s1, s2 = socket.socketpair()
    try:
        sh.play.start(ROM)
        sh._begin_mirror(SocketPipe(s1), host=True)
        _bring_up(sh, s2, sh.play.session._rom)

        assert sh._one_link_at_a_time(False) is True, "a cable was allowed over a mirror"
        assert sh._one_link_at_a_time(True) is False, "a mirror blocked itself"

        sh.play.detach_mirror()
        sh.play._link_peer = sh.play            # pretend a local cable is attached
        assert sh._one_link_at_a_time(True) is True, "a mirror was allowed over a cable"
        sh.play._link_peer = None
    finally:
        if sh._net_status is not None:
            sh._net_status.stop(); sh._net_status = None
        for s in (s1, s2):
            try: s.close()
            except OSError: pass
        sh.play.stop()


def test_a_failed_mirror_attempt_does_not_hijack_the_next_cable_link(sh, app):
    """⛔ `_mirror_pending` left set by a mirror attempt that timed out made the NEXT
    ordinary Host/Join quietly start a mirror session instead of a cable."""
    try:
        sh._mirror_pending = "host"
        sh._on_net_failed("timed out")
        assert sh._mirror_pending is None
    finally:
        sh.play.stop()


def test_a_long_session_does_not_hoard_state():
    """Unbounded growth is a bug that only shows up in the one session nobody tests:
    a long one. Inputs are dropped once played; a checksum whose partner never arrives
    is forgotten after a few rounds rather than kept for the whole match."""
    a, b = _pair(delay=2)
    for _ in range(netplay.CHECK_EVERY * 12):        # ~12 checksum rounds
        a.step(0x10); b.step(0x20)
    assert len(a.local_inputs) <= 4, f"played inputs were kept: {len(a.local_inputs)}"
    assert len(a.peer_inputs) <= 4
    assert len(a._mine) <= 8 and len(a._checks) <= 8

    # ...and a checksum whose partner NEVER arrives (a peer that stopped sending them)
    # must be forgotten rather than kept for the length of the match.
    # ⚠️ Not simulated by cutting the wire: with no peer input the session answers
    # "waiting" and returns BEFORE the checksum code, so that version of this test
    # exercised nothing -- it passed with the trim removed. Stuff the book instead.
    a._mine.update({100000 + i: i for i in range(50)})
    a.step(0x10)
    assert len(a._mine) <= 8, f"unanswered checksums piled up: {len(a._mine)}"


@pytest.mark.skipif(not (BIOS.exists() and ROM.exists()),
                    reason="needs the retail bios.bin (gitignored) and the probe ROM")
def test_the_debugger_tap_reaches_the_mirror_cable(sh, app):
    """In mirror play the cable is the in-process one between the two local consoles,
    pumped by the session rather than by _pump_link. A Link tab reading zero bytes on a
    busy cable is worse than no tab at all."""
    from core.link_debug import LinkMonitor, TX

    s1, s2 = socket.socketpair()
    try:
        sh.play.start(ROM)
        sh._begin_mirror(SocketPipe(s1), host=True)
        _bring_up(sh, s2, sh.play.session._rom)
        mon = LinkMonitor()
        sh.play.set_link_monitor(mon)
        assert sh.play._mirror.link.monitor_a is mon, "the tap never reached the cable"

        far = sh.play._mirror
        for f in range(200):                     # feed it the peer's inputs by hand
            far.peer_inputs[f] = 0x01
            sh.play._tick()
        assert mon.bytes_tx > 0, "the tap saw nothing on a cable that carried bytes"
        assert mon.raw(TX), "no transmit log"
    finally:
        if sh._net_status is not None:
            sh._net_status.stop(); sh._net_status = None
        for s in (s1, s2):
            try: s.close()
            except OSError: pass
        sh.play.stop()


@pytest.mark.skipif(not (BIOS.exists() and ROM.exists()),
                    reason="needs the retail bios.bin (gitignored) and the probe ROM")
def test_the_two_players_may_hold_DIFFERENT_cartridges(sh, app):
    """⚡ THE REASON THE IMAGES ARE TRADED AT ALL.

    Building the mirror from the LOCAL cartridge forced both players to hold the same
    image -- and a save lives inside the image, so it forced the same SAVE, which two
    players essentially never have. It also barred SNK-versus-Capcom in Card Fighters'
    Clash, and that pairing is the reason the game has a link.

    So the peer's console must be built from the bytes the PEER sent, not from ours.
    The far cartridge here differs in a byte the probe ROM never reads, which is enough
    to tell the two images apart while leaving the link behaviour intact.
    """
    s1, s2 = socket.socketpair()
    try:
        sh.play.start(ROM)

        mine = sh.play.session._rom
        theirs = bytearray(mine)
        theirs[0x1F0] ^= 0xFF               # a byte in the header padding, never read
        theirs = bytes(theirs)
        assert theirs != mine

        sh._begin_mirror(SocketPipe(s1), host=True)
        _bring_up(sh, s2, theirs)
        assert sh.play._mirror is not None, "a different cartridge was refused"

        # the mirror console really holds THEIR cartridge, not a copy of ours
        peer_cart = sh.play._mirror.peer.read(0x200000, len(theirs))
        assert peer_cart[0x1F0] == theirs[0x1F0]
        assert peer_cart[0x1F0] != mine[0x1F0], "the peer console got OUR cartridge"
        assert sh.play.machine.read(0x200000, 0x200)[0x1F0] == mine[0x1F0]

        for f in range(120):                 # and the match still runs
            sh.play._mirror.peer_inputs[f] = 0x01
            sh.play._tick()
        assert sh.play._mirror.frames_run > 100
        assert sh.play._mirror.desync_at is None
    finally:
        if sh._net_status is not None:
            sh._net_status.stop(); sh._net_status = None
        for s in (s1, s2):
            try: s.close()
            except OSError: pass
        sh.play.detach_mirror()
        sh.play.stop()


@pytest.mark.skipif(not (BIOS.exists() and ROM.exists()),
                    reason="needs the retail bios.bin (gitignored) and the probe ROM")
def test_the_shell_finishes_the_trade_on_a_wire_that_takes_a_little_at_a_time(sh, app):
    """The player's report, at the level they hit it: "trading cartridge" on a LAN,
    stuck, never finishing. Everything below this is unit-tested; this is the same
    wire under the real `Shell._pump_mirror_bringup`, which is what actually has to
    come out the other side with a live session.
    """
    near, far = _Trickle.pair(cap=256)          # a wire with a very small mouth
    try:
        sh.play.start(ROM)
        sh._begin_mirror(SocketPipe(near), host=True)
        _bring_up(sh, far, sh.play.session._rom, cap=40000)
        assert sh.play._mirror is not None, "the trade never finished"
        assert sh.play._mirror.pipe.pending == 0
    finally:
        if sh._net_status is not None:
            sh._net_status.stop(); sh._net_status = None
        sh.play.detach_mirror()
        sh.play.stop()


@pytest.mark.skipif(not (BIOS.exists() and ROM.exists()),
                    reason="needs the retail bios.bin (gitignored) and the probe ROM")
def test_the_peer_console_is_built_from_the_peer_settings(sh, app, monkeypatch):
    """⛔ THE SILENT DESYNC BEHIND "it connects, both consoles reboot, and it drops".

    Same cartridge, same BIOS, same build -- and still two different machines, because
    the mirror of the OTHER player's console was built from OUR settings. The cartridge
    language byte (0x6F87) is per player and a bilingual cart branches on it; the flash
    capacity went through this PC's setting even though the image already arrives padded
    to the sender's. Nothing in the fingerprint could see either, so nothing was
    refused: the two copies drifted, and the checksum ended the match about a second in
    with the single word "desync".
    """
    import ngpc_settings as cfg

    built = {}
    real_session = shell.NativeSession

    def spy(*a, **kw):
        built.update(kw)
        return real_session(*a, **kw)

    s1, s2 = socket.socketpair()
    try:
        sh._settings.setValue("general/cart_language", cfg.CART_LANG_EN)
        # Explicit, and bigger than what the peer will send -- so re-applying it to the
        # peer's image (the old behaviour) visibly resizes their console.
        sh._settings.setValue("general/flash_size", cfg.FLASH_16M)
        sh.play.start(ROM)
        theirs = sh.play.session._rom[:0x100000]      # their cart is an 8 Mbit one
        monkeypatch.setattr(shell, "NativeSession", spy)
        sh._begin_mirror(SocketPipe(s1), host=True)
        assert sh.play._mono_console() is False, "this test needs a COLOUR local console"
        _bring_up(sh, s2, theirs,
                  console={"lang": cfg.CART_LANG_JA, "mono": True})
        assert sh.play._mirror is not None, "the trade did not finish"
        assert built.get("language") == cfg.CART_LANG_JA, (
            "the peer's console was built with OUR cartridge language")
        assert built.get("flash_size") == len(theirs), (
            "the peer's console was resized by OUR flash setting")
        # Two BIOS-less consoles both fingerprint as "hle", so nothing refuses an NGP
        # playing an NGPC -- the mirror has to be told which one the peer is.
        assert built.get("k1ge_console") is True, (
            "the peer's console was built as OUR machine type")
    finally:
        sh._settings.remove("general/flash_size")
        if sh._net_status is not None:
            sh._net_status.stop(); sh._net_status = None
        for s in (s1, s2):
            try: s.close()
            except OSError: pass
        sh.play.detach_mirror()
        sh.play.stop()


@pytest.mark.skipif(not (BIOS.exists() and ROM.exists()),
                    reason="needs the retail bios.bin (gitignored) and the probe ROM")
def test_a_newer_message_is_not_wiped_by_an_older_flash(sh, app):
    """⛔ WHY THE PLAYER COULD NOT READ THE ERROR.

    Every `_flash` used to build ITS OWN timer, parented to the page and left running.
    A later message therefore inherited an earlier message's countdown: `attach_mirror`
    flashes "mirror ready" for 2.5 s, so a session that died inside those 2.5 s had its
    reason wiped about a second later. The one thing the player needed in order to say
    what had happened.
    """
    import time

    sh.play.start(ROM)
    try:
        sh.play._flash("first", 60)              # a short one, still counting
        sh.play._flash("second", 30_000)         # ...and the real message after it
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            app.processEvents()
            time.sleep(0.01)
        assert sh.play.overlay.text() == "second", (
            "an older flash's timer erased a newer message")
    finally:
        sh.play.stop()


@pytest.mark.skipif(not (BIOS.exists() and ROM.exists()),
                    reason="needs the retail bios.bin (gitignored) and the probe ROM")
def test_hosting_a_mirror_game_shows_the_host_its_own_address(sh, app, monkeypatch):
    """"Listening on port 7789" is not an address, and the other player has no other
    way to reach this PC. The cable mode has shown this panel -- LAN address, detected
    public one, a line to paste -- since it existed; the mirror was never wired to it,
    so hosting one meant going and finding your own IP by hand.
    """
    import ngpc_lobby

    built = []

    class FakeHostInfo:
        """The real one is a QDialog, and the shell uses two things of it beyond
        `show()`: `finished`, to read "closing this panel means never mind" as a
        cancel, and `close()`, to take the panel away once the peer is in."""

        finished = type("_Sig", (), {"connect": staticmethod(lambda *a: None)})()

        def __init__(self, game, port, lang, parent=None):
            built.append((game, port))

        def show(self):
            pass

        def close(self):
            pass

    monkeypatch.setattr(ngpc_lobby, "HostInfoDialog", FakeHostInfo)
    monkeypatch.setattr(shell.QInputDialog, "getInt",
                        staticmethod(lambda *a, **k: (7999, True)))
    monkeypatch.setattr(shell.Shell, "_ask_mirror_delay", lambda self: True)
    # ⚠️ No real listener: a QThread left blocked in accept() and torn down with the
    # test is a Qt-level abort, not a test failure -- and it lands on whoever runs
    # next. What this test is about is one wire: hosting must build the panel.
    # ⚠️ AND IT MUST RETURN True, LIKE THE REAL ONE. `_start_net` now reports whether
    # the attempt actually started, and its callers stop when it did not -- that is
    # what stops `_mirror_pending` being armed over an earlier attempt still in
    # flight. A fake returning None reads as "refused" and skips the very panel this
    # test is about.
    monkeypatch.setattr(shell.Shell, "_start_net", lambda *a, **k: True)
    try:
        sh.play.start(ROM)
        sh._host_mirror()
        assert built == [(ROM.stem, 7999)], (
            "hosting a mirror game never told the host its address")
    finally:
        sh._mirror_pending = None
        sh.play.stop()


@pytest.mark.skipif(not (BIOS.exists() and ROM.exists()),
                    reason="needs the retail bios.bin (gitignored) and the probe ROM")
def test_a_cartridge_that_arrives_different_from_its_announcement_is_refused(sh, app):
    """A truncated or corrupted transfer would otherwise show up as a desync twenty
    seconds into the match, which is the hardest possible way to be told about it."""
    s1, s2 = socket.socketpair()
    try:
        sh.play.start(ROM)
        sh._begin_mirror(SocketPipe(s1), host=True)
        # The far end promises one cartridge and sends another.
        _bring_up(sh, s2, sh.play.session._rom, announce="0123456789abcdef")
        assert sh.play._mirror is None, "a cartridge that did not match was accepted"
    finally:
        if sh._net_status is not None:
            sh._net_status.stop(); sh._net_status = None
        for s in (s1, s2):
            try: s.close()
            except OSError: pass
        sh.play.stop()



# ⛔ DEUX JOUEURS SUR LE MEME BUILD PEUVENT SIMULER DEUX MACHINES DIFFERENTES.
#
# `core_version` hache la DLL, mais le commutateur `--timing` est en PYTHON : depuis le
# 23/08/2026 le modele silicium est le defaut et `--timing legacy` reste disponible pour
# attribuer une regression. Deux PC avec la meme cartouche, le meme BIOS et le meme build
# s'annonçaient donc exactement la meme chose en tournant a des vitesses differentes -- le
# desync invisible au branchement, qui tue le match en derive. Meme forme que la faute deja
# payee sur `lang` et `mono`, a une difference pres : ce n'est pas un reglage par joueur
# qu'on peut refleter, il faut REFUSER.
def _timed_handshake(timing):
    return Handshake(rom_hash="r", bios_hash="b", core_version="c", timing=timing)


def test_the_same_timing_model_is_accepted():
    a, b = _timed_handshake("silicon"), _timed_handshake("silicon")
    assert a.check(b.payload()) is None


def test_a_different_timing_model_is_refused():
    a, b = _timed_handshake("silicon"), _timed_handshake("legacy")
    assert a.check(b.payload()) == "timing_model"
    assert b.check(a.payload()) == "timing_model"


def test_a_peer_from_before_the_switch_is_refused():
    """Un hello sans le champ vient d'un build dont le defaut ETAIT l'ancien modele : le
    lire comme `legacy` dit la verite sur ce qu'il simule, et aucune version de protocole
    n'a besoin de bouger."""
    import json
    a = _timed_handshake("silicon")
    import json
    vieux = json.loads(a.payload().decode())
    vieux.pop("timing")
    assert a.check(json.dumps(vieux).encode()) == "timing_model"
    assert _timed_handshake("legacy").check(json.dumps(vieux).encode()) is None


def test_the_handshake_follows_the_environment_when_not_told(monkeypatch):
    """⚠️ Le DEFAUT a change deux fois le 23/08/2026 (silicium, puis retour a l'ancien).
    Ce test ne pique donc pas une valeur : il verifie que la poignee de main dit la MEME
    chose que la machine, quel que soit le defaut du jour."""
    from core.native import active_timing_model

    def annonce():
        return Handshake(rom_hash="r", bios_hash="b", core_version="c").timing

    for choix in ("legacy", "silicon"):
        monkeypatch.setenv("NGPCRAFT_TIMING", choix)
        assert active_timing_model() == choix
        assert annonce() == choix
    monkeypatch.delenv("NGPCRAFT_TIMING")
    assert annonce() == active_timing_model()


# ⚡ L'ALLER-RETOUR EST MESURE, ET IL NE L'ETAIT NULLE PART.
#
# Avant ceci le delai se TAPAIT A LA MAIN par les deux joueurs (« ping en ms / 17, plus
# un ») et `DEFAULT_DELAY = 3` s'appliquait quelle que soit la ligne. Sans mesure on ne
# peut pas distinguer « le link est instable » de « la ligne de ce joueur est mauvaise »,
# donc aucun rapport de bug n'est interpretable -- LINK_NETPLAY_STUDY §2.2, prerequis de
# tout diagnostic.
#
# Le porteur est le message d'empreinte, qui partait deja : + horodatage de l'emetteur et
# ECHO du dernier recu. Quand l'echo revient, la difference est l'aller ET le retour --
# aucune horloge commune a supposer entre les deux PC, et 8 octets par empreinte.
def _mirror_pair():
    a, b = _trade(delay_a=3, delay_b=3)
    return (MirrorSession(FakeMachine(), FakeMachine(), FakeLink(), a.pipe, a.hs),
            MirrorSession(FakeMachine(), FakeMachine(), FakeLink(), b.pipe, b.hs))


def test_the_round_trip_is_measured_once_the_echo_comes_back():
    sa, sb = _mirror_pair()
    # il faut assez de trames pour qu'une empreinte parte, revienne, et que l'echo
    # nous revienne a son tour : trois passages de CHECK_EVERY suffisent largement.
    for _ in range(netplay.CHECK_EVERY * 3 + 4):
        sa.step(0)
        sb.step(0)
    assert sa.rtt_ms is not None, "aucun aller-retour mesure apres trois empreintes"
    assert sa.rtt_ms < 60000
    assert sa.rtt_max_ms >= sa.rtt_ms or sa.rtt_max_ms > 0


def test_a_peer_from_before_the_measurement_is_refused_not_misread():
    """⚠️ La taille du message d'empreinte a change. Un pair d'ancienne version le lirait
    a la mauvaise longueur et desynchroniserait sa PROPRE lecture du flux -- bien pire
    qu'un refus. La version du protocole doit donc l'arreter avant."""
    a = Handshake(rom_hash="r", bios_hash="b", core_version="c")
    import json
    vieux = json.loads(a.payload().decode())
    vieux["v"] = netplay.PROTOCOL_VERSION - 1
    assert a.check(json.dumps(vieux).encode()) == "protocol_version"
