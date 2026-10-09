"""Mirror netplay: send the BUTTONS across the network, not the cable.

⛔ THE CEILING THIS EXISTS TO LIFT. The other online mode relays the link cable's
own bytes. A link game writes a byte and BLOCKS on the peer's reply, so it advances
one logic frame per round trip: every millisecond of network latency is taken out of
the GAME's speed and nothing else. MEASURED through the shell (Fatal Fury link match,
the game's own logic counter 0x4B3C, per emulated frame):

    network round trip   0 ms   33 ms   67 ms   134 ms
    cable relayed        1.00    0.80    0.57     0.36

That is the "the fight runs in slow motion but the audio is perfect" report -- audio
comes from the APU, which waits for nobody.

🪞 THE WAY OUT. The NGPC link is two INDEPENDENT consoles that each run their own copy
of the game and share only serial bytes. So run BOTH consoles on BOTH machines: the
cable becomes an in-process byte pipe with no latency at all, and the only thing that
has to cross the network is each player's controller byte. Network latency is then
spent on INPUT DELAY instead of on speed. Same bench, same match, the peer's buttons
delayed instead of the cable:

    input delay      0     2     4     8     16 frames  (~267 ms of network)
    game speed      0.97  0.97  0.97  0.97  0.97

The speed stops depending on the ping. This is how every other emulator does netplay.

⚡ IT ONLY WORKS BECAUSE THE CORE IS DETERMINISTIC, and that was measured before any
of this was written, not assumed: a full link match (menus plus 300 frames of fighting
with varied input) replayed with the same inputs comes out byte-identical in both
consoles' work RAM and in the framebuffer -- including when the second run starts 2.5 s
later on the wall clock, and with the RTC set to a different hour. Capture/restore of
(CPU + AuxState + memory) round-trips exactly too, which is what a later rollback layer
would stand on.

⚠️ WHAT IT COSTS, and none of it is hidden from the player:
  * both sides run two consoles (measured 3.3 ms per frame for the pair, of 16.7);
  * both sides need the SAME cartridge, the same BIOS and the same core -- the
    handshake refuses the session otherwise, because a mismatch does not fail loudly,
    it drifts;
  * the session starts from power-on: nobody can join a game already in progress;
  * an input that has not arrived stalls BOTH sides for that frame. That is the honest
    behaviour of delay-based netplay, and it is what a rollback layer would remove.
  * a desync is detected, not repaired: the two sides compare a checksum of both
    consoles every second and say so.

This module is transport-agnostic on purpose: it talks to anything with `send(bytes)`
and `recv() -> bytes`, so the direct socket and the lobby relay both drive it, and the
tests drive it with a pair of lists.
"""

from __future__ import annotations

import json
import struct
import time
import zlib
from collections import deque
from typing import Protocol

from core.link import run_two_consoles_interleaved

# 2 : le message d'empreinte porte desormais l'horodatage et l'echo (mesure de
# l'aller-retour). Sa taille change, donc un pair d'avant ne peut pas le lire -- et la
# version le lui dit proprement au lieu de le laisser desynchroniser sa lecture.
# 3 : l'echo est avance du temps ou on l'a garde (voir `MirrorSession._echo`), et le
# retard SUIT l'aller-retour. Meme taille de message -- mais un pair d'avant renvoie
# l'echo brut, gonfle jusqu'a 1 s, et ferait grimper notre retard a son plafond. Mieux
# vaut le refuser proprement que jouer avec 300 ms de lag sans savoir pourquoi.
PROTOCOL_VERSION = 3

# Wire: [type:1][frame:4 LE][payload]. Fixed headers, so a partial read never has to
# be guessed at -- the reader keeps whatever it cannot complete yet.
_T_HELLO = 1        # payload = 2-byte length + JSON: what session this is
_T_INPUT = 2        # payload = 1 byte: the pad for that frame
_T_CHECK = 3        # payload = 12 bytes: empreinte + horodatage + echo (voir plus bas)
# ⚡ L'ALLER-RETOUR SE MESURE ICI, ET IL N'ETAIT MESURE NULLE PART.
#
# Avant ceci, `DEFAULT_DELAY = 3` s'appliquait quelle que soit la ligne et le delai se
# TAPAIT A LA MAIN par les deux joueurs (« ping en ms / 17, plus un »). Sans mesure, on ne
# peut pas distinguer « le link est instable » de « la ligne de ce joueur est mauvaise »,
# donc **aucun rapport de bug n'est interpretable** -- c'est le §2.2 de LINK_NETPLAY_STUDY,
# et c'est le prerequis de tout diagnostic.
#
# Le porteur est le message d'empreinte, qui part deja periodiquement : on lui ajoute
# l'horodatage de l'emetteur et l'ECHO du dernier horodatage recu. Quand l'echo revient,
# la difference est l'aller-retour complet. Aucun message nouveau, aucun trafic en plus
# que 8 octets par empreinte.
_CHECK_BODY = struct.Struct("<III")     # empreinte, horodatage emetteur, echo
# The cartridge trade that runs BEFORE the session (see CartExchange). Its records
# reuse this header with the LENGTH in the second field rather than a frame number.
_T_CART = 4         # payload = none; the field is the peer's compressed image length
_T_CHUNK = 5        # payload = that many bytes of it
# ⛔ WHY A SIDE SAYS GOODBYE INSTEAD OF JUST HANGING UP. Refusing used to mean closing
# the socket, and a TCP socket closed with unread data in its receive buffer sends a
# RESET -- so the player whose settings were fine got `[WinError 10054]` and no idea
# why, while the actual reason (a different core, a different BIOS) was on the OTHER
# screen. One record, sent before the hang-up, carries it across.
_T_BYE = 6          # payload = 2-byte length + reason text
_HDR = struct.Struct("<BI")

# How far ahead inputs are scheduled when nothing better is known. Three frames covers
# a ~50 ms round trip; the host puts its own choice in the handshake.
DEFAULT_DELAY = 3
CHECK_EVERY = 60            # frames between desync checksums (~1 s)

# ⚡ THE DELAY FOLLOWS THE LINE, ON EACH SIDE, WHILE THE MATCH RUNS.
#
# ⛔ WHY. The delay was a number fixed at the handshake -- 3 frames (~50 ms) unless the
# host typed another. That covers a ~33 ms round trip: a LAN, never Internet. Past it
# EVERY frame waits for the other PC, and the game crawls exactly like the cable mode
# it exists to replace -- "works on the LAN, dead online", in both modes, which is
# what the players reported. The round trip was measured (rtt_ms) and used by nothing.
#
# 🔑 WHY ONE SIDE MAY CHANGE IT ALONE. Every input travels with the frame number it is
# FOR, so the delay only decides when a PC schedules its OWN button; the other PC plays
# whatever frame-tagged stream it receives. Raising it leaves a gap of frames with no
# input -- filled with the current pad and SENT, so both PCs still play one identical
# stream. Lowering it just skips scheduling until `frame + delay` passes what is
# already scheduled. Nothing is ever guessed, so nothing can desync.
#
# ⛔ THE SIGNAL IS THE ROUND TRIP, NOT THE STALLS -- tried, measured, dropped. Two PCs
# never tick at exactly 60 Hz, so the faster one stalls on a regular beat whatever the
# delay; counting those stalls drove ITS delay to 15-18 frames on a simulated 80 ms
# line, which feeds it the peer's inputs no sooner and only lags its own buttons.
# Following the worst recent round trip gave 5/5 on the same line, at the same speed.
# Those one-tick drift stalls are the clock sync itself: in delay-based play a wait IS
# the slower PC catching up, so an explicit "give a tick back" (study step 3) measured
# to nothing and was not kept.
ADAPT_COOLDOWN = 30         # frames after a change before another one
ADAPT_CALM = 600            # frames (~10 s) between two steps DOWN: up is immediate
MAX_DELAY = 20              # ~333 ms: beyond, the line is not playable anyway
FRAME_MS = 1000 / 60


class Pipe(Protocol):
    """A reliable, ordered byte pipe -- what TCP and the lobby relay both are."""

    def send(self, data: bytes) -> None: ...
    def recv(self) -> bytes: ...


def _flush(pipe) -> None:
    """Push whatever a pipe is still holding back, without giving it new data.

    ⛔ THE DEADLOCK THIS ENDS. A pipe over a real socket cannot hand everything to the
    kernel on the spot -- when the send buffer is full it KEEPS the rest and waits to be
    called again. Both users of this module used to call `send` only when they had
    something new to say, so the moment the last record was queued the leftover stopped
    moving: the cartridge trade sat at "100 % sent" with megabytes never posted, and the
    peer waited for a tail that was two feet away on this PC. Flushing is now a thing
    they do every pump, whether or not they have anything to add.

    Optional on the transport: the lobby relay drains itself on its own thread, and the
    test pipe has no buffer at all.
    """
    f = getattr(pipe, "flush", None)
    if callable(f):
        f()


def _pending(pipe) -> int:
    """How many bytes the transport is still holding for us (0 if it never holds any)."""
    return int(getattr(pipe, "pending", 0) or 0)


# The socket errors a player can actually hit, said in words rather than in numbers.
# `[WinError 10054] Une connexion existante a dû être fermée par l'hôte distant` is a
# true statement about a socket and tells the player nothing about what to do; worse,
# it is what a refusal on the OTHER PC looks like from here, so it reads like a network
# fault when it is a settings mismatch.
_ERRNO_WORDS = {
    "10054": "peer_closed",     # WSAECONNRESET  / ECONNRESET
    "10053": "peer_closed",     # WSAECONNABORTED
    "10060": "no_answer",       # WSAETIMEDOUT
    "10061": "refused",         # WSAECONNREFUSED
    "104": "peer_closed",       # ECONNRESET, POSIX
    "110": "no_answer",         # ETIMEDOUT
    "111": "refused",           # ECONNREFUSED
}


def plain_network_error(why: str) -> str:
    """Keep the raw text, but lead with something a player can act on."""
    text = str(why or "peer closed")
    for code, word in _ERRNO_WORDS.items():
        if code in text:
            return f"{word} ({text})"
    return text


class ListPipe:
    """Two of these, cross-wired, are a network with no network in it (for tests)."""

    def __init__(self) -> None:
        self.peer: "ListPipe | None" = None
        self.delay_pumps = 0
        self._held: deque[tuple[int, bytes]] = deque()
        self._t = 0

    @staticmethod
    def pair(delay_pumps: int = 0) -> tuple["ListPipe", "ListPipe"]:
        a, b = ListPipe(), ListPipe()
        a.peer, b.peer = b, a
        a.delay_pumps = b.delay_pumps = delay_pumps
        return a, b

    def send(self, data: bytes) -> None:
        if not data or self.peer is None:
            return
        self._t += 1
        self.peer._held.append((self.peer._t + self.delay_pumps, bytes(data)))

    def recv(self) -> bytes:
        self._t += 1
        out = bytearray()
        while self._held and self._held[0][0] <= self._t:
            out += self._held.popleft()[1]
        return bytes(out)


class SocketPipe:
    """A TCP socket as a Pipe. Non-blocking, and it never loses a byte.

    ⛔ `sendall` is not usable here and the link cable learned it the hard way: on a
    non-blocking socket it raises BlockingIOError the moment the kernel buffer fills,
    WITHOUT saying how much it already handed over, so the caller cannot know what to
    resend. `send` reports what it took; the rest waits for the next call. A netplay
    stream that loses a byte does not degrade -- it desyncs.
    """

    def __init__(self, sock) -> None:
        self.sock = sock
        self.sock.setblocking(False)
        self._out = bytearray()
        self.lost: str | None = None

    @property
    def pending(self) -> int:
        """Bytes accepted from the caller that the kernel has not taken yet."""
        return len(self._out)

    def flush(self) -> None:
        """Try again with what the kernel refused last time.

        ⚡ This is the whole reason the buffer is safe to have. Without a way to retry
        that does not require new data, "the rest waits for the next call" is a promise
        nobody keeps once the caller has said everything it had to say -- see _flush().
        """
        if self.lost is not None or not self._out:
            return
        try:
            sent = self.sock.send(self._out)
            del self._out[:sent]
        except (BlockingIOError, InterruptedError):
            pass                                    # buffer full: keep it, try again
        except OSError as e:
            self._lose(str(e))

    def send(self, data: bytes) -> None:
        if self.lost is not None:
            return
        self._out += data
        self.flush()

    def recv(self) -> bytes:
        if self.lost is not None:
            return b""
        out = bytearray()
        try:
            while True:
                chunk = self.sock.recv(4096)
                if not chunk:
                    self._lose("peer closed the connection")
                    break
                out += chunk
        except (BlockingIOError, InterruptedError):
            pass
        except OSError as e:
            self._lose(str(e))
        return bytes(out)

    def _lose(self, why: str) -> None:
        if self.lost is None:
            self.lost = plain_network_error(why)
        try:
            self.sock.close()
        except OSError:
            pass


class Handshake:
    """What both sides must agree on before a single frame is simulated.

    Not politeness: the two consoles are simulated twice, once per PC, and anything
    that differs between the copies -- a different dump of the cartridge, a different
    BIOS, a core built with different timing -- makes them drift apart silently, mid
    match, with no error anywhere. Cheaper to refuse than to debug.

    ⚡ THE CARTRIDGE IS NOT ONE OF THOSE THINGS -- not any more. Requiring the same
    image meant requiring the same SAVE, since a save lives inside the cartridge
    image, and two players almost never have the same save. It also made Card
    Fighters' Clash impossible in this mode, and SNK-versus-Capcom is the whole point
    of that game. The images are TRADED instead (see :class:`CartExchange`), so each
    PC builds the other player's console from the other player's cartridge. What both
    sides still have to share is the BIOS and the build, because those decide how the
    code runs rather than what it is.
    """

    def __init__(self, *, rom_hash: str, bios_hash: str, core_version: str,
                 delay: int = DEFAULT_DELAY, host: bool = False,
                 console: dict | None = None, timing: str | None = None) -> None:
        # Our own cartridge fingerprint: announced, never compared for equality. The
        # receiving side checks the image it was SENT against it, which catches a
        # truncated or corrupted transfer -- the failure that would otherwise show up
        # as a desync twenty seconds into a match.
        self.rom_hash = rom_hash
        self.bios_hash = bios_hash
        self.core_version = core_version
        # ⛔ LE MODELE DE TEMPS, ET IL EST COMPARE -- pas seulement annonce.
        #
        # `core_version` hache la DLL, mais le commutateur `--timing` est en PYTHON :
        # deux joueurs sur le MEME build, l'un lance en `--timing legacy`, s'annoncent
        # exactement la meme chose et simulent deux machines de vitesses differentes.
        # C'est le desync qui ne se voit pas au branchement et tue le match en derive,
        # celui-la meme que `lang` et `mono` ont deja coute une fois.
        #
        # ⚡ Et contrairement a `lang`/`mono`, ce n'est PAS un reglage par joueur : on ne
        # peut pas construire la console du pair a partir de la sienne, il faut refuser.
        if timing is None:
            # Import tardif : `core.native` charge la DLL, et ce module s'importe aussi
            # la ou elle n'est pas necessaire.
            from core.native import active_timing_model
            timing = active_timing_model()
        self.timing = timing
        # ⚡ SETTINGS THAT SHAPE THE SENDER'S OWN CONSOLE -- announced, like the
        # cartridge, and for the same reason.
        #
        # ⛔ THE DESYNC THIS ENDS. The mirror of the other player's console was built
        # from OUR settings. Two PCs with the same cartridge, the same BIOS and the
        # same build could still be simulating two different machines: the cartridge
        # language byte (0x6F87) is a per-player setting, and a bilingual cartridge
        # branches on it. Nothing in the fingerprint saw that, so it did not refuse --
        # it drifted, and the checksum killed the match a second in, saying only
        # "desync". Announce it and build the peer's console from THEIRS, exactly as
        # with the traded cartridge: two players with different settings can now play.
        self.console = dict(console or {})
        self.peer_console: dict = {}
        self.delay = int(delay)
        self.host = bool(host)
        # Set when a session has been built on this handshake: from that moment the
        # delay is baked into simulated frames and can no longer be reconciled, only
        # refused. See check().
        self.locked = False

    def payload(self) -> bytes:
        return json.dumps({
            "v": PROTOCOL_VERSION, "rom": self.rom_hash, "bios": self.bios_hash,
            "core": self.core_version, "delay": self.delay, "cons": self.console,
            "timing": self.timing,
        }, sort_keys=True).encode("utf-8")

    def check(self, raw: bytes) -> str | None:
        """None when the peer's session matches ours; otherwise why it does not."""
        try:
            them = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return "unreadable_hello"
        if int(them.get("v", -1)) != PROTOCOL_VERSION:
            return "protocol_version"
        self.peer_rom_hash = them.get("rom")     # what their image must hash to
        cons = them.get("cons")
        if isinstance(cons, dict):
            self.peer_console = dict(cons)       # how to build THEIR console here
        if them.get("bios") != self.bios_hash:
            return "bios"
        if them.get("core") != self.core_version:
            return "core_version"
        # ⓘ Absent = un build d'avant le 23/08/2026, dont le defaut ETAIT l'ancien
        # modele. Le defaut de lecture dit donc la verite sur ce que ce pair simule,
        # et aucune version de protocole n'a besoin de bouger pour le refuser.
        if str(them.get("timing", "legacy")) != self.timing:
            return "timing_model"
        # Both sides schedule their own input the SAME number of frames ahead, so a
        # different delay means the two PCs play different input streams from frame
        # zero.
        #
        # ⚡ AND THE JOINER ADOPTS THE HOST'S, rather than the session being refused.
        # Direct-IP hosting asks BOTH players for a number, and two people typing the
        # same one is not something to build a mode on: any difference killed the
        # session, and -- because a refusal is a hang-up, and a hang-up with unread
        # data is a TCP reset -- the other player saw only `[WinError 10054]`. The
        # lobby already had the joiner follow the host; this puts it where BOTH
        # transports get it.
        #
        # ⛔ ONLY WHILE NOTHING HAS BEEN SIMULATED. Once a session exists the delay is
        # baked into frames already played, and the earlier reasoning holds exactly:
        # refusing is the version that cannot be subtly wrong. The session sends its
        # own hello with whatever it settled on, so a joiner that failed to adopt is
        # caught there, loudly, instead of drifting.
        peer_delay = int(them.get("delay", -1))
        if peer_delay < 0:
            return "input_delay"
        if peer_delay != self.delay:
            if self.locked:
                return "input_delay"
            if not self.host:
                self.delay = peer_delay
            # (the host keeps its own: the joiner adopts it, and the session-level
            # hello is where that gets checked rather than assumed)
        return None


class CartExchange:
    """Trade cartridge images, so each PC can build the OTHER player's console.

    ⛔ WHAT THIS UNBLOCKS. Building the mirror from the LOCAL image forced both players
    to hold the same cartridge -- and, because a save lives inside that image, the same
    SAVE. Two players almost never do. It also barred SNK-versus-Capcom in Card
    Fighters' Clash, which is the reason that game has a link at all.

    Runs BEFORE the session, on the same pipe, and both ends do the same thing, so
    there is no leader: each sends its image in chunks while reading the other's. It
    finishes when both directions are complete.

    ⚡ THE LEFTOVER MATTERS. Whoever finishes first starts sending SESSION records
    while the other is still receiving cartridge chunks, so those bytes land in this
    reader's buffer. They are kept in `leftover` and handed to the session, or the
    first inputs of the match would be eaten here.
    """

    # 32 KiB keeps a 4 MiB cartridge to ~128 sends without making one pump write
    # megabytes into a socket that has to stay responsive.
    CHUNK = 32 * 1024
    # ...and no more than this many bytes may be waiting on the transport before the
    # next chunk is cut. The shell pumps eight times per 16 ms tick, which offers 16 MB
    # a second to a network that is not going to take it: without a ceiling the surplus
    # piles up in the pipe's own buffer, the progress bar reads "sent" for bytes that
    # have not left the PC, and a 4 MiB cartridge is held in memory three times over.
    # Backpressure instead: ask the transport how far behind it is.
    IN_FLIGHT = 256 * 1024

    def __init__(self, pipe: Pipe, image: bytes, handshake: "Handshake", *,
                 chunk: int = CHUNK) -> None:
        self.pipe = pipe
        self.hs = handshake
        self.expect_hash: str | None = None  # filled from their hello, checked at the end
        self.greeted = False
        self._out = zlib.compress(bytes(image), 1)   # level 1: a cartridge is mostly
        self._sent = 0                               # already-compressed data anyway
        self._chunk = max(1024, int(chunk))
        self._in = bytearray()
        self._want: int | None = None       # their compressed length, once announced
        self._rx = bytearray()
        self.peer_image: bytes | None = None
        self.leftover = b""
        self.failed: str | None = None
        # Hello first, then our length: one record stream, so there is no ordering
        # between "who greets" and "who starts sending" to get wrong.
        js = self.hs.payload()
        self.pipe.send(_HDR.pack(_T_HELLO, 0) + struct.pack("<H", len(js)) + js)
        self.pipe.send(_HDR.pack(_T_CART, len(self._out)))

    @property
    def done(self) -> bool:
        # ⛔ "posted", not "queued". `_sent` only says which bytes were handed to the
        # transport; a socket keeps back what the kernel would not take. Finishing on
        # `_sent` alone declared the trade complete with megabytes still sitting in this
        # PC's own buffer -- and since the session that follows only writes to the wire
        # when it schedules a new input, and a session waiting for the peer never
        # schedules one, those bytes never moved again. Both players then waited for
        # each other for ever: one at "trading 100 %", the other frozen mid-percentage.
        return (self.greeted and self.peer_image is not None
                and self._sent >= len(self._out) and _pending(self.pipe) == 0)

    @property
    def progress(self) -> tuple[float, float]:
        """(sent, received) as fractions, for something honest to put on screen.

        "Sent" counts what the transport has actually posted, not what it was handed:
        a bar that sits at 100 % while the peer crawls is the symptom of the bug above,
        and it is worth not being able to draw it.
        """
        posted = max(0, self._sent - _pending(self.pipe))
        up = posted / len(self._out) if self._out else 1.0
        down = (len(self._in) / self._want) if self._want else 0.0
        return (min(1.0, up), min(1.0, down))

    def pump(self) -> None:
        if self.failed is not None:
            return
        lost = getattr(self.pipe, "lost", None)
        if lost:
            self.failed = str(lost)
            return
        # Retry whatever the transport still owes BEFORE cutting a new chunk: this is
        # the only thing that moves the tail once the last chunk has been queued.
        _flush(self.pipe)
        if self._sent < len(self._out) and _pending(self.pipe) < self.IN_FLIGHT:
            piece = self._out[self._sent:self._sent + self._chunk]
            self.pipe.send(_HDR.pack(_T_CHUNK, len(piece)) + piece)
            self._sent += len(piece)
        self._rx += self.pipe.recv()
        while self.peer_image is None and len(self._rx) >= _HDR.size:
            kind, n = _HDR.unpack_from(self._rx, 0)
            if kind == _T_HELLO:
                if len(self._rx) < _HDR.size + 2:
                    return
                (ln,) = struct.unpack_from("<H", self._rx, _HDR.size)
                if len(self._rx) < _HDR.size + 2 + ln:
                    return
                body = bytes(self._rx[_HDR.size + 2:_HDR.size + 2 + ln])
                del self._rx[:_HDR.size + 2 + ln]
                why = self.hs.check(body)
                if why:
                    self._refuse(why)
                    return
                self.expect_hash = self.hs.peer_rom_hash
                self.greeted = True
                continue
            if kind == _T_BYE:
                if len(self._rx) < _HDR.size + 2:
                    return
                (ln,) = struct.unpack_from("<H", self._rx, _HDR.size)
                if len(self._rx) < _HDR.size + 2 + ln:
                    return
                why = bytes(self._rx[_HDR.size + 2:_HDR.size + 2 + ln])
                self.failed = "peer_refused: " + why.decode("utf-8", "replace")
                return
            if kind == _T_CART:
                del self._rx[:_HDR.size]
                self._want = int(n)
                continue
            if kind != _T_CHUNK:
                self.failed = "unexpected_record"
                return
            if len(self._rx) < _HDR.size + n:
                return
            self._in += self._rx[_HDR.size:_HDR.size + n]
            del self._rx[:_HDR.size + n]
            if self._want is not None and len(self._in) >= self._want:
                self._finish()
        if self.peer_image is not None:
            self.leftover += bytes(self._rx)
            self._rx.clear()

    def _refuse(self, why: str) -> None:
        """Say no, and make sure the OTHER player is told which no it was.

        Without this the refusal is a silent hang-up: the peer's socket is reset and
        all it can report is the reset. The reason lives on this screen, and the
        player looking for it is at the other one.
        """
        self.failed = why
        try:
            msg = why.encode("utf-8")[:512]
            self.pipe.send(_HDR.pack(_T_BYE, 0) + struct.pack("<H", len(msg)) + msg)
            _flush(self.pipe)
        except Exception:  # noqa: BLE001 -- refusing already; the wire may be gone
            pass

    def _finish(self) -> None:
        try:
            image = zlib.decompress(bytes(self._in))
        except zlib.error as e:
            self._refuse(f"corrupt_transfer: {e}")
            return
        if self.expect_hash is not None and _image_hash(image) != self.expect_hash:
            # The peer told us what their cartridge hashes to in the hello. A transfer
            # that arrives different is a desync twenty seconds into the match; this
            # turns it into a refusal now.
            self._refuse("cartridge_transfer_mismatch")
            return
        self.peer_image = image


def _image_hash(image: bytes) -> str:
    import hashlib

    return hashlib.sha1(image).hexdigest()[:16]


class MirrorSession:
    """Two consoles, here, driven by two controllers -- one of them across a network.

    `local` is the console this player sees and plays; `peer` is the mirror of the
    other player's console, simulated here so the cable between them is a local byte
    pipe. Both machines are stepped in the order the shell's own relay uses (each
    console's frame is followed by a relay), because that order is what measured 1.00.
    """

    def __init__(self, local, peer, link, pipe: Pipe, handshake: Handshake,
                 prime: bytes = b"") -> None:
        self.local = local          # NativeMachine this player controls
        self.peer = peer            # NativeMachine mirroring the other player
        self.link = link            # core.link.InProcessLink between the two
        self.pipe = pipe
        self.hs = handshake
        self.delay = max(0, int(handshake.delay))
        # From here the delay is baked into frames: the trade could still reconcile two
        # different numbers, a running session cannot. Anything that disagrees now is
        # refused rather than adopted (Handshake.check).
        handshake.locked = True
        self.frame = 0
        # Inputs are scheduled `delay` frames ahead, so the opening frames have no
        # input to wait for -- pre-filled, identically on both sides.
        self.local_inputs: dict[int, int] = {f: 0 for f in range(self.delay)}
        self.peer_inputs: dict[int, int] = {f: 0 for f in range(self.delay)}
        # The last frame our own input is scheduled for. The delay may move during the
        # match (see ADAPT_*); this is what keeps the stream gap-free when it does.
        self._sched_to = self.delay - 1
        self.base_delay = self.delay            # the floor: never below what both agreed
        self.delay_max_seen = self.delay
        self._adapt_hold = 0                    # no change before this frame
        # ⚡ `prime` is what the bring-up read past the end of the cartridge trade:
        # whoever finished first was already sending session records. Dropping them
        # would eat the opening inputs of the match.
        self._rx = bytearray(prime)
        self._checks: dict[int, int] = {}       # frame -> the peer's checksum
        self._mine: dict[int, int] = {}         # frame -> ours, until the peer's lands
        # Mesure de l'aller-retour (§2.2 de LINK_NETPLAY_STUDY). `rtt_ms` est la
        # derniere valeur, `rtt_avg_ms` la moyenne de la session, `rtt_max_ms` le pire --
        # c'est le pire qui explique un a-coup, pas la moyenne.
        self.rtt_ms: int | None = None
        self.rtt_avg_ms = 0.0
        self.rtt_max_ms = 0
        self._rtt_n = 0
        self._peer_ts = 0
        self._peer_ts_at = 0                    # our clock when that timestamp arrived
        self._rtt_recent: deque[int] = deque(maxlen=8)
        self.desync_at: int | None = None
        self.rejected: str | None = None        # why the handshake failed, if it did
        self.greeted = False
        self.stalls = 0                         # frames we could not run: the ping, seen
        self.frames_run = 0
        self.bytes_out = 0
        self.bytes_in = 0
        js = self.hs.payload()
        self._send(_T_HELLO, 0, struct.pack("<H", len(js)) + js)

    # --- wire ---------------------------------------------------------------
    def _send(self, kind: int, frame: int, payload: bytes = b"") -> None:
        data = _HDR.pack(kind, frame & 0xFFFFFFFF) + payload
        self.pipe.send(data)
        self.bytes_out += len(data)

    def _reject(self, why: str) -> None:
        """Refuse the session, and tell the peer which refusal it was -- otherwise all
        it gets is the TCP reset that follows the hang-up (see CartExchange._refuse)."""
        if self.rejected is not None:
            return
        self.rejected = why
        try:
            msg = why.encode("utf-8")[:512]
            self._send(_T_BYE, 0, struct.pack("<H", len(msg)) + msg)
            _flush(self.pipe)
        except Exception:  # noqa: BLE001 -- refusing already; the wire may be gone
            pass

    def _drain(self) -> None:
        # ⚡ Push before pulling, every frame, even when we have nothing new to say.
        # A session only writes when it schedules an input, and a session that is
        # WAITING for the peer schedules none -- so a byte the kernel would not take
        # would stay here exactly when the peer is waiting for it. Two stalled sides
        # that both stop flushing never restart each other.
        _flush(self.pipe)
        chunk = self.pipe.recv()
        if chunk:
            self._rx += chunk
            self.bytes_in += len(chunk)
        while len(self._rx) >= _HDR.size:
            kind, frame = _HDR.unpack_from(self._rx, 0)
            need = {_T_INPUT: 1, _T_CHECK: _CHECK_BODY.size}.get(kind)
            if kind in (_T_HELLO, _T_BYE):
                if len(self._rx) < _HDR.size + 2:
                    return
                (n,) = struct.unpack_from("<H", self._rx, _HDR.size)
                need = 2 + n
            if need is None:
                # An unknown record type cannot be skipped safely (we do not know its
                # length), and guessing would desync the reader as well as the game.
                self.rejected = self.rejected or "unknown_record"
                self._rx.clear()
                return
            if len(self._rx) < _HDR.size + need:
                return
            body = bytes(self._rx[_HDR.size:_HDR.size + need])
            del self._rx[:_HDR.size + need]
            if kind == _T_INPUT:
                self.peer_inputs[frame] = body[0]
            elif kind == _T_CHECK:
                somme, leur_ts, echo = _CHECK_BODY.unpack(body)
                self._checks[frame] = somme
                self._peer_ts = leur_ts          # a renvoyer dans notre prochain envoi
                self._peer_ts_at = self._now_ms()
                if echo:
                    # L'echo est NOTRE horodatage revenu : la difference est l'aller
                    # ET le retour, sans horloge commune a supposer entre les deux PC.
                    aller_retour = (self._now_ms() - echo) & 0xFFFFFFFF
                    if aller_retour < 60000:      # au-dela, c'est un compteur qui a boucle
                        self.rtt_ms = aller_retour
                        n = self._rtt_n = self._rtt_n + 1
                        self.rtt_avg_ms = (self.rtt_avg_ms * (n - 1) + aller_retour) / n
                        self.rtt_max_ms = max(self.rtt_max_ms, aller_retour)
                        self._rtt_recent.append(aller_retour)
                        self._follow_rtt()
            elif kind == _T_BYE:
                # The peer refused, and said so instead of just dropping the socket.
                self.rejected = self.rejected or (
                    "peer_refused: " + body[2:].decode("utf-8", "replace"))
                return
            else:
                self.greeted = True
                why = self.hs.check(body[2:])
                if why:
                    self._reject(why)

    # --- state ---------------------------------------------------------------
    def checksum(self) -> int:
        """A cheap fingerprint of BOTH consoles, in the same order on both PCs.

        Player 1's console first, always -- otherwise the two sides would checksum
        their own console first and never agree even when perfectly in step.
        """
        p1, p2 = (self.local, self.peer) if self.hs.host else (self.peer, self.local)
        crc = zlib.crc32(p1.read(0x4000, 0x2C00))
        return zlib.crc32(p2.read(0x4000, 0x2C00), crc) & 0xFFFFFFFF

    # --- the frame ------------------------------------------------------------
    @staticmethod
    def _now_ms() -> int:
        """Une horloge MONOTONE, jamais l'heure du mur : un ajustement NTP en pleine
        partie ferait sauter l'aller-retour mesure, ou le rendrait negatif."""
        return int(time.monotonic() * 1000) & 0xFFFFFFFF

    def step(self, pad: int) -> str:
        """Advance one frame if the peer's input for it has arrived.

        Returns "ran", "waiting" (the peer's input is not here yet -- the caller must
        NOT advance anything else either), or "rejected".
        """
        self._drain()
        if self.rejected:
            return "rejected"
        # Our own input is scheduled for a frame in the future, and played when that
        # frame comes round -- the SAME delay the peer's input gets, so both PCs
        # simulate identical input streams.
        at = self.frame + self.delay
        # Everything from the last scheduled frame up to `at`: one frame normally, more
        # right after the delay grew (the gap gets the current pad, and is SENT -- the
        # peer must play it too), none right after it shrank.
        for f in range(self._sched_to + 1, at + 1):
            self.local_inputs[f] = pad & 0xFF
            self._send(_T_INPUT, f, bytes([pad & 0xFF]))
        self._sched_to = max(self._sched_to, at)
        if self.frame not in self.peer_inputs:
            self.stalls += 1
            return "waiting"

        mine = self.local_inputs[self.frame]
        theirs = self.peer_inputs[self.frame]
        self.local.write(0x00B0, bytes([mine]))
        self.peer.write(0x00B0, bytes([theirs]))
        # ⚡ PLAYER 1'S CONSOLE ALWAYS RUNS FIRST, on both PCs -- not "ours first".
        #
        # ⛔ THE DESYNC THIS ENDS, and the four-console test caught it on the first run:
        # each console's frame is followed by a relay (the order the shell's local cable
        # uses, and the one that measures full speed), so WHICH console runs first
        # decides whether a byte crosses this frame or the next. Ordering by "local,
        # then mirror" means the host runs P1,P2 and the joiner runs P2,P1 -- the same
        # match, simulated in two different orders, drifting by one received byte within
        # a few hundred frames.
        #
        # 🔑 ...AND THE TWO ARE INTERLEAVED, not run a whole frame each. This used to be
        # `first.run_frames(1); pump; second.run_frames(1); pump`, which freezes one
        # console for the whole of the other's frame -- a frame of latency on every
        # answer, in one direction, always. Card Fighters' Clash loses its VS handshake
        # to exactly that: measured through the shell's local cable, which had the same
        # shape, the HP exchange that starts a match dies after 118/102 bytes with
        # "LINK ERROR. CHECK CONNECTIONS AND SETTINGS." on one console and "CHOOSING
        # HP." for ever on the other. Mirror play is the answer for that game ONLINE --
        # its cable is local, so it carries no network latency at all -- but only if
        # the cable is stepped like a cable.
        #
        # Deterministic by construction: a fixed slice, a fixed order, the same code on
        # both PCs. Which console goes first still matters as much as it did above.
        first, second = ((self.local, self.peer) if self.hs.host
                         else (self.peer, self.local))
        run_two_consoles_interleaved(first, second, self.link)

        if self.frame % CHECK_EVERY == 0:
            # Fingerprint AFTER the frame ran, labelled with the frame that produced it,
            # and kept until the peer's copy for that same frame turns up -- it will
            # arrive a round trip later, not now.
            self._mine[self.frame] = self.checksum()
            self._send(_T_CHECK, self.frame,
                       _CHECK_BODY.pack(self._mine[self.frame], self._now_ms(),
                                        self._echo()))
        for f in sorted(set(self._mine) & set(self._checks)):
            if self._mine.pop(f) != self._checks.pop(f) and self.desync_at is None:
                self.desync_at = f
        # A checksum whose partner never turns up (a peer that stopped sending them)
        # must not accumulate for the length of the session. Keep a few rounds' worth --
        # enough to cover any round trip -- and forget the rest.
        for book in (self._mine, self._checks):
            while len(book) > 8:
                del book[min(book)]

        # Frames already played are never needed again; keeping them would grow without
        # bound over a long session.
        self.local_inputs.pop(self.frame, None)
        self.peer_inputs.pop(self.frame, None)
        self.frame += 1
        self.frames_run += 1
        self._maybe_shrink()
        return "ran"

    def _echo(self) -> int:
        """The peer's timestamp, sent back ADVANCED BY HOW LONG WE HELD IT.

        ⛔ THE ROUND TRIP ON SCREEN WAS WRONG BY UP TO A SECOND. Checksums leave once a
        second, so the peer's timestamp waited here until our next one before going
        back -- and that wait was counted as network. Measured on a simulated 80 ms
        line: 1030 ms. A report of "worst 900 ms" said nothing about the line.
        Advancing the echo by the hold time takes it out, in the same 4 bytes: a peer
        from before this just keeps the old (inflated) reading, nothing breaks.
        """
        if not self._peer_ts:
            return 0
        held = (self._now_ms() - self._peer_ts_at) & 0xFFFFFFFF
        return ((self._peer_ts + held) & 0xFFFFFFFF) or 1

    def _follow_rtt(self) -> None:
        """Raise the delay at once when the line needs it.

        A delay of D frames covers a one-way trip of about D-1 frames (one frame of
        slack for where in its tick each PC happens to be), so D = ceil(rtt/2) + 1 in
        frames, from the WORST of the last few round trips: the worst is what stalls.
        Lowering stays with _maybe_shrink, one frame at a time.
        """
        need = self._rtt_need()
        if need > self.delay:
            self.delay = min(MAX_DELAY, need)
            self.delay_max_seen = max(self.delay_max_seen, self.delay)
            self._adapt_hold = self.frame + ADAPT_COOLDOWN

    def _rtt_need(self) -> int:
        if not self._rtt_recent:
            return 0
        return int(-(-max(self._rtt_recent) // (2 * FRAME_MS))) + 1

    # --- the delay, adapted ----------------------------------------------------
    def _maybe_shrink(self) -> None:
        """One frame less, at most every ADAPT_CALM frames, while the worst recent
        round trip no longer needs it -- and never under what both players agreed."""
        if self.delay <= self.base_delay or self.frame < self._adapt_hold:
            return
        if not self._rtt_recent or self.delay - 1 < self._rtt_need():
            return
        self.delay -= 1
        self._adapt_hold = self.frame + ADAPT_CALM
