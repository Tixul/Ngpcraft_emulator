"""Savestate v1 serialization tests for NgpCraft Emulator."""

from __future__ import annotations

import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from core.cpu import (
    GeneralRegisters32,
    StatusFlags,
    create_bootstrap_cpu_state,
    create_unknown_control_registers,
)
from core.machine import load_machine_state
from core.run_steps import load_run_until
from core.savestate import (
    SAVESTATE_FORMAT,
    SAVESTATE_FORMAT_VERSION,
    build_savestate_payload,
    load_savestate,
    save_savestate,
)


def _write_demo_rom(path: Path, entry_point: int = 0x00200040) -> None:
    """Write a small synthetic .ngc ROM for test use."""
    data = bytearray(0x40)
    data[0x00:0x1C] = b"LICENSED BY SNK CORPORATION".ljust(0x1C, b"\x00")
    data[0x1C:0x20] = entry_point.to_bytes(4, "little")
    data[0x20:0x22] = (0x0000).to_bytes(2, "little")
    data[0x22] = 0
    data[0x23] = 0x10
    data[0x24:0x30] = b"SAVE TEST\x00\x00\x00"
    path.write_bytes(bytes(data))


class SavestateBootstrapRoundtripTests(unittest.TestCase):
    def test_save_and_load_bootstrap_state_round_trips(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            rom_path = tmp / "demo.ngc"
            _write_demo_rom(rom_path)
            machine = load_machine_state(rom_path)

            payload = build_savestate_payload(
                rom_path=rom_path,
                rom_header=machine.header,
                cpu=machine.cpu,
                writable_overlay={},
            )

            state_path = tmp / "demo_state.json"
            save_savestate(state_path, payload)

            doc = load_savestate(state_path, expected_rom_path=rom_path)

            self.assertEqual(doc.format_version, SAVESTATE_FORMAT_VERSION)
            self.assertEqual(doc.cpu.pc, 0x00200040)
            self.assertEqual(doc.rom_file_size, rom_path.stat().st_size)
            self.assertEqual(doc.writable_overlay, {})
            self.assertIsNone(doc.matched_on_last_step)

    def test_overlay_bytes_are_preserved_across_roundtrip(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            rom_path = tmp / "demo.ngc"
            _write_demo_rom(rom_path)
            machine = load_machine_state(rom_path)

            seeded_cpu = replace(
                machine.cpu,
                regs=replace(machine.cpu.regs, xwa=0x11223344, xsp=0x00006C00),
                flags=StatusFlags(sf=False, zf=True, vf=None, hf=None, cf=False),
                alt_flags=StatusFlags(sf=True, zf=False, vf=True, hf=False, cf=True, nf=True),
                iff_enabled=True,
            )
            overlay = {
                0x004000: 0x42,
                0x004001: 0xAB,
                0x006FFC: 0xFE,
            }

            payload = build_savestate_payload(
                rom_path=rom_path,
                rom_header=machine.header,
                cpu=seeded_cpu,
                writable_overlay=overlay,
                note="unit-test roundtrip",
            )
            state_path = tmp / "demo_state.json"
            save_savestate(state_path, payload)

            doc = load_savestate(state_path, expected_rom_path=rom_path)

            self.assertEqual(doc.cpu.regs.xwa, 0x11223344)
            self.assertEqual(doc.cpu.regs.xsp, 0x00006C00)
            self.assertEqual(doc.cpu.flags.zf, True)
            self.assertEqual(doc.cpu.flags.sf, False)
            self.assertIsNone(doc.cpu.flags.vf)
            assert doc.cpu.alt_flags is not None
            self.assertTrue(doc.cpu.alt_flags.sf)
            self.assertTrue(doc.cpu.alt_flags.cf)
            self.assertTrue(doc.cpu.alt_flags.nf)
            self.assertTrue(doc.cpu.iff_enabled)
            self.assertEqual(doc.writable_overlay, overlay)
            self.assertEqual(doc.note, "unit-test roundtrip")

    def test_v2_fields_nf_iff_level_rfp_round_trip(self) -> None:
        """Savestate v2 carries the new SR-derived fields end to end."""
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            rom_path = tmp / "demo.ngc"
            _write_demo_rom(rom_path)
            machine = load_machine_state(rom_path)

            seeded_cpu = replace(
                machine.cpu,
                flags=StatusFlags(
                    sf=True, zf=False, vf=True, hf=False, cf=True, nf=True,
                ),
                iff_enabled=True,
                iff_level=3,
                rfp=2,
                control_registers=replace(
                    create_unknown_control_registers(),
                    dmac=(0x1234, None, None, None),
                    dmam=(0x56, None, None, None),
                    intnest=2,
                ),
            )

            payload = build_savestate_payload(
                rom_path=rom_path,
                rom_header=machine.header,
                cpu=seeded_cpu,
                writable_overlay={},
            )
            state_path = tmp / "demo_state.json"
            save_savestate(state_path, payload)

            doc = load_savestate(state_path, expected_rom_path=rom_path)

            self.assertTrue(doc.cpu.flags.nf)
            self.assertEqual(doc.cpu.iff_level, 3)
            self.assertEqual(doc.cpu.rfp, 2)
            self.assertTrue(doc.cpu.iff_enabled)
            assert doc.cpu.control_registers is not None
            self.assertEqual(doc.cpu.control_registers.dmac[0], 0x1234)
            self.assertEqual(doc.cpu.control_registers.dmam[0], 0x56)
            self.assertEqual(doc.cpu.control_registers.intnest, 2)


class SavestateLoaderValidationTests(unittest.TestCase):
    def test_loader_rejects_mismatching_rom_hash(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            rom_path = tmp / "demo.ngc"
            _write_demo_rom(rom_path)
            machine = load_machine_state(rom_path)
            payload = build_savestate_payload(
                rom_path=rom_path,
                rom_header=machine.header,
                cpu=machine.cpu,
                writable_overlay={},
            )
            state_path = tmp / "demo_state.json"
            save_savestate(state_path, payload)

            other_rom = tmp / "other.ngc"
            _write_demo_rom(other_rom, entry_point=0x00209999)

            with self.assertRaises(ValueError) as exc_ctx:
                load_savestate(state_path, expected_rom_path=other_rom)

            self.assertIn("ROM hash mismatch", str(exc_ctx.exception))

    def test_loader_rejects_unknown_format(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            state_path = tmp / "bad.json"
            state_path.write_text(
                json.dumps(
                    {
                        "format": "some-other-format",
                        "format_version": SAVESTATE_FORMAT_VERSION,
                        "rom": {
                            "file_size": 0,
                            "sha256": "0" * 64,
                            "header_entry_point": 0,
                            "header_mode_raw": 0,
                        },
                        "cpu": {
                            "pc": 0,
                            "flags": {},
                            "registers": {},
                        },
                        "memory": {"writable_overlay": {}},
                    },
                    indent=2,
                ),
                encoding="utf-8",
            )

            with self.assertRaises(ValueError) as exc_ctx:
                load_savestate(state_path)

            self.assertIn("Unexpected savestate format", str(exc_ctx.exception))

    def test_loader_rejects_unknown_format_version(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            state_path = tmp / "bad.json"
            state_path.write_text(
                json.dumps(
                    {
                        "format": SAVESTATE_FORMAT,
                        "format_version": "9999-99-99.v999",
                        "rom": {
                            "file_size": 0,
                            "sha256": "0" * 64,
                            "header_entry_point": 0,
                            "header_mode_raw": 0,
                        },
                        "cpu": {
                            "pc": 0,
                            "flags": {},
                            "registers": {},
                        },
                        "memory": {"writable_overlay": {}},
                    },
                    indent=2,
                ),
                encoding="utf-8",
            )

            with self.assertRaises(ValueError) as exc_ctx:
                load_savestate(state_path)

            self.assertIn("Unknown savestate format_version", str(exc_ctx.exception))

    def test_loader_rejects_missing_required_field(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            state_path = tmp / "bad.json"
            state_path.write_text(
                json.dumps(
                    {
                        "format": SAVESTATE_FORMAT,
                        "format_version": SAVESTATE_FORMAT_VERSION,
                        "cpu": {
                            "pc": 0,
                            "flags": {},
                            "registers": {},
                        },
                        "memory": {"writable_overlay": {}},
                    },
                    indent=2,
                ),
                encoding="utf-8",
            )

            with self.assertRaises(ValueError) as exc_ctx:
                load_savestate(state_path)

            self.assertIn("missing required field", str(exc_ctx.exception))


class SavestateResumeTests(unittest.TestCase):
    """Cover the save -> load -> resume flow used by `run-until-exec --seed-from`."""

    def _write_demo_rom(self, path: Path, entry_point: int, body: bytes) -> None:
        data = bytearray(0x40)
        data[0x00:0x1C] = b"LICENSED BY SNK CORPORATION".ljust(0x1C, b"\x00")
        data[0x1C:0x20] = entry_point.to_bytes(4, "little")
        data[0x20:0x22] = (0x0000).to_bytes(2, "little")
        data[0x22] = 0
        data[0x23] = 0x10
        data[0x24:0x30] = b"RESUME TEST\x00"
        body_offset = entry_point - 0x00200000
        if body_offset < len(data):
            data[body_offset : body_offset + len(body)] = body
        else:
            data.extend(b"\x00" * (body_offset - len(data)))
            data.extend(body)
        path.write_bytes(bytes(data))

    def test_split_run_matches_direct_run(self) -> None:
        """Running N steps, saving, and resuming must land at the same frontier."""
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            rom_path = tmp / "demo.ngc"
            # NOP, NOP, NOP, add WA, 0x0001 via D0 C8 01 00
            # (D0 ALU-immediate, silicon-broken, stops here)
            body = b"\x00\x00\x00\xD8\xB8"
            self._write_demo_rom(rom_path, 0x00200040, body)

            direct = load_run_until(
                rom_path, target_pc=0x00200080, max_steps=10
            )

            self.assertEqual(direct.stop_reason, "stopped-on-silicon-broken")
            self.assertEqual(direct.executed_count, 3)
            self.assertEqual(direct.final_cpu.pc, 0x00200043)

            split_step1 = load_run_until(
                rom_path, target_pc=0x00200080, max_steps=2
            )
            self.assertEqual(split_step1.executed_count, 2)
            self.assertEqual(split_step1.final_cpu.pc, 0x00200042)

            machine = load_machine_state(rom_path)
            payload = build_savestate_payload(
                rom_path=rom_path,
                rom_header=machine.header,
                cpu=split_step1.final_cpu,
                writable_overlay=split_step1.final_memory,
            )
            state_path = tmp / "resume.json"
            save_savestate(state_path, payload)

            loaded = load_savestate(state_path, expected_rom_path=rom_path)

            resumed = load_run_until(
                rom_path,
                target_pc=0x00200080,
                max_steps=10,
                initial_cpu_state=loaded.cpu,
                initial_memory_bytes=loaded.writable_overlay,
            )

            self.assertEqual(resumed.stop_reason, direct.stop_reason)
            self.assertEqual(resumed.final_cpu.pc, direct.final_cpu.pc)
            self.assertEqual(
                split_step1.executed_count + resumed.executed_count,
                direct.executed_count,
            )

    def test_seed_from_state_preserves_writable_overlay(self) -> None:
        """The writable overlay captured in the savestate must be visible to the resumed run."""
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            rom_path = tmp / "demo.ngc"
            self._write_demo_rom(rom_path, 0x00200040, b"\x00\xD0\x61")
            machine = load_machine_state(rom_path)

            seeded_cpu = replace(
                machine.cpu,
                pc=0x00200040,
                regs=replace(machine.cpu.regs, xsp=0x00004100),
            )
            overlay = {0x004100: 0xAA, 0x004101: 0xBB}
            payload = build_savestate_payload(
                rom_path=rom_path,
                rom_header=machine.header,
                cpu=seeded_cpu,
                writable_overlay=overlay,
            )
            state_path = tmp / "resume.json"
            save_savestate(state_path, payload)

            loaded = load_savestate(state_path, expected_rom_path=rom_path)

            resumed = load_run_until(
                rom_path,
                target_pc=0x00200080,
                max_steps=5,
                initial_cpu_state=loaded.cpu,
                initial_memory_bytes=loaded.writable_overlay,
            )

            self.assertEqual(resumed.final_memory.get(0x004100), 0xAA)
            self.assertEqual(resumed.final_memory.get(0x004101), 0xBB)


if __name__ == "__main__":
    unittest.main()
