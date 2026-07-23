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
import re
import sys
import time
from collections import deque
from datetime import datetime
from pathlib import Path

from PyQt6.QtCore import Qt, QTimer, QSize, QObject, QThread, QEvent, pyqtSignal
from PyQt6.QtGui import (
    QImage, QPixmap, QKeyEvent, QKeySequence, QFont, QFontMetrics, QIcon,
)
from PyQt6.QtMultimedia import QAudioFormat, QAudioSink, QMediaDevices
from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QLabel, QPushButton, QVBoxLayout,
    QHBoxLayout, QGridLayout, QScrollArea, QStackedWidget, QListWidget,
    QListWidgetItem, QComboBox, QCheckBox, QSlider, QSpinBox, QLineEdit,
    QFileDialog, QSizePolicy, QFrame, QMessageBox, QMenu, QDialog,
    QPlainTextEdit, QDialogButtonBox,
)

sys.path.insert(0, str(Path(__file__).resolve().parent))

from core import native  # noqa: E402
from core import native_session as ns  # noqa: E402  (clock-mode ids live there)
from core.native_session import (  # noqa: E402
    NativeSession,
    SYSTEM_RAM_PATH as _SYSTEM_RAM,
    SYSTEM_RTC_PATH as _SYSTEM_RTC,
    write_rtc_file as _write_rtc_file,
)
from core.frame_pacer import FramePacer  # noqa: E402
from core.watches import WatchSet  # noqa: E402
from core.exec_breaks import ExecBreakSet  # noqa: E402
import ngpc_settings as cfg  # noqa: E402
import ngpc_video  # noqa: E402
import ngpc_library as lib  # noqa: E402
import ngpc_input  # noqa: E402
import ngpc_theme  # noqa: E402
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
DEFAULT_ROM_DIR = REPO / "roms"          # drop your .ngc/.ngp files here (or pick a folder)
DEFAULT_BIOS = REPO / "bios.bin"         # optional: a real NGPC BIOS enables "Boot BIOS"
THUMB_DIR = REPO / "thumbnails"          # auto-rendered covers -- a CACHE, prunable
COVER_DIR = REPO / "covers"              # covers the USER chose -- only ever READ
LIBRARY_DB = REPO / "library.json"       # play counts / last played / favourites
APP_ICON = BUNDLE / "assets" / "icone_ngpcraft.ico"
STATE_DIR = REPO / "savestates"
WATCH_DIR = REPO / "watches"             # per-ROM named memory watches (debugger)
STATE_SLOTS = 8
# Snapshot = the whole working image (I/O + RAM + VRAM, 0x0000..0xBFFF) + the CPU. The
# internal timing (scanline/timers/z80) re-syncs on the next VBlank, so this restores a
# live, playable state without a full-struct C serializer. Cart flash saves live in the
# battery file, not here.
STATE_MEM_LEN = 0x00C000
STATE_MAGIC = b"NGPCST01"
CRASH_DIR = REPO / "crashes"
# stop_status codes the core reports; the ones here mean the ROM did something the CPU
# stopped on (a fault or a not-yet-ported encoding) -- worth a crash report.
_REG_NAMES = ("XWA", "XBC", "XDE", "XHL", "XIX", "XIY", "XIZ", "XSP")
_STATUS_DESC = {
    10: ("SILICON_BROKEN", "encoding that breaks on real silicon"),
    11: ("SILICON_UNDEFINED", "encoding whose result the hardware leaves undefined"),
    12: ("DIVISION_BY_ZERO", "divide by zero / quotient overflow"),
    13: ("BIOS_SHUTDOWN", "the BIOS powered the console off"),
    20: ("UNKNOWN_OPCODE", "byte the decoder does not recognise"),
    21: ("TRUNCATED", "instruction ran off the end of memory"),
    22: ("UNMAPPED", "access to an unmapped address"),
    30: ("UNIMPLEMENTED", "a valid encoding this core has not ported yet"),
}
_CRASH_STATUSES = frozenset(_STATUS_DESC) - {13}   # 13 is a clean power-off, not a crash
THUMB_VERSION = 4       # bump to invalidate the on-disk cache after a render change
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

    def __init__(self, roms: list[Path], bios: Path | None) -> None:
        super().__init__()
        self._roms = roms
        self._bios = bios if bios and bios.exists() else None
        self._stop = False

    def stop(self) -> None:
        self._stop = True

    def run(self) -> None:
        THUMB_DIR.mkdir(exist_ok=True)
        # Prune covers from older render versions so the folder does not grow a
        # copy per THUMB_VERSION bump -- but ONLY files this worker wrote. Anything
        # else in here was put there by hand and stays (see `_AUTO_COVER_RE`).
        keep = f".v{THUMB_VERSION}.png"
        for old in THUMB_DIR.glob("*.png"):
            if not old.name.endswith(keep) and _AUTO_COVER_RE.search(old.name):
                try:
                    old.unlink()
                except OSError:
                    pass
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
            # No BIOS image, no rendering. A cartridge booted without one never
            # reaches its title screen -- it sits on a solid white or black frame
            # forever -- so every cover would come out a blank box. Leaving the
            # placeholder up is both honest and free; `bios.bin` is not distributed,
            # so a fresh clone lands here until the user points at their own dump.
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
        s = NativeSession(rom, bios_path=self._bios, autosave=False)
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

    def __init__(self, rom: Path, long_edge: int, sub: str, fav: bool, fav_tip: str) -> None:
        super().__init__()
        self.setObjectName("card")
        self.rom = rom
        w, h = _art_size(long_edge)
        self.setFixedWidth(w + 16)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(8, 8, 8, 10)
        lay.setSpacing(6)
        self.art = _ArtLabel(w, h)
        name = QLabel(_pretty(rom.stem))
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
                 sub: str, fav: bool, fav_tip: str) -> None:
        super().__init__()
        self.setObjectName("card")
        self.rom = rom
        self.setCursor(Qt.CursorShape.PointingHandCursor)
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
        name = QLabel(_pretty(rom.stem))
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


# ---------------------------------------------------------------- library
class RomReportDialog(QDialog):
    """The ROM analysis, as text you can read and save."""

    def __init__(self, parent, title: str, text: str) -> None:
        super().__init__(parent)
        self.setWindowTitle(f"ROM analysis — {title}")
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
        path, _ = QFileDialog.getSaveFileName(self, "Save report",
                                              f"{title}_analysis.txt", "Text (*.txt)")
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

        header = QHBoxLayout()
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
        self._open_btn = QPushButton()
        self._open_btn.setObjectName("primary")
        self._open_btn.clicked.connect(self._open_rom)
        header.addWidget(self._bios_btn)
        header.addWidget(self._folder_btn)
        header.addWidget(self._open_btn)
        root.addLayout(header)

        # view controls: mode (grid/list/compact) + cover size
        controls = QHBoxLayout()
        controls.setSpacing(8)
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
        self._open_btn.setText(cfg.tr(lang, "open_rom"))
        self._empty.setText(cfg.tr(lang, "no_roms"))
        self._bios_hint.setText(cfg.tr(lang, "covers_need_bios"))
        # Only offer "Boot BIOS" when a BIOS image is actually available.
        self._bios_btn.setVisible(self._bios() is not None)
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
        b = cfg.bios_path(self._settings)
        if b and Path(b).is_file():
            return Path(b)
        return DEFAULT_BIOS if DEFAULT_BIOS.is_file() else None

    def _sync_bios_hint(self) -> None:
        """Covers are rendered by BOOTING each ROM, which takes a BIOS. Say so when
        there is none, next to the blank cards it explains -- and only then."""
        self._bios_hint.setVisible(bool(self._all_roms) and self._bios() is None)

    def reload(self) -> None:
        self._stop_worker()
        # What the covers about to be rendered were rendered WITH (see showEvent).
        self._rendered_with = self._bios()
        d = self._rom_dir()
        self._all_roms = []
        if d:
            # Recurse: point it at a whole projects tree and it finds every ROM inside.
            roms: set[Path] = set()
            for pat in ("*.ngc", "*.ngp", "*.NGC", "*.NGP"):
                try:
                    roms.update(d.rglob(pat))
                except (OSError, ValueError):
                    pass
            self._all_roms = sorted(p for p in roms if p.is_file())
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
        RomReportDialog(self, rom.stem, text).exec()

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
                card = GameCard(rom, size, *decorate(rom))
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
                row = GameRow(rom, row_size, show_art, *decorate(rom))
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
        self._worker = ThumbWorker(roms, self._bios())
        self._worker.moveToThread(self._thread)
        self._thread.started.connect(self._worker.run)
        self._worker.ready.connect(self._on_thumb)
        self._worker.done.connect(self._thread.quit)
        self._thread.start()

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
        path = QFileDialog.getExistingDirectory(self, "ROM folder", cur)
        if path:
            self._settings.setValue("paths/rom_folder", path)
            self.reload()

    def _open_rom(self) -> None:
        cur = cfg.rom_folder(self._settings) or str(DEFAULT_ROM_DIR)
        path, _ = QFileDialog.getOpenFileName(
            self, "Open ROM", cur, "NGPC ROM (*.ngc *.ngp)")
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
            self.retranslate()
            self.reload()
            QTimer.singleShot(0, self._reflow_grid)
            return
        # A game just ended: its play count / playtime / last-played changed, so the
        # cards are stale -- and under "Last played" so is the whole ordering.
        self._arrange()
        # At construction the scroll area has no real width yet, so the first layout
        # uses a fallback and may not fill the window. Re-flow now we are on screen.
        QTimer.singleShot(0, self._reflow_grid)
        # Resume rendering the covers we have not made yet. It was stopped on hide so
        # it never shared the native core with a running game.
        if self._worker is None and self._all_roms:
            todo = [r for r in self._all_roms if str(r) not in self._images]
            if todo:
                self._start_worker(todo)

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
        root.addWidget(self._stack, 1)

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

        self._rows_general = [
            _row(self._lbl_lang, self._lang),
            _row(self._lbl_theme, self._theme),
            _row(self._lbl_shots, shotw),
            _row(self._lbl_savemode, self._savemode),
            _row(self._lbl_flashsize, self._flashsize),
            _row(self._lbl_rewind, self._rewind),
            _row(self._lbl_cartwait, self._cartwait),
        ]
        for r in self._rows_general:
            v.addWidget(r)
        v.addWidget(self._cartwait_hint)
        return w

    # -- Console (BIOS): the machine itself -- its boot, its clock, its coin cell.
    # Grouped here because they are one subject: all three are the CONSOLE's state
    # rather than the emulator's, and two of them are literally the same battery.
    def _bios_panel(self) -> QWidget:
        w, v = self._panel()

        self._bios_edit = QLineEdit(cfg.bios_path(self._settings))
        self._bios_edit.editingFinished.connect(
            lambda: self._settings.setValue("paths/bios", self._bios_edit.text()))
        self._bios_browse = QPushButton(); self._bios_browse.setObjectName("ghost")
        self._bios_browse.clicked.connect(self._pick_bios)
        biosw = QWidget(); bh = QHBoxLayout(biosw); bh.setContentsMargins(0, 0, 0, 0)
        self._bios_edit.setFixedWidth(220)
        bh.addWidget(self._bios_edit); bh.addWidget(self._bios_browse)
        self._lbl_bios = QLabel()

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
            (ns.CLOCK_PAUSED, "clk_paused")]
        self._clockmode = self._combo("bios/clock_mode", self._clock_items,
                                      cfg.clock_mode(self._settings))
        self._lbl_clockmode = QLabel()
        self._clockmode_hint = QLabel()
        self._clockmode_hint.setObjectName("hint")
        self._clockmode_hint.setWordWrap(True)

        # Pulling the coin cell. Destructive and not undoable, so it asks first.
        self._coincell_btn = QPushButton(); self._coincell_btn.setObjectName("ghost")
        self._coincell_btn.clicked.connect(self._reset_coin_cell)
        self._lbl_coincell = QLabel()
        self._coincell_hint = QLabel()
        self._coincell_hint.setObjectName("hint")
        self._coincell_hint.setWordWrap(True)

        for r in (_row(self._lbl_bios, biosw),
                  _row(self._lbl_realbios, self._realbios)):
            v.addWidget(r)
        v.addWidget(self._realbios_hint)
        v.addWidget(_row(self._lbl_clockmode, self._clockmode))
        v.addWidget(self._clockmode_hint)
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

        self._filter_items = [
            (vid.FILTER_NONE, "flt_none"), (vid.FILTER_SCANLINES, "flt_scanlines"),
            (vid.FILTER_LCD_GRID, "flt_lcdgrid"), (vid.FILTER_CRT, "flt_crt")]
        self._filter = self._combo("gfx/filter", self._filter_items,
                                   cfg.video_filter(self._settings))
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
        self._showfps = QCheckBox(); self._showfps.setChecked(cfg.show_fps(self._settings))
        self._showfps.toggled.connect(
            lambda b: (self._settings.setValue("gfx/show_fps", b), self.changed.emit()))

        self._lbl_scale = QLabel(); self._lbl_filter = QLabel(); self._lbl_color = QLabel()
        self._lbl_aspect = QLabel(); self._lbl_smooth = QLabel(); self._lbl_fs = QLabel()
        self._lbl_showfps = QLabel(); self._lbl_mono = QLabel()
        for r in (_row(self._lbl_scale, self._scale),
                  _row(self._lbl_mono, self._monobox),
                  _row(self._lbl_filter, self._filter),
                  _row(self._lbl_color, self._colorbox),
                  _row(self._lbl_aspect, self._aspectbox),
                  _row(self._lbl_smooth, self._smooth),
                  _row(self._lbl_showfps, self._showfps),
                  _row(self._lbl_fs, self._fs)):
            v.addWidget(r)
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
        # The bindings live on a picture of the console rather than in a list: the
        # field for "Left" sits next to the actual left of the d-pad. Same widgets,
        # same settings keys -- only the arrangement carries the meaning now.
        self._bindmap = ngpc_bindmap.BindMap(self._settings)
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
        self._pad_state = QLabel(); self._pad_state.setObjectName("hint")
        v.addWidget(self._pad_state)
        # A pad can be plugged in while this page is open, so the readout polls.
        self._pad_probe = ngpc_input.XInputPad()
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

    def _restore_keys(self) -> None:
        for label, btn in self._keybtns.items():
            code = cfg.DEFAULT_KEYS.get(label, 0)
            btn._key = code           # noqa: SLF001
            btn._render()             # noqa: SLF001
            cfg.set_binding(self._settings, label, code)
        self._settings.sync()
        self._refresh_conflicts()
        self.changed.emit()

    def _pick_bios(self) -> None:
        cur = cfg.bios_path(self._settings) or str(DEFAULT_ROM_DIR)
        path, _ = QFileDialog.getOpenFileName(self, "BIOS image", cur, "BIOS (*.bin *.rom);;All (*)")
        if path:
            self._bios_edit.setText(path)
            self._settings.setValue("paths/bios", path)

    def _pick_shots(self) -> None:
        cur = cfg.screenshot_dir(self._settings) or str(REPO / "screenshots")
        path = QFileDialog.getExistingDirectory(self, "Screenshots folder", cur)
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
        for i, (_val, key) in enumerate(self._clock_items):
            self._clockmode.setItemText(i, t(key))
        self._clockmode_hint.setText(t("clock_mode_hint"))
        self._lbl_coincell.setText(t("coin_cell"))
        self._coincell_btn.setText(t("coin_cell_reset"))
        self._coincell_hint.setText(t("coin_cell_hint"))
        self._lbl_cartwait.setText(t("cart_wait")); self._cartwait_hint.setText(t("cart_wait_hint"))
        self._lbl_scale.setText(t("lcd_scale")); self._lbl_smooth.setText(t("smoothing"))
        self._lbl_filter.setText(t("filter")); self._lbl_color.setText(t("color_profile"))
        self._lbl_aspect.setText(t("aspect")); self._lbl_fs.setText(t("fullscreen"))
        self._lbl_showfps.setText(t("show_fps"))
        self._lbl_mono.setText(t("mono_mode")); self._mono_hint.setText(t("mono_mode_hint"))
        for box, items in ((self._filter, self._filter_items),
                           (self._colorbox, self._color_items),
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
class PlayPage(QWidget):
    exit_requested = pyqtSignal()
    debug_requested = pyqtSignal()
    options_requested = pyqtSignal(str)   # "video" | "audio" | "controls"

    def __init__(self, settings, library: lib.Library) -> None:
        super().__init__()
        self.setObjectName("page")
        self._settings = settings
        self._lib = library
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
        self._pad = ngpc_input.XInputPad()
        self._pad_on = True                # the setting; the pad may still be absent
        self._pad_held = 0                 # controller mask, merged with `self.held`
        self._turbo_mask = 0               # joypad bits that autofire while held
        self._turbo_hz = 10
        self._frame = 0                    # free-running EMULATED frame counter
        self.pending = bytearray()
        self.watches = WatchSet()          # named memory watches, loaded per-ROM
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
        # Symbol table for breakpoint CONDITIONS, so a guard can say `[_player_hp] == 0`.
        # The debug window owns the loading and pushes it here.
        self.symbols = None
        self.pacer = FramePacer()
        self.sink = None
        self.audio = None
        self.debt = 0.0
        self.wall_last = time.perf_counter()

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        self.lcd = QLabel(); self.lcd.setObjectName("lcd")
        self.lcd.setAlignment(Qt.AlignmentFlag.AlignCenter)   # centres the (letterboxed) frame
        outer.addWidget(self.lcd, 1)                          # stretch 1 -> fills the page
        self.overlay = QLabel(""); self.overlay.setObjectName("overlay")
        self.overlay.setAlignment(Qt.AlignmentFlag.AlignCenter)
        outer.addWidget(self.overlay, 0, Qt.AlignmentFlag.AlignCenter)
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
        self._bar_tips.append((self._bar_show, "Show toolbar", cfg.HK_TOOLBAR))
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

    def _bios_path(self) -> Path | None:
        b = cfg.bios_path(self._settings)
        if b and Path(b).is_file():
            return Path(b)
        return DEFAULT_BIOS if DEFAULT_BIOS.is_file() else None

    def start(self, rom: Path) -> None:
        """Boot a game. Default is the instant hand-off (a running console handed
        to the cartridge). 'Console boot' runs the real BIOS power-on first: the NMI
        power-manager boots the BIOS, it plays its NEO GEO POCKET intro, and once it
        settles into its pre-boot shell loop `_bios_handoff_assist` hands off to the
        cartridge -- so the game boots on its own after the intro, like real hardware.
        A first-boot (unconfigured coin cell) still stops at the language/date setup."""
        self.stop()
        self._rom_path = Path(rom)
        self.watches.load(self._watch_path())   # this ROM's named watches, if any
        self.breaks.load(self._break_path())    # ...and its execution breakpoints
        self._crashed = False
        self._real_bios = cfg.real_bios(self._settings) and self._bios_path() is not None
        self._power_pressed = False
        mode = cfg.save_mode(self._settings)
        cap = cfg.flash_capacity_bytes(self._settings, Path(rom).stat().st_size)
        self.session = NativeSession(
            rom, bios_path=self._bios_path(), real_bios=self._real_bios,
            save_to_rom=mode in (cfg.SAVE_ROM, cfg.SAVE_BOTH),
            sidecar=mode in (cfg.SAVE_SIDECAR, cfg.SAVE_BOTH),
            flash_size=cap, clock_mode=cfg.clock_mode(self._settings),
            k1ge_console=cfg.mono_mode(self._settings) == cfg.MONO_K1GE)
        self.machine = self.session.machine
        # Silicon-calibrated cart-flash wait-states so self-timed games run at their
        # real 30fps instead of ~2x too fast. See cfg.cart_wait_states / project memo.
        if cfg.cart_wait_states(self._settings):
            self.machine.set_cart_wait(cfg.CART_FETCH_WAIT)
            self.machine.set_cart_data_wait(cfg.CART_DATA_WAIT)
            # LDIR block-copy timing: the last, strongly-evidenced but not-yet-ROM-confirmed
            # piece that takes self-timed games (Cool Boarders) to their hardware 30fps.
            self.machine.set_ldir_cost(cfg.CART_LDIR_COST)
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
        if bios is None:
            self.exit_requested.emit()
            return
        self._real_bios = True
        self._power_pressed = False
        # No cartridge: clear the last game's path, or the console's own BIOS screen
        # would be credited as playtime to whatever was played before it.
        self._rom_path = None
        self._raw = native.NativeMachine(_NO_CART, bios=bios.read_bytes())
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
        if cfg.audio_enabled(self._settings):
            self._open_audio()
        self.setFocus()
        self.timer.start(4)

    def stop(self) -> None:
        self.timer.stop()
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
        self._bindings = cfg.key_bindings(self._settings)
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
        show_bar = bool(self._settings.value("gfx/toolbar", True, type=bool))
        self.toolbar.setVisible(show_bar); self._bar_show.setVisible(not show_bar)
        if not show_bar:
            self._position_bar_show()
        # Real fullscreen on the top-level window (was previously only a canvas-fill flag).
        win = self.window()
        if win is not None:
            if self._fullscreen and not win.isFullScreen():
                win.showFullScreen()
            elif not self._fullscreen and win.isFullScreen():
                win.showNormal()
        if self.machine is not None:
            self._blit()          # re-fit after any settings change

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

    def _toggle_pause(self) -> None:
        self.paused = not self.paused
        self.overlay.setText(
            cfg.tr(cfg.language(self._settings), "paused") if self.paused else "")

    def _begin_fast_forward(self) -> None:
        self._ff = True
        self.wall_last = time.perf_counter(); self.debt = 0.0

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
            cfg.HK_FF: lambda: (setattr(self, "_ff", self._ff_btn.isChecked()),
                                setattr(self, "wall_last", time.perf_counter()),
                                setattr(self, "debt", 0.0)),
            cfg.HK_REWIND: self.stop_rewind,
        }

    def keyPressEvent(self, e: QKeyEvent) -> None:  # noqa: N802
        k = e.key()
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
        if self.session is not None:
            self.session.system_ram_baseline = coin
        # ⚡ THE CLOCK IS THE OTHER HALF OF THAT COIN CELL, and it must be carried over the
        # hand-off BY HAND. Two things would otherwise destroy it here: we blank the coin
        # cell for the reset, and the hand-off reset boots the BIOS internally to capture
        # its character RAM -- a boot that, seeing a blank cell, takes the dead-battery path
        # and stamps 1998-01-01 over the chip. So the date the BIOS just showed the player
        # would be gone by the time the game reads it. Snapshot it, hand it straight back.
        clock = self.machine.rtc()
        self.machine.set_battery_ram(b"")   # the game boots with clean work RAM (instant state)
        self.machine.reset(bios_handoff=True)
        self.machine.set_battery_ram(coin)  # coin cell back in the buffer (mem stays clean)
        self.machine.set_rtc(clock)         # and the clock the BIOS was running
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
        if self.session is not None:
            self.session.reboot()
        elif self._raw is not None:
            self._raw.reset(real_bios=True)
        self.held = 0
        self._power_pressed = False
        self._crashed = False
        self._bp_step_off = False
        self._did_handoff = False; self._menu_ticks = 0; self._bios_frames = 0
        self._rewind.clear(); self._rw_pos = None
        self.watches.rearm()
        self.apply_debug()                 # a reboot clears core breakpoints -> re-arm

    def _toggle_fullscreen(self) -> None:
        self._settings.setValue("gfx/fullscreen", not cfg.fullscreen(self._settings))
        self.apply_settings()
        if self.machine is not None:
            self._blit()

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
        if self.machine is not None:
            self._blit()

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

        btn("☰", "Menu", self.open_menu, action=cfg.HK_MENU)
        btn("⟲", "Reset", self._do_reset, action=cfg.HK_RESET)
        h.addSpacing(10)
        h.addWidget(QLabel("Slot"))
        self._slot_spin = QSpinBox(); self._slot_spin.setRange(1, STATE_SLOTS)
        self._slot_spin.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._slot_spin.valueChanged.connect(lambda v: setattr(self, "_slot", v - 1))
        h.addWidget(self._slot_spin)
        btn("💾", "Save state", lambda: self.save_state(), action=cfg.HK_SAVE)
        btn("📂", "Load state", lambda: self.load_state(), action=cfg.HK_LOAD)
        h.addSpacing(10)
        btn("📷", "Screenshot", self.screenshot, action=cfg.HK_SHOT)
        h.addSpacing(10)
        rw = QPushButton("⏪"); rw.setObjectName("barBtn")
        rw.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        rw.pressed.connect(self.start_rewind); rw.released.connect(self.stop_rewind)
        self._bar_tips.append((rw, "Hold to rewind", cfg.HK_REWIND))
        h.addWidget(rw)
        btn("⏵", "Step one frame forward", self.step_forward, action=cfg.HK_STEP)
        h.addSpacing(10)
        btn("−", "Slower", lambda: self.cycle_speed(False), action=cfg.HK_SLOWER)
        self._speed_lbl = QLabel("1×"); self._speed_lbl.setObjectName("barSpeed")
        self._speed_lbl.setFixedWidth(36)
        self._speed_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        h.addWidget(self._speed_lbl)
        btn("+", "Faster", lambda: self.cycle_speed(True), action=cfg.HK_FASTER)
        self._ff_btn = btn("⏩", "Fast-forward toggle (or hold)", self._set_ff,
                           checkable=True, action=cfg.HK_FF)
        h.addStretch()
        btn("⛶", "Fullscreen", self._toggle_fullscreen, action=cfg.HK_FS)
        btn("▾", "Hide toolbar", lambda: self._toggle_toolbar(False), action=cfg.HK_TOOLBAR)
        self._refresh_toolbar_tips()
        return bar

    def _refresh_toolbar_tips(self) -> None:
        """Re-stamp each toolbar tooltip with the key that is actually bound now."""
        for button, base, action in self._bar_tips:
            if action is None:
                button.setToolTip(base)
                continue
            key = QKeySequence(cfg.hotkey_code(self._settings, action)).toString()
            button.setToolTip(f"{base} ({key})" if key else base)

    def _toggle_toolbar(self, show: bool | None = None) -> None:
        show = (not self.toolbar.isVisible()) if show is None else show
        self.toolbar.setVisible(show)
        self._bar_show.setVisible(not show)
        if not show:
            self._position_bar_show()
        self._settings.setValue("gfx/toolbar", show)
        self.setFocus()

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
        self._ff = bool(on)
        self._reset_pacing()

    # ---- save states (per-ROM slots) --------------------------------------
    def _state_path(self, slot: int) -> Path | None:
        if self._rom_path is None:
            return None
        STATE_DIR.mkdir(exist_ok=True)
        return STATE_DIR / f"{self._rom_path.stem}.s{slot}"

    def _flash(self, msg: str) -> None:
        """Briefly show a status line over the game."""
        self.overlay.setText(msg)
        QTimer.singleShot(1100, lambda: self.overlay.setText(
            cfg.tr(cfg.language(self._settings), "paused") if self.paused else ""))

    def set_slot(self, slot: int) -> None:
        self._slot = max(0, min(STATE_SLOTS - 1, slot))
        if hasattr(self, "_slot_spin"):
            self._slot_spin.setValue(self._slot + 1)
        self._flash(cfg.tr(cfg.language(self._settings), "slot").format(n=self._slot + 1))

    # A snapshot is the CPU struct + the whole working image (I/O + RAM + VRAM). The
    # rewind ring and the save-state slots share it; slots just add a magic + go to disk.
    def _capture_state(self) -> bytes:
        return bytes(self.machine.cpu()) + self.machine.read(0, STATE_MEM_LEN)

    def _apply_state(self, body: bytes) -> None:
        cpu_len = ctypes.sizeof(type(self.machine.cpu()))
        cpu = type(self.machine.cpu()).from_buffer_copy(body[:cpu_len])
        self.machine.write(0, body[cpu_len:cpu_len + STATE_MEM_LEN])
        self.machine.set_cpu(cpu)

    def save_state(self, slot: int | None = None) -> None:
        if self.machine is None:
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
        slot = self._slot if slot is None else slot
        path = self._state_path(slot)
        if path is None or not path.is_file():
            self._flash(cfg.tr(cfg.language(self._settings), "state_empty").format(n=slot + 1))
            return
        blob = path.read_bytes()
        if not blob.startswith(STATE_MAGIC):
            self._flash("bad state"); return
        self._apply_state(blob[len(STATE_MAGIC):])
        self._rewind.clear(); self._rw_pos = None      # a loaded state starts a new timeline
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
        if self.machine is None:
            return
        self.paused = False
        self._rewinding = True
        self._rw_accum = 0

    def stop_rewind(self) -> None:
        """Release rewind -> resume normal forward play, cleanly (fresh pacing + sink so
        the audio clock never stalls)."""
        if not self._rewinding:
            return
        self._rewinding = False
        self._rw_pos = None
        self.overlay.setText("")
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
        self._flash(f"⏪ −{len(self._rewind) - 1 - self._rw_pos} f")

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
        self._drain_audio_silently()
        self._reset_pacing()
        self._restart_audio()          # a fresh sink -> pacing can never stay stalled

    def _rebuild_rewind_buffer(self) -> None:
        """Size the rewind ring from the setting (0 s = off). ~48 KiB per frame."""
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
        L.append(f"timing    : cart_wait={cfg.CART_FETCH_WAIT} data={cfg.CART_DATA_WAIT}"
                 f" ldir={cfg.CART_LDIR_COST}   real_bios={self._real_bios}")
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
    def screenshot(self) -> None:
        if self.machine is None:
            return
        folder = cfg.screenshot_dir(self._settings) or str(REPO / "screenshots")
        d = Path(folder); d.mkdir(parents=True, exist_ok=True)
        scale = max(2, min(6, self._scale or 4))
        pix = ngpc_video.render_pixmap(
            self.machine.framebuffer(), scale, self._filter, self._color, self._smooth)
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
        self._speed = steps[i]
        self._reset_pacing()
        if hasattr(self, "_speed_lbl"):
            self._speed_lbl.setText(f"{self._speed:g}×")
        self._flash(cfg.tr(cfg.language(self._settings), "speed").format(x=self._speed))

    def open_menu(self) -> None:
        if self.machine is None:
            return
        self.paused = True
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
        if self.machine is not None:
            self._blit()
        if not self.toolbar.isVisible():
            self._position_bar_show()
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

    def _tick(self) -> None:
        if self.machine is None:
            return
        self._poll_pad()
        if self._rewinding:                  # held rewind: run the game BACKWARD
            self._rw_accum += 1
            if self._rw_accum >= 4:          # ~60 fps reverse (the timer ticks ~4 ms)
                self._rw_accum = 0
                if len(self._rewind) > 1:
                    self._rewind.pop()       # drop the current frame...
                    self._apply_state(self._rewind[-1])   # ...show the one before it
                self._drain_audio_silently()
                self.overlay.setText(f"⏪ {len(self._rewind)}")
                self._blit()
            return
        if self.paused:
            return
        if self._rw_pos is not None:         # resuming after a frame-step scrub
            self._leave_rewind()
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
        # While the access probe owns the logs, watchpoints are suspended (see
        # apply_debug) -- so do not try to match against a window it is not watching.
        probing = self.access_probe is not None
        wrange = None if probing else self.watches.write_range()
        rrange = None if probing else self.watches.read_range()
        locked = self.watches.locked()
        ran = 0
        emu_t0 = time.perf_counter()
        for _ in range(due):
            # ⚡ ONCE PER FRAME, not once per tick. This used to be written above the
            # loop, so every frame of a multi-frame batch saw one frozen joypad state.
            # That is invisible for a held button but fatal to autofire: the press
            # train would come out at the tick rate (and at whatever the batch size
            # happened to be) instead of the rate that was asked for.
            self.machine.write(0x00B0, bytes([self._joypad_byte()]))
            self._frame += 1
            if wrange is not None:               # fresh per-frame write capture
                self.machine.set_write_log(wrange[0], wrange[1])
            if rrange is not None:               # ...and read capture
                self.machine.set_read_log(rrange[0], rrange[1])
            if probing:                          # fresh window per frame for the viewer
                self.machine.set_write_log(*self.access_probe)
                self.machine.set_read_log(*self.access_probe)
            summ = self.machine.run_frames(1)
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

    def _blit(self) -> None:
        if self.machine is None:
            return
        bw, bh = max(SCREEN_W, self.lcd.width()), max(SCREEN_H, self.lcd.height())
        # Render the filter/scanlines at the integer scale nearest the display box (so the
        # grid lines look right at this size), then fit_pixmap does the fractional remainder.
        k = max(1, min(bw // SCREEN_W, bh // SCREEN_H))
        k = min(k, 8)                                    # cap the numpy cost
        pix = ngpc_video.render_pixmap(
            self.machine.framebuffer(), k, self._filter, self._color, self._smooth)
        pix = ngpc_video.fit_pixmap(pix, bw, bh, self._aspect, self._smooth)
        self.lcd.setPixmap(pix)


# ---------------------------------------------------------------- shell
class Shell(QMainWindow):
    def __init__(self, rom: str | None = None) -> None:
        super().__init__()
        self._settings = cfg.make_settings()
        self.setWindowTitle("NgpCraft Emulator")
        if APP_ICON.is_file():
            self.setWindowIcon(QIcon(str(APP_ICON)))
        # Restore the last window size/position; fall back to a sensible default.
        geo = self._settings.value("win/geometry")
        if geo is not None:
            self.restoreGeometry(geo)
        else:
            self.resize(980, 660)
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
        self._rail_toggle.setFixedSize(26, 26); self._rail_toggle.setToolTip("Collapse / expand sidebar")
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
        self.settings.changed.connect(self._on_settings_changed)
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
        self._go(0)
        self._toggle_rail(not bool(self._settings.value("win/rail_collapsed", False, type=bool)))
        if rom:
            self._launch(rom)

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

    def _toggle_rail(self, show: bool | None = None) -> None:
        # Collapse to a thin strip that still holds the toggle (no overlap of content).
        show = (self._rail.width() < 100) if show is None else show
        self._rail.setFixedWidth(self._rail_w if show else RAIL_COLLAPSED_W)
        for wdg in self._rail_hideable:
            wdg.setVisible(show)
        self._rail_toggle.setText("‹" if show else "☰")
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
        self._fit_rail()               # the new labels may not be the old width
        self.library.retranslate()
        self.settings.retranslate()

    def _update_rail(self, idx: int) -> None:
        playing = self.play.machine is not None
        self._nav_resume.setVisible(playing)
        self._nav_resume.setChecked(idx == 2)
        self._nav_lib.setChecked(idx == 0)
        self._nav_set.setChecked(idx == 1)
        self.settings.set_resume_visible(playing and idx == 1)

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
            QMessageBox.warning(self, "Cannot start game",
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

    def _open_debug(self) -> None:
        if self._debug_win is None:
            self._debug_win = DebugWindow(self, self._settings)
        self._debug_win.attach(self.play)     # may be None -> "no game running"
        self._debug_win.show(); self._debug_win.raise_()
        self._debug_win.activateWindow()

    def _to_library(self) -> None:
        self.play.stop()
        if self._debug_win is not None:
            self._debug_win.attach(None)
        self._go(0)

    def _on_settings_changed(self) -> None:
        if self.play.machine is not None:
            self.play.apply_settings()

    def closeEvent(self, e) -> None:  # type: ignore[override]
        # Persist the window size/position, then shut the game down cleanly -- play.stop()
        # commits the cartridge's in-game save (saves/<rom>.flash) before the core is freed.
        self._settings.setValue("win/geometry", self.saveGeometry())
        if self._debug_win is not None:
            self._debug_win.close()
        self.play.stop()
        self.library._stop_worker()
        self._settings.sync()
        super().closeEvent(e)


def main() -> int:
    app = QApplication(sys.argv[:1])
    if APP_ICON.is_file():
        app.setWindowIcon(QIcon(str(APP_ICON)))
        # Make Windows show our icon in the taskbar (not the generic python.exe one).
        try:
            import ctypes
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("NgpCraft.Emulator")
        except Exception:
            pass
    rom = sys.argv[1] if len(sys.argv) > 1 else None
    shell = Shell(rom)
    shell.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
