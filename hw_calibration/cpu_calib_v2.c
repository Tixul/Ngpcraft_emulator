/* ============================================================================
 * NGPC CPU-SPEED CALIBRATION ROM  (v2, 2026-07-16)
 *
 * v1 PROVED the "2x too fast" bug is unmodelled CARTRIDGE-FLASH wait-states:
 * fetch-bound ops read ~3.4x too fast on emulators vs silicon, MUL/DIV ~2.5x,
 * frame length (RASV) correct. The fix is a two-parameter cart-flash model
 * (cart_wait = per-fetch-byte, cart_data_wait = per-data-byte). cart_wait=3 is
 * pinned by v1's register loops; cart_data_wait=5 was pinned only INDIRECTLY,
 * by Cool Boarders' 30fps. v2 measures the DATA-read cost DIRECTLY so silicon
 * can confirm (or correct) cart_data_wait on its own.
 *
 * NEW TESTS (read one byte per rep, same index arithmetic, only the source
 * differs -> the DIFFERENCES isolate the cart-data penalty):
 *   CSEQ : read cart flash SEQUENTIALLY  (stride 1)   -> page-mode / sequential
 *   CRND : read cart flash with a stride (277)         -> random-access latency
 *   RRND : same stride read from RAM (fast) = control  -> emu and hw must match
 * Reads use an ABSOLUTE pointer to 0x200000 (cart) / 0x004800 (RAM) so the
 * result does not depend on where the linker puts a const array.
 *
 * READING IT: each line = how many REPS-op batches finish in 60 frames (~1 s).
 *   CRND much SMALLER on hardware than emu  -> cart data reads are under-costed
 *     (raise cart_data_wait until CRND matches).
 *   CSEQ > CRND on hardware                 -> sequential cart reads are cheaper
 *     than random (flash page-mode) -> the model may want a 3rd (sequential-data)
 *     parameter; if CSEQ == CRND, the single cart_data_wait is correct.
 *   RRND matches emu<->hw                    -> control OK (RAM is not wait-stated).
 *
 * Built with the OFFICIAL Toshiba cc900 toolchain. Clock gear = 0 (6.144 MHz).
 * ==========================================================================*/

#include "ngpc.h"
#include "carthdr.h"
#include "library.h"

#define RAS_V (*(volatile u8 *)0x8009)   /* current raster line, 0..198, wraps */
#define REPS  200                        /* ops per batch */
#define FRAMES 60                        /* measurement window (~1 s) */

/* ---- v1 instruction-class tests (unchanged, for cross-check) -------------- */
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

/* ---- v2 data-read tests: one byte per rep from a chosen source ----------- *
 * base[idx] with base a byte pointer and idx a u16 keeps the address math on
 * the 16-bit index (base is the 24-bit constant), avoiding cc900 long-arith
 * quirks. The per-rep overhead (idx update + volatile add) is identical for
 * all three, so CRND-vs-RRND isolates the cart penalty and CSEQ-vs-CRND the
 * sequential-vs-random flash cost. */
#define MEASURE_TBL(NAME, BASEADDR, STRIDE, MASK)                    \
u16 NAME(void) {                                                     \
    u16 count; u8 frames; u8 prev, cur; u16 i;                       \
    volatile u16 acc; u16 idx;                                       \
    volatile const u8 *base = (volatile const u8 *)(BASEADDR);       \
    count = 0; frames = 0; acc = 0; idx = 0;                         \
    prev = RAS_V;                                                    \
    while (frames < FRAMES) {                                        \
        for (i = 0; i < REPS; i++) {                                 \
            idx = (u16)((idx + (STRIDE)) & (MASK));                  \
            acc = (u16)(acc + base[idx]);                            \
        }                                                            \
        count++;                                                     \
        cur = RAS_V;                                                 \
        if (cur < prev) frames++;                                    \
        prev = cur;                                                  \
    }                                                                \
    return count;                                                    \
}

MEASURE_TBL(m_cart_seq, 0x200000uL, 1,   0x0FFF)  /* sequential cart flash read */
MEASURE_TBL(m_cart_rnd, 0x200000uL, 277, 0x0FFF)  /* strided (random) cart read */
MEASURE_TBL(m_ram_rnd,  0x004800uL, 277, 0x07FF)  /* strided RAM read (control) */

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

    PrintString(P, PAL, 1, 1,  "CPU CALIB v2 /60f");
    PrintString(P, PAL, 1, 3,  "BASE :");
    PrintString(P, PAL, 1, 4,  "SHIFT:");
    PrintString(P, PAL, 1, 5,  "ADD  :");
    PrintString(P, PAL, 1, 6,  "MUL  :");
    PrintString(P, PAL, 1, 7,  "DIV  :");
    PrintString(P, PAL, 1, 8,  "MEM  :");
    PrintString(P, PAL, 1, 9,  "CSEQ :");
    PrintString(P, PAL, 1, 10, "CRND :");
    PrintString(P, PAL, 1, 11, "RRND :");
    PrintString(P, PAL, 1, 13, "RASV :");
    PrintString(P, PAL, 1, 15, "read hw vs emu");

    while (1) {
        PrintDecimal(P, PAL, 12, 3,  m_base(),     5);
        PrintDecimal(P, PAL, 12, 4,  m_shift(),    5);
        PrintDecimal(P, PAL, 12, 5,  m_add(),      5);
        PrintDecimal(P, PAL, 12, 6,  m_mul(),      5);
        PrintDecimal(P, PAL, 12, 7,  m_div(),      5);
        PrintDecimal(P, PAL, 12, 8,  m_mem(),      5);
        PrintDecimal(P, PAL, 12, 9,  m_cart_seq(), 5);
        PrintDecimal(P, PAL, 12, 10, m_cart_rnd(), 5);
        PrintDecimal(P, PAL, 12, 11, m_ram_rnd(),  5);
        PrintDecimal(P, PAL, 12, 13, rasv_max(),   3);
    }
}
