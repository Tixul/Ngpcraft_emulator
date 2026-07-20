"""The visual key-assignment map: bind a key by pointing at the console.

A picture of the machine with a capture field beside each control, joined by a
leader line. It replaces nothing structurally -- the fields are the same
`KeyCaptureButton`s the list used, wired to the same settings -- it only changes
where they sit, so a binding reads as "this key drives THAT button" instead of
"this key drives the row labelled Left".

Three things this has to get right, and each one is a place a mockup lies:

* **Scale.** The artwork is a fixed-size raster, so every position here is stored
  NORMALISED (0..1 of the console image) and multiplied at layout time. Nothing
  is in pixels, so nothing breaks when the window resizes.
* **Theme.** The labels, lines and field chrome are drawn by us in palette
  colours, never baked into the image. The console art is a transparent PNG so
  it sits on either background.
* **Language.** Same reason: a baked-in "HAUT" would still say HAUT in English.

POWER is shown but NOT bindable. The joypad register only has seven bits
(0x01..0x40); 0x80 is POWER and the C++ core drives it at boot. Rather than
silently drop it from the picture -- a console with no power button looks like a
bug -- it renders as a disabled field that says who owns it.
"""

from __future__ import annotations

import sys
from pathlib import Path

from PyQt6.QtCore import Qt, QRect, QPoint, pyqtSignal
from PyQt6.QtGui import QPixmap, QPainter, QPen, QColor, QPolygon
from PyQt6.QtWidgets import QWidget, QLabel

import ngpc_settings as cfg
import ngpc_theme

# Where each control sits, as a fraction of the console artwork. Measured off the
# asset itself (dark-blob centroids), not eyeballed -- see assets/ngpc_console.png.
# `side` says which way the leader line runs to reach its field.
ANCHORS: tuple[tuple[str, str, float, float, str], ...] = (
    # (binding label, i18n key, x, y, side)
    ("Power",  "btn_power",  0.1034, 0.1439, "left"),
    ("Up",     "btn_up",     0.1481, 0.2795, "left"),
    ("Left",   "btn_left",   0.0875, 0.3785, "left"),
    ("Down",   "btn_down",   0.1481, 0.4776, "left"),
    ("Right",  "btn_right",  0.2088, 0.3785, "left"),
    ("Option", "btn_option", 0.8921, 0.1408, "right"),
    ("B",      "btn_b",      0.8991, 0.3160, "right"),
    ("A",      "btn_a",      0.8110, 0.4098, "right"),
)

FIELD_W, FIELD_H = 132, 30
GUTTER = 150          # room reserved each side for the fields
LABEL_H = 15          # the small caption above each field
GAP = 18              # console-to-gutter breathing room
PAD = 10              # keep the fields off the panel edge
# Frozen into a single .exe, read-only resources are unpacked under _MEIPASS, not
# next to the module -- the same split ngpc_shell.py makes for its icon.
_ASSETS = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
ART = _ASSETS / "assets" / "ngpc_console.png"


class BindMap(QWidget):
    """Console picture + a capture field per control."""

    changed = pyqtSignal()      # a binding was rebound; the shell re-applies input

    def __init__(self, settings) -> None:
        super().__init__()
        self._settings = settings
        self._art = QPixmap(str(ART)) if ART.is_file() else QPixmap()
        self.buttons: dict[str, cfg.KeyCaptureButton] = {}
        self._captions: dict[str, QLabel] = {}
        self._console = QRect()
        self._power: QLabel | None = None
        self._tint: QPixmap | None = None
        self._tint_key: tuple | None = None

        for label, _key, _x, _y, _side in ANCHORS:
            cap = QLabel(self)
            cap.setObjectName("bindCaption")
            self._captions[label] = cap
            if label == "Power":
                # Not a binding: a read-only plate. See the module docstring.
                self._power = QLabel(self)
                self._power.setObjectName("bindDead")
                self._power.setAlignment(Qt.AlignmentFlag.AlignCenter)
                continue
            code = int(settings.value(f"input/{label}",
                                      cfg.DEFAULT_KEYS.get(label, 0), type=int))
            btn = cfg.KeyCaptureButton(code)
            btn.setObjectName("bindField")
            btn.setParent(self)

            def persist(new_code: int, lbl=label) -> None:
                cfg.set_binding(self._settings, lbl, int(new_code))
                self._settings.sync()
                self.changed.emit()
            btn.captured.connect(persist)
            self.buttons[label] = btn

        self.setMinimumHeight(300)
        self.retranslate()

    # -- geometry ---------------------------------------------------------
    def heightForWidth(self, w: int) -> int:      # noqa: N802 (Qt naming)
        if self._art.isNull():
            return 300
        art_w = max(1, w - 2 * (GUTTER + GAP))
        return int(art_w * self._art.height() / self._art.width())

    def hasHeightForWidth(self) -> bool:          # noqa: N802
        return True

    def resizeEvent(self, event) -> None:         # noqa: N802
        super().resizeEvent(event)
        self._relayout()

    def _relayout(self) -> None:
        """Scale the console to the middle, then hang each field off its anchor.

        Fields are placed at their anchor's own height and only then pushed apart
        where they would overlap, so each one stays as close as possible to the
        control it belongs to -- the whole point of the diagram."""
        if self._art.isNull():
            return
        avail_w = max(1, self.width() - 2 * (GUTTER + GAP))
        avail_h = max(1, self.height())
        scale = min(avail_w / self._art.width(), avail_h / self._art.height())
        cw = int(self._art.width() * scale)
        ch = int(self._art.height() * scale)
        self._console = QRect((self.width() - cw) // 2, (self.height() - ch) // 2, cw, ch)

        for side in ("left", "right"):
            rows = [(lbl, self._console.y() + y * ch)
                    for lbl, _k, _x, y, s in ANCHORS if s == side]
            rows.sort(key=lambda r: r[1])
            spread = _spread(
                [y for _l, y in rows], FIELD_H + LABEL_H + 8, 0, max(1, self.height()))
            x = PAD if side == "left" else self.width() - FIELD_W - PAD
            for (lbl, _y), cy in zip(rows, spread):
                top = int(cy - FIELD_H / 2)
                self._captions[lbl].setGeometry(x, top - LABEL_H - 2, FIELD_W, LABEL_H)
                w = self.buttons.get(lbl) or self._power
                if w is not None:
                    w.setGeometry(x, top, FIELD_W, FIELD_H)

    def _anchor_px(self, nx: float, ny: float) -> QPoint:
        return QPoint(int(self._console.x() + nx * self._console.width()),
                      int(self._console.y() + ny * self._console.height()))

    # -- painting ---------------------------------------------------------
    def _tinted(self, size, colour: QColor) -> QPixmap:
        """The line art, recoloured to the theme and scaled, cached.

        The asset is pure white with an alpha channel carrying the linework, so
        `SourceIn` repaints every stroke while keeping the antialiasing intact.
        That is why the art is stored colourless: a blueprint baked in one colour
        would be the same trap as a stylesheet with a colour written in place --
        legible in the theme it was drawn for, and wrong in the other."""
        key = (size.width(), size.height(), colour.rgba())
        if self._tint_key != key or self._tint is None:
            pm = self._art.scaled(size, Qt.AspectRatioMode.IgnoreAspectRatio,
                                  Qt.TransformationMode.SmoothTransformation)
            q = QPainter(pm)
            q.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceIn)
            q.fillRect(pm.rect(), colour)
            q.end()
            self._tint, self._tint_key = pm, key
        return self._tint

    def paintEvent(self, event) -> None:          # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        pal = ngpc_theme.current()
        if not self._art.isNull():
            p.drawPixmap(self._console.topLeft(),
                         self._tinted(self._console.size(), QColor(pal.text)))

        accent = QColor(pal.accent)
        for label, _key, nx, ny, side in ANCHORS:
            w = self.buttons.get(label) or self._power
            if w is None:
                continue
            a = self._anchor_px(nx, ny)
            g = w.geometry()
            # Meet the field on its inner edge, at its own height: an elbow, so the
            # line never crosses the console face diagonally.
            end = QPoint(g.right() + 1 if side == "left" else g.left() - 1,
                         g.center().y())
            dim = QColor(accent)
            dim.setAlpha(150 if label != "Power" else 70)
            p.setPen(QPen(dim, 2, Qt.PenStyle.SolidLine,
                          Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
            mid_x = (end.x() + a.x()) // 2
            p.drawPolyline(QPolygon([end, QPoint(mid_x, end.y()),
                                     QPoint(mid_x, a.y()), a]))
            p.setBrush(accent if label != "Power" else QColor(pal.text_dim))
            p.setPen(Qt.PenStyle.NoPen)
            p.drawEllipse(a, 4, 4)
        p.end()

    # -- text -------------------------------------------------------------
    def retranslate(self) -> None:
        lang = cfg.language(self._settings)
        for label, key, _x, _y, _s in ANCHORS:
            self._captions[label].setText(cfg.tr(lang, key))
        for btn in self.buttons.values():
            btn.set_prompt(cfg.tr(lang, "press_key"))
        if self._power is not None:
            self._power.setText(cfg.tr(lang, "power_managed"))

    def refresh(self) -> None:
        """Re-read every binding from settings (after a 'restore defaults')."""
        for label, btn in self.buttons.items():
            code = int(self._settings.value(f"input/{label}",
                                            cfg.DEFAULT_KEYS.get(label, 0), type=int))
            btn._key = code          # noqa: SLF001 -- same module family
            btn._render()            # noqa: SLF001


def _spread(centres: list[float], min_gap: float, lo: float, hi: float) -> list[float]:
    """Nudge sorted positions apart until none overlap, then clamp into [lo, hi].

    A single forward pass then a backward pass: the forward pass fixes overlaps,
    the backward pass pulls the stack back inside the widget if the forward pass
    pushed the last one off the bottom."""
    out = list(centres)
    for i in range(1, len(out)):
        out[i] = max(out[i], out[i - 1] + min_gap)
    overflow = out[-1] + min_gap / 2 - hi if out else 0
    if overflow > 0:
        out = [y - overflow for y in out]
    for i in range(len(out) - 2, -1, -1):
        out[i] = min(out[i], out[i + 1] - min_gap)
    shortfall = lo + min_gap / 2 - out[0] if out else 0
    if shortfall > 0:
        out = [y + shortfall for y in out]
    return out
