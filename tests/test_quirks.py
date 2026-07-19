"""Known hardware-quirk matching tests for NgpCraft Emulator."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from core.decode import decode_instruction_at
from core.memory import load_read_bus
from core.quirks import (
    load_known_quirk_database,
    match_known_quirk,
    match_known_silicon_broken,
)


class KnownQuirkTests(unittest.TestCase):
    def _write_demo_rom(self, path: Path, entry_point: int, body: bytes) -> None:
        data = bytearray(0x40)
        data[0x00:0x1C] = b"LICENSED BY SNK CORPORATION".ljust(0x1C, b"\x00")
        data[0x1C:0x20] = entry_point.to_bytes(4, "little")
        data[0x20:0x22] = (0x0000).to_bytes(2, "little")
        data[0x22] = 0
        data[0x23] = 0x10
        data[0x24:0x30] = b"TEST GAME\x00\x00\x00"
        body_offset = entry_point - 0x00200000
        if body_offset < len(data):
            data[body_offset : body_offset + len(body)] = body
        else:
            data.extend(b"\x00" * (body_offset - len(data)))
            data.extend(body)
        path.write_bytes(bytes(data))

    def test_load_known_quirk_database_exposes_version_and_entry_count(self) -> None:
        database = load_known_quirk_database()

        # v13 RETIRED `cpu.d0_d7_non_immediate`: it was a mis-diagnosis, hardware said
        # so on 2026-07-03, and its matcher was intercepting ordinary word-MEMORY
        # instructions -- it read raw[1] as a sub-opcode to safe-list, but in a memory
        # family raw[1] is an ADDRESS byte. Three commercial ROMs were being stopped by it.
        self.assertEqual(database.database_version, "2026-07-12.v13")
        self.assertEqual(database.entry_count, 2)

    def test_known_quirk_match_carries_non_empty_source_attribution(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            # Migrated off the retired `D0 FA` mis-diagnosis fixture (0xD0..0xD7
            # is now the WORD MEMORY-addressing family -> `D0 FA` decodes as a
            # clean `cpw (abs8)` and is no longer silicon-broken). The still-broken
            # form is the D0 ALU-immediate `add WA, 0x0001` (`D0 C8 01 00`),
            # HW-confirmed crash 2026-05-20.
            self._write_demo_rom(rom_path, 0x00200040, b"\xD8\xB8")
            bus = load_read_bus(rom_path)
            decoded = decode_instruction_at(bus, 0x00200040)

            match = match_known_quirk(decoded)

            self.assertIsNotNone(match)
            assert match is not None
            self.assertGreaterEqual(len(match.sources), 1)
            first = match.sources[0]
            self.assertTrue(first.document)

    def test_known_quirk_match_for_link_xiy_carries_source_attribution(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x00200040, b"\xED\x0C\x08\x00")
            bus = load_read_bus(rom_path)
            decoded = decode_instruction_at(bus, 0x00200040)

            match = match_known_silicon_broken(decoded)

            self.assertIsNotNone(match)
            assert match is not None
            self.assertEqual(match.quirk_id, "cpu.link_xiy_large_frame")
            self.assertGreaterEqual(len(match.sources), 1)
            for source in match.sources:
                self.assertTrue(source.document)

    def test_match_known_silicon_broken_flags_d0_alu_immediate_form(self) -> None:
        # Renamed + re-pointed off the retired `D0 FA` "non-immediate" fixture:
        # 0xD0..0xD7 is the WORD MEMORY-addressing family, so `D0 FA` now decodes
        # as `cpw (abs8)` (a valid memory op, not silicon-broken). The mechanism
        # under test -- silicon-broken flagging carrying the versioned quirk id --
        # is preserved by re-pointing to the still-broken D0 ALU-immediate form
        # `add WA, 0x0001` (`D0 C8 01 00`, HW crash 2026-05-20).
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x00200040, b"\xD8\xB8")
            bus = load_read_bus(rom_path)
            decoded = decode_instruction_at(bus, 0x00200040)

            match = match_known_silicon_broken(decoded)

            self.assertIsNotNone(match)
            assert match is not None
            self.assertEqual(match.database_version, load_known_quirk_database().database_version)
            self.assertEqual(match.quirk_id, "cpu.d8_df_register_to_register")

    def test_match_known_silicon_broken_flags_d0_alu_immediate(self) -> None:
        """Updated 2026-05-20 after HW crash on stargunner_j16_C4_phase4_BROKEN_HW.

        The earlier matcher version treated `D0 C8 lo hi` (= add WA, imm16)
        as safe because the ALU-imm sub-op range 0xC8..0xCF was listed in
        safe_second_ranges. The 2026-05-20 HW test proved this is silicon-
        broken on real NGPC — CC900 emits 0 instances of <alu> WA, imm in
        f_code, confirming. The matcher now correctly flags this form.
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x00200040, b"\xD8\xB8")
            bus = load_read_bus(rom_path)
            decoded = decode_instruction_at(bus, 0x00200040)

            match = match_known_silicon_broken(decoded)

            self.assertIsNotNone(match)
            assert match is not None
            self.assertEqual(match.quirk_id, "cpu.d8_df_register_to_register")

    def test_match_known_silicon_broken_flags_large_link_xiy(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x00200040, b"\xED\x0C\x08\x00")
            bus = load_read_bus(rom_path)
            decoded = decode_instruction_at(bus, 0x00200040)

            match = match_known_silicon_broken(decoded)

            self.assertIsNotNone(match)
            assert match is not None
            self.assertEqual(match.quirk_id, "cpu.link_xiy_large_frame")

    def test_match_known_silicon_broken_skips_targeted_safe_d8_83_exception(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x00200040, b"\xD8\x83")
            bus = load_read_bus(rom_path)
            decoded = decode_instruction_at(bus, 0x00200040)

            match = match_known_silicon_broken(decoded)

            self.assertIsNone(match)

    def test_match_known_silicon_broken_skips_hw_cleared_add_reg_reg(self) -> None:
        """HW-cleared 2026-07-05 (hw_test_addrr, GREEN): `add WA, WA` (D8 80)
        and `add DE, WA` (D8 82) execute correctly on real NGPC. The word
        arith/logic r+r family is no longer flagged silicon-broken."""
        for body in (b"\xD8\x80", b"\xD8\x82"):
            with tempfile.TemporaryDirectory() as tmpdir:
                rom_path = Path(tmpdir) / "demo.ngc"
                self._write_demo_rom(rom_path, 0x00200040, body)
                bus = load_read_bus(rom_path)
                decoded = decode_instruction_at(bus, 0x00200040)

                match = match_known_silicon_broken(decoded)

                self.assertIsNone(match, f"{body.hex()} should no longer be silicon-broken")

    def test_d9_50_divide_register_form_is_hw_cleared_not_flagged(self) -> None:
        # div WA, BC (D9 50) was HW-cleared 2026-07-06 (hw_test_muldiv GREEN):
        # it is no longer flagged as silicon-broken. A still-broken pocket form
        # (D8 B8, sub-op 0xB8) confirms the matcher still catches the rest.
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x00207CAC, b"\xD9\x50")
            bus = load_read_bus(rom_path)
            decoded = decode_instruction_at(bus, 0x00207CAC)
            self.assertIsNone(match_known_silicon_broken(decoded))

            self._write_demo_rom(rom_path, 0x00207CAC, b"\xD8\xB8")
            bus2 = load_read_bus(rom_path)
            decoded2 = decode_instruction_at(bus2, 0x00207CAC)
            match = match_known_silicon_broken(decoded2)
            self.assertIsNotNone(match)
            assert match is not None
            self.assertEqual(match.quirk_id, "cpu.d8_df_register_to_register")

    def test_match_known_quirk_returns_same_versioned_match_for_decode_only_use(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            # Migrated off the retired `D0 FA` fixture (now a clean `cpw (abs8)`
            # memory op) to the still-broken D0 ALU-immediate `add WA, 0x0001`.
            self._write_demo_rom(rom_path, 0x00200040, b"\xD8\xB8")
            bus = load_read_bus(rom_path)
            decoded = decode_instruction_at(bus, 0x00200040)

            match = match_known_quirk(decoded)

            self.assertIsNotNone(match)
            assert match is not None
            self.assertEqual(match.database_version, load_known_quirk_database().database_version)
            self.assertEqual(match.quirk_id, "cpu.d8_df_register_to_register")


if __name__ == "__main__":
    unittest.main()
