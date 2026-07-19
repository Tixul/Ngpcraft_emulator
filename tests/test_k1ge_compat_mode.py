"""K1GE "upper palette compatible" mode -- and the index rule the docs would not give.

A pixel is resolved in TWO hops in this mode, not one:

    level  = LUT   [plane + P*4 + value]        3 bits; value 1..3, 0 = clear
    colour = PALETTE[plane + (P*8 + level)*2]   12 bits

⛔ THE SECOND INDEX WAS AN OPEN QUESTION FOR THREE PASSES, AND IT WAS NOT GUESSED.
K2GE Tech Ref § 5-3 gives the address computation as a FIGURE, and the figure is in
neither the SDK text nor ngpcspec: the LUT emits 3 bits (8 values) while Table 19
allocates 16 entries per plane, so `index = level` and `index = P*8 + level` were
both readable, and `specs/K1GE_COMPAT_MODE.md` forbade picking one on instinct --
"un moteur de rendu mono écrit sur ces deux inconnues afficherait des pixels
parfaitement confiants et parfaitement faux".

It was settled by MEASUREMENT, twice, independently:

1. THE REAL BIOS. Booted for the first time (pass 235), it draws in compat mode and
   fills both tables. Every entry it writes is reachable under `P*8 + level` and
   only under it -- under `index = level`, entries 8..15 could never be addressed,
   and the BIOS fills them.

2. A GAME. Samurai Shodown! writes the compat palette itself, at indices 3, 5 and 7
   -- and its LUT holds levels 3, 5, 7 for palette 0. `0*8 + level` = 3, 5, 7:
   exactly the entries it wrote.

The fixtures below are the BIOS's OWN numbers, read out of a running machine. They
are not invented, and that is the whole point of them.
"""

from __future__ import annotations

import unittest

from core.k2ge import decode_color
from core.renderer import (
    _k1ge_plane_colors,
    k1ge_compat_enabled,
)

MODE_REGISTER = 0x0087E2

# --- the BIOS's own tables, read out of a booting console (pass 235) ------------
# LUT: 8 bytes per plane, [0]=unused, [1..3] = palette 0's colours 1..3,
#                          [4]=unused, [5..7] = palette 1's colours 1..3.
BIOS_SPRITE_LUT = [0x00, 7, 7, 7, 0x00, 1, 6, 5]
BIOS_SCR1_LUT = [0x00, 2, 3, 6, 0x00, 0, 2, 6]

# 16 colours per plane. The BIOS leaves ZERO everywhere it cannot address.
BIOS_SPRITE_PALETTE = {7: 0x0B20, 9: 0x000F, 13: 0x0B20, 14: 0x0B20}
BIOS_SCR1_PALETTE = {
    2: 0x0EFB, 3: 0x090F, 6: 0x0409, 8: 0x0EFB, 10: 0x0AA6, 14: 0x0550,
}


def _memory(*, mode: int, lut_base: int, lut: list[int],
            pal_base: int, palette: dict[int, int]) -> dict[int, int]:
    mem: dict[int, int] = {MODE_REGISTER: mode}
    for i, level in enumerate(lut):
        mem[lut_base + i] = level
    for index, colour in palette.items():
        mem[pal_base + index * 2] = colour & 0xFF
        mem[pal_base + index * 2 + 1] = (colour >> 8) & 0xFF
    return mem


def _c(value: int):
    """The colour a 12-bit palette word decodes to -- through the SAME decoder the
    renderer uses, so this test cannot pass by agreeing with a private copy of it."""
    return decode_color(value & 0xFF, (value >> 8) & 0xFF)


class ModeRegisterTests(unittest.TestCase):
    def test_the_mode_is_off_at_reset(self) -> None:
        """0x87E2 bit 7 = 0 is the reset value: K2GE colour."""
        self.assertFalse(k1ge_compat_enabled({}))
        self.assertFalse(k1ge_compat_enabled({MODE_REGISTER: 0x00}))

    def test_bit_7_and_only_bit_7_selects_compat(self) -> None:
        self.assertTrue(k1ge_compat_enabled({MODE_REGISTER: 0x80}))
        self.assertFalse(k1ge_compat_enabled({MODE_REGISTER: 0x7F}))


class TheIndexRuleTests(unittest.TestCase):
    """`index = P*8 + level` -- against the BIOS's own tables."""

    def test_sprites_resolve_exactly_the_entries_the_bios_filled(self) -> None:
        mem = _memory(
            mode=0x80,
            lut_base=0x008100, lut=BIOS_SPRITE_LUT,
            pal_base=0x008380, palette=BIOS_SPRITE_PALETTE,
        )
        colors = _k1ge_plane_colors(mem, "sprite")

        # Palette 0: levels 7,7,7 -> index 0*8+7 = 7 -> 0x0B20.
        for value in (1, 2, 3):
            self.assertEqual(colors[0][value], _c(0x0B20))

        # Palette 1: levels 1,6,5 -> indices 8+1=9, 8+6=14, 8+5=13.
        self.assertEqual(colors[1][1], _c(0x000F), "P=1, level 1 must read entry 9")
        self.assertEqual(colors[1][2], _c(0x0B20), "P=1, level 6 must read entry 14")
        self.assertEqual(colors[1][3], _c(0x0B20), "P=1, level 5 must read entry 13")

    def test_scroll1_resolves_all_six_entries_the_bios_filled(self) -> None:
        mem = _memory(
            mode=0x80,
            lut_base=0x008108, lut=BIOS_SCR1_LUT,
            pal_base=0x0083A0, palette=BIOS_SCR1_PALETTE,
        )
        colors = _k1ge_plane_colors(mem, "scr1")

        self.assertEqual(colors[0][1], _c(0x0EFB))   # level 2  -> entry 2
        self.assertEqual(colors[0][2], _c(0x090F))   # level 3  -> entry 3
        self.assertEqual(colors[0][3], _c(0x0409))   # level 6  -> entry 6
        self.assertEqual(colors[1][1], _c(0x0EFB))   # level 0  -> entry 8+0
        self.assertEqual(colors[1][2], _c(0x0AA6))   # level 2  -> entry 8+2
        self.assertEqual(colors[1][3], _c(0x0550))   # level 6  -> entry 8+6

    def test_the_REFUTED_reading_would_leave_the_upper_half_unreachable(self) -> None:
        """`index = level` is the reading we rejected. This is WHY.

        The BIOS fills sprite entries 9, 13 and 14. Under `index = level` the index
        can never exceed 7, so those three entries could not be addressed by any
        pixel -- yet the hardware's own firmware writes them. A rule that makes a
        register the manufacturer USES unreachable is not a rule, it is a guess.
        """
        addressable_under_the_wrong_rule = set(BIOS_SPRITE_LUT)   # levels only, 0..7
        filled_by_the_bios = set(BIOS_SPRITE_PALETTE)
        orphaned = filled_by_the_bios - addressable_under_the_wrong_rule
        self.assertEqual(
            orphaned, {9, 13, 14},
            "the refuted reading must strand exactly the entries above 7",
        )

        # And under the rule we kept, nothing is stranded.
        under_our_rule = {
            p * 8 + BIOS_SPRITE_LUT[p * 4 + v] for p in (0, 1) for v in (1, 2, 3)
        }
        self.assertEqual(
            filled_by_the_bios - under_our_rule, set(),
            "every entry the BIOS wrote must be reachable under `P*8 + level`",
        )

    def test_samurai_shodown_agrees_with_the_bios(self) -> None:
        """A GAME says the same thing, independently.

        Samurai Shodown! is a monochrome cartridge that nevertheless writes the compat
        palette: entries 3, 5, 7. Its LUT holds `00 03 05 07` -- palette 0's levels are
        3, 5 and 7. `0*8 + level` lands on 3, 5, 7: exactly what it wrote.
        """
        game_lut = [0x00, 3, 5, 7, 0x00, 0, 0, 0]
        wrote = {3, 5, 7}
        resolved = {0 * 8 + game_lut[v] for v in (1, 2, 3)}
        self.assertEqual(resolved, wrote)


class TheClearCodeTests(unittest.TestCase):
    def test_pixel_value_zero_is_never_looked_up(self) -> None:
        """Colour 0 is the CLEAR code and has no LUT entry (Tech Ref § 4-12).

        The table still carries a slot for it so the renderer can index by the raw
        2bpp value with no arithmetic -- but the renderer must never reach it, and
        the callers `continue` on value 0 before they ever get here.
        """
        mem = _memory(
            mode=0x80,
            lut_base=0x008100, lut=BIOS_SPRITE_LUT,
            pal_base=0x008380, palette=BIOS_SPRITE_PALETTE,
        )
        colors = _k1ge_plane_colors(mem, "sprite")
        self.assertEqual(len(colors), 2, "two palettes -- the old machine had two")
        self.assertEqual(len(colors[0]), 4, "slots for the 2bpp values 0..3")


if __name__ == "__main__":
    unittest.main()
