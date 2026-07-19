"""The pacer must hold real time against a real sound card, for minutes.

The gate is a virtual card that drains exactly 44 100 stereo frames a second --
the one thing a real card does reliably -- and an emulator whose per-frame audio
SWINGS the way the real one does (660..880 samples, because a single atomic
instruction can span 38 scanlines).

And the gate PROVES IT CAN FAIL: `test_the_old_wall_clock_loop_drifts` runs the
policy the player actually shipped (one frame per 16 ms tick) through the same
simulator and shows it piling up more than a second of latency in a minute --
which is the bug the playtest reported, reproduced in a unit test. A pacing test
that only ever passes would be worth nothing.
"""

from __future__ import annotations

from core.frame_pacer import FramePacer

AUDIO_RATE = 44100

# A frame's audio swings; the mean has to stay honest at 44100/60 = 734.75, so the
# swing sums to zero. Measured on Sonic: mean 733.7, min 661, max 882.
_SWING = (-75, +75, -110, +110, -40, +40, 0, 0)


def _frame_audio(index: int) -> int:
    """Stereo frames one emulated video frame produces. Mean 734.75, jittery."""
    base = 734 if index % 4 == 0 else 735      # mean 734.75
    return base + _SWING[index % len(_SWING)]


def _simulate(policy, seconds: float, tick_s: float) -> tuple[int, list[float]]:
    """Run `policy` against a card draining exactly AUDIO_RATE frames a second.

    `policy(backlog) -> n` says how many emulated frames to advance. The machine
    is assumed fast enough to actually run them -- this gates the POLICY, not the
    renderer. Returns the frames run and the backlog after every tick.
    """
    backlog = 0.0        # audio generated but not yet played, in stereo frames
    frames_run = 0
    history: list[float] = []
    for _ in range(int(seconds / tick_s)):
        backlog = max(0.0, backlog - tick_s * AUDIO_RATE)     # the card played
        for _ in range(policy(backlog)):
            backlog += _frame_audio(frames_run)
            frames_run += 1
        history.append(backlog)
    return frames_run, history


# ---------------------------------------------------------------- the policy


def test_full_cushion_runs_nothing() -> None:
    """When the card has its cushion, the right thing to do is NOTHING.

    This is the loop idling at exactly real time -- the whole objective.
    """
    pacer = FramePacer()
    assert pacer.frames_to_run(pacer.target_frames) == 0
    assert pacer.frames_to_run(pacer.target_frames + 5000) == 0


def test_one_frame_short_runs_one_frame() -> None:
    pacer = FramePacer()
    backlog = pacer.target_frames - int(pacer.audio_frames_per_video_frame)
    assert pacer.frames_to_run(backlog) == 1


def test_an_empty_card_is_refilled_but_never_in_one_gulp() -> None:
    """Catching up is capped: a stall must not fast-forward the game."""
    pacer = FramePacer(max_frames_per_tick=3)
    assert pacer.frames_to_run(0) == 3


# ------------------------------------------------------------- the long run


def test_pacer_holds_real_time_for_a_minute() -> None:
    """60 virtual seconds: 60 fps, bounded latency, and never a starved card."""
    pacer = FramePacer()
    frames, history = _simulate(pacer.frames_to_run, seconds=60.0, tick_s=0.004)

    # The card, not the timer, set the speed: 44100/734.75 = 60.02 frames a second.
    assert abs(frames - 3601) < 36, f"{frames} frames in 60 s -- not real time"

    # The latency is BOUNDED. It cannot grow, because we stop as soon as the
    # cushion is full. Worst case is the cushion plus one tick's catch-up burst.
    ceiling = pacer.target_frames + pacer.max_frames_per_tick * 880
    assert max(history) <= ceiling, f"latency reached {max(history)} frames"

    # And the card never ran dry after the initial fill: no gaps, no crackle.
    steady = history[500:]          # skip the first 2 s while the cushion fills
    assert min(steady) > 0, "the sound card starved"


def test_the_old_wall_clock_loop_drifts() -> None:
    """The policy the player shipped, in the same simulator: it MUST fail.

    `QTimer(1000 // 60)` is a 16 ms tick -- 62.5 fps, not 60 -- advancing one
    frame each time regardless of what the card has left. If this test ever
    stops showing a growing backlog, the simulator has stopped modelling the
    bug and every other assertion here is worthless.
    """
    frames, history = _simulate(lambda _backlog: 1, seconds=60.0, tick_s=0.016)

    assert frames == 3750                       # 62.5 fps: the truncation, exactly
    assert history[-1] > AUDIO_RATE, (
        f"expected the old loop to pile up over a second of audio, "
        f"got {history[-1] / AUDIO_RATE:.2f} s"
    )
    # And it never levels off. The surplus is CONSTANT (about 1 775 frames a
    # second), so the backlog is linear in time -- the second minute would be as
    # bad again, and the tenth worse still. That is why the playtest heard the
    # delay grow the longer it played.
    mid = len(history) // 2
    first_half = history[mid] - history[0]
    second_half = history[-1] - history[mid]
    assert second_half > 0.9 * first_half, "the old loop plateaued -- it does not"
