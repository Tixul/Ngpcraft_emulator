"""T6W28 sound chip oracle (clean-room) for NgpCraft Emulator.

Immutable, deterministic model of the NGPC T6W28 APU: three square (tone)
oscillators plus one noise oscillator, with *independent* left/right volume
latches (the stereo feature that distinguishes the T6W28 from a plain
SN76489).

This module is the reference oracle described in ``specs/APU_T6W28.md``.
It is written from scratch from that spec (facts + algorithm), not ported
from any LGPL source. Same design contract as ``core/cpu.py``: frozen
dataclasses and pure ``write_*`` / ``run_until`` functions that return a new
state instead of mutating in place.

Signal reconstruction here is deliberately naive (per-cycle bipolar
amplitude), enough for an opcode/register oracle. Band-limited synthesis is
a job for the future real-time C++ core, not for this reference model.
"""

from __future__ import annotations

from dataclasses import dataclass, replace

# Logarithmic attenuation table, index 0 = full, 15 = silent.
# volumes[i] = round(64 * 1.26 ** (15 - i) / 1.26 ** 15)
VOLUMES: tuple[int, ...] = (64, 50, 39, 31, 24, 19, 15, 12, 9, 7, 5, 4, 3, 2, 1, 0)

# Fixed noise divisor table selected by (data & 3) in {0, 1, 2}.
NOISE_PERIODS: tuple[int, ...] = (0x100, 0x200, 0x400)

# Tap position of the LFSR: 13 = white noise, 16 = tap disabled (periodic).
TAP_WHITE = 13
TAP_DISABLED = 16


@dataclass(frozen=True)
class Oscillator:
    """One of the four T6W28 oscillators (3 squares + 1 noise).

    ``volume_left`` / ``volume_right`` are post-table amplitudes (0..64).
    ``phase`` is the square-wave phase bit (unused by the noise osc).
    """

    volume_left: int = 0
    volume_right: int = 0
    period: int = 0
    phase: int = 0


@dataclass(frozen=True)
class NoiseState:
    """LFSR noise generator state."""

    shifter: int = 0x4000
    tap: int = TAP_WHITE
    period_select: int = 0  # 0..2 -> NOISE_PERIODS, 3 -> period_extra
    period_extra: int = 0
    volume_left: int = 0
    volume_right: int = 0


@dataclass(frozen=True)
class ApuState:
    """Full immutable T6W28 state.

    ``squares`` holds oscillators 0..2 (tone), ``noise`` is oscillator 3.
    ``latch_left`` / ``latch_right`` remember the last command byte written
    to each stereo port so a following data byte reuses its channel index.
    """

    squares: tuple[Oscillator, Oscillator, Oscillator] = (
        Oscillator(),
        Oscillator(),
        Oscillator(),
    )
    noise: NoiseState = NoiseState()
    latch_left: int = 0
    latch_right: int = 0


def reset_state() -> ApuState:
    """Return the cold-start APU image."""

    return ApuState()


def _active_noise_period(noise: NoiseState) -> int:
    if noise.period_select < 3:
        return NOISE_PERIODS[noise.period_select]
    return noise.period_extra


def _set_square_volume_left(state: ApuState, index: int, amp: int) -> ApuState:
    sq = replace(state.squares[index], volume_left=amp)
    squares = tuple(sq if i == index else s for i, s in enumerate(state.squares))
    return replace(state, squares=squares)  # type: ignore[arg-type]


def _set_square_volume_right(state: ApuState, index: int, amp: int) -> ApuState:
    sq = replace(state.squares[index], volume_right=amp)
    squares = tuple(sq if i == index else s for i, s in enumerate(state.squares))
    return replace(state, squares=squares)  # type: ignore[arg-type]


def _set_volume(state: ApuState, index: int, amp: int, *, left: bool) -> ApuState:
    """Set post-table volume for channel ``index`` on one stereo side."""

    if index < 3:
        if left:
            return _set_square_volume_left(state, index, amp)
        return _set_square_volume_right(state, index, amp)
    # noise oscillator (index 3)
    if left:
        return replace(state, noise=replace(state.noise, volume_left=amp))
    return replace(state, noise=replace(state.noise, volume_right=amp))


def _write_square_period(state: ApuState, index: int, data: int) -> ApuState:
    """Two-byte period build for a tone channel (14-bit, 0..0x3FFF)."""

    sq = state.squares[index]
    if data & 0x80:  # latch byte -> low nibble
        period = (sq.period & 0x3F00) | ((data << 4) & 0x00FF)
    else:  # data byte -> high 6 bits
        period = (sq.period & 0x00FF) | ((data << 8) & 0x3F00)
    squares = tuple(
        replace(sq, period=period) if i == index else s
        for i, s in enumerate(state.squares)
    )
    return replace(state, squares=squares)  # type: ignore[arg-type]


def write_left(state: ApuState, data: int) -> ApuState:
    """Handle a write to the LEFT sound port (NGPC I/O 0xA1).

    Sets tone periods and per-channel *left* volumes.
    """

    data &= 0xFF
    latch = data if (data & 0x80) else state.latch_left
    if data & 0x80:
        state = replace(state, latch_left=data)
    index = (latch >> 5) & 3
    if latch & 0x10:
        return _set_volume(state, index, VOLUMES[data & 0x0F], left=True)
    if index < 3:
        return _write_square_period(state, index, data)
    return state


def write_right(state: ApuState, data: int) -> ApuState:
    """Handle a write to the RIGHT sound port (NGPC I/O 0xA0).

    Sets per-channel *right* volumes plus the noise control / extra period.
    """

    data &= 0xFF
    latch = data if (data & 0x80) else state.latch_right
    if data & 0x80:
        state = replace(state, latch_right=data)
    index = (latch >> 5) & 3
    if latch & 0x10:
        return _set_volume(state, index, VOLUMES[data & 0x0F], left=False)
    if index == 2:
        # extra noise period, shares tone-3's two-byte build.
        noise = state.noise
        if data & 0x80:
            extra = (noise.period_extra & 0x3F00) | ((data << 4) & 0x00FF)
        else:
            extra = (noise.period_extra & 0x00FF) | ((data << 8) & 0x3F00)
        return replace(state, noise=replace(noise, period_extra=extra))
    if index == 3:
        select = data & 3
        tap = TAP_WHITE if (data & 0x04) else TAP_DISABLED
        return replace(
            state,
            noise=replace(state.noise, period_select=select, tap=tap, shifter=0x4000),
        )
    return state


def _lfsr_step(shifter: int, tap: int) -> tuple[int, bool]:
    """Advance the 15-bit LFSR one step; return (new_shifter, output_changed)."""

    changed = bool((shifter + 1) & 2)
    shifter = (((shifter << 14) ^ (shifter << tap)) & 0x4000) | (shifter >> 1)
    return shifter, changed


def run_until(state: ApuState, cycles: int) -> tuple[ApuState, list[tuple[int, int]]]:
    """Run all oscillators for ``cycles`` chip clocks.

    Returns the advanced state plus a list of ``(left, right)`` bipolar
    amplitude samples, one per chip clock. This is the naive oracle output;
    it is not band-limited.
    """

    if cycles <= 0:
        return state, []

    squares = list(state.squares)
    noise = state.noise
    noise_period = max(1, 2 * _active_noise_period(noise))
    samples: list[tuple[int, int]] = []

    for t in range(cycles):
        left = 0
        right = 0

        # Tone channels: toggle phase every `period` clocks; muted when
        # both volumes are 0 or the period is a near-ultrasonic <= 128.
        for i, sq in enumerate(squares):
            if sq.period > 128 and (sq.volume_left or sq.volume_right):
                if sq.period and (t % sq.period) == 0 and t > 0:
                    sq = replace(sq, phase=sq.phase ^ 1)
                    squares[i] = sq
                sign = 1 if sq.phase else -1
                left += sign * sq.volume_left
                right += sign * sq.volume_right

        # Noise channel.
        if noise.volume_left or noise.volume_right:
            sign = -1 if (noise.shifter & 1) else 1
            left += sign * noise.volume_left
            right += sign * noise.volume_right
            if t > 0 and (t % noise_period) == 0:
                shifter, _ = _lfsr_step(noise.shifter, noise.tap)
                noise = replace(noise, shifter=shifter)

        samples.append((left, right))

    new_state = replace(state, squares=tuple(squares), noise=noise)  # type: ignore[arg-type]
    return new_state, samples
