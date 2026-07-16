"""Memory watchpoint registry and event-log match helpers (v2).

A watchpoint is a passive observer: it does not change execution.  v2
matches against the `memory_writes` AND `memory_reads` fields of every
event in a captured event-log v2 payload, depending on the watchpoint
kind:

- `kind="write"` — matches against `memory_writes` only
- `kind="read"`  — matches against `memory_reads` only
- `kind="access"` — matches against both

The watchpoint registry lives next to other per-ROM artefacts under
`<rom_dir>/.ngpc_emu/watchpoints/<rom_stem>.watchpoints.json`.

Per `WATCHPOINTS.md` the file format is:

```json
{
  "format": "ngpc-emu-watchpoints",
  "format_version": "2026-05-20.v2",
  "watchpoints": [
    {"id": 1, "kind": "write", "start": 0x4000, "size": 1, "label": "scratch"}
  ]
}
```

Loaders reject unknown `format` / `format_version` strictly, per the
project's "no implicit upgrade" rule. The v1 → v2 bump is purely
additive in semantics (a v1 registry with only `write` kinds parses
identically) but is gated by the strict version check.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

WATCHPOINTS_FORMAT = "ngpc-emu-watchpoints"
WATCHPOINTS_FORMAT_VERSION = "2026-05-20.v3"
WATCHPOINT_KINDS = ("write", "read", "access")


@dataclass(frozen=True)
class Watchpoint:
    """One memory watchpoint.

    `kind` is one of `write` / `read` / `access`.
    `size` is the contiguous byte range starting at `start`.
    `label` is an optional human-readable annotation.
    `value` (added in v3) is an optional byte value filter: when set,
    a hit fires only if the **first byte** of the accessed range
    equals `value`. When `value` is None (default), every range
    overlap fires.
    """

    id: int
    kind: str
    start: int
    size: int
    label: str | None = None
    value: int | None = None

    def end_inclusive(self) -> int:
        return self.start + self.size - 1

    def contains(self, address: int) -> bool:
        return self.start <= address <= self.end_inclusive()

    def overlaps_range(self, address: int, size: int) -> bool:
        """True iff `[address, address+size-1]` overlaps `[start, end]`."""
        if size <= 0:
            return False
        write_end = address + size - 1
        return not (write_end < self.start or address > self.end_inclusive())

    def matches_value(self, data_hex: str) -> bool:
        """True when the value filter passes (or is absent).

        `data_hex` is the access's `data_hex` field (space-separated
        upper-case bytes, little-endian). The filter compares against
        the first byte of the range — the data unit that is most often
        the load/store operand value on a byte-granular access. When
        `self.value is None`, this always returns True.
        """
        if self.value is None:
            return True
        if not data_hex:
            return False
        first_byte_text = data_hex.split(" ", 1)[0]
        try:
            return int(first_byte_text, 16) == (self.value & 0xFF)
        except ValueError:
            return False


@dataclass(frozen=True)
class WatchpointHit:
    """One watchpoint hit recorded against a captured event log.

    `access_kind` is the kind of memory access that triggered the hit
    (`"write"` or `"read"`). A `kind="access"` watchpoint can produce
    hits with either value.
    """

    watchpoint: Watchpoint
    event_index: int
    event_pc: int
    address: int
    size: int
    data_hex: str
    assembly: str | None
    access_kind: str = "write"


def watchpoints_root_for_rom(rom_path: Path) -> Path:
    """Return the default watchpoint directory for one ROM."""
    return rom_path.resolve().parent / ".ngpc_emu" / "watchpoints"


def watchpoints_path_for_rom(rom_path: Path) -> Path:
    """Return the default watchpoint registry path for one ROM."""
    return watchpoints_root_for_rom(rom_path) / f"{rom_path.stem}.watchpoints.json"


def load_watchpoints(rom_path: Path) -> tuple[Watchpoint, ...]:
    """Load the watchpoint registry for one ROM.

    Returns an empty tuple if no registry exists yet.  Raises on
    unknown format / version (no implicit upgrade).
    """
    path = watchpoints_path_for_rom(rom_path)
    if not path.exists():
        return ()
    raw = json.loads(path.read_text(encoding="utf-8"))
    _validate_schema(raw, path)
    return tuple(_payload_to_watchpoint(item) for item in raw["watchpoints"])


def save_watchpoints(rom_path: Path, watchpoints: tuple[Watchpoint, ...]) -> Path:
    """Persist a watchpoint tuple for one ROM and return the file path."""
    path = watchpoints_path_for_rom(rom_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "format": WATCHPOINTS_FORMAT,
        "format_version": WATCHPOINTS_FORMAT_VERSION,
        "watchpoints": [_watchpoint_to_payload(wp) for wp in watchpoints],
    }
    path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    return path


def add_watchpoint(
    rom_path: Path,
    *,
    kind: str = "write",
    start: int,
    size: int = 1,
    label: str | None = None,
    value: int | None = None,
) -> Watchpoint:
    """Append a new watchpoint to the registry and return the saved row."""
    if kind not in WATCHPOINT_KINDS:
        raise ValueError(
            f"kind must be one of {WATCHPOINT_KINDS}; got {kind!r}"
        )
    if size <= 0:
        raise ValueError("size must be >= 1")
    if start < 0:
        raise ValueError("start must be >= 0")
    if value is not None and not (0 <= value <= 0xFF):
        raise ValueError("value must be a byte in 0..255 or None")
    existing = list(load_watchpoints(rom_path))
    next_id = (max((wp.id for wp in existing), default=0) + 1) if existing else 1
    wp = Watchpoint(
        id=next_id, kind=kind, start=start, size=size, label=label, value=value,
    )
    existing.append(wp)
    save_watchpoints(rom_path, tuple(existing))
    return wp


def remove_watchpoint(rom_path: Path, watchpoint_id: int) -> Watchpoint:
    """Delete a watchpoint by id; raise KeyError if not present."""
    existing = list(load_watchpoints(rom_path))
    for index, wp in enumerate(existing):
        if wp.id == watchpoint_id:
            existing.pop(index)
            save_watchpoints(rom_path, tuple(existing))
            return wp
    raise KeyError(f"no watchpoint with id={watchpoint_id} for {rom_path}")


def clear_watchpoints(rom_path: Path) -> int:
    """Remove all watchpoints for one ROM; return how many were dropped."""
    existing = load_watchpoints(rom_path)
    save_watchpoints(rom_path, ())
    return len(existing)


def match_event_log_accesses(
    watchpoints: tuple[Watchpoint, ...],
    event_log_payload: dict[str, object],
) -> tuple[WatchpointHit, ...]:
    """Return all watchpoint hits (write + read) in a captured event-log.

    A `kind="write"` watchpoint matches only `events[].memory_writes`.
    A `kind="read"` watchpoint matches only `events[].memory_reads`.
    A `kind="access"` watchpoint matches both. Hits preserve event order;
    within one event, all writes are scanned before all reads.
    """
    hits: list[WatchpointHit] = []
    events = event_log_payload.get("events") or []
    assert isinstance(events, list)
    for event in events:
        assert isinstance(event, dict)
        event_index_raw = event.get("index")
        event_pc_raw = event.get("pc")
        assembly = event.get("assembly")
        event_index = int(event_index_raw) if isinstance(event_index_raw, int) else -1
        event_pc = int(event_pc_raw) if isinstance(event_pc_raw, int) else 0
        assembly_str = assembly if isinstance(assembly, str) else None

        for access_kind, field_name in (("write", "memory_writes"), ("read", "memory_reads")):
            entries = event.get(field_name) or []
            if not isinstance(entries, list) or not entries:
                continue
            for entry in entries:
                assert isinstance(entry, dict)
                addr_raw = entry.get("address")
                size_raw = entry.get("size")
                data_hex_raw = entry.get("data_hex")
                if not isinstance(addr_raw, int) or not isinstance(size_raw, int):
                    continue
                data_hex = data_hex_raw if isinstance(data_hex_raw, str) else ""
                for wp in watchpoints:
                    if wp.kind == "write" and access_kind != "write":
                        continue
                    if wp.kind == "read" and access_kind != "read":
                        continue
                    # kind="access" matches either; write / read filters above.
                    if not wp.overlaps_range(addr_raw, size_raw):
                        continue
                    # v3: filter by byte value if the watchpoint set one.
                    if not wp.matches_value(data_hex):
                        continue
                    hits.append(
                        WatchpointHit(
                            watchpoint=wp,
                            event_index=event_index,
                            event_pc=event_pc,
                            address=addr_raw,
                            size=size_raw,
                            data_hex=data_hex,
                            assembly=assembly_str,
                            access_kind=access_kind,
                        )
                    )
    return tuple(hits)


def match_event_log_writes(
    watchpoints: tuple[Watchpoint, ...],
    event_log_payload: dict[str, object],
) -> tuple[WatchpointHit, ...]:
    """Backwards-compatible alias: match write-only watchpoints.

    Equivalent to `match_event_log_accesses` filtered to writes.
    Retained for the v1 CLI surface; new callers should use
    `match_event_log_accesses` directly.
    """
    return tuple(
        hit
        for hit in match_event_log_accesses(watchpoints, event_log_payload)
        if hit.access_kind == "write"
    )


def _watchpoint_to_payload(wp: Watchpoint) -> dict[str, object]:
    return {
        "id": wp.id,
        "kind": wp.kind,
        "start": wp.start,
        "start_hex": f"0x{wp.start:06X}",
        "size": wp.size,
        "label": wp.label,
        "value": wp.value,
        "value_hex": None if wp.value is None else f"0x{wp.value:02X}",
    }


def _payload_to_watchpoint(item: object) -> Watchpoint:
    assert isinstance(item, dict)
    value_raw = item.get("value")
    value = int(value_raw) if isinstance(value_raw, int) else None
    return Watchpoint(
        id=int(item["id"]),
        kind=str(item["kind"]),
        start=int(item["start"]),
        size=int(item["size"]),
        label=(str(item["label"]) if item.get("label") is not None else None),
        value=value,
    )


def _validate_schema(raw: object, path: Path) -> None:
    if not isinstance(raw, dict):
        raise ValueError(f"Watchpoint registry at {path} is not a JSON object.")
    if raw.get("format") != WATCHPOINTS_FORMAT:
        raise ValueError(
            f"Unexpected watchpoint format {raw.get('format')!r} at {path}; "
            f"expected {WATCHPOINTS_FORMAT!r}."
        )
    if raw.get("format_version") != WATCHPOINTS_FORMAT_VERSION:
        raise ValueError(
            f"Unknown watchpoint format_version {raw.get('format_version')!r} "
            f"at {path}; this build only understands {WATCHPOINTS_FORMAT_VERSION!r}."
        )
    items = raw.get("watchpoints")
    if not isinstance(items, list):
        raise ValueError(f"Watchpoint registry at {path} has no 'watchpoints' list.")
