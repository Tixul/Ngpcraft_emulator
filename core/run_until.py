"""Minimal static run-until-preview helpers for NgpCraft Emulator."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from core.fetch import NgpcFetchView, load_fetch_view
from core.step import StepPreview, build_step_preview


@dataclass(frozen=True)
class RunUntilPreviewRecord:
    """One chained static step in the current run-until preview model."""

    index: int
    step: StepPreview


@dataclass(frozen=True)
class RunUntilPreview:
    """Current minimal static run-until preview result."""

    start_pc: int
    target_pc: int
    mode: str
    max_steps: int
    records: tuple[RunUntilPreviewRecord, ...]
    emitted_count: int
    stop_reason: str
    reached_target: bool
    terminal_pc: int | None
    note: str


def build_run_until_preview(
    view: NgpcFetchView,
    target_pc: int,
    start_pc: int | None = None,
    max_steps: int = 16,
    mode: str = "over",
) -> RunUntilPreview:
    """Chain static step previews until a target is reached or the model becomes unresolved."""
    if max_steps <= 0:
        raise ValueError("max_steps must be >= 1")
    if mode not in {"into", "over"}:
        raise ValueError("mode must be 'into' or 'over'")

    pc = view.machine.cpu.pc if start_pc is None else start_pc
    if pc == target_pc:
        return RunUntilPreview(
            start_pc=pc,
            target_pc=target_pc,
            mode=mode,
            max_steps=max_steps,
            records=(),
            emitted_count=0,
            stop_reason="already-at-target",
            reached_target=True,
            terminal_pc=pc,
            note=_build_note(mode),
        )

    records: list[RunUntilPreviewRecord] = []
    seen_pcs = {pc}
    stop_reason = "max-steps-reached"
    reached_target = False
    terminal_pc: int | None = pc

    for index in range(max_steps):
        step = build_step_preview(view=view, start_pc=pc, mode=mode)
        records.append(RunUntilPreviewRecord(index=index, step=step))

        if step.decode.status != "decoded":
            stop_reason = f"stopped-on-{step.decode.status}"
            terminal_pc = step.decode.pc
            break

        if step.preview_target is None:
            stop_reason = f"stopped-on-{step.reason}"
            terminal_pc = step.decode.pc
            break

        if step.preview_target == target_pc:
            stop_reason = "target-reached"
            reached_target = True
            terminal_pc = target_pc
            break

        if step.preview_target in seen_pcs:
            stop_reason = "stopped-on-cycle"
            terminal_pc = step.preview_target
            break

        seen_pcs.add(step.preview_target)
        pc = step.preview_target
        terminal_pc = pc

    return RunUntilPreview(
        start_pc=view.machine.cpu.pc if start_pc is None else start_pc,
        target_pc=target_pc,
        mode=mode,
        max_steps=max_steps,
        records=tuple(records),
        emitted_count=len(records),
        stop_reason=stop_reason,
        reached_target=reached_target,
        terminal_pc=terminal_pc,
        note=_build_note(mode),
    )


def load_run_until_preview(
    path: str | Path,
    target_pc: int,
    start_pc: int | None = None,
    max_steps: int = 16,
    mode: str = "over",
) -> RunUntilPreview:
    """Load a ROM and build the current static run-until preview."""
    view = load_fetch_view(path)
    return build_run_until_preview(
        view=view,
        target_pc=target_pc,
        start_pc=start_pc,
        max_steps=max_steps,
        mode=mode,
    )


def _build_note(mode: str) -> str:
    mode_note = (
        "In 'over' mode it chains static step-over previews and assumes direct calls return "
        "normally."
        if mode == "over"
        else "In 'into' mode it chains static step-into previews and follows direct call targets."
    )
    return (
        "This run-until preview is decode-only and execution-neutral. "
        f"{mode_note} It stops as soon as control flow becomes runtime-dependent, a decode "
        "fails, a cycle is detected, or the step budget is exhausted."
    )
