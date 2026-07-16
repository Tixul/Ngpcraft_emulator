"""K2GE palette decoding helpers (M2 Phase 0 — inspectors only).

The K2GE color palette RAM lives at `0x8200..0x83FF` in the address
space. M2 Phase 0 covers the **inspector** side of palette work: read
the 256-byte palette block from the runtime overlay (or the cold-start
image when no overlay write hit the region) and decode it into a
human-friendly view. The renderer side (BG / sprite raster, scroll,
priorities) is M2 Phase 1+ and is not modeled by this module.

Source: `01_SDK/docs/NGPC_HW_QUICKREF.md` § "Palettes K2GE".

Memory map (K2GE color mode):

| Range            | Owner       | Layout                              |
|------------------|-------------|--------------------------------------|
| 0x8200..0x827F   | Sprite      | 16 palettes × 4 colors × 2 bytes    |
| 0x8280..0x82FF   | SCR1 (BG)   | 16 palettes × 4 colors × 2 bytes    |
| 0x8300..0x837F   | SCR2 (BG)   | 16 palettes × 4 colors × 2 bytes    |
| 0x8380..0x83DF   | K1GE compat | mono-mode palettes (not decoded v1) |
| 0x83E0..0x83EF   | Background  | 8 backdrop colors                    |
| 0x83F0..0x83FF   | Window      | 8 window colors                      |

Color encoding: each entry is two little-endian bytes forming a
12-bit `0BGR` value (bits 15..12 unused, B[11:8], G[7:4], R[3:0]).
Each component is a 4-bit value `0..15`.
"""

from __future__ import annotations

from dataclasses import dataclass

K2GE_PALETTE_SPRITE_BASE = 0x008200
K2GE_PALETTE_SCR1_BASE = 0x008280
K2GE_PALETTE_SCR2_BASE = 0x008300
K2GE_PALETTE_BG_COLORS_BASE = 0x0083E0
K2GE_PALETTE_WINDOW_COLORS_BASE = 0x0083F0

PALETTES_PER_PLANE = 16
COLORS_PER_PALETTE = 4
BYTES_PER_COLOR = 2  # little-endian 0BGR

K2GE_OAM_BASE = 0x008800
K2GE_OAM_PALETTE_CODES_BASE = 0x008C00
OAM_SPRITE_COUNT = 64
OAM_BYTES_PER_SPRITE = 4

K2GE_SCR1_TILEMAP_BASE = 0x009000
K2GE_SCR2_TILEMAP_BASE = 0x009800
TILEMAP_TILES_PER_ROW = 32
TILEMAP_TILES_PER_COL = 32
TILEMAP_BYTES_PER_TILE = 2

K2GE_CHAR_RAM_BASE = 0x00A000
CHAR_RAM_TILE_COUNT = 512  # 9-bit tile index 0..511
CHAR_RAM_BYTES_PER_TILE = 16  # 8 rows × 2 bytes
CHAR_RAM_TILE_WIDTH = 8
CHAR_RAM_TILE_HEIGHT = 8

# K2GE control register addresses (M2 Phase 1).
# Reference: 01_SDK/docs/NGPC_HW_QUICKREF.md § 5 "REGISTRES VIDÉO K2GE".
K2GE_REG_WBA_H = 0x008002          # Window origin X
K2GE_REG_WBA_V = 0x008003          # Window origin Y
K2GE_REG_WSI_H = 0x008004          # Window width  (default 0xFF)
K2GE_REG_WSI_V = 0x008005          # Window height (default 0xFF)
K2GE_REG_2D_CONTROL = 0x008012     # bit7 NEG, bits 2..0 OOWC
K2GE_REG_PO_H = 0x008020           # Sprite global offset X
K2GE_REG_PO_V = 0x008021           # Sprite global offset Y
K2GE_REG_SCROLL_PRIO = 0x008030    # bit7: 0=SCR1 front, 1=SCR2 front
K2GE_REG_S1SO_H = 0x008032         # SCR1 scroll offset X
K2GE_REG_S1SO_V = 0x008033         # SCR1 scroll offset Y
K2GE_REG_S2SO_H = 0x008034         # SCR2 scroll offset X
K2GE_REG_S2SO_V = 0x008035         # SCR2 scroll offset Y
K2GE_REG_BGC = 0x008118            # bit7=1,bit6=0 enable, bits 2..0 index
K2GE_REG_MODE = 0x0087E2           # bit7: 0=K2GE color, 1=K1GE compat

# PR.C (Priority Code) decoded labels — per NGPC_HW_QUICKREF.md.
_PRIORITY_LABELS = (
    "hidden",         # 00 — sprite not displayed
    "behind-scr",     # 01 — drawn behind both scroll planes
    "middle",         # 10 — drawn between SCR1 and SCR2
    "front",          # 11 — drawn in front of both scroll planes
)


@dataclass(frozen=True)
class K2geColor:
    """One palette entry decoded from the on-chip palette RAM.

    `raw` is the little-endian 16-bit value as it appears in memory
    (high byte is the high-order bits). `r`, `g`, `b` are the
    4-bit component values in `0..15`.
    """

    raw: int
    r: int
    g: int
    b: int

    def hex_rgb12(self) -> str:
        """Return the canonical 3-hex-digit BGR string (`0xBGR`)."""
        return f"0x{self.b:X}{self.g:X}{self.r:X}"

    def hex_rgb24(self) -> str:
        """Return the 24-bit RGB string by replicating each nibble.

        Useful for display in tooling that expects 8-bit channels:
        `0x05` (4-bit `5`) becomes `0x55` (8-bit), `0x0F` becomes
        `0xFF`. The exact replication trick keeps black at 0 and
        full intensity at 0xFF.
        """
        r8 = (self.r << 4) | self.r
        g8 = (self.g << 4) | self.g
        b8 = (self.b << 4) | self.b
        return f"#{r8:02X}{g8:02X}{b8:02X}"


@dataclass(frozen=True)
class K2gePalette:
    """One palette of 4 colors plus its plane / index identity."""

    plane: str  # "sprite" | "scr1" | "scr2" | "background" | "window"
    index: int
    base_address: int
    colors: tuple[K2geColor, ...]


def decode_color(low: int, high: int) -> K2geColor:
    """Decode two little-endian bytes into a `K2geColor`.

    The encoding is `0BGR` 12-bit: the low byte carries `GGGG RRRR`
    and the high byte carries `0000 BBBB`. Bits 15..12 of the raw
    value are reserved and should read as 0 on real silicon.
    """
    raw = (high & 0xFF) << 8 | (low & 0xFF)
    r = raw & 0x0F
    g = (raw >> 4) & 0x0F
    b = (raw >> 8) & 0x0F
    return K2geColor(raw=raw, r=r, g=g, b=b)


def _read_byte(memory: dict[int, int], address: int) -> int:
    """Read one byte at `address` from a memory dict, defaulting to 0.

    The dict is the merged view of the runtime overlay layered on
    top of the cold-start image — callers build it from a savestate
    payload's `writable_overlay` plus the cold-start image returned
    by `core/memory._build_builtin_readable_bytes`.
    """
    return memory.get(address & 0xFFFFFF, 0) & 0xFF


def read_plane_palettes(
    memory: dict[int, int],
    base_address: int,
    plane: str,
    count: int = PALETTES_PER_PLANE,
) -> tuple[K2gePalette, ...]:
    """Decode `count` consecutive 4-color palettes starting at `base_address`.

    `plane` is the human label attached to every returned palette;
    it does not change the decoding. The classic NGPC layout uses
    `count=16` for sprite / SCR1 / SCR2 planes.
    """
    palettes: list[K2gePalette] = []
    palette_stride = COLORS_PER_PALETTE * BYTES_PER_COLOR  # 8 bytes per palette
    for palette_index in range(count):
        palette_base = base_address + palette_index * palette_stride
        colors: list[K2geColor] = []
        for color_index in range(COLORS_PER_PALETTE):
            color_base = palette_base + color_index * BYTES_PER_COLOR
            low = _read_byte(memory, color_base)
            high = _read_byte(memory, color_base + 1)
            colors.append(decode_color(low, high))
        palettes.append(
            K2gePalette(
                plane=plane,
                index=palette_index,
                base_address=palette_base,
                colors=tuple(colors),
            )
        )
    return tuple(palettes)


def read_all_palettes(memory: dict[int, int]) -> dict[str, tuple[K2gePalette, ...]]:
    """Read sprite, SCR1, and SCR2 palettes (16 each, 4 colors each).

    Backdrop and window palette ranges (`0x83E0..0x83FF`) are read
    as a single 8-entry tuple with plane label `"background"` /
    `"window"` since they are not laid out as 4-color palettes.
    """
    sprite = read_plane_palettes(
        memory, K2GE_PALETTE_SPRITE_BASE, "sprite",
    )
    scr1 = read_plane_palettes(
        memory, K2GE_PALETTE_SCR1_BASE, "scr1",
    )
    scr2 = read_plane_palettes(
        memory, K2GE_PALETTE_SCR2_BASE, "scr2",
    )
    bg_colors = read_extra_color_block(memory, K2GE_PALETTE_BG_COLORS_BASE, "background")
    win_colors = read_extra_color_block(memory, K2GE_PALETTE_WINDOW_COLORS_BASE, "window")
    return {
        "sprite": sprite,
        "scr1": scr1,
        "scr2": scr2,
        "background": bg_colors,
        "window": win_colors,
    }


@dataclass(frozen=True)
class K2geSprite:
    """One sprite entry decoded from the OAM (0x8800..0x88FF) + CP.C (0x8C00).

    Layout per `NGPC_HW_QUICKREF.md` § "Sprite VRAM":

    ```
    +0 : C.C bits[7:0]      (tile number, low byte)
    +1 : [H.F][V.F][P.C][PR.C MSB][PR.C LSB][H.ch][V.ch][C.C bit8]
    +2 : H.P                (horizontal position)
    +3 : V.P                (vertical position)
    0x8C00+n : CP.C         (color palette code 0..15, K2GE color mode only)
    ```

    Chain semantics: when `h_chain` (resp. `v_chain`) is set, the H.P
    (resp. V.P) field is a *relative* offset from the previous sprite
    in the chain, not an absolute coordinate. The decoder reports the
    raw bytes; the renderer resolves the chain.

    Tile number `c_c` is a 9-bit value (`0..511`) combining the
    `C.C bits[7:0]` low byte and the `C.C bit8` high bit from byte +1.
    """

    index: int
    base_address: int
    raw_bytes: bytes        # 4 raw OAM bytes (C.C low, attrib, H.P, V.P)
    cp_c_raw: int           # raw CP.C byte from 0x8C00+index

    # Decoded fields
    c_c: int                # tile number, 9 bits (0..511)
    h_flip: bool
    v_flip: bool
    p_c: bool               # K2GE plane-code bit (see HW_QUICKREF; opaque flag)
    pr_c: int               # priority code 0..3
    pr_c_label: str         # human label: hidden / behind-scr / middle / front
    h_chain: bool
    v_chain: bool
    h_pos: int              # raw H.P byte (absolute or relative — see h_chain)
    v_pos: int              # raw V.P byte
    cp_c: int               # color palette code 0..15

    def is_hidden(self) -> bool:
        """True when PR.C is 00 (sprite is not displayed)."""
        return self.pr_c == 0


def decode_sprite(
    raw_oam: bytes, cp_c_byte: int, *, index: int, base_address: int
) -> K2geSprite:
    """Decode 4 OAM bytes + 1 CP.C byte into a `K2geSprite`."""
    assert len(raw_oam) == OAM_BYTES_PER_SPRITE, (
        f"sprite needs {OAM_BYTES_PER_SPRITE} bytes, got {len(raw_oam)}"
    )
    cc_low = raw_oam[0] & 0xFF
    attrib = raw_oam[1] & 0xFF
    h_pos = raw_oam[2] & 0xFF
    v_pos = raw_oam[3] & 0xFF

    cc_bit8 = attrib & 0x01
    c_c = (cc_bit8 << 8) | cc_low

    v_chain = bool((attrib >> 1) & 0x01)
    h_chain = bool((attrib >> 2) & 0x01)
    pr_c = (attrib >> 3) & 0x03
    p_c = bool((attrib >> 5) & 0x01)
    v_flip = bool((attrib >> 6) & 0x01)
    h_flip = bool((attrib >> 7) & 0x01)

    return K2geSprite(
        index=index,
        base_address=base_address,
        raw_bytes=bytes(raw_oam),
        cp_c_raw=cp_c_byte & 0xFF,
        c_c=c_c,
        h_flip=h_flip,
        v_flip=v_flip,
        p_c=p_c,
        pr_c=pr_c,
        pr_c_label=_PRIORITY_LABELS[pr_c],
        h_chain=h_chain,
        v_chain=v_chain,
        h_pos=h_pos,
        v_pos=v_pos,
        cp_c=cp_c_byte & 0x0F,
    )


def read_oam_sprites(
    memory: dict[int, int], count: int = OAM_SPRITE_COUNT,
) -> tuple[K2geSprite, ...]:
    """Decode the K2GE OAM (`0x8800..`) + CP.C strip (`0x8C00..`).

    `count` defaults to 64 (the hardware sprite slot count). The
    decoder always reads the full OAM block — there is no "stop on
    null entry" sentinel on K2GE, the renderer is expected to read
    all 64 slots and skip those with `pr_c == 0` (hidden).
    """
    sprites: list[K2geSprite] = []
    for sprite_index in range(count):
        oam_base = K2GE_OAM_BASE + sprite_index * OAM_BYTES_PER_SPRITE
        raw = bytes(
            _read_byte(memory, oam_base + offset)
            for offset in range(OAM_BYTES_PER_SPRITE)
        )
        cp_c = _read_byte(memory, K2GE_OAM_PALETTE_CODES_BASE + sprite_index)
        sprites.append(
            decode_sprite(
                raw, cp_c, index=sprite_index, base_address=oam_base,
            )
        )
    return tuple(sprites)


@dataclass(frozen=True)
class K2geTilemapEntry:
    """One scroll-plane tilemap entry decoded from `0x9000..0x9FFF`.

    Layout per `NGPC_HW_QUICKREF.md` § "Scroll Plane VRAM":

    ```
    +0 : C.C bits[7:0]              (tile number low byte)
    +1 : [H.F][V.F][P.C][CP.C 3:0][C.C bit8]
    ```

    Address of tile `(x, y)` in plane = `base + (y * 32 + x) * 2`.
    Tile number `c_c` is a 9-bit value (`0..511`) — bit 8 is the
    LSB of byte +1, bits 7..0 come from byte +0. Tile `0` is the
    NGPC transparent tile by convention; renderers use it to leave a
    tilemap cell unset.
    """

    plane: str          # "scr1" or "scr2"
    x: int              # tile column 0..31
    y: int              # tile row 0..31
    base_address: int   # address of byte +0 (start of this 2-byte entry)
    raw_bytes: bytes    # 2 raw bytes (C.C low, attrib)

    # Decoded fields
    c_c: int            # tile number, 9 bits (0..511)
    h_flip: bool
    v_flip: bool
    p_c: bool           # K2GE plane-code bit
    cp_c: int           # palette code 0..15

    def is_empty(self) -> bool:
        """True when the tile number is 0 (transparent / unused slot)."""
        return self.c_c == 0


def decode_tilemap_entry(
    raw: bytes, *, plane: str, x: int, y: int, base_address: int,
) -> K2geTilemapEntry:
    """Decode 2 raw bytes into a `K2geTilemapEntry`."""
    assert len(raw) == TILEMAP_BYTES_PER_TILE, (
        f"tilemap entry needs {TILEMAP_BYTES_PER_TILE} bytes, got {len(raw)}"
    )
    cc_low = raw[0] & 0xFF
    attrib = raw[1] & 0xFF

    cc_bit8 = attrib & 0x01
    c_c = (cc_bit8 << 8) | cc_low

    cp_c = (attrib >> 1) & 0x0F
    p_c = bool((attrib >> 5) & 0x01)
    v_flip = bool((attrib >> 6) & 0x01)
    h_flip = bool((attrib >> 7) & 0x01)

    return K2geTilemapEntry(
        plane=plane,
        x=x,
        y=y,
        base_address=base_address,
        raw_bytes=bytes(raw),
        c_c=c_c,
        h_flip=h_flip,
        v_flip=v_flip,
        p_c=p_c,
        cp_c=cp_c,
    )


def _plane_base_for(plane: str) -> int:
    if plane == "scr1":
        return K2GE_SCR1_TILEMAP_BASE
    if plane == "scr2":
        return K2GE_SCR2_TILEMAP_BASE
    raise ValueError(f"plane must be 'scr1' or 'scr2'; got {plane!r}")


def read_tilemap(
    memory: dict[int, int], plane: str,
) -> tuple[K2geTilemapEntry, ...]:
    """Decode all 32×32 = 1024 tilemap entries for one scroll plane.

    Returned in row-major order: index `(y * 32 + x)` is tile `(x, y)`.
    """
    base = _plane_base_for(plane)
    entries: list[K2geTilemapEntry] = []
    for y in range(TILEMAP_TILES_PER_COL):
        for x in range(TILEMAP_TILES_PER_ROW):
            entry_base = base + (y * TILEMAP_TILES_PER_ROW + x) * TILEMAP_BYTES_PER_TILE
            raw = bytes(
                _read_byte(memory, entry_base + offset)
                for offset in range(TILEMAP_BYTES_PER_TILE)
            )
            entries.append(
                decode_tilemap_entry(
                    raw, plane=plane, x=x, y=y, base_address=entry_base,
                )
            )
    return tuple(entries)


@dataclass(frozen=True)
class K2geTilePixels:
    """One 8×8 tile decoded from CHAR_RAM (`0xA000..0xBFFF`).

    Layout per `NGPC_HW_QUICKREF.md` § "Character RAM":

    - 16 bytes per tile, 8 rows × 2 bytes
    - Even byte (offset +0 of each row): dots 4..7, 2 bits each,
      MSB position = dot 4
    - Odd byte (offset +1): dots 0..3, MSB position = dot 0

    `pixels` is a tuple of 8 rows; each row is a tuple of 8 2-bit
    values in `0..3` (left→right). Value `0` is the conventional
    NGPC "transparent / palette background" slot.

    Address of tile #N = `0xA000 + N * 16`. Tile range is `0..511`
    (matching the 9-bit `C.C` field in sprite and tilemap entries).
    """

    tile_id: int
    base_address: int
    raw_bytes: bytes                              # 16 raw CHAR_RAM bytes
    pixels: tuple[tuple[int, ...], ...]           # 8 rows × 8 cells, each 0..3

    def is_blank(self) -> bool:
        """True when every pixel is 0 (the conventional empty tile)."""
        return all(value == 0 for row in self.pixels for value in row)


def _decode_tile_row(even_byte: int, odd_byte: int) -> tuple[int, ...]:
    """Decode one tile row's 2 bytes into 8 left→right 2-bit values.

    Layout per NGPC_HW_QUICKREF:
      odd_byte  bits[7:6]=dot0, [5:4]=dot1, [3:2]=dot2, [1:0]=dot3
      even_byte bits[7:6]=dot4, [5:4]=dot5, [3:2]=dot6, [1:0]=dot7
    """
    dots: list[int] = []
    # dots 0..3 come from odd byte (high bits first)
    for shift in (6, 4, 2, 0):
        dots.append((odd_byte >> shift) & 0x03)
    # dots 4..7 come from even byte
    for shift in (6, 4, 2, 0):
        dots.append((even_byte >> shift) & 0x03)
    return tuple(dots)


def decode_tile(raw: bytes, *, tile_id: int, base_address: int) -> K2geTilePixels:
    """Decode 16 raw CHAR_RAM bytes into a `K2geTilePixels`."""
    assert len(raw) == CHAR_RAM_BYTES_PER_TILE, (
        f"tile needs {CHAR_RAM_BYTES_PER_TILE} bytes, got {len(raw)}"
    )
    rows: list[tuple[int, ...]] = []
    for row_index in range(CHAR_RAM_TILE_HEIGHT):
        even_byte = raw[row_index * 2] & 0xFF
        odd_byte = raw[row_index * 2 + 1] & 0xFF
        rows.append(_decode_tile_row(even_byte, odd_byte))
    return K2geTilePixels(
        tile_id=tile_id,
        base_address=base_address,
        raw_bytes=bytes(raw),
        pixels=tuple(rows),
    )


def read_tile(memory: dict[int, int], tile_id: int) -> K2geTilePixels:
    """Read one tile (16 bytes) from CHAR_RAM and decode its pixels.

    Raises `ValueError` if `tile_id` is outside the 9-bit range
    `0..511` (the K2GE C.C field is exactly 9 bits).
    """
    if not (0 <= tile_id < CHAR_RAM_TILE_COUNT):
        raise ValueError(
            f"tile_id must be in 0..{CHAR_RAM_TILE_COUNT - 1}; got {tile_id}"
        )
    base = K2GE_CHAR_RAM_BASE + tile_id * CHAR_RAM_BYTES_PER_TILE
    raw = bytes(
        _read_byte(memory, base + offset)
        for offset in range(CHAR_RAM_BYTES_PER_TILE)
    )
    return decode_tile(raw, tile_id=tile_id, base_address=base)


@dataclass(frozen=True)
class K2geControlRegisters:
    """Decoded snapshot of the K2GE video control registers.

    Read from a merged cold-start + savestate memory view. See
    `01_SDK/docs/NGPC_HW_QUICKREF.md` § 5 "REGISTRES VIDÉO K2GE" for the
    raw layout. Pass 1.0 of the renderer consumes `bgc_*` to fill the
    backdrop; later passes consume the scroll, window, sprite-offset
    and 2D-control fields.
    """

    # Window region (out-of-window OOWC fill — pass 1.3).
    wba_h: int          # 0x8002
    wba_v: int          # 0x8003
    wsi_h: int          # 0x8004 — cold-start 0xFF (full width)
    wsi_v: int          # 0x8005 — cold-start 0xFF (full height)

    # 0x8012 — 2D control.
    neg: bool           # bit 7 — invert all colors (pass 1.3)
    oowc: int           # bits 2..0 — out-of-window color index 0..7

    # 0x8020 / 0x8021 — sprite global position offset.
    po_h: int
    po_v: int

    # 0x8030 bit 7 — scroll plane priority.
    scr2_in_front: bool

    # 0x8032..0x8035 — scroll plane offsets.
    s1so_h: int
    s1so_v: int
    s2so_h: int
    s2so_v: int

    # 0x8118 — backdrop control (BGC).
    bgc_enabled: bool   # bit 7 = 1 AND bit 6 = 0
    bgc_index: int      # bits 2..0 — index into backdrop palette (0..7)
    bgc_raw: int        # raw byte (diagnostics)

    # 0x87E2 — display mode.
    k1ge_compat: bool   # bit 7 — 1 = K1GE compat mode, 0 = K2GE color


def read_control_registers(memory: dict[int, int]) -> K2geControlRegisters:
    """Decode the K2GE control registers from a merged memory view.

    Cold-start image gives the documented power-on values: BGC=0x00
    (backdrop disabled, so the renderer falls back to black), WSI.H/V
    are 0x00 in the unbacked CPU I/O page rather than 0xFF — callers
    that need the "default 0xFF window" semantics should layer a real
    captured savestate that includes the reset writes.
    """
    bgc_raw = _read_byte(memory, K2GE_REG_BGC)
    # BGC enable: bit 7 = 1 AND bit 6 = 0 (per HW_QUICKREF.md).
    bgc_enabled = (bgc_raw & 0x80) != 0 and (bgc_raw & 0x40) == 0
    twod = _read_byte(memory, K2GE_REG_2D_CONTROL)
    return K2geControlRegisters(
        wba_h=_read_byte(memory, K2GE_REG_WBA_H),
        wba_v=_read_byte(memory, K2GE_REG_WBA_V),
        wsi_h=_read_byte(memory, K2GE_REG_WSI_H),
        wsi_v=_read_byte(memory, K2GE_REG_WSI_V),
        neg=bool(twod & 0x80),
        oowc=twod & 0x07,
        po_h=_read_byte(memory, K2GE_REG_PO_H),
        po_v=_read_byte(memory, K2GE_REG_PO_V),
        scr2_in_front=bool(_read_byte(memory, K2GE_REG_SCROLL_PRIO) & 0x80),
        s1so_h=_read_byte(memory, K2GE_REG_S1SO_H),
        s1so_v=_read_byte(memory, K2GE_REG_S1SO_V),
        s2so_h=_read_byte(memory, K2GE_REG_S2SO_H),
        s2so_v=_read_byte(memory, K2GE_REG_S2SO_V),
        bgc_enabled=bgc_enabled,
        bgc_index=bgc_raw & 0x07,
        bgc_raw=bgc_raw,
        k1ge_compat=bool(_read_byte(memory, K2GE_REG_MODE) & 0x80),
    )


def read_extra_color_block(
    memory: dict[int, int],
    base_address: int,
    plane: str,
    count: int = 8,
) -> tuple[K2gePalette, ...]:
    """Read the backdrop / window 8-entry color blocks.

    These ranges are flat 8-color tables rather than 16-by-4 palette
    grids. The result is wrapped as a single `K2gePalette` with
    `index=0` for symmetry with the other planes.
    """
    colors: list[K2geColor] = []
    for color_index in range(count):
        color_base = base_address + color_index * BYTES_PER_COLOR
        low = _read_byte(memory, color_base)
        high = _read_byte(memory, color_base + 1)
        colors.append(decode_color(low, high))
    return (
        K2gePalette(
            plane=plane,
            index=0,
            base_address=base_address,
            colors=tuple(colors),
        ),
    )
