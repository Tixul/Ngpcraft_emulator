"""Minimal PC-relative fetch tests for NgpCraft Emulator."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from core.fetch import fetch_next_bytes, load_fetch_view


class FetchTests(unittest.TestCase):
    def _write_demo_rom(self, path: Path, entry_point: int = 0x00200040) -> None:
        data = bytearray(0x40)
        data[0x00:0x1C] = b"LICENSED BY SNK CORPORATION".ljust(0x1C, b"\x00")
        data[0x1C:0x20] = entry_point.to_bytes(4, "little")
        data[0x20:0x22] = (0x0000).to_bytes(2, "little")
        data[0x22] = 0
        data[0x23] = 0x10
        data[0x24:0x30] = b"TEST GAME\x00\x00\x00"
        data.extend(b"\xDE\xAD\xBE\xEF")
        path.write_bytes(bytes(data))

    def test_fetch_next_reads_bytes_from_bootstrap_pc(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path)
            view = load_fetch_view(rom_path)

            result = fetch_next_bytes(view, size=4)

            self.assertEqual(result.pc, 0x00200040)
            self.assertEqual(result.status, "ok")
            self.assertEqual(result.data, b"\xDE\xAD\xBE\xEF")
            self.assertEqual(result.next_sequential_pc, 0x00200044)

    def test_fetch_next_reports_unbacked_when_pc_points_to_bios(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, entry_point=0x00FF0000)
            view = load_fetch_view(rom_path)

            result = fetch_next_bytes(view, size=1)

            self.assertEqual(result.status, "unbacked")
            self.assertIsNone(result.data)
            self.assertIsNone(result.next_sequential_pc)


if __name__ == "__main__":
    unittest.main()
