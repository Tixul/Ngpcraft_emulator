# Breakpoints v1

Purpose:
- start delivering the M4 debugger priority P0 item "breakpoints
  adresse/symbole" by giving the emulator a passive, deterministic,
  format-stable way to flag the events in a captured run where the PC
  hit a registered address
- pair cleanly with the watchpoints v3 stack: same per-ROM scoping,
  same post-run filter philosophy, same strict format-version policy
- defer live pause-on-hit to the M4 debugger; v1 deliberately does
  not change execution

Current source references:
- `WATCHPOINTS.md` (sibling format, identical envelope conventions)
- `EVENT_LOG.md` (consumer of `events[].pc`)
- `SAVESTATE.md` (sibling per-ROM persistence pattern)
- `ROADMAP.md` §M4 / §8

## 1. Scope of v1

v1 is deliberately narrow:

- **Address matches only.** A breakpoint fires when an
  `events[].pc` value equals `breakpoint.address` exactly. v1 does
  not match by symbol; the symbol → address resolution happens at
  `breakpoint add` time using the user-supplied address. Symbol-name
  shortcuts on the CLI will arrive once `.map` integration is wired.
- **Passive observer.** A breakpoint never changes execution. It does
  not pause, slow down, or alter the runtime overlay. It only filters
  events after capture. Live break-on-hit is M4 debugger territory.
- **Per-ROM registry.** Each ROM has its own breakpoint registry,
  the same way checkpoints, sessions, named goldens and watchpoints
  are scoped per-ROM.
- **Pairs with event-log v2.** Breakpoint hits are a derived view of
  an event log, not a new event type. v1 breakpoints query
  `events[].pc` from event-log v2 payloads.

## 2. Storage

Registry file: `<rom_dir>/.ngpc_emu/breakpoints/<rom_stem>.breakpoints.json`.

Schema:

```json
{
  "format": "ngpc-emu-breakpoints",
  "format_version": "2026-05-20.v1",
  "breakpoints": [
    {
      "id": 1,
      "address": 2150784,
      "address_hex": "0x0020D180",
      "label": "stargunner-frontier" | null
    }
  ]
}
```

Rules:
- `id` is a positive integer, unique within the file, assigned by
  `add_breakpoint` as `max(id) + 1`.
- `address` is a 24-bit PC value in `0..0xFFFFFF`; `address_hex` is
  informational only.
- `label` is optional free-form text.
- Duplicate addresses are allowed (each row carries its own
  independent label). Callers that want uniqueness must remove the
  existing entry first.
- Loaders reject unknown `format` and unknown `format_version`. No
  implicit upgrade path, per project policy.

## 3. Match semantics

For each event in a captured event-log payload:

- A breakpoint `(address, ...)` is hit by an event with PC `P` iff
  `P == address`.
- Hits preserve event order.
- If two breakpoints share an address (allowed by the registry), both
  fire on every matching event — useful for cross-referencing labels.

A `BreakpointHit` record carries:

- the matched `Breakpoint`
- the originating event's `index` and `pc`
- the originating event's `assembly` (informational only)
- the originating event's `status` (e.g. `"executed"`,
  `"silicon-broken"`, `"runtime-memory-unavailable"`)

## 4. CLI

### `breakpoint add <rom> <address> [--label LABEL] [--json]`

Adds one breakpoint to the registry for `<rom>` and prints the
assigned `id`. `<address>` accepts decimal or `0x`-prefixed hex.

### `breakpoint add-symbol <rom> <name> --map <file.map> [--label LABEL] [--json]`

Resolves `<name>` against a t900ld `.map` file and adds a breakpoint
at the resolved PC. When `--label` is omitted, the breakpoint label
defaults to the resolved symbol name so `breakpoint list` stays
self-describing. The registry stores only the address — rerun
`add-symbol` after a relink if the symbol moved.

### `breakpoint list <rom> [--json]`

Lists all breakpoints currently registered for `<rom>`.

### `breakpoint remove <rom> <id> [--json]`

Removes one breakpoint by id. Exit code 0 on success, non-zero if the
id is not registered.

### `breakpoint clear <rom> [--json]`

Removes all breakpoints for `<rom>` and prints how many were dropped.

### `breakpoint check <rom> <event_log.json> [--json]`

Loads the breakpoint registry for `<rom>`, loads the event-log v2
payload, runs `match_event_log_pc`, and prints the resulting hits.

Exit code:
- `0` — load succeeded (regardless of whether any hits were produced)
- `1` — registry or event-log file could not be read / parsed

The event-log loader verifies the ROM hash (same rule as
`eventlog inspect` / `eventlog diff` / `watchpoint check`).

## 5. Why a separate format from watchpoints

Watchpoints filter `events[].memory_writes` / `memory_reads`, indexed
by address range + access kind + byte value. Breakpoints filter
`events[].pc`, indexed by exact PC. The data models barely overlap,
so a unified "trigger" registry would force every row to carry
mostly-null fields that don't apply. Two narrow registries are easier
to read, easier to evolve, and let each track ship a version bump
independently.

## 6. Not modeled yet

- PC-range breakpoints (currently a breakpoint is a single address;
  ranges live in watchpoints as a different access kind)
- conditional breakpoints (`when XBC=0x1234`) — same dependency as
  the watchpoint register predicate item
- pause-on-hit / break-on-hit — v1 is a post-run filter; live
  pausing belongs to the M4 debugger
- step-out / step-over support — same dependency on live execution
