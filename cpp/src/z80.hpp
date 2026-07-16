/* z80.hpp — the NGPC's SOUND CPU.
 *
 * Everything here is sourced. See specs/Z80_SOUND_CPU.md, which records how each
 * address below was found: by instrumenting a real game on the native core, then
 * confirming it against SNK's own documentation.
 *
 * WHY IT EXISTS. The TLCS-900 core runs games at 48x real time and 25 of the 73
 * commercial ROMs compose a real picture. The other 42 sit on a black screen --
 * and not because of a rendering, boot or CPU bug. Pac-Man spins forever on
 *
 *     ld XIX, 0x000070DE ; ld A,(XIX) ; cp (XIX),A ; jr NZ
 *
 * waiting for SOMETHING ELSE to write 0x70DE. That something is the sound CPU:
 * 0x7000..0x7FFF is SHARED_Z80_RAM, and 50 000 instructions into its boot Pac-Man
 * has uploaded 2 691 bytes there, beginning `F3 31 C0 00 C3 E6 01` -- DI, LD SP,
 * JP. That is Z80 code, and there is no Z80 in this emulator.
 *
 * THE PROTOCOL (observed, then confirmed in 01_SDK/docs/K1SoundSim):
 *
 *     0x00B8  <- 0x55     release the Z80 from reset     (doc: BASE+18B8h)
 *     0x00BC  <- cmd      the dual-port comm register    (doc: BASE+18BCh)
 *     0x00BA  <- (any)    fire ONE NMI at the Z80        (doc: BASE+18BAh)
 *
 * and the Z80 sees its own memory at 0x0000, the comm register at 0x8000 and an
 * interrupt-control register at 0xC000.
 *
 * THE CLOCK. 3.072 MHz, exactly half the TLCS-900's 6.144. An integer ratio, so
 * the Z80 advances one cycle for every two of the main CPU's and no residue can
 * accumulate.
 *
 * THE DOCTRINE, unchanged: an un-ported opcode TRAPS LOUDLY. A sound CPU that
 * NOPs what it does not recognise would "work" while producing wrong answers, and
 * nothing would ever say so.
 */
#ifndef NGPC_Z80_HPP
#define NGPC_Z80_HPP

#include <cstdint>

namespace ngpc {

struct Machine;

/* The main CPU's view. */
constexpr uint32_t kZ80SharedRamBase  = 0x007000;
constexpr uint32_t kZ80SharedRamSize  = 0x1000;
/* ⚖️ THE RESET REGISTER IS 16 BITS, AND IT COMMANDS TWO CHIPS. SNK, K1SoundSim.txt
 * § 3.4.3.1 -- four states, one word (power-on value AAAAh, both in reset):
 *
 *      5555h -> Z80 RUN,   T6W28 RUN        AA55h -> Z80 RESET, T6W28 RUN
 *      55AAh -> Z80 RUN,   T6W28 reset      AAAAh -> both RESET
 *
 * Cross-read those four rows and the byte roles fall out: the HIGH byte (0xB9)
 * commands the Z80, the LOW byte (0xB8) commands the sound chip.
 *
 * This core drove the Z80 off 0xB8 -- the WRONG BYTE. It never showed, because every
 * game writes the word 5555h and both bytes come out 0x55; we released the Z80 for
 * the right reason by accident. A calibration ROM that wrote only the byte 0x55 to
 * 0xB8 (leaving 0xB9 at its power-on 0xAA = AA55h = Z80 RESET) is what exposed it:
 * on silicon the sound CPU never started, and its liveness stamp said so. */
constexpr uint32_t kZ80ResetRegister  = 0x0000B9;   /* HIGH byte: 0x55 = Z80 RUN   */
constexpr uint32_t kT6w28ResetRegister = 0x0000B8;  /* LOW byte : 0x55 = chip RUN  */
constexpr uint32_t kZ80NmiRegister    = 0x0000BA;   /* any write = one NMI     */
constexpr uint32_t kZ80CommRegister   = 0x0000BC;   /* dual-port, both see it  */
constexpr uint8_t  kZ80ReleaseValue   = 0x55;

/* ⚡ THE DAC — THE VOICE. The T6W28 makes tones; it cannot say "SEGAAA".
 *
 * A sampled voice reaches the speaker through a pair of 8-bit converters that the
 * MAIN CPU streams bytes into, one per channel, entirely bypassing the sound chip.
 * We modelled the sound chip and threw every one of these bytes away, which is why
 * a game's music was fine and its digitised voice was simply absent -- a silence
 * that no amount of staring at the PSG could explain.
 *
 * Found by measurement: Sonic's boot writes 26 050 bytes here in 300 frames, from
 * one instruction (0x3F1E72), and the values cluster around 0x80. That last detail
 * settles the format: this is UNSIGNED 8-bit PCM centred on 0x80, so 0x80 is
 * silence and the signed sample is (v - 0x80). */
constexpr uint32_t kDacLeftRegister   = 0x0000A2;
constexpr uint32_t kDacRightRegister  = 0x0000A3;

/* The Z80's own view. */
constexpr uint16_t kZ80CommAddress    = 0x8000;
constexpr uint16_t kZ80IntCtlAddress  = 0xC000;

/* The sound CPU can interrupt the MAIN one, and that is not a detail: three of
 * the BIOS's own routines go `ei 5 ; halt` and sleep until it does. The SNK doc
 * calls the register the Z80 writes to do it the "PC INT control register"
 * (Z80 address 0xC000), and the BIOS's user-vector table names slot 6 (0x6FD0)
 * "Interrupt from Z80" outright.
 *
 * On the main CPU it arrives as the INT0 pin: vector value 0x20 -> table index 8,
 * and its LEVEL is the low nibble of INTE0AD (0x0070) -- the register the BIOS
 * sets to 0x0D, i.e. INT0 at level 5, right before it halts with `ei 5`. Every
 * piece of that lines up, and the ten ROMs that were sleeping forever say the
 * rest. */
constexpr unsigned kIrqVectorIndexInt0 = 0x20 / 4;   /* 8 */

/* 3.072 MHz against 6.144: one Z80 cycle per two main-CPU cycles. */
constexpr uint32_t kZ80ClockDivider   = 2;

struct Z80 {
    /* Registers, in the pairs the ISA actually uses. */
    uint8_t a = 0, f = 0, b = 0, c = 0, d = 0, e = 0, h = 0, l = 0;
    uint8_t a_ = 0, f_ = 0, b_ = 0, c_ = 0, d_ = 0, e_ = 0, h_ = 0, l_ = 0;  /* the shadow set */
    uint16_t ix = 0, iy = 0, sp = 0, pc = 0;
    uint8_t i = 0, r = 0;
    bool iff1 = false, iff2 = false;
    uint8_t im = 0;

    bool halted = false;
    bool running = false;        /* false = held in reset by the main CPU      */
    bool nmi_pending = false;
    /* The MASKABLE interrupt line, and it does not come from anywhere inside the
     * Z80: SNK's own `8Bit.txt` says "T03 is used as an interrupt to the Z80 CPU".
     * The main CPU's 8-bit TIMER 3 is what paces the sound driver. Without it the
     * driver boots, idles, and never does a thing -- which is exactly what it did.
     */
    bool int_pending = false;

    /* Main-CPU cycles owed to the Z80. SIGNED on purpose: an instruction that costs
     * more than the credit left is borrowed against the next tick, never forgiven.
     * Clamping this at zero made the Z80 run 5x too fast (hw_calibration row 11). */
    int32_t cycle_credit = 0;
    uint64_t executed = 0;

    /* Set when an un-ported opcode is met. LOUD, never silent. */
    bool     trapped = false;
    uint16_t trap_pc = 0;
    uint8_t  trap_opcode = 0;
    uint8_t  trap_prefix = 0;    /* 0, or 0xCB / 0xDD / 0xED / 0xFD           */

    void reset();
};

/* Advance the Z80 by whatever `main_cycles` of the main CPU are worth. Does
 * nothing while it is held in reset. */
void z80_tick(Machine& m, uint32_t main_cycles);

/* The main CPU wrote one of the three control registers. */
void z80_control_write(Machine& m, uint32_t address, uint8_t value);

/* ⚡ THE WRITES THAT ARE ACTIONS, NOT STORES — IN ONE PLACE.
 *
 * Some addresses DO something when written: 0xB9 releases the sound CPU from reset,
 * 0xBA fires an NMI at it, 0xBC is the mailbox both CPUs read, and 0xA2 / 0xA3 push a
 * byte of sampled voice into the speaker. Left as plain memory, the byte just sits
 * there and the action never happens.
 *
 * This exists as ONE function because it briefly did not: `ngpc_bus_write` -- added as
 * "the same door the CPU uses" -- wrote the byte and skipped every action, so the DAC
 * test wrote to 0xA2 and heard silence. Two doors into the machine will always drift
 * apart; there is one. */
void io_action_write(Machine& m, uint32_t address, uint8_t value);

}  // namespace ngpc

#endif
