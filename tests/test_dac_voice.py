"""THE DAC — the voice. "SEGAAA".

The T6W28 makes tones. It cannot say a word. A sampled voice reaches the speaker
through a pair of 8-bit converters at I/O 0xA2 (left) and 0xA3 (right) that the MAIN
CPU streams bytes into, bypassing the sound chip completely.

We modelled the sound chip and let those bytes fall on the floor as ordinary memory.
So a game's music was fine and its digitised voice was simply ABSENT -- a silence with
no cause anywhere in the PSG, which is precisely why it survived so long. The user
heard it before any instrument here did: "j'entend pas non plus le jingle SEGAAA".

MEASURED, not assumed: Sonic's boot writes 26 050 bytes to these two ports in 300
frames, all from one instruction (0x3F1E72), and the values cluster around 0x80. That
last fact settles the format -- UNSIGNED 8-bit PCM centred on 0x80, so 0x80 is silence
and the signed sample is (v - 0x80). It also means a game that never touches the DAC
contributes exactly nothing, which is what keeps this from becoming a DC offset
smeared across every other game's music.
"""

from __future__ import annotations

import struct
import unittest
from pathlib import Path

from core import native
from core.native_session import NativeSession

ROMS = Path(__file__).resolve().parents[3] / "jeux officiel"
BIOS = ROMS / "bios_v10.bin"
SONIC = ROMS / "Sonic the Hedgehog Pocket Adventure (USA).ngc"

DAC_LEFT, DAC_RIGHT = 0x0000A2, 0x0000A3
SILENCE = 0x80


def _rom() -> bytes:
    rom = bytearray(b"\xFF" * 0x100000)
    rom[0:28] = b" LICENSED BY SNK CORPORATION"
    rom[0x1C:0x20] = (0x200040).to_bytes(4, "little")
    rom[0x23] = 0x10
    rom[0x40] = 0x05                     # halt
    return bytes(rom)


def _peak(pcm: bytes) -> int:
    if not pcm:
        return 0
    return max(abs(v) for v in struct.unpack(f"<{len(pcm)//2}h", pcm))


def _dc(pcm: bytes) -> float:
    if not pcm:
        return 0.0
    v = struct.unpack(f"<{len(pcm)//2}h", pcm)
    return sum(v) / len(v)


@unittest.skipUnless(native.available(), "native core not built")
class DacTests(unittest.TestCase):
    def setUp(self) -> None:
        self.m = native.NativeMachine(_rom())
        self.m.reset(bios_handoff=True)

    def tearDown(self) -> None:
        self.m.close()

    def _run(self, cycles: int = 4000) -> bytes:
        self.m.run(cycles, record=False)
        return self.m.audio(8192)

    def test_a_silent_dac_makes_NO_SOUND(self) -> None:
        """0x80 is mid-scale. A game that never writes the DAC must be unaffected.

        Get this wrong and every other game's music inherits a DC offset -- a defect
        that would be loudest in the games that never used the feature at all.
        """
        self.assertEqual(_peak(self._run()), 0)

    def test_a_byte_written_to_the_DAC_MOVES_THE_SPEAKER(self) -> None:
        self.m.bus_write(DAC_LEFT, 0xFF)         # full positive excursion
        pcm = self._run()
        self.assertGreater(_peak(pcm), 1000, "the DAC byte went nowhere")

    def test_the_two_converters_are_LEFT_and_RIGHT(self) -> None:
        """0xA2 = left, 0xA3 = right. Swap them and a stereo voice comes out backwards."""
        self.m.bus_write(DAC_LEFT, 0xFF)
        pcm = self._run()
        frames = struct.unpack(f"<{len(pcm)//2}h", pcm)
        left = [frames[i] for i in range(0, len(frames), 2)]
        right = [frames[i] for i in range(1, len(frames), 2)]
        self.assertGreater(max(left), 1000, "0xA2 did not drive the LEFT channel")
        self.assertEqual(max(right), 0, "0xA2 leaked into the right channel")

    def test_the_level_is_HELD_between_writes(self) -> None:
        """The converter keeps driving the last code it was given -- a zero-order hold.

        Without it a sample would be a train of clicks separated by silence.
        """
        self.m.bus_write(DAC_LEFT, 0xC0)
        first = _peak(self._run(2000))
        second = _peak(self._run(2000))          # nothing written in between
        self.assertGreater(first, 0)
        self.assertAlmostEqual(first, second, delta=1, msg="the DAC level decayed on its own")

    def test_it_is_SIGNED_around_0x80(self) -> None:
        """0x00 and 0xFF are opposite excursions of nearly equal size, not 0 and full."""
        self.m.bus_write(DAC_LEFT, 0x00)
        low = struct.unpack("<h", self._run(2000)[:2])[0]
        self.m.bus_write(DAC_LEFT, 0xFF)
        high = struct.unpack("<h", self._run(2000)[:2])[0]
        self.assertLess(low, 0, "0x00 must swing NEGATIVE, not to silence")
        self.assertGreater(high, 0)
        self.assertAlmostEqual(abs(low), abs(high), delta=abs(high) // 4)


@unittest.skipUnless(native.available(), "native core not built")
@unittest.skipUnless(SONIC.exists() and BIOS.exists(), "needs the retail cartridge + BIOS")
class SonicVoiceTests(unittest.TestCase):
    """The real thing: Sonic's boot says "SEGAAA", and it must reach the speaker."""

    def test_sonics_boot_actually_produces_audio(self) -> None:
        with NativeSession(SONIC, bios_path=BIOS, autosave=False) as s:
            peak, sounding = 0, 0
            for _ in range(300):
                s.run_frames(1)
                p = _peak(s.machine.audio(2000))
                peak = max(peak, p)
                sounding += p > 500
        self.assertGreater(peak, 4000, "the SEGA voice is silent")
        self.assertGreater(sounding, 20, "a jingle is not one frame long")

    def test_and_it_leaves_the_speaker_CENTRED_afterwards(self) -> None:
        """Sonic parks the DAC back on 0x80. If a game did not, we would be adding a
        constant offset to everything that followed -- so this is worth knowing."""
        with NativeSession(SONIC, bios_path=BIOS, autosave=False) as s:
            s.run_frames(600)
            self.assertAlmostEqual(_dc(s.machine.audio(4000)), 0, delta=200)


if __name__ == "__main__":
    unittest.main()
