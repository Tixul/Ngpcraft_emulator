# Event Log v2

Purpose:
- define the first stable, versioned on-disk format for a time-ordered
  stream of machine events emitted during one execution run
- close the M0 gate item `event log v1` that the ROADMAP flags as
  `a faire`
- keep the existing `trace-exec` command as the "latest / evolving"
  shape and let the event log carry the locked diff-friendly format
- make the format honest about what the current emulator actually
  observes; do not define event types for subsystems that are not yet
  modeled

Current source references:
- `SAVESTATE.md` (sister format; same envelope rules)
- `TRACE_EXEC.md` (evolving trace shape that this format supersedes
  for diff / CI / regression use)
- `TRACE.md` (decode-only preview — NOT a runtime event source)
- `EXECUTE.md` (source of the per-step execution payload the v1 event
  shape wraps)
- `HARDWARE_COMPAT_POLICY.md` (§3.2 diagnostics must not silently
  change execution)

Current scope:
- v1 is an execution event log: one event per attempted instruction
- v1 is designed for diff, CI regression, bisect and reproducible
  bug reports
- v1 is NOT a cycle trace (no cycle counts, no scanline positions)
- v1 is NOT a video/audio/DMA/IRQ event stream (those subsystems
  are not modeled yet — defining events for them would be forging
  content)
- v1 coexists with `trace-exec`; the CLI can emit either shape

## 1. Format envelope

Event log files are UTF-8 JSON with the following root object:

```
{
  "format": "ngpc-emu-event-log",
  "format_version": "2026-05-20.v2",
  "created_at_utc": "<ISO-8601 timestamp>",
  "emulator": {
    "project": "NgpCraft_emulator",
    "prototype": "python",
    "commit": "<short SHA when known, null otherwise>"
  },
  "rom": { ... },
  "quirks": { ... },
  "run_context": { ... },
  "events": [ ... ],
  "summary": { ... },
  "note": "<free-form operator note, optional>"
}
```

Mandatory top-level fields: `format`, `format_version`, `rom`,
`run_context`, `events`, `summary`.

## 2. ROM identity

Same contract as `SAVESTATE.md §2`:

```
"rom": {
  "path_when_saved": "<absolute path, informational only>",
  "file_size": <int>,
  "sha256": "<64-char hex digest of the ROM file bytes>",
  "header_title": "<title string>",
  "header_entry_point": <int>,
  "header_mode_raw": <int>
}
```

A loader MUST refuse any mismatching ROM when an ROM is provided for
verification. This mirrors the savestate rule and keeps the two
formats consistent.

## 3. Quirk database pinning

```
"quirks": {
  "database_version": "2026-04-22.v3"
}
```

- The quirk database version active at log time is pinned so event
  log diffs stay meaningful when quirk rules evolve.
- Per-event `matched_quirk` payloads (see §5) also carry their own
  `database_version` to detect any drift inside one log.

## 4. Run context

```
"run_context": {
  "start_pc": <int>,
  "start_pc_hex": "0x<8 hex>",
  "target_pc": <int or null>,
  "target_pc_hex": "0x<8 hex>" or null,
  "max_steps": <int or null>,
  "seed_registers": {"XWA": <int>, "DMAC0": <int>, ...} or null,
  "seed_xsp": <int or null>,
  "seed_from_savestate": null | {
    "format_version": "2026-04-22.v1",
    "rom_sha256": "<64 hex>",
    "cpu_pc": <int>
  }
}
```

- `start_pc`: actual PC the run started from (after any seed).
- `target_pc`: when the run was driven by `run-until-exec`; `null`
  for open-ended `trace-exec`-style runs.
- `seed_from_savestate`: set when `--seed-from <state.json>` fed the
  run; records the savestate's format_version, ROM hash, and CPU PC
  at load time. This is the reproducibility anchor.
- `seed_registers` / `seed_xsp`: recorded verbatim when the run was
  parameterized via `--seed-reg` / `--seed-xsp` (including modeled
  TLCS-900/H control-register seeds such as `DMAC0` / `INTNEST`).
- Bridge note:
  - engine-bridge `runtime.seed_presets` are flattened into the same
    effective `seed_registers` / `seed_xsp` fields instead of adding a
    separate event-log schema key
  - example: `bios-handoff-minimal` contributes `INTNEST=0` under
    `seed_registers`, while an explicit bridge `seed_xsp` override still
    appears in `seed_xsp`

## 5. Events

```
"events": [
  {
    "index": <int>,
    "event_type": "instruction-step",
    "pc": <int>,
    "pc_hex": "0x<8 hex>",
    "raw_bytes_hex": "XX XX XX",
    "assembly": "<mnemonic operands>" or null,
    "length": <int or null>,
    "status": "executed" | "cpu-halted" | "silicon-broken" | "requires-known-flags"
              | "runtime-memory-unavailable" | ... ,
    "next_pc": <int or null>,
    "next_pc_hex": "0x<8 hex>" or null,
    "written_registers": ["WA", "PC", ...],
    "memory_writes": [
      {
        "address": <int>,
        "address_hex": "0x<6 hex>",
        "size": <int>,
        "data_hex": "XX XX",
        "note": "<why>"
      }
    ],
    "memory_reads": [
      {
        "address": <int>,
        "address_hex": "0x<6 hex>",
        "size": <int>,
        "data_hex": "XX XX",
        "note": "<why>"
      }
    ],
    "flag_changes": [
      {"name": "Z", "before": true, "after": false}
    ],
    "matched_quirk": null | {
      "database_version": "<version>",
      "quirk_id": "<id>",
      "category": "<cat>",
      "confidence": "<level>",
      "summary": "<one-line>",
      "note": "<full note>",
      "sources": [
        {"document": "<path>", "section": "<id>" or null, "quote": "<text>" or null}
      ]
    },
    "note": "<execution note from executor>"
  }
]
```

- `cpu-halted` means the `HALT` instruction itself completed and produced
  a post-step CPU state, but the bounded run stopped immediately because
  resume requires a future interrupt.
- For statuses with a real post-step state such as `cpu-halted`,
  `next_pc` can be non-null even though the run summary stop reason is
  `stopped-on-...`.

Rules:
- Every attempted instruction produces exactly one event. There are
  no implicit gaps.
- The `status` field mirrors `ExecutionResult.status` 1-for-1.
- When `status != "executed"`, the event still carries the decoded
  instruction metadata; `written_registers`, `memory_writes`,
  `memory_reads` and `flag_changes` are empty lists, not `null`.
- `matched_quirk.sources` is the same attribution payload defined in
  `QUIRKS.md`; loaders MAY rely on it being non-empty when a match
  is present.
- `memory_reads` (added in v2) is the list of contiguous byte ranges
  the executor read from the writable overlay or the cold-start image
  to perform this step. Only executors that opted in surface this
  field; the rest emit an empty list. POP SR is the v2 reference
  case: it reports its 2-byte SR load at XSP.

## 6. v1 event vocabulary

v1 defines exactly one event type: `instruction-step`.

Future event types (deliberately out of scope until the corresponding
subsystem becomes a first-class citizen):
- `vblank-entered` / `vblank-exited`
- `hblank-started`
- `irq-dispatched`
- `dma-started` / `dma-completed`
- `timer-expired`
- `audio-tick` (probably too fine-grained to surface as-is)

Adding a future event type MUST bump `format_version` because the
event stream becomes a strict superset of v1 consumers can no longer
round-trip.

## 7. Summary

```
"summary": {
  "executed_count": <int>,
  "emitted_count": <int>,
  "stop_reason": "target-reached" | "step-budget-exhausted"
                 | "stopped-on-silicon-broken" | "stopped-on-<status>",
  "final_cpu_pc": <int>,
  "final_cpu_pc_hex": "0x<8 hex>",
  "matched_quirk_on_stop": null | { <same shape as §5.matched_quirk> }
}
```

- `emitted_count` is the number of events in `events`. It equals
  `executed_count + 1` when the run ended on a blocked step (that
  blocked step is recorded too), or `executed_count` when the run
  ended on `target-reached` or `step-budget-exhausted`.
- `matched_quirk_on_stop` is the per-step match from the last event
  when it caused the stop; otherwise `null`.

## 8. Versioning rule

- First shipped version: `2026-04-22.v1`.
- Current version: `2026-05-20.v2` — adds `events[].memory_reads`.
- Schema changes bump the `format_version` string.
- Loaders MUST reject `format != "ngpc-emu-event-log"`.
- Loaders MUST reject unknown `format_version`; no implicit upgrade
  paths for now.
- When the C++ core rewrite happens, the format is copied
  byte-for-byte before any field is added, removed or renamed.

## 9. Current CLI surface

- `python ngpc_emu.py eventlog capture <rom> <output.json> [--run-until <target_pc>] [--seed-reg ...] [--seed-from <state.json>]`
  - runs the current executor and writes a v1 event log.
  - when `--run-until` is omitted, capture is bounded by `--count`
    and stops with `step-budget-exhausted` if the budget is consumed
  - when `--run-until` is present, capture is bounded by
    `--max-steps`
- `python ngpc_emu.py eventlog inspect <input.json> [--rom <rom>] [--limit <N>]`
  - loads and prints summary + optionally the first N events
- `python ngpc_emu.py eventlog diff <a.json> <b.json>`
  - first-divergence event-level diff between two logs captured against
    the same ROM hash; a primary consumer of the format stability
    promise.

The CLI is intentionally narrow in v1:
- `capture` writes the locked format, not the older evolving
  `trace-exec` payload
- `inspect` validates format/version and can also enforce the ROM hash
- `diff` is first-divergence oriented and treats a run-context mismatch
  as an honest pre-event divergence

## 10. Relation to other formats

- `TRACE_EXEC.md` covers the current evolving in-memory trace shape.
  The CLI keeps `trace-exec` for ad-hoc use; the event log is the
  locked output.
- `TRACE.md` is a static decode preview and has no runtime events
  to emit.
- `SAVESTATE.md` captures a single instant. An event log captures a
  time series between two instants; a savestate taken at the end of
  a run + the event log that produced it cross-reference each other
  via ROM hash and CPU PC.
- `QUIRKS.md` defines the per-rule source attribution that
  `events[].matched_quirk.sources` uses verbatim.

## 11. Not modeled yet

- cycle counts / scanline positions
- IRQ dispatch events (IFF is tracked but interrupts are not
  delivered by the current executor)
- DMA channel progress
- VBlank / HBlank events
- timer expiry
- audio tick events
- any subsystem not yet observable in the current emulator prototype

These gaps are deliberate. Each one should graduate into the event
log vocabulary only after the corresponding subsystem becomes a
first-class citizen of the reference emulator. Adding "placeholder"
events for unmodeled subsystems would violate
`HARDWARE_COMPAT_POLICY.md §3.2` (diagnostics must not invent content).

## 12. Next extensions (likely v2+)

- video timing events once the K2GE model exists
- IRQ events once the dispatcher exists
- DMA events once channels are emulated
- cycle / clock stamping on `instruction-step` events once the cycle
  accounting model exists
- optional binary sidecar format for very long runs; text JSON stays
  the canonical interchange
