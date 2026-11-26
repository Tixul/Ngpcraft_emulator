"""An open-loop HiColor copier: its 19 raster blocks must stay IN PHASE.

The reference cartridge (a homebrew title screen) shows a 146-colour picture on a
16-colour machine by rewriting 20 characters and both scroll palettes every 8 scanlines,
double-buffered: while a band displays bank A, the copier fills bank B for the band after
it. The copy routine syncs ONCE on line 0 and then runs 19 blocks of 224 `ldirw` words
WITHOUT polling again, so every block must cost exactly one 8-line slice
(8 x 515 = 4120 cycles).

⚖️ WHY THIS IS A BETTER INSTRUMENT THAN A FRAME-RATE AVERAGE. A per-block error does not
average out, it ACCUMULATES against a starting margin of 73 cycles: 34 cycles too fast
per block and by the fourth block the copy has walked back into the LAST line of the band
still displaying the bank it is overwriting. That is a hard, legible failure -- one
corrupted line per band -- with no tolerance to argue about.

⛔ AND IT WAS BROKEN TWICE, THE SAME WAY: a timing change shipped without re-running it.
2026-08-06 `ldirw_cost` (billed per byte instead of per iteration); 2026-08-2x the silicon
recalibration, which let the bus interface unit prefetch through a block copy and handed
back 48 cycles a block. Hence this test: the ROM is not in the repository, so the check
skips where it is absent -- but where it is present nothing can move that phase silently.
Point `NGPC_HICOLOR_ROM` at the cartridge; its savestate is `savestates/<rom name>.s0`,
taken on the title screen.

⚠️ THE ASSERTION IS THE PHASE, NOT A PIXEL HASH. It says what the hardware requires --
every copy starts in the FIRST line of a band -- so a failure names the fault instead of
reporting that some picture changed.
"""

from __future__ import annotations

import unittest
import os
from pathlib import Path

from core import native
import ngpc_native as N

from rom_paths import BIOS, REPO

_ROM = Path(os.environ.get("NGPC_HICOLOR_ROM", ""))
ROM = _ROM if os.environ.get("NGPC_HICOLOR_ROM") and _ROM.is_file() else None
STATE = REPO / "savestates" / f"{ROM.stem if ROM else '-'}.s0"

# Character RAM. Bank A = the 20 characters SCR2 shows on lines 0-7, 16-23, ...;
# bank B = the 20 it shows on lines 8-15, 24-31, ... (the game builds the map that way).
BANK_A_FIRST_WORD = 0xA000
BAND = 8
VISIBLE_LINES = 152      # the picture; a refill past it lands in V-blank and shows nobody


@unittest.skipUnless(native.available(), "native core not built (cmake --build cpp/build)")
@unittest.skipUnless(ROM is not None, "NGPC_HICOLOR_ROM not set")
@unittest.skipUnless(STATE.exists(), "no title-screen savestate for NGPC_HICOLOR_ROM")
class HiColorCopierPhaseTests(unittest.TestCase):

    def _copy_starts(self) -> list[tuple[int, int]]:
        """(scanline, cycle) of every bank-A refill during one title-screen frame."""
        m = native.NativeMachine(ROM.read_bytes(),
                                 bios=BIOS.read_bytes() if BIOS.exists() else None)
        N.apply_timing(m, "silicon")
        m.reset(bios_handoff=True)
        N.load_state(m, STATE)
        for _ in range(3):                     # settle: the copier is mid-picture
            m.write(0x00B0, bytes([0]))
            m.run_frames(1)
        m.set_event_log(BANK_A_FIRST_WORD, BANK_A_FIRST_WORD)
        m.write(0x00B0, bytes([0]))
        m.run_frames(1)
        starts = [(e.scanline, e.cycle) for e in m.event_log(4096)
                  if e.addr == BANK_A_FIRST_WORD and e.scanline < VISIBLE_LINES]
        m.close()
        return starts

    def test_every_refill_starts_in_the_first_line_of_a_band(self) -> None:
        starts = self._copy_starts()
        self.assertGreaterEqual(len(starts), 8, "the title screen's copier did not run")
        late = [(line, cycle) for line, cycle in starts if line % BAND != 0]
        self.assertEqual(
            late, [],
            "a character-RAM refill began in the middle of a band: "
            f"{late}. The copier has drifted off the 4120-cycle slice, so it is "
            "overwriting the bank the beam is still reading -- one corrupted line per "
            "band. Check what changed the cost of a block copy or of the code around it "
            "(ldirw_cost, block_drains_queue, the fetch model).",
        )

    def test_the_block_cost_is_the_hardware_slice(self) -> None:
        """19 blocks over the 152 visible lines: 8 lines each, and never FASTER.

        Being a little slow is harmless -- the write stays behind the beam and the block
        has slack at its end. Being fast is fatal, which is why the lower bound is hard
        and the upper one is loose.
        """
        starts = self._copy_starts()
        steps = [(b[0] - a[0]) * 515 + (b[1] - a[1]) for a, b in zip(starts, starts[1:])]
        steps = [s for s in steps if 0 < s < 40_000]
        self.assertTrue(steps, "no consecutive refills to measure")
        slice_cycles = 2 * BAND * 515          # two blocks between two bank-A refills
        worst = min(steps)
        self.assertGreaterEqual(
            worst, slice_cycles,
            f"two blocks took {worst} cycles against the hardware's {slice_cycles}: the "
            "copier is running FAST, and the error accumulates until it overtakes the "
            "beam. (Measured with the drain armed: 8268, i.e. 1.0034x.)",
        )
        self.assertLess(worst, slice_cycles * 1.02, "the copier has fallen far behind")
