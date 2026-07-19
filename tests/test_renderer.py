"""K2GE renderer + `screenshot` CLI tests (M2 Phase 1 pass 1.0).

Coverage scope for pass 1.0:
- `K2geControlRegisters` decoder (cold-start + overlayed values)
- `render_frame` backdrop fill (disabled BGC → black; enabled BGC →
  indexed backdrop color)
- `frame_to_ppm_bytes` serialization (header, nibble replication)
- `screenshot` CLI end-to-end (PPM file written, JSON payload shape,
  `--seed-from` overlay influences the rendered color)
"""

from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from core.k2ge import (
    K2GE_CHAR_RAM_BASE,
    K2GE_OAM_BASE,
    K2GE_OAM_PALETTE_CODES_BASE,
    K2GE_PALETTE_BG_COLORS_BASE,
    K2GE_PALETTE_WINDOW_COLORS_BASE,
    K2GE_PALETTE_SCR1_BASE,
    K2GE_PALETTE_SCR2_BASE,
    K2GE_PALETTE_SPRITE_BASE,
    K2GE_REG_2D_CONTROL,
    K2GE_REG_BGC,
    K2GE_REG_MODE,
    K2GE_REG_PO_H,
    K2GE_REG_PO_V,
    K2GE_REG_S1SO_H,
    K2GE_REG_S1SO_V,
    K2GE_REG_S2SO_H,
    K2GE_REG_S2SO_V,
    K2GE_REG_SCROLL_PRIO,
    K2GE_REG_WBA_H,
    K2GE_REG_WBA_V,
    K2GE_REG_WSI_H,
    K2GE_REG_WSI_V,
    K2GE_SCR1_TILEMAP_BASE,
    K2GE_SCR2_TILEMAP_BASE,
    K2geColor,
    read_control_registers,
)
from core.renderer import resolve_sprite_positions
from core.machine import load_machine_state
from core.renderer import (
    NGPC_SCREEN_HEIGHT,
    NGPC_SCREEN_WIDTH,
    frame_to_ppm_bytes,
    render_frame,
    resolve_backdrop_color,
    resolve_oowc_color,
)
from core.savestate import build_savestate_payload, save_savestate
from ngpc_emu import main


def _write_demo_rom(path: Path, entry_point: int = 0x00200040) -> None:
    data = bytearray(0x40)
    data[0x00:0x1C] = b"LICENSED BY SNK CORPORATION".ljust(0x1C, b"\x00")
    data[0x1C:0x20] = entry_point.to_bytes(4, "little")
    data[0x22] = 0
    data[0x23] = 0x10
    data[0x24:0x30] = b"RENDERER1\x00\x00\x00"
    path.write_bytes(bytes(data))


class ControlRegisterDecodeTests(unittest.TestCase):
    def test_cold_start_view_decodes_to_zero_disabled_backdrop(self) -> None:
        # Empty memory dict — every register reads as 0 via the default
        # of `_read_byte`. This matches the cold-start CPU I/O page.
        ctrl = read_control_registers({})
        self.assertEqual(ctrl.wba_h, 0)
        self.assertEqual(ctrl.wba_v, 0)
        self.assertEqual(ctrl.wsi_h, 0)
        self.assertEqual(ctrl.wsi_v, 0)
        self.assertFalse(ctrl.neg)
        self.assertEqual(ctrl.oowc, 0)
        self.assertEqual(ctrl.po_h, 0)
        self.assertEqual(ctrl.po_v, 0)
        self.assertFalse(ctrl.scr2_in_front)
        self.assertEqual(ctrl.s1so_h, 0)
        self.assertEqual(ctrl.s1so_v, 0)
        self.assertEqual(ctrl.s2so_h, 0)
        self.assertEqual(ctrl.s2so_v, 0)
        self.assertFalse(ctrl.bgc_enabled)
        self.assertEqual(ctrl.bgc_index, 0)
        self.assertEqual(ctrl.bgc_raw, 0x00)
        self.assertFalse(ctrl.k1ge_compat)

    def test_bgc_enable_rule_requires_bit7_set_and_bit6_clear(self) -> None:
        # Bit 7 = 1, bit 6 = 0, bits 2..0 = 5 → enabled, index 5.
        ctrl = read_control_registers({K2GE_REG_BGC: 0x85})
        self.assertTrue(ctrl.bgc_enabled)
        self.assertEqual(ctrl.bgc_index, 5)
        self.assertEqual(ctrl.bgc_raw, 0x85)

        # Bit 7 = 1 but bit 6 also = 1 → disabled (per HW_QUICKREF rule).
        ctrl = read_control_registers({K2GE_REG_BGC: 0xC5})
        self.assertFalse(ctrl.bgc_enabled)
        self.assertEqual(ctrl.bgc_index, 5)

        # Bit 7 = 0, bit 6 = 0 → disabled.
        ctrl = read_control_registers({K2GE_REG_BGC: 0x05})
        self.assertFalse(ctrl.bgc_enabled)

    def test_2d_control_decodes_neg_and_oowc(self) -> None:
        # NEG=1, OOWC=4 → 0x84.
        ctrl = read_control_registers({K2GE_REG_2D_CONTROL: 0x84})
        self.assertTrue(ctrl.neg)
        self.assertEqual(ctrl.oowc, 4)

    def test_scroll_prio_decodes_high_bit(self) -> None:
        ctrl = read_control_registers({K2GE_REG_SCROLL_PRIO: 0x80})
        self.assertTrue(ctrl.scr2_in_front)
        ctrl = read_control_registers({K2GE_REG_SCROLL_PRIO: 0x00})
        self.assertFalse(ctrl.scr2_in_front)

    def test_scroll_window_sprite_offset_fields(self) -> None:
        memory = {
            K2GE_REG_WBA_H: 0x10, K2GE_REG_WBA_V: 0x20,
            K2GE_REG_WSI_H: 0xFF, K2GE_REG_WSI_V: 0x80,
            K2GE_REG_PO_H: 0x05, K2GE_REG_PO_V: 0xFB,
            K2GE_REG_S1SO_H: 0x33, K2GE_REG_S1SO_V: 0x44,
            K2GE_REG_S2SO_H: 0x55, K2GE_REG_S2SO_V: 0x66,
        }
        ctrl = read_control_registers(memory)
        self.assertEqual((ctrl.wba_h, ctrl.wba_v), (0x10, 0x20))
        self.assertEqual((ctrl.wsi_h, ctrl.wsi_v), (0xFF, 0x80))
        self.assertEqual((ctrl.po_h, ctrl.po_v), (0x05, 0xFB))
        self.assertEqual((ctrl.s1so_h, ctrl.s1so_v), (0x33, 0x44))
        self.assertEqual((ctrl.s2so_h, ctrl.s2so_v), (0x55, 0x66))

    def test_mode_k1ge_compat_bit7(self) -> None:
        ctrl = read_control_registers({K2GE_REG_MODE: 0x80})
        self.assertTrue(ctrl.k1ge_compat)
        ctrl = read_control_registers({K2GE_REG_MODE: 0x00})
        self.assertFalse(ctrl.k1ge_compat)


class ResolveBackdropTests(unittest.TestCase):
    def test_disabled_bgc_falls_back_to_black(self) -> None:
        ctrl = read_control_registers({K2GE_REG_BGC: 0x00})
        color = resolve_backdrop_color({}, ctrl)
        self.assertEqual((color.r, color.g, color.b), (0, 0, 0))

    def test_enabled_bgc_indexes_backdrop_block(self) -> None:
        # Enable BGC with index 3 → consume the 4th backdrop color.
        # Place a recognisable color in slot 3 (raw 0x0F00 → blue).
        slot_base = K2GE_PALETTE_BG_COLORS_BASE + 3 * 2
        memory = {
            K2GE_REG_BGC: 0x83,           # enabled, index = 3
            slot_base: 0x00,
            slot_base + 1: 0x0F,          # raw 0x0F00 = blue (B=15)
        }
        ctrl = read_control_registers(memory)
        color = resolve_backdrop_color(memory, ctrl)
        self.assertEqual((color.r, color.g, color.b), (0, 0, 15))
        self.assertEqual(color.hex_rgb24(), "#0000FF")


class RenderFrameTests(unittest.TestCase):
    def test_cold_start_frame_is_all_black(self) -> None:
        frame = render_frame({})
        self.assertEqual(frame.width, NGPC_SCREEN_WIDTH)
        self.assertEqual(frame.height, NGPC_SCREEN_HEIGHT)
        # Every pixel should be the cold-start backdrop = black.
        self.assertEqual(len(frame.pixels), NGPC_SCREEN_HEIGHT)
        self.assertEqual(len(frame.pixels[0]), NGPC_SCREEN_WIDTH)
        first = frame.pixels[0][0]
        self.assertEqual((first.r, first.g, first.b), (0, 0, 0))
        # Spot-check the four corners and the center.
        for y, x in (
            (0, 0),
            (0, NGPC_SCREEN_WIDTH - 1),
            (NGPC_SCREEN_HEIGHT - 1, 0),
            (NGPC_SCREEN_HEIGHT - 1, NGPC_SCREEN_WIDTH - 1),
            (NGPC_SCREEN_HEIGHT // 2, NGPC_SCREEN_WIDTH // 2),
        ):
            pixel = frame.pixels[y][x]
            self.assertEqual((pixel.r, pixel.g, pixel.b), (0, 0, 0))

    def test_overlay_with_enabled_bgc_fills_frame_with_indexed_color(self) -> None:
        # BGC index 2, backdrop slot 2 = red (raw 0x000F → r=15).
        slot_base = K2GE_PALETTE_BG_COLORS_BASE + 2 * 2
        memory = _new_memory()
        memory[K2GE_REG_BGC] = 0x82
        memory[slot_base] = 0x0F            # low byte: GGGG RRRR = 0x0F → r=15
        memory[slot_base + 1] = 0x00
        frame = render_frame(memory)
        # Every pixel should be the resolved backdrop = pure red.
        self.assertEqual(frame.backdrop_color.hex_rgb24(), "#FF0000")
        for row in frame.pixels:
            for pixel in row:
                self.assertEqual((pixel.r, pixel.g, pixel.b), (15, 0, 0))


# --- Helpers shared across renderer tests --------------------------------

def _new_memory() -> dict[int, int]:
    """Return a fresh memory dict pre-seeded with HW-reset register values.

    Real K2GE silicon resets `WSI.H = WSI.V = 0xFF` (window covers the
    full screen) and `REF = 0xC6`. The renderer's pass 1.3 window clip
    consumes WSI, so tests that drive `render_frame` start from this
    baseline and override individual registers as needed. Tests that
    only exercise the bare decoder (`read_control_registers({})`) keep
    the literal empty-dict form so they verify "what does the decoder
    return when no overlay is layered at all".
    """
    return {
        K2GE_REG_WSI_H: 0xFF,
        K2GE_REG_WSI_V: 0xFF,
    }


# --- Helpers for pass 1.1 scroll-plane tests -----------------------------

_TILEMAP_BASE_FOR_PLANE = {
    "scr1": K2GE_SCR1_TILEMAP_BASE,
    "scr2": K2GE_SCR2_TILEMAP_BASE,
}
_PALETTE_BASE_FOR_PLANE = {
    "scr1": K2GE_PALETTE_SCR1_BASE,
    "scr2": K2GE_PALETTE_SCR2_BASE,
}


def _set_palette_color(
    memory: dict[int, int],
    plane: str,
    palette_index: int,
    color_index: int,
    raw_0bgr: int,
) -> None:
    base = _PALETTE_BASE_FOR_PLANE[plane] + palette_index * 8 + color_index * 2
    memory[base] = raw_0bgr & 0xFF
    memory[base + 1] = (raw_0bgr >> 8) & 0xFF


def _set_tilemap_entry(
    memory: dict[int, int],
    plane: str,
    tx: int,
    ty: int,
    c_c: int,
    *,
    cp_c: int = 0,
    h_flip: bool = False,
    v_flip: bool = False,
    p_c: bool = False,
) -> None:
    base = _TILEMAP_BASE_FOR_PLANE[plane] + (ty * 32 + tx) * 2
    memory[base] = c_c & 0xFF
    attrib = (
        ((1 if h_flip else 0) << 7)
        | ((1 if v_flip else 0) << 6)
        | ((1 if p_c else 0) << 5)
        | ((cp_c & 0x0F) << 1)
        | ((c_c >> 8) & 0x01)
    )
    memory[base + 1] = attrib


def _set_tile_pixels(
    memory: dict[int, int],
    tile_id: int,
    rows: list[tuple[int, ...]],
) -> None:
    """Store an 8×8 tile (`rows` = 8 tuples of 8 ints 0..3) in CHAR_RAM.

    CHAR_RAM layout per `NGPC_HW_QUICKREF.md`:
      even byte (offset +0 of each row) packs dots 4..7, MSB=dot4
      odd byte  (offset +1)             packs dots 0..3, MSB=dot0
    """
    base = K2GE_CHAR_RAM_BASE + tile_id * 16
    for y, row in enumerate(rows):
        assert len(row) == 8, "tile rows must be 8 pixels wide"
        odd = 0
        even = 0
        for shift, value in zip((6, 4, 2, 0), row[0:4]):
            odd |= (value & 0x03) << shift
        for shift, value in zip((6, 4, 2, 0), row[4:8]):
            even |= (value & 0x03) << shift
        memory[base + y * 2] = even
        memory[base + y * 2 + 1] = odd


def _enable_red_backdrop(memory: dict[int, int]) -> None:
    """Convenience: enable BGC index 0 with backdrop slot 0 = red."""
    memory[K2GE_REG_BGC] = 0x80  # bit 7 set, bit 6 clear, index 0
    slot_base = K2GE_PALETTE_BG_COLORS_BASE
    memory[slot_base] = 0x0F      # raw 0x000F → r=15
    memory[slot_base + 1] = 0x00


class ScrollPlaneRenderTests(unittest.TestCase):
    """Pass 1.1 — SCR1/SCR2 raster: tilemap + CHAR_RAM + palette compose."""

    def test_cold_start_with_tilemap_all_zero_keeps_backdrop(self) -> None:
        # Even with the new scroll-plane code, an empty tilemap should
        # produce no draws — every pixel stays at the backdrop.
        memory: dict[int, int] = _new_memory()
        _enable_red_backdrop(memory)
        frame = render_frame(memory)
        for row in frame.pixels:
            for pixel in row:
                self.assertEqual((pixel.r, pixel.g, pixel.b), (15, 0, 0))

    def test_scr1_solid_tile_at_origin_renders_palette_color(self) -> None:
        memory: dict[int, int] = _new_memory()
        # Tile #1: all 64 pixels = value 1.
        rows = [(1,) * 8 for _ in range(8)]
        _set_tile_pixels(memory, 1, rows)
        # SCR1 palette 0, color 1 = green (raw 0x00F0).
        _set_palette_color(memory, "scr1", 0, 1, 0x00F0)
        # SCR1 tilemap (0, 0) → tile #1, palette 0.
        _set_tilemap_entry(memory, "scr1", 0, 0, 1, cp_c=0)
        frame = render_frame(memory)
        # Top-left 8×8 should be green; outside that block should be black backdrop.
        for y in range(8):
            for x in range(8):
                pixel = frame.pixels[y][x]
                self.assertEqual((pixel.r, pixel.g, pixel.b), (0, 15, 0))
        # Sample outside the tile.
        self.assertEqual(
            (frame.pixels[8][0].r, frame.pixels[8][0].g, frame.pixels[8][0].b),
            (0, 0, 0),
        )
        self.assertEqual(
            (frame.pixels[0][8].r, frame.pixels[0][8].g, frame.pixels[0][8].b),
            (0, 0, 0),
        )

    def test_palette_transparency_lets_backdrop_show_through(self) -> None:
        memory: dict[int, int] = _new_memory()
        _enable_red_backdrop(memory)
        # Tile #2: checkerboard 1 / 0 alternating per column.
        rows = [(1, 0, 1, 0, 1, 0, 1, 0) for _ in range(8)]
        _set_tile_pixels(memory, 2, rows)
        _set_palette_color(memory, "scr1", 0, 1, 0x0F00)  # color 1 = blue
        _set_tilemap_entry(memory, "scr1", 0, 0, 2, cp_c=0)
        frame = render_frame(memory)
        # Even columns (0, 2, 4, 6) → palette color 1 = blue.
        # Odd columns (1, 3, 5, 7) → palette index 0 = transparent → backdrop = red.
        for y in range(8):
            self.assertEqual(
                (frame.pixels[y][0].r, frame.pixels[y][0].g, frame.pixels[y][0].b),
                (0, 0, 15),
                msg=f"y={y} col=0",
            )
            self.assertEqual(
                (frame.pixels[y][1].r, frame.pixels[y][1].g, frame.pixels[y][1].b),
                (15, 0, 0),
                msg=f"y={y} col=1",
            )

    def test_h_flip_mirrors_horizontally(self) -> None:
        memory: dict[int, int] = _new_memory()
        # Tile #3 row 0 = [1, 0, 0, 0, 0, 0, 0, 0] — a single dot on the far left.
        rows: list[tuple[int, ...]] = [(0,) * 8 for _ in range(8)]
        rows[0] = (1, 0, 0, 0, 0, 0, 0, 0)
        _set_tile_pixels(memory, 3, rows)
        _set_palette_color(memory, "scr1", 0, 1, 0x00F0)  # green
        _set_tilemap_entry(memory, "scr1", 0, 0, 3, cp_c=0, h_flip=True)
        frame = render_frame(memory)
        # Without flip, pixel (0, 0) would be green. With H.F, it ends up at (7, 0).
        self.assertEqual(
            (frame.pixels[0][7].r, frame.pixels[0][7].g, frame.pixels[0][7].b),
            (0, 15, 0),
        )
        # Pixel (0, 0) reverts to backdrop (black cold-start).
        self.assertEqual(
            (frame.pixels[0][0].r, frame.pixels[0][0].g, frame.pixels[0][0].b),
            (0, 0, 0),
        )

    def test_v_flip_mirrors_vertically(self) -> None:
        memory: dict[int, int] = _new_memory()
        rows: list[tuple[int, ...]] = [(0,) * 8 for _ in range(8)]
        rows[0] = (1, 1, 1, 1, 1, 1, 1, 1)
        _set_tile_pixels(memory, 4, rows)
        _set_palette_color(memory, "scr1", 0, 1, 0x000F)  # red
        _set_tilemap_entry(memory, "scr1", 0, 0, 4, cp_c=0, v_flip=True)
        frame = render_frame(memory)
        # Row 0 was the solid line; with V.F it ends up at row 7.
        for x in range(8):
            pixel = frame.pixels[7][x]
            self.assertEqual((pixel.r, pixel.g, pixel.b), (15, 0, 0), msg=f"x={x}")
        # Row 0 reverts to backdrop.
        pixel0 = frame.pixels[0][0]
        self.assertEqual((pixel0.r, pixel0.g, pixel0.b), (0, 0, 0))

    def test_scroll_offset_h_shifts_view(self) -> None:
        memory: dict[int, int] = _new_memory()
        # Tile #5 row 0 = solid 1s.
        rows: list[tuple[int, ...]] = [(0,) * 8 for _ in range(8)]
        rows[0] = (1,) * 8
        _set_tile_pixels(memory, 5, rows)
        _set_palette_color(memory, "scr1", 0, 1, 0x00F0)  # green
        # Place tile at SCR1 (1, 0) — world-x range [8..15].
        _set_tilemap_entry(memory, "scr1", 1, 0, 5, cp_c=0)
        # Scroll right by 8 → screen pixel 0 reads world-x 8.
        memory[K2GE_REG_S1SO_H] = 8
        frame = render_frame(memory)
        # With scroll, the tile that lived at tx=1 now appears at sx=0..7.
        for x in range(8):
            pixel = frame.pixels[0][x]
            self.assertEqual((pixel.r, pixel.g, pixel.b), (0, 15, 0), msg=f"x={x}")
        # Beyond the tile's world range, backdrop (cold-start = black).
        self.assertEqual(
            (frame.pixels[0][8].r, frame.pixels[0][8].g, frame.pixels[0][8].b),
            (0, 0, 0),
        )

    def test_scr1_in_front_of_scr2_by_default(self) -> None:
        memory: dict[int, int] = _new_memory()
        # SCR1 tile = green at (0, 0).
        _set_tile_pixels(memory, 6, [(1,) * 8] * 8)
        _set_palette_color(memory, "scr1", 0, 1, 0x00F0)  # green
        _set_tilemap_entry(memory, "scr1", 0, 0, 6, cp_c=0)
        # SCR2 tile = blue, same position.
        _set_tile_pixels(memory, 7, [(1,) * 8] * 8)
        _set_palette_color(memory, "scr2", 0, 1, 0x0F00)  # blue
        _set_tilemap_entry(memory, "scr2", 0, 0, 7, cp_c=0)
        # SCROLL_PRIO bit 7 = 0 (default) → SCR1 in front.
        frame = render_frame(memory)
        for y in range(8):
            for x in range(8):
                pixel = frame.pixels[y][x]
                self.assertEqual((pixel.r, pixel.g, pixel.b), (0, 15, 0))

    def test_scr2_in_front_when_scroll_prio_bit7_set(self) -> None:
        memory: dict[int, int] = _new_memory()
        _set_tile_pixels(memory, 6, [(1,) * 8] * 8)
        _set_palette_color(memory, "scr1", 0, 1, 0x00F0)  # green
        _set_tilemap_entry(memory, "scr1", 0, 0, 6, cp_c=0)
        _set_tile_pixels(memory, 7, [(1,) * 8] * 8)
        _set_palette_color(memory, "scr2", 0, 1, 0x0F00)  # blue
        _set_tilemap_entry(memory, "scr2", 0, 0, 7, cp_c=0)
        # Flip the priority so SCR2 ends up on top.
        memory[K2GE_REG_SCROLL_PRIO] = 0x80
        frame = render_frame(memory)
        for y in range(8):
            for x in range(8):
                pixel = frame.pixels[y][x]
                self.assertEqual((pixel.r, pixel.g, pixel.b), (0, 0, 15))

    def test_front_plane_transparency_reveals_back_plane(self) -> None:
        memory: dict[int, int] = _new_memory()
        # SCR2 (back by default): solid green tile at (0, 0).
        _set_tile_pixels(memory, 7, [(1,) * 8] * 8)
        _set_palette_color(memory, "scr2", 0, 1, 0x00F0)  # green
        _set_tilemap_entry(memory, "scr2", 0, 0, 7, cp_c=0)
        # SCR1 (front): checkerboard 1 / 0 by column.
        _set_tile_pixels(memory, 6, [(1, 0, 1, 0, 1, 0, 1, 0)] * 8)
        _set_palette_color(memory, "scr1", 0, 1, 0x0F00)  # blue
        _set_tilemap_entry(memory, "scr1", 0, 0, 6, cp_c=0)
        frame = render_frame(memory)
        # Even columns → SCR1 blue, odd columns → SCR1 transparent so SCR2 green shows.
        for y in range(8):
            self.assertEqual(
                (frame.pixels[y][0].r, frame.pixels[y][0].g, frame.pixels[y][0].b),
                (0, 0, 15),
                msg=f"y={y} col=0 expected blue (front SCR1)",
            )
            self.assertEqual(
                (frame.pixels[y][1].r, frame.pixels[y][1].g, frame.pixels[y][1].b),
                (0, 15, 0),
                msg=f"y={y} col=1 expected green (back SCR2 through SCR1 transparent)",
            )

    def test_tile_with_c_c_bit8_high_decodes_as_9bit_tile(self) -> None:
        memory: dict[int, int] = _new_memory()
        # Tile #256 = base 0xA000 + 256*16 = 0xB000 ; place a recognisable line.
        rows: list[tuple[int, ...]] = [(0,) * 8 for _ in range(8)]
        rows[0] = (1,) * 8
        _set_tile_pixels(memory, 256, rows)
        _set_palette_color(memory, "scr1", 0, 1, 0x000F)  # red
        # Entry attrib bit 0 = c_c bit 8 = 1 to reach tile 256.
        _set_tilemap_entry(memory, "scr1", 0, 0, 256, cp_c=0)
        frame = render_frame(memory)
        for x in range(8):
            pixel = frame.pixels[0][x]
            self.assertEqual((pixel.r, pixel.g, pixel.b), (15, 0, 0), msg=f"x={x}")


# --- Helpers for pass 1.2 sprite-raster tests ---------------------------

def _set_sprite(
    memory: dict[int, int],
    index: int,
    c_c: int,
    h_pos: int,
    v_pos: int,
    *,
    pr_c: int = 3,
    cp_c: int = 0,
    h_flip: bool = False,
    v_flip: bool = False,
    p_c: bool = False,
    h_chain: bool = False,
    v_chain: bool = False,
) -> None:
    """Write one K2GE OAM entry (4 bytes at 0x8800+4n) + CP.C byte (0x8C00+n).

    Layout per `NGPC_HW_QUICKREF.md` § "Sprite VRAM" / `K2GE_OAM.md`.
    """
    base = K2GE_OAM_BASE + index * 4
    memory[base] = c_c & 0xFF
    attrib = (
        ((1 if h_flip else 0) << 7)
        | ((1 if v_flip else 0) << 6)
        | ((1 if p_c else 0) << 5)
        | ((pr_c & 0x03) << 3)
        | ((1 if h_chain else 0) << 2)
        | ((1 if v_chain else 0) << 1)
        | ((c_c >> 8) & 0x01)
    )
    memory[base + 1] = attrib
    memory[base + 2] = h_pos & 0xFF
    memory[base + 3] = v_pos & 0xFF
    memory[K2GE_OAM_PALETTE_CODES_BASE + index] = cp_c & 0x0F


def _set_sprite_palette_color(
    memory: dict[int, int],
    palette_index: int,
    color_index: int,
    raw_0bgr: int,
) -> None:
    base = K2GE_PALETTE_SPRITE_BASE + palette_index * 8 + color_index * 2
    memory[base] = raw_0bgr & 0xFF
    memory[base + 1] = (raw_0bgr >> 8) & 0xFF


class SpritePositionTests(unittest.TestCase):
    """Pass 1.2 — `resolve_sprite_positions` chain + global offset folding."""

    def test_unchained_sprite_uses_raw_h_v_pos(self) -> None:
        memory: dict[int, int] = _new_memory()
        _set_sprite(memory, 0, c_c=1, h_pos=10, v_pos=20, pr_c=3)
        control = read_control_registers(memory)
        positioned = resolve_sprite_positions(memory, control)
        sprite, sx, sy = positioned[0]
        self.assertEqual((sx, sy), (10, 20))
        self.assertEqual(sprite.c_c, 1)

    def test_h_chain_adds_previous_h_pos(self) -> None:
        memory: dict[int, int] = _new_memory()
        _set_sprite(memory, 0, c_c=1, h_pos=10, v_pos=20)
        _set_sprite(memory, 1, c_c=2, h_pos=8, v_pos=0, h_chain=True)
        # Sprite 1's screen X = sprite 0's effective X + 8 = 18.
        positioned = resolve_sprite_positions(memory, read_control_registers(memory))
        self.assertEqual((positioned[1][1], positioned[1][2]), (18, 0))

    def test_v_chain_adds_previous_v_pos(self) -> None:
        memory: dict[int, int] = _new_memory()
        _set_sprite(memory, 0, c_c=1, h_pos=0, v_pos=40)
        _set_sprite(memory, 1, c_c=2, h_pos=0, v_pos=8, v_chain=True)
        positioned = resolve_sprite_positions(memory, read_control_registers(memory))
        self.assertEqual((positioned[1][1], positioned[1][2]), (0, 48))

    def test_hidden_sprite_still_advances_chain_anchor(self) -> None:
        # Hidden sprite at h=100, then a chained visible sprite at h=5.
        # Chained position should be 105 — chain advances through PR.C=0.
        memory: dict[int, int] = _new_memory()
        _set_sprite(memory, 0, c_c=1, h_pos=100, v_pos=0, pr_c=0)
        _set_sprite(memory, 1, c_c=2, h_pos=5, v_pos=0, h_chain=True, pr_c=3)
        positioned = resolve_sprite_positions(memory, read_control_registers(memory))
        self.assertEqual(positioned[1][1], 105)

    def test_global_po_offset_added_to_every_sprite(self) -> None:
        memory: dict[int, int] = _new_memory()
        _set_sprite(memory, 0, c_c=1, h_pos=10, v_pos=20)
        memory[K2GE_REG_PO_H] = 3
        memory[K2GE_REG_PO_V] = 7
        positioned = resolve_sprite_positions(memory, read_control_registers(memory))
        self.assertEqual((positioned[0][1], positioned[0][2]), (13, 27))

    def test_chain_position_wraps_modulo_256(self) -> None:
        memory: dict[int, int] = _new_memory()
        _set_sprite(memory, 0, c_c=1, h_pos=250, v_pos=0)
        _set_sprite(memory, 1, c_c=2, h_pos=10, v_pos=0, h_chain=True)
        # 250 + 10 = 260 → wraps to 4 (mod 256).
        positioned = resolve_sprite_positions(memory, read_control_registers(memory))
        self.assertEqual(positioned[1][1], 4)


class SpriteRenderTests(unittest.TestCase):
    """Pass 1.2 — sprite raster: PR.C 4-level + flip + visibility."""

    def test_hidden_sprite_pr_c_zero_does_not_draw(self) -> None:
        memory: dict[int, int] = _new_memory()
        _set_tile_pixels(memory, 1, [(1,) * 8] * 8)
        _set_sprite_palette_color(memory, 0, 1, 0x000F)  # red
        _set_sprite(memory, 0, c_c=1, h_pos=10, v_pos=10, pr_c=0)
        frame = render_frame(memory)
        # Backdrop is black (cold-start) — sprite should NOT have drawn.
        for y in range(10, 18):
            for x in range(10, 18):
                pixel = frame.pixels[y][x]
                self.assertEqual((pixel.r, pixel.g, pixel.b), (0, 0, 0))

    def test_front_sprite_draws_at_h_v_pos(self) -> None:
        memory: dict[int, int] = _new_memory()
        _set_tile_pixels(memory, 1, [(1,) * 8] * 8)
        _set_sprite_palette_color(memory, 0, 1, 0x00F0)  # green
        _set_sprite(memory, 0, c_c=1, h_pos=20, v_pos=30, pr_c=3, cp_c=0)
        frame = render_frame(memory)
        # Sprite occupies pixels [20..27][30..37] (h,v offset by 8x8 tile size).
        for y in range(30, 38):
            for x in range(20, 28):
                pixel = frame.pixels[y][x]
                self.assertEqual((pixel.r, pixel.g, pixel.b), (0, 15, 0), msg=f"({x},{y})")
        # Outside the tile = backdrop (black cold-start).
        self.assertEqual(
            (frame.pixels[29][20].r, frame.pixels[29][20].g, frame.pixels[29][20].b),
            (0, 0, 0),
        )

    def test_sprite_palette_transparency(self) -> None:
        memory: dict[int, int] = _new_memory()
        _enable_red_backdrop(memory)
        # Checkerboard tile: alternating 1 / 0 per column.
        _set_tile_pixels(memory, 2, [(1, 0, 1, 0, 1, 0, 1, 0)] * 8)
        _set_sprite_palette_color(memory, 0, 1, 0x0F00)  # blue
        _set_sprite(memory, 0, c_c=2, h_pos=0, v_pos=0, pr_c=3)
        frame = render_frame(memory)
        # Even columns inside the 8×8 footprint → sprite blue.
        # Odd columns → palette transparent → backdrop red shows.
        for y in range(8):
            self.assertEqual(
                (frame.pixels[y][0].r, frame.pixels[y][0].g, frame.pixels[y][0].b),
                (0, 0, 15),
            )
            self.assertEqual(
                (frame.pixels[y][1].r, frame.pixels[y][1].g, frame.pixels[y][1].b),
                (15, 0, 0),
            )

    def test_sprite_h_flip(self) -> None:
        memory: dict[int, int] = _new_memory()
        rows: list[tuple[int, ...]] = [(0,) * 8 for _ in range(8)]
        rows[0] = (1, 0, 0, 0, 0, 0, 0, 0)
        _set_tile_pixels(memory, 3, rows)
        _set_sprite_palette_color(memory, 0, 1, 0x00F0)  # green
        _set_sprite(memory, 0, c_c=3, h_pos=0, v_pos=0, pr_c=3, h_flip=True)
        frame = render_frame(memory)
        # Dot was at column 0; with H.F it ends up at column 7 of the sprite.
        self.assertEqual(
            (frame.pixels[0][7].r, frame.pixels[0][7].g, frame.pixels[0][7].b),
            (0, 15, 0),
        )
        self.assertEqual(
            (frame.pixels[0][0].r, frame.pixels[0][0].g, frame.pixels[0][0].b),
            (0, 0, 0),
        )

    def test_sprite_v_flip(self) -> None:
        memory: dict[int, int] = _new_memory()
        rows: list[tuple[int, ...]] = [(0,) * 8 for _ in range(8)]
        rows[0] = (1,) * 8
        _set_tile_pixels(memory, 4, rows)
        _set_sprite_palette_color(memory, 0, 1, 0x000F)  # red
        _set_sprite(memory, 0, c_c=4, h_pos=0, v_pos=0, pr_c=3, v_flip=True)
        frame = render_frame(memory)
        # Solid row was at y=0 of the sprite; with V.F it ends up at y=7.
        for x in range(8):
            pixel = frame.pixels[7][x]
            self.assertEqual((pixel.r, pixel.g, pixel.b), (15, 0, 0), msg=f"x={x}")
        # Top row reverts to backdrop.
        self.assertEqual(
            (frame.pixels[0][0].r, frame.pixels[0][0].g, frame.pixels[0][0].b),
            (0, 0, 0),
        )

    def test_off_screen_sprite_does_not_draw(self) -> None:
        memory: dict[int, int] = _new_memory()
        _set_tile_pixels(memory, 5, [(1,) * 8] * 8)
        _set_sprite_palette_color(memory, 0, 1, 0x00F0)
        # H.P=200 puts the 8×8 sprite at x=200..207 — beyond 160 wide screen.
        _set_sprite(memory, 0, c_c=5, h_pos=200, v_pos=0, pr_c=3)
        frame = render_frame(memory)
        # No pixel anywhere should be green.
        for row in frame.pixels:
            for pixel in row:
                self.assertNotEqual((pixel.r, pixel.g, pixel.b), (0, 15, 0))

    def test_partially_off_screen_sprite_clips(self) -> None:
        memory: dict[int, int] = _new_memory()
        _set_tile_pixels(memory, 5, [(1,) * 8] * 8)
        _set_sprite_palette_color(memory, 0, 1, 0x00F0)  # green
        # H.P=156: sprite occupies x=156..163. Screen width is 160, so columns
        # 156..159 are visible (4 pixels) and 160..163 are clipped.
        _set_sprite(memory, 0, c_c=5, h_pos=156, v_pos=0, pr_c=3)
        frame = render_frame(memory)
        for x in range(156, 160):
            pixel = frame.pixels[0][x]
            self.assertEqual((pixel.r, pixel.g, pixel.b), (0, 15, 0), msg=f"x={x}")

    def test_pr_c_priority_behind_scr_back_covers_sprite(self) -> None:
        # SCR2 (back by default) has a solid tile at (0,0); a PR.C=01 sprite
        # placed at the same position should be COVERED by SCR2.
        memory: dict[int, int] = _new_memory()
        # Sprite tile and palette → green.
        _set_tile_pixels(memory, 1, [(1,) * 8] * 8)
        _set_sprite_palette_color(memory, 0, 1, 0x00F0)
        _set_sprite(memory, 0, c_c=1, h_pos=0, v_pos=0, pr_c=1, cp_c=0)
        # SCR2 tile and palette → blue.
        _set_tile_pixels(memory, 2, [(1,) * 8] * 8)
        _set_palette_color(memory, "scr2", 0, 1, 0x0F00)
        _set_tilemap_entry(memory, "scr2", 0, 0, 2, cp_c=0)
        frame = render_frame(memory)
        for y in range(8):
            for x in range(8):
                pixel = frame.pixels[y][x]
                # SCR2 blue covered the behind-sprite.
                self.assertEqual(
                    (pixel.r, pixel.g, pixel.b), (0, 0, 15), msg=f"({x},{y})"
                )

    def test_pr_c_priority_middle_covers_back_scr_under_front_scr(self) -> None:
        # Sprite PR.C=10 at (0,0); SCR2 (back) opaque red; SCR1 (front) opaque blue.
        # Compose: backdrop → SCR2 red → sprite green → SCR1 blue → result blue.
        memory: dict[int, int] = _new_memory()
        _set_tile_pixels(memory, 1, [(1,) * 8] * 8)
        _set_sprite_palette_color(memory, 0, 1, 0x00F0)  # green
        _set_sprite(memory, 0, c_c=1, h_pos=0, v_pos=0, pr_c=2, cp_c=0)
        _set_tile_pixels(memory, 2, [(1,) * 8] * 8)
        _set_palette_color(memory, "scr2", 0, 1, 0x000F)  # red
        _set_tilemap_entry(memory, "scr2", 0, 0, 2, cp_c=0)
        _set_tile_pixels(memory, 3, [(1,) * 8] * 8)
        _set_palette_color(memory, "scr1", 0, 1, 0x0F00)  # blue
        _set_tilemap_entry(memory, "scr1", 0, 0, 3, cp_c=0)
        frame = render_frame(memory)
        for y in range(8):
            for x in range(8):
                pixel = frame.pixels[y][x]
                self.assertEqual((pixel.r, pixel.g, pixel.b), (0, 0, 15))

    def test_pr_c_priority_middle_covers_back_scr_when_front_transparent(self) -> None:
        # Sprite PR.C=10 between SCR2 (back, opaque) and SCR1 (front, transparent).
        # Sprite should be visible — covers SCR2, SCR1 has no tile to cover it.
        memory: dict[int, int] = _new_memory()
        _set_tile_pixels(memory, 1, [(1,) * 8] * 8)
        _set_sprite_palette_color(memory, 0, 1, 0x00F0)  # green sprite
        _set_sprite(memory, 0, c_c=1, h_pos=0, v_pos=0, pr_c=2, cp_c=0)
        _set_tile_pixels(memory, 2, [(1,) * 8] * 8)
        _set_palette_color(memory, "scr2", 0, 1, 0x000F)  # red SCR2
        _set_tilemap_entry(memory, "scr2", 0, 0, 2, cp_c=0)
        # SCR1 left empty.
        frame = render_frame(memory)
        for y in range(8):
            for x in range(8):
                pixel = frame.pixels[y][x]
                self.assertEqual((pixel.r, pixel.g, pixel.b), (0, 15, 0))

    def test_pr_c_priority_front_always_on_top(self) -> None:
        # PR.C=11 sprite at (0,0); BOTH scroll planes opaque. Sprite wins.
        memory: dict[int, int] = _new_memory()
        _set_tile_pixels(memory, 1, [(1,) * 8] * 8)
        _set_sprite_palette_color(memory, 0, 1, 0x00F0)  # green
        _set_sprite(memory, 0, c_c=1, h_pos=0, v_pos=0, pr_c=3, cp_c=0)
        _set_tile_pixels(memory, 2, [(1,) * 8] * 8)
        _set_palette_color(memory, "scr2", 0, 1, 0x000F)
        _set_tilemap_entry(memory, "scr2", 0, 0, 2, cp_c=0)
        _set_tile_pixels(memory, 3, [(1,) * 8] * 8)
        _set_palette_color(memory, "scr1", 0, 1, 0x0F00)
        _set_tilemap_entry(memory, "scr1", 0, 0, 3, cp_c=0)
        frame = render_frame(memory)
        for y in range(8):
            for x in range(8):
                pixel = frame.pixels[y][x]
                self.assertEqual((pixel.r, pixel.g, pixel.b), (0, 15, 0))

    def test_chained_sprite_pair_renders_at_resolved_position(self) -> None:
        memory: dict[int, int] = _new_memory()
        # Two distinct tiles + two distinct sprite palettes.
        _set_tile_pixels(memory, 1, [(1,) * 8] * 8)
        _set_tile_pixels(memory, 2, [(1,) * 8] * 8)
        _set_sprite_palette_color(memory, 0, 1, 0x00F0)  # green
        _set_sprite_palette_color(memory, 1, 1, 0x0F00)  # blue
        # Anchor at (10, 20); chained H delta +8, absolute V=20 (v_chain off).
        _set_sprite(memory, 0, c_c=1, h_pos=10, v_pos=20, pr_c=3, cp_c=0)
        _set_sprite(memory, 1, c_c=2, h_pos=8, v_pos=20, pr_c=3, cp_c=1,
                    h_chain=True)
        frame = render_frame(memory)
        # Anchor green at x=10..17.
        self.assertEqual(
            (frame.pixels[20][10].r, frame.pixels[20][10].g, frame.pixels[20][10].b),
            (0, 15, 0),
        )
        # Chained sprite blue at x=18..25.
        self.assertEqual(
            (frame.pixels[20][18].r, frame.pixels[20][18].g, frame.pixels[20][18].b),
            (0, 0, 15),
        )
        # Gap-free composition: x=17 still green, x=18 already blue.
        self.assertEqual(
            (frame.pixels[20][17].r, frame.pixels[20][17].g, frame.pixels[20][17].b),
            (0, 15, 0),
        )

    def test_global_po_offset_shifts_all_sprites(self) -> None:
        memory: dict[int, int] = _new_memory()
        _set_tile_pixels(memory, 1, [(1,) * 8] * 8)
        _set_sprite_palette_color(memory, 0, 1, 0x000F)  # red
        _set_sprite(memory, 0, c_c=1, h_pos=0, v_pos=0, pr_c=3)
        memory[K2GE_REG_PO_H] = 5
        memory[K2GE_REG_PO_V] = 3
        frame = render_frame(memory)
        # Sprite should now appear at (5, 3)..(12, 10), not at (0, 0).
        self.assertEqual(
            (frame.pixels[3][5].r, frame.pixels[3][5].g, frame.pixels[3][5].b),
            (15, 0, 0),
        )
        # Original (0, 0) is now backdrop (black cold-start).
        self.assertEqual(
            (frame.pixels[0][0].r, frame.pixels[0][0].g, frame.pixels[0][0].b),
            (0, 0, 0),
        )


class RasterScrollTests(unittest.TestCase):
    """The scroll offset is latched PER LINE, not once per frame.

    K2GE Tech Ref, caution on 0x8032 (and again on 0x8030): *"The result of the value
    set in this register is displayed from the next line being drawn."* Games rewrite
    the offset while the beam runs -- to hold a HUD still over a scrolling field, or
    to fake parallax. This project's own engine ships `ngpc_raster_set_scroll_table`,
    so a renderer that samples the offset once a frame cannot draw the user's games.
    """

    @staticmethod
    def _raster_log(h_by_line: list[int]) -> tuple[bytes, ...]:
        """A 152-line log of the 0x8000..0x803F block, varying only S1SO.H."""
        lines = []
        for h in h_by_line:
            regs = bytearray(0x40)
            regs[0x8032 - 0x8000] = h          # S1SO.H
            lines.append(bytes(regs))
        return tuple(lines)

    def test_scroll_offset_can_differ_line_by_line(self) -> None:
        """Two halves of the screen, scrolled differently: the raster split."""
        memory: dict[int, int] = _new_memory()
        _set_tile_pixels(memory, 1, [(1,) * 8] * 8)
        _set_palette_color(memory, "scr1", 0, 1, 0x00F0)     # green
        # One tile at column 0. With H offset 0 it sits at x=0..7; with H offset 8
        # the plane shifts left by one tile and column 0 lands at x=248 -- off screen.
        _set_tilemap_entry(memory, "scr1", 0, 0, 1, cp_c=0)
        for row in range(1, 32):
            _set_tilemap_entry(memory, "scr1", 0, row, 1, cp_c=0)

        # Top half unscrolled, bottom half scrolled by 8: the tile column vanishes.
        log = self._raster_log([0] * 76 + [8] * 76)
        frame = render_frame(memory, log)

        top = frame.pixels[10][2]
        bottom = frame.pixels[100][2]
        self.assertEqual((top.r, top.g, top.b), (0, 15, 0), "top half must be drawn")
        self.assertEqual(
            (bottom.r, bottom.g, bottom.b), (0, 0, 0),
            "bottom half is scrolled by a whole tile -- if it still shows the tile, "
            "the renderer is applying ONE offset to the whole frame again",
        )

    def test_no_raster_log_keeps_the_single_snapshot(self) -> None:
        """Without a log there is no per-line history: nothing may change."""
        memory: dict[int, int] = _new_memory()
        _set_tile_pixels(memory, 1, [(1,) * 8] * 8)
        _set_palette_color(memory, "scr1", 0, 1, 0x00F0)
        _set_tilemap_entry(memory, "scr1", 0, 0, 1, cp_c=0)
        frame = render_frame(memory)
        pixel = frame.pixels[3][3]
        self.assertEqual((pixel.r, pixel.g, pixel.b), (0, 15, 0))


class SpriteEdgeWrapTests(unittest.TestCase):
    """A sprite's position is 8 BITS, so it WRAPS -- it can hang off the top edge.

    The MANUFACTURER says so outright -- K2GE Tech Ref § 3-1:

        VIRTUAL DISPLAY AREA : 256 x 256 [dot]  CYCLICAL STRUCTURE
        DISPLAY AREA         : 160 x 152 [dot]

    So a sprite at y = 250 occupies rows 250..255 and then 0..1, and those last two
    rows are INSIDE the display area. The renderer used to compute `250 + py`, find
    it past the bottom of a 152-line screen, and drop the sprite entirely: every
    sprite entering from the top or the left edge simply vanished -- on Sonic, 102
    pixels of a 24 320-pixel frame, a whole item box gone from the top of the screen.

    A third-party emulator was what made the missing box VISIBLE, and it happens to
    agree. It is not the source: the cyclical world is.
    """

    def test_sprite_hanging_off_the_top_shows_its_bottom_rows(self) -> None:
        memory: dict[int, int] = _new_memory()
        _set_tile_pixels(memory, 1, [(1,) * 8] * 8)
        _set_sprite_palette_color(memory, 0, 1, 0x000F)          # red
        _set_sprite(memory, 0, c_c=1, h_pos=20, v_pos=250, pr_c=3)

        frame = render_frame(memory)
        # rows 250..255 are off-screen; py = 6 and 7 land on screen rows 0 and 1.
        for y in (0, 1):
            pixel = frame.pixels[y][22]
            self.assertEqual(
                (pixel.r, pixel.g, pixel.b), (15, 0, 0),
                f"row {y} must show the sprite's bottom edge -- without the 8-bit "
                f"wrap the whole sprite disappears",
            )
        self.assertEqual(
            (frame.pixels[2][22].r, frame.pixels[2][22].g, frame.pixels[2][22].b),
            (0, 0, 0), "row 2 is past the sprite: it must stay backdrop",
        )

    def test_sprite_hanging_off_the_left_shows_its_right_columns(self) -> None:
        memory: dict[int, int] = _new_memory()
        _set_tile_pixels(memory, 1, [(1,) * 8] * 8)
        _set_sprite_palette_color(memory, 0, 1, 0x00F0)          # green
        _set_sprite(memory, 0, c_c=1, h_pos=252, v_pos=30, pr_c=3)

        frame = render_frame(memory)
        for x in (0, 1, 2, 3):                                   # px = 4..7 wrap to 0..3
            pixel = frame.pixels[32][x]
            self.assertEqual((pixel.r, pixel.g, pixel.b), (0, 15, 0), f"x={x}")
        self.assertEqual(
            (frame.pixels[32][4].r, frame.pixels[32][4].g, frame.pixels[32][4].b),
            (0, 0, 0), "x=4 is past the sprite's wrapped edge",
        )


class SpriteVsSpritePriorityTests(unittest.TestCase):
    """Which sprite wins a pixel two sprites both cover?

    K2GE Tech Ref § 4-3-3-1: the chip fills its line buffer starting from "the
    VRAM 0 address" and checks priority "to avoid writing over previously written
    data" — so the LOWEST OAM index wins, and sprite 0 is the topmost sprite.

    The renderer used to iterate 0..63 letting each sprite overwrite the last,
    i.e. the exact opposite, and NOT ONE TEST NOTICED: every sprite test drew a
    single sprite. On a Sonic gameplay frame that error decided 399 contested
    pixels, all of them wrongly.
    """

    def test_lower_oam_index_wins_a_contested_pixel(self) -> None:
        memory: dict[int, int] = _new_memory()
        _set_tile_pixels(memory, 1, [(1,) * 8] * 8)
        _set_sprite_palette_color(memory, 0, 1, 0x000F)  # palette 0 -> red
        _set_sprite_palette_color(memory, 1, 1, 0x00F0)  # palette 1 -> green
        # Both cover (20, 20). Sprite 0 is red, sprite 1 is green.
        _set_sprite(memory, 0, c_c=1, h_pos=20, v_pos=20, pr_c=3, cp_c=0)
        _set_sprite(memory, 1, c_c=1, h_pos=20, v_pos=20, pr_c=3, cp_c=1)

        pixel = render_frame(memory).pixels[22][22]
        self.assertEqual(
            (pixel.r, pixel.g, pixel.b), (15, 0, 0),
            "sprite 0 must win the pixel: the chip writes it first and refuses "
            "to overwrite it. Green here means the OAM order is inverted again.",
        )

    def test_a_transparent_pixel_claims_nothing(self) -> None:
        """A hole in the top sprite lets the one underneath show through.

        Palette index 0 is transparent, so it must not take ownership of the
        pixel -- otherwise every sprite would punch a rectangular hole in the
        sprites behind it instead of overlapping cleanly.
        """
        memory: dict[int, int] = _new_memory()
        # Tile 1: solid. Tile 2: entirely transparent (palette index 0).
        _set_tile_pixels(memory, 1, [(1,) * 8] * 8)
        _set_tile_pixels(memory, 2, [(0,) * 8] * 8)
        _set_sprite_palette_color(memory, 1, 1, 0x00F0)  # green
        _set_sprite(memory, 0, c_c=2, h_pos=20, v_pos=20, pr_c=3, cp_c=0)  # hole
        _set_sprite(memory, 1, c_c=1, h_pos=20, v_pos=20, pr_c=3, cp_c=1)  # green

        pixel = render_frame(memory).pixels[22][22]
        self.assertEqual((pixel.r, pixel.g, pixel.b), (0, 15, 0))

    def test_lower_index_wins_even_against_a_nearer_priority(self) -> None:
        """One line buffer, not one per PR.C.

        Sprite 0 is "furthest" (PR.C=01) and sprite 1 is "front" (PR.C=11), and
        they overlap. The chip has a SINGLE sprite line buffer -- that is the whole
        premise of the Character Over section -- so sprite 0 claims the pixel first
        and sprite 1 never gets it, no matter how near its priority. PR.C then only
        decides where that pixel sits against the scroll planes; here no plane is
        drawn, so sprite 0's colour reaches the screen.

        (§ 4-3-3-1's Figure 3 is an image and not in the extractable text, so this
        cross-priority case is read from the single-line-buffer description, not
        from the figure. It is stated here so a HW test can contradict it.)
        """
        memory: dict[int, int] = _new_memory()
        _set_tile_pixels(memory, 1, [(1,) * 8] * 8)
        _set_sprite_palette_color(memory, 0, 1, 0x000F)  # red
        _set_sprite_palette_color(memory, 1, 1, 0x00F0)  # green
        _set_sprite(memory, 0, c_c=1, h_pos=20, v_pos=20, pr_c=1, cp_c=0)  # furthest
        _set_sprite(memory, 1, c_c=1, h_pos=20, v_pos=20, pr_c=3, cp_c=1)  # front

        pixel = render_frame(memory).pixels[22][22]
        self.assertEqual((pixel.r, pixel.g, pixel.b), (15, 0, 0))


class WindowClipAndNegTests(unittest.TestCase):
    """Pass 1.3 — window clip OOWC + NEG invert post-process passes."""

    def test_cold_start_via_load_read_bus_has_full_window(self) -> None:
        # The cold-start image pre-populates WSI.H/V = 0xFF (HW reset
        # value). Verify the decoder reads that back.
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            _write_demo_rom(rom_path)
            machine = load_machine_state(rom_path)
            from core.memory import load_read_bus
            bus = load_read_bus(rom_path)
            memory = dict(bus.builtin_bytes)
            ctrl = read_control_registers(memory)
            self.assertEqual(ctrl.wsi_h, 0xFF)
            self.assertEqual(ctrl.wsi_v, 0xFF)
            # And the REF reset value too.
            self.assertEqual(memory[0x008006], 0xC6)

    def test_wsi_zero_clips_entire_screen_to_oowc_color(self) -> None:
        # Tile drawn at (0, 0) with a solid green palette;
        # WSI = 0 makes the window empty → every pixel becomes OOWC.
        memory: dict[int, int] = {}  # WSI defaults to 0 — total clip.
        _set_tile_pixels(memory, 1, [(1,) * 8] * 8)
        _set_palette_color(memory, "scr1", 0, 1, 0x00F0)  # green tile
        _set_tilemap_entry(memory, "scr1", 0, 0, 1, cp_c=0)
        # OOWC = 0 indexes WINDOW slot 0 (0x83F0, NOT the backdrop block --
        # Fatal Fury's black letterbox convicted the shared-block reading).
        memory[K2GE_PALETTE_WINDOW_COLORS_BASE] = 0x0F  # low: GGGG RRRR -> r=15
        memory[K2GE_PALETTE_WINDOW_COLORS_BASE + 1] = 0x00
        frame = render_frame(memory)
        # The green tile that DID compose at (0..7, 0..7) is overwritten
        # by OOWC red — every pixel ends up red.
        for row in frame.pixels:
            for pixel in row:
                self.assertEqual((pixel.r, pixel.g, pixel.b), (15, 0, 0))

    def test_partial_window_keeps_inside_pixels_clips_outside(self) -> None:
        memory = _new_memory()
        # Restrict window to [0..63, 0..63] (64×64 top-left region).
        memory[K2GE_REG_WSI_H] = 64
        memory[K2GE_REG_WSI_V] = 64
        # Solid green tile filling the whole top-left 64×64.
        _set_tile_pixels(memory, 1, [(1,) * 8] * 8)
        _set_palette_color(memory, "scr1", 0, 1, 0x00F0)
        for ty in range(8):
            for tx in range(8):
                _set_tilemap_entry(memory, "scr1", tx, ty, 1, cp_c=0)
        # OOWC = 0 -> WINDOW slot 0 = red.
        memory[K2GE_PALETTE_WINDOW_COLORS_BASE] = 0x0F
        memory[K2GE_PALETTE_WINDOW_COLORS_BASE + 1] = 0x00
        frame = render_frame(memory)
        # Inside window: green (tile rendered).
        self.assertEqual(
            (frame.pixels[10][10].r, frame.pixels[10][10].g, frame.pixels[10][10].b),
            (0, 15, 0),
        )
        # Outside window: OOWC red.
        self.assertEqual(
            (frame.pixels[100][100].r, frame.pixels[100][100].g, frame.pixels[100][100].b),
            (15, 0, 0),
        )
        # Last in-window pixel (window is [0..64[) — x=63 still inside.
        self.assertEqual(
            (frame.pixels[10][63].r, frame.pixels[10][63].g, frame.pixels[10][63].b),
            (0, 15, 0),
        )
        # First out-of-window pixel: x=64 (== x_max, half-open).
        self.assertEqual(
            (frame.pixels[10][64].r, frame.pixels[10][64].g, frame.pixels[10][64].b),
            (15, 0, 0),
        )
        # Same on the Y axis: row 63 inside, row 64 outside.
        self.assertEqual(
            (frame.pixels[63][10].r, frame.pixels[63][10].g, frame.pixels[63][10].b),
            (0, 15, 0),
        )
        self.assertEqual(
            (frame.pixels[64][10].r, frame.pixels[64][10].g, frame.pixels[64][10].b),
            (15, 0, 0),
        )

    def test_window_offset_via_wba(self) -> None:
        memory = _new_memory()
        # Window of size 16×16 centred away from origin: WBA=(40,30), WSI=(16,16).
        memory[K2GE_REG_WBA_H] = 40
        memory[K2GE_REG_WBA_V] = 30
        memory[K2GE_REG_WSI_H] = 16
        memory[K2GE_REG_WSI_V] = 16
        # OOWC slot 1 -> WINDOW block [1] = blue (backdrop block stays red:
        # the two blocks differing is exactly what this pins).
        memory[K2GE_REG_2D_CONTROL] = 0x01  # bits 2..0 = 1, NEG bit = 0
        memory[K2GE_PALETTE_WINDOW_COLORS_BASE + 2] = 0x00
        memory[K2GE_PALETTE_WINDOW_COLORS_BASE + 3] = 0x0F
        # Backdrop enabled, slot 0 = red.
        memory[K2GE_REG_BGC] = 0x80
        memory[K2GE_PALETTE_BG_COLORS_BASE] = 0x0F
        memory[K2GE_PALETTE_BG_COLORS_BASE + 1] = 0x00
        frame = render_frame(memory)
        # Inside the window (40..55, 30..45): backdrop red survives.
        self.assertEqual(
            (frame.pixels[35][45].r, frame.pixels[35][45].g, frame.pixels[35][45].b),
            (15, 0, 0),
        )
        # Outside: OOWC blue.
        self.assertEqual(
            (frame.pixels[0][0].r, frame.pixels[0][0].g, frame.pixels[0][0].b),
            (0, 0, 15),
        )
        # Boundary checks (half-open):
        self.assertEqual(
            (frame.pixels[30][39].r, frame.pixels[30][39].g, frame.pixels[30][39].b),
            (0, 0, 15),  # x=39 < x_min=40 → OOWC
        )
        self.assertEqual(
            (frame.pixels[30][40].r, frame.pixels[30][40].g, frame.pixels[30][40].b),
            (15, 0, 0),  # x=40 == x_min → inside
        )
        self.assertEqual(
            (frame.pixels[30][55].r, frame.pixels[30][55].g, frame.pixels[30][55].b),
            (15, 0, 0),  # x=55 < x_max=56 → inside
        )
        self.assertEqual(
            (frame.pixels[30][56].r, frame.pixels[30][56].g, frame.pixels[30][56].b),
            (0, 0, 15),  # x=56 == x_max → outside
        )

    def test_oowc_color_resolves_through_the_window_block(self) -> None:
        # OOWC reads 0x83F0 (HW_PAL_WIN), NOT the backdrop block at 0x83E0.
        # Fatal Fury's intro is the conviction: white-filled backdrop block,
        # grey ramp in the window block with entry 7 = BLACK, OOWC = 7 -- the
        # game wants a black letterbox, and one shared block painted it white.
        memory = _new_memory()
        memory[K2GE_REG_2D_CONTROL] = 0x05  # bits 2..0 = 5, NEG bit = 0
        # DISCRIMINATOR: the same slot differs between the two blocks.
        bg_slot = K2GE_PALETTE_BG_COLORS_BASE + 5 * 2
        memory[bg_slot] = 0x0F          # backdrop slot 5 = red (the DECOY)
        memory[bg_slot + 1] = 0x00
        win_slot = K2GE_PALETTE_WINDOW_COLORS_BASE + 5 * 2
        memory[win_slot] = 0x00         # window slot 5 = blue (the answer)
        memory[win_slot + 1] = 0x0F
        ctrl = read_control_registers(memory)
        oowc = resolve_oowc_color(memory, ctrl)
        self.assertEqual((oowc.r, oowc.g, oowc.b), (0, 0, 15))

    def test_backdrop_shows_even_when_the_enable_bits_are_clear(self) -> None:
        # The Tech Ref says D7=1/D6=0 is required and everything else is black;
        # real games disagree. Ogre Battle Gaiden writes a blue into 0x83E0[0],
        # sets BGC = 0x00, and expects a blue sky. The backdrop is the palette entry,
        # unconditionally; a game that wants black leaves the entry black.
        memory = _new_memory()
        memory[K2GE_REG_BGC] = 0x00  # D7 clear -> "disabled" under the old rule
        memory[K2GE_PALETTE_BG_COLORS_BASE] = 0x30      # 0x0830 = blue
        memory[K2GE_PALETTE_BG_COLORS_BASE + 1] = 0x08
        frame = render_frame(memory)
        self.assertEqual(
            (frame.pixels[76][80].r, frame.pixels[76][80].g, frame.pixels[76][80].b),
            (0, 3, 8),
            "a backdrop palette written by the game must show even with BGC bit7=0",
        )
        # And black really is just a black palette entry, not the enable bit:
        memory[K2GE_PALETTE_BG_COLORS_BASE] = 0x00
        memory[K2GE_PALETTE_BG_COLORS_BASE + 1] = 0x00
        frame = render_frame(memory)
        self.assertEqual(
            (frame.pixels[76][80].r, frame.pixels[76][80].g, frame.pixels[76][80].b),
            (0, 0, 0),
        )

    def test_neg_invert_flips_all_components(self) -> None:
        memory = _new_memory()
        # Backdrop enabled, slot 0 = red (r=15, g=0, b=0).
        memory[K2GE_REG_BGC] = 0x80
        memory[K2GE_PALETTE_BG_COLORS_BASE] = 0x0F
        memory[K2GE_PALETTE_BG_COLORS_BASE + 1] = 0x00
        # NEG = 1.
        memory[K2GE_REG_2D_CONTROL] = 0x80
        frame = render_frame(memory)
        # Backdrop red (15, 0, 0) inverted to (0, 15, 15) = cyan.
        for row in frame.pixels:
            for pixel in row:
                self.assertEqual((pixel.r, pixel.g, pixel.b), (0, 15, 15))

    def test_neg_applies_to_oowc_too(self) -> None:
        memory: dict[int, int] = {}  # WSI=0 → all OOWC + NEG inverts that too.
        # OOWC = 0, WINDOW slot 0 = red.
        memory[K2GE_PALETTE_WINDOW_COLORS_BASE] = 0x0F
        memory[K2GE_PALETTE_WINDOW_COLORS_BASE + 1] = 0x00
        # NEG = 1.
        memory[K2GE_REG_2D_CONTROL] = 0x80
        frame = render_frame(memory)
        # OOWC red (15, 0, 0) inverted to cyan (0, 15, 15).
        for row in frame.pixels:
            for pixel in row:
                self.assertEqual((pixel.r, pixel.g, pixel.b), (0, 15, 15))

    def test_neg_clear_is_a_no_op(self) -> None:
        memory = _new_memory()
        memory[K2GE_REG_BGC] = 0x80
        memory[K2GE_PALETTE_BG_COLORS_BASE] = 0x0F
        memory[K2GE_PALETTE_BG_COLORS_BASE + 1] = 0x00
        # NEG = 0 explicitly.
        memory[K2GE_REG_2D_CONTROL] = 0x00
        frame = render_frame(memory)
        # Red stays red.
        self.assertEqual(
            (frame.pixels[0][0].r, frame.pixels[0][0].g, frame.pixels[0][0].b),
            (15, 0, 0),
        )

    def test_neg_recomputes_raw_field_consistently(self) -> None:
        memory = _new_memory()
        # Backdrop = arbitrary 0x078A (r=10, g=8, b=7).
        memory[K2GE_REG_BGC] = 0x80
        memory[K2GE_PALETTE_BG_COLORS_BASE] = 0x8A      # GGGG RRRR
        memory[K2GE_PALETTE_BG_COLORS_BASE + 1] = 0x07  # 0000 BBBB
        memory[K2GE_REG_2D_CONTROL] = 0x80  # NEG on
        frame = render_frame(memory)
        # 10 → 5, 8 → 7, 7 → 8. Raw inverted = (8 << 8) | (7 << 4) | 5 = 0x0875.
        pixel = frame.pixels[0][0]
        self.assertEqual((pixel.r, pixel.g, pixel.b), (5, 7, 8))
        self.assertEqual(pixel.raw, 0x0875)


class PpmSerializationTests(unittest.TestCase):
    def test_header_then_body_byte_layout(self) -> None:
        frame = render_frame({})
        data = frame_to_ppm_bytes(frame)
        self.assertTrue(data.startswith(b"P6\n160 152\n255\n"))
        body = data[len(b"P6\n160 152\n255\n"):]
        # Body is exactly width × height × 3 bytes (RGB888).
        self.assertEqual(len(body), 160 * 152 * 3)
        # Cold-start backdrop is black → entire body is zero.
        self.assertEqual(body, bytes(160 * 152 * 3))

    def test_nibble_replication_of_4bit_components(self) -> None:
        # Render a fully-saturated red frame (r=15, g=0, b=0). WSI must be
        # opened (a raw dict means WSI=0 = the whole screen is OOWC-clipped;
        # this used to pass only because OOWC read the same block as BGC).
        slot_base = K2GE_PALETTE_BG_COLORS_BASE + 0
        memory = {
            K2GE_REG_BGC: 0x80,
            slot_base: 0x0F,
            slot_base + 1: 0x00,
            K2GE_REG_WSI_H: 0xFF,
            K2GE_REG_WSI_V: 0xFF,
        }
        frame = render_frame(memory)
        data = frame_to_ppm_bytes(frame)
        body = data[len(b"P6\n160 152\n255\n"):]
        # Every pixel should be R=0xFF (nibble replication of 0xF), G=B=0.
        for i in range(0, len(body), 3):
            self.assertEqual(body[i], 0xFF)
            self.assertEqual(body[i + 1], 0x00)
            self.assertEqual(body[i + 2], 0x00)


class ScreenshotCliTests(unittest.TestCase):
    def test_cli_writes_ppm_file_and_prints_summary(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            rom_path = tmp / "demo.ngc"
            _write_demo_rom(rom_path)
            output = tmp / "out.ppm"

            stdout = io.StringIO()
            with redirect_stdout(stdout):
                exit_code = main(
                    ["screenshot", str(rom_path), "--output", str(output)],
                )
            self.assertEqual(exit_code, 0)
            self.assertTrue(output.exists())
            data = output.read_bytes()
            self.assertTrue(data.startswith(b"P6\n160 152\n255\n"))
            text = stdout.getvalue()
            self.assertIn("Frame: 160", text)
            # Power-on K2GE state (2026-07-10): BGC is ON (0x8118 = 0x80) and
            # the default backdrop colour is set (0x83E0/E1 = 0x0FFF), so a
            # freshly-reset console is NOT a black screen with BGC off.
            self.assertIn("Backdrop: #FFFFFF", text)
            self.assertIn("bgc_enabled=True", text)

    def test_cli_json_payload_shape(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            rom_path = tmp / "demo.ngc"
            _write_demo_rom(rom_path)
            output = tmp / "out.ppm"

            stdout = io.StringIO()
            with redirect_stdout(stdout):
                exit_code = main(
                    [
                        "screenshot", str(rom_path),
                        "--output", str(output),
                        "--json",
                    ],
                )
            self.assertEqual(exit_code, 0)
            payload = json.loads(stdout.getvalue())
            self.assertEqual(payload["width"], NGPC_SCREEN_WIDTH)
            self.assertEqual(payload["height"], NGPC_SCREEN_HEIGHT)
            self.assertEqual(payload["renderer_pass"], "1.3")
            self.assertEqual(payload["backdrop_color"]["hex_rgb24"], "#FFFFFF")
            # Power-on K2GE state (2026-07-10): BGC is ON out of reset
            # (0x8118 = 0x80) -- it does not power on disabled.
            self.assertTrue(payload["control"]["backdrop_control"]["bgc_enabled"])
            self.assertEqual(payload["control"]["backdrop_control"]["bgc_raw_hex"], "0x80")
            self.assertEqual(
                payload["ppm_byte_count"],
                len(b"P6\n160 152\n255\n") + NGPC_SCREEN_WIDTH * NGPC_SCREEN_HEIGHT * 3,
            )

    def test_cli_seed_from_overlay_renders_backdrop_color(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            rom_path = tmp / "demo.ngc"
            _write_demo_rom(rom_path)
            machine = load_machine_state(rom_path)

            # Enable BGC with index 1 → backdrop slot 1 = green.
            slot_base = K2GE_PALETTE_BG_COLORS_BASE + 1 * 2
            overlay = {
                K2GE_REG_BGC: 0x81,
                slot_base: 0xF0,          # low: GGGG RRRR = 0xF0 → g=15
                slot_base + 1: 0x00,
            }
            state_path = tmp / "demo_state.json"
            save_savestate(
                state_path,
                build_savestate_payload(
                    rom_path=rom_path,
                    rom_header=machine.header,
                    cpu=machine.cpu,
                    writable_overlay=overlay,
                ),
            )

            output = tmp / "out.ppm"
            stdout = io.StringIO()
            with redirect_stdout(stdout):
                exit_code = main(
                    [
                        "screenshot", str(rom_path),
                        "--seed-from", str(state_path),
                        "--output", str(output),
                        "--json",
                    ],
                )
            self.assertEqual(exit_code, 0)
            payload = json.loads(stdout.getvalue())
            self.assertEqual(payload["backdrop_color"]["hex_rgb24"], "#00FF00")
            self.assertEqual(payload["control"]["backdrop_control"]["bgc_index"], 1)
            self.assertTrue(payload["control"]["backdrop_control"]["bgc_enabled"])
            self.assertEqual(payload["seed_from"], str(state_path))

            # File body should be 160×152 green pixels.
            data = output.read_bytes()
            body = data[len(b"P6\n160 152\n255\n"):]
            self.assertEqual(len(body), NGPC_SCREEN_WIDTH * NGPC_SCREEN_HEIGHT * 3)
            for i in range(0, len(body), 3):
                self.assertEqual(body[i], 0x00)
                self.assertEqual(body[i + 1], 0xFF)
                self.assertEqual(body[i + 2], 0x00)

    def test_cli_defaults_to_screenshot_ppm_in_cwd(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            rom_path = tmp / "demo.ngc"
            _write_demo_rom(rom_path)

            # Run inside tmp so the default ./screenshot.ppm lands there.
            import os
            old_cwd = os.getcwd()
            os.chdir(tmp)
            try:
                with redirect_stdout(io.StringIO()):
                    exit_code = main(["screenshot", str(rom_path)])
                self.assertEqual(exit_code, 0)
                default = tmp / "screenshot.ppm"
                self.assertTrue(default.exists())
                self.assertTrue(default.read_bytes().startswith(b"P6\n"))
            finally:
                os.chdir(old_cwd)


if __name__ == "__main__":
    unittest.main()
