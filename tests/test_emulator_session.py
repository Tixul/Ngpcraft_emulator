"""Stateful emulator session tests — covers the UI-facing state machine.

`EmulatorSession` is the live-state wrapper around the existing batch
executor primitives. The Tkinter UI sits on top, but the session's
behavior is independent of any display and fully testable here.

Covers:
- reset / step / load_savestate / save_savestate happy paths
- snapshot is read-only (mutations don't bleed back)
- frame_state advances per cycle after each step
- VBlank IRQ pending bit gets folded automatically when an advance
  crosses scanline 152
- render_lcd_ppm returns valid P6 PPM bytes for a 160×152 frame
"""

from __future__ import annotations

import io
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from core.breakpoints import breakpoints_path_for_rom
from core.cpu import StatusFlags, create_unknown_control_registers
from core.execute import NOP_CYCLES
from core.emulator_session import EmulatorSession, SessionSnapshot
from core.frame_timing import (
    CYCLES_PER_SCANLINE,
    IrqState,
    VISIBLE_SCANLINES,
)
from core.watchpoints import watchpoints_path_for_rom


def _write_demo_rom(path: Path, body: bytes = b"\x00", entry_point: int = 0x00200040) -> None:
    data = bytearray(0x50)
    data[0x00:0x1C] = b"LICENSED BY SNK CORPORATION".ljust(0x1C, b"\x00")
    data[0x1C:0x20] = entry_point.to_bytes(4, "little")
    data[0x23] = 0x10
    data[0x24:0x30] = b"UI MVP\x00\x00\x00\x00\x00\x00"
    body_offset = entry_point - 0x00200000
    data[body_offset : body_offset + len(body)] = body
    path.write_bytes(bytes(data))


def _write_demo_bios(path: Path, *, patches: dict[int, bytes] | None = None) -> None:
    data = bytearray(0x10000)
    if patches:
        for offset, chunk in patches.items():
            data[offset : offset + len(chunk)] = chunk
    path.write_bytes(bytes(data))


class SessionHaltWakeTests(unittest.TestCase):
    def _halt_ready_session(self, rom: Path, iff_level: int) -> EmulatorSession:
        _write_demo_rom(rom)
        session = EmulatorSession(rom)
        # Install a VBlank handler pointer at 0x6FCC and put the CPU in a
        # halt-wait state with a known SR shape + valid stack.
        handler = 0x00203000
        for i in range(4):
            session.memory[0x006FCC + i] = (handler >> (8 * i)) & 0xFF
        session.cpu = replace(
            session.cpu,
            pc=0x00201234,
            regs=replace(session.cpu.regs, xsp=0x00006C00),
            flags=StatusFlags(sf=False, zf=False, vf=False, hf=False, cf=False, nf=False),
            iff_level=iff_level,
            rfp=0,
        )
        return session

    def test_wake_halt_via_vblank_delivers_to_installed_handler(self) -> None:
        # Models the real SNK BIOS boot: `ei 5; halt` -> VBlank (level 6)
        # wakes the CPU into the handler pointed to by 0x6FCC.
        with tempfile.TemporaryDirectory() as tmpdir:
            # VBlank is level 4 (SDK): iff_level must be <= 4 to accept it.
            session = self._halt_ready_session(Path(tmpdir) / "demo.ngc", iff_level=4)

            woke = session.wake_halt_via_vblank()

            self.assertTrue(woke)
            self.assertEqual(session.cpu.pc, 0x00203000)            # jumped to handler
            self.assertEqual(session.cpu.regs.xsp, 0x00006BFA)      # pushed PC(4)+SR(2)
            # Toshiba manual: mask set to level+1 on acceptance (4 -> 5).
            self.assertEqual(session.cpu.iff_level, 5)
            self.assertFalse(session.irq_state.is_vblank_pending())  # pending cleared

    def test_wake_halt_via_vblank_stays_masked_when_iff_is_7(self) -> None:
        # Toshiba manual: `111` = "level 7 only (non-maskable)", so iff_level=7
        # is the ONLY value that masks a level-6 VBlank -- a genuinely stuck halt.
        # (iff_level=6 means "level 6 or higher" and DOES take the VBlank.)
        with tempfile.TemporaryDirectory() as tmpdir:
            session = self._halt_ready_session(Path(tmpdir) / "demo.ngc", iff_level=7)

            woke = session.wake_halt_via_vblank()

            self.assertFalse(woke)
            self.assertEqual(session.cpu.pc, 0x00201234)            # unchanged
            self.assertTrue(session.irq_state.is_vblank_pending())   # still pending


class SessionAutoWakeHaltTests(unittest.TestCase):
    """`step` auto-wakes a HALT on VBlank so the boot self-drives.

    On real hardware HALT parks the CPU until the video clock raises
    VBlank, which wakes it into the 0x6FCC handler. These tests use a
    synthetic HALT-at-entry ROM + a RAM handler (cold-start 0x00 = NOP)
    so the behavior is checked deterministically, independent of the
    real BIOS timing loops.
    """

    HALT = b"\x05"
    HANDLER = 0x00005000  # Work RAM (cold-start 0x00 = NOP), readable

    def _halt_at_entry_session(
        self, rom: Path, *, iff_level: int, auto_wake: bool = True,
    ) -> EmulatorSession:
        _write_demo_rom(rom, body=self.HALT)
        session = EmulatorSession(rom, auto_wake_on_halt=auto_wake)
        # Install the VBlank handler pointer at 0x6FCC (BIOS convention).
        for i in range(4):
            session.memory[0x006FCC + i] = (self.HANDLER >> (8 * i)) & 0xFF
        # VBlank is level 4 (SDK), and the Toshiba rule accepts an interrupt of
        # level L when L >= IFF -- so iff_level must be <= 4 for the halt to wake.
        session.cpu = replace(session.cpu, iff_level=iff_level)
        return session

    def test_step_auto_wakes_halt_into_handler(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            session = self._halt_at_entry_session(
                Path(tmpdir) / "demo.ngc", iff_level=4,
            )

            session.step(20)

            # The HALT did not freeze the run: it woke on VBlank and is
            # now running NOPs inside the handler region.
            self.assertGreaterEqual(session.last_auto_wakes, 1)
            self.assertNotEqual(session.last_stop_reason, "stopped-on-cpu-halted")
            self.assertGreaterEqual(session.cpu.pc, self.HANDLER)
            # Mask raised to VBlank level + 1 on acceptance (Toshiba manual): 4 -> 5.
            self.assertEqual(session.cpu.iff_level, 5)

    def test_step_without_auto_wake_stops_at_halt(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            session = self._halt_at_entry_session(
                Path(tmpdir) / "demo.ngc", iff_level=5, auto_wake=False,
            )

            session.step(20)

            self.assertEqual(session.last_stop_reason, "stopped-on-cpu-halted")
            self.assertEqual(session.last_auto_wakes, 0)
            self.assertEqual(session.cpu.pc, 0x00200041)  # PC past the HALT

    def test_step_stuck_halt_stops_even_with_auto_wake(self) -> None:
        # iff_level=7 ("level 7 only") masks the level-6 VBlank: a genuinely stuck
        # halt must still stop honestly, not spin forever.
        with tempfile.TemporaryDirectory() as tmpdir:
            session = self._halt_at_entry_session(
                Path(tmpdir) / "demo.ngc", iff_level=7,
            )

            session.step(20)

            self.assertEqual(session.last_stop_reason, "stopped-on-cpu-halted")
            self.assertEqual(session.last_auto_wakes, 0)


class SessionResetTests(unittest.TestCase):
    def test_fresh_session_matches_bootstrap(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom = Path(tmpdir) / "demo.ngc"
            _write_demo_rom(rom)
            session = EmulatorSession(rom)
            snap = session.snapshot()
            self.assertEqual(snap.cpu.pc, 0x00200040)
            self.assertEqual(snap.frame_state.scanline, 0)
            self.assertEqual(snap.frame_state.frame_count, 0)
            self.assertEqual(snap.irq_state.pending_mask, 0)
            self.assertEqual(snap.total_cycles_consumed, 0)
            self.assertIsNone(snap.last_stop_reason)

    def test_reset_after_step_clears_telemetry(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom = Path(tmpdir) / "demo.ngc"
            _write_demo_rom(rom)
            session = EmulatorSession(rom)
            session.step(3)
            self.assertGreater(session.total_cycles_consumed, 0)
            session.reset()
            snap = session.snapshot()
            self.assertEqual(snap.total_cycles_consumed, 0)
            self.assertEqual(snap.cpu.pc, 0x00200040)
            self.assertIsNone(snap.last_stop_reason)


class SessionStepTests(unittest.TestCase):
    def test_step_one_nop_advances_pc_by_one_and_consumes_cycles(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom = Path(tmpdir) / "demo.ngc"
            _write_demo_rom(rom, b"\x00")  # NOP at entry
            session = EmulatorSession(rom)
            result = session.step(1)
            self.assertEqual(result.executed_count, 1)
            self.assertEqual(result.stop_reason, "count-reached")
            snap = session.snapshot()
            self.assertEqual(snap.cpu.pc, 0x00200041)
            self.assertEqual(
                snap.total_cycles_consumed,
                NOP_CYCLES,
            )
            self.assertEqual(snap.last_executed_count, 1)
            self.assertEqual(snap.last_stop_reason, "count-reached")

    def test_step_rejects_zero_or_negative_count(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom = Path(tmpdir) / "demo.ngc"
            _write_demo_rom(rom)
            session = EmulatorSession(rom)
            with self.assertRaises(ValueError):
                session.step(0)
            with self.assertRaises(ValueError):
                session.step(-1)

    def test_step_accumulates_total_cycles_across_calls(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom = Path(tmpdir) / "demo.ngc"
            _write_demo_rom(rom, b"\x00\x00\x00\x00\x00")
            session = EmulatorSession(rom)
            session.step(2)
            session.step(3)
            snap = session.snapshot()
            self.assertEqual(
                snap.total_cycles_consumed,
                5 * NOP_CYCLES,
            )


class SessionFrameStateTests(unittest.TestCase):
    def test_step_advances_frame_state_via_cycles(self) -> None:
        # 259 NOPs × 2 cycles = 518 cycles > 517 = 1 scanline boundary.
        with tempfile.TemporaryDirectory() as tmpdir:
            rom = Path(tmpdir) / "demo.ngc"
            _write_demo_rom(rom, b"\x00" * 300)
            session = EmulatorSession(rom)
            session.step(259)
            snap = session.snapshot()
            self.assertEqual(snap.frame_state.scanline, 1)
            self.assertEqual(snap.frame_state.frame_count, 0)

    def test_cycle_residue_accumulates_across_small_batches(self) -> None:
        # 259 single-instruction steps (2 cycles each = 518 total) must
        # still cross 1 scanline boundary. Without residue tracking,
        # each batch's 2 cycles // 517 = 0 and the scanline never moves.
        with tempfile.TemporaryDirectory() as tmpdir:
            rom = Path(tmpdir) / "demo.ngc"
            _write_demo_rom(rom, b"\x00" * 300)
            session = EmulatorSession(rom)
            for _ in range(259):
                session.step(1)
            snap = session.snapshot()
            self.assertEqual(snap.frame_state.scanline, 1)

    def test_step_folds_vblank_pending_on_scanline_152_crossing(self) -> None:
        # Need enough cycles to cross 152 scanlines.
        # 152 scanlines × 517 cycles = 78,584 cycles
        # / 2 cycles per NOP = 39,292 instructions.
        # Use a smaller jump via savestate seeding instead.
        with tempfile.TemporaryDirectory() as tmpdir:
            rom = Path(tmpdir) / "demo.ngc"
            _write_demo_rom(rom, b"\x00" * 300)
            session = EmulatorSession(rom)
            # Manually advance frame_state to just before scanline 152.
            from core.frame_timing import FrameState
            session.frame_state = FrameState(scanline=151, frame_count=0)
            # 1 scanline × 517 cycles ≈ 259 NOPs to cross into VBlank.
            session.step(259)
            snap = session.snapshot()
            self.assertTrue(snap.frame_state.in_vblank)
            self.assertTrue(snap.irq_state.is_vblank_pending())


class SessionSavestateTests(unittest.TestCase):
    def test_save_then_load_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom = Path(tmpdir) / "demo.ngc"
            _write_demo_rom(rom, b"\x00\x00\x00")
            session = EmulatorSession(rom)
            session.step(2)
            saved = Path(tmpdir) / "save.json"
            session.save_savestate(saved, note="round-trip test")

            other = EmulatorSession(rom)
            other.load_savestate(saved)
            snap = other.snapshot()
            self.assertEqual(snap.cpu.pc, 0x00200042)
            # Counters reset to zero on load (the loaded state is the
            # new "zero point" for the live session).
            self.assertEqual(snap.total_cycles_consumed, 0)
            self.assertEqual(snap.last_stop_reason, "loaded-savestate")

    def test_load_savestate_carries_irq_pending(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom = Path(tmpdir) / "demo.ngc"
            _write_demo_rom(rom)
            session = EmulatorSession(rom)
            # Manually inject pending IRQ + save.
            session.irq_state = IrqState(pending_mask=0x10)  # VBlank = level 4
            saved = Path(tmpdir) / "save.json"
            session.save_savestate(saved)

            other = EmulatorSession(rom)
            other.load_savestate(saved)
            self.assertTrue(other.snapshot().irq_state.is_vblank_pending())


class SessionRenderTests(unittest.TestCase):
    def test_render_lcd_returns_valid_ppm_p6_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom = Path(tmpdir) / "demo.ngc"
            _write_demo_rom(rom)
            session = EmulatorSession(rom)
            ppm = session.render_lcd_ppm()
            # P6 header: magic + width + height + maxval + body
            self.assertTrue(ppm.startswith(b"P6"))
            # Body must be exactly 160 * 152 * 3 = 72,960 bytes RGB.
            # Header is a few dozen bytes — total ~72,975-73,000.
            self.assertGreater(len(ppm), 160 * 152 * 3)
            self.assertLess(len(ppm), 160 * 152 * 3 + 64)

    def test_snapshot_is_decoupled_from_session_mutations(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom = Path(tmpdir) / "demo.ngc"
            _write_demo_rom(rom)
            session = EmulatorSession(rom)
            snap = session.snapshot()
            session.step(1)
            # The snapshot's memory dict isn't mutated by the later step.
            self.assertNotEqual(
                snap.cpu.pc, session.snapshot().cpu.pc,
            )


class SessionK2geInspectorTests(unittest.TestCase):
    def test_merged_memory_view_keeps_builtin_and_overlay_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom = Path(tmpdir) / "demo.ngc"
            _write_demo_rom(rom)
            session = EmulatorSession(rom)
            # Overlay one sprite palette entry with pure red.
            session.memory[0x008200] = 0x0F
            session.memory[0x008201] = 0x00

            merged = session.build_merged_memory_view()
            self.assertEqual(merged[0x008200], 0x0F)
            self.assertEqual(merged[0x008201], 0x00)
            # Builtin reset values still come through when untouched.
            self.assertEqual(merged[0x008004], 0xFF)
            self.assertEqual(merged[0x008005], 0xFF)

            palettes = session.read_k2ge_palettes()
            self.assertEqual(
                palettes["sprite"][0].colors[0].hex_rgb24(), "#FF0000",
            )

    def test_read_k2ge_oam_visible_only_filters_hidden_sprites(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom = Path(tmpdir) / "demo.ngc"
            _write_demo_rom(rom)
            session = EmulatorSession(rom)
            # Sprite #0 visible: tile=1, PR.C=front, position (10, 20), palette 2.
            session.memory[0x008800] = 0x01
            session.memory[0x008801] = 0x18
            session.memory[0x008802] = 10
            session.memory[0x008803] = 20
            session.memory[0x008C00] = 0x02

            all_sprites = session.read_k2ge_oam_sprites()
            visible = session.read_k2ge_oam_sprites(visible_only=True)
            self.assertEqual(len(all_sprites), 64)
            self.assertEqual(len(visible), 1)
            self.assertEqual(visible[0].index, 0)
            self.assertEqual(visible[0].c_c, 1)
            self.assertEqual(visible[0].cp_c, 2)


class SessionStepUntilFrameAdvanceTests(unittest.TestCase):
    def test_advance_from_scanline_zero_reaches_next_frame(self) -> None:
        # 198 scanlines × 517 cycles ≈ 102,366 cycles = ~51,183 NOPs.
        # Use an explicit budget above that threshold.
        with tempfile.TemporaryDirectory() as tmpdir:
            rom = Path(tmpdir) / "demo.ngc"
            _write_demo_rom(rom, b"\x00" * 52000)
            session = EmulatorSession(rom)
            self.assertEqual(session.frame_state.frame_count, 0)
            executed = session.step_until_frame_advance(max_steps=60_000)
            self.assertGreater(executed, 0)
            self.assertEqual(session.frame_state.frame_count, 1)

    def test_advance_from_mid_frame_completes_in_fewer_steps(self) -> None:
        # Seed at the LAST scanline (198 -- the frame is 199 lines, 0..198, as
        # measured on silicon), so only one scanline remains before the wrap.
        # Use a small batch so the loop checks frame_count near the
        # actual boundary instead of overshooting by a full default
        # batch.
        with tempfile.TemporaryDirectory() as tmpdir:
            rom = Path(tmpdir) / "demo.ngc"
            _write_demo_rom(rom, b"\x00" * 300)
            session = EmulatorSession(rom)
            from core.frame_timing import FrameState
            session.frame_state = FrameState(scanline=198, frame_count=0)
            executed = session.step_until_frame_advance(batch=10)
            # 515 cycles / 2 cycles ≈ 258 instructions to wrap.
            # batch=10 means we may overshoot by up to 9 ; budget ~270.
            self.assertLess(executed, 300)
            self.assertEqual(session.frame_state.frame_count, 1)

    def test_rejects_invalid_batch_or_max_steps(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom = Path(tmpdir) / "demo.ngc"
            _write_demo_rom(rom)
            session = EmulatorSession(rom)
            with self.assertRaises(ValueError):
                session.step_until_frame_advance(batch=0)
            with self.assertRaises(ValueError):
                session.step_until_frame_advance(max_steps=0)

    def test_exhausts_max_steps_without_crashing(self) -> None:
        # max_steps too small to reach the next frame — should
        # return gracefully.
        with tempfile.TemporaryDirectory() as tmpdir:
            rom = Path(tmpdir) / "demo.ngc"
            _write_demo_rom(rom, b"\x00" * 300)
            session = EmulatorSession(rom)
            executed = session.step_until_frame_advance(max_steps=50)
            # Frame didn't advance ; executed up to budget.
            self.assertEqual(session.frame_state.frame_count, 0)
            self.assertGreater(executed, 0)


class SessionInspectorTests(unittest.TestCase):
    def test_read_memory_range_reads_rom_bytes_via_bus(self) -> None:
        # The ROM body is contiguous 0x00s (NOPs) starting at 0x200040.
        with tempfile.TemporaryDirectory() as tmpdir:
            rom = Path(tmpdir) / "demo.ngc"
            _write_demo_rom(rom, b"\x00\x01\x02\x03\x04")
            session = EmulatorSession(rom)
            data = session.read_memory_range(0x00200040, 5)
            self.assertEqual(data, [0x00, 0x01, 0x02, 0x03, 0x04])

    def test_read_memory_range_cpu_io_page_reads_power_on_values(self) -> None:
        # CPU I/O page 0x00..0xFF is a tracked register file. The TMP95C061
        # on-chip registers do NOT reset to zero (2026-07-10): they have
        # documented power-on values, transcribed from the reference emulator's
        # reset table. 0x00/0x01 = 0x00 but 0x02..0x0C = 0xFF.
        with tempfile.TemporaryDirectory() as tmpdir:
            rom = Path(tmpdir) / "demo.ngc"
            _write_demo_rom(rom)
            session = EmulatorSession(rom)
            self.assertEqual(
                session.read_memory_range(0x00000000, 4), [0x00, 0x00, 0xFF, 0xFF]
            )
            # A few load-bearing registers: TRUN, T01MOD, watchdog, INTxx.
            self.assertEqual(session.read_memory_range(0x00000020, 1), [0x80])
            self.assertEqual(session.read_memory_range(0x00000024, 1), [0x03])
            self.assertEqual(session.read_memory_range(0x0000006F, 1), [0x4E])
            self.assertEqual(session.read_memory_range(0x00000070, 1), [0x02])
            # INTE45 is the one register here the BIOS OVERWRITES before it hands the
            # cart the machine, so what a cartridge sees is 0xDC (INT4/VBlank at level
            # 4, INT5 at level 5) -- not the chip's own reset value of 0x32.
            #
            # ⚠️ AND THE DIFFERENCE IS LOAD-BEARING, WHICH IS WHY THIS ASSERTION MOVED
            # RATHER THAN BEING DELETED. Measured across six cartridges (Sonic, Puyo
            # Pop, Metal Slug 2, Fatal Fury, Neo Turf, Pac-Man): NOT ONE of them ever
            # writes INTE45. Every game on the machine depends on the BIOS having armed
            # it. VBlank's level is now READ from this register, so a hand-off that left
            # it at the chip's reset value would quietly hand every cart the wrong
            # interrupt priority.
            self.assertEqual(session.read_memory_range(0x00000071, 1), [0xDC])

    def test_read_memory_range_writable_overlay_shadows_bus(self) -> None:
        # When the session has a writable byte at an address, the
        # overlay value wins over the bus value.
        with tempfile.TemporaryDirectory() as tmpdir:
            rom = Path(tmpdir) / "demo.ngc"
            _write_demo_rom(rom)
            session = EmulatorSession(rom)
            # Overlay 0xAB at a Work RAM address (cold-start 0x00).
            session.memory[0x00006000] = 0xAB
            data = session.read_memory_range(0x00006000, 2)
            self.assertEqual(data[0], 0xAB)

    def test_read_memory_range_rejects_negative_count(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom = Path(tmpdir) / "demo.ngc"
            _write_demo_rom(rom)
            session = EmulatorSession(rom)
            with self.assertRaises(ValueError):
                session.read_memory_range(0, -1)

    def test_disassemble_around_pc_decodes_nops(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom = Path(tmpdir) / "demo.ngc"
            _write_demo_rom(rom, b"\x00\x00\x00\x00")
            session = EmulatorSession(rom)
            instructions = session.disassemble_around_pc(count=3)
            self.assertEqual(len(instructions), 3)
            for offset, (pc, decoded) in enumerate(instructions):
                self.assertEqual(pc, 0x00200040 + offset)
                self.assertEqual(decoded.status, "decoded")
                self.assertEqual(decoded.mnemonic, "nop")

    def test_disassemble_around_pc_stops_on_decode_failure(self) -> None:
        # Plant an undecoded byte sequence — the walker stops there.
        # 0xFF then NOPs : 0xFF likely won't decode in the current
        # subset, so we get one entry with non-decoded status.
        with tempfile.TemporaryDirectory() as tmpdir:
            rom = Path(tmpdir) / "demo.ngc"
            _write_demo_rom(rom, b"\xFF\x00\x00\x00")
            session = EmulatorSession(rom)
            instructions = session.disassemble_around_pc(count=10)
            # At minimum the first entry exists ; if it failed to
            # decode, the walker stopped there.
            self.assertGreaterEqual(len(instructions), 1)
            if instructions[0][1].status != "decoded":
                self.assertEqual(len(instructions), 1)

    def test_disassemble_rejects_zero_count(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom = Path(tmpdir) / "demo.ngc"
            _write_demo_rom(rom)
            session = EmulatorSession(rom)
            with self.assertRaises(ValueError):
                session.disassemble_around_pc(count=0)

    def test_disassemble_from_starts_at_explicit_address(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom = Path(tmpdir) / "demo.ngc"
            _write_demo_rom(rom, b"\x00\x00\x00\x00")
            session = EmulatorSession(rom)
            instructions = session.disassemble_from(0x00200042, count=2)
            self.assertEqual(instructions[0][0], 0x00200042)
            self.assertEqual(instructions[1][0], 0x00200043)
            self.assertEqual(instructions[0][1].mnemonic, "nop")

    def test_disassemble_from_rejects_zero_count(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom = Path(tmpdir) / "demo.ngc"
            _write_demo_rom(rom)
            session = EmulatorSession(rom)
            with self.assertRaises(ValueError):
                session.disassemble_from(0x00200040, count=0)


class SessionJoypadTests(unittest.TestCase):
    def test_joypad_state_defaults_to_zero(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom = Path(tmpdir) / "demo.ngc"
            _write_demo_rom(rom)
            session = EmulatorSession(rom)
            self.assertEqual(session.joypad_state(), 0)
            self.assertNotIn(EmulatorSession.JOYPAD_ADDRESS, session.memory)

    def test_set_joypad_mask_updates_overlay_and_clears_when_released(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom = Path(tmpdir) / "demo.ngc"
            _write_demo_rom(rom)
            session = EmulatorSession(rom)
            changed = session.set_joypad_mask(
                EmulatorSession.JOYPAD_UP | EmulatorSession.JOYPAD_A,
                pressed=True,
            )
            self.assertTrue(changed)
            self.assertEqual(
                session.joypad_state(),
                EmulatorSession.JOYPAD_UP | EmulatorSession.JOYPAD_A,
            )
            self.assertEqual(
                session.memory[EmulatorSession.JOYPAD_ADDRESS],
                EmulatorSession.JOYPAD_UP | EmulatorSession.JOYPAD_A,
            )
            changed = session.set_joypad_mask(EmulatorSession.JOYPAD_UP, pressed=False)
            self.assertTrue(changed)
            self.assertEqual(session.joypad_state(), EmulatorSession.JOYPAD_A)
            changed = session.set_joypad_mask(EmulatorSession.JOYPAD_A, pressed=False)
            self.assertTrue(changed)
            self.assertEqual(session.joypad_state(), 0)
            self.assertNotIn(EmulatorSession.JOYPAD_ADDRESS, session.memory)

    def test_set_joypad_mask_returns_false_when_state_unchanged(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom = Path(tmpdir) / "demo.ngc"
            _write_demo_rom(rom)
            session = EmulatorSession(rom)
            self.assertFalse(
                session.set_joypad_mask(EmulatorSession.JOYPAD_A, pressed=False),
            )
            self.assertTrue(
                session.set_joypad_mask(EmulatorSession.JOYPAD_A, pressed=True),
            )
            self.assertFalse(
                session.set_joypad_mask(EmulatorSession.JOYPAD_A, pressed=True),
            )


class SessionBiosHandoffTests(unittest.TestCase):
    """UI 0.7 — verify the BIOS-equivalent CPU seed on session
    construction. Values sourced from `01_SDK/docs/NGPC_HW_QUICKREF.md
    §2` (XSP = 0x6C00 = top of user RAM) + `ngpcspec.txt
    §INTERRUPT STATE` (DI at boot → iff_level = 7).
    """

    def test_bootstrap_session_seeds_xsp_at_top_of_user_ram(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom = Path(tmpdir) / "demo.ngc"
            _write_demo_rom(rom)
            session = EmulatorSession(rom)
            self.assertEqual(session.cpu.regs.xsp, 0x00006C00)

    def test_bootstrap_session_disables_interrupts(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom = Path(tmpdir) / "demo.ngc"
            _write_demo_rom(rom)
            session = EmulatorSession(rom)
            self.assertEqual(session.cpu.iff_level, 7)
            # `iff_enabled` derived alias should match: level 7 = all
            # maskable IRQs blocked.
            self.assertFalse(session.cpu.iff_enabled)

    def test_bootstrap_session_clears_flags_and_rfp(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom = Path(tmpdir) / "demo.ngc"
            _write_demo_rom(rom)
            session = EmulatorSession(rom)
            self.assertEqual(session.cpu.rfp, 0)
            f = session.cpu.flags
            self.assertEqual(
                (f.sf, f.zf, f.vf, f.hf, f.cf, f.nf),
                (False, False, False, False, False, False),
            )

    def test_bootstrap_session_seeds_intnest_to_zero(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom = Path(tmpdir) / "demo.ngc"
            _write_demo_rom(rom)
            session = EmulatorSession(rom)
            assert session.cpu.control_registers is not None
            self.assertEqual(session.cpu.control_registers.intnest, 0)

    def test_reset_reapplies_bios_handoff_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom = Path(tmpdir) / "demo.ngc"
            _write_demo_rom(rom, b"\x00" * 4)
            session = EmulatorSession(rom)
            session.step(2)
            session.reset()
            self.assertEqual(session.cpu.regs.xsp, 0x00006C00)
            self.assertEqual(session.cpu.iff_level, 7)
            assert session.cpu.control_registers is not None
            self.assertEqual(session.cpu.control_registers.intnest, 0)

    def test_opt_out_preserves_unseeded_bootstrap(self) -> None:
        # `apply_bios_handoff=False` lets the CLI / engine bridge
        # keep the pre-pass-48 "unmodeled" bootstrap CPU.
        with tempfile.TemporaryDirectory() as tmpdir:
            rom = Path(tmpdir) / "demo.ngc"
            _write_demo_rom(rom)
            session = EmulatorSession(rom, apply_bios_handoff=False)
            self.assertIsNone(session.cpu.regs.xsp)
            self.assertIsNone(session.cpu.iff_level)
            self.assertIsNone(session.cpu.flags.cf)
            self.assertEqual(
                session.cpu.control_registers,
                create_unknown_control_registers(),
            )

    def test_handoff_unblocks_first_call_instruction(self) -> None:
        # Pre-pass-48 : a real ROM whose first instruction is `CALL N`
        # blocks with `requires-known-stack-pointer`. With the BIOS
        # hand-off seed, the CALL can push its 4-byte return address
        # onto the stack and execute normally.
        with tempfile.TemporaryDirectory() as tmpdir:
            rom = Path(tmpdir) / "demo.ngc"
            # `1D xx xx xx 00` = CALL imm24 → 5 bytes. Target the next
            # instruction at 0x00200045 so we don't fault on the call
            # target itself.
            body = bytes([0x1D, 0x45, 0x00, 0x20, 0x00, 0x00])
            _write_demo_rom(rom, body)
            session = EmulatorSession(rom)
            # First step should consume the CALL successfully.
            session.step(1)
            self.assertEqual(session.last_stop_reason, "count-reached")
            # CALL pushes 4 bytes → XSP decreased by 4.
            self.assertEqual(session.cpu.regs.xsp, 0x00006BFC)
            # PC at the CALL target.
            self.assertEqual(session.cpu.pc, 0x00200045)


class SessionExternalBiosTests(unittest.TestCase):
    def test_session_bios_backing_unblocks_bios_region_read(self) -> None:
        # `ld XIX, (XIX+W)` reading from `0x00FFFE14` is the concrete
        # frontier seen in real SDK/bootstrap code paths. The live
        # session must be able to consume the same external 64 KB BIOS
        # image as the CLI executor commands.
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            rom = tmp / "demo.ngc"
            bios = tmp / "demo_bios.bin"
            _write_demo_rom(rom, b"\xE3\x03\xF0\xE1\x24")
            _write_demo_bios(bios, patches={0xFE12: b"\xEF\xCD\xAB\x89"})
            session = EmulatorSession(rom, bios_path=bios)
            session.cpu = replace(
                session.cpu,
                regs=replace(
                    session.cpu.regs,
                    xwa=0x00001234,
                    xix=0x00FFFE00,
                ),
            )

            result = session.step(1)

            self.assertEqual(result.stop_reason, "count-reached")
            self.assertEqual(session.last_stop_reason, "count-reached")
            self.assertEqual(session.cpu.regs.xix, 0x89ABCDEF)
            self.assertEqual(session.bios_path, bios)

    def test_set_bios_path_rejects_invalid_size(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            rom = tmp / "demo.ngc"
            bad_bios = tmp / "bad.bin"
            _write_demo_rom(rom)
            bad_bios.write_bytes(b"\x00" * 8)
            session = EmulatorSession(rom)
            with self.assertRaises(ValueError):
                session.set_bios_path(bad_bios)


class SessionSymbolTests(unittest.TestCase):
    def _write_map(self, path: Path, entries: list[tuple[str, int]]) -> None:
        lines = ["=== TEXT ==="]
        for name, addr in entries:
            lines.append(f"{name} 0x{addr:08X}")
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    def test_resolve_symbol_returns_none_without_map(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom = Path(tmpdir) / "demo.ngc"
            _write_demo_rom(rom)
            session = EmulatorSession(rom)
            self.assertIsNone(session.resolve_symbol(0x00200040))

    def test_load_symbol_map_exact_match(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            rom = tmp / "demo.ngc"
            sym = tmp / "demo.map"
            _write_demo_rom(rom)
            self._write_map(sym, [("main", 0x00200040)])
            session = EmulatorSession(rom)
            count = session.load_symbol_map(sym)
            self.assertEqual(count, 1)
            self.assertEqual(session.resolve_symbol(0x00200040), "main")

    def test_resolve_symbol_with_offset(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            rom = tmp / "demo.ngc"
            sym = tmp / "demo.map"
            _write_demo_rom(rom)
            self._write_map(sym, [("main", 0x00200040)])
            session = EmulatorSession(rom)
            session.load_symbol_map(sym)
            self.assertEqual(session.resolve_symbol(0x00200045), "main+0x5")


class SessionBreakpointTests(unittest.TestCase):
    def test_add_list_remove_clear(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom = Path(tmpdir) / "demo.ngc"
            _write_demo_rom(rom)
            session = EmulatorSession(rom)
            self.assertEqual(session.list_breakpoints(), ())
            session.add_breakpoint(0x00200042, "bp42")
            session.add_breakpoint(0x00200044)
            bps = session.list_breakpoints()
            self.assertEqual(len(bps), 2)
            self.assertEqual(bps[0].address, 0x00200042)
            self.assertEqual(bps[0].label, "bp42")
            self.assertEqual(bps[1].address, 0x00200044)
            self.assertIsNone(bps[1].label)
            self.assertTrue(session.has_breakpoint(0x00200042))
            self.assertTrue(session.remove_breakpoint(0x00200042))
            self.assertFalse(session.has_breakpoint(0x00200042))
            self.assertFalse(session.remove_breakpoint(0x00200042))  # already gone
            session.clear_breakpoints()
            self.assertEqual(session.list_breakpoints(), ())

    def test_breakpoint_registry_roundtrip_preserves_duplicate_addresses(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom = Path(tmpdir) / "demo.ngc"
            _write_demo_rom(rom)
            session = EmulatorSession(rom)
            bp1 = session.add_breakpoint(0x00200042, "boot")
            bp2 = session.add_breakpoint(0x00200042, "alt")
            path = session.save_breakpoint_registry()
            self.assertEqual(path, breakpoints_path_for_rom(rom))

            reloaded = EmulatorSession(rom)
            loaded_path, count = reloaded.load_breakpoint_registry()
            self.assertEqual(loaded_path, path)
            self.assertEqual(count, 2)
            rows = reloaded.list_breakpoints()
            self.assertEqual([bp.id for bp in rows], [bp1.id, bp2.id])
            self.assertEqual([bp.label for bp in rows], ["boot", "alt"])

    def test_step_stops_on_breakpoint_hit(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom = Path(tmpdir) / "demo.ngc"
            _write_demo_rom(rom, b"\x00" * 80)
            session = EmulatorSession(rom)
            session.add_breakpoint(0x00200045)
            result = session.step(10)
            self.assertEqual(session.cpu.pc, 0x00200045)
            self.assertEqual(session.last_stop_reason, "breakpoint-hit")
            self.assertEqual(session.last_executed_count, 5)
            # The returned RunStepsResult mirrors the stop reason.
            self.assertEqual(result.stop_reason, "breakpoint-hit")

    def test_single_step_over_existing_breakpoint(self) -> None:
        # Standard debugger UX : when PC is already on a BP, the
        # next `step(1)` advances past it before checking again.
        with tempfile.TemporaryDirectory() as tmpdir:
            rom = Path(tmpdir) / "demo.ngc"
            _write_demo_rom(rom, b"\x00" * 80)
            session = EmulatorSession(rom)
            session.add_breakpoint(0x00200040)  # = current PC
            session.step(1)
            self.assertEqual(session.cpu.pc, 0x00200041)
            self.assertEqual(session.last_stop_reason, "count-reached")

    def test_no_breakpoints_uses_fast_path(self) -> None:
        # Sanity : without BPs, executed_count matches the original
        # one-shot batch behavior — no inner-batching breakage.
        with tempfile.TemporaryDirectory() as tmpdir:
            rom = Path(tmpdir) / "demo.ngc"
            _write_demo_rom(rom, b"\x00" * 80)
            session = EmulatorSession(rom)
            session.step(75)
            self.assertEqual(session.last_executed_count, 75)
            self.assertEqual(session.cpu.pc, 0x00200040 + 75)


class SessionWatchpointTests(unittest.TestCase):
    def _write_io_store_rom(self, path: Path, io_addr: int, imm8: int) -> None:
        # 0x08 [io] [imm8] = LDB (CPU I/O `io`), imm8. Emits a
        # MemoryWrite at address `io_addr` so the watchpoint scan
        # can match it.
        data = bytearray(0x50)
        data[0x00:0x1C] = b"LICENSED BY SNK CORPORATION".ljust(0x1C, b"\x00")
        data[0x1C:0x20] = (0x00200040).to_bytes(4, "little")
        data[0x23] = 0x10
        data[0x24:0x30] = b"WP TEST\x00\x00\x00\x00\x00"
        data[0x40:0x43] = bytes([0x08, io_addr & 0xFF, imm8 & 0xFF])
        path.write_bytes(bytes(data))

    def test_add_list_remove_clear(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom = Path(tmpdir) / "demo.ngc"
            _write_demo_rom(rom)
            session = EmulatorSession(rom)
            self.assertEqual(session.list_watchpoints(), ())
            wp1 = session.add_watchpoint(0x10, kind="write")
            wp2 = session.add_watchpoint(0x20, kind="read", value=0x42)
            self.assertEqual(len(session.list_watchpoints()), 2)
            self.assertEqual(wp1.kind, "write")
            self.assertEqual(wp2.value, 0x42)
            self.assertTrue(session.remove_watchpoint(wp1.id))
            self.assertEqual(len(session.list_watchpoints()), 1)
            session.clear_watchpoints()
            self.assertEqual(session.list_watchpoints(), ())

    def test_watchpoint_registry_roundtrip_preserves_value_filter(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom = Path(tmpdir) / "demo.ngc"
            _write_demo_rom(rom)
            session = EmulatorSession(rom)
            wp = session.add_watchpoint(
                0x10, kind="access", size=2, value=0x42, label="probe",
            )
            path = session.save_watchpoint_registry()
            self.assertEqual(path, watchpoints_path_for_rom(rom))

            reloaded = EmulatorSession(rom)
            loaded_path, count = reloaded.load_watchpoint_registry()
            self.assertEqual(loaded_path, path)
            self.assertEqual(count, 1)
            rows = reloaded.list_watchpoints()
            self.assertEqual(rows[0].id, wp.id)
            self.assertEqual(rows[0].kind, "access")
            self.assertEqual(rows[0].size, 2)
            self.assertEqual(rows[0].value, 0x42)
            self.assertEqual(rows[0].label, "probe")

    def test_add_rejects_invalid_kind(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom = Path(tmpdir) / "demo.ngc"
            _write_demo_rom(rom)
            session = EmulatorSession(rom)
            with self.assertRaises(ValueError):
                session.add_watchpoint(0x10, kind="bogus")

    def test_step_stops_on_write_watchpoint_hit(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom = Path(tmpdir) / "demo.ngc"
            self._write_io_store_rom(rom, io_addr=0x10, imm8=0xAB)
            session = EmulatorSession(rom)
            session.add_watchpoint(0x10, kind="write")
            session.step(1)
            self.assertEqual(session.last_stop_reason, "watchpoint-hit")
            assert session.last_watch_hit is not None
            wp, access, addr, data = session.last_watch_hit
            self.assertEqual(access, "write")
            self.assertEqual(addr, 0x10)
            self.assertEqual(data, b"\xAB")

    def test_value_filter_suppresses_mismatched_write(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom = Path(tmpdir) / "demo.ngc"
            self._write_io_store_rom(rom, io_addr=0x10, imm8=0xAB)
            session = EmulatorSession(rom)
            session.add_watchpoint(0x10, kind="write", value=0xCC)
            session.step(1)
            self.assertEqual(session.last_stop_reason, "count-reached")
            self.assertIsNone(session.last_watch_hit)

    def test_kind_read_does_not_fire_on_write(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom = Path(tmpdir) / "demo.ngc"
            self._write_io_store_rom(rom, io_addr=0x10, imm8=0xAB)
            session = EmulatorSession(rom)
            session.add_watchpoint(0x10, kind="read")
            session.step(1)
            self.assertEqual(session.last_stop_reason, "count-reached")

    def test_no_watchpoints_uses_fast_path(self) -> None:
        # Sanity : without WPs (or BPs), the inner-batch loop
        # collapses to the original one-shot batch.
        with tempfile.TemporaryDirectory() as tmpdir:
            rom = Path(tmpdir) / "demo.ngc"
            _write_demo_rom(rom, b"\x00" * 80)
            session = EmulatorSession(rom)
            session.step(75)
            self.assertEqual(session.last_executed_count, 75)


class OpcodeCoverageCliTests(unittest.TestCase):
    """Pass 50 — `opcode-coverage` CLI smoke + minimal correctness check."""

    def test_coverage_reports_full_decoded_on_nop_only_rom(self) -> None:
        # An all-NOP ROM body should report 100% byte coverage.
        with tempfile.TemporaryDirectory() as tmpdir:
            rom = Path(tmpdir) / "demo.ngc"
            _write_demo_rom(rom, b"\x00" * 16)  # 16 NOPs
            from ngpc_emu import main
            stdout = io.StringIO()
            import contextlib
            with contextlib.redirect_stdout(stdout):
                exit_code = main([
                    "opcode-coverage", str(rom),
                    "--bytes", "16", "--json",
                ])
            self.assertEqual(exit_code, 0)
            import json as _json
            payload = _json.loads(stdout.getvalue())
            self.assertEqual(payload["coverage_byte_percent"], 100.0)
            self.assertEqual(payload["decoded_bytes"], 16)
            self.assertEqual(payload["unknown_opcode_total"], 0)
            self.assertEqual(payload["top_unknown_opcodes"], [])

    def test_coverage_records_top_unknown_opcode(self) -> None:
        # Body with a known-good NOP and a known-unknown 0xE3.
        with tempfile.TemporaryDirectory() as tmpdir:
            rom = Path(tmpdir) / "demo.ngc"
            # 0x00 NOP, 0xE3 (unknown), 0x00 NOP, 0xE3 unknown.
            _write_demo_rom(rom, b"\x00\xE3\x00\xE3")
            from ngpc_emu import main
            stdout = io.StringIO()
            import contextlib
            with contextlib.redirect_stdout(stdout):
                main([
                    "opcode-coverage", str(rom),
                    "--bytes", "4", "--json",
                ])
            import json as _json
            payload = _json.loads(stdout.getvalue())
            tops = payload["top_unknown_opcodes"]
            self.assertGreater(len(tops), 0)
            self.assertEqual(tops[0]["byte_hex"], "0xE3")
            self.assertGreaterEqual(tops[0]["count"], 2)

    def test_coverage_can_stop_on_known_silicon_broken_opcode(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom = Path(tmpdir) / "demo.ngc"
            # D0 FA = known silicon-broken decoded pair (D0..D6 word-memory
            # mis-decode, still honest-stopped); bytes after it should not
            # pollute the unknown census when the explicit stop flag is enabled.
            _write_demo_rom(rom, b"\xD8\xB8\xA8\x24\x0E\x00")
            from ngpc_emu import main
            stdout = io.StringIO()
            import contextlib
            with contextlib.redirect_stdout(stdout):
                main([
                    "opcode-coverage", str(rom),
                    "--bytes", "6",
                    "--stop-on-silicon-broken",
                    "--json",
                ])
            import json as _json
            payload = _json.loads(stdout.getvalue())
            self.assertEqual(payload["stop_reason"], "stopped-on-silicon-broken")
            self.assertEqual(payload["decoded_bytes"], 2)
            self.assertEqual(payload["unknown_opcode_total"], 0)
            self.assertEqual(payload["top_unknown_opcodes"], [])

    def test_coverage_separates_immediate_post_silicon_broken_fallout(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom = Path(tmpdir) / "demo.ngc"
            # Without the explicit stop flag, the immediate byte after the
            # known-broken D0 FA pair (D0..D6 word-memory mis-decode) should be
            # tracked separately from real unknown opcodes.
            _write_demo_rom(rom, b"\xD8\xB8\x04\x00")
            from ngpc_emu import main
            stdout = io.StringIO()
            import contextlib
            with contextlib.redirect_stdout(stdout):
                main([
                    "opcode-coverage", str(rom),
                    "--bytes", "4", "--json",
                ])
            import json as _json
            payload = _json.loads(stdout.getvalue())
            self.assertEqual(payload["decoded_bytes"], 3)
            self.assertEqual(payload["unknown_opcode_total"], 0)
            self.assertEqual(payload["unsupported_decoded_total"], 0)
            self.assertEqual(payload["silicon_broken_fallout_total"], 1)
            self.assertEqual(payload["top_unknown_opcodes"], [])
            self.assertEqual(payload["top_silicon_broken_fallout"][0]["byte_hex"], "0x04")

    def test_coverage_can_stop_on_non_fallthrough(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom = Path(tmpdir) / "demo.ngc"
            _write_demo_rom(rom, b"\x00\x0E\x04\x04")
            from ngpc_emu import main
            stdout = io.StringIO()
            import contextlib
            with contextlib.redirect_stdout(stdout):
                main([
                    "opcode-coverage", str(rom),
                    "--bytes", "4",
                    "--stop-on-non-fallthrough",
                    "--json",
                ])
            import json as _json
            payload = _json.loads(stdout.getvalue())
            self.assertEqual(payload["stop_reason"], "stopped-on-non-fallthrough")
            self.assertEqual(payload["decoded_bytes"], 2)
            self.assertEqual(payload["unknown_opcode_total"], 0)
            self.assertEqual(payload["top_unknown_opcodes"], [])

    def test_coverage_can_follow_direct_control_flow(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom = Path(tmpdir) / "demo.ngc"
            # 0x68 0x04 = unconditional JR to 0x00200046, skipping four dead bytes 0x04.
            _write_demo_rom(rom, b"\x68\x04\x04\x04\x04\x04\x00")
            from ngpc_emu import main
            stdout = io.StringIO()
            import contextlib
            with contextlib.redirect_stdout(stdout):
                main([
                    "opcode-coverage", str(rom),
                    "--bytes", "7",
                    "--follow-direct-control-flow",
                    "--json",
                ])
            import json as _json
            payload = _json.loads(stdout.getvalue())
            self.assertEqual(payload["stop_reason"], "worklist-exhausted")
            self.assertEqual(payload["decoded_bytes"], 3)
            self.assertEqual(payload["unknown_opcode_total"], 0)
            self.assertEqual(payload["top_unknown_opcodes"], [])

    def test_coverage_rejects_cfg_mode_with_stop_flags(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom = Path(tmpdir) / "demo.ngc"
            _write_demo_rom(rom, b"\x00")
            from ngpc_emu import main
            stderr = io.StringIO()
            import contextlib
            with contextlib.redirect_stderr(stderr):
                exit_code = main([
                    "opcode-coverage", str(rom),
                    "--follow-direct-control-flow",
                    "--stop-on-silicon-broken",
                ])
            self.assertEqual(exit_code, 1)
            self.assertIn("cannot be combined", stderr.getvalue())

    def test_coverage_rejects_missing_rom(self) -> None:
        from ngpc_emu import main
        stderr = io.StringIO()
        import contextlib
        with contextlib.redirect_stderr(stderr):
            exit_code = main([
                "opcode-coverage", "/nonexistent.ngc",
            ])
        self.assertEqual(exit_code, 1)
        self.assertIn("ROM not found", stderr.getvalue())


class _QtAvailableMixin:
    """Skip subclasses when PyQt6 + offscreen platform aren't available."""

    @classmethod
    def setUpClass(cls):  # type: ignore[no-untyped-def]
        try:
            import PyQt6  # noqa: F401
        except ImportError:
            raise unittest.SkipTest("PyQt6 not installed")
        import os
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls._qt_app = QApplication.instance() or QApplication([])


class UiDockStructureTests(_QtAvailableMixin, unittest.TestCase):
    """Pass 46 — verify the floating-dock layout + View menu wiring.

    Pure smoke ; uses Qt's `offscreen` platform plugin so no display
    is required. Skipped if PyQt6 isn't installed.
    """

    def setUp(self) -> None:
        self._clear_window_layout_settings()

    def tearDown(self) -> None:
        self._clear_window_layout_settings()

    def _clear_window_layout_settings(self) -> None:
        # Via make_settings(), so this lands on the temp .ini the root conftest
        # redirects to. Built by hand, it removed "window" from the user's own
        # settings on every run of this test.
        import ngpc_settings
        settings = ngpc_settings.make_settings()
        settings.remove("window")
        settings.sync()

    def _open_window(self):
        import tempfile
        from pathlib import Path
        tmp = tempfile.mkdtemp()
        rom = Path(tmp) / "demo.ngc"
        _write_demo_rom(rom)
        from ngpc_emu_ui_qt import EmulatorWindow
        return EmulatorWindow(rom_path=rom), rom

    def test_five_docks_created_floating_and_hidden_by_default(self) -> None:
        # Pass 52 : docks are HIDDEN by default ; user opens them via
        # View → <name>. The floating attribute stays True so when
        # they're shown they appear as independent top-level windows.
        window, _ = self._open_window()
        self.assertEqual(
            set(window._docks.keys()),
            {
                "registers", "disasm", "memory", "breakpoints",
                "watchpoints", "k2ge_video", "k2ge_palettes",
                "k2ge_oam", "k2ge_tilemaps",
            },
        )
        for name, dock in window._docks.items():
            self.assertTrue(dock.isFloating(), f"{name} should be floating")
            self.assertFalse(
                dock.isVisible(),
                f"{name} should be HIDDEN by default (pass 52)",
            )

    def test_view_menu_has_toggle_entries(self) -> None:
        window, _ = self._open_window()
        action_texts = [a.text() for a in window._view_menu.actions() if a.text()]
        for expected in (
            "CPU Registers", "Disassembly", "Memory",
            "Breakpoints", "Watchpoints", "K2GE Video",
            "K2GE Palettes", "K2GE OAM", "K2GE Tilemaps",
        ):
            self.assertIn(expected, action_texts)

    def test_dock_toggle_shows_then_hides(self) -> None:
        # Pass 52 : starts hidden, first toggle shows, second hides.
        window, _ = self._open_window()
        disasm = window._docks["disasm"]
        self.assertFalse(disasm.isVisible())
        disasm.toggleViewAction().trigger()
        self.assertTrue(disasm.isVisible())
        disasm.toggleViewAction().trigger()
        self.assertFalse(disasm.isVisible())

    def test_show_all_then_hide_all(self) -> None:
        window, _ = self._open_window()
        # Start state : all hidden.
        self.assertFalse(
            any(d.isVisible() for d in window._docks.values()),
        )
        window._on_show_all_docks()
        self.assertTrue(
            all(d.isVisible() for d in window._docks.values()),
        )
        window._on_hide_all_docks()
        self.assertFalse(
            any(d.isVisible() for d in window._docks.values()),
        )

    def test_qsettings_remembers_last_directory(self) -> None:
        # Pass 52 : `_remember_dir` persists to QSettings, and a fresh
        # window reads back the same value.
        import tempfile
        # The comment here used to claim "a unique test-only namespace"; there was
        # none, and this removed `last_dir/rom` from the user's real settings on
        # every run. `make_settings()` is the shared entry point EmulatorWindow now
        # uses too, so the conftest redirect covers both sides of this test.
        import ngpc_settings
        settings = ngpc_settings.make_settings()
        settings.remove("last_dir/rom")
        try:
            window, rom = self._open_window()
            self.assertEqual(window._last_dir("rom"), "")
            tmp = tempfile.mkdtemp()
            window._remember_dir("rom", str(Path(tmp) / "some_rom.ngc"))
            # Same window reads back.
            self.assertEqual(window._last_dir("rom"), tmp)
            # Fresh window reads the persisted value.
            from ngpc_emu_ui_qt import EmulatorWindow
            window2 = EmulatorWindow(rom_path=None)
            self.assertEqual(window2._last_dir("rom"), tmp)
        finally:
            settings.remove("last_dir/rom")

    def test_window_layout_persists_visibility(self) -> None:
        window, rom = self._open_window()
        window.show()
        self._qt_app.processEvents()
        disasm = window._docks["disasm"]
        self.assertFalse(disasm.isVisible())
        disasm.show()
        window.resize(777, 555)
        window._save_window_layout()

        from ngpc_emu_ui_qt import EmulatorWindow
        window2 = EmulatorWindow(rom_path=rom)
        window2.show()
        self._qt_app.processEvents()
        self.assertTrue(window2._layout_restored)
        self.assertTrue(window2._docks["disasm"].isVisible())
        self.assertEqual(window2.size().width(), 777)
        self.assertEqual(window2.size().height(), 555)

    def test_breakpoint_registry_menu_roundtrip(self) -> None:
        window, rom = self._open_window()
        window.session.add_breakpoint(0x00200042, "boot")  # type: ignore[union-attr]
        window._on_save_breakpoints_registry()

        from ngpc_emu_ui_qt import EmulatorWindow
        window2 = EmulatorWindow(rom_path=rom)
        self.assertEqual(window2.session.list_breakpoints(), ())  # type: ignore[union-attr]
        window2._on_load_breakpoints_registry()
        rows = window2.session.list_breakpoints()  # type: ignore[union-attr]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].address, 0x00200042)
        self.assertEqual(rows[0].label, "boot")
        self.assertTrue(breakpoints_path_for_rom(rom).exists())

    def test_watchpoint_registry_menu_roundtrip(self) -> None:
        window, rom = self._open_window()
        window.session.add_watchpoint(0x10, kind="read", value=0x7F)  # type: ignore[union-attr]
        window._on_save_watchpoints_registry()

        from ngpc_emu_ui_qt import EmulatorWindow
        window2 = EmulatorWindow(rom_path=rom)
        self.assertEqual(window2.session.list_watchpoints(), ())  # type: ignore[union-attr]
        window2._on_load_watchpoints_registry()
        rows = window2.session.list_watchpoints()  # type: ignore[union-attr]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].start, 0x10)
        self.assertEqual(rows[0].kind, "read")
        self.assertEqual(rows[0].value, 0x7F)
        self.assertTrue(watchpoints_path_for_rom(rom).exists())

    def test_window_constructor_propagates_bios_path_to_session(self) -> None:
        import tempfile
        tmp = tempfile.mkdtemp()
        rom = Path(tmp) / "demo.ngc"
        bios = Path(tmp) / "demo_bios.bin"
        _write_demo_rom(rom)
        _write_demo_bios(bios)
        from ngpc_emu_ui_qt import EmulatorWindow
        window = EmulatorWindow(rom_path=rom, bios_path=bios)
        self.assertIsNotNone(window.session)
        self.assertEqual(window.session.bios_path, bios)


class UiJoypadTests(_QtAvailableMixin, unittest.TestCase):
    def _open_window(self):
        import tempfile
        from pathlib import Path
        tmp = tempfile.mkdtemp()
        rom = Path(tmp) / "demo.ngc"
        _write_demo_rom(rom)
        from ngpc_emu_ui_qt import EmulatorWindow
        window = EmulatorWindow(rom_path=rom)
        window.show()
        self._qt_app.processEvents()
        return window

    def test_keyboard_mapping_updates_joypad_state(self) -> None:
        from PyQt6.QtCore import Qt
        from PyQt6.QtTest import QTest

        window = self._open_window()
        window.activateWindow()
        window.setFocus()
        self._qt_app.processEvents()

        QTest.keyPress(window, Qt.Key.Key_Up)
        self._qt_app.processEvents()
        self.assertEqual(
            window.session.joypad_state(),  # type: ignore[union-attr]
            EmulatorSession.JOYPAD_UP,
        )
        self.assertIn("pad=Up", window.statusBar().currentMessage())

        QTest.keyPress(window, Qt.Key.Key_Z)
        self._qt_app.processEvents()
        self.assertEqual(
            window.session.joypad_state(),  # type: ignore[union-attr]
            EmulatorSession.JOYPAD_UP | EmulatorSession.JOYPAD_A,
        )
        self.assertIn("pad=Up+A", window.statusBar().currentMessage())

        QTest.keyRelease(window, Qt.Key.Key_Up)
        QTest.keyRelease(window, Qt.Key.Key_Z)
        self._qt_app.processEvents()
        self.assertEqual(window.session.joypad_state(), 0)  # type: ignore[union-attr]
        self.assertIn("pad=none", window.statusBar().currentMessage())


class UiDisasmNavigationTests(_QtAvailableMixin, unittest.TestCase):
    def _open_window(self):
        import tempfile
        from pathlib import Path
        tmp = tempfile.mkdtemp()
        rom = Path(tmp) / "demo.ngc"
        _write_demo_rom(rom, b"\x00" * 8)
        from ngpc_emu_ui_qt import EmulatorWindow
        window = EmulatorWindow(rom_path=rom)
        window.show()
        self._qt_app.processEvents()
        return window, rom

    def _write_map(self, path: Path, entries: list[tuple[str, int]]) -> None:
        lines = ["=== TEXT ==="]
        for name, addr in entries:
            lines.append(f"{name} 0x{addr:08X}")
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    def test_go_to_address_reanchors_disassembly(self) -> None:
        window, _ = self._open_window()
        window._disasm_address_edit.setText("0x00200042")
        window._on_disasm_go()
        self.assertEqual(window._disasm_view_address, 0x00200042)
        self.assertIn("0x00200042", window._disasm_text.toPlainText())
        self.assertIn("disasm=0x00200042", window.statusBar().currentMessage())

    def test_go_to_pc_restores_live_anchor(self) -> None:
        window, _ = self._open_window()
        window._disasm_address_edit.setText("0x00200042")
        window._on_disasm_go()
        window._on_disasm_at_pc()
        self.assertIsNone(window._disasm_view_address)
        self.assertEqual(window._disasm_address_edit.text(), "0x00200040")
        self.assertIn("disasm=@PC", window.statusBar().currentMessage())

    def test_go_to_symbol_uses_loaded_map(self) -> None:
        window, rom = self._open_window()
        map_path = rom.parent / "demo.map"
        self._write_map(map_path, [("target_fn", 0x00200043)])
        loaded = window.session.load_symbol_map(map_path)  # type: ignore[union-attr]
        self.assertEqual(loaded, 1)
        window._disasm_address_edit.setText("target_fn")
        window._on_disasm_go()
        self.assertEqual(window._disasm_view_address, 0x00200043)
        self.assertEqual(window._disasm_address_edit.text(), "0x00200043")

class CliUiSubcommandSmokeTests(unittest.TestCase):
    """Confirm `ngpc_emu.py ui [rom]` parses correctly and the lazy
    PyQt import chain handles missing-ROM / no-arg cases without
    launching Qt.
    """

    def test_ui_subcommand_rejects_explicit_missing_rom(self) -> None:
        # `_cmd_ui` returns 1 (with "ROM not found" on stderr) before
        # importing PyQt6 — pure CLI dispatch path, no Qt needed.
        from ngpc_emu import main
        stderr = io.StringIO()
        import contextlib
        with contextlib.redirect_stderr(stderr):
            exit_code = main(["ui", "/nonexistent/path/does/not/exist.ngc"])
        self.assertEqual(exit_code, 1)
        self.assertIn("ROM not found", stderr.getvalue())

    def test_ui_subcommand_accepts_no_rom_argument(self) -> None:
        # `python ngpc_emu.py ui` (no rom) must dispatch cleanly. We
        # can't actually launch Qt headlessly, but we can intercept
        # the lazy launch_ui import to verify args.rom is None
        # propagates to a None rom_path in _cmd_ui.
        import argparse
        import sys
        from unittest.mock import patch
        from ngpc_emu import _cmd_ui

        captured: dict[str, object] = {}

        def fake_launch_ui(rom_path, *, bios_path=None):
            captured["rom_path"] = rom_path
            captured["bios_path"] = bios_path
            return 0

        # Mock the lazy import inside _cmd_ui's body.
        fake_module = type(sys)("ngpc_emu_ui_qt")
        fake_module.launch_ui = fake_launch_ui  # type: ignore[attr-defined]
        with patch.dict(sys.modules, {"ngpc_emu_ui_qt": fake_module}):
            args = argparse.Namespace(rom=None, bios=None)
            exit_code = _cmd_ui(args)
        self.assertEqual(exit_code, 0)
        self.assertIsNone(captured.get("rom_path"))
        self.assertIsNone(captured.get("bios_path"))

    def test_ui_subcommand_forwards_optional_bios_path(self) -> None:
        import argparse
        import sys
        import tempfile
        from unittest.mock import patch
        from ngpc_emu import _cmd_ui

        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            bios = tmp / "demo_bios.bin"
            _write_demo_bios(bios)
            captured: dict[str, object] = {}

            def fake_launch_ui(rom_path, *, bios_path=None):
                captured["rom_path"] = rom_path
                captured["bios_path"] = bios_path
                return 0

            fake_module = type(sys)("ngpc_emu_ui_qt")
            fake_module.launch_ui = fake_launch_ui  # type: ignore[attr-defined]
            with patch.dict(sys.modules, {"ngpc_emu_ui_qt": fake_module}):
                args = argparse.Namespace(rom=None, bios=str(bios))
                exit_code = _cmd_ui(args)
            self.assertEqual(exit_code, 0)
            self.assertIsNone(captured.get("rom_path"))
            self.assertEqual(captured.get("bios_path"), bios)


if __name__ == "__main__":
    unittest.main()
