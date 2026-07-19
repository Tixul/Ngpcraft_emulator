"""Minimal address-space tests for NgpCraft Emulator."""

from __future__ import annotations

import unittest
from pathlib import Path

from core.bus import build_address_space
from core.rom import NgpcRomHeader


class AddressSpaceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.header = NgpcRomHeader(
            path=Path("demo.ngc"),
            file_size=0x40000,
            copyright_text="LICENSED BY SNK CORPORATION",
            entry_point=0x00200040,
            game_id_raw=0,
            game_id_bcd="0000",
            version=0,
            mode_raw=0x10,
            title="DEMO",
        )
        self.space = build_address_space(self.header)

    def test_probe_loaded_cart_rom_address(self) -> None:
        probe = self.space.probe(0x200040)
        self.assertEqual(probe.status, "mapped")
        self.assertIsNotNone(probe.region)
        assert probe.region is not None
        self.assertEqual(probe.region.name, "CART_ROM_LOADED")
        self.assertEqual(probe.region_offset, 0x40)
        self.assertEqual(probe.file_offset, 0x40)

    def test_probe_bios_address(self) -> None:
        probe = self.space.probe(0xFF0100)
        self.assertEqual(probe.status, "mapped")
        self.assertIsNotNone(probe.region)
        assert probe.region is not None
        self.assertEqual(probe.region.name, "BIOS_ROM")
        self.assertIsNone(probe.file_offset)

    def test_probe_unloaded_cart_window_address(self) -> None:
        probe = self.space.probe(0x3FF000)
        self.assertEqual(probe.status, "mapped")
        self.assertIsNotNone(probe.region)
        assert probe.region is not None
        self.assertEqual(probe.region.name, "CART_ROM_UNLOADED")
        self.assertIsNone(probe.file_offset)

    def test_probe_unmapped_address(self) -> None:
        probe = self.space.probe(0x010000)
        self.assertEqual(probe.status, "unmapped")
        self.assertIsNone(probe.region)


if __name__ == "__main__":
    unittest.main()
