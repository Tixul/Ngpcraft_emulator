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

from core.watches import OPS

_COND = re.compile(r"^\s*([0-9A-Fa-f]+)(?:\.([124]))?\s*(==|!=|<=|>=|=|<|>)\s*(\S+)\s*$")


class ExecBreak:
    __slots__ = ("pc", "cond", "enabled")

    def __init__(self, pc: int = 0, cond: str = "", enabled: bool = True) -> None:
        self.pc = pc & 0xFFFFFF
        self.cond = cond or ""
        self.enabled = bool(enabled)

    def cond_true(self, m) -> bool:
        """Evaluate the optional guard. No/!malformed condition => always fires."""
        if not self.cond:
            return True
        mo = _COND.match(self.cond)
        if not mo:
            return True
        addr = int(mo.group(1), 16)
        size = int(mo.group(2)) if mo.group(2) else 1
        op = "=" if mo.group(3) == "==" else mo.group(3)
        fn = OPS.get(op)
        if fn is None:
            return True
        try:
            operand = int(mo.group(4), 0)
        except ValueError:
            return True
        cur = int.from_bytes(m.read(addr & 0xFFFFFF, size), "little")
        return fn(cur, operand)

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
