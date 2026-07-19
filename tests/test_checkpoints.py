"""Named checkpoint tests for NgpCraft Emulator."""

from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from core.checkpoints import (
    checkpoint_path_for_rom,
    delete_named_checkpoint,
    list_named_checkpoints,
    load_named_checkpoint,
    save_named_checkpoint,
)
from core.event_log import load_event_log
from core.machine import load_machine_state
from core.savestate import build_savestate_payload
from ngpc_emu import main


class NamedCheckpointTests(unittest.TestCase):
    def _write_demo_rom(self, path: Path, entry_point: int, body: bytes) -> None:
        data = bytearray(0x40)
        data[0x00:0x1C] = b"LICENSED BY SNK CORPORATION".ljust(0x1C, b"\x00")
        data[0x1C:0x20] = entry_point.to_bytes(4, "little")
        data[0x20:0x22] = (0x0000).to_bytes(2, "little")
        data[0x22] = 0
        data[0x23] = 0x10
        data[0x24:0x30] = b"CHECKPOINT\x00\x00"
        body_offset = entry_point - 0x00200000
        if body_offset < len(data):
            data[body_offset : body_offset + len(body)] = body
        else:
            data.extend(b"\x00" * (body_offset - len(data)))
            data.extend(body)
        path.write_bytes(bytes(data))

    def test_core_named_checkpoint_helpers_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x00200040, b"\x00")
            machine = load_machine_state(rom_path)
            payload = build_savestate_payload(
                rom_path=rom_path,
                rom_header=machine.header,
                cpu=machine.cpu,
                writable_overlay={},
                note="helper roundtrip",
            )

            checkpoint_path = checkpoint_path_for_rom(rom_path, "alpha checkpoint")
            save_named_checkpoint(checkpoint_path, payload)

            loaded = load_named_checkpoint(rom_path, "alpha checkpoint")
            self.assertEqual(loaded.document.cpu.pc, 0x00200040)
            self.assertEqual(loaded.path, checkpoint_path)

            listed = list_named_checkpoints(rom_path)
            self.assertEqual(len(listed), 1)
            self.assertEqual(listed[0].slug, "alpha-checkpoint")

            deleted_path = delete_named_checkpoint(rom_path, "alpha checkpoint")
            self.assertEqual(deleted_path, checkpoint_path)
            self.assertFalse(checkpoint_path.exists())

    def test_checkpoint_cli_save_list_load_delete(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            rom_path = tmp / "demo.ngc"
            self._write_demo_rom(rom_path, 0x00200040, b"\x00\x00\x00")

            stdout = io.StringIO()
            with redirect_stdout(stdout):
                exit_code = main(
                    [
                        "checkpoint",
                        "save",
                        str(rom_path),
                        "alpha",
                        "--run-until",
                        "0x00200042",
                        "--max-steps",
                        "4",
                        "--json",
                    ]
                )
            self.assertEqual(exit_code, 0)
            saved_payload = json.loads(stdout.getvalue())
            self.assertEqual(saved_payload["name"], "alpha")
            self.assertEqual(saved_payload["cpu_pc_hex"], "0x00200042")

            stdout = io.StringIO()
            with redirect_stdout(stdout):
                exit_code = main(["checkpoint", "list", str(rom_path), "--json"])
            self.assertEqual(exit_code, 0)
            list_payload = json.loads(stdout.getvalue())
            self.assertEqual(list_payload["count"], 1)
            self.assertEqual(list_payload["checkpoints"][0]["name"], "alpha")

            stdout = io.StringIO()
            with redirect_stdout(stdout):
                exit_code = main(["checkpoint", "load", str(rom_path), "alpha", "--json"])
            self.assertEqual(exit_code, 0)
            load_payload = json.loads(stdout.getvalue())
            self.assertEqual(load_payload["cpu_pc_hex"], "0x00200042")

            stdout = io.StringIO()
            with redirect_stdout(stdout):
                exit_code = main(["checkpoint", "delete", str(rom_path), "alpha", "--json"])
            self.assertEqual(exit_code, 0)
            delete_payload = json.loads(stdout.getvalue())
            self.assertEqual(delete_payload["name"], "alpha")

    def test_step_exec_cli_can_use_save_checkpoint_and_seed_checkpoint(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            rom_path = tmp / "demo.ngc"
            self._write_demo_rom(rom_path, 0x00200040, b"\x1D\x50\x00\x20\x00" + (b"\x00" * 11))

            stdout = io.StringIO()
            with redirect_stdout(stdout):
                exit_code = main(
                    [
                        "step-exec",
                        str(rom_path),
                        "--seed-xsp",
                        "0x4100",
                        "--save-checkpoint",
                        "first",
                        "--json",
                    ]
                )
            self.assertEqual(exit_code, 0)
            first_payload = json.loads(stdout.getvalue())
            self.assertEqual(first_payload["saved_state"]["kind"], "named-checkpoint")
            first = load_named_checkpoint(rom_path, "first")
            self.assertEqual(first.document.cpu.pc, 0x00200050)

            stdout = io.StringIO()
            with redirect_stdout(stdout):
                exit_code = main(
                    [
                        "step-exec",
                        str(rom_path),
                        "--seed-checkpoint",
                        "first",
                        "--save-checkpoint",
                        "second",
                        "--json",
                    ]
                )
            self.assertEqual(exit_code, 0)
            second_payload = json.loads(stdout.getvalue())
            self.assertEqual(second_payload["seed_from"]["kind"], "named-checkpoint")
            second = load_named_checkpoint(rom_path, "second")
            self.assertEqual(second.document.cpu.pc, 0x00200051)

    def test_eventlog_capture_cli_can_use_seed_checkpoint(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            rom_path = tmp / "demo.ngc"
            log_path = tmp / "demo.eventlog.json"
            self._write_demo_rom(rom_path, 0x00200040, b"\x00\x00")

            stdout = io.StringIO()
            with redirect_stdout(stdout):
                exit_code = main(
                    [
                        "checkpoint",
                        "save",
                        str(rom_path),
                        "alpha",
                        "--json",
                    ]
                )
            self.assertEqual(exit_code, 0)

            stdout = io.StringIO()
            with redirect_stdout(stdout):
                exit_code = main(
                    [
                        "eventlog",
                        "capture",
                        str(rom_path),
                        str(log_path),
                        "--count",
                        "1",
                        "--seed-checkpoint",
                        "alpha",
                        "--json",
                    ]
                )
            self.assertEqual(exit_code, 0)
            payload = json.loads(stdout.getvalue())
            self.assertEqual(payload["saved_to"], str(log_path))
            self.assertEqual(payload["seed_from"]["kind"], "named-checkpoint")
            self.assertEqual(payload["seed_from"]["name"], "alpha")

            loaded = load_event_log(log_path, expected_rom_path=rom_path)
            run_context = loaded["run_context"]
            assert isinstance(run_context, dict)
            seed_from = run_context["seed_from_savestate"]
            assert isinstance(seed_from, dict)
            self.assertEqual(seed_from["kind"], "named-checkpoint")
            self.assertEqual(seed_from["name"], "alpha")
            self.assertEqual(seed_from["cpu_pc_hex"], "0x00200040")


if __name__ == "__main__":
    unittest.main()
