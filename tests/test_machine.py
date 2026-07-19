"""Minimal machine bootstrap tests for NgpCraft Emulator."""

from __future__ import annotations

import unittest
from pathlib import Path

from core.machine import create_machine_state
from core.rom import NgpcRomHeader


class MachineBootstrapTests(unittest.TestCase):
    def test_machine_state_uses_header_entry_point(self) -> None:
        header = NgpcRomHeader(
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

        state = create_machine_state(header)

        self.assertEqual(state.rom_path, Path("demo.ngc"))
        self.assertEqual(state.cpu.pc, 0x00200040)
        self.assertIsNone(state.cpu.regs.xsp)
        self.assertEqual(state.cpu.modeled_fields, ("PC", "architectural-register-set"))
        self.assertIsNone(state.cpu.sr_raw)
        self.assertIsNone(state.cpu.register_bank)
        self.assertEqual(state.model_status, "partial-bootstrap")
        region_names = {region.name for region in state.memory_regions}
        self.assertIn("CPU_IO_PAGE", region_names)
        self.assertIn("CART_ROM_LOADED", region_names)
        self.assertIn("BIOS_ROM", region_names)
        cart_region = next(
            region for region in state.memory_regions if region.name == "CART_ROM_LOADED"
        )
        self.assertEqual(cart_region.start, 0x200000)
        self.assertEqual(cart_region.end, 0x23FFFF)


if __name__ == "__main__":
    unittest.main()
