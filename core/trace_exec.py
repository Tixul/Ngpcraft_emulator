"""Minimal real execution-trace helpers for NgpCraft Emulator."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from core.cpu import NgpcCpuState
from core.execute import ExecutionResult, IrqDeliveryResult, seed_cpu_state_for_execution
from core.fetch import NgpcFetchView, load_fetch_view
from core.run_steps import build_run_steps

if TYPE_CHECKING:
    from core.frame_timing import FrameState, IrqState


@dataclass(frozen=True)
class ExecutionTraceRecord:
    """One record in the current real execution trace model."""

    index: int
    execution: ExecutionResult


@dataclass(frozen=True)
class ExecutionTraceResult:
    """Current minimal instruction-by-instruction execution trace result."""

    start_pc: int
    requested_count: int
    emitted_count: int
    executed_count: int
    stop_reason: str
    final_cpu: NgpcCpuState
    final_memory: dict[int, int]
    records: tuple[ExecutionTraceRecord, ...]
    note: str
    final_irq_state: "IrqState | None" = None
    irq_deliveries: int = 0
    last_irq_delivery: IrqDeliveryResult | None = None
    total_cycles_consumed: int = 0


def build_execution_trace(
    view: NgpcFetchView,
    start_pc: int | None = None,
    count: int = 8,
    cpu_state: NgpcCpuState | None = None,
    memory_bytes: dict[int, int] | None = None,
    irq_state: "IrqState | None" = None,
) -> ExecutionTraceResult:
    """Capture a bounded real execution trace from the current executor."""
    run_result = build_run_steps(
        view=view,
        start_pc=start_pc,
        count=count,
        cpu_state=cpu_state,
        memory_bytes=memory_bytes,
        irq_state=irq_state,
    )
    return ExecutionTraceResult(
        start_pc=run_result.start_pc,
        requested_count=run_result.requested_count,
        emitted_count=run_result.emitted_count,
        executed_count=run_result.executed_count,
        stop_reason=run_result.stop_reason,
        final_cpu=run_result.final_cpu,
        final_memory=run_result.final_memory,
        records=tuple(
            ExecutionTraceRecord(index=record.index, execution=record.execution)
            for record in run_result.records
        ),
        note=(
            "This trace captures the current real executed instruction stream, not a static "
            "decode preview. It shares the same limits as the current execute-next subset and "
            "stops at the first instruction that cannot be executed honestly."
        ),
        final_irq_state=run_result.final_irq_state,
        irq_deliveries=run_result.irq_deliveries,
        last_irq_delivery=run_result.last_irq_delivery,
        total_cycles_consumed=run_result.total_cycles_consumed,
    )


def load_execution_trace(
    path: str | Path,
    start_pc: int | None = None,
    count: int = 8,
    seed_xsp: int | None = None,
    seed_registers: dict[str, int] | None = None,
    initial_cpu_state: NgpcCpuState | None = None,
    initial_memory_bytes: dict[int, int] | None = None,
    initial_frame_state: "FrameState | None" = None,
    initial_irq_state: "IrqState | None" = None,
) -> ExecutionTraceResult:
    """Load a ROM and capture a bounded real execution trace.

    Seeding rules:
    - If `initial_cpu_state` is given, it overrides the bootstrap CPU state.
      `seed_xsp` and `seed_registers` may still be applied on top for
      targeted overrides.
    - If `initial_memory_bytes` is given, it seeds the writable runtime
      overlay before the first traced instruction (typically from a savestate).
    - If `initial_frame_state` is given (M3 Phase 3.1b), the executor's
      read bus exposes the matching `RAS.V` + BLNK bit.
    - If `initial_irq_state` is given (M3 Phase 3.2.2b), the executor
      samples the IRQ controller between instructions and may deliver
      a pending VBlank IRQ.
    """
    view = load_fetch_view(path, frame_state=initial_frame_state)
    cpu_state = view.machine.cpu if initial_cpu_state is None else initial_cpu_state
    if seed_xsp is not None or seed_registers:
        cpu_state = seed_cpu_state_for_execution(
            cpu_state,
            register_values=seed_registers,
            seed_xsp=seed_xsp,
        )
    return build_execution_trace(
        view=view,
        start_pc=start_pc,
        count=count,
        cpu_state=cpu_state,
        memory_bytes=initial_memory_bytes,
        irq_state=initial_irq_state,
    )
