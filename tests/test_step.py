"""Minimal static step-preview tests for NgpCraft Emulator."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from core.fetch import load_fetch_view
from core.step import build_step_preview


class StepPreviewTests(unittest.TestCase):
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

    def test_step_preview_uses_sequential_pc_for_non_control_flow(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x00200040, b"\x08\x6F\x4E")
            view = load_fetch_view(rom_path)

            preview = build_step_preview(view)

            self.assertEqual(preview.decode.assembly, "ldb (HW_WATCHDOG), 0x4E")
            self.assertEqual(preview.mode, "into")
            self.assertEqual(preview.preview_target, 0x00200043)
            self.assertEqual(preview.reason, "sequential-non-control-flow")

    def test_step_preview_uses_direct_call_target(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x00200040, b"\x1D\x2F\x91\x20")
            view = load_fetch_view(rom_path)

            preview = build_step_preview(view)

            self.assertEqual(preview.decode.assembly, "call 0x20912F")
            self.assertEqual(preview.mode, "into")
            self.assertEqual(preview.preview_target, 0x0020912F)
            self.assertEqual(preview.reason, "direct-call-target")

    def test_step_preview_keeps_conditional_branch_unresolved(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x00200040, b"\x66\x16")
            view = load_fetch_view(rom_path)

            preview = build_step_preview(view)

            self.assertEqual(preview.decode.assembly, "jr Z, 0x200058")
            self.assertIsNone(preview.preview_target)
            self.assertEqual(preview.reason, "conditional-control-flow-unresolved")

    def test_step_preview_leaves_return_unresolved(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x00200040, b"\x0E")
            view = load_fetch_view(rom_path)

            preview = build_step_preview(view)

            self.assertEqual(preview.decode.assembly, "ret")
            self.assertIsNone(preview.preview_target)
            self.assertEqual(preview.reason, "runtime-control-flow-unresolved")

    def test_next_preview_uses_return_site_for_calls(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x00200040, b"\x1D\x2F\x91\x20")
            view = load_fetch_view(rom_path)

            preview = build_step_preview(view, mode="over")

            self.assertEqual(preview.decode.assembly, "call 0x20912F")
            self.assertEqual(preview.mode, "over")
            self.assertEqual(preview.preview_target, 0x00200044)
            self.assertEqual(preview.reason, "call-return-site-preview")

    def test_next_preview_uses_sequential_pc_for_non_control_flow(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x00200040, b"\x08\x6F\x4E")
            view = load_fetch_view(rom_path)

            preview = build_step_preview(view, mode="over")

            self.assertEqual(preview.decode.assembly, "ldb (HW_WATCHDOG), 0x4E")
            self.assertEqual(preview.mode, "over")
            self.assertEqual(preview.preview_target, 0x00200043)
            self.assertEqual(preview.reason, "sequential-non-control-flow")

    def test_next_preview_keeps_conditional_branch_unresolved(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x00200040, b"\x66\x16")
            view = load_fetch_view(rom_path)

            preview = build_step_preview(view, mode="over")

            self.assertEqual(preview.decode.assembly, "jr Z, 0x200058")
            self.assertEqual(preview.mode, "over")
            self.assertIsNone(preview.preview_target)
            self.assertEqual(preview.reason, "conditional-control-flow-unresolved")


if __name__ == "__main__":
    unittest.main()
