"""How fast should the emulator run? Ask the sound card, not the wall clock.

THE BUG THIS EXISTS TO KILL. The player used to advance one emulated frame per
`QTimer` tick, and the tick was `1000 // 60` -- which is **16 ms, not 16.667**,
so the loop free-ran at **62.5 fps**. Measured on Sonic, that is 62.5 x 734 =
45 856 stereo frames of audio produced every second while the card drains
exactly 44 100. The surplus (about 1 750 a second) piled into a queue that
nothing bounded:

    t= 19.1s  fps=63.0  queue=    0   latency=  53 ms
    t= 23.2s  fps=62.5  queue= 6175   latency= 220 ms
    t= 25.2s  fps=62.5  queue= 9738   latency= 301 ms     <- linear, forever

That queue IS the "sound is 1 or 2 seconds late" the playtest reported: the
slope puts it at 1.2 s after a minute. And on a heavy scene the same free-run
went the other way -- 57 fps, the queue emptied, and the card played silence.
One root cause, two opposite symptoms.

THE FIX. The sound card is the only accurate clock in the machine: it drains
44 100 frames a second, forever, and it cannot drift against itself. A wall
clock can, and does. So we invert the control -- we do not run frames on a
timer and hope the audio keeps up; we run frames only while the card is SHORT
of audio, and stop as soon as it has its cushion. The backlog only drains in
real time, so the loop self-limits to exactly real speed. No drift is possible,
because nothing is being counted -- we are following, not measuring.

The timer that calls this only has to fire OFTEN ENOUGH (every few ms). Its
accuracy stops mattering entirely, which is the point: an inaccurate timer was
the bug.

WHAT THIS DOES NOT FIX. Pacing cannot make a slow machine fast. If the front
end physically cannot produce 60 frames a second, the backlog drains and the
card starves no matter how cleverly we schedule -- the answer to that is to
make frames cheaper (the C++ renderer), not to schedule differently. What
pacing guarantees is that a machine which CAN keep up will never accumulate
latency, and that a machine which briefly cannot will drop video frames
(`max_frames_per_tick` lets it catch up) rather than break the audio. Audio
continuity beats video smoothness: a dropped frame is invisible, a gap is not.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

NGPC_FPS = 60
AUDIO_RATE_HZ = 44100


@dataclass(frozen=True)
class FramePacer:
    """Decides how many emulated frames to advance, from the unplayed audio.

    `target_latency_s` is the cushion we keep between the emulator and the
    speaker. It buys tolerance against a frame that takes longer than its
    share -- and frames DO swing, because one atomic instruction can span 38
    scanlines, so a frame's audio is anywhere from 660 to 880 samples. Too
    small and that swing starves the card; too large and the player feels
    spongy. 60 ms is about four frames of slack.
    """

    audio_rate_hz: int = AUDIO_RATE_HZ
    fps: int = NGPC_FPS
    target_latency_s: float = 0.060
    max_frames_per_tick: int = 3

    @property
    def target_frames(self) -> int:
        """The audio cushion we aim to keep unplayed, in stereo frames."""
        return int(self.target_latency_s * self.audio_rate_hz)

    @property
    def audio_frames_per_video_frame(self) -> float:
        """What one emulated frame is worth in audio. 44100/60 = 735."""
        return self.audio_rate_hz / self.fps

    def frames_to_run(self, backlog_frames: int) -> int:
        """How many emulated frames to advance now.

        `backlog_frames` is ALL the audio generated but not yet played --
        whatever sits in our own queue plus whatever the card holds and has
        not reached the speaker. That sum is the latency the player hears, and
        it is the only input this needs.

        Return 0 when the cushion is full: that is the loop idling at exactly
        real time, which is the whole objective.
        """
        if backlog_frames >= self.target_frames:
            return 0
        deficit = self.target_frames - backlog_frames
        needed = math.ceil(deficit / self.audio_frames_per_video_frame)
        return min(needed, self.max_frames_per_tick)
