"""Watchpoint registry and event-log match tests for NgpCraft Emulator."""

from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from core.watchpoints import (
    WATCHPOINTS_FORMAT_VERSION,
    Watchpoint,
    add_watchpoint,
    clear_watchpoints,
    load_watchpoints,
    match_event_log_accesses,
    match_event_log_writes,
    remove_watchpoint,
    save_watchpoints,
    watchpoints_path_for_rom,
)
from ngpc_emu import main


def _write_demo_rom(path: Path, entry_point: int = 0x00200040) -> None:
    data = bytearray(0x40)
    data[0x00:0x1C] = b"LICENSED BY SNK CORPORATION".ljust(0x1C, b"\x00")
    data[0x1C:0x20] = entry_point.to_bytes(4, "little")
    data[0x22] = 0
    data[0x23] = 0x10
    data[0x24:0x30] = b"WATCH TEST\x00\x00"
    path.write_bytes(bytes(data))


class WatchpointModelTests(unittest.TestCase):
    def test_overlaps_exact_byte_hit(self) -> None:
        wp = Watchpoint(id=1, kind="write", start=0x4000, size=1)
        self.assertTrue(wp.overlaps_range(0x4000, 1))
        self.assertFalse(wp.overlaps_range(0x3FFF, 1))
        self.assertFalse(wp.overlaps_range(0x4001, 1))

    def test_overlaps_word_write_spanning_watchpoint(self) -> None:
        wp = Watchpoint(id=2, kind="write", start=0x4001, size=1)
        self.assertTrue(wp.overlaps_range(0x4000, 2))
        self.assertTrue(wp.overlaps_range(0x4001, 2))
        self.assertFalse(wp.overlaps_range(0x4002, 2))

    def test_overlaps_range_zero_size_is_false(self) -> None:
        wp = Watchpoint(id=3, kind="write", start=0x4000, size=1)
        self.assertFalse(wp.overlaps_range(0x4000, 0))

    def test_range_watchpoint_inclusive_end(self) -> None:
        wp = Watchpoint(id=4, kind="write", start=0x5000, size=8)
        self.assertEqual(wp.end_inclusive(), 0x5007)
        self.assertTrue(wp.contains(0x5000))
        self.assertTrue(wp.contains(0x5007))
        self.assertFalse(wp.contains(0x5008))


class WatchpointRegistryRoundtripTests(unittest.TestCase):
    def test_empty_registry_returns_empty_tuple(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            _write_demo_rom(rom_path)

            self.assertEqual(load_watchpoints(rom_path), ())

    def test_add_assigns_increasing_ids_and_persists(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            _write_demo_rom(rom_path)

            wp1 = add_watchpoint(rom_path, start=0x4000, label="scratch")
            wp2 = add_watchpoint(rom_path, start=0x6F86, size=1, label="hw_user")

            self.assertEqual(wp1.id, 1)
            self.assertEqual(wp2.id, 2)
            self.assertEqual(wp1.label, "scratch")
            self.assertEqual(wp1.size, 1)
            self.assertEqual(wp1.kind, "write")

            reloaded = load_watchpoints(rom_path)
            self.assertEqual(len(reloaded), 2)
            self.assertEqual(reloaded[0].start, 0x4000)
            self.assertEqual(reloaded[1].start, 0x6F86)

    def test_add_rejects_unknown_kind(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            _write_demo_rom(rom_path)

            # "execute" is not a v2 kind; "read"/"write"/"access" all are.
            with self.assertRaises(ValueError):
                add_watchpoint(rom_path, kind="execute", start=0x4000)

    def test_add_rejects_non_positive_size(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            _write_demo_rom(rom_path)

            with self.assertRaises(ValueError):
                add_watchpoint(rom_path, start=0x4000, size=0)

    def test_remove_returns_dropped_row(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            _write_demo_rom(rom_path)

            wp1 = add_watchpoint(rom_path, start=0x4000)
            add_watchpoint(rom_path, start=0x4010)

            dropped = remove_watchpoint(rom_path, wp1.id)
            self.assertEqual(dropped.start, 0x4000)
            self.assertEqual(len(load_watchpoints(rom_path)), 1)

    def test_remove_unknown_id_raises_key_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            _write_demo_rom(rom_path)
            add_watchpoint(rom_path, start=0x4000)

            with self.assertRaises(KeyError):
                remove_watchpoint(rom_path, 999)

    def test_clear_drops_all_and_returns_count(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            _write_demo_rom(rom_path)
            add_watchpoint(rom_path, start=0x4000)
            add_watchpoint(rom_path, start=0x4010)

            dropped = clear_watchpoints(rom_path)
            self.assertEqual(dropped, 2)
            self.assertEqual(load_watchpoints(rom_path), ())

    def test_loader_rejects_unknown_format_version(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            _write_demo_rom(rom_path)
            path = watchpoints_path_for_rom(rom_path)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                json.dumps(
                    {
                        "format": "ngpc-emu-watchpoints",
                        "format_version": "9999-99-99.v999",
                        "watchpoints": [],
                    }
                ),
                encoding="utf-8",
            )

            with self.assertRaises(ValueError) as ctx:
                load_watchpoints(rom_path)
            self.assertIn("Unknown watchpoint format_version", str(ctx.exception))


class WatchpointEventLogMatchTests(unittest.TestCase):
    def _fake_event_log_payload(self) -> dict[str, object]:
        return {
            "events": [
                {
                    "index": 0,
                    "pc": 0x00200040,
                    "assembly": "nop",
                    "memory_writes": [],
                },
                {
                    "index": 1,
                    "pc": 0x00200041,
                    "assembly": "ld (0x4000), 0xAB",
                    "memory_writes": [
                        {
                            "address": 0x004000,
                            "address_hex": "0x004000",
                            "size": 1,
                            "data_hex": "AB",
                            "note": "byte store",
                        }
                    ],
                },
                {
                    "index": 2,
                    "pc": 0x00200043,
                    "assembly": "ldw (0x4000), 0xBEEF",
                    "memory_writes": [
                        {
                            "address": 0x004000,
                            "address_hex": "0x004000",
                            "size": 2,
                            "data_hex": "EF BE",
                            "note": "word store",
                        }
                    ],
                },
            ]
        }

    def test_match_yields_one_hit_per_overlap(self) -> None:
        wps = (Watchpoint(id=1, kind="write", start=0x4000, size=1, label="x"),)
        hits = match_event_log_writes(wps, self._fake_event_log_payload())

        self.assertEqual(len(hits), 2)
        self.assertEqual(hits[0].event_index, 1)
        self.assertEqual(hits[0].address, 0x004000)
        self.assertEqual(hits[0].data_hex, "AB")
        self.assertEqual(hits[1].event_index, 2)
        self.assertEqual(hits[1].size, 2)

    def test_multiple_watchpoints_each_emit_independent_hits(self) -> None:
        wps = (
            Watchpoint(id=1, kind="write", start=0x4000, size=1, label="A"),
            Watchpoint(id=2, kind="write", start=0x4001, size=1, label="B"),
        )
        hits = match_event_log_writes(wps, self._fake_event_log_payload())

        # word write at 0x4000 size=2 overlaps both A and B.  byte write
        # at 0x4000 size=1 overlaps only A.
        self.assertEqual(len(hits), 3)
        self.assertEqual([h.watchpoint.id for h in hits], [1, 1, 2])

    def test_no_match_when_address_out_of_range(self) -> None:
        wps = (Watchpoint(id=1, kind="write", start=0x5000, size=1),)
        hits = match_event_log_writes(wps, self._fake_event_log_payload())
        self.assertEqual(hits, ())


class WatchpointReadAndAccessMatchTests(unittest.TestCase):
    """v2 extensions: kind=read and kind=access match memory_reads."""

    def _fake_event_log_with_read(self) -> dict[str, object]:
        return {
            "events": [
                {
                    "index": 0,
                    "pc": 0x00200040,
                    "assembly": "pop SR",
                    "memory_writes": [],
                    "memory_reads": [
                        {
                            "address": 0x004000,
                            "address_hex": "0x004000",
                            "size": 2,
                            "data_hex": "81 88",
                            "note": "POP SR stack read",
                        }
                    ],
                },
                {
                    "index": 1,
                    "pc": 0x00200041,
                    "assembly": "ld (0x4000), 0x5A",
                    "memory_writes": [
                        {
                            "address": 0x004000,
                            "address_hex": "0x004000",
                            "size": 1,
                            "data_hex": "5A",
                            "note": "byte store",
                        }
                    ],
                    "memory_reads": [],
                },
            ]
        }

    def test_kind_read_matches_memory_reads_only(self) -> None:
        wps = (Watchpoint(id=1, kind="read", start=0x4000, size=1),)
        hits = match_event_log_accesses(wps, self._fake_event_log_with_read())

        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0].event_index, 0)
        self.assertEqual(hits[0].access_kind, "read")
        self.assertEqual(hits[0].address, 0x004000)
        self.assertEqual(hits[0].size, 2)

    def test_kind_write_ignores_memory_reads(self) -> None:
        wps = (Watchpoint(id=1, kind="write", start=0x4000, size=1),)
        hits = match_event_log_accesses(wps, self._fake_event_log_with_read())

        # Only the write event matches.
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0].event_index, 1)
        self.assertEqual(hits[0].access_kind, "write")

    def test_kind_access_matches_both_reads_and_writes(self) -> None:
        wps = (Watchpoint(id=1, kind="access", start=0x4000, size=1),)
        hits = match_event_log_accesses(wps, self._fake_event_log_with_read())

        self.assertEqual(len(hits), 2)
        kinds = {h.access_kind for h in hits}
        self.assertEqual(kinds, {"read", "write"})

    def test_match_event_log_writes_legacy_filters_to_writes(self) -> None:
        """The v1 alias keeps returning write hits only, even when read
        watchpoints exist in the registry."""
        wps = (
            Watchpoint(id=1, kind="write", start=0x4000, size=1),
            Watchpoint(id=2, kind="read", start=0x4000, size=1),
            Watchpoint(id=3, kind="access", start=0x4000, size=1),
        )
        hits = match_event_log_writes(wps, self._fake_event_log_with_read())

        # Two writers hit the write event (kind=write and kind=access).
        # No read hits should appear.
        self.assertTrue(all(h.access_kind == "write" for h in hits))
        wp_kinds = {h.watchpoint.kind for h in hits}
        self.assertEqual(wp_kinds, {"write", "access"})


class WatchpointCliTests(unittest.TestCase):
    def test_cli_add_list_remove_roundtrip(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            _write_demo_rom(rom_path)

            stdout = io.StringIO()
            with redirect_stdout(stdout):
                exit_code = main(
                    [
                        "watchpoint", "add", str(rom_path), "0x4000",
                        "--size", "2", "--label", "stack-frame", "--json",
                    ]
                )
            self.assertEqual(exit_code, 0)
            add_payload = json.loads(stdout.getvalue())
            self.assertEqual(add_payload["watchpoint"]["id"], 1)
            self.assertEqual(add_payload["watchpoint"]["start"], 0x4000)
            self.assertEqual(add_payload["watchpoint"]["size"], 2)
            self.assertEqual(add_payload["watchpoint"]["label"], "stack-frame")

            stdout = io.StringIO()
            with redirect_stdout(stdout):
                exit_code = main(["watchpoint", "list", str(rom_path), "--json"])
            self.assertEqual(exit_code, 0)
            list_payload = json.loads(stdout.getvalue())
            self.assertEqual(list_payload["count"], 1)
            self.assertEqual(list_payload["format_version"], WATCHPOINTS_FORMAT_VERSION)

            stdout = io.StringIO()
            with redirect_stdout(stdout):
                exit_code = main(["watchpoint", "remove", str(rom_path), "1", "--json"])
            self.assertEqual(exit_code, 0)
            remove_payload = json.loads(stdout.getvalue())
            self.assertEqual(remove_payload["removed"]["id"], 1)

            self.assertEqual(load_watchpoints(rom_path), ())

    def test_cli_check_against_captured_event_log(self) -> None:
        """End-to-end: capture a real event log that does a byte store, then
        verify watchpoint check picks up the matching write."""
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            rom_path = tmp / "demo.ngc"
            data = bytearray(0x40)
            data[0x00:0x1C] = b"LICENSED BY SNK CORPORATION".ljust(0x1C, b"\x00")
            data[0x1C:0x20] = (0x00200040).to_bytes(4, "little")
            data[0x22] = 0
            data[0x23] = 0x10
            data[0x24:0x30] = b"WATCH CHECK\x00"
            # F1 abs16le 00 imm8 = ld (abs16), imm8 ; then nop
            # writes 0x5A at 0x4000.
            body = b"\xF1\x00\x40\x00\x5A\x00"
            data.extend(body)
            rom_path.write_bytes(bytes(data))

            # Add a watchpoint at 0x4000.
            stdout = io.StringIO()
            with redirect_stdout(stdout):
                exit_code = main(
                    [
                        "watchpoint", "add", str(rom_path), "0x4000",
                        "--label", "ram-byte", "--json",
                    ]
                )
            self.assertEqual(exit_code, 0)

            # Capture an event log of 2 instructions.
            event_log_path = tmp / "demo.eventlog.json"
            stdout = io.StringIO()
            with redirect_stdout(stdout):
                exit_code = main(
                    [
                        "eventlog", "capture", str(rom_path), str(event_log_path),
                        "--count", "2", "--json",
                    ]
                )
            self.assertEqual(exit_code, 0)

            # Match watchpoints against the captured event log.
            stdout = io.StringIO()
            with redirect_stdout(stdout):
                exit_code = main(
                    [
                        "watchpoint", "check", str(rom_path),
                        str(event_log_path), "--json",
                    ]
                )
            self.assertEqual(exit_code, 0)
            check_payload = json.loads(stdout.getvalue())
            self.assertEqual(check_payload["watchpoint_count"], 1)
            self.assertGreaterEqual(check_payload["hit_count"], 1)
            first_hit = check_payload["hits"][0]
            self.assertEqual(first_hit["address"], 0x4000)
            self.assertEqual(first_hit["watchpoint"]["label"], "ram-byte")


class WatchpointReadCliEndToEndTests(unittest.TestCase):
    """End-to-end: capture POP SR (which emits memory_reads) and verify the
    CLI watchpoint flow picks up read-kind hits."""

    def test_cli_kind_read_detects_pop_sr_stack_load(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            rom_path = tmp / "demo.ngc"
            data = bytearray(0x40)
            data[0x00:0x1C] = b"LICENSED BY SNK CORPORATION".ljust(0x1C, b"\x00")
            data[0x1C:0x20] = (0x00200040).to_bytes(4, "little")
            data[0x22] = 0
            data[0x23] = 0x10
            data[0x24:0x30] = b"POP SR\x00\x00\x00\x00\x00\x00"
            # body: POP SR (0x03)
            data.extend(b"\x03\x00")
            rom_path.write_bytes(bytes(data))

            # Add a read watchpoint at the stack address we will seed.
            stdout = io.StringIO()
            with redirect_stdout(stdout):
                exit_code = main(
                    [
                        "watchpoint", "add", str(rom_path), "0x6BFE",
                        "--kind", "read", "--size", "2",
                        "--label", "sr-restore-slot", "--json",
                    ]
                )
            self.assertEqual(exit_code, 0)

            # Capture POP SR with the stack seeded so the read can succeed.
            # POP SR reads 2 bytes at XSP. The cold-start image has 0x00 at
            # the system page so the read returns 0x0000 (invalid SR shape
            # for HW but that's fine — we only care that the read happens).
            event_log_path = tmp / "demo.event.json"
            stdout = io.StringIO()
            with redirect_stdout(stdout):
                exit_code = main(
                    [
                        "eventlog", "capture", str(rom_path), str(event_log_path),
                        "--count", "1",
                        "--seed-xsp", "0x6BFE",
                        "--json",
                    ]
                )
            self.assertEqual(exit_code, 0)

            stdout = io.StringIO()
            with redirect_stdout(stdout):
                exit_code = main(
                    [
                        "watchpoint", "check", str(rom_path),
                        str(event_log_path), "--json",
                    ]
                )
            self.assertEqual(exit_code, 0)
            check_payload = json.loads(stdout.getvalue())
            self.assertEqual(check_payload["watchpoint_count"], 1)
            self.assertGreaterEqual(check_payload["hit_count"], 1)
            first_hit = check_payload["hits"][0]
            self.assertEqual(first_hit["access_kind"], "read")
            self.assertEqual(first_hit["address"], 0x6BFE)
            self.assertEqual(first_hit["watchpoint"]["kind"], "read")


class WatchpointPhase4ValueFilterTests(unittest.TestCase):
    """v3: byte-value filter on a watchpoint narrows hits to a specific
    operand value. Default (`value=None`) matches every access in range."""

    def _fake_event_log_with_writes(self) -> dict[str, object]:
        return {
            "events": [
                {
                    "index": 0,
                    "pc": 0x00200040,
                    "assembly": "ld (0x4000), 0x5A",
                    "memory_writes": [
                        {
                            "address": 0x004000,
                            "address_hex": "0x004000",
                            "size": 1,
                            "data_hex": "5A",
                            "note": "byte store",
                        }
                    ],
                    "memory_reads": [],
                },
                {
                    "index": 1,
                    "pc": 0x00200044,
                    "assembly": "ld (0x4000), 0xFF",
                    "memory_writes": [
                        {
                            "address": 0x004000,
                            "address_hex": "0x004000",
                            "size": 1,
                            "data_hex": "FF",
                            "note": "byte store",
                        }
                    ],
                    "memory_reads": [],
                },
            ]
        }

    def test_value_filter_matches_only_specific_byte(self) -> None:
        wps = (Watchpoint(id=1, kind="write", start=0x4000, size=1, value=0x5A),)
        hits = match_event_log_accesses(wps, self._fake_event_log_with_writes())

        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0].event_index, 0)
        self.assertEqual(hits[0].data_hex, "5A")

    def test_value_filter_no_hit_when_mismatch(self) -> None:
        wps = (Watchpoint(id=1, kind="write", start=0x4000, size=1, value=0xAB),)
        hits = match_event_log_accesses(wps, self._fake_event_log_with_writes())

        self.assertEqual(hits, ())

    def test_value_none_matches_every_access_in_range(self) -> None:
        """Backwards compatibility: value=None means no filter."""
        wps = (Watchpoint(id=1, kind="write", start=0x4000, size=1, value=None),)
        hits = match_event_log_accesses(wps, self._fake_event_log_with_writes())

        self.assertEqual(len(hits), 2)

    def test_matches_value_first_byte_only(self) -> None:
        """data_hex 'FF 00' starts with FF: value=0xFF matches, 0x00 does not."""
        wp_match = Watchpoint(id=1, kind="write", start=0, size=2, value=0xFF)
        wp_miss = Watchpoint(id=2, kind="write", start=0, size=2, value=0x00)
        self.assertTrue(wp_match.matches_value("FF 00"))
        self.assertFalse(wp_miss.matches_value("FF 00"))

    def test_value_filter_rejects_out_of_range_byte(self) -> None:
        with self.assertRaises(ValueError):
            from core.watchpoints import add_watchpoint as _add
            with tempfile.TemporaryDirectory() as tmpdir:
                rom_path = Path(tmpdir) / "demo.ngc"
                _write_demo_rom(rom_path)
                _add(rom_path, start=0x4000, value=999)


class WatchpointPhase3UniversalReadTrackingTests(unittest.TestCase):
    """Phase 3: every executor that calls `_read_runtime_bytes` now
    auto-surfaces its reads. Verify `kind="read"` matches against an
    opcode that is NOT POP SR (the original reference case)."""

    def test_cli_kind_read_matches_cp_abs24_immediate(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            rom_path = tmp / "demo.ngc"
            data = bytearray(0x40)
            data[0x00:0x1C] = b"LICENSED BY SNK CORPORATION".ljust(0x1C, b"\x00")
            data[0x1C:0x20] = (0x00200040).to_bytes(4, "little")
            data[0x22] = 0
            data[0x23] = 0x10
            data[0x24:0x30] = b"CP ABS24\x00\x00\x00\x00"
            # C2 abs24le 3F imm8 = cp (0x004000), 0x5A ; nop
            data.extend(b"\xC2\x00\x40\x00\x3F\x5A\x00")
            rom_path.write_bytes(bytes(data))

            stdout = io.StringIO()
            with redirect_stdout(stdout):
                exit_code = main(
                    [
                        "watchpoint", "add", str(rom_path), "0x4000",
                        "--kind", "read", "--label", "compare-target", "--json",
                    ]
                )
            self.assertEqual(exit_code, 0)

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
                        "watchpoint", "check", str(rom_path),
                        str(event_log_path), "--json",
                    ]
                )
            self.assertEqual(exit_code, 0)
            check_payload = json.loads(stdout.getvalue())
            self.assertEqual(check_payload["hit_count"], 1)
            hit = check_payload["hits"][0]
            self.assertEqual(hit["access_kind"], "read")
            self.assertEqual(hit["address"], 0x4000)


if __name__ == "__main__":
    unittest.main()
