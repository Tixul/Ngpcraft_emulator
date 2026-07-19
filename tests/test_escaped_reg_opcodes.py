"""Two opcodes Sonic runs every frame, and both of them wrote the WRONG REGISTER.

These are the bugs behind the playtest's "il manque les enemies". Neither was a
wrong CALCULATION -- both computed the right answer and put it somewhere else:

  `B1 2B`   = `ldcf A,(XBC)`.  The destination group's LDA range ran `0x20..0x37`
              in one block, which swallowed `0x28..0x2C` -- the A-indexed carry-bit
              ops -- and executed them as a register load. LDCF must touch NOTHING
              but the carry; we were writing the effective address into XHL. The
              routine it ends returns a boolean in HL, so it returned garbage.

  `C7 F0 0A C0` = `div IX,#0xC0`.  On the C7/D7/E7 ESCAPE the destination register
              is named by the RCODE byte, not by the first byte's 3-bit field --
              and that field is always 7 for an escape, so `rr = 7 >> 1 = 3` made
              every escaped MUL/DIV target XHL. We divided HL instead of IX.

Both were SILENT: the core reported `executed`, which is exactly the fallback
HARDWARE_COMPAT_POLICY.md § 9 forbids. And the differential gate could not see
them -- the Python reference decodes neither form, so the two cores never
disagreed. It took the ORACLE'S PICTURE (an enemy that was there and here was
not) to surface them.
"""

from __future__ import annotations

import unittest

from core import native

ENTRY = 0x200020


def _rom(code: bytes) -> bytes:
    """A minimal cartridge: a header whose entry point lands on `code`."""
    rom = bytearray(0x400)
    rom[0x1C:0x20] = ENTRY.to_bytes(4, "little")
    rom[0x20 : 0x20 + len(code)] = code
    return bytes(rom)


@unittest.skipUnless(native.available(), "native core not built")
class EscapedRegisterOpcodeTests(unittest.TestCase):
    def _run_one(self, code: bytes, regs: dict[int, int], flags: int = 0):
        with native.NativeMachine(_rom(code)) as m:
            m.reset(bios_handoff=True)
            cpu = m.cpu()
            for idx, val in regs.items():
                cpu.regs[idx] = val
            cpu.pc = ENTRY
            cpu.flags = flags
            m.set_cpu(cpu)
            summary, _ = m.run(1)
            self.assertEqual(
                native.status_name(summary.stop_status), "count-reached",
                f"the core refused {code.hex()}",
            )
            return m.cpu()

    def test_ldcf_a_mem_writes_the_carry_and_NOTHING_else(self) -> None:
        """`ldcf A,(XBC)` loads one bit of (XBC) into C. No register may move.

        If XHL comes back holding XBC's value, the LDA range has swallowed the
        carry-bit ops again -- which is the exact shape of the Sonic bug.
        """
        with native.NativeMachine(_rom(b"\xB1\x2B")) as m:
            m.reset(bios_handoff=True)
            m.write(0x004000, b"\x08")          # bit 3 set, bits 0-2 clear
            cpu = m.cpu()
            cpu.regs[0] = 0x0003            # A = 3 -> select bit 3
            cpu.regs[1] = 0x00004000            # XBC -> the byte we just wrote
            cpu.regs[3] = 0x0039_0001           # XHL: a boolean return value
            cpu.pc = ENTRY
            cpu.flags = 0
            m.set_cpu(cpu)
            m.run(1)
            after = m.cpu()

            self.assertEqual(
                after.regs[3], 0x0039_0001,
                "ldcf clobbered XHL -- it must write ONLY the carry flag",
            )
            self.assertTrue(after.flags & 0x01, "bit 3 of (XBC) is set: C must be 1")

        # ...and a clear bit must clear the carry, so the test can fail both ways.
        with native.NativeMachine(_rom(b"\xB1\x2B")) as m:
            m.reset(bios_handoff=True)
            m.write(0x004000, b"\x08")
            cpu = m.cpu()
            cpu.regs[0] = 0x0000                # A = 0 -> look at bit 0, which is CLEAR
            cpu.regs[1] = 0x00004000
            cpu.regs[3] = 0x0039_0001
            cpu.pc = ENTRY
            cpu.flags = 0x01                    # C starts SET
            m.set_cpu(cpu)
            m.run(1)
            after = m.cpu()
            self.assertEqual(after.regs[3], 0x0039_0001)
            self.assertFalse(after.flags & 0x01, "bit 0 is clear: C must be 0")

    def test_escaped_div_targets_the_register_the_RCODE_names(self) -> None:
        """`C7 F0 0A C0` is `div IX,#0xC0` -- IX, not XHL.

        rcode 0xF0 -> current bank, xreg = (0xF0 >> 2) & 7 = 4 = XIX. The first
        byte (0xC7) carries no register at all; deriving one from it targets XHL
        every single time.

        0x00C2 / 0xC0 = 1 remainder 2, so IX must come back 0x0201 (remainder in
        the high byte, quotient in the low) with its upper half untouched.
        """
        after = self._run_one(
            b"\xC7\xF0\x0A\xC0",
            {3: 0x0000_506C, 4: 0x001F_00C2},   # XHL = a live value; XIX = the dividend
        )
        self.assertEqual(
            after.regs[4], 0x001F_0201,
            "the escaped div must write XIX (rcode 0xF0), quotient 1 remainder 2",
        )
        self.assertEqual(
            after.regs[3], 0x0000_506C,
            "XHL must be untouched -- deriving the register from the escape byte "
            "makes every escaped MUL/DIV land on XHL",
        )

    def test_escaped_div_still_divides_correctly(self) -> None:
        """Same instruction, a different register: rcode 0xF4 names XIY (index 5)."""
        after = self._run_one(
            b"\xC7\xF4\x0A\x0A",                # div IY, #10
            {5: 0x00AA_0064},                   # IY = 100
        )
        # 100 / 10 = 10 remainder 0 -> 0x000A, upper half preserved.
        self.assertEqual(after.regs[5], 0x00AA_000A)

    def test_escaped_link_targets_the_RCODE_register_not_XSP(self) -> None:
        """`link XWA3,4` must link XWA3 -- found by AUDIT, not by a game breaking.

        The official assembler says the escape form is real:

            link XWA3,4  ->  e7 30 0c 04 00        unlk XWA3  ->  e7 30 0d

        and rcode 0x30 names bank 3's XWA. LINK writes the register back
        (`push r ; ld r,XSP ; add XSP,d`), and doing that through `c.regs[r]` --
        with `r` the low 3 bits of 0xE7, i.e. **7, XSP** -- would have linked the
        STACK POINTER instead. Same class of bug as the escaped MUL/DIV; this one
        was swept up before any ROM tripped over it.
        """
        with native.NativeMachine(_rom(b"\xE7\x30\x0C\x04\x00")) as m:
            m.reset(bios_handoff=True)
            cpu = m.cpu()
            cpu.pc = ENTRY
            cpu.regs[7] = 0x0000_6C00           # XSP
            cpu.banks[3][0] = 0x1122_3344       # XWA3, the register the rcode names
            m.set_cpu(cpu)
            summary, _ = m.run(1)
            self.assertEqual(native.status_name(summary.stop_status), "count-reached")
            after = m.cpu()

            # push XWA3 -> XSP 0x6C00-4 ; XWA3 := that ; XSP += 4 -> back to 0x6C00.
            self.assertEqual(
                after.banks[3][0], 0x0000_6BFC,
                "LINK must write the register the RCODE names (XWA3), not XSP",
            )
            self.assertEqual(after.regs[7], 0x0000_6C00)

    def test_a_store_sub_op_that_does_not_exist_must_TRAP(self) -> None:
        """`B1 48` is not an encoding. It must stop the core, not become `B1 40`.

        The stores are `0x40 + R` / `0x50 + R` / `0x60 + R` and R is THREE BITS --
        the official assembler pins them (`ld (XBC),A` -> `b1 41`). Our range ran
        `0x40..0x67` in one span, so 0x48..0x4F and 0x58..0x5F were executed
        silently as duplicate stores. An encoding we have not learned must TRAP:
        HARDWARE_COMPAT_POLICY.md § 9 forbids the quiet fallback, and this test is
        what keeps that promise honest.
        """
        with native.NativeMachine(_rom(b"\xB1\x48")) as m:
            m.reset(bios_handoff=True)
            cpu = m.cpu()
            cpu.pc = ENTRY
            m.set_cpu(cpu)
            summary, _ = m.run(1)
            self.assertEqual(
                native.status_name(summary.stop_status), "unimplemented",
                "a non-existent store sub-op was executed instead of trapping",
            )


if __name__ == "__main__":
    unittest.main()
