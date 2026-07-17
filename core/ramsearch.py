"""RAM search -- find WHERE a value lives, the way every retro debugger does it.

The classic loop: snapshot RAM, let the game change (take damage, score a point),
then keep only the addresses that moved the way the value did. A few passes narrow
tens of thousands of candidates down to the one holding "player HP", which you then
name and watch. Pure logic over a machine's `read`; the debug window drives it and
hands winners to the watch panel.
"""
from __future__ import annotations

SIZES = (1, 2, 4)

# Absolute filters compare each candidate to a value you type; relative filters
# compare it to what it was at the previous pass.
ABSOLUTE = {
    "=":  lambda c, o, x: c == x,
    "!=": lambda c, o, x: c != x,
    ">":  lambda c, o, x: c > x,
    "<":  lambda c, o, x: c < x,
}
RELATIVE = {
    "changed":   lambda c, o: c != o,
    "unchanged": lambda c, o: c == o,
    "increased": lambda c, o: c > o,
    "decreased": lambda c, o: c < o,
}


class RamSearch:
    def __init__(self) -> None:
        self.lo = 0x004000
        self.hi = 0x00C000
        self.size = 1
        self.signed = False
        self.cands: dict[int, int] = {}     # addr -> value at the last pass (raw)
        self.started = False

    def _val(self, raw: int) -> int:
        if not self.signed:
            return raw
        bits = self.size * 8
        return raw - (1 << bits) if raw >= (1 << (bits - 1)) else raw

    def format(self, raw: int) -> str:
        return f"{raw:0{self.size * 2}X}" if not self.signed else str(self._val(raw))

    def new_search(self, m, lo: int, hi: int, size: int, signed: bool) -> int:
        """Start over: every aligned slot in [lo, hi) becomes a candidate."""
        self.lo, self.hi = lo & 0xFFFFFF, hi & 0xFFFFFF
        self.size = size if size in SIZES else 1
        self.signed = bool(signed)
        data = m.read(self.lo, max(0, self.hi - self.lo))
        self.cands = {
            self.lo + off: int.from_bytes(data[off:off + self.size], "little")
            for off in range(0, len(data) - self.size + 1, self.size)
        }
        self.started = True
        return len(self.cands)

    def refine(self, m, op: str, operand: int | None = None) -> int:
        """Keep only candidates matching `op` (absolute needs `operand`; relative
        compares to the previous pass), then adopt the current values as the new
        baseline. Returns how many remain."""
        if not self.started:
            return 0
        kept: dict[int, int] = {}
        abs_fn = ABSOLUTE.get(op)
        rel_fn = RELATIVE.get(op)
        for addr, old in self.cands.items():
            cur = int.from_bytes(m.read(addr, self.size), "little")
            if abs_fn is not None:
                ok = operand is not None and abs_fn(self._val(cur), self._val(old), operand)
            elif rel_fn is not None:
                ok = rel_fn(self._val(cur), self._val(old))
            else:
                ok = False
            if ok:
                kept[addr] = cur
        self.cands = kept
        return len(kept)

    def results(self, m, limit: int = 1000) -> list[tuple[int, str]]:
        """Up to `limit` candidates as (address, live formatted value), lowest first."""
        out = []
        for addr in sorted(self.cands)[:limit]:
            try:
                cur = int.from_bytes(m.read(addr, self.size), "little")
                out.append((addr, self.format(cur)))
            except Exception:
                out.append((addr, "??"))
        return out

    def count(self) -> int:
        return len(self.cands)

    def clear(self) -> None:
        self.cands = {}
        self.started = False
