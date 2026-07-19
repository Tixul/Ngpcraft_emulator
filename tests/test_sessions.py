"""Named session tests for NgpCraft Emulator."""

from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from core.checkpoints import (
    checkpoint_path_for_rom,
    list_named_checkpoints,
    save_named_checkpoint,
)
from core.event_log import load_event_log
from core.machine import load_machine_state
from core.savestate import build_savestate_payload, save_savestate
from core.sessions import (
    delete_named_session,
    delete_named_session_snapshot,
    list_named_sessions,
    list_named_session_snapshots,
    load_named_session,
    load_named_session_snapshot,
    managed_checkpoint_name_for_session,
    managed_snapshot_checkpoint_name_for_session,
    restore_named_session_snapshot,
    save_named_session,
    save_named_session_snapshot,
    session_checkpoint_path_for_rom,
    session_snapshot_checkpoint_path_for_rom,
    session_path_for_rom,
)
from ngpc_emu import main


class NamedSessionTests(unittest.TestCase):
    def _write_demo_rom(self, path: Path, entry_point: int, body: bytes) -> None:
        data = bytearray(0x40)
        data[0x00:0x1C] = b"LICENSED BY SNK CORPORATION".ljust(0x1C, b"\x00")
        data[0x1C:0x20] = entry_point.to_bytes(4, "little")
        data[0x20:0x22] = (0x0000).to_bytes(2, "little")
        data[0x22] = 0
        data[0x23] = 0x10
        data[0x24:0x30] = b"SESSION\x00\x00\x00\x00\x00"
        body_offset = entry_point - 0x00200000
        if body_offset < len(data):
            data[body_offset : body_offset + len(body)] = body
        else:
            data.extend(b"\x00" * (body_offset - len(data)))
            data.extend(body)
        path.write_bytes(bytes(data))

    def test_core_named_session_helpers_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x00200040, b"\x00")
            machine = load_machine_state(rom_path)
            payload = build_savestate_payload(
                rom_path=rom_path,
                rom_header=machine.header,
                cpu=machine.cpu,
                writable_overlay={},
                note="session helper roundtrip",
            )

            checkpoint_name = managed_checkpoint_name_for_session("alpha session")
            checkpoint_path = checkpoint_path_for_rom(rom_path, checkpoint_name)
            save_named_checkpoint(checkpoint_path, payload)

            session = save_named_session(
                rom_path,
                "alpha session",
                current_checkpoint_name=checkpoint_name,
                last_action="unit-test",
                note="session note",
            )
            self.assertEqual(
                session.current_checkpoint_path,
                session_checkpoint_path_for_rom(rom_path, "alpha session"),
            )

            loaded = load_named_session(rom_path, "alpha session")
            self.assertEqual(loaded.document.cpu.pc, 0x00200040)
            self.assertEqual(loaded.last_action, "unit-test")
            self.assertEqual(loaded.path, session_path_for_rom(rom_path, "alpha session"))

            listed = list_named_sessions(rom_path)
            self.assertEqual(len(listed), 1)
            self.assertEqual(listed[0].slug, "alpha-session")

            deleted_session_path, deleted_checkpoint_path, deleted_snapshot_paths = delete_named_session(
                rom_path, "alpha session"
            )
            self.assertEqual(
                deleted_session_path, session_path_for_rom(rom_path, "alpha session")
            )
            self.assertEqual(deleted_checkpoint_path, checkpoint_path)
            self.assertEqual(deleted_snapshot_paths, ())
            self.assertFalse(deleted_session_path.exists())
            self.assertFalse(checkpoint_path.exists())

    def test_session_cli_save_list_load_delete(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            rom_path = tmp / "demo.ngc"
            self._write_demo_rom(rom_path, 0x00200040, b"\x00\x00\x00")

            stdout = io.StringIO()
            with redirect_stdout(stdout):
                exit_code = main(
                    [
                        "session",
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
                exit_code = main(["session", "list", str(rom_path), "--json"])
            self.assertEqual(exit_code, 0)
            list_payload = json.loads(stdout.getvalue())
            self.assertEqual(list_payload["count"], 1)
            self.assertEqual(list_payload["sessions"][0]["name"], "alpha")

            stdout = io.StringIO()
            with redirect_stdout(stdout):
                exit_code = main(["session", "load", str(rom_path), "alpha", "--json"])
            self.assertEqual(exit_code, 0)
            load_payload = json.loads(stdout.getvalue())
            self.assertEqual(load_payload["cpu_pc_hex"], "0x00200042")
            self.assertEqual(load_payload["checkpoint_name"], "session.alpha.current")

            stdout = io.StringIO()
            with redirect_stdout(stdout):
                exit_code = main(["session", "delete", str(rom_path), "alpha", "--json"])
            self.assertEqual(exit_code, 0)
            delete_payload = json.loads(stdout.getvalue())
            self.assertEqual(delete_payload["name"], "alpha")
            self.assertIn("session.alpha.current", delete_payload["deleted_checkpoint_path"])
            self.assertEqual(delete_payload["deleted_snapshot_paths"], [])

    def test_step_exec_cli_can_use_save_session_and_seed_session(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            rom_path = tmp / "demo.ngc"
            self._write_demo_rom(
                rom_path,
                0x00200040,
                b"\x1D\x50\x00\x20\x00" + (b"\x00" * 11),
            )

            stdout = io.StringIO()
            with redirect_stdout(stdout):
                exit_code = main(
                    [
                        "step-exec",
                        str(rom_path),
                        "--seed-xsp",
                        "0x4100",
                        "--save-session",
                        "alpha",
                        "--json",
                    ]
                )
            self.assertEqual(exit_code, 0)
            first_payload = json.loads(stdout.getvalue())
            self.assertEqual(first_payload["saved_state"]["kind"], "named-session")
            first = load_named_session(rom_path, "alpha")
            self.assertEqual(first.document.cpu.pc, 0x00200050)

            stdout = io.StringIO()
            with redirect_stdout(stdout):
                exit_code = main(
                    [
                        "step-exec",
                        str(rom_path),
                        "--seed-session",
                        "alpha",
                        "--save-session",
                        "alpha",
                        "--json",
                    ]
                )
            self.assertEqual(exit_code, 0)
            second_payload = json.loads(stdout.getvalue())
            self.assertEqual(second_payload["seed_from"]["kind"], "named-session")
            second = load_named_session(rom_path, "alpha")
            self.assertEqual(second.document.cpu.pc, 0x00200051)

    def test_eventlog_capture_cli_can_use_seed_session(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            rom_path = tmp / "demo.ngc"
            log_path = tmp / "demo.eventlog.json"
            self._write_demo_rom(rom_path, 0x00200040, b"\x00\x00")

            stdout = io.StringIO()
            with redirect_stdout(stdout):
                exit_code = main(
                    [
                        "session",
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
                        "--seed-session",
                        "alpha",
                        "--json",
                    ]
                )
            self.assertEqual(exit_code, 0)
            payload = json.loads(stdout.getvalue())
            self.assertEqual(payload["seed_from"]["kind"], "named-session")
            self.assertEqual(payload["seed_from"]["name"], "alpha")

            loaded = load_event_log(log_path, expected_rom_path=rom_path)
            run_context = loaded["run_context"]
            assert isinstance(run_context, dict)
            seed_from = run_context["seed_from_savestate"]
            assert isinstance(seed_from, dict)
            self.assertEqual(seed_from["kind"], "named-session")
            self.assertEqual(seed_from["name"], "alpha")

    def test_session_save_cli_can_use_auto_tick_run_until(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            rom_path = tmp / "demo.ngc"
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
                        "session",
                        "save",
                        str(rom_path),
                        "alpha",
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
            self.assertEqual(payload["cpu_pc_hex"], "0x00200047")
            self.assertEqual(payload["non_reference"]["address_hex"], "0x004000")

            session = load_named_session(rom_path, "alpha")
            self.assertEqual(session.document.cpu.pc, 0x00200047)
            self.assertIn("non-reference auto-tick", session.document.note or "")

    def test_core_named_session_snapshot_helpers_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x00200040, b"\x00")
            machine = load_machine_state(rom_path)
            payload = build_savestate_payload(
                rom_path=rom_path,
                rom_header=machine.header,
                cpu=machine.cpu,
                writable_overlay={},
                note="session snapshot roundtrip",
            )

            checkpoint_name = managed_checkpoint_name_for_session("alpha session")
            checkpoint_path = checkpoint_path_for_rom(rom_path, checkpoint_name)
            save_named_checkpoint(checkpoint_path, payload)
            save_named_session(
                rom_path,
                "alpha session",
                current_checkpoint_name=checkpoint_name,
                last_action="unit-test",
            )

            snapshot = save_named_session_snapshot(
                rom_path, "alpha session", "before-branch"
            )
            self.assertEqual(
                snapshot.path,
                session_snapshot_checkpoint_path_for_rom(
                    rom_path, "alpha session", "before-branch"
                ),
            )

            loaded = load_named_session_snapshot(
                rom_path, "alpha session", "before-branch"
            )
            self.assertEqual(loaded.document.cpu.pc, 0x00200040)
            self.assertEqual(
                loaded.checkpoint_name,
                managed_snapshot_checkpoint_name_for_session(
                    "alpha session", "before-branch"
                ),
            )

            listed = list_named_session_snapshots(rom_path, "alpha session")
            self.assertEqual(len(listed), 1)
            self.assertEqual(listed[0].slug, "before-branch")

            deleted_path = delete_named_session_snapshot(
                rom_path, "alpha session", "before-branch"
            )
            self.assertEqual(deleted_path, loaded.path)
            self.assertFalse(deleted_path.exists())

    def test_session_snapshot_cli_and_restore_flow(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            rom_path = tmp / "demo.ngc"
            self._write_demo_rom(
                rom_path,
                0x00200040,
                b"\x1D\x50\x00\x20\x00" + (b"\x00" * 11),
            )

            stdout = io.StringIO()
            with redirect_stdout(stdout):
                exit_code = main(
                    [
                        "session",
                        "save",
                        str(rom_path),
                        "alpha",
                        "--seed-xsp",
                        "0x4100",
                        "--json",
                    ]
                )
            self.assertEqual(exit_code, 0)

            stdout = io.StringIO()
            with redirect_stdout(stdout):
                exit_code = main(
                    [
                        "step-exec",
                        str(rom_path),
                        "--seed-session",
                        "alpha",
                        "--save-session",
                        "alpha",
                        "--json",
                    ]
                )
            self.assertEqual(exit_code, 0)
            current = load_named_session(rom_path, "alpha")
            self.assertEqual(current.document.cpu.pc, 0x00200050)

            stdout = io.StringIO()
            with redirect_stdout(stdout):
                exit_code = main(
                    [
                        "session",
                        "snapshot",
                        "save",
                        str(rom_path),
                        "alpha",
                        "after-call",
                        "--json",
                    ]
                )
            self.assertEqual(exit_code, 0)
            snapshot_payload = json.loads(stdout.getvalue())
            self.assertEqual(snapshot_payload["name"], "after-call")

            stdout = io.StringIO()
            with redirect_stdout(stdout):
                exit_code = main(
                    [
                        "step-exec",
                        str(rom_path),
                        "--seed-session",
                        "alpha",
                        "--save-session",
                        "alpha",
                        "--json",
                    ]
                )
            self.assertEqual(exit_code, 0)
            advanced = load_named_session(rom_path, "alpha")
            self.assertEqual(advanced.document.cpu.pc, 0x00200051)

            stdout = io.StringIO()
            with redirect_stdout(stdout):
                exit_code = main(
                    [
                        "session",
                        "snapshot",
                        "list",
                        str(rom_path),
                        "alpha",
                        "--json",
                    ]
                )
            self.assertEqual(exit_code, 0)
            list_payload = json.loads(stdout.getvalue())
            self.assertEqual(list_payload["count"], 1)
            self.assertEqual(list_payload["snapshots"][0]["name"], "after-call")

            stdout = io.StringIO()
            with redirect_stdout(stdout):
                exit_code = main(
                    [
                        "session",
                        "snapshot",
                        "restore",
                        str(rom_path),
                        "alpha",
                        "after-call",
                        "--json",
                    ]
                )
            self.assertEqual(exit_code, 0)
            restored_payload = json.loads(stdout.getvalue())
            self.assertEqual(restored_payload["snapshot_name"], "after-call")
            restored = load_named_session(rom_path, "alpha")
            self.assertEqual(restored.document.cpu.pc, 0x00200050)

            stdout = io.StringIO()
            with redirect_stdout(stdout):
                exit_code = main(
                    [
                        "session",
                        "snapshot",
                        "delete",
                        str(rom_path),
                        "alpha",
                        "after-call",
                        "--json",
                    ]
                )
            self.assertEqual(exit_code, 0)
            deleted_payload = json.loads(stdout.getvalue())
            self.assertEqual(deleted_payload["name"], "after-call")

    def test_checkpoint_list_hides_managed_session_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x00200040, b"\x00")
            machine = load_machine_state(rom_path)
            payload = build_savestate_payload(
                rom_path=rom_path,
                rom_header=machine.header,
                cpu=machine.cpu,
                writable_overlay={},
            )

            save_named_checkpoint(
                checkpoint_path_for_rom(rom_path, "visible-checkpoint"), payload
            )
            save_named_checkpoint(
                checkpoint_path_for_rom(
                    rom_path, managed_checkpoint_name_for_session("alpha")
                ),
                payload,
            )
            save_named_session(
                rom_path,
                "alpha",
                current_checkpoint_name=managed_checkpoint_name_for_session("alpha"),
            )
            save_named_session_snapshot(rom_path, "alpha", "boot")

            checkpoints = list_named_checkpoints(rom_path)
            self.assertEqual(len(checkpoints), 1)
            self.assertEqual(checkpoints[0].name, "visible-checkpoint")


if __name__ == "__main__":
    unittest.main()
