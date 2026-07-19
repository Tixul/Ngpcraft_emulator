"""M3 Phase 0 — frame/scanline state model + savestate v3 + tick-frame CLI."""

from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from core.frame_timing import (
    FRAMES_PER_SECOND,
    SCANLINES_PER_FRAME,
    VBLANK_SCANLINES,
    VISIBLE_SCANLINES,
    FrameState,
    advance_frames,
    advance_scanlines,
    detect_vblank_transitions,
    initial_frame_state,
)
from core.machine import load_machine_state
from core.savestate import (
    SAVESTATE_BACKWARD_COMPAT_VERSIONS,
    SAVESTATE_FORMAT_VERSION,
    build_savestate_payload,
    load_savestate,
    save_savestate,
)
from ngpc_emu import main


def _write_demo_rom(path: Path, entry_point: int = 0x00200040) -> None:
    data = bytearray(0x40)
    data[0x00:0x1C] = b"LICENSED BY SNK CORPORATION".ljust(0x1C, b"\x00")
    data[0x1C:0x20] = entry_point.to_bytes(4, "little")
    data[0x22] = 0
    data[0x23] = 0x10
    data[0x24:0x30] = b"FRAME TIME\x00\x00"
    path.write_bytes(bytes(data))


class FrameTimingConstantsTests(unittest.TestCase):
    """Lock the HW-canonical scanline budget from K2GETechRef."""

    def test_scanline_budget_is_199_per_frame(self) -> None:
        """199, MEASURED ON SILICON. It used to lock 198, and 198 was wrong.

        hw_calibration/bin/main.ngc reads RAS.V (0x8009) on a real NGPC and prints
        its MAXIMUM before the wrap. The console printed 00C6 = 198, so the counter
        runs 0..198 and the frame is 199 lines.

        The Tech Ref sentence we had inferred 198 from is ambiguous ("signal
        generation for the 0th line occurs at the beginning of line 198" -- the 198th
        line, or index 198?). The register is not ambiguous.
        """
        self.assertEqual(SCANLINES_PER_FRAME, 199)

    def test_visible_region_is_152_scanlines(self) -> None:
        self.assertEqual(VISIBLE_SCANLINES, 152)

    def test_vblank_region_is_47_scanlines(self) -> None:
        # 199 total - 152 visible. (46 back when the total was believed to be 198.)
        self.assertEqual(VBLANK_SCANLINES, 47)
        self.assertEqual(
            VISIBLE_SCANLINES + VBLANK_SCANLINES, SCANLINES_PER_FRAME,
        )

    def test_frames_per_second_is_60(self) -> None:
        self.assertEqual(FRAMES_PER_SECOND, 60)


class FrameStateBasicsTests(unittest.TestCase):
    def test_initial_frame_state_is_zero(self) -> None:
        s = initial_frame_state()
        self.assertEqual(s.scanline, 0)
        self.assertEqual(s.frame_count, 0)
        self.assertTrue(s.in_visible_region)
        self.assertFalse(s.in_vblank)

    def test_in_vblank_predicate_boundary(self) -> None:
        self.assertFalse(FrameState(scanline=151, frame_count=0).in_vblank)
        self.assertTrue(FrameState(scanline=152, frame_count=0).in_vblank)
        self.assertTrue(FrameState(scanline=197, frame_count=0).in_vblank)

    def test_in_visible_region_predicate_boundary(self) -> None:
        self.assertTrue(
            FrameState(scanline=0, frame_count=0).in_visible_region,
        )
        self.assertTrue(
            FrameState(scanline=151, frame_count=0).in_visible_region,
        )
        self.assertFalse(
            FrameState(scanline=152, frame_count=0).in_visible_region,
        )


class AdvanceScanlinesTests(unittest.TestCase):
    def test_zero_is_identity(self) -> None:
        s = FrameState(scanline=42, frame_count=3)
        self.assertEqual(advance_scanlines(s, 0), s)

    def test_no_wrap_within_frame(self) -> None:
        s = advance_scanlines(initial_frame_state(), 50)
        self.assertEqual(s.scanline, 50)
        self.assertEqual(s.frame_count, 0)

    def test_enter_vblank_at_152(self) -> None:
        s = advance_scanlines(initial_frame_state(), 152)
        self.assertEqual(s.scanline, 152)
        self.assertTrue(s.in_vblank)

    def test_wrap_at_frame_boundary(self) -> None:
        s = advance_scanlines(initial_frame_state(), SCANLINES_PER_FRAME)
        # +198 wraps to scanline 0 of next frame.
        self.assertEqual(s.scanline, 0)
        self.assertEqual(s.frame_count, 1)

    def test_multi_frame_advance(self) -> None:
        # 3 frames + 5 scanlines = 3*198 + 5 = 599 scanlines.
        s = advance_scanlines(initial_frame_state(), 3 * SCANLINES_PER_FRAME + 5)
        self.assertEqual(s.scanline, 5)
        self.assertEqual(s.frame_count, 3)

    def test_negative_raises(self) -> None:
        with self.assertRaises(ValueError):
            advance_scanlines(initial_frame_state(), -1)

    def test_frame_count_wraps_at_2_to_32(self) -> None:
        # Right at the boundary so the carry hits the wrap mask.
        s = FrameState(scanline=0, frame_count=0xFFFFFFFF)
        new = advance_scanlines(s, SCANLINES_PER_FRAME)
        self.assertEqual(new.frame_count, 0)


class AdvanceFramesTests(unittest.TestCase):
    def test_zero_is_identity(self) -> None:
        s = FrameState(scanline=42, frame_count=3)
        self.assertEqual(advance_frames(s, 0), s)

    def test_advance_snaps_scanline_to_zero(self) -> None:
        s = advance_frames(FrameState(scanline=100, frame_count=0), 1)
        self.assertEqual(s.scanline, 0)
        self.assertEqual(s.frame_count, 1)

    def test_advance_multiple_frames(self) -> None:
        s = advance_frames(FrameState(scanline=50, frame_count=10), 5)
        self.assertEqual(s.scanline, 0)
        self.assertEqual(s.frame_count, 15)

    def test_negative_raises(self) -> None:
        with self.assertRaises(ValueError):
            advance_frames(initial_frame_state(), -1)


class VBlankTransitionsTests(unittest.TestCase):
    def test_no_transitions_within_visible_region(self) -> None:
        events = detect_vblank_transitions(initial_frame_state(), 100)
        self.assertEqual(events, ())

    def test_one_enter_transition(self) -> None:
        events = detect_vblank_transitions(initial_frame_state(), 152)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].kind, "enter")
        self.assertEqual(events[0].scanline, 152)
        self.assertEqual(events[0].frame_count, 0)

    def test_enter_then_leave_at_frame_boundary(self) -> None:
        # 0 → 200 crosses both VBlank boundaries: enter at scanline 152
        # of frame 0, leave at scanline 0 of frame 1.
        events = detect_vblank_transitions(initial_frame_state(), 200)
        self.assertEqual(len(events), 2)
        self.assertEqual(events[0].kind, "enter")
        self.assertEqual(events[0].scanline, 152)
        self.assertEqual(events[0].frame_count, 0)
        self.assertEqual(events[1].kind, "leave")
        self.assertEqual(events[1].scanline, 0)
        self.assertEqual(events[1].frame_count, 1)

    def test_two_full_frames_yield_two_pairs(self) -> None:
        events = detect_vblank_transitions(
            initial_frame_state(), 2 * SCANLINES_PER_FRAME,
        )
        # Two enter + two leave = 4 transitions total.
        kinds = [e.kind for e in events]
        self.assertEqual(kinds, ["enter", "leave", "enter", "leave"])

    def test_starting_inside_vblank_first_event_is_leave(self) -> None:
        events = detect_vblank_transitions(
            FrameState(scanline=160, frame_count=5), 50,
        )
        # 160 → 210 wraps to scanline 12 of frame 6; leave happens at
        # scanline 0 of frame 6.
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].kind, "leave")
        self.assertEqual(events[0].scanline, 0)
        self.assertEqual(events[0].frame_count, 6)


class SavestateV3RoundTripTests(unittest.TestCase):
    def test_round_trip_preserves_frame_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            rom_path = tmp / "demo.ngc"
            _write_demo_rom(rom_path)
            machine = load_machine_state(rom_path)

            custom_state = FrameState(scanline=170, frame_count=42)
            payload = build_savestate_payload(
                rom_path=rom_path,
                rom_header=machine.header,
                cpu=machine.cpu,
                writable_overlay={},
                frame_state=custom_state,
            )
            self.assertEqual(payload["format_version"], SAVESTATE_FORMAT_VERSION)
            self.assertEqual(payload["frame_state"]["scanline"], 170)
            self.assertEqual(payload["frame_state"]["frame_count"], 42)

            state_path = tmp / "save.state.json"
            save_savestate(state_path, payload)

            loaded = load_savestate(state_path, expected_rom_path=rom_path)
            self.assertEqual(loaded.frame_state.scanline, 170)
            self.assertEqual(loaded.frame_state.frame_count, 42)
            self.assertTrue(loaded.frame_state.in_vblank)

    def test_default_frame_state_is_initial(self) -> None:
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
            self.assertEqual(payload["frame_state"]["scanline"], 0)
            self.assertEqual(payload["frame_state"]["frame_count"], 0)

    def test_v2_savestate_loads_with_default_frame_state(self) -> None:
        """Backward-compat: a v2 save (no `frame_state` field) must
        load and surface `initial_frame_state()` so existing fixtures
        and saved sessions keep working."""
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
            # Downgrade to a v2 shape: drop frame_state, force version.
            payload["format_version"] = SAVESTATE_BACKWARD_COMPAT_VERSIONS[0]
            payload.pop("frame_state", None)
            state_path = tmp / "save.state.json"
            save_savestate(state_path, payload)

            loaded = load_savestate(state_path, expected_rom_path=rom_path)
            self.assertEqual(loaded.frame_state, initial_frame_state())


class TickFrameCliTests(unittest.TestCase):
    def test_default_advances_one_scanline(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            rom_path = tmp / "demo.ngc"
            _write_demo_rom(rom_path)

            stdout = io.StringIO()
            with redirect_stdout(stdout):
                exit_code = main(["tick-frame", str(rom_path), "--json"])
            self.assertEqual(exit_code, 0)
            payload = json.loads(stdout.getvalue())
            self.assertEqual(payload["before"]["scanline"], 0)
            self.assertEqual(payload["after"]["scanline"], 1)
            self.assertEqual(payload["after"]["frame_count"], 0)

    def test_advance_200_scanlines_crosses_vblank_and_frame(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            rom_path = tmp / "demo.ngc"
            _write_demo_rom(rom_path)

            stdout = io.StringIO()
            with redirect_stdout(stdout):
                exit_code = main(
                    ["tick-frame", str(rom_path), "--scanlines", "200", "--json"],
                )
            self.assertEqual(exit_code, 0)
            payload = json.loads(stdout.getvalue())
            # 200 scanlines from line 0: the frame is 199 lines, so we land on line 1
            # of the NEXT frame. (It was line 2 back when a frame was thought to be
            # 198 lines -- the silicon says 199.)
            self.assertEqual(payload["after"]["scanline"], 1)
            self.assertEqual(payload["after"]["frame_count"], 1)
            kinds = [t["kind"] for t in payload["vblank_transitions"]]
            self.assertEqual(kinds, ["enter", "leave"])

    def test_advance_frames_snaps_to_scanline_zero(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            rom_path = tmp / "demo.ngc"
            _write_demo_rom(rom_path)

            stdout = io.StringIO()
            with redirect_stdout(stdout):
                exit_code = main(
                    ["tick-frame", str(rom_path), "--frames", "3", "--json"],
                )
            self.assertEqual(exit_code, 0)
            payload = json.loads(stdout.getvalue())
            self.assertEqual(payload["after"]["scanline"], 0)
            self.assertEqual(payload["after"]["frame_count"], 3)
            # No VBlank transitions reported when advancing by frames
            # (the helper is scanline-based).
            self.assertEqual(payload["vblank_transitions"], [])

    def test_mutual_exclusion_scanlines_and_frames(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            rom_path = tmp / "demo.ngc"
            _write_demo_rom(rom_path)

            with redirect_stdout(io.StringIO()):
                exit_code = main(
                    [
                        "tick-frame", str(rom_path),
                        "--scanlines", "10", "--frames", "1",
                    ],
                )
            self.assertEqual(exit_code, 1)

    def test_negative_scanlines_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            rom_path = tmp / "demo.ngc"
            _write_demo_rom(rom_path)

            with redirect_stdout(io.StringIO()):
                exit_code = main(
                    ["tick-frame", str(rom_path), "--scanlines", "-5"],
                )
            self.assertEqual(exit_code, 1)

    def test_seed_from_carries_starting_frame_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            rom_path = tmp / "demo.ngc"
            _write_demo_rom(rom_path)
            machine = load_machine_state(rom_path)

            seed_path = tmp / "seed.state.json"
            save_savestate(
                seed_path,
                build_savestate_payload(
                    rom_path=rom_path,
                    rom_header=machine.header,
                    cpu=machine.cpu,
                    writable_overlay={},
                    frame_state=FrameState(scanline=100, frame_count=7),
                ),
            )

            stdout = io.StringIO()
            with redirect_stdout(stdout):
                exit_code = main(
                    [
                        "tick-frame", str(rom_path),
                        "--seed-from", str(seed_path),
                        "--scanlines", "50",
                        "--json",
                    ],
                )
            self.assertEqual(exit_code, 0)
            payload = json.loads(stdout.getvalue())
            self.assertEqual(payload["before"]["scanline"], 100)
            self.assertEqual(payload["before"]["frame_count"], 7)
            self.assertEqual(payload["after"]["scanline"], 150)
            self.assertEqual(payload["after"]["frame_count"], 7)

    def test_save_state_writes_v3_payload_with_frame_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            rom_path = tmp / "demo.ngc"
            _write_demo_rom(rom_path)
            out_path = tmp / "out.state.json"

            with redirect_stdout(io.StringIO()):
                exit_code = main(
                    [
                        "tick-frame", str(rom_path),
                        "--scanlines", "152",
                        "--save-state", str(out_path),
                    ],
                )
            self.assertEqual(exit_code, 0)
            self.assertTrue(out_path.exists())
            doc = load_savestate(out_path, expected_rom_path=rom_path)
            self.assertEqual(doc.format_version, SAVESTATE_FORMAT_VERSION)
            self.assertEqual(doc.frame_state.scanline, 152)
            self.assertTrue(doc.frame_state.in_vblank)


if __name__ == "__main__":
    unittest.main()
