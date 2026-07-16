"""CHAR_RAM tile-atlas renderer (M2 Phase 1 extension).

Bridges the single-tile `tile-view` ASCII inspector (pass 19) and the
full framebuffer compose (passes 20-23). The atlas renders an
arbitrary list of 8×8 CHAR_RAM tiles into a grid as a flat pixel
array suitable for the `pixels_to_ppm_bytes` PPM writer in
`core/renderer.py`.

Atlas pixels do NOT go through the K2GE compose pipeline (no scroll
offsets, no sprite layering, no window clip, no NEG invert). It's a
pure "show me the contents of CHAR_RAM" inspector that consumes the
same `read_tile` kernel as the renderer's scroll-plane and sprite
helpers.

Reference:
- `K2GE_TILES.md` — single-tile rasterizer kernel
- `NGPC_HW_QUICKREF.md` § 5 "Character RAM"
"""

from __future__ import annotations

from typing import Iterable

from core.k2ge import (
    CHAR_RAM_TILE_COUNT,
    CHAR_RAM_TILE_HEIGHT,
    CHAR_RAM_TILE_WIDTH,
    K2geColor,
    K2gePalette,
    read_tile,
)

# 4-level grayscale colour table for default (palette-less) atlas
# rendering. Each entry corresponds to a 2-bpp tile pixel value 0..3
# and uses K2GE 4-bit components that nibble-replicate to the
# canonical grayscale ramp (0x00 / 0x55 / 0xAA / 0xFF in 8-bit).
GRAYSCALE_COLOR_TABLE: tuple[K2geColor, ...] = (
    K2geColor(raw=0x000, r=0,  g=0,  b=0),
    K2geColor(raw=0x555, r=5,  g=5,  b=5),
    K2geColor(raw=0xAAA, r=10, g=10, b=10),
    K2geColor(raw=0xFFF, r=15, g=15, b=15),
)


def _black_pixel() -> K2geColor:
    return GRAYSCALE_COLOR_TABLE[0]


def render_tile_atlas(
    memory: dict[int, int],
    tile_ids: Iterable[int],
    cols: int,
    *,
    palette: K2gePalette | None = None,
) -> tuple[int, int, list[list[K2geColor]]]:
    """Render an N-tile atlas into a `cols × ceil(N/cols)` grid.

    Returns `(width, height, pixels)` where `pixels` is a mutable
    list-of-lists of `K2geColor`. Width = `cols * 8`, height =
    `ceil(N/cols) * 8`. Unused grid cells (when `N % cols != 0`)
    stay at the grayscale "value 0" color (black) so the atlas
    always fills a clean rectangle.

    Each tile is read through `read_tile(memory, id)`; the 2-bpp
    value is resolved through `palette.colors[value]` when `palette`
    is provided, otherwise through `GRAYSCALE_COLOR_TABLE[value]`.

    Raises `ValueError` if `cols < 1` or any `tile_id` falls outside
    `0..511` (the 9-bit C.C range).
    """
    if cols < 1:
        raise ValueError(f"cols must be >= 1; got {cols}")

    tile_id_list = list(tile_ids)
    for tile_id in tile_id_list:
        if not (0 <= tile_id < CHAR_RAM_TILE_COUNT):
            raise ValueError(
                f"tile_id must be in 0..{CHAR_RAM_TILE_COUNT - 1}; got {tile_id}"
            )

    n = len(tile_id_list)
    rows = max(1, (n + cols - 1) // cols)
    width = cols * CHAR_RAM_TILE_WIDTH
    height = rows * CHAR_RAM_TILE_HEIGHT

    if palette is not None:
        color_table: tuple[K2geColor, ...] = palette.colors
    else:
        color_table = GRAYSCALE_COLOR_TABLE

    black = _black_pixel()
    pixels: list[list[K2geColor]] = [
        [black] * width for _ in range(height)
    ]

    # Decode each tile once (cache so a repeated tile id only reads CHAR_RAM
    # once even when the same id appears multiple times in the input list).
    tile_cache: dict[int, tuple[tuple[int, ...], ...]] = {}
    for grid_index, tile_id in enumerate(tile_id_list):
        tile = tile_cache.get(tile_id)
        if tile is None:
            tile = read_tile(memory, tile_id).pixels
            tile_cache[tile_id] = tile
        grid_col = grid_index % cols
        grid_row = grid_index // cols
        base_x = grid_col * CHAR_RAM_TILE_WIDTH
        base_y = grid_row * CHAR_RAM_TILE_HEIGHT
        for py in range(CHAR_RAM_TILE_HEIGHT):
            tile_row = tile[py]
            fb_row = pixels[base_y + py]
            for px in range(CHAR_RAM_TILE_WIDTH):
                fb_row[base_x + px] = color_table[tile_row[px]]

    return width, height, pixels
