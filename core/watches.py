"""Named memory watches for the debugger.

A watch pins a LOGICAL NAME to a memory address and reads its live value; it can
also BREAK (pause the game) when that value meets a condition. Watches persist
per-ROM, so a homebrew dev keeps their map -- "player_hp @ 0x4812", "state @
0x4a00" -- across sessions instead of re-typing raw addresses every time.

This module is pure logic + JSON persistence: no Qt, no core internals. The debug
window edits a WatchSet; the play loop calls `WatchSet.check(machine)` once per
frame and pauses on the first hit.
"""
from __future__ import annotations

import json
from pathlib import Path

SIZES = (1, 2, 4)
FORMATS = ("hex", "u", "s")            # hex, unsigned decimal, signed decimal

# Break conditions. "" = watch only (no break), "change" = break when it moves,
# "write" = break when ANY code writes the address (handled via the core write-log,
# not by value comparison here), and the comparison ops fire on the RISING edge of
# the condition becoming true.
BREAK_NONE = ""
BREAK_CHANGE = "change"
BREAK_WRITE = "write"
OPS = {
    "=":  lambda v, x: v == x,
    "!=": lambda v, x: v != x,
    "<":  lambda v, x: v < x,
    ">":  lambda v, x: v > x,
    "<=": lambda v, x: v <= x,
    ">=": lambda v, x: v >= x,
}
BREAKS = (BREAK_NONE, BREAK_CHANGE, BREAK_WRITE, *OPS.keys())


def _signed(raw: int, size: int) -> int:
    bits = size * 8
    return raw - (1 << bits) if raw >= (1 << (bits - 1)) else raw


class Watch:
    __slots__ = ("name", "addr", "size", "fmt", "brk", "value", "lock", "_last", "_armed")

    def __init__(self, name: str = "", addr: int = 0, size: int = 1,
                 fmt: str = "hex", brk: str = "", value: int = 0,
                 lock: bool = False) -> None:
        self.name = name
        self.addr = addr & 0xFFFFFF
        self.size = size if size in SIZES else 1
        self.fmt = fmt if fmt in FORMATS else "hex"
        self.brk = brk if brk in BREAKS else ""
        self.value = value                 # operand for the comparison ops / lock target
        self.lock = bool(lock)             # freeze: keep writing `value` every frame
        self._last: int | None = None      # last raw value (for "change")
        self._armed = True                 # rising-edge arm for the comparison ops

    # -- reading / formatting ------------------------------------------------
    def read_raw(self, m) -> int:
        return int.from_bytes(m.read(self.addr, self.size), "little")

    def display(self, raw: int) -> int:
        return _signed(raw, self.size) if self.fmt == "s" else raw

    def format(self, raw: int) -> str:
        if self.fmt == "hex":
            return f"{raw:0{self.size * 2}X}"
        return str(self.display(raw))

    # -- the break check (called once per frame) -----------------------------
    def lock_bytes(self) -> bytes:
        """The value to write back each frame when frozen (little-endian, sized)."""
        mask = (1 << (self.size * 8)) - 1
        return (self.value & mask).to_bytes(self.size, "little")

    def check(self, m) -> str | None:
        """Return a human reason if this watch should break NOW, else None.
        Always updates internal state so edge detection stays correct.
        `write` breaks are NOT evaluated here -- the play loop detects those from the
        core write-log, which also tells it WHICH pc did the write."""
        if not self.brk or self.brk == BREAK_WRITE:
            return None
        raw = self.read_raw(m)
        hit = None
        who = self.name or f"{self.addr:06X}"
        if self.brk == BREAK_CHANGE:
            if self._last is not None and raw != self._last:
                hit = f"{who}: {self.format(self._last)} -> {self.format(raw)}"
        else:
            op = OPS.get(self.brk)
            if op is not None:
                cur = op(self.display(raw), self.value)
                if cur and self._armed:
                    hit = f"{who} {self.brk} {self.value}  (= {self.format(raw)})"
                self._armed = not cur          # re-arm once the condition is false again
        self._last = raw
        return hit

    def to_dict(self) -> dict:
        return {"name": self.name, "addr": self.addr, "size": self.size,
                "fmt": self.fmt, "brk": self.brk, "value": self.value, "lock": self.lock}

    @classmethod
    def from_dict(cls, d: dict) -> "Watch":
        return cls(str(d.get("name", "")), int(d.get("addr", 0)),
                   int(d.get("size", 1)), str(d.get("fmt", "hex")),
                   str(d.get("brk", "")), int(d.get("value", 0)),
                   bool(d.get("lock", False)))


class WatchSet:
    def __init__(self) -> None:
        self.watches: list[Watch] = []

    def has_value_breaks(self) -> bool:
        return any(w.brk and w.brk != BREAK_WRITE for w in self.watches)

    def write_watches(self) -> list["Watch"]:
        return [w for w in self.watches if w.brk == BREAK_WRITE]

    def write_range(self) -> tuple[int, int] | None:
        """The [lo, hi] address window covering every 'write' break, or None. The play
        loop arms the core write-log over this range and matches exact addresses."""
        ww = self.write_watches()
        if not ww:
            return None
        lo = min(w.addr for w in ww)
        hi = max(w.addr + w.size - 1 for w in ww)
        return lo, hi

    def write_hit(self, addr: int):
        """The 'write' watch whose bytes cover `addr`, or None."""
        for w in self.write_watches():
            if w.addr <= addr < w.addr + w.size:
                return w
        return None

    def locked(self) -> list["Watch"]:
        return [w for w in self.watches if w.lock]

    def check(self, m) -> str | None:
        """Evaluate every VALUE watch (so edge state stays fresh) and return the first
        break reason. 'write' watches are handled separately, from the write-log."""
        hit = None
        for w in self.watches:
            r = w.check(m)
            if r and hit is None:
                hit = r
        return hit

    def rearm(self) -> None:
        for w in self.watches:
            w._last = None
            w._armed = True

    # -- per-ROM persistence -------------------------------------------------
    def save(self, path: Path) -> None:
        path = Path(path)
        if not self.watches:
            # nothing to keep -- remove a stale file rather than leave an empty map
            if path.exists():
                try:
                    path.unlink()
                except OSError:
                    pass
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps([w.to_dict() for w in self.watches], indent=2),
                        encoding="utf-8")

    def load(self, path: Path) -> None:
        self.watches = []
        path = Path(path)
        if not path.exists():
            return
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            self.watches = [Watch.from_dict(d) for d in raw]
        except (ValueError, OSError, TypeError):
            self.watches = []
