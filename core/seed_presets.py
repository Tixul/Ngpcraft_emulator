"""Shared seed presets used across session, CLI, and engine bridge."""

from __future__ import annotations


BIOS_HANDOFF_XSP = 0x00006C00
BIOS_HANDOFF_INTNEST = 0


def bios_handoff_minimal_seed_registers() -> dict[str, int]:
    """Return the current minimal BIOS hand-off seed register map.

    Shared by:
    - `EmulatorSession` hand-off seeding
    - CLI `--seed-bios-handoff-minimal`
    - engine bridge `runtime.seed_presets=["bios-handoff-minimal"]`
    """
    return {
        "XSP": BIOS_HANDOFF_XSP,
        "INTNEST": BIOS_HANDOFF_INTNEST,
    }
