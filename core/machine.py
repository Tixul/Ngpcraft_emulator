"""Minimal machine bootstrap state for NgpCraft Emulator."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from core.bus import AddressMapEntry, build_address_space
from core.cpu import NgpcCpuState, create_bootstrap_cpu_state
from core.rom import NgpcRomHeader, load_rom_header


@dataclass(frozen=True)
class NgpcMachineState:
    """Minimal machine state created from a ROM header."""

    rom_path: Path
    header: NgpcRomHeader
    cpu: NgpcCpuState
    memory_regions: tuple[AddressMapEntry, ...]
    model_status: str
    note: str


def create_machine_state(header: NgpcRomHeader) -> NgpcMachineState:
    """Build the current minimal reset/bootstrap model from a parsed header."""
    cpu = create_bootstrap_cpu_state(header.entry_point)
    return NgpcMachineState(
        rom_path=header.path,
        header=header,
        cpu=cpu,
        memory_regions=build_address_space(header).regions,
        model_status="partial-bootstrap",
        note=(
            "This is not yet a hardware-accurate reset snapshot. "
            "It is a minimal machine bootstrap view used to validate ROM loading "
            "and memory map assumptions before CPU execution exists."
        ),
    )


def load_machine_state(path: str | Path) -> NgpcMachineState:
    """Load ROM header and create the current minimal machine state."""
    return create_machine_state(load_rom_header(path))
