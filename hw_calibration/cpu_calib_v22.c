/*
 * main.c -- HW TEST RAS.H: WHAT DOES THE HORIZONTAL RASTER COUNTER READ?
 *
 * Arbitrates emulator PR 31, which makes RAS.H (0x8008) read
 *     (515 - position_in_line) >> 2      i.e. 0x80 at line start, down to 0x00.
 * K2GE Tech Ref 4-8: "the 10 bit internal subtraction counter of the horizontal
 * drawing operation time (internally 515 clock) is read in to RAS.H".
 *
 * Every pass, interrupts masked, the ROM reads the word at 0x8008 (low byte =
 * RAS.H, high byte = RAS.V) 1024 times in a row, twice:
 *     test A  a plain loop          (one read per loop turn)
 *     test B  8 reads per loop turn (reads closer together)
 * then prints a summary per test and holds it 2 s. Row 0 counts the passes.
 *
 * Results also stay in RAM for a probe: samples of B at 0x4800 (1024 words),
 * summaries at 0x5400 (A) and 0x5420 (B), layout R_* below.
 *
 * Only the 0..F hex font is used (A and B double as the test labels).
 */

typedef unsigned char  u8;
typedef unsigned short u16;
typedef unsigned long  u32;

#include "hexfont.h"

#define HW_LCD_CTL   (*(volatile u8  *)0x00008012)
#define HW_WIN_X     (*(volatile u8  *)0x00008002)
#define HW_WIN_Y     (*(volatile u8  *)0x00008003)
#define HW_WIN_W     (*(volatile u8  *)0x00008004)
#define HW_WIN_H     (*(volatile u8  *)0x00008005)
#define HW_SCR1_OFSX (*(volatile u8  *)0x00008032)
#define HW_SCR1_OFSY (*(volatile u8  *)0x00008033)
#define HW_SCR2_OFSX (*(volatile u8  *)0x00008034)
#define HW_SCR2_OFSY (*(volatile u8  *)0x00008035)
#define HW_BG_CTL    (*(volatile u8  *)0x00008118)
#define K1_S1PLT     ((volatile u8  *)0x00008108)
#define K2_SCR1_PAL0 ((volatile u16 *)0x00008280)
#define K2_BG_PAL0   (*(volatile u16 *)0x000083E0)

#define HW_WATCHDOG  (*(volatile u8  *)0x0000006F)
#define WDOG_KICK    0x4E
#define HW_USR_SHUT  (*(volatile u8  *)0x00006F85)
#define HW_JOYPAD    (*(volatile u8  *)0x00006F82)
#define PAD_POWER    0x80
#define VEC_VBLANK   (*(volatile u32 *)0x00006FCC)

#define SCR1_MAP     ((volatile u16 *)0x00009000)
#define SCR2_MAP     ((volatile u16 *)0x00009800)
#define CHAR_RAM     ((volatile u8  *)0x0000A000)

#define RAS16        (*(volatile u16 *)0x00008008)   /* lo = RAS.H, hi = RAS.V */

#define SAMP         ((volatile u16 *)0x00004800)    /* 1024 samples */
#define NSAMP        1024u
#define HIST         ((volatile u16 *)0x00005000)    /* 256 bins */
#define RES          ((volatile u16 *)0x00005400)    /* 16 words per test */

/* summary slots, per test */
#define R_HMIN   0
#define R_HMAX   1
#define R_VMAX   2
#define R_DOWN   3    /* same line, RAS.H went down  */
#define R_UP     4    /* same line, RAS.H went up    */
#define R_MODE   5    /* most frequent step (prev - cur) on one line */
#define R_MCNT   6    /* how many times it occurred  */
#define R_HB     7    /* first line change: RAS.H just before ... */
#define R_HA     8    /* ... and just after          */
#define R_VB     9
#define R_VA     10
#define R_JUMP   11   /* line changes that skipped a line (other than the wrap to 0) */
#define R_SAME   12   /* same line, RAS.H unchanged  */

#define TILE_A   11u
#define TILE_B   12u

static volatile u16 g_vbl;
static volatile u16 g_pass;

static void __interrupt vbl_isr(void);
static void power_off(void);
static void sample_rolled(void);
static void sample_unrolled(void);
static void analyze(u16 r);
static void draw_test(u16 row, u16 label, u16 r);
static void put_hex16(u16 cell, u16 v);
static void put_hex8(u16 cell, u16 v);

void main(void)
{
    u16 i, hold_pwr;

    for (i = 0; i < 0x400; i++) {
        SCR1_MAP[i] = 0;
        SCR2_MAP[i] = 0;
        if ((i & 0x3F) == 0) { HW_WATCHDOG = WDOG_KICK; }
    }
    for (i = 0; i < (u16)272; i++) { CHAR_RAM[i] = HEXFONT[i]; }

    K1_S1PLT[1] = 7; K1_S1PLT[2] = 7; K1_S1PLT[3] = 7;
    K2_SCR1_PAL0[0] = 0x0FFF; K2_SCR1_PAL0[1] = 0x0000;
    K2_SCR1_PAL0[2] = 0x0000; K2_SCR1_PAL0[3] = 0x0000;
    K2_BG_PAL0 = 0x0FFF;
    HW_BG_CTL  = 0x80;
    HW_WIN_X = 0; HW_WIN_Y = 0; HW_WIN_W = 160; HW_WIN_H = 152;
    HW_SCR1_OFSX = 0; HW_SCR1_OFSY = 0; HW_SCR2_OFSX = 0; HW_SCR2_OFSY = 0;
    HW_LCD_CTL = 0x00;

    /* RAM is NOT zeroed at power-on: set every global before use. */
    g_vbl = 0;
    g_pass = 0;
    VEC_VBLANK = (u32)vbl_isr;

    for (;;) {
        __asm(" db 0x06, 0x07");                   /* di: nothing between reads */
        HW_WATCHDOG = WDOG_KICK;
        sample_rolled();
        HW_WATCHDOG = WDOG_KICK;
        analyze(0);
        HW_WATCHDOG = WDOG_KICK;
        sample_unrolled();
        HW_WATCHDOG = WDOG_KICK;
        analyze(16);
        __asm(" db 0x06, 0x00");                   /* ei 0: VBlank + POWER alive */

        g_pass++;
        put_hex16(0 * 32 + 1, g_pass);
        draw_test(2, TILE_A, 0);
        draw_test(7, TILE_B, 16);
        /* rows 13-14: the first 8 raw samples of B (VVHH) */
        put_hex16(13 * 32 + 1,  SAMP[0]); put_hex16(13 * 32 + 6,  SAMP[1]);
        put_hex16(13 * 32 + 11, SAMP[2]); put_hex16(13 * 32 + 16, SAMP[3]);
        put_hex16(14 * 32 + 1,  SAMP[4]); put_hex16(14 * 32 + 6,  SAMP[5]);
        put_hex16(14 * 32 + 11, SAMP[6]); put_hex16(14 * 32 + 16, SAMP[7]);

        g_vbl = 0;
        hold_pwr = 0;
        while (g_vbl < 120) {
            HW_WATCHDOG = WDOG_KICK;
            if (HW_USR_SHUT) { power_off(); }
            if (HW_JOYPAD & PAD_POWER) {
                if (hold_pwr < 60) { hold_pwr++; }
                if (hold_pwr >= 20) { power_off(); }
            } else {
                hold_pwr = 0;
            }
        }
    }
}

static void __interrupt vbl_isr(void)
{
    HW_WATCHDOG = WDOG_KICK;
    g_vbl++;
}

static void power_off(void)
{
    HW_USR_SHUT = 0x01;
    for (;;) { HW_WATCHDOG = WDOG_KICK; }
}

static void sample_rolled(void)
{
    u16 i;
    for (i = 0; i < NSAMP; i++) { SAMP[i] = RAS16; }
}

static void sample_unrolled(void)
{
    volatile u16 *p;
    u16 i;
    p = SAMP;
    for (i = 0; i < (u16)(NSAMP / 8); i++) {
        p[0] = RAS16; p[1] = RAS16; p[2] = RAS16; p[3] = RAS16;
        p[4] = RAS16; p[5] = RAS16; p[6] = RAS16; p[7] = RAS16;
        p += 8;
    }
}

/* One flat function, results through RES[r + R_*] (cc900 mangles nested args). */
static void analyze(u16 r)
{
    u16 i, cur, prev, h, ph, v, pv, d, best, bestn, seen;
    u16 hmin, hmax, vmax, down, up, same, jump;

    for (i = 0; i < 256; i++) { HIST[i] = 0; }
    hmin = 0xFFFF; hmax = 0; vmax = 0; down = 0; up = 0; same = 0; jump = 0;
    seen = 0;
    RES[r + R_HB] = 0xFFFF; RES[r + R_HA] = 0xFFFF;
    RES[r + R_VB] = 0xFFFF; RES[r + R_VA] = 0xFFFF;

    prev = SAMP[0];
    for (i = 0; i < NSAMP; i++) {
        cur = SAMP[i];
        h = (u16)(cur & 0xFF);
        v = (u16)(cur >> 8);
        if (h < hmin) { hmin = h; }
        if (h > hmax) { hmax = h; }
        if (v > vmax) { vmax = v; }
        if (i != 0) {
            ph = (u16)(prev & 0xFF);
            pv = (u16)(prev >> 8);
            if (v == pv) {
                if (h < ph) { down++; } else if (h > ph) { up++; } else { same++; }
                d = (u16)((u16)(ph - h) & 0xFF);
                HIST[d]++;
            } else {
                if (!seen) {
                    RES[r + R_HB] = ph; RES[r + R_HA] = h;
                    RES[r + R_VB] = pv; RES[r + R_VA] = v;
                    seen = 1;
                }
                if (v != (u16)(pv + 1) && v != 0) { jump++; }
            }
        }
        prev = cur;
        if ((i & 0xFF) == 0) { HW_WATCHDOG = WDOG_KICK; }
    }

    best = 0; bestn = 0;
    for (d = 0; d < 256; d++) {
        if (HIST[d] > bestn) { bestn = HIST[d]; best = d; }
    }

    RES[r + R_HMIN] = hmin; RES[r + R_HMAX] = hmax; RES[r + R_VMAX] = vmax;
    RES[r + R_DOWN] = down; RES[r + R_UP] = up; RES[r + R_SAME] = same;
    RES[r + R_MODE] = best; RES[r + R_MCNT] = bestn; RES[r + R_JUMP] = jump;
}

/* Rows (see README):
 *   row+0  L  HMIN HMAX VMAX  JUMP
 *   row+1     DOWN  UP   SAME
 *   row+2     MODE  MCNT
 *   row+3     HB HA  VB VA          (first line change) */
static void draw_test(u16 row, u16 label, u16 r)
{
    u16 c;
    c = (u16)(row * 32);
    SCR1_MAP[c + 1] = label;
    put_hex8(c + 3, RES[r + R_HMIN]);
    put_hex8(c + 6, RES[r + R_HMAX]);
    put_hex8(c + 9, RES[r + R_VMAX]);
    put_hex16(c + 12, RES[r + R_JUMP]);
    c = (u16)(c + 32);
    put_hex16(c + 3, RES[r + R_DOWN]);
    put_hex16(c + 8, RES[r + R_UP]);
    put_hex16(c + 13, RES[r + R_SAME]);
    c = (u16)(c + 32);
    put_hex8(c + 3, RES[r + R_MODE]);
    put_hex16(c + 6, RES[r + R_MCNT]);
    c = (u16)(c + 32);
    put_hex8(c + 3, RES[r + R_HB]);
    put_hex8(c + 6, RES[r + R_HA]);
    put_hex8(c + 10, RES[r + R_VB]);
    put_hex8(c + 13, RES[r + R_VA]);
}

static void put_hex16(u16 cell, u16 v)
{
    u16 i;
    for (i = 0; i < 4; i++) {
        SCR1_MAP[cell + i] = (u16)(1 + ((v >> ((3 - i) << 2)) & 0x0F));
    }
}

static void put_hex8(u16 cell, u16 v)
{
    SCR1_MAP[cell]     = (u16)(1 + ((v >> 4) & 0x0F));
    SCR1_MAP[cell + 1] = (u16)(1 + (v & 0x0F));
}
