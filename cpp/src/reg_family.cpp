/* reg_family.cpp — the TLCS-900 REGISTER-DIRECT instruction family.
 *
 * First byte `C8 + zz + r` (C8..CF byte, D8..DF word, E8..EF long): `r` is the
 * register from the first byte, and the SECOND byte is the sub-opcode, whose low
 * 3 bits are a second register `R`. Encoding map, straight from the Toshiba
 * instruction lists (the "Codes (16 hex)" column literally reads
 * `C8 + zz + r : 88 + R`, etc.):
 *
 *   0x03        LD  r, #          state 3. 4. 6
 *   0x04 / 0x05 PUSH r / POP r
 *   0x06 / 0x07 CPL r / NEG r     2. 2. -
 *   0x12 / 0x13 EXTZ r / EXTS r   -. 3. 3
 *   0x1C        DJNZ r, d8        6 (r != 0) / 4 (r == 0)
 *   0x60..0x6F  INC / DEC #3, r   (#3 == 0 means 8)
 *   0x70..0x7F  SCC cc, r
 *   0x80..0x87  ADD R, r          2. 2. 2      0x88..0x8F  LD R, r
 *   0x90..0x97  ADC R, r                       0x98..0x9F  LD r, R
 *   0xA0..0xA7  SUB R, r                       0xA8..0xAF  LD r, #3
 *   0xB0..0xB7  SBC R, r                       0xB8..0xBF  EX R, r    3. 3. -
 *   0xC0..0xC7  AND R, r                       0xC8..0xCF  ALU r, #   3. 4. 6
 *   0xD0..0xD7  XOR R, r                       0xD8..0xDF  CP r, #3
 *   0xE0..0xE7  OR  R, r                       0xE8..0xEF  shift r, #4
 *   0xF0..0xF7  CP  R, r                       0xF8..0xFF  shift r, A
 *
 * `EX R, r` (0xB8) is another hole in the Python reference: it declines the
 * encoding, while the datasheet gives it an entry AND a state (3. 3. -).
 */
#include "machine.hpp"

#include <cstring>

namespace ngpc {

/* shared with mem_family.cpp -- same datasheet-sourced flag rules */
uint8_t alu_add_flags(uint8_t sz, uint64_t l, uint64_t r, uint32_t res);
uint8_t alu_sub_flags(uint8_t sz, uint64_t l, uint64_t r, uint32_t res);
uint8_t alu_logic_flags(uint8_t sz, uint32_t res, bool is_and);
bool    alu_even_parity(uint32_t v, uint8_t bits);

static inline uint32_t rmask(uint8_t sz)  { return sz == 0 ? 0xFFu : sz == 1 ? 0xFFFFu : 0xFFFFFFFFu; }
static inline uint32_t rsign(uint8_t sz)  { return sz == 0 ? 0x80u : sz == 1 ? 0x8000u : 0x80000000u; }
static inline uint8_t  rbits(uint8_t sz)  { return sz == 0 ? 8 : sz == 1 ? 16 : 32; }

/* R8 = W,A,B,C,D,E,H,L -> code i lives in XREG[i>>1], HIGH byte when i is even. */
static inline uint32_t rd_reg(const ngpc_cpu_t& c, unsigned i, uint8_t sz) {
    switch (sz) {
        case 0:  return uint8_t(c.regs[i >> 1] >> ((i & 1) ? 0 : 8));
        case 1:  return uint16_t(c.regs[i]);
        default: return c.regs[i];
    }
}
static inline void wr_reg(ngpc_cpu_t& c, unsigned i, uint8_t sz, uint32_t v) {
    switch (sz) {
        case 0: {
            const unsigned sh = (i & 1) ? 0 : 8;
            uint32_t& x = c.regs[i >> 1];
            x = (x & ~(uint32_t(0xFF) << sh)) | ((v & 0xFF) << sh);
            break;
        }
        case 1:  c.regs[i] = (c.regs[i] & 0xFFFF0000u) | (v & 0xFFFF); break;
        default: c.regs[i] = v; break;
    }
}

constexpr uint8_t RF_C = 0x01, RF_N = 0x02, RF_V = 0x04, RF_H = 0x10, RF_Z = 0x40, RF_S = 0x80;

/* --- the EXTENDED register file (the C7 / D7 / E7 escapes) ------------------
 * `C7 <rcode> <sub-op>`: the second byte is a full 8-bit REGISTER CODE, which
 * can name registers the 3-bit field cannot -- the byte halves of IX/IY/IZ/SP,
 * the upper halves (Q-registers), and the registers of any BANK.
 *
 * The map was recovered with the OFFICIAL TOSHIBA ASSEMBLER (asm900_oracle),
 * because NEITHER ngdis NOR the Python reference can decode these forms -- both
 * emit `db 0xC7`. That is exactly what the assembler-as-oracle is for.
 *
 *   ld RW3,1 -> C7 31 A9      ld IXL,1 -> C7 F0 A9      ld QA,1  -> C7 E2 A9
 *   ld RA3,1 -> C7 30 A9      ld RC3,1 -> C7 34 A9      ldw RWA3,1 -> D7 30 A9
 *
 * so:  code >= 0xE0  -> CURRENT bank, xreg = (code>>2)&7, byte pos = code&3
 *      code >= 0xD0  -> PREVIOUS bank, xreg = (code>>2)&3, byte pos = code&3
 *      code <  0xD0  -> ABSOLUTE bank = code>>4, slot = code&0xF,
 *                       xreg = slot>>2, byte pos = slot&3
 * Only XWA..XHL are banked, which is why the absolute form has just 4 xregs.
 *
 * `C7 31 A9` (`ld RW3, 1`) stops 14 of the 66 commercial ROMs: RW3 is the
 * register the BIOS reads to pick a `swi 1` system-call vector. */
uint32_t* rcode_slot(ngpc_cpu_t& c, uint8_t code, unsigned& pos) {
    if (code >= 0xE0) {                       // current bank
        pos = code & 3;
        return &c.regs[(code >> 2) & 7];
    }
    unsigned bank, xreg;
    if (code >= 0xD0) {                       // previous bank
        bank = unsigned((c.rfp + 3) & 3);
        xreg = (code >> 2) & 3;
        pos  = code & 3;
    } else {                                  // absolute bank
        bank = code >> 4;
        const unsigned slot = code & 0x0F;
        xreg = slot >> 2;
        pos  = slot & 3;
    }
    if (bank > 3) bank = 3;
    /* The live window IS bank[rfp]; the backing store may be stale for it. */
    return (bank == c.rfp) ? &c.regs[xreg] : &c.banks[bank][xreg];
}

uint32_t rd_rcode(ngpc_cpu_t& c, uint8_t code, uint8_t sz) {
    unsigned pos = 0;
    const uint32_t v = *rcode_slot(c, code, pos);
    switch (sz) {
        case 0:  return (v >> (8 * pos)) & 0xFF;
        case 1:  return (v >> (8 * pos)) & 0xFFFF;   // pos is 0 (low) or 2 (Q-half)
        default: return v;
    }
}
/* Read-only overload: address computation (decode_ea) holds the CPU by const
 * reference and must still resolve a register code. Reading a register cannot
 * mutate the CPU, so the cast is safe and stays confined to this one line. */
uint32_t rd_rcode(const ngpc_cpu_t& c, uint8_t code, uint8_t sz) {
    return rd_rcode(const_cast<ngpc_cpu_t&>(c), code, sz);
}

void wr_rcode(ngpc_cpu_t& c, uint8_t code, uint8_t sz, uint32_t val) {
    unsigned pos = 0;
    uint32_t* slot = rcode_slot(c, code, pos);
    switch (sz) {
        case 0: {
            const unsigned sh = 8 * pos;
            *slot = (*slot & ~(uint32_t(0xFF) << sh)) | ((val & 0xFF) << sh);
            break;
        }
        case 1: {
            const unsigned sh = 8 * pos;
            *slot = (*slot & ~(uint32_t(0xFFFF) << sh)) | ((val & 0xFFFF) << sh);
            break;
        }
        default: *slot = val; break;
    }
}

void store(Machine& m, ngpc_record_t* rec, uint32_t addr, uint32_t value, uint8_t size);
bool eval_cc(const ngpc_cpu_t& c, unsigned cc);

/* Rotate / shift. Toshiba group (8): S and Z from the result, H = 0, V = PARITY,
 * N = 0, C = the bit shifted out. RL/RR rotate THROUGH the carry. */
static uint32_t do_shift(ngpc_cpu_t& c, unsigned op, uint8_t sz, uint32_t v, unsigned n) {
    const uint32_t mask = rmask(sz), sb = rsign(sz);
    const uint8_t bits = rbits(sz);
    bool carry = (c.flags & RF_C) != 0;
    v &= mask;

    for (unsigned k = 0; k < n; ++k) {
        bool out;
        switch (op) {
            case 0: out = (v & sb) != 0; v = ((v << 1) | (out ? 1u : 0u)) & mask; break;              // RLC
            case 1: out = (v & 1u) != 0; v = ((v >> 1) | (out ? sb : 0u)) & mask; break;              // RRC
            case 2: out = (v & sb) != 0; v = ((v << 1) | (carry ? 1u : 0u)) & mask; break;            // RL
            case 3: out = (v & 1u) != 0; v = ((v >> 1) | (carry ? sb : 0u)) & mask; break;            // RR
            case 4: out = (v & sb) != 0; v = (v << 1) & mask; break;                                  // SLA
            case 5: out = (v & 1u) != 0; v = ((v >> 1) | (v & sb)) & mask; break;                     // SRA (sign-extend)
            case 6: out = (v & sb) != 0; v = (v << 1) & mask; break;                                  // SLL
            default: out = (v & 1u) != 0; v = (v >> 1) & mask; break;                                 // SRL
        }
        carry = out;
    }

    uint8_t f = 0;
    if (v & sb) f |= RF_S;
    if (v == 0) f |= RF_Z;
    if (alu_even_parity(v, bits)) f |= RF_V;
    if (carry) f |= RF_C;
    c.flags = f;                                   // H = 0, N = 0
    return v;
}

bool exec_reg_family(Machine& m, ngpc_record_t* rec, uint8_t op, uint32_t pc,
                     uint8_t& out_len, uint16_t& out_cycles, uint32_t& new_pc, bool& jumped);

bool exec_reg_family(Machine& m, ngpc_record_t* rec, uint8_t op, uint32_t pc,
                     uint8_t& out_len, uint16_t& out_cycles, uint32_t& new_pc, bool& jumped) {
    ngpc_cpu_t& c = m.cpu;

    /* C8..CF = byte, D8..DF = word, E8..EF = long. (C7/D7/E7 are the extended
     * register-code escapes and are NOT handled here yet.) */
    uint8_t sz;
    const bool escape = (op == 0xC7 || op == 0xD7 || op == 0xE7);
    if (escape)                        sz = uint8_t(op == 0xC7 ? 0 : op == 0xD7 ? 1 : 2);
    else if (op >= 0xC8 && op <= 0xCF) sz = 0;
    else if (op >= 0xD8 && op <= 0xDF) sz = 1;
    else if (op >= 0xE8 && op <= 0xEF) sz = 2;
    else return false;

    /* In escape mode the operand register is named by a full 8-bit code in the
     * SECOND byte, so the sub-opcode -- and every operand after it -- shifts by
     * one. Everything else about the family is identical. */
    const uint8_t  rcode = escape ? m.read8(pc + 1) : 0;
    const uint8_t  so    = escape ? 2 : 1;          // offset of the sub-opcode
    const unsigned r     = op & 0x07;               // register from the FIRST byte
    const uint8_t  sub   = m.read8(pc + so);
    const unsigned R     = sub & 0x07;              // register from the SUB-OPCODE

    auto RD = [&]() -> uint32_t { return escape ? rd_rcode(c, rcode, sz) : rd_reg(c, r, sz); };
    auto WR = [&](uint32_t v) { if (escape) wr_rcode(c, rcode, sz, v); else wr_reg(c, r, sz, v); };
    auto L  = [&](uint8_t n) -> uint8_t { return uint8_t(n + (escape ? 1 : 0)); };
    const uint32_t mask = rmask(sz);
    const uint8_t  nb   = sz == 0 ? 1 : sz == 1 ? 2 : 4;

    jumped = false;
    uint8_t len = 2;
    uint16_t cyc = 2;

    auto imm_at = [&](uint8_t off) -> uint32_t {
        uint32_t v = 0;
        for (uint8_t i = 0; i < nb; ++i) v |= uint32_t(m.read8(pc + off + i)) << (8 * i);
        return v;
    };

    /* --- ALU R,r and the LD/EX pairs: sub 0x80..0xFF ---------------------- */
    if (sub >= 0x80) {
        const unsigned row = (sub - 0x80) >> 3;     // 0..15
        const uint32_t rv = RD();                    // the operand register
        const uint32_t Rv = rd_reg(c, R, sz);       // the SUB-OPCODE register
        const bool carry = (c.flags & RF_C) != 0;

        switch (row) {
            case 0x0: {  // 0x80..0x87  ADD R, r
                const uint32_t res = uint32_t((uint64_t(Rv) + rv) & mask);
                c.flags = alu_add_flags(sz, Rv, rv, res); wr_reg(c, R, sz, res);
                out_len = L(2); out_cycles = 2; return true;
            }
            case 0x1: {  // 0x88..0x8F  LD R, r
                wr_reg(c, R, sz, rv);
                out_len = L(2); out_cycles = 2; return true;
            }
            case 0x2: {  // 0x90..0x97  ADC R, r
                const uint32_t res = uint32_t((uint64_t(Rv) + rv + carry) & mask);
                c.flags = alu_add_flags(sz, Rv, uint64_t(rv) + carry, res); wr_reg(c, R, sz, res);
                out_len = L(2); out_cycles = 2; return true;
            }
            case 0x3: {  // 0x98..0x9F  LD r, R
                WR(Rv);
                out_len = L(2); out_cycles = 2; return true;
            }
            case 0x4: {  // 0xA0..0xA7  SUB R, r
                const uint32_t res = uint32_t((uint64_t(Rv) - rv) & mask);
                c.flags = alu_sub_flags(sz, Rv, rv, res); wr_reg(c, R, sz, res);
                out_len = L(2); out_cycles = 2; return true;
            }
            case 0x5: {  // 0xA8..0xAF  LD r, #3   (3-bit immediate in the sub-opcode)
                WR(R);
                out_len = L(2); out_cycles = 2; return true;
            }
            case 0x6: {  // 0xB0..0xB7  SBC R, r
                const uint32_t res = uint32_t((uint64_t(Rv) - rv - carry) & mask);
                c.flags = alu_sub_flags(sz, Rv, uint64_t(rv) + carry, res); wr_reg(c, R, sz, res);
                out_len = L(2); out_cycles = 2; return true;
            }
            case 0x7: {  // 0xB8..0xBF  EX R, r   — state 3. 3. -, byte and word only.
                if (sz == 2) return false;
                wr_reg(c, R, sz, rv);
                WR(Rv);              // flags unchanged (symbol row `- - - - - -`)
                out_len = L(2); out_cycles = 3; return true;
            }
            case 0x8: {  // 0xC0..0xC7  AND R, r
                const uint32_t res = (Rv & rv) & mask;
                c.flags = alu_logic_flags(sz, res, true); wr_reg(c, R, sz, res);
                out_len = L(2); out_cycles = 2; return true;
            }
            case 0x9: {  // 0xC8..0xCF  ALU r, #   (ADD ADC SUB SBC AND XOR OR CP)
                const uint32_t imm = imm_at(uint8_t(so + 1));
                const unsigned kind = sub & 0x07;
                uint32_t res = 0; bool write = true;
                switch (kind) {
                    case 0: res = uint32_t((uint64_t(rv) + imm) & mask);          c.flags = alu_add_flags(sz, rv, imm, res); break;
                    case 1: res = uint32_t((uint64_t(rv) + imm + carry) & mask);  c.flags = alu_add_flags(sz, rv, uint64_t(imm) + carry, res); break;
                    case 2: res = uint32_t((uint64_t(rv) - imm) & mask);          c.flags = alu_sub_flags(sz, rv, imm, res); break;
                    case 3: res = uint32_t((uint64_t(rv) - imm - carry) & mask);  c.flags = alu_sub_flags(sz, rv, uint64_t(imm) + carry, res); break;
                    case 4: res = (rv & imm) & mask;  c.flags = alu_logic_flags(sz, res, true);  break;
                    case 5: res = (rv ^ imm) & mask;  c.flags = alu_logic_flags(sz, res, false); break;
                    case 6: res = (rv | imm) & mask;  c.flags = alu_logic_flags(sz, res, false); break;
                    default: res = uint32_t((uint64_t(rv) - imm) & mask);         c.flags = alu_sub_flags(sz, rv, imm, res);
                             write = false; break;   // CP
                }
                if (write) WR(res);
                out_len = uint8_t(L(2) + nb);
                out_cycles = (sz == 0) ? 3 : (sz == 1) ? 4 : 6;   // state 3. 4. 6
                return true;
            }
            case 0xA: {  // 0xD0..0xD7  XOR R, r
                const uint32_t res = (Rv ^ rv) & mask;
                c.flags = alu_logic_flags(sz, res, false); wr_reg(c, R, sz, res);
                out_len = L(2); out_cycles = 2; return true;
            }
            case 0xB: {  // 0xD8..0xDF  CP r, #3
                const uint32_t res = uint32_t((uint64_t(rv) - R) & mask);
                c.flags = alu_sub_flags(sz, rv, R, res);
                out_len = L(2); out_cycles = 2; return true;
            }
            case 0xC: {  // 0xE0..0xE7  OR R, r
                const uint32_t res = (Rv | rv) & mask;
                c.flags = alu_logic_flags(sz, res, false); wr_reg(c, R, sz, res);
                out_len = L(2); out_cycles = 2; return true;
            }
            case 0xD: {  // 0xE8..0xEF  shift r, #4   (#4 == 0 means 16)
                unsigned n = m.read8(pc + so + 1) & 0x0F;
                if (n == 0) n = 16;
                WR(do_shift(c, sub & 0x07, sz, rv, n));
                /* Cycles are NOT constant: Toshiba list (8) gives `RLC #4, r` the
                 * state "3 + n/4" -- the cost grows with the shift count. */
                out_len = L(3); out_cycles = uint16_t(3 + n / 4); return true;
            }
            case 0xE: {  // 0xF0..0xF7  CP R, r
                const uint32_t res = uint32_t((uint64_t(Rv) - rv) & mask);
                c.flags = alu_sub_flags(sz, Rv, rv, res);
                out_len = L(2); out_cycles = 2; return true;
            }
            default: {   // 0xF8..0xFF  shift r, A   (count comes from the A register)
                unsigned n = uint8_t(c.regs[NGPC_XWA] & 0xFF) & 0x0F;   // A = XWA low byte
                if (n == 0) n = 16;
                WR(do_shift(c, sub & 0x07, sz, rv, n));
                out_len = L(2); out_cycles = uint16_t(3 + n / 4);   // same "3 + n/4" state
                return true;
            }
        }
    }

    /* --- the fixed sub-opcodes -------------------------------------------- */
    const uint32_t rv = RD();

    /* RES / SET / CHG / BIT / TSET  #4, r   (sub 0x30..0x34).
     * The bit number lives in a THIRD byte, so these are 3 bytes long.
     * Cycles: 3, except TSET which is 4. Toshiba: BIT and TSET write
     * Z = inverted bit, H = 1, N = 0; RES / SET / CHG touch no flags at all. */
    if (sub >= 0x30 && sub <= 0x34) {
        /* The bit number is a 4-BIT field (`#4`), whatever the operand size --
         * so it is masked to 0..15 even for a 32-bit register. */
        const unsigned bit = m.read8(pc + so + 1) & 0x0Fu;
        const uint32_t bm = 1u << bit;
        const bool b = (rv & bm) != 0;
        switch (sub) {
            case 0x30: WR((rv & ~bm) & mask); break;                    // RES
            case 0x31: WR((rv |  bm) & mask); break;                    // SET
            case 0x32: WR((rv ^  bm) & mask); break;                    // CHG
            case 0x33: c.flags = uint8_t((c.flags & ~(RF_Z | RF_N))     // BIT
                                         | (b ? 0 : RF_Z) | RF_H);
                       break;
            default:   c.flags = uint8_t((c.flags & ~(RF_Z | RF_N))     // TSET
                                         | (b ? 0 : RF_Z) | RF_H);
                       WR((rv | bm) & mask);
                       break;
        }
        out_len = L(3);
        out_cycles = (sub == 0x34) ? 4 : 3;
        return true;
    }

    /* MUL / MULS / DIV / DIVS  rr, #   (sub 0x08..0x0B) -- the IMMEDIATE forms.
     * `d8 08 e8` (`multu WA, #`) stopped 29 of the 66 commercial ROMs.
     *
     * The destination is DOUBLE-WIDTH. Byte prefix: the source is A and the
     * result lands in WA. Word prefix: the source is WA and the result lands in
     * XWA. DIV puts the QUOTIENT in the low half and the REMAINDER in the high
     * half (<Divide>: "dst<lower half> <- dst / src, dst<upper half> <- remainder").
     *
     * FLAGS -- the two pages say opposite things, and both are exact:
     *   <Multiply> : `- - - - - -`   MUL and MULS change **NO FLAGS AT ALL**.
     *   <Divide>   : `- - - V - -`   DIV writes **V ONLY** -- 1 on divide-by-zero
     *                or quotient overflow, 0 otherwise. S, Z, H, N, C untouched.
     * (The reference was writing Z, S, C and V from the product on MUL, so code
     * that multiplied and then branched on Z got the wrong answer. Fixed there.)
     *
     * CYCLES, Toshiba list (4):  MUL 12.15.-   MULS 10.13.-
     *                            DIV 15.23.-   DIVS 18.26.-
     *
     * DIVIDE-BY-ZERO and QUOTIENT OVERFLOW leave the destination **undefined** --
     * the datasheet says so in as many words. We do not invent a value for it
     * (HARDWARE_COMPAT_POLICY.md §7): the machine stops, exactly as the reference
     * does. If a real game ever trips this, we will see it and decide with
     * evidence rather than with a guess. */
    if (sub >= 0x08 && sub <= 0x0B) {
        if (sz == 2) return false;                  // no long form
        const bool is_div    = (sub >= 0x0A);
        const bool is_signed = (sub & 0x01) != 0;
        const uint8_t half   = (sz == 0) ? 8 : 16;  // width of each half
        const uint32_t hmask = (sz == 0) ? 0xFFu : 0xFFFFu;
        const uint32_t wmask = (sz == 0) ? 0xFFFFu : 0xFFFFFFFFu;

        /* Same `rr` code table as the register and memory forms -- Toshiba states
         * it a third time, for `DIV rr,#`. Here the code is the FIRST byte's `r`
         * field, and at BYTE size only the ODD codes name a destination:
         *
         *      mul WA,7  -> C9 08 07        mul XWA,0x1234 -> D8 08 34 12
         *      mul BC,7  -> CB 08 07        mul XBC,0x1234 -> D9 08 34 12
         *      mul DE,7  -> CD 08 07        mul XSP,0x1234 -> DF 08 34 12
         *      mul HL,7  -> CF 08 07
         *
         * -- the official assembler, which will not emit `C8 08 ..` at all. This
         * core did execute it, and so did the reference, IDENTICALLY: the two
         * agreed on a wrong answer, which is precisely the failure mode the
         * differential gate cannot see and the assembler-as-oracle can. */
        /* ⚠️ ON THE ESCAPE PATH THE DESTINATION IS NAMED BY THE RCODE, NOT BY `r`.
         *
         * `r` is the low 3 bits of the FIRST byte, and for an escape that byte is
         * 0xC7 / 0xD7 / 0xE7 -- so r is always 7, and `rr = r >> 1 = 3` made EVERY
         * escaped MUL/DIV target XHL. The OFFICIAL TOSHIBA ASSEMBLER settles it:
         *
         *      div IX,0xC0   ->  c7 f0 0a c0     <- exactly the bytes Sonic runs
         *      div HL,0xC0   ->  cf 0a c0        <- a COMPLETELY different encoding
         *
         * We were reading an instruction that names IX and executing it on HL. The
         * caller then walked a corrupted object list -- one enemy short. The
         * arithmetic was right; the register was not.
         *
         * (A third-party emulator's picture is what made the missing enemy visible:
         * a TRIAGE signal, never a verdict. asm900 is the verdict.)
         *
         * The odd-code guard likewise only means anything for the 3-bit field. */
        if (!escape && sz == 0 && (r & 1) == 0) return false;   // no such word register
        const unsigned rr = (sz == 0) ? (r >> 1) : r;
        unsigned rpos = 0;
        uint32_t* const dslot = escape ? rcode_slot(c, rcode, rpos) : &c.regs[rr];
        /* At byte size the operand is the 16-bit HALF the rcode names (`rpos` is 0
         * for the low half, 2 for the Q-half); at word size it is the whole 32. */
        const unsigned rsh = (sz == 0) ? (8u * (rpos & 2u)) : 0u;

        const uint32_t imm = imm_at(uint8_t(so + 1)) & hmask;
        uint32_t& dst = *dslot;
        const uint32_t wide = (dst >> rsh) & wmask;  // the full double-width value

        if (is_div) {
            /* ⚖️ DIVIDE BY ZERO: THE DESTINATION IS WRITTEN, AND WE MEASURED WHAT.
             *
             * The manual defines the FLAG (V = 1) and says NOTHING about `dst`, so
             * this core used to leave it alone -- an honest refusal, but a wrong one.
             * hw_calibration/bin/main.ngc ran four dividends through `div WA,B` with
             * B = 0 on a real NGPC and printed WA each time:
             *
             *      0000 -> 00FF        FFFF -> FF00
             *      8001 -> 017F        1234 -> 34ED
             *
             * Read the halves and the rule falls out, with no fitting needed:
             *
             *      W (the remainder half) := the dividend's LOW  byte
             *      A (the quotient  half) := NOT the dividend's HIGH byte
             *
             * which is exactly a datapath that shifts the dividend left by one half
             * and accumulates the complement of eight comparisons that all take the
             * same branch. Measured on silicon, not modelled. */
            if (imm == 0) {
                const uint32_t lo = wide & hmask;
                const uint32_t hi = (wide >> half) & hmask;
                const uint32_t packed = ((lo & hmask) << half) | ((~hi) & hmask);
                dst = (sz == 0)
                          ? ((dst & ~(uint32_t(0xFFFFu) << rsh)) | (packed << rsh))
                          : packed;
                c.flags = uint8_t(c.flags | RF_V);
                out_cycles = is_signed ? (sz == 0 ? 18 : 26) : (sz == 0 ? 15 : 23);
                out_len = L(uint8_t(so + 1 + (sz == 0 ? 1 : sz == 1 ? 2 : 4)));
                return true;
            }
            uint32_t q, rem;
            if (is_signed) {
                const int32_t n = (sz == 0) ? int32_t(int16_t(uint16_t(wide))) : int32_t(wide);
                const int32_t d = (sz == 0) ? int32_t(int8_t(uint8_t(imm)))
                                            : int32_t(int16_t(uint16_t(imm)));
                q = uint32_t(n / d); rem = uint32_t(n % d);
            } else {
                q = wide / imm; rem = wide % imm;
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
            dst = (sz == 0)
                      ? ((dst & ~(uint32_t(0xFFFFu) << rsh)) | (packed << rsh))
                      : packed;
            c.flags = uint8_t(overflow ? (c.flags | RF_V) : (c.flags & ~RF_V));
            out_cycles = is_signed ? (sz == 0 ? 18 : 26) : (sz == 0 ? 15 : 23);
        } else {
            const uint32_t src = wide & hmask;      // the low half multiplies
            uint32_t res;
            if (is_signed) {
                const int32_t a = (sz == 0) ? int32_t(int8_t(uint8_t(src)))
                                            : int32_t(int16_t(uint16_t(src)));
                const int32_t b = (sz == 0) ? int32_t(int8_t(uint8_t(imm)))
                                            : int32_t(int16_t(uint16_t(imm)));
                res = uint32_t(a * b);
            } else {
                res = src * imm;
            }
            dst = (sz == 0)
                      ? ((dst & ~(uint32_t(0xFFFFu) << rsh)) | ((res & 0xFFFFu) << rsh))
                      : res;
            /* no flags at all */
            out_cycles = is_signed ? (sz == 0 ? 10 : 13) : (sz == 0 ? 12 : 15);
        }
        out_len = uint8_t(L(2) + nb);
        return true;
    }

    if (sub == 0x03) {                              // LD r, #    state 3. 4. 6
        /* `so + 1`, NOT a fixed 2: in escape mode (C7/D7/E7) the register code
         * pushes the sub-opcode -- and therefore the immediate -- one byte later.
         * Reading `pc + 2` there re-reads the SUB-OPCODE as the immediate, which
         * is how `ld RW3, 0x0E` silently became `ld RW3, 0x03`.
         * Caught by gate G3 (whole-ROM trace equivalence), not by the fuzzer:
         * the fuzz list covered `C7 .. A9` (LD r,#3, no immediate byte) but never
         * an escape form WITH an immediate. */
        WR(imm_at(uint8_t(so + 1)));
        out_len = uint8_t(L(2) + nb);
        out_cycles = (sz == 0) ? 3 : (sz == 1) ? 4 : 6;
        return true;
    }
    if (sub == 0x04) {                              // PUSH r
        c.regs[NGPC_XSP] -= nb;
        store(m, rec, c.regs[NGPC_XSP], rv, nb);
        out_len = L(2); out_cycles = (sz == 2) ? 6 : 4;
        return true;
    }
    if (sub == 0x05) {                              // POP r
        uint32_t v = 0;
        for (uint8_t i = 0; i < nb; ++i) v |= uint32_t(m.read8(c.regs[NGPC_XSP] + i)) << (8 * i);
        c.regs[NGPC_XSP] += nb;
        WR(v);
        out_len = L(2); out_cycles = (sz == 2) ? 7 : 5;
        return true;
    }
    if (sub == 0x06 || sub == 0x07) {               // CPL r / NEG r   2. 2. -
        if (sz == 2) return false;
        if (sub == 0x06) {                          // CPL: H and N set to 1, others unchanged
            WR((~rv) & mask);
            c.flags = uint8_t(c.flags | RF_H | RF_N);
        } else {                                    // NEG: 0 - r
            const uint32_t res = uint32_t((uint64_t(0) - rv) & mask);
            c.flags = alu_sub_flags(sz, 0, rv, res);
            WR(res);
        }
        out_len = L(2); out_cycles = 2;
        return true;
    }
    /* DAA r  —  C8 + r : 10.  BYTE ONLY.  Flags: * * * P - *.  State 4.
     *
     * The correction table is transcribed verbatim from the CPU manual (<Decimal
     * Adjust Accumulator>), which enumerates every (N, C, upper nibble, H, lower
     * nibble) combination and the value to ADD. Nothing here is inferred: the
     * SUB rows add FA/A0/9A, which are simply the two's complements of 06/60/66,
     * so both directions are a plain addition.
     *
     * A combination the table does NOT list cannot arise from a real ADD/ADC or
     * SUB/SBC/NEG, and the manufacturer defines no result for it. We refuse
     * rather than invent one. */
    if (sub == 0x10) {
        if (sz != 0) return false;                  // "ż × ×" -- byte only
        const uint8_t v  = uint8_t(rv);
        const uint8_t hi = uint8_t(v >> 4);
        const uint8_t lo = uint8_t(v & 0x0F);
        const bool n_before = (c.flags & RF_N) != 0;
        const bool c_before = (c.flags & RF_C) != 0;
        const bool h_before = (c.flags & RF_H) != 0;

        /* The manual's table enumerates (N, C, upper, H, lower) row by row, and
         * every one of its thirteen rows is reproduced EXACTLY by the two-nibble
         * rule below: correct the low nibble when it left decimal range or a half
         * carry occurred, correct the high nibble when IT left range or a carry
         * occurred, and subtract instead of add when N says the last operation was
         * a subtraction. (Check any row: c=0, h=0, upper A-F, lower 0-9 -> only the
         * high nibble is out of range -> +60, carry out -- which is the table's
         * fourth row.)
         *
         * Writing it as a rule rather than a lookup matters, because the table
         * only covers what a real BCD add or subtract can produce. It says nothing
         * about, say, N=0, C=0, H=1, lower=0xF -- which no BCD addition can reach,
         * but which a fuzzer reaches constantly. Refusing there would have been a
         * core that cannot run `daa` on arbitrary data; agreeing with the table
         * everywhere it speaks, and extending it the only way that stays
         * consistent with it, is the honest reading. */
        uint8_t add = 0;
        bool c_after = c_before;
        if (!n_before) {                             /* after ADD / ADC */
            if (h_before || lo > 9)               add = uint8_t(add + 0x06);
            if (c_before || hi > 9 || (hi == 9 && lo > 9)) {
                add = uint8_t(add + 0x60);
                c_after = true;
            }
        } else {                                     /* after SUB / SBC / NEG */
            if (h_before)                         add = uint8_t(add - 0x06);
            if (c_before) { add = uint8_t(add - 0x60); c_after = true; }
        }

        const uint8_t res = uint8_t(v + add);
        uint8_t f = uint8_t(c.flags & RF_N);          // "N = No change"
        if (res & 0x80) f = uint8_t(f | RF_S);
        if (res == 0)   f = uint8_t(f | RF_Z);
        /* H = "a carry from bit 3 to bit 4 ... as a result of the operation" --
         * and on the SUBTRACT path that is a half-BORROW, not a half-carry.
         * `(v ^ add ^ res) & 0x10` is the one expression that gives both, which is
         * why the ALU helpers in this core already use it. Adding the nibbles of a
         * two's-complement adjustment (0xFA for -6) gets the subtract case wrong. */
        if ((uint8_t(v ^ add ^ res)) & 0x10) f = uint8_t(f | RF_H);
        if (alu_even_parity(res, 8))         f = uint8_t(f | RF_V);
        if (c_after)                         f = uint8_t(f | RF_C);
        c.flags = f;
        WR(res);
        out_len = L(2); out_cycles = 4;
        return true;
    }
    /* --- bit and carry-bit operations on a register ---------------------------
     * Toshiba instruction list (6). Every code, flag row and state below is
     * copied from it; nothing is inferred.
     *
     *   0x20 : #4   ANDCF #4,r   CY <- CY & r<#4>     - - - - - *   3. 3. -
     *   0x21 : #4   ORCF  #4,r   CY <- CY | r<#4>     - - - - - *   3. 3. -
     *   0x22 : #4   XORCF #4,r   CY <- CY ^ r<#4>     - - - - - *   3. 3. -
     *   0x23 : #4   LDCF  #4,r   CY <- r<#4>          - - - - - *   3. 3. -
     *   0x24 : #4   STCF  #4,r   r<#4> <- CY          - - - - - -   3. 3. -
     *   0x28..0x2C  the same five, with the bit number taken from A at run time
     *   0x30 : #4   RES   #4,r   r<#4> <- 0           - - - - - -   3. 3. -
     *   0x31 : #4   SET   #4,r   r<#4> <- 1           - - - - - -   3. 3. -
     *   0x32 : #4   CHG   #4,r   r<#4> <- not r<#4>   - - - - - -   3. 3. -
     *   0x33 : #4   BIT   #4,r   Z <- not r<#4>       X * 1 X 0 -   3. 3. -
     *   0x34 : #4   TSET  #4,r   Z <- not r<#4>; r<#4> <- 1
     *                                                 X * 1 X 0 -   4. 4. -
     * Byte and word only -- there is no long form ("BW-").
     *
     * The `X` in the BIT/TSET row is not "don't care": Toshiba writes X where the
     * HARDWARE LEAVES THE FLAG UNDEFINED. We keep whatever was there rather than
     * invent a value. */
    if ((sub >= 0x20 && sub <= 0x24) || (sub >= 0x28 && sub <= 0x2C)) {
        if (sz == 2) return false;                        // "BW-" -- no long form
        const bool from_a = (sub >= 0x28 && sub <= 0x2C);
        unsigned bit;
        uint8_t ilen;
        if (from_a) {
            bit = uint8_t(c.regs[NGPC_XWA]) & 0x0F;       // A, the low byte of XWA
            ilen = 2;
        } else {
            bit = m.read8(pc + L(2)) & 0x0F;              // the #4 operand byte
            ilen = 3;
        }
        const uint8_t nbits = rbits(sz);
        if (bit >= nbits) {
            /* The bit selector is FOUR bits wide whatever the operand size, so on a
             * BYTE register it can name a bit that is not there. Toshiba's table
             * covers one of those cases and no other: STCF simply leaves the
             * operand alone. Everything else is undefined, and we say so rather
             * than pick a bit. (The reference core reached the same two rules from
             * the same table; they agree instruction for instruction.) */
            if (sub == 0x24 || sub == 0x2C) {             // STCF #4,r / STCF A,r
                out_len = L(ilen); out_cycles = 3;
                return true;                              // no write, no flags
            }
            m.pending_status = NGPC_SILICON_UNDEFINED;
            return true;
        }

        const uint32_t bm = 1u << bit;
        const bool bset = (rv & bm) != 0;
        const bool cy   = (c.flags & RF_C) != 0;

        if (sub <= 0x2C) {                                // the five carry-bit ops
            const unsigned kind = from_a ? (sub - 0x28) : (sub - 0x20);
            bool ncy;
            switch (kind) {
                case 0: ncy = cy && bset; break;          // ANDCF
                case 1: ncy = cy || bset; break;          // ORCF
                case 2: ncy = cy != bset; break;          // XORCF
                case 3: ncy = bset;       break;          // LDCF
                default:                                  // STCF -- writes the REGISTER
                    WR(cy ? (rv | bm) : (rv & ~bm));
                    out_len = L(ilen); out_cycles = 3;
                    return true;                          // flags untouched
            }
            c.flags = uint8_t((c.flags & ~RF_C) | (ncy ? RF_C : 0));
            out_len = L(ilen); out_cycles = 3;
            return true;
        }

        return false;   // unreachable: every sub in range is handled above
    }

    /* --- MINC / MDEC : modulo increment / decrement ---------------------------
     * Toshiba list (4). WORD ONLY. The immediate carried in the instruction is
     * NOT the modulus: it is `modulus - step`. Length 4, no flags.
     *
     *   D8 + r : 38/39/3A : (# - 1/2/4)   MINC1/2/4
     *     if (r mod #) == (# - step)  then r <- r - (# - step)  else r <- r + step
     *     state 5
     *   D8 + r : 3C/3D/3E : (# - 1/2/4)   MDEC1/2/4
     *     if (r mod #) == 0           then r <- r + (# - step)  else r <- r - step
     *     state 4
     *
     * This is the ring-buffer primitive: it walks a power-of-two window and wraps
     * without a branch. */
    if ((sub >= 0x38 && sub <= 0x3A) || (sub >= 0x3C && sub <= 0x3E)) {
        if (sz != 1) return false;                        // "-W-" -- word only
        const bool is_dec = sub >= 0x3C;
        const uint32_t step = 1u << (sub - (is_dec ? 0x3C : 0x38));    // 1, 2, 4
        const uint32_t enc = uint32_t(m.read8(pc + L(2))) |
                             (uint32_t(m.read8(pc + L(3))) << 8);      // # - step
        const uint32_t modulus = enc + step;
        if (modulus == 0 || (modulus & (modulus - 1)) != 0) return false;   // "# = 2**n"

        const uint32_t v = rv & 0xFFFFu;
        uint32_t res;
        if (is_dec) res = ((v % modulus) == 0)   ? (v + enc) : (v - step);
        else        res = ((v % modulus) == enc) ? (v - enc) : (v + step);
        WR(res & 0xFFFFu);                                // flags untouched
        out_len = L(4); out_cycles = is_dec ? 4 : 5;
        return true;
    }

    /* --- MUL / MULS / DIV / DIVS  RR, r --------------------------------------
     * `C8 + zz + r : 40/48/50/58 + RR`. The SOURCE is `r` (from the first byte);
     * the DESTINATION `RR` is named by the sub-opcode's low 3 bits and is TWICE
     * the operation width -- "when the operation is in bytes, a word register is
     * specified; when in words, a long word register" (Toshiba's own note).
     *
     * States, REGISTER form -- not the memory form's, which is two higher:
     *   MUL 11.14  ·  MULS 9.12  ·  DIV 15.23  ·  DIVS 18.26
     * Flags: MUL/MULS write NONE (`- - - - - -`); DIV/DIVS write V only. */
    if (sub >= 0x40 && sub <= 0x5F) {
        if (sz == 2) return false;                        // "BW-"
        const unsigned kind = (sub - 0x40) >> 3;          // 0 MUL 1 MULS 2 DIV 3 DIVS
        /* THE `RR` CODE IS NOT A REGISTER INDEX, AND IT MEANS DIFFERENT THINGS AT
         * DIFFERENT SIZES. Toshiba spells the table out (<Divide>, Note 3):
         *
         *      word operation -> RR is a LONG register    byte operation -> a WORD one
         *          XWA 000  XBC 001  XDE 010  XHL 011         WA 001   BC 011
         *          XIX 100  XIY 101  XIZ 110  XSP 111         DE 101   HL 111
         *
         * so in BYTE size only the ODD codes name anything, and code 001 is WA --
         * the low word of XWA -- not XBC. Taking the code as an array index made
         * `div WA,A` divide XBC, which is how the differential gate caught it.
         * That is also why the assembler REFUSES `mul SP,E`: at byte size there is
         * no such destination. */
        const unsigned code = sub & 0x07;
        if (sz == 0 && (code & 1) == 0) return false;      // no such word register
        const unsigned rr = (sz == 0) ? (code >> 1) : code;
        const bool is_div    = kind >= 2;
        const bool is_signed = (kind & 1) != 0;
        const uint8_t half   = (sz == 0) ? 8 : 16;
        const uint32_t hmask = (sz == 0) ? 0xFFu : 0xFFFFu;
        const uint32_t wmask = (sz == 0) ? 0xFFFFu : 0xFFFFFFFFu;

        const uint32_t src = rv & hmask;
        uint32_t& dst = c.regs[rr];
        const uint32_t wide = dst & wmask;

        if (is_div) {
            /* DIVIDE BY ZERO AND QUOTIENT OVERFLOW ARE **DEFINED**, NOT TRAPS.
             *
             * This core used to stop on both. That was wrong, and the manual says
             * so in one sentence (<Divide>, whose flag row is `- - - V - -`):
             *
             *   "V = 1 is set when divided by 0 or the quotient exceeds the
             *    numerals which can be expressed in bits of dst; otherwise 0."
             *
             * Toshiba defined a flag for exactly these two cases, which means the
             * program is meant to run straight through them and TEST V. Three
             * commercial ROMs do. Stopping made the core lie about them.
             *
             * ⚖️ AND WHAT LANDS IN dst ON A DIVIDE BY ZERO IS NOW MEASURED, not open.
             * The manual is silent, so this core used to leave dst alone -- an honest
             * refusal, and a wrong one. hw_calibration ran four dividends through
             * `div WA,B` with B = 0 on a real NGPC:
             *
             *      0000 -> 00FF     FFFF -> FF00     8001 -> 017F     1234 -> 34ED
             *
             * Split the halves and the rule needs no fitting:
             *      W (remainder half) := the dividend's LOW  byte
             *      A (quotient  half) := NOT the dividend's HIGH byte
             *
             * i.e. a datapath that shifts the dividend up by one half and accumulates
             * the complement of eight comparisons that all take the same branch.
             * (The QUOTIENT-OVERFLOW case is still open: keep the low half.) */
            if (src == 0) {
                const uint32_t lo = wide & hmask;
                const uint32_t hi = (wide >> half) & hmask;
                const uint32_t packed = ((lo & hmask) << half) | ((~hi) & hmask);
                dst = (sz == 0) ? ((dst & 0xFFFF0000u) | packed) : packed;
                c.flags = uint8_t(c.flags | RF_V);
                out_len = L(2);
                out_cycles = uint16_t(is_signed ? (sz == 0 ? 18 : 26) : (sz == 0 ? 15 : 23));
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
            c.flags = uint8_t(overflow ? (c.flags | RF_V) : (c.flags & ~RF_V));
            /* Cost calibrated to silicon: cpu_calib_v2 on hardware read DIV word 265 vs our
             * old 301 (under-costed); word 23->37 makes the emulator read 265. The datasheet
             * 15/23 is a floor -- real DIV is variable-latency and slower. MUL word 14->19
             * likewise matches silicon's 444 (was 481). Byte/signed scaled by the same ratio. */
            out_cycles = uint16_t(is_signed ? (sz == 0 ? 29 : 42) : (sz == 0 ? 24 : 37));
        } else {
            const uint32_t a = wide & hmask;
            uint32_t res;
            if (is_signed) {
                const int32_t x = (sz == 0) ? int32_t(int8_t(uint8_t(a)))
                                            : int32_t(int16_t(uint16_t(a)));
                const int32_t y = (sz == 0) ? int32_t(int8_t(uint8_t(src)))
                                            : int32_t(int16_t(uint16_t(src)));
                res = uint32_t(x * y);
            } else {
                res = a * src;
            }
            dst = (sz == 0) ? ((dst & 0xFFFF0000u) | (res & 0xFFFFu)) : res;
            out_cycles = uint16_t(is_signed ? (sz == 0 ? 12 : 16) : (sz == 0 ? 15 : 19));
        }
        out_len = L(2);                                   // MUL/MULS: no flags at all
        return true;
    }

    if (sub == 0x12 || sub == 0x13) {               // EXTZ / EXTS   -. 3. 3  (word and long)
        if (sz == 0) return false;
        const unsigned half = (sz == 1) ? 8 : 16;
        const uint32_t low = rv & ((1u << half) - 1u);
        const bool neg = (low >> (half - 1)) & 1u;
        const uint32_t hi = (sub == 0x13 && neg) ? (mask & ~((1u << half) - 1u)) : 0u;
        WR(low | hi);                 // flags unchanged
        out_len = L(2); out_cycles = 3;
        return true;
    }
    if (sub >= 0x60 && sub <= 0x6F) {               // INC / DEC #3, r   (#3 == 0 means 8)
        /* ⭐ THE FLAGS DEPEND ON THE SIZE, AND ONLY THE BYTE FORM HAS ANY.
         * Toshiba's instruction list gives this opcode THREE rows, and they do not
         * agree with each other:
         *
         *     INC #3, r   `C8 + r`        size B - -   flags  * * * V 0 -
         *     INC #3, r   `C8 + zz + r`   size - W L   flags  - - - - - -
         *     INC<W> #3, (mem)            size B W -   flags  * * * V 0 -
         *     (DEC is identical, with N = 1 instead of 0.)
         *
         * A 16- or 32-bit register INC/DEC changes NOTHING. This core wrote flags
         * at every size, and it cost four ROMs.
         *
         * Faselei's `strlen` is why. It searches for the NUL with a block compare
         * and then steps back onto the match:
         *
         *     243B74  cpir (XHL)      ; search -- sets Z when it MATCHES
         *     243B76  dec 1, XHL      ; back up onto the byte it found
         *     243B78  ret Z           ; "found" -- reading the CPIR's Z
         *
         * Clobbering Z in that DEC turns every found string into "not found".
         * `strlen` then returns its 0xFFFF not-found sentinel, the caller passes
         * that to a memcpy, and 65 534 bytes get copied over the stack -- return
         * address included. The CPU returned to address 0, hit the `swi 7` there,
         * and the BIOS error handler powered the console off. */
        const bool is_dec = sub >= 0x68;
        uint32_t n = sub & 0x07;
        if (n == 0) n = 8;
        const uint32_t res = uint32_t((is_dec ? (uint64_t(rv) - n) : (uint64_t(rv) + n)) & mask);
        if (sz == 0) {                              // BYTE only: `* * * V 0/1 -`
            const uint8_t keep_c = uint8_t(c.flags & RF_C);          // C untouched
            c.flags = is_dec ? alu_sub_flags(sz, rv, n, res) : alu_add_flags(sz, rv, n, res);
            c.flags = uint8_t((c.flags & ~RF_C) | keep_c);
        }
        WR(res);
        out_len = L(2); out_cycles = 2;
        return true;
    }
    if (sub >= 0x70 && sub <= 0x7F) {               // SCC cc, r  -- r := condition ? 1 : 0
        if (sz == 2) return false;                  // state "2. 2. -": byte and word only
        WR(eval_cc(c, sub & 0x0F) ? 1u : 0u);
        out_len = L(2); out_cycles = 2;
        return true;
    }

    /* LDC cr, r  (0x2E)  and  LDC r, cr  (0x2F).
     *   `C8 + zz + r : 2E : cr`, length 3, state **3. 3. 3**, no flags touched.
     * (NeoPop bills 8 here; the datasheet says 3. The datasheet wins, as usual.)
     *
     * `ldc DMAC0, WA` -- `D8 2E 20` -- is the single biggest remaining blocker on
     * the real corpus: **19 of the 66 commercial ROMs** stop on it, because they
     * program the micro-DMA controller during boot. */
    if (sub == 0x2E || sub == 0x2F) {
        const uint8_t cr = m.read8(pc + so + 1) & 0x3F;

        /* The transfer width comes from the CONTROL REGISTER, not from the `zz`
         * prefix: the control registers have fixed architectural widths.
         *   0x00..0x1C  DMAS0..3 / DMAD0..3  -> LONG
         *   0x20/24/28/2C  DMAC0..3          -> WORD
         *   0x22/26/2A/2E  DMAM0..3          -> BYTE
         *   0x30           INTNEST           -> WORD
         * So `ldc XWA, DMAC0` moves 16 bits and LEAVES THE HIGH HALF OF XWA
         * ALONE, even though the opcode carries the long prefix. Sizing it by
         * `zz` instead silently zeroes the top half of the destination. */
        uint8_t csz;
        if (cr <= 0x1F)                       csz = 2;   // DMAS / DMAD
        else if (cr == 0x30)                  csz = 1;   // INTNEST
        else if ((cr & 0x03) == 0x02)         csz = 0;   // DMAM (0x22/26/2A/2E)
        else                                  csz = 1;   // DMAC (0x20/24/28/2C)

        const uint32_t cmask = rmask(csz);
        if (sub == 0x2E) {                          // cr <- r
            c.cregs[cr] = (c.cregs[cr] & ~cmask) | ((escape ? rd_rcode(c, rcode, csz) : rd_reg(c, r, csz)) & cmask);
        } else {                                    // r <- cr
            if (escape) wr_rcode(c, rcode, csz, c.cregs[cr] & cmask); else wr_reg(c, r, csz, c.cregs[cr] & cmask);
        }
        out_len = L(3); out_cycles = 3;
        return true;
    }

    /* LINK r, d16   `E8 + r : 0C : d16`   LONG only, length 4, state 8.
     *   PUSH r ; LD r, XSP ; ADD XSP, d16     (the classic frame prologue)
     *
     * ⚠️ WRITE BACK THROUGH `WR`, NOT `c.regs[r]`. On the E7 escape the register is
     * named by the RCODE, and `r` is just the low 3 bits of 0xE7 -- i.e. 7, XSP. So
     * `link XWA3,4` would have linked the STACK POINTER. The escape form is real;
     * the official assembler emits it:
     *
     *      link XWA3,4  ->  e7 30 0c 04 00        unlk XWA3  ->  e7 30 0d
     *
     * Same class of bug as the escaped MUL/DIV above -- found by auditing for it
     * rather than by waiting for a game to trip over it. */
    /* BS1F A, r   `D8+r : 0E`   ·   BS1B A, r   `D8+r : 0F`     WORD ONLY.
     *
     * Toshiba, CPU900L1-55/56 -- and the datasheet is unusually explicit, so this
     * is transcribed, not inferred:
     *
     *   BS1F  searches the word FORWARD  (LSB -> MSB) for the first bit set to 1;
     *   BS1B  searches it BACKWARD (MSB -> LSB). The BIT NUMBER goes into A.
     *   Size column `× ż ×`: word only. The destination is always A -- it is not
     *   encoded, so nothing else can be written.
     *   Flags `- - - * - -`: V ALONE is touched. V = 1 iff src is all zeros.
     *   Its own examples: IX = 0x1200 -> BS1F gives A = 0x09, BS1B gives A = 0x0C.
     *
     * ⚠️ WHEN src == 0 THE DATASHEET SAYS A TAKES "AN UNDEFINED VALUE".
     * So we write NOTHING to A: there is no value we could store that would be
     * true, and inventing one is exactly what § 9 forbids. V is what software must
     * test, and V is what we set. (An open hardware question, and a cheap one for
     * the calibration ROM to answer: run BS1F on 0 and print A.)
     *
     * ⚠️ CYCLES ARE NOT SOURCED. The Toshiba instruction lists in `doc t_900` give
     * no figure for these two. The only number available is NeoPop's 4 -- and this
     * project has caught NeoPop's cycle counts being wrong on JR, CALR and MUL/DIV.
     * 4 is a PLACEHOLDER. Replace it the day a Toshiba list turns up. */
    if (sub == 0x0E || sub == 0x0F) {
        if (sz != 1) return false;                   // "× ż ×" -- word only
        const uint16_t src = uint16_t(rv);
        if (src == 0) {
            c.flags = uint8_t(c.flags | RF_V);       // "V = 1 if the contents of src are all 0s"
        } else {
            unsigned bit;
            if (sub == 0x0E) { bit = 0;  while (!((src >> bit) & 1u)) ++bit; }   // forward
            else             { bit = 15; while (!((src >> bit) & 1u)) --bit; }   // backward
            c.regs[NGPC_XWA] = (c.regs[NGPC_XWA] & ~0xFFu) | uint32_t(bit);      // A = XWA's low byte
            c.flags = uint8_t(c.flags & ~RF_V);
        }
        out_len = L(2); out_cycles = 4;
        return true;
    }

    if (sub == 0x0C) {
        if (sz != 2) return false;
        const int16_t d = int16_t(uint16_t(m.read8(pc + so + 1)) | (uint16_t(m.read8(pc + so + 2)) << 8));
        c.regs[NGPC_XSP] -= 4;
        store(m, rec, c.regs[NGPC_XSP], rv, 4);     // rv is already escape-aware
        WR(c.regs[NGPC_XSP]);
        c.regs[NGPC_XSP] += uint32_t(int32_t(d));
        out_len = L(4); out_cycles = 8;
        return true;
    }

    /* UNLK r   `E8 + r : 0D`   LONG only, length 2, state 7.
     *   LD XSP, r ; POP r      (the matching epilogue) -- same escape rule as LINK. */
    if (sub == 0x0D) {
        if (sz != 2) return false;
        c.regs[NGPC_XSP] = rv;                      // rv is already escape-aware
        uint32_t v = 0;
        for (uint8_t i = 0; i < 4; ++i) v |= uint32_t(m.read8(c.regs[NGPC_XSP] + i)) << (8 * i);
        c.regs[NGPC_XSP] += 4;
        WR(v);
        out_len = L(2); out_cycles = 7;
        return true;
    }
    if (sub == 0x1C) {                              // DJNZ r, d8   6 (r != 0) / 4 (r == 0)
        if (sz == 2) return false;                  // size column "BW-": no long form
        const int8_t d = int8_t(m.read8(pc + so + 1));
        const uint32_t res = uint32_t((uint64_t(rv) - 1) & mask);
        WR(res);                      // flags unchanged
        const uint32_t next = (pc + L(3)) & kAddrMask;
        if (res != 0) { new_pc = (next + uint32_t(int32_t(d))) & kAddrMask; jumped = true; }
        out_len = L(3); out_cycles = (res != 0) ? 6 : 4;
        return true;
    }

    (void)len; (void)cyc;
    return false;   // not ported yet -> honest trap
}

}  // namespace ngpc
