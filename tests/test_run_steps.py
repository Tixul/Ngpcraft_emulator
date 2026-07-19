"""Minimal stateful run-steps tests for NgpCraft Emulator."""

from __future__ import annotations

from dataclasses import replace
import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from core.fetch import load_fetch_view
from core.execute import seed_cpu_state_for_execution
from core.machine import load_machine_state
from core.run_steps import build_run_steps, build_run_until, load_run_steps
from core.savestate import build_savestate_payload, load_savestate, save_savestate
from ngpc_emu import main


class RunStepsTests(unittest.TestCase):
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

    def test_run_steps_carries_call_stack_state_into_ret(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            body = b"\x1D\x50\x00\x20" + (b"\x00" * 0x0C) + b"\x0E"
            self._write_demo_rom(rom_path, 0x00200040, body)
            base_view = load_fetch_view(rom_path)
            seeded_cpu = replace(
                base_view.machine.cpu,
                regs=replace(base_view.machine.cpu.regs, xsp=0x00004100),
            )

            result = build_run_steps(base_view, count=2, cpu_state=seeded_cpu)

            self.assertEqual(result.stop_reason, "count-reached")
            self.assertEqual(result.emitted_count, 2)
            self.assertEqual(result.executed_count, 2)
            self.assertEqual(result.records[0].execution.decode.assembly, "call 0x200050")
            self.assertEqual(result.records[1].execution.decode.assembly, "ret")
            self.assertEqual(result.final_cpu.pc, 0x00200044)
            self.assertEqual(result.final_cpu.regs.xsp, 0x00004100)
            self.assertEqual(result.final_memory[0x0040FC], 0x44)

    def test_run_steps_carries_overlay_between_pushw_and_pop_wa(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x00200040, b"\x0B\x20\x00\x48")
            base_view = load_fetch_view(rom_path)
            seeded_cpu = replace(
                base_view.machine.cpu,
                regs=replace(
                    base_view.machine.cpu.regs,
                    xsp=0x00004100,
                    xwa=0xAABBCCDD,
                ),
            )

            result = build_run_steps(base_view, count=2, cpu_state=seeded_cpu)

            self.assertEqual(result.stop_reason, "count-reached")
            self.assertEqual(result.executed_count, 2)
            self.assertEqual(result.records[0].execution.decode.assembly, "pushw 0x0020")
            self.assertEqual(result.records[1].execution.decode.assembly, "pop WA")
            self.assertEqual(result.final_cpu.regs.xwa, 0xAABB0020)
            self.assertEqual(result.final_cpu.regs.xsp, 0x00004100)
            self.assertEqual(result.final_cpu.pc, 0x00200044)

    def test_run_steps_stops_on_first_blocked_instruction(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            body = b"\x1D\x50\x00\x20" + (b"\x00" * 0x0C) + b"\x21\x00"
            self._write_demo_rom(rom_path, 0x00200040, body)
            base_view = load_fetch_view(rom_path)
            seeded_cpu = replace(
                base_view.machine.cpu,
                regs=replace(base_view.machine.cpu.regs, xsp=0x00004100),
            )

            result = build_run_steps(base_view, count=3, cpu_state=seeded_cpu)

            self.assertEqual(result.stop_reason, "stopped-on-requires-known-full-register")
            self.assertEqual(result.emitted_count, 2)
            self.assertEqual(result.executed_count, 1)
            self.assertEqual(result.records[0].execution.status, "executed")
            self.assertEqual(result.records[1].execution.status, "requires-known-full-register")
            self.assertEqual(result.final_cpu.pc, 0x00200050)
            self.assertEqual(result.final_cpu.regs.xsp, 0x000040FC)

    def test_run_steps_stops_on_halt_after_advancing_pc(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x00200040, b"\x05\x00")
            view = load_fetch_view(rom_path)

            result = build_run_steps(view, count=2)

            self.assertEqual(result.stop_reason, "stopped-on-cpu-halted")
            self.assertEqual(result.emitted_count, 1)
            self.assertEqual(result.executed_count, 0)
            self.assertEqual(result.records[0].execution.decode.assembly, "halt")
            self.assertEqual(result.records[0].execution.status, "cpu-halted")
            self.assertEqual(result.final_cpu.pc, 0x00200041)

    def test_run_until_stops_on_silicon_broken_frontier(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x00200040, b"\x47\x00\x60\x00\x00\xD8\xB8")
            view = load_fetch_view(rom_path)

            result = build_run_until(view, target_pc=0x00200080, max_steps=4)

            self.assertEqual(result.stop_reason, "stopped-on-silicon-broken")
            self.assertEqual(result.emitted_count, 2)
            self.assertEqual(result.executed_count, 1)
            assert result.last_record is not None
            self.assertEqual(result.last_record.execution.decode.assembly, "ex WA, WA")
            self.assertEqual(result.last_record.execution.status, "silicon-broken")
            self.assertIsNotNone(result.last_record.execution.matched_quirk)
            assert result.last_record.execution.matched_quirk is not None
            self.assertEqual(
                result.last_record.execution.matched_quirk.quirk_id,
                "cpu.d8_df_register_to_register",
            )
            self.assertEqual(result.final_cpu.pc, 0x00200045)

    def test_run_until_auto_tick_can_escape_byte_counter_spin_loop(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            body = b"\xC1\x00\x40\x3F\x00\x66\xF9\x00"
            self._write_demo_rom(rom_path, 0x00200040, body)
            view = load_fetch_view(rom_path)

            result = build_run_until(
                view,
                target_pc=0x00200047,
                max_steps=8,
                memory_bytes={0x004000: 0x00},
                auto_tick_address=0x004000,
                auto_tick_period=1,
            )

            self.assertEqual(result.stop_reason, "target-reached")
            self.assertEqual(result.executed_count, 4)
            self.assertEqual(result.final_cpu.pc, 0x00200047)
            self.assertGreaterEqual(result.final_memory[0x004000], 1)

    def test_run_steps_rejects_zero_count(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x00200040, b"\x00")
            view = load_fetch_view(rom_path)

            with self.assertRaisesRegex(ValueError, "count must be >= 1"):
                build_run_steps(view, count=0)

    def test_run_steps_can_progress_past_push_xiz_when_seeded(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x00200040, b"\x3E\x00")
            view = load_fetch_view(rom_path)
            seeded_cpu = seed_cpu_state_for_execution(
                view.machine.cpu,
                register_values={"XIZ": 0x12345678},
                seed_xsp=0x00004100,
            )

            result = build_run_steps(view, count=2, cpu_state=seeded_cpu)

            self.assertEqual(result.stop_reason, "count-reached")
            self.assertEqual(result.executed_count, 2)
            self.assertEqual(result.records[0].execution.decode.assembly, "push XIZ")
            self.assertEqual(result.final_cpu.regs.xsp, 0x000040FC)
            self.assertEqual(result.final_memory[0x0040FC], 0x78)
            self.assertEqual(result.final_memory[0x0040FD], 0x56)
            self.assertEqual(result.final_memory[0x0040FE], 0x34)
            self.assertEqual(result.final_memory[0x0040FF], 0x12)

    def test_run_steps_cli_accepts_banked_seed_registers(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x00200040, b"\x17\x03\x39")
            out = io.StringIO()

            with redirect_stdout(out):
                exit_code = main(
                    [
                        "run-steps",
                        str(rom_path),
                        "--count",
                        "2",
                        "--seed-xsp",
                        "0x00004100",
                        "--seed-reg",
                        "XBC@bank3=0x12345678",
                        "--json",
                    ]
                )

            self.assertEqual(exit_code, 0)
            payload = json.loads(out.getvalue())
            self.assertEqual(payload["executed_count"], 2)
            self.assertEqual(payload["records"][1]["decode"]["assembly"], "push XBC")
            self.assertEqual(payload["final_memory"][0]["address_hex"], "0x0040FC")
            self.assertEqual(payload["final_memory"][0]["value_hex"], "0x78")

    def test_step_exec_cli_accepts_control_register_seeds(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x00200040, b"\xE8\x2F\x10")
            out = io.StringIO()

            with redirect_stdout(out):
                exit_code = main(
                    [
                        "step-exec",
                        str(rom_path),
                        "--seed-reg",
                        "DMAD0=0x00201234",
                        "--json",
                    ]
                )

            self.assertEqual(exit_code, 0)
            payload = json.loads(out.getvalue())
            self.assertEqual(payload["executed_count"], 1)
            self.assertEqual(payload["execution"]["status"], "executed")
            self.assertEqual(payload["execution"]["decode"]["assembly"], "ldc XWA, DMAD0")
            self.assertEqual(payload["final_cpu"]["registers"]["xwa_hex"], "0x00201234")
            self.assertEqual(payload["seed_registers"][0]["name"], "DMAD0")

    def test_step_exec_cli_reports_halt_with_advanced_final_pc(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x00200040, b"\x05\x00")
            out = io.StringIO()

            with redirect_stdout(out):
                exit_code = main(
                    [
                        "step-exec",
                        str(rom_path),
                        "--json",
                    ]
                )

            self.assertEqual(exit_code, 0)
            payload = json.loads(out.getvalue())
            self.assertEqual(payload["executed_count"], 0)
            self.assertEqual(payload["stop_reason"], "stopped-on-cpu-halted")
            self.assertEqual(payload["execution"]["status"], "cpu-halted")
            self.assertEqual(payload["execution"]["after_cpu"]["pc_hex"], "0x00200041")
            self.assertEqual(payload["final_cpu"]["pc_hex"], "0x00200041")

    def test_run_steps_cli_seed_zero_bios_call_context_shortcut(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x00200040, b"\x17\x03\x39\x3A\x3B\x3D")
            out = io.StringIO()

            with redirect_stdout(out):
                exit_code = main(
                    [
                        "run-steps",
                        str(rom_path),
                        "--count",
                        "5",
                        "--seed-xsp",
                        "0x00004100",
                        "--seed-zero-bios-call-context",
                        "--json",
                    ]
                )

            self.assertEqual(exit_code, 0)
            payload = json.loads(out.getvalue())
            self.assertEqual(payload["executed_count"], 5)
            self.assertEqual(payload["records"][4]["decode"]["assembly"], "push XIY")
            self.assertEqual(payload["final_cpu"]["registers"]["xiy_hex"], "0x00000000")
            self.assertEqual(payload["final_memory"][0]["address_hex"], "0x0040F0")

    def test_run_steps_cli_seed_zero_caller_saved_shortcut(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x00200040, b"\x3E\x3C\x3D")
            out = io.StringIO()

            with redirect_stdout(out):
                exit_code = main(
                    [
                        "run-steps",
                        str(rom_path),
                        "--count",
                        "3",
                        "--seed-xsp",
                        "0x00004100",
                        "--seed-zero-caller-saved",
                        "--json",
                    ]
                )

            self.assertEqual(exit_code, 0)
            payload = json.loads(out.getvalue())
            self.assertEqual(payload["executed_count"], 2)
            self.assertEqual(payload["records"][0]["decode"]["assembly"], "push XIZ")
            self.assertEqual(payload["records"][1]["decode"]["assembly"], "push XIX")
            self.assertEqual(payload["records"][2]["status"], "requires-known-full-register")
            self.assertEqual(payload["records"][2]["decode"]["assembly"], "push XIY")

    def test_run_steps_cli_seed_zero_adecl_args_shortcut(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x00200040, b"\x38\x39\x3A\x3C")
            out = io.StringIO()

            with redirect_stdout(out):
                exit_code = main(
                    [
                        "run-steps",
                        str(rom_path),
                        "--count",
                        "4",
                        "--seed-xsp",
                        "0x00004100",
                        "--seed-zero-adecl-args",
                        "--json",
                    ]
                )

            self.assertEqual(exit_code, 0)
            payload = json.loads(out.getvalue())
            self.assertEqual(payload["executed_count"], 3)
            self.assertEqual(payload["records"][0]["decode"]["assembly"], "push XWA")
            self.assertEqual(payload["records"][1]["decode"]["assembly"], "push XBC")
            self.assertEqual(payload["records"][2]["decode"]["assembly"], "push XDE")
            self.assertEqual(payload["records"][3]["status"], "requires-known-full-register")
            self.assertEqual(payload["records"][3]["decode"]["assembly"], "push XIX")

    def test_run_steps_cli_seed_zero_toolchain_loop_iz_shortcut(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x00200040, b"\x3E\x3C")
            out = io.StringIO()

            with redirect_stdout(out):
                exit_code = main(
                    [
                        "run-steps",
                        str(rom_path),
                        "--count",
                        "2",
                        "--seed-xsp",
                        "0x00004100",
                        "--seed-zero-toolchain-loop-iz",
                        "--json",
                    ]
                )

            self.assertEqual(exit_code, 0)
            payload = json.loads(out.getvalue())
            self.assertEqual(payload["executed_count"], 1)
            self.assertEqual(payload["records"][0]["decode"]["assembly"], "push XIZ")
            self.assertEqual(payload["records"][1]["status"], "requires-known-full-register")
            self.assertEqual(payload["records"][1]["decode"]["assembly"], "push XIX")

    def test_run_steps_cli_seed_bios_handoff_xsp_shortcut(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x00200040, b"\xEF\x6C")
            out = io.StringIO()

            with redirect_stdout(out):
                exit_code = main(
                    [
                        "run-steps",
                        str(rom_path),
                        "--count",
                        "1",
                        "--seed-bios-handoff-xsp",
                        "--json",
                    ]
                )

            self.assertEqual(exit_code, 0)
            payload = json.loads(out.getvalue())
            self.assertEqual(payload["executed_count"], 1)
            self.assertEqual(payload["records"][0]["decode"]["assembly"], "dec 4, XSP")
            self.assertEqual(payload["final_cpu"]["registers"]["xsp_hex"], "0x00006BFC")

    def test_step_exec_cli_seed_bios_handoff_minimal_shortcut(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x00200040, b"\xE8\x2F\x30")
            out = io.StringIO()

            with redirect_stdout(out):
                exit_code = main(
                    [
                        "step-exec",
                        str(rom_path),
                        "--seed-bios-handoff-minimal",
                        "--seed-reg",
                        "XWA=0",
                        "--json",
                    ]
                )

            self.assertEqual(exit_code, 0)
            payload = json.loads(out.getvalue())
            self.assertEqual(payload["executed_count"], 1)
            self.assertEqual(payload["execution"]["status"], "executed")
            self.assertEqual(payload["execution"]["decode"]["assembly"], "ldc WA, INTNEST")
            self.assertEqual(payload["final_cpu"]["registers"]["xwa_hex"], "0x00000000")
            self.assertEqual(
                {row["name"]: row["value_hex"] for row in payload["seed_registers"]},
                {"INTNEST": "0x00000000", "XSP": "0x00006C00", "XWA": "0x00000000"},
            )

    def test_step_exec_cli_seed_bios_handoff_minimal_allows_explicit_intnest_override(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x00200040, b"\xE8\x2F\x30")
            out = io.StringIO()

            with redirect_stdout(out):
                exit_code = main(
                    [
                        "step-exec",
                        str(rom_path),
                        "--seed-bios-handoff-minimal",
                        "--seed-reg",
                        "XWA=0",
                        "--seed-reg",
                        "INTNEST=0x00001234",
                        "--json",
                    ]
                )

            self.assertEqual(exit_code, 0)
            payload = json.loads(out.getvalue())
            self.assertEqual(payload["executed_count"], 1)
            self.assertEqual(payload["execution"]["status"], "executed")
            self.assertEqual(payload["final_cpu"]["registers"]["xwa_hex"], "0x00001234")
            self.assertEqual(
                {row["name"]: row["value_hex"] for row in payload["seed_registers"]},
                {"INTNEST": "0x00001234", "XSP": "0x00006C00", "XWA": "0x00000000"},
            )

    def test_run_steps_carries_lda_and_indexed_store_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x0020D06D, b"\xF2\x66\x32\x21\x31\xBF\x04\x61")
            view = load_fetch_view(rom_path)
            seeded_cpu = seed_cpu_state_for_execution(
                view.machine.cpu,
                register_values={"XSP": 0x000040F8},
            )

            result = build_run_steps(view, count=2, cpu_state=seeded_cpu)

            self.assertEqual(result.stop_reason, "count-reached")
            self.assertEqual(result.executed_count, 2)
            self.assertEqual(result.records[0].execution.decode.assembly, "lda XBC, (0x213266)")
            self.assertEqual(result.records[1].execution.decode.assembly, "ld (XSP+4), XBC")
            self.assertEqual(result.final_cpu.regs.xbc, 0x00213266)
            self.assertEqual(result.final_cpu.pc, 0x0020D075)
            self.assertEqual(result.final_memory[0x0040FC], 0x66)
            self.assertEqual(result.final_memory[0x0040FD], 0x32)
            self.assertEqual(result.final_memory[0x0040FE], 0x21)
            self.assertEqual(result.final_memory[0x0040FF], 0x00)

    def test_run_steps_carries_prefixed_register_ld_after_lda_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            body = b"\xF2\x00\x40\x00\x33\xEB\x8D"
            self._write_demo_rom(rom_path, 0x0020D07F, body)
            view = load_fetch_view(rom_path)

            result = build_run_steps(view, count=2)

            self.assertEqual(result.stop_reason, "count-reached")
            self.assertEqual(result.executed_count, 2)
            self.assertEqual(result.records[0].execution.decode.assembly, "lda XHL, (0x004000)")
            self.assertEqual(result.records[1].execution.decode.assembly, "ld XIY, XHL")
            self.assertEqual(result.final_cpu.regs.xhl, 0x00004000)
            self.assertEqual(result.final_cpu.regs.xiy, 0x00004000)
            self.assertEqual(result.final_cpu.pc, 0x0020D086)

    def test_run_steps_can_carry_compare_branch_and_indexed_load(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            body = b"\xEC\xF1\x6F\x11\xAF\x04\x20"
            self._write_demo_rom(rom_path, 0x0020D08B, body)
            base_view = load_fetch_view(rom_path)
            seeded_cpu = replace(
                base_view.machine.cpu,
                regs=replace(
                    base_view.machine.cpu.regs,
                    xbc=0x00213266,
                    xix=0x00213350,
                    xsp=0x000040F0,
                ),
            )

            result = build_run_steps(
                base_view,
                count=3,
                cpu_state=seeded_cpu,
                memory_bytes={
                    0x0040F4: 0x66,
                    0x0040F5: 0x32,
                    0x0040F6: 0x21,
                    0x0040F7: 0x00,
                },
            )

            self.assertEqual(result.stop_reason, "count-reached")
            self.assertEqual(result.executed_count, 3)
            self.assertEqual(result.records[0].execution.decode.assembly, "cp XBC, XIX")
            self.assertEqual(result.records[1].execution.decode.assembly, "jr NC, 0x20D0A0")
            self.assertEqual(result.records[2].execution.decode.assembly, "ld XWA, (XSP+4)")
            self.assertEqual(result.final_cpu.pc, 0x0020D092)
            self.assertEqual(result.final_cpu.regs.xwa, 0x00213266)
            self.assertTrue(result.final_cpu.flags.cf)

    def test_run_steps_can_carry_copy_loop_post_increment_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            body = b"\xAF\x04\x20\xC5\xE0\x23\xBF\x04\x60\xF5\xF8\x43\xAF\x04\xFC\x67\xEF"
            self._write_demo_rom(rom_path, 0x0020D08F, body)
            base_view = load_fetch_view(rom_path)
            seeded_cpu = seed_cpu_state_for_execution(
                base_view.machine.cpu,
                register_values={
                    "XBC": 0xAABBCCDD,
                    "XIZ": 0x00005EBC,
                    "XIX": 0x00213268,
                },
                seed_xsp=0x000040F0,
            )

            result = build_run_steps(
                base_view,
                count=6,
                cpu_state=seeded_cpu,
                memory_bytes={
                    0x0040F4: 0x66,
                    0x0040F5: 0x32,
                    0x0040F6: 0x21,
                    0x0040F7: 0x00,
                    0x213266: 0x42,
                },
            )

            self.assertEqual(result.stop_reason, "count-reached")
            self.assertEqual(result.executed_count, 6)
            self.assertEqual(result.records[0].execution.decode.assembly, "ld XWA, (XSP+4)")
            self.assertEqual(result.records[1].execution.decode.assembly, "ld C, (XWA+)")
            self.assertEqual(result.records[2].execution.decode.assembly, "ld (XSP+4), XWA")
            self.assertEqual(result.records[3].execution.decode.assembly, "ld (XIZ+), C")
            self.assertEqual(result.records[4].execution.decode.assembly, "cp (XSP+4), XIX")
            self.assertEqual(result.records[5].execution.decode.assembly, "jr C, 0x20D08F")
            self.assertEqual(result.final_cpu.pc, 0x0020D08F)
            self.assertEqual(result.final_cpu.regs.xwa, 0x00213267)
            self.assertEqual(result.final_cpu.regs.xiz, 0x00005EBD)
            self.assertEqual(result.final_cpu.regs.xbc, 0xAABBCC42)
            self.assertTrue(result.final_cpu.flags.cf)
            self.assertEqual(result.final_memory[0x0040F4], 0x67)
            self.assertEqual(result.final_memory[0x0040F5], 0x32)
            self.assertEqual(result.final_memory[0x0040F6], 0x21)
            self.assertEqual(result.final_memory[0x0040F7], 0x00)
            self.assertEqual(result.final_memory[0x005EBC], 0x42)

    def test_run_steps_can_carry_pop_inc_ret_epilogue(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x0020D0AC, b"\x5E\xEF\x64\x0E")
            base_view = load_fetch_view(rom_path)
            seeded_cpu = seed_cpu_state_for_execution(
                base_view.machine.cpu,
                seed_xsp=0x000040F0,
            )

            result = build_run_steps(
                base_view,
                count=3,
                cpu_state=seeded_cpu,
                memory_bytes={
                    0x0040F0: 0x78,
                    0x0040F1: 0x56,
                    0x0040F2: 0x34,
                    0x0040F3: 0x12,
                    0x0040F8: 0xCA,
                    0x0040F9: 0x79,
                    0x0040FA: 0x20,
                    0x0040FB: 0x00,
                },
            )

            self.assertEqual(result.stop_reason, "count-reached")
            self.assertEqual(result.executed_count, 3)
            self.assertEqual(result.records[0].execution.decode.assembly, "pop XIZ")
            self.assertEqual(result.records[1].execution.decode.assembly, "inc 4, XSP")
            self.assertEqual(result.records[2].execution.decode.assembly, "ret")
            self.assertEqual(result.final_cpu.regs.xiz, 0x12345678)
            self.assertEqual(result.final_cpu.regs.xsp, 0x000040FC)
            self.assertEqual(result.final_cpu.pc, 0x002079CA)

    def test_run_steps_can_carry_abs16_compare_and_conditional_jr(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            body = b"\x21\x00\xC1\x91\x6F\x3F\x00\x66\x02\x21\x01"
            self._write_demo_rom(rom_path, 0x0020D0D8, body)
            base_view = load_fetch_view(rom_path)
            seeded_cpu = seed_cpu_state_for_execution(
                base_view.machine.cpu,
                register_values={"XWA": 0x11223344},
            )

            result = build_run_steps(
                base_view,
                count=3,
                cpu_state=seeded_cpu,
                memory_bytes={0x006F91: 0x00},
            )

            self.assertEqual(result.stop_reason, "count-reached")
            self.assertEqual(result.executed_count, 3)
            self.assertEqual(result.records[0].execution.decode.assembly, "ld A, 0x00")
            self.assertEqual(result.records[1].execution.decode.assembly, "cp (0x6F91), 0x00")
            self.assertEqual(result.records[2].execution.decode.assembly, "jr Z, 0x20D0E3")
            self.assertEqual(result.final_cpu.pc, 0x0020D0E3)
            self.assertTrue(result.final_cpu.flags.zf)

    def test_run_steps_can_cross_builtin_system_bytes_call_and_vector_init(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            main_body = (
                b"\x21\x00"
                b"\xC1\x91\x6F\x3F\x00"
                b"\x66\x02"
                b"\x21\x01"
                b"\xF2\x80\x5F\x00\x41"
                b"\xF1\x86\x6F\xB5"
                b"\xF1\x86\x6F\xBE"
                b"\x1D\x1D\xD2\x20"
                b"\xF2\xB0\xD0\x20\x30"
                b"\xF1\xB8\x6F\x60"
            )
            subroutine = (
                b"\xF2\x1A\x50\x00\x00\x00"
                b"\xF2\x1C\x50\x00\x00\x00"
                b"\xF2\x1E\x50\x00\x00\x00"
                b"\x0E"
            )
            gap_size = 0x0020D21D - 0x0020D0D8 - len(main_body)
            self._write_demo_rom(rom_path, 0x0020D0D8, main_body + (b"\x00" * gap_size) + subroutine)
            base_view = load_fetch_view(rom_path)
            seeded_cpu = seed_cpu_state_for_execution(
                base_view.machine.cpu,
                register_values={"XWA": 0x11223344},
                seed_xsp=0x00004100,
            )

            result = build_run_steps(base_view, count=14, cpu_state=seeded_cpu)

            self.assertEqual(result.stop_reason, "count-reached")
            self.assertEqual(result.executed_count, 14)
            self.assertEqual(result.records[2].execution.decode.assembly, "jr Z, 0x20D0E3")
            self.assertEqual(result.records[4].execution.decode.assembly, "ld (0x005F80), A")
            self.assertEqual(result.records[5].execution.decode.assembly, "res 5, (0x6F86)")
            self.assertEqual(result.records[6].execution.decode.assembly, "set 6, (0x6F86)")
            self.assertEqual(result.records[8].execution.decode.assembly, "ld (0x00501A), 0x00")
            self.assertEqual(result.records[11].execution.decode.assembly, "ret")
            self.assertEqual(result.records[12].execution.decode.assembly, "lda XWA, (0x20D0B0)")
            self.assertEqual(result.records[13].execution.decode.assembly, "ld (0x6FB8), XWA")
            self.assertEqual(result.final_cpu.pc, 0x0020D0FD)
            self.assertEqual(result.final_cpu.regs.xsp, 0x00004100)
            self.assertEqual(result.final_cpu.regs.xwa, 0x0020D0B0)
            self.assertEqual(result.final_memory[0x005F80], 0x01)
            self.assertEqual(result.final_memory[0x006F86], 0x40)
            self.assertEqual(result.final_memory[0x00501A], 0x00)
            self.assertEqual(result.final_memory[0x00501C], 0x00)
            self.assertEqual(result.final_memory[0x00501E], 0x00)
            self.assertEqual(result.final_memory[0x006FB8], 0xB0)
            self.assertEqual(result.final_memory[0x006FB9], 0xD0)
            self.assertEqual(result.final_memory[0x006FBA], 0x20)
            self.assertEqual(result.final_memory[0x006FBB], 0x00)

    def test_load_run_steps_can_resume_from_savestate_seed(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x00200040, b"\x00\x00\xD8\xB8")
            machine = load_machine_state(rom_path)
            seeded_cpu = replace(
                machine.cpu,
                pc=0x00200041,
                regs=replace(machine.cpu.regs, xsp=0x00004100),
            )
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

            result = load_run_steps(
                rom_path,
                count=2,
                initial_cpu_state=loaded.cpu,
                initial_memory_bytes=loaded.writable_overlay,
            )

            self.assertEqual(result.start_pc, 0x00200041)
            self.assertEqual(result.executed_count, 1)
            self.assertEqual(result.stop_reason, "stopped-on-silicon-broken")
            self.assertEqual(result.final_cpu.pc, 0x00200042)
            self.assertEqual(result.final_memory.get(0x004100), 0xAA)
            self.assertEqual(result.final_memory.get(0x004101), 0xBB)

    def test_run_steps_cli_can_save_and_resume_savestate(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            rom_path = tmp / "demo.ngc"
            self._write_demo_rom(rom_path, 0x00200040, b"\x00\x00\xD0\x61")
            state1 = tmp / "step1.json"
            state2 = tmp / "step2.json"

            stdout = io.StringIO()
            with redirect_stdout(stdout):
                exit_code = main(
                    [
                        "run-steps",
                        str(rom_path),
                        "--count",
                        "2",
                        "--save-state",
                        str(state1),
                        "--json",
                    ]
                )
            self.assertEqual(exit_code, 0)
            self.assertTrue(state1.exists())
            first = load_savestate(state1, expected_rom_path=rom_path)
            self.assertEqual(first.cpu.pc, 0x00200042)

            stdout = io.StringIO()
            with redirect_stdout(stdout):
                exit_code = main(
                    [
                        "run-steps",
                        str(rom_path),
                        "--count",
                        "2",
                        "--seed-from",
                        str(state1),
                        "--save-state",
                        str(state2),
                        "--json",
                    ]
                )
            self.assertEqual(exit_code, 0)
            self.assertTrue(state2.exists())
            second = load_savestate(state2, expected_rom_path=rom_path)
            self.assertEqual(second.cpu.pc, 0x00200042)
            self.assertIn("run-steps", second.note or "")

    def test_run_until_exec_cli_can_save_savestate(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            rom_path = tmp / "demo.ngc"
            self._write_demo_rom(rom_path, 0x00200040, b"\x00\x00\x00")
            state_path = tmp / "until.json"

            stdout = io.StringIO()
            with redirect_stdout(stdout):
                exit_code = main(
                    [
                        "run-until-exec",
                        str(rom_path),
                        "0x00200043",
                        "--save-state",
                        str(state_path),
                        "--json",
                    ]
                )
            self.assertEqual(exit_code, 0)
            self.assertTrue(state_path.exists())
            saved = load_savestate(state_path, expected_rom_path=rom_path)
            self.assertEqual(saved.cpu.pc, 0x00200043)
            self.assertIn("run-until-exec", saved.note or "")

    def test_run_until_exec_cli_reports_non_reference_auto_tick(self) -> None:
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
                        "run-until-exec",
                        str(rom_path),
                        "0x00200047",
                        "--seed-from",
                        str(state_path),
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
            self.assertEqual(payload["final_cpu"]["pc_hex"], "0x00200047")
            self.assertEqual(payload["non_reference"]["address_hex"], "0x004000")
            self.assertEqual(payload["non_reference"]["period"], 1)

    def test_step_exec_cli_can_save_and_resume_savestate(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            rom_path = tmp / "demo.ngc"
            self._write_demo_rom(rom_path, 0x00200040, b"\x1D\x50\x00\x20\x00" + (b"\x00" * 11))
            state1 = tmp / "step_exec_1.json"
            state2 = tmp / "step_exec_2.json"

            stdout = io.StringIO()
            with redirect_stdout(stdout):
                exit_code = main(
                    [
                        "step-exec",
                        str(rom_path),
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
            self.assertEqual(payload1["execution"]["status"], "executed")
            self.assertEqual(payload1["saved_state"]["cpu_pc_hex"], "0x00200050")
            first = load_savestate(state1, expected_rom_path=rom_path)
            self.assertEqual(first.cpu.pc, 0x00200050)

            stdout = io.StringIO()
            with redirect_stdout(stdout):
                exit_code = main(
                    [
                        "step-exec",
                        str(rom_path),
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
            self.assertIn("step-exec", second.note or "")


if __name__ == "__main__":
    unittest.main()
