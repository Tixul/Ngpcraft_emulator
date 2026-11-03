/*
 * main.c -- HW TEST: WHAT DOES ONE MEMORY ACCESS COST, PER REGION?
 *
 * hw_test_rash (03/11) showed our core running read loops 5-10 % too fast. This
 * ROM finds WHICH access is under-charged. The clock is the raster itself
 * (RAS.V + RAS.H, validated on the console by hw_test_rash): one line is 515
 * clocks, RAS.H counts down 4 clocks per step.
 *
 * Each test runs one access 8 times per loop turn, 64 turns, then the same loop
 * with the access 16 times per turn. The loop's own cost is identical in both,
 * so (T16 - T8) is exactly 512 accesses:  cost = (T16 - T8) / 512 clocks.
 * Every run starts on line 0 (active display), interrupts masked.
 *
 *   1  read  word  K2GE RAS (0x8008)        7  write byte RAM
 *   2  read  byte  K2GE RAS (0x8008)        8  read  word VRAM (0x9F00)
 *   3  read  word  K2GE PO.H/V (0x8020)     9  read  word cartridge ROM
 *   4  read  word  RAM (0x5F00)             A  read  word CPU I/O (0x0020)
 *   5  read  byte  RAM                      B  write word K2GE PO.H/V
 *   6  write word  RAM
 *
 * Screen, one row per test:   id  T8  T16  cost (clocks, 2 decimals)
 * T8/T16 in decimal clocks. Row 0 counts the passes (new pass every ~3 s).
 * Results also in RAM: T8 at 0x5400, T16 at 0x5420 (u16, index = test id).
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
#define RAS_V        (*(volatile u8  *)0x00008009)

/* the accessed locations */
#define K2_RAS_W     (*(volatile u16 *)0x00008008)
#define K2_RAS_B     (*(volatile u8  *)0x00008008)
#define K2_PO_W      (*(volatile u16 *)0x00008020)
#define RAM_W        (*(volatile u16 *)0x00005F00)
#define RAM_B        (*(volatile u8  *)0x00005F00)
#define VRAM_W       (*(volatile u16 *)0x00009F00)
#define ROM_W        (*(volatile u16 *)0x00200020)
#define IO_W         (*(volatile u16 *)0x00000020)

#define RES8         ((volatile u16 *)0x00005400)
#define RES16        ((volatile u16 *)0x00005420)
#define NTEST        12u

#define TILE_DOT     17u

#define OP8(S)   S; S; S; S; S; S; S; S;
#define TURNS    64u

/* One test = the same loop with 8, then 16, copies of the access. */
#define MEASURE(id, S)                                             \
    sync_line0(); t0 = RAS16;                                      \
    for (i = 0; i < TURNS; i++) { OP8(S) }                         \
    t1 = RAS16; RES8[id] = elapsed(t0, t1);                        \
    sync_line0(); t0 = RAS16;                                      \
    for (i = 0; i < TURNS; i++) { OP8(S) OP8(S) }                  \
    t1 = RAS16; RES16[id] = elapsed(t0, t1);

static volatile u16 g_vbl;
static volatile u16 g_pass;

static void __interrupt vbl_isr(void);
static void power_off(void);
static void measure_all(void);
static void sync_line0(void);
static u16  elapsed(u16 a, u16 b);
static void draw_row(u16 id);
static void put_dec5(u16 cell, u16 v);

void main(void)
{
    u16 i, hold_pwr;

    for (i = 0; i < 0x400; i++) {
        SCR1_MAP[i] = 0;
        SCR2_MAP[i] = 0;
        if ((i & 0x3F) == 0) { HW_WATCHDOG = WDOG_KICK; }
    }
    for (i = 0; i < (u16)288; i++) { CHAR_RAM[i] = HEXFONT[i]; }

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
    RAM_W = 0;                                     /* read by tests 4-5 */
    VEC_VBLANK = (u32)vbl_isr;

    for (;;) {
        __asm(" db 0x06, 0x07");                   /* di */
        measure_all();
        __asm(" db 0x06, 0x00");                   /* ei 0: VBlank + POWER alive */

        g_pass++;
        put_dec5(0 * 32 + 1, g_pass);
        for (i = 1; i < NTEST; i++) { draw_row(i); }

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

static void measure_all(void)
{
    u16 i, t0, t1, x;
    u8  y;

    x = 0; y = 0;
    MEASURE(1, x = K2_RAS_W)
    MEASURE(2, y = K2_RAS_B)
    MEASURE(3, x = K2_PO_W)
    MEASURE(4, x = RAM_W)
    MEASURE(5, y = RAM_B)
    MEASURE(6, RAM_W = 0)
    MEASURE(7, RAM_B = 0)
    MEASURE(8, x = VRAM_W)
    MEASURE(9, x = ROM_W)
    MEASURE(10, x = IO_W)
    MEASURE(11, K2_PO_W = 0)
    RAM_W = (u16)(x + y);                          /* keep x and y alive */
}

/* Wait for the START of line 0: leave the last line first. */
static void sync_line0(void)
{
    HW_WATCHDOG = WDOG_KICK;
    while (RAS_V != 0xC6) { }
    while (RAS_V != 0x00) { }
}

/* Clocks between two raster samples (VVHH). The frame is 199 lines of 515
 * clocks; RAS.H counts down one step per 4 clocks. Valid below 127 lines. */
static u16 elapsed(u16 a, u16 b)
{
    u16 va, ha, vb, hb, dv;
    va = (u16)(a >> 8); ha = (u16)(a & 0xFF);
    vb = (u16)(b >> 8); hb = (u16)(b & 0xFF);
    if (vb >= va) { dv = (u16)(vb - va); } else { dv = (u16)(vb + 199 - va); }
    return (u16)(dv * 515 + ha * 4 - hb * 4);
}

/* row = 1 + id:   id  T8  T16  cost   (cost = (T16 - T8) / 512, as "cc.dd") */
static void draw_row(u16 id)
{
    u16 c, d, cost, t8, t16;
    c = (u16)((id + 1) * 32);
    t8 = RES8[id];
    t16 = RES16[id];
    SCR1_MAP[c] = (u16)(1 + id);
    put_dec5(c + 2, t8);
    put_dec5(c + 8, t16);
    d = (u16)(t16 - t8);
    /* d * 100 / 512 without overflowing 16 bits: d * 25 / 128 */
    cost = (u16)((d >> 7) * 25 + ((d & 0x7F) * 25) / 128);
    SCR1_MAP[c + 14] = (u16)(1 + (cost / 1000) % 10);
    SCR1_MAP[c + 15] = (u16)(1 + (cost / 100) % 10);
    SCR1_MAP[c + 16] = TILE_DOT;
    SCR1_MAP[c + 17] = (u16)(1 + (cost / 10) % 10);
    SCR1_MAP[c + 18] = (u16)(1 + cost % 10);
}

static void put_dec5(u16 cell, u16 v)
{
    SCR1_MAP[cell]     = (u16)(1 + (v / 10000) % 10);
    SCR1_MAP[cell + 1] = (u16)(1 + (v / 1000) % 10);
    SCR1_MAP[cell + 2] = (u16)(1 + (v / 100) % 10);
    SCR1_MAP[cell + 3] = (u16)(1 + (v / 10) % 10);
    SCR1_MAP[cell + 4] = (u16)(1 + v % 10);
}
