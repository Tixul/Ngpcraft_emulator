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
#include <deque>
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

/* ⏰ THE RTC ALARM -- vector index 10 (0xFFFF28), the INT0 pin the calendar chip drives.
 *
 * Not the power button, which is index 8: the two are separate lines that this project
 * confused for one, because the name "INT0" is used for index 10 in some register maps
 * and for index 8 in others. Settled by reading the retail BIOS's vector table: index 10 goes to
 * 0xFF2856, and that handler is the ONLY code in the 64 KiB that references 0x6FC8 --
 * the RAM vector the SDK documents as the RTC alarm hook. Index 8 goes to 0xFF1898, the
 * power/boot handler.
 *
 * They are related on hardware, which is what made the confusion plausible: with the
 * console switched OFF the alarm line is what powers it back on to sound (the SDK's
 * VECT_ALARMDOWNSET). While a game is running the same alarm arrives here instead --
 * which is why the SDK says the two alarm calls cannot both be set at once. */
constexpr unsigned kIrqVectorIndexRtcAlarm = 10;

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
 * accepting nothing below 5. It is waiting for INT5.
 *
 * 🔑 A REFUTATION IS ONLY AS GOOD AS THE THING IT REFUTED. "Writing 0xC000 raises an
 * interrupt" was never the claim that failed; "it raises INT0" was. */
constexpr unsigned kIrqVectorIndexInt5   = 12;          // -> 0xFFFF30
constexpr uint32_t kK2geControlAddress   = 0x008000;    // bit 7 = VBlank IRQ enable
constexpr uint32_t kK2geRasterAddress    = 0x008009;    // RAS.V — the current scanline
constexpr uint32_t kK2geStatusAddress    = 0x008010;    // bit 6 = BLNK
/* Cycles to ACCEPT an interrupt -- read the vector, push PC and SR, raise the
 * mask, bump INTNEST, jump. TMP95C061B datasheet 3.3.1 "General-Purpose Interrupt
 * Processing" gives this as a table, not a number:
 *
 *     bus width of stack area   bus width of vector area   states
 *              8 bit                     8 bit               28
 *              8 bit                    16 bit               24
 *             16 bit                     8 bit               22
 *             16 bit                    16 bit             * 18 *
 *
 * This core charged 13, which is not in the table AT ALL -- it was never taken
 * from the documentation. 18 is the entry for the configuration this console
 * wires up: the stack lives in the internal work RAM and the vector area is
 * 0xFFFF00..0xFFFFFF, both on 16-bit paths.
 *
 * ⚠️ AND THE MEASUREMENT CANNOT ARBITRATE THIS, so do not claim it did. Sweeping
 * all four legal values across every point the tester brought back moves the cost
 * of receiving a byte from 49 us to 51 (silicon says 93) and the empty BIOS loop
 * by 8 counts in 1632. Interrupt entry is simply not where this core's remaining
 * timing error lives; 18 is adopted because it is the documented value for a
 * 16/16 machine, not because silicon picked it out. Machine::irq_entry_cycles
 * overrides it at runtime for exactly that kind of experiment. */
constexpr uint16_t kIrqDeliveryCycles    = 18;

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
/* SERIAL channel 0 == the LINK CABLE. Vector values 0x18/0x19 (ngpcspec.txt IRQ
 * table; the same numbering as VBlank=0x0B, Timer0=0x10) => table entries at
 * 0xFFFF60 and 0xFFFF64, which the retail BIOS fills with real handlers. Their
 * programmable level lives in INTES0 (0x77), which COMINIT sets to 0xEE (both
 * level 6).
 *
 * ⚠️ WE RAISE BY BEHAVIOUR, NOT BY NAME -- the handlers are CROSS-WIRED versus the
 * SDK's "INTTX0"/"INTRX0" labels, verified by disassembling this BIOS:
 *   vector 0x18 (SDK "INTTX0", hook 0x6FE4 -> 0xFF2D03 -> 0xFF2C4D):
 *       `ld (RXring+W),(0x50)` -- it READS SC0BUF into the RX ring. RECEIVE.
 *   vector 0x19 (SDK "INTRX0", hook 0x6FE8 -> 0xFF2CF9 -> 0xFF2C17):
 *       `ld (0x50),(TXring+W)` -- it WRITES the next TX byte to SC0BUF. TRANSMIT.
 * Raising the vector by its SDK name gets you the opposite handler: raising 0x18
 * on a transmit made the receive handler read our own just-sent byte back into
 * the RX ring -- a self-loopback (measured: the console received its own pad). */
constexpr unsigned kIrqVectorSerialReceive  = 0x18;  /* handler FILLS the RX ring   */
constexpr unsigned kIrqVectorSerialTransmit = 0x19;  /* handler DRAINS the TX ring   */
/* One byte on the link cable, in CPU cycles. DERIVED, not assumed -- see below.
 *
 * ⚡ THE DERIVATION, forward, from manufacturer documents and our own measurements
 * only. (An earlier comment reasoned BACKWARDS -- "the cable's *documented* 19200 bps
 * implies phi-T0 = fc/4, therefore fc = 6.144 MHz" -- which cannot then be used to
 * justify the baud. This does it in the honest direction.)
 *
 *  1. fc ~= 6.144 MHz, from the VIDEO timing, independently of anything serial:
 *     515 cycles/line * 199 lines * 60 Hz = 6 149 100 (K2GETechRef).
 *  2. What the machine actually programs, measured across TEN cartridges
 *     (scratchpad/baud.py): every one converges on SC0MOD = 0x69, BR0CR = 0x05 --
 *     and the writes come from the BIOS (PC 0xFF2BC9/0xFF2BCF), never from the
 *     cartridge. There is no per-game serial configuration to look up.
 *  3. What those bits mean, TMP95C061 datasheet section 3.11:
 *       SC0MOD = 0x69 -> SM = 10   : UART, 8-bit length
 *                        SC = 01   : clocked by the baud rate generator
 *                        RXE = 1, CTSE = 1 (bit 6; channel 0 only, fig 3.11(12))
 *       BR0CR  = 0x05 -> BR0CK = 00: input clock phi-T0 = fc/4
 *                        BR0S  = 5 : divide by 5
 *       UART mode divides by a further 16 -- the transmission and receive counters
 *       are labelled "UART only /16" in the channel-0 block diagram, fig 3.11(12).
 *  4. Therefore baud = fc / 4 / 5 / 16 = fc / 320 = 19 200 bps,
 *     and 8N1 is 10 bit-times, so one byte = 10 / 19 200 s = 520.83 us
 *     = 0.00052083 * 6 144 000 = 3200 CPU cycles, exactly the value below.
 *
 * Flow control (RTS) and the 64-byte BIOS rings still absorb drift, so this is not
 * knife-edge for correctness -- but it is no longer a guess either.
 * See PERF_TIMING_POLICY.md and specs/LINK_CABLE.md §2.2. */
constexpr int32_t kSerialByteCycles = 3200;
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

/* `Language` (SDK SysWork.txt): 0 = Japanese, 1 = English, read-only to the cart.
 * A bilingual cartridge picks its script from this byte and nothing else -- 24 games
 * of the corpus read it. The console's setup wizard writes it and the coin cell keeps
 * it; we skip that wizard, so it sat at 0 and every one of those games ran in Japanese
 * by default rather than by choice. */
constexpr uint32_t kSysLanguage        = 0x006F87;
constexpr uint8_t  kLanguageJapanese   = 0;
constexpr uint8_t  kLanguageEnglish    = 1;

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
 * (Derived here in pass 237 from SNK's own code.) */
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
/* Micro-DMA transfer-END interrupts (INTTC0..3). Vector VALUES 0x74/0x78/0x7C/0x80
 * on the TMP95C061 (verified against THIS BIOS: hw vec[29] @0xFFFF74 dispatches, via
 * the BIOS stub at 0xFF22E1, through the user slot 0x6FF0 = HW_INT_DMA0). A game that
 * re-arms a MicroDMA channel from its completion ISR -- Ogre Battle Gaiden drives its
 * card-scene raster split this way, resetting SCR1_X=0 for the dialogue-box lines --
 * needs this delivered or the whole scroll plane, dialogue box and all, slides off. */
constexpr unsigned kIrqVectorIndexIntTc0 = 0x74 / 4;  /* 29 */

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
        /* ⚡ THE RTC ALARM, vector index 10 -- and until now it could never fire, because
         * a vector with no entry here reads back priority 0 and is dropped on the floor.
         *
         * Index 10 is the alarm and index 8 is the power button; they are NOT the same
         * line, which is what made this look unimplementable at first. Settled by asking
         * the BIOS: its vector table at 0xFFFF00 puts index 10 at 0xFF2856, and that
         * handler is the only code in the whole 64 KiB that references 0x6FC8 -- the RAM
         * vector the SDK documents as "RTC alarm interrupt". Index 8 goes to 0xFF1898,
         * the power/boot handler. (They ARE related on hardware, just not the same pin:
         * with the console switched off the alarm line is what powers it back on to
         * sound -- that is VECT_ALARMDOWNSET. While a game runs it arrives here instead.)
         *
         * The level lives in the low nibble of 0x0070, which it shares with INT0 -- the
         * Ghidra loader independently names that byte "RTC_Alarm_Level". */
        case 10: out = {0x0070, false}; return true;   // INT0 pin == the RTC alarm
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
        /* Serial channel 0 == the link cable. Both levels live in INTES0 (0x77):
         * high nibble = the 0x18 vector, low nibble = the 0x19 vector (INT.H:
         * ITX0M=0x70, IRX0M=0x07). COMINIT writes 0xEE (both level 6), so the
         * nibble split is moot here, but kept faithful. (The HANDLERS these
         * vectors run are cross-wired vs their names -- see the vector constants.) */
        case kIrqVectorSerialReceive:  out = {0x0077, true};  return true;  // vector 0x18
        case kIrqVectorSerialTransmit: out = {0x0077, false}; return true;  // vector 0x19
        /* Micro-DMA transfer-end levels. The NGPC keeps INTETC01/INTETC23 at 0x79/0x7A
         * (not the generic H1 core's 0xF0/0xF1): the BIOS's INTLVSET routine writes 0x79
         * here -- observed writing 0x09 (level 1) for Ogre Battle's channel-0 raster ISR. */
        case 29: out = {0x0079, false}; return true;   // INTTC0
        case 30: out = {0x0079, true};  return true;   // INTTC1
        case 31: out = {0x007A, false}; return true;   // INTTC2
        case 32: out = {0x007A, true};  return true;   // INTTC3
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
 * `hw_entry_regs` freezes all eight registers in its very first
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

/* INT0 IS THE POWER BUTTON (pass 235): the BIOS boots, arms it, and sleeps. */
constexpr uint32_t kInt0PowerButton      = 8;
/* One flash die is 2 MiB. A 4 MiB cart is two of them; the second is wired to 0x800000
 * (pass 247) -- so it is a second CHIP, with its own block map and its own identity. */
constexpr uint32_t kCartChipSize         = 0x200000;

/* --- HARDWARE SAFETY: WHAT THE CONSOLE MINDS AND THE CODE CANNOT SEE --------
 *
 * SysPro.txt gives user software 0x4000..0x6BFF. The 0x6C00..0x6FFF page is
 * system-managed. XSP may EQUAL 0x6C00 because the stack descends before it
 * writes; a cart that leaves it above that boundary puts a later push or call
 * into system RAM, and that is what makes the BIOS restart or power off.
 *
 * The same manual requires the watchdog control at I/O 0x006F to receive the
 * clear code 0x4E periodically -- ngpcspec.txt says at least every 100 ms.
 *
 * ⚡ REPORTED, NOT ENFORCED. Neither of these STOPS a real console at the
 * instruction that commits them: the stack overwrite corrupts, and the watchdog
 * raises INTWD (WDMOD bit1 picks reset or interrupt). An emulator that halts
 * there is stricter than the silicon, and being stricter than the silicon is
 * how you end up debugging the emulator instead of the ROM. So the core COUNTS
 * them, keeps the first samples with their PC, and runs on -- the hygiene
 * counters' contract, applied to hardware safety. A caller that wants a gate
 * ("this build must be clean") arms ngpc_set_hw_guard and gets the batch
 * stopped with the matching status instead.
 *
 * ⚠️ THE TIMEOUT IS AN ASSUMPTION, NOT A MEASUREMENT. The 100 ms of the SDK is
 * the refresh rate asked of the PROGRAM, not the counter's period, and the
 * WDMOD prescaler bits (WDTP, bits 6-5) are not modelled: the retail BIOS ends
 * its boot with WDMOD=0xF0, so the console hands over with the watchdog ARMED
 * on its longest setting. One CPU second at the measured 6.144 MHz is what ares
 * uses and what we use, pending a hardware measurement. Being wrong here
 * changes WHEN the counter reports, never whether the ROM runs. */
constexpr uint32_t kUserStackTop          = 0x006C00;
constexpr uint32_t kSystemRamEnd          = 0x006FFF;
constexpr uint32_t kWatchdogModeIo        = 0x00006E;
constexpr uint32_t kWatchdogIo            = 0x00006F;
constexpr uint8_t  kWatchdogClearCode     = 0x4E;
constexpr uint8_t  kWatchdogDisableCode   = 0xB1;
constexpr uint32_t kWatchdogTimeoutCycles = 6144000;

inline bool cart_pc(uint32_t pc) {
    return (pc >= 0x200000 && pc <= 0x3FFFFF)
        || (pc >= 0x800000 && pc <= 0x9FFFFF);
}

/* --- CHARACTER RAM AT HAND-OFF: THE BIOS'S BOOT SCREEN IS STILL IN IT --------
 *
 * The hand-off's job is to leave the cart the state a real boot would have left, and
 * character RAM was a hole in that list. On the console the BIOS draws its start-up
 * screen out of these 8 KiB, and the tiles are STILL THERE when the cart takes over --
 * nothing clears them. Our fast path handed the game 8 KiB of zeros.
 *
 * ⚡ AND ONE GAME READS THEM AS A COPY-PROTECTION CHECK. Metal Slug - 2nd Mission
 * scans 0xA000..0xC000 for a 64-byte run of BIOS tile data (its own copy of the
 * pattern is at cart 0x28DCC4) and, when it does not find it, wipes the magic
 * "MET2" at 0x6A88. A per-frame check at 0x28DD86 then zeroes 0x46DC/0x46DD -- the
 * two bytes that hold the FIRE and JUMP button masks. The masks feed `and A,B` in
 * the shared "is this button down?" helper at 0x2102A5, so a zero mask reads as
 * "never pressed": the player can walk and throw grenades (mask 0x40, a constant in
 * the code) but can NEVER SHOOT OR JUMP. The game runs, looks perfect, and is
 * unplayable -- which is precisely what the check is designed to do to a copy.
 *
 * 🔑 The symptom named the wrong culprit. "A and B do nothing" reads as an INPUT bug,
 * and the input was provably perfect all the way to 0x425E. The distinction between
 * A and B did not die in a decode: the game never asked about them, because the
 * question it asks is `and A, <mask>` and the mask had been zeroed for us.
 *
 * ⛔ NOT A BLOB. The tiles are not stored in the BIOS image in this form (259 of the
 * 377 non-empty tiles are a 1bpp font expanded on the fly; the rest are built), so
 * there is nothing to copy out and no anchor to search for. We get them the only
 * honest way: RUN THE BIOS'S OWN BOOT for a moment and read the machine, exactly as
 * pass 237 got the grey ramp and the entry registers. Measured on this BIOS: char RAM
 * is empty through frame 8, fills between 16 and 32, and never changes after -- so 64
 * frames is double the settling time, and costs ~0.08 s once, at reset. */
constexpr uint32_t kCharRamBase          = 0x00A000;
constexpr uint32_t kCharRamSize          = 0x002000;
constexpr uint32_t kBiosWarmUpFrames     = 64;
constexpr uint32_t kBiosWarmUpMaxInstrs  = kBiosWarmUpFrames * 200000;

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

    /* 🔍 DEBUG LAYER MASK -- the video twin of `Apu::channel_mask`. A cleared bit drops
     * one layer from the COMPOSITE only; nothing else changes, because a scroll plane
     * holds no state of its own to disturb. The silicon has no such register: this is an
     * inspection tool (which plane owns that text? what does the art look like without
     * it?), so it MUST default to all-on, or every image gate would be measuring the
     * mask instead of the core. Deliberately NOT part of a savestate. */
    static constexpr uint8_t kLayerScr1     = 0x01;
    static constexpr uint8_t kLayerScr2     = 0x02;
    static constexpr uint8_t kLayerSprBack  = 0x04;   /* PR.C = 1, behind both planes */
    static constexpr uint8_t kLayerSprMid   = 0x08;   /* PR.C = 2, between the planes */
    static constexpr uint8_t kLayerSprFront = 0x10;   /* PR.C = 3, in front of all    */
    static constexpr uint8_t kLayerAll      = 0x1F;
    uint8_t layer_mask = kLayerAll;

    void render_scanline(uint32_t line);
    void snapshot_raster_line(uint32_t line) {
        if (line < kVisibleScanlines)
            std::memcpy(raster_log[line], &mem[kRasterRegBase], kRasterRegCount);
    }

    /* pending interrupts, keyed by the chip's own hardware vector index */
    uint64_t irq_pending = 0;   /* bit = vector index; 64-bit so INTTC3 (index 32) fits */

    /* POWER = the console's NMI (non-maskable, vector index 8 -> 0xFFFF20). On real
     * hardware the BIOS boots into an idle HALT and the POWER-button NMI is what runs
     * its boot handler and hands off to the cartridge. We press it on the user's behalf
     * each time the BIOS parks at its idle HALT: the FIRST press runs the (first-boot)
     * setup, and once that finishes the BIOS parks again -- a SECOND press then re-runs
     * the boot handler, which this time sees the console configured and boots the cart.
     * The setup itself SPINS (never halts), so no press fires during it. Capped so a
     * handler that keeps bouncing back to idle cannot loop forever. */
    uint8_t power_nmi_count = 0;

    /* Set while a hand-off reset is booting the BIOS to read character RAM back out
     * of it (see kCharRamBase). The warm-up resets the machine, so without this the
     * hand-off would recurse into itself forever. */
    bool in_bios_warm_up = false;

    /* How a family reports a stop that is NOT "not ported yet".
     *
     * Returning false from a family means UNIMPLEMENTED -- an encoding this core
     * has not learned. That is the wrong label for an encoding it HAS learned and
     * whose result the manufacturer leaves undefined (a DAA on non-BCD data, a
     * divide by zero). Those set `pending_status`; step() sees it, leaves PC where
     * it was, and reports the honest reason. */
    uint8_t pending_status = NGPC_OK;

    /* --- THE WATCHDOG, WITH TIME ACTUALLY PASSING FOR IT ---------------------
     *
     * The core used to keep the last byte written at 0x006F and nothing else, so
     * the counter never advanced: a ROM that resets a real console by starving
     * the watchdog ran here forever, and the emulator agreed with it.
     *
     * On expiry the counter RE-ARMS and the run continues (ares does the same),
     * because that is the shape of the hardware: the event fires, software gets
     * to be wrong about it repeatedly, and a ROM that never refreshes reports
     * once per period rather than once per session. */
    uint32_t watchdog_cycles = 0;
    bool     watchdog_enabled = true;
    void watchdog_reset(bool enabled) {
        watchdog_enabled = enabled;
        watchdog_cycles = 0;
    }
    void watchdog_clear() { watchdog_cycles = 0; }
    /* True on the tick that crosses the timeout. */
    bool watchdog_tick(uint32_t cycles) {
        if (!watchdog_enabled) return false;
        watchdog_cycles += cycles;
        if (watchdog_cycles < kWatchdogTimeoutCycles) return false;
        watchdog_cycles -= kWatchdogTimeoutCycles;
        return true;
    }

    /* --- THE TWO HARDWARE-SAFETY FINDINGS ------------------------------------
     *
     * Same contract as the hygiene counters below: a count, plus the first few
     * samples so a report can name the code that did it. `hw_guard_stop` is the
     * opt-in gate -- a bitmask of NGPC_HW_* kinds that END the batch with the
     * matching status instead of merely being counted.
     *
     * The stack finding is EDGE-triggered: a cart that parks XSP in the system
     * page would otherwise report once per instruction and drown the log. What
     * matters is the crossing, and its PC is the code that moved the stack. */
    struct ViolationRec { uint32_t pc; uint32_t detail; uint64_t cycle; uint32_t kind; uint32_t _pad; };
    static constexpr uint32_t kHwSize = 64;

    uint32_t hw_guard_stop = 0;
    uint64_t hw_watchdog_count = 0;
    uint64_t hw_stack_count = 0;
    uint64_t hw_flash_fetch_count = 0;
    uint32_t hw_n = 0;
    ViolationRec hw[kHwSize] = {};
    bool stack_in_system = false;
    bool fetching_busy_flash = false;

    inline void hw_reset() {
        hw_watchdog_count = hw_stack_count = hw_flash_fetch_count = 0;
        hw_n = 0;
        stack_in_system = false;
        fetching_busy_flash = false;
    }

    inline void note_violation(uint32_t kind, uint32_t pc, uint32_t detail) {
        if (kind == NGPC_HW_WATCHDOG)           ++hw_watchdog_count;
        else if (kind == NGPC_HW_SYSTEM_STACK)  ++hw_stack_count;
        else                                    ++hw_flash_fetch_count;
        if (hw_n < kHwSize) hw[hw_n++] = {pc, detail, total_cycles, kind, 0};
    }

    /* Is this PC inside a cartridge window whose chip is mid-operation? The first test is
     * an OR of two bytes that are zero for the whole life of a ROM that never saves, so
     * the hot path pays one load and one branch. */
    inline bool pc_in_busy_flash(uint32_t pc) const {
        if (!(flash_mode[0] | flash_mode[1])) return false;
        if (pc >= 0x200000 && pc <= 0x3FFFFF) return flash_mode[0] == FlashBusy;
        if (pc >= 0x800000 && pc <= 0x9FFFFF) return flash_mode[1] == FlashBusy;
        return false;
    }

    /* A/D converter state. Owned by the machine so a conversion survives across
     * run() batches, and ticked with the cycles each instruction consumed. */
    uint16_t adc_battery = kAdcFullScale;
    int32_t  adc_cycles_remaining = 0;
    bool     adc_busy = false;
    void adc_tick(uint32_t cycles);

    /* --- serial channel 0 (SC0) == THE LINK CABLE, I/O 0x50-0x53 -------------
     * The NGPC link cable is TLCS-900 serial channel 0, driven by the SNK BIOS
     * COM routines. Games never touch SC0 directly: they call the BIOS, whose
     * TX/RX interrupt handlers move bytes between a 64-byte ring in RAM
     * (0x6C80 TX / 0x6CC0 RX) and SC0BUF (0x50). We model the cable as a byte
     * pipe with two FIFOs a host bridges (in-process for 2-player-on-one-PC, or
     * over a socket for online):
     *   - `serial_tx` : bytes this machine has transmitted (host drains -> peer)
     *   - `serial_rx` : bytes queued for this machine to receive (host fills)
     * A byte the BIOS writes to SC0BUF is captured (io_action_write), pushed to
     * serial_tx after one baud-time, then INTTX0 is raised so the BIOS fetches
     * the next; a byte in serial_rx is presented at SC0BUF (read8) and INTRX0
     * raised so the BIOS files it in the ring. Flow control: RTS = port 0xB2
     * bit0 (0 = ready to receive, set by COMONRTS). Disabled by default -> the
     * registers stay inert and the cable reads as unplugged, unchanged from
     * before. See specs/LINK_CABLE.md and reference-ngpc-link-cable-serial-bios. */
    bool     serial_link_enabled = false;
    std::deque<uint8_t> serial_tx;
    std::deque<uint8_t> serial_rx;
    bool     serial_tx_busy = false;
    /* ...and whether that byte has actually STARTED going out. CTS0 gates the START
     * of a byte only: once the shift register is loaded the byte always completes,
     * however the peer waves its RTS about (datasheet fig 3.11(16) Note 1). Without
     * this a mid-byte CTS pulse froze a transmission hardware would have finished --
     * see serial_tick in memory.cpp for the game that proved it. */
    /* STAGE 1 of the transmitter: SC0BUF itself. Only used when
     * `tx_irq_on_buffer_free` is armed; otherwise the core keeps its single-stage
     * model and these stay empty. The CPU writes HERE, and the byte moves into the
     * shift register (stage 2, the fields below) when that register goes idle --
     * which is the moment the buffer is free and INTTX0 is raised. */
    bool     serial_tx_buf_full = false;
    uint8_t  serial_tx_buf_byte = 0;

    bool     serial_tx_shifting = false;
    uint8_t  serial_tx_byte = 0;
    int32_t  serial_tx_cycles = 0;
    /* CTS0 handshake input (TMP95C061 datasheet 3.11): when SC0MOD<CTSE> (bit6) is
     * set, the transmitter HALTS a queued byte while CTS0 is HIGH and only shifts it
     * out -- raising INTTX0 -- once CTS0 goes LOW. CTS0 is not a readable register; it
     * is a hardware pin wired to the PEER's RTS (any GPIO; on NGPC RTS = 0xB2 bit0).
     * A host bridging two machines drives this from the peer's RTS (ngpc_serial_set_cts).
     * Default false (LOW = transferable) so a lone machine / CTSE-disabled game is
     * unchanged. This is what lets Card Fighters' Clash's mutual handshake sync. */
    bool     serial_cts_high = false;
    /* Has the bridge ever told us what the peer's RTS is doing?
     *
     * ⚠️ NEEDED BECAUSE serial_cts_high's DEFAULT MEANS "PEER READY", and that
     * default is deliberate on the transmit side (a lone console must not have its
     * transmitter frozen by a CTS nobody drives). Reusing it for the cable-detect
     * bit without this guard made a console with a cable armed and NO peer report
     * a peer -- measured B1=0x02 where silicon reads 0x07. Gals' Fighters reports a
     * link error on exactly that. So detect asks two questions: has anyone spoken
     * for the peer, and what did they say. */
    bool     serial_cts_seen = false;
    /* Mutable: read8 is const, but the RX-buffer read is an ACKNOWLEDGE (it clears
     * the presented byte so the next can arrive -- the hardware's overrun guard). */
    /* The receiver is TWO stages on this chip as well -- "the data are shifted in the
     * receiving buffer 1 whenever the receive interrupt flag ... is cleared by reading
     * the received data" (datasheet 3.11). So a byte can be arriving while the previous
     * one is still unread; a single slot throttles the wire. Stage 2 (SC0BUF, what the
     * CPU reads) is serial_rx_pending / serial_rx_byte below; this is stage 1.
     *
     * ⚠️ IMPLEMENTED, CORRECT, AND MEASURABLY INERT (2026-08-21). Every probe test
     * returns bit-identical numbers with it on or off: the BIOS drains SC0BUF fast
     * enough that a second byte never lands while the first is unread, so the extra
     * slot is never used. Kept OFF -- more faithful on paper, but nothing measures it,
     * and this project does not ship changes no measurement can defend. Turn it on the
     * day a workload actually overruns. (The transmitter's second stage is a different
     * story: it is worth 3529 -> 3790 bytes a window.) */
    bool            rx_double_buffered = false;
    bool            serial_rx_shift_full = false;
    uint8_t         serial_rx_shift_byte = 0;

    mutable bool    serial_rx_pending = false;
    bool            serial_rx_had_pending = false;
    mutable uint8_t serial_rx_byte = 0;
    int32_t  serial_rx_cycles = 0;
    /* --- waking the host when the cable moves (ngpc_set_serial_break) --------
     * NOT saved state: `serial_event` means "something crossed during THIS
     * ngpc_run", and ngpc_run clears it on entry. `serial_break_on_event` is a
     * host policy, like a breakpoint, not a property of the console -- which is
     * why neither belongs in ngpc_link_state_t. */
    bool     serial_break_on_event = false;
    bool     serial_event = false;
    /* Last RTS bit seen by serial_tick, to spot a CHANGE. 0xFF = never sampled,
     * so the first tick after a reset does not invent an edge. */
    uint8_t  serial_rts_last = 0xFF;
    /* --- counters, for the debugger's Link tab (ngpc_serial_state) ----------
     * Bytes crossing the cable are visible from Python, but WHY they are not
     * crossing is not: a byte can sit in serial_tx_busy because the peer holds
     * CTS, sit in serial_rx because our own RTS is high, or be presented and
     * never read because the BIOS's receive interrupt is masked. Counting each
     * step separates "no cable" from "cable fine, nobody is draining it" --
     * exactly the question the 2P-greyed-out hunt had to answer by guesswork.
     * Pure observation: nothing here feeds back into emulation. */
    uint32_t serial_tx_count = 0;        /* bytes the CPU handed to SC0BUF     */
    uint32_t serial_wire_count = 0;      /* ...that finished shifting out      */
    uint32_t serial_rx_queued_count = 0; /* bytes the host pushed at us        */
    mutable uint32_t serial_rx_read_count = 0;  /* ...that the CPU read back   */
    uint32_t serial_irq_tx_count = 0;    /* INTTX0 raised (vector 0x19)        */
    uint32_t serial_irq_rx_count = 0;    /* INTRX0 raised (vector 0x18)        */
    uint32_t serial_cts_hold_ticks = 0;  /* ticks a byte was held by CTS0 high */
    /* How many times the cable was relayed while this console ran. Only
     * ngpc_run_linked moves it: a host that owns its own relay counts its own
     * pumps. It exists because the property "the cable is relayed MANY times
     * inside one frame, not once" is what The Last Blade's handshake needs, and
     * once the relay moved into the core the test that guarded it was counting
     * pumps that no longer happen -- an instrument that cannot fire. */
    uint32_t serial_relay_count = 0;
    /* The widest gap, in cycles, that opened between this console and its peer
     * during the last ngpc_run_linked call. THE number that says whether the pair
     * was really interleaved: totals can look perfectly even while the two ran a
     * whole frame each in sequence, which is the latency that loses a handshake.
     * Reset at the start of every linked call. */
    uint64_t serial_pair_max_gap = 0;
    uint32_t serial_rts_hold_ticks = 0;  /* ticks RX was held by our own RTS   */
    /* One byte on the wire, in CPU cycles, COMPUTED from what the machine actually
     * programmed -- see the derivation above kSerialByteCycles and specs/LINK_CABLE.md
     * §2.2/§2.3. This used to be the constant itself, which was right only because
     * every cartridge happens to use the same setup: the BIOS writes SC0MOD = 0x69 and
     * BR0CR = 0x05 for all of them (measured on ten). A cartridge that programmed
     * BR0CR = 0x15 would pick phi-T2 instead of phi-T0 -- 4800 bps, four times slower --
     * and the old code would still have shifted a byte every 3200 cycles.
     *
     * For the values every game does use this returns exactly kSerialByteCycles, so the
     * whole library is bit-identical; only configurations nobody selects change. */
    int32_t serial_byte_cycles() const;

    void serial_tick(uint32_t cycles);

    /* --- the on-board calendar IC (RTC), I/O 0x90..0x97 --------------------
     * A Neo Geo Pocket keeps a real-time clock alive on the coin cell. The BIOS
     * reads it at power-on; a lost/invalid clock is how it decides the coin cell
     * is DEAD -> "SUB BATTERY DEAD" + the first-run wizard, forever (the game
     * never boots). Modelling it as a valid, ticking BCD clock is what lets the
     * real-BIOS boot reach the cartridge. Register map (SDK / QUICKREF §3):
     *   0x90 enable(bit0) · 0x91 year · 0x92 month · 0x93 day · 0x94 hour
     *   0x95 minute · 0x96 second · 0x97 weekday(bits0-3) + (year&3)<<4
     * All fields are BCD. Seeded to a valid date at reset so the cell reads good. */
    struct Rtc {
        uint32_t counter = 0;
        uint8_t enable = 1;
        uint8_t year = 0x24, month = 0x01, day = 0x01;   /* 2024-01-01 */
        uint8_t hour = 0x00, minute = 0x00, second = 0x00, weekday = 0x01;
        /* --- THE ALARM, at 0x98-0x9A (+ enable in 0x90 bit 1) ------------------
         * Undocumented: no datasheet and no Ghidra loader has these. Found by
         * MEASUREMENT -- running VECT_ALARMSET on the real BIOS and logging the I/O
         * page. The BIOS writes the day to 0x98, the hour to 0x99 and the minute to
         * 0x9A, then sets 0x90 to 0x03 (bit0 clock + bit1 alarm). Verified by varying
         * the values passed and watching all three registers follow.
         *
         * Compare granularity is day/hour/minute -- there is no alarm second, which
         * matches the SDK's ALARM struct {Day, Hour, Min, Code}. */
        uint8_t alarm_enable = 0;
        uint8_t alarm_day = 0, alarm_hour = 0, alarm_minute = 0;
    } rtc;
    void    rtc_step(uint32_t cycles);
    /* Wind the clock forward by whole seconds, through the same carry chain the tick
     * uses -- for the time that passed while the emulator was closed (the coin cell
     * keeps a real console's clock running when it is switched off). */
    void    rtc_advance_seconds(uint32_t seconds);
    void    rtc_tick_one_second();
    bool    rtc_alarm_due() const;
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
    /* ⏱️ LE MICRO-DMA COUTE DU TEMPS, ET NOUS FACTURIONS ZERO.
     *
     * Datasheet TMP95C061, micro-DMA : **8 etats** par transfert octet ou mot, **12** en
     * 4 octets, **5** en mode compteur. Un etat vaut deux cycles. Le coeur volait ces
     * cycles a la machine : un jeu qui enchaine les transferts tournait donc trop vite,
     * et Big Bang Pro Wrestling en boucle **3,1 fins de transfert par trame** (INTTC3)
     * -- exactement le symptome signale manette en main, en solo comme en link.
     *
     * ⚠️ 0 = ancien comportement, pour attribuer une regression. Le relevé disait « a
     * mesurer avant de corriger : le micro-DMA touche beaucoup de jeux » -- d'ou
     * l'interrupteur plutot qu'une constante en dur. */
    uint32_t micro_dma_states = 0;
    /* ⚠️ Le cout d'un transfert ne peut PAS passer par `access_wait` : celui-ci est
     * remis a zero a la fin de chaque instruction, et un micro-DMA declenche depuis la
     * phase d'aiguillage des interruptions se produit ENTRE deux instructions -- sa
     * facture etait donc jetee. Mesure : Fatal Fury bougeait (son DMA part d'un tick
     * timer, dans l'instruction), Big Bang **pas du tout** (le sien part de
     * l'aiguillage). On accumule ici, et l'appelant l'avance sur l'horloge comme il le
     * fait pour l'entree en interruption. */
    uint32_t dma_cost_cycles = 0;
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
     * 0x200000, and on silicon the flash bus adds wait-states per byte. This core -- like
     * every emulator measured alongside it -- fetched the cart for FREE, so cart code ran ~3.4x too fast --
     * MEASURED by hw_calibration/cpu_calib_v1.ngc: on a real NGPC the short, fetch-bound
     * ops (BASE/ADD/SHIFT/MEM) run ~3.4x slower than this core, the execution-bound ones
     * (MUL/DIV) ~2.5x -- the exact signature of a per-fetch-byte penalty, while the raster
     * (RASV=198) matches. It does not change VBlank-locked games (Fatal Fury) but it is
     * why the SELF-TIMED games (Cool Boarders, Densha de Go) fit their frame's work in one
     * VBlank here and run at 60 fps where silicon spills to two VBlanks and 30 fps.
     *
     * Cycles added per BYTE of instruction FETCH off the cart bus. Calibrated by
     * cpu_calib_v1.ngc -> 3. `ngpc_set_cart_wait` is the knob; do not re-tune it by feel.
     * `access_wait` accumulates the penalty over one instruction's reads and the run loop
     * folds it into that instruction's cycle count.
     *
     * ⚠️ THE DEFAULT HERE IS 0, AND 0 IS NOT THE SILICON VALUE. It means "the old
     * free-fetch behaviour": when wait-states were added the field was left off so the
     * pre-existing timing stayed bit-for-bit unchanged, so an A/B against it stayed
     * possible, and so read8()'s hot path costs nothing when it is off. The SHIPPING
     * default is ON and lives one layer up, in the shell -- `cart_wait_states()` in
     * ngpc_settings.py returns True and ngpc_shell.py calls the setters on every ROM
     * load. So the APP is silicon-timed; a bare Machine is NOT. Anything that
     * instantiates this class directly (a bench harness, the MCP server, a test) gets
     * free fetch and measures a machine ~2.9x too fast unless it applies the set itself:
     *
     *     set_cart_wait(3)  set_cart_data_wait(0)  set_ldir_cost(14)   [vram_wait: below]
     *
     * The trap that creates is real and has cost people weeks: with fetch free, every
     * saving that comes from SHORTER CODE measures as exactly zero, because the thing it
     * saves is the one thing not being billed. See README "Timing -- wait states". */
    uint32_t cart_wait = 0;
    /* Instruction FETCH out of the on-chip BIOS ROM (0xFF0000+). Zero by default,
     * i.e. free, which is what this core has always assumed -- and the assumption
     * is now in doubt. MEASURED 2026-08-19: with cart timing correct, plain
     * cartridge code (registers, ROM reads, RAM reads) runs 7-9% too fast here,
     * but any loop that CALLS THE BIOS runs 23-30% too fast, in three separate
     * regimes. Interrupts cannot explain it -- the quietest of those loops takes
     * no serial interrupt at all. What is left is the cost of executing BIOS code,
     * and this is the knob that would carry it.
     *
     * ⛔ TESTED AND RULED OUT, 2026-08-19 -- kept because the negative result is
     * worth more than the knob. A uniform fetch wait CAN be made to fit: at 3
     * cycles/byte the round trip lands on 1095 and the quiet BIOS loop on 1598,
     * against silicon's 1095 and 1593. Two independent points, exact.
     *
     * But it costs THROUGHPUT: saturated transfer falls from 3757 to 3467 while
     * silicon does 3890-3963. Doubling how hard the probe feeds its ring changes
     * nothing (3752 -> 3461), so it is not a margin artefact -- the model really
     * does say hardware has a slower BIOS AND a faster wire, which cannot both be
     * true. ⇒ the undercharge is NOT uniform across BIOS code. Look for the
     * specific instructions instead. Left at 0; do not "tune" it. */
    uint32_t bios_wait = 0;
    /* Cycles charged for accepting an interrupt. 0 = use kIrqDeliveryCycles.
     *
     * The datasheet (3.3.1) does not give ONE number, it gives four -- 28 / 24 /
     * 22 / 18 states, keyed on the bus width of the stack area and of the vector
     * area. Rather than assume which pair the console wires up, this knob lets
     * the four documented candidates be measured against silicon. It is a
     * DEBUGGING AID, not a tuning parameter: only a value from that table may be
     * adopted, and the winner is hard-coded into kIrqDeliveryCycles. */
    uint32_t irq_entry_cycles = 0;
    /* Multiplies an instruction's OWN cycles (not the fetch wait) before they are
     * charged. Default 1 = unchanged.
     *
     * ⚠️ IT EXISTS BECAUSE THIS CORE'S CYCLE UNIT IS NOT WHAT ITS NUMBERS SAY.
     * Every handler charges Toshiba's figure verbatim -- nop 2, push #8 4, swi 19
     * -- and those are STATES. The datasheet prices a state at 80 ns at 25 MHz
     * (micro-DMA table, 3.4.5) while 4.3 gives tosc = 40 ns at the same clock, so
     * ⇒ ONE STATE IS TWO fc PERIODS. And fc is the unit everything else here is
     * in: kSerialByteCycles = 3200 for a byte at 19200 bps holds against 551 us
     * measured on silicon, and the frame is 102485 of them at 60 Hz. So the
     * instruction table is charged in HALF-STATES, and cart_wait = 3 has been
     * absorbing the difference.
     *
     * ⛔ AND YET DOUBLING IT IS REFUTED. If the unit were the whole story, some
     * (scale 2, cart_wait W) would fit every point at once. Measured against the
     * silicon campaign:
     *
     *     scale cw |   REG    ROM    RAM   LOOP  throughput
     *       1    3 |   +9%    +7%    +7%   +23%    -6%     <- today
     *       2    1 |  +24%   +17%   +17%    -2%   -12%
     *       2    2 |   -2%    -6%    -6%   -14%   -11%
     *       2    3 |  -20%   -21%   -21%   -23%   -11%
     *
     * Short-instruction loops and BIOS-call loops still want different values of
     * cart_wait; doubling does not remove that contradiction, it only moves which
     * side of zero it sits on. 🔑 The residual error is SHAPED, not scaled -- it
     * lives in which instruction costs what, exactly as the 2026-08-19 histogram
     * said. Settling it needs the TLCS-900/H per-instruction table, which no
     * document in this project contains (the ones we hold are 900/L1 and series
     * 91, and treating those as interchangeable has burned this project once).
     *
     * Kept at 1 with the negative result beside it, same as bios_wait. */
    uint32_t base_scale = 1;
    /* EXPERIMENT: charge the fetch wait once per 16-BIT WORD instead of once per
     * byte. The CPU's external bus is 16 bits wide, so an instruction spanning n
     * bytes costs the bus ceil-ish n/2 accesses, not n. Modelled by billing only
     * on even addresses -- which is the physical rule, alignment included. */
    bool fetch_wait_per_word = false;

    /* EXPERIMENT: the bus interface unit runs AHEAD of the execution unit.
     *
     * "The instruction execution unit and the bus interface unit of this CPU operate
     * independently" -- TMP95C061B datasheet, 3.3.1 notes. So a fetch is not simply
     * ADDED to an instruction's cost: it overlaps execution, and the CPU only stalls
     * when the instruction queue has run dry. This core adds it unconditionally,
     * which over-charges long instructions (their fetch had time to hide) and
     * under-charges short ones (nothing to hide behind).
     *
     * That is EXACTLY the shape of the error left after the state/word corrections:
     * the short-instruction loop REG wants a fetch wait of ~3.8 per word while the
     * BIOS-call loop LOOP wants ~2.0. Adding the two costs cannot satisfy both;
     * overlapping them might.
     *
     * Modelled as a debt: each instruction lets the BIU do `fetch` cycles of work
     * while the CPU does `base`, and only the shortfall is charged. The BIU can run
     * at most one queue ahead -- without the clamp, one long instruction would pay
     * for every fetch that follows it.
     *
     * ✅ AND THE QUEUE DEPTH IS SOURCED, not assumed. The 900/L1 CPU manual says it
     * three times: Table 1 "Differences between CPUs" gives "Instruction queue buffer
     * 4 bytes" in the column that covers 900/H AND 900/L1; the feature list says
     * "Pipeline system with 4-byte instruction queue buffer"; and a note on LDC warns
     * that execution lags fetch "because an instruction queue (4 bytes) and pipeline
     * processing method is used". Four bytes is TWO 16-bit words, so the slack is
     * exactly 2 x the per-word cost -- derived, never fitted. */
    /* EXPERIMENT: raise INTTX0 when the byte leaves the BUFFER, not when it leaves
     * the WIRE.
     *
     * The chip has two stages: "Transmission buffer (SC0BUF/SC1BUF) shifts out and
     * sends the transmission data written from the CPU ... using transmission shift
     * clock TxDSFT" (datasheet 3.11). SC0BUF hands its byte to the shift register and
     * is then FREE -- so the transmit-empty interrupt fires at the START of a byte's
     * time on the wire, and the handler has a whole byte time to load the next one.
     * That is how a real console streams back to back.
     *
     * This core raises it at the END, so every byte is followed by an ISR-shaped gap.
     * MEASURED: silicon streams at 1.03 byte times per byte; we manage 1.16, and the
     * round trip is 9% short. The structural gap is real.
     *
     * ⛔ BUT REFUTED AS IMPLEMENTED (2026-08-21): throughput collapses from 3529 bytes
     * a window to **127**. The reason is the point itself -- raising the interrupt
     * early without ACTUALLY double buffering means the handler writes the next byte
     * into a SC0BUF that still holds the current one, and bytes are overwritten. The
     * early interrupt is not a one-line change: it needs a genuine two-stage model,
     * SC0BUF plus a separate shift register, with the CPU's write landing in the
     * buffer while the shift register drains independently.
     *
     * ⇒ Kept at false, with the negative result. The IDEA is still the best candidate
     * for the remaining throughput and round-trip shortfall; this implementation of it
     * is not. */
    /* EXPERIMENT: the UART transmits with NO CABLE ATTACHED.
     *
     * TxD is a pin. Nothing on the chip knows whether anything is plugged into it, so
     * writing SC0BUF always shifts a byte out and always raises INTTX0 -- and the BIOS
     * transmit handler always runs. This core captures the SC0BUF write only when the
     * link is enabled, so an unplugged console pays NOTHING for talking.
     *
     * That is exactly the shape of the last discrepancy: the probe's BIOS-call loop
     * runs UNPLUGGED and comes out 6% too SLOW, while the same COM calls with a cable
     * come out 7% too FAST. Two BIOS-heavy loops disagreeing by 13 points, and the
     * only thing separating them is the cable. */
    /* EXPERIMENT: percent added to the computed byte time.
     *
     * `serial_byte_cycles()` derives 3200 cycles from the divider chain, and that
     * derivation is sound. But silicon has never measured 3200: a 128-frame stream
     * carried 3875 bytes on 2026-08-19 (3387 cycles a byte, the 551 us in the log) and
     * 3963 on 2026-08-21 (3310). Both are ABOVE 3200, by 6% and 3%.
     *
     * The relevance: at bios_wait 8 the round trip lands at -0.2% and the BIOS loop at
     * +7%; at 10 the loop lands at -1% and the round trip at -5%. The two cannot be
     * zeroed together -- unless the WIRE is a few percent longer than 3200, which is
     * exactly what both silicon streams say. Default 0 = the derived value.
     *
     * ⛔ REFUTED 2026-08-21, BY THE SIGN. A longer byte time makes a round trip take
     * LONGER, so it drives the round trip further from silicon, not closer: at
     * bios_wait 10 it goes -5% -> -6% (+3%) -> -8% (+5%). The reasoning was backwards.
     * And a SHORTER wire is not available either -- saturated throughput already sits
     * at +1.1%, so shortening it overshoots there.
     *
     * ⇒ The QUIET/round-trip tension is NOT the wire. Kept at 0 with the refutation;
     * the knob stays because the derived 3200 has still never been confirmed by a
     * silicon stream (3387 and 3310 cycles measured, on two different shoots). */
    uint32_t serial_byte_extra_pct = 0;

    bool     uart_runs_unplugged = false;

    bool     tx_irq_on_buffer_free = false;

    /* EXPERIMENT: a TAKEN branch empties the instruction queue.
     *
     * Table 1 of the 900/L1 CPU manual, in the column that covers 900/H: "Code fetch
     * during branch instruction execution -- jump address code is fetched ONLY WHEN
     * BRANCH CONDITION IS TRUE" (the 900H2 prefetches both ways). So on this part the
     * queue holds sequential code, and a taken branch throws it away: the BIU's
     * run-ahead credit is gone and the next instructions pay their fetch in full.
     *
     * Why it matters here: the per-word wait that best fits silicon comes out at ~9.6
     * cycles, which is neither a whole number of cycles nor of STATES (a state is two
     * cycles). A cost that lands between the legal values is a cost carrying someone
     * else's weight -- and a flush is charged per taken branch, not per word, so it
     * is exactly the kind of term that would explain the mismatch.
     *
     * ⛔ REFUTED AS AN IMPROVEMENT, 2026-08-21 -- and the reason is a sign, not a
     * number. A flush can only make an emulator SLOWER, and against silicon this core
     * is ALREADY slow (CPU loops -4%, round trip -8%). Measured, it moves every point
     * the wrong way: REG -4 -> -7, LOOP -6 -> -11, round trip 1118 -> 1084.
     *
     * ⇒ The behaviour is almost certainly real -- it is documented for this exact core
     * -- but it CANNOT be the residual, because the residual has the opposite sign.
     * Whatever is missing makes the console FASTER than us, not slower. Kept at false;
     * revisit only once the emulator is on the fast side of silicon. */
    bool     flush_queue_on_branch = false;
    /* Cycles de credit d'avance qui SURVIVENT a une branche prise, quand le vidage
     * est arme. 0 = vidage total (le comportement d'origine du drapeau).
     *
     * ⚖️ POURQUOI CE N'EST PAS UN BOOLEEN. La ROM v13 fait varier la DENSITE de
     * branches d'un facteur 8 a travail constant et lit la PENTE du cout, en cycles
     * par branche prise. Les deux reglages extremes encadrent le silicium sans
     * l'atteindre :
     *
     *     drapeau desarme (vidage nul)     12,2 cy/branche
     *     SILICIUM (tir 2026-08-27)      ~18,2 cy/branche
     *     drapeau arme, keep = 0          24,1 cy/branche
     *
     * ⇒ une branche prise coute bien PLUS que sa ligne de table -- notre refutation
     * du 21/08 rejetait a raison le vidage TOTAL et a tort le vidage tout court --
     * mais environ la MOITIE de ce que coute un vidage total. La file fait 4 octets,
     * soit deux mots : ce que le silicium decrit est un mot perdu et un mot conserve,
     * pas une file videe.
     *
     * ⛔ CE N'EST PAS UNE CONSTANTE CALEE SUR UN JEU. C'est une pente mesuree sur
     * quatre points alignes, et l'ordonnee de la droite absorbe tout le cout du
     * travail : une erreur sur `mul` ou `ld` la deplace sans toucher la pente. */
    uint32_t branch_flush_keep = 0;
    /* Credit d'avance qui SURVIT a une INTERRUPTION, en cycles. 0 = tout est jete.
     *
     * ⚖️ MEME QUESTION QUE POUR LA BRANCHE, ET PROBABLEMENT LA MEME REPONSE. Une IRQ est
     * un transfert de controle : la v14 a mesure qu'une branche prise n'emporte que
     * ~2,4 cy du credit, pas les 16 d'une file pleine (`branch_flush_keep` ~13-14). Or
     * la livraison d'interruption, elle, met le credit a ZERO.
     *
     * ⚡ ET CA SE VOIT DANS LA TRACE. Le stub d'aiguillage du BIOS est
     *     FF22A5  push (0x6FD6)   \  il empile le vecteur utilisateur
     *     FF22A9  push (0x6FD4)   /  (Timer 0 = 0x6FD4, quatre octets)
     *     FF22AD  ret             -> et saute dessus
     * Deux `push (nn)` IDENTIQUES a l'adresse pres, que nous facturons **40 et 24
     * cycles**. Les 16 d'ecart sont exactement le credit que le vidage a jete, paye par
     * la premiere instruction du stub.
     * Mesure : ROM v18, cout FIXE d'une IRQ -- silicium 111,1 cy, nous 131,2. */
    uint32_t irq_flush_keep = 0;
    /* DIAGNOSTIC (modele en OCTETS) : etat de la file au sortir de l'acceptation d'une
     * interruption, en 1/16 d'octet. La file est videe -- le PC part au vecteur -- mais
     * l'acceptation dure 18 etats pendant lesquels le bus, lui, tourne. Deux regles se
     * defendent : « le bus est pris par les empilements et la lecture du vecteur, donc
     * rien n'est recharge » (0) et « ce sont des cycles comme les autres, donc la file
     * se recharge » (jusqu'a la capacite). Balaye, pas pose : voir le README v18. */
    int32_t irq_queue_keep_q16 = 0;
    mutable uint32_t dbg_bios_charges = 0;  /* instrumentation temporaire */
    int32_t dbg_debt_in = 0;   /* biu_debt A L'ENTREE de la derniere instruction */
    uint32_t dbg_stall = 0;    /* et le stall qu'elle a paye */
    uint32_t dbg_aw = 0;       /* et son access_wait */
    /* Idem pour le modele en OCTETS : etat de la file A L'ENTREE de la derniere
     * instruction, et nombre d'octets qu'elle a fait lire. Sans ca, la file est la
     * seule partie du modele qu'on ne peut qu'inferer -- et c'est un compteur, pas un
     * raisonnement, qui a trouve le bug d'IRQ du 27/08. */
    int32_t dbg_q_in = 0;
    uint32_t dbg_q_bytes = 0;
    /* Cycles ajoutes a CHAQUE branche prise, sans condition -- l'hypothese concurrente
     * de `branch_flush_keep`.
     *
     * ⚖️ LES DEUX REPRODUISENT LA ROM v13, ET C'EST TOUT LE PROBLEME. Le vidage
     * partiel ne coute que si la file avait pris de l'avance ; la surcharge coute
     * toujours. Sur la boucle de la v13 -- construite exprES pour que l'avance soit
     * maximale a la branche -- les deux donnent la meme pente. Ils ne se separent que
     * sur du code FETCH-BOUND, ou l'avance est nulle : le vidage y est invisible, la
     * surcharge non. C'est le corpus v2/v10/v11/v12 qui tranche, pas la v13.
     *
     * ⛔ Ne pas armer les deux ensemble : ils modelisent la MEME mesure. */
    uint32_t branch_taken_extra = 0;
    /* Couts OCTET de `mul` et `div` reg-reg, en ETATS et en CYCLES respectivement.
     * 0 = utiliser les constantes de reg_family.cpp.
     *
     * ⚖️ ILS SONT COUPLES A `biu_slack`, ET C'EST TOUT LE PROBLEME. Ces deux
     * instructions sont execute-bound : leur temps sert au bus a prendre de l'avance,
     * donc leur cout APPARENT depend de l'avance qu'on autorise. Les caler a slack=16
     * puis baisser slack les rend fausses, et inversement -- a slack=6 le corpus voit
     * MUL a -10,8 % et DIV a -7 % alors que TOUT LE RESTE reste juste.
     * ⇒ On ne les regle pas separement : `biu_slack`, `mul` et `div` se resolvent
     * ENSEMBLE contre trois mesures (v16 page 0, v14 pages 3 et 4). Ces deux boutons
     * existent pour rendre ce balayage possible sans recompiler. */
    /* ===================== LA FILE, MODELISEE EN OCTETS =====================
     * `queue_bytes` = taille de la file d'instructions, en octets (4 sur ce coeur).
     * 0 = ancien modele, un credit d'avance en CYCLES plafonne par `biu_slack`.
     *
     * ⚖️ POURQUOI REMPLACER LE CREDIT EN CYCLES. `biu_slack` etait UN scalaire pour
     * trois regimes, et trois mesures le tiraient dans trois directions :
     * v16 page 0 demandait ~6, le corpus 16, le cout d'IRQ davantage. Un scalaire ne
     * peut pas satisfaire les trois -- ce n'etait pas une valeur a trouver, c'etait la
     * FORME du modele qui etait fausse.
     *
     * ⚡ LE MODELE PHYSIQUE N'A PAS DE PARAMETRE LIBRE. La file fait 4 OCTETS et le bus
     * en livre un tous les `fetch_wait_byte_q16 / 16` cycles (4,00, mesure v14 p.1) :
     *
     *     il manque         need = n - q      octets a l'instruction  -> le CPU cale
     *                       cale = need x cout_octet
     *     puis elle consomme ses n octets     -> q = max(0, q - n)
     *     puis, PENDANT ses e cycles d'execution, le bus recharge
     *                       q = min(4, q + e / cout_octet)
     *
     * Les deux plafonds -- 4 octets en avance, et pas plus vite qu'un octet par
     * 4 cycles -- sont des FAITS de la machine, pas des reglages.
     *
     * ⚡ ET IL TOMBE JUSTE LA OU L'AUTRE ECHOUAIT. Sur le montage v16 page 0 (une
     * division inseree dans une chaine de charges limitee par le bus), le credit en
     * cycles rend un cout marginal de 16,0 cy ; le modele en octets rend 26, et le
     * silicium 26,5. La difference tient a ce que le credit en cycles laissait DEUX
     * charges profiter de l'avance (16 cy contre 10 cy de deficit chacune) la ou la
     * file, elle, ne contient jamais que 4 octets -- soit les 4/5 d'une seule charge. */
    uint32_t queue_bytes = 0;
    mutable int32_t q_sixteenths = 0;   /* remplissage courant, en 1/16 d'octet */
    mutable uint32_t fetch_bytes = 0;   /* octets d'instruction lus, l'instruction en cours */
    /* Prix d'un octet FETCHE dans la region ou l'instruction courante a ete lue, en
     * 1/16 de cycle. 0 = aucun octet compte. La file est la meme pour la cartouche et
     * pour le BIOS -- c'est le meme bus -- mais leur tarif au mot n'a pas a etre le
     * meme, donc le prix suit la REGION plutot qu'une constante unique. */
    mutable int32_t fetch_bc16 = 0;

    uint32_t mul_byte_states = 0;
    uint32_t div_byte_cycles = 0;
    /* Idem pour la forme MOT, que la v14 n'a jamais exercee (ROM v17). */
    uint32_t mul_word_states = 0;
    uint32_t div_word_cycles = 0;
    /* A REPEATING block transfer holds the bus for its whole run -- a read and a write
     * every iteration -- so the instruction queue cannot run ahead during it and the
     * instruction after one pays its own fetch in full. Set, the BIU's run-ahead is
     * zeroed after LDIR/LDIRW/CPIR/... instead of being left at its one-queue credit.
     *
     * ⚖️ ARMED BY THE SILICON MODEL, AND MEASURED: BOMBERMAN's open-loop raster copier
     * must spend exactly 4120 cycles a block. Without this, 4086 (0.9917x) and the
     * title screen loses one line per band; with it, 4134 (1.0034x) and the picture is
     * pixel-clean. The eleven calibration ROMs are bit-identical either way. Full
     * account in ngpc_set_timing_silicon (core.cpp). Default off = the pre-model
     * behaviour, like every other piece of it. */
    bool     block_drains_queue = false;
    /* Set by the block-transfer handler when the loop actually REPEATED, read and
     * cleared by the run loop -- same shape as `cycles_already_measured`. */
    bool     block_transfer_ran = false;

    bool     fetch_pipelined = false;
    int32_t  biu_debt = 0;
    int32_t  biu_slack = 0;      /* set from cart_wait when the model is armed */
    /* EXPERIMENT: the queue's run-ahead is worth 2 x the wait of the region being
     * FETCHED, not always the cart's.
     *
     * The queue is four bytes -- two 16-bit words -- wherever the code lives. So the
     * time the bus can run ahead is 2 x cart_wait in cart code and 2 x bios_wait in
     * BIOS code, and using the cart's figure for both hands BIOS code credit it never
     * had. That is exactly the shape of the last gap: REG (cart arithmetic) reads -4%
     * while QUIET (BIOS COM calls) reads +7%, on the same window with the same VBlank
     * overhead, so the difference is entirely in the loop BODY. */
    bool     biu_slack_follows_region = false;

    /* EXPERIMENT: a DATA read from the BIOS ROM costs like a fetch from it.
     *
     * `bios_wait` is charged on instruction fetch only, so reading a table out of the
     * BIOS is free here. It is not free on silicon: the BIOS is a chip on the same bus,
     * and a read is a read.
     *
     * ⚡ AND THE COM PATH DOES IT ON EVERY CALL. A BIOS service is reached by an
     * indirect `call xix` whose vector is fetched with `ld xix,(xix+w)` from the table
     * at 0xFFFE00 -- four bytes, two words, billed at zero. The probe's QUIET loop makes
     * four such calls per iteration, which is exactly where its 125 missing cycles could
     * hide. (`cart_data_wait` stays 0 for a different and MEASURED reason: cpu_calib_v2
     * showed a cart data read costs the same as a RAM read. That measurement was about
     * the CART; it says nothing about the BIOS chip.) */
    uint32_t bios_data_wait = 0;

    /* ⛔ « CE COUT EST DEJA EN CYCLES, NE LE MET PAS A L'ECHELLE. »
     *
     * Le modele multiplie le cout des instructions par `base_scale` parce que les
     * chiffres de Toshiba sont des ETATS. Mais quelques couts de ce coeur ne viennent
     * PAS d'une table : ils ont ete MESURES en cycles contre du materiel, parce que la
     * doc est fausse ou incomplete pour eux --
     *   MUL / DIV : `cpu_calib_v2` lit DIV mot a 265 la ou la datasheet donne un
     *               plancher ; la vraie division est a latence variable et plus lente ;
     *   LDIR/LDIRW : 14 et 18, cales sur Cool Boarders et le copieur raster de
     *               Bomberman (fenetre d'UN cycle).
     *
     * Les avoir doubles avec le reste facturait ces instructions au DOUBLE, et ce sont
     * exactement celles dont vivent un HUD en split raster et un scroll de fond : le HUD
     * de Cool Boarders et le fond de KOF R-2 glitchaient en jeu. Un gestionnaire pose ce
     * drapeau, la boucle d'execution le lit puis le rearme. */
    bool     cycles_already_measured = false;

    /* Cout d'un fetch par MOT, en QUARTS de cycle. 0 = utiliser `cart_wait` en entier.
     *
     * ⛔ POURQUOI UNE FRACTION EST NECESSAIRE. Contre les boucles du test 6 -- du code
     * cartouche pur, sans le moindre appel COM, donc comparable malgre les ROMs de
     * relevé -- l'optimum tombe a ~9,6 : a 9 nous sommes 7 % TROP RAPIDES, a 10 nous
     * sommes 4 % TROP LENTS. Aucun entier ne s'en approche a mieux que 4 %.
     *
     * ⚡ Et ces 4 % se VOIENT : un HUD en split raster n'a qu'une ligne de marge, alors
     * un CPU legerement lent le rate quand la scene se charge -- le HUD de Cool Boarders
     * clignote « parfois plus, parfois moins selon les circonstances », ce qui est
     * exactement la forme d'une marge trop juste plutot que d'un defaut franc.
     *
     * Le reste est accumule en quarts et converti a la fin de l'instruction, la retenue
     * etant reportee : sur une boucle, le cout moyen est donc bien fractionnaire. */
    uint32_t fetch_wait_q4 = 0;
    /* Cout d'un octet FETCHE, en SEIZIEMES de cycle. 0 = ancien chemin, par MOT.
     *
     * ⚖️ POURQUOI PAR OCTET. Le bus cartouche est 8 BITS (AM8/16 est bonde haut sur
     * cette carte), donc le processeur va chercher UN OCTET par cycle de bus : le cout
     * d'un fetch est proportionnel aux octets, pas aux mots. Facturer par mot -- c'est
     * a dire une seule fois par adresse PAIRE -- fait dependre le prix d'une
     * instruction de sa PARITE : une instruction de 5 octets paye 3 charges si elle
     * commence en pair, 2 si elle commence en impair. 50 % d'ecart pour la meme
     * instruction.
     *
     * ⇒ C'EST CE QUI RENDAIT NOTRE MODELE SENSIBLE A L'ADRESSE la ou le silicium ne
     * l'est pas : ROM v12, la meme boucle a quatre adresses donne 682/682/683/682 sur
     * console et 715/733/732/732 chez nous.
     *
     * Le seizieme d'appoint est tire des quatre bits bas de l'adresse, sans etat --
     * meme raison qu'en `fetch_wait_q4` : une retenue reportee d'une instruction a
     * l'autre desynchronise un rejeu (le test libretro l'avait attrape). Sur seize
     * octets consecutifs, `v & 15` d'entre eux coutent un cycle de plus ; la moyenne
     * est exacte et ne depend d'aucun alignement.
     *
     * Mesure directe : ROM v14 page 1, chaines de `ld XWA,#imm32` -- 4,03 cy/octet sur
     * une droite qui ferme a 0,35 %. 64 seiziemes = 4,00 cy/octet = 8,00 cy/mot, ce que
     * la structure predit (2 cycles de bus x 2 etats x 2 cycles). */
    uint32_t fetch_wait_byte_q16 = 0;
    /* Cout FIXE d'un acces memoire de donnee, en cycles. 0 = gratuit (l'ancien
     * comportement, et une HYPOTHESE, pas une mesure).
     *
     * ⚖️ CE QUE v2 AVAIT PROUVE, ET CE QU'ELLE N'AVAIT PAS PROUVE. `CRND == RRND` dit
     * qu'une lecture de donnee en cartouche coute comme une lecture en RAM : elles sont
     * EGALES ENTRE ELLES, jamais qu'elles sont GRATUITES. Lire « egales » comme « donc
     * nulles » est un pas que v2 n'autorisait pas, et la valeur est restee a 0 pendant
     * un an sans que rien la contraigne.
     *
     * ⚡ MESURE : ROM v15 pages 1 et 2, huit acces par tour a trois largeurs, en lecture
     * puis en ecriture. Le surcout est **~4,05 cycles par ACCES** et il ne bouge pas avec
     * la largeur : +4,10 sur l'octet, +4,06 sur le mot, +4,03 sur le long. La difference
     * entre largeurs qui subsiste (32,5 cy pour huit acces) est deja portee par la table
     * d'instructions -- 4/4/6 etats -- et notre modele la rendait deja juste.
     *
     * ⛔ ET LA FORME COMPTE AUTANT QUE LA VALEUR. Une premiere version facturait par
     * OCTET (`data_wait_q16`), calee sur la seule largeur que la v14 avait mesuree : elle
     * callait cette page-la au cycle pres et ne corrigeait PAS le `MEM` du corpus. La v15
     * l'a refutee en une ligne -- une lecture d'un octet et une de deux octets coutent le
     * MEME prix (215 contre 216 comptes) -- donc le cout suit les ACCES, pas les octets.
     *
     * ✅ Et deux egalites de notre modele sont CONFIRMEES au passage : lecture et
     * ecriture coutent pareil (ecart <= 0,5 compte sur les quatre paires), et la largeur
     * ne joue que par ses etats.
     *
     * Facture dans `load_sized` et `store`, c'est-a-dire par ACCES et non par octet --
     * donc les `push`/`pop` d'un programme le sont aussi, puisqu'ils passent par la.
     * ⛔ SAUF L'ENTREE EN INTERRUPTION, qui serait comptee DEUX FOIS : les quatre valeurs
     * de Toshiba pour l'acceptation (28/24/22/18 etats) sont indexees sur la largeur de
     * bus de la ZONE DE PILE, donc elles contiennent deja le prix des ecritures de PC et
     * SR. Voir le `data_wait_cycles = 0` dans la livraison d'interruption (core.cpp).
     * ⛔ Les transferts bloc ne sont pas charges non plus : non mesures. */
    /* EXPERIMENT : `data_access_cycles` ne se paie que dans du code CARTOUCHE.
     *
     * ⚖️ POURQUOI CETTE FORME PLUTOT QU'UNE AUTRE. Deux mesures silicium se
     * contredisent sous une regle uniforme : la boucle `MEM` du corpus EXIGE ces 4 cy
     * (65,3 cy/tour mesures contre 62 sans, 66 avec), et le chemin d'interruption les
     * REFUSE (111,5 mesures contre 110 sans, 126 avec). Ce qui separe les deux n'est pas
     * la region des DONNEES -- les deux ecrivent en RAM -- c'est la region du CODE : la
     * boucle `MEM` est en cartouche, le stub d'interruption est en BIOS.
     *
     * ⚡ Et c'est un mecanisme, pas un reglage : si ce cout est une CONTENTION -- l'acces
     * de donnee vole un cycle de bus au prefetch -- il ne peut mordre que la ou le fetch
     * est cher, c'est-a-dire sur le bus 8 bits de la cartouche. Le BIOS ne le subit pas.
     * C'est exactement la regle deja appliquee a `branch_taken_extra`, pour la meme
     * raison et depuis la meme famille de mesures. */
    /* EXPERIMENT : une interruption est TRANSPARENTE pour l'etat de bus du flot
     * interrompu -- l'etat de la file (ou la dette) est sauve a la livraison et rendu au
     * `reti`.
     *
     * ⛔ CE QU'IL CORRIGE, ET C'EST MESURE. Sans lui, les cycles de l'ISR RECHARGENT la
     * file du code interrompu : une interruption y rend le code interrompu MOINS CHER
     * (-0,574 cy/instruction pour le credit, -0,224 pour la file), soit ~17 et ~7 cy de
     * ristourne par interruption. C'est impossible -- pendant l'ISR le bus cherche les
     * octets de l'ISR, pas ceux du flot interrompu.
     *
     * ⚡ ET LE SILICIUM LE DIT (ROM v19) : le cout d'une interruption est PLAT selon que
     * la boucle interrompue soit limitee par le bus (`ld XWA`, 5 octets) ou par
     * l'execution (`nop`, 1 octet) -- 112,6 / 112,0 / 110,7 / 110,5, contraste +1,5.
     * Nos deux modeles predisaient +18,0 et +11,6. La ristourne n'existe pas.
     *
     * ⚖️ Transparente, et pas « file vide au retour » : le silicium ne montre NI gain NI
     * surcout au retour. La pile de sauvegarde suit les imbrications. */
    /* EXPERIMENT : un transfert de controle PRIS qui CHANGE DE REGION jette l'avance.
     *
     * ⚖️ POURQUOI PAS `flush_queue_on_branch` TOUT COURT. L'avance de l'unite de bus est
     * du temps pendant lequel elle a lu des octets EN AVANCE. Quand une branche reste
     * dans la meme region, ces octets etaient au bon tarif et une partie survit
     * legitimement (v14 : `branch_flush_keep` ~13-14). Quand elle CHANGE de region, ils
     * ne valent rien : le credit a ete bati en lisant du BIOS a 4 cy/octet et serait
     * depense sur de la cartouche au meme tarif nominal, mais le bus n'a jamais touche
     * ces octets-la -- il ne connaissait meme pas l'adresse.
     *
     * ⚡ MESURE. Le `ret` du stub BIOS finit avec 16 cycles d'avance, et les deux
     * premieres charges du gestionnaire en cartouche les depensent : 10 et 14 cycles au
     * lieu de 20. Une charge dans un ISR coute 18,00 cy chez nous contre 20,29 mesures
     * sur console (v18 page 1). Vider a CHAQUE branche corrige ca mais casse le corpus
     * (0,31 % -> 2,85 %) : les boucles cartouche, elles, gardent leur credit. */
    bool     flush_on_region_change = false;

    bool     irq_transparent_queue = false;
    int32_t  irq_q_save[8] = {0};
    int32_t  irq_debt_save[8] = {0};
    uint8_t  irq_save_depth = 0;

    bool     data_wait_cart_only = false;
    uint32_t data_access_cycles = 0;
    /* Accumulateur de l'instruction en cours, en cycles entiers.
     *
     * ⛔ IL EST SEPARE DE `access_wait`, ET C'EST LE POINT. `access_wait` est du temps
     * de BUS que la file de prefetch peut recouvrir : le processeur ne cale que si la
     * file s'est videe. Un acces OPERANDE ne se recouvre pas -- il occupe le bus, donc
     * il retarde a la fois l'execution et le prefetch. Verse dans `access_wait`, le
     * cout d'une lecture etait tout simplement AVALE par le credit d'avance : passer
     * de 0 a 1 cycle par octet ne deplacait la page 2 de la v14 que de 12,05 a 12,21
     * au lieu des ~16 attendus. */
    mutable uint32_t data_wait_cycles = 0;
    mutable uint32_t access_wait_q4 = 0;   /* accumulateur de l instruction en cours */
    uint32_t fetch_wait_carry = 0; /* retenue reportee d'une instruction a l'autre */
    /* EXPERIMENT: drop the receiver's second charge of the byte time. Known to be a
     * real defect; known to make things WORSE on its own. Its counterpart was always
     * meant to be a CPU-timing correction, so it needs a knob to be tested WITH one. */
    bool rx_single_charge = false;
    /* EXPERIMENT: the relay refuses to hand a byte over while the RECEIVER's RTS is
     * high, and the byte then costs a full byte time once it is released.
     *
     * On silicon the receiver's RTS drives the sender's CTS, so a held byte is never
     * PUT ON THE WIRE -- when RTS drops the sender still has to shift it out. This
     * core pushes unconditionally and lets serial_tick present the byte later, which
     * skips that byte time. MEASURED: a round trip costs 3.74-4.02 byte times on
     * silicon against 2.67 here, and rts_hold runs to ~18000 ticks a run, so the gate
     * is engaged constantly.
     *
     * ⛔ REFUTED, 2026-08-20: throughput falls 3730 -> 2750 (1.10 -> 1.49 byte times
     * per byte, silicon 1.03) and the round trip does not move at all. Whatever the
     * missing cost is, it is not the byte waiting for the gate to open. */
    bool relay_gates_on_rts = false;
    /* EXPERIMENT: HALF DUPLEX -- a byte is not presented to the CPU while this
     * console's own transmitter is busy.
     *
     * Why this shape and not a flat per-byte surcharge: silicon gains NOTHING from
     * driving both directions at once (4.02 byte times per round trip against 3.74
     * with a single token), while a one-way stream is wire-saturated (1.03). Only a
     * model that costs something when BOTH directions are active, and nothing
     * otherwise, can produce all three at once.
     *
     * ⛔ REFUTED, 2026-08-20, and twice over. (1) Throughput collapses to 153 bytes a
     * window, 26.8 byte times each, where silicon does 3963. (2) The reference that
     * suggested it does not exist: test 1 is "SPEED BOTH WAYS" and silicon runs it at
     * 3963 against 3818 one-way, so THE LINK IS FULL DUPLEX ON SILICON. And the
     * "silicon gains nothing from two tokens" figure came from both consoles left on
     * role A -- a mode where neither echoes, so each mistakes the peer's ping for its
     * own reply. The ROM says so itself: role B's non-zero TRIPS is the WITNESS OF A
     * MISCONFIGURED RUN, not a measurement. Kept at false with the refutation. */
    bool rx_blocked_by_tx = false;
    /* Wait-states per byte of a DATA read off the cart. SILICON SAYS ZERO: cpu_calib_v2
     * measured a random cart read (CRND=252) and a RAM read (RRND=252) as identical, so
     * only instruction FETCH is wait-stated. An earlier `cart_data_wait=5` was a curve-fit
     * to Cool Boarders' frame rate and that ROM REFUTED it -- do not bring it back without
     * a measurement. There is deliberately NO fallback to cart_wait when this is 0: here 0
     * is the answer, not "unset". (Flash page-mode was the theory behind a fetch-vs-data
     * asymmetry; the numbers did not support it.) */
    uint32_t cart_data_wait = 0;
    /* Wait-states per byte written to display RAM (0x8000-0xBFFF): the K2GE "adjustment
     * circuitry" throttling CPU access to VRAM during the active drawing period.
     * THE EFFECT IS REAL AND SILICON-MEASURED -- cpu_calib_v3 came back VWR 452 < MEM 471,
     * a VRAM write costing more than a RAM write. What is NOT settled is the cost per byte,
     * so nothing in the shell sets this and it stays 0 (off) rather than shipping a guessed
     * integer. It is also NOT the explanation for Cool Boarders: that game writes VRAM
     * during vblank, and its residual turned out to be LDIR (see ldir_cost). If you pin the
     * cost, update hw_calibration/README.md, the shell and the README table together. */
    /* ⛔ ET IL NE SE PAIE PAS PENDANT UN TRANSFERT BLOC. `ldir_cost`/`ldirw_cost` ne
     * sortent pas d'une table : ils ont ete MESURES contre du materiel -- 18 contre le
     * copieur HiColor de Bomberman, qui ecrit justement en VRAM, avec une fenetre d'UN
     * cycle. Ce cout contient donc DEJA l'etranglement du K2GE ; l'ajouter par-dessus le
     * facture deux fois. C'est la meme garde que `data_wait_cycles` (voir
     * `data_wait_before` dans mem_family.cpp), et c'est le test
     * `test_bomberman_hicolor_phase` qui l'a exigee : arme sans elle, le copieur derive
     * de sa tranche de 4120 cycles et corrompt une ligne par bande. */
    mutable bool in_block_copy = false;
    /* ESSAI : un transfert bloc paie l'etranglement VRAM comme n'importe quelle
     * ecriture. Hypothese : `ldirw_cost = 18` etait la SOMME de l'instruction (14, ce
     * que la doc ET la v20 disent) et du throttle, mal attribuee a l'instruction --
     * la v20 mesure `ldirw` en RAM->RAM, ou il n'y a pas de throttle. */
    bool     block_pays_vram = false;
    /* ⚡ SURCOUT D'UN TRANSFERT BLOC DONT LA **SOURCE** EST EN CARTOUCHE, PAR OCTET LU.
     *
     * ROM v21, tir silicium 2026-08-30. Le meme `ldirw`, quatre chemins :
     *      RAM -> RAM   **14,04** cy/iteration   (annexe B (3), 7n+1 etats : 14,00)
     *      RAM -> VRAM  14,12   ⇒ la DESTINATION ne coute RIEN (+0,08)
     *      ROM -> RAM   18,16   ⇒ la SOURCE coute **+4,12**
     *      ROM -> VRAM  18,16   ⇒ et les deux effets s'ADDITIONNENT (+4,20 predit)
     *
     * ⇒ Le bus cartouche est 8 BITS : lire un MOT y coute deux acces d'octet. D'ou
     * **2 cycles par octet lu**, soit +4 sur une iteration mot.
     *
     * ⚖️ ET C'EST CE QUI EXPLIQUE `ldirw_cost = 18`. Ce nombre n'etait pas faux, il etait
     * **mal attribue** : cale sur le copieur HiColor de Bomberman, qui copie ROM -> VRAM,
     * il portait 14 (l'instruction) + 4 (sa source en cartouche) -- et nous l'appliquions
     * a TOUS les transferts, donc 29 % trop cher sur toute copie RAM -> RAM ou
     * RAM -> VRAM, c'est-a-dire la plupart de celles que font les jeux.
     *
     * ⛔ FORME OCTET : 2 cy/octet est DERIVE de la mesure du mot (4 / 2 octets), pas
     * mesure directement. Il faudrait une rotation `ldirb` a source cartouche pour le
     * confirmer. Le noter avant de s'appuyer dessus. */
    uint32_t block_cart_src_per_byte = 0;
    uint32_t vram_wait = 0;

    /* WHICH CONSOLE WE ARE PRETENDING TO BE, for a monochrome cartridge.
     * false (default) = NGPC: 0x6F91 reads 0x10, so a colour-aware mono game runs its
     * colourisation code and owns the compat palette. true = the original mono NGP:
     * 0x6F91 reads the cartridge's own header value and the 12-bit compat palette does
     * not exist on that silicon, so writes to it are ignored and the BIOS grey ramp
     * stands. Set BEFORE reset. */
    bool k1ge_console = false;
    /* WHICH LANGUAGE THE CONSOLE IS SET TO -- `Language` at 0x6F87 (see the constants).
     * A setting, exactly like the clock: on a console the setup wizard writes it and
     * the coin cell keeps it. Handed to the cart at the hand-off. Set BEFORE reset. */
    uint8_t language_code = kLanguageEnglish;
    /* Cycles per byte for LDIR/LDDR block copies. Datasheet 7n+1, so the field defaults to
     * 7 -- but the datasheet MUL/DIV figures already turned out to be FLOORS. 14 reproduces
     * Cool Boarders' silicon 30fps without touching Fatal Fury (one instruction-cost fix
     * explaining both games), so the shell ships 14 via cfg.CART_LDIR_COST while the raw
     * field keeps the datasheet number. Strongly evidenced, still pending a clean silicon
     * measurement (hw_calibration a_cpu_calib_v6.ngc, LDRR/LDVR). `ngpc_set_ldir_cost`. */
    uint16_t ldir_cost = 7;
    /* Cycles per ITERATION for the WORD forms, LDIRW/LDDRW. 0 = follow ldir_cost.
     *
     * ⚖️ THE TWO WIDTHS ARE NOT THE SAME INSTRUCTION AND THE LOOP IS PAID PER ITERATION,
     * NOT PER BYTE. `ldir_cost` is documented "per byte" and it is -- for the BYTE form,
     * where one iteration moves one byte. LDIRW moves TWO bytes per iteration, so billing
     * it the same number charged a word transfer at half price per byte. Cool Boarders,
     * which pinned 14, uses the BYTE form; nothing in that measurement ever constrained
     * the word form, and one field could not hold both answers anyway.
     *
     * ⚖️ MEASURED, on Thor's BOMBERMAN (2004) HiColor title screen. Its `hc_showHW` is an
     * OPEN-LOOP raster copier: 19 blocks of 224 `ldirw` words, no polling, each of which
     * must cost exactly one 8-scanline slice (8 * 515 = 4120 cycles) or the picture shears.
     * At 14 a block came to 3268 cycles -- 0.793x -- and the screen was garbage. 18 puts it
     * at 8328 per pair against a target of 8240 and the frame comes out PIXEL-IDENTICAL to
     * the same ROM's self-synchronising path (`hc_showEmu`, which polls RAS.V and is right
     * whatever the costs are). 17 -> 83% of pixels, 19 -> 4%: the window is one cycle wide,
     * which is what makes this ROM a better instrument than any frame-rate average.
     *
     * ⛔ The other way to close the same gap -- `cart_data_wait=2`, on the theory that the
     * copy's source is slow cart flash -- is REFUTED by cpu_calib_v2 on silicon (CRND ==
     * RRND). It was re-run here and it drops CRND to 252 under RRND 255. Do not revive it.
     *
     * a_cpu_calib_v6.ngc measures the BYTE form only; a word-form ROM would settle this
     * one the same way. `ngpc_set_ldirw_cost`. */
    uint16_t ldirw_cost = 0;
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
    /* ⏱️ AND IT TAKES TIME. A chip that is programming or erasing STOPS BEING MEMORY:
     * every read of its window answers a status byte instead of contents, and it does so
     * for as long as the operation runs. That window is the reason a flash stub is
     * copied into RAM and runs with interrupts masked -- an interrupt taken inside it
     * fetches its handler out of status bits.
     *
     * Committing the byte inside the bus cycle that carries the command, as this model
     * did until 2026-09-10, deletes that window entirely: the driver's status poll exits
     * on its FIRST turn, and nothing that depends on the elapsed time can happen. The
     * measurement is hw_test_flash_timing (04_MY_PROJECTS), which reports the poll turns
     * the shipped AMD stubs actually spend: ZERO here, against 108 116 turns per second
     * of the same loop on the same machine.
     *
     *   FlashBusy    the operation is running. Reads answer status: DQ7 = the COMPLEMENT
     *                of the bit 7 being written (0 while erasing), DQ6 toggles on every
     *                read, DQ5 = 0. Nothing matches what the driver asked for, so its
     *                poll loop spins -- which is the point.
     *   FlashFailed  the chip gave up: DQ5 = 1, and it stays that way until a reset
     *                command (0xF0). This is what a NOR cell asked to go back UP does --
     *                programming a 1 over a 0 without an erase. Silicon says so itself
     *                after its internal timeout; a synchronous model says nothing at all
     *                and the driver burns its whole timeout rope instead. */
    enum FlashMode : uint8_t { FlashRead = 0, FlashReadId = 1, FlashWrite = 2, FlashAck = 3,
                               FlashBusy = 4, FlashFailed = 5 };

    struct FlashBlock { uint32_t offset; uint32_t length; bool writable; };

    uint8_t  flash_mode[2] = {FlashRead, FlashRead};
    uint8_t  flash_step[2] = {0, 0};      /* how far into the AA/55/xx sequence */
    bool     flash_dirty[2] = {false, false};
    std::vector<FlashBlock> flash_blocks[2];

    /* The busy window, in the machine clock the rest of the timing model uses. */
    uint64_t        flash_busy_until[2] = {0, 0};
    uint8_t         flash_busy_dq7[2]   = {0, 0};   /* already complemented */
    uint8_t         flash_after_busy[2] = {FlashRead, FlashRead};
    mutable uint8_t flash_dq6[2]        = {0, 0};   /* toggles on every status read */

    /* ✅ MEASURED ON THE CARTRIDGE -- hw_test_flash_timing, 16 Mbit cart, two full runs.
     *
     * The ROM counts the turns the shipped AMD poll loop actually spends, and converts
     * them with a rate measured by the SAME loop, at the SAME RAM address, INTERRUPTS OFF
     * and WHILE THE CHIP IS WORKING -- the only state in which an erase's turns are
     * counted. Timed against RAS.V, which does not care about the interrupt mask.
     *
     *      8 KB block erase   5 856 turns (mean of 4)  ->  353 389 cycles   57.5 ms
     *      one byte programmed        3.363 turns      ->      203 cycles   33.0 us
     *      64 KB block erase       44 910 turns        ->  2 710 000 cycles  441 ms
     *
     * ⚠️ THE ERASE IS NOT A CONSTANT. The four 8 KB measurements were 5 392, 5 672, 5 993
     * and 6 365 turns -- 53.0 to 62.5 ms, an 18 % SPREAD, twice within a single run. The
     * number above is their mean and the spread is the honest error bar. A model that
     * prices it to the cycle is pricing noise.
     *
     * ⛔ AND BOTH FIGURES OUR DOCUMENTATION CARRIED WERE WRONG. NGPC_FLASH_SAVE_GUIDE.md
     * gave an 8 KB erase as ~5-15 ms -- and made that the REASON block 33 was chosen;
     * OPEN_ITEMS.md gave ~1 s. It is 57.5 ms. Block 33 is still the right choice and its
     * margin under a ~100 ms watchdog is LESS THAN TWO, not the order of magnitude the
     * table implied.
     *
     * ✅ ERASE SCALING BY SIZE IS NOW MEASURED, and linear is close: a 64 KB block costs
     * 7.67 times an 8 KB one, against the 8.00 the code assumes. Kept linear, 4 % high. */
    static constexpr uint64_t kFlashProgramCycles    = 203;      /* 33.0 us per byte */
    static constexpr uint64_t kFlashErasePer8KCycles = 353389;   /* 57.5 ms per 8 KB */

    /* ⛔ AND A PROGRAM THAT CANNOT SUCCEED NEVER SAYS SO. This cartridge was asked to put
     * 0xFF back over a 0x5A -- a NOR cell pulled back up, which no erase-less write can do
     * -- and it did NOT raise DQ5 in 262 143 turns of the poll: 2.45 SECONDS. The stub
     * escaped on its own iteration ceiling, not on anything the chip said.
     *
     * That is the opposite of what an AMD datasheet describes, and it is the measurement
     * that matters most here, because it is the mechanism: two and a half seconds spent
     * INTERRUPTS MASKED, no V-blank, no watchdog refresh from the game's handler, a
     * micro-DMA still reading a cartridge that has stopped being memory. A save that
     * "works" in an emulator and takes the console down.
     *
     * So the default is kFlashNever: the chip stays busy, DQ5 stays clear, and only a
     * reset command gets it back. The DQ5 path below stays reachable for a part that does
     * behave the way the datasheets say -- but not this one, and not by default. */
    static constexpr uint64_t kFlashNever = ~uint64_t(0);

    /* The dial. 0 = the old synchronous chip, kept so a corpus run can be bisected
     * against the model that shipped before this one. */
    uint64_t flash_program_cycles     = kFlashProgramCycles;
    uint64_t flash_erase_per8k_cycles = kFlashErasePer8KCycles;
    uint64_t flash_fail_cycles        = kFlashNever;

    void flash_begin_busy(int chip, uint64_t cycles, uint8_t dq7, uint8_t after);

    void flash_build_blocks(int chip, uint32_t size);
    void flash_adopt_capacity_from_save(int chip, uint32_t offset);
    void flash_adopt_capacity_from_block(int chip, uint32_t block);
    void flash_present_as(int chip, uint32_t capacity);
    uint32_t flash_presented_capacity(int chip) const;
    uint32_t flash_image_size(int chip) const;
    void     flash_measure_image();           /* once, when the cartridge goes in */
    uint32_t flash_image_bytes[2] = {0, 0};   /* data on each die, erased tail excluded */
    /* What a game asked the BIOS for, read at the `swi 1` -- BEFORE the BIOS turns it
     * into an address. That is the only moment the CARD NUMBER is still visible. */
    void bios_flash_syscall_hint();
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
         * write happened to leave in the I/O page. 0x98-0x9A are the alarm's
         * day/hour/minute -- part of the same chip, so they answer from it too. */
        if (a >= 0x90 && a <= 0x9A) return rtc_read(a);
        /* SC0BUF (0x50) READS THE RECEIVE BUFFER. ALWAYS.
         *
         * ⚡ The address is shared by two SEPARATE registers: a write loads the
         * TRANSMIT buffer, a read returns the RECEIVE buffer. The CPU cannot read
         * back what it transmitted. The pending flag is the "new data" indicator
         * (and our overrun guard): the FIRST read consumes it, but the buffer keeps
         * holding its byte until the next one is shifted in.
         *
         * ⛔ THE BUG THIS ENDS, and it took a two-console link to see it. This used
         * to fall through to `mem[0x50]` once the flag was clear -- and `mem[0x50]`
         * is where a TRANSMITTED byte was left. So a receive handler that touches
         * SC0BUF more than once for one byte (the retail BIOS's COM ISR, running
         * from RAM at 0x6D65, does) put THE LAST BYTE WE SENT into its ring instead
         * of the byte that arrived. MEASURED on Card Fighters' Clash, two consoles,
         * player 1 -> player 2: 532 bytes queued, 532 read, 532 appended to the BIOS
         * ring -- nothing lost, nothing duplicated, and byte 508 arrived as 0xA5
         * where 0x00 was sent. One byte, one wrong value: the packet's checksum then
         * failed (`cp H,A` at 0x24260B -> 0x242741), player 2 dropped the packet in
         * silence and never answered, and both consoles waited for each other for
         * ever on CHOOSE FIRST PLAYER. It was phase-dependent, which is why the same
         * game linked on one attempt and hung on the next.
         *
         * Fixed in the desktop tree 2026-08-02; carried into the libretro tree
         * 2026-08-03 when the two were reconciled. Condemning test:
         * `test_sc0buf_reads_the_receive_buffer_not_the_byte_we_transmitted`. */
        if (a == 0x000050 && serial_link_enabled) {
            if (serial_rx_pending) {
                serial_rx_pending = false;
                ++serial_rx_read_count;    /* debugger: the CPU really consumed it */
            }
            return serial_rx_byte;
        }
        /* Port 0xB1: bit1 = the CR2032 SUB-BATTERY, bit2 = a must-be-1 line (drop it and
         * SNK Gals' Fighter reports a link error). Leaving them 0 is the whole
         * "SUB BATTERY DEAD" loop -- the BIOS reads a dead coin cell and never leaves
         * the warning. Both read 1.
         *
         * bit0 is the POWER line read as a LEVEL, and it must stay 0 here. MEASURED:
         * with bit0 forced to 1 the BIOS boot parks blank at 0xFF1127 and never draws
         * its language/clock screens, whereas at 0 (the I/O page's own value) the boot
         * renders them. The polarity is model-dependent -- this core models POWER as
         * INT0 rather than as an NMI, and a core that chose the NMI would want the
         * opposite level. Trust the render, not a polarity carried over from a
         * different model. So force only bits 1 and 2. */
        if (a == 0x0000B1) {
            /* bit1 = the CR2032 sub-battery (always 1). bit2 = the link-cable DETECT
             * line: it reads 1 when nothing is plugged (idle) and 0 when a peer console
             * is connected through the cable. Card Fighters' Clash gates its handshake
             * transmission on bit2 == 0 (at 0x24065A: ld A,(0xB1); and 0x04; srl 2; the
             * coroutine does `cp A,1; ret Z`), so it can only ever become the link
             * initiator once it detects a cable -- with bit2 stuck at 1 it waits forever
             * ("EITHER PLAYER MUST PUSH A" never advances). Model bit2 from
             * serial_link_enabled, which the link layer arms exactly when a cable is
             * wired. With no cable bit2 stays 1 (SNK Gals' Fighter needs that: bit2 = 0
             * with no peer makes it report a link error). See project memory
             * project_ngpc_emulator_cfc_link_stall. */
            uint8_t v = uint8_t(mem[a] | 0x02);
            /* ⚡ MEASURED ON SILICON 2026-08-19, six physical states, two consoles.
             * bit2 does NOT mean "a cable is plugged in". It follows the PEER'S RTS
             * line -- the same signal that drives our CTS:
             *
             *   no cable / cable with the far end loose / peer SWITCHED OFF /
             *   peer powered but sitting in the BIOS ........... bit2 = 1 (no peer)
             *   peer running a cartridge that opened its port .. bit2 = 0 (peer here)
             *
             * A powered console at the BIOS with the cable in reads as NO PEER. The
             * old model derived this from serial_link_enabled, i.e. from the host
             * attaching a cable, so a game saw "peer present" against a console that
             * had not even called COMINIT.
             *
             * Two behaviours fall out of the correct signal for free: a peer that is
             * merely powered no longer registers, and UNPLUGGING mid-session is now
             * visible to the game -- which is how Match of the Millennium produces
             * its own LINK ERROR on hardware. Our peer-loss handling was entirely
             * host-side because the game could never see the cut.
             *
             * serial_cts_high IS the peer's RTS, crossed by the bridge. bit2 is an
             * INPUT, so it is forced, never OR'd from mem: a savestate can leave a
             * stale value in the I/O page. */
            if (serial_link_enabled && serial_cts_seen && !serial_cts_high)
                 v &= uint8_t(~0x04);
            else v |= 0x04;
            return v;
        }
        /* Port 0xB3 bit 2: a read-only INPUT, and it reads 1 on silicon whatever the
         * cable is doing -- 0x04 on one console and 0x07 on another, with and
         * without a peer (measured 2026-08-19). Bits 0-1 differ BETWEEN CONSOLES, so
         * they are left alone rather than invented; only the bit both machines agree
         * on is forced. The core used to return the byte last written, i.e. 0x00.
         *
         * ⚠️ 0xB3 is NOT the cable detect, and that question is now closed: it does
         * not move when a peer appears. 0xB1 bit 2 is the detect line. */
        if (a == 0x0000B3) return uint8_t(mem[a] | 0x04);

        /* Serial control SC0CR (0x51): bit 7 reads 1 on silicon once ANY traffic has
         * crossed, and stays set -- measured 0x00 with a console that had never sent
         * or received a byte, 0x80 in every test that moved one, still 0x80 after the
         * cable was yanked. A latch, not a constant. */
        if (a == 0x000051) {
            return uint8_t(mem[a] | (serial_wire_count || serial_rx_read_count ? 0x80 : 0x00));
        }

        /* Baud rate control BR0CR (0x53): bit 6 reads back 1 whatever is written --
         * 0x05 reads 0x45, 0x15 reads 0x55 (measured). That is BR0ADDE, the
         * fractional-divider enable, and serial_byte_cycles() deliberately ignores
         * it: the measured byte time matches the plain divider to 1% and the /4 step
         * of test 4 holds exactly, so honouring the bit would change nothing but the
         * arithmetic's honesty. Modelled at the READ, where it was seen. */
        if (a == 0x000053) return uint8_t(mem[a] | 0x40);

        /* Slow cart flash: every byte the CPU reads from a cartridge window costs
         * wait-states. Sequential instruction fetch (inside the fetch window) is cheap;
         * a random data read pays cart_data_wait. Accumulated here and folded into the
         * instruction's cycles by the run loop. Guarded so the default path is unchanged. */
        if (bios_wait && a >= 0xFF0000) {
            /* The BIOS sits on the SAME 16-bit external bus as the cart, so it obeys
             * the same rule: one wait per WORD when that model is armed, not per byte.
             * Charging the cart and letting the BIOS fetch for free is an asymmetry
             * nothing justifies. */
            const bool is_fetch = (a - fetch_window) < 8u;
            if (is_fetch && queue_bytes && fetch_wait_byte_q16) {
                /* LE BIOS EST SUR LE MEME BUS, DONC DANS LA MEME FILE. Le modele en
                 * octets ne recouvrait que la CARTOUCHE : le fetch BIOS s'ajoutait brut
                 * a `access_wait`, sans le moindre recouvrement avec l'execution, alors
                 * que le credit en cycles l'absorbait. Le chemin d'une interruption est
                 * presque entierement en BIOS (aiguillage `push`/`push`/`ret`), d'ou une
                 * ordonnee de 156,1 cy contre 111,1 mesures sur console -- et l'exces
                 * SUIVAIT `bios_wait` (156,1 / 141,4 / 124,1 a 8 / 4 / 0), ce qui le
                 * nomme au lieu de le deviner.
                 *
                 * ET IL N'INTRODUIT AUCUN PARAMETRE : le prix reste celui de la region
                 * -- `bios_wait` par MOT, soit `bios_wait / 2` par octet, ce que porte
                 * `fetch_bc16`. A 8 par mot c'est 4,00 cy/octet, exactement le tarif
                 * cartouche mesure par la v14 ; la moyenne ne bouge donc pas. Ce qui
                 * change, c'est que ce temps se RECOUVRE avec l'execution et ne depend
                 * plus de la parite de l'adresse. */
                ++fetch_bytes;
                fetch_bc16 = int32_t(bios_wait) * 8;   /* cy/octet, en 1/16 */
            }
            else if (is_fetch && (!fetch_wait_per_word || !(a & 1u))) { access_wait += bios_wait; ++dbg_bios_charges; }
            else if (!is_fetch && bios_data_wait
                     && (!fetch_wait_per_word || !(a & 1u))) access_wait += bios_data_wait;
        }
        if (cart_wait &&
            ((a >= 0x200000 && a <= 0x3FFFFF) || (a >= 0x800000 && a <= 0x9FFFFF))) {
            const bool is_fetch = (a - fetch_window) < 8u;   // unsigned wrap => outside == huge
            if (is_fetch && fetch_wait_per_word) {
                /* Un wait par acces 16 bits. En quarts de cycle si une valeur
                 * fractionnaire est posee -- l'entier ne descend pas sous 4 % d'erreur
                 * sur les boucles cartouche, et ces 4 % font clignoter un split raster. */
                if (fetch_wait_byte_q16 && queue_bytes) {
                    /* Modele FILE : on COMPTE l'octet, on ne le facture pas ici -- son
                     * prix depend de ce que la file contenait deja, et ca se decide a
                     * la fin de l'instruction. Voir `queue_bytes`. */
                    ++fetch_bytes;
                    fetch_bc16 = int32_t(fetch_wait_byte_q16);
                } else if (fetch_wait_byte_q16) {
                    /* Par OCTET : voir `fetch_wait_byte_q16`. Aucune condition sur la
                     * parite, c'est tout l'interet. */
                    access_wait += (fetch_wait_byte_q16 >> 4)
                                 + (((a & 15u) < (fetch_wait_byte_q16 & 15u)) ? 1u : 0u);
                } else if (!(a & 1u)) {
                    /* ⚡ LE QUART DE CYCLE SANS AUCUN ETAT. Le silicium demande 8,25
                     * cycles par mot (ROM a_irq_calib_v8) : on l'obtenait en REPORTANT
                     * une retenue d'une instruction a l'autre. C'etait de l'etat, et
                     * tout chemin qui lit hors du pas d'instruction (l'amorce BIOS, le
                     * flash) le decalait -- le test de rejeu libretro est tombe dessus
                     * immediatement : « non-deterministic state after replay ».
                     *
                     * Le motif est desormais tire de l'ADRESSE : sur quatre mots
                     * consecutifs, `q4 & 3` coutent un cycle de plus. Meme moyenne,
                     * reproductible depuis l'adresse seule, aucune retenue a sauver.
                     * Ni l'un ni l'autre ne modelise un mecanisme sous-cycle reel : ce
                     * sont deux facons d'arrondir la meme moyenne, et celle-ci ne peut
                     * pas desynchroniser un rejeu. */
                    if (fetch_wait_q4)
                        access_wait += (fetch_wait_q4 >> 2)
                                     + ((((a >> 1) & 3u) < (fetch_wait_q4 & 3u)) ? 1u : 0u);
                    else
                        access_wait += cart_wait;
                }
                if (a >= rlog_lo && a <= rlog_hi) note_read(a, mem[a]);
                return mem[a];
            }
            /* Silicon (cpu_calib_v2: CRND == RRND) says a cart DATA read costs the same
             * as RAM -- only the instruction FETCH is wait-stated. So data reads get
             * cart_data_wait; no fallback to cart_wait.
             *
             * ⚠️ AND ITS DEFAULT OF 0 IS AN ASSUMPTION, NOT THAT MEASUREMENT. v2 proved
             * cart-data and RAM are EQUAL TO EACH OTHER; it never said what they equal.
             * Reading "equal" as "therefore free" is a step v2 does not license, and the
             * value has sat at 0 ever since with nothing constraining it. First evidence
             * that it is NOT 0, from outside: Emulator_vs_Hardware_20260807 "Case B" --
             * ONE extra work-RAM read per sprite (40-60 sprites/frame) costs the device
             * ~9%, and this core charges exactly 0 for it. That is a RAM read, so the
             * same unmeasured quantity, in the other region v2 tied it to.
             *
             * Do NOT "fix" it by guessing a number here: a guessed cart_data_wait = 5 was
             * already shipped once and refuted by v2 itself. The measurement that would
             * settle it is specified in hw_calibration/README.md as v8 (N accesses vs
             * N+1, same region, active display vs vblank). */
            access_wait += is_fetch ? cart_wait : cart_data_wait;
        }
        if (a >= rlog_lo && a <= rlog_hi) note_read(a, mem[a]);   // disarmed by default
        if (hygiene_on) check_uninit_read(a);
        return mem[a];
    }
    /* Un acces de donnee, quelle que soit sa largeur. Les SFR (page 0x00-0xFF) et la
     * VRAM sont exclus : la premiere n'est pas de la memoire externe, la seconde a son
     * propre etranglement (`vram_wait`) et n'a pas ete mesuree ici. */
    inline void charge_data_access(uint32_t a) const {
        if (!data_access_cycles) return;
        a &= kAddrMask;
        const bool ram  = (a >= 0x004000u && a < 0x008000u);
        const bool cart = (a >= 0x200000u && a <= 0x3FFFFFu) ||
                          (a >= 0x800000u && a <= 0x9FFFFFu);
        if (ram || cart) data_wait_cycles += data_access_cycles;
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
        if (a >= elog_lo && a <= elog_hi) note_event(kEventWrite, a, v, pc);
        if (hygiene_on) mark_ram_written(a);
    }

    /* THE EVENT LOG -- WHEN in the frame did that happen?
     *
     * A write log says a register changed and who changed it. It cannot say the one
     * thing that matters for raster work: at which SCANLINE and how far into it. A
     * mid-frame scroll split, an HBlank HUD, a palette swap on line 100 -- all of
     * them are correct or broken purely as a function of raster timing, and until
     * now the only way to check was to guess.
     *
     * Every event carries its exact raster position, so the debugger can plot the
     * frame as a scanline x cycle grid: one pixel per cycle, per line.
     *
     * Armed over an address window (the video registers, typically). Off by default. */
    static constexpr uint8_t kEventWrite = 0;
    static constexpr uint8_t kEventIrq   = 1;

    struct EventRec {
        uint32_t pc;
        uint32_t addr;
        uint16_t scanline;
        uint16_t cycle;      /* cycles elapsed INTO that scanline */
        uint8_t  value;
        uint8_t  type;       /* kEventWrite / kEventIrq */
    };
    static constexpr uint32_t kElogSize = 4096;
    uint32_t elog_lo = 1;        /* lo > hi  ==  logging off */
    uint32_t elog_hi = 0;
    uint64_t elog_count = 0;
    EventRec elog[kElogSize] = {};

    /* THE HYGIENE COUNTERS -- what a ROM does that hardware tolerates but that is
     * almost always a bug.
     *
     * This core models the machine closely enough to JUDGE a cartridge, not merely
     * run it, and these are the two findings that need the core's cooperation:
     *
     *  - READ BEFORE WRITE. Work RAM comes up as whatever the last game left (or
     *    garbage on a cold machine). A game that reads a variable it never wrote is
     *    reading noise; it will look fine on the developer's emulator, whose RAM
     *    happens to be zeroed, and misbehave on a console that has been playing
     *    something else. The equivalent check finds real bugs on real games.
     *    Tracked with one bit per work-RAM byte -- 0x4000..0x7FFF, 2 KB of bitmap.
     *
     *  - WRITES THAT GO NOWHERE. A store to unmapped space is discarded by the bus
     *    and the program never learns. Writes to CART space are NOT counted: that is
     *    how an AMD flash command latch is addressed, so they are legitimate.
     *
     * Both off by default; the analyzer arms them for one boot. */
    /* ⚠️ USER RAM ONLY -- 0x4000..0x6BFF, not the whole RAM region.
     *
     * 0x6C00..0x6FFF is the BIOS SYSTEM PAGE and 0x7000..0x7FFF is the Z80's shared
     * RAM. Neither belongs to the game: the BIOS fills the system page during its own
     * boot, and in the hand-off start the analyzer uses that boot never runs. Watching
     * those ranges reported ~2000 "uninitialised reads" for every ROM, commercial ones
     * included -- a finding that fires on everything teaches you to ignore it. The
     * bound matches core/bus.py, which calls 0x4000..0x6BFF the user RAM area. */
    static constexpr uint32_t kRamLo = 0x004000, kRamHi = 0x006BFF;
    static constexpr uint32_t kRamSpan = kRamHi - kRamLo + 1;

    struct HygieneRec { uint32_t pc; uint32_t addr; };
    static constexpr uint32_t kHygSize = 256;

    bool hygiene_on = false;
    mutable uint64_t uninit_reads = 0;      /* reads of work RAM never written */
    mutable uint64_t lost_writes = 0;       /* stores to unmapped space */
    mutable uint32_t hyg_uninit_n = 0;      /* how many samples below are filled */
    mutable uint32_t hyg_lost_n = 0;
    mutable HygieneRec hyg_uninit[kHygSize] = {};
    mutable HygieneRec hyg_lost[kHygSize] = {};
    mutable uint8_t ram_written[kRamSpan / 8] = {};

    inline void hygiene_reset() {
        uninit_reads = lost_writes = 0;
        hyg_uninit_n = hyg_lost_n = 0;
        for (uint32_t i = 0; i < kRamSpan / 8; ++i) ram_written[i] = 0;
    }

    inline void mark_ram_written(uint32_t a) const {
        if (a < kRamLo || a > kRamHi) return;
        const uint32_t i = a - kRamLo;
        ram_written[i >> 3] |= uint8_t(1u << (i & 7));
    }

    inline void check_uninit_read(uint32_t a) const {
        if (a < kRamLo || a > kRamHi) return;
        if ((a - fetch_window) < 8u) return;          /* a fetch, not a data read */
        const uint32_t i = a - kRamLo;
        if (ram_written[i >> 3] & (1u << (i & 7))) return;
        ++uninit_reads;
        if (hyg_uninit_n < kHygSize) hyg_uninit[hyg_uninit_n++] = {cpu.pc, a};
    }

    inline void note_lost_write(uint32_t a) const {
        ++lost_writes;
        if (hyg_lost_n < kHygSize) hyg_lost[hyg_lost_n++] = {cpu.pc, a};
    }

    /* EXECUTION COVERAGE -- how much of the cartridge actually ran?
     *
     * One bit per byte of the cart window, set at the address of every instruction
     * retired. Without it, "the analyzer looked at this ROM" is an unfalsifiable
     * claim: a boot that sits on the title screen touches a sliver of the code and
     * reports just as confidently as one that played a level. With it, the question
     * "did pressing buttons actually reach more code" has a number.
     *
     * Also the foundation for a code/data logger: a byte that has been executed is
     * code, whatever the disassembler guesses.
     *
     * 2 MiB window -> 256 KiB of bitmap. Off by default. */
    static constexpr uint32_t kCovLo = 0x200000, kCovHi = 0x3FFFFF;
    static constexpr uint32_t kCovSpan = kCovHi - kCovLo + 1;

    bool coverage_on = false;
    uint32_t coverage_hits = 0;                  /* distinct addresses executed */
    std::vector<uint8_t> coverage;               /* allocated on first enable */

    inline void note_exec(uint32_t pc) {
        if (pc < kCovLo || pc > kCovHi || coverage.empty()) return;
        const uint32_t i = pc - kCovLo;
        uint8_t& cell = coverage[i >> 3];
        const uint8_t bit = uint8_t(1u << (i & 7));
        if (!(cell & bit)) { cell |= bit; ++coverage_hits; }
    }

    inline void note_event(uint8_t type, uint32_t a, uint8_t v, uint32_t pc) {
        elog[elog_count % kElogSize] = {
            pc, a, uint16_t(scanline), uint16_t(cycle_residue), v, type};
        ++elog_count;
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

    /* THE READ LOG -- who READ this address?
     *
     * The mirror of the write log, and the half that was missing. "Which routine
     * writes this?" was answerable; "which routine READS this?" was not, and that is
     * the question you ask about a flag nobody seems to act on, or a table you think
     * is dead. A debugger that can only watch writes can only see half of any
     * conversation.
     *
     * ⚠️ INSTRUCTION FETCHES ARE NOT LOGGED. Every fetch goes through `read8`, so
     * logging them all would bury the one data read you care about under thousands
     * of fetches of the code doing the reading -- and arming a window over ROM would
     * log essentially every instruction in it. Only reads from OUTSIDE the current
     * fetch window are recorded, which is exactly the "the program loaded a value"
     * event. Same test the cart wait-state accounting already uses.
     *
     * Off by default (lo > hi): the hot path pays two compares, like the write log. */
    struct ReadRec { uint32_t pc; uint32_t addr; uint8_t value; };
    static constexpr uint32_t kRlogSize = 8192;
    mutable uint32_t rlog_lo = 1;      /* lo > hi  ==  logging off */
    mutable uint32_t rlog_hi = 0;
    mutable uint64_t rlog_count = 0;   /* every logged read, including ring-dropped */
    mutable ReadRec rlog[kRlogSize] = {};

    inline void note_read(uint32_t a, uint8_t v) const {
        if ((a - fetch_window) < 8u) return;      /* an instruction fetch, not a data read */
        rlog[rlog_count % kRlogSize] = {cpu.pc, a, v};
        ++rlog_count;
    }

    /* THE CALL STACK -- "how did I get here?"
     *
     * The one question a breakpoint always raises and that neither a PC nor a
     * register dump can answer. Every serious console debugger
     * shows the chain of callers; this core had nothing.
     *
     * Kept as a SHADOW stack, updated per instruction, rather than by walking the
     * real stack afterwards: the T900 pushes no frame pointer, so a value on the
     * stack that happens to look like a code address is indistinguishable from a
     * return address. Watching the transitions as they happen is exact.
     *
     * Recognition is by SP movement plus the pushed value, not by decoding opcodes
     * -- the decoder lives in the other language and this must stay on the hot path:
     *   CALL  SP fell, and the value now on top points just past the instruction we
     *         were executing (that is what a return address IS).
     *   RET   SP rose to or past a frame's entry SP; unwind every frame it passed,
     *         which also handles a routine that pops its own frame and jumps.
     *
     * Off by default: enabling costs a couple of compares and one 4-byte read per
     * call, which is not something a player should pay for. */
    struct Frame {
        uint32_t caller_pc;   /* the address of the CALL instruction itself */
        uint32_t entry_pc;    /* where it went (the routine's first instruction) */
        uint32_t return_pc;   /* where it will come back to */
        uint32_t entry_sp;    /* SP just before the call pushed anything */
    };
    static constexpr uint32_t kCallDepth = 64;
    bool callstack_on = false;
    uint32_t call_depth = 0;
    uint64_t call_overflow = 0;   /* frames dropped because the array was full */
    Frame callstack[kCallDepth] = {};

    inline void note_control_flow(uint32_t pc_before, uint32_t sp_before) {
        const uint32_t sp_now = cpu.regs[7];
        if (sp_now == sp_before) return;                 /* the common case: no push/pop */
        if (sp_now < sp_before) {
            /* Something was pushed. It is a CALL only if the top of the stack now
             * holds an address just past the instruction we just ran -- a PUSH of
             * data, or a stack frame being opened, must not become a call.
             *
             * ⛔ RAW BYTES, NOT read32(). Going through the normal read path made this
             * observer generate memory accesses of its own: they landed in the read log
             * and, worse, tripped the uninitialised-read detector on stack bytes the
             * PROGRAM never touched. One debug tool was manufacturing findings for
             * another -- the ROM analyzer reported ten stack addresses as game bugs that
             * were purely this probe's own reads. An observer must not be observable. */
            const uint32_t sp = sp_now & kAddrMask;
            const uint32_t top = (uint32_t(mem[sp]) |
                                  (uint32_t(mem[(sp + 1) & kAddrMask]) << 8) |
                                  (uint32_t(mem[(sp + 2) & kAddrMask]) << 16)) & 0x00FFFFFFu;
            if (top > pc_before && (top - pc_before) <= 8u) {
                if (call_depth < kCallDepth) {
                    callstack[call_depth] = {pc_before, cpu.pc, top, sp_before};
                    ++call_depth;
                } else {
                    ++call_overflow;   /* deep recursion: report it, do not corrupt */
                }
            }
        } else {
            /* The stack shrank: retire every frame whose entry SP it has reached. */
            while (call_depth && callstack[call_depth - 1].entry_sp <= sp_now) --call_depth;
        }
    }

    uint32_t rom_entry_point() const {
        if (rom.size() < 0x20) return 0x200000;
        return uint32_t(rom[0x1C]) | (uint32_t(rom[0x1D]) << 8) |
               (uint32_t(rom[0x1E]) << 16) | (uint32_t(rom[0x1F]) << 24);
    }
};

}  // namespace ngpc
#endif
