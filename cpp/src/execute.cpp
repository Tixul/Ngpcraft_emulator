/* execute.cpp — decode + execute, one instruction.
 *
 * The Python core dispatches by trying ~100 `_try_execute_*` functions in a
 * LINEAR CHAIN, for every instruction (PERF_TIMING_POLICY.md §10.2). Here the
 * first opcode byte selects the handler directly. That is the whole speed
 * story; there is no cleverness beyond it and none is needed (a plain switch
 * interpreter clears the 615 000 instr/s target by two orders of magnitude).
 *
 * Cycle counts are NOT re-derived from the assembly text the way the Python
 * core does (`_executed_cycles_from_decoded` re-decodes the instruction by
 * string-matching its operands, ~310 lines). Each handler states its own count.
 *
 * PORT STATUS: this file grows one opcode family at a time. Every family lands
 * with gate G2 green (oracle_tools/native_diff.py). Anything not yet ported
 * falls through to NGPC_UNIMPLEMENTED and STOPS the machine, naming its opcode.
 */
#include "machine.hpp"

#include <cstring>

namespace ngpc {

/* --- byte-register file: r8 code -> (which XREG, which byte) ----------------
 * R8 = W,A,B,C,D,E,H,L (core/decode.py:11). So code i lives in XREG[i>>1], in
 * the HIGH byte when i is even (W,B,D,H) and the LOW byte when odd (A,C,E,L).
 * Getting this backwards silently corrupts every byte operation, so it is
 * pinned by gate G2 rather than by eyeballing. */
static inline uint8_t get_r8(const ngpc_cpu_t& c, unsigned i) {
    return uint8_t(c.regs[i >> 1] >> ((i & 1) ? 0 : 8));
}
static inline void set_r8(ngpc_cpu_t& c, unsigned i, uint8_t v) {
    const unsigned sh = (i & 1) ? 0 : 8;
    uint32_t& r = c.regs[i >> 1];
    r = (r & ~(uint32_t(0xFF) << sh)) | (uint32_t(v) << sh);
}
static inline void set_r16(ngpc_cpu_t& c, unsigned i, uint16_t v) {
    c.regs[i] = (c.regs[i] & 0xFFFF0000u) | v;
}
static inline void set_r32(ngpc_cpu_t& c, unsigned i, uint32_t v) { c.regs[i] = v; }

/* --- little-endian operand fetch ------------------------------------------ */
static inline uint16_t fetch16(const Machine& m, uint32_t a) {
    return uint16_t(m.read8(a) | (uint32_t(m.read8(a + 1)) << 8));
}
static inline uint32_t fetch32(const Machine& m, uint32_t a) {
    return uint32_t(m.read8(a)) | (uint32_t(m.read8(a + 1)) << 8) |
           (uint32_t(m.read8(a + 2)) << 16) | (uint32_t(m.read8(a + 3)) << 24);
}

/* Call AFTER the handler has set the new PC: next_pc is read back from the CPU,
 * so branches and returns record where they actually went. */
static inline void finish(ngpc_record_t* rec, const Machine& m, uint32_t pc,
                          uint8_t len, uint16_t cycles, uint32_t written_regs) {
    if (!rec) return;
    rec->pc = pc;
    rec->raw_len = len;
    /* Raw bytes are for the log only, so read the memory image directly -- going
     * through read8() would double-charge the cart wait-state the decode already paid. */
    for (uint8_t i = 0; i < len && i < NGPC_MAX_RAW; ++i)
        rec->raw[i] = m.mem[(pc + i) & kAddrMask];
    rec->next_pc = m.cpu.pc;
    rec->cycles = cycles;
    rec->status = NGPC_OK;
    rec->written_regs = written_regs;
}

/* --- condition codes -------------------------------------------------------
 * CC = F,LT,LE,ULE,OV,MI,Z,C,T,GE,GT,UGT,NOV,PL,NZ,NC (core/decode.py:14).
 * Mirrors _evaluate_condition_code (core/execute.py:18808) minus its tri-state:
 * here the flags always hold a value. */
static inline bool flag_c(const ngpc_cpu_t& c) { return (c.flags & 0x01) != 0; }
static inline bool flag_v(const ngpc_cpu_t& c) { return (c.flags & 0x04) != 0; }
static inline bool flag_z(const ngpc_cpu_t& c) { return (c.flags & 0x40) != 0; }
static inline bool flag_s(const ngpc_cpu_t& c) { return (c.flags & 0x80) != 0; }

bool eval_cc(const ngpc_cpu_t& c, unsigned cc) {
    const bool s = flag_s(c), z = flag_z(c), v = flag_v(c), cf = flag_c(c);
    switch (cc) {
        case 0:  return false;           // F   — never
        case 1:  return s != v;          // LT
        case 2:  return z || (s != v);   // LE
        case 3:  return cf || z;         // ULE
        case 4:  return v;               // OV
        case 5:  return s;               // MI
        case 6:  return z;               // Z
        case 7:  return cf;              // C
        case 8:  return true;            // T   — always
        case 9:  return s == v;          // GE
        case 10: return !z && (s == v);  // GT
        case 11: return !cf && !z;       // UGT
        case 12: return !v;              // NOV
        case 13: return !s;              // PL
        case 14: return !z;              // NZ
        default: return !cf;             // NC
    }
}

/* --- memory writes ---------------------------------------------------------
 * A write to ROM / BIOS / unmapped space is DISCARDED — but still REPORTED,
 * because a discarded cart-window write is exactly what latches an AMD flash
 * command (CPP_CORE_PORT.md §4 hazard 9). Mirrors _check_writable_range: if any
 * byte of the range is unwritable the whole write is dropped and memory is left
 * untouched. */
void store(Machine& m, ngpc_record_t* rec, uint32_t addr, uint32_t value, uint8_t size) {
    bool writable = true;
    for (uint8_t i = 0; i < size; ++i)
        if (!region_writable(region_of(addr + i))) { writable = false; break; }

    /* A write into a cart window is NOT a discarded store: it is a COMMAND to the
     * flash chip. The BIOS uses it to ask the cartridge what it is, and a core that
     * throws those bytes away as "read-only region" gets no answer and refuses to
     * boot the game. See machine.hpp. */
    if (!writable) {
        bool consumed = false;
        for (uint8_t i = 0; i < size; ++i)
            if (m.flash_command(addr + i, uint8_t(value >> (8 * i)))) consumed = true;
        if (consumed) {
            if (rec && rec->n_writes < NGPC_MAX_ACCESS) {
                ngpc_access_t& a = rec->writes[rec->n_writes++];
                a.address = addr; a.size = size; a.discarded = 1;
                for (uint8_t i = 0; i < size; ++i) a.data[i] = uint8_t(value >> (8 * i));
            }
            return;
        }
    }

    uint8_t bytes[4];
    for (uint8_t i = 0; i < size; ++i) bytes[i] = uint8_t(value >> (8 * i));  // little-endian

    if (writable)
        for (uint8_t i = 0; i < size; ++i) {
            const uint32_t a = (addr + i) & kAddrMask;
            m.mem[a] = bytes[i];
            /* VRAM-write wait (see Machine::vram_wait): the K2GE throttles CPU access to
             * display RAM DURING THE ACTIVE DRAWING PERIOD only -- in vblank the bus is
             * free, so a game that writes VRAM in vblank pays nothing. Confirmed by
             * cpu_calib_v3 on silicon (VWR < MEM). Guarded on active display so vblank
             * writes are never charged. */
            if (m.vram_wait && a >= 0x8000 && a <= 0xBFFF && !m.in_vblank())
                m.access_wait += m.vram_wait;
            m.note_write(a, bytes[i]);      // the write log; disarmed, this is 2 compares
            /* The RTC's registers are not plain I/O bytes: a write sets the clock --
             * or, at 0x98-0x9A, the alarm it should go off at. */
            if (a >= 0x90 && a <= 0x9A) m.rtc_write(a, bytes[i]);
        }

    /* The SOUND CPU's control registers are memory-mapped, and writing them is an
     * ACTION, not a store: 0xB8 releases the Z80 from reset, 0xBA fires an NMI at
     * it. A plain byte in the address space would just sit there.
     *
     * ⚡ AND SO ARE THE DAC PORTS. 0xA2 / 0xA3 are the left and right converters: the
     * main CPU streams a sampled voice straight into the speaker through them, with the
     * sound chip taking no part. Left as plain memory, the bytes just sat there and the
     * voice was silent -- which is exactly why Sonic's music played and its "SEGAAA"
     * did not. */
    if (writable)
        for (uint8_t i = 0; i < size; ++i)
            io_action_write(m, (addr + i) & kAddrMask, bytes[i]);

    /* A store the bus threw away, that was NOT a flash command. The program has no
     * way to find out, so nothing goes wrong visibly -- it just silently does not
     * happen. Counted only for genuinely unmapped space: cart-window writes are the
     * flash command latch and were already handled above. */
    if (!writable && m.hygiene_on && region_of(addr) == Region::Unmapped)
        m.note_lost_write(addr);

    if (rec && rec->n_writes < NGPC_MAX_ACCESS) {
        ngpc_access_t& a = rec->writes[rec->n_writes++];
        a.address = addr;
        a.size = size;
        a.discarded = writable ? 0 : 1;
        for (uint8_t i = 0; i < size; ++i) a.data[i] = bytes[i];
    }
}

static inline void push32(Machine& m, ngpc_record_t* rec, uint32_t value) {
    m.cpu.regs[NGPC_XSP] -= 4;                      // pre-decrement, then store
    store(m, rec, m.cpu.regs[NGPC_XSP], value, 4);
}
static inline uint32_t pop32(Machine& m) {
    const uint32_t v = fetch32(m, m.cpu.regs[NGPC_XSP]);
    m.cpu.regs[NGPC_XSP] += 4;
    return v;
}

/* Execute exactly one instruction. Returns an ngpc_status_t. On any status
 * other than NGPC_OK the CPU state is left UNTOUCHED — the machine stops where
 * it is, so the trap names the real offender. */
uint8_t step(Machine& m, ngpc_record_t* rec) {
    ngpc_cpu_t& c = m.cpu;
    const uint32_t pc = c.pc;
    const uint8_t op = m.read8(pc);

    if (rec) std::memset(rec, 0, sizeof(*rec));

    switch (op) {
        case 0x00: {  // nop
            finish(rec, m, pc, 1, 2, 0);
            c.pc = (pc + 1) & kAddrMask;
            return NGPC_OK;
        }

        /* ld R8, #imm8  — 2 bytes, 3 cycles */
        case 0x20: case 0x21: case 0x22: case 0x23:
        case 0x24: case 0x25: case 0x26: case 0x27: {
            const unsigned r = op & 0x07;
            set_r8(c, r, m.read8(pc + 1));
            finish(rec, m, pc, 2, 3, 1u << (r >> 1));
            c.pc = (pc + 2) & kAddrMask;
            return NGPC_OK;
        }

        /* ld R16, #imm16 — 3 bytes, 4 cycles */
        case 0x30: case 0x31: case 0x32: case 0x33:
        case 0x34: case 0x35: case 0x36: case 0x37: {
            const unsigned r = op & 0x07;
            set_r16(c, r, fetch16(m, pc + 1));
            finish(rec, m, pc, 3, 4, 1u << r);
            c.pc = (pc + 3) & kAddrMask;
            return NGPC_OK;
        }

        /* ld R32, #imm32 — 5 bytes, 6 cycles */
        case 0x40: case 0x41: case 0x42: case 0x43:
        case 0x44: case 0x45: case 0x46: case 0x47: {
            const unsigned r = op & 0x07;
            set_r32(c, r, fetch32(m, pc + 1));
            c.pc = (pc + 5) & kAddrMask;
            finish(rec, m, pc, 5, 6, 1u << r);
            return NGPC_OK;
        }

        /* push R16 — 1 byte, 3 cycles. XSP -= 2, then store the 16-bit half.
         * (Blocks 18 of the 66 commercial ROMs until it lands.) */
        case 0x28: case 0x29: case 0x2A: case 0x2B:
        case 0x2C: case 0x2D: case 0x2E: case 0x2F: {
            const unsigned r = op & 0x07;
            if (r == NGPC_XSP) break;              // same alias hazard as push XRR
            c.pc = (pc + 1) & kAddrMask;
            c.regs[NGPC_XSP] -= 2;
            store(m, rec, c.regs[NGPC_XSP], uint16_t(c.regs[r]), 2);
            finish(rec, m, pc, 1, 3, 1u << NGPC_XSP);
            return NGPC_OK;
        }

        /* pop R16 — 1 byte, 4 cycles. Load 16 bits from XSP, then XSP += 2.
         * The high half of the 32-bit register is PRESERVED. */
        case 0x48: case 0x49: case 0x4A: case 0x4B:
        case 0x4C: case 0x4D: case 0x4E: case 0x4F: {
            const unsigned r = op & 0x07;
            if (r == NGPC_XSP) break;
            set_r16(c, r, fetch16(m, c.regs[NGPC_XSP]));
            c.regs[NGPC_XSP] += 2;
            c.pc = (pc + 1) & kAddrMask;
            finish(rec, m, pc, 1, 4, (1u << r) | (1u << NGPC_XSP));
            return NGPC_OK;
        }

        /* push R32 — 1 byte, 5 cycles. XSP -= 4, then store (little-endian).
         *
         * `push XSP` / `pop XSP` (r == 7) are DECLINED, not executed: the
         * register being pushed is also the one the push mutates, so the result
         * depends on whether silicon latches XSP before or after the
         * pre-decrement. The Python reference refuses these honestly
         * (`unmodeled-stack-pointer-alias`) and this core must not be MORE
         * capable than its reference -- that would mean inventing a behaviour.
         * Settle it against hardware/datasheet, then port it on both sides. */
        case 0x38: case 0x39: case 0x3A: case 0x3B:
        case 0x3C: case 0x3D: case 0x3E: case 0x3F: {
            const unsigned r = op & 0x07;
            if (r == NGPC_XSP) break;   // -> NGPC_UNIMPLEMENTED
            c.pc = (pc + 1) & kAddrMask;
            push32(m, rec, c.regs[r]);
            finish(rec, m, pc, 1, 5, 1u << NGPC_XSP);
            return NGPC_OK;
        }

        /* pop R32 — 1 byte, 6 cycles. Load from XSP, then XSP += 4.
         * (0x58, NOT 0x50 — an encoding this project has been bitten by before.) */
        case 0x58: case 0x59: case 0x5A: case 0x5B:
        case 0x5C: case 0x5D: case 0x5E: case 0x5F: {
            const unsigned r = op & 0x07;
            if (r == NGPC_XSP) break;   // same alias hazard -> NGPC_UNIMPLEMENTED
            set_r32(c, r, pop32(m));
            c.pc = (pc + 1) & kAddrMask;
            finish(rec, m, pc, 1, 6, (1u << r) | (1u << NGPC_XSP));
            return NGPC_OK;
        }

        /* ret — 1 byte, 9 cycles.
         * NOTE: the return address is NOT masked to 24 bits, because the Python
         * reference core does not mask it either (verified by probe: a garbage
         * stack yields PC=0x7C83085F). The reference is what gate G2 compares
         * against, so masking here would BREAK equivalence and quietly bury the
         * question. Whether silicon truncates PC to the 24-bit address bus is a
         * real open question — but it is a FIDELITY question to settle against
         * hardware/oracle, not something to "fix" unilaterally mid-port. */
        case 0x0E: {
            c.pc = pop32(m);
            finish(rec, m, pc, 1, 9, 1u << NGPC_XSP);
            return NGPC_OK;
        }

        /* calr d16 — 3 bytes, 10 cycles. Push the return address, jump PC+3+d16. */
        case 0x1E: {
            const int16_t disp = int16_t(fetch16(m, pc + 1));
            const uint32_t ret_addr = (pc + 3) & kAddrMask;
            c.pc = (ret_addr + uint32_t(int32_t(disp))) & kAddrMask;
            push32(m, rec, ret_addr);
            finish(rec, m, pc, 3, 10, 1u << NGPC_XSP);
            return NGPC_OK;
        }

        /* ld (#8), #  —  `08 + z : #8 : #`, byte and word only.
         * Datasheet list (1): length "2 + #", state "5. 6. -".
         * The 8-bit absolute address is the on-chip I/O page, so this is how a
         * cart pokes a hardware register with a constant. It is the single
         * biggest remaining blocker on the real corpus: it stops 32 of the 66
         * commercial ROMs. */
        case 0x08: case 0x0A: {
            const bool word = (op == 0x0A);
            const uint32_t addr = m.read8(pc + 1);       // I/O page, zero-extended
            const uint8_t len = word ? 4 : 3;
            const uint32_t imm = word ? fetch16(m, pc + 2) : m.read8(pc + 2);
            c.pc = (pc + len) & kAddrMask;
            store(m, rec, addr, imm, word ? 2 : 1);
            finish(rec, m, pc, len, word ? 6 : 5, 0);
            return NGPC_OK;
        }

        /* ldf #3 (0x17) · incf (0x0C) · decf (0x0D) — the REGISTER BANK window.
         *
         * Toshiba: `LDF #3` = `17 : #3`, 2 bytes, state 2, "Set a register bank.
         * RFP <- #3 (0 at reset)". `INCF` / `DECF` = 1 byte, state 2, RFP +/- 1.
         *
         * Only XWA..XHL are BANKED -- XIX, XIY, XIZ and XSP are shared across all
         * four banks. So a bank switch flushes and reloads exactly four registers.
         * Swapping all eight would silently corrupt the stack pointer on every
         * `ldf`, which is the kind of bug that only shows up ten thousand
         * instructions later. (Matches the reference: pass 84 reloads the visible
         * XWA/XBC/XDE/XHL window on POP SR and RETI, and nothing else.)
         *
         * `ldf` stops 18 of the 66 commercial ROMs across its variants. */
        case 0x17: case 0x0C: case 0x0D: {
            const uint8_t len = (op == 0x17) ? 2 : 1;
            uint8_t next_rfp;
            if (op == 0x17)      next_rfp = uint8_t(m.read8(pc + 1) & 0x03);
            else if (op == 0x0C) next_rfp = uint8_t((c.rfp + 1) & 0x03);
            else                 next_rfp = uint8_t((c.rfp + 3) & 0x03);   // decf

            if (next_rfp != c.rfp) {
                for (unsigned i = 0; i < 4; ++i) c.banks[c.rfp][i] = c.regs[i];   // flush
                c.rfp = next_rfp;
                for (unsigned i = 0; i < 4; ++i) c.regs[i] = c.banks[c.rfp][i];   // reload
            }
            c.pc = (pc + len) & kAddrMask;
            finish(rec, m, pc, len, 2, 0);
            return NGPC_OK;
        }

        /* push #8 (0x09) — `09 : #8`, 2 bytes. Pushes ONE byte (SP -= 1), the byte
         * sibling of PUSHW #16 (T900 ref: "PUSH r/R/#imm push (8/16/32)"). 4 states:
         * PUSH A is 3, this adds one immediate fetch. Missing case crashed Baseball
         * Stars (a `09 07 09 06 09 05` byte-argument push run at 0x209294). */
        case 0x09: {                                    // push #imm8
            const uint8_t imm = m.read8(pc + 1);
            c.pc = (pc + 2) & kAddrMask;
            c.regs[NGPC_XSP] -= 1;
            store(m, rec, c.regs[NGPC_XSP], imm, 1);
            finish(rec, m, pc, 2, 4, 1u << NGPC_XSP);
            return NGPC_OK;
        }

        /* pushw #16 (0x0B) · push A (0x14) · pop A (0x15) · push F (0x18) · pop F (0x19)
         * Toshiba: PUSH F / PUSH A = 3, POP F / POP A = 4, PUSHW #16 = 5. */
        case 0x0B: {                                    // pushw #imm16
            const uint16_t imm = fetch16(m, pc + 1);
            c.pc = (pc + 3) & kAddrMask;
            c.regs[NGPC_XSP] -= 2;
            store(m, rec, c.regs[NGPC_XSP], imm, 2);
            finish(rec, m, pc, 3, 5, 1u << NGPC_XSP);
            return NGPC_OK;
        }
        case 0x14: case 0x18: {                         // push A / push F
            const uint8_t v = (op == 0x14) ? get_r8(c, 1) : c.flags;   // R8 index 1 IS A
            c.pc = (pc + 1) & kAddrMask;
            c.regs[NGPC_XSP] -= 1;
            store(m, rec, c.regs[NGPC_XSP], v, 1);
            finish(rec, m, pc, 1, 3, 1u << NGPC_XSP);
            return NGPC_OK;
        }
        case 0x15: case 0x19: {                         // pop A / pop F
            const uint8_t v = m.read8(c.regs[NGPC_XSP]);
            c.regs[NGPC_XSP] += 1;
            if (op == 0x15) set_r8(c, 1, v); else c.flags = v;
            c.pc = (pc + 1) & kAddrMask;
            finish(rec, m, pc, 1, 4, 1u << NGPC_XSP);
            return NGPC_OK;
        }

        /* swi #3 (0xF8..0xFF) — the software interrupt. 1 byte, 19 states.
         * Toshiba: "PUSH PC & SR ; JP (FFFF00H + 4 x #3)" -- an INDIRECT jump
         * through the CPU's hardware vector table.
         *
         * With a real BIOS attached there is nothing to high-level-emulate: `swi 1`
         * is just a call into the BIOS through its own vector, and the BIOS code
         * runs natively. (The Python reference HLEs it, which is why the
         * differential harness cannot judge this instruction -- its synthetic ROMs
         * carry no BIOS, so the vector table reads back zero. It is verified
         * against the retail BIOS instead.)
         *
         * `swi 1` is what stops most of the remaining ROMs. */
        case 0xF8: case 0xF9: case 0xFA: case 0xFB:
        case 0xFC: case 0xFD: case 0xFE: case 0xFF: {
            const unsigned n = op & 0x07;
            const uint16_t sr = uint16_t(c.flags)
                              | uint16_t((c.rfp & 0x03) << 8)
                              | uint16_t(1u << 11)                  // MAX
                              | uint16_t((c.iff_level & 0x07) << 12)
                              | uint16_t(1u << 15);                 // SYSM
            const uint32_t ret = (pc + 1) & kAddrMask;

            /* SR goes down FIRST, then PC -- so PC ends up ON TOP, which is
             * exactly what RETI pops first. Pushing them the other way round
             * leaves RETI restoring the SR as a program counter. */
            c.regs[NGPC_XSP] -= 2;
            store(m, rec, c.regs[NGPC_XSP], sr, 2);
            c.regs[NGPC_XSP] -= 4;
            store(m, rec, c.regs[NGPC_XSP], ret, 4);

            c.pc = fetch32(m, 0xFFFF00u + 4u * n);
            finish(rec, m, pc, 1, 19, 1u << NGPC_XSP);
            return NGPC_OK;
        }

        /* halt — the CPU stops until an interrupt. Not an error: a terminal. */
        case 0x05: {
            if (rec) { rec->pc = pc; rec->raw[0] = op; rec->raw_len = 1;
                       rec->cycles = 6; rec->status = NGPC_HALTED; }
            return NGPC_HALTED;
        }

        /* push SR — 2 bytes... no: 1 byte, 3 cycles. SR is rebuilt from the
         * modelled fields, exactly as core/cpu.py:encode_sr_from_state does
         * (MAX and SYSM read as 1 on NGPC silicon). */
        case 0x02: {
            const uint16_t sr = uint16_t(c.flags)
                              | uint16_t((c.rfp & 0x03) << 8)
                              | uint16_t(1u << 11)                   // MAX
                              | uint16_t((c.iff_level & 0x07) << 12)
                              | uint16_t(1u << 15);                  // SYSM
            c.pc = (pc + 1) & kAddrMask;
            c.regs[NGPC_XSP] -= 2;
            store(m, rec, c.regs[NGPC_XSP], sr, 2);
            finish(rec, m, pc, 1, 3, 1u << NGPC_XSP);
            return NGPC_OK;
        }

        /* pop SR (0x03) and reti (0x07).
         *
         * Restoring SR restores the REGISTER BANK POINTER too, so both have to
         * reload the visible XWA..XHL window -- and only those four, since
         * XIX/XIY/XIZ/XSP are shared across banks (see the `ldf` note).
         *
         * `pop SR` stops 21 of the 66 commercial ROMs once the real BIOS is
         * attached: it is how the BIOS returns from its interrupt entry. */
        case 0x03: case 0x07: {
            const bool is_reti = (op == 0x07);
            uint32_t new_pc = 0;
            if (is_reti) {                              // RETI pops PC first, then SR
                new_pc = fetch32(m, c.regs[NGPC_XSP]);
                c.regs[NGPC_XSP] += 4;
            }
            const uint16_t sr = fetch16(m, c.regs[NGPC_XSP]);
            c.regs[NGPC_XSP] += 2;

            const uint8_t next_rfp = uint8_t((sr >> 8) & 0x03);
            if (next_rfp != c.rfp) {
                for (unsigned i = 0; i < 4; ++i) c.banks[c.rfp][i] = c.regs[i];
                c.rfp = next_rfp;
                for (unsigned i = 0; i < 4; ++i) c.regs[i] = c.banks[c.rfp][i];
            }
            c.sr_raw    = sr;
            c.flags     = uint8_t(sr & 0xFF);
            c.iff_level = uint8_t((sr >> 12) & 0x07);

            c.pc = is_reti ? new_pc : ((pc + 1) & kAddrMask);
            finish(rec, m, pc, 1, is_reti ? 12 : 4, 1u << NGPC_XSP);
            return NGPC_OK;
        }

        /* ei #n / di — 2 bytes. `di` is the encoding with n == 7 (all maskable
         * interrupts blocked); Toshiba states 3 cycles for EI, 4 for DI. */
        case 0x06: {
            const uint8_t n = m.read8(pc + 1);
            c.iff_level = n & 0x07;
            c.pc = (pc + 2) & kAddrMask;
            finish(rec, m, pc, 2, (n == 0x07) ? 4 : 3, 0);
            return NGPC_OK;
        }

        /* rcf / scf / ccf / zcf — 1 byte, 2 cycles. Carry-flag control.
         *
         * Flag effects are the Toshiba SYMBOL ROWS, not the prose:
         *     RCF  S Z H V N C = - - 0 - 0 0
         *     SCF                - - 0 - 0 1
         *     CCF                - - x - 0 *      x = "an undefined value is set"
         *     ZCF                - - x - 0 *
         * so: H is cleared by RCF/SCF and UNDEFINED after CCF/ZCF; V is never
         * touched; N is always cleared.
         *
         * (The prose under RCF in our text copy of the manual has its V and N
         * lines swapped -- it claims "V = Reset to 0 / N = No change", the exact
         * inverse of its own symbol row. DOC_SOURCES_INDEX.md §0.2 warns that
         * this document's tables do not survive extraction. Trust the symbols.)
         *
         * For CCF/ZCF we leave H as it was. Any value is conformant -- the
         * datasheet declines to specify one -- and the differential gate knows
         * not to compare a flag the reference declares undefined. */
        case 0x10: case 0x11: case 0x12: case 0x13: {
            const bool cf = flag_c(c), z = flag_z(c);
            bool new_c;
            switch (op) {
                case 0x10: new_c = false; break;  // rcf — reset carry
                case 0x11: new_c = true;  break;  // scf — set carry
                case 0x12: new_c = !cf;   break;  // ccf — complement carry
                default:   new_c = !z;    break;  // zcf — carry := inverted Z
            }
            uint8_t f = uint8_t(c.flags & ~0x03u);          // clear C and N
            if (op == 0x10 || op == 0x11) f &= uint8_t(~0x10u);  // RCF/SCF: H := 0
            if (new_c) f |= 0x01u;
            c.flags = f;
            c.pc = (pc + 1) & kAddrMask;
            finish(rec, m, pc, 1, 2, 1u << 31);
            return NGPC_OK;
        }

        /* jp #16 / jp #24 — absolute jumps. 5 / 6 cycles. */
        case 0x1A: {
            c.pc = fetch16(m, pc + 1);
            finish(rec, m, pc, 3, 5, 0);
            return NGPC_OK;
        }
        case 0x1B: {
            c.pc = fetch32(m, pc + 1) & 0x00FFFFFFu;   // 24-bit immediate
            finish(rec, m, pc, 4, 6, 0);
            return NGPC_OK;
        }

        /* call #16 / call #24 — push the return address, then jump. 9 / 10 cycles.
         * 0x1D is the single biggest blocker on the real cart corpus: it is the
         * first instruction of five of the ROMs measured. */
        case 0x1C: {
            const uint32_t target = fetch16(m, pc + 1);
            const uint32_t ret_addr = (pc + 3) & kAddrMask;
            c.pc = target;
            push32(m, rec, ret_addr);
            finish(rec, m, pc, 3, 9, 1u << NGPC_XSP);
            return NGPC_OK;
        }
        case 0x1D: {
            const uint32_t target = fetch32(m, pc + 1) & 0x00FFFFFFu;
            const uint32_t ret_addr = (pc + 4) & kAddrMask;
            c.pc = target;
            push32(m, rec, ret_addr);
            finish(rec, m, pc, 4, 10, 1u << NGPC_XSP);
            return NGPC_OK;
        }

        /* retd d16 — RET, then ADD XSP, d16 (frame teardown). 3 bytes, 11 cycles. */
        case 0x0F: {
            const int16_t adjust = int16_t(fetch16(m, pc + 1));
            c.pc = pop32(m);
            c.regs[NGPC_XSP] += uint32_t(int32_t(adjust));
            finish(rec, m, pc, 3, 11, 1u << NGPC_XSP);
            return NGPC_OK;
        }

        /* jr cc, d8 — 2 bytes. Taken 5 cycles, not taken 2. cc 8 (T) = always,
         * cc 0 (F) = never. Target is PC + 2 + sign_extend(d8). */
        case 0x60: case 0x61: case 0x62: case 0x63:
        case 0x64: case 0x65: case 0x66: case 0x67:
        case 0x68: case 0x69: case 0x6A: case 0x6B:
        case 0x6C: case 0x6D: case 0x6E: case 0x6F: {
            const int8_t disp = int8_t(m.read8(pc + 1));
            const bool taken = eval_cc(c, op & 0x0F);
            const uint32_t next = (pc + 2) & kAddrMask;
            c.pc = taken ? ((next + uint32_t(int32_t(disp))) & kAddrMask) : next;
            finish(rec, m, pc, 2, taken ? 5 : 2, 0);
            return NGPC_OK;
        }

        /* jrl cc, d16 — 3 bytes. Same cycles as jr. Target is PC + 3 + d16. */
        case 0x70: case 0x71: case 0x72: case 0x73:
        case 0x74: case 0x75: case 0x76: case 0x77:
        case 0x78: case 0x79: case 0x7A: case 0x7B:
        case 0x7C: case 0x7D: case 0x7E: case 0x7F: {
            const int16_t disp = int16_t(fetch16(m, pc + 1));
            const bool taken = eval_cc(c, op & 0x0F);
            const uint32_t next = (pc + 3) & kAddrMask;
            c.pc = taken ? ((next + uint32_t(int32_t(disp))) & kAddrMask) : next;
            finish(rec, m, pc, 3, taken ? 5 : 2, 0);
            return NGPC_OK;
        }

        default:
            break;
    }

    /* The memory-operand families (first byte >= 0x80). This is the bulk of the
     * ISA: one effective-address decoder x one sub-opcode table, replacing what
     * the Python core spreads over thousands of lines of linear chain. */
    {
        uint8_t  len = 0;
        uint16_t cyc = 0;
        uint32_t new_pc = 0;
        bool     jumped = false;
        /* 0xC7 is the BYTE extended-register escape -- it sits below 0xC8, so a
         * `>= 0xC8` guard here silently never reaches it. (It did, for one build.) */
        if (op >= 0xC7 && exec_reg_family(m, rec, op, pc, len, cyc, new_pc, jumped)) {
            if (m.pending_status != NGPC_OK) {
                const uint8_t st = m.pending_status;
                m.pending_status = NGPC_OK;
                c.pc = pc;                       /* stop WHERE IT IS */
                return st;
            }
            c.pc = jumped ? new_pc : ((pc + len) & kAddrMask);
            finish(rec, m, pc, len, cyc, 0);
            return NGPC_OK;
        }
        if (op >= 0x80 && exec_mem_family(m, rec, op, pc, len, cyc, new_pc, jumped)) {
            if (m.pending_status != NGPC_OK) {
                const uint8_t st = m.pending_status;
                m.pending_status = NGPC_OK;
                c.pc = pc;
                return st;
            }
            c.pc = jumped ? new_pc : ((pc + len) & kAddrMask);
            finish(rec, m, pc, len, cyc, 0);
            return NGPC_OK;
        }
    }

    /* Not ported yet (or a deliberately declined encoding, like the XSP push/pop
     * alias). Do NOT advance PC and do NOT invent a result.
     * HARDWARE_COMPAT_POLICY.md §9: no silent fallback. */
    if (rec) {
        rec->pc = pc;
        rec->status = NGPC_UNIMPLEMENTED;
        rec->raw[0] = op;
        rec->raw_len = 1;
    }
    return NGPC_UNIMPLEMENTED;
}

}  // namespace ngpc
