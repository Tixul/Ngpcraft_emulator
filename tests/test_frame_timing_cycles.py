"""M3 Phase 3.2.0 + 3.2.1 — cycle estimate + frame_state advancement
during execution.

Phase 3.2.0 ships the cycle constants (`ESTIMATED_CYCLES_PER_INSTRUCTION`,
`CYCLES_PER_SCANLINE`) and the conversion helpers. Phase 3.2.1 plumbs
the advancement through the CLI handlers that emit savestates so
chained `step-exec` / `run-steps` / `run-until-exec` / etc. produce
output `frame_state` advanced by the executed cycle count.

The cycle estimate is the documented Phase 3.2.0 placeholder (flat
8 cycles per executed instruction). Phase 3.2.3 will replace it
with a per-opcode TLCS-900 cycle table.
"""

from __future__ import annotations

import io
import unittest
from contextlib import redirect_stdout
from pathlib import Path
import tempfile

from core.frame_timing import (
    CYCLES_PER_SCANLINE,
    ESTIMATED_CYCLES_PER_INSTRUCTION,
    SCANLINES_PER_FRAME,
    FrameState,
    advance_frame_state_by_cycles,
    initial_frame_state,
    scanlines_elapsed_from_cycles,
)
from core.machine import load_machine_state
from core.savestate import build_savestate_payload, load_savestate, save_savestate
from ngpc_emu import _advance_frame_state_for_run, main


def _write_demo_rom(path: Path, entry_point: int = 0x00200040) -> None:
    data = bytearray(0x40)
    data[0x00:0x1C] = b"LICENSED BY SNK CORPORATION".ljust(0x1C, b"\x00")
    data[0x1C:0x20] = entry_point.to_bytes(4, "little")
    data[0x22] = 0
    data[0x23] = 0x10
    data[0x24:0x30] = b"FRAME 3.2A\x00\x00"
    path.write_bytes(bytes(data))


class CycleConstantsLockTests(unittest.TestCase):
    """Lock the Phase 3.2.0 constants. Future refactors changing the
    cycle estimate or scanline budget MUST update these tests."""

    def test_estimated_cycles_per_instruction_is_8(self) -> None:
        # Placeholder value pending the per-opcode TLCS-900 table
        # (Phase 3.2.3). Documented as non-reference-mode in
        # HARDWARE_COMPAT_POLICY.md.
        self.assertEqual(ESTIMATED_CYCLES_PER_INSTRUCTION, 8)

    def test_cycles_per_scanline_is_515(self) -> None:
        """515 is what the manufacturer WRITES. 517 was inferred, and was wrong.

        K2GETechRef § 4-8: "the 10 bit internal subtraction counter of the
        horizontal drawing operation time (**internally 515 clock**)".

        This test used to lock 517, derived by assuming a 60.00 Hz frame and
        solving 6_144_000 / (60 * 198). Nothing documents 60.00 -- it is an
        OUTPUT of the timing, not an input. With 515 the refresh is 60.25 Hz,
        and our frame counter lines up exactly with the reference emulator's
        (with 517 it ran a frame behind by frame ~1869 of Sonic's demo).
        """
        self.assertEqual(CYCLES_PER_SCANLINE, 515)

    def test_one_scanline_costs_about_64_instructions(self) -> None:
        # Sanity: at 8 cycles/instr and 517 cycles/scanline, one
        # scanline boundary advances after ~64 instructions.
        approx = CYCLES_PER_SCANLINE // ESTIMATED_CYCLES_PER_INSTRUCTION
        self.assertEqual(approx, 64)


class ScanlinesElapsedHelperTests(unittest.TestCase):
    def test_zero_cycles_yields_zero_scanlines(self) -> None:
        self.assertEqual(scanlines_elapsed_from_cycles(0), 0)

    def test_under_one_scanline_yields_zero(self) -> None:
        self.assertEqual(scanlines_elapsed_from_cycles(CYCLES_PER_SCANLINE - 1), 0)

    def test_exactly_one_scanline(self) -> None:
        self.assertEqual(scanlines_elapsed_from_cycles(CYCLES_PER_SCANLINE), 1)

    def test_two_scanlines(self) -> None:
        self.assertEqual(scanlines_elapsed_from_cycles(2 * CYCLES_PER_SCANLINE), 2)

    def test_full_frame(self) -> None:
        # 198 × 517 cycles = one full frame.
        self.assertEqual(
            scanlines_elapsed_from_cycles(SCANLINES_PER_FRAME * CYCLES_PER_SCANLINE),
            SCANLINES_PER_FRAME,
        )

    def test_negative_raises(self) -> None:
        with self.assertRaises(ValueError):
            scanlines_elapsed_from_cycles(-1)


class AdvanceFrameStateByCyclesTests(unittest.TestCase):
    def test_zero_cycles_is_identity(self) -> None:
        s = FrameState(scanline=100, frame_count=5)
        self.assertEqual(advance_frame_state_by_cycles(s, 0), s)

    def test_one_scanline_advances_scanline_only(self) -> None:
        s = advance_frame_state_by_cycles(
            FrameState(scanline=100, frame_count=0), CYCLES_PER_SCANLINE,
        )
        self.assertEqual(s.scanline, 101)
        self.assertEqual(s.frame_count, 0)

    def test_full_frame_increments_frame_count(self) -> None:
        s = advance_frame_state_by_cycles(
            FrameState(scanline=0, frame_count=3),
            SCANLINES_PER_FRAME * CYCLES_PER_SCANLINE,
        )
        self.assertEqual(s.scanline, 0)
        self.assertEqual(s.frame_count, 4)

    def test_cross_vblank_boundary(self) -> None:
        # Start at scanline 150 (visible), advance enough cycles to
        # cross into VBlank (152).
        s = advance_frame_state_by_cycles(
            FrameState(scanline=150, frame_count=0),
            2 * CYCLES_PER_SCANLINE,  # advances 2 scanlines
        )
        self.assertEqual(s.scanline, 152)
        self.assertTrue(s.in_vblank)


class CliBoundaryAdvancementTests(unittest.TestCase):
    """`_advance_frame_state_for_run(seed, executed_count)` is the
    helper the CLI handlers call at the save-state boundary."""

    def test_none_seed_defaults_to_initial(self) -> None:
        out = _advance_frame_state_for_run(None, executed_count=0)
        self.assertEqual(out, initial_frame_state())

    def test_zero_executed_count_returns_seed_unchanged(self) -> None:
        seed = FrameState(scanline=100, frame_count=2)
        self.assertEqual(
            _advance_frame_state_for_run(seed, executed_count=0), seed,
        )

    def test_single_instruction_doesnt_cross_scanline(self) -> None:
        # 1 instr × 8 cycles = 8 cycles, well below the 517-cycle scanline.
        seed = FrameState(scanline=100, frame_count=0)
        out = _advance_frame_state_for_run(seed, executed_count=1)
        self.assertEqual(out.scanline, 100)

    def test_64_instructions_advance_one_scanline(self) -> None:
        # 64 instr × 8 = 512 cycles, just below the threshold.
        seed = FrameState(scanline=100, frame_count=0)
        out_64 = _advance_frame_state_for_run(seed, executed_count=64)
        self.assertEqual(out_64.scanline, 100)
        # 65 instr × 8 = 520 cycles, crosses the boundary.
        out_65 = _advance_frame_state_for_run(seed, executed_count=65)
        self.assertEqual(out_65.scanline, 101)

    def test_advancement_crosses_into_vblank(self) -> None:
        seed = FrameState(scanline=150, frame_count=0)
        # 200 instr × 8 = 1600 cycles → 3 scanlines elapsed.
        out = _advance_frame_state_for_run(seed, executed_count=200)
        self.assertEqual(out.scanline, 153)
        self.assertTrue(out.in_vblank)

    def test_advancement_from_no_seed_starts_at_initial(self) -> None:
        # Bootstrap run with no seed → starting at FrameState(0, 0).
        out = _advance_frame_state_for_run(None, executed_count=130)
        # 130 × 8 = 1040 → 2 scanlines.
        self.assertEqual(out.scanline, 2)
        self.assertEqual(out.frame_count, 0)


class StepExecAdvancesFrameStateTests(unittest.TestCase):
    """End-to-end: step-exec without seed advances frame_state in the
    output savestate (the bootstrap demo ROM at 0x200040 produces an
    `executed_count = 0` honest stop, but the helper still emits the
    initial frame_state, not None). This test pins the contract:
    the output savestate always carries a frame_state field."""

    def test_step_exec_emits_initial_frame_state_on_zero_count(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            rom_path = tmp / "demo.ngc"
            _write_demo_rom(rom_path)
            out_path = tmp / "after.state.json"

            with redirect_stdout(io.StringIO()):
                code = main(
                    ["step-exec", str(rom_path),
                     "--save-state", str(out_path)],
                )
            self.assertEqual(code, 0)
            doc = load_savestate(out_path, expected_rom_path=rom_path)
            # With executed_count = 0 (bootstrap ROM stops immediately),
            # advancement is 0 → output = initial_frame_state().
            self.assertEqual(doc.frame_state, initial_frame_state())

    def test_step_exec_with_seed_advances_or_preserves(self) -> None:
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
                    frame_state=FrameState(scanline=170, frame_count=4),
                ),
            )
            out_path = tmp / "after.state.json"

            with redirect_stdout(io.StringIO()):
                code = main([
                    "step-exec", str(rom_path),
                    "--seed-from", str(seed_path),
                    "--save-state", str(out_path),
                ])
            self.assertEqual(code, 0)
            doc = load_savestate(out_path, expected_rom_path=rom_path)
            # ROM stops at executed_count 0 → no advancement → seed verbatim.
            self.assertEqual(doc.frame_state.scanline, 170)
            self.assertEqual(doc.frame_state.frame_count, 4)


if __name__ == "__main__":
    unittest.main()
