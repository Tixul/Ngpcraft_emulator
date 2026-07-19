# Frame Diff + Named Frame Goldens v1 (pass 25)

Purpose:
- ship a byte-exact frame regression primitive (`frame diff`) on top of
  the binary P6 PPM format produced by the M2 Phase 1 renderer
- expose a named-frame-golden registry (`frame golden-save/list/check/delete`)
  parallel to the existing `eventlog golden-*` workflow, closing the
  "Niveau C — golden frames" gate from `ROADMAP.md` § 9
- give the operator and CI a way to lock visual regressions as the
  renderer evolves through M3 (raster IRQ, mid-frame swaps) and beyond

Sister specs:
- `RENDERER.md` — produces the PPMs this spec consumes
- `TILE_ATLAS.md` — alternate PPM source (atlas, not screen compose)
- `../core/goldens.py` — sibling event-log golden registry (manifest pattern)

## 1. Scope of v1

- **Byte-exact diff only.** Two PPMs match iff every RGB triplet is
  byte-identical. No perceptual / fuzzy / threshold modes.
- **PPM source-agnostic.** `frame diff` accepts any pair of valid P6
  PPMs at maxval 255 (the canonical encoding produced by
  `pixels_to_ppm_bytes`). It doesn't care whether they came from
  `screenshot`, `tiles-view`, or any external tool.
- **Per-ROM, per-name registry.** Each golden is stored under
  `<rom-dir>/.ngpc_emu/goldens-frame/<rom>.<slug>.golden.{ppm,json}`,
  mirroring the per-ROM layout that `eventlog golden-*` uses.
- **No mid-frame timing / raster IRQ.** Goldens capture a single
  snapshot via the same merged cold-start + savestate memory view as
  `screenshot`. Mid-frame swaps land in M3.

## 2. Data model

### 2.1 `FrameDiffResult`

`core/frame_diff.py`:

```python
@dataclass(frozen=True)
class FrameDiffResult:
    equal: bool
    width: int
    height: int
    total_pixels: int
    pixel_count_different: int
    diff_ratio: float                           # 0.0..1.0
    first_diff_pixel: tuple[int, int] | None    # (x, y) row-major scan
```

`pixel_count_different` counts RGB triplets that differ, not individual
bytes. `first_diff_pixel` is the leftmost-topmost mismatch position in
row-major scan order, or `None` when frames match.

### 2.2 `NamedFrameGolden`

`core/frame_goldens.py`:

```python
@dataclass(frozen=True)
class NamedFrameGolden:
    name: str
    slug: str
    ppm_path: Path
    manifest_path: Path
    manifest: dict[str, object]
```

The manifest is a JSON dict with these fields (`v1`):

| Field               | Type        | Notes                                    |
|---------------------|-------------|------------------------------------------|
| `format_version`    | int         | `FRAME_GOLDEN_FORMAT_VERSION` = 1        |
| `name`              | str         | The human-readable name passed at save   |
| `slug`              | str         | Filesystem-safe slug derived from `name` |
| `label`             | str \| null | Optional free-form note                  |
| `rom_path`          | str         | ROM path at capture time                 |
| `rom_sha256`        | str         | Hex digest of ROM bytes                  |
| `ppm_path`          | str         | Path to the stored PPM artifact          |
| `ppm_byte_count`    | int         | `len(ppm_bytes)`                         |
| `ppm_sha256`        | str         | Hex digest of PPM bytes                  |
| `width` / `height`  | int         | Dimensions of the stored frame           |
| `captured_at_utc`   | str (ISO)   | Capture timestamp                        |
| `seed_from`         | str \| null | `--seed-from` arg, when present          |
| `renderer_pass`     | str         | e.g. `"1.3"` — bumps with renderer       |
| `control_snapshot`  | dict        | `K2geControlRegisters` payload at capture |

The manifest is the source of truth for golden identity; the PPM file
is the artifact. A downstream integrity check can flag "manifest says
ppm_sha256 = X but the file on disk hashes to Y".

## 3. PPM parser

`core/frame_diff.py::parse_ppm_p6(data) -> (width, height, body_bytes)`
hand-rolls the parser to keep the renderer stack zero-deps. Behavior:

- Magic `P6` followed by any whitespace.
- Header tokens are width, height, maxval — separated by any whitespace.
- Comments (`#` to EOL) accepted between tokens.
- Maxval must be `255` (the canonical 8-bit encoding).
- Body length must be exactly `width × height × 3` bytes; any other
  size raises `ValueError`.

This contract intentionally matches what `pixels_to_ppm_bytes` produces
plus the small dialect headers the user might write by hand or pipe
from `convert`/`ffmpeg`/`gimp`.

## 4. CLI

`frame` is a top-level subcommand group with the following verbs.

### 4.1 `frame diff <ppm_a> <ppm_b> [--json]`

Byte-compare two PPM files. Exit code:
- `0` — frames are byte-identical
- `1` — one or more pixels differ, or either file is unreadable /
  malformed

JSON payload:

```json
{
  "ppm_a": "…", "ppm_b": "…",
  "width": 160, "height": 152,
  "total_pixels": 24320,
  "pixel_count_different": 0,
  "diff_ratio": 0.0,
  "first_diff_pixel": null,
  "equal": true
}
```

### 4.2 `frame golden-save <rom> <name> [--seed-from] [--label] [--json]`

Render the current frame (same path as `screenshot`) and store it as
a named golden under `.ngpc_emu/goldens-frame/`. The PPM artifact and
JSON manifest land next to each other.

Human output:

```
Saved frame golden 'boot' for /path/to/rom.ngc
  PPM:      …/.ngpc_emu/goldens-frame/rom.boot.golden.ppm  (72975 bytes)
  Manifest: …/.ngpc_emu/goldens-frame/rom.boot.golden.json
  Label: cold-start backdrop
```

### 4.3 `frame golden-check <rom> <name> [--seed-from] [--save-current PATH] [--json]`

Re-render the current frame, byte-compare against the stored golden,
exit `0` (match) or `1` (diff). `--save-current PATH.ppm` writes the
current frame for manual triage of a diff.

The JSON payload includes the same diff counters as `frame diff`,
plus `golden_ppm_path`, `current_ppm_path`, `seed_from`. Workflow:

```
$ frame golden-save rom.ngc boot
$ # … toolchain change …
$ frame golden-check rom.ngc boot --save-current /tmp/now.ppm
DIFF: 1247/24320 pixels (5.13%); first diff at (40, 30)
$ frame diff …/.ngpc_emu/goldens-frame/rom.boot.golden.ppm /tmp/now.ppm
```

### 4.4 `frame golden-check-all <rom> [--seed-from] [--stop-on-fail] [--save-current-dir DIR] [--json]`

Render the current frame **once** and compare it to every golden in
the registry. Exits `0` only when every golden matches; `1` if any
diff or load error occurred. Designed for CI single-command visual
regression.

`--stop-on-fail` short-circuits the iteration at the first diff or
error — useful when you only need a yes/no signal and want to bound
runtime over a large golden set.

`--save-current-dir DIR` writes the rendered PPM as
`<rom-stem>.current.ppm` under DIR so a failing run leaves a triage
artifact next to the goldens.

Human output:

```
ROM: …
Frame goldens: 3/3 checked, 2 passed, 1 failed
  [OK]   alpha
  [DIFF] beta  1247 px (5.13%); first @ [40, 30]
  [OK]   gamma
```

JSON shape:

```json
{
  "rom": "…",
  "seed_from": "…" | null,
  "total": 3,
  "checked": 3,
  "passed": 2,
  "failed": 1,
  "stopped_early": false,
  "all_equal": false,
  "save_current_dir": null,
  "results": [
    {"name": "alpha", "status": "match", "equal": true, ...},
    {"name": "beta",  "status": "diff",  "equal": false, "pixel_count_different": 1247, ...},
    {"name": "gamma", "status": "match", "equal": true, ...}
  ]
}
```

Status values: `match`, `diff`, `error` (the last covers manifest /
PPM read failures or dimension mismatches).

### 4.5 `frame golden-list <rom> [--json]`

Human output:

```
Frame goldens for /path/to/rom.ngc (2):
  boot                     160×152  72975 B  2026-05-20T08:15:28+00:00 — cold-start
  level1-vblank            160×152  72975 B  2026-05-20T09:02:11+00:00
```

JSON form returns `goldens: [...]` with name, dimensions, byte count,
sha256, capture timestamp, and label per entry.

### 4.6 `frame golden-delete <rom> <name> [--json]`

Removes both the manifest and the PPM. Exits `1` if the golden
doesn't exist.

## 5. Storage layout

```
<rom-dir>/.ngpc_emu/
  └── goldens-frame/
        ├── <rom>.<slug>.golden.ppm    binary P6 PPM bytes
        ├── <rom>.<slug>.golden.json   manifest
        ├── <rom>.<other>.golden.ppm
        └── <rom>.<other>.golden.json
```

Slug derivation uses `sanitize_checkpoint_name` (shared with the
checkpoint / session / event-log golden layers) so names stay
filesystem-safe across Windows / macOS / Linux.

## 6. Use cases

- **CI single-command regression**: lock N frames as goldens, run
  `frame golden-check-all <rom>` from a single CI step; non-zero
  exit fails the build, JSON output captures per-golden details for
  the report. `--stop-on-fail` keeps the runtime bounded when only
  the yes/no signal matters.
- **Renderer regression**: lock the StarGunner cold-start frame as a
  golden, run `frame golden-check` in CI to catch any compose-pipeline
  change that flips pixels.
- **Toolchain regression**: lock a frame after a known-good build,
  run `frame golden-check` after a toolchain change to verify the
  visible output hasn't shifted.
- **Cross-pass golden migration**: when a renderer pass bumps (e.g.
  pass 23 → pass 24), re-save goldens; the manifest's `renderer_pass`
  field records which pipeline produced them.
- **External diff**: `frame diff` against PPMs from any source (real
  hardware captures, other emulators) once aligned to 160×152.

## 7. Not modeled

- Perceptual / fuzzy diff modes (threshold, color distance, dither
  noise). Could land as `--tolerance` or `--mode fuzzy` later.
- Diff image output (`--out-diff PATH.ppm` that paints differences in
  red). Trivial extension when needed.
- Cross-ROM goldens (one golden shared by multiple ROMs). Out of
  scope; the registry is per-ROM by design.
- Frame-by-frame capture (would need M3 timing model). The current
  `screenshot` / `golden-save` produces one static frame from a
  memory snapshot.
