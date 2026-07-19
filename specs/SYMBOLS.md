# Symbol map loader v1

First symbol-aware layer for the NgpCraft Emulator. Loads a t900ld `.map`
file into an in-memory table and exposes lookups by name and by address.

## Input format

The loader accepts the t900ld map file shape:

```
# t900ld.py map file
# inputs: ...

=== Linker symbols ===
  _Bss_END                 0x00005572
  _StackTop                0x00006000

=== Public symbols ===
  _main                    0x00200200
  _shmup_update            0x00219A2D
```

Rules:

- Comment lines (`#`) are skipped.
- Each `=== NAME ===` line opens a logical section. Sections are stored in
  the order they appear; symbols recall the section header under which
  they were listed.
- A symbol line is `NAME 0xADDR` (any whitespace, trailing whitespace OK).
- Lines that do not match the symbol pattern are silently ignored.
- Missing file → `FileNotFoundError`.

The map file is the **authoritative source**. The loader never fabricates
symbols that are not in the file.

## API (`core/symbols.py`)

```python
table = load_map("path/to/file.map")
len(table)                          # total symbol count
table.sections                      # ordered section names
table.section_summary()             # [(section_name, count), ...]
table.lookup_name("_shmup_update")  # Symbol | None
table.lookup_address(0x219A2D)      # Symbol | None — nearest with addr <= PC
table.symbols_at_address(0x219A2D)  # all symbols at exactly this address
table.symbols_in_range(lo, hi)      # sorted list
```

`Symbol(name, address, section)` is the returned record.

### Reverse lookup contract

`lookup_address(pc)` returns the symbol whose address is the largest value
that is still `<= pc`. This matches how a debugger names a PC: the
function or label most recently entered.

- Returns `None` only when the PC is below every known symbol.
- Two symbols at the same address: the first one parsed is returned (use
  `symbols_at_address` to see all of them).

## CLI

```
python ngpc_emu.py map info  <file.map>           [--json]
python ngpc_emu.py map lookup-name  <file.map> <name>   [--json]
python ngpc_emu.py map lookup-addr  <file.map> <pc>     [--json]
```

`<pc>` accepts decimal or `0x`-prefixed hex.

`lookup-addr` returns symbol name, the offset from its base, and the
section. Returned JSON includes a `note` field clarifying the
"nearest <=" semantic for downstream tools (engine-bridge, profilers).

## Use cases unlocked

- **Final-PC symbol resolution wired into 4 execution commands** since
  2026-05-19 evening: `run-until-exec`, `step-exec`, `trace-exec`,
  `eventlog capture` all accept `--map <file>` and gain a `final_symbol`
  block in their JSON output (+ a one-line human-readable summary). The
  on-disk event-log JSON file itself is not modified — symbol awareness
  is a CLI diagnostic on top of the v1 format.
- Reporting the symbol around an honest stop frontier (e.g. silicon-broken
  opcode inside `_ngpc_mul32`).
- **Per-symbol profile of captured event logs** since 2026-05-19 evening
  (same session as the loader): `python ngpc_emu.py eventlog profile
  <log> --map <map>` buckets every event by owning symbol, separating
  executed from halted instructions, with a per-status breakdown and
  the min/max offset observed inside each function. This is the dynamic
  profile primitive that closes the symbol-aware diagnostic loop.

## Out of scope (v1)

- Section ranges (start/end of `.text`, `.bss`, etc.) — only individual
  symbols are loaded.
- Line-number debug info — none in t900ld map files.
- Caller/callee graph — not derivable from a single map file.
- DWARF or any other rich debug format.

These remain available later if needed; v1 is intentionally read-only
and minimal so it can plug into existing CLI without disturbing the
execution path.
