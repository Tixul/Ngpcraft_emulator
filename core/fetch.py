"""Minimal PC-relative fetch helpers for NgpCraft Emulator."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from core.machine import NgpcMachineState, load_machine_state
from typing import TYPE_CHECKING

from core.memory import MemoryReadResult, NgpcReadBus, load_read_bus

if TYPE_CHECKING:
    from core.frame_timing import FrameState


@dataclass(frozen=True)
class NgpcFetchView:
    """Current minimal fetch context."""

    machine: NgpcMachineState
    bus: NgpcReadBus


@dataclass(frozen=True)
class FetchResult:
    """Result of a raw byte fetch starting from the current PC."""

    pc: int
    width: int
    status: str
    read: MemoryReadResult
    data: bytes | None
    next_sequential_pc: int | None
    note: str


def fetch_next_bytes(view: NgpcFetchView, size: int = 4) -> FetchResult:
    """Fetch a raw byte window from the current PC without decoding it yet."""
    if size <= 0:
        raise ValueError("size must be >= 1")

    read = view.bus.read_bytes(view.machine.cpu.pc, size=size)
    if read.status != "ok":
        return FetchResult(
            pc=view.machine.cpu.pc,
            width=size,
            status=read.status,
            read=read,
            data=None,
            next_sequential_pc=None,
            note=(
                "Raw PC-relative fetch failed through the current minimal bus model. "
                "No instruction length or decode information is available yet."
            ),
        )

    return FetchResult(
        pc=view.machine.cpu.pc,
        width=size,
        status="ok",
        read=read,
        data=read.data,
        next_sequential_pc=view.machine.cpu.pc + size,
        note=(
            "Fetched a raw byte window from the current PC. "
            "This is not yet a decoded TLCS-900 instruction and does not imply the "
            "true instruction length."
        ),
    )


def load_fetch_view(
    path: str | Path,
    *,
    frame_state: "FrameState | None" = None,
    bios_path: str | Path | None = None,
) -> NgpcFetchView:
    """Load the current minimal fetch context from one ROM path.

    M3 Phase 3.1: `frame_state` is forwarded to the bus so reads of
    `RAS.V` (`0x008009`) and the BLNK bit of `2D Status` (`0x008010`)
    reflect the live frame timing. Callers that don't supply one
    (default) get the documented HW reset (scanline 0, BLNK=0).
    """
    return NgpcFetchView(
        machine=load_machine_state(path),
        bus=load_read_bus(path, frame_state=frame_state, bios_path=bios_path),
    )
