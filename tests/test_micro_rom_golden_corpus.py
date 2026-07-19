"""First synthetic micro-ROM golden corpus for NgpCraft Emulator."""

from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from dataclasses import replace
from pathlib import Path

from core.cpu import StatusFlags
from core.goldens import load_named_golden
from core.machine import load_machine_state
from core.quirks import load_known_quirk_database
from core.savestate import build_savestate_payload, save_savestate
from ngpc_emu import main


class MicroRomGoldenCorpusTests(unittest.TestCase):
    def _write_demo_rom(self, path: Path, entry_point: int, body: bytes) -> None:
        data = bytearray(0x40)
        data[0x00:0x1C] = b"LICENSED BY SNK CORPORATION".ljust(0x1C, b"\x00")
        data[0x1C:0x20] = entry_point.to_bytes(4, "little")
        data[0x20:0x22] = (0x0000).to_bytes(2, "little")
        data[0x22] = 0
        data[0x23] = 0x10
        data[0x24:0x30] = b"MICRO CORPUS\x00"
        body_offset = entry_point - 0x00200000
        if body_offset < len(data):
            data[body_offset : body_offset + len(body)] = body
        else:
            data.extend(b"\x00" * (body_offset - len(data)))
            data.extend(body)
        path.write_bytes(bytes(data))

    def test_first_micro_rom_corpus_passes_named_golden_checks(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)

            arithmetic_rom = tmp / "arith.ngc"
            stack_rom = tmp / "stack.ngc"
            control_rom = tmp / "control.ngc"
            control_state = tmp / "control.seed.json"

            # add A, 0x01 ; nop   (byte form C9 C8 imm8 — HW-safe vs D0 C8 imm16 silicon-broken)
            self._write_demo_rom(arithmetic_rom, 0x00200040, b"\xC9\xC8\x01\x00")
            # call 0x200050 ; (padding) ; ret
            self._write_demo_rom(
                stack_rom,
                0x00200040,
                b"\x1D\x50\x00\x20" + (b"\x00" * 0x0C) + b"\x0E",
            )
            # cp (0x4000), 0x00 ; jr Z, +1 ; nop ; nop
            self._write_demo_rom(
                control_rom,
                0x00200040,
                b"\xC1\x00\x40\x3F\x00\x66\x01\x00\x00",
            )

            control_machine = load_machine_state(control_rom)
            save_savestate(
                control_state,
                build_savestate_payload(
                    rom_path=control_rom,
                    rom_header=control_machine.header,
                    cpu=control_machine.cpu,
                    writable_overlay={0x004000: 0x00},
                ),
            )

            cases = [
                {
                    "rom": arithmetic_rom,
                    "name": "arith-add-a",
                    "save_args": [
                        "--count",
                        "2",
                        "--seed-reg",
                        "XWA=0xAABB1234",
                    ],
                    "expected_events": ["add A, 0x01", "nop"],
                    "expected_final_pc": "0x00200044",
                },
                {
                    "rom": stack_rom,
                    "name": "stack-call-ret",
                    "save_args": [
                        "--count",
                        "3",
                        "--seed-xsp",
                        "0x4100",
                    ],
                    "expected_events": ["call 0x200050", "ret", "nop"],
                    "expected_final_pc": "0x00200045",
                },
                {
                    "rom": control_rom,
                    "name": "control-jr-z",
                    "save_args": [
                        "--count",
                        "3",
                        "--seed-from",
                        str(control_state),
                    ],
                    "expected_events": ["cp (0x4000), 0x00", "jr Z, 0x200048", "nop"],
                    "expected_final_pc": "0x00200049",
                },
            ]

            for case in cases:
                stdout = io.StringIO()
                with redirect_stdout(stdout):
                    exit_code = main(
                        [
                            "eventlog",
                            "golden-save",
                            str(case["rom"]),
                            str(case["name"]),
                            *case["save_args"],
                            "--json",
                        ]
                    )
                self.assertEqual(exit_code, 0, msg=case["name"])
                save_payload = json.loads(stdout.getvalue())
                self.assertEqual(save_payload["name"], case["name"])
                self.assertEqual(save_payload["final_cpu_pc_hex"], case["expected_final_pc"])

                golden = load_named_golden(case["rom"], case["name"])
                events = golden.payload["events"]
                assert isinstance(events, list)
                self.assertEqual(
                    [event["assembly"] for event in events],  # type: ignore[index]
                    case["expected_events"],
                )

                stdout = io.StringIO()
                with redirect_stdout(stdout):
                    exit_code = main(
                        [
                            "eventlog",
                            "golden-check",
                            str(case["rom"]),
                            str(case["name"]),
                            *case["save_args"],
                            "--json",
                        ]
                    )
                self.assertEqual(exit_code, 0, msg=case["name"])
                check_payload = json.loads(stdout.getvalue())
                self.assertEqual(check_payload["status"], "match")
                self.assertIsNone(check_payload["diff"]["first_divergence"])

            stdout = io.StringIO()
            with redirect_stdout(stdout):
                exit_code = main(["eventlog", "golden-list", str(arithmetic_rom), "--json"])
            self.assertEqual(exit_code, 0)
            arith_list = json.loads(stdout.getvalue())
            self.assertEqual(arith_list["count"], 1)
            self.assertEqual(arith_list["goldens"][0]["name"], "arith-add-a")

            stdout = io.StringIO()
            with redirect_stdout(stdout):
                exit_code = main(["eventlog", "golden-list", str(stack_rom), "--json"])
            self.assertEqual(exit_code, 0)
            stack_list = json.loads(stdout.getvalue())
            self.assertEqual(stack_list["count"], 1)
            self.assertEqual(stack_list["goldens"][0]["name"], "stack-call-ret")

            stdout = io.StringIO()
            with redirect_stdout(stdout):
                exit_code = main(["eventlog", "golden-list", str(control_rom), "--json"])
            self.assertEqual(exit_code, 0)
            control_list = json.loads(stdout.getvalue())
            self.assertEqual(control_list["count"], 1)
            self.assertEqual(control_list["goldens"][0]["name"], "control-jr-z")

    def test_arithmetic_micro_rom_corpus_covers_sub_and_xor_or_flags(self) -> None:
        # Byte-form (C9 + sub-op + imm8) is HW-safe; D0 + word-imm form is
        # silicon-broken on real NGPC silicon (confirmed 2026-05-20 HW crash).
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            cases = [
                {
                    "rom": tmp / "arith_sub.ngc",
                    "name": "arith-sub-a-zero",
                    "body": b"\xC9\xCA\x01\x00",
                    "seed_reg": "XWA=0xAABB0001",
                    "expected_assembly": "sub A, 0x01",
                    "expected_final_pc": "0x00200044",
                    "expected_flag_after": {"Z": True},
                },
                {
                    "rom": tmp / "arith_and.ngc",
                    "name": "arith-and-a-zero",
                    "body": b"\xC9\xCC\x00\x00",
                    "seed_reg": "XWA=0xAABB00FF",
                    "expected_assembly": "and A, 0x00",
                    "expected_final_pc": "0x00200044",
                    "expected_flag_after": {"Z": True},
                },
                {
                    "rom": tmp / "arith_xor.ngc",
                    "name": "arith-xor-a-zero",
                    "body": b"\xC9\xCD\xFF\x00",
                    "seed_reg": "XWA=0xAABB00FF",
                    "expected_assembly": "xor A, 0xFF",
                    "expected_final_pc": "0x00200044",
                    "expected_flag_after": {"Z": True},
                },
                {
                    "rom": tmp / "arith_or.ngc",
                    "name": "arith-or-a-sign",
                    "body": b"\xC9\xCE\x80\x00",
                    "seed_reg": "XWA=0xAABB0000",
                    "expected_assembly": "or A, 0x80",
                    "expected_final_pc": "0x00200044",
                    "expected_flag_after": {"S": True},
                },
            ]

            for case in cases:
                self._write_demo_rom(case["rom"], 0x00200040, case["body"])  # type: ignore[arg-type]

                stdout = io.StringIO()
                with redirect_stdout(stdout):
                    exit_code = main(
                        [
                            "eventlog",
                            "golden-save",
                            str(case["rom"]),
                            str(case["name"]),
                            "--count",
                            "2",
                            "--seed-reg",
                            str(case["seed_reg"]),
                            "--json",
                        ]
                    )
                self.assertEqual(exit_code, 0, msg=str(case["name"]))
                save_payload = json.loads(stdout.getvalue())
                self.assertEqual(save_payload["name"], case["name"])
                self.assertEqual(save_payload["final_cpu_pc_hex"], case["expected_final_pc"])

                golden = load_named_golden(case["rom"], case["name"])  # type: ignore[arg-type]
                events = golden.payload["events"]
                assert isinstance(events, list)
                self.assertEqual(events[0]["assembly"], case["expected_assembly"])
                self.assertEqual(events[1]["assembly"], "nop")
                flag_changes = events[0]["flag_changes"]
                assert isinstance(flag_changes, list)
                observed_after = {change["name"]: change["after"] for change in flag_changes}
                for flag_name, expected_after in case["expected_flag_after"].items():  # type: ignore[union-attr]
                    self.assertEqual(
                        observed_after.get(flag_name),
                        expected_after,
                        msg=f"{case['name']} missing {flag_name}",
                    )

                stdout = io.StringIO()
                with redirect_stdout(stdout):
                    exit_code = main(
                        [
                            "eventlog",
                            "golden-check",
                            str(case["rom"]),
                            str(case["name"]),
                            "--count",
                            "2",
                            "--seed-reg",
                            str(case["seed_reg"]),
                            "--json",
                        ]
                    )
                self.assertEqual(exit_code, 0, msg=str(case["name"]))
                check_payload = json.loads(stdout.getvalue())
                self.assertEqual(check_payload["status"], "match")
                self.assertIsNone(check_payload["diff"]["first_divergence"])

    def test_arithmetic_micro_rom_corpus_covers_carry_and_overflow_flags(self) -> None:
        # Byte-form (C9 + sub-op + imm8). Carry/overflow boundaries scaled
        # to byte register A: 0xFF (-1 wrap), 0x7F (max signed +1 → 0x80),
        # 0x00 (sub 1 → 0xFF), 0x80 (sub 1 → 0x7F overflow).
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            cases = [
                {
                    "rom": tmp / "arith_add_carry.ngc",
                    "name": "arith-add-a-carry-zero",
                    "body": b"\xC9\xC8\x01\x00",
                    "seed_reg": "XWA=0xAABB00FF",
                    "expected_assembly": "add A, 0x01",
                    "expected_final_pc": "0x00200044",
                    "expected_flag_after": {"Z": True, "C": True},
                },
                {
                    "rom": tmp / "arith_add_overflow.ngc",
                    "name": "arith-add-a-overflow-sign",
                    "body": b"\xC9\xC8\x01\x00",
                    "seed_reg": "XWA=0xAABB007F",
                    "expected_assembly": "add A, 0x01",
                    "expected_final_pc": "0x00200044",
                    "expected_flag_after": {"S": True, "V": True},
                },
                {
                    "rom": tmp / "arith_sub_borrow.ngc",
                    "name": "arith-sub-a-borrow-sign",
                    "body": b"\xC9\xCA\x01\x00",
                    "seed_reg": "XWA=0xAABB0000",
                    "expected_assembly": "sub A, 0x01",
                    "expected_final_pc": "0x00200044",
                    "expected_flag_after": {"S": True, "C": True},
                },
                {
                    "rom": tmp / "arith_sub_overflow.ngc",
                    "name": "arith-sub-a-overflow",
                    "body": b"\xC9\xCA\x01\x00",
                    "seed_reg": "XWA=0xAABB0080",
                    "expected_assembly": "sub A, 0x01",
                    "expected_final_pc": "0x00200044",
                    "expected_flag_after": {"V": True},
                },
            ]

            for case in cases:
                self._write_demo_rom(case["rom"], 0x00200040, case["body"])  # type: ignore[arg-type]

                stdout = io.StringIO()
                with redirect_stdout(stdout):
                    exit_code = main(
                        [
                            "eventlog",
                            "golden-save",
                            str(case["rom"]),
                            str(case["name"]),
                            "--count",
                            "2",
                            "--seed-reg",
                            str(case["seed_reg"]),
                            "--json",
                        ]
                    )
                self.assertEqual(exit_code, 0, msg=str(case["name"]))
                save_payload = json.loads(stdout.getvalue())
                self.assertEqual(save_payload["name"], case["name"])
                self.assertEqual(save_payload["final_cpu_pc_hex"], case["expected_final_pc"])

                golden = load_named_golden(case["rom"], case["name"])  # type: ignore[arg-type]
                events = golden.payload["events"]
                assert isinstance(events, list)
                self.assertEqual(events[0]["assembly"], case["expected_assembly"])
                self.assertEqual(events[1]["assembly"], "nop")
                flag_changes = events[0]["flag_changes"]
                assert isinstance(flag_changes, list)
                observed_after = {change["name"]: change["after"] for change in flag_changes}
                for flag_name, expected_after in case["expected_flag_after"].items():  # type: ignore[union-attr]
                    self.assertEqual(
                        observed_after.get(flag_name),
                        expected_after,
                        msg=f"{case['name']} missing {flag_name}",
                    )

                stdout = io.StringIO()
                with redirect_stdout(stdout):
                    exit_code = main(
                        [
                            "eventlog",
                            "golden-check",
                            str(case["rom"]),
                            str(case["name"]),
                            "--count",
                            "2",
                            "--seed-reg",
                            str(case["seed_reg"]),
                            "--json",
                        ]
                    )
                self.assertEqual(exit_code, 0, msg=str(case["name"]))
                check_payload = json.loads(stdout.getvalue())
                self.assertEqual(check_payload["status"], "match")
                self.assertIsNone(check_payload["diff"]["first_divergence"])

    def test_arithmetic_micro_rom_corpus_covers_adc_sbc_and_half_carry(self) -> None:
        # Byte-form (C9 + sub-op + imm8). Half-carry boundaries scaled to
        # byte: 0x0F + 1 → 0x10 sets H; 0x10 - 1 → 0x0F sets H.
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)

            adc_rom = tmp / "arith_adc.ngc"
            sbc_rom = tmp / "arith_sbc.ngc"
            add_h_rom = tmp / "arith_add_h.ngc"
            sub_h_rom = tmp / "arith_sub_h.ngc"
            adc_state = tmp / "adc.seed.json"
            sbc_state = tmp / "sbc.seed.json"

            # adc A, 0x00 ; nop
            self._write_demo_rom(adc_rom, 0x00200040, b"\xC9\xC9\x00\x00")
            # sbc A, 0x00 ; nop
            self._write_demo_rom(sbc_rom, 0x00200040, b"\xC9\xCB\x00\x00")
            # add A, 0x01 ; nop
            self._write_demo_rom(add_h_rom, 0x00200040, b"\xC9\xC8\x01\x00")
            # sub A, 0x01 ; nop
            self._write_demo_rom(sub_h_rom, 0x00200040, b"\xC9\xCA\x01\x00")

            adc_machine = load_machine_state(adc_rom)
            adc_cpu = replace(
                adc_machine.cpu,
                flags=StatusFlags(sf=False, zf=False, vf=False, hf=False, cf=True),
                regs=replace(adc_machine.cpu.regs, xwa=0xAABB007F),
            )
            save_savestate(
                adc_state,
                build_savestate_payload(
                    rom_path=adc_rom,
                    rom_header=adc_machine.header,
                    cpu=adc_cpu,
                    writable_overlay={},
                ),
            )

            sbc_machine = load_machine_state(sbc_rom)
            sbc_cpu = replace(
                sbc_machine.cpu,
                flags=StatusFlags(sf=False, zf=False, vf=False, hf=False, cf=True),
                regs=replace(sbc_machine.cpu.regs, xwa=0xAABB0080),
            )
            save_savestate(
                sbc_state,
                build_savestate_payload(
                    rom_path=sbc_rom,
                    rom_header=sbc_machine.header,
                    cpu=sbc_cpu,
                    writable_overlay={},
                ),
            )

            cases = [
                {
                    "rom": adc_rom,
                    "name": "arith-adc-a-carry-in",
                    "save_args": ["--count", "2", "--seed-from", str(adc_state)],
                    "expected_assembly": "adc A, 0x00",
                    "expected_final_pc": "0x00200044",
                    "expected_flag_after": {"S": True, "V": True, "H": True, "C": False},
                },
                {
                    "rom": sbc_rom,
                    "name": "arith-sbc-a-borrow-in",
                    "save_args": ["--count", "2", "--seed-from", str(sbc_state)],
                    "expected_assembly": "sbc A, 0x00",
                    "expected_final_pc": "0x00200044",
                    "expected_flag_after": {"V": True, "H": True, "C": False},
                },
                {
                    "rom": add_h_rom,
                    "name": "arith-add-a-half-carry",
                    "save_args": ["--count", "2", "--seed-reg", "XWA=0xAABB000F"],
                    "expected_assembly": "add A, 0x01",
                    "expected_final_pc": "0x00200044",
                    "expected_flag_after": {"H": True},
                },
                {
                    "rom": sub_h_rom,
                    "name": "arith-sub-a-half-borrow",
                    "save_args": ["--count", "2", "--seed-reg", "XWA=0xAABB0010"],
                    "expected_assembly": "sub A, 0x01",
                    "expected_final_pc": "0x00200044",
                    "expected_flag_after": {"H": True},
                },
            ]

            for case in cases:
                stdout = io.StringIO()
                with redirect_stdout(stdout):
                    exit_code = main(
                        [
                            "eventlog",
                            "golden-save",
                            str(case["rom"]),
                            str(case["name"]),
                            *case["save_args"],
                            "--json",
                        ]
                    )
                self.assertEqual(exit_code, 0, msg=str(case["name"]))
                save_payload = json.loads(stdout.getvalue())
                self.assertEqual(save_payload["name"], case["name"])
                self.assertEqual(save_payload["final_cpu_pc_hex"], case["expected_final_pc"])

                golden = load_named_golden(case["rom"], case["name"])  # type: ignore[arg-type]
                events = golden.payload["events"]
                assert isinstance(events, list)
                self.assertEqual(events[0]["assembly"], case["expected_assembly"])
                self.assertEqual(events[1]["assembly"], "nop")
                flag_changes = events[0]["flag_changes"]
                assert isinstance(flag_changes, list)
                observed_after = {change["name"]: change["after"] for change in flag_changes}
                for flag_name, expected_after in case["expected_flag_after"].items():  # type: ignore[union-attr]
                    self.assertEqual(
                        observed_after.get(flag_name),
                        expected_after,
                        msg=f"{case['name']} missing {flag_name}",
                    )

                stdout = io.StringIO()
                with redirect_stdout(stdout):
                    exit_code = main(
                        [
                            "eventlog",
                            "golden-check",
                            str(case["rom"]),
                            str(case["name"]),
                            *case["save_args"],
                            "--json",
                        ]
                    )
                self.assertEqual(exit_code, 0, msg=str(case["name"]))
                check_payload = json.loads(stdout.getvalue())
                self.assertEqual(check_payload["status"], "match")
                self.assertIsNone(check_payload["diff"]["first_divergence"])

    def test_arithmetic_micro_rom_corpus_covers_byte_and_long_variants(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)

            add_w_rom = tmp / "arith_add_w.ngc"
            or_a_rom = tmp / "arith_or_a.ngc"
            add_xwa_rom = tmp / "arith_add_xwa.ngc"
            sub_xwa_rom = tmp / "arith_sub_xwa.ngc"

            # add W, 0x01 ; nop
            self._write_demo_rom(add_w_rom, 0x00200040, b"\xC8\xC8\x01\x00")
            # or A, 0x80 ; nop
            self._write_demo_rom(or_a_rom, 0x00200040, b"\xC9\xCE\x80\x00")
            # add XWA, 0x00000001 ; nop
            self._write_demo_rom(add_xwa_rom, 0x00200040, b"\xE8\xC8\x01\x00\x00\x00\x00")
            # sub XWA, 0x00000001 ; nop
            self._write_demo_rom(sub_xwa_rom, 0x00200040, b"\xE8\xCA\x01\x00\x00\x00\x00")

            cases = [
                {
                    "rom": add_w_rom,
                    "name": "arith-add-w-carry-zero",
                    "save_args": ["--count", "2", "--seed-reg", "XWA=0xAABBFF34"],
                    "expected_assembly": "add W, 0x01",
                    "expected_final_pc": "0x00200044",
                    "expected_flag_after": {"Z": True, "C": True},
                },
                {
                    "rom": or_a_rom,
                    "name": "arith-or-a-sign",
                    "save_args": ["--count", "2", "--seed-reg", "XWA=0xAABB1200"],
                    "expected_assembly": "or A, 0x80",
                    "expected_final_pc": "0x00200044",
                    "expected_flag_after": {"S": True},
                },
                {
                    "rom": add_xwa_rom,
                    "name": "arith-add-xwa-carry-zero",
                    "save_args": ["--count", "2", "--seed-reg", "XWA=0xFFFFFFFF"],
                    "expected_assembly": "add XWA, 0x00000001",
                    "expected_final_pc": "0x00200047",
                    "expected_flag_after": {"Z": True, "C": True},
                },
                {
                    "rom": sub_xwa_rom,
                    "name": "arith-sub-xwa-overflow",
                    "save_args": ["--count", "2", "--seed-reg", "XWA=0x80000000"],
                    "expected_assembly": "sub XWA, 0x00000001",
                    "expected_final_pc": "0x00200047",
                    "expected_flag_after": {"V": True},
                },
            ]

            for case in cases:
                stdout = io.StringIO()
                with redirect_stdout(stdout):
                    exit_code = main(
                        [
                            "eventlog",
                            "golden-save",
                            str(case["rom"]),
                            str(case["name"]),
                            *case["save_args"],
                            "--json",
                        ]
                    )
                self.assertEqual(exit_code, 0, msg=str(case["name"]))
                save_payload = json.loads(stdout.getvalue())
                self.assertEqual(save_payload["name"], case["name"])
                self.assertEqual(save_payload["final_cpu_pc_hex"], case["expected_final_pc"])

                golden = load_named_golden(case["rom"], case["name"])  # type: ignore[arg-type]
                events = golden.payload["events"]
                assert isinstance(events, list)
                self.assertEqual(events[0]["assembly"], case["expected_assembly"])
                self.assertEqual(events[1]["assembly"], "nop")
                flag_changes = events[0]["flag_changes"]
                assert isinstance(flag_changes, list)
                observed_after = {change["name"]: change["after"] for change in flag_changes}
                for flag_name, expected_after in case["expected_flag_after"].items():  # type: ignore[union-attr]
                    self.assertEqual(
                        observed_after.get(flag_name),
                        expected_after,
                        msg=f"{case['name']} missing {flag_name}",
                    )

                stdout = io.StringIO()
                with redirect_stdout(stdout):
                    exit_code = main(
                        [
                            "eventlog",
                            "golden-check",
                            str(case["rom"]),
                            str(case["name"]),
                            *case["save_args"],
                            "--json",
                        ]
                    )
                self.assertEqual(exit_code, 0, msg=str(case["name"]))
                check_payload = json.loads(stdout.getvalue())
                self.assertEqual(check_payload["status"], "match")
                self.assertIsNone(check_payload["diff"]["first_divergence"])

    def test_arithmetic_micro_rom_corpus_covers_byte_and_long_adc_sbc(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)

            adc_w_rom = tmp / "arith_adc_w.ngc"
            sbc_w_rom = tmp / "arith_sbc_w.ngc"
            adc_xwa_rom = tmp / "arith_adc_xwa.ngc"
            sbc_xwa_rom = tmp / "arith_sbc_xwa.ngc"
            adc_w_state = tmp / "adc_w.seed.json"
            sbc_w_state = tmp / "sbc_w.seed.json"
            adc_xwa_state = tmp / "adc_xwa.seed.json"
            sbc_xwa_state = tmp / "sbc_xwa.seed.json"

            # adc W, 0x00 ; nop
            self._write_demo_rom(adc_w_rom, 0x00200040, b"\xC8\xC9\x00\x00")
            # sbc W, 0x00 ; nop
            self._write_demo_rom(sbc_w_rom, 0x00200040, b"\xC8\xCB\x00\x00")
            # adc XWA, 0x00000000 ; nop
            self._write_demo_rom(adc_xwa_rom, 0x00200040, b"\xE8\xC9\x00\x00\x00\x00\x00")
            # sbc XWA, 0x00000000 ; nop
            self._write_demo_rom(sbc_xwa_rom, 0x00200040, b"\xE8\xCB\x00\x00\x00\x00\x00")

            for rom_path, state_path, xwa in (
                (adc_w_rom, adc_w_state, 0xAABB7F34),
                (sbc_w_rom, sbc_w_state, 0xAABB8034),
                (adc_xwa_rom, adc_xwa_state, 0x7FFFFFFF),
                (sbc_xwa_rom, sbc_xwa_state, 0x80000000),
            ):
                machine = load_machine_state(rom_path)
                cpu = replace(
                    machine.cpu,
                    flags=StatusFlags(sf=False, zf=False, vf=False, hf=False, cf=True),
                    regs=replace(machine.cpu.regs, xwa=xwa),
                )
                save_savestate(
                    state_path,
                    build_savestate_payload(
                        rom_path=rom_path,
                        rom_header=machine.header,
                        cpu=cpu,
                        writable_overlay={},
                    ),
                )

            cases = [
                {
                    "rom": adc_w_rom,
                    "name": "arith-adc-w-carry-in",
                    "save_args": ["--count", "2", "--seed-from", str(adc_w_state)],
                    "expected_assembly": "adc W, 0x00",
                    "expected_final_pc": "0x00200044",
                    "expected_flag_after": {"S": True, "V": True, "H": True, "C": False},
                },
                {
                    "rom": sbc_w_rom,
                    "name": "arith-sbc-w-borrow-in",
                    "save_args": ["--count", "2", "--seed-from", str(sbc_w_state)],
                    "expected_assembly": "sbc W, 0x00",
                    "expected_final_pc": "0x00200044",
                    "expected_flag_after": {"V": True, "H": True, "C": False},
                },
                {
                    "rom": adc_xwa_rom,
                    "name": "arith-adc-xwa-carry-in",
                    "save_args": ["--count", "2", "--seed-from", str(adc_xwa_state)],
                    "expected_assembly": "adc XWA, 0x00000000",
                    "expected_final_pc": "0x00200047",
                    "expected_flag_after": {"S": True, "V": True, "H": True, "C": False},
                },
                {
                    "rom": sbc_xwa_rom,
                    "name": "arith-sbc-xwa-borrow-in",
                    "save_args": ["--count", "2", "--seed-from", str(sbc_xwa_state)],
                    "expected_assembly": "sbc XWA, 0x00000000",
                    "expected_final_pc": "0x00200047",
                    "expected_flag_after": {"V": True, "H": True, "C": False},
                },
            ]

            for case in cases:
                stdout = io.StringIO()
                with redirect_stdout(stdout):
                    exit_code = main(
                        [
                            "eventlog",
                            "golden-save",
                            str(case["rom"]),
                            str(case["name"]),
                            *case["save_args"],
                            "--json",
                        ]
                    )
                self.assertEqual(exit_code, 0, msg=str(case["name"]))
                save_payload = json.loads(stdout.getvalue())
                self.assertEqual(save_payload["name"], case["name"])
                self.assertEqual(save_payload["final_cpu_pc_hex"], case["expected_final_pc"])

                golden = load_named_golden(case["rom"], case["name"])  # type: ignore[arg-type]
                events = golden.payload["events"]
                assert isinstance(events, list)
                self.assertEqual(events[0]["assembly"], case["expected_assembly"])
                self.assertEqual(events[1]["assembly"], "nop")
                flag_changes = events[0]["flag_changes"]
                assert isinstance(flag_changes, list)
                observed_after = {change["name"]: change["after"] for change in flag_changes}
                for flag_name, expected_after in case["expected_flag_after"].items():  # type: ignore[union-attr]
                    self.assertEqual(
                        observed_after.get(flag_name),
                        expected_after,
                        msg=f"{case['name']} missing {flag_name}",
                    )

                stdout = io.StringIO()
                with redirect_stdout(stdout):
                    exit_code = main(
                        [
                            "eventlog",
                            "golden-check",
                            str(case["rom"]),
                            str(case["name"]),
                            *case["save_args"],
                            "--json",
                        ]
                    )
                self.assertEqual(exit_code, 0, msg=str(case["name"]))
                check_payload = json.loads(stdout.getvalue())
                self.assertEqual(check_payload["status"], "match")
                self.assertIsNone(check_payload["diff"]["first_divergence"])

    def test_arithmetic_micro_rom_corpus_covers_byte_and_long_half_carry(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)

            add_w_rom = tmp / "arith_add_w_h.ngc"
            sub_w_rom = tmp / "arith_sub_w_h.ngc"
            add_xwa_rom = tmp / "arith_add_xwa_h.ngc"
            sub_xwa_rom = tmp / "arith_sub_xwa_h.ngc"

            # add W, 0x01 ; nop
            self._write_demo_rom(add_w_rom, 0x00200040, b"\xC8\xC8\x01\x00")
            # sub W, 0x01 ; nop
            self._write_demo_rom(sub_w_rom, 0x00200040, b"\xC8\xCA\x01\x00")
            # add XWA, 0x00000001 ; nop
            self._write_demo_rom(add_xwa_rom, 0x00200040, b"\xE8\xC8\x01\x00\x00\x00\x00")
            # sub XWA, 0x00000001 ; nop
            self._write_demo_rom(sub_xwa_rom, 0x00200040, b"\xE8\xCA\x01\x00\x00\x00\x00")

            cases = [
                {
                    "rom": add_w_rom,
                    "name": "arith-add-w-half-carry",
                    "save_args": ["--count", "2", "--seed-reg", "XWA=0xAABB0F34"],
                    "expected_assembly": "add W, 0x01",
                    "expected_final_pc": "0x00200044",
                    "expected_flag_after": {"H": True},
                },
                {
                    "rom": sub_w_rom,
                    "name": "arith-sub-w-half-borrow",
                    "save_args": ["--count", "2", "--seed-reg", "XWA=0xAABB1034"],
                    "expected_assembly": "sub W, 0x01",
                    "expected_final_pc": "0x00200044",
                    "expected_flag_after": {"H": True},
                },
                {
                    "rom": add_xwa_rom,
                    "name": "arith-add-xwa-half-carry",
                    "save_args": ["--count", "2", "--seed-reg", "XWA=0x0000000F"],
                    "expected_assembly": "add XWA, 0x00000001",
                    "expected_final_pc": "0x00200047",
                    "expected_flag_after": {"H": True},
                },
                {
                    "rom": sub_xwa_rom,
                    "name": "arith-sub-xwa-half-borrow",
                    "save_args": ["--count", "2", "--seed-reg", "XWA=0x00000010"],
                    "expected_assembly": "sub XWA, 0x00000001",
                    "expected_final_pc": "0x00200047",
                    "expected_flag_after": {"H": True},
                },
            ]

            for case in cases:
                stdout = io.StringIO()
                with redirect_stdout(stdout):
                    exit_code = main(
                        [
                            "eventlog",
                            "golden-save",
                            str(case["rom"]),
                            str(case["name"]),
                            *case["save_args"],
                            "--json",
                        ]
                    )
                self.assertEqual(exit_code, 0, msg=str(case["name"]))
                save_payload = json.loads(stdout.getvalue())
                self.assertEqual(save_payload["name"], case["name"])
                self.assertEqual(save_payload["final_cpu_pc_hex"], case["expected_final_pc"])

                golden = load_named_golden(case["rom"], case["name"])  # type: ignore[arg-type]
                events = golden.payload["events"]
                assert isinstance(events, list)
                self.assertEqual(events[0]["assembly"], case["expected_assembly"])
                self.assertEqual(events[1]["assembly"], "nop")
                flag_changes = events[0]["flag_changes"]
                assert isinstance(flag_changes, list)
                observed_after = {change["name"]: change["after"] for change in flag_changes}
                for flag_name, expected_after in case["expected_flag_after"].items():  # type: ignore[union-attr]
                    self.assertEqual(
                        observed_after.get(flag_name),
                        expected_after,
                        msg=f"{case['name']} missing {flag_name}",
                    )

                stdout = io.StringIO()
                with redirect_stdout(stdout):
                    exit_code = main(
                        [
                            "eventlog",
                            "golden-check",
                            str(case["rom"]),
                            str(case["name"]),
                            *case["save_args"],
                            "--json",
                        ]
                    )
                self.assertEqual(exit_code, 0, msg=str(case["name"]))
                check_payload = json.loads(stdout.getvalue())
                self.assertEqual(check_payload["status"], "match")
                self.assertIsNone(check_payload["diff"]["first_divergence"])

    def test_arithmetic_micro_rom_corpus_covers_byte_and_long_cp_without_writeback(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)

            cp_w_rom = tmp / "arith_cp_w.ngc"
            cp_xwa_rom = tmp / "arith_cp_xwa.ngc"

            # cp W, 0x34 ; nop
            self._write_demo_rom(cp_w_rom, 0x00200040, b"\xC8\xCF\x34\x00")
            # cp XWA, 0x00000001 ; nop
            self._write_demo_rom(cp_xwa_rom, 0x00200040, b"\xE8\xCF\x01\x00\x00\x00\x00")

            cases = [
                {
                    "rom": cp_w_rom,
                    "name": "arith-cp-w-zero-no-writeback",
                    "save_args": ["--count", "2", "--seed-reg", "XWA=0xAABB3434"],
                    "expected_assembly": "cp W, 0x34",
                    "expected_final_pc": "0x00200044",
                    "expected_flag_after": {"Z": True},
                },
                {
                    "rom": cp_xwa_rom,
                    "name": "arith-cp-xwa-zero-no-writeback",
                    "save_args": ["--count", "2", "--seed-reg", "XWA=0x00000001"],
                    "expected_assembly": "cp XWA, 0x00000001",
                    "expected_final_pc": "0x00200047",
                    "expected_flag_after": {"Z": True},
                },
            ]

            for case in cases:
                stdout = io.StringIO()
                with redirect_stdout(stdout):
                    exit_code = main(
                        [
                            "eventlog",
                            "golden-save",
                            str(case["rom"]),
                            str(case["name"]),
                            *case["save_args"],
                            "--json",
                        ]
                    )
                self.assertEqual(exit_code, 0, msg=str(case["name"]))
                save_payload = json.loads(stdout.getvalue())
                self.assertEqual(save_payload["name"], case["name"])
                self.assertEqual(save_payload["final_cpu_pc_hex"], case["expected_final_pc"])

                golden = load_named_golden(case["rom"], case["name"])  # type: ignore[arg-type]
                events = golden.payload["events"]
                assert isinstance(events, list)
                self.assertEqual(events[0]["assembly"], case["expected_assembly"])
                self.assertEqual(events[1]["assembly"], "nop")
                self.assertEqual(events[0]["written_registers"], ["PC"])
                flag_changes = events[0]["flag_changes"]
                assert isinstance(flag_changes, list)
                observed_after = {change["name"]: change["after"] for change in flag_changes}
                for flag_name, expected_after in case["expected_flag_after"].items():  # type: ignore[union-attr]
                    self.assertEqual(
                        observed_after.get(flag_name),
                        expected_after,
                        msg=f"{case['name']} missing {flag_name}",
                    )

                stdout = io.StringIO()
                with redirect_stdout(stdout):
                    exit_code = main(
                        [
                            "eventlog",
                            "golden-check",
                            str(case["rom"]),
                            str(case["name"]),
                            *case["save_args"],
                            "--json",
                        ]
                    )
                self.assertEqual(exit_code, 0, msg=str(case["name"]))
                check_payload = json.loads(stdout.getvalue())
                self.assertEqual(check_payload["status"], "match")
                self.assertIsNone(check_payload["diff"]["first_divergence"])

    def test_arithmetic_micro_rom_corpus_covers_byte_and_long_cp_flags(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)

            cp_w_borrow_rom = tmp / "arith_cp_w_borrow.ngc"
            cp_w_overflow_rom = tmp / "arith_cp_w_overflow.ngc"
            cp_xwa_borrow_rom = tmp / "arith_cp_xwa_borrow.ngc"
            cp_xwa_overflow_rom = tmp / "arith_cp_xwa_overflow.ngc"

            # cp W, 0x01 ; nop
            self._write_demo_rom(cp_w_borrow_rom, 0x00200040, b"\xC8\xCF\x01\x00")
            self._write_demo_rom(cp_w_overflow_rom, 0x00200040, b"\xC8\xCF\x01\x00")
            # cp XWA, 0x00000001 ; nop
            self._write_demo_rom(cp_xwa_borrow_rom, 0x00200040, b"\xE8\xCF\x01\x00\x00\x00\x00")
            self._write_demo_rom(cp_xwa_overflow_rom, 0x00200040, b"\xE8\xCF\x01\x00\x00\x00\x00")

            cases = [
                {
                    "rom": cp_w_borrow_rom,
                    "name": "arith-cp-w-borrow-sign-no-writeback",
                    "save_args": ["--count", "2", "--seed-reg", "XWA=0xAABB0034"],
                    "expected_assembly": "cp W, 0x01",
                    "expected_final_pc": "0x00200044",
                    "expected_flag_after": {"S": True, "C": True},
                },
                {
                    "rom": cp_w_overflow_rom,
                    "name": "arith-cp-w-overflow-no-writeback",
                    "save_args": ["--count", "2", "--seed-reg", "XWA=0xAABB8034"],
                    "expected_assembly": "cp W, 0x01",
                    "expected_final_pc": "0x00200044",
                    "expected_flag_after": {"V": True},
                },
                {
                    "rom": cp_xwa_borrow_rom,
                    "name": "arith-cp-xwa-borrow-sign-no-writeback",
                    "save_args": ["--count", "2", "--seed-reg", "XWA=0x00000000"],
                    "expected_assembly": "cp XWA, 0x00000001",
                    "expected_final_pc": "0x00200047",
                    "expected_flag_after": {"S": True, "C": True},
                },
                {
                    "rom": cp_xwa_overflow_rom,
                    "name": "arith-cp-xwa-overflow-no-writeback",
                    "save_args": ["--count", "2", "--seed-reg", "XWA=0x80000000"],
                    "expected_assembly": "cp XWA, 0x00000001",
                    "expected_final_pc": "0x00200047",
                    "expected_flag_after": {"V": True},
                },
            ]

            for case in cases:
                stdout = io.StringIO()
                with redirect_stdout(stdout):
                    exit_code = main(
                        [
                            "eventlog",
                            "golden-save",
                            str(case["rom"]),
                            str(case["name"]),
                            *case["save_args"],
                            "--json",
                        ]
                    )
                self.assertEqual(exit_code, 0, msg=str(case["name"]))
                save_payload = json.loads(stdout.getvalue())
                self.assertEqual(save_payload["name"], case["name"])
                self.assertEqual(save_payload["final_cpu_pc_hex"], case["expected_final_pc"])

                golden = load_named_golden(case["rom"], case["name"])  # type: ignore[arg-type]
                events = golden.payload["events"]
                assert isinstance(events, list)
                self.assertEqual(events[0]["assembly"], case["expected_assembly"])
                self.assertEqual(events[1]["assembly"], "nop")
                self.assertEqual(events[0]["written_registers"], ["PC"])
                flag_changes = events[0]["flag_changes"]
                assert isinstance(flag_changes, list)
                observed_after = {change["name"]: change["after"] for change in flag_changes}
                for flag_name, expected_after in case["expected_flag_after"].items():  # type: ignore[union-attr]
                    self.assertEqual(
                        observed_after.get(flag_name),
                        expected_after,
                        msg=f"{case['name']} missing {flag_name}",
                    )

                stdout = io.StringIO()
                with redirect_stdout(stdout):
                    exit_code = main(
                        [
                            "eventlog",
                            "golden-check",
                            str(case["rom"]),
                            str(case["name"]),
                            *case["save_args"],
                            "--json",
                        ]
                    )
                self.assertEqual(exit_code, 0, msg=str(case["name"]))
                check_payload = json.loads(stdout.getvalue())
                self.assertEqual(check_payload["status"], "match")
                self.assertIsNone(check_payload["diff"]["first_divergence"])

    def test_shift_micro_rom_corpus_covers_byte_and_long_immediates(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)

            rlc_w_rom = tmp / "shift_rlc_w.ngc"
            sra_w_rom = tmp / "shift_sra_w.ngc"
            rrc_xwa_rom = tmp / "shift_rrc_xwa.ngc"
            srl_xwa_rom = tmp / "shift_srl_xwa.ngc"

            # rlc 1, W ; nop
            self._write_demo_rom(rlc_w_rom, 0x00200040, b"\xC8\xE8\x01\x00")
            # sra 1, W ; nop
            self._write_demo_rom(sra_w_rom, 0x00200040, b"\xC8\xED\x01\x00")
            # rrc 1, XWA ; nop
            self._write_demo_rom(rrc_xwa_rom, 0x00200040, b"\xE8\xE9\x01\x00")
            # srl 1, XWA ; nop
            self._write_demo_rom(srl_xwa_rom, 0x00200040, b"\xE8\xEF\x01\x00")

            cases = [
                {
                    "rom": rlc_w_rom,
                    "name": "shift-rlc-w-carry-sign",
                    "save_args": ["--count", "2", "--seed-reg", "XWA=0xAABBC034"],
                    "expected_assembly": "rlc 1, W",
                    "expected_written_registers": ["W", "PC"],
                    "expected_final_pc": "0x00200044",
                    "expected_flag_after": {"S": True, "C": True},
                },
                {
                    "rom": sra_w_rom,
                    "name": "shift-sra-w-carry-zero",
                    "save_args": ["--count", "2", "--seed-reg", "XWA=0xAABB0134"],
                    "expected_assembly": "sra 1, W",
                    "expected_written_registers": ["W", "PC"],
                    "expected_final_pc": "0x00200044",
                    "expected_flag_after": {"Z": True, "C": True},
                },
                {
                    "rom": rrc_xwa_rom,
                    "name": "shift-rrc-xwa-carry-sign",
                    "save_args": ["--count", "2", "--seed-reg", "XWA=0x00000001"],
                    "expected_assembly": "rrc 1, XWA",
                    "expected_written_registers": ["XWA", "PC"],
                    "expected_final_pc": "0x00200044",
                    "expected_flag_after": {"S": True, "C": True},
                },
                {
                    "rom": srl_xwa_rom,
                    "name": "shift-srl-xwa-carry-zero",
                    "save_args": ["--count", "2", "--seed-reg", "XWA=0x00000001"],
                    "expected_assembly": "srl 1, XWA",
                    "expected_written_registers": ["XWA", "PC"],
                    "expected_final_pc": "0x00200044",
                    "expected_flag_after": {"Z": True, "C": True},
                },
            ]

            for case in cases:
                stdout = io.StringIO()
                with redirect_stdout(stdout):
                    exit_code = main(
                        [
                            "eventlog",
                            "golden-save",
                            str(case["rom"]),
                            str(case["name"]),
                            *case["save_args"],
                            "--json",
                        ]
                    )
                self.assertEqual(exit_code, 0, msg=str(case["name"]))
                save_payload = json.loads(stdout.getvalue())
                self.assertEqual(save_payload["name"], case["name"])
                self.assertEqual(save_payload["final_cpu_pc_hex"], case["expected_final_pc"])

                golden = load_named_golden(case["rom"], case["name"])  # type: ignore[arg-type]
                events = golden.payload["events"]
                assert isinstance(events, list)
                self.assertEqual(events[0]["assembly"], case["expected_assembly"])
                self.assertEqual(events[1]["assembly"], "nop")
                self.assertEqual(events[0]["written_registers"], case["expected_written_registers"])
                flag_changes = events[0]["flag_changes"]
                assert isinstance(flag_changes, list)
                observed_after = {change["name"]: change["after"] for change in flag_changes}
                for flag_name, expected_after in case["expected_flag_after"].items():  # type: ignore[union-attr]
                    self.assertEqual(
                        observed_after.get(flag_name),
                        expected_after,
                        msg=f"{case['name']} missing {flag_name}",
                    )

                stdout = io.StringIO()
                with redirect_stdout(stdout):
                    exit_code = main(
                        [
                            "eventlog",
                            "golden-check",
                            str(case["rom"]),
                            str(case["name"]),
                            *case["save_args"],
                            "--json",
                        ]
                    )
                self.assertEqual(exit_code, 0, msg=str(case["name"]))
                check_payload = json.loads(stdout.getvalue())
                self.assertEqual(check_payload["status"], "match")
                self.assertIsNone(check_payload["diff"]["first_divergence"])

    def test_bitops_micro_rom_corpus_covers_abs16_res_set_memory(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)

            builtin_rom = tmp / "bitops_builtin.ngc"
            overlay_rom = tmp / "bitops_overlay.ngc"
            overlay_state = tmp / "bitops_overlay.seed.json"

            # res 5, (0x6F86) ; set 6, (0x6F86)
            self._write_demo_rom(builtin_rom, 0x00200040, b"\xF1\x86\x6F\xB5\xF1\x86\x6F\xBE")
            # set 3, (0x4000) ; res 7, (0x4000)
            self._write_demo_rom(overlay_rom, 0x00200040, b"\xF1\x00\x40\xBB\xF1\x00\x40\xB7")

            overlay_machine = load_machine_state(overlay_rom)
            save_savestate(
                overlay_state,
                build_savestate_payload(
                    rom_path=overlay_rom,
                    rom_header=overlay_machine.header,
                    cpu=overlay_machine.cpu,
                    writable_overlay={0x004000: 0x80},
                ),
            )

            cases = [
                {
                    "rom": builtin_rom,
                    "name": "bitops-res-set-abs16-builtin",
                    "save_args": ["--count", "2"],
                    "expected_assemblies": ["res 5, (0x6F86)", "set 6, (0x6F86)"],
                    "expected_final_pc": "0x00200048",
                    "expected_writes": [
                        {"address_hex": "0x006F86", "data_hex": "00"},
                        {"address_hex": "0x006F86", "data_hex": "40"},
                    ],
                },
                {
                    "rom": overlay_rom,
                    "name": "bitops-set-res-abs16-overlay",
                    "save_args": ["--count", "2", "--seed-from", str(overlay_state)],
                    "expected_assemblies": ["set 3, (0x4000)", "res 7, (0x4000)"],
                    "expected_final_pc": "0x00200048",
                    "expected_writes": [
                        {"address_hex": "0x004000", "data_hex": "88"},
                        {"address_hex": "0x004000", "data_hex": "08"},
                    ],
                },
            ]

            for case in cases:
                stdout = io.StringIO()
                with redirect_stdout(stdout):
                    exit_code = main(
                        [
                            "eventlog",
                            "golden-save",
                            str(case["rom"]),
                            str(case["name"]),
                            *case["save_args"],
                            "--json",
                        ]
                    )
                self.assertEqual(exit_code, 0, msg=str(case["name"]))
                save_payload = json.loads(stdout.getvalue())
                self.assertEqual(save_payload["name"], case["name"])
                self.assertEqual(save_payload["final_cpu_pc_hex"], case["expected_final_pc"])

                golden = load_named_golden(case["rom"], case["name"])  # type: ignore[arg-type]
                events = golden.payload["events"]
                assert isinstance(events, list)
                self.assertEqual(
                    [event["assembly"] for event in events],  # type: ignore[index]
                    case["expected_assemblies"],
                )
                for event, expected_write in zip(events, case["expected_writes"]):  # type: ignore[arg-type]
                    self.assertEqual(event["written_registers"], ["PC"])
                    self.assertEqual(len(event["memory_writes"]), 1)
                    self.assertEqual(event["memory_writes"][0]["address_hex"], expected_write["address_hex"])
                    self.assertEqual(event["memory_writes"][0]["data_hex"], expected_write["data_hex"])

                stdout = io.StringIO()
                with redirect_stdout(stdout):
                    exit_code = main(
                        [
                            "eventlog",
                            "golden-check",
                            str(case["rom"]),
                            str(case["name"]),
                            *case["save_args"],
                            "--json",
                        ]
                    )
                self.assertEqual(exit_code, 0, msg=str(case["name"]))
                check_payload = json.loads(stdout.getvalue())
                self.assertEqual(check_payload["status"], "match")
                self.assertIsNone(check_payload["diff"]["first_divergence"])

    def test_memory_store_micro_rom_corpus_covers_abs16_and_abs24(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)

            abs16_imm8_rom = tmp / "mem_abs16_imm8.ngc"
            abs16_reg8_rom = tmp / "mem_abs16_reg8.ngc"
            abs24_imm16_rom = tmp / "mem_abs24_imm16.ngc"
            abs16_reg32_rom = tmp / "mem_abs16_reg32.ngc"

            # ld (0x4000), 0x5A ; nop
            self._write_demo_rom(abs16_imm8_rom, 0x00200040, b"\xF1\x00\x40\x00\x5A\x00")
            # ld (0x4001), A ; nop
            self._write_demo_rom(abs16_reg8_rom, 0x00200040, b"\xF1\x01\x40\x41\x00")
            # ldw (0x004020), 0x1234 ; nop
            self._write_demo_rom(abs24_imm16_rom, 0x00200040, b"\xF2\x20\x40\x00\x02\x34\x12\x00")
            # ld (0x4004), XWA ; nop
            self._write_demo_rom(abs16_reg32_rom, 0x00200040, b"\xF1\x04\x40\x60\x00")

            cases = [
                {
                    "rom": abs16_imm8_rom,
                    "name": "memory-ld-abs16-imm8-overlay",
                    "save_args": ["--count", "2"],
                    "expected_assemblies": ["ld (0x4000), 0x5A", "nop"],
                    "expected_final_pc": "0x00200046",
                    "expected_writes": [
                        {"address_hex": "0x004000", "data_hex": "5A"},
                    ],
                },
                {
                    "rom": abs16_reg8_rom,
                    "name": "memory-ld-abs16-a-overlay",
                    "save_args": ["--count", "2", "--seed-reg", "XWA=0xAABBCC42"],
                    "expected_assemblies": ["ld (0x4001), A", "nop"],
                    "expected_final_pc": "0x00200045",
                    "expected_writes": [
                        {"address_hex": "0x004001", "data_hex": "42"},
                    ],
                },
                {
                    "rom": abs24_imm16_rom,
                    "name": "memory-ldw-abs24-imm16-overlay",
                    "save_args": ["--count", "2"],
                    "expected_assemblies": ["ldw (0x004020), 0x1234", "nop"],
                    "expected_final_pc": "0x00200048",
                    "expected_writes": [
                        {"address_hex": "0x004020", "data_hex": "34 12"},
                    ],
                },
                {
                    "rom": abs16_reg32_rom,
                    "name": "memory-ld-abs16-xwa-overlay",
                    "save_args": ["--count", "2", "--seed-reg", "XWA=0x12345678"],
                    "expected_assemblies": ["ld (0x4004), XWA", "nop"],
                    "expected_final_pc": "0x00200045",
                    "expected_writes": [
                        {"address_hex": "0x004004", "data_hex": "78 56 34 12"},
                    ],
                },
            ]

            for case in cases:
                stdout = io.StringIO()
                with redirect_stdout(stdout):
                    exit_code = main(
                        [
                            "eventlog",
                            "golden-save",
                            str(case["rom"]),
                            str(case["name"]),
                            *case["save_args"],
                            "--json",
                        ]
                    )
                self.assertEqual(exit_code, 0, msg=str(case["name"]))
                save_payload = json.loads(stdout.getvalue())
                self.assertEqual(save_payload["name"], case["name"])
                self.assertEqual(save_payload["final_cpu_pc_hex"], case["expected_final_pc"])

                golden = load_named_golden(case["rom"], case["name"])  # type: ignore[arg-type]
                events = golden.payload["events"]
                assert isinstance(events, list)
                self.assertEqual(
                    [event["assembly"] for event in events],  # type: ignore[index]
                    case["expected_assemblies"],
                )
                first_event = events[0]
                self.assertEqual(first_event["written_registers"], ["PC"])
                self.assertEqual(len(first_event["memory_writes"]), len(case["expected_writes"]))
                for observed_write, expected_write in zip(first_event["memory_writes"], case["expected_writes"]):  # type: ignore[arg-type]
                    self.assertEqual(observed_write["address_hex"], expected_write["address_hex"])
                    self.assertEqual(observed_write["data_hex"], expected_write["data_hex"])

                stdout = io.StringIO()
                with redirect_stdout(stdout):
                    exit_code = main(
                        [
                            "eventlog",
                            "golden-check",
                            str(case["rom"]),
                            str(case["name"]),
                            *case["save_args"],
                            "--json",
                        ]
                    )
                self.assertEqual(exit_code, 0, msg=str(case["name"]))
                check_payload = json.loads(stdout.getvalue())
                self.assertEqual(check_payload["status"], "match")
                self.assertIsNone(check_payload["diff"]["first_divergence"])

    def test_stack_micro_rom_corpus_covers_push_pop_word_and_long(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)

            stack_wa_rom = tmp / "stack_wa.ngc"
            stack_xiz_rom = tmp / "stack_xiz.ngc"

            # push WA ; pop WA
            self._write_demo_rom(stack_wa_rom, 0x00200040, b"\x28\x48")
            # push XIZ ; pop XIZ
            self._write_demo_rom(stack_xiz_rom, 0x00200040, b"\x3E\x5E")

            cases = [
                {
                    "rom": stack_wa_rom,
                    "name": "stack-push-pop-wa-roundtrip",
                    "save_args": ["--count", "2", "--seed-reg", "XWA=0xAABB1234", "--seed-xsp", "0x4100"],
                    "expected_assemblies": ["push WA", "pop WA"],
                    "expected_final_pc": "0x00200042",
                    "expected_push_writes": [{"address_hex": "0x0040FE", "data_hex": "34 12"}],
                },
                {
                    "rom": stack_xiz_rom,
                    "name": "stack-push-pop-xiz-roundtrip",
                    "save_args": ["--count", "2", "--seed-reg", "XIZ=0x12345678", "--seed-xsp", "0x4100"],
                    "expected_assemblies": ["push XIZ", "pop XIZ"],
                    "expected_final_pc": "0x00200042",
                    "expected_push_writes": [{"address_hex": "0x0040FC", "data_hex": "78 56 34 12"}],
                },
            ]

            for case in cases:
                stdout = io.StringIO()
                with redirect_stdout(stdout):
                    exit_code = main(
                        [
                            "eventlog",
                            "golden-save",
                            str(case["rom"]),
                            str(case["name"]),
                            *case["save_args"],
                            "--json",
                        ]
                    )
                self.assertEqual(exit_code, 0, msg=str(case["name"]))
                save_payload = json.loads(stdout.getvalue())
                self.assertEqual(save_payload["name"], case["name"])
                self.assertEqual(save_payload["final_cpu_pc_hex"], case["expected_final_pc"])

                golden = load_named_golden(case["rom"], case["name"])  # type: ignore[arg-type]
                events = golden.payload["events"]
                assert isinstance(events, list)
                self.assertEqual(
                    [event["assembly"] for event in events],  # type: ignore[index]
                    case["expected_assemblies"],
                )

                push_event = events[0]
                self.assertEqual(push_event["written_registers"], ["XSP", "PC"])
                self.assertEqual(len(push_event["memory_writes"]), len(case["expected_push_writes"]))
                for observed_write, expected_write in zip(push_event["memory_writes"], case["expected_push_writes"]):  # type: ignore[arg-type]
                    self.assertEqual(observed_write["address_hex"], expected_write["address_hex"])
                    self.assertEqual(observed_write["data_hex"], expected_write["data_hex"])

                pop_event = events[1]
                self.assertEqual(pop_event["memory_writes"], [])
                self.assertIn("XSP", pop_event["written_registers"])
                self.assertIn("PC", pop_event["written_registers"])

                stdout = io.StringIO()
                with redirect_stdout(stdout):
                    exit_code = main(
                        [
                            "eventlog",
                            "golden-check",
                            str(case["rom"]),
                            str(case["name"]),
                            *case["save_args"],
                            "--json",
                        ]
                    )
                self.assertEqual(exit_code, 0, msg=str(case["name"]))
                check_payload = json.loads(stdout.getvalue())
                self.assertEqual(check_payload["status"], "match")
                self.assertIsNone(check_payload["diff"]["first_divergence"])

    def test_stack_micro_rom_corpus_covers_link_unlk_roundtrips(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)

            link_xwa_rom = tmp / "link_xwa.ngc"
            link_xbc_rom = tmp / "link_xbc.ngc"

            # link XWA, 0 ; unlk XWA
            self._write_demo_rom(link_xwa_rom, 0x00200040, b"\xE8\x0C\x00\x00\xE8\x0D")
            # link XBC, 8 ; unlk XBC
            self._write_demo_rom(link_xbc_rom, 0x00200040, b"\xE9\x0C\x08\x00\xE9\x0D")

            cases = [
                {
                    "rom": link_xwa_rom,
                    "name": "stack-link-unlk-xwa-roundtrip",
                    "save_args": ["--count", "2", "--seed-reg", "XWA=0x12345678", "--seed-xsp", "0x4100"],
                    "expected_assemblies": ["link XWA, 0", "unlk XWA"],
                    "expected_final_pc": "0x00200046",
                    "expected_link_writes": [{"address_hex": "0x0040FC", "data_hex": "78 56 34 12"}],
                    "expected_link_written_registers": ["XWA", "XSP", "PC"],
                    "expected_unlk_written_registers": ["XWA", "XSP", "PC"],
                },
                {
                    "rom": link_xbc_rom,
                    "name": "stack-link-unlk-xbc-positive-frame",
                    "save_args": ["--count", "2", "--seed-reg", "XBC=0x89ABCDEF", "--seed-xsp", "0x4100"],
                    "expected_assemblies": ["link XBC, 8", "unlk XBC"],
                    "expected_final_pc": "0x00200046",
                    "expected_link_writes": [{"address_hex": "0x0040FC", "data_hex": "EF CD AB 89"}],
                    "expected_link_written_registers": ["XBC", "XSP", "PC"],
                    "expected_unlk_written_registers": ["XBC", "XSP", "PC"],
                },
            ]

            for case in cases:
                stdout = io.StringIO()
                with redirect_stdout(stdout):
                    exit_code = main(
                        [
                            "eventlog",
                            "golden-save",
                            str(case["rom"]),
                            str(case["name"]),
                            *case["save_args"],
                            "--json",
                        ]
                    )
                self.assertEqual(exit_code, 0, msg=str(case["name"]))
                save_payload = json.loads(stdout.getvalue())
                self.assertEqual(save_payload["name"], case["name"])
                self.assertEqual(save_payload["final_cpu_pc_hex"], case["expected_final_pc"])

                golden = load_named_golden(case["rom"], case["name"])  # type: ignore[arg-type]
                events = golden.payload["events"]
                assert isinstance(events, list)
                self.assertEqual(
                    [event["assembly"] for event in events],  # type: ignore[index]
                    case["expected_assemblies"],
                )

                link_event = events[0]
                self.assertEqual(link_event["written_registers"], case["expected_link_written_registers"])
                self.assertEqual(len(link_event["memory_writes"]), len(case["expected_link_writes"]))
                for observed_write, expected_write in zip(link_event["memory_writes"], case["expected_link_writes"]):  # type: ignore[arg-type]
                    self.assertEqual(observed_write["address_hex"], expected_write["address_hex"])
                    self.assertEqual(observed_write["data_hex"], expected_write["data_hex"])

                unlk_event = events[1]
                self.assertEqual(unlk_event["memory_writes"], [])
                self.assertEqual(unlk_event["written_registers"], case["expected_unlk_written_registers"])

                stdout = io.StringIO()
                with redirect_stdout(stdout):
                    exit_code = main(
                        [
                            "eventlog",
                            "golden-check",
                            str(case["rom"]),
                            str(case["name"]),
                            *case["save_args"],
                            "--json",
                        ]
                    )
                self.assertEqual(exit_code, 0, msg=str(case["name"]))
                check_payload = json.loads(stdout.getvalue())
                self.assertEqual(check_payload["status"], "match")
                self.assertIsNone(check_payload["diff"]["first_divergence"])

    def test_stack_micro_rom_corpus_covers_link_xiy_large_frame_quirk_stop(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            quirk_rom = tmp / "link_xiy_quirk.ngc"

            # link XIY, 8 ; unlk XIY
            self._write_demo_rom(quirk_rom, 0x00200040, b"\xED\x0C\x08\x00\xED\x0D")

            stdout = io.StringIO()
            with redirect_stdout(stdout):
                exit_code = main(
                    [
                        "eventlog",
                        "golden-save",
                        str(quirk_rom),
                        "stack-link-xiy-large-frame-silicon-broken",
                        "--count",
                        "2",
                        "--seed-reg",
                        "XIY=0x11223344",
                        "--seed-xsp",
                        "0x4100",
                        "--json",
                    ]
                )
            self.assertEqual(exit_code, 0)
            save_payload = json.loads(stdout.getvalue())
            self.assertEqual(save_payload["name"], "stack-link-xiy-large-frame-silicon-broken")
            self.assertEqual(save_payload["stop_reason"], "stopped-on-silicon-broken")
            self.assertEqual(save_payload["executed_count"], 0)
            self.assertEqual(save_payload["emitted_count"], 1)
            self.assertEqual(save_payload["final_cpu_pc_hex"], "0x00200040")

            golden = load_named_golden(quirk_rom, "stack-link-xiy-large-frame-silicon-broken")
            events = golden.payload["events"]
            assert isinstance(events, list)
            self.assertEqual(len(events), 1)
            self.assertEqual(events[0]["assembly"], "link XIY, 8")
            self.assertEqual(events[0]["status"], "silicon-broken")
            self.assertEqual(events[0]["written_registers"], [])
            matched_quirk = events[0]["matched_quirk"]
            assert isinstance(matched_quirk, dict)
            self.assertEqual(matched_quirk["quirk_id"], "cpu.link_xiy_large_frame")
            self.assertEqual(
                matched_quirk["database_version"],
                load_known_quirk_database().database_version,
            )

            summary = golden.payload["summary"]
            assert isinstance(summary, dict)
            self.assertEqual(summary["stop_reason"], "stopped-on-silicon-broken")
            matched_quirk_on_stop = summary["matched_quirk_on_stop"]
            assert isinstance(matched_quirk_on_stop, dict)
            self.assertEqual(matched_quirk_on_stop["quirk_id"], "cpu.link_xiy_large_frame")

            stdout = io.StringIO()
            with redirect_stdout(stdout):
                exit_code = main(
                    [
                        "eventlog",
                        "golden-check",
                        str(quirk_rom),
                        "stack-link-xiy-large-frame-silicon-broken",
                        "--count",
                        "2",
                        "--seed-reg",
                        "XIY=0x11223344",
                        "--seed-xsp",
                        "0x4100",
                        "--json",
                    ]
                )
            self.assertEqual(exit_code, 0)
            check_payload = json.loads(stdout.getvalue())
            self.assertEqual(check_payload["status"], "match")
            self.assertIsNone(check_payload["diff"]["first_divergence"])


if __name__ == "__main__":
    unittest.main()
