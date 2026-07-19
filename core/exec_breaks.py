"""Live execution breakpoints -- pause the game when PC hits an address.

Distinct from `core/breakpoints.py` (the passive, event-log-matching registry): this
is the interactive debugger's list. The native core already stops at an armed PC
(STATUS_BREAKPOINT); this holds the host-side list, an optional guard condition, and
per-ROM persistence, and the play loop resumes past a breakpoint whose guard is false.

Condition syntax (optional):  ADDR[.SIZE] OP VALUE
    "4812 = 0"      fire only when byte 0x4812 == 0
    "4a00.2 > 0x100" ...when the 2-byte word at 0x4A00 > 256
ADDR is hex; VALUE is int(...,0) so "0x10" or "16" both work.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from core.expr import ExprError, compile_expr

# The ORIGINAL condition syntax: `ADDR[.SIZE] OP VALUE`, where ADDR is HEX and is
# implicitly a memory READ -- `4812 = 0` meant "the byte at 0x4812 is zero".
#
# ⚠️ That is not what the same text means in the expression language, where `4812`
# is a decimal literal and the whole thing reads "4812 equals 0", i.e. never true.
# Conditions saved by earlier versions live in watches/*.breaks.json, so they are
# detected here and rewritten rather than silently changing meaning.
_LEGACY = re.compile(r"^\s*([0-9A-Fa-f]+)(?:\.([124]))?\s*(==|!=|<=|>=|=|<|>)\s*(\S+)\s*$")


def _modernise(cond: str) -> str:
    """Rewrite a legacy `ADDR.SIZE OP VALUE` guard into the expression language."""
    mo = _LEGACY.match(cond)
    if not mo:
        return cond
    addr, size, op, value = mo.group(1), mo.group(2) or "1", mo.group(3), mo.group(4)
    op = "==" if op == "=" else op
    return f"[$" + addr + f",{size}] {op} {value}"


class ExecBreak:
    __slots__ = ("pc", "cond", "enabled", "_expr", "_expr_src", "_error")

    def __init__(self, pc: int = 0, cond: str = "", enabled: bool = True) -> None:
        self.pc = pc & 0xFFFFFF
        self.cond = cond or ""
        self.enabled = bool(enabled)
        self._expr = None
        self._expr_src = None
        self._error = ""

    @property
    def error(self) -> str:
        """Why the condition was rejected, or '' -- so the UI can SAY so."""
        return self._error

    def compile(self, symbols=None) -> str:
        """(Re)compile the guard. Returns the error message, or ''."""
        self._expr = None
        self._expr_src = self.cond
        self._error = ""
        if not self.cond:
            return ""
        try:
            self._expr = compile_expr(_modernise(self.cond), symbols)
        except ExprError as exc:
            self._error = str(exc)
        return self._error

    def cond_true(self, m, symbols=None) -> bool:
        """Evaluate the optional guard.

        A BROKEN condition fires, deliberately: a breakpoint you asked for that
        silently never triggers is far worse than one that triggers too often. But
        unlike the old code, the reason is recorded in `.error` so the UI can show
        it instead of leaving you to wonder.
        """
        if not self.cond:
            return True
        if self._expr is None or self._expr_src != self.cond:
            self.compile(symbols)
        if self._expr is None:
            return True                    # malformed -> fire, and `.error` says why
        try:
            return self._expr.is_true(m)
        except ExprError as exc:
            self._error = str(exc)
            return True

    def to_dict(self) -> dict:
        return {"pc": self.pc, "cond": self.cond, "enabled": self.enabled}

    @classmethod
    def from_dict(cls, d: dict) -> "ExecBreak":
        return cls(int(d.get("pc", 0)), str(d.get("cond", "")), bool(d.get("enabled", True)))


class ExecBreakSet:
    def __init__(self) -> None:
        self.items: list[ExecBreak] = []

    def enabled_pcs(self) -> list[int]:
        return sorted({b.pc for b in self.items if b.enabled})

    def at(self, pc: int) -> ExecBreak | None:
        for b in self.items:
            if b.enabled and b.pc == pc:
                return b
        return None

    def save(self, path: Path) -> None:
        path = Path(path)
        if not self.items:
            if path.exists():
                try:
                    path.unlink()
                except OSError:
                    pass
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps([b.to_dict() for b in self.items], indent=2),
                        encoding="utf-8")

    def load(self, path: Path) -> None:
        self.items = []
        path = Path(path)
        if not path.exists():
            return
        try:
            self.items = [ExecBreak.from_dict(d) for d in
                          json.loads(path.read_text(encoding="utf-8"))]
        except (ValueError, OSError, TypeError):
            self.items = []
