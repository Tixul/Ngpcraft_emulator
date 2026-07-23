/* render.cpp — the K2GE, DRAWING ONE SCANLINE AT A TIME.
 *
 * WHY THIS EXISTS, and it is two reasons, not one.
 *
 * 1. THE PICTURE WAS COMPOSED AT THE END OF THE FRAME, FROM THE FINAL VRAM.
 *    The silicon draws each line as the beam passes it. A scrolling game -- Metal Slug,
 *    say -- STREAMS new tiles and map rows into VRAM *while the frame is being drawn*,
 *    very often by DMA on the horizontal blank. So the top of the screen is drawn from
 *    the old data and the bottom from the new, and that is not a glitch: it is the whole
 *    technique. Composing from one end-of-frame snapshot paints every line with the
 *    FINAL state, which tears a band straight through the tilemap. The user saw exactly
 *    that ("une partie de la tilemap qui glitch") before any instrument here did.
 *
 * 2. IT WAS IN PYTHON, AND IT COST 13.5 ms OF A 16.67 ms FRAME.
 *    Measured on Metal Slug: emulation 0.75 ms, video-memory copy 0.91 ms, RENDER
 *    13.49 ms. The C++ core retires the whole machine in a twentieth of the time the
 *    shell took to colour it in. Any hiccup -- audio, GC, the scheduler -- dropped a
 *    frame, which is the other half of what the user reported ("des sauts d'image").
 *
 * ⚖️ THE PYTHON RENDERER STAYS, AS THE REFERENCE. It is the one with the citations, and
 * `tests/test_render_native.py` holds the two against each other pixel for pixel. A fast
 * renderer that quietly disagrees with the slow one is not an optimisation, it is a
 * second implementation of the machine -- and this project has exactly one.
 *
 * WHICH REGISTERS A LINE IS DRAWN WITH. The display block (0x8000..0x803F: scroll,
 * window, sprite offset, plane priority, 2D control) is taken from the RASTER SNAPSHOT
 * of this line -- the values standing as the line opened -- because the Tech Ref says a
 * write lands on the NEXT line. Everything else (palettes, the backdrop register, VRAM,
 * the OAM) is read LIVE, because that is what the beam sees.
 */
#include "machine.hpp"

namespace ngpc {

namespace {

/* 0BGR, 12 bits: low byte = GGGG RRRR, high byte = 0000 BBBB. */
inline uint16_t color_at(const Machine& m, uint32_t address) {
    return uint16_t((uint32_t(m.mem[address + 1]) << 8) | m.mem[address]);
}

constexpr uint32_t kPaletteSprite = 0x008200;
constexpr uint32_t kPaletteScr1   = 0x008280;
constexpr uint32_t kPaletteScr2   = 0x008300;
constexpr uint32_t kPaletteBg     = 0x0083E0;   /* backdrop (BGC), 8 entries */
/* ⛔ NOT the same block as the backdrop. This core used kPaletteBg for the
 * out-of-window fill too, and Fatal Fury's intro convicted it: the game fills
 * 0x83E0 with WHITE (its backdrop inside the window) and writes a grey RAMP at
 * 0x83F0 whose entry 7 is BLACK -- then sets OOWC=7. One block would make the
 * letterbox WHITE; the intended picture (and the reference map: HW_PAL_BG
 * 0x83E0 "couleur de fond", HW_PAL_WIN 0x83F0 "couleur hors-fenetre") is a
 * BLACK letterbox. A game does not build a ramp in a palette it never uses. */
constexpr uint32_t kPaletteOow    = 0x0083F0;   /* out-of-window, 8 entries */
constexpr uint32_t kOamBase       = 0x008800;
constexpr uint32_t kOamCpcBase    = 0x008C00;
constexpr uint32_t kScr1Map       = 0x009000;
constexpr uint32_t kScr2Map       = 0x009800;
constexpr uint32_t kCharRam       = 0x00A000;
constexpr uint32_t kBgcRegister   = 0x008118;
constexpr uint32_t kK1geMode      = 0x0087E2;

/* K1GE upper-palette-compatible mode: a 3-bit LEVEL look-up, then a 12-bit colour.
 * index = palette_code * 8 + level  (settled in pass 236 by the BIOS's own data). */
constexpr uint32_t kK1geLut[3]    = {0x008100, 0x008108, 0x008110};  /* spr, scr1, scr2 */
constexpr uint32_t kK1gePal[3]    = {0x008380, 0x0083A0, 0x0083C0};

/* One tile row: 8 left-to-right 2-bit values.
 *   odd byte  bits[7:6]=dot0 [5:4]=dot1 [3:2]=dot2 [1:0]=dot3
 *   even byte bits[7:6]=dot4 [5:4]=dot5 [3:2]=dot6 [1:0]=dot7          */
inline void tile_row(const Machine& m, unsigned tile, unsigned row, uint8_t out[8]) {
    const uint32_t base = kCharRam + tile * 16u + row * 2u;
    const uint8_t even = m.mem[base];
    const uint8_t odd  = m.mem[base + 1];
    out[0] = uint8_t((odd  >> 6) & 3); out[1] = uint8_t((odd  >> 4) & 3);
    out[2] = uint8_t((odd  >> 2) & 3); out[3] = uint8_t((odd  >> 0) & 3);
    out[4] = uint8_t((even >> 6) & 3); out[5] = uint8_t((even >> 4) & 3);
    out[6] = uint8_t((even >> 2) & 3); out[7] = uint8_t((even >> 0) & 3);
}

struct Regs {
    uint8_t wba_h, wba_v, wsi_h, wsi_v;
    uint8_t ctl2d;                  /* bit7 NEG, bits2..0 OOWC */
    uint8_t po_h, po_v;
    bool    scr2_in_front;          /* 0x8030 bit 7 */
    uint8_t s1so_h, s1so_v, s2so_h, s2so_v;
};

inline Regs regs_of_line(const Machine& m, uint32_t line) {
    const uint8_t* r = m.raster_log[line];   /* the 0x8000..0x803F block, as the line opened */
    Regs g;
    g.wba_h = r[0x02]; g.wba_v = r[0x03];
    g.wsi_h = r[0x04]; g.wsi_v = r[0x05];
    g.ctl2d = r[0x12];
    g.po_h  = r[0x20]; g.po_v  = r[0x21];
    g.scr2_in_front = (r[0x30] & 0x80) != 0;
    g.s1so_h = r[0x32]; g.s1so_v = r[0x33];
    g.s2so_h = r[0x34]; g.s2so_v = r[0x35];
    return g;
}

/* The colour a pixel value resolves to, for one plane and one palette code. */
struct PaletteView {
    bool     compat;
    uint32_t base;          /* K2GE: the plane's palette block. */
    uint32_t lut, cpal;     /* K1GE compat: the level LUT and the 12-bit palette. */
};

inline uint16_t resolve(const Machine& m, const PaletteView& pv,
                        unsigned code, unsigned value) {
    if (pv.compat) {
        /* Only the SINGLE P.C bit exists on the old machine: two palettes per plane. */
        const unsigned p_c = code & 1u;
        const unsigned level = m.mem[pv.lut + p_c * 4u + value] & 0x07u;
        return color_at(m, pv.cpal + (p_c * 8u + level) * 2u);
    }
    return color_at(m, pv.base + code * 8u + value * 2u);
}

}  // namespace

/* ⚡ ONE LINE OF THE PICTURE, drawn with what the machine holds RIGHT NOW.
 *
 * Back to front (Tech Ref Figure 4):
 *   backdrop · sprites PR.C=1 · back plane · sprites PR.C=2 · front plane · sprites
 *   PR.C=3 · window clip (out-of-window colour) · NEG invert.
 */
void Machine::render_scanline(uint32_t line) {
    if (line >= kVisibleScanlines) return;
    const Regs g = regs_of_line(*this, line);
    const bool compat = (mem[kK1geMode] & 0x80) != 0;

    uint16_t* row = &framebuffer[line * kScreenWidth];

    /* 1. THE BACKDROP. The Tech Ref reads "D7=1, D6=0 sets the BGC valid, other
     *    values set it not valid and the background colour is set to black"
     *    (4-6), and this core enforced that. But real games disagree: Ogre
     *    Battle Gaiden's intro writes a blue into 0x83E0[0], sets BGC = 0x00
     *    (D7=0), and expects a blue sky -- a black one would be a broken intro
     *    on the silicon it shipped on. The game is the authority over the manual
     *    here: the `(bgc & 0xC0) == 0x80` gate this core used to apply has to go.
     *    So the backdrop is the palette entry, unconditionally;
     *    a game that wants black simply leaves 0x83E0[index] black (the empty-
     *    memory cold start still resolves to 0, i.e. black). The enable bits do
     *    not gate the colour. */
    const uint8_t bgc = mem[kBgcRegister];
    const uint16_t backdrop = color_at(*this, kPaletteBg + (bgc & 0x07) * 2u);
    for (unsigned x = 0; x < kScreenWidth; ++x) row[x] = backdrop;

    /* --- the sprite line buffer -------------------------------------------------
     * Sprite 0 WINS. "During the write to the line buffer, the hardware checks the
     * priority [...] to avoid writing over previously written data" (Tech Ref
     * 4-3-3-1): a contested pixel belongs to the LOWEST OAM index. PR.C does not
     * decide who owns a pixel -- only where that pixel lands against the planes.
     *
     * The chain advances for EVERY entry, including hidden ones (PR.C = 0), so a
     * hidden anchor at the head of a group still positions its tail. */
    uint8_t  owner_value[kScreenWidth] = {};   /* 0 = unclaimed */
    uint16_t owner_color[kScreenWidth];
    uint8_t  owner_prc[kScreenWidth];

    const PaletteView spr_pv{compat, kPaletteSprite, kK1geLut[0], kK1gePal[0]};

    unsigned prev_h = 0, prev_v = 0;
    for (unsigned i = 0; i < 64; ++i) {
        const uint32_t o = kOamBase + i * 4u;
        const uint8_t attrib = mem[o + 1];
        const unsigned h_pos = mem[o + 2];
        const unsigned v_pos = mem[o + 3];
        const bool v_chain = (attrib >> 1) & 1u;
        const bool h_chain = (attrib >> 2) & 1u;

        const unsigned h = h_chain ? ((prev_h + h_pos) & 0xFFu) : h_pos;
        const unsigned v = v_chain ? ((prev_v + v_pos) & 0xFFu) : v_pos;
        prev_h = h; prev_v = v;

        const unsigned pr_c = (attrib >> 3) & 3u;
        if (pr_c == 0) continue;                       /* hidden -- but it anchored the chain */

        const unsigned screen_y = (v + g.po_v) & 0xFFu;
        /* ⚠️ The world is CYCLICAL: 256x256, of which 160x152 is shown (Tech Ref 3-1).
         * A sprite at y=249 hangs off the TOP and its last rows are ON screen. */
        const unsigned py = (line - screen_y) & 0xFFu;
        if (py >= 8) continue;                         /* this line misses the sprite */

        const unsigned screen_x = (h + g.po_h) & 0xFFu;
        const unsigned tile = (unsigned(attrib & 1u) << 8) | mem[o];
        const bool h_flip = (attrib >> 7) & 1u;
        const bool v_flip = (attrib >> 6) & 1u;
        const unsigned code = compat ? unsigned((attrib >> 5) & 1u)   /* P.C */
                                     : unsigned(mem[kOamCpcBase + i] & 0x0F);  /* CP.C */

        uint8_t px[8];
        tile_row(*this, tile, v_flip ? (7u - py) : py, px);

        for (unsigned i2 = 0; i2 < 8; ++i2) {
            const unsigned sx = (screen_x + i2) & 0xFFu;
            if (sx >= kScreenWidth) continue;
            if (owner_value[sx]) continue;             /* a lower OAM index took it */
            const unsigned value = px[h_flip ? (7u - i2) : i2];
            if (value == 0) continue;                  /* transparent: claims nothing */
            owner_value[sx] = 1;
            owner_color[sx] = resolve(*this, spr_pv, code, value);
            owner_prc[sx] = uint8_t(pr_c);
        }
    }

    /* 🔍 The debug layer mask (machine.hpp) gates COMPOSITION, never the line buffer
     * above: sprite 0 still wins its pixel whether or not its priority group is shown,
     * so hiding the front sprites reveals the SCROLL PLANE underneath -- not whatever
     * sprite lost the pixel. Anything else would be inventing an image the chip cannot
     * produce, and the point of this tool is to show what is really there. */
    auto blit_sprites = [&](unsigned want_prc) {
        if (!(layer_mask & (kLayerSprBack << (want_prc - 1u)))) return;
        for (unsigned x = 0; x < kScreenWidth; ++x)
            if (owner_value[x] && owner_prc[x] == want_prc) row[x] = owner_color[x];
    };

    auto draw_plane = [&](bool scr1) {
        if (!(layer_mask & (scr1 ? kLayerScr1 : kLayerScr2))) return;
        const uint32_t map  = scr1 ? kScr1Map : kScr2Map;
        const unsigned soh  = scr1 ? g.s1so_h : g.s2so_h;
        const unsigned sov  = scr1 ? g.s1so_v : g.s2so_v;
        const PaletteView pv{compat, scr1 ? kPaletteScr1 : kPaletteScr2,
                             kK1geLut[scr1 ? 1 : 2], kK1gePal[scr1 ? 1 : 2]};

        const unsigned wy = (line + sov) & 0xFFu;      /* the plane is 256x256, cyclical */
        const unsigned ty = wy >> 3;
        const unsigned py = wy & 7u;

        for (unsigned x = 0; x < kScreenWidth; ++x) {
            const unsigned wx = (x + soh) & 0xFFu;
            const uint32_t e = map + ((ty * 32u) + (wx >> 3)) * 2u;
            const uint8_t attrib = mem[e + 1];
            /* ⛔ NO "TILE 0 IS BLANK" RULE. Character 0 is 16 bytes of character RAM
             * like any other; transparency is per-PIXEL (value 0). See pass 242. */
            const unsigned tile = (unsigned(attrib & 1u) << 8) | mem[e];
            const bool h_flip = (attrib >> 7) & 1u;
            const bool v_flip = (attrib >> 6) & 1u;

            uint8_t px[8];
            tile_row(*this, tile, v_flip ? (7u - py) : py, px);
            const unsigned value = px[h_flip ? (7u - (wx & 7u)) : (wx & 7u)];
            if (value == 0) continue;                  /* transparent */

            const unsigned code = compat ? unsigned((attrib >> 5) & 1u)      /* P.C */
                                         : unsigned((attrib >> 1) & 0x0F);   /* CP.C */
            row[x] = resolve(*this, pv, code, value);
        }
    };

    blit_sprites(1);
    draw_plane(g.scr2_in_front);            /* back plane: SCR1 when SCR2 is in front */
    blit_sprites(2);
    draw_plane(!g.scr2_in_front);           /* front plane */
    blit_sprites(3);

    /* 7. OUTSIDE THE WINDOW. Half-open [WBA, WBA+WSI). Cold start is WBA=0, WSI=0xFF,
     *    which covers the whole screen -- so this is a no-op on a fresh reset.
     *    The fill colour comes from the WINDOW palette block (0x83F0), NOT the
     *    backdrop block -- see kPaletteOow above (Fatal Fury's black letterbox). */
    const uint16_t oowc = color_at(*this, kPaletteOow + (g.ctl2d & 0x07) * 2u);
    const unsigned y_in = (line >= g.wba_v) && (line < unsigned(g.wba_v) + g.wsi_v);
    for (unsigned x = 0; x < kScreenWidth; ++x) {
        const bool x_in = (x >= g.wba_h) && (x < unsigned(g.wba_h) + g.wsi_h);
        if (!y_in || !x_in) row[x] = oowc;
    }

    /* 8. NEG. Bit 7 of the 2D control inverts every component of every pixel the LCD
     *    receives -- the out-of-window fill included, which is why it runs last. */
    if (g.ctl2d & 0x80) {
        for (unsigned x = 0; x < kScreenWidth; ++x) {
            const uint16_t c = row[x];
            const uint16_t r = uint16_t((c & 0x0F) ^ 0x0F);
            const uint16_t gg = uint16_t(((c >> 4) & 0x0F) ^ 0x0F);
            const uint16_t b = uint16_t(((c >> 8) & 0x0F) ^ 0x0F);
            row[x] = uint16_t((b << 8) | (gg << 4) | r);
        }
    }
}

}  // namespace ngpc
