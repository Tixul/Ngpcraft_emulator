"""K2GE OAM decoding + `oam-info` CLI tests."""

from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from core.k2ge import (
    K2GE_OAM_BASE,
    K2GE_OAM_PALETTE_CODES_BASE,
    OAM_SPRITE_COUNT,
    decode_sprite,
    read_oam_sprites,
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
    data[0x24:0x30] = b"OAM TEST\x00\x00\x00\x00"
    path.write_bytes(bytes(data))


class K2geSpriteDecodeTests(unittest.TestCase):
    def test_decode_sprite_all_zero_bytes(self) -> None:
        s = decode_sprite(b"\x00\x00\x00\x00", 0x00, index=0, base_address=0x8800)
        self.assertEqual(s.c_c, 0)
        self.assertFalse(s.h_flip)
        self.assertFalse(s.v_flip)
        self.assertFalse(s.p_c)
        self.assertEqual(s.pr_c, 0)
        self.assertEqual(s.pr_c_label, "hidden")
        self.assertFalse(s.h_chain)
        self.assertFalse(s.v_chain)
        self.assertEqual(s.h_pos, 0)
        self.assertEqual(s.v_pos, 0)
        self.assertEqual(s.cp_c, 0)
        self.assertTrue(s.is_hidden())

    def test_decode_sprite_tile_high_bit(self) -> None:
        # C.C low = 0x80, C.C bit8 = 1 (attrib bit 0) → tile = 0x180 = 384.
        s = decode_sprite(b"\x80\x01\x00\x00", 0x00, index=0, base_address=0x8800)
        self.assertEqual(s.c_c, 0x180)

    def test_decode_sprite_h_flip_and_v_flip(self) -> None:
        # attrib bit 7 = H.F, bit 6 = V.F.
        s = decode_sprite(b"\x00\xC0\x00\x00", 0x00, index=0, base_address=0x8800)
        self.assertTrue(s.h_flip)
        self.assertTrue(s.v_flip)

    def test_decode_sprite_priority_codes_all_four(self) -> None:
        # PR.C is attrib bits[4:3].
        labels = ["hidden", "behind-scr", "middle", "front"]
        for prc in range(4):
            attrib = (prc & 0x03) << 3
            s = decode_sprite(
                bytes([0x00, attrib, 0x00, 0x00]), 0x00,
                index=0, base_address=0x8800,
            )
            self.assertEqual(s.pr_c, prc)
            self.assertEqual(s.pr_c_label, labels[prc])
            self.assertEqual(s.is_hidden(), prc == 0)

    def test_decode_sprite_chain_bits(self) -> None:
        # H.ch = bit 2, V.ch = bit 1.
        s_hc = decode_sprite(b"\x00\x04\x00\x00", 0x00, index=0, base_address=0x8800)
        self.assertTrue(s_hc.h_chain)
        self.assertFalse(s_hc.v_chain)
        s_vc = decode_sprite(b"\x00\x02\x00\x00", 0x00, index=0, base_address=0x8800)
        self.assertFalse(s_vc.h_chain)
        self.assertTrue(s_vc.v_chain)

    def test_decode_sprite_position(self) -> None:
        s = decode_sprite(b"\x00\x00\x50\x88", 0x00, index=0, base_address=0x8800)
        self.assertEqual(s.h_pos, 0x50)
        self.assertEqual(s.v_pos, 0x88)

    def test_decode_sprite_cp_c_low_nibble_only(self) -> None:
        # CP.C is the low 4 bits of the byte at 0x8C00+n.
        s = decode_sprite(b"\x00\x00\x00\x00", 0x4B, index=0, base_address=0x8800)
        self.assertEqual(s.cp_c, 0x0B)
        self.assertEqual(s.cp_c_raw, 0x4B)


class K2geOamReaderTests(unittest.TestCase):
    def test_cold_start_oam_yields_64_hidden_sprites(self) -> None:
        sprites = read_oam_sprites({})
        self.assertEqual(len(sprites), OAM_SPRITE_COUNT)
        self.assertEqual(len(sprites), 64)
        for s in sprites:
            self.assertTrue(s.is_hidden())

    def test_overlay_writes_to_sprite_3(self) -> None:
        # Sprite #3 base = 0x8800 + 3*4 = 0x880C.
        # Set tile=0x101 (bit8=1, low=0x01), PR.C=3 (front), pos=(0x20, 0x40), cp=5.
        memory = {
            0x00880C: 0x01,         # C.C low
            0x00880D: 0b00011001,   # attrib: PR.C=11 (3, bits 4..3=11), C.C bit8=1
            0x00880E: 0x20,
            0x00880F: 0x40,
            0x008C03: 0x05,         # CP.C for sprite 3
        }
        sprites = read_oam_sprites(memory)
        s = sprites[3]
        self.assertEqual(s.c_c, 0x101)
        self.assertEqual(s.pr_c, 3)
        self.assertEqual(s.pr_c_label, "front")
        self.assertEqual(s.h_pos, 0x20)
        self.assertEqual(s.v_pos, 0x40)
        self.assertEqual(s.cp_c, 5)
        self.assertFalse(s.is_hidden())


class OamInfoCliTests(unittest.TestCase):
    def test_cli_cold_start_shows_64_total_zero_visible(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            _write_demo_rom(rom_path)

            stdout = io.StringIO()
            with redirect_stdout(stdout):
                exit_code = main(["oam-info", str(rom_path), "--json"])
            self.assertEqual(exit_code, 0)
            payload = json.loads(stdout.getvalue())
            self.assertEqual(payload["total_sprites"], 64)
            self.assertEqual(payload["shown_count"], 64)
            self.assertEqual(len(payload["sprites"]), 64)
            self.assertTrue(all(s["hidden"] for s in payload["sprites"]))

    def test_cli_visible_only_filters_cold_start_to_zero(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            _write_demo_rom(rom_path)

            stdout = io.StringIO()
            with redirect_stdout(stdout):
                exit_code = main(["oam-info", str(rom_path), "--visible-only", "--json"])
            self.assertEqual(exit_code, 0)
            payload = json.loads(stdout.getvalue())
            self.assertEqual(payload["shown_count"], 0)
            self.assertEqual(payload["sprites"], [])

    def test_cli_seed_from_overlay_shows_visible_sprite(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            rom_path = tmp / "demo.ngc"
            _write_demo_rom(rom_path)
            machine = load_machine_state(rom_path)

            # Activate sprite #5 (base 0x8814) with PR.C=2 (middle).
            overlay = {
                0x008814: 0x40,           # C.C low = 0x40
                0x008815: 0b00010000,     # PR.C=10 (2, middle)
                0x008816: 0x10,           # H pos
                0x008817: 0x20,           # V pos
                0x008C05: 0x07,           # CP.C for sprite 5
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
                        "oam-info", str(rom_path),
                        "--seed-from", str(state_path),
                        "--visible-only", "--json",
                    ]
                )
            self.assertEqual(exit_code, 0)
            payload = json.loads(stdout.getvalue())
            self.assertEqual(payload["shown_count"], 1)
            s = payload["sprites"][0]
            self.assertEqual(s["index"], 5)
            self.assertEqual(s["tile"], 0x40)
            self.assertEqual(s["pr_c"], 2)
            self.assertEqual(s["pr_c_label"], "middle")
            self.assertEqual(s["cp_c"], 7)
            self.assertEqual(s["h_pos"], 0x10)
            self.assertEqual(s["v_pos"], 0x20)


if __name__ == "__main__":
    unittest.main()
