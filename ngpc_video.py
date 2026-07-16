"""Video post-processing for the shell's LCD — the filters/shaders layer.

The native core hands us a 160x152 frame of 12-bit colour (one u16 per pixel,
0x0BGR with 4 bits a channel). This module turns that into the QPixmap the LCD
shows, applying the user's chosen upscale, colour profile and screen filter
(scanlines, LCD grid, CRT) in numpy so it stays cheap at 60 fps.

Kept separate from the shell so the look can grow (and be unit-tested) without
touching the emulation loop. Inspired by the filter menus in RetroArch, mGBA and
Mesen: a small, curated set that actually suits a tiny handheld LCD.
"""

from __future__ import annotations

import numpy as np
from PyQt6.QtCore import Qt
from PyQt6.QtGui import QImage, QPixmap

SCREEN_W, SCREEN_H = 160, 152

# Filter ids (stored in settings; stable strings)
FILTER_NONE = "none"
FILTER_SCANLINES = "scanlines"
FILTER_LCD_GRID = "lcd_grid"
FILTER_CRT = "crt"
FILTERS = (FILTER_NONE, FILTER_SCANLINES, FILTER_LCD_GRID, FILTER_CRT)

# Colour profiles
COLOR_RAW = "raw"          # the pixels exactly as the core produced them
COLOR_LCD = "lcd"          # a gentle gamma + slight desaturation, the handheld look
COLOR_VIVID = "vivid"      # punchier, for big modern screens
COLOR_PROFILES = (COLOR_RAW, COLOR_LCD, COLOR_VIVID)

# Aspect handling
ASPECT_PIXEL = "pixel"     # integer scale, 1:1 pixels (sharpest)
ASPECT_FIT = "fit"         # fill the window keeping 160:152
ASPECT_STRETCH = "stretch"  # fill the window, ignore ratio
ASPECTS = (ASPECT_PIXEL, ASPECT_FIT, ASPECT_STRETCH)


def _decode(fb) -> np.ndarray:
    """160x152 core frame (u16 0x0BGR) -> (152,160,3) uint8 RGB."""
    a = np.asarray(fb, dtype=np.uint16).reshape(SCREEN_H, SCREEN_W)
    rgb = np.empty((SCREEN_H, SCREEN_W, 3), dtype=np.uint8)
    rgb[..., 0] = (a & 0x0F) * 17           # R = low nibble
    rgb[..., 1] = ((a >> 4) & 0x0F) * 17    # G
    rgb[..., 2] = ((a >> 8) & 0x0F) * 17    # B = high nibble
    return rgb


# Colour LUTs are cheap to precompute once (256 entries per channel).
def _gamma_lut(gamma: float, gain: float = 1.0) -> np.ndarray:
    x = np.linspace(0.0, 1.0, 256)
    y = np.clip((x ** gamma) * gain, 0.0, 1.0)
    return (y * 255.0 + 0.5).astype(np.uint8)


_LUT_LCD = _gamma_lut(0.90, 1.02)
_LUT_VIVID = _gamma_lut(1.15, 1.05)


def _apply_color(rgb: np.ndarray, profile: str) -> np.ndarray:
    if profile == COLOR_LCD:
        out = _LUT_LCD[rgb]
        # a touch of desaturation, the way a reflective LCD mutes pure colour
        luma = out.mean(axis=2, keepdims=True)
        return np.clip(out * 0.92 + luma * 0.08, 0, 255).astype(np.uint8)
    if profile == COLOR_VIVID:
        return _LUT_VIVID[rgb]
    return rgb


def _apply_filter(scaled: np.ndarray, filt: str, scale: int) -> np.ndarray:
    """`scaled` is the integer-upscaled RGB; darken rows/cols in place-ish."""
    if scale < 2 or filt == FILTER_NONE:
        return scaled
    out = scaled.astype(np.float32)
    h, w = out.shape[:2]
    if filt in (FILTER_SCANLINES, FILTER_CRT):
        # darken the lower part of every source-pixel band -> horizontal lines
        line = np.ones(scale, dtype=np.float32)
        line[-1] = 0.45 if filt == FILTER_CRT else 0.55
        if scale >= 3:
            line[-2] = 0.75
        rows = np.tile(line, h // scale + 1)[:h]
        out *= rows[:, None, None]
    if filt == FILTER_LCD_GRID:
        line = np.ones(scale, dtype=np.float32)
        line[-1] = 0.62
        rows = np.tile(line, h // scale + 1)[:h]
        cols = np.tile(line, w // scale + 1)[:w]
        out *= rows[:, None, None]
        out *= cols[None, :, None]
    if filt == FILTER_CRT:
        cols = np.ones(w, dtype=np.float32)
        col = np.ones(scale, dtype=np.float32); col[-1] = 0.85
        cols = np.tile(col, w // scale + 1)[:w]
        out *= cols[None, :, None]
        out = np.clip(out * 1.08, 0, 255)   # a little gain back for the glow
    return out.astype(np.uint8)


def render_array(fb, scale: int, filt: str, color: str) -> np.ndarray:
    """Full pipeline -> a contiguous (H*scale, W*scale, 3) uint8 RGB array."""
    scale = max(1, int(scale))
    rgb = _apply_color(_decode(fb), color)
    if scale > 1:
        rgb = np.repeat(np.repeat(rgb, scale, axis=0), scale, axis=1)
        rgb = _apply_filter(rgb, filt, scale)
    return np.ascontiguousarray(rgb)


def render_pixmap(fb, scale: int, filt: str, color: str, smooth: bool) -> QPixmap:
    arr = render_array(fb, scale, filt, color)
    h, w = arr.shape[:2]
    img = QImage(arr.data, w, h, 3 * w, QImage.Format.Format_RGB888)
    # keep the buffer alive for the QImage's lifetime by stashing it on the pixmap
    pix = QPixmap.fromImage(img.copy())
    return pix


def fit_pixmap(pix: QPixmap, box_w: int, box_h: int, aspect: str, smooth: bool) -> QPixmap:
    """Scale a rendered pixmap into a box for fullscreen / non-integer windows."""
    mode = (Qt.TransformationMode.SmoothTransformation if smooth
            else Qt.TransformationMode.FastTransformation)
    if aspect == ASPECT_STRETCH:
        return pix.scaled(box_w, box_h, Qt.AspectRatioMode.IgnoreAspectRatio, mode)
    if aspect == ASPECT_FIT:
        return pix.scaled(box_w, box_h, Qt.AspectRatioMode.KeepAspectRatio, mode)
    # pixel-perfect: largest integer multiple that fits
    k = max(1, min(box_w // SCREEN_W, box_h // SCREEN_H))
    return pix.scaled(SCREEN_W * k, SCREEN_H * k,
                      Qt.AspectRatioMode.KeepAspectRatio,
                      Qt.TransformationMode.FastTransformation)
