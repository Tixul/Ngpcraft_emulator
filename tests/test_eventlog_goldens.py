"""Named golden event-log tests for NgpCraft Emulator."""

from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from dataclasses import replace
from pathlib import Path

from core.goldens import load_named_golden
from core.machine import load_machine_state
from core.savestate import build_savestate_payload, save_savestate
from ngpc_emu import main


class EventLogGoldenTests(unittest.TestCase):
    def _write_demo_rom(self, path: Path, entry_point: int, body: bytes) -> None:
        data = bytearray(0x40)
        data[0x00:0x1C] = b"LICENSED BY SNK CORPORATION".ljust(0x1C, b"\x00")
        data[0x1C:0x20] = entry_point.to_bytes(4, "little")
        data[0x20:0x22] = (0x0000).to_bytes(2, "little")
        data[0x22] = 0
        data[0x23] = 0x10
        data[0x24:0x30] = b"GOLDEN TEST\x00"
        body_offset = entry_point - 0x00200000
        if body_offset < len(data):
            data[body_offset : body_offset + len(body)] = body
        else:
            data.extend(b"\x00" * (body_offset - len(data)))
            data.extend(body)
        path.write_bytes(bytes(data))

    def test_eventlog_golden_cli_save_list_load_delete(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            rom_path = tmp / "demo.ngc"
            self._write_demo_rom(rom_path, 0x00200040, b"\x00\x00\x00")

            stdout = io.StringIO()
            with redirect_stdout(stdout):
                exit_code = main(
                    [
                        "eventlog",
                        "golden-save",
                        str(rom_path),
                        "boot-path",
                        "--count",
                        "2",
                        "--json",
                    ]
                )
            self.assertEqual(exit_code, 0)
            save_payload = json.loads(stdout.getvalue())
            self.assertEqual(save_payload["name"], "boot-path")
            self.assertEqual(save_payload["executed_count"], 2)
            self.assertTrue(Path(save_payload["path"]).exists())

            stdout = io.StringIO()
            with redirect_stdout(stdout):
                exit_code = main(["eventlog", "golden-list", str(rom_path), "--json"])
            self.assertEqual(exit_code, 0)
            list_payload = json.loads(stdout.getvalue())
            self.assertEqual(list_payload["count"], 1)
            self.assertEqual(list_payload["goldens"][0]["name"], "boot-path")

            stdout = io.StringIO()
            with redirect_stdout(stdout):
                exit_code = main(
                    ["eventlog", "golden-load", str(rom_path), "boot-path", "--json"]
                )
            self.assertEqual(exit_code, 0)
            load_payload = json.loads(stdout.getvalue())
            self.assertEqual(load_payload["final_cpu_pc_hex"], "0x00200042")

            golden = load_named_golden(rom_path, "boot-path")
            self.assertEqual(golden.payload["summary"]["final_cpu_pc_hex"], "0x00200042")

            stdout = io.StringIO()
            with redirect_stdout(stdout):
                exit_code = main(
                    ["eventlog", "golden-delete", str(rom_path), "boot-path", "--json"]
                )
            self.assertEqual(exit_code, 0)
            delete_payload = json.loads(stdout.getvalue())
            self.assertEqual(delete_payload["name"], "boot-path")
            self.assertFalse(Path(delete_payload["deleted_path"]).exists())

    def test_eventlog_golden_check_cli_returns_zero_on_identical_run(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            rom_path = tmp / "demo.ngc"
            self._write_demo_rom(rom_path, 0x00200040, b"\x00\x00")

            stdout = io.StringIO()
            with redirect_stdout(stdout):
                exit_code = main(
                    [
                        "eventlog",
                        "golden-save",
                        str(rom_path),
                        "baseline",
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
                        "golden-check",
                        str(rom_path),
                        "baseline",
                        "--count",
                        "2",
                        "--json",
                    ]
                )
            self.assertEqual(exit_code, 0)
            payload = json.loads(stdout.getvalue())
            self.assertEqual(payload["status"], "match")
            self.assertIsNone(payload["diff"]["first_divergence"])

    def test_eventlog_golden_check_cli_returns_one_on_event_divergence(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            rom_path = tmp / "demo.ngc"
            state_path = tmp / "seed.state.json"
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
                        "golden-save",
                        str(rom_path),
                        "ret-baseline",
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
                        "golden-check",
                        str(rom_path),
                        "ret-baseline",
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
            self.assertEqual(payload["golden_name"], "ret-baseline")
            self.assertEqual(payload["diff"]["first_divergence"]["kind"], "event")
            self.assertEqual(payload["saved_current"], str(current_path))
            self.assertTrue(current_path.exists())


class EventLogGoldenCheckAllTests(unittest.TestCase):
    """Pass 27 — `eventlog golden-check-all` batch CI workflow."""

    def _write_demo_rom(self, path: Path, entry_point: int, body: bytes) -> None:
        data = bytearray(0x40)
        data[0x00:0x1C] = b"LICENSED BY SNK CORPORATION".ljust(0x1C, b"\x00")
        data[0x1C:0x20] = entry_point.to_bytes(4, "little")
        data[0x20:0x22] = (0x0000).to_bytes(2, "little")
        data[0x22] = 0
        data[0x23] = 0x10
        data[0x24:0x30] = b"GOLDEN ALL\x00\x00"
        body_offset = entry_point - 0x00200000
        if body_offset < len(data):
            data[body_offset : body_offset + len(body)] = body
        else:
            data.extend(b"\x00" * (body_offset - len(data)))
            data.extend(body)
        path.write_bytes(bytes(data))

    def test_empty_registry_exits_0_with_zero_total(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            rom_path = tmp / "demo.ngc"
            self._write_demo_rom(rom_path, 0x00200040, b"\x00\x00\x00")

            stdout = io.StringIO()
            with redirect_stdout(stdout):
                exit_code = main(
                    [
                        "eventlog", "golden-check-all", str(rom_path),
                        "--count", "2", "--json",
                    ],
                )
            self.assertEqual(exit_code, 0)
            payload = json.loads(stdout.getvalue())
            self.assertEqual(payload["total"], 0)
            self.assertEqual(payload["passed"], 0)
            self.assertEqual(payload["failed"], 0)
            self.assertTrue(payload["all_equal"])
            self.assertEqual(payload["results"], [])

    def test_two_matching_goldens_exit_0(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            rom_path = tmp / "demo.ngc"
            self._write_demo_rom(rom_path, 0x00200040, b"\x00\x00\x00")

            # Save two identical goldens (same capture config).
            for name in ("first", "second"):
                with redirect_stdout(io.StringIO()):
                    code = main(
                        [
                            "eventlog", "golden-save", str(rom_path), name,
                            "--count", "2",
                        ],
                    )
                self.assertEqual(code, 0)

            stdout = io.StringIO()
            with redirect_stdout(stdout):
                exit_code = main(
                    [
                        "eventlog", "golden-check-all", str(rom_path),
                        "--count", "2", "--json",
                    ],
                )
            self.assertEqual(exit_code, 0)
            payload = json.loads(stdout.getvalue())
            self.assertEqual(payload["total"], 2)
            self.assertEqual(payload["passed"], 2)
            self.assertEqual(payload["failed"], 0)
            self.assertTrue(payload["all_equal"])
            statuses = {r["name"]: r["status"] for r in payload["results"]}
            self.assertEqual(statuses, {"first": "match", "second": "match"})

    def test_count_mismatch_against_golden_reports_failed(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            rom_path = tmp / "demo.ngc"
            self._write_demo_rom(rom_path, 0x00200040, b"\x00\x00\x00\x00")

            # Save a golden captured with --count 2.
            with redirect_stdout(io.StringIO()):
                main(
                    [
                        "eventlog", "golden-save", str(rom_path), "two-step",
                        "--count", "2",
                    ],
                )

            # Run check-all with --count 4 → length divergence.
            stdout = io.StringIO()
            with redirect_stdout(stdout):
                exit_code = main(
                    [
                        "eventlog", "golden-check-all", str(rom_path),
                        "--count", "4", "--json",
                    ],
                )
            self.assertEqual(exit_code, 1)
            payload = json.loads(stdout.getvalue())
            self.assertEqual(payload["passed"], 0)
            self.assertEqual(payload["failed"], 1)
            self.assertFalse(payload["all_equal"])
            divergence = payload["results"][0]["first_divergence"]
            # Differing --count parameters change the run_context — the
            # diff surfaces that as the first divergence kind.
            self.assertIn(divergence["kind"], ("run_context", "length"))

    def test_stop_on_fail_short_circuits_after_first_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            rom_path = tmp / "demo.ngc"
            self._write_demo_rom(rom_path, 0x00200040, b"\x00\x00\x00\x00")

            # 'a' captured at count=4 (will match), 'b' captured at count=2
            # (will mismatch on length), 'c' captured at count=4 (would
            # match, but should be skipped by --stop-on-fail after 'b' fails).
            with redirect_stdout(io.StringIO()):
                main(["eventlog", "golden-save", str(rom_path), "a",
                      "--count", "4"])
                main(["eventlog", "golden-save", str(rom_path), "b",
                      "--count", "2"])
                main(["eventlog", "golden-save", str(rom_path), "c",
                      "--count", "4"])

            stdout = io.StringIO()
            with redirect_stdout(stdout):
                exit_code = main(
                    [
                        "eventlog", "golden-check-all", str(rom_path),
                        "--count", "4", "--stop-on-fail", "--json",
                    ],
                )
            self.assertEqual(exit_code, 1)
            payload = json.loads(stdout.getvalue())
            self.assertEqual(payload["total"], 3)
            # Sorted alphabetically by slug: a (match), b (mismatch), STOP.
            self.assertEqual(payload["checked"], 2)
            self.assertEqual(payload["passed"], 1)
            self.assertEqual(payload["failed"], 1)
            self.assertTrue(payload["stopped_early"])
            checked = [r["name"] for r in payload["results"]]
            self.assertEqual(checked, ["a", "b"])

    def test_save_current_writes_captured_log(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            rom_path = tmp / "demo.ngc"
            self._write_demo_rom(rom_path, 0x00200040, b"\x00\x00")
            current_path = tmp / "current.eventlog.json"

            with redirect_stdout(io.StringIO()):
                main(["eventlog", "golden-save", str(rom_path), "anchor",
                      "--count", "2"])
                exit_code = main(
                    [
                        "eventlog", "golden-check-all", str(rom_path),
                        "--count", "2",
                        "--save-current", str(current_path),
                    ],
                )
            self.assertEqual(exit_code, 0)
            self.assertTrue(current_path.exists())
            # The saved current log should parse as JSON.
            current_data = json.loads(current_path.read_text(encoding="utf-8"))
            self.assertIn("format_version", current_data)

    def test_human_summary_marks_each_golden(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            rom_path = tmp / "demo.ngc"
            self._write_demo_rom(rom_path, 0x00200040, b"\x00\x00\x00\x00")

            with redirect_stdout(io.StringIO()):
                main(["eventlog", "golden-save", str(rom_path), "ok-one",
                      "--count", "4"])
                main(["eventlog", "golden-save", str(rom_path), "bad-one",
                      "--count", "2"])

            stdout = io.StringIO()
            with redirect_stdout(stdout):
                exit_code = main(
                    [
                        "eventlog", "golden-check-all", str(rom_path),
                        "--count", "4",
                    ],
                )
            self.assertEqual(exit_code, 1)
            text = stdout.getvalue()
            self.assertIn("[OK]", text)
            self.assertIn("[MISMATCH]", text)
            self.assertIn("ok-one", text)
            self.assertIn("bad-one", text)


if __name__ == "__main__":
    unittest.main()
