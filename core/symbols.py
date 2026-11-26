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

import json
import re
from bisect import bisect_left, bisect_right
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
    # C source lines, only when the table came from a `.ngdbg` (`ngpcraft build -g`).
    lines: Optional["SourceLines"] = None

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


# --- NgpCraft debug file (`ngpcraft build -g` -> <rom>.ngdbg) -----------------
# JSON written by the NgpCraft toolchain's linker (t900ld `_ecrire_ngdbg`):
#
#     {"format": "ngpcraft-debug", "version": 1,
#      "fichiers": ["src/main.c", ...],
#      "lignes":   [[address, file_index, c_line], ...]   # sorted by address
#      "symboles": [{"nom", "adresse", "fichier", "section", "public"}, ...]}
#
# It carries EVERY named symbol (static functions too -- the .map lists only the
# public ones) and the address -> C line table the source-level debugger runs on.

NGDBG_FORMAT = "ngpcraft-debug"


@dataclass(frozen=True)
class SourcePos:
    """Where an address sits in the C source."""
    file: str          # project-relative path, as the makefile names it
    line: int
    start: int         # first address of this line's block of code
    end: int           # first address past it


class SourceLines:
    """Address <-> C line table.

    An address belongs to the last entry at or below it, up to the next entry --
    and never past the next SYMBOL either: code with no line info (hand-written
    asm, a library) that follows a C function must read as "unknown", not as the
    last line of that function.
    """

    def __init__(self, files: List[str], entries: List[Tuple[int, int, int]],
                 symbol_addresses: List[int]) -> None:
        self.files = list(files)
        ent = sorted((int(a), int(f), int(n)) for a, f, n in entries
                     if 0 <= int(f) < len(files))
        self._addr = [e[0] for e in ent]
        self._file = [e[1] for e in ent]
        self._line = [e[2] for e in ent]
        self._syms = sorted(set(int(a) for a in symbol_addresses))
        self._roots: Dict[str, Optional[Path]] = {}

    def __len__(self) -> int:
        return len(self._addr)

    def _end_of(self, i: int) -> int:
        end = self._addr[i + 1] if i + 1 < len(self._addr) else self._addr[i] + DEFAULT_MAX_SPAN
        k = bisect_right(self._syms, self._addr[i])
        if k < len(self._syms) and self._syms[k] < end:
            end = self._syms[k]
        return end

    def at(self, address: int) -> Optional[SourcePos]:
        """The C line `address` belongs to, or None (no line info there)."""
        i = bisect_right(self._addr, address) - 1
        if i < 0:
            return None
        end = self._end_of(i)
        if address >= end:
            return None
        return SourcePos(self.files[self._file[i]], self._line[i], self._addr[i], end)

    def is_line_start(self, address: int) -> bool:
        i = bisect_left(self._addr, address)
        return i < len(self._addr) and self._addr[i] == address

    def lines_in(self, file: str) -> Dict[int, List[int]]:
        """line -> [start addresses] for one file (a line may own several blocks:
        a `for` header, a loop body unrolled, an inlined copy...)."""
        if file not in self.files:
            return {}
        fi = self.files.index(file)
        out: Dict[int, List[int]] = {}
        for a, f, n in zip(self._addr, self._file, self._line):
            if f == fi:
                out.setdefault(n, []).append(a)
        return out

    def addresses_for(self, file: str, line: int) -> Tuple[int, List[int]]:
        """Addresses to break on for `file:line`. A line with no code of its own
        (comment, declaration, `{`) moves to the next line that has some, exactly
        as other debuggers do; returns (line actually used, addresses)."""
        table = self.lines_in(file)
        if not table:
            return (line, [])
        if line in table:
            return (line, table[line])
        after = [n for n in table if n > line]
        if not after:
            return (line, [])
        n = min(after)
        return (n, table[n])

    def resolve_file(self, file: str, near: Optional[Path]) -> Optional[Path]:
        """Find `file` (project-relative) on disk. The ROM usually sits in the
        project's `bin/`, so the source is found by walking up from it. The paths
        in the debug file are relative on purpose: no personal folder names in it."""
        p = Path(file)
        if p.is_absolute():
            return p if p.is_file() else None
        if near is None:
            return None
        key = f"{near}|{file}"
        if key in self._roots:
            return self._roots[key]
        found = None
        base = Path(near)
        for _ in range(5):
            cand = base / p
            if cand.is_file():
                found = cand
                break
            if base.parent == base:
                break
            base = base.parent
        self._roots[key] = found
        return found


def load_ngdbg(path: str) -> SymbolTable:
    """Parse a `.ngdbg` debug file: symbols (statics included) + C line table."""
    p = Path(path)
    if not p.is_file():
        raise FileNotFoundError(f"debug file not found: {path}")
    doc = json.loads(p.read_text(encoding="utf-8"))
    if doc.get("format") != NGDBG_FORMAT:
        raise ValueError(f"{p.name}: not an NgpCraft debug file")
    table = SymbolTable(source_path=str(p))
    files = [str(f) for f in doc.get("fichiers", [])]
    code_addrs = []
    for s in doc.get("symboles", []):
        name = str(s.get("nom", ""))
        if not name:
            continue
        addr = int(s.get("adresse", 0))
        section = str(s.get("section", "")) or "symbols"
        _add(table, name, addr, section)
        if "code" in section:
            code_addrs.append(addr)
    table._sorted_addresses = sorted(table._at_address.keys())
    table.lines = SourceLines(files, doc.get("lignes", []), code_addrs)
    return table


def load_symbols(path: str) -> SymbolTable:
    """`.ngdbg` or `.map`, chosen by content (both are produced next to a ROM)."""
    p = Path(path)
    if p.suffix.lower() == ".ngdbg":
        return load_ngdbg(path)
    return load_map(path)


def symbol_files_for_rom(rom: Path) -> List[Path]:
    """Candidate symbol files beside a ROM, best first: the debug file carries
    everything the map does, plus static functions and the C lines."""
    rom = Path(rom)
    out = []
    for suffix in (".ngdbg", ".map"):
        for cand in (rom.with_suffix(suffix), Path(str(rom) + suffix)):
            if cand.is_file() and cand not in out:
                out.append(cand)
    return out
