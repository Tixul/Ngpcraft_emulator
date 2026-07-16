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

from pathlib import Path

import numpy as np
from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtGui import QImage, QPixmap, QFont
from PyQt6.QtWidgets import (
    QMainWindow, QWidget, QLabel, QPushButton, QVBoxLayout, QHBoxLayout,
    QPlainTextEdit, QTabWidget, QComboBox, QLineEdit, QSpinBox, QCheckBox,
    QScrollArea, QFileDialog,
)

from core.decode import decode_instruction_at

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

        top = QWidget(); self.setCentralWidget(top)
        v = QVBoxLayout(top); v.setContentsMargins(8, 8, 8, 8); v.setSpacing(6)

        bar = QHBoxLayout()
        self._btn_pause = QPushButton("⏸ Pause"); self._btn_pause.clicked.connect(self._toggle_pause)
        self._btn_step = QPushButton("⏭ Step frame"); self._btn_step.clicked.connect(self._step)
        self._btn_reset = QPushButton("⟲ Reset"); self._btn_reset.clicked.connect(self._reset)
        for b in (self._btn_pause, self._btn_step, self._btn_reset):
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
        if self._m is None:
            return
        self._play.paused = True
        self._m.run_frames(1)
        self._play._blit()  # noqa: SLF001
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
        (self._refresh_cpu, self._refresh_disasm, self._refresh_mem,
         self._refresh_palette, self._refresh_tiles, self._refresh_sprites)[idx]()
