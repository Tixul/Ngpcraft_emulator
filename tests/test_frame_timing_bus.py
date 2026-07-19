"""M3 Phase 3.1a — frame_state driven read bus for RAS.V + BLNK.

Tests the bus-level override (`load_read_bus(frame_state=...)`) and
its propagation through `_build_palette_memory_view` /
`memory-dump --seed-from` / `tick-frame → memory-dump` workflow.

Executor-side reads (step-exec / run-* / eventlog capture chain) are
deferred to Phase 3.1b.
"""

from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from core.frame_timing import (
    SCANLINES_PER_FRAME,
    VISIBLE_SCANLINES,
    FrameState,
    initial_frame_state,
)
from core.machine import load_machine_state
from core.memory import load_read_bus
from core.savestate import build_savestate_payload, save_savestate
from ngpc_emu import main


def _write_demo_rom(path: Path, entry_point: int = 0x00200040) -> None:
    data = bytearray(0x40)
    data[0x00:0x1C] = b"LICENSED BY SNK CORPORATION".ljust(0x1C, b"\x00")
    data[0x1C:0x20] = entry_point.to_bytes(4, "little")
    data[0x22] = 0
    data[0x23] = 0x10
    data[0x24:0x30] = b"FRAME 3.1A\x00\x00"
    path.write_bytes(bytes(data))


class BusOverrideForFrameStateTests(unittest.TestCase):
    """Lock the documented behavior: `load_read_bus(frame_state=...)`
    drives `RAS.V` (0x8009) and the BLNK bit of 2D Status (0x8010)."""

    def test_no_frame_state_keeps_cold_start_zeros(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            _write_demo_rom(rom_path)
            bus = load_read_bus(rom_path)
            self.assertEqual(bus.builtin_bytes.get(0x008009, 0), 0x00)
            self.assertEqual(bus.builtin_bytes.get(0x008010, 0), 0x00)

    def test_explicit_initial_state_is_byte_identical_to_no_state(self) -> None:
        # initial_frame_state() must not change observable bytes vs.
        # the bootstrap default — preserves backward compat for every
        # caller that doesn't forward a frame_state yet.
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            _write_demo_rom(rom_path)
            bus_none = load_read_bus(rom_path)
            bus_initial = load_read_bus(
                rom_path, frame_state=initial_frame_state(),
            )
            self.assertEqual(bus_none.builtin_bytes, bus_initial.builtin_bytes)

    def test_scanline_in_visible_region_drives_ras_v_blnk_clear(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            _write_demo_rom(rom_path)
            bus = load_read_bus(
                rom_path,
                frame_state=FrameState(scanline=42, frame_count=0),
            )
            self.assertEqual(bus.builtin_bytes[0x008009], 42)
            self.assertEqual(bus.builtin_bytes[0x008010], 0x00)

    def test_scanline_in_vblank_sets_blnk_bit(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            _write_demo_rom(rom_path)
            bus = load_read_bus(
                rom_path,
                frame_state=FrameState(scanline=160, frame_count=0),
            )
            self.assertEqual(bus.builtin_bytes[0x008009], 160)
            # BLNK is bit 6 (0x40). Other bits stay 0 (C.OVR not modeled).
            self.assertEqual(bus.builtin_bytes[0x008010], 0x40)

    def test_scanline_at_visible_boundary_is_still_visible(self) -> None:
        # Scanline 151 is the last visible line; 152 is the first VBlank.
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            _write_demo_rom(rom_path)
            bus_151 = load_read_bus(
                rom_path, frame_state=FrameState(151, 0),
            )
            bus_152 = load_read_bus(
                rom_path, frame_state=FrameState(152, 0),
            )
            self.assertEqual(bus_151.builtin_bytes[0x008009], 151)
            self.assertEqual(bus_151.builtin_bytes[0x008010], 0x00)
            self.assertEqual(bus_152.builtin_bytes[0x008009], 152)
            self.assertEqual(bus_152.builtin_bytes[0x008010], 0x40)

    def test_scanline_at_last_vblank_line(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            _write_demo_rom(rom_path)
            last = SCANLINES_PER_FRAME - 1  # 197
            bus = load_read_bus(
                rom_path, frame_state=FrameState(last, 0),
            )
            self.assertEqual(bus.builtin_bytes[0x008009], last)
            self.assertEqual(bus.builtin_bytes[0x008010], 0x40)


class MemoryDumpSeedFromTests(unittest.TestCase):
    """`memory-dump --seed-from` consumes the savestate's frame_state
    and exposes the live RAS.V + BLNK byte to the CLI consumer."""

    def _seed_state(self, rom_path: Path, frame_state: FrameState) -> Path:
        machine = load_machine_state(rom_path)
        state_path = rom_path.parent / "seed.state.json"
        save_savestate(
            state_path,
            build_savestate_payload(
                rom_path=rom_path,
                rom_header=machine.header,
                cpu=machine.cpu,
                writable_overlay={},
                frame_state=frame_state,
            ),
        )
        return state_path

    def test_no_seed_returns_cold_start_zeros(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            _write_demo_rom(rom_path)
            stdout = io.StringIO()
            with redirect_stdout(stdout):
                exit_code = main(
                    [
                        "memory-dump", str(rom_path),
                        "0x8008", "--count", "16",
                        "--json",
                    ],
                )
            self.assertEqual(exit_code, 0)
            payload = json.loads(stdout.getvalue())
            by_addr = {b["address"]: b["value"] for b in payload["bytes"]}
            self.assertEqual(by_addr[0x008009], 0x00)
            self.assertEqual(by_addr[0x008010], 0x00)

    def test_seed_at_visible_scanline_shows_ras_v(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            _write_demo_rom(rom_path)
            state_path = self._seed_state(
                rom_path, FrameState(scanline=42, frame_count=0),
            )
            stdout = io.StringIO()
            with redirect_stdout(stdout):
                exit_code = main(
                    [
                        "memory-dump", str(rom_path),
                        "0x8009", "--count", "1",
                        "--seed-from", str(state_path),
                        "--json",
                    ],
                )
            self.assertEqual(exit_code, 0)
            payload = json.loads(stdout.getvalue())
            self.assertEqual(payload["bytes"][0]["address"], 0x008009)
            self.assertEqual(payload["bytes"][0]["value"], 42)

    def test_seed_in_vblank_sets_blnk_bit_at_0x8010(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            _write_demo_rom(rom_path)
            state_path = self._seed_state(
                rom_path, FrameState(scanline=160, frame_count=0),
            )
            stdout = io.StringIO()
            with redirect_stdout(stdout):
                exit_code = main(
                    [
                        "memory-dump", str(rom_path),
                        "0x8010", "--count", "1",
                        "--seed-from", str(state_path),
                        "--json",
                    ],
                )
            self.assertEqual(exit_code, 0)
            payload = json.loads(stdout.getvalue())
            self.assertEqual(payload["bytes"][0]["value"], 0x40)


class TickFrameThenMemoryDumpTests(unittest.TestCase):
    """End-to-end: tick-frame produces a savestate at scanline N,
    memory-dump --seed-from observes the live byte at 0x8009."""

    def test_round_trip_at_vblank_boundary(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            rom_path = tmp / "demo.ngc"
            state_path = tmp / "after_tick.state.json"
            _write_demo_rom(rom_path)

            # Tick to scanline 152 (first VBlank line).
            with redirect_stdout(io.StringIO()):
                code = main(
                    [
                        "tick-frame", str(rom_path),
                        "--scanlines", str(VISIBLE_SCANLINES),
                        "--save-state", str(state_path),
                    ],
                )
            self.assertEqual(code, 0)

            # Now dump RAS.V + 2D Status.
            stdout = io.StringIO()
            with redirect_stdout(stdout):
                code = main(
                    [
                        "memory-dump", str(rom_path),
                        "0x8009", "--count", "8",  # covers 0x8009..0x8010
                        "--seed-from", str(state_path),
                        "--json",
                    ],
                )
            self.assertEqual(code, 0)
            payload = json.loads(stdout.getvalue())
            by_addr = {b["address"]: b["value"] for b in payload["bytes"]}
            self.assertEqual(by_addr[0x008009], 152)
            self.assertEqual(by_addr[0x008010], 0x40)

    def test_round_trip_in_visible_region(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            rom_path = tmp / "demo.ngc"
            state_path = tmp / "after_tick.state.json"
            _write_demo_rom(rom_path)

            # Tick to scanline 100 (visible region).
            with redirect_stdout(io.StringIO()):
                main(
                    [
                        "tick-frame", str(rom_path),
                        "--scanlines", "100",
                        "--save-state", str(state_path),
                    ],
                )

            stdout = io.StringIO()
            with redirect_stdout(stdout):
                main(
                    [
                        "memory-dump", str(rom_path),
                        "0x8009", "--count", "8",
                        "--seed-from", str(state_path),
                        "--json",
                    ],
                )
            payload = json.loads(stdout.getvalue())
            by_addr = {b["address"]: b["value"] for b in payload["bytes"]}
            self.assertEqual(by_addr[0x008009], 100)
            self.assertEqual(by_addr[0x008010], 0x00)


if __name__ == "__main__":
    unittest.main()
