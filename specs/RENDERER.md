# K2GE Renderer v1 (M2 Phase 1)

Purpose:
- compose one 160 × 152 NGPC frame from a merged cold-start +
  savestate memory view
- export the frame as a dependency-free binary P6 PPM file (the
  `screenshot` CLI is the inspector half — the kernel side is
  `core/renderer.py`)
- close the ROADMAP §8 P0 "screenshots" bullet incrementally across
  4 sub-passes

Current source references:
- `../../01_SDK/docs/NGPC_HW_QUICKREF.md` § 5 "REGISTRES VIDÉO K2GE"
- `../../01_SDK/docs/K2GETechRef.txt`
- `K2GE_PALETTE.md` (sibling — backdrop palette block, 0BGR encoding)
- `K2GE_TILES.md` (sibling — single-tile rasterizer kernel reused by
  passes 1.1 and 1.2)
- `K2GE_OAM.md`, `K2GE_TILEMAP.md` (sibling — entries consumed by
  pass 1.1 + 1.2)

## 1. Sub-pass plan

M2 Phase 1 ships in **four additive passes**. Each pass keeps the
test suite green and only extends the renderer; the public API
(`render_frame(memory) -> RenderedFrame`) stays stable.

| Pass | Scope                                                         | Status |
|------|---------------------------------------------------------------|--------|
| 1.0  | `K2geControlRegisters` decoder, backdrop fill, PPM export, CLI | done  |
| 1.1  | SCR1 + SCR2 raster, scroll offsets, H.F/V.F flip, plane prio  | done  |
| 1.2  | Sprite raster: OAM iteration, PR.C composition, H.ch/V.ch     | done  |
| 1.3  | Window clip OOWC + NEG invert (closes ROADMAP §8 P0 fully)    | done  |

Pass 1.0 is the floor. Everything that lands later overlays its
output on top of the backdrop produced here.

## 2. Frame model

NGPC screen is **160 × 152 pixels at 60 fps**. The renderer produces
one frame from a static memory snapshot — no timing model, no scanline
IRQ, no frame loop. Those land in M3.

`core/renderer.py` exposes:

- `NGPC_SCREEN_WIDTH = 160`, `NGPC_SCREEN_HEIGHT = 152`
- `RenderedFrame(width, height, pixels, control, backdrop_color)`
  - `pixels` : `tuple[tuple[K2geColor, ...], ...]` — `height` rows ×
    `width` `K2geColor` cells, row-major
  - `control` : the `K2geControlRegisters` snapshot used during
    compose (preserved for JSON diagnostics)
  - `backdrop_color` : the resolved backdrop `K2geColor` (used to
    fill every pixel in pass 1.0)
- `render_frame(memory) -> RenderedFrame` — orchestrator. Pass 1.0
  fills with `backdrop_color`. Later passes layer SCR / sprites /
  window clip / NEG on top.
- `resolve_backdrop_color(memory, control) -> K2geColor` — the BGC
  resolution helper extracted so tests can drive it independently.
- `frame_to_ppm_bytes(frame) -> bytes` — binary P6 PPM serializer.

## 3. K2GE control registers (pass 1.0 decoder)

`core/k2ge.py` ships `K2geControlRegisters` + `read_control_registers(memory)`.

Register map per `NGPC_HW_QUICKREF.md` § 5:

| Address  | Field        | Purpose                                              | Pass |
|----------|--------------|------------------------------------------------------|------|
| `0x8002` | WBA.H        | Window origin X                                      | 1.3  |
| `0x8003` | WBA.V        | Window origin Y                                      | 1.3  |
| `0x8004` | WSI.H        | Window width (cold-start 0xFF means full width)     | 1.3  |
| `0x8005` | WSI.V        | Window height (cold-start 0xFF means full height)   | 1.3  |
| `0x8012` | 2D Control   | bit 7 = NEG (invert), bits 2..0 = OOWC color index   | 1.3  |
| `0x8020` | PO.H         | Sprite global offset X                               | 1.2  |
| `0x8021` | PO.V         | Sprite global offset Y                               | 1.2  |
| `0x8030` | Scroll Prio  | bit 7: 0 = SCR1 in front, 1 = SCR2 in front          | 1.1  |
| `0x8032` | S1SO.H       | SCR1 scroll offset X                                 | 1.1  |
| `0x8033` | S1SO.V       | SCR1 scroll offset Y                                 | 1.1  |
| `0x8034` | S2SO.H       | SCR2 scroll offset X                                 | 1.1  |
| `0x8035` | S2SO.V       | SCR2 scroll offset Y                                 | 1.1  |
| `0x8118` | BGC          | bit 7 = 1 AND bit 6 = 0 enables backdrop; bits 2..0 = index 0..7 | 1.0 |
| `0x87E2` | MODE         | bit 7: 0 = K2GE color mode, 1 = K1GE compat          | (read-only diag, not modeled) |

All registers are decoded in pass 1.0 so the `RenderedFrame.control`
snapshot is complete from day one. The pass column documents which
sub-pass actually consumes each field during compose; pass 1.0 itself
only acts on the BGC register.

## 4. Scroll plane raster (pass 1.1)

After backdrop fill, `render_frame` composites SCR1 + SCR2 in the
order driven by `K2geControlRegisters.scr2_in_front` (bit 7 of
`0x8030`):

- `scr2_in_front = False` (cold-start default) → SCR2 is drawn first
  (back), SCR1 second (front).
- `scr2_in_front = True` → SCR1 back, SCR2 front.

For each screen pixel `(sx, sy)`:

1. Wrap world coordinates through the 256 × 256 plane:
   `wx = (sx + soh) & 0xFF`, `wy = (sy + sov) & 0xFF`
2. Decode tilemap entry at `(tx, ty) = (wx >> 3, wy >> 3)` from
   `read_tilemap(memory, plane)`.
3. Skip if `entry.c_c == 0` (tile-0 transparent convention).
4. Apply H.F / V.F flip to the in-tile coordinates:
   `px = (wx & 7); if h_flip: px = 7 - px` (same for `py`).
5. Read the 2-bit pixel value from `read_tile(memory, entry.c_c)`.
   Skip if `value == 0` (palette index 0 = transparent).
6. Write `palettes[entry.cp_c].colors[value]` (palette plane base =
   `K2GE_PALETTE_SCR1_BASE` or `K2GE_PALETTE_SCR2_BASE`).

Tile data is cached for the duration of one `_render_scroll_plane`
call (a tile that appears in N tilemap cells is decoded once).

## 5. Sprite raster (pass 1.2)

After backdrop fill, sprites and scroll planes are interleaved in
6 layers (back → front), all consuming the same shared `tile_cache`
so any tile referenced by both a sprite and a tilemap cell decodes
once per frame:

1. Backdrop (pass 1.0)
2. Sprites with `PR.C = 01` (behind both scroll planes)
3. Back scroll plane (pass 1.1)
4. Sprites with `PR.C = 10` (between the two scroll planes)
5. Front scroll plane (pass 1.1)
6. Sprites with `PR.C = 11` (in front of everything)

Hidden sprites (`PR.C = 00`) are never drawn but still advance the
chain state in `resolve_sprite_positions`, so a chain group whose
anchor is hidden still positions its tail correctly.

### 5.1 `resolve_sprite_positions(memory, control)`

Iterates the 64 OAM entries in order, applying chain semantics and
the global sprite offset:

- `H.ch = 1` → `effective_h = (prev_h + sprite.h_pos) & 0xFF`
- `V.ch = 1` → `effective_v = (prev_v + sprite.v_pos) & 0xFF`
- `H.ch = 0` / `V.ch = 0` → `effective_* = sprite.*_pos`
- Always: `screen_x = (effective_h + control.po_h) & 0xFF`
  (same for `screen_y` with `po_v`)

The chain state advances **per OAM entry**, regardless of `PR.C`.

The 8-bit screen coordinates are not clipped here; sprites whose
position falls outside `0..159 / 0..151` simply don't paint pixels
during the layer pass.

### 5.2 `_render_sprite_layer(framebuffer, memory, positioned, target_pr_c, palettes, tile_cache)`

For each `(sprite, screen_x, screen_y)` in `positioned` whose
`sprite.pr_c == target_pr_c`:

1. Resolve `tile = read_tile(memory, sprite.c_c).pixels` (cached).
2. For each in-tile pixel `(px, py)`:
   - Clip if `screen_x + px >= 160` or `screen_y + py >= 152`.
   - Apply `H.F` / `V.F` flip to `(px, py)`.
   - Skip if `tile[py_eff][px_eff] == 0` (palette transparency).
   - Else write `palettes[sprite.cp_c].colors[value]`.

Sprite palette plane base = `K2GE_PALETTE_SPRITE_BASE` (`0x8200`).

## 6. Window clip + NEG invert (pass 1.3)

Two final-stage passes applied to the composed framebuffer after the
6 layers of §§ 4-5 have run.

### 6.1 Window clip

Window region (half-open): `[WBA.H, WBA.H + WSI.H[ × [WBA.V, WBA.V + WSI.V[`.

Every screen pixel outside that region is replaced by the OOWC color
(`resolve_oowc_color(memory, control)` — bits 2..0 of `0x8012` indexing
the 8-entry backdrop block at `0x83E0..0x83EF`).

Cold-start HW reset values are `WBA = 0` and `WSI = 0xFF`, so the
default window is `[0, 255[` which covers the entire 160 × 152 screen
— the clip pass is a no-op on a fresh reset.

The cold-start memory image (`core/memory.py::_build_builtin_readable_bytes`)
pre-populates `WSI.H = WSI.V = 0xFF` and `REF = 0xC6` to match the HW
reset values (the rest of the K2GE register range remains 0).

### 6.2 NEG invert

When bit 7 of `0x8012` is set, every 4-bit RGB component of every
pixel is inverted (`c → c ^ 0x0F`). NEG runs **after** window clip
so the OOWC fill is also inverted — matching the order in which real
silicon delivers pixels to the LCD.

## 7. Backdrop fill (pass 1.0)

- BGC register at `0x8118`:
  - bit 7 = 1 AND bit 6 = 0 → backdrop enabled, fill from indexed
    color in the backdrop block at `0x83E0..0x83EF`
  - any other pattern → disabled, fill with `K2geColor(0, 0, 0)`
    (cold-start screen is black on real hardware)
- Backdrop block: 8 colors × 2 bytes each at `0x83E0..0x83EF`.
  Indexed by BGC bits 2..0 (0..7). Format: little-endian 12-bit 0BGR
  same as every other K2GE palette entry.
- Cold-start of BGC raw byte is `0x00` → disabled → black screen.

The pass 1.0 fill is simply:

```python
backdrop = resolve_backdrop_color(memory, control)
pixels = [[backdrop] * 160 for _ in range(152)]
```

## 8. PPM export

Binary P6 PPM, RGB888. Layout:

```
P6\n
160 152\n
255\n
<160 × 152 × 3 raw bytes, row-major, RGB triplets>
```

Total file size = 15 byte header + 72 960 byte body = 72 975 bytes
on every NGPC frame.

Each K2GE 4-bit component is expanded to 8 bits by nibble replication
(`0x5 → 0x55`, `0xF → 0xFF`), matching `K2geColor.hex_rgb24()`. This
keeps black at 0x00 and full intensity at 0xFF without floating-point
scaling.

PPM was chosen over PNG to keep the renderer **zero-dependency**.
PNG support may land as a follow-up if Pillow becomes an accepted
optional dep; the kernel-side serializer stays PPM regardless.

## 9. CLI

### `screenshot <rom> [--seed-from state.json] [--output PATH.ppm] [--json]`

Renders one frame and writes the PPM. Default output path is
`./screenshot.ppm` in the working directory.

`--seed-from <state.json>` layers a savestate v2's writable overlay
on top of the cold-start image — so the captured run's BGC, backdrop
palette, scroll offsets and (later) tilemap / OAM mutations all feed
the compose.

Human-readable output (default):

```
ROM: …
Seed-from: stargunner_at_assert_fail.state.json
Frame: 160×152  output=./screenshot.ppm  bytes=72975
Backdrop: #000000  bgc_enabled=False  bgc_index=0  raw=0x00
Scroll prio: scr2_in_front=False  s1so=(0,0)  s2so=(0,0)
Sprite offset: PO=(0,0)  Window: WBA=(0,0)  WSI=(0,0)
2D Control: NEG=False  OOWC=0   MODE: k1ge_compat=False
Renderer pass 1.3 — full K2GE color-mode compose: backdrop + SCR1/SCR2 + sprites (PR.C 4-level) + window clip + NEG invert
```

JSON shape (`--json`):

```json
{
  "rom": "…",
  "seed_from": "…" | null,
  "width": 160, "height": 152,
  "output_path": "./screenshot.ppm",
  "ppm_byte_count": 72975,
  "backdrop_color": {
    "r": 0, "g": 0, "b": 0,
    "raw": 0, "raw_hex": "0x0000",
    "hex_rgb12": "0x000", "hex_rgb24": "#000000"
  },
  "control": {
    "window":          {"wba_h": …, "wba_v": …, "wsi_h": …, "wsi_v": …},
    "twod_control":    {"neg": false, "oowc": 0},
    "sprite_offset":   {"po_h": 0, "po_v": 0},
    "scroll_prio":     {"scr2_in_front": false},
    "scroll_offsets":  {"s1so_h": 0, "s1so_v": 0, "s2so_h": 0, "s2so_v": 0},
    "backdrop_control":{"bgc_raw": 0, "bgc_raw_hex": "0x00", "bgc_enabled": false, "bgc_index": 0},
    "mode":            {"k1ge_compat": false}
  },
  "renderer_pass": "1.3",
  "renderer_note": "backdrop + SCR1/SCR2 raster + sprites with PR.C 4-level composition (00 hidden / 01 behind / 10 middle / 11 front), H.ch/V.ch chain resolution, global PO.H/V sprite offset, H.F/V.F flip, palette transparency, window clip with OOWC fill, NEG invert — ROADMAP §8 P0 'screenshots' closed"
}
```

## 10. Why this is the M2 Phase 1 floor

Pass 1.0 is deliberately tiny but it is the load-bearing piece for
everything else:

- It establishes the **frame model** (`RenderedFrame` shape,
  row-major pixel grid, immutable `K2geColor` cells) that passes 1.1,
  1.2 and 1.3 will mutate.
- It establishes the **export format** (`frame_to_ppm_bytes` — every
  subsequent pass just produces a different `pixels` array).
- It establishes the **CLI contract** (`screenshot --seed-from
  --output --json`) — the existing M2 Phase 0 inspectors and the new
  renderer share the `_build_palette_memory_view` overlay-merge path.
- It decodes **all** K2GE control registers in one go, so later
  passes can drop in their compose logic without first re-doing the
  register plumbing.

Concretely: after pass 1.0 lands, every later compose step is a pure
function over `(memory, control, framebuffer) -> framebuffer'` that
mutates the row arrays in place — no architecture changes needed.

## 11. Not modeled yet

- Mid-frame raster IRQ palette / OAM / tilemap swaps (M3 timing).
- K1GE compat mono mode (`MODE` bit 7 = 1) — decoded but not rendered.
- PNG export (optional Pillow-backed follow-up).
- Multi-tile atlas view (`--tiles N..M`) bridging `tile-view` and
  the full framebuffer — orthogonal to the renderer, possibly later.
