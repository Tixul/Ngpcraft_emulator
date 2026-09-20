/* core.cpp — the flat C ABI implementation.
 *
 * PHASE 0: the CPU is EMPTY on purpose. ngpc_run() decodes nothing and traps
 * with NGPC_UNIMPLEMENTED on the first instruction, reporting the offending PC
 * and opcode byte. This is not a placeholder to be forgotten: it is the shape
 * every un-ported opcode keeps for the whole port. What is not yet ported is
 * LOUD, never silently wrong.
 *
 * The proof harness (specs/CPP_CORE_PORT.md §5) is built against THIS core
 * first, and it must FAIL against it. A harness that passes on an empty core
 * proves nothing.
 */
#include "machine.hpp"

#include <cstring>

using namespace ngpc;

extern "C" {

NGPC_API uint32_t ngpc_abi_version(void) { return NGPC_ABI_VERSION; }

NGPC_API ngpc_t* ngpc_create(void) {
    return reinterpret_cast<ngpc_t*>(new Machine());
}

NGPC_API void ngpc_destroy(ngpc_t* h) {
    delete reinterpret_cast<Machine*>(h);
}

NGPC_API int ngpc_load_rom(ngpc_t* h, const uint8_t* data, size_t len) {
    if (!h || !data || len < 0x30) return -1;   /* ROM_HEADER_SIZE */
    Machine* m = reinterpret_cast<Machine*>(h);
    m->rom.assign(data, data + len);
    m->flash_measure_image();
    /* The cartridge IS the flash chip. Its block map comes from its size, and a game
     * saves by erasing and programming the small blocks at the top (SDK FlashMem.txt).
     *
     * ⚡ AND A 4 MiB CART IS TWO CHIPS, so it is two block maps. This built ONE map sized
     * to the WHOLE image, which is wrong at both ends: chip 0's map ran to 4 MiB while its
     * window stops at 2 MiB -- putting the small top blocks, the ones a save actually
     * uses, at 0x5F0000, outside the cartridge altogether -- and chip 1 got no map at all,
     * so `flash_present(1)` was false and every command on the second die was refused
     * (measured: a program to 0x840000 was a silent no-op). Pass 247 gave the second die
     * its address window; it never gave it an identity. */
    const uint32_t total = uint32_t(len);
    const uint32_t chip0 = total < kCartChipSize ? total : kCartChipSize;
    m->flash_build_blocks(0, chip0);
    if (total > kCartChipSize) m->flash_build_blocks(1, total - kCartChipSize);
    return 0;
}

/* ---------------------------------------------------------------- the SAVE --
 * The cartridge's flash IS the save medium: a game erases a block and programs its
 * slot back in. So "the save file" is simply the part of the cart window the game
 * has changed -- and `ngpc_flash_dirty` says whether it changed anything at all,
 * which is what a front end needs in order not to write a file for nothing.
 *
 * `ngpc_flash_restore` writes straight into the cart window, past the read-only
 * region check, because that is what putting the cartridge back in the slot does. */
/* A write ON THE BUS, exactly as the CPU's `store()` performs it: writable regions
 * take it, and a cart-window write is DISCARDED as memory and handed to the flash
 * chip's command latch instead. This is not a test backdoor -- it is the same door
 * the CPU uses, and a test that reached around it would prove nothing about the path
 * a real game takes. */
NGPC_API void ngpc_bus_write(ngpc_t* h, uint32_t address, uint8_t value) {
    if (!h) return;
    Machine* m = reinterpret_cast<Machine*>(h);
    address &= kAddrMask;
    if (!m->write8(address, value)) {
        m->flash_command(address, value);      /* the cart window: a discarded write IS the command */
        return;
    }
    io_action_write(*m, address, value);       /* ...and the writes that DO something */
}

NGPC_API int ngpc_flash_dirty(ngpc_t* h) {
    if (!h) return 0;
    Machine* m = reinterpret_cast<Machine*>(h);
    return (m->flash_dirty[0] || m->flash_dirty[1]) ? 1 : 0;
}

NGPC_API void ngpc_flash_clear_dirty(ngpc_t* h) {
    if (!h) return;
    Machine* m = reinterpret_cast<Machine*>(h);
    m->flash_dirty[0] = m->flash_dirty[1] = false;
}

NGPC_API int ngpc_flash_restore(ngpc_t* h, uint32_t address,
                                const uint8_t* data, uint32_t len) {
    if (!h || !data) return -1;
    Machine* m = reinterpret_cast<Machine*>(h);
    /* ⚡ BOTH DIES. A 4 MiB cart is two chips and the second is wired to 0x800000, not
     * to the end of the first (pass 247, reset_memory). This check only ever knew about
     * chip 0's window, so re-inserting a 4 MiB cartridge -- which is what a reboot and a
     * save restore both are -- was refused for every byte on the second die. */
    const bool in_chip0 = address >= 0x200000 && uint64_t(address) + len <= 0x400000;
    const bool in_chip1 = address >= 0x800000 && uint64_t(address) + len <= 0xA00000;
    if (!in_chip0 && !in_chip1) return -1;
    for (uint32_t i = 0; i < len; ++i) m->mem[address + i] = data[i];
    return 0;
}

/* ⚡ THE COIN CELL. Hand the console the RAM it had when it was last switched off --
 * and hand it over BEFORE resetting, because `ngpc_reset` consults the marker INSIDE
 * that RAM to decide whether this is a first-ever boot or a resume. Restoring it after
 * the reset would be too late, and the BIOS would run its first-time wizard forever. */
NGPC_API void ngpc_set_battery_ram(ngpc_t* h, const uint8_t* data, uint32_t len) {
    if (!h) return;
    Machine* m = reinterpret_cast<Machine*>(h);
    if (!data || len == 0) { m->battery_ram.clear(); return; }   /* a dead cell */
    m->battery_ram.assign(data, data + (len > kRamSize ? kRamSize : len));
}

/* ⚡ AND THE OTHER HALF OF THAT COIN CELL: the clock. See ngpc_core.h for why these two
 * belong together and for what the BIOS was measured doing to each. */
NGPC_API void ngpc_get_rtc(ngpc_t* h, ngpc_rtc_t* out) {
    if (!h || !out) return;
    const Machine* m = reinterpret_cast<const Machine*>(h);
    out->enable  = m->rtc.enable;
    out->year    = m->rtc.year;
    out->month   = m->rtc.month;
    out->day     = m->rtc.day;
    out->hour    = m->rtc.hour;
    out->minute  = m->rtc.minute;
    out->second  = m->rtc.second;
    out->weekday = m->rtc.weekday;
    out->alarm_enable = m->rtc.alarm_enable;
    out->alarm_day    = m->rtc.alarm_day;
    out->alarm_hour   = m->rtc.alarm_hour;
    out->alarm_minute = m->rtc.alarm_minute;
    out->counter = m->rtc.counter;
}

NGPC_API void ngpc_set_rtc(ngpc_t* h, const ngpc_rtc_t* in) {
    if (!h || !in) return;
    Machine* m = reinterpret_cast<Machine*>(h);
    m->rtc.enable  = uint8_t(in->enable & 1u);
    m->rtc.year    = in->year;
    m->rtc.month   = in->month;
    m->rtc.day     = in->day;
    m->rtc.hour    = in->hour;
    m->rtc.minute  = in->minute;
    m->rtc.second  = in->second;
    m->rtc.weekday = uint8_t(in->weekday & 0x0Fu);
    m->rtc.alarm_enable = uint8_t(in->alarm_enable & 1u);
    m->rtc.alarm_day    = in->alarm_day;
    m->rtc.alarm_hour   = in->alarm_hour;
    m->rtc.alarm_minute = in->alarm_minute;
    m->rtc.counter = in->counter;
}

/* Wind the clock forward over time the console spent switched off. */
NGPC_API void ngpc_rtc_advance(ngpc_t* h, uint32_t seconds) {
    if (!h) return;
    reinterpret_cast<Machine*>(h)->rtc_advance_seconds(seconds);
}

NGPC_API uint32_t ngpc_get_framebuffer(ngpc_t* h, uint16_t* out, uint32_t max_pixels) {
    if (!h || !out) return 0;
    Machine* m = reinterpret_cast<Machine*>(h);
    const uint32_t n = Machine::kScreenWidth * Machine::kScreenHeight;
    const uint32_t want = max_pixels < n ? max_pixels : n;
    std::memcpy(out, m->framebuffer, want * sizeof(uint16_t));
    return want;
}

NGPC_API int ngpc_load_bios(ngpc_t* h, const uint8_t* data, size_t len) {
    if (!h || !data || len != 65536) return -1;
    Machine* m = reinterpret_cast<Machine*>(h);
    m->bios.assign(data, data + len);
    return 0;
}

/* ⚡ WHAT THE BIOS'S BOOT SCREEN LEAVES IN CHARACTER RAM.
 *
 * The hand-off seeds the state a real boot would have left behind; see the block on
 * `kCharRamBase` in machine.hpp for why those 8 KiB are part of that state and which
 * game reads them. This is the same method that produced the grey ramp and the entry
 * registers: power the console on for real, let the BIOS draw, and read the machine.
 *
 * It runs BEFORE the hand-off reset wipes memory, so the only thing that survives the
 * warm-up is the buffer we take out of it -- work RAM, the flash working image and
 * every register are re-initialised from scratch afterwards. `m->bios` and
 * `m->battery_ram` are members, not memory, so the boot cannot disturb them.
 *
 * Returns false (and leaves `out` untouched) when there is no BIOS to boot. */
static bool capture_bios_boot_char_ram(ngpc_t* h, uint8_t* out) {
    Machine* m = reinterpret_cast<Machine*>(h);
    if (m->bios.empty()) return false;          /* no BIOS -> nothing to seed, and no invention */

    ngpc_reset(h, kResetBiosBoot);

    ngpc_summary_t s;
    std::memset(&s, 0, sizeof(s));
    ngpc_run_frames(h, kBiosWarmUpFrames, kBiosWarmUpMaxInstrs, &s);

    /* THE HALT IS NOT A HANG: IT IS THE CONSOLE SWITCHED OFF. The BIOS boots, arms
     * INT0 and sleeps; INT0 is the POWER BUTTON. Same press the shell makes on the
     * player's behalf -- if the two ever diverge, the BIOS never draws and this
     * returns the zeros it was meant to replace. */
    if (s.stop_status == NGPC_HALTED) {
        ngpc_raise_irq(h, kInt0PowerButton);
        ngpc_run_frames(h, kBiosWarmUpFrames, kBiosWarmUpMaxInstrs, &s);
    }

    std::memcpy(out, &m->mem[kCharRamBase], kCharRamSize);
    return true;
}

NGPC_API void ngpc_reset(ngpc_t* h, int reset_mode) {
    if (!h) return;
    Machine* m = reinterpret_cast<Machine*>(h);
    const bool apply_bios_handoff = (reset_mode == kResetHandoff);

    /* Taken before `reset_memory()` below wipes it, and put back at the end of the
     * hand-off. The recursion guard matters: the warm-up resets the machine itself,
     * and without it a hand-off reset would boot the BIOS forever. */
    std::vector<uint8_t> bios_char_ram;
    if (apply_bios_handoff && !m->in_bios_warm_up) {
        m->in_bios_warm_up = true;
        bios_char_ram.resize(kCharRamSize);
        if (!capture_bios_boot_char_ram(h, bios_char_ram.data())) bios_char_ram.clear();
        m->in_bios_warm_up = false;
    }

    m->reset_memory();

    std::memset(&m->cpu, 0, sizeof(m->cpu));
    m->cpu.pc = m->rom_entry_point();

    /* ⚡ THE CONSOLE POWERING ON, FOR REAL.
     *
     * `bios_handoff = false` used to mean "seed nothing" and STILL START AT THE CART'S
     * ENTRY POINT -- so the BIOS's own boot code had never once run in this emulator,
     * in either mode. We diagnosed that in pass 237 and never fixed it; this is the fix.
     *
     * The hardware reads its reset vector out of the table at 0xFFFF00 (-> 0xFF204A in
     * the retail BIOS). But if the RAM marker says the console has booted before, it
     * goes to VECT_SHUTDOWN instead, so the BIOS can run the cleanup it would normally
     * do when you swap cartridges. See machine.hpp. */
    if (reset_mode == kResetBiosBoot) {
        const bool been_here_before = m->mem[kBiosRamMarker] != 0;
        const uint32_t slot = been_here_before ? kVectShutdown : kHwResetVector;
        m->cpu.pc = uint32_t(m->read8(slot))
                  | (uint32_t(m->read8(slot + 1)) << 8)
                  | (uint32_t(m->read8(slot + 2)) << 16);
        if (been_here_before) {
            m->cpu.regs[NGPC_XSP] = kBiosBootXsp;   /* a system call needs a stack */
        }
    }
    m->scanline = m->frame_count = m->cycle_residue = 0;
    m->irq_pending = 0;
    m->power_nmi_count = 0;
    m->adc_busy = false;
    m->adc_cycles_remaining = 0;
    for (unsigned i = 0; i < 4; ++i) { m->timer_count[i] = 0; m->timer_clock[i] = 0; }
    m->ti0_pending_pulses = 0;
    m->z80.running = false;      /* held in reset until the main CPU writes 0x55 to 0xB8 */
    m->z80.reset();
    m->z80_port_writes = 0;
    m->apu_writes = 0;
    m->total_cycles = 0;
    /* The BIOS owns WDMOD/WDCR while it boots, and it does both things: it
     * DISABLES the counter early (WDCR=0xB1, twice) and re-arms it at the end
     * (WDMOD=0xF0) before handing the console over -- measured on the retail
     * image at 0xFF204F/0xFF215D/0xFF19B6 and 0xFF1BC0. So a power-on starts
     * with it off and the BIOS decides; a hand-off starts where the BIOS left
     * it: ARMED, the cartridge's duty from its first instruction. */
    m->watchdog_reset(reset_mode != NGPC_RESET_BIOS_BOOT);
    m->hw_reset();
    m->apu.reset();
    /* The COMMAND LATCH resets; the flash CONTENTS do not. A power cycle with the
     * cartridge still in the slot does not wipe your save, and neither does this.
     * (`reset_memory()` above reloads the cart image, so a front end that wants the
     * save back must hand it over with `ngpc_flash_restore` -- which is exactly what
     * putting the cartridge back in does.) */
    m->flash_mode[0] = m->flash_mode[1] = Machine::FlashRead;
    m->flash_step[0] = m->flash_step[1] = 0;
    /* A chip caught mid-erase by a reset is a chip that is no longer erasing: the
     * busy window is part of the command latch, not of the contents. */
    m->flash_busy_until[0] = m->flash_busy_until[1] = 0;
    m->flash_after_busy[0] = m->flash_after_busy[1] = Machine::FlashRead;

    /* ⚡ THE LANGUAGE. `Language` (0x6F87, SDK SysWork.txt): 0 = Japanese, 1 = English,
     * read-only to the cartridge -- and READ BY 24 GAMES of the corpus. A dual-language
     * cartridge picks its script from this byte and nothing else.
     *
     * ⚠️ WHO OWNS IT DEPENDS ON HOW YOU BOOTED, because the two modes are two different
     * machines to configure:
     *
     *   * CONSOLE BOOT runs the real BIOS, which HAS a setup screen. That screen is the
     *     console's own control panel: what the player sets there is written into
     *     battery RAM and kept (`commit_system_ram` saves that page live). Stamping a
     *     setting over it would make the BIOS screen a decoration -- you would change
     *     the language on it and watch the emulator undo the choice at the next launch.
     *     So here we write nothing: the coin cell answers.
     *   * THE HAND-OFF skips that screen entirely. Nothing would ever write the byte,
     *     and its power-on value is 0 -- Japanese, chosen by nobody. The setting is the
     *     only control panel this mode has, so it lands here.
     *
     * A first console boot on a blank cell still stops at the BIOS setup, which is where
     * the choice belongs on that path; it is now SAVED, which it was not before. */
    if (apply_bios_handoff) {
        m->mem[kSysLanguage] = m->language_code;

        /* State the real BIOS leaves for the cart at entry. Without XSP a real
         * ROM cannot execute its first instruction (it is a CALL).
         *
         * The pointer registers are MEASURED ON SILICON (see machine.hpp). Handing
         * the cart eight zeros is not a neutral default: Puyo Pop's init loop clears
         * memory through XIX, and on hardware XIX points harmlessly into BIOS ROM.
         * With zero it swept the I/O page instead and killed the timers. */
        m->cpu.regs[NGPC_XSP] = kBiosHandoffXsp;
        m->cpu.regs[NGPC_XIX] = kBiosHandoffXix;
        m->cpu.regs[NGPC_XIY] = kBiosHandoffXiy;
        m->cpu.regs[NGPC_XIZ] = kBiosHandoffXiz;
        m->cpu.regs[NGPC_XWA] = kBiosHandoffXwa;
        m->cpu.regs[NGPC_XBC] = kBiosHandoffXbc;
        /* XDE and XHL are deliberately NOT seeded: two flashes gave two different
         * values, so they are BIOS scratch and no cartridge can depend on them. */
        /* ⚡ INTE45 = 0xDC -- INT4 (VBlank) at level 4, INT5 at level 5.
         *
         * MEASURED off the real BIOS boot (pass 237): this is what it leaves armed
         * before it jumps to the cartridge. It matters now that VBlank's level is
         * READ from this register instead of being hardcoded: a cartridge that never
         * writes INTE45 -- and several do not -- would otherwise inherit level 0,
         * which the chip reads as "interrupt prohibited", and never see a VBlank at
         * all. The BIOS arms it precisely so the cart does not have to. */
        m->mem[0x000071]      = kBiosHandoffInte45;
        m->cpu.iff_level      = kBiosHandoffIffLevel;
        m->cpu.rfp            = 0;
        m->seed_user_vector_table();

        /* ⚡ THE SAVE. What the BIOS learnt about the cartridge at power-on.
         *
         * A game does not talk to the flash chip: it calls the BIOS (`swi 1` with
         * RW3 = VECT_FLASHWRITE / VECT_FLASHERS), and the BIOS's routine reads this
         * byte before it does anything else. Zero means "no cartridge" and it returns
         * the error 0xFF having touched nothing -- which is EXACTLY what happened to
         * every save this emulator ever took, even after the flash chip below became
         * real. The chip was right and nobody was reaching it.
         *
         * The hand-off exists to leave the cart the state the BIOS boot would have,
         * and this is part of that state, no different from XSP or INTE45. */
        m->mem[kBiosFlashCardType0] = m->flash_size_code(0);
        m->mem[kBiosFlashCardType1] = m->flash_size_code(1);

        /* The BIOS's boot screen, still in character RAM -- see machine.hpp. Empty
         * when no BIOS is attached, and then the cart gets the zeros it always got:
         * we seed what we MEASURED, never a stand-in for it. */
        if (!bios_char_ram.empty())
            std::memcpy(&m->mem[kCharRamBase], bios_char_ram.data(), kCharRamSize);
    }
    m->cpu.banks[m->cpu.rfp][NGPC_XSP] = m->cpu.regs[NGPC_XSP];
}

/* --- the interrupt controller ---------------------------------------------
 * Raise VBlank when the raster crosses out of the visible area, and deliver it
 * BETWEEN instructions (never inside one -- the block instructions loop inside
 * the opcode and silicon cannot interrupt them either).
 *
 * Gate and frame, both from the Toshiba CPU manual, both previously off by one
 * in this project (retracted 2026-07-10, passes 183-184):
 *   - a level-L interrupt is accepted when **L >= IFF**, not `L > IFF`;
 *   - on acceptance the mask becomes **min(L + 1, 7)**, not L;
 *   - SR is pushed FIRST and PC SECOND, so PC ends up on top -- which is what
 *     RETI pops first;
 *   - the jump is INDIRECT, through the hardware vector table at 0xFFFF00.
 * VBlank is index 11 (slot 0xFFFF2C) at level 4, and the K2GE only raises it
 * while bit 7 of its control register (0x8000) is set. */
static void advance_raster(ngpc::Machine& m, uint16_t cycles) {
    using namespace ngpc;
    m.cycle_residue += cycles;
    while (m.cycle_residue >= kCyclesPerScanline) {
        m.cycle_residue -= kCyclesPerScanline;

        /* ⚡ THE LINE THAT JUST ENDED IS NOW DRAWN -- with the VRAM as it stands at this
         * instant, not as it will stand at the end of the frame. A scrolling game streams
         * tiles in mid-frame (often by DMA on the horizontal blank), so the top of the
         * screen legitimately shows older data than the bottom. Composing the whole frame
         * from the final snapshot tears a band through the tilemap. See render.cpp. */
        m.render_scanline(m.scanline);

        const bool was_vblank = m.in_vblank();
        if (++m.scanline >= kScanlinesPerFrame) { m.scanline = 0; ++m.frame_count; }

        /* H-INT, pulsed ON THIS RASTER'S CLOCK. ngpcspec.txt: "The signal
         * generation begins 1 H before the Hardware Drawing Period starts.
         * (Please be aware H_INT signal is not generated at line 151 and signal
         * generation for the 0th line occurs at the beginning of line 198.)"
         * So the TI0 pin pulses at the START of lines 198 and 0..150 -- 152 per
         * frame ("152 Hint occur every time", K2GETechRef 4-5-2), each one a
         * full line AHEAD of the line it announces: that whole line is the
         * silicon's safety margin for a scroll-split handler.
         *
         * This used to be derived inside timer_tick from a PRIVATE cycle
         * accumulator. Two faults, both measured on Metal Slug: its phase
         * against the raster was whatever history left it at (here: exactly ON
         * a line boundary, so the ~50-cycle delivery quantisation flipped the
         * game's split line back and forth -- the HUD's top line flickered);
         * and every IRQ delivery advanced the raster 13 cycles the private
         * accumulator never saw, so the phase also DRIFTED a full line every
         * few frames. The pin belongs to the K2GE: it pulses on the K2GE's own
         * line, not on a copy of it. */
        if (m.scanline == kScanlinesPerFrame - 1 || m.scanline <= kVisibleScanlines - 2)
            ++m.ti0_pending_pulses;

        if (!was_vblank && m.in_vblank()) {          // the visible->VBlank edge
            if (m.read8(kK2geControlAddress) & 0x80) // the source-enable gate
                m.irq_pending |= 1u << kIrqVectorIndexVBlank;
        }
        /* The raster registers the game polls. */
        m.mem[kK2geRasterAddress] = uint8_t(m.scanline);
        m.mem[kK2geStatusAddress] = m.in_vblank() ? 0x40 : 0x00;   // BLNK

        /* Freeze the display registers this line will be drawn with. A write made
         * DURING a line takes effect on the next one (Tech Ref caution on 0x8030 and
         * 0x8032), so the values standing as the line opens are exactly its own. */
        m.snapshot_raster_line(m.scanline);
    }
}

/* The sources this core raises, with their priority levels. The vector INDEX is
 * the entry in the CPU's hardware vector table at 0xFFFF00; the LEVEL is what
 * the IFF mask is compared against. Highest level wins; ties go to the lower
 * vector index, which is the datasheet's own priority order. */
static const unsigned kIrqSourceIndices[] = {
    ngpc::kIrqVectorIndexInt0,       /* the POWER button: it is what wakes the BIOS */
    /* The calendar chip's alarm. A source missing from THIS list can never be delivered
     * however correctly it is raised -- which is exactly why the alarm did nothing for
     * so long: the vector existed, the BIOS had a handler for it, and nobody looked. */
    ngpc::kIrqVectorIndexRtcAlarm,
    ngpc::kIrqVectorIndexInt5,       /* the SOUND CPU interrupting the main one */
    ngpc::kIrqVectorIndexVBlank,
    ngpc::kIrqVectorIndexIntT0, ngpc::kIrqVectorIndexIntT0 + 1,
    ngpc::kIrqVectorIndexIntT0 + 2, ngpc::kIrqVectorIndexIntT0 + 3,
    ngpc::kIrqVectorIndexIntAd,
    /* Serial channel 0 == the link cable. Absent from this list, INTTX0/INTRX0
     * could be raised correctly and still never reach the BIOS COM handlers -- the
     * same trap the RTC alarm sat in. Level-gated by INTES0 (0x77); a machine with
     * the link disabled never raises them, so this is inert until a cable is set up. */
    ngpc::kIrqVectorSerialReceive, ngpc::kIrqVectorSerialTransmit,
    /* Micro-DMA transfer-end (INTTC0..3): a channel raises these when its DMAC hits 0,
     * and a game re-arms the channel (and, for Ogre Battle, resets the scroll split) from
     * the completion ISR. micro_dma_service never matches a channel on THESE vectors, so
     * they fall through the DMA step and vector the CPU normally, level-gated by 0x79/0x7A. */
    ngpc::kIrqVectorIndexIntTc0,     ngpc::kIrqVectorIndexIntTc0 + 1,
    ngpc::kIrqVectorIndexIntTc0 + 2, ngpc::kIrqVectorIndexIntTc0 + 3,
};

/* The level a source is delivered at. VBlank is fixed; everything else reads its
 * PROGRAMMED level out of an INTxx nibble, and a level of 0 means software has
 * DISABLED that source. See machine.hpp. */
static uint8_t irq_level_of(const ngpc::Machine& m, unsigned index) {
    using namespace ngpc;
    IrqPriorityReg reg;
    if (!irq_priority_register(index, reg)) return 0;
    const uint8_t raw = m.read8(reg.address);
    const uint8_t level = uint8_t((reg.high_nibble ? (raw >> 4) : raw) & 0x07);
    /* TMP95C061 SFR table (p.184): the three level bits encode 1..6, and **BOTH 000
     * AND 111 mean "Prohibit interrupt request"**. Treating 7 as a level -- which
     * this core did -- lets a source software has explicitly SHUT OFF fire anyway. */
    if (level == 0 || level == 7) return 0;
    return level;
}

static bool deliver_irq(ngpc::Machine& m) {
    using namespace ngpc;

    /* ⭐⭐ THE INTERRUPT MAY NOT BE FOR THE CPU AT ALL — AND THE LEVEL DOES NOT GATE IT.
     *
     * If a micro-DMA channel is armed on a vector, the request drives a DMA transfer and
     * the processor never sees it. That is the raster scroll: timer 0 is clocked by the
     * HORIZONTAL BLANK (T01MOD = 0), matches at the split line, and the DMA copies the
     * next scroll value from a table into 0x8032 without a single CPU instruction.
     *
     * ⛔ THIS RAN AFTER THE LEVEL GATE, AND SO IT NEVER RAN AT ALL. A source whose
     * interrupt level is 0 was dropped as "disabled" before anyone asked whether a DMA
     * was waiting on it -- and a game doing a raster split sets EXACTLY THAT: Puyo Pop
     * leaves INTET01 = 0 (no CPU interrupt wanted, thank you) and arms micro-DMA 0 on
     * vector 0x10 with a destination of 0x8032. The level gates delivery TO THE CPU; the
     * DMA controller is a DIFFERENT CONSUMER and is not behind that gate.
     *
     * The evidence is the GAME's own configuration: if level 0 killed the DMA too, Puyo
     * Pop's split could never have worked on the silicon it shipped on.
     *
     * Delivering such a request to the CPU instead sends it into a BIOS stub that jumps
     * through a user hook nobody installed, lands at address 0, hits the `swi 7` there,
     * and the BIOS powers the console off. Ten ROMs did exactly that.
     * See specs/MICRO_DMA.md. */
    bool dma_ran = false;
    for (unsigned index : kIrqSourceIndices) {
        if (!(m.irq_pending & (uint64_t(1) << index))) continue;
        if (m.micro_dma_service(index)) {
            m.irq_pending &= ~(uint64_t(1) << index);   /* consumed -- the CPU is not disturbed */
            dma_ran = true;
        }
    }

    /* Le bus a servi le transfert : cette duree existe pour la machine entiere, pas
     * seulement pour le CPU. On l'avance comme l'entree en interruption juste apres. */
    if (m.dma_cost_cycles) {
        const uint32_t c = m.dma_cost_cycles;
        m.dma_cost_cycles = 0;
        advance_raster(m, uint16_t(c));
        m.adc_tick(c); m.rtc_step(c); m.timer_tick(c);
        m.serial_tick(c); z80_tick(m, c); m.apu.tick(c);
        m.total_cycles += c;
    }

    unsigned best_index = 0;
    uint8_t  best_level = 0;
    bool     found = false;
    for (unsigned index : kIrqSourceIndices) {
        if (!(m.irq_pending & (uint64_t(1) << index))) continue;
        const uint8_t level = irq_level_of(m, index);
        if (level == 0) continue;                                   // source disabled
        if (level < m.cpu.iff_level) continue;                      // L >= IFF
        if (!found || level > best_level) { found = true; best_index = index; best_level = level; }
    }
    if (!found) return false;
    (void)dma_ran;

    ngpc_cpu_t& c = m.cpu;
    const uint16_t sr = uint16_t(c.flags)
                      | uint16_t((c.rfp & 0x03) << 8)
                      | uint16_t(1u << 11)                           // MAX
                      | uint16_t((c.iff_level & 0x07) << 12)
                      | uint16_t(1u << 15);                          // SYSM

    c.regs[NGPC_XSP] -= 2;
    store(m, nullptr, c.regs[NGPC_XSP], sr, 2);
    c.regs[NGPC_XSP] -= 4;
    store(m, nullptr, c.regs[NGPC_XSP], c.pc, 4);

    c.iff_level = uint8_t(best_level + 1 > 7 ? 7 : best_level + 1);
    c.pc = m.read32(kIrqVectorTableBase + 4u * best_index);
    m.irq_pending &= ~(uint64_t(1) << best_index);
    /* Log the delivery with its raster position. An interrupt is half of every raster
     * effect -- seeing the register writes without the IRQ that triggered them shows
     * the symptom and hides the cause. `addr` carries the vector index. */
    if (m.elog_lo <= m.elog_hi) m.note_event(ngpc::Machine::kEventIrq, best_index, 0, c.pc);
    return true;
}

/* The POWER NMI. Non-maskable: it does NOT consult the level gate (that is the whole
 * point of an NMI, and the BIOS idle loop sits with INT0 disabled, so a maskable pulse
 * would be thrown away -- the bug the old INT0 prototype hit). Vector index 8 -> the
 * table entry at 0xFFFF20, which the BIOS fills with its power/boot handler (0xFF1898).
 * That handler validates the cartridge and hands off to it. */
static void deliver_nmi(ngpc::Machine& m) {
    using namespace ngpc;
    ngpc_cpu_t& c = m.cpu;
    const uint16_t sr = uint16_t(c.flags)
                      | uint16_t((c.rfp & 0x03) << 8)
                      | uint16_t(1u << 11)
                      | uint16_t((c.iff_level & 0x07) << 12)
                      | uint16_t(1u << 15);
    c.regs[NGPC_XSP] -= 2;
    store(m, nullptr, c.regs[NGPC_XSP], sr, 2);
    c.regs[NGPC_XSP] -= 4;
    store(m, nullptr, c.regs[NGPC_XSP], c.pc, 4);
    c.iff_level = 7;                                    // NMI runs at the top priority
    c.pc = m.read32(kIrqVectorTableBase + 4u * 8u);     // idx 8 = 0xFFFF20 -> 0xFF1898
}

/* The watchdog just crossed its period. Count it wherever it happened -- the
 * counter runs during HALT and inside interrupt entry too, which is exactly
 * where a starve hides -- and answer whether this ends the batch. It only does
 * for a caller that asked for a gate; by default this is a diagnostic and the
 * ROM keeps running, because that is what the console does. */
static bool note_watchdog(ngpc::Machine& m, ngpc_summary_t& s, uint32_t pc) {
    m.note_violation(NGPC_HW_WATCHDOG, pc, ngpc::kWatchdogTimeoutCycles);
    if (!(m.hw_guard_stop & NGPC_HW_WATCHDOG)) return false;
    s.stop_status = NGPC_WATCHDOG_RESET;
    s.stop_pc     = pc;
    s.stop_opcode = m.read8(pc);
    return true;
}

NGPC_API int ngpc_run(ngpc_t* h, uint32_t max_instrs,
                      ngpc_record_t* out_records, uint32_t records_cap,
                      ngpc_summary_t* out_summary) {
    if (!h) return -1;
    Machine* m = reinterpret_cast<Machine*>(h);

    ngpc_summary_t s;
    std::memset(&s, 0, sizeof(s));
    s.stop_status = NGPC_COUNT_REACHED;

    /* "Something crossed the cable during THIS call" -- so the question is asked
     * fresh each time. Cleared even when nobody armed the break, otherwise the
     * first armed call would answer for traffic that happened before it. */
    m->serial_event = false;

    ngpc_record_t scratch;
    for (uint32_t i = 0; i < max_instrs; ++i) {
        /* Breakpoints are checked HERE, in the core. The Python shell does it by
         * dropping its batch size to 1, which under a native core would mean one
         * FFI crossing per instruction (~292 ns) and would erase the speedup.
         * See CPP_CORE_PORT.md §4 hazard 7. */
        if (!m->breakpoints.empty()) {
            bool hit = false;
            for (uint32_t bp : m->breakpoints) if (bp == m->cpu.pc) { hit = true; break; }
            if (hit && i > 0) { s.stop_status = NGPC_BREAKPOINT; s.stop_pc = m->cpu.pc; break; }
        }

        const bool want_record = out_records && s.emitted < records_cap;
        ngpc_record_t* rec = want_record ? &out_records[s.emitted] : &scratch;

        const uint32_t pc_before = m->cpu.pc;
        if (m->coverage_on) m->note_exec(pc_before);
        const uint32_t sp_before = m->callstack_on ? m->cpu.regs[7] : 0u;
        m->fetch_window = pc_before;   // fetch bytes read in read8() get the cheap cart cost
        const uint8_t st = ngpc::step(*m, rec);
        if (m->callstack_on) m->note_control_flow(pc_before, sp_before);

        /* User XSP=0x6C00 is the exclusive top of a descending stack. The BIOS
         * legitimately uses the system page while executing in BIOS ROM, but a
         * cartridge instruction that moves XSP above the boundary is precisely
         * the corruption SysPro warns can restart or power off the console.
         *
         * Recorded on the CROSSING only: a cart that parks its stack up there is
         * one bug, not one per instruction, and the crossing's PC is the code
         * that moved it. Nothing stops unless the caller asked for a gate. */
        /* One unsigned range test, and the expensive half (cart_pc) is reached only
         * when the answer CHANGED -- which for a well-behaved ROM is never. This is
         * per-instruction code in the hot loop; the readable form of the same test
         * cost 4% of the core's throughput. */
        /* ⛔ EXECUTING OUT OF A CHIP THAT IS BUSY. See NGPC_HW_FLASH_BUSY_FETCH. Edge
         * triggered: one finding per derailment, carrying the PC that fetched first. */
        {
            const bool busy_fetch = m->pc_in_busy_flash(pc_before);
            if (busy_fetch != m->fetching_busy_flash) {
                m->fetching_busy_flash = busy_fetch;
                if (busy_fetch) m->note_violation(NGPC_HW_FLASH_BUSY_FETCH, pc_before,
                                                  m->cpu.pc);
            }
        }

        const uint32_t stack_addr = m->cpu.regs[NGPC_XSP] & kAddrMask;
        const bool stack_in_system =
            (stack_addr - (kUserStackTop + 1)) <= (kSystemRamEnd - kUserStackTop - 1);
        if (stack_in_system != m->stack_in_system
            && st == NGPC_OK && cart_pc(pc_before)) {
            m->stack_in_system = stack_in_system;
            if (stack_in_system) {
                m->note_violation(NGPC_HW_SYSTEM_STACK, pc_before, stack_addr);
                if (m->hw_guard_stop & NGPC_HW_SYSTEM_STACK) {
                    rec->status = NGPC_SYSTEM_STACK_VIOLATION;
                    s.stop_status = NGPC_SYSTEM_STACK_VIOLATION;
                    s.stop_pc     = pc_before;
                    s.stop_opcode = m->read8(pc_before);
                    if (want_record) ++s.emitted;
                    break;
                }
            }
        }

        if (st == NGPC_HALTED) {
            /* HALT is not a dead stop on real hardware: the CPU parks, the video
             * clock keeps running, and the next interrupt resumes it inside its
             * handler. Games use it as their frame barrier -- three of the corpus
             * ROMs sit on a HALT for their whole boot. A core that stops here is
             * reporting a hang that does not exist.
             *
             * So idle the machine forward a scanline at a time, ticking the raster
             * and the converter, until something is delivered. If a whole frame
             * goes by with nothing -- every source masked, or none enabled -- the
             * halt IS terminal and we say so honestly. PC still points AT the
             * halt, so the machine re-parks if the handler returns. */

            /* ⚡ THE BIOS -> CARTRIDGE HAND-OFF. On silicon the console powers into an
             * idle HALT inside the BIOS and waits for the POWER-button NMI to run its
             * boot handler. A halt with PC in BIOS space (>= 0xFF0000) IS that idle --
             * a game halts in cart space -- so the FIRST time we see it we press POWER
             * on the player's behalf, which kicks the BIOS into playing its intro and
             * running down to its final pre-boot idle at 0xFF1127.
             *
             * ⛔ FIRE EXACTLY ONCE (`== 0`, not `< 8`). A REPEATED press re-enters the
             * boot handler every ~30 frames, which resets the BIOS's own frame counter
             * (0x4E01) mid-count and bounces it around its menu forever -- the "going in
             * circles" the intro showed. One press: the intro plays, the counter reaches
             * its target, and the BIOS settles at 0xFF1127. The shell completes the final
             * step from there (PlayPage._bios_handoff_assist), because our BIOS's own
             * 0xFF1898 boot handler does not carry the last jump to the cart. */
            if (pc_before >= 0xFF0000 && m->power_nmi_count == 0) {
                ++m->power_nmi_count;                  // press POWER once
                deliver_nmi(*m);
                ++s.executed;
                continue;                              // resume inside the boot handler
            }

            bool woke = false, wd_stop = false, ser_stop = false;
            for (unsigned line = 0; line <= kScanlinesPerFrame; ++line) {
                advance_raster(*m, kCyclesPerScanline);
                m->adc_tick(kCyclesPerScanline);
                m->rtc_step(kCyclesPerScanline);
                m->timer_tick(kCyclesPerScanline);
                m->serial_tick(kCyclesPerScanline);
                z80_tick(*m, kCyclesPerScanline);
                m->apu.tick(kCyclesPerScanline);
                s.total_cycles += kCyclesPerScanline;
                m->total_cycles += kCyclesPerScanline;
                /* A HALT with the watchdog armed and no interrupt to refresh it is
                 * the classic starve: report it, and keep idling unless gated. */
                if (m->watchdog_tick(kCyclesPerScanline)
                    && note_watchdog(*m, s, pc_before)) { wd_stop = true; break; }
                if (m->irq_pending && deliver_irq(*m)) {
                    ++s.irq_deliveries;
                    /* Les memes gardes que l'autre point de livraison : le trafic de
                     * l'entree est deja dans les 18 etats documentes, et le laisser
                     * s'accumuler le ferait payer par la premiere instruction du
                     * gestionnaire. ⛔ Ce site-la n'en avait AUCUNE -- deux chemins qui
                     * livrent la meme interruption doivent la livrer pareil. */
                    m->access_wait = 0;
                    m->data_wait_cycles = 0;
                    m->biu_debt = 0;
                    woke = true;
                    break;
                }
                /* A byte can finish shifting out while the CPU is parked on a
                 * HALT, and its own INTTX0 may be masked -- so waiting for the
                 * wake-up would hold the relay for the rest of the frame. Tested
                 * after delivery: when both happen the interrupt wins and the
                 * end-of-iteration test below picks the event up instead. */
                if (m->serial_break_on_event && m->serial_event) { ser_stop = true; break; }
            }
            if (wd_stop) break;
            if (ser_stop) {
                s.stop_status = NGPC_SERIAL_EVENT;
                s.stop_pc     = pc_before;   /* still parked ON the halt */
                break;
            }
            if (!woke) {
                s.stop_status = NGPC_HALTED;
                s.stop_pc     = pc_before;
                s.stop_opcode = m->read8(pc_before);
                break;
            }
            ++s.executed;
            continue;
        }

        if (st != NGPC_OK) {
            /* Trap: the machine stops WHERE IT IS. PC is not advanced, so the
             * offending instruction can be inspected. */
            s.stop_status = st;
            s.stop_pc     = pc_before;
            s.stop_opcode = m->read8(pc_before);
            break;
        }

        ++s.executed;
        if (want_record) ++s.emitted;

        /* WHERE AN INSTRUCTION'S COST IS FINALLY SETTLED: its own cycles, scaled, plus
         * whatever the bus made it wait -- `access_wait`, accumulated in read8().
         *
         * ⚡ AND THE BUS DOES NOT SIMPLY ADD. "The instruction execution unit and the bus
         * interface unit of this CPU operate independently" (TMP95C061B 3.3.1 notes), so
         * a fetch OVERLAPS execution: the CPU stalls only when the 4-byte instruction
         * queue has run dry. That is the `fetch_pipelined` branch, and it is what lets a
         * short-instruction loop and a BIOS-call loop agree at the same time -- adding
         * the two costs could never satisfy both. `biu_slack` is how far ahead the bus
         * may run, i.e. one queue: two 16-bit words.
         *
         * ⚠️ `base_scale` is here because Toshiba's per-instruction figures are STATES
         * and a state is two of these cycles. See PERF_TIMING_POLICY.md §9ter for the
         * whole model and the provenance of every piece.
         *
         * The else branch is the pre-2026-08-21 behaviour, kept because
         * `NGPCRAFT_TIMING=legacy` and `cart_wait=0` (a fresh Machine) both need it. */
        /* Le PC n'est pas tombe en sequence : le transfert de controle a ete PRIS. */
        const bool branch_taken =
            m->cpu.pc != ((pc_before + rec->raw_len) & ngpc::kAddrMask);
        if (m->access_wait_q4) {
            /* Quarts -> cycles entiers, la retenue passant a l'instruction suivante :
             * c'est ce report qui rend un cout moyen fractionnaire possible sur une boucle. */
            const uint32_t total = m->access_wait_q4 + m->fetch_wait_carry;
            m->access_wait += total / 4u;
            m->fetch_wait_carry = total % 4u;
            m->access_wait_q4 = 0;
        }
        if (m->fetch_pipelined && m->queue_bytes && m->fetch_wait_byte_q16) {
            /* ===== LA FILE, EN OCTETS. Voir `queue_bytes` dans machine.hpp. =====
             * Trois pas, et aucun parametre libre : ce qui manque cale le processeur,
             * l'instruction consomme ses octets, puis le bus recharge pendant qu'elle
             * s'execute -- sans jamais depasser 4 octets ni un octet par 4 cycles. */
            const int32_t base = int32_t(rec->cycles) *
                (m->cycles_already_measured ? 1 : int32_t(m->base_scale));
            /* Le prix de l'octet est celui de la REGION d'ou l'instruction a ete lue
             * (cartouche ou BIOS) -- meme file, meme bus, tarif propre a chacun. */
            const int32_t bc16 = m->fetch_bc16 ? m->fetch_bc16
                                               : int32_t(m->fetch_wait_byte_q16);
            const int32_t cap16 = int32_t(m->queue_bytes) * 16;

            int32_t stall = 0;
            m->dbg_q_in = m->q_sixteenths;
            m->dbg_q_bytes = m->fetch_bytes;
            const int32_t need16 = int32_t(m->fetch_bytes) * 16 - m->q_sixteenths;
            if (need16 > 0) {
                stall = (need16 * bc16 + 128) / 256;   /* octets manquants x cout */
                m->q_sixteenths = 0;
            } else {
                m->q_sixteenths = -need16;
            }
            /* Le bus recharge pendant l'execution : e / cout_octet octets. */
            if (bc16 > 0) m->q_sixteenths += (base * 256) / bc16;
            if (m->q_sixteenths > cap16) m->q_sixteenths = cap16;
            /* Un transfert bloc tient le bus tout du long : rien n'a ete prefetche
             * derriere lui, il laisse la file VIDE (mesure : Bomberman, 4120 cy). */
            if (m->block_drains_queue && m->block_transfer_ran) m->q_sixteenths = 0;
            /* Un transfert de controle pris jette ce que la file contenait. */
            if (m->flush_queue_on_branch && branch_taken) m->q_sixteenths = 0;

            m->dbg_stall = uint32_t(stall);
            m->dbg_aw = m->access_wait;
            m->fetch_bytes = 0;
            m->fetch_bc16 = 0;
            rec->cycles = uint16_t(base + stall + m->access_wait);
        } else if (m->fetch_pipelined) {
            /* The BIU works through `access_wait` while the CPU works through the
             * instruction's own cycles; only the shortfall stalls the CPU. */
            const int32_t base = int32_t(rec->cycles) *
                (m->cycles_already_measured ? 1 : int32_t(m->base_scale));
            m->dbg_debt_in = m->biu_debt; m->dbg_aw = m->access_wait;
            m->biu_debt += int32_t(m->access_wait) - base;
            int32_t stall = 0;
            if (m->biu_debt > 0) { stall = m->biu_debt; m->biu_debt = 0; }
            m->dbg_stall = uint32_t(stall);
            const int32_t slack =
                (m->biu_slack_follows_region && pc_before >= 0xFF0000u)
                    ? int32_t(2u * m->bios_wait) : m->biu_slack;
            if (m->biu_debt < -slack) m->biu_debt = -slack;
            /* ⛔⛔ ET LE VIDAGE DE FILE SE FAIT **APRES** CETTE COMPTABILITE, PAS AVANT.
             *
             * Il etait applique avant : il jetait donc l'avance que la branche AVAIT en
             * entrant, jamais celle qu'elle CREE en s'executant. Or c'est la seconde qui
             * est fausse -- un `ret` du stub BIOS, qui ne fetche pas un octet de la
             * cartouche, se terminait avec 16 cycles d'avance que le gestionnaire en
             * cartouche depensait ensuite : ses deux premieres charges coutaient 10 et 14
             * cycles au lieu de 20. C'est exactement pourquoi le corpus etait « insensible
             * a ce reglage » -- le bouton ne pouvait rien changer.
             *
             * Mesure : une charge dans un ISR coute 20,29 cy sur console (v18 page 1) ;
             * nous en donnions 18,68. Le PC n'est pas tombe en sequence, donc la file
             * contient les mauvais octets et l'avance de l'unite de bus est perdue.
             *
             * ⚖️ MAIS PAS TOUTE. `branch_flush_keep` est le credit qui SURVIT a la
             * redirection, en cycles ; 0 = tout est jete. La file fait 4 octets, soit DEUX
             * mots : la redirection ne peut pas invalider les deux de la meme facon, le
             * mot deja engage sur le bus se termine. Le nombre vient de la ROM v13. */
            if (m->flush_queue_on_branch && branch_taken) {
                const int32_t keep = int32_t(m->branch_flush_keep);
                if (m->biu_debt < -keep) m->biu_debt = -keep;
            }
            /* ⚡ ET UN CHANGEMENT DE REGION JETTE TOUT, branche ou pas de bouton.
             * Voir `flush_on_region_change` : l'avance batie en lisant une region ne se
             * depense pas dans une autre -- le bus n'a jamais touche ces octets-la. */
            if (m->flush_on_region_change && branch_taken) {
                const bool src_cart = (pc_before >= 0x200000u && pc_before <= 0x3FFFFFu) ||
                                      (pc_before >= 0x800000u && pc_before <= 0x9FFFFFu);
                const uint32_t npc = m->cpu.pc & ngpc::kAddrMask;
                const bool dst_cart = (npc >= 0x200000u && npc <= 0x3FFFFFu) ||
                                      (npc >= 0x800000u && npc <= 0x9FFFFFu);
                if (src_cart != dst_cart && m->biu_debt < 0) m->biu_debt = 0;
            }
            /* A block transfer owned the bus for its whole run: nothing was prefetched
             * behind it, so it leaves the queue EMPTY rather than a queue ahead. */
            if (m->block_drains_queue && m->block_transfer_ran) m->biu_debt = 0;
            rec->cycles = uint16_t(base + stall);
        } else {
            rec->cycles = uint16_t(
                rec->cycles * (m->cycles_already_measured ? 1u : m->base_scale)
                + m->access_wait);
        }
        /* Surcharge inconditionnelle d'un transfert de controle PRIS -- l'hypothese
         * concurrente du credit de file : le surcout mesure ne dependrait pas de
         * l'avance accumulee, il serait paye a chaque branche prise. Les deux
         * reproduisent la ROM v13 ; seul le reste du corpus les separe. Defaut 0. */
        /* ⛔ SEULEMENT SUR DU CODE CARTOUCHE. Cette surcharge represente le cout de
         * RECHARGEMENT de la file sur le bus 8 bits de la cartouche -- elle a ete
         * mesuree la (v14 rotation C). Le BIOS et la RAM ne sont pas sur ce bus et leur
         * fetch est facture zero ici : leur faire payer un rechargement qu'ils ne
         * subissent pas surfacture tout le code du BIOS, et donc chaque interruption,
         * qui passe par son aiguillage.
         * Mesure : ROM v18, cout FIXE d'une IRQ -- silicium 111,1 cy, nous 135,6 avant
         * cette condition. */
        /* ⛔ SAUF SUR UN `reti` QUAND L'INTERRUPTION EST TRANSPARENTE. Cette surcharge
         * represente le RECHARGEMENT de la file sur le bus 8 bits de la cartouche. Or si
         * l'interruption rend au flot interrompu l'etat de bus qu'il avait
         * (`irq_transparent_queue`), le `reti` ne recharge rien : la file est rendue, pas
         * refaite. La facturer ici contredirait la transparence qu'on vient d'adopter --
         * et c'est exactement les 4 cy qui separaient notre chemin (114) des 110 de
         * l'annexe B, pour 111,5 mesures au silicium. */
        const bool reti_here = (rec->raw_len == 1 && rec->raw[0] == 0x07);
        if (m->branch_taken_extra && branch_taken &&
            !(m->irq_transparent_queue && reti_here && m->irq_save_depth) &&
            ((pc_before >= 0x200000u && pc_before <= 0x3FFFFFu) ||
             (pc_before >= 0x800000u && pc_before <= 0x9FFFFFu)))
            rec->cycles = uint16_t(rec->cycles + m->branch_taken_extra);

        /* Le temps d'acces aux donnees s'ajoute SANS passer par le recouvrement du
         * prefetch : voir `data_wait_cycles` dans machine.hpp. */
        if (m->data_wait_cycles) {
            /* ⛔ EN CARTOUCHE SEULEMENT quand `data_wait_cart_only` est arme : voir le
             * champ dans machine.hpp -- deux mesures silicium separent les deux cas par
             * la region du CODE, pas par celle des donnees. */
            if (!m->data_wait_cart_only ||
                ((pc_before >= 0x200000u && pc_before <= 0x3FFFFFu) ||
                 (pc_before >= 0x800000u && pc_before <= 0x9FFFFFu)))
                rec->cycles = uint16_t(rec->cycles + m->data_wait_cycles);
            m->data_wait_cycles = 0;
        }

        /* ⚡ LE `reti` REND AU FLOT INTERROMPU L'ETAT DE BUS QU'IL AVAIT. Voir
         * `irq_transparent_queue` : sans ca, les cycles de l'ISR lui rechargent sa file
         * et l'interruption le rend MOINS CHER, ce que le silicium refute (ROM v19). */
        if (m->irq_transparent_queue && m->irq_save_depth && reti_here) {
            --m->irq_save_depth;
            m->q_sixteenths = m->irq_q_save[m->irq_save_depth];
            m->biu_debt = m->irq_debt_save[m->irq_save_depth];
        }

        m->cycles_already_measured = false;
        m->block_transfer_ran = false;
        m->access_wait = 0;
        m->fetch_bytes = 0;
        m->fetch_bc16 = 0;
        m->fetch_window = 0xFFFFFFFFu;   // next step re-arms it before its own fetch

        s.total_cycles += rec->cycles;
        m->total_cycles += rec->cycles;   /* the machine clock the APU log timestamps against */

        /* Frame pacing and the peripherals live in the core, not across the FFI
         * seam (CPP_CORE_PORT.md §4 hazard 4): crossing it per instruction costs
         * 292 ns and would erase the whole speedup. */
        advance_raster(*m, rec->cycles);
        m->adc_tick(rec->cycles);
        m->rtc_step(rec->cycles);
        m->timer_tick(rec->cycles);
        m->serial_tick(rec->cycles);
        z80_tick(*m, rec->cycles);
        m->apu.tick(rec->cycles);
        if (m->watchdog_tick(rec->cycles) && note_watchdog(*m, s, pc_before)) break;

        /* ...and so does interrupt delivery, BETWEEN instructions. */
        if (m->irq_pending && deliver_irq(*m)) {
            ++s.irq_deliveries;
            /* ⚠️ SCALED LIKE EVERY OTHER COST. kIrqDeliveryCycles is 18 STATES
             * (datasheet 3.3.1, confirmed by Appendix B table (11)), so if the
             * instruction table is being read as states it must be too -- leaving it
             * unscaled would make an interrupt entry half price relative to the code
             * around it. Worth ~18 cycles on every received byte, which is squarely
             * inside the receive path's remaining shortfall. */
            const uint32_t irq_cost = m->base_scale *
                (m->irq_entry_cycles ? m->irq_entry_cycles : uint32_t(kIrqDeliveryCycles));
            /* ⚡ UNE INTERRUPTION VIDE LA FILE D'INSTRUCTIONS. Le PC part au vecteur :
             * les quatre octets deja lus ne servent plus, et l'avance que l'unite de bus
             * avait prise est perdue. La garder decale le timing APRES chaque ISR, et un
             * split raster ne vit que de ca -- il ne demande pas « plus vite », il demande
             * de tomber au bon endroit. */
            {
                const int32_t keep = int32_t(m->irq_flush_keep);
                if (m->biu_debt < -keep) m->biu_debt = -keep;
            }
            /* ⛔ ET LA MEME CHOSE POUR LE MODELE EN OCTETS. Le commentaire ci-dessus dit
             * « une interruption vide la file » ; il ne le faisait que pour le credit en
             * cycles. Un modele qui contredit le mecanisme que son propre commentaire
             * decrit est faux, quel que soit ce que ca donne sur le corpus. */
            if (m->irq_transparent_queue && m->irq_save_depth < 8) {
                m->irq_q_save[m->irq_save_depth] = m->q_sixteenths;
                m->irq_debt_save[m->irq_save_depth] = m->biu_debt;
                ++m->irq_save_depth;
            }
            m->q_sixteenths = m->irq_queue_keep_q16;
            m->fetch_bytes = 0;
            m->fetch_bc16 = 0;
            /* ⛔ ET LES EMPILEMENTS DE L'ENTREE NE SE FACTURENT PAS DEUX FOIS. Les quatre
             * valeurs de Toshiba pour l'acceptation d'une interruption -- 28 / 24 / 22 /
             * 18 etats -- sont indexees sur LA LARGEUR DE BUS DE LA ZONE DE PILE. Elles
             * contiennent donc deja le prix des ecritures de PC et SR. `deliver_irq` passe
             * par `store`, qui accumule `data_access_cycles` ; laisser cette accumulation
             * la ferait payer une seconde fois, sur le dos de l'instruction suivante.
             * Mesure : la ROM v8 (WORK1) est passee de -1,8 % a -6,4 % le jour ou le cout
             * d'acces a ete arme, alors que l'entree elle-meme est deja au MINIMUM des
             * quatre valeurs documentees et ne peut pas descendre. */
            m->data_wait_cycles = 0;
            /* ⛔⛔ ET `access_wait` -- C'ETAIT LE BUG, ET IL ETAIT VISIBLE A L'INSTRUCTION.
             * `access_wait` est remis a zero a la FIN d'un pas d'instruction ; la
             * livraison d'interruption se fait APRES. Or `deliver_irq` LIT LE VECTEUR,
             * quatre octets en BIOS, soit deux mots a `bios_wait` = **16 cycles** qui
             * s'accumulaient alors dans un `access_wait` que plus personne ne remettait a
             * zero -- et qui retombaient sur la PREMIERE instruction du gestionnaire.
             *
             * ⚡ MESURE : les deux `push` du stub d'aiguillage du BIOS,
             *     FF22A5  push (0x6FD6)   cy=40   access_wait=32   stall=16
             *     FF22A9  push (0x6FD4)   cy=24   access_wait=16   stall=0
             * instructions IDENTIQUES a l'adresse pres, memes acces, et deux charges
             * `bios_wait` chacune (compteur `ngpc_dbg_bios_charges`). Les 16 de plus de
             * la premiere ne venaient donc pas d'elle : ils venaient de l'entree.
             *
             * Et comme les empilements ci-dessous, ce trafic est deja dans les 18 etats
             * documentes de l'acceptation : le facturer, c'est le compter deux fois. */
            m->access_wait = 0;
            s.total_cycles += irq_cost;
            m->total_cycles += irq_cost;
            /* The cycles of an interrupt entry are cycles LIKE ANY OTHERS:
             * every peripheral clock runs through them. Only the raster used
             * to -- so each delivery slid the timers' (and the APU's, and the
             * Z80's) clock that far against the raster. At 10-15 deliveries a
             * frame that swept a whole 515-cycle line every few frames, and
             * Metal Slug's raster split beat up and down one line with it. */
            advance_raster(*m, irq_cost);
            m->adc_tick(irq_cost);
            m->rtc_step(irq_cost);
            m->timer_tick(irq_cost);
            m->serial_tick(irq_cost);
            z80_tick(*m, irq_cost);
            m->apu.tick(irq_cost);
            if (m->watchdog_tick(irq_cost)
                && note_watchdog(*m, s, pc_before)) break;
        }

        /* ⚡ THE CABLE MOVED -- hand back so the host can relay it NOW.
         *
         * Tested here, at the very end of the iteration, so the instruction is
         * fully retired first: its cycles are counted, its peripherals ticked,
         * its interrupt delivered. The machine is left on an instruction
         * boundary and the next ngpc_run resumes as if nothing had happened --
         * this is a RENDEZVOUS, not a trap. */
        if (m->serial_break_on_event && m->serial_event) {
            s.stop_status = NGPC_SERIAL_EVENT;
            s.stop_pc     = m->cpu.pc;
            break;
        }
    }

    s.scanline    = m->scanline;
    s.frame_count = m->frame_count;
    /* TI0 now pulses on the raster's own clock (see advance_raster), so there is
     * no private timer phase left to expose: these ABI fields report the raster's
     * sub-line position instead, which is the phase every peripheral now shares. */
    s.timer_hblank_cycles = m->cycle_residue;
    s.timer_hblank_line   = m->scanline;
    if (out_summary) *out_summary = s;
    return 0;
}

NGPC_API int ngpc_run_frames(ngpc_t* h, uint32_t frames, uint32_t max_instrs,
                             ngpc_summary_t* out_summary) {
    if (!h) return -1;
    Machine* m = reinterpret_cast<Machine*>(h);

    /* The frame boundary is the RASTER's, and the raster lives here. The shell
     * must never have to guess where a frame ends. */
    const uint32_t target = m->frame_count + frames;

    ngpc_summary_t total;
    std::memset(&total, 0, sizeof(total));
    total.stop_status = NGPC_COUNT_REACHED;

    uint32_t budget = max_instrs;
    while (m->frame_count < target && budget > 0) {
        /* Stop ON the frame boundary, not a burst past it.
         *
         * This used to chunk a flat 4096 instructions and check the frame counter
         * between chunks, with a comment calling that "roughly a tenth of a frame".
         * It is not: a frame is about ten thousand instructions, so 4096 is nearly
         * HALF of one, and `run_frames(N)` routinely stopped FORTY-SEVEN SCANLINES
         * past the boundary. The emulator was right; every frame-aligned comparison
         * against it was reading a state the game had already moved on from.
         *
         * So size the chunk from the CYCLES left in the frame, divided by a FAT
         * instruction. Dividing by a THIN one (a 2-cycle `nop`) is the trap: that
         * bounds the chunk's cost from BELOW, and a chunk of 2 500 average
         * instructions burns 25 000 cycles, not 5 000 -- it sails straight past the
         * boundary. Dividing by ~40 makes the chunk shrink to 1 as we approach, and
         * the crossing instruction is then the first one that actually reaches the
         * boundary -- which is as exact as an atomic instruction allows.
         * The loop still lives in the core, so this costs no FFI crossings. */
        const uint32_t cycles_per_frame = kCyclesPerScanline * kScanlinesPerFrame;
        const uint32_t cycles_done = m->scanline * kCyclesPerScanline + m->cycle_residue;
        const uint32_t cycles_left =
            cycles_done < cycles_per_frame ? cycles_per_frame - cycles_done : 1;
        uint32_t chunk = cycles_left / 40;
        if (chunk == 0) chunk = 1;
        if (chunk > 4096u) chunk = 4096u;
        if (chunk > budget) chunk = budget;
        ngpc_summary_t s;
        ngpc_run(h, chunk, nullptr, 0, &s);

        total.executed     += s.executed;
        total.total_cycles += s.total_cycles;
        total.irq_deliveries += s.irq_deliveries;
        budget -= s.executed;

        if (s.stop_status != NGPC_COUNT_REACHED) {
            total.stop_status = s.stop_status;
            total.stop_pc     = s.stop_pc;
            total.stop_opcode = s.stop_opcode;
            break;
        }
        if (s.executed == 0) break;     /* no forward progress: do not spin */
    }

    total.scanline    = m->scanline;
    total.frame_count = m->frame_count;
    if (out_summary) *out_summary = total;
    return 0;
}

/* ============================ THE CABLED PAIR =============================
 *
 * ⚡ BOTH CONSOLES AND THE CABLE, INSIDE THE CORE. Until now the core owned the
 * serial hardware but not the cable: the host ran console A for a slice, crossed
 * the FFI boundary to relay bytes, ran console B for a slice, crossed back. That
 * slice was counted in INSTRUCTIONS -- an approximation of cable time by something
 * that is not cable time -- and the study of how the Game Boy scene solved this
 * exact problem (LINK_NETPLAY_STUDY.md §4, L3) found the same answer everywhere:
 * everyone who shipped a working link put BOTH consoles and the cable in the core,
 * paced by the hardware's own serial clock. Card Fighters' Clash's versus handshake
 * and The Last Blade's threshold are not two bugs; they are two symptoms of pacing
 * a cable with an instruction count.
 *
 * ⛔ WHY AN EVENT ALONE IS NOT ENOUGH, measured 2026-08-14. `ngpc_set_serial_break`
 * hands back at the exact cycle the cable moves, which sounds like the whole answer
 * -- and as the ONLY rule it is worse than the slice it replaces: it fires on what
 * a console SENDS, so a console that is quietly computing says nothing and runs to
 * the end of its frame while its peer has not run at all. Median step measured at
 * one whole frame, which is exactly the scheduling that loses the VS handshake.
 *
 * ⚡ SO THE RULE HERE IS BOTH, AND THE SECOND HALF IS A CLOCK, NOT A COUNT: always
 * advance whichever console is BEHIND IN CYCLES, in steps bounded by a fraction of
 * the cable's own byte time, and relay whenever either console reports the cable
 * moved. The two consoles can therefore never be more than one quantum of emulated
 * time apart -- which is the property "run a slice each" never had, and the reason
 * one console could answer a byte a whole frame late in one direction.
 *
 * Determinism: the lag test breaks ties towards A, the quantum is derived from the
 * machine's own registers, and nothing here reads a wall clock. Two runs of the
 * same pair from the same state produce the same bytes in the same order. */

/* How finely the pair is interleaved, as a divisor of one byte's time on the wire.
 * The cable's own clock is the unit that matters, so this is expressed against it
 * rather than picked: at the byte time every cartridge actually programs (3200
 * cycles) it gives 400 CYCLES, where the host-side slice it replaces was 400
 * INSTRUCTIONS -- roughly ten times coarser, and in the wrong unit. */
constexpr int32_t kCableQuantumDivisor = 8;
constexpr uint32_t kCableQuantumFloor = 64;      /* cycles; a sane floor if a game
                                                  * programs an absurd baud rate  */
/* Average cycles per instruction used to turn a cycle budget into an instruction
 * budget. Same reasoning as ngpc_run_frames: divide by a FAT instruction so the
 * chunk cannot sail past its target. */
constexpr uint32_t kCyclesPerFatInstruction = 40;

namespace {

/* One relay, both directions, with the hardware handshake cross-wired.
 *
 * Each console's CTS0 pin is the OTHER console's RTS line (datasheet 3.11: RTS is
 * any GPIO wired to the peer's CTS0). A console ready to receive (RTS low) pulls its
 * peer's CTS0 low, letting the peer's CTSE-gated transmitter START a byte.
 *
 * ⚠️ BYTES ARE THEN PUSHED UNCONDITIONALLY, NOT GATED ON THE RECEIVER'S RTS — and
 * that is a decision, not an oversight. Two relays exist in this project and they
 * disagree: the shell's local two-player path pushes unconditionally, on the grounds
 * that `serial_tick` is the authoritative gate (it only PRESENTS a queued byte to the
 * CPU once that console's RTS is low, so delivering early merely queues it, exactly
 * like a real cable) and that gating here could strand a handshake byte and read to
 * the game as "no cable". `InProcessLink._relay`, which mirror netplay uses, gates.
 * Both have been validated on Card Fighters' Clash — on different core versions,
 * which is why project memory records the divergence as UNSETTLED and says to measure
 * before touching it.
 *
 * This function replaces the SHELL's local path first, so it copies the shell's rule.
 * Changing mirror netplay to come through here means settling that question with a
 * measurement, not inheriting an answer by accident. */
void relay_pair(Machine& a, Machine& b) {
    ++a.serial_relay_count;
    ++b.serial_relay_count;
    const bool a_ready = (a.mem[0x0000B2] & 0x01) == 0;
    const bool b_ready = (b.mem[0x0000B2] & 0x01) == 0;
    a.serial_cts_high = !b_ready;  a.serial_cts_seen = true;
    b.serial_cts_high = !a_ready;  b.serial_cts_seen = true;

    /* The byte stays in the SENDER's queue while the receiver says "not ready", and
     * pays a fresh byte time when it is finally released -- an RTS edge already marks
     * the cable as moved, so the release is picked up at once. Off by default. */
    const bool gate = a.relay_gates_on_rts || b.relay_gates_on_rts;
    if (!a.serial_tx.empty() && (!gate || b_ready)) {
        if (gate) b.serial_rx_cycles = b.serial_byte_cycles();
        b.serial_rx_queued_count += uint32_t(a.serial_tx.size());
        b.serial_rx.insert(b.serial_rx.end(), a.serial_tx.begin(), a.serial_tx.end());
        a.serial_tx.clear();
    }
    if (!b.serial_tx.empty() && (!gate || a_ready)) {
        if (gate) a.serial_rx_cycles = a.serial_byte_cycles();
        a.serial_rx_queued_count += uint32_t(b.serial_tx.size());
        a.serial_rx.insert(a.serial_rx.end(), b.serial_tx.begin(), b.serial_tx.end());
        b.serial_tx.clear();
    }
}

/* The interleaving quantum in cycles, taken from the cable rather than chosen. Both
 * consoles are asked and the SMALLER wins: a pair is only as coarse as its faster
 * side can afford, and two consoles with different baud rates are a case the host
 * bridge could not express at all. */
uint32_t cable_quantum_cycles(const Machine& a, const Machine& b) {
    const int32_t ba = a.serial_byte_cycles();
    const int32_t bb = b.serial_byte_cycles();
    int32_t byte_cycles = (ba < bb ? ba : bb);
    if (byte_cycles <= 0) byte_cycles = 3200;          /* the standard configuration */
    uint32_t q = uint32_t(byte_cycles / kCableQuantumDivisor);
    if (q < kCableQuantumFloor) q = kCableQuantumFloor;
    return q;
}

} // namespace

/* Advance two cabled consoles by `frames` frames each, relaying between them here.
 *
 * The two summaries are per console. A console that stops (a trap, a breakpoint, a
 * terminal halt) stops the pair: leaving its peer running against a console that is
 * no longer executing is how one screen ends up a second ahead of the other.
 *
 * The host must still enable the serial hardware on both machines first -- the cable
 * is plugged in BEFORE either console boots, which is what a game checking for a peer
 * during its own start-up requires. ABI 18. */
NGPC_API int ngpc_run_linked(ngpc_t* ha, ngpc_t* hb, uint32_t frames,
                             uint32_t max_instrs,
                             ngpc_summary_t* out_a, ngpc_summary_t* out_b) {
    if (!ha || !hb || ha == hb) return -1;
    Machine* a = reinterpret_cast<Machine*>(ha);
    Machine* b = reinterpret_cast<Machine*>(hb);

    const uint32_t target_a = a->frame_count + frames;
    const uint32_t target_b = b->frame_count + frames;

    ngpc_summary_t sa, sb;
    std::memset(&sa, 0, sizeof(sa));
    std::memset(&sb, 0, sizeof(sb));
    sa.stop_status = sb.stop_status = NGPC_COUNT_REACHED;

    /* The rendezvous is armed for the duration and restored afterwards: it is a host
     * policy everywhere else, and a linked run must not silently redefine it. */
    const bool saved_break_a = a->serial_break_on_event;
    const bool saved_break_b = b->serial_break_on_event;
    a->serial_break_on_event = true;
    b->serial_break_on_event = true;

    uint32_t budget = max_instrs;
    /* ⛔ THE HANG THIS PREVENTS. `budget` only falls as instructions retire, and a step
     * can legitimately return NGPC_SERIAL_EVENT having retired NONE: a console parked on
     * a HALT idles forward a scanline at a time, and the peer's byte arriving during that
     * idle IS an event. Treating the event as "carry on" -- which it is -- while the
     * budget does not move is an infinite loop with no exit, and the two-player window
     * would simply freeze. So rounds that retire nothing are counted, and enough of them
     * in a row ends the call even though nothing has failed. The bound is generous: a
     * real HALT is resolved by its interrupt within a line or two, so this cannot cut a
     * healthy pair short -- it exists to make the pathological case terminate. */
    unsigned idle_rounds = 0;
    constexpr unsigned kMaxIdleRounds = 512;
    /* ⛔ THE LAG IS MEASURED WITHIN THIS CALL, NOT SINCE POWER-ON. Comparing the two
     * machines' lifetime cycle counts looks equivalent and is not: the two consoles can
     * drift apart OUTSIDE this function -- the shell runs one alone whenever its peer is
     * paused, rewinding, or already holds frames in its queue, and a restored save state
     * moves one of them wholesale. With a lifetime comparison, the first steps of the
     * next call then belong entirely to whichever console is "behind", so its peer stands
     * still for that long: the whole-frame latency the interleaving exists to remove,
     * reappearing only sometimes, depending on how the frame pacer batched its work.
     *
     * MEASURED as the user reported it: the old scheduling played Card Fighters' Clash
     * fine while this one failed DIFFERENTLY EACH TRY -- white screen, frozen screen, a
     * character select that never ends. A deterministic core failing differently each
     * run means the decision depends on something outside it, and this was it.
     *
     * Counting from zero here makes each call self-contained: whatever happened before,
     * the two consoles share this frame evenly. */
    uint64_t used_a = 0, used_b = 0;
    a->serial_pair_max_gap = b->serial_pair_max_gap = 0;
    /* ⛔ A CONSOLE THAT STOPS MUST NOT TAKE ITS PEER DOWN MID-FRAME. Ending the whole
     * call on the first stop looks tidy and is wrong twice over: with a breakpoint on
     * player 1 -- an ordinary thing to do, the two-player debugger exists -- player 2
     * never ran at all (MEASURED: executed = 0), so the shell recorded a frame for it
     * that did not happen, and on screen player 2 simply froze. The arrangement this
     * replaces let the other console finish its frame, and so does this: a stopped
     * console stops being a CANDIDATE, its peer runs on to its own frame boundary, and
     * the call ends when both are done or stopped. */
    bool stop_a = false, stop_b = false;
    while (budget > 0 && idle_rounds < kMaxIdleRounds &&
           ((a->frame_count < target_a && !stop_a) ||
            (b->frame_count < target_b && !stop_b))) {
        relay_pair(*a, *b);

        /* Whichever console is behind in EMULATED TIME runs next -- ties to A, so the
         * order is fixed and two runs of the same pair agree byte for byte. A console
         * that has finished its frames, or that stopped, is no longer a candidate; its
         * peer then runs on alone, which is the only way both can land on a boundary. */
        const bool a_out = stop_a || a->frame_count >= target_a;
        const bool b_out = stop_b || b->frame_count >= target_b;
        Machine* lag;
        ngpc_summary_t* lag_sum;
        ngpc_t* lag_handle;
        bool* lag_stop;
        if (a_out) { lag = b; lag_sum = &sb; lag_handle = hb; lag_stop = &stop_b; }
        else if (b_out) { lag = a; lag_sum = &sa; lag_handle = ha; lag_stop = &stop_a; }
        else if (used_a <= used_b) {
            lag = a; lag_sum = &sa; lag_handle = ha; lag_stop = &stop_a;
        } else {
            lag = b; lag_sum = &sb; lag_handle = hb; lag_stop = &stop_b;
        }

        /* Never overshoot the peer by more than a quantum, and never overshoot the
         * frame boundary either -- the same cycles-left arithmetic run_frames uses. */
        const uint32_t quantum = cable_quantum_cycles(*a, *b);
        const uint32_t cycles_per_frame = kCyclesPerScanline * kScanlinesPerFrame;
        const uint32_t cycles_done =
            lag->scanline * kCyclesPerScanline + lag->cycle_residue;
        const uint32_t cycles_left =
            cycles_done < cycles_per_frame ? cycles_per_frame - cycles_done : 1;
        const uint32_t cycles_step = cycles_left < quantum ? cycles_left : quantum;

        uint32_t chunk = cycles_step / kCyclesPerFatInstruction;
        if (chunk == 0) chunk = 1;
        if (chunk > budget) chunk = budget;

        ngpc_summary_t s;
        ngpc_run(lag_handle, chunk, nullptr, 0, &s);

        lag_sum->executed       += s.executed;
        lag_sum->total_cycles   += s.total_cycles;
        (lag == a ? used_a : used_b) += s.total_cycles;
        const uint64_t gap = used_a > used_b ? used_a - used_b : used_b - used_a;
        if (gap > a->serial_pair_max_gap)
            a->serial_pair_max_gap = b->serial_pair_max_gap = gap;
        lag_sum->irq_deliveries += s.irq_deliveries;
        budget -= s.executed;

        /* NGPC_SERIAL_EVENT is the rendezvous, not a fault: the cable moved, so the
         * loop simply relays it on the next turn. Anything else ends the pair. */
        if (s.stop_status != NGPC_COUNT_REACHED && s.stop_status != NGPC_SERIAL_EVENT) {
            lag_sum->stop_status = s.stop_status;
            lag_sum->stop_pc     = s.stop_pc;
            lag_sum->stop_opcode = s.stop_opcode;
            *lag_stop = true;
        }
        if (s.executed == 0) {
            /* A rendezvous with nothing retired is fine once -- see kMaxIdleRounds. A
             * stop with nothing retired and no event is a console that will not move. */
            if (s.stop_status != NGPC_SERIAL_EVENT) break;
            ++idle_rounds;
        } else {
            idle_rounds = 0;
        }
    }
    /* One last relay so a byte produced by the final step is not left in a FIFO
     * until the next call -- the frame the host is about to present would be one
     * relay behind the state it reports. */
    relay_pair(*a, *b);

    a->serial_break_on_event = saved_break_a;
    b->serial_break_on_event = saved_break_b;

    sa.scanline = a->scanline; sa.frame_count = a->frame_count;
    sb.scanline = b->scanline; sb.frame_count = b->frame_count;
    if (out_a) *out_a = sa;
    if (out_b) *out_b = sb;
    return 0;
}

/* How many times the core relayed this console's cable since the link came up.
 * Zero unless ngpc_run_linked is driving the pair -- a host relaying for itself
 * counts its own pumps. See Machine::serial_relay_count. ABI v18. */
/* The widest cycle gap that opened between the two consoles during the last linked
 * call -- the honest measure of whether they were interleaved. See the header. */
NGPC_API uint64_t ngpc_link_pair_max_gap(ngpc_t* h) {
    if (!h) return 0;
    return reinterpret_cast<Machine*>(h)->serial_pair_max_gap;
}

NGPC_API uint32_t ngpc_link_relay_count(ngpc_t* h) {
    if (!h) return 0;
    return reinterpret_cast<Machine*>(h)->serial_relay_count;
}

NGPC_API void ngpc_set_write_log(ngpc_t* h, uint32_t lo, uint32_t hi) {
    if (!h) return;
    Machine* m = reinterpret_cast<Machine*>(h);
    m->wlog_lo = lo;
    m->wlog_hi = hi;
    m->wlog_count = 0;
}

NGPC_API uint64_t ngpc_write_log_count(ngpc_t* h) {
    if (!h) return 0;
    return reinterpret_cast<Machine*>(h)->wlog_count;
}

NGPC_API void ngpc_set_coverage(ngpc_t* h, int enabled) {
    if (!h) return;
    Machine* m = reinterpret_cast<Machine*>(h);
    m->coverage_on = (enabled != 0);
    m->coverage_hits = 0;
    if (enabled) {
        m->coverage.assign(Machine::kCovSpan / 8, 0);
    } else {
        m->coverage.clear();
        m->coverage.shrink_to_fit();
    }
}

NGPC_API uint32_t ngpc_coverage_hits(ngpc_t* h) {
    if (!h) return 0;
    return reinterpret_cast<Machine*>(h)->coverage_hits;
}

NGPC_API uint32_t ngpc_get_coverage(ngpc_t* h, uint8_t* out, uint32_t n) {
    if (!h) return 0;
    Machine* m = reinterpret_cast<Machine*>(h);
    const uint32_t have = uint32_t(m->coverage.size());
    if (!out || n == 0) return have;             /* size query */
    const uint32_t want = have < n ? have : n;
    for (uint32_t i = 0; i < want; ++i) out[i] = m->coverage[i];
    return want;
}

NGPC_API void ngpc_set_hygiene(ngpc_t* h, int enabled) {
    if (!h) return;
    Machine* m = reinterpret_cast<Machine*>(h);
    m->hygiene_on = (enabled != 0);
    m->hygiene_reset();
}

NGPC_API uint64_t ngpc_uninit_reads(ngpc_t* h) {
    if (!h) return 0;
    return reinterpret_cast<Machine*>(h)->uninit_reads;
}

NGPC_API uint64_t ngpc_lost_writes(ngpc_t* h) {
    if (!h) return 0;
    return reinterpret_cast<Machine*>(h)->lost_writes;
}

static uint32_t copy_hygiene(const Machine::HygieneRec* src, uint32_t have,
                             ngpc_hygiene_t* out, uint32_t n) {
    const uint32_t want = have < n ? have : n;
    for (uint32_t i = 0; i < want; ++i) {
        out[i].pc = src[i].pc;
        out[i].addr = src[i].addr;
    }
    return want;
}

NGPC_API uint32_t ngpc_get_uninit_reads(ngpc_t* h, ngpc_hygiene_t* out, uint32_t n) {
    if (!h || !out || n == 0) return 0;
    Machine* m = reinterpret_cast<Machine*>(h);
    return copy_hygiene(m->hyg_uninit, m->hyg_uninit_n, out, n);
}

NGPC_API uint32_t ngpc_get_lost_writes(ngpc_t* h, ngpc_hygiene_t* out, uint32_t n) {
    if (!h || !out || n == 0) return 0;
    Machine* m = reinterpret_cast<Machine*>(h);
    return copy_hygiene(m->hyg_lost, m->hyg_lost_n, out, n);
}

/* --- hardware safety. Counting is always on; only STOPPING is a choice. */
NGPC_API void ngpc_set_hw_guard(ngpc_t* h, uint32_t stop_mask) {
    if (!h) return;
    reinterpret_cast<Machine*>(h)->hw_guard_stop =
        stop_mask & (NGPC_HW_WATCHDOG | NGPC_HW_SYSTEM_STACK);
}

NGPC_API uint64_t ngpc_hw_violations(ngpc_t* h, uint32_t kind) {
    if (!h) return 0;
    Machine* m = reinterpret_cast<Machine*>(h);
    uint64_t n = 0;
    if (kind & NGPC_HW_WATCHDOG)     n += m->hw_watchdog_count;
    if (kind & NGPC_HW_SYSTEM_STACK) n += m->hw_stack_count;
    if (kind & NGPC_HW_FLASH_BUSY_FETCH) n += m->hw_flash_fetch_count;
    return n;
}

NGPC_API uint32_t ngpc_get_hw_violations(ngpc_t* h, ngpc_violation_t* out, uint32_t n) {
    if (!h || !out || n == 0) return 0;
    Machine* m = reinterpret_cast<Machine*>(h);
    const uint32_t want = m->hw_n < n ? m->hw_n : n;
    for (uint32_t i = 0; i < want; ++i) {
        out[i].pc     = m->hw[i].pc;
        out[i].detail = m->hw[i].detail;
        out[i].cycle  = m->hw[i].cycle;
        out[i].kind   = m->hw[i].kind;
        out[i]._pad   = 0;
    }
    return want;
}

NGPC_API void ngpc_set_event_log(ngpc_t* h, uint32_t lo, uint32_t hi) {
    if (!h) return;
    Machine* m = reinterpret_cast<Machine*>(h);
    m->elog_lo = lo;
    m->elog_hi = hi;
    m->elog_count = 0;
}

NGPC_API uint64_t ngpc_event_log_count(ngpc_t* h) {
    if (!h) return 0;
    return reinterpret_cast<Machine*>(h)->elog_count;
}

NGPC_API uint32_t ngpc_get_event_log(ngpc_t* h, ngpc_event_t* out, uint32_t n) {
    if (!h || !out || n == 0) return 0;
    Machine* m = reinterpret_cast<Machine*>(h);
    const uint64_t total = m->elog_count;
    const uint64_t held = total < Machine::kElogSize ? total : Machine::kElogSize;
    const uint32_t want = uint32_t(held < n ? held : n);
    const uint64_t first = total - want;
    for (uint32_t i = 0; i < want; ++i) {
        const Machine::EventRec& e = m->elog[(first + i) % Machine::kElogSize];
        out[i].pc = e.pc;
        out[i].addr = e.addr;
        out[i].scanline = e.scanline;
        out[i].cycle = e.cycle;
        out[i].value = e.value;
        out[i].type = e.type;
    }
    return want;
}

NGPC_API void ngpc_set_callstack(ngpc_t* h, int enabled) {
    if (!h) return;
    Machine* m = reinterpret_cast<Machine*>(h);
    m->callstack_on = (enabled != 0);
    if (!enabled) { m->call_depth = 0; m->call_overflow = 0; }
}

NGPC_API uint32_t ngpc_callstack_depth(ngpc_t* h) {
    if (!h) return 0;
    return reinterpret_cast<Machine*>(h)->call_depth;
}

NGPC_API uint64_t ngpc_callstack_overflow(ngpc_t* h) {
    if (!h) return 0;
    return reinterpret_cast<Machine*>(h)->call_overflow;
}

NGPC_API uint32_t ngpc_get_callstack(ngpc_t* h, ngpc_frame_t* out, uint32_t n) {
    if (!h || !out || n == 0) return 0;
    Machine* m = reinterpret_cast<Machine*>(h);
    const uint32_t want = m->call_depth < n ? m->call_depth : n;
    for (uint32_t i = 0; i < want; ++i) {
        const Machine::Frame& f = m->callstack[i];
        out[i].caller_pc = f.caller_pc;
        out[i].entry_pc = f.entry_pc;
        out[i].return_pc = f.return_pc;
        out[i].entry_sp = f.entry_sp;
    }
    return want;
}

NGPC_API void ngpc_set_read_log(ngpc_t* h, uint32_t lo, uint32_t hi) {
    if (!h) return;
    Machine* m = reinterpret_cast<Machine*>(h);
    m->rlog_lo = lo;
    m->rlog_hi = hi;
    m->rlog_count = 0;
}

NGPC_API uint64_t ngpc_read_log_count(ngpc_t* h) {
    if (!h) return 0;
    return reinterpret_cast<Machine*>(h)->rlog_count;
}

NGPC_API uint32_t ngpc_get_read_log(ngpc_t* h, ngpc_read_t* out, uint32_t n) {
    if (!h || !out || n == 0) return 0;
    Machine* m = reinterpret_cast<Machine*>(h);
    const uint64_t total = m->rlog_count;
    const uint64_t held = total < Machine::kRlogSize ? total : Machine::kRlogSize;
    const uint32_t want = uint32_t(held < n ? held : n);
    /* The most recent `want`, oldest first. */
    const uint64_t first = total - want;
    for (uint32_t i = 0; i < want; ++i) {
        const Machine::ReadRec& r = m->rlog[(first + i) % Machine::kRlogSize];
        out[i].pc = r.pc;
        out[i].addr = r.addr;
        out[i].value = r.value;
    }
    return want;
}

NGPC_API uint32_t ngpc_get_write_log(ngpc_t* h, ngpc_write_t* out, uint32_t n) {
    if (!h || !out || n == 0) return 0;
    Machine* m = reinterpret_cast<Machine*>(h);
    const uint64_t total = m->wlog_count;
    const uint64_t held = total < Machine::kWlogSize ? total : Machine::kWlogSize;
    const uint32_t want = uint32_t(held < n ? held : n);
    /* The most recent `want`, oldest first. */
    const uint64_t first = total - want;
    for (uint32_t i = 0; i < want; ++i) {
        const Machine::WriteRec& r = m->wlog[(first + i) % Machine::kWlogSize];
        out[i].pc = r.pc;
        out[i].addr = r.addr;
        out[i].value = r.value;
    }
    return want;
}

NGPC_API int ngpc_get_raster_log(ngpc_t* h, uint8_t* out, uint32_t n) {
    if (!h || !out) return -1;
    Machine* m = reinterpret_cast<Machine*>(h);
    const uint32_t need = NGPC_RASTER_LINES * NGPC_RASTER_REGS;
    if (n < need) return -1;
    std::memcpy(out, m->raster_log, need);
    return int(need);
}

NGPC_API void ngpc_get_cpu(ngpc_t* h, ngpc_cpu_t* out) {
    if (!h || !out) return;
    *out = reinterpret_cast<Machine*>(h)->cpu;
}

NGPC_API void ngpc_set_timer_base(ngpc_t* h, uint32_t cycles_per_phi_t1) {
    if (!h || cycles_per_phi_t1 == 0) return;
    reinterpret_cast<Machine*>(h)->timer_base = cycles_per_phi_t1;
}

/* Wait-states per byte fetched from cartridge flash. 0 = free (the old, ~3.4x-too-fast
 * behaviour). Calibrated by hw_calibration/cpu_calib_v1.ngc. See Machine::cart_wait. */
/* ONE call that arms the whole silicon timing model, so no caller has to remember
 * eight settings and no caller can arm half of them.
 *
 * The model, and where each piece comes from:
 *   base x2        Toshiba's instruction costs are STATES; a state is two periods of
 *                  fc (900/L1 manual; TMP95C061B prices a state at 80 ns at 25 MHz
 *                  while 4.3 gives tosc = 40 ns). DOCUMENTED.
 *   fetch per word the external bus is 16 bits. DOCUMENTED.
 *   pipelined      "the instruction execution unit and the bus interface unit of this
 *                  CPU operate independently" (3.3.1 notes). DOCUMENTED.
 *   slack 2 x word the instruction queue is 4 bytes = 2 words (900/L1 Table 1, the
 *                  feature list, and the LDC note -- three places). DERIVED.
 *   irq x scale    kIrqDeliveryCycles is 18 STATES, so it scales like everything else.
 *   BIOS bus       the BIOS is on the same 16-bit bus; billing the cart and letting
 *                  the BIOS fetch free was an asymmetry nothing justified.
 *   two-stage TX   SC0BUF and the shift register are separate, so INTTX0 fires when
 *                  the BUFFER frees -- at the START of a byte (3.11). DOCUMENTED.
 *   rx single      the receiver charged the byte time twice. A defect, now released.
 *
 * ⚠️ `word_wait` and `bios_wait` are the only CALIBRATED numbers. Against the silicon
 * campaign (10 / 8): a received byte costs 96.0 us against 96.2 measured, the four CPU
 * regimes land within 6%, throughput -4% and the round trip -8%. See OPEN_ITEMS.md. */
/* 33 quarts = 8,25 cycles par mot de 16 bits. Voir le bloc au point d'usage. */
static constexpr uint32_t kSiliconFetchWaitQ4 = 33;

/* ⏱️ HOW LONG THE CARTRIDGE FLASH IS BUSY, in machine cycles.
 *
 * A program or an erase takes real time on silicon, and during it the chip answers
 * status instead of contents -- which is why a flash stub runs from RAM with interrupts
 * masked. Committing inside the command's own bus cycle deletes that window and with it
 * every defect that depends on it.
 *
 * `program` is per byte, `erase_per_8k` is per 8 KB of block (a 64 KB block costs eight
 * times as much), `fail` is how long the chip tries before raising DQ5 on a program it
 * cannot do -- a 1 asked over a 0. ALL THREE ZERO restores the synchronous chip this
 * model was until 2026-09-10, which is what a corpus bisection needs.
 *
 * ⚠️ The built-in defaults are DOCUMENTED, NOT MEASURED, and our documentation
 * disagrees with itself about the erase by a factor of a hundred -- see the constants in
 * machine.hpp. hw_test_flash_timing (04_MY_PROJECTS) measures all three on the cartridge;
 * this setter is where its answer goes. */
NGPC_API void ngpc_set_flash_timing(ngpc_t* h, uint32_t program, uint32_t erase_per_8k,
                                    uint32_t fail) {
    if (!h) return;
    auto* m = reinterpret_cast<Machine*>(h);
    m->flash_program_cycles     = program;
    m->flash_erase_per8k_cycles = erase_per_8k;
    m->flash_fail_cycles        = fail;
}

NGPC_API void ngpc_set_timing_silicon(ngpc_t* h, uint32_t word_wait, uint32_t bios) {
    if (!h) return;
    auto* m = reinterpret_cast<Machine*>(h);
    m->base_scale         = 2;
    m->cart_wait          = word_wait;   /* la voie entiere ; fetch_wait_q4 la remplace */
    m->cart_data_wait     = 0;
    m->bios_wait          = bios;
    /* ⚡ AND DATA READS FROM THE BIOS COST THE SAME AS FETCHES FROM IT -- the same
     * number, not a new one. It is one chip on one bus; a read is a read.
     *
     * It matters because a BIOS service is reached by an indirect `call xix` whose
     * vector is pulled out of the table at 0xFFFE00 -- four bytes, two words, on every
     * single call. Billing that at zero made BIOS-CALL code cheap while BIOS-INTERRUPT
     * code was right, which is precisely the shape of the gap it closes: the probe's
     * polling loop went from +7% to +3% against silicon while the cart-only loops moved
     * by two counts in six thousand (they never read BIOS data) and the cost of a
     * received byte stayed inside 1%.
     *
     * ⚠️ `cart_data_wait` stays 0 for a different and MEASURED reason -- cpu_calib_v2
     * showed a cart data read costs the same as a RAM read. That was about the CART. It
     * never said anything about this chip. */
    m->bios_data_wait     = bios;
    /* ⚡ EN QUARTS DE CYCLE, PARCE QUE L'ENTIER NE SUFFIT PAS. Contre les boucles du
     * test 6 -- code cartouche pur, aucun appel COM, donc comparables -- l'optimum tombe
     * entre 9 et 10 : a 10 on est 4 % trop LENT, a 9 on est 7 % trop RAPIDE. A 9,75 :
     * REG -2 %, ROM -1 %, RAM 0 %.
     *
     * Ces 4 % n'etaient pas cosmetiques : un HUD en split raster n'a qu'une ligne de
     * marge, et un CPU legerement lent le rate quand la scene se charge -- le HUD de
     * Cool Boarders clignotait « parfois plus, parfois moins selon les circonstances »,
     * ce qui est la forme d'une marge trop juste, pas d'un defaut franc. */
    /* ⛔ REMIS A 0 (donc `cart_wait` entier), ET C'EST UNE MESURE QUI L'A DECIDE.
     *
     * 9,75 rapproche les boucles cartouche du silicium (REG -4 % -> -2 %, RAM -3 % -> 0),
     * et j'en attendais aussi plus de MARGE pour les splits raster. C'est l'inverse : sur
     * le savestate de Cool Boarders ou le perso heurte un mur en continu, le HUD saute
     * d'une trame **13 fois sur 900** a 9,75 et **0 fois** a 10. Le detecteur est
     * `scratchpad/cb5.py` : il compte, ligne par ligne, les trames qui different de leurs
     * deux voisines -- et les 13 tombent toutes sur la ligne 8, la frontiere du split.
     *
     * ⇒ Un split raster ne demande pas « plus vite », il demande de tomber au bon endroit.
     * Deux pour cent de precision sur un banc ne valent pas un HUD qui clignote. */
    /* ⚖️ LE QUART DE CYCLE EST MESURE, PAS DEVINE (ROM a_irq_calib_v8, silicium,
     * 2026-08-23). L'attente entiere ne peut pas encadrer le silicium : mot=9 donne
     * 238 lots, mot=8 en donne 269, et la console en fait **261**. Les trois grandeurs
     * de la ROM concordent sur ~8,3 (8,26 / 8,42 / 8,34), et 8,25 les reproduit a moins
     * de 2 % : 262/222/251 contre 261/218/249 mesures.
     *
     * ⛔ Un quart avait deja ete essaye le 22/08 (9,75) et RETIRE, parce qu'il aggravait
     * le HUD de Cool Boarders. C'etait un reglage a l'oreille sur un jeu ; celui-ci vient
     * d'un tir silicium avec RASV=198 et des chiffres stables. La difference n'est pas la
     * valeur, c'est d'ou elle vient. */
    m->fetch_wait_q4      = kSiliconFetchWaitQ4;
    /* ⚖️ ARME AU NOMINAL, ET LE SILICIUM L'A CONFIRME (ROM a_dma_calib_v9, tir du
     * 24/08 : DMAC descendu de 9120, soit 152 transferts par trame, RASV=198).
     *
     *   transfert d'octet -- silicium **12,9 cycles**, nous **13,0** au nominal ✅
     *   mode compteur     -- silicium **5,2**, nous 10,4 : le x2 etait de trop, retire.
     *
     * Il etait reste a 0 le 23/08 faute de mesure, et c'etait la bonne decision : a
     * l'aveugle il ralentissait Fatal Fury de 4,3 % alors que des jeux etaient deja
     * signales trop lents. Une journee d'attente contre un chiffre invente. */
    m->micro_dma_states   = 8;
    m->access_wait_q4     = 0;
    m->fetch_wait_carry   = 0;
    m->fetch_wait_per_word = true;
    /* ⚡ FETCH FACTURE PAR OCTET, ET C'EST LA FORME QUI COMPTE AUTANT QUE LA VALEUR.
     * Le bus cartouche est 8 bits : le prix d'un fetch suit les OCTETS. Facturer par
     * MOT rendait le cout d'une instruction dependant de sa PARITE -- 5 octets payaient
     * 3 charges en partant d'une adresse paire, 2 en partant d'une impaire -- et c'est
     * ce qui rendait le modele sensible a l'adresse la ou le silicium ne l'est pas
     * (ROM v12 : 682/682/683/682 sur console, 715/733/732/732 chez nous).
     * Valeur mesuree DIRECTEMENT par la ROM v14 page 1 : 4,03 cy/octet sur une droite
     * qui ferme a 0,35 %. 64 seiziemes = 4,00, ce que la structure predit aussi
     * (2 cycles de bus x 2 etats x 2 cycles = 8,00 cy/mot). */
    m->fetch_wait_byte_q16 = 64;
    /* Surcharge d'une branche PRISE, en cycles. Mesuree sur la rotation C de la v14
     * page 0 -- branche prise file VIDE, donc sans part conditionnelle : silicium
     * 16,3 cy/branche contre 11,3 chez nous. Le corpus des 26 cases confirme
     * l'optimum a 4 (ecart moyen 1,30 %, contre 2,01 % a 5 et 2,74 % a 6).
     * ⛔ Le fetch par octet SEUL degrade le corpus (7,13 %) : les 8,25 cy/mot d'avant
     * sur-facturaient le bus pour compenser cette branche non facturee. Les deux
     * corrections vont ENSEMBLE ou pas du tout. */
    m->branch_taken_extra = 4;
    /* Cout FIXE d'un acces memoire de donnee. Mesure par la ROM v15 (pages 1 et 2) :
     * ~4,05 cy par ACCES, identique en lecture et en ecriture, et INDEPENDANT de la
     * largeur -- une lecture d'un octet et une de deux coutent le meme prix sur console
     * (215 contre 216 comptes). ⛔ Une premiere version facturait par OCTET, calee sur la
     * seule largeur que la v14 avait mesuree : la v15 l'a refutee en une ligne. C'etait
     * la derniere case du corpus qui restait a plus de 6 % (`MEM`). */
    m->data_access_cycles = 4;
    /* ⚡ ET CE COUT NE SE PAIE QUE DANS DU CODE CARTOUCHE (ROM v19, tir silicium).
     *
     * Deux mesures se contredisaient sous une regle uniforme : la boucle `MEM` du corpus
     * EXIGE ces 4 cy (silicium 65,3 cy/tour ; nous 62 sans, 66 avec) et le chemin d'une
     * interruption les REFUSE (silicium 111,5 ; annexe B 110 ; nous 130 avec, 114 sans).
     * Les deux ecrivent en RAM : ce n'est donc pas la region des DONNEES qui les separe,
     * c'est celle du CODE -- `MEM` est en cartouche, le stub d'interruption est en BIOS.
     *
     * ⚖️ Et c'est un MECANISME : si ce cout est une contention -- l'acces de donnee vole
     * un cycle de bus au prefetch -- il ne mord que la ou le fetch est cher, le bus
     * 8 bits de la cartouche. Meme regle, meme raison que `branch_taken_extra`.
     * Corpus : 0,40 % -> 0,32 % ; chemin d'IRQ 130 -> 114 pour 111,5 mesures. */
    m->data_wait_cart_only = true;
    /* ⚡ ET UNE INTERRUPTION EST TRANSPARENTE POUR L'ETAT DE BUS DU FLOT INTERROMPU.
     *
     * ⛔ Sans ca, les cycles de l'ISR RECHARGENT la file du code interrompu : une
     * interruption le rendait MOINS CHER (-0,574 cy/instruction), soit ~17 cy de
     * ristourne par interruption -- qui compensaient exactement la sur-facturation
     * ci-dessus. Deux erreurs de signes opposes : c'est pour ca qu'aucun balayage a un
     * bouton n'a jamais converge en deux campagnes.
     *
     * ⚡ LE SILICIUM LE TRANCHE (ROM v19) : le cout d'une interruption est PLAT selon que
     * la boucle interrompue soit limitee par le bus ou par l'execution -- 112,6 / 112,0 /
     * 110,7 / 110,5, contraste **+1,5**, quand nos modeles predisaient +18,0 et +11,6.
     * Arme, notre contraste tombe a **-1,2**. */
    m->irq_transparent_queue = true;
    /* ⚡ ET UN TRANSFERT DE CONTROLE PRIS QUI CHANGE DE REGION JETTE L'AVANCE.
     *
     * Le `ret` du stub BIOS finissait avec 16 cycles d'avance -- batie en lisant des
     * octets de BIOS -- que les deux premieres charges du gestionnaire en CARTOUCHE
     * depensaient : 10 et 14 cycles au lieu de 20. Le bus n'a jamais touche ces
     * octets-la ; il ne connaissait meme pas l'adresse du gestionnaire.
     *
     * ⚖️ ET PAS `flush_queue_on_branch` TOUT COURT : une branche qui reste dans la meme
     * region garde legitimement une part de son avance (v14, `branch_flush_keep` ~13-14),
     * et vider a chaque branche casse le corpus (0,31 % -> 2,85 %). Conditionne au
     * CHANGEMENT de region, le corpus ne bouge pas d'un centieme (0,31 % / 1,89 %)
     * pendant qu'une charge dans un ISR passe de 18,00 a 19,25 cy mesures, pour 20,29
     * sur console (v18 page 1), et la pente derivee de 18,68 a 19,62. */
    m->flush_on_region_change = true;
    /* ⚡ L'ÉTRANGLEMENT VRAM DU K2GE, ENFIN CHIFFRÉ (ROM v3, tir silicium).
     *
     * Il etait livre a 0 depuis 2026-07 avec la mention « effet confirme, cout non
     * epingle -- on ne livre pas un chiffre invente ». Le tir existait pourtant :
     * VWR **452** contre MEM **471**. A 0 nous rendions VWR = **503**, soit +11 % -- et
     * pire, une ecriture VRAM y coutait MOINS qu'une ecriture RAM (elle est exclue de
     * `charge_data_access`). A 9 cycles par octet en affichage actif : VWR = **452**,
     * au compte pres, MEM inchange a 471. Balayage monotone, valeur unique.
     *
     * ⛔ NE SE PAIE PAS PENDANT UN TRANSFERT BLOC -- voir `in_block_copy`. `ldirw_cost`
     * = 18 a ete mesure contre le copieur HiColor de Bomberman, qui ecrit justement en
     * VRAM, avec une fenetre d'UN cycle : ce cout contient deja l'etranglement. Sans
     * cette garde, `test_bomberman_hicolor_phase` tombe -- le copieur derive de sa
     * tranche de 4120 cycles et corrompt une ligne par bande. */
    /* ⚡ ET IL SE PAIE PAR ACCES, PAS PAR OCTET (ROM v20 page 3, tir silicium) : une
     * ecriture MOT en VRAM coute **2,95 cy** de plus qu'en RAM, et une ecriture OCTET
     * **2,95** aussi -- rapport **1,00**. Meme refutation que `data_wait_q16` par la v15.
     * ⚖️ La v3 ne pouvait pas trancher : elle n'ecrit que des OCTETS, ou les deux formes
     * coincident. C'est la double difference de la v20 (les memes ecritures refaites en
     * RAM) qui separe l'etranglement du cout propre de l'instruction.
     * Valeur : 10 equilibre les DEUX tirs (v3 VWR -0,9 %, v20 V8B +1,3 %) la ou 9 collait
     * a la v3 (0,0 %) mais laissait la v20 a +3,9 %. */
    m->vram_wait          = 10;
    /* La file d'instructions, MODELISEE EN OCTETS -- 4, la taille documentee. Elle
     * remplace le credit d'avance en cycles (`biu_slack`), qui etait UN scalaire pour
     * trois regimes et que trois mesures tiraient dans trois directions. Le modele en
     * octets n'a aucun parametre libre et rend 26,6 cy/division sur le montage v16
     * page 0, contre 26,5 mesures sur console (le credit en cycles rendait 16,0). */
    /* ⚡ LA FILE, MODELISEE EN OCTETS -- 4, la taille documentee. Elle remplace le
     * credit d'avance en CYCLES (`biu_slack`), qui etait UN scalaire pour trois regimes
     * et que trois mesures tiraient dans trois directions incompatibles.
     *
     * Le modele n'a AUCUN parametre libre : la file fait 4 octets, le bus en livre un
     * tous les 4 cycles, et c'est tout. Sur le montage v16 page 0 il rend 26,6
     * cy/division contre **26,5 mesures** ; le credit en cycles rendait 16,0.
     *
     * ⛔ IL NE S'ARME PAS SEUL. Arme au-dessus des couts MOT de `mul`/`div` herites du
     * fetch a 10 cy/mot, il laissait MUL a -9 %, DIV a -6 % et cassait l'ancrage
     * Bomberman -- ce qui ressemblait a une refutation du modele alors que c'etaient les
     * deux constantes qui etaient fausses. La ROM v17 les a mesurees (47 et 15).
     *
     * ⛔ ET MEME AVEC ELLES, IL RESTE DESARME (0). Avec le triplet complet le corpus
     * tombe a 1,5 % et MUL/DIV rentrent (+2 %), mais TOUTES les cases restent a -1 a
     * -2,3 % : la machine entiere est ~1,5 % trop lente, et ca suffit a decaler la
     * phase du copieur de Bomberman (dont la marge est d'UNE ligne) et a casser le HUD
     * de Cool Boarders. Il manque encore une piece que le modele ne facture pas.
     * ⇒ Ne PAS armer avant que ce biais uniforme soit explique -- pas compense, explique.
     *   `ngpc_set_queue_bytes(4)` l'arme pour la mesure. */
    m->queue_bytes        = 0;
    m->fetch_pipelined    = true;
    m->biu_debt           = 0;
    /* ⚠️ LA MARGE SUIT LE COUT REEL DU MOT, PAS LE PARAMETRE ENTIER. La file fait
     * 4 octets = 2 mots ; sa valeur en cycles est donc 2 x le cout d'un mot, et depuis
     * le recalage silicium ce cout est `fetch_wait_q4 / 4` (8,25), pas `word_wait` (10).
     * Garder l'entier laissait la file croire qu'elle pouvait prendre 20 cycles d'avance
     * la ou elle n'en a que 16 -- une incoherence introduite par le recalage lui-meme. */
    m->biu_slack          = int32_t((2u * kSiliconFetchWaitQ4) / 4u);
    /* ⛔ CES DEUX-LA NE SONT PAS DOUBLES, ET LES DOUBLER ETAIT UNE VRAIE FAUTE.
     *
     * Le reste du modele double les chiffres de Toshiba parce que ce sont des ETATS.
     * Ceux-ci n'en sont pas : ils ont ete MESURES en cycles d'emulateur, contre du
     * materiel. 14 vient de Cool Boarders ramene a ses 30 fps reels ; 18 vient du
     * copieur en boucle ouverte de l'ecran-titre de Bomberman, qui doit depenser
     * exactement 8 lignes par bloc -- a 17 comme a 19 l'image se dechire. Une fenetre
     * d'UN cycle.
     *
     * ⚡ Les avoir doubles facturait chaque copie de bloc au double, et c'est
     * precisement ce qui fait dechirer un HUD en split raster : le HUD de Cool Boarders
     * et le fond de KOF R-2 glitchaient en jeu, observes manette en main. Aucun banc ne
     * l'a vu -- le corpus ne regarde que des ecrans d'intro. */
    m->ldir_cost          = 14;
    /* ⚠️ 18, ET LE SILICIUM DIT 14 -- CONFLIT OUVERT, PAS UN OUBLI (ROM v20 page 0).
     * Un `ldirw` RAM->RAM coute **14,16 cy/iteration** sur console, exactement comme un
     * `ldirb` (14,12) et exactement l'annexe B (3), `7n+1` etats. Mais le copieur
     * HiColor de Bomberman -- qui ecrit en **VRAM** -- exige 18 : a 14 il tourne 21 %
     * trop vite (6476 cycles pour deux blocs contre 8240 mesures sur console).
     * ⛔ Et l'hypothese evidente est REFUTEE : « 18 = 14 + l'etranglement VRAM » ne tient
     * pas. Arme (`block_pays_vram`), le throttle ne change **rien** au copieur -- il
     * passe par `access_wait`, donc il est integralement absorbe par le recouvrement,
     * le cout de base d'un bloc etant enorme. Et meme non absorbe il vaut 2,9, pas 4.
     * ⇒ La difference RAM/VRAM sur un transfert bloc est REELLE et n'est pas encore
     * expliquee. On garde 18 : il tient l'ancrage jouable, et 14 casse une image. */
    /* ⚡ 14, ET NON 18 (ROM v21, tir silicium). Le meme `ldirw` coute **14,04**
     * cy/iteration en RAM -> RAM -- l'annexe B (3) au centieme -- et **18,16** en
     * ROM -> RAM. Ce n'est donc pas l'instruction qui vaut 18 : c'est 14 plus le prix de
     * lire sa SOURCE sur le bus 8 bits de la cartouche. Voir
     * `block_cart_src_per_byte`. La destination, elle, ne coute rien (RAM -> VRAM :
     * 14,12). Notre 18 uniforme etait 29 % trop cher sur toute copie RAM -> RAM ou
     * RAM -> VRAM -- la plupart de celles que font les jeux. */
    m->ldirw_cost         = 14;
    m->block_cart_src_per_byte = 2;   /* +4 par iteration MOT : v21, +4,12 mesures */
    /* ⚡ ET LA FILE D'INSTRUCTIONS NE PREND AUCUNE AVANCE PENDANT UNE COPIE DE BLOC.
     *
     * Une copie repetee tient le bus pendant toute sa duree -- lecture et ecriture a
     * chaque iteration -- donc l'unite de bus n'a pas un seul creneau pour precharger
     * derriere elle. Sans ceci elle sortait du bloc avec une file PLEINE, et les
     * instructions suivantes voyaient leur fetch offert (`biu_slack`, 16 cycles) autant
     * de fois qu'il y avait de blocs.
     *
     * ⚖️ MESURE, sur l'instrument le plus fin que ce projet possede : le copieur raster
     * en BOUCLE OUVERTE de BOMBERMAN (Thor, 2004). Il se synchronise UNE fois sur la
     * ligne 0 puis enchaine 19 blocs de 224 mots sans jamais repoller, donc chaque bloc
     * doit couter exactement une tranche de 8 lignes -- 8 x 515 = 4120 cycles :
     *
     *   sans ceci   4086 cycles/bloc (0,9917x) -- 34 cycles TROP RAPIDE par bloc
     *   avec        4134 cycles/bloc (1,0034x) -- le plus proche jamais mesure
     *   (l'ancien modele, `legacy`, donnait 4158 : 1,0092x)
     *
     * ⛔ ET CES 34 CYCLES SE VOIENT, parce qu'ils ne s'annulent pas : ils s'ACCUMULENT.
     * L'ecriture part 34 cycles plus tot a chaque bloc et la marge de depart n'est que
     * de 73 cycles, donc des le 4e bloc l'ecriture repasse dans la DERNIERE ligne de la
     * bande precedente -- celle qui affiche encore la banque qu'on ecrase. Resultat :
     * la ligne 8k+7 corrompue sur 15 bandes, et rien ailleurs. Une derive lente ne
     * donne pas un defaut flou, elle donne un defaut NET a partir d'un seuil.
     *
     * ⚠️ Le sens de l'erreur compte : etre un peu LENT est sans effet (l'ecriture reste
     * derriere le faisceau, le jeu a de la marge en fin de bloc), etre un peu RAPIDE est
     * fatal. C'est pour ca que `legacy` sortait juste malgre +0,92 %.
     *
     * ⚖️ ET L'ORACLE QUI EXISTAIT DEJA A ETE INTERROGE AVANT DE SHIPPER : les ONZE ROM
     * de `hw_calibration/` rendent un framebuffer IDENTIQUE AU BIT avec et sans -- aucun
     * chiffre silicium ne bouge. Corpus 83 jeux : 80 identiques au bit, 3 deplaces d'UNE
     * trame d'animation (regardes a l'ecran, rien de degrade), 0 erreur. */
    m->block_drains_queue = true;
    m->tx_irq_on_buffer_free = true;
    m->rx_single_charge   = true;

    /* ⛔ AND EVERY EXPERIMENTAL KNOB IS CLEARED, not left as it was found.
     *
     * "One call arms the model" is a lie if a knob somebody set earlier survives it: the
     * caller would get the model PLUS a refuted hypothesis, and no measurement would say
     * so. This function must fully DEFINE the machine's timing, not merely add to it --
     * which is the same discipline that the eight-setters-in-three-places bugs cost a
     * day to learn. Anything added below must be cleared here too. */
    m->flush_queue_on_branch = false;
    m->data_wait_cycles = 0;
    m->q_sixteenths = 0;
    m->fetch_bytes = 0;
    m->fetch_bc16 = 0;
    m->biu_slack_follows_region = false;
    m->relay_gates_on_rts    = false;
    m->rx_blocked_by_tx      = false;
    m->rx_double_buffered    = false;
    m->serial_byte_extra_pct = 0;
    m->irq_entry_cycles      = 0;     /* 0 = the documented kIrqDeliveryCycles */
    /* ⛔ NOT ARMED, and the reason is discipline rather than doubt.
     *
     * The behaviour is almost certainly right: TxD is a pin, nothing on the chip knows
     * what is plugged into it, and without this an unplugged console runs its BIOS-call
     * loop 15% FASTER (1250 -> 1436) purely because the transmitter stops existing.
     *
     * But its only NUMERIC justification was the probe's LOOP figure from the 2026-08-19
     * shoot -- a reference later disqualified because no ROM identity was recorded and
     * the ROM had changed since. So the benefit is unproven against any shoot that can
     * defend itself, while the cost is measured: with it armed, a serialised state no
     * longer replays identically (libretro smoke, "non-deterministic state after
     * replay"), because an unplugged console can hold a byte mid-transmit and not every
     * piece of that survives the round trip.
     *
     * ⇒ Unproven benefit does not buy a proven regression. Re-arm it when a shoot with
     * a recorded md5 measures test 6, and finish tracing the state it needs. */
    m->uart_runs_unplugged = false;
}

NGPC_API void ngpc_set_byte_extra(ngpc_t* h, uint32_t pct) {
    if (!h) return;
    reinterpret_cast<Machine*>(h)->serial_byte_extra_pct = pct;
}

NGPC_API void ngpc_set_uart_unplugged(ngpc_t* h, int on) {
    if (!h) return;
    reinterpret_cast<Machine*>(h)->uart_runs_unplugged = on != 0;
}

NGPC_API void ngpc_set_micro_dma_states(ngpc_t* h, uint32_t eighths) {
    if (h) reinterpret_cast<Machine*>(h)->micro_dma_states = eighths;
}

NGPC_API void ngpc_set_fetch_wait_q4(ngpc_t* h, uint32_t quarters) {
    if (!h) return;
    auto* m = reinterpret_cast<Machine*>(h);
    m->fetch_wait_q4 = quarters;
    m->access_wait_q4 = 0;
    m->fetch_wait_carry = 0;
}

NGPC_API void ngpc_set_bios_data_wait(ngpc_t* h, uint32_t cycles) {
    if (!h) return;
    reinterpret_cast<Machine*>(h)->bios_data_wait = cycles;
}

NGPC_API void ngpc_set_slack_by_region(ngpc_t* h, int on) {
    if (!h) return;
    reinterpret_cast<Machine*>(h)->biu_slack_follows_region = on != 0;
}

NGPC_API void ngpc_set_block_drains_queue(ngpc_t* h, int on) {
    if (!h) return;
    reinterpret_cast<Machine*>(h)->block_drains_queue = on != 0;
}

NGPC_API void ngpc_set_branch_flush(ngpc_t* h, int on) {
    if (!h) return;
    reinterpret_cast<Machine*>(h)->flush_queue_on_branch = on != 0;
}

NGPC_API void ngpc_set_branch_flush_keep(ngpc_t* h, uint32_t cycles) {
    if (!h) return;
    reinterpret_cast<Machine*>(h)->branch_flush_keep = cycles;
}

NGPC_API void ngpc_set_branch_taken_extra(ngpc_t* h, uint32_t cycles) {
    if (!h) return;
    reinterpret_cast<Machine*>(h)->branch_taken_extra = cycles;
}

NGPC_API void ngpc_set_fetch_wait_byte_q16(ngpc_t* h, uint32_t sixteenths) {
    if (!h) return;
    reinterpret_cast<Machine*>(h)->fetch_wait_byte_q16 = sixteenths;
}

NGPC_API void ngpc_set_data_access_cycles(ngpc_t* h, uint32_t cycles) {
    if (!h) return;
    reinterpret_cast<Machine*>(h)->data_access_cycles = cycles;
}

NGPC_API void ngpc_dbg_biu(ngpc_t* h, int32_t* debt_in, uint32_t* stall, uint32_t* aw) {
    if (!h) return;
    auto* m = reinterpret_cast<Machine*>(h);
    if (debt_in) *debt_in = m->dbg_debt_in;
    if (stall)   *stall   = m->dbg_stall;
    if (aw)      *aw      = m->dbg_aw;
}

NGPC_API void ngpc_dbg_queue(ngpc_t* h, int32_t* q_in, uint32_t* bytes,
                             uint32_t* stall, uint32_t* aw) {
    if (!h) return;
    auto* m = reinterpret_cast<Machine*>(h);
    if (q_in)  *q_in  = m->dbg_q_in;
    if (bytes) *bytes = m->dbg_q_bytes;
    if (stall) *stall = m->dbg_stall;
    if (aw)    *aw    = m->dbg_aw;
}

NGPC_API uint32_t ngpc_dbg_bios_charges(ngpc_t* h) {
    if (!h) return 0;
    auto* m = reinterpret_cast<Machine*>(h);
    const uint32_t v = m->dbg_bios_charges;
    m->dbg_bios_charges = 0;
    return v;
}

NGPC_API void ngpc_set_irq_flush_keep(ngpc_t* h, uint32_t cycles) {
    if (!h) return;
    reinterpret_cast<Machine*>(h)->irq_flush_keep = cycles;
}

NGPC_API void ngpc_set_biu_slack(ngpc_t* h, int32_t cycles) {
    if (!h) return;
    reinterpret_cast<Machine*>(h)->biu_slack = cycles;
}

NGPC_API void ngpc_set_flush_on_region_change(ngpc_t* h, int on) {
    if (!h) return;
    reinterpret_cast<Machine*>(h)->flush_on_region_change = (on != 0);
}

NGPC_API void ngpc_set_irq_transparent_queue(ngpc_t* h, int on) {
    if (!h) return;
    auto* m = reinterpret_cast<Machine*>(h);
    m->irq_transparent_queue = (on != 0);
    m->irq_save_depth = 0;
}

NGPC_API void ngpc_set_block_pays_vram(ngpc_t* h, int on) {
    if (!h) return;
    reinterpret_cast<Machine*>(h)->block_pays_vram = (on != 0);
}

NGPC_API void ngpc_set_data_wait_cart_only(ngpc_t* h, int on) {
    if (!h) return;
    reinterpret_cast<Machine*>(h)->data_wait_cart_only = (on != 0);
}

NGPC_API void ngpc_set_irq_queue_keep_q16(ngpc_t* h, int32_t q16) {
    if (!h) return;
    reinterpret_cast<Machine*>(h)->irq_queue_keep_q16 = q16;
}

NGPC_API void ngpc_set_queue_bytes(ngpc_t* h, uint32_t bytes) {
    if (!h) return;
    auto* m = reinterpret_cast<Machine*>(h);
    m->queue_bytes = bytes;
    m->q_sixteenths = 0;
    m->fetch_bytes = 0;
    m->fetch_bc16 = 0;
}

NGPC_API void ngpc_set_muldiv_word(ngpc_t* h, uint32_t mul_states, uint32_t div_cycles) {
    if (!h) return;
    auto* m = reinterpret_cast<Machine*>(h);
    m->mul_word_states = mul_states;
    m->div_word_cycles = div_cycles;
}

NGPC_API void ngpc_set_muldiv_byte(ngpc_t* h, uint32_t mul_states, uint32_t div_cycles) {
    if (!h) return;
    auto* m = reinterpret_cast<Machine*>(h);
    m->mul_byte_states = mul_states;
    m->div_byte_cycles = div_cycles;
}

NGPC_API void ngpc_set_rx_double(ngpc_t* h, int on) {
    if (!h) return;
    reinterpret_cast<Machine*>(h)->rx_double_buffered = on != 0;
}

NGPC_API void ngpc_set_tx_irq_early(ngpc_t* h, int on) {
    if (!h) return;
    reinterpret_cast<Machine*>(h)->tx_irq_on_buffer_free = on != 0;
}

NGPC_API void ngpc_set_fetch_pipelined(ngpc_t* h, int on, int slack) {
    if (!h) return;
    auto* m = reinterpret_cast<Machine*>(h);
    m->fetch_pipelined = on != 0;
    m->biu_debt = 0;
    m->biu_slack = slack;
}

NGPC_API void ngpc_set_half_duplex(ngpc_t* h, int on) {
    if (!h) return;
    reinterpret_cast<Machine*>(h)->rx_blocked_by_tx = on != 0;
}

NGPC_API void ngpc_set_relay_gate(ngpc_t* h, int on) {
    if (!h) return;
    reinterpret_cast<Machine*>(h)->relay_gates_on_rts = on != 0;
}

NGPC_API void ngpc_set_rx_single(ngpc_t* h, int on) {
    if (!h) return;
    reinterpret_cast<Machine*>(h)->rx_single_charge = on != 0;
}

NGPC_API void ngpc_set_fetch_word(ngpc_t* h, int on) {
    if (!h) return;
    reinterpret_cast<Machine*>(h)->fetch_wait_per_word = on != 0;
}

NGPC_API void ngpc_set_base_scale(ngpc_t* h, uint32_t k) {
    if (!h) return;
    reinterpret_cast<Machine*>(h)->base_scale = k ? k : 1;
}

NGPC_API void ngpc_set_irq_entry(ngpc_t* h, uint32_t cycles) {
    if (!h) return;
    reinterpret_cast<Machine*>(h)->irq_entry_cycles = cycles;
}

NGPC_API void ngpc_set_bios_wait(ngpc_t* h, uint32_t cycles_per_byte) {
    if (!h) return;
    reinterpret_cast<Machine*>(h)->bios_wait = cycles_per_byte;
}

NGPC_API void ngpc_set_cart_wait(ngpc_t* h, uint32_t cycles_per_byte) {
    if (!h) return;
    reinterpret_cast<Machine*>(h)->cart_wait = cycles_per_byte;
}

NGPC_API void ngpc_set_cart_data_wait(ngpc_t* h, uint32_t cycles_per_byte) {
    if (!h) return;
    reinterpret_cast<Machine*>(h)->cart_data_wait = cycles_per_byte;
}

NGPC_API void ngpc_set_k1ge_console(ngpc_t* h, int on) {
    if (!h) return;
    reinterpret_cast<Machine*>(h)->k1ge_console = (on != 0);
}

NGPC_API void ngpc_set_vram_wait(ngpc_t* h, uint32_t cycles_per_byte) {
    if (!h) return;
    reinterpret_cast<Machine*>(h)->vram_wait = cycles_per_byte;
}

NGPC_API void ngpc_set_ldir_cost(ngpc_t* h, uint32_t cycles_per_byte) {
    if (!h) return;
    reinterpret_cast<Machine*>(h)->ldir_cost = uint16_t(cycles_per_byte ? cycles_per_byte : 7);
}

/* Cost per ITERATION of the WORD block copies (LDIRW/LDDRW). 0 = follow ldir_cost, which
 * is what every existing caller gets, so nothing moves until someone asks for it.
 * Measured at 18 on Bomberman's open-loop HiColor copier -- see Machine::ldirw_cost. */
NGPC_API void ngpc_set_ldirw_cost(ngpc_t* h, uint32_t cycles_per_iteration) {
    if (!h) return;
    reinterpret_cast<Machine*>(h)->ldirw_cost = uint16_t(cycles_per_iteration);
}

/* Present the cart as a flash chip of `bytes` capacity (a standard 4/8/16 Mbit part),
 * rebuilding the erasable-block map. A real flashcart's chip is bigger than an under-filled
 * homebrew ROM, and a game that saves in the chip's top block (StarGunner -> block 33 at
 * 0x1FA000 on a 16 Mbit part) needs that block to EXIST. 0 = leave it at the ROM size. */
NGPC_API void ngpc_set_flash_size(ngpc_t* h, uint32_t chip, uint32_t bytes) {
    if (!h || chip > 1 || bytes == 0) return;
    reinterpret_cast<Machine*>(h)->flash_build_blocks(int(chip), bytes);
}

/* The console's language setting, handed to the cartridge at 0x6F87 on the hand-off:
 * 0 = Japanese, 1 = English (SDK SysWork.txt). 24 games of the corpus read it, and a
 * bilingual cartridge has nothing else to go on. On real hardware the setup wizard
 * writes it and the coin cell remembers; we skip the wizard, so this is where the
 * choice lives. Set BEFORE reset. */
NGPC_API void ngpc_set_language(ngpc_t* h, uint32_t code) {
    if (!h) return;
    reinterpret_cast<Machine*>(h)->language_code = uint8_t(code ? 1 : 0);
}

/* What the chip currently presents as -- WHICH IS NOT ALWAYS WHAT WAS SET. The cartridge
 * corrects us mid-session (flash_adopt_capacity_from_block / _from_save), and whoever
 * persists the save has to know: writing the .ngc back at the size we GUESSED pads an
 * under-filled cart out to a size it never had, and on the next load that padding reads
 * as image -- which is precisely what stops the cart from correcting us a second time.
 * Measured before this existed: a 512 KiB cart saved fine, its file grew to 2 MiB, and
 * every save from the second session on was ANDed into an unerased slot. 0 = empty slot. */
NGPC_API uint32_t ngpc_flash_capacity(ngpc_t* h, uint32_t chip) {
    if (!h || chip > 1) return 0;
    return reinterpret_cast<Machine*>(h)->flash_presented_capacity(int(chip));
}

NGPC_API void ngpc_raise_irq(ngpc_t* h, uint32_t vector_index) {
    if (!h || vector_index >= 32) return;
    reinterpret_cast<Machine*>(h)->irq_pending |= (uint64_t(1) << vector_index);
}

/* --- link cable (serial channel 0) ----------------------------------------
 * The cable is a byte pipe. A host wires two machines together by draining each
 * one's transmit FIFO (ngpc_serial_read_tx) and pushing it into the other's
 * receive FIFO (ngpc_serial_write_rx) -- in-process for two players on one PC,
 * or across a socket for online. Enable is off by default: the serial registers
 * stay inert and the cable reads as unplugged, unchanged from before. */
NGPC_API void ngpc_serial_set_enabled(ngpc_t* h, int on) {
    if (!h) return;
    Machine& m = *reinterpret_cast<Machine*>(h);
    m.serial_link_enabled = (on != 0);
    /* Counters are per-cable-session: plugging in starts a fresh reading, so the
     * debugger's totals answer "since this link came up", not "since power-on". */
    m.serial_tx_count = m.serial_wire_count = 0;
    m.serial_rx_queued_count = m.serial_rx_read_count = 0;
    m.serial_irq_tx_count = m.serial_irq_rx_count = 0;
    m.serial_cts_hold_ticks = m.serial_rts_hold_ticks = 0;
    m.serial_relay_count = 0;
    if (!on) {
        m.serial_tx.clear();
        m.serial_rx.clear();
        m.serial_tx_busy = false;
        m.serial_tx_shifting = false;
        m.serial_rx_pending = false;
        m.serial_tx_cycles = 0;
        m.serial_rx_cycles = 0;
    }
}

/* Ask ngpc_run to hand back the moment the cable moves. See the header for why
 * this replaces the host's instruction-quota poll rather than tuning it. */
NGPC_API void ngpc_set_serial_break(ngpc_t* h, int on) {
    if (!h) return;
    Machine& m = *reinterpret_cast<Machine*>(h);
    m.serial_break_on_event = (on != 0);
    /* Arming is not a report about the past: drop anything already noticed so
     * the first run afterwards answers only for its own traffic. */
    m.serial_event = false;
}

/* Drain up to `max` bytes this machine has transmitted; returns the count. */
NGPC_API uint32_t ngpc_serial_read_tx(ngpc_t* h, uint8_t* out, uint32_t max) {
    if (!h || !out) return 0;
    Machine& m = *reinterpret_cast<Machine*>(h);
    uint32_t n = 0;
    while (n < max && !m.serial_tx.empty()) {
        out[n++] = m.serial_tx.front();
        m.serial_tx.pop_front();
    }
    return n;
}

/* Queue `n` bytes for this machine to receive from the peer. */
NGPC_API void ngpc_serial_write_rx(ngpc_t* h, const uint8_t* data, uint32_t n) {
    if (!h || !data) return;
    Machine& m = *reinterpret_cast<Machine*>(h);
    for (uint32_t i = 0; i < n; ++i) m.serial_rx.push_back(data[i]);
    m.serial_rx_queued_count += n;
}

/* 1 = this machine's RTS is low (ready to receive), 0 = holding the peer off.
 * A host may consult this to honour flow control before pushing bytes. */
NGPC_API int ngpc_serial_rts(ngpc_t* h) {
    if (!h) return 0;
    Machine& m = *reinterpret_cast<Machine*>(h);
    return (m.mem[0x0000B2] & 0x01) == 0 ? 1 : 0;
}

/* Drive this machine's CTS0 handshake input (wired to the PEER's RTS on the cable).
 * high != 0 -> CTS0 HIGH -> if SC0MOD<CTSE> is set, this machine's transmitter is
 * halted (byte held, no INTTX0) until the peer drops RTS. A host bridging two
 * machines calls this each pump with the peer's RTS state. See Machine::serial_tick. */
NGPC_API void ngpc_serial_set_cts(ngpc_t* h, int high) {
    if (!h) return;
    Machine* m = reinterpret_cast<Machine*>(h);
    m->serial_cts_high = (high != 0);
    m->serial_cts_seen = true;
}

/* Read-only snapshot of the whole channel for the debugger's Link tab. Every
 * field is either a counter incremented where the event happens or a register
 * read straight out of the I/O page -- no state is touched, so watching costs
 * nothing and changes nothing. See ngpc_serial_state_t for what each means. */
NGPC_API void ngpc_serial_state(ngpc_t* h, ngpc_serial_state_t* out) {
    if (!h || !out) return;
    const Machine& m = *reinterpret_cast<const Machine*>(h);
    out->enabled         = m.serial_link_enabled ? 1u : 0u;
    out->tx_depth        = uint32_t(m.serial_tx.size());
    out->rx_depth        = uint32_t(m.serial_rx.size());
    out->tx_busy         = m.serial_tx_busy ? 1u : 0u;
    out->rx_pending      = m.serial_rx_pending ? 1u : 0u;
    out->cts_high        = m.serial_cts_high ? 1u : 0u;
    out->rts_low         = (m.mem[0x0000B2] & 0x01) == 0 ? 1u : 0u;
    out->ctse            = (m.mem[0x000052] & 0x40) != 0 ? 1u : 0u;
    out->tx_count        = m.serial_tx_count;
    out->wire_count      = m.serial_wire_count;
    out->rx_queued_count = m.serial_rx_queued_count;
    out->rx_read_count   = m.serial_rx_read_count;
    out->irq_tx_count    = m.serial_irq_tx_count;
    out->irq_rx_count    = m.serial_irq_rx_count;
    out->cts_hold_ticks  = m.serial_cts_hold_ticks;
    out->rts_hold_ticks  = m.serial_rts_hold_ticks;
    out->sc0buf          = m.mem[0x000050];
    out->sc0cr           = m.mem[0x000051];
    out->sc0mod          = m.mem[0x000052];
    out->br0cr           = m.mem[0x000053];
    /* 0xB1 as the GAME sees it, not as it sits in the I/O page. read8 forces the
     * sub-battery bit and drives bit2 -- the cable-DETECT line Card Fighters'
     * Clash gates its handshake on -- from the cable state, so dumping the raw
     * byte here would report "no cable" on a working one.
     *
     * ⛔ AND IT MUST BE THE SAME RULE READ8 USES. This stayed on the OLD model
     * (bit2 from serial_link_enabled alone) after read8 moved to the peer's RTS,
     * so the debugger's Link tab reported `port_b1 = 0x02`, "cable detected", on
     * an online session where the CPU was reading 0x06, "no peer" -- the exact
     * fault that was being looked for, hidden by the instrument sent to look for
     * it. Measured with tools/link_online_probe.py. Mirrors machine.hpp read8;
     * change the two together. */
    out->port_b1         = uint32_t(uint8_t(
        (m.serial_link_enabled && m.serial_cts_seen && !m.serial_cts_high)
            ? ((m.mem[0x0000B1] | 0x02) & ~0x04)
            :  (m.mem[0x0000B1] | 0x02 | 0x04)));
    out->port_b2         = m.mem[0x0000B2];
}

NGPC_API void ngpc_set_apu_channel_mask(ngpc_t* h, uint32_t mask) {
    if (!h) return;
    // bit0..2 = squares, bit3 = noise, bit4 = DAC. Debug mute/solo only.
    reinterpret_cast<Machine*>(h)->apu.channel_mask = uint8_t(mask & 0x1F);
}

NGPC_API void ngpc_set_layer_mask(ngpc_t* h, uint32_t mask) {
    if (!h) return;
    // bit0 = SCR1, bit1 = SCR2, bit2..4 = sprites by PR.C. Debug show/hide only:
    // it drops a layer from the composite and touches nothing else. See machine.hpp.
    reinterpret_cast<Machine*>(h)->layer_mask = uint8_t(mask & Machine::kLayerAll);
}

NGPC_API uint32_t ngpc_get_layer_mask(ngpc_t* h) {
    if (!h) return Machine::kLayerAll;
    return reinterpret_cast<Machine*>(h)->layer_mask;
}

NGPC_API void ngpc_get_apu_state(ngpc_t* h, ngpc_apu_state_t* out) {
    if (!h || !out) return;
    const Apu& a = reinterpret_cast<Machine*>(h)->apu;
    for (int i = 0; i < 3; ++i) {
        out->square_vol_left[i]  = a.square[i].vol_left;
        out->square_vol_right[i] = a.square[i].vol_right;
        out->square_period[i]    = a.square[i].period;
    }
    out->noise_vol_left      = a.noise.vol_left;
    out->noise_vol_right     = a.noise.vol_right;
    out->noise_shifter       = a.noise.shifter;
    out->noise_tap           = a.noise.tap;
    out->noise_period_select = a.noise.period_select;
    out->noise_period_extra  = a.noise.period_extra;
    out->latch_left          = a.latch_left;
    out->latch_right         = a.latch_right;
}

NGPC_API uint32_t ngpc_get_audio(ngpc_t* h, int16_t* out, uint32_t frames) {
    if (!h || !out || frames == 0) return 0;
    return reinterpret_cast<Machine*>(h)->apu.drain(out, frames);
}

NGPC_API uint64_t ngpc_audio_dropped(ngpc_t* h) {
    if (!h) return 0;
    return reinterpret_cast<Machine*>(h)->apu.dropped;
}

NGPC_API uint64_t ngpc_apu_write_count(ngpc_t* h) {
    if (!h) return 0;
    return reinterpret_cast<Machine*>(h)->apu_writes;
}

NGPC_API uint32_t ngpc_get_apu_writes(ngpc_t* h, ngpc_apu_write_t* out, uint32_t n) {
    if (!h || !out || n == 0) return 0;
    Machine* m = reinterpret_cast<Machine*>(h);
    const uint64_t total = m->apu_writes;
    const uint64_t held  = total < Machine::kApuLogSize ? total : Machine::kApuLogSize;
    const uint32_t want  = uint32_t(held < n ? held : n);
    const uint64_t first = total - want;                  /* oldest we still keep */
    for (uint32_t i = 0; i < want; ++i)
        out[i] = m->apu_log[(first + i) % Machine::kApuLogSize];
    return want;
}

NGPC_API void ngpc_get_z80(ngpc_t* h, ngpc_z80_t* out) {
    if (!h || !out) return;
    const Machine* m = reinterpret_cast<Machine*>(h);
    const Z80& z = m->z80;
    out->running     = z.running ? 1 : 0;
    out->halted      = z.halted ? 1 : 0;
    out->trapped     = z.trapped ? 1 : 0;
    out->trap_prefix = z.trap_prefix;
    out->trap_pc     = z.trap_pc;
    out->trap_opcode = z.trap_opcode;
    out->_pad        = 0;
    out->pc          = z.pc;
    out->sp          = z.sp;
    out->executed    = z.executed;
    out->port_writes = m->z80_port_writes;
}

NGPC_API void ngpc_set_cpu(ngpc_t* h, const ngpc_cpu_t* in) {
    if (!h || !in) return;
    reinterpret_cast<Machine*>(h)->cpu = *in;
}

/* --- the rest of the machine, for savestates -------------------------------
 * See the contract in ngpc_core.h: this is the state a snapshot needs that does
 * NOT live in the memory image -- the sound CPU's registers, the T6W28's, and the
 * timer up-counters that pace them. Leaving it out is what made the sound die on
 * every state load. */
NGPC_API void ngpc_get_aux_state(ngpc_t* h, ngpc_aux_state_t* out) {
    if (!h || !out) return;
    const Machine* m = reinterpret_cast<Machine*>(h);
    const Z80& z = m->z80;
    const Apu& a = m->apu;

    std::memset(out, 0, sizeof(*out));
    out->version = NGPC_AUX_STATE_VERSION;
    out->size    = uint32_t(sizeof(ngpc_aux_state_t));

    out->z80_a  = z.a;  out->z80_f  = z.f;  out->z80_b  = z.b;  out->z80_c  = z.c;
    out->z80_d  = z.d;  out->z80_e  = z.e;  out->z80_h  = z.h;  out->z80_l  = z.l;
    out->z80_a2 = z.a_; out->z80_f2 = z.f_; out->z80_b2 = z.b_; out->z80_c2 = z.c_;
    out->z80_d2 = z.d_; out->z80_e2 = z.e_; out->z80_h2 = z.h_; out->z80_l2 = z.l_;
    out->z80_ix = z.ix; out->z80_iy = z.iy; out->z80_sp = z.sp; out->z80_pc = z.pc;
    out->z80_i  = z.i;  out->z80_r  = z.r;  out->z80_im = z.im;
    out->z80_iff1        = z.iff1 ? 1 : 0;
    out->z80_iff2        = z.iff2 ? 1 : 0;
    out->z80_halted      = z.halted ? 1 : 0;
    out->z80_running     = z.running ? 1 : 0;
    out->z80_nmi_pending = z.nmi_pending ? 1 : 0;
    out->z80_int_pending = z.int_pending ? 1 : 0;
    out->z80_trapped     = z.trapped ? 1 : 0;
    out->z80_trap_prefix = z.trap_prefix;
    out->z80_trap_opcode = z.trap_opcode;
    out->z80_trap_pc     = z.trap_pc;
    out->z80_int_ack     = m->z80_int_ack;
    out->z80_cycle_credit = z.cycle_credit;
    out->z80_executed     = z.executed;

    for (int i = 0; i < 3; ++i) {
        out->square_vol_left[i]  = a.square[i].vol_left;
        out->square_vol_right[i] = a.square[i].vol_right;
        out->square_period[i]    = a.square[i].period;
        out->square_phase[i]     = a.square[i].phase;
        out->square_counter[i]   = a.square[i].counter;
    }
    out->noise_vol_left      = a.noise.vol_left;
    out->noise_vol_right     = a.noise.vol_right;
    out->noise_shifter       = a.noise.shifter;
    out->noise_tap           = a.noise.tap;
    out->noise_period_select = a.noise.period_select;
    out->noise_period_extra  = a.noise.period_extra;
    out->noise_counter       = a.noise.counter;
    out->latch_left          = a.latch_left;
    out->latch_right         = a.latch_right;
    out->dac_left            = a.dac_left;
    out->dac_right           = a.dac_right;
    out->apu_main_residue    = a.main_residue;
    out->apu_step_fp         = a.step_fp;
    out->apu_chip_residue    = a.chip_residue;

    for (int i = 0; i < 4; ++i) {
        out->timer_count[i] = m->timer_count[i];
        out->timer_clock[i] = m->timer_clock[i];
    }
    out->to3_half_periods   = m->to3_half_periods;
    out->ti0_pending_pulses = m->ti0_pending_pulses;
    out->irq_pending        = m->irq_pending;
    out->scanline           = m->scanline;
    out->frame_count        = m->frame_count;
    out->cycle_residue      = m->cycle_residue;
    out->biu_debt           = uint32_t(m->biu_debt);
}

NGPC_API int ngpc_set_aux_state(ngpc_t* h, const ngpc_aux_state_t* in) {
    /* ⛔ A RESTORED STATE RESUMES WITH AN EMPTY INSTRUCTION QUEUE, and the two serial
     * buffer stages empty with it.
     *
     * These are timing state that no public struct carries, so leaving them alone made
     * a replay diverge from the run it replayed -- caught by the libretro smoke test as
     * "non-deterministic state after replay". Clearing them is not a fudge: a snapshot
     * is exactly the kind of boundary at which a pipeline may be considered flushed,
     * and the error it can introduce is bounded by the queue -- four bytes, i.e. two
     * word fetches. Reproducibility is worth more than those.
     *
     * ⚠️ ANYTHING ELSE ADDED TO Machine THAT INFLUENCES TIMING BELONGS HERE TOO, or in
     * a serialised struct. There is no third option that stays deterministic. */
    /* ⛔ ON N'EFFACE PLUS `biu_debt` ICI -- il est desormais DANS le blob (voir
     * ngpc_core.h). L'effacer faisait diverger le rejeu de ce qu'il rejoue. Les deux
     * accumulateurs de quart de cycle, eux, sont sans etat depuis le motif tire de
     * l'adresse ; on les remet a zero par prudence, ils valent deja 0. */
    if (h) {
        auto* mm = reinterpret_cast<Machine*>(h);
        mm->access_wait_q4 = 0;
        mm->fetch_wait_carry = 0;
    }
    if (!h || !in) return -1;
    /* A blob from another build is REFUSED, not half-applied: a savestate written by
     * an older core carries a different layout, and reading one field of it as
     * another is how a "restored" machine ends up quietly insane. */
    if (in->version != NGPC_AUX_STATE_VERSION || in->size != sizeof(ngpc_aux_state_t))
        return -1;

    Machine* m = reinterpret_cast<Machine*>(h);
    Z80& z = m->z80;
    Apu& a = m->apu;

    z.a  = in->z80_a;  z.f  = in->z80_f;  z.b  = in->z80_b;  z.c  = in->z80_c;
    z.d  = in->z80_d;  z.e  = in->z80_e;  z.h  = in->z80_h;  z.l  = in->z80_l;
    z.a_ = in->z80_a2; z.f_ = in->z80_f2; z.b_ = in->z80_b2; z.c_ = in->z80_c2;
    z.d_ = in->z80_d2; z.e_ = in->z80_e2; z.h_ = in->z80_h2; z.l_ = in->z80_l2;
    z.ix = in->z80_ix; z.iy = in->z80_iy; z.sp = in->z80_sp; z.pc = in->z80_pc;
    z.i  = in->z80_i;  z.r  = in->z80_r;  z.im = in->z80_im;
    z.iff1        = in->z80_iff1 != 0;
    z.iff2        = in->z80_iff2 != 0;
    z.halted      = in->z80_halted != 0;
    z.running     = in->z80_running != 0;
    /* These two are EDGE state, and restoring the memory image just forged one of
     * them: 0x00BA is a door ("fire one NMI"), not a byte of storage, so writing the
     * image back rings the sound CPU's doorbell. Putting the snapshot's own value
     * here is what cancels that phantom. */
    z.nmi_pending = in->z80_nmi_pending != 0;
    z.int_pending = in->z80_int_pending != 0;
    z.trapped     = in->z80_trapped != 0;
    z.trap_prefix = in->z80_trap_prefix;
    z.trap_opcode = in->z80_trap_opcode;
    z.trap_pc     = in->z80_trap_pc;
    z.cycle_credit = in->z80_cycle_credit;
    z.executed     = in->z80_executed;
    m->z80_int_ack = in->z80_int_ack;

    for (int i = 0; i < 3; ++i) {
        a.square[i].vol_left  = in->square_vol_left[i];
        a.square[i].vol_right = in->square_vol_right[i];
        a.square[i].period    = in->square_period[i];
        a.square[i].phase     = in->square_phase[i];
        a.square[i].counter   = in->square_counter[i];
    }
    a.noise.vol_left      = in->noise_vol_left;
    a.noise.vol_right     = in->noise_vol_right;
    a.noise.shifter       = in->noise_shifter;
    a.noise.tap           = in->noise_tap;
    a.noise.period_select = in->noise_period_select;
    a.noise.period_extra  = in->noise_period_extra;
    a.noise.counter       = in->noise_counter;
    a.latch_left          = in->latch_left;
    a.latch_right         = in->latch_right;
    a.dac_left            = in->dac_left;
    a.dac_right           = in->dac_right;
    a.main_residue        = in->apu_main_residue;
    a.step_fp             = in->apu_step_fp;
    a.chip_residue        = in->apu_chip_residue;
    /* `channel_mask` and the output ring are deliberately untouched: the first is a
     * UI setting, the second is audio the host has not played yet. */

    for (int i = 0; i < 4; ++i) {
        m->timer_count[i] = in->timer_count[i];
        m->timer_clock[i] = in->timer_clock[i];
    }
    m->to3_half_periods   = in->to3_half_periods;
    m->ti0_pending_pulses = in->ti0_pending_pulses;
    m->irq_pending        = in->irq_pending;
    m->scanline           = in->scanline;
    m->frame_count        = in->frame_count;
    m->cycle_residue      = in->cycle_residue;
    m->biu_debt           = int32_t(in->biu_debt);
    return 0;
}

/* --- the link cable as saveable state (see ngpc_core.h for WHY) ------------- */

NGPC_API void ngpc_get_link_state(ngpc_t* h, ngpc_link_state_t* out) {
    if (!h || !out) return;
    const Machine* m = reinterpret_cast<Machine*>(h);

    std::memset(out, 0, sizeof(*out));
    out->version = NGPC_LINK_STATE_VERSION;
    out->size    = uint32_t(sizeof(ngpc_link_state_t));

    out->link_enabled = m->serial_link_enabled ? 1 : 0;
    out->tx_busy      = m->serial_tx_busy ? 1 : 0;
    out->tx_shifting  = m->serial_tx_shifting ? 1 : 0;
    out->tx_byte      = m->serial_tx_byte;
    out->cts_high     = m->serial_cts_high ? 1 : 0;
    out->cts_seen     = m->serial_cts_seen ? 1 : 0;   /* see the header: the detect line */
    out->rx_pending   = m->serial_rx_pending ? 1 : 0;
    out->rx_byte      = m->serial_rx_byte;
    out->tx_cycles    = m->serial_tx_cycles;
    out->rx_cycles    = m->serial_rx_cycles;
    out->tx_buf_full    = m->serial_tx_buf_full ? 1 : 0;
    out->tx_buf_byte    = m->serial_tx_buf_byte;
    out->rx_shift_full  = m->serial_rx_shift_full ? 1 : 0;
    out->rx_shift_byte  = m->serial_rx_shift_byte;
    out->rx_had_pending = m->serial_rx_had_pending ? 1 : 0;

    /* Clamp rather than truncate in silence: a snapshot that quietly dropped cable
     * bytes would be the very bug this block closes, wearing a green hat. */
    const size_t tx_n = m->serial_tx.size();
    const size_t rx_n = m->serial_rx.size();
    if (tx_n > NGPC_LINK_FIFO_MAX || rx_n > NGPC_LINK_FIFO_MAX) out->overflow = 1;
    out->tx_len = uint32_t(tx_n < NGPC_LINK_FIFO_MAX ? tx_n : NGPC_LINK_FIFO_MAX);
    out->rx_len = uint32_t(rx_n < NGPC_LINK_FIFO_MAX ? rx_n : NGPC_LINK_FIFO_MAX);
    for (uint32_t i = 0; i < out->tx_len; ++i) out->tx_fifo[i] = m->serial_tx[i];
    for (uint32_t i = 0; i < out->rx_len; ++i) out->rx_fifo[i] = m->serial_rx[i];

    out->tx_count        = m->serial_tx_count;
    out->wire_count      = m->serial_wire_count;
    out->rx_queued_count = m->serial_rx_queued_count;
    out->rx_read_count   = m->serial_rx_read_count;
    out->irq_tx_count    = m->serial_irq_tx_count;
    out->irq_rx_count    = m->serial_irq_rx_count;
    out->cts_hold_ticks  = m->serial_cts_hold_ticks;
    out->rts_hold_ticks  = m->serial_rts_hold_ticks;
}

NGPC_API int ngpc_set_link_state(ngpc_t* h, const ngpc_link_state_t* in) {
    if (!h || !in) return -1;
    /* Same contract as the aux block: a blob from another build is refused whole. */
    if (in->version != NGPC_LINK_STATE_VERSION || in->size != sizeof(ngpc_link_state_t))
        return -1;
    if (in->tx_len > NGPC_LINK_FIFO_MAX || in->rx_len > NGPC_LINK_FIFO_MAX)
        return -1;

    Machine* m = reinterpret_cast<Machine*>(h);

    m->serial_link_enabled = in->link_enabled != 0;
    m->serial_tx_busy      = in->tx_busy != 0;
    m->serial_tx_shifting  = in->tx_shifting != 0;
    m->serial_tx_byte      = in->tx_byte;
    m->serial_cts_high     = in->cts_high != 0;
    m->serial_cts_seen     = in->cts_seen != 0;
    m->serial_rx_pending   = in->rx_pending != 0;
    m->serial_tx_buf_full    = in->tx_buf_full != 0;
    m->serial_tx_buf_byte    = in->tx_buf_byte;
    m->serial_rx_shift_full  = in->rx_shift_full != 0;
    m->serial_rx_shift_byte  = in->rx_shift_byte;
    m->serial_rx_had_pending = in->rx_had_pending != 0;
    m->serial_rx_byte      = in->rx_byte;
    m->serial_tx_cycles    = in->tx_cycles;
    m->serial_rx_cycles    = in->rx_cycles;

    m->serial_tx.assign(in->tx_fifo, in->tx_fifo + in->tx_len);
    m->serial_rx.assign(in->rx_fifo, in->rx_fifo + in->rx_len);

    m->serial_tx_count        = in->tx_count;
    m->serial_wire_count      = in->wire_count;
    m->serial_rx_queued_count = in->rx_queued_count;
    m->serial_rx_read_count   = in->rx_read_count;
    m->serial_irq_tx_count    = in->irq_tx_count;
    m->serial_irq_rx_count    = in->irq_rx_count;
    m->serial_cts_hold_ticks  = in->cts_hold_ticks;
    m->serial_rts_hold_ticks  = in->rts_hold_ticks;
    return 0;
}

NGPC_API int ngpc_read_mem(ngpc_t* h, uint32_t addr, uint8_t* out, uint32_t n) {
    if (!h || !out) return -1;
    Machine* m = reinterpret_cast<Machine*>(h);
    for (uint32_t i = 0; i < n; ++i) out[i] = m->read8(addr + i);
    return 0;
}

NGPC_API int ngpc_write_mem(ngpc_t* h, uint32_t addr, const uint8_t* in, uint32_t n) {
    if (!h || !in) return -1;
    Machine* m = reinterpret_cast<Machine*>(h);
    /* Host-side writes (debugger poke, seeding) bypass the region guard on
     * purpose: the debugger is allowed to patch ROM in its own image. Guest
     * writes go through store(), which does enforce it.
     *
     * They do NOT bypass the sound CPU's control registers. Poking 0xB8 has to
     * release the Z80 exactly as the game's own write would -- those bytes are an
     * ACTION, not storage, and a debugger that could write them without the action
     * happening would be lying about the machine. */
    for (uint32_t i = 0; i < n; ++i) {
        const uint32_t a = (addr + i) & kAddrMask;
        m->mem[a] = in[i];
        if (a == kZ80ResetRegister || a == kZ80NmiRegister || a == kZ80CommRegister)
            z80_control_write(*m, a, in[i]);
    }
    return 0;
}

NGPC_API int ngpc_set_breakpoints(ngpc_t* h, const uint32_t* pcs, uint32_t n) {
    if (!h) return -1;
    Machine* m = reinterpret_cast<Machine*>(h);
    m->breakpoints.assign(pcs, pcs + n);
    return 0;
}

}  // extern "C"
