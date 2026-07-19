"""RAM search -- find WHERE a value lives, the way every retro debugger does it.

The classic loop: snapshot RAM, let the game change (take damage, score a point),
then keep only the addresses that moved the way the value did. A few passes narrow
tens of thousands of candidates down to the one holding "player HP", which you then
name and watch. Pure logic over a machine's `read`; the debug window drives it and
hands winners to the watch panel.

Three things here are worth knowing because the old version got them wrong:

* **One bulk read per pass, not one per candidate.** Refining used to call the
  machine's `read` once per surviving address -- 32768 ctypes round-trips to filter
  a 32 KB range. The whole window is read once and sliced with numpy instead.

* **Unaligned search.** The scan step used to be the value size, so a 16-bit value
  at an odd address simply could not be found. `aligned=False` steps one byte.

* **Undo.** Every pass pushes the previous state; one bad filter no longer means
  starting the hunt over.
"""
from __future__ import annotations

import numpy as np

SIZES = (1, 2, 4)

# How deep the undo stack goes. Each entry is three arrays over the surviving
# candidates, so even a full first pass over 32 KB is a few hundred KB -- cheap
# next to never being able to take a mis-click back.
UNDO_DEPTH = 16

# Absolute filters compare each candidate to a value you type.
ABSOLUTE = ("=", "!=", ">", "<", ">=", "<=")
# Relative filters compare it to what it was at the previous pass.
RELATIVE = ("changed", "unchanged", "increased", "decreased")
# Filters that compare to the previous pass BY a given amount (the operand is a
# delta, not a value): "lost exactly 3 HP" survives a hit that always costs 3.
DELTA = ("increased_by", "decreased_by", "changed_by")
# Compares each address's change COUNT to the operand -- the trick for finding a
# coordinate: move for 6 frames, then ask for the addresses that changed 6 times.
COUNTED = ("changes",)


class RamSearch:
    def __init__(self) -> None:
        self.lo = 0x004000
        self.hi = 0x00C000
        self.size = 1
        self.signed = False
        self.aligned = True
        self.started = False
        # Parallel arrays, all indexed the same way. Kept as numpy so a pass is a
        # handful of vector ops rather than a Python loop over 32k addresses.
        self.addrs = np.zeros(0, dtype=np.uint32)
        self.prev = np.zeros(0, dtype=np.int64)     # value at the last pass
        self.changes = np.zeros(0, dtype=np.uint32)  # times it moved since the search began
        self._last_seen = np.zeros(0, dtype=np.int64)  # value at the last change-tracking tick
        self._undo: list[tuple] = []

    # ---- reading -------------------------------------------------------
    def _decode(self, window: np.ndarray, offsets: np.ndarray) -> np.ndarray:
        """Little-endian values of `self.size` bytes at each offset, as int64.

        Composed byte by byte rather than by re-viewing the buffer, because a
        view would force alignment -- and finding the unaligned ones is the point.
        """
        out = np.zeros(len(offsets), dtype=np.int64)
        for i in range(self.size):
            out |= window[offsets + i].astype(np.int64) << np.int64(8 * i)
        if self.signed:
            bits = self.size * 8
            out = np.where(out >= (1 << (bits - 1)), out - (1 << bits), out)
        return out

    def _window(self, m) -> np.ndarray:
        span = max(0, self.hi - self.lo)
        return np.frombuffer(m.read(self.lo, span), dtype=np.uint8)

    def _live(self, m) -> np.ndarray:
        """Current value of every surviving candidate, in one read."""
        if not len(self.addrs):
            return np.zeros(0, dtype=np.int64)
        return self._decode(self._window(m), (self.addrs - self.lo).astype(np.int64))

    def format(self, value: int) -> str:
        if self.signed:
            return str(int(value))
        return f"{int(value) & ((1 << (self.size * 8)) - 1):0{self.size * 2}X}"

    # ---- search --------------------------------------------------------
    def new_search(self, m, lo: int, hi: int, size: int, signed: bool,
                   aligned: bool = True) -> int:
        """Start over: every slot in [lo, hi) becomes a candidate."""
        self.lo, self.hi = lo & 0xFFFFFF, hi & 0xFFFFFF
        self.size = size if size in SIZES else 1
        self.signed = bool(signed)
        self.aligned = bool(aligned)
        self._undo.clear()
        window = self._window(m)
        last = len(window) - self.size
        if last < 0:
            self.addrs = np.zeros(0, dtype=np.uint32)
            self.prev = np.zeros(0, dtype=np.int64)
            self.changes = np.zeros(0, dtype=np.uint32)
            self._last_seen = self.prev.copy()
            self.started = True
            return 0
        step = self.size if self.aligned else 1
        offsets = np.arange(0, last + 1, step, dtype=np.int64)
        self.addrs = (self.lo + offsets).astype(np.uint32)
        self.prev = self._decode(window, offsets)
        self.changes = np.zeros(len(offsets), dtype=np.uint32)
        self._last_seen = self.prev.copy()
        self.started = True
        return len(self.addrs)

    def track_changes(self, m) -> None:
        """Count, per candidate, how many times its value has moved.

        Call this once per emulated FRAME for the count to mean what a
        "number of changes" means. Called at a slower poll rate it still works,
        it just counts polls -- so the debug window drives it from the frame loop.
        """
        if not self.started or not len(self.addrs):
            return
        cur = self._live(m)
        self.changes += (cur != self._last_seen).astype(np.uint32)
        self._last_seen = cur

    def clear_changes(self) -> None:
        self.changes[:] = 0

    def _push_undo(self) -> None:
        self._undo.append((self.addrs.copy(), self.prev.copy(),
                           self.changes.copy(), self._last_seen.copy()))
        if len(self._undo) > UNDO_DEPTH:
            self._undo.pop(0)

    def can_undo(self) -> bool:
        return bool(self._undo)

    def undo(self) -> int:
        """Take back the last pass. Returns the restored candidate count."""
        if not self._undo:
            return len(self.addrs)
        self.addrs, self.prev, self.changes, self._last_seen = self._undo.pop()
        return len(self.addrs)

    def refine(self, m, op: str, operand: int | None = None) -> int:
        """Keep only candidates matching `op`, then adopt the current values as the
        new baseline. Returns how many remain.

        `operand` is a VALUE for the absolute ops, a DELTA for the *_by ops, and a
        change COUNT for "changes".
        """
        if not self.started:
            return 0
        if not len(self.addrs):
            return 0
        cur = self._live(m)
        old = self.prev

        if op in ABSOLUTE:
            if operand is None:
                return len(self.addrs)
            x = np.int64(operand)
            keep = {"=": cur == x, "!=": cur != x, ">": cur > x, "<": cur < x,
                    ">=": cur >= x, "<=": cur <= x}[op]
        elif op in RELATIVE:
            keep = {"changed": cur != old, "unchanged": cur == old,
                    "increased": cur > old, "decreased": cur < old}[op]
        elif op in DELTA:
            if operand is None:
                return len(self.addrs)
            d = np.int64(operand)
            keep = {"increased_by": cur == old + d,
                    "decreased_by": cur == old - d,
                    "changed_by": np.abs(cur - old) == abs(int(d))}[op]
        elif op in COUNTED:
            if operand is None:
                return len(self.addrs)
            keep = self.changes == np.uint32(operand)
        else:
            return len(self.addrs)

        self._push_undo()
        self.addrs = self.addrs[keep]
        self.prev = cur[keep]
        self.changes = self.changes[keep]
        self._last_seen = self._last_seen[keep]
        return len(self.addrs)

    def eliminate(self, addresses) -> int:
        """Drop specific addresses by hand (the 'that one is the timer' move)."""
        if not len(self.addrs):
            return 0
        drop = np.isin(self.addrs, np.array(list(addresses), dtype=np.uint32))
        if not drop.any():
            return len(self.addrs)
        self._push_undo()
        keep = ~drop
        self.addrs = self.addrs[keep]
        self.prev = self.prev[keep]
        self.changes = self.changes[keep]
        self._last_seen = self._last_seen[keep]
        return len(self.addrs)

    def results(self, m, limit: int = 1000) -> list[tuple[int, str, str, int]]:
        """Up to `limit` candidates as (address, live value, previous value, changes),
        lowest address first. Showing the previous value next to the live one is what
        makes a filter's effect legible."""
        if not len(self.addrs):
            return []
        try:
            cur = self._live(m)
        except Exception:
            cur = self.prev
        n = min(limit, len(self.addrs))
        return [(int(self.addrs[i]), self.format(cur[i]), self.format(self.prev[i]),
                 int(self.changes[i])) for i in range(n)]

    def count(self) -> int:
        return len(self.addrs)

    def clear(self) -> None:
        self.addrs = np.zeros(0, dtype=np.uint32)
        self.prev = np.zeros(0, dtype=np.int64)
        self.changes = np.zeros(0, dtype=np.uint32)
        self._last_seen = np.zeros(0, dtype=np.int64)
        self._undo.clear()
        self.started = False
