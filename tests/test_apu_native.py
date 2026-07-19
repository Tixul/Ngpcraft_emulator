"""The T6W28, wired at last -- and the tribunal that says it is not just noise.

TWO KINDS OF EVIDENCE, because they prove different things.

1. DIFFERENTIAL (the register decode). The native chip in cpp/src/apu.cpp is held
   against the clean-room Python model in core/apu.py, replayed from reset with
   the exact byte stream a real sound driver produced. That proves the two AGREE.

   It does NOT prove either is right. This project has been bitten by exactly that
   before: both cores read the MUL/DIV `RR` code as an array index, agreed with
   each other perfectly, and were both wrong -- only asm900 broke the tie.

2. INDEPENDENT (is it music?). So the second test looks at neither model's code.
   It takes the tone periods the drivers actually wrote and asks whether the
   frequencies they name land on the twelve-tone scale. A wrong decode scatters
   frequencies between the semitones; a right one produces a scale, because a
   composer put one there.

WHO WRITES TO THE CHIP was MEASURED, not assumed -- see cpp/src/apu.hpp. Across all
73 commercial ROMs the drivers touch Z80 addresses 0x4000 and 0x4001 and nothing
else in that window; the `OUT (0xFF)` writes that 71 of them also make increment by
exactly one every time and are a tick counter, not sound. They are deliberately NOT
fed to the chip.
"""

from __future__ import annotations

import math
from pathlib import Path

import pytest

from core import apu as oracle
from core import native

ROM_DIR = Path(__file__).resolve().parents[3] / "jeux officiel"
BIOS_PATH = ROM_DIR / "bios_v10.bin"

APU_WRITE_MEM = 1

# (rom, frames) chosen so the 4096-entry write log still holds EVERY write the
# driver has made -- the oracle replays from reset, so it must see all of them.
# The test asserts that, rather than trusting the number.
CASES = [
    ("Sonic the Hedgehog Pocket Adventure (USA).ngc", 300),
    ("Metal Slug - 2nd Mission (USA).ngc", 200),
    ("Fatal Fury - First Contact (USA).ngc", 40),
]

pytestmark = pytest.mark.skipif(
    not (BIOS_PATH.exists() and (ROM_DIR / CASES[0][0]).exists()),
    reason="needs the real BIOS image and commercial ROMs",
)


def _run(rom_name: str, frames: int) -> native.NativeMachine:
    machine = native.NativeMachine(
        (ROM_DIR / rom_name).read_bytes(), bios=BIOS_PATH.read_bytes()
    )
    machine.reset(bios_handoff=True)
    for _ in range(frames):
        machine.run_frames(1)
    return machine


def _replay(writes) -> oracle.ApuState:
    state = oracle.reset_state()
    for w in writes:
        if w.kind != APU_WRITE_MEM:
            continue  # OUT (0xFF) is a tick counter, not sound -- see the docstring
        # Address bit A0 picks the stereo side. Measured across all 73 ROMs.
        if w.address & 1:
            state = oracle.write_left(state, w.value)
        else:
            state = oracle.write_right(state, w.value)
    return state


@pytest.mark.parametrize("rom_name,frames", CASES)
def test_native_chip_agrees_with_the_python_oracle(rom_name: str, frames: int) -> None:
    """Same byte stream in, same registers out."""
    machine = _run(rom_name, frames)

    total = machine.apu_write_count()
    assert total <= machine.APU_LOG_SIZE, (
        f"{rom_name} made {total} writes and the log only keeps "
        f"{machine.APU_LOG_SIZE}: the oracle would be replaying an incomplete "
        "stream and any 'agreement' would be an accident. Lower the frame count."
    )
    writes = machine.apu_writes()
    assert any(w.kind == APU_WRITE_MEM for w in writes), "the driver never drove the chip"

    want = _replay(writes)
    got = machine.apu_state()

    for i in range(3):
        assert got.square_period[i] == want.squares[i].period, f"channel {i} period"
        assert got.square_vol_left[i] == want.squares[i].volume_left, f"channel {i} volume L"
        assert got.square_vol_right[i] == want.squares[i].volume_right, f"channel {i} volume R"
    assert got.noise_vol_left == want.noise.volume_left
    assert got.noise_vol_right == want.noise.volume_right
    assert got.noise_tap == want.noise.tap
    assert got.noise_period_select == want.noise.period_select
    assert got.noise_period_extra == want.noise.period_extra
    assert got.latch_left == want.latch_left
    assert got.latch_right == want.latch_right


def test_the_tones_are_music_not_noise() -> None:
    """Independent of BOTH models: do the periods name notes on the 12-tone scale?

    frequency = clock / (32 * n), and the stored period is already 16n, so it is
    simply clock / (2 * period).
    """
    machine = _run("Fatal Fury - First Contact (USA).ngc", 40)

    state = oracle.reset_state()
    periods: set[int] = set()
    for w in machine.apu_writes():
        if w.kind != APU_WRITE_MEM:
            continue
        before = tuple(sq.period for sq in state.squares)
        state = (
            oracle.write_left(state, w.value)
            if (w.address & 1)
            else oracle.write_right(state, w.value)
        )
        for i, sq in enumerate(state.squares):
            if sq.period != before[i] and sq.period > 128:
                periods.add(sq.period)

    assert len(periods) >= 8, f"expected a melody, got {len(periods)} distinct tones"

    cents_off = []
    for period in periods:
        freq = 3072000 / (2 * period)
        semitones = 12 * math.log2(freq / 440.0)
        cents_off.append(abs(semitones - round(semitones)) * 100)

    median = sorted(cents_off)[len(cents_off) // 2]
    # Periods chosen at random would sit ~25 cents off the nearest semitone on
    # average, and half of them worse than that. Real music lands much closer --
    # but the chip's period resolution is coarse in the top octaves, so this is a
    # floor to catch a broken decode, not a claim about tuning accuracy.
    assert median < 20, (
        f"the tone periods do not fall on a musical scale (median {median:.1f} "
        "cents off the nearest semitone) -- the decode is probably wrong"
    )


def test_a_real_rom_produces_stereo_audio_and_drops_nothing() -> None:
    """End to end: the chip must actually emit sound, and the host must keep up."""
    machine = _run("Metal Slug - 2nd Mission (USA).ngc", 1)
    pcm = bytearray()
    for _ in range(300):
        machine.run_frames(1)
        pcm += machine.audio()

    assert machine.audio_dropped() == 0, "the ring buffer overran: samples were lost"

    samples = memoryview(bytes(pcm)).cast("h")
    assert len(samples) > 44100, "less than half a second of audio for five seconds of play"

    left = samples[0::2]
    right = samples[1::2]
    assert any(left), "the left channel is pure silence"
    assert any(v != left[0] for v in left), "the left channel is a constant, not a waveform"
    # The T6W28's whole point is independent stereo volumes; a mono model would
    # make these identical and nobody would notice.
    assert any(l != r for l, r in zip(left, right)), "left and right are identical: not stereo"
