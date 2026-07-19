# Watchpoints v3

Purpose:
- close the M1d gate item "watchpoint signale une ecriture RAM ou IO"
  by giving the emulator a passive, deterministic, format-stable way
  to observe memory writes AND reads in captured runs
- prepare the M4 debugger by making "memory access at address X with
  value V" a first-class queryable event in the toolchain
- v1 (2026-05-20) was write-only; v2 (2026-05-20) adds `kind="read"`
  and `kind="access"` paired with the event-log v2 `memory_reads`
  field; v3 (2026-05-20) adds an optional `value` byte filter for
  pinpoint matching (e.g. "find the opcode that writes `0xFF` to
  `0x6000`")

Current source references:
- `EVENT_LOG.md` (consumers of `events[].memory_writes`)
- `EXECUTE.md` (where memory writes originate in the runtime overlay)
- `SAVESTATE.md` (sibling per-ROM persistence pattern)
- `ROADMAP.md` §M1d / §M4

## 1. Scope of v1

v2 scope:

- **Three access kinds.** `write` (matches `memory_writes` only),
  `read` (matches `memory_reads` only), `access` (matches both).
  A v1 registry with only `write` rows reads identically through v2.
- **Read coverage is universal (Phase 3, 2026-05-20).** Every
  executor that calls `_read_runtime_bytes` automatically surfaces
  its reads via a per-step module accumulator that
  `build_execute_next` clears and folds into `ExecutionResult.memory_reads`.
  No call-site changes were needed; the 22 read sites are uniformly
  covered. The earlier v2 reference case (POP SR) now goes through
  the same accumulator path.
- **Passive observer.** A watchpoint never changes execution. It does
  not pause, slow down, or alter the runtime overlay. It only filters
  events after capture.
- **Per-ROM registry.** Each ROM has its own watchpoint registry, the
  same way checkpoints, sessions and named goldens are scoped per-ROM.
- **Pairs with event-log v2.** Watchpoint hits are a derived view of
  an event log, not a new event type. v2 watchpoints query
  `events[].memory_writes` AND `events[].memory_reads` from event-log
  v2 payloads. Loading an event-log v1 payload against v2 watchpoints
  is rejected by the loader per `EVENT_LOG.md §8`.

## 2. Storage

Registry file: `<rom_dir>/.ngpc_emu/watchpoints/<rom_stem>.watchpoints.json`.

Schema:

```json
{
  "format": "ngpc-emu-watchpoints",
  "format_version": "2026-05-20.v3",
  "watchpoints": [
    {
      "id": 1,
      "kind": "write",
      "start": 16384,
      "start_hex": "0x004000",
      "size": 1,
      "label": "stack scratch" | null,
      "value": 255 | null,
      "value_hex": "0xFF" | null
    }
  ]
}
```

Rules:
- `id` is a positive integer, unique within the file, assigned by
  `add_watchpoint` as `max(id) + 1`.
- `kind` is one of `"write"`, `"read"`, or `"access"` in v2+.
- `value` (v3) is an optional byte filter in `0..255`. When set, the
  watchpoint fires only if the **first byte** of the accessed range
  equals `value`. When `null`, every range overlap fires (v2 default
  semantics). `value_hex` is informational only.
- `start` is the byte address; `start_hex` is informational only.
- `size` is the contiguous byte range starting at `start`. `size >= 1`.
- `label` is optional free-form text.
- Loaders reject unknown `format` and unknown `format_version`. No
  implicit upgrade path, per project policy.

## 3. Match semantics

For each event in a captured event-log payload, and each entry in
`event.memory_writes`:

- A watchpoint `(start, size)` is hit by a write `(address, write_size)`
  iff the inclusive ranges `[start, start+size-1]` and
  `[address, address+write_size-1]` overlap.
- One write may hit multiple watchpoints; each hit is emitted
  independently.
- Hits preserve event order and watchpoint declaration order.

A `WatchpointHit` record carries:

- the matched `Watchpoint`
- the originating event's `index` and `pc`
- the originating write's `address`, `size` and `data_hex`
- the originating event's `assembly` (informational only)

## 4. CLI

### `watchpoint add <rom> <address> [--kind write|read|access] [--size N] [--label LABEL] [--value BYTE] [--json]`

Adds one watchpoint to the registry for `<rom>` and prints the
assigned `id`. `<address>` and `--value` accept decimal or
`0x`-prefixed hex. `--kind` defaults to `write` for v1 compatibility.
`--value` defaults to no filter (every range overlap fires).

### `watchpoint list <rom> [--json]`

Lists all watchpoints currently registered for `<rom>`.

### `watchpoint remove <rom> <id> [--json]`

Removes one watchpoint by id. Exit code 0 on success, non-zero if the
id is not registered.

### `watchpoint clear <rom> [--json]`

Removes all watchpoints for `<rom>` and prints how many were dropped.

### `watchpoint check <rom> <event_log.json> [--json]`

Loads the watchpoint registry for `<rom>`, loads the event-log v1
payload, runs `match_event_log_writes`, and prints the resulting hits.

Exit code:
- `0` — load succeeded (regardless of whether any hits were produced)
- `1` — registry or event-log file could not be read / parsed

The event-log loader verifies the ROM hash (same rule as
`eventlog inspect` / `eventlog diff`).

## 5. Why not extend the event-log format

Per `EVENT_LOG.md` §8, adding a new event type or per-event field
bumps `format_version` and breaks loader compatibility. Watchpoint
hits are entirely derivable from the existing `events[].memory_writes`
array, so v1 stays as a query overlay rather than a stored field.
Future versions may add a cache layer (e.g. precomputed hits inside
the event-log payload) once the field set stabilises across the C++
core rewrite.

## 6. Not modeled yet

- multi-byte value filters (v3 compares only the first byte of the
  accessed range; a `ldw (addr), 0xBEEF` write of two bytes can be
  filtered against `value=0xEF` but not against `0xBEEF` directly)
- register-state predicates (e.g. "fire only when XBC=0x1234") — v3
  has no access to the CPU snapshot beyond what the event carries
- pause-on-hit / break-on-hit — v3 is still a post-run filter; live
  pausing belongs to the M4 debugger
- VRAM-aware visualisation of hit clusters — M4 / M5 territory
- instruction-fetch reads (those bytes are consumed by the decoder,
  not via `_read_runtime_bytes`, so they are deliberately excluded
  from `memory_reads`)
