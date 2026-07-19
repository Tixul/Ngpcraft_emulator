# K2GE OAM v1 (M2 Phase 0)

Purpose:
- decode the K2GE Object Attribute Memory (`0x8800..0x88FF`) and the
  per-sprite palette-code strip (`0x8C00..0x8C3F`) into a structured
  view without requiring a rendered framebuffer
- close another third of ROADMAP §8 P0 "inspecteur VRAM/OAM/palettes"
- pair with `palette-info` (the palette-RAM inspector shipped in
  pass 16): same overlay-merge helper, same `--seed-from` workflow,
  different lens on the same captured savestate

Current source references:
- `../../01_SDK/docs/NGPC_HW_QUICKREF.md` § "Sprite VRAM (0x8800)"
- `MEMORY_READ.md` § 2 (K2GE registers cold-start image)
- `K2GE_PALETTE.md` (sibling format and shared conventions)

## 1. Scope of v1 (inspector only)

v1 is the inspector half of M2 Phase 0 OAM work:

- **Decodes** the OAM + CP.C strip as observed via the read bus +
  optional savestate overlay. No rendering, no sprite raster, no
  scroll plane composition.
- **Does not** resolve sprite chains. `h_chain`/`v_chain` flags are
  reported raw; rendering chained sprites is a Phase 1 renderer job.
- **Does not** modify the executor or any runtime state. Reads only.

## 2. Memory layout

Per `NGPC_HW_QUICKREF.md`:

```
0x8800 + 4*n  (n in 0..63):
  +0 : C.C bits[7:0]      (tile number, low byte)
  +1 : [H.F][V.F][P.C][PR.C MSB][PR.C LSB][H.ch][V.ch][C.C bit8]
  +2 : H.P                (horizontal position byte)
  +3 : V.P                (vertical position byte)

0x8C00 + n    (n in 0..63):
  CP.C        (color palette code 0..15, K2GE color mode only)
```

So the OAM is 64 sprites × 4 bytes = **256 bytes** at `0x8800..0x88FF`,
followed by 64 × 1 byte = **64 bytes** at `0x8C00..0x8C3F`.

`PR.C` (Priority Code, 2 bits) maps to:

| Value | Label        | Renderer effect                       |
|-------|--------------|----------------------------------------|
| `00`  | `hidden`     | sprite is not displayed                |
| `01`  | `behind-scr` | drawn behind both scroll planes        |
| `10`  | `middle`     | drawn between SCR1 and SCR2            |
| `11`  | `front`      | drawn in front of both scroll planes   |

Tile number `C.C` is a 9-bit value (`0..511`) combining the low byte
(`+0`) and the high bit (`+1` bit 0).

Chain semantics: when `h_chain` (resp. `v_chain`) is set, the H.P
(resp. V.P) field is a *relative* offset from the previous sprite
in the chain, not an absolute coordinate. The decoder reports the
raw bytes; the renderer resolves the chain.

## 3. Data model

`core/k2ge.py` exposes:

- `K2geSprite(index, base_address, raw_bytes, cp_c_raw, c_c, h_flip,
  v_flip, p_c, pr_c, pr_c_label, h_chain, v_chain, h_pos, v_pos, cp_c)`
- `decode_sprite(raw_oam, cp_c_byte, *, index, base_address)` —
  direct decode of 4 OAM bytes + 1 CP.C byte.
- `read_oam_sprites(memory, count=64)` — read the full OAM table.
- Constants `K2GE_OAM_BASE = 0x008800`,
  `K2GE_OAM_PALETTE_CODES_BASE = 0x008C00`, `OAM_SPRITE_COUNT = 64`.

`K2geSprite.is_hidden()` returns `True` when `pr_c == 0`.

## 4. CLI

### `oam-info <rom> [--visible-only] [--seed-from state.json] [--json]`

Prints the 64 sprite slots.

`--visible-only` filters out sprites whose `PR.C` is 0 (hidden) —
useful for inspecting a captured run frame where most slots are
inactive.

`--seed-from` layers a savestate v2's writable overlay on the
cold-start image so cells written during the captured run are
decoded as they would appear at the captured PC.

`--json` emits a structured payload:

```json
{
  "rom": "…",
  "seed_from": "post_init.state.json" | null,
  "oam_base_hex": "0x008800",
  "cp_c_base_hex": "0x008C00",
  "total_sprites": 64,
  "visible_only": true,
  "shown_count": 1,
  "sprites": [
    {
      "index": 5,
      "base_address_hex": "0x008814",
      "raw_bytes_hex": "40 10 10 20",
      "tile": 64,
      "tile_hex": "0x040",
      "h_flip": false, "v_flip": false,
      "p_c": false,
      "pr_c": 2, "pr_c_label": "middle",
      "h_chain": false, "v_chain": false,
      "h_pos": 16, "v_pos": 32,
      "cp_c": 7, "cp_c_raw": 7,
      "hidden": false
    }
  ]
}
```

Human-readable rows use a 5-char flag mnemonic:
- `H` for `h_flip`, `V` for `v_flip`, `P` for `p_c`,
  `h` for `h_chain`, `v` for `v_chain`.

## 5. Not modeled yet

- sprite-chain resolution (V.ch / H.ch following the previous
  sprite's position).
- sprite raster / framebuffer composition (M2 Phase 1+).
- sprite occlusion vs scroll planes (priority resolution is reported
  but not applied — needs a real renderer).
- K1GE-compat sprite mode at `0x8380..0x839F` (mono palette indices).
- `P.C` semantic clarification — the bit is exposed raw; the
  hardware documentation we have today does not nail down what the
  renderer should do with it on a NGPC K2GE in color mode. Tracked
  as a `?` until a HW-validated reference fills it in.
