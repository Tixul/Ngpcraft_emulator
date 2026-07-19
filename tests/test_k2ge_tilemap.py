"""K2GE tilemap decoding + `tilemap-info` CLI tests."""

from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from core.k2ge import (
    K2GE_SCR1_TILEMAP_BASE,
    K2GE_SCR2_TILEMAP_BASE,
    TILEMAP_TILES_PER_COL,
    TILEMAP_TILES_PER_ROW,
    decode_tilemap_entry,
    read_tilemap,
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
    data[0x24:0x30] = b"TILE TEST\x00\x00\x00"
    path.write_bytes(bytes(data))


class K2geTilemapDecodeTests(unittest.TestCase):
    def test_decode_zero_entry_is_empty(self) -> None:
        e = decode_tilemap_entry(
            b"\x00\x00", plane="scr1", x=0, y=0, base_address=0x9000,
        )
        self.assertEqual(e.c_c, 0)
        self.assertFalse(e.h_flip)
        self.assertFalse(e.v_flip)
        self.assertFalse(e.p_c)
        self.assertEqual(e.cp_c, 0)
        self.assertTrue(e.is_empty())

    def test_decode_tile_high_bit(self) -> None:
        # C.C low = 0x40, attrib bit 0 = 1 → tile = 0x140 = 320.
        e = decode_tilemap_entry(
            b"\x40\x01", plane="scr1", x=0, y=0, base_address=0x9000,
        )
        self.assertEqual(e.c_c, 0x140)
        self.assertFalse(e.is_empty())

    def test_decode_cp_c_is_low_4_bits_of_attrib_shifted(self) -> None:
        # attrib = 0b0001_1110 → CP.C = 0b1111 = 15.
        e = decode_tilemap_entry(
            b"\x01\x1E", plane="scr1", x=0, y=0, base_address=0x9000,
        )
        self.assertEqual(e.cp_c, 15)

    def test_decode_flip_and_pc_bits(self) -> None:
        # H.F bit 7, V.F bit 6, P.C bit 5.
        e_hf = decode_tilemap_entry(b"\x01\x80", plane="scr1", x=0, y=0, base_address=0x9000)
        e_vf = decode_tilemap_entry(b"\x01\x40", plane="scr1", x=0, y=0, base_address=0x9000)
        e_pc = decode_tilemap_entry(b"\x01\x20", plane="scr1", x=0, y=0, base_address=0x9000)
        self.assertTrue(e_hf.h_flip)
        self.assertFalse(e_hf.v_flip)
        self.assertTrue(e_vf.v_flip)
        self.assertFalse(e_vf.h_flip)
        self.assertTrue(e_pc.p_c)


class K2geTilemapReaderTests(unittest.TestCase):
    def test_cold_start_tilemap_is_all_empty(self) -> None:
        entries = read_tilemap({}, "scr1")
        self.assertEqual(len(entries), TILEMAP_TILES_PER_ROW * TILEMAP_TILES_PER_COL)
        self.assertEqual(len(entries), 1024)
        for e in entries:
            self.assertTrue(e.is_empty())

    def test_scr1_and_scr2_bases_match_constants(self) -> None:
        scr1 = read_tilemap({}, "scr1")
        scr2 = read_tilemap({}, "scr2")
        self.assertEqual(scr1[0].base_address, K2GE_SCR1_TILEMAP_BASE)
        self.assertEqual(scr2[0].base_address, K2GE_SCR2_TILEMAP_BASE)

    def test_invalid_plane_raises(self) -> None:
        with self.assertRaises(ValueError):
            read_tilemap({}, "scr3")

    def test_row_major_ordering(self) -> None:
        entries = read_tilemap({}, "scr1")
        # Entry at index (y*32 + x) should report exactly that (x, y).
        for y in (0, 5, 31):
            for x in (0, 7, 31):
                e = entries[y * 32 + x]
                self.assertEqual((e.x, e.y), (x, y))

    def test_overlay_at_scr1_tile_5_3_decodes(self) -> None:
        # Tile (x=5, y=3) on SCR1 base = 0x9000 + (3*32 + 5)*2 = 0x9000 + 0xCA = 0x90CA.
        # tile=0x080, CP.C=2, H.F=1.
        memory = {
            0x0090CA: 0x80,           # C.C low = 0x80
            0x0090CB: 0b10000100,     # H.F=1, CP.C=2 (bits 4..1 = 0010), bit0 = 0 → tile = 0x080
        }
        entries = read_tilemap(memory, "scr1")
        e = entries[3 * 32 + 5]
        self.assertEqual(e.c_c, 0x080)
        self.assertEqual(e.cp_c, 2)
        self.assertTrue(e.h_flip)
        self.assertFalse(e.is_empty())


class TilemapInfoCliTests(unittest.TestCase):
    def test_cli_cold_start_grid_shows_all_dots(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            _write_demo_rom(rom_path)

            stdout = io.StringIO()
            with redirect_stdout(stdout):
                exit_code = main(["tilemap-info", str(rom_path)])
            self.assertEqual(exit_code, 0)
            out = stdout.getvalue()
            self.assertIn("Plane: scr1", out)
            self.assertIn("Non-empty: 0/1024", out)
            # 32 row labels appear.
            self.assertIn(" 0: ", out)
            self.assertIn("31: ", out)
            # No tiles set → many dots.
            self.assertIn("." * 32, out)

    def test_cli_json_default_lists_1024_entries(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            _write_demo_rom(rom_path)

            stdout = io.StringIO()
            with redirect_stdout(stdout):
                exit_code = main(["tilemap-info", str(rom_path), "--json"])
            self.assertEqual(exit_code, 0)
            payload = json.loads(stdout.getvalue())
            self.assertEqual(payload["plane"], "scr1")
            self.assertEqual(payload["total_entries"], 1024)
            self.assertEqual(payload["shown_count"], 1024)
            self.assertEqual(payload["non_empty_count"], 0)
            self.assertEqual(len(payload["entries"]), 1024)

    def test_cli_non_empty_filters_cold_start_to_zero(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            _write_demo_rom(rom_path)

            stdout = io.StringIO()
            with redirect_stdout(stdout):
                exit_code = main(
                    ["tilemap-info", str(rom_path), "--non-empty", "--json"]
                )
            self.assertEqual(exit_code, 0)
            payload = json.loads(stdout.getvalue())
            self.assertEqual(payload["shown_count"], 0)
            self.assertEqual(payload["entries"], [])

    def test_cli_seed_from_overlay_decodes_two_tiles(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            rom_path = tmp / "demo.ngc"
            _write_demo_rom(rom_path)
            machine = load_machine_state(rom_path)

            # SCR2 tile (x=0, y=0) and (x=1, y=0).
            # base = 0x9800.
            overlay = {
                0x009800: 0x10,                 # tile (0,0): C.C low = 0x10
                0x009801: 0b00000110,           # CP.C = 3 (bits 4..1 = 0011), tile = 0x010
                0x009802: 0x42,                 # tile (1,0): C.C low = 0x42
                0x009803: 0b10001000,           # H.F=1, CP.C=4 (bits 4..1 = 0100), tile = 0x042
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
                        "tilemap-info", str(rom_path),
                        "--plane", "scr2",
                        "--non-empty",
                        "--seed-from", str(state_path),
                        "--json",
                    ]
                )
            self.assertEqual(exit_code, 0)
            payload = json.loads(stdout.getvalue())
            self.assertEqual(payload["plane"], "scr2")
            self.assertEqual(payload["shown_count"], 2)
            tiles = sorted(payload["entries"], key=lambda e: (e["y"], e["x"]))
            self.assertEqual(tiles[0]["tile"], 0x010)
            self.assertEqual(tiles[0]["cp_c"], 3)
            self.assertFalse(tiles[0]["h_flip"])
            self.assertEqual(tiles[1]["tile"], 0x042)
            self.assertEqual(tiles[1]["cp_c"], 4)
            self.assertTrue(tiles[1]["h_flip"])


if __name__ == "__main__":
    unittest.main()
