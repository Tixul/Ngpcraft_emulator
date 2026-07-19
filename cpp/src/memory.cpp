/* memory.cpp — flat address space + documented power-on state.
 *
 * The Python core reads memory through three layers (_RuntimeOverlayDecodeBus
 * -> NgpcReadBus -> probe) and allocates an AddressProbe per byte: 113 861
 * read_bytes calls per 16 000 instructions (PERF_TIMING_POLICY.md §10.2).
 * Here the whole 24-bit space is one flat array and a read is an array index.
 *
 * Every power-on value below is transcribed from core/memory.py, which cites
 * its own sources (TMP95C061 datasheet, NGPC_HW_QUICKREF.md §5, and the
 * NeoPop reset table). Do not "tidy" these values: a wrong one boots a
 * plausible-but-wrong machine, which is worse than not booting.
 */
#include <cstring>

#include "machine.hpp"

namespace ngpc {

/* TMP95C061 on-chip I/O page (0x000000..0x0000FF). These registers do NOT
 * reset to zero. Mirrors core/memory.py:_IO_PAGE_RESET_VALUES exactly. */
static const uint8_t kIoPageReset[0x100] = {
    /* 0x00 */ 0x00,0x00,0xFF,0xFF,0xFF,0xFF,0xFF,0xFF, 0xFF,0xFF,0xFF,0xFF,0xFF,0x08,0xFF,0xFF,
    /* 0x10 */ 0x34,0x3C,0xFF,0xFF,0xFF,0x3F,0x00,0x00, 0x3F,0xFF,0x2D,0x01,0xFF,0xFF,0x03,0xB2,
    /* 0x20 */ 0x80,0x00,0x01,0x90,0x03,0xB0,0x90,0x62, 0x05,0x00,0x00,0x00,0x0C,0x0C,0x4C,0x4C,
    /* 0x30 */ 0x00,0x00,0x00,0x00,0x00,0x00,0x00,0x00, 0x30,0x00,0x00,0x00,0x20,0xFF,0x80,0x7F,
    /* 0x40 */ 0x00,0x00,0x00,0x00,0x00,0x00,0x00,0x00, 0x30,0x00,0x00,0x00,0x00,0x00,0x00,0x00,
    /* 0x50 */ 0x00,0x20,0x69,0x15,0x00,0x00,0x00,0x00, 0x00,0x00,0x00,0x00,0xFF,0xFF,0xFF,0xFF,
    /* 0x60 */ 0x00,0x00,0x00,0x00,0x00,0x00,0x00,0x00, 0x17,0x17,0x03,0x03,0x02,0x00,0x00,0x4E,
    /* 0x70 */ 0x02,0x32,0x00,0x00,0x00,0x00,0x00,0x00, 0x00,0x00,0x00,0x00,0x00,0x00,0x00,0x00,
    /* 0x80 */ 0x00,0x00,0x00,0x00,0x00,0x00,0x00,0x00, 0x00,0x00,0x00,0x00,0x00,0x00,0x00,0x00,
    /* 0x90 */ 0x00,0x00,0x00,0x00,0x00,0x00,0x00,0x00, 0x00,0x00,0x00,0x00,0x00,0x00,0x00,0x00,
    /* 0xA0 */ 0x00,0x00,0x00,0x00,0x00,0x00,0x00,0x00, 0x00,0x00,0x00,0x00,0x00,0x00,0x00,0x00,
    /* 0xB0 */ 0x00,0x00,0x00,0x00,0x0A,0x00,0x00,0x00, 0xAA,0xAA,0x00,0x00,0x00,0x00,0x00,0x00,
    /* 0xC0 */ 0x00,0x00,0x00,0x00,0x00,0x00,0x00,0x00, 0x00,0x00,0x00,0x00,0x00,0x00,0x00,0x00,
    /* 0xD0 */ 0x00,0x00,0x00,0x00,0x00,0x00,0x00,0x00, 0x00,0x00,0x00,0x00,0x00,0x00,0x00,0x00,
    /* 0xE0 */ 0x00,0x00,0x00,0x00,0x00,0x00,0x00,0x00, 0x00,0x00,0x00,0x00,0x00,0x00,0x00,0x00,
    /* 0xF0 */ 0x00,0x00,0x00,0x00,0x00,0x00,0x00,0x00, 0x00,0x00,0x00,0x00,0x00,0x00,0x00,0x00,
};

Region region_of(uint32_t addr) {
    addr &= kAddrMask;
    if (addr <= 0x0000FF)                    return Region::IoPage;    /* TMP95C061 on-chip regs */
    if (addr >= 0x004000 && addr <= 0x006FFF) return Region::Ram;      /* work RAM + system page */
    if (addr >= 0x007000 && addr <= 0x007FFF) return Region::Ram;      /* shared Z80 RAM         */
    if (addr >= 0x008000 && addr <= 0x008FFF) return Region::K2ge;     /* video regs + palette   */
    if (addr >= 0x009000 && addr <= 0x00BFFF) return Region::Vram;     /* SCR1/SCR2/CHAR RAM     */
    if (addr >= 0x200000 && addr <= 0x3FFFFF) return Region::CartRom;  /* read-only -> flash     */
    if (addr >= 0xFF0000)                     return Region::Bios;     /* read-only              */
    return Region::Unmapped;
}

bool region_writable(Region r) {
    switch (r) {
        case Region::IoPage:
        case Region::Ram:
        case Region::K2ge:
        case Region::Vram:
            return true;
        /* CartRom / Bios / Unmapped: the write is DISCARDED. It is still
         * reported (ngpc_access_t.discarded = 1) because that is exactly what
         * drives the AMD-flash command latch. See CPP_CORE_PORT.md §4 hazard 9. */
        default:
            return false;
    }
}

/* The BIOS's power-on code fills the 18-slot user vector table with a default
 * RETI stub; our hand-off jumps straight to the cartridge and never runs it. So
 * do the same fill here -- but READ the stub out of the BIOS image instead of
 * memorising its address, by finding the fill routine's own anchor:
 *
 *     45 <imm32>        ld XIY, <default handler>
 *     44 B8 6F 00 00    ld XIX, 0x00006FB8        <- the anchor
 *
 * A BIOS without that routine gets no table and no invented address. */
uint32_t Machine::seed_user_vector_table() {
    static const uint8_t kAnchor[] = {0x44, 0xB8, 0x6F, 0x00, 0x00};   /* ld XIX, 0x00006FB8 */
    if (bios.size() < sizeof(kAnchor) + 5) return 0;

    for (size_t i = 5; i + sizeof(kAnchor) <= bios.size(); ++i) {
        if (std::memcmp(&bios[i], kAnchor, sizeof(kAnchor)) != 0) continue;
        if (bios[i - 5] != 0x45) continue;                             /* ld XIY, imm32 */
        const uint32_t stub = uint32_t(bios[i - 4]) | (uint32_t(bios[i - 3]) << 8) |
                              (uint32_t(bios[i - 2]) << 16) | (uint32_t(bios[i - 1]) << 24);
        if (stub < kBiosBase) continue;                                /* must point INTO the BIOS */
        for (unsigned slot = 0; slot < kUserVectorTableSlots; ++slot) {
            const uint32_t a = kUserVectorTableBase + slot * 4;
            mem[a + 0] = uint8_t(stub);
            mem[a + 1] = uint8_t(stub >> 8);
            mem[a + 2] = uint8_t(stub >> 16);
            mem[a + 3] = uint8_t(stub >> 24);
        }
        return stub;
    }
    return 0;
}

void Machine::reset_memory() {
    std::fill(mem.begin(), mem.end(), uint8_t(0));

    for (uint32_t a = 0; a < 0x100; ++a) mem[a] = kIoPageReset[a];

    /* A/D data register = the BATTERY gauge. NeoPop resets these to 0 because it
     * HLEs the BIOS and never runs the real power-on battery check; we DO run it,
     * and 0 means "flat battery" -> the BIOS powers the console off (DEVLOG 183).
     * TMP95C061 Fig. 3.12(3-1): ADREG = (result << 6) | 0x3F, unused bits read 1.
     * Full scale 0x03FF = healthy. */
    mem[0x000060] = 0xFF;
    mem[0x000061] = 0xFF;

    /* Work RAM / system page / shared Z80 RAM / K2GE / VRAM are 0 at power-on
     * (already zeroed above). The documented non-zero cells follow. */

    /* The BIOS copies the ROM header mode byte here at power-on. */
    mem[0x006F91] = rom.size() > 0x23 ? rom[0x23] : 0x00;

    /* ⚡ K1GE COMPATIBLE MODE, FOR THE MONOCHROME CARTRIDGES.
     *
     * Header byte 0x23 names the machine the game was written for: 0x00 = the
     * monochrome NGP (K1GE), 0x10 = the NGPC (K2GE). A mono game predates the
     * colour console and never asks for the mode itself -- the BIOS sets it from the
     * header, and this is the BIOS's own code (0xFF17C4):
     *
     *      cp A, 0x10
     *      jr NC, ...            ; >= 0x10 -> colour, leave the mode at 0
     *      ld (0x87F0), 0xAA     ; unlock  (the mode register is write-protected)
     *      ld (0x87E2), 0x80     ; MODE = 1 -> K1GE upper-palette compatible
     *      ld (0x6F95), 0x00     ; the system "colour mode" flag
     *      ld (0x87F0), 0x55     ; re-lock
     *
     * We do not run the BIOS's boot code, so we hand the cart the state it leaves --
     * exactly as we do for the user vector table and the registers. Without it, the
     * mono games run and draw, and the renderer resolves every pixel through the
     * K2GE palettes they never wrote: a blank screen. */
    if (rom.size() > 0x23 && rom[0x23] < 0x10) {
        mem[0x0087E2] = 0x80;
        mem[0x0087F0] = 0x55;      // locked, which is where the BIOS leaves it
        mem[0x006F95] = 0x00;

        /* ⚡ THE COMPAT COLOUR PALETTE -- THE GREY RAMP. MEASURED OFF THE BIOS.
         *
         * A K1GE game writes the 3-bit LEVEL table (0x8100) -- that WAS its palette on
         * the old machine -- and knows nothing about the 12-bit colour table the K2GE
         * resolves those levels through. That table is the COLOUR THEME the console
         * applies to old cartridges, exactly as a Game Boy Color tints a Game Boy game,
         * and it is the BIOS that installs it.
         *
         * ⛔ AND IT IS NOT A TABLE IN THE BIOS ROM. I searched the whole image for a
         * grey ramp and found none, and briefly took that as evidence there wasn't one.
         * There is: the BIOS COMPUTES it. Booting the real BIOS with a mono cartridge
         * (pass 237) and reading the palette back is what produced these numbers --
         * all four planes, the same eight levels, repeated across the sixteen entries:
         *
         *     FFF DDD BBB 999 777 444 333 000   x2
         *
         * 🔑 "I could not find it in the ROM" is not "it does not exist". The machine
         * had it all along; I was looking for the wrong SHAPE. */
        static const uint16_t kGreyRamp[8] = {
            0x0FFF, 0x0DDD, 0x0BBB, 0x0999, 0x0777, 0x0444, 0x0333, 0x0000,
        };
        for (uint32_t base : {0x0083A0u, 0x0083C0u, 0x0083E0u, 0x008380u}) {
            for (unsigned i = 0; i < 16; ++i) {
                const uint16_t c = kGreyRamp[i & 7];
                mem[base + i * 2]     = uint8_t(c);
                mem[base + i * 2 + 1] = uint8_t(c >> 8);
            }
        }
    }

    /* BIOS hand-off system RAM the cart sees at entry. Cross-checked 2026-07-09
     * against cosim --dump-mem 0x6F80 and found UNIVERSAL across carts. Without
     * these, carts diverge at instruction ~1 (Neo Turf reads 0x6F84). */
    mem[0x006F80] = 0xFF;  /* ADC/contrast reading, 0x03FF full-scale (low)  */
    mem[0x006F81] = 0x03;  /*                                        (high)  */
    mem[0x006F84] = 0x40;  /* BIOS system status byte                        */
    mem[0x006F87] = 0x01;  /* BIOS system status byte                        */

    /* K2GE power-on values (NGPC_HW_QUICKREF.md §5 + NeoPop reset_memory()).
     * 0x8000 = 0xC0 is load-bearing: VBlank+HBlank interrupts are ENABLED at
     * reset. An all-zero default booted a console with interrupts OFF. */
    mem[0x008000] = 0xC0;  /* control: VBlank (b7) + HBlank (b6) IRQ enabled */
    mem[0x008004] = 0xFF;  /* WSI.H — window width, full screen              */
    mem[0x008005] = 0xFF;  /* WSI.V — window height, full screen             */
    mem[0x008006] = 0xC6;  /* REF   — frame rate, never modified             */
    mem[0x008118] = 0x80;  /* BGC on                                          */
    mem[0x0083E0] = 0xFF;  /* default backdrop colour (low)                  */
    mem[0x0083E1] = 0x0F;  /* default backdrop colour (high)                 */
    mem[0x0083F0] = 0xFF;  /* default window colour (low)                    */
    mem[0x0083F1] = 0x0F;  /* default window colour (high)                   */
    mem[0x008400] = 0xFF;  /* LED on                                          */

    /* Cartridge window. ERASED FLASH READS AS 0xFF -- the whole cart window is
     * flash, and any cell the ROM image does not cover is simply erased. Leaving
     * it at 0x00 makes every read past the end of the image return zero, which is
     * NOT what the hardware does and NOT what the reference does either (DEVLOG
     * 2026-04-20, "cart flash erased-read fallback").
     *
     * This one byte value was behind 22 of the residual memory-family
     * divergences: the instructions were right, the operand they loaded was not. */
    /* The cart window reads ERASED FLASH (0xFF) where the image does not reach.
     *
     * ⛔ THE SECOND CHIP'S WINDOW (0x800000) USED TO BE DELIBERATELY UNMAPPED, on
     * the claim "the BIOS only ever touches it to ask what chip it is". True for
     * every 2 MiB cart -- and FALSE for the three 4 MiB carts in the corpus. A
     * 4 MiB cart is TWO dies, and the hardware maps the second at 0x800000:
     * SNK vs. Capcom MotM keeps its whole intro (tile data, page descriptors,
     * even pointers INTO the same window) above 0x800000. With the window
     * unmapped every one of those reads returned ZERO, the game's decompressor
     * faithfully copied zeros into character RAM, and the intro played blind on
     * a dash-tile screen forever (pass 247: char RAM frozen at 73/512 tiles from
     * frame 200 to 3000, while the engine heartbeat ran perfectly).
     *
     * The old copy loop had the same bug from the other side: it spilled a 4 MiB
     * image straight through 0x200000..0x5FFFFF, planting the second die's bytes
     * at 0x400000 -- an address range that is NOT a cartridge window on this bus.
     * Chip 0 is capped at its die size; the remainder goes where the pins go. */
    std::fill(mem.begin() + 0x200000, mem.begin() + 0x400000, uint8_t(0xFF));
    const size_t chip0 = rom.size() < size_t(0x200000) ? rom.size() : size_t(0x200000);
    for (size_t i = 0; i < chip0; ++i)
        mem[0x200000 + i] = rom[i];
    if (rom.size() > 0x200000) {
        std::fill(mem.begin() + 0x800000, mem.begin() + 0xA00000, uint8_t(0xFF));
        const size_t chip1 = rom.size() - 0x200000 < size_t(0x200000)
                                 ? rom.size() - 0x200000 : size_t(0x200000);
        for (size_t i = 0; i < chip1; ++i)
            mem[0x800000 + i] = rom[0x200000 + i];
    }
    /* Carts of 2 MiB or less keep the window unmapped, exactly as before: the
     * fuzz gate reads unmapped space there and both cores must keep agreeing. */
    flash_mode[0] = flash_mode[1] = 0;
    flash_step[0] = flash_step[1] = 0;

    /* BIOS image, when attached. */
    for (size_t i = 0; i < bios.size() && (0xFF0000 + i) <= kAddrMask; ++i)
        mem[0xFF0000 + i] = bios[i];

    /* ⚡ WHAT THE SUB-BATTERY KEPT. Applied AFTER the wipe above, because on the real
     * console a power cycle does not clear this RAM -- a coin cell holds it, and that
     * is why the BIOS remembers your language and the date. Same contract as the flash
     * contents: `reset()` is a power cycle, not a factory reset. */
    for (size_t i = 0; i < battery_ram.size() && i < kRamSize; ++i)
        mem[kRamStart + i] = battery_ram[i];
}


/* --- A/D converter -------------------------------------------------------
 * Transcribed from the TMP95C061 datasheet (3.12.1), and deliberately kept in
 * the same shape as core/adc.py so the two can be diffed by eye:
 *   - writing ADS = 1 starts a conversion and raises ADBF;
 *   - ADS "is always read as 0", so the start bit is cleared as it is consumed;
 *   - on completion EOCF goes up, ADBF goes down, the result is published to
 *     ADREG0, and INTAD is raised;
 *   - in repeat mode the next conversion begins immediately. */
void Machine::adc_tick(uint32_t cycles) {
    uint8_t admod = mem[kAdmodAddress];

    if (!adc_busy) {
        if (!(admod & kAdmodAds)) return;
        adc_busy = true;
        adc_cycles_remaining =
            int32_t((admod & kAdmodAdcs) ? kAdcCyclesLowSpeed : kAdcCyclesHighSpeed);
        mem[kAdmodAddress] = uint8_t((admod & ~kAdmodAds) | kAdmodAdbf);
        return;
    }

    adc_cycles_remaining -= int32_t(cycles);
    if (adc_cycles_remaining > 0) return;

    adc_busy = false;
    const uint16_t result = adc_battery & kAdcFullScale;
    /* ADREG0L: bits 7-6 = the low 2 bits of the result; bits 5-0 READ AS 1.
     * That is why the BIOS does `ldw WA,(0x60); srl 6`. */
    mem[kAdreg0LowAddr]  = uint8_t(((result & 0x03) << 6) | 0x3F);
    mem[kAdreg0HighAddr] = uint8_t((result >> 2) & 0xFF);

    admod = uint8_t((mem[kAdmodAddress] & ~kAdmodAdbf) | kAdmodEocf);
    if (admod & kAdmodRepet) {
        adc_busy = true;
        adc_cycles_remaining =
            int32_t((admod & kAdmodAdcs) ? kAdcCyclesLowSpeed : kAdcCyclesHighSpeed);
        admod = uint8_t(admod | kAdmodAdbf);
    }
    mem[kAdmodAddress] = admod;
    irq_pending |= 1u << kIrqVectorIndexIntAd;
}

/* --- the 8-bit timers ------------------------------------------------------
 * Same shape as core/timers.py so the two can be diffed by eye. */
void Machine::timer_tick(uint32_t cycles) {
    /* The 2D controller's horizontal blank is wired to the external pin TI0.
     * The K2GE produces those pulses -- 152 per frame, at the start of lines
     * 198 and 0..150 ("The signal generation begins 1 H before the Hardware
     * Drawing Period starts", ngpcspec.txt) -- and advance_raster raises them
     * ON THE RASTER'S OWN CLOCK. Deriving them here from a private cycle
     * accumulator gave them an arbitrary phase against the raster, and Metal
     * Slug's HUD split flickered across it. The pulses are consumed (and, with
     * the prescaler stopped, DISCARDED -- the pin pulses whether or not the CPU
     * is listening, exactly as before) whatever else this call does. */
    const uint32_t hblanks = ti0_pending_pulses;
    ti0_pending_pulses = 0;
    if (cycles == 0 && hblanks == 0) return;
    const uint8_t trun = mem[kTrunAddress];
    if (!(trun & kTrunPrescaler)) {
        /* PRRUN = 0 zero-clears and STOPS the prescaler, so no internal tap
         * produces anything. (SDK 8Bit.txt.) */
        return;
    }

    const uint8_t t01mod = mem[kT01modAddress];
    const uint8_t t23mod = mem[kT23modAddress];
    const unsigned mode[4] = {
        unsigned(t01mod & 0x03), unsigned((t01mod >> 2) & 0x03),
        unsigned(t23mod & 0x03), unsigned((t23mod >> 2) & 0x03),
    };
    const uint8_t treg[4] = {
        mem[kTreg0Address], mem[kTreg1Address], mem[kTreg2Address], mem[kTreg3Address],
    };
    /* 0 = "not an internal tap" (an external pin, or the cascade). */
    /* The taps are RATIOS off phi-T1 (1 : 4 : 16 : 256 -- datasheet and SDK agree on
     * THAT much); only the base is in dispute. See ngpc_set_timer_base. */
    const uint32_t kEvenSources[4] = {0, timer_base, timer_base * 4, timer_base * 16};
    const uint32_t kOddSources[4]  = {0, timer_base, timer_base * 16, timer_base * 256};

    uint32_t cascade[4] = {0, 0, 0, 0};

    for (unsigned i = 0; i < 4; ++i) {
        if (!(trun & (1u << i))) {
            /* A STOPPED TLCS-900 timer holds its up-counter CLEARED (TxRUN=0 resets UC);
             * it does not merely freeze. Games re-phase a per-frame HBlank raster effect
             * by stop/starting Timer0 in VBlank every frame (TRUN 0x88 -> 0x8B): the
             * restart must begin the count at 0 so the first match lands the SAME scanline
             * each frame -- dev_ref Effects-and-Raster 6.5: "the fire line does not drift".
             * Freezing the count here instead carried a fractional remainder across frames
             * (Super Real Mahjong: TREG0=12 over 152 HBlanks = 12.67 matches/frame), so its
             * MicroDMA backdrop-colour gradient (table -> 0x8118) drifted and corrupted a
             * band of scanlines that swept the felt on a ~7-frame cycle. */
            timer_count[i] = 0;
            timer_clock[i] = 0;
            continue;
        }
        const bool even = (i % 2) == 0;
        const uint32_t period = even ? kEvenSources[mode[i]] : kOddSources[mode[i]];

        uint32_t ticks;
        if (period == 0) {
            /* Mode 00: an EVEN timer takes the external pin -- which on this
             * machine only exists for timer 0 (the horizontal blank). An ODD timer
             * takes the paired timer's overflows: the 16-bit cascade. */
            if (even) ticks = (i == 0) ? hblanks : 0u;
            else      ticks = cascade[i - 1];
        } else {
            timer_clock[i] += cycles;
            ticks = timer_clock[i] / period;
            timer_clock[i] %= period;
        }
        if (ticks == 0) continue;

        /* "If it is in unison, the up counter is 0-cleared and an interrupt is
         * generated." A TREG of 0 matches on OVERFLOW, i.e. a limit of 256. */
        const uint32_t limit = treg[i] ? uint32_t(treg[i]) : 0x100u;
        const uint32_t total = timer_count[i] + ticks;
        const uint32_t matches = total / limit;
        timer_count[i] = total % limit;
        if (matches) {
            if (even) cascade[i] = matches;
            irq_pending |= 1u << (kIrqVectorIndexIntT0 + i);
            /* "T03 is used as an interrupt to the Z80 CPU" -- SNK, 8Bit.txt. Timer 3
             * is the sound driver's heartbeat. It is not an option: without it the
             * driver boots, idles forever, and the game waits on a handshake that
             * will never come.
             *
             * TO3 is the timer's external OUTPUT PIN, and a timer output pin is a
             * flip-flop: it TOGGLES on every match, so the square wave it puts out
             * runs at HALF the match rate. The Z80 sees one edge per full period.
             * Silicon says so (hw_calibration): the main CPU counts 976 INTT3 in two
             * seconds while the Z80 takes 485 interrupts -- exactly half. Asserting
             * on every match handed the sound driver twice its tempo. */
            if (i == 3) {
                to3_half_periods += matches;
                if (to3_half_periods >= 2u) {      /* one full period of the square wave */
                    to3_half_periods &= 1u;
                    z80.int_pending = true;
                }
            }
        }
    }
}

/* --- the calendar IC (RTC), I/O 0x90..0x97 --------------------------------
 * Transcribed from ares ngp/cpu/rtc.cpp + io.cpp: BCD fields that tick once a
 * second. Modelling this is what stops the BIOS deciding the coin cell is dead.
 * The CPU runs at ~6.144 MHz, so one RTC second is that many cycles. */
static constexpr uint32_t kRtcCyclesPerSecond = 6144000u;

/* The alarm's own vector. See irq_priority_register in machine.hpp for how index 10 was
 * pinned down, and why it is not the power button's index 8. */
constexpr uint32_t kRtcAlarmVector = 10;

uint8_t Machine::rtc_read(uint32_t addr) const {
    switch (addr) {
        /* bit0 = the clock runs, bit1 = the alarm is armed. Returning only bit0 meant the
         * BIOS wrote 0x03 and read back 0x01 -- it could never see its own alarm. */
        case 0x90: return uint8_t((rtc.enable & 1u) | ((rtc.alarm_enable & 1u) << 1));
        case 0x91: return rtc.year;
        case 0x92: return rtc.month;
        case 0x93: return rtc.day;
        case 0x94: return rtc.hour;
        case 0x95: return rtc.minute;
        case 0x96: return rtc.second;
        case 0x97: return uint8_t((rtc.weekday & 0x0Fu) | ((rtc.year & 3u) << 4));
        case 0x98: return rtc.alarm_day;
        case 0x99: return rtc.alarm_hour;
        case 0x9A: return rtc.alarm_minute;
        default:   return 0;
    }
}

void Machine::rtc_write(uint32_t addr, uint8_t v) {
    switch (addr) {
        case 0x90:
            rtc.enable       = uint8_t(v & 1u);
            rtc.alarm_enable = uint8_t((v >> 1) & 1u);
            break;
        case 0x91: rtc.year    = v; break;
        case 0x92: rtc.month   = v; break;
        case 0x93: rtc.day     = v; break;
        case 0x94: rtc.hour    = v; break;
        case 0x95: rtc.minute  = v; break;
        case 0x96: rtc.second  = v; break;
        case 0x97: rtc.weekday = uint8_t(v & 0x0Fu); break;
        case 0x98: rtc.alarm_day    = v; break;
        case 0x99: rtc.alarm_hour   = v; break;
        case 0x9A: rtc.alarm_minute = v; break;
        default:   break;
    }
}

static uint8_t rtc_days_in_month(uint8_t bcd_month, uint8_t bcd_year) {
    switch (bcd_month) {
        case 0x02: return ((bcd_year & 3u) == 0) ? 0x29 : 0x28;   /* Feb / leap */
        case 0x04: case 0x06: case 0x09: case 0x11: return 0x30;
        default:   return 0x31;
    }
}

/* ONE SECOND on the calendar chip: the BCD carry chain, byte for byte per ares. Each
 * `return` is "the carry stopped here", which is what the original wrote as `continue`. */
void Machine::rtc_tick_one_second() {
    rtc.second++;
    if ((rtc.second & 0x0Fu) <= 0x09) return;
    rtc.second += 6;
    if (rtc.second <= 0x59) return;
    rtc.second = 0;
    rtc.minute++;
    if ((rtc.minute & 0x0Fu) <= 0x09) return;
    rtc.minute += 6;
    if (rtc.minute <= 0x59) return;
    rtc.minute = 0;
    rtc.hour++;
    if ((rtc.hour & 0x0Fu) >= 0x0a) rtc.hour += 6;
    if (rtc.hour <= 0x23) return;
    rtc.hour = 0;
    rtc.weekday++;
    if (rtc.weekday >= 7) rtc.weekday = 0;
    rtc.day++;
    if ((rtc.day & 0x0Fu) >= 0x0a) rtc.day += 6;
    if (rtc.day <= rtc_days_in_month(rtc.month, rtc.year)) return;
    rtc.day = 1;
    rtc.month++;
    if ((rtc.month & 0x0Fu) >= 0x0a) rtc.month += 6;
    if (rtc.month <= 0x12) return;
    rtc.month = 1;
    rtc.year++;
    if ((rtc.year & 0x0Fu) >= 0x0a) rtc.year += 6;
    if (rtc.year <= 0x99) return;
    rtc.year = 0;
}

/* Has the clock just reached the armed alarm?
 *
 * Day/hour/minute only -- the chip has no alarm second (SDK: ALARM{Day,Hour,Min,Code}) --
 * so the match is taken on the second the minute turns over, ONCE, rather than staying
 * true for all sixty seconds of that minute. Day 0 is the "every day" case: the BIOS
 * normalises the SDK's 0xFF wildcard before it ever reaches the chip, so anything it
 * writes is a real day; a game poking the register directly may not, and an impossible
 * day should not silently mean "never". */
bool Machine::rtc_alarm_due() const {
    if (!rtc.alarm_enable) return false;
    if (rtc.second != 0x00) return false;
    if (rtc.minute != rtc.alarm_minute) return false;
    if (rtc.hour != rtc.alarm_hour) return false;
    return rtc.alarm_day == 0x00 || rtc.alarm_day == rtc.day;
}

/* Split out of `rtc_step` so that catching up on time that passed while the emulator was
 * CLOSED goes through this exact chain rather than a second implementation -- a real
 * console's clock runs off the coin cell whether or not you are playing. One carry chain,
 * both callers, and an alarm crossed while the machine was dark still fires. */
void Machine::rtc_advance_seconds(uint32_t seconds) {
    for (uint32_t i = 0; i < seconds; ++i) {
        if (!rtc.enable) return;        /* a stopped clock does not run, wound or ticked */
        rtc_tick_one_second();
        if (rtc_alarm_due()) irq_pending |= (1ull << kRtcAlarmVector);
    }
}

void Machine::rtc_step(uint32_t cycles) {
    rtc.counter += cycles;
    while (rtc.counter >= kRtcCyclesPerSecond) {
        rtc.counter -= kRtcCyclesPerSecond;
        rtc_advance_seconds(1);
    }
}

/* --- the cartridge flash chips ---------------------------------------------
 * The AMD autoselect command sequence, and the ID the BIOS reads back from it.
 * See the note in machine.hpp for why a cart that cannot answer this does not
 * boot at all. */
/* The manufacturer's own block map -- SDK FlashMem.txt, "BLOCK NUMBER" tables:
 * 64 KiB blocks all the way up, and the LAST 64 KiB split 32 / 8 / 8 / 16. The small
 * blocks at the top are exactly where a save goes: erasing 8 KiB to rewrite one slot
 * is the whole reason the chip is divided that way. */
void Machine::flash_build_blocks(int chip, uint32_t size) {
    auto& b = flash_blocks[chip];
    b.clear();
    if (size < 0x10000) return;
    for (uint32_t off = 0; off + 0x10000 <= size - 0x10000; off += 0x10000)
        b.push_back({off, 0x10000, true});
    const uint32_t top = size - 0x10000;
    b.push_back({top,          0x8000, true});   /* 32 KiB */
    b.push_back({top + 0x8000, 0x2000, true});   /*  8 KiB */
    b.push_back({top + 0xA000, 0x2000, true});   /*  8 KiB */
    b.push_back({top + 0xC000, 0x4000, true});   /* 16 KiB */
}

int Machine::flash_block_of(int chip, uint32_t offset) const {
    const auto& b = flash_blocks[chip];
    for (size_t i = 0; i < b.size(); ++i)
        if (offset >= b[i].offset && offset < b[i].offset + b[i].length) return int(i);
    return -1;
}

/* ⚡ THE CARTRIDGE TELLS US WHICH CHIP IT IS -- BY WHERE IT SAVES.
 *
 * A game erases by BLOCK NUMBER (SDK FlashMem.txt / BLOCK_NO.INC) and the number->address
 * table differs per card, so the capacity we present IS the block numbering. Block 17 is
 * 0xFA000 on an 8 Mbit card and 0x110000 on a 16 Mbit one. Present Delta Warp's 8 Mbit cart
 * as 16 Mbit and its erase lands two blocks from its save: the area is never cleared, the
 * read-back verify fails, and the game says "SAVE ERROR!" -- measured, 9 erases at 0x310000
 * while it programmed 0x2FA000.
 *
 * ⛔ AND THE CAPACITY CANNOT BE DERIVED FROM THE ROM IMAGE. The chip is bigger than the
 * image burned on it, by however much the publisher chose: Delta Warp is 512 KiB on an
 * 8 Mbit part, StarGunner is a small homebrew on a 16 Mbit one. "Always 16 Mbit" breaks the
 * first, "the next size up" breaks the second. Two carts, two answers, same ROM size --
 * there is no static rule, which is exactly why this used to be guesswork.
 *
 * 🔑 BUT THE SDK'S OWN TABLE HAS A CONSTANT IN IT. On all three cards the save block is the
 * SECOND 8 KiB BLOCK FROM THE TOP, so `capacity - save address == 0x6000`, exactly:
 *
 *      4 Mbit  block  9 @ 0x07A000     8 Mbit  block 17 @ 0x0FA000     16 Mbit  block 33 @ 0x1FA000
 *
 * So the cart answers the question itself, the first time it programs: capacity =
 * offset + 0x6000. And the trigger is precise rather than fuzzy -- offset + 0x6000 is a
 * standard capacity for THREE offsets only, and they are precisely the three save blocks.
 * A cart already presenting at the derived size (every full-size cart, saving at 0x1FA000)
 * changes nothing. The game's own retry then finds the geometry right. */
void Machine::flash_adopt_capacity_from_save(int chip, uint32_t offset) {
    const uint32_t derived = offset + 0x6000;
    if (derived != 0x080000 && derived != 0x100000 && derived != 0x200000) return;
    if (flash_blocks[chip].empty()) return;
    const auto& top = flash_blocks[chip].back();
    if (top.offset + top.length == derived) return;      /* already this card */
    flash_build_blocks(chip, derived);
    /* The BIOS reads the card type before it touches the chip, so restate it: the block
     * map and this byte are two halves of one answer. */
    mem[chip == 0 ? kBiosFlashCardType0 : kBiosFlashCardType1] = flash_size_code(chip);
}

/* ⚠️ A NOR CELL ONLY GOES DOWN. Programming ANDs; only an erase restores the ones. */
void Machine::flash_program(int chip, uint32_t base, uint32_t addr, uint8_t data) {
    flash_adopt_capacity_from_save(chip, addr - base);
    const int blk = flash_block_of(chip, addr - base);
    if (blk < 0 || !flash_blocks[chip][blk].writable) return;
    const uint8_t before = mem[addr];
    const uint8_t after  = uint8_t(before & data);
    if (after != before) { mem[addr] = after; flash_dirty[chip] = true; }
}

void Machine::flash_erase_block(int chip, uint32_t base, uint32_t addr) {
    const int blk = flash_block_of(chip, addr - base);
    if (blk < 0 || !flash_blocks[chip][blk].writable) return;
    const auto& b = flash_blocks[chip][blk];
    for (uint32_t i = 0; i < b.length; ++i) mem[base + b.offset + i] = 0xFF;
    flash_dirty[chip] = true;
}

void Machine::flash_erase_all(int chip, uint32_t base) {
    for (const auto& b : flash_blocks[chip]) {
        if (!b.writable) continue;
        for (uint32_t i = 0; i < b.length; ++i) mem[base + b.offset + i] = 0xFF;
    }
    flash_dirty[chip] = true;
}

/* The AMD/Fujitsu command state machine. Every cart-window write the CPU makes is
 * DISCARDED as memory and lands here instead -- which is exactly what a flash chip
 * does with it.
 *
 *   AA @ 5555 · 55 @ 2AAA · then one of
 *      90  autoselect (the chip answers its ID instead of its contents)
 *      A0  program    -- the NEXT cart write is programmed in, ANDed
 *      F0  reset      -- back to being memory
 *      80  erase prefix -> AA @ 5555 · 55 @ 2AAA · then
 *              10 @ 5555  erase the WHOLE chip
 *              30 @ addr  erase the BLOCK containing addr
 *      9A  protect prefix -> ... 9A @ addr : the block becomes read-only
 */
/* The cart's size, decided ONCE. `flash_build_blocks` is what puts a chip in a slot,
 * so an empty slot has no blocks -- and answers nothing. The 0x800000 window is the
 * DEVELOPMENT slot (SDK FlashMem.txt: "This area CS1 is only valid during development
 * ... cannot be used to run the program in the production version"), and a retail
 * console has nothing in it. We used to answer for it anyway, with chip 0's own size,
 * and the real BIOS duly wrote down that a second cartridge of the same size was
 * plugged in. It invented a cartridge that is not there. */
uint8_t Machine::flash_device_id(int chip) const {
    if (!flash_present(chip)) return 0;
    const size_t sz = flash_blocks[chip].back().offset + flash_blocks[chip].back().length;
    return sz <= 0x080000 ? 0xAB          /*  4 Mbit */
         : sz <= 0x100000 ? 0x2C          /*  8 Mbit */
                          : 0x2F;         /* 16 Mbit */
}

uint8_t Machine::flash_size_code(int chip) const {
    switch (flash_device_id(chip)) {
        case 0xAB: return 1;              /*  4 Mbit */
        case 0x2C: return 2;              /*  8 Mbit */
        case 0x2F: return 3;              /* 16 Mbit */
        default:   return 0;              /* no cartridge in this slot */
    }
}

bool Machine::flash_command(uint32_t addr, uint8_t value) {
    int chip = -1;
    uint32_t base = 0;
    if (addr >= 0x200000 && addr <= 0x3FFFFF)      { chip = 0; base = 0x200000; }
    else if (addr >= 0x800000 && addr <= 0x9FFFFF) { chip = 1; base = 0x800000; }
    if (chip < 0 || !flash_present(chip)) return false;

    const uint32_t offset = addr - base;
    const uint32_t cmd    = offset & 0x7FFF;     /* the command latch ignores the high bits */
    uint8_t& step = flash_step[chip];
    uint8_t& mode = flash_mode[chip];

    if (mode == FlashWrite) {                    /* the cycle after A0 IS the data */
        flash_program(chip, base, addr, value);
        mode = FlashRead;
        step = 0;
        return true;
    }
    if (value == 0xF0) { mode = FlashRead; step = 0; return true; }

    if (step == 0 && cmd == 0x5555 && value == 0xAA) { step = 1; return true; }
    if (step == 1 && cmd == 0x2AAA && value == 0x55) { step = 2; return true; }
    if (step == 2 && cmd == 0x5555) {
        switch (value) {
            case 0x90: mode = FlashReadId; step = 0; return true;   /* autoselect */
            case 0xA0: mode = FlashWrite;  step = 0; return true;   /* program    */
            case 0x80: step = 3;                     return true;   /* erase      */
            case 0x9A: step = 3;                     return true;   /* protect    */
            default:   mode = FlashRead;   step = 0; return true;
        }
    }
    if (step == 3 && cmd == 0x5555 && value == 0xAA) { step = 4; return true; }
    if (step == 4 && cmd == 0x2AAA && value == 0x55) { step = 5; return true; }
    if (step == 5) {
        step = 0;
        if (cmd == 0x5555 && value == 0x10) { flash_erase_all(chip, base); mode = FlashAck; return true; }
        if (value == 0x30) { flash_erase_block(chip, base, addr); mode = FlashAck; return true; }
        if (value == 0x9A) {                                        /* protect a block */
            const int blk = flash_block_of(chip, offset);
            if (blk >= 0) { flash_blocks[chip][blk].writable = false; flash_dirty[chip] = true; }
            mode = FlashRead;
            return true;
        }
        mode = FlashRead;
        return true;
    }

    step = 0;
    return false;
}

bool Machine::flash_id_read(uint32_t addr, uint8_t& out) const {
    int chip = -1;
    uint32_t base = 0;
    if (addr >= 0x200000 && addr <= 0x3FFFFF)      { chip = 0; base = 0x200000; }
    else if (addr >= 0x800000 && addr <= 0x9FFFFF) { chip = 1; base = 0x800000; }
    if (chip < 0 || !flash_present(chip)) return false;

    if (flash_mode[chip] == FlashAck) {
        /* An erase answers ONE read and then the chip is memory again -- that is the
         * "done" the driver's status-poll loop is waiting for. */
        const_cast<Machine*>(this)->flash_mode[chip] = FlashRead;
        out = 0xFF;
        return true;
    }
    if (flash_mode[chip] != FlashReadId) return false;

    /* The device ID names the SIZE, and the BIOS reads it to know how big the cart is.
     * SDK FlashMem.txt gives three parts (4 / 8 / 16 Mbit); the manufacturer byte is
     * Toshiba's 0x98. Hardcoding one size told every cartridge it was 8 Mbit. */
    switch (addr - base) {
        case 0: out = kFlashManufacturerId; return true;   /* 0x98 = Toshiba */
        case 1: out = flash_device_id(chip); return true;   /* the SIZE, decided once */
        case 2: out = 0x02;                 return true;
        case 3: out = kFlashDeviceId3;      return true;   /* the BIOS masks it with 0xF8 and wants 0x80 */
        default: out = 0xFF; return true;
    }
}

/* --- the micro-DMA ---------------------------------------------------------
 * The interrupt that does not go to the CPU. See machine.hpp and
 * specs/MICRO_DMA.md. */
bool Machine::micro_dma_service(unsigned vector_index) {
    if (vector_index == 0) return false;

    for (unsigned ch = 0; ch < 4; ++ch) {
        if (mem[kDma0vAddress + ch] != uint8_t(vector_index)) continue;

        uint32_t& src = cpu.cregs[0x00 + 4 * ch];
        uint32_t& dst = cpu.cregs[0x10 + 4 * ch];
        uint32_t& cnt = cpu.cregs[0x20 + 4 * ch];   /* 16-bit, low half used */
        const uint8_t mode = uint8_t(cpu.cregs[0x22 + 4 * ch]);

        const unsigned kind = mode >> 2;
        const unsigned zz = mode & 0x03;
        const uint8_t size = (zz == 0) ? 1 : (zz == 1) ? 2 : 4;
        if (zz == 3) return false;                  /* "reserved" -- do not invent one */

        if (kind <= 4) {
            /* One transfer, source to destination, at the size the mode names. */
            uint32_t value = 0;
            for (uint8_t i = 0; i < size; ++i)
                value |= uint32_t(read8(src + i)) << (8 * i);
            for (uint8_t i = 0; i < size; ++i) {
                const uint32_t a = (dst + i) & kAddrMask;
                if (region_writable(region_of(a))) mem[a] = uint8_t(value >> (8 * i));
            }
            switch (kind) {
                case 0: dst += size; break;         /* (DMAD+) <- (DMAS)  */
                case 1: dst -= size; break;         /* (DMAD-) <- (DMAS)  */
                case 2: src += size; break;         /* (DMAD)  <- (DMAS+) */
                case 3: src -= size; break;         /* (DMAD)  <- (DMAS-) */
                default: break;                     /* fixed              */
            }
        } else if (kind == 5) {
            ++src;                                   /* counter mode: nothing moves */
        } else {
            return false;                            /* not a mode the chip defines */
        }

        const uint16_t left = uint16_t(uint16_t(cnt) - 1);
        cnt = (cnt & 0xFFFF0000u) | left;
        if (left == 0) {
            /* "the HDMA start vector is cleared, and the HDMA start source of the
             * channel is also cleared" -- datasheet §3.3.2. */
            mem[kDma0vAddress + ch] = 0;
            /* ...AND the transfer-END interrupt INTTCn is requested. This IS needed:
             * the SDK's own auto-rearm pattern re-programs DMAS/DMAC/DMAxV from the
             * INTTCn ISR (user slot 0x6FF0+4n), and Ogre Battle Gaiden drives its
             * card-scene raster split from it -- the ISR resets SCR1_X=0 so the lower
             * (dialogue-box) scanlines render unscrolled. Left silent, the box slid off
             * with the plane. The vector index is now established (see machine.hpp,
             * confirmed against this BIOS's dispatch stub) and it is level-gated by the
             * INTETC01/23 nibble like every other source, so a game that leaves the
             * level at 0 still sees nothing. */
            irq_pending |= uint64_t(1) << (kIrqVectorIndexIntTc0 + ch);
        }
        return true;                                 /* the DMA itself does not vector the CPU */
    }
    return false;
}

}  // namespace ngpc
