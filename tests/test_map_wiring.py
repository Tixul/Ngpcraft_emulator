"""Verify --map wiring in execution commands.

The 4 execution commands (step-exec, run-steps not applicable since it has
no --map yet, trace-exec, run-until-exec, eventlog capture) accept an
optional --map argument. When set, the JSON result must include a
'final_symbol' block reporting the symbol that owns the final PC.

These tests build a minimal ROM that the emulator can step through honestly
for a few instructions, write a tiny .map file that covers the bootstrap
address range, then invoke each CLI command and check both the WITH-map
and WITHOUT-map output paths.

The on-disk shape of the event-log file is not part of this contract: the
final_symbol enrichment for eventlog-capture lives in the CLI summary only.
"""

from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from ngpc_emu import main


_MAP_BODY = """\
# t900ld.py map file
# inputs: synthetic

=== Linker symbols ===
  _Bss_START               0x00004000

=== Public symbols ===
  __startup                0x00200040
  _isr_dummy               0x002000A0
  _main                    0x00200200
"""


def _write_demo_rom(path: Path, entry_point: int, body: bytes) -> None:
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


def _run_cli(argv: list[str]) -> dict:
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = main(argv)
    assert rc in (0, 1), f"unexpected rc {rc}"
    return json.loads(buf.getvalue())


class MapWiringTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        self.rom_path = Path(self.tmpdir.name) / "demo.ngc"
        self.map_path = Path(self.tmpdir.name) / "demo.map"
        # Body: NOP, NOP, NOP — three honest instructions starting at entry.
        _write_demo_rom(self.rom_path, 0x00200040, b"\x00\x00\x00")
        self.map_path.write_text(_MAP_BODY, encoding="utf-8")

    # --- run-until-exec ----------------------------------------------------

    def test_run_until_exec_without_map_omits_final_symbol(self) -> None:
        payload = _run_cli(
            [
                "run-until-exec",
                str(self.rom_path),
                "0x00200080",
                "--max-steps",
                "8",
                "--json",
            ]
        )
        self.assertNotIn("final_symbol", payload)

    def test_run_until_exec_with_map_reports_owning_symbol(self) -> None:
        payload = _run_cli(
            [
                "run-until-exec",
                str(self.rom_path),
                "0x00200080",
                "--max-steps",
                "8",
                "--map",
                str(self.map_path),
                "--json",
            ]
        )
        self.assertIn("final_symbol", payload)
        fs = payload["final_symbol"]
        self.assertTrue(fs["found"])
        self.assertEqual(fs["owning_symbol"], "__startup")
        # Final PC must be inside the __startup span (i.e. >= 0x00200040,
        # < _isr_dummy at 0x002000A0). NOPs advance PC by 1 each.
        self.assertGreaterEqual(fs["offset_from_symbol"], 0)
        self.assertLess(fs["offset_from_symbol"], 0x60)
        self.assertEqual(fs["section"], "Public symbols")

    # --- run-steps ---------------------------------------------------------

    def test_run_steps_with_map_reports_owning_symbol(self) -> None:
        payload = _run_cli(
            [
                "run-steps",
                str(self.rom_path),
                "--count",
                "2",
                "--map",
                str(self.map_path),
                "--json",
            ]
        )
        self.assertIn("final_symbol", payload)
        self.assertEqual(payload["final_symbol"]["owning_symbol"], "__startup")

    def test_run_steps_without_map_omits_final_symbol(self) -> None:
        payload = _run_cli(
            ["run-steps", str(self.rom_path), "--count", "2", "--json"]
        )
        self.assertNotIn("final_symbol", payload)

    # --- step-exec ---------------------------------------------------------

    def test_step_exec_with_map_reports_owning_symbol(self) -> None:
        payload = _run_cli(
            [
                "step-exec",
                str(self.rom_path),
                "--map",
                str(self.map_path),
                "--json",
            ]
        )
        self.assertIn("final_symbol", payload)
        self.assertEqual(payload["final_symbol"]["owning_symbol"], "__startup")

    def test_step_exec_without_map_omits_final_symbol(self) -> None:
        payload = _run_cli(
            ["step-exec", str(self.rom_path), "--json"]
        )
        self.assertNotIn("final_symbol", payload)

    # --- trace-exec --------------------------------------------------------

    def test_trace_exec_with_map_reports_owning_symbol(self) -> None:
        payload = _run_cli(
            [
                "trace-exec",
                str(self.rom_path),
                "--count",
                "2",
                "--map",
                str(self.map_path),
                "--json",
            ]
        )
        self.assertIn("final_symbol", payload)
        self.assertEqual(payload["final_symbol"]["owning_symbol"], "__startup")

    def test_trace_exec_without_map_omits_final_symbol(self) -> None:
        payload = _run_cli(
            ["trace-exec", str(self.rom_path), "--count", "2", "--json"]
        )
        self.assertNotIn("final_symbol", payload)

    # --- eventlog capture --------------------------------------------------

    def test_eventlog_capture_with_map_reports_owning_symbol(self) -> None:
        out_path = Path(self.tmpdir.name) / "ev.json"
        payload = _run_cli(
            [
                "eventlog",
                "capture",
                str(self.rom_path),
                str(out_path),
                "--count",
                "2",
                "--map",
                str(self.map_path),
                "--json",
            ]
        )
        self.assertIn("final_symbol", payload)
        self.assertEqual(payload["final_symbol"]["owning_symbol"], "__startup")
        # The captured event-log file on disk must NOT contain final_symbol —
        # that enrichment lives only in the CLI summary.
        ev = json.loads(out_path.read_text(encoding="utf-8"))
        self.assertNotIn("final_symbol", ev)

    def test_eventlog_capture_without_map_omits_final_symbol(self) -> None:
        out_path = Path(self.tmpdir.name) / "ev.json"
        payload = _run_cli(
            [
                "eventlog",
                "capture",
                str(self.rom_path),
                str(out_path),
                "--count",
                "2",
                "--json",
            ]
        )
        self.assertNotIn("final_symbol", payload)

    # --- map file missing -------------------------------------------------

    def test_map_missing_file_raises_file_not_found(self) -> None:
        # main() catches FileNotFoundError and returns 1 with stderr msg
        rc = main(
            [
                "run-until-exec",
                str(self.rom_path),
                "0x00200080",
                "--max-steps",
                "8",
                "--map",
                "/nonexistent/path.map",
                "--json",
            ]
        )
        self.assertEqual(rc, 1)


if __name__ == "__main__":
    unittest.main()
