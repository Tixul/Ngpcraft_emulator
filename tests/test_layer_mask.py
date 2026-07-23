"""The debug LAYER MASK — show/hide a plane or a sprite priority group in the picture.

The video counterpart of the APU's channel mute. On this machine text and artwork are
always on different layers (the chip has no other way to superimpose them), so hiding a
layer is how you find out which one owns what, and how a clean background plate comes
out of a game without editing its VRAM.

Three things have to hold, and each is tested to CONDEMN:

1. **Default is the real picture.** Both cores start at `LAYER_ALL`. If this ever slips,
   every image gate in this repo silently starts measuring the mask instead of the core.
2. **One concept, not two.** The mask means the same bits in `cpp/src/render.cpp` and in
   `core/renderer.py`. A mask that meant different things in the two cores would make the
   differential gate compare two different pictures and call it agreement -- the exact
   failure that let the passe-230 rcode bug survive 72 ROMs.
3. **It removes a layer and NOTHING else.** No machine state, no timing: the same mask
   restored must give back the same frame, bit for bit.
"""

from __future__ import annotations

import unittest

from core import native
from core.renderer import (
    LAYER_ALL,
    LAYER_SCR1,
    LAYER_SCR2,
    LAYER_SPR_BACK,
    LAYER_SPR_FRONT,
    LAYER_SPR_MID,
    LAYER_SPRITES,
    render_frame,
)

SCR1_MAP, SCR2_MAP, CHAR_RAM = 0x9000, 0x9800, 0xA000
SCR1_PAL, SCR2_PAL, SPR_PAL = 0x8280, 0x8300, 0x8200
OAM, OAM_CPC = 0x8800, 0x8C00


def _rom() -> bytes:
    rom = bytearray(b"\xFF" * 0x100000)
    rom[0:28] = b" LICENSED BY SNK CORPORATION"
    rom[0x1C:0x20] = (0x200040).to_bytes(4, "little")
    rom[0x23] = 0x10
    rom[0x40] = 0x05                       # halt: the scene stops changing
    return bytes(rom)


class LayerBitAgreementTests(unittest.TestCase):
    """The two cores must not drift apart on what a bit means."""

    def test_the_python_and_native_bit_names_are_the_same_values(self) -> None:
        M = native.NativeMachine
        self.assertEqual(
            (M.LAYER_SCR1, M.LAYER_SCR2, M.LAYER_SPR_BACK, M.LAYER_SPR_MID,
             M.LAYER_SPR_FRONT, M.LAYER_SPRITES, M.LAYER_ALL),
            (LAYER_SCR1, LAYER_SCR2, LAYER_SPR_BACK, LAYER_SPR_MID,
             LAYER_SPR_FRONT, LAYER_SPRITES, LAYER_ALL),
        )

    def test_all_is_exactly_the_five_layers(self) -> None:
        self.assertEqual(
            LAYER_ALL,
            LAYER_SCR1 | LAYER_SCR2 | LAYER_SPR_BACK | LAYER_SPR_MID | LAYER_SPR_FRONT,
        )


@unittest.skipUnless(native.available(), "native core not built")
class LayerMaskTests(unittest.TestCase):
    """A static scene with both planes and sprites at all three priorities."""

    def setUp(self) -> None:
        self.m = native.NativeMachine(_rom())
        self.m.reset(bios_handoff=True)
        self.m.write(0x8002, bytes([0x00, 0x00, 0xA0, 0x98]))   # full-screen window

        # ⚠️ THE SCENE HAS TO LEAVE HOLES. An opaque front plane hides the back plane and
        # the low-priority sprites completely -- and then "hiding that layer changed
        # nothing" is the CORRECT picture, so the test would pass on a mask that does
        # nothing at all. Tile 0 is left as 16 zero bytes (every pixel value 0 =
        # transparent) and the two tilemaps are punched with different periods, so each
        # layer has somewhere it is genuinely the visible one.
        self.m.write(CHAR_RAM, bytes(16))                       # tile 0: transparent
        self.m.write(CHAR_RAM + 16, bytes([0x55, 0x55] * 8))    # tile 1: opaque
        self.m.write(SCR1_PAL, bytes(range(0x20)))
        self.m.write(SCR2_PAL, bytes(range(0x20, 0x40)))
        self.m.write(SPR_PAL, bytes(range(0x40, 0x60)))
        for ty in range(32):
            for tx in range(32):
                i = ty * 32 + tx
                self.m.write(SCR1_MAP + i * 2, bytes([1 if tx % 2 == 0 else 0, 0x02]))
                self.m.write(SCR2_MAP + i * 2, bytes([1 if tx % 4 == 1 else 0, 0x04]))
        # Front plane = SCR1 (opaque on even tile columns), back plane = SCR2 (opaque on
        # tx%4==1). So PR.C=3 shows anywhere, PR.C=2 needs SCR1 transparent (tx odd), and
        # PR.C=1 needs BOTH transparent (tx%4==3).
        for slot, (prc, tx) in enumerate(((1, 3), (2, 1), (3, 6))):
            self.m.write(OAM + slot * 4, bytes([1, prc << 3, tx * 8, 8]))
            self.m.write(OAM_CPC + slot, bytes([slot + 1]))

    def tearDown(self) -> None:
        self.m.close()

    def _frame(self, mask: int) -> list[int]:
        self.m.set_layer_mask(mask)
        self.m.run_frames(2)
        return self.m.framebuffer()

    # ---- 1. the default -------------------------------------------------------
    def test_a_fresh_machine_shows_everything(self) -> None:
        self.assertEqual(self.m.layer_mask(), LAYER_ALL)

    def test_never_touching_the_mask_draws_the_same_as_setting_it_to_all(self) -> None:
        """LAYER_ALL must be a genuine no-op, not merely what the field happens to hold:
        a machine that is never told about the mask and one explicitly set to LAYER_ALL
        must produce the same frame. The scene halts, so the frames are comparable."""
        self.m.run_frames(2)
        untouched = self.m.framebuffer()
        self.assertEqual(self._frame(LAYER_ALL), untouched)

    # ---- 2. the two cores agree under EVERY mask -------------------------------
    def test_both_renderers_draw_the_same_picture_under_every_mask(self) -> None:
        for mask in range(LAYER_ALL + 1):
            with self.subTest(mask=f"{mask:#04x}"):
                fb = self._frame(mask)
                blob = self.m.read(0x8000, 0x4000)
                mem = {0x8000 + i: b for i, b in enumerate(blob)}
                py = render_frame(mem, self.m.raster_log(), layer_mask=mask)
                self.assertEqual(fb, [p.raw for row in py.pixels for p in row])

    # ---- 3. it really removes the layer, and only the layer -------------------
    def test_hiding_a_plane_removes_its_colours(self) -> None:
        """CONDEMNS a no-op mask: each plane paints from its own palette block, so its
        colours must vanish when it is hidden and come back when it is not."""
        full = set(self._frame(LAYER_ALL))
        without_scr1 = set(self._frame(LAYER_ALL & ~LAYER_SCR1))
        without_scr2 = set(self._frame(LAYER_ALL & ~LAYER_SCR2))
        self.assertLess(without_scr1, full, "hiding SCR1 changed nothing")
        self.assertLess(without_scr2, full, "hiding SCR2 changed nothing")
        self.assertNotEqual(without_scr1, without_scr2, "the two planes hid the same thing")

    def test_each_sprite_priority_group_hides_on_its_own(self) -> None:
        full = self._frame(LAYER_ALL)
        for bit in (LAYER_SPR_BACK, LAYER_SPR_MID, LAYER_SPR_FRONT):
            with self.subTest(bit=f"{bit:#04x}"):
                self.assertNotEqual(self._frame(LAYER_ALL & ~bit), full)

    def test_masking_is_reversible(self) -> None:
        """Composition only: no machine state changes, so the picture must come back."""
        before = self._frame(LAYER_ALL)
        self._frame(LAYER_SCR1)
        self._frame(0)
        self.assertEqual(self._frame(LAYER_ALL), before)

    def test_a_hidden_sprite_group_reveals_the_plane_not_the_sprite_behind_it(self) -> None:
        """⚠️ THE SUBTLE ONE. Sprite 0 wins its pixel whatever is shown -- the chip's line
        buffer is resolved before PR.C places anything. So hiding the front group must
        expose the SCROLL PLANE, never the sprite that lost the pixel to it. Anything
        else would be inventing an image the hardware cannot produce."""
        # Two sprites on the same pixels: index 0 in front (PR.C=3), index 1 behind
        # both planes (PR.C=1). Index 0 wins the line buffer by being index 0.
        self.m.write(OAM + 0, bytes([3, 3 << 3, 40, 40]))
        self.m.write(OAM + 4, bytes([3, 1 << 3, 40, 40]))
        self.m.write(OAM_CPC + 0, bytes([1]))
        self.m.write(OAM_CPC + 1, bytes([2]))
        self.m.write(OAM + 8, bytes([0, 0, 0, 0]))          # clear the third sprite

        planes_only = self._frame(LAYER_SCR1 | LAYER_SCR2)
        hidden_front = self._frame(LAYER_ALL & ~LAYER_SPR_FRONT)
        i = 41 * native.SCREEN_W + 41                       # inside both sprites
        self.assertEqual(
            hidden_front[i], planes_only[i],
            "hiding the front sprites exposed something other than the scroll plane",
        )


if __name__ == "__main__":
    unittest.main()
