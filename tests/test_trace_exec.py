"""Minimal real execution-trace tests for NgpCraft Emulator."""

from __future__ import annotations

from dataclasses import replace
import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from core.machine import load_machine_state
from core.savestate import build_savestate_payload, load_savestate, save_savestate
from core.fetch import load_fetch_view
from core.trace_exec import build_execution_trace, load_execution_trace
from ngpc_emu import main


class ExecutionTraceTests(unittest.TestCase):
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

    def test_execution_trace_records_real_executed_steps(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(
                rom_path,
                0x00200040,
                b"\x47\x00\x60\x00\x00\x00",
            )
            view = load_fetch_view(rom_path)

            trace = build_execution_trace(view, count=2)

            self.assertEqual(trace.start_pc, 0x00200040)
            self.assertEqual(trace.requested_count, 2)
            self.assertEqual(trace.emitted_count, 2)
            self.assertEqual(trace.executed_count, 2)
            self.assertEqual(trace.stop_reason, "count-reached")
            self.assertEqual(trace.records[0].execution.decode.assembly, "ld XSP, 0x00006000")
            self.assertEqual(trace.records[1].execution.decode.assembly, "nop")

    def test_execution_trace_stops_on_first_blocked_instruction(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(
                rom_path,
                0x00200040,
                b"\x30\x34\x12",
            )
            view = load_fetch_view(rom_path)

            trace = build_execution_trace(view, count=3)

            self.assertEqual(trace.emitted_count, 1)
            self.assertEqual(trace.executed_count, 0)
            self.assertEqual(trace.stop_reason, "stopped-on-requires-known-full-register")
            self.assertEqual(trace.records[0].execution.decode.assembly, "ld WA, 0x1234")
            self.assertEqual(trace.records[0].execution.status, "requires-known-full-register")

    def test_execution_trace_reports_silicon_broken_frontier(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(
                rom_path,
                0x00200040,
                b"\x47\x00\x60\x00\x00\xD8\xB8",
            )
            view = load_fetch_view(rom_path)

            trace = build_execution_trace(view, count=3)

            self.assertEqual(trace.emitted_count, 2)
            self.assertEqual(trace.executed_count, 1)
            self.assertEqual(trace.stop_reason, "stopped-on-silicon-broken")
            self.assertEqual(trace.records[1].execution.decode.assembly, "ex WA, WA")
            self.assertEqual(trace.records[1].execution.status, "silicon-broken")
            self.assertIsNotNone(trace.records[1].execution.matched_quirk)
            assert trace.records[1].execution.matched_quirk is not None
            self.assertEqual(
                trace.records[1].execution.matched_quirk.quirk_id,
                "cpu.d8_df_register_to_register",
            )

    def test_load_execution_trace_can_resume_from_savestate_seed(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x00200040, b"\x00\x00\xD8\xB8")
            machine = load_machine_state(rom_path)
            seeded_cpu = replace(machine.cpu, pc=0x00200041)
            overlay = {0x004100: 0xAA, 0x004101: 0xBB}
            payload = build_savestate_payload(
                rom_path=rom_path,
                rom_header=machine.header,
                cpu=seeded_cpu,
                writable_overlay=overlay,
            )
            state_path = Path(tmpdir) / "seed.json"
            save_savestate(state_path, payload)
            loaded = load_savestate(state_path, expected_rom_path=rom_path)

            trace = load_execution_trace(
                rom_path,
                count=2,
                initial_cpu_state=loaded.cpu,
                initial_memory_bytes=loaded.writable_overlay,
            )

            self.assertEqual(trace.start_pc, 0x00200041)
            self.assertEqual(trace.executed_count, 1)
            self.assertEqual(trace.stop_reason, "stopped-on-silicon-broken")
            self.assertEqual(trace.final_cpu.pc, 0x00200042)
            self.assertEqual(trace.final_memory.get(0x004100), 0xAA)
            self.assertEqual(trace.final_memory.get(0x004101), 0xBB)

    def test_trace_exec_cli_can_save_and_resume_savestate(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            rom_path = tmp / "demo.ngc"
            self._write_demo_rom(rom_path, 0x00200040, b"\x1D\x50\x00\x20\x00" + (b"\x00" * 11))
            state1 = tmp / "trace1.json"
            state2 = tmp / "trace2.json"

            stdout = io.StringIO()
            with redirect_stdout(stdout):
                exit_code = main(
                    [
                        "trace-exec",
                        str(rom_path),
                        "--count",
                        "1",
                        "--seed-xsp",
                        "0x4100",
                        "--save-state",
                        str(state1),
                        "--json",
                    ]
                )
            self.assertEqual(exit_code, 0)
            payload1 = json.loads(stdout.getvalue())
            self.assertEqual(payload1["executed_count"], 1)
            self.assertEqual(payload1["saved_state"]["cpu_pc_hex"], "0x00200050")
            first = load_savestate(state1, expected_rom_path=rom_path)
            self.assertEqual(first.cpu.pc, 0x00200050)

            stdout = io.StringIO()
            with redirect_stdout(stdout):
                exit_code = main(
                    [
                        "trace-exec",
                        str(rom_path),
                        "--count",
                        "1",
                        "--seed-from",
                        str(state1),
                        "--save-state",
                        str(state2),
                        "--json",
                    ]
                )
            self.assertEqual(exit_code, 0)
            payload2 = json.loads(stdout.getvalue())
            self.assertEqual(payload2["start_pc_hex"], "0x00200050")
            self.assertEqual(payload2["executed_count"], 1)
            second = load_savestate(state2, expected_rom_path=rom_path)
            self.assertEqual(second.cpu.pc, 0x00200051)
            self.assertIn("trace-exec", second.note or "")


if __name__ == "__main__":
    unittest.main()
