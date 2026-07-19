# Engine Integration Contract v1

Purpose:
- define the first explicit contract between `NgpCraft_engine` and
  `NgpCraft_emulator`
- remove ambiguity around how the engine should launch, parameterize,
  and consume the emulator
- close the remaining M0 documentation gap without pretending that the
  full GUI/debugger integration already exists

Current source references:
- `README.md`
- `ROADMAP.md`
- `FEATURE_MATRIX.md`
- `../NgpCraft_engine/README_manual.md`
- `../NgpCraft_engine/API_REFERENCE.md`
- `../NgpCraft_engine/ui/run_dialog.py`
- `../NgpCraft_engine/ui/tabs/project_tab.py`
- `../NgpCraft_engine/core/validation_runner.py`

Current problem:
- `NgpCraft_engine` still models "run the game" as:
  - pick an external emulator binary
  - pick or auto-detect one ROM
  - launch the binary detached with the ROM path
- this is too weak for:
  - reproducible smoke runs
  - savestate/event-log based workflows
  - deep links into debug/profiler/capture actions
  - future standalone/embedded parity guarantees

This contract therefore defines what the engine may rely on, even
before every action is implemented.

## 1. Integration modes

Two integration modes exist by design:

1. `controlled-standalone`
   - `NgpCraft_engine` launches the emulator as a separate process
   - preferred first shipping mode
   - keeps crash isolation and packaging simple
   - works for both GUI actions and headless CI actions

2. `embedded`
   - `NgpCraft_engine` hosts emulator UI/widgets or talks directly to
     an in-process core API
   - explicitly out of scope for the first shipping contract

Rule:
- v1 of the integration contract standardizes `controlled-standalone`
  first
- `embedded` may arrive later, but it MUST preserve the same action
  vocabulary and artifact semantics where applicable

## 2. Non-negotiable rules

1. `NgpCraft_engine` MUST NOT depend on `run/emulator_path` pointing to
   an arbitrary third-party emulator in the normal workflow.
2. The emulator remains the reference product; the engine integration
   reuses it instead of forking behavior.
3. Every engine action that matters for automation MUST have a headless
   counterpart, even if the engine also exposes a GUI affordance.
4. ROM identity is by content hash when a stable artifact format
   (`savestate`, `event log`) is involved; path alone is not enough.
5. Saves, traces, event logs, captures, and savestates MUST be stored
   in engine-controlled paths, not hidden inside ad-hoc working dirs.
6. Diagnostics may add information, but MUST NOT silently change the
   executed result compared to standalone reference behavior.

## 3. Contract versioning

Version string:
- `ngpc-engine-bridge.v1`

Versioning rule:
- adding or removing required fields bumps the version
- changing action semantics in a non-backward-compatible way bumps the
  version
- future C++ core integration must preserve the same action contract
  before extending it

## 4. Preferred transport

The v1 transport is:
- one standalone emulator process launch
- one JSON request file passed by path
- one JSON response emitted on `stdout`
- zero requirement for a long-lived IPC session

Why:
- robust on Windows
- easy to invoke from Python, PyQt, tests, and CI
- avoids quoting/path problems for large argument sets
- decouples request schema from CLI flag churn

Canonical process shape:

```text
ngpc_emu <engine-bridge> <request.json>
```

Current prototype command:

```text
python ngpc_emu.py engine-bridge <request.json>
```

During prototype stage, direct command-line subcommands may still exist
for manual use. The bridge contract is the engine-facing stable layer.

## 5. Request envelope

The engine writes one UTF-8 JSON request:

```json
{
  "format": "ngpc-engine-bridge-request",
  "format_version": "ngpc-engine-bridge.v1",
  "action": "run" | "debug" | "profile" | "smoke-run" | "capture-eventlog" | "capture-savestate" | "render-screenshot" | "render-tile-atlas" | "check-frame-golden-all" | "check-eventlog-golden-all" | "save-frame-golden" | "save-eventlog-golden" | "delete-frame-golden" | "delete-eventlog-golden",
  "project": {
    "project_root": "C:/.../MyProject",
    "project_name": "MyProject",
    "invoker": "NgpCraft_engine",
    "invoker_version": "<string or null>"
  },
  "build": {
    "rom_path": "C:/.../bin/main.ngc",
    "rom_sha256": "<optional 64 hex or null>",
    "map_path": "C:/.../bin/main.map",
    "symbols_available": true
  },
  "runtime": {
    "start_mode": "bootstrap" | "savestate",
    "seed_from_savestate": "C:/.../state.json",
    "seed_presets": ["bios-handoff-minimal"],
    "seed_registers": {"XIZ": 0, "DMAC0": 4660},
    "seed_xsp": 27648,
    "target_pc": 2150400,
    "max_steps": 1000
  },
  "artifacts": {
    "workspace_dir": "C:/.../.ngpc_emu",
    "event_log_path": "C:/.../.ngpc_emu/last_run.eventlog.json",
    "savestate_path": "C:/.../.ngpc_emu/last_run.state.json",
    "trace_path": "C:/.../.ngpc_emu/last_run.trace.json",
    "capture_dir": "C:/.../.ngpc_emu/captures"
  },
  "ui": {
    "focus_symbol": "Player_Update",
    "focus_scene": "stage_01",
    "focus_asset_kind": "tilemap",
    "focus_asset_id": "bg_stage_01"
  },
  "note": "optional free-form operator note"
}
```

Rules:
- `format` and `format_version` are required
- `action`, `project`, `build`, `runtime`, and `artifacts` are required
- `runtime.seed_presets`, when present, must be a list of known preset names
  applied before explicit `runtime.seed_registers` / `runtime.seed_xsp`
- `runtime.seed_registers`, when present, may name:
  - architectural 32-bit registers `XWA/XBC/XDE/XHL/XIX/XIY/XIZ/XSP`
  - modeled TLCS-900/H control registers `DMAS0..3`, `DMAD0..3`, `DMAC0..3`, `DMAM0..3`, `INTNEST`
- the currently supported `runtime.seed_presets` entry is:
  - `bios-handoff-minimal` -> `XSP = 0x00006C00`, `INTNEST = 0`
- precedence rule:
  - preset defaults apply first
  - explicit `runtime.seed_registers` overrides matching preset entries
  - explicit `runtime.seed_xsp` overrides the preset-provided `XSP`
- `ui` is optional and only advisory
- unknown optional fields MUST be ignored, not rejected

## 6. Action vocabulary

### 6.1 `run`

Purpose:
- launch the current build quickly for normal user workflow

Expected behavior:
- if the emulator has a GUI/player mode, open it on the provided ROM
- if it does not yet have a GUI, this action may temporarily degrade to
  a headless proof action only during prototype stage, but the contract
  itself is still reserved for the eventual standalone player

Engine expectation:
- one-click "Run latest build" without configuring an external emulator

### 6.2 `debug`

Purpose:
- open the current build in debugger-oriented mode

Expected behavior:
- same ROM as `run`
- consume `map_path` when present
- honor `ui.focus_symbol` / `ui.focus_scene` when possible

### 6.x `build.map_path` enrichment (cross-cutting since 2026-05-19)

When the request carries `build.map_path` pointing at a t900ld map file,
the bridge loads it once and enriches the response's `result` block with
symbol-aware fields, in addition to the action's normal output:

- `result.final_symbol`: stable dict describing the symbol that owns the
  final CPU PC (`owning_symbol`, `owning_symbol_address_hex`,
  `offset_from_symbol`, `section`). Present for every action that
  produces a `final_cpu_pc`, including `capture-savestate` and the
  partial `run`/`debug`/`profile` fallback path.
- `result.event_log_profile_excerpt`: top-N (default 5) per-symbol
  bucketing of the captured event log, with the same shape as
  `eventlog profile`. Present only for actions that wrote an event log
  in this invocation (`capture-eventlog`, `smoke-run`, and any
  `run`/`debug`/`profile` invocation where `event_log_path` was set).

The enrichment is strictly additive: omitting `map_path` keeps the
response byte-identical to the pre-symbol bridge behavior. A `map_path`
that does not exist makes the bridge raise rather than emit an empty
enrichment, because a misleading "symbols silently absent" response is
worse than a clear error.

Engines that have a map file but do not want enrichment can simply omit
`build.map_path`; the `symbols_available` field is informational and
does not by itself enable enrichment.

Engine expectation:
- one-click "Debug current build"
- future deep links from project assets to emulator views

### 6.3 `profile`

Purpose:
- open or produce profiler-oriented output for the current build

Expected behavior:
- may be GUI or headless depending on maturity
- artifacts written under `artifacts.workspace_dir`

### 6.4 `smoke-run`

Purpose:
- deterministic engine-driven runtime smoke check replacing the current
  third-party emulator launch in `validation_runner.py`

Expected behavior:
- no user interaction required
- returns a machine-readable result
- may produce event log and/or savestate artifacts

### 6.5 `capture-eventlog`

Purpose:
- ask the emulator to run using the current execution subset and emit a
  stable event log at `artifacts.event_log_path`

Expected behavior:
- equivalent in spirit to:
  - `eventlog capture <rom> ...`
- the bridge action exists so the engine does not need to know the
  evolving manual CLI surface

### 6.6 `capture-savestate`

Purpose:
- ask the emulator to emit a stable savestate at
  `artifacts.savestate_path`

Expected behavior:
- equivalent in spirit to:
  - `savestate save <rom> ...`

### 6.7 `render-screenshot` (pass 28)

Purpose:
- ask the emulator to render one K2GE frame and write a binary P6
  PPM to `artifacts.screenshot_path`
- expose the same rendering pipeline as the CLI `screenshot` command
  (full M2 Phase 1.3 compose: backdrop + SCR1/SCR2 + sprites with
  PR.C 4-level + window clip OOWC + NEG invert)

Expected behavior:
- when `runtime.start_mode == "bootstrap"` the renderer consumes the
  cold-start memory image (HW-correct: `WSI=0xFF`, `REF=0xC6`)
- when `runtime.start_mode == "savestate"` the writable overlay from
  `runtime.seed_from_savestate` is layered before rendering — same
  contract as the CLI `screenshot --seed-from`
- the PPM file is written before the response is emitted
- `result.screenshot` carries dimensions, byte count, SHA-256 of
  the PPM bytes, the resolved backdrop color, the full K2GE
  control-register snapshot, and the `renderer_pass` (`"1.3"` at
  v1)
- `result.stop_reason` is the literal string `"frame-rendered"`;
  `result.executed_count` is `0` because the renderer does not
  advance any CPU step

Request requirements:
- `artifacts.screenshot_path` is **required** — the bridge raises
  `EngineBridgeError` if missing
- when `build.map_path` is set, `result.final_symbol` is still
  enriched for the resolved start-PC, same as every other action

Equivalent CLI:
- `screenshot <rom> [--seed-from STATE] --output PATH.ppm [--json]`

### 6.8 `render-tile-atlas` (pass 29)

Purpose:
- ask the emulator to render a grid of CHAR_RAM tiles and write a
  binary P6 PPM to `artifacts.tile_atlas_path`
- expose the `tiles-view` inspector through the bridge (sibling of
  `render-screenshot` for the atlas surface)

Expected behavior:
- when `runtime.start_mode == "bootstrap"` the renderer consumes the
  cold-start CHAR_RAM image (all zeros)
- when `runtime.start_mode == "savestate"` the writable overlay
  layered before rendering, matching `tiles-view --seed-from`
- the atlas PPM file is written before the response is emitted
- `result.tile_atlas` carries dimensions, byte count, SHA-256 of
  PPM bytes, tile count, first/last tile id, cols, rows, the
  `colorisation` mode (`grayscale` or `palette`), and the resolved
  palette payload when colorisation is `palette`
- `result.stop_reason` is `"atlas-rendered"`,
  `result.executed_count` is `0`

Optional atlas parameters live under `runtime.atlas`:

```json
{
  "runtime": {
    "start_mode": "bootstrap",
    "atlas": {
      "tile_range": [0, 511],
      "cols": 16,
      "palette_plane": "scr1" | "scr2" | "sprite" | null,
      "palette_index": 0..15 | null
    }
  }
}
```

Defaults: `tile_range = [0, 511]` (full CHAR_RAM), `cols = 16`,
no palette (grayscale 4-level via the canonical 0x00/0x55/0xAA/0xFF
ramp).

Validation rules:
- `tile_atlas_path` is **required** — missing it raises
  `EngineBridgeError`
- `tile_range` must be `[start, end]` ints with `0 ≤ start ≤ end ≤ 511`
- `cols` must be a positive int (default 16)
- `palette_plane` must be `"sprite" / "scr1" / "scr2"` when set
- `palette_index` must be `0..15` when set
- both `palette_plane` and `palette_index` must be set together
  (or both null for grayscale)

Equivalent CLI:
- `tiles-view <rom> [--range N..M] [--cols C] [--plane PLANE --palette N] [--seed-from STATE] --output PATH.ppm [--json]`

### 6.9 `check-frame-golden-all` (pass 30)

Purpose:
- ask the emulator to render one frame and diff it against every
  named frame golden registered for the ROM
- expose the CLI `frame golden-check-all` workflow through the
  bridge so NgpCraft_engine validation suites can run visual
  regression as one JSON request

Expected behavior:
- builds the merged memory view the same way `render-screenshot`
  does (cold-start image + optional savestate overlay when
  `runtime.start_mode == "savestate"`)
- enumerates frame goldens under
  `<rom-dir>/.ngpc_emu/goldens-frame/`
- renders the current frame **once** and diffs it against every
  stored golden — same render-once-diff-many pattern as the CLI
- per-golden status is one of `match`, `diff`, `error` (the last
  covers manifest/PPM load failures, distinct from a pixel diff)
- `runtime.stop_on_fail = true` (optional bool) short-circuits at
  the first failure; the result block sets `stopped_early: true`
- when `artifacts.screenshot_path` is set, the rendered current
  frame is written there as a triage artifact — operators can
  inspect it side-by-side with the diverging goldens
- `result.stop_reason = "frame-goldens-checked"`,
  `result.executed_count = 0`

Response status mapping:
- all goldens match (or empty registry) → `status: "ok"`,
  `error: null`
- one or more goldens diverge / error → `status: "error"` with an
  `error` block typed `frame-golden-mismatch`

Result block (`result.frame_goldens_check`):

```json
{
  "frame_goldens_check": {
    "total": 3,
    "checked": 3,
    "passed": 2,
    "failed": 1,
    "all_equal": false,
    "stopped_early": false,
    "results": [
      {"name": "alpha", "status": "match", "equal": true, "pixel_count_different": 0, "diff_ratio": 0.0, "first_diff_pixel": null},
      {"name": "beta",  "status": "diff",  "equal": false, "pixel_count_different": 1247, "diff_ratio": 0.0513, "first_diff_pixel": [40, 30]},
      {"name": "gamma", "status": "match", "equal": true, "pixel_count_different": 0, "diff_ratio": 0.0, "first_diff_pixel": null}
    ]
  }
}
```

Validation rules:
- `runtime.stop_on_fail` must be a bool when provided (raises
  `EngineBridgeError` otherwise)
- `artifacts.screenshot_path` is optional (triage write target);
  no other artifact path is required because the golden registry
  is the source of truth

Equivalent CLI:
- `frame golden-check-all <rom> [--seed-from STATE] [--stop-on-fail] [--save-current-dir DIR] [--json]`

### 6.10 `check-eventlog-golden-all` (pass 31)

Purpose:
- ask the emulator to capture one event-log run and diff it against
  every named event-log golden registered for the ROM
- expose the CLI `eventlog golden-check-all` workflow through the
  bridge — symmetric of § 6.9 for the trace regression layer
  (Niveau B of the § 9 test pyramid)

Expected behavior:
- builds the current event-log capture via the same path as
  `capture-eventlog` / `smoke-run` (uses `runtime.start_pc`,
  `runtime.target_pc`, `runtime.max_steps`, `runtime.seed_presets`,
  `runtime.seed_registers`, `runtime.seed_xsp`, `runtime.start_mode`)
- enumerates event-log goldens under
  `<rom-dir>/.ngpc_emu/goldens/`
- diffs the single captured payload against every stored golden
  using `diff_event_logs` (same render-once-diff-many pattern as
  § 6.9 for frames)
- per-golden status is `match` or `mismatch`; `first_divergence`
  carries the standard event-log diff payload (`kind: event | length
  | run_context`) for triage
- `runtime.stop_on_fail = true` (optional bool) short-circuits at
  the first mismatch; `stopped_early: true` in the result
- when `artifacts.event_log_path` is set, the captured current event
  log is written there as a triage artifact (same slot reused as
  `capture-eventlog` / `smoke-run`)
- `result.stop_reason = "eventlog-goldens-checked"`,
  `result.executed_count` reflects the current capture's step count

Response status mapping:
- all goldens match (or empty registry) → `status: "ok"`,
  `error: null`
- one or more goldens diverge → `status: "error"` with an
  `error` block typed `eventlog-golden-mismatch`

Homogeneous golden assumption:
- every stored golden must have been captured with **compatible**
  parameters (same `--count` / `--run-until` / `--seed-from`).
  Mixed configs surface as `first_divergence.kind: "run_context"`
  even when the executed events would otherwise match — same caveat
  as the CLI `eventlog golden-check-all`.

Result block (`result.eventlog_goldens_check`):

```json
{
  "eventlog_goldens_check": {
    "total": 3,
    "checked": 3,
    "passed": 2,
    "failed": 1,
    "all_equal": false,
    "stopped_early": false,
    "results": [
      {"name": "boot-baseline",  "golden_path": "…", "status": "match",    "first_divergence": null},
      {"name": "level1-frame3",  "golden_path": "…", "status": "mismatch", "first_divergence": {"kind": "event", "index": 12, "left": {...}, "right": {...}}},
      {"name": "level1-frame4",  "golden_path": "…", "status": "match",    "first_divergence": null}
    ]
  }
}
```

Validation rules:
- `runtime.stop_on_fail` must be a bool when provided (raises
  `EngineBridgeError` otherwise)
- `artifacts.event_log_path` is optional (triage write target)

Equivalent CLI:
- `eventlog golden-check-all <rom> [--count N | --run-until ADDR] [tous flags capture] [--stop-on-fail] [--save-current PATH] [--json]`

### 6.11 `save-frame-golden` (pass 32)

Purpose:
- create a new named frame golden in the per-ROM registry through
  the bridge, closing the **golden lifecycle** for visual regression
  (save + check both bridge-driveable from NgpCraft_engine)

Expected behavior:
- renders the current frame via the same path as `render-screenshot`
  (`runtime.start_mode` selects bootstrap vs. savestate overlay)
- writes the PPM and the JSON manifest under
  `<rom-dir>/.ngpc_emu/goldens-frame/<rom>.<slug>.golden.{ppm,json}`
- the saved golden is byte-identical to what `render-screenshot`
  would emit for the same memory view — `save → check` is a strict
  match by construction
- `result.stop_reason = "frame-golden-saved"`,
  `result.executed_count = 0`

Required request fields:
- `golden_name` (top-level, **required** — the human-readable name
  used to derive the on-disk slug)
- `artifacts` block can omit `screenshot_path` — the golden's PPM
  path is the registry-derived one, not a caller-specified
  artifact

Optional fields:
- `golden_label` (top-level, optional — free-form note persisted
  in the manifest)
- `runtime.start_mode` (`"bootstrap"` or `"savestate"`)
- `runtime.seed_from_savestate` (when `start_mode == "savestate"`)

Result block (`result.frame_golden_save`):

```json
{
  "frame_golden_save": {
    "name": "boot-baseline",
    "slug": "boot-baseline",
    "label": "cold-start backdrop" | null,
    "ppm_path": "<rom-dir>/.ngpc_emu/goldens-frame/rom.boot-baseline.golden.ppm",
    "manifest_path": "<rom-dir>/.ngpc_emu/goldens-frame/rom.boot-baseline.golden.json",
    "ppm_byte_count": 72975,
    "ppm_sha256": "<64 hex>",
    "width": 160, "height": 152,
    "captured_at_utc": "2026-05-20T08:15:28.908020+00:00",
    "renderer_pass": "1.3",
    "seed_from": "<path>" | null
  }
}
```

The manifest on disk carries the same fields **plus** a full K2GE
`control_snapshot` block (window / scroll_prio / scroll_offsets /
sprite_offset / twod_control / backdrop_control / mode) so a future
audit can reconstruct exactly which control-register state produced
the golden frame.

Equivalent CLI:
- `frame golden-save <rom> <name> [--seed-from STATE] [--label TEXT] [--json]`

### 6.12 `save-eventlog-golden` (pass 33)

Purpose:
- create a new named event-log golden in the per-ROM registry
  through the bridge, closing the **trace golden lifecycle** for
  Niveau B regression (symmetric of § 6.11 for visual)

Expected behavior:
- captures one current event log via the same builder as
  `capture-eventlog` / `smoke-run` /
  `check-eventlog-golden-all` (uses `runtime.start_pc`,
  `runtime.target_pc`, `runtime.max_steps`, `runtime.seed_presets`,
  `runtime.seed_registers`, `runtime.seed_xsp`, `runtime.start_mode`)
- writes the captured event-log JSON under
  `<rom-dir>/.ngpc_emu/goldens/<rom>.<slug>.eventlog.json`
- the saved golden is the same payload `check-eventlog-golden-all`
  would diff against — `save → check-all` is a bit-identical match
  by construction when capture params line up

Required request fields:
- `golden_name` (top-level, **required**)

Optional fields:
- `note` (top-level — already in the request envelope; persisted in
  the saved event-log payload, plays the same role as `golden_label`
  does for frame goldens)
- all standard capture-config fields (`runtime.start_pc`,
  `runtime.target_pc`, `runtime.max_steps`, `runtime.seed_presets`,
  etc.)

Result block (`result.eventlog_golden_save`):

```json
{
  "eventlog_golden_save": {
    "name": "boot-baseline",
    "golden_path": "<rom-dir>/.ngpc_emu/goldens/rom.boot-baseline.eventlog.json",
    "executed_count": 8,
    "final_cpu_pc": 2150400,
    "emitted_count": 8,
    "stop_reason_capture": "step-budget-exhausted",
    "note": "captured VBlank ISR" | null
  }
}
```

`stop_reason: "eventlog-golden-saved"`. Symbol enrichment carries
over (final_symbol + event_log_profile_excerpt when
`build.map_path` is set).

Equivalent CLI:
- `eventlog golden-save <rom> <name> [--count N | --run-until ADDR] [tous flags capture] [--note TEXT] [--json]`

### 6.13 `delete-frame-golden` (pass 33)

Purpose:
- prune one named frame golden from the per-ROM registry through
  the bridge

Required request fields:
- `golden_name` (top-level, **required**)

Result block (`result.frame_golden_delete`):

```json
{
  "frame_golden_delete": {
    "name": "obsolete",
    "deleted_ppm_path": "<rom-dir>/.ngpc_emu/goldens-frame/rom.obsolete.golden.ppm",
    "deleted_manifest_path": "<rom-dir>/.ngpc_emu/goldens-frame/rom.obsolete.golden.json"
  }
}
```

`stop_reason: "frame-golden-deleted"`, `executed_count: 0`. When the
golden does not exist, the action raises `EngineBridgeError` with a
typed message (`frame golden not found for '<name>'`).

Equivalent CLI:
- `frame golden-delete <rom> <name> [--json]`

### 6.14 `delete-eventlog-golden` (pass 33)

Purpose:
- prune one named event-log golden from the per-ROM registry through
  the bridge (symmetric of § 6.13 for Niveau B)

Required request fields:
- `golden_name` (top-level, **required**)

Result block (`result.eventlog_golden_delete`):

```json
{
  "eventlog_golden_delete": {
    "name": "obsolete-trace",
    "deleted_golden_path": "<rom-dir>/.ngpc_emu/goldens/rom.obsolete-trace.eventlog.json"
  }
}
```

`stop_reason: "eventlog-golden-deleted"`, `executed_count: 0`. When
the golden does not exist, the action raises `EngineBridgeError`
(`event-log golden not found for '<name>'`).

Equivalent CLI:
- `eventlog golden-delete <rom> <name> [--json]`

## 7. Response envelope

The emulator writes one UTF-8 JSON response on `stdout`:

```json
{
  "format": "ngpc-engine-bridge-response",
  "format_version": "ngpc-engine-bridge.v1",
  "action": "smoke-run",
  "status": "ok" | "error" | "partial",
  "summary": "short human-readable sentence",
  "rom": {
    "path": "C:/.../main.ngc",
    "sha256": "<64 hex or null>"
  },
  "artifacts": {
    "event_log_path": "C:/.../last_run.eventlog.json",
    "savestate_path": "C:/.../last_run.state.json",
    "trace_path": null,
    "capture_dir": "C:/.../captures",
    "screenshot_path": "C:/.../frame.ppm",
    "tile_atlas_path": "C:/.../atlas.ppm"
  },
  "result": {
    "stop_reason": "target-reached",
    "executed_count": 1000,
    "final_cpu_pc": 2150400
  },
  "error": null
}
```

Rules:
- `status=ok` means the requested action completed as contracted
- `status=partial` means the emulator delivered a bounded fallback
  result that the engine may surface, but not confuse with full support
- `status=error` means no meaningful result should be assumed

## 8. Path ownership

### 8.1 ROM selection

The engine owns ROM discovery.

Rule:
- `NgpCraft_engine` passes the exact ROM path it wants opened
- the emulator MUST NOT silently pick a different "newest ROM"
  when the request already names one

Reason:
- the engine already knows which build/export action just ran
- silent re-discovery would create hard-to-debug mismatches

### 8.2 Workspace / artifacts

The engine owns artifact destinations.

Rule:
- the engine passes explicit paths/directories for generated artifacts
- the emulator writes there or returns an error
- the emulator MUST NOT bury engine-launched outputs in opaque temp
  directories when a stable artifact path was requested

Recommended per-project workspace:
- `<project_root>/.ngpc_emu/`

Typical contents:
- `last_run.eventlog.json`
- `last_run.state.json`
- `captures/`
- future profiler outputs

### 8.3 Saves

Persistent in-game saves MUST remain stable across rebuilds.

Rule:
- engine-launched runs for one project should resolve to one stable save
  root derived from project identity, not one save location per transient
  build folder
- rebuilding `bin/main.ngc` MUST NOT silently orphan the project's save

Recommended policy:
- derive save root from `project_root` (or future stable project UUID),
  not from the ROM filename alone

## 9. Transition from current engine behavior

Current engine state:
- `ui/run_dialog.py` stores:
  - `run/emulator_path`
  - `run/rom_path`
- `validation_runner.py` tries to locate one external emulator through:
  - the `NGPNG_SMOKE_EMULATOR` environment variable
  - failing that, a hard-coded list of well-known emulator binary names
    probed on `PATH`

v1 transition rule:

1. Normal workflow
   - stop requiring the user to choose a third-party emulator path
   - prefer a discovered/bundled `NgpCraft_emulator` bridge target

2. Compatibility window
   - keep legacy external-emulator support only as an explicit fallback
   - label it as fallback, not reference integration

3. Validation suite
   - migrate `--validation-run --smoke-run` toward bridge action
     `smoke-run`
   - stop modeling "runtime smoke" as "did some external emulator
     binary launch for 2 seconds"

## 10. Scope honesty for the current emulator prototype

At the time this contract is written, the emulator prototype already
has:
- headless CLI
- savestate v1
- event log v1
- bounded real execution trace
- one first `engine-bridge <request.json>` entry point
- Windows-friendly request loading (`utf-8-sig` accepted for BOM-bearing
  JSON files)

It does NOT yet have:
- final standalone GUI player/debugger
- symbol-aware debugger integration
- capture/profiler GUI

Therefore:
- this contract formalizes the target handshake now
- the first bridge entry point is now real, but only the headless
  actions are fully useful
- the engine MUST NOT claim full run/debug parity before those actions
  are actually wired

## 11. Acceptance for M0 closure

The integration-contract gate is considered closed when:
- this document exists
- it names one preferred first shipping mode
- it defines:
  - action vocabulary
  - request/response ownership
  - artifact path ownership
  - save-root policy
  - migration away from `run/emulator_path`

The gate does NOT require the full engine-side wiring yet.
That wiring belongs to later milestones (`M6` in `ROADMAP.md`).

## 12. Next follow-ups

1. Patch `NgpCraft_engine` run flow to prefer the bridge over
   `run/emulator_path`
2. Patch `validation_runner.py` smoke-run to use bridge action
   `smoke-run`
3. Add engine-side validation cases proving:
   - direct launch of latest build
   - event log capture into engine workspace
   - save-path stability across rebuilds
4. Replace the current `partial` fallback for bridge actions
   `run` / `debug` / `profile` with real standalone GUI/debugger wiring
