"""M3 Phase 3.2.2a — VBlank IRQ pending state model + transition detection.

Phase 3.2.2a ships the `IrqState` dataclass + `fold_vblank_irq_pending`
helper + savestate v3 additive `irq_state` field + tick-frame
observability. Phase 3.2.2b will wire the pending state into the
executor (push PC/SR + jump to vector at 0x006FCC + iff_level
gating + RETI). Phase 3.2.2a is state-only.
"""

from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from core.frame_timing import (
    IRQ_LEVEL_VBLANK,
    VBLANK_VECTOR_ADDRESS,
    VISIBLE_SCANLINES,
    FrameState,
    IrqState,
    advance_scanlines,
    detect_vblank_transitions,
    fold_vblank_irq_pending,
    initial_frame_state,
    initial_irq_state,
)
from core.machine import load_machine_state
from core.savestate import build_savestate_payload, load_savestate, save_savestate
from ngpc_emu import main


def _write_demo_rom(path: Path, entry_point: int = 0x00200040) -> None:
    data = bytearray(0x40)
    data[0x00:0x1C] = b"LICENSED BY SNK CORPORATION".ljust(0x1C, b"\x00")
    data[0x1C:0x20] = entry_point.to_bytes(4, "little")
    data[0x22] = 0
    data[0x23] = 0x10
    data[0x24:0x30] = b"IRQ 3.2.2A\x00\x00"
    path.write_bytes(bytes(data))


class IrqConstantsLockTests(unittest.TestCase):
    def test_vblank_level_is_4(self) -> None:
        # Official SNK SDK (`01_SDK/docs/SysPro.txt`, USER PROGRAM INTERRUPT
        # OPERATION VECTOR): "It is forbidden to prohibit Vertical Blanking
        # Interrupt (Interrupt level 4)". The reference emulator agrees (it gates
        # VBlank on `statusIFF() <= 4`, which under the Toshiba rule "level L is
        # accepted when L >= IFF" is exactly level 4).
        #
        # This had been raised to 6 on 2026-07-03 by INFERENCE from the BIOS's
        # `ei 5; halt`, but that inference rested on our mask gate, which was
        # itself off by one. Two documented sources beat one inference built on a
        # bug -- see frame_timing.py for the full reasoning.
        self.assertEqual(IRQ_LEVEL_VBLANK, 4)

    def test_vblank_vector_address(self) -> None:
        # Per HW QuickRef § 4 — BIOS installs the VBlank vector JMP at
        # 0x006FCC in the system RAM page.
        self.assertEqual(VBLANK_VECTOR_ADDRESS, 0x006FCC)


class IrqStateBasicsTests(unittest.TestCase):
    def test_initial_is_empty(self) -> None:
        s = initial_irq_state()
        self.assertEqual(s.pending_mask, 0)
        self.assertFalse(s.is_vblank_pending())

    def test_with_vblank_pending_sets_bit_4(self) -> None:
        s = initial_irq_state().with_vblank_pending()
        self.assertEqual(s.pending_mask, 1 << IRQ_LEVEL_VBLANK)
        self.assertEqual(s.pending_mask, 0x10)  # VBlank = level 4 (SDK)
        self.assertTrue(s.is_vblank_pending())

    def test_with_vblank_cleared_keeps_other_bits(self) -> None:
        # Set the VBlank bit (level 6) + bit 2 (a placeholder for a future
        # IRQ source), then clear VBlank and confirm bit 2 survives.
        seed = IrqState(pending_mask=(1 << IRQ_LEVEL_VBLANK) | 0x04)
        self.assertTrue(seed.is_vblank_pending())
        cleared = seed.with_vblank_cleared()
        # VBlank bit cleared, bit 2 preserved.
        self.assertFalse(cleared.is_vblank_pending())
        self.assertEqual(cleared.pending_mask, 0x04)

    def test_idempotent_set(self) -> None:
        # Setting twice doesn't double-bit or break.
        once = initial_irq_state().with_vblank_pending()
        twice = once.with_vblank_pending()
        self.assertEqual(once, twice)


class FoldVblankIrqPendingTests(unittest.TestCase):
    def test_no_transitions_returns_state_unchanged(self) -> None:
        seed = IrqState(pending_mask=0x02)
        out = fold_vblank_irq_pending(seed, ())
        self.assertEqual(out, seed)

    def test_one_enter_transition_sets_vblank(self) -> None:
        before = initial_frame_state()
        transitions = detect_vblank_transitions(before, VISIBLE_SCANLINES)
        # One enter event at scanline 152.
        self.assertEqual(len(transitions), 1)
        self.assertEqual(transitions[0].kind, "enter")
        out = fold_vblank_irq_pending(initial_irq_state(), transitions)
        self.assertTrue(out.is_vblank_pending())

    def test_leave_transition_does_not_clear(self) -> None:
        # Cross both visible→VBlank and VBlank→next-frame in one advance.
        before = initial_frame_state()
        transitions = detect_vblank_transitions(before, 200)
        kinds = [t.kind for t in transitions]
        self.assertEqual(kinds, ["enter", "leave"])
        # After enter+leave, pending is still set (Phase 3.2.2a never
        # clears — executor clears on delivery in Phase 3.2.2b).
        out = fold_vblank_irq_pending(initial_irq_state(), transitions)
        self.assertTrue(out.is_vblank_pending())

    def test_already_pending_remains_pending(self) -> None:
        seed = initial_irq_state().with_vblank_pending()
        transitions = detect_vblank_transitions(
            initial_frame_state(), VISIBLE_SCANLINES,
        )
        out = fold_vblank_irq_pending(seed, transitions)
        self.assertEqual(out, seed)


class SavestateV3IrqStateTests(unittest.TestCase):
    def test_default_irq_state_in_payload_is_empty(self) -> None:
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
            self.assertEqual(payload["irq_state"]["pending_mask"], 0)

    def test_round_trip_preserves_pending_mask(self) -> None:
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
                irq_state=IrqState(pending_mask=0x10),
            )
            state_path = tmp / "save.state.json"
            save_savestate(state_path, payload)
            loaded = load_savestate(state_path, expected_rom_path=rom_path)
            self.assertEqual(loaded.irq_state.pending_mask, 0x10)
            self.assertTrue(loaded.irq_state.is_vblank_pending())

    def test_v2_save_loads_with_initial_irq_state(self) -> None:
        # Backward compat: a savestate missing `irq_state` defaults to
        # `initial_irq_state()` (no pending IRQs).
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
            # Drop irq_state to simulate a v3-pre-3.2.2a payload.
            payload.pop("irq_state", None)
            state_path = tmp / "save.state.json"
            save_savestate(state_path, payload)
            loaded = load_savestate(state_path, expected_rom_path=rom_path)
            self.assertEqual(loaded.irq_state, initial_irq_state())


class TickFrameIrqObservabilityTests(unittest.TestCase):
    def test_advance_to_vblank_sets_pending_mask_in_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            rom_path = tmp / "demo.ngc"
            _write_demo_rom(rom_path)

            stdout = io.StringIO()
            with redirect_stdout(stdout):
                exit_code = main(
                    [
                        "tick-frame", str(rom_path),
                        "--scanlines", "160", "--json",
                    ],
                )
            self.assertEqual(exit_code, 0)
            payload = json.loads(stdout.getvalue())
            self.assertEqual(payload["irq_before"]["pending_mask"], 0)
            self.assertEqual(payload["irq_after"]["pending_mask"], 0x10)
            self.assertTrue(payload["irq_after"]["vblank_pending"])
            self.assertEqual(payload["constants"]["vblank_irq_level"], 4)

    def test_advance_within_visible_region_keeps_irq_clear(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            rom_path = tmp / "demo.ngc"
            _write_demo_rom(rom_path)

            stdout = io.StringIO()
            with redirect_stdout(stdout):
                main([
                    "tick-frame", str(rom_path),
                    "--scanlines", "100", "--json",
                ])
            payload = json.loads(stdout.getvalue())
            self.assertEqual(payload["irq_after"]["pending_mask"], 0)

    def test_seed_carries_pending_mask_into_tick_frame(self) -> None:
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
                    frame_state=FrameState(scanline=160, frame_count=0),
                    irq_state=IrqState(pending_mask=0x10),
                ),
            )

            stdout = io.StringIO()
            with redirect_stdout(stdout):
                main([
                    "tick-frame", str(rom_path),
                    "--seed-from", str(seed_path),
                    "--scanlines", "10", "--json",
                ])
            payload = json.loads(stdout.getvalue())
            # Already pending coming in; advancement of 10 scanlines
            # within VBlank doesn't cross another enter boundary.
            self.assertEqual(payload["irq_before"]["pending_mask"], 0x10)
            self.assertEqual(payload["irq_after"]["pending_mask"], 0x10)

    def test_advance_across_full_frame_sets_pending_once(self) -> None:
        # 0 → 198 crosses enter(152) + leave(0 of next frame). Phase
        # 3.2.2a sets pending on enter and never clears, so after a
        # full-frame advance the bit remains set.
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            rom_path = tmp / "demo.ngc"
            _write_demo_rom(rom_path)

            stdout = io.StringIO()
            with redirect_stdout(stdout):
                main([
                    "tick-frame", str(rom_path),
                    "--scanlines", "198", "--json",
                ])
            payload = json.loads(stdout.getvalue())
            self.assertTrue(payload["irq_after"]["vblank_pending"])


if __name__ == "__main__":
    unittest.main()
