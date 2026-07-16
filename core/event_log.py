"""Stable event-log v1 capture, load, inspect, and diff helpers."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

from core.cpu import NgpcCpuState
from core.execute import build_execute_next, seed_cpu_state_for_execution
from core.fetch import NgpcFetchView, load_fetch_view
from core.quirks import KnownQuirkMatch, load_known_quirk_database
from core.rom import NgpcRomHeader
from core.savestate import compute_rom_sha256

EVENT_LOG_FORMAT = "ngpc-emu-event-log"
EVENT_LOG_FORMAT_VERSION = "2026-05-20.v2"
_POST_STATE_TERMINAL_STATUSES = {"cpu-halted"}


def build_event_log_payload(
    *,
    rom_path: Path,
    rom_header: NgpcRomHeader,
    view: NgpcFetchView,
    start_pc: int | None = None,
    target_pc: int | None = None,
    max_steps: int = 8,
    cpu_state: NgpcCpuState | None = None,
    memory_bytes: dict[int, int] | None = None,
    seed_registers: dict[str, int] | None = None,
    seed_xsp: int | None = None,
    seed_from_savestate: dict[str, object] | None = None,
    auto_tick_address: int | None = None,
    auto_tick_period: int = 256,
    note: str | None = None,
    created_at_utc: str | None = None,
) -> dict[str, object]:
    """Capture one event-log payload using the current execution subset."""
    if max_steps <= 0:
        raise ValueError("max_steps must be >= 1")
    if auto_tick_period <= 0:
        raise ValueError("auto_tick_period must be >= 1")

    current_cpu = view.machine.cpu if cpu_state is None else cpu_state
    if seed_xsp is not None or seed_registers:
        current_cpu = seed_cpu_state_for_execution(
            current_cpu,
            register_values=seed_registers,
            seed_xsp=seed_xsp,
        )
    if start_pc is not None:
        current_cpu = replace(current_cpu, pc=start_pc)

    current_memory = {} if memory_bytes is None else dict(memory_bytes)
    actual_start_pc = current_cpu.pc
    created = created_at_utc or datetime.now(timezone.utc).isoformat()
    quirk_db = load_known_quirk_database()

    events: list[dict[str, object]] = []
    executed_count = 0
    stop_reason = "step-budget-exhausted"
    matched_quirk_on_stop: dict[str, object] | None = None

    for index in range(max_steps):
        if target_pc is not None and current_cpu.pc == target_pc:
            stop_reason = "target-reached"
            break

        execution = build_execute_next(
            view=view,
            cpu_state=current_cpu,
            memory_bytes=current_memory,
        )
        events.append(_execution_to_event(index=index, execution=execution))

        if execution.status in _POST_STATE_TERMINAL_STATUSES:
            assert execution.after_cpu is not None
            assert execution.after_memory is not None
            current_cpu = execution.after_cpu
            current_memory = execution.after_memory
            stop_reason = f"stopped-on-{execution.status}"
            matched_quirk_on_stop = _known_quirk_to_dict(execution.matched_quirk)
            break

        if execution.status != "executed" or execution.after_cpu is None or execution.after_memory is None:
            stop_reason = f"stopped-on-{execution.status}"
            matched_quirk_on_stop = _known_quirk_to_dict(execution.matched_quirk)
            break

        executed_count += 1
        current_cpu = execution.after_cpu
        current_memory = execution.after_memory
        if auto_tick_address is not None and executed_count % auto_tick_period == 0:
            tick_addr = auto_tick_address & 0xFFFFFF
            prev = current_memory.get(tick_addr, 0)
            current_memory[tick_addr] = (prev + 1) & 0xFF

    return {
        "format": EVENT_LOG_FORMAT,
        "format_version": EVENT_LOG_FORMAT_VERSION,
        "created_at_utc": created,
        "emulator": {
            "project": "NgpCraft_emulator",
            "prototype": "python",
            "commit": None,
        },
        "rom": {
            "path_when_saved": str(rom_path),
            "file_size": rom_header.file_size,
            "sha256": compute_rom_sha256(rom_path),
            "header_title": rom_header.title,
            "header_entry_point": rom_header.entry_point,
            "header_mode_raw": rom_header.mode_raw,
        },
        "quirks": {
            "database_version": quirk_db.database_version,
        },
        "run_context": {
            "start_pc": actual_start_pc,
            "start_pc_hex": f"0x{actual_start_pc:08X}",
            "target_pc": target_pc,
            "target_pc_hex": None if target_pc is None else f"0x{target_pc:08X}",
            "max_steps": max_steps,
            "seed_registers": _seed_registers_dict(seed_registers),
            "seed_xsp": seed_xsp,
            "seed_from_savestate": seed_from_savestate,
            "auto_tick_address": auto_tick_address,
            "auto_tick_address_hex": (
                None if auto_tick_address is None else f"0x{auto_tick_address & 0xFFFFFF:06X}"
            ),
            "auto_tick_period": auto_tick_period if auto_tick_address is not None else None,
        },
        "events": events,
        "summary": {
            "executed_count": executed_count,
            "emitted_count": len(events),
            "stop_reason": stop_reason,
            "final_cpu_pc": current_cpu.pc,
            "final_cpu_pc_hex": f"0x{current_cpu.pc:08X}",
            "matched_quirk_on_stop": matched_quirk_on_stop,
        },
        "note": note,
    }


def capture_event_log(
    rom_path: str | Path,
    *,
    output_path: Path,
    start_pc: int | None = None,
    target_pc: int | None = None,
    max_steps: int = 8,
    seed_registers: dict[str, int] | None = None,
    seed_xsp: int | None = None,
    initial_cpu_state: NgpcCpuState | None = None,
    initial_memory_bytes: dict[int, int] | None = None,
    seed_from_savestate: dict[str, object] | None = None,
    auto_tick_address: int | None = None,
    auto_tick_period: int = 256,
    note: str | None = None,
) -> dict[str, object]:
    """Load one ROM, capture an event log, and save it as UTF-8 JSON."""
    rom = Path(rom_path)
    view = load_fetch_view(rom)
    payload = build_event_log_payload(
        rom_path=rom,
        rom_header=view.machine.header,
        view=view,
        start_pc=start_pc,
        target_pc=target_pc,
        max_steps=max_steps,
        cpu_state=initial_cpu_state,
        memory_bytes=initial_memory_bytes,
        seed_registers=seed_registers,
        seed_xsp=seed_xsp,
        seed_from_savestate=seed_from_savestate,
        auto_tick_address=auto_tick_address,
        auto_tick_period=auto_tick_period,
        note=note,
    )
    save_event_log(output_path, payload)
    return payload


def save_event_log(path: Path, payload: dict[str, object]) -> None:
    """Write one event-log payload to disk as UTF-8 JSON."""
    path.write_text(
        json.dumps(payload, indent=2, sort_keys=True),
        encoding="utf-8",
    )


def load_event_log(
    path: Path,
    *,
    expected_rom_path: Path | None = None,
) -> dict[str, object]:
    """Load and validate one event-log payload."""
    raw = json.loads(path.read_text(encoding="utf-8"))
    _validate_event_log_schema(raw, path)

    rom_section = raw["rom"]
    assert isinstance(rom_section, dict)
    if expected_rom_path is not None:
        actual_hash = compute_rom_sha256(expected_rom_path)
        expected_hash = rom_section["sha256"]
        if actual_hash != expected_hash:
            raise ValueError(
                f"ROM hash mismatch: event log at {path} was captured against "
                f"sha256 {expected_hash} but {expected_rom_path} is sha256 "
                f"{actual_hash}"
            )

    return raw


def diff_event_logs(
    left: dict[str, object],
    right: dict[str, object],
) -> dict[str, object]:
    """Return a first-divergence diff between two loaded event logs."""
    left_rom = left["rom"]
    right_rom = right["rom"]
    assert isinstance(left_rom, dict)
    assert isinstance(right_rom, dict)
    left_hash = left_rom["sha256"]
    right_hash = right_rom["sha256"]
    if left_hash != right_hash:
        raise ValueError(
            "event logs were captured against different ROM hashes and cannot be "
            "diffed honestly"
        )

    left_run = left["run_context"]
    right_run = right["run_context"]
    assert isinstance(left_run, dict)
    assert isinstance(right_run, dict)
    if left_run != right_run:
        return {
            "rom_sha256": left_hash,
            "left_format_version": left["format_version"],
            "right_format_version": right["format_version"],
            "first_divergence": {
                "kind": "run_context",
                "field": "run_context",
                "left": left_run,
                "right": right_run,
            },
            "left_summary": left["summary"],
            "right_summary": right["summary"],
            "note": "The runs were parameterized differently before any event-level compare.",
        }

    left_events = left["events"]
    right_events = right["events"]
    assert isinstance(left_events, list)
    assert isinstance(right_events, list)

    for index, (left_event, right_event) in enumerate(zip(left_events, right_events)):
        if left_event != right_event:
            return {
                "rom_sha256": left_hash,
                "left_format_version": left["format_version"],
                "right_format_version": right["format_version"],
                "first_divergence": {
                    "kind": "event",
                    "index": index,
                    "left": left_event,
                    "right": right_event,
                },
                "left_summary": left["summary"],
                "right_summary": right["summary"],
                "note": "The logs share the same ROM hash and run context but diverge on one event.",
            }

    if len(left_events) != len(right_events):
        return {
            "rom_sha256": left_hash,
            "left_format_version": left["format_version"],
            "right_format_version": right["format_version"],
            "first_divergence": {
                "kind": "length",
                "index": min(len(left_events), len(right_events)),
                "left_event_count": len(left_events),
                "right_event_count": len(right_events),
            },
            "left_summary": left["summary"],
            "right_summary": right["summary"],
            "note": "The shared prefix is identical but one log contains more events.",
        }

    return {
        "rom_sha256": left_hash,
        "left_format_version": left["format_version"],
        "right_format_version": right["format_version"],
        "first_divergence": None,
        "left_summary": left["summary"],
        "right_summary": right["summary"],
        "note": "No event-level divergence was found; the logs are identical for the compared fields.",
    }


def _validate_event_log_schema(raw: object, path: Path) -> None:
    if not isinstance(raw, dict):
        raise ValueError(f"Event log at {path} is not a JSON object.")
    if raw.get("format") != EVENT_LOG_FORMAT:
        raise ValueError(
            f"Unexpected event log format {raw.get('format')!r} at {path}; "
            f"expected {EVENT_LOG_FORMAT!r}."
        )
    if raw.get("format_version") != EVENT_LOG_FORMAT_VERSION:
        raise ValueError(
            f"Unknown event log format_version {raw.get('format_version')!r} "
            f"at {path}; this build only understands "
            f"{EVENT_LOG_FORMAT_VERSION!r}."
        )
    for required in ("rom", "run_context", "events", "summary"):
        if required not in raw:
            raise ValueError(
                f"Event log at {path} is missing required field {required!r}."
            )


def _execution_to_event(index: int, execution: object) -> dict[str, object]:
    from core.execute import ExecutionResult

    assert isinstance(execution, ExecutionResult)
    next_pc = None if execution.after_cpu is None else execution.after_cpu.pc
    return {
        "index": index,
        "event_type": "instruction-step",
        "pc": execution.decode.pc,
        "pc_hex": f"0x{execution.decode.pc:08X}",
        "raw_bytes_hex": (
            None
            if execution.decode.raw_bytes is None
            else execution.decode.raw_bytes.hex(" ").upper()
        ),
        "assembly": execution.decode.assembly,
        "length": execution.decode.length,
        "status": execution.status,
        "next_pc": next_pc,
        "next_pc_hex": None if next_pc is None else f"0x{next_pc:08X}",
        "written_registers": list(execution.written_registers),
        "memory_writes": [
            {
                "address": write.address,
                "address_hex": f"0x{write.address:06X}",
                "size": len(write.data),
                "data_hex": write.data.hex(" ").upper(),
                "note": write.note,
            }
            for write in execution.memory_writes
        ],
        "memory_reads": [
            {
                "address": read.address,
                "address_hex": f"0x{read.address:06X}",
                "size": len(read.data),
                "data_hex": read.data.hex(" ").upper(),
                "note": read.note,
            }
            for read in execution.memory_reads
        ],
        "flag_changes": _flag_changes(execution),
        "matched_quirk": _known_quirk_to_dict(execution.matched_quirk),
        "note": execution.note,
    }


def _flag_changes(execution: object) -> list[dict[str, object]]:
    from core.execute import ExecutionResult

    assert isinstance(execution, ExecutionResult)
    if execution.after_cpu is None:
        return []

    changes: list[dict[str, object]] = []
    for key, label in (
        ("sf", "S"),
        ("zf", "Z"),
        ("vf", "V"),
        ("hf", "H"),
        ("cf", "C"),
    ):
        before_value = getattr(execution.before_cpu.flags, key)
        after_value = getattr(execution.after_cpu.flags, key)
        if before_value != after_value:
            changes.append(
                {
                    "name": label,
                    "before": before_value,
                    "after": after_value,
                }
            )
    return changes


def _known_quirk_to_dict(quirk: KnownQuirkMatch | None) -> dict[str, object] | None:
    if quirk is None:
        return None
    return {
        "database_version": quirk.database_version,
        "quirk_id": quirk.quirk_id,
        "category": quirk.category,
        "confidence": quirk.confidence,
        "summary": quirk.summary,
        "note": quirk.note,
        "sources": [
            {
                "document": source.document,
                "section": source.section,
                "quote": source.quote,
            }
            for source in quirk.sources
        ],
    }


def _seed_registers_dict(seed_registers: dict[str, int] | None) -> dict[str, int] | None:
    if not seed_registers:
        return None
    return {name: value for name, value in sorted(seed_registers.items())}
