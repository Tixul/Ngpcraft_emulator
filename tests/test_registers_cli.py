"""CLI tests for `registers` (rich CPU register view)."""

from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from dataclasses import replace
from pathlib import Path

from core.cpu import StatusFlags, create_unknown_control_registers
from core.machine import load_machine_state
from core.savestate import build_savestate_payload, save_savestate
from ngpc_emu import main


def _write_demo_rom(path: Path, entry_point: int = 0x00200040) -> None:
    data = bytearray(0x40)
    data[0x00:0x1C] = b"LICENSED BY SNK CORPORATION".ljust(0x1C, b"\x00")
    data[0x1C:0x20] = entry_point.to_bytes(4, "little")
    data[0x22] = 0
    data[0x23] = 0x10
    data[0x24:0x30] = b"REGS TEST\x00\x00\x00"
    path.write_bytes(bytes(data))


class RegistersHumanOutputTests(unittest.TestCase):
    def test_bootstrap_shows_pc_and_unknown_for_the_rest(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            _write_demo_rom(rom_path)

            stdout = io.StringIO()
            with redirect_stdout(stdout):
                exit_code = main(["registers", str(rom_path)])
            self.assertEqual(exit_code, 0)
            out = stdout.getvalue()
            self.assertIn("PC : 0x00200040", out)
            self.assertIn("SR : <unknown>", out)
            self.assertIn("IFF: <unknown>", out)
            self.assertIn("RFP: <unknown>", out)
            self.assertIn("XWA", out)
            self.assertIn("XSP", out)
            self.assertIn("DMAS0", out)
            self.assertIn("INTNEST", out)
            # Flag row prints '?' for every unknown flag.
            self.assertIn("S=? Z=? V=? H=? C=? N=?", out)


class RegistersJsonOutputTests(unittest.TestCase):
    def test_json_payload_shape_for_bootstrap(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            _write_demo_rom(rom_path)

            stdout = io.StringIO()
            with redirect_stdout(stdout):
                exit_code = main(["registers", str(rom_path), "--json"])
            self.assertEqual(exit_code, 0)
            payload = json.loads(stdout.getvalue())
            self.assertEqual(payload["pc_hex"], "0x00200040")
            self.assertIsNone(payload["sr_raw"])
            self.assertIsNone(payload["iff_level"])
            self.assertIsNone(payload["rfp"])
            self.assertIn("alt_flags", payload)
            self.assertIn("control_registers", payload)
            self.assertIsNone(payload["alt_flags"]["S"])
            self.assertEqual(len(payload["registers"]), 8)
            self.assertEqual(len(payload["control_registers"]), 17)
            xwa = next(r for r in payload["registers"] if r["long_name"] == "XWA")
            self.assertEqual(xwa["word_low_name"], "WA")
            self.assertEqual(xwa["byte_high_name"], "W")
            self.assertEqual(xwa["byte_low_name"], "A")
            self.assertIsNone(xwa["long"])
            dmac0 = next(r for r in payload["control_registers"] if r["name"] == "DMAC0")
            self.assertEqual(dmac0["size"], "word")
            self.assertIsNone(dmac0["value"])
            # XSP/XIX/XIY/XIZ have no R8 sub-register decomposition.
            xsp = next(r for r in payload["registers"] if r["long_name"] == "XSP")
            self.assertEqual(xsp["word_low_name"], "SP")
            self.assertIsNone(xsp["byte_high_name"])
            self.assertIsNone(xsp["byte_low_name"])


class RegistersSeedFromTests(unittest.TestCase):
    def test_seed_from_savestate_loads_modeled_fields(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            rom_path = tmp / "demo.ngc"
            _write_demo_rom(rom_path)
            machine = load_machine_state(rom_path)
            seeded_cpu = replace(
                machine.cpu,
                pc=0x0020D180,
                regs=replace(
                    machine.cpu.regs,
                    xwa=0x11223344,
                    xbc=0xDEADBEEF,
                    xsp=0x00006C00,
                ),
                flags=StatusFlags(
                    sf=True, zf=False, vf=False, hf=True, cf=True, nf=False,
                ),
                alt_flags=StatusFlags(
                    sf=False, zf=True, vf=True, hf=False, cf=False, nf=True,
                ),
                iff_enabled=True,
                iff_level=3,
                rfp=2,
                control_registers=replace(
                    create_unknown_control_registers(),
                    dmas=(0x00201234, None, None, None),
                    dmac=(0x1234, None, None, None),
                    dmam=(0x56, None, None, None),
                    intnest=2,
                ),
            )
            state_path = tmp / "demo_state.json"
            save_savestate(
                state_path,
                build_savestate_payload(
                    rom_path=rom_path,
                    rom_header=machine.header,
                    cpu=seeded_cpu,
                    writable_overlay={},
                ),
            )

            stdout = io.StringIO()
            with redirect_stdout(stdout):
                exit_code = main(
                    [
                        "registers", str(rom_path),
                        "--seed-from", str(state_path),
                        "--json",
                    ]
                )
            self.assertEqual(exit_code, 0)
            payload = json.loads(stdout.getvalue())
            self.assertEqual(payload["pc_hex"], "0x0020D180")
            self.assertEqual(payload["iff_level"], 3)
            self.assertEqual(payload["rfp"], 2)
            self.assertEqual(payload["flags"]["S"], True)
            self.assertEqual(payload["flags"]["H"], True)
            self.assertEqual(payload["flags"]["C"], True)
            self.assertEqual(payload["flags"]["N"], False)
            self.assertEqual(payload["alt_flags"]["S"], False)
            self.assertEqual(payload["alt_flags"]["Z"], True)
            self.assertEqual(payload["alt_flags"]["N"], True)
            xwa = next(r for r in payload["registers"] if r["long_name"] == "XWA")
            self.assertEqual(xwa["long"], 0x11223344)
            self.assertEqual(xwa["long_hex"], "0x11223344")
            self.assertEqual(xwa["word_low_hex"], "0x3344")
            self.assertEqual(xwa["byte_high_hex"], "0x33")
            self.assertEqual(xwa["byte_low_hex"], "0x44")
            xbc = next(r for r in payload["registers"] if r["long_name"] == "XBC")
            self.assertEqual(xbc["long_hex"], "0xDEADBEEF")
            dmas0 = next(r for r in payload["control_registers"] if r["name"] == "DMAS0")
            self.assertEqual(dmas0["value_hex"], "0x00201234")
            dmac0 = next(r for r in payload["control_registers"] if r["name"] == "DMAC0")
            self.assertEqual(dmac0["value_hex"], "0x1234")
            dmam0 = next(r for r in payload["control_registers"] if r["name"] == "DMAM0")
            self.assertEqual(dmam0["value_hex"], "0x56")
            intnest = next(r for r in payload["control_registers"] if r["name"] == "INTNEST")
            self.assertEqual(intnest["value"], 2)


class CpuInfoJsonOutputTests(unittest.TestCase):
    def test_cpu_info_json_exposes_control_registers(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            _write_demo_rom(rom_path)

            stdout = io.StringIO()
            with redirect_stdout(stdout):
                exit_code = main(["cpu-info", str(rom_path), "--json"])
            self.assertEqual(exit_code, 0)
            payload = json.loads(stdout.getvalue())
            self.assertIn("control_registers", payload)
            self.assertEqual(len(payload["control_registers"]), 17)
            intnest = next(r for r in payload["control_registers"] if r["name"] == "INTNEST")
            self.assertEqual(intnest["size"], "word")
            self.assertIsNone(intnest["value"])


if __name__ == "__main__":
    unittest.main()
