"""The secondary addressing byte names its base register with an RCODE.

`E3 14 21` is `ld XBC,(XBC1)` -- the base is BANK 1's BC, named by the extended
register code 0x14. The decoder read it as a bare 3-bit register number instead
(`(data >> 2) & 7` = 5 = XIY) and dereferenced XIY.

That decode is right by ACCIDENT for a current-bank code (0xE0..0xFF, where
rcode = 0xE0 + reg*4 + pos, so `(rcode >> 2) & 7` IS the register) -- which is
all a compiler normally emits. It is wrong for every explicitly-banked code, and
Densha de Go! 2 walks its object list through one: it read a ROM address instead
of the list in RAM, fed a pointer to a jump-table index, and drove the PC out of
the cartridge. It was the only colour ROM in the corpus with a black screen.

The official assembler settles the encoding:

    ld XBC,(XIY+0x1234) -> e3 f5 34 12 21     0xF5 = XIY's rcode | the d16 flag
    ld XBC,(XIY+WA)     -> e3 07 f4 e0 21     the indexed form, rcodes throughout
    ld XBC,(XIY+)       -> e5 f6 21           0xF6 = XIY's rcode | step 4

The byte is `rrrrrrmm`: six bits of register code, two of mode. The (r32+r8)
branch had ALREADY been fixed to go through `rd_rcode()`; the two branches beside
it were left with the bare decode. One rule, three implementations, two holes.

These tests are DISCRIMINATING: each seeds XIY with a decoy address holding a
different value, so the old decode returns the decoy and fails.
"""

from __future__ import annotations

import unittest

from core import native

ENTRY = 0x200020

XWA, XBC, XDE, XHL, XIX, XIY, XIZ, XSP = range(8)

RAM_LIST = 0x004982      # where bank 1's BC will point -- the real base
RAM_DECOY = 0x004100     # where XIY will point -- what the bare decode would read

TRUE_VALUE = 0x00217B13  # what the correct base must yield
DECOY_VALUE = 0xDEADBEEF  # what XIY would yield if the base were decoded as XIY


def _rom(code: bytes) -> bytes:
    rom = bytearray(0x400)
    rom[0x1C:0x20] = ENTRY.to_bytes(4, "little")
    rom[0x20 : 0x20 + len(code)] = code
    return bytes(rom)


@unittest.skipUnless(native.available(), "native core not built")
class SecondaryByteRcodeTests(unittest.TestCase):
    def _run(self, code: bytes, *, bank1_bc: int, decoy: int) -> object:
        with native.NativeMachine(_rom(code)) as m:
            m.reset(bios_handoff=True)
            # The real list, reachable only through bank 1's BC.
            m.write(RAM_LIST, TRUE_VALUE.to_bytes(4, "little"))
            m.write(RAM_LIST + 4, TRUE_VALUE.to_bytes(4, "little"))
            # The decoy, reachable only through XIY.
            m.write(RAM_DECOY, DECOY_VALUE.to_bytes(4, "little"))
            m.write(RAM_DECOY + 4, DECOY_VALUE.to_bytes(4, "little"))

            cpu = m.cpu()
            cpu.banks[1][XBC] = bank1_bc      # rcode 0x14 = bank 1's BC
            cpu.regs[XIY] = decoy             # rcode 0xF4 -- NOT what 0x14 names
            cpu.rfp = 0                       # we are running in bank 0
            cpu.pc = ENTRY
            m.set_cpu(cpu)
            summary, _ = m.run(1)
            self.assertEqual(
                native.status_name(summary.stop_status), "count-reached",
                f"the core refused {code.hex()}",
            )
            return m.cpu()

    def test_r32_base_comes_from_the_rcode_not_a_3_bit_field(self) -> None:
        """`E3 14 21` = `ld XBC,(XBC1)`. Bank 1's BC is the pointer, not XIY."""
        after = self._run(b"\xE3\x14\x21", bank1_bc=RAM_LIST, decoy=RAM_DECOY)
        self.assertEqual(
            after.regs[XBC], TRUE_VALUE,
            "the base register was taken as XIY -- the secondary byte is an RCODE",
        )

    def test_r32_plus_d16_base_comes_from_the_rcode(self) -> None:
        """`E3 15 04 00 20` = `ld XWA,(XBC1+4)`. Densha's dispatcher, exactly."""
        after = self._run(b"\xE3\x15\x04\x00\x20", bank1_bc=RAM_LIST, decoy=RAM_DECOY)
        self.assertEqual(
            after.regs[XWA], TRUE_VALUE,
            "the (r32+d16) base was taken as XIY -- it too is named by an RCODE",
        )

    def test_post_increment_reads_and_writes_back_through_the_rcode(self) -> None:
        """`E5 16 21` = `ld XBC,(XBC1+)`, step 4. The write-back must hit bank 1."""
        # 0x16 = rcode 0x14 (bank 1's BC) | step 4.
        after = self._run(b"\xE5\x16\x21", bank1_bc=RAM_LIST, decoy=RAM_DECOY)
        self.assertEqual(after.regs[XBC], TRUE_VALUE, "post-inc read the wrong base")
        self.assertEqual(
            after.banks[1][XBC], RAM_LIST + 4,
            "the post-increment wrote back to a bare register instead of bank 1's BC",
        )
        self.assertEqual(
            after.regs[XIY], RAM_DECOY,
            "the write-back landed on XIY -- it must go through the rcode",
        )

    def test_current_bank_codes_still_resolve(self) -> None:
        """The forms that ALREADY worked must keep working.

        `E3 F4 21` = `ld XBC,(XIY)`: rcode 0xF4 IS XIY, so here the base really is
        XIY. This is the case the bare decode got right, and the reason the bug
        survived 73 ROMs.
        """
        after = self._run(b"\xE3\xF4\x21", bank1_bc=RAM_LIST, decoy=RAM_DECOY)
        self.assertEqual(after.regs[XBC] & 0xFFFFFFFF, DECOY_VALUE)

    def test_an_undefined_mode_traps_instead_of_being_read_as_r32(self) -> None:
        """`mm = 2` names no addressing mode. It must STOP, not guess.

        The old code let every unrecognised secondary byte fall through to `(r32)`
        and reported `executed` -- the silent fallback § 9 forbids.
        """
        with native.NativeMachine(_rom(b"\xE3\x16\x21")) as m:
            m.reset(bios_handoff=True)
            cpu = m.cpu()
            cpu.pc = ENTRY
            m.set_cpu(cpu)
            summary, _ = m.run(1)
            self.assertEqual(
                native.status_name(summary.stop_status), "unimplemented",
                "an undefined addressing mode was executed anyway",
            )


class PythonReferenceResolvesTheSameCodes(unittest.TestCase):
    """The REFERENCE core had the identical bug, so the two AGREED -- IN ERROR.

    That is the failure mode the differential gate cannot catch: it proves the two
    cores say the same thing, never that either is right. Both are now bound to the
    same map, and this test is what keeps them there.
    """

    def _cpu(self, *, xiy: int, bank1_xbc: int, rfp: int = 0):
        from core.cpu import BankedByteRegisters, GeneralRegisters32, NgpcCpuState, StatusFlags

        def slots(value: int) -> tuple[int | None, ...]:
            return tuple((value >> (8 * i)) & 0xFF for i in range(4))

        banks = tuple(
            BankedByteRegisters(
                slots=(
                    slots(0)                                    # XWA
                    + (slots(bank1_xbc) if b == 1 else slots(0))  # XBC
                    + slots(0)                                  # XDE
                    + slots(0)                                  # XHL
                )
            )
            for b in range(4)
        )
        return NgpcCpuState(
            pc=0x200000,
            sr_raw=None,
            flags=StatusFlags(sf=False, zf=False, vf=False, hf=False, cf=False, nf=False),
            register_bank=rfp,
            regs=GeneralRegisters32(
                xwa=0, xbc=0, xde=0, xhl=0, xix=0, xiy=xiy, xiz=0, xsp=0
            ),
            modeled_fields=(),
            note="",
            rfp=rfp,
            register_banks=banks,
        )

    def test_a_banked_code_names_the_banked_register_not_xiy(self) -> None:
        from core.execute import secondary_base_r32

        cpu = self._cpu(xiy=RAM_DECOY, bank1_xbc=RAM_LIST)
        name, value, refusal = secondary_base_r32(cpu, 0x14)   # bank 1's BC
        self.assertIsNone(refusal)
        self.assertEqual(name, "XBC1")
        self.assertEqual(
            value, RAM_LIST,
            "the reference resolved the base as XIY -- the same bug the native core had",
        )

    def test_the_d16_flag_does_not_change_which_register_is_named(self) -> None:
        from core.execute import secondary_base_r32

        cpu = self._cpu(xiy=RAM_DECOY, bank1_xbc=RAM_LIST)
        # 0x15 = the same code 0x14 with the d16 mode bit set.
        self.assertEqual(secondary_base_r32(cpu, 0x15)[1], RAM_LIST)

    def test_a_current_bank_code_still_names_xiy(self) -> None:
        from core.execute import secondary_base_r32

        cpu = self._cpu(xiy=RAM_DECOY, bank1_xbc=RAM_LIST)
        name, value, refusal = secondary_base_r32(cpu, 0xF4)
        self.assertIsNone(refusal)
        self.assertEqual((name, value), ("XIY", RAM_DECOY))

    def test_an_unnameable_code_refuses_instead_of_guessing(self) -> None:
        from core.execute import secondary_base_r32

        cpu = self._cpu(xiy=RAM_DECOY, bank1_xbc=RAM_LIST)
        _, value, refusal = secondary_base_r32(cpu, 0x80)   # register-file space we do not map
        self.assertIsNotNone(refusal, "a code we cannot name must REFUSE, not guess a register")
        self.assertIsNone(value)


if __name__ == "__main__":
    unittest.main()
