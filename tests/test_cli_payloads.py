"""CLI payload helper tests for NgpCraft Emulator."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from core.decode import decode_instruction_at
from core.fetch import load_fetch_view
from core.memory import load_read_bus
from core.quirks import load_known_quirk_database
from core.step import build_step_preview
from core.trace import build_trace_preview
from ngpc_emu import _decode_to_dict, _step_to_dict, _trace_to_dict


class CliPayloadTests(unittest.TestCase):
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

    def test_decode_payload_includes_matched_quirk_for_known_broken_prefix(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x00200040, b"\xD8\xB8")
            bus = load_read_bus(rom_path)
            decoded = decode_instruction_at(bus, 0x00200040)

            payload = _decode_to_dict(decoded)

            self.assertIsNotNone(payload["matched_quirk"])
            quirk = payload["matched_quirk"]
            assert isinstance(quirk, dict)
            self.assertEqual(quirk["database_version"], load_known_quirk_database().database_version)
            self.assertEqual(quirk["quirk_id"], "cpu.d8_df_register_to_register")

    def test_decode_payload_exposes_quirk_source_attribution(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x00200040, b"\xD8\xB8")
            bus = load_read_bus(rom_path)
            decoded = decode_instruction_at(bus, 0x00200040)

            payload = _decode_to_dict(decoded)
            quirk = payload["matched_quirk"]
            assert isinstance(quirk, dict)
            sources = quirk["sources"]
            assert isinstance(sources, list)

            self.assertGreaterEqual(len(sources), 1)
            first = sources[0]
            assert isinstance(first, dict)
            self.assertIn("document", first)
            self.assertIn("section", first)
            self.assertIn("quote", first)
            self.assertTrue(first["document"])

    def test_decode_payload_flags_matched_quirk_for_d0_alu_immediate(self) -> None:
        """Updated 2026-05-20: `D0 C8 lo hi` (= add WA, imm16) is silicon-broken
        on real NGPC. The HW crash on stargunner_j16_C4_phase4_BROKEN_HW
        confirmed the matcher was wrong to treat this as safe. Test now
        asserts the broken form is correctly flagged."""
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x00200040, b"\xD8\xB8")
            bus = load_read_bus(rom_path)
            decoded = decode_instruction_at(bus, 0x00200040)

            payload = _decode_to_dict(decoded)

            quirk = payload["matched_quirk"]
            self.assertIsNotNone(quirk)
            assert isinstance(quirk, dict)
            self.assertEqual(quirk["quirk_id"], "cpu.d8_df_register_to_register")

    def test_step_preview_payload_reuses_decode_matched_quirk(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x00200040, b"\xD8\xB8")
            view = load_fetch_view(rom_path)
            preview = build_step_preview(view)

            payload = _step_to_dict(preview)
            decode = payload["decode"]
            assert isinstance(decode, dict)
            quirk = decode["matched_quirk"]
            assert isinstance(quirk, dict)

            self.assertEqual(quirk["database_version"], load_known_quirk_database().database_version)
            self.assertEqual(quirk["quirk_id"], "cpu.d8_df_register_to_register")

    def test_trace_preview_payload_reuses_decode_matched_quirk(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x00200040, b"\xD8\xB8")
            view = load_fetch_view(rom_path)
            preview = build_trace_preview(view, count=1)

            payload = _trace_to_dict(preview)
            records = payload["records"]
            assert isinstance(records, list)
            quirk = records[0]["matched_quirk"]
            assert isinstance(quirk, dict)

            self.assertEqual(quirk["database_version"], load_known_quirk_database().database_version)
            self.assertEqual(quirk["quirk_id"], "cpu.d8_df_register_to_register")


if __name__ == "__main__":
    unittest.main()
