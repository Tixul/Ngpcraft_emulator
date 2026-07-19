"""BS1F / BS1B -- bit search, forward and backward.

The King of Fighters: Battle de Paradise stops dead on `D9 0F` (`bs1b A,BC`).
It was simply never ported. The official Toshiba assembler names the encoding:

    bs1f A,BC -> d9 0e        bs1b A,BC -> d9 0f

and the datasheet (CPU900L1-55/56) is unusually explicit about the rest, so these
tests are transcribed from IT, not from our implementation -- including its own
worked examples, which are the strongest assertions here:

    IX = 0x1200  ->  BS1F sets A = 0x09        (bit 9, the lowest set bit)
                     BS1B sets A = 0x0C        (bit 12, the highest set bit)

Flags row `- - - * - -`: V alone moves. V = 1 iff the source is all zeros -- and in
that case the datasheet calls A "an undefined value", so we write nothing to it.
"""

from __future__ import annotations

import unittest

from core import native

ENTRY = 0x200020

XWA, XBC, XDE, XHL, XIX, XIY, XIZ, XSP = range(8)

F_V = 0x04


def _rom(code: bytes) -> bytes:
    rom = bytearray(0x400)
    rom[0x1C:0x20] = ENTRY.to_bytes(4, "little")
    rom[0x20 : 0x20 + len(code)] = code
    return bytes(rom)


@unittest.skipUnless(native.available(), "native core not built")
class BitSearchTests(unittest.TestCase):
    def _run(self, code: bytes, *, src_reg: int, src: int, a: int = 0xEE, flags: int = 0):
        # The source may NOT be XWA: XWA carries A, the destination, and seeding it
        # would overwrite the very word we are asking the core to search.
        assert src_reg != XWA, "seed the source in a register that is not XWA"
        with native.NativeMachine(_rom(code)) as m:
            m.reset(bios_handoff=True)
            cpu = m.cpu()
            cpu.regs[src_reg] = src
            cpu.regs[XWA] = 0x1234_5600 | a          # A = XWA's low byte
            cpu.pc = ENTRY
            cpu.flags = flags
            m.set_cpu(cpu)
            summary, _ = m.run(1)
            self.assertEqual(
                native.status_name(summary.stop_status), "count-reached",
                f"the core refused {code.hex()}",
            )
            return m.cpu()

    def test_bs1f_finds_the_lowest_set_bit(self) -> None:
        """The datasheet's own example: IX = 0x1200 -> A = 0x09."""
        after = self._run(b"\xD9\x0E", src_reg=XBC, src=0x0000_1200)  # bs1f A,BC
        self.assertEqual(after.regs[XWA] & 0xFF, 0x09)
        self.assertEqual(after.flags & F_V, 0, "V must be cleared when a bit was found")

    def test_bs1b_finds_the_highest_set_bit(self) -> None:
        """The datasheet's own example: IX = 0x1200 -> A = 0x0C."""
        after = self._run(b"\xD9\x0F", src_reg=XBC, src=0x0000_1200)  # bs1b A,BC
        self.assertEqual(after.regs[XWA] & 0xFF, 0x0C)
        self.assertEqual(after.flags & F_V, 0)

    def test_the_bc_form_that_kof_battle_de_paradise_stops_on(self) -> None:
        """`D9 0F` = `bs1b A,BC` -- the exact instruction that halted the ROM."""
        after = self._run(b"\xD9\x0F", src_reg=XBC, src=0x0000_8001)
        self.assertEqual(after.regs[XWA] & 0xFF, 15, "backward search must find bit 15")
        after = self._run(b"\xD9\x0E", src_reg=XBC, src=0x0000_8001)
        self.assertEqual(after.regs[XWA] & 0xFF, 0, "forward search must find bit 0")

    def test_only_the_low_16_bits_are_searched(self) -> None:
        """Word size. A 1 in the upper half of the 32-bit register is NOT there."""
        after = self._run(b"\xD9\x0F", src_reg=XBC, src=0xFFFF_0010)
        self.assertEqual(after.regs[XWA] & 0xFF, 4, "the search must not see bits 16..31")

    def test_a_zero_source_sets_V_and_leaves_A_ALONE(self) -> None:
        """`V = 1 if the contents of src are all 0s`, and A is UNDEFINED.

        We write nothing to A rather than invent a value the hardware does not
        define. Software is expected to test V -- that is what V is for.
        """
        after = self._run(b"\xD9\x0F", src_reg=XBC, src=0x0000_0000, a=0xEE)
        self.assertEqual(after.flags & F_V, F_V, "V must be SET when no bit is found")
        self.assertEqual(
            after.regs[XWA] & 0xFF, 0xEE,
            "A must be left untouched: the datasheet calls its value undefined",
        )

    def test_the_other_flags_do_not_move(self) -> None:
        """Flags row `- - - * - -`: S, Z, H, N and C are all `No change`."""
        keep = 0x80 | 0x40 | 0x10 | 0x02 | 0x01      # S Z H N C
        after = self._run(b"\xD9\x0E", src_reg=XBC, src=0x0000_0100, flags=keep)
        self.assertEqual(
            after.flags & keep, keep,
            "BS1F touched a flag other than V",
        )

    def test_the_byte_and_long_forms_do_not_exist(self) -> None:
        """Size column `× ż ×`: word only. Byte and long must TRAP, not guess."""
        for code, label in ((b"\xC9\x0F", "byte"), (b"\xE9\x0F", "long")):
            with native.NativeMachine(_rom(code)) as m:
                m.reset(bios_handoff=True)
                cpu = m.cpu()
                cpu.pc = ENTRY
                m.set_cpu(cpu)
                summary, _ = m.run(1)
                self.assertEqual(
                    native.status_name(summary.stop_status), "unimplemented",
                    f"the {label} form of BS1B does not exist and must not execute",
                )


if __name__ == "__main__":
    unittest.main()
