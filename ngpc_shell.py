"""NgpCraft — the modern front-end.

A clean, PPSSPP-shaped shell over the FAST native core: a left rail (Library /
Settings), a game LIBRARY of cover cards with live thumbnails, a categorized
SETTINGS screen (General / Graphics / Audio / Controls), and a PLAY view with
sound and configurable keys. One dark theme, one window, no debugger clutter.

    python ngpc_shell.py                 # open the library
    python ngpc_shell.py "<rom>.ngc"     # boot straight into a game

The old PyQt debugger (`ngpc_emu_ui_qt.py`) stays for register-level work; this
is the everyday player. Both drive the same emulation; this one uses the C++
core for real-time speed and audio (see scripts/play.py for the pacing story).
"""

from __future__ import annotations

import ctypes
import hashlib
import sys
import time
from collections import deque
from datetime import datetime
from pathlib import Path

from PyQt6.QtCore import Qt, QTimer, QSize, QObject, QThread, QEvent, pyqtSignal
from PyQt6.QtGui import QImage, QPixmap, QKeyEvent, QFont, QIcon
from PyQt6.QtMultimedia import QAudioFormat, QAudioSink, QMediaDevices
from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QLabel, QPushButton, QVBoxLayout,
    QHBoxLayout, QGridLayout, QScrollArea, QStackedWidget, QListWidget,
    QListWidgetItem, QComboBox, QCheckBox, QSlider, QSpinBox, QLineEdit,
    QFileDialog, QSizePolicy, QFrame, QMessageBox,
)

sys.path.insert(0, str(Path(__file__).resolve().parent))

from core import native  # noqa: E402
from core.native_session import NativeSession, SYSTEM_RAM_PATH as _SYSTEM_RAM  # noqa: E402
from core.frame_pacer import FramePacer  # noqa: E402
from core.watches import WatchSet  # noqa: E402
from core.exec_breaks import ExecBreakSet  # noqa: E402
import ngpc_settings as cfg  # noqa: E402
import ngpc_video  # noqa: E402
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
DEFAULT_ROM_DIR = REPO / "roms"          # drop your .ngc/.ngp files here (or pick a folder)
DEFAULT_BIOS = REPO / "bios.bin"         # optional: a real NGPC BIOS enables "Boot BIOS"
THUMB_DIR = REPO / "thumbnails"
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
THUMB_VERSION = 3       # bump to invalidate the on-disk cache after a render change
# Frames to sample for a thumbnail. We keep the RICHEST one so a boot logo, a
# fade-to-black or a mono fade-to-white does not become the cover, and we sample
# DEEP (titles/attract can be late) but STOP EARLY once a frame is clearly a real
# screen -- so a colourful game costs one sample, only the stubborn ones cost six.
THUMB_SAMPLE_FRAMES = (360, 600, 840, 1120, 1500, 1900)
THUMB_GOOD_ENOUGH = 22   # distinct-colour score that ends the search early

ACCENT = "#4aa3ff"
STYLE = f"""
* {{ color: #e7e9ee; font-family: 'Segoe UI', system-ui, sans-serif; }}
QMainWindow, QWidget#page {{ background: #15171c; }}
QWidget#rail {{ background: #101216; }}
QLabel#appTitle {{ font-size: 18px; font-weight: 700; padding: 14px 12px; }}
QLabel#pageTitle {{ font-size: 22px; font-weight: 700; }}
QLabel#hint {{ color: #8b93a3; }}
QPushButton#rail {{
    text-align: left; padding: 11px 16px; border: none; border-radius: 8px;
    font-size: 14px; background: transparent; color: #b8c0cf;
}}
QPushButton#rail:hover {{ background: #1c2028; color: #ffffff; }}
QPushButton#rail:checked {{ background: {ACCENT}22; color: {ACCENT}; font-weight: 600; }}
QPushButton#primary {{
    background: {ACCENT}; color: #06121f; border: none; border-radius: 8px;
    padding: 9px 18px; font-weight: 600;
}}
QPushButton#primary:hover {{ background: #6fb6ff; }}
QPushButton#ghost {{
    background: #21252e; border: 1px solid #2c313c; border-radius: 8px;
    padding: 8px 16px;
}}
QPushButton#ghost:hover {{ border-color: {ACCENT}; }}
QFrame#card {{ background: #1b1e25; border: 1px solid #262b34; border-radius: 12px; }}
QFrame#card:hover {{ border-color: {ACCENT}; background: #20242d; }}
QLabel#cardName {{ font-size: 12px; color: #cfd5e0; padding: 0 6px; }}
QFrame#settingRow {{ background: #1b1e25; border-radius: 10px; }}
QListWidget#cats {{ background: transparent; border: none; font-size: 14px; outline: 0; }}
QListWidget#cats::item {{ padding: 10px 14px; border-radius: 8px; margin: 2px 6px; }}
QListWidget#cats::item:selected {{ background: {ACCENT}22; color: {ACCENT}; }}
QComboBox, QLineEdit, QSpinBox {{
    background: #12141a; border: 1px solid #2c313c; border-radius: 7px; padding: 6px 8px;
}}
QComboBox:hover, QLineEdit:hover {{ border-color: {ACCENT}; }}
QSlider::groove:horizontal {{ height: 6px; background: #2c313c; border-radius: 3px; }}
QSlider::handle:horizontal {{ width: 16px; background: {ACCENT}; border-radius: 8px; margin: -6px 0; }}
QCheckBox {{ spacing: 8px; }}
QScrollArea {{ border: none; }}
QLabel#lcd {{ background: #000000; border-radius: 6px; }}
QLabel#overlay {{ font-size: 20px; font-weight: 700; }}
QLabel#osd {{ color: #7CFC7C; font-weight: 700; font-family: "Consolas", monospace;
  background: rgba(0,0,0,0.45); border-radius: 4px; padding: 2px 6px; }}
QFrame#playbar {{ background: #14181f; border-top: 1px solid #262c36; }}
QPushButton#barBtn {{ background: #1c222b; color: #cfd6e0; border: 1px solid #2c333e;
  border-radius: 5px; padding: 4px 9px; font-size: 15px; }}
QPushButton#barBtn:hover {{ background: #262d38; }}
QPushButton#barBtn:checked {{ background: #2f6feb; color: #ffffff; border-color: #2f6feb; }}
QLabel#barSpeed {{ color: #7CFC7C; font-weight: 700; font-family: "Consolas", monospace; }}
QPushButton#barShow {{ background: rgba(20,24,31,0.85); color: #cfd6e0;
  border: 1px solid #2c333e; border-top-left-radius: 5px; border-top-right-radius: 5px; }}
QPushButton#railToggle {{ background: transparent; color: #7a8494; border: none;
  font-size: 18px; font-weight: 700; }}
QPushButton#railToggle:hover {{ color: #cfd6e0; }}
QWidget#overlayMenu {{ background: rgba(10, 12, 16, 0.72); }}
QFrame#menuPanel {{ background: #191d25; border: 1px solid #2c313c; border-radius: 14px; }}
QLabel#menuTitle {{ font-size: 18px; font-weight: 700; color: #ffffff; }}
QPushButton#menuItem {{
    text-align: left; padding: 11px 16px; border: none; border-radius: 9px;
    font-size: 15px; background: transparent; color: #cdd3de;
}}
QPushButton#menuItem:hover {{ background: #232833; color: #ffffff; }}
QPushButton#menuItem[sel="true"] {{ background: {ACCENT}; color: #06121f; font-weight: 600; }}
"""


# ---------------------------------------------------------------- thumbnails
def _cover_path(rom: Path) -> Path:
    """The on-disk cover cache for a ROM -- UNIQUE per full path. Two projects can
    each hold a `main.ngc`; a stem-only name would make them share one cover (and,
    with a recursive scan, overwrite each other)."""
    tag = hashlib.md5(str(rom).encode("utf-8", "surrogatepass")).hexdigest()[:8]
    return THUMB_DIR / f"{rom.stem}.{tag}.v{THUMB_VERSION}.png"


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
        # copy per THUMB_VERSION bump.
        keep = f".v{THUMB_VERSION}.png"
        for old in THUMB_DIR.glob("*.png"):
            if not old.name.endswith(keep):
                try:
                    old.unlink()
                except OSError:
                    pass
        for rom in self._roms:
            if self._stop:
                break
            cache = _cover_path(rom)
            if cache.exists():
                img = QImage(str(cache))
                if not img.isNull():
                    self.ready.emit(str(rom), img)
                    continue
            try:
                img = self._render(rom)
            except Exception:
                continue
            if img is not None:
                img.save(str(cache))
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
        if best_fb is None:
            return None
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
            "background:#0c0e12; border-radius:8px; color:#2c313c;"
            f" font-size:{max(16, self._h // 4)}px;")

    def set_image(self, img: QImage) -> None:
        self.setStyleSheet("background:#0c0e12; border-radius:8px;")
        self.setPixmap(QPixmap.fromImage(img).scaled(
            self._w, self._h, Qt.AspectRatioMode.IgnoreAspectRatio,
            Qt.TransformationMode.FastTransformation))
        self.setText("")


class GameCard(QFrame):
    """A grid cover: art on top, name under it."""

    clicked = pyqtSignal(str)

    def __init__(self, rom: Path, long_edge: int) -> None:
        super().__init__()
        self.setObjectName("card")
        self.rom = rom
        w, h = _art_size(long_edge)
        self.setFixedWidth(w + 16)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(8, 8, 8, 10)
        lay.setSpacing(8)
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

    def set_image(self, img: QImage) -> None:
        self.art.set_image(img)

    def mousePressEvent(self, e) -> None:  # type: ignore[override]
        if e.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit(str(self.rom))


class GameRow(QFrame):
    """A list row: a small cover (optional) + the name, full width."""

    clicked = pyqtSignal(str)

    def __init__(self, rom: Path, long_edge: int, show_art: bool) -> None:
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
class LibraryPage(QWidget):
    play_requested = pyqtSignal(str)
    boot_bios_requested = pyqtSignal()

    def __init__(self, settings) -> None:
        super().__init__()
        self.setObjectName("page")
        self._settings = settings
        self._items: dict[str, QWidget] = {}     # rom -> current card/row widget
        self._images: dict[str, QImage] = {}      # rom -> thumbnail, kept for reflow
        self._grid = None                         # QGridLayout when in grid view
        self._grid_cards: list[QWidget] = []      # cards in order, for re-flow
        self._grid_cols = 0
        self._grid_card_w = 0
        self._roms: list[Path] = []
        self._thread: QThread | None = None
        self._worker: ThumbWorker | None = None

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
        root.addLayout(controls)

        self._empty = QLabel()
        self._empty.setObjectName("hint")
        self._empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        root.addWidget(self._empty)

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
        # Only offer "Boot BIOS" when a BIOS image is actually available.
        has_bios = bool(cfg.bios_path(self._settings) and Path(cfg.bios_path(self._settings)).is_file()) \
            or DEFAULT_BIOS.is_file()
        self._bios_btn.setVisible(has_bios)
        self._view_btns[cfg.VIEW_GRID].setText(cfg.tr(lang, "view_grid"))
        self._view_btns[cfg.VIEW_LIST].setText(cfg.tr(lang, "view_list"))
        self._view_btns[cfg.VIEW_COMPACT].setText(cfg.tr(lang, "view_compact"))
        self._size_lbl.setText(cfg.tr(lang, "thumb_size"))
        self._sync_view_buttons()

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

    def reload(self) -> None:
        self._stop_worker()
        d = self._rom_dir()
        self._roms = []
        if d:
            # Recurse: point it at a whole projects tree and it finds every ROM inside.
            roms: set[Path] = set()
            for pat in ("*.ngc", "*.ngp", "*.NGC", "*.NGP"):
                try:
                    roms.update(d.rglob(pat))
                except (OSError, ValueError):
                    pass
            self._roms = sorted(p for p in roms if p.is_file())
        self._images.clear()
        self._rebuild()
        if self._roms:
            self._start_worker(self._roms)

    def _rebuild(self) -> None:
        """Lay the library out for the current view mode + size, reusing any
        thumbnails already in memory (switching views never re-renders)."""
        self._items.clear()
        host = QWidget(); host.setObjectName("page")
        view = cfg.library_view(self._settings)
        size = cfg.thumb_size(self._settings)
        self._empty.setVisible(not self._roms)

        if view == cfg.VIEW_GRID:
            grid = QGridLayout(host)
            grid.setContentsMargins(0, 0, 0, 0); grid.setSpacing(16)
            grid.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft)
            card_w = _art_size(size)[0] + 16
            self._grid_card_w = card_w
            cols = self._cols_for_width(self._scroll.viewport().width())
            cards = []
            for i, rom in enumerate(self._roms):
                card = GameCard(rom, size)
                card.clicked.connect(self.play_requested.emit)
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
                row = GameRow(rom, row_size, show_art)
                row.clicked.connect(self.play_requested.emit)
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

    def showEvent(self, e) -> None:  # type: ignore[override]
        # At construction the scroll area has no real width yet, so the first layout uses
        # a fallback and may not fill the window. Re-flow once we are actually on screen.
        super().showEvent(e)
        QTimer.singleShot(0, self._reflow_grid)

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
        self._images[rom_str] = img          # keep for view/size switches
        item = self._items.get(rom_str)
        if item is not None:
            item.set_image(img)

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
        # Coming back to the library (e.g. a game just closed) -> resume rendering the
        # covers we have not made yet. It was stopped on hide so it never shared the
        # native core with a running game.
        super().showEvent(e)
        if self._worker is None and self._roms:
            todo = [r for r in self._roms if str(r) not in self._images]
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
        for key in ("cat_general", "cat_graphics", "cat_audio", "cat_controls", "cat_hotkeys"):
            QListWidgetItem(self._cats)
        self._cats.currentRowChanged.connect(lambda i: self._stack.setCurrentIndex(i))
        root.addWidget(self._cats)

        self._stack = QStackedWidget()
        self._stack.addWidget(self._general_panel())
        self._stack.addWidget(self._graphics_panel())
        self._stack.addWidget(self._audio_panel())
        self._stack.addWidget(self._controls_panel())
        self._stack.addWidget(self._hotkeys_panel())
        root.addWidget(self._stack, 1)

        self._cats.setCurrentRow(0)
        self.retranslate()

    _CATEGORY_ROW = {"video": 1, "audio": 2, "controls": 3}

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

        self._realbios = QCheckBox()
        self._realbios.setChecked(cfg.real_bios(self._settings))
        self._realbios.toggled.connect(
            lambda b: (self._settings.setValue("general/real_bios", b), self.changed.emit()))
        self._lbl_realbios = QLabel()

        self._bios_edit = QLineEdit(cfg.bios_path(self._settings))
        self._bios_edit.editingFinished.connect(
            lambda: self._settings.setValue("paths/bios", self._bios_edit.text()))
        self._bios_browse = QPushButton(); self._bios_browse.setObjectName("ghost")
        self._bios_browse.clicked.connect(self._pick_bios)
        biosw = QWidget(); bh = QHBoxLayout(biosw); bh.setContentsMargins(0, 0, 0, 0)
        self._bios_edit.setFixedWidth(220); bh.addWidget(self._bios_edit); bh.addWidget(self._bios_browse)
        self._lbl_bios = QLabel()

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

        self._realbios_hint = QLabel()
        self._realbios_hint.setObjectName("hint")
        self._realbios_hint.setWordWrap(True)
        self._rows_general = [
            _row(self._lbl_lang, self._lang),
            _row(self._lbl_bios, biosw),
            _row(self._lbl_shots, shotw),
            _row(self._lbl_savemode, self._savemode),
            _row(self._lbl_flashsize, self._flashsize),
            _row(self._lbl_rewind, self._rewind),
            _row(self._lbl_realbios, self._realbios),
            _row(self._lbl_cartwait, self._cartwait),
        ]
        for r in self._rows_general:
            v.addWidget(r)
        v.addWidget(self._cartwait_hint)
        v.addWidget(self._realbios_hint)
        return w

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
        self._lbl_showfps = QLabel()
        for r in (_row(self._lbl_scale, self._scale),
                  _row(self._lbl_filter, self._filter),
                  _row(self._lbl_color, self._colorbox),
                  _row(self._lbl_aspect, self._aspectbox),
                  _row(self._lbl_smooth, self._smooth),
                  _row(self._lbl_showfps, self._showfps),
                  _row(self._lbl_fs, self._fs)):
            v.addWidget(r)
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
        self._keybtns: dict[str, cfg.KeyCaptureButton] = {}
        for label, _mask in cfg.JOYPAD_BUTTONS:
            code = int(self._settings.value(
                f"input/{label}", cfg.DEFAULT_KEYS.get(label, 0), type=int))
            btn = cfg.KeyCaptureButton(code)
            btn.setObjectName("ghost"); btn.setFixedWidth(140)

            def persist(lbl=label, b=btn) -> None:
                cfg.set_binding(self._settings, lbl, b.key_code())
                self.changed.emit()
            btn.clicked.connect(lambda _=False: None)
            btn.installEventFilter(self)   # persist after capture (keyRelease)
            self._keybtns[label] = btn
            lab = QLabel(label)
            v.addWidget(_row(lab, btn))
        self._restore = QPushButton(); self._restore.setObjectName("ghost")
        self._restore.clicked.connect(self._restore_keys)
        v.addWidget(self._restore)
        return w

    # -- Hotkeys (reference list; the bindings live under Controls)
    def _hotkeys_panel(self) -> QWidget:
        w, v = self._panel()
        self._hk_intro = QLabel(); self._hk_intro.setObjectName("hint")
        v.addWidget(self._hk_intro)
        self._hk_labels: list[tuple[QLabel, str]] = []
        for key in ("hk_menu", "hk_pause", "hk_reset", "hk_fs", "hk_size",
                    "hk_state", "hk_speed", "hk_shot", "hk_debug"):
            lab = QLabel()
            row = QFrame(); row.setObjectName("settingRow")
            h = QHBoxLayout(row); h.setContentsMargins(14, 10, 14, 10)
            h.addWidget(lab); h.addStretch()
            v.addWidget(row)
            self._hk_labels.append((lab, key))
        return w

    def eventFilter(self, obj, event) -> bool:  # type: ignore[override]
        # After a KeyCaptureButton captures (on KeyPress), persist on the next event.
        from PyQt6.QtCore import QEvent
        if isinstance(obj, cfg.KeyCaptureButton) and event.type() == QEvent.Type.KeyPress:
            res = super().eventFilter(obj, event)
            for label, b in self._keybtns.items():
                if b is obj:
                    cfg.set_binding(self._settings, label, b.key_code())
                    self.changed.emit()
            return res
        return super().eventFilter(obj, event)

    def _restore_keys(self) -> None:
        for label, btn in self._keybtns.items():
            code = cfg.DEFAULT_KEYS.get(label, 0)
            btn._key = code           # noqa: SLF001
            btn._render()             # noqa: SLF001
            cfg.set_binding(self._settings, label, code)
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

    def retranslate(self) -> None:
        lang = cfg.language(self._settings)
        t = lambda k: cfg.tr(lang, k)
        for i, key in enumerate(("cat_general", "cat_graphics", "cat_audio",
                                 "cat_controls", "cat_hotkeys")):
            self._cats.item(i).setText(t(key))
        self._resume_banner.setText("▶  " + t("m_resume"))
        self._hk_intro.setText(t("hk_intro"))
        for lab, key in self._hk_labels:
            lab.setText(t(key))
        self._lbl_lang.setText(t("language")); self._lbl_bios.setText(t("bios"))
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
        self._lbl_cartwait.setText(t("cart_wait")); self._cartwait_hint.setText(t("cart_wait_hint"))
        self._lbl_scale.setText(t("lcd_scale")); self._lbl_smooth.setText(t("smoothing"))
        self._lbl_filter.setText(t("filter")); self._lbl_color.setText(t("color_profile"))
        self._lbl_aspect.setText(t("aspect")); self._lbl_fs.setText(t("fullscreen"))
        self._lbl_showfps.setText(t("show_fps"))
        for box, items in ((self._filter, self._filter_items),
                           (self._colorbox, self._color_items),
                           (self._aspectbox, self._aspect_items)):
            for i, (_val, key) in enumerate(items):
                box.setItemText(i, t(key))
        self._lbl_aon.setText(t("audio_on")); self._lbl_vol.setText(t("volume"))
        self._ctrl_hint.setText(t("controls_hint")); self._restore.setText(t("restore"))
        for b in self._keybtns.values():
            b._render()  # noqa: SLF001


# ---------------------------------------------------------------- in-game menu
class OverlayMenu(QWidget):
    """A translucent full-page pause menu over the running game, keyboard- and
    mouse-navigable. It emits `chosen(action_id)`; the owner keeps the game alive
    and acts on it. Modelled on RetroArch's Quick Menu -- the game never unloads
    until you explicitly quit."""

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

    def __init__(self, settings) -> None:
        super().__init__()
        self.setObjectName("page")
        self._settings = settings
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
        self.pending = bytearray()
        self.watches = WatchSet()          # named memory watches, loaded per-ROM
        self.breaks = ExecBreakSet()       # PC execution breakpoints, loaded per-ROM
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

        # A discoverable control bar under the screen (save states, speed, shot, reset…).
        self.toolbar = self._make_toolbar()
        outer.addWidget(self.toolbar, 0)
        # A little always-visible tab to bring the bar back when it is hidden.
        self._bar_show = QPushButton("▴", self); self._bar_show.setObjectName("barShow")
        self._bar_show.setFixedSize(30, 18); self._bar_show.setToolTip("Show toolbar (H)")
        self._bar_show.clicked.connect(lambda: self._toggle_toolbar(True))
        self._bar_show.hide()
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)

        self.menu = OverlayMenu(self)
        self.menu.chosen.connect(self._on_menu_choice)
        self._menu_open = False

        self.timer = QTimer(self)
        self.timer.setTimerType(Qt.TimerType.PreciseTimer)
        self.timer.timeout.connect(self._tick)

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
        rng = self.watches.write_range()
        try:
            if rng is not None:
                self.machine.set_write_log(rng[0], rng[1])
            else:
                self.machine.set_write_log(1, 0)    # lo > hi disarms
        except Exception:
            pass

    def _bios_path(self) -> Path | None:
        b = cfg.bios_path(self._settings)
        if b and Path(b).is_file():
            return Path(b)
        return DEFAULT_BIOS if DEFAULT_BIOS.is_file() else None

    def start(self, rom: Path) -> None:
        """Boot a game. Default is the instant hand-off (a running console handed
        to the cartridge). 'Console boot' runs the real BIOS power-on first -- it
        now clears the old "SUB BATTERY DEAD" screen and shows the language/date
        setup, but does NOT yet hand off from the BIOS to the cartridge (the NMI
        power-manager path is unfinished -- the BIOS-to-cart hand-off is unfinished),
        so games should be launched with console boot OFF for now."""
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
            flash_size=cap)
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
        self._raw = native.NativeMachine(_NO_CART, bios=bios.read_bytes())
        if _SYSTEM_RAM.exists():
            self._raw.set_battery_ram(_SYSTEM_RAM.read_bytes())
        self._raw.reset(real_bios=True)
        self.session = None
        self.machine = self._raw
        self._begin_run()

    def _begin_run(self) -> None:
        self.held = 0
        self.paused = False
        self.overlay.setText("")
        self.apply_settings()
        if cfg.audio_enabled(self._settings):
            self._open_audio()
        self.setFocus()
        self.timer.start(4)

    def stop(self) -> None:
        self.timer.stop()
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
        # Tab is the fast-forward key; grab it before Qt uses it for focus traversal.
        if e.type() in (QEvent.Type.KeyPress, QEvent.Type.KeyRelease) \
                and e.key() == int(Qt.Key.Key_Tab):
            (self.keyPressEvent if e.type() == QEvent.Type.KeyPress
             else self.keyReleaseEvent)(e)
            return True
        return super().event(e)

    def keyPressEvent(self, e: QKeyEvent) -> None:  # noqa: N802
        k = e.key()
        if k == int(Qt.Key.Key_Escape):
            self.open_menu(); return                       # PAUSE, don't quit
        if k == int(Qt.Key.Key_F1):
            self.debug_requested.emit(); return
        if k == int(Qt.Key.Key_F11):
            self._toggle_fullscreen(); return
        if (e.modifiers() & Qt.KeyboardModifier.ControlModifier) and \
                int(Qt.Key.Key_1) <= k <= int(Qt.Key.Key_5):
            self.set_window_scale(k - int(Qt.Key.Key_1) + 1); return
        if k == int(Qt.Key.Key_P):
            self.paused = not self.paused
            self.overlay.setText(cfg.tr(cfg.language(self._settings), "paused")
                                 if self.paused else ""); return
        if k == int(Qt.Key.Key_F5):
            self._do_reset(); return
        # save states: F2 save, F4 load, F3 cycle slot
        if k == int(Qt.Key.Key_F2):
            self.save_state(); return
        if k == int(Qt.Key.Key_F4):
            self.load_state(); return
        if k == int(Qt.Key.Key_F3):
            self.set_slot((self._slot + 1) % STATE_SLOTS); return
        if k == int(Qt.Key.Key_F12):
            self.screenshot(); return
        if k == int(Qt.Key.Key_H):
            self._toggle_toolbar(); return
        # speed: Tab (hold) = fast-forward; [ / ] step slower/faster
        if k == int(Qt.Key.Key_Tab):
            if not e.isAutoRepeat():
                self._ff = True; self.wall_last = time.perf_counter(); self.debt = 0.0
            return
        if k == int(Qt.Key.Key_BracketRight):
            self.cycle_speed(True); return
        if k == int(Qt.Key.Key_BracketLeft):
            self.cycle_speed(False); return
        # rewind: ',' one frame back, '.' one frame forward ("what did I just see?")
        if k == int(Qt.Key.Key_Comma):          # HOLD to rewind; release resumes
            if not e.isAutoRepeat():
                self.start_rewind()
            return
        if k == int(Qt.Key.Key_Period):
            self.step_forward(); return
        bit = self._bindings.get(k)
        if bit and not e.isAutoRepeat():
            self.held |= bit

    # ---- in-game menu / suspend-resume
    def _do_reset(self) -> None:
        if self.session is not None:
            self.session.reboot()
        elif self._raw is not None:
            self._raw.reset(real_bios=True)
        self.held = 0
        self._power_pressed = False
        self._crashed = False
        self._bp_step_off = False
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

        def btn(text, tip, slot, checkable=False):
            b = QPushButton(text); b.setObjectName("barBtn"); b.setToolTip(tip)
            b.setFocusPolicy(Qt.FocusPolicy.NoFocus); b.setCheckable(checkable)
            if checkable:
                b.toggled.connect(slot)
            else:
                b.clicked.connect(slot)
            h.addWidget(b); return b

        btn("☰", "Menu (Esc)", self.open_menu)
        btn("⟲", "Reset (F5)", self._do_reset)
        h.addSpacing(10)
        h.addWidget(QLabel("Slot"))
        self._slot_spin = QSpinBox(); self._slot_spin.setRange(1, STATE_SLOTS)
        self._slot_spin.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._slot_spin.valueChanged.connect(lambda v: setattr(self, "_slot", v - 1))
        h.addWidget(self._slot_spin)
        btn("💾", "Save state (F2)", lambda: self.save_state())
        btn("📂", "Load state (F4)", lambda: self.load_state())
        h.addSpacing(10)
        btn("📷", "Screenshot (F12)", self.screenshot)
        h.addSpacing(10)
        rw = QPushButton("⏪"); rw.setObjectName("barBtn")
        rw.setToolTip("Hold to rewind ( , )"); rw.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        rw.pressed.connect(self.start_rewind); rw.released.connect(self.stop_rewind)
        h.addWidget(rw)
        btn("⏵", "Step one frame forward ( . )", self.step_forward)
        h.addSpacing(10)
        btn("−", "Slower ( [ )", lambda: self.cycle_speed(False))
        self._speed_lbl = QLabel("1×"); self._speed_lbl.setObjectName("barSpeed")
        self._speed_lbl.setFixedWidth(36)
        self._speed_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        h.addWidget(self._speed_lbl)
        btn("+", "Faster ( ] )", lambda: self.cycle_speed(True))
        self._ff_btn = btn("⏩", "Fast-forward toggle (or hold Tab)", self._set_ff, checkable=True)
        h.addStretch()
        btn("⛶", "Fullscreen (F11)", self._toggle_fullscreen)
        btn("▾", "Hide toolbar (H)", lambda: self._toggle_toolbar(False))
        return bar

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
            fr = cfg.language(self._settings) == "fr"
            msg = ("⚠ Ce jeu a besoin du BIOS Neo Geo Pocket (non chargé).\n"
                   "Ajoutez un bios.bin — Réglages ▸ BIOS." if fr else
                   "⚠ This game needs the Neo Geo Pocket BIOS (not loaded).\n"
                   "Add a bios.bin — Settings ▸ BIOS.")
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
        if self.sink is not None:
            self.sink.stop(); self.sink = None; self.audio = None
        self.pending.clear()

    def resume_play(self) -> None:
        if self.machine is None:
            return
        self.menu.hide(); self._menu_open = False
        self.paused = False
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
        if e.key() == int(Qt.Key.Key_Tab) and not e.isAutoRepeat():
            # releasing Tab returns to whatever the toolbar's FF toggle says
            self._ff = self._ff_btn.isChecked()
            self.wall_last = time.perf_counter(); self.debt = 0.0
            return
        if e.key() == int(Qt.Key.Key_Comma) and not e.isAutoRepeat():
            self.stop_rewind(); return       # releasing rewind -> resume forward
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
        if bp is not None and bp.cond_true(self.machine):
            self.paused = True
            self._bp_step_off = True         # step off it when the user resumes
            where = f"{pc:06X}" + (f"  [{bp.cond}]" if bp.cond else "")
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

    def _tick(self) -> None:
        if self.machine is None:
            return
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
            # SAFETY NET: at 1x the pacer idles between frames (a few ticks), but if it
            # returns 0 for ~0.3 s the audio clock has stalled (a sink stuck underrun after
            # a long rewind pause). Reopen it so the loop can never stay frozen.
            self._stall_ticks += 1
            if self._stall_ticks > 75:
                self._stall_ticks = 0
                self._restart_audio()
            return
        self._stall_ticks = 0
        self.machine.write(0x00B0, bytes([self.held & 0x7F]))
        wrange = self.watches.write_range()      # break-on-write window, if any
        locked = self.watches.locked()
        for _ in range(due):
            if wrange is not None:               # fresh per-frame write capture
                self.machine.set_write_log(wrange[0], wrange[1])
            summ = self.machine.run_frames(1)
            if summ.stop_status in _CRASH_STATUSES and not self._crashed:
                self._on_crash(summ); return
            if summ.stop_status == native.STATUS_BREAKPOINT:
                if self._on_breakpoint(summ.stop_pc):
                    return                        # paused at a breakpoint whose guard held
            if wrange is not None and self._check_write_break():
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
            if self._rewind_on:                  # frame-perfect rewind history
                self._rewind.append(self._capture_state())
            if self._song is not None:           # per-frame note capture (.ngps export)
                self._song.feed(self.machine.apu_state())
            # THE BIOS HALTS ON PURPOSE: it arms INT0 (the POWER button) and
            # sleeps. Press it once, on the player's behalf -- they asked for the
            # console to come on by launching. (Same reason NativeSession and ares
            # do it.) Only in real-BIOS mode; a hand-off game never halts here.
            if (self._real_bios and not self._power_pressed
                    and summ.stop_status == native.STATUS_HALTED):
                self.machine.raise_irq(8)      # INT0 = power
                self._power_pressed = True
                self.machine.run_frames(1)
            if self.audio is not None:
                a = self.machine.audio()          # always drain the core's audio buffer
                self._last_audio = a              # feed the debug oscilloscope
                if self._speed == 1.0 and not self._ff:
                    self.pending += a             # ...but only queue it at real speed (mute FF/slow)
        self._drain_pending()
        if self._vgm is not None:            # capturing music -> log this tick's PSG writes
            self._vgm.feed(self.machine.apu_write_count(), self.machine.apu_writes())
        self._blit()
        self._update_osd(due)

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
        self.setStyleSheet(STYLE)

        central = QWidget(); central.setObjectName("page")
        root = QHBoxLayout(central); root.setContentsMargins(0, 0, 0, 0); root.setSpacing(0)

        # left rail (collapses to a thin strip that still holds the toggle)
        rail = QWidget(); rail.setObjectName("rail"); rail.setFixedWidth(190)
        self._rail = rail
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
        self.library = LibraryPage(self._settings)
        self.settings = SettingsPage(self._settings)
        self.play = PlayPage(self._settings)
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
        self.settings.resume_requested.connect(lambda: self._go(2))
        self._debug_win = None

        self._retranslate()
        self._go(0)
        self._toggle_rail(not bool(self._settings.value("win/rail_collapsed", False, type=bool)))
        if rom:
            self._launch(rom)

    def _toggle_rail(self, show: bool | None = None) -> None:
        # Collapse to a thin 44px strip that still holds the toggle (no overlap of content).
        show = (self._rail.width() < 100) if show is None else show
        self._rail.setFixedWidth(190 if show else 44)
        for wdg in self._rail_hideable:
            wdg.setVisible(show)
        self._rail_toggle.setText("‹" if show else "☰")
        self._settings.setValue("win/rail_collapsed", not show)

    def _retranslate(self) -> None:
        lang = cfg.language(self._settings)
        self._nav_resume.setText("   " + cfg.tr(lang, "m_resume"))
        self._nav_lib.setText("   " + cfg.tr(lang, "library"))
        self._nav_set.setText("   " + cfg.tr(lang, "settings"))
        self._nav_dbg.setText("   " + cfg.tr(lang, "m_debug"))
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
