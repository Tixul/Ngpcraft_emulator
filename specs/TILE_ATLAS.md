# Tile Atlas v1 (M2 Phase 1 extension — pass 24)

Purpose:
- bridge the single-tile `tile-view` ASCII inspector (pass 19) and
  the full framebuffer compose (passes 20-23) with a "show me a grid
  of tiles" inspector
- render any sub-range of CHAR_RAM into one PPM image so the
  operator can browse the cart's tile data at a glance
- reuse the M2 Phase 0 / Phase 1 kernels (`read_tile`,
  `read_plane_palettes`, `pixels_to_ppm_bytes`) without going through
  the K2GE compose pipeline

Current source references:
- `K2GE_TILES.md` — single-tile rasterizer kernel
- `RENDERER.md` — companion PPM writer (`pixels_to_ppm_bytes`)
- `../../01_SDK/docs/NGPC_HW_QUICKREF.md` § "Character RAM"

## 1. Scope

v1 is a **pure inspector**: no scroll, no flip, no priority, no window
clip, no NEG invert. The atlas is the rendered contents of CHAR_RAM
in row-major grid order, layered over the optional savestate overlay
through the same `_build_palette_memory_view` helper every other M2
inspector uses.

The output of `render_tile_atlas` is **not** a `RenderedFrame` — it's
a bare `(width, height, pixels)` tuple suitable for
`core.renderer.pixels_to_ppm_bytes`. The atlas has no K2GE control
register snapshot to carry because no clip / priority / inversion
applies.

## 2. Memory layout

The atlas consumes the same CHAR_RAM layout as `K2GE_TILES.md`:

```
CHAR_RAM : 0xA000..0xBFFF   (8 192 bytes = 512 tiles × 16 bytes)
```

Each tile is 8 × 8 pixels at 2 bpp. The 9-bit `c_c` tile range is
`0..511` matching the sprite / tilemap entries.

## 3. Data model

`core/atlas.py` exposes:

- `GRAYSCALE_COLOR_TABLE: tuple[K2geColor, ...]` — 4-entry table
  mapping 2-bpp tile values to `K2geColor`s that nibble-replicate to
  the canonical 4-level grayscale ramp (`0x00 / 0x55 / 0xAA / 0xFF`
  in 8-bit).
- `render_tile_atlas(memory, tile_ids, cols, *, palette=None) ->
  (width, height, pixels)`

Output shape:
- `width = cols * 8`
- `height = ceil(N / cols) * 8` where `N = len(tile_ids)`
- `pixels` is a mutable `list[list[K2geColor]]` of `height` rows ×
  `width` columns

Unused grid cells (when `N % cols != 0`) stay at the grayscale "value
0" color (black). Repeated tile ids in `tile_ids` are decoded once
via an internal cache.

## 4. Color resolution

- **Grayscale (default)**: pixel value 0..3 → `GRAYSCALE_COLOR_TABLE[v]`
  (4-bit components 0/5/10/15 → 8-bit 0x00/0x55/0xAA/0xFF).
- **Palette (`--plane PLANE --palette N`)**: pixel value 0..3 →
  `palette.colors[v]`. Palette resolution uses
  `read_plane_palettes(memory, BASE, plane)` exactly like `tile-view`.

The two `--plane` and `--palette` flags must be passed together; one
alone errors with exit 1, same contract as the single-tile inspector.

## 5. CLI

### `tiles-view <rom> [--range N..M] [--cols C] [--plane sprite|scr1|scr2 --palette N] [--seed-from state.json] [--output PATH.ppm] [--json]`

Default range: `0..511` (full CHAR_RAM, 32 rows × 16 cols).
Default cols: `16`.
Default output: `./tiles.ppm` in the working directory.

Range syntax:
- `N..M` — inclusive range, decimal or `0x`-prefixed hex on either side
- `N` — single tile id

Human-readable output:

```
ROM: …
Atlas: 512 tiles, 16 cols × 32 rows = 128×256 px
Output: ./tiles.ppm  bytes=98319
Colorisation: grayscale (4-level)
```

JSON payload (`--json`):

```json
{
  "rom": "…",
  "seed_from": "…" | null,
  "tile_count": 512,
  "first_tile": 0, "last_tile": 511,
  "cols": 16, "rows": 32,
  "width": 128, "height": 256,
  "output_path": "./tiles.ppm",
  "ppm_byte_count": 98319,
  "palette_plane": "scr1" | null,
  "palette_index": 0 | null,
  "colorisation": "palette" | "grayscale",
  "palette": {...} | null
}
```

## 6. Why this is "M2 Phase 1 extension"

The atlas isn't part of the K2GE compose pipeline. It doesn't honor
scroll offsets, plane priorities, sprite layers, window clip or NEG
invert — those would change the meaning of "show me CHAR_RAM" from
"inspect raw tile data" to "preview how a game would use this data".

The atlas is the visual equivalent of `palette-info` / `oam-info` /
`tilemap-info`: a passive inspector keyed off the merged cold-start
+ savestate memory view, without executor side effects.

Concretely:
- `tile-view <id>` answers "what does tile #N look like?"
- `tiles-view --range N..M` answers "what does this swath of
  CHAR_RAM look like?"
- `screenshot` answers "what does the K2GE engine actually
  composite onto the screen?"

The three inspectors cover three different debugging questions.

## 7. PPM byte budget

| Range        | Cols | Width × Height | Body bytes | Total PPM |
|--------------|------|----------------|-----------|-----------|
| `0..511`     | 16   | 128 × 256      | 98 304    | 98 319    |
| `0..511`     | 32   | 256 × 128      | 98 304    | 98 319    |
| `0..127`     | 16   | 128 × 64       | 24 576    | 24 591    |
| `0..15`      | 4    | 32 × 32        | 3 072     | 3 087     |

Header is `P6\n<W> <H>\n255\n` (length depends on width / height
digit count — 15 bytes for the 128×256 default).

## 8. Not modeled

- per-tile labelling (tile id text rendered into the atlas) — out of
  scope for v1, would require a font rasterizer
- inter-tile gap / grid lines — current default is "no gap", a `--gap N`
  flag could land later
- highlight / filter (e.g. "only tiles referenced by the current
  tilemap") — orthogonal, could be a follow-up
