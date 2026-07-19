"""Gate G2 — differential fuzz, Python reference core vs native C++ core.

Wired into pytest so it runs on every pass of the port, not only when someone
remembers to. See oracle_tools/native_diff.py for the design and the agreement
rules; specs/CPP_CORE_PORT.md §5 for why this gate exists.

The contract these tests pin:

  * on a family that IS ported, the two cores must agree on the FULL state —
    PC, 8 registers, 6 flags, IFF, RFP, memory writes, and cycle count;
  * a family that is NOT ported yet must show up as `todo`, never as agreement
    (a native core that silently NOPs an unknown opcode would "agree" its way
    through the whole port);
  * the harness itself must be able to convict. `test_harness_detects_a_planted
    _divergence` proves the comparison actually fires, so a run of zero
    divergences means something.
"""

from __future__ import annotations

import collections
import random
import tempfile
import unittest
from pathlib import Path

from core import native
from oracle_tools.native_diff import (
    ENTRY,
    Seed,
    Step,
    compare,
    make_rom,
    native_step,
    python_step,
)

def _ported() -> list[str]:
    """Encodings of every family ported so far. Grow this as the port advances;
    a family is not "done" until it is listed here and green."""
    ops = [
        "00",                                       # nop
        "205a", "2199", "2400", "26f0",             # ld R8,  #imm8
        "303412", "3778ff", "3200ff",               # ld R16, #imm16
        "43e00e2100", "47006c0000", "40ffffffff",   # ld R32, #imm32
        "02",                                       # push SR
        "0600", "0603", "0607",                     # ei #n / di (n == 7)
        "10", "11", "12", "13",                     # rcf / scf / ccf / zcf
        "1a3412", "1b34122000",                     # jp #16 / jp #24
        "1c3412", "1d00012000",                     # call #16 / call #24
        "0e", "0f0800", "0ff8ff",                   # ret / retd d16 (fwd + back)
        "1e0300", "1efdff",                         # calr (fwd + back)
    ]
    for cc in range(16):                            # jr cc,d8 and jrl cc,d16 — all 16 conditions
        ops += [f"{0x60 + cc:02x}05", f"{0x60 + cc:02x}fb", f"{0x70 + cc:02x}2001"]
    for r in range(7):                              # push/pop R32 (XSP excluded, see below)
        ops += [f"{0x38 + r:02x}", f"{0x58 + r:02x}"]
    for r in range(7):                              # push/pop R16
        ops += [f"{0x28 + r:02x}", f"{0x48 + r:02x}"]
    ops += ["08205a", "0800ff", "0a203412"]         # ld (#8),# — the I/O poke
    # --- the MEMORY-OPERAND families -------------------------------------------
    # One effective-address decoder x one sub-opcode table. Sub-ops: LD R,(mem),
    # EX (mem),R, the full ADD/ADC/SUB/SBC/AND/XOR/OR/CP matrix both ways,
    # INC/DEC #3,(mem), and the destination group (stores, LDA, JP/CALL cc, RET cc).
    for fb in (0x80, 0x90, 0xA0):                   # byte / word / long, mode (R) and (R+d8)
        subs = [0x20, 0x80, 0x88, 0x90, 0x98, 0xA0, 0xA8,
                0xB0, 0xB8, 0xC0, 0xC8, 0xD0, 0xD8, 0xE0, 0xE8, 0xF0, 0xF8]
        if fb != 0xA0:
            # EX (mem),R and INC/DEC #3,(mem) have NO LONG FORM: Toshiba gives them
            # the size column "BW-" (states "6. 6. -"). The native core refuses a
            # long one; the reference executes it anyway, on its 8-cycle
            # placeholder. Listing them here for long would assert a form that does
            # not exist.
            subs += [0x30, 0x61, 0x69]
        for sub in subs:
            ops += [f"{fb:02x}{sub:02x}", f"{fb + 8:02x}04{sub:02x}"]
    for fb in (0xC0, 0xD0, 0xE0):                   # abs8
        for sub in (0x20, 0x80, 0xC0, 0xF0):
            ops.append(f"{fb:02x}50{sub:02x}")
    for fb in (0xC1, 0xD1, 0xE1):                   # abs16 (0xC1 alone blocked 32/66 ROMs)
        for sub in (0x20, 0x80, 0xC0, 0xF0):
            ops.append(f"{fb:02x}0050{sub:02x}")
    ops += ["b04000", "b05000", "b06000",           # LD (mem),R — the stores
            "b02000", "b03000",                     # LDA
            "b00011", "f0504000"]
    # --- the REGISTER-DIRECT family (C8+zz+r) ---------------------------------
    for fb, imm in ((0xC8, "ff"), (0xD8, "ff11"), (0xE8, "ff112233")):
        for sub in (0x88, 0x98, 0xA8, 0x80, 0x90, 0xA0, 0xB0,
                    0xC0, 0xD0, 0xE0, 0xF0, 0xB8, 0xD8):
            ops.append(f"{fb:02x}{sub:02x}")
        ops.append(f"{fb:02x}03{imm}")                       # LD r, #
        for k in range(8):                                   # ALU r, #
            ops.append(f"{fb:02x}{0xC8 + k:02x}{imm}")
        for sub in (0x04, 0x05, 0x06, 0x07, 0x12, 0x13,      # PUSH/POP/CPL/NEG/EXTZ/EXTS
                    0x61, 0x69, 0x67, 0x6F):                 # INC/DEC #3
            ops.append(f"{fb:02x}{sub:02x}")
        # SCC and DJNZ are BYTE and WORD only ("2. 2. -" / "BW-"). Listing them
        # for long would assert a form the datasheet does not give.
        if fb != 0xE8:
            for sub in (0x70, 0x76, 0x7E):
                ops.append(f"{fb:02x}{sub:02x}")
            ops.append(f"{fb:02x}1c05")                      # DJNZ
        ops += [f"{fb:02x}2e20", f"{fb:02x}2f20"]            # LDC cr,r / LDC r,cr
    ops += ["e80c0800", "e80d"]                              # LINK / UNLK (long only)
    ops += ["1700", "1701", "1702", "1703", "0c", "0d"]      # ldf #3 / incf / decf — the bank window
    ops += ["03", "07"]                                      # pop SR / reti (21 ROMs)
    ops += ["0b1234", "14", "15", "18", "19"]                # pushw #16 / push-pop A / push-pop F
    # MUL / MULS / DIV / DIVS  rr, #  (29 ROMs).
    # The WORD divides are not in the random-seed gate: with a random 32-bit XWA
    # the quotient overflows almost every time, and on overflow the datasheet says
    # the destination is UNDEFINED -- so the native core stops honestly, while the
    # reference's word path executes anyway (its own BYTE path stops, so it is
    # inconsistent with itself). Word divide is checked separately, with a divisor
    # that keeps the quotient in range.
    # The byte forms name their destination with an ODD code -- `mul WA,7` is
    # `C9 08 07`, and the official assembler will not emit `C8 08 ..` at all. This
    # list used to carry the C8 form, and BOTH cores executed it identically, which
    # is exactly the bug a differential gate cannot see. Fuzz the four real ones.
    for fb in (0xC9, 0xCB, 0xCD, 0xCF):                      # dst = WA / BC / DE / HL
        for k in (0x08, 0x09, 0x0A, 0x0B):
            ops.append(f"{fb:02x}{k:02x}07")
    ops += ["d808e800", "d809e800"]                          # word MUL / MULS
    # MUL / DIV RR, (mem)  (11 ROMs). Byte forms only: the reference MIS-DECODES
    # the whole `D5` word family (it reads `d5 ec 4d` as `sla 13, IY`, while asm900
    # says `muls XIY,(XHL+)` = `D5 ED 4D` and that the real `sla 13,IY` is
    # `DD EC 0D`). Its D0..D7 re-decode is a known open chantier (DEVLOG pass 155),
    # so the word forms cannot be gated against it -- they are verified against the
    # assembler instead, not listed here.
    for k in (0x45, 0x4D, 0x55, 0x5D):
        ops.append(f"c5ec{k:02x}")
    for k in range(0x78, 0x80):                              # shifts on a memory operand
        ops += [f"80{k:02x}", f"c050{k:02x}"]
    ops += ["8004", "9004", "c05004", "d1005004"]            # PUSH (mem)  (23 ROMs)
    ops += ["b004", "b006", "f05004"]                        # POP (mem)
    ops += ["8019a050", "9019a050"]                          # LD (nn),(mem)
    # the block instructions. NOTE: the repeating forms are NOT covered here --
    # with a random 16-bit BC they would copy tens of thousands of bytes per case.
    # They are exercised in test_native_core.py with a controlled BC.
    ops += ["8310", "8312", "8314", "8316", "9310", "9314", "8510"]
    for sub in (0x30, 0x31, 0x32, 0x33, 0x34):               # RES/SET/CHG/BIT/TSET #4, r
        for fb in (0xC8, 0xD8, 0xE8):
            ops.append(f"{fb:02x}{sub:02x}03")
    # the destination-group BIT / CARRY-BIT ops (andcf stopped 10 ROMs)
    for k in (0x86, 0x8E, 0x96, 0x9E, 0xA6, 0xAE, 0xB6, 0xBE, 0xC6, 0xCE):
        ops += [f"f10050{k:02x}", f"b0{k:02x}", f"f050{k:02x}"]
    # The EXTENDED register escapes (C7/D7/E7). The register code is a full byte,
    # so it can name IXL/QA/RW3 etc. Encodings verified with the OFFICIAL TOSHIBA
    # ASSEMBLER (asm900_oracle) -- ngdis and the reference are both blind to them.
    ops += ["c7f0a9",    # ld IXL, 1
            "c7e2a9",    # ld QA, 1
            "c731a9",    # ld RW3, 1   <- stopped 14 of the 66 commercial ROMs
            "c731fb",    # rr A, RW3
            "d7e2a9",    # ldw QWA, 1
            "d730a9",    # ldw RWA3, 1
            "e730a9"]    # ld  XWA3, 1
    for fb in (0xC8, 0xD8, 0xE8):
        pass
        for k in range(8):                                   # shift r,#4 -- 00 means SIXTEEN
            ops += [f"{fb:02x}{0xE8 + k:02x}03", f"{fb:02x}{0xE8 + k:02x}00"]
        for k in range(8):                                   # shift r, A
            ops.append(f"{fb:02x}{0xF8 + k:02x}")
    # --- the families the interrupt controller flushed out ---------------------
    # Delivering interrupts made the core execute the BIOS's own handlers for the
    # first time, and they use encodings no cart body ever reached. Every encoding
    # below was confirmed byte-for-byte with the OFFICIAL TOSHIBA ASSEMBLER.
    ops += ["c910", "cd10"]                                  # daa A / daa E  (BYTE only)

    # LD (mem),(#16) -- a memory-to-memory move. `ld (0xB2),(0x6E85)` is what the
    # BIOS interrupt handler runs; it stopped 54 of the 73 ROMs on day one.
    ops += ["f0b214856e",    # ld  (0xB2),(0x6E85)
            "f0b216856e",    # ldw (0xB2),(0x6E85)
            "bb1014004a",    # ld  (XHL+16),(0x4A00)
            "b414004a"]      # ld  (XIX),(0x4A00)

    # MUL / MULS / DIV / DIVS  RR, r -- the REGISTER-operand forms. The `RR` code
    # is NOT an array index: at word size it names a LONG register (000..111 =
    # XWA..XSP), at byte size a WORD one, and only the ODD codes exist there
    # (001 = WA, 011 = BC, 101 = DE, 111 = HL). Fuzz every legal destination, so a
    # core that reads the code as an index cannot hide -- one did.
    for base in (0x40, 0x48, 0x50, 0x58):
        for code in (1, 3, 5, 7):                  # byte: dst = WA / BC / DE / HL
            ops.append(f"c9{base + code:02x}")     #   src = A
        for code in range(8):                      # word: dst = XWA .. XSP
            ops.append(f"d9{base + code:02x}")     #   src = BC

    # MINC / MDEC -- the ring-buffer primitive. The immediate is `modulus - step`,
    # NOT the modulus: `minc1 16,BC` carries 15.  (`d8` = word, r = 0 -> WA.)
    ops += ["d9380f00", "d9390e00", "d93a0c00",   # minc1 / minc2 / minc4, modulus 16
            "d93c0f00", "d93d0e00", "d93e0c00"]   # mdec1 / mdec2 / mdec4, modulus 16

    # The carry-bit group in its REGISTER form (the memory form is already above).
    for sub in (0x20, 0x21, 0x22, 0x23, 0x24):   # ANDCF/ORCF/XORCF/LDCF/STCF #4, r
        ops += [f"c9{sub:02x}03", f"d9{sub:02x}0b"]
    for sub in (0x28, 0x29, 0x2A, 0x2B, 0x2C):   # ...and with the bit number in A
        ops += [f"c9{sub:02x}", f"d9{sub:02x}"]

    return ops


PORTED = _ported()

# Families deliberately NOT ported yet — they must report `todo`, not `agree`.
# 0xC1 is the top blocker on the real corpus (it stops 32 of 66 commercial ROMs).
# Encodings the reference executes and the native core does NOT yet. They must
# report `todo` -- never `agree`, which is what a core that silently NOPed unknown
# opcodes would do.
# Everything that used to sit here -- `mul RR,r`, `div RR,r`, `minc2` -- is ported
# now, along with DAA, the bit/carry-bit group and `LD (mem),(#16)`. The list is
# empty, and that is the point: it is the port's own scoreboard. Leave it here.
# The moment a new family is found un-ported, its encoding goes in and this gate
# proves the core says `todo` about it instead of quietly agreeing.
UNPORTED: list[str] = []

# Deliberately DECLINED, on both sides: `push XSP` / `pop XSP` alias onto the very
# register the instruction mutates, so the result depends on whether silicon
# latches XSP before or after the pre-decrement. The Python reference refuses them
# (`unmodeled-stack-pointer-alias`) and the native core must not be more capable
# than its reference. Settle against hardware, then port on both sides at once.
DECLINED = ["3f", "5f"]


@unittest.skipUnless(native.available(), "native core not built (cmake --build cpp/build)")
class NativeDiffGateTests(unittest.TestCase):
    def _run(self, encodings: list[str], *, cases: int, rng_seed: int = 99) -> dict[str, int]:
        rng = random.Random(rng_seed)
        tally: dict[str, int] = collections.defaultdict(int)
        self.detail: list[str] = []

        with tempfile.TemporaryDirectory() as tmp:
            rom_path = Path(tmp) / "gate.ngc"
            for _ in range(cases):
                body = bytes.fromhex(rng.choice(encodings))
                seed = Seed.random(rng)
                rom_bytes = make_rom(body)
                rom_path.write_bytes(rom_bytes)

                verdict = compare(
                    python_step(rom_path, seed, ENTRY),
                    native_step(rom_bytes, seed, ENTRY),
                    body=body,
                )
                tally[verdict.kind] += 1
                if verdict.kind == "DIVERGENCE":
                    self.detail.append(f"{body.hex()}: " + "; ".join(verdict.fields))
        return tally

    def test_ported_families_agree_on_the_full_state(self) -> None:
        """Zero DIVERGENCE across every ported family.

        Note what is deliberately NOT asserted: that every case is a plain
        `agree`. On the memory family it cannot be, and that is exactly what the
        other verdicts are for. The reference omits the addressing-mode cycle
        adder, bills a flat 8 wherever its table has no row, keeps writes it
        should have discarded, still cries "silicon-broken" on a quirk the project
        already retracted, and refuses 768 encodings the OFFICIAL TOSHIBA
        ASSEMBLER happily emits. Each of those is a PROVEN reference defect with
        its own named bucket -- not a port bug, and not something to bury by
        copying it.

        What must be zero is the bucket that means "one of us is wrong and we do
        not know which one".
        """
        tally = self._run(PORTED, cases=300)
        self.assertEqual(
            tally["DIVERGENCE"], 0, "\n".join(self.detail[:10]) or "unexpected divergence"
        )
        self.assertEqual(tally["todo"], 0, "a family listed as PORTED is not actually ported")
        self.assertGreater(tally["agree"], 0)

    def test_stack_pointer_alias_is_declined_by_both_cores(self) -> None:
        """`push XSP` / `pop XSP`: the reference declines, so we decline. A native
        core that is MORE capable than its reference has invented a behaviour."""
        tally = self._run(DECLINED, cases=20)
        self.assertEqual(tally["both-refuse"], 20)
        self.assertEqual(tally["agree"], 0)
        self.assertEqual(tally["DIVERGENCE"], 0)

    def test_unported_families_report_todo_not_agreement(self) -> None:
        """A core that silently NOPed unknown opcodes would 'agree' its way
        through the entire port. It must decline instead.

        We assert the SHAPE, not a count: how these split between `todo` (the
        reference executes, we don't yet) and `both-refuse` (neither core models
        this encoding) depends on what the Python core happens to model, which is
        not this test's business. What IS this test's business: the native core
        must never claim to have executed one of them.
        """
        if not UNPORTED:
            self.skipTest(
                "no family is un-ported right now. The list is empty ON PURPOSE and "
                "this test stays: the next family found missing goes in it, and the "
                "gate proves the core says `todo` about it rather than quietly agreeing."
            )
        cases = 30
        tally = self._run(UNPORTED, cases=cases)
        self.assertEqual(tally["agree"], 0, "the native core executed an unported opcode")
        self.assertEqual(tally["DIVERGENCE"], 0)
        self.assertEqual(tally["todo"] + tally["both-refuse"], cases)
        # At least one case must exercise the decline path itself (reference
        # executes, native core says "not ported"), or this test is vacuous.
        self.assertGreater(tally["todo"], 0)

    def test_harness_detects_a_planted_divergence(self) -> None:
        """The tribunal must be able to convict.

        A gate that cannot fail proves nothing, so we hand `compare()` two
        results that differ by one register and assert it says so. (The same
        check was run end-to-end against a real planted bug in set_r8() during
        phase 0: 60/60 convictions.)
        """
        base = dict(
            status="executed",
            executed=True,
            pc=0x200105,
            regs=(1, 2, 3, 4, 5, 6, 7, 8),
            flags=(False,) * 6,
            iff_level=7,
            rfp=0,
            writes=(),
            cycles=6,
        )
        good = Step(**base)
        self.assertEqual(compare(good, Step(**base)).kind, "agree")

        for field, bad_value in (
            ("regs", (1, 2, 3, 0xDEAD, 5, 6, 7, 8)),
            ("pc", 0x200106),
            ("cycles", 8),
            ("flags", (True, False, False, False, False, False)),
            ("writes", ((0x4000, "ff", False),)),
        ):
            verdict = compare(good, Step(**{**base, field: bad_value}))
            self.assertEqual(verdict.kind, "DIVERGENCE", f"{field} slipped through the gate")
            self.assertTrue(verdict.fields)


if __name__ == "__main__":
    unittest.main()
