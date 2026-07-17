"""The debug window — a live look inside the running console.

A separate, non-modal window the shell opens with F1 / the rail / the pause menu.
It reads the native machine directly (registers, memory, VRAM) and refreshes a few
times a second while visible. Modelled on Mesen's debugger: CPU state, a
disassembly around PC, a memory hex viewer, and the graphics viewers (palette,
tiles, sprites).

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
from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtGui import QImage, QPixmap, QFont
from PyQt6.QtWidgets import (
    QMainWindow, QWidget, QLabel, QPushButton, QVBoxLayout, QHBoxLayout,
    QPlainTextEdit, QTabWidget, QComboBox, QLineEdit, QSpinBox, QCheckBox,
    QScrollArea, QFileDialog, QTableWidget, QTableWidgetItem, QHeaderView,
    QAbstractItemView,
)

from core.decode import decode_instruction_at
from core.watches import Watch
from core.exec_breaks import ExecBreak
from core.ramsearch import RamSearch
from core.vgm_export import VgmRecorder
from core.ngps_export import NgpsRecorder

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
OAM_BASE = 0x008800
OAM_CPC = 0x008C00
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


def decode_tiles(char_bytes: bytes, palette_rgb: np.ndarray) -> np.ndarray:
    """char_bytes: N*16 bytes of 2bpp tiles -> (rows*8, cols*8, 3) uint8 sheet."""
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
    cols = 16
    rows = (n + cols - 1) // cols
    sheet = np.zeros((rows * 8, cols * 8, 3), np.uint8)
    for i in range(n):
        r, c = divmod(i, cols)
        sheet[r * 8:(r + 1) * 8, c * 8:(c + 1) * 8] = rgb[i]
    return sheet


def _pixmap(arr: np.ndarray, scale: int) -> QPixmap:
    arr = np.ascontiguousarray(arr)
    h, w = arr.shape[:2]
    img = QImage(arr.data, w, h, 3 * w, QImage.Format.Format_RGB888)
    pix = QPixmap.fromImage(img.copy())
    return pix.scaled(w * scale, h * scale, Qt.AspectRatioMode.IgnoreAspectRatio,
                      Qt.TransformationMode.FastTransformation)


class DebugWindow(QMainWindow):
    def __init__(self, parent, settings) -> None:
        super().__init__(parent)
        self.setWindowTitle("NgpCraft — Debug")
        self.setWindowFlag(Qt.WindowType.Window, True)
        self.resize(760, 640)
        self._play = None
        self._settings = settings
        self._frozen = False
        self._tiles_arr = None
        self._pal_arr = None
        self._watch_building = False       # guards table edits from re-committing
        self._watch_rom = None             # last ROM stem shown, to reload on change
        self._breaks_building = False
        self._breaks_rom = None
        self._ram = RamSearch()            # RAM-search session (this window's)
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
        self._tabs.addTab(self._mem_tab(), "Memory")
        self._tabs.addTab(self._watch_tab(), "Watch")
        self._tabs.addTab(self._breaks_tab(), "Breakpoints")
        self._tabs.addTab(self._ramsearch_tab(), "RAM Search")
        self._tabs.addTab(self._audio_tab(), "Audio")
        self._tabs.addTab(self._palette_tab(), "Palette")
        self._tabs.addTab(self._tiles_tab(), "Tiles")
        self._tabs.addTab(self._sprites_tab(), "Sprites")
        self._tabs.currentChanged.connect(lambda _i: self.refresh())
        v.addWidget(self._tabs, 1)

        self._timer = QTimer(self)
        self._timer.timeout.connect(self._on_timer)

    # ---- lifecycle
    def attach(self, play) -> None:
        self._play = play

    @property
    def _m(self):
        return self._play.machine if self._play is not None else None

    def showEvent(self, e) -> None:  # type: ignore[override]
        self._timer.start(120)
        self.refresh(); super().showEvent(e)

    def hideEvent(self, e) -> None:  # type: ignore[override]
        self._timer.stop(); super().hideEvent(e)

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
        lines = [f"PC   {c.pc:06X}      flags [{flags}]   IFF {c.iff_level}",
                 f"SR   {c.sr_raw:04X}", ""]
        regs = list(c.regs)
        for i, name in enumerate(REG_NAMES):
            r = regs[i] if i < len(regs) else 0
            lines.append(f"{name} {r:08X}   {name[1:]} {r & 0xFFFF:04X}   "
                         f"{name[1]} {r & 0xFF:02X}")
        self._cpu_text.setPlainText("\n".join(lines))

    # ---- Disassembly tab
    def _disasm_tab(self) -> QWidget:
        w = QWidget(); lay = QVBoxLayout(w)
        bar = QHBoxLayout()
        bar.addWidget(QLabel("Lines"))
        self._dis_count = QSpinBox(); self._dis_count.setRange(8, 400); self._dis_count.setValue(32)
        self._dis_count.valueChanged.connect(self.refresh)
        bar.addWidget(self._dis_count)
        bar.addWidget(QLabel("Trace"))
        self._trace_count = QSpinBox(); self._trace_count.setRange(64, 500000)
        self._trace_count.setValue(5000); self._trace_count.setSingleStep(1000)
        bar.addWidget(self._trace_count)
        self._btn_trace = QPushButton("⏺ Trace to file…"); self._btn_trace.setObjectName("ghost")
        self._btn_trace.setToolTip("Run that many instructions and write every one to a file "
                                   "(advances the game).")
        self._btn_trace.clicked.connect(self._trace_to_file)
        bar.addWidget(self._btn_trace)
        bar.addStretch()
        lay.addLayout(bar)
        self._dis_text = QPlainTextEdit(); self._dis_text.setReadOnly(True)
        self._dis_text.setFont(QFont(_MONO, 11))
        lay.addWidget(self._dis_text)
        lay.addLayout(self._export_row(
            lambda: self._save_text(self._dis_text.toPlainText(), "disasm.txt")))
        return w

    def _refresh_disasm(self) -> None:
        m = self._m
        if m is None:
            self._dis_text.setPlainText("(no game running)"); return
        bus = _Bus(m)
        pc = m.cpu().pc
        cur = pc
        out = []
        for _ in range(self._dis_count.value()):
            try:
                d = decode_instruction_at(bus, pc)
            except Exception:
                out.append(f"  {pc:06X}  ??"); break
            raw = (d.raw_bytes or b"").hex(" ")
            asm = d.assembly or (d.mnemonic or "??")
            mark = "▶" if pc == cur else " "
            out.append(f"{mark} {pc:06X}  {raw:<14} {asm}")
            if d.status != "decoded" or d.next_sequential_pc is None:
                break
            pc = d.next_sequential_pc
        self._dis_text.setPlainText("\n".join(out))

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
        lines = [f"; execution trace, {total} instructions from PC={m.cpu().pc:06X}"]
        remaining = total
        while remaining > 0:
            _summ, recs = m.run(min(remaining, 4096), record=True)
            if not recs:
                break
            for r in recs:
                raw = bytes(r.raw[:r.raw_len])
                lines.append(f"{r.pc:06X}  {raw.hex(' '):<14} {_disasm_bytes(raw, r.pc)}")
            remaining -= len(recs)
        Path(path).write_text("\n".join(lines), encoding="utf-8")
        self._status.setText(f"traced {total - remaining} instr -> {Path(path).name}")
        self._play.paused = was_paused
        self._play._blit()  # noqa: SLF001
        self.refresh()

    # ---- Memory tab
    def _mem_tab(self) -> QWidget:
        w = QWidget(); lay = QVBoxLayout(w)
        bar = QHBoxLayout()
        self._mem_region = QComboBox()
        for name, addr in MEM_REGIONS:
            self._mem_region.addItem(name, addr)
        self._mem_region.currentIndexChanged.connect(self._on_region)
        self._mem_addr = QLineEdit("004000"); self._mem_addr.setFixedWidth(90)
        self._mem_addr.setFont(QFont(_MONO, 10))
        self._mem_addr.editingFinished.connect(self.refresh)
        self._mem_rows = QSpinBox(); self._mem_rows.setRange(8, 4096); self._mem_rows.setValue(24)
        self._mem_rows.valueChanged.connect(self.refresh)
        bar.addWidget(QLabel("Region")); bar.addWidget(self._mem_region)
        bar.addWidget(QLabel("Addr")); bar.addWidget(self._mem_addr)
        bar.addWidget(QLabel("Rows")); bar.addWidget(self._mem_rows)
        bar.addStretch()
        lay.addLayout(bar)
        self._mem_text = QPlainTextEdit(); self._mem_text.setReadOnly(True)
        self._mem_text.setFont(QFont(_MONO, 10))
        lay.addWidget(self._mem_text)
        # Poke: write hex bytes at an address (turns the viewer into an editor).
        poke = QHBoxLayout()
        self._poke_addr = QLineEdit(); self._poke_addr.setFixedWidth(90)
        self._poke_addr.setFont(QFont(_MONO, 10)); self._poke_addr.setPlaceholderText("addr")
        self._poke_val = QLineEdit(); self._poke_val.setFont(QFont(_MONO, 10))
        self._poke_val.setPlaceholderText("hex bytes, e.g. 12 FF 00")
        poke_btn = QPushButton("Poke"); poke_btn.clicked.connect(self._poke)
        self._poke_val.returnPressed.connect(self._poke)
        poke.addWidget(QLabel("Write")); poke.addWidget(self._poke_addr)
        poke.addWidget(QLabel("=")); poke.addWidget(self._poke_val, 1); poke.addWidget(poke_btn)
        lay.addLayout(poke)
        lay.addLayout(self._export_row(
            lambda: self._save_text(self._mem_text.toPlainText(), "memory.txt")))
        return w

    def _poke(self) -> None:
        m = self._m
        if m is None:
            return
        try:
            addr = int((self._poke_addr.text() or self._mem_addr.text()), 16) & 0xFFFFFF
            data = bytes(int(b, 16) for b in self._poke_val.text().split())
        except ValueError:
            self._poke_val.setStyleSheet("color:#e06c75")   # bad input -> red
            return
        if data:
            self._poke_val.setStyleSheet("")
            m.write(addr, data)
            self.refresh()

    def _on_region(self) -> None:
        self._mem_addr.setText(f"{self._mem_region.currentData():06X}")
        self.refresh()

    def _refresh_mem(self) -> None:
        m = self._m
        if m is None:
            self._mem_text.setPlainText("(no game running)"); return
        try:
            base = int(self._mem_addr.text(), 16) & 0xFFFFF0
        except ValueError:
            return
        nrows = self._mem_rows.value()
        rows = []
        data = m.read(base & 0xFFFFFF, 16 * nrows)
        for r in range(nrows):
            addr = base + r * 16
            chunk = data[r * 16:(r + 1) * 16]
            hexs = " ".join(f"{b:02X}" for b in chunk)
            ascii_ = "".join(chr(b) if 32 <= b < 127 else "." for b in chunk)
            rows.append(f"{addr:06X}  {hexs:<47}  {ascii_}")
        self._mem_text.setPlainText("\n".join(rows))

    # ---- Watch tab
    _SIZE_OPTS = [("1", 1), ("2", 2), ("4", 4)]
    _FMT_OPTS = [("hex", "hex"), ("dec", "u"), ("s.dec", "s")]
    _BREAK_OPTS = [("—", ""), ("change", "change"), ("write", "write"),
                   ("=", "="), ("≠", "!="), ("<", "<"), (">", ">"), ("≤", "<="), ("≥", ">=")]
    _WATCH_COLS = ["Name", "Addr", "Size", "Fmt", "Break", "Value", "Lock", "Live"]

    def _watch_tab(self) -> QWidget:
        w = QWidget(); lay = QVBoxLayout(w)
        lay.addWidget(QLabel(
            "Name memory addresses and watch them live. Break: 'change' / a comparison "
            "pauses on value; 'write' pauses and shows which PC wrote it. Lock freezes the "
            "address to Value. Saved per ROM."))
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
            "Pause when PC reaches an address. Condition (optional): 'ADDR[.size] OP VALUE', "
            "e.g. '4812 = 0' or '4a00.2 > 0x100' — fires only when it holds. Saved per ROM."))
        t = QTableWidget(0, 3)
        t.setHorizontalHeaderLabels(["PC", "Condition", "On"])
        t.verticalHeader().setVisible(False)
        t.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        hh = t.horizontalHeader()
        hh.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        hh.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        hh.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        t.itemChanged.connect(self._on_break_item)
        self._break_table = t
        lay.addWidget(t, 1)
        bar = QHBoxLayout()
        add = QPushButton("＋ Add"); add.setObjectName("ghost"); add.clicked.connect(self._break_add)
        rem = QPushButton("－ Remove"); rem.setObjectName("ghost"); rem.clicked.connect(self._break_remove)
        bar.addWidget(add); bar.addWidget(rem); bar.addStretch()
        lay.addLayout(bar)
        return w

    def _break_add_row(self, bp: ExecBreak | None = None) -> None:
        t = self._break_table
        r = t.rowCount(); t.insertRow(r)
        t.setItem(r, 0, QTableWidgetItem(f"{bp.pc:06X}" if bp else ""))
        t.setItem(r, 1, QTableWidgetItem(bp.cond if bp else ""))
        on = QTableWidgetItem()
        on.setFlags(Qt.ItemFlag.ItemIsUserCheckable | Qt.ItemFlag.ItemIsEnabled)
        on.setCheckState(Qt.CheckState.Checked if (bp is None or bp.enabled)
                         else Qt.CheckState.Unchecked)
        on.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
        t.setItem(r, 2, on)

    def _break_add(self) -> None:
        self._breaks_building = True
        self._break_add_row()
        self._breaks_building = False

    def _break_remove(self) -> None:
        r = self._break_table.currentRow()
        if r >= 0:
            self._break_table.removeRow(r)
            self._commit_breaks()

    def _on_break_item(self, _item) -> None:
        if not self._breaks_building:
            self._commit_breaks()

    def _row_to_break(self, r: int) -> ExecBreak | None:
        t = self._break_table
        cell = t.item(r, 0)
        pctxt = cell.text().strip() if cell else ""
        if not pctxt:
            return None
        try:
            pc = int(pctxt, 16)
        except ValueError:
            return None
        cond = t.item(r, 1).text().strip() if t.item(r, 1) else ""
        on = t.item(r, 2)
        enabled = on is None or on.checkState() == Qt.CheckState.Checked
        return ExecBreak(pc, cond, enabled)

    def _commit_breaks(self) -> None:
        if self._play is None or self._breaks_building:
            return
        items = [b for r in range(self._break_table.rowCount())
                 if (b := self._row_to_break(r)) is not None]
        self._play.breaks.items = items
        self._play._save_breaks()  # noqa: SLF001

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
        nb = QPushButton("New search"); nb.setObjectName("ghost"); nb.clicked.connect(self._rs_new)
        r1.addWidget(QLabel("Range")); r1.addWidget(self._rs_start)
        r1.addWidget(QLabel("‥")); r1.addWidget(self._rs_end)
        r1.addWidget(QLabel("Size")); r1.addWidget(self._rs_size)
        r1.addWidget(self._rs_signed); r1.addWidget(nb); r1.addStretch()
        lay.addLayout(r1)
        r2 = QHBoxLayout()
        self._rs_value = QLineEdit(); self._rs_value.setFixedWidth(84)
        self._rs_value.setPlaceholderText("value"); self._rs_value.setFont(QFont(_MONO, 10))
        r2.addWidget(self._rs_value)
        for lab, op in (("=", "="), ("≠", "!="), (">", ">"), ("<", "<")):
            b = QPushButton(lab); b.setObjectName("ghost"); b.setFixedWidth(32)
            b.clicked.connect(lambda _c, o=op: self._rs_filter(o, True)); r2.addWidget(b)
        r2.addSpacing(10)
        for lab, op in (("changed", "changed"), ("=prev", "unchanged"),
                        ("▲", "increased"), ("▼", "decreased")):
            b = QPushButton(lab); b.setObjectName("ghost")
            b.clicked.connect(lambda _c, o=op: self._rs_filter(o, False)); r2.addWidget(b)
        r2.addStretch()
        lay.addLayout(r2)
        self._rs_count = QLabel("no search"); self._rs_count.setObjectName("hint")
        lay.addWidget(self._rs_count)
        t = QTableWidget(0, 2); t.setHorizontalHeaderLabels(["Address", "Value"])
        t.verticalHeader().setVisible(False)
        t.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        t.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        t.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        t.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
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
                                 self._rs_signed.isChecked())
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
                self._rs_value.setStyleSheet("color:#e06c75"); return
            self._rs_value.setStyleSheet("")
        n = self._ram.refine(m, op, operand)
        self._rs_count.setText(f"{n} candidates")
        self._rs_update_list()

    def _rs_update_list(self) -> None:
        m = self._m
        res = self._ram.results(m) if m is not None else []
        t = self._rs_list
        t.setRowCount(0)
        for addr, val in res:
            r = t.rowCount(); t.insertRow(r)
            a = QTableWidgetItem(f"{addr:06X}"); a.setFont(QFont(_MONO, 10))
            v = QTableWidgetItem(val); v.setFont(QFont(_MONO, 10))
            v.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            t.setItem(r, 0, a); t.setItem(r, 1, v)
        total = self._ram.count()
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
        res = dict(self._ram.results(m))         # addr -> live value, no structural change
        t = self._rs_list
        for r in range(t.rowCount()):
            a = t.item(r, 0); v = t.item(r, 1)
            if a is None or v is None:
                continue
            try:
                addr = int(a.text(), 16)
            except ValueError:
                continue
            if addr in res:
                v.setText(res[addr])

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
        self._scope.setStyleSheet("background:#111;")
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
    def _palette_tab(self) -> QWidget:
        w = QWidget(); lay = QVBoxLayout(w)
        lay.addWidget(QLabel("Colour palettes (each row = a 4-colour sub-palette)"))
        self._pal_label = QLabel(); self._pal_label.setAlignment(Qt.AlignmentFlag.AlignTop)
        sc = QScrollArea(); sc.setWidget(self._pal_label); sc.setWidgetResizable(True)
        lay.addWidget(sc)
        lay.addLayout(self._export_row(
            lambda: self._save_png(self._pal_arr, "palette.png"), "💾 Save PNG…"))
        return w

    def _refresh_palette(self) -> None:
        m = self._m
        if m is None:
            self._pal_label.setText("(no game running)"); return
        blocks = [("Sprite", 0x008200, 16), ("Plane 1", 0x008280, 16),
                  ("Plane 2", 0x008300, 16), ("Backdrop", 0x0083E0, 2),
                  ("Window", 0x0083F0, 2)]
        cell = 16
        total_rows = sum(rows for _, _, rows in blocks)
        img = np.zeros((total_rows * cell, 4 * cell, 3), np.uint8)
        y = 0
        for _name, base, rows in blocks:
            for r in range(rows):
                for c in range(4):
                    col = _read_u16(m, base + (r * 4 + c) * 2)
                    img[y * cell:(y + 1) * cell, c * cell:(c + 1) * cell] = _rgb_from_u16(col)
                y += 1
        self._pal_arr = np.repeat(np.repeat(img, 2, 0), 2, 1)
        self._pal_label.setPixmap(_pixmap(img, 2))

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
        lay.addLayout(bar)
        self._tile_label = QLabel(); self._tile_label.setAlignment(Qt.AlignmentFlag.AlignTop)
        sc = QScrollArea(); sc.setWidget(self._tile_label); sc.setWidgetResizable(True)
        lay.addWidget(sc)
        lay.addLayout(self._export_row(
            lambda: self._save_png(self._tiles_arr, "tiles.png"), "💾 Save PNG…"))
        return w

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
            self._tile_label.setText("(no game running)"); return
        char = m.read(CHAR_RAM, 256 * 16)
        sheet = decode_tiles(char, self._tile_palette_rgb())
        self._tiles_arr = np.repeat(np.repeat(sheet, 3, 0), 3, 1)
        self._tile_label.setPixmap(_pixmap(sheet, 3))

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

    # ---- refresh dispatch
    def refresh(self) -> None:
        if self._m is not None and self._play is not None:
            self._btn_pause.setText("▶ Resume" if self._play.paused else "⏸ Pause")
            self._status.setText("paused" if self._play.paused else "running")
        idx = self._tabs.currentIndex()
        (self._refresh_cpu, self._refresh_disasm, self._refresh_mem, self._refresh_watch,
         self._refresh_breaks, self._refresh_ramsearch, self._refresh_audio,
         self._refresh_palette, self._refresh_tiles, self._refresh_sprites)[idx]()
