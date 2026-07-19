"""K2GE palette decoding + `palette-info` CLI tests."""

from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from core.k2ge import (
    K2GE_PALETTE_SCR1_BASE,
    K2GE_PALETTE_SCR2_BASE,
    K2GE_PALETTE_SPRITE_BASE,
    decode_color,
    read_all_palettes,
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
    data[0x24:0x30] = b"PAL TEST\x00\x00\x00\x00"
    path.write_bytes(bytes(data))


class K2geColorDecodeTests(unittest.TestCase):
    def test_decode_color_zero_is_black(self) -> None:
        c = decode_color(0x00, 0x00)
        self.assertEqual((c.r, c.g, c.b), (0, 0, 0))
        self.assertEqual(c.raw, 0x0000)
        self.assertEqual(c.hex_rgb24(), "#000000")

    def test_decode_color_full_red(self) -> None:
        # raw = 0x000F → R=F, G=0, B=0. Low byte = 0x0F.
        c = decode_color(0x0F, 0x00)
        self.assertEqual((c.r, c.g, c.b), (0xF, 0, 0))
        self.assertEqual(c.hex_rgb24(), "#FF0000")

    def test_decode_color_full_blue(self) -> None:
        # raw = 0x0F00 → R=0, G=0, B=F. High byte = 0x0F.
        c = decode_color(0x00, 0x0F)
        self.assertEqual((c.r, c.g, c.b), (0, 0, 0xF))
        self.assertEqual(c.hex_rgb24(), "#0000FF")

    def test_decode_color_hex_rgb12_canonical(self) -> None:
        # raw = 0x0BGR: B=A, G=5, R=3 → 0x0A53.
        c = decode_color(0x53, 0x0A)
        self.assertEqual(c.hex_rgb12(), "0xA53")

    def test_decode_color_replicates_nibble_to_byte(self) -> None:
        # R=5 (4-bit) → 0x55 (8-bit) in hex_rgb24.
        c = decode_color(0x55, 0x05)
        self.assertEqual(c.hex_rgb24(), "#555555")


class K2gePaletteReaderTests(unittest.TestCase):
    def test_read_all_palettes_returns_five_planes(self) -> None:
        memory: dict[int, int] = {}  # cold-start, no overrides
        palettes = read_all_palettes(memory)
        self.assertEqual(
            set(palettes.keys()),
            {"sprite", "scr1", "scr2", "background", "window"},
        )

    def test_sprite_scr1_scr2_each_carry_16_palettes(self) -> None:
        palettes = read_all_palettes({})
        for plane in ("sprite", "scr1", "scr2"):
            self.assertEqual(len(palettes[plane]), 16, msg=plane)
            for p in palettes[plane]:
                self.assertEqual(len(p.colors), 4)

    def test_sprite_palette_base_address_matches_constant(self) -> None:
        palettes = read_all_palettes({})
        self.assertEqual(palettes["sprite"][0].base_address, K2GE_PALETTE_SPRITE_BASE)
        self.assertEqual(palettes["scr1"][0].base_address, K2GE_PALETTE_SCR1_BASE)
        self.assertEqual(palettes["scr2"][0].base_address, K2GE_PALETTE_SCR2_BASE)

    def test_cold_start_palette_is_all_zeros(self) -> None:
        palettes = read_all_palettes({})
        for p in palettes["sprite"]:
            for c in p.colors:
                self.assertEqual((c.r, c.g, c.b), (0, 0, 0))

    def test_overlay_color_is_decoded(self) -> None:
        # Sprite palette #2 (base 0x8200 + 2*8 = 0x8210), color #1 (offset +2)
        # raw = 0x0A53 → B=A, G=5, R=3.
        memory = {0x008212: 0x53, 0x008213: 0x0A}
        palettes = read_all_palettes(memory)
        c = palettes["sprite"][2].colors[1]
        self.assertEqual(c.r, 3)
        self.assertEqual(c.g, 5)
        self.assertEqual(c.b, 0xA)


class PaletteInfoCliTests(unittest.TestCase):
    def test_cli_default_prints_all_planes(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            _write_demo_rom(rom_path)

            stdout = io.StringIO()
            with redirect_stdout(stdout):
                exit_code = main(["palette-info", str(rom_path)])
            self.assertEqual(exit_code, 0)
            out = stdout.getvalue()
            self.assertIn("sprite", out)
            self.assertIn("scr1", out)
            self.assertIn("scr2", out)
            self.assertIn("background", out)
            self.assertIn("window", out)

    def test_cli_kind_filters_to_one_plane(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            _write_demo_rom(rom_path)

            stdout = io.StringIO()
            with redirect_stdout(stdout):
                exit_code = main(["palette-info", str(rom_path), "--kind", "sprite", "--json"])
            self.assertEqual(exit_code, 0)
            payload = json.loads(stdout.getvalue())
            self.assertEqual(set(payload["planes"].keys()), {"sprite"})
            self.assertEqual(len(payload["planes"]["sprite"]), 16)

    def test_cli_seed_from_overlay_decodes_written_palette(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            rom_path = tmp / "demo.ngc"
            _write_demo_rom(rom_path)
            machine = load_machine_state(rom_path)

            # Sprite palette #3, color #2 (offset +4 from palette base 0x8218)
            # raw = 0x0F0F → R=F, G=F, B=0 (a yellow).
            overlay = {
                0x00821C: 0xFF,  # low byte: GGGG RRRR = 0xFF
                0x00821D: 0x00,  # high byte: 0000 BBBB = 0x00
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
                        "palette-info", str(rom_path),
                        "--kind", "sprite",
                        "--seed-from", str(state_path),
                        "--json",
                    ]
                )
            self.assertEqual(exit_code, 0)
            payload = json.loads(stdout.getvalue())
            sprite3_color2 = payload["planes"]["sprite"][3]["colors"][2]
            self.assertEqual(sprite3_color2["r"], 0xF)
            self.assertEqual(sprite3_color2["g"], 0xF)
            self.assertEqual(sprite3_color2["b"], 0x0)
            self.assertEqual(sprite3_color2["hex_rgb24"], "#FFFF00")


if __name__ == "__main__":
    unittest.main()
