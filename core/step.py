"""Minimal static step-preview helpers for NgpCraft Emulator."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from core.decode import DecodeResult, decode_instruction_at
from core.fetch import NgpcFetchView, load_fetch_view


@dataclass(frozen=True)
class StepPreview:
    """Current minimal non-executing step-into preview."""

    decode: DecodeResult
    mode: str
    preview_target: int | None
    reason: str
    note: str


def build_step_preview(
    view: NgpcFetchView,
    start_pc: int | None = None,
    mode: str = "into",
) -> StepPreview:
    """Build a decode-only step preview from one address."""
    if mode not in {"into", "over"}:
        raise ValueError("mode must be 'into' or 'over'")

    pc = view.machine.cpu.pc if start_pc is None else start_pc
    decoded = decode_instruction_at(view.bus, pc)

    if decoded.status != "decoded":
        return StepPreview(
            decode=decoded,
            mode=mode,
            preview_target=None,
            reason="decode-not-available",
            note=(
                "Static step preview could not resolve a next target because decode did not "
                "succeed at the requested address."
            ),
        )

    if mode == "over":
        return _build_step_over_preview(decoded)
    return _build_step_into_preview(decoded)


def _build_step_into_preview(decoded: DecodeResult) -> StepPreview:
    """Build a static step-into preview from one decoded instruction."""
    if decoded.control_flow_kind is None:
        return StepPreview(
            decode=decoded,
            mode="into",
            preview_target=decoded.next_sequential_pc,
            reason="sequential-non-control-flow",
            note=(
                "This is a non-control-flow instruction in the current model, so static step "
                "preview advances to the sequential next PC."
            ),
        )

    if decoded.control_flow_kind in {"jump", "call"} and decoded.direct_target is not None:
        return StepPreview(
            decode=decoded,
            mode="into",
            preview_target=decoded.direct_target,
            reason=f"direct-{decoded.control_flow_kind}-target",
            note=(
                "This instruction has a statically known direct target, so static step-into "
                "preview points to that target."
            ),
        )

    if decoded.control_flow_kind == "conditional-branch":
        return StepPreview(
            decode=decoded,
            mode="into",
            preview_target=None,
            reason="conditional-control-flow-unresolved",
            note=(
                "This control-flow instruction depends on runtime condition state, so static "
                "step preview does not claim one next target."
            ),
        )

    return StepPreview(
        decode=decoded,
        mode="into",
        preview_target=None,
        reason="runtime-control-flow-unresolved",
        note=(
            "This control-flow instruction transfers control in a way the current static step "
            "preview cannot resolve without execution state changes."
        ),
    )


def _build_step_over_preview(decoded: DecodeResult) -> StepPreview:
    """Build a static step-over preview from one decoded instruction."""
    if decoded.control_flow_kind is None:
        return StepPreview(
            decode=decoded,
            mode="over",
            preview_target=decoded.next_sequential_pc,
            reason="sequential-non-control-flow",
            note=(
                "This is a non-control-flow instruction in the current model, so static step-over "
                "preview advances to the sequential next PC."
            ),
        )

    if decoded.control_flow_kind == "call":
        return StepPreview(
            decode=decoded,
            mode="over",
            preview_target=decoded.next_sequential_pc,
            reason="call-return-site-preview",
            note=(
                "Static step-over preview keeps the current frame and points to the return site "
                "after the call, assuming the call returns normally."
            ),
        )

    if decoded.control_flow_kind == "jump" and decoded.direct_target is not None:
        return StepPreview(
            decode=decoded,
            mode="over",
            preview_target=decoded.direct_target,
            reason="direct-jump-target",
            note=(
                "This is direct non-call control flow, so static step-over preview points to the "
                "known jump target."
            ),
        )

    if decoded.control_flow_kind == "conditional-branch":
        return StepPreview(
            decode=decoded,
            mode="over",
            preview_target=None,
            reason="conditional-control-flow-unresolved",
            note=(
                "This conditional control-flow instruction depends on runtime state, so static "
                "step-over preview does not claim one next target."
            ),
        )

    return StepPreview(
        decode=decoded,
        mode="over",
        preview_target=None,
        reason="runtime-control-flow-unresolved",
        note=(
            "This control-flow instruction transfers control in a way the current static step-over "
            "preview cannot resolve without execution state changes."
        ),
    )


def load_step_preview(
    path: str | Path,
    start_pc: int | None = None,
    mode: str = "into",
) -> StepPreview:
    """Load a ROM and build the current static step preview."""
    view = load_fetch_view(path)
    return build_step_preview(view=view, start_pc=start_pc, mode=mode)
