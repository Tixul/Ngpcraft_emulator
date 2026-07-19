"""M3 Phase 3.2.3a — per-instruction cycle accounting infrastructure.

⚠️ THE ADDRESSING-MODE ADDER IS PART OF THE COST, and these assertions say so
explicitly. Toshiba bills a memory-operand instruction as

    cycles = base(instruction) + extra(addressing mode)

and gives the second term its own table (instruction lists (10)). This core used
to apply NO adder at all, and these tests pinned that defect -- every expectation
below was the bare base. They now assert `base + _addressing_mode_cycle_extra(...)`,
which keeps the manufacturer's base constant under test while making the adder
visible at every site.

Phase 3.2.3a wires `cycles_consumed` through `ExecutionResult` and
`IrqDeliveryResult`, accumulates `total_cycles_consumed` in
`RunStepsResult` / `RunUntilResult` / `ExecutionTraceResult`, and
switches `_advance_frame_state_for_run` to consume the cycle total
instead of multiplying `executed_count` by the flat estimate.

Currently unpopulated opcodes still contribute the flat
`ESTIMATED_CYCLES_PER_INSTRUCTION` (8), but the first 3.2.3b slice
now overrides common control-flow / CPU-control instructions
(`NOP`, `RETI`, etc.) with real Toshiba timing. IRQ delivery still
contributes `IRQ_DELIVERY_CYCLES` (13).
"""

from __future__ import annotations

import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from core.cpu import StatusFlags, create_unknown_control_registers
from core.execute import (
    _addressing_mode_cycle_extra,
    ALU_MEM_DEST_LONG_CYCLES,
    ALU_MEM_DEST_WORD_CYCLES,
    ALU_MEM_IMM16_CYCLES,
    BIT_MEM_READ_CYCLES,
    BIT_MEM_WRITE_CYCLES,
    CF_REG_CYCLES,
    CP_MEM_IMM8_CYCLES,
    CF_MEM_READ_CYCLES,
    ALU_IMM8_CYCLES,
    ALU_REG_REG_CYCLES,
    CPL_NEG_CYCLES,
    BS1_CYCLES,
    DAA_CYCLES,
    DJNZ_CYCLES_NOT_TAKEN,
    DJNZ_CYCLES_TAKEN,
    EX_FF_CYCLES,
    EXT_CYCLES,
    INCDEC_REG_CYCLES,
    INCDEC_MEM_BYTE_CYCLES,
    INCF_DECF_CYCLES,
    IRQ_DELIVERY_CYCLES,
    CALL_MEM_CYCLES,
    JP_MEM_CYCLES,
    LDC_CONTROL_REGISTER_CYCLES,
    LD_IMM16_CYCLES,
    LD_IMM32_CYCLES,
    LD_IMM8_CYCLES,
    LDX_CYCLES,
    LD_REG_REG_CYCLES,
    LD_SMALL_IMM_CYCLES,
    LDA_CYCLES,
    MEM_LOAD_BYTE_CYCLES,
    MEM_LOAD_LONG_CYCLES,
    MEM_LOAD_WORD_CYCLES,
    MEM_STORE_IMM16_CYCLES,
    MEM_STORE_IMM8_CYCLES,
    MEM_STORE_LONG_CYCLES,
    MIRR_CYCLES,
    MINC_CYCLES,
    MDEC_CYCLES,
    MULA_CYCLES,
    NOP_CYCLES,
    PAA_CYCLES,
    POP_R16_CYCLES,
    POP_R32_CYCLES,
    POP_PREFIX_BYTE_CYCLES,
    POP_PREFIX_LONG_CYCLES,
    PUSH_MEM_WORD_CYCLES,
    PUSH_PREFIX_BYTE_CYCLES,
    PUSH_PREFIX_LONG_CYCLES,
    PUSH_R16_CYCLES,
    PUSH_R32_CYCLES,
    PUSHW_IMM16_CYCLES,
    REG_BIT_OP_CYCLES,
    REG_TSET_OP_CYCLES,
    RETI_CYCLES,
    ROTSHIFT_MEM_BYTE_CYCLES,
    SHIFT_IMM_BASE_CYCLES,
    SHIFT_REG_A_CYCLES,
    build_execute_next,
    try_deliver_pending_irq,
)
from core.fetch import load_fetch_view
from core.frame_timing import (
    ESTIMATED_CYCLES_PER_INSTRUCTION,
    IrqState,
    initial_irq_state,
)
from core.run_steps import build_run_steps, build_run_until


def _write_demo_rom(path: Path, body: bytes, entry_point: int = 0x00200040) -> None:
    data = bytearray(0x50)
    data[0x00:0x1C] = b"LICENSED BY SNK CORPORATION".ljust(0x1C, b"\x00")
    data[0x1C:0x20] = entry_point.to_bytes(4, "little")
    data[0x23] = 0x10
    data[0x24:0x30] = b"CYCLES 3.2.3A\x00\x00"
    body_offset = entry_point - 0x00200000
    data[body_offset : body_offset + len(body)] = body
    path.write_bytes(bytes(data))


def _seeded_cpu(view, *, xsp=0x6C00, iff_level=0):
    base = view.machine.cpu
    return replace(
        base,
        regs=replace(base.regs, xsp=xsp),
        flags=StatusFlags(sf=False, zf=False, vf=False, hf=False, cf=False, nf=False),
        iff_level=iff_level,
        rfp=0,
    )


class CycleConstantsTests(unittest.TestCase):
    def test_irq_delivery_cycles_is_13(self) -> None:
        # Toshiba TLCS-900/H IRQ entry cost.
        self.assertEqual(IRQ_DELIVERY_CYCLES, 13)

    def test_estimated_cycles_per_instruction_is_8(self) -> None:
        # Flat placeholder until Phase 3.2.3b populates the table.
        self.assertEqual(ESTIMATED_CYCLES_PER_INSTRUCTION, 8)


class ExecutionResultCyclesTests(unittest.TestCase):
    def test_nop_consumes_real_cycles(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            _write_demo_rom(rom_path, b"\x00")
            view = load_fetch_view(rom_path)
            result = build_execute_next(view)
            self.assertEqual(result.status, "executed")
            self.assertEqual(result.cycles_consumed, NOP_CYCLES + _addressing_mode_cycle_extra(b"\x00"))

    def test_reti_consumes_real_cycles(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            _write_demo_rom(rom_path, b"\x07")
            view = load_fetch_view(rom_path)
            cpu = _seeded_cpu(view, xsp=0x6BFA)
            memory = {
                0x006BFA: 0x34,
                0x006BFB: 0x12,
                0x006BFC: 0x20,
                0x006BFD: 0x00,
                0x006BFE: 0x00,
                0x006BFF: 0x88,
            }
            result = build_execute_next(view, cpu_state=cpu, memory_bytes=memory)
            self.assertEqual(result.status, "executed")
            self.assertEqual(result.cycles_consumed, RETI_CYCLES + _addressing_mode_cycle_extra(b"\x07"))

    def test_ex_ff_consumes_real_cycles(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            _write_demo_rom(rom_path, b"\x16")
            view = load_fetch_view(rom_path)
            result = build_execute_next(view, cpu_state=_seeded_cpu(view))
            self.assertEqual(result.status, "executed")
            self.assertEqual(result.cycles_consumed, EX_FF_CYCLES + _addressing_mode_cycle_extra(b"\x16"))

    def test_ld_r8_imm8_consumes_real_cycles(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            _write_demo_rom(rom_path, b"\x21\x34")
            view = load_fetch_view(rom_path)
            cpu = replace(
                _seeded_cpu(view),
                regs=replace(_seeded_cpu(view).regs, xwa=0x00000000),
            )
            result = build_execute_next(view, cpu_state=cpu)
            self.assertEqual(result.status, "executed")
            self.assertEqual(result.cycles_consumed, LD_IMM8_CYCLES + _addressing_mode_cycle_extra(b"\x21\x34"))

    def test_ld_r16_imm16_consumes_real_cycles(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            _write_demo_rom(rom_path, b"\x30\x34\x12")
            view = load_fetch_view(rom_path)
            cpu = replace(
                _seeded_cpu(view),
                regs=replace(_seeded_cpu(view).regs, xwa=0x00000000),
            )
            result = build_execute_next(view, cpu_state=cpu)
            self.assertEqual(result.status, "executed")
            self.assertEqual(result.cycles_consumed, LD_IMM16_CYCLES + _addressing_mode_cycle_extra(b"\x30\x34\x12"))

    def test_ld_r32_imm32_consumes_real_cycles(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            _write_demo_rom(rom_path, b"\x47\x00\x60\x00\x00")
            view = load_fetch_view(rom_path)
            result = build_execute_next(view, cpu_state=_seeded_cpu(view))
            self.assertEqual(result.status, "executed")
            self.assertEqual(result.cycles_consumed, LD_IMM32_CYCLES + _addressing_mode_cycle_extra(b"\x47\x00\x60\x00\x00"))

    def test_lda_abs24_consumes_real_cycles(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            _write_demo_rom(rom_path, b"\xF2\x78\x56\x34\x30")
            view = load_fetch_view(rom_path)
            result = build_execute_next(view, cpu_state=_seeded_cpu(view))
            self.assertEqual(result.status, "executed")
            self.assertEqual(result.cycles_consumed, LDA_CYCLES + _addressing_mode_cycle_extra(b"\xF2\x78\x56\x34\x30"))

    def test_prefixed_ld_reg_reg_consumes_real_cycles(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            _write_demo_rom(rom_path, b"\xEB\x8D")
            view = load_fetch_view(rom_path)
            cpu = replace(
                _seeded_cpu(view),
                regs=replace(_seeded_cpu(view).regs, xhl=0x00004000),
            )
            result = build_execute_next(view, cpu_state=cpu)
            self.assertEqual(result.status, "executed")
            self.assertEqual(result.cycles_consumed, LD_REG_REG_CYCLES + _addressing_mode_cycle_extra(b"\xEB\x8D"))

    def test_prefixed_ld_small_imm_consumes_real_cycles(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            _write_demo_rom(rom_path, b"\xE9\xAD")
            view = load_fetch_view(rom_path)
            result = build_execute_next(view, cpu_state=_seeded_cpu(view))
            self.assertEqual(result.status, "executed")
            self.assertEqual(result.cycles_consumed, LD_SMALL_IMM_CYCLES + _addressing_mode_cycle_extra(b"\xE9\xAD"))

    def test_prefixed_alu_reg_reg_consumes_real_cycles(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            _write_demo_rom(rom_path, b"\xE9\x80")
            view = load_fetch_view(rom_path)
            cpu = replace(
                _seeded_cpu(view),
                regs=replace(_seeded_cpu(view).regs, xwa=0x10, xbc=0x05),
            )
            result = build_execute_next(view, cpu_state=cpu)
            self.assertEqual(result.status, "executed")
            self.assertEqual(result.cycles_consumed, ALU_REG_REG_CYCLES + _addressing_mode_cycle_extra(b"\xE9\x80"))

    def test_prefixed_alu_imm8_consumes_real_cycles(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            _write_demo_rom(rom_path, b"\xC9\xC8\x01")
            view = load_fetch_view(rom_path)
            cpu = replace(
                _seeded_cpu(view),
                regs=replace(_seeded_cpu(view).regs, xwa=0x00000010),
            )
            result = build_execute_next(view, cpu_state=cpu)
            self.assertEqual(result.status, "executed")
            self.assertEqual(result.cycles_consumed, ALU_IMM8_CYCLES + _addressing_mode_cycle_extra(b"\xC9\xC8\x01"))

    def test_prefixed_carry_flag_register_uses_real_cycles(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            _write_demo_rom(rom_path, b"\xC9\x20\x03")
            view = load_fetch_view(rom_path)
            cpu = replace(
                _seeded_cpu(view),
                regs=replace(_seeded_cpu(view).regs, xwa=0x00000008),
                flags=StatusFlags(sf=False, zf=False, vf=False, hf=False, cf=True, nf=False),
            )
            result = build_execute_next(view, cpu_state=cpu)
            self.assertEqual(result.status, "executed")
            self.assertEqual(result.cycles_consumed, CF_REG_CYCLES + _addressing_mode_cycle_extra(b"\xC9\x20\x03"))

    def test_prefixed_ldc_uses_real_cycles(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            _write_demo_rom(rom_path, b"\xD8\x2E\x20")
            view = load_fetch_view(rom_path)
            cpu = replace(
                _seeded_cpu(view),
                regs=replace(_seeded_cpu(view).regs, xwa=0x00001234),
            )
            result = build_execute_next(view, cpu_state=cpu)
            self.assertEqual(result.status, "executed")
            self.assertEqual(result.cycles_consumed, LDC_CONTROL_REGISTER_CYCLES + _addressing_mode_cycle_extra(b"\xD8\x2E\x20"))

    def test_prefixed_inc_dec_consumes_real_cycles(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            _write_demo_rom(rom_path, b"\xEF\x6C")
            view = load_fetch_view(rom_path)
            cpu = replace(
                _seeded_cpu(view),
                regs=replace(_seeded_cpu(view).regs, xsp=0x00004100),
            )
            result = build_execute_next(view, cpu_state=cpu)
            self.assertEqual(result.status, "executed")
            self.assertEqual(result.cycles_consumed, INCDEC_REG_CYCLES + _addressing_mode_cycle_extra(b"\xEF\x6C"))

    def test_prefixed_exts_consumes_real_cycles(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            _write_demo_rom(rom_path, b"\xE8\x13")
            view = load_fetch_view(rom_path)
            cpu = replace(
                _seeded_cpu(view),
                regs=replace(_seeded_cpu(view).regs, xwa=0x00008001),
            )
            result = build_execute_next(view, cpu_state=cpu)
            self.assertEqual(result.status, "executed")
            self.assertEqual(result.cycles_consumed, EXT_CYCLES + _addressing_mode_cycle_extra(b"\xE8\x13"))

    def test_prefixed_cpl_uses_real_cycles(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            _write_demo_rom(rom_path, b"\xC9\x06")
            view = load_fetch_view(rom_path)
            cpu = replace(
                _seeded_cpu(view),
                regs=replace(_seeded_cpu(view).regs, xwa=0x0000005A),
            )
            result = build_execute_next(view, cpu_state=cpu)
            self.assertEqual(result.status, "executed")
            self.assertEqual(result.cycles_consumed, CPL_NEG_CYCLES + _addressing_mode_cycle_extra(b"\xC9\x06"))

    def test_prefixed_neg_uses_real_cycles(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            _write_demo_rom(rom_path, b"\xC9\x07")
            view = load_fetch_view(rom_path)
            cpu = replace(
                _seeded_cpu(view),
                regs=replace(_seeded_cpu(view).regs, xwa=0x00000005),
            )
            result = build_execute_next(view, cpu_state=cpu)
            self.assertEqual(result.status, "executed")
            self.assertEqual(result.cycles_consumed, CPL_NEG_CYCLES + _addressing_mode_cycle_extra(b"\xC9\x07"))

    def test_c7_byte_slice_load_and_alu_use_same_real_cycles(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            _write_demo_rom(rom_path, b"\xC7\xE6\x99")
            view = load_fetch_view(rom_path)
            cpu = replace(
                _seeded_cpu(view),
                regs=replace(_seeded_cpu(view).regs, xwa=0x000000AB, xbc=0x00000000),
            )
            load_result = build_execute_next(view, cpu_state=cpu)
            self.assertEqual(load_result.status, "executed")
            self.assertEqual(load_result.cycles_consumed, LD_REG_REG_CYCLES)

            rom_path_alu = Path(tmpdir) / "demo_alu.ngc"
            _write_demo_rom(rom_path_alu, b"\xC7\xE6\x80")
            view_alu = load_fetch_view(rom_path_alu)
            cpu_alu = replace(
                _seeded_cpu(view_alu),
                regs=replace(_seeded_cpu(view_alu).regs, xwa=0x00000100, xbc=0x00200000),
            )
            alu_result = build_execute_next(view_alu, cpu_state=cpu_alu)
            self.assertEqual(alu_result.status, "executed")
            self.assertEqual(alu_result.cycles_consumed, ALU_REG_REG_CYCLES)

    def test_c7_shift_immediate_uses_toshiba_formula(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            _write_demo_rom(rom_path, b"\xC7\xE6\xE8\x03")
            view = load_fetch_view(rom_path)
            cpu = replace(
                _seeded_cpu(view),
                regs=replace(_seeded_cpu(view).regs, xbc=0x00810000),
            )
            result = build_execute_next(view, cpu_state=cpu)
            self.assertEqual(result.status, "executed")
            self.assertEqual(result.cycles_consumed, SHIFT_IMM_BASE_CYCLES + _addressing_mode_cycle_extra(b"\xC7\xE6\xE8\x03"))

    def test_c7_shift_by_a_uses_real_cycles(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            _write_demo_rom(rom_path, b"\xC7\xE6\xF8")
            view = load_fetch_view(rom_path)
            cpu = replace(
                _seeded_cpu(view),
                regs=replace(_seeded_cpu(view).regs, xwa=0x00000003, xbc=0x00810000),
            )
            result = build_execute_next(view, cpu_state=cpu)
            self.assertEqual(result.status, "executed")
            # `3 + n/4` with A = 3 -> 3. The old flat SHIFT_REG_A_CYCLES = 2 was
            # wrong for any count >= 4 (Toshiba list (8) gives the `A` form the
            # same state as the `#4` form).
            self.assertEqual(result.cycles_consumed, 3 + _addressing_mode_cycle_extra(b"\xC7\xE6\xF8"))

    def test_c7_cpl_uses_real_cycles(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            _write_demo_rom(rom_path, b"\xC7\xE6\x06")
            view = load_fetch_view(rom_path)
            cpu = replace(
                _seeded_cpu(view),
                regs=replace(_seeded_cpu(view).regs, xbc=0x005A0000),
            )
            result = build_execute_next(view, cpu_state=cpu)
            self.assertEqual(result.status, "executed")
            self.assertEqual(result.cycles_consumed, CPL_NEG_CYCLES + _addressing_mode_cycle_extra(b"\xC7\xE6\x06"))

    def test_c7_neg_uses_real_cycles(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            _write_demo_rom(rom_path, b"\xC7\xE6\x07")
            view = load_fetch_view(rom_path)
            cpu = replace(
                _seeded_cpu(view),
                regs=replace(_seeded_cpu(view).regs, xbc=0x00050000),
            )
            result = build_execute_next(view, cpu_state=cpu)
            self.assertEqual(result.status, "executed")
            self.assertEqual(result.cycles_consumed, CPL_NEG_CYCLES + _addressing_mode_cycle_extra(b"\xC7\xE6\x07"))

    def test_c7_carry_flag_register_uses_real_cycles(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            _write_demo_rom(rom_path, b"\xC7\xE6\x20\x03")
            view = load_fetch_view(rom_path)
            cpu = replace(
                _seeded_cpu(view),
                regs=replace(_seeded_cpu(view).regs, xbc=0x00080000),
                flags=StatusFlags(sf=False, zf=False, vf=False, hf=False, cf=True, nf=False),
            )
            result = build_execute_next(view, cpu_state=cpu)
            self.assertEqual(result.status, "executed")
            self.assertEqual(result.cycles_consumed, CF_REG_CYCLES + _addressing_mode_cycle_extra(b"\xC7\xE6\x20\x03"))

    def test_c7_ldc_uses_real_cycles(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            _write_demo_rom(rom_path, b"\xC7\xE6\x2F\x22")
            view = load_fetch_view(rom_path)
            cpu = replace(
                _seeded_cpu(view),
                regs=replace(_seeded_cpu(view).regs, xbc=0x11223344),
                control_registers=replace(
                    create_unknown_control_registers(),
                    dmam=(0xAA, None, None, None),
                ),
            )
            result = build_execute_next(view, cpu_state=cpu)
            self.assertEqual(result.status, "executed")
            self.assertEqual(result.cycles_consumed, LDC_CONTROL_REGISTER_CYCLES + _addressing_mode_cycle_extra(b"\xC7\xE6\x2F\x22"))

    def test_memory_byte_load_consumes_real_cycles(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            _write_demo_rom(rom_path, b"\x80\x27")
            view = load_fetch_view(rom_path)
            cpu = replace(
                _seeded_cpu(view),
                regs=replace(_seeded_cpu(view).regs, xwa=0x00005000, xhl=0x00000000),
            )
            result = build_execute_next(
                view, cpu_state=cpu, memory_bytes={0x005000: 0xAB},
            )
            self.assertEqual(result.status, "executed")
            self.assertEqual(result.cycles_consumed, MEM_LOAD_BYTE_CYCLES + _addressing_mode_cycle_extra(b"\x80\x27"))

    def test_memory_long_load_consumes_real_cycles(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            _write_demo_rom(rom_path, b"\xE4\xE0\x21")
            view = load_fetch_view(rom_path)
            cpu = replace(
                _seeded_cpu(view),
                regs=replace(_seeded_cpu(view).regs, xwa=0x00005004),
            )
            result = build_execute_next(
                view,
                cpu_state=cpu,
                memory_bytes={
                    0x005000: 0x78,
                    0x005001: 0x56,
                    0x005002: 0x34,
                    0x005003: 0x12,
                },
            )
            self.assertEqual(result.status, "executed")
            self.assertEqual(result.cycles_consumed, MEM_LOAD_LONG_CYCLES + _addressing_mode_cycle_extra(b"\xE4\xE0\x21"))

    def test_memory_long_store_consumes_real_cycles(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            _write_demo_rom(rom_path, b"\xBF\x04\x61")
            view = load_fetch_view(rom_path)
            cpu = replace(
                _seeded_cpu(view),
                regs=replace(_seeded_cpu(view).regs, xsp=0x000040F8, xbc=0x00213266),
            )
            result = build_execute_next(view, cpu_state=cpu)
            self.assertEqual(result.status, "executed")
            self.assertEqual(result.cycles_consumed, MEM_STORE_LONG_CYCLES + _addressing_mode_cycle_extra(b"\xBF\x04\x61"))

    def test_memory_imm8_store_consumes_real_cycles(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            _write_demo_rom(rom_path, b"\xB8\x01\x00\xA0")
            view = load_fetch_view(rom_path)
            cpu = replace(
                _seeded_cpu(view),
                regs=replace(_seeded_cpu(view).regs, xwa=0x00005000),
            )
            result = build_execute_next(view, cpu_state=cpu)
            self.assertEqual(result.status, "executed")
            self.assertEqual(result.cycles_consumed, MEM_STORE_IMM8_CYCLES + _addressing_mode_cycle_extra(b"\xB8\x01\x00\xA0"))

    def test_memory_imm16_store_consumes_real_cycles(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            _write_demo_rom(rom_path, b"\xB1\x02\x34\x12")
            view = load_fetch_view(rom_path)
            cpu = replace(
                _seeded_cpu(view),
                regs=replace(_seeded_cpu(view).regs, xbc=0x00005000),
            )
            result = build_execute_next(view, cpu_state=cpu)
            self.assertEqual(result.status, "executed")
            self.assertEqual(result.cycles_consumed, MEM_STORE_IMM16_CYCLES + _addressing_mode_cycle_extra(b"\xB1\x02\x34\x12"))

    def test_memory_compare_uses_load_family_cycles(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            _write_demo_rom(rom_path, b"\xD2\xFC\x5E\x00\xF0")
            view = load_fetch_view(rom_path)
            cpu = replace(
                _seeded_cpu(view),
                regs=replace(_seeded_cpu(view).regs, xwa=0x000055AA),
            )
            result = build_execute_next(
                view,
                cpu_state=cpu,
                memory_bytes={0x005EFC: 0xAA, 0x005EFD: 0x55},
            )
            self.assertEqual(result.status, "executed")
            self.assertEqual(result.cycles_consumed, MEM_LOAD_WORD_CYCLES + _addressing_mode_cycle_extra(b"\xD2\xFC\x5E\x00\xF0"))

    def test_memory_alu_register_destination_uses_load_family_cycles(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            _write_demo_rom(rom_path, b"\x9F\x04\x80")
            view = load_fetch_view(rom_path)
            cpu = replace(
                _seeded_cpu(view),
                regs=replace(_seeded_cpu(view).regs, xsp=0x00006000, xwa=0xAABB0002),
            )
            result = build_execute_next(
                view,
                cpu_state=cpu,
                memory_bytes={0x006004: 0x01, 0x006005: 0x00},
            )
            self.assertEqual(result.status, "executed")
            self.assertEqual(result.cycles_consumed, MEM_LOAD_WORD_CYCLES + _addressing_mode_cycle_extra(b"\x9F\x04\x80"))

    def test_memory_alu_memory_destination_uses_real_cycles(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            _write_demo_rom(rom_path, b"\x9F\x04\x88")
            view = load_fetch_view(rom_path)
            cpu = replace(
                _seeded_cpu(view),
                regs=replace(_seeded_cpu(view).regs, xsp=0x00006000, xwa=0x00000002),
            )
            result = build_execute_next(
                view,
                cpu_state=cpu,
                memory_bytes={0x006004: 0x01, 0x006005: 0x00},
            )
            self.assertEqual(result.status, "executed")
            self.assertEqual(result.cycles_consumed, ALU_MEM_DEST_WORD_CYCLES + _addressing_mode_cycle_extra(b"\x9F\x04\x88"))

    def test_memory_long_alu_memory_destination_uses_real_cycles(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            _write_demo_rom(rom_path, b"\xAF\x04\xD8")
            view = load_fetch_view(rom_path)
            cpu = replace(
                _seeded_cpu(view),
                regs=replace(_seeded_cpu(view).regs, xsp=0x00006000, xwa=0x0F0F0F0F),
            )
            result = build_execute_next(
                view,
                cpu_state=cpu,
                memory_bytes={0x006004: 0xFF, 0x006005: 0x00, 0x006006: 0xFF, 0x006007: 0x00},
            )
            self.assertEqual(result.status, "executed")
            self.assertEqual(result.cycles_consumed, ALU_MEM_DEST_LONG_CYCLES + _addressing_mode_cycle_extra(b"\xAF\x04\xD8"))

    def test_memory_alu_immediate_word_uses_real_cycles(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            _write_demo_rom(rom_path, b"\x9F\x04\x38\x34\x12")
            view = load_fetch_view(rom_path)
            cpu = replace(
                _seeded_cpu(view),
                regs=replace(_seeded_cpu(view).regs, xsp=0x00006000),
            )
            result = build_execute_next(
                view,
                cpu_state=cpu,
                memory_bytes={0x006004: 0x01, 0x006005: 0x00},
            )
            self.assertEqual(result.status, "executed")
            self.assertEqual(result.cycles_consumed, ALU_MEM_IMM16_CYCLES + _addressing_mode_cycle_extra(b"\x9F\x04\x38\x34\x12"))

    def test_memory_compare_immediate_uses_real_cycles(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            _write_demo_rom(rom_path, b"\xC1\x91\x6F\x3F\x00")
            view = load_fetch_view(rom_path)
            result = build_execute_next(
                view,
                memory_bytes={0x006F91: 0x00},
            )
            self.assertEqual(result.status, "executed")
            self.assertEqual(result.cycles_consumed, CP_MEM_IMM8_CYCLES + _addressing_mode_cycle_extra(b"\xC1\x91\x6F\x3F\x00"))

    def test_memory_increment_uses_real_cycles(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            _write_demo_rom(rom_path, b"\xC2\x06\x4F\x00\x61")
            view = load_fetch_view(rom_path)
            cpu = replace(
                _seeded_cpu(view),
                flags=StatusFlags(sf=False, zf=False, vf=False, hf=False, cf=True, nf=True),
            )
            result = build_execute_next(
                view,
                cpu_state=cpu,
                memory_bytes={0x004F06: 0x7F},
            )
            self.assertEqual(result.status, "executed")
            self.assertEqual(result.cycles_consumed, INCDEC_MEM_BYTE_CYCLES + _addressing_mode_cycle_extra(b"\xC2\x06\x4F\x00\x61"))

    def test_memory_bit_test_uses_real_cycles(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            _write_demo_rom(rom_path, b"\xF2\x6A\x4C\x00\xCC")
            view = load_fetch_view(rom_path)
            cpu = replace(
                _seeded_cpu(view),
                flags=StatusFlags(sf=True, zf=False, vf=True, hf=False, cf=True, nf=True),
            )
            result = build_execute_next(
                view,
                cpu_state=cpu,
                memory_bytes={0x004C6A: 0x00},
            )
            self.assertEqual(result.status, "executed")
            self.assertEqual(result.cycles_consumed, BIT_MEM_READ_CYCLES + _addressing_mode_cycle_extra(b"\xF2\x6A\x4C\x00\xCC"))

    def test_memory_tset_uses_real_cycles(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            _write_demo_rom(rom_path, b"\xF2\x6A\x4C\x00\xA8")
            view = load_fetch_view(rom_path)
            cpu = replace(
                _seeded_cpu(view),
                flags=StatusFlags(sf=True, zf=False, vf=True, hf=False, cf=True, nf=True),
            )
            result = build_execute_next(
                view,
                cpu_state=cpu,
                memory_bytes={0x004C6A: 0x00},
            )
            self.assertEqual(result.status, "executed")
            self.assertEqual(result.cycles_consumed, BIT_MEM_WRITE_CYCLES + _addressing_mode_cycle_extra(b"\xF2\x6A\x4C\x00\xA8"))

    def test_memory_andcf_uses_real_cycles(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            _write_demo_rom(rom_path, b"\xF2\x6A\x4C\x00\x80")
            view = load_fetch_view(rom_path)
            cpu = replace(
                _seeded_cpu(view),
                flags=StatusFlags(sf=True, zf=False, vf=True, hf=False, cf=True, nf=True),
            )
            result = build_execute_next(
                view,
                cpu_state=cpu,
                memory_bytes={0x004C6A: 0x00},
            )
            self.assertEqual(result.status, "executed")
            self.assertEqual(result.cycles_consumed, CF_MEM_READ_CYCLES + _addressing_mode_cycle_extra(b"\xF2\x6A\x4C\x00\x80"))

    def test_memory_ldcf_uses_real_cycles(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            _write_demo_rom(rom_path, b"\xF2\x6A\x4C\x00\x2B")
            view = load_fetch_view(rom_path)
            cpu = replace(
                _seeded_cpu(view),
                flags=StatusFlags(sf=False, zf=True, vf=True, hf=False, cf=False, nf=True),
                regs=replace(_seeded_cpu(view).regs, xwa=0x00000004),
            )
            result = build_execute_next(
                view,
                cpu_state=cpu,
                memory_bytes={0x004C6A: 0x10},
            )
            self.assertEqual(result.status, "executed")
            self.assertEqual(result.cycles_consumed, CF_MEM_READ_CYCLES + _addressing_mode_cycle_extra(b"\xF2\x6A\x4C\x00\x2B"))

    def test_memory_stcf_uses_real_cycles(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            _write_demo_rom(rom_path, b"\xF2\x6A\x4C\x00\xA0")
            view = load_fetch_view(rom_path)
            cpu = replace(
                _seeded_cpu(view),
                flags=StatusFlags(sf=False, zf=True, vf=True, hf=False, cf=False, nf=True),
            )
            result = build_execute_next(
                view,
                cpu_state=cpu,
                memory_bytes={0x004C6A: 0xFF},
            )
            self.assertEqual(result.status, "executed")
            self.assertEqual(result.cycles_consumed, BIT_MEM_WRITE_CYCLES + _addressing_mode_cycle_extra(b"\xF2\x6A\x4C\x00\xA0"))

    def test_memory_res_uses_real_cycles(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            _write_demo_rom(rom_path, b"\xF1\x10\x80\xB3")
            view = load_fetch_view(rom_path)
            result = build_execute_next(view)
            self.assertEqual(result.status, "executed")
            self.assertEqual(result.cycles_consumed, BIT_MEM_WRITE_CYCLES + _addressing_mode_cycle_extra(b"\xF1\x10\x80\xB3"))

    def test_memory_set_uses_real_cycles(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            _write_demo_rom(rom_path, b"\xF1\x10\x80\xBB")
            view = load_fetch_view(rom_path)
            result = build_execute_next(view)
            self.assertEqual(result.status, "executed")
            self.assertEqual(result.cycles_consumed, BIT_MEM_WRITE_CYCLES + _addressing_mode_cycle_extra(b"\xF1\x10\x80\xBB"))

    def test_memory_rotate_uses_real_cycles(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            _write_demo_rom(rom_path, b"\x83\x78")
            view = load_fetch_view(rom_path)
            cpu = replace(
                _seeded_cpu(view),
                regs=replace(_seeded_cpu(view).regs, xhl=0x00006000),
                flags=StatusFlags(sf=False, zf=False, vf=False, hf=False, cf=False, nf=False),
            )
            result = build_execute_next(
                view,
                cpu_state=cpu,
                memory_bytes={0x00006000: 0x81},
            )
            self.assertEqual(result.status, "executed")
            self.assertEqual(result.cycles_consumed, ROTSHIFT_MEM_BYTE_CYCLES + _addressing_mode_cycle_extra(b"\x83\x78"))

    def test_memory_shift_through_carry_uses_real_cycles(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            _write_demo_rom(rom_path, b"\x83\x7A")
            view = load_fetch_view(rom_path)
            cpu = replace(
                _seeded_cpu(view),
                regs=replace(_seeded_cpu(view).regs, xhl=0x00006000),
                flags=StatusFlags(sf=False, zf=False, vf=False, hf=False, cf=True, nf=False),
            )
            result = build_execute_next(
                view,
                cpu_state=cpu,
                memory_bytes={0x00006000: 0x40},
            )
            self.assertEqual(result.status, "executed")
            self.assertEqual(result.cycles_consumed, ROTSHIFT_MEM_BYTE_CYCLES + _addressing_mode_cycle_extra(b"\x83\x7A"))

    def test_register_bit_op_uses_real_cycles(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            _write_demo_rom(rom_path, b"\xC9\x33\x02")
            view = load_fetch_view(rom_path)
            cpu = replace(
                _seeded_cpu(view),
                flags=StatusFlags(sf=True, zf=False, vf=True, hf=False, cf=True, nf=True),
                regs=replace(_seeded_cpu(view).regs, xwa=0x00000004),
            )
            result = build_execute_next(view, cpu_state=cpu)
            self.assertEqual(result.status, "executed")
            self.assertEqual(result.cycles_consumed, REG_BIT_OP_CYCLES + _addressing_mode_cycle_extra(b"\xC9\x33\x02"))

    def test_register_set_op_uses_real_cycles(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            _write_demo_rom(rom_path, b"\xC9\x31\x06")
            view = load_fetch_view(rom_path)
            cpu = replace(
                _seeded_cpu(view),
                flags=StatusFlags(sf=True, zf=False, vf=True, hf=False, cf=True, nf=True),
                regs=replace(_seeded_cpu(view).regs, xwa=0x00000000),
            )
            result = build_execute_next(view, cpu_state=cpu)
            self.assertEqual(result.status, "executed")
            self.assertEqual(result.cycles_consumed, REG_BIT_OP_CYCLES + _addressing_mode_cycle_extra(b"\xC9\x31\x06"))

    def test_register_tset_op_uses_real_cycles(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            _write_demo_rom(rom_path, b"\xC9\x34\x06")
            view = load_fetch_view(rom_path)
            cpu = replace(
                _seeded_cpu(view),
                flags=StatusFlags(sf=False, zf=False, vf=True, hf=True, cf=False, nf=True),
                regs=replace(_seeded_cpu(view).regs, xwa=0x00000000),
            )
            result = build_execute_next(view, cpu_state=cpu)
            self.assertEqual(result.status, "executed")
            self.assertEqual(result.cycles_consumed, REG_TSET_OP_CYCLES + _addressing_mode_cycle_extra(b"\xC9\x34\x06"))

    def test_incf_consumes_real_cycles(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            _write_demo_rom(rom_path, b"\x0C")
            view = load_fetch_view(rom_path)
            cpu = replace(_seeded_cpu(view), rfp=2, register_bank=2)
            result = build_execute_next(view, cpu_state=cpu)
            self.assertEqual(result.status, "executed")
            self.assertEqual(result.cycles_consumed, INCF_DECF_CYCLES + _addressing_mode_cycle_extra(b"\x0C"))

    def test_decf_consumes_real_cycles(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            _write_demo_rom(rom_path, b"\x0D")
            view = load_fetch_view(rom_path)
            cpu = replace(_seeded_cpu(view), rfp=0, register_bank=0)
            result = build_execute_next(view, cpu_state=cpu)
            self.assertEqual(result.status, "executed")
            self.assertEqual(result.cycles_consumed, INCF_DECF_CYCLES + _addressing_mode_cycle_extra(b"\x0D"))

    def test_shift_immediate_register_uses_real_cycles(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            _write_demo_rom(rom_path, b"\xC9\xE8\x03")
            view = load_fetch_view(rom_path)
            cpu = replace(
                _seeded_cpu(view),
                regs=replace(_seeded_cpu(view).regs, xwa=0x00000081),
            )
            result = build_execute_next(view, cpu_state=cpu)
            self.assertEqual(result.status, "executed")
            self.assertEqual(result.cycles_consumed, SHIFT_IMM_BASE_CYCLES + _addressing_mode_cycle_extra(b"\xC9\xE8\x03"))

    def test_shift_immediate_register_cycle_formula_steps_at_4(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            _write_demo_rom(rom_path, b"\xC9\xEC\x04")
            view = load_fetch_view(rom_path)
            cpu = replace(
                _seeded_cpu(view),
                regs=replace(_seeded_cpu(view).regs, xwa=0x00000011),
            )
            result = build_execute_next(view, cpu_state=cpu)
            self.assertEqual(result.status, "executed")
            self.assertEqual(result.cycles_consumed, SHIFT_IMM_BASE_CYCLES + 1 + _addressing_mode_cycle_extra(b"\xC9\xEC\x04"))

    def test_rotate_through_carry_register_uses_real_cycles(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            _write_demo_rom(rom_path, b"\xC9\xEA\x01")
            view = load_fetch_view(rom_path)
            cpu = replace(
                _seeded_cpu(view),
                regs=replace(_seeded_cpu(view).regs, xwa=0x00000040),
                flags=StatusFlags(sf=False, zf=False, vf=False, hf=False, cf=True, nf=False),
            )
            result = build_execute_next(view, cpu_state=cpu)
            self.assertEqual(result.status, "executed")
            self.assertEqual(result.cycles_consumed, SHIFT_IMM_BASE_CYCLES + _addressing_mode_cycle_extra(b"\xC9\xEA\x01"))

    def test_shift_by_a_register_uses_real_cycles(self) -> None:
        """A shift-by-A costs `3 + n/4`, where n is the RUNTIME count in A.

        It is not a flat cost. Toshiba instruction list (8) gives `RLC A, r` the
        same state as `RLC #4, r` -- "3 + n/4" -- so the price grows with the
        number of shifts. This test used to assert the old flat
        `SHIFT_REG_A_CYCLES = 2`, which is wrong for any count >= 4; the
        byte-level cycle resolver had no way to see A, so the handler now bills it
        where the count is actually known. (Corrected 2026-07-12, found by the C++
        differential harness.)
        """
        for a_value, expected in ((0x03, 3), (0x04, 4), (0x0C, 6), (0x0F, 6)):
            with self.subTest(a=a_value):
                with tempfile.TemporaryDirectory() as tmpdir:
                    rom_path = Path(tmpdir) / "demo.ngc"
                    _write_demo_rom(rom_path, b"\xC9\xF8")
                    view = load_fetch_view(rom_path)
                    cpu = replace(
                        _seeded_cpu(view),
                        regs=replace(_seeded_cpu(view).regs, xwa=0x00008100 | a_value),
                    )
                    result = build_execute_next(view, cpu_state=cpu)
                    self.assertEqual(result.status, "executed")
                    self.assertEqual(result.cycles_consumed, expected + _addressing_mode_cycle_extra(b"\xC9\xF8"))

    def test_ldx_consumes_real_cycles(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            _write_demo_rom(rom_path, b"\xF7\xAA\x66\xBB\xCC\xDD")
            view = load_fetch_view(rom_path)
            result = build_execute_next(view)
            self.assertEqual(result.status, "executed")
            self.assertEqual(result.cycles_consumed, LDX_CYCLES + _addressing_mode_cycle_extra(b"\xF7\xAA\x66\xBB\xCC\xDD"))

    def test_pushw_memory_consumes_real_cycles(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            _write_demo_rom(rom_path, b"\xD2\x02\x5F\x00\x04")
            view = load_fetch_view(rom_path)
            cpu = replace(_seeded_cpu(view), regs=replace(_seeded_cpu(view).regs, xsp=0x00006010))
            result = build_execute_next(
                view,
                cpu_state=cpu,
                memory_bytes={0x005F02: 0xAA, 0x005F03: 0x55},
            )
            self.assertEqual(result.status, "executed")
            self.assertEqual(result.cycles_consumed, PUSH_MEM_WORD_CYCLES + _addressing_mode_cycle_extra(b"\xD2\x02\x5F\x00\x04"))

    def test_pushw_immediate_consumes_real_cycles(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            _write_demo_rom(rom_path, b"\x0B\x34\x12")
            view = load_fetch_view(rom_path)
            result = build_execute_next(view, cpu_state=_seeded_cpu(view))
            self.assertEqual(result.status, "executed")
            self.assertEqual(result.cycles_consumed, PUSHW_IMM16_CYCLES + _addressing_mode_cycle_extra(b"\x0B\x34\x12"))

    def test_push_r16_consumes_real_cycles(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            _write_demo_rom(rom_path, b"\x28")
            view = load_fetch_view(rom_path)
            base_cpu = _seeded_cpu(view)
            cpu = replace(base_cpu, regs=replace(base_cpu.regs, xwa=0x12345678))
            result = build_execute_next(view, cpu_state=cpu)
            self.assertEqual(result.status, "executed")
            self.assertEqual(result.cycles_consumed, PUSH_R16_CYCLES + _addressing_mode_cycle_extra(b"\x28"))

    def test_push_r32_consumes_real_cycles(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            _write_demo_rom(rom_path, b"\x38")
            view = load_fetch_view(rom_path)
            base_cpu = _seeded_cpu(view)
            cpu = replace(base_cpu, regs=replace(base_cpu.regs, xwa=0x12345678))
            result = build_execute_next(view, cpu_state=cpu)
            self.assertEqual(result.status, "executed")
            self.assertEqual(result.cycles_consumed, PUSH_R32_CYCLES + _addressing_mode_cycle_extra(b"\x38"))

    def test_pop_r16_consumes_real_cycles(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            _write_demo_rom(rom_path, b"\x48")
            view = load_fetch_view(rom_path)
            cpu = replace(_seeded_cpu(view), regs=replace(_seeded_cpu(view).regs, xwa=0x00000000))
            result = build_execute_next(
                view,
                cpu_state=cpu,
                memory_bytes={0x006C00: 0x78, 0x006C01: 0x56},
            )
            self.assertEqual(result.status, "executed")
            self.assertEqual(result.cycles_consumed, POP_R16_CYCLES + _addressing_mode_cycle_extra(b"\x48"))

    def test_pop_r32_consumes_real_cycles(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            _write_demo_rom(rom_path, b"\x58")
            view = load_fetch_view(rom_path)
            cpu = _seeded_cpu(view)
            result = build_execute_next(
                view,
                cpu_state=cpu,
                memory_bytes={
                    0x006C00: 0x78,
                    0x006C01: 0x56,
                    0x006C02: 0x34,
                    0x006C03: 0x12,
                },
            )
            self.assertEqual(result.status, "executed")
            self.assertEqual(result.cycles_consumed, POP_R32_CYCLES + _addressing_mode_cycle_extra(b"\x58"))

    def test_prefixed_push_byte_consumes_real_cycles(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            _write_demo_rom(rom_path, b"\xC9\x04")
            view = load_fetch_view(rom_path)
            cpu = replace(_seeded_cpu(view), regs=replace(_seeded_cpu(view).regs, xwa=0x0000005A))
            result = build_execute_next(view, cpu_state=cpu)
            self.assertEqual(result.status, "executed")
            self.assertEqual(result.cycles_consumed, PUSH_PREFIX_BYTE_CYCLES + _addressing_mode_cycle_extra(b"\xC9\x04"))

    def test_prefixed_pop_byte_consumes_real_cycles(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            _write_demo_rom(rom_path, b"\xC9\x05")
            view = load_fetch_view(rom_path)
            cpu = replace(_seeded_cpu(view), regs=replace(_seeded_cpu(view).regs, xwa=0x00000000))
            result = build_execute_next(
                view,
                cpu_state=cpu,
                memory_bytes={0x006C00: 0x5A},
            )
            self.assertEqual(result.status, "executed")
            self.assertEqual(result.cycles_consumed, POP_PREFIX_BYTE_CYCLES + _addressing_mode_cycle_extra(b"\xC9\x05"))

    def test_prefixed_push_long_consumes_real_cycles(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            _write_demo_rom(rom_path, b"\xE9\x04")
            view = load_fetch_view(rom_path)
            cpu = replace(_seeded_cpu(view), regs=replace(_seeded_cpu(view).regs, xbc=0x12345678))
            result = build_execute_next(view, cpu_state=cpu)
            self.assertEqual(result.status, "executed")
            self.assertEqual(result.cycles_consumed, PUSH_PREFIX_LONG_CYCLES + _addressing_mode_cycle_extra(b"\xE9\x04"))

    def test_prefixed_pop_long_consumes_real_cycles(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            _write_demo_rom(rom_path, b"\xE9\x05")
            view = load_fetch_view(rom_path)
            cpu = replace(_seeded_cpu(view), regs=replace(_seeded_cpu(view).regs, xbc=0x00000000))
            result = build_execute_next(
                view,
                cpu_state=cpu,
                memory_bytes={
                    0x006C00: 0x78,
                    0x006C01: 0x56,
                    0x006C02: 0x34,
                    0x006C03: 0x12,
                },
            )
            self.assertEqual(result.status, "executed")
            self.assertEqual(result.cycles_consumed, POP_PREFIX_LONG_CYCLES + _addressing_mode_cycle_extra(b"\xE9\x05"))

    def test_c7_push_slice_consumes_real_cycles(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            _write_demo_rom(rom_path, b"\xC7\xE6\x04")
            view = load_fetch_view(rom_path)
            cpu = replace(_seeded_cpu(view), regs=replace(_seeded_cpu(view).regs, xbc=0x005A0000))
            result = build_execute_next(view, cpu_state=cpu)
            self.assertEqual(result.status, "executed")
            self.assertEqual(result.cycles_consumed, PUSH_PREFIX_BYTE_CYCLES + _addressing_mode_cycle_extra(b"\xC7\xE6\x04"))

    def test_c7_pop_slice_consumes_real_cycles(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            _write_demo_rom(rom_path, b"\xC7\xE6\x05")
            view = load_fetch_view(rom_path)
            cpu = replace(_seeded_cpu(view), regs=replace(_seeded_cpu(view).regs, xbc=0x00000000))
            result = build_execute_next(
                view,
                cpu_state=cpu,
                memory_bytes={0x006C00: 0x5A},
            )
            self.assertEqual(result.status, "executed")
            self.assertEqual(result.cycles_consumed, POP_PREFIX_BYTE_CYCLES + _addressing_mode_cycle_extra(b"\xC7\xE6\x05"))

    def test_prefixed_daa_consumes_real_cycles(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            _write_demo_rom(rom_path, b"\xC9\x10")
            view = load_fetch_view(rom_path)
            cpu = replace(
                _seeded_cpu(view),
                regs=replace(_seeded_cpu(view).regs, xwa=0x0000006C),
                flags=StatusFlags(sf=False, zf=False, vf=False, hf=False, cf=False, nf=False),
            )
            result = build_execute_next(view, cpu_state=cpu)
            self.assertEqual(result.status, "executed")
            self.assertEqual(result.cycles_consumed, DAA_CYCLES + _addressing_mode_cycle_extra(b"\xC9\x10"))

    def test_c7_daa_consumes_real_cycles(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            _write_demo_rom(rom_path, b"\xC7\xE6\x10")
            view = load_fetch_view(rom_path)
            cpu = replace(
                _seeded_cpu(view),
                regs=replace(_seeded_cpu(view).regs, xbc=0x006C0000),
                flags=StatusFlags(sf=False, zf=False, vf=False, hf=False, cf=False, nf=False),
            )
            result = build_execute_next(view, cpu_state=cpu)
            self.assertEqual(result.status, "executed")
            self.assertEqual(result.cycles_consumed, DAA_CYCLES + _addressing_mode_cycle_extra(b"\xC7\xE6\x10"))

    def test_prefixed_long_paa_consumes_real_cycles(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            _write_demo_rom(rom_path, b"\xD8\x14")
            view = load_fetch_view(rom_path)
            cpu = replace(
                _seeded_cpu(view),
                regs=replace(_seeded_cpu(view).regs, xwa=0x00234567),
            )
            result = build_execute_next(view, cpu_state=cpu)
            self.assertEqual(result.status, "executed")
            self.assertEqual(result.cycles_consumed, PAA_CYCLES + _addressing_mode_cycle_extra(b"\xD8\x14"))

    def test_prefixed_byte_djnz_taken_consumes_real_cycles(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            _write_demo_rom(rom_path, b"\xC9\x1C\xFE")
            view = load_fetch_view(rom_path)
            cpu = replace(
                _seeded_cpu(view),
                regs=replace(_seeded_cpu(view).regs, xwa=0x00000002),
            )
            result = build_execute_next(view, cpu_state=cpu)
            self.assertEqual(result.status, "executed")
            self.assertEqual(result.cycles_consumed, DJNZ_CYCLES_TAKEN + _addressing_mode_cycle_extra(b"\xC9\x1C\xFE"))

    def test_prefixed_byte_djnz_not_taken_consumes_real_cycles(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            _write_demo_rom(rom_path, b"\xC9\x1C\xFE")
            view = load_fetch_view(rom_path)
            cpu = replace(
                _seeded_cpu(view),
                regs=replace(_seeded_cpu(view).regs, xwa=0x00000001),
            )
            result = build_execute_next(view, cpu_state=cpu)
            self.assertEqual(result.status, "executed")
            self.assertEqual(result.cycles_consumed, DJNZ_CYCLES_NOT_TAKEN + _addressing_mode_cycle_extra(b"\xC9\x1C\xFE"))

    def test_prefixed_mirr_consumes_real_cycles(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            _write_demo_rom(rom_path, b"\xDB\x16")
            view = load_fetch_view(rom_path)
            cpu = replace(
                _seeded_cpu(view),
                regs=replace(_seeded_cpu(view).regs, xhl=0x00001234),
            )
            result = build_execute_next(view, cpu_state=cpu)
            self.assertEqual(result.status, "executed")
            self.assertEqual(result.cycles_consumed, MIRR_CYCLES + _addressing_mode_cycle_extra(b"\xDB\x16"))

    def test_prefixed_bs1_consumes_real_cycles(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            _write_demo_rom(rom_path, b"\xDC\x0E")
            view = load_fetch_view(rom_path)
            cpu = replace(
                _seeded_cpu(view),
                regs=replace(_seeded_cpu(view).regs, xwa=0xAAAA0000, xix=0x00001200),
            )
            result = build_execute_next(view, cpu_state=cpu)
            self.assertEqual(result.status, "executed")
            self.assertEqual(result.cycles_consumed, BS1_CYCLES + _addressing_mode_cycle_extra(b"\xDC\x0E"))

    def test_prefixed_mula_consumes_real_cycles(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            _write_demo_rom(rom_path, b"\xDC\x19")
            view = load_fetch_view(rom_path)
            cpu = replace(
                _seeded_cpu(view),
                regs=replace(
                    _seeded_cpu(view).regs,
                    xde=0x00000100,
                    xhl=0x00000200,
                    xix=0x50000000,
                ),
            )
            result = build_execute_next(
                view,
                cpu_state=cpu,
                memory_bytes={
                    0x0100: 0x34,
                    0x0101: 0x12,
                    0x0200: 0xAB,
                    0x0201: 0x89,
                },
            )
            self.assertEqual(result.status, "executed")
            self.assertEqual(result.cycles_consumed, MULA_CYCLES + _addressing_mode_cycle_extra(b"\xDC\x19"))

    def test_prefixed_minc_consumes_real_cycles(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            _write_demo_rom(rom_path, b"\xDC\x39\x06\x00")
            view = load_fetch_view(rom_path)
            cpu = replace(
                _seeded_cpu(view),
                regs=replace(_seeded_cpu(view).regs, xix=0x00001236),
            )
            result = build_execute_next(view, cpu_state=cpu)
            self.assertEqual(result.status, "executed")
            self.assertEqual(result.cycles_consumed, MINC_CYCLES + _addressing_mode_cycle_extra(b"\xDC\x39\x06\x00"))

    def test_prefixed_mdec_consumes_real_cycles(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            _write_demo_rom(rom_path, b"\xDC\x3C\x07\x00")
            view = load_fetch_view(rom_path)
            cpu = replace(
                _seeded_cpu(view),
                regs=replace(_seeded_cpu(view).regs, xix=0x00001230),
            )
            result = build_execute_next(view, cpu_state=cpu)
            self.assertEqual(result.status, "executed")
            self.assertEqual(result.cycles_consumed, MDEC_CYCLES + _addressing_mode_cycle_extra(b"\xDC\x3C\x07\x00"))

    def test_secondary_indexed_jump_consumes_real_cycles(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            _write_demo_rom(rom_path, b"\xF3\x07\xF0\xE0\xD8")
            view = load_fetch_view(rom_path)
            cpu = replace(
                _seeded_cpu(view),
                regs=replace(_seeded_cpu(view).regs, xix=0x00201000, xwa=0x00000034),
            )
            result = build_execute_next(view, cpu_state=cpu)
            self.assertEqual(result.status, "executed")
            self.assertEqual(result.cycles_consumed, JP_MEM_CYCLES + _addressing_mode_cycle_extra(b"\xF3\x07\xF0\xE0\xD8"))

    def test_indirect_call_via_xix_consumes_real_cycles(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            _write_demo_rom(rom_path, b"\xB4\xE8")
            view = load_fetch_view(rom_path)
            cpu = replace(
                _seeded_cpu(view, xsp=0x6010),
                regs=replace(_seeded_cpu(view, xsp=0x6010).regs, xix=0x00201234, xsp=0x6010),
            )
            result = build_execute_next(view, cpu_state=cpu)
            self.assertEqual(result.status, "executed")
            self.assertEqual(result.cycles_consumed, CALL_MEM_CYCLES + _addressing_mode_cycle_extra(b"\xB4\xE8"))

    def test_blocked_result_still_carries_default_cycles(self) -> None:
        # Block on PUSH SR without modeled flags.
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            _write_demo_rom(rom_path, b"\x02")
            view = load_fetch_view(rom_path)
            cpu = replace(
                view.machine.cpu,
                regs=replace(view.machine.cpu.regs, xsp=0x6C00),
            )
            result = build_execute_next(view, cpu_state=cpu)
            self.assertNotEqual(result.status, "executed")
            # Default value is still 8 — but the run loop only sums
            # cycles when status == "executed", so this doesn't leak
            # into total_cycles_consumed.
            self.assertEqual(
                result.cycles_consumed, ESTIMATED_CYCLES_PER_INSTRUCTION,
            )


class IrqDeliveryCyclesTests(unittest.TestCase):
    def test_delivered_irq_reports_irq_delivery_cycles(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            _write_demo_rom(rom_path, b"\x00")
            view = load_fetch_view(rom_path)
            cpu = _seeded_cpu(view, iff_level=0)
            result = try_deliver_pending_irq(
                view=view, cpu=cpu, memory={},
                irq_state=initial_irq_state().with_vblank_pending(),
            )
            self.assertTrue(result.delivered)
            self.assertEqual(result.cycles_consumed, IRQ_DELIVERY_CYCLES + _addressing_mode_cycle_extra(b"\x00"))

    def test_no_delivery_reports_zero_cycles(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            _write_demo_rom(rom_path, b"\x00")
            view = load_fetch_view(rom_path)
            cpu = _seeded_cpu(view, iff_level=7)  # masks everything
            result = try_deliver_pending_irq(
                view=view, cpu=cpu, memory={},
                irq_state=initial_irq_state().with_vblank_pending(),
            )
            self.assertFalse(result.delivered)
            self.assertEqual(result.cycles_consumed, 0 + _addressing_mode_cycle_extra(b"\x00"))


class RunResultCycleAccumulationTests(unittest.TestCase):
    def test_run_steps_sums_executed_instruction_cycles(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            _write_demo_rom(rom_path, b"\x00\x00\x00")
            view = load_fetch_view(rom_path)
            result = build_run_steps(view, count=3)
            self.assertEqual(result.executed_count, 3)
            self.assertEqual(result.total_cycles_consumed, 3 * NOP_CYCLES)

    def test_run_steps_adds_irq_delivery_cycles_when_delivered(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            _write_demo_rom(rom_path, b"\x00\x00\x00")
            view = load_fetch_view(rom_path)
            cpu = _seeded_cpu(view, iff_level=0)
            irq = initial_irq_state().with_vblank_pending()
            result = build_run_steps(view, count=2, cpu_state=cpu, irq_state=irq)
            self.assertEqual(result.irq_deliveries, 1)
            self.assertEqual(
                result.total_cycles_consumed,
                IRQ_DELIVERY_CYCLES + 2 * NOP_CYCLES,
            )

    def test_run_steps_no_cycles_when_blocked_on_first_step(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            _write_demo_rom(rom_path, b"\x02")  # PUSH SR — needs full SR.
            view = load_fetch_view(rom_path)
            cpu = replace(
                view.machine.cpu,
                regs=replace(view.machine.cpu.regs, xsp=0x6C00),
            )
            result = build_run_steps(view, count=3, cpu_state=cpu)
            self.assertEqual(result.executed_count, 0)
            self.assertEqual(result.total_cycles_consumed, 0)

    def test_run_until_sums_cycles_across_loop(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            _write_demo_rom(rom_path, b"\x00\x00\x00\x00\x00")
            view = load_fetch_view(rom_path)
            result = build_run_until(
                view, target_pc=0x00200044, cpu_state=_seeded_cpu(view),
            )
            self.assertEqual(result.executed_count, 4)
            self.assertEqual(result.total_cycles_consumed, 4 * NOP_CYCLES)


class FrameStateAdvancementTests(unittest.TestCase):
    def test_advance_helper_consumes_total_cycles_when_provided(self) -> None:
        from ngpc_emu import _advance_frame_state_for_run
        from core.frame_timing import (
            FrameState,
            CYCLES_PER_SCANLINE,
        )

        # 517 cycles = exactly 1 scanline.
        start = FrameState(scanline=10, frame_count=0)
        result = _advance_frame_state_for_run(
            start, executed_count=999,  # ignored when total_cycles passed
            total_cycles_consumed=CYCLES_PER_SCANLINE,
        )
        assert result is not None
        self.assertEqual(result.scanline, 11)

    def test_advance_helper_falls_back_to_executed_count_path(self) -> None:
        from ngpc_emu import _advance_frame_state_for_run
        from core.frame_timing import FrameState

        # 64 instructions × 8 cycles = 512 cycles, just under 1 scanline.
        # 65 instructions × 8 = 520, just over 1 scanline.
        start = FrameState(scanline=0, frame_count=0)
        below = _advance_frame_state_for_run(start, 64)
        above = _advance_frame_state_for_run(start, 65)
        assert below is not None and above is not None
        self.assertEqual(below.scanline, 0)
        self.assertEqual(above.scanline, 1)

    def test_advance_helper_irq_cycles_contribute_to_frame_state(self) -> None:
        from ngpc_emu import _advance_frame_state_for_run
        from core.frame_timing import FrameState, CYCLES_PER_SCANLINE

        # Two equivalent runs:
        #   A: 64 instructions × 8 = 512 cycles (stays scanline 0)
        #   B: 63 instructions × 8 + 1 IRQ × 13 = 504 + 13 = 517 cycles
        #      (crosses to scanline 1)
        # The IRQ cycle accounting matters: the cycle total path detects
        # the boundary, the executed-count path doesn't.
        start = FrameState(scanline=0, frame_count=0)
        cycle_path = _advance_frame_state_for_run(
            start, executed_count=63, total_cycles_consumed=63 * 8 + 13,
        )
        legacy_path = _advance_frame_state_for_run(start, executed_count=64)
        assert cycle_path is not None and legacy_path is not None
        # 517 cycles = exactly 1 scanline boundary.
        self.assertEqual(cycle_path.scanline, 1)
        # 64 × 8 = 512 < 517 → stays scanline 0.
        self.assertEqual(legacy_path.scanline, 0)


if __name__ == "__main__":
    unittest.main()
