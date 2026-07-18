/* z80.cpp — the sound CPU. Clean-room, from the documented Zilog instruction set.
 *
 * See z80.hpp for WHY this exists and specs/Z80_SOUND_CPU.md for how every
 * address in it was established. The short version: 42 of the 73 commercial ROMs
 * show a black screen because they are waiting for their sound driver to answer,
 * and there was no sound CPU to run it.
 *
 * FLAGS, in the order the silicon lays them out:
 *
 *     bit 7 S   sign          bit 3  F3  (a copy of result bit 3)
 *     bit 6 Z   zero          bit 2  P/V parity / overflow
 *     bit 5 F5  (result bit 5)  bit 1  N   last op was a subtract
 *     bit 4 H   half carry    bit 0  C   carry
 *
 * F5 and F3 are undocumented but real: they are copies of bits 5 and 3 of the
 * result, and code has been known to read them back through PUSH AF. They cost
 * nothing to keep correct, so they are kept correct.
 *
 * AN UN-PORTED OPCODE TRAPS. It does not NOP, it does not guess. A sound CPU that
 * quietly skipped what it did not recognise would still "run" -- and would hand
 * the main CPU a wrong answer with nothing to say so.
 */
#include "machine.hpp"
#include "z80.hpp"

namespace ngpc {

constexpr uint8_t ZF_C = 0x01, ZF_N = 0x02, ZF_PV = 0x04, ZF_F3 = 0x08,
                  ZF_H = 0x10, ZF_F5 = 0x20, ZF_Z = 0x40, ZF_S = 0x80;

void Z80::reset() {
    a = f = b = c = d = e = h = l = 0;
    a_ = f_ = b_ = c_ = d_ = e_ = h_ = l_ = 0;
    ix = iy = sp = pc = 0;
    i = r = 0;
    iff1 = iff2 = false;
    im = 0;
    halted = false;
    nmi_pending = false;
    int_pending = false;
    cycle_credit = 0;
    executed = 0;
    trapped = false;
    trap_pc = trap_opcode = trap_prefix = 0;
    /* `running` is NOT cleared here: it is the main CPU that holds the Z80 in
     * reset, through 0xB8, and a CPU reset must not release it. */
}

/* --- the Z80's address space ----------------------------------------------
 *
 * 0x0000..0x3FFF : its 4 KB of RAM, mirrored. The main CPU sees the same bytes at
 *                  0x7000 -- that is the whole point of the shared window.
 * 0x4000..0x7FFF : the T6W28 sound chip. **NOT RAM.**
 * 0x8000..0xBFFF : the dual-port comm register (the main CPU's 0x00BC).
 * 0xC000..0xFFFF : the interrupt-acknowledge register.
 *
 * The 0x4000 page is where this first went wrong, and it is worth recording why.
 * Mirroring "everything below 0x8000" onto the 4 KB of RAM looked harmless -- and
 * it silently ALIASED every sound-chip write back onto the driver's own code and
 * data, one page lower. The driver kept running and kept corrupting itself.
 *
 * What settled it was not a guess: the Z80 was asked WHICH 4 KB pages it writes,
 * and it answered 0x0000, 0x4000, 0x8000 and 0xC000 -- four distinct regions, not
 * one mirrored one. A memory map with three of its four regions folded into the
 * first is not a memory map. */
static inline uint8_t z80_read(Machine& m, uint16_t addr) {
    if (addr < 0x4000) return m.mem[kZ80SharedRamBase + (addr & 0x0FFF)];
    if (addr < 0x8000) return 0xFF;                    /* the sound chip is write-only */
    return m.mem[kZ80CommRegister];                    /* comm + int-ack read the latch */
}
static inline void z80_write(Machine& m, uint16_t addr, uint8_t v) {
    if (addr < 0x4000) {
        const uint32_t a = kZ80SharedRamBase + (addr & 0x0FFF);
        m.mem[a] = v;
        /* The shared RAM is where the two processors TALK. A write log that cannot
         * see the Z80's half of the conversation answers "does the sound driver ever
         * reply?" with a confident, wrong ZERO. */
        m.note_write_from(a, v, Machine::kWlogZ80Pc | m.z80.pc);
        return;
    }
    if (addr < 0x8000) {
        m.log_apu_write(addr, v, NGPC_APU_WRITE_MEM);
        /* MEASURED across all 73 ROMs: the drivers touch 0x4000 and 0x4001 and
         * NOTHING ELSE in this window. A0 picks the stereo side (apu.hpp). */
        if (addr & 1) m.apu.write_left(v);
        else          m.apu.write_right(v);
        return;
    }
    if (addr < 0xC000) {
        m.mem[kZ80CommRegister] = v;
        m.note_write_from(kZ80CommRegister, v, Machine::kWlogZ80Pc | m.z80.pc);
        return;
    }

    /* ⚡ 0xC000 = THE PC INT CONTROL REGISTER. THE SOUND CPU INTERRUPTS THE MAIN ONE.
     *
     * SNK, K1SoundSim § 5.2.2, write-only, at exactly this address:
     *     "INTx request to the PC generated with write access (data invalid)"
     *
     * ⛔ AND THIS WAS ONCE WRITTEN OFF AS "TESTED AND REFUTED". It was not. The old
     * note said raising an interrupt here took the corpus from 52 ROMs drawing a
     * picture down to 35, and concluded that 0xC000 must therefore be the
     * ACKNOWLEDGE. It is not: § 5.2.4 puts the acknowledge on the I/O PORTS (an
     * `OUT`, which is why the calibration ROM's ISR ends in `OUT (0xFF),A`), and this
     * address is a MEMORY write.
     *
     * The experiment was right; the VECTOR was wrong. SNK writes "INTx", the old code
     * read it as INT0, and INT0 is a different pin. The BIOS names the real one
     * itself: it programs INTE45 = 0xDC -- INT4 at level 4 (that is VBlank) and
     * **INT5 at level 5** -- and then sits on `ei 5 ; halt`, which accepts nothing
     * below 5. It is waiting for INT5, and INT5 is what this write raises.
     *
     * 🔑 A REFUTATION IS ONLY AS GOOD AS THE CLAIM IT REFUTED. "Writing 0xC000 raises
     * an interrupt" was never what failed. "It raises INT0" was. */
    m.irq_pending |= 1u << kIrqVectorIndexInt5;
    m.z80_int_ack = v;
}

/* Writes aimed at the T6W28. The chip is modelled (core/apu.py) but NOT yet wired
 * to a mixer. What we must not do is pretend: every such write is now RECORDED --
 * address/port, value and cycle -- so the audio chantier starts from what the real
 * sound drivers DO rather than from an assumption about which door they use, and
 * so nobody can mistake silence for a working chip. */
static inline void z80_out(Machine& m, uint8_t port, uint8_t value) {
    /* ⚖️ AN I/O WRITE IS THE INTERRUPT ACKNOWLEDGE. SNK, K1SoundSim.txt § 5.2.4,
     * "Z80 Interrupt Acknowledge Register", ports 00..FFh, write-only:
     *
     *      "Releases INT request to the Z80 with write access (invalid data)."
     *
     * THE INT IS A LEVEL, NOT A PULSE. Timer 3's external output TO3 holds the line
     * asserted (§ 5.2.1: the Z80 has no timer of its own and is wired to TO3), and
     * only this write drops it. A core that auto-clears the line on acceptance --
     * which this one did -- models a pulse, and a driver that forgets to acknowledge
     * would be re-entered forever on silicon while running fine here.
     *
     * ⭐ AND THIS CLOSES A REAL MYSTERY. Pass 209 found that 71 of the 73 ROMs run
     * `OUT (0xFF),A`, recorded it, and wrote "destination unknown" rather than guess.
     * THIS is the destination: it is the interrupt acknowledge. The manual says the
     * data is INVALID, which is why the byte looked like a meaningless tick counter --
     * the driver simply dumps whatever is in A. Not sound. An acknowledgement.
     *
     * A calibration ROM whose ISR did NOT acknowledge is what proved it: on real
     * hardware the Z80 took 54 831 interrupts in two seconds and completed ONE loop
     * iteration -- it was re-entering the handler the instant it left it. */
    m.z80.int_pending = false;
    m.z80_int_ack = value;
    m.log_apu_write(port, value, NGPC_APU_WRITE_PORT);
}
static inline uint8_t z80_in(Machine& m, uint8_t port) {
    (void)port;
    return 0xFF;   /* open bus */
}

/* --- flag helpers ---------------------------------------------------------- */
static inline uint8_t sz53(uint8_t v) {
    return uint8_t((v & (ZF_S | ZF_F5 | ZF_F3)) | (v == 0 ? ZF_Z : 0));
}
static inline bool parity8(uint8_t v) {
    v ^= uint8_t(v >> 4); v ^= uint8_t(v >> 2); v ^= uint8_t(v >> 1);
    return (v & 1) == 0;             // even parity -> P/V set
}
static inline uint8_t sz53p(uint8_t v) {
    return uint8_t(sz53(v) | (parity8(v) ? ZF_PV : 0));
}

static inline void z_add8(Z80& z, uint8_t v, uint8_t carry_in) {
    const unsigned res = unsigned(z.a) + v + carry_in;
    const uint8_t r8 = uint8_t(res);
    uint8_t f = sz53(r8);
    if (res > 0xFF) f |= ZF_C;
    if (((z.a ^ v ^ 0x80) & (z.a ^ r8)) & 0x80) f |= ZF_PV;
    if (((z.a & 0x0F) + (v & 0x0F) + carry_in) > 0x0F) f |= ZF_H;
    z.a = r8; z.f = f;               // N = 0
}
static inline uint8_t z_sub8_core(Z80& z, uint8_t lhs, uint8_t v, uint8_t carry_in) {
    const int res = int(lhs) - int(v) - int(carry_in);
    const uint8_t r8 = uint8_t(res);
    uint8_t f = uint8_t(sz53(r8) | ZF_N);
    if (res < 0) f |= ZF_C;
    if (((lhs ^ v) & (lhs ^ r8)) & 0x80) f |= ZF_PV;
    if ((int(lhs & 0x0F) - int(v & 0x0F) - int(carry_in)) < 0) f |= ZF_H;
    z.f = f;
    return r8;
}
static inline void z_sub8(Z80& z, uint8_t v, uint8_t carry_in) {
    z.a = z_sub8_core(z, z.a, v, carry_in);
}
static inline void z_cp8(Z80& z, uint8_t v) {
    const uint8_t r8 = z_sub8_core(z, z.a, v, 0);
    (void)r8;
    /* CP takes F5 and F3 from the OPERAND, not the result -- one of the few places
     * the undocumented bits differ from every other subtract. */
    z.f = uint8_t((z.f & ~(ZF_F5 | ZF_F3)) | (v & (ZF_F5 | ZF_F3)));
}
static inline void z_and(Z80& z, uint8_t v) { z.a &= v; z.f = uint8_t(sz53p(z.a) | ZF_H); }
static inline void z_or (Z80& z, uint8_t v) { z.a |= v; z.f = sz53p(z.a); }
static inline void z_xor(Z80& z, uint8_t v) { z.a ^= v; z.f = sz53p(z.a); }

static inline uint8_t z_inc8(Z80& z, uint8_t v) {
    const uint8_t r8 = uint8_t(v + 1);
    uint8_t f = uint8_t((z.f & ZF_C) | sz53(r8));
    if ((r8 & 0x0F) == 0) f |= ZF_H;
    if (r8 == 0x80) f |= ZF_PV;
    z.f = f;                          // N = 0
    return r8;
}
static inline uint8_t z_dec8(Z80& z, uint8_t v) {
    const uint8_t r8 = uint8_t(v - 1);
    uint8_t f = uint8_t((z.f & ZF_C) | sz53(r8) | ZF_N);
    if ((v & 0x0F) == 0) f |= ZF_H;
    if (r8 == 0x7F) f |= ZF_PV;
    z.f = f;
    return r8;
}

static inline uint16_t z_add16(Z80& z, uint16_t lhs, uint16_t rhs) {
    const unsigned res = unsigned(lhs) + rhs;
    const uint16_t r16 = uint16_t(res);
    uint8_t f = uint8_t(z.f & (ZF_S | ZF_Z | ZF_PV));
    if (res > 0xFFFF) f |= ZF_C;
    if (((lhs & 0x0FFF) + (rhs & 0x0FFF)) > 0x0FFF) f |= ZF_H;
    f |= uint8_t((r16 >> 8) & (ZF_F5 | ZF_F3));
    z.f = f;                          // N = 0
    return r16;
}

/* --- 16-bit register pairs, by their opcode index -------------------------- */
static inline uint16_t rp(const Z80& z, unsigned i) {
    switch (i) {
        case 0: return uint16_t((z.b << 8) | z.c);
        case 1: return uint16_t((z.d << 8) | z.e);
        case 2: return uint16_t((z.h << 8) | z.l);
        default: return z.sp;
    }
}
static inline void set_rp(Z80& z, unsigned i, uint16_t v) {
    switch (i) {
        case 0: z.b = uint8_t(v >> 8); z.c = uint8_t(v); break;
        case 1: z.d = uint8_t(v >> 8); z.e = uint8_t(v); break;
        case 2: z.h = uint8_t(v >> 8); z.l = uint8_t(v); break;
        default: z.sp = v; break;
    }
}

static inline bool cc(const Z80& z, unsigned k) {
    switch (k) {
        case 0: return !(z.f & ZF_Z);   case 1: return  (z.f & ZF_Z);
        case 2: return !(z.f & ZF_C);   case 3: return  (z.f & ZF_C);
        case 4: return !(z.f & ZF_PV);  case 5: return  (z.f & ZF_PV);
        case 6: return !(z.f & ZF_S);   default: return (z.f & ZF_S);
    }
}

/* --- one instruction ------------------------------------------------------- */
namespace {

struct Ctx {
    Machine& m;
    Z80& z;
    uint8_t fetch8()  { return z80_read(m, z.pc++); }
    uint16_t fetch16() { const uint8_t lo = fetch8(); return uint16_t(lo | (fetch8() << 8)); }
    void push16(uint16_t v) {
        z80_write(m, --z.sp, uint8_t(v >> 8));
        z80_write(m, --z.sp, uint8_t(v));
    }
    uint16_t pop16() {
        const uint8_t lo = z80_read(m, z.sp++);
        const uint8_t hi = z80_read(m, z.sp++);
        return uint16_t(lo | (hi << 8));
    }
    /* The eight 8-bit operands an opcode's 3-bit field can name. Index 6 is
     * `(HL)`, which is memory, not a register -- that is why this returns a value
     * and `put_r` exists separately. */
    uint8_t get_r(unsigned i) {
        switch (i) {
            case 0: return z.b; case 1: return z.c; case 2: return z.d; case 3: return z.e;
            case 4: return z.h; case 5: return z.l;
            case 6: return z80_read(m, uint16_t((z.h << 8) | z.l));
            default: return z.a;
        }
    }
    void put_r(unsigned i, uint8_t v) {
        switch (i) {
            case 0: z.b = v; break; case 1: z.c = v; break; case 2: z.d = v; break;
            case 3: z.e = v; break; case 4: z.h = v; break; case 5: z.l = v; break;
            case 6: z80_write(m, uint16_t((z.h << 8) | z.l), v); break;
            default: z.a = v; break;
        }
    }
};

/* CB: the rotate/shift and bit families. Complete -- there are no holes here. */
unsigned exec_cb(Ctx& x) {
    Z80& z = x.z;
    const uint8_t op = x.fetch8();
    const unsigned kind = op >> 6;
    const unsigned bit = (op >> 3) & 7;
    const unsigned r = op & 7;
    uint8_t v = x.get_r(r);

    if (kind == 0) {                              // rotates and shifts
        const bool carry = (z.f & ZF_C) != 0;
        uint8_t out;
        switch (bit) {
            case 0: out = uint8_t(v & 0x80); v = uint8_t((v << 1) | (out ? 1 : 0)); break; // RLC
            case 1: out = uint8_t(v & 0x01); v = uint8_t((v >> 1) | (out ? 0x80 : 0)); break; // RRC
            case 2: out = uint8_t(v & 0x80); v = uint8_t((v << 1) | (carry ? 1 : 0)); break; // RL
            case 3: out = uint8_t(v & 0x01); v = uint8_t((v >> 1) | (carry ? 0x80 : 0)); break; // RR
            case 4: out = uint8_t(v & 0x80); v = uint8_t(v << 1); break;                    // SLA
            case 5: out = uint8_t(v & 0x01); v = uint8_t((v >> 1) | (v & 0x80)); break;      // SRA
            case 6: out = uint8_t(v & 0x80); v = uint8_t((v << 1) | 1); break;               // SLL (undocumented, and real)
            default: out = uint8_t(v & 0x01); v = uint8_t(v >> 1); break;                    // SRL
        }
        z.f = uint8_t(sz53p(v) | (out ? ZF_C : 0));
        x.put_r(r, v);
        return r == 6 ? 15u : 8u;
    }
    if (kind == 1) {                              // BIT
        const bool set = (v >> bit) & 1;
        uint8_t f = uint8_t((z.f & ZF_C) | ZF_H);
        if (!set) f |= uint8_t(ZF_Z | ZF_PV);
        if (bit == 7 && set) f |= ZF_S;
        f |= uint8_t(v & (ZF_F5 | ZF_F3));
        z.f = f;
        return r == 6 ? 12u : 8u;
    }
    if (kind == 2) x.put_r(r, uint8_t(v & ~(1u << bit)));   // RES
    else           x.put_r(r, uint8_t(v |  (1u << bit)));   // SET
    return r == 6 ? 15u : 8u;
}

/* ED: the block moves, the 16-bit adds with carry, the interrupt modes. */
unsigned exec_ed(Ctx& x) {
    Machine& m = x.m;
    Z80& z = x.z;
    const uint8_t op = x.fetch8();

    switch (op) {
        case 0x44: case 0x4C: case 0x54: case 0x5C:                 // NEG
        case 0x64: case 0x6C: case 0x74: case 0x7C: {
            const uint8_t v = z.a;
            z.a = 0;
            z_sub8(z, v, 0);
            return 8;
        }
        case 0x46: case 0x4E: case 0x66: case 0x6E: z.im = 0; return 8;   // IM 0
        case 0x56: case 0x76: z.im = 1; return 8;                          // IM 1
        case 0x5E: case 0x7E: z.im = 2; return 8;                          // IM 2
        case 0x47: z.i = z.a; return 9;                                    // LD I,A
        case 0x4F: z.r = z.a; return 9;                                    // LD R,A
        case 0x57: z.a = z.i; z.f = uint8_t((z.f & ZF_C) | sz53(z.a) | (z.iff2 ? ZF_PV : 0)); return 9;
        case 0x5F: z.a = z.r; z.f = uint8_t((z.f & ZF_C) | sz53(z.a) | (z.iff2 ? ZF_PV : 0)); return 9;
        case 0x45: case 0x55: case 0x65: case 0x75:                        // RETN
        case 0x4D: case 0x5D: case 0x6D: case 0x7D:                        // RETI
            z.pc = x.pop16(); z.iff1 = z.iff2; return 14;

        /* ⚡ RRD / RLD — HALF A BYTE AT A TIME, BETWEEN A AND (HL).
         *
         * The nibble is the point: packing two 4-bit values into one byte is how
         * music data stays small (a note and a volume, a length and an index), and
         * these are the fast way to unpack it. A driver with nibble-packed pattern
         * data needs them; the ones in the corpus store a byte per value, which is
         * why the hole never showed. */
        case 0x67: {                                                       // RRD
            const uint16_t hl = uint16_t((z.h << 8) | z.l);
            const uint8_t v = z80_read(m, hl);
            z80_write(m, hl, uint8_t(((z.a & 0x0F) << 4) | (v >> 4)));
            z.a = uint8_t((z.a & 0xF0) | (v & 0x0F));
            z.f = uint8_t((z.f & ZF_C) | sz53p(z.a));                      // H = N = 0
            return 18;
        }
        case 0x6F: {                                                       // RLD
            const uint16_t hl = uint16_t((z.h << 8) | z.l);
            const uint8_t v = z80_read(m, hl);
            z80_write(m, hl, uint8_t((v << 4) | (z.a & 0x0F)));
            z.a = uint8_t((z.a & 0xF0) | (v >> 4));
            z.f = uint8_t((z.f & ZF_C) | sz53p(z.a));                      // H = N = 0
            return 18;
        }
        default: break;
    }

    /* ⚡ IN r,(C) / OUT (C),r — THE PORT NUMBER FROM A REGISTER.
     *
     * The immediate forms (0xD3 / 0xDB) were already here; these take the port from
     * C so it can be computed. That matters on THIS console specifically: an I/O
     * write is the interrupt acknowledge (K1SoundSim § 5.2.4, see z80_out), so a
     * driver acknowledging with `out (c),a` rather than `out (0xFF),a` would have
     * trapped the sound CPU dead.
     *
     * ⚠️ Operand 6 is the undocumented pair -- `IN (C)` sets flags and discards the
     * byte, `OUT (C),0` writes zero. Neither may go through get_r/put_r, which would
     * read or write (HL) instead of a register. */
    if ((op & 0xC7) == 0x40) {                                             // IN r,(C)
        const uint8_t v = z80_in(m, z.c);
        const unsigned r = (op >> 3) & 7;
        if (r != 6) x.put_r(r, v);
        z.f = uint8_t((z.f & ZF_C) | sz53p(v));                            // H = N = 0
        return 12;
    }
    if ((op & 0xC7) == 0x41) {                                             // OUT (C),r
        const unsigned r = (op >> 3) & 7;
        z80_out(m, z.c, (r == 6) ? uint8_t(0) : x.get_r(r));
        return 12;
    }

    /* ADC / SBC HL,rr  -- `01 rr 1 010` = SBC, `01 rr 1 011` hmm: the pattern is
     * 0x42 + 0x10*rr for SBC and 0x4A + 0x10*rr for ADC. */
    if ((op & 0xCF) == 0x42 || (op & 0xCF) == 0x4A) {
        const unsigned i = (op >> 4) & 3;
        const uint16_t hl = uint16_t((z.h << 8) | z.l);
        const uint16_t v = rp(z, i);
        const uint8_t carry = uint8_t(z.f & ZF_C);
        uint32_t res;
        uint8_t f;
        if ((op & 0x08) == 0) {                    // SBC HL,rr
            res = uint32_t(hl) - v - carry;
            f = ZF_N;
            if (int32_t(hl & 0x0FFF) - int32_t(v & 0x0FFF) - carry < 0) f |= ZF_H;
            if (((hl ^ v) & (hl ^ uint16_t(res))) & 0x8000) f |= ZF_PV;
            if (int32_t(hl) - int32_t(v) - int32_t(carry) < 0) f |= ZF_C;
        } else {                                   // ADC HL,rr
            res = uint32_t(hl) + v + carry;
            f = 0;
            if (((hl & 0x0FFF) + (v & 0x0FFF) + carry) > 0x0FFF) f |= ZF_H;
            if (((hl ^ v ^ 0x8000) & (hl ^ uint16_t(res))) & 0x8000) f |= ZF_PV;
            if (res > 0xFFFF) f |= ZF_C;
        }
        const uint16_t r16 = uint16_t(res);
        if (r16 == 0) f |= ZF_Z;
        f |= uint8_t((r16 >> 8) & (ZF_S | ZF_F5 | ZF_F3));
        z.h = uint8_t(r16 >> 8); z.l = uint8_t(r16);
        z.f = f;
        return 15;
    }

    /* LD (nn),rr  /  LD rr,(nn) */
    if ((op & 0xCF) == 0x43 || (op & 0xCF) == 0x4B) {
        const unsigned i = (op >> 4) & 3;
        const uint16_t nn = x.fetch16();
        if ((op & 0x08) == 0) {                    // LD (nn),rr
            const uint16_t v = rp(z, i);
            z80_write(m, nn, uint8_t(v));
            z80_write(m, uint16_t(nn + 1), uint8_t(v >> 8));
        } else {                                   // LD rr,(nn)
            const uint8_t lo = z80_read(m, nn);
            const uint8_t hi = z80_read(m, uint16_t(nn + 1));
            set_rp(z, i, uint16_t(lo | (hi << 8)));
        }
        return 20;
    }

    /* The block instructions. LDIR/LDDR are how a sound driver moves a pattern. */
    if (op >= 0xA0 && op <= 0xBB && (op & 0x04) == 0) {
        const bool inc = (op & 0x08) == 0;
        const bool repeat = (op & 0x10) != 0;
        const unsigned kind = op & 0x03;           // 0 = LD, 1 = CP, 2 = IN, 3 = OUT
        const int step = inc ? 1 : -1;

        uint16_t hl = uint16_t((z.h << 8) | z.l);

        /* ⚡ THE BLOCK I/O OPS — INI / IND / OUTI / OUTD AND THEIR REPEATING FORMS.
         *
         * Same shape as LDIR, but one end is an I/O PORT instead of memory. On this
         * console the T6W28 is MEMORY-mapped (0x4000/0x4001), so its drivers move
         * data with plain stores and never need these -- which is exactly why they
         * sat un-ported behind a trap for so long.
         *
         * ⚠️ THEY COUNT WITH B ALONE, NOT BC. That is the trap, and it is why this
         * cannot share the writeback below: LDIR/CPIR decrement the 16-bit pair BC,
         * these decrement the 8-bit register B and leave C alone -- because C holds
         * the PORT NUMBER. Running them through the BC path would corrupt the port
         * on every iteration, silently.
         *
         * The flags are the manual's, not the silicon's: Zilog defines only Z (from
         * B) and N (set), calls S/H/PV undefined, and says carry is not affected.
         * Real hardware is known to do more (N from bit 7 of the byte, H and C from
         * an overflow involving it); that model is NOT implemented, and nothing here
         * could test it if it were. */
        if (kind >= 2) {
            if (kind == 2) {                       // INI / IND / INIR / INDR
                z80_write(m, hl, z80_in(m, z.c));
            } else {                               // OUTI / OUTD / OTIR / OTDR
                z80_out(m, z.c, z80_read(m, hl));
            }
            hl = uint16_t(hl + step);
            z.b = uint8_t(z.b - 1);
            z.h = uint8_t(hl >> 8); z.l = uint8_t(hl);
            z.f = uint8_t((z.f & ZF_C) | ZF_N | sz53(z.b));
            if (repeat && z.b != 0) {
                z.pc = uint16_t(z.pc - 2);         // the loop is INSIDE the opcode
                return 21;
            }
            return 16;
        }

        uint16_t de = uint16_t((z.d << 8) | z.e);
        uint16_t bc = uint16_t((z.b << 8) | z.c);

        if (kind == 0) {                           // LDI / LDD / LDIR / LDDR
            const uint8_t v = z80_read(m, hl);
            z80_write(m, de, v);
            hl = uint16_t(hl + step); de = uint16_t(de + step); --bc;
            const uint8_t n = uint8_t(z.a + v);
            z.f = uint8_t((z.f & (ZF_S | ZF_Z | ZF_C))
                          | (bc ? ZF_PV : 0)
                          | (n & ZF_F3)
                          | ((n & 0x02) ? ZF_F5 : 0));
        } else if (kind == 1) {                    // CPI / CPD / CPIR / CPDR
            const uint8_t v = z80_read(m, hl);
            const uint8_t r8 = uint8_t(z.a - v);
            hl = uint16_t(hl + step); --bc;
            uint8_t f = uint8_t((z.f & ZF_C) | ZF_N | sz53(r8));
            f &= uint8_t(~(ZF_F5 | ZF_F3));
            if ((int(z.a & 0x0F) - int(v & 0x0F)) < 0) f |= ZF_H;
            if (bc) f |= ZF_PV;
            z.f = f;
            if (r8 == 0) {                          // a CP block op STOPS on a match
                z.b = uint8_t(bc >> 8); z.c = uint8_t(bc);
                z.h = uint8_t(hl >> 8); z.l = uint8_t(hl);
                return 16;
            }
        }

        z.b = uint8_t(bc >> 8); z.c = uint8_t(bc);
        z.d = uint8_t(de >> 8); z.e = uint8_t(de);
        z.h = uint8_t(hl >> 8); z.l = uint8_t(hl);

        if (repeat && bc != 0) {
            z.pc = uint16_t(z.pc - 2);              // re-execute: the loop is INSIDE the opcode
            return 21;
        }
        return 16;
    }

    z.trapped = true; z.trap_pc = uint16_t(z.pc - 2);
    z.trap_opcode = op; z.trap_prefix = 0xED;
    return 0;
}

/* DD / FD: the same instruction set again, with IX or IY standing in for HL and a
 * signed displacement on the memory forms. */
unsigned exec_index(Ctx& x, uint8_t prefix) {
    Machine& m = x.m;
    Z80& z = x.z;
    uint16_t& xy = (prefix == 0xDD) ? z.ix : z.iy;
    const uint8_t op = x.fetch8();

    auto xy_h = [&]() { return uint8_t(xy >> 8); };
    auto xy_l = [&]() { return uint8_t(xy); };
    auto set_xy_h = [&](uint8_t v) { xy = uint16_t((v << 8) | (xy & 0xFF)); };
    auto set_xy_l = [&](uint8_t v) { xy = uint16_t((xy & 0xFF00) | v); };
    auto ea = [&]() { return uint16_t(xy + int8_t(x.fetch8())); };

    switch (op) {
        case 0x21: xy = x.fetch16(); return 14;                              // LD xy,nn
        case 0x22: { const uint16_t nn = x.fetch16();                        // LD (nn),xy
                     z80_write(m, nn, uint8_t(xy));
                     z80_write(m, uint16_t(nn + 1), uint8_t(xy >> 8)); return 20; }
        case 0x2A: { const uint16_t nn = x.fetch16();                        // LD xy,(nn)
                     xy = uint16_t(z80_read(m, nn) | (z80_read(m, uint16_t(nn + 1)) << 8));
                     return 20; }
        case 0x23: ++xy; return 10;                                          // INC xy
        case 0x2B: --xy; return 10;                                          // DEC xy
        case 0xE5: x.push16(xy); return 15;                                  // PUSH xy
        case 0xE1: xy = x.pop16(); return 14;                                // POP xy
        case 0xE9: z.pc = xy; return 8;                                      // JP (xy)
        case 0xF9: z.sp = xy; return 10;                                     // LD SP,xy
        case 0xE3: { const uint16_t t = x.pop16(); x.push16(xy); xy = t; return 23; }  // EX (SP),xy
        case 0x24: set_xy_h(z_inc8(z, xy_h())); return 8;
        case 0x25: set_xy_h(z_dec8(z, xy_h())); return 8;
        case 0x2C: set_xy_l(z_inc8(z, xy_l())); return 8;
        case 0x2D: set_xy_l(z_dec8(z, xy_l())); return 8;
        case 0x26: set_xy_h(x.fetch8()); return 11;
        case 0x2E: set_xy_l(x.fetch8()); return 11;
        case 0x34: { const uint16_t a = ea(); z80_write(m, a, z_inc8(z, z80_read(m, a))); return 23; }
        case 0x35: { const uint16_t a = ea(); z80_write(m, a, z_dec8(z, z80_read(m, a))); return 23; }
        case 0x36: { const uint16_t a = ea(); z80_write(m, a, x.fetch8()); return 19; }
        case 0x09: xy = z_add16(z, xy, uint16_t((z.b << 8) | z.c)); return 15;
        case 0x19: xy = z_add16(z, xy, uint16_t((z.d << 8) | z.e)); return 15;
        case 0x29: xy = z_add16(z, xy, xy); return 15;
        case 0x39: xy = z_add16(z, xy, z.sp); return 15;
        case 0xCB: {                                                         // DDCB / FDCB
            const uint16_t a = ea();
            const uint8_t sub = x.fetch8();
            const unsigned kind = sub >> 6;
            const unsigned bit = (sub >> 3) & 7;
            uint8_t v = z80_read(m, a);
            if (kind == 1) {                                                 // BIT
                const bool set = (v >> bit) & 1;
                uint8_t f = uint8_t((z.f & ZF_C) | ZF_H);
                if (!set) f |= uint8_t(ZF_Z | ZF_PV);
                if (bit == 7 && set) f |= ZF_S;
                f |= uint8_t((a >> 8) & (ZF_F5 | ZF_F3));
                z.f = f;
                return 20;
            }
            if (kind == 0) {
                const bool carry = (z.f & ZF_C) != 0;
                uint8_t out;
                switch (bit) {
                    case 0: out = uint8_t(v & 0x80); v = uint8_t((v << 1) | (out ? 1 : 0)); break;
                    case 1: out = uint8_t(v & 0x01); v = uint8_t((v >> 1) | (out ? 0x80 : 0)); break;
                    case 2: out = uint8_t(v & 0x80); v = uint8_t((v << 1) | (carry ? 1 : 0)); break;
                    case 3: out = uint8_t(v & 0x01); v = uint8_t((v >> 1) | (carry ? 0x80 : 0)); break;
                    case 4: out = uint8_t(v & 0x80); v = uint8_t(v << 1); break;
                    case 5: out = uint8_t(v & 0x01); v = uint8_t((v >> 1) | (v & 0x80)); break;
                    case 6: out = uint8_t(v & 0x80); v = uint8_t((v << 1) | 1); break;
                    default: out = uint8_t(v & 0x01); v = uint8_t(v >> 1); break;
                }
                z.f = uint8_t(sz53p(v) | (out ? ZF_C : 0));
            } else if (kind == 2) v = uint8_t(v & ~(1u << bit));
            else                  v = uint8_t(v |  (1u << bit));
            z80_write(m, a, v);
            return 23;
        }
        default: break;
    }

    /* LD r,(xy+d) and LD (xy+d),r */
    if ((op & 0xC7) == 0x46 && op != 0x76) {         // LD r,(xy+d)
        const unsigned r = (op >> 3) & 7;
        x.put_r(r, z80_read(m, ea()));
        return 19;
    }
    if ((op & 0xF8) == 0x70 && op != 0x76) {         // LD (xy+d),r
        const unsigned r = op & 7;
        const uint16_t a = ea();
        z80_write(m, a, x.get_r(r));
        return 19;
    }
    /* ALU A,(xy+d) */
    if (op >= 0x80 && op <= 0xBF && (op & 7) == 6) {
        const uint8_t v = z80_read(m, ea());
        switch ((op >> 3) & 7) {
            case 0: z_add8(z, v, 0); break;
            case 1: z_add8(z, v, uint8_t(z.f & ZF_C)); break;
            case 2: z_sub8(z, v, 0); break;
            case 3: z_sub8(z, v, uint8_t(z.f & ZF_C)); break;
            case 4: z_and(z, v); break;
            case 5: z_xor(z, v); break;
            case 6: z_or(z, v); break;
            default: z_cp8(z, v); break;
        }
        return 19;
    }

    z.trapped = true; z.trap_pc = uint16_t(z.pc - 2);
    z.trap_opcode = op; z.trap_prefix = prefix;
    return 0;
}

unsigned exec_one(Machine& m, Z80& z) {
    Ctx x{m, z};

    if (z.halted) return 4;                        // parked until an interrupt

    const uint16_t pc0 = z.pc;
    const uint8_t op = x.fetch8();
    z.r = uint8_t((z.r & 0x80) | ((z.r + 1) & 0x7F));

    /* The 8-bit load block: 0x40..0x7F is `LD r,r'` in its entirety, except 0x76
     * which is HALT -- the one hole in an otherwise perfectly regular table. */
    if (op >= 0x40 && op <= 0x7F) {
        if (op == 0x76) { z.halted = true; return 4; }
        const unsigned dst = (op >> 3) & 7;
        const unsigned src = op & 7;
        x.put_r(dst, x.get_r(src));
        return (dst == 6 || src == 6) ? 7u : 4u;
    }
    /* The ALU block: 0x80..0xBF, eight operations x eight operands. */
    if (op >= 0x80 && op <= 0xBF) {
        const unsigned src = op & 7;
        const uint8_t v = x.get_r(src);
        switch ((op >> 3) & 7) {
            case 0: z_add8(z, v, 0); break;
            case 1: z_add8(z, v, uint8_t(z.f & ZF_C)); break;
            case 2: z_sub8(z, v, 0); break;
            case 3: z_sub8(z, v, uint8_t(z.f & ZF_C)); break;
            case 4: z_and(z, v); break;
            case 5: z_xor(z, v); break;
            case 6: z_or(z, v); break;
            default: z_cp8(z, v); break;
        }
        return src == 6 ? 7u : 4u;
    }

    switch (op) {
        case 0x00: return 4;                                           // NOP
        case 0x08: {                                                   // EX AF,AF'
            uint8_t t = z.a; z.a = z.a_; z.a_ = t;
            t = z.f; z.f = z.f_; z.f_ = t;
            return 4;
        }
        case 0xD9: {                                                   // EXX
            uint8_t t;
            t = z.b; z.b = z.b_; z.b_ = t;  t = z.c; z.c = z.c_; z.c_ = t;
            t = z.d; z.d = z.d_; z.d_ = t;  t = z.e; z.e = z.e_; z.e_ = t;
            t = z.h; z.h = z.h_; z.h_ = t;  t = z.l; z.l = z.l_; z.l_ = t;
            return 4;
        }
        case 0xEB: {                                                   // EX DE,HL
            uint8_t t = z.d; z.d = z.h; z.h = t;
            t = z.e; z.e = z.l; z.l = t;
            return 4;
        }
        case 0xE3: {                                                   // EX (SP),HL
            const uint16_t t = x.pop16();
            x.push16(uint16_t((z.h << 8) | z.l));
            z.h = uint8_t(t >> 8); z.l = uint8_t(t);
            return 19;
        }
        case 0x02: z80_write(m, uint16_t((z.b << 8) | z.c), z.a); return 7;   // LD (BC),A
        case 0x12: z80_write(m, uint16_t((z.d << 8) | z.e), z.a); return 7;   // LD (DE),A
        case 0x0A: z.a = z80_read(m, uint16_t((z.b << 8) | z.c)); return 7;
        case 0x1A: z.a = z80_read(m, uint16_t((z.d << 8) | z.e)); return 7;
        case 0x32: { const uint16_t nn = x.fetch16(); z80_write(m, nn, z.a); return 13; }
        case 0x3A: { const uint16_t nn = x.fetch16(); z.a = z80_read(m, nn); return 13; }
        case 0x22: { const uint16_t nn = x.fetch16();                          // LD (nn),HL
                     z80_write(m, nn, z.l); z80_write(m, uint16_t(nn + 1), z.h); return 16; }
        case 0x2A: { const uint16_t nn = x.fetch16();                          // LD HL,(nn)
                     z.l = z80_read(m, nn); z.h = z80_read(m, uint16_t(nn + 1)); return 16; }
        case 0x07: {                                                   // RLCA
            const uint8_t out = uint8_t(z.a & 0x80);
            z.a = uint8_t((z.a << 1) | (out ? 1 : 0));
            z.f = uint8_t((z.f & (ZF_S | ZF_Z | ZF_PV)) | (out ? ZF_C : 0) | (z.a & (ZF_F5 | ZF_F3)));
            return 4;
        }
        case 0x0F: {                                                   // RRCA
            const uint8_t out = uint8_t(z.a & 0x01);
            z.a = uint8_t((z.a >> 1) | (out ? 0x80 : 0));
            z.f = uint8_t((z.f & (ZF_S | ZF_Z | ZF_PV)) | (out ? ZF_C : 0) | (z.a & (ZF_F5 | ZF_F3)));
            return 4;
        }
        case 0x17: {                                                   // RLA
            const uint8_t out = uint8_t(z.a & 0x80);
            z.a = uint8_t((z.a << 1) | ((z.f & ZF_C) ? 1 : 0));
            z.f = uint8_t((z.f & (ZF_S | ZF_Z | ZF_PV)) | (out ? ZF_C : 0) | (z.a & (ZF_F5 | ZF_F3)));
            return 4;
        }
        case 0x1F: {                                                   // RRA
            const uint8_t out = uint8_t(z.a & 0x01);
            z.a = uint8_t((z.a >> 1) | ((z.f & ZF_C) ? 0x80 : 0));
            z.f = uint8_t((z.f & (ZF_S | ZF_Z | ZF_PV)) | (out ? ZF_C : 0) | (z.a & (ZF_F5 | ZF_F3)));
            return 4;
        }
        case 0x27: {                                                   // DAA
            uint8_t adj = 0;
            bool carry = (z.f & ZF_C) != 0;
            if ((z.f & ZF_H) || (z.a & 0x0F) > 9) adj |= 0x06;
            if (carry || z.a > 0x99) { adj |= 0x60; carry = true; }
            const uint8_t before = z.a;
            z.a = (z.f & ZF_N) ? uint8_t(z.a - adj) : uint8_t(z.a + adj);
            uint8_t f = uint8_t((z.f & ZF_N) | sz53p(z.a) | (carry ? ZF_C : 0));
            if ((before ^ z.a) & 0x10) f |= ZF_H;
            z.f = f;
            return 4;
        }
        case 0x2F: z.a = uint8_t(~z.a);                                // CPL
                   z.f = uint8_t((z.f & (ZF_S | ZF_Z | ZF_PV | ZF_C)) | ZF_H | ZF_N | (z.a & (ZF_F5 | ZF_F3)));
                   return 4;
        case 0x37: z.f = uint8_t((z.f & (ZF_S | ZF_Z | ZF_PV)) | ZF_C | (z.a & (ZF_F5 | ZF_F3))); return 4;  // SCF
        case 0x3F: {                                                   // CCF
            const bool carry = (z.f & ZF_C) != 0;
            z.f = uint8_t((z.f & (ZF_S | ZF_Z | ZF_PV)) | (carry ? ZF_H : 0)
                          | (carry ? 0 : ZF_C) | (z.a & (ZF_F5 | ZF_F3)));
            return 4;
        }
        case 0x10: {                                                   // DJNZ
            const int8_t d = int8_t(x.fetch8());
            if (--z.b) { z.pc = uint16_t(z.pc + d); return 13; }
            return 8;
        }
        case 0x18: { const int8_t d = int8_t(x.fetch8()); z.pc = uint16_t(z.pc + d); return 12; }  // JR
        case 0xC3: z.pc = x.fetch16(); return 10;                      // JP nn
        case 0xE9: z.pc = uint16_t((z.h << 8) | z.l); return 4;        // JP (HL)
        /* ⚡ LD SP,HL — THE STACK POINTER FROM A COMPUTED ADDRESS.
         *
         * This was missing outright, and it is not an exotic opcode: it is how code
         * sets the stack when the address is worked out at run time instead of being
         * assembled in. The immediate form `ld sp,nn` (0x31) was always here, which
         * is why no sound driver ever noticed -- every one of them opens with
         * `ld sp,0x1000`.
         *
         * ⛔ AND THE INDEXED FORM WAS ALREADY PRESENT: `LD SP,IX` (DD F9) is handled
         * in exec_index below. Only the base form was absent -- an oversight, not a
         * decision. Found by sweeping all 1280 opcode slots against a reference core,
         * which is a thing running one driver and declaring victory cannot do. */
        case 0xF9: z.sp = uint16_t((z.h << 8) | z.l); return 6;        // LD SP,HL
        case 0xCD: { const uint16_t nn = x.fetch16(); x.push16(z.pc); z.pc = nn; return 17; }
        case 0xC9: z.pc = x.pop16(); return 10;                        // RET
        case 0xF3: z.iff1 = z.iff2 = false; return 4;                  // DI
        case 0xFB: z.iff1 = z.iff2 = true; return 4;                   // EI
        case 0xD3: { const uint8_t p = x.fetch8(); z80_out(m, p, z.a); return 11; }   // OUT (n),A
        case 0xDB: { const uint8_t p = x.fetch8(); z.a = z80_in(m, p); return 11; }   // IN A,(n)
        case 0xCB: return exec_cb(x);
        case 0xED: return exec_ed(x);
        case 0xDD: case 0xFD: return exec_index(x, op);
        default: break;
    }

    /* JR cc,d  (0x20 / 0x28 / 0x30 / 0x38) */
    if ((op & 0xE7) == 0x20) {
        const int8_t d = int8_t(x.fetch8());
        if (cc(z, (op >> 3) & 3)) { z.pc = uint16_t(z.pc + d); return 12; }
        return 7;
    }
    /* LD rr,nn / ADD HL,rr / INC rr / DEC rr */
    if ((op & 0xCF) == 0x01) { set_rp(z, (op >> 4) & 3, x.fetch16()); return 10; }
    if ((op & 0xCF) == 0x09) {
        const uint16_t hl = z_add16(z, uint16_t((z.h << 8) | z.l), rp(z, (op >> 4) & 3));
        z.h = uint8_t(hl >> 8); z.l = uint8_t(hl);
        return 11;
    }
    if ((op & 0xCF) == 0x03) { const unsigned i = (op >> 4) & 3; set_rp(z, i, uint16_t(rp(z, i) + 1)); return 6; }
    if ((op & 0xCF) == 0x0B) { const unsigned i = (op >> 4) & 3; set_rp(z, i, uint16_t(rp(z, i) - 1)); return 6; }
    /* INC r / DEC r / LD r,n */
    if ((op & 0xC7) == 0x04) { const unsigned r = (op >> 3) & 7; x.put_r(r, z_inc8(z, x.get_r(r))); return r == 6 ? 11u : 4u; }
    if ((op & 0xC7) == 0x05) { const unsigned r = (op >> 3) & 7; x.put_r(r, z_dec8(z, x.get_r(r))); return r == 6 ? 11u : 4u; }
    if ((op & 0xC7) == 0x06) { const unsigned r = (op >> 3) & 7; x.put_r(r, x.fetch8()); return r == 6 ? 10u : 7u; }
    /* RET cc / JP cc,nn / CALL cc,nn / PUSH / POP / RST / ALU A,n */
    if ((op & 0xC7) == 0xC0) { if (cc(z, (op >> 3) & 7)) { z.pc = x.pop16(); return 11; } return 5; }
    if ((op & 0xC7) == 0xC2) { const uint16_t nn = x.fetch16(); if (cc(z, (op >> 3) & 7)) z.pc = nn; return 10; }
    if ((op & 0xC7) == 0xC4) {
        const uint16_t nn = x.fetch16();
        if (cc(z, (op >> 3) & 7)) { x.push16(z.pc); z.pc = nn; return 17; }
        return 10;
    }
    if ((op & 0xCF) == 0xC5) {                                          // PUSH rr (AF at index 3)
        const unsigned i = (op >> 4) & 3;
        x.push16(i == 3 ? uint16_t((z.a << 8) | z.f) : rp(z, i));
        return 11;
    }
    if ((op & 0xCF) == 0xC1) {                                          // POP rr
        const unsigned i = (op >> 4) & 3;
        const uint16_t v = x.pop16();
        if (i == 3) { z.a = uint8_t(v >> 8); z.f = uint8_t(v); }
        else set_rp(z, i, v);
        return 10;
    }
    if ((op & 0xC7) == 0xC7) { x.push16(z.pc); z.pc = uint16_t(op & 0x38); return 11; }   // RST
    if ((op & 0xC7) == 0xC6) {                                          // ALU A,n
        const uint8_t v = x.fetch8();
        switch ((op >> 3) & 7) {
            case 0: z_add8(z, v, 0); break;
            case 1: z_add8(z, v, uint8_t(z.f & ZF_C)); break;
            case 2: z_sub8(z, v, 0); break;
            case 3: z_sub8(z, v, uint8_t(z.f & ZF_C)); break;
            case 4: z_and(z, v); break;
            case 5: z_xor(z, v); break;
            case 6: z_or(z, v); break;
            default: z_cp8(z, v); break;
        }
        return 7;
    }

    z.trapped = true; z.trap_pc = pc0; z.trap_opcode = op; z.trap_prefix = 0;
    return 0;
}

}  // namespace

/* --- the seam -------------------------------------------------------------- */

void io_action_write(Machine& m, uint32_t address, uint8_t value) {
    if (address == kZ80ResetRegister || address == kZ80NmiRegister ||
        address == kZ80CommRegister) {
        z80_control_write(m, address, value);
    } else if (address == kDacLeftRegister || address == kDacRightRegister) {
        m.apu.write_dac(value, address == kDacLeftRegister);
    }
}

void z80_control_write(Machine& m, uint32_t address, uint8_t value) {
    Z80& z = m.z80;
    if (address == kZ80ResetRegister) {
        const bool release = (value == kZ80ReleaseValue);
        if (release && !z.running) {
            z.reset();
            z.running = true;
        } else if (!release) {
            z.running = false;
        }
        return;
    }
    if (address == kZ80NmiRegister) {
        /* "NMI request generated once to the Z80 with write access (data
         * invalid)" -- SNK, K1SoundSim. The byte written means nothing. */
        z.nmi_pending = true;
        return;
    }
    /* 0xBC is the dual-port comm register: the main CPU's write IS the Z80's
     * memory at 0x8000. Nothing to do -- they are the same byte. */
}

void z80_tick(Machine& m, uint32_t main_cycles) {
    Z80& z = m.z80;
    if (!z.running || z.trapped) return;

    /* 3.072 MHz against 6.144: one Z80 cycle for every two of the main CPU's.
     *
     * The credit is booked in MAIN-clock units and is allowed to go NEGATIVE. That
     * sign matters. z80_tick is called once per main-CPU instruction, so it hands
     * over ~5 Z80 cycles at a time -- less than most Z80 instructions cost. An
     * unsigned budget clamped at zero therefore THREW THE OVERSPEND AWAY and gave
     * the Z80 a free instruction per call whatever its price; a 13-cycle DJNZ was
     * billed like a 4-cycle NOP.
     *
     * Silicon caught it. hw_calibration row 11 counts a 3364-T-state loop for two
     * seconds: the console runs it 1792 times (= 3.01 MHz, so the 3.072 MHz clock
     * is right), we ran it 9300 times. The Z80 -- and the music it drives -- was
     * 5x too fast. An instruction that outruns the credit is not free; it is
     * borrowed against the next tick. */
    z.cycle_credit += int32_t(main_cycles);

    while (z.cycle_credit >= int32_t(kZ80ClockDivider)) {   /* at least one Z80 cycle */
        unsigned cost;

        if (z.nmi_pending) {
            /* An NMI cannot be masked, and it WAKES a halted CPU -- which is the
             * whole point here: the driver parks on HALT and the main CPU pokes
             * 0xBA to hand it work. */
            z.nmi_pending = false;
            z.halted = false;
            z.iff2 = z.iff1;
            z.iff1 = false;
            z80_write(m, --z.sp, uint8_t(z.pc >> 8));
            z80_write(m, --z.sp, uint8_t(z.pc));
            z.pc = 0x0066;
            cost = 11;
        } else if (z.int_pending && z.iff1) {
            /* The maskable interrupt: timer 3 of the MAIN cpu drives it (SNK,
             * 8Bit.txt). Unlike the NMI it can be masked, and the driver does mask
             * it while it touches shared state.
             *
             * ⚠️ The line is NOT cleared here. It is a LEVEL, and only an I/O write
             * releases it (K1SoundSim § 5.2.4 -- see z80_out). Clearing it on
             * acceptance would model a pulse, and would silently forgive a driver
             * that never acknowledges. */
            z.halted = false;
            z.iff1 = z.iff2 = false;
            z80_write(m, --z.sp, uint8_t(z.pc >> 8));
            z80_write(m, --z.sp, uint8_t(z.pc));
            if (z.im == 2) {
                /* Mode 2: the vector is a table lookup through I. The peripheral
                 * puts a byte on the bus; with nothing driving it, that is 0xFF. */
                const uint16_t slot = uint16_t((z.i << 8) | 0xFF);
                z.pc = uint16_t(z80_read(m, slot) | (z80_read(m, uint16_t(slot + 1)) << 8));
            } else {
                z.pc = 0x0038;                 /* modes 0 and 1 both land here */
            }
            cost = 13;
        } else {
            cost = exec_one(m, z);
            if (z.trapped) return;           // LOUD: the run stops here
            ++z.executed;
        }

        z.cycle_credit -= int32_t(cost) * int32_t(kZ80ClockDivider);
    }
}

}  // namespace ngpc
