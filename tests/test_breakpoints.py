"""Breakpoint registry and event-log PC-match tests."""

from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from core.breakpoints import (
    BREAKPOINTS_FORMAT_VERSION,
    Breakpoint,
    add_breakpoint,
    breakpoints_path_for_rom,
    clear_breakpoints,
    load_breakpoints,
    match_event_log_pc,
    remove_breakpoint,
)
from ngpc_emu import main


def _write_demo_rom(path: Path, entry_point: int = 0x00200040) -> None:
    data = bytearray(0x40)
    data[0x00:0x1C] = b"LICENSED BY SNK CORPORATION".ljust(0x1C, b"\x00")
    data[0x1C:0x20] = entry_point.to_bytes(4, "little")
    data[0x22] = 0
    data[0x23] = 0x10
    data[0x24:0x30] = b"BP TEST\x00\x00\x00\x00\x00"
    path.write_bytes(bytes(data))


class BreakpointRegistryTests(unittest.TestCase):
    def test_empty_registry_returns_empty_tuple(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            _write_demo_rom(rom_path)
            self.assertEqual(load_breakpoints(rom_path), ())

    def test_add_assigns_increasing_ids_and_persists(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            _write_demo_rom(rom_path)

            bp1 = add_breakpoint(rom_path, address=0x0020D180, label="frontier")
            bp2 = add_breakpoint(rom_path, address=0x00200200, label="_main")

            self.assertEqual(bp1.id, 1)
            self.assertEqual(bp2.id, 2)
            self.assertEqual(bp1.address, 0x0020D180)

            reloaded = load_breakpoints(rom_path)
            self.assertEqual(len(reloaded), 2)
            self.assertEqual(reloaded[0].label, "frontier")
            self.assertEqual(reloaded[1].label, "_main")

    def test_duplicate_address_is_allowed_with_distinct_ids(self) -> None:
        """Two breakpoints on the same PC stay independent rows."""
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            _write_demo_rom(rom_path)

            bp1 = add_breakpoint(rom_path, address=0x200040, label="boot")
            bp2 = add_breakpoint(rom_path, address=0x200040, label="alt-label")

            self.assertNotEqual(bp1.id, bp2.id)
            reloaded = load_breakpoints(rom_path)
            self.assertEqual(len(reloaded), 2)

    def test_add_rejects_negative_or_out_of_range_address(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            _write_demo_rom(rom_path)

            with self.assertRaises(ValueError):
                add_breakpoint(rom_path, address=-1)
            with self.assertRaises(ValueError):
                add_breakpoint(rom_path, address=0x01000000)

    def test_remove_drops_only_targeted_id(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            _write_demo_rom(rom_path)

            bp1 = add_breakpoint(rom_path, address=0x200040)
            add_breakpoint(rom_path, address=0x200080)

            dropped = remove_breakpoint(rom_path, bp1.id)
            self.assertEqual(dropped.address, 0x200040)
            self.assertEqual(len(load_breakpoints(rom_path)), 1)

    def test_remove_unknown_id_raises_key_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            _write_demo_rom(rom_path)
            add_breakpoint(rom_path, address=0x200040)
            with self.assertRaises(KeyError):
                remove_breakpoint(rom_path, 999)

    def test_clear_drops_all_and_returns_count(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            _write_demo_rom(rom_path)
            add_breakpoint(rom_path, address=0x200040)
            add_breakpoint(rom_path, address=0x200080)

            dropped = clear_breakpoints(rom_path)
            self.assertEqual(dropped, 2)
            self.assertEqual(load_breakpoints(rom_path), ())

    def test_loader_rejects_unknown_format_version(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            _write_demo_rom(rom_path)
            path = breakpoints_path_for_rom(rom_path)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                json.dumps(
                    {
                        "format": "ngpc-emu-breakpoints",
                        "format_version": "9999-99-99.v999",
                        "breakpoints": [],
                    }
                ),
                encoding="utf-8",
            )
            with self.assertRaises(ValueError) as ctx:
                load_breakpoints(rom_path)
            self.assertIn("Unknown breakpoint format_version", str(ctx.exception))


class BreakpointMatchTests(unittest.TestCase):
    def _fake_event_log(self) -> dict[str, object]:
        return {
            "events": [
                {"index": 0, "pc": 0x00200040, "assembly": "nop", "status": "executed"},
                {"index": 1, "pc": 0x00200041, "assembly": "ld WA, 0", "status": "executed"},
                {"index": 2, "pc": 0x00200044, "assembly": "nop", "status": "executed"},
                {"index": 3, "pc": 0x0020D180, "assembly": "ld XBC, XWA", "status": "silicon-broken"},
            ]
        }

    def test_match_fires_on_exact_pc(self) -> None:
        bps = (Breakpoint(id=1, address=0x0020D180, label="frontier"),)
        hits = match_event_log_pc(bps, self._fake_event_log())

        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0].event_index, 3)
        self.assertEqual(hits[0].event_pc, 0x0020D180)
        self.assertEqual(hits[0].status, "silicon-broken")
        self.assertEqual(hits[0].breakpoint.label, "frontier")

    def test_match_misses_when_no_pc_overlap(self) -> None:
        bps = (Breakpoint(id=1, address=0x00500000),)
        hits = match_event_log_pc(bps, self._fake_event_log())
        self.assertEqual(hits, ())

    def test_match_emits_one_hit_per_breakpoint_on_same_address(self) -> None:
        """If two breakpoints share an address, both fire."""
        bps = (
            Breakpoint(id=1, address=0x00200040, label="A"),
            Breakpoint(id=2, address=0x00200040, label="B"),
        )
        hits = match_event_log_pc(bps, self._fake_event_log())

        self.assertEqual(len(hits), 2)
        labels = {h.breakpoint.label for h in hits}
        self.assertEqual(labels, {"A", "B"})


class BreakpointCliTests(unittest.TestCase):
    def test_cli_add_list_remove_roundtrip(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            _write_demo_rom(rom_path)

            stdout = io.StringIO()
            with redirect_stdout(stdout):
                exit_code = main(
                    [
                        "breakpoint", "add", str(rom_path), "0x0020D180",
                        "--label", "frontier", "--json",
                    ]
                )
            self.assertEqual(exit_code, 0)
            add_payload = json.loads(stdout.getvalue())
            self.assertEqual(add_payload["breakpoint"]["id"], 1)
            self.assertEqual(add_payload["breakpoint"]["address"], 0x0020D180)
            self.assertEqual(add_payload["breakpoint"]["label"], "frontier")

            stdout = io.StringIO()
            with redirect_stdout(stdout):
                exit_code = main(["breakpoint", "list", str(rom_path), "--json"])
            self.assertEqual(exit_code, 0)
            list_payload = json.loads(stdout.getvalue())
            self.assertEqual(list_payload["count"], 1)
            self.assertEqual(list_payload["format_version"], BREAKPOINTS_FORMAT_VERSION)

            stdout = io.StringIO()
            with redirect_stdout(stdout):
                exit_code = main(["breakpoint", "remove", str(rom_path), "1", "--json"])
            self.assertEqual(exit_code, 0)
            self.assertEqual(load_breakpoints(rom_path), ())

    def test_cli_check_against_captured_event_log(self) -> None:
        """End-to-end: capture a real event log, then check that a
        breakpoint at the entry-point PC fires."""
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            rom_path = tmp / "demo.ngc"
            data = bytearray(0x40)
            data[0x00:0x1C] = b"LICENSED BY SNK CORPORATION".ljust(0x1C, b"\x00")
            data[0x1C:0x20] = (0x00200040).to_bytes(4, "little")
            data[0x22] = 0
            data[0x23] = 0x10
            data[0x24:0x30] = b"BP CHECK\x00\x00\x00\x00"
            # body: nop ; nop ; nop
            data.extend(b"\x00\x00\x00")
            rom_path.write_bytes(bytes(data))

            stdout = io.StringIO()
            with redirect_stdout(stdout):
                exit_code = main(
                    [
                        "breakpoint", "add", str(rom_path), "0x00200041",
                        "--label", "second-nop", "--json",
                    ]
                )
            self.assertEqual(exit_code, 0)

            event_log_path = tmp / "demo.event.json"
            stdout = io.StringIO()
            with redirect_stdout(stdout):
                exit_code = main(
                    [
                        "eventlog", "capture", str(rom_path), str(event_log_path),
                        "--count", "3", "--json",
                    ]
                )
            self.assertEqual(exit_code, 0)

            stdout = io.StringIO()
            with redirect_stdout(stdout):
                exit_code = main(
                    [
                        "breakpoint", "check", str(rom_path),
                        str(event_log_path), "--json",
                    ]
                )
            self.assertEqual(exit_code, 0)
            check_payload = json.loads(stdout.getvalue())
            self.assertEqual(check_payload["breakpoint_count"], 1)
            self.assertEqual(check_payload["hit_count"], 1)
            first_hit = check_payload["hits"][0]
            self.assertEqual(first_hit["event_pc"], 0x00200041)
            self.assertEqual(first_hit["breakpoint"]["label"], "second-nop")


class BreakpointAddSymbolCliTests(unittest.TestCase):
    """Phase 2: `breakpoint add-symbol` resolves a name via a .map file."""

    _MAP_BODY = """=== Public symbols ===
  __startup                0x00200040
  _main                    0x00200080
  _ngpc_vblank             0x0020D0CB
"""

    def _write_map(self, path: Path) -> None:
        path.write_text(self._MAP_BODY, encoding="utf-8")

    def test_add_symbol_resolves_then_breakpoint_check_hits_pc(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            rom_path = tmp / "demo.ngc"
            data = bytearray(0x40)
            data[0x00:0x1C] = b"LICENSED BY SNK CORPORATION".ljust(0x1C, b"\x00")
            data[0x1C:0x20] = (0x00200040).to_bytes(4, "little")
            data[0x22] = 0
            data[0x23] = 0x10
            data[0x24:0x30] = b"SYMBOL\x00\x00\x00\x00\x00\x00"
            # body: nop ; nop ; nop ; nop (entry at 0x200040)
            data.extend(b"\x00\x00\x00\x00\x00")
            rom_path.write_bytes(bytes(data))

            map_path = tmp / "demo.map"
            self._write_map(map_path)

            stdout = io.StringIO()
            with redirect_stdout(stdout):
                exit_code = main(
                    [
                        "breakpoint", "add-symbol", str(rom_path),
                        "__startup",
                        "--map", str(map_path),
                        "--json",
                    ]
                )
            self.assertEqual(exit_code, 0)
            add_payload = json.loads(stdout.getvalue())
            self.assertEqual(add_payload["resolved_symbol"]["name"], "__startup")
            self.assertEqual(add_payload["resolved_symbol"]["address"], 0x00200040)
            self.assertEqual(add_payload["breakpoint"]["address"], 0x00200040)
            # Label defaults to the resolved symbol name.
            self.assertEqual(add_payload["breakpoint"]["label"], "__startup")

            # Capture one event-log step at PC=0x00200040 and verify the
            # breakpoint registered at that PC actually fires.
            event_log_path = tmp / "demo.event.json"
            stdout = io.StringIO()
            with redirect_stdout(stdout):
                exit_code = main(
                    [
                        "eventlog", "capture", str(rom_path), str(event_log_path),
                        "--count", "1", "--json",
                    ]
                )
            self.assertEqual(exit_code, 0)

            stdout = io.StringIO()
            with redirect_stdout(stdout):
                exit_code = main(
                    [
                        "breakpoint", "check", str(rom_path),
                        str(event_log_path), "--json",
                    ]
                )
            self.assertEqual(exit_code, 0)
            check_payload = json.loads(stdout.getvalue())
            self.assertEqual(check_payload["hit_count"], 1)
            self.assertEqual(check_payload["hits"][0]["event_pc"], 0x00200040)

    def test_add_symbol_unknown_name_returns_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            rom_path = tmp / "demo.ngc"
            _write_demo_rom(rom_path)
            map_path = tmp / "demo.map"
            self._write_map(map_path)

            stdout = io.StringIO()
            with redirect_stdout(stdout):
                exit_code = main(
                    [
                        "breakpoint", "add-symbol", str(rom_path),
                        "_nonexistent_symbol",
                        "--map", str(map_path),
                        "--json",
                    ]
                )
            self.assertEqual(exit_code, 1)
            err_payload = json.loads(stdout.getvalue())
            self.assertIn("error", err_payload)
            self.assertIn("_nonexistent_symbol", err_payload["error"])
            # Registry must not have been written.
            self.assertEqual(load_breakpoints(rom_path), ())

    def test_add_symbol_with_explicit_label_overrides_default(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            rom_path = tmp / "demo.ngc"
            _write_demo_rom(rom_path)
            map_path = tmp / "demo.map"
            self._write_map(map_path)

            stdout = io.StringIO()
            with redirect_stdout(stdout):
                exit_code = main(
                    [
                        "breakpoint", "add-symbol", str(rom_path),
                        "_ngpc_vblank",
                        "--map", str(map_path),
                        "--label", "raster-callback",
                        "--json",
                    ]
                )
            self.assertEqual(exit_code, 0)
            payload = json.loads(stdout.getvalue())
            self.assertEqual(payload["breakpoint"]["label"], "raster-callback")
            self.assertEqual(payload["breakpoint"]["address"], 0x0020D0CB)


if __name__ == "__main__":
    unittest.main()
