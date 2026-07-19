"""Tests for stable event-log v1 helpers."""

from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from dataclasses import replace
from pathlib import Path

from core.event_log import (
    EVENT_LOG_FORMAT,
    EVENT_LOG_FORMAT_VERSION,
    build_event_log_payload,
    capture_event_log,
    diff_event_logs,
    load_event_log,
)
from core.fetch import load_fetch_view
from core.machine import load_machine_state
from core.savestate import build_savestate_payload, save_savestate
from ngpc_emu import main


class EventLogTests(unittest.TestCase):
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

    def test_build_event_log_payload_records_executed_steps(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x00200040, b"\x00\x00")
            view = load_fetch_view(rom_path)

            payload = build_event_log_payload(
                rom_path=rom_path,
                rom_header=view.machine.header,
                view=view,
                max_steps=2,
                note="smoke",
            )

            self.assertEqual(payload["format"], EVENT_LOG_FORMAT)
            self.assertEqual(payload["format_version"], EVENT_LOG_FORMAT_VERSION)
            summary = payload["summary"]
            assert isinstance(summary, dict)
            self.assertEqual(summary["executed_count"], 2)
            self.assertEqual(summary["emitted_count"], 2)
            self.assertEqual(summary["stop_reason"], "step-budget-exhausted")
            self.assertEqual(summary["final_cpu_pc_hex"], "0x00200042")
            events = payload["events"]
            assert isinstance(events, list)
            self.assertEqual(events[0]["assembly"], "nop")
            self.assertEqual(events[0]["status"], "executed")
            self.assertEqual(events[0]["next_pc_hex"], "0x00200041")
            self.assertEqual(payload["note"], "smoke")

    def test_build_event_log_payload_records_blocked_stop_quirk(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x00200040, b"\xD8\xB8")
            view = load_fetch_view(rom_path)

            payload = build_event_log_payload(
                rom_path=rom_path,
                rom_header=view.machine.header,
                view=view,
                max_steps=4,
            )

            summary = payload["summary"]
            assert isinstance(summary, dict)
            self.assertEqual(summary["executed_count"], 0)
            self.assertEqual(summary["emitted_count"], 1)
            self.assertEqual(summary["stop_reason"], "stopped-on-silicon-broken")
            quirk = summary["matched_quirk_on_stop"]
            assert isinstance(quirk, dict)
            self.assertEqual(quirk["quirk_id"], "cpu.d8_df_register_to_register")

    def test_build_event_log_payload_records_halt_with_advanced_final_pc(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x00200040, b"\x05\x00")
            view = load_fetch_view(rom_path)

            payload = build_event_log_payload(
                rom_path=rom_path,
                rom_header=view.machine.header,
                view=view,
                max_steps=4,
            )

            summary = payload["summary"]
            events = payload["events"]
            assert isinstance(summary, dict)
            assert isinstance(events, list)
            self.assertEqual(summary["executed_count"], 0)
            self.assertEqual(summary["emitted_count"], 1)
            self.assertEqual(summary["stop_reason"], "stopped-on-cpu-halted")
            self.assertEqual(summary["final_cpu_pc_hex"], "0x00200041")
            self.assertEqual(events[0]["status"], "cpu-halted")
            self.assertEqual(events[0]["next_pc_hex"], "0x00200041")

    def test_build_event_log_payload_can_use_auto_tick_to_reach_target(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            body = b"\xC1\x00\x40\x3F\x00\x66\xF9\x00"
            self._write_demo_rom(rom_path, 0x00200040, body)
            view = load_fetch_view(rom_path)

            payload = build_event_log_payload(
                rom_path=rom_path,
                rom_header=view.machine.header,
                view=view,
                target_pc=0x00200047,
                max_steps=8,
                memory_bytes={0x004000: 0x00},
                auto_tick_address=0x004000,
                auto_tick_period=1,
            )

            summary = payload["summary"]
            run_context = payload["run_context"]
            assert isinstance(summary, dict)
            assert isinstance(run_context, dict)
            self.assertEqual(summary["stop_reason"], "target-reached")
            self.assertEqual(summary["executed_count"], 4)
            self.assertEqual(summary["final_cpu_pc_hex"], "0x00200047")
            self.assertEqual(run_context["auto_tick_address_hex"], "0x004000")
            self.assertEqual(run_context["auto_tick_period"], 1)

    def test_capture_and_load_event_log_roundtrip(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            log_path = Path(tmpdir) / "demo_eventlog.json"
            self._write_demo_rom(rom_path, 0x00200040, b"\x00")

            payload = capture_event_log(
                rom_path,
                output_path=log_path,
                max_steps=1,
                seed_from_savestate={
                    "format_version": "2026-04-22.v1",
                    "rom_sha256": "abc",
                    "cpu_pc": 0x00200040,
                },
            )
            loaded = load_event_log(log_path, expected_rom_path=rom_path)

            self.assertEqual(loaded["format"], EVENT_LOG_FORMAT)
            self.assertEqual(loaded["format_version"], EVENT_LOG_FORMAT_VERSION)
            run_context = loaded["run_context"]
            assert isinstance(run_context, dict)
            self.assertEqual(run_context["seed_from_savestate"], payload["run_context"]["seed_from_savestate"])

    def test_load_event_log_rejects_rom_hash_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_a = Path(tmpdir) / "a.ngc"
            rom_b = Path(tmpdir) / "b.ngc"
            log_path = Path(tmpdir) / "demo_eventlog.json"
            self._write_demo_rom(rom_a, 0x00200040, b"\x00")
            self._write_demo_rom(rom_b, 0x00200040, b"\x0E")

            capture_event_log(rom_a, output_path=log_path, max_steps=1)

            with self.assertRaisesRegex(ValueError, "ROM hash mismatch"):
                load_event_log(log_path, expected_rom_path=rom_b)

    def test_diff_event_logs_reports_first_event_divergence(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x00200040, b"\x0E")
            view = load_fetch_view(rom_path)
            seeded_cpu = replace(
                view.machine.cpu,
                regs=replace(view.machine.cpu.regs, xsp=0x00004100),
            )

            left = build_event_log_payload(
                rom_path=rom_path,
                rom_header=view.machine.header,
                view=view,
                max_steps=1,
                cpu_state=seeded_cpu,
                memory_bytes={
                    0x004100: 0x34,
                    0x004101: 0x12,
                    0x004102: 0x20,
                    0x004103: 0x00,
                },
            )
            right = build_event_log_payload(
                rom_path=rom_path,
                rom_header=view.machine.header,
                view=view,
                max_steps=1,
                cpu_state=seeded_cpu,
            )

            diff = diff_event_logs(left, right)

            first = diff["first_divergence"]
            assert isinstance(first, dict)
            self.assertEqual(first["kind"], "event")
            self.assertEqual(first["index"], 0)

    def test_eventlog_capture_cli_can_report_non_reference_auto_tick(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            rom_path = tmp / "demo.ngc"
            log_path = tmp / "demo.eventlog.json"
            state_path = tmp / "seed.state.json"
            body = b"\xC1\x00\x40\x3F\x00\x66\xF9\x00"
            self._write_demo_rom(rom_path, 0x00200040, body)
            machine = load_machine_state(rom_path)
            save_savestate(
                state_path,
                build_savestate_payload(
                    rom_path=rom_path,
                    rom_header=machine.header,
                    cpu=machine.cpu,
                    writable_overlay={0x004000: 0x00},
                ),
            )

            stdout = io.StringIO()
            with redirect_stdout(stdout):
                exit_code = main(
                    [
                        "eventlog",
                        "capture",
                        str(rom_path),
                        str(log_path),
                        "--seed-from",
                        str(state_path),
                        "--run-until",
                        "0x00200047",
                        "--max-steps",
                        "8",
                        "--auto-tick-addr",
                        "0x4000",
                        "--auto-tick-period",
                        "1",
                        "--json",
                    ]
                )
            self.assertEqual(exit_code, 0)
            payload = json.loads(stdout.getvalue())
            self.assertEqual(payload["stop_reason"], "target-reached")
            self.assertEqual(payload["final_cpu_pc_hex"], "0x00200047")
            self.assertEqual(payload["non_reference"]["address_hex"], "0x004000")

            loaded = load_event_log(log_path, expected_rom_path=rom_path)
            run_context = loaded["run_context"]
            assert isinstance(run_context, dict)
            self.assertEqual(run_context["auto_tick_address_hex"], "0x004000")
            self.assertEqual(run_context["auto_tick_period"], 1)

    def test_eventlog_check_cli_returns_zero_on_identical_run(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            rom_path = tmp / "demo.ngc"
            golden_path = tmp / "golden.eventlog.json"
            self._write_demo_rom(rom_path, 0x00200040, b"\x00\x00")

            stdout = io.StringIO()
            with redirect_stdout(stdout):
                exit_code = main(
                    [
                        "eventlog",
                        "capture",
                        str(rom_path),
                        str(golden_path),
                        "--count",
                        "2",
                        "--json",
                    ]
                )
            self.assertEqual(exit_code, 0)

            stdout = io.StringIO()
            with redirect_stdout(stdout):
                exit_code = main(
                    [
                        "eventlog",
                        "check",
                        str(rom_path),
                        str(golden_path),
                        "--count",
                        "2",
                        "--json",
                    ]
                )
            self.assertEqual(exit_code, 0)
            payload = json.loads(stdout.getvalue())
            self.assertEqual(payload["status"], "match")
            self.assertIsNone(payload["diff"]["first_divergence"])

    def test_eventlog_check_cli_returns_one_on_event_divergence(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            rom_path = tmp / "demo.ngc"
            state_path = tmp / "seed.state.json"
            golden_path = tmp / "golden.eventlog.json"
            current_path = tmp / "current.eventlog.json"
            self._write_demo_rom(rom_path, 0x00200040, b"\x0E")

            machine = load_machine_state(rom_path)
            seeded_cpu = replace(
                machine.cpu,
                regs=replace(machine.cpu.regs, xsp=0x00004100),
            )
            save_savestate(
                state_path,
                build_savestate_payload(
                    rom_path=rom_path,
                    rom_header=machine.header,
                    cpu=seeded_cpu,
                    writable_overlay={
                        0x004100: 0x50,
                        0x004101: 0x00,
                        0x004102: 0x20,
                        0x004103: 0x00,
                    },
                ),
            )

            stdout = io.StringIO()
            with redirect_stdout(stdout):
                exit_code = main(
                    [
                        "eventlog",
                        "capture",
                        str(rom_path),
                        str(golden_path),
                        "--seed-from",
                        str(state_path),
                        "--count",
                        "1",
                        "--json",
                    ]
                )
            self.assertEqual(exit_code, 0)

            save_savestate(
                state_path,
                build_savestate_payload(
                    rom_path=rom_path,
                    rom_header=machine.header,
                    cpu=seeded_cpu,
                    writable_overlay={
                        0x004100: 0x60,
                        0x004101: 0x00,
                        0x004102: 0x20,
                        0x004103: 0x00,
                    },
                ),
            )

            stdout = io.StringIO()
            with redirect_stdout(stdout):
                exit_code = main(
                    [
                        "eventlog",
                        "check",
                        str(rom_path),
                        str(golden_path),
                        "--seed-from",
                        str(state_path),
                        "--count",
                        "1",
                        "--save-current",
                        str(current_path),
                        "--json",
                    ]
                )
            self.assertEqual(exit_code, 1)
            payload = json.loads(stdout.getvalue())
            self.assertEqual(payload["status"], "mismatch")
            self.assertEqual(payload["diff"]["first_divergence"]["kind"], "event")
            self.assertEqual(payload["saved_current"], str(current_path))
            self.assertTrue(current_path.exists())


if __name__ == "__main__":
    unittest.main()
