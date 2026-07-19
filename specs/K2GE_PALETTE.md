# K2GE Palette v1 (M2 Phase 0)

Purpose:
- decode the K2GE on-chip palette RAM (`0x8200..0x83FF`) into a
  human-readable view without requiring a rendered framebuffer
- close the ROADMAP §8 P0 item "inspecteur VRAM/OAM/palettes" for
  the palette half (the OAM / tilemap halves land in later M2 passes)
- pair cleanly with `memory-dump --seed-from` and `registers
  --seed-from`: same workflow ("what does the captured run look
  like?"), different lenses

Current source references:
- `../../01_SDK/docs/NGPC_HW_QUICKREF.md` § "Palettes K2GE"
- `MEMORY_READ.md` § 2 (K2GE registers cold-start image)
- `ADDRESS_SPACE.md` (region `K2GE_REGS` `0x008000..0x008FFF`)

## 1. Scope of v1 (inspector only)

v1 is the inspector half of M2 Phase 0:

- **Decodes** the palette RAM as currently observed via the read bus
  + an optional savestate overlay. No rendering, no rasterising, no
  sprite / scroll plane composition.
- **Does not** model the K2GE control registers that select active
  planes, scroll origins, sprite priorities, raster IRQ thresholds.
  Those land in M2 Phase 1+ when the framebuffer pipeline ships.
- **Does not** modify the executor or any runtime state. Reads only.

## 2. Memory layout

Per `NGPC_HW_QUICKREF.md` (K2GE color mode):

| Address range      | Plane        | Layout                              |
|--------------------|--------------|--------------------------------------|
| `0x8200..0x827F`   | Sprite       | 16 palettes × 4 colors × 2 bytes    |
| `0x8280..0x82FF`   | SCR1 (BG)    | 16 palettes × 4 colors × 2 bytes    |
| `0x8300..0x837F`   | SCR2 (BG)    | 16 palettes × 4 colors × 2 bytes    |
| `0x8380..0x83DF`   | K1GE compat  | mono-mode palettes (NOT decoded v1) |
| `0x83E0..0x83EF`   | Background   | 8 backdrop colors                    |
| `0x83F0..0x83FF`   | Window       | 8 window colors                      |

Each color entry is **two little-endian bytes** forming a 12-bit
`0BGR` value:

```
Low byte (offset 0):  GGGG RRRR
High byte (offset 1): 0000 BBBB
```

Each component (`R`, `G`, `B`) is a 4-bit value in `0..15`. Bits
15..12 of the raw value are reserved (read as 0 on real silicon).

## 3. Data model

`core/k2ge.py` exposes:

- `K2geColor(raw, r, g, b)` — one decoded entry; helpers
  `hex_rgb12()` (canonical `0xBGR`) and `hex_rgb24()` (`#RRGGBB`
  via nibble replication so `0x5` becomes `0x55`).
- `K2gePalette(plane, index, base_address, colors)` — one 4-color
  group. `plane` is the human label (`"sprite"`, `"scr1"`, `"scr2"`,
  `"background"`, `"window"`); `index` is the slot inside the plane.
- `decode_color(low, high)` — direct byte-pair decode.
- `read_plane_palettes(memory, base, plane, count=16)` — read N
  consecutive 4-color palettes.
- `read_extra_color_block(memory, base, plane, count=8)` — read the
  flat backdrop / window 8-entry blocks (these are not 4-color
  groups, so they wrap as a single `K2gePalette` with `index=0`).
- `read_all_palettes(memory)` — return all five planes in one dict.

`memory` is a `dict[int, int]` keyed by 24-bit address. Callers build
it from the read bus cold-start image (`NgpcReadBus.builtin_bytes`)
optionally layered with a savestate's `writable_overlay`. Unbacked
addresses default to `0` (the K2GE cold-start value).

## 4. CLI

### `palette-info <rom> [--kind PLANE] [--seed-from state.json] [--json]`

Prints the decoded palettes for one or all planes.

`--kind` choices: `all` (default), `sprite`, `scr1`, `scr2`,
`background`, `window`.

`--seed-from` loads a savestate v2 JSON; its writable overlay is
layered on top of the cold-start image so cells written during the
captured run are decoded as they would appear at the captured PC.

`--json` emits a structured payload:

```json
{
  "rom": "…",
  "seed_from": "post_init.state.json" | null,
  "planes": {
    "sprite": [
      {
        "plane": "sprite",
        "index": 0,
        "base_address": 33280,
        "base_address_hex": "0x008200",
        "colors": [
          {
            "raw": 0,
            "raw_hex": "0x0000",
            "r": 0, "g": 0, "b": 0,
            "hex_rgb12": "0x000",
            "hex_rgb24": "#000000"
          },
          ...
        ]
      },
      ...
    ],
    "scr1": [...],
    ...
  }
}
```

## 5. Why this is the M2 Phase 0 starter

Palette is the simplest of the three M2 inspectors:

- **No timing model required.** The K2GE registers that ship later
  (scroll origins, raster IRQ, sprite priority) interact with the
  frame loop; palette is pure data and decodes the same way on
  every cycle.
- **No tile decoder required.** Palette decoding is byte-pair → RGB;
  it does not need a 2-bpp packed tile reader, sprite OAM iteration,
  or scroll-plane composition.
- **Closes one ROADMAP §8 P0 third.** The "inspecteur VRAM/OAM/palettes"
  bullet has three independent halves; palette is the one that needs
  zero new infrastructure beyond the existing read bus.

The OAM (`palette-info` peer) and tilemap inspectors land in
follow-up passes, sharing the same `_build_palette_memory_view`
overlay-merge helper.

## 6. Not modeled yet

- K1GE mono-compatibility palette range (`0x8380..0x83DF`) — decoded
  bytes can still be inspected via `memory-dump`, but the v1
  `palette-info` does not produce a structured K1GE view.
- Per-pixel rendering and tile composition (M2 Phase 1+).
- Palette mutations during a run captured frame-by-frame (M2 Phase 2
  when the frame loop exists).
- Mid-frame palette swaps via raster IRQ (M3 timing model).
