"""Live RAS.H reads in the native core, using only synthetic code and RAM.

K2GE Technical Reference section 4-8 defines RAS.H as the upper eight bits
of the 515-clock horizontal subtraction counter. These tests pin the core's
existing line-phase convention, not hardware read-at-rollover microtiming.
Run from the repository root: python -m unittest discover -s tests -p test_rash_native.py -v
"""

from __future__ import annotations

import unittest

from core import native

RAS_H, RAS_V = 0x8008, 0x8009
CLOCKS_PER_LINE = 515


@unittest.skipUnless(native.available(), "native core not built (cmake --build cpp/build)")
class HorizontalRasterNativeTests(unittest.TestCase):
    def setUp(self) -> None:
        header = bytearray(64)
        header[0x1C:0x20] = (0x200040).to_bytes(4, "little")
        self.m = native.NativeMachine(bytes(header) + bytes.fromhex("68fe"))
        self.addCleanup(self.m.close)
        self.m.reset(bios_handoff=False)
        self.m.write(0x4000, bytes(4096))  # RAM NOPs avoid cartridge-fetch waits.
        cpu = self.m.cpu()
        cpu.pc = 0x4000
        cpu.iff_level = 7
        self.m.set_cpu(cpu)

    def set_phase(self, phase: int, line: int = 0) -> None:
        state = self.m.aux_state()
        state.cycle_residue, state.scanline = phase, line
        # The aux-state API expects its corresponding memory image first.
        self.m.write(RAS_V, bytes([line]))
        self.assertTrue(self.m.set_aux_state(state))

    def test_all_phases_use_upper_counter_bits_including_vblank(self) -> None:
        for line in (0, 151, 152, 198):
            values = []
            for phase in range(CLOCKS_PER_LINE):
                self.set_phase(phase, line)
                value = self.m.read(RAS_H, 1)[0]
                self.assertEqual(value, (CLOCKS_PER_LINE - phase) >> 2,
                                 f"line={line}, phase={phase}")
                values.append(value)
            self.assertEqual((min(values), max(values)), (0, 128))
            self.assertTrue(all(b <= a for a, b in zip(values, values[1:])))

    def test_instruction_cycles_advance_the_same_counter(self) -> None:
        self.set_phase(100, 7)
        self.assertEqual(self.m.read(RAS_H, 1)[0], 103)
        for phase, expected in ((102, 103), (104, 102)):
            summary, _ = self.m.run(1, record=False)
            self.assertEqual(summary.executed, 1)
            self.assertEqual(self.m.aux_state().cycle_residue, phase)
            self.assertEqual(self.m.read(RAS_H, 1)[0], expected)

    def test_reads_do_not_advance_the_counter(self) -> None:
        self.set_phase(255, 5)
        before = self.m.aux_state()
        for _ in range(20):
            self.assertEqual(self.m.read(RAS_H, 1), b"\x41")  # 260 >> 2.
        after = self.m.aux_state()
        self.assertEqual((after.cycle_residue, after.scanline),
                         (before.cycle_residue, before.scanline))

    def test_memory_pokes_cannot_replace_live_counter(self) -> None:
        self.set_phase(255)
        for value in (0, 1, 0x55, 0xFF):
            self.m.write(RAS_H, bytes([value]))
            self.assertEqual(self.m.read(RAS_H, 1)[0], 65)

    def test_counter_wraps_with_line_and_frame(self) -> None:
        for line, next_line in ((0, 1), (198, 0)):
            self.set_phase(514, line)
            before_frame = self.m.aux_state().frame_count
            self.assertEqual(self.m.read(RAS_H, 1)[0], 0)
            summary, _ = self.m.run(1, record=False)  # NOP: next line, phase 1.
            self.assertEqual(summary.executed, 1)
            state = self.m.aux_state()
            self.assertEqual((state.cycle_residue, state.scanline), (1, next_line))
            self.assertEqual(self.m.read(RAS_H, 2), bytes([128, next_line]))
            self.assertEqual(state.frame_count, before_frame + int(line == 198))

    def test_cpu_byte_and_word_loads_use_live_register(self) -> None:
        for opcode, mask in (("c1088021", 0xFF), ("d1088020", 0xFFFF)):
            with self.subTest(opcode=opcode):
                self.set_phase(100, 7)
                self.m.write(0x4000, bytes.fromhex(opcode))
                cpu = self.m.cpu()
                cpu.pc = 0x4000
                self.m.set_cpu(cpu)
                expected = ((515 - 100) >> 2) | (7 << 8)
                summary, _ = self.m.run(1, record=False)
                self.assertEqual(summary.executed, 1)
                self.assertEqual(self.m.cpu().regs[0] & mask, expected & mask)

    def test_state_restore_restores_counter_phase(self) -> None:
        self.set_phase(400, 23)
        snapshot = self.m.aux_state()
        expected = bytes([28, 23])
        self.assertEqual(self.m.read(RAS_H, 2), expected)
        self.m.run(100, record=False)
        self.assertNotEqual(self.m.read(RAS_H, 1), expected[:1])
        self.m.write(RAS_V, expected[1:2])  # Restore the matching memory-image byte.
        self.assertTrue(self.m.set_aux_state(snapshot))
        self.assertEqual(self.m.read(RAS_H, 2), expected)


if __name__ == "__main__":
    unittest.main()
