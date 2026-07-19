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


# How far past a symbol an address may sit and still be called part of it. Big
# enough for a fat data table, small enough that BIOS space (0xFFxxxx) can never
# be attributed to a game symbol down in ROM (0x20xxxx). See `lookup_address`.
DEFAULT_MAX_SPAN = 0x2000

_SECTION_HEADER_RE = re.compile(r"^===\s*(.+?)\s*===\s*$")
_SYMBOL_LINE_RE = re.compile(
    r"^\s*([A-Za-z_.][A-Za-z_0-9.]*)\s+0x([0-9A-Fa-f]+)\s*$"
)

# --- Toshiba tulink (.map from the official cc900/tulink chain) --------------
# The other map format in this project, and the one every real build produces:
#
#     Symbol table for main.abs
#       Symbol       Address  In-sec   Cross-reference
#       ------------ -------- -------- ------------------------
#     Input module : main_c
#       _main          20186E f_code
#       _BitmapNewMask                 <- a long name WRAPS...
#                      20160E f_const  <- ...and its address is on the next line
#
# Two things here break a naive parser and did: the address has no `0x` prefix,
# and a name longer than the column width is pushed onto its own line. Missing
# either means silently loading zero symbols from a map that is full of them.
_TU_TABLE_START_RE = re.compile(r"^\s*Symbol table for\b", re.IGNORECASE)
_TU_MODULE_RE = re.compile(r"^\s*Input module\s*:\s*(\S+)")
_TU_SYMBOL_RE = re.compile(
    r"^\s{2,}([A-Za-z_.$][A-Za-z_0-9.$]*)\s+([0-9A-Fa-f]{1,8})\s+(\S+)"
)
# A name alone on its line (the wrapped case): no address follows it.
_TU_NAME_ONLY_RE = re.compile(r"^\s{2,}([A-Za-z_.$][A-Za-z_0-9.$]*)\s*$")
# The continuation line carrying the wrapped name's address.
_TU_ADDR_ONLY_RE = re.compile(r"^\s{2,}([0-9A-Fa-f]{1,8})\s+(\S+)")


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

    def lookup_address(self, address: int,
                       max_span: int = DEFAULT_MAX_SPAN) -> Optional[Symbol]:
        """Nearest symbol with address <= the requested PC, within `max_span` bytes.

        Returns None when the PC is below the lowest known symbol, or so far past
        the nearest one that the attribution would be meaningless.

        ⚠️ That distance guard is not decoration. A map covers the GAME; the PC
        spends plenty of time in BIOS space megabytes above the last symbol, and an
        unbounded "nearest symbol below" happily reported things like
        `INT_LV_SET+DF0B07` for a BIOS address — a name, an offset, and no truth in
        either. The same happens across the RAM/ROM boundary, where the symbol
        below an address can be in an entirely different memory region. Past
        `max_span` the honest answer is "no idea", so that is what this returns.

        `max_span` is deliberately generous (a large data table is still a symbol
        someone wants named) but far below the distance between memory regions.
        """
        if not self._sorted_addresses:
            return None
        idx = bisect_right(self._sorted_addresses, address) - 1
        if idx < 0:
            return None
        addr = self._sorted_addresses[idx]
        if max_span and (address - addr) > max_span:
            return None
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


def _add(table: SymbolTable, name: str, address: int, section: str) -> None:
    sym = Symbol(name=name, address=address, section=section)
    table._by_name[name] = sym
    table._at_address.setdefault(address, []).append(sym)
    if section not in table.sections:
        table.sections.append(section)


def _load_t900ld(table: SymbolTable, lines: List[str]) -> int:
    current_section = "unspecified"
    found = 0
    for line in lines:
        stripped = line.lstrip()
        if not stripped or stripped.startswith("#"):
            continue
        mh = _SECTION_HEADER_RE.match(stripped)
        if mh:
            current_section = mh.group(1)
            if current_section not in table.sections:
                table.sections.append(current_section)
            continue
        ms = _SYMBOL_LINE_RE.match(line)
        if ms:
            _add(table, ms.group(1), int(ms.group(2), 16), current_section)
            found += 1
    return found


def _load_tulink(table: SymbolTable, lines: List[str]) -> int:
    """Parse the Toshiba tulink 'Symbol table for ...' block.

    Only the symbol table is read; the memory-layout tables above it use the same
    shape (name, hex, section) and would otherwise inject section rows as symbols.
    """
    found = 0
    in_table = False
    pending_name: Optional[str] = None
    for line in lines:
        if not in_table:
            if _TU_TABLE_START_RE.match(line):
                in_table = True
            continue
        if line.strip().startswith(("Symbol ", "-----")):
            continue
        mm = _TU_MODULE_RE.match(line)
        if mm:
            pending_name = None      # a module header ends any wrapped name
            continue
        if pending_name is not None:
            ma = _TU_ADDR_ONLY_RE.match(line)
            if ma:
                _add(table, pending_name, int(ma.group(1), 16), ma.group(2))
                found += 1
                pending_name = None
                continue
            pending_name = None      # not a continuation after all; fall through
        ms = _TU_SYMBOL_RE.match(line)
        if ms:
            _add(table, ms.group(1), int(ms.group(2), 16), ms.group(3))
            found += 1
            continue
        mn = _TU_NAME_ONLY_RE.match(line)
        if mn:
            pending_name = mn.group(1)   # address is on the next line
    return found


def load_map(path: str) -> SymbolTable:
    """Parse a .map file and return a SymbolTable.

    Handles BOTH map formats this project deals with: the clean-room t900ld
    (`name  0xADDR` under `=== section ===` headers) and the Toshiba tulink map
    the official chain emits. The format is detected from the content rather than
    the filename, since both are called `.map`.

    Tolerant parser: unrecognized lines are ignored. The file must exist; a
    missing file raises FileNotFoundError.
    """
    p = Path(path)
    if not p.is_file():
        raise FileNotFoundError(f"map file not found: {path}")

    table = SymbolTable(source_path=str(p))
    with p.open("r", encoding="utf-8", errors="replace") as f:
        lines = [raw.rstrip("\n") for raw in f]

    # tulink first: its files are unambiguous (they announce the symbol table),
    # and its `name ADDR section` rows never match the t900ld pattern anyway.
    if any(_TU_TABLE_START_RE.match(ln) for ln in lines):
        _load_tulink(table, lines)
    else:
        _load_t900ld(table, lines)

    table._sorted_addresses = sorted(table._at_address.keys())
    return table
