"""Export captured music to the NGPC sound creator's own song format (.ngps).

A .ngps song is a tracker: patterns of rows over 4 channels (3 tone + noise). Each
cell is either null (sustain / empty) or {"n": note, "i": instrument, "a": attn}
where n is a MIDI note (1-127) or 0xFF for note-off, and a=0xFF means "use the
instrument's default level".

We sample the chip once per frame, turn each channel's period+volume into a note,
then quantise to rows at the creator's default speed (tpr = 8 ticks/row at 60 Hz,
so 8 frames = 1 row), split into 64-row patterns, and chain them with an order list.
The composer opens the result in the tracker and re-voices it with real instruments.
"""
from __future__ import annotations

import json
import math

FRAMES_PER_ROW = 8       # matches the creator's default ticks-per-row at 60 Hz
PATTERN_LEN = 64         # rows per pattern (TrackerDocument::kDefaultLength)
CHANNELS = 4             # 3 tone + 1 noise
NOTE_OFF = 0xFF
NOISE_NOTE = 48          # the noise channel's note; the noise instrument maps it


def _freq_to_midi(freq: float) -> int:
    if freq <= 0:
        return -1
    n = int(round(69 + 12 * math.log2(freq / 440.0)))
    return n if 1 <= n <= 127 else -1


class NgpsRecorder:
    """Fed one APU state per frame; builds a .ngps song."""

    def __init__(self) -> None:
        self.frames: list[tuple[int, int, int, int]] = []   # per-frame notes, -1 = silent

    def begin(self) -> None:
        self.frames = []

    def feed(self, st) -> None:
        notes = []
        for i in range(3):
            per = st.square_period[i]
            vol = max(st.square_vol_left[i], st.square_vol_right[i])
            notes.append(_freq_to_midi(96000.0 / per) if (per > 0 and vol > 0) else -1)
        nvol = max(st.noise_vol_left, st.noise_vol_right)
        notes.append(NOISE_NOTE if nvol > 0 else -1)
        self.frames.append((notes[0], notes[1], notes[2], notes[3]))

    def empty(self) -> bool:
        return not any(n >= 0 for row in self.frames for n in row)

    def build(self) -> bytes:
        rows = self.frames[::FRAMES_PER_ROW]                 # one row every 8 frames
        if not rows:
            rows = [(-1, -1, -1, -1)]
        # pad to a whole number of patterns
        while len(rows) % PATTERN_LEN:
            rows.append((-1, -1, -1, -1))
        nrows = len(rows)

        # per channel: a cell only when the note changes; null while it holds
        cells = [[None] * nrows for _ in range(CHANNELS)]
        prev = [-1] * CHANNELS
        for r, row in enumerate(rows):
            for c in range(CHANNELS):
                note = row[c]
                if note == prev[c]:
                    continue                                 # sustain -> leave null
                if note >= 0:
                    cells[c][r] = {"n": note, "i": 0, "a": NOTE_OFF}
                elif prev[c] >= 0:
                    cells[c][r] = {"n": NOTE_OFF, "i": 0, "a": NOTE_OFF}   # note-off
                prev[c] = note

        npat = nrows // PATTERN_LEN
        patterns = []
        for p in range(npat):
            lo, hi = p * PATTERN_LEN, (p + 1) * PATTERN_LEN
            patterns.append({
                "channels": [cells[c][lo:hi] for c in range(CHANNELS)],
                "length": PATTERN_LEN,
            })
        song = {
            "loop_point": 0,
            "order": list(range(npat)),
            "patterns": patterns,
            "version": 1,
        }
        return json.dumps(song, separators=(",", ":")).encode("utf-8")
