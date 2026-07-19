/* ============================================================================
 * NGPC CPU-SPEED CALIBRATION ROM  (v1, 2026-07-16)
 *
 * WHY: Cool Boarders Pocket / Densha de Go run 2x too fast on EVERY emulator
 * in every emulator measured, this one included, vs real hardware. They self-time their frame
 * rate; on silicon their per-frame work spills past one VBlank (-> 30 fps),
 * in emulators it fits (-> 60 fps). That means emulators execute the game's
 * work in fewer cycles than real hardware. This ROM MEASURES it, per
 * instruction class, using the VBlank as the reference clock.
 *
 * HOW TO READ IT: each line = how many 200-op batches finish in 60 frames
 * (~1 s). BIGGER number = CPU did more = faster per op. Compare the numbers on
 * REAL HARDWARE to the numbers the emulator prints:
 *   - a class that reads ~2.4x SMALLER on hardware is UNDER-costed in the
 *     emulator -> the bug.
 *   - all classes matching => CPU cycle counts are fine, the 2x is elsewhere.
 * RASV = max scanline seen (197 => 198 lines/frame ; 198 => 199 lines).
 *
 * Built with the OFFICIAL Toshiba cc900 toolchain. Clock gear = 0 (6.144 MHz).
 * ==========================================================================*/

#include "ngpc.h"
#include "carthdr.h"
#include "library.h"

#define RAS_V (*(volatile u8 *)0x8009)   /* current raster line, 0..198, wraps */
#define REPS  200                        /* ops per batch */
#define FRAMES 60                        /* measurement window (~1 s) */

/* Count how many REPS-op batches complete over FRAMES video frames.
 * Frame boundary = RAS_V wraps (current < previous). The per-batch overhead is
 * identical for every test, so the DIFFERENCES isolate each op's relative cost. */
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

MEASURE(m_base,  v = w)                 /* baseline: bare loop + reg move */
MEASURE(m_shift, v = w << 5)            /* word shift (Cool Boarders hot op) */
MEASURE(m_add,   v = v + w)             /* reg-reg add */
MEASURE(m_mul,   v = v * w)             /* multiply */
MEASURE(m_div,   v = w / (v | 1))       /* divide (|1 avoids div-by-0) */
MEASURE(m_mem,   *(volatile u8 *)0x4200 = (u8)v)  /* RAM write */

/* Max scanline seen over a long sample -> settles 198 vs 199 lines/frame. */
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
    CpuSpeed(0);                        /* full clock, gear 0, like the games */

    PrintString(P, PAL, 1, 1,  "CPU CALIB batch/60f");
    PrintString(P, PAL, 1, 3,  "BASE :");
    PrintString(P, PAL, 1, 4,  "SHIFT:");
    PrintString(P, PAL, 1, 5,  "ADD  :");
    PrintString(P, PAL, 1, 6,  "MUL  :");
    PrintString(P, PAL, 1, 7,  "DIV  :");
    PrintString(P, PAL, 1, 8,  "MEM  :");
    PrintString(P, PAL, 1, 10, "RASV :");
    PrintString(P, PAL, 1, 12, "read hw vs emu");

    while (1) {
        PrintDecimal(P, PAL, 12, 3, m_base(),  5);
        PrintDecimal(P, PAL, 12, 4, m_shift(), 5);
        PrintDecimal(P, PAL, 12, 5, m_add(),   5);
        PrintDecimal(P, PAL, 12, 6, m_mul(),   5);
        PrintDecimal(P, PAL, 12, 7, m_div(),   5);
        PrintDecimal(P, PAL, 12, 8, m_mem(),   5);
        PrintDecimal(P, PAL, 12, 10, rasv_max(), 3);
    }
}
