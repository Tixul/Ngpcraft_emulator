"""The debug window — a live look inside the running console.

A separate, non-modal window the shell opens with F1 / the rail / the pause menu.
It reads the native machine directly (registers, memory, VRAM) and refreshes a few
times a second while visible. The usual debugger furniture for a console of this
era: CPU state, a disassembly around PC, a memory hex viewer, and the graphics
viewers (palette, tiles, sprites).

Made for actual use, not just looking:
  * ❄ Freeze holds every view still (the disassembly follows PC, so a running
    game scrolls it past too fast to read -- freeze to study one moment).
  * every tab has an Export button: text views save .txt, the palette and tile
    sheets save .png.
  * the disassembly can trace a run of N instructions to a file, so you capture a
    whole stretch of execution instead of the single instant on screen.
Nothing here drives the emulation except the explicit Pause / Step / Reset / Trace
controls.
"""

from __future__ import annotations

import math
from pathlib import Path

import numpy as np
from PyQt6.QtCore import Qt, QTimer, QRect
from PyQt6.QtGui import QImage, QPixmap, QFont, QBrush, QColor, QKeySequence, QShortcut
from PyQt6.QtWidgets import (
    QMainWindow, QWidget, QLabel, QPushButton, QVBoxLayout, QHBoxLayout,
    QPlainTextEdit, QTabWidget, QComboBox, QLineEdit, QSpinBox, QCheckBox,
    QScrollArea, QFileDialog, QTableWidget, QTableWidgetItem, QHeaderView,
    QAbstractItemView, QToolTip, QApplication, QMessageBox,
)

from core import native
from core.decode import decode_instruction_at
from core.watches import Watch
from core.exec_breaks import ExecBreak
from core.ramsearch import RamSearch
from core.symbols import SymbolTable, load_map
from core.vgm_export import VgmRecorder
from core.ngps_export import NgpsRecorder
import ngpc_theme

_NOTE_NAMES = ("C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B")


def _freq_to_note(freq: float) -> str:
    if freq <= 0:
        return "—"
    midi = int(round(69 + 12 * math.log2(freq / 440.0)))
    if not 0 <= midi < 128:
        return "—"
    return f"{_NOTE_NAMES[midi % 12]}{midi // 12 - 1}"

# ---- VRAM / palette map (mirrors cpp/src/render.cpp) ----
CHAR_RAM = 0x00A000
# Character RAM is 8 KiB = 512 tiles of 16 bytes, and sprites address it with a 9-bit
# index (the attribute byte's bit 0 is tile bit 8), so tiles 256-511 are ordinary,
# heavily-used graphics -- not an exotic corner. The viewer used to read a flat 256.
CHAR_RAM_SIZE = 0x2000
TILE_BYTES = 16
CHAR_RAM_TILES = CHAR_RAM_SIZE // TILE_BYTES
TILE_ATLAS_COLS = 16
# On-screen geometry of the tile atlas, shared by the sheet builder (decode_tiles) and
# the hover-inspect grid so a mouse position maps back to the tile it is over.
TILE_ATLAS_PITCH = 10     # 1px frame + 8px tile + 1px frame, per cell in the sheet
TILE_ATLAS_SCALE = 3      # the sheet is drawn at 3x
OAM_BASE = 0x008800
OAM_CPC = 0x008C00
# The cartridge is mapped here; a ROM file offset is (CPU address − CART_BASE). The
# Text tab shows both so a hit maps straight back to a byte in the .ngc on disk.
CART_BASE = 0x200000
# The two tilemaps, 32x32 entries of 2 bytes (mirrors cpp/src/render.cpp kScr1Map/kScr2Map).
# Entry = [tile low 8 bits][attrib], and attrib bit 0 is tile bit 8 -- the same 9-bit index
# sprites use, because all three consumers read the SAME character RAM.
SCR1_MAP = 0x009000
SCR2_MAP = 0x009800
TILEMAP_BYTES = 32 * 32 * 2
# 515 cycles x 199 scanlines at 6.144 MHz = 59.95 Hz (cpp/src/machine.hpp). This is the
# whole budget a game gets between two pictures.
CYCLES_PER_FRAME = 515 * 199

# Who is using a tile, as a bitmask. Character RAM is shared and the hardware keeps no
# ownership at all -- so this is worked out from who REFERENCES each tile right now.
USE_SCR1, USE_SCR2, USE_SPRITE = 1, 2, 4
# Colour per usage. "Shared" is deliberately loud: a tile pulled by both a plane and a
# sprite is usually a range that was loaded over another one, which is a real and
# hard-to-see bug when it happens by accident.
# The three consumer colours are theme-independent -- they are DATA labels, and a
# blue plane stays blue in either theme. "Free space" is the exception: it is a
# neutral, so it has to follow the background or every unused tile reads as full.
USAGE_COLOURS = {
    0: ngpc_theme.DARK.usage_free,        # nobody -- free space (themed)
    USE_SCR1: (59, 130, 246),             # plane 1
    USE_SCR2: (34, 168, 83),              # plane 2
    USE_SPRITE: (245, 158, 11),           # sprites
}
USAGE_SHARED = (236, 72, 153)             # more than one consumer


def tile_usage(m) -> np.ndarray:
    """Which consumer references each of the 512 tiles, as a USE_* bitmask per tile."""
    usage = np.zeros(CHAR_RAM_TILES, np.uint8)
    for base, flag in ((SCR1_MAP, USE_SCR1), (SCR2_MAP, USE_SCR2)):
        raw = m.read(base, TILEMAP_BYTES)
        ids = np.frombuffer(raw, np.uint8).reshape(-1, 2)
        tiles = ids[:, 0].astype(np.uint16) | ((ids[:, 1] & 1).astype(np.uint16) << 8)
        usage[np.unique(tiles)] |= flag
    oam = m.read(OAM_BASE, 64 * 4)
    for i in range(64):
        usage[((oam[i * 4 + 1] & 1) << 8) | oam[i * 4]] |= USE_SPRITE
    return usage
PAL = {
    "Sprite": 0x008200, "Plane 1 (SCR1)": 0x008280, "Plane 2 (SCR2)": 0x008300,
    "Backdrop": 0x0083E0, "Window": 0x0083F0,
}
REG_NAMES = ("XWA", "XBC", "XDE", "XHL", "XIX", "XIY", "XIZ", "XSP")

MEM_REGIONS = [
    ("CPU I/O", 0x000000), ("Work RAM", 0x004000), ("K2GE regs", 0x008000),
    ("Palette", 0x008200), ("OAM sprites", 0x008800), ("Tilemap 1", 0x009000),
    ("Tilemap 2", 0x009800), ("Character RAM", 0x00A000), ("Cartridge", 0x200000),
    ("BIOS", 0xFF0000),
]

_MONO = "Consolas"

# Instructions that push a return address, so "step over" should run them to
# completion instead of diving in. `swi` is the BIOS call gate and behaves the same.
_CALL_MNEMONICS = frozenset({"call", "calr", "swi"})

# Row tint for the instruction the PC is on, and the "leave it alone" brush for
# every other row (a default-constructed QBrush clears any previous highlight).
# Memory-viewer access tint follows the usual convention: blue was read, red was
# written. All of these are REBOUND by `use_palette` -- they are module globals
# read at call time, so rebinding reaches every table without touching the rows.
PALETTE = ngpc_theme.DARK
_PC_ROW_BG = ngpc_theme.brush(PALETTE.dbg_pc_row)
_NO_BRUSH = QBrush()
_READ_BG = ngpc_theme.brush(PALETTE.dbg_read)
_WRITE_BG = ngpc_theme.brush(PALETTE.dbg_write)


def use_palette(p) -> None:
    """Point this module's colours at a new theme. Call before repainting."""
    global PALETTE, _PC_ROW_BG, _READ_BG, _WRITE_BG
    PALETTE = p
    _PC_ROW_BG = ngpc_theme.brush(p.dbg_pc_row)
    _READ_BG = ngpc_theme.brush(p.dbg_read)
    _WRITE_BG = ngpc_theme.brush(p.dbg_write)
    USAGE_COLOURS[0] = p.usage_free
# How many sampled frames a byte stays lit after being touched. At 60 fps this is
# about a second -- long enough to see a one-off access, short enough that a busy
# region does not just stay solid.
_ACCESS_FADE = 60


class _ReadResult:
    __slots__ = ("status", "data")

    def __init__(self, status: str, data: bytes | None) -> None:
        self.status = status
        self.data = data


class _Bus:
    """Adapts the native machine to the read interface the decoder wants."""

    def __init__(self, machine) -> None:
        self._m = machine

    def read_bytes(self, address: int, size: int = 1) -> _ReadResult:
        try:
            return _ReadResult("ok", bytes(self._m.read(address & 0xFFFFFF, size)))
        except Exception:
            return _ReadResult("unmapped", None)


class _BytesBus:
    """Decode straight from a captured instruction's own bytes."""

    def __init__(self, data: bytes, base: int) -> None:
        self._d = data
        self._base = base

    def read_bytes(self, address: int, size: int = 1) -> _ReadResult:
        off = address - self._base
        if 0 <= off and off + size <= len(self._d):
            return _ReadResult("ok", bytes(self._d[off:off + size]))
        return _ReadResult("unmapped", None)


def _rgb_from_u16(c: int) -> tuple[int, int, int]:
    return ((c & 0x0F) * 17, ((c >> 4) & 0x0F) * 17, ((c >> 8) & 0x0F) * 17)


def _read_u16(m, addr: int) -> int:
    b = m.read(addr & 0xFFFFFF, 2)
    return b[0] | (b[1] << 8)


def _disasm_bytes(raw: bytes, pc: int) -> str:
    try:
        d = decode_instruction_at(_BytesBus(raw, pc), pc)
        return d.assembly or (d.mnemonic or "??")
    except Exception:
        return "??"


def _access_text(acc, count: int, tag: str) -> str:
    """Render an instruction's memory accesses as 'R[4123]=07'."""
    out = []
    for i in range(min(count, len(acc))):
        a = acc[i]
        size = max(1, min(4, a.size))
        val = int.from_bytes(bytes(a.data[:size]), "little")
        out.append(f"{tag}[{a.address:06X}]={val:0{size * 2}X}")
    return " ".join(out)


def _trace_detail_text(rec) -> str:
    """The half of a trace record the logger used to discard: which registers the
    instruction wrote, and every memory address it touched. This is what turns a
    trace from 'what ran' into 'what it did'."""
    parts = []
    if rec.written_regs:
        names = [REG_NAMES[i] for i in range(len(REG_NAMES))
                 if rec.written_regs & (1 << i)]
        if names:
            parts.append("regs=" + ",".join(names))
    reads = _access_text(rec.reads, rec.n_reads, "R")
    if reads:
        parts.append(reads)
    writes = _access_text(rec.writes, rec.n_writes, "W")
    if writes:
        parts.append(writes)
    return "  ".join(parts)


def decode_tiles(char_bytes: bytes, palette_rgb: np.ndarray,
                 usage: "np.ndarray | None" = None,
                 show: "set[int] | None" = None) -> np.ndarray:
    """char_bytes: N*16 bytes of 2bpp tiles -> an (rows, cols) sheet of 8x8 tiles.

    With `usage` (a USE_* bitmask per tile, from `tile_usage`) each tile gets a 1px frame
    saying who references it, and `show` filters: a tile whose consumers are all unchecked
    is dimmed rather than removed, so the grid keeps its shape and a tile's position still
    tells you its index.
    """
    n = len(char_bytes) // 16
    if n == 0:
        return np.zeros((8, 8, 3), np.uint8)
    data = np.frombuffer(char_bytes[: n * 16], dtype=np.uint8).reshape(n, 8, 2)
    even = data[:, :, 0]
    odd = data[:, :, 1]
    px = np.empty((n, 8, 8), np.uint8)
    px[:, :, 0] = (odd >> 6) & 3; px[:, :, 1] = (odd >> 4) & 3
    px[:, :, 2] = (odd >> 2) & 3; px[:, :, 3] = odd & 3
    px[:, :, 4] = (even >> 6) & 3; px[:, :, 5] = (even >> 4) & 3
    px[:, :, 6] = (even >> 2) & 3; px[:, :, 7] = even & 3
    rgb = palette_rgb[px]
    cols = TILE_ATLAS_COLS
    rows = (n + cols - 1) // cols

    if usage is None:
        sheet = np.zeros((rows * 8, cols * 8, 3), np.uint8)
        for i in range(n):
            r, c = divmod(i, cols)
            sheet[r * 8:(r + 1) * 8, c * 8:(c + 1) * 8] = rgb[i]
        return sheet

    pitch = TILE_ATLAS_PITCH                      # 1px frame + 8px tile + 1px frame
    sheet = np.zeros((rows * pitch, cols * pitch, 3), np.uint8)
    for i in range(n):
        r, c = divmod(i, cols)
        u = int(usage[i]) if i < len(usage) else 0
        consumers = [f for f in (USE_SCR1, USE_SCR2, USE_SPRITE) if u & f]
        if len(consumers) > 1:
            frame = USAGE_SHARED
        elif consumers:
            frame = USAGE_COLOURS[consumers[0]]
        else:
            frame = USAGE_COLOURS[0]
        tile = rgb[i]
        if show is not None and not (set(consumers) & show if consumers else 0 in show):
            tile = (tile.astype(np.uint16) * 3 // 10).astype(np.uint8)   # filtered out
            frame = tuple(v * 3 // 10 for v in frame)
        y, x = r * pitch, c * pitch
        sheet[y:y + pitch, x:x + pitch] = frame
        sheet[y + 1:y + 9, x + 1:x + 9] = tile
    return sheet


class _Gauge(QLabel):
    """A labelled bar that fills left-to-right and colours green→amber→red with the
    value. Two moods: `usage` (full = red = running out, for a VRAM budget) and
    `health` (full = green = all good, for 'is the game keeping up'). A neutral grey
    state says 'no reading' -- e.g. a still screen where the frame rate can't be told.

    The fill is a stylesheet gradient, not a custom `paintEvent`: a Python override
    that paints is exactly what turns a stray exception into a hard process abort (see
    the root conftest), and a plain QLabel with a computed stylesheet cannot."""

    def __init__(self, mood: str = "usage") -> None:
        super().__init__()
        self._mood = mood
        self._value = 0.0
        self._neutral = True
        self._caption = ""
        self.setMinimumHeight(24)
        self.set_value(0.0, "", neutral=True)

    @staticmethod
    def _severity_colour(sev: float) -> QColor:
        sev = max(0.0, min(1.0, sev))
        if sev < 0.5:                      # green -> amber
            t = sev * 2
            return QColor(int(60 + t * 160), int(190 + t * 10), 70)
        t = (sev - 0.5) * 2                # amber -> red
        return QColor(int(220 + t * 15), int(200 - t * 160), int(70 - t * 30))

    def set_value(self, value: float, caption: str, *, neutral: bool = False) -> None:
        self._value = max(0.0, min(1.0, value))
        self._caption = caption
        self._neutral = neutral
        if neutral:
            fill, frac = QColor(150, 150, 155), 1.0
        else:
            sev = self._value if self._mood == "usage" else (1.0 - self._value)
            fill, frac = self._severity_colour(sev), self._value
        c = fill.name()
        track = "rgba(255,255,255,0.11)"
        # A gradient that is `fill` up to `frac`, then the empty track after it.
        edge = min(max(frac, 0.0001), 0.9999)
        stops = (f"stop:0 {c}, stop:{edge:.4f} {c}, "
                 f"stop:{min(edge + 0.0001, 1.0):.4f} {track}, stop:1 {track}")
        self.setStyleSheet(
            "QLabel {"
            f"  background: qlineargradient(x1:0,y1:0,x2:1,y2:0, {stops});"
            "  border-radius: 11px; color: white; padding-left: 11px;"
            "  font-weight: bold;"
            "}")
        self.setText(caption)


class _TileGrid(QLabel):
    """The tile atlas, made hover-inspectable. Moving over a tile reports the numbers
    you need to poke or replace it -- its index, its VRAM address, who references it,
    and its 16 raw bytes -- as a tooltip AND in a status line below. Clicking a tile
    copies that block to the clipboard; the status line is selectable too, so a single
    number is a drag-and-Ctrl+C away.

    A pure view: it holds no machine data and asks a callback for the text, so the
    debug window stays the one place that reads the core."""

    def __init__(self, cell_px: int, cols: int, info) -> None:
        super().__init__()
        self._cell = cell_px          # on-screen pixels per tile (pitch * scale)
        self._cols = cols
        self._info = info             # (col, row) -> str | None  (full, copyable block)
        self._status = None           # (text | None, copied: bool) -> None
        self._last = None             # last block under the cursor, for a click
        self.setMouseTracking(True)
        self.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)

    def set_status_sink(self, fn) -> None:
        self._status = fn

    def _at(self, e) -> "str | None":
        pix = self.pixmap()
        if pix is None or pix.isNull():
            return None
        x, y = int(e.position().x()), int(e.position().y())
        if not (0 <= x < pix.width() and 0 <= y < pix.height()):
            return None
        col, row = x // self._cell, y // self._cell
        if col >= self._cols:
            return None
        return self._info(col, row)

    def mouseMoveEvent(self, e) -> None:  # type: ignore[override]
        text = self._at(e)
        self._last = text
        if text:
            QToolTip.showText(e.globalPosition().toPoint(), text, self)
        else:
            QToolTip.hideText()
        if self._status is not None:
            self._status(text, False)
        super().mouseMoveEvent(e)

    def mousePressEvent(self, e) -> None:  # type: ignore[override]
        text = self._at(e) or self._last
        if text and self._status is not None:
            self._status(text, True)
        super().mousePressEvent(e)


def _pixmap(arr: np.ndarray, scale: int) -> QPixmap:
    arr = np.ascontiguousarray(arr)
    h, w = arr.shape[:2]
    img = QImage(arr.data, w, h, 3 * w, QImage.Format.Format_RGB888)
    pix = QPixmap.fromImage(img.copy())
    return pix.scaled(w * scale, h * scale, Qt.AspectRatioMode.IgnoreAspectRatio,
                      Qt.TransformationMode.FastTransformation)


class DebugWindow(QMainWindow):
    def restyle(self, palette) -> None:
        """Repaint for a new theme.

        The window chrome needs no work -- this is a child of the Shell, so Qt
        pushes the Shell's stylesheet down the hierarchy for free. What is left
        is everything QSS cannot express: the table-row BRUSHES, the legend
        swatches (a colour that IS data, not decoration), and the tile atlas,
        which paints its 'free space' frames into a numpy image and so has to be
        re-rendered rather than restyled."""
        use_palette(palette)
        self._scope.setStyleSheet(f"background:{PALETTE.bg_scope};")
        # Conditional colours: only repaint the ones currently saying something,
        # so a cleared field stays cleared instead of turning permanently red.
        for lab, token in ((self._dis_goto, PALETTE.error),
                           (self._mem_addr, PALETTE.error),
                           (self._rs_value, PALETTE.error),
                           (self._break_err, PALETTE.warning)):
            if lab.styleSheet():
                lab.setStyleSheet(f"color:{token}")
        for flag, cb in self._tile_show.items():
            cb.setStyleSheet(f"color: rgb{USAGE_COLOURS[flag]};")
        self._tile_shared.setStyleSheet(f"color: rgb{USAGE_SHARED};")
        for name, _base, _rows, flag in self.PAL_BLOCKS:
            self._pal_show[name].setStyleSheet(f"color: rgb{USAGE_COLOURS[flag]};")
            self._pal_widgets[name][0].setStyleSheet(
                f"color: rgb{USAGE_COLOURS[flag]}; font-weight: bold;")
        self.refresh()          # re-renders the tile/palette atlases

    def __init__(self, parent, settings) -> None:
        super().__init__(parent)
        self.setWindowTitle("NgpCraft — Debug")
        self.setWindowFlag(Qt.WindowType.Window, True)
        self.resize(760, 640)
        self._play = None
        self._settings = settings
        self._frozen = False
        self._tiles_arr = None
        self._tiles_usage = None            # last USE_* bitmask per tile, for hover
        self._tiles_char = b""              # last raw character RAM, for hover
        self._tiles_n = 0                   # tiles actually present last refresh
        self._pal_arr = None
        self._watch_building = False       # guards table edits from re-committing
        self._watch_rom = None             # last ROM stem shown, to reload on change
        self._breaks_building = False
        self._breaks_rom = None
        self._ram = RamSearch()            # RAM-search session (this window's)
        # Symbols from the toolchain's .map. `core/symbols.py` has existed and worked
        # for a long time; the debugger simply never asked it anything, so every
        # address here read as a bare number even when the names were on disk.
        self._symbols: SymbolTable | None = None
        self._symbols_name = ""            # file the table came from, for the UI
        self._symbols_rom = None           # ROM stem we last auto-loaded for
        self._dis_base: int | None = None  # address the listing starts at (None = follow PC)
        self._dis_rows: list[int] = []     # row index -> address, for click-to-breakpoint
        self._cs_on = False                # call-stack tracking currently armed in the core
        self._vgm_rec = None               # last VGM capture, kept for saving
        self._song_rec = None              # last .ngps capture, kept for saving

        top = QWidget(); self.setCentralWidget(top)
        v = QVBoxLayout(top); v.setContentsMargins(8, 8, 8, 8); v.setSpacing(6)

        bar = QHBoxLayout()
        self._btn_pause = QPushButton("⏸ Pause"); self._btn_pause.clicked.connect(self._toggle_pause)
        self._btn_back = QPushButton("⏪ Back"); self._btn_back.clicked.connect(self._step_back)
        self._btn_back.setToolTip("Rewind one frame ( , )")
        self._btn_step = QPushButton("⏭ Step"); self._btn_step.clicked.connect(self._step)
        self._btn_step.setToolTip("Step one frame forward ( . )")
        self._btn_reset = QPushButton("⟲ Reset"); self._btn_reset.clicked.connect(self._reset)
        for b in (self._btn_pause, self._btn_back, self._btn_step, self._btn_reset):
            b.setObjectName("ghost"); bar.addWidget(b)
        self._freeze = QCheckBox("❄ Freeze view")
        self._freeze.setToolTip("Stop auto-refresh so the view holds still (the game keeps running).")
        self._freeze.toggled.connect(self._on_freeze)
        bar.addWidget(self._freeze)
        bar.addStretch()
        self._status = QLabel(""); self._status.setObjectName("hint"); bar.addWidget(self._status)
        v.addLayout(bar)

        self._tabs = QTabWidget()
        self._tabs.addTab(self._cpu_tab(), "CPU")
        self._tabs.addTab(self._disasm_tab(), "Disassembly")
        self._tabs.addTab(self._callstack_tab(), "Call Stack")
        self._tabs.addTab(self._events_tab(), "Events")
        self._tabs.addTab(self._mem_tab(), "Memory")
        self._tabs.addTab(self._watch_tab(), "Watch")
        self._tabs.addTab(self._breaks_tab(), "Breakpoints")
        self._tabs.addTab(self._ramsearch_tab(), "RAM Search")
        self._tabs.addTab(self._audio_tab(), "Audio")
        self._tabs.addTab(self._palette_tab(), "Palette")
        self._tabs.addTab(self._tiles_tab(), "Tiles")
        self._tabs.addTab(self._sprites_tab(), "Sprites")
        self._tabs.addTab(self._layers_tab(), "Layers")
        self._tabs.addTab(self._load_tab(), "Load")
        self._tabs.addTab(self._text_tab(), "Text")
        self._tabs.addTab(self._crack_tab(), "Crack")
        self._tabs.addTab(self._pointers_tab(), "Pointers")
        self._tabs.addTab(self._compare_tab(), "Compare")
        self._tabs.currentChanged.connect(lambda _i: self.refresh())
        v.addWidget(self._tabs, 1)

        # Long help/description labels wrap instead of dictating a huge minimum WIDTH: an
        # unwrapped sentence forces the whole window at least as wide as the sentence, which
        # is why the debugger could not be made small. Wrapping lets it shrink; the text
        # just reflows onto more lines. Short field/value labels are left alone.
        for lbl in self.findChildren(QLabel):
            if (lbl.pixmap() is None and not lbl.wordWrap()
                    and len(lbl.text()) > 45):
                lbl.setWordWrap(True)
        self.setMinimumSize(360, 320)

        self._timer = QTimer(self)
        self._timer.timeout.connect(self._on_timer)

        # The stepping keys every debugger shares, so the hands stay on the keyboard.
        for keys, slot in (("F7", self._step_instr), ("F8", self._step_over),
                           ("Shift+F8", self._step_out), ("F4", self._run_to_cursor),
                           ("F9", self._toggle_breakpoint_at_cursor),
                           ("Ctrl+G", self._focus_goto)):
            QShortcut(QKeySequence(keys), self, activated=slot)

    def _focus_goto(self) -> None:
        for i in range(self._tabs.count()):
            if self._tabs.tabText(i) == "Disassembly":
                self._tabs.setCurrentIndex(i); break
        self._dis_goto.setFocus(); self._dis_goto.selectAll()

    def _toggle_breakpoint_at_cursor(self) -> None:
        addr = self._dis_selected_addr()
        if addr is not None:
            self._toggle_breakpoint(addr)

    # ---- lifecycle
    def attach(self, play) -> None:
        # Drop the per-frame subscription on the OUTGOING player first: its hook list
        # outlives this window's idea of what is running, and a stale bound method
        # would keep sampling a machine that is being torn down.
        self._detach_frame_hooks()
        self._play = play
        if play is not None:
            if self._rs_track.isChecked():
                self._rs_set_tracking(True)
            if self._mem_hl.isChecked():
                self._mem_set_highlight(True)
            # A new game means a new core: re-arm the shadow stack if we are visible.
            self._set_callstack(self.isVisible())
            self._push_symbols()
        else:
            self._cs_on = False

    def _detach_frame_hooks(self) -> None:
        play = self._play
        if play is None or not hasattr(play, "frame_hooks"):
            return
        for hook in (self._rs_track_tick, self._mem_sample_access):
            if hook in play.frame_hooks:
                play.frame_hooks.remove(hook)
        # Hand the access logs back to the watchpoints.
        if play.access_probe is not None:
            play.access_probe = None
            try:
                play.apply_debug()
            except Exception:
                pass

    @property
    def _m(self):
        return self._play.machine if self._play is not None else None

    def showEvent(self, e) -> None:  # type: ignore[override]
        self._timer.start(120)
        self.showEvent_resubscribe()   # hideEvent unsubscribed us; restore if still ticked
        self._set_callstack(True)
        self.refresh(); super().showEvent(e)

    def _set_callstack(self, on: bool) -> None:
        """Arm/disarm the core's shadow stack. Tracking costs ~1% of emulation speed,
        which is nothing while debugging and pointless while just playing -- so it
        follows the debug window rather than being a setting nobody would find."""
        m = self._m
        self._cs_on = bool(on)
        if m is None:
            return
        try:
            m.set_callstack(bool(on))
        except Exception:
            self._cs_on = False

    def hideEvent(self, e) -> None:  # type: ignore[override]
        # Closing the debug window must not leave a sampler running in the game loop.
        self._detach_frame_hooks()
        self._set_callstack(False)
        self._timer.stop(); super().hideEvent(e)

    def showEvent_resubscribe(self) -> None:
        if self._rs_track.isChecked():
            self._rs_set_tracking(True)
        if self._mem_hl.isChecked():
            self._mem_set_highlight(True)

    def _on_timer(self) -> None:
        if not self._frozen:
            self.refresh()

    def _on_freeze(self, on: bool) -> None:
        self._frozen = on
        if not on:
            self.refresh()

    # ---- emulation controls
    def _toggle_pause(self) -> None:
        if self._play is None:
            return
        self._play.paused = not self._play.paused
        self.refresh()

    def _step(self) -> None:
        if self._play is None:
            return
        self._play.step_forward()  # integrates with the rewind ring
        self.refresh()

    def _step_back(self) -> None:
        if self._play is None:
            return
        self._play.step_back()
        self.refresh()

    def _reset(self) -> None:
        if self._play is not None:
            self._play._do_reset()  # noqa: SLF001
            self.refresh()

    # ---- export helpers
    def _save_text(self, text: str, default: str) -> None:
        path, _ = QFileDialog.getSaveFileName(self, "Export to file", default,
                                              "Text (*.txt);;All files (*)")
        if path:
            Path(path).write_text(text, encoding="utf-8")
            self._status.setText(f"saved {Path(path).name}")

    def _save_png(self, arr, default: str) -> None:
        if arr is None:
            return
        path, _ = QFileDialog.getSaveFileName(self, "Export image", default, "PNG (*.png)")
        if path:
            a = np.ascontiguousarray(arr)
            h, w = a.shape[:2]
            QImage(a.data, w, h, 3 * w, QImage.Format.Format_RGB888).copy().save(path)
            self._status.setText(f"saved {Path(path).name}")

    def _export_row(self, on_click, label: str = "💾 Export…") -> QHBoxLayout:
        row = QHBoxLayout()
        btn = QPushButton(label); btn.setObjectName("ghost")
        btn.clicked.connect(on_click)
        row.addStretch(); row.addWidget(btn)
        return row

    # ---- CPU tab
    def _cpu_tab(self) -> QWidget:
        w = QWidget(); lay = QVBoxLayout(w)
        self._cpu_text = QPlainTextEdit(); self._cpu_text.setReadOnly(True)
        self._cpu_text.setFont(QFont(_MONO, 11))
        lay.addWidget(self._cpu_text)
        lay.addLayout(self._export_row(
            lambda: self._save_text(self._cpu_text.toPlainText(), "cpu_state.txt")))
        return w

    def _refresh_cpu(self) -> None:
        m = self._m
        if m is None:
            self._cpu_text.setPlainText("(no game running)"); return
        c = m.cpu()
        f = c.flags
        flags = "".join(n for n, bit in
                        (("S", 7), ("Z", 6), ("H", 4), ("V", 2), ("N", 1), ("C", 0))
                        if (f >> bit) & 1) or "-"
        self._auto_load_symbols()
        where = self._sym_text(c.pc)
        lines = [f"PC   {c.pc:06X}{('  <' + where + '>') if where else ''}"
                 f"      flags [{flags}]   IFF {c.iff_level}",
                 f"SR   {c.sr_raw:04X}", ""]
        regs = list(c.regs)
        for i, name in enumerate(REG_NAMES):
            r = regs[i] if i < len(regs) else 0
            lines.append(f"{name} {r:08X}   {name[1:]} {r & 0xFFFF:04X}   "
                         f"{name[1]} {r & 0xFF:02X}")

        # ---- what the CONSOLE is doing, which is not what the on-screen fps shows.
        # That readout is the host's, and the pacer holds it at 60 on any machine fast
        # enough to matter. These three are the emulated machine's own figures.
        if self._play is not None and hasattr(self._play, "perf"):
            p = self._play.perf()
            budget = CYCLES_PER_FRAME
            game = p["game_fps"]
            verdict = ("keeping up" if game >= 58 else
                       "nothing moving on screen" if game <= 0.5 else
                       "below 60 — not updating every frame")
            # Speed is "times faster than real time the host COULD go". As a headline that
            # reads as "the game runs 24x too fast", which is the opposite of what happens
            # -- the pacer keeps playback at 1x. Shown as load instead: the share of real
            # time actually spent emulating, where over 100% is the failure case.
            speed = p["speed"]
            load = (100.0 / speed) if speed > 0 else 0.0
            headroom = (f"{speed:.0f}x headroom" if speed >= 1.5
                        else "THE HOST IS THE LIMIT")
            lines += [
                "",
                "-- console load ------------------------------",
                f"sprite activity   {game:5.1f} /s   ({verdict})",
                f"instr last frame  {p['instr']:5d}      (frame budget {budget} cycles)",
                f"host load         {load:5.1f}%      "
                f"({headroom} — the game still plays at 1x)",
                "",
                "host load is how much of real time this PC spends emulating: 4% means",
                "it could run the console ~24x faster, but the pacer holds playback at",
                "1x so the game runs at hardware speed. Over 100% it cannot keep up.",
                "sprite activity INFERS the game's update rate from the sprite table",
                "changing, so it is a hint, not a measurement: it reads 0 on a still",
                "screen, and misses an update that rewrites identical values.",
            ]
        self._cpu_text.setPlainText("\n".join(lines))

    # ---- symbols -----------------------------------------------------------
    def _auto_load_symbols(self) -> None:
        """Look for the toolchain's .map beside the ROM, once per ROM.

        Tries `<rom>.map` (game.ngc -> game.map) and `<rom>.ngc.map`. Silent when
        there is none -- plenty of ROMs are third-party and have no symbols.
        """
        play = self._play
        rom = getattr(play, "_rom_path", None) if play is not None else None
        stem = rom.stem if rom else None
        if stem == self._symbols_rom:
            return
        self._symbols_rom = stem
        self._symbols = None
        self._symbols_name = ""
        if rom is None:
            self._update_symbol_label()
            return
        for cand in (rom.with_suffix(".map"), Path(str(rom) + ".map")):
            if cand.is_file():
                try:
                    self._symbols = load_map(str(cand))
                    self._symbols_name = cand.name
                except (OSError, ValueError):
                    self._symbols = None
                break
        self._push_symbols()
        self._update_symbol_label()

    def _push_symbols(self) -> None:
        """Hand the table to the player so breakpoint CONDITIONS can name symbols."""
        if self._play is not None:
            self._play.symbols = self._symbols
            self._validate_conditions()

    def _load_symbols_dialog(self) -> None:
        start = ""
        play = self._play
        rom = getattr(play, "_rom_path", None) if play is not None else None
        if rom is not None:
            start = str(rom.parent)
        path, _ = QFileDialog.getOpenFileName(self, "Load symbol map", start,
                                              "Linker map (*.map);;All files (*)")
        if not path:
            return
        try:
            self._symbols = load_map(path)
            self._symbols_name = Path(path).name
        except (OSError, ValueError) as exc:
            self._symbols = None; self._symbols_name = ""
            self._status.setText(f"map failed: {exc}")
        self._push_symbols()
        self._update_symbol_label()
        self.refresh()

    def _update_symbol_label(self) -> None:
        if self._symbols is not None and len(self._symbols):
            self._sym_label.setText(f"{len(self._symbols)} symbols — {self._symbols_name}")
        else:
            self._sym_label.setText("no symbols")

    def _sym_text(self, addr: int) -> str:
        """'name' at its exact address, 'name+12' inside it, '' when unknown."""
        if self._symbols is None:
            return ""
        sym = self._symbols.lookup_address(addr)
        if sym is None:
            return ""
        delta = addr - sym.address
        return sym.name if delta == 0 else f"{sym.name}+{delta:X}"

    def _resolve_addr(self, text: str) -> int | None:
        """Accept either a hex address or a symbol NAME wherever an address is asked
        for. Typing `player_update` beats looking it up in the map by hand."""
        text = (text or "").strip()
        if not text:
            return None
        if self._symbols is not None:
            sym = self._symbols.lookup_name(text)
            if sym is None and not text.startswith("_"):
                sym = self._symbols.lookup_name("_" + text)   # t900ld prefixes with _
            if sym is not None:
                return sym.address
        try:
            return int(text, 16) & 0xFFFFFF
        except ValueError:
            return None

    # ---- Disassembly tab
    _DIS_COLS = ["", "Address", "Symbol", "Bytes", "Instruction"]

    def _disasm_tab(self) -> QWidget:
        w = QWidget(); lay = QVBoxLayout(w)

        # -- navigation: this listing used to be nailed to the PC with no way to look
        # anywhere else, which makes reading a routine you are not standing in impossible.
        nav = QHBoxLayout()
        nav.addWidget(QLabel("Go to"))
        self._dis_goto = QLineEdit(); self._dis_goto.setFixedWidth(150)
        self._dis_goto.setFont(QFont(_MONO, 10))
        self._dis_goto.setPlaceholderText("address or symbol")
        self._dis_goto.returnPressed.connect(self._dis_do_goto)
        nav.addWidget(self._dis_goto)
        b = QPushButton("Go"); b.setObjectName("ghost"); b.clicked.connect(self._dis_do_goto)
        nav.addWidget(b)
        self._dis_follow = QCheckBox("follow PC"); self._dis_follow.setChecked(True)
        self._dis_follow.setToolTip("Keep the listing anchored on the program counter.\n"
                                    "Unticked, it stays where you scrolled to.")
        self._dis_follow.toggled.connect(self._dis_on_follow)
        nav.addWidget(self._dis_follow)
        for lab, delta, tip in (("▲", -1, "page up"), ("▼", 1, "page down")):
            pb = QPushButton(lab); pb.setObjectName("ghost"); pb.setFixedWidth(30)
            pb.setToolTip(tip)
            pb.clicked.connect(lambda _c, d=delta: self._dis_page(d))
            nav.addWidget(pb)
        nav.addWidget(QLabel("Lines"))
        self._dis_count = QSpinBox(); self._dis_count.setRange(8, 400); self._dis_count.setValue(32)
        self._dis_count.valueChanged.connect(self.refresh)
        nav.addWidget(self._dis_count)
        nav.addStretch()
        self._sym_label = QLabel("no symbols"); self._sym_label.setObjectName("hint")
        nav.addWidget(self._sym_label)
        sb = QPushButton("Load .map…"); sb.setObjectName("ghost")
        sb.setToolTip("Load the linker map so addresses show function names.")
        sb.clicked.connect(self._load_symbols_dialog)
        nav.addWidget(sb)
        lay.addLayout(nav)

        # -- stepping. The debugger only had frame-granularity stepping, which is
        # useless for following code: one frame is tens of thousands of instructions.
        step = QHBoxLayout()
        for lab, slot, tip in (
                ("⤓ Step", self._step_instr, "Execute ONE instruction (F7)"),
                ("⤼ Over", self._step_over, "One instruction, but run a call to its return (F8)"),
                ("⤴ Out", self._step_out, "Run until the current routine returns (Shift+F8)"),
                ("→ Run to", self._run_to_cursor, "Run until PC reaches the selected line (F4)")):
            b = QPushButton(lab); b.setObjectName("ghost"); b.setToolTip(tip)
            b.clicked.connect(slot); step.addWidget(b)
        step.addSpacing(12)
        step.addWidget(QLabel("Trace"))
        self._trace_count = QSpinBox(); self._trace_count.setRange(64, 500000)
        self._trace_count.setValue(5000); self._trace_count.setSingleStep(1000)
        step.addWidget(self._trace_count)
        self._trace_detail = QCheckBox("regs + memory")
        self._trace_detail.setToolTip("Log the registers each instruction wrote and every\n"
                                      "memory read/write it made, not just the mnemonic.")
        self._trace_detail.setChecked(True)
        step.addWidget(self._trace_detail)
        self._btn_trace = QPushButton("⏺ Trace to file…"); self._btn_trace.setObjectName("ghost")
        self._btn_trace.setToolTip("Run that many instructions and write every one to a file "
                                   "(advances the game).")
        self._btn_trace.clicked.connect(self._trace_to_file)
        step.addWidget(self._btn_trace)
        step.addStretch()
        lay.addLayout(step)

        t = QTableWidget(0, len(self._DIS_COLS))
        t.setHorizontalHeaderLabels(self._DIS_COLS)
        t.verticalHeader().setVisible(False)
        t.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        t.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        hh = t.horizontalHeader()
        hh.setSectionResizeMode(0, QHeaderView.ResizeMode.Fixed)
        t.setColumnWidth(0, 24)
        for c in (1, 2, 3):
            hh.setSectionResizeMode(c, QHeaderView.ResizeMode.ResizeToContents)
        hh.setSectionResizeMode(4, QHeaderView.ResizeMode.Stretch)
        # Click the left gutter to arm/disarm a breakpoint on that line, the way every
        # debugger does it -- the Breakpoints tab is for editing conditions, not for
        # typing addresses you are already looking at.
        t.cellClicked.connect(self._dis_cell_clicked)
        self._dis_table = t
        lay.addWidget(t, 1)
        lay.addLayout(self._export_row(lambda: self._save_text(self._dis_dump(), "disasm.txt")))
        return w

    # -- navigation helpers
    def _dis_anchor(self) -> int:
        """Where the listing starts: the PC when following, else where we scrolled."""
        if self._dis_base is not None:
            return self._dis_base
        m = self._m
        return m.cpu().pc if m is not None else 0

    def _dis_do_goto(self) -> None:
        addr = self._resolve_addr(self._dis_goto.text())
        if addr is None:
            self._dis_goto.setStyleSheet(f"color:{PALETTE.error}")
            return
        self._dis_goto.setStyleSheet("")
        self._dis_follow.setChecked(False)     # going somewhere means stop following
        self._dis_base = addr                  # AFTER the checkbox: its toggled handler
        self._refresh_disasm()                 # rewrites _dis_base from the old anchor

    def _dis_on_follow(self, on: bool) -> None:
        self._dis_base = None if on else self._dis_anchor()
        self._refresh_disasm()

    def _dis_page_refresh(self) -> None:
        self._refresh_disasm()

    def _dis_page(self, direction: int) -> None:
        """Page the listing. Backwards is a guess -- instructions are variable length,
        so there is no exact 'previous instruction'. Stepping back by two bytes per
        line and re-syncing forwards is the standard approximation."""
        rows = self._dis_rows
        n = self._dis_count.value()
        anchor = self._dis_anchor()
        self._dis_follow.setChecked(False)     # before _dis_base, see _dis_do_goto
        if direction > 0 and rows:
            self._dis_base = rows[-1]
        else:
            self._dis_base = max(0, anchor - 2 * n)
        self._refresh_disasm()

    def _dis_selected_addr(self) -> int | None:
        r = self._dis_table.currentRow()
        if 0 <= r < len(self._dis_rows):
            return self._dis_rows[r]
        return None

    def _dis_cell_clicked(self, row: int, col: int) -> None:
        if col != 0 or not (0 <= row < len(self._dis_rows)):
            return
        self._toggle_breakpoint(self._dis_rows[row])

    def _toggle_breakpoint(self, addr: int) -> None:
        play = self._play
        if play is None:
            return
        items = play.breaks.items
        for i, bp in enumerate(items):
            if bp.pc == addr:
                del items[i]
                break
        else:
            items.append(ExecBreak(addr, "", True))
        play._save_breaks()          # noqa: SLF001  (persists + re-arms the core)
        self._breaks_rom = None      # make the Breakpoints tab repopulate
        self._refresh_disasm()       # repaint the gutter even from another tab

    def _breakpoint_pcs(self) -> dict[int, bool]:
        play = self._play
        if play is None:
            return {}
        return {bp.pc: bp.enabled for bp in play.breaks.items}

    def _dis_lines(self, limit: int | None = None) -> list[tuple[int, str, str, str]]:
        """(address, symbol, raw bytes, assembly) from the current anchor."""
        m = self._m
        if m is None:
            return []
        bus = _Bus(m)
        pc = self._dis_anchor()
        out = []
        for _ in range(limit if limit is not None else self._dis_count.value()):
            try:
                d = decode_instruction_at(bus, pc)
            except Exception:
                d = None
            if d is None or d.status != "decoded" or d.next_sequential_pc is None:
                # ⚠️ Do NOT stop the listing here. The decoder has gaps (BIOS code hits
                # them), and bailing out at the first unknown byte left the rest of the
                # window blank -- so one undecodable opcode hid the entire routine after
                # it. Show the byte, resync one byte on, and carry on: a disassembler
                # that gives up is worse than one that admits a hole.
                try:
                    byte = m.read(pc & 0xFFFFFF, 1)[0]
                    raw = f"{byte:02X}"
                except Exception:
                    raw = ""
                out.append((pc, self._sym_text(pc), raw, "??"))
                pc = (pc + 1) & 0xFFFFFF
                continue
            raw = (d.raw_bytes or b"").hex(" ")
            asm = d.assembly or (d.mnemonic or "??")
            out.append((pc, self._sym_text(pc), raw, asm))
            pc = d.next_sequential_pc
        return out

    def _dis_dump(self) -> str:
        return "\n".join(f"{a:06X}  {s:<24} {r:<14} {i}" for a, s, r, i in self._dis_lines())

    def _refresh_disasm(self) -> None:
        m = self._m
        t = self._dis_table
        if m is None:
            t.setRowCount(0); self._dis_rows = []
            return
        self._auto_load_symbols()
        cur_pc = m.cpu().pc
        bps = self._breakpoint_pcs()
        lines = self._dis_lines()
        self._dis_rows = [a for a, _s, _r, _i in lines]
        t.setRowCount(len(lines))
        mono = QFont(_MONO, 10)
        for row, (addr, sym, raw, asm) in enumerate(lines):
            # gutter: ● an armed breakpoint, ○ a disabled one, ▶ where the PC is
            if addr in bps:
                gut = "●" if bps[addr] else "○"
            else:
                gut = "▶" if addr == cur_pc else ""
            cells = [gut, f"{addr:06X}", sym, raw, asm]
            for col, text in enumerate(cells):
                it = t.item(row, col)
                if it is None:
                    it = QTableWidgetItem()
                    if col:
                        it.setFont(mono)
                    else:
                        it.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                    t.setItem(row, col, it)
                it.setText(text)
            # The current instruction is highlighted so the eye finds it after a step.
            for col in range(len(cells)):
                it = t.item(row, col)
                it.setBackground(_PC_ROW_BG if addr == cur_pc else _NO_BRUSH)

    # -- stepping actions
    def _decoded_at_pc(self):
        m = self._m
        if m is None:
            return None
        try:
            return decode_instruction_at(_Bus(m), m.cpu().pc)
        except Exception:
            return None

    def _after_step(self, note: str) -> None:
        if self._play is not None:
            self._play.overlay.setText("⏸ " + note)
        # Refresh FIRST: it rewrites the status line with "paused"/"running", so
        # setting the note before it would flash and vanish. Also force the
        # disassembly to redraw even when another tab is showing -- you can step
        # from the CPU or Memory tab and expect the listing to have kept up.
        self.refresh()
        self._refresh_disasm()
        self._status.setText(note)

    def _step_instr(self) -> None:
        if self._play is None or self._m is None:
            return
        self._play.step_instruction(1)
        self._after_step(f"step — PC {self._m.cpu().pc:06X} {self._sym_text(self._m.cpu().pc)}")

    def _step_over(self) -> None:
        play = self._play
        if play is None or self._m is None:
            return
        d = self._decoded_at_pc()
        is_call = bool(d and (d.mnemonic or "") in _CALL_MNEMONICS)
        nxt = d.next_sequential_pc if d else None
        ran = play.step_over(nxt, is_call)
        self._after_step(f"step over — {ran} instr — PC {self._m.cpu().pc:06X} "
                         f"{self._sym_text(self._m.cpu().pc)}")

    def _step_out(self) -> None:
        play = self._play
        if play is None or self._m is None:
            return
        ran = play.step_out()
        self._after_step(f"step out — {ran} instr — PC {self._m.cpu().pc:06X} "
                         f"{self._sym_text(self._m.cpu().pc)}")

    def _run_to_cursor(self) -> None:
        play = self._play
        if play is None or self._m is None:
            return
        addr = self._dis_selected_addr()
        if addr is None:
            self._status.setText("run to: select a line first")
            return
        reached, ran = play.run_until_pc([addr])
        where = f"{self._m.cpu().pc:06X} {self._sym_text(self._m.cpu().pc)}"
        self._after_step(f"run to {addr:06X} — {'reached' if reached else 'stopped'} "
                         f"after {ran} instr — PC {where}")

    def _trace_to_file(self) -> None:
        m = self._m
        if m is None:
            return
        total = self._trace_count.value()
        path, _ = QFileDialog.getSaveFileName(self, "Trace execution to file",
                                              "trace.txt", "Text (*.txt)")
        if not path:
            return
        was_paused = self._play.paused
        self._play.paused = True
        detail = self._trace_detail.isChecked()
        lines = [f"; execution trace, {total} instructions from PC={m.cpu().pc:06X}"]
        if self._symbols is not None:
            lines.append(f"; symbols: {self._symbols_name}")
        if detail:
            lines.append("; columns: PC  bytes  instruction  ; regs=  R=read  W=write")
        remaining = total
        while remaining > 0:
            _summ, recs = m.run(min(remaining, 4096), record=True)
            if not recs:
                break
            for r in recs:
                raw = bytes(r.raw[:r.raw_len])
                sym = self._sym_text(r.pc)
                head = f"{r.pc:06X}  {raw.hex(' '):<14} {_disasm_bytes(raw, r.pc):<28}"
                if sym:
                    head = f"{head} ; {sym}"
                if detail:
                    # The core already records which registers were written and every
                    # memory access -- the trace used to throw all of it away and log
                    # only the mnemonic, which is the least useful half.
                    extra = _trace_detail_text(r)
                    if extra:
                        head = f"{head}  {extra}" if sym else f"{head} {extra}"
                lines.append(head)
            remaining -= len(recs)
        Path(path).write_text("\n".join(lines), encoding="utf-8")
        self._status.setText(f"traced {total - remaining} instr -> {Path(path).name}")
        self._play.paused = was_paused
        self._play._blit()  # noqa: SLF001
        self.refresh()

    # ---- Call Stack tab
    def _callstack_tab(self) -> QWidget:
        w = QWidget(); lay = QVBoxLayout(w)
        lay.addWidget(QLabel(
            "How execution got here. The innermost routine is at the top; double-click "
            "a row to show it in the disassembly.\n"
            "Tracked as a shadow stack while the debugger is open — a CALL is seen by "
            "the return address landing on the stack, a RET by the stack unwinding past "
            "it, so a plain PUSH is never mistaken for a call."))
        t = QTableWidget(0, 5)
        t.setHorizontalHeaderLabels(["#", "Routine", "Address", "Returns to", "Called from"])
        t.verticalHeader().setVisible(False)
        t.setFont(QFont(_MONO, 10))
        t.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        t.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        hh = t.horizontalHeader()
        hh.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        hh.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        for c in (2, 3, 4):
            hh.setSectionResizeMode(c, QHeaderView.ResizeMode.ResizeToContents)
        t.cellDoubleClicked.connect(self._cs_goto)
        self._cs_table = t
        self._cs_rows: list[int] = []
        lay.addWidget(t, 1)
        self._cs_note = QLabel(""); self._cs_note.setObjectName("hint")
        lay.addWidget(self._cs_note)
        lay.addLayout(self._export_row(lambda: self._save_text(self._cs_dump(), "callstack.txt")))
        return w

    def _cs_frames(self) -> list[tuple[int, int, int, int]]:
        """(entry_pc, return_pc, caller_pc, entry_sp), innermost FIRST."""
        m = self._m
        if m is None:
            return []
        try:
            frames = m.callstack()
        except Exception:
            return []
        return [(f.entry_pc, f.return_pc, f.caller_pc, f.entry_sp) for f in reversed(frames)]

    def _cs_dump(self) -> str:
        lines = []
        m = self._m
        if m is not None:
            pc = m.cpu().pc
            lines.append(f"  PC  {pc:06X}  {self._sym_text(pc)}")
        for i, (entry, ret, caller, sp) in enumerate(self._cs_frames()):
            lines.append(f"{i:3d}  {entry:06X}  {self._sym_text(entry):<28} "
                         f"ret {ret:06X}  from {caller:06X}  sp {sp:08X}")
        return "\n".join(lines)

    def _cs_goto(self, row: int, _col: int) -> None:
        if 0 <= row < len(self._cs_rows):
            self._dis_follow.setChecked(False)
            self._dis_base = self._cs_rows[row]
            self._refresh_disasm()
            for i in range(self._tabs.count()):
                if self._tabs.tabText(i) == "Disassembly":
                    self._tabs.setCurrentIndex(i); break

    def _refresh_callstack(self) -> None:
        m = self._m
        t = self._cs_table
        if m is None:
            t.setRowCount(0); self._cs_rows = []
            self._cs_note.setText("(no game running)")
            return
        self._auto_load_symbols()
        frames = self._cs_frames()
        # Row 0 is where the PC actually IS, which is not a frame -- the innermost
        # routine has been entered but has not called anything yet.
        pc = m.cpu().pc
        rows = [(pc, None, None)] + [(e, r, c) for e, r, c, _s in frames]
        self._cs_rows = [a for a, _r, _c in rows]
        t.setRowCount(len(rows))
        for i, (addr, ret, caller) in enumerate(rows):
            cells = ["▶" if i == 0 else str(i),
                     self._sym_text(addr) or "(no symbol)",
                     f"{addr:06X}",
                     f"{ret:06X}" if ret is not None else "",
                     f"{caller:06X}" if caller is not None else ""]
            for c, text in enumerate(cells):
                it = t.item(i, c)
                if it is None:
                    it = QTableWidgetItem(); t.setItem(i, c, it)
                it.setText(text)
                it.setBackground(_PC_ROW_BG if i == 0 else _NO_BRUSH)
        try:
            dropped = m.callstack_overflow()
        except Exception:
            dropped = 0
        if not self._cs_on:
            self._cs_note.setText("tracking is off — open this tab to enable it")
        elif dropped:
            self._cs_note.setText(f"⚠ {dropped} frames dropped (deeper than 64) — "
                                  "the view is truncated, not wrong")
        else:
            self._cs_note.setText(f"{len(frames)} frames")

    # ---- Events tab (the raster timeline)
    def _events_tab(self) -> QWidget:
        w = QWidget(); lay = QVBoxLayout(w)
        lay.addWidget(QLabel(
            "WHEN in the frame things happen. Every video-register write and every "
            "interrupt, plotted at the scanline and cycle it occurred on.\n"
            "This is the view for raster work: a scroll split, an HBlank HUD or a "
            "mid-frame palette swap is correct or broken purely as a function of "
            "timing, and no write log can show that."))
        bar = QHBoxLayout()
        self._ev_on = QCheckBox("record")
        self._ev_on.setToolTip("Arm the core's event log over the chosen window.")
        self._ev_on.toggled.connect(self._ev_set_recording)
        bar.addWidget(self._ev_on)
        bar.addWidget(QLabel("Window"))
        self._ev_lo = QLineEdit("008000"); self._ev_hi = QLineEdit("0083FF")
        for e in (self._ev_lo, self._ev_hi):
            e.setFixedWidth(74); e.setFont(QFont(_MONO, 10))
            e.editingFinished.connect(self._ev_rearm)
        bar.addWidget(self._ev_lo); bar.addWidget(QLabel("‥")); bar.addWidget(self._ev_hi)
        b = QPushButton("video regs"); b.setObjectName("ghost")
        b.setToolTip("0x8000-0x83FF: scroll, palette, window, raster control")
        b.clicked.connect(lambda: (self._ev_lo.setText("008000"),
                                   self._ev_hi.setText("0083FF"), self._ev_rearm()))
        bar.addWidget(b)
        bar.addStretch()
        self._ev_note = QLabel(""); self._ev_note.setObjectName("hint")
        bar.addWidget(self._ev_note)
        lay.addLayout(bar)

        self._ev_canvas = QLabel(); self._ev_canvas.setObjectName("lcd")
        self._ev_canvas.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
        lay.addWidget(self._ev_canvas)

        t = QTableWidget(0, 6)
        t.setHorizontalHeaderLabels(["Line", "Cycle", "Kind", "Address", "Value", "PC"])
        t.verticalHeader().setVisible(False)
        t.setFont(QFont(_MONO, 10))
        t.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        t.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        hh = t.horizontalHeader()
        for c in range(5):
            hh.setSectionResizeMode(c, QHeaderView.ResizeMode.ResizeToContents)
        hh.setSectionResizeMode(5, QHeaderView.ResizeMode.Stretch)
        self._ev_table = t
        lay.addWidget(t, 1)
        lay.addLayout(self._export_row(lambda: self._save_text(self._ev_dump(), "events.txt")))
        return w

    def _ev_window(self) -> tuple[int, int]:
        try:
            lo = int(self._ev_lo.text(), 16) & 0xFFFFFF
            hi = int(self._ev_hi.text(), 16) & 0xFFFFFF
        except ValueError:
            return (0x008000, 0x0083FF)
        return (lo, hi)

    def _ev_set_recording(self, on: bool) -> None:
        m = self._m
        if m is None:
            return
        try:
            m.set_event_log(*(self._ev_window() if on else (1, 0)))
        except Exception:
            pass

    def _ev_rearm(self) -> None:
        if self._ev_on.isChecked():
            self._ev_set_recording(True)

    def _ev_events(self) -> list:
        m = self._m
        if m is None or not self._ev_on.isChecked():
            return []
        try:
            return m.event_log(2048)
        except Exception:
            return []

    def _ev_dump(self) -> str:
        out = ["; line  cycle  kind   addr    value  pc"]
        for e in self._ev_events():
            kind = "IRQ" if e.type == native.EVENT_IRQ else "W"
            out.append(f"{e.scanline:5d}  {e.cycle:5d}  {kind:<5} {e.addr:06X}  "
                       f"{e.value:02X}     {e.pc:06X}")
        return "\n".join(out)

    def _refresh_events(self) -> None:
        events = self._ev_events()
        self._ev_draw(events)
        t = self._ev_table
        # Newest first: the interesting event is the one that just happened.
        shown = list(reversed(events))[:400]
        t.setRowCount(len(shown))
        for r, e in enumerate(shown):
            kind = "IRQ" if e.type == native.EVENT_IRQ else "write"
            addr = (f"vec {e.addr}" if e.type == native.EVENT_IRQ else f"{e.addr:06X}")
            for c, text in enumerate((str(e.scanline), str(e.cycle), kind, addr,
                                      f"{e.value:02X}", f"{e.pc:06X}")):
                it = t.item(r, c)
                if it is None:
                    it = QTableWidgetItem(); t.setItem(r, c, it)
                it.setText(text)
        if not self._ev_on.isChecked():
            self._ev_note.setText("not recording")
        else:
            m = self._m
            total = m.event_log_count() if m is not None else 0
            self._ev_note.setText(
                f"{total} events" + (f" (showing the last {len(events)})"
                                     if total > len(events) else ""))

    # The timeline bitmap: X is the cycle within a scanline, Y is the scanline.
    _EV_W, _EV_H = 515, 199

    def _ev_draw(self, events) -> None:
        """Plot the frame as a scanline x cycle grid: one pixel per cycle, per line."""
        img = np.zeros((self._EV_H, self._EV_W, 3), dtype=np.uint8)
        img[:, :] = (14, 16, 20)
        # The visible area, so "during the picture" vs "in VBlank" is readable at a glance.
        img[:152, :] = (22, 26, 33)
        img[:, ::64] = np.maximum(img[:, ::64], (30, 34, 42))   # cycle gridlines
        for e in events:
            y = min(self._EV_H - 1, int(e.scanline))
            x = min(self._EV_W - 1, int(e.cycle))
            colour = (255, 190, 70) if e.type == native.EVENT_IRQ else (90, 200, 255)
            # A 3px mark: one pixel per event is invisible at this scale.
            img[max(0, y - 1):y + 2, max(0, x - 1):x + 2] = colour
        h, wid = img.shape[:2]
        qimg = QImage(np.ascontiguousarray(img).data, wid, h, 3 * wid,
                      QImage.Format.Format_RGB888).copy()
        self._ev_canvas.setPixmap(QPixmap.fromImage(qimg).scaled(
            wid, h * 2, Qt.AspectRatioMode.IgnoreAspectRatio,
            Qt.TransformationMode.FastTransformation))

    # ---- Memory tab
    def _mem_tab(self) -> QWidget:
        w = QWidget(); lay = QVBoxLayout(w)
        bar = QHBoxLayout()
        self._mem_region = QComboBox()
        for name, addr in MEM_REGIONS:
            self._mem_region.addItem(name, addr)
        self._mem_region.currentIndexChanged.connect(self._on_region)
        self._mem_addr = QLineEdit("004000"); self._mem_addr.setFixedWidth(120)
        self._mem_addr.setFont(QFont(_MONO, 10))
        self._mem_addr.setToolTip("Address in hex, or a symbol name when a .map is loaded.")
        self._mem_addr.editingFinished.connect(self._refresh_mem)
        self._mem_rows = QSpinBox(); self._mem_rows.setRange(8, 4096); self._mem_rows.setValue(24)
        self._mem_rows.valueChanged.connect(self._refresh_mem)
        bar.addWidget(QLabel("Region")); bar.addWidget(self._mem_region)
        bar.addWidget(QLabel("Addr")); bar.addWidget(self._mem_addr)
        bar.addWidget(QLabel("Rows")); bar.addWidget(self._mem_rows)
        bar.addSpacing(12)
        # Colour each byte by what last touched it -- the one thing that turns a
        # memory viewer from a hex dump into a live picture of what the program is
        # doing. Now that the core has BOTH access logs, this is just a matter of
        # reading them once a frame.
        self._mem_hl = QCheckBox("highlight accesses")
        self._mem_hl.setToolTip(
            "Tint bytes the game just READ (blue) or WROTE (red), fading over a second.\n"
            "The core has one read-log and one write-log window, so while this is on it\n"
            "owns them and read/write WATCHPOINTS are suspended.")
        self._mem_hl.toggled.connect(self._mem_set_highlight)
        bar.addWidget(self._mem_hl)
        bar.addStretch()
        self._mem_note = QLabel(""); self._mem_note.setObjectName("hint")
        bar.addWidget(self._mem_note)
        lay.addLayout(bar)

        t = QTableWidget(0, 18)
        t.setHorizontalHeaderLabels(
            ["Address"] + [f"{i:X}" for i in range(16)] + ["ASCII"])
        t.verticalHeader().setVisible(False)
        t.setFont(QFont(_MONO, 10))
        hh = t.horizontalHeader()
        hh.setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        hh.setSectionResizeMode(17, QHeaderView.ResizeMode.Stretch)
        # Editing a byte where you can see it, instead of retyping its address into a
        # separate poke box. Type two hex digits and press Enter.
        t.itemChanged.connect(self._on_mem_item)
        self._mem_table = t
        self._mem_building = False
        self._mem_base = 0x004000
        # addr -> [reads, writes] seen in the last sampled frames, for the tint.
        self._mem_access: dict[int, list[int]] = {}
        lay.addWidget(t, 1)
        lay.addWidget(QLabel(
            "Click a byte and type two hex digits to change it. Symbol names work in "
            "the address box."))
        lay.addLayout(self._export_row(lambda: self._save_text(self._mem_dump(), "memory.txt")))
        return w

    def _on_region(self) -> None:
        self._mem_addr.setText(f"{self._mem_region.currentData():06X}")
        self._refresh_mem()

    def _mem_set_highlight(self, on: bool) -> None:
        play = self._play
        self._mem_access.clear()
        if play is None or not hasattr(play, "frame_hooks"):
            return
        if on:
            if self._mem_sample_access not in play.frame_hooks:
                play.frame_hooks.append(self._mem_sample_access)
            self._mem_apply_probe()
        else:
            if self._mem_sample_access in play.frame_hooks:
                play.frame_hooks.remove(self._mem_sample_access)
            play.access_probe = None
            play.apply_debug()
        self._mem_update_note()

    def _mem_update_note(self) -> None:
        play = self._play
        if not self._mem_hl.isChecked():
            self._mem_note.setText("")
            return
        suspended = play is not None and (play.watches.write_watches()
                                          or play.watches.read_watches())
        self._mem_note.setText("⚠ read/write watchpoints suspended while highlighting"
                               if suspended else "sampling accesses")

    def _mem_apply_probe(self) -> None:
        """Point the core's access logs at exactly the bytes on screen."""
        play = self._play
        if play is None or not self._mem_hl.isChecked():
            return
        span = 16 * self._mem_rows.value()
        play.access_probe = (self._mem_base & 0xFFFFFF,
                             min(0xFFFFFF, self._mem_base + span - 1))
        play.apply_debug()

    def _mem_sample_access(self) -> None:
        """Per-frame hook: fold this frame's accesses into the tint map and age the
        rest, so a byte touched once glows and then fades instead of staying lit."""
        m = self._m
        if m is None:
            return
        for addr in list(self._mem_access):
            hit = self._mem_access[addr]
            hit[0] = max(0, hit[0] - 1)
            hit[1] = max(0, hit[1] - 1)
            if not hit[0] and not hit[1]:
                del self._mem_access[addr]
        try:
            if m.read_log_count():
                for rec in m.read_log(2048):
                    self._mem_access.setdefault(rec.addr, [0, 0])[0] = _ACCESS_FADE
            if m.write_log_count():
                for rec in m.write_log(2048):
                    self._mem_access.setdefault(rec.addr, [0, 0])[1] = _ACCESS_FADE
            # Consume the rings. Re-arming is what zeroes the counters, and without it
            # this re-reads the SAME entries on every sample: the fade never expires and
            # the map grows without bound. The play loop also re-arms per frame, but a
            # sampler that only works when someone else resets it is a trap.
            probe = getattr(self._play, "access_probe", None)
            if probe is not None:
                m.set_read_log(*probe)
                m.set_write_log(*probe)
        except Exception:
            pass

    def _mem_bytes(self) -> tuple[int, bytes]:
        m = self._m
        if m is None:
            return (self._mem_base, b"")
        addr = self._resolve_addr(self._mem_addr.text())
        if addr is None:
            self._mem_addr.setStyleSheet(f"color:{PALETTE.error}")
            return (self._mem_base, m.read(self._mem_base, 16 * self._mem_rows.value()))
        self._mem_addr.setStyleSheet("")
        base = addr & 0xFFFFF0
        if base != self._mem_base:
            self._mem_base = base
            self._mem_apply_probe()      # follow the view with the access window
        return (base, m.read(base & 0xFFFFFF, 16 * self._mem_rows.value()))

    def _mem_dump(self) -> str:
        base, data = self._mem_bytes()
        out = []
        for r in range(len(data) // 16):
            chunk = data[r * 16:(r + 1) * 16]
            ascii_ = "".join(chr(b) if 32 <= b < 127 else "." for b in chunk)
            out.append(f"{base + r * 16:06X}  {' '.join(f'{b:02X}' for b in chunk):<47}  {ascii_}")
        return "\n".join(out)

    def _on_mem_item(self, item) -> None:
        """A byte cell was edited: parse two hex digits and poke it."""
        if self._mem_building or self._m is None:
            return
        col = item.column()
        if not (1 <= col <= 16):
            return
        addr = self._mem_base + item.row() * 16 + (col - 1)
        try:
            value = int(item.text().strip(), 16)
        except ValueError:
            self._refresh_mem(); return          # bad input -> put the old byte back
        if not (0 <= value <= 0xFF):
            self._refresh_mem(); return
        self._m.write(addr & 0xFFFFFF, bytes([value]))
        self._status.setText(f"wrote {value:02X} to {addr:06X}")
        self._refresh_mem()

    def _refresh_mem(self) -> None:
        m = self._m
        t = self._mem_table
        if m is None:
            t.setRowCount(0); return
        self._auto_load_symbols()
        base, data = self._mem_bytes()
        nrows = len(data) // 16
        self._mem_building = True
        try:
            t.setRowCount(nrows)
            for r in range(nrows):
                addr = base + r * 16
                chunk = data[r * 16:(r + 1) * 16]
                cells = [f"{addr:06X}"] + [f"{b:02X}" for b in chunk] + \
                        ["".join(chr(b) if 32 <= b < 127 else "." for b in chunk)]
                for c, text in enumerate(cells):
                    it = t.item(r, c)
                    if it is None:
                        it = QTableWidgetItem()
                        if c == 0 or c == 17:     # address + ascii are read-only
                            it.setFlags(Qt.ItemFlag.ItemIsEnabled)
                        t.setItem(r, c, it)
                    it.setText(text)
                    if 1 <= c <= 16:
                        hit = self._mem_access.get(addr + c - 1)
                        # A byte both read and written this frame shows as written:
                        # the write is the more informative half.
                        it.setBackground(_WRITE_BG if (hit and hit[1]) else
                                         _READ_BG if (hit and hit[0]) else _NO_BRUSH)
        finally:
            self._mem_building = False
        self._mem_update_note()

    # ---- Watch tab
    _SIZE_OPTS = [("1", 1), ("2", 2), ("4", 4)]
    _FMT_OPTS = [("hex", "hex"), ("dec", "u"), ("s.dec", "s")]
    _BREAK_OPTS = [("—", ""), ("change", "change"), ("write", "write"), ("read", "read"),
                   ("=", "="), ("≠", "!="), ("<", "<"), (">", ">"), ("≤", "<="), ("≥", ">=")]
    _WATCH_COLS = ["Name", "Addr", "Size", "Fmt", "Break", "Value", "Lock", "Live"]

    def _watch_tab(self) -> QWidget:
        w = QWidget(); lay = QVBoxLayout(w)
        lay.addWidget(QLabel(
            "Name memory addresses and watch them live. Break: 'change' / a comparison "
            "pauses on value; 'write' and 'read' pause and show which PC touched it "
            "('read' ignores instruction fetches, so it means the code USED the value). "
            "Lock freezes the address to Value. Saved per ROM."))
        t = QTableWidget(0, len(self._WATCH_COLS))
        t.setHorizontalHeaderLabels(self._WATCH_COLS)
        t.verticalHeader().setVisible(False)
        t.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        hh = t.horizontalHeader()
        hh.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        for c in range(1, len(self._WATCH_COLS)):
            hh.setSectionResizeMode(c, QHeaderView.ResizeMode.ResizeToContents)
        t.itemChanged.connect(self._on_watch_item)
        self._watch_table = t
        lay.addWidget(t, 1)
        bar = QHBoxLayout()
        add = QPushButton("＋ Add"); add.setObjectName("ghost"); add.clicked.connect(self._watch_add)
        rem = QPushButton("－ Remove"); rem.setObjectName("ghost"); rem.clicked.connect(self._watch_remove)
        bar.addWidget(add); bar.addWidget(rem); bar.addStretch()
        lay.addLayout(bar)
        return w

    def _combo_widget(self, options, current) -> QComboBox:
        cb = QComboBox()
        for label, data in options:
            cb.addItem(label, data)
        i = cb.findData(current)
        if i >= 0:
            cb.setCurrentIndex(i)
        cb.currentIndexChanged.connect(self._commit_watches)
        return cb

    def _watch_add_row(self, wt: Watch | None = None) -> None:
        t = self._watch_table
        r = t.rowCount(); t.insertRow(r)
        t.setItem(r, 0, QTableWidgetItem(wt.name if wt else ""))
        t.setItem(r, 1, QTableWidgetItem(f"{wt.addr:06X}" if wt else ""))
        t.setCellWidget(r, 2, self._combo_widget(self._SIZE_OPTS, wt.size if wt else 1))
        t.setCellWidget(r, 3, self._combo_widget(self._FMT_OPTS, wt.fmt if wt else "hex"))
        t.setCellWidget(r, 4, self._combo_widget(self._BREAK_OPTS, wt.brk if wt else ""))
        t.setItem(r, 5, QTableWidgetItem(str(wt.value) if wt else ""))
        lock = QTableWidgetItem()
        lock.setFlags(Qt.ItemFlag.ItemIsUserCheckable | Qt.ItemFlag.ItemIsEnabled)
        lock.setCheckState(Qt.CheckState.Checked if (wt and wt.lock) else Qt.CheckState.Unchecked)
        lock.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
        t.setItem(r, 6, lock)
        live = QTableWidgetItem("")
        live.setFlags(Qt.ItemFlag.ItemIsEnabled)              # read-only, not editable
        live.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
        t.setItem(r, 7, live)

    def _watch_add(self) -> None:
        self._watch_building = True
        self._watch_add_row()
        self._watch_building = False

    def _watch_remove(self) -> None:
        r = self._watch_table.currentRow()
        if r >= 0:
            self._watch_table.removeRow(r)
            self._commit_watches()

    def _on_watch_item(self, _item) -> None:
        if not self._watch_building:
            self._commit_watches()

    def _row_to_watch(self, r: int) -> Watch | None:
        t = self._watch_table
        cell = t.item(r, 1)
        addr_txt = cell.text().strip() if cell else ""
        if not addr_txt:
            return None
        try:
            addr = int(addr_txt, 16)
        except ValueError:
            return None
        name = (t.item(r, 0).text().strip() if t.item(r, 0) else "")
        size = t.cellWidget(r, 2).currentData()
        fmt = t.cellWidget(r, 3).currentData()
        brk = t.cellWidget(r, 4).currentData()
        vtxt = (t.item(r, 5).text().strip() if t.item(r, 5) else "")
        try:
            value = int(vtxt, 0) if vtxt else 0
        except ValueError:
            value = 0
        lock_item = t.item(r, 6)
        lock = bool(lock_item and lock_item.checkState() == Qt.CheckState.Checked)
        return Watch(name, addr, size, fmt, brk, value, lock)

    def _commit_watches(self) -> None:
        if self._play is None or self._watch_building:
            return
        ws = [w for r in range(self._watch_table.rowCount())
              if (w := self._row_to_watch(r)) is not None]
        self._play.watches.watches = ws
        self._play._save_watches()  # noqa: SLF001

    def _rebuild_watch_table(self) -> None:
        """Replace every row from the play's watch list. Structural table edits must
        NOT run inside a refresh/signal (mutating the widget tree from a tab-change or
        paint handler can crash Qt) -- this is only ever reached via singleShot(0)."""
        play = self._play
        if play is None:
            return
        self._watch_building = True
        try:
            self._watch_table.setRowCount(0)
            for wt in play.watches.watches:
                self._watch_add_row(wt)
        finally:
            self._watch_building = False

    def _refresh_watch(self) -> None:
        play = self._play
        if play is None or self._watch_building:
            return
        stem = play._rom_path.stem if play._rom_path else None  # noqa: SLF001
        if stem != self._watch_rom:                  # ROM changed -> rebuild, but deferred
            self._watch_rom = stem
            QTimer.singleShot(0, self._rebuild_watch_table)
            return                                   # live values fill in next tick
        m = self._m                                  # structure stable -> only touch Live
        self._watch_building = True
        try:
            for r in range(self._watch_table.rowCount()):
                live = self._watch_table.item(r, 7)
                if live is None:
                    continue
                wt = self._row_to_watch(r)
                try:
                    live.setText(wt.format(wt.read_raw(m)) if (wt and m is not None) else "")
                except Exception:
                    live.setText("??")
        finally:
            self._watch_building = False

    # ---- Breakpoints tab
    def _breaks_tab(self) -> QWidget:
        w = QWidget(); lay = QVBoxLayout(w)
        lay.addWidget(QLabel(
            "Pause when PC reaches an address — or a symbol NAME, if a .map is loaded.\n"
            "Condition (optional), C-like: registers (a, wa, xhl, pc, sp), flags "
            "(fz fc fs fh fv fn), memory ([$4812] 1 byte, {$4a00} 2, [addr,4] 4), "
            "symbols, and && || ! ( ) + - * & | << >>.\n"
            "e.g.  a == $44 && fz     ·     [$4812] == 0 && pc < $202000     ·     "
            "{_score} > 1000\n"
            "Old 'ADDR.size OP VALUE' conditions still mean exactly what they did."))
        t = QTableWidget(0, 4)
        t.setHorizontalHeaderLabels(["PC", "Symbol", "Condition", "On"])
        t.verticalHeader().setVisible(False)
        t.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        hh = t.horizontalHeader()
        hh.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        hh.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        hh.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        hh.setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        t.itemChanged.connect(self._on_break_item)
        self._break_table = t
        lay.addWidget(t, 1)
        # A condition that does not compile used to be accepted in silence and then
        # fire on every hit -- indistinguishable from a guard that is simply true.
        # Now it is checked as you type and named.
        self._break_err = QLabel(""); self._break_err.setObjectName("hint")
        self._break_err.setWordWrap(True)
        self._break_err.setStyleSheet(f"color:{PALETTE.warning};")
        lay.addWidget(self._break_err)
        bar = QHBoxLayout()
        add = QPushButton("＋ Add"); add.setObjectName("ghost"); add.clicked.connect(self._break_add)
        rem = QPushButton("－ Remove"); rem.setObjectName("ghost"); rem.clicked.connect(self._break_remove)
        bar.addWidget(add); bar.addWidget(rem); bar.addStretch()
        lay.addLayout(bar)
        return w

    def _validate_conditions(self) -> None:
        """Compile every guard against the current symbols and report the bad ones."""
        play = self._play
        if play is None:
            return
        bad = []
        for bp in play.breaks.items:
            if not bp.cond:
                continue
            err = bp.compile(self._symbols)
            if err:
                bad.append(f"{bp.pc:06X}: {bp.cond} — {err}")
        self._break_err.setText(
            "⚠ these conditions do not compile, so their breakpoints fire every time:\n"
            + "\n".join(bad) if bad else "")
        self._break_err.setVisible(bool(bad))

    def _break_add_row(self, bp: ExecBreak | None = None) -> None:
        t = self._break_table
        r = t.rowCount(); t.insertRow(r)
        t.setItem(r, 0, QTableWidgetItem(f"{bp.pc:06X}" if bp else ""))
        # Read-only: it is derived from the address, not something to type into.
        sym = QTableWidgetItem(self._sym_text(bp.pc) if bp else "")
        sym.setFlags(Qt.ItemFlag.ItemIsEnabled)
        t.setItem(r, 1, sym)
        t.setItem(r, 2, QTableWidgetItem(bp.cond if bp else ""))
        on = QTableWidgetItem()
        on.setFlags(Qt.ItemFlag.ItemIsUserCheckable | Qt.ItemFlag.ItemIsEnabled)
        on.setCheckState(Qt.CheckState.Checked if (bp is None or bp.enabled)
                         else Qt.CheckState.Unchecked)
        on.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
        t.setItem(r, 3, on)

    def _break_add(self) -> None:
        self._breaks_building = True
        self._break_add_row()
        self._breaks_building = False

    def _break_remove(self) -> None:
        r = self._break_table.currentRow()
        if r >= 0:
            self._break_table.removeRow(r)
            self._commit_breaks()

    def _on_break_item(self, item) -> None:
        if self._breaks_building:
            return
        self._commit_breaks()
        # Editing the PC column may have been a symbol name; redraw so it shows as the
        # resolved address plus its symbol. Only that column, or typing a condition
        # would yank the cursor out of the cell on every keystroke.
        if item is not None and item.column() == 0:
            QTimer.singleShot(0, self._rebuild_break_table)

    def _row_to_break(self, r: int) -> ExecBreak | None:
        t = self._break_table
        cell = t.item(r, 0)
        pctxt = cell.text().strip() if cell else ""
        if not pctxt:
            return None
        # A symbol name is accepted here too, and normalised back to its address so
        # what is saved stays a plain PC (the map may change between builds).
        pc = self._resolve_addr(pctxt)
        if pc is None:
            return None
        cond = t.item(r, 2).text().strip() if t.item(r, 2) else ""
        on = t.item(r, 3)
        enabled = on is None or on.checkState() == Qt.CheckState.Checked
        return ExecBreak(pc, cond, enabled)

    def _commit_breaks(self) -> None:
        if self._play is None or self._breaks_building:
            return
        items = [b for r in range(self._break_table.rowCount())
                 if (b := self._row_to_break(r)) is not None]
        self._play.breaks.items = items
        self._play._save_breaks()  # noqa: SLF001
        self._validate_conditions()

    def _rebuild_break_table(self) -> None:
        play = self._play
        if play is None:
            return
        self._breaks_building = True
        try:
            self._break_table.setRowCount(0)
            for bp in play.breaks.items:
                self._break_add_row(bp)
        finally:
            self._breaks_building = False

    def _refresh_breaks(self) -> None:
        play = self._play
        if play is None or self._breaks_building:
            return
        stem = play._rom_path.stem if play._rom_path else None  # noqa: SLF001
        if stem != self._breaks_rom:
            self._breaks_rom = stem
            QTimer.singleShot(0, self._rebuild_break_table)

    # ---- RAM Search tab
    def _ramsearch_tab(self) -> QWidget:
        w = QWidget(); lay = QVBoxLayout(w)
        lay.addWidget(QLabel(
            "Find where a value lives: New search, let the game change it, then filter. "
            "Double-click a hit (or Add) to name & watch it."))
        r1 = QHBoxLayout()
        self._rs_start = QLineEdit("004000"); self._rs_start.setFixedWidth(74)
        self._rs_end = QLineEdit("00C000"); self._rs_end.setFixedWidth(74)
        for e in (self._rs_start, self._rs_end):
            e.setFont(QFont(_MONO, 10))
        self._rs_size = QComboBox()
        for lab, d in (("1", 1), ("2", 2), ("4", 4)):
            self._rs_size.addItem(lab, d)
        self._rs_signed = QCheckBox("signed")
        # A 16-bit value at an odd address is invisible to an aligned scan, and a
        # hand-written struct puts values wherever it likes -- so this is a switch,
        # not a policy. Aligned stays the default: it is 2-4x fewer candidates.
        self._rs_unaligned = QCheckBox("unaligned")
        self._rs_unaligned.setToolTip(
            "Scan every byte offset instead of stepping by the value size.\n"
            "Slower and noisier, but it is the only way to find a 16/32-bit value\n"
            "that does not sit on a multiple of its size.")
        nb = QPushButton("New search"); nb.setObjectName("ghost"); nb.clicked.connect(self._rs_new)
        r1.addWidget(QLabel("Range")); r1.addWidget(self._rs_start)
        r1.addWidget(QLabel("‥")); r1.addWidget(self._rs_end)
        r1.addWidget(QLabel("Size")); r1.addWidget(self._rs_size)
        r1.addWidget(self._rs_signed); r1.addWidget(self._rs_unaligned)
        r1.addWidget(nb); r1.addStretch()
        lay.addLayout(r1)

        # -- filters that take the value box
        r2 = QHBoxLayout()
        self._rs_value = QLineEdit(); self._rs_value.setFixedWidth(84)
        self._rs_value.setPlaceholderText("value"); self._rs_value.setFont(QFont(_MONO, 10))
        r2.addWidget(self._rs_value)
        for lab, op, tip in (("=", "=", "equal to the value"),
                             ("≠", "!=", "not equal to the value"),
                             (">", ">", "greater than the value"),
                             ("<", "<", "less than the value"),
                             ("≥", ">=", "greater than or equal to the value"),
                             ("≤", "<=", "less than or equal to the value")):
            b = QPushButton(lab); b.setObjectName("ghost"); b.setFixedWidth(32)
            b.setToolTip(tip)
            b.clicked.connect(lambda _c, o=op: self._rs_filter(o, True)); r2.addWidget(b)
        r2.addSpacing(10)
        # Deltas: the operand is an AMOUNT, not a value. "a hit always costs 3 HP"
        # is a far sharper filter than "it decreased".
        for lab, op, tip in (("+N", "increased_by", "went up by exactly the value"),
                             ("−N", "decreased_by", "went down by exactly the value"),
                             ("±N", "changed_by", "moved by exactly the value, either way")):
            b = QPushButton(lab); b.setObjectName("ghost"); b.setFixedWidth(38)
            b.setToolTip(tip)
            b.clicked.connect(lambda _c, o=op: self._rs_filter(o, True)); r2.addWidget(b)
        r2.addStretch()
        lay.addLayout(r2)

        # -- filters that compare to the previous pass, plus undo
        r3 = QHBoxLayout()
        for lab, op, tip in (("changed", "changed", "different from the last pass"),
                             ("=prev", "unchanged", "same as the last pass"),
                             ("▲", "increased", "higher than the last pass"),
                             ("▼", "decreased", "lower than the last pass")):
            b = QPushButton(lab); b.setObjectName("ghost"); b.setToolTip(tip)
            b.clicked.connect(lambda _c, o=op: self._rs_filter(o, False)); r3.addWidget(b)
        r3.addSpacing(10)
        # The change counter, and the filter that uses it. This is how you find a
        # coordinate: hold right for N frames, then ask who changed exactly N times.
        self._rs_track = QCheckBox("count changes")
        self._rs_track.setToolTip(
            "Count, per address, how many times it changes -- sampled once per\n"
            "emulated frame while this is ticked. Then 'changes =' finds the address\n"
            "that moved exactly as often as the thing you were doing.")
        self._rs_track.toggled.connect(self._rs_set_tracking)
        r3.addWidget(self._rs_track)
        b = QPushButton("changes ="); b.setObjectName("ghost")
        b.setToolTip("keep addresses whose change count equals the value box")
        b.clicked.connect(lambda: self._rs_filter("changes", True)); r3.addWidget(b)
        b = QPushButton("↺ counts"); b.setObjectName("ghost")
        b.setToolTip("reset every change count to zero")
        b.clicked.connect(self._rs_clear_counts); r3.addWidget(b)
        r3.addSpacing(10)
        self._rs_undo = QPushButton("↶ Undo"); self._rs_undo.setObjectName("ghost")
        self._rs_undo.setToolTip("take back the last filter")
        self._rs_undo.clicked.connect(self._rs_do_undo); self._rs_undo.setEnabled(False)
        r3.addWidget(self._rs_undo)
        self._rs_drop = QPushButton("✕ Eliminate"); self._rs_drop.setObjectName("ghost")
        self._rs_drop.setToolTip("drop the selected rows by hand")
        self._rs_drop.clicked.connect(self._rs_eliminate); r3.addWidget(self._rs_drop)
        r3.addStretch()
        lay.addLayout(r3)

        self._rs_count = QLabel("no search"); self._rs_count.setObjectName("hint")
        lay.addWidget(self._rs_count)
        t = QTableWidget(0, 4)
        t.setHorizontalHeaderLabels(["Address", "Value", "Previous", "Changes"])
        t.verticalHeader().setVisible(False)
        t.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        t.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        t.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        for c in (1, 2, 3):
            t.horizontalHeader().setSectionResizeMode(c, QHeaderView.ResizeMode.ResizeToContents)
        t.cellDoubleClicked.connect(lambda *_: self._rs_add_to_watch())
        self._rs_list = t
        lay.addWidget(t, 1)
        add = QPushButton("＋ Add selected to Watch"); add.setObjectName("ghost")
        add.clicked.connect(self._rs_add_to_watch)
        lay.addWidget(add)
        return w

    def _rs_new(self) -> None:
        m = self._m
        if m is None:
            return
        try:
            lo = int(self._rs_start.text(), 16); hi = int(self._rs_end.text(), 16)
        except ValueError:
            return
        n = self._ram.new_search(m, lo, hi, self._rs_size.currentData(),
                                 self._rs_signed.isChecked(),
                                 aligned=not self._rs_unaligned.isChecked())
        self._rs_count.setText(f"{n} candidates")
        self._rs_update_list()

    def _rs_filter(self, op: str, needs_value: bool) -> None:
        m = self._m
        if m is None or not self._ram.started:
            return
        operand = None
        if needs_value:
            try:
                operand = int(self._rs_value.text(), 0)
            except ValueError:
                self._rs_value.setStyleSheet(f"color:{PALETTE.error}"); return
            self._rs_value.setStyleSheet("")
        n = self._ram.refine(m, op, operand)
        self._rs_count.setText(f"{n} candidates")
        self._rs_update_list()

    def _rs_track_tick(self) -> None:
        """Subscribed to the player's per-frame hook while 'count changes' is on."""
        m = self._m
        if m is not None:
            self._ram.track_changes(m)

    def _rs_set_tracking(self, on: bool) -> None:
        """Subscribe/unsubscribe the change counter from the emulation loop. Counting
        has to happen per FRAME (that is what makes 'changed 6 times' mean 'moved for
        6 frames'), not at the debug window's 8 Hz refresh."""
        play = self._play
        if play is None or not hasattr(play, "frame_hooks"):
            return
        hooks = play.frame_hooks
        if on and self._rs_track_tick not in hooks:
            hooks.append(self._rs_track_tick)
        elif not on and self._rs_track_tick in hooks:
            hooks.remove(self._rs_track_tick)

    def _rs_do_undo(self) -> None:
        n = self._ram.undo()
        self._rs_count.setText(f"{n} candidates (undone)")
        self._rs_update_list()

    def _rs_clear_counts(self) -> None:
        self._ram.clear_changes()
        self._rs_update_list()

    def _rs_eliminate(self) -> None:
        rows = sorted({i.row() for i in self._rs_list.selectedIndexes()})
        addrs = []
        for r in rows:
            cell = self._rs_list.item(r, 0)
            if cell is not None:
                try:
                    addrs.append(int(cell.text(), 16))
                except ValueError:
                    pass
        if addrs:
            n = self._ram.eliminate(addrs)
            self._rs_count.setText(f"{n} candidates")
            self._rs_update_list()

    def _rs_update_list(self) -> None:
        m = self._m
        res = self._ram.results(m) if m is not None else []
        t = self._rs_list
        t.setRowCount(0)
        for addr, val, prev, changes in res:
            r = t.rowCount(); t.insertRow(r)
            a = QTableWidgetItem(f"{addr:06X}"); a.setFont(QFont(_MONO, 10))
            cells = [a]
            for text in (val, prev, str(changes)):
                it = QTableWidgetItem(text); it.setFont(QFont(_MONO, 10))
                it.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                cells.append(it)
            for c, it in enumerate(cells):
                t.setItem(r, c, it)
        total = self._ram.count()
        self._rs_undo.setEnabled(self._ram.can_undo())
        if total > len(res):
            self._rs_count.setText(f"{total} candidates (showing {len(res)})")

    def _rs_add_to_watch(self) -> None:
        if self._play is None:
            return
        rows = sorted({i.row() for i in self._rs_list.selectedIndexes()})
        if not rows:
            return
        size = self._rs_size.currentData()
        fmt = "s" if self._rs_signed.isChecked() else "hex"
        for r in rows:
            cell = self._rs_list.item(r, 0)
            if cell is None:
                continue
            addr = int(cell.text(), 16)
            self._play.watches.watches.append(Watch(f"ram_{addr:06X}", addr, size, fmt))
        self._play._save_watches()  # noqa: SLF001
        self._watch_rom = None       # force the Watch tab to repopulate from the list
        for i in range(self._tabs.count()):
            if self._tabs.tabText(i) == "Watch":
                self._tabs.setCurrentIndex(i); break

    def _refresh_ramsearch(self) -> None:
        m = self._m
        if m is None or not self._ram.started:
            return
        # addr -> (live value, change count). No structural change: the rows stay put
        # so a selection survives the refresh.
        res = {a: (v, c) for a, v, _p, c in self._ram.results(m)}
        t = self._rs_list
        for r in range(t.rowCount()):
            a = t.item(r, 0); v = t.item(r, 1); ch = t.item(r, 3)
            if a is None or v is None:
                continue
            try:
                addr = int(a.text(), 16)
            except ValueError:
                continue
            if addr in res:
                live, changes = res[addr]
                v.setText(live)
                if ch is not None:
                    ch.setText(str(changes))

    # ---- Audio tab
    _APU_CHANS = ("Square 1", "Square 2", "Square 3", "Noise", "DAC")

    def _audio_tab(self) -> QWidget:
        w = QWidget(); lay = QVBoxLayout(w)
        mrow = QHBoxLayout(); mrow.addWidget(QLabel("Mute / solo:"))
        self._mute_boxes = []
        for name in self._APU_CHANS:
            cb = QCheckBox(name); cb.setChecked(True)
            cb.toggled.connect(self._apply_mute)
            self._mute_boxes.append(cb); mrow.addWidget(cb)
        mrow.addStretch()
        lay.addLayout(mrow)

        t = QTableWidget(4, 5)
        t.setHorizontalHeaderLabels(["Channel", "Freq", "Note", "Vol L", "Vol R"])
        t.verticalHeader().setVisible(False)
        t.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        for c in range(5):
            t.horizontalHeader().setSectionResizeMode(c, QHeaderView.ResizeMode.Stretch)
        t.setFixedHeight(140)
        self._apu_table = t
        lay.addWidget(t)

        self._scope = QLabel(); self._scope.setFixedHeight(84)
        self._scope.setStyleSheet(f"background:{PALETTE.bg_scope};")
        lay.addWidget(self._scope)

        self._z80_lbl = QLabel("—"); self._z80_lbl.setObjectName("hint")
        self._z80_lbl.setFont(QFont(_MONO, 9))
        lay.addWidget(self._z80_lbl)

        vrow = QHBoxLayout()
        self._vgm_btn = QPushButton("⏺ Record"); self._vgm_btn.setObjectName("ghost")
        self._vgm_btn.setToolTip("Capture the music (for VGM and MIDI export)")
        self._vgm_btn.setCheckable(True); self._vgm_btn.toggled.connect(self._toggle_vgm)
        self._vgm_save = QPushButton("💾 VGM…"); self._vgm_save.setObjectName("ghost")
        self._vgm_save.clicked.connect(self._save_vgm)
        self._song_save = QPushButton("💾 Song (.ngps)…"); self._song_save.setObjectName("ghost")
        self._song_save.setToolTip("Save as a sound-creator song (.ngps) to open in the tracker")
        self._song_save.clicked.connect(self._save_song)
        self._vgm_lbl = QLabel(""); self._vgm_lbl.setObjectName("hint")
        vrow.addWidget(self._vgm_btn); vrow.addWidget(self._vgm_save)
        vrow.addWidget(self._song_save); vrow.addWidget(self._vgm_lbl); vrow.addStretch()
        lay.addLayout(vrow)

        self._apu_log = QPlainTextEdit(); self._apu_log.setReadOnly(True)
        self._apu_log.setFont(QFont(_MONO, 9))
        lay.addWidget(self._apu_log, 1)
        return w

    def _mute_mask(self) -> int:
        return sum((1 << i) for i, cb in enumerate(self._mute_boxes) if cb.isChecked())

    def _apply_mute(self) -> None:
        if self._m is not None:
            try:
                self._m.set_apu_channel_mask(self._mute_mask())
            except Exception:
                pass

    def _toggle_vgm(self, on: bool) -> None:
        if self._play is None or self._m is None:
            self._vgm_btn.blockSignals(True); self._vgm_btn.setChecked(False)
            self._vgm_btn.blockSignals(False)
            return
        if on:
            vrec = VgmRecorder(); vrec.begin(self._m.apu_write_count())
            srec = NgpsRecorder(); srec.begin()
            self._vgm_rec = vrec; self._song_rec = srec
            self._play._vgm = vrec                     # noqa: SLF001  (play loop feeds these)
            self._play._song = srec                    # noqa: SLF001
            self._vgm_btn.setText("⏹ Stop")
        else:
            self._play._vgm = None                     # noqa: SLF001  (freeze the buffers)
            self._play._song = None                    # noqa: SLF001
            self._vgm_btn.setText("⏺ Record")

    def _save_vgm(self) -> None:
        rec = self._vgm_rec
        if rec is None or rec.empty():
            self._status.setText("nothing recorded"); return
        path, _ = QFileDialog.getSaveFileName(self, "Save VGM", "capture.vgm", "VGM (*.vgm)")
        if path:
            Path(path).write_bytes(rec.build())
            self._status.setText(f"saved {Path(path).name}")

    def _save_song(self) -> None:
        rec = self._song_rec
        if rec is None or rec.empty():
            self._status.setText("nothing recorded"); return
        path, _ = QFileDialog.getSaveFileName(self, "Save song", "capture.ngps",
                                              "NGPC song (*.ngps)")
        if path:
            Path(path).write_bytes(rec.build())
            self._status.setText(f"saved {Path(path).name}")

    def _scope_pixmap(self, audio: bytes):
        w, h = 480, 80
        arr = np.zeros((h, w, 3), np.uint8)
        arr[h // 2, :] = (40, 40, 40)                  # centre line
        if audio and len(audio) >= 4:
            s = np.frombuffer(audio, np.int16)
            left = s[0::2].astype(np.float32)
            if len(left) >= 2:
                idx = np.linspace(0, len(left) - 1, w).astype(int)
                pts = left[idx]
                peak = max(1.0, float(np.abs(pts).max()))
                ys = (h // 2 - (pts / peak) * (h // 2 - 2)).astype(int).clip(0, h - 1)
                arr[ys, np.arange(w)] = (120, 220, 120)
        return _pixmap(arr, 1)

    def _refresh_audio(self) -> None:
        self._apply_mute()                             # keep the mask across game resets
        m = self._m
        t = self._apu_table
        if m is None:
            for r in range(4):
                for c in range(5):
                    t.setItem(r, c, QTableWidgetItem(""))
            return
        st = m.apu_state()
        def put(r, vals):
            for c, v in enumerate(vals):
                it = QTableWidgetItem(str(v))
                if c:
                    it.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                t.setItem(r, c, it)
        for i in range(3):
            per = st.square_period[i]
            freq = (96000.0 / per) if per > 0 else 0.0
            on = per > 0 and (st.square_vol_left[i] or st.square_vol_right[i])
            put(i, [self._APU_CHANS[i], f"{freq:.0f} Hz" if on else "—",
                    _freq_to_note(freq) if on else "—",
                    st.square_vol_left[i], st.square_vol_right[i]])
        nsel = st.noise_period_select
        nmode = "tone3" if nsel == 3 else f"÷{[512, 1024, 2048][nsel]}" if nsel < 3 else "—"
        put(3, ["Noise", nmode, "white" if st.noise_tap else "—",
                st.noise_vol_left, st.noise_vol_right])

        self._scope.setPixmap(self._scope_pixmap(getattr(self._play, "_last_audio", b"")))

        z = m.z80()
        self._z80_lbl.setText(
            f"Z80 sound CPU  pc={z.pc:04X} sp={z.sp:04X}  {'RUN' if z.running else 'halt'}"
            f"   executed={z.executed}   chip-writes={m.apu_write_count()}")

        ws = m.apu_writes()
        lines = []
        for a in ws[-26:]:
            door = ("L" if a.address & 1 else "R") if a.kind == 1 else "port"
            lines.append(f"{a.cycle:>13}  {door:>4}  {a.address:04X} = {a.value:02X}")
        self._apu_log.setPlainText("\n".join(lines))
        if self._vgm_rec is not None:
            state = "● rec" if self._play and self._play._vgm is not None else "stopped"  # noqa: SLF001
            self._vgm_lbl.setText(f"{len(self._vgm_rec.events)} writes ({state})")

    # ---- Palette tab
    # Which consumer each palette block belongs to. Unlike character RAM, palettes ARE
    # split by hardware -- sprites and the two planes each own their own 16 sub-palettes.
    PAL_BLOCKS = (("Sprites", 0x008200, 16, USE_SPRITE),
                  ("Plane 1 (SCR1)", 0x008280, 16, USE_SCR1),
                  ("Plane 2 (SCR2)", 0x008300, 16, USE_SCR2),
                  ("Backdrop", 0x0083E0, 2, 0),
                  ("Window", 0x0083F0, 2, 0))

    def _palette_tab(self) -> QWidget:
        w = QWidget(); lay = QVBoxLayout(w)
        lay.addWidget(QLabel("Colour palettes (each row = a 4-colour sub-palette)"))

        # Same per-consumer boxes as the Tiles tab. The blocks used to be stacked into one
        # unlabelled image, so you could not tell whose palette a row was.
        self._pal_show = {}
        who = QHBoxLayout()
        who.addWidget(QLabel("Show:"))
        for name, _base, _rows, flag in self.PAL_BLOCKS:
            cb = QCheckBox(name)
            cb.setChecked(True)
            cb.stateChanged.connect(self.refresh)
            cb.setStyleSheet(f"color: rgb{USAGE_COLOURS[flag]};")
            who.addWidget(cb)
            self._pal_show[name] = cb
        who.addStretch()
        lay.addLayout(who)

        inner = QWidget(); self._pal_rows = QVBoxLayout(inner)
        self._pal_rows.setAlignment(Qt.AlignmentFlag.AlignTop)
        self._pal_widgets = {}
        for name, _base, _rows, flag in self.PAL_BLOCKS:
            head = QLabel(name)
            head.setStyleSheet(f"color: rgb{USAGE_COLOURS[flag]}; font-weight: bold;")
            img = QLabel(); img.setAlignment(Qt.AlignmentFlag.AlignLeft)
            self._pal_rows.addWidget(head)
            self._pal_rows.addWidget(img)
            self._pal_widgets[name] = (head, img)
        sc = QScrollArea(); sc.setWidget(inner); sc.setWidgetResizable(True)
        lay.addWidget(sc)
        lay.addLayout(self._export_row(
            lambda: self._save_png(self._pal_arr, "palette.png"), "💾 Save PNG…"))
        return w

    def _refresh_palette(self) -> None:
        m = self._m
        if m is None:
            for head, img in self._pal_widgets.values():
                img.setText("(no game running)"); img.setPixmap(QPixmap())
                head.setVisible(True); img.setVisible(True)
            return
        cell = 16
        parts = []
        for name, base, rows, _flag in self.PAL_BLOCKS:
            head, img = self._pal_widgets[name]
            visible = self._pal_show[name].isChecked()
            head.setVisible(visible); img.setVisible(visible)
            block = np.zeros((rows * cell, 4 * cell, 3), np.uint8)
            for r in range(rows):
                for c in range(4):
                    col = _read_u16(m, base + (r * 4 + c) * 2)
                    block[r * cell:(r + 1) * cell, c * cell:(c + 1) * cell] = _rgb_from_u16(col)
            if visible:
                img.setPixmap(_pixmap(block, 2))
                parts.append(block)
        # The export keeps whatever is on screen, so a saved PNG matches what you saw.
        combined = np.concatenate(parts, axis=0) if parts else np.zeros((1, 1, 3), np.uint8)
        self._pal_arr = np.repeat(np.repeat(combined, 2, 0), 2, 1)

    # ---- Tiles tab
    def _tiles_tab(self) -> QWidget:
        w = QWidget(); lay = QVBoxLayout(w)
        bar = QHBoxLayout()
        self._tile_pal = QComboBox()
        for name in ("Grayscale", *PAL.keys()):
            self._tile_pal.addItem(name)
        self._tile_pal.currentIndexChanged.connect(self.refresh)
        self._tile_sub = QSpinBox(); self._tile_sub.setRange(0, 15)
        self._tile_sub.valueChanged.connect(self.refresh)
        bar.addWidget(QLabel("Palette")); bar.addWidget(self._tile_pal)
        bar.addWidget(QLabel("Sub")); bar.addWidget(self._tile_sub)
        bar.addStretch()
        # The Sprites tab names tiles by index (they run past 255), so say how to find one.
        bar.addWidget(QLabel(f"{CHAR_RAM_TILES} tiles · 16 per row · id = row×16 + column"))
        lay.addLayout(bar)

        # ⚡ WHO OWNS A TILE? Nobody: character RAM is one shared pool and the hardware
        # records no ownership. What we CAN show is who currently references each tile,
        # read out of the two tilemaps and OAM. Per-consumer boxes, like the audio tab's
        # per-channel mute, so a plane's tiles can be picked out of the shared sheet.
        self._tile_show = {}
        who = QHBoxLayout()
        who.addWidget(QLabel("Show:"))
        for flag, name in ((USE_SCR1, "Plane 1 (SCR1)"), (USE_SCR2, "Plane 2 (SCR2)"),
                           (USE_SPRITE, "Sprites"), (0, "Unused")):
            cb = QCheckBox(name)
            cb.setChecked(True)
            cb.stateChanged.connect(self.refresh)
            colour = USAGE_COLOURS[flag]
            cb.setStyleSheet(f"color: rgb{colour};")
            who.addWidget(cb)
            self._tile_show[flag] = cb
        shared = self._tile_shared = QLabel("■ shared")
        shared.setStyleSheet(f"color: rgb{USAGE_SHARED};")
        shared.setToolTip("Referenced by more than one consumer — often a tile range that "
                          "was loaded over another one.")
        who.addWidget(shared)
        who.addStretch()
        lay.addLayout(who)
        self._tile_label = _TileGrid(
            TILE_ATLAS_PITCH * TILE_ATLAS_SCALE, TILE_ATLAS_COLS, self._tile_info)
        self._tile_label.set_status_sink(self._tile_status)
        sc = QScrollArea(); sc.setWidget(self._tile_label); sc.setWidgetResizable(True)
        lay.addWidget(sc)

        # Hover reads a tile out; the line stays put so you can select a number from it,
        # and a click copies the whole block. Read-only + monospace, like the dumps.
        self._tile_status_line = QLineEdit(); self._tile_status_line.setReadOnly(True)
        self._tile_status_line.setFont(QFont(_MONO, 10))
        self._tile_status_line.setPlaceholderText(
            "Hover a tile for its address; click it to copy.")
        lay.addWidget(self._tile_status_line)

        lay.addLayout(self._export_row(
            lambda: self._save_png(self._tiles_arr, "tiles.png"), "💾 Save PNG…"))
        return w

    def _tile_info(self, col: int, row: int) -> "str | None":
        """The copyable block for the tile at (col, row), or None past the last one."""
        idx = row * TILE_ATLAS_COLS + col
        if idx >= self._tiles_n:
            return None
        addr = CHAR_RAM + idx * TILE_BYTES
        usage = self._tiles_usage
        u = int(usage[idx]) if usage is not None and idx < len(usage) else 0
        consumers = [n for f, n in ((USE_SCR1, "SCR1"), (USE_SCR2, "SCR2"),
                                    (USE_SPRITE, "sprites")) if u & f]
        if len(consumers) > 1:
            who = "shared (" + ", ".join(consumers) + ")"
        elif consumers:
            who = consumers[0]
        else:
            who = "unused"
        raw = self._tiles_char[idx * TILE_BYTES:(idx + 1) * TILE_BYTES]
        hexb = " ".join(f"{b:02X}" for b in raw)
        lines = [
            f"tile {idx} (0x{idx:03X})",
            f"addr 0x{addr:06X}–0x{addr + TILE_BYTES - 1:06X}",
            f"used by {who}",
            f"bytes {hexb}",
        ]
        # Past 255 a tile is only reachable as a sprite via the attribute's bit 0.
        if idx >= 256:
            lines.insert(1, f"sprite ref: code 0x{idx & 0xFF:02X} + attrib bit0")
        return "\n".join(lines)

    def _tile_status(self, text: "str | None", copy: bool) -> None:
        """Feed the status line from a hover (copy=False) or a click (copy=True)."""
        if not text:
            self._tile_status_line.clear(); return
        one = text.replace("\n", "   ")
        if copy:
            QApplication.clipboard().setText(text)
            self._tile_status_line.setText("✔ copied   " + one)
        else:
            self._tile_status_line.setText(one)

    def _tile_palette_rgb(self) -> np.ndarray:
        m = self._m
        name = self._tile_pal.currentText()
        if name == "Grayscale" or m is None:
            return np.array([[0, 0, 0], [90, 90, 90], [170, 170, 170], [255, 255, 255]],
                            np.uint8)
        base = PAL[name] + self._tile_sub.value() * 8
        return np.array([_rgb_from_u16(_read_u16(m, base + i * 2)) for i in range(4)],
                        np.uint8)

    def _refresh_tiles(self) -> None:
        m = self._m
        if m is None:
            self._tiles_n = 0
            self._tile_label.setText("(no game running)"); return
        char = m.read(CHAR_RAM, CHAR_RAM_SIZE)
        usage = tile_usage(m)
        show = {flag for flag, cb in self._tile_show.items() if cb.isChecked()}
        sheet = decode_tiles(char, self._tile_palette_rgb(), usage, show)
        # Keep what hover needs: the bytes, the per-tile usage, and how many tiles there are.
        self._tiles_char = char
        self._tiles_usage = usage
        self._tiles_n = len(char) // TILE_BYTES
        s = TILE_ATLAS_SCALE
        self._tiles_arr = np.repeat(np.repeat(sheet, s, 0), s, 1)
        self._tile_label.setPixmap(_pixmap(sheet, s))

    # ---- Sprites tab
    def _sprites_tab(self) -> QWidget:
        w = QWidget(); lay = QVBoxLayout(w)
        lay.addWidget(QLabel("OAM (64 sprites): tile, position, priority"))
        self._spr_text = QPlainTextEdit(); self._spr_text.setReadOnly(True)
        self._spr_text.setFont(QFont(_MONO, 10))
        lay.addWidget(self._spr_text)
        lay.addLayout(self._export_row(
            lambda: self._save_text(self._spr_text.toPlainText(), "sprites.txt")))
        return w

    def _refresh_sprites(self) -> None:
        m = self._m
        if m is None:
            self._spr_text.setPlainText("(no game running)"); return
        oam = m.read(OAM_BASE, 64 * 4)
        cpc = m.read(OAM_CPC, 64)
        rows = ["idx  tile  attrib  H   V   prc flip pal"]
        for i in range(64):
            code = oam[i * 4]; attrib = oam[i * 4 + 1]
            h = oam[i * 4 + 2]; vv = oam[i * 4 + 3]
            tile = ((attrib & 1) << 8) | code
            prc = (attrib >> 3) & 3
            flip = ("H" if (attrib >> 7) & 1 else "-") + ("V" if (attrib >> 6) & 1 else "-")
            pal = cpc[i] & 0x0F
            if prc == 0 and h == 0 and vv == 0:
                continue
            rows.append(f"{i:3d}  {tile:03X}   {attrib:02X}     {h:3d} {vv:3d}  {prc}   "
                        f"{flip}   {pal:X}")
        self._spr_text.setPlainText("\n".join(rows))

    # ---- Layers tab
    # The video counterpart of the Audio tab's mute/solo: the same idea, applied to the
    # composite instead of the mix. On this machine text and artwork are ALWAYS on
    # different layers -- the chip has no other way to superimpose them -- so hiding a
    # layer is how you find out which one owns what, and how you get a clean background
    # plate out of a game without touching its VRAM.
    _LAYERS = (
        (native.NativeMachine.LAYER_SCR1, "Plane 1 (SCR1)"),
        (native.NativeMachine.LAYER_SCR2, "Plane 2 (SCR2)"),
        (native.NativeMachine.LAYER_SPR_BACK, "Sprites · behind"),
        (native.NativeMachine.LAYER_SPR_MID, "Sprites · middle"),
        (native.NativeMachine.LAYER_SPR_FRONT, "Sprites · front"),
    )
    _LAYER_PREVIEW_SCALE = 3

    def _layers_tab(self) -> QWidget:
        w = QWidget(); lay = QVBoxLayout(w)
        lay.addWidget(QLabel("Show / hide layers in the rendered picture:"))

        self._layer_boxes = []
        for bit, name in self._LAYERS:
            row = QHBoxLayout()
            cb = QCheckBox(name); cb.setChecked(True)
            cb.toggled.connect(self._apply_layers)
            self._layer_boxes.append((bit, cb))
            solo = QPushButton("solo"); solo.setObjectName("ghost")
            solo.setToolTip("Show this layer only")
            solo.clicked.connect(lambda _c=False, b=bit: self._solo_layer(b))
            row.addWidget(cb); row.addWidget(solo); row.addStretch()
            lay.addLayout(row)

        btns = QHBoxLayout()
        allb = QPushButton("All on"); allb.setObjectName("ghost")
        allb.clicked.connect(lambda: self._solo_layer(None))
        btns.addWidget(allb); btns.addStretch()
        lay.addLayout(btns)

        # ⚠️ RAW pixels on purpose: no gamma, no LCD filter. This preview doubles as the
        # export source, and an extracted background plate has to be the colours the
        # palette actually holds -- a screen-emulation filter would bake a look into art
        # that is meant to be re-imported.
        self._layer_view = QLabel("(no game running)")
        self._layer_view.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lay.addWidget(self._layer_view, 1)

        self._layer_hint = QLabel(
            "Hidden layers change the picture only — no machine state, no timing. "
            "Turn them all back on before judging fidelity.")
        self._layer_hint.setObjectName("hint"); self._layer_hint.setWordWrap(True)
        lay.addWidget(self._layer_hint)

        lay.addLayout(self._export_row(
            lambda: self._save_png(self._layer_rgb(), "layer.png"), "💾 Export PNG…"))
        return w

    def _layer_mask(self) -> int:
        return sum(bit for bit, cb in self._layer_boxes if cb.isChecked())

    def _apply_layers(self) -> None:
        if self._m is not None:
            try:
                self._m.set_layer_mask(self._layer_mask())
            except Exception:
                pass

    def _solo_layer(self, bit: int | None) -> None:
        """`bit` = show that layer alone; `None` = show everything again."""
        for b, cb in self._layer_boxes:
            cb.blockSignals(True)
            cb.setChecked(bit is None or b == bit)
            cb.blockSignals(False)
        self._apply_layers()
        self.refresh()

    def _layer_rgb(self):
        """The composed picture at 1:1, RGB, as the mask currently shows it."""
        m = self._m
        if m is None:
            return None
        fb = np.asarray(m.framebuffer(), dtype=np.uint16)
        if fb.size != native.SCREEN_W * native.SCREEN_H:
            return None
        a = fb.reshape(native.SCREEN_H, native.SCREEN_W)
        rgb = np.empty((native.SCREEN_H, native.SCREEN_W, 3), dtype=np.uint8)
        rgb[..., 0] = (a & 0x0F) * 17            # R = low nibble of 0BGR
        rgb[..., 1] = ((a >> 4) & 0x0F) * 17     # G
        rgb[..., 2] = ((a >> 8) & 0x0F) * 17     # B = high nibble
        return rgb

    def _refresh_layers(self) -> None:
        self._apply_layers()          # keep the mask across game resets, like the audio one
        rgb = self._layer_rgb()
        if rgb is None:
            self._layer_view.setText("(no game running)")
            return
        self._layer_view.setPixmap(_pixmap(rgb, self._LAYER_PREVIEW_SCALE))

    # ---- Load tab (live resource gauges)
    # What a dev actually runs out of on this hardware: the 64-sprite OAM and the 512-tile
    # character RAM. Both are read straight from VRAM, so they are exact, not estimates.
    # The frame-rate gauge is the honest CPU signal: a cycle-duty % is useless here (the
    # cart bus keeps the CPU ~100% busy every frame, and games spin-poll VBlank rather than
    # HALT -- measured: 0 of 25 games ever idled), so what matters is whether the game
    # completes its work in time, which shows up as its update rate holding at 60.
    def _load_tab(self) -> QWidget:
        w = QWidget(); lay = QVBoxLayout(w)
        lay.addWidget(QLabel("Live resource use (updates as the game runs):"))

        self._g_cpu = _Gauge("health")
        self._g_spr = _Gauge("usage")
        self._g_tile = _Gauge("usage")
        for label, tip, g in (
            ("Frame rate", "Does the game finish its frame in time? 60 = keeping up. "
                           "Grey = nothing moving on screen, so it can't be told. A pure "
                           "CPU-cycle % is not meaningful on NGPC: the cart bus keeps the "
                           "CPU busy every frame, so it would read ~100% always.", self._g_cpu),
            ("Sprites", "Active entries in the 64-sprite OAM. Red = near the hardware limit.",
             self._g_spr),
            ("Char RAM", "Distinct tiles referenced, of 512 in character RAM. Red = the "
                         "tile budget is nearly full.", self._g_tile),
        ):
            lab = QLabel(label); lab.setFixedWidth(90)
            lab.setToolTip(tip); g.setToolTip(tip)
            row = QHBoxLayout(); row.addWidget(lab); row.addWidget(g, 1)
            lay.addLayout(row)

        lay.addStretch(1)
        note = QLabel(
            "Sprites and Char RAM are read straight from VRAM — exact counts, not "
            "estimates. Frame rate is inferred from the sprite table changing, so it is a "
            "hint: it reads grey on a still screen where nothing updates.")
        note.setObjectName("hint"); note.setWordWrap(True)
        lay.addWidget(note)
        return w

    def _refresh_load(self) -> None:
        m = self._m
        if m is None:
            for g in (self._g_cpu, self._g_spr, self._g_tile):
                g.set_value(0.0, "(no game running)", neutral=True)
            return
        # Sprites: the same 'is it in use' test the Sprites tab uses.
        oam = m.read(OAM_BASE, 64 * 4)
        active = sum(1 for i in range(64)
                     if not ((oam[i * 4 + 1] >> 3) & 3 == 0 and oam[i * 4 + 2] == 0
                             and oam[i * 4 + 3] == 0))
        self._g_spr.set_value(active / 64, f"Sprites   {active} / 64")
        # Char RAM: distinct tiles referenced by the two planes and OAM.
        used = int((tile_usage(m) != 0).sum())
        self._g_tile.set_value(used / CHAR_RAM_TILES,
                               f"Char RAM   {used} / {CHAR_RAM_TILES} tiles")
        # Frame rate: the honest overload signal (see the class comment).
        play = self._play
        if play is not None and hasattr(play, "perf"):
            fps = play.perf().get("game_fps", 0.0)
            if fps <= 0.5:
                self._g_cpu.set_value(0.0, "Frame rate   — (nothing moving)", neutral=True)
            else:
                self._g_cpu.set_value(min(1.0, fps / 60.0), f"Frame rate   {fps:4.0f} / 60 fps")
        else:
            self._g_cpu.set_value(0.0, "Frame rate   —", neutral=True)

    # ---- Text tab (character-table tools; ANY rom, nothing game-specific)
    # A ROM is text plus code plus art; this tab is the text half a fan-translation
    # lives in. It reads live memory through a USER-loaded .tbl (byte<->character map),
    # so it is a general tool: point it at any game with its own table. Three jobs --
    # DECODE a region into readable strings, SEARCH for a phrase by its exact bytes
    # (table), or crack an unknown encoding by letter spacing (relative, no table).
    # The heavy lifting is in `core/texttable.py`; this is only wiring.
    def _text_tab(self) -> QWidget:
        w = QWidget(); lay = QVBoxLayout(w)
        self._txt_table = None                        # loaded TextTable, or None

        top = QHBoxLayout()
        self._txt_load = QPushButton("Load table (.tbl)…"); self._txt_load.setObjectName("ghost")
        self._txt_load.clicked.connect(self._txt_pick_table)
        self._txt_tbl_lbl = QLabel("no table loaded"); self._txt_tbl_lbl.setObjectName("hint")
        top.addWidget(self._txt_load); top.addWidget(self._txt_tbl_lbl, 1)
        lay.addLayout(top)

        # -- decode a region into strings
        dec = QHBoxLayout()
        dec.addWidget(QLabel("Decode at"))
        self._txt_addr = QLineEdit("200000"); self._txt_addr.setFixedWidth(120)
        self._txt_addr.setFont(QFont(_MONO, 10))
        self._txt_addr.setToolTip("Address in hex, or a symbol name when a .map is loaded.")
        self._txt_addr.editingFinished.connect(self._txt_decode)
        dec.addWidget(self._txt_addr)
        dec.addWidget(QLabel("bytes"))
        self._txt_len = QSpinBox(); self._txt_len.setRange(16, 8192); self._txt_len.setValue(256)
        self._txt_len.valueChanged.connect(self._txt_decode)
        dec.addWidget(self._txt_len)
        go = QPushButton("Decode"); go.setObjectName("ghost"); go.clicked.connect(self._txt_decode)
        dec.addWidget(go); dec.addStretch()
        lay.addLayout(dec)

        self._txt_out = QPlainTextEdit(); self._txt_out.setReadOnly(True)
        self._txt_out.setFont(QFont(_MONO, 10))
        lay.addWidget(self._txt_out, 1)
        lay.addLayout(self._export_row(
            lambda: self._save_text(self._txt_out.toPlainText(), "text_dump.txt")))

        # -- search: by table (exact bytes) or relative (unknown encoding)
        sr = QHBoxLayout()
        sr.addWidget(QLabel("Find"))
        self._txt_find = QLineEdit(); self._txt_find.setFixedWidth(200)
        self._txt_find.returnPressed.connect(self._txt_search)
        sr.addWidget(self._txt_find)
        self._txt_mode = QComboBox(); self._txt_mode.addItems(["Table", "Relative"])
        self._txt_mode.setToolTip(
            "Table: the exact bytes the loaded table encodes the text to.\n"
            "Relative: crack an unknown encoding — match the SPACING between the "
            "letters, no table needed. Type a word you can read on screen.")
        sr.addWidget(self._txt_mode)
        sr.addWidget(QLabel("from"))
        self._txt_from = QLineEdit("200000"); self._txt_from.setFixedWidth(100)
        self._txt_from.setFont(QFont(_MONO, 10))
        sr.addWidget(self._txt_from)
        sr.addWidget(QLabel("KiB"))
        self._txt_size = QSpinBox(); self._txt_size.setRange(1, 8192); self._txt_size.setValue(2048)
        sr.addWidget(self._txt_size)
        sb = QPushButton("Search"); sb.setObjectName("ghost"); sb.clicked.connect(self._txt_search)
        sr.addWidget(sb)
        scan = QPushButton("Scan strings"); scan.setObjectName("ghost")
        scan.setToolTip("List every run of text in the region (needs a table). "
                        "Export the result for a script dump.")
        scan.clicked.connect(self._txt_scan)
        sr.addWidget(scan); sr.addStretch()
        lay.addLayout(sr)

        self._txt_hits = QPlainTextEdit(); self._txt_hits.setReadOnly(True)
        self._txt_hits.setFont(QFont(_MONO, 10))
        lay.addWidget(self._txt_hits, 1)
        lay.addLayout(self._export_row(
            lambda: self._save_text(self._txt_hits.toPlainText(), "text_search.txt")))

        # A table chosen last time comes back automatically.
        saved = self._settings.value("paths/text_table", "", type=str)
        if saved and Path(saved).is_file():
            self._txt_load_table(saved)
        return w

    @staticmethod
    def _rom_off(addr: int) -> str:
        return f" (ROM 0x{addr - CART_BASE:06X})" if addr >= CART_BASE else ""

    def _txt_pick_table(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "Character table", "", "Table (*.tbl);;All files (*)")
        if path:
            self._txt_load_table(path)

    def _txt_load_table(self, path: str) -> None:
        from core.texttable import parse_tbl
        try:
            src = Path(path).read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            self._txt_tbl_lbl.setText(f"could not read table: {exc}"); return
        self._txt_table = parse_tbl(src)
        self._settings.setValue("paths/text_table", path)
        terms = " ".join(t.hex().upper() for t in self._txt_table.terminators) or "none"
        self._txt_tbl_lbl.setText(
            f"{Path(path).name} — {len(self._txt_table)} entries · end token: {terms}")
        self._txt_decode()

    def _txt_decode(self) -> None:
        m = self._m
        if m is None:
            self._txt_out.setPlainText("(no game running)"); return
        if self._txt_table is None:
            self._txt_out.setPlainText("Load a .tbl table to decode text."); return
        addr = self._resolve_addr(self._txt_addr.text())
        if addr is None:
            self._txt_out.setPlainText("bad address"); return
        data = m.read(addr & 0xFFFFFF, self._txt_len.value())
        lines, i = [], 0
        while i < len(data):
            text, used = self._txt_table.decode(data[i:], stop_at_end=True)
            if used == 0:
                break
            a = addr + i
            lines.append(f"{a:06X}{self._rom_off(a)}  {text!r}")
            i += used
        self._txt_out.setPlainText("\n".join(lines) if lines else "(nothing)")

    def _txt_search(self) -> None:
        m = self._m
        if m is None:
            self._txt_hits.setPlainText("(no game running)"); return
        needle = self._txt_find.text()
        if not needle:
            return
        start = self._resolve_addr(self._txt_from.text())
        if start is None:
            self._txt_hits.setPlainText("bad start address"); return
        data = m.read(start & 0xFFFFFF, self._txt_size.value() * 1024)
        mode = self._txt_mode.currentText()
        if mode == "Table":
            if self._txt_table is None:
                self._txt_hits.setPlainText("Table search needs a loaded .tbl."); return
            from core.texttable import table_search
            hits = table_search(data, needle, self._txt_table)
        else:
            from core.texttable import relative_search
            hits = relative_search(data, needle)
        if not hits:
            self._txt_hits.setPlainText("no match"); return
        cap = 500
        head = f"{len(hits)} match(es)" + (f" — showing first {cap}" if len(hits) > cap else "")
        lines = [head]
        for off in hits[:cap]:
            a = start + off
            if mode == "Table" and self._txt_table is not None:
                ctx, _ = self._txt_table.decode(data[off:off + 32], stop_at_end=True)
                ctx = repr(ctx)
            else:
                ctx = " ".join(f"{b:02X}" for b in data[off:off + min(max(len(needle), 4), 16)])
            lines.append(f"{a:06X}{self._rom_off(a)}  {ctx}")
        # Relative search also HANDS YOU the encoding: from the first hit, the byte each
        # of the word's letters used -- the seed of a brand-new .tbl.
        if mode == "Relative":
            b0 = data[hits[0]]; c0 = ord(needle[0])
            seed = {c: (b0 + (ord(c) - c0)) & 0xFF for c in dict.fromkeys(needle)}
            lines.append("")
            lines.append("derived from first hit:  " +
                         "  ".join(f"{c!r}={b:02X}" for c, b in sorted(seed.items())))
        self._txt_hits.setPlainText("\n".join(lines))

    def _txt_scan(self) -> None:
        """List every run of text in the search region -- a whole-region script dump.
        Export the box to get the strings in a file, ready to translate offline."""
        m = self._m
        if m is None:
            self._txt_hits.setPlainText("(no game running)"); return
        if self._txt_table is None:
            self._txt_hits.setPlainText("Scanning for strings needs a loaded .tbl."); return
        start = self._resolve_addr(self._txt_from.text())
        if start is None:
            self._txt_hits.setPlainText("bad start address"); return
        from core.texttable import scan_strings
        data = m.read(start & 0xFFFFFF, self._txt_size.value() * 1024)
        runs = scan_strings(data, self._txt_table, min_len=4)
        if not runs:
            self._txt_hits.setPlainText("no strings found"); return
        cap = 5000
        head = f"{len(runs)} string(s)" + (f" — showing first {cap}" if len(runs) > cap else "")
        lines = [head]
        for off, _blen, text in runs[:cap]:
            a = start + off
            lines.append(f"{a:06X}{self._rom_off(a)}  {text!r}")
        self._txt_hits.setPlainText("\n".join(lines))

    def _refresh_text(self) -> None:
        # The decode view is cheap and following a live RAM buffer is useful, so keep it
        # current -- but hold the scroll position so a running game does not yank it.
        if self._m is None or self._txt_table is None:
            return
        bar = self._txt_out.verticalScrollBar(); pos = bar.value()
        self._txt_decode()
        bar.setValue(min(pos, bar.maximum()))

    # ---- Crack tab (build a .tbl from words you can read -- ANY rom)
    # Relative search finds WHERE a known word sits; reading the bytes under it turns
    # that into a real mapping, even for a non-linear encoding. Feed a few readable
    # words and this assembles a table you can save and refine.
    def _crack_tab(self) -> QWidget:
        w = QWidget(); lay = QVBoxLayout(w)
        lay.addWidget(QLabel(
            "Type words you can READ on screen (one per line). Each is located by "
            "relative search; the bytes under it become a character table."))

        self._crack_words = QPlainTextEdit(); self._crack_words.setFont(QFont(_MONO, 10))
        self._crack_words.setPlaceholderText("player\nmagic\nattack\n; a common word can be pinned:\nyes @ 21A0C0")
        self._crack_words.setMaximumHeight(110)
        self._crack_words.setToolTip(
            "One word per line. A word that matches in many places is ambiguous — pin it "
            "to a spot you found (relative search in the Text tab) with 'word @ offset'.")
        lay.addWidget(self._crack_words)

        row = QHBoxLayout()
        row.addWidget(QLabel("Search from"))
        self._crack_from = QLineEdit("200000"); self._crack_from.setFixedWidth(100)
        self._crack_from.setFont(QFont(_MONO, 10))
        row.addWidget(self._crack_from)
        row.addWidget(QLabel("KiB"))
        self._crack_size = QSpinBox(); self._crack_size.setRange(1, 8192); self._crack_size.setValue(2048)
        row.addWidget(self._crack_size)
        cb = QPushButton("Crack → table"); cb.setObjectName("ghost")
        cb.clicked.connect(self._crack_run)
        row.addWidget(cb); row.addStretch()
        lay.addLayout(row)

        lay.addWidget(QLabel("Derived table (edit freely, then save or use):"))
        self._crack_out = QPlainTextEdit(); self._crack_out.setFont(QFont(_MONO, 10))
        lay.addWidget(self._crack_out, 1)

        act = QHBoxLayout()
        save = QPushButton("Save .tbl…"); save.setObjectName("ghost")
        save.clicked.connect(self._crack_save)
        use = QPushButton("Use in Text tab"); use.setObjectName("ghost")
        use.clicked.connect(self._crack_use)
        act.addWidget(save); act.addWidget(use); act.addStretch()
        lay.addLayout(act)
        return w

    def _crack_run(self) -> None:
        m = self._m
        if m is None:
            self._crack_out.setPlainText("(no game running)"); return
        start = self._resolve_addr(self._crack_from.text())
        if start is None:
            self._crack_out.setPlainText("bad start address"); return
        # Each line is a word, or "word @ hexaddr" to pin a common one. A pinned CPU
        # address is turned into an offset into the region we are about to read.
        entries: list = []
        for line in self._crack_words.toPlainText().splitlines():
            line = line.strip()
            if not line or line.startswith(";"):
                continue
            if "@" in line:
                word, _, off_s = line.partition("@")
                word = word.strip()
                addr = self._resolve_addr(off_s.strip())
                if addr is None:
                    self._crack_out.setPlainText(f"bad offset in: {line}"); return
                entries.append((word, addr - start if addr >= start else addr))
            else:
                entries.append(line)
        if not entries:
            self._crack_out.setPlainText("Enter at least one word above."); return
        from core.texttable import crack_from_words, build_tbl
        data = m.read(start & 0xFFFFFF, self._crack_size.value() * 1024)
        mapping, report = crack_from_words(data, entries)
        tbl = build_tbl(mapping)
        note = "\n".join(f"; {r}" for r in report)
        self._crack_out.setPlainText((note + "\n" if note else "") + tbl)

    def _crack_save(self) -> None:
        path, _ = QFileDialog.getSaveFileName(self, "Save table", "table.tbl", "Table (*.tbl)")
        if path:
            try:
                Path(path).write_text(self._crack_out.toPlainText(), encoding="utf-8")
            except OSError as exc:
                QMessageBox.warning(self, "Save table", str(exc))

    def _crack_use(self) -> None:
        """Make the edited table the Text tab's active one, without leaving the app."""
        from core.texttable import parse_tbl
        self._txt_table = parse_tbl(self._crack_out.toPlainText())
        terms = " ".join(t.hex().upper() for t in self._txt_table.terminators) or "none"
        self._txt_tbl_lbl.setText(f"(cracked) — {len(self._txt_table)} entries · end token: {terms}")

    def _refresh_crack(self) -> None:
        pass                    # on-demand only; never clobber what the user typed

    # ---- Pointers tab (find references / pointer tables -- ANY rom)
    def _pointers_tab(self) -> QWidget:
        w = QWidget(); lay = QVBoxLayout(w)
        lay.addWidget(QLabel(
            "Find what points at an address (to repoint a moved string), or locate the "
            "pointer tables themselves."))

        cfgrow = QHBoxLayout()
        cfgrow.addWidget(QLabel("Pointer"))
        self._ptr_width = QComboBox(); self._ptr_width.addItems(["32-bit LE", "24-bit LE", "16-bit LE"])
        cfgrow.addWidget(self._ptr_width)
        cfgrow.addWidget(QLabel("base +"))
        self._ptr_base = QLineEdit("000000"); self._ptr_base.setFixedWidth(90)
        self._ptr_base.setFont(QFont(_MONO, 10))
        self._ptr_base.setToolTip("Added to the stored value to get a CPU address. 0 for "
                                  "absolute pointers; a bank base for 16-bit offsets.")
        cfgrow.addWidget(self._ptr_base)
        cfgrow.addWidget(QLabel("scan from"))
        self._ptr_from = QLineEdit("200000"); self._ptr_from.setFixedWidth(90)
        self._ptr_from.setFont(QFont(_MONO, 10))
        cfgrow.addWidget(self._ptr_from)
        cfgrow.addWidget(QLabel("KiB"))
        self._ptr_size = QSpinBox(); self._ptr_size.setRange(1, 8192); self._ptr_size.setValue(2048)
        cfgrow.addWidget(self._ptr_size)
        cfgrow.addStretch()
        lay.addLayout(cfgrow)

        findrow = QHBoxLayout()
        findrow.addWidget(QLabel("Find pointers to"))
        self._ptr_target = QLineEdit(); self._ptr_target.setFixedWidth(120)
        self._ptr_target.setFont(QFont(_MONO, 10))
        self._ptr_target.setPlaceholderText("address or symbol")
        self._ptr_target.returnPressed.connect(self._ptr_find)
        findrow.addWidget(self._ptr_target)
        findrow.addWidget(QLabel("± "))
        self._ptr_tol = QSpinBox(); self._ptr_tol.setRange(0, 256); self._ptr_tol.setValue(0)
        self._ptr_tol.setToolTip("Tolerance: also catch a pointer this many bytes into the target.")
        findrow.addWidget(self._ptr_tol)
        fb = QPushButton("Find"); fb.setObjectName("ghost"); fb.clicked.connect(self._ptr_find)
        findrow.addWidget(fb)
        findrow.addSpacing(20)
        findrow.addWidget(QLabel("min run"))
        self._ptr_run = QSpinBox(); self._ptr_run.setRange(2, 4096); self._ptr_run.setValue(8)
        findrow.addWidget(self._ptr_run)
        tb = QPushButton("Scan tables"); tb.setObjectName("ghost"); tb.clicked.connect(self._ptr_scan)
        findrow.addWidget(tb); findrow.addStretch()
        lay.addLayout(findrow)

        self._ptr_out = QPlainTextEdit(); self._ptr_out.setReadOnly(True)
        self._ptr_out.setFont(QFont(_MONO, 10))
        lay.addWidget(self._ptr_out, 1)
        lay.addLayout(self._export_row(
            lambda: self._save_text(self._ptr_out.toPlainText(), "pointers.txt")))
        return w

    def _ptr_params(self) -> "tuple | None":
        width = {0: 4, 1: 3, 2: 2}[self._ptr_width.currentIndex()]
        try:
            base = int(self._ptr_base.text(), 16)
        except ValueError:
            self._ptr_out.setPlainText("bad base"); return None
        start = self._resolve_addr(self._ptr_from.text())
        if start is None:
            self._ptr_out.setPlainText("bad scan-from address"); return None
        return width, base, start

    def _ptr_find(self) -> None:
        m = self._m
        if m is None:
            self._ptr_out.setPlainText("(no game running)"); return
        p = self._ptr_params()
        if p is None:
            return
        width, base, start = p
        target = self._resolve_addr(self._ptr_target.text())
        if target is None:
            self._ptr_out.setPlainText("bad target address"); return
        from core.pointers import find_pointers_to
        data = m.read(start & 0xFFFFFF, self._ptr_size.value() * 1024)
        hits = find_pointers_to(data, target, base=base, width=width,
                                tolerance=self._ptr_tol.value())
        if not hits:
            self._ptr_out.setPlainText("no pointer found"); return
        cap = 2000
        lines = [f"{len(hits)} pointer(s) to {target:06X}"
                 + (f" — showing first {cap}" if len(hits) > cap else "")]
        for off in hits[:cap]:
            a = start + off
            lines.append(f"{a:06X}{self._rom_off(a)}")
        self._ptr_out.setPlainText("\n".join(lines))

    def _ptr_scan(self) -> None:
        m = self._m
        if m is None:
            self._ptr_out.setPlainText("(no game running)"); return
        p = self._ptr_params()
        if p is None:
            return
        width, base, start = p
        from core.pointers import scan_pointer_tables
        size = self._ptr_size.value() * 1024
        data = m.read(start & 0xFFFFFF, size)
        # Plausible target range: the region we are scanning, in CPU space.
        lo = (start & 0xFFFFFF)
        tables = scan_pointer_tables(data, base=base, width=width,
                                     lo=lo, hi=lo + size, min_run=self._ptr_run.value())
        if not tables:
            self._ptr_out.setPlainText("no pointer table found"); return
        cap = 2000
        lines = [f"{len(tables)} table(s)"
                 + (f" — showing first {cap}" if len(tables) > cap else ""),
                 "offset            count  first→"]
        for off, count, first in tables[:cap]:
            a = start + off
            lines.append(f"{a:06X}{self._rom_off(a)}  {count:5d}  {first:06X}")
        self._ptr_out.setPlainText("\n".join(lines))

    def _refresh_pointers(self) -> None:
        pass                    # on-demand only

    # ---- Compare tab (byte-diff against a second ROM -- ANY pair)
    # A released patch is an oracle: what it changed is the text. Diff the running cart
    # against a second .ngc and, with a table loaded, read both sides of each change.
    def _compare_tab(self) -> QWidget:
        w = QWidget(); lay = QVBoxLayout(w)
        self._cmp_path = None

        top = QHBoxLayout()
        load = QPushButton("Load ROM B…"); load.setObjectName("ghost")
        load.clicked.connect(self._cmp_pick)
        self._cmp_lbl = QLabel("no second ROM loaded"); self._cmp_lbl.setObjectName("hint")
        top.addWidget(load); top.addWidget(self._cmp_lbl, 1)
        lay.addLayout(top)

        row = QHBoxLayout()
        row.addWidget(QLabel("Compare from"))
        self._cmp_from = QLineEdit("200000"); self._cmp_from.setFixedWidth(100)
        self._cmp_from.setFont(QFont(_MONO, 10))
        row.addWidget(self._cmp_from)
        row.addWidget(QLabel("KiB"))
        self._cmp_size = QSpinBox(); self._cmp_size.setRange(1, 8192); self._cmp_size.setValue(2048)
        row.addWidget(self._cmp_size)
        cb = QPushButton("Compare"); cb.setObjectName("ghost"); cb.clicked.connect(self._cmp_run)
        row.addWidget(cb); row.addStretch()
        lay.addLayout(row)

        self._cmp_out = QPlainTextEdit(); self._cmp_out.setReadOnly(True)
        self._cmp_out.setFont(QFont(_MONO, 10))
        lay.addWidget(self._cmp_out, 1)
        lay.addLayout(self._export_row(
            lambda: self._save_text(self._cmp_out.toPlainText(), "rom_diff.txt")))
        return w

    def _cmp_pick(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Second ROM", "", "NGPC ROM (*.ngc *.ngp);;All (*)")
        if path:
            self._cmp_path = path
            self._cmp_lbl.setText(Path(path).name)

    def _cmp_run(self) -> None:
        m = self._m
        if m is None:
            self._cmp_out.setPlainText("(no game running)"); return
        if not self._cmp_path:
            self._cmp_out.setPlainText("Load a second ROM to compare against."); return
        start = self._resolve_addr(self._cmp_from.text())
        if start is None:
            self._cmp_out.setPlainText("bad start address"); return
        try:
            romb = Path(self._cmp_path).read_bytes()
        except OSError as exc:
            self._cmp_out.setPlainText(f"could not read ROM B: {exc}"); return
        size = self._cmp_size.value() * 1024
        a = m.read(start & 0xFFFFFF, size)
        # ROM B is a cartridge image, so its file offset for a CPU address is addr-CART_BASE.
        b_off = (start - CART_BASE) if start >= CART_BASE else start
        b = romb[b_off:b_off + size]
        n = min(len(a), len(b))
        if n == 0:
            self._cmp_out.setPlainText("nothing to compare (ROM B too short for that range)."); return
        from core.romdiff import diff_ranges
        ranges = diff_ranges(a[:n], b[:n])
        if not ranges:
            self._cmp_out.setPlainText("no differences in that range."); return
        cap = 2000
        lines = [f"{len(ranges)} changed range(s)"
                 + (f" — showing first {cap}" if len(ranges) > cap else "")]
        for off, sa, sb in ranges[:cap]:
            addr = start + off
            if self._txt_table is not None:
                ta = repr(self._txt_table.decode(sa, stop_at_end=False)[0])
                tb = repr(self._txt_table.decode(sb, stop_at_end=False)[0])
            else:
                ta = sa.hex(" ").upper()
                tb = sb.hex(" ").upper()
            lines.append(f"{addr:06X}{self._rom_off(addr)}  A:{ta}")
            lines.append(f"{'':>{6 + len(self._rom_off(addr))}}  B:{tb}")
        self._cmp_out.setPlainText("\n".join(lines))

    def _refresh_compare(self) -> None:
        pass                    # on-demand only

    # ---- refresh dispatch
    def refresh(self) -> None:
        if self._m is not None and self._play is not None:
            self._btn_pause.setText("▶ Resume" if self._play.paused else "⏸ Pause")
            self._status.setText("paused" if self._play.paused else "running")
        idx = self._tabs.currentIndex()
        # ⚠️ This tuple is positional: it MUST stay in the same order as the addTab
        # calls in __init__, or a tab silently refreshes a different panel.
        (self._refresh_cpu, self._refresh_disasm, self._refresh_callstack,
         self._refresh_events, self._refresh_mem, self._refresh_watch,
         self._refresh_breaks, self._refresh_ramsearch, self._refresh_audio,
         self._refresh_palette, self._refresh_tiles, self._refresh_sprites,
         self._refresh_layers, self._refresh_load, self._refresh_text,
         self._refresh_crack, self._refresh_pointers, self._refresh_compare)[idx]()
