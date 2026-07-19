"""⛔ THERE IS NO "TILE 0 IS BLANK" RULE. Character 0 is a tile like any other.

The renderer used to carry this line:

    if entry.c_c == 0:
        continue  # tile-0 = transparent (NGPC convention)

That convention was INVENTED. The K2GE tech ref gives character code 0 no special
status: it is 16 bytes of character RAM, exactly like character 1 or character 511.
Transparency on this machine is per-PIXEL -- colour index 0 -- and never per-tile.

WHAT IT COST, and how it was found. The user reported two things about Sonic's boot:
"white patches behind the logo that stick out", and later "the background should fade
from black to white, and I don't hear the SEGAAA jingle either". Three symptoms, and
the tempting move was to treat them as three bugs.

The screen fills its whole background with tile 0, whose bytes are 0xAA -- packed 2bpp,
that is eight pixels of colour index 2, a solid field. We threw the field away and drew
only the logo's own tiles. So:

  * the "white patches" were the ONLY part of the background we drew -- the logo's
    bounding box -- with the discarded field showing through as backdrop black;
  * the "fade never happens" was the SAME bug: the fade was running perfectly all
    along (0xDDD at frame 90 -> 0xFFF at frame 200, confirmed against the oracle's own
    memory), we simply were not drawing the surface it was fading.

🔑 ONE ROOT CAUSE WORE TWO FACES, and I spent a long time hunting the second one as if
it were a separate defect -- reading the game's fade arithmetic instruction by
instruction while the real bug sat in our own renderer. The machine state was
BYTE-IDENTICAL to the oracle's the whole time; only the interpretation differed. When
two emulators agree on every byte and disagree on the picture, stop reading the game.
"""

from __future__ import annotations

import unittest

from core.renderer import render_frame

# A minimal K2GE scene: SCR1 on, one palette, the whole plane made of tile 0.
SCR1_MAP = 0x9000
SCR2_MAP = 0x9800
CHAR_RAM = 0xA000
SCR1_PAL = 0x8280          # K2GE scroll-1 colour palettes
CONTROL = 0x8000


def _scene(tile_for_the_whole_plane: int, tile_bytes: dict[int, bytes]) -> dict[int, int]:
    mem: dict[int, int] = {}

    # K2GE mode (not K1GE compat), display on.
    mem[0x87E2] = 0x00
    mem[CONTROL] = 0xC0
    mem[0x8118] = 0x80         # backdrop enabled, index 0 -> 0x83E0 = 0x000 = black
    mem[0x8012] = 0x00
    # The WINDOW, full screen. Left at zero it is 0x0 pixels wide and every pixel on the
    # screen is "outside the window" -- the planes are never consulted at all.
    mem[0x8002], mem[0x8003] = 0x00, 0x00     # window origin
    mem[0x8004], mem[0x8005] = 0xA0, 0x98     # 160 x 152

    # SCR1 palette 0: entry 2 = white (0x0FFF), so an index-2 pixel is unmistakable.
    mem[SCR1_PAL + 4] = 0xFF
    mem[SCR1_PAL + 5] = 0x0F

    # The whole 32x32 SCR1 plane is one tile -- the tile under test.
    for i in range(32 * 32):
        mem[SCR1_MAP + i * 2] = tile_for_the_whole_plane & 0xFF
        mem[SCR1_MAP + i * 2 + 1] = (tile_for_the_whole_plane >> 8) & 0x01

    # ...and SCR2 must be TRANSPARENT, or it covers SCR1 and this test measures nothing.
    # Point it at tile 1, whose 16 bytes are left at zero: every pixel is colour index 0.
    # (Pointing it at tile 0 -- the obvious "empty" choice -- is exactly the mistake the
    # renderer used to make: tile 0 is a real tile, and here it would paint the screen
    # with SCR2's own all-black palette.)
    for i in range(32 * 32):
        mem[SCR2_MAP + i * 2] = 0x01
        mem[SCR2_MAP + i * 2 + 1] = 0x00

    for index, data in tile_bytes.items():
        for i, b in enumerate(data):
            mem[CHAR_RAM + index * 16 + i] = b
    return mem


class TileZeroTests(unittest.TestCase):
    # 0xAA = packed 2bpp = 1010 1010 -> eight pixels of colour index 2. This is
    # literally the byte Sonic's background tile is made of.
    SOLID_INDEX_2 = bytes([0xAA] * 16)
    ALL_INDEX_0 = bytes(16)

    def _colors(self, mem: dict[int, int]) -> set[tuple[int, int, int]]:
        frame = render_frame(mem)
        return {(p.r, p.g, p.b) for row in frame.pixels for p in row}

    def test_TILE_ZERO_IS_DRAWN_like_any_other_tile(self) -> None:
        """The regression this file exists for. Tile 0 holds pixels; draw them."""
        mem = _scene(0, {0: self.SOLID_INDEX_2})
        self.assertIn(
            (15, 15, 15), self._colors(mem),
            "tile 0 was skipped -- but character 0 is a tile like any other, and the "
            "K2GE spec gives it no special status",
        )

    def test_the_same_tile_at_a_NON_zero_index_draws_identically(self) -> None:
        """Tile 0 and tile 7 carrying the same bytes must produce the same picture.

        If they differ, something is treating the INDEX as meaningful rather than the
        DATA -- which is exactly the bug.
        """
        zero = render_frame(_scene(0, {0: self.SOLID_INDEX_2}))
        seven = render_frame(_scene(7, {7: self.SOLID_INDEX_2}))
        self.assertEqual(
            [[(p.r, p.g, p.b) for p in row] for row in zero.pixels],
            [[(p.r, p.g, p.b) for p in row] for row in seven.pixels],
            "tile 0 renders differently from the same data at index 7",
        )

    def test_transparency_is_PER_PIXEL_not_per_tile(self) -> None:
        """A tile whose pixels are all colour index 0 IS transparent -- because of its
        DATA, not its index. That rule is real, and it must survive."""
        mem = _scene(0, {0: self.ALL_INDEX_0})
        self.assertEqual(
            self._colors(mem), {(0, 0, 0)},
            "an all-index-0 tile must show the backdrop through it",
        )


if __name__ == "__main__":
    unittest.main()
