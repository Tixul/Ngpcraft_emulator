"""Minimal static run-until-preview tests for NgpCraft Emulator."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from core.fetch import load_fetch_view
from core.run_until import build_run_until_preview


class RunUntilPreviewTests(unittest.TestCase):
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

    def test_run_until_preview_reports_already_at_target(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x00200040, b"\x00")
            view = load_fetch_view(rom_path)

            preview = build_run_until_preview(view, target_pc=0x00200040)

            self.assertEqual(preview.stop_reason, "already-at-target")
            self.assertTrue(preview.reached_target)
            self.assertEqual(preview.emitted_count, 0)
            self.assertEqual(preview.terminal_pc, 0x00200040)

    def test_run_until_preview_reaches_sequential_target(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x00200040, b"\x08\x6F\x4E\x00")
            view = load_fetch_view(rom_path)

            preview = build_run_until_preview(view, target_pc=0x00200043)

            self.assertEqual(preview.stop_reason, "target-reached")
            self.assertTrue(preview.reached_target)
            self.assertEqual(preview.emitted_count, 1)
            self.assertEqual(preview.records[0].step.decode.assembly, "ldb (HW_WATCHDOG), 0x4E")
            self.assertEqual(preview.records[0].step.preview_target, 0x00200043)

    def test_run_until_preview_over_mode_uses_call_return_site(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x00200040, b"\x1D\x2F\x91\x20")
            view = load_fetch_view(rom_path)

            preview = build_run_until_preview(view, target_pc=0x00200044, mode="over")

            self.assertEqual(preview.stop_reason, "target-reached")
            self.assertTrue(preview.reached_target)
            self.assertEqual(preview.records[0].step.reason, "call-return-site-preview")
            self.assertEqual(preview.terminal_pc, 0x00200044)

    def test_run_until_preview_into_mode_reaches_direct_call_target(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x00200040, b"\x1D\x2F\x91\x20")
            view = load_fetch_view(rom_path)

            preview = build_run_until_preview(view, target_pc=0x0020912F, mode="into")

            self.assertEqual(preview.stop_reason, "target-reached")
            self.assertTrue(preview.reached_target)
            self.assertEqual(preview.records[0].step.reason, "direct-call-target")
            self.assertEqual(preview.terminal_pc, 0x0020912F)

    def test_run_until_preview_stops_on_unresolved_conditional_flow(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x00200040, b"\x66\x16")
            view = load_fetch_view(rom_path)

            preview = build_run_until_preview(view, target_pc=0x00200058)

            self.assertEqual(
                preview.stop_reason,
                "stopped-on-conditional-control-flow-unresolved",
            )
            self.assertFalse(preview.reached_target)
            self.assertIsNone(preview.records[0].step.preview_target)
            self.assertEqual(preview.terminal_pc, 0x00200040)

    def test_run_until_preview_stops_on_cycle(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x00200040, b"\x68\xFE")
            view = load_fetch_view(rom_path)

            preview = build_run_until_preview(view, target_pc=0x00200050, mode="into")

            self.assertEqual(preview.stop_reason, "stopped-on-cycle")
            self.assertFalse(preview.reached_target)
            self.assertEqual(preview.records[0].step.preview_target, 0x00200040)
            self.assertEqual(preview.terminal_pc, 0x00200040)

    def test_run_until_preview_stops_on_max_steps(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x00200040, b"\x00\x00")
            view = load_fetch_view(rom_path)

            preview = build_run_until_preview(
                view,
                target_pc=0x00200042,
                mode="into",
                max_steps=1,
            )

            self.assertEqual(preview.stop_reason, "max-steps-reached")
            self.assertFalse(preview.reached_target)
            self.assertEqual(preview.emitted_count, 1)
            self.assertEqual(preview.terminal_pc, 0x00200041)


if __name__ == "__main__":
    unittest.main()
