"""Symbol table loader for t900ld .map files.

t900ld map format:
    # header comments
    === Linker symbols ===
      _SomeSymbol              0xADDRESS
    === Public symbols ===
      _function_name           0xADDRESS

This module loads the file into a SymbolTable that supports:
  - name -> address lookup
  - address -> "nearest symbol with addr <= PC" lookup (for resolving an
    arbitrary PC back to the function that owns it)
  - section listing for diagnostics

This is the first symbol-aware layer for the debugger. It is intentionally
read-only: the map file is the authoritative source and the table never
fabricates symbols that are not in it.
"""

from __future__ import annotations

import re
from bisect import bisect_right
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple


_SECTION_HEADER_RE = re.compile(r"^===\s*(.+?)\s*===\s*$")
_SYMBOL_LINE_RE = re.compile(
    r"^\s*([A-Za-z_.][A-Za-z_0-9.]*)\s+0x([0-9A-Fa-f]+)\s*$"
)


@dataclass(frozen=True)
class Symbol:
    name: str
    address: int
    section: str  # which "=== ... ===" header the symbol appeared under


@dataclass
class SymbolTable:
    """Static symbol table loaded from a t900ld .map file.

    Internally:
      - `_by_name` is a dict for direct name -> Symbol lookup.
      - `_sorted_addresses` is a sorted list of addresses; combined with
        `_at_address` (dict address -> [Symbol, ...]) it supports the
        "nearest symbol with addr <= PC" query in O(log N).

    Two symbols may share the same address (e.g. label + section start).
    `lookup_address` returns the first listed at that address. To see all
    symbols at the same address, use `symbols_at_address`.
    """

    source_path: str = ""
    _by_name: Dict[str, Symbol] = field(default_factory=dict)
    _at_address: Dict[int, List[Symbol]] = field(default_factory=dict)
    _sorted_addresses: List[int] = field(default_factory=list)
    sections: List[str] = field(default_factory=list)

    def __len__(self) -> int:
        return len(self._by_name)

    def lookup_name(self, name: str) -> Optional[Symbol]:
        """Exact name match. Returns None if not found."""
        return self._by_name.get(name)

    def lookup_address(self, address: int) -> Optional[Symbol]:
        """Nearest symbol with address <= the requested PC.

        Returns None when the PC is below the lowest known symbol. This
        deliberately does NOT guess past the highest symbol; if a PC sits
        beyond the last symbol with no upper bound info, the caller can
        still receive that last symbol — that is the expected debugger
        behavior (PC belongs to the function it last entered).
        """
        if not self._sorted_addresses:
            return None
        idx = bisect_right(self._sorted_addresses, address) - 1
        if idx < 0:
            return None
        addr = self._sorted_addresses[idx]
        return self._at_address[addr][0]

    def symbols_at_address(self, address: int) -> List[Symbol]:
        """All symbols that share exactly this address. Empty list if none."""
        return list(self._at_address.get(address, ()))

    def symbols_in_range(self, start: int, end_inclusive: int) -> List[Symbol]:
        """All symbols whose address is in [start, end_inclusive].

        Returned sorted by address.
        """
        out: List[Symbol] = []
        for addr in self._sorted_addresses:
            if addr < start:
                continue
            if addr > end_inclusive:
                break
            out.extend(self._at_address[addr])
        return out

    def section_summary(self) -> List[Tuple[str, int]]:
        """For each section header seen in the map file, count of symbols."""
        per: Dict[str, int] = {s: 0 for s in self.sections}
        for sym in self._by_name.values():
            per[sym.section] = per.get(sym.section, 0) + 1
        return [(s, per[s]) for s in self.sections]


def load_map(path: str) -> SymbolTable:
    """Parse a t900ld .map file and return a SymbolTable.

    Tolerant parser: unknown sections are still recorded under their
    header name, unrecognized lines are ignored. The file must exist; a
    missing file raises FileNotFoundError.
    """
    p = Path(path)
    if not p.is_file():
        raise FileNotFoundError(f"map file not found: {path}")

    table = SymbolTable(source_path=str(p))
    current_section = "unspecified"

    with p.open("r", encoding="utf-8", errors="replace") as f:
        for raw_line in f:
            line = raw_line.rstrip("\n")
            if not line:
                continue
            stripped = line.lstrip()
            if stripped.startswith("#"):
                continue
            mh = _SECTION_HEADER_RE.match(stripped)
            if mh:
                current_section = mh.group(1)
                if current_section not in table.sections:
                    table.sections.append(current_section)
                continue
            ms = _SYMBOL_LINE_RE.match(line)
            if not ms:
                continue
            name = ms.group(1)
            address = int(ms.group(2), 16)
            sym = Symbol(name=name, address=address, section=current_section)
            table._by_name[name] = sym
            bucket = table._at_address.setdefault(address, [])
            bucket.append(sym)

    table._sorted_addresses = sorted(table._at_address.keys())
    return table
