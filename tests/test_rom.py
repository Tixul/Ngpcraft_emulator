"""Minimal ROM header tests for NgpCraft Emulator."""

from __future__ import annotations

import unittest
from pathlib import Path

from core.rom import ROM_HEADER_SIZE, parse_rom_header_bytes


class RomHeaderParseTests(unittest.TestCase):
    def test_parse_minimal_header(self) -> None:
        data = bytearray(ROM_HEADER_SIZE)
        data[0x00:0x1C] = b"COPYRIGHT BY SNK" + (b"\x00" * 12)
        data[0x1C:0x20] = (0x00200100).to_bytes(4, "little")
        data[0x20:0x22] = bytes((0x34, 0x12))
        data[0x22] = 7
        data[0x23] = 0x10
        data[0x24:0x30] = b"TEST GAME\x00\x00\x00"

        header = parse_rom_header_bytes(bytes(data), Path("test.ngc"))

        self.assertEqual(header.path, Path("test.ngc"))
        self.assertEqual(header.file_size, ROM_HEADER_SIZE)
        self.assertEqual(header.entry_point, 0x00200100)
        self.assertEqual(header.game_id_raw, 0x1234)
        self.assertEqual(header.game_id_bcd, "1234")
        self.assertEqual(header.version, 7)
        self.assertEqual(header.mode_raw, 0x10)
        self.assertTrue(header.is_color)
        self.assertEqual(header.mode_name, "color")
        self.assertEqual(header.title, "TEST GAME")

    def test_reject_small_rom(self) -> None:
        with self.assertRaises(ValueError):
            parse_rom_header_bytes(b"\x00" * (ROM_HEADER_SIZE - 1), Path("bad.ngc"))


if __name__ == "__main__":
    unittest.main()
