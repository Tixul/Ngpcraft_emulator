# K2GE Tile Pixels v1 (M2 Phase 0.5 — first visual lens)

Purpose:
- decode one 8×8 CHAR_RAM tile (`0xA000..0xBFFF`) into a structured
  pixel grid so the emulator can show **actual graphics** in text
  form, not just hex dumps
- bridge from the static tilemap inspector (M2 Phase 0, pass 18) to
  the eventual M2 Phase 1 renderer: this is the per-tile rasterizer
  the framebuffer composition will reuse
- give the operator a quick way to confirm "is this tile what I
  think it is?" before the full framebuffer pipeline lands

Current source references:
- `../../01_SDK/docs/NGPC_HW_QUICKREF.md` § "Character RAM (0xA000, tiles 8×8, 2bpp)"
- `K2GE_PALETTE.md` (sibling — palette colorisation pipe)
- `K2GE_TILEMAP.md` (sibling — consumer of tile numbers)
- `K2GE_OAM.md` (sibling — same C.C 9-bit tile field)

## 1. Scope of v1 (single-tile inspector)

v1 is deliberately narrow: one tile at a time, no framebuffer, no
sprite chains, no scroll offsets, no plane composition.

- **Decodes** 16 raw CHAR_RAM bytes into an 8×8 grid of 2-bit values
  (`0..3`).
- **Optional palette colorisation**: when `--plane` + `--palette N`
  are passed, each pixel value `0..3` is resolved to a `K2geColor`
  via the requested palette (sprite / SCR1 / SCR2 plane).
- **Reads only**. No executor side effect, no overlay write.
- Pairs with every other M2 Phase 0 inspector via the shared
  `--seed-from <state.json>` workflow.

## 2. Memory layout

Per `NGPC_HW_QUICKREF.md`:

```
CHAR_RAM : 0xA000..0xBFFF   (8 192 bytes = 512 tiles × 16 bytes)

Per tile (16 bytes = 8 rows × 2 bytes):
  Even byte (offset +0 of each row): dots 4..7, MSB position = dot 4
  Odd byte  (offset +1 of each row): dots 0..3, MSB position = dot 0

Per byte, 2 bits per dot:
  bits[7:6] = first dot   (dot 0 for odd byte, dot 4 for even byte)
  bits[5:4] = second dot
  bits[3:2] = third dot
  bits[1:0] = fourth dot
```

Address of tile #N = `0xA000 + N * 16`. Tile range is `0..511`,
matching the 9-bit `C.C` field carried by sprite + tilemap entries.

Conventional value semantics:
- `0` is the **transparent / palette-background** slot. The
  `K2geTilePixels.is_blank()` helper returns `True` when every
  pixel is `0`.
- `1`, `2`, `3` are foreground shades. Without a palette they map
  to light / medium / full ASCII shades; with a palette they map to
  `palette.colors[1..3]`.

## 3. Data model

`core/k2ge.py` exposes:

- `K2geTilePixels(tile_id, base_address, raw_bytes, pixels)` —
  `pixels` is `tuple[tuple[int, ...], ...]` of 8 rows × 8 cells.
- `decode_tile(raw, *, tile_id, base_address)` — direct decode of
  16 raw CHAR_RAM bytes.
- `read_tile(memory, tile_id)` — read 16 bytes from CHAR_RAM (via
  the merged overlay+cold-start view) and decode. Raises
  `ValueError` if `tile_id` is outside `0..511`.
- Constants `K2GE_CHAR_RAM_BASE = 0x00A000`,
  `CHAR_RAM_TILE_COUNT = 512`, `CHAR_RAM_BYTES_PER_TILE = 16`,
  `CHAR_RAM_TILE_WIDTH = 8`, `CHAR_RAM_TILE_HEIGHT = 8`.

## 4. CLI

### `tile-view <rom> <tile-id> [--plane sprite|scr1|scr2] [--palette N] [--seed-from state.json] [--json]`

Renders one tile as 4-level grayscale ASCII art:

| Value | Glyph (default) | Meaning                              |
|-------|------------------|--------------------------------------|
| `0`   | ` ` (space)      | transparent / palette background     |
| `1`   | `░` (light)       | first foreground shade               |
| `2`   | `▒` (medium)      | second foreground shade              |
| `3`   | `█` (full block)  | third foreground shade               |

Example output:

```
ROM: …/demo.ngc
Seed-from: demo.state.json
Tile #1 (0x001)  address=0x00A010  blank=False
Pixels (value -> glyph: 0=' ', 1=light, 2=medium, 3=full block):
  y=0  |█ █ █ █ |  values=[3 0 3 0 3 0 3 0]
  y=1  | █ █ █ █|  values=[0 3 0 3 0 3 0 3]
  …
```

`--plane <sprite|scr1|scr2>` + `--palette N` (N in `0..15`) attach
the resolved `K2geColor` to every pixel in the `--json` payload
(`hex_rgb24` per pixel). The two flags must be passed together; one
alone errors out with exit 1.

`--seed-from state.json` layers a savestate v2's writable overlay
on the cold-start CHAR_RAM so tiles loaded by the captured run are
decoded.

`--json` emits a structured payload:

```json
{
  "rom": "…",
  "seed_from": "demo.state.json" | null,
  "char_ram_base_hex": "0x00A000",
  "tile_width": 8, "tile_height": 8,
  "palette_plane": "scr1" | null,
  "palette_index": 2 | null,
  "palette": {...} | null,                  // resolved K2gePalette
  "tile": {
    "tile_id": 1, "tile_id_hex": "0x001",
    "base_address_hex": "0x00A010",
    "raw_bytes_hex": "CC CC 33 33 …",
    "blank": false,
    "rows": [
      {
        "y": 0,
        "values": [3, 0, 3, 0, 3, 0, 3, 0],
        "glyphs": "█ █ █ █ ",
        "hex_rgb24": ["#0000FF", "#000000", …]    // only when palette set
      },
      …
    ]
  }
}
```

## 5. Why this is the M2 Phase 0.5 bridge

Pass 18 closed the static-inspector trio (palette / OAM / tilemap)
— but those tools answer "what is in the table?" not "what does
the table mean visually?". `tile-view` is the first command that
shows actual graphical content as graphical content, on a
single-tile granularity.

The next M2 step is the **full tile composition** (M2 Phase 1):
combine tilemap + CHAR_RAM + palette + scroll registers into a
plane raster, then merge sprites with priority codes into a
framebuffer. `tile-view` is the rasterizer kernel for that
pipeline — when Phase 1 lands, the framebuffer code will call
`read_tile(memory, c_c)` exactly the same way the CLI does now.

## 6. Not modeled yet

- **Tile flip on read**: the renderer applies `H.F` / `V.F`
  (from the sprite or tilemap entry) at composition time. `tile-view`
  shows the raw CHAR_RAM pixels in canonical orientation.
- **Multi-tile views**: the inspector is single-tile by design.
  A `--tiles N..M` range or a 16×16 grid of all CHAR_RAM tiles is
  a follow-up.
- **Scroll-plane raster** (M2 Phase 1).
- **Sprite raster with chain resolution** (M2 Phase 1).
- **Framebuffer compose** (M2 Phase 1).
- **PNG / PPM file output** (M2 Phase 1 — once the framebuffer
  exists).
