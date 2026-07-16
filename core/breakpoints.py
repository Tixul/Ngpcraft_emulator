"""PC-address breakpoint registry and event-log match helpers (v1).

A breakpoint is a passive observer in v1: it does not pause execution
or alter the runtime overlay. It only filters events from a captured
event-log v2 payload to those whose `pc` matches a registered address.
Live pause-on-hit is reserved for the M4 debugger.

The breakpoint registry lives next to other per-ROM artefacts under
`<rom_dir>/.ngpc_emu/breakpoints/<rom_stem>.breakpoints.json`.

Per `BREAKPOINTS.md` the file format is:

```json
{
  "format": "ngpc-emu-breakpoints",
  "format_version": "2026-05-20.v1",
  "breakpoints": [
    {"id": 1, "address": 0x0020D180, "label": "stargunner-frontier"}
  ]
}
```

Loaders reject unknown `format` / `format_version` strictly, per the
project's "no implicit upgrade" rule.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

BREAKPOINTS_FORMAT = "ngpc-emu-breakpoints"
BREAKPOINTS_FORMAT_VERSION = "2026-05-20.v1"


@dataclass(frozen=True)
class Breakpoint:
    """One PC-address breakpoint.

    `address` is the 24-bit PC value at which the breakpoint fires.
    `label` is an optional human-readable annotation.
    """

    id: int
    address: int
    label: str | None = None


@dataclass(frozen=True)
class BreakpointHit:
    """One breakpoint hit recorded against a captured event log."""

    breakpoint: Breakpoint
    event_index: int
    event_pc: int
    assembly: str | None
    status: str | None


def breakpoints_root_for_rom(rom_path: Path) -> Path:
    """Return the default breakpoint directory for one ROM."""
    return rom_path.resolve().parent / ".ngpc_emu" / "breakpoints"


def breakpoints_path_for_rom(rom_path: Path) -> Path:
    """Return the default breakpoint registry path for one ROM."""
    return breakpoints_root_for_rom(rom_path) / f"{rom_path.stem}.breakpoints.json"


def load_breakpoints(rom_path: Path) -> tuple[Breakpoint, ...]:
    """Load the breakpoint registry for one ROM.

    Returns an empty tuple if no registry exists yet.  Raises on
    unknown format / version (no implicit upgrade).
    """
    path = breakpoints_path_for_rom(rom_path)
    if not path.exists():
        return ()
    raw = json.loads(path.read_text(encoding="utf-8"))
    _validate_schema(raw, path)
    return tuple(_payload_to_breakpoint(item) for item in raw["breakpoints"])


def save_breakpoints(rom_path: Path, breakpoints: tuple[Breakpoint, ...]) -> Path:
    """Persist a breakpoint tuple for one ROM and return the file path."""
    path = breakpoints_path_for_rom(rom_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "format": BREAKPOINTS_FORMAT,
        "format_version": BREAKPOINTS_FORMAT_VERSION,
        "breakpoints": [_breakpoint_to_payload(bp) for bp in breakpoints],
    }
    path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    return path


def add_breakpoint(
    rom_path: Path,
    *,
    address: int,
    label: str | None = None,
) -> Breakpoint:
    """Append a new breakpoint to the registry and return the saved row.

    Adding the same address twice is allowed: each gets its own id, so
    the operator can carry independent labels (e.g. "vblank entry" and
    "frame counter inc") for the same PC. Callers that want uniqueness
    must remove the existing entry first.
    """
    if address < 0:
        raise ValueError("address must be >= 0")
    if address > 0xFFFFFF:
        raise ValueError("address must fit in 24 bits (<= 0xFFFFFF)")
    existing = list(load_breakpoints(rom_path))
    next_id = (max((bp.id for bp in existing), default=0) + 1) if existing else 1
    bp = Breakpoint(id=next_id, address=address, label=label)
    existing.append(bp)
    save_breakpoints(rom_path, tuple(existing))
    return bp


def remove_breakpoint(rom_path: Path, breakpoint_id: int) -> Breakpoint:
    """Delete a breakpoint by id; raise KeyError if not present."""
    existing = list(load_breakpoints(rom_path))
    for index, bp in enumerate(existing):
        if bp.id == breakpoint_id:
            existing.pop(index)
            save_breakpoints(rom_path, tuple(existing))
            return bp
    raise KeyError(f"no breakpoint with id={breakpoint_id} for {rom_path}")


def clear_breakpoints(rom_path: Path) -> int:
    """Remove all breakpoints for one ROM; return how many were dropped."""
    existing = load_breakpoints(rom_path)
    save_breakpoints(rom_path, ())
    return len(existing)


def match_event_log_pc(
    breakpoints: tuple[Breakpoint, ...],
    event_log_payload: dict[str, object],
) -> tuple[BreakpointHit, ...]:
    """Return all breakpoint hits in a captured event-log payload.

    A breakpoint fires when `event.pc == breakpoint.address`. Hits
    preserve event order; if two breakpoints share an address (allowed
    by the registry), both fire on every matching event.
    """
    hits: list[BreakpointHit] = []
    events = event_log_payload.get("events") or []
    assert isinstance(events, list)
    # Group breakpoints by address for O(events) matching when the
    # registry is small enough to fit in memory.
    by_address: dict[int, list[Breakpoint]] = {}
    for bp in breakpoints:
        by_address.setdefault(bp.address, []).append(bp)
    for event in events:
        assert isinstance(event, dict)
        pc_raw = event.get("pc")
        if not isinstance(pc_raw, int):
            continue
        bps = by_address.get(pc_raw)
        if not bps:
            continue
        event_index_raw = event.get("index")
        event_index = int(event_index_raw) if isinstance(event_index_raw, int) else -1
        assembly_raw = event.get("assembly")
        status_raw = event.get("status")
        assembly = assembly_raw if isinstance(assembly_raw, str) else None
        status = status_raw if isinstance(status_raw, str) else None
        for bp in bps:
            hits.append(
                BreakpointHit(
                    breakpoint=bp,
                    event_index=event_index,
                    event_pc=pc_raw,
                    assembly=assembly,
                    status=status,
                )
            )
    return tuple(hits)


def _breakpoint_to_payload(bp: Breakpoint) -> dict[str, object]:
    return {
        "id": bp.id,
        "address": bp.address,
        "address_hex": f"0x{bp.address:08X}",
        "label": bp.label,
    }


def _payload_to_breakpoint(item: object) -> Breakpoint:
    assert isinstance(item, dict)
    return Breakpoint(
        id=int(item["id"]),
        address=int(item["address"]),
        label=(str(item["label"]) if item.get("label") is not None else None),
    )


def _validate_schema(raw: object, path: Path) -> None:
    if not isinstance(raw, dict):
        raise ValueError(f"Breakpoint registry at {path} is not a JSON object.")
    if raw.get("format") != BREAKPOINTS_FORMAT:
        raise ValueError(
            f"Unexpected breakpoint format {raw.get('format')!r} at {path}; "
            f"expected {BREAKPOINTS_FORMAT!r}."
        )
    if raw.get("format_version") != BREAKPOINTS_FORMAT_VERSION:
        raise ValueError(
            f"Unknown breakpoint format_version {raw.get('format_version')!r} "
            f"at {path}; this build only understands {BREAKPOINTS_FORMAT_VERSION!r}."
        )
    items = raw.get("breakpoints")
    if not isinstance(items, list):
        raise ValueError(f"Breakpoint registry at {path} has no 'breakpoints' list.")
