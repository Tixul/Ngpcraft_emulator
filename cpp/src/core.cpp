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
    /* The cartridge IS the flash chip. Its block map comes from its size, and a game
     * saves by erasing and programming the small blocks at the top (SDK FlashMem.txt). */
    m->flash_build_blocks(0, uint32_t(len));
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
    if (address < 0x200000 || uint64_t(address) + len > 0x400000) return -1;
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

NGPC_API void ngpc_reset(ngpc_t* h, int reset_mode) {
    if (!h) return;
    Machine* m = reinterpret_cast<Machine*>(h);
    const bool apply_bios_handoff = (reset_mode == kResetHandoff);
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
     * do when you swap cartridges. See machine.hpp, and note that ares lands on exactly
     * the same rule from the other direction. */
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
    m->apu.reset();
    /* The COMMAND LATCH resets; the flash CONTENTS do not. A power cycle with the
     * cartridge still in the slot does not wipe your save, and neither does this.
     * (`reset_memory()` above reloads the cart image, so a front end that wants the
     * save back must hand it over with `ngpc_flash_restore` -- which is exactly what
     * putting the cartridge back in does.) */
    m->flash_mode[0] = m->flash_mode[1] = Machine::FlashRead;
    m->flash_step[0] = m->flash_step[1] = 0;

    if (apply_bios_handoff) {
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
    ngpc::kIrqVectorIndexInt5,       /* the SOUND CPU interrupting the main one */
    ngpc::kIrqVectorIndexVBlank,
    ngpc::kIrqVectorIndexIntT0, ngpc::kIrqVectorIndexIntT0 + 1,
    ngpc::kIrqVectorIndexIntT0 + 2, ngpc::kIrqVectorIndexIntT0 + 3,
    ngpc::kIrqVectorIndexIntAd,
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
     * The evidence is the GAME's own configuration, not an emulator's: if level 0 killed
     * the DMA too, Puyo Pop's split could never have worked on the silicon it shipped on.
     * (NeoPop's `TestIntHDMA` checks the DMA vectors first and tests no level anywhere --
     * it agrees, but it is the corroboration, not the reason.)
     *
     * Delivering such a request to the CPU instead sends it into a BIOS stub that jumps
     * through a user hook nobody installed, lands at address 0, hits the `swi 7` there,
     * and the BIOS powers the console off. Ten ROMs did exactly that.
     * See specs/MICRO_DMA.md. */
    bool dma_ran = false;
    for (unsigned index : kIrqSourceIndices) {
        if (!(m.irq_pending & (1u << index))) continue;
        if (m.micro_dma_service(index)) {
            m.irq_pending &= ~(1u << index);   /* consumed -- the CPU is not disturbed */
            dma_ran = true;
        }
    }

    unsigned best_index = 0;
    uint8_t  best_level = 0;
    bool     found = false;
    for (unsigned index : kIrqSourceIndices) {
        if (!(m.irq_pending & (1u << index))) continue;
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
    m.irq_pending &= ~(1u << best_index);
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

NGPC_API int ngpc_run(ngpc_t* h, uint32_t max_instrs,
                      ngpc_record_t* out_records, uint32_t records_cap,
                      ngpc_summary_t* out_summary) {
    if (!h) return -1;
    Machine* m = reinterpret_cast<Machine*>(h);

    ngpc_summary_t s;
    std::memset(&s, 0, sizeof(s));
    s.stop_status = NGPC_COUNT_REACHED;

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
        m->fetch_window = pc_before;   // fetch bytes read in read8() get the cheap cart cost
        const uint8_t st = ngpc::step(*m, rec);

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

            bool woke = false;
            for (unsigned line = 0; line <= kScanlinesPerFrame; ++line) {
                advance_raster(*m, kCyclesPerScanline);
                m->adc_tick(kCyclesPerScanline);
                m->rtc_step(kCyclesPerScanline);
                m->timer_tick(kCyclesPerScanline);
                z80_tick(*m, kCyclesPerScanline);
                m->apu.tick(kCyclesPerScanline);
                s.total_cycles += kCyclesPerScanline;
                m->total_cycles += kCyclesPerScanline;
                if (m->irq_pending && deliver_irq(*m)) {
                    ++s.irq_deliveries;
                    woke = true;
                    break;
                }
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

        /* THE CART FLASH IS SLOW: fold in the wait-states this instruction paid reading
         * the cart bus (fetch + data), accumulated in read8(). This is what makes cart
         * code run at silicon speed instead of ~3.4x too fast (Machine::cart_wait,
         * measured by cpu_calib_v1.ngc). Default cart_wait=0 leaves access_wait at 0. */
        rec->cycles = uint16_t(rec->cycles + m->access_wait);
        m->access_wait = 0;
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
        z80_tick(*m, rec->cycles);
        m->apu.tick(rec->cycles);

        /* ...and so does interrupt delivery, BETWEEN instructions. */
        if (m->irq_pending && deliver_irq(*m)) {
            ++s.irq_deliveries;
            s.total_cycles += kIrqDeliveryCycles;
            m->total_cycles += kIrqDeliveryCycles;
            /* The 13 cycles of an interrupt entry are cycles LIKE ANY OTHERS:
             * every peripheral clock runs through them. Only the raster used
             * to -- so each delivery slid the timers' (and the APU's, and the
             * Z80's) clock 13 cycles against the raster. At 10-15 deliveries a
             * frame that swept a whole 515-cycle line every few frames, and
             * Metal Slug's raster split beat up and down one line with it. */
            advance_raster(*m, kIrqDeliveryCycles);
            m->adc_tick(kIrqDeliveryCycles);
            m->rtc_step(kIrqDeliveryCycles);
            m->timer_tick(kIrqDeliveryCycles);
            z80_tick(*m, kIrqDeliveryCycles);
            m->apu.tick(kIrqDeliveryCycles);
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
NGPC_API void ngpc_set_cart_wait(ngpc_t* h, uint32_t cycles_per_byte) {
    if (!h) return;
    reinterpret_cast<Machine*>(h)->cart_wait = cycles_per_byte;
}

NGPC_API void ngpc_set_cart_data_wait(ngpc_t* h, uint32_t cycles_per_byte) {
    if (!h) return;
    reinterpret_cast<Machine*>(h)->cart_data_wait = cycles_per_byte;
}

NGPC_API void ngpc_set_vram_wait(ngpc_t* h, uint32_t cycles_per_byte) {
    if (!h) return;
    reinterpret_cast<Machine*>(h)->vram_wait = cycles_per_byte;
}

NGPC_API void ngpc_set_ldir_cost(ngpc_t* h, uint32_t cycles_per_byte) {
    if (!h) return;
    reinterpret_cast<Machine*>(h)->ldir_cost = uint16_t(cycles_per_byte ? cycles_per_byte : 7);
}

/* Present the cart as a flash chip of `bytes` capacity (a standard 4/8/16 Mbit part),
 * rebuilding the erasable-block map. A real flashcart's chip is bigger than an under-filled
 * homebrew ROM, and a game that saves in the chip's top block (StarGunner -> block 33 at
 * 0x1FA000 on a 16 Mbit part) needs that block to EXIST. 0 = leave it at the ROM size. */
NGPC_API void ngpc_set_flash_size(ngpc_t* h, uint32_t chip, uint32_t bytes) {
    if (!h || chip > 1 || bytes == 0) return;
    reinterpret_cast<Machine*>(h)->flash_build_blocks(int(chip), bytes);
}

NGPC_API void ngpc_raise_irq(ngpc_t* h, uint32_t vector_index) {
    if (!h || vector_index >= 32) return;
    reinterpret_cast<Machine*>(h)->irq_pending |= (1u << vector_index);
}

NGPC_API void ngpc_set_apu_channel_mask(ngpc_t* h, uint32_t mask) {
    if (!h) return;
    // bit0..2 = squares, bit3 = noise, bit4 = DAC. Debug mute/solo only.
    reinterpret_cast<Machine*>(h)->apu.channel_mask = uint8_t(mask & 0x1F);
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
