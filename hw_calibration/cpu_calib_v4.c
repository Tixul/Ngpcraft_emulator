/* ============================================================================
 * NGPC CPU-SPEED CALIBRATION ROM  (v4, 2026-07-16)
 *
 * The story so far (all on silicon):
 *   v1/v2 -> instruction FETCH from cart is wait-stated (cart_wait=3, CONFIRMED);
 *            cart DATA reads are NOT (CRND==RRND); MUL/DIV were under-costed (fixed).
 *   v3    -> VRAM writes ARE throttled in the active drawing period (VWR<MEM),
 *            but Cool Boarders writes VRAM in vblank, so that is not its bottleneck.
 * With a silicon-exact CPU model Cool Boarders STILL runs ~51fps vs 30 on silicon.
 * The one thing left: it does a big per-frame **LDIR** (block copy, ~thousands of
 * bytes, to RAM) that the calib never measured. Our LDIR costs 7 cycles/byte
 * (Toshiba datasheet) -- and the datasheet MUL/DIV figures already proved to be
 * FLOORS. If real LDIR is ~2x that, Cool Boarders lands at 30fps (tested) while
 * Fatal Fury stays at 60 (tested). v4 MEASURES the LDIR cost directly.
 *
 * NEW TESTS (guaranteed LDIR via inline asm; a C copy loop would not compile to one):
 *   LDRR : LDIR of 64 bytes RAM->RAM  per rep  -> the pure block-copy cost/byte
 *   LDVR : LDIR of 64 bytes RAM->VRAM per rep  -> LDRR + any VRAM-write throttle
 * Emulator now (LDIR=7): LDRR and LDVR print the SAME large number. On silicon:
 *   LDRR much SMALLER than the emulator's LDRR -> LDIR is under-costed; the ratio is
 *     the real cycles/byte (7 * emu/silicon). This is the Cool Boarders bug.
 *   LDVR < LDRR -> writing the block to VRAM costs extra on top (the v3 throttle).
 *
 * Built with the OFFICIAL Toshiba cc900 toolchain. Clock gear = 0 (6.144 MHz).
 * ==========================================================================*/

#include "ngpc.h"
#include "carthdr.h"
#include "library.h"

#define RAS_V (*(volatile u8 *)0x8009)
#define REPS  200
#define FRAMES 60

#define MEASURE(NAME, OP)                                    \
u16 NAME(void) {                                             \
    u16 count; u8 frames; u8 prev, cur; u16 i;               \
    volatile u16 v; volatile u16 w;                          \
    count = 0; frames = 0; v = 1; w = 3;                     \
    prev = RAS_V;                                            \
    while (frames < FRAMES) {                                \
        for (i = 0; i < REPS; i++) { OP; }                   \
        count++;                                             \
        cur = RAS_V;                                         \
        if (cur < prev) frames++;                            \
        prev = cur;                                          \
    }                                                        \
    return count;                                            \
}

MEASURE(m_base,  v = w)
MEASURE(m_shift, v = w << 5)
MEASURE(m_add,   v = v + w)
MEASURE(m_mul,   v = v * w)
MEASURE(m_div,   v = w / (v | 1))
MEASURE(m_mem,   *(volatile u8 *)0x4200 = (u8)v)
MEASURE(m_vram,  *(volatile u8 *)0xBE00 = (u8)v)

/* One 64-byte LDIR, dest given by the (xde) setup. We PUSH/POP the three registers
 * LDIR touches (XHL src, XDE dst, BC count) so the C loop's variables survive. The
 * batch loop spans active + vblank lines, so a VRAM-dest LDIR sees the average throttle. */
#define LDIR64(DST_HEX)                                      \
    __asm(" push xhl"); __asm(" push xde"); __asm(" push bc"); \
    __asm(" ld xhl,0x4900");                                  \
    __asm(" ld xde," DST_HEX);                                \
    __asm(" ld bc,64");                                       \
    __asm(" ldirb (xde+),(xhl+)");                            \
    __asm(" pop bc"); __asm(" pop xde"); __asm(" pop xhl")

u16 m_ldir_ram(void) {
    u16 count; u8 frames; u8 prev, cur; u16 i;
    count = 0; frames = 0; prev = RAS_V;
    while (frames < FRAMES) {
        for (i = 0; i < REPS; i++) { LDIR64("0x4d00"); }   /* RAM -> RAM */
        count++; cur = RAS_V; if (cur < prev) frames++; prev = cur;
    }
    return count;
}

u16 m_ldir_vram(void) {
    u16 count; u8 frames; u8 prev, cur; u16 i;
    count = 0; frames = 0; prev = RAS_V;
    while (frames < FRAMES) {
        for (i = 0; i < REPS; i++) { LDIR64("0xbc00"); }   /* RAM -> VRAM (char RAM) */
        count++; cur = RAS_V; if (cur < prev) frames++; prev = cur;
    }
    return count;
}

u8 rasv_max(void) {
    u8 mx; u8 r; u16 s;
    mx = 0;
    for (s = 0; s < 40000; s++) { r = RAS_V; if (r > mx && r < 250) mx = r; }
    return mx;
}

#define PAL 0
#define P   SCR_1_PLANE

void main(void) {
    InitNGPC();
    SetBackgroundColour(RGB(2, 2, 4));
    SysSetSystemFont();
    SetPalette(P, PAL, 4, RGB(15, 15, 15), RGB(15, 15, 15), RGB(15, 15, 15));
    CpuSpeed(0);

    PrintString(P, PAL, 1, 1,  "CPU CALIB v4 /60f");
    PrintString(P, PAL, 1, 3,  "BASE :");
    PrintString(P, PAL, 1, 4,  "SHIFT:");
    PrintString(P, PAL, 1, 5,  "ADD  :");
    PrintString(P, PAL, 1, 6,  "MUL  :");
    PrintString(P, PAL, 1, 7,  "DIV  :");
    PrintString(P, PAL, 1, 8,  "MEM  :");
    PrintString(P, PAL, 1, 9,  "VWR  :");
    PrintString(P, PAL, 1, 10, "LDRR :");
    PrintString(P, PAL, 1, 11, "LDVR :");
    PrintString(P, PAL, 1, 13, "RASV :");
    PrintString(P, PAL, 1, 15, "read hw vs emu");

    while (1) {
        PrintDecimal(P, PAL, 12, 3,  m_base(),      5);
        PrintDecimal(P, PAL, 12, 4,  m_shift(),     5);
        PrintDecimal(P, PAL, 12, 5,  m_add(),       5);
        PrintDecimal(P, PAL, 12, 6,  m_mul(),       5);
        PrintDecimal(P, PAL, 12, 7,  m_div(),       5);
        PrintDecimal(P, PAL, 12, 8,  m_mem(),       5);
        PrintDecimal(P, PAL, 12, 9,  m_vram(),      5);
        PrintDecimal(P, PAL, 12, 10, m_ldir_ram(),  5);
        PrintDecimal(P, PAL, 12, 11, m_ldir_vram(), 5);
        PrintDecimal(P, PAL, 12, 13, rasv_max(),    3);
    }
}
