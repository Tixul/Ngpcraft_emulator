"""CHAR_RAM tile-atlas renderer + `tiles-view` CLI tests (pass 24)."""

from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from core.atlas import GRAYSCALE_COLOR_TABLE, render_tile_atlas
from core.k2ge import (
    CHAR_RAM_TILE_COUNT,
    K2GE_CHAR_RAM_BASE,
    K2GE_PALETTE_SCR1_BASE,
    read_plane_palettes,
)
from core.machine import load_machine_state
from core.renderer import pixels_to_ppm_bytes
from core.savestate import build_savestate_payload, save_savestate
from ngpc_emu import main


def _write_demo_rom(path: Path, entry_point: int = 0x00200040) -> None:
    data = bytearray(0x40)
    data[0x00:0x1C] = b"LICENSED BY SNK CORPORATION".ljust(0x1C, b"\x00")
    data[0x1C:0x20] = entry_point.to_bytes(4, "little")
    data[0x22] = 0
    data[0x23] = 0x10
    data[0x24:0x30] = b"TILE ATLAS\x00\x00"
    path.write_bytes(bytes(data))


def _set_tile_pixels(
    memory: dict[int, int],
    tile_id: int,
    rows: list[tuple[int, ...]],
) -> None:
    """Store an 8×8 tile (`rows` = 8 tuples of 8 ints 0..3) in CHAR_RAM."""
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


def _set_scr1_palette_color(
    memory: dict[int, int], palette_index: int, color_index: int, raw_0bgr: int,
) -> None:
    base = K2GE_PALETTE_SCR1_BASE + palette_index * 8 + color_index * 2
    memory[base] = raw_0bgr & 0xFF
    memory[base + 1] = (raw_0bgr >> 8) & 0xFF


class TileAtlasGeometryTests(unittest.TestCase):
    def test_default_full_atlas_dimensions(self) -> None:
        width, height, pixels = render_tile_atlas(
            {}, list(range(CHAR_RAM_TILE_COUNT)), cols=16,
        )
        # 16 cols × 8 px = 128 wide ; 32 rows × 8 px = 256 tall.
        self.assertEqual((width, height), (128, 256))
        self.assertEqual(len(pixels), 256)
        self.assertEqual(len(pixels[0]), 128)

    def test_partial_range_exact_fit(self) -> None:
        width, height, pixels = render_tile_atlas({}, [0, 1, 2, 3], cols=2)
        self.assertEqual((width, height), (16, 16))

    def test_partial_row_pads_last_row_with_black(self) -> None:
        # 3 tiles in 2 cols → 2 rows, last cell (col=1, row=1) unused.
        width, height, pixels = render_tile_atlas({}, [0, 1, 2], cols=2)
        self.assertEqual((width, height), (16, 16))
        # Unused slot at (col=1, row=1) = pixels [8..15, 8..15] = black.
        for y in range(8, 16):
            for x in range(8, 16):
                pixel = pixels[y][x]
                self.assertEqual((pixel.r, pixel.g, pixel.b), (0, 0, 0))

    def test_single_tile_atlas_is_one_tile_wide(self) -> None:
        width, height, _ = render_tile_atlas({}, [0], cols=1)
        self.assertEqual((width, height), (8, 8))

    def test_zero_cols_raises(self) -> None:
        with self.assertRaises(ValueError):
            render_tile_atlas({}, [0], cols=0)

    def test_invalid_tile_id_raises(self) -> None:
        with self.assertRaises(ValueError):
            render_tile_atlas({}, [-1], cols=1)
        with self.assertRaises(ValueError):
            render_tile_atlas({}, [CHAR_RAM_TILE_COUNT], cols=1)


class TileAtlasColorTests(unittest.TestCase):
    def test_grayscale_color_table_maps_4_levels(self) -> None:
        # Default grayscale table: 0→black, 1→0x55, 2→0xAA, 3→0xFF
        # (4-bit nibbles 0/5/10/15 nibble-replicated).
        self.assertEqual(GRAYSCALE_COLOR_TABLE[0].r, 0)
        self.assertEqual(GRAYSCALE_COLOR_TABLE[1].r, 5)
        self.assertEqual(GRAYSCALE_COLOR_TABLE[2].r, 10)
        self.assertEqual(GRAYSCALE_COLOR_TABLE[3].r, 15)

    def test_grayscale_pixel_resolves_each_value(self) -> None:
        memory: dict[int, int] = {}
        # Row 0: all four 2-bit codes (0,1,2,3 each replicated twice across 8 pixels).
        _set_tile_pixels(memory, 0, [(0, 0, 1, 1, 2, 2, 3, 3)] + [(0,) * 8] * 7)
        _, _, pixels = render_tile_atlas(memory, [0], cols=1)
        self.assertEqual(pixels[0][0].raw, GRAYSCALE_COLOR_TABLE[0].raw)
        self.assertEqual(pixels[0][2].raw, GRAYSCALE_COLOR_TABLE[1].raw)
        self.assertEqual(pixels[0][4].raw, GRAYSCALE_COLOR_TABLE[2].raw)
        self.assertEqual(pixels[0][6].raw, GRAYSCALE_COLOR_TABLE[3].raw)

    def test_palette_colorisation_overrides_grayscale(self) -> None:
        memory: dict[int, int] = {}
        # SCR1 palette 0 with three distinct foreground colors.
        _set_scr1_palette_color(memory, 0, 1, 0x000F)   # red
        _set_scr1_palette_color(memory, 0, 2, 0x00F0)   # green
        _set_scr1_palette_color(memory, 0, 3, 0x0F00)   # blue
        _set_tile_pixels(memory, 0, [(1, 2, 3, 0, 0, 0, 0, 0)] + [(0,) * 8] * 7)
        palettes = read_plane_palettes(memory, K2GE_PALETTE_SCR1_BASE, "scr1")
        _, _, pixels = render_tile_atlas(memory, [0], cols=1, palette=palettes[0])
        self.assertEqual((pixels[0][0].r, pixels[0][0].g, pixels[0][0].b), (15, 0, 0))
        self.assertEqual((pixels[0][1].r, pixels[0][1].g, pixels[0][1].b), (0, 15, 0))
        self.assertEqual((pixels[0][2].r, pixels[0][2].g, pixels[0][2].b), (0, 0, 15))

    def test_tiles_placed_at_correct_grid_position(self) -> None:
        memory: dict[int, int] = {}
        _set_tile_pixels(memory, 0, [(3,) * 8] * 8)  # all white
        _set_tile_pixels(memory, 1, [(1,) * 8] * 8)  # all light gray
        _, _, pixels = render_tile_atlas(memory, [0, 1], cols=2)
        # Tile 0 occupies cols 0..7, tile 1 occupies cols 8..15.
        self.assertEqual(pixels[0][0].raw, GRAYSCALE_COLOR_TABLE[3].raw)
        self.assertEqual(pixels[0][7].raw, GRAYSCALE_COLOR_TABLE[3].raw)
        self.assertEqual(pixels[0][8].raw, GRAYSCALE_COLOR_TABLE[1].raw)
        self.assertEqual(pixels[0][15].raw, GRAYSCALE_COLOR_TABLE[1].raw)

    def test_repeated_tile_id_uses_cache(self) -> None:
        # Just verify it doesn't crash and renders the same tile twice.
        memory: dict[int, int] = {}
        _set_tile_pixels(memory, 5, [(3,) * 8] * 8)
        _, _, pixels = render_tile_atlas(memory, [5, 5], cols=2)
        # Both grid cells should be white.
        self.assertEqual(pixels[0][0].raw, GRAYSCALE_COLOR_TABLE[3].raw)
        self.assertEqual(pixels[0][8].raw, GRAYSCALE_COLOR_TABLE[3].raw)


class TilesViewCliTests(unittest.TestCase):
    def test_default_full_atlas_writes_ppm(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            rom_path = tmp / "demo.ngc"
            _write_demo_rom(rom_path)
            output = tmp / "atlas.ppm"

            stdout = io.StringIO()
            with redirect_stdout(stdout):
                exit_code = main(
                    ["tiles-view", str(rom_path), "--output", str(output)],
                )
            self.assertEqual(exit_code, 0)
            self.assertTrue(output.exists())
            data = output.read_bytes()
            self.assertTrue(data.startswith(b"P6\n128 256\n255\n"))
            # Cold-start CHAR_RAM is all zero → entire atlas is black.
            body = data[len(b"P6\n128 256\n255\n"):]
            self.assertEqual(body, bytes(128 * 256 * 3))

    def test_partial_range_with_cols(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            rom_path = tmp / "demo.ngc"
            _write_demo_rom(rom_path)
            output = tmp / "atlas.ppm"

            with redirect_stdout(io.StringIO()):
                exit_code = main(
                    [
                        "tiles-view", str(rom_path),
                        "--range", "0..3",
                        "--cols", "2",
                        "--output", str(output),
                    ],
                )
            self.assertEqual(exit_code, 0)
            data = output.read_bytes()
            self.assertTrue(data.startswith(b"P6\n16 16\n255\n"))

    def test_json_payload_shape(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            rom_path = tmp / "demo.ngc"
            _write_demo_rom(rom_path)
            output = tmp / "atlas.ppm"

            stdout = io.StringIO()
            with redirect_stdout(stdout):
                exit_code = main(
                    [
                        "tiles-view", str(rom_path),
                        "--range", "0..15",
                        "--cols", "4",
                        "--output", str(output),
                        "--json",
                    ],
                )
            self.assertEqual(exit_code, 0)
            payload = json.loads(stdout.getvalue())
            self.assertEqual(payload["tile_count"], 16)
            self.assertEqual(payload["first_tile"], 0)
            self.assertEqual(payload["last_tile"], 15)
            self.assertEqual(payload["cols"], 4)
            self.assertEqual(payload["rows"], 4)
            self.assertEqual(payload["width"], 32)
            self.assertEqual(payload["height"], 32)
            self.assertEqual(payload["colorisation"], "grayscale")
            self.assertIsNone(payload["palette"])

    def test_cli_rejects_bad_range(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            rom_path = tmp / "demo.ngc"
            _write_demo_rom(rom_path)

            with redirect_stdout(io.StringIO()):
                exit_code = main(
                    [
                        "tiles-view", str(rom_path),
                        "--range", "10..3",
                        "--output", str(tmp / "atlas.ppm"),
                    ],
                )
            # Reversed range: start > end → exit 1.
            self.assertEqual(exit_code, 1)

    def test_cli_seed_from_overlay_renders_tile_data(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            rom_path = tmp / "demo.ngc"
            _write_demo_rom(rom_path)
            machine = load_machine_state(rom_path)

            # Tile #2 with a recognisable pattern.
            overlay: dict[int, int] = {}
            _set_tile_pixels(overlay, 2, [(3,) * 8] * 8)
            state_path = tmp / "state.json"
            save_savestate(
                state_path,
                build_savestate_payload(
                    rom_path=rom_path,
                    rom_header=machine.header,
                    cpu=machine.cpu,
                    writable_overlay=overlay,
                ),
            )

            output = tmp / "atlas.ppm"
            with redirect_stdout(io.StringIO()):
                exit_code = main(
                    [
                        "tiles-view", str(rom_path),
                        "--range", "0..3",
                        "--cols", "2",
                        "--seed-from", str(state_path),
                        "--output", str(output),
                    ],
                )
            self.assertEqual(exit_code, 0)
            data = output.read_bytes()
            body = data[len(b"P6\n16 16\n255\n"):]
            # Tile 2 is at grid position (0, 1) — cols=2, idx=2 → row 1.
            # That covers pixels [0..7, 8..15]. Each pixel should be 0xFF
            # (grayscale white from value 3 nibble-replicated).
            # Pixel at (x=0, y=8): byte offset = (8 * 16 + 0) * 3 = 384.
            self.assertEqual(body[384], 0xFF)
            self.assertEqual(body[385], 0xFF)
            self.assertEqual(body[386], 0xFF)


if __name__ == "__main__":
    unittest.main()
