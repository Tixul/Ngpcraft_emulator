"""Minimal linear trace preview helpers for NgpCraft Emulator."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from core.decode import DecodeResult, decode_instruction_at
from core.fetch import NgpcFetchView, load_fetch_view
from core.quirks import match_known_silicon_broken


@dataclass(frozen=True)
class TracePreviewRecord:
    """One record in the current linear trace preview model."""

    index: int
    decode: DecodeResult


@dataclass(frozen=True)
class TracePreview:
    """Current minimal linear trace preview result."""

    start_pc: int
    records: tuple[TracePreviewRecord, ...]
    requested_count: int
    emitted_count: int
    stop_reason: str
    note: str


def build_trace_preview(
    view: NgpcFetchView,
    count: int = 8,
    start_pc: int | None = None,
    stop_on_control_flow: bool = False,
    stop_on_silicon_broken: bool = True,
) -> TracePreview:
    """Build a sequential non-executing trace preview from one start address."""
    if count <= 0:
        raise ValueError("count must be >= 1")

    pc = view.machine.cpu.pc if start_pc is None else start_pc
    records: list[TracePreviewRecord] = []
    stop_reason = "count-reached"

    for index in range(count):
        decoded = decode_instruction_at(view.bus, pc)
        records.append(TracePreviewRecord(index=index, decode=decoded))
        if decoded.status != "decoded":
            stop_reason = f"stopped-on-{decoded.status}"
            break
        if stop_on_silicon_broken and match_known_silicon_broken(decoded) is not None:
            stop_reason = "stopped-on-silicon-broken"
            break
        if stop_on_control_flow and _is_control_flow_record(decoded):
            stop_reason = "stopped-on-control-flow"
            break
        if decoded.next_sequential_pc is None:
            stop_reason = "stopped-on-missing-next-pc"
            break
        pc = decoded.next_sequential_pc

    return TracePreview(
        start_pc=view.machine.cpu.pc if start_pc is None else start_pc,
        records=tuple(records),
        requested_count=count,
        emitted_count=len(records),
        stop_reason=stop_reason,
        note=(
            "This trace preview is a linear decode-only walk using sequential next-PC values. "
            "It does not execute instructions, does not follow taken branches, and is not yet "
            "the final instruction-trace format."
        ),
    )


def load_trace_preview(
    path: str | Path,
    count: int = 8,
    start_pc: int | None = None,
    stop_on_control_flow: bool = False,
    stop_on_silicon_broken: bool = True,
) -> TracePreview:
    """Load a ROM and build the current linear trace preview."""
    view = load_fetch_view(path)
    return build_trace_preview(
        view=view,
        count=count,
        start_pc=start_pc,
        stop_on_control_flow=stop_on_control_flow,
        stop_on_silicon_broken=stop_on_silicon_broken,
    )


def _is_control_flow_record(decoded: DecodeResult) -> bool:
    """Return true when a decoded instruction changes or may change control flow."""
    return decoded.control_flow_kind is not None
