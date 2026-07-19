"""Shared seed-preset regression tests."""

from __future__ import annotations

import unittest

from core.seed_presets import (
    BIOS_HANDOFF_INTNEST,
    BIOS_HANDOFF_XSP,
    bios_handoff_minimal_seed_registers,
)


class SeedPresetTests(unittest.TestCase):
    def test_bios_handoff_minimal_seed_registers_match_documented_values(self) -> None:
        self.assertEqual(BIOS_HANDOFF_XSP, 0x00006C00)
        self.assertEqual(BIOS_HANDOFF_INTNEST, 0)
        self.assertEqual(
            bios_handoff_minimal_seed_registers(),
            {
                "XSP": 0x00006C00,
                "INTNEST": 0,
            },
        )

    def test_bios_handoff_minimal_seed_registers_returns_fresh_dict(self) -> None:
        seed_map = bios_handoff_minimal_seed_registers()
        seed_map["INTNEST"] = 0x1234
        self.assertEqual(
            bios_handoff_minimal_seed_registers(),
            {
                "XSP": 0x00006C00,
                "INTNEST": 0,
            },
        )


if __name__ == "__main__":
    unittest.main()
