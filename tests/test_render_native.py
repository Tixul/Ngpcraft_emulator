"""The C++ renderer must draw the SAME PICTURE as the Python one. Pixel for pixel.

`core/renderer.py` is the REFERENCE: it is the one carrying the Tech Ref citations, the
retractions and the reasoning. `cpp/src/render.cpp` is the one that runs 20x faster and
draws at the right MOMENT (line by line, as the beam passes). Those are two different
virtues, and the second is worthless without the first.

A fast renderer that quietly disagrees with the slow one is not an optimisation -- it is
a SECOND IMPLEMENTATION OF THE MACHINE, and this project has exactly one. So the two are
held against each other here on scenes where they MUST agree.

⚠️ WHERE THEY LEGITIMATELY DIVERGE, and why that is the point: on a scene that CHANGES
mid-frame. Python composes the frame from the end-of-frame memory; the core draws each
line as the beam reaches it. A scrolling game streams tiles into VRAM while the frame is
being drawn -- so the top of the screen genuinely shows older data than the bottom, and
the core is RIGHT while the reference is smeared. That divergence is not a bug to be
tested away; it is the whole reason the renderer moved. These tests therefore pin the
agreement on STATIC scenes, which is where "same picture" is a meaningful claim.
"""

from __future__ import annotations

import unittest
from pathlib import Path

from core import native
from core.renderer import NGPC_SCREEN_HEIGHT as H, NGPC_SCREEN_WIDTH as W, render_frame

ROMS = Path(__file__).resolve().parents[3] / "jeux officiel"
BIOS = ROMS / "bios_v10.bin"

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


@unittest.skipUnless(native.available(), "native core not built")
class RendererAgreementTests(unittest.TestCase):
    """A scene is poked into VRAM, the machine is left to draw a whole frame, and the
    two renderers are compared. The ROM halts, so nothing moves: any difference is a
    difference of INTERPRETATION, which is exactly what we want to catch."""

    def setUp(self) -> None:
        self.m = native.NativeMachine(_rom())
        self.m.reset(bios_handoff=True)
        # A full-screen window, or every pixel is "outside" and the scene is invisible.
        self.m.write(0x8002, bytes([0x00, 0x00, 0xA0, 0x98]))

    def tearDown(self) -> None:
        self.m.close()

    def _both(self) -> tuple[list[int], list[int]]:
        """Run one whole frame, then take both pictures."""
        self.m.run_frames(2)
        native_fb = self.m.framebuffer()
        blob = self.m.read(0x8000, 0x4000)
        mem = {0x8000 + i: b for i, b in enumerate(blob)}
        py = render_frame(mem, self.m.raster_log())
        py_fb = [p.raw for row in py.pixels for p in row]
        return native_fb, py_fb

    def _assert_same(self, msg: str) -> None:
        native_fb, py_fb = self._both()
        if native_fb == py_fb:
            return
        bad = [i for i in range(W * H) if native_fb[i] != py_fb[i]]
        i = bad[0]
        self.fail(
            f"{msg}: {len(bad)} of {W*H} pixels differ. "
            f"First at ({i % W}, {i // W}): core={native_fb[i]:#05x} python={py_fb[i]:#05x}"
        )

    def test_a_blank_machine(self) -> None:
        self._assert_same("an empty screen")

    def test_the_backdrop(self) -> None:
        self.m.write(0x8118, bytes([0x80 | 3]))          # BGC on, index 3
        self.m.write(0x83E0 + 6, bytes([0x0F, 0x0A]))    # entry 3 = some colour
        self._assert_same("the backdrop")

    def test_scroll_planes_with_flips_and_palettes(self) -> None:
        for t in range(4):
            self.m.write(CHAR_RAM + t * 16, bytes([0x1B, 0xE4] * 8))
        self.m.write(SCR1_PAL, bytes(range(0x20)))
        self.m.write(SCR2_PAL, bytes(range(0x20, 0x40)))
        for i in range(32 * 32):
            self.m.write(SCR1_MAP + i * 2, bytes([i & 3, ((i & 3) << 1) | ((i & 1) << 7)]))
            self.m.write(SCR2_MAP + i * 2, bytes([(i + 1) & 3, ((i & 7) << 1) | ((i & 1) << 6)]))
        self.m.write(0x8032, bytes([13, 45, 200, 7]))    # both planes, awkward offsets
        self._assert_same("the scroll planes")

    def test_plane_priority_flips_the_order(self) -> None:
        self.test_scroll_planes_with_flips_and_palettes()
        self.m.write(0x8030, bytes([0x80]))              # SCR2 in front
        self._assert_same("SCR2 in front")

    def test_sprites_with_chaining_priority_and_wrap(self) -> None:
        for t in range(4):
            self.m.write(CHAR_RAM + t * 16, bytes([0x4E, 0xB1] * 8))
        self.m.write(SPR_PAL, bytes(range(0x40, 0x60)))
        for i in range(64):
            attrib = ((i % 3) + 1) << 3          # PR.C 1..3, never 0
            if i % 4 == 1:
                attrib |= 0x06                   # H.ch + V.ch: a chained tail
            if i % 5 == 0:
                attrib |= 0x80                   # H flip
            if i % 7 == 0:
                attrib |= 0x40                   # V flip
            # y = 249 puts the sprite off the TOP: its last rows wrap onto the screen.
            x = (i * 9) % 256
            y = 249 if i % 11 == 0 else (i * 5) % 256
            self.m.write(OAM + i * 4, bytes([i % 4, attrib, x, y]))
            self.m.write(OAM_CPC + i, bytes([i % 16]))
        self.m.write(0x8020, bytes([0xF8, 0xF8]))        # PO.H / PO.V = -8
        self._assert_same("the sprites")

    def test_a_hidden_sprite_still_anchors_its_chain(self) -> None:
        for t in range(2):
            self.m.write(CHAR_RAM + t * 16, bytes([0xAA] * 16))
        self.m.write(SPR_PAL, bytes(range(0x10, 0x30)))
        self.m.write(OAM + 0, bytes([1, 0x00, 40, 40]))        # PR.C = 0: HIDDEN
        self.m.write(OAM + 4, bytes([1, 0x06 | (2 << 3), 8, 8]))  # chained to it
        self._assert_same("a hidden chain anchor")

    def test_the_window_and_the_out_of_window_colour(self) -> None:
        self.test_scroll_planes_with_flips_and_palettes()
        self.m.write(0x8002, bytes([20, 15, 90, 60]))    # a sub-window
        self.m.write(0x8012, bytes([0x05]))              # OOWC = 5
        self.m.write(0x83E0 + 10, bytes([0x77, 0x03]))
        self._assert_same("the window clip")

    def test_NEG_inverts_everything_including_the_fill(self) -> None:
        self.test_the_window_and_the_out_of_window_colour()
        self.m.write(0x8012, bytes([0x80 | 0x05]))       # NEG + OOWC 5
        self._assert_same("the negative")

    def test_k1ge_compatible_mode(self) -> None:
        """A monochrome cartridge draws through the 3-bit level LUT, not the palettes."""
        self.test_scroll_planes_with_flips_and_palettes()
        self.m.write(0x87E2, bytes([0x80]))              # compat mode ON
        self.m.write(0x8100, bytes([(i * 3) & 7 for i in range(24)]))   # the LUTs
        self.m.write(0x8380, bytes(range(0x60, 0xC0)))                  # the 12-bit palettes
        self.test_sprites_with_chaining_priority_and_wrap()
        self._assert_same("K1GE compat mode")


@unittest.skipUnless(native.available(), "native core not built")
@unittest.skipUnless(BIOS.exists(), "the corpus lives beside the BIOS")
class RealGamesAgreeOnAStillFrameTests(unittest.TestCase):
    """Commercial ROMs, on frames where nothing moves mid-frame.

    Where a game DOES stream VRAM mid-frame the two must differ -- and the core is the
    one telling the truth. So this asserts agreement on the games whose title screens
    are still, and does not pretend the rest is a defect.
    """

    STILL = ["Puyo Pop (USA).ngc", "Sonic the Hedgehog Pocket Adventure (USA).ngc"]

    def test_the_two_renderers_agree_within_a_hair(self) -> None:
        from core.native_session import NativeSession

        for name in self.STILL:
            rom = ROMS / name
            if not rom.exists():
                continue
            with self.subTest(rom=name):
                with NativeSession(rom, bios_path=BIOS, autosave=False) as s:
                    s.run_frames(900)
                    core_fb = s.machine.framebuffer()
                    py = render_frame(s.video_memory(), s.machine.raster_log())
                    py_fb = [p.raw for row in py.pixels for p in row]
                differ = sum(1 for i in range(W * H) if core_fb[i] != py_fb[i])
                self.assertLess(
                    differ, W * H // 100,
                    f"{name}: {differ} pixels differ between the core and the reference "
                    f"renderer on a still frame -- they are drawing different machines",
                )


if __name__ == "__main__":
    unittest.main()
