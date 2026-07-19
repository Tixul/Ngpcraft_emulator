/* mem_family.cpp — the TLCS-900 memory-operand instruction families.
 *
 * This is the bulk of the ISA: first opcode byte >= 0x80 splits into
 *
 *     zz  = (b & 0x30) >> 4                 0=byte 1=word 2=long 3=DESTINATION
 *     mem = ((b & 0x40) >> 2) | (b & 0x0F)  0..21 = memory modes, >=23 = register-direct
 *
 * and the byte after the addressing mode's operands is the SUB-OPCODE.
 *
 * EVERYTHING HERE IS SOURCED, NOT INFERRED. See specs/TLCS900_MEMORY_FAMILY.md:
 *   - encodings   -> asm900.exe, the OFFICIAL Toshiba assembler (round-trip)
 *   - cycles      -> Toshiba TLCS-900/L1 instruction lists (4)/(5)/(10)
 *   - flags       -> the datasheet's per-instruction SYMBOL rows
 *
 * The datasheet is the ONLY reference here. Cycle figures that circulate in the
 * wider NGP scene disagree with it on JR (8/4 vs 5/2), on MUL/DIV (18/26 vs
 * 13/16), and on nearly every addressing-mode adder; where they conflict, the
 * Toshiba list wins. Do not "fix" a cycle count to match a figure found
 * elsewhere without a datasheet row, or a calibration-ROM measurement, behind it.
 *
 * CYCLE MODEL — the part our Python reference gets wrong:
 *     cycles = base(instruction) + extra(addressing mode)
 * The Python core applies NO addressing-mode adder at all, so every memory-op
 * cycle count it reports is too low. We implement the datasheet model, which
 * makes this core deliberately MORE accurate than the reference. The harness
 * verifies the identity `cpp == py + extra(mode)` so that a real cycle bug is
 * still caught rather than waved through.
 */
#include "machine.hpp"

#include <cstring>

namespace ngpc {

/* ---------------------------------------------------------------- flags -- */
constexpr uint8_t F_C = 0x01, F_N = 0x02, F_V = 0x04, F_H = 0x10, F_Z = 0x40, F_S = 0x80;

bool alu_even_parity(uint32_t v, uint8_t bits);
bool alu_even_parity(uint32_t v, uint8_t bits) {
    unsigned n = 0;
    for (uint8_t i = 0; i < bits; ++i) n += (v >> i) & 1u;
    return (n & 1u) == 0u;   // even population count -> V = 1
}

static inline uint32_t size_mask(uint8_t sz)  { return sz == 0 ? 0xFFu : sz == 1 ? 0xFFFFu : 0xFFFFFFFFu; }
static inline uint32_t sign_bit(uint8_t sz)   { return sz == 0 ? 0x80u : sz == 1 ? 0x8000u : 0x80000000u; }
static inline uint8_t  size_bytes(uint8_t sz) { return sz == 0 ? 1 : sz == 1 ? 2 : 4; }

/* ADD/ADC. Datasheet: S Z H V N=0 C all written -- except in LONG size, where H
 * is left alone (there is no 32-bit half-carry). */
/* l and r are taken as 64-bit ON PURPOSE. ADC passes `right + carry`, which in
 * LONG size can be 0xFFFFFFFF + 1 -- that wraps to 0 in 32-bit arithmetic and
 * silently loses the carry. The reference computes it in arbitrary precision;
 * 64 bits is enough to reproduce it exactly. */
uint8_t alu_add_flags(uint8_t sz, uint64_t l, uint64_t r, uint32_t res) {
    const uint32_t mask = size_mask(sz), sb = sign_bit(sz);
    uint8_t out = 0;
    if (res & sb) out |= F_S;
    if ((res & mask) == 0) out |= F_Z;
    if ((~(l ^ r)) & (l ^ uint64_t(res)) & uint64_t(sb)) out |= F_V;
    if ((l ^ r ^ uint64_t(res)) & 0x10u) out |= F_H;
    if ((l + r) > uint64_t(mask)) out |= F_C;
    return out;                                  // N = 0
}

/* SUB/SBC/CP. N = 1. */
uint8_t alu_sub_flags(uint8_t sz, uint64_t l, uint64_t r, uint32_t res) {
    const uint32_t mask = size_mask(sz), sb = sign_bit(sz);
    uint8_t out = F_N;
    if (res & sb) out |= F_S;
    if ((res & mask) == 0) out |= F_Z;
    if ((l ^ r) & (l ^ uint64_t(res)) & uint64_t(sb)) out |= F_V;
    if ((l ^ r ^ uint64_t(res)) & 0x10u) out |= F_H;
    if (l < r) out |= F_C;   // borrow, on RAW operands
    return out;
}

/* AND/OR/XOR. V is the PARITY flag (even population -> 1); H is SET by AND and
 * CLEARED by OR/XOR; C and N are cleared. In LONG size there is no 32-bit
 * parity, so V is left untouched. (Both rules were corrected in the Python core
 * on 2026-07-09 by triage against hardware behaviour, and the datasheet agrees.) */
uint8_t alu_logic_flags(uint8_t sz, uint32_t res, bool is_and) {
    const uint32_t mask = size_mask(sz), sb = sign_bit(sz);
    uint8_t out = 0;
    if (res & sb) out |= F_S;
    if ((res & mask) == 0) out |= F_Z;
    /* Toshiba symbol rows are SIZE-INDEPENDENT: AND = `* * 1 P 0 0`,
     * OR/XOR = `* * 0 P 0 0`. So V is the parity of the result at EVERY size
     * (including 32-bit), and H is set by AND / cleared by OR-XOR at every size.
     * ⚠️ The common shortcut is to skip parity in long mode and leave V alone.
     * The symbol rows do not license that, so we compute it at all three sizes. */
    const uint8_t bits = sz == 0 ? 8 : sz == 1 ? 16 : 32;
    if (alu_even_parity(res & mask, bits)) out |= F_V;
    if (is_and) out |= F_H;
    return out;                                  // C = 0, N = 0
}

/* ------------------------------------------------- effective address ------ */
struct Ea {
    bool     ok    = false;
    uint32_t addr  = 0;    // masked to the 24-bit bus -- for MEMORY ACCESS
    uint32_t raw   = 0;    // unmasked 32-bit result of the address expression
    uint8_t  len   = 0;    // bytes consumed INCLUDING the first opcode byte, EXCLUDING the sub-op
    uint8_t  extra = 0;    // Toshiba addressing-mode cycle adder
    /* The register written back by pre-dec / post-inc. Held as an EXTENDED
     * REGISTER CODE, not a bare 3-bit number: the secondary byte names its base
     * register the same way the C7/D7/E7 escapes do, so a banked register has no
     * 3-bit number at all. -1 = no write-back. */
    int      wb_rcode = -1;
    uint32_t wb_val = 0;
};

/* The base register of a secondary-byte addressing mode.
 *
 * ⚠️ The byte is `rrrrrrmm`: the top six bits are an EXTENDED REGISTER CODE (with
 * its byte-position field zeroed) and the low two carry the mode -- the d16 flag,
 * or the pre-dec/post-inc step. It is NOT a bare 3-bit register number.
 *
 * `(data >> 2) & 7` happens to give the right answer for a CURRENT-BANK code
 * (0xE0..0xFF, where rcode = 0xE0 + reg*4 + pos), which is all a compiler
 * normally emits -- so the wrong decode survived every ROM in the corpus but one.
 * Densha de Go! 2 walks its object list through a BANKED register (`E3 14 21` =
 * `ld XBC,(XBC1)`, rcode 0x14 = bank 1's BC): the bare decode read `(XIY)`
 * instead, i.e. a ROM address instead of the list in RAM, and the dispatcher that
 * followed jumped through a garbage jump-table index. The official assembler
 * settles the encoding:
 *     ld XBC,(XIY+0x1234) -> e3 f5 34 12 21     (0xF5 = XIY's rcode | d16 flag)
 *     ld XBC,(XIY+)       -> e5 f6 21           (0xF6 = XIY's rcode | step 4)
 * -- the same map `rd_rcode()` already implements, and which the (r32+r8) branch
 * below was already fixed to use. It was fixed in ONE branch and left wrong in
 * the two beside it. */
static inline uint32_t ea_base_reg(const ngpc_cpu_t& c, uint8_t data) {
    return rd_rcode(c, uint8_t(data & 0xFC), 2);          // sz 2 = long
}

/* Toshiba instruction list (10) "Addressing mode":
 *   (R) +0 · (R+d8) +1 · (#8) +1 · (#16) +2 · (#24) +3
 *   (r) +1 · (r+d16) +3 · (r+r8) +3 · (r+r16) +3 · (-r) +1 · (r+) +1
 * ⚠️ A 0/2/2/2/3/5/8/3/3 adder set circulates in the scene; it is not what list
 * (10) says, and adopting it silently inflates every memory-op cycle count. */
static Ea decode_ea(const Machine& m, uint32_t pc, uint8_t mem) {
    Ea e;
    const ngpc_cpu_t& c = m.cpu;
    /* Every effective address is masked to the 24-bit address bus. The registers
     * are 32-bit, so a pointer with garbage in its top byte must still land in
     * the real address space (the reference core does this in _mask_address). */

    if (mem <= 7) {                                    // (R)
        e.ok = true; e.addr = c.regs[mem]; e.len = 1; e.extra = 0;
        return e;
    }
    if (mem <= 15) {                                   // (R + d8)
        const int8_t d = int8_t(m.read8(pc + 1));
        e.ok = true; e.addr = c.regs[mem - 8] + uint32_t(int32_t(d)); e.len = 2; e.extra = 1;
        return e;
    }
    if (mem == 16) {                                   // (#8)
        e.ok = true; e.addr = m.read8(pc + 1); e.len = 2; e.extra = 1;
        return e;
    }
    if (mem == 17) {                                   // (#16)
        e.ok = true;
        e.addr = uint32_t(m.read8(pc + 1)) | (uint32_t(m.read8(pc + 2)) << 8);
        e.len = 3; e.extra = 2;
        return e;
    }
    if (mem == 18) {                                   // (#24)
        e.ok = true;
        e.addr = uint32_t(m.read8(pc + 1)) | (uint32_t(m.read8(pc + 2)) << 8) |
                 (uint32_t(m.read8(pc + 3)) << 16);
        e.len = 4; e.extra = 3;
        return e;
    }
    if (mem == 19) {                                   // secondary byte
        const uint8_t data = m.read8(pc + 1);
        if (data == 0x03 || data == 0x07) {            // (r32 + r8) / (r32 + r16)
            /* Base and index are named with the EXTENDED REGISTER CODES, the same
             * ones the C7/D7/E7 escapes use -- not with a bare 3-bit register
             * number. Decoding them by hand here got `(XIX+A)` and `(XIX+W)` to
             * read the SAME byte and `(XIX+C)` to read A. The official assembler
             * settles it:
             *     ld (XIX+A),A  -> F3 03 F0 E0 41
             *     ld (XIX+W),A  -> F3 03 F0 E1 41      (E0 = A, E1 = W)
             *     ld (XIX+C),A  -> F3 03 F0 E4 41
             *     ld (XIX+WA),A -> F3 07 F0 E0 41
             * so this must go through rd_rcode(), which is the map that was
             * recovered from that assembler in the first place.
             *
             * The index is added as a SIGNED value (Toshiba, "Register indirect
             * with index"): a byte index reaches +-127 around the base. */
            const uint8_t bcode = m.read8(pc + 2);
            const uint8_t icode = m.read8(pc + 3);
            const uint32_t base = rd_rcode(c, bcode, 2);          // r32
            const int32_t index = (data == 0x03)
                ? int32_t(int8_t(uint8_t(rd_rcode(c, icode, 0))))
                : int32_t(int16_t(uint16_t(rd_rcode(c, icode, 1))));
            e.ok = true; e.addr = base + uint32_t(index); e.len = 4; e.extra = 3;
            return e;
        }
        if (data == 0x13) {                            // pc-relative (this is LDAR)
            const int16_t d = int16_t(uint16_t(m.read8(pc + 2)) | (uint16_t(m.read8(pc + 3)) << 8));
            e.ok = true; e.addr = (pc + 4) + uint32_t(int32_t(d)); e.len = 4; e.extra = 3;
            return e;
        }
        if ((data & 0x03) == 0x01) {                   // (r32 + d16)
            const int16_t d = int16_t(uint16_t(m.read8(pc + 2)) | (uint16_t(m.read8(pc + 3)) << 8));
            e.ok = true; e.addr = ea_base_reg(c, data) + uint32_t(int32_t(d)); e.len = 4; e.extra = 3;
            return e;
        }
        if ((data & 0x03) == 0x00) {                   // (r32)
            e.ok = true; e.addr = ea_base_reg(c, data); e.len = 2; e.extra = 1;
            return e;
        }
        /* `mm` = 2, or 3 on a code the special forms above do not name: Toshiba
         * defines no such mode. It TRAPS (§9) rather than being quietly read as
         * `(r32)`, which is what the old fallthrough did. */
        return e;                                      // e.ok == false
    }
    if (mem == 20 || mem == 21) {                      // (-r) pre-dec / (r+) post-inc
        const uint8_t data = m.read8(pc + 1);
        uint32_t step;
        switch (data & 0x03) {
            case 0: step = 1; break;
            case 1: step = 2; break;
            case 2: step = 4; break;
            /* `data & 3 == 3` is not a defined step. The tempting shortcut is to
             * fall through and silently reuse the PREVIOUS instruction's address;
             * that turns a bad encoding into a plausible wrong answer. We refuse:
             * an undefined encoding must trap, not guess. */
            default: return e;                          // e.ok == false
        }
        /* NOTE: the step comes from the OPERAND BYTE (1/2/4), *not* from the
         * instruction's zz size field. Getting this wrong corrupts every
         * stack-walking loop in a subtle, data-dependent way. */
        const uint32_t base = ea_base_reg(c, data);
        e.ok = true; e.len = 2; e.extra = 1; e.wb_rcode = int(uint8_t(data & 0xFC));
        if (mem == 20) { e.wb_val = base - step; e.addr = e.wb_val; }   // address = NEW value
        else           { e.addr = base; e.wb_val = base + step; }       // address = OLD value
        return e;
    }
    return e;   // mem >= 22: not a memory mode
}

static Ea decode_ea_masked(const Machine& m, uint32_t pc, uint8_t mem) {
    Ea e = decode_ea(m, pc, mem);
    /* A memory ACCESS goes out on the 24-bit address bus, so it is masked. But
     * `LDA` performs no access at all -- it just computes the address expression
     * and drops the result in a 32-bit register -- so it keeps the full value.
     * (Verified against the reference: `lda XWA,(XWA)` with XWA = 0xD82C07CD
     * yields 0xD82C07CD, not 0x002C07CD.) */
    e.raw = e.addr;
    e.addr &= kAddrMask;
    return e;
}

/* -------------------------------------------------------- memory access --- */
static uint32_t load_sized(const Machine& m, uint32_t a, uint8_t sz) {
    switch (sz) {
        case 0:  return m.read8(a);
        case 1:  return uint32_t(m.read8(a)) | (uint32_t(m.read8(a + 1)) << 8);
        default: return uint32_t(m.read8(a)) | (uint32_t(m.read8(a + 1)) << 8) |
                        (uint32_t(m.read8(a + 2)) << 16) | (uint32_t(m.read8(a + 3)) << 24);
    }
}

void record_read(ngpc_record_t* rec, uint32_t addr, uint32_t value, uint8_t sz);
void record_read(ngpc_record_t* rec, uint32_t addr, uint32_t value, uint8_t sz) {
    if (!rec || rec->n_reads >= NGPC_MAX_ACCESS) return;
    ngpc_access_t& a = rec->reads[rec->n_reads++];
    a.address = addr;
    a.size = size_bytes(sz);
    a.discarded = 0;
    for (uint8_t i = 0; i < a.size; ++i) a.data[i] = uint8_t(value >> (8 * i));
}

/* --------------------------------------------------- register accessors --- */
static inline uint32_t get_reg_sized(const ngpc_cpu_t& c, unsigned r, uint8_t sz) {
    switch (sz) {
        case 0:  return uint8_t(c.regs[r >> 1] >> ((r & 1) ? 0 : 8));   // R8 = W,A,B,C,D,E,H,L
        case 1:  return uint16_t(c.regs[r]);
        default: return c.regs[r];
    }
}
static inline void set_reg_sized(ngpc_cpu_t& c, unsigned r, uint8_t sz, uint32_t v) {
    switch (sz) {
        case 0: {
            const unsigned sh = (r & 1) ? 0 : 8;
            uint32_t& x = c.regs[r >> 1];
            x = (x & ~(uint32_t(0xFF) << sh)) | ((v & 0xFF) << sh);
            break;
        }
        case 1:  c.regs[r] = (c.regs[r] & 0xFFFF0000u) | (v & 0xFFFF); break;
        default: c.regs[r] = v; break;
    }
}

/* =============================== SOURCE group (zz = 0/1/2) ================ */
static bool exec_source(Machine& m, ngpc_record_t* rec, uint32_t pc,
                        uint8_t sz, const Ea& e, uint8_t& out_len, uint16_t& out_cycles) {
    ngpc_cpu_t& c = m.cpu;
    const uint8_t sub = m.read8(pc + e.len);
    const unsigned r  = sub & 0x07;
    const uint8_t  nb = size_bytes(sz);

    uint8_t len = uint8_t(e.len + 1);
    uint16_t base = 0;

    auto commit_wb = [&]() {
        if (e.wb_rcode >= 0) wr_rcode(c, uint8_t(e.wb_rcode), 2, e.wb_val);   // sz 2 = long
    };

    /* --- block instructions -------------------------------------------------
     *   0x10 LDI   0x11 LDIR   0x12 LDD   0x13 LDDR
     *   0x14 CPI   0x15 CPIR   0x16 CPD   0x17 CPDR
     * BYTE and WORD only ("BW-"). Toshiba list (3):
     *   LDI / LDD    state 8            LDIR / LDDR   state **7n + 1**
     *   CPI / CPD    state 6. 6. -      CPIR / CPDR   state **6n + 1**
     * -- NOT a flat multiple of the single form. (The reference bills 8n and 6n.)
     *
     * The transfer pair is (XDE) <- (mem), except when the addressing register is
     * XIY (opcode low nibble == 5), where it becomes (XIX) <- (XIY). The compares
     * take A (byte) or WA (word) against the memory operand and write nothing.
     * The whole loop runs INSIDE the instruction: no interrupt mid-block.
     *
     * `ldir` is what stops 27 of the 66 commercial ROMs. */
    if (sub >= 0x10 && sub <= 0x17) {
        if (sz == 2) return false;                    // no long form
        const bool is_compare = sub >= 0x14;
        const bool repeats    = (sub & 0x01) != 0;
        const bool backwards  = (sub & 0x02) != 0;
        const int32_t step = backwards ? -int32_t(nb) : int32_t(nb);

        const uint8_t first = m.read8(pc);
        const uint8_t mm    = uint8_t(((first & 0x40) >> 2) | (first & 0x0F));
        if (mm > 7) return false;                     // block ops use the (R) mode
        const unsigned dst_reg = ((first & 0x0F) == 5) ? NGPC_XIX : NGPC_XDE;

        unsigned iterations = 0;
        do {
            const uint32_t src = c.regs[mm] & kAddrMask;
            const uint32_t v   = load_sized(m, src, sz);
            if (iterations == 0) record_read(rec, src, v, sz);

            if (is_compare) {
                /* The accumulator is A (byte) or WA (word). In the R8 file
                 * (W,A,B,C,D,E,H,L) **A is index 1** -- index 0 is W. Comparing
                 * against W instead of A is a silent, data-dependent wrong
                 * answer, which is exactly what the gate caught. */
                const uint32_t a   = (sz == 0) ? get_reg_sized(c, 1, 0)
                                               : get_reg_sized(c, 0, 1);
                const uint32_t res = uint32_t((uint64_t(a) - v) & size_mask(sz));
                const uint8_t keep_c = uint8_t(c.flags & F_C);
                c.flags = alu_sub_flags(sz, a, v, res);
                /* C is NOT touched by the block compares (symbol row `* * * * 1 -`). */
                c.flags = uint8_t((c.flags & ~F_C) | keep_c);
            } else {
                store(m, rec, c.regs[dst_reg] & kAddrMask, v, nb);
                /* ⚠️ THE POINTER IS A 32-BIT REGISTER. THE BUS IS 24 BITS. NOT THE
                 * SAME THING, AND THE DIFFERENCE IS A REGISTER BYTE THAT BELONGS TO
                 * THE PROGRAM.
                 *
                 * This used to mask the WRITE-BACK to 24 bits, on the reasoning that
                 * "the walking pointers are addresses, so the top byte cannot
                 * accumulate garbage". The top byte is not garbage: it is the
                 * register's, and an NGPC address only needs 24 of its 32 bits, so
                 * software is free to keep something there. POCKET TENNIS COLOR keeps
                 * its LOOP COUNTER there -- pointer in the low 24 bits, count in the
                 * top byte, ended by `djnz QH` (`C7 EF 1C`, and the official assembler
                 * confirms QH is exactly XHL's top byte). Our `ldir` wiped the counter
                 * on every pass, the `djnz` never reached zero, and the game spun
                 * forever on a blank screen.
                 *
                 * The mask belongs on the ACCESS -- which is what the two `& kAddrMask`
                 * above it are for -- never on the register. Same distinction this file
                 * already draws for LDA, which computes an address and keeps all 32
                 * bits because it performs no access at all. */
                c.regs[dst_reg] = uint32_t(int32_t(c.regs[dst_reg]) + step);
            }
            c.regs[mm] = uint32_t(int32_t(c.regs[mm]) + step);

            /* BC is the 16-bit counter; its high half is untouched. */
            c.regs[NGPC_XBC] = (c.regs[NGPC_XBC] & 0xFFFF0000u) |
                               ((uint16_t(c.regs[NGPC_XBC]) - 1) & 0xFFFF);
            ++iterations;
            /* The repeating COMPARES are a SEARCH: they stop on a match (Z = 1),
             * not only when BC runs out. `cpir` that ignores the match would scan
             * the whole block every time -- right answer for BC, wrong pointers,
             * wrong cycles, and a wrong "found it" position. */
            if (repeats && is_compare && (c.flags & F_Z)) break;
        } while (repeats && uint16_t(c.regs[NGPC_XBC]) != 0);

        /* Toshiba, both groups: **V = 1 while BC != 0 after execution, 0 once it
         * reaches 0** -- it is the "more to copy" flag, not a constant.
         *   LDI/LDD/LDIR/LDDR : `- - 0 * 0 -`  (S, Z, C untouched; H = 0; N = 0)
         *   CPI/CPD/CPIR/CPDR : `* * * * 1 -`  (S, Z, H from the subtract;
         *                                        N = 1; **C untouched**)          */
        const bool more = uint16_t(c.regs[NGPC_XBC]) != 0;
        if (!is_compare) c.flags = uint8_t(c.flags & ~(F_H | F_N));
        c.flags = uint8_t((c.flags & ~F_V) | (more ? F_V : 0));
        if (is_compare) c.flags = uint8_t(c.flags | F_N);

        out_len = uint8_t(e.len + 1);
        /* LDIR/LDDR cost per byte. Toshiba datasheet says 7n+1, but the datasheet MUL/DIV
         * figures already proved to be FLOORS (v2 silicon), and 7 leaves Cool Boarders (big
         * per-frame RAM ldir) at ~51fps vs its silicon 30. m_ldir_cost lets us test higher
         * values: 14 puts Cool Boarders at 30fps AND leaves Fatal Fury at 60 -- one fix, both
         * games. Default 7 (datasheet); the shell can raise it, pending final confirmation. */
        const uint16_t per = (is_compare ? 6 : m.ldir_cost);
        out_cycles = uint16_t(
            (repeats ? per * iterations + 1
                     : (is_compare ? 6 : 8)) + e.extra);
        return true;
    }

    /* MUL / MULS / DIV / DIVS  RR, (mem)   (sub 0x40..0x5F).
     * The destination RR is named by the sub-opcode's low 3 bits and is
     * DOUBLE-WIDTH; the memory operand is the multiplier / divisor.
     * Toshiba list (4):  MUL 13.16.-  MULS 11.14.-  DIV 16.24.-  DIVS 19.27.-
     * Flags: MUL none at all; DIV writes V only (see reg_family.cpp).
     * `c5 ec 45` (`mul IY, (XHL+)`) stops 11 of the 66 commercial ROMs. */
    if (sub >= 0x40 && sub <= 0x5F) {
        if (sz == 2) return false;                  // no long form
        const unsigned kind = (sub - 0x40) >> 3;    // 0 MUL · 1 MULS · 2 DIV · 3 DIVS
        const bool is_div    = kind >= 2;
        const bool is_signed = (kind & 1) != 0;
        const uint8_t half   = (sz == 0) ? 8 : 16;
        const uint32_t hmask = (sz == 0) ? 0xFFu : 0xFFFFu;
        const uint32_t wmask = (sz == 0) ? 0xFFFFu : 0xFFFFFFFFu;

        /* Same `RR` code table as the register form, and the same trap: it is not
         * an array index, and it means different things at different sizes.
         * Toshiba, <Divide> Note 3 -- "RR of the DIV RR,r AND DIV RR,(mem)":
         *      word op -> a LONG register, codes 000..111 = XWA..XSP
         *      byte op -> a WORD register, and ONLY the odd codes exist:
         *                 001 = WA, 011 = BC, 101 = DE, 111 = HL
         * so at byte size code 001 is WA (the low word of XWA), not XBC. */
        if (sz == 0 && (r & 1) == 0) return false;  // no such word register
        const unsigned rr = (sz == 0) ? (r >> 1) : r;

        const uint32_t src = load_sized(m, e.addr, sz) & hmask;
        record_read(rec, e.addr, src, sz);
        commit_wb();

        uint32_t& dst = c.regs[rr];
        const uint32_t wide = dst & wmask;

        if (is_div) {
            /* Divide by zero and quotient overflow both set V and RUN ON. The
             * manual defines V for exactly those two cases (`- - - V - -`), which
             * means the program is meant to continue and branch on it -- stopping
             * here made the core lie. Full note in reg_family.cpp. */
            if (src == 0) {
                c.flags = uint8_t(c.flags | F_V);
                out_len = uint8_t(e.len + 1);
                out_cycles = uint16_t((is_signed ? (sz == 0 ? 19 : 27) : (sz == 0 ? 16 : 24)) + e.extra);
                return true;
            }
            uint32_t q, rem;
            if (is_signed) {
                const int32_t n = (sz == 0) ? int32_t(int16_t(uint16_t(wide))) : int32_t(wide);
                const int32_t d = (sz == 0) ? int32_t(int8_t(uint8_t(src)))
                                            : int32_t(int16_t(uint16_t(src)));
                q = uint32_t(n / d); rem = uint32_t(n % d);
            } else {
                q = wide / src; rem = wide % src;
            }
            /* THE OVERFLOW TEST IS SIGNED FOR DIVS. Toshiba: V = 1 when "the
             * quotient exceeds the numerals which can be expressed in bits of
             * dst" -- and for a SIGNED divide those numerals run -128..127
             * (byte) or -32768..32767 (word). Testing `q & ~hmask` treats the
             * quotient as unsigned, so a perfectly representable quotient of -1
             * (0xFFFFFFFF) looks like a wild overflow and V goes up on a divide
             * that never overflowed. */
            bool overflow;
            if (is_signed) {
                const int32_t qs = int32_t(q);
                const int32_t lo = (sz == 0) ? -128 : -32768;
                const int32_t hi = (sz == 0) ? 127 : 32767;
                overflow = (qs < lo) || (qs > hi);
            } else {
                overflow = (q & ~hmask) != 0;
            }
            const uint32_t packed = ((rem & hmask) << half) | (q & hmask);
            dst = (sz == 0) ? ((dst & 0xFFFF0000u) | packed) : packed;
            c.flags = uint8_t(overflow ? (c.flags | F_V) : (c.flags & ~F_V));
            out_cycles = uint16_t((is_signed ? (sz == 0 ? 19 : 27) : (sz == 0 ? 16 : 24)) + e.extra);
        } else {
            const uint32_t a = wide & hmask;
            uint32_t res;
            if (is_signed) {
                const int32_t x = (sz == 0) ? int32_t(int8_t(uint8_t(a))) : int32_t(int16_t(uint16_t(a)));
                const int32_t y = (sz == 0) ? int32_t(int8_t(uint8_t(src))) : int32_t(int16_t(uint16_t(src)));
                res = uint32_t(x * y);
            } else {
                res = a * src;
            }
            dst = (sz == 0) ? ((dst & 0xFFFF0000u) | (res & 0xFFFFu)) : res;
            out_cycles = uint16_t((is_signed ? (sz == 0 ? 11 : 14) : (sz == 0 ? 13 : 16)) + e.extra);
        }
        out_len = uint8_t(e.len + 1);
        return true;
    }

    /* RLC / RRC / RL / RR / SLA / SRA / SLL / SRL  (mem)   (sub 0x78..0x7F).
     * One shift, on a memory operand. Byte and word only; state 6. 6. -.
     * Flags are the rotate/shift row `* * 0 P 0 *` -- V is the PARITY. */
    if (sub >= 0x78 && sub <= 0x7F) {
        if (sz == 2) return false;
        const uint32_t mask = size_mask(sz), sb = sign_bit(sz);
        const uint8_t bits = (sz == 0) ? 8 : 16;
        const uint32_t v0 = load_sized(m, e.addr, sz) & mask;
        record_read(rec, e.addr, v0, sz);
        commit_wb();

        const bool cy = (c.flags & F_C) != 0;
        uint32_t v = v0;
        bool out;
        switch (sub & 0x07) {
            case 0: out = (v & sb) != 0; v = ((v << 1) | (out ? 1u : 0u)) & mask; break;   // RLC
            case 1: out = (v & 1u) != 0; v = ((v >> 1) | (out ? sb : 0u)) & mask; break;   // RRC
            case 2: out = (v & sb) != 0; v = ((v << 1) | (cy ? 1u : 0u)) & mask; break;    // RL
            case 3: out = (v & 1u) != 0; v = ((v >> 1) | (cy ? sb : 0u)) & mask; break;    // RR
            case 4: out = (v & sb) != 0; v = (v << 1) & mask; break;                       // SLA
            case 5: out = (v & 1u) != 0; v = ((v >> 1) | (v & sb)) & mask; break;          // SRA
            case 6: out = (v & sb) != 0; v = (v << 1) & mask; break;                       // SLL
            default: out = (v & 1u) != 0; v = (v >> 1) & mask; break;                      // SRL
        }
        uint8_t f = 0;
        if (v & sb) f |= F_S;
        if (v == 0) f |= F_Z;
        if (alu_even_parity(v, bits)) f |= F_V;
        if (out) f |= F_C;
        c.flags = f;                                 // H = 0, N = 0
        store(m, rec, e.addr, v, size_bytes(sz));
        out_len = uint8_t(e.len + 1);
        out_cycles = uint16_t(6 + e.extra);
        return true;
    }

    /* PUSH (mem)  (sub 0x04).  Byte and word only; state **6. 6. -**.
     * (A figure of 7 circulates for this; the datasheet says 6, and so does our
     * own reference. 6 it is.)
     * `d2 a0 27 ff 04` (`pushw (0xFF27A0)`) stops 23 of the 66 commercial ROMs. */
    if (sub == 0x04) {
        if (sz == 2) return false;
        const uint32_t v = load_sized(m, e.addr, sz);
        record_read(rec, e.addr, v, sz);
        commit_wb();
        c.regs[NGPC_XSP] -= nb;
        store(m, rec, c.regs[NGPC_XSP], v, nb);
        out_len = uint8_t(e.len + 1);
        out_cycles = uint16_t(6 + e.extra);
        return true;
    }

    /* LD (nn),(mem)   (sub 0x19) -- memory-to-memory, the destination given as a
     * 16-bit absolute. `80 + zz + mem : 19 : #16`, state 8, byte and word only. */
    if (sub == 0x19) {
        if (sz == 2) return false;
        const uint32_t dst = uint32_t(m.read8(pc + e.len + 1))
                           | (uint32_t(m.read8(pc + e.len + 2)) << 8);
        const uint32_t v = load_sized(m, e.addr, sz);
        record_read(rec, e.addr, v, sz);
        commit_wb();
        store(m, rec, dst, v, nb);
        out_len = uint8_t(e.len + 3);
        out_cycles = uint16_t(8 + e.extra);
        return true;
    }

    /* LD R,(mem) — datasheet state 4. 4. 6 */
    if (sub >= 0x20 && sub <= 0x27) {
        const uint32_t v = load_sized(m, e.addr, sz);
        record_read(rec, e.addr, v, sz);
        commit_wb();
        set_reg_sized(c, r, sz, v);
        base = (sz == 2) ? 6 : 4;
        out_len = len; out_cycles = uint16_t(base + e.extra);
        return true;
    }

    /* EX (mem),R — byte and word ONLY, no long form. */
    if (sub >= 0x30 && sub <= 0x37) {
        if (sz == 2) return false;
        const uint32_t mv = load_sized(m, e.addr, sz);
        const uint32_t rv = get_reg_sized(c, r, sz);
        record_read(rec, e.addr, mv, sz);
        commit_wb();
        store(m, rec, e.addr, rv, nb);
        set_reg_sized(c, r, sz, mv);
        base = 6;
        out_len = len; out_cycles = uint16_t(base + e.extra);
        return true;
    }

    /* ALU (mem),#imm — sub 0x38..0x3F. Byte and word ONLY (state "7. 8. -"). */
    if (sub >= 0x38 && sub <= 0x3F) {
        if (sz == 2) return false;
        uint32_t imm;
        if (sz == 0) { imm = m.read8(pc + e.len + 1); len = uint8_t(len + 1); }
        else         { imm = uint32_t(m.read8(pc + e.len + 1)) |
                             (uint32_t(m.read8(pc + e.len + 2)) << 8); len = uint8_t(len + 2); }

        const uint32_t mv = load_sized(m, e.addr, sz);
        record_read(rec, e.addr, mv, sz);
        commit_wb();

        const uint32_t mask = size_mask(sz);
        const bool carry = (c.flags & F_C) != 0;
        uint32_t res = 0;
        bool write_back = true;
        switch (sub) {
            case 0x38: res = (mv + imm) & mask;                 c.flags = alu_add_flags(sz, mv, imm, res); break;
            case 0x39: res = uint32_t((uint64_t(mv) + imm + carry) & mask); c.flags = alu_add_flags(sz, mv, uint64_t(imm) + carry, res); break;
            case 0x3A: res = (mv - imm) & mask;                 c.flags = alu_sub_flags(sz, mv, imm, res); break;
            case 0x3B: res = uint32_t((uint64_t(mv) - imm - carry) & mask); c.flags = alu_sub_flags(sz, mv, uint64_t(imm) + carry, res); break;
            case 0x3C: res = (mv & imm) & mask;                 c.flags = alu_logic_flags(sz, res, true);  break;
            case 0x3D: res = (mv ^ imm) & mask;                 c.flags = alu_logic_flags(sz, res, false); break;
            case 0x3E: res = (mv | imm) & mask;                 c.flags = alu_logic_flags(sz, res, false); break;
            default:   res = (mv - imm) & mask;                 c.flags = alu_sub_flags(sz, mv, imm, res);
                       write_back = false; break;               // 0x3F CP: no write-back
        }
        if (write_back) store(m, rec, e.addr, res, nb);
        base = (sub == 0x3F) ? (sz == 0 ? 5 : 6)     // CP (mem),#  = 5. 6. -
                             : (sz == 0 ? 7 : 8);    // ALU (mem),# = 7. 8. -
        out_len = len; out_cycles = uint16_t(base + e.extra);
        return true;
    }

    /* INC / DEC #3,(mem) — n == 0 means 8 (datasheet Note 1). C is NOT touched
     * (symbol row `* * * V 0 -` / `* * * V 1 -`).
     * BYTE and WORD ONLY: Toshiba list (4) gives `INC<W> #3,(mem)` the size
     * column "BW-" and the state "6. 6. -". There is no long form. (Our Python
     * reference executes one anyway, billing its flat 8-cycle fallback — a
     * reference defect, reported, not copied.) */
    if (sub >= 0x60 && sub <= 0x6F) {
        if (sz == 2) return false;
        const bool is_dec = sub >= 0x68;
        uint32_t n = sub & 0x07;
        if (n == 0) n = 8;

        const uint32_t mv = load_sized(m, e.addr, sz);
        record_read(rec, e.addr, mv, sz);
        commit_wb();

        const uint32_t mask = size_mask(sz);
        const uint8_t keep_c = uint8_t(c.flags & F_C);
        const uint32_t res = (is_dec ? (mv - n) : (mv + n)) & mask;
        c.flags = is_dec ? alu_sub_flags(sz, mv, n, res)
                         : alu_add_flags(sz, mv, n, res);
        c.flags = uint8_t((c.flags & ~F_C) | keep_c);   // carry preserved on INC/DEC (mem)
        store(m, rec, e.addr, res, nb);
        base = 6;
        out_len = len; out_cycles = uint16_t(base + e.extra);
        return true;
    }

    /* ALU R,(mem) and ALU (mem),R — sub 0x80..0xFF.
     * Datasheet:  R,(mem)  state 4. 4. 6      (mem),R  state 6. 6. 10
     *   ADD 80/88 · ADC 90/98 · SUB A0/A8 · SBC B0/B8
     *   AND C0/C8 · XOR D0/D8 · OR  E0/E8 · CP  F0/F8 */
    if (sub >= 0x80) {
        const unsigned family = (sub - 0x80) >> 4;   // 0=ADD 1=ADC 2=SUB 3=SBC 4=AND 5=XOR 6=OR 7=CP
        const bool to_memory  = (sub & 0x08) != 0;   // low half -> register dst, high half -> memory dst

        const uint32_t mv = load_sized(m, e.addr, sz);
        const uint32_t rv = get_reg_sized(c, r, sz);
        record_read(rec, e.addr, mv, sz);
        commit_wb();

        const uint32_t left  = to_memory ? mv : rv;   // destination operand
        const uint32_t right = to_memory ? rv : mv;
        const uint32_t mask  = size_mask(sz);
        const bool carry = (c.flags & F_C) != 0;

        uint32_t res = 0;
        bool write = true;
        switch (family) {
            case 0: res = (left + right) & mask;          c.flags = alu_add_flags(sz, left, right, res); break;
            case 1: res = uint32_t((uint64_t(left) + right + carry) & mask); c.flags = alu_add_flags(sz, left, uint64_t(right) + carry, res); break;
            case 2: res = (left - right) & mask;          c.flags = alu_sub_flags(sz, left, right, res); break;
            case 3: res = uint32_t((uint64_t(left) - right - carry) & mask); c.flags = alu_sub_flags(sz, left, uint64_t(right) + carry, res); break;
            case 4: res = (left & right) & mask;          c.flags = alu_logic_flags(sz, res, true);  break;
            case 5: res = (left ^ right) & mask;          c.flags = alu_logic_flags(sz, res, false); break;
            case 6: res = (left | right) & mask;          c.flags = alu_logic_flags(sz, res, false); break;
            default: res = (left - right) & mask;         c.flags = alu_sub_flags(sz, left, right, res);
                     write = false; break;                // CP writes nothing
        }
        if (write) {
            if (to_memory) store(m, rec, e.addr, res, nb);
            else           set_reg_sized(c, r, sz, res);
        }
        if (family == 7) base = (sz == 2) ? 6 : 4;   // CP: 4.4.6 BOTH ways (no write-back)
        else if (to_memory) base = (sz == 2) ? 10 : 6;
        else                base = (sz == 2) ? 6 : 4;
        out_len = len; out_cycles = uint16_t(base + e.extra);
        return true;
    }

    return false;   // not ported yet -> the caller traps
}

/* ============================ DESTINATION group (zz == 3) ================= */
static bool exec_dest(Machine& m, ngpc_record_t* rec, uint32_t pc,
                      const Ea& e, uint8_t& out_len, uint16_t& out_cycles, uint32_t& new_pc,
                      bool& jumped) {
    ngpc_cpu_t& c = m.cpu;
    const uint8_t sub = m.read8(pc + e.len);
    const unsigned r  = sub & 0x07;

    uint8_t len = uint8_t(e.len + 1);
    uint16_t base = 0;
    jumped = false;

    auto commit_wb = [&]() {
        if (e.wb_rcode >= 0) wr_rcode(c, uint8_t(e.wb_rcode), 2, e.wb_val);   // sz 2 = long
    };

    /* LD (mem),R  —  B0 + mem : 40 + zz + R.  Datasheet state 4. 4. 6.
     * THE STORES LIVE HERE, not at sub-op 0x30 of the source group (that is EX).
     *
     * ⚠️ `R` IS THREE BITS, so the sub-op runs 0x40..0x47 / 0x50..0x57 / 0x60..0x67.
     * The tests above ran `0x40..0x67` in one span, which also swallowed 0x48..0x4F,
     * 0x58..0x5F and 0x68..0x6F -- encodings that DO NOT EXIST, executed silently as
     * duplicate stores. The official assembler pins the real ones:
     *
     *      ld (XBC),A  ->  b1 41        ld (XBC),WA  ->  b1 50
     *      ld (XBC),XWA -> b1 60
     *
     * Same shape as the LDA range that swallowed the carry-bit ops. An encoding we
     * have not learned must TRAP, not quietly become its neighbour (§ 9). */
    if (sub >= 0x40 && sub <= 0x67 && (sub & 0x08) == 0) {
        const unsigned zz = (sub - 0x40) >> 4;      // 0x40 byte, 0x50 word, 0x60 long
        if (zz > 2) return false;
        commit_wb();
        store(m, rec, e.addr, get_reg_sized(c, r, uint8_t(zz)), size_bytes(uint8_t(zz)));
        base = (zz == 2) ? 6 : 4;
        out_len = len; out_cycles = uint16_t(base + e.extra);
        return true;
    }

    /* LD (mem),#8 (sub 0x00) and LD (mem),#16 (sub 0x02). */
    if (sub == 0x00 || sub == 0x02) {
        const uint8_t zz = (sub == 0x00) ? 0 : 1;
        uint32_t imm;
        if (zz == 0) { imm = m.read8(pc + e.len + 1); len = uint8_t(len + 1); base = 5; }
        else         { imm = uint32_t(m.read8(pc + e.len + 1)) |
                             (uint32_t(m.read8(pc + e.len + 2)) << 8); len = uint8_t(len + 2); base = 6; }
        commit_wb();
        store(m, rec, e.addr, imm, size_bytes(zz));
        out_len = len; out_cycles = uint16_t(base + e.extra);
        return true;
    }

    /* LD (mem),(#16)  —  B0 + mem : 14 + z : #16.  Flags: - - - - - -.  State 8. 8. -
     *
     * A memory-to-memory move: the source is a 16-bit ABSOLUTE address carried in
     * the instruction, the destination is the addressing-mode operand. The SNK
     * BIOS uses it in its interrupt handlers, which is why it stopped 54 of the
     * 73 ROMs the moment the interrupt controller started delivering.
     *
     *   ld  (0xB2),(0x6E85)  ->  F0 B2 14 85 6E
     *   ldw (0xB2),(0x6E85)  ->  F0 B2 16 85 6E
     *   ld  (XHL+16),(0x6E85) -> BB 10 14 85 6E
     *
     * (The mirror form, LD (#16),(mem), is sub-op 0x19 of the SOURCE group.) */
    if (sub == 0x14 || sub == 0x16) {
        const uint8_t zz = (sub == 0x14) ? 0 : 1;
        const uint32_t src = uint32_t(m.read8(pc + e.len + 1)) |
                             (uint32_t(m.read8(pc + e.len + 2)) << 8);
        len = uint8_t(len + 2);
        commit_wb();
        store(m, rec, e.addr, load_sized(m, src, size_bytes(zz)), size_bytes(zz));
        out_len = len; out_cycles = uint16_t(8 + e.extra);
        return true;
    }

    /* POP (mem)  (sub 0x04 byte / 0x06 word).  State 6. */
    if (sub == 0x04 || sub == 0x06) {
        const uint8_t zz = (sub == 0x04) ? 0 : 1;
        const uint8_t n = size_bytes(zz);
        uint32_t v = 0;
        for (uint8_t i = 0; i < n; ++i) v |= uint32_t(m.read8(c.regs[NGPC_XSP] + i)) << (8 * i);
        c.regs[NGPC_XSP] += n;
        commit_wb();
        store(m, rec, e.addr, v, n);
        out_len = uint8_t(e.len + 1);
        out_cycles = uint16_t(6 + e.extra);
        return true;
    }

    /* LDA R,mem — the address itself, not its contents. 0x20..0x27 word reg,
     * 0x30..0x37 long reg. With mode-19 sub-mode 0x13 this is LDAR.
     *
     * ⚠️ THE RANGE STOPS AT 0x27. It used to be one block, `0x20..0x37`, which
     * SWALLOWED 0x28..0x2C -- the A-indexed carry-bit operations -- and executed
     * them as a register load. Sonic runs `B1 2B`, and the OFFICIAL TOSHIBA
     * ASSEMBLER settles what that is:
     *
     *      ldcf A,(XBC)   ->  b1 2b        <- exactly the bytes the game runs
     *      lda  HL,(XBC)  ->  REJECTED     <- the encoding DOES NOT EXIST
     *
     * So the instruction we were executing was one the hardware does not have.
     * LDCF must touch NOTHING but the carry flag; we were writing the effective
     * address into XHL instead. The routine it ends returns a boolean in HL, so it
     * returned garbage, and the enemy it was about to spawn never appeared. THAT is
     * the "il manque les enemies" of the playtest.
     *
     * (A third-party emulator's PICTURE is what made the missing enemy visible. It
     * is a TRIAGE signal and never a verdict -- asm900 is the verdict.)
     *
     * The comment further down even claimed those opcodes were "NOT ported". They
     * were not merely unported -- they were being EXECUTED, silently and wrongly,
     * which is precisely the silent fallback HARDWARE_COMPAT_POLICY.md §9 forbids.
     * An unimplemented opcode must TRAP, not quietly do something else. */
    if ((sub >= 0x20 && sub <= 0x27) || (sub >= 0x30 && sub <= 0x37)) {
        const uint8_t zz = (sub < 0x30) ? 1 : 2;
        commit_wb();
        set_reg_sized(c, r, zz, e.raw);   // the ADDRESS, unmasked -- LDA reads nothing
        base = 4;
        out_len = len; out_cycles = uint16_t(base + e.extra);
        return true;
    }

    /* The A-INDEXED carry-bit operations (0x28..0x2C), byte operand only:
     *
     *   0x28 ANDCF A,(mem)    0x29 ORCF A,(mem)    0x2A XORCF A,(mem)
     *   0x2B LDCF  A,(mem)    0x2C STCF A,(mem)
     *
     * Identical to the `#3` forms below except that the BIT NUMBER comes from A
     * instead of from the sub-opcode. Only STCF writes memory; the rest touch the
     * carry flag and nothing else -- no register is written, which is the whole
     * point of the bug above. Base cycles from specs/TLCS900_MEMORY_FAMILY.md § 4. */
    if (sub >= 0x28 && sub <= 0x2C) {
        const unsigned bit = c.regs[NGPC_XWA] & 0x07;   // A = the low byte of WA
        const uint8_t mv = uint8_t(load_sized(m, e.addr, 0));
        record_read(rec, e.addr, mv, 0);
        commit_wb();

        const bool b = (mv >> bit) & 1u;
        const bool cy = (c.flags & F_C) != 0;
        switch (sub) {
            case 0x28: c.flags = uint8_t((c.flags & ~F_C) | ((cy && b) ? F_C : 0)); break;
            case 0x29: c.flags = uint8_t((c.flags & ~F_C) | ((cy || b) ? F_C : 0)); break;
            case 0x2A: c.flags = uint8_t((c.flags & ~F_C) | ((cy != b) ? F_C : 0)); break;
            case 0x2B: c.flags = uint8_t((c.flags & ~F_C) | (b ? F_C : 0)); break;
            default:   store(m, rec, e.addr,                                   // 0x2C STCF
                             uint8_t(cy ? (mv | (1u << bit)) : (mv & ~(1u << bit))), 1);
                       break;
        }
        out_len = uint8_t(e.len + 1);
        out_cycles = uint16_t(8 + e.extra);
        return true;
    }

    /* --- the BIT / CARRY-BIT operations on memory (byte operand only) --------
     *   0x80..0x87  ANDCF #3,(mem)     0x88..0x8F  ORCF  #3,(mem)
     *   0x90..0x97  XORCF #3,(mem)     0x98..0x9F  LDCF  #3,(mem)
     *   0xA0..0xA7  STCF  #3,(mem)     0xA8..0xAF  TSET  #3,(mem)
     *   0xB0..0xB7  RES   #3,(mem)     0xB8..0xBF  SET   #3,(mem)
     *   0xC0..0xC7  CHG   #3,(mem)     0xC8..0xCF  BIT   #3,(mem)
     * Toshiba list (6): `LDCF #3,(mem)` state **6**, `STCF #3,(mem)` state **7**,
     * both "6.-.-" -- BYTE only. The read-only ops cost 6, the read-modify-write
     * ops 7. `andcf` stops 10 of the 66 commercial ROMs.
     *
     * The A-indexed variants (sub 0x28..0x2C) are handled just above -- they used to
     * be claimed "NOT ported" here while in fact being executed as an LDA. Sonic uses
     * `ldcf A,(XBC)` every frame. */
    if (sub >= 0x80 && sub <= 0xCF) {
        const unsigned bit = sub & 0x07;
        const unsigned kind = (sub - 0x80) >> 3;   // 0..9
        const uint8_t mv = uint8_t(load_sized(m, e.addr, 0));
        record_read(rec, e.addr, mv, 0);
        commit_wb();

        const bool b = (mv >> bit) & 1u;
        const bool cy = (c.flags & F_C) != 0;
        uint8_t out = mv;
        bool writes = false;
        uint16_t base = 6;

        switch (kind) {
            case 0: c.flags = uint8_t((c.flags & ~F_C) | ((cy && b) ? F_C : 0)); break;   // ANDCF
            case 1: c.flags = uint8_t((c.flags & ~F_C) | ((cy || b) ? F_C : 0)); break;   // ORCF
            case 2: c.flags = uint8_t((c.flags & ~F_C) | ((cy != b) ? F_C : 0)); break;   // XORCF
            case 3: c.flags = uint8_t((c.flags & ~F_C) | (b ? F_C : 0)); break;           // LDCF
            case 4: out = uint8_t(cy ? (mv | (1u << bit)) : (mv & ~(1u << bit)));         // STCF
                    writes = true; base = 7; break;
            case 5: c.flags = uint8_t((c.flags & ~(F_Z | F_N)) | (b ? 0 : F_Z) | F_H);    // TSET
                    out = uint8_t(mv | (1u << bit)); writes = true; base = 7; break;
            case 6: out = uint8_t(mv & ~(1u << bit)); writes = true; base = 7; break;     // RES
            case 7: out = uint8_t(mv | (1u << bit));  writes = true; base = 7; break;     // SET
            case 8: out = uint8_t(mv ^ (1u << bit));  writes = true; base = 7; break;     // CHG
            default: c.flags = uint8_t((c.flags & ~(F_Z | F_N)) | (b ? 0 : F_Z) | F_H);   // BIT
                    break;
        }
        if (writes) store(m, rec, e.addr, out, 1);
        out_len = uint8_t(e.len + 1);
        out_cycles = uint16_t(base + e.extra);
        return true;
    }

    /* JP cc,mem — Toshiba instruction list (9): state 7/4 (T/F), + M. */
    if (sub >= 0xD0 && sub <= 0xDF) {
        const bool taken = eval_cc(c, sub & 0x0F);
        commit_wb();
        base = taken ? 7 : 4;
        out_len = len; out_cycles = uint16_t(base + e.extra);
        if (taken) { new_pc = e.addr; jumped = true; }
        return true;
    }

    /* CALL cc,mem — Toshiba instruction list (9): state 12/4 (T/F), + M. */
    if (sub >= 0xE0 && sub <= 0xEF) {
        const bool taken = eval_cc(c, sub & 0x0F);
        commit_wb();
        base = taken ? 12 : 4;
        out_len = len; out_cycles = uint16_t(base + e.extra);
        if (taken) {
            const uint32_t ret_addr = (pc + len) & kAddrMask;
            c.regs[NGPC_XSP] -= 4;
            store(m, rec, c.regs[NGPC_XSP], ret_addr, 4);
            new_pc = e.addr; jumped = true;
        }
        return true;
    }

    /* RET cc — pops PC and IGNORES the address entirely, but still consumed the
     * addressing-mode operand bytes and still pays their cycle adder. */
    if (sub >= 0xF0) {
        const bool taken = eval_cc(c, sub & 0x0F);
        commit_wb();
        base = taken ? 12 : 4;
        out_len = len; out_cycles = uint16_t(base + e.extra);
        if (taken) {
            new_pc = load_sized(m, c.regs[NGPC_XSP], 2);
            c.regs[NGPC_XSP] += 4;
            jumped = true;
        }
        return true;
    }

    return false;
}

/* ============================== entry point ============================== */
bool exec_mem_family(Machine& m, ngpc_record_t* rec, uint8_t op, uint32_t pc,
                     uint8_t& out_len, uint16_t& out_cycles, uint32_t& new_pc, bool& jumped);

bool exec_mem_family(Machine& m, ngpc_record_t* rec, uint8_t op, uint32_t pc,
                     uint8_t& out_len, uint16_t& out_cycles, uint32_t& new_pc, bool& jumped) {
    const uint8_t zz  = uint8_t((op & 0x30) >> 4);
    const uint8_t mem = uint8_t(((op & 0x40) >> 2) | (op & 0x0F));

    if (mem >= 22) return false;    // register-direct escape / invalid — not this family

    /* 0xF6 is invalid; 0xF7 is LDX; 0xF8..0xFF are SWI. The destination group
     * stops at 0xF5 -- treating 0xF0..0xFF as stores would swallow all of them. */
    if (op >= 0xF6) return false;

    const Ea e = decode_ea_masked(m, pc, mem);
    if (!e.ok) return false;

    jumped = false;
    if (zz == 3) return exec_dest(m, rec, pc, e, out_len, out_cycles, new_pc, jumped);
    return exec_source(m, rec, pc, zz, e, out_len, out_cycles);
}

}  // namespace ngpc
