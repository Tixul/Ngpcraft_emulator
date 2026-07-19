"""Tests for per-symbol bucketing of event-log payloads."""

from __future__ import annotations

import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from core.event_log import EVENT_LOG_FORMAT_VERSION
from core.event_log_profile import (
    EVENT_LOG_PROFILE_FORMAT,
    EVENT_LOG_PROFILE_VERSION,
    bucket_event_log_by_symbol,
)
from core.symbols import SymbolTable, Symbol, load_map
from ngpc_emu import main


def _build_symbol_table(symbols: list[tuple[str, int, str]]) -> SymbolTable:
    table = SymbolTable(source_path="<synthetic>")
    for name, addr, section in symbols:
        sym = Symbol(name=name, address=addr, section=section)
        table._by_name[name] = sym
        table._at_address.setdefault(addr, []).append(sym)
        if section not in table.sections:
            table.sections.append(section)
    table._sorted_addresses = sorted(table._at_address.keys())
    return table


def _make_event(index: int, pc: int, status: str = "executed") -> dict:
    return {
        "index": index,
        "event_type": "instruction-step",
        "pc": pc,
        "pc_hex": f"0x{pc:08X}",
        "raw_bytes_hex": "00",
        "assembly": "nop",
        "length": 1,
        "status": status,
        "next_pc": pc + 1,
        "next_pc_hex": f"0x{pc + 1:08X}",
        "written_registers": [],
        "memory_writes": [],
    }


def _make_event_log(events: list[dict], final_pc: int) -> dict:
    return {
        "format": "ngpc-emu-event-log",
        "format_version": EVENT_LOG_FORMAT_VERSION,
        "rom": {"sha256": "deadbeef" * 8, "path_when_saved": "x.ngc"},
        "events": events,
        "summary": {
            "executed_count": sum(1 for e in events if e["status"] == "executed"),
            "emitted_count": len(events),
            "stop_reason": "step-budget-exhausted",
            "final_cpu_pc": final_pc,
            "final_cpu_pc_hex": f"0x{final_pc:08X}",
            "matched_quirk_on_stop": None,
        },
    }


class EventLogProfileTests(unittest.TestCase):
    def setUp(self) -> None:
        self.table = _build_symbol_table([
            ("__startup", 0x00200040, "Public symbols"),
            ("_ngpc_mul32", 0x0020D0CB, "Public symbols"),
            ("_main", 0x00200200, "Public symbols"),
        ])

    def test_format_markers_present(self) -> None:
        log = _make_event_log([], final_pc=0)
        out = bucket_event_log_by_symbol(log, self.table)
        self.assertEqual(out["format"], EVENT_LOG_PROFILE_FORMAT)
        self.assertEqual(out["format_version"], EVENT_LOG_PROFILE_VERSION)

    def test_empty_event_list_produces_empty_bucket_list(self) -> None:
        log = _make_event_log([], final_pc=0)
        out = bucket_event_log_by_symbol(log, self.table)
        self.assertEqual(out["total_events"], 0)
        self.assertEqual(out["resolved_events"], 0)
        self.assertEqual(out["unresolved_events"], 0)
        self.assertEqual(out["distinct_symbols"], 0)
        self.assertEqual(out["buckets"], [])

    def test_events_in_one_symbol_collapse_into_one_bucket(self) -> None:
        events = [
            _make_event(0, 0x00200040),
            _make_event(1, 0x00200041),
            _make_event(2, 0x00200042),
        ]
        log = _make_event_log(events, final_pc=0x00200042)
        out = bucket_event_log_by_symbol(log, self.table)
        self.assertEqual(out["distinct_symbols"], 1)
        self.assertEqual(len(out["buckets"]), 1)
        bucket = out["buckets"][0]
        self.assertEqual(bucket["symbol"], "__startup")
        self.assertEqual(bucket["total_events"], 3)
        self.assertEqual(bucket["executed_events"], 3)
        self.assertEqual(bucket["halted_events"], 0)
        self.assertEqual(bucket["first_pc"], 0x00200040)
        self.assertEqual(bucket["last_pc"], 0x00200042)
        self.assertEqual(bucket["min_offset"], 0)
        self.assertEqual(bucket["max_offset"], 2)

    def test_buckets_sorted_by_descending_total(self) -> None:
        events = (
            [_make_event(i, 0x00200200) for i in range(5)]  # _main x 5
            + [_make_event(5, 0x00200040), _make_event(6, 0x00200041)]  # __startup x 2
        )
        log = _make_event_log(events, final_pc=0x00200041)
        out = bucket_event_log_by_symbol(log, self.table)
        self.assertEqual([b["symbol"] for b in out["buckets"]], ["_main", "__startup"])

    def test_halted_events_distinguished_from_executed(self) -> None:
        events = [
            _make_event(0, 0x0020D0CB, status="executed"),
            _make_event(1, 0x0020D0CD, status="silicon-broken"),
            _make_event(2, 0x0020D180, status="requires-known-full-register"),
        ]
        log = _make_event_log(events, final_pc=0x0020D180)
        out = bucket_event_log_by_symbol(log, self.table)
        self.assertEqual(out["distinct_symbols"], 1)
        bucket = out["buckets"][0]
        self.assertEqual(bucket["symbol"], "_ngpc_mul32")
        self.assertEqual(bucket["executed_events"], 1)
        self.assertEqual(bucket["halted_events"], 2)
        # halted breakdown carries the actual statuses
        self.assertEqual(
            out["halted_status_breakdown"]["silicon-broken"], 1
        )
        self.assertEqual(
            out["halted_status_breakdown"]["requires-known-full-register"], 1
        )

    def test_events_below_lowest_symbol_count_as_unresolved(self) -> None:
        events = [_make_event(0, 0x00100000)]  # below __startup
        log = _make_event_log(events, final_pc=0x00100000)
        out = bucket_event_log_by_symbol(log, self.table)
        self.assertEqual(out["unresolved_events"], 1)
        self.assertEqual(out["resolved_events"], 0)

    def test_events_missing_pc_are_silently_skipped(self) -> None:
        events = [{"index": 0, "status": "executed"}, _make_event(1, 0x00200040)]
        log = _make_event_log(events, final_pc=0x00200040)
        out = bucket_event_log_by_symbol(log, self.table)
        self.assertEqual(out["total_events"], 2)
        self.assertEqual(out["resolved_events"], 1)
        # The malformed event is not counted as unresolved either — it has
        # no PC, it is simply not analyzable.
        self.assertEqual(out["unresolved_events"], 0)

    def test_min_max_offsets_track_observed_range(self) -> None:
        events = [
            _make_event(0, 0x00200040 + 5),
            _make_event(1, 0x00200040 + 2),
            _make_event(2, 0x00200040 + 9),
        ]
        log = _make_event_log(events, final_pc=0x00200049)
        out = bucket_event_log_by_symbol(log, self.table)
        bucket = out["buckets"][0]
        self.assertEqual(bucket["min_offset"], 2)
        self.assertEqual(bucket["max_offset"], 9)
        # last_pc tracks the LAST event seen, not the highest offset
        self.assertEqual(bucket["last_pc"], 0x00200049)

    def test_rom_sha256_is_forwarded_from_event_log(self) -> None:
        log = _make_event_log([_make_event(0, 0x00200040)], final_pc=0x00200040)
        out = bucket_event_log_by_symbol(log, self.table)
        self.assertEqual(out["rom_sha256"], "deadbeef" * 8)


_MAP_BODY = """\
# t900ld synthetic map

=== Public symbols ===
  __startup                0x00200040
  _ngpc_mul32              0x0020D0CB
  _main                    0x00200200
"""

_EVENT_LOG_BODY = json.dumps({
    "format": "ngpc-emu-event-log",
    "format_version": EVENT_LOG_FORMAT_VERSION,
    "created_at_utc": "2026-05-19T22:00:00+00:00",
    "emulator": {"project": "NgpCraft_emulator", "prototype": "python", "commit": None},
    "rom": {
        "sha256": "ab" * 32,
        "path_when_saved": "x.ngc",
        "file_size": 64,
        "header_title": "TEST",
        "header_entry_point": 0x00200040,
        "header_mode_raw": 0x10,
    },
    "quirks": {"database_version": "test"},
    "run_context": {
        "start_pc": 0x00200040,
        "start_pc_hex": "0x00200040",
        "target_pc": None,
        "target_pc_hex": None,
        "max_steps": 8,
        "seed_registers": {},
        "seed_xsp": None,
        "seed_from_savestate": None,
    },
    "events": [
        {
            "index": 0, "event_type": "instruction-step",
            "pc": 0x00200040, "pc_hex": "0x00200040",
            "raw_bytes_hex": "00", "assembly": "nop", "length": 1,
            "status": "executed",
            "next_pc": 0x00200041, "next_pc_hex": "0x00200041",
            "written_registers": [], "memory_writes": [],
        },
        {
            "index": 1, "event_type": "instruction-step",
            "pc": 0x0020D0CB, "pc_hex": "0x0020D0CB",
            "raw_bytes_hex": "D8 89", "assembly": "ld XBC, XWA", "length": 2,
            "status": "silicon-broken",
            "next_pc": None, "next_pc_hex": None,
            "written_registers": [], "memory_writes": [],
        },
    ],
    "summary": {
        "executed_count": 1,
        "emitted_count": 2,
        "stop_reason": "stopped-on-silicon-broken",
        "final_cpu_pc": 0x0020D0CB,
        "final_cpu_pc_hex": "0x0020D0CB",
        "matched_quirk_on_stop": None,
    },
    "note": None,
})


class EventLogProfileCLITests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.map_path = Path(self.tmp.name) / "demo.map"
        self.log_path = Path(self.tmp.name) / "demo.event.json"
        self.map_path.write_text(_MAP_BODY, encoding="utf-8")
        self.log_path.write_text(_EVENT_LOG_BODY, encoding="utf-8")

    def _run(self, argv: list[str]) -> dict:
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = main(argv)
        self.assertEqual(rc, 0)
        return json.loads(buf.getvalue())

    def test_cli_profile_basic_json(self) -> None:
        out = self._run([
            "eventlog", "profile", str(self.log_path),
            "--map", str(self.map_path),
            "--json",
        ])
        self.assertEqual(out["distinct_symbols"], 2)
        names = {b["symbol"] for b in out["buckets"]}
        self.assertSetEqual(names, {"__startup", "_ngpc_mul32"})

    def test_cli_profile_requires_map_argument(self) -> None:
        # argparse exits with SystemExit when a required argument is missing
        with self.assertRaises(SystemExit):
            main(["eventlog", "profile", str(self.log_path), "--json"])

    def test_cli_profile_top_limit_truncates_buckets(self) -> None:
        out = self._run([
            "eventlog", "profile", str(self.log_path),
            "--map", str(self.map_path),
            "--top", "1",
            "--json",
        ])
        self.assertEqual(len(out["buckets"]), 1)
        # distinct_symbols counts ALL hit symbols, not just top
        self.assertEqual(out["distinct_symbols"], 2)


if __name__ == "__main__":
    unittest.main()
