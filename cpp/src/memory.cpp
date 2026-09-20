/* memory.cpp — flat address space + documented power-on state.
 *
 * The Python core reads memory through three layers (_RuntimeOverlayDecodeBus
 * -> NgpcReadBus -> probe) and allocates an AddressProbe per byte: 113 861
 * read_bytes calls per 16 000 instructions (PERF_TIMING_POLICY.md §10.2).
 * Here the whole 24-bit space is one flat array and a read is an array index.
 *
 * Every power-on value below is transcribed from core/memory.py, which cites
 * its own sources (TMP95C061 datasheet, NGPC_HW_QUICKREF.md §5). Do not
 * "tidy" these values: a wrong one boots a
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
    /* 0xB0 */ 0x00,0x00,0x00,0x00,0x0A,0x00,0x05,0x00, 0xAA,0xAA,0x00,0x00,0xFE,0x00,0x00,0x00,   /* B6/BC: measured 0x05/0xFE on silicon */
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
    /* THE SECOND DIE. A 4 MiB cartridge is two chips and the hardware maps the second
     * at 0x800000 -- the copy loop below fills it, read8() answers flash IDs from it,
     * and flash_command() accepts its unlock sequence. This function never learned
     * about it and kept calling the window "unmapped", which was harmless while nobody
     * asked (CartRom and Unmapped are equally non-writable) and became wrong the moment
     * something did: the ROM analyzer reported the BIOS's perfectly normal cartridge
     * probe -- 0xAA to 0x805555, 0x55 to 0x802AAA, the AMD unlock sequence -- as
     * "writes into unmapped space", on every 4 MiB cart. */
    if (addr >= 0x800000 && addr <= 0x9FFFFF) return Region::CartRom;
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
    /* ⛔ TIMING STATE IS STATE. The bus interface unit's run-ahead debt and the two
     * serial buffer stages are per-machine, and leaving them across a reset makes a
     * replay start from a different point than the first run -- which is exactly how
     * the libretro smoke test caught this: "non-deterministic state after replay".
     * Anything added to Machine that influences timing belongs here AND in the
     * savestate, or the core stops being reproducible. */
    biu_debt = 0;
    /* ⚡ ET LA RETENUE FRACTIONNAIRE. Depuis le recalage silicium l'attente de
     * lecture vaut 8,25 cycles par mot : le quart restant est REPORTE d'une
     * instruction a l'autre, donc c'est de l'etat, pas une constante. L'oublier
     * ici a fait tomber le test de rejeu libretro dans la seconde. */
    access_wait_q4 = 0;
    fetch_wait_carry = 0;
    /* ⚡ Le drapeau « une copie de bloc vient de tourner » est efface a la fin de CHAQUE
     * instruction, donc il ne peut pas franchir une savestate (elles sont prises entre
     * deux instructions) -- mais il est remis a zero ici pour la meme raison que les
     * trois du dessus : rien de ce qui influence le temps ne survit a un reset. */
    block_transfer_ran = false;
    serial_tx_buf_full = false;
    serial_tx_buf_byte = 0;
    serial_rx_shift_full = false;
    serial_rx_shift_byte = 0;
    serial_rx_had_pending = false;

    std::fill(mem.begin(), mem.end(), uint8_t(0));

    for (uint32_t a = 0; a < 0x100; ++a) mem[a] = kIoPageReset[a];

    /* A/D data register = the BATTERY gauge. Resetting these to 0 is only safe if you
     * HLE the BIOS and never run the real power-on battery check; we DO run it,
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
        /* ...and the mode bit that SELECTS that table. 0x87E2 bit 7 is the K2GE's
         * "K1GE upper-palette compatible" switch; on real K1GE silicon there is no
         * switch, the machine simply IS that. The mono BIOS therefore never writes the
         * register (disassembled: it does not address 0x87E2 once), so our renderer sat
         * in K2GE colour mode and resolved every pixel through colour palettes nobody
         * had filled -- the two-tone screen.
         *
         * So the RENDERER no longer asks this byte on a mono console (render.cpp: the
         * machine decides, not the register). We still stamp it, for the debugger and
         * for anything that reads the register file expecting it to describe the
         * machine -- but nothing depends on it surviving: a cartridge clearing the
         * video page puts it back to zero, and on this console that must change
         * nothing at all. */
        mem[0x0087E2] = 0x80;

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
     * against a reference dump at 0x6F80 and found UNIVERSAL across carts. Without
     * these, carts diverge at instruction ~1 (Neo Turf reads 0x6F84). */
    mem[0x006F80] = 0xFF;  /* ADC/contrast reading, 0x03FF full-scale (low)  */
    mem[0x006F81] = 0x03;  /*                                        (high)  */
    mem[0x006F83] = 0x10;
    mem[0x006F86] = 0x40;
    /* ⚡ 0x6F91 IS HOW A CARTRIDGE KNOWS IT IS ON A COLOUR CONSOLE.
     *
     * Read off a REAL BIOS boot (real_bios=true, 700 frames) and diffed against what
     * this hand-off used to leave. We were seeding 0x40/0x01 at 0x6F84/0x6F87; the
     * BIOS actually puts them at 0x6F83/0x6F86 and -- the part that matters -- fills
     * 0x6F91 = 0x10 and 0x6F92 = 0x03, which we left at ZERO.
     *
     * A colour-aware mono cartridge tests that byte (Cool Boarders does it literally:
     * `cp (0x6F91), 0x10`). With zero there, Samurai Shodown decides it is running on
     * a plain mono NGP and never runs its colourisation code: measured, it wrote SIXTEEN
     * bytes of palette in 1200 frames and nothing at all to the K2GE blocks, so its
     * fighter stayed a two-tone silhouette. On the user's real NGPC the same cartridge
     * comes up partly COLOURED -- green bamboo, characters near their canonical
     * colours -- which is the game's own data, not a BIOS theme.
     *
     * So this is not decoration: it is the flag that selects the cartridge's colour
     * path, and leaving it zero silently downgraded every colour-aware mono game. */
    if (!k1ge_console) {
        mem[0x006F91] = 0x10;
        mem[0x006F92] = 0x03;
    } else {
        /* THE MONO NGP ANSWERS FOR ITSELF -- and it answers 0x00.
         *
         * This used to leave whatever ROM load had seeded here, which is the
         * CARTRIDGE HEADER byte (rom[0x23], see above). For a mono cartridge that
         * reads 0x00 and looked right by accident; for a COLOUR cartridge the header
         * says 0x10, so a colour game put in our "mono NGP" was told it was on an
         * NGPC and behaved exactly as it does on one -- which is the bug a user
         * reported against SNK vs. Capcom, a colour game that shows a DIFFERENT
         * screen on mono hardware.
         *
         * The console, not the cartridge, owns this byte: disassembled, the mono
         * NGP's own BIOS does `ld (0x6F91), 0x00` (encoded F1 91 6F 00 00) exactly
         * where the NGPC's does `ld (0x6F91), 0x10`, and it never writes 0x6F92 at
         * all. So we stamp what that silicon stamps, whatever cartridge is in the
         * slot -- and when the user supplies the real mono BIOS image, its own code
         * writes the same value over the top. */
        mem[0x006F91] = 0x00;
        mem[0x006F92] = 0x00;

        /* THE MONO NGP'S GREY RAMP, WHICH ON THAT SILICON IS NOT A PALETTE AT ALL.
         *
         * On a K1GE the 3-bit LEVEL tables at 0x8100 ARE the picture: level -> grey is
         * fixed in the panel, there is no 12-bit colour block. We render through the
         * K2GE's compat palette at 0x8380..0x83DF, and mono mode BLOCKS writes there
         * (execute.cpp) because a cartridge cannot reach that silicon -- but the block
         * caught the BIOS's own writes too, so the table stayed all ZERO and every
         * level resolved to the same colour: the screen came out in two tones, and the
         * NGP boot logo appeared over the SNK wallpaper that should have been invisible.
         *
         * So the ramp is part of the machine, stamped at power-on: the eight greys the
         * retail BIOS programs (FFF DDD BBB 999 666 444 222 000, read off a real boot),
         * for both sub-palettes of all three planes. Neutral on purpose -- the greys are
         * the panel's, and the BIOS colour THEME is an NGPC feature that mono hardware
         * has no way to apply. */
        static const uint16_t kGreyRamp[8] = {
            0x0FFF, 0x0DDD, 0x0BBB, 0x0999, 0x0666, 0x0444, 0x0222, 0x0000};
        for (uint32_t base = 0x008380; base < 0x0083E0; base += 2) {
            const uint16_t grey = kGreyRamp[((base - 0x008380) >> 1) & 0x07];
            mem[base] = uint8_t(grey & 0xFF);
            mem[base + 1] = uint8_t(grey >> 8);
        }
    }

    /* K2GE power-on values (NGPC_HW_QUICKREF.md §5).
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

/* --- serial channel 0 (the link cable) ------------------------------------
 * A byte pipe between two machines. Disabled -> no-op (cable unplugged), which
 * is the pre-link behaviour every existing ROM and test still gets. See
 * machine.hpp for the model. */
/* TMP95C061 datasheet section 3.11, figures 3.11(6) SC0MOD, 3.11(8) BR0CR and
 * 3.11(12) the channel-0 block diagram. Cycles per BIT are the product of the baud
 * generator's input-clock divider, its own divider, and UART's 16x oversampling; a
 * byte is that times the frame length in bits. */
int32_t Machine::serial_byte_cycles() const {
    const uint8_t sc0mod = mem[0x000052];
    const uint8_t br0cr  = mem[0x000053];

    /* SC0MOD<SM1,SM0>: 00 = I/O interface mode (not a UART frame at all), 01/10/11 =
     * UART with 7 / 8 / 9 data bits. A frame is start + data + stop. */
    const unsigned sm = (sc0mod >> 2) & 0x3;
    if (sm == 0) return int32_t(kSerialByteCycles)
                      + int32_t(uint32_t(kSerialByteCycles) * serial_byte_extra_pct / 100u);
    const int32_t bits = int32_t(sm + 8);       /* 01->9, 10->10, 11->11 */

    /* SC0MOD<SC1,SC0> picks what clocks the shift register. */
    const unsigned sc = sc0mod & 0x3;
    int32_t cycles_per_bit;
    if (sc == 1) {
        /* Baud rate generator. BR0CR<BR0CK1,0> selects phi-T0 (fc/4), phi-T2 (fc/16),
         * phi-T8 (fc/64) or phi-T32 (fc/256); BR0CR<BR0S3:0> divides by 2..15, with
         * 0000 meaning 16 (and 0001 documented as "don't set"). */
        const int32_t tap = int32_t(4u << (2u * ((br0cr >> 4) & 0x3)));
        const unsigned s = br0cr & 0x0F;
        const int32_t div = (s == 0) ? 16 : int32_t(s);
        cycles_per_bit = tap * div * 16;
    } else if (sc == 2) {
        cycles_per_bit = 2 * 16;                /* internal clock phi1 = fc/2, then /16 */
    } else {
        /* 00 = timer 2 match output, 11 = don't care. Nothing programs these on this
         * console; keep the previous behaviour rather than invent a rate. */
        return kSerialByteCycles;
    }
    const int32_t base = bits * cycles_per_bit;
    return base + int32_t(uint32_t(base) * serial_byte_extra_pct / 100u);
}

void Machine::serial_tick(uint32_t cycles) {
    if (!serial_link_enabled) {
        /* No cable, but the UART does not know that: a byte written to SC0BUF still
         * shifts out and still raises INTTX0. Only the wire is missing, so the byte is
         * dropped instead of queued. */
        if (!uart_runs_unplugged || !(serial_tx_busy || serial_tx_buf_full)) return;
        /* ⚠️ SAME ORDER AS THE CABLED PATH, and for the same reason: count the shift
         * register DOWN first, then load the buffer into whatever it freed. Loading
         * first leaves the register idle until the next instruction, which is a dead
         * gap on every single byte -- measured on the cabled path as ~2200 short stalls
         * a window, and worth 3529 -> 4008 bytes once fixed. */
        if (serial_tx_busy) {
            serial_tx_cycles -= int32_t(cycles);
            if (serial_tx_cycles <= 0) {
                serial_tx_busy = false;
                serial_tx_shifting = false;      /* the byte went to the pin, nowhere */
                if (!tx_irq_on_buffer_free) {
                    irq_pending |= 1u << kIrqVectorSerialTransmit;
                    ++serial_irq_tx_count;
                }
            }
        }
        if (tx_irq_on_buffer_free && serial_tx_buf_full && !serial_tx_busy) {
            serial_tx_byte     = serial_tx_buf_byte;
            serial_tx_buf_full = false;
            serial_tx_busy     = true;
            serial_tx_shifting = true;
            serial_tx_cycles   = serial_byte_cycles();
            irq_pending |= 1u << kIrqVectorSerialTransmit;   /* SC0BUF is free */
            ++serial_irq_tx_count;
        }
        return;
    }

    /* TX: the BIOS wrote a byte to SC0BUF (captured in io_action_write). After one
     * baud-time it is on the wire: hand it to the host to relay, and raise the
     * TRANSMIT-buffer-empty interrupt so the BIOS transmit handler pulls the next
     * byte from its ring. (That handler lives on the 0x19 vector on this BIOS.)
     *
     * CTS0 handshake (SC0MOD<CTSE>=bit6, datasheet 3.11 fig.16): when enabled, a
     * queued byte is HELD while CTS0 is HIGH and only shifts out once CTS0 goes LOW
     * -- so INTTX0 fires only when the PEER (whose RTS drives our CTS0) is ready to
     * receive. Games that just blast bytes leave CTSE off / the peer's RTS low, so
     * nothing changes for them; Card Fighters' Clash relies on this gate to keep the
     * two consoles' handshake in step. Without it we completed every send instantly,
     * one-sided, and CFC's mutual rendezvous never converged.
     *
     * ⚡ CTS GATES THE START OF A BYTE, NEVER A BYTE ALREADY GOING OUT. The datasheet
     * says it twice -- "when the CTS0 pin goes high, AFTER COMPLETION OF THE CURRENT
     * DATA SEND, data send is halted", and Note 1 of fig 3.11(16): "if the CTS signal
     * rises during transmission, the NEXT data is not sent after the completion of the
     * current transmission". A shift register that has begun cannot be paused.
     *
     * ⛔ THE BUG THIS ENDS: this used to re-test CTS every tick and freeze the
     * in-flight byte's countdown, so a peer that pulsed its RTS high for a fraction of
     * a frame stalled a byte that hardware would have finished. MEASURED, Card
     * Fighters' Clash at the HP exchange that starts a VS match: the peer raises RTS
     * just as we start a packet, the sender is held 6001 ticks, a 26-byte packet drags
     * over three frames instead of two, and the game gives up -- "LINK ERROR. CHECK
     * CONNECTIONS AND SETTINGS." on one console while the other waits at "CHOOSING HP."
     * for ever. That is the reported "both press A and nothing happens".
     *
     * It only became visible when the cable started being relayed every LINK_SLICE
     * instructions (for The Last Blade): at one relay per frame the peer's RTS pulse
     * fell between two samples and was never seen. So the finer relay did not break
     * CFC -- it exposed a transmitter that was always wrong. */
    if (serial_tx_busy) {
        const bool ctse       = (mem[0x000052] & 0x40) != 0;  /* SC0MOD<CTSE> */
        const bool cts_blocks = ctse && serial_cts_high;      /* peer not ready */
        if (serial_tx_shifting || !cts_blocks) {
            serial_tx_shifting = true;     /* committed: this byte now always finishes */
            serial_tx_cycles -= int32_t(cycles);
            if (serial_tx_cycles <= 0) {
                serial_tx.push_back(serial_tx_byte);
                serial_tx_busy = false;
                serial_tx_shifting = false;
                ++serial_wire_count;
                if (!tx_irq_on_buffer_free) {
                    irq_pending |= 1u << kIrqVectorSerialTransmit;
                    ++serial_irq_tx_count;
                }
                /* THE byte is on the wire, this exact cycle. A host that asked to
                 * be woken (ngpc_set_serial_break) relays it now instead of at the
                 * end of some instruction quota. */
                serial_event = true;
            }
        } else {
            ++serial_cts_hold_ticks;       /* debugger: "held by the peer", not idle */
        }
    }

    /* STAGE 1 -> STAGE 2, and it runs AFTER the shift countdown ON PURPOSE. The
     * shift register may have gone idle during THIS tick; loading before the
     * countdown would leave it empty until the next instruction, which is a dead
     * gap on every single byte -- measured as 1200 short stalls a window. INTTX0
     * fires here, at the START of the byte's time on the wire, not at its end.
     * CTS gates the START of a byte, so it gates this transfer and nothing else. */
    if (tx_irq_on_buffer_free && serial_tx_buf_full && !serial_tx_busy) {
        const bool ctse = (mem[0x000052] & 0x40) != 0;
        if (!(ctse && serial_cts_high)) {
            serial_tx_byte     = serial_tx_buf_byte;
            serial_tx_buf_full = false;
            serial_tx_busy     = true;
            serial_tx_shifting = true;
            serial_tx_cycles   = serial_byte_cycles();
            irq_pending |= 1u << kIrqVectorSerialTransmit;   /* SC0BUF is free */
            ++serial_irq_tx_count;
        } else {
            ++serial_cts_hold_ticks;
        }
    }

    /* RX: present the next queued byte at SC0BUF and raise INTRX0 -- but only when
     * the previous one has been read (serial_rx_pending clears in read8, the
     * overrun guard) AND our RTS says we are ready to receive (0xB2 bit0 == 0, set
     * low by COMONRTS). serial_rx_cycles starts at 0 so the first byte arrives
     * promptly, then paces at one byte-time. */
    /* Stage 1 -> stage 2: the CPU has read SC0BUF, so the byte already in the shift
     * register moves up and raises its interrupt at once. */
    if (rx_double_buffered && serial_rx_shift_full && !serial_rx_pending) {
        serial_rx_byte = serial_rx_shift_byte;
        serial_rx_shift_full = false;
        serial_rx_pending = true;
        irq_pending |= 1u << kIrqVectorSerialReceive;
        ++serial_irq_rx_count;
    }

    const bool half_duplex_block = rx_blocked_by_tx && serial_tx_busy;
    const bool rx_slot_free = rx_double_buffered ? !serial_rx_shift_full
                                                 : !serial_rx_pending;
    if (rx_slot_free && !serial_rx.empty() && !half_duplex_block
        && (mem[0x0000B2] & 0x01) == 0) {
        /* ⛔ THE RECEIVER CHARGES THE BYTE TIME A SECOND TIME, AND THAT IS A REAL
         * DEFECT -- but removing it ALONE makes the emulator LESS like hardware, so
         * it stays until its counterpart is understood. MEASURED 2026-08-19 on the
         * round-trip probe, against silicon:
         *
         *   pattern                       here    fixed    silicon
         *   A pings / B echoes (1 token)   766     1278      1095
         *   both ping        (2 tokens)   1533     2428      1021
         *
         * Silicon gains NOTHING from running both directions at once (x0.93 going
         * from one pattern to the other); we very nearly double (x2.00). So hardware
         * is bound by something we do not model -- most likely the cost of servicing
         * the serial ISRs, which caps the exchange rate whatever the traffic shape.
         * The double charge happens to hide that, and taking it away exposes it: A/B
         * improves from -30% to +17%, A/A degrades from +50% to +138%.
         *
         * ⛔ AND ITS OLD RELEASE CONDITION IS VOID. It used to read "re-apply this
         * together with the interrupt-cost correction". That correction has since
         * landed (13 -> 18, datasheet 3.3.1, confirmed again by Appendix B table
         * (11)) and it is worth NOTHING here: swept from 13 to 56, it moves the cost
         * of a received byte from 49 us to 56 where silicon wants 96.
         *
         * ⚠️ AND THE MEASUREMENT THAT USED TO JUDGE THIS SITS ON A KNIFE EDGE -- OURS,
         * not the metric's. The round trip is a LOCK-STEP counter: the token either
         * fits inside the current frame's window or waits for the next. MEASURED
         * 2026-08-20: correcting `LD R,#` by ONE cycle -- an unrelated instruction --
         * moved it from 1533 to 761, and across configurations it jumps between 761,
         * 1082, 1533, 2043 and 2154. So anything "validated" against the round trip
         * alone is validated against a coin toss, and that includes the 2026-08-20
         * note claiming three corrections close the model to 3%.
         *
         * ⛔ DO NOT shorten that to "the round trip is a bad instrument" -- it was
         * written that way once and silicon refuted it. The AUTO campaign on real
         * hardware (2026-08-21) returns 1218 / 1220 / 1217 on three consecutive runs:
         * 0.25% dispersion, cross-checked by role B echoing exactly their sum, 3655.
         * The console is rock steady and WE jump. That asymmetry is a fact about this
         * core, not about the test.
         *
         * ⇒ Release it only with a CONTINUOUS metric agreeing: throughput and the
         * test-6 CPU counters. `Machine::rx_single_charge` is the knob to try it. */
        serial_rx_cycles -= int32_t(cycles);
        if (rx_single_charge && serial_rx_had_pending) {
            serial_rx_cycles = 0;              /* the byte's time was already paid */
            serial_rx_had_pending = false;
        }
        if (serial_rx_cycles <= 0) {
            const uint8_t got = serial_rx.front();
            serial_rx.pop_front();
            serial_rx_had_pending = true;
            serial_rx_cycles = serial_byte_cycles();
            if (rx_double_buffered && serial_rx_pending) {
                /* SC0BUF still holds the previous byte: park this one in the shift
                 * register. It moves up, and interrupts, when the CPU reads. */
                serial_rx_shift_byte = got;
                serial_rx_shift_full = true;
            } else {
                serial_rx_byte = got;
                serial_rx_pending = true;
                irq_pending |= 1u << kIrqVectorSerialReceive;
                ++serial_irq_rx_count;
            }
        }
    } else if (!serial_rx.empty() && (mem[0x0000B2] & 0x01) != 0) {
        ++serial_rts_hold_ticks;    /* debugger: bytes waiting, WE are not ready */
    }

    /* OUR RTS drives the PEER's CTS, and the peer's transmitter is held while it
     * is high -- so an RTS edge is cable traffic even though no byte moved. A
     * host that only relayed on bytes would leave the peer stalled against a
     * handshake we already released: that is the shape of the Card Fighters'
     * Clash hang, where each console waits for the other. Sampled here rather
     * than hooked onto the 0xB2 write so that ONE place decides what "the cable
     * moved" means. */
    const uint8_t rts_now = uint8_t(mem[0x0000B2] & 0x01);
    if (rts_now != serial_rts_last) {
        serial_rts_last = rts_now;
        serial_event = true;
    }
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
            /* ⚡ ONE MATCH, ONE MICRO-DMA TRANSFER -- AND `irq_pending` IS A BITMASK.
             *
             * A per-HBlank scroll table is a channel armed on INTT0 walking one entry
             * per scanline: the Nth entry MUST land on the Nth line, or every line
             * below the loss is drawn with its neighbour's offset. The request used to
             * be recorded by OR-ing one bit and answered later, in deliver_irq, which
             * runs BETWEEN INSTRUCTIONS -- so two matches falling inside a single long
             * instruction collapsed into one transfer and the table ran a line behind
             * for the whole rest of the frame.
             *
             * MEASURED, SNK Gals' Fighters (the player's own save state, 120 frames):
             * a healthy frame delivers 152 entries, and the entry that hands the bottom
             * HUD its S2SO.V = 8 arrives on line 135, in time for line 136's snapshot.
             * On the frames that lost one -- always at line 0 or 1, where the VBlank
             * handler's block copy straddles the first HBlanks of the picture -- only
             * 151 arrived, the split reached line 136 too late to be latched, and line
             * 136 came out as a row of playfield above the special bar. That is the
             * reported flickering line, and it is the SAME seam Samurai Shodown! 2 was
             * fixed at, by a different mechanism (see render.cpp: the window is live).
             *
             * The DMA controller is not the CPU and does not wait for an instruction
             * boundary: it steals a bus cycle when the request happens. So answer each
             * match here, as it happens. A channel that runs its count out clears its
             * own start vector, so the loop stops on its own; whatever is left over --
             * no channel armed, or the channel just disarmed -- falls through to the
             * CPU exactly as before. */
            uint32_t unserviced = matches;
            while (unserviced && micro_dma_service(kIrqVectorIndexIntT0 + i))
                --unserviced;
            if (unserviced) irq_pending |= uint64_t(1) << (kIrqVectorIndexIntT0 + i);
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
 * BCD fields that tick once a second, per the SDK's register map. Modelling
 * this is what stops the BIOS deciding the coin cell is dead.
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

/* ONE SECOND on the calendar chip: the BCD carry chain.
 *
 * Standard BCD adjust, and there is only one shape it can take: increment, and if the
 * low nibble has passed 9 add 6 to carry it into the high nibble; if the field has
 * passed its limit, zero it and carry into the next. The limits are the calendar's, not
 * a choice -- 0x59 seconds, 0x59 minutes, 0x23 hours, 0x12 months, 0x99 years. Each
 * `return` means "the carry stopped here".
 *
 * ⚠️ TWO PLACES WHERE THE OBVIOUS TRANSLATION IS WRONG. Both are easy slips that
 * survive casual testing, and both have been found in the wild:
 *   - the month length must be indexed by the MONTH, not the day. Indexing by the day
 *     agrees with the right answer for much of the month, so it hides.
 *   - the year rollover must compare the FULL BYTE against 0x99. Comparing a 4-bit
 *     field against 0x99 is always true, and the year then never rolls over at all. */
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
 * 🔑 BUT THE CART ASKS FOR ITS SAVE IN ITS OWN CARD'S UNITS, and that request is readable.
 * On all three cards the save is the top of the chip -- the two 8 KiB blocks numbered n-3
 * and n-2:
 *
 *      4 Mbit  blocks  8,  9 @ 0x078000     8 Mbit  16, 17 @ 0x0F8000     16 Mbit  32, 33 @ 0x1F8000
 *
 * So the cartridge answers the question itself, two ways, and we take both:
 *   - `flash_adopt_capacity_from_block` -- the BLOCK NUMBER a game hands the BIOS, read at
 *     the `swi 1`, BEFORE the BIOS turns it into an address. This is the one that gets the
 *     FIRST save right, and it is the one that was missing.
 *   - `flash_adopt_capacity_from_save`  -- the ADDRESS, for homebrew that drives the chip
 *     directly and never calls the BIOS.
 *
 * A cart already presenting at the derived size (every full-size cart) changes nothing. */
/* The three cards the SDK's block tables describe. There is no fourth. */
static constexpr uint32_t kCardCapacities[3] = {0x080000, 0x100000, 0x200000};

/* How many blocks a card of `capacity` has: 64 KiB blocks all the way up, the last
 * 64 KiB split in four. So the two 8 KiB SAVE blocks are numbers n-3 and n-2:
 *      4 Mbit  n=11 -> 8, 9        8 Mbit  n=19 -> 16, 17       16 Mbit  n=35 -> 32, 33 */
static uint32_t card_block_count(uint32_t capacity) { return capacity / 0x10000 + 3; }

uint32_t Machine::flash_presented_capacity(int chip) const {
    if (flash_blocks[chip].empty()) return 0;
    const auto& top = flash_blocks[chip].back();
    return top.offset + top.length;
}

/* The image burned on this die -- meaning the DATA on it, not the file's length.
 *
 * A chip is never smaller than what is written on it, and that is the floor under any
 * size the cartridge claims. But trailing 0xFF is not written: it is what an ERASED
 * cell reads as, indistinguishable from a chip nobody filled to the top. Counting it
 * as image is how a file that grew once stays grown: an under-filled cart persisted at
 * a guessed 16 Mbit comes back looking like a 2 MiB image, its own correction is then
 * refused as "smaller than the ROM", and every save after that is programmed into a
 * slot nobody erased. Ignoring the erased tail heals those files on the next load, and
 * costs nothing on a full cart -- a 2 MiB ROM's data still reaches far above any
 * smaller card, so the floor still holds.
 *
 * Computed ONCE, when the cartridge goes in. It was briefly computed per call, which
 * is a scan of the erased tail on EVERY flash command -- and a 256-byte save is 256
 * programs, so an under-filled cart would have walked its 1.5 MiB of padding a
 * quarter of a million times for one save. */
void Machine::flash_measure_image() {
    const uint32_t total = uint32_t(rom.size());
    for (int chip = 0; chip < 2; ++chip) {
        const uint32_t begin = chip == 0 ? 0u : kCartChipSize;
        if (total <= begin) { flash_image_bytes[chip] = 0; continue; }
        uint32_t end = chip == 0 ? (total < kCartChipSize ? total : kCartChipSize)
                                 : total - begin;
        while (end > 0 && rom[begin + end - 1] == 0xFF) --end;
        flash_image_bytes[chip] = end;
    }
}

uint32_t Machine::flash_image_size(int chip) const { return flash_image_bytes[chip]; }

/* ⚠️ THE CARD-TYPE BYTE AND THE BLOCK MAP MUST NEVER DISAGREE.
 *
 * They are two halves of one answer: the BIOS turns a block NUMBER into an ADDRESS
 * using the byte, and the chip decides how much to erase from the map. Let them drift
 * and a game asking for its 8 KiB save block gets 64 KiB of its own ROM erased --
 * measured, exactly that, when the byte said 8 Mbit over a 16 Mbit map. With
 * `save_to_rom` on (the default) that lands in the .ngc.
 *
 * So every exit from this function leaves the byte describing the map, including the
 * exit that REFUSES a resize -- the one path that used to leave a stale byte standing.
 * (A cartridge on silicon cannot get into that state at all: the BIOS reads the chip's
 * own ID at power-on. If a game clobbers the byte afterwards, a real console erases
 * the wrong block too, and so do we -- that part is faithful, not a bug.) */
void Machine::flash_present_as(int chip, uint32_t capacity) {
    if (flash_blocks[chip].empty()) return;             /* no cartridge in this slot */
    const uint32_t card_type_addr = chip == 0 ? kBiosFlashCardType0 : kBiosFlashCardType1;
    if (capacity < flash_image_size(chip) ||            /* smaller than its own image */
        capacity == flash_presented_capacity(chip)) {   /* already this card          */
        mem[card_type_addr] = flash_size_code(chip);    /* ...but say so, either way   */
        return;
    }
    flash_build_blocks(chip, capacity);
    /* The BIOS reads the card type before it touches the chip, so restate it: the block
     * map and this byte are two halves of one answer. */
    mem[chip == 0 ? kBiosFlashCardType0 : kBiosFlashCardType1] = flash_size_code(chip);
}

/* ⚡ THE CARTRIDGE TELLS US WHICH CHIP IT IS -- BY THE BLOCK NUMBER IT ASKS FOR.
 *
 * This is the earliest and the sharpest of the two signals, and it is the one that was
 * missing. A game does not hand the BIOS an address; it hands it a BLOCK NUMBER
 * (`ld rb3, BLOCK_NB` ; VECT_FLASHERS), a number it took from the SDK table FOR ITS OWN
 * CARD. The BIOS then turns that number into an address using the card type at 0x6C58 --
 * so by the time the chip sees anything, the identity is gone. Read at the `swi 1`, the
 * number IS the answer:
 *
 *      block 16/17 -> an 8 Mbit card      block 8/9 -> 4 Mbit      block 32/33 -> 16 Mbit
 *
 * ⛔ WHY THE ADDRESS RULE BELOW IS NOT ENOUGH. It only recognises a save that lands in
 * the top 64 KiB, and it only fires on the PROGRAM -- after the erase has already gone to
 * the wrong block. Measured: a cart presented one size too big saves once (a virgin chip
 * is all 0xFF, so the first program needs no erase at all -- the same false green as
 * 2026-07-18) and then fails FOREVER, because the slot is never cleared again. That is
 * the "sometimes I have to pick the size by hand" the user reported.
 *
 * The guards, in order of how much damage they prevent:
 *   - a block inside the presented card's OWN top four is the card saving normally: no
 *     signal, nothing to learn.
 *   - the derived capacity must hold the image (`flash_present_as`). Shrinking a card is
 *     the dangerous direction -- it moves the save DOWN, into the game's own code -- so a
 *     capacity that would cut into the image is refused outright.
 *   - the number must name a save block on EXACTLY ONE of the three cards. */
void Machine::flash_adopt_capacity_from_block(int chip, uint32_t block) {
    const uint32_t present = flash_presented_capacity(chip);
    if (present == 0) return;
    /* Its own top four: the card saving normally, nothing to learn. Written as a RANGE and
     * not as ">= n-4", because a number ABOVE the presented map is the opposite case --
     * a card bigger than the one we are showing, which is exactly what must get through. */
    const uint32_t n = card_block_count(present);
    if (block >= n - 4 && block < n) return;

    uint32_t derived = 0;
    for (uint32_t cap : kCardCapacities) {
        const uint32_t candidate_blocks = card_block_count(cap);
        if (block != candidate_blocks - 3 && block != candidate_blocks - 2) continue;
        if (derived) return;                              /* two readings: no answer */
        derived = cap;
    }
    if (derived) flash_present_as(chip, derived);
}

/* The same question asked of an ADDRESS, for the homebrew that drives the chip itself and
 * never goes near the BIOS. A save lives in the LAST 64 KiB of the chip -- that is what
 * the split into 32/8/8/16 is for -- so an offset that falls in the top 64 KiB of a
 * standard card, while the card we present puts it mid-chip, names the card.
 *
 * (This used to test `offset + 0x6000` exactly, which recognises only the second 8 KiB
 * block. Real games use the whole top: 0xF8000, 0xF9F00, 0xFBF00 are all in the corpus.) */
void Machine::flash_adopt_capacity_from_save(int chip, uint32_t offset) {
    for (uint32_t cap : kCardCapacities)
        if (offset >= cap - 0x10000 && offset < cap) { flash_present_as(chip, cap); return; }
}

/* Listen to the request a game makes of the BIOS, at the `swi 1` -- the last moment it is
 * still expressed in the CARTRIDGE's own units rather than in addresses.
 *
 *      ld rw3, VECT_FLASHERS (8) ; ld ra3, card ; ld rb3, BLOCK_NB ; swi 1
 *      ld rw3, VECT_FLASHWRITE (6) ; ld ra3, card ; ld xde3, offset ; swi 1
 *
 * (SNK SysCall.txt / SYSTEM.INC; the register layout is the one tests/test_bios_flash_
 * syscall.py drives.) This only READS registers -- `swi 1` still runs the real BIOS
 * routine through the hardware vector table, exactly as before. */
void Machine::bios_flash_syscall_hint() {
    const uint32_t* b3 = (cpu.rfp == 3) ? cpu.regs : cpu.banks[3];
    const uint8_t vector = uint8_t(b3[0] >> 8);      /* RW3 -- which system call */
    const uint8_t card   = uint8_t(b3[0]);           /* RA3 -- 0 = 0x200000, 1 = 0x800000 */
    if (card > 1) return;
    if (vector == 8)      flash_adopt_capacity_from_block(card, uint8_t(b3[1] >> 8));  /* RB3 */
    else if (vector == 6) flash_adopt_capacity_from_save(card, b3[2] & 0x00FFFFFFu);   /* XDE3 */
}

/* Arm the busy window. The BYTES ARE ALREADY COMMITTED when this is called -- what the
 * window changes is what a READ of the window answers until it closes, which is the only
 * thing the silicon does differently. `after` is what the chip becomes when it closes:
 * FlashAck for an erase (the one-shot 0xFF a BIOS poll consumes), FlashRead for a
 * program, FlashFailed for a program that cannot succeed.
 *
 * `cycles == 0` is the synchronous chip this model used to be: no window at all. */
void Machine::flash_begin_busy(int chip, uint64_t cycles, uint8_t dq7, uint8_t after) {
    if (cycles == 0) {
        flash_mode[chip] = (after == FlashFailed) ? uint8_t(FlashRead) : after;
        return;
    }
    /* kFlashNever: the window never closes. Measured -- see the constants in machine.hpp:
     * an impossible program on this cartridge answers status for as long as it is asked,
     * and a driver only ever leaves on its own timeout. */
    flash_busy_until[chip] = (cycles == kFlashNever) ? kFlashNever : total_cycles + cycles;
    flash_busy_dq7[chip]   = dq7;
    flash_after_busy[chip] = after;
    flash_mode[chip]       = FlashBusy;
}

/* ⚠️ A NOR CELL ONLY GOES DOWN. Programming ANDs; only an erase restores the ones. */
void Machine::flash_program(int chip, uint32_t base, uint32_t addr, uint8_t data) {
    flash_adopt_capacity_from_save(chip, addr - base);
    const int blk = flash_block_of(chip, addr - base);
    if (blk < 0 || !flash_blocks[chip][blk].writable) return;
    const uint8_t before = mem[addr];
    const uint8_t after  = uint8_t(before & data);
    if (after != before) { mem[addr] = after; flash_dirty[chip] = true; }

    /* ASKING A 0 BACK UP TO 1 IS NOT A SLOW WRITE, IT IS A WRITE THAT NEVER LANDS.
     *
     * ⛔ AND THE CHIP NEVER ADMITS IT. Measured on the cartridge (hw_test_flash_timing,
     * ROW 9): 262 143 turns of the poll -- 2.45 SECONDS -- and no DQ5. The driver leaves
     * on its own iteration ceiling or not at all, and every one of those seconds is spent
     * with interrupts masked on a cartridge that has stopped being memory. That is the
     * mechanism behind a save that passes in an emulator and takes the console down.
     *
     * `flash_fail_cycles` defaults to kFlashNever for exactly that reason. A finite value
     * gives the DQ5 behaviour the AMD datasheets describe, for a part that has it. */
    const bool impossible = (data & uint8_t(~before)) != 0;
    flash_begin_busy(chip,
                     impossible ? flash_fail_cycles : flash_program_cycles,
                     uint8_t(~data & 0x80),
                     impossible ? uint8_t(FlashFailed) : uint8_t(FlashRead));
}

void Machine::flash_erase_block(int chip, uint32_t base, uint32_t addr) {
    flash_adopt_capacity_from_save(chip, addr - base);
    const int blk = flash_block_of(chip, addr - base);
    if (blk < 0 || !flash_blocks[chip][blk].writable) return;
    const auto& b = flash_blocks[chip][blk];
    for (uint32_t i = 0; i < b.length; ++i) mem[base + b.offset + i] = 0xFF;
    flash_dirty[chip] = true;
    /* The erase's cost is the BLOCK'S, not a constant: 8 KB is the save block every game
     * of this project uses, 64 KB is the one the watchdog cannot survive. */
    flash_begin_busy(chip, flash_erase_per8k_cycles * b.length / 0x2000, 0x00, FlashAck);
}

void Machine::flash_erase_all(int chip, uint32_t base) {
    uint32_t total = 0;
    for (const auto& b : flash_blocks[chip]) {
        if (!b.writable) continue;
        for (uint32_t i = 0; i < b.length; ++i) mem[base + b.offset + i] = 0xFF;
        total += b.length;
    }
    flash_dirty[chip] = true;
    flash_begin_busy(chip, flash_erase_per8k_cycles * total / 0x2000, 0x00, FlashAck);
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

    /* A CHIP THAT IS WORKING IS NOT LISTENING. Commands sent during a program or an
     * erase are swallowed by the silicon, and one that has raised DQ5 accepts nothing
     * but a reset -- which is exactly how a driver is supposed to clear it. */
    if (mode == FlashBusy) {
        /* ⚡ THE RESET WORKS ON A CHIP THAT IS STUCK, NOT ON ONE THAT IS WORKING, and the
         * cartridge taught us both halves.
         *
         *   * hw_test_flash_timing ROW 10: right after a program that can never complete,
         *     the byte reads back as 0x5A -- and the only thing between the two is the
         *     stub's closing AA/55/F0. So F0 gets a STUCK chip back.
         *   * The counter-check ROM sent an F0 to a chip in the middle of a real erase,
         *     by accident, and THE CONSOLE DIED: the reset was ignored, the chip stayed
         *     busy, and the return fetched its next instruction out of status bits. Our
         *     model accepted that F0 and survived, so it hid the bug -- which is exactly
         *     the permissiveness this whole chantier is about.
         *
         * The endless window is the failed program; a finite one is work in progress. */
        if (value == 0xF0 && flash_busy_until[chip] == kFlashNever) {
            mode = FlashRead; step = 0; return true;
        }
        if (total_cycles < flash_busy_until[chip]) return true;
        mode = flash_after_busy[chip];
    }
    if (mode == FlashFailed) {
        if (value == 0xF0) { mode = FlashRead; step = 0; }
        return true;
    }

    if (mode == FlashWrite) {                    /* the cycle after A0 IS the data */
        /* flash_program decides what the chip becomes -- busy, then read or DQ5-failed.
         * Do NOT set `mode` here: this line used to say FlashRead and it overwrote the
         * busy window one instruction after it was armed. */
        flash_program(chip, base, addr, value);
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
        /* Same here: the erase arms its own busy window and FlashAck is what it
         * becomes when that window closes, not what it is now. */
        if (cmd == 0x5555 && value == 0x10) { flash_erase_all(chip, base); return true; }
        if (value == 0x30) { flash_erase_block(chip, base, addr); return true; }
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

    /* THE CHIP IS BUSY: it answers status, not contents, and it answers it to EVERY read
     * of its window -- a poll, a data fetch, an instruction fetch, a micro-DMA. */
    if (flash_mode[chip] == FlashBusy) {
        if (total_cycles < flash_busy_until[chip]) {
            flash_dq6[chip] ^= 0x40;                    /* DQ6 toggles while it works */
            out = uint8_t(flash_busy_dq7[chip] | flash_dq6[chip]);
            return true;
        }
        const_cast<Machine*>(this)->flash_mode[chip] = flash_after_busy[chip];
        if (flash_mode[chip] == FlashRead) return false;   /* memory again */
    }
    /* It gave up, and it says so until something resets it. DQ5 is the only bit a
     * driver has to tell "not yet" from "never". */
    if (flash_mode[chip] == FlashFailed) {
        flash_dq6[chip] ^= 0x40;
        out = uint8_t(flash_busy_dq7[chip] | flash_dq6[chip] | 0x20);
        return true;
    }
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
                if (!region_writable(region_of(a))) continue;
                mem[a] = uint8_t(value >> (8 * i));
                /* ⚡ A DMA WRITE IS STILL A WRITE, AND IT IS THE ONE RASTER WORK IS MADE OF.
                 * The event log exists to answer "when in the frame did this register
                 * change", and the answer for a scroll split is almost never a CPU store:
                 * the split is driven by micro-DMA precisely so no instruction has to run.
                 * Logging only `write8` left the log showing a game's vblank set-up and
                 * NOTHING on the lines where the split actually happens -- a silence that
                 * reads as "this game does no raster effect". It does; the CPU just is not
                 * the one doing it. Same window, same fields, `pc` = the DMA'd source. */
                if (a >= elog_lo && a <= elog_hi)
                    note_event(kEventWrite, a, mem[a], src + i);
            }
            switch (kind) {
                case 0: dst += size; break;         /* (DMAD+) <- (DMAS)  */
                case 1: dst -= size; break;         /* (DMAD-) <- (DMAS)  */
                case 2: src += size; break;         /* (DMAD)  <- (DMAS+) */
                case 3: src -= size; break;         /* (DMAD)  <- (DMAS-) */
                default: break;                     /* fixed              */
            }
            /* Le transfert vient d'avoir lieu : il a pris le bus. 8 etats en 1 ou 2
             * octets, 12 en 4 (datasheet, micro-DMA). En cycles = x2. */
            if (micro_dma_states)
                dma_cost_cycles += 2u * ((size == 4) ? 12u : 8u) * micro_dma_states / 8u;
        } else if (kind == 5) {
            ++src;                                   /* counter mode: nothing moves */
            /* ⚖️ LE MODE COMPTEUR N'EST PAS DOUBLE, ET C'EST MESURE (ROM v9, tir
             * silicium du 24/08) : la console facture **5,2 cycles** par passage en
             * mode compteur contre 12,9 pour un transfert d'octet. Doubler les 5 etats
             * de la datasheet comme on double le reste donnait 10,4 -- deux fois trop.
             * Le transfert d'octet, lui, tombe pile sur le nominal double (13,0). */
            if (micro_dma_states) dma_cost_cycles += 5u * micro_dma_states / 8u;
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
