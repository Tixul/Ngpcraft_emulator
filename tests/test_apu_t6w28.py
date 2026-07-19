"""T6W28 APU oracle tests (clean-room audio core).

Covers the register interface and generator facts from ``specs/APU_T6W28.md``:
volume table, two-byte period build, separate L/R volume latches (stereo pan),
noise divisor select, white-vs-periodic tap, and LFSR advance.
"""

from __future__ import annotations

import unittest

from core.apu import (
    NOISE_PERIODS,
    TAP_DISABLED,
    TAP_WHITE,
    VOLUMES,
    ApuState,
    reset_state,
    run_until,
    write_left,
    write_right,
)


class VolumeTableTest(unittest.TestCase):
    def test_curve_endpoints_and_length(self) -> None:
        self.assertEqual(len(VOLUMES), 16)
        self.assertEqual(VOLUMES[0], 64)  # index 0 = full volume
        self.assertEqual(VOLUMES[15], 0)  # index 15 = silent
        # strictly non-increasing logarithmic attenuation
        self.assertTrue(all(a >= b for a, b in zip(VOLUMES, VOLUMES[1:])))


class RegisterInterfaceTest(unittest.TestCase):
    def test_left_volume_latch_sets_only_left(self) -> None:
        # latch: bit7=1, index=0 (bits 6:5=00), volume bit4=1, nibble=0 -> full
        state = write_left(reset_state(), 0x90)
        self.assertEqual(state.squares[0].volume_left, 64)
        self.assertEqual(state.squares[0].volume_right, 0)

    def test_right_volume_latch_sets_only_right(self) -> None:
        state = write_right(reset_state(), 0x90)
        self.assertEqual(state.squares[0].volume_right, 64)
        self.assertEqual(state.squares[0].volume_left, 0)

    def test_stereo_pan_same_channel(self) -> None:
        # channel 1: left full (0x0), right attenuated (0xF -> 0)
        state = reset_state()
        state = write_left(state, 0xB0 | 0x00)   # index 1, vol latch, nibble 0
        state = write_right(state, 0xB0 | 0x0F)  # index 1, vol latch, nibble 15
        self.assertEqual(state.squares[1].volume_left, 64)
        self.assertEqual(state.squares[1].volume_right, 0)

    def test_channel_index_from_latch_bits(self) -> None:
        # index = (latch >> 5) & 3 ; latch 0xD0 -> index 2
        state = write_left(reset_state(), 0xD0 | 0x04)  # index 2, vol, nibble 4
        self.assertEqual(state.squares[2].volume_left, VOLUMES[4])

    def test_two_byte_period_build(self) -> None:
        # tone channel 0 period is built across a latch byte then a data byte.
        state = reset_state()
        state = write_left(state, 0x80 | 0x0A)  # latch, index0, tone, low nibble
        state = write_left(state, 0x3F)         # data, high 6 bits
        expected = ((0x3F << 8) & 0x3F00) | ((0x8A << 4) & 0x00FF)
        self.assertEqual(state.squares[0].period, expected)

    def test_data_byte_reuses_latched_index(self) -> None:
        # A bare data byte (bit7=0) must target the last latched channel.
        state = reset_state()
        state = write_left(state, 0xA0)  # latch index 1, tone command
        state = write_left(state, 0x10)  # data byte -> still channel 1
        self.assertNotEqual(state.squares[1].period, 0)
        self.assertEqual(state.squares[0].period, 0)


class NoiseTest(unittest.TestCase):
    def test_fixed_period_select(self) -> None:
        # right latch index 3 (0xE0), select 1 -> NOISE_PERIODS[1], white tap
        state = write_right(reset_state(), 0xE0 | 0x04 | 0x01)
        self.assertEqual(state.noise.period_select, 1)
        self.assertEqual(NOISE_PERIODS[state.noise.period_select], 0x200)
        self.assertEqual(state.noise.tap, TAP_WHITE)

    def test_periodic_tap_when_bit2_clear(self) -> None:
        state = write_right(reset_state(), 0xE0 | 0x00)  # bit2 clear
        self.assertEqual(state.noise.tap, TAP_DISABLED)

    def test_noise_reconfig_resets_shifter(self) -> None:
        state = ApuState(noise=reset_state().noise)
        state = write_right(state, 0xE0 | 0x04 | 0x02)
        self.assertEqual(state.noise.shifter, 0x4000)
        self.assertEqual(state.noise.period_select, 2)

    def test_extra_noise_period_two_byte_build(self) -> None:
        # right index 2 (0xC0) writes noise.period_extra.
        state = reset_state()
        state = write_right(state, 0xC0 | 0x0A)  # latch, index2 -> low nibble
        state = write_right(state, 0x3F)         # data -> high 6 bits
        expected = ((0x3F << 8) & 0x3F00) | ((0xCA << 4) & 0x00FF)
        self.assertEqual(state.noise.period_extra, expected)


class RunTest(unittest.TestCase):
    def test_silent_when_no_volume(self) -> None:
        _, samples = run_until(reset_state(), 64)
        self.assertEqual(len(samples), 64)
        self.assertTrue(all(s == (0, 0) for s in samples))

    def test_square_toggles_and_is_bipolar(self) -> None:
        state = reset_state()
        state = write_left(state, 0x90)          # ch0 left full
        state = write_left(state, 0x80 | 0x00)   # period low
        state = write_left(state, 0x02)          # period high -> 0x200
        _, samples = run_until(state, 0x200 * 4)
        lefts = {s[0] for s in samples}
        # amplitude reaches both +64 and -64 as the phase toggles
        self.assertIn(64, lefts)
        self.assertIn(-64, lefts)

    def test_run_zero_cycles_is_noop(self) -> None:
        state = write_left(reset_state(), 0x90)
        new_state, samples = run_until(state, 0)
        self.assertEqual(samples, [])
        self.assertEqual(new_state, state)


if __name__ == "__main__":
    unittest.main()
