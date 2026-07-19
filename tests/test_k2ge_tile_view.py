"""CHAR_RAM 2bpp tile decoding + `tile-view` CLI tests."""

from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from core.k2ge import (
    CHAR_RAM_TILE_COUNT,
    K2GE_CHAR_RAM_BASE,
    decode_tile,
    read_tile,
)
from core.machine import load_machine_state
from core.savestate import build_savestate_payload, save_savestate
from ngpc_emu import main


def _write_demo_rom(path: Path, entry_point: int = 0x00200040) -> None:
    data = bytearray(0x40)
    data[0x00:0x1C] = b"LICENSED BY SNK CORPORATION".ljust(0x1C, b"\x00")
    data[0x1C:0x20] = entry_point.to_bytes(4, "little")
    data[0x22] = 0
    data[0x23] = 0x10
    data[0x24:0x30] = b"TILE VIEW\x00\x00\x00"
    path.write_bytes(bytes(data))


class K2geTileDecodeTests(unittest.TestCase):
    def test_decode_blank_tile_is_all_zero(self) -> None:
        t = decode_tile(b"\x00" * 16, tile_id=0, base_address=0xA000)
        self.assertTrue(t.is_blank())
        for row in t.pixels:
            self.assertEqual(row, (0, 0, 0, 0, 0, 0, 0, 0))

    def test_decode_row_packs_dots_left_to_right(self) -> None:
        # Row 0: odd byte (dots 0..3) = 0xE4 = 0b11_10_01_00 → dots = [3, 2, 1, 0]
        #        even byte (dots 4..7) = 0x1B = 0b00_01_10_11 → dots = [0, 1, 2, 3]
        # Full row = [3, 2, 1, 0, 0, 1, 2, 3]
        raw = bytearray(16)
        raw[0] = 0x1B  # even byte for row 0 (dots 4..7)
        raw[1] = 0xE4  # odd byte for row 0  (dots 0..3)
        t = decode_tile(bytes(raw), tile_id=1, base_address=0xA010)
        self.assertEqual(t.pixels[0], (3, 2, 1, 0, 0, 1, 2, 3))
        # Other rows stay blank.
        for y in range(1, 8):
            self.assertEqual(t.pixels[y], (0,) * 8)

    def test_decode_all_pixel_values_appear(self) -> None:
        # Build a tile where row y uses uniform pixel value (y % 4) so we
        # exercise all four 2-bit codes 0, 1, 2, 3 across rows.
        raw = bytearray(16)
        for y in range(8):
            v = y % 4
            # Replicate the 2-bit value across all 4 positions of each byte.
            byte_value = (v << 6) | (v << 4) | (v << 2) | v
            raw[y * 2] = byte_value      # even byte: dots 4..7
            raw[y * 2 + 1] = byte_value  # odd byte: dots 0..3
        t = decode_tile(bytes(raw), tile_id=2, base_address=0xA020)
        for y, expected in enumerate([0, 1, 2, 3, 0, 1, 2, 3]):
            self.assertEqual(t.pixels[y], (expected,) * 8, msg=f"row {y}")
        # Non-blank because rows 1..3 and 5..7 carry non-zero values.
        self.assertFalse(t.is_blank())


class K2geTileReaderTests(unittest.TestCase):
    def test_cold_start_tile_is_blank(self) -> None:
        t = read_tile({}, 0)
        self.assertTrue(t.is_blank())
        self.assertEqual(t.base_address, K2GE_CHAR_RAM_BASE)

    def test_tile_id_out_of_range_raises(self) -> None:
        with self.assertRaises(ValueError):
            read_tile({}, -1)
        with self.assertRaises(ValueError):
            read_tile({}, CHAR_RAM_TILE_COUNT)

    def test_tile_address_is_base_plus_16_times_id(self) -> None:
        # Tile #3 starts at 0xA000 + 3*16 = 0xA030.
        t = read_tile({}, 3)
        self.assertEqual(t.base_address, 0xA030)

    def test_overlay_at_tile_5_decodes(self) -> None:
        # Tile #5 base = 0xA000 + 5*16 = 0xA050.
        # Row 0 odd byte (dots 0..3) = 0xFF → all 4 dots = 3.
        memory = {0xA050: 0x00, 0xA051: 0xFF}
        t = read_tile(memory, 5)
        self.assertEqual(t.pixels[0], (3, 3, 3, 3, 0, 0, 0, 0))


class TileViewCliTests(unittest.TestCase):
    def test_cli_json_default_shape(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            _write_demo_rom(rom_path)

            stdout = io.StringIO()
            with redirect_stdout(stdout):
                exit_code = main(["tile-view", str(rom_path), "0", "--json"])
            self.assertEqual(exit_code, 0)
            payload = json.loads(stdout.getvalue())
            self.assertEqual(payload["tile"]["tile_id"], 0)
            self.assertEqual(payload["tile"]["tile_id_hex"], "0x000")
            self.assertEqual(payload["tile"]["base_address_hex"], "0x00A000")
            self.assertTrue(payload["tile"]["blank"])
            self.assertEqual(len(payload["tile"]["rows"]), 8)
            self.assertIsNone(payload["palette"])

    def test_cli_rejects_tile_id_out_of_range(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            _write_demo_rom(rom_path)

            with redirect_stdout(io.StringIO()):
                exit_code = main(["tile-view", str(rom_path), "0x200"])
            self.assertEqual(exit_code, 1)

    def test_cli_seed_from_overlay_renders_pattern(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            rom_path = tmp / "demo.ngc"
            _write_demo_rom(rom_path)
            machine = load_machine_state(rom_path)

            # Tile #7 base = 0xA000 + 7*16 = 0xA070.
            # Write row 0 odd byte (dots 0..3) = 0xE4 → dots [3,2,1,0],
            #       row 0 even byte (dots 4..7) = 0x1B → dots [0,1,2,3].
            overlay = {0xA070: 0x1B, 0xA071: 0xE4}
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

            stdout = io.StringIO()
            with redirect_stdout(stdout):
                exit_code = main(
                    [
                        "tile-view", str(rom_path), "7",
                        "--seed-from", str(state_path),
                        "--json",
                    ]
                )
            self.assertEqual(exit_code, 0)
            payload = json.loads(stdout.getvalue())
            self.assertFalse(payload["tile"]["blank"])
            row0 = payload["tile"]["rows"][0]
            self.assertEqual(row0["values"], [3, 2, 1, 0, 0, 1, 2, 3])

    def test_cli_palette_resolution_attaches_hex_rgb(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            rom_path = tmp / "demo.ngc"
            _write_demo_rom(rom_path)
            machine = load_machine_state(rom_path)

            # Seed SCR1 palette #2 with four distinct colors (one per 2-bit
            # value), and tile #4 row 0 with values [0, 1, 2, 3, 3, 2, 1, 0].
            # SCR1 palette base = 0x8280. Palette #2 base = 0x8280 + 2*8 = 0x8290.
            overlay = {
                # color 0 = black (low=0x00 high=0x00)
                0x8290: 0x00, 0x8291: 0x00,
                # color 1 = red (raw 0x000F → low=0x0F high=0x00)
                0x8292: 0x0F, 0x8293: 0x00,
                # color 2 = green (raw 0x00F0 → low=0xF0 high=0x00)
                0x8294: 0xF0, 0x8295: 0x00,
                # color 3 = blue (raw 0x0F00 → low=0x00 high=0x0F)
                0x8296: 0x00, 0x8297: 0x0F,
                # tile #4 base = 0xA000 + 4*16 = 0xA040
                # row 0 odd byte = 0xE4 → [3,2,1,0], even = 0x1B → [0,1,2,3]
                0xA040: 0x1B, 0xA041: 0xE4,
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

            stdout = io.StringIO()
            with redirect_stdout(stdout):
                exit_code = main(
                    [
                        "tile-view", str(rom_path), "4",
                        "--plane", "scr1",
                        "--palette", "2",
                        "--seed-from", str(state_path),
                        "--json",
                    ]
                )
            self.assertEqual(exit_code, 0)
            payload = json.loads(stdout.getvalue())
            self.assertEqual(payload["palette_plane"], "scr1")
            self.assertEqual(payload["palette_index"], 2)
            self.assertIsNotNone(payload["palette"])

            row0 = payload["tile"]["rows"][0]
            self.assertEqual(row0["values"], [3, 2, 1, 0, 0, 1, 2, 3])
            # The palette has black/red/green/blue at indices 0..3.
            self.assertEqual(row0["hex_rgb24"][0], "#0000FF")  # value 3 = blue
            self.assertEqual(row0["hex_rgb24"][1], "#00FF00")  # value 2 = green
            self.assertEqual(row0["hex_rgb24"][2], "#FF0000")  # value 1 = red
            self.assertEqual(row0["hex_rgb24"][3], "#000000")  # value 0 = black

    def test_cli_palette_without_plane_errors(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            _write_demo_rom(rom_path)

            with redirect_stdout(io.StringIO()):
                exit_code = main(
                    ["tile-view", str(rom_path), "0", "--palette", "2"]
                )
            self.assertEqual(exit_code, 1)


if __name__ == "__main__":
    unittest.main()
