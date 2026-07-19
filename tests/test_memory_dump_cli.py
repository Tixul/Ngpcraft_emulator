"""CLI tests for `memory-dump` (hexdump-style inspector)."""

from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from core.machine import load_machine_state
from core.savestate import build_savestate_payload, save_savestate
from ngpc_emu import main


def _write_demo_rom(path: Path, entry_point: int = 0x00200040) -> None:
    data = bytearray(0x40)
    data[0x00:0x1C] = b"LICENSED BY SNK CORPORATION".ljust(0x1C, b"\x00")
    data[0x1C:0x20] = entry_point.to_bytes(4, "little")
    data[0x22] = 0
    data[0x23] = 0x10
    data[0x24:0x30] = b"DUMP TEST\x00\x00\x00"
    path.write_bytes(bytes(data))


class MemoryDumpHumanOutputTests(unittest.TestCase):
    def test_dump_work_ram_cold_start_shows_zero_rows(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            _write_demo_rom(rom_path)

            stdout = io.StringIO()
            with redirect_stdout(stdout):
                exit_code = main(
                    [
                        "memory-dump", str(rom_path), "0x004000",
                        "--count", "16",
                    ]
                )
            self.assertEqual(exit_code, 0)
            out = stdout.getvalue()
            self.assertIn("0x004000", out)
            self.assertIn("00 00 00 00 00 00 00 00", out)
            self.assertIn("|................|", out)

    def test_dump_rom_header_shows_ascii_title(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            _write_demo_rom(rom_path)

            stdout = io.StringIO()
            with redirect_stdout(stdout):
                exit_code = main(
                    [
                        "memory-dump", str(rom_path), "0x200000",
                        "--count", "32",
                    ]
                )
            self.assertEqual(exit_code, 0)
            out = stdout.getvalue()
            self.assertIn("LICENSED BY SNK", out)


class MemoryDumpJsonOutputTests(unittest.TestCase):
    def test_json_emits_one_entry_per_byte_with_address(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            _write_demo_rom(rom_path)

            stdout = io.StringIO()
            with redirect_stdout(stdout):
                exit_code = main(
                    [
                        "memory-dump", str(rom_path), "0x004000",
                        "--count", "4", "--json",
                    ]
                )
            self.assertEqual(exit_code, 0)
            payload = json.loads(stdout.getvalue())
            self.assertEqual(payload["count"], 4)
            self.assertEqual(payload["address"], 0x4000)
            self.assertEqual(len(payload["bytes"]), 4)
            self.assertEqual(payload["bytes"][0]["address_hex"], "0x004000")
            self.assertEqual(payload["bytes"][0]["value"], 0)
            self.assertEqual(payload["bytes"][3]["address_hex"], "0x004003")


class MemoryDumpSeedFromTests(unittest.TestCase):
    def test_seed_from_savestate_overlays_writable_cells(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            rom_path = tmp / "demo.ngc"
            _write_demo_rom(rom_path)
            machine = load_machine_state(rom_path)

            # Build a savestate whose overlay writes 0xAB / 0xCD to RAM.
            overlay = {
                0x004000: 0xAB,
                0x004001: 0xCD,
                0x004003: 0x42,
            }
            state_path = tmp / "demo_state.json"
            save_savestate(
                state_path,
                build_savestate_payload(
                    rom_path=rom_path,
                    rom_header=machine.header,
                    cpu=machine.cpu,
                    writable_overlay=overlay,
                ),
            )

            stdout = io.StringIO()
            with redirect_stdout(stdout):
                exit_code = main(
                    [
                        "memory-dump", str(rom_path), "0x004000",
                        "--count", "4",
                        "--seed-from", str(state_path),
                        "--json",
                    ]
                )
            self.assertEqual(exit_code, 0)
            payload = json.loads(stdout.getvalue())
            self.assertEqual(payload["seed_from"], str(state_path))
            byte_values = [b["value"] for b in payload["bytes"]]
            # Overlay shadows the cold-start zeros at the three written slots;
            # the unwritten slot (0x004002) stays at 0x00.
            self.assertEqual(byte_values, [0xAB, 0xCD, 0x00, 0x42])


class MemoryDumpRowWidthTests(unittest.TestCase):
    def test_width_8_yields_smaller_rows(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            _write_demo_rom(rom_path)

            stdout = io.StringIO()
            with redirect_stdout(stdout):
                exit_code = main(
                    [
                        "memory-dump", str(rom_path), "0x004000",
                        "--count", "16", "--width", "8",
                    ]
                )
            self.assertEqual(exit_code, 0)
            out = stdout.getvalue()
            # With width 8 there should be at least two distinct row prefixes.
            self.assertIn("0x004000", out)
            self.assertIn("0x004008", out)


if __name__ == "__main__":
    unittest.main()
