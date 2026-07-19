# K2GE Tilemap v1 (M2 Phase 0)

Purpose:
- decode the two K2GE scroll-plane tilemaps (`0x9000..0x97FF` for SCR1,
  `0x9800..0x9FFF` for SCR2) into a structured view without requiring
  a rendered framebuffer
- close the third (final) part of ROADMAP §8 P0 "inspecteur
  VRAM/OAM/palettes"
- pair with `palette-info` and `oam-info`: same overlay-merge helper,
  same `--seed-from` workflow

Current source references:
- `../../01_SDK/docs/NGPC_HW_QUICKREF.md` § "Scroll Plane VRAM"
- `K2GE_PALETTE.md`, `K2GE_OAM.md` (sibling specs)
- `MEMORY_READ.md` § 2 (K2GE registers cold-start image)

## 1. Scope of v1 (inspector only)

v1 is the inspector half of M2 Phase 0 tilemap work:

- **Decodes** the 32×32 grid as observed via the read bus + an
  optional savestate overlay. No rendering, no scroll plane
  composition, no scrolling offsets applied.
- **Does not** apply the K2GE scroll registers (`HW_SCRx_HORZ` /
  `HW_SCRx_VERT`) — the inspector shows the raw tilemap, not the
  pixel-positioned plane.
- **Does not** modify the executor or any runtime state. Reads only.

## 2. Memory layout

Per `NGPC_HW_QUICKREF.md`:

```
SCR1 tilemap : 0x9000..0x97FF  (2 048 bytes = 32×32 tiles × 2 bytes)
SCR2 tilemap : 0x9800..0x9FFF  (2 048 bytes = 32×32 tiles × 2 bytes)

Per tile (2 bytes):
  +0 : C.C bits[7:0]              (tile number low byte)
  +1 : [H.F][V.F][P.C][CP.C 3:0][C.C bit8]

Address of tile (x, y) = base + (y * 32 + x) * 2
```

Byte +1 fields:
- bit 7 : `H.F` (horizontal flip)
- bit 6 : `V.F` (vertical flip)
- bit 5 : `P.C` (K2GE plane-code bit)
- bits 4..1 : `CP.C` palette code (`0..15`)
- bit 0 : `C.C` bit 8 (tile number high bit → tile is 9-bit `0..511`)

Tile `0` is the NGPC transparent / unused convention: renderers do
not draw it. The `K2geTilemapEntry.is_empty()` helper returns `True`
for `c_c == 0` so callers can filter "the cells the game has not
touched yet".

## 3. Data model

`core/k2ge.py` exposes:

- `K2geTilemapEntry(plane, x, y, base_address, raw_bytes, c_c,
  h_flip, v_flip, p_c, cp_c)`
- `decode_tilemap_entry(raw, *, plane, x, y, base_address)` — direct
  decode of 2 raw bytes.
- `read_tilemap(memory, plane)` — read all 1024 entries for `plane`
  (`"scr1"` or `"scr2"`) in row-major order. Raises `ValueError`
  on an unknown plane name.
- Constants `K2GE_SCR1_TILEMAP_BASE = 0x009000`,
  `K2GE_SCR2_TILEMAP_BASE = 0x009800`,
  `TILEMAP_TILES_PER_ROW = 32`, `TILEMAP_TILES_PER_COL = 32`,
  `TILEMAP_BYTES_PER_TILE = 2`.

`K2geTilemapEntry.is_empty()` returns `c_c == 0`.

## 4. CLI

### `tilemap-info <rom> [--plane scr1|scr2] [--non-empty] [--list] [--seed-from state.json] [--json]`

Two human-readable shapes:

1. **Compact ASCII grid** (default): one 32-character line per row,
   each cell:
   - `.` for empty (tile 0)
   - `0..9` for tile 1..9
   - `a..z` for 10..35
   - `A..Z` for 36..61
   - `+` for tile 62..511
   The compression is one-way; the goal is a quick visual overview
   of "which cells are set" without scrolling 1024 lines.
2. **Per-tile list** (`--list`): one line per entry with full
   `(x, y)` coordinates, address, tile number (decimal + hex),
   palette code, and flag mnemonic `HVP`.

`--non-empty` filters out tile-0 entries from the list view; the
grid view still draws all 32×32 cells but with `.` for the empty
ones.

`--plane scr2` switches to the second plane (base `0x9800`).

`--seed-from` layers a savestate v2's writable overlay on the
cold-start image.

`--json` emits a structured payload:

```json
{
  "rom": "…",
  "seed_from": "post_init.state.json" | null,
  "plane": "scr1",
  "base_address_hex": "0x009000",
  "grid_size": [32, 32],
  "total_entries": 1024,
  "non_empty_only": false,
  "shown_count": 1024,
  "non_empty_count": 0,
  "entries": [
    {
      "plane": "scr1",
      "x": 0, "y": 0,
      "base_address_hex": "0x009000",
      "raw_bytes_hex": "00 00",
      "tile": 0, "tile_hex": "0x000",
      "h_flip": false, "v_flip": false, "p_c": false,
      "cp_c": 0,
      "empty": true
    },
    ...
  ]
}
```

## 5. Not modeled yet

- scroll registers (`HW_SCRx_HORZ` / `HW_SCRx_VERT`) — the inspector
  shows the raw tilemap, not the pixel-positioned plane.
- per-pixel rendering and tile composition (M2 Phase 1+).
- scroll-plane occlusion / windowing.
- frame-by-frame tilemap mutation capture (M2 Phase 2 when the frame
  loop exists).
- mid-frame tilemap swaps via raster IRQ (M3 timing model).
