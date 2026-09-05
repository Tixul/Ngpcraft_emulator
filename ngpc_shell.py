"""NgpCraft — the modern front-end.

A clean, PPSSPP-shaped shell over the FAST native core: a left rail (Library /
Settings), a game LIBRARY of cover cards with live thumbnails, a categorized
SETTINGS screen (General / Graphics / Audio / Controls), and a PLAY view with
sound and configurable keys. One window, no debugger clutter, and a theme that
follows Windows by default (see ngpc_theme.py).

    python ngpc_shell.py                 # open the library
    python ngpc_shell.py "<rom>.ngc"     # boot straight into a game

The old PyQt debugger (`ngpc_emu_ui_qt.py`) stays for register-level work; this
is the everyday player. Both drive the same emulation; this one uses the C++
core for real-time speed and audio (see scripts/play.py for the pacing story).
"""

from __future__ import annotations

import ctypes
import hashlib
import os
import re
import stat
import sys
import time
from collections import deque
from datetime import datetime
from pathlib import Path

from PyQt6.QtCore import (Qt, QTimer, QSize, QObject, QThread, QEvent, QDateTime,
                          QRect, pyqtSignal)
from PyQt6.QtGui import (
    QImage, QPixmap, QKeyEvent, QKeySequence, QFont, QFontMetrics, QIcon,
    QPainter, QColor,
)
from PyQt6.QtMultimedia import QAudioFormat, QAudioSink, QMediaDevices
from PyQt6.QtWidgets import (
    QDateTimeEdit, QApplication, QMainWindow, QWidget, QLabel, QPushButton, QVBoxLayout,
    QHBoxLayout, QGridLayout, QScrollArea, QStackedWidget, QListWidget,
    QListWidgetItem, QComboBox, QCheckBox, QSlider, QSpinBox, QLineEdit,
    QFileDialog, QSizePolicy, QFrame, QMessageBox, QMenu, QDialog,
    QPlainTextEdit, QDialogButtonBox, QInputDialog, QRadioButton, QButtonGroup,
    QLayout, QSpacerItem,
)

sys.path.insert(0, str(Path(__file__).resolve().parent))

from core import native  # noqa: E402
from core import rom_loader  # noqa: E402
from core import native_session as ns  # noqa: E402  (clock-mode ids live there)
from core.native_session import (  # noqa: E402
    NativeSession,
    SYSTEM_RAM_PATH as _SYSTEM_RAM,
    SYSTEM_RTC_PATH as _SYSTEM_RTC,
    write_rtc_file as _write_rtc_file,
)
from core.frame_pacer import FramePacer  # noqa: E402
from core.watches import WatchSet  # noqa: E402
from core.cheats import CheatSet  # noqa: E402
from core import link as core_link  # noqa: E402  (the cable's own constants)
from core import link_debug  # noqa: E402  (the debugger's cable tap)
from core.exec_breaks import ExecBreakSet  # noqa: E402
import ngpc_settings as cfg  # noqa: E402
import ngpc_video  # noqa: E402
import ngpc_library as lib  # noqa: E402
import ngpc_input  # noqa: E402
import ngpc_theme  # noqa: E402
import ngpc_filters  # noqa: E402

# Graphics filter plugins: discovered once, from the folder the user pointed at. A
# module-level registry for the same reason PALETTE is one -- the render path is deep
# inside the play loop and has no business carrying a plugin list around.
FILTERS = ngpc_filters.FilterRegistry()
import ngpc_bindmap  # noqa: E402
import ngpc_debug  # noqa: E402
from ngpc_debug import DebugWindow  # noqa: E402

# A 'no cartridge' image for the BIOS-alone boot: 64 KiB of erased flash (0xFF).
# The BIOS reads the flash card-type, sees nothing, and shows its own screen.
_NO_CART = b"\xff" * 0x10000

# Two roots. When frozen into a single .exe (PyInstaller), read-only resources
# bundled inside it (the icon) live under sys._MEIPASS, while user data — ROMs,
# saves, screenshots — must live BESIDE the .exe so it survives across launches
# (the _MEIPASS extraction dir is wiped on exit). From source both are the repo.
if getattr(sys, "frozen", False):
    REPO = Path(sys.executable).resolve().parent      # writable data, next to the .exe
    BUNDLE = Path(getattr(sys, "_MEIPASS", REPO))     # read-only, bundled in the .exe
else:
    REPO = Path(__file__).resolve().parent
    BUNDLE = REPO
SCREEN_W, SCREEN_H = 160, 152
# Real-BIOS boot: how many BIOS frames to let run before handing off to the cart, so the
# whole NEO GEO POCKET intro plays (logo ~frame 300, intro ends ~460) instead of being
# skipped by the 1-frame boot transient that briefly touches the FF5xxx menu region.
BIOS_INTRO_FRAMES = 400
# The left rail MEASURES itself against its longest nav label instead of taking a
# width. Those labels are translated, so a fixed number is a bet on the language:
# 190 px fit English and French and clipped Portuguese ("Ferramentas de depuração").
# The floor is that old width, so nothing changes for a language that already fit;
# the ceiling stops one long label from eating the window.
RAIL_MIN_W, RAIL_MAX_W, RAIL_COLLAPSED_W = 190, 320, 44
RAIL_TEXT_PAD = 2 * 16 + 2 * 8 + 10   # QPushButton#rail padding + rail margins + slack
RAIL_INDENT = "   "                   # the nav entries' hanging indent, on every line
# Below this window width the rail folds itself to the thin strip: on a 1366x768
# laptop (and worse at 125% scaling) 254 px of nav is a quarter of the page. It is
# AUTOMATIC and reversible -- widening the window brings it back -- and it never
# overwrites the user's own collapse preference, see `Shell._auto_rail`.
RAIL_AUTO_COLLAPSE_W = 760
DEFAULT_ROM_DIR = REPO / "roms"          # drop your .ngc/.ngp files here (or pick a folder)
DEFAULT_BIOS = REPO / "bios.bin"         # optional: a real NGPC BIOS enables "Boot BIOS"
# ⛔ BUNDLE, PAS REPO. C'est une ressource QU'ON EXPÉDIE, au même titre que l'icône,
# pas une donnée de l'utilisateur. Cherchée à côté de l'.exe elle n'y était jamais :
# le spec ne l'embarquait pas et le zip de CI ne la posait pas — donc la promesse
# d'ouverture du README, « aucun BIOS nécessaire », était FAUSSE dans le binaire
# distribué, et sans un mot : tout le code la garde derrière `.is_file()`, donc
# l'application se contentait de se comporter comme si elle n'existait pas.
HLE_BIOS = BUNDLE / "hle_bios" / "bios_hle.bin"  # clean-room fallback: runs games with no bios.bin
# The ORIGINAL monochrome NGP's own BIOS, if the user dropped one next to the app.
# Names people actually give that dump -- the extension says nothing about the file.
DEFAULT_BIOS_MONO = [REPO / n for n in
                     ("ngp_bios.bin", "ngp_bios.ngp", "bios_ngp.bin", "ngpbios.bin")]
THUMB_DIR = REPO / "thumbnails"          # auto-rendered covers -- a CACHE, prunable
COVER_DIR = REPO / "covers"              # covers the USER chose -- only ever READ
LIBRARY_DB = REPO / "library.json"       # play counts / last played / favourites


# ---------------------------------------------------------------------------
# Fitting the window to the screen it is actually on
#
# ⛔ THE BUG THIS FIXES. "sur mon lenovo x240 je ne vois pas le menu a gauche,
# l'emulateur se met full screen sans moyen de le fermer autrement que alt f4"
# -- user report 2026-08-02. A window row saved on a big screen (or a settings
# file carried over with the portable build) comes back VERBATIM: Qt's
# `restoreGeometry` only rescues a window that is ENTIRELY off-screen, so one
# that is merely BIGGER than the laptop's panel is restored oversized, with its
# right and bottom edges past the desktop.
# ---------------------------------------------------------------------------
def clamp_geometry(frame: QRect, avail: QRect) -> QRect:
    """The largest part of `frame` that fits inside `avail`, kept where it was.

    Pure so the rule is testable without a second monitor: shrink first (a window
    taller than the screen is the case that hides the title bar), then slide back
    inside. Never grows a window that already fits."""
    w = min(frame.width(), avail.width())
    h = min(frame.height(), avail.height())
    x = min(max(frame.x(), avail.x()), avail.x() + avail.width() - w)
    y = min(max(frame.y(), avail.y()), avail.y() + avail.height() - h)
    return QRect(x, y, w, h)


class _Stretch(QSpacerItem):
    """Marker: a FlowLayout gap that eats the leftover width of ITS row."""


class FlowLayout(QLayout):
    """A horizontal row that WRAPS onto another line instead of overflowing.

    A `QHBoxLayout` has a minimum width equal to the sum of its children, and a
    window narrower than that does not shrink it -- it clips it, silently pushing
    the rightmost controls off the page. That is why the library's buttons and its
    search/sort row disappeared on a 1366x768 laptop.

    `addStretch()` is honoured per ROW, so as long as everything fits on one line
    the layout is pixel-identical to the QHBoxLayout it replaces (title left,
    buttons right); narrower, the row simply becomes two."""

    def __init__(self, parent=None, hspace: int = 6, vspace: int = 6) -> None:
        super().__init__(parent)
        self._items: list = []
        self._h, self._v = hspace, vspace
        self.setContentsMargins(0, 0, 0, 0)

    # -- QLayout plumbing
    def addItem(self, item) -> None:      # noqa: N802  (Qt name)
        self._items.append(item)

    def count(self) -> int:
        return len(self._items)

    def itemAt(self, i: int):             # noqa: N802
        return self._items[i] if 0 <= i < len(self._items) else None

    def takeAt(self, i: int):             # noqa: N802
        return self._items.pop(i) if 0 <= i < len(self._items) else None

    def expandingDirections(self):        # noqa: N802
        return Qt.Orientation(0)

    def addStretch(self) -> None:         # noqa: N802
        self.addItem(_Stretch(0, 0, QSizePolicy.Policy.Minimum,
                              QSizePolicy.Policy.Minimum))

    def addSpacing(self, px: int) -> None:  # noqa: N802
        self.addItem(QSpacerItem(px, 0, QSizePolicy.Policy.Fixed,
                                 QSizePolicy.Policy.Minimum))

    def hasHeightForWidth(self) -> bool:  # noqa: N802
        return True

    def heightForWidth(self, width: int) -> int:   # noqa: N802
        return self._lay(QRect(0, 0, width, 0), apply=False)

    def setGeometry(self, rect: QRect) -> None:    # noqa: N802
        super().setGeometry(rect)
        self._lay(rect, apply=True)

    def minimumSize(self) -> QSize:       # noqa: N802
        # The WIDEST single item, not their sum: everything else can wrap under it.
        size = QSize(0, 0)
        for it in self._items:
            size = size.expandedTo(it.minimumSize())
        m = self.contentsMargins()
        return size + QSize(m.left() + m.right(), m.top() + m.bottom())

    def sizeHint(self) -> QSize:          # noqa: N802
        return self.minimumSize()

    def _lay(self, rect: QRect, apply: bool) -> int:
        m = self.contentsMargins()
        eff = rect.adjusted(m.left(), m.top(), -m.right(), -m.bottom())
        rows: list[list] = []
        cur: list = []
        used = 0
        for it in self._items:
            w = it.sizeHint().width()
            need = w + (self._h if cur else 0)
            if cur and used + need > eff.width():
                rows.append(cur)
                cur, used, need = [], 0, w
            cur.append(it)
            used += need
        if cur:
            rows.append(cur)

        y = eff.y()
        stretched = False        # a row at/after the stretch keeps hugging the right
        for row in rows:
            row_w = sum(it.sizeHint().width() for it in row) + self._h * (len(row) - 1)
            row_h = max((it.sizeHint().height() for it in row), default=0)
            stretches = [it for it in row if isinstance(it, _Stretch)]
            leftover = max(0, eff.width() - row_w)
            share = leftover // len(stretches) if stretches else 0
            # What wraps out of a right-aligned group stays right-aligned: the buttons
            # that were flush right on one line must not jump to the left on two.
            x = eff.x() + (leftover if stretched and not stretches else 0)
            stretched = stretched or bool(stretches)
            for it in row:
                hint = it.sizeHint()
                w = hint.width() + (share if isinstance(it, _Stretch) else 0)
                if apply:
                    it.setGeometry(QRect(x, y + (row_h - hint.height()) // 2,
                                         w, hint.height()))
                x += w + self._h
            y += row_h + self._v
        height = (y - self._v - eff.y()) if rows else 0
        return height + m.top() + m.bottom()


def _resolve_bios(configured: str) -> "Path | None":
    """Pick the BIOS image: an explicitly configured path, else a real bios.bin
    next to the app, else the clean-room HLE fallback (hle_bios/bios_hle.bin) so
    games still run out of the box with no BIOS supplied. A real BIOS, when
    present, always wins -- it gives full fidelity and the console boot animation.

    (It no longer wins on saves or on the link cable: the HLE image drives the
    flash chip and the COM rings itself now -- hle_bios/README.md. What it does
    not do is the boot/setup UI, which is the point of skipping it.)"""
    if configured and Path(configured).is_file():
        return Path(configured)
    if DEFAULT_BIOS.is_file():
        return DEFAULT_BIOS
    return HLE_BIOS if HLE_BIOS.is_file() else None


def resolve_selected_bios(settings) -> "tuple[Path | None, str]":
    """The BIOS that will actually boot, and WHICH SLOT it came from.

    Three slots, one selector (Settings ▸ BIOS): the colour NGPC dump, the mono NGP
    dump, and our own clean-room HLE image. A slot the user selected but never filled
    falls back rather than refusing to boot -- but the slot returned is then the one
    that answered, so the panel can say so instead of quietly booting the other
    machine. Returns (None, choice) only when even the HLE image is missing."""
    choice = cfg.bios_choice(settings)
    if choice == cfg.BIOS_USE_HLE:
        return (HLE_BIOS if HLE_BIOS.is_file() else None), cfg.BIOS_USE_HLE
    if choice == cfg.BIOS_USE_MONO:
        mono = _resolve_bios_mono(cfg.bios_mono_path(settings))
        if mono is not None:
            return mono, cfg.BIOS_USE_MONO
        # No mono dump: the colour BIOS still boots the game, and the K1GE console
        # setting still applies -- close to an NGP, and `_bios_slot_note` says which.
    found = _resolve_bios(cfg.bios_path(settings))
    if found is None:
        return None, cfg.BIOS_USE_COLOUR
    return found, (cfg.BIOS_USE_HLE if found == HLE_BIOS else cfg.BIOS_USE_COLOUR)


def console_is_mono(settings) -> bool:
    """Is this a mono NGP? Decided by THE BIOS THAT WILL BOOT, not by the slot it sits in.

    A cartridge detects its console by reading 0x6F91, and that byte is stamped by the
    BIOS -- so "which machine am I" and "which BIOS is loaded" are the same question,
    and the emulator must not answer them differently. We identify the image itself
    (`bios_kind`), so a mono dump saved in the colour slot still boots a mono NGP and a
    colour dump in the mono slot does not pretend otherwise: what is loaded is what runs.

    The explicit Console setting still forces mono on top -- it is the only way to run
    the mono machine for a user who has no NGP dump at all (our HLE image is neither
    console's BIOS), and it is what the K1GE restrictions hung on before this existed."""
    if cfg.mono_mode(settings) == cfg.MONO_K1GE:
        return True
    found, slot = resolve_selected_bios(settings)
    kind = bios_kind(found)
    if kind != BIOS_UNKNOWN:
        return kind == BIOS_MONO
    # An image we cannot identify is taken at its slot's word.
    return slot == cfg.BIOS_USE_MONO


def _resolve_bios_mono(configured: str) -> "Path | None":
    """The monochrome NGP's own BIOS, or None. NO fallback on purpose: falling back
    to the colour BIOS would boot an NGPC while the settings say NGP, which is the
    confusion this whole setting exists to end. The caller decides what to do with
    None (we keep the colour BIOS + the K1GE restrictions, and say so)."""
    if configured and Path(configured).is_file():
        return Path(configured)
    for cand in DEFAULT_BIOS_MONO:
        if cand.is_file():
            return cand
    return None


# What tells the two BIOS images apart, proven by disassembling both dumps: each one
# stamps the console-type byte at 0x6F91 with its own machine's id -- `ld (0x6F91),#`,
# encoded `F1 91 6F 00 <value>` -- and only the colour one ever addresses a K2GE
# register (0x87E2, encoded `E2 87 00`). A cartridge reads 0x6F91 and nothing else to
# know which console it is in, so this is the same test the games do.
_BIOS_STAMP_COLOUR = b"\xF1\x91\x6F\x00\x10"
_BIOS_STAMP_MONO = b"\xF1\x91\x6F\x00\x00"
_BIOS_K2GE_REG = b"\xE2\x87\x00"
BIOS_COLOUR, BIOS_MONO, BIOS_UNKNOWN = "colour", "mono", "unknown"


_BIOS_KIND_CACHE: dict[tuple, str] = {}


def bios_kind(path: Path | None) -> str:
    """Which console an image is the BIOS of: `BIOS_COLOUR` (NGPC), `BIOS_MONO` (the
    original NGP) or `BIOS_UNKNOWN` (not a BIOS we recognise -- our own HLE image
    included, which stamps neither byte). Used to tell the user, when they pick one,
    that they just handed the mono slot a colour BIOS."""
    if path is None:
        return BIOS_UNKNOWN
    try:
        st = path.stat()
        key = (str(path), st.st_mtime_ns, st.st_size)
        cached = _BIOS_KIND_CACHE.get(key)
        if cached is not None:
            return cached
        data = path.read_bytes()
    except OSError:
        return BIOS_UNKNOWN
    if _BIOS_STAMP_COLOUR in data and _BIOS_K2GE_REG in data:
        kind = BIOS_COLOUR
    elif _BIOS_STAMP_MONO in data and _BIOS_K2GE_REG not in data:
        kind = BIOS_MONO
    else:
        kind = BIOS_UNKNOWN
    _BIOS_KIND_CACHE[key] = kind
    return kind
APP_ICON = BUNDLE / "assets" / "icone_ngpcraft.ico"
STATE_DIR = REPO / "savestates"
WATCH_DIR = REPO / "watches"             # per-ROM named memory watches (debugger)
STATE_SLOTS = 8
# Snapshot = the whole working image (I/O + RAM + VRAM, 0x0000..0xBFFF) + the CPU + the
# machine state that is NOT memory (sound CPU, sound chip, timers -- core.native.AuxState).
# Cart flash saves live in the battery file, not here.
#
# ⛔ v1 SNAPSHOTS HAD NO SOUND BLOCK, AND THE MUSIC DIED ON EVERY LOAD. The comment here
# used to claim "the internal timing (scanline/timers/z80) re-syncs on the next VBlank".
# It does not: the sound driver runs on a SECOND CPU whose RAM is in the image but whose
# PC and pointers were not, so a load dropped a Z80 mid-playback onto a driver state from
# somewhere else. The main CPU thinks it already asked for that music, so it never asks
# again -- until the game changes scene and issues a fresh command, which is exactly the
# "a scene change fixes it" in the bug report. v1 files still load (without the block).
#
# ⛔ v2 SNAPSHOTS HAD NO CABLE, AND A LINKED SESSION RESTORED TO THE WRONG ONE. Same
# family, one subsystem over: serial channel 0's FIFOs, shift register and handshake
# pins live in the core's Machine, not in the image, so a v2 restore was a NO-OP on the
# cable. MEASURED 2026-08-04: captured at rx_depth=2, ran 30 frames to rx_depth=3,
# restored -- every field stayed at the post-30-frame value; and two re-simulations from
# the "same" restored state diverged by one cable byte in 60 frames. Reachable wherever
# F2 and rewind are live in a cabled session (local 2P, direct-IP; mirror refuses them
# for its own reason). v3 adds core.native.LinkState. v1 and v2 files still load, each
# without the blocks it predates. See LINK_NETPLAY_STUDY.md.
STATE_MEM_LEN = 0x00C000
# v4 : + l'horloge calendaire. ⚡ ELLE MANQUAIT, ET CA CASSAIT LE DETERMINISME.
# L'horloge est de l'etat MACHINE, pas de la memoire : restaurer l'image remet bien ses
# registres 0x90-0x97, mais son compteur interne continue d'ou il en etait et les
# reecrit au tick suivant. Mesure : sauver / jouer 90 trames / restaurer / rejouer
# 90 trames donnait un octet different a **0x000096**, sur Fatal Fury comme sur Metal
# Slug, sous les DEUX modeles de temps. Restaurer l'horloge rend le rejeu exact.
# ⇒ prerequis de tout diagnostic de desync : un netplay ne peut pas etre plus
# deterministe que le rejeu local.
STATE_MAGIC = b"NGPCST04"
STATE_MAGIC_V3 = b"NGPCST03"
STATE_MAGIC_V2 = b"NGPCST02"
STATE_MAGIC_V1 = b"NGPCST01"
CRASH_DIR = REPO / "crashes"
# stop_status codes the core reports; the ones here mean the ROM did something the CPU
# stopped on (a fault or a not-yet-ported encoding) -- worth a crash report.
_REG_NAMES = ("XWA", "XBC", "XDE", "XHL", "XIX", "XIY", "XIZ", "XSP")
_STATUS_DESC = {
    10: ("SILICON_BROKEN", "encoding that breaks on real silicon"),
    11: ("SILICON_UNDEFINED", "encoding whose result the hardware leaves undefined"),
    12: ("DIVISION_BY_ZERO", "divide by zero / quotient overflow"),
    13: ("BIOS_SHUTDOWN", "the BIOS powered the console off"),
    # Only ever reached with ngpc_set_hw_guard armed -- the player never arms it, so
    # these describe a gated run (the analyzer, a build gate), not ordinary play.
    14: ("SYSTEM_STACK_VIOLATION",
         "cartridge XSP entered BIOS-owned RAM at 0x6C01..0x6FFF"),
    15: ("WATCHDOG_RESET",
         "armed watchdog starved; the SDK asks for 0x006F ← 0x4E at least every 100 ms"),
    20: ("UNKNOWN_OPCODE", "byte the decoder does not recognise"),
    21: ("TRUNCATED", "instruction ran off the end of memory"),
    22: ("UNMAPPED", "access to an unmapped address"),
    30: ("UNIMPLEMENTED", "a valid encoding this core has not ported yet"),
}
_CRASH_STATUSES = frozenset(_STATUS_DESC) - {13}   # 13 is a clean power-off, not a crash
THUMB_VERSION = 5       # bump to invalidate the on-disk cache after a render change
# Frames to sample for a thumbnail. We keep the RICHEST one so a boot logo, a
# fade-to-black or a mono fade-to-white does not become the cover, and we sample
# DEEP (titles/attract can be late) but STOP EARLY once a frame is clearly a real
# screen -- so a colourful game costs one sample, only the stubborn ones cost six.
THUMB_SAMPLE_FRAMES = (360, 600, 840, 1120, 1500, 1900)
THUMB_GOOD_ENOUGH = 22   # distinct-colour score that ends the search early
# Under this many distinct colours the capture carries NO PICTURE: one flat fill,
# maybe a second colour. That is what a cartridge sitting on its boot screen looks
# like -- with no BIOS a commercial game never gets past it and every frame comes
# back solid white (0xFFF) or solid black. Such a frame is never cached: writing it
# would pin a white box to disk for good, which is exactly the bug this guards
# against. The card keeps its placeholder and the next launch tries again.
# The floor is safe: the thinnest REAL cover measured (a white title screen) scores
# 4, so 2 separates "nothing on screen" from "a legitimately plain screen".
THUMB_BLANK_SCORE = 2

# The palette every widget paints with. A module global rather than a value
# threaded through constructors: cards and stars are built deep inside layout
# code that has no business carrying a theme argument, and the Shell owns the
# only writer (`_apply_theme`). Read it at PAINT time, never cache a colour --
# a colour captured at import is a colour that survives a theme change.
PALETTE = ngpc_theme.DARK

# Right-click menu labels, a module global for the same reason PALETTE is one: cards
# are built deep inside layout code that has no business carrying a language argument,
# and the Shell owns the only writer (`LibraryPage.retranslate`). Read at MENU time,
# never cached in an action -- a label captured at build time survives a language switch.
MENU_TEXT = {
    "analyze": "🔍  Analyze ROM…",
    "cover_set": "🖼  Choose cover image…",
    "cover_reset": "↺  Back to the auto cover",
}


# ---------------------------------------------------------------- the ROM scan
# Everything the library lists: a bare cartridge image, or an archive holding one.
ROM_EXTS = frozenset({".ngc", ".ngp", ".zip", ".7z"})
# How deep under the ROM folder we are willing to look. A collection is a handful of
# folders deep; anything past this is a link that points back into the tree (see
# `scan_roms`), and a depth limit is what stops us walking it forever.
SCAN_MAX_DEPTH = 12
# The two reparse tags that mean "this directory is really somewhere else": a
# junction (what `mklink /J`, and every drive-sync tool, makes) and a symlink. NOT
# every reparse point -- a OneDrive "files on-demand" folder is one too, and skipping
# those would hide a library that lives in OneDrive, which is where many of them do.
_LINK_TAGS = {getattr(stat, "IO_REPARSE_TAG_MOUNT_POINT", 0xA0000003),
              getattr(stat, "IO_REPARSE_TAG_SYMLINK", 0xA000000C)}


def _is_link_dir(entry: os.DirEntry) -> bool:
    """True for a directory that is a junction or a symlink.

    ⚠️ `entry.is_symlink()` is FALSE for a Windows junction -- which is why
    `os.walk(followlinks=False)` does not protect against one. The reparse TAG is
    what actually says so."""
    try:
        if entry.is_symlink():
            return True
        return getattr(entry.stat(follow_symlinks=False), "st_reparse_tag", 0) in _LINK_TAGS
    except OSError:
        return False


def _file_id(path: Path) -> object:
    """What makes two directory entries the SAME file: the volume + inode pair
    Windows fills in as well as Unix. Falls back to the resolved path when the
    filesystem does not number its files."""
    try:
        st = path.stat()
    except OSError:
        return str(path)
    if st.st_ino:
        return (st.st_dev, st.st_ino)
    try:
        return os.path.realpath(path).casefold()
    except OSError:
        return str(path)


def choose_game_in_archive(parent, path: str | Path, lang: str) -> str | None:
    """`path` as opened by the user, resolved to ONE game.

    A collection archive opened by hand is ambiguous, and the old answer -- load the
    largest member -- silently picked a game nobody asked for. So ask; a bare ROM or a
    single-game archive is returned untouched and nothing is asked. None = cancelled.
    """
    p = Path(path)
    if p.suffix.lower() not in rom_loader.ARCHIVE_EXTS:
        return str(p)
    try:
        members = rom_loader.list_roms(p)
    except Exception:
        return str(p)
    if len(members) < 2:
        return str(p)
    labels = [Path(m).stem for m in members]
    choice, ok = QInputDialog.getItem(
        parent, cfg.tr(lang, "archive_pick_title"),
        cfg.tr(lang, "archive_pick_label").format(archive=p.name), labels, 0, False)
    if not ok:
        return None
    return str(p / members[labels.index(choice)])


def scan_roms(root: Path, max_depth: int = SCAN_MAX_DEPTH) -> list[Path]:
    """Every ROM and archive under `root` -- each PHYSICAL file exactly ONCE.

    Hand-rolled rather than `rglob`, because of the bug a user hit: ONE cartridge in
    the folder, TWO cards in the library. A junction makes the same file reachable
    under two paths and `rglob` walks straight through it (with one pointing back up
    the tree it recurses until Windows refuses the path, and the `except OSError`
    around it then swallowed the WHOLE scan, so the library came back half empty).
    Junctions are not exotic: drive-sync clients, a `roms` folder linked into a
    projects tree, and the legacy profile folders are all made of them.

    So: shallowest-first (a file keeps the path the user would name), never descend
    through a link, cap the depth anyway, and dedupe on file identity -- which also
    catches hardlinks and two drive letters onto one volume. A folder that cannot be
    read is skipped; it never takes the rest of the library with it.
    """
    out: list[Path] = []
    seen: set[object] = set()
    queue: deque[tuple[Path, int]] = deque([(Path(root), 0)])
    visited_dirs: set[object] = {_file_id(Path(root))}
    while queue:
        folder, depth = queue.popleft()
        try:
            with os.scandir(folder) as it:
                entries = list(it)
        except OSError:
            continue
        for entry in entries:
            try:
                is_dir = entry.is_dir(follow_symlinks=False)
            except OSError:
                continue
            if is_dir:
                if depth >= max_depth or _is_link_dir(entry):
                    continue
                ident = _file_id(Path(entry.path))
                if ident in visited_dirs:
                    continue          # a loop we did not recognise as a link
                visited_dirs.add(ident)
                queue.append((Path(entry.path), depth + 1))
                continue
            if os.path.splitext(entry.name)[1].casefold() not in ROM_EXTS:
                continue
            path = Path(entry.path)
            ident = _file_id(path)
            if ident in seen:
                continue
            seen.add(ident)
            out.extend(_games_in(path))
    out.sort()
    return out


def _games_in(path: Path) -> list[Path]:
    """One entry per GAME for a physical file: usually itself, several for a
    collection archive.

    ⚡ AN ARCHIVE HOLDING SEVERAL GAMES BECOMES SEVERAL CARDS, each addressed by a
    virtual `Pack.zip/Game A.ngc` path. Everything a game owns is derived from its
    `Path` -- the flash save (`saves/<stem>.flash`), the savestates, the watches, the
    cover, the library entry -- so this hands each game inside the archive its own,
    and `Pack.zip` alone would have given forty games ONE shared save file. (It would
    also have booted whichever member happened to be the largest, silently.)

    ⛔ AN ARCHIVE WITH ONE GAME KEEPS ITS OWN PATH, and that is not tidiness: today
    `Sonic.zip` saves to `saves/Sonic.flash`, and expanding it to `Sonic.zip/Sonic
    Pocket Adventure.ngc` would rename the save, the states and the cover of every
    single-game archive in every existing library. The new shape is only used where
    the old one cannot work.
    """
    if path.suffix.lower() not in rom_loader.ARCHIVE_EXTS:
        return [path]
    try:
        members = rom_loader.list_roms(path)
    except Exception:
        return [path]          # unreadable archive: one card, and the loader will say why
    if len(members) < 2:
        return [path]
    return [path / m for m in members]


# ---------------------------------------------------------------- thumbnails
# Image types accepted as a hand-picked cover.
_COVER_EXTS = (".png", ".jpg", ".jpeg", ".bmp", ".gif", ".webp")
# What an AUTO-rendered cover is named: "<stem>.<8 hex>.v<N>.png". The prune below
# matches on this and nothing else -- a file a human put in the folder is not ours
# to delete, and deleting it is exactly what used to eat a hand-placed title screen
# on every update that bumped THUMB_VERSION.
_AUTO_COVER_RE = re.compile(r"\.[0-9a-f]{8}\.v\d+\.png$", re.IGNORECASE)
def _path_tag(rom: Path) -> str:
    """8 hex digits identifying a ROM by its FULL path."""
    return hashlib.md5(str(rom).encode("utf-8", "surrogatepass")).hexdigest()[:8]


def _cover_path(rom: Path) -> Path:
    """The on-disk cover cache for a ROM -- UNIQUE per full path. Two projects can
    each hold a `main.ngc`; a stem-only name would make them share one cover (and,
    with a recursive scan, overwrite each other)."""
    return THUMB_DIR / f"{rom.stem}.{_path_tag(rom)}.v{THUMB_VERSION}.png"


def custom_cover(rom: Path) -> Path | None:
    """The cover the USER chose for this ROM, if any. It beats the rendered one and
    is never regenerated, pruned, or invalidated by a THUMB_VERSION bump: `covers/`
    is a folder the emulator only ever reads. Two names are accepted, most specific
    first -- the tagged one pins the cover to ONE file on disk (two NgpCraft projects
    both build a `main.ngc`), the plain one is drop-in friendly and keeps working
    when the whole install moves."""
    if not COVER_DIR.is_dir():
        return None
    for stem in (f"{rom.stem}.{_path_tag(rom)}", rom.stem):
        for ext in _COVER_EXTS:
            p = COVER_DIR / f"{stem}{ext}"
            if p.is_file():
                return p
    return None


class ThumbWorker(QObject):
    """Renders a small screenshot per ROM on a background thread, once, and
    caches it to disk. Never touches the UI thread except via `ready`."""

    ready = pyqtSignal(str, QImage)
    done = pyqtSignal()

    def __init__(self, roms: list[Path], bios: Path | None, mono: bool = False) -> None:
        super().__init__()
        self._roms = roms
        self._bios = bios if bios and bios.exists() else None
        # A cover is rendered by booting the game, so it must boot on the console the
        # user selected -- otherwise the library shows colour covers for a mono NGP.
        self._mono = mono
        self._stop = False

    def stop(self) -> None:
        self._stop = True

    def _prune(self) -> None:
        """Drop auto-rendered covers from an OLDER render version, so the folder does
        not grow a copy per THUMB_VERSION bump.

        ⚠️ IT NO LONGER PURGES WHEN THE BIOS CHANGES. It briefly did: a cover is named
        after the ROM path alone, so switching BIOS kept showing the covers the previous
        one had produced, and re-rendering looked like the correct fix. It is not worth
        it -- re-rendering a real library is minutes of booting every game, for a picture
        that differs by a shade. Covers are rendered ONCE, and `Regenerate covers` in the
        Library is there for the day you actually want them redone.

        Only files this worker wrote are ever removed (`_AUTO_COVER_RE`); anything else
        in this folder was put there by hand and stays.
        """
        keep = f".v{THUMB_VERSION}.png"
        for old in THUMB_DIR.glob("*.png"):
            if _AUTO_COVER_RE.search(old.name) and not old.name.endswith(keep):
                try:
                    old.unlink()
                except OSError:
                    pass

    def run(self) -> None:
        THUMB_DIR.mkdir(exist_ok=True)
        self._prune()
        for rom in self._roms:
            if self._stop:
                break
            # A cover the user picked wins, and is never re-rendered over.
            src = custom_cover(rom) or _cover_path(rom)
            if src.exists():
                img = QImage(str(src))
                if not img.isNull():
                    self.ready.emit(str(rom), img)
                    continue
            # No BIOS image at all, no rendering. A cartridge booted without one
            # never reaches its title screen -- it sits on a solid white or black
            # frame forever -- so every cover would come out a blank box, and the
            # placeholder is both honest and free. This is now rare: `_bios()` falls
            # back to the clean-room HLE image (hle_bios/bios_hle.bin), so covers
            # render out of the box; this branch only hits if even that is missing.
            if self._bios is None:
                continue
            try:
                img = self._render(rom)
            except Exception:
                continue
            if img is not None:
                img.save(str(_cover_path(rom)))
                self.ready.emit(str(rom), img)
        self.done.emit()

    @staticmethod
    def _content_score(fb: list[int]) -> int:
        """How 'interesting' a frame is = how many distinct colours it shows.
        A boot logo, a black fade or a mono white fade scores near 1; a real
        title screen scores dozens. Sampling every 4th pixel is plenty."""
        seen = set()
        for i in range(0, len(fb), 4):
            seen.add(fb[i])
            if len(seen) > 64:
                break
        return len(seen)

    def _render(self, rom: Path) -> QImage | None:
        best_fb = None
        best_score = -1
        s = NativeSession(rom, bios_path=self._bios, autosave=False,
                          k1ge_console=self._mono)
        try:
            done = 0
            for target in THUMB_SAMPLE_FRAMES:
                if self._stop:
                    break
                s.machine.run_frames(target - done)
                done = target
                fb = s.machine.framebuffer()
                score = self._content_score(fb)
                if score > best_score:
                    best_score, best_fb = score, fb
                if best_score >= THUMB_GOOD_ENOUGH:
                    break             # a real screen -- no need to sample deeper
        finally:
            s.close()
        if best_fb is None or best_score <= THUMB_BLANK_SCORE:
            return None      # nothing on screen -- see THUMB_BLANK_SCORE
        # Build the image from a raw BGRA buffer (per-pixel setPixel would crawl).
        buf = bytearray(SCREEN_W * SCREEN_H * 4)
        i = 0
        for c in best_fb:
            buf[i] = ((c >> 8) & 0x0F) * 17      # B
            buf[i + 1] = ((c >> 4) & 0x0F) * 17  # G
            buf[i + 2] = (c & 0x0F) * 17         # R
            buf[i + 3] = 0xFF
            i += 4
        return QImage(bytes(buf), SCREEN_W, SCREEN_H,
                      QImage.Format.Format_RGB32).copy()


def _art_size(long_edge: int) -> tuple[int, int]:
    """NGPC is 160x152. Fit the cover to `long_edge` on its width."""
    w = long_edge
    h = round(long_edge * SCREEN_H / SCREEN_W)
    return w, h


class _ArtLabel(QLabel):
    """The cover image itself: a placeholder glyph until a thumbnail lands."""

    def __init__(self, w: int, h: int) -> None:
        super().__init__()
        self.setFixedSize(w, h)
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._w, self._h = w, h
        self._placeholder()

    def _placeholder(self) -> None:
        self.setText("◼")
        self.setStyleSheet(
            f"background:{PALETTE.bg_art}; border-radius:8px;"
            f" color:{PALETTE.text_placeholder};"
            f" font-size:{max(16, self._h // 4)}px;")

    def set_image(self, img: QImage) -> None:
        self.setStyleSheet(f"background:{PALETTE.bg_art}; border-radius:8px;")
        self.setPixmap(QPixmap.fromImage(img).scaled(
            self._w, self._h, Qt.AspectRatioMode.IgnoreAspectRatio,
            Qt.TransformationMode.FastTransformation))
        self.setText("")


class _FavStar(QPushButton):
    """The favourite toggle. A button, not a click on the card, so pressing it
    never launches the game underneath it."""

    def __init__(self, on: bool, tip: str) -> None:
        super().__init__("★" if on else "☆")
        self.setObjectName("favStar")
        self.setFixedSize(22, 22)
        self.setToolTip(tip)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setStyleSheet(
            "QPushButton#favStar { border: none; background: transparent; font-size: 15px;"
            f" color: {PALETTE.fav_on if on else PALETTE.fav_off}; }}"
            f"QPushButton#favStar:hover {{ color: {PALETTE.fav_on}; }}")


class _RomMenuMixin:
    """Right-click on a game: the actions that are about the FILE rather than about
    playing it. Both the grid card and the list row need exactly this."""

    # re-declared as signals on each concrete class
    analyze_requested = None
    cover_set_requested = None
    cover_reset_requested = None

    def contextMenuEvent(self, e) -> None:  # type: ignore[override]
        menu = QMenu(self)
        rom = str(self.rom)
        menu.addAction(MENU_TEXT["analyze"]).triggered.connect(
            lambda: self.analyze_requested.emit(rom))
        menu.addSeparator()
        menu.addAction(MENU_TEXT["cover_set"]).triggered.connect(
            lambda: self.cover_set_requested.emit(rom))
        # Only offer the reset when there is a chosen cover to drop.
        if custom_cover(self.rom) is not None:
            menu.addAction(MENU_TEXT["cover_reset"]).triggered.connect(
                lambda: self.cover_reset_requested.emit(rom))
        menu.exec(e.globalPos())


class GameCard(_RomMenuMixin, QFrame):
    """A grid cover: art on top, name and play stats under it."""

    clicked = pyqtSignal(str)
    fav_toggled = pyqtSignal(str)
    analyze_requested = pyqtSignal(str)
    cover_set_requested = pyqtSignal(str)
    cover_reset_requested = pyqtSignal(str)

    def __init__(self, rom: Path, long_edge: int, sub: str, fav: bool, fav_tip: str,
                 title: str | None = None) -> None:
        super().__init__()
        self.setObjectName("card")
        self.rom = rom
        w, h = _art_size(long_edge)
        self.setFixedWidth(w + 16)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        # The card is narrow and the title is capped at two lines, so a disambiguated
        # (full) name can be clipped: the FULL PATH is always one hover away -- which
        # is also what tells two cards that look alike apart, and where each lives.
        self.setToolTip(str(rom))
        lay = QVBoxLayout(self)
        lay.setContentsMargins(8, 8, 8, 10)
        lay.setSpacing(6)
        self.art = _ArtLabel(w, h)
        name = QLabel(title or _pretty(rom.stem))
        name.setObjectName("cardName")
        name.setWordWrap(True)
        name.setAlignment(Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop)
        # Room for two full lines (a one-line title just leaves even space so the
        # cards keep a common height). Was 30px -> the 2nd line was cut in half.
        name.setFixedHeight(2 * name.fontMetrics().lineSpacing() + 6)
        lay.addWidget(self.art, 0, Qt.AlignmentFlag.AlignHCenter)
        lay.addWidget(name)
        # Stats line + star share one row so the card height stays uniform.
        foot = QHBoxLayout(); foot.setContentsMargins(0, 0, 0, 0); foot.setSpacing(4)
        self._sub = QLabel(sub); self._sub.setObjectName("hint")
        self._sub.setStyleSheet("font-size:11px;")
        foot.addWidget(self._sub); foot.addStretch()
        self.star = _FavStar(fav, fav_tip)
        self.star.clicked.connect(lambda: self.fav_toggled.emit(str(self.rom)))
        foot.addWidget(self.star)
        lay.addLayout(foot)

    def set_image(self, img: QImage) -> None:
        self.art.set_image(img)

    def mousePressEvent(self, e) -> None:  # type: ignore[override]
        if e.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit(str(self.rom))


class GameRow(_RomMenuMixin, QFrame):
    """A list row: a small cover (optional) + the name and play stats, full width."""

    clicked = pyqtSignal(str)
    fav_toggled = pyqtSignal(str)
    analyze_requested = pyqtSignal(str)
    cover_set_requested = pyqtSignal(str)
    cover_reset_requested = pyqtSignal(str)

    def __init__(self, rom: Path, long_edge: int, show_art: bool,
                 sub: str, fav: bool, fav_tip: str, title: str | None = None) -> None:
        super().__init__()
        self.setObjectName("card")
        self.rom = rom
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setToolTip(str(rom))
        h = QHBoxLayout(self)
        h.setContentsMargins(10, 6, 14, 6)
        h.setSpacing(12)
        self.art = None
        if show_art:
            w, ah = _art_size(long_edge)
            self.art = _ArtLabel(w, ah)
            self.setFixedHeight(ah + 12)
            h.addWidget(self.art)
        else:
            self.setFixedHeight(40)
        name = QLabel(title or _pretty(rom.stem))
        name.setObjectName("cardName")
        h.addWidget(name)
        h.addStretch()
        self._sub = QLabel(sub); self._sub.setObjectName("hint")
        self._sub.setStyleSheet("font-size:11px;")
        h.addWidget(self._sub)
        self.star = _FavStar(fav, fav_tip)
        self.star.clicked.connect(lambda: self.fav_toggled.emit(str(self.rom)))
        h.addWidget(self.star)

    def set_image(self, img: QImage) -> None:
        if self.art is not None:
            self.art.set_image(img)

    def mousePressEvent(self, e) -> None:  # type: ignore[override]
        if e.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit(str(self.rom))


def _pretty(stem: str) -> str:
    # Trim the usual "(USA)", "(Japan) (En) ..." dump tags for a cleaner card.
    cut = stem.split(" (")[0].split(" [")[0]
    return cut.strip() or stem


def _titles(roms: list[Path]) -> dict[str, str]:
    """The title each card shows, decided over the WHOLE library at once.

    `_pretty` drops the dump tags, which is what makes a grid of "Faselei! (Europe)"
    readable -- but two dumps of one game ("Biomotor Unitron (USA)" and "(USA, Europe)",
    a translation patch and its base ROM) then read as the SAME title on two cards.
    That is the bug a user reported as "two thumbnails for one ROM": two real files,
    one visible name. A title that is not unique therefore keeps the full file name,
    so the cards say what actually differs between them.
    """
    counts: dict[str, int] = {}
    for rom in roms:
        key = _pretty(rom.stem).casefold()
        counts[key] = counts.get(key, 0) + 1
    out: dict[str, str] = {}
    for rom in roms:
        short = _pretty(rom.stem)
        out[str(rom)] = short if counts[short.casefold()] == 1 else rom.stem
    return out


# ---------------------------------------------------------------- library
class RomReportDialog(QDialog):
    """The ROM analysis, as text you can read and save."""

    def __init__(self, parent, title: str, text: str, lang: str = "en") -> None:
        super().__init__(parent)
        self._lang = lang
        self.setWindowTitle(cfg.tr(lang, "rom_report_title").format(name=title))
        self.resize(760, 560)
        lay = QVBoxLayout(self)
        view = QPlainTextEdit(text); view.setReadOnly(True)
        view.setFont(QFont("Consolas", 10))
        lay.addWidget(view)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save |
                                   QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.reject)
        buttons.button(QDialogButtonBox.StandardButton.Save).clicked.connect(
            lambda: self._save(text, title))
        lay.addWidget(buttons)

    def _save(self, text: str, title: str) -> None:
        path, _ = QFileDialog.getSaveFileName(
            self, cfg.tr(self._lang, "rom_report_save"),
            f"{title}_analysis.txt", cfg.tr(self._lang, "filter_text_files"))
        if path:
            Path(path).write_text(text, encoding="utf-8")


class LibraryPage(QWidget):
    play_requested = pyqtSignal(str)
    boot_bios_requested = pyqtSignal()

    def __init__(self, settings, library: lib.Library) -> None:
        super().__init__()
        self.setObjectName("page")
        self._settings = settings
        self._lib = library
        self._items: dict[str, QWidget] = {}     # rom -> current card/row widget
        self._images: dict[str, QImage] = {}      # rom -> thumbnail, kept for reflow
        self._grid = None                         # QGridLayout when in grid view
        self._grid_cards: list[QWidget] = []      # cards in order, for re-flow
        self._grid_cols = 0
        self._grid_card_w = 0
        self._all_roms: list[Path] = []           # everything the scan found
        self._roms: list[Path] = []               # ...after search / filter / sort
        self._thread: QThread | None = None
        self._worker: ThumbWorker | None = None
        self._rendered_with: Path | None = None   # BIOS the current covers came from

        root = QVBoxLayout(self)
        root.setContentsMargins(28, 22, 28, 16)
        root.setSpacing(14)

        # Both bars WRAP (FlowLayout) instead of overflowing: on a small laptop a
        # QHBoxLayout pushed "Ouvrir une ROM", the search box and the sort controls
        # clean off the right edge, with no scrollbar to reach them.
        header = FlowLayout(hspace=6, vspace=6)
        self._title = QLabel()
        self._title.setObjectName("pageTitle")
        header.addWidget(self._title)
        header.addStretch()
        self._bios_btn = QPushButton()
        self._bios_btn.setObjectName("ghost")
        self._bios_btn.clicked.connect(self.boot_bios_requested.emit)
        self._folder_btn = QPushButton()
        self._folder_btn.setObjectName("ghost")
        self._folder_btn.clicked.connect(self._choose_folder)
        self._regen_btn = QPushButton()
        self._regen_btn.setObjectName("ghost")
        self._regen_btn.clicked.connect(self.regenerate_covers)
        self._open_btn = QPushButton()
        self._open_btn.setObjectName("primary")
        self._open_btn.clicked.connect(self._open_rom)
        header.addWidget(self._bios_btn)
        header.addWidget(self._folder_btn)
        header.addWidget(self._regen_btn)
        header.addWidget(self._open_btn)
        root.addLayout(header)

        # view controls: mode (grid/list/compact) + cover size
        controls = FlowLayout(hspace=8, vspace=6)
        self._view_btns: dict[str, QPushButton] = {}
        for mode in (cfg.VIEW_GRID, cfg.VIEW_LIST, cfg.VIEW_COMPACT):
            b = QPushButton(); b.setObjectName("ghost"); b.setCheckable(True)
            b.clicked.connect(lambda _=False, m=mode: self._set_view(m))
            self._view_btns[mode] = b
            controls.addWidget(b)
        controls.addSpacing(16)
        self._size_lbl = QLabel(); self._size_lbl.setObjectName("hint")
        controls.addWidget(self._size_lbl)
        self._size = QSlider(Qt.Orientation.Horizontal)
        self._size.setFixedWidth(150); self._size.setRange(80, 240)
        self._size.setValue(cfg.thumb_size(self._settings))
        self._size.valueChanged.connect(self._on_size)
        controls.addWidget(self._size)
        controls.addStretch()

        # search / filter / sort -- the three things a listing of more than a
        # dozen ROMs needs and the old one had none of.
        self._search = QLineEdit()
        self._search.setFixedWidth(190)
        self._search.setClearButtonEnabled(True)
        self._search.textChanged.connect(self._on_search)
        controls.addWidget(self._search)

        self._filterbox = QComboBox()
        for fid in (lib.FILTER_ALL, lib.FILTER_FAV, lib.FILTER_NEVER):
            self._filterbox.addItem("", fid)
        self._filterbox.currentIndexChanged.connect(self._on_filter)
        controls.addWidget(self._filterbox)

        self._sortbox = QComboBox()
        for sid in lib.SORT_KEYS:
            self._sortbox.addItem("", sid)
        self._sortbox.currentIndexChanged.connect(self._on_sort)
        controls.addWidget(self._sortbox)

        # One direction toggle instead of a duplicate menu entry per key: this is
        # what turns "most played" into "least played" and A->Z into Z->A.
        self._revbtn = QPushButton("↓")
        self._revbtn.setObjectName("ghost"); self._revbtn.setCheckable(True)
        self._revbtn.setFixedWidth(34)
        self._revbtn.clicked.connect(self._on_reverse)
        controls.addWidget(self._revbtn)
        root.addLayout(controls)

        self._empty = QLabel()
        self._empty.setObjectName("hint")
        self._empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._empty.setWordWrap(True)   # its text is set dynamically -- wrap so it never
        self._empty.setMinimumWidth(1)  # dictates the window width
        root.addWidget(self._empty)

        # Why the covers are blank, said once, only when it is true.
        self._bios_hint = QLabel()
        self._bios_hint.setObjectName("hint")
        self._bios_hint.setWordWrap(True)
        self._bios_hint.setVisible(False)
        root.addWidget(self._bios_hint)

        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(True)
        self._host = None
        root.addWidget(self._scroll, 1)

        self.retranslate()
        self.reload()

    def retranslate(self) -> None:
        lang = cfg.language(self._settings)
        self._title.setText(cfg.tr(lang, "library"))
        self._bios_btn.setText(cfg.tr(lang, "boot_bios"))
        self._folder_btn.setText(cfg.tr(lang, "set_folder"))
        self._regen_btn.setText(cfg.tr(lang, "covers_regen"))
        self._regen_btn.setToolTip(cfg.tr(lang, "covers_regen_tip"))
        self._open_btn.setText(cfg.tr(lang, "open_rom"))
        self._empty.setText(cfg.tr(lang, "no_roms"))
        self._bios_hint.setText(cfg.tr(lang, "covers_need_bios"))
        # Only offer "Boot BIOS" when a REAL BIOS is available -- the console boot
        # (intro + language/clock) needs it; the clean-room HLE fallback can't do it.
        _b = self._bios()
        self._bios_btn.setVisible(_b is not None and _b != HLE_BIOS)
        self._sync_bios_hint()
        self._view_btns[cfg.VIEW_GRID].setText(cfg.tr(lang, "view_grid"))
        self._view_btns[cfg.VIEW_LIST].setText(cfg.tr(lang, "view_list"))
        self._view_btns[cfg.VIEW_COMPACT].setText(cfg.tr(lang, "view_compact"))
        self._size_lbl.setText(cfg.tr(lang, "thumb_size"))
        # Right-click labels live in a module global (see MENU_TEXT); this is its
        # only writer, so a language switch reaches menus built on any card.
        MENU_TEXT.update({k: cfg.tr(lang, f"menu_{k}")
                          for k in ("analyze", "cover_set", "cover_reset")})
        self._search.setPlaceholderText(cfg.tr(lang, "search"))
        self._revbtn.setToolTip(cfg.tr(lang, "sort_reverse"))
        # Re-label the combos in place; blocking the signal keeps a language switch
        # from looking like the user re-picked a sort key (which would re-lay out).
        for box, keys in ((self._filterbox, (lib.FILTER_ALL, lib.FILTER_FAV, lib.FILTER_NEVER)),
                          (self._sortbox, lib.SORT_KEYS)):
            box.blockSignals(True)
            for i, sid in enumerate(keys):
                box.setItemText(i, cfg.tr(lang, f"{'filter' if box is self._filterbox else 'sort'}_{sid}"))
            box.blockSignals(False)
        self._sync_view_buttons()
        self._sync_arrange_controls()

    def _sync_arrange_controls(self) -> None:
        """Push the saved search/sort/filter into the widgets without re-triggering
        their handlers (which would save them straight back and re-lay out)."""
        for box, value in ((self._sortbox, cfg.library_sort(self._settings)),
                           (self._filterbox, cfg.library_filter(self._settings))):
            idx = box.findData(value)
            if idx >= 0 and idx != box.currentIndex():
                box.blockSignals(True); box.setCurrentIndex(idx); box.blockSignals(False)
        rev = cfg.library_reverse(self._settings)
        self._revbtn.setChecked(rev)
        self._revbtn.setText("↑" if rev else "↓")

    def _rom_dir(self) -> Path | None:
        d = cfg.rom_folder(self._settings)
        if d and Path(d).is_dir():
            return Path(d)
        if DEFAULT_ROM_DIR.is_dir():
            return DEFAULT_ROM_DIR
        return None

    def _bios(self) -> Path | None:
        return resolve_selected_bios(self._settings)[0]

    def _sync_bios_hint(self) -> None:
        """Covers are rendered by BOOTING each ROM, which takes a BIOS. Say so when
        there is none, next to the blank cards it explains -- and only then."""
        self._bios_hint.setVisible(bool(self._all_roms) and self._bios() is None)

    def reload(self) -> None:
        self._stop_worker()
        # What the covers about to be rendered were rendered WITH (see showEvent).
        self._rendered_with = self._bios()
        d = self._rom_dir()
        self._all_roms = scan_roms(d) if d else []
        self._images.clear()
        self._arrange()
        self._sync_bios_hint()
        # Render covers for EVERY ROM, not just the visible ones: filtering to
        # favourites and back must not leave the rest of the library blank.
        if self._all_roms:
            self._start_worker(self._all_roms)

    # ---- search / filter / sort
    def _arrange(self, rebuild: bool = True) -> None:
        self._roms = self._lib.arrange(
            self._all_roms,
            cfg.library_sort(self._settings), cfg.library_reverse(self._settings),
            cfg.library_filter(self._settings), self._search.text(), _pretty)
        if rebuild:
            self._rebuild()

    def _on_search(self, _text: str) -> None:
        self._arrange()

    def _on_filter(self, _idx: int) -> None:
        self._settings.setValue("library/filter", self._filterbox.currentData())
        self._arrange()

    def _on_sort(self, _idx: int) -> None:
        self._settings.setValue("library/sort", self._sortbox.currentData())
        self._arrange()

    def _on_reverse(self) -> None:
        rev = self._revbtn.isChecked()
        self._settings.setValue("library/sort_reverse", rev)
        self._revbtn.setText("↑" if rev else "↓")
        self._arrange()

    def analyze_rom(self, rom_str: str) -> None:
        """Boot the ROM in a throwaway core with the hygiene counters armed and report
        what is wrong with it. The thumbnail worker must be stopped first -- two native
        cores at once is the crash `_stop_worker` exists to prevent."""
        from core import romcheck
        rom = Path(rom_str)
        self._stop_worker()
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            report = romcheck.analyse(rom, bios=self._bios(), frames=300)
            text = report.text()
        except Exception as exc:
            text = f"The analysis itself failed:\n\n{type(exc).__name__}: {exc}"
        finally:
            QApplication.restoreOverrideCursor()
        RomReportDialog(self, rom.stem, text, cfg.language(self._settings)).exec()

    def set_cover(self, rom_str: str) -> None:
        """Pick an image and make it this game's cover, for good. It is COPIED into
        `covers/`, a folder the emulator only ever reads, so nothing the app does --
        a re-scan, a cache-version bump, an update -- can put a rendered screenshot
        back in its place. Overwriting the install keeps it too: `covers/` is user
        data that ships in no archive."""
        rom = Path(rom_str)
        lang = cfg.language(self._settings)
        title = cfg.tr(lang, "cover_set")
        path, _ = QFileDialog.getOpenFileName(
            self, title, str(rom.parent),
            "Images (*.png *.jpg *.jpeg *.bmp *.gif *.webp)")
        if not path:
            return
        img = QImage(path)
        if img.isNull():
            QMessageBox.warning(self, title, cfg.tr(lang, "cover_bad"))
            return
        # Same stem twice in the library (every NgpCraft project builds a `main.ngc`)
        # -> pin the cover to THIS path; otherwise use the plain, drop-in name, which
        # also keeps working if the library folder is moved.
        twin = any(o != rom and o.stem == rom.stem for o in self._all_roms)
        stem = f"{rom.stem}.{_path_tag(rom)}" if twin else rom.stem
        target = COVER_DIR / f"{stem}.png"
        try:
            COVER_DIR.mkdir(exist_ok=True)
            # A cover this game already had, under some other extension, would still
            # be found first. Clear the ones at OUR name only -- never a twin's.
            for ext in _COVER_EXTS:
                other = COVER_DIR / f"{stem}{ext}"
                if other != target and other.is_file():
                    other.unlink()
            ok = img.save(str(target), "PNG")
        except OSError as exc:
            QMessageBox.warning(self, title, f"{cfg.tr(lang, 'cover_failed')}\n\n{exc}")
            return
        if not ok:
            QMessageBox.warning(self, title, cfg.tr(lang, "cover_failed"))
            return
        self._apply_cover(rom, img)

    def reset_cover(self, rom_str: str) -> None:
        """Drop the chosen cover and fall back to the rendered screenshot."""
        rom = Path(rom_str)
        dropped = False
        while (p := custom_cover(rom)) is not None:
            try:
                p.unlink()
            except OSError:
                break
            dropped = True
        if not dropped:
            return
        cache = _cover_path(rom)
        if cache.is_file():
            img = QImage(str(cache))
            if not img.isNull():
                self._apply_cover(rom, img)
                return
        # Never rendered (the chosen cover meant we never booted it): render now. The
        # whole list goes back through the worker -- the cached ones come straight back.
        self._stop_worker()
        if self._all_roms:
            self._start_worker(self._all_roms)

    def _apply_cover(self, rom: Path, img: QImage) -> None:
        """Show `img` for `rom` now, and keep it across view/size switches."""
        self._images[str(rom)] = img
        item = self._items.get(str(rom))
        if item is not None:
            item.set_image(img)

    def _on_fav(self, rom_str: str) -> None:
        self._lib.toggle_favorite(Path(rom_str))
        # Re-arrange rather than just repaint the star: under the Favourites
        # filter, un-starring a game must actually drop it out of the view.
        self._arrange()

    def _subtitle(self, rom: Path) -> str:
        """The small stats line under a title: what the new sort keys are sorting on,
        shown so the ordering is legible instead of mysterious."""
        lang = cfg.language(self._settings)
        plays = self._lib.plays(rom)
        if not plays:
            return cfg.tr(lang, "never_played")
        units = cfg.time_units(lang)
        bits = [cfg.tr(lang, "plays_n").format(n=plays)]
        played = lib.format_playtime(self._lib.playtime(rom), units)
        if played != "—":
            bits.append(played)
        last = lib.format_last(self._lib.last_played(rom), units)
        if last:
            bits.append(last)
        return " · ".join(bits)

    def _rebuild(self) -> None:
        """Lay the library out for the current view mode + size, reusing any
        thumbnails already in memory (switching views never re-renders)."""
        self._items.clear()
        host = QWidget(); host.setObjectName("page")
        view = cfg.library_view(self._settings)
        size = cfg.thumb_size(self._settings)
        lang = cfg.language(self._settings)
        # Two different empties: no ROMs at all (point me at a folder) vs. a search
        # or filter that matched nothing (your library is fine, the query is not).
        self._empty.setVisible(not self._roms)
        self._empty.setText(cfg.tr(lang, "no_roms" if not self._all_roms else "no_match"))
        fav_add, fav_rm = cfg.tr(lang, "fav_add"), cfg.tr(lang, "fav_remove")
        # Decided over the WHOLE library, not the filtered view, so a search or a
        # favourites filter never changes the name a card goes by.
        titles = _titles(self._all_roms)

        def decorate(rom: Path) -> tuple[str, bool, str]:
            fav = self._lib.is_favorite(rom)
            return self._subtitle(rom), fav, (fav_rm if fav else fav_add)

        if view == cfg.VIEW_GRID:
            grid = QGridLayout(host)
            grid.setContentsMargins(0, 0, 0, 0); grid.setSpacing(16)
            grid.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft)
            card_w = _art_size(size)[0] + 16
            self._grid_card_w = card_w
            cols = self._cols_for_width(self._scroll.viewport().width())
            cards = []
            for i, rom in enumerate(self._roms):
                card = GameCard(rom, size, *decorate(rom), title=titles.get(str(rom)))
                card.clicked.connect(self.play_requested.emit)
                card.fav_toggled.connect(self._on_fav)
                card.analyze_requested.connect(self.analyze_rom)
                card.cover_set_requested.connect(self.set_cover)
                card.cover_reset_requested.connect(self.reset_cover)
                self._items[str(rom)] = card
                cards.append(card)
                grid.addWidget(card, i // cols, i % cols)
            self._grid = grid; self._grid_cards = cards; self._grid_cols = cols
        else:
            self._grid = None; self._grid_cards = []; self._grid_cols = 0
            col = QVBoxLayout(host)
            col.setContentsMargins(0, 0, 0, 0); col.setSpacing(6)
            col.setAlignment(Qt.AlignmentFlag.AlignTop)
            show_art = (view == cfg.VIEW_LIST)
            row_size = min(size, 96)     # list covers are capped so rows stay tidy
            for rom in self._roms:
                row = GameRow(rom, row_size, show_art, *decorate(rom),
                              title=titles.get(str(rom)))
                row.clicked.connect(self.play_requested.emit)
                row.fav_toggled.connect(self._on_fav)
                row.analyze_requested.connect(self.analyze_rom)
                row.cover_set_requested.connect(self.set_cover)
                row.cover_reset_requested.connect(self.reset_cover)
                self._items[str(rom)] = row
                col.addWidget(row)

        # paint any thumbnails we already have
        for rom_str, img in self._images.items():
            item = self._items.get(rom_str)
            if item is not None:
                item.set_image(img)

        self._scroll.setWidget(host)
        self._host = host

    def _cols_for_width(self, w: int) -> int:
        card_w = getattr(self, "_grid_card_w", 0) or 120
        return max(1, (w or 900) // (card_w + 16))

    def _reflow_grid(self) -> None:
        # Grid view: re-flow the covers to fill the current width (only when the column
        # count actually changes, so a drag-resize is cheap and flicker-free).
        if cfg.library_view(self._settings) != cfg.VIEW_GRID or not getattr(self, "_grid", None):
            return
        cols = self._cols_for_width(self._scroll.viewport().width())
        if cols != self._grid_cols:
            self._grid_cols = cols
            for idx, card in enumerate(self._grid_cards):
                self._grid.addWidget(card, idx // cols, idx % cols)

    def resizeEvent(self, e) -> None:  # type: ignore[override]
        super().resizeEvent(e)
        self._reflow_grid()

    def _set_view(self, mode: str) -> None:
        self._settings.setValue("library/view", mode)
        self._sync_view_buttons()
        self._rebuild()

    def _sync_view_buttons(self) -> None:
        cur = cfg.library_view(self._settings)
        for mode, btn in self._view_btns.items():
            btn.setChecked(mode == cur)

    def _on_size(self, value: int) -> None:
        self._settings.setValue("library/thumb_size", value)
        self._rebuild()

    def _start_worker(self, roms: list[Path]) -> None:
        self._thread = QThread(self)
        self._worker = ThumbWorker(roms, self._bios(), console_is_mono(self._settings))
        self._worker.moveToThread(self._thread)
        self._thread.started.connect(self._worker.run)
        self._worker.ready.connect(self._on_thumb)
        self._worker.done.connect(self._thread.quit)
        self._thread.start()

    def _resume_missing_covers(self) -> None:
        """Render the covers that are MISSING, and only those.

        This is what makes a new ROM dropped in the folder cost one boot instead of a
        whole library: everything already on disk is served from the cache, and a game
        whose cover could not be rendered (no BIOS at the time, a game that never
        reached a real screen) is simply retried. Nothing is thrown away here."""
        if self._worker is not None or not self._all_roms:
            return
        todo = [r for r in self._all_roms
                if str(r) not in self._images
                and not (custom_cover(r) or _cover_path(r)).exists()]
        # ...plus the ones whose cover exists on disk but is not in memory yet: those
        # cost a file read, not a boot, and the card would otherwise stay blank.
        todo += [r for r in self._all_roms
                 if str(r) not in self._images and r not in todo]
        if todo:
            self._start_worker(todo)

    def regenerate_covers(self) -> None:
        """Throw the rendered covers away and make them again, for the whole library.

        The one place that costs a full re-render, and it is a button because it should
        be a DECISION: booting every game in a real collection is minutes of work. Covers
        the user chose (`covers/`) are untouched -- this only drops what the emulator
        itself rendered."""
        lang = cfg.language(self._settings)
        if not self._all_roms:
            return
        if QMessageBox.question(
                self, cfg.tr(lang, "covers_regen"),
                cfg.tr(lang, "covers_regen_ask").format(n=len(self._all_roms))
        ) != QMessageBox.StandardButton.Yes:
            return
        self._stop_worker()
        for rom in self._all_roms:
            cache = _cover_path(rom)
            try:
                if cache.is_file():
                    cache.unlink()
            except OSError:
                pass
        self._images.clear()
        self._rebuild()
        self._start_worker(self._all_roms)

    def _stop_worker(self) -> None:
        """Fully stop the thumbnail worker and JOIN it before returning. It renders
        each cover by booting the ROM in the native core, so it MUST NOT still be
        running when a game launches its own core -- two cores at once crashes. The
        stop is cooperative (checked between short frame batches), so the wait is
        brief; but it is unbounded on purpose -- abandoning the thread (the old 2 s
        timeout) is exactly what let a second core start on top of it."""
        if self._worker:
            self._worker.stop()
        if self._thread:
            self._thread.quit()
            self._thread.wait()
        self._thread = None
        self._worker = None

    def _on_thumb(self, rom_str: str, img: QImage) -> None:
        self._apply_cover(Path(rom_str), img)

    def _choose_folder(self) -> None:
        cur = cfg.rom_folder(self._settings) or str(DEFAULT_ROM_DIR)
        path = QFileDialog.getExistingDirectory(
            self, cfg.tr(cfg.language(self._settings), "rom_folder_pick"), cur)
        if path:
            self._settings.setValue("paths/rom_folder", path)
            self.reload()

    def _open_rom(self) -> None:
        cur = cfg.rom_folder(self._settings) or str(DEFAULT_ROM_DIR)
        lang = cfg.language(self._settings)
        path, _ = QFileDialog.getOpenFileName(
            self, cfg.tr(lang, "open_rom_pick"), cur,
            cfg.tr(lang, "filter_rom_files"))
        if path:
            path = choose_game_in_archive(self, path, lang)   # a collection -> which one?
        if path:
            self.play_requested.emit(path)

    def showEvent(self, e) -> None:  # type: ignore[override]
        # NOTE: this used to be declared TWICE in this class -- the second definition
        # silently replaced the first, so the re-flow below never ran and the grid kept
        # the column count it guessed before it had a real width. One handler now.
        super().showEvent(e)
        # A BIOS picked in Settings unblocks the covers: without one nothing could be
        # rendered, so re-run the whole pass on the way back here rather than making
        # the user restart. Gated on the path actually CHANGING -- this fires on every
        # return from Settings or from a game. `reload` re-arranges and re-starts the
        # worker itself, so there is nothing left for the rest of this handler to do.
        if self._bios() != self._rendered_with:
            # A BIOS appearing UNBLOCKS the covers that could not be rendered without
            # one. It does not invalidate the ones already on disk: the worker reuses
            # every cover it finds, so this pass costs one boot per MISSING cover and
            # nothing at all for a library that is already complete.
            self._rendered_with = self._bios()
            self._resume_missing_covers()
            QTimer.singleShot(0, self._reflow_grid)
            return
        # A game just ended: its play count / playtime / last-played changed, so the
        # cards are stale -- and under "Last played" so is the whole ordering.
        self._arrange()
        # At construction the scroll area has no real width yet, so the first layout
        # uses a fallback and may not fill the window. Re-flow now we are on screen.
        QTimer.singleShot(0, self._reflow_grid)
        # Resume rendering the covers we have not made yet -- including any ROM that
        # appeared in the folder since the last visit. Stopped on hide, so it never
        # shares the native core with a running game.
        self._resume_missing_covers()

    def hideEvent(self, e) -> None:  # type: ignore[override]
        self._stop_worker()
        super().hideEvent(e)


# ---------------------------------------------------------------- settings
def _row(label_widget: QWidget, control: QWidget) -> QFrame:
    f = QFrame()
    f.setObjectName("settingRow")
    h = QHBoxLayout(f)
    h.setContentsMargins(14, 10, 14, 10)
    h.addWidget(label_widget)
    h.addStretch()
    h.addWidget(control)
    return f


class SettingsPage(QWidget):
    changed = pyqtSignal()          # something the shell should re-apply
    language_changed = pyqtSignal()
    theme_changed = pyqtSignal()
    resume_requested = pyqtSignal()
    scale_changed = pyqtSignal(int)  # window-size preset -> resize the window now
    storage_changed = pyqtSignal()   # settings store swapped (portable <-> registry)

    def __init__(self, settings) -> None:
        super().__init__()
        self.setObjectName("page")
        self._settings = settings

        outer = QVBoxLayout(self)
        outer.setContentsMargins(20, 16, 20, 20); outer.setSpacing(12)
        self._resume_banner = QPushButton()
        self._resume_banner.setObjectName("primary")
        self._resume_banner.clicked.connect(self.resume_requested.emit)
        self._resume_banner.hide()
        outer.addWidget(self._resume_banner)

        root = QHBoxLayout()
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(18)
        outer.addLayout(root, 1)

        self._cats = QListWidget()
        self._cats.setObjectName("cats")
        self._cats.setFixedWidth(150)
        for key in ("cat_general", "cat_bios", "cat_graphics", "cat_audio",
                    "cat_controls", "cat_hotkeys"):
            QListWidgetItem(self._cats)
        self._cats.currentRowChanged.connect(lambda i: self._stack.setCurrentIndex(i))
        root.addWidget(self._cats)

        self._stack = QStackedWidget()
        self._stack.addWidget(self._general_panel())
        self._stack.addWidget(self._bios_panel())
        self._stack.addWidget(self._graphics_panel())
        self._stack.addWidget(self._audio_panel())
        self._stack.addWidget(self._controls_panel())
        self._stack.addWidget(self._hotkeys_panel())
        # SCROLLED, both ways. The tallest panel wants ~1000 px and the widest ~660;
        # a 1366x768 laptop (614 px of usable height at 125% scaling) has neither, and
        # without a scroll area the rows past the fold are simply unreachable -- there
        # is no way to click a button you cannot bring on screen.
        self._panel_scroll = QScrollArea()
        self._panel_scroll.setObjectName("page")
        self._panel_scroll.setWidgetResizable(True)
        self._panel_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self._panel_scroll.setWidget(self._stack)
        root.addWidget(self._panel_scroll, 1)

        self._cats.setCurrentRow(0)
        self.retranslate()

    _CATEGORY_ROW = {"bios": 1, "video": 2, "audio": 3, "controls": 4}

    def show_category(self, category: str) -> None:
        self._cats.setCurrentRow(self._CATEGORY_ROW.get(category, 0))

    def set_resume_visible(self, visible: bool) -> None:
        self._resume_banner.setVisible(visible)

    def _panel(self) -> tuple[QWidget, QVBoxLayout]:
        w = QWidget()
        w.setObjectName("page")
        v = QVBoxLayout(w)
        v.setContentsMargins(4, 4, 12, 4)
        v.setSpacing(10)
        v.setAlignment(Qt.AlignmentFlag.AlignTop)
        return w, v

    # -- General
    def _general_panel(self) -> QWidget:
        w, v = self._panel()
        self._lang = QComboBox()
        for code, name in cfg.LANGUAGES:
            self._lang.addItem(name, code)
        cur = cfg.language(self._settings)
        self._lang.setCurrentIndex([c for c, _ in cfg.LANGUAGES].index(cur))
        self._lang.currentIndexChanged.connect(self._on_lang)
        self._lbl_lang = QLabel()

        # Theme sits next to language because it is the same KIND of setting: it
        # changes how the app presents itself, not how it emulates.
        self._theme = QComboBox()
        for tid, _key in ngpc_theme.THEMES:
            self._theme.addItem("", tid)          # text filled by retranslate()
        cur_theme = cfg.theme(self._settings)
        self._theme.setCurrentIndex([t for t, _ in ngpc_theme.THEMES].index(cur_theme))
        self._theme.currentIndexChanged.connect(self._on_theme)
        self._lbl_theme = QLabel()

        self._shot_edit = QLineEdit(cfg.screenshot_dir(self._settings))
        self._shot_edit.setPlaceholderText(str(REPO / "screenshots"))
        self._shot_edit.editingFinished.connect(
            lambda: self._settings.setValue("paths/screenshots", self._shot_edit.text()))
        self._shot_browse = QPushButton(); self._shot_browse.setObjectName("ghost")
        self._shot_browse.clicked.connect(self._pick_shots)
        shotw = QWidget(); sh = QHBoxLayout(shotw); sh.setContentsMargins(0, 0, 0, 0)
        self._shot_edit.setFixedWidth(220); sh.addWidget(self._shot_edit); sh.addWidget(self._shot_browse)
        self._lbl_shots = QLabel()

        self._savemode = self._combo("general/save_mode", [
            (cfg.SAVE_ROM, "save_rom"), (cfg.SAVE_SIDECAR, "save_sidecar"),
            (cfg.SAVE_BOTH, "save_both")], cfg.save_mode(self._settings))
        self._lbl_savemode = QLabel()

        self._flashsize = self._combo("general/flash_size", [
            (cfg.FLASH_AUTO, "flash_auto"), (cfg.FLASH_4M, "flash_4m"),
            (cfg.FLASH_8M, "flash_8m"), (cfg.FLASH_16M, "flash_16m")],
            cfg.flash_size_setting(self._settings))
        self._lbl_flashsize = QLabel()

        self._rewind = self._combo("debug/rewind_seconds", [
            ("0", "rewind_off"), ("10", "rewind_10"),
            ("20", "rewind_20"), ("30", "rewind_30")],
            str(cfg.rewind_seconds(self._settings)))
        self._lbl_rewind = QLabel()

        self._cartwait = QCheckBox()
        self._cartwait.setChecked(cfg.cart_wait_states(self._settings))
        self._cartwait.toggled.connect(
            lambda b: (self._settings.setValue("general/cart_wait_states", b), self.changed.emit()))
        self._lbl_cartwait = QLabel()
        self._cartwait_hint = QLabel()
        self._cartwait_hint.setObjectName("hint")
        self._cartwait_hint.setWordWrap(True)

        # Storage location: a portable config.ini next to the app vs the OS-native
        # store (registry). Toggling migrates every setting across (cfg.set_portable)
        # and live-swaps the store for the whole session (storage_changed).
        self._portable = QCheckBox()
        self._portable.setChecked(cfg.portable_mode())
        self._portable.toggled.connect(self._on_portable_toggled)
        self._lbl_portable = QLabel()
        self._portable_hint = QLabel()
        self._portable_hint.setObjectName("hint")
        self._portable_hint.setWordWrap(True)

        self._rows_general = [
            _row(self._lbl_lang, self._lang),
            _row(self._lbl_theme, self._theme),
            _row(self._lbl_shots, shotw),
            _row(self._lbl_savemode, self._savemode),
            _row(self._lbl_flashsize, self._flashsize),
            _row(self._lbl_rewind, self._rewind),
            _row(self._lbl_cartwait, self._cartwait),
            _row(self._lbl_portable, self._portable),
        ]
        for r in self._rows_general:
            v.addWidget(r)
        v.addWidget(self._cartwait_hint)
        v.addWidget(self._portable_hint)
        return w

    def _on_portable_toggled(self, enabled: bool) -> None:
        """Migrate settings to the chosen store and switch to it for this session."""
        if enabled == cfg.portable_mode():
            return
        cfg.set_portable(enabled)
        self._settings = cfg.make_settings()   # this page now uses the new store
        self.storage_changed.emit()            # let the shell swap it everywhere
        self.changed.emit()

    # -- Console (BIOS): the machine itself -- its boot, its clock, its coin cell.
    # Grouped here because they are one subject: all three are the CONSOLE's state
    # rather than the emulator's, and two of them are literally the same battery.
    def _bios_panel(self) -> QWidget:
        w, v = self._panel()

        # THREE BIOS IMAGES, ONE SELECTOR. The colour NGPC dump and the mono NGP dump
        # are two different machines (the mono one stamps 0x6F91 = 0x00, the colour one
        # 0x10, which is how a cartridge knows where it is); the third is our own
        # clean-room HLE image, which needs no path because it ships with the emulator.
        # A radio in front of each says which one boots -- so "which BIOS am I running?"
        # is answered by looking, not by deducing it from three other settings.
        # NOTHING here ships a BIOS dump: both paths point at files the USER supplies.
        self._bios_group = QButtonGroup(self)
        self._bios_radios: dict[str, QRadioButton] = {}
        self._bios_edit = QLineEdit(cfg.bios_path(self._settings))
        self._bios_edit.editingFinished.connect(
            lambda: (self._settings.setValue("paths/bios", self._bios_edit.text()),
                     self._sync_bios_slots()))
        self._bios_browse = QPushButton(); self._bios_browse.setObjectName("ghost")
        self._bios_browse.clicked.connect(self._pick_bios)
        self._bios_mono_edit = QLineEdit(cfg.bios_mono_path(self._settings))
        self._bios_mono_edit.editingFinished.connect(
            lambda: (self._settings.setValue("paths/bios_mono", self._bios_mono_edit.text()),
                     self._sync_bios_slots()))
        self._bios_mono_browse = QPushButton(); self._bios_mono_browse.setObjectName("ghost")
        self._bios_mono_browse.clicked.connect(self._pick_bios_mono)

        def slot(choice: str, edit: QLineEdit | None, browse: QPushButton | None) -> QWidget:
            row = QWidget(); h = QHBoxLayout(row); h.setContentsMargins(0, 0, 0, 0)
            radio = QRadioButton()
            radio.setChecked(cfg.bios_choice(self._settings) == choice)
            radio.toggled.connect(
                lambda on, c=choice: self._on_bios_choice(c) if on else None)
            self._bios_group.addButton(radio)
            self._bios_radios[choice] = radio
            radio.setMinimumWidth(210)
            h.addWidget(radio)
            if edit is not None:
                edit.setFixedWidth(220)
                h.addWidget(edit); h.addWidget(browse)
            h.addStretch()
            return row

        self._lbl_bios = QLabel()
        bios_rows = [slot(cfg.BIOS_USE_COLOUR, self._bios_edit, self._bios_browse),
                     slot(cfg.BIOS_USE_MONO, self._bios_mono_edit, self._bios_mono_browse),
                     slot(cfg.BIOS_USE_HLE, None, None)]
        self._bios_hint2 = QLabel(); self._bios_hint2.setObjectName("hint")
        self._bios_hint2.setWordWrap(True)

        self._realbios = QCheckBox()
        self._realbios.setChecked(cfg.real_bios(self._settings))
        self._realbios.toggled.connect(
            lambda b: (self._settings.setValue("general/real_bios", b), self.changed.emit()))
        self._lbl_realbios = QLabel()
        self._realbios_hint = QLabel()
        self._realbios_hint.setObjectName("hint")
        self._realbios_hint.setWordWrap(True)

        self._clock_items = [
            (ns.CLOCK_HARDWARE, "clk_hardware"), (ns.CLOCK_HOST, "clk_host"),
            (ns.CLOCK_PAUSED, "clk_paused"), (ns.CLOCK_MANUAL, "clk_manual")]
        self._clockmode = self._combo("bios/clock_mode", self._clock_items,
                                      cfg.clock_mode(self._settings))
        self._lbl_clockmode = QLabel()
        # The date/time MANUAL mode uses. Shown only for that mode: a field that does
        # nothing in the other three is a question the panel should not be asking.
        self._clockset = QDateTimeEdit()
        self._clockset.setDisplayFormat("yyyy-MM-dd  HH:mm:ss")
        self._clockset.setCalendarPopup(True)
        self._clockset.setDateTime(QDateTime.fromString(
            cfg.clock_manual(self._settings), Qt.DateFormat.ISODate))
        self._clockset.dateTimeChanged.connect(
            lambda dt: (self._settings.setValue(
                "bios/clock_manual", dt.toString(Qt.DateFormat.ISODate)),
                self.changed.emit()))
        self._lbl_clockset = QLabel()
        self._clockmode.currentIndexChanged.connect(lambda _i: self._sync_clock_rows())
        self._clockmode_hint = QLabel()
        self._clockmode_hint.setObjectName("hint")
        self._clockmode_hint.setWordWrap(True)

        # THE CONSOLE'S LANGUAGE -- not the emulator's. A bilingual cartridge reads
        # 0x6F87 to pick its script (SDK SysWork.txt), and on hardware the BIOS setup
        # wizard is what sets it. We skip that wizard, so the choice belongs here.
        self._cartlang_items = [(cfg.CART_LANG_EN, "cart_lang_en"),
                                (cfg.CART_LANG_JA, "cart_lang_ja")]
        self._cartlang = self._combo("general/cart_language", self._cartlang_items,
                                     cfg.cart_language(self._settings))
        self._lbl_cartlang = QLabel()
        self._cartlang_hint = QLabel()
        self._cartlang_hint.setObjectName("hint")
        self._cartlang_hint.setWordWrap(True)

        # Pulling the coin cell. Destructive and not undoable, so it asks first.
        self._coincell_btn = QPushButton(); self._coincell_btn.setObjectName("ghost")
        self._coincell_btn.clicked.connect(self._reset_coin_cell)
        self._lbl_coincell = QLabel()
        self._coincell_hint = QLabel()
        self._coincell_hint.setObjectName("hint")
        self._coincell_hint.setWordWrap(True)

        v.addWidget(self._lbl_bios)
        for r in bios_rows:
            v.addWidget(r)
        v.addWidget(self._bios_hint2)
        v.addWidget(_row(self._lbl_realbios, self._realbios))
        v.addWidget(self._realbios_hint)
        v.addWidget(_row(self._lbl_clockmode, self._clockmode))
        self._clockset_row = _row(self._lbl_clockset, self._clockset)
        v.addWidget(self._clockset_row)
        v.addWidget(self._clockmode_hint)
        v.addWidget(_row(self._lbl_cartlang, self._cartlang))
        v.addWidget(self._cartlang_hint)
        v.addWidget(_row(self._lbl_coincell, self._coincell_btn))
        v.addWidget(self._coincell_hint)
        return w

    def _reset_coin_cell(self) -> None:
        """Pull the console's battery: forget the BIOS settings AND the clock.

        The two files are one battery on hardware, so they go together -- clearing only
        the settings would leave a console that runs first-boot setup while still
        insisting it is a date it cannot have remembered. Cartridge saves live in the
        ROM / .flash files and are deliberately untouched.
        """
        lang = cfg.language(self._settings)
        t = lambda k: cfg.tr(lang, k)  # noqa: E731
        files = [p for p in (_SYSTEM_RAM, _SYSTEM_RTC) if p.exists()]
        if not files:
            QMessageBox.information(self, t("coin_cell_confirm_title"), t("coin_cell_empty"))
            return
        if QMessageBox.question(
                self, t("coin_cell_confirm_title"), t("coin_cell_confirm"),
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No) != QMessageBox.StandardButton.Yes:
            return
        for p in files:
            try:
                p.unlink()
            except OSError:
                pass
        QMessageBox.information(self, t("coin_cell_confirm_title"), t("coin_cell_done"))

    # -- Graphics
    def _combo(self, key: str, items: list[tuple[str, str]], current: str):
        """A combo bound to a settings key. items = [(id, label_key)]."""
        c = QComboBox()
        # Don't let a long option ("Follow the PC's clock…") stretch the combo -- and the
        # whole settings column -- to its own width. Cap it; the dropdown still shows the
        # full text, and the current line elides if the window is narrow.
        c.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        c.setMinimumContentsLength(12)
        c.setMaximumWidth(240)
        for value, _labelkey in items:
            c.addItem("", value)
        idx = [v for v, _ in items].index(current) if current in [v for v, _ in items] else 0
        c.setCurrentIndex(idx)
        c.currentIndexChanged.connect(
            lambda _i, cc=c, kk=key: (self._settings.setValue(kk, cc.currentData()),
                                      self.changed.emit()))
        return c

    def _graphics_panel(self) -> QWidget:
        import ngpc_video as vid
        w, v = self._panel()
        self._scale = QSpinBox(); self._scale.setRange(1, 8)
        self._scale.setValue(cfg.lcd_scale(self._settings))
        # A window-size preset: resize the window to this many x now. The canvas still
        # follows a later manual resize -- this is a shortcut, not a hard lock.
        self._scale.valueChanged.connect(
            lambda n: (self._settings.setValue("gfx/lcd_scale", n), self.scale_changed.emit(n)))

        # GRAPHICS FILTER PLUGINS. The list is built-ins + whatever the plugin folder
        # holds; see ngpc_filters.py for why the good scalers are loaded, not shipped.
        self._filter_items = [
            (vid.FILTER_NONE, "flt_none"), (vid.FILTER_SCANLINES, "flt_scanlines"),
            (vid.FILTER_LCD_GRID, "flt_lcdgrid"), (vid.FILTER_CRT, "flt_crt")]
        self._filter = self._combo("gfx/filter", self._filter_items,
                                   cfg.video_filter(self._settings))
        self._plug_edit = QLineEdit(cfg.filters_dir(self._settings))
        self._plug_edit.setFixedWidth(220)
        self._plug_edit.editingFinished.connect(
            lambda: self._set_filters_dir(self._plug_edit.text()))
        self._plug_browse = QPushButton(); self._plug_browse.setObjectName("ghost")
        self._plug_browse.clicked.connect(self._pick_filters_dir)
        plugw = QWidget(); ph = QHBoxLayout(plugw); ph.setContentsMargins(0, 0, 0, 0)
        ph.addWidget(self._plug_edit); ph.addWidget(self._plug_browse)
        self._lbl_plugins = QLabel()
        self._plug_hint = QLabel(); self._plug_hint.setObjectName("hint")
        self._plug_hint.setWordWrap(True)
        self._plugw = plugw
        self._color_items = [
            (vid.COLOR_RAW, "col_raw"), (vid.COLOR_LCD, "col_lcd"),
            (vid.COLOR_VIVID, "col_vivid")]
        self._colorbox = self._combo("gfx/color", self._color_items,
                                     cfg.color_profile(self._settings))
        self._aspect_items = [
            (vid.ASPECT_PIXEL, "asp_pixel"), (vid.ASPECT_FIT, "asp_fit"),
            (vid.ASPECT_STRETCH, "asp_stretch")]
        self._aspectbox = self._combo("gfx/aspect", self._aspect_items,
                                      cfg.aspect_mode(self._settings))

        self._mono_items = [(cfg.MONO_K2GE, "mono_k2ge"), (cfg.MONO_K1GE, "mono_k1ge")]
        self._monobox = self._combo("gfx/mono_mode", self._mono_items,
                                    cfg.mono_mode(self._settings))
        self._mono_hint = QLabel(); self._mono_hint.setObjectName("hint")
        self._mono_hint.setWordWrap(True)

        self._smooth = QCheckBox(); self._smooth.setChecked(cfg.smoothing(self._settings))
        self._smooth.toggled.connect(
            lambda b: (self._settings.setValue("gfx/smoothing", b), self.changed.emit()))
        self._fs = QCheckBox(); self._fs.setChecked(cfg.fullscreen(self._settings))
        self._fs.toggled.connect(
            lambda b: (self._settings.setValue("gfx/fullscreen", b), self.changed.emit()))
        self._fshide = QCheckBox(); self._fshide.setChecked(cfg.fs_hide_ui(self._settings))
        self._fshide.toggled.connect(
            lambda b: (self._settings.setValue("gfx/fs_hide_ui", b), self.changed.emit()))
        self._tbautohide = QCheckBox(); self._tbautohide.setChecked(cfg.toolbar_autohide(self._settings))
        self._tbautohide.toggled.connect(
            lambda b: (self._settings.setValue("gfx/toolbar_autohide", b), self.changed.emit()))
        self._showfps = QCheckBox(); self._showfps.setChecked(cfg.show_fps(self._settings))
        self._showfps.toggled.connect(
            lambda b: (self._settings.setValue("gfx/show_fps", b), self.changed.emit()))

        self._lbl_scale = QLabel(); self._lbl_filter = QLabel(); self._lbl_color = QLabel()
        self._lbl_aspect = QLabel(); self._lbl_smooth = QLabel(); self._lbl_fs = QLabel()
        self._lbl_showfps = QLabel(); self._lbl_mono = QLabel(); self._lbl_fshide = QLabel()
        self._lbl_tbautohide = QLabel()
        for r in (_row(self._lbl_scale, self._scale),
                  _row(self._lbl_mono, self._monobox),
                  _row(self._lbl_filter, self._filter),
                  _row(self._lbl_plugins, self._plugw),
                  _row(self._lbl_color, self._colorbox),
                  _row(self._lbl_aspect, self._aspectbox),
                  _row(self._lbl_smooth, self._smooth),
                  _row(self._lbl_showfps, self._showfps),
                  _row(self._lbl_fs, self._fs),
                  _row(self._lbl_fshide, self._fshide),
                  _row(self._lbl_tbautohide, self._tbautohide)):
            v.addWidget(r)
        v.addWidget(self._plug_hint)
        v.addWidget(self._mono_hint)
        return w

    # -- Audio
    def _audio_panel(self) -> QWidget:
        w, v = self._panel()
        self._aon = QCheckBox(); self._aon.setChecked(cfg.audio_enabled(self._settings))
        self._aon.toggled.connect(
            lambda b: (self._settings.setValue("audio/enabled", b), self.changed.emit()))
        self._vol = QSlider(Qt.Orientation.Horizontal); self._vol.setFixedWidth(200)
        self._vol.setRange(0, 100); self._vol.setValue(cfg.audio_volume(self._settings))
        self._vol.valueChanged.connect(
            lambda n: (self._settings.setValue("audio/volume", n), self.changed.emit()))
        self._lbl_aon = QLabel(); self._lbl_vol = QLabel()
        for r in (_row(self._lbl_aon, self._aon), _row(self._lbl_vol, self._vol)):
            v.addWidget(r)
        return w

    # -- Controls
    def _controls_panel(self) -> QWidget:
        w, v = self._panel()
        self._ctrl_hint = QLabel(); self._ctrl_hint.setObjectName("hint")
        self._ctrl_hint.setWordWrap(True)
        v.addWidget(self._ctrl_hint)

        # Player selector: the same map edits player 1 or player 2 (for two-player
        # link play). P1 keeps the legacy bindings; P2 has its own set + its own
        # gamepad. Defaults to P1 so the common case is unchanged.
        self._ctrl_player = 1
        self._player_sel = QComboBox()
        self._player_sel.addItem("", 1)      # labelled in retranslate: "Player N"
        self._player_sel.addItem("", 2)
        self._player_sel.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._player_sel.currentIndexChanged.connect(self._on_ctrl_player)
        self._lbl_player = QLabel()
        v.addWidget(_row(self._lbl_player, self._player_sel))

        # The bindings live on a picture of the console rather than in a list: the
        # field for "Left" sits next to the actual left of the d-pad. Same widgets,
        # same settings keys -- only the arrangement carries the meaning now.
        self._bindmap = ngpc_bindmap.BindMap(self._settings, self._ctrl_player)
        self._bindmap.changed.connect(self._refresh_conflicts)
        self._bindmap.changed.connect(self.changed.emit)
        self._keybtns = self._bindmap.buttons     # `_restore_keys` still drives these
        v.addWidget(self._bindmap, 1)

        # In game the hotkeys are matched BEFORE the joypad bindings, so a button
        # bound to Esc/Tab/P/F5... is simply dead and nothing says why. Say why.
        self._conflict = QLabel(); self._conflict.setObjectName("hint")
        self._conflict.setWordWrap(True)
        self._conflict.setStyleSheet(f"color:{PALETTE.warning};")
        v.addWidget(self._conflict)

        self._restore = QPushButton(); self._restore.setObjectName("ghost")
        self._restore.clicked.connect(self._restore_keys)
        v.addWidget(self._restore)

        # -- turbo (autofire)
        self._turbo_hint = QLabel(); self._turbo_hint.setObjectName("hint")
        self._turbo_hint.setWordWrap(True)
        v.addWidget(self._turbo_hint)
        self._turbo_boxes: dict[str, QCheckBox] = {}
        self._turbo_labels: dict[str, QLabel] = {}
        for label in cfg.TURBO_BUTTONS:
            box = QCheckBox(); box.setChecked(cfg.turbo_on(self._settings, label))

            def on_turbo(state: bool, lbl=label) -> None:
                cfg.set_turbo(self._settings, lbl, state)
                self.changed.emit()
            box.toggled.connect(on_turbo)
            lab = QLabel()
            self._turbo_boxes[label] = box
            self._turbo_labels[label] = lab
            v.addWidget(_row(lab, box))
        self._turbo_rate = QComboBox()
        for hz in cfg.TURBO_RATES:
            self._turbo_rate.addItem("", hz)
        idx = self._turbo_rate.findData(cfg.turbo_hz(self._settings))
        self._turbo_rate.setCurrentIndex(max(0, idx))
        self._turbo_rate.currentIndexChanged.connect(
            lambda _i: (self._settings.setValue("input/turbo_hz", self._turbo_rate.currentData()),
                        self.changed.emit()))
        self._lbl_turbo_rate = QLabel()
        v.addWidget(_row(self._lbl_turbo_rate, self._turbo_rate))

        # -- controller
        self._pad_hint = QLabel(); self._pad_hint.setObjectName("hint")
        self._pad_hint.setWordWrap(True)
        v.addWidget(self._pad_hint)
        self._pad_box = QCheckBox(); self._pad_box.setChecked(cfg.gamepad_enabled(self._settings))
        self._pad_box.toggled.connect(
            lambda b: (self._settings.setValue("input/gamepad", b), self.changed.emit()))
        self._lbl_pad = QLabel()
        v.addWidget(_row(self._lbl_pad, self._pad_box))
        # Which XInput controller drives the SELECTED player (1-4). P1=1, P2=2 by
        # default, so two players can each hold their own pad.
        self._pad_idx_spin = QSpinBox(); self._pad_idx_spin.setRange(1, 4)
        self._pad_idx_spin.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._pad_idx_spin.setValue(cfg.player_pad_index(self._settings, self._ctrl_player) + 1)
        self._pad_idx_spin.valueChanged.connect(
            lambda v: (cfg.set_player_pad_index(self._settings, self._ctrl_player, v - 1),
                       self.changed.emit()))
        self._lbl_pad_idx = QLabel()
        v.addWidget(_row(self._lbl_pad_idx, self._pad_idx_spin))
        self._pad_state = QLabel(); self._pad_state.setObjectName("hint")
        v.addWidget(self._pad_state)
        # A pad can be plugged in while this page is open, so the readout polls.
        self._pad_probe = ngpc_input.make_pad(0)
        self._pad_timer = QTimer(self)
        self._pad_timer.timeout.connect(self._refresh_pad_state)
        self._pad_timer.start(1000)
        return w

    def _refresh_conflicts(self) -> None:
        """Report both ways a binding can be ambiguous: a joypad button shadowed by
        a hotkey (the button goes dead), and two hotkeys on one key (one never
        fires). Reads the SETTINGS, so it is right whichever panel made the mess."""
        lang = cfg.language(self._settings)
        pad_clashes, dupes = cfg.conflicts(self._settings, lang)
        pad_text = (cfg.tr(lang, "key_conflict").format(hk="; ".join(pad_clashes))
                    if pad_clashes else "")
        for label in (self._conflict, self._hk_conflict):
            label.setText(pad_text)
            label.setVisible(bool(pad_clashes))
        self._hk_dupe.setText(
            cfg.tr(lang, "hk_dupe").format(hk="; ".join(dupes)) if dupes else "")
        self._hk_dupe.setVisible(bool(dupes))

    def _refresh_pad_state(self) -> None:
        lang = cfg.language(self._settings)
        if not self._pad_probe.available:
            key = "pad_none"
        else:
            # Reading the mask is what updates `connected`; the value is discarded.
            self._pad_probe.poll()
            key = "pad_on" if self._pad_probe.connected else "pad_off"
        self._pad_state.setText(cfg.tr(lang, key))

    # -- Hotkeys (rebindable; this panel used to be a read-only cheat sheet)
    def _hotkeys_panel(self) -> QWidget:
        w, v = self._panel()
        self._hk_intro = QLabel(); self._hk_intro.setObjectName("hint")
        self._hk_intro.setWordWrap(True)
        v.addWidget(self._hk_intro)
        self._hkbtns: dict[str, cfg.KeyCaptureButton] = {}
        self._hk_labels: list[tuple[QLabel, str]] = []
        for action, _default, name_key in cfg.HOTKEYS:
            btn = cfg.KeyCaptureButton(cfg.hotkey_code(self._settings, action))
            btn.setObjectName("ghost"); btn.setFixedWidth(140)

            # Same contract as the joypad buttons: persist on `captured`, which
            # fires AFTER the new code is stored (see KeyCaptureButton).
            def persist(new_code: int, act=action) -> None:
                cfg.set_hotkey(self._settings, act, int(new_code))
                self._settings.sync()
                self._refresh_conflicts()
                self.changed.emit()
            btn.captured.connect(persist)
            self._hkbtns[action] = btn
            lab = QLabel()
            self._hk_labels.append((lab, name_key))
            v.addWidget(_row(lab, btn))

        self._hk_dupe = QLabel(); self._hk_dupe.setObjectName("hint")
        self._hk_dupe.setWordWrap(True)
        self._hk_dupe.setStyleSheet(f"color:{PALETTE.warning};")
        v.addWidget(self._hk_dupe)
        # The joypad-vs-hotkey clash is shown in BOTH panels: you can create it
        # from either side, and you should not have to guess which one to open.
        self._hk_conflict = QLabel(); self._hk_conflict.setObjectName("hint")
        self._hk_conflict.setWordWrap(True)
        self._hk_conflict.setStyleSheet(f"color:{PALETTE.warning};")
        v.addWidget(self._hk_conflict)

        self._hk_restore = QPushButton(); self._hk_restore.setObjectName("ghost")
        self._hk_restore.clicked.connect(self._restore_hotkeys)
        v.addWidget(self._hk_restore)
        return w

    def _restore_hotkeys(self) -> None:
        for action, btn in self._hkbtns.items():
            code = cfg.DEFAULT_HOTKEYS.get(action, 0)
            btn._key = code           # noqa: SLF001
            btn._render()             # noqa: SLF001
            cfg.set_hotkey(self._settings, action, code)
        self._settings.sync()
        self._refresh_conflicts()
        self.changed.emit()

    def _on_ctrl_player(self, _idx: int) -> None:
        """Switch the bindmap + pad-index field to the chosen player's layout."""
        self._ctrl_player = int(self._player_sel.currentData() or 1)
        self._bindmap.set_player(self._ctrl_player)
        self._pad_idx_spin.blockSignals(True)
        self._pad_idx_spin.setValue(
            cfg.player_pad_index(self._settings, self._ctrl_player) + 1)
        self._pad_idx_spin.blockSignals(False)

    def _restore_keys(self) -> None:
        defaults = cfg._default_keys(self._ctrl_player)
        for label, btn in self._keybtns.items():
            code = defaults.get(label, 0)
            btn._key = code           # noqa: SLF001
            btn._render()             # noqa: SLF001
            cfg.set_binding(self._settings, label, code, self._ctrl_player)
        self._settings.sync()
        self._refresh_conflicts()
        self.changed.emit()

    def _pick_bios(self) -> None:
        cur = cfg.bios_path(self._settings) or str(DEFAULT_ROM_DIR)
        lang = cfg.language(self._settings)
        path, _ = QFileDialog.getOpenFileName(self, cfg.tr(lang, "bios_slot_colour"), cur,
                                              cfg.tr(lang, "filter_bios_files"))
        if path:
            self._bios_edit.setText(path)
            self._settings.setValue("paths/bios", path)
            self._sync_bios_slots()

    def _pick_bios_mono(self) -> None:
        cur = cfg.bios_mono_path(self._settings) or str(REPO)
        lang = cfg.language(self._settings)
        path, _ = QFileDialog.getOpenFileName(
            self, cfg.tr(lang, "bios_mono"), cur,
            "BIOS (*.bin *.rom *.ngp *.ngc);;All (*)")
        if not path:
            return
        self._bios_mono_edit.setText(path)
        self._settings.setValue("paths/bios_mono", path)
        self._sync_bios_slots()

    def _on_bios_choice(self, choice: str) -> None:
        """Pick the BIOS -- and, when that choice names a machine, pick the machine too.

        The mono NGP BIOS only exists on mono silicon and the colour one only on an
        NGPC, so leaving the Console setting behind is how a user ends up watching the
        NGP boot intro and then playing in colour. The HLE slot names no machine: it
        leaves the Console setting alone."""
        self._settings.setValue("bios/active", choice)
        implied = {cfg.BIOS_USE_MONO: cfg.MONO_K1GE,
                   cfg.BIOS_USE_COLOUR: cfg.MONO_K2GE}.get(choice)
        if implied is not None and cfg.mono_mode(self._settings) != implied:
            self._settings.setValue("gfx/mono_mode", implied)
            box = getattr(self, "_monobox", None)
            if box is not None:
                idx = box.findData(implied)
                if idx >= 0:
                    box.setCurrentIndex(idx)
        self._sync_bios_slots()
        self.changed.emit()

    def _sync_bios_slots(self) -> None:
        """Say, under the three slots, WHICH image will actually boot and what it is.

        The check is the same one a cartridge makes: an NGPC BIOS stamps 0x6F91 = 0x10,
        the mono NGP's stamps 0x00. So a colour dump dropped in the mono slot is caught
        here rather than three settings later, when a game 'mysteriously' still thinks
        it is on an NGPC."""
        lang = cfg.language(self._settings)
        found, slot = resolve_selected_bios(self._settings)
        choice = cfg.bios_choice(self._settings)
        if found is None:
            self._bios_hint2.setText(cfg.tr(lang, "bios_none"))
            return
        if slot != choice:
            # The selected slot was empty; something else answered for it.
            self._bios_hint2.setText(
                cfg.tr(lang, "bios_fallback").format(name=found.name))
            return
        if choice == cfg.BIOS_USE_HLE:
            self._bios_hint2.setText(cfg.tr(lang, "bios_hle_hint"))
            return
        kind = bios_kind(found)
        want = BIOS_MONO if choice == cfg.BIOS_USE_MONO else BIOS_COLOUR
        if kind == BIOS_UNKNOWN:
            key = "bios_slot_unknown"
        elif kind == want:
            key = "bios_slot_ok_mono" if want == BIOS_MONO else "bios_slot_ok_colour"
        else:
            key = "bios_slot_wrong_mono" if want == BIOS_MONO else "bios_slot_wrong_colour"
        self._bios_hint2.setText(cfg.tr(lang, key).format(name=found.name))

    def _set_filters_dir(self, path: str) -> None:
        self._settings.setValue("paths/filters", path)
        FILTERS.sync(path, force=True)
        self._sync_filter_items()
        self.changed.emit()

    def _sync_clock_rows(self) -> None:
        """The manual date/time only exists for the manual mode."""
        self._clockset_row.setVisible(
            cfg.clock_mode(self._settings) == ns.CLOCK_MANUAL)

    def _pick_filters_dir(self) -> None:
        lang = cfg.language(self._settings)
        cur = cfg.filters_dir(self._settings) or str(REPO)
        path = QFileDialog.getExistingDirectory(self, cfg.tr(lang, "filter_plugins"), cur)
        if path:
            self._plug_edit.setText(path)
            self._set_filters_dir(path)

    def _sync_filter_items(self) -> None:
        """Rebuild the filter list (built-ins + plugins) and say what the folder holds.

        The rejected files are listed too, with their reason. A plugin that simply does
        not appear is a bug report we would receive as "your filter list is broken"."""
        lang = cfg.language(self._settings)
        current = cfg.video_filter(self._settings)
        self._filter.blockSignals(True)
        self._filter.clear()
        for ident, key in self._filter_items:
            self._filter.addItem(cfg.tr(lang, key), ident)
        for plug in FILTERS.plugins:
            self._filter.addItem(plug.name, plug.ident)
        idx = self._filter.findData(current)
        self._filter.setCurrentIndex(idx if idx >= 0 else 0)
        self._filter.blockSignals(False)
        if idx < 0 and current.startswith("plugin:"):
            # The selected plugin is gone (folder moved, file deleted): fall back rather
            # than leave a setting pointing at nothing.
            self._settings.setValue("gfx/filter", vid.FILTER_NONE)

        if not cfg.filters_dir(self._settings):
            self._plug_hint.setText(cfg.tr(lang, "filter_plugins_hint"))
            return
        bits = []
        if FILTERS.plugins:
            bits.append(cfg.tr(lang, "filter_plugins_found").format(
                n=len(FILTERS.plugins),
                names=", ".join(p.name for p in FILTERS.plugins)))
        else:
            bits.append(cfg.tr(lang, "filter_plugins_none"))
        for bad in FILTERS.rejected:
            bits.append(cfg.tr(lang, "filter_plugins_bad").format(
                name=bad.source.name, why=bad.reason))
        self._plug_hint.setText("\n".join(bits))

    def _pick_shots(self) -> None:
        cur = cfg.screenshot_dir(self._settings) or str(REPO / "screenshots")
        path = QFileDialog.getExistingDirectory(
            self, cfg.tr(cfg.language(self._settings), "screenshots"), cur)
        if path:
            self._shot_edit.setText(path)
            self._settings.setValue("paths/screenshots", path)

    def _on_lang(self, _idx: int) -> None:
        self._settings.setValue("general/language", self._lang.currentData())
        self.retranslate()
        self.language_changed.emit()

    def _on_theme(self, _idx: int) -> None:
        self._settings.setValue("general/theme", self._theme.currentData())
        self.theme_changed.emit()       # the Shell repaints everything, us included

    def restyle(self) -> None:
        """Re-apply the inline colours the global stylesheet cannot reach.

        Warning labels carry their own sheet (a stylesheet rule cannot express
        "amber only when this text means trouble"), so a theme change has to
        come back and rewrite them by hand."""
        warn = f"color:{PALETTE.warning};"
        for lab in (self._conflict, self._hk_dupe, self._hk_conflict):
            if lab.styleSheet():        # only the ones currently showing a warning
                lab.setStyleSheet(warn)
        self._bindmap.update()          # leader lines are painted, not styled

    def retranslate(self) -> None:
        lang = cfg.language(self._settings)
        t = lambda k: cfg.tr(lang, k)
        for i, key in enumerate(("cat_general", "cat_bios", "cat_graphics", "cat_audio",
                                 "cat_controls", "cat_hotkeys")):
            self._cats.item(i).setText(t(key))
        self._resume_banner.setText("▶  " + t("m_resume"))
        self._hk_intro.setText(t("hotkeys_hint"))
        self._hk_restore.setText(t("restore"))
        for lab, key in self._hk_labels:
            lab.setText(t(key))
        for b in self._hkbtns.values():
            b._render()  # noqa: SLF001
        self._lbl_lang.setText(t("language")); self._lbl_bios.setText(t("bios"))
        self._lbl_theme.setText(t("theme"))
        for i, (_tid, key) in enumerate(ngpc_theme.THEMES):
            self._theme.setItemText(i, t(key))
        self._lbl_realbios.setText(t("console_boot")); self._bios_browse.setText(t("browse"))
        self._bios_mono_browse.setText(t("browse"))
        for choice, key in ((cfg.BIOS_USE_COLOUR, "bios_slot_colour"),
                            (cfg.BIOS_USE_MONO, "bios_slot_mono"),
                            (cfg.BIOS_USE_HLE, "bios_slot_hle")):
            self._bios_radios[choice].setText(t(key))
        self._sync_bios_slots()
        self._lbl_shots.setText(t("screenshots")); self._shot_browse.setText(t("browse"))
        self._lbl_savemode.setText(t("save_mode"))
        for i, (_id, key) in enumerate([(cfg.SAVE_ROM, "save_rom"),
                (cfg.SAVE_SIDECAR, "save_sidecar"), (cfg.SAVE_BOTH, "save_both")]):
            self._savemode.setItemText(i, t(key))
        self._lbl_flashsize.setText(t("flash_size"))
        for i, key in enumerate(["flash_auto", "flash_4m", "flash_8m", "flash_16m"]):
            self._flashsize.setItemText(i, t(key))
        self._lbl_rewind.setText(t("rewind"))
        for i, key in enumerate(["rewind_off", "rewind_10", "rewind_20", "rewind_30"]):
            self._rewind.setItemText(i, t(key))
        self._realbios_hint.setText(t("console_boot_hint"))
        self._lbl_clockmode.setText(t("clock_mode"))
        self._lbl_clockset.setText(t("clock_manual"))
        if cfg.clock_mode(self._settings) == ns.CLOCK_MANUAL:
            self._clockmode_hint.setText(t("clock_manual_hint"))
        self._sync_clock_rows()
        for i, (_val, key) in enumerate(self._clock_items):
            self._clockmode.setItemText(i, t(key))
        self._clockmode_hint.setText(t("clock_mode_hint"))
        self._lbl_cartlang.setText(t("cart_lang"))
        for i, (_val, key) in enumerate(self._cartlang_items):
            self._cartlang.setItemText(i, t(key))
        self._cartlang_hint.setText(t("cart_lang_hint"))
        self._lbl_coincell.setText(t("coin_cell"))
        self._coincell_btn.setText(t("coin_cell_reset"))
        self._coincell_hint.setText(t("coin_cell_hint"))
        self._lbl_cartwait.setText(t("cart_wait")); self._cartwait_hint.setText(t("cart_wait_hint"))
        self._lbl_portable.setText(t("portable")); self._portable_hint.setText(t("portable_hint"))
        self._lbl_scale.setText(t("lcd_scale")); self._lbl_smooth.setText(t("smoothing"))
        self._lbl_filter.setText(t("filter")); self._lbl_color.setText(t("color_profile"))
        self._lbl_aspect.setText(t("aspect")); self._lbl_fs.setText(t("fullscreen"))
        self._lbl_fshide.setText(t("fs_hide_ui"))
        self._lbl_tbautohide.setText(t("toolbar_autohide"))
        self._lbl_showfps.setText(t("show_fps"))
        self._lbl_mono.setText(t("mono_mode")); self._mono_hint.setText(t("mono_mode_hint"))
        self._lbl_plugins.setText(t("filter_plugins"))
        self._plug_browse.setText(t("browse"))
        self._sync_filter_items()
        for box, items in ((self._colorbox, self._color_items),
                           (self._monobox, self._mono_items),
                           (self._aspectbox, self._aspect_items)):
            for i, (_val, key) in enumerate(items):
                box.setItemText(i, t(key))
        self._lbl_aon.setText(t("audio_on")); self._lbl_vol.setText(t("volume"))
        self._ctrl_hint.setText(t("controls_hint")); self._restore.setText(t("restore"))
        self._bindmap.retranslate()
        # turbo + controller
        self._turbo_hint.setText(t("turbo_hint"))
        for label, lab in self._turbo_labels.items():
            lab.setText(t("turbo").format(btn=label))
        self._lbl_turbo_rate.setText(t("turbo_rate"))
        for i, hz in enumerate(cfg.TURBO_RATES):
            self._turbo_rate.setItemText(i, t("turbo_hz").format(n=hz))
        self._pad_hint.setText(t("gamepad_hint")); self._lbl_pad.setText(t("gamepad"))
        # These two used to be a hardcoded `"Joueur" if french else "Player"` in the
        # code -- a translation no translator could ever reach, invisible to the lang
        # files, and wrong in every other language. Same route as everything else now.
        self._lbl_player.setText(t("player"))
        self._lbl_pad_idx.setText(t("pad_index"))
        for i in range(self._player_sel.count()):
            self._player_sel.setItemText(i, t("player_n").format(n=i + 1))
        self._refresh_pad_state()
        self._refresh_conflicts()


# ---------------------------------------------------------------- in-game menu
class OverlayMenu(QWidget):
    """A translucent full-page pause menu over the running game, keyboard- and
    mouse-navigable. It emits `chosen(action_id)`; the owner keeps the game alive
    and acts on it. The game never unloads until you explicitly quit -- pausing
    is not a reason to throw machine state away."""

    chosen = pyqtSignal(str)

    def __init__(self, parent: QWidget) -> None:
        super().__init__(parent)
        self.setObjectName("overlayMenu")
        self._buttons: list[QPushButton] = []
        self._ids: list[str] = []
        self._sel = 0

        self.panel = QFrame(self); self.panel.setObjectName("menuPanel")
        pv = QVBoxLayout(self.panel)
        pv.setContentsMargins(14, 14, 14, 16); pv.setSpacing(4)
        self.title = QLabel(""); self.title.setObjectName("menuTitle")
        self.title.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        pv.addWidget(self.title); pv.addSpacing(8)
        self._list = QVBoxLayout(); self._list.setSpacing(2)
        pv.addLayout(self._list)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.hide()

    def set_items(self, title: str, items: list[tuple[str, str]]) -> None:
        self.title.setText(title)
        while self._list.count():
            it = self._list.takeAt(0)
            if it.widget():
                it.widget().deleteLater()
        self._buttons.clear(); self._ids.clear()
        for action_id, label in items:
            b = QPushButton(label); b.setObjectName("menuItem")
            b.setCursor(Qt.CursorShape.PointingHandCursor)
            b.clicked.connect(lambda _=False, a=action_id: self.chosen.emit(a))
            b.enterEvent = lambda _e, i=len(self._buttons): self._select(i)  # type: ignore
            self._list.addWidget(b)
            self._buttons.append(b); self._ids.append(action_id)
        self._sel = 0
        self._refresh()

    def _select(self, i: int) -> None:
        self._sel = i % max(1, len(self._buttons))
        self._refresh()

    def _refresh(self) -> None:
        for i, b in enumerate(self._buttons):
            b.setProperty("sel", "true" if i == self._sel else "false")
            b.style().unpolish(b); b.style().polish(b)

    def show_over(self) -> None:
        p = self.parentWidget()
        if p is not None:
            self.setGeometry(0, 0, p.width(), p.height())
        self.show(); self.raise_(); self.setFocus()
        self._center()

    def _center(self) -> None:
        self.panel.adjustSize()
        pw, ph = self.panel.width(), self.panel.height()
        self.panel.move((self.width() - pw) // 2, (self.height() - ph) // 2)

    def resizeEvent(self, e) -> None:  # type: ignore[override]
        self._center(); super().resizeEvent(e)

    def keyPressEvent(self, e: QKeyEvent) -> None:  # noqa: N802
        k = e.key()
        if k in (int(Qt.Key.Key_Down), int(Qt.Key.Key_S)):
            self._select(self._sel + 1)
        elif k in (int(Qt.Key.Key_Up), int(Qt.Key.Key_W)):
            self._select(self._sel - 1)
        elif k in (int(Qt.Key.Key_Return), int(Qt.Key.Key_Enter),
                   int(Qt.Key.Key_X), int(Qt.Key.Key_Space)):
            if self._ids:
                self.chosen.emit(self._ids[self._sel])
        elif k in (int(Qt.Key.Key_Escape), int(Qt.Key.Key_Backspace)):
            self.chosen.emit("resume")
        else:
            super().keyPressEvent(e)


# ---------------------------------------------------------------- play
class _Canvas(QLabel):
    """The game surface. A QLabel like before, but it reports pointer movement (mouse
    tracking on, so it fires with no button held) so the toolbar can auto-hide when the
    mouse goes still and come back the moment it moves. No paintEvent override -- that is
    what turns a stray exception into a hard abort (see the debugger's gauge); a plain
    label showing a pixmap cannot."""

    def __init__(self, on_activity, on_doubleclick) -> None:
        super().__init__()
        self._on_activity = on_activity
        self._on_doubleclick = on_doubleclick
        self.setMouseTracking(True)

    def mouseMoveEvent(self, e) -> None:  # type: ignore[override]
        self._on_activity()
        super().mouseMoveEvent(e)

    def mouseDoubleClickEvent(self, e) -> None:  # type: ignore[override]
        # Double-click toggles fullscreen -- the standard way in and out, and the way
        # back that a fullscreen with no visible chrome needs.
        if e.button() == Qt.MouseButton.LeftButton:
            self._on_doubleclick()
        super().mouseDoubleClickEvent(e)


class _OverlayLabel(QLabel):
    """The centred message strip under the game. Its 20px font means an EMPTY label
    still reserves a line of height -- which, once the toolbar auto-hides in
    fullscreen, is the stray blank bar left along the bottom. Collapsing to zero
    space whenever it has nothing to say makes that bar disappear."""

    def setText(self, text) -> None:  # type: ignore[override]
        super().setText(text)
        self.setVisible(bool(text))


# How fast a HELD rewind runs, as frames dropped per step (a step is ~1/60 s).
# The first half-second stays at 1x so a short tap still lands on the frame you
# meant; then it doubles every half-second up to 8x, which crosses a 30-second
# buffer in under four seconds. Kept as data, and as a pure function, because
# "why did it jump" is a question you should be able to answer by reading it.
REWIND_RAMP = ((30, 1), (60, 2), (90, 4))
REWIND_MAX_SPEED = 8


def rewind_speed(steps_held: int) -> int:
    for limit, speed in REWIND_RAMP:
        if steps_held < limit:
            return speed
    return REWIND_MAX_SPEED


class _RewindBar(QWidget):
    """The rewind history as a strip under the game, while you are in it.

    Rewind used to say `⏪ 137` in the middle of the screen. That is a number of
    frames, which nobody thinks in, over the picture you are trying to look at --
    and it says nothing about the one thing that actually matters while you hold
    the key: how much history is LEFT before you run out of it.

    So: a strip along the bottom, the width of the ring, filled up to where you
    are, with the time in seconds. It appears when you rewind and goes away when
    you stop -- there is nothing to look at the rest of the time, and a permanent
    bar under the picture is a permanent tax on the picture.

    Dragging it scrubs. That falls out of the same data and is the reason a strip
    beats a counter: you can go BACK TO A MOMENT rather than feel your way there
    one key-press at a time.
    """

    scrubbed = pyqtSignal(int)          # requested position in the ring
    grabbed = pyqtSignal()              # the mouse took hold of it
    dropped = pyqtSignal()              # ...and let go

    HEIGHT = 30
    MARGIN = 10

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setFixedHeight(self.HEIGHT)
        self.setVisible(False)
        self.setCursor(Qt.CursorShape.SizeHorCursor)
        # It floats OVER the picture, so it draws its own backing -- without one the
        # game would show through the text and neither would be readable.
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, False)
        self._total = 0
        self._pos = 0
        self._speed = 1
        self._dragging = False

    def set_range(self, total: int, pos: int, speed: int = 1) -> None:
        total = max(0, int(total))
        pos = max(0, min(total - 1, int(pos))) if total else 0
        speed = max(1, int(speed))
        if (total, pos, speed) == (self._total, self._pos, self._speed):
            return          # nothing moved: the paused loop calls this every tick
        self._total, self._pos, self._speed = total, pos, speed
        self.update()

    @property
    def frames_back(self) -> int:
        return max(0, self._total - 1 - self._pos)

    def _label(self) -> str:
        # Kept SHORT: every character the label takes is a character of track, and
        # the track is the part you aim at. The first wording spelled out "of 10 s
        # held" and left the bar less than half the width of its own strip.
        # The speed is shown while it ramps, because a picture that suddenly runs
        # eight times faster with nothing on screen to say so reads as a glitch.
        rate = f"  ×{self._speed}" if self._speed > 1 else ""
        return (f"−{self.frames_back / 60.0:.1f}s / "
                f"{self._total / 60.0:.0f}s{rate}")

    INSET = 12                       # the rounded plate's left+right padding

    def _track_width(self) -> int:
        """The track stops BEFORE the label. Drawing them over each other put the
        text on top of the fill exactly when the fill was full -- which is where
        the cursor sits most of the time."""
        metrics = QFontMetrics(self._font())
        usable = self.width() - self.INSET
        return max(20, usable - metrics.horizontalAdvance(self._label()) - 18)

    def _font(self):
        f = self.font(); f.setPointSize(9)
        return f

    def _index_at(self, x: int) -> int:
        x -= self.INSET // 2             # the plate's left padding
        track = self._track_width()
        if self._total <= 1 or track <= 1:
            return 0
        frac = min(1.0, max(0.0, x / track))
        return int(round(frac * (self._total - 1)))

    def mousePressEvent(self, e) -> None:  # type: ignore[override]
        self._dragging = True
        self.grabbed.emit()             # stop the linger timer: it is in use now
        self.scrubbed.emit(self._index_at(int(e.position().x())))

    def mouseMoveEvent(self, e) -> None:  # type: ignore[override]
        if self._dragging:
            self.scrubbed.emit(self._index_at(int(e.position().x())))

    def mouseReleaseEvent(self, e) -> None:  # type: ignore[override]
        if self._dragging:
            self._dragging = False
            self.dropped.emit()

    def paintEvent(self, e) -> None:  # type: ignore[override]
        p = ngpc_theme.current()
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        w, h = self.width(), self.height()
        plate = QColor(p.bg_window)
        plate.setAlpha(216)             # readable over any frame, still not opaque
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(plate)
        painter.drawRoundedRect(0, 0, w, h, 8, 8)
        w -= 12                          # inset, so nothing touches the rounded edge
        painter.translate(6, 0)
        track = self._track_width()
        track_h = 6
        y = (h - track_h) // 2
        painter.setBrush(QColor(p.bg_hover))
        painter.drawRoundedRect(0, y, track, track_h, 3, 3)
        fill = 0
        if self._total > 1:
            # Filled from the OLDEST frame still held up to where you are, so the
            # LEFT edge is the end of the history -- which is the thing you want to
            # see coming while the key is down.
            fill = int(track * (self._pos / (self._total - 1)))
            painter.setBrush(QColor(p.accent))
            painter.drawRoundedRect(0, y, max(2, fill), track_h, 3, 3)
        # A tick per second of history, drawn AFTER the fill -- underneath it they
        # vanished exactly where the cursor spends its time. Without them the strip
        # says how far back you are only in words; with them you can see it, which
        # is the difference between reading a number and knowing where you are.
        if self._total > 60:
            tick = QColor(p.text)
            tick.setAlpha(90)          # readable over the fill AND over the track
            painter.setBrush(tick)
            seconds = self._total / 60.0
            for i in range(1, int(seconds) + 1):
                x = int(track * (i / seconds))
                if 2 < x < track - 2:
                    painter.drawRect(x, y - 3, 1, track_h + 6)
        if self._total > 1:
            painter.setBrush(QColor(p.accent))
            painter.drawEllipse(max(5, min(track - 5, fill)) - 5, h // 2 - 5, 10, 10)
        painter.setPen(QColor(p.text))
        painter.setFont(self._font())
        painter.drawText(QRect(track, 0, w - track - 4, h),
                         int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter),
                         self._label())
        painter.end()


def _close_pipe(pipe) -> None:
    """Let go of whatever a core.netplay pipe is carrying.

    Mirror play runs over more than one transport now -- a direct socket
    (core.netplay.SocketPipe) or the lobby relay (core.lobby.LobbyPipe) -- and each
    owns something nobody else will close. The socket one exposes `sock`; the lobby
    one has a room to leave and a client thread to stop, behind `close()`.
    """
    if pipe is None:
        return
    closer = getattr(pipe, "close", None)
    if callable(closer):
        try:
            closer()
        except Exception:  # noqa: BLE001 -- tearing down; nothing left to salvage
            pass
        return
    sock = getattr(pipe, "sock", None)
    if sock is not None:
        try:
            sock.close()
        except OSError:
            pass


class PlayPage(QWidget):
    exit_requested = pyqtSignal()
    debug_requested = pyqtSignal()
    options_requested = pyqtSignal(str)   # "video" | "audio" | "controls"
    link_requested = pyqtSignal()         # 🔗 : two players on this PC (2nd window)
    net_host_requested = pyqtSignal()     # 🔗 : host a direct network game
    net_join_requested = pyqtSignal()     # 🔗 : join a direct network game
    net_lobby_requested = pyqtSignal()    # 🔗 : open the online lobby (server)
    net_link_lost = pyqtSignal(str)       # the online peer went away, with the reason
    mirror_ended = pyqtSignal(str)        # a mirror session refused or desynced
    net_mirror_host_requested = pyqtSignal()   # 🔗 : host a MIRROR game
    net_mirror_join_requested = pyqtSignal()   # 🔗 : join a MIRROR game
    net_cancel_requested = pyqtSignal()   # 🔗 : take back a host/join not yet connected
    link_frame_requested = pyqtSignal()   # 🔗 : both consoles inside one window

    def __init__(self, settings, library: lib.Library) -> None:
        super().__init__()
        self.setObjectName("page")
        self._settings = settings
        self._lib = library
        # Which player this page is, for two-player link play: it selects the
        # input bindings + gamepad, and whose serial bytes go where. 1 = the normal
        # single-player page; a second page (P2) is created in its own window.
        self._player = 1
        self._link_peer = None               # the other PlayPage, when linked (local 2P)
        # Frames this console already ran inside the peer's interleaved step, waiting
        # for our own tick to collect them. See _run_frame_interleaved.
        self._prerun = deque()
        self._net_link = None                # a core.link.TcpLink, when linked online
        # True while a direct host/join is still looking for its peer. The shell owns
        # the attempt; this is the page's copy, so the 🔗 menu can offer to cancel it.
        self.net_pending = False
        # Mirror netplay (core/netplay.py): both consoles run HERE and only the
        # controller bytes cross the network. None = not in that mode.
        self._mirror = None
        self._mirror_peer_session = None
        self._mirror_waits = 0               # consecutive frames spent waiting on the peer
        self._mirror_desynced = False
        # A linked guest (player 2) runs LEAN: no audio sink of its own. Two audio
        # sinks fighting for the device both crackle AND make the 1x audio-clock
        # pacer stall (a starved sink returns 0 frames due, freezing that console
        # for up to ~300 ms) -- which desyncs the two consoles enough to break a
        # game's link handshake. Lean pages pace off the wall clock instead, at a
        # steady 60 fps, and stay silent. Player 1 keeps the audio.
        self._lean = False
        self._link_tx_total = 0              # bytes this console has put on the cable
        # A core.link_debug.LinkMonitor when the debugger's Link tab is watching:
        # it taps every byte the cable carries, delivers hand-injected ones, and
        # applies any latency/loss the user dialled in. None = untapped, and the
        # relay below runs exactly as it did before.
        self.link_monitor = None
        self._play_t0: float | None = None   # wall clock since the game last resumed
        self.session: NativeSession | None = None
        self.machine = None
        self._raw = None                 # a bare machine for the BIOS-alone boot
        self._real_bios = False
        self._power_pressed = False
        self.held = 0
        self.paused = False
        self._rom_path: Path | None = None
        self._crashed = False              # latched when the ROM faults, until next start/reset
        self._bp_step_off = False          # parked on a breakpoint: step past it on resume
        self._rewind: deque[bytes] = deque()           # frame-perfect history
        self._rw_pos: int | None = None    # scrub cursor, or None when live at the tip
        self._rw_hold = 0                  # steps the rewind key has been held
        self._rw_bar_timer = None          # keeps the strip up after a rewind
        self._rw_resume_after_drag = False  # was the game running when you grabbed it
        self._rewind_on = True
        self._rewinding = False            # held-rewind active (running backward)
        self._rw_accum = 0                 # tick divider so rewind plays at ~60 fps
        self._rebuild_rewind_buffer()
        self._last_audio = b""             # last drained chunk, for the debug oscilloscope
        self._vgm = None                   # a VgmRecorder while capturing, else None
        self._song = None                  # a NgpsRecorder while capturing, else None
        self._stall_ticks = 0              # consecutive idle ticks -> unstick audio pacing
        self._did_handoff = False          # real-BIOS: completed the BIOS->cart hand-off
        self._bios_frames = 0              # real-BIOS: frames run since power-on (intro gate)
        self._menu_ticks = 0               # frames the BIOS has idled in its shell loop
        self._slot = 0                     # active save-state slot (0..STATE_SLOTS-1)
        self._speed = 1.0                  # emulation speed multiplier
        self._ff = False                   # fast-forward while a key is held
        self._scale = cfg.lcd_scale(settings)
        self._smooth = cfg.smoothing(settings)
        self._filter = cfg.video_filter(settings)
        self._color = cfg.color_profile(settings)
        self._aspect = cfg.aspect_mode(settings)
        self._fullscreen = cfg.fullscreen(settings)
        self._bindings: dict[int, int] = {}
        self._hotkeys: dict[int, str] = {}   # key code -> hotkey action id
        # -- input beyond the keyboard: a controller, and autofire.
        self._pad = ngpc_input.make_pad(0)
        self._pad_on = True                # the setting; the pad may still be absent
        self._pad_held = 0                 # controller mask, merged with `self.held`
        self._turbo_mask = 0               # joypad bits that autofire while held
        self._turbo_hz = 10
        self._frame = 0                    # free-running EMULATED frame counter
        self.pending = bytearray()
        self.watches = WatchSet()          # named memory watches, loaded per-ROM
        # Named cheats: groups of addresses held at a value. NOT a second freezing
        # mechanism -- they are written at the same point in the frame as the
        # locked watches below, because two things that both "hold a value" would
        # eventually disagree about which one won.
        self.cheats = CheatSet()
        self.breaks = ExecBreakSet()       # PC execution breakpoints, loaded per-ROM
        # Debug tools that need to sample state once per EMULATED frame rather than at
        # the UI's refresh rate -- the RAM-search change counter is meaningless unless
        # it is counting frames. Empty (and free) unless a tool subscribes.
        self.frame_hooks: list = []
        # The memory viewer's access-highlighting probe. The core has ONE read-log and
        # ONE write-log window, so a viewer that wants to colour accesses and a
        # watchpoint that wants to break on them are competing for the same instrument.
        # Rather than let them silently clobber each other, the viewer sets this and the
        # debug UI says out loud that watchpoints are suspended while it is on.
        self.access_probe: tuple[int, int] | None = None
        # Input recording / replay. The console takes ONE byte of input per frame,
        # so a session IS a snapshot plus a list of bytes -- see core/movie.py.
        # Exactly one of these is ever set: recording a replay would just copy the
        # file back out, which is not a thing anyone means to do.
        self.movie_rec = None              # core.movie.Recorder while recording
        self.movie_play = None             # core.movie.Player while replaying
        self.movie_ended = None            # set to the finished Player, for the UI
        # Symbol table for breakpoint CONDITIONS, so a guard can say `[_player_hp] == 0`.
        # The debug window owns the loading and pushes it here.
        self.symbols = None
        self.pacer = FramePacer()
        self.sink = None
        self.audio = None
        self.debt = 0.0
        self._reblit_pending = False       # one deferred re-fit at a time (see _reblit_soon)
        # Toolbar auto-hide: a still mouse hides the bar after a moment; any move brings it
        # back. `_idle_hidden` is the transient hide, kept apart from the user's saved
        # show/hide preference so the two never fight (see refresh_toolbar).
        self._idle_hidden = False
        self._autohide_ms = 2500
        self._autohide_timer = QTimer(self); self._autohide_timer.setSingleShot(True)
        self._autohide_timer.timeout.connect(self._idle_hide_toolbar)
        self.wall_last = time.perf_counter()

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        self.lcd = _Canvas(self._on_pointer_activity, self._toggle_fullscreen)
        self.lcd.setObjectName("lcd")
        self.lcd.setAlignment(Qt.AlignmentFlag.AlignCenter)   # centres the (letterboxed) frame
        outer.addWidget(self.lcd, 1)                          # stretch 1 -> fills the page
        self.overlay = _OverlayLabel(""); self.overlay.setObjectName("overlay")
        self.overlay.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.overlay.setVisible(False)   # empty -> takes no space (no stray bottom bar)
        outer.addWidget(self.overlay, 0, Qt.AlignmentFlag.AlignCenter)
        # The rewind history, under the picture and ONLY while you are in it. It
        # belongs here rather than in the debug window: rewinding is something you
        # do with the game in front of you, and a strip you have to open another
        # window to look at is a strip you never look at.
        # ⛔ NOT `outer.addWidget`. In the layout it TOOK SPACE, so the picture shrank
        # the moment you touched rewind and grew back when you let go -- the image
        # jumping around is worse than the information is worth. It is a floating
        # child over the bottom of the canvas instead, like the OSD, and `_place_
        # rewind_bar` follows the window.
        self.rewind_bar = _RewindBar(self)
        self.rewind_bar.scrubbed.connect(self._scrub_to)
        self.rewind_bar.grabbed.connect(self._begin_scrub)
        self.rewind_bar.dropped.connect(self._end_scrub)
        # On-screen stats (FPS etc.), a floating child pinned top-left over the canvas.
        self.osd = QLabel("", self); self.osd.setObjectName("osd")
        self.osd.move(10, 8); self.osd.setVisible(False)
        self._fps = 0.0; self._fps_frames = 0; self._fps_t0 = time.perf_counter()
        # The core's own frame counter, so the readout can report frames that were really
        # EMULATED rather than frames the pacer asked for -- see _tick.
        self._core_frames = 0
        # ⚡ CONSOLE-SIDE LOAD, for the debug tools. The on-screen fps says how the HOST is
        # doing, and on any modern PC that is a flat 60 that tells you nothing. What is
        # worth knowing is what the emulated console is doing: how much of its frame the
        # game is burning, and whether the game is still hitting 60 or has fallen behind
        # the way it would on real hardware. See `perf()`.
        self._perf_instr = 0
        self._perf_wall = 0.0            # real seconds actually spent emulating
        self._perf_frames = 0            # console frames emulated in that time
        self._perf_oam_prev = b""
        self._perf_oam_hits = 0          # frames in which the sprite table changed
        self._perf_t0 = time.perf_counter()
        self._perf = {"speed": 0.0, "game_fps": 0.0, "instr": 0}

        # A discoverable control bar under the screen (save states, speed, shot, reset…).
        self.toolbar = self._make_toolbar()
        outer.addWidget(self.toolbar, 0)
        # A little always-visible tab to bring the bar back when it is hidden.
        self._bar_show = QPushButton("▴", self); self._bar_show.setObjectName("barShow")
        self._bar_show.setFixedSize(30, 18)
        self._bar_tips.append((self._bar_show, "tb_show_toolbar", cfg.HK_TOOLBAR))
        self._refresh_toolbar_tips()
        self._bar_show.clicked.connect(lambda: self._toggle_toolbar(True))
        self._bar_show.hide()
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)

        self.menu = OverlayMenu(self)
        self.menu.chosen.connect(self._on_menu_choice)
        self._menu_open = False

        self.timer = QTimer(self)
        self.timer.setTimerType(Qt.TimerType.PreciseTimer)
        self.timer.timeout.connect(self._tick)
        self._check_hotkey_table()

    # ---- lifecycle
    def _watch_path(self) -> Path:
        stem = self._rom_path.stem if self._rom_path else "ngpc"
        return WATCH_DIR / f"{stem}.json"

    def _break_path(self) -> Path:
        stem = self._rom_path.stem if self._rom_path else "ngpc"
        return WATCH_DIR / f"{stem}.breaks.json"

    def _cheat_path(self) -> Path:
        stem = self._rom_path.stem if self._rom_path else "ngpc"
        return WATCH_DIR / f"{stem}.cheats.json"

    def save_cheats(self) -> None:
        if self._rom_path is not None:
            try:
                self.cheats.save(self._cheat_path())
            except OSError:
                pass

    def _save_watches(self) -> None:
        if self._rom_path is not None:
            try:
                self.watches.save(self._watch_path())
            except OSError:
                pass
        self.apply_debug()

    def _save_breaks(self) -> None:
        if self._rom_path is not None:
            try:
                self.breaks.save(self._break_path())
            except OSError:
                pass
        self.apply_debug()

    def apply_debug(self) -> None:
        """Push the current breakpoints and write-log window into the core. Called
        after any edit from the debug window, and once per game start."""
        if self.machine is None:
            return
        try:
            self.machine.set_breakpoints(self.breaks.enabled_pcs())
        except Exception:
            pass
        # The access probe (memory-viewer highlighting) WINS when it is on: it is an
        # explicit, visible mode, and pretending both can own the log window would mean
        # one of them quietly returning nothing.
        wrng = self.access_probe or self.watches.write_range()
        rrng = self.access_probe or self.watches.read_range()
        try:
            self.machine.set_write_log(*(wrng if wrng else (1, 0)))   # lo > hi disarms
        except Exception:
            pass
        try:
            self.machine.set_read_log(*(rrng if rrng else (1, 0)))
        except Exception:
            pass

    def _mono_console(self) -> bool:
        """True when this is the ORIGINAL monochrome NGP, not an NGPC. A property of the
        CONSOLE, so it applies to every cartridge -- a colour game in an NGP is a real
        situation, and several of them (SNK vs. Capcom) notice and show another screen.

        ⚡ THE BIOS THAT BOOTS IS THE MACHINE. The two used to be independent settings,
        and a user who selected the NGP BIOS got its boot intro followed by the game in
        full colour -- because the silicon underneath was still an NGPC. You cannot boot
        one console's BIOS on the other's hardware; there is no such machine."""
        return console_is_mono(self._settings)

    def _bios_path(self) -> Path | None:
        """The BIOS the user selected in Settings ▸ BIOS -- one of three slots."""
        return resolve_selected_bios(self._settings)[0]

    def _using_hle_bios(self) -> bool:
        """True when the BIOS in use is the clean-room HLE fallback (no real bios.bin).
        The HLE image only supports the instant hand-off, not the console-boot path."""
        p = self._bios_path()
        return p is not None and p == HLE_BIOS

    def start(self, rom: Path) -> None:
        """Boot a game. Default is the instant hand-off (a running console handed
        to the cartridge). 'Console boot' runs the real BIOS power-on first: the NMI
        power-manager boots the BIOS, it plays its NEO GEO POCKET intro, and once it
        settles into its pre-boot shell loop `_bios_handoff_assist` hands off to the
        cartridge -- so the game boots on its own after the intro, like real hardware.
        A first-boot (unconfigured coin cell) still stops at the language/date setup."""
        self.stop()
        # A frame a link peer ran ahead belonged to the console just closed; the new one
        # has run nothing. (stop() detaches a mirror, but a local cable peer is detached
        # by the shell, so this cannot be left to it.)
        self._prerun.clear()
        self._rom_path = Path(rom)
        self.watches.load(self._watch_path())   # this ROM's named watches, if any
        self.cheats.load(self._cheat_path())    # ...and its cheats
        self.breaks.load(self._break_path())    # ...and its execution breakpoints
        self._crashed = False
        # Console boot needs a real BIOS; the clean-room HLE only does the hand-off.
        self._real_bios = (cfg.real_bios(self._settings)
                           and self._bios_path() is not None
                           and not self._using_hle_bios())
        self._power_pressed = False
        mode = cfg.save_mode(self._settings)
        # Unpack a .zip/.7z here (once) so the flash chip is sized on the ROM's real,
        # uncompressed length -- the archive's file size would be wrong -- and the bytes
        # are handed straight to the session instead of being read twice.
        loaded = rom_loader.load(self._rom_path)
        cap = cfg.flash_capacity_bytes(self._settings, len(loaded.data))
        self.session = NativeSession(
            rom, rom_bytes=loaded.data, from_archive=loaded.from_archive,
            bios_path=self._bios_path(), real_bios=self._real_bios,
            save_to_rom=mode in (cfg.SAVE_ROM, cfg.SAVE_BOTH),
            sidecar=mode in (cfg.SAVE_SIDECAR, cfg.SAVE_BOTH),
            flash_size=cap, clock_mode=cfg.clock_mode(self._settings),
            clock_manual=cfg.clock_manual(self._settings),
            k1ge_console=self._mono_console(),
            language=cfg.cart_language(self._settings),
            # The HLE image has no BIOS setup screen, so there the UI setting is the
            # console's only control panel. With a real BIOS, the BIOS owns it.
            hle_bios=resolve_selected_bios(self._settings)[1] == cfg.BIOS_USE_HLE,
            # ⚡ PLAYER 2 IS A SECOND CONSOLE, not a second window onto the first.
            # Both pages are built here, so without this they shared one coin cell:
            # the same clock down to the crystal's sub-second phase, and a write-back
            # race where the last window closed stamped its time over the other's.
            # `set_player(2)` runs before `start()`, so this is already right.
            # See NativeSession.second_console for the measurements.
            second_console=(self._player == 2))
        self.machine = self.session.machine
        if self._link_peer is not None:          # keep the cable live across a restart
            self.machine.serial_set_enabled(True)
        # Silicon-calibrated cart-flash wait-states so self-timed games run at their
        # real 30fps instead of ~2x too fast. See cfg.cart_wait_states / project memo.
        if cfg.cart_wait_states(self._settings):
            # ONE call arms the whole model -- states x2, per-word fetch, the pipelined
            # bus interface unit and its 4-byte queue, the two-stage transmitter. Arming
            # half of it measures a machine no player has, which is exactly why the eight
            # separate setters were collapsed into this.
            self.machine.set_timing_silicon(cfg.CART_FETCH_WAIT, cfg.CART_BIOS_WAIT)
        self.watches.rearm()
        self.apply_debug()                 # arm breakpoints + write-log for this ROM
        self._rebuild_rewind_buffer()      # pick up any rewind-length change
        self._did_handoff = False; self._menu_ticks = 0; self._bios_frames = 0
        self._lib.note_launch(self._rom_path)   # one more play, and the most recent
        self._begin_run()

    def start_bios(self) -> None:
        """Boot the BIOS with NO cartridge -- the console's own screen (language,
        clock/date). One of the NGPC's signature features. Needs a BIOS image."""
        self.stop()
        bios = self._bios_path()
        if bios is None or bios == HLE_BIOS:   # console boot needs a real BIOS
            self.exit_requested.emit()
            return
        self._real_bios = True
        self._power_pressed = False
        # No cartridge: clear the last game's path, or the console's own BIOS screen
        # would be credited as playtime to whatever was played before it.
        self._rom_path = None
        self._raw = native.NativeMachine(_NO_CART, bios=bios.read_bytes())
        # ⚡ WHICH CONSOLE THIS IS -- the one thing this path forgot to say.
        #
        # Booting a cartridge goes through NativeSession, which is handed
        # `k1ge_console=self._mono_console()`. This path builds its machine by hand and
        # never did, so a monochrome NGP dump was booted on K2GE silicon. The BIOS runs
        # perfectly there (measured: same PC, same 900 frames) -- it simply never writes
        # the colour palettes it has no reason to know about, so every pixel resolves
        # through empty palette RAM and the whole screen is BLACK. That is the reported
        # "select a monochrome bios and then boot bios -> black screen": not a boot
        # failure at all, a console-type one. With the flag set the same dump comes up
        # in its seven greys, on its own SELECT ONE / POCKET MENU screens.
        #
        # `_mono_console` is the same answer the cartridge path uses, and it is the BIOS
        # IMAGE that decides -- you cannot run one console's BIOS on the other's silicon.
        self._raw.set_k1ge_console(self._mono_console())
        if _SYSTEM_RAM.exists():
            self._raw.set_battery_ram(_SYSTEM_RAM.read_bytes())
        # The console's clock, kept alive by the same coin cell as those settings. This is
        # the screen where the player SETS the date, so it is the one place it must not be
        # thrown away -- and the BIOS only ever rewrites the chip itself when the cell is
        # blank, so on a configured console what we restore here is what it will show.
        # Same policy as a game boot, from the same place: see native_session.apply_saved_clock.
        ns.apply_saved_clock(self._raw, _SYSTEM_RTC, cfg.clock_mode(self._settings))
        self._raw.reset(real_bios=True)
        self.session = None
        self.machine = self._raw
        self._begin_run()

    # ---- play history
    def _commit_playtime(self) -> None:
        """Bank the wall time since the game last started or resumed. Called on every
        exit from running -- suspend (a menu) as well as stop -- so time spent sitting
        in the settings screen is not counted as time spent playing."""
        if self._play_t0 is not None and self._rom_path is not None:
            self._lib.add_playtime(self._rom_path, time.perf_counter() - self._play_t0)
        self._play_t0 = None

    def _begin_run(self) -> None:
        self.held = 0
        self.paused = False
        self._play_t0 = time.perf_counter()
        self.overlay.setText("")
        self.apply_settings()
        if cfg.audio_enabled(self._settings) and not self._lean:
            self._open_audio()               # lean guests stay silent (see _lean)
        self.setFocus()
        self.timer.start(4)

    def stop(self) -> None:
        self.timer.stop()
        self._autohide_timer.stop(); self._idle_hidden = False   # no game -> no idle hide
        # ⚡ The mirror owns a SECOND console and a socket. Left attached, they outlive
        # the game that opened them, and the next `start()` would step a session whose
        # local machine has been closed.
        self.detach_mirror()
        self._commit_playtime()
        if self._rom_path is not None:    # keep this ROM's watches + breakpoints
            try:
                self.watches.save(self._watch_path())
                self.breaks.save(self._break_path())
            except OSError:
                pass
        if self.sink is not None:
            self.sink.stop(); self.sink = None; self.audio = None
        self.pending.clear()
        if self.session is not None:
            try:
                self.session.close()          # commits the cart save + the coin cell
            except Exception:
                pass
        elif self._raw is not None:
            try:
                if self._real_bios:            # keep the BIOS's language/date
                    _SYSTEM_RAM.parent.mkdir(parents=True, exist_ok=True)
                    _SYSTEM_RAM.write_bytes(self._raw.battery_ram())
                    # Both halves of the coin cell, or the console half-remembers: the
                    # date the player just set on the BIOS's own clock screen lives in the
                    # calendar chip, which is machine state and rides in no RAM dump.
                    _write_rtc_file(_SYSTEM_RTC, self._raw.rtc())
            except Exception:
                pass
            try:
                self._raw.close()
            except Exception:
                pass
        self.session = None
        self._raw = None
        self.machine = None

    def apply_settings(self) -> None:
        self._bindings = cfg.key_bindings(self._settings, self._player)
        self._pad = ngpc_input.make_pad(
            cfg.player_pad_index(self._settings, self._player))
        self._hotkeys = cfg.hotkey_bindings(self._settings)
        self._refresh_toolbar_tips()      # a rebind must not leave the bar naming the old key
        self._turbo_mask = cfg.turbo_mask(self._settings)
        self._turbo_hz = cfg.turbo_hz(self._settings)
        self._pad_on = cfg.gamepad_enabled(self._settings)
        if not self._pad_on:
            self._pad_held = 0        # drop anything the pad was holding
        self._scale = cfg.lcd_scale(self._settings)
        self._smooth = cfg.smoothing(self._settings)
        self._filter = cfg.video_filter(self._settings)
        self._color = cfg.color_profile(self._settings)
        self._aspect = cfg.aspect_mode(self._settings)
        self._fullscreen = cfg.fullscreen(self._settings)
        # The LCD ALWAYS fills the page and follows the window; _blit() scales the
        # frame into whatever size it currently has (with the chosen aspect). The
        # 'scale' setting is now just the preset window size applied on launch, not a
        # hard cap on the canvas -- resizing the window (or a preset) drives the size.
        self.lcd.setMinimumSize(SCREEN_W, SCREEN_H)
        self.lcd.setMaximumSize(16777215, 16777215)
        self.lcd.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Ignored)
        self.osd.setVisible(cfg.show_fps(self._settings)); self.osd.raise_()
        if cfg.rewind_seconds(self._settings) != (self._rewind.maxlen // 60 if self._rewind_on else 0):
            self._rebuild_rewind_buffer()      # rewind length changed -> resize the ring
        self.refresh_toolbar()      # preference + fullscreen-hide + idle auto-hide, one place
        # Real fullscreen on the top-level window (was previously only a canvas-fill flag).
        win = self.window()
        if win is not None:
            if self._fullscreen and not win.isFullScreen():
                win.showFullScreen()
            elif not self._fullscreen and win.isFullScreen():
                win.showNormal()
        if self.machine is not None:
            self._blit()          # re-fit after any settings change (filter/colour/aspect)
            self._reblit_soon()   # ...and again once a fullscreen/toolbar change has laid out

    def _open_audio(self) -> None:
        fmt = QAudioFormat()
        fmt.setSampleRate(native.NativeMachine.AUDIO_RATE_HZ)
        fmt.setChannelCount(2)
        fmt.setSampleFormat(QAudioFormat.SampleFormat.Int16)
        dev = QMediaDevices.defaultAudioOutput()
        if dev is None or dev.isNull():
            return
        self.sink = QAudioSink(dev, fmt, self)
        self.sink.setBufferSize(int(0.10 * native.NativeMachine.AUDIO_RATE_HZ) * 4)
        vol = cfg.audio_volume(self._settings) / 100.0
        try:
            self.sink.setVolume(vol)
        except Exception:
            pass
        self.audio = self.sink.start()

    # ---- input
    def event(self, e) -> bool:  # type: ignore[override]
        # Qt eats Tab (and Shift+Tab) for focus traversal before any key handler sees
        # it. Grab both here unconditionally: focus traversal is meaningless in the
        # game view, and this must not depend on Tab still being the fast-forward key
        # -- it is rebindable now, and something else may have been bound TO Tab.
        if e.type() in (QEvent.Type.KeyPress, QEvent.Type.KeyRelease) \
                and e.key() in (int(Qt.Key.Key_Tab), int(Qt.Key.Key_Backtab)):
            (self.keyPressEvent if e.type() == QEvent.Type.KeyPress
             else self.keyReleaseEvent)(e)
            return True
        return super().event(e)

    # ---- two consoles on one PC move TOGETHER ------------------------------
    # ⛔ THE BUG THIS ENDS, reported by a homebrew author building a link game:
    # "if you adjust the speed (or pause) in one session, it would be useful to
    # apply the same adjustment to the other side". It is not a convenience --
    # it is a desync. `_run_frame_relaying` deliberately refuses to drive a peer
    # that is paused or rewinding (a console must stay where the user put it), so
    # pausing ONE window left the OTHER console running alone against a partner
    # that had stopped answering, and the game's own link protocol timed out.
    # Speed is the same story one step down: one console at 0.25x and the other
    # at 1x are two consoles that no longer agree on when a byte is due.
    #
    # 🔑 MIRROR NETPLAY IS DELIBERATELY NOT INCLUDED, and that is not an oversight.
    # There both consoles run on BOTH PCs from one shared input stream, and
    # `MirrorSession.step()` returns "waiting" until the peer's input for THIS
    # frame has arrived (core/netplay.py) -- so neither PC can run ahead of the
    # other whatever its speed dial says. The mode is already self-limiting:
    # nothing to propagate, and nothing to refuse either.
    def _link_both(self, apply) -> None:
        """Run `apply` on this console and, in local 2-player, on the peer too.

        `apply` takes a PlayPage and must be one of the `_apply_*` helpers below,
        which never mirror in turn -- so the propagation lives HERE and only here
        and there is no way to build a loop out of it.
        """
        apply(self)
        peer = self._link_peer
        if peer is not None and peer is not self and peer.machine is not None:
            apply(peer)

    def _apply_pause(self, on: bool) -> None:
        self.paused = bool(on)
        self.overlay.setText(
            cfg.tr(cfg.language(self._settings), "paused") if self.paused else "")

    def _pause_peer(self, on: bool) -> None:
        """Pause/resume ONLY the linked peer. For callers that already own what
        their own console shows -- the pause MENU draws itself over the screen, so
        it must not also get the "paused" banner underneath, while player 2, who
        sees no menu at all, needs to be told why their console just stopped."""
        peer = self._link_peer
        if peer is not None and peer is not self and peer.machine is not None:
            peer._apply_pause(on)                            # noqa: SLF001

    def _apply_ff(self, on: bool, *, flush_audio: bool, sync_button: bool = False) -> None:
        """`flush_audio` keeps the two callers' behaviour exactly as it was: the
        toolbar toggle drops the queued audio (a stale fast-forward backlog froze
        the loop), the held hotkey only restarts the wall clock.

        ⚠️ `sync_button` is for the TOGGLE path only. The held hotkey must leave the
        toolbar button alone: releasing it restores whatever the button says, so a
        press that also ticked the button would make fast-forward stick on release.
        """
        self._ff = bool(on)
        btn = getattr(self, "_ff_btn", None)
        if sync_button and btn is not None and btn.isChecked() != self._ff:
            btn.blockSignals(True)       # ...or the peer's toggle calls us straight back
            btn.setChecked(self._ff)
            btn.blockSignals(False)
        if flush_audio:
            self._reset_pacing()
        else:
            self.wall_last = time.perf_counter(); self.debt = 0.0

    def _apply_speed(self, speed: float) -> None:
        self._speed = speed
        self._reset_pacing()
        if hasattr(self, "_speed_lbl"):
            self._speed_lbl.setText(f"{self._speed:g}×")
        self._flash(cfg.tr(cfg.language(self._settings), "speed").format(x=self._speed))

    def _toggle_pause(self) -> None:
        want = not self.paused
        self._link_both(lambda pg: pg._apply_pause(want))    # noqa: SLF001 -- sibling page

    def _begin_fast_forward(self) -> None:
        self._link_both(lambda pg: pg._apply_ff(True, flush_audio=False))  # noqa: SLF001

    def _end_fast_forward(self) -> None:
        """Releasing the hotkey returns to whatever the toolbar's toggle says."""
        on = self._ff_btn.isChecked()
        self._link_both(lambda pg: pg._apply_ff(on, flush_audio=False))    # noqa: SLF001

    def _hotkey_actions(self) -> dict:
        """action id -> what it does. Every entry must exist in cfg.HOTKEYS, and
        every id in cfg.HOTKEYS must appear here -- `_check_hotkey_table` asserts
        both at startup so a half-added hotkey cannot ship as a dead key."""
        return {
            cfg.HK_MENU: self.open_menu,               # PAUSE, don't quit
            cfg.HK_DEBUG: self.debug_requested.emit,
            cfg.HK_FS: self._toggle_fullscreen,
            cfg.HK_PAUSE: self._toggle_pause,
            cfg.HK_RESET: self._do_reset,
            cfg.HK_SAVE: self.save_state,
            cfg.HK_LOAD: self.load_state,
            cfg.HK_SLOT: lambda: self.set_slot((self._slot + 1) % STATE_SLOTS),
            cfg.HK_SHOT: self.screenshot,
            cfg.HK_TOOLBAR: self._toggle_toolbar,
            cfg.HK_FASTER: lambda: self.cycle_speed(True),
            cfg.HK_SLOWER: lambda: self.cycle_speed(False),
            cfg.HK_STEP: self.step_forward,
            # hold-style (see cfg.HOLD_HOTKEYS): press starts, release ends
            cfg.HK_FF: self._begin_fast_forward,
            cfg.HK_REWIND: self.start_rewind,
        }

    def _check_hotkey_table(self) -> None:
        """Fail loudly if the hotkey LIST and the hotkey HANDLERS have drifted apart.
        A key in cfg.HOTKEYS with no handler is a rebindable key that does nothing --
        exactly the sort of thing that ships unnoticed because nobody presses it."""
        listed = {a for a, _k, _n in cfg.HOTKEYS}
        handled = set(self._hotkey_actions())
        if listed != handled:
            raise RuntimeError(
                f"hotkey table mismatch: no handler for {sorted(listed - handled)}, "
                f"handler for unlisted {sorted(handled - listed)}")
        if not cfg.HOLD_HOTKEYS <= set(self._release_actions()):
            raise RuntimeError("hold hotkeys with no release handler: "
                               f"{sorted(cfg.HOLD_HOTKEYS - set(self._release_actions()))}")

    def _release_actions(self) -> dict:
        """What ends a held hotkey. Only cfg.HOLD_HOTKEYS need an entry."""
        return {
            # releasing fast-forward returns to whatever the toolbar's toggle says
            cfg.HK_FF: self._end_fast_forward,
            cfg.HK_REWIND: self.stop_rewind,
        }

    def keyPressEvent(self, e: QKeyEvent) -> None:  # noqa: N802
        k = e.key()
        # Escape LEAVES fullscreen first -- the universal 'get me out', and the reason a
        # fullscreen with hidden chrome is never a trap. Only then does Escape fall through
        # to its normal binding (the pause menu) when windowed. The menu's own Escape is
        # handled while it has focus, so this fires only when no menu is open.
        if k == int(Qt.Key.Key_Escape):
            win = self.window()
            if win is not None and win.isFullScreen():
                self._toggle_fullscreen(); return
        # Window size is the one hotkey that is not rebindable -- it needs Ctrl, so
        # it can never shadow a joypad key. Checked first so a plain digit stays free.
        if (e.modifiers() & Qt.KeyboardModifier.ControlModifier) and \
                int(Qt.Key.Key_1) <= k <= int(Qt.Key.Key_5):
            self.set_window_scale(k - int(Qt.Key.Key_1) + 1); return
        action = self._hotkeys.get(k)
        if action is not None:
            # A held hotkey must fire once, not once per auto-repeat: fast-forward
            # would keep resetting its own pacing clock and rewind would restart.
            if action in cfg.HOLD_HOTKEYS and e.isAutoRepeat():
                return
            handler = self._hotkey_actions().get(action)
            if handler is not None:
                handler()
            return
        bit = self._bindings.get(k)
        if bit and not e.isAutoRepeat():
            self.held |= bit

    # ---- in-game menu / suspend-resume
    def _bios_handoff_assist(self) -> None:
        """Complete the BIOS -> cartridge hand-off the way the console does.

        On real hardware the BIOS boots (our NMI drives that), plays its NEO GEO POCKET
        intro, validates the cart — it reads the header at 0x200000, including the 24-bit
        ENTRY VECTOR at 0x20001C — then JUMPS to that entry. Our core runs the whole
        authentic boot (config, RTC, cart validation AND the intro logo), but the BIOS's
        own shell loop never issues that final jump.

        So we wait until the intro has actually PLAYED (the console has run a good few
        seconds of BIOS frames — the NEO GEO POCKET logo shows around frame 300 and the
        intro ends near 460) and the BIOS has then settled into its pre-boot shell loop
        (PC parked low in BIOS space, FF11xx), and only THEN perform the jump it would —
        the game boots exactly where the cartridge's entry point says.

        The frame gate matters: the raw "PC hit FF5xxx" signal fires on a 1-frame boot
        transient (~frame 4) long before the real intro renders, so keying off it hands
        off instantly and the user never sees the intro. Counting BIOS frames instead
        guarantees the whole intro plays. An UNCONFIGURED console never reaches the FF11xx
        idle — it sits on the setup screen (FF35xx) waiting for input — so this correctly
        leaves first-boot setup alone.
        """
        if (self._did_handoff or self.session is None or not self._real_bios
                or self.machine is None):
            return
        pc = self.machine.cpu().pc
        # ⚡ FIRST-BOOT SELF-CONFIGURE. A console whose coin cell never went through the
        # language/date/color wizard drops into that SETUP screen (PC in 0xFF35xx) and would
        # sit there forever -- the player only wanted to launch a game. So we auto-complete
        # the wizard with its defaults by writing the exact "setup confirmed" flag its final
        # screen writes (0x64E5 = 0xFF; found by reversing the BIOS setup loop at 0xFF35E7 /
        # 0xFF3610). The BIOS then finalizes the config, plays its FULL intro, and -- because
        # we persist the coin cell at hand-off -- the console is configured from then on and
        # never shows the wizard again. So even a brand-new console gets: intro -> game.
        if 0xFF3000 <= pc < 0xFF4000:                 # in the first-boot setup wizard
            self.machine.write(0x0064E5, b"\xFF")     # its "confirmed" flag -> complete it
            self._menu_ticks = 0
            return
        # Once the BIOS has played its whole intro and parked in its pre-boot idle shell loop
        # (PC in 0xFF11xx), hand off to the cartridge. Waiting for BIOS_INTRO_FRAMES lets the
        # entire NEO GEO POCKET intro play first.
        if not (0xFF1000 <= pc < 0xFF2000 and self._bios_frames >= BIOS_INTRO_FRAMES):
            self._menu_ticks = 0                      # still booting / in the intro -> wait
            return
        need = 6
        self._menu_ticks += 1              # confirm it has truly settled there
        if self._menu_ticks < need:
            return
        # ⛔ DO NOT pre-read the entry vector from live memory to "sanity check" it. The
        # BIOS boot leaves the cart FLASH in AUTOSELECT mode (it probed the chip ID), so
        # 0x200000.. reads back the chip's ID bytes (0x98 0x2C ...) and 0x20001C reads
        # 0xFFFFFF -- "not sane" -- so we would bail WITHOUT booting: the exact live blank
        # the head-less test never reproduced, because it reset unconditionally. The
        # hand-off reset RELOADS the pristine cart image (flash back in read mode) and
        # takes PC from the ROM's real entry point, so there is nothing to validate first.
        #
        # Boot the cart with the exact post-BIOS machine state a game expects -- the SAME
        # clean slate the instant hand-off gives it: seeded system bytes + ZEROED work RAM.
        #
        # ⚡ DETERMINISM. The real BIOS scribbles its own scratch all over work RAM while it
        # runs, and that scratch keeps changing for a few frames after the intro. Our
        # hand-off can land on any of those frames (the pacer runs a variable number), so
        # handing the cart the BIOS's live work RAM makes it inherit DIFFERENT leftover
        # bytes each launch -- Back To My Fruits then drew doubled/glitched sprites on some
        # boots and rendered perfectly on others. Booting from a blank slate makes real-BIOS
        # mode render byte-for-byte identical to the (always-correct) instant mode, every
        # launch. The coin cell (language/date) is preserved in the battery-RAM BUFFER for a
        # later reboot and for the save, while the live work RAM the game sees is clean.
        # (A raw PC jump instead of this reset leaves video/peripherals wrong -> blank cart.)
        #
        # ⚡ THE COIN CELL is what the BIOS has in work RAM RIGHT NOW: it booted with the
        # configured RAM and stamped its "booted" marker (0x6C7A = 0xA5A5), so this holds
        # the language/date + marker that make the NEXT boot skip first-time setup. We must
        # capture it HERE, before wiping work RAM for the game -- persisting the game's blank
        # work RAM instead would drop the marker and the BIOS would run first-boot every
        # launch. It becomes the session's baseline (what commit_system_ram writes) and goes
        # back in the buffer so a reboot still boots configured.
        coin = self.machine.battery_ram()
        # ⚡ THE WIZARD WE ANSWERED FOR THEM. We auto-completed the BIOS's first-boot setup
        # with its defaults, and this cell is about to become what the console remembers --
        # so on a console that had never been configured, the language the player chose in
        # Settings is written in as their answer to that screen. Exactly once: from here on
        # the cell is configured, and only the BIOS's own setup screen (Boot BIOS) changes
        # it. Without this the setting was dead for everyone running a real BIOS -- the
        # wizard's default would silently become the console's language, for good.
        if self.session is not None and self.session.started_unconfigured:
            lang = bytes([cfg.cart_language(self._settings) & 0xFF])
            self.machine.write(0x006F87, lang)
            coin = bytearray(coin)
            coin[0x006F87 - ns.native.RAM_START:0x006F88 - ns.native.RAM_START] = lang
            coin = bytes(coin)
        if self.session is not None:
            self.session.system_ram_baseline = coin
        # ⚡ THE CLOCK IS THE OTHER HALF OF THAT COIN CELL, and it must be carried over the
        # hand-off BY HAND. Two things would otherwise destroy it here: we blank the coin
        # cell for the reset, and the hand-off reset boots the BIOS internally to capture
        # its character RAM -- a boot that, seeing a blank cell, takes the dead-battery path
        # and stamps 1998-01-01 over the chip. So the date the BIOS just showed the player
        # would be gone by the time the game reads it. Snapshot it, hand it straight back.
        clock = self.machine.rtc()
        # ⚡ THE RESET RELOADS THE PRISTINE CART IMAGE, so the cartridge's SAVE and the
        # console's settings page have to be carried over it by hand -- exactly as a power
        # cycle does. Done in the session, next to `reboot`, which has the same rule; see
        # `NativeSession.handoff_reset` for the two reports this closes (a separate-file
        # save that never loaded, and the BIOS config reset at every launch).
        self.session.handoff_reset(coin, clock)
        self.apply_debug()                  # re-arm breakpoints/write-log after the reset
        self._did_handoff = True
        self.overlay.setText("")
        # Drop the queued BIOS audio so the game's sound is not delayed behind it. We only
        # clear our own pending buffer -- we do NOT stop/restart the QAudioSink here: it is
        # already playing (opened in _begin_run), its buffer is a tiny 0.1 s, and tearing it
        # down mid-tick was killing in-game sound. The sink flows straight from BIOS to game.
        self._reset_pacing()

    # ---- instruction-level stepping (the debugger's primitives) -----------
    # A runaway backstop for "run until we get somewhere". A whole frame is a few
    # tens of thousands of instructions, so this is several frames' worth: enough
    # for any call to return, small enough that a wrong guess does not hang the UI.
    STEP_LIMIT = 2_000_000

    def step_instruction(self, count: int = 1) -> int:
        """Execute exactly `count` instructions and stop. Nothing to do with frames:
        the core keeps its own raster/timer state, so the screen simply shows whatever
        was drawn last until enough instructions have passed to finish a frame.

        Returns how many instructions actually retired.
        """
        if self.machine is None:
            return 0
        self.paused = True
        # Standing ON an enabled breakpoint must not stop us before we move: the core
        # skips a breakpoint on the first instruction of a batch, which is exactly the
        # behaviour we want here, but we clear the flag anyway so a later resume does
        # not step off a second time.
        self._bp_step_off = False
        summ, _ = self.machine.run(max(1, count), record=False)
        self._blit()
        return int(summ.executed)

    def run_until_pc(self, targets, limit: int | None = None) -> tuple[bool, int]:
        """Run until PC reaches one of `targets`, a user breakpoint fires, or `limit`
        instructions pass. Returns (reached_a_target, instructions_run).

        The temporary targets are merged into the core's breakpoint list and removed
        again in a `finally`, so an exception mid-run cannot leave phantom breakpoints
        armed on the machine.
        """
        if self.machine is None:
            return (False, 0)
        targets = {int(t) & 0xFFFFFF for t in targets}
        limit = self.STEP_LIMIT if limit is None else limit
        self.paused = True
        saved = self.breaks.enabled_pcs()
        ran = 0
        try:
            self.machine.set_breakpoints(sorted(set(saved) | targets))
            while ran < limit:
                summ, _ = self.machine.run(min(4096, limit - ran), record=False)
                ran += int(summ.executed)
                if summ.stop_status == native.STATUS_BREAKPOINT:
                    return (int(summ.stop_pc) in targets, ran)
                if summ.stop_status in _CRASH_STATUSES:
                    return (False, ran)
                if int(summ.executed) == 0:
                    return (False, ran)      # halted and going nowhere
        finally:
            try:
                self.machine.set_breakpoints(saved)
            except Exception:
                pass
            self._blit()
        return (False, ran)

    def step_over(self, next_pc: int | None, is_call: bool) -> int:
        """One instruction, but a call runs to completion. `next_pc`/`is_call` come
        from the caller's disassembly -- the player does not decode.

        The stack pointer is the guard: a recursive routine hits its own return
        address before it is really done, so 'we are back' means the PC is right AND
        the stack has unwound to at least where it started.
        """
        if self.machine is None:
            return 0
        if not is_call or next_pc is None:
            return self.step_instruction(1)
        entry_sp = self.machine.cpu().regs[7]
        total = 0
        while total < self.STEP_LIMIT:
            reached, ran = self.run_until_pc([next_pc], self.STEP_LIMIT - total)
            total += ran
            if not reached:
                return total                       # breakpoint, crash, or gave up
            if self.machine.cpu().regs[7] >= entry_sp:
                return total                       # really back, not mid-recursion
            self.step_instruction(1)               # move off it and keep waiting
            total += 1
        return total

    def step_out(self) -> int:
        """Run until the current routine returns, i.e. until the stack pointer climbs
        back above where it is now. Stepping one at a time is the only honest way --
        we do not know the return address without reading the frame, and a leaf that
        never pushed one would send us somewhere arbitrary."""
        if self.machine is None:
            return 0
        self.paused = True
        entry_sp = self.machine.cpu().regs[7]
        ran = 0
        try:
            # ONE instruction per crossing, on purpose. Batching would only tell us the
            # stack had unwound at the end of the batch, landing us up to a batch-length
            # PAST the return -- which is not a step-out, it is a jump into the middle of
            # the caller. A crossing is ~0.3 us, so even a long routine costs milliseconds.
            while ran < self.STEP_LIMIT:
                summ, _ = self.machine.run(1, record=False)
                ran += int(summ.executed)
                if summ.stop_status in _CRASH_STATUSES or int(summ.executed) == 0:
                    break
                if self.machine.cpu().regs[7] > entry_sp:
                    break
        finally:
            self._blit()
        return ran

    def _do_reset(self) -> None:
        if self._mirror_blocks():
            return
        if self.session is not None:
            self.session.reboot()
        elif self._raw is not None:
            self._raw.reset(real_bios=True)
        self.held = 0
        self._prerun.clear()          # those frames belong to the console we just reset
        self._power_pressed = False
        self._crashed = False
        self._bp_step_off = False
        self._did_handoff = False; self._menu_ticks = 0; self._bios_frames = 0
        self._rewind.clear(); self._rw_pos = None
        self.rewind_bar.setVisible(False)   # no history left to show
        self.watches.rearm()
        self.apply_debug()                 # a reboot clears core breakpoints -> re-arm

    def _toggle_fullscreen(self) -> None:
        # Base the flip on the WINDOW's real state, not the stored flag: double-click, the
        # hotkey and Escape all reach this, and if the two ever drifted a toggle could send
        # you deeper into fullscreen instead of out.
        win = self.window()
        going_fs = not (win is not None and win.isFullScreen())
        self._settings.setValue("gfx/fullscreen", going_fs)
        self.apply_settings()
        self._reblit_soon()   # showFullScreen/showNormal lays out next turn -- fit to it then

    def set_window_scale(self, k: int) -> None:
        """Preset window size: make the canvas exactly k x (160*k by 152*k), keeping the
        current window chrome. The canvas still follows any later manual resize."""
        win = self.window()
        if win is None or win.isFullScreen():
            return
        self._settings.setValue("gfx/scale", int(k))
        dw = max(0, win.width() - self.lcd.width())     # window - canvas = chrome/margins
        dh = max(0, win.height() - self.lcd.height())
        win.resize(SCREEN_W * k + dw, SCREEN_H * k + dh)
        # A preset bigger than the screen is a window whose title bar you cannot reach.
        # Ask for it anyway (above) so the canvas is as large as it can be, then let the
        # window fold back onto the panel it is on.
        fit = getattr(win, "fit_to_screen", None)
        if callable(fit):
            fit()
        if self.machine is not None:
            self._blit()
            self._reblit_soon()   # the resize lays out next turn -- fit to the real box then

    # ---- player toolbar ---------------------------------------------------
    def _make_toolbar(self) -> QFrame:
        bar = QFrame(); bar.setObjectName("playbar")
        h = QHBoxLayout(bar); h.setContentsMargins(8, 4, 8, 4); h.setSpacing(6)

        # (button, base tooltip, hotkey action). The key part of each tooltip is
        # filled in by `_refresh_toolbar_tips` from the CURRENT binding -- these
        # used to name Esc/F5/F12 literally, which a rebind turned into a lie.
        self._bar_tips: list[tuple[QPushButton, str, str | None]] = []

        def btn(text, tip, slot, checkable=False, action=None):
            b = QPushButton(text); b.setObjectName("barBtn")
            b.setFocusPolicy(Qt.FocusPolicy.NoFocus); b.setCheckable(checkable)
            if checkable:
                b.toggled.connect(slot)
            else:
                b.clicked.connect(slot)
            self._bar_tips.append((b, tip, action))
            h.addWidget(b); return b

        # Tooltips are lang-file KEYS (see `_refresh_toolbar_tips`), so the whole bar
        # is translatable from lang/*.json.
        btn("☰", "tb_menu", self.open_menu, action=cfg.HK_MENU)
        btn("⟲", "tb_reset", self._do_reset, action=cfg.HK_RESET)
        self._link_btn = btn("🔗", "link_btn_tip", self._show_link_menu)
        h.addSpacing(10)
        self._slot_lbl = QLabel(cfg.tr(cfg.language(self._settings), "tb_slot"))
        h.addWidget(self._slot_lbl)
        self._slot_spin = QSpinBox(); self._slot_spin.setRange(1, STATE_SLOTS)
        self._slot_spin.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._slot_spin.valueChanged.connect(lambda v: setattr(self, "_slot", v - 1))
        h.addWidget(self._slot_spin)
        btn("💾", "tb_save_state", lambda: self.save_state(), action=cfg.HK_SAVE)
        btn("📂", "tb_load_state", lambda: self.load_state(), action=cfg.HK_LOAD)
        h.addSpacing(10)
        btn("📷", "tb_screenshot", self.screenshot, action=cfg.HK_SHOT)
        h.addSpacing(10)
        rw = QPushButton("⏪"); rw.setObjectName("barBtn")
        rw.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        rw.pressed.connect(self.start_rewind); rw.released.connect(self.stop_rewind)
        self._bar_tips.append((rw, "tb_rewind", cfg.HK_REWIND))
        h.addWidget(rw)
        # ⏭, not ⏵: this steps ONE FRAME and pauses. With a play triangle on it
        # people press it to resume, nothing resumes, and the emulator looks stuck.
        btn("⏭", "tb_step", self.step_forward, action=cfg.HK_STEP)
        h.addSpacing(10)
        btn("−", "tb_slower", lambda: self.cycle_speed(False), action=cfg.HK_SLOWER)
        self._speed_lbl = QLabel("1×"); self._speed_lbl.setObjectName("barSpeed")
        self._speed_lbl.setFixedWidth(36)
        self._speed_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        h.addWidget(self._speed_lbl)
        btn("+", "tb_faster", lambda: self.cycle_speed(True), action=cfg.HK_FASTER)
        self._ff_btn = btn("⏩", "tb_ff", self._set_ff,
                           checkable=True, action=cfg.HK_FF)
        h.addStretch()
        btn("⛶", "tb_fullscreen", self._toggle_fullscreen, action=cfg.HK_FS)
        btn("▾", "tb_hide_toolbar", lambda: self._toggle_toolbar(False), action=cfg.HK_TOOLBAR)
        self._refresh_toolbar_tips()
        return bar

    def _refresh_toolbar_tips(self) -> None:
        """Re-stamp each toolbar tooltip with the key that is actually bound now.

        Each `base` is a lang-file key, translated here through `cfg.tr` (which
        returns the string unchanged when it is not a known key, so this stays safe
        for any plain literal). Called on build, on rebind, and on language change,
        so switching language re-translates the whole bar live."""
        lang = cfg.language(self._settings)
        if getattr(self, "_slot_lbl", None) is not None:
            self._slot_lbl.setText(cfg.tr(lang, "tb_slot"))   # the "Slot" label is not a tip
        for button, base, action in self._bar_tips:
            base = cfg.tr(lang, base)
            if action is None:
                button.setToolTip(base)
                continue
            key = QKeySequence(cfg.hotkey_code(self._settings, action)).toString()
            button.setToolTip(f"{base} ({key})" if key else base)

    def _toggle_toolbar(self, show: bool | None = None) -> None:
        # Flip the saved PREFERENCE (not the transient idle state): base the toggle on
        # what the user last chose, so pressing it while the bar is idle-hidden still
        # does the intuitive thing.
        pref = bool(self._settings.value("gfx/toolbar", True, type=bool))
        show = (not pref) if show is None else show
        self._settings.setValue("gfx/toolbar", show)
        self._idle_hidden = False          # a manual toggle clears an idle hide
        self.refresh_toolbar()
        # Hiding/showing the toolbar hands its height to the LCD, but this widget does not
        # resize (only its inner layout), so nothing else re-fits the frame. Do it once the
        # layout has settled -- otherwise a paused game keeps the old, mis-scaled picture.
        self._reblit_soon()
        self.setFocus()

    def _fs_hides_chrome(self) -> bool:
        win = self.window()
        return (win is not None and win.isFullScreen()
                and cfg.fs_hide_ui(self._settings))

    def _toolbar_wants_visible(self) -> bool:
        """Whether the user wants the toolbar at all -- their saved show/hide preference.
        Fullscreen no longer forces it off: it makes it AUTO-HIDE instead (see
        `_autohide_active`), so a mouse move still brings it back over the game. Kept off
        `isVisible()`, which also needs the window shown and so lies in a headless test."""
        return bool(self._settings.value("gfx/toolbar", True, type=bool))

    def _autohide_active(self) -> bool:
        """The toolbar auto-hides (still mouse -> gone, any move -> back) when the user
        turned that on, OR whenever fullscreen is hiding the chrome -- so a fullscreen
        toolbar is reachable by moving the mouse instead of being gone for good."""
        return cfg.toolbar_autohide(self._settings) or self._fs_hides_chrome()

    def refresh_toolbar(self) -> None:
        """The ONE place that sets the toolbar's (and its 'show' nub's) visibility, from
        the inputs that must not fight: the saved preference, the idle/fullscreen
        auto-hide, and its transient hidden state."""
        want = self._toolbar_wants_visible()
        autohide = self._autohide_active()
        self.toolbar.setVisible(want and not (autohide and self._idle_hidden))
        # The 'show toolbar' nub is the windowed way back when the user HID it on purpose;
        # in fullscreen a mouse move reveals the bar instead, so no nub there.
        nub = (not want) and not self._fs_hides_chrome()
        self._bar_show.setVisible(nub)
        if nub:
            self._position_bar_show()
        self._arm_autohide()

    # ---- toolbar auto-hide (still mouse -> hide; any move -> show) ----------
    def _arm_autohide(self) -> None:
        """(Re)start the idle countdown, but only while the bar is up and something wants
        it to auto-hide. Stopping it otherwise keeps a hidden or pinned bar from ticking."""
        should = (self._autohide_active() and self.machine is not None
                  and not self._idle_hidden and self._toolbar_wants_visible())
        if should:
            self._autohide_timer.start(self._autohide_ms)
        else:
            self._autohide_timer.stop()

    def _on_pointer_activity(self) -> None:
        """The canvas saw the mouse move: bring an idle-hidden bar back, or push the
        countdown out if it is already up."""
        if not self._autohide_active() or self.machine is None:
            return
        if self._idle_hidden:
            self._idle_hidden = False
            self.refresh_toolbar()
            self._reblit_soon()
        else:
            self._arm_autohide()

    def _idle_hide_toolbar(self) -> None:
        if (not self._autohide_active() or self.machine is None
                or self._idle_hidden or not self._toolbar_wants_visible()):
            return
        # Don't pull the bar out from under a cursor resting ON it -- wait another beat.
        if self.toolbar.underMouse():
            self._autohide_timer.start(self._autohide_ms)
            return
        self._idle_hidden = True
        self.refresh_toolbar()
        self._reblit_soon()

    def _position_bar_show(self) -> None:
        self._bar_show.move(self.width() - self._bar_show.width() - 8,
                            self.height() - self._bar_show.height())
        self._bar_show.raise_()

    def _reset_pacing(self) -> None:
        """Drop the queued audio + restart the wall clock. Called on any speed change so
        returning to 1x never hits a stale backlog (which froze the loop -- frames_to_run
        returns 0 while the huge fast-forward backlog drains)."""
        self.pending.clear()
        self.wall_last = time.perf_counter(); self.debt = 0.0

    def _drain_pending(self) -> None:
        """Push whatever queued audio the card can take. Runs EVERY tick (even when we
        advance no frames), so `pending` can never sit full and stall the pacer."""
        if self.audio is not None and self.sink is not None and self.pending:
            free = self.sink.bytesFree()
            take = min(free, len(self.pending)); take -= take % 4
            if take > 0:
                self.audio.write(bytes(self.pending[:take])); del self.pending[:take]

    def _restart_audio(self) -> None:
        """Reopen the audio sink from scratch. After a long pause (scrubbing the rewind)
        the sink can sit underrun and stop reporting free space, which stalls the audio-
        clock pacing forever; a fresh sink guarantees the loop resumes."""
        if self.audio is None and self.sink is None:
            return
        try:
            if self.sink is not None:
                self.sink.stop()
        except Exception:
            pass
        self.sink = None; self.audio = None
        self.pending.clear()
        if cfg.audio_enabled(self._settings):
            self._open_audio()

    def _set_ff(self, on: bool) -> None:
        self._link_both(                                                 # noqa: SLF001
            lambda pg: pg._apply_ff(on, flush_audio=True, sync_button=True))

    # ---- save states (per-ROM slots) --------------------------------------
    def _state_path(self, slot: int) -> Path | None:
        if self._rom_path is None:
            return None
        STATE_DIR.mkdir(exist_ok=True)
        return STATE_DIR / f"{self._rom_path.stem}.s{slot}"

    def _mirror_blocks(self) -> bool:
        """True (and says so) when an action would rewind THIS console only.

        ⛔ In mirror play the other PC is simulating both consoles too, from the shared
        input stream alone. A savestate, a rewind or a reset applied here and nowhere
        else puts the two copies in different states -- the checksum would catch it a
        second later and end the match, which is a rotten way to answer a keypress. So
        the keypress is refused instead, and the player is told why.
        """
        if self._mirror is None:
            return False
        self._flash(cfg.tr(cfg.language(self._settings), "mirror_local_only"))
        return True

    def _flash(self, msg: str, ms: int = 1100) -> None:
        """Briefly show a status line over the game.

        ⛔ NOT `QTimer.singleShot(ms, lambda: ...)`. That timer belongs to nobody: it
        outlives the page, and when it fires into a widget that has been torn down the
        process dies with qFatal or an access violation, no traceback, at whatever the
        test suite happens to be doing a couple of modules later. MEASURED TWICE this
        session -- once from the link-cable message, once from here, and the second time
        it looked exactly like a bug in somebody else's tests. A QTimer PARENTED to the
        page is destroyed with the page, so the call cannot outlive its target; the
        callback still guards, because a page can be deleted while the timer is queued.

        ⛔ ...AND ONE TIMER, RESTARTED -- not a new one per message. A second `_flash`
        used to leave the FIRST one running (parented to the page, so alive and still
        connected), and when it expired it wiped a message that was newer than itself.
        That is not cosmetic: `attach_mirror` flashes "mirror ready" for 2.5 s, so a
        mirror session that ended inside those 2.5 s had its reason erased about a
        second later -- "the connection drops and I have no time to read the error".
        The one thing the player needed in order to say what went wrong.
        """
        self.overlay.setText(msg)
        if getattr(self, "_overlay_timer", None) is None:
            self._overlay_timer = QTimer(self)
            self._overlay_timer.setSingleShot(True)
            self._overlay_timer.timeout.connect(self._clear_overlay)
        self._overlay_timer.stop()
        self._overlay_timer.start(ms)

    def _clear_overlay(self) -> None:
        try:
            self.overlay.setText(
                cfg.tr(cfg.language(self._settings), "paused") if self.paused else "")
        except RuntimeError:
            pass            # the page went away first; nothing to clear

    def set_slot(self, slot: int) -> None:
        self._slot = max(0, min(STATE_SLOTS - 1, slot))
        if hasattr(self, "_slot_spin"):
            self._slot_spin.setValue(self._slot + 1)
        self._flash(cfg.tr(cfg.language(self._settings), "slot").format(n=self._slot + 1))

    # A snapshot is the CPU struct + the sound/timer block + the whole working image
    # (I/O + RAM + VRAM). The rewind ring and the save-state slots share it; slots just
    # add a magic + go to disk.
    def _capture_state(self) -> bytes:
        return (bytes(self.machine.rtc())
                + bytes(self.machine.cpu())
                + bytes(self.machine.aux_state())
                + bytes(self.machine.link_state())
                + self.machine.read(0, STATE_MEM_LEN))

    def _apply_state(self, body: bytes, aux: bool = True, link: bool = True,
                     rtc: bool = True) -> None:
        # Frames a link peer ran ahead for us describe the console we are about to
        # overwrite; collecting them after the load would report work this timeline
        # never did (and rewind calls this for every step).
        self._prerun.clear()
        if rtc:
            rtc_len = ctypes.sizeof(native.RtcState)
            self.machine.set_rtc(native.RtcState.from_buffer_copy(body[:rtc_len]))
            body = body[rtc_len:]
        cpu_len = ctypes.sizeof(type(self.machine.cpu()))
        aux_len = ctypes.sizeof(native.AuxState) if aux else 0
        link_len = ctypes.sizeof(native.LinkState) if link else 0
        head = cpu_len + aux_len + link_len
        cpu = type(self.machine.cpu()).from_buffer_copy(body[:cpu_len])
        mem = body[head:head + STATE_MEM_LEN]
        self.machine.write(0, mem)
        self.machine.set_cpu(cpu)
        # ⚠️ AFTER the image, never before. Writing the image goes through the control
        # registers, and 0x00BA is a door -- "fire one NMI at the sound CPU" -- not a
        # byte of storage. Restoring the sound CPU's own state here is what cancels that
        # phantom NMI, on top of putting its registers and the chip's back.
        if aux:
            st = native.AuxState.from_buffer_copy(body[cpu_len:cpu_len + aux_len])
            self.machine.set_aux_state(st)
        # ...and the cable after the image for the same shape of reason: SC0MOD/BR0CR
        # live in the image and decide how fast a byte shifts.
        if link:
            ls = native.LinkState.from_buffer_copy(
                body[cpu_len + aux_len:cpu_len + aux_len + link_len])
            self.machine.set_link_state(ls)

    def save_state(self, slot: int | None = None) -> None:
        if self.machine is None:
            return
        if self._mirror_blocks():
            return
        slot = self._slot if slot is None else slot
        path = self._state_path(slot)
        if path is None:
            return
        path.write_bytes(STATE_MAGIC + self._capture_state())
        self._flash(cfg.tr(cfg.language(self._settings), "state_saved").format(n=slot + 1))

    def load_state(self, slot: int | None = None) -> None:
        if self.machine is None:
            return
        if self._mirror_blocks():
            return
        slot = self._slot if slot is None else slot
        path = self._state_path(slot)
        if path is None or not path.is_file():
            self._flash(cfg.tr(cfg.language(self._settings), "state_empty").format(n=slot + 1))
            return
        blob = path.read_bytes()
        # An older file predates a block: load it as it was written, rather than refuse
        # a state the user already has. v1 has no sound block (the music will still need
        # a scene change); v2 has no cable block (a state taken mid-transfer restores the
        # channel the machine happens to be holding -- which is exactly what v2 always
        # did, so nothing gets worse by loading one).
        if blob.startswith(STATE_MAGIC):
            self._apply_state(blob[len(STATE_MAGIC):])
        elif blob.startswith(STATE_MAGIC_V3):
            self._apply_state(blob[len(STATE_MAGIC_V3):], rtc=False)
        elif blob.startswith(STATE_MAGIC_V2):
            self._apply_state(blob[len(STATE_MAGIC_V2):], link=False, rtc=False)
        elif blob.startswith(STATE_MAGIC_V1):
            self._apply_state(blob[len(STATE_MAGIC_V1):], aux=False, link=False,
                              rtc=False)
        else:
            self._flash("bad state"); return
        self._rewind.clear(); self._rw_pos = None      # a loaded state starts a new timeline
        self.rewind_bar.setVisible(False)
        self._blit()
        self._flash(cfg.tr(cfg.language(self._settings), "state_loaded").format(n=slot + 1))

    # ---- rewind ----------------------------------------------------------
    def _drain_audio_silently(self) -> None:
        """Empty the core's audio ring without queuing it. Scrubbing/stepping produces
        audio we never play; if it piled up, the first live frame would dump a huge
        backlog into the pacer and freeze the loop (frames_to_run stays 0)."""
        if self.machine is not None:
            try:
                self.machine.audio()
            except Exception:
                pass

    def start_rewind(self) -> None:
        """Begin holding rewind: the game runs BACKWARD through the history for as long
        as the key/button is held. Release -> resume forward (stop_rewind)."""
        if self.machine is None or self._mirror_blocks():
            return
        self.paused = False
        self._rewinding = True
        self._rw_accum = 0
        self._rw_hold = 0                  # steps held, for the acceleration ramp

    def stop_rewind(self) -> None:
        """Release rewind -> resume normal forward play, cleanly (fresh pacing + sink so
        the audio clock never stalls)."""
        if not self._rewinding:
            return
        self._rewinding = False
        self._rw_pos = None
        self._rw_hold = 0                  # next hold starts slow again
        self.overlay.setText("")
        self._linger_rewind_bar()          # stays grabbable for a few seconds
        self._reset_pacing()
        self._restart_audio()

    def step_back(self) -> None:
        """Go one frame into the past (pauses). Repeat to scrub further back."""
        if self.machine is None or not self._rewind:
            return
        self.paused = True
        if self._rw_pos is None:
            self._rw_pos = len(self._rewind) - 1        # start scrubbing from the tip
        if self._rw_pos > 0:
            self._rw_pos -= 1
        self._apply_state(self._rewind[self._rw_pos])
        self._drain_audio_silently()
        self._blit()
        self._show_rewind_bar()

    def step_forward(self) -> None:
        """Go one frame toward the present: replay a buffered frame, or run a fresh one
        at the tip (pauses)."""
        if self.machine is None:
            return
        self.paused = True
        if self._rw_pos is not None and self._rw_pos < len(self._rewind) - 1:
            self._rw_pos += 1
            self._apply_state(self._rewind[self._rw_pos])
            self._flash(f"⏩ −{len(self._rewind) - 1 - self._rw_pos} f")
        else:
            self._leave_rewind()                        # branch from here if we had scrubbed
            self.machine.run_frames(1)
            self._rewind.append(self._capture_state())
            self._flash("⏩ +1 f")
        self._drain_audio_silently()
        self._blit()

    def _leave_rewind(self) -> None:
        """Return to live play from a scrubbed position: drop the frames we rewound past
        so the game continues from where we are now, and restart pacing cleanly so the
        loop does not stall on a stale audio backlog."""
        if self._rw_pos is not None:
            while len(self._rewind) > self._rw_pos + 1:
                self._rewind.pop()
            self._rw_pos = None
        self.overlay.setText("")
        self._linger_rewind_bar()
        self._drain_audio_silently()
        self._reset_pacing()
        self._restart_audio()          # a fresh sink -> pacing can never stay stalled

    # ---- the rewind strip --------------------------------------------------
    # It LINGERS after you let go of the key. Before that it vanished the instant
    # the key came up -- so the only moment it existed was the moment your hand was
    # busy holding a key, and the draggable cursor could never actually be grabbed.
    # Two ways to use rewind means both have to be reachable from the other.
    LINGER_MS = 4000

    def _place_rewind_bar(self) -> None:
        """Sit it over the bottom of the canvas, inside the picture's own width."""
        bar = getattr(self, "rewind_bar", None)
        if bar is None:
            return
        width = max(240, min(self.lcd.width() - 40, 560))
        x = (self.width() - width) // 2
        bottom = self.lcd.y() + self.lcd.height()
        y = min(self.height(), bottom) - bar.HEIGHT - bar.MARGIN
        bar.setGeometry(x, max(0, y), width, bar.HEIGHT)
        bar.raise_()

    def _hold_rewind_bar(self) -> None:
        """Keep the strip up: it is in use, so it must not vanish under the cursor."""
        if self._rw_bar_timer is not None:
            self._rw_bar_timer.stop()

    def _begin_scrub(self) -> None:
        """The mouse took hold of the strip.

        ⚠️ The "was it running" question can only be answered HERE, before the
        first `_scrub_to` pauses the game. Asking it from inside `_scrub_to` reads
        the pause that scrubbing itself just set, so the answer is always "it was
        paused" and the game never resumes -- which is exactly the trap this whole
        change exists to close.
        """
        self._hold_rewind_bar()
        self._rw_resume_after_drag = not self.paused

    def _end_scrub(self) -> None:
        """Let go of the strip: carry on playing from where you dropped it.

        ⛔ THE TRAP THIS CLOSES. Dragging pauses -- it has to, or the game runs out
        from under the position you are choosing. But leaving it paused afterwards
        strands you: this toolbar has NO play button. `⏵` is a one-frame step
        (which pauses again) and `⏩` does nothing at all while paused, because the
        loop returns before it. The only ways out were a key and the menu, and a
        mouse gesture should not require finding either.

        So the mouse mirrors the rewind key exactly: hold to move through time,
        let go to carry on. A game that was already paused before the drag stays
        paused, because then pausing was the thing you asked for.
        """
        if getattr(self, "_rw_resume_after_drag", False):
            self.paused = False
            self.overlay.setText("")
        self._linger_rewind_bar()

    def _linger_rewind_bar(self) -> None:
        """Leave it up for a few seconds so it can be grabbed, then put it away."""
        if not self.rewind_bar.isVisible():
            return
        if self._rw_bar_timer is None:
            # ⛔ PARENTED, never `QTimer.singleShot`. A timer that belongs to nobody
            # outlives the page, and firing into a torn-down widget kills the
            # process with no traceback (see _flash).
            self._rw_bar_timer = QTimer(self)
            self._rw_bar_timer.setSingleShot(True)
            self._rw_bar_timer.timeout.connect(self._hide_rewind_bar)
        self._rw_bar_timer.start(self.LINGER_MS)

    def _hide_rewind_bar(self) -> None:
        if self._rewinding or self._rw_pos is not None:
            return                      # still in use: the timer lost the race
        self.rewind_bar.setVisible(False)

    def _show_rewind_bar(self, speed: int = 1) -> None:
        """Show the history strip at the position we are actually at.

        While the key is HELD the ring is being popped, so the present is always
        its last frame. While frame-stepping, `_rw_pos` is the cursor and the ring
        is left intact -- two different truths, and the strip has to ask which one
        applies rather than assume one of them.
        """
        total = len(self._rewind)
        if total <= 1:
            self.rewind_bar.setVisible(False)
            return
        pos = self._rw_pos if self._rw_pos is not None else total - 1
        self.rewind_bar.set_range(total, pos, speed)
        if not self.rewind_bar.isVisible():
            self._place_rewind_bar()
            self.rewind_bar.setVisible(True)
            self.rewind_bar.raise_()

    def _scrub_to(self, index: int) -> None:
        """Jump to a point on the strip.

        Dragging is why a strip beats a counter: you can go back to A MOMENT
        instead of feeling your way there one key-press at a time.
        """
        if self.machine is None or len(self._rewind) <= 1:
            return
        index = max(0, min(len(self._rewind) - 1, int(index)))
        # A drag is a deliberate look at the past, so it pauses -- exactly as
        # stepping back does. Letting the game run under a dragged cursor would
        # fight every position you chose.
        self.paused = True
        self._rewinding = False
        self._hold_rewind_bar()
        self._rw_pos = index
        self._apply_state(self._rewind[index])
        self._drain_audio_silently()
        self._blit()
        self._show_rewind_bar()

    def _rebuild_rewind_buffer(self) -> None:
        """Size the rewind ring from the setting (0 s = off). ~51 KiB per frame.

        (48 KiB of image + the CPU struct + the sound/timer block + the 2112-byte link
        block, whose two 1 KiB FIFOs are nearly always almost empty -- a fixed size is
        what keeps the block ABI-stable, and it costs ~4% more ring: 28.5 -> 29.7 MiB
        at the 10 s setting.)"""
        secs = cfg.rewind_seconds(self._settings)
        self._rewind_on = secs > 0
        self._rewind = deque(maxlen=max(1, secs * 60))
        self._rw_pos = None

    # ---- crash reporting --------------------------------------------------
    def _needs_bios(self, summ) -> bool:
        """The no-BIOS signature: with no BIOS loaded the game calls a routine through
        the empty vector table at 0xFFFE00, jumps to 0, and faults down in low memory.
        Most homebrew do this on boot -- so a fault below the cart with no BIOS almost
        always means 'this game needs the BIOS', not a real emulation bug."""
        return self._bios_path() is None and summ.stop_pc < 0x200000

    def _on_crash(self, summ) -> None:
        self._crashed = True
        self.paused = True                       # stop re-running the faulting instruction
        needs_bios = self._needs_bios(summ)
        try:
            path = self._write_crash_report(summ, needs_bios)
        except Exception:
            path = None
        if needs_bios:
            msg = cfg.tr(cfg.language(self._settings), "crash_needs_bios")
        else:
            name, _desc = _STATUS_DESC.get(summ.stop_status, ("STATUS_%d" % summ.stop_status, ""))
            msg = f"⚠ ROM crashed — {name}"
        if path is not None:
            msg += f"\nreport: {path.name}"
        self.overlay.setText(msg)

    def _write_crash_report(self, summ, needs_bios: bool = False) -> Path | None:
        m = self.machine
        if m is None or self._rom_path is None:
            return None
        CRASH_DIR.mkdir(exist_ok=True)
        cpu = m.cpu()
        name, desc = _STATUS_DESC.get(summ.stop_status, ("STATUS_%d" % summ.stop_status, "?"))
        pc = summ.stop_pc
        L = []
        L.append("NgpCraft Emulator — crash report")
        L.append(f"time      : {datetime.now():%Y-%m-%d %H:%M:%S}")
        L.append(f"rom       : {self._rom_path.name}")
        L.append(f"reason    : {name} (status {summ.stop_status}) — {desc}")
        if needs_bios:
            L.append("likely    : NO BIOS LOADED — this game calls the NGPC BIOS through the")
            L.append("            vector table at 0xFFFE00, which is empty without a bios.bin.")
            L.append("            Add one in Settings ▸ BIOS. Most homebrew need the BIOS to run.")
        L.append(f"pc        : {pc:06X}")
        L.append(f"opcode    : {summ.stop_opcode:02X}")
        L.append(f"frame     : {summ.frame_count}   scanline: {summ.scanline}"
                 f"   cycles: {summ.total_cycles}")
        # ⚠️ EVERY KNOB THE MACHINE WAS GIVEN, or the line lies by omission. This block
        # exists so a crash can be re-run under the timing that produced it, and `ldirw`
        # is now a SEPARATE answer from `ldir` (a word iteration moves two bytes -- see
        # cfg.CART_LDIRW_COST). Reporting one and not the other reproduces another run.
        L.append(f"timing    : silicon word={cfg.CART_FETCH_WAIT} bios={cfg.CART_BIOS_WAIT}"
                 f" data={cfg.CART_DATA_WAIT} (states x2, per-word fetch, pipelined BIU,"
                 f" 4-byte queue, two-stage TX)   real_bios={self._real_bios}")
        L.append("")
        # registers
        L.append("registers (32-bit):")
        for i, rn in enumerate(_REG_NAMES):
            L.append(f"  {rn} = {cpu.regs[i]:08X}")
        L.append(f"  PC = {cpu.pc:06X}   SR = {cpu.sr_raw:04X}   F = {cpu.flags:02X}")
        L.append("")
        # bytes at PC
        raw = m.read(max(0, pc - 8) & 0xFFFFFF, 32)
        L.append(f"bytes @ pc-8 : {' '.join(f'{b:02X}' for b in raw)}")
        L.append("")
        # memory windows: around PC and around the stack pointer
        def dump(label, base, length):
            base &= 0xFFFFF0
            data = m.read(base & 0xFFFFFF, length)
            L.append(label)
            for r in range(length // 16):
                chunk = data[r * 16:(r + 1) * 16]
                hexs = " ".join(f"{b:02X}" for b in chunk)
                asc = "".join(chr(b) if 32 <= b < 127 else "." for b in chunk)
                L.append(f"  {base + r * 16:06X}  {hexs:<47}  {asc}")
        dump(f"memory @ pc ({pc:06X}):", pc - 0x10, 0x40)
        dump(f"stack @ xsp ({cpu.regs[7]:06X}):", cpu.regs[7], 0x40)
        text = "\n".join(L) + "\n"
        path = CRASH_DIR / f"{self._rom_path.stem}_{datetime.now():%Y%m%d_%H%M%S}_crash.txt"
        path.write_text(text, encoding="utf-8")
        return path

    # ---- screenshot -------------------------------------------------------
    def _scaler(self, rgb):
        """The plugin upscaler for the current filter setting, applied to one frame.

        Returns (picture, factor) -- (None, 0) when no plugin is selected or when the
        plugin just failed and was dropped. A filter that breaks costs the FILTER, never
        the frame: the game keeps running, unfiltered, and the panel says why."""
        ident = self._filter
        if not ident.startswith("plugin:"):
            return None, 0
        plug = FILTERS.get(ident)
        if plug is None:
            return None, 0
        out = FILTERS.run(ident, rgb)
        return (out, plug.scale) if out is not None else (None, 0)

    def screenshot(self) -> None:
        if self.machine is None:
            return
        folder = cfg.screenshot_dir(self._settings) or str(REPO / "screenshots")
        d = Path(folder); d.mkdir(parents=True, exist_ok=True)
        scale = max(2, min(6, self._scale or 4))
        pix = ngpc_video.render_pixmap(
            self.machine.framebuffer(), scale, self._filter, self._color, self._smooth,
            self._scaler)
        stem = self._rom_path.stem if self._rom_path else "ngpc"
        name = f"{stem}_{datetime.now():%Y%m%d_%H%M%S}.png"
        pix.save(str(d / name), "PNG")
        self._flash(cfg.tr(cfg.language(self._settings), "shot_saved").format(name=name))

    # ---- emulation speed --------------------------------------------------
    def cycle_speed(self, up: bool) -> None:
        steps = [0.25, 0.5, 1.0, 2.0, 4.0]
        try:
            i = steps.index(self._speed)
        except ValueError:
            i = 2
        i = max(0, min(len(steps) - 1, i + (1 if up else -1)))
        self._link_both(lambda pg: pg._apply_speed(steps[i]))    # noqa: SLF001

    def open_menu(self) -> None:
        if self.machine is None:
            return
        self.paused = True
        self._pause_peer(True)         # local 2P: the other console waits for us
        self._menu_open = True
        lang = cfg.language(self._settings)
        t = lambda key: cfg.tr(lang, key)
        self.menu.set_items(t("menu_title"), [
            ("resume", t("m_resume")), ("reset", t("m_reset")),
            ("savestate", t("m_savestate").format(n=self._slot + 1)),
            ("loadstate", t("m_loadstate").format(n=self._slot + 1)),
            ("video", t("m_video")), ("audio", t("m_audio")),
            ("controls", t("m_controls")), ("debug", t("m_debug")),
            ("quit", t("m_quit"))])
        self.menu.show_over()

    def close_menu(self) -> None:
        self.menu.hide()
        self._menu_open = False
        if self.machine is not None:
            self.paused = False
            self._pause_peer(False)
            self.setFocus()

    def _on_menu_choice(self, action: str) -> None:
        if action == "resume":
            self.close_menu()
        elif action == "reset":
            self._do_reset(); self.close_menu()
        elif action == "savestate":
            self.save_state(); self.close_menu()
        elif action == "loadstate":
            self.load_state(); self.close_menu()
        elif action in ("video", "audio", "controls"):
            self.menu.hide(); self._menu_open = False   # stay paused & alive
            self.options_requested.emit(action)
        elif action == "debug":
            self.menu.hide(); self._menu_open = False
            self.debug_requested.emit()
        elif action == "quit":
            self.menu.hide(); self._menu_open = False
            self.exit_requested.emit()

    def suspend(self) -> None:
        """Leave the game running-but-paused and alive (navigating to a menu)."""
        if self.machine is None:
            return
        self.paused = True
        self._pause_peer(True)         # local 2P: it must not play on without us
        self.timer.stop()
        self._commit_playtime()
        if self.sink is not None:
            self.sink.stop(); self.sink = None; self.audio = None
        self.pending.clear()

    def resume_play(self) -> None:
        if self.machine is None:
            return
        self.menu.hide(); self._menu_open = False
        self.paused = False
        # ⚠️ THE PEER MUST BE WOKEN HERE TOO, and this is the path that is easy to
        # miss: `_on_menu_choice` leaves the pause menu for Video/Audio/Controls
        # WITHOUT going through close_menu -- it stays paused on purpose and comes
        # back through resume_play. Pausing the peer in open_menu and forgetting it
        # here left player 2 frozen for the rest of the session.
        self._pause_peer(False)
        self._play_t0 = time.perf_counter()   # the clock restarts (see _commit_playtime)
        self.apply_settings()
        if cfg.audio_enabled(self._settings) and self.sink is None:
            self._open_audio()
        self.setFocus()
        self.wall_last = time.perf_counter(); self.debt = 0.0
        if not self.timer.isActive():
            self.timer.start(4)

    def resizeEvent(self, e) -> None:  # type: ignore[override]
        if self._menu_open:
            self.menu.setGeometry(0, 0, self.width(), self.height())
            self.menu._center()  # noqa: SLF001
        # Follow the window: rescale the frame to the new canvas size. When running,
        # the next _tick() re-blits anyway; this keeps it live while paused/dragging.
        # The immediate blit reads the size we have NOW (mid-resize the child may still
        # be stale); the deferred one re-fits once the layout has settled, which is what
        # makes a paused game come out right after a fullscreen or a maximise.
        if self.machine is not None:
            self._blit()
            self._reblit_soon()
        if not self.toolbar.isVisible():
            self._position_bar_show()
        self._place_rewind_bar()
        super().resizeEvent(e)

    def keyReleaseEvent(self, e: QKeyEvent) -> None:  # noqa: N802
        action = self._hotkeys.get(e.key())
        if action is not None:
            if action in cfg.HOLD_HOTKEYS and not e.isAutoRepeat():
                self._release_actions()[action]()
            return
        bit = self._bindings.get(e.key())
        if bit and not e.isAutoRepeat():
            self.held &= ~bit

    # ---- loop (pacing mirrors scripts/play.py)
    def _backlog(self) -> int:
        held = 0
        if self.sink is not None:
            held = max(0, self.sink.bufferSize() - self.sink.bytesFree())
        return (len(self.pending) + held) // 4

    def _frames_due(self) -> int:
        # At exactly 1x with audio, stay locked to the audio clock (no drift, no crackle).
        if self.audio is not None and self._speed == 1.0 and not self._ff:
            return self.pacer.frames_to_run(self._backlog())
        # Otherwise pace off the wall clock and scale by speed / fast-forward.
        mult = 8.0 if self._ff else self._speed
        now = time.perf_counter()
        self.debt += (now - self.wall_last) * 60.0 * mult
        self.wall_last = now
        due = int(self.debt); self.debt -= due
        cap = self.pacer.max_frames_per_tick * (8 if (self._ff or self._speed > 1) else 1)
        return min(due, cap)

    def _on_breakpoint(self, pc: int) -> bool:
        """A core PC breakpoint fired. Pause if its guard holds; otherwise step past it
        so the frame can continue. Returns True when we paused (caller must stop)."""
        bp = self.breaks.at(pc)
        if bp is not None and bp.cond_true(self.machine, self.symbols):
            self.paused = True
            self._bp_step_off = True         # step off it when the user resumes
            where = f"{pc:06X}" + (f"  [{bp.cond}]" if bp.cond else "")
            if bp.error:
                where += f"  ⚠ {bp.error}"     # fired because the guard is broken
            self.overlay.setText(f"⏸ breakpoint — {where}")
            self._blit()
            return True
        self._step_off_breakpoint()          # stale, or guard false -> move past it
        return False

    def _step_off_breakpoint(self) -> None:
        """Execute the one instruction we are parked on with breakpoints disabled, so
        we do not re-trigger the same address immediately."""
        pcs = self.breaks.enabled_pcs()
        try:
            self.machine.set_breakpoints([])
            self.machine.run(1, record=False)
        except Exception:
            pass
        finally:
            try:
                self.machine.set_breakpoints(pcs)
            except Exception:
                pass

    def _check_read_break(self) -> bool:
        """After a frame, see if anything READ a watched address; pause on the first,
        naming the PC that read it. The mirror of `_check_write_break` -- note that
        instruction fetches are not logged by the core, so this only fires on a real
        data read, not on the CPU walking over the address as code."""
        try:
            if self.machine.read_log_count() == 0:
                return False
            recs = self.machine.read_log()
        except Exception:
            return False
        for rec in recs:
            w = self.watches.read_hit(rec.addr)
            if w is not None:
                self.paused = True
                who = w.name or f"{w.addr:06X}"
                self.overlay.setText(
                    f"⏸ watchpoint R — {who} read ={rec.value:02X} by PC {rec.pc:06X}")
                self._blit()
                return True
        return False

    def _arm_capture(self, probing: bool, wrange, rrange) -> None:
        """Open a fresh capture window for the frame about to run.

        Called once per frame by whoever is about to RUN that frame: this page's own
        tick, or -- in a local 2-player link -- the peer that drives it. Arming resets
        the core's log, so it must happen before the frame and nowhere else.
        """
        if wrange is not None:               # fresh per-frame write capture
            self.machine.set_write_log(wrange[0], wrange[1])
        if rrange is not None:               # ...and read capture
            self.machine.set_read_log(rrange[0], rrange[1])
        if probing:                          # fresh window per frame for the viewer
            self.machine.set_write_log(*self.access_probe)
            self.machine.set_read_log(*self.access_probe)

    def _capture_windows(self):
        """(probing, write range, read range) for this page's next frame.

        While the access probe owns the logs, watchpoints are suspended (see
        apply_debug) -- so do not try to match against a window it is not watching.
        """
        probing = self.access_probe is not None
        return (probing,
                None if probing else self.watches.write_range(),
                None if probing else self.watches.read_range())

    def _check_write_break(self) -> bool:
        """After a frame, see if any 'write' watch's address was written; pause on the
        first, naming the PC that did it (from the core write-log)."""
        try:
            if self.machine.write_log_count() == 0:
                return False
            recs = self.machine.write_log()
        except Exception:
            return False
        for rec in recs:
            w = self.watches.write_hit(rec.addr)
            if w is not None:
                self.paused = True
                who = w.name or f"{w.addr:06X}"
                self.overlay.setText(
                    f"⏸ watchpoint W — {who} written ={rec.value:02X} by PC {rec.pc:06X}")
                self._blit()
                return True
        return False

    # ---- controller + autofire
    def _poll_pad(self) -> None:
        """Refresh the controller mask. Cheap when no pad is plugged in -- XInputPad
        rate-limits the probe itself (querying an empty slot is slow)."""
        self._pad_held = self._pad.poll() if self._pad_on else 0

    def _joypad_byte(self) -> int:
        """What the console should see on 0xB0 for THIS frame: keyboard and
        controller merged, with the turbo-flagged buttons chopped into a press
        train. Only the low 7 bits are joypad -- 0x80 is POWER."""
        held = (self.held | self._pad_held) & 0x7F
        if self._turbo_mask:
            held = ngpc_input.apply_turbo(held, self._turbo_mask, self._frame, self._turbo_hz)
        return held & 0x7F

    # ---- input recording / replay -----------------------------------------
    # The whole feature hangs off ONE call site: the byte written to 0x00B0 once
    # per emulated frame. Recording taps it, replay replaces it. Nothing else in
    # the loop knows this exists, which is why it cannot desynchronise from the
    # emulation -- there is no second clock to drift against.
    def _movie_byte(self) -> int:
        """The controller byte this frame: live, recorded, or replayed."""
        if self.movie_play is not None:
            b = self.movie_play.next()
            if b is None:
                self.movie_ended = self.movie_play
                self.movie_play = None
                self._flash("▶ replay finished")
                return self._joypad_byte()      # the controller is yours again
            return b
        b = self._joypad_byte()
        if self.movie_rec is not None:
            self.movie_rec.record(b)
        return b

    def movie_start_recording(self, header: dict) -> bool:
        """Begin a recording from the machine as it stands right now.

        The starting state is captured HERE, not at the first frame: a recording
        that begins one frame late replays one frame out of step forever, and a
        one-frame drift is exactly the kind of difference that reads as an
        emulation bug rather than as a broken tool.
        """
        from core import movie as mv
        if self.machine is None or self._mirror_blocks():
            return False
        self.movie_play = None
        self.movie_rec = mv.Recorder(header, self._capture_state())
        self._flash("⏺ recording input")
        return True

    def movie_stop_recording(self):
        rec, self.movie_rec = self.movie_rec, None
        if rec is not None:
            self._flash(f"⏺ recorded {rec.frames} frames")
        return rec.movie if rec is not None else None

    def movie_play_back(self, movie) -> bool:
        """Replay a movie, applying its starting state first if it carries one."""
        if self.machine is None or self._mirror_blocks():
            return False
        from core import movie as mv
        self.movie_rec = None
        self.movie_ended = None
        if movie.state:
            self._apply_state(movie.state)
            self._blit()
        self.movie_play = mv.Player(movie)
        self.paused = False
        self._flash(f"▶ replaying {movie.frames} frames")
        return True

    def movie_stop(self) -> None:
        if self.movie_play is not None:
            self._flash("▶ replay stopped")
        self.movie_play = None
        self.movie_rec = None

    # ---- link cable (two-player) ------------------------------------------
    def _show_link_menu(self) -> None:
        """The 🔗 button: pick how to link a second console."""
        t = lambda k: cfg.tr(cfg.language(self._settings), k)
        m = QMenu(self)
        # A host/join still looking for its peer needs a way OUT of the menu it was
        # started from -- without one, "waiting for player 2" was a one-way door and
        # every other entry below silently did nothing. First, because that is what
        # the player came back here for.
        if self.net_pending:
            m.addAction(t("link_cancel_pending"), self.net_cancel_requested.emit)
            m.addSeparator()
        m.addAction(t("link_2p_local"), self.link_requested.emit)
        m.addAction(t("link_2p_framed"), self.link_frame_requested.emit)
        m.addSeparator()
        m.addAction(t("link_online_lobby"), self.net_lobby_requested.emit)
        m.addSeparator()
        m.addAction(t("link_host_direct"), self.net_host_requested.emit)
        m.addAction(t("link_join_direct"), self.net_join_requested.emit)
        m.addSeparator()
        # The second online mode, kept ALONGSIDE the cable one rather than replacing
        # it: mirror play is far faster on a slow link but demands the same cartridge,
        # save, BIOS and build on both sides. See core/netplay.py.
        m.addAction(t("link_mirror_host"), self.net_mirror_host_requested.emit)
        m.addAction(t("link_mirror_join"), self.net_mirror_join_requested.emit)
        m.exec(self._link_btn.mapToGlobal(self._link_btn.rect().bottomLeft()))

    def set_player(self, n: int) -> None:
        """Make this page player `n`: reload its input bindings + gamepad."""
        self._player = int(n)
        self.apply_settings()

    def attach_link(self, peer: "PlayPage") -> None:
        """Wire this console to `peer` over the emulated link cable. Each page
        relays its own transmitted bytes to the other's receive FIFO every frame
        (a byte pipe -- no shared clock, honouring the peer's RTS)."""
        self._link_peer = peer
        if self.machine is not None:
            self.machine.serial_set_enabled(True)
            self._power_cycle_for_link()

    # ⚡ PLUGGING A CABLE IN MEANS SWITCHING BOTH CONSOLES ON AFTERWARDS.
    #
    # ⛔ THE BUG THIS ENDS: "Samurai Shodown! 2 — does not detect the second player
    # and the VS Mode is impossible to be selected." MEASURED, two consoles + the
    # real BIOS: the game sends its first link packet (FC 01 00 30, through the BIOS
    # COM send at 0xFF2C36) at FRAME 5 of its own boot, and latches the answer. With
    # a peer it keeps talking (151 packets by frame 300) and VS PLAY opens; with no
    # answer it goes quiet for the rest of the session -- pressing A on VS PLAY then
    # does nothing at all, which is exactly what the tester saw.
    #
    # Arming the cable at frame 0 works; arming it at frame 30 or at frame 240 is
    # already too late, and no amount of waiting brings it back (measured across a
    # sweep: 298 bytes at arm-frame 0, ZERO at every later one). And the shell's own
    # flow is the late one -- you boot a game, THEN press the link button.
    #
    # 🔑 So the cable cannot be handed to a console that is already running. On real
    # hardware you plug the cable in and then turn the consoles on; do that. The
    # console power-cycles with the link already armed, and the game's frame-5 probe
    # finds its peer. PROVEN end to end: after this, player 1 reaches SELECT PLAYER
    # as PLAYER 1 and player 2 as PLAYER 2, 516 bytes crossing each way.
    def _power_cycle_for_link(self) -> None:
        if self.machine is None or self._frame == 0:
            return          # never ran a frame: the cable is there from its first one
        self.machine.serial_set_enabled(False)   # drop whatever the FIFOs held...
        self._do_reset()
        self.machine.serial_set_enabled(True)    # ...and plug in for the fresh boot
        # SAY SO. From the player's seat a game that jumps back to the SNK logo the
        # instant the second console appears is indistinguishable from a crash -- and
        # it has been reported as one. It is deliberate: the cable has to be in before
        # the game boots or the game never sees it.
        self._flash_overlay("link_rebooted")

    def detach_link(self) -> None:
        if self.machine is not None:
            self.machine.serial_set_enabled(False)
        self._link_peer = None
        # Frames run for us by a peer we no longer have would be collected as if the
        # console had advanced, and it has not.
        self._prerun.clear()

    def attach_net_link(self, net) -> None:
        """Wire this console to a REMOTE peer over a core.link.TcpLink (LAN/online).
        Only the local player controls this window; the cable carries the serial
        bytes over the socket. Serial is enabled by the TcpLink itself."""
        self._net_link = net
        if self.machine is not None:
            self.machine.serial_set_enabled(True)
            # Same reason as the local cable: a game that probed the link during its
            # own boot will never look again. See _power_cycle_for_link. Both ends do
            # this when the connection comes up, so both boot with the cable in.
            self._power_cycle_for_link()

    def detach_net_link(self) -> None:
        if self._net_link is not None:
            self._net_link.disconnect()
        self._net_link = None
        if self.machine is not None and self._link_peer is None:
            self.machine.serial_set_enabled(False)

    # ---- mirror netplay: the OTHER online mode (core/netplay.py) --------------
    # The cable mode above sends the cable's own bytes, so the game waits for the
    # network and slows down with it (measured: 0.57x speed at a 67 ms round trip).
    # Mirror mode instead runs BOTH consoles here and sends only the controller
    # bytes, so the cable is local and the latency is spent on input delay rather
    # than on speed. Both modes stay: this one is faster, the other one is the one
    # that works with a peer who has a different save, a different BIOS or a
    # different build.
    # How long the reason a mirror match ended stays on screen. Long: it is the only
    # thing the player can tell anybody afterwards, and a session can die in under a
    # second -- fast enough that a short flash is gone before it is read.
    MIRROR_ERROR_MS = 15_000

    def attach_mirror(self, session, peer_session) -> None:
        """`session` is a core.netplay.MirrorSession already wired to the transport;
        `peer_session` is the second console's NativeSession, kept so it can be
        closed with the link."""
        self._mirror = session
        self._mirror_peer_session = peer_session
        # A new session starts with a clean slate: leaving the previous one's verdict
        # set would end this one on its first frame, for a desync that is not its own.
        self._mirror_desynced = False
        self._mirror_waits = 0
        self._flash_overlay("mirror_ready")

    def _step_mirror(self):
        """One frame of mirror netplay, or None when the peer's input is not here.

        The session runs BOTH consoles and relays the local cable between them, so
        this returns the summary of OUR console's frame for the rest of _tick to use.
        Waiting is not a stall to hide: it is the network, and the player is told
        about it rather than left wondering why the picture stopped.
        """
        t = lambda k: cfg.tr(cfg.language(self._settings), k)
        # ⛔ A PEER THAT VANISHES MUST NOT LOOK LIKE A SLOW ONE. Without this the
        # session waits for an input that will never come, for ever, showing "waiting
        # for the other player" -- the same failure the cable mode had to fix (see
        # core.link.TcpLink.lost) and just as unreadable from the player's seat.
        # ⚡ A REASON THE PLAYER CAN ACTUALLY READ. These three lines are the only
        # account of why a match ended, and they were plain `setText`, so the next
        # `_flash` timer still counting down from "mirror ready" erased them a second
        # later. Flashed, and for long enough to read and repeat back.
        lost = getattr(self._mirror.pipe, "lost", None)
        if lost:
            self._flash(t("link_lost").format(why=lost), self.MIRROR_ERROR_MS)
            self.mirror_ended.emit(str(lost))
            return None
        result = self._mirror.step(self._joypad_byte())
        if result == "rejected":
            self._flash(t("mirror_refused").format(why=self._mirror.rejected),
                        self.MIRROR_ERROR_MS)
            self.mirror_ended.emit(str(self._mirror.rejected))
            return None
        if result == "waiting":
            self._mirror_waits += 1
            if self._mirror_waits > 30:          # half a second: say something
                self.overlay.setText(t("mirror_waiting"))
            return None
        if self._mirror_waits:
            self._mirror_waits = 0
            self.overlay.setText("")
        if self._mirror.desync_at is not None and not self._mirror_desynced:
            # Detected, never repaired: the two PCs now hold different consoles, and
            # pretending otherwise would let the match carry on as a fiction. The frame
            # itself DID run, so it is still counted -- only the session ends.
            self._mirror_desynced = True
            self._flash(t("mirror_desync"), self.MIRROR_ERROR_MS)
            self.mirror_ended.emit("desync")
        # The session already ran our console's frame; hand back a summary for the
        # crash/breakpoint checks the rest of _tick does.
        return self.machine.run(0, record=False)[0]

    def detach_mirror(self) -> None:
        if self._mirror is not None:
            _close_pipe(self._mirror.pipe)   # the pipe owns its transport
        self._mirror = None
        self._mirror_desynced = False
        self._mirror_waits = 0
        if self._mirror_peer_session is not None:
            try:
                self._mirror_peer_session.close()
            except Exception:
                pass
        self._mirror_peer_session = None

    def set_link_monitor(self, monitor) -> None:
        """Tap (or untap) this console's cable for the debugger's Link tab.

        The monitor has to reach whichever relay is actually carrying the bytes:
        a local 2P link is pumped here, but an online one is pumped inside the
        TcpLink/LobbyLink, so it needs the tap handed to it directly. `None`
        removes the tap everywhere.
        """
        self.link_monitor = monitor
        if self._net_link is not None:
            self._net_link.monitor = monitor
        # ...and in mirror play the cable is the IN-PROCESS one between the two local
        # consoles, pumped by the session rather than by _pump_link. Without this the
        # Link tab would read zero bytes on a cable that is busy -- and a debug tool
        # that reads zero on a working link is worse than no tool.
        if self._mirror is not None:
            self._mirror.link.monitor_a = monitor

    def link_mode(self) -> str:
        """Which cable, if any, this console is on -- for the debugger to show."""
        if self._net_link is not None:
            return {"TcpLink": "lan", "LobbyLink": "lobby",
                    "LoopbackLink": "loopback"}.get(
                        type(self._net_link).__name__, "net")
        if self._mirror is not None:
            return "mirror"
        if self._link_peer is not None:
            return "local2p"
        return "none"

    # ⚠️ WHAT SLICING COSTS -- the other half of the LINK_SLICE story (see _run_frame_relaying
    # for why we slice at all: The Last Blade's handshake does not survive one relay a frame).
    #
    # Measured before that was understood (Fatal Fury link match, two consoles, the game's own
    # logic counter 0x4B3C per emulated frame, through this shell):
    #
    #     cable delay      relay once per frame     relay every ~2000 instructions
    #     none                    1.000                        0.957
    #     1 frame                 0.800                        0.733
    #     2 frames                0.573                        0.553
    #
    # So slicing is not free, and this table was once read as "slicing never helps". That
    # reading was wrong -- it only measured SPEED, and the thing slicing buys is CORRECTNESS
    # (a handshake that completes at all). Keep both facts: slice, but do not slice finer
    # than the handshake needs, and never slice a console with no peer.
    #
    # Half of the intuition here IS right and still applies: each page pumps after ITS OWN
    # frame and the two tick one after the other, so player 1's bytes reach player 2 before
    # player 2 runs. The asymmetry is the OTHER direction -- player 2's answer waits for the
    # next frame -- and that is the frame The Last Blade times out on.
    #
    # (A bench that steps both consoles and THEN pumps once invents a frame of latency that
    # the shell does not have. Measure through this path, not through such a bench.)
    #
    # What the table does say is that latency is taken out of the GAME's speed and
    # nothing else -- 0.57 at two frames of it. That is the "combat in slow motion,
    # audio perfect" report: audio comes from the APU, which waits for nobody. Online,
    # that latency is the network's, and no relay schedule of ours can remove it.
    def _flash_overlay(self, key: str, ms: int = 2500) -> None:
        """A translated `_flash` -- same owned-timer safety, see _flash."""
        self._flash(cfg.tr(cfg.language(self._settings), key), ms)


    # How many instructions a linked console runs between two relays of the cable.
    # ⚠️ THIS NUMBER IS A CORRECTNESS FIGURE, NOT A TUNING KNOB. A whole frame between
    # relays costs one frame of latency in ONE direction only (player 1 runs, we relay,
    # player 2 runs and can consume those bytes the same frame -- player 1 only sees the
    # answer next frame). The Last Blade's link handshake times out on exactly that:
    # measured on the game's own "message received" byte 0x4B9D, the console that speaks
    # first waits +0 -> +6 for the reply and gives up at +6, one frame short.
    #    slice     | message received | consumed | link driver at +900
    #    whole frame|  P2 only, +6     | NEVER    | dead (0xFF) -> "LINK ERROR"
    #    2000 instr |  P2 only, +8     | NEVER    | dead (0xFF)
    #    400 instr  |  BOTH at +4      | +6       | alive (0x14), both in Player Select
    #    100 instr  |  BOTH at +4      | +6       | alive (0x14)
    # 2000 was tried first and judged on the final screen alone -- it fails, which is why
    # an earlier attempt at this concluded "sub-frame relaying does not help". It does;
    # the slice was simply too coarse. Do not raise it without re-running that table.
    # Shared with mirror netplay, which owns two consoles for the same reason.
    #
    # ⛔ AND `ngpc_set_serial_break` DOES NOT REPLACE IT. MEASURED 2026-08-14, after a
    # perf report ("link games run about half speed vs hardware") sent us looking here.
    # The temptation is obvious: the core knows the exact cycle a byte hits the wire, so
    # arm the break, drop the quota, and stop guessing. It is wrong, because THIS NUMBER
    # DOES TWO JOBS and the break only does one of them:
    #     1. relay the cable promptly            -- the break is strictly better
    #     2. advance BOTH consoles in small steps -- the break cannot do this at all
    # Job 2 is what the table above measures. The break fires on OUR transmit and OUR RTS;
    # a console that is quietly computing produces no event, so it runs until the frame
    # ends -- and the other console has not run at all meanwhile. Measured step size per
    # `run()` call, same harness, 120 frames:
    #     slice 400   : every step 400 instructions, 0 of 4901 steps >= 0.8 frame
    #     break, free : median step 6263 (A WHOLE FRAME), 242 of 484 steps >= 0.8 frame
    # That is precisely the "a frame each, then relay" scheduling this class exists to
    # replace, and which the CFC bench scores 2 of 5 against 5 of 5.
    #
    # Keeping the cap AND arming the break (stop at 400, or earlier on a byte) is safe but
    # buys nothing: 20 544 FFI calls against 19 164, the same wall time to two decimals,
    # and cable throughput was already identical (1.7 bytes/frame either way on the probe
    # ROM). The relay was never the bottleneck -- two consoles cost 2x by construction and
    # the slicing on top is 6%, leaving ~880 emulated fps here, 14.7x realtime.
    LINK_SLICE = core_link.CABLE_SLICE

    # How far ahead of its own tick a linked peer may be driven. A pacer hands out up
    # to a few frames at once (PlayPage._frames_due), and every one of those has to be
    # interleaved or the peer is frozen for it. Past this the peer is not keeping up
    # and the plain path takes over. See _run_frame_interleaved.
    PRERUN_MAX = 8

    def _run_frame_relaying(self):
        """Run one frame, relaying the cable every `LINK_SLICE` instructions.

        Unlinked consoles keep the plain one-call-per-frame path: slicing costs a few
        percent of speed and buys nothing when there is no peer to hear the bytes.
        """
        if self._net_link is None and self._link_peer is None:
            summ = self.machine.run_frames(1)
            self._pump_link()
            return summ
        if self._prerun:
            # Our peer already ran this frame for us, interleaved with its own. Collect
            # it -- running again here would advance this console twice.
            return self._prerun.popleft()
        peer = self._link_peer
        # Only drive a peer that is actually free to run this frame. A paused or
        # rewinding console must stay where the user put it, and one that has not yet
        # collected the frame we ran for it must not be advanced twice.
        if (peer is not None and peer.machine is not None and not peer.paused
                and not peer._rewinding
                and len(peer._prerun) < self.PRERUN_MAX):        # noqa: SLF001
            return self._run_frame_interleaved(peer)
        # Probe the core's own counter rather than trusting `_core_frames`: a rewind or a
        # savestate can move the core without the page's copy following.
        start = self.machine.run(0, record=False)[0].frame_count
        total = 0
        for _ in range(256):
            summ, _ = self.machine.run(self.LINK_SLICE, record=False)
            total += summ.executed
            self._pump_link()
            if summ.stop_status in _CRASH_STATUSES:
                break
            if summ.stop_status == native.STATUS_BREAKPOINT:
                break
            if summ.executed == 0:               # core refused to advance
                break
            if summ.frame_count != start:        # the frame is done
                break
        else:
            # Never leave a frame half-run: whatever happened, finish it the plain way.
            summ = self.machine.run_frames(1)
            total += summ.executed
            self._pump_link()
        summ.executed = total
        return summ

    # ⚡ TWO CONSOLES ON ONE PC MUST ADVANCE TOGETHER, NOT ONE WHOLE FRAME AT A TIME.
    #
    # Each page used to run its own frame end to end while the other console stood
    # frozen. Relaying the cable mid-frame (LINK_SLICE) fixed one direction: player 1's
    # bytes reach player 2 before player 2 runs, so player 2 can answer in the same
    # frame. The OTHER direction stayed a whole frame late -- player 1 cannot see that
    # answer until its next frame, because it has already finished this one.
    #
    # ⛔ THE BUG THIS ENDS: "select exhibition, both press A, and the game will not
    # continue -- the host can back out with B, the other player cannot do anything."
    # MEASURED end to end (Card Fighters' Clash, SNK + Capcom carts, from the VS menu):
    # the HP exchange that starts a match dies after 118/102 bytes, one console showing
    # "LINK ERROR. CHECK CONNECTIONS AND SETTINGS." and the other waiting at "CHOOSING
    # HP." for ever. The game side is not subtle: its packet reader (0x24277C) polls
    # COMGETDATA a few dozen times and, if the next byte is not in the BIOS ring yet,
    # jumps to 0x242735 -> 0x242759, which raises its error flag. One frame of one-sided
    # latency is enough to lose that race.
    #
    # PROVEN, same bench, only this scheduling changed, sweeping an arbitrary
    # instruction-level phase offset between the two consoles (0/97/1000/5000/20000):
    #     a frame each, then relay   -> 2 runs of 5 reach the match
    #     interleaved by LINK_SLICE  -> 5 of 5, every time
    # and with it the match actually starts (both consoles at SHUFFLE PILE, 428/482
    # bytes and climbing) -- which no CFC link had ever reached here.
    #
    # 🔑 AND IT HAS TO SURVIVE A LUMPY PACER. `_frames_due()` is not 1 every tick: the
    # audio clock (player 1) and the wall clock (player 2) hand out 0 frames for a
    # while and then 3. Driving the peer for only the FIRST frame of such a batch
    # leaves it frozen for the other two -- the very gap this exists to close.
    # MEASURED with the same CFC bench, one page running its frames in batches:
    #     batch of 1 or 2 -> the match starts;  batch of 3 or 4 -> LINK ERROR again,
    # and the tester's report of the batched case is "it gets further now, but it
    # blocks on the last screen before the match, CHOOSE FIRST PLAYER".
    # So the peer's ready-run frames QUEUE (`_prerun`) instead of occupying one slot,
    # and the driver keeps interleaving until the queue is PRERUN_MAX deep -- past
    # that the peer is simply not keeping up, and falling back to the plain path is
    # better than running it further and further ahead of its own tick.
    #
    # ⚠️ Online this cannot be done: the peer is on another PC, and its answer is a
    # network round trip away. How much of that a game survives is its own business --
    # MEASURED for CFC's VS handshake, with a smooth (not frame-bunched) delay on the
    # wire: fine up to ~60 ms one way, dead by ~77 ms. So a cable-relayed CFC match
    # wants a good connection, and mirror play (core/netplay.py) is the mode that does
    # not care at all.
    # ⚠️ Beware of measuring this with a delay quantised to whole FRAMES: that delivers
    # in bursts and dies far earlier, which is how "CFC tolerates no latency" was once
    # concluded. It measures the burst, not the latency.
    def _run_frame_interleaved(self, peer: "PlayPage"):
        """Advance this console AND its local peer by one frame, a slice at a time."""
        # The peer's own tick writes its controller before ITS frame; that frame is
        # about to happen here instead, so give it the freshest pad state rather than
        # the one left over from last time.
        peer.machine.write(0x00B0, bytes([peer._joypad_byte()]))   # noqa: SLF001
        # ...and its debug capture, for the same reason: this frame is the peer's, so
        # its watchpoints and its access probe have to be armed for it HERE. Its own
        # tick cannot do it -- by the time it runs, the frame is over.
        peer._arm_capture(*peer._capture_windows())                # noqa: SLF001

        # ⚡ THE PAIR IS THE CORE'S JOB NOW. Below this line is the arrangement it
        # replaces: a slice of INSTRUCTIONS each, with the relay done from here between
        # slices. An instruction count is not cable time -- the core paces the pair on
        # the cable's own byte time and relays the moment either console reports a byte
        # moved, so the two can never be more than one quantum of emulated time apart.
        # See ngpc_run_linked and LINK_NETPLAY_STUDY.md L3.
        #
        # ⛔ EXCEPT WHEN SOMETHING IS SITTING IN THE WIRE. The debugger's link monitor
        # records every byte and can deliberately delay, drop or inject them; it is a
        # synthetic tool by design and it lives in Python, so a relay inside the core
        # cannot call it. An armed monitor therefore keeps the host-side path, which is
        # the behaviour it was written against. Same for a net link: that relay goes to
        # a socket, not to the peer beside it.
        if (not core_link.host_relay_forced()
                and self.link_monitor is None and peer.link_monitor is None
                and self._net_link is None and peer._net_link is None):   # noqa: SLF001
            summ_a, summ_b = native.run_linked(self.machine, peer.machine, 1)
            peer._prerun.append(summ_b)                            # noqa: SLF001
            # ⛔ THE BYTE COUNTER HAS TO COME FROM SOMEWHERE ELSE NOW. It used to be
            # totted up by _pump_link, which this path does not call -- so the two-player
            # window title would have sat at "P1->0 B  P2->0 B" for ever, which reads as
            # a dead cable rather than as a counter that stopped being fed. The core has
            # the honest number: bytes that actually finished shifting out onto the wire.
            self._link_tx_total = self.machine.serial_state().wire_count
            peer._link_tx_total = peer.machine.serial_state().wire_count   # noqa: SLF001
            return summ_a

        pages = (self, peer)
        starts = [p.machine.run(0, record=False)[0].frame_count for p in pages]
        totals = [0, 0]
        summaries = [None, None]
        done = [False, False]
        for _ in range(256):
            for i, p in enumerate(pages):
                if done[i]:
                    continue
                summ, _ = p.machine.run(p.LINK_SLICE, record=False)
                summaries[i] = summ
                totals[i] += summ.executed
                if (summ.stop_status in _CRASH_STATUSES
                        or summ.stop_status == native.STATUS_BREAKPOINT
                        or summ.executed == 0                 # core refused to advance
                        or summ.frame_count != starts[i]):    # this console's frame is done
                    done[i] = True
            for p in pages:
                p._pump_link()                    # noqa: SLF001 -- same class
            if all(done):
                break
        for i, p in enumerate(pages):
            if summaries[i] is None or not done[i]:
                # Never leave a frame half-run: finish it the plain way.
                summaries[i] = p.machine.run_frames(1)
                totals[i] += summaries[i].executed
                p._pump_link()                    # noqa: SLF001
            summaries[i].executed = totals[i]
        # The peer's frame is spent; its own tick collects it instead of running again.
        peer._prerun.append(summaries[1])         # noqa: SLF001
        return summaries[0]

    def _pump_link(self) -> None:
        mon = self.link_monitor
        if mon is not None:
            mon.frame = self._frame              # stamp this pump's log entries
        if self._net_link is not None:           # online: relay over the socket
            self._net_link.pump()                # (the net link carries the monitor)
            self._link_tx_total = self._net_link.bytes_out
            # A peer can vanish mid-game -- someone quits, a laptop sleeps, a NAT
            # forgets the mapping. The link records that instead of raising it (see
            # core.link.TcpLink.lost, and what raising it here used to do to the
            # process), so this is where the game stops being told to talk to a
            # console that is no longer there.
            lost = getattr(self._net_link, "lost", None)
            if lost:
                self.net_link_lost.emit(str(lost))
            return
        peer = self._link_peer
        if peer is None or peer.machine is None or self.machine is None:
            # No peer at all: a monitor can still stand in for one, feeding the
            # console bytes the user typed by hand (fake peer, real receive path).
            if mon is not None and self.machine is not None:
                link_debug.deliver_injected(self.machine, mon)
            return
        # Cross-wire the CTS0 handshake pin: our CTS0 is the PEER's RTS line (the
        # datasheet's "RTS is any GPIO -> the peer's CTS0"). With SC0MOD<CTSE> set,
        # the core holds our transmit until CTS0 is low (peer ready), so INTTX0 fires
        # only when the peer can receive -- what Card Fighters' Clash's mutual
        # handshake needs. serial_rts() is True when RTS is LOW (ready).
        self.machine.serial_set_cts(not peer.machine.serial_rts())
        # Drain our transmit FIFO into the peer's receive FIFO unconditionally: the
        # core's serial_tick is the authoritative flow-control gate -- it only
        # PRESENTS a queued byte to the peer's CPU once the peer's RTS is low, so
        # delivering early just queues it, exactly like a real cable. (Gating here
        # on the peer's RTS instead could strand a handshake byte and read as
        # "no cable".)
        data = self.machine.serial_read_tx()
        # The monitor watches (and may delay/drop) what leaves us; the PEER's
        # monitor is the one that records the arrival, since a monitor belongs to
        # a console, not to a wire. Called even on an empty drain so bytes held
        # back for a simulated latency get released.
        if mon is not None:
            data = mon.on_tx(data)
        if data:
            peer.machine.serial_write_rx(data)
            self._link_tx_total += len(data)
            if peer.link_monitor is not None:
                peer.link_monitor.on_rx(data)
        link_debug.deliver_injected(self.machine, mon)

    def _tick(self) -> None:
        if self.machine is None:
            return
        self._poll_pad()
        if self._rewinding:                  # held rewind: run the game BACKWARD
            self._rw_accum += 1
            if self._rw_accum >= 4:          # ~60 fps reverse (the timer ticks ~4 ms)
                self._rw_accum = 0
                # ⚡ IT SPEEDS UP THE LONGER YOU HOLD IT. At one frame per step this
                # ran backwards in real time, so reaching the far end of a 30-second
                # buffer took thirty seconds of holding a key -- which is not
                # rewinding, it is waiting. The first half-second stays at 1x so a
                # short tap still lands on the frame you meant; after that it ramps
                # to 8x, and the whole buffer is a couple of seconds away.
                self._rw_hold += 1
                steps = rewind_speed(self._rw_hold)
                popped = 0
                while popped < steps and len(self._rewind) > 1:
                    self._rewind.pop()       # drop the current frame...
                    popped += 1
                if popped:
                    self._apply_state(self._rewind[-1])   # ...show the one before it
                    self._drain_audio_silently()
                # The strip below the picture, not a frame count over it: `⏪ 137`
                # is a unit nobody thinks in, printed on the thing you are trying
                # to look at, and it never said the one thing that matters while
                # you hold the key -- how much history is LEFT.
                self._show_rewind_bar(speed=steps)
                self._blit()
            return
        if self.paused:
            # ⚡ THE STRIP IS REACHABLE WHILE PAUSED, and that is the whole point of
            # having a draggable one. It first appeared ONLY while rewinding -- so
            # to see it you had to hold the key, and while holding the key you
            # cannot drag it. A scrubber you can never grab is a decoration.
            self._show_rewind_bar()
            return
        if self._rw_pos is not None:         # resuming after a frame-step scrub
            self._leave_rewind()             # ...which puts the strip away itself
        elif self._rw_bar_timer is None or not self._rw_bar_timer.isActive():
            self.rewind_bar.setVisible(False)   # live, and the linger has expired
        if self._bp_step_off:                # resuming while parked on a breakpoint
            self._step_off_breakpoint()
            self._bp_step_off = False
        due = self._frames_due()
        if due == 0:
            self._drain_pending()     # keep feeding the card even when we run no frames,
            # The BIOS idles in its pre-boot shell loop with the PC frozen: the hand-off
            # must still count that idle even on a zero-frame tick, or a full audio cushion
            # (from the intro jingle) would keep the pacer at 0 and the hand-off never fires
            # -- the game would sit on the BIOS's blank post-intro screen forever.
            if self._real_bios and not self._did_handoff:
                self._bios_handoff_assist()
            # SAFETY NET: at 1x the pacer idles between frames (a few ticks), but if it
            # returns 0 for ~0.3 s the audio clock has stalled (a sink stuck underrun after
            # a long rewind pause). Reopen it so the loop can never stay frozen.
            self._stall_ticks += 1
            if self._stall_ticks > 75:
                self._stall_ticks = 0
                self._restart_audio()
            return
        self._stall_ticks = 0
        probing, wrange, rrange = self._capture_windows()
        locked = self.watches.locked()
        ran = 0
        emu_t0 = time.perf_counter()
        for _ in range(due):
            # ⚡ ONCE PER FRAME, not once per tick. This used to be written above the
            # loop, so every frame of a multi-frame batch saw one frozen joypad state.
            # That is invisible for a held button but fatal to autofire: the press
            # train would come out at the tick rate (and at whatever the batch size
            # happened to be) instead of the rate that was asked for.
            # In mirror mode the session owns the controller byte: it plays the one
            # captured `delay` frames ago, so both PCs feed the two consoles the same
            # streams. Writing the live pad here would fight it.
            # ⛔ ...UNLESS A LINK PEER ALREADY RAN THIS FRAME FOR US. Arming the capture
            # windows RESETS them, so doing it here would wipe what that frame recorded
            # a moment ago -- and the watchpoint check below would then read an empty
            # log every time. MEASURED before this guard: player 2's frame captured 801
            # writes inside player 1's tick, and player 2's own tick saw 0. The driver
            # arms them instead (see _run_frame_interleaved).
            already_ran = bool(self._prerun)
            if self._mirror is None and not already_ran:
                self.machine.write(0x00B0, bytes([self._movie_byte()]))
            self._frame += 1
            if not already_ran:
                self._arm_capture(probing, wrange, rrange)
            if self._mirror is not None:
                summ = self._step_mirror()
                if summ is None:
                    self._frame -= 1             # nothing ran; do not count this frame
                    break                        # the peer's buttons have not arrived
            else:
                summ = self._run_frame_relaying()
            # Console-side load. `executed` is the game's own work for this frame, and a
            # changed sprite table means the game completed a logic update -- a game that
            # has fallen behind updates on fewer frames than the LCD draws. Read straight
            # from OAM rather than the write log, which watchpoints already own.
            self._perf_instr = summ.executed
            oam = self.machine.read(0x008800, 64 * 4)
            if oam != self._perf_oam_prev:
                self._perf_oam_hits += 1
            self._perf_oam_prev = oam
            # ⚡ COUNT WHAT THE CORE ACTUALLY DREW, not what the pacer asked for. The
            # readout used to be handed `due`, and `due` is derived from the wall clock
            # (`debt += elapsed * 60`) -- so it reported ~60 by construction and could
            # never show the emulator falling behind, which is the one thing an fps
            # readout exists for. A frame cut short (breakpoint, crash) does not count.
            ran += max(0, summ.frame_count - self._core_frames)
            self._core_frames = summ.frame_count
            if self._real_bios and not self._did_handoff:
                self._bios_frames += 1        # gate the BIOS->cart hand-off on intro length
            if summ.stop_status in _CRASH_STATUSES and not self._crashed:
                self._on_crash(summ); return
            if summ.stop_status == native.STATUS_BREAKPOINT:
                if self._on_breakpoint(summ.stop_pc):
                    return                        # paused at a breakpoint whose guard held
            if wrange is not None and self._check_write_break():
                return
            if rrange is not None and self._check_read_break():
                return
            if self.watches.has_value_breaks():
                hit = self.watches.check(self.machine)
                if hit:
                    self.paused = True
                    self.overlay.setText(f"⏸ watchpoint — {hit}")
                    self._blit()
                    return
            for w in locked:                     # freeze: pin each locked value
                self.machine.write(w.addr, w.lock_bytes())
            # Cheats ride the SAME point in the frame as the locks above, on
            # purpose: a second place that also holds values would race this one,
            # and which won would depend on where in the frame each ran.
            self.cheats.apply(self.machine)
            for hook in self.frame_hooks:        # per-frame debug sampling, if subscribed
                try:
                    hook()
                except Exception:
                    pass                          # a debug tool must never kill the game
            if self._rewind_on:                  # frame-perfect rewind history
                self._rewind.append(self._capture_state())
            if self._song is not None:           # per-frame note capture (.ngps export)
                self._song.feed(self.machine.apu_state())
            # POWER-ON is handled INSIDE the core now: on the first HALT in BIOS space it
            # delivers the power NMI (once), which is how the real console boots. We must
            # NOT also press INT0 here: the BIOS halts AGAIN at the end of its intro (its
            # pre-boot idle), and pressing power there re-launches the intro in a loop --
            # exactly the "intro, reboot, intro, blank" the hand-off assist below is
            # waiting to resolve. Let it idle; _bios_handoff_assist takes it into the cart.
            if self.audio is not None:
                a = self.machine.audio()          # always drain the core's audio buffer
                self._last_audio = a              # feed the debug oscilloscope
                if self._speed == 1.0 and not self._ff:
                    self.pending += a             # ...but only queue it at real speed (mute FF/slow)
        self._drain_pending()
        if self._real_bios and not self._did_handoff:
            self._bios_handoff_assist()      # BIOS booted -> jump into the cartridge
        if self._vgm is not None:            # capturing music -> log this tick's PSG writes
            self._vgm.feed(self.machine.apu_write_count(), self.machine.apu_writes())
        self._perf_wall += time.perf_counter() - emu_t0
        self._perf_frames += ran
        self._blit()
        self._update_osd(ran)

    def perf(self) -> dict:
        """What the emulated CONSOLE is doing -- for the debug tools.

        `speed`    how many times faster than real time the core could run. This is the
                   honest "is the emulator coping" figure: the on-screen fps is pinned at
                   60 by the pacer on any machine fast enough, and says nothing.
        `game_fps` how often the GAME completes a logic update, from the sprite table
                   changing. 60 means it is keeping up; less means it is slowing down the
                   way it would on hardware. ⚠️ Reads 0 on a screen where nothing moves --
                   a menu or a title -- because there is genuinely nothing to update.
                   Counted per second of CONSOLE time, not of wall time: fast-forward runs
                   eight console seconds per real one, and the game's rate must not appear
                   to octuple because the player held Tab.
        `instr`    instructions the game executed in the last frame, out of a budget of
                   102 485 cycles. With cartridge wait-states on that is roughly 7 500 for
                   fetch-bound code; without them about three times more, which is why
                   emulators that skip the cart bus never show a game struggling.
        """
        now = time.perf_counter()
        dt = now - self._perf_t0
        if dt >= 0.5 and self._perf_frames:
            console_seconds = self._perf_frames / 60.0
            self._perf = {
                "speed": console_seconds / self._perf_wall if self._perf_wall else 0.0,
                "game_fps": self._perf_oam_hits / console_seconds,
                "instr": self._perf_instr,
            }
            self._perf_wall = 0.0; self._perf_frames = 0
            self._perf_oam_hits = 0; self._perf_t0 = now
        return self._perf

    def _update_osd(self, ran: int) -> None:
        if not self.osd.isVisible():
            return
        self._fps_frames += ran
        now = time.perf_counter()
        dt = now - self._fps_t0
        if dt >= 0.5:
            self._fps = self._fps_frames / dt
            self._fps_frames = 0; self._fps_t0 = now
            parts = [f"{self._fps:4.1f} fps"]
            if self._ff:
                parts.append("⏩")                       # fast-forward
            elif self._speed != 1.0:
                parts.append(f"{self._speed:g}x")
            self.osd.setText("  ".join(parts))
            self.osd.adjustSize()

    def _reblit_soon(self) -> None:
        """Re-fit the frame AFTER Qt has settled the new layout.

        Reading the LCD's size in the SAME turn a fullscreen / toolbar / sidebar change
        happens gives the size it had BEFORE the change: the layout has not run yet. The
        frame is then scaled to a stale box -- a wrong aspect, worst in Stretch -- and a
        PAUSED game never fixes it, because only a running `_tick` re-blits. Hiding the
        toolbar does not even resize this widget (only its inner layout shifts), so no
        resizeEvent fires to correct it either. Deferring one event-loop turn reads the
        box the layout actually produced. Coalesced so a window drag queues just one."""
        if self.machine is None or self._reblit_pending:
            return
        self._reblit_pending = True

        def _go() -> None:
            self._reblit_pending = False
            self._blit()
        QTimer.singleShot(0, _go)

    def _blit(self) -> None:
        if self.machine is None:
            return
        bw, bh = max(SCREEN_W, self.lcd.width()), max(SCREEN_H, self.lcd.height())
        # Render the filter/scanlines at the integer scale nearest the display box (so the
        # grid lines look right at this size), then fit_pixmap does the fractional remainder.
        k = max(1, min(bw // SCREEN_W, bh // SCREEN_H))
        k = min(k, 8)                                    # cap the numpy cost
        pix = ngpc_video.render_pixmap(
            self.machine.framebuffer(), k, self._filter, self._color, self._smooth,
            self._scaler)
        pix = ngpc_video.fit_pixmap(pix, bw, bh, self._aspect, self._smooth)
        self.lcd.setPixmap(pix)


# ---------------------------------------------------------------- shell
def _local_ip() -> str:
    """Best-effort LAN address to show a host (what a peer on the same network,
    or the same Tailscale/ZeroTier net, dials). No packet is sent -- connecting a
    UDP socket just resolves the default-route interface. Falls back to loopback."""
    import socket
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


class _Cancelled(Exception):
    """The player took the attempt back. Not a failure -- nothing to report."""


def _net_error_text(lang: str, exc: Exception, mode: str) -> str:
    """Turn a socket failure into a sentence that names a CAUSE, not an errno.

    ⛔ WHAT THIS REPLACES: `failed.emit(str(e))`, which put "[WinError 10061] No
    connection could be made because the target machine actively refused it" in
    front of a player. Every one of these cases has a different fix, and the
    number is the only part that does not say which.
    """
    import errno
    import socket
    key = None
    if isinstance(exc, socket.gaierror):
        key = "net_err_name"
    elif isinstance(exc, TimeoutError):
        key = "net_err_timeout_host" if mode == "host" else "net_err_timeout_join"
    elif isinstance(exc, ConnectionRefusedError):
        key = "net_err_refused"
    elif isinstance(exc, PermissionError):
        key = "net_err_denied"
    elif isinstance(exc, OSError) and exc.errno is not None:
        # ⚠️ `errno is not None` is not defensive noise: an OSError raised without
        # one would otherwise match the `None` that getattr leaves in these tuples
        # on a platform without the WSA aliases, and report "port already in use"
        # for something else entirely.
        in_use = {errno.EADDRINUSE, getattr(errno, "WSAEADDRINUSE", errno.EADDRINUSE)}
        if exc.errno in in_use:
            key = "net_err_in_use"
        elif exc.errno in (errno.EADDRNOTAVAIL, errno.EACCES):
            key = "net_err_denied"
        # ⛔ SIGNALE PAR UN JOUEUR : « link failed [WinError 10051] une operation a ete
        # tentee sur un reseau impossible a atteindre ». C'est WSAENETUNREACH, et il
        # tombait dans le `str(exc)` que cette fonction existe justement pour eviter --
        # le numero est la seule partie qui ne dit pas quoi faire.
        #
        # Cette famille-la a une cause tres precise et une seule: l'adresse composee
        # n'est PAS JOIGNABLE depuis ce PC. En pratique c'est presque toujours une
        # adresse de reseau local (192.168.x / 10.x) donnee par un joueur qui n'est pas
        # sur le meme reseau -- l'autre joueur a lu l'IP que sa box lui donne au lieu de
        # son IP publique. Un pare-feu ne donne pas ca (il donne un refus ou un delai).
        else:
            unreachable = {getattr(errno, n) for n in
                           ("ENETUNREACH", "EHOSTUNREACH", "ENETDOWN",
                            "WSAENETUNREACH", "WSAEHOSTUNREACH", "WSAENETDOWN")
                           if getattr(errno, n, None) is not None}
            if exc.errno in unreachable:
                key = "net_err_unreachable"
    if key is None:
        return cfg.tr(lang, "link_failed").format(why=str(exc))
    return cfg.tr(lang, "link_failed").format(why=cfg.tr(lang, key))


class _NetConnect(QThread):
    """Background accept (host) or connect (join) so the UI never blocks on the
    handshake. Emits the connected socket on success.

    ⛔ THE BUG THIS ENDS, reported as "direct host/client mode never seems to
    actually get through". Three faults compounded into one dead end:

      1. The host's `accept()` blocked FOREVER -- no timeout at all, while the
         joining side already had one.
      2. `cancel()` existed but was reachable from no menu: its only callers were
         peer-loss and app teardown, both of which assume a link that EXISTS. A
         pending attempt could not be taken back.
      3. So `_start_net`'s "one attempt at a time" guard stayed armed for the rest
         of the session, and every later Host/Join click returned in silence.

    The player's experience: click Host, nothing; click Join, nothing, without a
    word; restart the emulator. So the wait is now bounded, interruptible at any
    moment, and joining RETRIES -- because "the other player has not clicked Host
    yet" is the normal case, not an error, and it used to fail instantly with
    ECONNREFUSED the moment you were the first of the two to be ready.
    """

    connected = pyqtSignal(object)     # a connected socket.socket
    failed = pyqtSignal(str)           # already-translated, player-facing

    # ⛔ CINQ MINUTES N'ETAIENT PAS « GENEREUSES », C'ETAIT LA PANNE.
    #
    # Signale ainsi: « j'ai essaye de differentes facons, en LAN ou en ligne, les deux
    # IP, rien ne change: en attente de connexion..... ». Constate en regardant les
    # VRAIES sockets des deux instances qui tournaient (tools/watch_link_sockets.py):
    # **aucune ecoute**, alors que l'ecran affichait toujours « en attente ». L'attente
    # avait expire, et rien ne le disait.
    #
    # Deux joueurs qui montent une partie ne sont pas synchrones a la minute pres: on
    # clique Heberger, on va sur l'autre PC, on charge la ROM, on se met d'accord sur
    # Discord. Passe la 5e minute l'hote n'ecoute plus, et le pair qui arrive enfin
    # recoit un refus de connexion pendant que l'hote, lui, montre encore « en attente ».
    # Les deux joueurs voient alors deux pannes differentes de la meme non-panne.
    #
    # ⚡ Ce n'est pas une valeur a rallonger indefiniment: la tentative est ANNULABLE
    # (menu 🔗 et fermeture du panneau d'accueil -> `_cancel_net_attempt`), donc la
    # sortie est entre les mains du joueur, pas d'un chronometre. La demi-heure est ce
    # qui reste pour qu'un fil oublie ne survive pas a la session.
    HOST_TIMEOUT_S = 1800.0   # trente minutes -- et annulable a tout moment
    JOIN_TIMEOUT_S = 1800.0   # idem: le rejoignant retente tant que l'hote n'ecoute pas
    POLL_S = 0.25             # how fast a cancel is noticed

    def __init__(self, mode: str, host: str, port: int, lang: str = "en") -> None:
        super().__init__()
        import threading
        self._mode = mode
        self._host = host
        self._port = port
        self._lang = lang
        self._srv = None
        self._cancel = threading.Event()

    # ---- the two ways in ---------------------------------------------------
    def _accept(self):
        """Listen, then wait in POLL_S slices so a cancel lands within a quarter
        second instead of never."""
        import select
        import socket
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            srv.bind(("0.0.0.0", self._port))
            srv.listen(1)
            self._srv = srv
            deadline = time.monotonic() + self.HOST_TIMEOUT_S
            while not self._cancel.is_set():
                if time.monotonic() >= deadline:
                    raise TimeoutError("nobody connected")
                # ⚠️ THE ACCEPT BELONGS INSIDE THIS GUARD, and a test proved it: a
                # socket that cancel() closed under us is reported READABLE, so the
                # accept that follows raises WSAENOTSOCK. Guarding only the select
                # turned a cancel the player asked for into "⚠ link failed: [WinError
                # 10038]" -- an error message for doing exactly what was requested.
                try:
                    ready, _, _ = select.select([srv], [], [], self.POLL_S)
                    if ready:
                        if self._cancel.is_set():
                            break
                        conn, _ = srv.accept()
                        return conn
                except OSError:
                    if not self._cancel.is_set():
                        raise                  # a real failure, not our own cancel
                    break                      # cancel() closed the socket under us
            raise _Cancelled()
        finally:
            self._srv = None
            try:
                srv.close()
            except OSError:
                pass

    def _dial(self):
        """Connect, retrying while the host is not listening yet."""
        import socket
        deadline = time.monotonic() + self.JOIN_TIMEOUT_S
        last: Exception | None = None
        while not self._cancel.is_set():
            left = deadline - time.monotonic()
            if left <= 0:
                break
            try:
                return socket.create_connection((self._host, self._port),
                                                timeout=min(2.0, left))
            except socket.gaierror:
                raise                          # a name that does not resolve never will
            except OSError as e:
                last = e
            if self._cancel.wait(self.POLL_S):
                break
        if self._cancel.is_set():
            raise _Cancelled()
        raise last if last is not None else TimeoutError("could not connect")

    def run(self) -> None:
        import socket
        try:
            conn = self._accept() if self._mode == "host" else self._dial()
        except _Cancelled:
            return                             # the player asked; say nothing
        except Exception as e:  # noqa: BLE001 -- any failure is just "could not link"
            self.failed.emit(_net_error_text(self._lang, e, self._mode))
            return
        # ⚠️ A CANCEL AND A CONNECTION CAN CROSS. The peer may land in the instant
        # between the last `_cancel` check and here, and emitting then would attach
        # a link the player has just taken back. Losing the race means dropping the
        # socket, which is what "cancel" was asked to mean.
        if self._cancel.is_set():
            try:
                conn.close()
            except OSError:
                pass
            return
        conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        self.connected.emit(conn)

    def cancel(self) -> None:
        self._cancel.set()
        srv = self._srv                        # unblock a pending select() at once
        if srv is not None:
            try:
                srv.close()
            except OSError:
                pass


class _LinkInput(QObject):
    """Global key router for two-player link play. Two windows, one keyboard: the
    OS delivers keys only to the focused window, so this app-level filter routes
    each key to whichever player's bindings own it -- both play at once, whichever
    window is focused. Keys that are not a joypad binding (hotkeys, text) fall
    through untouched."""

    def __init__(self, p1: "PlayPage", p2: "PlayPage") -> None:
        super().__init__()
        self._pages = (p1, p2)

    def eventFilter(self, obj, e) -> bool:  # noqa: N802
        t = e.type()
        if t in (QEvent.Type.KeyPress, QEvent.Type.KeyRelease) \
                and isinstance(e, QKeyEvent) and not e.isAutoRepeat():
            press = t == QEvent.Type.KeyPress
            k = e.key()
            for pg in self._pages:
                bit = pg._bindings.get(k)     # noqa: SLF001 -- sibling in this module
                if bit:
                    if press:
                        pg.held |= bit
                    else:
                        pg.held &= ~bit
                    return True               # consumed: the focused page won't double-handle
        return False


LINK_WINDOW_GAP = 8          # px between the two consoles, so the edge is visible
LINK_WINDOW_CASCADE = 32     # offset when the screen cannot hold both side by side


def side_by_side(primary, available, *, gap=LINK_WINDOW_GAP, min_w=0, min_h=0):
    """Where two equal consoles go. Takes/returns (x, y, w, h) tuples.

    ⛔ WHAT THIS REPLACES. Player 2's window opened at a hard-coded 3x (480x496) at
    whatever position the window manager felt like -- which is on top of player 1.
    Bigger main window, smaller second one, sitting over it: "elle est souvent ouverte
    par defaut par dessus la premiere et plus petite".

    ⚖️ THE TWO RULES ARE NOT EQUAL, and the order is the whole design:
      1. THE CONSOLES ARE ALWAYS THE SAME SIZE. They are the same machine, and a
         two-player game where one player has the bigger screen is not a fair one.
      2. Neither hides the other -- side by side, WHEN THE SCREEN ALLOWS ("quand c'est
         possible"). When it cannot, they cascade: still equal, deliberately offset so
         the one underneath can be grabbed. Breaking rule 1 to satisfy rule 2 would
         re-create the exact complaint.

    `min_w`/`min_h` are the floor the WINDOWS impose, and they are not optional: a Qt
    window silently refuses to shrink below its own minimum, so a width computed without
    them is honoured by one window, ignored by the other, and the two end up different
    sizes again. MEASURED offscreen, before this argument existed: player 1 obeyed at
    400 px while player 2 stopped at its 730 px minimum. The caller passes the larger of
    the two windows' minimums, so whatever comes back is a size both can actually take.

    Pure geometry, no Qt: the screen is an argument, so the awkward cases (a narrow
    laptop, a second monitor at a negative x) are testable without owning one.
    """
    ax, ay, aw, ah = available
    px, py, w, h = primary

    h = max(min_h, min(h, ah))
    w = max(min_w, min(w, aw))
    room = aw - gap
    if 2 * w > room:                             # cannot stand side by side as-is
        shrunk = max(min_w, room // 2)
        if 2 * shrunk <= room:
            w = shrunk                           # both shrink, equally
        else:
            # The screen is narrower than two windows at their own minimum. Keep them
            # equal and cascade: overlapping is the honest outcome here, unequal is not.
            x = max(ax, min(px, ax + aw - w))
            y = max(ay, min(py, ay + ah - h))
            ox = max(ax, min(x + LINK_WINDOW_CASCADE, ax + aw - w))
            oy = max(ay, min(y + LINK_WINDOW_CASCADE, ay + ah - h))
            return (x, y, w, h), (ox, oy, w, h)

    total = 2 * w + gap
    x = min(px, ax + aw - total)                 # slide left until the pair fits...
    x = max(x, ax)                               # ...but never off the other edge
    y = min(py, ay + ah - h)
    y = max(y, ay)
    return (x, y, w, h), (x + w + gap, y, w, h)


class _Link2PWindow(QMainWindow):
    """Player 2's window: a second, full PlayPage (its own toolbar, save states,
    and -- crucially -- its own cartridge whose flash save loads normally)."""

    closed = pyqtSignal()

    def __init__(self, play, settings, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle(cfg.tr(cfg.language(settings), "window_player2"))
        # The page is built by the caller, so the separate-window and single-frame
        # modes wire player 2 identically and only differ in where it is shown.
        self.play = play
        self.setCentralWidget(self.play)
        self.resize(SCREEN_W * 3, SCREEN_H * 3 + 40)   # replaced by side_by_side()

    def closeEvent(self, e) -> None:          # noqa: N802
        self.play.stop()
        self.closed.emit()
        super().closeEvent(e)


class _Link2PFrame(QMainWindow):
    """Both consoles in ONE window, side by side and always the same size.

    Asked for as "un grand cadre qui gere la taille des deux en meme temps et les
    maintient a taille egale". Equal stretch factors in one row are exactly that: the
    layout owns both widths, so there is no size to keep in sync by hand and no way for
    them to drift apart -- resize the frame and both consoles follow, together.

    Player 1's page is BORROWED from the shell's stack for the session and handed back
    when the frame closes (see Shell._end_link_2p), so its cartridge, its save states
    and its audio keep running throughout -- nothing restarts.
    """

    closed = pyqtSignal()

    def __init__(self, p1, p2, settings, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle(cfg.tr(cfg.language(settings), "window_link_frame"))
        self.p1, self.play = p1, p2
        central = QWidget(); central.setObjectName("page")
        row = QHBoxLayout(central)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(LINK_WINDOW_GAP)
        row.addWidget(p1, 1)                  # 1 : 1 -- equal, whatever the frame does
        row.addWidget(p2, 1)
        self.setCentralWidget(central)
        # ⚠️ TAKING A PAGE OUT OF A QStackedWidget LEAVES IT HIDDEN, and a hidden widget
        # gets no room from a layout however its stretch factor reads. MEASURED on a
        # rendered frame: player 1 sat at its stale 640x480 while player 2 took the whole
        # 1200. The stretch factors were already right and the picture was still wrong --
        # which is why the test for this asserts the resulting WIDTHS, not the settings.
        p1.show()
        p2.show()
        # ⚖️ AND EQUAL STRETCH ONLY SHARES THE SURPLUS. Each page starts from its own
        # minimum, and the two are not the same -- player 2 hides the 🔗 button, so its
        # toolbar is narrower. MEASURED, again on the rendered frame: 767 against 726.
        # A common floor is what actually makes "maintient a taille egale" true; it is
        # put back when the frame closes so the shell does not keep an inflated minimum
        # for the rest of the session.
        self._p1_min_w = p1.minimumWidth()
        floor = max(p1.minimumSizeHint().width(), p2.minimumSizeHint().width())
        p1.setMinimumWidth(floor)
        p2.setMinimumWidth(floor)

    def closeEvent(self, e) -> None:          # noqa: N802
        # Only player 2 is ours to stop. Player 1 belongs to the shell and is about to
        # be put back in it -- stopping it here would end the session the player is
        # still in the middle of. Its minimum width is ours to give back, though: it was
        # widened to match player 2 and the shell would be stuck with it otherwise.
        self.p1.setMinimumWidth(self._p1_min_w)
        self.play.stop()
        self.closed.emit()
        super().closeEvent(e)


class Shell(QMainWindow):
    def __init__(self, rom: str | None = None) -> None:
        super().__init__()
        self._settings = cfg.make_settings()
        # Settings live in the registry by default, which has no undo. Keep a copy
        # beside the app and put it back when the store comes up empty -- see
        # `cfg.protect_settings`. Reported, never silent: settings reappearing on
        # their own is nearly as confusing as losing them.
        self._settings_recovered = cfg.protect_settings(self._settings)
        self.setWindowTitle("NgpCraft Emulator")
        if APP_ICON.is_file():
            self.setWindowIcon(QIcon(str(APP_ICON)))
        # Restore the last window size/position; fall back to a sensible default.
        geo = self._settings.value("win/geometry")
        if geo is not None:
            self.restoreGeometry(geo)
        else:
            self.resize(980, 660)
        # ⛔ NEVER come back FULLSCREEN. `saveGeometry` records the fullscreen bit, so
        # quitting from a fullscreen game used to reopen the whole shell fullscreen --
        # on the LIBRARY page, where nothing handled Escape and the sidebar was hidden
        # with it: no menu, no title bar, no way out but Alt+F4 (user report, 2026-08-02).
        # The game's own `gfx/fullscreen` preference is untouched and still applies when
        # a cartridge starts, which is the only place fullscreen has an exit.
        if self.isFullScreen():
            self.showNormal()
        self._apply_theme()

        central = QWidget(); central.setObjectName("page")
        root = QHBoxLayout(central); root.setContentsMargins(0, 0, 0, 0); root.setSpacing(0)

        # left rail (collapses to a thin strip that still holds the toggle)
        rail = QWidget(); rail.setObjectName("rail"); rail.setFixedWidth(RAIL_MIN_W)
        self._rail = rail
        self._rail_w = RAIL_MIN_W            # last width `_fit_rail` measured
        self._nav_text: dict[QPushButton, str] = {}   # button -> its raw label
        rlay = QVBoxLayout(rail); rlay.setContentsMargins(8, 8, 8, 12); rlay.setSpacing(4)
        head = QHBoxLayout(); head.setContentsMargins(0, 0, 0, 0)
        self._rail_title = QLabel("◆ NgpCraft"); self._rail_title.setObjectName("appTitle")
        self._rail_toggle = QPushButton("‹"); self._rail_toggle.setObjectName("railToggle")
        self._rail_toggle.setFixedSize(26, 26)
        self._rail_toggle.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._rail_toggle.clicked.connect(lambda: self._toggle_rail())
        head.addWidget(self._rail_title); head.addStretch(); head.addWidget(self._rail_toggle)
        rlay.addLayout(head)
        self._nav_resume = QPushButton(); self._nav_resume.setObjectName("rail")
        self._nav_resume.setCheckable(True)
        self._nav_resume.clicked.connect(lambda: self._go(2))
        self._nav_resume.hide()
        self._nav_lib = QPushButton(); self._nav_lib.setObjectName("rail"); self._nav_lib.setCheckable(True)
        self._nav_set = QPushButton(); self._nav_set.setObjectName("rail"); self._nav_set.setCheckable(True)
        self._nav_lib.clicked.connect(lambda: self._go(0))
        self._nav_set.clicked.connect(lambda: self._go(1))
        self._nav_dbg = QPushButton(); self._nav_dbg.setObjectName("rail")
        self._nav_dbg.clicked.connect(self._open_debug)
        rlay.addWidget(self._nav_resume)
        rlay.addWidget(self._nav_lib); rlay.addWidget(self._nav_set)
        rlay.addWidget(self._nav_dbg)
        rlay.addStretch()
        self._ver = QLabel("native core"); self._ver.setObjectName("hint")
        self._ver.setStyleSheet("padding:8px 12px; font-size:11px;")
        rlay.addWidget(self._ver)
        root.addWidget(rail)
        # everything except the toggle is hidden when the rail is collapsed
        self._rail_hideable = [self._rail_title, self._nav_resume, self._nav_lib,
                               self._nav_set, self._nav_dbg, self._ver]

        self._stack = QStackedWidget()
        # One shared play-history store: the library reads it to sort and the player
        # writes it as you play, so returning from a game shows fresh counts.
        self._library_db = lib.Library(LIBRARY_DB)
        self.library = LibraryPage(self._settings, self._library_db)
        self.settings = SettingsPage(self._settings)
        self.play = PlayPage(self._settings, self._library_db)
        self._stack.addWidget(self.library)
        self._stack.addWidget(self.settings)
        self._stack.addWidget(self.play)
        root.addWidget(self._stack, 1)
        self.setCentralWidget(central)

        self.library.play_requested.connect(self._launch)
        self.library.boot_bios_requested.connect(self._launch_bios)
        self.play.exit_requested.connect(self._to_library)
        self.play.options_requested.connect(self._play_options)
        self.play.debug_requested.connect(self._open_debug)
        self.play.link_requested.connect(self._launch_link_2p)
        self.play.link_frame_requested.connect(self._launch_link_2p_framed)
        self.play.net_host_requested.connect(self._host_online)
        self.play.net_join_requested.connect(self._join_online)
        self.play.net_lobby_requested.connect(self._open_lobby)
        self.play.net_cancel_requested.connect(self._cancel_net_attempt)
        self.play.net_link_lost.connect(self._on_net_link_lost)
        self.play.net_mirror_host_requested.connect(self._host_mirror)
        self.play.net_mirror_join_requested.connect(self._join_mirror)
        self.play.mirror_ended.connect(self._on_mirror_ended)
        self._mirror_pending = None        # "host"/"join" while a mirror link connects
        self._mirror_boot = None           # the cartridge trade, while it runs
        self._mirror_boot_host = False
        self._mirror_timer = None
        self._link2p = None                # the P2 window, when 2-player is active
        self._link_input = None            # the global key router, when active
        self._link_status = None           # P2 title-bar link diagnostic timer
        self._net_thread = None            # background accept/connect, when linking online
        self._net_retiring = []            # cancelled workers, held until they really stop
        self._net_status = None            # online link diagnostic timer
        self._net_clock = None             # « en attente » qui COMPTE, voir _start_net
        self._host_info = None             # the host connection-info dialog
        self.settings.changed.connect(self._on_settings_changed)
        self.settings.storage_changed.connect(self._on_storage_changed)
        self.settings.scale_changed.connect(self.play.set_window_scale)
        self.settings.language_changed.connect(self._retranslate)
        self.settings.theme_changed.connect(self._restyle)
        self.settings.resume_requested.connect(lambda: self._go(2))
        self._debug_win = None

        # Follow the OS live: on "system", flipping Windows to light repaints the
        # app immediately rather than at the next launch. Qt 6.5+; harmless if the
        # platform never emits it.
        try:
            QApplication.styleHints().colorSchemeChanged.connect(self._on_system_theme)
        except (AttributeError, RuntimeError):
            pass

        self._retranslate()
        # Long help/empty-state labels wrap instead of forcing the window at least as wide
        # as their longest sentence -- which is what kept the minimum window so big. Run
        # after retranslate so the text is set; short field labels are left alone. The nav
        # labels manage their own wrapping (see _fit_rail), so skip the rail.
        for lbl in self.findChildren(QLabel):
            if (lbl not in self._nav_text and lbl.pixmap() is None
                    and not lbl.wordWrap() and len(lbl.text()) > 40):
                lbl.setWordWrap(True)
        self.setMinimumSize(360, 320)
        self._go(0)
        self._auto_collapsed = False
        self._toggle_rail(not bool(self._settings.value("win/rail_collapsed", False, type=bool)))
        self.fit_to_screen()
        self._auto_rail()
        if rom:
            self._launch(rom)

    # ---- adapting to the screen we are actually on -----------------------
    def fit_to_screen(self) -> None:
        """Shrink/slide the window back inside the current screen's work area.

        Idempotent, and a no-op for a window that already fits -- so it can run on
        every show without ever fighting a size the user chose."""
        if self.isFullScreen() or self.isMaximized():
            return
        scr = self.screen() or QApplication.primaryScreen()
        if scr is None:
            return
        frame = self.frameGeometry()
        fitted = clamp_geometry(frame, scr.availableGeometry())
        if fitted == frame:
            return
        # The frame is what must fit (title bar + borders), the client area is what we
        # can resize -- before the window is shown the two are the same, after it they
        # differ by the decorations, so measure the difference instead of guessing it.
        inner = self.geometry()
        dw = frame.width() - inner.width()
        dh = frame.height() - inner.height()
        self.resize(max(self.minimumWidth(), fitted.width() - dw),
                    max(self.minimumHeight(), fitted.height() - dh))
        self.move(fitted.x() + (inner.x() - frame.x()),
                  fitted.y() + (inner.y() - frame.y()))

    def _auto_rail(self) -> None:
        """Fold the rail on a narrow window, unfold it when there is room again.

        Only ever undoes ITSELF: the saved `win/rail_collapsed` preference is neither
        read as permission nor overwritten here, so a user who collapsed the rail on
        purpose keeps it collapsed after a resize."""
        if not hasattr(self, "_rail"):
            return
        narrow = self.width() < RAIL_AUTO_COLLAPSE_W
        expanded = self._rail.width() >= 100
        if narrow and expanded:
            self._auto_collapsed = True
            self._toggle_rail(False, remember=False)
        elif not narrow and self._auto_collapsed:
            self._auto_collapsed = False
            if not bool(self._settings.value("win/rail_collapsed", False, type=bool)):
                self._toggle_rail(True, remember=False)

    def resizeEvent(self, e) -> None:  # type: ignore[override]
        super().resizeEvent(e)
        self._auto_rail()

    def keyPressEvent(self, e: QKeyEvent) -> None:  # noqa: N802
        # The last-resort way out of fullscreen. The game view has its own Escape
        # handler; everywhere ELSE nothing did, which is what turned a fullscreen
        # library into an Alt+F4 trap. F11 works from any page for the same reason.
        if self.isFullScreen() and (e.key() == int(Qt.Key.Key_F11)
                                    or (e.key() == int(Qt.Key.Key_Escape)
                                        and self._stack.currentWidget() is not self.play)):
            self._settings.setValue("gfx/fullscreen", False)
            self.showNormal()
            self.fit_to_screen()
            return
        super().keyPressEvent(e)

    def showEvent(self, e) -> None:  # type: ignore[override]
        super().showEvent(e)
        # Now that the window has real decorations, re-check it fits the screen.
        self.fit_to_screen()
        # Say it ONCE, and only when settings really came back. Restoring somebody's
        # preferences behind their back is nearly as confusing as losing them: they
        # would have no idea why the app knows their BIOS path again.
        recovered, self._settings_recovered = self._settings_recovered, 0
        if not recovered:
            return
        lang = cfg.language(self._settings)
        # ⛔ NOT `QMessageBox.information()`. That call is MODAL and blocks until
        # someone clicks it -- so the emulator stops dead at startup behind a box
        # nobody asked for, and anything driving it without a human (the test
        # suite, a script) hangs forever. Measured: it took the suite from 2
        # minutes to "never finishes".
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Information)
        box.setWindowTitle(cfg.tr(lang, "settings_restored_title"))
        box.setText(cfg.tr(lang, "settings_restored").format(n=recovered))
        box.setModal(False)
        box.show()

    @staticmethod
    def _wrap_nav(label: str, fm: QFontMetrics) -> str:
        """Break a nav label over two lines, at the word boundary that leaves the
        WIDEST line as narrow as possible.

        The rail is sized on that widest line, so the balanced split is the cheapest
        one -- 'Ferramentas de / depuração' costs less width than filling line one
        greedily. A single unbreakable word comes back unchanged; there is nothing
        to split, and the caller falls back to a tooltip."""
        words = label.split()
        if len(words) < 2:
            return RAIL_INDENT + label
        best, best_w = None, None
        for i in range(1, len(words)):
            top = RAIL_INDENT + " ".join(words[:i])
            bot = RAIL_INDENT + " ".join(words[i:])
            w = max(fm.horizontalAdvance(top), fm.horizontalAdvance(bot))
            if best_w is None or w < best_w:
                best, best_w = f"{top}\n{bot}", w
        return best

    def _fit_rail(self) -> None:
        """Lay the nav labels out for the current language, then size the rail to them.

        Called after anything that changes those labels or their font. A label too long
        for one line is broken over TWO rather than reworded: the translation belongs to
        whoever wrote it, and the layout is what has to give. Only a label that fits on
        neither -- one unbreakable word wider than the whole rail -- is left clipped,
        with a tooltip so it stays readable.

        Measured in the font Qt will actually PAINT with, and in BOLD: the selected entry
        is 600-weight, so measuring that form means selecting one never re-clips it."""
        widest = 0
        for b, label in self._nav_text.items():
            # Polish first: the 14px comes from the STYLESHEET, and an unpolished
            # button still reports the default app font -- a narrower one, which is
            # how you measure a rail that then clips.
            b.ensurePolished()
            f = QFont(b.font()); f.setBold(True)
            fm = QFontMetrics(f)
            text = RAIL_INDENT + label
            if fm.horizontalAdvance(text) > RAIL_MAX_W - RAIL_TEXT_PAD:
                text = self._wrap_nav(label, fm)
            b.setText(text)
            lines = [fm.horizontalAdvance(ln) for ln in text.split("\n")]
            widest = max(widest, max(lines))
            b.setToolTip("" if max(lines) <= RAIL_MAX_W - RAIL_TEXT_PAD else label)
        self._rail_w = max(RAIL_MIN_W, min(RAIL_MAX_W, widest + RAIL_TEXT_PAD))
        if self._rail.width() >= 100:     # a collapsed rail stays collapsed
            self._rail.setFixedWidth(self._rail_w)

    def _toggle_rail(self, show: bool | None = None, remember: bool = True) -> None:
        # Collapse to a thin strip that still holds the toggle (no overlap of content).
        # `remember=False` is the automatic narrow-window fold: it must not rewrite the
        # preference, or resizing would silently answer a question only the user asks.
        show = (self._rail.width() < 100) if show is None else show
        self._rail.setFixedWidth(self._rail_w if show else RAIL_COLLAPSED_W)
        for wdg in self._rail_hideable:
            wdg.setVisible(show)
        self._rail_toggle.setText("‹" if show else "☰")
        if remember:
            self._auto_collapsed = False    # an explicit choice outranks the auto-fold
            self._settings.setValue("win/rail_collapsed", not show)

    def _apply_theme(self) -> None:
        """Resolve the chosen theme and hand it to every painter.

        Sets the module global FIRST, then the stylesheet: widgets rebuilt during
        the restyle below read `PALETTE` as they are constructed, so a stale
        global would build the new UI in the old theme's colours."""
        global PALETTE
        PALETTE = ngpc_theme.resolve(cfg.theme(self._settings))
        ngpc_theme.set_current(PALETTE)     # for widgets that PAINT (the key map)
        # The debugger keeps its own module-level brushes; point them here too, so
        # a debug window opened LATER is already in the right theme.
        ngpc_debug.use_palette(PALETTE)
        app = QApplication.instance()
        if app is not None:
            ngpc_theme.apply_app_palette(app, PALETTE)
        self.setStyleSheet(ngpc_theme.build_style(PALETTE))

    def _restyle(self) -> None:
        """Repaint the whole app for the current theme, live.

        Structural twin of `_retranslate`: the stylesheet covers most widgets,
        then each page fixes up what it drew by hand. The library is REBUILT
        rather than restyled -- every card holds a cover thumbnail on a themed
        backdrop, and rebuilding reuses the cached images anyway."""
        self._apply_theme()
        self._fit_rail()               # a restyle can hand the rail a different font
        self.settings.restyle()
        self.library._rebuild()        # noqa: SLF001 -- cards bake in palette colours
        if self._debug_win is not None:
            self._debug_win.restyle(PALETTE)

    def _on_system_theme(self, _scheme) -> None:
        """Windows switched light/dark. Only our fault if we said we'd follow."""
        if cfg.theme(self._settings) == ngpc_theme.THEME_SYSTEM:
            self._restyle()

    def _retranslate(self) -> None:
        lang = cfg.language(self._settings)
        # Keep the labels UNDECORATED here: `_fit_rail` owns how they are laid out
        # (indent, and a line break when one line will not do), and it needs the
        # original text to re-wrap from on the next language or theme change.
        self._nav_text = {
            self._nav_resume: cfg.tr(lang, "m_resume"),
            self._nav_lib: cfg.tr(lang, "library"),
            self._nav_set: cfg.tr(lang, "settings"),
            self._nav_dbg: cfg.tr(lang, "m_debug"),
        }
        self._rail_toggle.setToolTip(cfg.tr(lang, "rail_toggle_tip"))
        self._fit_rail()               # the new labels may not be the old width
        self.library.retranslate()
        self.settings.retranslate()
        self.play._refresh_toolbar_tips()   # noqa: SLF001 -- re-translate the bottom toolbar

    def _update_rail(self, idx: int) -> None:
        playing = self.play.machine is not None
        self._nav_resume.setVisible(playing)
        self._nav_resume.setChecked(idx == 2)
        self._nav_lib.setChecked(idx == 0)
        self._nav_set.setChecked(idx == 1)
        self.settings.set_resume_visible(playing and idx == 1)
        # Every route between pages passes here (the rail, a launch, a failed launch,
        # the in-game options), and the page is one of the inputs to "is the nav hidden
        # for the game?" -- so re-decide it in the one place they all reach.
        self._sync_fullscreen_chrome()

    def _go(self, idx: int) -> None:
        # Leaving the game for a menu SUSPENDS it (paused, still loaded) so the
        # player can come back with Resume -- only "Quit to library" unloads it.
        if self._stack.currentWidget() is self.play and idx != 2:
            self.play.suspend()
        self._stack.setCurrentIndex(idx)
        if idx == 2 and self.play.machine is not None:
            self.play.resume_play()
        self._update_rail(idx)

    def _launch(self, rom_str: str) -> None:
        self.library._stop_worker()          # no thumbnail core alongside the game's core
        self._stack.setCurrentWidget(self.play)
        try:
            self.play.start(Path(rom_str))
        except Exception as exc:             # a bad ROM/save must never crash to desktop
            try:
                self.play.stop()
            except Exception:
                pass
            self._stack.setCurrentWidget(self.library)
            self._update_rail(0)
            QMessageBox.warning(self, cfg.tr(cfg.language(self._settings), "launch_failed"),
                                f"{Path(rom_str).name}\n\n{type(exc).__name__}: {exc}")
            return
        self._update_rail(2)

    def _launch_bios(self) -> None:
        self._stack.setCurrentWidget(self.play)
        self.play.start_bios()
        self._update_rail(2)

    def _play_options(self, category: str) -> None:
        # from the in-game menu: keep the game paused & alive, show its settings
        self.play.suspend()
        self.settings.show_category(category)
        self._stack.setCurrentWidget(self.settings)
        self._update_rail(1)

    def _launch_link_2p(self) -> None:
        self._start_link_2p(framed=False)

    def _launch_link_2p_framed(self) -> None:
        self._start_link_2p(framed=True)

    def _start_link_2p(self, *, framed: bool) -> None:
        """Open player 2 and wire the link cable. P2 chooses its OWN cartridge (its
        flash save loads normally); the two consoles then exchange serial bytes each
        frame. Input is split per player via a global router.

        `framed` picks WHERE the two consoles live: two separate windows laid out side
        by side, or one window holding both. Everything else -- the cable, the input
        router, the diagnostics -- is identical, which is the point of building player
        2's page here rather than inside either window class.
        """
        if self._link2p is not None:              # already linked -> just surface it
            self._link2p.raise_(); self._link2p.activateWindow(); return
        if self.play.machine is None or self._one_link_at_a_time(False):
            return                                 # nothing running to link to
        start_dir = (str(self.play._rom_path.parent) if self.play._rom_path
                     else str(DEFAULT_ROM_DIR))
        rom2, _ = QFileDialog.getOpenFileName(
            self, "Player 2 — choose a cartridge (may be the same as player 1)",
            start_dir, "NGPC ROMs (*.ngc *.ngp *.npc *.zip *.7z);;All (*)")
        if rom2:
            rom2 = choose_game_in_archive(self, rom2, cfg.language(self._settings))
        if not rom2:
            return
        p2 = PlayPage(self._settings, self._library_db)
        p2._link_btn.hide()                        # noqa: SLF001 -- P2 never re-links
        p2.set_player(2)                           # P2 bindings + gamepad
        p2._lean = True                            # silent + wall-clock paced (see _lean)
        p2.start(Path(rom2))                       # its own flash save loads here
        self.play.attach_link(p2)                  # wire the cable both ways
        p2.attach_link(self.play)
        self._link_input = _LinkInput(self.play, p2)
        QApplication.instance().installEventFilter(self._link_input)

        self._link_framed = framed
        if framed:
            # Borrow player 1's page out of the shell for the session; _end_link_2p
            # puts it back. Nothing about the running console is disturbed.
            self._stack.removeWidget(self.play)
            win = _Link2PFrame(self.play, p2, self._settings, self)
            win.resize(self.size().width(), self.size().height())
            win.move(self.pos())
            self.hide()
        else:
            win = _Link2PWindow(p2, self._settings, self)
            self._lay_out_side_by_side(win)
        win.closed.connect(self._end_link_2p)
        self._link2p = win
        # Live link diagnostic in P2's title bar: if both counts climb once both
        # consoles reach the game's VS / link menu, the cable is carrying bytes and
        # any remaining "no link" is a protocol/timing detail; if they stay 0, the
        # games are not probing serial through this path (or not both at the menu).
        self._link_status = QTimer(self)
        self._link_status.timeout.connect(self._refresh_link_title)
        self._link_status.start(500)
        self._refresh_link_title()
        win.show(); win.raise_()
        stamp_taskbar_icon(win)               # player 2 / the 2P frame is a button too

    def _lay_out_side_by_side(self, win) -> None:
        """Same size as player 1's window, next to it -- not on top of it."""
        screen = self.screen() or QApplication.primaryScreen()
        avail = screen.availableGeometry()
        mine = self.frameGeometry()
        # ⚡ ASK, THEN READ BACK. A window silently refuses to shrink below its own
        # minimum, and that minimum is not something to predict -- `minimumSizeHint()`
        # is inflated by every page in the stack and by the toolbar's translated
        # labels, so trusting it made both windows far too wide. Instead: ask both for
        # the same size, see what each ACTUALLY took, and settle on the larger. Neither
        # can refuse that, so they finish equal by construction rather than by guess.
        self.resize(mine.width(), mine.height())
        win.resize(mine.width(), mine.height())
        w = max(self.width(), win.width())
        h = max(self.height(), win.height())
        self.resize(w, h); win.resize(w, h)
        grew = self.frameGeometry()
        one, two = side_by_side(
            (mine.x(), mine.y(), grew.width(), grew.height()),
            (avail.x(), avail.y(), avail.width(), avail.height()),
            min_w=grew.width(), min_h=grew.height())
        # Player 1 only moves/resizes when the pair could not fit as it stood; when it
        # already fits, `one` is exactly where it already is and this is a no-op.
        self.resize(one[2], one[3]); self.move(one[0], one[1])
        win.resize(two[2], two[3]); win.move(two[0], two[1])

    def _refresh_link_title(self) -> None:
        if self._link2p is None:
            return
        p2 = self._link2p.play
        self._link2p.setWindowTitle(
            f"NGPC — Player 2   ⇄ cable  P1→{self.play._link_tx_total} B  "
            f"P2→{p2._link_tx_total} B")

    def _end_link_2p(self) -> None:
        if self._link2p is None:
            return
        if getattr(self, "_link_status", None) is not None:
            self._link_status.stop(); self._link_status = None
        QApplication.instance().removeEventFilter(self._link_input)
        # BOTH pages, not just this one. Player 2's page still points at player 1's, and
        # a single tick from its timer before it is destroyed would drive a console that
        # believes it is on its own again.
        peer = self._link2p.play
        self.play.detach_link()
        peer.detach_link()
        if getattr(self, "_link_framed", False):
            # Hand player 1's page back to the shell, exactly where it lives, and come
            # out from behind the frame. Called BEFORE `_to_library` switches pages
            # (see its first lines), so the stack is whole again by the time it does.
            self._stack.insertWidget(2, self.play)
            self._stack.setCurrentWidget(self.play)
            self.show(); self.raise_(); self.activateWindow()
        self._link_framed = False
        self._link_input = None
        self._link2p = None

    # ---- online link (LAN / internet) -------------------------------------
    def _host_online(self) -> None:
        if self.play.machine is None or self._one_link_at_a_time(False):
            return
        port, ok = QInputDialog.getInt(
            self, "Host a network game", "Listen on port:", 7788, 1, 65535)
        if not ok:
            return
        if not self._start_net("host", "", int(port),
                               cfg.tr(cfg.language(self._settings), "link_waiting")
                               .format(port=port)):
            return
        # Show the connection info (public IP auto-detected) + port-forward/risks help.
        self._show_host_info(int(port))

    def _join_online(self) -> None:
        if self.play.machine is None or self._one_link_at_a_time(False):
            return
        text, ok = QInputDialog.getText(
            self, "Join a network game",
            "Host address (IP or Tailscale name), e.g. 192.168.1.42:7788")
        if not ok or not text.strip():
            return
        host, _, p = text.strip().partition(":")
        port = int(p) if p.strip().isdigit() else 7788
        self._start_net("join", host.strip(), port,
                        cfg.tr(cfg.language(self._settings), "link_connecting")
                        .format(addr=f"{host}:{port}"))

    def _start_net(self, mode: str, host: str, port: int, waiting: str) -> bool:
        """True when the attempt actually started.

        ⚠️ THE CALLER MUST HONOUR THE ANSWER, and the mirror is why: it arms
        `_mirror_pending` to say which kind of session the next connection becomes.
        Arming it BEFORE a start that gets refused leaves the flag set over an
        earlier CABLE attempt still in flight -- which then connects and silently
        opens a mirror session instead. So the flag is armed only on a real start.
        """
        lang = cfg.language(self._settings)
        if self._net_thread is not None:            # one attempt at a time
            # ⛔ NEVER A SILENT `return`. This guard is what turned a host attempt
            # nobody could reach into a dead emulator: it stayed armed for the rest
            # of the session and swallowed every later Host/Join click without a
            # word. Say it is busy, and offer the way out in the same breath.
            if QMessageBox.question(
                    self, cfg.tr(lang, "net_pending_title"),
                    cfg.tr(lang, "net_pending_ask"),
                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                    QMessageBox.StandardButton.Yes) \
                    == QMessageBox.StandardButton.Yes:
                self._cancel_net_attempt()
            return False
        # ⛔ UNE ATTENTE MUETTE NE SE DISTINGUE PAS D'UNE ATTENTE MORTE, et c'est ce
        # qui a coute le plus cher dans le rapport « rien ne change, en attente de
        # connexion..... »: le texte etait pose ici une fois pour toutes, donc il
        # disait exactement la meme chose quand le fil ecoutait et quand il avait
        # abandonne depuis dix minutes. Un compteur qui avance est la seule chose qui
        # separe les deux SANS rien demander au joueur -- et s'il s'arrete, la
        # tentative est finie.
        self._stop_net_clock()
        started = time.monotonic()

        # ⛔ « 127.0.0.1 », C'EST CE PC -- ET RIEN NE LE DISAIT.
        #
        # Mesure sur la machine d'un joueur bloque « en attente »: une seule vraie
        # instance de l'emulateur, en SYN_SENT vers 127.0.0.1:7789, pendant que le
        # panneau d'adresse etait ouvert sur l'AUTRE PC. L'adresse de bouclage avait
        # ete prise pour « l'adresse du serveur qu'on m'a donnee ». Elle est
        # parfaitement legitime -- deux fenetres sur un seul PC s'en servent -- donc on
        # ne la refuse pas: on rappelle ce qu'elle veut dire, et seulement une fois
        # qu'il est clair que ca ne prend pas.
        loopback = mode == "join" and host.strip().lower() in (
            "127.0.0.1", "localhost", "::1", "0.0.0.0")

        def tick() -> None:
            secs = int(time.monotonic() - started)
            txt = cfg.tr(lang, "link_waiting_since").format(
                msg=waiting, mmss=f"{secs // 60}:{secs % 60:02d}")
            if loopback and secs >= 5:
                txt += "  " + cfg.tr(lang, "net_hint_loopback")
            self.play.overlay.setText(txt)

        tick()
        self._net_clock = QTimer(self)
        self._net_clock.timeout.connect(tick)
        self._net_clock.start(1000)
        th = _NetConnect(mode, host, port, lang)
        th.connected.connect(self._on_net_connected)
        th.failed.connect(self._on_net_failed)
        th.finished.connect(lambda t=th: self._on_net_thread_finished(t))
        self._net_thread = th
        self.play.net_pending = True
        th.start()
        return True

    def _stop_net_clock(self) -> None:
        """L'attente est finie, quelle qu'en soit la raison: le compteur s'arrete, et
        celui qui regarde l'ecran le voit."""
        if self._net_clock is not None:
            self._net_clock.stop()
            self._net_clock = None

    def _on_net_thread_finished(self, th) -> None:
        """⚠️ WHICH worker finished matters, and the old handler did not ask.

        A cancelled attempt does not stop instantly -- the join path can sit inside
        a blocking connect for up to its 2 s slice -- and the player may well have
        started a NEW attempt in between. A handler that just cleared `_net_thread`
        would then throw away the new attempt's bookkeeping, and with it the only
        Python reference keeping its QThread alive.
        """
        if th in self._net_retiring:
            self._net_retiring.remove(th)
        if self._net_thread is th:
            self._net_thread = None
            self.play.net_pending = False
            self._stop_net_clock()

    def _retire_net_thread(self, th) -> None:
        """Cancel a worker and KEEP IT ALIVE until it has really stopped.

        ⛔ THE CRASH THIS AVOIDS. Dropping the last Python reference to a RUNNING
        QThread destroys the C++ object under the OS thread that is still in it,
        and Qt answers in ~QThread with "Destroyed while thread is still running"
        followed by terminate() -- a hard process death, no traceback, exactly the
        signature the root conftest documents for the test runner. `cancel()` is
        not instantaneous, so the object is parked here until its own `finished`
        takes it off the list.
        """
        if th is None:
            return
        self._net_retiring.append(th)
        th.cancel()

    def _shutdown_net(self) -> None:
        """Leave no running worker behind when the window goes away."""
        self._retire_net_thread(self._net_thread)
        self._net_thread = None
        for th in list(self._net_retiring):
            th.cancel()
            th.wait(2500)
        self._net_retiring.clear()

    def _net_attempt_pending(self) -> bool:
        return self._net_thread is not None

    def _from_current_worker(self) -> bool:
        """True unless this signal comes from a worker we have already let go.

        ⚠️ A BOOLEAN "abandoned" FLAG IS NOT ENOUGH, and that was the first try at
        this. There can be TWO workers in flight -- one retiring after a cancel, one
        the player has just started -- and starting the new one resets the flag,
        which lets the cancelled one's late `connected` through after all: the
        emulator attaches the very attempt that was taken back. The SENDER is the
        only thing that says which worker is talking.

        A direct call (no signal) has no sender and is trusted, so the slots stay
        callable from a test.
        """
        src = self.sender()
        return src is None or src is self._net_thread

    def _cancel_net_attempt(self) -> None:
        """Take back a host/join that has not connected. Reachable from the 🔗 menu
        and from closing the host-info panel -- the two places a player who wants
        out actually looks.

        ⚠️ IT CAN BE ASKED FOR AFTER THE ATTEMPT HAS ALREADY SUCCEEDED, and both ways
        in are why: `QMessageBox.question` and `QMenu.exec` each run a NESTED event
        loop, inside which the worker's queued `connected` is delivered like any
        other. So the peer can arrive between the question being put and the answer
        coming back, and between the menu opening and the entry being clicked.
        Without this guard the cancel then wiped a LIVE session -- overlay banner,
        host panel and mirror mode -- for a link that was already up.
        """
        if not self._net_attempt_pending():
            return
        # The worker's signals are QUEUED to this thread, so one emitted just before
        # the cancel is still in flight and will be delivered AFTER us. Clearing
        # `_net_thread` first is what lets `_from_current_worker` recognise it and
        # drop it -- without that, a cancelled attempt could still attach itself.
        th, self._net_thread = self._net_thread, None
        self.play.net_pending = False
        self._retire_net_thread(th)
        self._close_host_info()
        self._mirror_pending = None            # or the next Host would start a mirror
        # ⛔ ET LE COMPTEUR S'ARRETE ICI AUSSI. Sans ca il continuait d'ecrire
        # « en attente — 4:12 » par-dessus tout le reste, sur une tentative annulee:
        # le compteur avait ete ajoute pour qu'une attente morte se voie, et il en
        # fabriquait une vivante.
        self._stop_net_clock()
        self.play.overlay.setText("")
        self.play._flash(cfg.tr(cfg.language(self._settings), "net_cancelled"))

    def _close_host_info(self) -> None:
        dlg, self._host_info = getattr(self, "_host_info", None), None
        if dlg is not None:
            dlg.close()

    def _show_host_info(self, port: int) -> None:
        """The host's own address panel -- and the cancel handle that goes with it.

        Closing it while nothing has connected yet means "never mind", which is the
        gesture a player makes long before finding a menu entry. `_close_host_info`
        clears the attribute BEFORE closing, so tearing it down on success cannot
        loop back round and cancel the link that just came up.
        """
        from ngpc_lobby import HostInfoDialog
        game = self.play._rom_path.stem if self.play._rom_path else "?"
        self._host_info_port = int(port)
        self._host_info = HostInfoDialog(game, int(port),
                                         cfg.language(self._settings), self)
        self._host_info.finished.connect(self._on_host_info_closed)
        self._host_info.show()

    def _on_host_info_closed(self, *_a) -> None:
        """Le panneau d'adresse se ferme. L'HEBERGEMENT CONTINUE.

        ⛔ IL ANNULAIT LA TENTATIVE, ET C'EST LA PANNE QU'ON A CHERCHEE TOUTE LA
        SOIREE. Rapporte comme « rien ne change, en attente de connexion..... », en
        LAN comme en ligne, en cable comme en miroir. Reproduit en regardant les
        VRAIES sockets (tools/watch_link_sockets.py + un banc sur `Shell`):

            clic « Heberger »            -> ecoute sur le port : OUI
            fermeture du panneau d'adresse -> ecoute sur le port : NON

        Or ce panneau est une FICHE: il affiche ton IP locale, ton IP publique
        detectee, la ligne a coller dans Discord, et un bouton « Fermer ». Sa propre
        classe le dit -- `HostInfoDialog`: « Non-modal -- hosting keeps running behind
        it ». Un joueur lit son adresse, la copie, ferme la fiche pour revoir son jeu,
        et vient d'arreter d'heberger sans qu'un mot le lui dise. Son pair, lui, tape
        dans un port ferme: cote hote « en attente », cote invite « connexion refusee ».
        Deux ecrans, deux pannes, une seule cause.

        ⚡ L'INTENTION D'ORIGINE RESTE SERVIE AUTREMENT. Fermer le panneau etait pense
        comme « la sortie ou un joueur qui veut arreter va regarder ». Mais la sortie
        existe deja et elle est EXPLICITE -- « Annuler la tentative », dans le menu 🔗.
        Une fiche qu'on ferme ne dit pas « j'abandonne »; elle dit « j'ai lu ». Alors on
        garde l'ecoute, et on RAPPELLE ou est le bouton qui arrete.
        """
        port = getattr(self, "_host_info_port", 0)
        self._host_info = None
        if self._net_attempt_pending():
            self.play._flash(cfg.tr(cfg.language(self._settings),   # noqa: SLF001
                                    "host_still_listening").format(port=port), 6000)

    def _on_net_connected(self, sock) -> None:
        from core.link import TcpLink
        # ⚠️ THE CONSOLE MAY HAVE MOVED ON WHILE WE WERE SEARCHING. An attempt can be
        # in flight for minutes, and nothing stopped the player wiring a local cable
        # or a mirror in the meantime -- `_one_link_at_a_time` only sees links that
        # already EXIST. Attaching on top of one is the exact fight that guard was
        # written to prevent: two relays over one serial FIFO.
        busy = self.play._link_peer is not None or self.play._mirror is not None
        if not self._from_current_worker() or busy:  # cancelled, superseded, or too late
            try: sock.close()
            except OSError: pass
            if busy:
                self.play._flash(cfg.tr(cfg.language(self._settings), "link_busy"))
            return
        self._stop_net_clock()       # relie: plus rien a compter
        self._close_host_info()      # the panel answered its question: they are in
        if self._mirror_pending is not None:        # the other online mode
            from core.netplay import SocketPipe
            mode, self._mirror_pending = self._mirror_pending, None
            self._begin_mirror(SocketPipe(sock), host=(mode == "host"))
            return
        if self.play.machine is None:
            try: sock.close()
            except OSError: pass
            return
        net = TcpLink(self.play.machine, sock)
        self.play.attach_net_link(net)
        self.play.overlay.setText(cfg.tr(cfg.language(self._settings), "link_ready"))
        self._net_status = QTimer(self)
        self._net_status.timeout.connect(
            lambda: self.setWindowTitle(
                f"NgpCraft — online ⇄  out {net.bytes_out} B   in {net.bytes_in} B"))
        self._net_status.start(500)

    # ---- mirror netplay: the second online mode -------------------------------
    def _host_mirror(self) -> None:
        if self.play.machine is None or self._one_link_at_a_time(True):
            return
        port, ok = QInputDialog.getInt(
            self, "Host a mirror game", "Listen on port:", 7789, 1, 65535)
        if not ok or not self._ask_mirror_delay():
            return
        if not self._start_net("host", "", int(port),
                               cfg.tr(cfg.language(self._settings), "link_waiting")
                               .format(port=port)):
            return
        self._mirror_pending = "host"      # only once the attempt is really ours
        # ⛔ THE HOST HAS TO BE TOLD ITS OWN ADDRESS -- there is no other way for the
        # other player to reach it, and "listening on port 7789" is not an address. The
        # cable mode has shown this panel (LAN address, auto-detected public one, a
        # ready-to-paste line) since it existed; the mirror was simply never wired to
        # it, so hosting a mirror game meant going and finding your IP by hand.
        self._show_host_info(int(port))

    def _join_mirror(self) -> None:
        if self.play.machine is None or self._one_link_at_a_time(True):
            return
        text, ok = QInputDialog.getText(
            self, "Join a mirror game",
            "Host address (IP or Tailscale name), e.g. 192.168.1.42:7789")
        if not ok or not text.strip():
            return
        host, _, pt = text.strip().partition(":")
        port = int(pt) if pt.strip().isdigit() else 7789
        # ⛔ NOT ASKED FOR ANY MORE. The input delay has to be identical on both PCs,
        # and asking two people to type the same number is not a design -- any
        # difference refused the session, and a refusal is a hang-up, which the other
        # player saw as a bare `[WinError 10054]`. The host's number is the one that
        # counts; this side takes it from the handshake, exactly as the lobby already
        # did (core.netplay.Handshake.check).
        if not self._start_net("join", host.strip(), port,
                               cfg.tr(cfg.language(self._settings), "link_connecting")
                               .format(addr=f"{host}:{port}")):
            return
        self._mirror_pending = "join"      # only once the attempt is really ours

    # ⚡ IN MIRROR MODE THE TWO CONSOLES ARE BUILT THE SAME WAY ON BOTH PCs, and that
    # is not a detail -- it is the whole mode.
    #
    # ⛔ THE DESYNC THIS ENDS, caught by the shell test on its first run: the local
    # console comes from PlayPage.start with the player's own settings (flash size,
    # save mode, HLE or retail BIOS) and its clock set from THIS PC's wall clock, while
    # the mirror was built with defaults. So player 1's console here and player 1's
    # mirror over there were two different machines, and the checksum caught it at
    # frame ZERO. Two rules fix it, both of them boring and both of them required:
    #   * the mirror boots from the LOCAL console's cartridge image, byte for byte
    #     (which carries the save with it), with the same console settings;
    #   * both consoles are reset onto ONE agreed session clock, because "now" is a
    #     different number on the two PCs and the BIOS copies it into RAM at boot.
    # A game that shows the date will show the session clock. That is the price, it is
    # visible, and it is smaller than a match that silently drifts apart.
    MIRROR_CLOCK = (0x00, 0x01, 0x01, 0x00, 0x00, 0x00, 0x04)   # 2000-01-01 00:00:00

    def _one_link_at_a_time(self, want_mirror: bool) -> bool:
        """True (and says so) when another kind of link is already attached.

        ⛔ The two modes drive the SAME console two different ways: the cable relays its
        serial FIFO to a peer, the mirror runs a second console here and owns the pad
        byte. Both at once means two relays fighting over one FIFO and two writers of
        one controller port -- so the second one asked for is refused, plainly, instead
        of half-attaching.
        """
        p = self.play
        mirrored = p._mirror is not None
        cabled = p._link_peer is not None or p._net_link is not None
        if (want_mirror and cabled) or (not want_mirror and mirrored):
            p._flash(cfg.tr(cfg.language(self._settings), "link_busy"))
            return True
        return False

    def _ask_mirror_delay(self) -> bool:
        """How many frames of input delay to cover the round trip.

        BOTH PLAYERS MUST TYPE THE SAME NUMBER -- the delay decides which frame an
        input is played on, so two different values are two different matches and the
        handshake refuses them. Roughly: ping in ms / 17, plus one.
        """
        d, ok = QInputDialog.getInt(
            self, "Mirror play",
            "Input delay in frames (BOTH players must use the same number).\n"
            "About ping-in-ms / 17, plus one:", cfg.mirror_delay(self._settings),
            0, 30)
        if ok:
            self._settings.setValue("online/mirror_delay", int(d))
        return bool(ok)

    def _mirror_handshake(self, host: bool, delay: int | None = None):
        """The fingerprint both PCs must agree on, and our own cartridge's.

        The cartridge is ANNOUNCED, not compared: the images are traded (see
        core.netplay.CartExchange), because requiring the same one meant requiring the
        same SAVE, and two players almost never have that. What still has to match is
        what decides how the code RUNS.
        """
        import hashlib
        from core.netplay import Handshake

        page = self.play
        bios = page._bios_path()
        bios_hash = (hashlib.sha1(Path(bios).read_bytes()).hexdigest()[:16]
                     if bios else "hle")
        # The wait-state switch is a per-PC SETTING that changes how fast the code
        # runs, so it belongs in the fingerprint: two players on different sides of
        # that toggle would drift, and drift is what the handshake exists to refuse.
        return Handshake(
            rom_hash=hashlib.sha1(page.session._rom).hexdigest()[:16],
            bios_hash=bios_hash,
            # ...and the SCHEDULE the two consoles are stepped on. core_fingerprint
            # hashes the DLL, so a change to how Python interleaves them would not
            # move it -- and two builds stepping the pair differently drift.
            core_version=(native.core_fingerprint()
                          + f"+ilv{core_link.CABLE_SLICE}"
                          + ("+waits" if cfg.cart_wait_states(self._settings) else "")),
            # The input delay MUST be identical on both PCs (the handshake refuses a
            # mismatch), so when the room advertises one, that is the one -- the joiner
            # follows the host rather than its own setting.
            delay=(cfg.mirror_delay(self._settings) if delay is None else int(delay)),
            # ⚡ ...and the per-player settings that shape THIS console, so the other PC
            # can build its mirror of it the same way. Not a thing to agree on -- a
            # thing to copy, like the cartridge. The cartridge LANGUAGE byte (0x6F87)
            # is per player and a bilingual cart branches on it, so a mirror built from
            # the local setting was a different machine and the match died on its own
            # checksum, saying only "desync".
            #
            # Mono/colour follows the selected BIOS, which the fingerprint compares --
            # EXCEPT when the BIOS is simulated: an NGP and an NGPC running without a
            # BIOS file both fingerprint as "hle", and they are not the same console.
            console={"lang": int(cfg.cart_language(self._settings)),
                     "mono": bool(self.play._mono_console())},
            host=host)

    def _begin_mirror(self, pipe, host: bool, delay: int | None = None) -> None:
        """Trade cartridges with the peer, then start the mirror session.

        ⚡ TWO PHASES, AND THE FIRST ONE IS WHY THE MODE IS USABLE AT ALL. Building the
        mirror from the LOCAL cartridge forced both players to hold the same image --
        and a save lives inside the image, so it forced the same SAVE. It also barred
        SNK-versus-Capcom in Card Fighters' Clash, which is the reason that game has a
        link. So each side sends its own image and builds the peer's console from the
        one it receives. A few MiB, once, with progress on screen.
        """
        from core.netplay import CartExchange

        # A trade already running belongs to an attempt nobody is waiting for any more:
        # end it (and let go of its transport) rather than leak a timer and a socket --
        # or, over the lobby, a live client thread sitting in a room.
        self._end_mirror_bringup()
        page = self.play
        if page.machine is None or page.session is None or page._rom_path is None:
            _close_pipe(pipe)
            return
        self._mirror_boot = CartExchange(pipe, page.session._rom,
                                         self._mirror_handshake(host, delay))
        self._mirror_boot_host = host
        page._flash(cfg.tr(cfg.language(self._settings), "mirror_trading"), 60_000)
        self._mirror_timer = QTimer(self)
        self._mirror_timer.timeout.connect(self._pump_mirror_bringup)
        self._mirror_timer.start(16)

    def _pump_mirror_bringup(self) -> None:
        """Drive the cartridge trade, then hand over to the session."""
        x = self._mirror_boot
        if x is None:
            return
        if self.play.machine is None:
            # The game was closed mid-trade. There is nothing left to build a session
            # for, and pumping on would leave the player sitting in a lobby room.
            self._end_mirror_bringup()
            return
        for _ in range(8):          # a few chunks per tick: a 4 MiB cart is ~128 of them
            x.pump()
            if x.done or x.failed:
                break
        lang = cfg.language(self._settings)
        if x.failed:
            self._end_mirror_bringup()
            self.play._flash(cfg.tr(lang, "mirror_refused").format(why=x.failed),
                             PlayPage.MIRROR_ERROR_MS)
            return
        if not x.done:
            up, down = x.progress
            self.play.overlay.setText(
                cfg.tr(lang, "mirror_trading_pct").format(pct=int(min(up, down) * 100)))
            return
        peer_image, prime = x.peer_image, x.leftover
        pipe, hs = x.pipe, x.hs
        self._end_mirror_bringup(close=False)   # the session takes the transport over
        self._start_mirror(pipe, hs, peer_image, prime)

    def _end_mirror_bringup(self, close: bool = True) -> None:
        """Stop the cartridge trade, and by default let go of its transport.

        ⛔ `close=False` is for the ONE caller that hands the pipe straight to the
        session -- everybody else is abandoning, and an abandoned trade used to leave
        its socket open (and, over the lobby, a client thread and a room the player
        never left).
        """
        if self._mirror_timer is not None:
            self._mirror_timer.stop(); self._mirror_timer = None
        if close and self._mirror_boot is not None:
            _close_pipe(self._mirror_boot.pipe)
        self._mirror_boot = None

    def _start_mirror(self, pipe, hs, peer_image: bytes, prime: bytes) -> None:
        from core.link import InProcessLink
        from core.netplay import MirrorSession

        page = self.play
        if page.machine is None or page.session is None or page._rom_path is None:
            _close_pipe(pipe)          # nobody else holds it now
            return
        # The mirror is a session like any other, minus the two things a copy of
        # somebody else's console must not have: it never writes a save, and it has no
        # window -- nobody watches it, it exists so the cable has something to talk to.
        # ⚡ Its cartridge is the one the PEER sent, save and all.
        peer = NativeSession(
            page._rom_path, rom_bytes=peer_image, autosave=False,
            bios_path=page._bios_path(), real_bios=page._real_bios,
            save_to_rom=False, sidecar=False,
            # ⛔ Sized from the IMAGE, and ONLY from the image. The peer's session
            # already padded its cartridge up to its own flash capacity before sending
            # it, so the image's length IS that capacity. Passing it through
            # `flash_capacity_bytes` re-applied OUR setting on top -- an explicit
            # 16 Mbit here turned a peer's 8 Mbit cart into a 16 Mbit one, which is a
            # different machine on each side of the same match. The comment said "from
            # the image"; the code still asked this PC.
            flash_size=len(peer_image),
            k1ge_console=bool(hs.peer_console.get("mono", page._mono_console())),
            # ⚡ THEIR cartridge-language setting, not ours: it is a property of the
            # console being mirrored (0x6F87), and a bilingual cart branches on it.
            language=int(hs.peer_console.get("lang", cfg.cart_language(self._settings))),
            hle_bios=resolve_selected_bios(self._settings)[1] == cfg.BIOS_USE_HLE)
        # ⚡ AND THE SAME TIMING. PlayPage.start tunes its machine AFTER building it
        # (silicon-calibrated cart wait-states); a mirror built without them executes
        # the same code at a different speed, which is a desync inside one frame --
        # measured, the two copies parted company on the very first one.
        #
        # ⛔ AND IT MUST BE THE SAME *CALL*, not the same intention. This block used to
        # spell out four setters while PlayPage.start spelled out its own; when the
        # timing model grew to eight settings behind `set_timing_silicon`, updating one
        # site and not the other gave the two consoles of the SAME PC different
        # machines. They desynced on frame 2, exactly as the paragraph above predicts.
        if cfg.cart_wait_states(self._settings):
            peer.machine.set_timing_silicon(cfg.CART_FETCH_WAIT, cfg.CART_BIOS_WAIT)
        clock = native.RtcState(1, *self.MIRROR_CLOCK)
        for m in (page.machine, peer.machine):
            m.set_rtc(clock)
            m.serial_set_enabled(True)      # the cable is in before either boots
            m.reset(real_bios=page._real_bios, bios_handoff=not page._real_bios)
        # A console that has just been power-cycled needs the page's own boot
        # bookkeeping cleared with it -- the same things _do_reset clears. Skipping it
        # left the BIOS hand-off assist thinking it had already run, on the console-boot
        # path; and a reset clears the core's breakpoints, so they are re-armed.
        page._frame = 0
        page._did_handoff = False
        page._bios_frames = 0
        page._menu_ticks = 0
        page._crashed = False
        page._power_pressed = False
        page.watches.rearm()
        page.apply_debug()
        link = InProcessLink(page.machine, peer.machine)
        session = MirrorSession(page.machine, peer.machine, link, pipe, hs, prime)
        page.attach_mirror(session, peer)
        self._net_status = QTimer(self)
        # ⚡ L'ALLER-RETOUR EST AFFICHE, SINON IL N'EXISTE PAS POUR LE JOUEUR.
        #
        # Le mesurer sans le montrer ne sert a rien : c'est le testeur qui rapporte, et
        # jusqu'ici il ne pouvait dire que « ca a lache ». Avec le pire aller-retour a
        # l'ecran, un rapport devient « ca a lache, pointe a 340 ms » -- et la on sait
        # si le probleme est le link ou la ligne (LINK_NETPLAY_STUDY §2.2 et §2.5).
        def _titre():
            bout = ""
            if session.rtt_ms is not None:
                bout = (f", aller-retour {session.rtt_ms} ms"
                        f" (moy {session.rtt_avg_ms:.0f}, pire {session.rtt_max_ms})")
            if session.desync_at is not None:
                bout += f"  ⚠ desync trame {session.desync_at}"
            self.setWindowTitle(f"NgpCraft — mirror ⇄  {session.frames_run} frames, "
                                f"{session.stalls} waits{bout}")

        self._net_status.timeout.connect(_titre)
        self._net_status.start(500)

    def _on_mirror_ended(self, why: str) -> None:
        self._end_mirror_bringup()      # a trade still running belongs to this session
        self.play.detach_mirror()
        if self._net_status is not None:
            self._net_status.stop(); self._net_status = None

    def _on_net_failed(self, msg: str) -> None:
        if not self._from_current_worker():
            return          # a worker we let go; its failure notice would be noise
        # ⛔ CLEAR THE MODE FLAG. Left set by a mirror attempt that timed out, the next
        # ordinary Host/Join would silently start a MIRROR session instead of a cable.
        self._mirror_pending = None
        self._stop_net_clock()
        self.play.overlay.setText(
            cfg.tr(cfg.language(self._settings), "link_failed").format(why=msg))

    def _open_lobby(self) -> None:
        if self.play.machine is None or self._one_link_at_a_time(False):
            return
        from ngpc_lobby import LobbyDialog
        game = self.play._rom_path.stem if self.play._rom_path else "?"
        dlg = LobbyDialog(self._settings, game, self)
        dlg.linked.connect(self._on_lobby_linked)
        dlg.exec()

    def _on_lobby_linked(self, client, info: dict | None = None) -> None:
        from core.lobby import LobbyLink, LobbyPipe
        if self.play.machine is None:
            client.close(); return
        # ⚡ THE ROOM SAYS WHICH LINK IT IS. The relay carries opaque bytes, so the two
        # clients have to agree on what they mean -- and only the host chose. A room
        # made by a client that predates mirror rooms says nothing, which means cable.
        info = info or {}
        if info.get("mode") == "mirror":
            # Mirror play over the lobby: the room, the pairing and the NAT traversal
            # are the same; only what travels on it changes. Card Fighters' Clash needs
            # this -- its VS handshake gives up on latency the cable mode cannot avoid.
            self._begin_mirror(LobbyPipe(client), host=(info.get("role") == "host"),
                               delay=info.get("delay"))
            return
        net = LobbyLink(self.play.machine, client)
        self.play.attach_net_link(net)
        # The lobby has its OWN way of losing a peer, and it does not go through
        # TcpLink at all: this link only ever touches thread-safe queues, so it cannot
        # crash the way the direct one did -- it went QUIET instead, and the player was
        # left waiting on a console that had already left the room. The client knows;
        # nothing was listening.
        client.peer_left.connect(
            lambda: self._on_net_link_lost(
                cfg.tr(cfg.language(self._settings), "link_peer_left")))
        client.disconnected.connect(self._on_net_link_lost)
        self.play.overlay.setText(cfg.tr(cfg.language(self._settings), "link_ready"))
        self._net_status = QTimer(self)
        self._net_status.timeout.connect(
            lambda: self.setWindowTitle(
                f"NgpCraft — online ⇄  out {net.bytes_out} B   in {net.bytes_in} B"))
        self._net_status.start(500)

    def _on_net_link_lost(self, why: str) -> None:
        """The online peer went away. Say so, and put the console back on its own.

        The game keeps running -- a dropped cable is not a dropped game, and on real
        hardware unplugging the lead does not switch the console off. What must not
        happen is the emulator carrying on pumping a dead socket, which is where the
        crash came from.
        """
        self._end_net_link()
        self.play._flash(cfg.tr(cfg.language(self._settings), "link_lost").format(why=why))

    def _end_net_link(self) -> None:
        if self._net_status is not None:
            self._net_status.stop(); self._net_status = None
            self.setWindowTitle("NgpCraft")
        th, self._net_thread = self._net_thread, None
        self._retire_net_thread(th)
        self.play.net_pending = False
        self._close_host_info()
        self.play.detach_net_link()

    def _open_debug(self) -> None:
        if self._debug_win is None:
            self._debug_win = DebugWindow(self, self._settings)
        self._debug_win.attach(self.play)     # may be None -> "no game running"
        self._debug_win.show(); self._debug_win.raise_()
        self._debug_win.activateWindow()
        stamp_taskbar_icon(self._debug_win)   # its own button, so its own icon

    def _to_library(self) -> None:
        if self._link2p is not None:              # tear down 2-player first
            self._link2p.close()                  # its closeEvent -> _end_link_2p
        self._end_net_link()                      # ...and any online link
        self.play.stop()
        if self._debug_win is not None:
            self._debug_win.attach(None)
        self._go(0)

    def _on_settings_changed(self) -> None:
        if self.play.machine is not None:
            self.play.apply_settings()
        # `apply_settings` may enter/leave fullscreen (which fires changeEvent), but the
        # "hide UI in fullscreen" toggle itself changes nothing about the window state --
        # so re-sync here too, or ticking it while already fullscreen would do nothing.
        self._sync_fullscreen_chrome()

    def _on_storage_changed(self) -> None:
        # The settings store moved (portable config.ini <-> registry) and the values
        # were already migrated. Point the shell and every page at the new store so the
        # rest of the session reads/writes the right place -- the lambdas bound to each
        # page read `self._settings` at call time, so reassigning it is enough.
        s = cfg.make_settings()
        self._settings = s
        self.library._settings = s
        self.settings._settings = s
        self.play._settings = s

    def _sync_fullscreen_chrome(self) -> None:
        """In fullscreen, optionally hide the sidebar and player toolbar so the game gets
        the whole screen. Leaving fullscreen restores them -- the toolbar to the user's
        SAVED preference, never forced back on. Driven by the window state (so every way
        into fullscreen is covered) and by the Settings toggle."""
        # A WindowStateChange can fire mid-construction (restoreGeometry, the first show)
        # before the rail and the play page exist -- ignore it until the UI is built.
        if not hasattr(self, "_rail") or not hasattr(self, "play"):
            return
        # ...and only over the GAME. Hiding the nav on the library or the settings page
        # leaves a fullscreen window with no menu and no title bar -- the "je ne vois pas
        # le menu a gauche" half of the 2026-08-02 report. There is nothing to give the
        # screen to on those pages anyway.
        playing = self._stack.currentWidget() is self.play
        hide = self.isFullScreen() and cfg.fs_hide_ui(self._settings) and playing
        self._rail.setVisible(not hide)
        # On the EDGE into hidden-chrome fullscreen, start the toolbar hidden (a mouse move
        # reveals it, like a media player); on the way back out, show it again. Edge-only,
        # so a later re-sync while fullscreen never re-hides a bar a move just revealed.
        was = getattr(self, "_fs_chrome_hidden", False)
        if hide and not was:
            self.play._idle_hidden = True
        elif was and not hide:
            self.play._idle_hidden = False
        self._fs_chrome_hidden = hide
        # The toolbar (and its nub) is the PlayPage's own call now -- it folds fullscreen,
        # the saved preference and the idle auto-hide into one decision.
        self.play.refresh_toolbar()
        self.play._reblit_soon()

    def changeEvent(self, e) -> None:  # type: ignore[override]
        # The one place that reliably fires for EVERY route into or out of fullscreen --
        # the F11 hotkey, the toolbar button, the settings checkbox, or the OS window
        # controls -- so the chrome follows the window state no matter who changed it.
        if e.type() == QEvent.Type.WindowStateChange:
            self._sync_fullscreen_chrome()
        super().changeEvent(e)

    def closeEvent(self, e) -> None:  # type: ignore[override]
        # Persist the window size/position, then shut the game down cleanly -- play.stop()
        # commits the cartridge's in-game save (saves/<rom>.flash) before the core is freed.
        self._settings.setValue("win/geometry", self.saveGeometry())
        if self._debug_win is not None:
            self._debug_win.close()
        self._shutdown_net()      # a host/join still searching must not outlive us
        self.play.stop()
        self.library._stop_worker()
        self._settings.sync()
        super().closeEvent(e)


TASKBAR_APP_ID = "NgpCraft.Emulator"


def claim_taskbar_identity(frozen: bool, platform: str) -> str | None:
    """The AppUserModelID to claim, or None to leave Windows' own identity alone.

    ⛔ THE BUG THIS DECIDES. "When i start the emu the icon in taskbar is a generic
    one. If i click pin to taskbar then it displays the right icon" -- user report
    2026-07-28. Everything the icon could have depended on was measured healthy: the
    .ico carries all nine sizes 16..256, the shell call returns S_OK and reads back,
    the window answers WM_GETICON with a live handle for both sizes from source AND
    from the packaged .exe, the .exe carries an extractable icon resource, and moving
    the call before QApplication changes nothing (the window is stamped with no
    per-window PKEY_AppUserModel_ID either way).

    What is left is the identity itself. An EXPLICIT AppUserModelID takes the taskbar
    button off the executable and onto an id that resolves to nothing -- no installer
    here ever registered a shortcut carrying it -- so the button has no app to take an
    icon from. "Pin to taskbar" is the act of CREATING that shortcut, which is exactly
    why pinning fixes it, and fixes it for good.

    ⚖️ SO THE ANSWER IS NOT THE SAME IN BOTH BUILDS, and that is the whole point:

      * frozen (.exe)  -- the executable already IS a proper identity with its own
        embedded icon (spec: icon=assets/icone_ngpcraft.ico). Claim nothing and
        Windows uses it. This is the path an end user is on.
      * from source    -- the executable is python.exe, whose identity and icon are
        not ours and would pool this app with every other Python GUI. There the
        explicit id is still the lesser evil, so keep it.

    📌 AND THAT WAS STILL NOT THE WHOLE BUG: the button also needs an icon it can reach
    without a shortcut existing. That is the other half, see taskbar_window_props below.

    Non-Windows platforms have no such concept; the caller no-ops there.
    """
    if platform != "win32":
        return None
    return None if frozen else TASKBAR_APP_ID


def _claim_windows_taskbar_identity() -> None:
    app_id = claim_taskbar_identity(bool(getattr(sys, "frozen", False)), sys.platform)
    if app_id is None:
        return
    try:
        import ctypes
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(app_id)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# The taskbar BUTTON's own icon (Windows 11)
#
# ⛔ THE BUG THIS FIXES, AND WHY THE 2026-07-28 ROUND WAS NOT ENOUGH. "l'icone ne
# s'affiche pas bien chez certains utilisateurs sous Windows 11 dans la barre du bas
# [...] l'icone devient correcte s'il la pin a la barre d'application" -- 2026-07-30,
# on ANOTHER user's machine, where the .exe's icon in Explorer is CORRECT. So icon
# extraction from the binary works there: the file has the picture, the button does
# not. Measured healthy again from here: the .ico carries all nine sizes 16..256, and
# the shipped .exe carries RT_GROUP_ICON #1 plus all nine RT_ICONs, every one readable.
#
# ⚖️ "Pinning fixes it" is the whole diagnosis. A taskbar button gets its picture from
# the SHELL ITEM its identity resolves to -- a shortcut. A portable .exe that no
# installer ever registered has no shortcut anywhere, so on a machine that happens to
# have one (a pin, a desktop shortcut -- the developer's machine always does) the button
# looks right, and on a clean machine it falls back to the generic icon. Pinning is the
# act of creating the missing shortcut, which is why it fixes it, and fixes it for good.
# Claiming no AppUserModelID (2026-07-28) removed a WRONG identity but could not conjure
# the shortcut the right one still needs.
#
# 🎯 So stop depending on a shortcut existing. Windows lets a window carry its own
# relaunch info, and RelaunchIconResource is exactly "the icon this button must use":
# the shell reads it off the window's property store instead of hunting for a shell
# item, and it is also what the shortcut gets baked with if the user does pin later.
# Three properties, because the shell documents them as one set (icon alone is ignored).
#
# This is strictly ADDITIVE: it hands the button an icon source where it had none, and
# leaves claim_taskbar_identity's decision -- and the .exe's own identity, which is the
# fallback if any of this fails -- exactly as they are.
# ---------------------------------------------------------------------------
TASKBAR_RELAUNCH_NAME = "NgpCraft Emulator"

# {9F4C2855-9F79-4B39-A8D0-E1D42DE1D5F3}, the shell's AppUserModel property set.
_PK_RELAUNCH_COMMAND = 2
_PK_RELAUNCH_ICON = 3
_PK_RELAUNCH_NAME = 4


def taskbar_window_props(exe: str, script: str, icon: str | None,
                         frozen: bool, platform: str) -> dict[str, str] | None:
    """The relaunch properties to stamp on our top-level windows, or None to stamp none.

    Pure on purpose -- the values are the whole correctness of this fix, and they must be
    checkable without a Windows message loop. An icon reference is "<file>,<index>"; the
    two builds do NOT point it at the same file:

      * frozen (.exe) -- the executable itself, whose embedded resource IS our icon
        (spec: icon=assets/icone_ngpcraft.ico). One file, no way for it to go missing.
      * from source    -- the executable is python.exe and its icon is not ours, so the
        reference must be the .ico we ship. Without a bundled icon there is nothing
        honest to point at, so stamp nothing rather than a path that resolves to junk.
    """
    if platform != "win32":
        return None
    if frozen:
        icon_ref = f"{exe},0"
        command = f'"{exe}"'
    else:
        if not icon:
            return None
        icon_ref = f"{icon},0"
        command = f'"{exe}" "{script}"'
    return {"RelaunchIconResource": icon_ref,
            "RelaunchCommand": command,
            "RelaunchDisplayNameResource": TASKBAR_RELAUNCH_NAME}


def _current_taskbar_window_props() -> dict[str, str] | None:
    return taskbar_window_props(
        exe=sys.executable,
        script=str(Path(__file__).resolve()),
        icon=str(APP_ICON) if APP_ICON.is_file() else None,
        frozen=bool(getattr(sys, "frozen", False)),
        platform=sys.platform,
    )


class _PROPERTYKEY(ctypes.Structure):
    class _GUID(ctypes.Structure):
        _fields_ = [("d1", ctypes.c_ulong), ("d2", ctypes.c_ushort),
                    ("d3", ctypes.c_ushort), ("d4", ctypes.c_ubyte * 8)]
    _fields_ = [("fmtid", _GUID), ("pid", ctypes.c_ulong)]


class _PROPVARIANT(ctypes.Structure):
    # 24 bytes on x64: the union starts at offset 8, and we only ever use VT_LPWSTR.
    _fields_ = [("vt", ctypes.c_ushort), ("r1", ctypes.c_ubyte), ("r2", ctypes.c_ubyte),
                ("r3", ctypes.c_ulong), ("pwsz", ctypes.c_wchar_p),
                ("tail", ctypes.c_ulonglong)]


_VT_LPWSTR = 31
_APP_USER_MODEL_FMTID = (0x9F4C2855, 0x9F79, 0x4B39,
                         (0xA8, 0xD0, 0xE1, 0xD4, 0x2D, 0xE1, 0xD5, 0xF3))
_IID_IPROPERTYSTORE = (0x886D8EEB, 0x8CF2, 0x4446,
                       (0x8D, 0x02, 0xCD, 0xBA, 0x1D, 0xBD, 0xCF, 0x99))
_PROP_PIDS = {"RelaunchCommand": _PK_RELAUNCH_COMMAND,
              "RelaunchIconResource": _PK_RELAUNCH_ICON,
              "RelaunchDisplayNameResource": _PK_RELAUNCH_NAME}


def _guid(spec) -> "_PROPERTYKEY._GUID":
    d1, d2, d3, d4 = spec
    return _PROPERTYKEY._GUID(d1, d2, d3, (ctypes.c_ubyte * 8)(*d4))


def _window_property_store(hwnd: int):
    """(store pointer, vtable) for a live top-level window, or (None, None)."""
    iid = _guid(_IID_IPROPERTYSTORE)
    store = ctypes.c_void_p()
    shell32 = ctypes.windll.shell32
    shell32.SHGetPropertyStoreForWindow.argtypes = [
        ctypes.c_void_p, ctypes.POINTER(_PROPERTYKEY._GUID), ctypes.POINTER(ctypes.c_void_p)]
    shell32.SHGetPropertyStoreForWindow.restype = ctypes.c_long
    if shell32.SHGetPropertyStoreForWindow(
            ctypes.c_void_p(hwnd), ctypes.byref(iid), ctypes.byref(store)) < 0:
        return None, None
    vtbl = ctypes.cast(store, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p)))[0]
    return store, vtbl


def _com_call(vtbl, slot: int, *argtypes):
    proto = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p, *argtypes)
    return proto(vtbl[slot])


def stamp_window_taskbar_props(hwnd: int, props: dict[str, str] | None = None) -> bool:
    """Give this window's taskbar button its own icon. True if the shell took every value.

    Best-effort by contract: every failure path leaves the button exactly where it was
    (on the .exe's identity), so this can never make the icon worse than before.
    """
    if sys.platform != "win32" or not hwnd:
        return False
    if props is None:
        props = _current_taskbar_window_props()
    if not props:
        return False
    store = None
    try:
        store, vtbl = _window_property_store(hwnd)
        if store is None:
            return False
        # IPropertyStore: 3 GetCount, 4 GetAt, 5 GetValue, 6 SetValue, 7 Commit.
        set_value = _com_call(vtbl, 6, ctypes.POINTER(_PROPERTYKEY),
                             ctypes.POINTER(_PROPVARIANT))
        commit = _com_call(vtbl, 7)
        ok = True
        for name, value in props.items():
            key = _PROPERTYKEY(_guid(_APP_USER_MODEL_FMTID), _PROP_PIDS[name])
            var = _PROPVARIANT(_VT_LPWSTR, 0, 0, 0, value, 0)
            ok = set_value(store, ctypes.byref(key), ctypes.byref(var)) >= 0 and ok
        return commit(store) >= 0 and ok
    except Exception:
        return False
    finally:
        if store:
            try:
                _com_call(ctypes.cast(store, ctypes.POINTER(
                    ctypes.POINTER(ctypes.c_void_p)))[0], 2)(store)
            except Exception:
                pass


def read_window_taskbar_props(hwnd: int) -> dict[str, str]:
    """What the shell now holds for this window -- the read-back the runtime gate needs."""
    if sys.platform != "win32" or not hwnd:
        return {}
    store, vtbl = _window_property_store(hwnd)
    if store is None:
        return {}
    try:
        get_value = _com_call(vtbl, 5, ctypes.POINTER(_PROPERTYKEY),
                             ctypes.POINTER(_PROPVARIANT))
        out: dict[str, str] = {}
        for name, pid in _PROP_PIDS.items():
            key = _PROPERTYKEY(_guid(_APP_USER_MODEL_FMTID), pid)
            var = _PROPVARIANT()
            if get_value(store, ctypes.byref(key), ctypes.byref(var)) >= 0 \
                    and var.vt == _VT_LPWSTR and var.pwsz:
                out[name] = var.pwsz
        return out
    finally:
        try:
            _com_call(vtbl, 2)(store)
        except Exception:
            pass


def stamp_taskbar_icon(win) -> bool:
    """Stamp a Qt top-level window. Call it AFTER show(): winId must be a real HWND."""
    try:
        return stamp_window_taskbar_props(int(win.winId()))
    except Exception:
        return False


def main() -> int:
    app = QApplication(sys.argv[:1])
    if APP_ICON.is_file():
        app.setWindowIcon(QIcon(str(APP_ICON)))
    _claim_windows_taskbar_identity()

    # --timing legacy  ->  the CPU timing this emulator shipped before 2026-08-21.
    #
    # ⚠️ A FLAG, NOT JUST AN ENVIRONMENT VARIABLE. The switch already existed as
    # NGPCRAFT_TIMING, and telling someone to "run with NGPCRAFT_TIMING=legacy" is not an
    # instruction on Windows -- it is not a command prefix here, and pasting it after the
    # script name makes it a ROM path. An A/B switch nobody can work out how to flip is
    # not a switch. This accepts either spelling and reports which one it took.
    args = [a for a in sys.argv[1:]]
    for i, a in enumerate(list(args)):
        low = a.lower()
        if low in ("--timing=silicon", "--timing=legacy", "--legacy-timing"):
            os.environ["NGPCRAFT_TIMING"] = low.split("=")[-1].replace("--", "")
            args.remove(a); break
        if low == "--timing" and i + 1 < len(args):
            # ⚠️ UNE VALEUR INCONNUE EST REFUSEE, PAS IGNOREE. Le defaut est desormais le
            # modele silicium : une faute de frappe (`--timing legcy`) donnerait donc le
            # silicium en silence, et on croirait comparer alors qu'on mesure deux fois
            # la meme machine. C'est exactement le mode de panne que ce commutateur
            # existe pour eviter -- il a deja coute une demi-journee sur un bisect ou les
            # cinq essais testaient la meme chose.
            choix = args[i + 1].lower()
            if choix not in ("silicon", "legacy"):
                raise SystemExit(f"--timing {args[i + 1]!r} inconnu : "
                                 f"attendu 'silicon' ou 'legacy'.")
            os.environ["NGPCRAFT_TIMING"] = choix
            args.remove(args[i + 1]); args.remove(a); break
    if os.environ.get("NGPCRAFT_TIMING", "").strip().lower() == "legacy":
        print("[timing] ANCIEN modele (avant 2026-08-21), pour attribuer une regression.")
    else:
        print("[timing] modele silicium (defaut). --timing legacy pour comparer.")

    rom = args[0] if args else None
    shell = Shell(rom)
    shell.show()
    # AFTER show(): the property store needs a real HWND, which winId only has once the
    # window exists. This is what puts our picture on the taskbar button (see above).
    stamp_taskbar_icon(shell)
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
