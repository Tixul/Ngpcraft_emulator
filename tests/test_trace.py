"""Minimal linear trace preview tests for NgpCraft Emulator."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from core.fetch import load_fetch_view
from core.trace import build_trace_preview


class TracePreviewTests(unittest.TestCase):
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

    def test_trace_preview_walks_sequential_instructions(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(
                rom_path,
                0x00200040,
                b"\x08\x6F\x4E\x47\x00\x60\x00\x00\xC8\xE1",
            )
            view = load_fetch_view(rom_path)

            preview = build_trace_preview(view, count=3)

            self.assertEqual(preview.start_pc, 0x00200040)
            self.assertEqual(preview.requested_count, 3)
            self.assertEqual(preview.emitted_count, 3)
            self.assertEqual(preview.stop_reason, "count-reached")
            self.assertEqual(preview.records[0].decode.assembly, "ldb (HW_WATCHDOG), 0x4E")
            self.assertEqual(preview.records[1].decode.assembly, "ld XSP, 0x00006000")
            self.assertEqual(preview.records[2].decode.assembly, "or A, W")

    def test_trace_preview_stops_on_unknown_opcode(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(
                rom_path,
                0x00200040,
                b"\x08\x6F\x4E\x1F",
            )
            view = load_fetch_view(rom_path)

            preview = build_trace_preview(view, count=3)

            self.assertEqual(preview.emitted_count, 2)
            self.assertEqual(preview.stop_reason, "stopped-on-unknown-opcode")
            self.assertEqual(preview.records[0].decode.assembly, "ldb (HW_WATCHDOG), 0x4E")
            self.assertEqual(preview.records[1].decode.status, "unknown-opcode")

    def test_trace_preview_can_start_from_explicit_address(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(
                rom_path,
                0x00200040,
                b"\x08\x6F\x4E\x47\x00\x60\x00\x00\xC8\xE1",
            )
            view = load_fetch_view(rom_path)

            preview = build_trace_preview(view, count=2, start_pc=0x00200043)

            self.assertEqual(preview.start_pc, 0x00200043)
            self.assertEqual(preview.records[0].decode.assembly, "ld XSP, 0x00006000")
            self.assertEqual(preview.records[1].decode.assembly, "or A, W")

    def test_trace_preview_can_stop_on_control_flow(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(
                rom_path,
                0x00200040,
                b"\x08\x6F\x4E\x66\x02\x47\x00\x60\x00\x00",
            )
            view = load_fetch_view(rom_path)

            preview = build_trace_preview(view, count=5, stop_on_control_flow=True)

            self.assertEqual(preview.emitted_count, 2)
            self.assertEqual(preview.stop_reason, "stopped-on-control-flow")
            self.assertEqual(preview.records[0].decode.assembly, "ldb (HW_WATCHDOG), 0x4E")
            self.assertEqual(preview.records[1].decode.assembly, "jr Z, 0x200047")

    def test_trace_preview_stops_on_known_silicon_broken_opcode(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(
                rom_path,
                0x00200040,
                b"\x08\x6F\x4E\xD8\xB8\xC7\xEA",
            )
            view = load_fetch_view(rom_path)

            preview = build_trace_preview(view, count=5)

            self.assertEqual(preview.emitted_count, 2)
            self.assertEqual(preview.stop_reason, "stopped-on-silicon-broken")
            self.assertEqual(preview.records[0].decode.assembly, "ldb (HW_WATCHDOG), 0x4E")
            # `D8 B8` = `ex WA, WA`, in the 0xB8..0xBF pocket of the D8..DF word
            # register family -- the one range never cleared on real silicon. It
            # replaces the retired `D0 FA` fixture (see core/quirks_db.json v13).
            self.assertEqual(preview.records[1].decode.assembly, "ex WA, WA")


if __name__ == "__main__":
    unittest.main()
