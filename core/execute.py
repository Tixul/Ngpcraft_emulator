"""Minimal real execute-next helpers for NgpCraft Emulator."""

from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path

from core.cpu import (
    BankedByteRegisters,
    GeneralRegisters32,
    NgpcCpuState,
    StatusFlags,
    Tlcs900ControlRegisters,
    create_unknown_control_registers,
    decode_f_to_flags,
    decode_sr_to_fields,
    encode_f_from_flags,
    encode_sr_from_state,
)
from core.decode import (
    CC,
    C7_REGISTER_NAMES,
    CONTROL_REGISTER_NAMES,
    control_register_name,
    DecodeResult,
    c7_current_bank_slice,
    d7_current_bank_slice,
    decode_instruction_at,
)
from core.fetch import NgpcFetchView, load_fetch_view
from core.frame_timing import ESTIMATED_CYCLES_PER_INSTRUCTION
from core.memory import MemoryReadResult
from core.quirks import KnownQuirkMatch, match_known_silicon_broken


# Per-instruction cycle cost placeholder + first populated overrides.
#
# `ESTIMATED_CYCLES_PER_INSTRUCTION` remains the fallback for any opcode
# whose TLCS-900 timing has not been populated yet. Phase 3.2.3b starts
# by overriding the common control-flow / CPU-control subset directly from
# the local Toshiba TLCS-900/L1 CPU datasheet (`900L1 Core ... Datasheet.txt`):
# NOP, EI/DI, LDF, PUSH/POP SR, SWI, JP/JR/JRL, CALL/RET/RETD/RETI,
# LINK/UNLK, plus the currently executed register/immediate arithmetic
# and transfer subset (`LD`, `LDA`, `EX`, `ADD/ADC/SUB/SBC/AND/XOR/OR/CP`,
# `INC/DEC`, `EXTZ/EXTS`). Remaining executed opcodes still use the flat
# 8-cycle placeholder until their table rows are wired in.
#
# IRQ entry cost (Toshiba TLCS-900/H spec): ~13 cycles for push PC + SR
# and load vector. We use 13 as the canonical IRQ-delivery cost for the
# `IrqDeliveryResult.cycles_consumed` field.
IRQ_DELIVERY_CYCLES = 13
NOP_CYCLES = 2
EX_FF_CYCLES = 2
EI_CYCLES = 3
DI_CYCLES = 4
CF_CPU_CONTROL_CYCLES = 2
LDX_CYCLES = 8
PUSH_SR_CYCLES = 3
POP_SR_CYCLES = 4
PUSH_F_CYCLES = 3
POP_F_CYCLES = 4
PUSH_A_CYCLES = 3
POP_A_CYCLES = 4
BLOCK_TRANSFER_CYCLES = 8       # LDI / LDD -- the single-step forms
BLOCK_COMPARE_CYCLES = 6        # CPI / CPD


def _block_repeat_cycles(iterations: int, *, compare: bool) -> int:
    """Cost of a REPEATING block instruction -- LDIR/LDDR and CPIR/CPDR.

    Toshiba instruction list (3) gives them the states **`7n + 1`** and
    **`6n + 1`** -- NOT a flat multiple of the single-step form. Billing
    `8 * n` and `6 * n` (which this core did until 2026-07-12) overcharges a
    `ldir` by one cycle per iteration and undercharges a `cpir` by one overall,
    and that error scales with the copy length. Found by the C++ differential
    harness.
    """
    per = 6 if compare else 7
    return per * iterations + 1
PUSH_R16_CYCLES = 3
PUSH_R32_CYCLES = 5
PUSHW_IMM16_CYCLES = 5
POP_R16_CYCLES = 4
POP_R32_CYCLES = 6
PUSH_PREFIX_BYTE_CYCLES = 4
PUSH_PREFIX_WORD_CYCLES = 4
PUSH_PREFIX_LONG_CYCLES = 6
POP_PREFIX_BYTE_CYCLES = 5
POP_PREFIX_WORD_CYCLES = 5
POP_PREFIX_LONG_CYCLES = 7
SWI_CYCLES = 19
HALT_CYCLES = 6
JP16_CYCLES = 5
JP24_CYCLES = 6
JP_MEM_CYCLES = 7
JR_CYCLES_TAKEN = 5
JR_CYCLES_NOT_TAKEN = 2
DJNZ_CYCLES_TAKEN = 6
DJNZ_CYCLES_NOT_TAKEN = 4
CALL16_CYCLES = 9
CALL24_CYCLES = 10
CALR_CYCLES = 10
CALL_MEM_CYCLES = 12
RET_CYCLES = 9
RETD_CYCLES = 11
RETI_CYCLES = 12
LINK_CYCLES = 8
UNLK_CYCLES = 7
LDF_CYCLES = 2
INCF_DECF_CYCLES = 2
LD_REG_REG_CYCLES = 2
LD_SMALL_IMM_CYCLES = 2
LD_IMM8_CYCLES = 3
LD_IMM16_CYCLES = 4
LD_IMM32_CYCLES = 6
LDA_CYCLES = 4

# Holes in the cycle table, found by running both cores in lockstep on real games
# and listing every instruction where this core fell back to the flat 8-cycle
# placeholder. Each value below is the manufacturer's, from the instruction lists.
LD_ABS8_IMM_CYCLES = (5, 6)            # LD<W> (#8),#        "5. 6. -"   (byte, word)
MUL_IMM_CYCLES  = (12, 15)             # MUL  rr,#           "12.15. -"
MULS_IMM_CYCLES = (10, 13)             # MULS rr,#           "10.13. -"
DIV_IMM_CYCLES  = (15, 23)             # DIV  rr,#           "15.23. -"
DIVS_IMM_CYCLES = (18, 26)             # DIVS rr,#           "18.26. -"
EX_REG_REG_CYCLES = 3
ALU_REG_REG_CYCLES = 2
ALU_IMM8_CYCLES = 3
ALU_IMM16_CYCLES = 4
ALU_IMM32_CYCLES = 6
CP_IMM3_CYCLES = 2
INCDEC_REG_CYCLES = 2
EXT_CYCLES = 3
LDC_CONTROL_REGISTER_CYCLES = 3
MEM_LOAD_BYTE_CYCLES = 4
MEM_LOAD_WORD_CYCLES = 4
MEM_LOAD_LONG_CYCLES = 6
MEM_STORE_BYTE_CYCLES = 4
MEM_STORE_WORD_CYCLES = 4
MEM_STORE_LONG_CYCLES = 6
MEM_STORE_IMM8_CYCLES = 5
MEM_STORE_IMM16_CYCLES = 6
ALU_MEM_DEST_BYTE_CYCLES = 6
ALU_MEM_DEST_WORD_CYCLES = 6
ALU_MEM_DEST_LONG_CYCLES = 10
ALU_MEM_IMM8_CYCLES = 7
ALU_MEM_IMM16_CYCLES = 8
CP_MEM_IMM8_CYCLES = 5
CP_MEM_IMM16_CYCLES = 6
INCDEC_MEM_BYTE_CYCLES = 6
INCDEC_MEM_WORD_CYCLES = 6
CF_MEM_READ_CYCLES = 6
CF_REG_CYCLES = 3
BIT_MEM_READ_CYCLES = 6
BIT_MEM_WRITE_CYCLES = 7
ROTSHIFT_MEM_BYTE_CYCLES = 6
ROTSHIFT_MEM_WORD_CYCLES = 6
REG_BIT_OP_CYCLES = 3
REG_TSET_OP_CYCLES = 4
SHIFT_IMM_BASE_CYCLES = 3
SHIFT_REG_A_CYCLES = 2
CPL_NEG_CYCLES = 2
DAA_CYCLES = 4
PAA_CYCLES = 4
MIRR_CYCLES = 3
BS1_CYCLES = 2
MULA_CYCLES = 19
MINC_CYCLES = 5
MDEC_CYCLES = 4
PUSH_MEM_WORD_CYCLES = 6


R8 = ("W", "A", "B", "C", "D", "E", "H", "L")
R16 = ("WA", "BC", "DE", "HL", "IX", "IY", "IZ", "SP")
R32 = ("XWA", "XBC", "XDE", "XHL", "XIX", "XIY", "XIZ", "XSP")
REG32_FIELDS = ("xwa", "xbc", "xde", "xhl", "xix", "xiy", "xiz", "xsp")
SEEDABLE_REGISTERS = dict(zip(R32, REG32_FIELDS))
SEEDED_BANKED_REGISTERS = dict(zip(R32[:4], REG32_FIELDS[:4]))
SEEDABLE_CONTROL_REGISTERS = {
    name.upper(): code for code, name in CONTROL_REGISTER_NAMES.items()
}
READ_ONLY_REGION_KINDS = {"rom", "rom-gap", "bios"}
_BANKED_CORE_FIELDS = REG32_FIELDS[:4]


def secondary_base_r32(before_cpu, code: int) -> tuple[str, int | None, str | None]:
    """Resolve the BASE register named by a secondary addressing byte.

    Returns `(name, value, refusal)`.  `refusal` is non-None when the code names
    something this core cannot resolve honestly, and the caller must stop rather
    than guess.

    ⚠️ THE SECONDARY BYTE IS `rrrrrrmm`: six bits of EXTENDED REGISTER CODE -- the
    same code the C7/D7/E7 escapes use -- and two bits of mode (the d16 flag, or
    the pre-dec/post-inc step).  It is NOT a bare 3-bit register number.

        0xE0..0xFF  current bank      code = 0xE0 + reg*4 + byte_pos
        0xD0..0xDF  previous bank     (XWA..XHL only)
        0x00..0x3F  absolute bank b   code = b*16 + reg*4 + byte_pos

    Reading it as `(code >> 2) & 7` is right BY ACCIDENT for a current-bank code
    (there, `(0xE0 + reg*4 + pos) >> 2 & 7` IS `reg`) and wrong for every other --
    which is why the mistake survived 72 of the corpus's 73 ROMs: compilers emit
    current-bank codes almost exclusively.  Densha de Go! 2 does not.  It walks its
    object list through bank 1's BC (`E3 14 21` = `ld XBC,(XBC1)`, code 0x14), and
    the bare decode dereferenced XIY instead -- a ROM address instead of the list
    in RAM.  The native core had the same bug; the two cores AGREED, IN ERROR, so
    the differential gate could not see it.

    The official Toshiba assembler settles the encoding:

        ld XBC,(XIY+0x1234) -> e3 f5 34 12 21     0xF5 = XIY's rcode | the d16 flag
        ld XBC,(XIY+)       -> e5 f6 21           0xF6 = XIY's rcode | step 4
    """
    rcode = code & 0xFC                       # drop the two mode bits

    if rcode >= 0xE0:                         # current bank -- XWA..XSP
        index = (rcode >> 2) & 0x07
        return (R32[index], getattr(before_cpu.regs, REG32_FIELDS[index]), None)

    if rcode >= 0xD0:                         # previous bank -- XWA..XHL only
        current = _current_register_bank_index(before_cpu)
        if current is None:
            return (
                f"code 0x{code:02X}",
                None,
                "RFP must be known before a PREVIOUS-BANK register code can name a register.",
            )
        bank = (current + 3) & 0b11
        index = (rcode >> 2) & 0x03
        return (f"{R32[index]}{bank}", _read_bank_long(before_cpu, bank, index), None)

    if rcode < 0x40:                          # absolute bank 0..3 -- XWA..XHL only
        bank = rcode >> 4
        index = (rcode & 0x0F) >> 2
        return (f"{R32[index]}{bank}", _read_bank_long(before_cpu, bank, index), None)

    # 0x40..0xCF is register-file space this core does not map to a named 32-bit
    # register.  Refuse: a guessed base register reads the wrong memory, silently.
    return (
        f"code 0x{code:02X}",
        None,
        f"Secondary-byte register code 0x{code:02X} names no 32-bit register this core can resolve.",
    )


def _read_bank_long(before_cpu, bank_index: int, r32_index: int) -> int | None:
    """Read a full 32-bit BANKED core register (XWA/XBC/XDE/XHL of any bank).

    The live bank is the one `regs` holds; its backing slots may be stale.
    """
    current = _current_register_bank_index(before_cpu)
    if current is not None and bank_index == current:
        return getattr(before_cpu.regs, REG32_FIELDS[r32_index])
    banks = _ensure_register_banks(before_cpu)
    slots = banks[bank_index].slots[r32_index * 4 : r32_index * 4 + 4]
    return _banked_owner_value_from_slots(slots)


@dataclass(frozen=True)
class MemoryWrite:
    """One contiguous memory write emitted by the current execution subset."""

    address: int
    data: bytes
    note: str


@dataclass(frozen=True)
class MemoryRead:
    """One contiguous memory read observed by the current execution subset.

    Surfaced for read-watchpoint matching. Only executors that opt in
    currently populate this; the default empty tuple is correct for any
    executor that has not been instrumented yet.
    """

    address: int
    data: bytes
    note: str


@dataclass(frozen=True)
class ExecutionResult:
    """Result of one real execution attempt from the current minimal subset."""

    before_cpu: NgpcCpuState
    after_cpu: NgpcCpuState | None
    decode: DecodeResult
    status: str
    written_registers: tuple[str, ...]
    memory_writes: tuple[MemoryWrite, ...]
    after_memory: dict[int, int] | None
    note: str
    matched_quirk: KnownQuirkMatch | None = None
    memory_reads: tuple[MemoryRead, ...] = ()
    # M3 Phase 3.2.3a/3.2.3b: per-instruction cycle cost. Defaults to
    # the shared fallback `ESTIMATED_CYCLES_PER_INSTRUCTION` for
    # unpopulated opcodes; common control-flow / CPU-control paths now
    # override this with real TLCS-900 timing. Blocked executions still
    # carry the fallback — they didn't advance state, but the run loop
    # only sums this when `status == "executed"` so blocked steps don't
    # contribute.
    cycles_consumed: int = ESTIMATED_CYCLES_PER_INSTRUCTION


@dataclass(frozen=True)
class _RuntimeOverlayDecodeBus:
    """Overlay-aware instruction-fetch view used by the executor only.

    Data reads already consult the writable runtime overlay before
    falling back to the read bus. This adapter gives instruction decode
    the same visibility so RAM-resident handlers or stubs written
    earlier in the run can execute.
    """

    base_bus: object
    overlay: dict[int, int]

    def read_bytes(self, address: int, size: int = 1) -> MemoryReadResult:
        if size <= 0:
            raise ValueError("size must be >= 1")
        if not self.overlay:
            return self.base_bus.read_bytes(address, size=size)

        chunks: list[int] = []
        first_probe = None
        for offset in range(size):
            cur_addr = _mask_address(address + offset)
            if cur_addr in self.overlay:
                probe = self.base_bus.address_space.probe(cur_addr)
                if first_probe is None:
                    first_probe = probe
                if probe.region is None:
                    return MemoryReadResult(
                        address=address,
                        width=size,
                        status="unmapped",
                        probe=probe,
                        data=None,
                        note=(
                            "Instruction fetch touched an unmapped address. "
                            "The writable runtime overlay does not make unmapped "
                            "space executable."
                        ),
                    )
                chunks.append(self.overlay[cur_addr] & 0xFF)
                continue

            read = self.base_bus.read_bytes(cur_addr, size=1)
            if first_probe is None:
                first_probe = read.probe
            if read.status != "ok" or read.data is None:
                return MemoryReadResult(
                    address=address,
                    width=size,
                    status=read.status,
                    probe=read.probe,
                    data=None,
                    note=(
                        "Instruction fetch could not be satisfied after checking the "
                        "writable runtime overlay and then the read bus."
                    ),
                )
            chunks.extend(read.data)

        assert first_probe is not None
        return MemoryReadResult(
            address=address,
            width=size,
            status="ok",
            probe=first_probe,
            data=bytes(chunks),
            note=(
                "Instruction fetch was satisfied from the writable runtime overlay "
                "and/or the current read bus."
            ),
        )


def build_execute_next(
    view: NgpcFetchView,
    start_pc: int | None = None,
    cpu_state: NgpcCpuState | None = None,
    memory_bytes: dict[int, int] | None = None,
) -> ExecutionResult:
    """Execute one instruction from the current narrow honest subset."""
    before_cpu = view.machine.cpu if cpu_state is None else cpu_state
    if start_pc is not None:
        before_cpu = replace(before_cpu, pc=start_pc)
    before_memory = {} if memory_bytes is None else dict(memory_bytes)

    _STEP_READS.clear()
    result = _dispatch_execute_next(view, before_cpu, before_memory)
    if _STEP_READS and not result.memory_reads:
        result = replace(result, memory_reads=tuple(_STEP_READS))
    elif _STEP_READS:
        # An executor already populated memory_reads explicitly (POP SR).
        # Trust the executor's own bookkeeping in that case.
        pass
    return result


def _dispatch_execute_next(
    view: NgpcFetchView,
    before_cpu: NgpcCpuState,
    before_memory: dict[int, int],
) -> ExecutionResult:
    decoded = decode_instruction_at(
        _RuntimeOverlayDecodeBus(view.bus, before_memory),
        before_cpu.pc,
    )
    if decoded.status != "decoded":
        return _blocked_result(
            before_cpu=before_cpu,
            decoded=decoded,
            status=decoded.status,
            note=(
                "Execution could not begin because the current instruction could not be "
                "decoded at the requested address."
            ),
        )

    silicon_broken_result = _try_stop_known_silicon_broken(
        before_cpu=before_cpu,
        decoded=decoded,
    )
    if silicon_broken_result is not None:
        return silicon_broken_result

    d8_copy_result = _try_execute_d8_df_register_copy(
        before_cpu=before_cpu,
        before_memory=before_memory,
        decoded=decoded,
    )
    if d8_copy_result is not None:
        return d8_copy_result

    if decoded.mnemonic == "nop":
        return _executed_result(
            before_cpu=before_cpu,
            decoded=decoded,
            written_registers=("PC",),
            memory_writes=(),
            after_memory=before_memory,
            new_pc=decoded.next_sequential_pc,
            reg_updates=None,
            note=(
                "Executed NOP from the current real execution subset. Only PC advanced to the "
                "next sequential address."
            ),
        )

    if decoded.mnemonic == "halt":
        return _halted_result(
            before_cpu=before_cpu,
            decoded=decoded,
            after_memory=before_memory,
            note=(
                "Executed HALT from the current real execution subset. PC advanced to the next "
                "sequential address, then the CPU entered the halted state and now requires an "
                "interrupt to resume."
            ),
        )

    load_result = _try_execute_load_immediate(before_cpu, before_memory, decoded)
    if load_result is not None:
        return load_result

    lda_result = _try_execute_lda_absolute(before_cpu, before_memory, decoded)
    if lda_result is not None:
        return lda_result

    b0_memory_result = _try_execute_b0_memory(
        view=view,
        before_cpu=before_cpu,
        before_memory=before_memory,
        decoded=decoded,
    )
    if b0_memory_result is not None:
        return b0_memory_result

    abs8_long_memory_result = _try_execute_abs8_long_memory(
        view=view,
        before_cpu=before_cpu,
        before_memory=before_memory,
        decoded=decoded,
    )
    if abs8_long_memory_result is not None:
        return abs8_long_memory_result

    pre_decrement_result = _try_execute_pre_decrement_load(
        view=view,
        before_cpu=before_cpu,
        before_memory=before_memory,
        decoded=decoded,
    )
    if pre_decrement_result is not None:
        return pre_decrement_result

    cpu_io_result = _try_execute_cpu_io_store(
        before_cpu=before_cpu,
        before_memory=before_memory,
        decoded=decoded,
    )
    if cpu_io_result is not None:
        return cpu_io_result

    ldx_result = _try_execute_ldx_direct(
        view=view,
        before_cpu=before_cpu,
        before_memory=before_memory,
        decoded=decoded,
    )
    if ldx_result is not None:
        return ldx_result

    abs16_word_memory_result = _try_execute_abs16_word_memory(
        view=view,
        before_cpu=before_cpu,
        before_memory=before_memory,
        decoded=decoded,
    )
    if abs16_word_memory_result is not None:
        return abs16_word_memory_result

    abs8_word_memory_result = _try_execute_abs8_word_memory(
        view=view,
        before_cpu=before_cpu,
        before_memory=before_memory,
        decoded=decoded,
    )
    if abs8_word_memory_result is not None:
        return abs8_word_memory_result

    abs24_word_memory_result = _try_execute_abs24_word_memory(
        view=view,
        before_cpu=before_cpu,
        before_memory=before_memory,
        decoded=decoded,
    )
    if abs24_word_memory_result is not None:
        return abs24_word_memory_result

    abs24_long_memory_result = _try_execute_abs24_long_memory(
        view=view,
        before_cpu=before_cpu,
        before_memory=before_memory,
        decoded=decoded,
    )
    if abs24_long_memory_result is not None:
        return abs24_long_memory_result

    abs16_byte_memory_result = _try_execute_abs16_byte_memory(
        view=view,
        before_cpu=before_cpu,
        before_memory=before_memory,
        decoded=decoded,
    )
    if abs16_byte_memory_result is not None:
        return abs16_byte_memory_result

    lda_abs8_result = _try_execute_lda_abs8(before_cpu, before_memory, decoded)
    if lda_abs8_result is not None:
        return lda_abs8_result

    prefixed_ld_result = _try_execute_prefixed_register_ld(before_cpu, before_memory, decoded)
    if prefixed_ld_result is not None:
        return prefixed_ld_result

    prefixed_push_pop_result = _try_execute_prefixed_push_pop(
        view=view,
        before_cpu=before_cpu,
        before_memory=before_memory,
        decoded=decoded,
    )
    if prefixed_push_pop_result is not None:
        return prefixed_push_pop_result

    prefixed_compare_result = _try_execute_prefixed_compare(before_cpu, before_memory, decoded)
    if prefixed_compare_result is not None:
        return prefixed_compare_result

    c7_ext_result = _try_execute_c7_extended_register(
        view=view,
        before_cpu=before_cpu,
        before_memory=before_memory,
        decoded=decoded,
    )
    if c7_ext_result is not None:
        return c7_ext_result

    e7_ext_result = _try_execute_e7_extended_register(
        view=view,
        before_cpu=before_cpu,
        before_memory=before_memory,
        decoded=decoded,
    )
    if e7_ext_result is not None:
        return e7_ext_result

    d7_ext_result = _try_execute_d7_extended_register(
        view=view,
        before_cpu=before_cpu,
        before_memory=before_memory,
        decoded=decoded,
    )
    if d7_ext_result is not None:
        return d7_ext_result

    swi_result = _try_execute_swi(
        view=view,
        before_cpu=before_cpu,
        before_memory=before_memory,
        decoded=decoded,
    )
    if swi_result is not None:
        return swi_result

    ei_di_result = _try_execute_ei_di(
        before_cpu=before_cpu,
        before_memory=before_memory,
        decoded=decoded,
    )
    if ei_di_result is not None:
        return ei_di_result

    carry_cpu_control_result = _try_execute_cpu_carry_flag_control(
        before_cpu=before_cpu,
        before_memory=before_memory,
        decoded=decoded,
    )
    if carry_cpu_control_result is not None:
        return carry_cpu_control_result

    ex_ff_result = _try_execute_ex_ff(
        before_cpu=before_cpu,
        before_memory=before_memory,
        decoded=decoded,
    )
    if ex_ff_result is not None:
        return ex_ff_result

    ldf_result = _try_execute_ldf(
        before_cpu=before_cpu,
        before_memory=before_memory,
        decoded=decoded,
    )
    if ldf_result is not None:
        return ldf_result

    incf_decf_result = _try_execute_incf_decf(
        before_cpu=before_cpu,
        before_memory=before_memory,
        decoded=decoded,
    )
    if incf_decf_result is not None:
        return incf_decf_result

    push_pop_sr_result = _try_execute_push_pop_sr(
        view=view,
        before_cpu=before_cpu,
        before_memory=before_memory,
        decoded=decoded,
    )
    if push_pop_sr_result is not None:
        return push_pop_sr_result

    reti_result = _try_execute_reti(
        view=view,
        before_cpu=before_cpu,
        before_memory=before_memory,
        decoded=decoded,
    )
    if reti_result is not None:
        return reti_result

    arithmetic_result = _try_execute_prefixed_inc_dec(
        before_cpu=before_cpu,
        before_memory=before_memory,
        decoded=decoded,
    )
    if arithmetic_result is not None:
        return arithmetic_result

    alu_reg_result = _try_execute_prefixed_alu_register(
        before_cpu=before_cpu,
        before_memory=before_memory,
        decoded=decoded,
    )
    if alu_reg_result is not None:
        return alu_reg_result

    reg_muldiv_result = _try_execute_prefixed_register_muldiv(
        before_cpu=before_cpu,
        before_memory=before_memory,
        decoded=decoded,
    )
    if reg_muldiv_result is not None:
        return reg_muldiv_result

    byte_muldiv_result = _try_execute_prefixed_byte_muldiv(
        before_cpu=before_cpu,
        before_memory=before_memory,
        decoded=decoded,
    )
    if byte_muldiv_result is not None:
        return byte_muldiv_result

    link_unlk_result = _try_execute_link_unlk(
        view=view,
        before_cpu=before_cpu,
        before_memory=before_memory,
        decoded=decoded,
    )
    if link_unlk_result is not None:
        return link_unlk_result

    shift_imm_result = _try_execute_prefixed_shift_imm(
        before_cpu=before_cpu,
        before_memory=before_memory,
        decoded=decoded,
    )
    if shift_imm_result is not None:
        return shift_imm_result

    shift_a_result = _try_execute_prefixed_shift_a(
        before_cpu=before_cpu,
        before_memory=before_memory,
        decoded=decoded,
    )
    if shift_a_result is not None:
        return shift_a_result

    cp_imm3_result = _try_execute_prefixed_cp_imm3(
        before_cpu=before_cpu,
        before_memory=before_memory,
        decoded=decoded,
    )
    if cp_imm3_result is not None:
        return cp_imm3_result

    bit_mutation_result = _try_execute_prefixed_bit_mutation(
        before_cpu=before_cpu,
        before_memory=before_memory,
        decoded=decoded,
    )
    if bit_mutation_result is not None:
        return bit_mutation_result

    bit_test_result = _try_execute_prefixed_bit_test(
        before_cpu=before_cpu,
        before_memory=before_memory,
        decoded=decoded,
    )
    if bit_test_result is not None:
        return bit_test_result

    carry_flag_reg_result = _try_execute_prefixed_carry_flag_register(
        before_cpu=before_cpu,
        before_memory=before_memory,
        decoded=decoded,
    )
    if carry_flag_reg_result is not None:
        return carry_flag_reg_result

    alu_imm_result = _try_execute_prefixed_alu_immediate(
        before_cpu=before_cpu,
        before_memory=before_memory,
        decoded=decoded,
    )
    if alu_imm_result is not None:
        return alu_imm_result

    word_muldiv_imm_result = _try_execute_prefixed_word_muldiv_immediate(
        before_cpu=before_cpu,
        before_memory=before_memory,
        decoded=decoded,
    )
    if word_muldiv_imm_result is not None:
        return word_muldiv_imm_result

    divide_imm_result = _try_execute_prefixed_divide_immediate(
        before_cpu=before_cpu,
        before_memory=before_memory,
        decoded=decoded,
    )
    if divide_imm_result is not None:
        return divide_imm_result

    multiply_imm_result = _try_execute_prefixed_multiply_immediate(
        before_cpu=before_cpu,
        before_memory=before_memory,
        decoded=decoded,
    )
    if multiply_imm_result is not None:
        return multiply_imm_result

    daa_result = _try_execute_prefixed_daa(
        before_cpu=before_cpu,
        before_memory=before_memory,
        decoded=decoded,
    )
    if daa_result is not None:
        return daa_result

    paa_result = _try_execute_prefixed_paa(
        before_cpu=before_cpu,
        before_memory=before_memory,
        decoded=decoded,
    )
    if paa_result is not None:
        return paa_result

    mirr_result = _try_execute_prefixed_mirr(
        before_cpu=before_cpu,
        before_memory=before_memory,
        decoded=decoded,
    )
    if mirr_result is not None:
        return mirr_result

    mula_result = _try_execute_prefixed_mula(
        view=view,
        before_cpu=before_cpu,
        before_memory=before_memory,
        decoded=decoded,
    )
    if mula_result is not None:
        return mula_result

    modulo_adjust_result = _try_execute_prefixed_modulo_adjust(
        before_cpu=before_cpu,
        before_memory=before_memory,
        decoded=decoded,
    )
    if modulo_adjust_result is not None:
        return modulo_adjust_result

    bs1_result = _try_execute_prefixed_bs1(
        before_cpu=before_cpu,
        before_memory=before_memory,
        decoded=decoded,
    )
    if bs1_result is not None:
        return bs1_result

    cpl_neg_result = _try_execute_prefixed_cpl_neg(
        before_cpu=before_cpu,
        before_memory=before_memory,
        decoded=decoded,
    )
    if cpl_neg_result is not None:
        return cpl_neg_result

    ext_result = _try_execute_prefixed_ext(
        before_cpu=before_cpu,
        before_memory=before_memory,
        decoded=decoded,
    )
    if ext_result is not None:
        return ext_result

    ldc_result = _try_execute_prefixed_ldc(
        before_cpu=before_cpu,
        before_memory=before_memory,
        decoded=decoded,
    )
    if ldc_result is not None:
        return ldc_result

    block_result = _try_execute_nonrepeat_block_memory(
        view=view,
        before_cpu=before_cpu,
        before_memory=before_memory,
        decoded=decoded,
    )
    if block_result is not None:
        return block_result

    repeat_block_result = _try_execute_repeat_block_memory(
        view=view,
        before_cpu=before_cpu,
        before_memory=before_memory,
        decoded=decoded,
    )
    if repeat_block_result is not None:
        return repeat_block_result

    reg_indirect_load_result = _try_execute_reg_indirect_load(
        view=view,
        before_cpu=before_cpu,
        before_memory=before_memory,
        decoded=decoded,
    )
    if reg_indirect_load_result is not None:
        return reg_indirect_load_result

    reg_indirect_word_result = _try_execute_reg_indirect_word(
        view=view,
        before_cpu=before_cpu,
        before_memory=before_memory,
        decoded=decoded,
    )
    if reg_indirect_word_result is not None:
        return reg_indirect_word_result

    reg_indirect_long_result = _try_execute_reg_indirect_long(
        view=view,
        before_cpu=before_cpu,
        before_memory=before_memory,
        decoded=decoded,
    )
    if reg_indirect_long_result is not None:
        return reg_indirect_long_result

    reg_indirect_store_result = _try_execute_reg_indirect_store(
        view=view,
        before_cpu=before_cpu,
        before_memory=before_memory,
        decoded=decoded,
    )
    if reg_indirect_store_result is not None:
        return reg_indirect_store_result

    indexed_store_result = _try_execute_indexed_store(
        view=view,
        before_cpu=before_cpu,
        before_memory=before_memory,
        decoded=decoded,
    )
    if indexed_store_result is not None:
        return indexed_store_result

    indexed_imm_store_result = _try_execute_indexed_imm_store(
        view=view,
        before_cpu=before_cpu,
        before_memory=before_memory,
        decoded=decoded,
    )
    if indexed_imm_store_result is not None:
        return indexed_imm_store_result

    indexed_load_result = _try_execute_indexed_load(
        view=view,
        before_cpu=before_cpu,
        before_memory=before_memory,
        decoded=decoded,
    )
    if indexed_load_result is not None:
        return indexed_load_result

    secondary_indexed_load_result = _try_execute_secondary_indexed_load(
        view=view,
        before_cpu=before_cpu,
        before_memory=before_memory,
        decoded=decoded,
    )
    if secondary_indexed_load_result is not None:
        return secondary_indexed_load_result

    secondary_indexed_jump_result = _try_execute_secondary_indexed_jump(
        before_cpu=before_cpu,
        before_memory=before_memory,
        decoded=decoded,
    )
    if secondary_indexed_jump_result is not None:
        return secondary_indexed_jump_result

    secondary_indexed_bit_result = _try_execute_secondary_indexed_bit(
        view=view,
        before_cpu=before_cpu,
        before_memory=before_memory,
        decoded=decoded,
    )
    if secondary_indexed_bit_result is not None:
        return secondary_indexed_bit_result

    indexed_muldiv_result = _try_execute_indexed_word_muldiv(
        view=view,
        before_cpu=before_cpu,
        before_memory=before_memory,
        decoded=decoded,
    )
    if indexed_muldiv_result is not None:
        return indexed_muldiv_result

    indexed_push_result = _try_execute_indexed_push(
        view=view,
        before_cpu=before_cpu,
        before_memory=before_memory,
        decoded=decoded,
    )
    if indexed_push_result is not None:
        return indexed_push_result

    post_increment_result = _try_execute_post_increment_byte(
        view=view,
        before_cpu=before_cpu,
        before_memory=before_memory,
        decoded=decoded,
    )
    if post_increment_result is not None:
        return post_increment_result

    indexed_word_misc_result = _try_execute_indexed_word_misc(
        view=view,
        before_cpu=before_cpu,
        before_memory=before_memory,
        decoded=decoded,
    )
    if indexed_word_misc_result is not None:
        return indexed_word_misc_result

    indexed_byte_incdec_result = _try_execute_indexed_byte_incdec(
        view=view,
        before_cpu=before_cpu,
        before_memory=before_memory,
        decoded=decoded,
    )
    if indexed_byte_incdec_result is not None:
        return indexed_byte_incdec_result

    indexed_long_misc_result = _try_execute_indexed_long_misc(
        view=view,
        before_cpu=before_cpu,
        before_memory=before_memory,
        decoded=decoded,
    )
    if indexed_long_misc_result is not None:
        return indexed_long_misc_result

    indexed_byte_alu_result = _try_execute_indexed_byte_alu(
        view=view,
        before_cpu=before_cpu,
        before_memory=before_memory,
        decoded=decoded,
    )
    if indexed_byte_alu_result is not None:
        return indexed_byte_alu_result

    indexed_byte_alu_imm_result = _try_execute_indexed_byte_alu_immediate(
        view=view,
        before_cpu=before_cpu,
        before_memory=before_memory,
        decoded=decoded,
    )
    if indexed_byte_alu_imm_result is not None:
        return indexed_byte_alu_imm_result

    indexed_word_alu_result = _try_execute_indexed_word_alu(
        view=view,
        before_cpu=before_cpu,
        before_memory=before_memory,
        decoded=decoded,
    )
    if indexed_word_alu_result is not None:
        return indexed_word_alu_result

    indexed_long_alu_result = _try_execute_indexed_long_alu(
        view=view,
        before_cpu=before_cpu,
        before_memory=before_memory,
        decoded=decoded,
    )
    if indexed_long_alu_result is not None:
        return indexed_long_alu_result

    indexed_rmw_add_result = _try_execute_indexed_rmw_add(
        view=view,
        before_cpu=before_cpu,
        before_memory=before_memory,
        decoded=decoded,
    )
    if indexed_rmw_add_result is not None:
        return indexed_rmw_add_result

    indexed_cp_imm_result = _try_execute_indexed_cp_immediate(
        view=view,
        before_cpu=before_cpu,
        before_memory=before_memory,
        decoded=decoded,
    )
    if indexed_cp_imm_result is not None:
        return indexed_cp_imm_result

    indexed_compare_result = _try_execute_indexed_compare(
        view=view,
        before_cpu=before_cpu,
        before_memory=before_memory,
        decoded=decoded,
    )
    if indexed_compare_result is not None:
        return indexed_compare_result

    stack_result = _try_execute_stack_or_call(
        view=view,
        before_cpu=before_cpu,
        before_memory=before_memory,
        decoded=decoded,
    )
    if stack_result is not None:
        return stack_result

    if decoded.control_flow_kind == "jump" and decoded.direct_target is not None:
        return _executed_result(
            before_cpu=before_cpu,
            decoded=decoded,
            written_registers=("PC",),
            memory_writes=(),
            after_memory=before_memory,
            new_pc=decoded.direct_target,
            reg_updates=None,
            # An UNCONDITIONAL jump is, by definition, taken. Without this the
            # cycle resolver sees `branch_taken=None` and falls back to the flat
            # 8-cycle placeholder -- so `jr T, d8` (0x68, decoded as kind="jump")
            # cost 8 while the *same* instruction taken via a real condition cost
            # JR_CYCLES_TAKEN = 5. Two prices for one behaviour.
            #
            # Found 2026-07-11 by the C++ differential harness
            # (oracle_tools/native_diff.py), which is the first thing in this
            # project to compare cycle counts opcode by opcode.
            #
            # 5 is the MANUFACTURER value: Toshiba TLCS-900/L1 instruction list
            # (9) "Jump, Call and Return" gives `JR [cc,] $+2+d8` -> State
            # "5/2 (T/F)". (Mednafen's table and NeoPop both say 8/4 here; they
            # are hand-tuned and lose to the datasheet -- same precedent as the
            # pass-184 timer-rate ruling.)
            cycles_consumed=_executed_cycles_from_decoded(decoded, branch_taken=True),
            note=(
                "Executed a direct unconditional jump from the current real execution subset. "
                "PC now points to the decoded direct target."
            ),
        )

    conditional_branch_result = _try_execute_conditional_branch(before_cpu, before_memory, decoded)
    if conditional_branch_result is not None:
        return conditional_branch_result

    abs_jump_result = _try_execute_abs_conditional_jump(before_cpu, before_memory, decoded)
    if abs_jump_result is not None:
        return abs_jump_result

    ret_cond_result = _try_execute_ret_conditional(
        view=view,
        before_cpu=before_cpu,
        before_memory=before_memory,
        decoded=decoded,
    )
    if ret_cond_result is not None:
        return ret_cond_result

    if decoded.control_flow_kind in {"conditional-return", "conditional-branch"}:
        return _blocked_result(
            before_cpu=before_cpu,
            decoded=decoded,
            status="runtime-state-required",
            note=(
                "This branch depends on runtime flag state, which the current minimal execution "
                "subset does not model well enough to choose honestly."
            ),
        )

    if decoded.control_flow_kind == "interrupt":
        return _blocked_result(
            before_cpu=before_cpu,
            decoded=decoded,
            status="unmodeled-side-effects",
            note=(
                "This instruction decoded successfully but requires interrupt, halt or other "
                "side effects that are not modeled in the current real execution subset."
            ),
        )

    return _blocked_result(
        before_cpu=before_cpu,
        decoded=decoded,
        status="unsupported-decoded-instruction",
        note=(
            "The instruction decoded successfully, but its state effects are not implemented in "
            "the current real execution subset yet."
        ),
    )


def load_execute_next(
    path: str | Path,
    start_pc: int | None = None,
    seed_xsp: int | None = None,
    seed_registers: dict[str, int] | None = None,
    bios_path: str | Path | None = None,
) -> ExecutionResult:
    """Load a ROM and execute one instruction from the current minimal subset."""
    view = load_fetch_view(path, bios_path=bios_path)
    cpu_state = view.machine.cpu
    if seed_xsp is not None or seed_registers:
        cpu_state = seed_cpu_state_for_execution(
            cpu_state,
            register_values=seed_registers,
            seed_xsp=seed_xsp,
        )
    return build_execute_next(view=view, start_pc=start_pc, cpu_state=cpu_state)


def _try_execute_load_immediate(
    before_cpu: NgpcCpuState,
    before_memory: dict[int, int],
    decoded: DecodeResult,
) -> ExecutionResult | None:
    raw = decoded.raw_bytes
    if raw is None:
        return None

    if 0x20 <= raw[0] <= 0x27 and len(raw) == 2:
        return _execute_register_immediate(
            before_cpu=before_cpu,
            before_memory=before_memory,
            decoded=decoded,
            size_kind="byte",
            register_index=raw[0] & 0x07,
            value=raw[1],
        )

    if 0x30 <= raw[0] <= 0x37 and len(raw) == 3:
        return _execute_register_immediate(
            before_cpu=before_cpu,
            before_memory=before_memory,
            decoded=decoded,
            size_kind="word",
            register_index=raw[0] & 0x07,
            value=int.from_bytes(raw[1:3], "little"),
        )

    if 0x40 <= raw[0] <= 0x47 and len(raw) == 5:
        return _execute_register_immediate(
            before_cpu=before_cpu,
            before_memory=before_memory,
            decoded=decoded,
            size_kind="long",
            register_index=raw[0] & 0x07,
            value=int.from_bytes(raw[1:5], "little"),
        )

    if raw[0] in range(0xC8, 0xD0) and len(raw) == 3 and raw[1] == 0x03:
        return _execute_register_immediate(
            before_cpu=before_cpu,
            before_memory=before_memory,
            decoded=decoded,
            size_kind="byte",
            register_index=raw[0] & 0x07,
            value=raw[2],
        )

    if raw[0] in range(0xD0, 0xD8) and len(raw) == 4 and raw[1] == 0x03:
        return _execute_register_immediate(
            before_cpu=before_cpu,
            before_memory=before_memory,
            decoded=decoded,
            size_kind="word",
            register_index=raw[0] & 0x07,
            value=int.from_bytes(raw[2:4], "little"),
        )

    if raw[0] in tuple(range(0xD8, 0xE0)) + tuple(range(0xE8, 0xF0)) and len(raw) == 6 and raw[1] == 0x03:
        return _execute_register_immediate(
            before_cpu=before_cpu,
            before_memory=before_memory,
            decoded=decoded,
            size_kind="long",
            register_index=raw[0] & 0x07,
            value=int.from_bytes(raw[2:6], "little"),
        )

    # ld r, #3 — compact 2-byte small-immediate load (catalog: C8+zz+r : A8+#3).
    # The 3-bit immediate value (0..7) is embedded in the lower bits of the second byte.
    # C8..CF = byte register, D0..D7 = word register, D8..DF = long register.
    if len(raw) == 2 and 0xA8 <= raw[1] <= 0xAF:
        info = _prefixed_register_execute_info(raw[0])
        if info is not None:
            size_kind, register_index = info
            return _execute_register_immediate(
                before_cpu=before_cpu,
                before_memory=before_memory,
                decoded=decoded,
                size_kind=size_kind,
                register_index=register_index,
                value=raw[1] & 0x07,
                note=(
                    "Executed prefixed small-immediate load (ld r, #3) from the current real "
                    "execution subset. The 3-bit value embedded in the opcode was written to "
                    "the destination register."
                ),
            )

    return None


def _try_execute_lda_absolute(
    before_cpu: NgpcCpuState,
    before_memory: dict[int, int],
    decoded: DecodeResult,
) -> ExecutionResult | None:
    raw = decoded.raw_bytes
    if raw is None:
        return None

    # F2 abs24 lda (5 bytes, op at raw[4]) or F1 abs16 lda (4 bytes, op at raw[3]).
    # Hanafuda frontier `F1 B8 6F 35` = lda XIY, (0x6FB8).
    #
    # The DESTINATION group's sub-op table gives LDA **two** rows, one per register
    # width (specs/TLCS900_MEMORY_FAMILY.md):
    #
    #     0x20..0x27   LDA R, mem   -- WORD register
    #     0x30..0x37   LDA R, mem   -- LONG register
    #
    # Only the long row was here. The word row was executed as `ld R8, (abs16)` --
    # a byte LOAD inherited from gb2t900 -- so it fetched the CONTENTS of the
    # address instead of the address itself. Puyo Pop is what it cost: `F1 FF 0F 20`
    # is `lda WA, (0x0FFF)` and must leave WA = 0x0FFF; the reference left it 0.
    # Gate G3 caught the two cores disagreeing on exactly that register.
    size_kind = "long"
    if len(raw) == 5 and raw[0] == 0xF2 and 0x30 <= raw[4] <= 0x37:
        target_address = int.from_bytes(raw[1:4], "little") & 0xFFFFFF
        register_index = raw[4] & 0x07
    elif len(raw) == 4 and raw[0] == 0xF1 and 0x30 <= raw[3] <= 0x37:
        target_address = int.from_bytes(raw[1:3], "little") & 0xFFFF
        register_index = raw[3] & 0x07
    elif len(raw) == 4 and raw[0] == 0xF1 and 0x20 <= raw[3] <= 0x27:
        target_address = int.from_bytes(raw[1:3], "little") & 0xFFFF
        register_index = raw[3] & 0x07
        size_kind = "word"
    elif len(raw) == 3 and raw[0] == 0xF0 and 0x20 <= raw[2] <= 0x27:
        target_address = raw[1]
        register_index = raw[2] & 0x07
        size_kind = "word"
    elif len(raw) == 3 and raw[0] == 0xF0 and 0x30 <= raw[2] <= 0x37:
        target_address = raw[1]
        register_index = raw[2] & 0x07
    else:
        return None

    return _execute_register_immediate(
        before_cpu=before_cpu,
        before_memory=before_memory,
        decoded=decoded,
        size_kind=size_kind,
        register_index=register_index,
        value=target_address,
        note=(
            "Executed LDA absolute from the current real execution subset. The destination "
            "register now holds the decoded effective address value, not the memory "
            "contents at that address."
        ),
    )


def _try_execute_b0_memory(
    view: NgpcFetchView,
    before_cpu: NgpcCpuState,
    before_memory: dict[int, int],
    decoded: DecodeResult,
) -> ExecutionResult | None:
    def execute_absolute_bit_operation(
        *,
        target_address: int,
        op_byte: int,
        width_label: str,
    ) -> ExecutionResult:
        source_data = _read_runtime_bytes(view, before_memory, target_address, 1)
        if source_data is None:
            return _blocked_result(
                before_cpu=before_cpu,
                decoded=decoded,
                status="runtime-memory-unavailable",
                note=(
                    f"This {width_label} bit-manipulation instruction needs a readable source byte, "
                    "but neither the writable runtime overlay nor the current read bus can provide it."
                ),
            )

        old_value = source_data[0]
        bit_index = op_byte & 0x07
        bit_mask = 1 << bit_index
        op_base = op_byte & 0xF8

        if op_base == 0xC8:
            return _executed_result(
                before_cpu=before_cpu,
                decoded=decoded,
                written_registers=("PC",),
                memory_writes=(),
                after_memory=before_memory,
                new_pc=decoded.next_sequential_pc,
                reg_updates=None,
                flags_updates={
                    "zf": (old_value & bit_mask) == 0,
                    "hf": True,
                    "nf": False,
                },
                note=(
                    f"Executed {width_label} BIT bit test from the current real execution subset. "
                    f"Bit {bit_index} of mem8(0x{target_address:06X}) determined Z, while H=1 and N=0."
                ),
            )

        if op_base == 0xA8:
            new_value = old_value | bit_mask
            # TSET writes H and N too, exactly like the BIT branch above it.
            # Toshiba's symbol row for `TSET #3, (mem)` is `x * 1 x 0 -`:
            #   S undefined, Z = inverted bit, **H = 1**, V undefined, **N = 0**,
            #   C unchanged.
            # This branch was setting Z alone and leaving H and N at whatever the
            # caller had, which is why `tset` was the ONE bit-op that diverged.
            # (Found 2026-07-12 by the C++ differential harness.)
            flags_updates = {
                "zf": (old_value & bit_mask) == 0,
                "hf": True,
                "nf": False,
            }
            action_name = "TSET"
        elif op_base == 0xB0:
            new_value = old_value & (~bit_mask & 0xFF)
            flags_updates = None
            action_name = "RES"
        elif op_base == 0xB8:
            new_value = old_value | bit_mask
            flags_updates = None
            action_name = "SET"
        elif op_base == 0xC0:
            new_value = old_value ^ bit_mask
            flags_updates = None
            action_name = "CHG"
        else:
            return _blocked_result(
                before_cpu=before_cpu,
                decoded=decoded,
                status="not-yet-modeled",
                note=f"{width_label} bit opcode 0x{op_byte:02X} is not modeled yet.",
            )

        write_status, write_note = _check_writable_range(view, target_address, 1)
        if write_status == "write-discarded":
            return _executed_result(
                before_cpu=before_cpu,
                decoded=decoded,
                written_registers=("PC",),
                memory_writes=(
                    MemoryWrite(
                        address=target_address,
                        data=bytes((new_value & 0xFF,)),
                        note=f"[DISCARDED] {write_note}",
                    ),
                ),
                after_memory=before_memory,
                new_pc=decoded.next_sequential_pc,
                reg_updates=None,
                flags_updates=flags_updates,
                note=(
                    f"{width_label} {action_name} destination was unmapped or read-only; write "
                    "silently discarded (open-bus behavior - execution continues)."
                ),
            )
        if write_status is not None:
            return _blocked_result(
                before_cpu=before_cpu,
                decoded=decoded,
                status=write_status,
                note=write_note,
            )

        after_memory = dict(before_memory)
        after_memory[target_address] = new_value & 0xFF
        return _executed_result(
            before_cpu=before_cpu,
            decoded=decoded,
            written_registers=("PC",),
            memory_writes=(
                MemoryWrite(
                    address=target_address,
                    data=bytes((new_value & 0xFF,)),
                    note=f"Writable runtime overlay updated by {width_label} {action_name} execution.",
                ),
            ),
            after_memory=after_memory,
            new_pc=decoded.next_sequential_pc,
            reg_updates=None,
            flags_updates=flags_updates,
            note=(
                f"Executed {width_label} {action_name} bit operation: mem8(0x{target_address:06X}) "
                f"0x{old_value:02X} -> 0x{new_value & 0xFF:02X}."
            ),
        )

    def execute_absolute_cf_operation(
        *,
        target_address: int,
        op_byte: int,
        width_label: str,
    ) -> ExecutionResult:
        source_data = _read_runtime_bytes(view, before_memory, target_address, 1)
        if source_data is None:
            return _blocked_result(
                before_cpu=before_cpu,
                decoded=decoded,
                status="runtime-memory-unavailable",
                note=(
                    f"This {width_label} carry-flag memory instruction needs a readable source byte, "
                    "but neither the writable runtime overlay nor the current read bus can provide it."
                ),
            )

        mem_value = source_data[0]
        if 0x28 <= op_byte <= 0x2C:
            a_name, a_value = _extract_register_value(before_cpu, "byte", 1)
            if a_value is None:
                return _blocked_result(
                    before_cpu=before_cpu,
                    decoded=decoded,
                    status="requires-known-full-register",
                    note=(
                        f"{a_name} must be known before this {width_label} carry-flag memory "
                        "instruction can derive its dynamic bit index."
                    ),
                )
            bit_index = a_value & 0x0F
        else:
            bit_index = op_byte & 0x07

        if bit_index >= 8:
            return _blocked_result(
                before_cpu=before_cpu,
                decoded=decoded,
                status="silicon-undefined",
                note=(
                    f"{width_label} carry-flag memory bit index {bit_index} is undefined for a "
                    "byte operand on TLCS-900/H."
                ),
            )

        bit_value = (mem_value >> bit_index) & 1
        bit_mask = 1 << bit_index

        if (
            op_byte in (0x28, 0x29, 0x2A, 0x2C)
            or (op_byte & 0xF8) in (0x80, 0x88, 0x90, 0xA0)
        ) and before_cpu.flags.cf is None:
            return _blocked_result(
                before_cpu=before_cpu,
                decoded=decoded,
                status="runtime-state-required",
                note=(
                    f"This {width_label} carry-flag memory instruction needs the carry flag "
                    "known in the current CPU state."
                ),
            )

        if op_byte in (0x2B,) or (op_byte & 0xF8) == 0x98:
            return _executed_result(
                before_cpu=before_cpu,
                decoded=decoded,
                written_registers=("PC",),
                memory_writes=(),
                after_memory=before_memory,
                new_pc=decoded.next_sequential_pc,
                reg_updates=None,
                flags_updates={"cf": bool(bit_value)},
                note=(
                    f"Executed {width_label} LDCF from the current real execution subset. "
                    f"CF <- bit {bit_index} of mem8(0x{target_address:06X})."
                ),
            )

        carry = int(before_cpu.flags.cf)
        if op_byte in (0x28,) or (op_byte & 0xF8) == 0x80:
            new_carry = bool(carry & bit_value)
            action_name = "ANDCF"
        elif op_byte in (0x29,) or (op_byte & 0xF8) == 0x88:
            new_carry = bool(carry | bit_value)
            action_name = "ORCF"
        elif op_byte in (0x2A,) or (op_byte & 0xF8) == 0x90:
            new_carry = bool(carry ^ bit_value)
            action_name = "XORCF"
        else:
            new_value = (mem_value & (~bit_mask & 0xFF)) | (bit_mask if carry else 0)
            return _execute_absolute_store(
                view=view,
                before_cpu=before_cpu,
                before_memory=before_memory,
                decoded=decoded,
                target_address=target_address,
                data=bytes((new_value & 0xFF,)),
                note=(
                    f"Executed {width_label} STCF from the current real execution subset. "
                    f"Bit {bit_index} was written from CF into mem8(0x{target_address:06X})."
                ),
                memory_note=f"Writable runtime overlay updated by {width_label} STCF execution.",
            )

        return _executed_result(
            before_cpu=before_cpu,
            decoded=decoded,
            written_registers=("PC",),
            memory_writes=(),
            after_memory=before_memory,
            new_pc=decoded.next_sequential_pc,
            reg_updates=None,
            flags_updates={"cf": new_carry},
            note=(
                f"Executed {width_label} {action_name} from the current real execution subset. "
                f"CF updated from prior C={carry} and bit {bit_index} of mem8(0x{target_address:06X})."
            ),
        )

    raw = decoded.raw_bytes
    if raw is None or raw[0] not in (0xC2, 0xF0, 0xF1, 0xF2, 0xF3):
        return None

    if raw[0] == 0xF0 and len(raw) == 4 and raw[2] == 0x00:
        target_address = _mask_address(raw[1])
        return _execute_absolute_store(
            view=view,
            before_cpu=before_cpu,
            before_memory=before_memory,
            decoded=decoded,
            target_address=target_address,
            data=bytes((raw[3],)),
            note=(
                "Executed abs8 immediate byte store from the current real execution subset. "
                "The decoded immediate byte was written to the writable runtime overlay."
            ),
            memory_note="Writable runtime overlay updated by abs8 immediate byte store execution.",
        )

    if raw[0] == 0xF0 and len(raw) == 3 and 0x40 <= raw[2] <= 0x47:
        # ld (abs8), R8 — store a byte register to the CPU-I/O-page address.
        # Real SNK BIOS boot: `ld (0xBC), A` (F0 BC 41).
        target_address = _mask_address(raw[1])
        reg_index = raw[2] & 0x07
        reg_name, reg_value = _extract_register_value(before_cpu, "byte", reg_index)
        if reg_value is None:
            return _blocked_result(
                before_cpu=before_cpu,
                decoded=decoded,
                status="requires-known-full-register",
                note=(
                    f"{reg_name} must be known before `ld (0x{target_address & 0xFF:02X}), "
                    f"{reg_name}` can store it honestly."
                ),
            )
        return _execute_absolute_store(
            view=view,
            before_cpu=before_cpu,
            before_memory=before_memory,
            decoded=decoded,
            target_address=target_address,
            data=bytes((reg_value & 0xFF,)),
            note=(
                f"Executed abs8 byte register store `ld (0x{target_address & 0xFF:02X}), "
                f"{reg_name}` from the current real execution subset."
            ),
            memory_note="Writable runtime overlay updated by abs8 byte register store execution.",
        )

    if raw[0] == 0xF0 and len(raw) == 3 and 0x50 <= raw[2] <= 0x57:
        # ldw (abs8), R16 — store a 16-bit register to the CPU-I/O-page address.
        # Cool Cool Jam / KOF Battle frontier `F0 B8 50` = ldw (0xB8), WA.
        target_address = _mask_address(raw[1])
        reg_index = raw[2] & 0x07
        reg_name, reg_value = _extract_register_value(before_cpu, "word", reg_index)
        if reg_value is None:
            return _blocked_result(
                before_cpu=before_cpu,
                decoded=decoded,
                status="requires-known-full-register",
                note=f"{reg_name} must be known before `ldw (0x{target_address & 0xFF:02X}), {reg_name}` can store it.",
            )
        return _execute_absolute_store(
            view=view,
            before_cpu=before_cpu,
            before_memory=before_memory,
            decoded=decoded,
            target_address=target_address,
            data=(reg_value & 0xFFFF).to_bytes(2, "little"),
            note=(
                f"Executed abs8 word register store `ldw (0x{target_address & 0xFF:02X}), "
                f"{reg_name}` from the current real execution subset."
            ),
            memory_note="Writable runtime overlay updated by abs8 word register store execution.",
        )

    if raw[0] == 0xF0 and len(raw) == 5 and raw[2] == 0x02:
        target_address = _mask_address(raw[1])
        imm16 = int.from_bytes(raw[3:5], "little")
        return _execute_absolute_store(
            view=view,
            before_cpu=before_cpu,
            before_memory=before_memory,
            decoded=decoded,
            target_address=target_address,
            data=imm16.to_bytes(2, "little"),
            note=(
                "Executed abs8 immediate word store from the current real execution subset. "
                "The decoded 16-bit immediate was written to the writable runtime overlay."
            ),
            memory_note="Writable runtime overlay updated by abs8 immediate word store execution.",
        )

    if raw[0] == 0xF0 and len(raw) == 5 and raw[2] in (0x14, 0x16):
        target_address = _mask_address(raw[1])
        source_address = _mask_address(int.from_bytes(raw[3:5], "little"))
        width = 1 if raw[2] == 0x14 else 2
        source_bytes = _read_runtime_bytes(view, before_memory, source_address, width)
        if source_bytes is None:
            return _blocked_result(
                before_cpu=before_cpu,
                decoded=decoded,
                status="runtime-memory-unavailable",
                note=(
                    f"This abs8 memory-to-memory {'byte' if width == 1 else 'word'} store needs "
                    f"{width} readable source byte(s) at 0x{source_address:04X}, but neither "
                    "the writable runtime overlay nor the current read bus can provide them."
                ),
            )
        return _execute_absolute_store(
            view=view,
            before_cpu=before_cpu,
            before_memory=before_memory,
            decoded=decoded,
            target_address=target_address,
            data=source_bytes,
            note=(
                f"Executed abs8 memory-to-memory {'byte' if width == 1 else 'word'} store from "
                "the current real execution subset. The source bytes were read from the "
                "writable runtime overlay or the current read bus, then written to the abs8 "
                "destination."
            ),
            memory_note=(
                f"Writable runtime overlay updated by abs8 memory-to-memory "
                f"{'byte' if width == 1 else 'word'} store execution."
            ),
        )

    if raw[0] == 0xC2 and len(raw) == 7 and raw[4] == 0x19:
        # ld (abs16), (abs24): memory-to-memory BYTE move. Read the source byte
        # at the abs24 address, write it to the trailing abs16 destination.
        # Puzzle Link / Tsunagete `C2 C7 44 00 19 32 D6` = ld (0xD632), (0x0044C7).
        src_address = _mask_address(int.from_bytes(raw[1:4], "little"))
        dest_address = _mask_address(int.from_bytes(raw[5:7], "little"))
        data = _read_runtime_bytes(view, before_memory, src_address, 1)
        if data is None:
            return _blocked_result(
                before_cpu=before_cpu, decoded=decoded, status="runtime-memory-unavailable",
                note="This mem-to-mem byte move needs a readable source byte, unavailable in overlay/bus.",
            )
        return _execute_absolute_store(
            view=view, before_cpu=before_cpu, before_memory=before_memory, decoded=decoded,
            target_address=dest_address, data=bytes(data),
            note=(f"Executed mem-to-mem byte move ld (0x{dest_address & 0xFFFF:04X}), "
                  f"(0x{src_address:06X})=0x{data[0]:02X}."),
            memory_note="Writable runtime overlay updated by mem-to-mem byte move.",
        )

    if raw[0] == 0xC2 and len(raw) == 5 and 0x20 <= raw[4] <= 0x27:
        # ld R8, (abs24): read one byte from abs24 address, load into R8 register
        target_address = _mask_address(int.from_bytes(raw[1:4], "little"))
        data = _read_runtime_bytes(view, before_memory, target_address, 1)
        if data is None:
            return _blocked_result(
                before_cpu=before_cpu,
                decoded=decoded,
                status="runtime-memory-unavailable",
                note=(
                    "This abs24 byte load needs a readable source byte, but neither the "
                    "writable runtime overlay nor the current read bus can provide it."
                ),
            )
        return _execute_register_immediate(
            before_cpu=before_cpu,
            before_memory=before_memory,
            decoded=decoded,
            size_kind="byte",
            register_index=raw[4] & 0x07,
            value=data[0],
            note=(
                "Executed abs24 byte load from the current real execution subset. The source "
                "byte was read from the writable runtime overlay or the current read bus."
            ),
        )

    if raw[0] == 0xC2 and len(raw) == 5 and 0x40 <= raw[4] <= 0x47:
        # ld (abs24), R8: store one byte from R8 register to abs24 address
        source_register_name, source_value = _extract_register_value(
            before_cpu=before_cpu,
            size_kind="byte",
            register_index=raw[4] & 0x07,
        )
        if source_value is None:
            owner_name = R32[(raw[4] & 0x07) // 2]
            return _blocked_result(
                before_cpu=before_cpu,
                decoded=decoded,
                status="requires-known-full-register",
                note=(
                    f"{source_register_name} cannot be stored honestly until {owner_name} is "
                    "already known in the current CPU state."
                ),
            )
        target_address = _mask_address(int.from_bytes(raw[1:4], "little"))
        return _execute_absolute_store(
            view=view,
            before_cpu=before_cpu,
            before_memory=before_memory,
            decoded=decoded,
            target_address=target_address,
            data=bytes((source_value & 0xFF,)),
            note=(
                "Executed abs24 byte store from the current real execution subset. The "
                "source register byte was written to the writable runtime overlay."
            ),
            memory_note="Writable runtime overlay updated by abs24 byte store execution.",
        )

    if raw[0] == 0xC2 and len(raw) == 5 and 0x60 <= raw[4] <= 0x6F:
        target_address = _mask_address(int.from_bytes(raw[1:4], "little"))
        op = raw[4]
        count_code = op & 0x07
        count = 8 if count_code == 0 else count_code
        is_dec = op >= 0x68
        source = _read_runtime_bytes(view, before_memory, target_address, 1)
        if source is None:
            return _blocked_result(
                before_cpu=before_cpu,
                decoded=decoded,
                status="runtime-memory-unavailable",
                note=(
                    "This abs24 byte inc/dec needs a readable source byte, but neither the "
                    "writable runtime overlay nor the current read bus can provide it."
                ),
            )
        old_value = source[0]
        if is_dec:
            new_value = (old_value - count) & 0xFF
            mnemonic = "decb"
            flags_updates = dict(_compute_subtract_flags("byte", old_value, count))
        else:
            new_value = (old_value + count) & 0xFF
            mnemonic = "incb"
            flags_updates = dict(_compute_add_flags("byte", old_value, count))
        flags_updates.pop("cf", None)

        write_status, write_note = _check_writable_range(view, target_address, 1)
        if write_status is not None and write_status != "write-discarded":
            return _blocked_result(
                before_cpu=before_cpu,
                decoded=decoded,
                status=write_status,
                note=write_note,
            )

        if write_status == "write-discarded":
            after_memory = dict(before_memory)
            mem_write = MemoryWrite(
                address=target_address,
                data=bytes((new_value,)),
                note=f"[DISCARDED] {write_note}",
            )
        else:
            after_memory = dict(before_memory)
            after_memory[target_address] = new_value
            mem_write = MemoryWrite(
                address=target_address,
                data=bytes((new_value,)),
                note="Writable runtime overlay updated by abs24 byte inc/dec.",
            )
        return _executed_result(
            before_cpu=before_cpu,
            decoded=decoded,
            written_registers=("PC",),
            memory_writes=(mem_write,),
            after_memory=after_memory,
            new_pc=decoded.next_sequential_pc,
            reg_updates=None,
            flags_updates=flags_updates,
            note=(
                f"Executed {mnemonic} {count}, (0x{target_address:06X}): mem8 0x{old_value:02X} "
                f"-> 0x{new_value:02X}. CF preserved."
            ),
        )

    if raw[0] == 0xC2 and len(raw) == 5 and 0x80 <= raw[4] <= 0xFF:
        target_address = _mask_address(int.from_bytes(raw[1:4], "little"))
        mem_data = _read_runtime_bytes(view, before_memory, target_address, 1)
        if mem_data is None:
            return _blocked_result(
                before_cpu=before_cpu,
                decoded=decoded,
                status="runtime-memory-unavailable",
                note=(
                    "This abs24 byte ALU needs a readable source byte, but neither the "
                    "writable runtime overlay nor the current read bus can provide it."
                ),
            )

        sub_op = raw[4]
        operation = {
            0x8: "add",
            0x9: "adc",
            0xA: "sub",
            0xB: "sbc",
            0xC: "and",
            0xD: "xor",
            0xE: "or",
            0xF: "cp",
        }.get(sub_op >> 4)
        if operation is None:
            return None
        store_to_memory = bool(sub_op & 0x08)
        register_index = sub_op & 0x07
        register_name, register_value = _extract_register_value(before_cpu, "byte", register_index)
        if register_value is None:
            owner_name = R32[register_index // 2]
            return _blocked_result(
                before_cpu=before_cpu,
                decoded=decoded,
                status="requires-known-full-register",
                note=(
                    f"{register_name} cannot be used honestly until {owner_name} is already "
                    "known in the current CPU state."
                ),
            )
        if operation in ("adc", "sbc") and before_cpu.flags.cf is None:
            return _blocked_result(
                before_cpu=before_cpu,
                decoded=decoded,
                status="runtime-state-required",
                note=(
                    f"{operation.upper()} on abs24 byte memory requires a known carry flag, "
                    "which is not modeled in the current CPU state."
                ),
            )

        mem_value = mem_data[0]
        carry = int(before_cpu.flags.cf) if operation in ("adc", "sbc") else 0
        register_is_left = not store_to_memory
        if register_is_left:
            left_value = register_value
            right_value = mem_value
        else:
            left_value = mem_value
            right_value = register_value

        if operation == "add":
            result = (left_value + right_value) & 0xFF
            flags_updates = _compute_add_flags("byte", left_value, right_value)
        elif operation == "adc":
            result = (left_value + right_value + carry) & 0xFF
            flags_updates = _compute_add_flags("byte", left_value, right_value + carry)
        elif operation == "sub":
            result = (left_value - right_value) & 0xFF
            flags_updates = _compute_subtract_flags("byte", left_value, right_value)
        elif operation == "sbc":
            result = (left_value - right_value - carry) & 0xFF
            flags_updates = _compute_subtract_flags("byte", left_value, right_value + carry)
        elif operation == "and":
            result = left_value & right_value
            flags_updates = _compute_logical_flags("byte", result, half_carry=True)
        elif operation == "xor":
            result = left_value ^ right_value
            flags_updates = _compute_logical_flags("byte", result)
        elif operation == "or":
            result = left_value | right_value
            flags_updates = _compute_logical_flags("byte", result)
        else:
            result = (left_value - right_value) & 0xFF
            flags_updates = _compute_subtract_flags("byte", left_value, right_value)

        if operation == "cp":
            direction = "register-minus-memory" if register_is_left else "memory-minus-register"
            return _executed_result(
                before_cpu=before_cpu,
                decoded=decoded,
                written_registers=("PC",),
                memory_writes=(),
                after_memory=before_memory,
                new_pc=decoded.next_sequential_pc,
                reg_updates=None,
                flags_updates=flags_updates,
                note=(
                    f"Executed abs24 byte compare ({direction}) from the current real execution "
                    "subset. One byte was read at the absolute address and the modeled flag "
                    "subset now reflects the subtraction result."
                ),
            )

        if store_to_memory:
            write_status, write_note = _check_writable_range(view, target_address, 1)
            if write_status == "write-discarded":
                return _executed_result(
                    before_cpu=before_cpu,
                    decoded=decoded,
                    written_registers=("PC",),
                    memory_writes=(
                        MemoryWrite(
                            address=target_address,
                            data=bytes((result,)),
                            note=f"[DISCARDED] {write_note}",
                        ),
                    ),
                    after_memory=before_memory,
                    new_pc=decoded.next_sequential_pc,
                    reg_updates=None,
                    flags_updates=flags_updates,
                    note=(
                        "Abs24 byte ALU destination was unmapped or read-only; write silently "
                        "discarded (open-bus behavior — execution continues)."
                    ),
                )
            if write_status is not None:
                return _blocked_result(
                    before_cpu=before_cpu,
                    decoded=decoded,
                    status=write_status,
                    note=write_note,
                )

            after_memory = dict(before_memory)
            after_memory[target_address] = result
            return _executed_result(
                before_cpu=before_cpu,
                decoded=decoded,
                written_registers=("PC",),
                memory_writes=(
                    MemoryWrite(
                        address=target_address,
                        data=bytes((result,)),
                        note=f"Writable runtime overlay updated by abs24 byte {operation.upper()} execution.",
                    ),
                ),
                after_memory=after_memory,
                new_pc=decoded.next_sequential_pc,
                reg_updates=None,
                flags_updates=flags_updates,
                note=(
                    f"Executed abs24 byte {operation}: mem(0x{target_address:06X})=0x{mem_value:02X}, "
                    f"{register_name}=0x{register_value:02X} -> mem=0x{result:02X}."
                ),
            )

        result_name, reg_updates = _build_register_update(
            before_cpu=before_cpu,
            size_kind="byte",
            register_index=register_index,
            value=result,
        )
        if reg_updates is None:
            owner_name = R32[register_index // 2]
            return _blocked_result(
                before_cpu=before_cpu,
                decoded=decoded,
                status="requires-known-full-register",
                note=(
                    f"{register_name} cannot be updated honestly until {owner_name} is "
                    "already known in the current CPU state."
                ),
            )
        return _executed_result(
            before_cpu=before_cpu,
            decoded=decoded,
            written_registers=(result_name, "PC"),
            memory_writes=(),
            after_memory=before_memory,
            new_pc=decoded.next_sequential_pc,
            reg_updates=reg_updates,
            flags_updates=flags_updates,
            note=(
                f"Executed abs24 byte {operation}: {register_name}=0x{register_value:02X}, "
                f"mem(0x{target_address:06X})=0x{mem_value:02X} -> {result_name}=0x{result:02X}."
            ),
        )

    if raw[0] == 0xC2 and len(raw) == 6 and 0x38 <= raw[4] <= 0x3F:
        target_address = _mask_address(int.from_bytes(raw[1:4], "little"))
        mem_data = _read_runtime_bytes(view, before_memory, target_address, 1)
        if mem_data is None:
            return _blocked_result(
                before_cpu=before_cpu,
                decoded=decoded,
                status="runtime-memory-unavailable",
                note=(
                    "This abs24 byte ALU-immediate needs a readable source byte, but "
                    "neither the writable runtime overlay nor the current read bus can provide it."
                ),
            )
        imm8 = raw[5]
        mem_value = mem_data[0]
        operation = {
            0x38: "add",
            0x39: "adc",
            0x3A: "sub",
            0x3B: "sbc",
            0x3C: "and",
            0x3D: "xor",
            0x3E: "or",
            0x3F: "cp",
        }[raw[4]]
        carry = 0
        if operation in ("adc", "sbc"):
            if before_cpu.flags.cf is None:
                return _blocked_result(
                    before_cpu=before_cpu,
                    decoded=decoded,
                    status="runtime-state-required",
                    note=(
                        f"{operation.upper()} on abs24 byte memory requires a known carry flag, "
                        "which is not modeled in the current CPU state."
                    ),
                )
            carry = int(before_cpu.flags.cf)

        if operation == "add":
            result = (mem_value + imm8) & 0xFF
            flags_updates = _compute_add_flags("byte", mem_value, imm8)
        elif operation == "adc":
            result = (mem_value + imm8 + carry) & 0xFF
            flags_updates = _compute_add_flags("byte", mem_value, imm8 + carry)
        elif operation == "sub":
            result = (mem_value - imm8) & 0xFF
            flags_updates = _compute_subtract_flags("byte", mem_value, imm8)
        elif operation == "sbc":
            result = (mem_value - imm8 - carry) & 0xFF
            flags_updates = _compute_subtract_flags("byte", mem_value, imm8 + carry)
        elif operation == "and":
            result = mem_value & imm8
            flags_updates = _compute_logical_flags("byte", result, half_carry=True)
        elif operation == "xor":
            result = mem_value ^ imm8
            flags_updates = _compute_logical_flags("byte", result)
        elif operation == "or":
            result = mem_value | imm8
            flags_updates = _compute_logical_flags("byte", result)
        else:
            result = (mem_value - imm8) & 0xFF
            flags_updates = _compute_subtract_flags("byte", mem_value, imm8)

        if operation == "cp":
            return _executed_result(
                before_cpu=before_cpu,
                decoded=decoded,
                written_registers=("PC",),
                memory_writes=(),
                after_memory=before_memory,
                new_pc=decoded.next_sequential_pc,
                reg_updates=None,
                flags_updates=flags_updates,
                note=(
                    "Executed abs24 byte compare-immediate from the current real execution subset. "
                    f"Flags = mem(0x{target_address:06X})=0x{mem_value:02X} - 0x{imm8:02X}."
                ),
            )

        write_status, write_note = _check_writable_range(view, target_address, 1)
        if write_status == "write-discarded":
            return _executed_result(
                before_cpu=before_cpu,
                decoded=decoded,
                written_registers=("PC",),
                memory_writes=(
                    MemoryWrite(
                        address=target_address,
                        data=bytes((result,)),
                        note=f"[DISCARDED] {write_note}",
                    ),
                ),
                after_memory=before_memory,
                new_pc=decoded.next_sequential_pc,
                reg_updates=None,
                flags_updates=flags_updates,
                note=(
                    "Abs24 byte ALU-immediate destination was unmapped or read-only; write "
                    "silently discarded (open-bus behavior - execution continues)."
                ),
            )
        if write_status is not None:
            return _blocked_result(
                before_cpu=before_cpu,
                decoded=decoded,
                status=write_status,
                note=write_note,
            )

        after_memory = dict(before_memory)
        after_memory[target_address] = result
        return _executed_result(
            before_cpu=before_cpu,
            decoded=decoded,
            written_registers=("PC",),
            memory_writes=(
                MemoryWrite(
                    address=target_address,
                    data=bytes((result,)),
                    note=f"Writable runtime overlay updated by abs24 byte {operation.upper()} immediate execution.",
                ),
            ),
            after_memory=after_memory,
            new_pc=decoded.next_sequential_pc,
            reg_updates=None,
            flags_updates=flags_updates,
            note=(
                f"Executed abs24 byte {operation.upper()}-immediate from the current real "
                f"execution subset. mem8(0x{target_address:06X})=0x{mem_value:02X}, "
                f"imm=0x{imm8:02X} -> 0x{result:02X}."
            ),
        )

    if raw[0] == 0xF1 and len(raw) == 4 and 0x20 <= raw[3] <= 0x27:
        # ld R8, (abs16): load one byte from absolute 16-bit address into R8.
        # Mirror of the 0x40..0x47 store below; read pattern follows the abs16
        # word load in _try_execute_abs16_word_memory.
        target_address = _mask_address(int.from_bytes(raw[1:3], "little"))
        data = _read_runtime_bytes(view, before_memory, target_address, 1)
        if data is None:
            return _blocked_result(
                before_cpu=before_cpu,
                decoded=decoded,
                status="runtime-memory-unavailable",
                note=(
                    "This abs16 byte load needs 1 readable source byte, but neither the "
                    "writable runtime overlay nor the current read bus can provide it."
                ),
            )
        value = data[0]
        return _execute_register_immediate(
            before_cpu=before_cpu,
            before_memory=before_memory,
            decoded=decoded,
            size_kind="byte",
            register_index=raw[3] & 0x07,
            value=value,
            note=(
                f"Executed abs16 byte load ld R8, (0x{target_address & 0xFFFF:04X}): "
                f"value=0x{value:02X} into target R8."
            ),
        )

    if raw[0] == 0xF1 and len(raw) == 4 and 0x40 <= raw[3] <= 0x47:
        # ld (abs16), R8: store one byte from R8 register to absolute 16-bit address
        source_register_name, source_value = _extract_register_value(
            before_cpu=before_cpu,
            size_kind="byte",
            register_index=raw[3] & 0x07,
        )
        if source_value is None:
            owner_name = R32[(raw[3] & 0x07) // 2]
            return _blocked_result(
                before_cpu=before_cpu,
                decoded=decoded,
                status="requires-known-full-register",
                note=(
                    f"{source_register_name} cannot be stored honestly until {owner_name} is "
                    "already known in the current CPU state."
                ),
            )
        target_address = _mask_address(int.from_bytes(raw[1:3], "little"))
        return _execute_absolute_store(
            view=view,
            before_cpu=before_cpu,
            before_memory=before_memory,
            decoded=decoded,
            target_address=target_address,
            data=bytes((source_value & 0xFF,)),
            note=(
                "Executed abs16 byte store from the current real execution subset. The "
                "source register byte was written to the writable runtime overlay."
            ),
            memory_note="Writable runtime overlay updated by abs16 byte store execution.",
        )

    if raw[0] == 0xF1 and len(raw) == 4 and 0x50 <= raw[3] <= 0x57:
        # ldw (abs16), R16: store two bytes (low/high) from R16 register
        # to absolute 16-bit address.
        register_index = raw[3] & 0x07
        source_register_name, source_value = _extract_register_value(
            before_cpu=before_cpu,
            size_kind="word",
            register_index=register_index,
        )
        if source_value is None:
            owner_name = R32[register_index]
            return _blocked_result(
                before_cpu=before_cpu,
                decoded=decoded,
                status="requires-known-full-register",
                note=(
                    f"{source_register_name} cannot be stored honestly until {owner_name} is "
                    "already known in the current CPU state."
                ),
            )
        target_address = _mask_address(int.from_bytes(raw[1:3], "little"))
        return _execute_absolute_store(
            view=view,
            before_cpu=before_cpu,
            before_memory=before_memory,
            decoded=decoded,
            target_address=target_address,
            data=(source_value & 0xFFFF).to_bytes(2, "little"),
            note=(
                "Executed abs16 word store from the current real execution subset. The "
                "low 16 bits of the source register were written little-endian to the "
                "writable runtime overlay."
            ),
            memory_note="Writable runtime overlay updated by abs16 word store execution.",
        )

    if raw[0] == 0xF2 and len(raw) == 5 and 0x40 <= raw[4] <= 0x47:
        source_register_name, source_value = _extract_register_value(
            before_cpu=before_cpu,
            size_kind="byte",
            register_index=raw[4] & 0x07,
        )
        if source_value is None:
            owner_name = R32[(raw[4] & 0x07) // 2]
            return _blocked_result(
                before_cpu=before_cpu,
                decoded=decoded,
                status="requires-known-full-register",
                note=(
                    f"{source_register_name} cannot be stored honestly until {owner_name} is "
                    "already known in the current CPU state."
                ),
            )

        target_address = _mask_address(int.from_bytes(raw[1:4], "little"))
        return _execute_absolute_store(
            view=view,
            before_cpu=before_cpu,
            before_memory=before_memory,
            decoded=decoded,
            target_address=target_address,
            data=bytes((source_value & 0xFF,)),
            note=(
                "Executed absolute byte store from the current real execution subset. The "
                "source register byte was written to the writable runtime overlay."
            ),
            memory_note="Writable runtime overlay updated by absolute byte store execution.",
        )

    if raw[0] == 0xF2 and len(raw) == 5 and 0x50 <= raw[4] <= 0x57:
        source_register_name, source_value = _extract_register_value(
            before_cpu=before_cpu,
            size_kind="word",
            register_index=raw[4] & 0x07,
        )
        if source_value is None:
            return _blocked_result(
                before_cpu=before_cpu,
                decoded=decoded,
                status="requires-known-full-register",
                note=(
                    f"{source_register_name} cannot be stored honestly until its current word "
                    "value is known in the CPU state."
                ),
            )

        target_address = _mask_address(int.from_bytes(raw[1:4], "little"))
        return _execute_absolute_store(
            view=view,
            before_cpu=before_cpu,
            before_memory=before_memory,
            decoded=decoded,
            target_address=target_address,
            data=(source_value & 0xFFFF).to_bytes(2, "little"),
            note=(
                "Executed abs24 word store from the current real execution subset. The "
                "low 16 bits of the source register were written little-endian to the "
                "writable runtime overlay."
            ),
            memory_note="Writable runtime overlay updated by abs24 word store execution.",
        )

    if raw[0] == 0xF2 and len(raw) == 6 and raw[4] == 0x00:
        target_address = _mask_address(int.from_bytes(raw[1:4], "little"))
        return _execute_absolute_store(
            view=view,
            before_cpu=before_cpu,
            before_memory=before_memory,
            decoded=decoded,
            target_address=target_address,
            data=bytes((raw[5],)),
            note=(
                "Executed absolute immediate byte store from the current real execution subset. "
                "The decoded immediate byte was written to the writable runtime overlay."
            ),
            memory_note=(
                "Writable runtime overlay updated by absolute immediate byte store execution."
            ),
        )

    if raw[0] == 0xF2 and len(raw) == 7 and raw[4] == 0x02:
        target_address = _mask_address(int.from_bytes(raw[1:4], "little"))
        imm16 = int.from_bytes(raw[5:7], "little")
        return _execute_absolute_store(
            view=view,
            before_cpu=before_cpu,
            before_memory=before_memory,
            decoded=decoded,
            target_address=target_address,
            data=imm16.to_bytes(2, "little"),
            note=(
                "Executed absolute immediate word store from the current real execution subset. "
                "The decoded 16-bit immediate was written to the writable runtime overlay."
            ),
            memory_note=(
                "Writable runtime overlay updated by absolute immediate word store execution."
            ),
        )

    if raw[0] == 0xF2 and len(raw) == 5 and 0xE8 <= raw[4] <= 0xEF:
        cc_idx = raw[4] & 0x0F
        if cc_idx != 8:
            condition_result = _evaluate_condition_code(cc_idx, before_cpu.flags)
            if condition_result is None:
                return _blocked_result(
                    before_cpu=before_cpu,
                    decoded=decoded,
                    status="runtime-state-required",
                    note=(
                        f"call {CC[cc_idx]}, (abs24): the condition flag(s) required to evaluate "
                        "this indirect call are not fully known in the current CPU state."
                    ),
                )
            if not condition_result:
                return _executed_result(
                    before_cpu=before_cpu,
                    decoded=decoded,
                    written_registers=("PC",),
                    memory_writes=(),
                    after_memory=before_memory,
                    new_pc=decoded.next_sequential_pc,
                    reg_updates=None,
                    note=(
                        f"Executed conditional indirect call via abs24 memory with condition "
                        f"{CC[cc_idx]} false. No target fetch or stack write was performed."
                    ),
                )

        # THE EFFECTIVE ADDRESS **IS** THE TARGET -- it is not a pointer to chase.
        #
        # Toshiba, <Call>: "CALL cc, dst -- if cc then PUSH PC: PC <- dst", and for
        # a memory-operand form `dst` is what the addressing mode COMPUTES. So
        # `F2 00 8F 22 EF` (`call (0x228F00)`) sets PC to 0x228F00. This path used
        # to read four bytes FROM 0x228F00 and jump to whatever they held, which in
        # Fatal Fury and SNK Gals' Fighters sent the CPU to 0x031702 -- an address
        # that is neither RAM nor cartridge. Same rule as JP: `jp (XIX)` jumps TO
        # XIX; it does not jump to the memory XIX points at.
        target_address = _mask_address(int.from_bytes(raw[1:4], "little"))

        if decoded.next_sequential_pc is None:
            return _blocked_result(
                before_cpu=before_cpu,
                decoded=decoded,
                status="unsupported-decoded-instruction",
                note="abs24 CALL has no sequential return site in the current decode payload.",
            )

        return _execute_call(
            view=view,
            before_cpu=before_cpu,
            before_memory=before_memory,
            decoded=decoded,
            target_pc=target_address,
            return_pc=decoded.next_sequential_pc,
            cycles_consumed=CALL_MEM_CYCLES,
        )

    if raw[0] == 0xF2 and len(raw) == 5 and 0x60 <= raw[4] <= 0x67:
        source_register_name, source_value = _extract_register_value(
            before_cpu=before_cpu,
            size_kind="long",
            register_index=raw[4] & 0x07,
        )
        if source_value is None:
            return _blocked_result(
                before_cpu=before_cpu,
                decoded=decoded,
                status="requires-known-full-register",
                note=(
                    f"{source_register_name} cannot be stored honestly until its current full "
                    "value is known in the CPU state."
                ),
            )

        target_address = _mask_address(int.from_bytes(raw[1:4], "little"))
        return _execute_absolute_store(
            view=view,
            before_cpu=before_cpu,
            before_memory=before_memory,
            decoded=decoded,
            target_address=target_address,
            data=source_value.to_bytes(4, "little"),
            note=(
                "Executed abs24 long store from the current real execution subset. The source "
                "32-bit register value was written to the writable runtime overlay."
            ),
            memory_note="Writable runtime overlay updated by abs24 long store execution.",
        )

    if raw[0] == 0xF1 and len(raw) == 4 and 0x60 <= raw[3] <= 0x67:
        source_register_name, source_value = _extract_register_value(
            before_cpu=before_cpu,
            size_kind="long",
            register_index=raw[3] & 0x07,
        )
        if source_value is None:
            return _blocked_result(
                before_cpu=before_cpu,
                decoded=decoded,
                status="requires-known-full-register",
                note=(
                    f"{source_register_name} cannot be stored honestly until its current full "
                    "value is known in the CPU state."
                ),
            )

        target_address = _mask_address(int.from_bytes(raw[1:3], "little"))
        return _execute_absolute_store(
            view=view,
            before_cpu=before_cpu,
            before_memory=before_memory,
            decoded=decoded,
            target_address=target_address,
            data=source_value.to_bytes(4, "little"),
            note=(
                "Executed abs16 long store from the current real execution subset. The source "
                "32-bit register value was written to the writable runtime overlay."
            ),
            memory_note="Writable runtime overlay updated by abs16 long store execution.",
        )

    if raw[0] == 0xF1 and len(raw) == 5 and raw[3] == 0x00:
        target_address = _mask_address(int.from_bytes(raw[1:3], "little"))
        return _execute_absolute_store(
            view=view,
            before_cpu=before_cpu,
            before_memory=before_memory,
            decoded=decoded,
            target_address=target_address,
            data=bytes((raw[4],)),
            note=(
                "Executed abs16 immediate byte store from the current real execution subset. "
                "The decoded immediate byte was written to the writable runtime overlay."
            ),
            memory_note="Writable runtime overlay updated by abs16 immediate byte store execution.",
        )

    if raw[0] == 0xF1 and len(raw) == 6 and raw[3] == 0x02:
        target_address = _mask_address(int.from_bytes(raw[1:3], "little"))
        return _execute_absolute_store(
            view=view,
            before_cpu=before_cpu,
            before_memory=before_memory,
            decoded=decoded,
            target_address=target_address,
            data=raw[4:6],
            note=(
                "Executed abs16 immediate word store from the current real execution subset. "
                "The decoded 16-bit immediate was written little-endian to the writable overlay."
            ),
            memory_note="Writable runtime overlay updated by abs16 immediate word store execution.",
        )

    if raw[0] == 0xF0 and len(raw) == 3 and 0xA8 <= raw[2] <= 0xCF:
        # tset/res/set/chg/bit #n, (abs8). Real SNK BIOS boot: `set 2, (0xB3)`.
        target_address = _mask_address(raw[1])
        return execute_absolute_bit_operation(
            target_address=target_address,
            op_byte=raw[2],
            width_label="abs8",
        )

    if raw[0] == 0xF1 and len(raw) == 4 and 0xA8 <= raw[3] <= 0xCF:
        target_address = _mask_address(int.from_bytes(raw[1:3], "little"))
        return execute_absolute_bit_operation(
            target_address=target_address,
            op_byte=raw[3],
            width_label="abs16",
        )

    if raw[0] == 0xF1 and len(raw) == 4 and (
        0x28 <= raw[3] <= 0x2C or 0x80 <= raw[3] <= 0xA7
    ):
        target_address = _mask_address(int.from_bytes(raw[1:3], "little"))
        return execute_absolute_cf_operation(
            target_address=target_address,
            op_byte=raw[3],
            width_label="abs16",
        )

    if raw[0] == 0xF2 and len(raw) == 5 and (
        0x28 <= raw[4] <= 0x2C or 0x80 <= raw[4] <= 0xA7
    ):
        target_address = _mask_address(int.from_bytes(raw[1:4], "little"))
        return execute_absolute_cf_operation(
            target_address=target_address,
            op_byte=raw[4],
            width_label="abs24",
        )

    if raw[0] == 0xF2 and len(raw) == 5 and 0xA8 <= raw[4] <= 0xCF:
        target_address = _mask_address(int.from_bytes(raw[1:4], "little"))
        return execute_absolute_bit_operation(
            target_address=target_address,
            op_byte=raw[4],
            width_label="abs24",
        )

    if raw[0] == 0xF3 and len(raw) == 5 and (raw[1] & 0x03) == 0x01 and 0x30 <= raw[4] <= 0x37:
        # ARI secondary mode=1: lda R32, (r32+d16)
        # Encoding: F3 [secondary] [d16-lo] [d16-hi] [0x30+dest_r32]
        # secondary bits[1:0] = 0x01 (mode 1), bits[4:2] = r32_base index
        r32_base_name, r32_base_value, _base_refusal = secondary_base_r32(before_cpu, raw[1])
        if _base_refusal is not None:
            return _blocked_result(
                before_cpu=before_cpu,
                decoded=decoded,
                status="unimplemented",
                note=_base_refusal,
            )
        dest_r32_index = raw[4] & 0x07

        if r32_base_value is None:
            return _blocked_result(
                before_cpu=before_cpu,
                decoded=decoded,
                status="requires-known-address-register",
                note=(
                    f"{r32_base_name} must be known to compute the base for this ARI "
                    "secondary d16 lda."
                ),
            )

        # d16 is signed 16-bit displacement at bytes 2:4
        d16_raw = int.from_bytes(raw[2:4], "little")
        d16 = d16_raw if d16_raw < 0x8000 else d16_raw - 0x10000
        effective_address = _mask_address(r32_base_value + d16)

        return _execute_register_immediate(
            before_cpu=before_cpu,
            before_memory=before_memory,
            decoded=decoded,
            size_kind="long",
            register_index=dest_r32_index,
            value=effective_address,
            note=(
                f"Executed ARI secondary d16 lda from the current real execution subset. "
                f"EA = {r32_base_name}(0x{r32_base_value:06X}) + {d16} "
                f"= 0x{effective_address:06X}, stored into {R32[dest_r32_index]}."
            ),
        )

    if (
        raw[0] == 0xF3
        and (raw[1] & 0x03) == 0x01
        and ((len(raw) == 6 and raw[4] == 0x00) or (len(raw) == 7 and raw[4] == 0x02))
    ):
        # ARI secondary mode=1 immediate store: ld/ldw (r32+d16), imm
        # Encoding: F3 [secondary] [d16-lo] [d16-hi] [op] [imm...]
        #   op 0x00 -> ld  (r32+d16), imm8   (6 bytes)
        #   op 0x02 -> ldw (r32+d16), imm16  (7 bytes)
        r32_base_name, r32_base_value, _base_refusal = secondary_base_r32(before_cpu, raw[1])
        if _base_refusal is not None:
            return _blocked_result(
                before_cpu=before_cpu,
                decoded=decoded,
                status="unimplemented",
                note=_base_refusal,
            )

        if r32_base_value is None:
            return _blocked_result(
                before_cpu=before_cpu,
                decoded=decoded,
                status="requires-known-address-register",
                note=(
                    f"{r32_base_name} must be known to compute the effective address for "
                    "this ARI secondary d16 immediate store."
                ),
            )

        d16_raw = int.from_bytes(raw[2:4], "little")
        d16 = d16_raw if d16_raw < 0x8000 else d16_raw - 0x10000
        effective_address = _mask_address(r32_base_value + d16)

        if raw[4] == 0x00:
            data = bytes((raw[5],))
            width_label = "byte"
            imm_text = f"0x{raw[5]:02X}"
        else:
            imm16 = int.from_bytes(raw[5:7], "little")
            data = imm16.to_bytes(2, "little")
            width_label = "word"
            imm_text = f"0x{imm16:04X}"

        return _execute_absolute_store(
            view=view,
            before_cpu=before_cpu,
            before_memory=before_memory,
            decoded=decoded,
            target_address=effective_address,
            data=data,
            note=(
                f"Executed ARI secondary d16 {width_label} store from the current real "
                f"execution subset. EA = {r32_base_name}(0x{r32_base_value:06X}) + {d16} "
                f"= 0x{effective_address:06X} written with immediate {imm_text}."
            ),
            memory_note=(
                "Writable runtime overlay updated by ARI secondary d16 immediate store "
                "execution."
            ),
        )

    if raw[0] == 0xF3 and len(raw) == 5 and 0x30 <= raw[4] <= 0x37:
        # ARI secondary indexed: lda R32, (r32+r16)
        # Encoding: F3 [secondary] [r32_base_byte] [r16_index_byte] [0x30+dest_r32]
        # Computes EA = r32_base + r16_index, stores into dest_r32.
        dest_r32_index = raw[4] & 0x07

        r32_base_name, r32_base_value, _base_refusal = secondary_base_r32(before_cpu, raw[2])
        if _base_refusal is not None:
            return _blocked_result(
                before_cpu=before_cpu,
                decoded=decoded,
                status="unimplemented",
                note=_base_refusal,
            )

        r16_name, r16_full, _index_refusal = _resolve_index_displacement(
            before_cpu, raw[1], raw[3]
        )
        if _index_refusal is not None:
            return _blocked_result(
                before_cpu=before_cpu,
                decoded=decoded,
                status="requires-known-source-register",
                note=_index_refusal + " Guessing one would silently compute a wrong address.",
            )

        if r32_base_value is None:
            return _blocked_result(
                before_cpu=before_cpu,
                decoded=decoded,
                status="requires-known-address-register",
                note=(
                    f"{r32_base_name} must be known to compute the base for this ARI "
                    "secondary indexed lda."
                ),
            )
        if r16_full is None:
            return _blocked_result(
                before_cpu=before_cpu,
                decoded=decoded,
                status="requires-known-full-register",
                note=(
                    f"{r16_name} must be known to compute the index for this ARI secondary "
                    "indexed lda."
                ),
            )

        r16_value = r16_full
        effective_address = _mask_address(r32_base_value + r16_value)

        return _execute_register_immediate(
            before_cpu=before_cpu,
            before_memory=before_memory,
            decoded=decoded,
            size_kind="long",
            register_index=dest_r32_index,
            value=effective_address,
            note=(
                f"Executed ARI secondary indexed lda from the current real execution subset. "
                f"EA = {r32_base_name}(0x{r32_base_value:06X}) + {r16_name}(0x{r16_value:04X}) "
                f"= 0x{effective_address:06X}, stored into {R32[dest_r32_index]}."
            ),
        )

    if raw[0] == 0xF3 and len(raw) == 6 and raw[4] == 0x00:
        # ARI secondary indexed: ld (r32+r16), imm8
        # Encoding: F3 [secondary] [r32_byte] [r16_byte] 00 [imm8]
        # r32_byte: base r32 = (r32_byte >> 2) & 7
        # r16_byte: index r16 = (r16_byte >> 2) & 7 (lower 16-bit of the r32 register)
        r32_name, r32_value, _base_refusal = secondary_base_r32(before_cpu, raw[2])
        if _base_refusal is not None:
            return _blocked_result(
                before_cpu=before_cpu,
                decoded=decoded,
                status="unimplemented",
                note=_base_refusal,
            )
        r16_name, r16_full, _index_refusal = _resolve_index_displacement(
            before_cpu, raw[1], raw[3]
        )
        if _index_refusal is not None:
            return _blocked_result(
                before_cpu=before_cpu,
                decoded=decoded,
                status="requires-known-source-register",
                note=_index_refusal + " Guessing one would silently compute a wrong address.",
            )
        if r32_value is None:
            return _blocked_result(
                before_cpu=before_cpu,
                decoded=decoded,
                status="requires-known-full-register",
                note=(
                    f"{r32_name} must be known to compute the effective address for "
                    "this ARI secondary indexed byte store."
                ),
            )
        if r16_full is None:
            return _blocked_result(
                before_cpu=before_cpu,
                decoded=decoded,
                status="requires-known-full-register",
                note=(
                    f"{r16_name} must be known to compute the index for "
                    "this ARI secondary indexed byte store."
                ),
            )
        r16_value = r16_full
        effective_address = _mask_address(r32_value + r16_value)
        imm8 = raw[5]
        return _execute_absolute_store(
            view=view,
            before_cpu=before_cpu,
            before_memory=before_memory,
            decoded=decoded,
            target_address=effective_address,
            data=bytes((imm8,)),
            note=(
                f"Executed ARI secondary indexed byte store from the current real execution "
                f"subset. Address {r32_name}+{r16_name}=0x{effective_address:06X} written "
                f"with immediate 0x{imm8:02X}."
            ),
            memory_note=(
                "Writable runtime overlay updated by ARI secondary indexed byte store execution."
            ),
        )

    if raw[0] == 0xF3 and len(raw) == 7 and raw[4] == 0x02:
        # ARI secondary indexed: ldw (r32+r16), imm16
        # Encoding: F3 [secondary] [r32_byte] [r16_byte] 02 [imm16-lo] [imm16-hi]
        r32_name, r32_value, _base_refusal = secondary_base_r32(before_cpu, raw[2])
        if _base_refusal is not None:
            return _blocked_result(
                before_cpu=before_cpu,
                decoded=decoded,
                status="unimplemented",
                note=_base_refusal,
            )
        r16_name, r16_full, _index_refusal = _resolve_index_displacement(
            before_cpu, raw[1], raw[3]
        )
        if _index_refusal is not None:
            return _blocked_result(
                before_cpu=before_cpu,
                decoded=decoded,
                status="requires-known-source-register",
                note=_index_refusal + " Guessing one would silently compute a wrong address.",
            )
        if r32_value is None:
            return _blocked_result(
                before_cpu=before_cpu,
                decoded=decoded,
                status="requires-known-full-register",
                note=(
                    f"{r32_name} must be known to compute the effective address for "
                    "this ARI secondary indexed word store."
                ),
            )
        if r16_full is None:
            return _blocked_result(
                before_cpu=before_cpu,
                decoded=decoded,
                status="requires-known-full-register",
                note=(
                    f"{r16_name} must be known to compute the index for "
                    "this ARI secondary indexed word store."
                ),
            )
        r16_value = r16_full
        effective_address = _mask_address(r32_value + r16_value)
        imm16 = int.from_bytes(raw[5:7], "little")
        return _execute_absolute_store(
            view=view,
            before_cpu=before_cpu,
            before_memory=before_memory,
            decoded=decoded,
            target_address=effective_address,
            data=imm16.to_bytes(2, "little"),
            note=(
                f"Executed ARI secondary indexed word store from the current real execution "
                f"subset. Address {r32_name}+{r16_name}=0x{effective_address:06X} written "
                f"with immediate 0x{imm16:04X}."
            ),
            memory_note=(
                "Writable runtime overlay updated by ARI secondary indexed word store execution."
            ),
        )

    if (
        raw[0] == 0xF3
        and len(raw) == 5
        and (raw[1] & 0x03) == 0x01
        and 0x40 <= raw[4] <= 0x67
    ):
        # ARI secondary mode=1 register store: ld/ldw/ld (r32+d16), R8/R16/R32
        # Encoding: F3 [secondary] [d16-lo] [d16-hi] [0x40..0x67]
        r32_base_name, r32_base_value, _base_refusal = secondary_base_r32(before_cpu, raw[1])
        if _base_refusal is not None:
            return _blocked_result(
                before_cpu=before_cpu,
                decoded=decoded,
                status="unimplemented",
                note=_base_refusal,
            )

        if r32_base_value is None:
            return _blocked_result(
                before_cpu=before_cpu,
                decoded=decoded,
                status="requires-known-address-register",
                note=(
                    f"{r32_base_name} must be known to compute the effective address for "
                    "this ARI secondary d16 register store."
                ),
            )

        d16_raw = int.from_bytes(raw[2:4], "little")
        d16 = d16_raw if d16_raw < 0x8000 else d16_raw - 0x10000
        effective_address = _mask_address(r32_base_value + d16)

        op = raw[4]
        if 0x40 <= op <= 0x47:
            size_kind = "byte"
            data_size = 1
        elif 0x50 <= op <= 0x57:
            size_kind = "word"
            data_size = 2
        else:
            size_kind = "long"
            data_size = 4

        source_name, source_value = _extract_register_value(before_cpu, size_kind, op & 0x07)
        if source_value is None:
            return _blocked_result(
                before_cpu=before_cpu,
                decoded=decoded,
                status="requires-known-full-register",
                note=(
                    f"{source_name} must be known to execute this ARI secondary d16 "
                    f"{size_kind} store."
                ),
            )

        return _execute_absolute_store(
            view=view,
            before_cpu=before_cpu,
            before_memory=before_memory,
            decoded=decoded,
            target_address=effective_address,
            data=source_value.to_bytes(data_size, "little"),
            note=(
                f"Executed ARI secondary d16 {size_kind} store from the current real "
                f"execution subset. EA = {r32_base_name}(0x{r32_base_value:06X}) + {d16} "
                f"= 0x{effective_address:06X} written with "
                f"{source_name}=0x{source_value:0{data_size * 2}X}."
            ),
            memory_note=(
                "Writable runtime overlay updated by ARI secondary d16 register store "
                "execution."
            ),
        )

    if (
        raw[0] == 0xF3
        and len(raw) == 5
        and (raw[1] & 0x03) == 0x03
        and 0x40 <= raw[4] <= 0x67
    ):
        # ARI secondary indexed register store: ld/ldw/ld (r32+r16), R8/R16/R32
        # Encoding: F3 [secondary] [r32_byte] [r16_byte] [0x40..0x67]
        r32_name, r32_value, _base_refusal = secondary_base_r32(before_cpu, raw[2])
        if _base_refusal is not None:
            return _blocked_result(
                before_cpu=before_cpu,
                decoded=decoded,
                status="unimplemented",
                note=_base_refusal,
            )
        r16_name, r16_full, _index_refusal = _resolve_index_displacement(
            before_cpu, raw[1], raw[3]
        )
        if _index_refusal is not None:
            return _blocked_result(
                before_cpu=before_cpu,
                decoded=decoded,
                status="requires-known-source-register",
                note=_index_refusal + " Guessing one would silently compute a wrong address.",
            )
        if r32_value is None:
            return _blocked_result(
                before_cpu=before_cpu,
                decoded=decoded,
                status="requires-known-full-register",
                note=(
                    f"{r32_name} must be known to compute the effective address for "
                    "this ARI secondary indexed register store."
                ),
            )
        if r16_full is None:
            return _blocked_result(
                before_cpu=before_cpu,
                decoded=decoded,
                status="requires-known-full-register",
                note=(
                    f"{r16_name} must be known to compute the index for "
                    "this ARI secondary indexed register store."
                ),
            )

        effective_address = _mask_address(r32_value + r16_full)
        op = raw[4]
        if 0x40 <= op <= 0x47:
            size_kind = "byte"
            data_size = 1
            size_label = "byte"
        elif 0x50 <= op <= 0x57:
            size_kind = "word"
            data_size = 2
            size_label = "word"
        else:
            size_kind = "long"
            data_size = 4
            size_label = "long"

        source_name, source_value = _extract_register_value(before_cpu, size_kind, op & 0x07)
        if source_value is None:
            return _blocked_result(
                before_cpu=before_cpu,
                decoded=decoded,
                status="requires-known-full-register",
                note=(
                    f"{source_name} must be known to execute this ARI secondary indexed "
                    f"{size_label} store."
                ),
            )

        return _execute_absolute_store(
            view=view,
            before_cpu=before_cpu,
            before_memory=before_memory,
            decoded=decoded,
            target_address=effective_address,
            data=source_value.to_bytes(data_size, "little"),
            note=(
                f"Executed ARI secondary indexed {size_label} store from the current real "
                f"execution subset. Address {r32_name}+{r16_name}=0x{effective_address:06X} "
                f"written with {source_name}=0x{source_value:0{data_size * 2}X}."
            ),
            memory_note=(
                "Writable runtime overlay updated by ARI secondary indexed register store execution."
            ),
        )

    return None


def _try_execute_cpu_io_store(
    before_cpu: NgpcCpuState,
    before_memory: dict[int, int],
    decoded: DecodeResult,
) -> ExecutionResult | None:
    """Execute CPU I/O immediate stores: ldb (n), imm8 and ldw (n), imm16.

    These write to TLCS-900 internal CPU peripheral registers (address space
    0x00..0xFF). The value is now written through to the runtime overlay so
    later reads (e.g. read-modify-write config sequences like the BIOS boot's
    `or (0x00B2), imm8`) observe the last written value. This models the CPU
    on-chip I/O page as a tracked register file (last-write-wins), faithful for
    the config registers the BIOS drives at reset; status registers with read
    side-effects would need per-register modeling later.

    Encoding:
      08 [n] [imm8]        => ldb (n), imm8   (3 bytes)
      0A [n] [imm16-lo] [imm16-hi]  => ldw (n), imm16  (4 bytes)
    """
    raw = decoded.raw_bytes
    if raw is None or raw[0] not in (0x08, 0x0A):
        return None

    if raw[0] == 0x08 and len(raw) == 3:
        io_addr = raw[1]
        imm8 = raw[2]
        after_memory = dict(before_memory)
        after_memory[_mask_address(io_addr)] = imm8
        return _executed_result(
            before_cpu=before_cpu,
            decoded=decoded,
            written_registers=("PC",),
            memory_writes=(
                MemoryWrite(
                    address=io_addr,
                    data=bytes((imm8,)),
                    note=(
                        f"CPU I/O byte store to peripheral register 0x{io_addr:02X} with "
                        f"immediate 0x{imm8:02X} (tracked in the runtime overlay)."
                    ),
                ),
            ),
            after_memory=after_memory,
            new_pc=decoded.next_sequential_pc,
            reg_updates=None,
            note=(
                f"Executed CPU I/O byte store ldb (0x{io_addr:02X}), 0x{imm8:02X} from the "
                "current real execution subset (written through to the tracked I/O register file)."
            ),
        )

    if raw[0] == 0x0A and len(raw) == 4:
        io_addr = raw[1]
        imm16 = int.from_bytes(raw[2:4], "little")
        after_memory = dict(before_memory)
        for offset, byte_value in enumerate(imm16.to_bytes(2, "little")):
            after_memory[_mask_address(io_addr + offset)] = byte_value
        return _executed_result(
            before_cpu=before_cpu,
            decoded=decoded,
            written_registers=("PC",),
            memory_writes=(
                MemoryWrite(
                    address=io_addr,
                    data=imm16.to_bytes(2, "little"),
                    note=(
                        f"CPU I/O word store to peripheral register 0x{io_addr:02X} with "
                        f"immediate 0x{imm16:04X} (tracked in the runtime overlay)."
                    ),
                ),
            ),
            after_memory=after_memory,
            new_pc=decoded.next_sequential_pc,
            reg_updates=None,
            note=(
                f"Executed CPU I/O word store ldw (0x{io_addr:02X}), 0x{imm16:04X} from the "
                "current real execution subset (written through to the tracked I/O register file)."
            ),
        )

    return None


def _try_execute_ldx_direct(
    view: NgpcFetchView,
    before_cpu: NgpcCpuState,
    before_memory: dict[int, int],
    decoded: DecodeResult,
) -> ExecutionResult | None:
    """Execute `LDX (#8), #` — direct 8-bit address extract store.

    The local Toshiba docs describe this as a 6-byte instruction with
    every other byte acting as padding on the 16-bit bus. We model the
    observable effect only: write the immediate byte to the direct
    0x00..0xFF address selected by the `#8` field.
    """
    raw = decoded.raw_bytes
    if raw is None or len(raw) != 6 or raw[0] != 0xF7:
        return None
    if decoded.mnemonic != "ldx":
        return None

    target_address = raw[2]
    imm = raw[4]
    return _execute_absolute_store(
        view=view,
        before_cpu=before_cpu,
        before_memory=before_memory,
        decoded=decoded,
        target_address=target_address,
        data=bytes((imm,)),
        note=(
            "Executed LDX direct-address store from the current real execution subset. "
            "The extracted immediate byte was written to the direct 0x00..0xFF target."
        ),
        memory_note="Writable runtime overlay updated by LDX direct-address execution.",
    )


def _try_execute_abs16_word_memory(
    view: NgpcFetchView,
    before_cpu: NgpcCpuState,
    before_memory: dict[int, int],
    decoded: DecodeResult,
) -> ExecutionResult | None:
    """Execute the abs16 word-memory subset (prefix 0xD1).

    Currently implemented:
      - cpw (abs16), imm16  (D1 lo hi 3F imm_lo imm_hi, 6 bytes)
        Loads 2 bytes from the absolute address, computes the subtraction
        with the 16-bit immediate, and updates the modeled flag subset.
        No write-back (compare only).
    """
    raw = decoded.raw_bytes
    if raw is None or raw[0] != 0xD1:
        return None

    if len(raw) == 6 and raw[3] == 0x19:
        # ldw (abs16), (abs16): mem-to-mem WORD move (abs16 src -> abs16 dest).
        # Word sibling of the C1 op-0x19 byte form. The real SNK BIOS cart
        # hand-off uses `D1 04 6C 19 84 6E` = ldw (0x6E84), (0x6C04).
        src_address = _mask_address(int.from_bytes(raw[1:3], "little"))
        dest_address = _mask_address(int.from_bytes(raw[4:6], "little"))
        data = _read_runtime_bytes(view, before_memory, src_address, 2)
        if data is None:
            return _blocked_result(
                before_cpu=before_cpu, decoded=decoded, status="runtime-memory-unavailable",
                note="This abs16 word mem-to-mem move needs 2 readable source bytes, unavailable in overlay/bus.",
            )
        return _execute_absolute_store(
            view=view, before_cpu=before_cpu, before_memory=before_memory, decoded=decoded,
            target_address=dest_address, data=bytes(data),
            note=(f"Executed mem-to-mem word move ldw (0x{dest_address & 0xFFFF:04X}), "
                  f"(0x{src_address & 0xFFFF:04X})=0x{int.from_bytes(data, 'little'):04X}."),
            memory_note="Writable runtime overlay updated by abs16 word mem-to-mem move.",
        )

    if len(raw) == 4 and 0x20 <= raw[3] <= 0x27:
        # ld R16, (abs16): load 2 bytes from abs16, write to R16 destination.
        target_address = _mask_address(int.from_bytes(raw[1:3], "little"))
        data = _read_runtime_bytes(view, before_memory, target_address, 2)
        if data is None:
            return _blocked_result(
                before_cpu=before_cpu,
                decoded=decoded,
                status="runtime-memory-unavailable",
                note=(
                    "This abs16 word load needs 2 readable source bytes, but neither the "
                    "writable runtime overlay nor the current read bus can provide them."
                ),
            )
        value = int.from_bytes(data, "little")
        return _execute_register_immediate(
            before_cpu=before_cpu,
            before_memory=before_memory,
            decoded=decoded,
            size_kind="word",
            register_index=raw[3] & 0x07,
            value=value,
            note=(
                f"Executed abs16 word load ld R16, (0x{target_address & 0xFFFF:04X}): "
                f"value=0x{value:04X} into target R16."
            ),
        )

    if len(raw) == 6 and raw[3] == 0x3F:
        target_address = _mask_address(int.from_bytes(raw[1:3], "little"))
        data = _read_runtime_bytes(view, before_memory, target_address, 2)
        if data is None:
            return _blocked_result(
                before_cpu=before_cpu,
                decoded=decoded,
                status="runtime-memory-unavailable",
                note=(
                    "This abs16 word compare needs 2 readable source bytes, but neither the "
                    "writable runtime overlay nor the current read bus can provide them."
                ),
            )
        mem_value = int.from_bytes(data, "little")
        imm = int.from_bytes(raw[4:6], "little")
        flags_updates = _compute_subtract_flags("word", mem_value, imm)
        return _executed_result(
            before_cpu=before_cpu,
            decoded=decoded,
            written_registers=("PC",),
            memory_writes=(),
            after_memory=before_memory,
            new_pc=decoded.next_sequential_pc,
            reg_updates=None,
            flags_updates=flags_updates,
            note=(
                "Executed abs16 word compare-immediate from the current real execution subset. "
                f"Flags = mem16(0x{target_address & 0xFFFF:04X})=0x{mem_value:04X} - "
                f"imm=0x{imm:04X}."
            ),
        )

    if len(raw) == 4 and raw[3] == 0x04:
        # pushw (abs16): read 2 bytes from the absolute address, push onto stack.
        target_address = _mask_address(int.from_bytes(raw[1:3], "little"))
        data = _read_runtime_bytes(view, before_memory, target_address, 2)
        if data is None:
            return _blocked_result(
                before_cpu=before_cpu,
                decoded=decoded,
                status="runtime-memory-unavailable",
                note=(
                    "This abs16 word push needs 2 readable source bytes, but neither the "
                    "writable runtime overlay nor the current read bus can provide them."
                ),
            )
        return _execute_push_bytes(
            view=view, before_cpu=before_cpu, before_memory=before_memory, decoded=decoded,
            data=bytes(data),
            note=(
                f"Executed pushw (0x{target_address & 0xFFFF:04X}): pushed word "
                f"0x{int.from_bytes(data, 'little'):04X} from memory."
            ),
        )

    if len(raw) == 6 and 0x38 <= raw[3] <= 0x3E:
        # ALUw (abs16), imm16 -- word read-modify-write with a 16-bit immediate.
        # Crush Roller frontier `D1 E6 4E 3E 01 00` = orw (0x4EE6), 0x0001.
        target_address = _mask_address(int.from_bytes(raw[1:3], "little"))
        op_byte = raw[3]
        op_name = {0x38: "add", 0x39: "adc", 0x3A: "sub", 0x3B: "sbc",
                   0x3C: "and", 0x3D: "xor", 0x3E: "or"}[op_byte]
        needs_carry = op_byte in (0x39, 0x3B)
        if needs_carry and before_cpu.flags.cf is None:
            return _blocked_result(
                before_cpu=before_cpu, decoded=decoded, status="runtime-state-required",
                note=f"{op_name}w (abs16), imm16 needs a known carry flag.",
            )
        carry = int(before_cpu.flags.cf) if needs_carry else 0
        data = _read_runtime_bytes(view, before_memory, target_address, 2)
        if data is None:
            return _blocked_result(
                before_cpu=before_cpu, decoded=decoded, status="runtime-memory-unavailable",
                note="This abs16 word ALU-immediate needs 2 readable source bytes, unavailable in overlay/bus.",
            )
        mem_value = int.from_bytes(data, "little")
        imm = int.from_bytes(raw[4:6], "little")
        if op_byte in (0x38, 0x39):
            result = (mem_value + imm + carry) & 0xFFFF
            flags = _compute_add_flags("word", mem_value, imm + carry)
        elif op_byte in (0x3A, 0x3B):
            result = (mem_value - imm - carry) & 0xFFFF
            flags = _compute_subtract_flags("word", mem_value, imm + carry)
        elif op_byte == 0x3C:
            result = mem_value & imm
            flags = _compute_logical_flags("word", result, half_carry=True)
        elif op_byte == 0x3D:
            result = mem_value ^ imm
            flags = _compute_logical_flags("word", result)
        else:  # 0x3E or
            result = mem_value | imm
            flags = _compute_logical_flags("word", result)
        new_bytes = result.to_bytes(2, "little")
        after_memory = dict(before_memory)
        after_memory[target_address] = new_bytes[0]
        after_memory[_mask_address(target_address + 1)] = new_bytes[1]
        return _executed_result(
            before_cpu=before_cpu, decoded=decoded, written_registers=("PC",),
            memory_writes=(MemoryWrite(address=target_address, data=new_bytes,
                                       note=f"{op_name.upper()}w (abs16), imm16 word RMW."),),
            after_memory=after_memory, new_pc=decoded.next_sequential_pc,
            reg_updates=None, flags_updates=flags,
            note=f"Executed {op_name}w (0x{target_address & 0xFFFF:04X})=0x{mem_value:04X}, 0x{imm:04X} -> 0x{result:04X}.",
        )

    if len(raw) == 4 and 0x60 <= raw[3] <= 0x6F:
        # inc/dec #n, (abs16) word RMW. CF preserved (matches other inc/dec-mem).
        # Mirror of the D2 abs24 form. The real SNK BIOS VBlank frame handler
        # bumps a 16-bit frame counter with `D1 18 6C 61` = incw 1, (0x6C18).
        target_address = _mask_address(int.from_bytes(raw[1:3], "little"))
        op_byte = raw[3]
        count = (op_byte & 0x07) or 8
        is_dec = op_byte >= 0x68
        data = _read_runtime_bytes(view, before_memory, target_address, 2)
        if data is None:
            return _blocked_result(
                before_cpu=before_cpu, decoded=decoded, status="runtime-memory-unavailable",
                note="This abs16 word inc/dec needs 2 readable source bytes, unavailable in overlay/bus.",
            )
        mem_value = int.from_bytes(data, "little")
        if is_dec:
            new_value = (mem_value - count) & 0xFFFF
            flags = dict(_compute_subtract_flags("word", mem_value, count))
        else:
            new_value = (mem_value + count) & 0xFFFF
            flags = dict(_compute_add_flags("word", mem_value, count))
        flags.pop("cf", None)  # inc/dec on memory preserves carry
        new_bytes = new_value.to_bytes(2, "little")
        after_memory = dict(before_memory)
        after_memory[target_address] = new_bytes[0]
        after_memory[_mask_address(target_address + 1)] = new_bytes[1]
        return _executed_result(
            before_cpu=before_cpu, decoded=decoded, written_registers=("PC",),
            memory_writes=(MemoryWrite(address=target_address, data=new_bytes,
                                       note=f"{'DECW' if is_dec else 'INCW'} #{count} (abs16) RMW."),),
            after_memory=after_memory, new_pc=decoded.next_sequential_pc,
            reg_updates=None, flags_updates=flags,
            note=(f"Executed {'decw' if is_dec else 'incw'} {count}, (0x{target_address & 0xFFFF:04X}) "
                  f"0x{mem_value:04X} -> 0x{new_value:04X} (CF preserved)."),
        )

    if len(raw) == 4 and 0xF0 <= raw[3] <= 0xF7:
        # cp R16, (abs16): word compare (register minus memory), flags only.
        # Mirror of the D2 abs24 form. BIOS frame handler: `D1 1A 6C F0` =
        # cp WA, (0x6C1A) -- compare the frame counter against a threshold.
        target_address = _mask_address(int.from_bytes(raw[1:3], "little"))
        reg_name, reg_value = _extract_register_value(
            before_cpu=before_cpu, size_kind="word", register_index=raw[3] & 0x07,
        )
        if reg_value is None:
            return _blocked_result(
                before_cpu=before_cpu, decoded=decoded, status="requires-known-full-register",
                note=f"{reg_name} cannot be compared honestly until its full value is known.",
            )
        data = _read_runtime_bytes(view, before_memory, target_address, 2)
        if data is None:
            return _blocked_result(
                before_cpu=before_cpu, decoded=decoded, status="runtime-memory-unavailable",
                note="This abs16 word compare needs 2 readable source bytes, unavailable in overlay/bus.",
            )
        mem_value = int.from_bytes(data, "little")
        flags_updates = _compute_subtract_flags("word", reg_value, mem_value)
        return _executed_result(
            before_cpu=before_cpu, decoded=decoded, written_registers=("PC",),
            memory_writes=(), after_memory=before_memory, new_pc=decoded.next_sequential_pc,
            reg_updates=None, flags_updates=flags_updates,
            note=(f"Executed abs16 word compare cp {reg_name}=0x{reg_value:04X}, "
                  f"(0x{target_address & 0xFFFF:04X})=0x{mem_value:04X}."),
        )

    if len(raw) == 4 and 0xF8 <= raw[3] <= 0xFF:
        # cpw (abs16), R16: word compare (memory minus register), flags only.
        # Mirror of the D0 abs8 form. BIOS checksum-verify: `D1 14 6C F8` =
        # cpw (0x6C14), WA.
        target_address = _mask_address(int.from_bytes(raw[1:3], "little"))
        reg_name, reg_value = _extract_register_value(
            before_cpu=before_cpu, size_kind="word", register_index=raw[3] & 0x07,
        )
        if reg_value is None:
            return _blocked_result(
                before_cpu=before_cpu, decoded=decoded, status="requires-known-full-register",
                note=f"{reg_name} must be known before this abs16 word compare can execute honestly.",
            )
        data = _read_runtime_bytes(view, before_memory, target_address, 2)
        if data is None:
            return _blocked_result(
                before_cpu=before_cpu, decoded=decoded, status="runtime-memory-unavailable",
                note="This abs16 word compare needs 2 readable source bytes, unavailable in overlay/bus.",
            )
        mem_value = int.from_bytes(data, "little")
        flags_updates = _compute_subtract_flags("word", mem_value, reg_value)
        return _executed_result(
            before_cpu=before_cpu, decoded=decoded, written_registers=("PC",),
            memory_writes=(), after_memory=before_memory, new_pc=decoded.next_sequential_pc,
            reg_updates=None, flags_updates=flags_updates,
            note=(f"Executed abs16 word compare cpw (0x{target_address & 0xFFFF:04X})=0x{mem_value:04X}, "
                  f"{reg_name}=0x{reg_value:04X}."),
        )

    return None


def _try_execute_abs8_word_memory(
    view: NgpcFetchView,
    before_cpu: NgpcCpuState,
    before_memory: dict[int, int],
    decoded: DecodeResult,
) -> ExecutionResult | None:
    """Execute the abs8 word-memory subset (prefix 0xD0). Mirror of 0xD1.

    HW-confirmed 2026-07-03: 0xD0..0xD7 is a WORD memory family (not word
    register-direct). Implemented: `ld R16,(abs8)` and `cpw (abs8),imm16`
    (the real SNK BIOS boot runs `cpw (0xB6),0x0050` = D0 B6 3F 50 00).
    Address is a single byte (CPU I/O page 0x0000xx).
    """
    raw = decoded.raw_bytes
    if raw is None or raw[0] != 0xD0:
        return None

    if len(raw) == 3 and 0x20 <= raw[2] <= 0x27:
        target_address = _mask_address(raw[1])
        data = _read_runtime_bytes(view, before_memory, target_address, 2)
        if data is None:
            return _blocked_result(
                before_cpu=before_cpu,
                decoded=decoded,
                status="runtime-memory-unavailable",
                note=(
                    "This abs8 word load needs 2 readable source bytes, but neither the "
                    "writable runtime overlay nor the current read bus can provide them."
                ),
            )
        value = int.from_bytes(data, "little")
        return _execute_register_immediate(
            before_cpu=before_cpu,
            before_memory=before_memory,
            decoded=decoded,
            size_kind="word",
            register_index=raw[2] & 0x07,
            value=value,
            note=(
                f"Executed abs8 word load ld R16, (0x{target_address & 0xFF:02X}): "
                f"value=0x{value:04X} into target R16."
            ),
        )

    if len(raw) == 5 and raw[2] == 0x3F:
        target_address = _mask_address(raw[1])
        data = _read_runtime_bytes(view, before_memory, target_address, 2)
        if data is None:
            return _blocked_result(
                before_cpu=before_cpu,
                decoded=decoded,
                status="runtime-memory-unavailable",
                note=(
                    "This abs8 word compare needs 2 readable source bytes, but neither the "
                    "writable runtime overlay nor the current read bus can provide them."
                ),
            )
        mem_value = int.from_bytes(data, "little")
        imm = int.from_bytes(raw[3:5], "little")
        flags_updates = _compute_subtract_flags("word", mem_value, imm)
        return _executed_result(
            before_cpu=before_cpu,
            decoded=decoded,
            written_registers=("PC",),
            memory_writes=(),
            after_memory=before_memory,
            new_pc=decoded.next_sequential_pc,
            reg_updates=None,
            flags_updates=flags_updates,
            note=(
                "Executed abs8 word compare-immediate from the current real execution subset. "
                f"Flags = mem16(0x{target_address & 0xFF:02X})=0x{mem_value:04X} - imm=0x{imm:04X}."
            ),
        )

    if len(raw) == 3 and 0xF8 <= raw[2] <= 0xFF:
        # cpw (abs8), R16 — compare the memory word with the register (flags only).
        reg_name, reg_val = _extract_register_value(before_cpu, "word", raw[2] & 0x07)
        if reg_val is None:
            return _blocked_result(
                before_cpu=before_cpu, decoded=decoded, status="requires-known-full-register",
                note=f"{reg_name} must be known before this abs8 word compare can execute honestly.",
            )
        target_address = _mask_address(raw[1])
        data = _read_runtime_bytes(view, before_memory, target_address, 2)
        if data is None:
            return _blocked_result(
                before_cpu=before_cpu, decoded=decoded, status="runtime-memory-unavailable",
                note="This abs8 word compare needs 2 readable source bytes not available in overlay/bus.",
            )
        mem_value = int.from_bytes(data, "little")
        flags_updates = _compute_subtract_flags("word", mem_value, reg_val)
        return _executed_result(
            before_cpu=before_cpu, decoded=decoded, written_registers=("PC",),
            memory_writes=(), after_memory=before_memory, new_pc=decoded.next_sequential_pc,
            reg_updates=None, flags_updates=flags_updates,
            note=f"Executed cpw (0x{target_address & 0xFF:02X})=0x{mem_value:04X} - {reg_name}=0x{reg_val:04X}.",
        )

    return None


def _try_execute_abs24_word_memory(
    view: NgpcFetchView,
    before_cpu: NgpcCpuState,
    before_memory: dict[int, int],
    decoded: DecodeResult,
) -> ExecutionResult | None:
    """Execute the abs24 word-memory subset (prefix 0xD2)."""
    raw = decoded.raw_bytes
    if raw is None or raw[0] != 0xD2:
        return None

    if len(raw) == 7 and raw[4] == 0x19:
        # ldw (abs16), (abs24): mem-to-mem WORD move (abs24 src -> abs16 dest).
        # Word sibling of the C2 op-0x19 byte form. The real SNK BIOS hand-off
        # fills the 0x6C0x hand-off area from BIOS defaults, e.g.
        # `D2 42 E2 FF 19 04 6C` = ldw (0x6C04), (0xFFE242).
        src_address = _mask_address(int.from_bytes(raw[1:4], "little"))
        dest_address = _mask_address(int.from_bytes(raw[5:7], "little"))
        data = _read_runtime_bytes(view, before_memory, src_address, 2)
        if data is None:
            return _blocked_result(
                before_cpu=before_cpu, decoded=decoded, status="runtime-memory-unavailable",
                note="This abs24 word mem-to-mem move needs 2 readable source bytes, unavailable in overlay/bus.",
            )
        return _execute_absolute_store(
            view=view, before_cpu=before_cpu, before_memory=before_memory, decoded=decoded,
            target_address=dest_address, data=bytes(data),
            note=(f"Executed mem-to-mem word move ldw (0x{dest_address & 0xFFFF:04X}), "
                  f"(0x{src_address:06X})=0x{int.from_bytes(data, 'little'):04X}."),
            memory_note="Writable runtime overlay updated by abs24 word mem-to-mem move.",
        )

    if len(raw) == 5 and raw[4] == 0x04:
        target_address = _mask_address(int.from_bytes(raw[1:4], "little"))
        data = _read_runtime_bytes(view, before_memory, target_address, 2)
        if data is None:
            return _blocked_result(
                before_cpu=before_cpu,
                decoded=decoded,
                status="runtime-memory-unavailable",
                note=(
                    "This abs24 word push needs 2 readable source bytes, but neither the "
                    "writable runtime overlay nor the current read bus can provide them."
                ),
            )
        return _execute_push_bytes(
            view=view,
            before_cpu=before_cpu,
            before_memory=before_memory,
            decoded=decoded,
            data=data,
            note=(
                "Executed abs24 word push from the current real execution subset. Two bytes "
                "were read from the abs24 source and pushed onto the stack."
            ),
        )

    if len(raw) == 5 and 0x20 <= raw[4] <= 0x27:
        target_address = _mask_address(int.from_bytes(raw[1:4], "little"))
        data = _read_runtime_bytes(view, before_memory, target_address, 2)
        if data is None:
            return _blocked_result(
                before_cpu=before_cpu,
                decoded=decoded,
                status="runtime-memory-unavailable",
                note=(
                    "This abs24 word load needs 2 readable source bytes, but neither the "
                    "writable runtime overlay nor the current read bus can provide them."
                ),
            )
        value = int.from_bytes(data, "little")
        return _execute_register_immediate(
            before_cpu=before_cpu,
            before_memory=before_memory,
            decoded=decoded,
            size_kind="word",
            register_index=raw[4] & 0x07,
            value=value,
            note=(
                f"Executed abs24 word load ld R16, (0x{target_address:06X}): "
                f"value=0x{value:04X} into target R16."
            ),
        )

    if len(raw) == 7 and raw[4] == 0x3F:
        # cpw (abs24), imm16 — load the memory word, subtract the 16-bit
        # immediate, update the modeled flags. No write-back (compare only).
        # Battle-cart frontier `D2 56 47 00 3F FF 7F` = cpw (0x004756), 0x7FFF.
        target_address = _mask_address(int.from_bytes(raw[1:4], "little"))
        data = _read_runtime_bytes(view, before_memory, target_address, 2)
        if data is None:
            return _blocked_result(
                before_cpu=before_cpu,
                decoded=decoded,
                status="runtime-memory-unavailable",
                note=(
                    "This abs24 word compare needs 2 readable source bytes, but neither the "
                    "writable runtime overlay nor the current read bus can provide them."
                ),
            )
        mem_value = int.from_bytes(data, "little")
        imm = int.from_bytes(raw[5:7], "little")
        flags_updates = _compute_subtract_flags("word", mem_value, imm)
        return _executed_result(
            before_cpu=before_cpu,
            decoded=decoded,
            written_registers=("PC",),
            memory_writes=(),
            after_memory=before_memory,
            new_pc=decoded.next_sequential_pc,
            reg_updates=None,
            flags_updates=flags_updates,
            note=(
                "Executed abs24 word compare-immediate from the current real execution subset. "
                f"Flags = mem16(0x{target_address:06X})=0x{mem_value:04X} - imm=0x{imm:04X}."
            ),
        )

    if len(raw) == 5 and 0xF0 <= raw[4] <= 0xF7:
        target_address = _mask_address(int.from_bytes(raw[1:4], "little"))
        reg_name, reg_value = _extract_register_value(
            before_cpu=before_cpu,
            size_kind="word",
            register_index=raw[4] & 0x07,
        )
        if reg_value is None:
            return _blocked_result(
                before_cpu=before_cpu,
                decoded=decoded,
                status="requires-known-full-register",
                note=(
                    f"{reg_name} cannot be compared honestly until its current full value is "
                    "known in the CPU state."
                ),
            )
        data = _read_runtime_bytes(view, before_memory, target_address, 2)
        if data is None:
            return _blocked_result(
                before_cpu=before_cpu,
                decoded=decoded,
                status="runtime-memory-unavailable",
                note=(
                    "This abs24 word compare needs 2 readable source bytes, but neither the "
                    "writable runtime overlay nor the current read bus can provide them."
                ),
            )
        mem_value = int.from_bytes(data, "little")
        flags_updates = _compute_subtract_flags("word", reg_value, mem_value)
        return _executed_result(
            before_cpu=before_cpu,
            decoded=decoded,
            written_registers=("PC",),
            memory_writes=(),
            after_memory=before_memory,
            new_pc=decoded.next_sequential_pc,
            reg_updates=None,
            flags_updates=flags_updates,
            note=(
                "Executed abs24 word compare (register-minus-memory) from the current real "
                f"execution subset. Flags = {reg_name}=0x{reg_value:04X} - "
                f"mem16(0x{target_address:06X})=0x{mem_value:04X}."
            ),
        )

    if len(raw) == 5 and 0x60 <= raw[4] <= 0x6F:
        # inc/dec #n, (abs24) word RMW. CF preserved (matches other inc/dec-mem).
        # Baseball frontier `D2 F3 4B 00 61` = incw 1, (0x004BF3).
        target_address = _mask_address(int.from_bytes(raw[1:4], "little"))
        op_byte = raw[4]
        count = (op_byte & 0x07) or 8
        is_dec = op_byte >= 0x68
        data = _read_runtime_bytes(view, before_memory, target_address, 2)
        if data is None:
            return _blocked_result(
                before_cpu=before_cpu, decoded=decoded, status="runtime-memory-unavailable",
                note="This abs24 word inc/dec needs 2 readable source bytes, unavailable in overlay/bus.",
            )
        mem_value = int.from_bytes(data, "little")
        if is_dec:
            new_value = (mem_value - count) & 0xFFFF
            flags = dict(_compute_subtract_flags("word", mem_value, count))
        else:
            new_value = (mem_value + count) & 0xFFFF
            flags = dict(_compute_add_flags("word", mem_value, count))
        flags.pop("cf", None)  # inc/dec on memory preserves carry
        new_bytes = new_value.to_bytes(2, "little")
        after_memory = dict(before_memory)
        after_memory[target_address] = new_bytes[0]
        after_memory[_mask_address(target_address + 1)] = new_bytes[1]
        return _executed_result(
            before_cpu=before_cpu, decoded=decoded, written_registers=("PC",),
            memory_writes=(MemoryWrite(address=target_address, data=new_bytes,
                                       note=f"{'DECW' if is_dec else 'INCW'} #{count} (abs24) RMW."),),
            after_memory=after_memory, new_pc=decoded.next_sequential_pc,
            reg_updates=None, flags_updates=flags,
            note=(f"Executed {'decw' if is_dec else 'incw'} {count}, (0x{target_address:06X}) "
                  f"0x{mem_value:04X} -> 0x{new_value:04X} (CF preserved)."),
        )

    return None


def _try_execute_abs24_long_memory(
    view: NgpcFetchView,
    before_cpu: NgpcCpuState,
    before_memory: dict[int, int],
    decoded: DecodeResult,
) -> ExecutionResult | None:
    """Execute the abs24 LONG-memory subset (prefix 0xE2). Long mirror of 0xD2.

    Implemented: `ld R32, (abs24)` (op 0x20..0x27, 5 bytes) -- reads 4 bytes from
    the absolute 24-bit address into the destination R32. dialogue-cart frontier
    `E2 5A 49 00 20` = `ld XWA, (0x00495A)`.
    """
    raw = decoded.raw_bytes
    if raw is None:
        return None

    # E2 abs24 long (5 bytes, op at raw[4]) or E1 abs16 long (4 bytes, op at raw[3]).
    # Ogre Battle Gaiden frontier `E1 02 40 23` = ld XHL, (0x4002).
    if len(raw) == 5 and raw[0] == 0xE2 and 0x20 <= raw[4] <= 0x27:
        target_address = _mask_address(int.from_bytes(raw[1:4], "little"))
        register_index = raw[4] & 0x07
        addr_hex = f"0x{target_address:06X}"
    elif len(raw) == 4 and raw[0] == 0xE1 and 0x20 <= raw[3] <= 0x27:
        target_address = _mask_address(int.from_bytes(raw[1:3], "little"))
        register_index = raw[3] & 0x07
        addr_hex = f"0x{target_address:04X}"
    else:
        return None

    data = _read_runtime_bytes(view, before_memory, target_address, 4)
    if data is None:
        return _blocked_result(
            before_cpu=before_cpu,
            decoded=decoded,
            status="runtime-memory-unavailable",
            note=(
                "This abs long load needs 4 readable source bytes, but neither the "
                "writable runtime overlay nor the current read bus can provide them."
            ),
        )
    value = int.from_bytes(data, "little")
    return _execute_register_immediate(
        before_cpu=before_cpu,
        before_memory=before_memory,
        decoded=decoded,
        size_kind="long",
        register_index=register_index,
        value=value,
        note=f"Executed abs long load ld R32, ({addr_hex}): value=0x{value:08X} into target R32.",
    )


def _try_execute_abs8_long_memory(
    view: NgpcFetchView,
    before_cpu: NgpcCpuState,
    before_memory: dict[int, int],
    decoded: DecodeResult,
) -> ExecutionResult | None:
    raw = decoded.raw_bytes
    if raw is None or raw[0] != 0xE0 or len(raw) != 3:
        return None

    if 0x20 <= raw[2] <= 0x27:
        target_address = _mask_address(raw[1])
        data = _read_runtime_bytes(view, before_memory, target_address, 4)
        if data is None:
            return _blocked_result(
                before_cpu=before_cpu,
                decoded=decoded,
                status="runtime-memory-unavailable",
                note=(
                    "This abs8 long load needs 4 readable source bytes, but neither the "
                    "writable runtime overlay nor the current read bus can provide them."
                ),
            )
        value = int.from_bytes(data, "little")
        return _execute_register_immediate(
            before_cpu=before_cpu,
            before_memory=before_memory,
            decoded=decoded,
            size_kind="long",
            register_index=raw[2] & 0x07,
            value=value,
            note=(
                f"Executed abs8 long load ld R32, (0x{target_address:02X}): "
                f"value=0x{value:08X} into target R32."
            ),
        )

    return None


def _try_execute_pre_decrement_load(
    view: NgpcFetchView,
    before_cpu: NgpcCpuState,
    before_memory: dict[int, int],
    decoded: DecodeResult,
) -> ExecutionResult | None:
    raw = decoded.raw_bytes
    if raw is None or len(raw) != 3 or raw[0] not in (0xC4, 0xD4, 0xE4):
        return None
    if not (0x20 <= raw[2] <= 0x27):
        return None

    address_register_index = _post_increment_r32_index(raw[1])
    address_register_name = R32[address_register_index]
    address_field = REG32_FIELDS[address_register_index]
    base_address = getattr(before_cpu.regs, address_field)
    if base_address is None:
        return _blocked_result(
            before_cpu=before_cpu,
            decoded=decoded,
            status="requires-known-address-register",
            note=(
                f"{address_register_name} must be known before this pre-decrement memory form "
                "can compute its effective address honestly."
            ),
        )

    size_kind = {0xC4: "byte", 0xD4: "word", 0xE4: "long"}[raw[0]]
    width = {"byte": 1, "word": 2, "long": 4}[size_kind]
    decremented_address = (base_address - width) & 0xFFFFFFFF
    target_address = _mask_address(decremented_address)

    destination_index = raw[2] & 0x07
    if size_kind == "byte":
        destination_field = REG32_FIELDS[destination_index // 2]
        destination_name = R8[destination_index]
    elif size_kind == "word":
        destination_field = REG32_FIELDS[destination_index]
        destination_name = R16[destination_index]
    else:
        destination_field = REG32_FIELDS[destination_index]
        destination_name = R32[destination_index]

    if destination_field == address_field:
        return _blocked_result(
            before_cpu=before_cpu,
            decoded=decoded,
            status="unmodeled-register-alias-side-effects",
            note=(
                f"{destination_name} aliases {address_register_name}, and this pre-decrement "
                f"{size_kind} load would need alias ordering the current subset does not model yet."
            ),
        )

    data = _read_runtime_bytes(view, before_memory, target_address, width)
    if data is None:
        return _blocked_result(
            before_cpu=before_cpu,
            decoded=decoded,
            status="runtime-memory-unavailable",
            note=(
                f"This pre-decrement {size_kind} load needs readable source bytes, but neither "
                "the writable runtime overlay nor the current read bus can provide them."
            ),
        )

    destination_name, reg_updates = _build_register_update(
        before_cpu=before_cpu,
        size_kind=size_kind,
        register_index=destination_index,
        value=int.from_bytes(data, "little"),
    )
    if reg_updates is None:
        owner_name = R32[destination_index // 2] if size_kind == "byte" else R32[destination_index]
        return _blocked_result(
            before_cpu=before_cpu,
            decoded=decoded,
            status="requires-known-full-register",
            note=(
                f"{destination_name} cannot be updated honestly until {owner_name} is already "
                "known in the current CPU state."
            ),
        )

    reg_updates[address_field] = decremented_address
    return _executed_result(
        before_cpu=before_cpu,
        decoded=decoded,
        written_registers=(destination_name, address_register_name, "PC"),
        memory_writes=(),
        after_memory=before_memory,
        new_pc=decoded.next_sequential_pc,
        reg_updates=reg_updates,
        note=(
            f"Executed pre-decrement {size_kind} load from the current real execution subset. "
            "The address register was decremented before the access and the loaded bytes were "
            "taken from the readable runtime view."
        ),
    )


def _try_execute_abs16_byte_memory(
    view: NgpcFetchView,
    before_cpu: NgpcCpuState,
    before_memory: dict[int, int],
    decoded: DecodeResult,
) -> ExecutionResult | None:
    raw = decoded.raw_bytes
    if raw is None or raw[0] not in (0xC0, 0xC1):
        return None

    # ld (abs16), (abs16) -- memory-to-memory BYTE move (abs16 src -> abs16 dest).
    # Card Fighters Clash frontier `C1 08 80 19 BA 4F` = ld (0x4FBA), (0x8008).
    if raw[0] == 0xC1 and len(raw) == 6 and raw[3] == 0x19:
        src_address = _mask_address(int.from_bytes(raw[1:3], "little"))
        dest_address = _mask_address(int.from_bytes(raw[4:6], "little"))
        data = _read_runtime_bytes(view, before_memory, src_address, 1)
        if data is None:
            return _blocked_result(
                before_cpu=before_cpu, decoded=decoded, status="runtime-memory-unavailable",
                note="This abs16 mem-to-mem byte move needs a readable source byte, unavailable in overlay/bus.",
            )
        return _execute_absolute_store(
            view=view, before_cpu=before_cpu, before_memory=before_memory, decoded=decoded,
            target_address=dest_address, data=bytes(data),
            note=(f"Executed mem-to-mem byte move ld (0x{dest_address & 0xFFFF:04X}), "
                  f"(0x{src_address & 0xFFFF:04X})=0x{data[0]:02X}."),
            memory_note="Writable runtime overlay updated by abs16 mem-to-mem byte move.",
        )

    # ld (abs16), (abs8) -- memory-to-memory BYTE move (abs8 src -> abs16 dest).
    # The abs8-source sibling of the C1/C2 op-0x19 forms. The real SNK BIOS
    # VBlank frame handler uses `C0 B2 19 85 6E` = ld (0x6E85), (0xB2). The abs8
    # source is zero-extended into the CPU I/O page (0x0000xx).
    if raw[0] == 0xC0 and len(raw) == 5 and raw[2] == 0x19:
        src_address = _mask_address(raw[1])
        dest_address = _mask_address(int.from_bytes(raw[3:5], "little"))
        data = _read_runtime_bytes(view, before_memory, src_address, 1)
        if data is None:
            return _blocked_result(
                before_cpu=before_cpu, decoded=decoded, status="runtime-memory-unavailable",
                note="This abs8 mem-to-mem byte move needs a readable source byte, unavailable in overlay/bus.",
            )
        return _execute_absolute_store(
            view=view, before_cpu=before_cpu, before_memory=before_memory, decoded=decoded,
            target_address=dest_address, data=bytes(data),
            note=(f"Executed mem-to-mem byte move ld (0x{dest_address & 0xFFFF:04X}), "
                  f"(0x{src_address & 0xFF:02X})=0x{data[0]:02X}."),
            memory_note="Writable runtime overlay updated by abs8 mem-to-mem byte move.",
        )

    # Shared abs8 (C0) / abs16 (C1) byte-memory family. They differ only in the
    # address width: C0 carries a 1-byte address (zero-extended into the CPU
    # I/O page 0x0000xx) and C1 a 2-byte address. Everything below is width-
    # agnostic once the address, op byte and optional immediate are extracted.
    addr_len = 1 if raw[0] == 0xC0 else 2
    base = 1 + addr_len            # index of the op byte
    if len(raw) not in (base + 1, base + 2):
        return None
    target_address = _mask_address(int.from_bytes(raw[1:1 + addr_len], "little"))
    op = raw[base]
    imm = raw[base + 1] if len(raw) == base + 2 else None

    # ex (mem), R8 -- exchange the memory byte with a byte register (len base+1).
    # Ganbare frontier `C1 A0 44 36` = ex (0x44A0), H.
    if len(raw) == base + 1 and 0x30 <= op <= 0x37:
        r8_index = op & 0x07
        r8_name, r8_value = _extract_register_value(before_cpu, "byte", r8_index)
        if r8_value is None:
            return _blocked_result(
                before_cpu=before_cpu, decoded=decoded, status="requires-known-full-register",
                note=f"{r8_name} must be known before this abs16 byte exchange can execute.",
            )
        source = _read_runtime_bytes(view, before_memory, target_address, 1)
        if source is None:
            return _blocked_result(
                before_cpu=before_cpu, decoded=decoded, status="runtime-memory-unavailable",
                note="This abs16 byte exchange needs a readable memory byte, unavailable in overlay/bus.",
            )
        mem_value = source[0]
        reg_name, reg_updates = _build_register_update(before_cpu, "byte", r8_index, mem_value)
        if reg_updates is None:
            return _blocked_result(
                before_cpu=before_cpu, decoded=decoded, status="requires-known-full-register",
                note=f"{r8_name} owner must be known to complete this exchange.",
            )
        after_memory = dict(before_memory)
        after_memory[target_address] = r8_value & 0xFF
        return _executed_result(
            before_cpu=before_cpu, decoded=decoded, written_registers=(reg_name, "PC"),
            memory_writes=(MemoryWrite(address=target_address, data=bytes([r8_value & 0xFF]),
                                       note=f"EX (abs16), {r8_name} memory side."),),
            after_memory=after_memory, new_pc=decoded.next_sequential_pc,
            reg_updates=reg_updates, flags_updates=None,
            note=(f"Executed ex (0x{target_address & 0xFFFF:04X})=0x{mem_value:02X}, {r8_name}=0x{r8_value & 0xFF:02X} "
                  "(swapped)."),
        )

    # inc/dec N, (mem) byte form has no immediate (len == base + 1).
    # Catalog: encode_mem_abs*_inc_dec — count_code 0 means 8.
    if len(raw) == base + 1 and 0x60 <= op <= 0x6F:
        count_code = op & 0x07
        count = 8 if count_code == 0 else count_code
        is_dec = op >= 0x68
        source = _read_runtime_bytes(view, before_memory, target_address, 1)
        if source is None:
            return _blocked_result(
                before_cpu=before_cpu,
                decoded=decoded,
                status="runtime-memory-unavailable",
                note=(
                    "This abs16 byte inc/dec needs a readable source byte, but neither the "
                    "writable runtime overlay nor the current read bus can provide it."
                ),
            )
        old_value = source[0]
        if is_dec:
            new_value = (old_value - count) & 0xFF
            mnemonic = "decb"
        else:
            new_value = (old_value + count) & 0xFF
            mnemonic = "incb"

        write_status, write_note = _check_writable_range(view, target_address, 1)
        if write_status is not None and write_status != "write-discarded":
            return _blocked_result(
                before_cpu=before_cpu,
                decoded=decoded,
                status=write_status,
                note=write_note,
            )

        # Flags: TLCS-900 updates Z/S/V/H on inc/dec mem; CF is preserved
        # (unlike ADD/SUB which update CF). We reuse the standard add/sub
        # flag helpers but strip CF so the existing carry stays unmodified.
        # N (negative/sub) is documented but not tracked in this CPU model.
        if is_dec:
            flags_updates = dict(_compute_subtract_flags("byte", old_value, count))
        else:
            flags_updates = dict(_compute_add_flags("byte", old_value, count))
        flags_updates.pop("cf", None)

        if write_status == "write-discarded":
            after_memory = dict(before_memory)
            mem_write = MemoryWrite(
                address=target_address,
                data=bytes((new_value,)),
                note=f"[DISCARDED] {write_note}",
            )
        else:
            after_memory = dict(before_memory)
            after_memory[target_address] = new_value
            mem_write = MemoryWrite(
                address=target_address,
                data=bytes((new_value,)),
                note="Writable runtime overlay updated by abs16 byte inc/dec.",
            )
        return _executed_result(
            before_cpu=before_cpu,
            decoded=decoded,
            written_registers=("PC",),
            memory_writes=(mem_write,),
            after_memory=after_memory,
            new_pc=decoded.next_sequential_pc,
            reg_updates=None,
            flags_updates=flags_updates,
            note=(
                f"Executed {mnemonic} {count}, (0x{target_address & 0xFFFF:04X}): "
                f"mem8 0x{old_value:02X} -> 0x{new_value:02X}. ZF/SF updated; "
                "VF/HF/NF intentionally left unchanged."
            ),
        )

    if len(raw) == base + 1 and 0x20 <= op <= 0x27:
        data = _read_runtime_bytes(view, before_memory, target_address, 1)
        if data is None:
            return _blocked_result(
                before_cpu=before_cpu,
                decoded=decoded,
                status="runtime-memory-unavailable",
                note=(
                    "This abs byte load needs a readable source byte, but neither the "
                    "writable runtime overlay nor the current read bus can provide it."
                ),
            )

        return _execute_register_immediate(
            before_cpu=before_cpu,
            before_memory=before_memory,
            decoded=decoded,
            size_kind="byte",
            register_index=op & 0x07,
            value=data[0],
            note=(
                "Executed abs16 byte load from the current real execution subset. The source "
                "byte was read from the writable runtime overlay or the current read bus."
            ),
        )

    if len(raw) == base + 2 and op == 0x3F:
        data = _read_runtime_bytes(view, before_memory, target_address, 1)
        if data is None:
            return _blocked_result(
                before_cpu=before_cpu,
                decoded=decoded,
                status="runtime-memory-unavailable",
                note=(
                    "This abs byte compare needs a readable source byte, but neither the "
                    "writable runtime overlay nor the current read bus can provide it."
                ),
            )

        flags_updates = _compute_subtract_flags("byte", data[0], imm)
        return _executed_result(
            before_cpu=before_cpu,
            decoded=decoded,
            written_registers=("PC",),
            memory_writes=(),
            after_memory=before_memory,
            new_pc=decoded.next_sequential_pc,
            reg_updates=None,
            flags_updates=flags_updates,
            note=(
                "Executed abs16 byte compare-immediate from the current real execution subset. "
                "The source byte came from the writable runtime overlay or the current read "
                "bus and the modeled flag subset now reflects the subtraction result."
            ),
        )

    if len(raw) == base + 2 and 0x38 <= op <= 0x3E:
        data = _read_runtime_bytes(view, before_memory, target_address, 1)
        if data is None:
            return _blocked_result(
                before_cpu=before_cpu,
                decoded=decoded,
                status="runtime-memory-unavailable",
                note=(
                    "This abs byte ALU-immediate needs a readable source byte, but neither the "
                    "writable runtime overlay nor the current read bus can provide it."
                ),
            )

        imm8 = imm
        mem_value = data[0]
        operation = {
            0x38: "add",
            0x39: "adc",
            0x3A: "sub",
            0x3B: "sbc",
            0x3C: "and",
            0x3D: "xor",
            0x3E: "or",
        }[op]
        carry = 0
        if operation in ("adc", "sbc"):
            if before_cpu.flags.cf is None:
                return _blocked_result(
                    before_cpu=before_cpu,
                    decoded=decoded,
                    status="runtime-state-required",
                    note=(
                        f"{operation.upper()} on abs16 byte memory requires a known carry flag, "
                        "which is not modeled in the current CPU state."
                    ),
                )
            carry = int(before_cpu.flags.cf)

        if operation == "add":
            result = (mem_value + imm8) & 0xFF
            flags_updates = _compute_add_flags("byte", mem_value, imm8)
        elif operation == "adc":
            result = (mem_value + imm8 + carry) & 0xFF
            flags_updates = _compute_add_flags("byte", mem_value, imm8 + carry)
        elif operation == "sub":
            result = (mem_value - imm8) & 0xFF
            flags_updates = _compute_subtract_flags("byte", mem_value, imm8)
        elif operation == "sbc":
            result = (mem_value - imm8 - carry) & 0xFF
            flags_updates = _compute_subtract_flags("byte", mem_value, imm8 + carry)
        elif operation == "and":
            result = mem_value & imm8
            flags_updates = _compute_logical_flags("byte", result, half_carry=True)
        elif operation == "xor":
            result = mem_value ^ imm8
            flags_updates = _compute_logical_flags("byte", result)
        else:
            result = mem_value | imm8
            flags_updates = _compute_logical_flags("byte", result)

        write_status, write_note = _check_writable_range(view, target_address, 1)
        if write_status == "write-discarded":
            return _executed_result(
                before_cpu=before_cpu,
                decoded=decoded,
                written_registers=("PC",),
                memory_writes=(
                    MemoryWrite(
                        address=target_address,
                        data=bytes((result,)),
                        note=f"[DISCARDED] {write_note}",
                    ),
                ),
                after_memory=before_memory,
                new_pc=decoded.next_sequential_pc,
                reg_updates=None,
                flags_updates=flags_updates,
                note=(
                    "Abs16 byte ALU-immediate destination was unmapped or read-only; write "
                    "silently discarded (open-bus behavior - execution continues)."
                ),
            )
        if write_status is not None:
            return _blocked_result(
                before_cpu=before_cpu,
                decoded=decoded,
                status=write_status,
                note=write_note,
            )

        after_memory = dict(before_memory)
        after_memory[target_address] = result
        return _executed_result(
            before_cpu=before_cpu,
            decoded=decoded,
            written_registers=("PC",),
            memory_writes=(
                MemoryWrite(
                    address=target_address,
                    data=bytes((result,)),
                    note=f"Writable runtime overlay updated by abs16 byte {operation.upper()} immediate execution.",
                ),
            ),
            after_memory=after_memory,
            new_pc=decoded.next_sequential_pc,
            reg_updates=None,
            flags_updates=flags_updates,
            note=(
                f"Executed abs16 byte {operation.upper()}-immediate from the current real "
                f"execution subset. mem8(0x{target_address:04X})=0x{mem_value:02X}, "
                f"imm=0x{imm8:02X} -> 0x{result:02X}."
            ),
        )

    # Byte ALU with abs memory source/dest: R8 <op> (mem) and (mem) <op> R8.
    # op high nibble = operation, bit3 = direction ((mem),R8), low 3 = R8 index.
    # Verified against our NGPC disassembler. The real SNK BIOS boot sums HW
    # registers into A here (`add A,(abs16)` = C1 lo hi 0x81).
    if len(raw) == base + 1 and 0x80 <= op <= 0xFF:
        operation = {
            0x8: "add", 0x9: "adc", 0xA: "sub", 0xB: "sbc",
            0xC: "and", 0xD: "xor", 0xE: "or", 0xF: "cp",
        }[(op >> 4) & 0xF]
        mem_is_dest = bool(op & 0x08)
        reg_index = op & 0x07
        reg_name, reg_value = _extract_register_value(before_cpu, "byte", reg_index)
        if reg_value is None:
            return _blocked_result(
                before_cpu=before_cpu,
                decoded=decoded,
                status="requires-known-full-register",
                note=(
                    f"{reg_name} must be known before this abs byte {operation.upper()} with a "
                    "register operand can execute honestly."
                ),
            )
        data = _read_runtime_bytes(view, before_memory, target_address, 1)
        if data is None:
            return _blocked_result(
                before_cpu=before_cpu,
                decoded=decoded,
                status="runtime-memory-unavailable",
                note=(
                    "This abs byte ALU needs a readable source byte, but neither the writable "
                    "runtime overlay nor the current read bus can provide it."
                ),
            )
        mem_value = data[0]
        # dest <op> src: for R8-dest the register is dest and memory the source;
        # for (mem)-dest the memory is dest and the register the source.
        dst_val, src_val = (mem_value, reg_value) if mem_is_dest else (reg_value, mem_value)

        carry = 0
        if operation in ("adc", "sbc"):
            if before_cpu.flags.cf is None:
                return _blocked_result(
                    before_cpu=before_cpu,
                    decoded=decoded,
                    status="runtime-state-required",
                    note=(
                        f"{operation.upper()} on abs byte memory requires a known carry flag, "
                        "which is not modeled in the current CPU state."
                    ),
                )
            carry = int(before_cpu.flags.cf)

        if operation == "add":
            result = (dst_val + src_val) & 0xFF
            flags_updates = _compute_add_flags("byte", dst_val, src_val)
        elif operation == "adc":
            result = (dst_val + src_val + carry) & 0xFF
            flags_updates = _compute_add_flags("byte", dst_val, src_val + carry)
        elif operation == "sub":
            result = (dst_val - src_val) & 0xFF
            flags_updates = _compute_subtract_flags("byte", dst_val, src_val)
        elif operation == "sbc":
            result = (dst_val - src_val - carry) & 0xFF
            flags_updates = _compute_subtract_flags("byte", dst_val, src_val + carry)
        elif operation == "cp":
            result = (dst_val - src_val) & 0xFF
            flags_updates = _compute_subtract_flags("byte", dst_val, src_val)
        elif operation == "and":
            result = dst_val & src_val
            flags_updates = _compute_logical_flags("byte", result, half_carry=True)
        elif operation == "xor":
            result = dst_val ^ src_val
            flags_updates = _compute_logical_flags("byte", result)
        else:  # or
            result = dst_val | src_val
            flags_updates = _compute_logical_flags("byte", result)

        mem_hex = f"0x{target_address & 0xFFFF:04X}"
        if operation == "cp":
            # Flags only, no writeback (either direction).
            return _executed_result(
                before_cpu=before_cpu,
                decoded=decoded,
                written_registers=("PC",),
                memory_writes=(),
                after_memory=before_memory,
                new_pc=decoded.next_sequential_pc,
                reg_updates=None,
                flags_updates=flags_updates,
                note=(
                    f"Executed abs byte CP ({reg_name} vs mem8({mem_hex})=0x{mem_value:02X}); "
                    "flags only, no writeback."
                ),
            )

        if not mem_is_dest:
            # Register destination: write result back into R8.
            _, reg_updates = _build_register_update(before_cpu, "byte", reg_index, result)
            if reg_updates is None:
                return _blocked_result(
                    before_cpu=before_cpu,
                    decoded=decoded,
                    status="requires-known-full-register",
                    note=(
                        f"{reg_name}'s owning register must be known before this abs byte "
                        f"{operation.upper()} can write its result honestly."
                    ),
                )
            return _executed_result(
                before_cpu=before_cpu,
                decoded=decoded,
                written_registers=(reg_name, "PC"),
                memory_writes=(),
                after_memory=before_memory,
                new_pc=decoded.next_sequential_pc,
                reg_updates=reg_updates,
                flags_updates=flags_updates,
                note=(
                    f"Executed abs byte {operation.upper()} {reg_name}, (mem8({mem_hex})="
                    f"0x{mem_value:02X}) -> {reg_name}=0x{result:02X}."
                ),
            )

        # Memory destination: write result back to the abs address.
        write_status, write_note = _check_writable_range(view, target_address, 1)
        if write_status is not None and write_status != "write-discarded":
            return _blocked_result(
                before_cpu=before_cpu, decoded=decoded, status=write_status, note=write_note,
            )
        if write_status == "write-discarded":
            return _executed_result(
                before_cpu=before_cpu,
                decoded=decoded,
                written_registers=("PC",),
                memory_writes=(
                    MemoryWrite(
                        address=target_address,
                        data=bytes((result,)),
                        note=f"[DISCARDED] {write_note}",
                    ),
                ),
                after_memory=before_memory,
                new_pc=decoded.next_sequential_pc,
                reg_updates=None,
                flags_updates=flags_updates,
                note=(
                    f"Abs byte {operation.upper()} (mem),{reg_name} destination was unmapped or "
                    "read-only; write silently discarded (open-bus)."
                ),
            )
        after_memory = dict(before_memory)
        after_memory[target_address] = result
        return _executed_result(
            before_cpu=before_cpu,
            decoded=decoded,
            written_registers=("PC",),
            memory_writes=(
                MemoryWrite(
                    address=target_address,
                    data=bytes((result,)),
                    note=f"Writable runtime overlay updated by abs byte {operation.upper()} (mem),R8.",
                ),
            ),
            after_memory=after_memory,
            new_pc=decoded.next_sequential_pc,
            reg_updates=None,
            flags_updates=flags_updates,
            note=(
                f"Executed abs byte {operation.upper()} (mem8({mem_hex})=0x{mem_value:02X}), "
                f"{reg_name} -> 0x{result:02X}."
            ),
        )

    return None


def _try_execute_lda_abs8(
    before_cpu: NgpcCpuState,
    before_memory: dict[int, int],
    decoded: DecodeResult,
) -> ExecutionResult | None:
    """Execute `lda R, (abs8)` (prefix 0xF0): load the effective abs8 address.

    LDA does not read memory — it loads the operand's effective address into R.
    For an abs8 operand `(0xnn)` that address is the zero-extended CPU-I/O-page
    address 0x0000nn. No flags change. op 0x20..0x27 -> R16, 0x30..0x37 -> R32.
    """
    raw = decoded.raw_bytes
    if raw is None or raw[0] != 0xF0 or len(raw) != 3 or decoded.mnemonic != "lda":
        return None
    address = raw[1]              # effective address = 0x0000nn
    op = raw[2]
    if 0x20 <= op <= 0x27:
        size_kind, reg_index = "word", op & 0x07
    elif 0x30 <= op <= 0x37:
        size_kind, reg_index = "long", op & 0x07
    else:
        return None

    reg_name, reg_updates = _build_register_update(before_cpu, size_kind, reg_index, address)
    if reg_updates is None:
        # A word destination needs its owning 32-bit register known so the high
        # 16 bits are preserved; stop honestly otherwise.
        return _blocked_result(
            before_cpu=before_cpu,
            decoded=decoded,
            status="requires-known-full-register",
            note=(
                f"{reg_name}'s owning register must be known before `lda {reg_name}, "
                f"(0x{address:02X})` can write its low half honestly."
            ),
        )
    return _executed_result(
        before_cpu=before_cpu,
        decoded=decoded,
        written_registers=(reg_name, "PC"),
        memory_writes=(),
        after_memory=before_memory,
        new_pc=decoded.next_sequential_pc,
        reg_updates=reg_updates,
        note=(
            f"Executed `lda {reg_name}, (0x{address:02X})`: {reg_name} <- effective address "
            f"0x{address:06X} (no memory access, no flags changed)."
        ),
    )


def _execute_absolute_store(
    view: NgpcFetchView,
    before_cpu: NgpcCpuState,
    before_memory: dict[int, int],
    decoded: DecodeResult,
    target_address: int,
    data: bytes,
    note: str,
    memory_note: str,
) -> ExecutionResult:
    write_status, write_note = _check_writable_range(view, target_address, len(data))
    if write_status == "write-discarded":
        # Real hardware silently discards writes to unmapped / ROM addresses.
        # Execution continues; the destination memory is not updated.
        return _executed_result(
            before_cpu=before_cpu,
            decoded=decoded,
            written_registers=("PC",),
            memory_writes=(
                MemoryWrite(
                    address=target_address,
                    data=data,
                    note=f"[DISCARDED] {write_note}",
                ),
            ),
            after_memory=before_memory,
            new_pc=decoded.next_sequential_pc,
            reg_updates=None,
            note=(
                f"{note} The destination address was unmapped or read-only; the write was "
                "silently discarded (open-bus behavior — execution continues)."
            ),
        )
    if write_status is not None:
        return _blocked_result(
            before_cpu=before_cpu,
            decoded=decoded,
            status=write_status,
            note=write_note,
        )

    after_memory = dict(before_memory)
    for offset, value in enumerate(data):
        after_memory[_mask_address(target_address + offset)] = value

    return _executed_result(
        before_cpu=before_cpu,
        decoded=decoded,
        written_registers=("PC",),
        memory_writes=(
            MemoryWrite(
                address=target_address,
                data=data,
                note=memory_note,
            ),
        ),
        after_memory=after_memory,
        new_pc=decoded.next_sequential_pc,
        reg_updates=None,
        note=note,
    )


def _try_execute_reg_indirect_load(
    view: NgpcFetchView,
    before_cpu: NgpcCpuState,
    before_memory: dict[int, int],
    decoded: DecodeResult,
) -> ExecutionResult | None:
    """Execute (r32) byte-indirect instructions.

    Encoding: [0x80+r32_idx] [op] [optional extra]
    op=0x20..0x27: ld R8, (r32)         — 2 bytes
    op=0x3F:       cp (r32), imm8       — 3 bytes
    op=0xF0..0xF7: cp R8, (r32)         — 2 bytes (pass 51)
                  Compare R8 with mem byte ; flags = R8 - mem ;
                  no memory write.
                  Source : ngdis/tlcs900_zz_mem.c case 0xF0
    """
    raw = decoded.raw_bytes
    if raw is None or not (0x80 <= raw[0] <= 0x87):
        return None
    op = raw[1] if len(raw) >= 2 else None
    if op is None:
        return None
    is_2byte_supported = (
        (0x20 <= op <= 0x27)            # LD R8, (R32) load
        or (0x30 <= op <= 0x37)         # EX (R32), R8 (pass 55)
        or (0x60 <= op <= 0x6F)         # INC/DEC #n, (R32) (pass 56)
        or (0x78 <= op <= 0x7F)         # shift family on (R32) (pass 56)
        or (0x80 <= op <= 0x8F)         # ADD R8/(R32) both directions (pass 54)
        or (0x90 <= op <= 0x9F)         # ADC R8/(R32) both directions (pass 55)
        or (0xA0 <= op <= 0xAF)         # SUB R8/(R32) both directions (pass 54)
        or (0xB0 <= op <= 0xBF)         # SBC R8/(R32) both directions (pass 55)
        or (0xC0 <= op <= 0xEF)         # AND/OR/XOR R8/(R32) both directions
        or (0xF0 <= op <= 0xFF)         # CP R8/(R32) both directions (pass 51 + 55)
    )
    is_3byte_supported = 0x38 <= op <= 0x3F  # (R32), imm8 ALU + CP imm (pass 55)
    if len(raw) == 2 and not is_2byte_supported:
        return None
    if len(raw) == 3 and not is_3byte_supported:
        return None
    if len(raw) not in (2, 3):
        return None

    r32_index = raw[0] & 0x07
    r32_field = REG32_FIELDS[r32_index]
    r32_name = R32[r32_index]
    r32_value = getattr(before_cpu.regs, r32_field)
    if r32_value is None:
        return _blocked_result(
            before_cpu=before_cpu,
            decoded=decoded,
            status="requires-known-address-register",
            note=(
                f"{r32_name} must be known before this register-indirect load can compute "
                "its source address honestly."
            ),
        )

    source_address = _mask_address(r32_value)
    data_bytes = _read_runtime_bytes(view, before_memory, source_address, 1)
    if data_bytes is None:
        return _blocked_result(
            before_cpu=before_cpu,
            decoded=decoded,
            status="runtime-memory-unavailable",
            note=(
                f"This register-indirect load needs a readable byte at ({r32_name})="
                f"0x{source_address:06X}, but neither the writable runtime overlay nor "
                "the current read bus can provide it."
            ),
        )

    if 0x38 <= raw[1] <= 0x3F:
        # Pass 55 : ALU (R32), imm8 — 3-byte RMW byte family.
        # Sub-op map (per ngdis/tlcs900_zz_mem.c) :
        #   0x38 = ADD   0x39 = ADC   0x3A = SUB   0x3B = SBC
        #   0x3C = AND   0x3D = XOR   0x3E = OR    0x3F = CP (no write)
        # ADC/SBC need a known C flag (block honestly if unknown).
        sub_op = raw[1]
        imm8 = raw[2]
        mem_byte = data_bytes[0]
        op_name = {
            0x38: "add", 0x39: "adc", 0x3A: "sub", 0x3B: "sbc",
            0x3C: "and", 0x3D: "xor", 0x3E: "or",  0x3F: "cp",
        }[sub_op]
        needs_carry = sub_op in (0x39, 0x3B)
        if needs_carry and before_cpu.flags.cf is None:
            return _blocked_result(
                before_cpu=before_cpu,
                decoded=decoded,
                status="runtime-state-required",
                note=(
                    f"{op_name.upper()} ({r32_name}), imm8 requires a known carry "
                    "flag, which is not modeled in the current CPU state."
                ),
            )
        carry = int(before_cpu.flags.cf) if needs_carry else 0
        if sub_op in (0x38, 0x39):  # ADD / ADC
            result = (mem_byte + imm8 + carry) & 0xFF
            flags_updates = _compute_add_flags("byte", mem_byte, imm8 + carry)
        elif sub_op in (0x3A, 0x3B, 0x3F):  # SUB / SBC / CP
            result = (mem_byte - imm8 - carry) & 0xFF
            flags_updates = _compute_subtract_flags("byte", mem_byte, imm8 + carry)
        elif sub_op == 0x3C:  # AND
            result = mem_byte & imm8
            flags_updates = _compute_logical_flags("byte", result, half_carry=True)
        elif sub_op == 0x3D:  # XOR
            result = mem_byte ^ imm8
            flags_updates = _compute_logical_flags("byte", result)
        else:  # 0x3E OR
            result = mem_byte | imm8
            flags_updates = _compute_logical_flags("byte", result)

        if sub_op == 0x3F:
            # CP : flags only, no write.
            return _executed_result(
                before_cpu=before_cpu,
                decoded=decoded,
                written_registers=("PC",),
                memory_writes=(),
                after_memory=before_memory,
                new_pc=decoded.next_sequential_pc,
                reg_updates=None,
                flags_updates=flags_updates,
                note=(
                    f"Executed cp ({r32_name})=0x{source_address:06X}=0x{mem_byte:02X}, "
                    f"0x{imm8:02X}."
                ),
            )
        after_memory = dict(before_memory)
        after_memory[_mask_address(source_address)] = result
        return _executed_result(
            before_cpu=before_cpu,
            decoded=decoded,
            written_registers=("PC",),
            memory_writes=(
                MemoryWrite(
                    address=_mask_address(source_address),
                    data=bytes((result,)),
                    note=f"{op_name.upper()} ({r32_name}), imm8 : mem byte updated.",
                ),
            ),
            after_memory=after_memory,
            new_pc=decoded.next_sequential_pc,
            reg_updates=None,
            flags_updates=flags_updates,
            note=(
                f"Executed {op_name} ({r32_name}=0x{source_address:06X})=0x{mem_byte:02X}, "
                f"0x{imm8:02X} → mem=0x{result:02X}."
            ),
        )

    if 0xF0 <= raw[1] <= 0xFF:
        # cp R8, (r32) [0xF0..0xF7]   — pass 51 — flags = R8 - mem
        # cp (r32), R8 [0xF8..0xFF]   — pass 55 — flags = mem - R8
        # Both : no register or memory write.
        # Source : ngdis/tlcs900_zz_mem.c case 0xF0 "CP R,(mem)" + case 0xF8 "CP (mem),R".
        sub_op = raw[1]
        mem_on_left = sub_op >= 0xF8
        r8_index = sub_op & 0x07
        r8_name, r8_value = _extract_register_value(
            before_cpu, "byte", r8_index,
        )
        if r8_value is None:
            return _blocked_result(
                before_cpu=before_cpu,
                decoded=decoded,
                status="requires-known-source-register",
                note=(
                    f"CP {r8_name}, ({r32_name}) needs the value of {r8_name} "
                    f"(owner = {R32[r8_index // 2]}) to be modeled."
                ),
            )
        if mem_on_left:
            flags_updates = _compute_subtract_flags(
                "byte", data_bytes[0], r8_value,
            )
            note = (
                f"Executed cp ({r32_name}), {r8_name}. Flags = "
                f"mem({r32_name}=0x{source_address:06X})=0x{data_bytes[0]:02X} - "
                f"{r8_name}=0x{r8_value:02X}."
            )
        else:
            flags_updates = _compute_subtract_flags(
                "byte", r8_value, data_bytes[0],
            )
            note = (
                f"Executed cp {r8_name}, ({r32_name}). Flags = "
                f"{r8_name}=0x{r8_value:02X} - mem({r32_name}=0x{source_address:06X})"
                f"=0x{data_bytes[0]:02X}."
            )
        return _executed_result(
            before_cpu=before_cpu,
            decoded=decoded,
            written_registers=("PC",),
            memory_writes=(),
            after_memory=before_memory,
            new_pc=decoded.next_sequential_pc,
            reg_updates=None,
            flags_updates=flags_updates,
            note=note,
        )

    if 0x30 <= raw[1] <= 0x37:
        # Pass 55 : EX (R32), R8 — swap mem byte with R8 ; flags unchanged.
        # Source : ngdis/tlcs900_zz_mem.c case 0x30 "EX (mem),R".
        r8_index = raw[1] & 0x07
        r8_name, r8_value = _extract_register_value(
            before_cpu, "byte", r8_index,
        )
        if r8_value is None:
            return _blocked_result(
                before_cpu=before_cpu,
                decoded=decoded,
                status="requires-known-source-register",
                note=(
                    f"EX ({r32_name}), {r8_name} needs the value of {r8_name} "
                    f"(owner = {R32[r8_index // 2]}) to be modeled."
                ),
            )
        mem_byte = data_bytes[0]
        # mem ← old R8 ; R8 ← old mem.
        result_name, reg_updates = _build_register_update(
            before_cpu, "byte", r8_index, mem_byte,
        )
        if reg_updates is None:
            return _blocked_result(
                before_cpu=before_cpu,
                decoded=decoded,
                status="requires-known-full-register",
                note=(
                    f"EX ({r32_name}), {r8_name} needs the owner register of "
                    f"{r8_name} fully known to write back."
                ),
            )
        after_memory = dict(before_memory)
        after_memory[_mask_address(source_address)] = r8_value
        return _executed_result(
            before_cpu=before_cpu,
            decoded=decoded,
            written_registers=(result_name, "PC"),
            memory_writes=(
                MemoryWrite(
                    address=_mask_address(source_address),
                    data=bytes((r8_value,)),
                    note=f"EX ({r32_name}), {r8_name} : mem byte ← old R8.",
                ),
            ),
            after_memory=after_memory,
            new_pc=decoded.next_sequential_pc,
            reg_updates=reg_updates,
            flags_updates=None,
            note=(
                f"Executed ex ({r32_name}=0x{source_address:06X})=0x{mem_byte:02X}, "
                f"{r8_name}=0x{r8_value:02X} : swapped values "
                f"(mem ← 0x{r8_value:02X}, {result_name} ← 0x{mem_byte:02X})."
            ),
        )

    if 0x60 <= raw[1] <= 0x6F:
        # Pass 56 : INC #n, (R32) / DEC #n, (R32) — RMW with 3-bit immediate.
        # n = (sub_op & 0x07) ; 0 → 8 (Toshiba spec quirk).
        # Direction by sub_op range : 0x60..0x67 = INC, 0x68..0x6F = DEC.
        # Flags : updates S/Z/V/H ; N depends on direction ; **CF preserved**
        # (per existing abs16 INC/DEC pattern, line ~1438).
        sub_op = raw[1]
        is_dec = sub_op >= 0x68
        op_name = "dec" if is_dec else "inc"
        count = sub_op & 0x07
        if count == 0:
            count = 8
        mem_byte = data_bytes[0]
        if is_dec:
            new_value = (mem_byte - count) & 0xFF
            flags_updates = dict(_compute_subtract_flags("byte", mem_byte, count))
        else:
            new_value = (mem_byte + count) & 0xFF
            flags_updates = dict(_compute_add_flags("byte", mem_byte, count))
        flags_updates.pop("cf", None)  # INC/DEC mem preserves carry.
        after_memory = dict(before_memory)
        after_memory[_mask_address(source_address)] = new_value
        return _executed_result(
            before_cpu=before_cpu,
            decoded=decoded,
            written_registers=("PC",),
            memory_writes=(
                MemoryWrite(
                    address=_mask_address(source_address),
                    data=bytes((new_value,)),
                    note=f"{op_name.upper()} #{count}, ({r32_name}) : mem byte updated.",
                ),
            ),
            after_memory=after_memory,
            new_pc=decoded.next_sequential_pc,
            reg_updates=None,
            flags_updates=flags_updates,
            note=(
                f"Executed {op_name} {count}, ({r32_name}=0x{source_address:06X}) : "
                f"mem 0x{mem_byte:02X} → 0x{new_value:02X} (CF preserved)."
            ),
        )

    if 0x78 <= raw[1] <= 0x7F:
        # Pass 56 : shift/rotate (R32) — 8-bit byte memory RMW, count=1.
        # Sub-op layout per ngdis/tlcs900_zz_mem.c :
        #   0x78 RLC   0x79 RRC   0x7A RL   0x7B RR
        #   0x7C SLA   0x7D SRA   0x7E SLL   0x7F SRL
        # Reuses the rotate/shift logic from the register-form shift family
        # (line ~4025) but operates on the byte at (R32). Carry handling :
        #   RLC : C ← MSB ; bit0 ← MSB
        #   RRC : C ← LSB ; bit7 ← LSB
        #   RL  : new C ← MSB ; bit0 ← old C
        #   RR  : new C ← LSB ; bit7 ← old C
        #   SLA/SLL : C ← MSB ; bit0 ← 0
        #   SRA : C ← LSB ; bit7 ← sign (preserved)
        #   SRL : C ← LSB ; bit7 ← 0
        # RL/RR require a known CF (rotate through carry).
        sub_op = raw[1]
        op_name = {
            0x78: "rlc", 0x79: "rrc", 0x7A: "rl",  0x7B: "rr",
            0x7C: "sla", 0x7D: "sra", 0x7E: "sll", 0x7F: "srl",
        }[sub_op]
        needs_carry = sub_op in (0x7A, 0x7B)  # RL / RR through carry
        if needs_carry and before_cpu.flags.cf is None:
            return _blocked_result(
                before_cpu=before_cpu,
                decoded=decoded,
                status="runtime-state-required",
                note=(
                    f"{op_name.upper()} ({r32_name}) is a rotate-through-carry that "
                    "requires a known C flag, which is not modeled in the current CPU state."
                ),
            )
        mem_byte = data_bytes[0]
        msb = (mem_byte >> 7) & 1
        lsb = mem_byte & 1
        sign_bit = msb
        if sub_op == 0x78:    # RLC : bit0 ← MSB
            new_value = ((mem_byte << 1) | msb) & 0xFF
            carry_out = bool(msb)
        elif sub_op == 0x79:  # RRC : bit7 ← LSB
            new_value = ((mem_byte >> 1) | (lsb << 7)) & 0xFF
            carry_out = bool(lsb)
        elif sub_op == 0x7A:  # RL through carry : bit0 ← old C
            old_c = int(before_cpu.flags.cf)
            new_value = ((mem_byte << 1) | old_c) & 0xFF
            carry_out = bool(msb)
        elif sub_op == 0x7B:  # RR through carry : bit7 ← old C
            old_c = int(before_cpu.flags.cf)
            new_value = ((mem_byte >> 1) | (old_c << 7)) & 0xFF
            carry_out = bool(lsb)
        elif sub_op in (0x7C, 0x7E):  # SLA / SLL : bit0 ← 0 (identical for byte)
            new_value = (mem_byte << 1) & 0xFF
            carry_out = bool(msb)
        elif sub_op == 0x7D:  # SRA : sign-extending
            new_value = ((mem_byte >> 1) | (sign_bit << 7)) & 0xFF
            carry_out = bool(lsb)
        else:                 # 0x7F SRL : logical right, bit7 ← 0
            new_value = (mem_byte >> 1) & 0xFF
            carry_out = bool(lsb)
        flags_updates = {
            "sf": bool(new_value >> 7),
            "zf": new_value == 0,
            # V is the PARITY of the result -- Toshiba list (8), row `* * 0 P 0 *`.
            "vf": _has_even_parity(new_value & 0xFF),
            "hf": False,  # shift/rotate clear H (Z80/TLCS-900)
            "nf": False,  # shift/rotate clear N
            "hf": False,
            "cf": carry_out,
            "nf": False,
        }
        after_memory = dict(before_memory)
        after_memory[_mask_address(source_address)] = new_value
        return _executed_result(
            before_cpu=before_cpu,
            decoded=decoded,
            written_registers=("PC",),
            memory_writes=(
                MemoryWrite(
                    address=_mask_address(source_address),
                    data=bytes((new_value,)),
                    note=f"{op_name.upper()} ({r32_name}) : mem byte updated.",
                ),
            ),
            after_memory=after_memory,
            new_pc=decoded.next_sequential_pc,
            reg_updates=None,
            flags_updates=flags_updates,
            note=(
                f"Executed {op_name} ({r32_name}=0x{source_address:06X}) : "
                f"mem 0x{mem_byte:02X} → 0x{new_value:02X}, C ← {int(carry_out)}."
            ),
        )

    if (0x90 <= raw[1] <= 0x9F) or (0xB0 <= raw[1] <= 0xBF):
        # Pass 55 : ADC/SBC R8 ↔ (R32) — carry/borrow propagation.
        # Sub-op layout (verified against NgpCraft_Disasm oracle) :
        #   0x90..0x97 = ADC R8, (R32)   — R8 ← R8 + mem + C
        #   0x98..0x9F = ADC (R32), R8   — mem ← mem + R8 + C
        #   0xB0..0xB7 = SBC R8, (R32)   — R8 ← R8 - mem - C
        #   0xB8..0xBF = SBC (R32), R8   — mem ← mem - R8 - C
        # Direction by bit 3 of op : 0=R8←, 1=mem←
        sub_op = raw[1]
        is_adc = 0x90 <= sub_op <= 0x9F
        op_name = "adc" if is_adc else "sbc"
        store_to_memory = bool(sub_op & 0x08)
        r8_index = sub_op & 0x07
        r8_name, r8_value = _extract_register_value(
            before_cpu, "byte", r8_index,
        )
        if r8_value is None:
            return _blocked_result(
                before_cpu=before_cpu,
                decoded=decoded,
                status="requires-known-source-register",
                note=(
                    f"{op_name.upper()} on {r8_name}/({r32_name}) needs "
                    f"{r8_name} value (owner = {R32[r8_index // 2]}) modeled."
                ),
            )
        if before_cpu.flags.cf is None:
            return _blocked_result(
                before_cpu=before_cpu,
                decoded=decoded,
                status="runtime-state-required",
                note=(
                    f"{op_name.upper()} on byte (R32) memory requires a known carry "
                    "flag, which is not modeled in the current CPU state."
                ),
            )
        carry = int(before_cpu.flags.cf)
        mem_byte = data_bytes[0]
        if store_to_memory:
            left, right = mem_byte, r8_value
        else:
            left, right = r8_value, mem_byte
        if is_adc:
            result = (left + right + carry) & 0xFF
            flags_updates = _compute_add_flags("byte", left, right + carry)
        else:
            result = (left - right - carry) & 0xFF
            flags_updates = _compute_subtract_flags("byte", left, right + carry)
        if store_to_memory:
            after_memory = dict(before_memory)
            after_memory[_mask_address(source_address)] = result
            return _executed_result(
                before_cpu=before_cpu,
                decoded=decoded,
                written_registers=("PC",),
                memory_writes=(
                    MemoryWrite(
                        address=_mask_address(source_address),
                        data=bytes((result,)),
                        note=(
                            f"{op_name.upper()} ({r32_name}), {r8_name} (carry={carry}) : "
                            f"mem byte updated."
                        ),
                    ),
                ),
                after_memory=after_memory,
                new_pc=decoded.next_sequential_pc,
                reg_updates=None,
                flags_updates=flags_updates,
                note=(
                    f"Executed {op_name} ({r32_name}=0x{source_address:06X})=0x{mem_byte:02X}, "
                    f"{r8_name}=0x{r8_value:02X}, C={carry} → mem=0x{result:02X}."
                ),
            )
        result_name, reg_updates = _build_register_update(
            before_cpu, "byte", r8_index, result,
        )
        if reg_updates is None:
            return _blocked_result(
                before_cpu=before_cpu,
                decoded=decoded,
                status="requires-known-full-register",
                note=(
                    f"{op_name.upper()} {r8_name}, ({r32_name}) needs the owner "
                    f"register of {r8_name} fully known to write back."
                ),
            )
        return _executed_result(
            before_cpu=before_cpu,
            decoded=decoded,
            written_registers=(result_name, "PC"),
            memory_writes=(),
            after_memory=before_memory,
            new_pc=decoded.next_sequential_pc,
            reg_updates=reg_updates,
            flags_updates=flags_updates,
            note=(
                f"Executed {op_name} {r8_name}=0x{r8_value:02X}, "
                f"({r32_name}=0x{source_address:06X})=0x{mem_byte:02X}, C={carry} → "
                f"{result_name}=0x{result:02X}."
            ),
        )

    if (0x80 <= raw[1] <= 0x8F) or (0xA0 <= raw[1] <= 0xAF):
        # Pass 54 : arithmetic ALU on byte (R32) memory operand.
        # Sub-op layout (verified against NgpCraft_Disasm oracle) :
        #   0x80..0x87 = ADD R8, (R32)  — R8 ← R8 + mem
        #   0x88..0x8F = ADD (R32), R8  — mem ← mem + R8
        #   0xA0..0xA7 = SUB R8, (R32)  — R8 ← R8 - mem
        #   0xA8..0xAF = SUB (R32), R8  — mem ← mem - R8
        # Direction by bit 3 of op : 0=R8←, 1=mem←
        sub_op = raw[1]
        is_add = 0x80 <= sub_op <= 0x8F
        op_name = "add" if is_add else "sub"
        store_to_memory = bool(sub_op & 0x08)
        r8_index = sub_op & 0x07
        r8_name, r8_value = _extract_register_value(
            before_cpu, "byte", r8_index,
        )
        if r8_value is None:
            return _blocked_result(
                before_cpu=before_cpu,
                decoded=decoded,
                status="requires-known-source-register",
                note=(
                    f"{op_name.upper()} on {r8_name}/({r32_name}) needs "
                    f"{r8_name} value (owner = {R32[r8_index // 2]}) modeled."
                ),
            )
        mem_byte = data_bytes[0]
        # ADD/SUB direction sets the "left = right op right" semantics.
        # For ADD (R8 ← R8+mem)   : flags from (R8 + mem), result = R8+mem mod 256
        # For ADD ((R32) ← mem+R8) : flags from (mem + R8), result = mem+R8 mod 256
        # For SUB (R8 ← R8-mem)   : flags from (R8 - mem)
        # For SUB ((R32) ← mem-R8) : flags from (mem - R8)
        if is_add:
            if store_to_memory:
                left, right = mem_byte, r8_value
            else:
                left, right = r8_value, mem_byte
            result = (left + right) & 0xFF
            flags_updates = _compute_add_flags("byte", left, right)
        else:  # sub
            if store_to_memory:
                left, right = mem_byte, r8_value
            else:
                left, right = r8_value, mem_byte
            result = (left - right) & 0xFF
            flags_updates = _compute_subtract_flags("byte", left, right)
        if store_to_memory:
            after_memory = dict(before_memory)
            after_memory[_mask_address(source_address)] = result
            return _executed_result(
                before_cpu=before_cpu,
                decoded=decoded,
                written_registers=("PC",),
                memory_writes=(
                    MemoryWrite(
                        address=_mask_address(source_address),
                        data=bytes((result,)),
                        note=(
                            f"{op_name.upper()} ({r32_name}), {r8_name} : "
                            f"mem byte updated."
                        ),
                    ),
                ),
                after_memory=after_memory,
                new_pc=decoded.next_sequential_pc,
                reg_updates=None,
                flags_updates=flags_updates,
                note=(
                    f"Executed {op_name} ({r32_name}={source_address:#08x}), "
                    f"{r8_name}={r8_value:#04x} : "
                    f"mem {mem_byte:#04x} → {result:#04x}."
                ),
            )
        # R8 ← result
        result_name, reg_updates = _build_register_update(
            before_cpu, "byte", r8_index, result,
        )
        if reg_updates is None:
            return _blocked_result(
                before_cpu=before_cpu,
                decoded=decoded,
                status="requires-known-full-register",
                note=(
                    f"{op_name.upper()} {r8_name}, ({r32_name}) needs the "
                    f"owner register of {r8_name} fully known to write back."
                ),
            )
        return _executed_result(
            before_cpu=before_cpu,
            decoded=decoded,
            written_registers=(result_name, "PC"),
            memory_writes=(),
            after_memory=before_memory,
            new_pc=decoded.next_sequential_pc,
            reg_updates=reg_updates,
            flags_updates=flags_updates,
            note=(
                f"Executed {op_name} {r8_name}={r8_value:#04x}, "
                f"({r32_name}={source_address:#08x})={mem_byte:#04x} → "
                f"{result_name}={result:#04x}."
            ),
        )

    if 0xC0 <= raw[1] <= 0xEF:
        # Pass 53 : logical ALU on byte (R32) memory operand.
        # Sub-op layout (verified against NgpCraft_Disasm oracle) :
        #   0xC0..0xC7 = AND R8, (R32)  — direction: R8 ← R8 & mem
        #   0xC8..0xCF = AND (R32), R8  — direction: mem ← mem & R8
        #   0xD0..0xD7 = XOR R8, (R32)
        #   0xD8..0xDF = XOR (R32), R8
        #   0xE0..0xE7 = OR  R8, (R32)
        #   0xE8..0xEF = OR  (R32), R8
        # Operation by high nibble of (op - 0xC0) >> 4 :
        #   0,1 → AND ; 2,3 → XOR ; 4,5 → OR.
        # Direction by bit 3 of op : 0=R8←mem, 1=mem←R8.
        sub_op = raw[1]
        op_idx = (sub_op - 0xC0) >> 4   # 0..2
        op_name = ("and", "xor", "or")[op_idx]
        store_to_memory = bool(sub_op & 0x08)
        r8_index = sub_op & 0x07
        r8_name, r8_value = _extract_register_value(
            before_cpu, "byte", r8_index,
        )
        if r8_value is None:
            return _blocked_result(
                before_cpu=before_cpu,
                decoded=decoded,
                status="requires-known-source-register",
                note=(
                    f"{op_name.upper()} on {r8_name}/({r32_name}) needs "
                    f"{r8_name} value (owner = {R32[r8_index // 2]}) modeled."
                ),
            )
        mem_byte = data_bytes[0]
        if op_name == "and":
            result = r8_value & mem_byte
        elif op_name == "or":
            result = r8_value | mem_byte
        else:  # xor
            result = r8_value ^ mem_byte
        # AND SETS H. Toshiba instruction list (5) "Logical operations" gives the
        # symbol row `* * 1 P 0 0` for EVERY form of AND (and `* * 0 P 0 0` for
        # OR/XOR). This handler covers AND/XOR/OR on (R32) in both directions and
        # was passing no `half_carry` at all, so AND-on-memory left H at 0.
        # Found 2026-07-11 by the C++ differential harness (native_diff.py); the
        # register-operand paths above (e.g. line 1835) already do this right.
        flags_updates = _compute_logical_flags(
            "byte", result, half_carry=(op_name == "and"),
        )
        if store_to_memory:
            # mem ← result : update writable overlay at (R32).
            after_memory = dict(before_memory)
            after_memory[_mask_address(source_address)] = result & 0xFF
            return _executed_result(
                before_cpu=before_cpu,
                decoded=decoded,
                written_registers=("PC",),
                memory_writes=(
                    MemoryWrite(
                        address=_mask_address(source_address),
                        data=bytes((result & 0xFF,)),
                        note=(
                            f"{op_name.upper()} ({r32_name}), {r8_name} : "
                            f"mem byte updated."
                        ),
                    ),
                ),
                after_memory=after_memory,
                new_pc=decoded.next_sequential_pc,
                reg_updates=None,
                flags_updates=flags_updates,
                note=(
                    f"Executed {op_name} ({r32_name}={source_address:#08x}), "
                    f"{r8_name}={r8_value:#04x} → mem={result & 0xFF:#04x}."
                ),
            )
        # else: R8 ← result.
        result_name, reg_updates = _build_register_update(
            before_cpu, "byte", r8_index, result & 0xFF,
        )
        if reg_updates is None:
            return _blocked_result(
                before_cpu=before_cpu,
                decoded=decoded,
                status="requires-known-full-register",
                note=(
                    f"{op_name.upper()} {r8_name}, ({r32_name}) needs the "
                    f"owner register of {r8_name} fully known to write back."
                ),
            )
        return _executed_result(
            before_cpu=before_cpu,
            decoded=decoded,
            written_registers=(result_name, "PC"),
            memory_writes=(),
            after_memory=before_memory,
            new_pc=decoded.next_sequential_pc,
            reg_updates=reg_updates,
            flags_updates=flags_updates,
            note=(
                f"Executed {op_name} {r8_name}={r8_value:#04x}, "
                f"({r32_name}={source_address:#08x})={mem_byte:#04x} → "
                f"{result_name}={result & 0xFF:#04x}."
            ),
        )

    dest_r8_index = raw[1] & 0x07
    dest_r8_name, reg_updates = _build_register_update(
        before_cpu, "byte", dest_r8_index, data_bytes[0]
    )
    if reg_updates is None:
        return _blocked_result(
            before_cpu=before_cpu,
            decoded=decoded,
            status="requires-known-full-register",
            note=(
                f"The owner register of {dest_r8_name} must be fully known to write the "
                "loaded byte back honestly."
            ),
        )

    return _executed_result(
        before_cpu=before_cpu,
        decoded=decoded,
        written_registers=(dest_r8_name, "PC"),
        memory_writes=(),
        after_memory=before_memory,
        new_pc=decoded.next_sequential_pc,
        reg_updates=reg_updates,
        note=(
            f"Executed register-indirect byte load from the current real execution subset. "
            f"Byte 0x{data_bytes[0]:02X} read from ({r32_name})=0x{source_address:06X} "
            f"and written to {dest_r8_name}."
        ),
    )


def _try_execute_nonrepeat_block_memory(
    view: NgpcFetchView,
    before_cpu: NgpcCpuState,
    before_memory: dict[int, int],
    decoded: DecodeResult,
) -> ExecutionResult | None:
    """Execute the currently modeled non-repeat block-memory subset."""
    raw = decoded.raw_bytes
    if raw is None or len(raw) != 2:
        return None

    first = raw[0]
    op = raw[1]
    if op not in {0x10, 0x12, 0x14, 0x16}:
        return None
    if not (0x80 <= first <= 0x87 or 0x90 <= first <= 0x97):
        return None

    size_kind = "byte" if 0x80 <= first <= 0x87 else "word"
    width = 1 if size_kind == "byte" else 2
    step = width if op in {0x10, 0x14} else -width

    bc_name, bc_value = _extract_register_value(before_cpu, "word", 1)
    if bc_value is None:
        return _blocked_result(
            before_cpu=before_cpu,
            decoded=decoded,
            status="requires-known-full-register",
            note=(
                f"{bc_name} must be known and writable before {decoded.mnemonic.upper()} can "
                "update the block counter honestly."
            ),
        )
    _, bc_updates = _build_register_update(before_cpu, "word", 1, (bc_value - 1) & 0xFFFF)
    if bc_updates is None:
        return _blocked_result(
            before_cpu=before_cpu,
            decoded=decoded,
            status="requires-known-full-register",
            note=(
                f"{bc_name} cannot be updated honestly until its owning register XBC is fully "
                "known in the current CPU state."
            ),
        )
    bc_after = (bc_value - 1) & 0xFFFF

    if op in {0x10, 0x12}:
        pair_info = {
            0x03: ("XDE", 2, "XHL", 3),
            0x05: ("XIX", 4, "XIY", 5),
        }.get(first & 0x07)
        if pair_info is None:
            return None

        destination_name, destination_index, source_name, source_index = pair_info
        _, destination_address = _extract_register_value(before_cpu, "long", destination_index)
        _, source_address = _extract_register_value(before_cpu, "long", source_index)
        if destination_address is None or source_address is None:
            missing_name = destination_name if destination_address is None else source_name
            return _blocked_result(
                before_cpu=before_cpu,
                decoded=decoded,
                status="requires-known-address-register",
                note=(
                    f"{decoded.mnemonic.upper()} needs both implicit pointer registers known, "
                    f"but {missing_name} is not modeled in the current CPU state."
                ),
            )

        # ⚠️ THE POINTER IS A 32-BIT REGISTER; THE BUS IS 24 BITS. The mask belongs on
        # the ACCESS, never on the register: an NGPC address needs only 24 of the
        # register's 32 bits, so software is free to keep something in the top byte --
        # and Pocket Tennis Color keeps its LOOP COUNTER there (pointer low, count high,
        # ended by `djnz QH`). Masking the write-back wiped that counter on every pass.
        source_register = source_address
        destination_register = destination_address
        source_address = _mask_address(source_address)
        data = _read_runtime_bytes(view, before_memory, source_address, width)
        if data is None:
            return _blocked_result(
                before_cpu=before_cpu,
                decoded=decoded,
                status="runtime-memory-unavailable",
                note=(
                    f"{decoded.mnemonic.upper()} needs {width} readable byte(s) at "
                    f"{source_name}=0x{source_address:06X}, but neither the writable runtime "
                    "overlay nor the current read bus can provide them."
                ),
            )

        destination_address = _mask_address(destination_address)
        after_memory = dict(before_memory)
        for offset, byte_value in enumerate(data):
            after_memory[_mask_address(destination_address + offset)] = byte_value

        reg_updates = dict(bc_updates)
        reg_updates[REG32_FIELDS[destination_index]] = (destination_register + step) & 0xFFFFFFFF
        reg_updates[REG32_FIELDS[source_index]] = (source_register + step) & 0xFFFFFFFF
        direction = "incremented" if step > 0 else "decremented"
        return _executed_result(
            before_cpu=before_cpu,
            decoded=decoded,
            written_registers=(bc_name, destination_name, source_name, "PC"),
            memory_writes=(
                MemoryWrite(
                    address=destination_address,
                    data=data,
                    note=(
                        f"{decoded.mnemonic.upper()} copied {width} byte(s) from {source_name} "
                        f"to {destination_name}."
                    ),
                ),
            ),
            after_memory=after_memory,
            new_pc=decoded.next_sequential_pc,
            reg_updates=reg_updates,
            flags_updates={"hf": False, "vf": bc_after != 0, "nf": False},
            cycles_consumed=BLOCK_TRANSFER_CYCLES,
            note=(
                f"Executed {decoded.mnemonic.upper()} from the current real execution subset. "
                f"{width} byte(s) were copied from {source_name}=0x{source_address:06X} to "
                f"{destination_name}=0x{destination_address:06X}; both pointers {direction} by "
                f"{width} and BC decremented to 0x{bc_after:04X}."
            ),
        )

    pointer_index = first & 0x07
    pointer_name, pointer_value = _extract_register_value(before_cpu, "long", pointer_index)
    if pointer_value is None:
        return _blocked_result(
            before_cpu=before_cpu,
            decoded=decoded,
            status="requires-known-address-register",
            note=(
                f"{pointer_name} must be known before {decoded.mnemonic.upper()} can compute "
                "its effective address honestly."
            ),
        )
    if pointer_index == 1:
        return _blocked_result(
            before_cpu=before_cpu,
            decoded=decoded,
            status="unmodeled-register-alias-side-effects",
            note=(
                f"{decoded.mnemonic.upper()} on {pointer_name} would update both {pointer_name} "
                "and BC inside XBC; the current subset does not model that alias ordering yet."
            ),
        )

    accumulator_index = 1 if size_kind == "byte" else 0
    accumulator_name, accumulator_value = _extract_register_value(
        before_cpu,
        size_kind,
        accumulator_index,
    )
    if accumulator_value is None:
        return _blocked_result(
            before_cpu=before_cpu,
            decoded=decoded,
            status="requires-known-full-register",
            note=(
                f"{accumulator_name} cannot be compared honestly until XWA is fully known in "
                "the current CPU state."
            ),
        )

    # ⚠️ 32-bit register, 24-bit bus. Mask the ACCESS, never the register: the top byte
    # is the program's, and software uses it (see the block-copy paths above).
    pointer_register = pointer_value
    pointer_value = _mask_address(pointer_value)
    data = _read_runtime_bytes(view, before_memory, pointer_value, width)
    if data is None:
        return _blocked_result(
            before_cpu=before_cpu,
            decoded=decoded,
            status="runtime-memory-unavailable",
            note=(
                f"{decoded.mnemonic.upper()} needs {width} readable byte(s) at "
                f"{pointer_name}=0x{pointer_value:06X}, but neither the writable runtime overlay "
                "nor the current read bus can provide them."
            ),
        )

    _, pointer_updates = _build_register_update(
        before_cpu,
        "long",
        pointer_index,
        (pointer_register + step) & 0xFFFFFFFF,
    )
    assert pointer_updates is not None

    flags_updates = dict(
        _compute_subtract_flags(size_kind, accumulator_value, int.from_bytes(data, "little"))
    )
    flags_updates["cf"] = before_cpu.flags.cf
    flags_updates["vf"] = bc_after != 0

    reg_updates = dict(bc_updates)
    reg_updates.update(pointer_updates)
    direction = "advanced" if step > 0 else "retreated"
    return _executed_result(
        before_cpu=before_cpu,
        decoded=decoded,
        written_registers=(bc_name, pointer_name, "PC"),
        memory_writes=(),
        after_memory=before_memory,
        new_pc=decoded.next_sequential_pc,
        reg_updates=reg_updates,
        flags_updates=flags_updates,
        cycles_consumed=BLOCK_COMPARE_CYCLES,
        note=(
            f"Executed {decoded.mnemonic.upper()} from the current real execution subset. "
            f"{accumulator_name}=0x{accumulator_value:0{width * 2}X} was compared against "
            f"memory at {pointer_name}=0x{pointer_value:06X}; {pointer_name} then {direction} "
            f"by {width} and BC decremented to 0x{bc_after:04X} while carry remained unchanged."
        ),
    )


def _try_execute_repeat_block_memory(
    view: NgpcFetchView,
    before_cpu: NgpcCpuState,
    before_memory: dict[int, int],
    decoded: DecodeResult,
) -> ExecutionResult | None:
    """Execute the currently modeled repeat block-memory subset.

    Covers the four repeat block instructions the repo decoder already
    exposes on the direct byte/word block family:

      - `LDIR` / `LDDR` (op ``0x11`` / ``0x13``) copy `BC` items from the
        implicit source pointer to the implicit destination pointer,
        post-adjusting both pointers by the operand width each iteration,
        until `BC == 0`.
      - `CPIR` / `CPDR` (op ``0x15`` / ``0x17``) compare `A`/`WA` against
        the implicit pointer target each iteration, post-adjusting the
        pointer, until a match is found (`Z == 1`) or `BC == 0`.

    The whole repeat runs atomically in this bounded model: every memory
    access needed to reach the honest stopping point must be available up
    front, otherwise the instruction blocks without mutating any state.
    (Real silicon can interrupt a block repeat mid-flight and resume it via
    `RETI`; the bounded single-step model does not sample interrupts inside
    one instruction, so it either runs the repeat to completion or blocks.)
    """
    raw = decoded.raw_bytes
    if raw is None or len(raw) != 2:
        return None

    first = raw[0]
    op = raw[1]
    if op not in {0x11, 0x13, 0x15, 0x17}:
        return None
    if not (0x80 <= first <= 0x87 or 0x90 <= first <= 0x97):
        return None

    size_kind = "byte" if 0x80 <= first <= 0x87 else "word"
    width = 1 if size_kind == "byte" else 2
    step = width if op in {0x11, 0x15} else -width

    bc_name, bc_value = _extract_register_value(before_cpu, "word", 1)
    if bc_value is None:
        return _blocked_result(
            before_cpu=before_cpu,
            decoded=decoded,
            status="requires-known-full-register",
            note=(
                f"{bc_name} must be known and writable before {decoded.mnemonic.upper()} can "
                "resolve its honest repeat count."
            ),
        )
    _, bc_zero_update = _build_register_update(before_cpu, "word", 1, 0)
    if bc_zero_update is None:
        return _blocked_result(
            before_cpu=before_cpu,
            decoded=decoded,
            status="requires-known-full-register",
            note=(
                f"{bc_name} cannot be updated honestly until its owning register XBC is fully "
                "known in the current CPU state."
            ),
        )

    # The TLCS-900 repeat forms perform the operation, decrement BC, then
    # repeat while BC != 0. A starting BC of 0 therefore wraps and runs the
    # full 0x10000 pass, matching the silicon rather than short-circuiting.
    iteration_budget = bc_value if bc_value != 0 else 0x10000

    if op in {0x11, 0x13}:
        pair_info = {
            0x03: ("XDE", 2, "XHL", 3),
            0x05: ("XIX", 4, "XIY", 5),
        }.get(first & 0x07)
        if pair_info is None:
            return None

        destination_name, destination_index, source_name, source_index = pair_info
        _, destination_address = _extract_register_value(before_cpu, "long", destination_index)
        _, source_address = _extract_register_value(before_cpu, "long", source_index)
        if destination_address is None or source_address is None:
            missing_name = destination_name if destination_address is None else source_name
            return _blocked_result(
                before_cpu=before_cpu,
                decoded=decoded,
                status="requires-known-address-register",
                note=(
                    f"{decoded.mnemonic.upper()} needs both implicit pointer registers known, "
                    f"but {missing_name} is not modeled in the current CPU state."
                ),
            )

        # ⚠️ The pointers are 32-BIT REGISTERS walked on a 24-BIT BUS. The mask goes on
        # every ACCESS below and on NOTHING ELSE: the top byte belongs to the program,
        # and Pocket Tennis Color parks its loop counter there (see the non-repeat path).
        after_memory = dict(before_memory)
        writes: list[MemoryWrite] = []
        current_source = source_address
        current_destination = destination_address
        for _iteration in range(iteration_budget):
            # Read through the progressively updated overlay so overlapping
            # source/destination ranges observe earlier writes, as on silicon.
            data = _read_runtime_bytes(
                view, after_memory, _mask_address(current_source), width
            )
            if data is None:
                return _blocked_result(
                    before_cpu=before_cpu,
                    decoded=decoded,
                    status="runtime-memory-unavailable",
                    note=(
                        f"{decoded.mnemonic.upper()} needs {width} readable byte(s) at "
                        f"{source_name}=0x{current_source:06X} to keep repeating, but neither the "
                        "writable runtime overlay nor the current read bus can provide them."
                    ),
                )
            for offset, byte_value in enumerate(data):
                after_memory[_mask_address(current_destination + offset)] = byte_value
            writes.append(
                MemoryWrite(
                    address=_mask_address(current_destination),
                    data=data,
                    note=(
                        f"{decoded.mnemonic.upper()} copied {width} byte(s) from {source_name} "
                        f"to {destination_name}."
                    ),
                )
            )
            current_source = (current_source + step) & 0xFFFFFFFF
            current_destination = (current_destination + step) & 0xFFFFFFFF

        reg_updates = dict(bc_zero_update)
        reg_updates[REG32_FIELDS[destination_index]] = current_destination
        reg_updates[REG32_FIELDS[source_index]] = current_source
        direction = "incremented" if step > 0 else "decremented"
        return _executed_result(
            before_cpu=before_cpu,
            decoded=decoded,
            written_registers=(bc_name, destination_name, source_name, "PC"),
            memory_writes=tuple(writes),
            after_memory=after_memory,
            new_pc=decoded.next_sequential_pc,
            reg_updates=reg_updates,
            flags_updates={"hf": False, "vf": False, "nf": False},
            cycles_consumed=_block_repeat_cycles(iteration_budget, compare=False),
            note=(
                f"Executed {decoded.mnemonic.upper()} from the current real execution subset. "
                f"{iteration_budget} item(s) of {width} byte(s) were copied from {source_name} to "
                f"{destination_name}; both pointers {direction} by {width} per item and BC "
                "counted down to 0x0000."
            ),
        )

    pointer_index = first & 0x07
    pointer_name, pointer_value = _extract_register_value(before_cpu, "long", pointer_index)
    if pointer_value is None:
        return _blocked_result(
            before_cpu=before_cpu,
            decoded=decoded,
            status="requires-known-address-register",
            note=(
                f"{pointer_name} must be known before {decoded.mnemonic.upper()} can compute "
                "its effective address honestly."
            ),
        )
    if pointer_index == 1:
        return _blocked_result(
            before_cpu=before_cpu,
            decoded=decoded,
            status="unmodeled-register-alias-side-effects",
            note=(
                f"{decoded.mnemonic.upper()} on {pointer_name} would update both {pointer_name} "
                "and BC inside XBC; the current subset does not model that alias ordering yet."
            ),
        )

    accumulator_index = 1 if size_kind == "byte" else 0
    accumulator_name, accumulator_value = _extract_register_value(
        before_cpu,
        size_kind,
        accumulator_index,
    )
    if accumulator_value is None:
        return _blocked_result(
            before_cpu=before_cpu,
            decoded=decoded,
            status="requires-known-full-register",
            note=(
                f"{accumulator_name} cannot be compared honestly until XWA is fully known in "
                "the current CPU state."
            ),
        )

    current_pointer = _mask_address(pointer_value)
    bc_remaining = bc_value
    iterations = 0
    matched = False
    last_flags: dict[str, bool] | None = None
    while True:
        data = _read_runtime_bytes(view, before_memory, current_pointer, width)
        if data is None:
            return _blocked_result(
                before_cpu=before_cpu,
                decoded=decoded,
                status="runtime-memory-unavailable",
                note=(
                    f"{decoded.mnemonic.upper()} needs {width} readable byte(s) at "
                    f"{pointer_name}=0x{current_pointer:06X} to keep repeating, but neither the "
                    "writable runtime overlay nor the current read bus can provide them."
                ),
            )
        memory_value = int.from_bytes(data, "little")
        last_flags = dict(_compute_subtract_flags(size_kind, accumulator_value, memory_value))
        current_pointer = _mask_address(current_pointer + step)
        bc_remaining = (bc_remaining - 1) & 0xFFFF
        iterations += 1
        if accumulator_value == memory_value:
            matched = True
            break
        if bc_remaining == 0:
            break

    assert last_flags is not None
    _, bc_update = _build_register_update(before_cpu, "word", 1, bc_remaining)
    assert bc_update is not None
    _, pointer_update = _build_register_update(before_cpu, "long", pointer_index, current_pointer)
    assert pointer_update is not None

    flags_updates = dict(last_flags)
    flags_updates["cf"] = before_cpu.flags.cf
    flags_updates["vf"] = bc_remaining != 0

    reg_updates = dict(bc_update)
    reg_updates.update(pointer_update)
    direction = "advanced" if step > 0 else "retreated"
    stop_clause = (
        f"a match at {pointer_name}=0x{_mask_address(current_pointer - step):06X}"
        if matched
        else "BC reaching 0x0000"
    )
    return _executed_result(
        before_cpu=before_cpu,
        decoded=decoded,
        written_registers=(bc_name, pointer_name, "PC"),
        memory_writes=(),
        after_memory=before_memory,
        new_pc=decoded.next_sequential_pc,
        reg_updates=reg_updates,
        flags_updates=flags_updates,
        cycles_consumed=_block_repeat_cycles(iterations, compare=True),
        note=(
            f"Executed {decoded.mnemonic.upper()} from the current real execution subset. "
            f"{accumulator_name} was compared against memory for {iterations} iteration(s) until "
            f"{stop_clause}; {pointer_name} {direction} by {width} per iteration, BC counted down "
            f"to 0x{bc_remaining:04X}, and carry remained unchanged."
        ),
    )


def _try_execute_reg_indirect_long(
    view: NgpcFetchView,
    before_cpu: NgpcCpuState,
    before_memory: dict[int, int],
    decoded: DecodeResult,
) -> ExecutionResult | None:
    """Execute long `(r32)` instructions on the 0xA0..0xA7 family.

    Currently the `LD R32, (r32)` load (op 0x20..0x27): reads 4 bytes at the
    effective address into the destination register.
    """
    raw = decoded.raw_bytes
    if raw is None or len(raw) != 2 or not (0xA0 <= raw[0] <= 0xA7):
        return None
    op = raw[1]
    if not (0x20 <= op <= 0x27):
        return None

    base_index = raw[0] & 0x07
    base_name = R32[base_index]
    base_value = getattr(before_cpu.regs, REG32_FIELDS[base_index])
    if base_value is None:
        return _blocked_result(
            before_cpu=before_cpu,
            decoded=decoded,
            status="requires-known-address-register",
            note=(
                f"{base_name} must be known before this long register-indirect load can "
                "compute its effective address honestly."
            ),
        )

    source_address = _mask_address(base_value)
    mem_bytes = _read_runtime_bytes(view, before_memory, source_address, 4)
    if mem_bytes is None:
        return _blocked_result(
            before_cpu=before_cpu,
            decoded=decoded,
            status="runtime-memory-unavailable",
            note=(
                f"This long register-indirect load needs 4 readable bytes at ({base_name})="
                f"0x{source_address:06X}, but neither the writable runtime overlay nor the "
                "current read bus can provide them."
            ),
        )

    return _execute_register_immediate(
        before_cpu=before_cpu,
        before_memory=before_memory,
        decoded=decoded,
        size_kind="long",
        register_index=op & 0x07,
        value=int.from_bytes(mem_bytes, "little"),
        note=(
            f"Executed register-indirect long load ld {R32[op & 0x07]}, ({base_name}) from the "
            "current real execution subset. Four bytes were read into the destination register."
        ),
    )


def _try_execute_reg_indirect_word(
    view: NgpcFetchView,
    before_cpu: NgpcCpuState,
    before_memory: dict[int, int],
    decoded: DecodeResult,
) -> ExecutionResult | None:
    """Execute word `(r32)` instructions on the 0x90..0x97 family."""
    raw = decoded.raw_bytes
    if raw is None or len(raw) not in (2, 4) or not (0x90 <= raw[0] <= 0x97):
        return None

    op = raw[1]
    muldiv_mode = {
        0x40: "mul",
        0x48: "muls",
        0x50: "div",
        0x58: "divs",
    }.get(op & 0xF8)
    if muldiv_mode is not None:
        base_register_index = raw[0] & 0x07
        base_register_name = R32[base_register_index]
        base_address = getattr(before_cpu.regs, REG32_FIELDS[base_register_index])
        if base_address is None:
            return _blocked_result(
                before_cpu=before_cpu,
                decoded=decoded,
                status="requires-known-address-register",
                note=(
                    f"{base_register_name} must be known before this word register-indirect {muldiv_mode} "
                    "can compute its effective address honestly."
                ),
            )

        destination_index = op & 0x07
        destination_name = R32[destination_index]
        destination_value = getattr(before_cpu.regs, REG32_FIELDS[destination_index])
        if destination_value is None:
            return _blocked_result(
                before_cpu=before_cpu,
                decoded=decoded,
                status="requires-known-full-register",
                note=(
                    f"{destination_name} must be known before this word register-indirect {muldiv_mode} "
                    "can read its operand half honestly."
                ),
            )

        source_address = _mask_address(base_address)
        mem_bytes = _read_runtime_bytes(view, before_memory, source_address, 2)
        if mem_bytes is None:
            return _blocked_result(
                before_cpu=before_cpu,
                decoded=decoded,
                status="runtime-memory-unavailable",
                note=(
                    f"This word register-indirect {muldiv_mode} needs 2 readable bytes at "
                    f"({base_register_name})=0x{source_address:06X}, but neither the writable "
                    "runtime overlay nor the current read bus can provide them."
                ),
            )

        return _execute_word_memory_muldiv_common(
            before_cpu=before_cpu,
            before_memory=before_memory,
            decoded=decoded,
            mode=muldiv_mode,
            destination_index=destination_index,
            destination_value=destination_value,
            memory_word=int.from_bytes(mem_bytes, "little"),
            operand_description=f"({base_register_name})",
        )

    r32_index = raw[0] & 0x07
    r32_name = R32[r32_index]
    r32_field = REG32_FIELDS[r32_index]
    r32_value = getattr(before_cpu.regs, r32_field)
    if r32_value is None:
        return _blocked_result(
            before_cpu=before_cpu,
            decoded=decoded,
            status="requires-known-address-register",
            note=(
                f"{r32_name} must be known before this word register-indirect form can compute "
                "its effective address honestly."
            ),
        )

    source_address = _mask_address(r32_value)
    mem_bytes = _read_runtime_bytes(view, before_memory, source_address, 2)
    if mem_bytes is None:
        return _blocked_result(
            before_cpu=before_cpu,
            decoded=decoded,
            status="runtime-memory-unavailable",
            note=(
                f"This word register-indirect form needs 2 readable bytes at ({r32_name})="
                f"0x{source_address:06X}, but neither the writable runtime overlay nor the "
                "current read bus can provide them."
            ),
        )

    if op == 0x04:
        return _execute_push_bytes(
            view=view,
            before_cpu=before_cpu,
            before_memory=before_memory,
            decoded=decoded,
            data=mem_bytes,
            note=(
                "Executed word register-indirect push from the current real execution subset. "
                "Two bytes were read from the readable runtime view and pushed onto the stack."
            ),
        )

    op = raw[1]
    mem_value = int.from_bytes(mem_bytes, "little")

    if 0x20 <= op <= 0x27:
        return _execute_register_immediate(
            before_cpu=before_cpu,
            before_memory=before_memory,
            decoded=decoded,
            size_kind="word",
            register_index=op & 0x07,
            value=mem_value,
            note=(
                "Executed register-indirect word load from the current real execution subset. "
                "Two bytes were read from the writable runtime overlay or the current read bus."
            ),
        )

    if 0x30 <= op <= 0x37:
        register_index = op & 0x07
        reg_name, reg_value = _extract_register_value(before_cpu, "word", register_index)
        if reg_value is None:
            return _blocked_result(
                before_cpu=before_cpu,
                decoded=decoded,
                status="requires-known-source-register",
                note=(
                    f"EX ({r32_name}), {reg_name} needs the value of {reg_name} modeled "
                    "before it can swap honestly."
                ),
            )
        result_name, reg_updates = _build_register_update(before_cpu, "word", register_index, mem_value)
        if reg_updates is None:
            return _blocked_result(
                before_cpu=before_cpu,
                decoded=decoded,
                status="requires-known-full-register",
                note=(
                    f"EX ({r32_name}), {reg_name} needs the owner register of {reg_name} fully "
                    "known to write back."
                ),
            )
        after_memory = dict(before_memory)
        stored = reg_value.to_bytes(2, "little")
        after_memory[source_address] = stored[0]
        after_memory[_mask_address(source_address + 1)] = stored[1]
        return _executed_result(
            before_cpu=before_cpu,
            decoded=decoded,
            written_registers=(result_name, "PC"),
            memory_writes=(
                MemoryWrite(
                    address=source_address,
                    data=stored,
                    note=f"EX ({r32_name}), {reg_name} : mem word updated.",
                ),
            ),
            after_memory=after_memory,
            new_pc=decoded.next_sequential_pc,
            reg_updates=reg_updates,
            flags_updates=None,
            note=(
                f"Executed ex ({r32_name}=0x{source_address:06X})=0x{mem_value:04X}, "
                f"{reg_name}=0x{reg_value:04X}."
            ),
        )

    if len(raw) == 4 and 0x38 <= op <= 0x3F:
        operation = {
            0x38: "add",
            0x39: "adc",
            0x3A: "sub",
            0x3B: "sbc",
            0x3C: "and",
            0x3D: "xor",
            0x3E: "or",
            0x3F: "cp",
        }[op]
        if operation in ("adc", "sbc") and before_cpu.flags.cf is None:
            return _blocked_result(
                before_cpu=before_cpu,
                decoded=decoded,
                status="runtime-state-required",
                note=(
                    f"{operation.upper()} on word register-indirect memory requires a known carry "
                    "flag, which is not modeled in the current CPU state."
                ),
            )
        carry = int(before_cpu.flags.cf) if operation in ("adc", "sbc") else 0
        imm = int.from_bytes(raw[2:4], "little")
        if operation == "add":
            result = (mem_value + imm) & 0xFFFF
            flags_updates = _compute_add_flags("word", mem_value, imm)
        elif operation == "adc":
            result = (mem_value + imm + carry) & 0xFFFF
            flags_updates = _compute_add_flags("word", mem_value, imm + carry)
        elif operation == "sub":
            result = (mem_value - imm) & 0xFFFF
            flags_updates = _compute_subtract_flags("word", mem_value, imm)
        elif operation == "sbc":
            result = (mem_value - imm - carry) & 0xFFFF
            flags_updates = _compute_subtract_flags("word", mem_value, imm + carry)
        elif operation == "and":
            result = mem_value & imm
            flags_updates = _compute_logical_flags("word", result, half_carry=True)
        elif operation == "xor":
            result = mem_value ^ imm
            flags_updates = _compute_logical_flags("word", result)
        elif operation == "or":
            result = mem_value | imm
            flags_updates = _compute_logical_flags("word", result)
        else:
            result = (mem_value - imm) & 0xFFFF
            flags_updates = _compute_subtract_flags("word", mem_value, imm)

        if operation == "cp":
            return _executed_result(
                before_cpu=before_cpu,
                decoded=decoded,
                written_registers=("PC",),
                memory_writes=(),
                after_memory=before_memory,
                new_pc=decoded.next_sequential_pc,
                reg_updates=None,
                flags_updates=flags_updates,
                note=(
                    "Executed word register-indirect compare-immediate from the current real "
                    "execution subset."
                ),
            )

        result_bytes = result.to_bytes(2, "little")
        write_status, write_note = _check_writable_range(view, source_address, 2)
        if write_status == "write-discarded":
            return _executed_result(
                before_cpu=before_cpu,
                decoded=decoded,
                written_registers=("PC",),
                memory_writes=(
                    MemoryWrite(address=source_address, data=result_bytes, note=f"[DISCARDED] {write_note}"),
                ),
                after_memory=before_memory,
                new_pc=decoded.next_sequential_pc,
                reg_updates=None,
                flags_updates=flags_updates,
                note="Word register-indirect immediate ALU write was discarded.",
            )
        if write_status is not None:
            return _blocked_result(
                before_cpu=before_cpu,
                decoded=decoded,
                status=write_status,
                note=write_note,
            )
        after_memory = dict(before_memory)
        after_memory[source_address] = result_bytes[0]
        after_memory[_mask_address(source_address + 1)] = result_bytes[1]
        return _executed_result(
            before_cpu=before_cpu,
            decoded=decoded,
            written_registers=("PC",),
            memory_writes=(
                MemoryWrite(
                    address=source_address,
                    data=result_bytes,
                    note=f"Word register-indirect {operation.upper()} immediate updated memory.",
                ),
            ),
            after_memory=after_memory,
            new_pc=decoded.next_sequential_pc,
            reg_updates=None,
            flags_updates=flags_updates,
            note=f"Executed {operation} ({r32_name}), imm16.",
        )

    if 0x60 <= op <= 0x6F:
        count = op & 0x07
        if count == 0:
            count = 8
        is_dec = op >= 0x68
        if is_dec:
            result = (mem_value - count) & 0xFFFF
            flags_updates = dict(_compute_subtract_flags("word", mem_value, count))
        else:
            result = (mem_value + count) & 0xFFFF
            flags_updates = dict(_compute_add_flags("word", mem_value, count))
        flags_updates.pop("cf", None)
        result_bytes = result.to_bytes(2, "little")
        write_status, write_note = _check_writable_range(view, source_address, 2)
        if write_status == "write-discarded":
            return _executed_result(
                before_cpu=before_cpu,
                decoded=decoded,
                written_registers=("PC",),
                memory_writes=(MemoryWrite(address=source_address, data=result_bytes, note=f"[DISCARDED] {write_note}"),),
                after_memory=before_memory,
                new_pc=decoded.next_sequential_pc,
                reg_updates=None,
                flags_updates=flags_updates,
                note="Word register-indirect INC/DEC write was discarded.",
            )
        if write_status is not None:
            return _blocked_result(before_cpu=before_cpu, decoded=decoded, status=write_status, note=write_note)
        after_memory = dict(before_memory)
        after_memory[source_address] = result_bytes[0]
        after_memory[_mask_address(source_address + 1)] = result_bytes[1]
        return _executed_result(
            before_cpu=before_cpu,
            decoded=decoded,
            written_registers=("PC",),
            memory_writes=(MemoryWrite(address=source_address, data=result_bytes, note="Word register-indirect INC/DEC updated memory."),),
            after_memory=after_memory,
            new_pc=decoded.next_sequential_pc,
            reg_updates=None,
            flags_updates=flags_updates,
            note=f"Executed {'dec' if is_dec else 'inc'} {count}, ({r32_name}).",
        )

    if 0x80 <= op <= 0xFF:
        operation = {
            0x8: "add",
            0x9: "adc",
            0xA: "sub",
            0xB: "sbc",
            0xC: "and",
            0xD: "xor",
            0xE: "or",
            0xF: "cp",
        }.get(op >> 4)
        if operation is None:
            return None
        register_index = op & 0x07
        register_name, register_value = _extract_register_value(before_cpu, "word", register_index)
        if register_value is None:
            return _blocked_result(
                before_cpu=before_cpu,
                decoded=decoded,
                status="requires-known-source-register",
                note=(
                    f"{operation.upper()} on {register_name}/({r32_name}) needs {register_name} "
                    "modeled in the current CPU state."
                ),
            )
        if operation in ("adc", "sbc") and before_cpu.flags.cf is None:
            return _blocked_result(
                before_cpu=before_cpu,
                decoded=decoded,
                status="runtime-state-required",
                note=(
                    f"{operation.upper()} on word register-indirect memory requires a known carry flag, "
                    "which is not modeled in the current CPU state."
                ),
            )
        carry = int(before_cpu.flags.cf) if operation in ("adc", "sbc") else 0
        store_to_memory = bool(op & 0x08)
        register_is_left = not store_to_memory
        left_value = register_value if register_is_left else mem_value
        right_value = mem_value if register_is_left else register_value

        if operation == "add":
            result = (left_value + right_value) & 0xFFFF
            flags_updates = _compute_add_flags("word", left_value, right_value)
        elif operation == "adc":
            result = (left_value + right_value + carry) & 0xFFFF
            flags_updates = _compute_add_flags("word", left_value, right_value + carry)
        elif operation == "sub":
            result = (left_value - right_value) & 0xFFFF
            flags_updates = _compute_subtract_flags("word", left_value, right_value)
        elif operation == "sbc":
            result = (left_value - right_value - carry) & 0xFFFF
            flags_updates = _compute_subtract_flags("word", left_value, right_value + carry)
        elif operation == "and":
            result = left_value & right_value
            flags_updates = _compute_logical_flags("word", result, half_carry=True)
        elif operation == "xor":
            result = left_value ^ right_value
            flags_updates = _compute_logical_flags("word", result)
        elif operation == "or":
            result = left_value | right_value
            flags_updates = _compute_logical_flags("word", result)
        else:
            result = (left_value - right_value) & 0xFFFF
            flags_updates = _compute_subtract_flags("word", left_value, right_value)

        if operation == "cp":
            return _executed_result(
                before_cpu=before_cpu,
                decoded=decoded,
                written_registers=("PC",),
                memory_writes=(),
                after_memory=before_memory,
                new_pc=decoded.next_sequential_pc,
                reg_updates=None,
                flags_updates=flags_updates,
                note=f"Executed {operation} on word register-indirect memory.",
            )

        if store_to_memory:
            result_bytes = result.to_bytes(2, "little")
            write_status, write_note = _check_writable_range(view, source_address, 2)
            if write_status == "write-discarded":
                return _executed_result(
                    before_cpu=before_cpu,
                    decoded=decoded,
                    written_registers=("PC",),
                    memory_writes=(MemoryWrite(address=source_address, data=result_bytes, note=f"[DISCARDED] {write_note}"),),
                    after_memory=before_memory,
                    new_pc=decoded.next_sequential_pc,
                    reg_updates=None,
                    flags_updates=flags_updates,
                    note="Word register-indirect ALU write was discarded.",
                )
            if write_status is not None:
                return _blocked_result(before_cpu=before_cpu, decoded=decoded, status=write_status, note=write_note)
            after_memory = dict(before_memory)
            after_memory[source_address] = result_bytes[0]
            after_memory[_mask_address(source_address + 1)] = result_bytes[1]
            return _executed_result(
                before_cpu=before_cpu,
                decoded=decoded,
                written_registers=("PC",),
                memory_writes=(MemoryWrite(address=source_address, data=result_bytes, note=f"Word register-indirect {operation.upper()} updated memory."),),
                after_memory=after_memory,
                new_pc=decoded.next_sequential_pc,
                reg_updates=None,
                flags_updates=flags_updates,
                note=f"Executed {operation} ({r32_name}), {register_name}.",
            )

        result_name, reg_updates = _build_register_update(before_cpu, "word", register_index, result)
        if reg_updates is None:
            return _blocked_result(
                before_cpu=before_cpu,
                decoded=decoded,
                status="requires-known-full-register",
                note=(
                    f"{operation.upper()} {register_name}, ({r32_name}) needs the owner register "
                    f"of {register_name} fully known to write back."
                ),
            )
        return _executed_result(
            before_cpu=before_cpu,
            decoded=decoded,
            written_registers=(result_name, "PC"),
            memory_writes=(),
            after_memory=before_memory,
            new_pc=decoded.next_sequential_pc,
            reg_updates=reg_updates,
            flags_updates=flags_updates,
            note=f"Executed {operation} {register_name}, ({r32_name}).",
        )

    return None


def _try_execute_reg_indirect_store(
    view: NgpcFetchView,
    before_cpu: NgpcCpuState,
    before_memory: dict[int, int],
    decoded: DecodeResult,
) -> ExecutionResult | None:
    """Execute (r32) register-indirect stores.

    Encoding: B0..B7 = prefix (address r32 = byte & 0x07), then op byte:
      40..47 xx    => ld  (r32), R8   — 2 bytes
      50..57       => ldw (r32), R16  — 2 bytes
      60..67       => ld  (r32), R32  — 2 bytes
      00 xx        => ld  (r32), imm8 — 3 bytes
      02 xx xx     => ldw (r32), imm16 — 4 bytes
    """
    raw = decoded.raw_bytes
    if raw is None or not (0xB0 <= raw[0] <= 0xB7):
        return None

    register_index = raw[0] & 0x07
    addr_r32_name = R32[register_index]
    addr_r32_field = REG32_FIELDS[register_index]
    addr_value = getattr(before_cpu.regs, addr_r32_field)
    if addr_value is None:
        return _blocked_result(
            before_cpu=before_cpu,
            decoded=decoded,
            status="requires-known-address-register",
            note=(
                f"{addr_r32_name} must be known to resolve the target address for this "
                "register-indirect store."
            ),
        )

    target_address = _mask_address(addr_value)

    if len(raw) == 2 and 0xB0 <= raw[1] <= 0xCF:
        # bit/res/set/chg #n, (r32) — B0+mem : C8/B0/B8/C0 + #3 (byte on memory).
        # `B1 C8` = `bit 0, (XBC)` (platformer_3 / mr_robot frontier).
        op = raw[1]
        n = op & 0x07
        data = _read_runtime_bytes(view, before_memory, target_address, 1)
        if data is None:
            return _blocked_result(
                before_cpu=before_cpu,
                decoded=decoded,
                status="runtime-memory-unavailable",
                note=(
                    f"This register-indirect bit op needs 1 readable byte at "
                    f"({addr_r32_name})=0x{target_address:06X}, but neither the writable "
                    "runtime overlay nor the current read bus can provide it."
                ),
            )
        mem_byte = data[0]
        bit_set = bool(mem_byte & (1 << n))
        if 0xC8 <= op <= 0xCF:
            # bit — read-only test: Z = NOT bit, H = 1, N = 0, CF/SF untouched.
            return _executed_result(
                before_cpu=before_cpu,
                decoded=decoded,
                written_registers=("PC",),
                memory_writes=(),
                after_memory=before_memory,
                new_pc=decoded.next_sequential_pc,
                reg_updates=None,
                flags_updates={"zf": not bit_set, "hf": True, "nf": False},
                note=(
                    f"Executed bit {n}, ({addr_r32_name}): mem8[0x{target_address:06X}]="
                    f"0x{mem_byte:02X}, bit {n} -> Z={int(not bit_set)}."
                ),
            )
        if 0xB0 <= op <= 0xB7:
            new_byte = mem_byte & (~(1 << n) & 0xFF)
            operation = "res"
        elif 0xB8 <= op <= 0xBF:
            new_byte = (mem_byte | (1 << n)) & 0xFF
            operation = "set"
        else:  # 0xC0..0xC7
            new_byte = (mem_byte ^ (1 << n)) & 0xFF
            operation = "chg"
        write_status, write_note = _check_writable_range(view, target_address, 1)
        if write_status == "write-discarded":
            return _executed_result(
                before_cpu=before_cpu,
                decoded=decoded,
                written_registers=("PC",),
                memory_writes=(
                    MemoryWrite(address=target_address, data=bytes([new_byte]), note=f"[DISCARDED] {write_note}"),
                ),
                after_memory=before_memory,
                new_pc=decoded.next_sequential_pc,
                reg_updates=None,
                note=(
                    f"Register-indirect {operation} {n}, ({addr_r32_name}) destination was "
                    "unmapped or read-only; write silently discarded (open bus)."
                ),
            )
        if write_status is not None:
            return _blocked_result(
                before_cpu=before_cpu, decoded=decoded, status=write_status, note=write_note,
            )
        after_memory = dict(before_memory)
        after_memory[target_address] = new_byte
        return _executed_result(
            before_cpu=before_cpu,
            decoded=decoded,
            written_registers=("PC",),
            memory_writes=(
                MemoryWrite(
                    address=target_address,
                    data=bytes([new_byte]),
                    note=f"Writable runtime overlay updated by register-indirect {operation.upper()}.",
                ),
            ),
            after_memory=after_memory,
            new_pc=decoded.next_sequential_pc,
            reg_updates=None,
            note=(
                f"Executed {operation} {n}, ({addr_r32_name}): mem8[0x{target_address:06X}] "
                f"0x{mem_byte:02X} -> 0x{new_byte:02X}."
            ),
        )

    if len(raw) == 2 and 0xD0 <= raw[1] <= 0xDF:
        # jp [cc,] (r32): jump to the address held in the r32 register.
        # cc=8 (op 0xD8) is unconditional (e.g. B3 D8 = JP (XHL),
        # B4 D8 = JP (XIX), used by the GB2T900 HAL register-return).
        cc_idx = raw[1] & 0x0F
        taken = True
        if cc_idx != 8:
            condition_result = _evaluate_condition_code(cc_idx, before_cpu.flags)
            if condition_result is None:
                return _blocked_result(
                    before_cpu=before_cpu,
                    decoded=decoded,
                    status="requires-known-flags",
                    note=(
                        f"jp {CC[cc_idx]}, ({addr_r32_name}): the condition flag(s) are not "
                        "known in the current CPU state."
                    ),
                )
            taken = condition_result
        new_pc = target_address if taken else decoded.next_sequential_pc
        return _executed_result(
            before_cpu=before_cpu,
            decoded=decoded,
            written_registers=("PC",),
            memory_writes=(),
            after_memory=before_memory,
            new_pc=new_pc,
            reg_updates=None,
            note=(
                f"Executed jp ({addr_r32_name}) -> 0x{new_pc:06X} "
                f"({addr_r32_name}=0x{addr_value:08X})."
            ),
        )

    if len(raw) == 2 and 0x30 <= raw[1] <= 0x37:
        # lda Rdst, (Rbase): Rdst = current Rbase value (effective address).
        # No memory access, no flag update.
        dest_index = raw[1] & 0x07
        dest_name = R32[dest_index]
        dest_field = REG32_FIELDS[dest_index]
        return _executed_result(
            before_cpu=before_cpu,
            decoded=decoded,
            written_registers=(dest_name, "PC"),
            memory_writes=(),
            after_memory=before_memory,
            new_pc=decoded.next_sequential_pc,
            reg_updates={dest_field: addr_value & 0xFFFFFFFF},
            note=(
                f"Executed lda {dest_name}, ({addr_r32_name}): "
                f"{dest_name}={addr_r32_name}=0x{addr_value:08X}."
            ),
        )

    if len(raw) == 2 and 0x40 <= raw[1] <= 0x47:
        src_name, src_value = _extract_register_value(before_cpu, "byte", raw[1] & 0x07)
        if src_value is None:
            owner = R32[(raw[1] & 0x07) // 2]
            return _blocked_result(
                before_cpu=before_cpu,
                decoded=decoded,
                status="requires-known-full-register",
                note=(
                    f"{src_name} cannot be stored honestly until {owner} is already known "
                    "in the current CPU state."
                ),
            )
        return _execute_absolute_store(
            view=view,
            before_cpu=before_cpu,
            before_memory=before_memory,
            decoded=decoded,
            target_address=target_address,
            data=bytes((src_value & 0xFF,)),
            note=(
                f"Executed register-indirect byte store ld ({addr_r32_name}), {src_name}. "
                "The source byte was written to the writable runtime overlay."
            ),
            memory_note="Writable runtime overlay updated by register-indirect byte store.",
        )

    if len(raw) == 2 and 0x50 <= raw[1] <= 0x57:
        src_name, src_value = _extract_register_value(before_cpu, "word", raw[1] & 0x07)
        if src_value is None:
            owner = R32[raw[1] & 0x07]
            return _blocked_result(
                before_cpu=before_cpu,
                decoded=decoded,
                status="requires-known-full-register",
                note=(
                    f"{src_name} cannot be stored honestly until {owner} is already known "
                    "in the current CPU state."
                ),
            )
        return _execute_absolute_store(
            view=view,
            before_cpu=before_cpu,
            before_memory=before_memory,
            decoded=decoded,
            target_address=target_address,
            data=(src_value & 0xFFFF).to_bytes(2, "little"),
            note=(
                f"Executed register-indirect word store ldw ({addr_r32_name}), {src_name}. "
                "The source word was written to the writable runtime overlay (little-endian)."
            ),
            memory_note="Writable runtime overlay updated by register-indirect word store.",
        )

    if len(raw) == 2 and 0x60 <= raw[1] <= 0x67:
        src_name, src_value = _extract_register_value(before_cpu, "long", raw[1] & 0x07)
        if src_value is None:
            return _blocked_result(
                before_cpu=before_cpu,
                decoded=decoded,
                status="requires-known-full-register",
                note=(
                    f"{src_name} cannot be stored honestly until its current full value is "
                    "known in the CPU state."
                ),
            )
        return _execute_absolute_store(
            view=view,
            before_cpu=before_cpu,
            before_memory=before_memory,
            decoded=decoded,
            target_address=target_address,
            data=src_value.to_bytes(4, "little"),
            note=(
                f"Executed register-indirect long store ld ({addr_r32_name}), {src_name}. "
                "The 32-bit source value was written to the writable runtime overlay."
            ),
            memory_note="Writable runtime overlay updated by register-indirect long store.",
        )

    if len(raw) == 3 and raw[1] == 0x00:
        imm8 = raw[2]
        return _execute_absolute_store(
            view=view,
            before_cpu=before_cpu,
            before_memory=before_memory,
            decoded=decoded,
            target_address=target_address,
            data=bytes((imm8,)),
            note=(
                "Executed register-indirect byte store (ld (r32), imm8). "
                "The immediate byte was written to the writable runtime overlay."
            ),
            memory_note="Writable runtime overlay updated by register-indirect byte store.",
        )

    if len(raw) == 4 and raw[1] == 0x02:
        imm16 = int.from_bytes(raw[2:4], "little")
        return _execute_absolute_store(
            view=view,
            before_cpu=before_cpu,
            before_memory=before_memory,
            decoded=decoded,
            target_address=target_address,
            data=imm16.to_bytes(2, "little"),
            note=(
                "Executed register-indirect word store (ldw (r32), imm16). "
                "The immediate word was written to the writable runtime overlay (little-endian)."
            ),
            memory_note="Writable runtime overlay updated by register-indirect word store.",
        )

    return None


def _try_execute_prefixed_register_ld(
    before_cpu: NgpcCpuState,
    before_memory: dict[int, int],
    decoded: DecodeResult,
) -> ExecutionResult | None:
    raw = decoded.raw_bytes
    if raw is None or len(raw) != 2:
        return None

    info = _prefixed_register_execute_info(raw[0])
    if info is None:
        return None

    size_kind, first_register_index = info
    second = raw[1]
    if 0x88 <= second <= 0x8F:
        destination_index = second & 0x07
        source_index = first_register_index
    elif 0x98 <= second <= 0x9F:
        destination_index = first_register_index
        source_index = second & 0x07
    else:
        return None

    source_register_name, source_value = _extract_register_value(
        before_cpu=before_cpu,
        size_kind=size_kind,
        register_index=source_index,
    )
    if source_value is None:
        owner_name = R32[source_index // 2] if size_kind == "byte" else R32[source_index]
        return _blocked_result(
            before_cpu=before_cpu,
            decoded=decoded,
            status="requires-known-full-register",
            note=(
                f"{source_register_name} cannot be copied honestly until {owner_name} is already "
                "known in the current CPU state."
            ),
        )

    destination_register_name, reg_updates = _build_register_update(
        before_cpu=before_cpu,
        size_kind=size_kind,
        register_index=destination_index,
        value=source_value,
    )
    if reg_updates is None:
        owner_name = R32[destination_index // 2] if size_kind == "byte" else R32[destination_index]
        return _blocked_result(
            before_cpu=before_cpu,
            decoded=decoded,
            status="requires-known-full-register",
            note=(
                f"{destination_register_name} cannot be updated honestly until {owner_name} is "
                "already known in the current CPU state."
            ),
        )

    return _executed_result(
        before_cpu=before_cpu,
        decoded=decoded,
        written_registers=(destination_register_name, "PC"),
        memory_writes=(),
        after_memory=before_memory,
        new_pc=decoded.next_sequential_pc,
        reg_updates=reg_updates,
        note=(
            "Executed prefixed register-to-register load from the current real execution "
            "subset. The destination register view now mirrors the known source register value."
        ),
    )


def _try_execute_prefixed_compare(
    before_cpu: NgpcCpuState,
    before_memory: dict[int, int],
    decoded: DecodeResult,
) -> ExecutionResult | None:
    raw = decoded.raw_bytes
    if raw is None or len(raw) != 2:
        return None

    info = _prefixed_register_execute_info(raw[0])
    if info is None:
        return None

    size_kind, right_index = info
    second = raw[1]
    if not (0xF0 <= second <= 0xF7):
        return None

    left_index = second & 0x07
    left_name, left_value = _extract_register_value(
        before_cpu=before_cpu,
        size_kind=size_kind,
        register_index=left_index,
    )
    if left_value is None:
        owner_name = R32[left_index // 2] if size_kind == "byte" else R32[left_index]
        return _blocked_result(
            before_cpu=before_cpu,
            decoded=decoded,
            status="requires-known-full-register",
            note=(
                f"{left_name} cannot be compared honestly until {owner_name} is already known "
                "in the current CPU state."
            ),
        )

    right_name, right_value = _extract_register_value(
        before_cpu=before_cpu,
        size_kind=size_kind,
        register_index=right_index,
    )
    if right_value is None:
        owner_name = R32[right_index // 2] if size_kind == "byte" else R32[right_index]
        return _blocked_result(
            before_cpu=before_cpu,
            decoded=decoded,
            status="requires-known-full-register",
            note=(
                f"{right_name} cannot be compared honestly until {owner_name} is already known "
                "in the current CPU state."
            ),
        )

    flags_updates = _compute_subtract_flags(size_kind, left_value, right_value)
    return _executed_result(
        before_cpu=before_cpu,
        decoded=decoded,
        written_registers=("PC",),
        memory_writes=(),
        after_memory=before_memory,
        new_pc=decoded.next_sequential_pc,
        reg_updates=None,
        flags_updates=flags_updates,
        note=(
            "Executed prefixed compare from the current real execution subset. No register "
            "value changed, but the modeled flag subset now reflects the subtraction-style "
            "compare result."
        ),
    )


def _try_execute_prefixed_push_pop(
    view: NgpcFetchView,
    before_cpu: NgpcCpuState,
    before_memory: dict[int, int],
    decoded: DecodeResult,
) -> ExecutionResult | None:
    """Execute the safe prefixed register PUSH/POP subset.

    The byte family (`C8..CF`) is safe and fully modeled. The long families
    (`D8..DF`, `E8..EF`) also admit the sub-op `0x04/0x05` through the local
    quirk tables, so once the target register is known we can reuse the
    existing writable-stack helpers directly. Word-family `D0..D7` forms are
    still filtered upstream by the silicon-broken matcher.
    """
    raw = decoded.raw_bytes
    if raw is None or len(raw) != 2 or raw[1] not in (0x04, 0x05):
        return None

    info = _prefixed_register_execute_info(raw[0])
    if info is None:
        return None

    size_kind, register_index = info
    reg_name, reg_value = _extract_register_value(before_cpu, size_kind, register_index)

    if raw[1] == 0x04:
        if reg_value is None:
            owner_name = R32[register_index] if size_kind != "byte" else R32[register_index // 2]
            return _blocked_result(
                before_cpu=before_cpu,
                decoded=decoded,
                status="requires-known-full-register",
                note=(
                    f"{reg_name} cannot be pushed honestly until its owning register {owner_name} "
                    "is known in the current CPU state."
                ),
            )

        width = {"byte": 1, "word": 2, "long": 4}[size_kind]
        return _execute_push_bytes(
            view=view,
            before_cpu=before_cpu,
            before_memory=before_memory,
            decoded=decoded,
            data=reg_value.to_bytes(width, "little"),
            note=(
                "Executed prefixed PUSH from the current real execution subset. The selected "
                "register value was written to the writable stack model."
            ),
        )

    return _execute_pop_register(
        view=view,
        before_cpu=before_cpu,
        before_memory=before_memory,
        decoded=decoded,
        size_kind=size_kind,
        register_index=register_index,
    )


def _try_execute_stack_or_call(
    view: NgpcFetchView,
    before_cpu: NgpcCpuState,
    before_memory: dict[int, int],
    decoded: DecodeResult,
) -> ExecutionResult | None:
    raw = decoded.raw_bytes
    if raw is None:
        return None

    first = raw[0]

    if first == 0x14 and len(raw) == 1:
        register_name, value = _extract_register_value(before_cpu, "byte", 1)
        if value is None:
            return _blocked_result(
                before_cpu=before_cpu,
                decoded=decoded,
                status="requires-known-full-register",
                note=(
                    f"{register_name} cannot be pushed honestly until its owning 32-bit register "
                    "is known in the current CPU state."
                ),
            )
        return _execute_push_bytes(
            view=view,
            before_cpu=before_cpu,
            before_memory=before_memory,
            decoded=decoded,
            data=bytes((value,)),
            note=(
                "Executed PUSH A from the current real execution subset. The current A byte was "
                "written to the writable stack model."
            ),
            cycles_consumed=PUSH_A_CYCLES,
        )

    if first == 0x15 and len(raw) == 1:
        return _execute_pop_register(
            view=view,
            before_cpu=before_cpu,
            before_memory=before_memory,
            decoded=decoded,
            size_kind="byte",
            register_index=1,
            cycles_consumed=POP_A_CYCLES,
        )

    if first == 0x18 and len(raw) == 1:
        f_value = encode_f_from_flags(before_cpu.flags)
        if f_value is None:
            return _blocked_result(
                before_cpu=before_cpu,
                decoded=decoded,
                status="requires-known-flags",
                note=(
                    "PUSH F needs the full modeled flag byte (S/Z/V/H/N/C). At least one flag "
                    "is still unknown in the current CPU state."
                ),
            )
        return _execute_push_bytes(
            view=view,
            before_cpu=before_cpu,
            before_memory=before_memory,
            decoded=decoded,
            data=bytes((f_value,)),
            note=(
                f"Executed PUSH F: 8-bit F=0x{f_value:02X} written to the writable stack model."
            ),
            cycles_consumed=PUSH_F_CYCLES,
        )

    if first == 0x19 and len(raw) == 1:
        xsp = before_cpu.regs.xsp
        if xsp is None:
            return _blocked_result(
                before_cpu=before_cpu,
                decoded=decoded,
                status="requires-known-stack-pointer",
                note=(
                    "POP F needs XSP, but the current bootstrap CPU state still leaves the stack "
                    "pointer unknown."
                ),
            )
        data = _read_runtime_bytes(view, before_memory, _mask_address(xsp), 1)
        if data is None:
            return _blocked_result(
                before_cpu=before_cpu,
                decoded=decoded,
                status="stack-data-unavailable",
                note=(
                    "POP F needs 1 readable byte at the current XSP, but the current writable "
                    "stack model and read bus do not provide it."
                ),
            )
        f_value = int(data[0]) & 0xFF
        new_xsp = (xsp + 1) & 0xFFFFFFFF
        new_flags = decode_f_to_flags(f_value)

        modeled_fields = before_cpu.modeled_fields
        if "executed-subset" not in modeled_fields:
            modeled_fields = (*modeled_fields, "executed-subset")
        if "modeled-flags-subset" not in modeled_fields:
            modeled_fields = (*modeled_fields, "modeled-flags-subset")

        after_cpu = replace(
            before_cpu,
            pc=decoded.next_sequential_pc,
            regs=replace(before_cpu.regs, xsp=new_xsp),
            flags=new_flags,
            modeled_fields=modeled_fields,
            note=(
                f"{before_cpu.note} Executed POP F: 8-bit F=0x{f_value:02X} loaded from the "
                "writable stack model; the modeled flag subset now comes from that byte."
            ),
        )
        return ExecutionResult(
            before_cpu=before_cpu,
            after_cpu=after_cpu,
            decode=decoded,
            status="executed",
            written_registers=("F", "XSP", "PC"),
            memory_writes=(),
            after_memory=before_memory,
            note=(
                f"Executed POP F: 8-bit F=0x{f_value:02X} loaded from the writable stack model; "
                f"XSP advanced to 0x{new_xsp:08X}."
            ),
            cycles_consumed=POP_F_CYCLES,
        )

    if first == 0x0B and len(raw) == 3:
        imm16 = raw[1:3]
        return _execute_push_bytes(
            view=view,
            before_cpu=before_cpu,
            before_memory=before_memory,
            decoded=decoded,
            data=imm16,
            note=(
                "Executed PUSHW immediate from the current real execution subset. The immediate "
                "word was written to the current writable stack model."
            ),
            cycles_consumed=PUSHW_IMM16_CYCLES,
        )

    if 0x28 <= first <= 0x2F and len(raw) == 1:
        register_index = first & 0x07
        if register_index == 7:
            return _blocked_result(
                before_cpu=before_cpu,
                decoded=decoded,
                status="unmodeled-stack-pointer-alias",
                note=(
                    "PUSH SP is not implemented yet because the current subset does not model "
                    "the self-referential stack-pointer alias semantics carefully enough."
                ),
            )
        register_name, value = _extract_register_value(before_cpu, "word", register_index)
        if value is None:
            return _blocked_result(
                before_cpu=before_cpu,
                decoded=decoded,
                status="requires-known-full-register",
                note=(
                    f"{register_name} cannot be pushed honestly until its owning 32-bit register "
                    "is known in the current CPU state."
                ),
            )
        return _execute_push_bytes(
            view=view,
            before_cpu=before_cpu,
            before_memory=before_memory,
            decoded=decoded,
            data=value.to_bytes(2, "little"),
            note=(
                "Executed PUSH R16 from the current real execution subset. The current register "
                "value was written to the writable stack model."
            ),
            cycles_consumed=PUSH_R16_CYCLES,
        )

    if 0x38 <= first <= 0x3F and len(raw) == 1:
        register_index = first & 0x07
        if register_index == 7:
            return _blocked_result(
                before_cpu=before_cpu,
                decoded=decoded,
                status="unmodeled-stack-pointer-alias",
                note=(
                    "PUSH XSP is not implemented yet because the current subset does not model "
                    "the self-referential stack-pointer semantics carefully enough."
                ),
            )
        register_name, value = _extract_register_value(before_cpu, "long", register_index)
        if value is None:
            return _blocked_result(
                before_cpu=before_cpu,
                decoded=decoded,
                status="requires-known-full-register",
                note=(
                    f"{register_name} cannot be pushed honestly until its current full value is "
                    "known in the CPU state."
                ),
            )
        return _execute_push_bytes(
            view=view,
            before_cpu=before_cpu,
            before_memory=before_memory,
            decoded=decoded,
            data=value.to_bytes(4, "little"),
            note=(
                "Executed PUSH R32 from the current real execution subset. The current register "
                "value was written to the writable stack model."
            ),
            cycles_consumed=PUSH_R32_CYCLES,
        )

    if 0x48 <= first <= 0x4F and len(raw) == 1:
        register_index = first & 0x07
        if register_index == 7:
            return _blocked_result(
                before_cpu=before_cpu,
                decoded=decoded,
                status="unmodeled-stack-pointer-alias",
                note=(
                    "POP SP is not implemented yet because the current subset does not model "
                    "that stack-pointer alias case carefully enough."
                ),
            )
        return _execute_pop_register(
            view=view,
            before_cpu=before_cpu,
            before_memory=before_memory,
            decoded=decoded,
            size_kind="word",
            register_index=register_index,
            cycles_consumed=POP_R16_CYCLES,
        )

    if 0x58 <= first <= 0x5F and len(raw) == 1:
        register_index = first & 0x07
        if register_index == 7:
            return _blocked_result(
                before_cpu=before_cpu,
                decoded=decoded,
                status="unmodeled-stack-pointer-alias",
                note=(
                    "POP XSP is not implemented yet because the current subset does not model "
                    "that stack-pointer alias case carefully enough."
                ),
            )
        return _execute_pop_register(
            view=view,
            before_cpu=before_cpu,
            before_memory=before_memory,
            decoded=decoded,
            size_kind="long",
            register_index=register_index,
            cycles_consumed=POP_R32_CYCLES,
        )

    if first in (0x1C, 0x1D, 0x1E) and decoded.direct_target is not None:
        if decoded.next_sequential_pc is None:
            return _blocked_result(
                before_cpu=before_cpu,
                decoded=decoded,
                status="unsupported-decoded-instruction",
                note="CALL-like instruction has no sequential return site in the current decode payload.",
            )
        return _execute_call(
            view=view,
            before_cpu=before_cpu,
            before_memory=before_memory,
            decoded=decoded,
            target_pc=decoded.direct_target,
            return_pc=decoded.next_sequential_pc,
        )

    if 0xB0 <= first <= 0xB7 and len(raw) == 2 and 0xE0 <= raw[1] <= 0xEF:
        # call [cc,] (r32): indirect call through the register-held pointer.
        # cc=8 (op 0xE8) is unconditional (e.g. B0 E8 = CALL (XWA),
        # B4 E8 = CALL (XIX)).
        register_index = first & 0x07
        addr_r32_name = R32[register_index]
        addr_value = getattr(before_cpu.regs, REG32_FIELDS[register_index])
        if addr_value is None:
            return _blocked_result(
                before_cpu=before_cpu,
                decoded=decoded,
                status="requires-known-full-register",
                note=(
                    f"CALL ({addr_r32_name}) needs {addr_r32_name} modeled in the current CPU "
                    "state so the indirect target can be computed honestly."
                ),
            )

        cc_idx = raw[1] & 0x0F
        if cc_idx != 8:
            condition_result = _evaluate_condition_code(cc_idx, before_cpu.flags)
            if condition_result is None:
                return _blocked_result(
                    before_cpu=before_cpu,
                    decoded=decoded,
                    status="requires-known-flags",
                    note=(
                        f"call {CC[cc_idx]}, ({addr_r32_name}): the condition flag(s) are not "
                        "known in the current CPU state."
                    ),
                )
            if not condition_result:
                # Condition false: fall through without pushing a return address.
                return _executed_result(
                    before_cpu=before_cpu,
                    decoded=decoded,
                    written_registers=("PC",),
                    memory_writes=(),
                    after_memory=before_memory,
                    new_pc=decoded.next_sequential_pc,
                    reg_updates=None,
                    note=(
                        f"Executed call {CC[cc_idx]}, ({addr_r32_name}) not taken: condition "
                        "false, so PC advanced sequentially and the stack was untouched."
                    ),
                    cycles_consumed=CALL_MEM_CYCLES,
                )

        if decoded.next_sequential_pc is None:
            return _blocked_result(
                before_cpu=before_cpu,
                decoded=decoded,
                status="unsupported-decoded-instruction",
                note=(
                    f"Indirect CALL via {addr_r32_name} has no sequential return site in the "
                    "current decode payload."
                ),
            )
        return _execute_call(
            view=view,
            before_cpu=before_cpu,
            before_memory=before_memory,
            decoded=decoded,
            target_pc=_mask_address(addr_value),
            return_pc=decoded.next_sequential_pc,
            cycles_consumed=CALL_MEM_CYCLES,
        )

    if first == 0x0E:
        return _execute_return(
            view=view,
            before_cpu=before_cpu,
            before_memory=before_memory,
            decoded=decoded,
            stack_adjust=0,
            note=(
                "Executed RET from the current real execution subset. PC was restored from the "
                "writable stack model."
            ),
        )

    if first == 0x0F and len(raw) == 3:
        stack_adjust = _signed_u16(raw[1:3])
        return _execute_return(
            view=view,
            before_cpu=before_cpu,
            before_memory=before_memory,
            decoded=decoded,
            stack_adjust=stack_adjust,
            note=(
                "Executed RETD from the current real execution subset. PC was restored from the "
                "writable stack model and XSP was adjusted by the decoded immediate."
            ),
        )

    return None


def _try_execute_indexed_store(
    view: NgpcFetchView,
    before_cpu: NgpcCpuState,
    before_memory: dict[int, int],
    decoded: DecodeResult,
) -> ExecutionResult | None:
    raw = decoded.raw_bytes
    if raw is None or len(raw) != 3:
        return None

    first = raw[0]
    if not (0xB8 <= first <= 0xBF):
        return None

    store_opcode = raw[2]
    if 0x40 <= store_opcode <= 0x47:
        size_kind = "byte"
    elif 0x50 <= store_opcode <= 0x57:
        size_kind = "word"
    elif 0x60 <= store_opcode <= 0x67:
        size_kind = "long"
    else:
        return None

    address_register_index = first & 0x07
    base_address = getattr(before_cpu.regs, REG32_FIELDS[address_register_index])
    address_register_name = R32[address_register_index]
    if base_address is None:
        return _blocked_result(
            before_cpu=before_cpu,
            decoded=decoded,
            status="requires-known-address-register",
            note=(
                f"{address_register_name} must be known before this indexed store can compute "
                "its effective address honestly."
            ),
        )

    source_register_index = store_opcode & 0x07
    source_register_name, source_value = _extract_register_value(
        before_cpu=before_cpu,
        size_kind=size_kind,
        register_index=source_register_index,
    )
    if source_value is None:
        owner_name = R32[source_register_index // 2] if size_kind == "byte" else R32[source_register_index]
        return _blocked_result(
            before_cpu=before_cpu,
            decoded=decoded,
            status="requires-known-full-register",
            note=(
                f"{source_register_name} cannot be stored honestly until {owner_name} is already "
                "known in the current CPU state."
            ),
        )

    displacement = _signed_u8(raw[1])
    effective_address = (base_address + displacement) & 0xFFFFFFFF
    target_address = _mask_address(effective_address)
    width = {"byte": 1, "word": 2, "long": 4}[size_kind]
    data = source_value.to_bytes(width, "little")
    write_status, write_note = _check_writable_range(view, target_address, width)
    if write_status == "write-discarded":
        return _executed_result(
            before_cpu=before_cpu,
            decoded=decoded,
            written_registers=("PC",),
            memory_writes=(
                MemoryWrite(
                    address=target_address,
                    data=data,
                    note=f"[DISCARDED] {write_note}",
                ),
            ),
            after_memory=before_memory,
            new_pc=decoded.next_sequential_pc,
            reg_updates=None,
            note=(
                "Indexed store destination was unmapped or read-only; write silently "
                "discarded (open-bus behavior — execution continues)."
            ),
        )
    if write_status is not None:
        return _blocked_result(
            before_cpu=before_cpu,
            decoded=decoded,
            status=write_status,
            note=write_note,
        )

    after_memory = dict(before_memory)
    for offset, value in enumerate(data):
        after_memory[_mask_address(target_address + offset)] = value

    return _executed_result(
        before_cpu=before_cpu,
        decoded=decoded,
        written_registers=("PC",),
        memory_writes=(
            MemoryWrite(
                address=target_address,
                data=data,
                note="Writable runtime overlay updated by indexed store execution.",
            ),
        ),
        after_memory=after_memory,
        new_pc=decoded.next_sequential_pc,
        reg_updates=None,
        note=(
            "Executed indexed store from the current real execution subset. The effective "
            "address was computed from the known address register plus displacement and the "
            "bytes were written to the writable runtime overlay."
        ),
    )


def _try_execute_indexed_imm_store(
    view: NgpcFetchView,
    before_cpu: NgpcCpuState,
    before_memory: dict[int, int],
    decoded: DecodeResult,
) -> ExecutionResult | None:
    """Execute (r32+d8) immediate stores: ld (r32+d8), imm8 and ldw (r32+d8), imm16.

    Encoding:
      [B8+r] [d8] [00] [imm8]        => ld  (r32+d8), imm8   (4 bytes)
      [B8+r] [d8] [02] [lo] [hi]     => ldw (r32+d8), imm16  (5 bytes)
    """
    raw = decoded.raw_bytes
    if raw is None or not (0xB8 <= raw[0] <= 0xBF):
        return None

    op = raw[2] if len(raw) >= 3 else None
    if op == 0x00 and len(raw) == 4:
        width, imm = 1, raw[3]
        data_bytes = bytes((imm,))
    elif op == 0x02 and len(raw) == 5:
        width, imm = 2, int.from_bytes(raw[3:5], "little")
        data_bytes = imm.to_bytes(2, "little")
    else:
        return None

    r32_index = raw[0] & 0x07
    r32_field = REG32_FIELDS[r32_index]
    r32_name = R32[r32_index]
    base_address = getattr(before_cpu.regs, r32_field)
    if base_address is None:
        return _blocked_result(
            before_cpu=before_cpu,
            decoded=decoded,
            status="requires-known-address-register",
            note=(
                f"{r32_name} must be known before this indexed immediate store can compute "
                "its effective address honestly."
            ),
        )

    displacement = _signed_u8(raw[1])
    effective_address = (base_address + displacement) & 0xFFFFFFFF
    target_address = _mask_address(effective_address)
    return _execute_absolute_store(
        view=view,
        before_cpu=before_cpu,
        before_memory=before_memory,
        decoded=decoded,
        target_address=target_address,
        data=data_bytes,
        note=(
            f"Executed indexed immediate {'byte' if width == 1 else 'word'} store from the "
            f"current real execution subset. Address {r32_name}+{displacement}="
            f"0x{target_address:06X} written with immediate 0x{imm:0{width*2}X}."
        ),
        memory_note=(
            "Writable runtime overlay updated by indexed immediate store execution."
        ),
    )


def _try_execute_indexed_load(
    view: NgpcFetchView,
    before_cpu: NgpcCpuState,
    before_memory: dict[int, int],
    decoded: DecodeResult,
) -> ExecutionResult | None:
    raw = decoded.raw_bytes
    # 3-byte (r32+d8) forms, plus the 5-byte `ld (abs16), (r32+d8)` mem-to-mem move.
    if raw is None or len(raw) not in (3, 5):
        return None

    first = raw[0]

    # lda R32, (r32+d8) — effective address form (B8..BF, op 30..37)
    if 0xB8 <= first <= 0xBF and 0x30 <= raw[2] <= 0x37:
        load_opcode = raw[2]
        address_register_index = first & 0x07
        address_register_name = R32[address_register_index]
        base_address = getattr(before_cpu.regs, REG32_FIELDS[address_register_index])
        if base_address is None:
            return _blocked_result(
                before_cpu=before_cpu,
                decoded=decoded,
                status="requires-known-address-register",
                note=(
                    f"{address_register_name} must be known before this indexed lda can "
                    "compute its effective address honestly."
                ),
            )
        displacement = _signed_u8(raw[1])
        effective_address = (base_address + displacement) & 0xFFFFFF
        destination_index = load_opcode & 0x07
        return _execute_register_immediate(
            before_cpu=before_cpu,
            before_memory=before_memory,
            decoded=decoded,
            size_kind="long",
            register_index=destination_index,
            value=effective_address,
            note=(
                "Executed indexed lda (load effective address) from the current real execution "
                "subset. The effective address was computed from the base register plus "
                "displacement and stored directly as a 32-bit value."
            ),
        )

    # bit #n, (r32+d8) — read-only bit test (B8..BF, op C8..CF).
    if 0xB8 <= first <= 0xBF and 0xC8 <= raw[2] <= 0xCF:
        address_register_index = first & 0x07
        address_register_name = R32[address_register_index]
        base_address = getattr(before_cpu.regs, REG32_FIELDS[address_register_index])
        if base_address is None:
            return _blocked_result(
                before_cpu=before_cpu,
                decoded=decoded,
                status="requires-known-address-register",
                note=(
                    f"{address_register_name} must be known before this indexed bit test can "
                    "compute its effective address honestly."
                ),
            )
        effective_address = _mask_address((base_address + _signed_u8(raw[1])) & 0xFFFFFFFF)
        source_data = _read_runtime_bytes(view, before_memory, effective_address, 1)
        if source_data is None:
            return _blocked_result(
                before_cpu=before_cpu,
                decoded=decoded,
                status="runtime-memory-unavailable",
                note=(
                    "This indexed bit test needs a readable source byte at its effective address, "
                    "but neither the writable runtime overlay nor the current read bus can provide it."
                ),
            )
        bit_index = raw[2] & 0x07
        bit_set = bool(source_data[0] & (1 << bit_index))
        return _executed_result(
            before_cpu=before_cpu,
            decoded=decoded,
            written_registers=("PC",),
            memory_writes=(),
            after_memory=before_memory,
            new_pc=decoded.next_sequential_pc,
            reg_updates=None,
            flags_updates={"zf": not bit_set, "hf": True, "nf": False},
            note=(
                f"Executed indexed BIT bit test from the current real execution subset. Bit "
                f"{bit_index} of mem8({address_register_name}+d8=0x{effective_address:06X}) "
                "determined Z, while H=1 and N=0."
            ),
        )

    # res/set/chg #n, (r32+d8) — RMW bit ops (B8..BF, op B0..C7).
    if 0xB8 <= first <= 0xBF and 0xB0 <= raw[2] <= 0xC7:
        op_name = {0xB0: "res", 0xB8: "set", 0xC0: "chg"}[raw[2] & 0xF8]
        address_register_index = first & 0x07
        address_register_name = R32[address_register_index]
        base_address = getattr(before_cpu.regs, REG32_FIELDS[address_register_index])
        if base_address is None:
            return _blocked_result(
                before_cpu=before_cpu,
                decoded=decoded,
                status="requires-known-address-register",
                note=(
                    f"{address_register_name} must be known before this indexed {op_name} can "
                    "compute its effective address honestly."
                ),
            )
        effective_address = _mask_address((base_address + _signed_u8(raw[1])) & 0xFFFFFFFF)
        source_data = _read_runtime_bytes(view, before_memory, effective_address, 1)
        if source_data is None:
            return _blocked_result(
                before_cpu=before_cpu,
                decoded=decoded,
                status="runtime-memory-unavailable",
                note=(
                    f"This indexed {op_name} needs a readable source byte at its effective "
                    "address, but neither the writable runtime overlay nor the current read bus "
                    "can provide it."
                ),
            )
        bit_index = raw[2] & 0x07
        mem_value = source_data[0]
        if op_name == "res":
            new_value = mem_value & (~(1 << bit_index) & 0xFF)
        elif op_name == "set":
            new_value = (mem_value | (1 << bit_index)) & 0xFF
        else:  # chg
            new_value = (mem_value ^ (1 << bit_index)) & 0xFF
        write_status, write_note = _check_writable_range(view, effective_address, 1)
        if write_status == "write-discarded":
            return _executed_result(
                before_cpu=before_cpu,
                decoded=decoded,
                written_registers=("PC",),
                memory_writes=(
                    MemoryWrite(address=effective_address, data=bytes([new_value]), note=f"[DISCARDED] {write_note}"),
                ),
                after_memory=before_memory,
                new_pc=decoded.next_sequential_pc,
                reg_updates=None,
                note=(
                    f"Indexed {op_name} {bit_index}, ({address_register_name}+d8) destination was "
                    "unmapped or read-only; write silently discarded (open bus)."
                ),
            )
        if write_status is not None:
            return _blocked_result(
                before_cpu=before_cpu, decoded=decoded, status=write_status, note=write_note,
            )
        after_memory = dict(before_memory)
        after_memory[effective_address] = new_value
        return _executed_result(
            before_cpu=before_cpu,
            decoded=decoded,
            written_registers=("PC",),
            memory_writes=(
                MemoryWrite(
                    address=effective_address,
                    data=bytes([new_value]),
                    note=f"{op_name.upper()} #{bit_index} indexed (r32+d8) mem RMW updated.",
                ),
            ),
            after_memory=after_memory,
            new_pc=decoded.next_sequential_pc,
            reg_updates=None,
            note=(
                f"Executed indexed {op_name} {bit_index}, ({address_register_name}+d8="
                f"0x{effective_address:06X}): mem 0x{mem_value:02X} -> 0x{new_value:02X} "
                "(flags unchanged)."
            ),
        )

    # ld (abs16), (r32+d8) -- memory-to-memory BYTE move (indexed source ->
    # abs16 destination). Shougi / Melon-chan `8F 04 19 32 47` = ld (0x4732), (XSP+4).
    if 0x88 <= first <= 0x8F and len(raw) == 5 and raw[2] == 0x19:
        src_index = first & 0x07
        src_name = R32[src_index]
        base_address = getattr(before_cpu.regs, REG32_FIELDS[src_index])
        if base_address is None:
            return _blocked_result(
                before_cpu=before_cpu, decoded=decoded, status="requires-known-address-register",
                note=f"{src_name} must be known before this indexed mem-to-mem move can compute its address.",
            )
        src_address = _mask_address((base_address + _signed_u8(raw[1])) & 0xFFFFFFFF)
        dest_address = _mask_address(int.from_bytes(raw[3:5], "little"))
        data = _read_runtime_bytes(view, before_memory, src_address, 1)
        if data is None:
            return _blocked_result(
                before_cpu=before_cpu, decoded=decoded, status="runtime-memory-unavailable",
                note="This indexed mem-to-mem byte move needs a readable source byte, unavailable in overlay/bus.",
            )
        return _execute_absolute_store(
            view=view, before_cpu=before_cpu, before_memory=before_memory, decoded=decoded,
            target_address=dest_address, data=bytes(data),
            note=(f"Executed mem-to-mem byte move ld (0x{dest_address & 0xFFFF:04X}), "
                  f"({src_name}+d8=0x{src_address:06X})=0x{data[0]:02X}."),
            memory_note="Writable runtime overlay updated by indexed mem-to-mem byte move.",
        )

    if 0x88 <= first <= 0x8F:
        size_kind = "byte"
    elif 0x98 <= first <= 0x9F:
        size_kind = "word"
    elif 0xA8 <= first <= 0xAF:
        size_kind = "long"
    else:
        return None

    load_opcode = raw[2]
    if not (0x20 <= load_opcode <= 0x27):
        return None

    address_register_index = first & 0x07
    base_address = getattr(before_cpu.regs, REG32_FIELDS[address_register_index])
    address_register_name = R32[address_register_index]
    if base_address is None:
        return _blocked_result(
            before_cpu=before_cpu,
            decoded=decoded,
            status="requires-known-address-register",
            note=(
                f"{address_register_name} must be known before this indexed load can compute "
                "its effective address honestly."
            ),
        )

    destination_index = load_opcode & 0x07
    displacement = _signed_u8(raw[1])
    effective_address = (base_address + displacement) & 0xFFFFFFFF
    width = {"byte": 1, "word": 2, "long": 4}[size_kind]
    data = _read_runtime_bytes(view, before_memory, _mask_address(effective_address), width)
    if data is None:
        return _blocked_result(
            before_cpu=before_cpu,
            decoded=decoded,
            status="runtime-memory-unavailable",
            note=(
                "This indexed load needs readable bytes at its effective address, but neither "
                "the writable runtime overlay nor the current read bus can provide them."
            ),
        )

    return _execute_register_immediate(
        before_cpu=before_cpu,
        before_memory=before_memory,
        decoded=decoded,
        size_kind=size_kind,
        register_index=destination_index,
        value=int.from_bytes(data, "little"),
        note=(
            "Executed indexed load from the current real execution subset. The effective "
            "address was computed from the known address register plus displacement and the "
            "loaded bytes came from the writable runtime overlay or read bus."
        ),
    )


def _execute_secondary_indexed_incdec(
    before_cpu: NgpcCpuState,
    before_memory: dict[int, int],
    decoded: DecodeResult,
    op_byte: int,
    size_kind: str,
    width: int,
    effective_address: int,
    memory_value: int,
    ea_note: str,
) -> ExecutionResult:
    """Execute a secondary-indexed INC/DEC #n, (mem) read-modify-write.

    `n = op & 0x07` with `0 -> 8` (Toshiba quirk). INC/DEC on memory updates
    `S/Z/V/H` and `N` (0 for INC, 1 for DEC) but **preserves CF**, matching the
    existing `(R32)` / abs INC/DEC handlers.
    """
    is_dec = op_byte >= 0x68
    op_name = "dec" if is_dec else "inc"
    count = (op_byte & 0x07) or 8
    mask = (1 << (width * 8)) - 1
    if is_dec:
        new_value = (memory_value - count) & mask
        flags_updates = dict(_compute_subtract_flags(size_kind, memory_value, count))
    else:
        new_value = (memory_value + count) & mask
        flags_updates = dict(_compute_add_flags(size_kind, memory_value, count))
    flags_updates.pop("cf", None)  # INC/DEC mem preserves carry.

    new_bytes = new_value.to_bytes(width, "little")
    after_memory = dict(before_memory)
    for offset, byte_value in enumerate(new_bytes):
        after_memory[_mask_address(effective_address + offset)] = byte_value

    return _executed_result(
        before_cpu=before_cpu,
        decoded=decoded,
        written_registers=("PC",),
        memory_writes=(
            MemoryWrite(
                address=_mask_address(effective_address),
                data=new_bytes,
                note=f"{op_name.upper()} #{count} secondary-indexed mem RMW updated.",
            ),
        ),
        after_memory=after_memory,
        new_pc=decoded.next_sequential_pc,
        reg_updates=None,
        flags_updates=flags_updates,
        cycles_consumed=INCDEC_MEM_BYTE_CYCLES if width == 1 else INCDEC_MEM_WORD_CYCLES,
        note=(
            f"Executed secondary-indexed {size_kind} {op_name} #{count} from the current real "
            f"execution subset. EA = {ea_note}; mem 0x{memory_value:0{width * 2}X} -> "
            f"0x{new_value:0{width * 2}X} (CF preserved)."
        ),
    )


_SECONDARY_INDEXED_ALU_NAMES = {
    0x80: "add", 0x90: "adc", 0xA0: "sub", 0xB0: "sbc",
    0xC0: "and", 0xD0: "xor", 0xE0: "or", 0xF0: "cp",
}


def _execute_secondary_indexed_alu(
    before_cpu: NgpcCpuState,
    before_memory: dict[int, int],
    decoded: DecodeResult,
    op_byte: int,
    size_kind: str,
    width: int,
    register_index: int,
    effective_address: int,
    memory_value: int,
    ea_note: str,
) -> ExecutionResult:
    """Execute a secondary-indexed ALU op against a memory operand.

    Covers the `add/adc/sub/sbc/and/xor/or/cp` family in both directions:
      - `R,(mem)` (op low nibble 0x0): dest = register.
      - `(mem),R` (op low nibble 0x8): dest = memory (read-modify-write).
    `cp` is compare-only in either direction. Operand order follows ngdis
    (`tlcs900_zz_mem.c`): the destination is the left operand. ADC/SBC fold the
    modeled carry into the right operand exactly like the `(R32),imm8` handler.
    """
    op_name = _SECONDARY_INDEXED_ALU_NAMES[op_byte & 0xF0]
    mem_is_dest = bool(op_byte & 0x08)
    reg_name, reg_value = _extract_register_value(before_cpu, size_kind, register_index)
    if reg_value is None:
        return _blocked_result(
            before_cpu=before_cpu,
            decoded=decoded,
            status="requires-known-full-register",
            note=(
                f"{reg_name} must be known to execute this secondary-indexed {size_kind} "
                f"{op_name} honestly."
            ),
        )

    needs_carry = op_name in ("adc", "sbc")
    if needs_carry and before_cpu.flags.cf is None:
        return _blocked_result(
            before_cpu=before_cpu,
            decoded=decoded,
            status="runtime-state-required",
            note=(
                f"{op_name.upper()} needs a known carry flag before this secondary-indexed "
                f"{size_kind} op can execute honestly."
            ),
        )
    carry = int(before_cpu.flags.cf) if needs_carry else 0

    left, right = (memory_value, reg_value) if mem_is_dest else (reg_value, memory_value)
    mask = (1 << (width * 8)) - 1
    if op_name in ("add", "adc"):
        result = (left + right + carry) & mask
        flags_updates = _compute_add_flags(size_kind, left, right + carry)
    elif op_name in ("sub", "sbc", "cp"):
        result = (left - right - carry) & mask
        flags_updates = _compute_subtract_flags(size_kind, left, right + carry)
    elif op_name == "and":
        result = left & right
        flags_updates = _compute_logical_flags(size_kind, result, half_carry=True)
    elif op_name == "xor":
        result = left ^ right
        flags_updates = _compute_logical_flags(size_kind, result)
    else:  # or
        result = left | right
        flags_updates = _compute_logical_flags(size_kind, result)

    hex_w = width * 2
    if op_name == "cp":
        return _executed_result(
            before_cpu=before_cpu,
            decoded=decoded,
            written_registers=("PC",),
            memory_writes=(),
            after_memory=before_memory,
            new_pc=decoded.next_sequential_pc,
            reg_updates=None,
            flags_updates=flags_updates,
            note=(
                f"Executed secondary-indexed {size_kind} cp. EA = {ea_note}; "
                f"flags = 0x{left:0{hex_w}X} - 0x{right:0{hex_w}X}."
            ),
        )

    if mem_is_dest:
        new_bytes = result.to_bytes(width, "little")
        after_memory = dict(before_memory)
        for offset, byte_value in enumerate(new_bytes):
            after_memory[_mask_address(effective_address + offset)] = byte_value
        return _executed_result(
            before_cpu=before_cpu,
            decoded=decoded,
            written_registers=("PC",),
            memory_writes=(
                MemoryWrite(
                    address=_mask_address(effective_address),
                    data=new_bytes,
                    note=f"{op_name.upper()} (mem), {reg_name} secondary-indexed RMW updated.",
                ),
            ),
            after_memory=after_memory,
            new_pc=decoded.next_sequential_pc,
            reg_updates=None,
            flags_updates=flags_updates,
            note=(
                f"Executed secondary-indexed {size_kind} {op_name} (mem), {reg_name}. "
                f"EA = {ea_note}; mem 0x{memory_value:0{hex_w}X} -> 0x{result:0{hex_w}X}."
            ),
        )

    register_name, reg_updates = _build_register_update(
        before_cpu, size_kind=size_kind, register_index=register_index, value=result,
    )
    if reg_updates is None:
        return _blocked_result(
            before_cpu=before_cpu,
            decoded=decoded,
            status="requires-known-full-register",
            note=(
                f"{reg_name} is only partially representable; this secondary-indexed "
                f"{op_name} can be applied only when its owner register is known."
            ),
        )
    return _executed_result(
        before_cpu=before_cpu,
        decoded=decoded,
        written_registers=(register_name, "PC"),
        memory_writes=(),
        after_memory=before_memory,
        new_pc=decoded.next_sequential_pc,
        reg_updates=reg_updates,
        flags_updates=flags_updates,
        note=(
            f"Executed secondary-indexed {size_kind} {op_name} {reg_name}, (mem). "
            f"EA = {ea_note}; {reg_name} 0x{reg_value:0{hex_w}X} -> 0x{result:0{hex_w}X}."
        ),
    )


def _try_execute_secondary_indexed_load(
    view: NgpcFetchView,
    before_cpu: NgpcCpuState,
    before_memory: dict[int, int],
    decoded: DecodeResult,
) -> ExecutionResult | None:
    raw = decoded.raw_bytes
    if raw is None or len(raw) not in (5, 6) or raw[0] not in (0xC3, 0xD3, 0xE3):
        return None

    secondary = raw[1]
    mode = secondary & 0x03

    if mode == 0x01:
        # (r32+d16): base from the secondary byte, signed 16-bit displacement.
        # Destination/op per the op byte: 0x20..0x27 = load, C3 0x3F = cp imm8.
        is_load = len(raw) == 5 and 0x20 <= raw[4] <= 0x27
        is_cp_reg = len(raw) == 5 and 0xF0 <= raw[4] <= 0xF7
        is_incdec = len(raw) == 5 and 0x60 <= raw[4] <= 0x6F
        is_push = len(raw) == 5 and raw[4] == 0x04
        is_cp_imm = len(raw) == 6 and raw[0] == 0xC3 and raw[4] == 0x3F
        is_alu = len(raw) == 5 and 0x80 <= raw[4] <= 0xFF and not is_cp_reg
        if not (is_load or is_cp_reg or is_incdec or is_push or is_cp_imm or is_alu):
            return None
        base_name, base_value, _base_refusal = secondary_base_r32(before_cpu, secondary)
        if _base_refusal is not None:
            return _blocked_result(
                before_cpu=before_cpu,
                decoded=decoded,
                status="unimplemented",
                note=_base_refusal,
            )
        if base_value is None:
            return _blocked_result(
                before_cpu=before_cpu,
                decoded=decoded,
                status="requires-known-address-register",
                note=(
                    f"{base_name} must be known before this secondary-indexed d16 access can "
                    "compute its effective address honestly."
                ),
            )

        d16_raw = int.from_bytes(raw[2:4], "little")
        d16 = d16_raw if d16_raw < 0x8000 else d16_raw - 0x10000
        effective_address = _mask_address((base_value + d16) & 0xFFFFFFFF)

        if is_cp_imm:
            data = _read_runtime_bytes(view, before_memory, effective_address, 1)
            if data is None:
                return _blocked_result(
                    before_cpu=before_cpu,
                    decoded=decoded,
                    status="runtime-memory-unavailable",
                    note=(
                        "This secondary-indexed d16 byte compare needs 1 readable byte at its "
                        "effective address, but neither the writable runtime overlay nor the "
                        "current read bus can provide it."
                    ),
                )
            memory_value = data[0]
            imm8 = raw[5]
            return _executed_result(
                before_cpu=before_cpu,
                decoded=decoded,
                written_registers=("PC",),
                memory_writes=(),
                after_memory=before_memory,
                new_pc=decoded.next_sequential_pc,
                reg_updates=None,
                flags_updates=_compute_subtract_flags("byte", memory_value, imm8),
                note=(
                    f"Executed secondary-indexed d16 byte compare from the current real "
                    f"execution subset. EA = {base_name}(0x{base_value:06X}) + {d16} "
                    f"= 0x{effective_address:06X}; compared mem8 0x{memory_value:02X} against "
                    f"imm8 0x{imm8:02X}."
                ),
            )

        register_index = raw[4] & 0x07
        if raw[0] == 0xC3:
            size_kind = "byte"
            width = 1
        elif raw[0] == 0xD3:
            size_kind = "word"
            width = 2
        else:
            size_kind = "long"
            width = 4

        data = _read_runtime_bytes(view, before_memory, effective_address, width)
        if data is None:
            return _blocked_result(
                before_cpu=before_cpu,
                decoded=decoded,
                status="runtime-memory-unavailable",
                note=(
                    f"This secondary-indexed d16 {size_kind} access needs {width} readable "
                    "byte(s) at its effective address, but neither the writable runtime "
                    "overlay nor the current read bus can provide them."
                ),
            )
        memory_value = int.from_bytes(data, "little")

        if is_alu:
            return _execute_secondary_indexed_alu(
                before_cpu=before_cpu,
                before_memory=before_memory,
                decoded=decoded,
                op_byte=raw[4],
                size_kind=size_kind,
                width=width,
                register_index=register_index,
                effective_address=effective_address,
                memory_value=memory_value,
                ea_note=f"{base_name}(0x{base_value:06X}) + {d16} = 0x{effective_address:06X}",
            )

        if is_push:
            return _execute_push_bytes(
                view=view,
                before_cpu=before_cpu,
                before_memory=before_memory,
                decoded=decoded,
                data=bytes(data),
                note=(
                    f"Executed secondary-indexed d16 {size_kind} push: pushed mem "
                    f"0x{memory_value:0{width * 2}X} from EA {base_name}(0x{base_value:06X}) "
                    f"+ {d16} = 0x{effective_address:06X}."
                ),
            )

        if is_incdec:
            return _execute_secondary_indexed_incdec(
                before_cpu=before_cpu,
                before_memory=before_memory,
                decoded=decoded,
                op_byte=raw[4],
                size_kind=size_kind,
                width=width,
                effective_address=effective_address,
                memory_value=memory_value,
                ea_note=f"{base_name}(0x{base_value:06X}) + {d16} = 0x{effective_address:06X}",
            )

        if is_cp_reg:
            reg_name, reg_value = _extract_register_value(before_cpu, size_kind, register_index)
            if reg_value is None:
                return _blocked_result(
                    before_cpu=before_cpu,
                    decoded=decoded,
                    status="requires-known-full-register",
                    note=(
                        f"{reg_name} must be known to execute this secondary-indexed d16 "
                        f"{size_kind} compare honestly."
                    ),
                )
            return _executed_result(
                before_cpu=before_cpu,
                decoded=decoded,
                written_registers=("PC",),
                memory_writes=(),
                after_memory=before_memory,
                new_pc=decoded.next_sequential_pc,
                reg_updates=None,
                flags_updates=_compute_subtract_flags(size_kind, reg_value, memory_value),
                note=(
                    f"Executed secondary-indexed d16 {size_kind} compare from the current real "
                    f"execution subset. EA = {base_name}(0x{base_value:06X}) + {d16} "
                    f"= 0x{effective_address:06X}; compared {reg_name}=0x{reg_value:0{width * 2}X} "
                    f"against mem 0x{memory_value:0{width * 2}X}."
                ),
            )

        return _execute_register_immediate(
            before_cpu=before_cpu,
            before_memory=before_memory,
            decoded=decoded,
            size_kind=size_kind,
            register_index=register_index,
            value=memory_value,
            note=(
                f"Executed secondary-indexed d16 {size_kind} load from the current real "
                f"execution subset. EA = {base_name}(0x{base_value:06X}) + {d16} "
                f"= 0x{effective_address:06X}; {width} byte(s) were read into the "
                "destination register."
            ),
        )

    if mode != 0x03:
        return None

    base_name, base_value, _base_refusal = secondary_base_r32(before_cpu, raw[2])
    if _base_refusal is not None:
        return _blocked_result(
            before_cpu=before_cpu,
            decoded=decoded,
            status="unimplemented",
            note=_base_refusal,
        )
    if base_value is None:
        return _blocked_result(
            before_cpu=before_cpu,
            decoded=decoded,
            status="requires-known-address-register",
            note=(
                f"{base_name} must be known before this secondary-indexed load can "
                "compute its effective address honestly."
            ),
        )

    if secondary & 0x04:
        index_kind = "word"
        index_index = _indexed_r16_index_from_code(raw[3])
        if index_index is None:
            return _blocked_result(
                before_cpu=before_cpu,
                decoded=decoded,
                status="unimplemented",
                note=(
                    f"Register-index WORD code 0x{raw[3]:02X} is not a current-bank r16. "
                    "Guessing one would silently index the wrong register."
                ),
            )
    else:
        index_kind = "byte"
        index_index = _indexed_r8_index_from_code(raw[3])
        if index_index is None:
            return _blocked_result(
                before_cpu=before_cpu,
                decoded=decoded,
                status="requires-known-source-register",
                note=(
                    f"Register-index byte code 0x{raw[3]:02X} names a register this core "
                    "cannot resolve (a bank escape, a Q-half, or a half of XIX..XSP). "
                    "Guessing one would compute a wrong effective address silently."
                ),
            )

    index_name, index_value = _extract_register_value(
        before_cpu=before_cpu,
        size_kind=index_kind,
        register_index=index_index,
    )
    if index_value is None and index_kind == "byte":
        index_value = _extract_current_banked_r8_value(before_cpu, index_index)
    if index_value is None:
        return _blocked_result(
            before_cpu=before_cpu,
            decoded=decoded,
            status="requires-known-source-register",
            note=(
                f"{index_name} must be known before this secondary-indexed load can "
                "compute its effective address honestly."
            ),
        )

    index_disp = (
        _indexed_signed_byte(index_value)
        if index_kind == "byte"
        else _indexed_signed_word(index_value)
    )
    effective_address = _mask_address((base_value + index_disp) & 0xFFFFFFFF)
    if raw[0] == 0xC3 and len(raw) == 6 and raw[4] == 0x3F:
        data = _read_runtime_bytes(view, before_memory, effective_address, 1)
        if data is None:
            return _blocked_result(
                before_cpu=before_cpu,
                decoded=decoded,
                status="runtime-memory-unavailable",
                note=(
                    "This secondary-indexed byte compare needs 1 readable byte at its effective "
                    "address, but neither the writable runtime overlay nor the current read bus "
                    "can provide it."
                ),
            )
        memory_value = data[0]
        imm8 = raw[5]
        return _executed_result(
            before_cpu=before_cpu,
            decoded=decoded,
            written_registers=("PC",),
            memory_writes=(),
            after_memory=before_memory,
            new_pc=decoded.next_sequential_pc,
            reg_updates=None,
            flags_updates=_compute_subtract_flags("byte", memory_value, imm8),
            note=(
                f"Executed secondary-indexed byte compare from the current real execution subset. "
                f"EA = {base_name}(0x{base_value:06X}) + {index_name}(0x{index_value:X}) = "
                f"0x{effective_address:06X}; compared mem8 0x{memory_value:02X} against "
                f"imm8 0x{imm8:02X}."
            ),
        )

    is_cp_reg = 0xF0 <= raw[4] <= 0xF7
    is_incdec = 0x60 <= raw[4] <= 0x6F
    is_alu = 0x80 <= raw[4] <= 0xFF and not is_cp_reg
    is_push = raw[4] == 0x04
    if not (0x20 <= raw[4] <= 0x27 or is_cp_reg or is_incdec or is_alu or is_push):
        return None

    register_index = raw[4] & 0x07
    if raw[0] == 0xC3:
        size_kind = "byte"
        width = 1
    elif raw[0] == 0xD3:
        size_kind = "word"
        width = 2
    else:
        size_kind = "long"
        width = 4

    data = _read_runtime_bytes(view, before_memory, effective_address, width)
    if data is None:
        return _blocked_result(
            before_cpu=before_cpu,
            decoded=decoded,
            status="runtime-memory-unavailable",
            note=(
                f"This secondary-indexed {size_kind} access needs {width} readable byte(s) at its "
                "effective address, but neither the writable runtime overlay nor the current read "
                "bus can provide them."
            ),
        )
    memory_value = int.from_bytes(data, "little")

    if is_push:
        return _execute_push_bytes(
            view=view,
            before_cpu=before_cpu,
            before_memory=before_memory,
            decoded=decoded,
            data=bytes(data),
            note=(
                f"Executed secondary-indexed {size_kind} push: pushed mem "
                f"0x{memory_value:0{width * 2}X} from EA {base_name}(0x{base_value:06X}) + "
                f"{index_name}(0x{index_value:X}) = 0x{effective_address:06X}."
            ),
        )

    if is_alu:
        return _execute_secondary_indexed_alu(
            before_cpu=before_cpu,
            before_memory=before_memory,
            decoded=decoded,
            op_byte=raw[4],
            size_kind=size_kind,
            width=width,
            register_index=register_index,
            effective_address=effective_address,
            memory_value=memory_value,
            ea_note=(
                f"{base_name}(0x{base_value:06X}) + {index_name}(0x{index_value:X}) "
                f"= 0x{effective_address:06X}"
            ),
        )

    if is_incdec:
        return _execute_secondary_indexed_incdec(
            before_cpu=before_cpu,
            before_memory=before_memory,
            decoded=decoded,
            op_byte=raw[4],
            size_kind=size_kind,
            width=width,
            effective_address=effective_address,
            memory_value=memory_value,
            ea_note=(
                f"{base_name}(0x{base_value:06X}) + {index_name}(0x{index_value:X}) "
                f"= 0x{effective_address:06X}"
            ),
        )

    if is_cp_reg:
        reg_name, reg_value = _extract_register_value(before_cpu, size_kind, register_index)
        if reg_value is None:
            return _blocked_result(
                before_cpu=before_cpu,
                decoded=decoded,
                status="requires-known-full-register",
                note=(
                    f"{reg_name} must be known to execute this secondary-indexed "
                    f"{size_kind} compare honestly."
                ),
            )
        return _executed_result(
            before_cpu=before_cpu,
            decoded=decoded,
            written_registers=("PC",),
            memory_writes=(),
            after_memory=before_memory,
            new_pc=decoded.next_sequential_pc,
            reg_updates=None,
            flags_updates=_compute_subtract_flags(size_kind, reg_value, memory_value),
            note=(
                f"Executed secondary-indexed {size_kind} compare from the current real execution "
                f"subset. EA = {base_name}(0x{base_value:06X}) + {index_name}(0x{index_value:X}) = "
                f"0x{effective_address:06X}; compared {reg_name}=0x{reg_value:0{width * 2}X} against "
                f"mem 0x{memory_value:0{width * 2}X}."
            ),
        )

    return _execute_register_immediate(
        before_cpu=before_cpu,
        before_memory=before_memory,
        decoded=decoded,
        size_kind=size_kind,
        register_index=register_index,
        value=memory_value,
        note=(
            f"Executed secondary-indexed {size_kind} load from the current real execution subset. "
            f"EA = {base_name}(0x{base_value:06X}) + {index_name}(0x{index_value:X}) = "
            f"0x{effective_address:06X}; {width} byte(s) were read from the writable runtime overlay "
            "or current read bus into the destination register."
        ),
    )


def _try_execute_secondary_indexed_bit(
    view: NgpcFetchView,
    before_cpu: NgpcCpuState,
    before_memory: dict[int, int],
    decoded: DecodeResult,
) -> ExecutionResult | None:
    """Execute `BIT #n, (mem)` on the F3 secondary-indexed addressing.

    Covers mode=1 `(r32+d16)` and mode=3 `(r32+r16)` (op `0xC8..0xCF`): reads
    one byte at the effective address, sets `Z = NOT bit`, `H = 1`, `N = 0`,
    and writes nothing.
    """
    raw = decoded.raw_bytes
    if raw is None or len(raw) != 5 or raw[0] != 0xF3 or not (0xC8 <= raw[4] <= 0xCF):
        return None

    secondary = raw[1]
    mode = secondary & 0x03

    if mode == 0x01:
        base_name, base_value, _base_refusal = secondary_base_r32(before_cpu, secondary)
        if _base_refusal is not None:
            return _blocked_result(
                before_cpu=before_cpu,
                decoded=decoded,
                status="unimplemented",
                note=_base_refusal,
            )
        if base_value is None:
            return _blocked_result(
                before_cpu=before_cpu,
                decoded=decoded,
                status="requires-known-address-register",
                note=(
                    f"{base_name} must be known before this secondary-indexed d16 bit test can "
                    "compute its effective address honestly."
                ),
            )
        d16_raw = int.from_bytes(raw[2:4], "little")
        d16 = d16_raw if d16_raw < 0x8000 else d16_raw - 0x10000
        effective_address = _mask_address((base_value + d16) & 0xFFFFFFFF)
        ea_note = f"{base_name}+d8=0x{effective_address:06X}"
    elif mode == 0x03:
        base_name, base_value, _base_refusal = secondary_base_r32(before_cpu, raw[2])
        if _base_refusal is not None:
            return _blocked_result(
                before_cpu=before_cpu,
                decoded=decoded,
                status="unimplemented",
                note=_base_refusal,
            )
        if base_value is None:
            return _blocked_result(
                before_cpu=before_cpu,
                decoded=decoded,
                status="requires-known-address-register",
                note=(
                    f"{base_name} must be known before this secondary-indexed bit test can "
                    "compute its effective address honestly."
                ),
            )
        index_kind = "word" if secondary & 0x04 else "byte"
        if index_kind == "word":
            index_index = _indexed_r16_index_from_code(raw[3])
            if index_index is None:
                return _blocked_result(
                    before_cpu=before_cpu,
                    decoded=decoded,
                    status="unimplemented",
                    note=(
                        f"Register-index WORD code 0x{raw[3]:02X} is not a current-bank r16. "
                        "Guessing one would silently index the wrong register."
                    ),
                )
        else:
            index_index = _indexed_r8_index_from_code(raw[3])
            if index_index is None:
                return _blocked_result(
                    before_cpu=before_cpu,
                    decoded=decoded,
                    status="requires-known-source-register",
                    note=(
                        f"Register-index byte code 0x{raw[3]:02X} names a register this core "
                        "cannot resolve (a bank escape, a Q-half, or a half of XIX..XSP). "
                        "Guessing one would compute a wrong effective address silently."
                    ),
                )
        index_name, index_value = _extract_register_value(
            before_cpu=before_cpu,
            size_kind=index_kind,
            register_index=index_index,
        )
        if index_value is None and index_kind == "byte":
            index_value = _extract_current_banked_r8_value(before_cpu, index_index)
        if index_value is None:
            return _blocked_result(
                before_cpu=before_cpu,
                decoded=decoded,
                status="requires-known-source-register",
                note=(
                    f"{index_name} must be known before this secondary-indexed bit test can "
                    "compute its effective address honestly."
                ),
            )
        index_disp = (
            _indexed_signed_byte(index_value)
            if index_kind == "byte"
            else _indexed_signed_word(index_value)
        )
        effective_address = _mask_address((base_value + index_disp) & 0xFFFFFFFF)
        ea_note = f"{base_name}+{index_name}=0x{effective_address:06X}"
    else:
        return None

    source_data = _read_runtime_bytes(view, before_memory, effective_address, 1)
    if source_data is None:
        return _blocked_result(
            before_cpu=before_cpu,
            decoded=decoded,
            status="runtime-memory-unavailable",
            note=(
                "This secondary-indexed bit test needs a readable source byte at its effective "
                "address, but neither the writable runtime overlay nor the current read bus can "
                "provide it."
            ),
        )
    bit_index = raw[4] & 0x07
    bit_set = bool(source_data[0] & (1 << bit_index))
    return _executed_result(
        before_cpu=before_cpu,
        decoded=decoded,
        written_registers=("PC",),
        memory_writes=(),
        after_memory=before_memory,
        new_pc=decoded.next_sequential_pc,
        reg_updates=None,
        flags_updates={"zf": not bit_set, "hf": True, "nf": False},
        note=(
            f"Executed secondary-indexed BIT bit test from the current real execution subset. "
            f"Bit {bit_index} of mem8({ea_note}) determined Z, while H=1 and N=0."
        ),
    )


def _try_execute_secondary_indexed_jump(
    before_cpu: NgpcCpuState,
    before_memory: dict[int, int],
    decoded: DecodeResult,
) -> ExecutionResult | None:
    raw = decoded.raw_bytes
    if raw is None or len(raw) != 5 or raw[0] != 0xF3 or raw[4] != 0xD8:
        return None

    secondary = raw[1]
    if (secondary & 0x03) != 0x03:
        return None

    base_name, base_value, _base_refusal = secondary_base_r32(before_cpu, raw[2])
    if _base_refusal is not None:
        return _blocked_result(
            before_cpu=before_cpu,
            decoded=decoded,
            status="unimplemented",
            note=_base_refusal,
        )
    if base_value is None:
        return _blocked_result(
            before_cpu=before_cpu,
            decoded=decoded,
            status="requires-known-address-register",
            note=(
                f"{base_name} must be known before this secondary-indexed jump can compute "
                "its target honestly."
            ),
        )

    if secondary & 0x04:
        index_kind = "word"
        index_index = _indexed_r16_index_from_code(raw[3])
        if index_index is None:
            return _blocked_result(
                before_cpu=before_cpu,
                decoded=decoded,
                status="unimplemented",
                note=(
                    f"Register-index WORD code 0x{raw[3]:02X} is not a current-bank r16. "
                    "Guessing one would silently index the wrong register."
                ),
            )
    else:
        index_kind = "byte"
        index_index = _indexed_r8_index_from_code(raw[3])
        if index_index is None:
            return _blocked_result(
                before_cpu=before_cpu,
                decoded=decoded,
                status="requires-known-source-register",
                note=(
                    f"Register-index byte code 0x{raw[3]:02X} names a register this core "
                    "cannot resolve (a bank escape, a Q-half, or a half of XIX..XSP). "
                    "Guessing one would compute a wrong effective address silently."
                ),
            )

    index_name, index_value = _extract_register_value(
        before_cpu=before_cpu,
        size_kind=index_kind,
        register_index=index_index,
    )
    if index_value is None and index_kind == "byte":
        index_value = _extract_current_banked_r8_value(before_cpu, index_index)
    if index_value is None:
        return _blocked_result(
            before_cpu=before_cpu,
            decoded=decoded,
            status="requires-known-source-register",
            note=(
                f"{index_name} must be known before this secondary-indexed jump can compute "
                "its target honestly."
            ),
        )

    target_address = _mask_address((base_value + index_value) & 0xFFFFFFFF)
    return _executed_result(
        before_cpu=before_cpu,
        decoded=decoded,
        written_registers=("PC",),
        memory_writes=(),
        after_memory=before_memory,
        new_pc=target_address,
        reg_updates=None,
        flags_updates=None,
        note=(
            f"Executed secondary-indexed jump from the current real execution subset. "
            f"Target = {base_name}(0x{base_value:06X}) + {index_name}(0x{index_value:X}) = "
            f"0x{target_address:06X}."
        ),
        cycles_consumed=JP_MEM_CYCLES,
    )


def _execute_word_memory_muldiv_common(
    before_cpu: NgpcCpuState,
    before_memory: dict[int, int],
    decoded: DecodeResult,
    *,
    mode: str,
    destination_index: int,
    destination_value: int,
    memory_word: int,
    operand_description: str,
) -> ExecutionResult:
    """Execute word-memory MUL/MULS/DIV/DIVS into a 32-bit destination register."""

    def _to_signed(value: int, bits: int) -> int:
        sign_bit = 1 << (bits - 1)
        mask = (1 << bits) - 1
        value &= mask
        if value & sign_bit:
            return value - (1 << bits)
        return value

    destination_name = R32[destination_index]
    destination_field = REG32_FIELDS[destination_index]

    if mode == "mul":
        left = destination_value & 0xFFFF
        right = memory_word
        raw_result = left * right
        result = raw_result & 0xFFFFFFFF
        # MUL / MULS CHANGE NO FLAGS -- Toshiba's <Multiply> page is `- - - - - -`,
        # spelled out line by line ("S = No change", "Z = No change", ...). This
        # shared word mul/div core was publishing Z, S, C and V from the product,
        # so code that multiplied and then branched on Z got the wrong answer.
        # The hardware tests behind this handler (hw_test_muldiv, 2026-07-06/08)
        # validated the RESULT PLACEMENT -- the remainder in the high word -- not
        # the flags. Found 2026-07-12 by the C++ differential harness.
        flags_updates = None
        note = (
            f"Executed {mode}: ({destination_name} & 0xFFFF)=0x{left:04X} * "
            f"{operand_description}=0x{right:04X} -> 0x{result:08X}."
        )
    elif mode == "muls":
        left_signed = _to_signed(destination_value, 16)
        right_signed = _to_signed(memory_word, 16)
        raw_result = left_signed * right_signed
        result = raw_result & 0xFFFFFFFF
        flags_updates = None   # see the <Multiply> note above: no flags at all
        note = (
            f"Executed {mode}: signed16({destination_name})={left_signed} * "
            f"signed16({operand_description})={right_signed} -> 0x{result:08X}."
        )
    elif mode in ("div", "divs"):
        if memory_word == 0:
            return _blocked_result(
                before_cpu=before_cpu,
                decoded=decoded,
                status="division-by-zero",
                note=(
                    f"{mode.upper()} by zero is not modeled honestly: TLCS-900/H sets VF and "
                    "the packed destination result is not something this emulator should guess."
                ),
            )

        if mode == "div":
            quotient_full = destination_value // memory_word
            remainder_full = destination_value % memory_word
            overflow = quotient_full > 0xFFFF
        else:
            signed_dividend = _to_signed(destination_value, 32)
            signed_divisor = _to_signed(memory_word, 16)
            quotient_full = int(signed_dividend / signed_divisor)
            remainder_full = signed_dividend - (quotient_full * signed_divisor)
            overflow = quotient_full < -0x8000 or quotient_full > 0x7FFF

        quotient_packed = quotient_full & 0xFFFF
        remainder_packed = remainder_full & 0xFFFF
        result = (remainder_packed << 16) | quotient_packed
        # DIV writes **V ONLY**. Toshiba's <Divide> symbol row is `- - - V - -`:
        # V = 1 on divide-by-zero or quotient overflow, 0 otherwise; S, Z, H, N and
        # C are all "No change". This branch was also publishing Z, S and C from
        # the quotient, so a `div` followed by a branch on S or Z read the wrong
        # answer. Found 2026-07-12 by gate G3 on Pac-Man and Neo Turf Masters,
        # where the native core kept S and the reference cleared it.
        flags_updates = {"vf": overflow}
        note = (
            f"Executed {mode}: {destination_name}=0x{destination_value:08X} / "
            f"{operand_description}=0x{memory_word:04X} -> quot=0x{quotient_packed:04X}, "
            f"rem=0x{remainder_packed:04X}, packed=0x{result:08X}."
        )
    else:
        raise ValueError(f"Unsupported word-memory mul/div mode: {mode}")

    return _executed_result(
        before_cpu=before_cpu,
        decoded=decoded,
        written_registers=(destination_name, "PC"),
        memory_writes=(),
        after_memory=before_memory,
        new_pc=decoded.next_sequential_pc,
        reg_updates={destination_field: result},
        flags_updates=flags_updates,
        note=note,
    )


def _try_execute_indexed_word_muldiv(
    view: NgpcFetchView,
    before_cpu: NgpcCpuState,
    before_memory: dict[int, int],
    decoded: DecodeResult,
) -> ExecutionResult | None:
    """Word-indexed memory multiply and unsigned divide.

    Encoding (3 bytes):
      [0x98+r_base] [d8] [op]
        op in 0x40..0x47 → mul XRdst, (Rbase+d8)
        op in 0x50..0x57 → div XRdst, (Rbase+d8)

    Semantics:
      mul: XRdst = (HLdst(low16) * mem_word) & 0xFFFFFFFF.
           Both operands unsigned. The 32-bit product replaces XRdst.
      div: dividend = XRdst (full 32-bit), divisor = mem_word.
           quotient (low 16) and remainder (upper 16) merge into XRdst.
           Division by zero blocks honestly: the CPU sets VF and skips
           the write-back on real hardware, but this implementation
           refuses rather than guessing which guard cc900 relied on.

    Catalog reference: t900cc.py jalon 6 (HW-validated) lists these
    encodings as the safe replacement for the broken D8+r+r 32-bit
    register-register multiplications.
    """
    raw = decoded.raw_bytes
    if raw is None or len(raw) != 3:
        return None
    first = raw[0]
    if not (0x98 <= first <= 0x9F):
        return None
    op = raw[2]
    mode = {
        0x40: "mul",
        0x48: "muls",
        0x50: "div",
        0x58: "divs",
    }.get(op & 0xF8)
    if mode is None:
        return None

    base_register_index = first & 0x07
    base_register_name = R32[base_register_index]
    base_address = getattr(before_cpu.regs, REG32_FIELDS[base_register_index])
    if base_address is None:
        return _blocked_result(
            before_cpu=before_cpu,
            decoded=decoded,
            status="requires-known-address-register",
            note=(
                f"{base_register_name} must be known before this indexed {mode} can compute "
                "its effective address honestly."
            ),
        )

    destination_index = op & 0x07
    dest_long_name = R32[destination_index]
    dest_field = REG32_FIELDS[destination_index]
    dest_long_value = getattr(before_cpu.regs, dest_field)
    if dest_long_value is None:
        return _blocked_result(
            before_cpu=before_cpu,
            decoded=decoded,
            status="requires-known-full-register",
            note=(
                f"{dest_long_name} must be known before this indexed {mode} can read its "
                "operand half honestly."
            ),
        )

    displacement = _signed_u8(raw[1])
    effective_address = (base_address + displacement) & 0xFFFFFFFF
    data = _read_runtime_bytes(view, before_memory, _mask_address(effective_address), 2)
    if data is None:
        return _blocked_result(
            before_cpu=before_cpu,
            decoded=decoded,
            status="runtime-memory-unavailable",
            note=(
                f"This indexed {mode} needs 2 readable bytes at its effective address, but "
                "neither the writable runtime overlay nor the read bus can provide them."
            ),
        )
    mem_word = int.from_bytes(data, "little")

    return _execute_word_memory_muldiv_common(
        before_cpu=before_cpu,
        before_memory=before_memory,
        decoded=decoded,
        mode=mode,
        destination_index=destination_index,
        destination_value=dest_long_value,
        memory_word=mem_word,
        operand_description=f"mem16(0x{effective_address:06X})",
    )


def _try_execute_indexed_push(
    view: NgpcFetchView,
    before_cpu: NgpcCpuState,
    before_memory: dict[int, int],
    decoded: DecodeResult,
) -> ExecutionResult | None:
    """Execute push/pushw/pushl (r32+d8) — push memory-indexed value onto stack.

    Encoding: [80+zz+mem_r] [d8] [04]
      - 0x88..0x8F + op=0x04 → push  (byte,  1 byte)
      - 0x98..0x9F + op=0x04 → pushw (word,  2 bytes)
      - 0xA8..0xAF + op=0x04 → pushl (long,  4 bytes)

    Catalog: 80 + zz + mem : 04 → (−XSP) ← (mem)
    """
    raw = decoded.raw_bytes
    if raw is None or len(raw) != 3:
        return None

    first = raw[0]
    if 0x88 <= first <= 0x8F:
        width = 1
    elif 0x98 <= first <= 0x9F:
        width = 2
    elif 0xA8 <= first <= 0xAF:
        width = 4
    else:
        return None

    if raw[2] != 0x04:
        return None

    address_register_index = first & 0x07
    address_register_name = R32[address_register_index]
    base_address = getattr(before_cpu.regs, REG32_FIELDS[address_register_index])
    if base_address is None:
        return _blocked_result(
            before_cpu=before_cpu,
            decoded=decoded,
            status="requires-known-address-register",
            note=(
                f"{address_register_name} must be known before this indexed push can compute "
                "its effective address honestly."
            ),
        )

    displacement = _signed_u8(raw[1])
    effective_address = _mask_address((base_address + displacement) & 0xFFFFFFFF)
    data = _read_runtime_bytes(view, before_memory, effective_address, width)
    if data is None:
        return _blocked_result(
            before_cpu=before_cpu,
            decoded=decoded,
            status="runtime-memory-unavailable",
            note=(
                "This indexed push needs readable bytes at its effective address, but neither "
                "the writable runtime overlay nor the current read bus can provide them."
            ),
        )

    return _execute_push_bytes(
        view=view,
        before_cpu=before_cpu,
        before_memory=before_memory,
        decoded=decoded,
        data=data,
        note=(
            "Executed indexed push from the current real execution subset. The effective "
            "address was computed from the known address register plus displacement; the "
            "loaded bytes were pushed onto the stack."
        ),
    )


def _try_execute_post_increment_byte(
    view: NgpcFetchView,
    before_cpu: NgpcCpuState,
    before_memory: dict[int, int],
    decoded: DecodeResult,
) -> ExecutionResult | None:
    raw = decoded.raw_bytes
    if raw is None or len(raw) not in (3, 4, 5) or raw[0] not in (0xC5, 0xD5, 0xE5, 0xF5):
        return None

    address_register_index = _post_increment_r32_index(raw[1])
    address_register_name = R32[address_register_index]
    address_field = REG32_FIELDS[address_register_index]
    base_address = getattr(before_cpu.regs, address_field)
    if base_address is None:
        return _blocked_result(
            before_cpu=before_cpu,
            decoded=decoded,
            status="requires-known-address-register",
            note=(
                f"{address_register_name} must be known before this post-increment memory form "
                "can compute its effective address honestly."
            ),
        )

    target_address = _mask_address(base_address)
    advanced_address = (base_address + 1) & 0xFFFFFFFF

    if raw[0] in (0xC5, 0xD5, 0xE5) and len(raw) == 3 and 0x20 <= raw[2] <= 0x27:
        size_kind = {0xC5: "byte", 0xD5: "word", 0xE5: "long"}[raw[0]]
        width = {"byte": 1, "word": 2, "long": 4}[size_kind]
        destination_index = raw[2] & 0x07
        if size_kind == "byte":
            destination_field = REG32_FIELDS[destination_index // 2]
            destination_name = R8[destination_index]
        else:
            destination_field = REG32_FIELDS[destination_index]
            destination_name = R16[destination_index] if size_kind == "word" else R32[destination_index]
        if destination_field == address_field:
            return _blocked_result(
                before_cpu=before_cpu,
                decoded=decoded,
                status="unmodeled-register-alias-side-effects",
                note=(
                    f"{destination_name} aliases {address_register_name}, and this post-increment "
                    f"{size_kind} load would need alias ordering the current subset does not model yet."
                ),
            )

        data = _read_runtime_bytes(view, before_memory, target_address, width)
        if data is None:
            return _blocked_result(
                before_cpu=before_cpu,
                decoded=decoded,
                status="runtime-memory-unavailable",
                note=(
                    f"This post-increment {size_kind} load needs readable source bytes, but neither "
                    "the writable runtime overlay nor the current read bus can provide them."
                ),
            )

        destination_name, reg_updates = _build_register_update(
            before_cpu=before_cpu,
            size_kind=size_kind,
            register_index=destination_index,
            value=int.from_bytes(data, "little"),
        )
        if reg_updates is None:
            owner_name = R32[destination_index // 2] if size_kind == "byte" else R32[destination_index]
            return _blocked_result(
                before_cpu=before_cpu,
                decoded=decoded,
                status="requires-known-full-register",
                note=(
                    f"{destination_name} cannot be updated honestly until {owner_name} is "
                    "already known in the current CPU state."
                ),
            )

        reg_updates[address_field] = (base_address + width) & 0xFFFFFFFF
        return _executed_result(
            before_cpu=before_cpu,
            decoded=decoded,
            written_registers=(destination_name, address_register_name, "PC"),
            memory_writes=(),
            after_memory=before_memory,
            new_pc=decoded.next_sequential_pc,
            reg_updates=reg_updates,
            note=(
                f"Executed post-increment {size_kind} load from the current real execution subset. "
                "Source bytes were loaded from the readable runtime view and the address register "
                "was advanced after the access."
            ),
        )

    if raw[0] == 0xC5 and len(raw) == 3 and 0x20 <= raw[2] <= 0x27:
        destination_index = raw[2] & 0x07
        destination_field = REG32_FIELDS[destination_index // 2]
        destination_name = R8[destination_index]
        if destination_field == address_field:
            return _blocked_result(
                before_cpu=before_cpu,
                decoded=decoded,
                status="unmodeled-register-alias-side-effects",
                note=(
                    f"{destination_name} aliases {address_register_name}, and this post-increment "
                    "byte load would need alias ordering the current subset does not model yet."
                ),
            )

        data = _read_runtime_bytes(view, before_memory, target_address, 1)
        if data is None:
            return _blocked_result(
                before_cpu=before_cpu,
                decoded=decoded,
                status="runtime-memory-unavailable",
                note=(
                    "This post-increment byte load needs a readable source byte, but neither "
                    "the writable runtime overlay nor the current read bus can provide it."
                ),
            )

        destination_name, reg_updates = _build_register_update(
            before_cpu=before_cpu,
            size_kind="byte",
            register_index=destination_index,
            value=data[0],
        )
        if reg_updates is None:
            owner_name = R32[destination_index // 2]
            return _blocked_result(
                before_cpu=before_cpu,
                decoded=decoded,
                status="requires-known-full-register",
                note=(
                    f"{destination_name} cannot be updated honestly until {owner_name} is "
                    "already known in the current CPU state."
                ),
            )

        reg_updates[address_field] = advanced_address
        return _executed_result(
            before_cpu=before_cpu,
            decoded=decoded,
            written_registers=(destination_name, address_register_name, "PC"),
            memory_writes=(),
            after_memory=before_memory,
            new_pc=decoded.next_sequential_pc,
            reg_updates=reg_updates,
            note=(
                "Executed post-increment byte load from the current real execution subset. One "
                "byte was loaded from the readable runtime view and the address register was "
                "advanced after the access."
            ),
        )

    # C5 byte ALU R8, (r32+) [0x_0] / (r32+), R8 [0x_8] with post-increment.
    # `C5 F0 81` = add A, (XIX+) (Bakumatsu / Last Blade).
    if raw[0] == 0xC5 and len(raw) == 3 and 0x80 <= raw[2] <= 0xFF:
        op_byte = raw[2]
        alu = _SECONDARY_INDEXED_ALU_NAMES[op_byte & 0xF0]
        mem_is_dest = bool(op_byte & 0x08)
        r8_index = op_byte & 0x07
        r8_name, r8_value = _extract_register_value(before_cpu, "byte", r8_index)
        if r8_value is None:
            return _blocked_result(
                before_cpu=before_cpu, decoded=decoded, status="requires-known-full-register",
                note=f"{r8_name} must be known before this post-increment {alu} can execute.",
            )
        needs_carry = alu in ("adc", "sbc")
        if needs_carry and before_cpu.flags.cf is None:
            return _blocked_result(
                before_cpu=before_cpu, decoded=decoded, status="runtime-state-required",
                note=f"{alu.upper()} (r32+) needs a known carry flag.",
            )
        carry = int(before_cpu.flags.cf) if needs_carry else 0
        data = _read_runtime_bytes(view, before_memory, target_address, 1)
        if data is None:
            return _blocked_result(
                before_cpu=before_cpu, decoded=decoded, status="runtime-memory-unavailable",
                note="This post-increment byte ALU needs a readable source byte, unavailable in overlay/bus.",
            )
        mem_value = data[0]
        left, right = (mem_value, r8_value) if mem_is_dest else (r8_value, mem_value)
        if alu in ("add", "adc"):
            result = (left + right + carry) & 0xFF
            flags = _compute_add_flags("byte", left, right + carry)
        elif alu in ("sub", "sbc", "cp"):
            result = (left - right - carry) & 0xFF
            flags = _compute_subtract_flags("byte", left, right + carry)
        elif alu == "and":
            result = left & right
            flags = _compute_logical_flags("byte", result, half_carry=True)
        elif alu == "xor":
            result = left ^ right
            flags = _compute_logical_flags("byte", result)
        else:  # or
            result = left | right
            flags = _compute_logical_flags("byte", result)

        if alu == "cp":  # flags only, but the pointer still post-increments
            return _executed_result(
                before_cpu=before_cpu, decoded=decoded,
                written_registers=(address_register_name, "PC"), memory_writes=(),
                after_memory=before_memory, new_pc=decoded.next_sequential_pc,
                reg_updates={address_field: advanced_address}, flags_updates=flags,
                note=f"Executed cp {r8_name}, ({address_register_name}+); pointer advanced.",
            )
        if mem_is_dest:
            after_memory = dict(before_memory)
            after_memory[target_address] = result
            return _executed_result(
                before_cpu=before_cpu, decoded=decoded,
                written_registers=(address_register_name, "PC"),
                memory_writes=(MemoryWrite(address=target_address, data=bytes([result]),
                                           note=f"{alu.upper()} (r32+), {r8_name} RMW."),),
                after_memory=after_memory, new_pc=decoded.next_sequential_pc,
                reg_updates={address_field: advanced_address}, flags_updates=flags,
                note=f"Executed {alu} ({address_register_name}+), {r8_name}; pointer advanced.",
            )
        if REG32_FIELDS[r8_index // 2] == address_field:
            return _blocked_result(
                before_cpu=before_cpu, decoded=decoded,
                status="unmodeled-register-alias-side-effects",
                note=f"{r8_name} aliases the pointer {address_register_name}; alias ordering not modeled.",
            )
        _, reg_updates = _build_register_update(before_cpu, "byte", r8_index, result)
        if reg_updates is None:
            return _blocked_result(
                before_cpu=before_cpu, decoded=decoded, status="requires-known-full-register",
                note=f"{r8_name} owner must be known to write back this post-increment {alu}.",
            )
        reg_updates[address_field] = advanced_address
        return _executed_result(
            before_cpu=before_cpu, decoded=decoded,
            written_registers=(r8_name, address_register_name, "PC"), memory_writes=(),
            after_memory=before_memory, new_pc=decoded.next_sequential_pc,
            reg_updates=reg_updates, flags_updates=flags,
            note=f"Executed {alu} {r8_name}, ({address_register_name}+) -> 0x{result:02X}; pointer advanced.",
        )

    # C5 mul/muls/div/divs RR, (r32+) [0x40..0x5F] -- 16-bit reg, 8-bit post-inc source.
    # `C5 EC 45` = mul IY, (XHL+) (Dive Alert / Koi Koi / Rockman / ...).
    if raw[0] == 0xC5 and len(raw) == 3 and 0x40 <= raw[2] <= 0x5F:
        op_byte = raw[2]
        mnem = {0x40: "mul", 0x48: "muls", 0x50: "div", 0x58: "divs"}[op_byte & 0xF8]
        # The `RR` code is NOT a register index. Toshiba, <Divide> Note 3 -- which
        # says it governs "DIV RR,r AND DIV RR,(mem)", so it covers this form too.
        # At BYTE size the destination is a WORD register and only the ODD codes
        # name one:  001 = WA   011 = BC   101 = DE   111 = HL.
        # Reading the code as an index sent `mul DE,(XHL+)` into XIY. The byte
        # register-register path further down already gets this right
        # (`(op & 7) >> 1`); this path did not, and the differential gate caught
        # the inconsistency between them.
        dest_code = op_byte & 0x07
        if (dest_code & 1) == 0:
            return _blocked_result(
                before_cpu=before_cpu, decoded=decoded, status="unknown-opcode",
                note=(
                    f"{mnem} destination code {dest_code:03b} names no word register: at "
                    "byte size only the odd codes exist (WA / BC / DE / HL)."
                ),
            )
        r16_index = dest_code >> 1
        r16_name, r16_value = _extract_register_value(before_cpu, "word", r16_index)
        if r16_value is None:
            return _blocked_result(
                before_cpu=before_cpu, decoded=decoded, status="requires-known-full-register",
                note=f"{r16_name} must be known before this post-increment {mnem} can execute.",
            )
        data = _read_runtime_bytes(view, before_memory, target_address, 1)
        if data is None:
            return _blocked_result(
                before_cpu=before_cpu, decoded=decoded, status="runtime-memory-unavailable",
                note="This post-increment mul/div needs a readable source byte, unavailable in overlay/bus.",
            )
        mem_value = data[0]
        if mnem in ("mul", "muls"):
            low = r16_value & 0xFF
            if mnem == "muls":
                a = low - 256 if low >= 128 else low
                m = mem_value - 256 if mem_value >= 128 else mem_value
                result = (a * m) & 0xFFFF
            else:
                result = (low * mem_value) & 0xFFFF
        else:  # div / divs -- 16-bit / 8-bit -> quotient(low8), remainder(high8)
            if mem_value == 0:
                return _blocked_result(
                    before_cpu=before_cpu, decoded=decoded, status="division-by-zero",
                    note=f"{mnem} {r16_name}, ({address_register_name}+) divides by a zero memory byte.",
                )
            if mnem == "divs":
                dividend = r16_value - 0x10000 if r16_value >= 0x8000 else r16_value
                divisor = mem_value - 256 if mem_value >= 128 else mem_value
                quot = int(dividend / divisor)
                rem = dividend - quot * divisor
            else:
                quot = r16_value // mem_value
                rem = r16_value % mem_value
            result = ((rem & 0xFF) << 8) | (quot & 0xFF)
        _, reg_updates = _build_register_update(before_cpu, "word", r16_index, result)
        if reg_updates is None:
            return _blocked_result(
                before_cpu=before_cpu, decoded=decoded, status="requires-known-full-register",
                note=f"{r16_name} owner must be known to write back this post-increment {mnem}.",
            )
        reg_updates[address_field] = advanced_address
        return _executed_result(
            before_cpu=before_cpu, decoded=decoded,
            written_registers=(r16_name, address_register_name, "PC"), memory_writes=(),
            after_memory=before_memory, new_pc=decoded.next_sequential_pc,
            reg_updates=reg_updates,
            note=f"Executed {mnem} {r16_name}, ({address_register_name}+) -> 0x{result:04X}; pointer advanced.",
        )

    # D5 word ALU/cp (r32+), imm16 with post-increment by 2.
    # Puyo Pop frontier `D5 E5 3F FF FF` = cpw (XBC+), 0xFFFF.
    if raw[0] == 0xD5 and len(raw) == 5 and 0x38 <= raw[2] <= 0x3F:
        op_byte = raw[2]
        op_name = {0x38: "add", 0x39: "adc", 0x3A: "sub", 0x3B: "sbc",
                   0x3C: "and", 0x3D: "xor", 0x3E: "or", 0x3F: "cp"}[op_byte]
        needs_carry = op_byte in (0x39, 0x3B)
        if needs_carry and before_cpu.flags.cf is None:
            return _blocked_result(
                before_cpu=before_cpu, decoded=decoded, status="runtime-state-required",
                note=f"{op_name}w (r32+), imm16 needs a known carry flag.",
            )
        carry = int(before_cpu.flags.cf) if needs_carry else 0
        imm = int.from_bytes(raw[3:5], "little")
        data = _read_runtime_bytes(view, before_memory, target_address, 2)
        if data is None:
            return _blocked_result(
                before_cpu=before_cpu, decoded=decoded, status="runtime-memory-unavailable",
                note="This post-increment word ALU-immediate needs 2 readable source bytes, unavailable in overlay/bus.",
            )
        mem_value = int.from_bytes(data, "little")
        word_advanced = (base_address + 2) & 0xFFFFFFFF
        if op_byte in (0x38, 0x39):
            result = (mem_value + imm + carry) & 0xFFFF
            flags = _compute_add_flags("word", mem_value, imm + carry)
        elif op_byte in (0x3A, 0x3B, 0x3F):
            result = (mem_value - imm - carry) & 0xFFFF
            flags = _compute_subtract_flags("word", mem_value, imm + carry)
        elif op_byte == 0x3C:
            result = mem_value & imm
            flags = _compute_logical_flags("word", result, half_carry=True)
        elif op_byte == 0x3D:
            result = mem_value ^ imm
            flags = _compute_logical_flags("word", result)
        else:  # 0x3E or
            result = mem_value | imm
            flags = _compute_logical_flags("word", result)
        if op_byte == 0x3F:  # cpw: flags only, pointer still post-increments
            return _executed_result(
                before_cpu=before_cpu, decoded=decoded,
                written_registers=(address_register_name, "PC"), memory_writes=(),
                after_memory=before_memory, new_pc=decoded.next_sequential_pc,
                reg_updates={address_field: word_advanced}, flags_updates=flags,
                note=f"Executed cpw ({address_register_name}+)=0x{mem_value:04X}, 0x{imm:04X}; pointer +2.",
            )
        new_bytes = result.to_bytes(2, "little")
        after_memory = dict(before_memory)
        after_memory[target_address] = new_bytes[0]
        after_memory[_mask_address(target_address + 1)] = new_bytes[1]
        return _executed_result(
            before_cpu=before_cpu, decoded=decoded,
            written_registers=(address_register_name, "PC"),
            memory_writes=(MemoryWrite(address=target_address, data=new_bytes,
                                       note=f"{op_name.upper()}w (r32+), imm16 word RMW."),),
            after_memory=after_memory, new_pc=decoded.next_sequential_pc,
            reg_updates={address_field: word_advanced}, flags_updates=flags,
            note=f"Executed {op_name}w ({address_register_name}+) 0x{mem_value:04X},0x{imm:04X} -> 0x{result:04X}; pointer +2.",
        )

    if raw[0] == 0xF5 and len(raw) == 3 and 0x40 <= raw[2] <= 0x47:
        source_index = raw[2] & 0x07
        source_field = REG32_FIELDS[source_index // 2]
        source_name, source_value = _extract_register_value(
            before_cpu=before_cpu,
            size_kind="byte",
            register_index=source_index,
        )
        if source_field == address_field:
            return _blocked_result(
                before_cpu=before_cpu,
                decoded=decoded,
                status="unmodeled-register-alias-side-effects",
                note=(
                    f"{source_name} aliases {address_register_name}, and this post-increment "
                    "byte store would need alias ordering the current subset does not model yet."
                ),
            )

        if source_value is None:
            owner_name = R32[source_index // 2]
            return _blocked_result(
                before_cpu=before_cpu,
                decoded=decoded,
                status="requires-known-full-register",
                note=(
                    f"{source_name} cannot be stored honestly until {owner_name} is already "
                    "known in the current CPU state."
                ),
            )

        data = bytes((source_value & 0xFF,))
        write_status, write_note = _check_writable_range(view, target_address, 1)
        if write_status == "write-discarded":
            # Register advances even when the write is discarded (hardware behavior).
            return _executed_result(
                before_cpu=before_cpu,
                decoded=decoded,
                written_registers=(address_register_name, "PC"),
                memory_writes=(
                    MemoryWrite(
                        address=target_address,
                        data=data,
                        note=f"[DISCARDED] {write_note}",
                    ),
                ),
                after_memory=before_memory,
                new_pc=decoded.next_sequential_pc,
                reg_updates={address_field: advanced_address},
                note=(
                    "Post-increment byte store destination was unmapped or read-only; write "
                    "silently discarded. Address register still advanced (open-bus behavior)."
                ),
            )
        if write_status is not None:
            return _blocked_result(
                before_cpu=before_cpu,
                decoded=decoded,
                status=write_status,
                note=write_note,
            )

        after_memory = dict(before_memory)
        after_memory[target_address] = data[0]
        return _executed_result(
            before_cpu=before_cpu,
            decoded=decoded,
            written_registers=(address_register_name, "PC"),
            memory_writes=(
                MemoryWrite(
                    address=target_address,
                    data=data,
                    note="Writable runtime overlay updated by post-increment byte store execution.",
                ),
            ),
            after_memory=after_memory,
            new_pc=decoded.next_sequential_pc,
            reg_updates={address_field: advanced_address},
            note=(
                "Executed post-increment byte store from the current real execution subset. The "
                "source byte was written to the writable runtime overlay and the address "
                "register advanced after the access."
            ),
        )

    if raw[0] == 0xF5 and len(raw) == 3 and 0x50 <= raw[2] <= 0x57:
        source_index = raw[2] & 0x07
        source_field = REG32_FIELDS[source_index]
        source_name, source_value = _extract_register_value(
            before_cpu=before_cpu,
            size_kind="word",
            register_index=source_index,
        )
        if source_field == address_field:
            return _blocked_result(
                before_cpu=before_cpu,
                decoded=decoded,
                status="unmodeled-register-alias-side-effects",
                note=(
                    f"{source_name} aliases {address_register_name}, and this post-increment "
                    "word store would need alias ordering the current subset does not model yet."
                ),
            )

        if source_value is None:
            return _blocked_result(
                before_cpu=before_cpu,
                decoded=decoded,
                status="requires-known-full-register",
                note=(
                    f"{source_name} cannot be stored honestly until its current word value is "
                    "known in the current CPU state."
                ),
            )

        advanced_address_word = (base_address + 2) & 0xFFFFFFFF
        data = (source_value & 0xFFFF).to_bytes(2, "little")
        write_status, write_note = _check_writable_range(view, target_address, 2)
        if write_status == "write-discarded":
            return _executed_result(
                before_cpu=before_cpu,
                decoded=decoded,
                written_registers=(address_register_name, "PC"),
                memory_writes=(
                    MemoryWrite(
                        address=target_address,
                        data=data,
                        note=f"[DISCARDED] {write_note}",
                    ),
                ),
                after_memory=before_memory,
                new_pc=decoded.next_sequential_pc,
                reg_updates={address_field: advanced_address_word},
                note=(
                    "Post-increment word store destination was unmapped or read-only; write "
                    "silently discarded. Address register still advanced by 2 (open-bus behavior)."
                ),
            )
        if write_status is not None:
            return _blocked_result(
                before_cpu=before_cpu,
                decoded=decoded,
                status=write_status,
                note=write_note,
            )

        after_memory = dict(before_memory)
        after_memory[target_address] = data[0]
        after_memory[_mask_address(target_address + 1)] = data[1]
        return _executed_result(
            before_cpu=before_cpu,
            decoded=decoded,
            written_registers=(address_register_name, "PC"),
            memory_writes=(
                MemoryWrite(
                    address=target_address,
                    data=data,
                    note="Writable runtime overlay updated by post-increment word store execution.",
                ),
            ),
            after_memory=after_memory,
            new_pc=decoded.next_sequential_pc,
            reg_updates={address_field: advanced_address_word},
            note=(
                "Executed post-increment word store from the current real execution subset. The "
                "source 16-bit register value was written to the writable runtime overlay and "
                "the address register was advanced by 2 after the access."
            ),
        )

    if raw[0] == 0xF5 and len(raw) == 3 and 0x60 <= raw[2] <= 0x67:
        source_index = raw[2] & 0x07
        source_field = REG32_FIELDS[source_index]
        source_name = R32[source_index]
        source_value = getattr(before_cpu.regs, source_field)
        if source_value is None:
            return _blocked_result(
                before_cpu=before_cpu,
                decoded=decoded,
                status="requires-known-full-register",
                note=(
                    f"{source_name} cannot be stored honestly until its current full value is "
                    "known in the current CPU state."
                ),
            )

        if source_field == address_field:
            return _blocked_result(
                before_cpu=before_cpu,
                decoded=decoded,
                status="unmodeled-register-alias-side-effects",
                note=(
                    f"{source_name} aliases {address_register_name}, and this post-increment "
                    "long store would need alias ordering the current subset does not model yet."
                ),
            )

        advanced_address_long = (base_address + 4) & 0xFFFFFFFF
        data = source_value.to_bytes(4, "little")
        write_status, write_note = _check_writable_range(view, target_address, 4)
        if write_status == "write-discarded":
            return _executed_result(
                before_cpu=before_cpu,
                decoded=decoded,
                written_registers=(address_register_name, "PC"),
                memory_writes=(
                    MemoryWrite(
                        address=target_address,
                        data=data,
                        note=f"[DISCARDED] {write_note}",
                    ),
                ),
                after_memory=before_memory,
                new_pc=decoded.next_sequential_pc,
                reg_updates={address_field: advanced_address_long},
                note=(
                    "Post-increment long store destination was unmapped or read-only; write "
                    "silently discarded. Address register still advanced by 4 (open-bus behavior)."
                ),
            )
        if write_status is not None:
            return _blocked_result(
                before_cpu=before_cpu,
                decoded=decoded,
                status=write_status,
                note=write_note,
            )

        after_memory = dict(before_memory)
        for offset in range(4):
            after_memory[target_address + offset] = data[offset]
        return _executed_result(
            before_cpu=before_cpu,
            decoded=decoded,
            written_registers=(address_register_name, "PC"),
            memory_writes=(
                MemoryWrite(
                    address=target_address,
                    data=data,
                    note="Writable runtime overlay updated by post-increment long store execution.",
                ),
            ),
            after_memory=after_memory,
            new_pc=decoded.next_sequential_pc,
            reg_updates={address_field: advanced_address_long},
            note=(
                "Executed post-increment long store from the current real execution subset. The "
                "source 32-bit register value was written to the writable runtime overlay and "
                "the address register was advanced by 4 after the access."
            ),
        )

    if raw[0] == 0xF5 and len(raw) == 5 and raw[2] == 0x02:
        advanced_address_word = (base_address + 2) & 0xFFFFFFFF
        data = raw[3:5]
        write_status, write_note = _check_writable_range(view, target_address, 2)
        if write_status == "write-discarded":
            return _executed_result(
                before_cpu=before_cpu,
                decoded=decoded,
                written_registers=(address_register_name, "PC"),
                memory_writes=(
                    MemoryWrite(
                        address=target_address,
                        data=data,
                        note=f"[DISCARDED] {write_note}",
                    ),
                ),
                after_memory=before_memory,
                new_pc=decoded.next_sequential_pc,
                reg_updates={address_field: advanced_address_word},
                note=(
                    "Post-increment immediate word store destination was unmapped or read-only; "
                    "write silently discarded. Address register still advanced by 2 (open-bus behavior)."
                ),
            )
        if write_status is not None:
            return _blocked_result(
                before_cpu=before_cpu,
                decoded=decoded,
                status=write_status,
                note=write_note,
            )

        after_memory = dict(before_memory)
        after_memory[target_address] = data[0]
        after_memory[_mask_address(target_address + 1)] = data[1]
        return _executed_result(
            before_cpu=before_cpu,
            decoded=decoded,
            written_registers=(address_register_name, "PC"),
            memory_writes=(
                MemoryWrite(
                    address=target_address,
                    data=data,
                    note="Writable runtime overlay updated by post-increment immediate word store execution.",
                ),
            ),
            after_memory=after_memory,
            new_pc=decoded.next_sequential_pc,
            reg_updates={address_field: advanced_address_word},
            note=(
                "Executed post-increment immediate word store from the current real execution subset. "
                "The decoded 16-bit immediate was written to the writable runtime overlay and the "
                "address register was advanced by 2 after the access."
            ),
        )

    if raw[0] == 0xF5 and len(raw) == 4 and raw[2] == 0x00:
        data = bytes((raw[3],))
        write_status, write_note = _check_writable_range(view, target_address, 1)
        if write_status == "write-discarded":
            return _executed_result(
                before_cpu=before_cpu,
                decoded=decoded,
                written_registers=(address_register_name, "PC"),
                memory_writes=(
                    MemoryWrite(
                        address=target_address,
                        data=data,
                        note=f"[DISCARDED] {write_note}",
                    ),
                ),
                after_memory=before_memory,
                new_pc=decoded.next_sequential_pc,
                reg_updates={address_field: advanced_address},
                note=(
                    "Post-increment immediate byte store destination was unmapped or read-only; "
                    "write silently discarded. Address register still advanced (open-bus behavior)."
                ),
            )
        if write_status is not None:
            return _blocked_result(
                before_cpu=before_cpu,
                decoded=decoded,
                status=write_status,
                note=write_note,
            )

        after_memory = dict(before_memory)
        after_memory[target_address] = raw[3]
        return _executed_result(
            before_cpu=before_cpu,
            decoded=decoded,
            written_registers=(address_register_name, "PC"),
            memory_writes=(
                MemoryWrite(
                    address=target_address,
                    data=data,
                    note="Writable runtime overlay updated by post-increment immediate store execution.",
                ),
            ),
            after_memory=after_memory,
            new_pc=decoded.next_sequential_pc,
            reg_updates={address_field: advanced_address},
            note=(
                "Executed post-increment immediate byte store from the current real execution "
                "subset. The decoded immediate byte was written to the writable runtime overlay "
                "and the address register advanced after the access."
            ),
        )

    return None


def _try_execute_indexed_byte_incdec(
    view: NgpcFetchView,
    before_cpu: NgpcCpuState,
    before_memory: dict[int, int],
    decoded: DecodeResult,
) -> ExecutionResult | None:
    """Execute BYTE indexed `(R32+d8)` INC/DEC (RMW): `8F 08 69` = dec 1,(XSP+8).

    Encoding: [0x88..0x8F] [d8] [0x60..0x6F]. Byte-size mirror of the word form
    in `_try_execute_indexed_word_misc` (0x98..0x9F). Count = op & 0x07 (0 -> 8);
    op >= 0x68 is DEC, else INC. CF is preserved (inc/dec do not touch carry).
    menu_test_project frontier at 0x208a8c.
    """
    raw = decoded.raw_bytes
    if raw is None or len(raw) != 3 or not (0x88 <= raw[0] <= 0x8F):
        return None
    sub_op = raw[2]
    if not (0x60 <= sub_op <= 0x6F):
        return None

    base_index = raw[0] & 0x07
    base_name = R32[base_index]
    base_address = getattr(before_cpu.regs, REG32_FIELDS[base_index])
    if base_address is None:
        return _blocked_result(
            before_cpu=before_cpu,
            decoded=decoded,
            status="requires-known-address-register",
            note=(
                f"{base_name} must be known before this indexed byte inc/dec can compute "
                "its effective address honestly."
            ),
        )

    displacement = _signed_u8(raw[1])
    effective_address = _mask_address((base_address + displacement) & 0xFFFFFFFF)
    mem_data = _read_runtime_bytes(view, before_memory, effective_address, 1)
    if mem_data is None:
        return _blocked_result(
            before_cpu=before_cpu,
            decoded=decoded,
            status="runtime-memory-unavailable",
            note=(
                f"This indexed byte inc/dec needs 1 readable byte at "
                f"({base_name}{'+' if displacement >= 0 else ''}{displacement})="
                f"0x{effective_address:06X}, but neither the writable runtime overlay nor "
                "the current read bus can provide it."
            ),
        )
    mem_value = mem_data[0]

    count = (sub_op & 0x07) or 8
    is_dec = sub_op >= 0x68
    if is_dec:
        result = (mem_value - count) & 0xFF
        flags_updates = dict(_compute_subtract_flags("byte", mem_value, count))
        operation = "dec"
    else:
        result = (mem_value + count) & 0xFF
        flags_updates = dict(_compute_add_flags("byte", mem_value, count))
        operation = "inc"
    flags_updates.pop("cf", None)

    write_status, write_note = _check_writable_range(view, effective_address, 1)
    if write_status == "write-discarded":
        return _executed_result(
            before_cpu=before_cpu,
            decoded=decoded,
            written_registers=("PC",),
            memory_writes=(
                MemoryWrite(address=effective_address, data=bytes([result]), note=f"[DISCARDED] {write_note}"),
            ),
            after_memory=before_memory,
            new_pc=decoded.next_sequential_pc,
            reg_updates=None,
            flags_updates=flags_updates,
            note=(
                f"Indexed byte {operation} destination was unmapped or read-only; write "
                "silently discarded (open-bus behavior — execution continues)."
            ),
        )
    if write_status is not None:
        return _blocked_result(
            before_cpu=before_cpu, decoded=decoded, status=write_status, note=write_note,
        )

    after_memory = dict(before_memory)
    after_memory[effective_address] = result
    return _executed_result(
        before_cpu=before_cpu,
        decoded=decoded,
        written_registers=("PC",),
        memory_writes=(
            MemoryWrite(
                address=effective_address,
                data=bytes([result]),
                note=f"Writable runtime overlay updated by indexed byte {operation.upper()} execution.",
            ),
        ),
        after_memory=after_memory,
        new_pc=decoded.next_sequential_pc,
        reg_updates=None,
        flags_updates=flags_updates,
        note=(
            f"Executed indexed byte {operation}: mem[{base_name}"
            f"{'+' if displacement >= 0 else ''}{displacement}]=0x{mem_value:02X}, "
            f"count={count} -> mem=0x{result:02X}."
        ),
    )


def _try_execute_indexed_word_misc(
    view: NgpcFetchView,
    before_cpu: NgpcCpuState,
    before_memory: dict[int, int],
    decoded: DecodeResult,
) -> ExecutionResult | None:
    """Execute indexed word `(R32+d8)` immediate-ALU and INC/DEC forms."""
    raw = decoded.raw_bytes
    if raw is None or len(raw) not in (3, 5):
        return None
    if not (0x98 <= raw[0] <= 0x9F):
        return None

    sub_op = raw[2]
    is_inc_dec = len(raw) == 3 and 0x60 <= sub_op <= 0x6F
    is_imm_alu = len(raw) == 5 and 0x38 <= sub_op <= 0x3F
    if not (is_inc_dec or is_imm_alu):
        return None

    base_r32_index = raw[0] & 0x07
    base_r32_name = R32[base_r32_index]
    base_address = getattr(before_cpu.regs, REG32_FIELDS[base_r32_index])
    if base_address is None:
        return _blocked_result(
            before_cpu=before_cpu,
            decoded=decoded,
            status="requires-known-address-register",
            note=(
                f"{base_r32_name} must be known before this indexed word form can compute "
                "its effective address honestly."
            ),
        )

    displacement = _signed_u8(raw[1])
    effective_address = _mask_address((base_address + displacement) & 0xFFFFFFFF)
    mem_data = _read_runtime_bytes(view, before_memory, effective_address, 2)
    if mem_data is None:
        return _blocked_result(
            before_cpu=before_cpu,
            decoded=decoded,
            status="runtime-memory-unavailable",
            note=(
                f"This indexed word form needs 2 readable bytes at "
                f"({base_r32_name}{'+' if displacement >= 0 else ''}{displacement})="
                f"0x{effective_address:06X}, but neither the writable runtime overlay nor "
                "the current read bus can provide them."
            ),
        )
    mem_value = int.from_bytes(mem_data, "little")

    if is_inc_dec:
        count = sub_op & 0x07
        if count == 0:
            count = 8
        is_dec = sub_op >= 0x68
        if is_dec:
            result = (mem_value - count) & 0xFFFF
            flags_updates = dict(_compute_subtract_flags("word", mem_value, count))
            operation = "dec"
        else:
            result = (mem_value + count) & 0xFFFF
            flags_updates = dict(_compute_add_flags("word", mem_value, count))
            operation = "inc"
        flags_updates.pop("cf", None)
    else:
        operation = {
            0x38: "add",
            0x39: "adc",
            0x3A: "sub",
            0x3B: "sbc",
            0x3C: "and",
            0x3D: "xor",
            0x3E: "or",
            0x3F: "cp",
        }[sub_op]
        if operation in ("adc", "sbc") and before_cpu.flags.cf is None:
            return _blocked_result(
                before_cpu=before_cpu,
                decoded=decoded,
                status="runtime-state-required",
                note=(
                    f"{operation.upper()} on indexed word memory requires a known carry flag, "
                    "which is not modeled in the current CPU state."
                ),
            )
        carry = int(before_cpu.flags.cf) if operation in ("adc", "sbc") else 0
        imm = int.from_bytes(raw[3:5], "little")
        if operation == "add":
            result = (mem_value + imm) & 0xFFFF
            flags_updates = _compute_add_flags("word", mem_value, imm)
        elif operation == "adc":
            result = (mem_value + imm + carry) & 0xFFFF
            flags_updates = _compute_add_flags("word", mem_value, imm + carry)
        elif operation == "sub":
            result = (mem_value - imm) & 0xFFFF
            flags_updates = _compute_subtract_flags("word", mem_value, imm)
        elif operation == "sbc":
            result = (mem_value - imm - carry) & 0xFFFF
            flags_updates = _compute_subtract_flags("word", mem_value, imm + carry)
        elif operation == "and":
            result = mem_value & imm
            flags_updates = _compute_logical_flags("word", result, half_carry=True)
        elif operation == "xor":
            result = mem_value ^ imm
            flags_updates = _compute_logical_flags("word", result)
        elif operation == "or":
            result = mem_value | imm
            flags_updates = _compute_logical_flags("word", result)
        else:
            result = (mem_value - imm) & 0xFFFF
            flags_updates = _compute_subtract_flags("word", mem_value, imm)
            return _executed_result(
                before_cpu=before_cpu,
                decoded=decoded,
                written_registers=("PC",),
                memory_writes=(),
                after_memory=before_memory,
                new_pc=decoded.next_sequential_pc,
                reg_updates=None,
                flags_updates=flags_updates,
                note=(
                    f"Executed indexed word compare-immediate: mem[{base_r32_name}"
                    f"{'+' if displacement >= 0 else ''}{displacement}]=0x{mem_value:04X} - "
                    f"imm=0x{imm:04X}."
                ),
            )

    result_bytes = result.to_bytes(2, "little")
    write_status, write_note = _check_writable_range(view, effective_address, 2)
    if write_status == "write-discarded":
        return _executed_result(
            before_cpu=before_cpu,
            decoded=decoded,
            written_registers=("PC",),
            memory_writes=(
                MemoryWrite(address=effective_address, data=result_bytes, note=f"[DISCARDED] {write_note}"),
            ),
            after_memory=before_memory,
            new_pc=decoded.next_sequential_pc,
            reg_updates=None,
            flags_updates=flags_updates,
            note=(
                f"Indexed word {operation} destination was unmapped or read-only; write "
                "silently discarded (open-bus behavior — execution continues)."
            ),
        )
    if write_status is not None:
        return _blocked_result(
            before_cpu=before_cpu,
            decoded=decoded,
            status=write_status,
            note=write_note,
        )

    after_memory = dict(before_memory)
    after_memory[effective_address] = result_bytes[0]
    after_memory[_mask_address(effective_address + 1)] = result_bytes[1]
    detail = (
        f"imm=0x{int.from_bytes(raw[3:5], 'little'):04X}"
        if is_imm_alu
        else f"count={8 if (sub_op & 0x07) == 0 else (sub_op & 0x07)}"
    )
    return _executed_result(
        before_cpu=before_cpu,
        decoded=decoded,
        written_registers=("PC",),
        memory_writes=(
            MemoryWrite(
                address=effective_address,
                data=result_bytes,
                note=f"Writable runtime overlay updated by indexed word {operation.upper()} execution.",
            ),
        ),
        after_memory=after_memory,
        new_pc=decoded.next_sequential_pc,
        reg_updates=None,
        flags_updates=flags_updates,
        note=(
            f"Executed indexed word {operation}: mem[{base_r32_name}"
            f"{'+' if displacement >= 0 else ''}{displacement}]=0x{mem_value:04X}, "
            f"{detail} -> mem=0x{result:04X}."
        ),
    )


def _try_execute_indexed_long_misc(
    view: NgpcFetchView,
    before_cpu: NgpcCpuState,
    before_memory: dict[int, int],
    decoded: DecodeResult,
) -> ExecutionResult | None:
    """Execute indexed long `(R32+d8)` immediate-ALU and INC/DEC forms."""
    raw = decoded.raw_bytes
    if raw is None or len(raw) not in (3, 7):
        return None
    if not (0xA8 <= raw[0] <= 0xAF):
        return None

    sub_op = raw[2]
    is_inc_dec = len(raw) == 3 and 0x60 <= sub_op <= 0x6F
    is_imm_alu = len(raw) == 7 and 0x38 <= sub_op <= 0x3F
    if not (is_inc_dec or is_imm_alu):
        return None

    base_r32_index = raw[0] & 0x07
    base_r32_name = R32[base_r32_index]
    base_address = getattr(before_cpu.regs, REG32_FIELDS[base_r32_index])
    if base_address is None:
        return _blocked_result(
            before_cpu=before_cpu,
            decoded=decoded,
            status="requires-known-address-register",
            note=(
                f"{base_r32_name} must be known before this indexed long form can compute "
                "its effective address honestly."
            ),
        )

    displacement = _signed_u8(raw[1])
    effective_address = _mask_address((base_address + displacement) & 0xFFFFFFFF)
    mem_data = _read_runtime_bytes(view, before_memory, effective_address, 4)
    if mem_data is None:
        return _blocked_result(
            before_cpu=before_cpu,
            decoded=decoded,
            status="runtime-memory-unavailable",
            note=(
                f"This indexed long form needs 4 readable bytes at "
                f"({base_r32_name}{'+' if displacement >= 0 else ''}{displacement})="
                f"0x{effective_address:06X}, but neither the writable runtime overlay nor "
                "the current read bus can provide them."
            ),
        )
    mem_value = int.from_bytes(mem_data, "little")

    if is_inc_dec:
        count = sub_op & 0x07
        if count == 0:
            count = 8
        is_dec = sub_op >= 0x68
        if is_dec:
            result = (mem_value - count) & 0xFFFFFFFF
            flags_updates = dict(_compute_subtract_flags("long", mem_value, count))
            operation = "dec"
        else:
            result = (mem_value + count) & 0xFFFFFFFF
            flags_updates = dict(_compute_add_flags("long", mem_value, count))
            operation = "inc"
        flags_updates.pop("cf", None)
    else:
        operation = {
            0x38: "add",
            0x39: "adc",
            0x3A: "sub",
            0x3B: "sbc",
            0x3C: "and",
            0x3D: "xor",
            0x3E: "or",
            0x3F: "cp",
        }[sub_op]
        if operation in ("adc", "sbc") and before_cpu.flags.cf is None:
            return _blocked_result(
                before_cpu=before_cpu,
                decoded=decoded,
                status="runtime-state-required",
                note=(
                    f"{operation.upper()} on indexed long memory requires a known carry flag, "
                    "which is not modeled in the current CPU state."
                ),
            )
        carry = int(before_cpu.flags.cf) if operation in ("adc", "sbc") else 0
        imm = int.from_bytes(raw[3:7], "little")
        if operation == "add":
            result = (mem_value + imm) & 0xFFFFFFFF
            flags_updates = _compute_add_flags("long", mem_value, imm)
        elif operation == "adc":
            result = (mem_value + imm + carry) & 0xFFFFFFFF
            flags_updates = _compute_add_flags("long", mem_value, imm + carry)
        elif operation == "sub":
            result = (mem_value - imm) & 0xFFFFFFFF
            flags_updates = _compute_subtract_flags("long", mem_value, imm)
        elif operation == "sbc":
            result = (mem_value - imm - carry) & 0xFFFFFFFF
            flags_updates = _compute_subtract_flags("long", mem_value, imm + carry)
        elif operation == "and":
            result = mem_value & imm
            flags_updates = _compute_logical_flags("long", result, half_carry=True)
        elif operation == "xor":
            result = mem_value ^ imm
            flags_updates = _compute_logical_flags("long", result)
        elif operation == "or":
            result = mem_value | imm
            flags_updates = _compute_logical_flags("long", result)
        else:
            result = (mem_value - imm) & 0xFFFFFFFF
            flags_updates = _compute_subtract_flags("long", mem_value, imm)
            return _executed_result(
                before_cpu=before_cpu,
                decoded=decoded,
                written_registers=("PC",),
                memory_writes=(),
                after_memory=before_memory,
                new_pc=decoded.next_sequential_pc,
                reg_updates=None,
                flags_updates=flags_updates,
                note=(
                    f"Executed indexed long compare-immediate: mem[{base_r32_name}"
                    f"{'+' if displacement >= 0 else ''}{displacement}]=0x{mem_value:08X} - "
                    f"imm=0x{imm:08X}."
                ),
            )

    result_bytes = result.to_bytes(4, "little")
    write_status, write_note = _check_writable_range(view, effective_address, 4)
    if write_status == "write-discarded":
        return _executed_result(
            before_cpu=before_cpu,
            decoded=decoded,
            written_registers=("PC",),
            memory_writes=(
                MemoryWrite(address=effective_address, data=result_bytes, note=f"[DISCARDED] {write_note}"),
            ),
            after_memory=before_memory,
            new_pc=decoded.next_sequential_pc,
            reg_updates=None,
            flags_updates=flags_updates,
            note=(
                f"Indexed long {operation} destination was unmapped or read-only; write silently "
                "discarded (open-bus behavior - execution continues)."
            ),
        )
    if write_status is not None:
        return _blocked_result(
            before_cpu=before_cpu,
            decoded=decoded,
            status=write_status,
            note=write_note,
        )

    after_memory = dict(before_memory)
    for offset, value in enumerate(result_bytes):
        after_memory[_mask_address(effective_address + offset)] = value

    return _executed_result(
        before_cpu=before_cpu,
        decoded=decoded,
        written_registers=("PC",),
        memory_writes=(
            MemoryWrite(
                address=effective_address,
                data=result_bytes,
                note="Writable runtime overlay updated by indexed long immediate/inc-dec execution.",
            ),
        ),
        after_memory=after_memory,
        new_pc=decoded.next_sequential_pc,
        reg_updates=None,
        flags_updates=flags_updates,
        note=(
            f"Executed indexed long {operation}: mem[{base_r32_name}"
            f"{'+' if displacement >= 0 else ''}{displacement}]=0x{mem_value:08X} -> "
            f"0x{result:08X}."
        ),
    )


def _try_execute_indexed_byte_alu_immediate(
    view: NgpcFetchView,
    before_cpu: NgpcCpuState,
    before_memory: dict[int, int],
    decoded: DecodeResult,
) -> ExecutionResult | None:
    """Execute indexed byte ALU-immediate on `(R32+d8)` memory operands.

    Encoding: `[0x88..0x8F] [d8] [0x38..0x3F] [imm8]` (4 bytes). Sub-op map (per
    ngdis `tlcs900_zz_mem.c`): 0x38 add, 0x39 adc, 0x3A sub, 0x3B sbc, 0x3C and,
    0x3D xor, 0x3E or, 0x3F cp. All write the byte back except cp (flags only).
    ADC/SBC fold the modeled carry into the immediate, matching the `(R32),imm8`
    handler. Dialogue-cart frontier `8D 1F 3C FC` = `and (XIY+31), 0xFC`.
    """
    raw = decoded.raw_bytes
    if raw is None or len(raw) != 4:
        return None
    if not (0x88 <= raw[0] <= 0x8F and 0x38 <= raw[2] <= 0x3F):
        return None

    base_r32_index = raw[0] & 0x07
    base_r32_name = R32[base_r32_index]
    base_address = getattr(before_cpu.regs, REG32_FIELDS[base_r32_index])
    if base_address is None:
        return _blocked_result(
            before_cpu=before_cpu,
            decoded=decoded,
            status="requires-known-address-register",
            note=(
                f"{base_r32_name} must be known before this indexed byte ALU-immediate can "
                "compute its effective address honestly."
            ),
        )

    sub_op = raw[2]
    op_name = {
        0x38: "add", 0x39: "adc", 0x3A: "sub", 0x3B: "sbc",
        0x3C: "and", 0x3D: "xor", 0x3E: "or", 0x3F: "cp",
    }[sub_op]
    needs_carry = sub_op in (0x39, 0x3B)
    if needs_carry and before_cpu.flags.cf is None:
        return _blocked_result(
            before_cpu=before_cpu,
            decoded=decoded,
            status="runtime-state-required",
            note=(
                f"{op_name.upper()} ({base_r32_name}+d8), imm8 requires a known carry flag, "
                "which is not modeled in the current CPU state."
            ),
        )
    carry = int(before_cpu.flags.cf) if needs_carry else 0
    imm8 = raw[3]

    displacement = _signed_u8(raw[1])
    effective_address = _mask_address((base_address + displacement) & 0xFFFFFFFF)
    mem_data = _read_runtime_bytes(view, before_memory, effective_address, 1)
    if mem_data is None:
        return _blocked_result(
            before_cpu=before_cpu,
            decoded=decoded,
            status="runtime-memory-unavailable",
            note=(
                f"This indexed byte ALU-immediate needs a readable byte at "
                f"({base_r32_name}{'+' if displacement >= 0 else ''}{displacement})="
                f"0x{effective_address:06X}, but neither the writable runtime overlay nor "
                "the current read bus can provide it."
            ),
        )
    mem_value = mem_data[0]

    if sub_op in (0x38, 0x39):  # add / adc
        result = (mem_value + imm8 + carry) & 0xFF
        flags_updates = _compute_add_flags("byte", mem_value, imm8 + carry)
    elif sub_op in (0x3A, 0x3B, 0x3F):  # sub / sbc / cp
        result = (mem_value - imm8 - carry) & 0xFF
        flags_updates = _compute_subtract_flags("byte", mem_value, imm8 + carry)
    elif sub_op == 0x3C:  # and
        result = mem_value & imm8
        flags_updates = _compute_logical_flags("byte", result, half_carry=True)
    elif sub_op == 0x3D:  # xor
        result = mem_value ^ imm8
        flags_updates = _compute_logical_flags("byte", result)
    else:  # 0x3E or
        result = mem_value | imm8
        flags_updates = _compute_logical_flags("byte", result)

    ea_note = (
        f"({base_r32_name}{'+' if displacement >= 0 else ''}{displacement})"
        f"=0x{effective_address:06X}"
    )
    if sub_op == 0x3F:  # cp: flags only, no write-back.
        return _executed_result(
            before_cpu=before_cpu,
            decoded=decoded,
            written_registers=("PC",),
            memory_writes=(),
            after_memory=before_memory,
            new_pc=decoded.next_sequential_pc,
            reg_updates=None,
            flags_updates=flags_updates,
            note=f"Executed cp {ea_note}=0x{mem_value:02X}, 0x{imm8:02X}.",
        )

    after_memory = dict(before_memory)
    after_memory[effective_address] = result
    return _executed_result(
        before_cpu=before_cpu,
        decoded=decoded,
        written_registers=("PC",),
        memory_writes=(
            MemoryWrite(
                address=effective_address,
                data=bytes((result,)),
                note=f"{op_name.upper()} ({base_r32_name}+d8), imm8 : mem byte updated.",
            ),
        ),
        after_memory=after_memory,
        new_pc=decoded.next_sequential_pc,
        reg_updates=None,
        flags_updates=flags_updates,
        note=f"Executed {op_name} {ea_note}=0x{mem_value:02X}, 0x{imm8:02X} -> mem=0x{result:02X}.",
    )


def _try_execute_indexed_byte_alu(
    view: NgpcFetchView,
    before_cpu: NgpcCpuState,
    before_memory: dict[int, int],
    decoded: DecodeResult,
) -> ExecutionResult | None:
    """Execute indexed byte ALU on `(R32+d8)` memory operands."""
    raw = decoded.raw_bytes
    if raw is None or len(raw) != 3:
        return None
    if not (0x88 <= raw[0] <= 0x8F and 0x80 <= raw[2] <= 0xFF):
        return None

    base_r32_index = raw[0] & 0x07
    base_r32_name = R32[base_r32_index]
    base_address = getattr(before_cpu.regs, REG32_FIELDS[base_r32_index])
    if base_address is None:
        return _blocked_result(
            before_cpu=before_cpu,
            decoded=decoded,
            status="requires-known-address-register",
            note=(
                f"{base_r32_name} must be known before this indexed byte ALU can compute "
                "its effective address honestly."
            ),
        )

    sub_op = raw[2]
    op_group = sub_op >> 4
    operation = {
        0x8: "add",
        0x9: "adc",
        0xA: "sub",
        0xB: "sbc",
        0xC: "and",
        0xD: "xor",
        0xE: "or",
        0xF: "cp",
    }.get(op_group)
    if operation is None:
        return None
    store_to_memory = bool(sub_op & 0x08)
    register_index = sub_op & 0x07
    register_name, register_value = _extract_register_value(
        before_cpu=before_cpu,
        size_kind="byte",
        register_index=register_index,
    )
    if register_value is None:
        return _blocked_result(
            before_cpu=before_cpu,
            decoded=decoded,
            status="requires-known-source-register",
            note=(
                f"{operation.upper()} on {register_name}/({base_r32_name}+d8) needs "
                f"{register_name} modeled in the current CPU state."
            ),
        )

    if operation in ("adc", "sbc") and before_cpu.flags.cf is None:
        return _blocked_result(
            before_cpu=before_cpu,
            decoded=decoded,
            status="runtime-state-required",
            note=(
                f"{operation.upper()} on indexed byte memory requires a known carry flag, "
                "which is not modeled in the current CPU state."
            ),
        )

    displacement = _signed_u8(raw[1])
    effective_address = _mask_address((base_address + displacement) & 0xFFFFFFFF)
    mem_data = _read_runtime_bytes(view, before_memory, effective_address, 1)
    if mem_data is None:
        return _blocked_result(
            before_cpu=before_cpu,
            decoded=decoded,
            status="runtime-memory-unavailable",
            note=(
                f"This indexed byte ALU needs a readable byte at "
                f"({base_r32_name}{'+' if displacement >= 0 else ''}{displacement})="
                f"0x{effective_address:06X}, but neither the writable runtime overlay nor "
                "the current read bus can provide it."
            ),
        )

    mem_value = mem_data[0]
    carry = int(before_cpu.flags.cf) if operation in ("adc", "sbc") else 0
    register_is_left = not store_to_memory
    if register_is_left:
        left_value = register_value
        right_value = mem_value
    else:
        left_value = mem_value
        right_value = register_value

    if operation == "add":
        result = (left_value + right_value) & 0xFF
        flags_updates = _compute_add_flags("byte", left_value, right_value)
    elif operation == "adc":
        result = (left_value + right_value + carry) & 0xFF
        flags_updates = _compute_add_flags("byte", left_value, right_value + carry)
    elif operation == "sub":
        result = (left_value - right_value) & 0xFF
        flags_updates = _compute_subtract_flags("byte", left_value, right_value)
    elif operation == "sbc":
        result = (left_value - right_value - carry) & 0xFF
        flags_updates = _compute_subtract_flags("byte", left_value, right_value + carry)
    elif operation == "and":
        result = left_value & right_value
        flags_updates = _compute_logical_flags("byte", result, half_carry=True)
    elif operation == "xor":
        result = left_value ^ right_value
        flags_updates = _compute_logical_flags("byte", result)
    elif operation == "or":
        result = left_value | right_value
        flags_updates = _compute_logical_flags("byte", result)
    else:
        result = (left_value - right_value) & 0xFF
        flags_updates = _compute_subtract_flags("byte", left_value, right_value)

    if operation == "cp":
        direction = "register-minus-memory" if register_is_left else "memory-minus-register"
        return _executed_result(
            before_cpu=before_cpu,
            decoded=decoded,
            written_registers=("PC",),
            memory_writes=(),
            after_memory=before_memory,
            new_pc=decoded.next_sequential_pc,
            reg_updates=None,
            flags_updates=flags_updates,
            note=(
                f"Executed indexed byte compare ({direction}) from the current real execution "
                "subset. One byte was read at the effective address and the modeled flag "
                "subset now reflects the subtraction result."
            ),
        )

    if store_to_memory:
        result_bytes = bytes((result,))
        write_status, write_note = _check_writable_range(view, effective_address, 1)
        if write_status == "write-discarded":
            return _executed_result(
                before_cpu=before_cpu,
                decoded=decoded,
                written_registers=("PC",),
                memory_writes=(
                    MemoryWrite(
                        address=effective_address,
                        data=result_bytes,
                        note=f"[DISCARDED] {write_note}",
                    ),
                ),
                after_memory=before_memory,
                new_pc=decoded.next_sequential_pc,
                reg_updates=None,
                note=(
                    "Indexed byte ALU destination was unmapped or read-only; write silently "
                    "discarded (open-bus behavior — execution continues)."
                ),
                flags_updates=flags_updates,
            )
        if write_status is not None:
            return _blocked_result(
                before_cpu=before_cpu,
                decoded=decoded,
                status=write_status,
                note=write_note,
            )

        after_memory = dict(before_memory)
        after_memory[effective_address] = result
        return _executed_result(
            before_cpu=before_cpu,
            decoded=decoded,
            written_registers=("PC",),
            memory_writes=(
                MemoryWrite(
                    address=effective_address,
                    data=result_bytes,
                    note=f"Writable runtime overlay updated by indexed byte {operation.upper()} execution.",
                ),
            ),
            after_memory=after_memory,
            new_pc=decoded.next_sequential_pc,
            reg_updates=None,
            flags_updates=flags_updates,
            note=(
                f"Executed indexed byte {operation}: mem[{base_r32_name}"
                f"{'+' if displacement >= 0 else ''}{displacement}]=0x{mem_value:02X}, "
                f"{register_name}=0x{register_value:02X} -> mem=0x{result:02X}."
            ),
        )

    result_name, reg_updates = _build_register_update(
        before_cpu=before_cpu,
        size_kind="byte",
        register_index=register_index,
        value=result,
    )
    if reg_updates is None:
        return _blocked_result(
            before_cpu=before_cpu,
            decoded=decoded,
            status="requires-known-full-register",
            note=(
                f"{operation.upper()} {register_name}, ({base_r32_name}+d8) needs the owner "
                f"register of {register_name} fully known to write back."
            ),
        )
    return _executed_result(
        before_cpu=before_cpu,
        decoded=decoded,
        written_registers=(result_name, "PC"),
        memory_writes=(),
        after_memory=before_memory,
        new_pc=decoded.next_sequential_pc,
        reg_updates=reg_updates,
        flags_updates=flags_updates,
        note=(
            f"Executed indexed byte {operation}: {register_name}=0x{register_value:02X}, "
            f"mem[{base_r32_name}{'+' if displacement >= 0 else ''}{displacement}]="
            f"0x{mem_value:02X} -> {result_name}=0x{result:02X}."
        ),
    )


def _try_execute_indexed_word_alu(
    view: NgpcFetchView,
    before_cpu: NgpcCpuState,
    before_memory: dict[int, int],
    decoded: DecodeResult,
) -> ExecutionResult | None:
    """Execute indexed word ALU on `(R32+d8)` memory operands.

    Encoding: `[0x98+r_base] [d8] [sub_op]`
      - `0x80..0x87` / `0x88..0x8F` = ADD `R16,(mem)` / `(mem),R16`
      - `0x90..0x97` / `0x98..0x9F` = ADC `R16,(mem)` / `(mem),R16`
      - `0xA0..0xA7` / `0xA8..0xAF` = SUB `R16,(mem)` / `(mem),R16`
      - `0xB0..0xB7` / `0xB8..0xBF` = SBC `R16,(mem)` / `(mem),R16`
      - `0xC0..0xC7` / `0xC8..0xCF` = AND `R16,(mem)` / `(mem),R16`
      - `0xD0..0xD7` / `0xD8..0xDF` = XOR `R16,(mem)` / `(mem),R16`
      - `0xE0..0xE7` / `0xE8..0xEF` = OR  `R16,(mem)` / `(mem),R16`
      - `0xF0..0xF7` / `0xF8..0xFF` = CP  `R16,(mem)` / `(mem),R16`
    """
    raw = decoded.raw_bytes
    if raw is None or len(raw) != 3:
        return None
    if not (0x98 <= raw[0] <= 0x9F and 0x80 <= raw[2] <= 0xFF):
        return None

    base_r32_index = raw[0] & 0x07
    base_r32_name = R32[base_r32_index]
    base_address = getattr(before_cpu.regs, REG32_FIELDS[base_r32_index])
    if base_address is None:
        return _blocked_result(
            before_cpu=before_cpu,
            decoded=decoded,
            status="requires-known-address-register",
            note=(
                f"{base_r32_name} must be known before this indexed word ALU can compute "
                "its effective address honestly."
            ),
        )

    sub_op = raw[2]
    op_group = sub_op >> 4
    operation = {
        0x8: "add",
        0x9: "adc",
        0xA: "sub",
        0xB: "sbc",
        0xC: "and",
        0xD: "xor",
        0xE: "or",
        0xF: "cp",
    }.get(op_group)
    if operation is None:
        return None
    store_to_memory = bool(sub_op & 0x08)
    register_index = sub_op & 0x07
    register_name, register_value = _extract_register_value(
        before_cpu=before_cpu,
        size_kind="word",
        register_index=register_index,
    )
    if register_value is None:
        return _blocked_result(
            before_cpu=before_cpu,
            decoded=decoded,
            status="requires-known-source-register",
            note=(
                f"{operation.upper()} on {register_name}/({base_r32_name}+d8) needs "
                f"{register_name} modeled in the current CPU state."
            ),
        )

    if operation in ("adc", "sbc") and before_cpu.flags.cf is None:
        return _blocked_result(
            before_cpu=before_cpu,
            decoded=decoded,
            status="runtime-state-required",
            note=(
                f"{operation.upper()} on indexed word memory requires a known carry flag, "
                "which is not modeled in the current CPU state."
            ),
        )

    displacement = _signed_u8(raw[1])
    effective_address = _mask_address((base_address + displacement) & 0xFFFFFFFF)
    mem_data = _read_runtime_bytes(view, before_memory, effective_address, 2)
    if mem_data is None:
        return _blocked_result(
            before_cpu=before_cpu,
            decoded=decoded,
            status="runtime-memory-unavailable",
            note=(
                f"This indexed word ALU needs 2 readable bytes at "
                f"({base_r32_name}{'+' if displacement >= 0 else ''}{displacement})="
                f"0x{effective_address:06X}, but neither the writable runtime overlay nor "
                "the current read bus can provide them."
            ),
        )

    mem_value = int.from_bytes(mem_data, "little")
    carry = int(before_cpu.flags.cf) if operation in ("adc", "sbc") else 0
    register_is_left = not store_to_memory
    if register_is_left:
        left_value = register_value
        right_value = mem_value
    else:
        left_value = mem_value
        right_value = register_value

    if operation == "add":
        result = (left_value + right_value) & 0xFFFF
        flags_updates = _compute_add_flags("word", left_value, right_value)
    elif operation == "adc":
        result = (left_value + right_value + carry) & 0xFFFF
        flags_updates = _compute_add_flags("word", left_value, right_value + carry)
    elif operation == "sub":
        result = (left_value - right_value) & 0xFFFF
        flags_updates = _compute_subtract_flags("word", left_value, right_value)
    elif operation == "sbc":
        result = (left_value - right_value - carry) & 0xFFFF
        flags_updates = _compute_subtract_flags("word", left_value, right_value + carry)
    elif operation == "and":
        result = left_value & right_value
        flags_updates = _compute_logical_flags("word", result, half_carry=True)
    elif operation == "xor":
        result = left_value ^ right_value
        flags_updates = _compute_logical_flags("word", result)
    elif operation == "or":
        result = left_value | right_value
        flags_updates = _compute_logical_flags("word", result)
    else:
        result = (left_value - right_value) & 0xFFFF
        flags_updates = _compute_subtract_flags("word", left_value, right_value)

    if operation == "cp":
        direction = "register-minus-memory" if register_is_left else "memory-minus-register"
        return _executed_result(
            before_cpu=before_cpu,
            decoded=decoded,
            written_registers=("PC",),
            memory_writes=(),
            after_memory=before_memory,
            new_pc=decoded.next_sequential_pc,
            reg_updates=None,
            flags_updates=flags_updates,
            note=(
                f"Executed indexed word compare ({direction}) from the current real execution "
                "subset. Two bytes were read at the effective address and the modeled flag "
                "subset now reflects the subtraction result."
            ),
        )

    if store_to_memory:
        result_bytes = result.to_bytes(2, "little")
        write_status, write_note = _check_writable_range(view, effective_address, 2)
        if write_status == "write-discarded":
            return _executed_result(
                before_cpu=before_cpu,
                decoded=decoded,
                written_registers=("PC",),
                memory_writes=(
                    MemoryWrite(
                        address=effective_address,
                        data=result_bytes,
                        note=f"[DISCARDED] {write_note}",
                    ),
                ),
                after_memory=before_memory,
                new_pc=decoded.next_sequential_pc,
                reg_updates=None,
                note=(
                    "Indexed word ALU destination was unmapped or read-only; write silently "
                    "discarded (open-bus behavior — execution continues)."
                ),
                flags_updates=flags_updates,
            )
        if write_status is not None:
            return _blocked_result(
                before_cpu=before_cpu,
                decoded=decoded,
                status=write_status,
                note=write_note,
            )

        after_memory = dict(before_memory)
        after_memory[effective_address] = result_bytes[0]
        after_memory[_mask_address(effective_address + 1)] = result_bytes[1]
        return _executed_result(
            before_cpu=before_cpu,
            decoded=decoded,
            written_registers=("PC",),
            memory_writes=(
                MemoryWrite(
                    address=effective_address,
                    data=result_bytes,
                    note=f"Writable runtime overlay updated by indexed word {operation.upper()} execution.",
                ),
            ),
            after_memory=after_memory,
            new_pc=decoded.next_sequential_pc,
            reg_updates=None,
            flags_updates=flags_updates,
            note=(
                f"Executed indexed word {operation}: mem[{base_r32_name}"
                f"{'+' if displacement >= 0 else ''}{displacement}]=0x{mem_value:04X}, "
                f"{register_name}=0x{register_value:04X} -> mem=0x{result:04X}."
            ),
        )

    result_name, reg_updates = _build_register_update(
        before_cpu=before_cpu,
        size_kind="word",
        register_index=register_index,
        value=result,
    )
    if reg_updates is None:
        return _blocked_result(
            before_cpu=before_cpu,
            decoded=decoded,
            status="requires-known-full-register",
            note=(
                f"{operation.upper()} {register_name}, ({base_r32_name}+d8) needs the owner "
                f"register of {register_name} fully known to write back."
            ),
        )
    return _executed_result(
        before_cpu=before_cpu,
        decoded=decoded,
        written_registers=(result_name, "PC"),
        memory_writes=(),
        after_memory=before_memory,
        new_pc=decoded.next_sequential_pc,
        reg_updates=reg_updates,
        flags_updates=flags_updates,
        note=(
            f"Executed indexed word {operation}: {register_name}=0x{register_value:04X}, "
            f"mem[{base_r32_name}{'+' if displacement >= 0 else ''}{displacement}]="
            f"0x{mem_value:04X} -> {result_name}=0x{result:04X}."
        ),
    )


def _try_execute_indexed_rmw_add(
    view: NgpcFetchView,
    before_cpu: NgpcCpuState,
    before_memory: dict[int, int],
    decoded: DecodeResult,
) -> ExecutionResult | None:
    """Execute indexed memory RMW ADD: add (r32+d8), R32.

    Encoding: [A8+r] [d8] [88+R]  — 3 bytes.
    Reads 4 bytes at (r32+d8), adds source R32, writes result back.
    Catalog: A8+r : 88+R => add (r32+d8), R32.
    """
    raw = decoded.raw_bytes
    if raw is None or len(raw) != 3:
        return None
    if not (0xA8 <= raw[0] <= 0xAF and 0x88 <= raw[2] <= 0x8F):
        return None

    base_r32_index = raw[0] & 0x07
    base_r32_name = R32[base_r32_index]
    base_r32_field = REG32_FIELDS[base_r32_index]
    base_address = getattr(before_cpu.regs, base_r32_field)
    if base_address is None:
        return _blocked_result(
            before_cpu=before_cpu,
            decoded=decoded,
            status="requires-known-address-register",
            note=(
                f"{base_r32_name} must be known before this indexed memory RMW ADD can compute "
                "its effective address honestly."
            ),
        )

    src_r32_index = raw[2] & 0x07
    src_r32_name, src_value = _extract_register_value(
        before_cpu=before_cpu,
        size_kind="long",
        register_index=src_r32_index,
    )
    if src_value is None:
        return _blocked_result(
            before_cpu=before_cpu,
            decoded=decoded,
            status="requires-known-full-register",
            note=(
                f"{src_r32_name} must be known before this indexed memory RMW ADD can read "
                "the addition operand honestly."
            ),
        )

    displacement = _signed_u8(raw[1])
    effective_address = _mask_address((base_address + displacement) & 0xFFFFFFFF)
    mem_data = _read_runtime_bytes(view, before_memory, effective_address, 4)
    if mem_data is None:
        return _blocked_result(
            before_cpu=before_cpu,
            decoded=decoded,
            status="runtime-memory-unavailable",
            note=(
                f"This indexed memory RMW ADD needs 4 readable bytes at "
                f"({base_r32_name}{'+' if displacement >= 0 else ''}{displacement})="
                f"0x{effective_address:06X}, but neither the writable runtime overlay nor "
                "the current read bus can provide them."
            ),
        )

    mem_value = int.from_bytes(mem_data, "little")
    result = (mem_value + src_value) & 0xFFFFFFFF
    result_bytes = result.to_bytes(4, "little")

    write_status, write_note = _check_writable_range(view, effective_address, 4)
    if write_status == "write-discarded":
        return _executed_result(
            before_cpu=before_cpu,
            decoded=decoded,
            written_registers=("PC",),
            memory_writes=(
                MemoryWrite(
                    address=effective_address,
                    data=result_bytes,
                    note=f"[DISCARDED] {write_note}",
                ),
            ),
            after_memory=before_memory,
            new_pc=decoded.next_sequential_pc,
            reg_updates=None,
            note=(
                "Indexed RMW ADD destination was unmapped or read-only; write silently "
                "discarded (open-bus behavior — execution continues)."
            ),
        )
    if write_status is not None:
        return _blocked_result(
            before_cpu=before_cpu,
            decoded=decoded,
            status=write_status,
            note=write_note,
        )

    after_memory = dict(before_memory)
    for offset, value in enumerate(result_bytes):
        after_memory[_mask_address(effective_address + offset)] = value

    flags = _compute_add_flags("long", mem_value, src_value)
    return _executed_result(
        before_cpu=before_cpu,
        decoded=decoded,
        written_registers=("PC",),
        memory_writes=(
            MemoryWrite(
                address=effective_address,
                data=result_bytes,
                note="Writable runtime overlay updated by indexed memory RMW ADD execution.",
            ),
        ),
        after_memory=after_memory,
        new_pc=decoded.next_sequential_pc,
        reg_updates=None,
        flags_updates=flags,
        note=(
            f"Executed indexed memory RMW ADD from the current real execution subset. "
            f"mem[{base_r32_name}{'+' if displacement >= 0 else ''}{displacement}]="
            f"0x{mem_value:08X} + {src_r32_name}=0x{src_value:08X} = "
            f"0x{result:08X} written back to 0x{effective_address:06X}."
        ),
    )


def _try_execute_indexed_long_alu(
    view: NgpcFetchView,
    before_cpu: NgpcCpuState,
    before_memory: dict[int, int],
    decoded: DecodeResult,
) -> ExecutionResult | None:
    """Execute indexed long ALU on `(R32+d8)` memory operands."""
    raw = decoded.raw_bytes
    if raw is None or len(raw) != 3:
        return None
    if not (0xA8 <= raw[0] <= 0xAF and 0x80 <= raw[2] <= 0xFF):
        return None

    sub_op = raw[2]
    operation = {
        0x8: "add",
        0x9: "adc",
        0xA: "sub",
        0xB: "sbc",
        0xC: "and",
        0xD: "xor",
        0xE: "or",
        0xF: "cp",
    }.get(sub_op >> 4)
    if operation is None:
        return None

    base_r32_index = raw[0] & 0x07
    base_r32_name = R32[base_r32_index]
    base_address = getattr(before_cpu.regs, REG32_FIELDS[base_r32_index])
    if base_address is None:
        return _blocked_result(
            before_cpu=before_cpu,
            decoded=decoded,
            status="requires-known-address-register",
            note=(
                f"{base_r32_name} must be known before this indexed long ALU can compute "
                "its effective address honestly."
            ),
        )

    register_index = sub_op & 0x07
    register_name, register_value = _extract_register_value(
        before_cpu=before_cpu,
        size_kind="long",
        register_index=register_index,
    )
    if register_value is None:
        return _blocked_result(
            before_cpu=before_cpu,
            decoded=decoded,
            status="requires-known-full-register",
            note=(
                f"{operation.upper()} on {register_name}/({base_r32_name}+d8) needs "
                f"{register_name} modeled in the current CPU state."
            ),
        )

    if operation in ("adc", "sbc") and before_cpu.flags.cf is None:
        return _blocked_result(
            before_cpu=before_cpu,
            decoded=decoded,
            status="runtime-state-required",
            note=(
                f"{operation.upper()} on indexed long memory requires a known carry flag, "
                "which is not modeled in the current CPU state."
            ),
        )

    displacement = _signed_u8(raw[1])
    effective_address = _mask_address((base_address + displacement) & 0xFFFFFFFF)
    mem_data = _read_runtime_bytes(view, before_memory, effective_address, 4)
    if mem_data is None:
        return _blocked_result(
            before_cpu=before_cpu,
            decoded=decoded,
            status="runtime-memory-unavailable",
            note=(
                f"This indexed long ALU needs 4 readable byte(s) at "
                f"({base_r32_name}{'+' if displacement >= 0 else ''}{displacement})="
                f"0x{effective_address:06X}, but neither the writable runtime overlay nor "
                "the current read bus can provide them."
            ),
        )

    mem_value = int.from_bytes(mem_data, "little")
    carry = int(before_cpu.flags.cf) if operation in ("adc", "sbc") else 0
    register_is_left = not bool(sub_op & 0x08)
    if register_is_left:
        left_value = register_value
        right_value = mem_value
    else:
        left_value = mem_value
        right_value = register_value

    if operation == "add":
        result = (left_value + right_value) & 0xFFFFFFFF
        flags_updates = _compute_add_flags("long", left_value, right_value)
    elif operation == "adc":
        result = (left_value + right_value + carry) & 0xFFFFFFFF
        flags_updates = _compute_add_flags("long", left_value, right_value + carry)
    elif operation == "sub":
        result = (left_value - right_value) & 0xFFFFFFFF
        flags_updates = _compute_subtract_flags("long", left_value, right_value)
    elif operation == "sbc":
        result = (left_value - right_value - carry) & 0xFFFFFFFF
        flags_updates = _compute_subtract_flags("long", left_value, right_value + carry)
    elif operation == "and":
        result = left_value & right_value
        flags_updates = _compute_logical_flags("long", result, half_carry=True)
    elif operation == "xor":
        result = left_value ^ right_value
        flags_updates = _compute_logical_flags("long", result)
    elif operation == "or":
        result = left_value | right_value
        flags_updates = _compute_logical_flags("long", result)
    else:
        result = (left_value - right_value) & 0xFFFFFFFF
        flags_updates = _compute_subtract_flags("long", left_value, right_value)

    if operation == "cp":
        direction = "register-minus-memory" if register_is_left else "memory-minus-register"
        return _executed_result(
            before_cpu=before_cpu,
            decoded=decoded,
            written_registers=("PC",),
            memory_writes=(),
            after_memory=before_memory,
            new_pc=decoded.next_sequential_pc,
            reg_updates=None,
            flags_updates=flags_updates,
            note=(
                f"Executed indexed long compare ({direction}) from the current real execution "
                "subset. Four bytes were read at the effective address and the modeled flag "
                "subset now reflects the subtraction result."
            ),
        )

    if not register_is_left:
        result_bytes = result.to_bytes(4, "little")
        write_status, write_note = _check_writable_range(view, effective_address, 4)
        if write_status == "write-discarded":
            return _executed_result(
                before_cpu=before_cpu,
                decoded=decoded,
                written_registers=("PC",),
                memory_writes=(
                    MemoryWrite(address=effective_address, data=result_bytes, note=f"[DISCARDED] {write_note}"),
                ),
                after_memory=before_memory,
                new_pc=decoded.next_sequential_pc,
                reg_updates=None,
                flags_updates=flags_updates,
                note=(
                    "Indexed long ALU destination was unmapped or read-only; write silently "
                    "discarded (open-bus behavior - execution continues)."
                ),
            )
        if write_status is not None:
            return _blocked_result(
                before_cpu=before_cpu,
                decoded=decoded,
                status=write_status,
                note=write_note,
            )

        after_memory = dict(before_memory)
        for offset, value in enumerate(result_bytes):
            after_memory[_mask_address(effective_address + offset)] = value
        return _executed_result(
            before_cpu=before_cpu,
            decoded=decoded,
            written_registers=("PC",),
            memory_writes=(
                MemoryWrite(
                    address=effective_address,
                    data=result_bytes,
                    note=f"Writable runtime overlay updated by indexed long {operation.upper()} execution.",
                ),
            ),
            after_memory=after_memory,
            new_pc=decoded.next_sequential_pc,
            reg_updates=None,
            flags_updates=flags_updates,
            note=(
                f"Executed indexed long {operation}: mem[{base_r32_name}"
                f"{'+' if displacement >= 0 else ''}{displacement}]=0x{mem_value:08X}, "
                f"{register_name}=0x{register_value:08X} -> mem=0x{result:08X}."
            ),
        )

    result_name, reg_updates = _build_register_update(
        before_cpu=before_cpu,
        size_kind="long",
        register_index=register_index,
        value=result,
    )
    if reg_updates is None:
        return _blocked_result(
            before_cpu=before_cpu,
            decoded=decoded,
            status="requires-known-full-register",
            note=(
                f"{operation.upper()} {register_name}, ({base_r32_name}+d8) needs the owner "
                f"register of {register_name} fully known to write back."
            ),
        )
    return _executed_result(
        before_cpu=before_cpu,
        decoded=decoded,
        written_registers=(result_name, "PC"),
        memory_writes=(),
        after_memory=before_memory,
        new_pc=decoded.next_sequential_pc,
        reg_updates=reg_updates,
        flags_updates=flags_updates,
        note=(
            f"Executed indexed long {operation}: {register_name}=0x{register_value:08X}, "
            f"mem[{base_r32_name}{'+' if displacement >= 0 else ''}{displacement}]="
            f"0x{mem_value:08X} -> {result_name}=0x{result:08X}."
        ),
    )


def _try_execute_indexed_compare(
    view: NgpcFetchView,
    before_cpu: NgpcCpuState,
    before_memory: dict[int, int],
    decoded: DecodeResult,
) -> ExecutionResult | None:
    raw = decoded.raw_bytes
    if raw is None or len(raw) != 3:
        return None

    first = raw[0]
    # F8..FF = cp (r32+d8), R32 — memory is left operand; flags = mem - R32
    # F0..F7 = cp R32, (r32+d8) — register is left operand; flags = R32 - mem
    if not (0xA8 <= first <= 0xAF and (0xF0 <= raw[2] <= 0xFF)):
        return None

    r32_is_left = 0xF0 <= raw[2] <= 0xF7  # cp R32, (mem): R32 - mem
    reg_index = raw[2] & 0x07

    address_register_index = first & 0x07
    address_register_name = R32[address_register_index]
    base_address = getattr(before_cpu.regs, REG32_FIELDS[address_register_index])
    if base_address is None:
        return _blocked_result(
            before_cpu=before_cpu,
            decoded=decoded,
            status="requires-known-address-register",
            note=(
                f"{address_register_name} must be known before this indexed compare can compute "
                "its effective address honestly."
            ),
        )

    reg_name, reg_value = _extract_register_value(
        before_cpu=before_cpu,
        size_kind="long",
        register_index=reg_index,
    )
    if reg_value is None:
        return _blocked_result(
            before_cpu=before_cpu,
            decoded=decoded,
            status="requires-known-full-register",
            note=(
                f"{reg_name} cannot be compared honestly until its current full value is "
                "known in the CPU state."
            ),
        )

    effective_address = _mask_address((base_address + _signed_u8(raw[1])) & 0xFFFFFFFF)
    data = _read_runtime_bytes(view, before_memory, effective_address, 4)
    if data is None:
        return _blocked_result(
            before_cpu=before_cpu,
            decoded=decoded,
            status="runtime-memory-unavailable",
            note=(
                "This indexed compare needs readable bytes at its effective address, but "
                "neither the writable runtime overlay nor the current read bus can provide them."
            ),
        )

    mem_value = int.from_bytes(data, "little")
    if r32_is_left:
        # cp R32, (r32+d8): flags = R32 - mem
        flags_updates = _compute_subtract_flags("long", reg_value, mem_value)
        direction_note = "register-minus-memory"
    else:
        # cp (r32+d8), R32: flags = mem - R32
        flags_updates = _compute_subtract_flags("long", mem_value, reg_value)
        direction_note = "memory-minus-register"
    return _executed_result(
        before_cpu=before_cpu,
        decoded=decoded,
        written_registers=("PC",),
        memory_writes=(),
        after_memory=before_memory,
        new_pc=decoded.next_sequential_pc,
        reg_updates=None,
        flags_updates=flags_updates,
        note=(
            f"Executed indexed memory compare ({direction_note}) from the current real execution "
            "subset. Four bytes were read at the effective address and the modeled flag subset "
            "now reflects the subtraction result."
        ),
    )


def _try_execute_indexed_cp_immediate(
    view: NgpcFetchView,
    before_cpu: NgpcCpuState,
    before_memory: dict[int, int],
    decoded: DecodeResult,
) -> ExecutionResult | None:
    """Execute indexed memory compare with immediate: cp (r32+d8), imm.

    Encoding:
      [88+r] [d8] [3F] [imm8]          — 4 bytes, byte size
      [98+r] [d8] [3F] [lo] [hi]       — 5 bytes, word size
      [A8+r] [d8] [3F] [b0..b3]        — 7 bytes, long size
    Flags = mem - imm (subtract flags).
    """
    raw = decoded.raw_bytes
    if raw is None or len(raw) < 4:
        return None

    first = raw[0]
    if raw[2] != 0x3F:
        return None

    if 0x88 <= first <= 0x8F and len(raw) == 4:
        size_kind = "byte"
        width = 1
        imm = raw[3]
    elif 0x98 <= first <= 0x9F and len(raw) == 5:
        size_kind = "word"
        width = 2
        imm = int.from_bytes(raw[3:5], "little")
    elif 0xA8 <= first <= 0xAF and len(raw) == 7:
        size_kind = "long"
        width = 4
        imm = int.from_bytes(raw[3:7], "little")
    else:
        return None

    base_r32_index = first & 0x07
    base_r32_name = R32[base_r32_index]
    base_address = getattr(before_cpu.regs, REG32_FIELDS[base_r32_index])
    if base_address is None:
        return _blocked_result(
            before_cpu=before_cpu,
            decoded=decoded,
            status="requires-known-address-register",
            note=(
                f"{base_r32_name} must be known before this indexed compare-immediate can "
                "compute its effective address honestly."
            ),
        )

    displacement = _signed_u8(raw[1])
    effective_address = _mask_address((base_address + displacement) & 0xFFFFFFFF)
    mem_data = _read_runtime_bytes(view, before_memory, effective_address, width)
    if mem_data is None:
        return _blocked_result(
            before_cpu=before_cpu,
            decoded=decoded,
            status="runtime-memory-unavailable",
            note=(
                f"This indexed compare-immediate needs {width} readable byte(s) at "
                f"({base_r32_name}{'+' if displacement >= 0 else ''}{displacement})="
                f"0x{effective_address:06X}, but neither the writable runtime overlay nor "
                "the current read bus can provide them."
            ),
        )

    mem_value = int.from_bytes(mem_data, "little")
    flags_updates = _compute_subtract_flags(size_kind, mem_value, imm)
    return _executed_result(
        before_cpu=before_cpu,
        decoded=decoded,
        written_registers=("PC",),
        memory_writes=(),
        after_memory=before_memory,
        new_pc=decoded.next_sequential_pc,
        reg_updates=None,
        flags_updates=flags_updates,
        note=(
            f"Executed indexed compare-immediate from the current real execution subset. "
            f"mem[{base_r32_name}{'+' if displacement >= 0 else ''}{displacement}]="
            f"0x{mem_value:0{width*2}X} vs imm=0x{imm:0{width*2}X}, flags reflect subtraction."
        ),
    )


def _try_execute_prefixed_register_muldiv(
    before_cpu: NgpcCpuState,
    before_memory: dict[int, int],
    decoded: DecodeResult,
) -> ExecutionResult | None:
    """Prefixed register-register MUL/MULS/DIV/DIVS (word), sub-op 0x40..0x5F.

    Encoding (2 bytes): [D8..DF] [op]
      op & 0xF8: 0x40=mul, 0x48=muls, 0x50=div, 0x58=divs.
      Source register (16-bit multiplier / divisor) = the D8..DF prefix.
      Destination = op & 0x07: the full 32-bit register that holds the
      dividend (div) or receives the 32-bit product (mul); div packs the
      quotient into its low word and the remainder into its high word.

    HW-CLEARED 2026-07-06 (hw_test_muldiv, `div WA,BC` = D9 50: XWA
    0x000003E8 / BC 0x000A -> XWA 0x00000064, quotient 100 / remainder 0).
    The mul/div r+r pocket is NOT silicon-broken; this is the executor half
    of the quirks_db reclassification (safe_second_ranges now include
    0x40..0x5F). Reuses `_execute_word_memory_muldiv_common` with the source
    register value standing in for the memory operand.
    """
    raw = decoded.raw_bytes
    if raw is None or len(raw) != 2:
        return None

    info = _prefixed_register_execute_info(raw[0])
    if info is None:
        return None
    size_kind, src_index = info
    if size_kind != "word":
        # mul/div r+r is the D8..DF working-bank WORD prefix family only.
        return None

    op = raw[1]
    if not (0x40 <= op <= 0x5F):
        return None
    mode = {0x40: "mul", 0x48: "muls", 0x50: "div", 0x58: "divs"}.get(op & 0xF8)
    if mode is None:
        return None

    src_name, src_value = _extract_register_value(before_cpu, "word", src_index)
    if src_value is None:
        return _blocked_result(
            before_cpu=before_cpu,
            decoded=decoded,
            status="requires-known-full-register",
            note=(
                f"{src_name} must be known before this register-register {mode} can be "
                "executed honestly."
            ),
        )

    dest_index = op & 0x07
    dest_field = REG32_FIELDS[dest_index]
    dest_value = getattr(before_cpu.regs, dest_field)
    if dest_value is None:
        return _blocked_result(
            before_cpu=before_cpu,
            decoded=decoded,
            status="requires-known-full-register",
            note=(
                f"{R32[dest_index]} must be known before this register-register {mode} can "
                "read its 32-bit operand half honestly."
            ),
        )

    return _execute_word_memory_muldiv_common(
        before_cpu=before_cpu,
        before_memory=before_memory,
        decoded=decoded,
        mode=mode,
        destination_index=dest_index,
        destination_value=dest_value,
        memory_word=src_value & 0xFFFF,
        operand_description=src_name,
    )


def _try_execute_prefixed_byte_muldiv(
    before_cpu: NgpcCpuState,
    before_memory: dict[int, int],
    decoded: DecodeResult,
) -> ExecutionResult | None:
    """C8..CF BYTE MUL/MULS/DIV/DIVS r+r (2 bytes).

    Encoding: [C8..CF] [0x40..0x5F]. The source 8-bit register is the C8..CF
    prefix low nibble; the destination is the r16 pair holding r8 (op & 0x07).
      mul/muls: 8x8 -> 16-bit product into the pair.
      div/divs: dividend = the 16-bit pair, divisor = the 8-bit source,
                quotient -> low byte, remainder -> high byte of the pair.

    HW-CLEARED 2026-07-08 (hw_test_bytediv): `div A, C` (CB 51) with WA=0x1F64,
    C=0x64 -> WA=0x2450 = remainder 0x24 (high) | quotient 0x50 (low). NOT
    silicon-broken, even though the sibling `add A, C` (CB 81) IS a HW crash --
    the CB C-source family is sub-op-specific. div is the HW-tested representative;
    mul/muls/divs inferred safe by the same logic used for the word pocket. This
    unblocks the byte mul/div runtime helper shared by shmup_demo / mr_robot /
    a_test_battle.
    """
    raw = decoded.raw_bytes
    if raw is None or len(raw) != 2 or not (0xC8 <= raw[0] <= 0xCF):
        return None
    op = raw[1]
    if not (0x40 <= op <= 0x5F):
        return None
    mode = {0x40: "mul", 0x48: "muls", 0x50: "div", 0x58: "divs"}.get(op & 0xF8)
    if mode is None:
        return None

    src_index = raw[0] & 0x07
    src_name, src_value = _extract_register_value(before_cpu, "byte", src_index)
    if src_value is None:
        src_value = _extract_current_banked_r8_value(before_cpu, src_index)
    if src_value is None:
        return _blocked_result(
            before_cpu=before_cpu, decoded=decoded, status="requires-known-full-register",
            note=f"{src_name}'s owning register must be known before this byte {mode}.",
        )
    src_value &= 0xFF

    dest_r16_index = (op & 0x07) >> 1  # r8 (op&7) -> its r16 pair
    dest_field = REG32_FIELDS[dest_r16_index]
    dest_r32 = getattr(before_cpu.regs, dest_field)
    if dest_r32 is None:
        return _blocked_result(
            before_cpu=before_cpu, decoded=decoded, status="requires-known-full-register",
            note=f"{R32[dest_r16_index]} must be known before this byte {mode}.",
        )
    dest_16 = dest_r32 & 0xFFFF

    def _s8(v: int) -> int:
        return v - 0x100 if v & 0x80 else v

    def _s16(v: int) -> int:
        return v - 0x10000 if v & 0x8000 else v

    # FLAGS. Toshiba gives <Multiply> the symbol row `- - - - - -` -- it writes NO
    # FLAG AT ALL -- and <Divide> the row `- - - V - -`: V ONLY, and V means "the
    # divisor was zero or the quotient does not fit". Publishing S, Z and C from
    # the product, as this path did, invents three flags the silicon never touches;
    # a `mul` followed by `jr Z` would then branch on a value the real CPU still
    # holds from whatever ran before it. The same defect was fixed in two other
    # MUL/DIV paths already; the differential gate found this third one.
    if mode == "mul":
        result_16 = ((dest_16 & 0xFF) * src_value) & 0xFFFF
        flags = None
    elif mode == "muls":
        result_16 = (_s8(dest_16 & 0xFF) * _s8(src_value)) & 0xFFFF
        flags = None
    else:  # div / divs
        if src_value == 0:
            return _blocked_result(
                before_cpu=before_cpu, decoded=decoded, status="division-by-zero",
                note=f"byte {mode.upper()} by zero: TLCS-900/H sets VF and the packed result is undefined.",
            )
        if mode == "div":
            quot = dest_16 // src_value
            rem = dest_16 % src_value
            overflow = quot > 0xFF
        else:
            sd, sv = _s16(dest_16), _s8(src_value)
            quot = int(sd / sv)
            rem = sd - quot * sv
            overflow = quot < -0x80 or quot > 0x7F
        result_16 = ((rem & 0xFF) << 8) | (quot & 0xFF)
        flags = {"vf": overflow}

    new32 = (dest_r32 & 0xFFFF0000) | (result_16 & 0xFFFF)
    return _executed_result(
        before_cpu=before_cpu, decoded=decoded, written_registers=(R32[dest_r16_index], "PC"),
        memory_writes=(), after_memory=before_memory, new_pc=decoded.next_sequential_pc,
        reg_updates={dest_field: new32}, flags_updates=flags,
        note=f"Executed byte {mode}: {R32[dest_r16_index]}(0x{dest_16:04X}) {mode} {src_name}=0x{src_value:02X} -> 0x{result_16:04X}.",
    )


def _try_execute_prefixed_alu_register(
    before_cpu: NgpcCpuState,
    before_memory: dict[int, int],
    decoded: DecodeResult,
) -> ExecutionResult | None:
    """Prefixed register-register ALU: add/adc/sub/sbc/and/or/xor/cp R, r.

    Catalog encoding (C8+zz+r : op+R):
      0x80 = ADD, 0x90 = ADC, 0xA0 = SUB, 0xB0 = SBC,
      0xC0 = AND, 0xD0 = XOR, 0xE0 = OR,  0xF0 = CP.
    Source register r is identified by the C8+zz+r prefix byte.
    Destination register R is encoded in the op byte (op & 0x07).
    Length: 2 bytes.

    ADC / SBC consume the current carry flag; they block honestly when CF
    is unknown rather than guessing. ADD / OR / etc. emit fresh flags
    (cf is set by ADD/SUB; OR/AND/XOR clear CF/HF and set S/Z based on
    the result). This is what unblocks the cc900/cdecl/adecl crt0 BSS
    init + DataROM copy loops on real builds (CE 90 = `adc W, H`).
    """
    raw = decoded.raw_bytes
    if raw is None or len(raw) != 2:
        return None

    info = _prefixed_register_execute_info(raw[0])
    if info is None:
        return None

    op = raw[1]
    if 0x80 <= op <= 0x87:
        alu_op = "add"
    elif 0x90 <= op <= 0x97:
        alu_op = "adc"
    elif 0xA0 <= op <= 0xA7:
        alu_op = "sub"
    elif 0xB0 <= op <= 0xB7:
        alu_op = "sbc"
    elif 0xC0 <= op <= 0xC7:
        alu_op = "and"
    elif 0xD0 <= op <= 0xD7:
        alu_op = "xor"
    elif 0xE0 <= op <= 0xE7:
        alu_op = "or"
    elif 0xF0 <= op <= 0xF7:
        alu_op = "cp"
    else:
        return None

    size_kind, src_index = info
    dest_index = op & 0x07

    src_name, src_value = _extract_register_value(before_cpu, size_kind, src_index)
    if src_value is None and size_kind == "byte":
        src_value = _extract_current_banked_r8_value(before_cpu, src_index)
    if src_value is None:
        return _blocked_result(
            before_cpu=before_cpu,
            decoded=decoded,
            status="requires-known-full-register",
            note=(
                f"{src_name} must be known before this register-register {alu_op} can be "
                "executed honestly."
            ),
        )

    dest_name, dest_value = _extract_register_value(before_cpu, size_kind, dest_index)
    if dest_value is None and size_kind == "byte":
        dest_value = _extract_current_banked_r8_value(before_cpu, dest_index)
    if dest_value is None:
        return _blocked_result(
            before_cpu=before_cpu,
            decoded=decoded,
            status="requires-known-full-register",
            note=(
                f"{dest_name} must be known before this register-register {alu_op} can be "
                "executed honestly."
            ),
        )

    # ADC / SBC need a known carry flag. Without CF we cannot honestly
    # compute the result — block rather than guess.
    if alu_op in ("adc", "sbc"):
        if before_cpu.flags.cf is None:
            return _blocked_result(
                before_cpu=before_cpu,
                decoded=decoded,
                status="runtime-state-required",
                note=(
                    f"Register-register {alu_op} requires a known carry flag, "
                    "which is not modeled in the current CPU state."
                ),
            )
        carry = int(before_cpu.flags.cf)
    else:
        carry = 0

    bits = {"byte": 8, "word": 16, "long": 32}[size_kind]
    mask = (1 << bits) - 1

    if alu_op == "add":
        result = (dest_value + src_value) & mask
        flags = _compute_add_flags(size_kind, dest_value, src_value)
    elif alu_op == "adc":
        result = (dest_value + src_value + carry) & mask
        flags = _compute_add_flags(size_kind, dest_value, src_value + carry)
    elif alu_op == "sub":
        result = (dest_value - src_value) & mask
        flags = _compute_subtract_flags(size_kind, dest_value, src_value)
    elif alu_op == "sbc":
        result = (dest_value - src_value - carry) & mask
        flags = _compute_subtract_flags(size_kind, dest_value, src_value + carry)
    elif alu_op == "and":
        result = dest_value & src_value
        flags = _compute_logical_flags(size_kind, result, half_carry=True)
    elif alu_op == "xor":
        result = dest_value ^ src_value
        flags = _compute_logical_flags(size_kind, result)
    elif alu_op == "or":
        result = dest_value | src_value
        flags = _compute_logical_flags(size_kind, result)
    else:  # cp
        result = None  # no write-back for cp
        flags = _compute_subtract_flags(size_kind, dest_value, src_value)

    if result is None:
        # cp: flags only, no register write-back
        return _executed_result(
            before_cpu=before_cpu,
            decoded=decoded,
            written_registers=("PC",),
            memory_writes=(),
            after_memory=before_memory,
            new_pc=decoded.next_sequential_pc,
            reg_updates=None,
            flags_updates=flags,
            note=(
                f"Executed register-register cp from the current real execution subset. "
                f"Flags = {dest_name}(0x{dest_value:0{bits//4}X}) - {src_name}(0x{src_value:0{bits//4}X})."
            ),
        )

    extra_cpu_updates = None
    reg_name, reg_updates = _build_register_update(before_cpu, size_kind, dest_index, result)
    if reg_updates is None and size_kind == "byte":
        reg_updates, extra_cpu_updates = _build_current_banked_r8_update(before_cpu, dest_index, result)
        reg_name = R8[dest_index]
    if reg_updates is None and extra_cpu_updates is None:
        return _blocked_result(
            before_cpu=before_cpu,
            decoded=decoded,
            status="requires-known-full-register",
            note=(
                f"The owner register of {dest_name} must be fully known to write back the "
                f"{alu_op} result."
            ),
        )

    symbols = {
        "add": "+",
        "adc": "+ (carry)",
        "sub": "-",
        "sbc": "- (borrow)",
        "and": "&",
        "xor": "^",
        "or": "|",
    }
    symbol = symbols[alu_op]
    return _executed_result(
        before_cpu=before_cpu,
        decoded=decoded,
        written_registers=(reg_name, "PC"),
        memory_writes=(),
        after_memory=before_memory,
        new_pc=decoded.next_sequential_pc,
        reg_updates=reg_updates,
        flags_updates=flags,
        extra_cpu_updates=extra_cpu_updates,
        note=(
            f"Executed register-register {alu_op} from the current real execution subset. "
            f"{dest_name} = {dest_name} {symbol} {src_name} = 0x{result:0{bits//4}X}."
        ),
    )


def _try_execute_prefixed_shift_imm(
    before_cpu: NgpcCpuState,
    before_memory: dict[int, int],
    decoded: DecodeResult,
) -> ExecutionResult | None:
    """Shift/rotate register by 4-bit immediate count.

    Catalog encoding: [C8+zz+r] [E8..EF] [count(#4)]  — 3 bytes.
      E8=RLC, E9=RRC, EA=RL, EB=RR, EC=SLA, ED=SRA, EE=SLL, EF=SRL.
    Count is lower nibble of the 3rd byte (4-bit immediate, range 0..15).
    """
    raw = decoded.raw_bytes
    if raw is None or len(raw) != 3:
        return None

    info = _prefixed_register_execute_info(raw[0])
    if info is None:
        return None

    op = raw[1]
    if not (0xE8 <= op <= 0xEF):
        return None

    size_kind, register_index = info
    # `#4 == 0` means SIXTEEN shifts, not zero. Toshiba instruction list (8),
    # Note 1: "When #4/A is used to specify the number of shifts, module 16
    # (0 to 15) is used. **Code 0 means 16 shifts.**" It applies to BOTH the
    # immediate form and the shift-by-A form. Treating 0 as a no-op made every
    # maximal shift a no-op -- and mis-billed it (the cost is `3 + n/4`, so 7,
    # not 3). Found 2026-07-12 by the C++ differential harness.
    count = (raw[2] & 0x0F) or 16
    return _execute_prefixed_shift_register(
        before_cpu=before_cpu,
        before_memory=before_memory,
        decoded=decoded,
        size_kind=size_kind,
        register_index=register_index,
        op=op,
        count=count,
        count_source=f"imm4=0x{count:X}",
    )

    reg_name, reg_value = _extract_register_value(before_cpu, size_kind, register_index)
    if reg_value is None:
        return _blocked_result(
            before_cpu=before_cpu,
            decoded=decoded,
            status="requires-known-full-register",
            note=(
                f"{reg_name} must be known before this shift/rotate can be executed honestly."
            ),
        )

    bits = {"byte": 8, "word": 16, "long": 32}[size_kind]
    mask = (1 << bits) - 1

    if op == 0xE8:   # RLC — rotate left, MSB → CY and bit 0
        result = ((reg_value << count) | (reg_value >> (bits - count))) & mask if count else reg_value
    elif op == 0xE9:  # RRC — rotate right, LSB → CY and MSB
        result = ((reg_value >> count) | (reg_value << (bits - count))) & mask if count else reg_value
    elif op in (0xEA, 0xEB):  # RL / RR — rotate through carry
        if before_cpu.flags.cf is None:
            return _blocked_result(
                before_cpu=before_cpu,
                decoded=decoded,
                status="requires-known-flags",
                note=(
                    f"{decoded.mnemonic.upper()} rotate-through-carry requires CY, but the "
                    "current CPU state does not know that flag yet."
                ),
            )
        carry_bit = 1 if before_cpu.flags.cf else 0
        result = reg_value
        if op == 0xEA:
            for _ in range(count):
                next_carry = (result >> (bits - 1)) & 1
                result = ((result << 1) & mask) | carry_bit
                carry_bit = next_carry
        else:
            for _ in range(count):
                next_carry = result & 1
                result = ((result >> 1) | (carry_bit << (bits - 1))) & mask
                carry_bit = next_carry
    elif op == 0xEC:  # SLA — shift left arithmetic (same as SLL for positive counts)
        result = (reg_value << count) & mask
    elif op == 0xED:  # SRA — shift right arithmetic (sign-extending)
        sign_bit = (reg_value >> (bits - 1)) & 1
        result = reg_value >> count
        if sign_bit:
            fill = ((1 << count) - 1) << (bits - count)
            result = (result | fill) & mask
    elif op == 0xEE:  # SLL — shift left logical
        result = (reg_value << count) & mask
    else:             # 0xEF: SRL — shift right logical
        result = reg_value >> count

    op_names = {0xE8: "rlc", 0xE9: "rrc", 0xEA: "rl", 0xEB: "rr", 0xEC: "sla", 0xED: "sra", 0xEE: "sll", 0xEF: "srl"}
    op_name = op_names[op]

    # Compute carry (MSB shifted out for left shifts, LSB for right shifts)
    if count:
        if op in (0xEA, 0xEB):
            carry_out = bool(carry_bit)
        elif op in (0xE8, 0xEC, 0xEE):
            carry_out = bool((reg_value >> (bits - count)) & 1)
        else:
            carry_out = bool((reg_value >> (count - 1)) & 1)
    else:
        carry_out = False

    flags = {
        "sf": bool(result >> (bits - 1)),
        "zf": result == 0,
        # V is the PARITY of the result -- Toshiba list (8), row `* * 0 P 0 *`.
        "vf": _has_even_parity(result & ((1 << bits) - 1)),
        "hf": False,  # shift/rotate clear H (Z80/TLCS-900)
        "nf": False,  # shift/rotate clear N
        "cf": carry_out,
    }

    reg_update_name, reg_updates = _build_register_update(before_cpu, size_kind, register_index, result)
    if reg_updates is None:
        return _blocked_result(
            before_cpu=before_cpu,
            decoded=decoded,
            status="requires-known-full-register",
            note=(
                f"The owner register of {reg_name} must be fully known to write back the "
                f"{op_name} result."
            ),
        )

    return _executed_result(
        before_cpu=before_cpu,
        decoded=decoded,
        written_registers=(reg_update_name, "PC"),
        memory_writes=(),
        after_memory=before_memory,
        new_pc=decoded.next_sequential_pc,
        reg_updates=reg_updates,
        flags_updates=flags,
        note=(
            f"Executed {op_name} {count}, {reg_name} from the current real execution subset. "
            f"{reg_name} = 0x{reg_value:0{bits//4}X} << {count} = 0x{result:0{bits//4}X}."
        ),
    )


def _try_execute_prefixed_shift_a(
    before_cpu: NgpcCpuState,
    before_memory: dict[int, int],
    decoded: DecodeResult,
) -> ExecutionResult | None:
    """Shift/rotate register by the current A register low nibble.

    Catalog encoding: [C8+zz+r] [F8..FF]  - 2 bytes.
      F8=RLC, F9=RRC, FA=RL, FB=RR, FC=SLA, FD=SRA, FE=SLL, FF=SRL.
    """
    raw = decoded.raw_bytes
    if raw is None or len(raw) != 2:
        return None

    info = _prefixed_register_execute_info(raw[0])
    if info is None:
        return None

    op = raw[1]
    if not (0xF8 <= op <= 0xFF):
        return None

    _, count_value = _extract_register_value(before_cpu, "byte", 1)
    if count_value is None:
        return _blocked_result(
            before_cpu=before_cpu,
            decoded=decoded,
            status="requires-known-full-register",
            note=(
                "A must be known before this shift-by-A form can read its count honestly."
            ),
        )

    size_kind, register_index = info
    # `#4 == 0` means SIXTEEN shifts, not zero. Toshiba instruction list (8),
    # Note 1: "When #4/A is used to specify the number of shifts, module 16
    # (0 to 15) is used. **Code 0 means 16 shifts.**" It applies to BOTH the
    # immediate form and the shift-by-A form. Treating 0 as a no-op made every
    # maximal shift a no-op -- and mis-billed it (the cost is `3 + n/4`, so 7,
    # not 3). Found 2026-07-12 by the C++ differential harness.
    count = (count_value & 0x0F) or 16
    return _execute_prefixed_shift_register(
        before_cpu=before_cpu,
        before_memory=before_memory,
        decoded=decoded,
        size_kind=size_kind,
        register_index=register_index,
        op=op - 0x10,
        count=count,
        count_source=f"A&0x0F -> {count} shifts",
    )


def _execute_prefixed_shift_register(
    *,
    before_cpu: NgpcCpuState,
    before_memory: dict[int, int],
    decoded: DecodeResult,
    size_kind: str,
    register_index: int,
    op: int,
    count: int,
    count_source: str,
) -> ExecutionResult:
    reg_name, reg_value = _extract_register_value(before_cpu, size_kind, register_index)
    if reg_value is None:
        return _blocked_result(
            before_cpu=before_cpu,
            decoded=decoded,
            status="requires-known-full-register",
            note=(
                f"{reg_name} must be known before this shift/rotate can be executed honestly."
            ),
        )

    shift_eval = _evaluate_shift_rotate(
        before_cpu=before_cpu,
        decoded=decoded,
        size_kind=size_kind,
        reg_value=reg_value,
        op=op,
        count=count,
    )
    if isinstance(shift_eval, ExecutionResult):
        return shift_eval

    result, flags = shift_eval
    op_name = _SHIFT_OP_NAMES[op]
    bits = {"byte": 8, "word": 16, "long": 32}[size_kind]

    reg_update_name, reg_updates = _build_register_update(before_cpu, size_kind, register_index, result)
    if reg_updates is None:
        return _blocked_result(
            before_cpu=before_cpu,
            decoded=decoded,
            status="requires-known-full-register",
            note=(
                f"The owner register of {reg_name} must be fully known to write back the "
                f"{op_name} result."
            ),
        )

    return _executed_result(
        before_cpu=before_cpu,
        decoded=decoded,
        written_registers=(reg_update_name, "PC"),
        memory_writes=(),
        after_memory=before_memory,
        new_pc=decoded.next_sequential_pc,
        reg_updates=reg_updates,
        flags_updates=flags,
        # Cycles are "3 + n/4" -- Toshiba instruction list (8) gives that state to
        # BOTH shift forms, `RLC #4, r` and `RLC A, r`.
        #
        # The byte-level cycle resolver cannot compute it for the A form, because
        # the count lives in a REGISTER, not in the encoding -- so it fell back to
        # a flat SHIFT_REG_A_CYCLES = 2, which is simply the wrong number for any
        # count >= 4. We bill it here instead, where the count is actually known.
        # (The #4 form was already right; passing it explicitly changes nothing
        # there and keeps one formula for both.)
        cycles_consumed=_shift_imm_register_cycles(count),
        note=(
            f"Executed {op_name} on {reg_name} from the current real execution subset using "
            f"{count_source}. {reg_name}: 0x{reg_value:0{bits//4}X} -> 0x{result:0{bits//4}X}."
        ),
    )


_SHIFT_OP_NAMES = {
    0xE8: "rlc", 0xE9: "rrc", 0xEA: "rl", 0xEB: "rr",
    0xEC: "sla", 0xED: "sra", 0xEE: "sll", 0xEF: "srl",
}


def _evaluate_shift_rotate(
    *,
    before_cpu: NgpcCpuState,
    decoded: DecodeResult,
    size_kind: str,
    reg_value: int,
    op: int,
    count: int,
) -> tuple[int, dict[str, bool]] | ExecutionResult:
    bits = {"byte": 8, "word": 16, "long": 32}[size_kind]
    mask = (1 << bits) - 1
    sign_bit = 1 << (bits - 1)

    if op in (0xEA, 0xEB) and before_cpu.flags.cf is None:
        return _blocked_result(
            before_cpu=before_cpu,
            decoded=decoded,
            status="requires-known-flags",
            note=(
                f"{decoded.mnemonic.upper()} rotate-through-carry requires CY, but the "
                "current CPU state does not know that flag yet."
            ),
        )

    # Applied ONE STEP AT A TIME, the way the hardware does it.
    #
    # The previous implementation used closed-form expressions like
    #   (reg_value << count) | (reg_value >> (bits - count))
    # which RAISES `ValueError: negative shift count` as soon as `count > bits` --
    # and `count > bits` is a perfectly legal encoding: the shift immediate is 4
    # bits and `#4 == 0` means SIXTEEN (datasheet Note 1), so `rlc 16, A` on a
    # BYTE register crashed the emulator outright. The carry-out expressions had
    # the same hole. Found 2026-07-12 by the C++ differential harness.
    #
    # A loop is correct for every count, needs no special cases, and is what the
    # silicon actually does.
    carry = bool(before_cpu.flags.cf) if op in (0xEA, 0xEB) else False
    result = reg_value & mask
    for _ in range(count):
        if op == 0xE8:      # RLC
            carry = bool(result & sign_bit)
            result = ((result << 1) | (1 if carry else 0)) & mask
        elif op == 0xE9:    # RRC
            carry = bool(result & 1)
            result = ((result >> 1) | (sign_bit if carry else 0)) & mask
        elif op == 0xEA:    # RL  (through carry)
            next_carry = bool(result & sign_bit)
            result = ((result << 1) | (1 if carry else 0)) & mask
            carry = next_carry
        elif op == 0xEB:    # RR  (through carry)
            next_carry = bool(result & 1)
            result = ((result >> 1) | (sign_bit if carry else 0)) & mask
            carry = next_carry
        elif op in (0xEC, 0xEE):   # SLA / SLL
            carry = bool(result & sign_bit)
            result = (result << 1) & mask
        elif op == 0xED:    # SRA -- arithmetic, sign-extending
            carry = bool(result & 1)
            result = ((result >> 1) | (result & sign_bit)) & mask
        else:               # 0xEF SRL -- logical
            carry = bool(result & 1)
            result = (result >> 1) & mask

    return result, {
        "sf": bool(result & sign_bit),
        "zf": result == 0,
        # V is the PARITY flag. Toshiba instruction list (8) "Rotate and Shift"
        # gives EVERY entry the symbol row `* * 0 P 0 *`. This was previously
        # hard-coded to False with the comment "not modeled".
        "vf": _has_even_parity(result & mask),
        "hf": False,  # `0` in the symbol row
        "nf": False,  # `0` in the symbol row
        "cf": carry,
    }


def _evaluate_cpl_neg(
    *,
    size_kind: str,
    reg_value: int,
    op: int,
) -> tuple[int, dict[str, bool]]:
    bits = {"byte": 8, "word": 16}[size_kind]
    mask = (1 << bits) - 1

    if op == 0x06:
        return (~reg_value) & mask, {
            "hf": True,
            "nf": True,
        }

    result = (-reg_value) & mask
    return result, _compute_subtract_flags(size_kind, 0, reg_value)


def _has_even_parity(value: int) -> bool:
    return (value.bit_count() & 1) == 0


def _evaluate_daa(
    *,
    reg_value: int,
    carry_in: bool,
    half_carry_in: bool,
    subtract_in: bool,
) -> tuple[int, dict[str, bool]]:
    adjust = 0
    if subtract_in:
        if carry_in:
            adjust |= 0x60
        if half_carry_in:
            adjust |= 0x06
        result = (reg_value - adjust) & 0xFF
        half_carry_out = (
            _compute_subtract_flags("byte", reg_value, adjust)["hf"] if adjust else False
        )
        carry_out = carry_in
    else:
        carry_out = carry_in or reg_value > 0x99
        if carry_out:
            adjust |= 0x60
        if half_carry_in or (reg_value & 0x0F) > 0x09:
            adjust |= 0x06
        result = (reg_value + adjust) & 0xFF
        half_carry_out = _compute_add_flags("byte", reg_value, adjust)["hf"] if adjust else False

    return result, {
        "sf": bool(result & 0x80),
        "zf": result == 0,
        "vf": _has_even_parity(result),
        "hf": half_carry_out,
        "cf": carry_out,
        "nf": subtract_in,
    }


def _try_execute_prefixed_cp_imm3(
    before_cpu: NgpcCpuState,
    before_memory: dict[int, int],
    decoded: DecodeResult,
) -> ExecutionResult | None:
    """CP R, imm3 — compare register with 3-bit embedded immediate.

    Catalog encoding: [C8+zz+r] [D8+imm3]  — 2 bytes.
    Immediate = second_byte & 0x07, range 0..7.
    Flags = subtract_flags(reg, imm3). No write-back.
    """
    raw = decoded.raw_bytes
    if raw is None or len(raw) != 2:
        return None

    info = _prefixed_register_execute_info(raw[0])
    if info is None:
        return None

    if not (0xD8 <= raw[1] <= 0xDF):
        return None

    size_kind, register_index = info
    imm3 = raw[1] & 0x07

    reg_name, reg_value = _extract_register_value(before_cpu, size_kind, register_index)
    if reg_value is None:
        owner = R32[register_index] if size_kind == "long" else R32[register_index // 2]
        return _blocked_result(
            before_cpu=before_cpu,
            decoded=decoded,
            status="requires-known-full-register",
            note=(
                f"{reg_name} must be known before this cp-imm3 compare can be executed "
                f"honestly. Owner register {owner} is not yet in the current CPU state."
            ),
        )

    flags_updates = _compute_subtract_flags(size_kind, reg_value, imm3)
    bits = {"byte": 8, "word": 16, "long": 32}[size_kind]
    return _executed_result(
        before_cpu=before_cpu,
        decoded=decoded,
        written_registers=("PC",),
        memory_writes=(),
        after_memory=before_memory,
        new_pc=decoded.next_sequential_pc,
        reg_updates=None,
        flags_updates=flags_updates,
        note=(
            f"Executed prefixed cp imm3 from the current real execution subset. "
            f"Flags = {reg_name}(0x{reg_value:0{bits//4}X}) - {imm3}."
        ),
    )


def _try_execute_prefixed_bit_test(
    before_cpu: NgpcCpuState,
    before_memory: dict[int, int],
    decoded: DecodeResult,
) -> ExecutionResult | None:
    """BIT #4, r for prefixed byte/word/long register families.

    Catalog encoding: [C8+zz+r] [33] [#4].
    Modeled flags per TLCS-900/L1 datasheet:
      - Z = not src<bit>
      - H = 1
      - N = 0
      - S/V/C preserved
    """
    raw = decoded.raw_bytes
    if raw is None or len(raw) != 3:
        return None

    info = _prefixed_register_execute_info(raw[0])
    if info is None or raw[1] != 0x33:
        return None

    size_kind, register_index = info
    bit_index = raw[2] & 0x0F
    bits = {"byte": 8, "word": 16, "long": 32}[size_kind]

    reg_name, reg_value = _extract_register_value(before_cpu, size_kind, register_index)
    if reg_value is None:
        owner = R32[register_index] if size_kind == "long" else R32[register_index // 2]
        return _blocked_result(
            before_cpu=before_cpu,
            decoded=decoded,
            status="requires-known-full-register",
            note=(
                f"{reg_name} must be known before this BIT test can be executed honestly. "
                f"Owner register {owner} is not yet in the current CPU state."
            ),
        )

    if bit_index >= bits:
        return _blocked_result(
            before_cpu=before_cpu,
            decoded=decoded,
            status="silicon-undefined",
            note=(
                f"BIT {bit_index}, {reg_name} is undefined for {size_kind}-sized register "
                "operands on TLCS-900/H."
            ),
        )

    zf = ((reg_value >> bit_index) & 1) == 0
    flags_updates = {
        "zf": zf,
        "hf": True,
        "nf": False,
    }
    return _executed_result(
        before_cpu=before_cpu,
        decoded=decoded,
        written_registers=("PC",),
        memory_writes=(),
        after_memory=before_memory,
        new_pc=decoded.next_sequential_pc,
        reg_updates=None,
        flags_updates=flags_updates,
        note=(
            f"Executed prefixed BIT immediate from the current real execution subset. "
            f"Tested {reg_name} bit {bit_index} from 0x{reg_value:0{bits//4}X}."
        ),
    )


def _try_execute_prefixed_bit_mutation(
    before_cpu: NgpcCpuState,
    before_memory: dict[int, int],
    decoded: DecodeResult,
) -> ExecutionResult | None:
    """RES/SET/CHG/TSET #4, r for prefixed byte/word/long register families."""
    raw = decoded.raw_bytes
    if raw is None or len(raw) != 3:
        return None

    info = _prefixed_register_execute_info(raw[0])
    if info is None or raw[1] not in (0x30, 0x31, 0x32, 0x34):
        return None

    size_kind, register_index = info
    bit_index = raw[2] & 0x0F
    bits = {"byte": 8, "word": 16, "long": 32}[size_kind]
    reg_name, reg_value = _extract_register_value(before_cpu, size_kind, register_index)
    if reg_value is None:
        owner = R32[register_index] if size_kind == "long" else R32[register_index // 2]
        return _blocked_result(
            before_cpu=before_cpu,
            decoded=decoded,
            status="requires-known-full-register",
            note=(
                f"{reg_name} must be known before this prefixed bit mutation can be executed "
                f"honestly. Owner register {owner} is not yet in the current CPU state."
            ),
        )

    if bit_index >= bits:
        return _blocked_result(
            before_cpu=before_cpu,
            decoded=decoded,
            status="silicon-undefined",
            note=(
                f"{decoded.assembly} is undefined for {size_kind}-sized register operands on "
                "TLCS-900/H."
            ),
        )

    bit_mask = 1 << bit_index
    old_bit_set = bool(reg_value & bit_mask)
    op = raw[1]
    if op == 0x30:
        new_value = reg_value & ~bit_mask
        flags_updates = None
    elif op == 0x31:
        new_value = reg_value | bit_mask
        flags_updates = None
    elif op == 0x32:
        new_value = reg_value ^ bit_mask
        flags_updates = None
    else:
        new_value = reg_value | bit_mask
        # TSET writes H = 1 and N = 0 as well (Toshiba symbol row `x * 1 x 0 -`).
        # The register form had the same omission as the memory form; both were
        # surfaced by the C++ differential harness on 2026-07-12.
        flags_updates = {"zf": not old_bit_set, "hf": True, "nf": False}

    result_name, reg_updates = _build_register_update(before_cpu, size_kind, register_index, new_value)
    if reg_updates is None:
        owner = R32[register_index] if size_kind == "long" else R32[register_index // 2]
        return _blocked_result(
            before_cpu=before_cpu,
            decoded=decoded,
            status="requires-known-full-register",
            note=(
                f"{result_name} cannot be updated honestly until owner register {owner} is known "
                "in the current CPU state."
            ),
        )

    return _executed_result(
        before_cpu=before_cpu,
        decoded=decoded,
        written_registers=(result_name, "PC"),
        memory_writes=(),
        after_memory=before_memory,
        new_pc=decoded.next_sequential_pc,
        reg_updates=reg_updates,
        flags_updates=flags_updates,
        note=(
            f"Executed prefixed {decoded.mnemonic.upper()} immediate from the current real "
            f"execution subset. Updated {reg_name} bit {bit_index}."
        ),
    )


def _try_execute_prefixed_alu_immediate(
    before_cpu: NgpcCpuState,
    before_memory: dict[int, int],
    decoded: DecodeResult,
) -> ExecutionResult | None:
    """Prefixed ALU with immediate: add/adc/sub/sbc/and/xor/or/cp r, #.

    Catalog encoding: C8+zz+r : C8..CF : #<size>.
    The destination register is identified by the C8+zz+r prefix byte.
    The operation is identified by the second byte (0xC8..0xCF).
    """
    raw = decoded.raw_bytes
    if raw is None or len(raw) < 3:
        return None

    info = _prefixed_register_execute_info(raw[0])
    if info is None:
        return None

    op = raw[1]
    if op not in (0xC8, 0xC9, 0xCA, 0xCB, 0xCC, 0xCD, 0xCE, 0xCF):
        return None

    size_kind, register_index = info
    if size_kind == "byte":
        if len(raw) != 3:
            return None
        imm = raw[2]
    elif size_kind == "word":
        if len(raw) != 4:
            return None
        imm = int.from_bytes(raw[2:4], "little")
    else:
        if len(raw) != 6:
            return None
        imm = int.from_bytes(raw[2:6], "little")

    reg_name, reg_value = _extract_register_value(before_cpu, size_kind, register_index)
    if reg_value is None:
        owner = R32[register_index]
        return _blocked_result(
            before_cpu=before_cpu,
            decoded=decoded,
            status="requires-known-full-register",
            note=(
                f"{reg_name} cannot be used honestly until {owner} is known in the current "
                "CPU state."
            ),
        )

    # ADC and SBC require carry flag — block if unknown.
    if op in (0xC9, 0xCB):
        if before_cpu.flags.cf is None:
            return _blocked_result(
                before_cpu=before_cpu,
                decoded=decoded,
                status="runtime-state-required",
                note=(
                    "ADC/SBC with immediate requires a known carry flag, which is not modeled "
                    "in the current CPU state."
                ),
            )
        carry = int(before_cpu.flags.cf)
    else:
        carry = 0

    bits = {"byte": 8, "word": 16, "long": 32}[size_kind]
    mask = (1 << bits) - 1

    if op == 0xC8:  # ADD
        result = (reg_value + imm) & mask
        flags = _compute_add_flags(size_kind, reg_value, imm)
        write_back = True
    elif op == 0xC9:  # ADC
        result = (reg_value + imm + carry) & mask
        flags = _compute_add_flags(size_kind, reg_value, imm + carry)
        write_back = True
    elif op == 0xCA:  # SUB
        result = (reg_value - imm) & mask
        flags = _compute_subtract_flags(size_kind, reg_value, imm)
        write_back = True
    elif op == 0xCB:  # SBC
        result = (reg_value - imm - carry) & mask
        flags = _compute_subtract_flags(size_kind, reg_value, imm + carry)
        write_back = True
    elif op == 0xCC:  # AND
        result = (reg_value & imm) & mask
        flags = _compute_logical_flags(size_kind, result, half_carry=True)
        write_back = True
    elif op == 0xCD:  # XOR
        result = (reg_value ^ imm) & mask
        flags = _compute_logical_flags(size_kind, result)
        write_back = True
    elif op == 0xCE:  # OR
        result = (reg_value | imm) & mask
        flags = _compute_logical_flags(size_kind, result)
        write_back = True
    else:  # CP (0xCF)
        result = reg_value
        flags = _compute_subtract_flags(size_kind, reg_value, imm)
        write_back = False

    if write_back:
        _, reg_updates = _build_register_update(before_cpu, size_kind, register_index, result)
        if reg_updates is None:
            return _blocked_result(
                before_cpu=before_cpu,
                decoded=decoded,
                status="requires-known-full-register",
                note=(
                    f"{reg_name} cannot be updated honestly until its owning 32-bit register "
                    "is known in the current CPU state."
                ),
            )
        written = (reg_name, "PC")
    else:
        reg_updates = None
        written = ("PC",)

    op_names = {
        0xC8: "ADD", 0xC9: "ADC", 0xCA: "SUB", 0xCB: "SBC",
        0xCC: "AND", 0xCD: "XOR", 0xCE: "OR", 0xCF: "CP",
    }
    return _executed_result(
        before_cpu=before_cpu,
        decoded=decoded,
        written_registers=written,
        memory_writes=(),
        after_memory=before_memory,
        new_pc=decoded.next_sequential_pc,
        reg_updates=reg_updates,
        flags_updates=flags,
        note=(
            f"Executed prefixed {op_names[op]} immediate from the current real execution subset."
        ),
    )


def _carry_flag_op_info(op_byte: int) -> tuple[str, bool] | None:
    return {
        0x20: ("andcf", True),
        0x21: ("orcf", True),
        0x22: ("xorcf", True),
        0x23: ("ldcf", True),
        0x24: ("stcf", True),
        0x28: ("andcf", False),
        0x29: ("orcf", False),
        0x2A: ("xorcf", False),
        0x2B: ("ldcf", False),
        0x2C: ("stcf", False),
    }.get(op_byte)


def _try_execute_prefixed_carry_flag_register(
    before_cpu: NgpcCpuState,
    before_memory: dict[int, int],
    decoded: DecodeResult,
) -> ExecutionResult | None:
    """Execute prefixed ANDCF/ORCF/XORCF/LDCF/STCF on register operands."""
    raw = decoded.raw_bytes
    if raw is None or len(raw) < 2:
        return None

    info = _prefixed_register_execute_info(raw[0])
    op_info = _carry_flag_op_info(raw[1])
    if info is None or op_info is None:
        return None

    mnemonic, uses_immediate = op_info
    size_kind, register_index = info

    if uses_immediate:
        if len(raw) != 3:
            return None
        bit_index = raw[2] & 0x0F
    else:
        if len(raw) != 2:
            return None
        _, a_value = _extract_register_value(before_cpu, "byte", 1)
        if a_value is None:
            return _blocked_result(
                before_cpu=before_cpu,
                decoded=decoded,
                status="requires-known-full-register",
                note=f"{decoded.assembly} needs A known to derive its dynamic bit index honestly.",
            )
        bit_index = a_value & 0x0F

    if size_kind == "long":
        return _blocked_result(
            before_cpu=before_cpu,
            decoded=decoded,
            status="silicon-undefined",
            note=(
                f"{decoded.assembly} is not defined for long register operands in the local "
                "Toshiba TLCS-900/L1 table."
            ),
        )

    reg_name, reg_value = _extract_register_value(before_cpu, size_kind, register_index)
    if reg_value is None:
        owner_name = R32[register_index if size_kind == "word" else register_index // 2]
        return _blocked_result(
            before_cpu=before_cpu,
            decoded=decoded,
            status="requires-known-full-register",
            note=(
                f"{decoded.assembly} needs {reg_name} known; owner {owner_name} is still "
                "unknown in the current CPU state."
            ),
        )

    bits = 8 if size_kind == "byte" else 16
    if bits == 8 and bit_index >= 8:
        if mnemonic == "stcf":
            return _executed_result(
                before_cpu=before_cpu,
                decoded=decoded,
                written_registers=("PC",),
                memory_writes=(),
                after_memory=before_memory,
                new_pc=decoded.next_sequential_pc,
                reg_updates=None,
                note=(
                    f"Executed {decoded.assembly}: byte STCF with bit index {bit_index} leaves the "
                    "operand unchanged per the local Toshiba note."
                ),
            )
        return _blocked_result(
            before_cpu=before_cpu,
            decoded=decoded,
            status="silicon-undefined",
            note=(
                f"{decoded.assembly} uses byte bit index {bit_index}, which is undefined for this "
                "carry-flag register form in the local Toshiba table."
            ),
        )

    bit_value = (reg_value >> bit_index) & 1

    if mnemonic in {"andcf", "orcf", "xorcf", "stcf"} and before_cpu.flags.cf is None:
        return _blocked_result(
            before_cpu=before_cpu,
            decoded=decoded,
            status="runtime-state-required",
            note=f"{decoded.assembly} needs the carry flag known in the current CPU state.",
        )

    if mnemonic == "ldcf":
        return _executed_result(
            before_cpu=before_cpu,
            decoded=decoded,
            written_registers=("PC",),
            memory_writes=(),
            after_memory=before_memory,
            new_pc=decoded.next_sequential_pc,
            reg_updates=None,
            flags_updates={"cf": bool(bit_value)},
            note=f"Executed {decoded.assembly}: CF <- bit {bit_index} of {reg_name}.",
        )

    carry = int(before_cpu.flags.cf)
    if mnemonic == "andcf":
        return _executed_result(
            before_cpu=before_cpu,
            decoded=decoded,
            written_registers=("PC",),
            memory_writes=(),
            after_memory=before_memory,
            new_pc=decoded.next_sequential_pc,
            reg_updates=None,
            flags_updates={"cf": bool(carry & bit_value)},
            note=f"Executed {decoded.assembly}: CF <- C AND {reg_name}<{bit_index}>.",
        )
    if mnemonic == "orcf":
        return _executed_result(
            before_cpu=before_cpu,
            decoded=decoded,
            written_registers=("PC",),
            memory_writes=(),
            after_memory=before_memory,
            new_pc=decoded.next_sequential_pc,
            reg_updates=None,
            flags_updates={"cf": bool(carry | bit_value)},
            note=f"Executed {decoded.assembly}: CF <- C OR {reg_name}<{bit_index}>.",
        )
    if mnemonic == "xorcf":
        return _executed_result(
            before_cpu=before_cpu,
            decoded=decoded,
            written_registers=("PC",),
            memory_writes=(),
            after_memory=before_memory,
            new_pc=decoded.next_sequential_pc,
            reg_updates=None,
            flags_updates={"cf": bool(carry ^ bit_value)},
            note=f"Executed {decoded.assembly}: CF <- C XOR {reg_name}<{bit_index}>.",
        )

    new_value = (reg_value & ~(1 << bit_index)) | (carry << bit_index)
    reg_update_name, reg_updates = _build_register_update(
        before_cpu, size_kind, register_index, new_value
    )
    if reg_updates is None:
        owner_name = R32[register_index if size_kind == "word" else register_index // 2]
        return _blocked_result(
            before_cpu=before_cpu,
            decoded=decoded,
            status="requires-known-full-register",
            note=(
                f"{decoded.assembly} cannot write back honestly until {owner_name} is already known "
                "in the current CPU state."
            ),
        )
    return _executed_result(
        before_cpu=before_cpu,
        decoded=decoded,
        written_registers=(reg_update_name, "PC"),
        memory_writes=(),
        after_memory=before_memory,
        new_pc=decoded.next_sequential_pc,
        reg_updates=reg_updates,
        note=f"Executed {decoded.assembly}: {reg_name}<{bit_index}> <- CF.",
    )


def _try_execute_prefixed_word_muldiv_immediate(
    before_cpu: NgpcCpuState,
    before_memory: dict[int, int],
    decoded: DecodeResult,
) -> ExecutionResult | None:
    """D8..DF WORD MUL/MULS/DIV/DIVS with a 16-bit immediate operand (4 bytes).

    Encoding: [D8..DF] [0x08|0x09|0x0A|0x0B] [imm16-lo] [imm16-hi]
      0x08=mul, 0x09=muls, 0x0A=div, 0x0B=divs.  `D8 0A 00 50` = div XWA,0x5000;
      `DB 0B 64 00` = divs XHL,0x0064 (menu_test_project frontier).

    The 32-bit extended register (raw[0] & 0x07) is the dividend / multiplicand,
    the imm16 is the divisor / multiplier -- identical semantics to the reg-reg
    mul/div (HW-validated hw_test_muldiv 2026-07-06 + hw_test_muldiv2 2026-07-08:
    remainder in the high word, 32-bit dividend). Reuses the shared word mul/div
    core. The D8..DF-word divide/mul-immediate broke when the prefix was re-sized
    word (2026-07-03) -- the legacy divide-immediate executor only kept the byte
    and long paths; this restores the word path.
    """
    raw = decoded.raw_bytes
    if raw is None or len(raw) != 4 or not (0xD8 <= raw[0] <= 0xDF):
        return None
    mode = {0x08: "mul", 0x09: "muls", 0x0A: "div", 0x0B: "divs"}.get(raw[1])
    if mode is None:
        return None

    dest_index = raw[0] & 0x07
    dest_field = REG32_FIELDS[dest_index]
    dest_value = getattr(before_cpu.regs, dest_field)
    if dest_value is None:
        return _blocked_result(
            before_cpu=before_cpu,
            decoded=decoded,
            status="requires-known-full-register",
            note=(
                f"{R32[dest_index]} must be known before this register-immediate {mode} "
                "can read its 32-bit operand half honestly."
            ),
        )

    imm = int.from_bytes(raw[2:4], "little")
    return _execute_word_memory_muldiv_common(
        before_cpu=before_cpu,
        before_memory=before_memory,
        decoded=decoded,
        mode=mode,
        destination_index=dest_index,
        destination_value=dest_value,
        memory_word=imm,
        operand_description=f"0x{imm:04X}",
    )


def _try_execute_prefixed_divide_immediate(
    before_cpu: NgpcCpuState,
    before_memory: dict[int, int],
    decoded: DecodeResult,
) -> ExecutionResult | None:
    """DIV rr,# / DIV RR,# for the observed prefixed register families.

    Observed and modeled forms:
      - C8..CF : 0A : imm8   -> div R16[r], imm8
      - D8..DF/E8..EF : 0A : imm16 -> div R32[r], imm16

    Result packing follows the TLCS-900/H datasheet:
      - word destination: low byte = quotient, high byte = remainder
      - long destination: low word = quotient, high word = remainder

    Only VF is documented to change; the current subset preserves S/Z/H/N/C.
    Divide-by-zero and quotient overflow leave the destination undefined, so
    those cases are blocked honestly instead of being guessed.
    """
    raw = decoded.raw_bytes
    if raw is None or len(raw) < 3:
        return None

    info = _prefixed_register_execute_info(raw[0])
    if info is None or raw[1] != 0x0A:
        return None

    size_kind, register_index = info

    if size_kind == "byte":
        if len(raw) != 3:
            return None
        # The `rr` code is NOT a register index (Toshiba, <Divide> Note 3, stated a
        # third time for `DIV rr,#`). At BYTE size the destination is a WORD
        # register and only the ODD codes name one:
        #       001 = WA   011 = BC   101 = DE   111 = HL
        # The official assembler agrees and will not emit an even one:
        #       mul WA,7 -> C9 08 07     mul DE,7 -> CD 08 07
        # Indexing straight into R16 sent `mul DE,7` into XIY. The native core made
        # the SAME mistake, so the differential gate saw two cores agreeing on a
        # wrong answer -- only the assembler could tell them apart.
        if (register_index & 1) == 0:
            return _blocked_result(
                before_cpu=before_cpu, decoded=decoded, status="unknown-opcode",
                note=(
                    f"byte mul/div destination code {register_index:03b} names no word "
                    "register: only the odd codes exist (WA / BC / DE / HL)."
                ),
            )
        register_index >>= 1
        exec_size_kind = "word"
        reg_name = R16[register_index]
        imm = raw[2]
        quotient_mask = 0xFF
        pack_shift = 8
    elif size_kind == "long":
        if len(raw) != 4:
            return None
        exec_size_kind = "long"
        reg_name = R32[register_index]
        imm = int.from_bytes(raw[2:4], "little")
        quotient_mask = 0xFFFF
        pack_shift = 16
    else:
        return None

    reg_name, reg_value = _extract_register_value(before_cpu, exec_size_kind, register_index)
    if reg_value is None:
        owner = R32[register_index]
        return _blocked_result(
            before_cpu=before_cpu,
            decoded=decoded,
            status="requires-known-full-register",
            note=(
                f"{reg_name} must be known before this divide-immediate can be executed "
                f"honestly. Owner register {owner} is not yet in the current CPU state."
            ),
        )

    if imm == 0:
        return _blocked_result(
            before_cpu=before_cpu,
            decoded=decoded,
            status="silicon-undefined",
            note=(
                f"DIV {reg_name}, 0 is architecturally flagged through VF, but the destination "
                "result is undefined. The current subset does not guess that state."
            ),
        )

    quotient = reg_value // imm
    remainder = reg_value % imm
    if quotient > quotient_mask:
        return _blocked_result(
            before_cpu=before_cpu,
            decoded=decoded,
            status="silicon-undefined",
            note=(
                f"DIV {reg_name}, 0x{imm:0{2 if exec_size_kind == 'word' else 4}X} overflows "
                "the quotient field; the destination result is undefined on TLCS-900/H."
            ),
        )

    result = ((remainder & quotient_mask) << pack_shift) | (quotient & quotient_mask)
    _, reg_updates = _build_register_update(before_cpu, exec_size_kind, register_index, result)
    if reg_updates is None:
        return _blocked_result(
            before_cpu=before_cpu,
            decoded=decoded,
            status="requires-known-full-register",
            note=(
                f"{reg_name} cannot be updated honestly until its owning 32-bit register is "
                "known in the current CPU state."
            ),
        )

    flags_updates = {"vf": False}
    bits = 16 if exec_size_kind == "word" else 32
    return _executed_result(
        before_cpu=before_cpu,
        decoded=decoded,
        written_registers=(reg_name, "PC"),
        memory_writes=(),
        after_memory=before_memory,
        new_pc=decoded.next_sequential_pc,
        reg_updates=reg_updates,
        flags_updates=flags_updates,
        note=(
            f"Executed prefixed DIV immediate from the current real execution subset. "
            f"{reg_name}=0x{reg_value:0{bits//4}X} / 0x{imm:0{2 if exec_size_kind == 'word' else 4}X} "
            f"-> quot=0x{quotient:0{quotient_mask.bit_length()//4}X}, "
            f"rem=0x{remainder:0{quotient_mask.bit_length()//4}X}."
        ),
    )


def _try_execute_prefixed_multiply_immediate(
    before_cpu: NgpcCpuState,
    before_memory: dict[int, int],
    decoded: DecodeResult,
) -> ExecutionResult | None:
    """MULTU / MULS with immediate operand.

    Catalog encoding:
      long prefix (D8..DF / E8..EF): r32 : 08/09 : imm16 — 4 bytes
        multu XR32, imm16 → XR32 = unsigned(XR32 & 0xFFFF) * unsigned(imm16)
        muls  XR32, imm16 → XR32 = signed(XR32 & 0xFFFF)  * signed(imm16)
      word prefix (D0..D7): r16 : 08/09 : imm8 — 3 bytes
        multu R16, imm8 → R16 = unsigned(R16 & 0xFF) * unsigned(imm8)
        muls  R16, imm8 → R16 = signed(R16 & 0xFF)   * signed(imm8)

    Result is stored back into the full register (masked to size_kind width).
    Flags: ZF = (result == 0), SF = sign of result, CF = VF = overflow into upper half.
    For simplicity the current implementation sets ZF/SF/CF/VF honestly and NF=0.
    """
    raw = decoded.raw_bytes
    if raw is None:
        return None

    info = _prefixed_register_execute_info(raw[0])
    if info is None:
        return None

    op = raw[1] if len(raw) >= 2 else None
    if op not in (0x08, 0x09):
        return None

    size_kind, register_index = info

    if size_kind == "long":
        if len(raw) != 4:
            return None
        imm_raw = int.from_bytes(raw[2:4], "little")
    elif size_kind in ("word", "byte"):
        # byte prefix (C8..CF): semantics are word — a WORD register is the
        # destination and the lower byte is the source.
        if len(raw) != 3:
            return None
        imm_raw = raw[2]
        if size_kind == "byte":
            # ...but the code that names that word register is NOT an index.
            # Toshiba, <Multiply>/<Divide> Note 3: at byte size only the ODD codes
            # name a destination -- 001 = WA, 011 = BC, 101 = DE, 111 = HL -- and
            # the official assembler emits exactly that (`mul BC,7` = CB 08 07,
            # never C8/CA/CC/CE). Indexing straight into R16 sent `muls BC,7` into
            # XHL. The native core had the identical bug, so the two cores AGREED
            # on the wrong answer and only the assembler could break the tie.
            if (register_index & 1) == 0:
                return _blocked_result(
                    before_cpu=before_cpu, decoded=decoded, status="unknown-opcode",
                    note=(
                        f"byte mul destination code {register_index:03b} names no word "
                        "register: only the odd codes exist (WA / BC / DE / HL)."
                    ),
                )
            register_index >>= 1
        # Normalize to word semantics (R16 access, 8-bit operand, 16-bit result)
        size_kind = "word"
    else:
        return None

    reg_name, reg_value = _extract_register_value(before_cpu, size_kind, register_index)
    if reg_value is None:
        owner = R32[register_index]
        return _blocked_result(
            before_cpu=before_cpu,
            decoded=decoded,
            status="requires-known-full-register",
            note=(
                f"{reg_name} must be known before this multiply-immediate can be executed "
                f"honestly. Owner register {owner} is not yet in the current CPU state."
            ),
        )

    is_signed = (op == 0x09)

    if size_kind == "long":
        # operand width = lower 16 bits of r32
        operand_bits = 16
        result_mask = 0xFFFFFFFF
    else:
        # operand width = lower 8 bits of r16
        operand_bits = 8
        result_mask = 0xFFFF

    operand_sign_bit = 1 << (operand_bits - 1)
    operand_mask = operand_sign_bit - 1 + operand_sign_bit  # 2**operand_bits - 1

    lower = reg_value & operand_mask
    imm = imm_raw & operand_mask

    if is_signed:
        if lower >= operand_sign_bit:
            lower -= 2 * operand_sign_bit
        if imm >= operand_sign_bit:
            imm -= 2 * operand_sign_bit

    raw_result = lower * imm
    result = raw_result & result_mask

    # Overflow: result does not fit in a full-width signed value of the result register size.
    # CF/VF = 1 if the product could not be represented exactly in result_mask bits.
    if is_signed:
        result_bits = result_mask.bit_length()
        result_sign_bit = 1 << (result_bits - 1)
        signed_result = result if result < result_sign_bit else result - (1 << result_bits)
        overflow = raw_result != signed_result
    else:
        overflow = raw_result > result_mask

    zf = result == 0
    result_bits = result_mask.bit_length()
    sf = bool(result >> (result_bits - 1))

    # MUL / MULS CHANGE NO FLAGS. Toshiba's <Multiply> page gives the symbol row
    # `- - - - - -`, and spells it out line by line ("S = No change", ...). This
    # handler was writing Z, S, C and V from the product -- so any code doing
    # `mul` and then branching on Z or S got the wrong answer.
    #
    # (Contrast <Divide>, which is `- - - V - -`: V alone, set on divide-by-zero
    # or quotient overflow. The divide handler below already gets that right.)
    #
    # Found 2026-07-12 by the C++ differential harness.
    flags_updates = None
    _ = (zf, sf, overflow)   # computed above; deliberately not published

    _, reg_updates = _build_register_update(before_cpu, size_kind, register_index, result)
    if reg_updates is None:
        return _blocked_result(
            before_cpu=before_cpu,
            decoded=decoded,
            status="requires-known-full-register",
            note=(
                f"{reg_name} cannot be updated honestly until its owning 32-bit register "
                "is known in the current CPU state."
            ),
        )

    mnemonic = "muls" if is_signed else "multu"
    # Toshiba list (4): MUL rr,# = 12. 15. -   MULS rr,# = 10. 13. -
    # (this core billed its flat 8-cycle placeholder for both)
    mul_cycles = (
        (10 if size_kind == "word" else 13) if is_signed
        else (12 if size_kind == "word" else 15)
    )
    return _executed_result(
        before_cpu=before_cpu,
        decoded=decoded,
        written_registers=(reg_name, "PC"),
        memory_writes=(),
        after_memory=before_memory,
        new_pc=decoded.next_sequential_pc,
        reg_updates=reg_updates,
        flags_updates=flags_updates,
        cycles_consumed=mul_cycles,
        note=(
            f"Executed prefixed {mnemonic} immediate from the current real execution subset. "
            f"{reg_name} = {lower} * {imm} = {raw_result} (result masked to {size_kind})."
        ),
    )


def _try_execute_prefixed_ext(
    before_cpu: NgpcCpuState,
    before_memory: dict[int, int],
    decoded: DecodeResult,
) -> ExecutionResult | None:
    """EXTS / EXTZ — extend sign or zero into the upper half of a register.

    Catalog: C8+zz+r : 12 (EXTZ) / C8+zz+r : 13 (EXTS).
    - EXTS: dst<upper half> <- sign_bit of dst<lower half>
    - EXTZ: dst<upper half> <- 0
    Not applicable to byte-size registers (marked × in the catalog).
    Flags: no change.
    """
    raw = decoded.raw_bytes
    if raw is None or len(raw) != 2:
        return None

    if raw[1] not in (0x12, 0x13):
        return None

    info = _prefixed_register_execute_info(raw[0])
    if info is None:
        return None

    size_kind, register_index = info
    if size_kind == "byte":
        return _blocked_result(
            before_cpu=before_cpu,
            decoded=decoded,
            status="silicon-undefined",
            note=(
                f"{decoded.assembly} is not defined on byte-size registers in the local "
                "Toshiba table."
            ),
        )

    reg_name, current_value = _extract_register_value(before_cpu, size_kind, register_index)
    if current_value is None:
        owner_name = R32[register_index]
        return _blocked_result(
            before_cpu=before_cpu,
            decoded=decoded,
            status="requires-known-full-register",
            note=(
                f"{reg_name} cannot be sign/zero extended honestly until {owner_name} is "
                "already known in the current CPU state."
            ),
        )

    if size_kind == "long":
        lower_half = current_value & 0xFFFF
        if raw[1] == 0x13:
            upper_fill = 0xFFFF if (lower_half & 0x8000) else 0x0000
            new_value = lower_half | (upper_fill << 16)
            op_note = "EXTS long: upper 16 bits filled with sign of bit 15."
        else:
            new_value = lower_half
            op_note = "EXTZ long: upper 16 bits cleared to zero."
    else:
        lower_half = current_value & 0xFF
        if raw[1] == 0x13:
            upper_fill = 0xFF if (lower_half & 0x80) else 0x00
            new_value = lower_half | (upper_fill << 8)
            op_note = "EXTS word: upper byte filled with sign of bit 7."
        else:
            new_value = lower_half
            op_note = "EXTZ word: upper byte cleared to zero."

    _, reg_updates = _build_register_update(before_cpu, size_kind, register_index, new_value)
    if reg_updates is None:
        return _blocked_result(
            before_cpu=before_cpu,
            decoded=decoded,
            status="requires-known-full-register",
            note=(
                f"{reg_name} cannot be updated honestly until its owning 32-bit register is "
                "known in the current CPU state."
            ),
        )

    mnemonic = "EXTS" if raw[1] == 0x13 else "EXTZ"
    return _executed_result(
        before_cpu=before_cpu,
        decoded=decoded,
        written_registers=(reg_name, "PC"),
        memory_writes=(),
        after_memory=before_memory,
        new_pc=decoded.next_sequential_pc,
        reg_updates=reg_updates,
        note=(
            f"Executed {mnemonic} from the current real execution subset. {op_note}"
        ),
    )


def _try_execute_prefixed_ldc(
    before_cpu: NgpcCpuState,
    before_memory: dict[int, int],
    decoded: DecodeResult,
) -> ExecutionResult | None:
    """Execute prefixed LDC register/control-register forms for real."""
    raw = decoded.raw_bytes
    if raw is None or len(raw) != 3 or raw[1] not in (0x2E, 0x2F):
        return None

    info = _prefixed_register_execute_info(raw[0])
    if info is None:
        return None

    register_index = info[1]
    control_info = _read_control_register_value(before_cpu, raw[2])
    if control_info is None:
        control_name = control_register_name(raw[2])
        direction = "write" if raw[1] == 0x2E else "read"
        return _blocked_result(
            before_cpu=before_cpu,
            decoded=decoded,
            status="unmodeled-control-register",
            note=(
                f"{decoded.assembly} targets TLCS-900/H control register {control_name}. "
                f"The current executor does not model {direction} access to this control-register "
                "number yet, so it stops here instead of inventing side effects."
            ),
        )

    control_name, size_kind, control_value = control_info
    register_name, register_value = _extract_register_value(before_cpu, size_kind, register_index)

    if raw[1] == 0x2E:
        if register_value is None:
            return _blocked_result(
                before_cpu=before_cpu,
                decoded=decoded,
                status="requires-known-source-register",
                note=(
                    f"{decoded.assembly} needs source register {register_name} modeled before "
                    f"writing TLCS-900/H control register {control_name}."
                ),
            )
        write_result = _write_control_register_value(before_cpu, raw[2], register_value)
        assert write_result is not None
        _, extra_cpu_updates = write_result
        return _executed_result(
            before_cpu=before_cpu,
            decoded=decoded,
            written_registers=(control_name, "PC"),
            memory_writes=(),
            after_memory=before_memory,
            new_pc=decoded.next_sequential_pc,
            reg_updates=None,
            extra_cpu_updates=extra_cpu_updates,
            note=(
                f"Executed {decoded.assembly}: {control_name}=0x{register_value:0{2 if size_kind == 'byte' else 4 if size_kind == 'word' else 8}X}."
            ),
            cycles_consumed=LDC_CONTROL_REGISTER_CYCLES,
        )

    if control_value is None:
        return _blocked_result(
            before_cpu=before_cpu,
            decoded=decoded,
            status="requires-known-control-register",
            note=(
                f"{decoded.assembly} needs TLCS-900/H control register {control_name} modeled "
                "before it can be read back honestly."
            ),
        )

    _, reg_updates = _build_register_update(before_cpu, size_kind, register_index, control_value)
    if reg_updates is None:
        return _blocked_result(
            before_cpu=before_cpu,
            decoded=decoded,
            status="requires-known-full-register",
            note=(
                f"{decoded.assembly} needs destination owner register {R32[register_index if size_kind != 'byte' else register_index // 2]} "
                "fully known before the selected slice can be written back honestly."
            ),
        )
    return _executed_result(
        before_cpu=before_cpu,
        decoded=decoded,
        written_registers=(register_name, "PC"),
        memory_writes=(),
        after_memory=before_memory,
        new_pc=decoded.next_sequential_pc,
        reg_updates=reg_updates,
        note=(
            f"Executed {decoded.assembly}: {register_name}=0x{control_value:0{2 if size_kind == 'byte' else 4 if size_kind == 'word' else 8}X} "
            f"read from {control_name}."
        ),
        cycles_consumed=LDC_CONTROL_REGISTER_CYCLES,
    )


def _try_execute_prefixed_daa(
    before_cpu: NgpcCpuState,
    before_memory: dict[int, int],
    decoded: DecodeResult,
) -> ExecutionResult | None:
    """DAA on the prefixed register family.

    The local Toshiba table exposes `DAA r` only for byte-size register forms.
    It consumes the incoming C/H/N flags and updates S/Z/H/V(parity)/N/C.
    """
    raw = decoded.raw_bytes
    if raw is None or len(raw) != 2 or raw[1] != 0x10:
        return None

    info = _prefixed_register_execute_info(raw[0])
    if info is None:
        return None

    size_kind, register_index = info
    if size_kind != "byte":
        return _blocked_result(
            before_cpu=before_cpu,
            decoded=decoded,
            status="silicon-undefined",
            note=(
                f"{decoded.assembly} is only defined for byte-size register operands in the "
                "local Toshiba TLCS-900/L1 table."
            ),
        )

    if (
        before_cpu.flags.cf is None
        or before_cpu.flags.hf is None
        or before_cpu.flags.nf is None
    ):
        return _blocked_result(
            before_cpu=before_cpu,
            decoded=decoded,
            status="requires-known-flags",
            note=(
                f"{decoded.assembly} requires the incoming C, H, and N flags to be known "
                "before BCD adjustment can be executed honestly."
            ),
        )

    reg_name, reg_value = _extract_register_value(before_cpu, size_kind, register_index)
    if reg_value is None:
        owner_name = R32[register_index // 2]
        return _blocked_result(
            before_cpu=before_cpu,
            decoded=decoded,
            status="requires-known-full-register",
            note=(
                f"{reg_name} cannot be decimal-adjusted honestly until {owner_name} is already "
                "known in the current CPU state."
            ),
        )

    result, flags_updates = _evaluate_daa(
        reg_value=reg_value,
        carry_in=before_cpu.flags.cf,
        half_carry_in=before_cpu.flags.hf,
        subtract_in=before_cpu.flags.nf,
    )
    reg_update_name, reg_updates = _build_register_update(before_cpu, size_kind, register_index, result)
    if reg_updates is None:
        return _blocked_result(
            before_cpu=before_cpu,
            decoded=decoded,
            status="requires-known-full-register",
            note=(
                f"{reg_name} cannot be written back honestly until its owning 32-bit register "
                "is known in the current CPU state."
            ),
        )

    return _executed_result(
        before_cpu=before_cpu,
        decoded=decoded,
        written_registers=(reg_update_name, "PC"),
        memory_writes=(),
        after_memory=before_memory,
        new_pc=decoded.next_sequential_pc,
        reg_updates=reg_updates,
        flags_updates=flags_updates,
        note=(
            "Executed DAA from the current real execution subset. "
            f"{reg_name}=0x{result:02X} after BCD correction."
        ),
    )


def _try_execute_prefixed_paa(
    before_cpu: NgpcCpuState,
    before_memory: dict[int, int],
    decoded: DecodeResult,
) -> ExecutionResult | None:
    """PAA on the prefixed register family.

    The local Toshiba table exposes `PAA r` only for word/long register forms.
    It increments the destination by 1 only when bit 0 is set and leaves flags
    unchanged.
    """
    raw = decoded.raw_bytes
    if raw is None or len(raw) != 2 or raw[1] != 0x14:
        return None

    info = _prefixed_register_execute_info(raw[0])
    if info is None:
        return None

    size_kind, register_index = info
    if size_kind == "byte":
        return _blocked_result(
            before_cpu=before_cpu,
            decoded=decoded,
            status="silicon-undefined",
            note=(
                f"{decoded.assembly} is only defined for word/long register operands in the "
                "local Toshiba TLCS-900/L1 table."
            ),
        )

    reg_name, reg_value = _extract_register_value(before_cpu, size_kind, register_index)
    if reg_value is None:
        owner_name = R32[register_index]
        return _blocked_result(
            before_cpu=before_cpu,
            decoded=decoded,
            status="requires-known-full-register",
            note=(
                f"{reg_name} cannot be pointer-adjusted honestly until {owner_name} is already "
                "known in the current CPU state."
            ),
        )

    bits = {"word": 16, "long": 32}[size_kind]
    mask = (1 << bits) - 1
    result = (reg_value + 1) & mask if (reg_value & 0x01) else reg_value

    reg_update_name, reg_updates = _build_register_update(before_cpu, size_kind, register_index, result)
    if reg_updates is None:
        return _blocked_result(
            before_cpu=before_cpu,
            decoded=decoded,
            status="requires-known-full-register",
            note=(
                f"{reg_name} cannot be written back honestly until its owning 32-bit register "
                "is known in the current CPU state."
            ),
        )

    action = "incremented to the next even address" if result != reg_value else "already even"
    return _executed_result(
        before_cpu=before_cpu,
        decoded=decoded,
        written_registers=(reg_update_name, "PC"),
        memory_writes=(),
        after_memory=before_memory,
        new_pc=decoded.next_sequential_pc,
        reg_updates=reg_updates,
        note=(
            "Executed PAA from the current real execution subset. "
            f"{reg_name} was {action}."
        ),
    )


def _bit_reverse_16(value: int) -> int:
    result = 0
    for bit_index in range(16):
        result = (result << 1) | ((value >> bit_index) & 1)
    return result


def _try_execute_prefixed_mirr(
    before_cpu: NgpcCpuState,
    before_memory: dict[int, int],
    decoded: DecodeResult,
) -> ExecutionResult | None:
    """MIRR on the prefixed register family.

    The local Toshiba table exposes `MIRR r` as a word-only form encoded on
    `D8..DF : 16`, despite those prefix bytes otherwise serving the working-bank
    long family in the broader register-prefix space.
    """
    raw = decoded.raw_bytes
    if raw is None or len(raw) != 2 or raw[1] != 0x16 or not (0xD8 <= raw[0] <= 0xDF):
        return None

    register_index = raw[0] & 0x07
    reg_name, reg_value = _extract_register_value(before_cpu, "word", register_index)
    if reg_value is None:
        owner_name = R32[register_index]
        return _blocked_result(
            before_cpu=before_cpu,
            decoded=decoded,
            status="requires-known-full-register",
            note=(
                f"{reg_name} cannot be mirrored honestly until {owner_name} is already known "
                "in the current CPU state."
            ),
        )

    result = _bit_reverse_16(reg_value)
    reg_update_name, reg_updates = _build_register_update(
        before_cpu,
        size_kind="word",
        register_index=register_index,
        value=result,
    )
    if reg_updates is None:
        return _blocked_result(
            before_cpu=before_cpu,
            decoded=decoded,
            status="requires-known-full-register",
            note=(
                f"{reg_name} cannot be written back honestly until its owning 32-bit register "
                "is known in the current CPU state."
            ),
        )

    return _executed_result(
        before_cpu=before_cpu,
        decoded=decoded,
        written_registers=(reg_update_name, "PC"),
        memory_writes=(),
        after_memory=before_memory,
        new_pc=decoded.next_sequential_pc,
        reg_updates=reg_updates,
        note=(
            "Executed MIRR from the current real execution subset. "
            f"{reg_name}=0x{result:04X} after 16-bit bit reversal."
        ),
    )


def _try_execute_prefixed_mula(
    view: NgpcFetchView,
    before_cpu: NgpcCpuState,
    before_memory: dict[int, int],
    decoded: DecodeResult,
) -> ExecutionResult | None:
    """MULA on the prefixed register family.

    The local Toshiba table exposes `MULA rr` as the long-register form
    `D8..DF : 19`, with the selected `rr` receiving:
    `rr <- rr + (XDE) * (XHL)` where both memory operands are signed
    16-bit words, followed by `XHL <- XHL - 2`.
    """
    raw = decoded.raw_bytes
    if raw is None or len(raw) != 2 or raw[1] != 0x19 or not (0xD8 <= raw[0] <= 0xDF):
        return None

    register_index = raw[0] & 0x07

    dest_name, dest_value = _extract_register_value(before_cpu, "long", register_index)
    if dest_value is None:
        return _blocked_result(
            before_cpu=before_cpu,
            decoded=decoded,
            status="requires-known-full-register",
            note=(
                f"{dest_name} must be known before {decoded.assembly} can be executed honestly."
            ),
        )

    _, xde_value = _extract_register_value(before_cpu, "long", 2)
    if xde_value is None:
        return _blocked_result(
            before_cpu=before_cpu,
            decoded=decoded,
            status="requires-known-full-register",
            note="XDE must be known before MULA can fetch its first signed 16-bit operand.",
        )

    _, xhl_value = _extract_register_value(before_cpu, "long", 3)
    if xhl_value is None:
        return _blocked_result(
            before_cpu=before_cpu,
            decoded=decoded,
            status="requires-known-full-register",
            note="XHL must be known before MULA can fetch its second signed 16-bit operand.",
        )

    left_bytes = _read_runtime_bytes(view, before_memory, _mask_address(xde_value), 2)
    if left_bytes is None:
        return _blocked_result(
            before_cpu=before_cpu,
            decoded=decoded,
            status="runtime-memory-unavailable",
            note=(
                f"MULA could not read the signed word pointed to by XDE at 0x{xde_value & 0xFFFFFF:06X}."
            ),
        )

    right_bytes = _read_runtime_bytes(view, before_memory, _mask_address(xhl_value), 2)
    if right_bytes is None:
        return _blocked_result(
            before_cpu=before_cpu,
            decoded=decoded,
            status="runtime-memory-unavailable",
            note=(
                f"MULA could not read the signed word pointed to by XHL at 0x{xhl_value & 0xFFFFFF:06X}."
            ),
        )

    left = _signed_u16(left_bytes)
    right = _signed_u16(right_bytes)
    dest_signed = dest_value if dest_value < 0x80000000 else dest_value - 0x100000000
    raw_result = dest_signed + (left * right)
    result = raw_result & 0xFFFFFFFF
    signed_result = result if result < 0x80000000 else result - 0x100000000
    overflow = raw_result != signed_result

    _, reg_updates = _build_register_update(before_cpu, "long", register_index, result)
    assert reg_updates is not None

    # The local Toshiba wording is sequential: load the sum into dst, then
    # decrement XHL by 2. If dst is XHL, the post-add register value is what
    # gets decremented.
    new_xhl = ((result if register_index == 3 else xhl_value) - 2) & 0xFFFFFFFF
    reg_updates["xhl"] = new_xhl

    written_registers = ("XHL", "PC") if dest_name == "XHL" else (dest_name, "XHL", "PC")
    flags_updates = {
        "sf": bool(result & 0x80000000),
        "zf": result == 0,
        "vf": overflow,
    }

    return _executed_result(
        before_cpu=before_cpu,
        decoded=decoded,
        written_registers=written_registers,
        memory_writes=(),
        after_memory=before_memory,
        new_pc=decoded.next_sequential_pc,
        reg_updates=reg_updates,
        flags_updates=flags_updates,
        note=(
            "Executed MULA from the current real execution subset. "
            f"{dest_name}=0x{dest_value:08X} + ({left} * {right}) -> 0x{result:08X}; "
            f"XHL then decremented to 0x{new_xhl:08X}."
        ),
    )


def _try_execute_prefixed_modulo_adjust(
    before_cpu: NgpcCpuState,
    before_memory: dict[int, int],
    decoded: DecodeResult,
) -> ExecutionResult | None:
    """MINC/MDEC modulo adjust special cases on `D8..DF`.

    The local Toshiba table exposes these six word-only forms on the
    `D8..DF` prefix bytes even though that range normally maps to the
    32-bit working-bank family. The encoded imm16 is the documented
    `# - step` payload:
      MINC1/MDEC1: imm = # - 1
      MINC2/MDEC2: imm = # - 2
      MINC4/MDEC4: imm = # - 4
    where `#` must be a power of two in the documented range.
    """
    raw = decoded.raw_bytes
    if raw is None or len(raw) != 4 or not (0xD8 <= raw[0] <= 0xDF):
        return None

    op = raw[1]
    op_info = {
        0x38: ("minc1", 1, True),
        0x39: ("minc2", 2, True),
        0x3A: ("minc4", 4, True),
        0x3C: ("mdec1", 1, False),
        0x3D: ("mdec2", 2, False),
        0x3E: ("mdec4", 4, False),
    }.get(op)
    if op_info is None:
        return None

    mnemonic, step, is_increment = op_info
    register_index = raw[0] & 0x07
    encoded_immediate = int.from_bytes(raw[2:4], "little")
    modulo = encoded_immediate + step

    # Documented constraint: # must be 2**n with 1<=n<=15 / 2<=n<=15 / 3<=n<=15
    # depending on the step. Since modulo is a power of two, the minimum valid
    # value is always step * 2.
    valid_modulo = (
        modulo > encoded_immediate
        and modulo >= (step * 2)
        and modulo <= 0x8000
        and (modulo & (modulo - 1)) == 0
    )
    if not valid_modulo:
        return _blocked_result(
            before_cpu=before_cpu,
            decoded=decoded,
            status="silicon-undefined",
            note=(
                f"{decoded.assembly} uses encoded imm16=0x{encoded_immediate:04X}, which does not "
                f"reconstruct a documented modulo window (# = imm + {step} must be a power of two "
                f"between 0x{step * 2:04X} and 0x8000)."
            ),
        )

    reg_name, reg_value = _extract_register_value(before_cpu, "word", register_index)
    if reg_value is None:
        owner_name = R32[register_index]
        return _blocked_result(
            before_cpu=before_cpu,
            decoded=decoded,
            status="requires-known-full-register",
            note=(
                f"{reg_name} cannot be adjusted honestly until {owner_name} is already known in "
                "the current CPU state."
            ),
        )

    residue = reg_value % modulo
    if is_increment:
        result = (reg_value - encoded_immediate) if residue == encoded_immediate else (reg_value + step)
        direction_note = (
            f"wrapped by subtracting 0x{encoded_immediate:04X}"
            if residue == encoded_immediate
            else f"advanced by +{step}"
        )
    else:
        result = (reg_value + encoded_immediate) if residue == 0 else (reg_value - step)
        direction_note = (
            f"wrapped by adding 0x{encoded_immediate:04X}"
            if residue == 0
            else f"retreated by -{step}"
        )
    result &= 0xFFFF

    reg_update_name, reg_updates = _build_register_update(
        before_cpu,
        size_kind="word",
        register_index=register_index,
        value=result,
    )
    if reg_updates is None:
        return _blocked_result(
            before_cpu=before_cpu,
            decoded=decoded,
            status="requires-known-full-register",
            note=(
                f"{reg_name} cannot be written back honestly until its owning 32-bit register "
                "is known in the current CPU state."
            ),
        )

    return _executed_result(
        before_cpu=before_cpu,
        decoded=decoded,
        written_registers=(reg_update_name, "PC"),
        memory_writes=(),
        after_memory=before_memory,
        new_pc=decoded.next_sequential_pc,
        reg_updates=reg_updates,
        note=(
            f"Executed prefixed {mnemonic.upper()} from the current real execution subset. "
            f"Encoded imm16=0x{encoded_immediate:04X} reconstructs modulo #=0x{modulo:04X}; "
            f"{reg_name}=0x{result:04X} ({direction_note})."
        ),
    )


def _try_execute_prefixed_bs1(
    before_cpu: NgpcCpuState,
    before_memory: dict[int, int],
    decoded: DecodeResult,
) -> ExecutionResult | None:
    """BS1F / BS1B on the prefixed register family.

    The local Toshiba table exposes these as word-only special cases on
    `D8..DF : 0E/0F`, with `A` as the fixed destination.
    """
    raw = decoded.raw_bytes
    if raw is None or len(raw) != 2 or raw[1] not in (0x0E, 0x0F) or not (0xD8 <= raw[0] <= 0xDF):
        return None

    register_index = raw[0] & 0x07
    src_name, src_value = _extract_register_value(before_cpu, "word", register_index)
    if src_value is None:
        owner_name = R32[register_index]
        return _blocked_result(
            before_cpu=before_cpu,
            decoded=decoded,
            status="requires-known-full-register",
            note=(
                f"{src_name} cannot be scanned honestly until {owner_name} is already known in "
                "the current CPU state."
            ),
        )

    if src_value == 0:
        return _blocked_result(
            before_cpu=before_cpu,
            decoded=decoded,
            status="silicon-undefined",
            note=(
                f"{decoded.assembly} leaves A undefined when the source word is zero in the local "
                "Toshiba TLCS-900/L1 table."
            ),
        )

    if raw[1] == 0x0E:
        bit_index = (src_value & -src_value).bit_length() - 1
        direction = "forward"
    else:
        bit_index = src_value.bit_length() - 1
        direction = "backward"

    dest_name, reg_updates = _build_register_update(
        before_cpu,
        size_kind="byte",
        register_index=1,  # A
        value=bit_index,
    )
    if reg_updates is None:
        return _blocked_result(
            before_cpu=before_cpu,
            decoded=decoded,
            status="requires-known-full-register",
            note=(
                "A cannot be updated honestly until XWA is already known in the current CPU state."
            ),
        )

    return _executed_result(
        before_cpu=before_cpu,
        decoded=decoded,
        written_registers=(dest_name, "PC"),
        memory_writes=(),
        after_memory=before_memory,
        new_pc=decoded.next_sequential_pc,
        reg_updates=reg_updates,
        flags_updates={"vf": False},
        note=(
            "Executed BS1 from the current real execution subset. "
            f"{decoded.mnemonic.upper()} searched {src_name} {direction} and wrote bit index "
            f"{bit_index} into A."
        ),
    )


def _try_execute_prefixed_cpl_neg(
    before_cpu: NgpcCpuState,
    before_memory: dict[int, int],
    decoded: DecodeResult,
) -> ExecutionResult | None:
    """CPL / NEG on prefixed byte/word register families.

    Toshiba table (local datasheet):
    - `CPL r` : flags `H=1`, `N=1`, others unchanged
    - `NEG r` : flags follow `0 - r` with `N=1`
    Long-size forms are marked undefined in the local catalog, so we
    stop honestly instead of inventing a long-width behavior.
    """
    raw = decoded.raw_bytes
    if raw is None or len(raw) != 2 or raw[1] not in (0x06, 0x07):
        return None

    info = _prefixed_register_execute_info(raw[0])
    if info is None:
        return None

    size_kind, register_index = info
    if size_kind == "long":
        return _blocked_result(
            before_cpu=before_cpu,
            decoded=decoded,
            status="silicon-undefined",
            note=(
                f"{decoded.assembly} is not defined for long-size register operands in the "
                "local Toshiba TLCS-900/L1 table."
            ),
        )

    reg_name, reg_value = _extract_register_value(before_cpu, size_kind, register_index)
    if reg_value is None:
        owner_name = R32[register_index // 2]
        return _blocked_result(
            before_cpu=before_cpu,
            decoded=decoded,
            status="requires-known-full-register",
            note=(
                f"{reg_name} cannot be updated honestly until {owner_name} is already known "
                "in the current CPU state."
            ),
        )

    result, flags_updates = _evaluate_cpl_neg(size_kind=size_kind, reg_value=reg_value, op=raw[1])
    reg_update_name, reg_updates = _build_register_update(before_cpu, size_kind, register_index, result)
    if reg_updates is None:
        return _blocked_result(
            before_cpu=before_cpu,
            decoded=decoded,
            status="requires-known-full-register",
            note=(
                f"{reg_name} cannot be written back honestly until its owning 32-bit register "
                "is known in the current CPU state."
            ),
        )

    return _executed_result(
        before_cpu=before_cpu,
        decoded=decoded,
        written_registers=(reg_update_name, "PC"),
        memory_writes=(),
        after_memory=before_memory,
        new_pc=decoded.next_sequential_pc,
        reg_updates=reg_updates,
        flags_updates=flags_updates,
        note=(
            f"Executed {decoded.mnemonic.upper()} from the current real execution subset. "
            f"{reg_name}=0x{result:0{({'byte': 2, 'word': 4}[size_kind])}X}."
        ),
    )


def _e7_register_target(reg_code: int) -> tuple[str, int, int] | None:
    """Resolve an E7 (long) register code to a 32-bit register target.

    Returns ("current", r32_index, 0) for codes 0xE0..0xFF (XWA..XSP), or
    ("banked", bank, r32_index) for codes 0x00..0x3F (banked XWA..XHL), or None.
    """
    if 0xE0 <= reg_code <= 0xFF and (reg_code & 3) == 0:
        return ("current", (reg_code - 0xE0) >> 2, 0)
    if reg_code < 0x40 and (reg_code & 3) == 0:
        return ("banked", reg_code >> 4, (reg_code >> 2) & 3)
    return None


def _e7_read_r32(before_cpu: NgpcCpuState, target: tuple[str, int, int]) -> int | None:
    kind, a, b = target
    if kind == "current":
        return getattr(before_cpu.regs, REG32_FIELDS[a])
    parts = [_extract_banked_core_byte(before_cpu, a, b, p) for p in range(4)]
    if any(p is None for p in parts):
        return None
    return parts[0] | (parts[1] << 8) | (parts[2] << 16) | (parts[3] << 24)


def _e7_write_r32(
    before_cpu: NgpcCpuState, target: tuple[str, int, int], value: int
) -> tuple[dict[str, int] | None, dict[str, object] | None]:
    value &= 0xFFFFFFFF
    kind, a, b = target
    if kind == "current":
        return ({REG32_FIELDS[a]: value}, None)
    banks = _ensure_register_banks(before_cpu)
    for p in range(4):
        banks = _replace_register_bank_slot(banks, a, b * 4 + p, (value >> (8 * p)) & 0xFF)
    reg_updates = None
    if _current_register_bank_index(before_cpu, fallback_zero=True) == a:
        reg_updates = {_BANKED_CORE_FIELDS[b]: value}
    return (reg_updates, {"register_banks": banks})


_E7_ALU_RR = {
    0x80: "add", 0x88: "ld", 0x90: "adc", 0x98: "ld",
    0xA0: "sub", 0xB0: "sbc", 0xB8: "ex", 0xC0: "and",
    0xD0: "xor", 0xE0: "or", 0xF0: "cp",
}


def _try_execute_e7_extended_register(
    view: NgpcFetchView,
    before_cpu: NgpcCpuState,
    before_memory: dict[int, int],
    decoded: DecodeResult,
) -> ExecutionResult | None:
    """Execute the E7 LONG extended-register prefix family (full 32-bit regs).

    Handles the forms the real SNK BIOS boot delay loop uses: `ld r,#3` (0xA8),
    `inc/dec #n,r` (0x60/0x68), `cp r,imm32` / ALU-imm32 (0xC8..0xCF), `ld r,imm32`
    (0x03), `cp r,#3` (0xD8). Operates on current-bank (XWA..XSP) or banked
    (XWA..XHL of banks 0..3) 32-bit registers via the register-bank model.
    """
    raw = decoded.raw_bytes
    if raw is None or len(raw) < 3 or raw[0] != 0xE7:
        return None
    reg_code = raw[1]
    op = raw[2]
    target = _e7_register_target(reg_code)
    if target is None:
        return _blocked_result(
            before_cpu=before_cpu,
            decoded=decoded,
            status="unsupported-decoded-instruction",
            note=f"E7 {decoded.assembly}: register code 0x{reg_code:02X} is not a modeled 32-bit register.",
        )
    reg_name = (decoded.operands or "").split(",")[0].strip() or f"reg0x{reg_code:02X}"
    new_pc = decoded.next_sequential_pc

    def _blocked_unknown() -> ExecutionResult:
        return _blocked_result(
            before_cpu=before_cpu,
            decoded=decoded,
            status="requires-known-full-register",
            note=f"E7 {decoded.assembly} needs {reg_name} to be modeled before it can execute.",
        )

    # push R32 (op 0x04) -- push the long E7 register onto the stack.
    # Baseball Stars (Pocket) frontier `E7 30 04` = push XWA3.
    if op == 0x04:
        value = _e7_read_r32(before_cpu, target)
        if value is None:
            return _blocked_unknown()
        return _execute_push_bytes(
            view=view,
            before_cpu=before_cpu,
            before_memory=before_memory,
            decoded=decoded,
            data=(value & 0xFFFFFFFF).to_bytes(4, "little"),
            note=f"Executed push {reg_name}: pushed 0x{value:08X} (long) onto the stack.",
        )

    # pop R32 (op 0x05) -- pop a long from the stack into the E7 register.
    # Baseball Stars (Pocket/Color) frontier `E7 3C 05` = pop <banked r32>.
    if op == 0x05:
        xsp = before_cpu.regs.xsp
        if xsp is None:
            return _blocked_result(
                before_cpu=before_cpu,
                decoded=decoded,
                status="requires-known-address-register",
                note=f"E7 {decoded.assembly} needs XSP known to pop from the stack.",
            )
        data = _read_runtime_bytes(view, before_memory, _mask_address(xsp), 4)
        if data is None:
            return _blocked_result(
                before_cpu=before_cpu,
                decoded=decoded,
                status="runtime-memory-unavailable",
                note="This E7 pop needs 4 readable bytes at XSP, unavailable in overlay/bus.",
            )
        value = int.from_bytes(data, "little")
        reg_updates, extra = _e7_write_r32(before_cpu, target, value)
        reg_updates = dict(reg_updates or {})
        reg_updates["xsp"] = _mask_address(xsp + 4)
        return _executed_result(
            before_cpu=before_cpu, decoded=decoded,
            written_registers=(reg_name, "XSP", "PC"),
            memory_writes=(), after_memory=before_memory, new_pc=new_pc,
            reg_updates=reg_updates, extra_cpu_updates=extra,
            note=f"Executed pop {reg_name}: popped 0x{value:08X} (long); XSP += 4.",
        )

    # ld/ex/ALU R32 <-> E7 long reg (decode_zz_r r+r at long size).
    # cave frontier `E7 3C 9A` = ld XHL3, XDE (hi 0x98 = ext reg is dest).
    hi = op & 0xF8
    if ((0x40 <= op < 0xC8) or (0xD0 <= op < 0xE8) or (0xF0 <= op < 0xF8)) and hi in _E7_ALU_RR:
        other_index = op & 0x07
        other_name = R32[other_index]
        other_value = getattr(before_cpu.regs, REG32_FIELDS[other_index])

        def _blocked_other() -> ExecutionResult:
            return _blocked_result(
                before_cpu=before_cpu,
                decoded=decoded,
                status="requires-known-full-register",
                note=f"E7 {decoded.assembly} needs {other_name} to be modeled before it can execute.",
            )

        if hi == 0x88:  # ld r(normal) <- R(ext) : normal register is the destination.
            ext_value = _e7_read_r32(before_cpu, target)
            if ext_value is None:
                return _blocked_unknown()
            return _executed_result(
                before_cpu=before_cpu, decoded=decoded, written_registers=(other_name, "PC"),
                memory_writes=(), after_memory=before_memory, new_pc=new_pc,
                reg_updates={REG32_FIELDS[other_index]: ext_value & 0xFFFFFFFF},
                note=f"Executed {decoded.assembly}: {other_name} <- {reg_name}=0x{ext_value:08X}.",
            )
        if hi == 0x98:  # ld R(ext) <- r(normal) : E7 register is the destination.
            if other_value is None:
                return _blocked_other()
            reg_updates, extra = _e7_write_r32(before_cpu, target, other_value)
            return _executed_result(
                before_cpu=before_cpu, decoded=decoded, written_registers=(reg_name, "PC"),
                memory_writes=(), after_memory=before_memory, new_pc=new_pc,
                reg_updates=reg_updates, extra_cpu_updates=extra,
                note=f"Executed {decoded.assembly}: {reg_name} <- {other_name}=0x{other_value:08X}.",
            )
        if hi == 0xB8:  # ex r(normal), R(ext) : swap the two 32-bit registers.
            ext_value = _e7_read_r32(before_cpu, target)
            if ext_value is None:
                return _blocked_unknown()
            if other_value is None:
                return _blocked_other()
            reg_updates, extra = _e7_write_r32(before_cpu, target, other_value)
            reg_updates = dict(reg_updates or {})
            reg_updates[REG32_FIELDS[other_index]] = ext_value & 0xFFFFFFFF
            return _executed_result(
                before_cpu=before_cpu, decoded=decoded, written_registers=(other_name, reg_name, "PC"),
                memory_writes=(), after_memory=before_memory, new_pc=new_pc,
                reg_updates=reg_updates, extra_cpu_updates=extra,
                note=f"Executed {decoded.assembly}: swapped {other_name}<->{reg_name}.",
            )

        # add/adc/sub/sbc/and/xor/or/cp: dest = normal register, source = E7 reg.
        alu = _E7_ALU_RR[hi]
        ext_value = _e7_read_r32(before_cpu, target)
        if ext_value is None:
            return _blocked_unknown()
        if other_value is None:
            return _blocked_other()
        if alu in ("adc", "sbc") and before_cpu.flags.cf is None:
            return _blocked_result(
                before_cpu=before_cpu, decoded=decoded, status="runtime-state-required",
                note=f"E7 {decoded.assembly} needs a known carry flag.",
            )
        carry = int(before_cpu.flags.cf) if alu in ("adc", "sbc") else 0
        if alu == "cp":
            flags = _compute_subtract_flags("long", other_value, ext_value)
            return _executed_result(
                before_cpu=before_cpu, decoded=decoded, written_registers=("PC",),
                memory_writes=(), after_memory=before_memory, new_pc=new_pc,
                reg_updates=None, flags_updates=flags,
                note=f"Executed {decoded.assembly}: flags = {other_name}=0x{other_value:08X} - {reg_name}=0x{ext_value:08X}.",
            )
        if alu in ("add", "adc"):
            new_value = (other_value + ext_value + carry) & 0xFFFFFFFF
            flags = _compute_add_flags("long", other_value, ext_value + carry)
        elif alu in ("sub", "sbc"):
            new_value = (other_value - ext_value - carry) & 0xFFFFFFFF
            flags = _compute_subtract_flags("long", other_value, ext_value + carry)
        elif alu == "and":
            new_value = other_value & ext_value
            flags = _compute_logical_flags("long", new_value, half_carry=True)
        elif alu == "xor":
            new_value = other_value ^ ext_value
            flags = _compute_logical_flags("long", new_value, half_carry=(alu == "and"))
        else:  # or
            new_value = other_value | ext_value
            flags = _compute_logical_flags("long", new_value, half_carry=(alu == "and"))
        return _executed_result(
            before_cpu=before_cpu, decoded=decoded, written_registers=(other_name, "PC"),
            memory_writes=(), after_memory=before_memory, new_pc=new_pc,
            reg_updates={REG32_FIELDS[other_index]: new_value}, flags_updates=flags,
            note=f"Executed {decoded.assembly}: {other_name} -> 0x{new_value:08X}.",
        )

    # ld r, #3 (compact immediate 0..7) — no flags.
    if 0xA8 <= op <= 0xAF:
        reg_updates, extra = _e7_write_r32(before_cpu, target, op & 0x07)
        return _executed_result(
            before_cpu=before_cpu, decoded=decoded, written_registers=(reg_name, "PC"),
            memory_writes=(), after_memory=before_memory, new_pc=new_pc,
            reg_updates=reg_updates, extra_cpu_updates=extra,
            note=f"Executed {decoded.assembly}: {reg_name} <- {op & 0x07}.",
        )

    # inc/dec #n, r — Z/S/V/H update, CF preserved.
    if 0x60 <= op <= 0x6F:
        cur = _e7_read_r32(before_cpu, target)
        if cur is None:
            return _blocked_unknown()
        n = (op & 0x07) or 8
        is_dec = op >= 0x68
        new_value = (cur - n if is_dec else cur + n) & 0xFFFFFFFF
        flags = dict(
            _compute_subtract_flags("long", cur, n) if is_dec else _compute_add_flags("long", cur, n)
        )
        flags.pop("cf", None)
        reg_updates, extra = _e7_write_r32(before_cpu, target, new_value)
        return _executed_result(
            before_cpu=before_cpu, decoded=decoded, written_registers=(reg_name, "PC"),
            memory_writes=(), after_memory=before_memory, new_pc=new_pc,
            reg_updates=reg_updates, extra_cpu_updates=extra, flags_updates=flags,
            note=f"Executed {decoded.assembly}: {reg_name} 0x{cur:08X} -> 0x{new_value:08X}.",
        )

    # cp r, #3 — flags only.
    if 0xD8 <= op <= 0xDF:
        cur = _e7_read_r32(before_cpu, target)
        if cur is None:
            return _blocked_unknown()
        flags = _compute_subtract_flags("long", cur, op & 0x07)
        return _executed_result(
            before_cpu=before_cpu, decoded=decoded, written_registers=("PC",),
            memory_writes=(), after_memory=before_memory, new_pc=new_pc,
            reg_updates=None, flags_updates=flags,
            note=f"Executed {decoded.assembly}: flags = {reg_name} - {op & 0x07}.",
        )

    # ld r, imm32 (0x03) and ALU r, imm32 (0xC8..0xCF).
    if op == 0x03 or (0xC8 <= op <= 0xCF):
        if len(raw) < 7:
            return None
        imm = int.from_bytes(raw[3:7], "little")
        if op == 0x03:  # ld imm32 — no flags
            reg_updates, extra = _e7_write_r32(before_cpu, target, imm)
            return _executed_result(
                before_cpu=before_cpu, decoded=decoded, written_registers=(reg_name, "PC"),
                memory_writes=(), after_memory=before_memory, new_pc=new_pc,
                reg_updates=reg_updates, extra_cpu_updates=extra,
                note=f"Executed {decoded.assembly}: {reg_name} <- 0x{imm:08X}.",
            )
        cur = _e7_read_r32(before_cpu, target)
        if cur is None:
            return _blocked_unknown()
        alu = ("add", "adc", "sub", "sbc", "and", "xor", "or", "cp")[op & 0x07]
        if alu == "cp":  # flags only
            flags = _compute_subtract_flags("long", cur, imm)
            return _executed_result(
                before_cpu=before_cpu, decoded=decoded, written_registers=("PC",),
                memory_writes=(), after_memory=before_memory, new_pc=new_pc,
                reg_updates=None, flags_updates=flags,
                note=f"Executed {decoded.assembly}: flags = {reg_name}(0x{cur:08X}) - 0x{imm:08X}.",
            )
        if alu in ("add", "sub"):
            new_value = (cur + imm if alu == "add" else cur - imm) & 0xFFFFFFFF
            flags = (_compute_add_flags if alu == "add" else _compute_subtract_flags)("long", cur, imm)
        elif alu in ("and", "or", "xor"):
            new_value = {"and": cur & imm, "or": cur | imm, "xor": cur ^ imm}[alu]
            flags = _compute_logical_flags("long", new_value, half_carry=(alu == "and"))
        else:  # adc/sbc need carry
            if before_cpu.flags.cf is None:
                return _blocked_result(
                    before_cpu=before_cpu, decoded=decoded, status="runtime-state-required",
                    note=f"E7 {decoded.assembly} needs a known carry flag.",
                )
            carry = int(before_cpu.flags.cf)
            new_value = (cur + imm + carry if alu == "adc" else cur - imm - carry) & 0xFFFFFFFF
            flags = (_compute_add_flags if alu == "adc" else _compute_subtract_flags)("long", cur, imm + carry)
        reg_updates, extra = _e7_write_r32(before_cpu, target, new_value)
        return _executed_result(
            before_cpu=before_cpu, decoded=decoded, written_registers=(reg_name, "PC"),
            memory_writes=(), after_memory=before_memory, new_pc=new_pc,
            reg_updates=reg_updates, extra_cpu_updates=extra, flags_updates=flags,
            note=f"Executed {decoded.assembly}: {reg_name} -> 0x{new_value:08X}.",
        )

    return _blocked_result(
        before_cpu=before_cpu, decoded=decoded, status="unsupported-decoded-instruction",
        note=f"E7 sub-op 0x{op:02X} ({decoded.assembly}) is decoded but not executed yet.",
    )


def _d7_read_word(before_cpu: NgpcCpuState, slice_target: tuple[int, int]) -> int | None:
    """Read the 16-bit word slice of a current-bank register (D7 prefix)."""
    r32_index, word_pos = slice_target
    full = getattr(before_cpu.regs, REG32_FIELDS[r32_index])
    if full is None:
        return None
    return (full >> (word_pos * 16)) & 0xFFFF


def _d7_write_word(
    before_cpu: NgpcCpuState, slice_target: tuple[int, int], value: int
) -> dict[str, int] | None:
    """Write a 16-bit word slice into a current-bank register, preserving the
    other half. Returns a reg_updates dict, or None if the owning register's
    full value is unknown (cannot preserve the other half honestly)."""
    r32_index, word_pos = slice_target
    full = getattr(before_cpu.regs, REG32_FIELDS[r32_index])
    if full is None:
        return None
    shift = word_pos * 16
    new_full = (full & ~(0xFFFF << shift) & 0xFFFFFFFF) | ((value & 0xFFFF) << shift)
    return {REG32_FIELDS[r32_index]: new_full & 0xFFFFFFFF}


def _d7_resolve_target(reg_code: int):
    """Resolve a D7 (word) register code to a 16-bit word target.

    Current-bank codes 0xE0..0xFF -> ("current", r32_index, word_pos).
    Banked codes 0x00..0x3F (RWA0..QHL3) -> ("banked", bank, r32_index, word_pos).
    word_pos 0 = bits 0..15 (R.../WA...), 1 = bits 16..31 (Q...). HW-confirmed
    2026-07-08: `D7 34 A8` = `ld RBC3, 0` (bank-3 XBC low word) executes on a real
    NGPC (hw_test_d7reg) -- ngdis's own r16_names table agrees code 0x34 = RBC3;
    only its buggy `tset 8, SP` annotation disagreed.
    """
    if 0xE0 <= reg_code <= 0xFF and (reg_code & 0x01) == 0:
        offset = reg_code - 0xE0
        return ("current", offset // 4, (offset % 4) // 2)
    if reg_code < 0x40 and (reg_code & 0x01) == 0:
        return ("banked", (reg_code >> 4) & 0x03, (reg_code >> 2) & 0x03, (reg_code >> 1) & 0x01)
    return None


def _d7_read_word2(before_cpu: NgpcCpuState, target) -> int | None:
    """Read the 16-bit word slice named by a D7 target (current or banked)."""
    if target[0] == "current":
        _, r32, word_pos = target
        full = getattr(before_cpu.regs, REG32_FIELDS[r32])
    else:
        _, bank, r32, word_pos = target
        full = _e7_read_r32(before_cpu, ("banked", bank, r32))
    if full is None:
        return None
    return (full >> (word_pos * 16)) & 0xFFFF


def _d7_write_word2(
    before_cpu: NgpcCpuState, target, value: int
) -> tuple[dict[str, int] | None, dict[str, object] | None]:
    """Write a 16-bit word slice (current or banked), preserving the other half.

    Returns (reg_updates, extra_cpu_updates). For a banked non-current write the
    result lands in extra_cpu_updates (register_banks); reg_updates is then None
    (or a current-bank mirror when the bank happens to be active). Both None means
    the owning 32-bit register's full value is unknown.
    """
    value &= 0xFFFF
    if target[0] == "current":
        _, r32, word_pos = target
        full = getattr(before_cpu.regs, REG32_FIELDS[r32])
        if full is None:
            return (None, None)
        shift = word_pos * 16
        new_full = (full & ~(0xFFFF << shift) & 0xFFFFFFFF) | (value << shift)
        return ({REG32_FIELDS[r32]: new_full & 0xFFFFFFFF}, None)
    _, bank, r32, word_pos = target
    full = _e7_read_r32(before_cpu, ("banked", bank, r32))
    if full is None:
        return (None, None)
    shift = word_pos * 16
    new_full = (full & ~(0xFFFF << shift) & 0xFFFFFFFF) | (value << shift)
    return _e7_write_r32(before_cpu, ("banked", bank, r32), new_full)


def _try_execute_d7_extended_register(
    view: NgpcFetchView,
    before_cpu: NgpcCpuState,
    before_memory: dict[int, int],
    decoded: DecodeResult,
) -> ExecutionResult | None:
    """Execute the D7 WORD extended-register prefix family (16-bit word slices).

    Mirrors the E7 (long) executor at word size, operating on the two 16-bit
    slices of the current-bank registers XWA..XSP via `d7_current_bank_slice`.
    Real engine runtime helper: `push QIZ` (`D7 FA 04`) at a function entry —
    the frontier that used to mis-decode as the 2-byte `rl A, SP`.
    """
    raw = decoded.raw_bytes
    if raw is None or len(raw) < 3 or raw[0] != 0xD7:
        return None
    reg_code = raw[1]
    op = raw[2]
    target = _d7_resolve_target(reg_code)
    if target is None:
        return _blocked_result(
            before_cpu=before_cpu, decoded=decoded, status="unsupported-decoded-instruction",
            note=f"D7 {decoded.assembly}: register code 0x{reg_code:02X} is not a modeled word register.",
        )
    reg_name = (decoded.operands or "").split(",")[-1].strip() or f"reg0x{reg_code:02X}"
    new_pc = decoded.next_sequential_pc

    def _blocked_unknown() -> ExecutionResult:
        return _blocked_result(
            before_cpu=before_cpu, decoded=decoded, status="requires-known-full-register",
            note=f"D7 {decoded.assembly} needs {reg_name}'s owning 32-bit register modeled before it can execute.",
        )

    # push r (0x04) — push the 16-bit slice, little-endian.
    if op == 0x04:
        value = _d7_read_word2(before_cpu, target)
        if value is None:
            return _blocked_unknown()
        data = bytes((value & 0xFF, (value >> 8) & 0xFF))
        return _execute_push_bytes(
            view=view, before_cpu=before_cpu, before_memory=before_memory, decoded=decoded,
            data=data, note=f"Executed {decoded.assembly}: pushed {reg_name}=0x{value:04X} (word).",
        )

    # pop r (0x05) — pop a 16-bit word off the stack into the slice.
    if op == 0x05:
        xsp = before_cpu.regs.xsp
        if xsp is None:
            return _blocked_result(
                before_cpu=before_cpu, decoded=decoded, status="requires-known-stack-pointer",
                note=f"D7 {decoded.assembly} needs a known XSP.",
            )
        popped = _read_runtime_bytes(view, before_memory, _mask_address(xsp), 2)
        if popped is None or len(popped) < 2:
            return _blocked_result(
                before_cpu=before_cpu, decoded=decoded, status="runtime-memory-unavailable",
                note=f"D7 {decoded.assembly}: stack word at 0x{xsp & 0xFFFFFF:06X} is not modeled.",
            )
        value = popped[0] | (popped[1] << 8)
        reg_updates, d7_extra = _d7_write_word2(before_cpu, target, value)
        if reg_updates is None and d7_extra is None:
            return _blocked_unknown()
        reg_updates = dict(reg_updates or {})
        reg_updates["xsp"] = (xsp + 2) & 0xFFFFFFFF
        return _executed_result(
            before_cpu=before_cpu, decoded=decoded, written_registers=(reg_name, "XSP", "PC"),
            memory_writes=(), after_memory=before_memory, new_pc=new_pc, reg_updates=reg_updates,
            extra_cpu_updates=d7_extra,
            note=f"Executed {decoded.assembly}: {reg_name} <- 0x{value:04X} (popped word).",
        )

    # ld r, #3 (0xA8..0xAF) — compact immediate, no flags. HW-confirmed retail
    # entry idiom `D7 34 A8` = ld RBC3, 0 (banked-word write).
    if 0xA8 <= op <= 0xAF:
        reg_updates, d7_extra = _d7_write_word2(before_cpu, target, op & 0x07)
        if reg_updates is None and d7_extra is None:
            return _blocked_unknown()
        return _executed_result(
            before_cpu=before_cpu, decoded=decoded, written_registers=(reg_name, "PC"),
            memory_writes=(), after_memory=before_memory, new_pc=new_pc, reg_updates=reg_updates,
            extra_cpu_updates=d7_extra,
            note=f"Executed {decoded.assembly}: {reg_name} <- {op & 0x07}.",
        )

    # inc/dec #n, r (0x60/0x68) — Z/S/V/H, CF preserved.
    if 0x60 <= op <= 0x6F:
        cur = _d7_read_word2(before_cpu, target)
        if cur is None:
            return _blocked_unknown()
        n = (op & 0x07) or 8
        is_dec = op >= 0x68
        new_value = (cur - n if is_dec else cur + n) & 0xFFFF
        flags = dict(_compute_subtract_flags("word", cur, n) if is_dec else _compute_add_flags("word", cur, n))
        flags.pop("cf", None)
        reg_updates, d7_extra = _d7_write_word2(before_cpu, target, new_value)
        if reg_updates is None and d7_extra is None:
            return _blocked_unknown()
        return _executed_result(
            before_cpu=before_cpu, decoded=decoded, written_registers=(reg_name, "PC"),
            memory_writes=(), after_memory=before_memory, new_pc=new_pc,
            reg_updates=reg_updates, extra_cpu_updates=d7_extra, flags_updates=flags,
            note=f"Executed {decoded.assembly}: {reg_name} 0x{cur:04X} -> 0x{new_value:04X}.",
        )

    # cp r, #3 (0xD8..0xDF) — flags only.
    if 0xD8 <= op <= 0xDF:
        cur = _d7_read_word2(before_cpu, target)
        if cur is None:
            return _blocked_unknown()
        flags = _compute_subtract_flags("word", cur, op & 0x07)
        return _executed_result(
            before_cpu=before_cpu, decoded=decoded, written_registers=("PC",),
            memory_writes=(), after_memory=before_memory, new_pc=new_pc,
            reg_updates=None, flags_updates=flags,
            note=f"Executed {decoded.assembly}: flags = {reg_name} - {op & 0x07}.",
        )

    # ld r, imm16 (0x03) and ALU r, imm16 (0xC8..0xCF).
    if op == 0x03 or (0xC8 <= op <= 0xCF):
        if len(raw) < 5:
            return None
        imm = int.from_bytes(raw[3:5], "little")
        if op == 0x03:  # ld imm16 — no flags
            reg_updates, d7_extra = _d7_write_word2(before_cpu, target, imm)
            if reg_updates is None and d7_extra is None:
                return _blocked_unknown()
            return _executed_result(
                before_cpu=before_cpu, decoded=decoded, written_registers=(reg_name, "PC"),
                memory_writes=(), after_memory=before_memory, new_pc=new_pc, reg_updates=reg_updates,
                extra_cpu_updates=d7_extra,
                note=f"Executed {decoded.assembly}: {reg_name} <- 0x{imm:04X}.",
            )
        cur = _d7_read_word2(before_cpu, target)
        if cur is None:
            return _blocked_unknown()
        alu = ("add", "adc", "sub", "sbc", "and", "xor", "or", "cp")[op & 0x07]
        if alu == "cp":
            flags = _compute_subtract_flags("word", cur, imm)
            return _executed_result(
                before_cpu=before_cpu, decoded=decoded, written_registers=("PC",),
                memory_writes=(), after_memory=before_memory, new_pc=new_pc,
                reg_updates=None, flags_updates=flags,
                note=f"Executed {decoded.assembly}: flags = {reg_name}(0x{cur:04X}) - 0x{imm:04X}.",
            )
        if alu in ("add", "sub"):
            new_value = (cur + imm if alu == "add" else cur - imm) & 0xFFFF
            flags = (_compute_add_flags if alu == "add" else _compute_subtract_flags)("word", cur, imm)
        elif alu in ("and", "or", "xor"):
            new_value = {"and": cur & imm, "or": cur | imm, "xor": cur ^ imm}[alu]
            flags = _compute_logical_flags("word", new_value, half_carry=(alu == "and"))
        else:  # adc/sbc need carry
            if before_cpu.flags.cf is None:
                return _blocked_result(
                    before_cpu=before_cpu, decoded=decoded, status="runtime-state-required",
                    note=f"D7 {decoded.assembly} needs a known carry flag.",
                )
            carry = int(before_cpu.flags.cf)
            new_value = (cur + imm + carry if alu == "adc" else cur - imm - carry) & 0xFFFF
            flags = (_compute_add_flags if alu == "adc" else _compute_subtract_flags)("word", cur, imm + carry)
        reg_updates, d7_extra = _d7_write_word2(before_cpu, target, new_value)
        if reg_updates is None and d7_extra is None:
            return _blocked_unknown()
        return _executed_result(
            before_cpu=before_cpu, decoded=decoded, written_registers=(reg_name, "PC"),
            memory_writes=(), after_memory=before_memory, new_pc=new_pc,
            reg_updates=reg_updates, extra_cpu_updates=d7_extra, flags_updates=flags,
            note=f"Executed {decoded.assembly}: {reg_name} -> 0x{new_value:04X}.",
        )

    # ld R, r (0x88..0x8F) — copy the D7 extended WORD register (source) into a
    # normal current-bank r16 destination (op & 0x07), preserving the dest's
    # high word. Real menu_test div-helper epilogue: `ld HL, QWA` (D7 E2 8B)
    # lifts the remainder out of the high word of XWA.
    if 0x88 <= op <= 0x8F:
        src_value = _d7_read_word2(before_cpu, target)
        if src_value is None:
            return _blocked_unknown()
        dest_index = op & 0x07
        dest_field = REG32_FIELDS[dest_index]
        dest_cur = getattr(before_cpu.regs, dest_field)
        if dest_cur is None:
            return _blocked_result(
                before_cpu=before_cpu, decoded=decoded, status="requires-known-full-register",
                note=(
                    f"D7 {decoded.assembly}: destination {R32[dest_index]} must be known so its "
                    "high word survives the 16-bit copy."
                ),
            )
        dest_name = (decoded.operands or "").split(",")[0].strip() or R32[dest_index]
        new_value = (dest_cur & 0xFFFF0000) | (src_value & 0xFFFF)
        return _executed_result(
            before_cpu=before_cpu, decoded=decoded, written_registers=(dest_name, "PC"),
            memory_writes=(), after_memory=before_memory, new_pc=new_pc,
            reg_updates={dest_field: new_value},
            note=f"Executed {decoded.assembly}: {dest_name} <- {reg_name}=0x{src_value:04X} (word copy).",
        )

    # ld r, R (0x98..0x9F) — copy a normal current-bank r16 (source) INTO the D7
    # extended WORD register (destination), the store mirror of the 0x88..0x8F
    # load form. Real frontier for 3 carts: `ld QWA, WA` (D7 E2 98) / `ld QIZ, WA`
    # (D7 FA 98) / `ld QWA, IZ` (D7 E2 9E) lift a value into a register's high word.
    if 0x98 <= op <= 0x9F:
        src_index = op & 0x07
        src_name, src_value = _extract_register_value(before_cpu, "word", src_index)
        if src_value is None:
            return _blocked_result(
                before_cpu=before_cpu, decoded=decoded, status="requires-known-full-register",
                note=(
                    f"D7 {decoded.assembly}: source {R16[src_index]} must be known before its "
                    "16-bit value can be copied into the extended register slice."
                ),
            )
        reg_updates, d7_extra = _d7_write_word2(before_cpu, target, src_value & 0xFFFF)
        if reg_updates is None and d7_extra is None:
            return _blocked_unknown()
        return _executed_result(
            before_cpu=before_cpu, decoded=decoded, written_registers=(reg_name, "PC"),
            memory_writes=(), after_memory=before_memory, new_pc=new_pc, reg_updates=reg_updates,
            extra_cpu_updates=d7_extra,
            note=f"Executed {decoded.assembly}: {reg_name} <- {R16[src_index]}=0x{src_value:04X} (word copy).",
        )

    # shift/rotate by 4-bit immediate on the D7 word slice (op 0xE8..0xEF,
    # 4 bytes: [D7][reg][op][count]). rlc/rrc/rl/rr/sla/sra/sll/srl. This is the
    # imm-count family (SAFE); the shift-BY-A family 0xF8..0xFF is the broken one.
    # a_test_battle frontier: `srl 3, QBC` (D7 E6 EF 03).
    if 0xE8 <= op <= 0xEF and len(raw) >= 4:
        count = (raw[3] & 0x0F) or 16
        value = _d7_read_word2(before_cpu, target)
        if value is None:
            return _blocked_unknown()
        bits, mask = 16, 0xFFFF
        carry_in = 0
        if op in (0xEA, 0xEB):
            if before_cpu.flags.cf is None:
                return _blocked_result(
                    before_cpu=before_cpu, decoded=decoded, status="requires-known-flags",
                    note=f"D7 {decoded.assembly} rotate-through-carry needs a known CF.",
                )
            carry_in = 1 if before_cpu.flags.cf else 0
        result = value
        carry_out = bool(carry_in) if op in (0xEA, 0xEB) else False
        if count:
            if op == 0xE8:  # rlc
                result = ((value << count) | (value >> (bits - count))) & mask
                carry_out = bool((value >> (bits - count)) & 1)
            elif op == 0xE9:  # rrc
                result = ((value >> count) | (value << (bits - count))) & mask
                carry_out = bool((value >> (count - 1)) & 1)
            elif op == 0xEA:  # rl (through carry)
                r, c = value, carry_in
                for _ in range(count):
                    nc = (r >> (bits - 1)) & 1
                    r = ((r << 1) & mask) | c
                    c = nc
                result, carry_out = r, bool(c)
            elif op == 0xEB:  # rr (through carry)
                r, c = value, carry_in
                for _ in range(count):
                    nc = r & 1
                    r = ((r >> 1) | (c << (bits - 1))) & mask
                    c = nc
                result, carry_out = r, bool(c)
            elif op in (0xEC, 0xEE):  # sla / sll
                result = (value << count) & mask
                carry_out = bool((value >> (bits - count)) & 1)
            elif op == 0xED:  # sra (sign-extending)
                sign = (value >> (bits - 1)) & 1
                result = value >> count
                if sign:
                    result = (result | (((1 << count) - 1) << (bits - count))) & mask
                carry_out = bool((value >> (count - 1)) & 1)
            else:  # 0xEF srl
                result = value >> count
                carry_out = bool((value >> (count - 1)) & 1)
        reg_updates, d7_extra = _d7_write_word2(before_cpu, target, result)
        if reg_updates is None and d7_extra is None:
            return _blocked_unknown()
        flags = {"zf": result == 0, "sf": bool(result & 0x8000), "cf": carry_out,
                 "hf": False, "nf": False}
        return _executed_result(
            before_cpu=before_cpu, decoded=decoded, written_registers=(reg_name, "PC"),
            memory_writes=(), after_memory=before_memory, new_pc=new_pc,
            reg_updates=reg_updates, extra_cpu_updates=d7_extra, flags_updates=flags,
            note=f"Executed {decoded.assembly}: {reg_name} 0x{value:04X} -> 0x{result:04X} (count {count}).",
        )

    # ALU R, r (0x80..0xF7, low nibble 0..7) — `<alu> <normal r16 dest>, <D7 word
    # reg src>`: add(0x80) adc(0x90) sub(0xA0) sbc(0xB0) and(0xC0) xor(0xD0)
    # or(0xE0) cp(0xF0). The D7 extended word register is the source, the current-
    # bank r16 (op & 0x07) the destination. Frontier for 3 carts: `add HL, QWA`
    # (D7 E2 83), `sub HL, QDE` (D7 EA A3), `add IY, QWA` (D7 E2 85).
    _D7_ALU_MAP = {0x80: "add", 0x90: "adc", 0xA0: "sub", 0xB0: "sbc",
                   0xC0: "and", 0xD0: "xor", 0xE0: "or", 0xF0: "cp"}
    if (op & 0xF8) in _D7_ALU_MAP:
        alu = _D7_ALU_MAP[op & 0xF8]
        src_value = _d7_read_word2(before_cpu, target)
        if src_value is None:
            return _blocked_unknown()
        dest_index = op & 0x07
        dest_field = REG32_FIELDS[dest_index]
        dest_cur = getattr(before_cpu.regs, dest_field)
        if dest_cur is None:
            return _blocked_result(
                before_cpu=before_cpu, decoded=decoded, status="requires-known-full-register",
                note=(
                    f"D7 {decoded.assembly}: destination {R16[dest_index]} must be known so its "
                    "high word survives the 16-bit ALU op."
                ),
            )
        dest_val = dest_cur & 0xFFFF
        if alu in ("adc", "sbc"):
            if before_cpu.flags.cf is None:
                return _blocked_result(
                    before_cpu=before_cpu, decoded=decoded, status="runtime-state-required",
                    note=f"D7 {decoded.assembly} needs a known carry flag.",
                )
            carry = int(before_cpu.flags.cf)
        else:
            carry = 0
        if alu == "add":
            result = (dest_val + src_value) & 0xFFFF
            flags = _compute_add_flags("word", dest_val, src_value)
        elif alu == "adc":
            result = (dest_val + src_value + carry) & 0xFFFF
            flags = _compute_add_flags("word", dest_val, src_value + carry)
        elif alu == "sub":
            result = (dest_val - src_value) & 0xFFFF
            flags = _compute_subtract_flags("word", dest_val, src_value)
        elif alu == "sbc":
            result = (dest_val - src_value - carry) & 0xFFFF
            flags = _compute_subtract_flags("word", dest_val, src_value + carry)
        elif alu == "and":
            result = dest_val & src_value
            flags = _compute_logical_flags("word", result, half_carry=True)
        elif alu == "xor":
            result = dest_val ^ src_value
            flags = _compute_logical_flags("word", result)
        elif alu == "or":
            result = dest_val | src_value
            flags = _compute_logical_flags("word", result)
        else:  # cp — flags only, no write-back
            flags = _compute_subtract_flags("word", dest_val, src_value)
            return _executed_result(
                before_cpu=before_cpu, decoded=decoded, written_registers=("PC",),
                memory_writes=(), after_memory=before_memory, new_pc=new_pc,
                reg_updates=None, flags_updates=flags,
                note=f"Executed {decoded.assembly}: flags = {R16[dest_index]}(0x{dest_val:04X}) - {reg_name}(0x{src_value:04X}).",
            )
        new32 = (dest_cur & 0xFFFF0000) | result
        return _executed_result(
            before_cpu=before_cpu, decoded=decoded, written_registers=(R16[dest_index], "PC"),
            memory_writes=(), after_memory=before_memory, new_pc=new_pc,
            reg_updates={dest_field: new32}, flags_updates=flags,
            note=f"Executed {decoded.assembly}: {R16[dest_index]} 0x{dest_val:04X} {alu} {reg_name}=0x{src_value:04X} -> 0x{result:04X}.",
        )

    return _blocked_result(
        before_cpu=before_cpu, decoded=decoded, status="unsupported-decoded-instruction",
        note=f"D7 sub-op 0x{op:02X} ({decoded.assembly}) is decoded but not executed yet.",
    )


def _try_execute_c7_extended_register(
    view: NgpcFetchView,
    before_cpu: NgpcCpuState,
    before_memory: dict[int, int],
    decoded: DecodeResult,
) -> ExecutionResult | None:
    """Execute the C7 extended-register prefix family (current-bank slices).

    The C7 prefix carries an 8-bit register selector. Codes 0xE0..0xFF name
    byte slices of the eight current-bank 32-bit registers (e.g. QC = bits
    16..23 of XBC, QIZH = bits 24..31 of XIZ). These map directly onto the
    modeled R32 state, so we execute LD / ALU / CP / INC / DEC for real.
    The banked backing store added later also lets the explicit-bank and
    previous-bank byte selectors participate honestly for byte-granular load,
    store, and stack traffic.
    """
    raw = decoded.raw_bytes
    if raw is None or len(raw) < 3 or raw[0] != 0xC7:
        return None

    reg_byte = raw[1]
    op = raw[2]
    reg_name = C7_REGISTER_NAMES[reg_byte]
    slice_target = c7_current_bank_slice(reg_byte)
    alt_bank_target = _resolve_c7_alt_bank_target(before_cpu, reg_byte)
    target_r32_name: str | None = None
    if slice_target is not None:
        r32_index, byte_pos = slice_target
        ext_value = _extract_byte_slice(before_cpu, r32_index, byte_pos)
        target_r32_name = R32[r32_index]
    elif alt_bank_target is not None:
        bank_index, r32_index, byte_pos = alt_bank_target
        ext_value = _extract_banked_core_byte(before_cpu, bank_index, r32_index, byte_pos)
        target_r32_name = f"bank{bank_index}:{R32[r32_index]}"
    else:
        return _blocked_result(
            before_cpu=before_cpu,
            decoded=decoded,
            status="unsupported-decoded-instruction",
            note=(
                f"C7 {decoded.assembly}: register code 0x{reg_byte:02X} does not map "
                "to a modeled current-bank, previous-bank or explicit-bank byte register."
            ),
        )
    new_pc = decoded.next_sequential_pc

    def _blocked(status: str, note: str) -> ExecutionResult:
        return _blocked_result(
            before_cpu=before_cpu, decoded=decoded, status=status, note=note,
        )

    def _need_ext() -> ExecutionResult | None:
        if ext_value is None:
            return _blocked(
                "requires-known-source-register",
                f"C7 {decoded.assembly} needs {reg_name} "
                f"(byte {byte_pos} of {target_r32_name}) to be modeled.",
            )
        return None

    def _write_ext(new_value: int) -> tuple[dict[str, int] | None, dict[str, object] | None, str]:
        if slice_target is not None:
            return (
                _build_byte_slice_update(before_cpu, r32_index, byte_pos, new_value),
                None,
                R32[r32_index],
            )
        assert alt_bank_target is not None
        reg_updates, new_banks = _build_banked_core_byte_update(
            before_cpu, bank_index, r32_index, byte_pos, new_value,
        )
        written_name = f"{R32[r32_index]}@bank{bank_index}"
        extra_updates = None if new_banks is None else {"register_banks": new_banks}
        return reg_updates, extra_updates, written_name

    def _execute_carry_flag_c7(bit_index: int) -> ExecutionResult:
        if (blocked := _need_ext()) is not None:
            return blocked
        if bit_index >= 8:
            if op in (0x24, 0x2C):
                return _executed_result(
                    before_cpu=before_cpu,
                    decoded=decoded,
                    written_registers=("PC",),
                    memory_writes=(),
                    after_memory=before_memory,
                    new_pc=new_pc,
                    reg_updates=None,
                    note=(
                        f"Executed {decoded.assembly}: byte C7 STCF with bit index {bit_index} leaves "
                        "the selected slice unchanged per the local Toshiba note."
                    ),
                )
            return _blocked(
                "silicon-undefined",
                f"{decoded.assembly} uses byte bit index {bit_index}, which is undefined in the local "
                "Toshiba table for this carry-flag form.",
            )

        mnemonic = decoded.mnemonic
        bit_value = (ext_value >> bit_index) & 1

        if mnemonic in {"andcf", "orcf", "xorcf", "stcf"} and before_cpu.flags.cf is None:
            return _blocked(
                "runtime-state-required",
                f"{decoded.assembly} needs the carry flag known in the current CPU state.",
            )

        if mnemonic == "ldcf":
            return _executed_result(
                before_cpu=before_cpu,
                decoded=decoded,
                written_registers=("PC",),
                memory_writes=(),
                after_memory=before_memory,
                new_pc=new_pc,
                reg_updates=None,
                flags_updates={"cf": bool(bit_value)},
                note=f"Executed {decoded.assembly}: CF <- bit {bit_index} of {reg_name}.",
            )

        carry = int(before_cpu.flags.cf)
        if mnemonic == "andcf":
            return _executed_result(
                before_cpu=before_cpu,
                decoded=decoded,
                written_registers=("PC",),
                memory_writes=(),
                after_memory=before_memory,
                new_pc=new_pc,
                reg_updates=None,
                flags_updates={"cf": bool(carry & bit_value)},
                note=f"Executed {decoded.assembly}: CF <- C AND {reg_name}<{bit_index}>.",
            )
        if mnemonic == "orcf":
            return _executed_result(
                before_cpu=before_cpu,
                decoded=decoded,
                written_registers=("PC",),
                memory_writes=(),
                after_memory=before_memory,
                new_pc=new_pc,
                reg_updates=None,
                flags_updates={"cf": bool(carry | bit_value)},
                note=f"Executed {decoded.assembly}: CF <- C OR {reg_name}<{bit_index}>.",
            )
        if mnemonic == "xorcf":
            return _executed_result(
                before_cpu=before_cpu,
                decoded=decoded,
                written_registers=("PC",),
                memory_writes=(),
                after_memory=before_memory,
                new_pc=new_pc,
                reg_updates=None,
                flags_updates={"cf": bool(carry ^ bit_value)},
                note=f"Executed {decoded.assembly}: CF <- C XOR {reg_name}<{bit_index}>.",
            )

        reg_updates, extra_cpu_updates, written_name = _write_ext(
            (ext_value & ~(1 << bit_index)) | (carry << bit_index)
        )
        if reg_updates is None and extra_cpu_updates is None:
            return _blocked(
                "requires-known-full-register",
                f"{decoded.assembly} needs {target_r32_name} fully known before the selected slice can be "
                "written back honestly.",
            )
        return _executed_result(
            before_cpu=before_cpu,
            decoded=decoded,
            written_registers=("PC", written_name),
            memory_writes=(),
            after_memory=before_memory,
            new_pc=new_pc,
            reg_updates=reg_updates,
            flags_updates=None,
            extra_cpu_updates=extra_cpu_updates,
            note=f"Executed {decoded.assembly}: {reg_name}<{bit_index}> <- CF.",
        )

    in_reg_range = (0x40 <= op < 0xC8) or (0xD0 <= op < 0xE8) or (0xF0 <= op < 0xF8)
    if in_reg_range:
        hi = op & 0xF8
        # ----- register/register byte family : the "other" operand is a
        # standard current-bank R8 (op & 7), the extended reg is the C7 slice.
        if hi in (0x80, 0x88, 0x90, 0x98, 0xA0, 0xB0, 0xC0, 0xD0, 0xE0, 0xF0):
            r8_index = op & 0x07
            r8_name, r8_value = _extract_register_value(before_cpu, "byte", r8_index)

            if hi == 0x88:  # LD R, r  → R8 = ext
                if (blocked := _need_ext()) is not None:
                    return blocked
                _, reg_updates = _build_register_update(before_cpu, "byte", r8_index, ext_value)
                if reg_updates is None and extra_cpu_updates is None:
                    return _blocked(
                        "requires-known-full-register",
                        f"ld {r8_name}, {reg_name} needs {R32[r8_index // 2]} fully known.",
                    )
                return _executed_result(
                    before_cpu=before_cpu, decoded=decoded,
                    written_registers=("PC", r8_name), memory_writes=(),
                    after_memory=before_memory, new_pc=new_pc,
                    reg_updates=reg_updates, flags_updates=None,
                    note=f"Executed ld {r8_name}, {reg_name}: {r8_name}=0x{ext_value:02X}.",
                )

            if hi == 0x98:  # LD r, R  → ext = R8
                if r8_value is None:
                    return _blocked(
                        "requires-known-source-register",
                        f"ld {reg_name}, {r8_name} needs {r8_name} "
                        f"(owner {R32[r8_index // 2]}) modeled.",
                    )
                reg_updates, extra_cpu_updates, written_name = _write_ext(r8_value)
                # A banked (non-current-bank) write returns reg_updates=None and
                # lands in extra_cpu_updates -- only block if BOTH are None.
                if reg_updates is None and extra_cpu_updates is None:
                    return _blocked(
                        "requires-known-full-register",
                        f"ld {reg_name}, {r8_name} needs {target_r32_name} fully known.",
                    )
                return _executed_result(
                    before_cpu=before_cpu, decoded=decoded,
                    written_registers=("PC", written_name), memory_writes=(),
                    after_memory=before_memory, new_pc=new_pc,
                    reg_updates=reg_updates, flags_updates=None, extra_cpu_updates=extra_cpu_updates,
                    note=f"Executed ld {reg_name}, {r8_name}: {reg_name}=0x{r8_value:02X}.",
                )

            # Arithmetic / logical / compare with R8 as destination/accumulator.
            if (blocked := _need_ext()) is not None:
                return blocked
            if r8_value is None:
                return _blocked(
                    "requires-known-source-register",
                    f"{decoded.mnemonic} {r8_name}, {reg_name} needs {r8_name} "
                    f"(owner {R32[r8_index // 2]}) modeled.",
                )
            carry = before_cpu.flags.cf
            if hi in (0x90, 0xB0) and carry is None:  # ADC / SBC
                return _blocked(
                    "runtime-state-required",
                    f"{decoded.mnemonic} {r8_name}, {reg_name} needs the carry flag known.",
                )
            if hi == 0x80:    # ADD
                result = (r8_value + ext_value) & 0xFF
                flags = _compute_add_flags("byte", r8_value, ext_value)
            elif hi == 0x90:  # ADC
                result = (r8_value + ext_value + int(carry)) & 0xFF
                flags = _compute_add_flags("byte", r8_value, ext_value + int(carry))
            elif hi == 0xA0:  # SUB
                result = (r8_value - ext_value) & 0xFF
                flags = _compute_subtract_flags("byte", r8_value, ext_value)
            elif hi == 0xB0:  # SBC
                result = (r8_value - ext_value - int(carry)) & 0xFF
                flags = _compute_subtract_flags("byte", r8_value, ext_value + int(carry))
            elif hi == 0xC0:  # AND
                result = r8_value & ext_value
                flags = _compute_logical_flags("byte", result, half_carry=True)
            elif hi == 0xD0:  # XOR
                result = r8_value ^ ext_value
                flags = _compute_logical_flags("byte", result)
            elif hi == 0xE0:  # OR
                result = r8_value | ext_value
                flags = _compute_logical_flags("byte", result)
            else:             # 0xF0 CP — flags only, no write
                flags = _compute_subtract_flags("byte", r8_value, ext_value)
                return _executed_result(
                    before_cpu=before_cpu, decoded=decoded,
                    written_registers=("PC",), memory_writes=(),
                    after_memory=before_memory, new_pc=new_pc,
                    reg_updates=None, flags_updates=flags,
                    note=f"Executed cp {r8_name}, {reg_name}. Flags = "
                         f"0x{r8_value:02X} - 0x{ext_value:02X}.",
                )
            _, reg_updates = _build_register_update(before_cpu, "byte", r8_index, result)
            if reg_updates is None and extra_cpu_updates is None:
                return _blocked(
                    "requires-known-full-register",
                    f"{decoded.mnemonic} {r8_name}, {reg_name} needs {R32[r8_index // 2]} known.",
                )
            return _executed_result(
                before_cpu=before_cpu, decoded=decoded,
                written_registers=("PC", r8_name), memory_writes=(),
                after_memory=before_memory, new_pc=new_pc,
                reg_updates=reg_updates, flags_updates=flags,
                note=f"Executed {decoded.mnemonic} {r8_name}, {reg_name}: {r8_name}=0x{result:02X}.",
            )

        if hi in (0x60, 0x68):  # INC / DEC #3, r  — CF preserved (Toshiba RMW rule)
            if (blocked := _need_ext()) is not None:
                return blocked
            n = (op & 0x07) or 8
            if hi == 0x60:
                result = (ext_value + n) & 0xFF
                flags = _compute_add_flags("byte", ext_value, n)
            else:
                result = (ext_value - n) & 0xFF
                flags = _compute_subtract_flags("byte", ext_value, n)
            flags.pop("cf", None)  # INC/DEC on a register preserve carry.
            reg_updates, extra_cpu_updates, written_name = _write_ext(result)
            if reg_updates is None and extra_cpu_updates is None:
                return _blocked(
                    "requires-known-full-register",
                    f"{decoded.mnemonic} {n}, {reg_name} needs {target_r32_name} fully known.",
                )
            return _executed_result(
                before_cpu=before_cpu, decoded=decoded,
                written_registers=("PC", written_name), memory_writes=(),
                after_memory=before_memory, new_pc=new_pc,
                reg_updates=reg_updates, flags_updates=flags, extra_cpu_updates=extra_cpu_updates,
                note=f"Executed {decoded.mnemonic} {n}, {reg_name}: {reg_name}=0x{result:02X}.",
            )

        if hi == 0xA8:  # LD r, #3
            value = op & 0x07
            reg_updates, extra_cpu_updates, written_name = _write_ext(value)
            if reg_updates is None and extra_cpu_updates is None:
                return _blocked(
                    "requires-known-full-register",
                    f"ld {reg_name}, {value} needs {target_r32_name} fully known.",
                )
            return _executed_result(
                before_cpu=before_cpu, decoded=decoded,
                written_registers=("PC", written_name), memory_writes=(),
                after_memory=before_memory, new_pc=new_pc,
                reg_updates=reg_updates, flags_updates=None, extra_cpu_updates=extra_cpu_updates,
                note=f"Executed ld {reg_name}, {value}: {reg_name}=0x{value:02X}.",
            )

        if hi == 0xD8:  # CP r, #3 — flags only
            if (blocked := _need_ext()) is not None:
                return blocked
            imm = op & 0x07
            flags = _compute_subtract_flags("byte", ext_value, imm)
            return _executed_result(
                before_cpu=before_cpu, decoded=decoded,
                written_registers=("PC",), memory_writes=(),
                after_memory=before_memory, new_pc=new_pc,
                reg_updates=None, flags_updates=flags,
                note=f"Executed cp {reg_name}, {imm}. Flags = 0x{ext_value:02X} - 0x{imm:02X}.",
            )

        return _blocked(
            "unsupported-decoded-instruction",
            f"C7 sub-op 0x{op:02X} ({decoded.assembly}) is decoded but not executed yet.",
        )

    if 0x20 <= op <= 0x24 and len(raw) >= 4:
        return _execute_carry_flag_c7(raw[3] & 0x0F)

    if 0x28 <= op <= 0x2C:
        _, a_value = _extract_register_value(before_cpu, "byte", 1)
        if a_value is None:
            return _blocked(
                "requires-known-full-register",
                f"{decoded.assembly} needs A known to derive its dynamic bit index honestly.",
            )
        return _execute_carry_flag_c7(a_value & 0x0F)

    if op in (0x2E, 0x2F) and len(raw) >= 4:
        control_info = _read_control_register_value(before_cpu, raw[3])
        if control_info is None:
            control_name = control_register_name(raw[3])
            direction = "write" if op == 0x2E else "read"
            return _blocked(
                "unmodeled-control-register",
                f"{decoded.assembly} targets TLCS-900/H control register {control_name} through the "
                f"C7 byte-slice form. The current executor does not model {direction} access to that "
                "control-register number yet.",
            )
        control_name, size_kind, control_value = control_info
        if size_kind != "byte":
            return _blocked(
                "silicon-undefined",
                f"{decoded.assembly} targets non-byte control register {control_name} through a C7 "
                "byte-slice form, which the local Toshiba table marks undefined.",
            )

        if op == 0x2E:
            if (blocked := _need_ext()) is not None:
                return blocked
            write_result = _write_control_register_value(before_cpu, raw[3], ext_value)
            assert write_result is not None
            _, extra_cpu_updates = write_result
            return _executed_result(
                before_cpu=before_cpu,
                decoded=decoded,
                written_registers=("PC", control_name),
                memory_writes=(),
                after_memory=before_memory,
                new_pc=new_pc,
                reg_updates=None,
                extra_cpu_updates=extra_cpu_updates,
                note=f"Executed {decoded.assembly}: {control_name}=0x{ext_value:02X}.",
                cycles_consumed=LDC_CONTROL_REGISTER_CYCLES,
            )

        if control_value is None:
            return _blocked(
                "requires-known-control-register",
                f"{decoded.assembly} needs TLCS-900/H control register {control_name} modeled "
                "before it can be read back honestly.",
            )
        reg_updates, extra_cpu_updates, written_name = _write_ext(control_value)
        if reg_updates is None and extra_cpu_updates is None:
            return _blocked(
                "requires-known-full-register",
                f"{decoded.assembly} needs {target_r32_name} fully known before the selected slice "
                "can be written back honestly.",
            )
        return _executed_result(
            before_cpu=before_cpu,
            decoded=decoded,
            written_registers=("PC", written_name),
            memory_writes=(),
            after_memory=before_memory,
            new_pc=new_pc,
            reg_updates=reg_updates,
            extra_cpu_updates=extra_cpu_updates,
            note=f"Executed {decoded.assembly}: {reg_name}=0x{control_value:02X} read from {control_name}.",
            cycles_consumed=LDC_CONTROL_REGISTER_CYCLES,
        )

    if 0xE8 <= op <= 0xEF and len(raw) >= 4:
        if (blocked := _need_ext()) is not None:
            return blocked
        # `#4 == 0` means SIXTEEN shifts (datasheet Note 1), and the cost is
        # `3 + n/4`, not a flat constant. The C7/D7/E7 extended-register
        # handlers each carried their own copy of both bugs.
        count = (raw[3] & 0x0F) or 16
        shift_eval = _evaluate_shift_rotate(
            before_cpu=before_cpu,
            decoded=decoded,
            size_kind="byte",
            reg_value=ext_value,
            op=op,
            count=count,
        )
        if isinstance(shift_eval, ExecutionResult):
            return shift_eval
        result, flags = shift_eval
        reg_updates, extra_cpu_updates, written_name = _write_ext(result)
        if reg_updates is None and extra_cpu_updates is None:
            return _blocked(
                "requires-known-full-register",
                f"{decoded.mnemonic} {count}, {reg_name} needs {target_r32_name} fully known.",
            )
        return _executed_result(
            before_cpu=before_cpu, decoded=decoded,
            written_registers=("PC", written_name), memory_writes=(),
            after_memory=before_memory, new_pc=new_pc,
            reg_updates=reg_updates, flags_updates=flags, extra_cpu_updates=extra_cpu_updates,
            # Shifts cost `3 + n/4` -- Toshiba list (8) gives BOTH the `#4` and the
            # `A` forms that state. The extended-register handlers were each
            # billing a flat constant of their own.
            cycles_consumed=_shift_imm_register_cycles(count),
            note=(
                f"Executed {decoded.mnemonic} {count}, {reg_name} using the current C7 byte-slice "
                f"executor. {reg_name}=0x{result:02X}."
            ),
        )

    if 0xF8 <= op <= 0xFF:
        if (blocked := _need_ext()) is not None:
            return blocked
        _, a_value = _extract_register_value(before_cpu, "byte", 1)
        if a_value is None:
            return _blocked(
                "requires-known-full-register",
                f"{decoded.mnemonic} A, {reg_name} needs A known to source the shift count honestly.",
            )
        # `#4 == 0` means SIXTEEN shifts (datasheet Note 1), and the cost is
        # `3 + n/4`, not a flat constant. The C7/D7/E7 extended-register
        # handlers each carried their own copy of both bugs.
        count = (a_value & 0x0F) or 16
        shift_eval = _evaluate_shift_rotate(
            before_cpu=before_cpu,
            decoded=decoded,
            size_kind="byte",
            reg_value=ext_value,
            op=op - 0x10,
            count=count,
        )
        if isinstance(shift_eval, ExecutionResult):
            return shift_eval
        result, flags = shift_eval
        reg_updates, extra_cpu_updates, written_name = _write_ext(result)
        if reg_updates is None and extra_cpu_updates is None:
            return _blocked(
                "requires-known-full-register",
                f"{decoded.mnemonic} A, {reg_name} needs {target_r32_name} fully known.",
            )
        return _executed_result(
            before_cpu=before_cpu, decoded=decoded,
            written_registers=("PC", written_name), memory_writes=(),
            after_memory=before_memory, new_pc=new_pc,
            reg_updates=reg_updates, flags_updates=flags, extra_cpu_updates=extra_cpu_updates,
            cycles_consumed=_shift_imm_register_cycles(count),
            note=(
                f"Executed {decoded.mnemonic} A, {reg_name} using count=A&0x0F=0x{count:X}. "
                f"{reg_name}=0x{result:02X}."
            ),
        )

    if op == 0x04:
        if (blocked := _need_ext()) is not None:
            return blocked
        return _execute_push_bytes(
            view=view,
            before_cpu=before_cpu,
            before_memory=before_memory,
            decoded=decoded,
            data=bytes((ext_value,)),
            note=(
                f"Executed push {reg_name} using the C7 byte-slice executor. The selected "
                "slice byte was written to the writable stack model."
            ),
        )

    if op == 0x05:
        xsp = before_cpu.regs.xsp
        if xsp is None:
            return _blocked(
                "requires-known-stack-pointer",
                "This instruction needs XSP, but the current CPU state still leaves it unknown.",
            )
        data = _read_runtime_bytes(view, before_memory, _mask_address(xsp), 1)
        if data is None:
            return _blocked(
                "stack-data-unavailable",
                "This POP-like instruction needs a readable byte at the current XSP.",
            )
        reg_updates, extra_cpu_updates, written_name = _write_ext(data[0])
        if reg_updates is None and extra_cpu_updates is None:
            return _blocked(
                "requires-known-full-register",
                f"pop {reg_name} needs {target_r32_name} fully known.",
            )
        if reg_updates is None:
            reg_updates = {}
        reg_updates["xsp"] = (xsp + 1) & 0xFFFFFFFF
        return _executed_result(
            before_cpu=before_cpu, decoded=decoded,
            written_registers=("PC", written_name, "XSP"), memory_writes=(),
            after_memory=before_memory, new_pc=new_pc,
            reg_updates=reg_updates, flags_updates=None, extra_cpu_updates=extra_cpu_updates,
            note=(
                f"Executed pop {reg_name} using the C7 byte-slice executor. {reg_name} was "
                "loaded from the writable stack model or read bus, and XSP advanced by 1."
            ),
        )

    if op in (0x06, 0x07):
        if (blocked := _need_ext()) is not None:
            return blocked
        result, flags = _evaluate_cpl_neg(size_kind="byte", reg_value=ext_value, op=op)
        reg_updates, extra_cpu_updates, written_name = _write_ext(result)
        if reg_updates is None and extra_cpu_updates is None:
            return _blocked(
                "requires-known-full-register",
                f"{decoded.mnemonic} {reg_name} needs {target_r32_name} fully known.",
            )
        return _executed_result(
            before_cpu=before_cpu, decoded=decoded,
            written_registers=("PC", written_name), memory_writes=(),
            after_memory=before_memory, new_pc=new_pc,
            reg_updates=reg_updates, flags_updates=flags, extra_cpu_updates=extra_cpu_updates,
            note=f"Executed {decoded.mnemonic} {reg_name}: {reg_name}=0x{result:02X}.",
        )

    if op == 0x10:
        if (blocked := _need_ext()) is not None:
            return blocked
        if (
            before_cpu.flags.cf is None
            or before_cpu.flags.hf is None
            or before_cpu.flags.nf is None
        ):
            return _blocked(
                "requires-known-flags",
                f"daa {reg_name} needs the C, H, and N flags known.",
            )
        result, flags = _evaluate_daa(
            reg_value=ext_value,
            carry_in=before_cpu.flags.cf,
            half_carry_in=before_cpu.flags.hf,
            subtract_in=before_cpu.flags.nf,
        )
        reg_updates, extra_cpu_updates, written_name = _write_ext(result)
        if reg_updates is None and extra_cpu_updates is None:
            return _blocked(
                "requires-known-full-register",
                f"daa {reg_name} needs {target_r32_name} fully known.",
            )
        return _executed_result(
            before_cpu=before_cpu, decoded=decoded,
            written_registers=("PC", written_name), memory_writes=(),
            after_memory=before_memory, new_pc=new_pc,
            reg_updates=reg_updates, flags_updates=flags, extra_cpu_updates=extra_cpu_updates,
            note=f"Executed daa {reg_name}: {reg_name}=0x{result:02X}.",
        )

    if op == 0x0D:
        return _blocked(
            "silicon-undefined",
            f"{decoded.assembly} is not defined on C7 byte-slice register forms in the local "
            "Toshiba table.",
        )

    if op in (0x12, 0x13):
        return _blocked(
            "silicon-undefined",
            f"{decoded.assembly} is not defined on byte-size registers in the local Toshiba table.",
        )

    # ----- immediate ALU family : C7 <reg> {03,C8..CF} imm8 -----
    _C7_IMM_ALU = {0xC8: "add", 0xC9: "adc", 0xCA: "sub", 0xCB: "sbc",
                   0xCC: "and", 0xCD: "xor", 0xCE: "or", 0xCF: "cp", 0x03: "ld"}
    if op in _C7_IMM_ALU and len(raw) >= 4:
        imm = raw[3]
        if op == 0x03:  # LD r, #imm8
            reg_updates, extra_cpu_updates, written_name = _write_ext(imm)
            # A banked (non-current-bank) write puts the result in extra_cpu_updates
            # (register_banks), not reg_updates -- so reg_updates being None is normal
            # there. Only block if BOTH are None (owner truly unknown). Writing an
            # immediate byte never needs the owner's other bytes known.
            if reg_updates is None and extra_cpu_updates is None:
                return _blocked(
                    "requires-known-full-register",
                    f"ld {reg_name}, 0x{imm:02X} needs {target_r32_name} fully known.",
                )
            return _executed_result(
                before_cpu=before_cpu, decoded=decoded,
                written_registers=("PC", written_name), memory_writes=(),
                after_memory=before_memory, new_pc=new_pc,
                reg_updates=reg_updates, flags_updates=None, extra_cpu_updates=extra_cpu_updates,
                note=f"Executed ld {reg_name}, 0x{imm:02X}.",
            )
        if (blocked := _need_ext()) is not None:
            return blocked
        carry = before_cpu.flags.cf
        if op in (0xC9, 0xCB) and carry is None:  # ADC / SBC
            return _blocked(
                "runtime-state-required",
                f"{_C7_IMM_ALU[op]} {reg_name}, 0x{imm:02X} needs the carry flag known.",
            )
        if op == 0xC8:    # ADD
            result = (ext_value + imm) & 0xFF
            flags = _compute_add_flags("byte", ext_value, imm)
        elif op == 0xC9:  # ADC
            result = (ext_value + imm + int(carry)) & 0xFF
            flags = _compute_add_flags("byte", ext_value, imm + int(carry))
        elif op == 0xCA:  # SUB
            result = (ext_value - imm) & 0xFF
            flags = _compute_subtract_flags("byte", ext_value, imm)
        elif op == 0xCB:  # SBC
            result = (ext_value - imm - int(carry)) & 0xFF
            flags = _compute_subtract_flags("byte", ext_value, imm + int(carry))
        elif op == 0xCC:  # AND
            result = ext_value & imm
            flags = _compute_logical_flags("byte", result, half_carry=True)
        elif op == 0xCD:  # XOR
            result = ext_value ^ imm
            flags = _compute_logical_flags("byte", result)
        elif op == 0xCE:  # OR
            result = ext_value | imm
            flags = _compute_logical_flags("byte", result)
        else:             # 0xCF CP — flags only
            flags = _compute_subtract_flags("byte", ext_value, imm)
            return _executed_result(
                before_cpu=before_cpu, decoded=decoded,
                written_registers=("PC",), memory_writes=(),
                after_memory=before_memory, new_pc=new_pc,
                reg_updates=None, flags_updates=flags,
                note=f"Executed cp {reg_name}, 0x{imm:02X}. Flags = "
                     f"0x{ext_value:02X} - 0x{imm:02X}.",
            )
        reg_updates, extra_cpu_updates, written_name = _write_ext(result)
        if reg_updates is None and extra_cpu_updates is None:
            return _blocked(
                "requires-known-full-register",
                f"{_C7_IMM_ALU[op]} {reg_name}, 0x{imm:02X} needs {target_r32_name} fully known.",
            )
        return _executed_result(
            before_cpu=before_cpu, decoded=decoded,
            written_registers=("PC", written_name), memory_writes=(),
            after_memory=before_memory, new_pc=new_pc,
            reg_updates=reg_updates, flags_updates=flags, extra_cpu_updates=extra_cpu_updates,
            note=f"Executed {_C7_IMM_ALU[op]} {reg_name}, 0x{imm:02X}: {reg_name}=0x{result:02X}.",
        )

    return _blocked(
        "unsupported-decoded-instruction",
        f"C7 sub-op 0x{op:02X} ({decoded.assembly}) is decoded but not executed yet.",
    )


# --- SWI 1 (BIOS SYSTEM_CALL) HLE dispatch -------------------------------
#
# On NGPC, `swi 1` is the BIOS system-call trap. The real SNK BIOS reads the
# vector index from RW3 (the W byte of the bank-3 register file) and jumps to
# the matching handler; NeoPop replicates this exactly with
#   pc = loadL(0xFFFE00 + (rCodeB(0x31) << 2))   [TLCS900h_interpret_single.c]
# where rCodeB(0x31) == RW3. The per-vector side effects below are the
# reverse-engineered SNK BIOS behaviour transcribed from the NeoPop Core
# (bios.c vectable[] + biosHLE.c). Cross-checked against the cosim oracle:
# Metal Slug's `swi 1` @0x200012 vectors to 0xFF1030 (index 1 = CLOCKGEARSET),
# pushes the return address (SP 0x6C00->0x6BFC) and returns to 0x200013 with no
# cartridge-visible state change -- exactly the net effect our HLE produces.
#
# We collapse the BIOS handler into a single HLE step (NeoPop spends two trace
# steps: the swi itself, then the `0x1F` iBIOSHLE marker at the vector). The
# NET post-return CPU/memory state is identical; only an instruction-index
# aligned trace (cosim) skews by one step per swi.
_SWI1_VECT_SHUTDOWN = 0x00
_SWI1_VECT_CLOCKGEARSET = 0x01
_SWI1_VECT_RTCGET = 0x02
_SWI1_VECT_INTLVSET = 0x04
_SWI1_VECT_SYSFONTSET = 0x05
_SWI1_VECT_FLASHWRITE = 0x06

# Vectors that touch no cartridge-visible state on our reference model, so PC
# simply advances (matches NeoPop's HLE which RETs without side effects):
#   1  CLOCKGEARSET  clock gear only scales wall-clock speed; our model runs at
#                    reference speed, so it is a documented no-op.
#   3/10/12/15       unmapped/unknown BIOS entries that RET immediately.
#   14 GEMODESET     TODO/no-op in NeoPop; no reference-visible state change.
_SWI1_NOOP_VECTORS = frozenset({0x01, 0x03, 0x0A, 0x0C, 0x0E, 0x0F})

# Vectors that return SYS_SUCCESS in RA3 (=0) and otherwise no-op on our model
# (flash/alarm/comms-init calls whose real work our append-only save design or
# reference model does not need to perform):
#   7  FLASHALLERS   8  FLASHERS      9  ALARMSET
#   11 ALARMDOWNSET  13 FLASHPROTECT  16 COMINIT
_SWI1_SUCCESS_VECTORS = frozenset({0x07, 0x08, 0x09, 0x0B, 0x0D, 0x10})

# BIOS calls whose real side effect is not modelled yet. We keep the honest
# PC-advance stub (no faked state) but name the vector so the gap-filling
# workflow knows exactly which handler a ROM is waiting on. RTCGET needs a host
# clock injection design; SYSFONTSET needs a font asset decision; FLASHWRITE
# needs the flash overlay subsystem. Comms vectors (0x11..0x1A) also land here.
_SWI1_VECT_NAMES = {
    0x00: "SHUTDOWN",
    0x01: "CLOCKGEARSET",
    0x02: "RTCGET",
    0x03: "(unknown-3)",
    0x04: "INTLVSET",
    0x05: "SYSFONTSET",
    0x06: "FLASHWRITE",
    0x07: "FLASHALLERS",
    0x08: "FLASHERS",
    0x09: "ALARMSET",
    0x0A: "(unknown-10)",
    0x0B: "ALARMDOWNSET",
    0x0C: "(unknown-12)",
    0x0D: "FLASHPROTECT",
    0x0E: "GEMODESET",
    0x0F: "(unknown-15)",
    0x10: "COMINIT",
    0x11: "COMSENDSTART",
    0x12: "COMRECIVESTART",
    0x13: "COMCREATEDATA",
    0x14: "COMGETDATA",
    0x15: "COMONRTS",
    0x16: "COMOFFRTS",
    0x17: "COMSENDSTATUS",
    0x18: "COMRECIVESTATUS",
    0x19: "COMCREATEBUFDATA",
    0x1A: "COMGETBUFDATA",
}

# INTLVSET (VECT_INTLVSET) maps an interrupt source (RC3) to a priority-level
# nibble written into the TLCS-900 interrupt-controller I/O registers. Layout
# transcribed from NeoPop biosHLE.c: (io_address, high_nibble?) per source.
#   src 0 RTC alarm | 1 Z80 | 2..5 timer0..3 | 6..9 DMA0..3
_SWI1_INTLVSET_TABLE = {
    0x00: (0x0070, False),
    0x01: (0x0071, True),
    0x02: (0x0073, False),
    0x03: (0x0073, True),
    0x04: (0x0074, False),
    0x05: (0x0074, True),
    0x06: (0x0079, False),
    0x07: (0x0079, True),
    0x08: (0x007A, False),
    0x09: (0x007A, True),
}


def _read_bank3_byte(before_cpu: NgpcCpuState, r32_index: int, byte_pos: int) -> int | None:
    """Read one byte of a bank-3 core register (RA3/RW3/RC3/RB3...)."""
    return _extract_banked_core_byte(before_cpu, 3, r32_index, byte_pos)


def _read_bank3_long(before_cpu: NgpcCpuState, r32_index: int) -> int | None:
    """Read a full 32-bit bank-3 core register (XWA3/XBC3/XDE3/XHL3)."""
    banks = _ensure_register_banks(before_cpu)
    slots = banks[3].slots[r32_index * 4 : r32_index * 4 + 4]
    return _banked_owner_value_from_slots(slots)


def _read_bank3_word(before_cpu: NgpcCpuState, r32_index: int) -> int | None:
    """Read the low 16-bit word of a bank-3 core register (WA3/BC3/DE3/HL3)."""
    lo = _extract_banked_core_byte(before_cpu, 3, r32_index, 0)
    hi = _extract_banked_core_byte(before_cpu, 3, r32_index, 1)
    if lo is None or hi is None:
        return None
    return (hi << 8) | lo


def _to_bcd(value: int) -> int:
    return (((value // 10) & 0x0F) << 4) | (value % 10)


# The calendar chip's registers, and what a powered-on console reads back from each
# (core/memory.py seeds the same values -- keep the two in step).
_RTC_REGS = (0x000091, 0x000092, 0x000093, 0x000094, 0x000095, 0x000096, 0x000097)
_RTC_SEED = {0x000091: 0x24, 0x000092: 0x01, 0x000093: 0x01, 0x000097: 0x01}


def _bios_rtc_bcd_bytes(memory: dict[int, int]) -> bytes:
    """The 7 packed-BCD bytes VECT_RTCGET hands back: year(since 2000), month, day,
    hour, minute, second, then ((year & 3) << 4) | weekday.

    ⚡ READ OUT OF THE MACHINE'S OWN CLOCK CHIP (I/O 0x91-0x97), not off the host.
    This used to call `time.localtime()`, copying NeoPop's HLE, and that gave the
    console TWO clocks that disagreed: a game reading the registers saw the emulated
    chip, and the same game asking the BIOS saw the wall clock on the desk. On
    hardware there is only ever one -- the BIOS reads the very same chip, and the
    native core (which runs the real BIOS rather than this shortcut) always did. It
    also made the reference core non-deterministic, which for the half of a
    differential pair whose job is to be reproducible is a defect on its own.

    The registers ARE the layout: 0x97 already reads back weekday with the leap phase
    in its top nibble, so the bytes come straight across.

    ⚠️ This core seeds its clock and leaves it frozen -- it models the chip's power-on
    value, not a running clock (the ticking one lives in the native core). So RTCGET
    here returns a fixed instant. That is a known, deliberate gap, and it is still
    strictly better than reporting a time the rest of the machine disagrees with.
    """
    return bytes(memory.get(reg, _RTC_SEED.get(reg, 0x00)) & 0xFF for reg in _RTC_REGS)


def _swi1_note_prefix(vect: int) -> str:
    name = _SWI1_VECT_NAMES.get(vect, f"0x{vect:02X}")
    return f"SWI 1 (BIOS {name}, RW3=0x{vect:02X})"


def _try_execute_swi(
    view: NgpcFetchView,
    before_cpu: NgpcCpuState,
    before_memory: dict[int, int],
    decoded: DecodeResult,
) -> ExecutionResult | None:
    """Execute SWI n. For `swi 1` this is the BIOS SYSTEM_CALL HLE dispatch.

    `swi 1` reads the vector index from RW3 and applies the reverse-engineered
    SNK BIOS side effect (see the vector tables above). Other `swi n` remain an
    honest PC-advance stub: the trap is acknowledged and PC advances to the
    return address, but the (unmodelled) BIOS-internal effects are omitted.
    """
    raw = decoded.raw_bytes
    if raw is None or len(raw) != 1 or not (0xF8 <= raw[0] <= 0xFF):
        return None
    if decoded.mnemonic != "swi":
        return None

    new_pc = decoded.next_sequential_pc
    if new_pc is None:
        return None

    n = raw[0] & 0x07

    # WITH A REAL BIOS ATTACHED, THERE IS NOTHING TO HIGH-LEVEL-EMULATE.
    #
    # Toshiba defines SWI as a plain hardware trap (<Software Interrupt>):
    #
    #     1) XSP <- XSP - 6
    #     2) (XSP) <- SR
    #     3) (XSP + 2) <- 32-bit PC
    #     4) PC <- (address referred by vector + num x 4)
    #
    # -- an INDIRECT jump through the CPU's vector table at 0xFFFF00. When the BIOS
    # image is present that table is readable and the BIOS code runs, exactly as on
    # silicon. The HLE below is a BIOS-LESS convenience (homebrew, bootstrap
    # sessions), not a model of the hardware, and it had become the single biggest
    # blind spot in whole-ROM trace equivalence: 28 of the 73 commercial ROMs reach
    # a `swi` within their first hundred instructions, and the comparison had to
    # stop there because the two cores were then legitimately in different code.
    #
    # So: vector when we can, HLE only when we cannot.
    hw_slot = _mask_address(0xFFFF00 + 4 * n)
    vector = _read_runtime_bytes_silent(view, before_memory, hw_slot, 4)
    target = int.from_bytes(vector, "little") & 0xFFFFFFFF if vector is not None else 0
    if target != 0:
        sr_value = encode_sr_from_state(before_cpu)
        xsp = before_cpu.regs.xsp
        if sr_value is not None and xsp is not None:
            # SR goes down first, PC second -- so PC ends up on TOP, which is what
            # RETI pops first. The other order restores the SR as a program counter.
            sr_target = (xsp - 2) & 0xFFFFFFFF
            pc_target = (xsp - 6) & 0xFFFFFFFF
            writes = (
                MemoryWrite(
                    address=_mask_address(sr_target),
                    data=sr_value.to_bytes(2, "little"),
                    note="SWI pushed SR (2 bytes).",
                ),
                MemoryWrite(
                    address=_mask_address(pc_target),
                    data=new_pc.to_bytes(4, "little"),
                    note="SWI pushed the return PC (4 bytes), on top of SR.",
                ),
            )
            after_memory = dict(before_memory)
            for w in writes:
                for offset, byte in enumerate(w.data):
                    after_memory[_mask_address(w.address + offset)] = byte
            return _executed_result(
                before_cpu=before_cpu,
                decoded=decoded,
                written_registers=("XSP", "PC"),
                memory_writes=writes,
                after_memory=after_memory,
                new_pc=_mask_address(target),
                reg_updates={"xsp": _mask_address(pc_target)},
                cycles_consumed=SWI_CYCLES,
                note=(
                    f"Executed SWI {n} as the hardware trap it is: pushed SR=0x{sr_value:04X} and "
                    f"PC=0x{new_pc:08X}, then vectored through slot 0x{hw_slot:06X} to the BIOS "
                    f"handler at 0x{_mask_address(target):06X}."
                ),
            )

    if n == 1:
        return _execute_swi1_system_call(view, before_cpu, before_memory, decoded, new_pc)

    # swi 0/2..7: not the system-call path (3..6 are software interrupts on
    # real silicon). Keep the honest PC-advance stub.
    return _swi_pc_advance_stub(
        before_cpu,
        before_memory,
        decoded,
        new_pc,
        note=(
            f"Executed SWI {n}: BIOS/interrupt trap acknowledged; effects not modelled. "
            "PC advanced to next instruction as if the trap returned normally."
        ),
    )


def _swi_pc_advance_stub(
    before_cpu: NgpcCpuState,
    before_memory: dict[int, int],
    decoded: DecodeResult,
    new_pc: int,
    *,
    note: str,
) -> ExecutionResult:
    modeled_fields = before_cpu.modeled_fields
    if "executed-subset" not in modeled_fields:
        modeled_fields = (*modeled_fields, "executed-subset")
    after_cpu = replace(
        before_cpu,
        pc=new_pc,
        modeled_fields=modeled_fields,
        note=(
            "This CPU state includes effects from the current minimal real execution "
            f"subset. {note}"
        ),
    )
    return ExecutionResult(
        before_cpu=before_cpu,
        after_cpu=after_cpu,
        decode=decoded,
        status="executed",
        written_registers=("PC",),
        memory_writes=(),
        after_memory=before_memory,
        note=note,
        cycles_consumed=SWI_CYCLES,
    )


def _execute_swi1_system_call(
    view: NgpcFetchView,
    before_cpu: NgpcCpuState,
    before_memory: dict[int, int],
    decoded: DecodeResult,
    new_pc: int,
) -> ExecutionResult:
    # Vector index = RW3 (rCodeB(0x31) on NeoPop) = W byte of bank-3 WA.
    vect = _read_bank3_byte(before_cpu, 0, 1)
    if vect is None:
        return _blocked_result(
            before_cpu=before_cpu,
            decoded=decoded,
            status="bios-call-requires-known-register",
            note=(
                "SWI 1 (BIOS SYSTEM_CALL) needs the vector index in RW3 (bank-3 W byte), "
                "but that register byte is still unknown in the current modelled state. "
                "Seed RW3 (e.g. from a full register snapshot) to dispatch the BIOS call."
            ),
        )

    if vect == _SWI1_VECT_SHUTDOWN:
        return _blocked_result(
            before_cpu=before_cpu,
            decoded=decoded,
            status="bios-shutdown",
            note=(
                f"{_swi1_note_prefix(vect)}: system power-off requested. The BIOS never "
                "returns from VECT_SHUTDOWN; execution stops honestly instead of advancing."
            ),
        )

    if vect in _SWI1_NOOP_VECTORS:
        return _swi_pc_advance_stub(
            before_cpu,
            before_memory,
            decoded,
            new_pc,
            note=(
                f"{_swi1_note_prefix(vect)}: no cartridge-visible side effect on the "
                "reference model. PC advanced to the return address."
            ),
        )

    if vect in _SWI1_SUCCESS_VECTORS:
        return _swi1_return_success(before_cpu, before_memory, decoded, new_pc, vect)

    if vect == _SWI1_VECT_INTLVSET:
        return _swi1_intlvset(view, before_cpu, before_memory, decoded, new_pc, vect)

    if vect == _SWI1_VECT_RTCGET:
        return _swi1_rtcget(before_cpu, before_memory, decoded, new_pc, vect)

    if vect == _SWI1_VECT_FLASHWRITE:
        return _swi1_flashwrite(view, before_cpu, before_memory, decoded, new_pc, vect)

    if vect == _SWI1_VECT_SYSFONTSET:
        return _swi1_sysfontset(view, before_cpu, before_memory, decoded, new_pc, vect)

    if 0x11 <= vect <= 0x1A:
        return _swi1_comms(before_cpu, before_memory, decoded, new_pc, vect)

    # Any vector we do not model: honest PC-advance stub, named so the
    # gap-filling workflow knows exactly which handler a ROM is waiting on.
    return _swi_pc_advance_stub(
        before_cpu,
        before_memory,
        decoded,
        new_pc,
        note=(
            f"{_swi1_note_prefix(vect)}: BIOS side effect not modelled yet. PC advanced to "
            "the return address as if the call returned; its state changes are omitted."
        ),
    )


def _swi1_write_bank3_and_advance(
    before_cpu: NgpcCpuState,
    before_memory: dict[int, int],
    decoded: DecodeResult,
    new_pc: int,
    *,
    bank3_bytes: dict[tuple[int, int], int],
    written_registers: tuple[str, ...],
    note: str,
    mem_bytes: dict[int, int] | None = None,
) -> ExecutionResult:
    """Apply a set of bank-3 byte writes (and optional RAM byte writes) as a
    BIOS-call side effect, then advance PC.

    `bank3_bytes` maps (r32_index, byte_pos) -> value. Chaining is done by
    feeding the running banks back through `_build_banked_core_byte_update`.
    """
    working = before_cpu
    reg_updates: dict[str, int] = {}
    new_banks = None
    for (r32_index, byte_pos), value in bank3_bytes.items():
        ru, banks = _build_banked_core_byte_update(
            working, bank_index=3, r32_index=r32_index, byte_pos=byte_pos, value=value
        )
        if banks is not None:
            new_banks = banks
            working = replace(working, register_banks=banks)
        if ru is not None:
            reg_updates.update(ru)

    extra: dict[str, object] = {}
    if new_banks is not None:
        extra["register_banks"] = new_banks

    memory_writes: tuple[MemoryWrite, ...] = ()
    after_memory = before_memory
    if mem_bytes:
        after_memory = dict(before_memory)
        writes = []
        for addr, value in mem_bytes.items():
            after_memory[addr] = value & 0xFF
            writes.append(
                MemoryWrite(
                    address=addr,
                    data=bytes([value & 0xFF]),
                    note=note,
                )
            )
        memory_writes = tuple(writes)

    return _executed_result(
        before_cpu=before_cpu,
        decoded=decoded,
        written_registers=written_registers,
        memory_writes=memory_writes,
        after_memory=after_memory,
        new_pc=new_pc,
        reg_updates=reg_updates or None,
        extra_cpu_updates=extra or None,
        note=note,
        cycles_consumed=SWI_CYCLES,
    )


def _swi1_return_success(
    before_cpu: NgpcCpuState,
    before_memory: dict[int, int],
    decoded: DecodeResult,
    new_pc: int,
    vect: int,
) -> ExecutionResult:
    """SYS_SUCCESS-returning BIOS calls: write RA3 = 0 and advance PC."""
    return _swi1_write_bank3_and_advance(
        before_cpu,
        before_memory,
        decoded,
        new_pc,
        bank3_bytes={(0, 0): 0},  # RA3 = 0
        written_registers=("PC", "RA3"),
        note=(
            f"{_swi1_note_prefix(vect)}: returned SYS_SUCCESS (RA3=0); no other "
            "reference-visible side effect on our model. PC advanced to the return address."
        ),
    )


def _swi1_comms(
    before_cpu: NgpcCpuState,
    before_memory: dict[int, int],
    decoded: DecodeResult,
    new_pc: int,
    vect: int,
) -> ExecutionResult:
    """Link-cable BIOS calls with a well-defined "no peer connected" result.

    A single-unit emulator has no serial peer, so the faithful outcome is
    "nothing to send/receive". Transcribed from NeoPop biosHLE.c for the
    no-cable path (`system_comms_read`/`write` return empty). Data-transfer
    vectors that require an actual peer + the comms IRQ (COMCREATEDATA 0x13,
    COMCREATEBUFDATA 0x19, COMGETBUFDATA 0x1A) stay as named stubs.
    """
    if vect in (0x11, 0x12):
        # COMSENDSTART / COMRECIVESTART: nothing to do.
        return _swi_pc_advance_stub(
            before_cpu,
            before_memory,
            decoded,
            new_pc,
            note=(
                f"{_swi1_note_prefix(vect)}: no serial peer connected; nothing to start. "
                "PC advanced to the return address."
            ),
        )
    if vect == 0x14:
        # COMGETDATA: no data available -> RA3 = 1 (COM_BUF_EMPTY).
        return _swi1_write_bank3_and_advance(
            before_cpu,
            before_memory,
            decoded,
            new_pc,
            bank3_bytes={(0, 0): 1},  # RA3 = COM_BUF_EMPTY
            written_registers=("PC", "RA3"),
            note=(
                f"{_swi1_note_prefix(vect)}: no serial peer connected; returned COM_BUF_EMPTY "
                "(RA3=1). PC advanced to the return address."
            ),
        )
    if vect in (0x17, 0x18):
        # COMSENDSTATUS / COMRECIVESTATUS: buffer count word = 0 (WA3 = RA3|RW3).
        return _swi1_write_bank3_and_advance(
            before_cpu,
            before_memory,
            decoded,
            new_pc,
            bank3_bytes={(0, 0): 0, (0, 1): 0},  # WA3 = 0
            written_registers=("PC", "WA3"),
            note=(
                f"{_swi1_note_prefix(vect)}: no serial peer connected; buffer count = 0 "
                "(WA3=0). PC advanced to the return address."
            ),
        )
    if vect in (0x15, 0x16):
        # COMONRTS -> RTS(0x00B2)=0 ; COMOFFRTS -> RTS=1.
        rts_value = 0 if vect == 0x15 else 1
        return _swi1_write_bank3_and_advance(
            before_cpu,
            before_memory,
            decoded,
            new_pc,
            bank3_bytes={},
            mem_bytes={0x00B2: rts_value},
            written_registers=("PC",),
            note=(
                f"{_swi1_note_prefix(vect)}: set the RTS handshake byte (0x00B2) = "
                f"{rts_value}. PC advanced to the return address."
            ),
        )
    # Data-transfer comms vectors need a real peer + the comms IRQ: leave stub.
    return _swi_pc_advance_stub(
        before_cpu,
        before_memory,
        decoded,
        new_pc,
        note=(
            f"{_swi1_note_prefix(vect)}: serial data-transfer call needs a connected peer and "
            "the comms IRQ, not modelled yet. PC advanced to the return address."
        ),
    )


def _swi1_intlvset(
    view: NgpcFetchView,
    before_cpu: NgpcCpuState,
    before_memory: dict[int, int],
    decoded: DecodeResult,
    new_pc: int,
    vect: int,
) -> ExecutionResult:
    """VECT_INTLVSET: set an interrupt source's priority level in the INTxx I/O
    registers. level=RB3, source=RC3 (NeoPop biosHLE.c)."""
    level = _read_bank3_byte(before_cpu, 1, 1)  # RB3 = B byte of bank-3 BC
    source = _read_bank3_byte(before_cpu, 1, 0)  # RC3 = C byte of bank-3 BC
    if level is None or source is None:
        return _blocked_result(
            before_cpu=before_cpu,
            decoded=decoded,
            status="bios-call-requires-known-register",
            note=(
                f"{_swi1_note_prefix(vect)} needs the level (RB3) and source (RC3) bank-3 "
                "registers, but at least one is unknown in the current modelled state."
            ),
        )

    entry = _SWI1_INTLVSET_TABLE.get(source & 0xFF)
    if entry is None:
        # Source out of the documented 0..9 range: BIOS falls through the switch
        # with no register write. Advance PC unchanged.
        return _swi_pc_advance_stub(
            before_cpu,
            before_memory,
            decoded,
            new_pc,
            note=(
                f"{_swi1_note_prefix(vect)}: interrupt source 0x{source & 0xFF:02X} is outside "
                "the documented 0..9 range; no INTxx register written. PC advanced."
            ),
        )

    io_addr, high_nibble = entry
    current = _read_runtime_bytes(view, before_memory, io_addr, 1)
    if current is None:
        return _blocked_result(
            before_cpu=before_cpu,
            decoded=decoded,
            status="runtime-memory-unavailable",
            note=(
                f"{_swi1_note_prefix(vect)}: could not read the current INTxx register at "
                f"0x{io_addr:06X} to preserve its untouched nibble."
            ),
        )
    old = current[0]
    lvl = level & 0x07
    if high_nibble:
        new_val = (old & 0x0F) | (lvl << 4)
    else:
        new_val = (old & 0xF0) | lvl

    after_memory = dict(before_memory)
    after_memory[io_addr] = new_val
    mem_write = MemoryWrite(
        address=io_addr,
        data=bytes([new_val]),
        note=(
            f"INTLVSET set interrupt source 0x{source & 0xFF:02X} to priority level "
            f"{lvl} in INT register 0x{io_addr:06X}."
        ),
    )
    return _executed_result(
        before_cpu=before_cpu,
        decoded=decoded,
        written_registers=("PC",),
        memory_writes=(mem_write,),
        after_memory=after_memory,
        new_pc=new_pc,
        reg_updates=None,
        note=(
            f"{_swi1_note_prefix(vect)}: wrote priority level {lvl} for interrupt source "
            f"0x{source & 0xFF:02X} into INT register 0x{io_addr:06X} (=0x{new_val:02X}). "
            "PC advanced to the return address."
        ),
        cycles_consumed=SWI_CYCLES,
    )


def _swi1_rtcget(
    before_cpu: NgpcCpuState,
    before_memory: dict[int, int],
    decoded: DecodeResult,
    new_pc: int,
    vect: int,
) -> ExecutionResult:
    """VECT_RTCGET: write 7 packed-BCD real-time-clock bytes into the caller's
    buffer at XHL3. Mirrors NeoPop biosHLE.c (buffer pointer = rCodeL(0x3C) =
    XHL3, guarded to buffers below 0xC000)."""
    buf = _read_bank3_long(before_cpu, 3)  # XHL3 = bank-3 XHL
    if buf is None:
        return _blocked_result(
            before_cpu=before_cpu,
            decoded=decoded,
            status="bios-call-requires-known-register",
            note=(
                f"{_swi1_note_prefix(vect)} needs the destination buffer pointer in XHL3, "
                "but that bank-3 register is unknown in the current modelled state."
            ),
        )

    if buf >= 0xC000:
        # NeoPop guard: buffers at/above 0xC000 are rejected; the call returns
        # without writing. Advance PC with no side effect.
        return _swi_pc_advance_stub(
            before_cpu,
            before_memory,
            decoded,
            new_pc,
            note=(
                f"{_swi1_note_prefix(vect)}: destination buffer XHL3=0x{buf:06X} is >= 0xC000; "
                "BIOS rejects it and writes nothing. PC advanced to the return address."
            ),
        )

    bcd = _bios_rtc_bcd_bytes(before_memory)
    base = _mask_address(buf)
    after_memory = dict(before_memory)
    for offset, value in enumerate(bcd):
        after_memory[_mask_address(buf + offset)] = value
    mem_write = MemoryWrite(
        address=base,
        data=bcd,
        note=(
            "RTCGET wrote 7 packed-BCD RTC bytes (year, month, day, hour, minute, "
            "second, weekday) into the caller's buffer at XHL3."
        ),
    )
    return _executed_result(
        before_cpu=before_cpu,
        decoded=decoded,
        written_registers=("PC",),
        memory_writes=(mem_write,),
        after_memory=after_memory,
        new_pc=new_pc,
        reg_updates=None,
        note=(
            f"{_swi1_note_prefix(vect)}: wrote the host real-time clock as 7 packed-BCD bytes "
            f"to the buffer at 0x{base:06X} (XHL3). PC advanced to the return address."
        ),
        cycles_consumed=SWI_CYCLES,
    )


# --- SYSFONTSET ----------------------------------------------------------
#
# FIDELITY CHOICE: use the REAL SNK font, never a
# substitute. The font is not embedded here -- it lives in the BIOS image the
# user supplies (`--bios`), at BIOS offset 0x8DCF (= CPU 0xFF8DCF), 0x800 bytes
# (256 glyphs x 8 rows, 1 bit per pixel). Verified against the retail dump:
# glyph 0x41 renders a correct 8x8 'A'. NeoPop's bios.c installs its own copy at
# exactly this offset, confirming it is where the real BIOS keeps the font.
#
# So: nothing proprietary is distributed with the emulator, AND the glyphs are
# pixel-exact to real hardware. With no BIOS attached we honest-stop rather than
# fabricate a font.
_SYSFONT_BIOS_ADDRESS = 0xFF8DCF
_SYSFONT_SOURCE_BYTES = 0x800
# Destination: CHAR RAM. Each 1bpp source byte (8 pixels) expands to one 16-bit
# 2bpp word, so 0x800 source bytes -> 0x1000 bytes at 0x00A000..0x00AFFF.
_SYSFONT_CHAR_RAM_BASE = 0x00A000

# FLASHWRITE copies at most one flash block (0x10000 bytes / 256 units) per call
# on real HW; reject absurdly large counts rather than allocate unbounded.
_SWI1_FLASHWRITE_MAX_UNITS = 0x100


def _sysfont_expand_row(source_byte: int, fg: int, bg: int) -> int:
    """Expand one 1bpp font row (8 pixels) into a 2bpp 16-bit CHAR RAM word.

    Mirrors NeoPop biosHLE.c: the word is shifted left 2 bits per pixel and the
    pixel's 2-bit colour index is OR'd in, so pixel 0 (the source MSB) ends up in
    the word's high bits. `fg`/`bg` are masked to 2 bits (CHAR RAM is 2bpp);
    NeoPop OR's the raw nibble, which would bleed into the neighbouring pixel for
    out-of-range colours -- masking is identical for every valid input.
    """
    word = 0
    for bit_index in range(8):
        word = (word << 2) & 0xFFFF
        pixel_set = (source_byte >> (7 - bit_index)) & 1
        word |= fg if pixel_set else bg
    return word


def _swi1_sysfontset(
    view: NgpcFetchView,
    before_cpu: NgpcCpuState,
    before_memory: dict[int, int],
    decoded: DecodeResult,
    new_pc: int,
    vect: int,
) -> ExecutionResult:
    """VECT_SYSFONTSET: expand the BIOS 8x8 font into CHAR RAM as 2bpp tiles.

    RA3 carries the colours: low 2 bits = foreground index, high nibble =
    background index (NeoPop biosHLE.c). The glyph bitmaps are read from the
    attached BIOS image -- see the fidelity note above.
    """
    colours = _read_bank3_byte(before_cpu, 0, 0)  # RA3
    if colours is None:
        return _blocked_result(
            before_cpu=before_cpu,
            decoded=decoded,
            status="bios-call-requires-known-register",
            note=(
                f"{_swi1_note_prefix(vect)} needs the colour byte in RA3 (low 2 bits = "
                "foreground, high nibble = background), but it is unknown in the current state."
            ),
        )

    font = _read_runtime_bytes(
        view, before_memory, _SYSFONT_BIOS_ADDRESS, _SYSFONT_SOURCE_BYTES
    )
    if font is None:
        return _blocked_result(
            before_cpu=before_cpu,
            decoded=decoded,
            status="bios-font-unavailable",
            note=(
                f"{_swi1_note_prefix(vect)}: the system font lives in the BIOS at "
                f"0x{_SYSFONT_BIOS_ADDRESS:06X} and no BIOS image is attached, so the real glyphs "
                "cannot be read. Attach the BIOS (--bios) to run this call. We stop honestly "
                "rather than substitute a different font, which would not be pixel-faithful to "
                "real hardware."
            ),
        )

    fg = colours & 0x03
    bg = (colours >> 4) & 0x03
    payload = bytearray()
    for source_byte in font:
        payload += _sysfont_expand_row(source_byte, fg, bg).to_bytes(2, "little")

    base = _SYSFONT_CHAR_RAM_BASE
    write_status, write_note = _check_writable_range(view, base, len(payload))
    if write_status is not None:
        return _blocked_result(
            before_cpu=before_cpu,
            decoded=decoded,
            status=write_status,
            note=f"{_swi1_note_prefix(vect)}: CHAR RAM is not writable. {write_note}",
        )

    after_memory = dict(before_memory)
    for offset, value in enumerate(payload):
        after_memory[_mask_address(base + offset)] = value
    mem_write = MemoryWrite(
        address=base,
        data=bytes(payload),
        note=(
            f"SYSFONTSET expanded the {_SYSFONT_SOURCE_BYTES}-byte BIOS font into "
            f"{len(payload)} bytes of 2bpp CHAR RAM at 0x{base:06X}."
        ),
    )
    return _executed_result(
        before_cpu=before_cpu,
        decoded=decoded,
        written_registers=("PC",),
        memory_writes=(mem_write,),
        after_memory=after_memory,
        new_pc=new_pc,
        reg_updates=None,
        note=(
            f"{_swi1_note_prefix(vect)}: expanded the real BIOS 8x8 font into CHAR RAM at "
            f"0x{base:06X} as 2bpp tiles (foreground={fg}, background={bg}). "
            "PC advanced to the return address."
        ),
        cycles_consumed=SWI_CYCLES,
    )


def _swi1_flashwrite(
    view: NgpcFetchView,
    before_cpu: NgpcCpuState,
    before_memory: dict[int, int],
    decoded: DecodeResult,
    new_pc: int,
    vect: int,
) -> ExecutionResult:
    """VECT_FLASHWRITE: copy `BC3 * 256` bytes from RAM (XHL3) into the cart
    flash window (bank + XDE3), returning SYS_SUCCESS in RA3.

    Params per NeoPop biosHLE.c: RA3 selects the bank (0 -> 0x200000, 1 ->
    0x800000), XDE3 = destination offset, XHL3 = source, BC3 = count in 256-byte
    units. The written bytes land in the session's writable overlay, which
    shadows the cart ROM image exactly like real NOR flash overlays the cart --
    so subsequent reads (and the savestate) see the persisted save. The direct
    cart-write path (AMD unlock + /WE toggle used by the project's own flash lib)
    is a separate session-layer chantier; this models the BIOS-mediated path.
    """
    bank_sel = _read_bank3_byte(before_cpu, 0, 0)  # RA3
    dest_off = _read_bank3_long(before_cpu, 2)  # XDE3
    src = _read_bank3_long(before_cpu, 3)  # XHL3
    units = _read_bank3_word(before_cpu, 1)  # BC3 (count in 256-byte units)
    if bank_sel is None or dest_off is None or src is None or units is None:
        return _blocked_result(
            before_cpu=before_cpu,
            decoded=decoded,
            status="bios-call-requires-known-register",
            note=(
                f"{_swi1_note_prefix(vect)} needs RA3 (bank), XDE3 (dest), XHL3 (src) and BC3 "
                "(count) in bank 3, but at least one is unknown in the current modelled state."
            ),
        )

    if units == 0:
        # Nothing to copy; the BIOS still returns SYS_SUCCESS.
        return _swi1_return_success(before_cpu, before_memory, decoded, new_pc, vect)

    if units > _SWI1_FLASHWRITE_MAX_UNITS:
        return _blocked_result(
            before_cpu=before_cpu,
            decoded=decoded,
            status="bios-call-out-of-range",
            note=(
                f"{_swi1_note_prefix(vect)}: BC3 count {units} exceeds one flash block "
                f"({_SWI1_FLASHWRITE_MAX_UNITS} * 256 bytes); refusing rather than guessing."
            ),
        )

    total = units * 256
    bank_base = 0x800000 if (bank_sel & 0xFF) == 1 else 0x200000
    dest = _mask_address(bank_base + dest_off)

    payload = _read_runtime_bytes(view, before_memory, src, total)
    if payload is None:
        return _blocked_result(
            before_cpu=before_cpu,
            decoded=decoded,
            status="runtime-memory-unavailable",
            note=(
                f"{_swi1_note_prefix(vect)}: could not read the {total}-byte source buffer at "
                f"0x{_mask_address(src):06X} (XHL3) to copy into flash."
            ),
        )

    after_memory = dict(before_memory)
    for offset, value in enumerate(payload):
        after_memory[_mask_address(dest + offset)] = value
    mem_write = MemoryWrite(
        address=dest,
        data=payload,
        note=(
            f"FLASHWRITE copied {total} bytes from 0x{_mask_address(src):06X} into the cart "
            f"flash window at 0x{dest:06X}."
        ),
    )

    # RA3 = SYS_SUCCESS (0) on completion.
    reg_updates, new_banks = _build_banked_core_byte_update(
        before_cpu, bank_index=3, r32_index=0, byte_pos=0, value=0
    )
    extra: dict[str, object] = {}
    if new_banks is not None:
        extra["register_banks"] = new_banks
    return _executed_result(
        before_cpu=before_cpu,
        decoded=decoded,
        written_registers=("PC", "RA3"),
        memory_writes=(mem_write,),
        after_memory=after_memory,
        new_pc=new_pc,
        reg_updates=reg_updates,
        extra_cpu_updates=extra or None,
        note=(
            f"{_swi1_note_prefix(vect)}: copied {total} bytes into the cart flash window at "
            f"0x{dest:06X} and returned SYS_SUCCESS (RA3=0). PC advanced to the return address."
        ),
        cycles_consumed=SWI_CYCLES,
    )


def _try_execute_ei_di(
    before_cpu: NgpcCpuState,
    before_memory: dict[int, int],
    decoded: DecodeResult,
) -> ExecutionResult | None:
    """Execute ei n (enable interrupts) and di (disable interrupts).

    Encoding:
      06 07 = di  (disable interrupts; equivalent to ei 7 = mask all maskable IRQs)
      06 nn = ei n  (set interrupt mask level to n in [0..7])

    TLCS-900/H model: SR[12:14] is the 3-bit interrupt mask level (IFF).
    `iff_level` is the canonical field; `iff_enabled` stays as a derived
    legacy convenience (True when level < 7, False when level == 7).
    No IRQ servicing is performed: that requires the IRQ/VBlank model
    which is not yet implemented.
    """
    raw = decoded.raw_bytes
    if raw is None or len(raw) != 2 or raw[0] != 0x06:
        return None
    if decoded.mnemonic not in ("ei", "di"):
        return None

    new_pc = decoded.next_sequential_pc
    if new_pc is None:
        return None

    if raw[1] == 0x07:
        new_level = 7
        new_iff_enabled = False
        written = ("IFF", "PC")
        note = (
            "Executed di: interrupt mask level set to 7 (all maskable IRQs blocked). "
            "Actual interrupt servicing is not modeled yet."
        )
    else:
        new_level = raw[1] & 0b111
        new_iff_enabled = new_level < 7
        written = ("IFF", "PC")
        note = (
            f"Executed ei {new_level}: interrupt mask level set to {new_level}. "
            "Actual interrupt servicing is not modeled yet."
        )

    modeled_fields = before_cpu.modeled_fields
    if "executed-subset" not in modeled_fields:
        modeled_fields = (*modeled_fields, "executed-subset")
    if "IFF" not in modeled_fields:
        modeled_fields = (*modeled_fields, "IFF")

    after_cpu = replace(
        before_cpu,
        pc=new_pc,
        iff_enabled=new_iff_enabled,
        iff_level=new_level,
        modeled_fields=modeled_fields,
        note=(
            "This CPU state includes effects from the current minimal real execution subset. "
            f"{note}"
        ),
    )

    return ExecutionResult(
        before_cpu=before_cpu,
        after_cpu=after_cpu,
        decode=decoded,
        status="executed",
        written_registers=written,
        memory_writes=(),
        after_memory=before_memory,
        note=note,
        cycles_consumed=(DI_CYCLES if raw[1] == 0x07 else EI_CYCLES),
    )


def _try_execute_ex_ff(
    before_cpu: NgpcCpuState,
    before_memory: dict[int, int],
    decoded: DecodeResult,
) -> ExecutionResult | None:
    """Execute `EX F,F'` (opcode 0x16).

    TLCS-900/H exchanges the current visible flag set with the shadow
    alternate flag set `F'`. On NGPC we model the six architecturally
    surfaced flags (`S/Z/V/H/C/N`) in both places.

    If `before_cpu.alt_flags` is absent (older fixtures / payloads), we
    treat the shadow set as fully unknown instead of fabricating values.
    """
    raw = decoded.raw_bytes
    if raw is None or len(raw) != 1 or raw[0] != 0x16:
        return None
    if decoded.mnemonic != "ex" or decoded.operands != "F,F'":
        return None

    new_pc = decoded.next_sequential_pc
    if new_pc is None:
        return None

    shadow_flags = before_cpu.alt_flags
    if shadow_flags is None:
        shadow_flags = StatusFlags(
            sf=None, zf=None, vf=None, hf=None, cf=None, nf=None,
        )

    modeled_fields = before_cpu.modeled_fields
    if "executed-subset" not in modeled_fields:
        modeled_fields = (*modeled_fields, "executed-subset")
    if "modeled-flags-subset" not in modeled_fields:
        modeled_fields = (*modeled_fields, "modeled-flags-subset")
    if "alternate-flags" not in modeled_fields:
        modeled_fields = (*modeled_fields, "alternate-flags")

    after_cpu = replace(
        before_cpu,
        pc=new_pc,
        flags=shadow_flags,
        alt_flags=before_cpu.flags,
        modeled_fields=modeled_fields,
        note=(
            "This CPU state includes effects from the current minimal real execution subset. "
            "Executed EX F,F': swapped the visible flag set with the shadow F' set."
        ),
    )
    return ExecutionResult(
        before_cpu=before_cpu,
        after_cpu=after_cpu,
        decode=decoded,
        status="executed",
        written_registers=("F", "F'", "PC"),
        memory_writes=(),
        after_memory=before_memory,
        note=(
            "Executed EX F,F': swapped the visible flag set with the shadow F' set."
        ),
        cycles_consumed=EX_FF_CYCLES,
    )


def _try_execute_cpu_carry_flag_control(
    before_cpu: NgpcCpuState,
    before_memory: dict[int, int],
    decoded: DecodeResult,
) -> ExecutionResult | None:
    """Execute fixed carry-flag CPU-control ops `RCF/SCF/CCF/ZCF`.

    Local Toshiba TLCS-900/L1 documentation defines:
      - RCF : CY <- 0, H <- 0, N <- 0
      - SCF : CY <- 1, H <- 0, N <- 0
      - CCF : CY <- not CY, H <- undefined, N <- 0
      - ZCF : CY <- not Z, H <- undefined, N <- 0

    `V`, `S`, and `Z` remain unchanged. When an input flag required to
    derive the new carry value is still unknown (`CF` for `CCF`, `ZF`
    for `ZCF`), the instruction still executes but the resulting carry
    flag remains unknown (`None`) rather than being guessed.
    """
    raw = decoded.raw_bytes
    if raw is None or len(raw) != 1 or raw[0] not in (0x10, 0x11, 0x12, 0x13):
        return None
    if decoded.mnemonic not in {"rcf", "scf", "ccf", "zcf"}:
        return None

    new_pc = decoded.next_sequential_pc
    if new_pc is None:
        return None

    mnemonic = decoded.mnemonic
    if mnemonic == "rcf":
        new_cf = False
        new_hf = False
        note = "Executed RCF: carry flag reset to 0, H reset to 0, N reset to 0."
    elif mnemonic == "scf":
        new_cf = True
        new_hf = False
        note = "Executed SCF: carry flag set to 1, H reset to 0, N reset to 0."
    elif mnemonic == "ccf":
        new_cf = None if before_cpu.flags.cf is None else (not before_cpu.flags.cf)
        new_hf = None
        note = (
            "Executed CCF: carry flag complemented, H became undefined, and N reset to 0."
        )
    else:
        assert mnemonic == "zcf"
        new_cf = None if before_cpu.flags.zf is None else (not before_cpu.flags.zf)
        new_hf = None
        note = (
            "Executed ZCF: carry flag loaded from the inverted Z flag, H became undefined, "
            "and N reset to 0."
        )

    return _executed_result(
        before_cpu=before_cpu,
        decoded=decoded,
        written_registers=("PC",),
        memory_writes=(),
        after_memory=before_memory,
        new_pc=new_pc,
        reg_updates=None,
        note=note,
        flags_updates={
            "cf": new_cf,
            "hf": new_hf,
            "nf": False,
        },
        cycles_consumed=CF_CPU_CONTROL_CYCLES,
    )


def _try_execute_ldf(
    before_cpu: NgpcCpuState,
    before_memory: dict[int, int],
    decoded: DecodeResult,
) -> ExecutionResult | None:
    """Execute LDF n (load register File pointer) — opcode 0x17 imm.

    Sets the 2-bit RFP (bits 8..9 of SR), selecting which physical
    register bank is active (TLCS-900/H has 4 banks 0..3).

    The current bank model flushes the outgoing visible
    `XWA/XBC/XDE/XHL` window into the backing store, then reloads the
    incoming bank immediately. `LDF`, `POP SR`, and `RETI` therefore
    share the same observable visible-window bank-switch behavior.
    """
    raw = decoded.raw_bytes
    if raw is None or len(raw) != 2 or raw[0] != 0x17:
        return None
    if decoded.mnemonic != "ldf":
        return None

    new_pc = decoded.next_sequential_pc
    if new_pc is None:
        return None

    new_rfp = raw[1] & 0b11  # SR layout : RFP is the low 2 bits of imm
    old_bank, banks, new_regs = _switch_visible_core_bank(before_cpu, new_rfp)
    modeled_fields = before_cpu.modeled_fields
    if "executed-subset" not in modeled_fields:
        modeled_fields = (*modeled_fields, "executed-subset")
    if "RFP" not in modeled_fields:
        modeled_fields = (*modeled_fields, "RFP")

    after_cpu = replace(
        before_cpu,
        pc=new_pc,
        regs=new_regs,
        rfp=new_rfp,
        register_bank=new_rfp,
        register_banks=banks,
        modeled_fields=modeled_fields,
        note=(
            f"{before_cpu.note} Executed LDF {new_rfp}: current bank {old_bank} "
            "was flushed to the banked byte-register backing store, then the "
            f"visible XWA/XBC/XDE/XHL set was reloaded from bank {new_rfp}."
        ),
    )

    return ExecutionResult(
        before_cpu=before_cpu,
        after_cpu=after_cpu,
        decode=decoded,
        status="executed",
        written_registers=("RFP", "PC"),
        memory_writes=(),
        after_memory=before_memory,
        note=(
            f"Executed LDF {new_rfp}: register file pointer set to {new_rfp}. "
            f"Visible XWA/XBC/XDE/XHL were flushed from bank {old_bank} and "
            f"reloaded from bank {new_rfp}."
        ),
        cycles_consumed=LDF_CYCLES,
    )


def _try_execute_incf_decf(
    before_cpu: NgpcCpuState,
    before_memory: dict[int, int],
    decoded: DecodeResult,
) -> ExecutionResult | None:
    """Execute INCF / DECF bank-rotation instructions.

    TLCS-900/H rotates the visible core-register window across the four
    banked XWA/XBC/XDE/XHL backing stores. Like `LDF`, this helper
    flushes the outgoing visible core set into the current bank, then
    reloads the incoming bank immediately.
    """
    raw = decoded.raw_bytes
    if raw is None or len(raw) != 1 or raw[0] not in (0x0C, 0x0D):
        return None
    if decoded.mnemonic not in {"incf", "decf"}:
        return None

    new_pc = decoded.next_sequential_pc
    if new_pc is None:
        return None

    current_bank = _current_register_bank_index(before_cpu, fallback_zero=True)
    assert current_bank is not None
    delta = 1 if raw[0] == 0x0C else -1
    new_rfp = (current_bank + delta) & 0b11
    old_bank, banks, new_regs = _switch_visible_core_bank(before_cpu, new_rfp)
    mnemonic_upper = decoded.mnemonic.upper()

    modeled_fields = before_cpu.modeled_fields
    if "executed-subset" not in modeled_fields:
        modeled_fields = (*modeled_fields, "executed-subset")
    if "RFP" not in modeled_fields:
        modeled_fields = (*modeled_fields, "RFP")

    after_cpu = replace(
        before_cpu,
        pc=new_pc,
        regs=new_regs,
        rfp=new_rfp,
        register_bank=new_rfp,
        register_banks=banks,
        modeled_fields=modeled_fields,
        note=(
            f"{before_cpu.note} Executed {mnemonic_upper}: current bank {old_bank} "
            "was flushed to the banked byte-register backing store, then the "
            f"visible XWA/XBC/XDE/XHL set was reloaded from bank {new_rfp}."
        ),
    )

    return ExecutionResult(
        before_cpu=before_cpu,
        after_cpu=after_cpu,
        decode=decoded,
        status="executed",
        written_registers=("RFP", "PC"),
        memory_writes=(),
        after_memory=before_memory,
        note=(
            f"Executed {mnemonic_upper}: register file pointer rotated from bank "
            f"{old_bank} to {new_rfp}; visible XWA/XBC/XDE/XHL were flushed and "
            "reloaded accordingly."
        ),
        cycles_consumed=INCF_DECF_CYCLES,
    )


def _try_execute_push_pop_sr(
    view: NgpcFetchView,
    before_cpu: NgpcCpuState,
    before_memory: dict[int, int],
    decoded: DecodeResult,
) -> ExecutionResult | None:
    """Execute PUSH SR (0x02) and POP SR (0x03).

    PUSH SR : encodes the modeled SR from `before_cpu` and writes the
              16-bit value little-endian onto the stack, decrementing XSP
              by 2. Requires every SR-derived field to be modeled (six
              flags + iff_level + rfp); otherwise stops with
              `requires-known-sr`.

    POP SR  : reads 2 bytes from the current XSP, decodes them into the
              individual SR fields and applies them to flags, iff_level
              and rfp atomically. XSP advances by 2.

    Encoding from `T900_DENSE_REF.md` opcode table:
      02  PUSH SR
      03  POP  SR
    """
    raw = decoded.raw_bytes
    if raw is None or len(raw) != 1:
        return None
    if raw[0] not in (0x02, 0x03):
        return None
    if decoded.mnemonic not in ("push", "pop"):
        return None

    new_pc = decoded.next_sequential_pc
    if new_pc is None:
        return None

    if raw[0] == 0x02:
        # PUSH SR
        sr_value = encode_sr_from_state(before_cpu)
        if sr_value is None:
            return _blocked_result(
                before_cpu=before_cpu,
                decoded=decoded,
                status="requires-known-sr",
                note=(
                    "PUSH SR needs the full SR shape modeled (six ALU flags "
                    "plus iff_level plus rfp). At least one is still unknown "
                    "in the current CPU state."
                ),
            )
        data = sr_value.to_bytes(2, "little")
        return _execute_push_bytes(
            view=view,
            before_cpu=before_cpu,
            before_memory=before_memory,
            decoded=decoded,
            data=data,
            note=(
                f"Executed PUSH SR: 16-bit SR=0x{sr_value:04X} written "
                "little-endian to the writable stack model. XSP decremented "
                "by 2."
            ),
            cycles_consumed=PUSH_SR_CYCLES,
        )

    # POP SR
    xsp = before_cpu.regs.xsp
    if xsp is None:
        return _blocked_result(
            before_cpu=before_cpu,
            decoded=decoded,
            status="requires-known-stack-pointer",
            note=(
                "POP SR needs XSP, but the current bootstrap CPU state still "
                "leaves the stack pointer unknown."
            ),
        )
    data = _read_runtime_bytes(view, before_memory, _mask_address(xsp), 2)
    if data is None:
        return _blocked_result(
            before_cpu=before_cpu,
            decoded=decoded,
            status="stack-data-unavailable",
            note=(
                "POP SR needs 2 readable bytes at the current XSP, but the "
                "current writable stack model and read bus do not provide them."
            ),
        )
    sr_value = int.from_bytes(data, "little")
    fields = decode_sr_to_fields(sr_value)

    new_flags = StatusFlags(
        sf=bool(fields["sf"]),
        zf=bool(fields["zf"]),
        vf=bool(fields["vf"]),
        hf=bool(fields["hf"]),
        cf=bool(fields["cf"]),
        nf=bool(fields["nf"]),
    )
    new_iff_level = int(fields["iff_level"])
    new_rfp = int(fields["rfp"])
    new_xsp = (xsp + 2) & 0xFFFFFFFF
    old_bank, banks, new_regs = _switch_visible_core_bank(before_cpu, new_rfp)

    modeled_fields = before_cpu.modeled_fields
    if "executed-subset" not in modeled_fields:
        modeled_fields = (*modeled_fields, "executed-subset")
    if "modeled-flags-subset" not in modeled_fields:
        modeled_fields = (*modeled_fields, "modeled-flags-subset")
    if "IFF" not in modeled_fields:
        modeled_fields = (*modeled_fields, "IFF")
    if "RFP" not in modeled_fields:
        modeled_fields = (*modeled_fields, "RFP")

    after_cpu = replace(
        before_cpu,
        pc=new_pc,
        regs=replace(new_regs, xsp=new_xsp),
        flags=new_flags,
        iff_level=new_iff_level,
        iff_enabled=(new_iff_level < 7),
        rfp=new_rfp,
        register_bank=new_rfp,
        register_banks=banks,
        sr_raw=sr_value,
        modeled_fields=modeled_fields,
        note=(
            f"{before_cpu.note} Executed POP SR: 16-bit SR=0x{sr_value:04X} "
            "loaded from the writable stack model. All six flags, iff_level "
            f"={new_iff_level} and rfp={new_rfp} are now derived from the "
            f"popped value. Outgoing bank {old_bank} was flushed and bank "
            f"{new_rfp} reloaded into the visible core-register window."
        ),
    )

    return ExecutionResult(
        before_cpu=before_cpu,
        after_cpu=after_cpu,
        decode=decoded,
        status="executed",
        written_registers=("SR", "IFF", "RFP", "XSP", "PC"),
        memory_writes=(),
        after_memory=before_memory,
        # memory_reads is populated automatically by build_execute_next
        # from _STEP_READS, which captured the 2-byte read above.
        note=(
            f"Executed POP SR: read 0x{sr_value:04X} from stack at "
            f"0x{xsp:08X}; XSP advanced to 0x{new_xsp:08X}. Six flags + "
            f"iff_level + rfp updated atomically; bank {old_bank} was "
            f"flushed and bank {new_rfp} reloaded for XWA/XBC/XDE/XHL."
        ),
        cycles_consumed=POP_SR_CYCLES,
    )


@dataclass(frozen=True)
class IrqDeliveryResult:
    """Outcome of an IRQ-delivery attempt between two instructions.

    `delivered` is True if an IRQ was accepted: PC + SR pushed onto
    the stack, control transferred through the modeled vector slot
    resolution path, iff_level raised to the delivered level, and the
    corresponding `pending_mask` bit cleared.

    `delivered` is False when no IRQ was deliverable (either nothing
    pending, or all pending IRQs are masked by the current iff_level).
    In that case `after_cpu` / `after_memory` / `after_irq_state` are
    identical to the inputs (returned for caller convenience).

    `blocked_reason` is set when the delivery couldn't proceed because
    of missing modeled state (e.g. unknown XSP or unencodable SR).
    The run loop should surface this as a stop reason rather than
    silently skipping IRQ delivery.

    `cycles_consumed` is the IRQ-entry cost (push PC + SR + vector
    load). Per Toshiba TLCS-900/H spec, ~13 cycles. Zero when no
    delivery happened.
    """

    delivered: bool
    after_cpu: NgpcCpuState
    after_memory: dict[int, int]
    after_irq_state: "IrqState"
    blocked_reason: str | None
    note: str
    cycles_consumed: int = 0
    vector_slot_address: int | None = None
    vector_slot_raw: int | None = None
    vector_target: int | None = None
    used_handler_pointer: bool = False
    used_slot_fallback: bool = False
    # True when the IRQ vectored through the CPU's HARDWARE interrupt table in
    # BIOS ROM (0xFFFF00 + index*4) -- what real silicon always does. False means
    # we took the BIOS-less homebrew shortcut straight to the RAM hook 0x006FCC.
    used_hw_vector_table: bool = False


def _read_runtime_bytes_silent(
    view: NgpcFetchView,
    memory_bytes: dict[int, int],
    address: int,
    size: int,
) -> bytes | None:
    """Read bytes through overlay then bus without recording a step read."""
    data = bytearray()
    for offset in range(size):
        cur_addr = _mask_address(address + offset)
        if cur_addr in memory_bytes:
            data.append(memory_bytes[cur_addr] & 0xFF)
            continue
        read = view.bus.read_bytes(cur_addr, size=1)
        if read.status != "ok" or read.data is None:
            # Truly-unmapped address = OPEN BUS. HW-measured on a real
            # NGPC (hw_test_openbus, 2026-07-08): a read of an address outside
            # every mapped region returns 0x00 and does NOT hang (the TLCS-900/H
            # has no bus-fault trap). Model that faithfully instead of honest-
            # stopping. Addresses that sit INSIDE a region but are merely
            # unbacked (an unmodeled peripheral / not-yet-loaded cart image) stay
            # None -> honest-stop, because that is modelable state we have not
            # modeled yet, not open bus.
            probe = view.bus.address_space.probe(cur_addr)
            if probe.region is None:
                data.append(0x00)
                continue
            return None
        data.extend(read.data)
    return bytes(data)


def try_deliver_pending_vector_irq(
    view: NgpcFetchView,
    cpu: NgpcCpuState,
    memory: dict[int, int],
    irq_state: "IrqState",
) -> IrqDeliveryResult:
    """Deliver a pending HARDWARE-VECTOR source (A/D completion, timers, ...).

    These are the sources identified by their Toshiba vector index rather than
    by the VBlank special case. Their priority level is *programmable*: it lives
    in an INTxx register (the same nibbles `VECT_INTLVSET` writes), and a level
    of 0 means the source is disabled. Everything else -- the mask gate
    (`L >= IFF`), the PC+SR push, the post-acceptance mask (`IFF = L + 1`) and
    the hardware vector table at 0xFFFF00 -- follows the same datasheet rules as
    the VBlank path.
    """
    from core.frame_timing import (
        IrqState,
        irq_hw_vector_slot,
        irq_level_from_priority_register,
    )

    if not irq_state.pending_vectors:
        return IrqDeliveryResult(
            delivered=False,
            after_cpu=cpu,
            after_memory=memory,
            after_irq_state=irq_state,
            blocked_reason=None,
            note="No hardware-vector IRQ pending.",
        )
    if cpu.iff_level is None or cpu.regs.xsp is None:
        return IrqDeliveryResult(
            delivered=False,
            after_cpu=cpu,
            after_memory=memory,
            after_irq_state=irq_state,
            blocked_reason=None,
            note="Hardware-vector IRQ deferred: iff_level or XSP not modelled yet.",
        )
    sr_value = encode_sr_from_state(cpu)
    if sr_value is None:
        return IrqDeliveryResult(
            delivered=False,
            after_cpu=cpu,
            after_memory=memory,
            after_irq_state=irq_state,
            blocked_reason=None,
            note="Hardware-vector IRQ deferred: SR not fully modelled.",
        )

    # Pick the highest-priority deliverable source (ties: lowest vector index,
    # which is the chip's own default-priority order).
    best: tuple[int, int] | None = None  # (level, index)
    for index in sorted(irq_state.pending_vectors):
        level = irq_level_from_priority_register(index, memory)
        if level is None or level == 0:
            continue  # unknown register, or level 0 = source disabled
        if level < cpu.iff_level:
            continue  # masked: accepted only when L >= IFF
        if best is None or level > best[0]:
            best = (level, index)
    if best is None:
        return IrqDeliveryResult(
            delivered=False,
            after_cpu=cpu,
            after_memory=memory,
            after_irq_state=irq_state,
            blocked_reason=None,
            note=(
                "Hardware-vector IRQ pending but none deliverable "
                f"(iff_level={cpu.iff_level}; sources either masked or at level 0)."
            ),
        )
    level, index = best

    hw_slot = irq_hw_vector_slot(index)
    hw_bytes = _read_runtime_bytes_silent(view, memory, hw_slot, 4)
    target = (
        int.from_bytes(hw_bytes, "little") & 0xFFFFFF if hw_bytes is not None else 0
    )
    if target == 0:
        return IrqDeliveryResult(
            delivered=False,
            after_cpu=cpu,
            after_memory=memory,
            after_irq_state=irq_state,
            blocked_reason=None,
            note=(
                f"Hardware-vector IRQ (index {index}) has no handler: vector slot "
                f"0x{hw_slot:06X} is unreadable or zero (no BIOS attached?)."
            ),
        )

    xsp = cpu.regs.xsp
    sr_target = (xsp - 2) & 0xFFFFFFFF
    pc_target = (xsp - 6) & 0xFFFFFFFF
    for addr, size in ((sr_target, 2), (pc_target, 4)):
        status, note = _check_writable_range(view, _mask_address(addr), size)
        if status is not None:
            return IrqDeliveryResult(
                delivered=False,
                after_cpu=cpu,
                after_memory=memory,
                after_irq_state=irq_state,
                blocked_reason=status,
                note=f"Hardware-vector IRQ cannot push its stack frame: {note}",
            )

    new_memory = dict(memory)
    for offset, byte in enumerate(sr_value.to_bytes(2, "little")):
        new_memory[_mask_address(sr_target + offset)] = byte
    for offset, byte in enumerate((cpu.pc & 0xFFFFFFFF).to_bytes(4, "little")):
        new_memory[_mask_address(pc_target + offset)] = byte

    modeled_fields = cpu.modeled_fields
    for field in ("executed-subset", "IFF"):
        if field not in modeled_fields:
            modeled_fields = (*modeled_fields, field)

    new_iff = min(level + 1, 7)  # Toshiba: mask := received level + 1
    after_cpu = replace(
        cpu,
        pc=_mask_address(target),
        regs=replace(cpu.regs, xsp=pc_target),
        iff_level=new_iff,
        iff_enabled=(new_iff < 7),
        control_registers=_adjust_intnest(cpu, +1),
        modeled_fields=modeled_fields,
        note=(
            f"{cpu.note} Delivered hardware-vector IRQ index {index} (level {level}) "
            f"via 0x{hw_slot:06X} -> handler 0x{target:06X}."
        ),
    )
    return IrqDeliveryResult(
        delivered=True,
        after_cpu=after_cpu,
        after_memory=new_memory,
        after_irq_state=irq_state.with_vector_cleared(index),
        blocked_reason=None,
        note=(
            f"Delivered hardware-vector IRQ index {index} (level {level}) through the "
            f"hardware vector table slot 0x{hw_slot:06X} -> BIOS handler "
            f"0x{target:06X}; raised iff_level to {new_iff}."
        ),
        cycles_consumed=IRQ_DELIVERY_CYCLES,
        vector_slot_address=hw_slot,
        vector_slot_raw=target,
        vector_target=_mask_address(target),
        used_handler_pointer=True,
        used_slot_fallback=False,
        used_hw_vector_table=True,
    )


def try_deliver_pending_irq(
    view: NgpcFetchView,
    cpu: NgpcCpuState,
    memory: dict[int, int],
    irq_state: "IrqState",
) -> IrqDeliveryResult:
    """Sample IRQ controller between instructions and deliver if possible.

    Phase 3.2.2b currently models only the VBlank source (level 4 at
    `VBLANK_VECTOR_ADDRESS = 0x006FCC`). Gating rule per TLCS-900/H
    interrupt controller: a pending IRQ at level L is delivered when
    `L > cpu.iff_level` (the iff field is the *maximum masked* level,
    so an IRQ above that level interrupts).

    Stack frame layout (matches `_try_execute_reti` per Toshiba spec):
      SR pushed first (2 bytes) — ends up at XSP+4..XSP+5
      PC pushed second (4 bytes) — ends up on top at XSP..XSP+3
    XSP decremented by 6 total. RETI pops PC then SR.

    After delivery: `iff_level` is raised to the delivered IRQ's
    level (so same-or-lower-priority IRQs are masked during the ISR),
    and the pending bit is cleared. Control transfer prefers the
    4-byte handler pointer stored at the vector slot itself (matching
    the SDK's `*(Interrupt**)0x6FCC = handler` model). If the slot is
    still zero / unset, the current minimal model falls back to the
    vector address directly so bootstrap-only workflows remain usable.
    """
    from core.adc import IRQ_VECTOR_INDEX_INTAD
    from core.frame_timing import (
        IRQ_LEVEL_VBLANK,
        IRQ_VECTOR_INDEX_VBLANK,
        K2GE_CONTROL_ADDRESS,
        K2GE_VBLANK_IRQ_ENABLE_BIT,
        IrqState,
        VBLANK_VECTOR_ADDRESS,
        irq_hw_vector_slot,
    )

    # --- WHICH SOURCE ------------------------------------------------------
    # This used to be `if not irq_state.is_vblank_pending(): return` -- VBlank was
    # the only source that could ever be DELIVERED, even though `run_steps` has
    # been folding the A/D completion interrupt into `pending_vectors` all along.
    # So INTAD went pending and stayed pending forever.
    #
    # That is not cosmetic. INTAD is the interrupt whose handler refills the
    # battery reading at 0x6F80, and the BIOS powers the console OFF when that
    # reading looks flat (core/adc.py, module docstring). A whole-ROM trace of
    # Densetsu no Ogre Battle against the native core is what surfaced it: the two
    # cores agreed on 913 instructions and then disagreed about the interrupt mask,
    # because one of them had taken an interrupt the other could not take.
    #
    # Both sources sit at level 4. Ties go to the lower vector index, which is the
    # datasheet's own priority order, so VBlank wins a simultaneous raise.
    from core.frame_timing import (
        IRQ_HW_PRIORITY_REGISTERS,
        irq_level_from_priority_register,
    )

    # INTERRUPT PRIORITY IS PROGRAMMABLE, NOT A CONSTANT.
    #
    # VBlank sits at a fixed level 4 (the SNK SDK says so outright). Every OTHER
    # source reads its level out of an INTxx register nibble at delivery time, and
    # **a level of 0 means software has DISABLED it** (Toshiba's levels run 1..7).
    # `IRQ_HW_PRIORITY_REGISTERS` has held that map since Phase 3.2.2b; nothing was
    # using it, because nothing but VBlank was ever delivered.
    #
    # The native core hard-coded level 4 for the timers on its first attempt and
    # the corpus answered immediately: 69 ROMs running two million instructions
    # cleanly fell to 56, sixteen of them parked on a HALT, because timers whose
    # level software had left at 0 were firing anyway. Read the register.
    candidates: list[tuple[int, int, str]] = []      # (level, vector index, name)
    if irq_state.is_vblank_pending():
        candidates.append((IRQ_LEVEL_VBLANK, IRQ_VECTOR_INDEX_VBLANK, "VBlank"))
    for vector_index in sorted(IRQ_HW_PRIORITY_REGISTERS):
        if not irq_state.is_vector_pending(vector_index):
            continue
        level = irq_level_from_priority_register(vector_index, memory)
        if not level:                               # unknown, or 0 = disabled
            continue
        name = "INTAD" if vector_index == IRQ_VECTOR_INDEX_INTAD else f"vec[{vector_index}]"
        candidates.append((level, vector_index, name))

    if not candidates:
        return IrqDeliveryResult(
            delivered=False,
            after_cpu=cpu,
            after_memory=memory,
            after_irq_state=irq_state,
            blocked_reason=None,
            note="No deliverable IRQ pending.",
        )

    # Highest level wins; a tie goes to the lower vector index, which is the
    # datasheet's own priority order.
    src_level, src_vector_index, src_name = max(candidates, key=lambda c: (c[0], -c[1]))
    src_is_vblank = src_vector_index == IRQ_VECTOR_INDEX_VBLANK

    if cpu.iff_level is None:
        # Soft fail: we can't decide whether to deliver, so treat it as
        # "don't deliver this iteration". This keeps step-exec usable
        # from bootstrap CPU states (where iff_level is None until
        # software runs `ei`/`di` or pops an SR). NOT a blocked_reason
        # — the run continues normally.
        return IrqDeliveryResult(
            delivered=False,
            after_cpu=cpu,
            after_memory=memory,
            after_irq_state=irq_state,
            blocked_reason=None,
            note=(
                "VBlank IRQ is pending but iff_level is unknown; deferring "
                "delivery until the CPU's interrupt-mask state becomes modeled."
            ),
        )

    # Source-enable gate: real hardware only raises VBlank when the K2GE control
    # register has its VBlank-IRQ-enable bit set (reference emulator:
    # `ram[0x8000] & 0x80`). Without this we delivered VBlank even to software
    # that had explicitly disabled it.
    k2ge_control = (
        _read_runtime_bytes_silent(view, memory, K2GE_CONTROL_ADDRESS, 1)
        if src_is_vblank
        else None      # INTAD has no K2GE gate: the converter only fires if asked
    )
    if k2ge_control is not None and not (
        k2ge_control[0] & K2GE_VBLANK_IRQ_ENABLE_BIT
    ):
        return IrqDeliveryResult(
            delivered=False,
            after_cpu=cpu,
            after_memory=memory,
            after_irq_state=irq_state,
            blocked_reason=None,
            note=(
                "VBlank IRQ pending but DISABLED at the source: the K2GE control "
                f"register 0x{K2GE_CONTROL_ADDRESS:06X} has its VBlank-enable bit "
                f"(0x{K2GE_VBLANK_IRQ_ENABLE_BIT:02X}) clear."
            ),
        )

    # Mask gate, per the Toshiba TLCS-900/L1 CPU manual (SR bits 12-14, IFF2:0):
    #   000/001 -> enables interrupts with level 1 or higher
    #   010     -> level 2 or higher   ...   110 -> level 6 or higher
    #   111     -> level 7 only (non-maskable)
    # i.e. an interrupt of level L is ACCEPTED when **L >= IFF**. We previously
    # required L > IFF (strictly greater), which is off by one: at IFF=6 a
    # level-6 VBlank must still be taken ("level 6 or higher"), but we masked it.
    # That is exactly why the real BIOS boot sat forever at iff_level=6 with zero
    # IRQ deliveries. Datasheet-grounded fix (2026-07-10).
    if src_level < cpu.iff_level:
        return IrqDeliveryResult(
            delivered=False,
            after_cpu=cpu,
            after_memory=memory,
            after_irq_state=irq_state,
            blocked_reason=None,
            note=(
                f"{src_name} IRQ pending but masked: iff_level={cpu.iff_level} "
                f"> level {src_level}."
            ),
        )

    sr_value = encode_sr_from_state(cpu)
    if sr_value is None:
        # Soft defer: SR not fully modeled yet (some flag is None).
        # The run continues — once software touches the flags or
        # pops an SR, the next sample can deliver.
        return IrqDeliveryResult(
            delivered=False,
            after_cpu=cpu,
            after_memory=memory,
            after_irq_state=irq_state,
            blocked_reason=None,
            note=(
                "VBlank IRQ delivery deferred: SR shape not fully modeled "
                "(six flags + iff_level + rfp required to push SR)."
            ),
        )

    xsp = cpu.regs.xsp
    if xsp is None:
        # Soft defer: XSP not modeled yet. The run continues.
        return IrqDeliveryResult(
            delivered=False,
            after_cpu=cpu,
            after_memory=memory,
            after_irq_state=irq_state,
            blocked_reason=None,
            note=(
                "VBlank IRQ delivery deferred: XSP is unknown in the current "
                "CPU state, cannot push PC + SR onto the stack."
            ),
        )

    pc_bytes = (cpu.pc & 0xFFFFFFFF).to_bytes(4, "little")
    sr_bytes = sr_value.to_bytes(2, "little")
    # Toshiba TLCS-900/H convention: PC ends on top of the stack so RETI
    # pops PC first. We achieve that by pushing SR first (high address),
    # then PC second (low address = top of stack after both pushes).
    sr_target = (xsp - 2) & 0xFFFFFFFF
    pc_target = (xsp - 6) & 0xFFFFFFFF

    sr_status, sr_note = _check_writable_range(view, _mask_address(sr_target), 2)
    if sr_status is not None:
        return IrqDeliveryResult(
            delivered=False,
            after_cpu=cpu,
            after_memory=memory,
            after_irq_state=irq_state,
            blocked_reason=sr_status,
            note=f"VBlank IRQ delivery cannot push SR: {sr_note}",
        )
    pc_status, pc_note = _check_writable_range(view, _mask_address(pc_target), 4)
    if pc_status is not None:
        return IrqDeliveryResult(
            delivered=False,
            after_cpu=cpu,
            after_memory=memory,
            after_irq_state=irq_state,
            blocked_reason=pc_status,
            note=f"VBlank IRQ delivery cannot push PC: {pc_note}",
        )

    new_memory = dict(memory)
    for offset, byte in enumerate(sr_bytes):
        new_memory[_mask_address(sr_target + offset)] = byte
    for offset, byte in enumerate(pc_bytes):
        new_memory[_mask_address(pc_target + offset)] = byte

    # --- Vector resolution -------------------------------------------------
    # HARDWARE-FAITHFUL path: on real silicon every interrupt vectors through
    # the CPU's hardware vector table in BIOS ROM (0xFFFF00 + index*4). For
    # VBlank that is vec[11] -> the BIOS frame handler, which does its per-frame
    # work and THEN chains to the user hook at 0x006FCC. Prefer this whenever a
    # BIOS image is attached (the table is only readable when it is).
    #
    # FALLBACK (no BIOS attached): jump via the RAM hook 0x006FCC directly. That
    # is a homebrew-only shortcut -- homebrew installs its ISR there and runs
    # BIOS-less -- and is NOT what hardware does. Documented, not silent.
    vector_slot_address = VBLANK_VECTOR_ADDRESS
    vector_target = VBLANK_VECTOR_ADDRESS
    vector_note = f"set PC to vector slot 0x{VBLANK_VECTOR_ADDRESS:08X}"
    vector_slot_raw = None
    used_handler_pointer = False
    used_slot_fallback = True
    used_hw_vector_table = False

    hw_slot = irq_hw_vector_slot(src_vector_index)
    hw_bytes = _read_runtime_bytes_silent(view, new_memory, hw_slot, 4)
    hw_target_raw = (
        int.from_bytes(hw_bytes, "little") & 0xFFFFFFFF
        if hw_bytes is not None
        else 0
    )
    if hw_target_raw != 0:
        vector_slot_address = hw_slot
        vector_slot_raw = hw_target_raw
        vector_target = _mask_address(hw_target_raw)
        used_handler_pointer = True
        used_slot_fallback = False
        used_hw_vector_table = True
        vector_note = (
            f"vectored through the hardware interrupt table: slot 0x{hw_slot:06X} "
            f"(vec[{src_vector_index}]) -> BIOS handler 0x{vector_target:08X}, "
            f"which chains to the user hook 0x{VBLANK_VECTOR_ADDRESS:06X}"
        )
    elif not src_is_vblank:
        # No BIOS attached and no RAM hook exists for INTAD: there is nowhere to
        # vector. Leave it pending rather than jump somewhere invented.
        return IrqDeliveryResult(
            delivered=False,
            after_cpu=cpu,
            after_memory=memory,
            after_irq_state=irq_state,
            blocked_reason=None,
            note=(
                "INTAD is pending but the hardware vector table is unreadable (no "
                "BIOS attached) and INTAD has no RAM hook. Deferred."
            ),
        )
    else:
        vector_bytes = _read_runtime_bytes_silent(
            view, new_memory, VBLANK_VECTOR_ADDRESS, 4
        )
        if vector_bytes is not None:
            raw_target = int.from_bytes(vector_bytes, "little") & 0xFFFFFFFF
            vector_slot_raw = raw_target
            if raw_target != 0:
                vector_target = _mask_address(raw_target)
                used_handler_pointer = True
                used_slot_fallback = False
                vector_note = (
                    f"no BIOS attached, so the hardware vector table is unreadable; "
                    f"loaded handler 0x{vector_target:08X} from the RAM hook "
                    f"0x{VBLANK_VECTOR_ADDRESS:08X} (homebrew shortcut)"
                )
            else:
                vector_note = (
                    f"vector slot 0x{VBLANK_VECTOR_ADDRESS:08X} is unset (0x00000000), "
                    f"so delivery falls back to the slot address itself"
                )

    new_xsp = pc_target
    # Post-acceptance mask, per the Toshiba TLCS-900/L1 CPU manual: "When an
    # interrupt is received, the mask register sets a value HIGHER BY 1 than the
    # interrupt level received. When an interrupt with level 7 is received, 111
    # is set." So IFF = min(level + 1, 7) -- NOT `IFF = level` (what we used to
    # do, which left the same level unmasked inside its own handler and so let it
    # re-enter). Datasheet-grounded fix (2026-07-10).
    new_iff_level = min(src_level + 1, 7)
    new_control_registers = _adjust_intnest(cpu, +1)

    modeled_fields = cpu.modeled_fields
    for field in ("executed-subset", "IFF"):
        if field not in modeled_fields:
            modeled_fields = (*modeled_fields, field)

    after_cpu = replace(
        cpu,
        pc=vector_target,
        regs=replace(cpu.regs, xsp=new_xsp),
        iff_level=new_iff_level,
        iff_enabled=(new_iff_level < 7),
        control_registers=new_control_registers,
        modeled_fields=modeled_fields,
        note=(
            f"{cpu.note} Delivered {src_name} IRQ: pushed PC=0x{cpu.pc:08X} (4B) "
            f"and SR=0x{sr_value:04X} (2B), {vector_note}, "
            f"raised iff_level to {new_iff_level}."
        ),
    )
    after_irq_state = (
        irq_state.with_vblank_cleared()
        if src_is_vblank
        else irq_state.with_vector_cleared(src_vector_index)
    )

    return IrqDeliveryResult(
        delivered=True,
        after_cpu=after_cpu,
        after_memory=new_memory,
        after_irq_state=after_irq_state,
        blocked_reason=None,
        note=(
            f"Delivered {src_name} IRQ (level {src_level}); {vector_note}. "
            f"Stack frame: PC at 0x{pc_target:08X}, SR at 0x{sr_target:08X}, "
            f"XSP advanced from 0x{xsp:08X} to 0x{new_xsp:08X}."
        ),
        cycles_consumed=IRQ_DELIVERY_CYCLES,
        vector_slot_address=vector_slot_address,
        vector_slot_raw=vector_slot_raw,
        vector_target=vector_target,
        used_handler_pointer=used_handler_pointer,
        used_slot_fallback=used_slot_fallback,
        used_hw_vector_table=used_hw_vector_table,
    )


def _try_execute_reti(
    view: NgpcFetchView,
    before_cpu: NgpcCpuState,
    before_memory: dict[int, int],
    decoded: DecodeResult,
) -> ExecutionResult | None:
    """Execute RETI (0x07) — return from interrupt.

    TLCS-900/H stack frame on IRQ entry (per Toshiba spec):
      [XSP+0..3] = saved PC (32-bit, little-endian, on top of stack)
      [XSP+4..5] = saved SR (16-bit, little-endian, below PC)

    RETI pops PC first (4 bytes), then SR (2 bytes). XSP advances
    by 6 total. The popped SR restores all six flags + iff_level +
    rfp atomically.
    """
    raw = decoded.raw_bytes
    if raw is None or len(raw) != 1 or raw[0] != 0x07:
        return None
    if decoded.mnemonic != "reti":
        return None

    xsp = before_cpu.regs.xsp
    if xsp is None:
        return _blocked_result(
            before_cpu=before_cpu,
            decoded=decoded,
            status="requires-known-stack-pointer",
            note=(
                "RETI needs XSP, but the current bootstrap CPU state still "
                "leaves the stack pointer unknown."
            ),
        )

    pc_data = _read_runtime_bytes(view, before_memory, _mask_address(xsp), 4)
    if pc_data is None:
        return _blocked_result(
            before_cpu=before_cpu,
            decoded=decoded,
            status="stack-data-unavailable",
            note=(
                "RETI needs 4 readable bytes at XSP for the saved PC, but the "
                "current writable stack model and read bus do not provide them."
            ),
        )
    sr_data = _read_runtime_bytes(view, before_memory, _mask_address(xsp + 4), 2)
    if sr_data is None:
        return _blocked_result(
            before_cpu=before_cpu,
            decoded=decoded,
            status="stack-data-unavailable",
            note=(
                "RETI needs 2 readable bytes at XSP+4 for the saved SR, but "
                "the current writable stack model and read bus do not provide them."
            ),
        )

    new_pc = int.from_bytes(pc_data, "little") & 0xFFFFFFFF
    sr_value = int.from_bytes(sr_data, "little")
    fields = decode_sr_to_fields(sr_value)
    new_flags = StatusFlags(
        sf=bool(fields["sf"]),
        zf=bool(fields["zf"]),
        vf=bool(fields["vf"]),
        hf=bool(fields["hf"]),
        cf=bool(fields["cf"]),
        nf=bool(fields["nf"]),
    )
    new_iff_level = int(fields["iff_level"])
    new_rfp = int(fields["rfp"])
    new_xsp = (xsp + 6) & 0xFFFFFFFF
    old_bank, banks, new_regs = _switch_visible_core_bank(before_cpu, new_rfp)
    new_control_registers = _adjust_intnest(before_cpu, -1)

    modeled_fields = before_cpu.modeled_fields
    for field in ("executed-subset", "modeled-flags-subset", "IFF", "RFP"):
        if field not in modeled_fields:
            modeled_fields = (*modeled_fields, field)

    after_cpu = replace(
        before_cpu,
        pc=new_pc,
        regs=replace(new_regs, xsp=new_xsp),
        flags=new_flags,
        iff_level=new_iff_level,
        iff_enabled=(new_iff_level < 7),
        rfp=new_rfp,
        register_bank=new_rfp,
        register_banks=banks,
        control_registers=new_control_registers,
        sr_raw=sr_value,
        modeled_fields=modeled_fields,
        note=(
            f"{before_cpu.note} Executed RETI: PC=0x{new_pc:08X} popped from "
            f"0x{xsp:08X}, SR=0x{sr_value:04X} popped from 0x{(xsp + 4) & 0xFFFFFFFF:08X}; "
            f"XSP advanced to 0x{new_xsp:08X}. Outgoing bank {old_bank} was "
            f"flushed and bank {new_rfp} reloaded into the visible "
            "core-register window."
        ),
    )

    return ExecutionResult(
        before_cpu=before_cpu,
        after_cpu=after_cpu,
        decode=decoded,
        status="executed",
        written_registers=("SR", "IFF", "RFP", "XSP", "PC"),
        memory_writes=(),
        after_memory=before_memory,
        note=(
            f"Executed RETI: popped PC=0x{new_pc:08X} (4B) and SR=0x{sr_value:04X} "
            f"(2B) from stack at 0x{xsp:08X}; XSP advanced by 6 to 0x{new_xsp:08X}. "
            f"Bank {old_bank} was flushed and bank {new_rfp} reloaded for "
            "XWA/XBC/XDE/XHL."
        ),
        cycles_consumed=RETI_CYCLES,
    )


def _try_execute_prefixed_inc_dec(
    before_cpu: NgpcCpuState,
    before_memory: dict[int, int],
    decoded: DecodeResult,
) -> ExecutionResult | None:
    raw = decoded.raw_bytes
    if raw is None or len(raw) < 2:
        return None

    info = _prefixed_register_execute_info(raw[0])
    if info is None:
        return None

    size_kind, register_index = info
    second = raw[1]
    count = second & 0x07
    if count == 0:
        count = 8

    if 0x60 <= second <= 0x67:
        return _execute_register_inc_dec(
            before_cpu=before_cpu,
            before_memory=before_memory,
            decoded=decoded,
            size_kind=size_kind,
            register_index=register_index,
            count=count,
            operation="inc",
        )

    if 0x68 <= second <= 0x6F:
        return _execute_register_inc_dec(
            before_cpu=before_cpu,
            before_memory=before_memory,
            decoded=decoded,
            size_kind=size_kind,
            register_index=register_index,
            count=count,
            operation="dec",
        )

    if second == 0x1C:
        if len(raw) != 3 or decoded.direct_target is None or decoded.next_sequential_pc is None:
            return None
        if size_kind == "long":
            return _blocked_result(
                before_cpu=before_cpu,
                decoded=decoded,
                status="silicon-undefined",
                note=(
                    f"{decoded.assembly} is only defined for byte/word register operands in the "
                    "local Toshiba TLCS-900/L1 table."
                ),
            )

        register_name, current_value = _extract_register_value(
            before_cpu=before_cpu,
            size_kind=size_kind,
            register_index=register_index,
        )
        if current_value is None:
            owner_name = R32[register_index // 2] if size_kind == "byte" else R32[register_index]
            return _blocked_result(
                before_cpu=before_cpu,
                decoded=decoded,
                status="requires-known-full-register",
                note=(
                    f"{register_name} must be known before this djnz can decrement and test it "
                    f"honestly. Owner register {owner_name} is not yet in the current CPU state."
                ),
            )

        bits = {"byte": 8, "word": 16}[size_kind]
        mask = (1 << bits) - 1
        new_value = (current_value - 1) & mask
        branch_taken = new_value != 0
        reg_update_name, reg_updates = _build_register_update(
            before_cpu,
            size_kind=size_kind,
            register_index=register_index,
            value=new_value,
        )
        if reg_updates is None:
            owner_name = R32[register_index // 2] if size_kind == "byte" else R32[register_index]
            return _blocked_result(
                before_cpu=before_cpu,
                decoded=decoded,
                status="requires-known-full-register",
                note=(
                    f"{register_name} cannot be written back honestly until its owning register "
                    f"{owner_name} is already known in the current CPU state."
                ),
            )

        new_pc = decoded.direct_target if branch_taken else decoded.next_sequential_pc
        branch_text = "taken" if branch_taken else "not taken"
        return _executed_result(
            before_cpu=before_cpu,
            decoded=decoded,
            written_registers=(reg_update_name, "PC"),
            memory_writes=(),
            after_memory=before_memory,
            new_pc=new_pc,
            reg_updates=reg_updates,
            note=(
                "Executed DJNZ from the current real execution subset. "
                f"{register_name} decremented to 0x{new_value:0{({'byte': 2, 'word': 4}[size_kind])}X}; "
                f"branch was {branch_text}."
            ),
            cycles_consumed=_executed_cycles_from_decoded(decoded, branch_taken=branch_taken),
        )

    if 0x70 <= second <= 0x7F:
        # SCC cc, r — set register to 1 if condition cc is true, else 0.
        # CC index is the full low nibble (0..15), not just the low 3 bits.
        cc_idx = second & 0x0F
        if cc_idx == 0:
            condition_result = False
        elif cc_idx == 8:
            condition_result = True
        else:
            condition_result = _evaluate_condition_code(cc_idx, before_cpu.flags)
        if condition_result is None:
            return _blocked_result(
                before_cpu=before_cpu,
                decoded=decoded,
                status="requires-known-flags",
                note=(
                    f"scc {CC[cc_idx]}, r needs flag(s) the current CPU model has not yet "
                    f"tracked. Run this instruction after a prior op that sets the required "
                    f"flags so the condition becomes known."
                ),
            )
        value = 1 if condition_result else 0
        return _execute_register_immediate(
            before_cpu=before_cpu,
            before_memory=before_memory,
            decoded=decoded,
            size_kind=size_kind,
            register_index=register_index,
            value=value,
            note=(
                f"Executed scc {CC[cc_idx]}, r: condition was "
                f"{'true' if condition_result else 'false'}, register was set to {value}."
            ),
        )

    return None


def _execute_register_immediate(
    before_cpu: NgpcCpuState,
    before_memory: dict[int, int],
    decoded: DecodeResult,
    size_kind: str,
    register_index: int,
    value: int,
    note: str | None = None,
) -> ExecutionResult:
    register_name, reg_updates = _build_register_update(
        before_cpu,
        size_kind=size_kind,
        register_index=register_index,
        value=value,
    )
    if reg_updates is None:
        if size_kind == "byte":
            owner_name = R32[register_index // 2]
            target_name = R8[register_index]
        else:
            owner_name = R32[register_index]
            target_name = R16[register_index] if size_kind == "word" else R32[register_index]
        return _blocked_result(
            before_cpu=before_cpu,
            decoded=decoded,
            status="requires-known-full-register",
            note=(
                f"{target_name} is only partially representable in the current CPU model. "
                f"This write can only be applied honestly when {owner_name} is already known."
            ),
        )

    return _executed_result(
        before_cpu=before_cpu,
        decoded=decoded,
        written_registers=(register_name, "PC"),
        memory_writes=(),
        after_memory=before_memory,
        new_pc=decoded.next_sequential_pc,
        reg_updates=reg_updates,
        note=note
        if note is not None
        else (
            "Executed an immediate register load from the current real execution subset. "
            "PC advanced and the targeted register view is now updated in the CPU state."
        ),
    )


def _try_execute_conditional_branch(
    before_cpu: NgpcCpuState,
    before_memory: dict[int, int],
    decoded: DecodeResult,
) -> ExecutionResult | None:
    raw = decoded.raw_bytes
    if (
        decoded.control_flow_kind != "conditional-branch"
        or raw is None
        or decoded.direct_target is None
        or decoded.next_sequential_pc is None
    ):
        return None

    if decoded.mnemonic not in {"jr", "jrl"}:
        return None

    condition_result = _evaluate_condition_code(raw[0] & 0x0F, before_cpu.flags)
    if condition_result is None:
        return None

    new_pc = decoded.direct_target if condition_result else decoded.next_sequential_pc
    branch_text = "taken" if condition_result else "not taken"
    return _executed_result(
        before_cpu=before_cpu,
        decoded=decoded,
        written_registers=("PC",),
        memory_writes=(),
        after_memory=before_memory,
        new_pc=new_pc,
        reg_updates=None,
        note=(
            "Executed conditional branch from the current real execution subset. The branch "
            f"condition was modeled from the current known flag subset and was {branch_text}."
        ),
        cycles_consumed=_executed_cycles_from_decoded(
            decoded, branch_taken=condition_result,
        ),
    )


def _try_execute_abs_conditional_jump(
    before_cpu: NgpcCpuState,
    before_memory: dict[int, int],
    decoded: DecodeResult,
) -> ExecutionResult | None:
    """Execute `jp cc, (absN)` -- conditional jump to an absolute address.

    PC = direct_target (the absolute effective address, NOT dereferenced) if the
    condition holds, else the next sequential PC. cc = op & 0x0F; the op byte is
    the last prefix byte of the F0/F1/F2 abs8/abs16/abs24 forms. Unconditional
    (cc=8) is decoded as a plain "jump" and handled by the generic direct path.
    F2 `jp cc, (abs24)` is the entry frontier of ~12 retail carts.
    """
    raw = decoded.raw_bytes
    if (
        decoded.control_flow_kind != "conditional-branch"
        or decoded.mnemonic != "jp"
        or raw is None
        or decoded.direct_target is None
        or decoded.next_sequential_pc is None
    ):
        return None
    if raw[0] == 0xF2 and len(raw) >= 5:
        cc_idx = raw[4] & 0x0F
    elif raw[0] == 0xF1 and len(raw) >= 4:
        cc_idx = raw[3] & 0x0F
    elif raw[0] == 0xF0 and len(raw) >= 3:
        cc_idx = raw[2] & 0x0F
    else:
        return None

    condition_result = _evaluate_condition_code(cc_idx, before_cpu.flags)
    if condition_result is None:
        return None  # flags unknown -> the generic conditional-branch block stops honestly

    new_pc = decoded.direct_target if condition_result else decoded.next_sequential_pc
    branch_text = "taken" if condition_result else "not taken"
    return _executed_result(
        before_cpu=before_cpu,
        decoded=decoded,
        written_registers=("PC",),
        memory_writes=(),
        after_memory=before_memory,
        new_pc=new_pc,
        reg_updates=None,
        note=(
            "Executed jp cc, (abs) conditional absolute jump from the current real execution "
            f"subset. The branch was {branch_text}."
        ),
    )


def _execute_register_inc_dec(
    before_cpu: NgpcCpuState,
    before_memory: dict[int, int],
    decoded: DecodeResult,
    size_kind: str,
    register_index: int,
    count: int,
    operation: str,
) -> ExecutionResult:
    register_name, current_value = _extract_register_value(
        before_cpu=before_cpu,
        size_kind=size_kind,
        register_index=register_index,
    )
    if current_value is None:
        owner_name = R32[register_index // 2] if size_kind == "byte" else R32[register_index]
        return _blocked_result(
            before_cpu=before_cpu,
            decoded=decoded,
            status="requires-known-full-register",
            note=(
                f"{register_name} cannot be updated honestly by {operation} until "
                f"{owner_name} is already known in the current CPU state."
            ),
        )

    mask = {"byte": 0xFF, "word": 0xFFFF, "long": 0xFFFFFFFF}[size_kind]
    delta = count if operation == "inc" else -count
    new_value = (current_value + delta) & mask
    register_name, reg_updates = _build_register_update(
        before_cpu=before_cpu,
        size_kind=size_kind,
        register_index=register_index,
        value=new_value,
    )
    if reg_updates is None:
        owner_name = R32[register_index // 2] if size_kind == "byte" else R32[register_index]
        return _blocked_result(
            before_cpu=before_cpu,
            decoded=decoded,
            status="requires-known-full-register",
            note=(
                f"{register_name} cannot be updated honestly by {operation} until "
                f"{owner_name} is already known in the current CPU state."
            ),
        )

    # ⭐ THE FLAGS DEPEND ON THE SIZE, AND ONLY THE BYTE FORM HAS ANY.
    #
    # Toshiba's instruction list gives this opcode THREE rows, and they disagree:
    #
    #     INC #3, r   `C8 + r`        size B - -   flags  * * * V 0 -
    #     INC #3, r   `C8 + zz + r`   size - W L   flags  - - - - - -
    #     INC<W> #3, (mem)            size B W -   flags  * * * V 0 -
    #     (DEC is identical, with N = 1 instead of 0.)
    #
    # A 16- or 32-bit register INC/DEC changes NOTHING. Both cores wrote flags at
    # every size, agreed with each other perfectly, and were both wrong -- which is
    # exactly what a differential gate cannot catch. It cost four ROMs.
    #
    # Faselei's `strlen` is why. It finds the NUL with a block compare, then steps
    # back onto it:
    #
    #     243B74  cpir (XHL)      ; search -- sets Z when it MATCHES
    #     243B76  dec 1, XHL      ; back up onto the byte it found
    #     243B78  ret Z           ; "found" -- reading the CPIR's Z
    #
    # Clobber Z in that DEC and every found string becomes "not found". `strlen`
    # returns its 0xFFFF not-found sentinel, the caller hands that to a memcpy, and
    # 65 534 bytes go over the stack -- return address included. The CPU returned to
    # address 0, hit the `swi 7` there, and the BIOS powered the console off.
    #
    # (The earlier note here said the byte row applied at every size. It cited the
    # right table and read one row of it.)
    if size_kind == "byte":
        if operation == "inc":
            flags_updates = _compute_add_flags(size_kind, current_value, count)
        else:
            flags_updates = _compute_subtract_flags(size_kind, current_value, count)
        flags_updates.pop("cf")   # C is not touched by INC/DEC
    else:
        flags_updates = {}        # word / long: `- - - - - -`

    verb = "incremented" if operation == "inc" else "decremented"
    return _executed_result(
        before_cpu=before_cpu,
        decoded=decoded,
        written_registers=(register_name, "PC"),
        memory_writes=(),
        after_memory=before_memory,
        new_pc=decoded.next_sequential_pc,
        reg_updates=reg_updates,
        flags_updates=flags_updates,
        note=(
            "Executed prefixed register arithmetic from the current real execution subset. "
            f"The targeted register view was {verb} by {count} and PC advanced "
            "sequentially. Flags S/Z/H/V/N updated per the Toshiba instruction list; "
            "C is preserved."
        ),
    )


def _try_execute_link_unlk(
    view: NgpcFetchView,
    before_cpu: NgpcCpuState,
    before_memory: dict[int, int],
    decoded: DecodeResult,
) -> ExecutionResult | None:
    """Execute link Rxx, d16 / unlk Rxx (frame pointer setup/teardown).

    Encoding (TLCS-900 stack frame instructions):
      link Rxx, d16 = (0xE8+r) 0x0C disp_lo disp_hi   (4 bytes)
      unlk Rxx      = (0xE8+r) 0x0D                    (2 bytes)

    `link Rxx, d16` semantics:
      1. push Rxx (4 bytes) at XSP-4
      2. Rxx = XSP after push
      3. XSP = Rxx + sign_extend(d16)

    `unlk Rxx` semantics:
      1. XSP = Rxx
      2. pop Rxx (4 bytes) from memory at XSP, XSP += 4

    Notes:
      - link/unlk with XSP (register index 7) is forbidden — self-reference
        on the stack pointer is not modeled.
      - link XIY, N >= 5 is silicon-broken on NGPC; the decoder already
        emits a warning. The executor performs the architectural operation
        anyway so traces remain consistent — the matched-quirk plumbing
        records the broken-on-HW status separately.
    """
    raw = decoded.raw_bytes
    if raw is None or len(raw) < 2:
        return None
    first = raw[0]
    if not (0xE8 <= first <= 0xEF):
        return None
    second = raw[1]
    if second not in (0x0C, 0x0D):
        return None

    register_index = first & 0x07
    if register_index == 7:
        return _blocked_result(
            before_cpu=before_cpu,
            decoded=decoded,
            status="unmodeled-stack-pointer-alias",
            note=(
                "link/unlk XSP is not modeled because the current subset "
                "does not represent the self-referential stack-pointer case."
            ),
        )

    register_name, current_value = _extract_register_value(
        before_cpu, "long", register_index
    )
    if current_value is None:
        return _blocked_result(
            before_cpu=before_cpu,
            decoded=decoded,
            status="requires-known-full-register",
            note=(
                f"{register_name} must be known before link/unlk can be executed honestly."
            ),
        )

    xsp = before_cpu.regs.xsp
    if xsp is None:
        return _blocked_result(
            before_cpu=before_cpu,
            decoded=decoded,
            status="requires-known-stack-pointer",
            note=(
                "link/unlk needs a known XSP, but the current bootstrap CPU "
                "state still leaves the stack pointer unknown."
            ),
        )

    if second == 0x0C:
        if len(raw) != 4:
            return None
        disp16 = int.from_bytes(raw[2:4], "little", signed=True)
        new_frame = (xsp - 4) & 0xFFFFFFFF
        new_xsp = (new_frame + disp16) & 0xFFFFFFFF

        target_address = _mask_address(new_frame)
        push_data = current_value.to_bytes(4, "little")
        write_status, write_note = _check_writable_range(view, target_address, 4)
        if write_status is not None:
            return _blocked_result(
                before_cpu=before_cpu,
                decoded=decoded,
                status=write_status,
                note=write_note,
            )
        after_memory = dict(before_memory)
        for offset, value in enumerate(push_data):
            after_memory[_mask_address(target_address + offset)] = value

        reg_field = REG32_FIELDS[register_index]
        reg_updates = {reg_field: new_frame, "xsp": new_xsp}

        return _executed_result(
            before_cpu=before_cpu,
            decoded=decoded,
            written_registers=(register_name, "XSP", "PC"),
            memory_writes=(
                MemoryWrite(
                    address=target_address,
                    data=push_data,
                    note="Writable stack model updated by LINK push.",
                ),
            ),
            after_memory=after_memory,
            new_pc=decoded.next_sequential_pc,
            reg_updates=reg_updates,
            note=(
                f"Executed LINK {register_name}, {disp16}: pushed previous "
                f"{register_name}=0x{current_value:08X} at 0x{target_address:06X}, "
                f"{register_name}=0x{new_frame:08X}, XSP=0x{new_xsp:08X}."
            ),
            cycles_consumed=LINK_CYCLES,
        )

    # second == 0x0D: unlk Rxx
    if len(raw) != 2:
        return None
    pop_address = _mask_address(current_value)
    popped = _read_runtime_bytes(view, before_memory, pop_address, 4)
    if popped is None:
        return _blocked_result(
            before_cpu=before_cpu,
            decoded=decoded,
            status="stack-data-unavailable",
            note=(
                "UNLK needs readable bytes at the frame pointer address, but the "
                "writable stack model and read bus do not provide them."
            ),
        )
    popped_value = int.from_bytes(popped, "little")
    new_xsp = (current_value + 4) & 0xFFFFFFFF
    reg_field = REG32_FIELDS[register_index]
    reg_updates = {reg_field: popped_value, "xsp": new_xsp}
    return _executed_result(
        before_cpu=before_cpu,
        decoded=decoded,
        written_registers=(register_name, "XSP", "PC"),
        memory_writes=(),
        after_memory=before_memory,
        new_pc=decoded.next_sequential_pc,
        reg_updates=reg_updates,
        note=(
            f"Executed UNLK {register_name}: XSP set to "
            f"{register_name}(0x{current_value:08X}), popped 4 bytes -> "
            f"{register_name}=0x{popped_value:08X}, XSP=0x{new_xsp:08X}."
        ),
        cycles_consumed=UNLK_CYCLES,
    )


def _execute_push_bytes(
    view: NgpcFetchView,
    before_cpu: NgpcCpuState,
    before_memory: dict[int, int],
    decoded: DecodeResult,
    data: bytes,
    note: str,
    cycles_consumed: int | None = None,
) -> ExecutionResult:
    xsp = before_cpu.regs.xsp
    if xsp is None:
        return _blocked_result(
            before_cpu=before_cpu,
            decoded=decoded,
            status="requires-known-stack-pointer",
            note=(
                "This instruction needs XSP, but the current bootstrap CPU state still leaves "
                "the stack pointer unknown."
            ),
        )

    new_xsp = (xsp - len(data)) & 0xFFFFFFFF
    target_address = _mask_address(new_xsp)
    write_status, write_note = _check_writable_range(view, target_address, len(data))
    if write_status is not None:
        return _blocked_result(
            before_cpu=before_cpu,
            decoded=decoded,
            status=write_status,
            note=write_note,
        )

    after_memory = dict(before_memory)
    for offset, value in enumerate(data):
        after_memory[_mask_address(target_address + offset)] = value

    return _executed_result(
        before_cpu=before_cpu,
        decoded=decoded,
        written_registers=("XSP", "PC"),
        memory_writes=(
            MemoryWrite(
                address=target_address,
                data=data,
                note="Writable stack model updated by PUSH-like execution.",
            ),
        ),
        after_memory=after_memory,
        new_pc=decoded.next_sequential_pc,
        reg_updates={"xsp": new_xsp},
        note=note,
        cycles_consumed=cycles_consumed,
    )


def _execute_pop_register(
    view: NgpcFetchView,
    before_cpu: NgpcCpuState,
    before_memory: dict[int, int],
    decoded: DecodeResult,
    size_kind: str,
    register_index: int,
    cycles_consumed: int | None = None,
) -> ExecutionResult:
    xsp = before_cpu.regs.xsp
    if xsp is None:
        return _blocked_result(
            before_cpu=before_cpu,
            decoded=decoded,
            status="requires-known-stack-pointer",
            note=(
                "This instruction needs XSP, but the current bootstrap CPU state still leaves "
                "the stack pointer unknown."
            ),
        )

    width = {"byte": 1, "word": 2, "long": 4}[size_kind]
    data = _read_runtime_bytes(view, before_memory, _mask_address(xsp), width)
    if data is None:
        return _blocked_result(
            before_cpu=before_cpu,
            decoded=decoded,
            status="stack-data-unavailable",
            note=(
                "This POP-like instruction needs readable bytes at the current XSP, but the "
                "current writable stack model and read bus do not provide them."
            ),
        )

    register_name, reg_updates = _build_register_update(
        before_cpu=before_cpu,
        size_kind=size_kind,
        register_index=register_index,
        value=int.from_bytes(data, "little"),
    )
    extra_cpu_updates = None
    if reg_updates is None and size_kind == "byte":
        reg_updates, extra_cpu_updates = _build_current_banked_r8_update(
            before_cpu, register_index, int.from_bytes(data, "little"),
        )
        register_name = R8[register_index]
    if reg_updates is None and extra_cpu_updates is None:
        return _blocked_result(
            before_cpu=before_cpu,
            decoded=decoded,
            status="requires-known-full-register",
            note=(
                f"{register_name} is only partially representable in the current CPU model. "
                "The POP destination cannot be updated honestly until its owning 32-bit register "
                "is already known."
            ),
        )

    if reg_updates is None:
        # A banked-r8 POP destination updates the CPU via `extra_cpu_updates`,
        # leaving `reg_updates` None; we still need a dict to carry the XSP bump.
        reg_updates = {}
    reg_updates["xsp"] = (xsp + width) & 0xFFFFFFFF
    return _executed_result(
        before_cpu=before_cpu,
        decoded=decoded,
        written_registers=(register_name, "XSP", "PC"),
        memory_writes=(),
        after_memory=before_memory,
        new_pc=decoded.next_sequential_pc,
        reg_updates=reg_updates,
        extra_cpu_updates=extra_cpu_updates,
        note=(
            "Executed POP from the current real execution subset. The destination register was "
            "loaded from the writable stack model or read bus, and XSP advanced accordingly."
        ),
        cycles_consumed=cycles_consumed,
    )


def _execute_call(
    view: NgpcFetchView,
    before_cpu: NgpcCpuState,
    before_memory: dict[int, int],
    decoded: DecodeResult,
    target_pc: int,
    return_pc: int,
    cycles_consumed: int | None = None,
) -> ExecutionResult:
    xsp = before_cpu.regs.xsp
    if xsp is None:
        return _blocked_result(
            before_cpu=before_cpu,
            decoded=decoded,
            status="requires-known-stack-pointer",
            note=(
                "This CALL-like instruction needs XSP, but the current bootstrap CPU state still "
                "leaves the stack pointer unknown."
            ),
        )

    return_bytes = return_pc.to_bytes(4, "little")
    new_xsp = (xsp - 4) & 0xFFFFFFFF
    target_address = _mask_address(new_xsp)
    write_status, write_note = _check_writable_range(view, target_address, 4)
    if write_status is not None:
        return _blocked_result(
            before_cpu=before_cpu,
            decoded=decoded,
            status=write_status,
            note=write_note,
        )

    after_memory = dict(before_memory)
    for offset, value in enumerate(return_bytes):
        after_memory[_mask_address(target_address + offset)] = value

    return _executed_result(
        before_cpu=before_cpu,
        decoded=decoded,
        written_registers=("XSP", "PC"),
        memory_writes=(
            MemoryWrite(
                address=target_address,
                data=return_bytes,
                note="Writable stack model updated with CALL return address.",
            ),
        ),
        after_memory=after_memory,
        new_pc=target_pc,
        reg_updates={"xsp": new_xsp},
        note=(
            "Executed CALL from the current real execution subset. The sequential return address "
            "was pushed to the writable stack model and PC moved to the direct target."
        ),
        cycles_consumed=cycles_consumed,
    )


def _execute_return(
    view: NgpcFetchView,
    before_cpu: NgpcCpuState,
    before_memory: dict[int, int],
    decoded: DecodeResult,
    stack_adjust: int,
    note: str,
    cycles_consumed: int | None = None,
) -> ExecutionResult:
    xsp = before_cpu.regs.xsp
    if xsp is None:
        return _blocked_result(
            before_cpu=before_cpu,
            decoded=decoded,
            status="requires-known-stack-pointer",
            note=(
                "This return-like instruction needs XSP, but the current bootstrap CPU state "
                "still leaves the stack pointer unknown."
            ),
        )

    return_bytes = _read_runtime_bytes(view, before_memory, _mask_address(xsp), 4)
    if return_bytes is None:
        return _blocked_result(
            before_cpu=before_cpu,
            decoded=decoded,
            status="stack-data-unavailable",
            note=(
                "This return-like instruction needs a saved PC at the current XSP, but the "
                "current writable stack model and read bus do not provide it."
            ),
        )

    new_pc = int.from_bytes(return_bytes, "little") & 0xFFFFFFFF
    new_xsp = (xsp + 4 + stack_adjust) & 0xFFFFFFFF
    return _executed_result(
        before_cpu=before_cpu,
        decoded=decoded,
        written_registers=("XSP", "PC"),
        memory_writes=(),
        after_memory=before_memory,
        new_pc=new_pc,
        reg_updates={"xsp": new_xsp},
        note=note,
        cycles_consumed=cycles_consumed,
    )


def _try_execute_ret_conditional(
    view: NgpcFetchView,
    before_cpu: NgpcCpuState,
    before_memory: dict[int, int],
    decoded: DecodeResult,
) -> ExecutionResult | None:
    """Execute conditional return: ret CC.

    Encoding: B0 [F0+CC_idx]  — 2 bytes.
    If condition is true: pop 4 bytes from XSP, jump to that address.
    If condition is false: advance PC (fall through).
    """
    raw = decoded.raw_bytes
    if raw is None or len(raw) != 2:
        return None
    if raw[0] != 0xB0 or not (0xF0 <= raw[1] <= 0xFF):
        return None

    cc_idx = raw[1] & 0x0F
    # CC index 8 = always true (unconditional ret) — handled by existing ret executor
    if cc_idx == 8:
        return None

    condition_result = _evaluate_condition_code(cc_idx, before_cpu.flags)
    if condition_result is None:
        # Condition flag not known — block
        return _blocked_result(
            before_cpu=before_cpu,
            decoded=decoded,
            status="runtime-state-required",
            note=(
                f"ret {CC[cc_idx]}: the condition flag(s) required to evaluate CC index {cc_idx} "
                "are not currently known in the CPU state."
            ),
        )

    if not condition_result:
        # Condition false: fall through
        return _executed_result(
            before_cpu=before_cpu,
            decoded=decoded,
            written_registers=("PC",),
            memory_writes=(),
            after_memory=before_memory,
            new_pc=decoded.next_sequential_pc,
            reg_updates=None,
            note=(
                f"Executed conditional ret {CC[cc_idx]} (condition false = fall through) "
                "from the current real execution subset."
            ),
            cycles_consumed=4,
        )

    # Condition true: perform actual return (pop 4 bytes from stack)
    return _execute_return(
        view=view,
        before_cpu=before_cpu,
        before_memory=before_memory,
        decoded=decoded,
        stack_adjust=0,
        note=(
            f"Executed conditional ret {CC[cc_idx]} (condition true = return) "
            "from the current real execution subset."
        ),
        cycles_consumed=12,
    )


def _extract_register_value(
    before_cpu: NgpcCpuState,
    size_kind: str,
    register_index: int,
) -> tuple[str, int | None]:
    if size_kind == "byte":
        register_name = R8[register_index]
        owner_value = getattr(before_cpu.regs, REG32_FIELDS[register_index // 2])
        if owner_value is None:
            return register_name, _extract_current_banked_r8_value(before_cpu, register_index)
        shift = 8 if register_index % 2 == 0 else 0
        return register_name, (owner_value >> shift) & 0xFF

    if size_kind == "long":
        register_name = R32[register_index]
        value = getattr(before_cpu.regs, REG32_FIELDS[register_index])
        if value is None:
            return register_name, _extract_current_banked_r32_value(before_cpu, register_index)
        return register_name, value

    register_name = R16[register_index]
    owner_value = getattr(before_cpu.regs, REG32_FIELDS[register_index])
    if owner_value is None:
        return register_name, _extract_current_banked_r16_value(before_cpu, register_index)
    return register_name, owner_value & 0xFFFF


def _build_register_update(
    before_cpu: NgpcCpuState,
    size_kind: str,
    register_index: int,
    value: int,
) -> tuple[str, dict[str, int] | None]:
    if size_kind == "long":
        return (
            R32[register_index],
            {REG32_FIELDS[register_index]: value & 0xFFFFFFFF},
        )

    if size_kind == "word":
        field_name = REG32_FIELDS[register_index]
        current_value = getattr(before_cpu.regs, field_name)
        if current_value is None:
            return (R16[register_index], None)
        new_value = (current_value & 0xFFFF0000) | (value & 0xFFFF)
        return (R16[register_index], {field_name: new_value & 0xFFFFFFFF})

    field_name = REG32_FIELDS[register_index // 2]
    current_value = getattr(before_cpu.regs, field_name)
    if current_value is None:
        return (R8[register_index], None)

    if register_index % 2 == 0:
        new_value = (current_value & 0xFFFF00FF) | ((value & 0xFF) << 8)
    else:
        new_value = (current_value & 0xFFFFFF00) | (value & 0xFF)
    return (R8[register_index], {field_name: new_value & 0xFFFFFFFF})


def _current_register_bank_index(
    cpu: NgpcCpuState,
    *,
    fallback_zero: bool = False,
) -> int | None:
    if cpu.rfp is not None:
        return cpu.rfp & 0b11
    if cpu.register_bank is not None:
        return cpu.register_bank & 0b11
    if fallback_zero:
        return 0
    return None


def _pack_banked_slots_from_value(value: int | None) -> tuple[int | None, ...]:
    if value is None:
        return (None, None, None, None)
    return tuple((value >> (8 * pos)) & 0xFF for pos in range(4))


def _banked_owner_value_from_slots(slots: tuple[int | None, ...]) -> int | None:
    if any(slot is None for slot in slots):
        return None
    assert len(slots) == 4
    return (
        int(slots[0])
        | (int(slots[1]) << 8)
        | (int(slots[2]) << 16)
        | (int(slots[3]) << 24)
    ) & 0xFFFFFFFF


def _ensure_register_banks(cpu: NgpcCpuState) -> tuple[BankedByteRegisters, ...]:
    if cpu.register_banks is not None:
        return cpu.register_banks
    banks = [[None] * 16 for _ in range(4)]
    current_bank = _current_register_bank_index(cpu, fallback_zero=True)
    assert current_bank is not None
    for r32_index, field_name in enumerate(_BANKED_CORE_FIELDS):
        slots = _pack_banked_slots_from_value(getattr(cpu.regs, field_name))
        start = r32_index * 4
        for byte_pos, value in enumerate(slots):
            banks[current_bank][start + byte_pos] = value
    return tuple(BankedByteRegisters(slots=tuple(bank)) for bank in banks)


def _replace_register_bank_slot(
    banks: tuple[BankedByteRegisters, ...],
    bank_index: int,
    slot_index: int,
    value: int | None,
) -> tuple[BankedByteRegisters, ...]:
    bank_slots = list(banks[bank_index].slots)
    bank_slots[slot_index] = None if value is None else (value & 0xFF)
    updated_bank = BankedByteRegisters(slots=tuple(bank_slots))
    updated_banks = list(banks)
    updated_banks[bank_index] = updated_bank
    return tuple(updated_banks)


def _sync_core_reg_updates_into_banks(
    before_cpu: NgpcCpuState,
    reg_updates: dict[str, int] | None,
) -> tuple[BankedByteRegisters, ...] | None:
    if reg_updates is None:
        return before_cpu.register_banks
    touched = [field for field in _BANKED_CORE_FIELDS if field in reg_updates]
    if not touched:
        return before_cpu.register_banks
    current_bank = _current_register_bank_index(before_cpu, fallback_zero=True)
    assert current_bank is not None
    banks = _ensure_register_banks(before_cpu)
    for field_name in touched:
        slots = _pack_banked_slots_from_value(reg_updates[field_name])
        start = _BANKED_CORE_FIELDS.index(field_name) * 4
        bank_slots = list(banks[current_bank].slots)
        for byte_pos, value in enumerate(slots):
            bank_slots[start + byte_pos] = value
        updated_bank = BankedByteRegisters(slots=tuple(bank_slots))
        updated_banks = list(banks)
        updated_banks[current_bank] = updated_bank
        banks = tuple(updated_banks)
    return banks


def _extract_banked_core_byte(
    before_cpu: NgpcCpuState,
    bank_index: int,
    r32_index: int,
    byte_pos: int,
) -> int | None:
    banks = _ensure_register_banks(before_cpu)
    slot_index = (r32_index * 4) + byte_pos
    return banks[bank_index].slots[slot_index]


def _build_banked_core_byte_update(
    before_cpu: NgpcCpuState,
    bank_index: int,
    r32_index: int,
    byte_pos: int,
    value: int,
) -> tuple[dict[str, int] | None, tuple[BankedByteRegisters, ...] | None]:
    banks = _ensure_register_banks(before_cpu)
    slot_index = (r32_index * 4) + byte_pos
    new_banks = _replace_register_bank_slot(banks, bank_index, slot_index, value)
    reg_updates = None
    current_bank = _current_register_bank_index(before_cpu, fallback_zero=True)
    if current_bank == bank_index:
        field_name = _BANKED_CORE_FIELDS[r32_index]
        slots = new_banks[bank_index].slots[r32_index * 4 : (r32_index * 4) + 4]
        owner_value = _banked_owner_value_from_slots(slots)
        if owner_value is None:
            return None, None
        reg_updates = {field_name: owner_value}
    return reg_updates, new_banks


def _load_visible_core_regs_from_bank(
    regs,
    banks: tuple[BankedByteRegisters, ...],
    bank_index: int,
):
    updates: dict[str, int | None] = {}
    for r32_index, field_name in enumerate(_BANKED_CORE_FIELDS):
        slots = banks[bank_index].slots[r32_index * 4 : (r32_index * 4) + 4]
        updates[field_name] = _banked_owner_value_from_slots(slots)
    return replace(regs, **updates)


def _switch_visible_core_bank(
    before_cpu: NgpcCpuState,
    new_bank: int,
) -> tuple[int, tuple[BankedByteRegisters, ...], GeneralRegisters32]:
    """Flush the current visible XWA/XBC/XDE/XHL bank then load another.

    TLCS-900/H bank switching only affects the core XWA..XHL register file
    window. This helper persists the currently visible core registers into the
    outgoing bank's backing store, then reloads the visible XWA/XBC/XDE/XHL
    fields from `new_bank`.
    """
    old_bank = _current_register_bank_index(before_cpu, fallback_zero=True)
    assert old_bank is not None
    banks = _ensure_register_banks(before_cpu)
    bank_slots = list(banks[old_bank].slots)
    for r32_index, field_name in enumerate(_BANKED_CORE_FIELDS):
        start = r32_index * 4
        slots = _pack_banked_slots_from_value(getattr(before_cpu.regs, field_name))
        for byte_pos, value in enumerate(slots):
            bank_slots[start + byte_pos] = value
    updated_banks = list(banks)
    updated_banks[old_bank] = BankedByteRegisters(slots=tuple(bank_slots))
    banks = tuple(updated_banks)
    new_regs = _load_visible_core_regs_from_bank(before_cpu.regs, banks, new_bank)
    return old_bank, banks, new_regs


def _resolve_c7_alt_bank_target(
    before_cpu: NgpcCpuState,
    reg_byte: int,
) -> tuple[int, int, int] | None:
    if 0x00 <= reg_byte <= 0x3F:
        bank_index = reg_byte // 16
        within = reg_byte % 16
        return bank_index, within // 4, within % 4
    if 0xD0 <= reg_byte <= 0xDF:
        current_bank = _current_register_bank_index(before_cpu, fallback_zero=True)
        assert current_bank is not None
        within = reg_byte - 0xD0
        return (current_bank - 1) & 0b11, within // 4, within % 4
    return None


def _indexed_r16_index_from_code(code: int) -> int | None:
    """The 16-bit INDEX register of `(r32 + r16)`, named by an extended code.

    The byte path below already refuses a code it cannot name. The WORD path did
    not: it took `(code >> 2) & 7` outright, which is only right for a CURRENT-BANK
    code (0xE0..0xFF, byte position 0). A banked code silently indexed the wrong
    register -- the same mistake `secondary_base_r32()` documents for the BASE.

    Returns None when the code names something this core cannot resolve, and the
    caller must stop rather than guess an index.
    """
    if code < 0xE0 or (code & 0x03) != 0:
        return None
    return (code >> 2) & 0x07


def _indexed_r8_index_from_code(code: int) -> int | None:
    """Map a register-index-mode BYTE index code to an `R8` table index.

    The indexed modes `(r32 + r8)` / `(r32 + r16)` name their index register with
    the FULL extended register code, not with the 3-bit register number used by
    the register-direct family. Reading it as a 3-bit number silently picks the
    wrong register: `(XIX+A)` and `(XIX+W)` both resolve to W, and `(XIX+C)`
    resolves to A. That is what this core did, and a whole-ROM trace of Bakumatsu
    Rouman caught it.

    The official Toshiba assembler settles the map:

        ld (XIX+A),A  ->  F3 03 F0 E0 41
        ld (XIX+W),A  ->  F3 03 F0 E1 41
        ld (XIX+C),A  ->  F3 03 F0 E4 41
        ld (XIX+B),A  ->  F3 03 F0 E5 41

    so `xreg = (code >> 2) & 7` and `byte = code & 3`, where byte 0 is the LOW
    half of the word and byte 1 the HIGH half. In `R8` order ("W","A","B","C",
    "D","E","H","L") the low half of XWA is A (index 1) and the high half is W
    (index 0) -- hence the `+ 1` / `+ 0` below.

    Returns None for anything this table cannot name (a bank escape, an IXL-style
    half of XIX..XSP, a Q-half). The caller must then stop honestly rather than
    guess a register.
    """
    if code < 0xE0:                      # bank escape -- not a current-bank code
        return None
    xreg = (code >> 2) & 0x07
    pos = code & 0x03
    if xreg >= 4 or pos >= 2:            # XIX..XSP have no R8 name; pos 2/3 = Q-half
        return None
    return xreg * 2 + (1 if pos == 0 else 0)


def _indexed_signed_byte(value: int) -> int:
    """Sign-extend a register-index BYTE displacement.

    OPEN HARDWARE QUESTION. Toshiba calls the index register "the register
    specified as the 8- or 16-bit **displacement**" (CPU manual, "Register Index
    Addressing Mode"), and every displacement the manual gives a range for is
    signed (`(r + d16)`: "-8000H to +7FFFH"). So signed is the documented
    reading. But the manual gives no negative example for the REGISTER index and
    states no range for it, and neither the assembler nor any disassembler can
    reveal a run-time semantic. This is flagged in specs/TLCS900_MEMORY_FAMILY.md
    and a hardware test ROM is the arbiter, exactly as for D0..D7 and D8..DF.
    """
    return value - 0x100 if value >= 0x80 else value


def _indexed_signed_word(value: int) -> int:
    """Sign-extend a register-index WORD displacement. Same open question."""
    return value - 0x10000 if value >= 0x8000 else value


def _resolve_index_displacement(
    before_cpu: NgpcCpuState,
    secondary: int,
    code: int,
) -> tuple[str, int | None, str | None]:
    """Resolve the index register of `(r32 + r8)` / `(r32 + r16)`.

    Returns `(name, displacement, refusal)`. `refusal` is non-None when the code
    names a register this core cannot resolve, and the caller must stop rather
    than guess.

    TWO bugs lived here, both found by a whole-ROM trace of Bakumatsu Rouman:

    1. **The size was never read.** The secondary byte's bit 2 picks the index
       size -- 0x03 = an 8-bit register, 0x07 = a 16-bit one -- but the guards
       only tested `secondary & 0x03`, which is true for BOTH. So `ld (XIX+W),A`
       was executed as `ld (XIX+WA),A`: it took the whole 16-bit WA as the index
       instead of the single byte W, and stored the byte 0xFF bytes away from
       where it belonged.

    2. **The register code was read as a 3-bit number.** It is the full extended
       register code. The official Toshiba assembler settles the map:
           ld (XIX+A),A -> F3 03 F0 E0 41       ld (XIX+C),A -> F3 03 F0 E4 41
           ld (XIX+W),A -> F3 03 F0 E1 41       ld (XIX+B),A -> F3 03 F0 E5 41
       so xreg = (code >> 2) & 7 and byte = code & 3 (0 = low half, 1 = high).
    """
    if secondary & 0x04:
        index_index = (code >> 2) & 0x07
        if code < 0xE0 or (code & 0x03) != 0:
            return (
                f"code 0x{code:02X}",
                None,
                f"Register-index WORD code 0x{code:02X} is not a current-bank r16.",
            )
        name = R16[index_index]
        value = getattr(before_cpu.regs, REG32_FIELDS[index_index])
        if value is None:
            return name, None, None
        return name, _indexed_signed_word(value & 0xFFFF), None

    index_index = _indexed_r8_index_from_code(code)
    if index_index is None:
        return (
            f"code 0x{code:02X}",
            None,
            f"Register-index BYTE code 0x{code:02X} names a register this core cannot "
            "resolve (a bank escape, a Q-half, or a half of XIX..XSP).",
        )
    name, value = _extract_register_value(
        before_cpu=before_cpu, size_kind="byte", register_index=index_index
    )
    if value is None:
        value = _extract_current_banked_r8_value(before_cpu, index_index)
    if value is None:
        return name, None, None
    return name, _indexed_signed_byte(value & 0xFF), None


def _extract_current_banked_r8_value(
    before_cpu: NgpcCpuState,
    register_index: int,
) -> int | None:
    owner_index = register_index // 2
    if owner_index >= 4:
        return None
    current_bank = _current_register_bank_index(before_cpu, fallback_zero=True)
    assert current_bank is not None
    slot_index = owner_index * 4 + (1 if register_index % 2 == 0 else 0)
    return _ensure_register_banks(before_cpu)[current_bank].slots[slot_index]


def _extract_current_banked_r16_value(
    before_cpu: NgpcCpuState,
    register_index: int,
) -> int | None:
    if register_index >= 4:
        return None
    current_bank = _current_register_bank_index(before_cpu, fallback_zero=True)
    assert current_bank is not None
    slot_base = register_index * 4
    slots = _ensure_register_banks(before_cpu)[current_bank].slots[slot_base : slot_base + 2]
    if any(slot is None for slot in slots):
        return None
    return (int(slots[0]) | (int(slots[1]) << 8)) & 0xFFFF


def _extract_current_banked_r32_value(
    before_cpu: NgpcCpuState,
    register_index: int,
) -> int | None:
    if register_index >= 4:
        return None
    current_bank = _current_register_bank_index(before_cpu, fallback_zero=True)
    assert current_bank is not None
    slot_base = register_index * 4
    slots = _ensure_register_banks(before_cpu)[current_bank].slots[slot_base : slot_base + 4]
    return _banked_owner_value_from_slots(slots)


def _build_current_banked_r8_update(
    before_cpu: NgpcCpuState,
    register_index: int,
    value: int,
) -> tuple[dict[str, int] | None, dict[str, object] | None]:
    owner_index = register_index // 2
    if owner_index >= 4:
        return None, None
    current_bank = _current_register_bank_index(before_cpu, fallback_zero=True)
    assert current_bank is not None
    slot_index = owner_index * 4 + (1 if register_index % 2 == 0 else 0)
    banks = _ensure_register_banks(before_cpu)
    new_banks = _replace_register_bank_slot(banks, current_bank, slot_index, value)
    slots = new_banks[current_bank].slots[owner_index * 4 : (owner_index * 4) + 4]
    owner_value = _banked_owner_value_from_slots(slots)
    if owner_value is not None:
        return {_BANKED_CORE_FIELDS[owner_index]: owner_value}, {"register_banks": new_banks}
    return None, {"register_banks": new_banks}


def _extract_byte_slice(
    before_cpu: NgpcCpuState,
    r32_index: int,
    byte_pos: int,
) -> int | None:
    """Read one byte (`byte_pos` 0..3) of a current-bank 32-bit register.

    Used by the C7 extended-register family, where the Q-prefixed names
    (QA, QC, QIZH, …) address the upper two bytes of XWA..XSP. Returns
    None when the owning register value is not modeled.
    """
    owner_value = getattr(before_cpu.regs, REG32_FIELDS[r32_index])
    if owner_value is None:
        return None
    return (owner_value >> (8 * byte_pos)) & 0xFF


def _build_byte_slice_update(
    before_cpu: NgpcCpuState,
    r32_index: int,
    byte_pos: int,
    value: int,
) -> dict[str, int] | None:
    """Build a reg-update writing `value` into byte `byte_pos` of an R32.

    Returns None when the owning register is unknown (we cannot preserve
    the other three bytes, so the write would be dishonest).
    """
    field_name = REG32_FIELDS[r32_index]
    owner_value = getattr(before_cpu.regs, field_name)
    if owner_value is None:
        return None
    shift = 8 * byte_pos
    cleared = owner_value & (~(0xFF << shift) & 0xFFFFFFFF)
    return {field_name: (cleared | ((value & 0xFF) << shift)) & 0xFFFFFFFF}


_STEP_READS: list[MemoryRead] = []


def _read_runtime_bytes(
    view: NgpcFetchView,
    memory_bytes: dict[int, int],
    address: int,
    size: int,
) -> bytes | None:
    """Read `size` bytes starting at `address` via the runtime overlay then bus.

    Successful reads are appended to the per-step accumulator
    `_STEP_READS`. `build_execute_next` clears the accumulator at the
    start of each step and folds the collected reads into
    `ExecutionResult.memory_reads` so watchpoint matching covers every
    executor automatically.

    Returns `None` if any byte in the range is unbacked; in that case,
    no entry is appended (a partial read does not surface as a complete
    `MemoryRead`).
    """
    data = bytearray()
    for offset in range(size):
        cur_addr = _mask_address(address + offset)
        if cur_addr in memory_bytes:
            data.append(memory_bytes[cur_addr])
            continue
        read = view.bus.read_bytes(cur_addr, size=1)
        if read.status != "ok" or read.data is None:
            # Truly-unmapped address = OPEN BUS. HW-measured on a real
            # NGPC (hw_test_openbus, 2026-07-08): a read of an address outside
            # every mapped region returns 0x00 and does NOT hang (the TLCS-900/H
            # has no bus-fault trap). Model that faithfully instead of honest-
            # stopping. Addresses that sit INSIDE a region but are merely
            # unbacked (an unmodeled peripheral / not-yet-loaded cart image) stay
            # None -> honest-stop, because that is modelable state we have not
            # modeled yet, not open bus.
            probe = view.bus.address_space.probe(cur_addr)
            if probe.region is None:
                data.append(0x00)
                continue
            return None
        data.extend(read.data)
    payload = bytes(data)
    _STEP_READS.append(
        MemoryRead(
            address=_mask_address(address),
            data=payload,
            note=(
                "Executor read this contiguous range via the writable runtime "
                "overlay or the read bus to perform the current step."
            ),
        )
    )
    return payload


def _check_writable_range(
    view: NgpcFetchView,
    address: int,
    size: int,
) -> tuple[str | None, str]:
    """Check whether a memory range is writable.

    Return values:
      (None, "")                   -- range is writable, proceed normally
      ("write-discarded", note)    -- unmapped or ROM address; real hardware silently
                                      discards the write (open bus / no WE signal).
                                      Callers that model memory stores MUST continue
                                      execution.  Callers that model stack stores MAY
                                      treat this as a stop condition.
    """
    for offset in range(size):
        cur_addr = _mask_address(address + offset)
        probe = view.bus.address_space.probe(cur_addr)
        if probe.region is None:
            return (
                "write-discarded",
                (
                    f"Write to unmapped address 0x{cur_addr:06X}: silently discarded on "
                    "real hardware (open bus — nothing responds to this address)."
                ),
            )
        if probe.region.kind in READ_ONLY_REGION_KINDS:
            return (
                "write-discarded",
                (
                    f"Write to read-only region '{probe.region.name}' at 0x{cur_addr:06X}: "
                    "silently discarded on real hardware (no write-enable for this region)."
                ),
            )
    return None, ""


def _prefixed_register_execute_info(first_opcode: int) -> tuple[str, int] | None:
    # 0xD0..0xD7 IS NOT A REGISTER PREFIX. It is the word MEMORY-addressing family
    # (mirror of the byte C0..C7), settled ON HARDWARE 2026-07-03 -- the comment
    # three lines below has said so ever since, but the D0..D7 row stayed in this
    # table. The consequence was not a stray flag or a cycle count: EVERY
    # `_try_execute_prefixed_*` handler claimed a D0..D7 MEMORY instruction and
    # executed it as a REGISTER operation. `d5 ed 21` decodes correctly as
    # `ld BC,(XHL+)` -- and was then executed as `sra IY`.
    #
    # The decoder was right and the executor was wrong, so nothing caught it, until
    # gate G3 (whole-ROM trace equivalence) ran Pac-Man against the native core on
    # 2026-07-12 and the two machines parted ways at instruction 113.
    #
    # The register-direct prefixes are ONLY C8..CF (byte), D8..DF (word) and
    # E8..EF (long). Anything else here is a mis-claim.
    if 0xC8 <= first_opcode <= 0xCF:
        return ("byte", first_opcode & 0x07)
    if 0xD8 <= first_opcode <= 0xDF:
        # WORD, not long — matches decode._prefixed_register_info. Ground truth
        # ngdis getzz(0xD8)=1=word; HW-confirmed 2026-07-03 (hw_test_off ROM:
        # D8 89 -> AAAA3344 word copy, D9 1C -> 0002FFFF word djnz). The genuine
        # long prefix is 0xE8..0xEF (getzz=2).
        return ("word", first_opcode & 0x07)
    if 0xE8 <= first_opcode <= 0xEF:
        return ("long", first_opcode & 0x07)
    return None


def _control_register_descriptor(control_code: int) -> tuple[str, str, int | None] | None:
    if control_code in (0x00, 0x04, 0x08, 0x0C):
        return ("dmas", "long", control_code // 4)
    if control_code in (0x10, 0x14, 0x18, 0x1C):
        return ("dmad", "long", (control_code - 0x10) // 4)
    if control_code in (0x20, 0x24, 0x28, 0x2C):
        return ("dmac", "word", (control_code - 0x20) // 4)
    if control_code in (0x22, 0x26, 0x2A, 0x2E):
        return ("dmam", "byte", (control_code - 0x22) // 4)
    if control_code == 0x30:
        return ("intnest", "word", None)
    return None


def _ensure_cpu_control_registers(cpu: NgpcCpuState) -> Tlcs900ControlRegisters:
    if cpu.control_registers is not None:
        return cpu.control_registers
    return create_unknown_control_registers()


def _read_control_register_value(
    cpu: NgpcCpuState,
    control_code: int,
) -> tuple[str, str, int | None] | None:
    descriptor = _control_register_descriptor(control_code)
    if descriptor is None:
        return None
    field_name, size_kind, index = descriptor
    control = _ensure_cpu_control_registers(cpu)
    if field_name == "intnest":
        return control_register_name(control_code), size_kind, control.intnest
    values = getattr(control, field_name)
    assert index is not None
    return control_register_name(control_code), size_kind, values[index]


def _write_control_register_value(
    before_cpu: NgpcCpuState,
    control_code: int,
    value: int,
) -> tuple[str, dict[str, object]] | None:
    descriptor = _control_register_descriptor(control_code)
    if descriptor is None:
        return None
    field_name, size_kind, index = descriptor
    control = _ensure_cpu_control_registers(before_cpu)
    masked_value = value & {"byte": 0xFF, "word": 0xFFFF, "long": 0xFFFFFFFF}[size_kind]

    if field_name == "intnest":
        new_control = replace(control, intnest=masked_value)
    else:
        assert index is not None
        values = list(getattr(control, field_name))
        values[index] = masked_value
        new_control = replace(control, **{field_name: tuple(values)})
    return control_register_name(control_code), {"control_registers": new_control}


def _adjust_intnest(
    cpu: NgpcCpuState,
    delta: int,
) -> Tlcs900ControlRegisters | None:
    control = cpu.control_registers
    if control is None or control.intnest is None:
        return control
    return replace(control, intnest=(control.intnest + delta) & 0xFFFF)


def seed_cpu_state_for_execution(
    cpu: NgpcCpuState,
    register_values: dict[str, int] | None = None,
    seed_xsp: int | None = None,
) -> NgpcCpuState:
    seed_map: dict[str, int] = {}
    bank_seed_map: dict[tuple[int, str], int] = {}
    control_seed_map: dict[int, int] = {}
    if register_values is not None:
        for register_name, value in register_values.items():
            normalized_name = register_name.upper()
            if "@BANK" in normalized_name:
                base_name, _, bank_suffix = normalized_name.partition("@BANK")
                field_name = SEEDED_BANKED_REGISTERS.get(base_name)
                if field_name is None or not bank_suffix.isdigit():
                    raise ValueError(
                        "banked seed register name must use XWA@bank0..3, "
                        "XBC@bank0..3, XDE@bank0..3 or XHL@bank0..3"
                    )
                bank_index = int(bank_suffix)
                if bank_index > 3:
                    raise ValueError("banked seed register bank must be 0..3")
                bank_seed_map[(bank_index, field_name)] = value & 0xFFFFFFFF
                continue
            field_name = SEEDABLE_REGISTERS.get(normalized_name)
            if field_name is not None:
                seed_map[field_name] = value & 0xFFFFFFFF
                continue
            control_code = SEEDABLE_CONTROL_REGISTERS.get(normalized_name)
            if control_code is None:
                raise ValueError(
                    "seed register name must be one of: "
                    + ", ".join((*SEEDABLE_REGISTERS, *SEEDABLE_CONTROL_REGISTERS))
                )
            control_seed_map[control_code] = value & 0xFFFFFFFF

    if seed_xsp is not None:
        seed_xsp_value = seed_xsp & 0xFFFFFFFF
        current_xsp = seed_map.get("xsp")
        if current_xsp is not None and current_xsp != seed_xsp_value:
            raise ValueError("conflicting seed values were provided for XSP")
        seed_map["xsp"] = seed_xsp_value

    if not seed_map and not bank_seed_map and not control_seed_map:
        return cpu

    modeled_fields = cpu.modeled_fields
    if "user-seeded-registers" not in modeled_fields:
        modeled_fields = (*modeled_fields, "user-seeded-registers")

    note_parts = []
    for register_name, field_name in SEEDABLE_REGISTERS.items():
        if field_name in seed_map:
            note_parts.append(f"{register_name}=0x{seed_map[field_name]:08X}")
    if bank_seed_map:
        for bank_index, field_name in sorted(bank_seed_map):
            note_parts.append(
                f"{field_name.upper()}@bank{bank_index}=0x{bank_seed_map[(bank_index, field_name)]:08X}"
            )
    if control_seed_map:
        for control_code in sorted(control_seed_map):
            note_parts.append(
                f"{control_register_name(control_code)}=0x{control_seed_map[control_code]:08X}"
            )

    register_banks = cpu.register_banks
    if bank_seed_map:
        current_bank = _current_register_bank_index(cpu, fallback_zero=False)
        banks = _ensure_register_banks(cpu)
        for (bank_index, field_name), value in bank_seed_map.items():
            owner_index = _BANKED_CORE_FIELDS.index(field_name)
            bank_slots = list(banks[bank_index].slots)
            slot_base = owner_index * 4
            for byte_pos, byte_value in enumerate(_pack_banked_slots_from_value(value)):
                bank_slots[slot_base + byte_pos] = byte_value
            updated_banks = list(banks)
            updated_banks[bank_index] = BankedByteRegisters(slots=tuple(bank_slots))
            banks = tuple(updated_banks)
            if current_bank == bank_index:
                current_value = seed_map.get(field_name)
                if current_value is not None and current_value != value:
                    raise ValueError(
                        f"conflicting seed values were provided for {field_name.upper()} and "
                        f"{field_name.upper()}@bank{bank_index}"
                    )
                seed_map[field_name] = value
        register_banks = banks

    control_registers = cpu.control_registers
    if control_seed_map:
        seeded_control = _ensure_cpu_control_registers(cpu)
        for control_code, value in sorted(control_seed_map.items()):
            write_result = _write_control_register_value(
                replace(cpu, control_registers=seeded_control),
                control_code,
                value,
            )
            assert write_result is not None
            seeded_control = write_result[1]["control_registers"]  # type: ignore[assignment]
        control_registers = seeded_control

    return replace(
        cpu,
        regs=replace(cpu.regs, **seed_map),
        modeled_fields=modeled_fields,
        register_banks=register_banks,
        control_registers=control_registers,
        note=(
            f"{cpu.note} A user-supplied execution seed currently sets "
            + ", ".join(note_parts)
            + " for this command invocation."
        ),
    )


def _addressing_mode_cycle_extra(raw: bytes | None) -> int:
    """The cycles an ADDRESSING MODE adds on top of the instruction's own cost.

    THE MISSING HALF OF THIS CORE'S CYCLE MODEL. Toshiba bills a memory-operand
    instruction as

        cycles = base(instruction) + extra(addressing mode)

    and gives the second term its own table -- "900/L1 Instruction Lists (10),
    Addressing mode":

        (R) +0 · (R+d8) +1 · (#8) +1 · (#16) +2 · (#24) +3
        (r) +1 · (r+d16) +3 · (r+r8) +3 · (r+r16) +3 · (-r) +1 · (r+) +1

    This core applied NO adder at all, so every memory-operand cycle count it
    reported was too low. The defect was already measured -- the differential gate
    reports it as its own verdict (`cycles-adder`), having checked the identity
    `native == python + extra(mode)` case by case -- and it is what stopped
    whole-ROM trace equivalence dead: once an interrupt or a peripheral is
    CYCLE-SCHEDULED, an under-billed instruction moves the event, and the two cores
    take the interrupt at different instructions while executing identical code.

    Applied HERE, in the one place every executed instruction passes through, so no
    handler can forget it. Only the memory families carry a mode (first byte >=
    0x80 with a `mem` field below 23); the register-direct forms and everything
    below 0x80 add nothing.
    """
    if not raw:
        return 0
    first = raw[0]
    if first < 0x80:
        return 0
    mem = ((first & 0x40) >> 2) | (first & 0x0F)
    if mem >= 22:                      # 23+ = register-direct; no addressing mode
        return 0
    if mem <= 7:                       # (R)
        return 0
    if mem <= 15:                      # (R + d8)
        return 1
    if mem == 16:                      # (#8)
        return 1
    if mem == 17:                      # (#16)
        return 2
    if mem == 18:                      # (#24)
        return 3
    if mem == 19:                      # the secondary-byte modes
        if len(raw) < 2:
            return 0
        data = raw[1]
        if data in (0x03, 0x07):       # (r + r8) / (r + r16)
            return 3
        if data == 0x13:               # pc-relative -- this is how LDAR works
            return 3
        if (data & 0x03) == 0x01:      # (r + d16)
            return 3
        return 1                       # (r)
    return 1                           # 20 = (-r), 21 = (r+)


def _destination_group_subop(raw: bytes | None) -> int | None:
    """The sub-opcode of a DESTINATION-group instruction, or None.

    The destination group is `zz == 3` (first byte `0xB0..0xBF` / `0xF0..0xF7`).
    The sub-opcode sits after the addressing mode's own operand bytes, so its
    offset depends on the mode -- which is why it cannot simply be `raw[1]`.
    """
    if not raw:
        return None
    first = raw[0]
    if first < 0x80 or (first & 0x30) != 0x30:
        return None
    mem = ((first & 0x40) >> 2) | (first & 0x0F)
    if mem <= 7:
        offset = 1                                  # (R)
    elif mem <= 16:
        offset = 2                                  # (R+d8), (#8)
    elif mem == 17:
        offset = 3                                  # (#16)
    elif mem == 18:
        offset = 4                                  # (#24)
    elif mem == 19:
        if len(raw) < 2:
            return None
        data = raw[1]
        offset = 2 if (data & 0x03) == 0 and data not in (0x03, 0x07, 0x13) else 4
    elif mem <= 21:
        offset = 2                                  # (-r), (r+)
    else:
        return None                                 # register-direct
    return raw[offset] if len(raw) > offset else None


# Toshiba instruction list (9), "Jump, Call and Return". Note 1 of that table:
# "(T/F) represents the number of states at true / false."
#
#     JP  [cc,]mem   B0 + mem : D0 + cc     7/4  (T/F)   + M
#     CALL[cc,]mem   B0 + mem : E0 + cc    12/4  (T/F)   + M
#     RET  cc        B0      : F0 + cc     12/4  (T/F)
#
# BOTH cores were wrong here, and in different ways. This one billed a FLAT cost
# with no taken/not-taken distinction at all (`JP_MEM_CYCLES = 7` whether the
# branch was taken or not, `CALL_MEM_CYCLES = 12` likewise -- so an untaken call
# cost three times what silicon charges). The native core had the distinction but
# the wrong numbers (9/6 and 12/6). The differential gate found it once the
# addressing-mode adder stopped masking the difference.
_BRANCH_MEM_CYCLES = {
    0xD0: (7, 4),    # JP cc, mem
    0xE0: (12, 4),   # CALL cc, mem
    0xF0: (12, 4),   # RET cc
}


def _billed_cycles(
    decoded: DecodeResult,
    explicit: int | None,
    new_pc: int | None = None,
) -> int:
    """What an executed instruction actually costs: base + addressing-mode adder.

    The adder is skipped when the resolver has NO entry for the instruction and
    falls back to the flat placeholder -- see `_resolved_cycles_from_decoded`.
    Stacking an adder on top of an admission of ignorance would produce a number
    that is wrong in a new way; the differential gate keeps reporting the honest
    verdict (`ref-placeholder-cycles`) instead.

    The conditional memory-operand branches are settled HERE rather than at each
    handler, because here we know whether the branch was TAKEN: `new_pc` differs
    from the sequential PC exactly when it was. Handlers used to pass a flat cost
    and several forgot the not-taken case entirely.
    """
    raw = decoded.raw_bytes
    extra = _addressing_mode_cycle_extra(raw)

    sub = _destination_group_subop(raw)
    if sub is not None and new_pc is not None and decoded.next_sequential_pc is not None:
        entry = _BRANCH_MEM_CYCLES.get(sub & 0xF0)
        if entry is not None:
            taken_cost, untaken_cost = entry
            taken = new_pc != decoded.next_sequential_pc
            return (taken_cost if taken else untaken_cost) + extra

    if explicit is not None:
        return explicit + extra
    resolved = _resolved_cycles_from_decoded(decoded)
    if resolved is None:
        return ESTIMATED_CYCLES_PER_INSTRUCTION
    return resolved + extra


def _executed_result(
    before_cpu: NgpcCpuState,
    decoded: DecodeResult,
    written_registers: tuple[str, ...],
    memory_writes: tuple[MemoryWrite, ...],
    after_memory: dict[int, int],
    new_pc: int | None,
    reg_updates: dict[str, int] | None,
    note: str,
    flags_updates: dict[str, bool | None] | None = None,
    memory_reads: tuple[MemoryRead, ...] = (),
    extra_cpu_updates: dict[str, object] | None = None,
    cycles_consumed: int | None = None,
) -> ExecutionResult:
    if new_pc is None:
        return _blocked_result(
            before_cpu=before_cpu,
            decoded=decoded,
            status="unsupported-decoded-instruction",
            note=(
                "The instruction decoded successfully, but no next PC is available for honest "
                "execution in the current subset."
            ),
            memory_reads=memory_reads,
        )

    regs = before_cpu.regs
    if reg_updates is not None:
        regs = replace(regs, **reg_updates)

    flags = before_cpu.flags
    if flags_updates is not None:
        flags = replace(flags, **flags_updates)

    modeled_fields = before_cpu.modeled_fields
    if "executed-subset" not in modeled_fields:
        modeled_fields = (*modeled_fields, "executed-subset")
    if flags_updates is not None and "modeled-flags-subset" not in modeled_fields:
        modeled_fields = (*modeled_fields, "modeled-flags-subset")

    cpu_updates = {} if extra_cpu_updates is None else dict(extra_cpu_updates)
    if "register_banks" not in cpu_updates:
        synced_banks = _sync_core_reg_updates_into_banks(before_cpu, reg_updates)
        if synced_banks is not None:
            cpu_updates["register_banks"] = synced_banks
    if "register_bank" not in cpu_updates:
        current_bank = _current_register_bank_index(before_cpu, fallback_zero=False)
        if current_bank is not None:
            cpu_updates["register_bank"] = current_bank

    after_cpu = replace(
        before_cpu,
        pc=new_pc,
        regs=regs,
        flags=flags,
        modeled_fields=modeled_fields,
        note=(
            "This CPU state includes effects from the current minimal real execution subset. "
            "Only instructions whose state changes are representable and implemented are "
            f"applied. {note}"
        ),
        **cpu_updates,
    )
    return ExecutionResult(
        before_cpu=before_cpu,
        after_cpu=after_cpu,
        decode=decoded,
        status="executed",
        written_registers=written_registers,
        memory_writes=memory_writes,
        after_memory=after_memory,
        memory_reads=memory_reads,
        note=note,
        cycles_consumed=_billed_cycles(decoded, cycles_consumed, new_pc),
    )


def _halted_result(
    before_cpu: NgpcCpuState,
    decoded: DecodeResult,
    after_memory: dict[int, int],
    note: str,
) -> ExecutionResult:
    result = _executed_result(
        before_cpu=before_cpu,
        decoded=decoded,
        written_registers=("PC",),
        memory_writes=(),
        after_memory=after_memory,
        new_pc=decoded.next_sequential_pc,
        reg_updates=None,
        note=note,
        cycles_consumed=HALT_CYCLES,
    )
    assert result.after_cpu is not None
    return replace(
        result,
        status="cpu-halted",
        after_memory=after_memory,
    )


def _blocked_result(
    before_cpu: NgpcCpuState,
    decoded: DecodeResult,
    status: str,
    note: str,
    matched_quirk: KnownQuirkMatch | None = None,
    memory_reads: tuple[MemoryRead, ...] = (),
) -> ExecutionResult:
    return ExecutionResult(
        before_cpu=before_cpu,
        after_cpu=None,
        decode=decoded,
        status=status,
        written_registers=(),
        memory_writes=(),
        after_memory=None,
        memory_reads=memory_reads,
        note=note,
        matched_quirk=matched_quirk,
    )


def _cycles_by_size(size_kind: str, *, byte: int, word: int, long: int) -> int:
    return {"byte": byte, "word": word, "long": long}[size_kind]


def _split_operand_tokens(operands: str) -> tuple[str, ...]:
    if not operands:
        return ()
    return tuple(part.strip() for part in operands.split(","))


def _is_memory_operand(token: str) -> bool:
    return token.startswith("(") and token.endswith(")")


def _register_token_size(token: str) -> str | None:
    token = token.strip()
    if token in R8 or token in C7_REGISTER_NAMES:
        return "byte"
    if token in R16:
        return "word"
    if token in R32:
        return "long"
    return None


def _immediate_token_size(token: str) -> str | None:
    token = token.strip()
    if token.startswith("0x"):
        digits = token[2:]
        if len(digits) <= 2:
            return "byte"
        if len(digits) <= 4:
            return "word"
        if len(digits) <= 8:
            return "long"
        return None
    if token.isdigit():
        value = int(token, 10)
        if value <= 0xFF:
            return "byte"
        if value <= 0xFFFF:
            return "word"
        if value <= 0xFFFFFFFF:
            return "long"
    return None


def _memory_operand_size_from_opcode(decoded: DecodeResult) -> str | None:
    raw = decoded.raw_bytes
    if raw is None or len(raw) == 0:
        return None
    prefixed_info = _prefixed_register_execute_info(raw[0])
    if prefixed_info is not None:
        return prefixed_info[0]

    first = raw[0]
    if 0x80 <= first <= 0x8F or first in (0xC1, 0xC2):
        return "byte"
    if 0x90 <= first <= 0x9F or first in (0xD1, 0xD2):
        return "word"
    if 0xA0 <= first <= 0xAF or first in (0xE1, 0xE2):
        return "long"
    return None


def _memory_family_cycles_from_decoded(decoded: DecodeResult) -> int | None:
    raw = decoded.raw_bytes
    if raw is None:
        return None
    operands = _split_operand_tokens(decoded.operands)
    if not operands:
        return None

    # LDA R, mem -- state 4 (+ the addressing mode's adder, applied centrally).
    # It was missing from this table entirely, so every `lda XIX,(XIX+32)` was
    # billed the flat 8-cycle placeholder. It is one of the most frequent
    # instructions in a real boot: 18 of the first 1500 in Bakumatsu Rouman alone.
    if decoded.mnemonic == "lda":
        return LDA_CYCLES

    if decoded.mnemonic in {"ld", "ldw"} and len(operands) == 2:
        left, right = operands
        if _is_memory_operand(left):
            src_size = _register_token_size(right)
            if src_size is not None:
                return _cycles_by_size(
                    src_size,
                    byte=MEM_STORE_BYTE_CYCLES,
                    word=MEM_STORE_WORD_CYCLES,
                    long=MEM_STORE_LONG_CYCLES,
                )
            if decoded.mnemonic == "ldw":
                return MEM_STORE_IMM16_CYCLES
            if right.startswith("0x") or right.isdigit():
                return MEM_STORE_IMM8_CYCLES
        if _is_memory_operand(right):
            dest_size = _register_token_size(left)
            if dest_size is not None:
                return _cycles_by_size(
                    dest_size,
                    byte=MEM_LOAD_BYTE_CYCLES,
                    word=MEM_LOAD_WORD_CYCLES,
                    long=MEM_LOAD_LONG_CYCLES,
                )

    if decoded.mnemonic in {"add", "adc", "sub", "sbc", "and", "xor", "or", "cp"} and len(operands) == 2:
        left, right = operands
        if _is_memory_operand(right):
            reg_size = _register_token_size(left)
            if reg_size is not None:
                return _cycles_by_size(
                    reg_size,
                    byte=MEM_LOAD_BYTE_CYCLES,
                    word=MEM_LOAD_WORD_CYCLES,
                    long=MEM_LOAD_LONG_CYCLES,
                )
        if _is_memory_operand(left):
            reg_size = _register_token_size(right)
            if reg_size is not None:
                if decoded.mnemonic == "cp":
                    return _cycles_by_size(
                        reg_size,
                        byte=MEM_LOAD_BYTE_CYCLES,
                        word=MEM_LOAD_WORD_CYCLES,
                        long=MEM_LOAD_LONG_CYCLES,
                    )
                return _cycles_by_size(
                    reg_size,
                    byte=ALU_MEM_DEST_BYTE_CYCLES,
                    word=ALU_MEM_DEST_WORD_CYCLES,
                    long=ALU_MEM_DEST_LONG_CYCLES,
                )
            imm_size = _immediate_token_size(right)
            if imm_size is not None and imm_size != "long":
                if decoded.mnemonic == "cp":
                    return _cycles_by_size(
                        imm_size,
                        byte=CP_MEM_IMM8_CYCLES,
                        word=CP_MEM_IMM16_CYCLES,
                        long=ESTIMATED_CYCLES_PER_INSTRUCTION,
                    )
                return _cycles_by_size(
                    imm_size,
                    byte=ALU_MEM_IMM8_CYCLES,
                    word=ALU_MEM_IMM16_CYCLES,
                    long=ESTIMATED_CYCLES_PER_INSTRUCTION,
                )

    if decoded.mnemonic in {"inc", "dec"} and len(operands) == 2 and _is_memory_operand(operands[1]):
        size_kind = _memory_operand_size_from_opcode(decoded)
        if size_kind in {"byte", "word"}:
            return _cycles_by_size(
                size_kind,
                byte=INCDEC_MEM_BYTE_CYCLES,
                word=INCDEC_MEM_WORD_CYCLES,
                long=ESTIMATED_CYCLES_PER_INSTRUCTION,
            )

    if decoded.mnemonic in {"ldcf", "andcf", "orcf", "xorcf"} and len(operands) == 2 and _is_memory_operand(operands[1]):
        return CF_MEM_READ_CYCLES

    if decoded.mnemonic == "bit" and len(operands) == 2 and _is_memory_operand(operands[1]):
        return BIT_MEM_READ_CYCLES

    if decoded.mnemonic in {"stcf", "res", "set", "chg", "tset"} and len(operands) == 2 and _is_memory_operand(operands[1]):
        return BIT_MEM_WRITE_CYCLES

    if decoded.mnemonic in {"rlc", "rrc", "rl", "rr", "sla", "sra", "sll", "srl"} and len(operands) == 1 and _is_memory_operand(operands[0]):
        size_kind = _memory_operand_size_from_opcode(decoded)
        if size_kind in {"byte", "word"}:
            return _cycles_by_size(
                size_kind,
                byte=ROTSHIFT_MEM_BYTE_CYCLES,
                word=ROTSHIFT_MEM_WORD_CYCLES,
                long=ESTIMATED_CYCLES_PER_INSTRUCTION,
            )

    if decoded.mnemonic == "pushw" and len(operands) == 1 and _is_memory_operand(operands[0]):
        return PUSH_MEM_WORD_CYCLES

    return None


def _shift_imm_register_cycles(count: int) -> int:
    return SHIFT_IMM_BASE_CYCLES + (count // 4)


def _executed_cycles_from_decoded(
    decoded: DecodeResult,
    *,
    branch_taken: bool | None = None,
) -> int:
    """Public resolver: never returns None, so every caller still gets a number."""
    resolved = _resolved_cycles_from_decoded(decoded, branch_taken=branch_taken)
    return ESTIMATED_CYCLES_PER_INSTRUCTION if resolved is None else resolved


def _resolved_cycles_from_decoded(
    decoded: DecodeResult,
    *,
    branch_taken: bool | None = None,
) -> int | None:
    """The same table, but it SAYS SO when it has no entry.

    The difference matters for the addressing-mode adder. `ESTIMATED_CYCLES_PER_
    INSTRUCTION` is not a cost -- it is the admission that this core does not know
    the instruction's cost. Adding a mode adder on top of an admission produces a
    number that is wrong in a NEW way, so the adder is skipped there and the
    differential gate keeps reporting the honest verdict (`ref-placeholder-cycles`)
    instead of a bogus mismatch.
    """
    raw = decoded.raw_bytes
    if raw is None or len(raw) == 0:
        return None

    first = raw[0]

    # THE C7 / D7 / E7 ESCAPES COST WHAT THEY WOULD COST WITHOUT THE ESCAPE.
    #
    # The escape byte changes only how the register is NAMED -- it lets the second
    # byte be a full 8-bit register code (IXL, QA, RW3, any bank) instead of a
    # 3-bit field. The operation, and therefore its cost, is unchanged. But the
    # table below reads the SUB-OPCODE at `raw[1]`, and under an escape the
    # register code has pushed the sub-opcode out to `raw[2]`. So nothing matched
    # and every escape form fell through to the flat 8-cycle placeholder.
    #
    # That mattered. `ldw RBC3,0` (`D7 34 A8`) costs 2 states and was billed 8.
    # Over a boot sequence the reference's clock therefore ran ahead of the native
    # core's, and the two took the VBlank interrupt at different instructions --
    # which is exactly what a whole-ROM trace of Fatal Fury reported.
    #
    # Strip the register code and resolve the equivalent non-escape form:
    # C7 -> C8, D7 -> D8, E7 -> E8.
    if first in (0xC7, 0xD7, 0xE7) and len(raw) >= 3:
        return _resolved_cycles_from_decoded(
            replace(decoded, raw_bytes=bytes([first + 1]) + raw[2:]),
            branch_taken=branch_taken,
        )

    # LD<W> (#8),#  --  `08 + z : #8 : #`.  States 5 (byte) / 6 (word).
    # The I/O poke: `08 1B 01` writes 1 to 0x00001B. It was billed 8.
    if first in (0x08, 0x0A) and len(raw) >= 3:
        return LD_ABS8_IMM_CYCLES[0 if first == 0x08 else 1]

    if raw == b"\x00":
        return NOP_CYCLES
    if raw == b"\x16" and decoded.mnemonic == "ex" and decoded.operands == "F,F'":
        return EX_FF_CYCLES
    if first == 0x06 and len(raw) == 2:
        return DI_CYCLES if raw[1] == 0x07 else EI_CYCLES
    if first == 0xF7 and len(raw) == 6 and decoded.mnemonic == "ldx":
        return LDX_CYCLES
    if first == 0x17 and len(raw) == 2:
        return LDF_CYCLES
    if raw in (b"\x0C", b"\x0D"):
        return INCF_DECF_CYCLES
    if 0x20 <= first <= 0x27 and len(raw) == 2 and decoded.mnemonic == "ld":
        return LD_IMM8_CYCLES
    if 0x30 <= first <= 0x37 and len(raw) == 3 and decoded.mnemonic == "ld":
        return LD_IMM16_CYCLES
    if 0x40 <= first <= 0x47 and len(raw) == 5 and decoded.mnemonic == "ld":
        return LD_IMM32_CYCLES
    if first == 0xF2 and len(raw) == 5 and decoded.mnemonic == "lda":
        return LDA_CYCLES
    if raw == b"\x02":
        return PUSH_SR_CYCLES
    if raw == b"\x03":
        return POP_SR_CYCLES
    if 0x60 <= first <= 0x6F and len(raw) == 2 and decoded.mnemonic == "jr":
        if branch_taken is None:
            return None
        return JR_CYCLES_TAKEN if branch_taken else JR_CYCLES_NOT_TAKEN
    if 0x70 <= first <= 0x7F and len(raw) == 3 and decoded.mnemonic == "jrl":
        if branch_taken is None:
            return None
        return JR_CYCLES_TAKEN if branch_taken else JR_CYCLES_NOT_TAKEN
    if prefixed_info := _prefixed_register_execute_info(first):
        if len(raw) >= 2 and raw[1] == 0x1C and decoded.mnemonic == "djnz":
            if branch_taken is None:
                return None
            return DJNZ_CYCLES_TAKEN if branch_taken else DJNZ_CYCLES_NOT_TAKEN
    if 0xD8 <= first <= 0xDF and len(raw) == 2 and raw[1] in (0x0E, 0x0F) and decoded.mnemonic in {"bs1f", "bs1b"}:
        return BS1_CYCLES
    if 0xD8 <= first <= 0xDF and len(raw) == 2 and raw[1] == 0x16 and decoded.mnemonic == "mirr":
        return MIRR_CYCLES
    if 0xD8 <= first <= 0xDF and len(raw) == 2 and raw[1] == 0x19 and decoded.mnemonic == "mula":
        return MULA_CYCLES
    if 0xD8 <= first <= 0xDF and len(raw) == 4 and raw[1] in (0x38, 0x39, 0x3A):
        return MINC_CYCLES
    if 0xD8 <= first <= 0xDF and len(raw) == 4 and raw[1] in (0x3C, 0x3D, 0x3E):
        return MDEC_CYCLES
    if first == 0x1A and len(raw) == 3:
        return JP16_CYCLES
    if first == 0x1B and len(raw) == 4:
        return JP24_CYCLES
    if first == 0x1C and len(raw) == 3:
        return CALL16_CYCLES
    if first == 0x1D and len(raw) == 4:
        return CALL24_CYCLES
    if first == 0x1E and len(raw) == 3:
        return CALR_CYCLES
    if raw == b"\x0E":
        return RET_CYCLES
    if first == 0x0F and len(raw) == 3:
        return RETD_CYCLES
    if raw == b"\x07":
        return RETI_CYCLES
    if 0xE8 <= first <= 0xEF and len(raw) == 4 and raw[1] == 0x0C:
        return LINK_CYCLES
    if 0xE8 <= first <= 0xEF and len(raw) == 2 and raw[1] == 0x0D:
        return UNLK_CYCLES

    prefixed_info = _prefixed_register_execute_info(first)
    if prefixed_info is not None and len(raw) >= 2:
        size_kind, _ = prefixed_info
        second = raw[1]
        # MUL / MULS / DIV / DIVS  rr,#  --  `C8 + zz + r : 08..0B : #`.
        # `D8 08 ..` (mul XWA,#) was billed 8; it costs 15.
        if second in (0x08, 0x09, 0x0A, 0x0B):
            table = {
                0x08: MUL_IMM_CYCLES,
                0x09: MULS_IMM_CYCLES,
                0x0A: DIV_IMM_CYCLES,
                0x0B: DIVS_IMM_CYCLES,
            }[second]
            if size_kind == "byte":
                return table[0]
            if size_kind == "word":
                return table[1]
            return None                     # no long form ("BW-")
        if second in (0x20, 0x21, 0x22, 0x23, 0x24, 0x28, 0x29, 0x2A, 0x2B, 0x2C):
            return CF_REG_CYCLES
        if decoded.mnemonic == "ld" and (0x88 <= second <= 0x8F or 0x98 <= second <= 0x9F):
            return LD_REG_REG_CYCLES
        if decoded.mnemonic == "ex" and 0xB8 <= second <= 0xBF:
            return EX_REG_REG_CYCLES
        if decoded.mnemonic == "ld" and 0xA8 <= second <= 0xAF:
            return LD_SMALL_IMM_CYCLES
        if decoded.mnemonic == "cp" and 0xD8 <= second <= 0xDF:
            return CP_IMM3_CYCLES
        if decoded.mnemonic in {"inc", "dec"} and (
            0x60 <= second <= 0x67 or 0x68 <= second <= 0x6F
        ):
            return INCDEC_REG_CYCLES
        if decoded.mnemonic == "push" and second == 0x04:
            return _cycles_by_size(
                size_kind,
                byte=PUSH_PREFIX_BYTE_CYCLES,
                word=PUSH_PREFIX_WORD_CYCLES,
                long=PUSH_PREFIX_LONG_CYCLES,
            )
        if decoded.mnemonic == "pop" and second == 0x05:
            return _cycles_by_size(
                size_kind,
                byte=POP_PREFIX_BYTE_CYCLES,
                word=POP_PREFIX_WORD_CYCLES,
                long=POP_PREFIX_LONG_CYCLES,
            )
        if decoded.mnemonic in {"cpl", "neg"} and second in (0x06, 0x07):
            return CPL_NEG_CYCLES
        if decoded.mnemonic == "daa" and second == 0x10:
            return DAA_CYCLES
        if decoded.mnemonic == "paa" and second == 0x14:
            return _cycles_by_size(
                size_kind,
                byte=ESTIMATED_CYCLES_PER_INSTRUCTION,
                word=PAA_CYCLES,
                long=PAA_CYCLES,
            )
        if size_kind in {"byte", "word"}:
            if decoded.mnemonic in {"bit", "res", "set", "chg"} and second in (0x30, 0x31, 0x32, 0x33):
                return REG_BIT_OP_CYCLES
            if decoded.mnemonic == "tset" and second == 0x34:
                return REG_TSET_OP_CYCLES
        if decoded.mnemonic in {"rlc", "rrc", "rl", "rr", "sla", "sra", "sll", "srl"} and 0xE8 <= second <= 0xEF and len(raw) >= 3:
            return _shift_imm_register_cycles((raw[2] & 0x0F) or 16)
        if decoded.mnemonic in {"rlc", "rrc", "rl", "rr", "sla", "sra", "sll", "srl"} and 0xF8 <= second <= 0xFF:
            return SHIFT_REG_A_CYCLES
        if decoded.mnemonic == "scc" and 0x70 <= second <= 0x7F:
            return _cycles_by_size(size_kind, byte=2, word=2, long=ESTIMATED_CYCLES_PER_INSTRUCTION)
        if decoded.mnemonic in {"extz", "exts"} and second in (0x12, 0x13):
            return EXT_CYCLES
        if decoded.mnemonic in {"add", "adc", "sub", "sbc", "and", "xor", "or", "cp"}:
            if second & 0xF8 in (0x80, 0x90, 0xA0, 0xB0, 0xC0, 0xD0, 0xE0, 0xF0):
                return ALU_REG_REG_CYCLES
            if second in (0xC8, 0xC9, 0xCA, 0xCB, 0xCC, 0xCD, 0xCE, 0xCF):
                return _cycles_by_size(
                    size_kind,
                    byte=ALU_IMM8_CYCLES,
                    word=ALU_IMM16_CYCLES,
                    long=ALU_IMM32_CYCLES,
                )
        if decoded.mnemonic == "ld" and second == 0x03:
            return _cycles_by_size(
                size_kind,
                byte=LD_IMM8_CYCLES,
                word=LD_IMM16_CYCLES,
                long=LD_IMM32_CYCLES,
            )

    if first == 0xC7 and len(raw) >= 3:
        op = raw[2]
        op_hi = op & 0xF8
        if op in (0x20, 0x21, 0x22, 0x23, 0x24, 0x28, 0x29, 0x2A, 0x2B, 0x2C):
            return CF_REG_CYCLES
        if decoded.mnemonic == "ld" and op_hi in (0x88, 0x98, 0xA8):
            return LD_REG_REG_CYCLES if op_hi != 0xA8 else LD_SMALL_IMM_CYCLES
        if decoded.mnemonic == "ld" and op == 0x03 and len(raw) >= 4:
            return LD_IMM8_CYCLES
        if decoded.mnemonic in {"inc", "dec"} and op_hi in (0x60, 0x68):
            return INCDEC_REG_CYCLES
        if decoded.mnemonic == "push" and op == 0x04:
            return PUSH_PREFIX_BYTE_CYCLES
        if decoded.mnemonic == "pop" and op == 0x05:
            return POP_PREFIX_BYTE_CYCLES
        if decoded.mnemonic == "cp" and op_hi == 0xD8:
            return CP_IMM3_CYCLES
        if decoded.mnemonic in {"cpl", "neg"} and op in (0x06, 0x07):
            return CPL_NEG_CYCLES
        if decoded.mnemonic == "daa" and op == 0x10:
            return DAA_CYCLES
        if decoded.mnemonic in {"rlc", "rrc", "rl", "rr", "sla", "sra", "sll", "srl"}:
            if 0xE8 <= op <= 0xEF and len(raw) >= 4:
                return _shift_imm_register_cycles((raw[3] & 0x0F) or 16)
            if 0xF8 <= op <= 0xFF:
                return SHIFT_REG_A_CYCLES
        if decoded.mnemonic in {"add", "adc", "sub", "sbc", "and", "xor", "or", "cp"}:
            if op_hi in (0x80, 0x90, 0xA0, 0xB0, 0xC0, 0xD0, 0xE0, 0xF0):
                return ALU_REG_REG_CYCLES
            if op in (0xC8, 0xC9, 0xCA, 0xCB, 0xCC, 0xCD, 0xCE, 0xCF) and len(raw) >= 4:
                return ALU_IMM8_CYCLES
    memory_cycles = _memory_family_cycles_from_decoded(decoded)
    if memory_cycles is not None:
        return memory_cycles

    # LAST RESORT, KEYED ON THE ENCODING RATHER THAN THE DISASSEMBLY.
    #
    # `_memory_family_cycles_from_decoded` reads the operand STRINGS, so it only
    # works where the disassembler names the instruction correctly. It does not for
    # the re-sized word family `D0..D7`: this core still prints `d0 92 f8` as
    # `adc DE, WA` -- a leftover of the old "D0 is a register prefix" reading,
    # retracted on hardware in 2026-07-03 (the executor was fixed, the decoder was
    # not). The helper sees no memory operand, gives up, and the instruction is
    # billed the flat placeholder.
    #
    # The ENCODING is not in doubt, so bill from it. Toshiba gives every memory
    # load, store, address-compute and ALU form the same base -- 4. 4. 6 -- and the
    # addressing-mode adder goes on top, centrally, in `_billed_cycles`.
    if first >= 0x80 and len(raw) >= 2:
        mem_mode = ((first & 0x40) >> 2) | (first & 0x0F)
        if mem_mode < 22:                                  # a real addressing mode
            dest_sub = _destination_group_subop(raw)
            if dest_sub is None:                           # source group
                sub = raw[1]
                is_long = ((first & 0x30) >> 4) == 2
                # In the SOURCE group 0x80..0xFF is the ALU matrix.
                known = 0x20 <= sub <= 0x27 or sub >= 0x80
            else:                                          # destination group
                sub = dest_sub
                is_long = 0x60 <= sub <= 0x67 or 0x30 <= sub <= 0x37
                # ...but in the DESTINATION group that range is the bit ops and the
                # conditional branches, which are costed above. Only the loads,
                # stores and address-computes fall here.
                known = 0x20 <= sub <= 0x37 or 0x40 <= sub <= 0x67
            if known:
                return 6 if is_long else 4

    return None


_XREG_NAMES = frozenset(R32)

# `ld Rd, Rs` register-to-register copies on the prefixed-register family.
# Sub-op 0x88..0x8F and 0x98..0x9F decode as `ld` (see decode register_ops).
# HW status: `mr_robot` (retail cartridge, confirmed booting on a real
# NGPC) executes `ld BC, WA` (D8 89) in its boot path, and hw_test_off (flashed
# 2026-07-03) proved D8 89 is a 16-bit WORD copy (result AAAA3344, high 16 of
# BC preserved) — the D8..DF prefix is WORD, not long. The genuine long copy is
# E8..EF (`ld XBC, XWA`). Both execute here via the shared size map.
_D8_DF_LD_COPY_SUBOPS = frozenset(range(0x88, 0x90)) | frozenset(range(0x98, 0xA0))
_R16_NAMES = frozenset(R16)


def _try_execute_d8_df_register_copy(
    before_cpu: NgpcCpuState,
    before_memory: dict[int, int],
    decoded: DecodeResult,
) -> ExecutionResult | None:
    """Execute a `ld Rd, Rs` register copy on the D8..DF (word) / E8..EF (long) prefix.

    D8..DF is the WORD prefix (HW-confirmed 2026-07-03: `ld BC, WA` = D8 89 copies
    only the low 16 bits, leaving the destination's high 16 unchanged). E8..EF is
    the genuine LONG prefix (`ld XBC, XWA`, full 32-bit copy). `PC` advances and no
    flags change (a plain `ld` register move). Returns None for any non-`ld`-copy
    form so the rest of the family (add/sub/mul/div r+r) keeps its honest handling.
    """
    raw = decoded.raw_bytes
    if raw is None or len(raw) != 2:
        return None
    if raw[1] not in _D8_DF_LD_COPY_SUBOPS or decoded.mnemonic != "ld":
        return None
    info = _prefixed_register_execute_info(raw[0])
    if info is None or info[0] not in ("word", "long"):
        return None
    size_kind = info[0]
    if decoded.operands is None:
        return None
    parts = [part.strip() for part in decoded.operands.split(",")]
    name_set = _R16_NAMES if size_kind == "word" else _XREG_NAMES
    name_table = R16 if size_kind == "word" else R32
    if len(parts) != 2 or parts[0] not in name_set or parts[1] not in name_set:
        return None
    dest_name, src_name = parts
    _, src_value = _extract_register_value(
        before_cpu=before_cpu,
        size_kind=size_kind,
        register_index=name_table.index(src_name),
    )
    if src_value is None:
        return _blocked_result(
            before_cpu=before_cpu,
            decoded=decoded,
            status="requires-known-full-register",
            note=(
                f"{src_name} must be known before `ld {dest_name}, {src_name}` can copy it "
                "honestly."
            ),
        )
    reg_name, reg_updates = _build_register_update(
        before_cpu,
        size_kind=size_kind,
        register_index=name_table.index(dest_name),
        value=src_value,
    )
    if reg_updates is None:
        # WORD copy needs the destination's full 32-bit owner known so its high
        # 16 bits can be preserved; stop honestly if it is not yet in state.
        return _blocked_result(
            before_cpu=before_cpu,
            decoded=decoded,
            status="requires-known-full-register",
            note=(
                f"{dest_name}'s owning 32-bit register must be known before `ld {dest_name}, "
                f"{src_name}` can preserve its upper bits on this word copy."
            ),
        )
    width = 4 if size_kind == "long" else 2
    return _executed_result(
        before_cpu=before_cpu,
        decoded=decoded,
        written_registers=(reg_name, "PC"),
        memory_writes=(),
        after_memory=before_memory,
        new_pc=decoded.next_sequential_pc,
        reg_updates=reg_updates,
        note=(
            f"Executed `ld {dest_name}, {src_name}` {size_kind} register copy "
            f"({dest_name} <- 0x{src_value:0{width * 2}X}). "
            + (
                "D8..DF word copy is HW-confirmed (retail mr_robot boot; hw_test_off "
                "D8 89 -> AAAA3344)."
                if size_kind == "word"
                else "E8..EF is the genuine long-register copy prefix."
            )
            + " See HARDWARE_COMPAT_POLICY.md."
        ),
    )


def _silicon_broken_register_copy_remediation(decoded: DecodeResult) -> str | None:
    """Return a toolchain remediation hint for a silicon-broken `ld Xd, Xs` copy.

    Applies to the 16-bit `0xD0..0xD7` register-to-register `ld` copies, which
    are still classified silicon-broken. NOTE (2026-07-02): the 32-bit
    `0xD8..0xDF` `ld` copies are now known executable (retail mr_robot boots on
    real NGPC using `ld XBC, XWA` = `D8 89`), so this 16-bit sibling is very
    likely also safe and only stays blocked pending its own direct evidence.
    The workaround the toolchain uses is `push <src>` + `pop <dst>` (2 bytes,
    identical ROM size). Diagnostic layer only — execution still stops.
    """
    raw = decoded.raw_bytes
    if raw is None or len(raw) != 2 or not (0xD0 <= raw[0] <= 0xD7):
        return None
    if decoded.mnemonic != "ld" or decoded.operands is None:
        return None
    parts = [part.strip() for part in decoded.operands.split(",")]
    if len(parts) != 2:
        return None
    dest, src = parts
    return (
        f" Toolchain remediation: this register-to-register copy `ld {dest}, {src}` is currently "
        f"classified silicon-broken, but it is the same `ld` copy family as the HW-confirmed "
        f"32-bit `D8..DF` copies (retail mr_robot) and is very likely safe; a `push {src}` + "
        f"`pop {dest}` workaround (same 2-byte ROM size) is available if a conservative rebuild "
        "is preferred pending a dedicated flashed test ROM."
    )


def _try_stop_known_silicon_broken(
    before_cpu: NgpcCpuState,
    decoded: DecodeResult,
) -> ExecutionResult | None:
    match = match_known_silicon_broken(decoded)
    if match is not None:
        note = match.note
        remediation = _silicon_broken_register_copy_remediation(decoded)
        if remediation is not None:
            note = f"{note}{remediation}"
        return _blocked_result(
            before_cpu=before_cpu,
            decoded=decoded,
            status="silicon-broken",
            note=note,
            matched_quirk=match,
        )

    return None


def _mask_address(address: int) -> int:
    return address & 0xFFFFFF


def _signed_u16(data: bytes) -> int:
    value = int.from_bytes(data, "little")
    return value - 0x10000 if value >= 0x8000 else value


def _signed_u8(value: int) -> int:
    return value - 0x100 if value >= 0x80 else value


def _post_increment_r32_index(encoded: int) -> int:
    # In the TLCS-900/H ARI_PI encoding, the register is carried in bits[4:2] of the
    # memory-form byte (each register occupies 4 slots in the full banked table).
    # Extracting bits[4:2] gives the correct 0..7 index for the current-bank registers.
    return (encoded >> 2) & 0x07


def _compute_add_flags(
    size_kind: str,
    left_value: int,
    right_value: int,
) -> dict[str, bool]:
    """Compute the modeled flag subset for an ADD-family result.

    Catalog: ADD/ADC — flags S Z H V N=0 C all modified.
    """
    bits = {"byte": 8, "word": 16, "long": 32}[size_kind]
    mask = (1 << bits) - 1
    sign_bit = 1 << (bits - 1)
    result = (left_value + right_value) & mask
    return {
        "sf": bool(result & sign_bit),
        "zf": result == 0,
        "vf": bool(((~(left_value ^ right_value)) & (left_value ^ result) & sign_bit) != 0),
        "hf": bool(((left_value ^ right_value ^ result) & 0x10) != 0),
        "cf": (left_value + right_value) > mask,
        "nf": False,
    }


def _compute_logical_flags(
    size_kind: str, result: int, *, half_carry: bool = False,
) -> dict[str, bool]:
    """Compute the modeled flag subset for a logical (AND/OR/XOR) result.

    TLCS-900/H semantics (Z80 heritage): S and Z depend on result; **V is the
    PARITY flag** (even parity of the result -> V=1) -- the V bit is
    "Parity / Overflow" per `T900_DENSE_REF.md` §SR (bit 2 = P/V; even parity
    PE => VF=1, odd parity PO => VF=0). C and N are cleared. **H is SET (1) for
    AND and CLEARED (0) for OR/XOR** (the classic Z80 rule) -- callers pass
    `half_carry=True` for AND, and the default (False) covers OR/XOR.

    (Corrected 2026-07-09 from `oracle_tools/cosim_diff` triage against the
    native NeoPop reference, each verified against the TLCS-900 spec: (1) V was
    wrongly forced to 0 -- `xor WA,WA` on Big Bang set V=1 (parity of 0 = even);
    (2) H was not written at all -- `and W,0xE0` on Neo Turf Masters set H=1.)
    """
    bits = {"byte": 8, "word": 16, "long": 32}[size_kind]
    sign_bit = 1 << (bits - 1)
    return {
        "sf": bool(result & sign_bit),
        "zf": result == 0,
        "vf": _has_even_parity(result & ((1 << bits) - 1)),
        "hf": half_carry,
        "cf": False,
        "nf": False,
    }


def _compute_subtract_flags(
    size_kind: str,
    left_value: int,
    right_value: int,
) -> dict[str, bool]:
    bits = {"byte": 8, "word": 16, "long": 32}[size_kind]
    mask = (1 << bits) - 1
    sign_bit = 1 << (bits - 1)
    result = (left_value - right_value) & mask
    return {
        "sf": bool(result & sign_bit),
        "zf": result == 0,
        "vf": bool(((left_value ^ right_value) & (left_value ^ result) & sign_bit) != 0),
        "hf": bool(((left_value ^ right_value ^ result) & 0x10) != 0),
        "cf": left_value < right_value,
        "nf": True,
    }


def _evaluate_condition_code(cc_index: int, flags: StatusFlags) -> bool | None:
    sf = flags.sf
    zf = flags.zf
    vf = flags.vf
    cf = flags.cf

    if cc_index == 0:
        return False
    if cc_index == 8:
        return True
    if cc_index == 1:
        return None if sf is None or vf is None else sf != vf
    if cc_index == 2:
        return None if sf is None or vf is None or zf is None else zf or (sf != vf)
    if cc_index == 3:
        return None if cf is None or zf is None else cf or zf
    if cc_index == 4:
        return vf
    if cc_index == 5:
        return sf
    if cc_index == 6:
        return zf
    if cc_index == 7:
        return cf
    if cc_index == 9:
        return None if sf is None or vf is None else sf == vf
    if cc_index == 10:
        return None if sf is None or vf is None or zf is None else (not zf) and (sf == vf)
    if cc_index == 11:
        return None if cf is None or zf is None else (not cf) and (not zf)
    if cc_index == 12:
        return None if vf is None else not vf
    if cc_index == 13:
        return None if sf is None else not sf
    if cc_index == 14:
        return None if zf is None else not zf
    if cc_index == 15:
        return None if cf is None else not cf
    raise ValueError(f"unsupported condition-code index: {cc_index}")
