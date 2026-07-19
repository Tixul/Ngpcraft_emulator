"""Tests for the oracle_tools suite (trace diff logic).

Pure Python and no compiler needed, so these run inside the normal suite and
keep the tools from rotting.

Scope note (2026-07-19): the extractors and co-simulation drivers this file
used to cover were retired, because they read facts out of -- or linked
against -- third-party emulator source. Every fact they produced is already in
the Toshiba TLCS-900/H documentation under `doc t_900` and in
NGPC_HW_QUICKREF.md, so they bought a second opinion we did not need. Primary
sources only. What remains covers the trace-diff logic, which is entirely ours.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

_ORACLE = Path(__file__).resolve().parent.parent / "oracle_tools"
sys.path.insert(0, str(_ORACLE))

import trace_diff  # noqa: E402


class TraceDiffTest(unittest.TestCase):
    def _rec(self, pc, regs=None, f=None):
        return trace_diff._norm_record(pc, regs or {}, f)

    def test_identical_traces_no_divergence(self) -> None:
        a = [self._rec(0x100, {"wa": 1}), self._rec(0x102, {"wa": 2})]
        res = trace_diff.diff(a, list(a))
        self.assertIsNone(res["first"])
        self.assertEqual(res["diverging_steps"], 0)

    def test_register_divergence_detected(self) -> None:
        ref = [self._rec(0x100, {"wa": 1}), self._rec(0x102, {"bc": 5})]
        ours = [self._rec(0x100, {"wa": 1}), self._rec(0x102, {"bc": 9})]
        res = trace_diff.diff(ref, ours)
        self.assertIsNotNone(res["first"])
        self.assertEqual(res["first"]["index"], 1)
        self.assertIn("bc", res["first"]["fields"])

    def test_pc_divergence_stops_early(self) -> None:
        ref = [self._rec(0x100), self._rec(0x102), self._rec(0x104)]
        ours = [self._rec(0x100), self._rec(0x200), self._rec(0x999)]
        res = trace_diff.diff(ref, ours)
        self.assertEqual(res["first"]["index"], 1)
        self.assertIn("pc", res["first"]["fields"])

    def test_unknown_register_is_skipped(self) -> None:
        # our side reports None (unknown) -> must NOT count as a mismatch
        ref = [self._rec(0x100, {"wa": 0x1234})]
        ours = [self._rec(0x100, {"wa": None})]
        res = trace_diff.diff(ref, ours)
        self.assertIsNone(res["first"])

    def test_flag_divergence_detected(self) -> None:
        # the real COLUMNS finding shape: same regs, F differs (Z flag)
        ref = [self._rec(0x204FD0, {}, f=0x42)]
        ours = [self._rec(0x204FD0, {}, f=0x02)]
        res = trace_diff.diff(ref, ours)
        self.assertIn("f", res["first"]["fields"])

    def test_flag_derived_from_sr_in_json(self) -> None:
        import json
        import tempfile

        line = json.dumps({"i": 0, "pc": 0x100, "wa": 0, "sr": 0xF842})
        with tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False) as fh:
            fh.write(line + "\n")
            path = Path(fh.name)
        try:
            rec = trace_diff.parse_trace(path)[0]
            self.assertEqual(rec["f"], 0x42)  # low byte of SR
        finally:
            path.unlink(missing_ok=True)


if __name__ == "__main__":
    unittest.main()
