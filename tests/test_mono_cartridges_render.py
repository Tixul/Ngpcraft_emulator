"""The MONOCHROME cartridges draw. All of them. End to end.

Four ROMs in the corpus were written for the Neo Geo Pocket, before the Color
existed, and all four came back BLACK for the whole life of this project. They are
not broken and they never were: a K1GE game writes the 3-bit LEVEL table at 0x8100
-- which WAS its palette on the old machine -- and knows nothing about the 12-bit
colour table the K2GE resolves those levels through. That table is the COLOUR THEME
the console applies to old cartridges, exactly as a Game Boy Color tints a Game Boy
game, and the BIOS installs it before it hands over the machine.

We do not run the BIOS's boot code, so we hand the cart the state it leaves:

    0x87E2 = 0x80                K1GE upper-palette-compatible mode  (BIOS: 0xFF17C4)
    0x8380/A0/C0/E0 = the ramp   FFF DDD BBB 999 777 444 333 000, x2, all four planes

⛔ THE RAMP IS NOT A TABLE IN THE BIOS ROM. I searched the whole image for one, found
nothing, and briefly wrote that down as evidence that there was no ramp to find. The
BIOS COMPUTES it. Booting the real BIOS with a mono cartridge (pass 237) and reading
the palette straight back out of the machine is what produced these numbers.

🔑 "I could not find it in the ROM" is not "it does not exist". It was there the whole
time; I was looking for the wrong SHAPE.

These tests drive the real cartridges through the real renderer. A unit test on the
palette maths would have passed for years while the screen stayed blank.
"""

from __future__ import annotations

import unittest
from pathlib import Path

from core import native
from core.native_session import NativeSession
from core.renderer import k1ge_compat_enabled

ROMS = Path(__file__).resolve().parents[3] / "jeux officiel"
BIOS = ROMS / "bios_v10.bin"

# The eight levels the BIOS computes, in order.
GREY_RAMP = (0x0FFF, 0x0DDD, 0x0BBB, 0x0999, 0x0777, 0x0444, 0x0333, 0x0000)

MONO_CARTRIDGES = (
    "King of Fighters R-1, The (Europe).ngp",
    "Samurai Shodown! (Europe).ngp",
    "Melon-chan no Seichou Nikki (Japan).ngp",
)


def _session(name: str) -> NativeSession | None:
    rom = ROMS / name
    if not rom.exists() or not native.available():
        return None
    return NativeSession(rom, bios_path=BIOS if BIOS.exists() else None, autosave=False)


@unittest.skipUnless(native.available(), "native core not built")
@unittest.skipUnless(ROMS.exists(), "the commercial corpus is not on this machine")
class MonoCartridgeTests(unittest.TestCase):
    def test_the_handoff_puts_a_mono_cart_in_compat_mode(self) -> None:
        s = _session(MONO_CARTRIDGES[0])
        if s is None:
            self.skipTest("ROM missing")
        with s:
            self.assertTrue(
                k1ge_compat_enabled(s.video_memory()),
                "a cartridge whose header says NGP must come up in K1GE compat mode",
            )

    def test_the_handoff_installs_the_bios_grey_ramp_on_every_plane(self) -> None:
        s = _session(MONO_CARTRIDGES[0])
        if s is None:
            self.skipTest("ROM missing")
        with s:
            for base, plane in (
                (0x8380, "sprite"), (0x83A0, "scr1"), (0x83C0, "scr2"), (0x83E0, "backdrop"),
            ):
                raw = s.machine.read(base, 32)
                got = [int.from_bytes(raw[i * 2 : i * 2 + 2], "little") for i in range(16)]
                self.assertEqual(
                    got, list(GREY_RAMP) * 2,
                    f"the {plane} plane did not get the BIOS's ramp",
                )

    def test_a_COLOUR_cart_is_left_alone(self) -> None:
        """The seeding must key off the HEADER, not fire for everyone.

        Sonic is a colour game: it writes the K2GE palettes itself, and stamping a grey
        ramp over its compat table would be harmless only by luck.
        """
        s = _session("Sonic the Hedgehog Pocket Adventure (USA).ngc")
        if s is None:
            self.skipTest("ROM missing")
        with s:
            self.assertFalse(k1ge_compat_enabled(s.video_memory()))

    def test_every_mono_cartridge_actually_DRAWS(self) -> None:
        """The one that matters. Not the palette maths -- the picture.

        Each of these ran, and drew, and resolved every pixel through a K2GE palette it
        had never written: a screen of one colour, for the whole life of the project. A
        test on the arithmetic would have been green throughout.
        """
        for name in MONO_CARTRIDGES:
            with self.subTest(cartridge=name):
                s = _session(name)
                if s is None:
                    self.skipTest("ROM missing")
                with s:
                    # 1800 frames: Samurai Shodown fades through white around 900, and a
                    # single unlucky sample is not a verdict.
                    s.run_frames(1800)
                    frame = s.render()
                    colours = {
                        (px.r, px.g, px.b) for row in frame.pixels for px in row
                    }
                    self.assertGreater(
                        len(colours), 1,
                        f"{name} is still a single flat colour -- it is not drawing",
                    )


if __name__ == "__main__":
    unittest.main()
