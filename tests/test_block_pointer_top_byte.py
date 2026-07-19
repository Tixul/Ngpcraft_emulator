"""A walking pointer is a 32-BIT REGISTER. The bus is 24 bits. Not the same thing.

The block instructions used to mask their pointer WRITE-BACK to 24 bits, and the code
said why: "the walking pointers are addresses, so the top byte does not accumulate
garbage". That reasoning is wrong, and the wrongness is the whole bug: the top byte is
not garbage, it is the REGISTER'S, and an NGPC address needs only 24 of its 32 bits --
so software is free to keep something up there.

POCKET TENNIS COLOR does exactly that. It packs a pointer and a loop counter into one
register -- address in the low 24 bits, count in the top byte -- and ends the loop with
`djnz QH` (`C7 EF 1C`; the official assembler confirms QH is XHL's top byte):

    ld  XHL, (XWA+)          ; XHL = 0x03004018  -> ptr 0x004018, count 3
    loop:
    ld  DE, (XHL+)
    ldir (XHL)               ; <- wiped the count on every pass
    djnz QH, loop            ; -> the count never reached zero

The counter went 3 -> 0xFF on the first pass and the game spun forever on a blank
screen. The mask belongs on the ACCESS -- which is where it already was -- and nowhere
else. Same distinction this codebase already draws for LDA, which keeps all 32 bits
because it performs no access at all.
"""

from __future__ import annotations

import unittest

from core import native

ENTRY = 0x200020

XWA, XBC, XDE, XHL, XIX, XIY, XIZ, XSP = range(8)

SRC = 0x004800
DST = 0x004900
TAG = 0x7F          # the byte the program parks above its pointer


def _rom(code: bytes) -> bytes:
    rom = bytearray(0x400)
    rom[0x1C:0x20] = ENTRY.to_bytes(4, "little")
    rom[0x20 : 0x20 + len(code)] = code
    return bytes(rom)


@unittest.skipUnless(native.available(), "native core not built")
class BlockPointerTopByteTests(unittest.TestCase):
    def _run(self, code: bytes, *, bc: int):
        with native.NativeMachine(_rom(code)) as m:
            m.reset(bios_handoff=True)
            m.write(SRC, bytes(range(1, 17)))
            cpu = m.cpu()
            cpu.regs[XHL] = (TAG << 24) | SRC       # source pointer + the program's tag
            cpu.regs[XDE] = (TAG << 24) | DST       # destination, tagged the same way
            cpu.regs[XBC] = bc
            cpu.pc = ENTRY
            m.set_cpu(cpu)
            summary, _ = m.run(1)
            self.assertEqual(
                native.status_name(summary.stop_status), "count-reached",
                f"the core refused {code.hex()}",
            )
            return m.cpu()

    def test_ldi_keeps_the_top_byte_of_both_pointers(self) -> None:
        """`93 10` = `ldi (XDE),(XHL)`, word. One step, and the tag must survive."""
        after = self._run(b"\x93\x10", bc=4)
        self.assertEqual(
            after.regs[XHL], (TAG << 24) | (SRC + 2),
            "LDI masked the source pointer and destroyed the program's top byte",
        )
        self.assertEqual(
            after.regs[XDE], (TAG << 24) | (DST + 2),
            "LDI masked the destination pointer and destroyed the program's top byte",
        )

    def test_ldir_keeps_the_top_byte_across_every_iteration(self) -> None:
        """`93 11` = `ldir (XDE),(XHL)`. This is the one Pocket Tennis runs."""
        after = self._run(b"\x93\x11", bc=4)
        self.assertEqual(after.regs[XHL], (TAG << 24) | (SRC + 8))
        self.assertEqual(after.regs[XDE], (TAG << 24) | (DST + 8))
        self.assertEqual(after.regs[XBC] & 0xFFFF, 0, "BC must run down to zero")

    def test_the_bytes_still_land_at_the_MASKED_address(self) -> None:
        """The tag must NOT reach the bus: it is a register byte, not an address.

        This is the other half of the rule, and the one a careless fix would break --
        dropping the mask on the ACCESS as well would send the copy to 0x7F004900.
        """
        after = self._run(b"\x93\x11", bc=4)
        del after
        # A separate run so we can read memory back out of a live machine.
        with native.NativeMachine(_rom(b"\x93\x11")) as m:
            m.reset(bios_handoff=True)
            m.write(SRC, bytes(range(1, 17)))
            cpu = m.cpu()
            cpu.regs[XHL] = (TAG << 24) | SRC
            cpu.regs[XDE] = (TAG << 24) | DST
            cpu.regs[XBC] = 4
            cpu.pc = ENTRY
            m.set_cpu(cpu)
            m.run(1)
            self.assertEqual(
                m.read(DST, 8), bytes(range(1, 9)),
                "the copy did not land at the 24-bit address -- the tag reached the bus",
            )

    def test_cpi_keeps_the_top_byte_too(self) -> None:
        """`93 14` = `cpi (XHL)`. Same register, same rule, different instruction."""
        after = self._run(b"\x93\x14", bc=4)
        self.assertEqual(
            after.regs[XHL], (TAG << 24) | (SRC + 2),
            "CPI masked its pointer -- the rule is not per-instruction, it is per-BUS",
        )


if __name__ == "__main__":
    unittest.main()
