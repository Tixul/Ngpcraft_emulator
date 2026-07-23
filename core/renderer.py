"""K2GE framebuffer renderer (M2 Phase 1).

Pass 1.0 deliverable: backdrop-only frame compose + binary P6 PPM
export. Subsequent passes add scroll-plane raster (1.1), sprite raster
with PR.C composition (1.2), and window clip + NEG invert (1.3).

The renderer reads through the same merged cold-start + savestate
memory view that the M2 Phase 0 inspectors consume (`palette-info`,
`oam-info`, `tilemap-info`, `tile-view`), so a single `--seed-from
<state.json>` workflow drives both inspection and visual export.

Source references:
- `01_SDK/docs/NGPC_HW_QUICKREF.md` § 5 "REGISTRES VIDÉO K2GE"
- `core/k2ge.py` — `K2geControlRegisters`, palette decoders, tile decoder

NGPC screen: 160 × 152 pixels, 60 fps. The renderer does not model
timing yet — it produces a single frame from a static memory snapshot.
"""

from __future__ import annotations

from dataclasses import dataclass

from core.k2ge import (
    K2GE_PALETTE_BG_COLORS_BASE,
    K2GE_PALETTE_WINDOW_COLORS_BASE,
    K2GE_PALETTE_SCR1_BASE,
    K2GE_PALETTE_SCR2_BASE,
    K2GE_PALETTE_SPRITE_BASE,
    K2geColor,
    K2geControlRegisters,
    K2geSprite,
    decode_color,
    read_control_registers,
    read_oam_sprites,
    read_plane_palettes,
    read_tile,
    read_tilemap,
)

NGPC_SCREEN_WIDTH = 160
NGPC_SCREEN_HEIGHT = 152

# Debug layer mask -- the video twin of the APU's channel mute. MUST match the native
# core bit for bit (`Machine::kLayer*` in cpp/src/machine.hpp, `ngpc_set_layer_mask`):
# a mask that means different things in the two cores would make the differential gate
# compare two different pictures and call it agreement.
LAYER_SCR1 = 0x01
LAYER_SCR2 = 0x02
LAYER_SPR_BACK = 0x04      # PR.C = 1, behind both planes
LAYER_SPR_MID = 0x08       # PR.C = 2, between the planes
LAYER_SPR_FRONT = 0x10     # PR.C = 3, in front of everything
LAYER_SPRITES = LAYER_SPR_BACK | LAYER_SPR_MID | LAYER_SPR_FRONT
LAYER_ALL = 0x1F

# Scroll plane geometry: 32 tiles × 32 tiles × 8-pixel tiles = 256×256 pixel
# plane. Scroll offsets wrap modulo this size.
_SCR_PLANE_PIXEL_SIZE = 256
_TILE_SIZE = 8


@dataclass(frozen=True)
class RenderedFrame:
    """One composed framebuffer plus the control-register snapshot.

    `pixels` is a tuple of `height` rows; each row is a tuple of
    `width` `K2geColor` entries. `K2geColor` carries 4-bit RGB
    components (0..15) matching the K2GE 12-bit 0BGR encoding.

    `control` is the `K2geControlRegisters` snapshot read at compose
    time — preserved for JSON diagnostics and for tests that need to
    assert which scroll / window / priority bits drove the output.
    """

    width: int
    height: int
    pixels: tuple[tuple[K2geColor, ...], ...]
    control: K2geControlRegisters
    backdrop_color: K2geColor


def _read_byte(memory: dict[int, int], address: int) -> int:
    return memory.get(address & 0xFFFFFF, 0) & 0xFF


def resolve_oowc_color(
    memory: dict[int, int], control: K2geControlRegisters,
) -> K2geColor:
    """Resolve the out-of-window color from the OOWC index + WINDOW block.

    `control.oowc` is bits 2..0 of register `0x8012` (2D Control); it indexes
    the 8-entry WINDOW block at `0x83F0..0x83FF` -- NOT the backdrop block at
    `0x83E0` this used to read (both cores shared that mistake, so the
    differential gate never saw it). Fatal Fury's intro convicted it: the game
    fills 0x83E0 with WHITE (its in-window backdrop), writes a grey ramp at
    0x83F0 whose entry 7 is BLACK, and sets OOWC=7 -- a black letterbox around
    the portrait. One shared block would paint it white. A game does not build
    a ramp in a palette it never uses. (Register map concurs: HW_PAL_BG 0x83E0
    "couleur de fond", HW_PAL_WIN 0x83F0 "couleur hors-fenetre".) On HW the
    OOWC color fills every screen pixel outside the window `[WBA, WBA+WSI[`.
    """
    color_base = K2GE_PALETTE_WINDOW_COLORS_BASE + control.oowc * 2
    low = _read_byte(memory, color_base)
    high = _read_byte(memory, color_base + 1)
    return decode_color(low, high)


def resolve_backdrop_color(
    memory: dict[int, int], control: K2geControlRegisters,
) -> K2geColor:
    """Resolve the screen-wide backdrop color from BGC + backdrop palette.

    The Tech Ref reads "D7=1 D6=0 valid, else black", and this used to enforce
    it. Real games disagree: Ogre Battle Gaiden writes a blue into 0x83E0[0],
    sets BGC=0x00, and expects a blue sky. The game is the authority here
    ("HACK: 01 AUG 2002 - Always on!", commenting out `(bgc & 0xC0) == 0x80`).
    So the backdrop is the palette entry unconditionally; a game that wants
    black leaves 0x83E0[index] black (empty-memory cold start still resolves to
    0). The enable bits do not gate the colour. `control.bgc_enabled` stays on
    the struct for callers that report register state, but the picture ignores
    it.
    """
    color_base = K2GE_PALETTE_BG_COLORS_BASE + control.bgc_index * 2
    low = _read_byte(memory, color_base)
    high = _read_byte(memory, color_base + 1)
    return decode_color(low, high)


def _scroll_offset_for_plane(
    control: K2geControlRegisters, plane: str,
) -> tuple[int, int]:
    if plane == "scr1":
        return control.s1so_h, control.s1so_v
    if plane == "scr2":
        return control.s2so_h, control.s2so_v
    raise ValueError(f"plane must be 'scr1' or 'scr2'; got {plane!r}")


RasterLog = "tuple[bytes, ...]"   # 152 rows of the 0x8000..0x803F register block

_RASTER_BASE = 0x8000
_REG_S1SO_H = 0x8032 - _RASTER_BASE
_REG_S2SO_H = 0x8034 - _RASTER_BASE


def _line_scroll_offsets(
    control: K2geControlRegisters, plane: str, raster_log: RasterLog | None,
) -> list[tuple[int, int]]:
    """The (H, V) scroll offset each of the 152 lines is drawn with.

    A frame is not one picture drawn from one set of registers. The scroll offset
    registers are latched PER LINE -- the K2GE Tech Ref says so twice, in its caution
    on 0x8030 and again on 0x8032: *"The result of the value set in this register is
    displayed from the next line being drawn."* Games exploit that to split the screen
    (a fixed HUD over a scrolling field) and to fake parallax by rewriting the offset
    on every H-blank. This project's own engine ships `ngpc_raster_set_scroll_table`,
    so a one-offset-per-frame renderer cannot even draw the user's own games right.

    Without a raster log (a synthetic memory view, a savestate, a unit test) there is
    no per-line history to honour, and the frame's single register snapshot applies to
    every line -- which is exactly the old behaviour, so nothing regresses.

    ⚠️ NOT MODELLED, deliberately: `P.F` (plane priority) and the window registers can
    also change mid-frame. They are latched per line by the same rule, but honouring
    them means composing the whole frame line by line, not just the scroll. No ROM in
    the corpus was seen to need it; when one is, this is where it goes.
    """
    fallback = _scroll_offset_for_plane(control, plane)
    if raster_log is None:
        return [fallback] * NGPC_SCREEN_HEIGHT

    h_reg = _REG_S1SO_H if plane == "scr1" else _REG_S2SO_H
    return [
        (line[h_reg], line[h_reg + 1]) if len(line) > h_reg + 1 else fallback
        for line in raster_log[:NGPC_SCREEN_HEIGHT]
    ]


def _palette_base_for_plane(plane: str) -> int:
    if plane == "scr1":
        return K2GE_PALETTE_SCR1_BASE
    if plane == "scr2":
        return K2GE_PALETTE_SCR2_BASE
    raise ValueError(f"plane must be 'scr1' or 'scr2'; got {plane!r}")


# --------------------------------------------------------------------------- #
# K1GE "upper palette compatible" mode -- the MONOCHROME games, and the BIOS.
#
# The cartridge header's byte 0x23 says which machine the game was written for
# (0x00 = the monochrome NGP, 0x10 = the colour NGPC), and the BIOS puts the K2GE
# into K1GE-compatible mode for the old ones. The mode bit is 0x87E2 bit 7
# (K2GETechRef § 4-9, Table 10; 0 = K2GE colour, which is the reset value).
#
# In that mode a pixel is resolved in TWO hops, not one:
#
#   1. a 3-bit LEVEL out of a lookup table at 0x8100 (Tech Ref § 4-12/13/14),
#      indexed by the plane, the 1-bit palette code P.C, and the 2bpp pixel value:
#
#         level = LUT[plane_base + P*4 + value]          value 1..3; 0 = clear
#
#   2. a 12-bit COLOUR out of the compat palette (Table 19):
#
#         colour = COMPAT[plane_base + (P*8 + level) * 2]
#
# ⚠️ THE SECOND INDEX WAS THE OPEN QUESTION, AND IT WAS SETTLED BY MEASUREMENT, NOT
# BY TASTE. Tech Ref § 5-3 gives the address computation as a FIGURE, and the figure
# is in neither the SDK text nor ngpcspec: the LUT emits 3 bits (8 values) while
# Table 19 allocates 16 entries per plane, so `index = level` and
# `index = P*8 + level` were both readable. Booting the REAL BIOS decided it -- the
# BIOS draws in compat mode and fills both tables, and every entry it writes is
# reachable under `P*8 + level` and only under it:
#
#     SPRITES  LUT pal1 = 1,6,5  ->  entries 9, 14, 13   (plus 7 from pal0)
#              palette non-zero at exactly  7, 9, 13, 14
#     SCROLL1  LUT pal1 = 0,2,6  ->  entries 8, 10, 14   (plus 2,3,6 from pal0)
#              palette non-zero at exactly  2, 3, 6, 8, 10, 14
#
# Under `index = level`, entries 8..15 could never be addressed -- and the BIOS
# fills them. See specs/K1GE_COMPAT_MODE.md § 3-bis.
# --------------------------------------------------------------------------- #

_K1GE_MODE_REGISTER = 0x0087E2          # bit 7: 1 = K1GE upper-palette compatible
_K1GE_MODE_BIT = 0x80

# Tech Ref § 4-12/13/14 -- the 3-bit level LUT, 8 bytes per plane.
_K1GE_LUT_BASE = {"sprite": 0x008100, "scr1": 0x008108, "scr2": 0x008110}
# Table 19 -- the 12-bit compat colour palettes, 16 entries (32 bytes) per plane.
_K1GE_COMPAT_BASE = {"sprite": 0x008380, "scr1": 0x0083A0, "scr2": 0x0083C0}


def k1ge_compat_enabled(memory: dict[int, int]) -> bool:
    """True when the K2GE is in K1GE upper-palette-compatible mode."""
    return bool(memory.get(_K1GE_MODE_REGISTER, 0) & _K1GE_MODE_BIT)


def _k1ge_plane_colors(memory: dict[int, int], plane: str) -> tuple[tuple[K2geColor, ...], ...]:
    """`[p_c][value]` -> colour, for one plane, in K1GE compat mode.

    Index 0 of each row is never read (pixel value 0 is the clear code) but is kept
    so the table can be indexed by the raw 2bpp value with no arithmetic.
    """
    lut_base = _K1GE_LUT_BASE[plane]
    pal_base = _K1GE_COMPAT_BASE[plane]

    rows: list[tuple[K2geColor, ...]] = []
    for p_c in (0, 1):
        colors: list[K2geColor] = []
        for value in range(4):
            level = memory.get(lut_base + p_c * 4 + value, 0) & 0x07
            entry = pal_base + (p_c * 8 + level) * 2
            colors.append(
                decode_color(memory.get(entry, 0), memory.get(entry + 1, 0))
            )
        rows.append(tuple(colors))
    return tuple(rows)


def _render_scroll_plane(
    framebuffer: list[list[K2geColor]],
    memory: dict[int, int],
    control: K2geControlRegisters,
    plane: str,
    tile_cache: dict[int, tuple[tuple[int, ...], ...]] | None = None,
    raster_log: RasterLog | None = None,
) -> None:
    """Composite one K2GE scroll plane onto a mutable framebuffer.

    Iterates per screen pixel — for each (sx, sy), wraps through the
    256×256 plane via the plane's 8-bit scroll offset, decodes the
    32×32 tilemap entry, applies H.F/V.F flip, reads the 2bpp tile
    pixel value, and writes the palette-resolved color when both
    `entry.c_c != 0` (tile-0 transparent convention) and `value != 0`
    (per-pixel palette transparency).

    `tile_cache` can be shared with sibling sprite-layer calls in the
    same frame so a tile referenced by both a sprite and a tilemap
    cell is decoded once per frame; a fresh local cache is allocated
    when the caller passes `None`.
    """
    line_offsets = _line_scroll_offsets(control, plane, raster_log)
    compat = k1ge_compat_enabled(memory)
    if compat:
        k1ge_colors = _k1ge_plane_colors(memory, plane)
        palettes = ()
    else:
        palette_base = _palette_base_for_plane(plane)
        palettes = read_plane_palettes(memory, palette_base, plane)
        k1ge_colors = ()
    tilemap = read_tilemap(memory, plane)
    if tile_cache is None:
        tile_cache = {}

    for sy in range(NGPC_SCREEN_HEIGHT):
        soh, sov = line_offsets[sy]
        wy = (sy + sov) & (_SCR_PLANE_PIXEL_SIZE - 1)
        ty = wy >> 3
        py = wy & (_TILE_SIZE - 1)
        row_base = ty * 32
        fb_row = framebuffer[sy]
        for sx in range(NGPC_SCREEN_WIDTH):
            wx = (sx + soh) & (_SCR_PLANE_PIXEL_SIZE - 1)
            tx = wx >> 3
            entry = tilemap[row_base + tx]
            # ⛔ THERE IS NO "TILE 0 IS BLANK" RULE. There used to be one here --
            # `if entry.c_c == 0: continue  # tile-0 = transparent (NGPC convention)` --
            # and that convention was INVENTED. The K2GE spec gives character 0 no
            # special status: it is 16 bytes of character RAM like every other tile.
            # Transparency on this machine is per-PIXEL (colour index 0), never per-tile.
            #
            # What it cost: Sonic's SEGA screen fills the whole background with tile 0,
            # whose bytes are 0xAA -- eight pixels of colour index 2 -- so the screen
            # should be a solid white field with the logo on top. We threw the field away
            # and drew only the logo's own tiles, which is why the user saw "white patches
            # behind the logo that stick out" on a black screen: the patches were the ONLY
            # part of the background we were drawing.
            tile_pixels = tile_cache.get(entry.c_c)
            if tile_pixels is None:
                tile_pixels = read_tile(memory, entry.c_c).pixels
                tile_cache[entry.c_c] = tile_pixels
            px = wx & (_TILE_SIZE - 1)
            px_eff = (_TILE_SIZE - 1 - px) if entry.h_flip else px
            py_eff = (_TILE_SIZE - 1 - py) if entry.v_flip else py
            value = tile_pixels[py_eff][px_eff]
            if value == 0:
                continue  # palette index 0 = transparent
            # In K1GE compat mode the palette code is the SINGLE P.C bit, not the
            # 4-bit CP.C: the old machine only had two palettes per plane.
            fb_row[sx] = (
                k1ge_colors[int(entry.p_c)][value] if compat
                else palettes[entry.cp_c].colors[value]
            )


def resolve_sprite_positions(
    memory: dict[int, int], control: K2geControlRegisters,
) -> list[tuple[K2geSprite, int, int]]:
    """Iterate OAM, fold chain offsets + global PO.H/V offset.

    Returns a list of `(sprite, screen_x, screen_y)` tuples in OAM
    order. `screen_x` and `screen_y` are 8-bit wrapped (`0..255`) —
    callers that want to draw clip per-pixel against the
    `NGPC_SCREEN_WIDTH × NGPC_SCREEN_HEIGHT` window; sprites at high
    coordinates simply stay off-screen rather than wrapping.

    Chain semantics per `NGPC_HW_QUICKREF.md` § 5: when `H.ch` is set
    on sprite N, its `H.P` field is treated as a delta from sprite
    N-1's effective position (same for `V.ch`/`V.P`). The chain
    state advances for **every** OAM entry, including hidden
    (`PR.C == 0`) sprites, so that placing a hidden anchor at the
    head of a chain group still positions its tail correctly.

    The global sprite offset `PO.H/V` (`0x8020`/`0x8021`) is added
    last to every sprite. Cold-start values are 0, so a frame that
    never writes those registers behaves exactly like a pure OAM
    list.
    """
    sprites = read_oam_sprites(memory)
    prev_h = 0
    prev_v = 0
    positioned: list[tuple[K2geSprite, int, int]] = []
    for sprite in sprites:
        if sprite.h_chain:
            h = (prev_h + sprite.h_pos) & 0xFF
        else:
            h = sprite.h_pos
        if sprite.v_chain:
            v = (prev_v + sprite.v_pos) & 0xFF
        else:
            v = sprite.v_pos
        prev_h = h
        prev_v = v
        screen_x = (h + control.po_h) & 0xFF
        screen_y = (v + control.po_v) & 0xFF
        positioned.append((sprite, screen_x, screen_y))
    return positioned


def build_sprite_line_buffer(
    memory: dict[int, int],
    positioned_sprites: list[tuple[K2geSprite, int, int]],
    palettes: tuple,
    tile_cache: dict[int, tuple[tuple[int, ...], ...]],
) -> dict[int, list[tuple[int, int, K2geColor]]]:
    """Fill the sprite line buffer the way the chip does: sprite 0 wins.

    K2GE Tech Ref § 4-3-3-1, *VRAM Address and Character Sprite Priority*:

        "Priority for sprites on screen is dependent on the VRAM address. The
         hardware reads the values from **the VRAM 0 address** and writes to the
         line buffer. During the write to the line buffer, the hardware checks
         the priority [...] **to avoid writing over previously written data**."

    So a contested pixel belongs to the LOWEST OAM index — sprite 0 is the chip's
    topmost sprite, not its bottom one. This renderer used to iterate 0..63 letting
    each sprite paint over the last, which is exactly backwards. Measured on a Sonic
    gameplay frame: 399 pixels were contested by two visible sprites, and the old
    order showed the wrong one on **all 399** — enemies vanishing under other
    sprites, and a metasprite compositing its own tiles in reverse.

    There is ONE buffer, not one per priority. A sprite that owns a pixel owns it
    whatever its `PR.C`; `PR.C` only decides where that pixel lands relative to the
    two scroll planes (Figure 4), which is why the caller blits in three passes.
    A transparent pixel (palette index 0) claims nothing, so a lower sprite's hole
    lets a higher-indexed sprite through.

    Returns the claimed pixels grouped by `PR.C` (1, 2, 3), in no particular order —
    each pixel appears exactly once across the three lists.
    """
    owned = bytearray(NGPC_SCREEN_WIDTH * NGPC_SCREEN_HEIGHT)
    layers: dict[int, list[tuple[int, int, K2geColor]]] = {1: [], 2: [], 3: []}

    compat = k1ge_compat_enabled(memory)
    k1ge_colors = _k1ge_plane_colors(memory, "sprite") if compat else ()

    for sprite, screen_x, screen_y in positioned_sprites:   # OAM order: 0 first
        if sprite.pr_c == 0:
            continue        # not shown -- but it still anchored the chain earlier
        tile = tile_cache.get(sprite.c_c)
        if tile is None:
            tile = read_tile(memory, sprite.c_c).pixels
            tile_cache[sprite.c_c] = tile
        # K1GE compat mode: two palettes, selected by the single P.C bit, resolved
        # through the 3-bit level LUT. K2GE mode: 16 palettes, selected by CP.C.
        palette_colors = (
            k1ge_colors[int(sprite.p_c)] if compat
            else palettes[sprite.cp_c].colors
        )
        layer = layers[sprite.pr_c]
        for py in range(_TILE_SIZE):
            # ⚠️ The coordinate space WRAPS. The manufacturer says so outright --
            # K2GE Tech Ref § 3-1, COORDINATES AND DISPLAY AREA:
            #
            #     VIRTUAL DISPLAY AREA : 256 x 256 [dot]  CYCLICAL STRUCTURE
            #     DISPLAY AREA         : 160 x 152 [dot]
            #
            # So a sprite at y = 249 hangs off the TOP: its rows run 249..255 and
            # then 0..6, and those last rows are INSIDE the display area. We used to
            # compute `249 + py`, find it past the bottom of a 152-line screen, and
            # drop the sprite entirely -- every sprite entering from the top or the
            # left edge simply vanished. (A third-party emulator states the same rule
            # the other way round, as a negative coordinate; it AGREES, it is not the
            # source. The cyclical world is.)
            sy = (screen_y + py) & 0xFF
            if sy >= NGPC_SCREEN_HEIGHT:
                continue
            py_eff = (_TILE_SIZE - 1 - py) if sprite.v_flip else py
            row = tile[py_eff]
            row_base = sy * NGPC_SCREEN_WIDTH
            for px in range(_TILE_SIZE):
                sx = (screen_x + px) & 0xFF
                if sx >= NGPC_SCREEN_WIDTH:
                    continue
                if owned[row_base + sx]:
                    continue        # a lower-indexed sprite already took this pixel
                px_eff = (_TILE_SIZE - 1 - px) if sprite.h_flip else px
                value = row[px_eff]
                if value == 0:
                    continue        # transparent: claims nothing
                owned[row_base + sx] = 1
                layer.append((sy, sx, palette_colors[value]))

    return layers


def _blit_sprite_layer(
    framebuffer: list[list[K2geColor]],
    layer: list[tuple[int, int, K2geColor]],
) -> None:
    """Paint one `PR.C` group of the sprite line buffer onto the framebuffer."""
    for sy, sx, color in layer:
        framebuffer[sy][sx] = color


def _apply_window_clip(
    framebuffer: list[list[K2geColor]],
    control: K2geControlRegisters,
    oowc_color: K2geColor,
) -> None:
    """Replace every pixel outside the active window with `oowc_color`.

    The window region is half-open `[WBA.H, WBA.H + WSI.H[` in X and
    `[WBA.V, WBA.V + WSI.V[` in Y. Cold-start `WSI = 0xFF` + `WBA = 0`
    yields `[0, 255[` which covers the entire 160×152 screen so this
    pass is a no-op on a fresh reset — exactly matching real silicon.

    Software that initialises a sub-window (e.g. menu region) drives
    every other pixel through this fill. The renderer does NOT enforce
    the documented `WBA + WSI ≤ 160 / 152` software constraint — if a
    game violates it, the fill simply doesn't kick in for those pixels
    (still HW-faithful).
    """
    x_min = control.wba_h
    x_max = control.wba_h + control.wsi_h
    y_min = control.wba_v
    y_max = control.wba_v + control.wsi_v
    for sy in range(NGPC_SCREEN_HEIGHT):
        if y_min <= sy < y_max:
            fb_row = framebuffer[sy]
            for sx in range(NGPC_SCREEN_WIDTH):
                if not (x_min <= sx < x_max):
                    fb_row[sx] = oowc_color
        else:
            framebuffer[sy] = [oowc_color] * NGPC_SCREEN_WIDTH


def _apply_neg_invert(framebuffer: list[list[K2geColor]]) -> None:
    """Invert every 4-bit RGB component (`c → c ^ 0x0F`) in place.

    K2GE 2D Control bit 7 (`0x8012`) flips the entire visible output —
    in-window composed pixels and the OOWC fill alike. The inversion
    runs last so the OOWC fill (from `_apply_window_clip`) is also
    inverted, matching the order in which real silicon delivers pixels
    to the LCD.
    """
    for sy in range(NGPC_SCREEN_HEIGHT):
        fb_row = framebuffer[sy]
        for sx in range(NGPC_SCREEN_WIDTH):
            c = fb_row[sx]
            inv_r = c.r ^ 0x0F
            inv_g = c.g ^ 0x0F
            inv_b = c.b ^ 0x0F
            fb_row[sx] = K2geColor(
                raw=(inv_b << 8) | (inv_g << 4) | inv_r,
                r=inv_r,
                g=inv_g,
                b=inv_b,
            )


def render_frame(
    memory: dict[int, int],
    raster_log: RasterLog | None = None,
    layer_mask: int = LAYER_ALL,
) -> RenderedFrame:
    """Compose one NGPC frame from a merged memory view.

    `layer_mask` is the debug show/hide mask (`LAYER_SCR1` … `LAYER_SPR_FRONT`); it
    must stay bit-for-bit the same concept as the native core's `ngpc_set_layer_mask`,
    which is why both live under the same names. Default `LAYER_ALL` = the real
    picture; anything else is an inspection view and must never feed a fidelity gate.

    `raster_log` is the K2GE register block (0x8000..0x803F) as it stood at the start
    of each of the 152 visible lines -- what the native core records while the beam
    runs. Passing it makes the scroll offsets PER LINE, which is what the hardware
    latches (see `_line_scroll_offsets`). Omitting it renders every line from the one
    end-of-frame snapshot, which is what a plain memory dict can offer.

    Pass 1.3 final pipeline (back → front, then two post-process passes):
      1. backdrop fill (BGC-resolved color or black)
      2. sprites with PR.C = 01 (behind both scroll planes)
      3. back scroll plane (SCR2 by default, SCR1 when bit 7 of
         `0x8030` is set)
      4. sprites with PR.C = 10 (between the two scroll planes)
      5. front scroll plane (SCR1 by default, SCR2 when prio flips)
      6. sprites with PR.C = 11 (in front of everything)
      7. window clip — pixels outside `[WBA, WBA+WSI[` replaced by OOWC
         color (bits 2..0 of `0x8012` indexing the backdrop block)
      8. NEG invert — when bit 7 of `0x8012` is set, every component
         of every pixel is inverted (`c ^= 0x0F`), including OOWC fill

    Hidden sprites (`PR.C == 00`) are never drawn but still advance
    the chain state in `resolve_sprite_positions`.
    """
    control = read_control_registers(memory)
    backdrop = resolve_backdrop_color(memory, control)
    framebuffer: list[list[K2geColor]] = [
        [backdrop] * NGPC_SCREEN_WIDTH for _ in range(NGPC_SCREEN_HEIGHT)
    ]
    tile_cache: dict[int, tuple[tuple[int, ...], ...]] = {}
    sprite_palettes = read_plane_palettes(
        memory, K2GE_PALETTE_SPRITE_BASE, "sprite",
    )
    positioned_sprites = resolve_sprite_positions(memory, control)

    if control.scr2_in_front:
        back_plane, front_plane = "scr1", "scr2"
    else:
        back_plane, front_plane = "scr2", "scr1"

    # One pass over the sprites, sprite 0 first, first-write-wins -- the chip's own
    # line buffer (see `build_sprite_line_buffer`). PR.C then places each claimed
    # pixel against the scroll planes; it never decides WHICH sprite owns it.
    sprite_layers = build_sprite_line_buffer(
        memory, positioned_sprites, sprite_palettes, tile_cache,
    )

    # The layer mask gates COMPOSITION only, never `build_sprite_line_buffer` above:
    # sprite 0 still wins its pixel whatever is shown, so hiding the front sprites
    # reveals the scroll plane under them -- not the sprite that lost the pixel.
    def _sprites(prc: int) -> None:
        if layer_mask & (LAYER_SPR_BACK << (prc - 1)):
            _blit_sprite_layer(framebuffer, sprite_layers[prc])

    def _plane(plane: str) -> None:
        if layer_mask & (LAYER_SCR1 if plane == "scr1" else LAYER_SCR2):
            _render_scroll_plane(
                framebuffer, memory, control, plane, tile_cache, raster_log,
            )

    _sprites(1)
    _plane(back_plane)
    _sprites(2)
    _plane(front_plane)
    _sprites(3)

    oowc_color = resolve_oowc_color(memory, control)
    _apply_window_clip(framebuffer, control, oowc_color)
    if control.neg:
        _apply_neg_invert(framebuffer)

    pixels = tuple(tuple(row) for row in framebuffer)
    return RenderedFrame(
        width=NGPC_SCREEN_WIDTH,
        height=NGPC_SCREEN_HEIGHT,
        pixels=pixels,
        control=control,
        backdrop_color=backdrop,
    )


def pixels_to_ppm_bytes(width: int, height: int, pixels) -> bytes:
    """Serialize an arbitrary `width × height` `K2geColor` grid as P6 PPM.

    Generic over the source pixel grid: consumed by both the
    `RenderedFrame` screen renderer (via `frame_to_ppm_bytes`) and the
    tile-atlas inspector in `core/atlas.py`. Each K2GE 4-bit component
    is expanded to 8 bits by nibble replication (`0x5 → 0x55`),
    matching `K2geColor.hex_rgb24()`.

    `pixels` is any iterable yielding `height` rows of `width`
    `K2geColor`-like objects (must expose `r`, `g`, `b` 4-bit fields).
    Tuples-of-tuples and lists-of-lists both work.
    """
    header = f"P6\n{width} {height}\n255\n".encode("ascii")
    body = bytearray(width * height * 3)
    cursor = 0
    for row in pixels:
        for color in row:
            body[cursor] = (color.r << 4) | color.r
            body[cursor + 1] = (color.g << 4) | color.g
            body[cursor + 2] = (color.b << 4) | color.b
            cursor += 3
    return header + bytes(body)


def frame_to_ppm_bytes(frame: RenderedFrame) -> bytes:
    """Serialize a `RenderedFrame` as binary P6 PPM (RGB888).

    Thin wrapper over `pixels_to_ppm_bytes` preserved as the public
    convenience for the screen renderer; new callers should prefer
    `pixels_to_ppm_bytes(width, height, pixels)` directly when they
    don't already have a `RenderedFrame`.
    """
    return pixels_to_ppm_bytes(frame.width, frame.height, frame.pixels)
