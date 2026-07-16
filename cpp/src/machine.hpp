/* machine.hpp — internal C++ machine state. NOT part of the ABI.
 *
 * Concrete-state by contract (specs/CPP_CORE_PORT.md §2): every register always
 * holds a value. The Python core's `int | None` tri-state and its ~215
 * `requires-known-*` honest stops are an ANALYSIS feature and stay in Python.
 * What survives here is the other kind of honest stop: hardware truths
 * (silicon-broken, bios-shutdown, division-by-zero) and coverage gaps
 * (NGPC_UNIMPLEMENTED), which must trap loudly — HARDWARE_COMPAT_POLICY.md §9.
 */
#ifndef NGPC_MACHINE_HPP
#define NGPC_MACHINE_HPP

#include <algorithm>
#include <cstdint>
#include <cstring>
#include <vector>

#include "ngpc_core.h"
#include "apu.hpp"
#include "z80.hpp"

namespace ngpc {

constexpr uint32_t kAddrMask = 0x00FFFFFF;   /* 24-bit address space */
constexpr size_t   kMemSize  = 0x01000000;   /* 16 MB flat           */

/* Frame pacing. The scanline period is the manufacturer's ("internally 515 clock",
 * K2GE Tech Ref § 4-8). The scanline COUNT is now MEASURED ON SILICON.
 *
 * ⚖️ 199, AND IT USED TO BE 198.  hw_calibration/bin/main.ngc, flashed on a real
 * NGPC, reads RAS.V (0x8009) and prints its MAXIMUM before the wrap:
 *
 *      the console printed 00C6 = 198   ->  the counter runs 0..198  ->  199 LINES
 *
 * The Tech Ref's sentence ("signal generation for the 0th line occurs at the
 * beginning of line 198") was AMBIGUOUS -- 198th line, or index 198? We read it as
 * 198 lines. The silicon says index 198 exists, so the frame is 199 lines and the
 * refresh is 6_144_000 / (515 * 199) = 59.95 Hz.
 *
 * ⛔ Do NOT "restore" 198 because a document sounds like it says so. The document is
 * ambiguous; the register is not. */
constexpr uint32_t kCyclesPerScanline = 515;
constexpr uint32_t kScanlinesPerFrame = 199;   // MEASURED: RAS.V reaches 198
constexpr uint32_t kVisibleScanlines  = 152;

/* --- the interrupt controller (specs/FRAME_TIMING.md) ----------------------
 * Every source is named by the CHIP'S OWN hardware vector index, so we invent no
 * numbering of our own. VBlank is index 11 -> slot 0xFFFF2C, and it arrives on
 * the INT4 pin at **level 4** -- the SNK SDK says so in as many words
 * ("Vertical Blanking Interrupt (Interrupt level 4)"), and the pass-184
 * retraction settled it after a wrong inference had raised it to 6.
 *
 * Toshiba's rule (CPU manual, SR bits 12-14): a level-L interrupt is accepted
 * when **L >= IFF** (not `>`), and on acceptance the mask becomes **min(L+1, 7)**.
 * Both of those were off by one in this project once; do not "simplify" them. */
constexpr uint32_t kIrqVectorTableBase   = 0xFFFF00;
constexpr uint32_t kBiosBase             = 0xFF0000;
constexpr unsigned kIrqVectorIndexVBlank = 11;          // -> 0xFFFF2C  (it IS the INT4 pin)
constexpr uint8_t  kIrqLevelVBlank       = 4;

/* ⚡ INT5 -- THE SOUND CPU'S INTERRUPT TO THE MAIN CPU. Vector index 12 (0xFFFF30).
 *
 * The Z80 raises it by WRITING ITS OWN 0xC000. SNK says so in as many words, and its
 * table of contents lists TWO registers where this project only ever wired one:
 *
 *   § 5.2.2  PC INT Control Register   C000h, write-only:
 *            "INTx request to the PC generated with write access (data invalid)"
 *   § 5.2.4  Z80 Interrupt Acknowledge  ports 00..FF, write-only:
 *            "Releases INT request to the Z80 with write access (invalid data)"
 *
 * ⛔ THIS WAS TRIED ONCE AND WRITTEN OFF AS REFUTED -- "raising INT0 there took the
 * corpus from 52 ROMs drawing a picture down to 35". THE EXPERIMENT WAS RIGHT AND THE
 * VECTOR WAS WRONG. SNK writes "INTx", we read it as INT0, and INT0 is a different
 * pin. The BIOS itself names the real one: it programs INTE45 = 0xDC, which is INT4 at
 * level 4 (that is VBlank) and **INT5 at level 5** -- and then sits on `ei 5 ; halt`,
 * accepting nothing below 5. It is waiting for INT5. (ares agrees, independently:
 * `case 0xc000: return cpu.int5.raise();`.)
 *
 * 🔑 A REFUTATION IS ONLY AS GOOD AS THE THING IT REFUTED. "Writing 0xC000 raises an
 * interrupt" was never the claim that failed; "it raises INT0" was. */
constexpr unsigned kIrqVectorIndexInt5   = 12;          // -> 0xFFFF30
constexpr uint32_t kK2geControlAddress   = 0x008000;    // bit 7 = VBlank IRQ enable
constexpr uint32_t kK2geRasterAddress    = 0x008009;    // RAS.V — the current scanline
constexpr uint32_t kK2geStatusAddress    = 0x008010;    // bit 6 = BLNK
constexpr uint16_t kIrqDeliveryCycles    = 13;

/* --- the A/D converter (TMP95C061 datasheet, Figure 3.12) ------------------
 * The NGPC uses it for exactly one thing: the battery gauge. That is not a
 * cosmetic detail -- the BIOS reads the cached reading at 0x6F80, compares it
 * against a low-battery threshold, and POWERS THE CONSOLE OFF if it looks flat.
 * The reading only ever gets there because the A/D COMPLETION interrupt handler
 * puts it there. A core without a converter never boots a real BIOS. */
constexpr uint32_t kAdmodAddress    = 0x00006D;   /* mode/status register      */
constexpr uint32_t kAdreg0LowAddr   = 0x000060;
constexpr uint32_t kAdreg0HighAddr  = 0x000061;
constexpr uint8_t  kAdmodEocf  = 0x80;   /* conversion End    (R)             */
constexpr uint8_t  kAdmodAdbf  = 0x40;   /* conversion Busy   (R)             */
constexpr uint8_t  kAdmodRepet = 0x20;   /* 1 = repeat mode                   */
constexpr uint8_t  kAdmodAdcs  = 0x08;   /* 1 = low speed (320 states)        */
constexpr uint8_t  kAdmodAds   = 0x04;   /* write 1 = START. Always reads 0.  */
/* "160 States = 12.8 us (at 25 MHz)" => one state is two clocks. */
constexpr uint32_t kAdcCyclesHighSpeed = 160 * 2;
constexpr uint32_t kAdcCyclesLowSpeed  = 320 * 2;
/* INTAD is vector value 0x0070 (Table 3.3 (1)) => table entry 0x70/4 = 28. */
constexpr unsigned kIrqVectorIndexIntAd = 28;
constexpr uint8_t  kIrqLevelIntAd       = 4;
/* 10-bit full scale. An emulator has no cell, so we model a healthy one; a flat
 * reading would make the BIOS power the console off (see above). */
constexpr uint16_t kAdcFullScale = 0x03FF;

/* What the cartridge's flash chips answer to the BIOS's autoselect probe. The
 * BIOS accepts manufacturer 0x98 (Toshiba), 0xEC or 0xB0, and it checks the
 * device ID against 0xAB / 0x2C / 0x2F, plus a third byte it masks with 0xF8 and
 * expects to be 0x80. */
constexpr uint8_t kFlashManufacturerId = 0x98;   /* Toshiba */
constexpr uint8_t kFlashDeviceId       = 0x2C;
constexpr uint8_t kFlashDeviceId3      = 0x80;

/* ⚡ WHERE THE BIOS WRITES DOWN WHAT CARTRIDGE IT FOUND.
 *
 * At power-on the BIOS runs the autoselect probe on both chip-select windows and
 * stores a SIZE CODE for each: 1 = 4 Mbit, 2 = 8 Mbit, 3 = 16 Mbit, 0 = no card.
 * Its flash system calls (VECT_FLASHWRITE / VECT_FLASHERS, SysCall.txt) read this
 * byte FIRST, and return the error 0xFF without touching the cartridge if it is
 * zero -- which is what the whole save path did here, because the hand-off skips
 * the BIOS boot and nobody ever wrote it.
 *
 * The addresses and the encoding are not guessed: booting the real BIOS with a
 * 4/8/16 Mbit cartridge and reading these two bytes back gives 1/2/3 (pass 240).
 * That experiment also proves the autoselect model below is right -- the BIOS
 * could only have learnt the size by asking OUR chip. */
constexpr uint32_t kBiosFlashCardType0 = 0x006C58;   /* CS0 -- the game cartridge */
constexpr uint32_t kBiosFlashCardType1 = 0x006C59;   /* CS1 -- the development slot */

/* --- THE SUB-BATTERY: the console's RAM is NOT volatile ---------------------
 *
 * A NGPC keeps its 12 KiB of work RAM alive with a coin cell, which is why the BIOS
 * remembers your language, the date and the colour theme across a power-off -- and why
 * PULLING THE BATTERIES RESETS THE BIOS (the user hit exactly this on real hardware).
 * Boot the real BIOS with a blank RAM and it says "SUB BATTERY DEAD" and runs its
 * first-time wizard, every single time.
 *
 * `0x6C7A` is the marker the BIOS writes when it enters or leaves a halt, so it is
 * never zero once the console has booted once. On power-on the hardware consults it:
 *
 *    RAM blank  -> the RESET vector (0xFFFF00): a first-ever boot.
 *    RAM kept   -> VECT_SHUTDOWN (0xFFFE00) with XSP = 0x6C00, so the BIOS can finish
 *                  the cleanup it would normally do when you switch cartridges.
 *
 * (Derived here in pass 237 from SNK's own code; ares reaches the identical rule
 * independently -- `ngp/cpu/cpu.cpp::power`, testing `ram[0x2c7a]`.) */
constexpr uint32_t kRamStart      = 0x004000;
constexpr uint32_t kRamSize       = 0x003000;      /* 12 KiB */
constexpr uint32_t kBiosRamMarker = 0x006C7A;      /* non-zero once it has booted once */
constexpr uint32_t kHwResetVector = 0xFFFF00;
constexpr uint32_t kVectShutdown  = 0xFFFE00;
constexpr uint32_t kBiosBootXsp   = 0x006C00;      /* a system call needs a stack */

/* How the machine comes up. Overloading one bool was hiding a third case. */
enum ResetMode : int {
    kResetRaw     = 0,   /* PC = cart entry, nothing seeded. The synthetic-ROM / fuzz
                          * mode the differential gate runs both cores in. */
    kResetHandoff = 1,   /* PC = cart entry + the state the BIOS boot would have left.
                          * The DEFAULT: what a game sees when the console hands over. */
    kResetBiosBoot = 2,  /* THE CONSOLE POWERING ON. PC = the hardware reset vector and
                          * the real BIOS runs. Needs a BIOS image; without one the
                          * vector table reads zero and there is nothing to run. */
};

/* --- the four 8-bit timers (TMP95C061 + the official SNK SDK, 8Bit.txt) -----
 * Two of the corpus ROMs park on a HALT and never wake, because the interrupt
 * they are waiting for is a TIMER one and this core had no timers. On silicon a
 * HALT with every source silent is a hang; here it was an artefact.
 *
 *   TRUN   (0x20)  bit n = run timer n; bit 7 = PRRUN (the prescaler itself)
 *   TREG0..3       the compare values -- a TREG of 0 matches on OVERFLOW
 *   T01MOD (0x24)  bits 1-0 = timer 0 source, bits 3-2 = timer 1
 *   T23MOD (0x28)  bits 1-0 = timer 2 source, bits 3-2 = timer 3
 *
 * Clock sources: an EVEN timer takes 00 = the external pin, 01/10/11 = T1/T4/T16.
 * An ODD timer takes 00 = the paired timer's overflow (the 16-bit cascade),
 * 01/10/11 = T1/T16/T256. On the NGPC the 2D controller's HORIZONTAL BLANK is
 * wired to the external pin TI0, so timer 0 in mode 00 counts SCANLINES; timer 2
 * has no external pin and simply does not count there.
 *
 * Prescaler taps in CPU cycles, from the SDK's measured periods at 6.144 MHz:
 * T1 = 20.83us = 128 cycles, T4 = 512, T16 = 2048, T256 = 32768 (and 128 x 256 =
 * 32768, which is the internal consistency check). The reference emulator uses
 * 240 here and even labels its own timer path "HACK"; we follow the SDK. */
constexpr uint32_t kTrunAddress   = 0x000020;
constexpr uint32_t kTreg0Address  = 0x000022;
constexpr uint32_t kTreg1Address  = 0x000023;
constexpr uint32_t kT01modAddress = 0x000024;
constexpr uint32_t kTreg2Address  = 0x000026;
constexpr uint32_t kTreg3Address  = 0x000027;
constexpr uint32_t kT23modAddress = 0x000028;
constexpr uint8_t  kTrunPrescaler = 0x80;
/* Toshiba Table 3.3 (1): the vector VALUE divided by four. */
constexpr unsigned kIrqVectorIndexIntT0 = 0x40 / 4;   /* 16 */

/* --- interrupt PRIORITY IS PROGRAMMABLE, NOT A CONSTANT --------------------
 * VBlank is fixed at level 4 (the SNK SDK says so outright), but every other
 * source reads its level out of an INTxx register AT DELIVERY TIME, one nibble
 * each -- and **a level of 0 means the source is DISABLED** (Toshiba's levels run
 * 1..7). That register is exactly what the BIOS call VECT_INTLVSET writes.
 *
 * I hard-coded level 4 for the timers on the first attempt. The corpus answered
 * immediately: 69 ROMs clean fell to 56, with sixteen of them parked on a HALT,
 * because timers whose level software had left at 0 were firing anyway and
 * derailing the boot. Read the register.
 *
 *      vector 16 (INTT0) -> 0x0073 low nibble    vector 18 (INTT2) -> 0x0074 low
 *      vector 17 (INTT1) -> 0x0073 high          vector 19 (INTT3) -> 0x0074 high
 *      vector 28 (INTAD) -> 0x0070 high nibble   (it shares the register with INT0)
 */
/* --- THE MICRO-DMA (HDMA) ---------------------------------------------------
 * READ specs/MICRO_DMA.md. Everything below is from the TMP95C061 datasheet's SFR
 * table (p.184-185) and the SDK's own worked example -- nothing is inferred.
 *
 * An interrupt whose vector INDEX has been written into one of the four start-
 * vector registers is serviced by DMA and **NEVER VECTORS THE CPU**. That is how
 * the raster scroll works: timer 0 fires on every horizontal blank (11 880 times a
 * second) and the DMA copies the next scroll value out of a table -- without a
 * single CPU instruction. A core that delivers that interrupt to the CPU sends it
 * into a BIOS stub that jumps through a user hook nobody installed.
 *
 *     DMA0V..DMA3V = I/O 0x7C..0x7F, holding the vector INDEX (0x10 = timer 0).
 *
 * The transfer parameters are CPU CONTROL registers, and the game's own code fixes
 * the map beyond doubt (`ldc DMAD0,XWA` = cr 0x10, `ldc DMAM0,A` = cr 0x22):
 *
 *     DMASn = 0x00 + 4n   source        DMACn = 0x20 + 4n   counter (16-bit)
 *     DMADn = 0x10 + 4n   destination   DMAMn = 0x22 + 4n   mode    (8-bit)
 *
 * DMAM = (mode << 2) | zz, with zz = 0 byte / 1 word / 2 four bytes, and
 *
 *     mode 0: (DMAD+) <- (DMAS)     I/O -> memory, destination increments
 *     mode 1: (DMAD-) <- (DMAS)
 *     mode 2: (DMAD)  <- (DMAS+)    memory -> I/O, source increments  <- raster
 *     mode 3: (DMAD)  <- (DMAS-)
 *     mode 4: (DMAD)  <- (DMAS)     fixed, I/O -> I/O
 *     mode 5: counter only
 *
 * The game writes DMAM0 = 0x09 (mode 2, word) with DMAD0 = 0x8034 -- the SCROLL
 * register -- and DMAM1 = 0x08 (mode 2, byte) with DMAD1 = 0x8118. The SDK's
 * example calls 0x08 "memory to I/O byte transfer mode". It matches exactly. */
constexpr uint32_t kDma0vAddress = 0x00007C;   /* .. 0x7F */

/* ⚠️ HYPOTHESIS TESTED AND REFUTED -- the nibble order of INTET01/INTET23.
 *
 * A game sets timers 0 and 1 up as a 16-BIT CASCADE (T01MOD = 0x00 makes timer 1
 * count timer 0's overflows), and a cascade produces its interrupt on the UPPER
 * half. So it looked as though INTET01's enabled nibble had to be INTT1's, not
 * INTT0's -- i.e. that this map has the two the wrong way round.
 *
 * Swapping them moved the corpus by exactly one ROM (52 -> 53 drawing, 10 -> 9
 * halting) and BROKE two others in a new way: SNK Gals' Fighters and Sonic stopped
 * halting and started executing at address 0x000000 instead -- a crash wearing a
 * different hat. That is not a fix, it is a symptom moving. Reverted.
 *
 * The real defect is still upstream and still unexplained: the BIOS's Timer-0 ISR
 * (hardware vec[16] -> 0xFF22A5) jumps THROUGH THE USER VECTOR AT 0x6FD4, which
 * nobody -- not the BIOS, not the game -- ever writes. It is zero, the CPU lands at
 * address 0, hits the `swi 7` there, and the BIOS's trap handler powers the console
 * off. Something is wrong before that jump, and swapping nibbles does not find it. */
struct IrqPriorityReg { uint32_t address; bool high_nibble; };
inline bool irq_priority_register(unsigned vector_index, IrqPriorityReg& out) {
    switch (vector_index) {
        case 16: out = {0x0073, false}; return true;   // INTT0
        case 17: out = {0x0073, true};  return true;   // INTT1
        case 18: out = {0x0074, false}; return true;   // INTT2
        case 19: out = {0x0074, true};  return true;   // INTT3
        case 8:  out = {0x0070, false}; return true;   // INT0
        case 28: out = {0x0070, true};  return true;   // INTAD (shares 0x70 with INT0)
        /* INTE45 (0x71) carries BOTH halves of the pair this project kept apart:
         *   low nibble  = INT4 -- and VBlank IS the INT4 pin
         *   high nibble = INT5 -- the sound CPU interrupting the main one
         * VBlank's level used to be HARDCODED to 4 here, on the SDK's word ("Vertical
         * Blanking Interrupt (Interrupt level 4)"). That sentence describes what the
         * BIOS leaves, not what a game keeps: every game measured reprograms it --
         * Sonic, Puyo Pop and Metal Slug all write INTE45 = 0x32, i.e. **VBlank at
         * level 2 and INT5 at level 3**. With VBlank frozen at 4 it OUTRANKED INT5,
         * and on silicon it is the other way round. A level that software writes is
         * not ours to fix. */
        case 11: out = {0x0071, false}; return true;   // INT4  == VBlank
        case 12: out = {0x0071, true};  return true;   // INT5
        default: return false;
    }
}

/* BIOS hand-off seed (DEVLOG pass 48, sourced from NGPC_HW_QUICKREF §2 and
 * ngpcspec.txt): XSP = top of user RAM, interrupts masked (DI at boot). */
constexpr uint32_t kBiosHandoffXsp      = 0x6C00;
constexpr uint8_t  kBiosHandoffIffLevel = 7;
/* INTE45, as the real BIOS leaves it: INT4 (VBlank) level 4, INT5 level 5. */
constexpr uint8_t  kBiosHandoffInte45   = 0xDC;

/* ⚡ THE REGISTERS THE REAL BIOS LEAVES FOR THE CART. MEASURED ON SILICON, TWICE.
 *
 * `04_MY_PROJECTS/hw_entry_regs` freezes all eight registers in its very first
 * instruction (it IS the cart entry point) and prints them. Flashed on a real NGPC:
 *
 *      XIX = 00FF23C3   XWA = 000000DD   XSP = 00006C00   <- STABLE across power-ons
 *      XIY = 00FF23DF   XBC = 00200018
 *      XIZ = 00006480
 *
 *      XDE, XHL  ->  DIFFERENT ON EVERY POWER-ON (0x00006BFF then 0x002040FF; 0x50)
 *
 * ⚠️ TWO READINGS, AND THEY DISAGREED -- WHICH IS ITSELF THE ANSWER. Six registers
 * came back IDENTICAL both times; XDE and XHL did not. They are BIOS scratch, and no
 * cartridge can depend on them. So they are NOT seeded: seeding one measurement of a
 * value that is not reproducible would be dressing up a coin toss as a fact.
 * (And it is why a single flash is never enough. One sample does not make a rule.)
 *
 * ✅ XSP CONFIRMED: our long-standing 0x6C00 is exactly what the console reports
 * (the ROM prints 0x6BFC, its own prologue having already pushed 4 bytes).
 *
 * We used to hand the cart EIGHT ZEROS, and that is not a neutral choice -- it is a
 * wrong one, and it broke a game.
 *
 * PUYO POP is the proof. Its init loop clears both tilemaps at once:
 *
 *      ld  XIZ, 0x9000
 *      loop:  ldw (XIZ+), 0      ; SCR1
 *             ldw (XIX+), 0      ; ... and NOTHING IN THE CARTRIDGE SETS XIX
 *             djnz BC, loop      ; 1024 times
 *
 * On silicon XIX points INTO THE BIOS ROM, which is READ-ONLY: those 1024 writes are
 * DISCARDED and the loop is harmless. With our zero, they landed on the I/O PAGE and
 * wiped the timer registers -- so timer 3 stopped, the Z80 took no interrupt, the
 * sound driver never answered the handshake at 0x70DE, and the main CPU spun forever
 * on a blank screen. The game is sloppy; it works because the BIOS hands it a pointer
 * that cannot do damage.
 *
 * ⛔ AND THIS IS WHY WE ASKED THE CONSOLE INSTEAD OF GUESSING. The working hypothesis
 * was XIX = 0x9800 (SCR2's base, exactly the right size) -- forcing it made the game
 * boot, which felt like proof. IT WAS NOT. 0x9800 and 0xFF23C3 have only one thing in
 * common: NEITHER IS THE I/O PAGE. A fix that works for the wrong reason is a fix that
 * will break the next game.
 *
 * ⚠️ One console, one BIOS. XBC (0x200018) points into the cart header, so it may well
 * be header-derived rather than cart-independent -- but it is MEASURED, and zero was
 * not. */
constexpr uint32_t kBiosHandoffXix = 0x00FF23C3;
constexpr uint32_t kBiosHandoffXiy = 0x00FF23DF;
constexpr uint32_t kBiosHandoffXiz = 0x00006480;
constexpr uint32_t kBiosHandoffXwa = 0x000000DD;
constexpr uint32_t kBiosHandoffXbc = 0x00200018;
/* XDE and XHL: NOT seeded. They vary between power-ons -- see above. */

/* --- the USER interrupt vector table (RAM) -------------------------------
 * SysPro.txt: every interrupt vectors through the BIOS, which chains to a user
 * handler pointer in RAM at `0x6FB8 + 4n` -- 18 slots. The BIOS's power-on code
 * FILLS all 18 with a default stub before it ever starts the cartridge:
 *
 *     FF239D  ld   XIY, 0x00FF23DF     <- the default handler ...
 *     FF23A2  ld   XIX, 0x00006FB8     <- ... the table ...
 *     FF23A7  ld   BC, 0x0012          <- ... 18 entries ...
 *     FF23AA  ld   (XIX+), XIY
 *     FF23AD  djnz BC, 0xFF23AA
 *     FF23DF  reti                     <- and the stub is a bare RETI.
 *
 * THIS IS WHY GAMES SURVIVE AN INTERRUPT THEY NEVER HOOKED. Fatal Fury enables
 * the H-blank interrupt (INTT0, level 3) at boot and only arms the micro-DMA on
 * the screens that actually scroll a raster -- for every other screen the H-int
 * fires 152 times a frame and lands on this RETI. We hand off to the cartridge
 * WITHOUT running the BIOS's power-on code, so the table stayed all-zero, the
 * CPU jumped to address 0, hit the `swi 7` there, and the BIOS error handler
 * powered the console off. That was the ten "halting" ROMs (DEVLOG pass 208).
 *
 * The stub address is READ OUT OF THE BIOS IMAGE, never memorised: we find the
 * fill routine by its `ld XIX, 0x00006FB8` anchor and take the `ld XIY, imm32`
 * in front of it. A BIOS that does not contain the routine leaves the table
 * zeroed and says so, rather than inventing an address to jump to. */
constexpr uint32_t kUserVectorTableBase  = 0x6FB8;
constexpr unsigned kUserVectorTableSlots = 18;

enum class Region { Unmapped, IoPage, Ram, K2ge, Vram, CartRom, Bios };

uint8_t step(struct Machine& m, ngpc_record_t* rec);

/* shared between execute.cpp and mem_family.cpp */
bool eval_cc(const ngpc_cpu_t& c, unsigned cc);
void store(struct Machine& m, ngpc_record_t* rec, uint32_t addr, uint32_t value, uint8_t size);

/* The extended register-file codes (`0xE0|(xreg<<2)|byte` and the bank escapes).
 * Recovered with the official Toshiba assembler; see reg_family.cpp. The memory
 * family needs them too: the indexed modes `(r32 + r8)` / `(r32 + r16)` name
 * their base and index registers with exactly these codes. */
uint32_t* rcode_slot(ngpc_cpu_t& c, uint8_t code, unsigned& pos);
uint32_t rd_rcode(ngpc_cpu_t& c, uint8_t code, uint8_t sz);
uint32_t rd_rcode(const ngpc_cpu_t& c, uint8_t code, uint8_t sz);
void wr_rcode(ngpc_cpu_t& c, uint8_t code, uint8_t sz, uint32_t val);
bool exec_reg_family(struct Machine& m, ngpc_record_t* rec, uint8_t op, uint32_t pc,
                     uint8_t& out_len, uint16_t& out_cycles, uint32_t& new_pc, bool& jumped);
bool exec_mem_family(struct Machine& m, ngpc_record_t* rec, uint8_t op, uint32_t pc,
                     uint8_t& out_len, uint16_t& out_cycles, uint32_t& new_pc, bool& jumped);

Region region_of(uint32_t addr);
bool   region_writable(Region r);

struct Machine {
    std::vector<uint8_t> mem;
    std::vector<uint8_t> rom;
    std::vector<uint8_t> bios;
    /* What the coin cell kept. Empty = the cell is dead (or was never fitted), which is
     * a blank RAM and a BIOS that says so. Restored by `reset_memory()` AFTER the wipe,
     * exactly like the flash contents: a power cycle does not erase either of them. */
    std::vector<uint8_t> battery_ram;
    ngpc_cpu_t           cpu{};
    std::vector<uint32_t> breakpoints;

    /* frame pacing */
    uint32_t scanline     = 0;
    uint32_t frame_count  = 0;
    uint32_t cycle_residue = 0;

    /* THE RASTER LOG -- the K2GE display registers as they stood at the START of
     * each visible scanline.
     *
     * A frame is not one picture drawn from one set of registers. Games rewrite the
     * scroll registers WHILE the beam runs -- Sonic drives its parallax by having
     * the micro-DMA write S2SO.H (0x8034) on every H-blank from a table, which is
     * exactly what pass 206 found when it decoded DMAD0. Sampling the registers once
     * per frame renders such a game with a single arbitrary offset, and both scroll
     * planes then carry the SAME offset all the way down the screen -- which is what
     * we measured on Sonic, at every sample, without one exception.
     *
     * Start-of-line is the right instant to sample, and the manufacturer says so:
     * the K2GE Tech Ref's caution on both 0x8030 and 0x8032 reads "The result of the
     * value set in this register is displayed FROM THE NEXT LINE being drawn." So a
     * write during line N lands on line N+1, and the snapshot taken as line N begins
     * is precisely the set of values line N is drawn with. */
    static constexpr uint32_t kRasterRegBase  = 0x008000;
    static constexpr uint32_t kRasterRegCount = 0x40;    /* 0x8000..0x803F */
    uint8_t raster_log[kVisibleScanlines][kRasterRegCount] = {};

    /* ⚡ THE PICTURE, drawn ONE LINE AT A TIME as the beam passes -- which is what the
     * silicon does, and the only way a game that streams VRAM mid-frame comes out right.
     * Raw 12-bit 0BGR, exactly what the palette holds. See render.cpp. */
    static constexpr uint32_t kScreenWidth  = 160;
    static constexpr uint32_t kScreenHeight = kVisibleScanlines;   /* 152 */
    uint16_t framebuffer[kScreenWidth * kScreenHeight] = {};
    void render_scanline(uint32_t line);
    void snapshot_raster_line(uint32_t line) {
        if (line < kVisibleScanlines)
            std::memcpy(raster_log[line], &mem[kRasterRegBase], kRasterRegCount);
    }

    /* pending interrupts, keyed by the chip's own hardware vector index */
    uint32_t irq_pending = 0;

    /* How a family reports a stop that is NOT "not ported yet".
     *
     * Returning false from a family means UNIMPLEMENTED -- an encoding this core
     * has not learned. That is the wrong label for an encoding it HAS learned and
     * whose result the manufacturer leaves undefined (a DAA on non-BCD data, a
     * divide by zero). Those set `pending_status`; step() sees it, leaves PC where
     * it was, and reports the honest reason. */
    uint8_t pending_status = NGPC_OK;

    /* A/D converter state. Owned by the machine so a conversion survives across
     * run() batches, and ticked with the cycles each instruction consumed. */
    uint16_t adc_battery = kAdcFullScale;
    int32_t  adc_cycles_remaining = 0;
    bool     adc_busy = false;
    void adc_tick(uint32_t cycles);

    /* --- the on-board calendar IC (RTC), I/O 0x90..0x97 --------------------
     * A Neo Geo Pocket keeps a real-time clock alive on the coin cell. The BIOS
     * reads it at power-on; a lost/invalid clock is how it decides the coin cell
     * is DEAD -> "SUB BATTERY DEAD" + the first-run wizard, forever (the game
     * never boots). Modelling it as a valid, ticking BCD clock is what lets the
     * real-BIOS boot reach the cartridge. Layout mirrors ares ngp/cpu (io.cpp):
     *   0x90 enable(bit0) · 0x91 year · 0x92 month · 0x93 day · 0x94 hour
     *   0x95 minute · 0x96 second · 0x97 weekday(bits0-3) + (year&3)<<4
     * All fields are BCD. Seeded to a valid date at reset so the cell reads good. */
    struct Rtc {
        uint32_t counter = 0;
        uint8_t enable = 1;
        uint8_t year = 0x24, month = 0x01, day = 0x01;   /* 2024-01-01 */
        uint8_t hour = 0x00, minute = 0x00, second = 0x00, weekday = 0x01;
    } rtc;
    void    rtc_step(uint32_t cycles);
    uint8_t rtc_read(uint32_t addr) const;
    void    rtc_write(uint32_t addr, uint8_t value);

    /* Timer state. The up-counters are internal chip state -- they are NOT
     * memory-mapped -- so they live here, while TRUN / TREG / TxxMOD are read out
     * of the I/O page, which is where software's writes land. */
    uint32_t timer_count[4] = {0, 0, 0, 0};
    uint32_t timer_clock[4] = {0, 0, 0, 0};
    /* H-INT pulses the K2GE has produced and timer 0 (TI0, mode 00) has not yet
     * consumed. Raised by advance_raster ON THE RASTER'S OWN CLOCK -- see the
     * pulse schedule note there. This replaces a PRIVATE cycle accumulator whose
     * phase against the raster was whatever history left it at; in Metal Slug it
     * sat exactly ON a line boundary, so IRQ-delivery quantisation flipped the
     * game's raster split between two lines and the HUD's top line flickered. */
    uint32_t ti0_pending_pulses = 0;
    /* TO3 -- timer 3's external output pin, which is what the Z80's interrupt line
     * hangs off. It is a flip-flop, so it TOGGLES on each match and the Z80 gets one
     * interrupt per FULL period, i.e. one per two matches. Silicon: 976 INTT3 on the
     * main CPU against 485 interrupts taken by the Z80, over the same two seconds. */
    uint32_t to3_half_periods = 0;
    /* ⚖️ MEASURED ON SILICON. 128. And an EAR had put it at 512 -- wrongly.
     *
     * This is the prescaler tap the timers' mode field selects, and everything
     * downstream of it -- the music tempo of EVERY game -- is this one number, since
     * timer 3 is the only interrupt that drives the sound CPU and the games all
     * program it identically (T23MOD = 0x05, TREG3 = 98). Tempo = base x 98.
     *
     * WHAT WAS ALREADY SETTLED. The serial port's baud generator runs off the same
     * prescaler; the BIOS writes BR0CR = 0x05 for the link cable's documented
     * 19 200 bps, so phi-T0 = fc/4 and fc = 6.144 MHz. The LADDER was sure. Which
     * RUNG the 2-bit mode field picks was not: the SDK and the datasheet name the
     * taps differently and contradict each other.
     *
     *     tap k=4 : 128 cycles   <- the SDK calls this "T1"
     *     tap k=6 : 512 cycles   <- the SDK calls this "T4"
     *
     * ⛔ THE HISTORY, BECAUSE IT IS THE LESSON. This core used 128. A playtest said
     * the music ran "far too fast", so it was changed to 512 -- picked by ear, blind,
     * out of four candidates. It felt like strong evidence. It was not: the audio
     * pipeline was ALSO broken at the time (the player ran at 62.5 fps and piled up
     * one to two seconds of latency), so the tempo was being judged through a defect.
     *
     * ⚖️ hw_calibration/bin/main.ngc -- built with the OFFICIAL Toshiba toolchain and
     * flashed on a real NGPC -- counts INTT3 across 120 VBlanks and PRINTS the count.
     * The console printed 03D0 = 976, i.e. ~488 ticks a second:
     *
     *     1 / (488 x 98) s  =  20.9 us  =  128 cycles at 6.144 MHz
     *
     * ⇒ THE TAP IS 128. The ear was wrong by a factor of four, and the ROM settled it
     * with an integer. Do not re-tune this by listening. `ngpc_set_timer_base` keeps
     * it a knob for experiments, not for opinions. */
    uint32_t timer_base = 128;   // MEASURED on real hardware
    void timer_tick(uint32_t cycles);

    /* ⚖️ THE CARTRIDGE FLASH IS SLOW. Every instruction is FETCHED from the cart at
     * 0x200000, and on silicon the flash bus adds wait-states per byte. This core (and
     * ares, and BizHawk) fetched the cart for FREE, so cart code ran ~3.4x too fast --
     * MEASURED by hw_calibration/cpu_calib_v1.ngc: on a real NGPC the short, fetch-bound
     * ops (BASE/ADD/SHIFT/MEM) run ~3.4x slower than this core, the execution-bound ones
     * (MUL/DIV) ~2.5x -- the exact signature of a per-fetch-byte penalty, while the raster
     * (RASV=198) matches. It does not change VBlank-locked games (Fatal Fury) but it is
     * why the SELF-TIMED games (Cool Boarders, Densha de Go) fit their frame's work in one
     * VBlank here and run at 60 fps where silicon spills to two VBlanks and 30 fps.
     *
     * Cycles added per BYTE accessed on the cart bus -- instruction fetch AND data
     * reads, since both cross the slow flash bus. 0 = the old free-fetch behaviour.
     * Calibrated by cpu_calib_v1.ngc. `ngpc_set_cart_wait` is the knob; do not re-tune
     * it by feel. `access_wait` accumulates the penalty over one instruction's reads and
     * the run loop folds it into that instruction's cycle count. */
    uint32_t cart_wait = 0;
    /* Random data reads off the cart pay MORE than sequential fetch: flash page-mode
     * makes consecutive fetch bytes cheap, but an arbitrary LD from a cart table eats the
     * full random-access latency. `cart_wait` is the per-fetch-byte cost (calibrated by
     * the register-loop ROM); `cart_data_wait` the per-data-byte cost (calibrated so the
     * silicon-confirmed 30fps of Cool Boarders reproduces). 0 => fall back to cart_wait. */
    uint32_t cart_data_wait = 0;
    /* EXPERIMENTAL: wait-states per byte written to the display RAM (0x8000-0xBFFF).
     * Hypothesis under test (not yet silicon-confirmed): the K2GE "adjustment circuitry"
     * throttles CPU VRAM access during the active drawing period, so a game doing a big
     * per-frame ldir into char RAM (Cool Boarders -> 0xBC00) is slower on silicon than a
     * CPU model alone predicts. 0 = off. Needs a v3 calibration ROM to confirm. */
    uint32_t vram_wait = 0;
    /* Cycles per byte for LDIR/LDDR block copies. Datasheet 7n+1 (default 7); may be a floor
     * like MUL/DIV were. 14 reproduces Cool Boarders' silicon 30fps without touching Fatal
     * Fury. `ngpc_set_ldir_cost` is the knob; pending a clean silicon measurement. */
    uint16_t ldir_cost = 7;
    mutable uint32_t access_wait = 0;
    /* Set by the run loop to the PC of the instruction being executed, so read8() can
     * tell a fetch byte (inside [pc, pc+8)) from a data read and charge the right cost. */
    mutable uint32_t fetch_window = 0xFFFFFFFFu;

    /* The SOUND CPU. It is held in reset until the main CPU writes 0x55 to 0xB8,
     * and it lives in the same flat address space -- its memory IS the shared
     * window at 0x7000. See z80.hpp. */
    Z80      z80;
    uint8_t  z80_int_ack = 0;
    uint64_t z80_port_writes = 0;
    Apu apu;                     /* the T6W28 -- wired, see apu.hpp */

    /* The APU write log. The T6W28 is modelled (core/apu.py) but not yet wired;
     * until it is, every write aimed at it is RECORDED -- not just counted -- so
     * that the audio chantier starts from what the real sound drivers actually
     * do, rather than from an assumption about which door they use. */
    static constexpr size_t kApuLogSize = 4096;
    ngpc_apu_write_t apu_log[kApuLogSize] = {};
    uint64_t apu_writes = 0;      /* TOTAL ever seen; the log holds the last 4096 */
    uint64_t total_cycles = 0;    /* what the log timestamps against */

    void log_apu_write(uint16_t address, uint8_t value, uint8_t kind) {
        apu_log[apu_writes % kApuLogSize] = {total_cycles, address, value, kind};
        ++apu_writes;
        ++z80_port_writes;
    }

    /* --- the CARTRIDGE FLASH CHIPS ------------------------------------------
     * A cart is TWO flash chips, and the BIOS wants to know what they are before
     * it will boot: it runs the textbook AMD autoselect sequence on each and reads
     * back a manufacturer and a device ID.
     *
     *     (base + 0x5555) <- 0xAA
     *     (base + 0x2AAA) <- 0x55
     *     (base + 0x5555) <- 0x90        enter ID mode
     *         read (base + 0) = manufacturer, (base + 1) and (base + 3) = device
     *     (base + 0x5555) <- 0xF0        back to reading the array
     *
     * The BIOS accepts manufacturer 0x98 (Toshiba), 0xEC (Samsung) or 0xB0
     * (Sharp), and it checks BOTH chips. With no flash model at all, the second
     * chip answered 0x00, the BIOS concluded there was no cartridge, and TEN ROMs
     * went to sleep on `ei 5 ; halt` rather than boot. That halt was never an
     * interrupt problem: it was the BIOS refusing to run a cart it could not
     * identify. */
    /* ---------------------------------------------------------------- FLASH --
     * THE SAVE HARDWARE. The cartridge IS a NOR flash chip, and a game saves by
     * programming it in place. This core knew the AMD unlock sequence and the
     * autoselect ID (enough for the BIOS to identify the cart) and stopped there --
     * the erase and program commands were, in the old comment's own words,
     * "swallowed, not faked". Which means every save this emulator has ever taken
     * went nowhere, silently, and the user found out by losing one.
     *
     * The protocol is AMD/Fujitsu, and the block map is the manufacturer's
     * (SDK FlashMem.txt): 64 KiB blocks, with the LAST 64 KiB split 32 / 8 / 8 / 16.
     *
     * ⚠️ A NOR CELL CAN ONLY BE PULLED TO ZERO. Programming ANDs the byte in --
     * `cell &= data` -- and only an ERASE puts the 1 bits back (0xFF). A model that
     * simply stores the byte writes data the silicon could not have produced, and it
     * would hide exactly the bug a homebrew author needs to see: a slot programmed
     * twice without an erase in between. */
    enum FlashMode : uint8_t { FlashRead = 0, FlashReadId = 1, FlashWrite = 2, FlashAck = 3 };

    struct FlashBlock { uint32_t offset; uint32_t length; bool writable; };

    uint8_t  flash_mode[2] = {FlashRead, FlashRead};
    uint8_t  flash_step[2] = {0, 0};      /* how far into the AA/55/xx sequence */
    bool     flash_dirty[2] = {false, false};
    std::vector<FlashBlock> flash_blocks[2];

    void flash_build_blocks(int chip, uint32_t size);
    void flash_program(int chip, uint32_t base, uint32_t addr, uint8_t data);
    void flash_erase_block(int chip, uint32_t base, uint32_t addr);
    void flash_erase_all(int chip, uint32_t base);
    int  flash_block_of(int chip, uint32_t offset) const;

    /* How big the cartridge in `chip`'s slot is -- asked two different ways.
     *
     * `flash_device_id` is what the chip answers the autoselect probe; `flash_size_code`
     * is what the BIOS writes down after decoding that answer. They MUST agree, so the
     * size is decided in exactly one place and the other reads it back. Two independent
     * size ladders is how a 4 Mbit cartridge ends up being told it is 8 Mbit by one path
     * and 4 by the other. An empty slot has no chip: both answer 0. */
    uint8_t flash_device_id(int chip) const;
    uint8_t flash_size_code(int chip) const;
    bool    flash_present(int chip) const { return !flash_blocks[chip].empty(); }

    /* Run one micro-DMA transfer for the channel armed on `vector_index`, if any.
     * Returns true when the interrupt was CONSUMED by the DMA and must therefore
     * NOT be delivered to the CPU. */
    bool micro_dma_service(unsigned vector_index);
    bool flash_command(uint32_t addr, uint8_t value);
    bool flash_id_read(uint32_t addr, uint8_t& out) const;   /* T6W28 writes, counted until the APU lands */

    bool in_vblank() const { return scanline >= kVisibleScanlines; }

    Machine() : mem(kMemSize, 0) {}

    void reset_memory();

    /* Do what the BIOS's power-on code does to the user vector table, since the
     * hand-off skips it. See kUserVectorTableBase above for the disassembly and
     * for why leaving it zeroed powered ten ROMs off. Returns the stub address,
     * or 0 when the BIOS holds no fill routine (table left zeroed -- honest). */
    uint32_t seed_user_vector_table();

    inline uint8_t read8(uint32_t a) const {
        a &= kAddrMask;
        /* A flash chip in autoselect mode stops being memory: it answers its ID.
         * The check is two comparisons on the hot path and is worth it -- without
         * it the BIOS cannot identify the cartridge and refuses to boot it. */
        if ((flash_mode[0] || flash_mode[1]) &&
            ((a >= 0x200000 && a <= 0x3FFFFF) || (a >= 0x800000 && a <= 0x9FFFFF))) {
            uint8_t id;
            if (flash_id_read(a, id)) return id;
        }
        /* The RTC's registers answer from the clock, not from the byte the last
         * write happened to leave in the I/O page. */
        if (a >= 0x90 && a <= 0x97) return rtc_read(a);
        /* Port 0xB1 (ares ngp/cpu/io.cpp): bit1 = the CR2032 SUB-BATTERY, bit2 = a
         * must-be-1 line ("or SNK Gals' Fighter shows a link error"). Leaving them 0
         * is the whole "SUB BATTERY DEAD" loop -- the BIOS reads a dead coin cell and
         * never leaves the warning. Both read 1.
         *
         * bit0 is the POWER line read as a LEVEL. ares drives it !power (1 = released),
         * but MEASURED against this core it must stay 0: with bit0 forced to 1 the BIOS
         * boot parks blank at 0xFF1127 and never draws its language/clock screens,
         * whereas at 0 (the I/O page's own value) the boot renders them. This core
         * models POWER as INT0 rather than the NMI ares uses, and that difference is
         * exactly why the level polarity it wants is inverted -- the empirical render
         * wins over copying ares' polarity blind. So force only bits 1 and 2. */
        if (a == 0x0000B1) return uint8_t(mem[a] | 0x06);
        /* Slow cart flash: every byte the CPU reads from a cartridge window costs
         * wait-states. Sequential instruction fetch (inside the fetch window) is cheap;
         * a random data read pays cart_data_wait. Accumulated here and folded into the
         * instruction's cycles by the run loop. Guarded so the default path is unchanged. */
        if (cart_wait &&
            ((a >= 0x200000 && a <= 0x3FFFFF) || (a >= 0x800000 && a <= 0x9FFFFF))) {
            const bool is_fetch = (a - fetch_window) < 8u;   // unsigned wrap => outside == huge
            /* Silicon (cpu_calib_v2: CRND == RRND) says a cart DATA read costs the same
             * as RAM -- only the instruction FETCH is wait-stated. So data reads get
             * cart_data_wait, which defaults to 0 (free); no fallback to cart_wait. */
            access_wait += is_fetch ? cart_wait : cart_data_wait;
        }
        return mem[a];
    }
    inline uint32_t read32(uint32_t a) const {
        return uint32_t(read8(a)) | (uint32_t(read8(a + 1)) << 8) |
               (uint32_t(read8(a + 2)) << 16) | (uint32_t(read8(a + 3)) << 24);
    }

    /* Returns false when the write was DISCARDED (ROM / BIOS / unmapped).
     * A discarded write is still real information: it is what latches an AMD
     * flash command. The caller records it. */
    inline bool write8(uint32_t a, uint8_t v) {
        a &= kAddrMask;
        if (!region_writable(region_of(a))) return false;
        mem[a] = v;
        note_write(a, v);
        return true;
    }

    /* Every path that lands a byte in memory must come through here, or the write log
     * lies by omission. There are TWO such paths, and that is not an accident: the
     * CPU's `store()` does its own region check because it must also feed the flash
     * command latch and the Z80 control registers, so it writes `mem[]` directly. A
     * log hooked only into `write8()` reported ZERO writes to a tilemap that was
     * visibly changing -- an instrument that cannot fire is worse than none. */
    inline void note_write(uint32_t a, uint8_t v) { note_write_from(a, v, cpu.pc); }

    /* Same, for a write that did NOT come from the main CPU.
     *
     * The Z80 writes the shared RAM straight into `mem[]` too, so the log was blind
     * to it -- and the shared RAM is exactly where the two processors talk. Asking
     * "does the sound driver ever answer?" returned a confident ZERO, from an
     * instrument that could not fire. That is the SECOND time this log has lied by
     * omission; see the note above about `store()`.
     *
     * A Z80 program counter is 16-bit and a main-CPU one is 24-bit, so they would be
     * indistinguishable in the log. `kWlogZ80Pc` marks them: a reader that ignores
     * the flag still sees a plausible address, which is precisely the failure we are
     * refusing, so the flag is set OUTSIDE the 24-bit bus where it cannot be missed. */
    static constexpr uint32_t kWlogZ80Pc = 0x80000000u;

    inline void note_write_from(uint32_t a, uint8_t v, uint32_t pc) {
        if (a >= wlog_lo && a <= wlog_hi) {          // disarmed by default: lo > hi
            wlog[wlog_count % kWlogSize] = {pc, a, v};
            ++wlog_count;
        }
    }

    /* THE WRITE LOG -- who wrote here, and from what code?
     *
     * The native core had breakpoints on PC and nothing on memory, so the only way
     * to ask "which routine filled this tilemap, and why did it stop" was to guess.
     * This answers it: arm an address window, run, and read back (PC, address, value)
     * for every write that landed inside it. It is the native half of the Python
     * core's watchpoints, and it is the instrument this project's own method calls
     * for -- trace, first anomaly, then disassemble THE GAME'S code.
     *
     * Off by default (lo > hi), so the hot path pays two compares and nothing else.
     * The ring keeps the most recent kWlogSize writes; `wlog_count` is the TRUE total,
     * so a caller can always tell that it missed some rather than quietly seeing a
     * partial history. */
    struct WriteRec { uint32_t pc; uint32_t addr; uint8_t value; };
    static constexpr uint32_t kWlogSize = 8192;
    uint32_t wlog_lo = 1;      /* lo > hi  ==  logging off */
    uint32_t wlog_hi = 0;
    uint64_t wlog_count = 0;   /* every write seen, even the ones the ring dropped */
    WriteRec wlog[kWlogSize] = {};

    uint32_t rom_entry_point() const {
        if (rom.size() < 0x20) return 0x200000;
        return uint32_t(rom[0x1C]) | (uint32_t(rom[0x1D]) << 8) |
               (uint32_t(rom[0x1E]) << 16) | (uint32_t(rom[0x1F]) << 24);
    }
};

}  // namespace ngpc
#endif
