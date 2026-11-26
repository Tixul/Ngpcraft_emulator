"""A repeating block transfer leaves the instruction QUEUE EMPTY, and a game proves it.

The TLCS-900's bus interface unit runs ahead of the execution unit, so an instruction's
fetch usually overlaps the one before it (`fetch_pipelined`, `biu_slack` = one 4-byte
queue). A REPEATING block copy is the exception: LDIR/LDIRW own the bus for their whole
run -- a read and a write every iteration -- so nothing can be prefetched behind them,
and the instruction after one pays its fetch in full.

⛔ WITHOUT THIS, THE MODEL HANDED OUT A FREE QUEUE PER BLOCK COPY, and the reference HiColor copier
(a 2004 homebrew title screen) convicted it. It syncs ONCE on line 0 and then runs 19 blocks
of 224 `ldirw` words open-loop, so each block must cost exactly one 8-scanline slice
(8 x 515 = 4120 cycles) or the picture shears. Measured on the player's own save state:

    without the drain   4086 cycles/block (0.9917x)   -- 34 cycles too FAST, every block
    with it             4134 cycles/block (1.0034x)   -- the closest ever measured

The 34 cycles ACCUMULATE: the copy starts 34 cycles earlier each block against a starting
margin of 73, so from the fourth block on it writes into the LAST line of the band still
displaying the bank it is overwriting. One corrupted line per band, 15 bands -- which is
what the player reported, and what `test_hicolor_copier_phase` pins.
"""

from __future__ import annotations

import unittest
from pathlib import Path

from core import native
import ngpc_native as N

REPO = Path(__file__).resolve().parents[1]
SPIN, BLOCK, AFTER_BLOCK = 0x200040, 0x200044, 0x200046
REG_XBC, REG_XIX, REG_XIY = 1, 4, 5
# `biu_slack` in the armed silicon model: one queue = two words at the calibrated word
# cost (core.cpp, ngpc_set_timing_silicon). The drain can never be worth more than this.
ONE_QUEUE = 16


def _rom() -> bytes:
    """`ldirw (XIX+),(XIY+)` at 0x200044, then a run of `ld XWA,#imm32`, then `jr $`.

    `95 11` is LDIRW (XIX+),(XIY+) -- the encoding tests/test_decode.py and
    tests/test_execute.py already pin. Entering at 0x200046 instead skips the copy and
    runs the SAME loads, which is the control: no block, no drain, no difference.

    ⛔ LES SUIVANTES ETAIENT DES `nop`, ET LA SONDE EST DEVENUE AVEUGLE. Depuis que le
    fetch est facture par OCTET (`fetch_wait_byte_q16`, mesure directe ROM v14 page 1),
    un `nop` coute exactement 4,0 cycles de bus pour 4,0 cycles d'execution : il est PILE
    a l'equilibre, la file ne prend jamais d'avance derriere lui, et vider un credit qui
    n'existe pas ne coute rien. Les deux mesures tombaient sur le meme nombre et ce test
    echouait alors que le mecanisme, lui, marchait toujours -- `test_hicolor_
    copier_phase`, l'ancrage silicium REEL (4120 cycles par bande), est reste vert tout du long.

    ⇒ `ld XWA,#imm32` : 5 octets pour 5 etats, soit 20 cycles de bus contre 10
    d'execution. Franchement fetch-bound, donc il DEPENSE le credit d'avance -- et c'est
    exactement ce qu'un drain lui retire. Meme lecon que la ROM v13 : une sonde faite
    d'instructions a l'equilibre ne mesure rien.
    """
    rom = bytearray(b"\xFF" * 0x100000)
    rom[0:28] = b" LICENSED BY SNK CORPORATION"
    rom[0x1C:0x20] = SPIN.to_bytes(4, "little")
    rom[0x23] = 0x10
    rom[0x40:0x44] = b"\x00\x68\xFE\x00"          # nop ; jr $   (the reset vector parks)
    rom[0x44:0x46] = b"\x95\x11"                  # ldirw (XIX+),(XIY+)
    rom[0x46:0x78] = b"\x40\x01\x01\x01\x01" * 10  # ld XWA,# : ils payent le fetch
    rom[0x78:0x7A] = b"\x68\xFE"                  # jr $
    return bytes(rom)


@unittest.skipUnless(native.available(), "native core not built (cmake --build cpp/build)")
class BlockCopyDrainsTheQueueTests(unittest.TestCase):

    def _cycles(self, *, drain: bool, with_block: bool, instrs: int = 12) -> int:
        m = native.NativeMachine(_rom())
        m.set_timing_silicon(*N.SILICON_TIMING.values())
        m.set_block_drains_queue(drain)
        m.reset(bios_handoff=True)
        cpu = m.cpu()
        cpu.pc = BLOCK if with_block else AFTER_BLOCK
        cpu.regs[REG_XBC] = 0x0040                # words to copy
        cpu.regs[REG_XIX] = 0x007000
        cpu.regs[REG_XIY] = 0x007800
        m.set_cpu(cpu)
        summary = m.run_frames(1, max_instrs=instrs)
        m.close()
        return int(summary.total_cycles)

    def test_the_instructions_after_a_block_copy_pay_their_own_fetch(self) -> None:
        without = self._cycles(drain=False, with_block=True)
        with_it = self._cycles(drain=True, with_block=True)
        self.assertGreater(
            with_it, without,
            "with the drain armed the nops after the copy must cost MORE: without it "
            "they were spending a queue the block transfer never had a slot to fill.",
        )
        self.assertLessEqual(
            with_it - without, ONE_QUEUE,
            "the drain can only ever be worth the queue the BIU had run ahead by "
            f"({ONE_QUEUE} cycles); more than that means it is billing something else.",
        )

    def test_code_with_no_block_copy_is_untouched(self) -> None:
        """The control. Same nops, entered past the copy: the knob must do nothing.

        This is what makes the change attributable -- a timing knob that moves code it
        has no business moving cannot be defended by any measurement.
        """
        self.assertEqual(
            self._cycles(drain=False, with_block=False),
            self._cycles(drain=True, with_block=False),
        )

    def test_the_silicon_model_arms_it_and_legacy_clears_it(self) -> None:
        """One call must DEFINE the machine, not add to it (core.cpp says so in as many
        words). So the model that measured this carries it, and the A/B model drops it."""
        m = native.NativeMachine(_rom())
        m.reset(bios_handoff=True)
        cpu_pc = BLOCK
        def cost() -> int:
            c = m.cpu(); c.pc = cpu_pc
            c.regs[REG_XBC] = 0x0040; c.regs[REG_XIX] = 0x007000; c.regs[REG_XIY] = 0x007800
            m.set_cpu(c)
            return int(m.run_frames(1, max_instrs=12).total_cycles)
        m.set_timing_silicon(*N.SILICON_TIMING.values())
        armed = cost()
        m.set_timing_silicon(*N.SILICON_TIMING.values())
        m.set_block_drains_queue(False)
        cleared = cost()
        m.close()
        self.assertGreater(
            armed, cleared,
            "set_timing_silicon must leave the drain ON -- it is part of the model that "
            "was measured against the reference copier's 4120-cycle slice, not an optional extra.",
        )
