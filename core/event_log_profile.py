"""Per-symbol bucketing of an event-log payload.

Reads an event-log v1 payload + a `.map` symbol table and produces a
profile: how many events fell inside each owning symbol, how many ran to
completion vs halted, the address range observed inside each symbol, etc.

This is the first dynamic profile primitive: it bridges the captured
execution traces (event log) and the static debugger symbols. Once the
emulator can run deep enough to reach gameplay code, the same primitive
gives instruction-count-per-function which is what the toolchain needs
for B3 profile-first on `shmup_update`.
"""

from __future__ import annotations

from typing import Any

from core.symbols import SymbolTable


EVENT_LOG_PROFILE_FORMAT = "ngpc-emu-event-log-profile"
EVENT_LOG_PROFILE_VERSION = "2026-05-19.v1"


def bucket_event_log_by_symbol(
    event_log_payload: dict[str, Any],
    symbol_table: SymbolTable,
) -> dict[str, Any]:
    """Bucketize event-log events by the symbol that owns each PC.

    Returns a JSON-ready dict with stable shape:
      - format/version markers
      - map_source (path of the .map file)
      - rom_sha256 (forwarded from the event log; useful when correlating
        a profile back to a specific build)
      - total counters (events seen, events resolved, events unresolved)
      - per-symbol buckets, sorted by descending total_events
      - per-status counters per bucket (executed / halted / other)
      - first / last PC observed inside the symbol (useful to locate
        the hot region inside a long function)

    Events with no resolvable owning symbol are collected separately
    into an `unresolved_events` integer. The function does NOT mutate
    the input event log; it is a read-only analysis primitive.
    """
    events = event_log_payload.get("events") or []
    buckets: dict[str, dict[str, Any]] = {}
    unresolved = 0
    halted_statuses: dict[str, int] = {}

    for event in events:
        if not isinstance(event, dict):
            continue
        pc = event.get("pc")
        if not isinstance(pc, int):
            continue
        sym = symbol_table.lookup_address(pc)
        if sym is None:
            unresolved += 1
            continue
        bucket = buckets.get(sym.name)
        if bucket is None:
            bucket = {
                "symbol": sym.name,
                "symbol_address": sym.address,
                "symbol_address_hex": f"0x{sym.address:08X}",
                "section": sym.section,
                "total_events": 0,
                "executed_events": 0,
                "halted_events": 0,
                "first_pc": pc,
                "first_pc_hex": f"0x{pc:08X}",
                "last_pc": pc,
                "last_pc_hex": f"0x{pc:08X}",
                "min_offset": pc - sym.address,
                "max_offset": pc - sym.address,
            }
            buckets[sym.name] = bucket
        bucket["total_events"] += 1
        status = event.get("status")
        if status == "executed":
            bucket["executed_events"] += 1
        else:
            bucket["halted_events"] += 1
            if isinstance(status, str):
                halted_statuses[status] = halted_statuses.get(status, 0) + 1
        offset = pc - sym.address
        if offset < bucket["min_offset"]:
            bucket["min_offset"] = offset
        if offset > bucket["max_offset"]:
            bucket["max_offset"] = offset
        bucket["last_pc"] = pc
        bucket["last_pc_hex"] = f"0x{pc:08X}"

    sorted_buckets = sorted(
        buckets.values(),
        key=lambda b: (-b["total_events"], b["symbol_address"]),
    )

    rom = event_log_payload.get("rom") if isinstance(event_log_payload, dict) else None
    rom_sha = rom.get("sha256") if isinstance(rom, dict) else None
    summary = event_log_payload.get("summary") if isinstance(event_log_payload, dict) else None
    final_pc = None
    if isinstance(summary, dict):
        final_pc = summary.get("final_cpu_pc")

    return {
        "format": EVENT_LOG_PROFILE_FORMAT,
        "format_version": EVENT_LOG_PROFILE_VERSION,
        "map_source": symbol_table.source_path,
        "rom_sha256": rom_sha,
        "final_cpu_pc": final_pc,
        "final_cpu_pc_hex": (
            f"0x{final_pc:08X}" if isinstance(final_pc, int) else None
        ),
        "total_events": len(events),
        "resolved_events": sum(b["total_events"] for b in buckets.values()),
        "unresolved_events": unresolved,
        "distinct_symbols": len(buckets),
        "halted_status_breakdown": dict(
            sorted(halted_statuses.items(), key=lambda kv: -kv[1])
        ),
        "buckets": sorted_buckets,
        "note": (
            "Per-symbol bucketing of an event-log v1 payload. Each event "
            "is assigned to the symbol with the highest address <= its PC. "
            "Buckets are sorted by total event count, descending. "
            "executed_events vs halted_events distinguishes instructions "
            "the emulator ran from those that hit an honest stop (e.g. "
            "silicon-broken opcode, unknown opcode). first_pc / last_pc "
            "and min_offset / max_offset locate the hot region inside "
            "long symbols. This is a static read-only analysis: the input "
            "event log is not modified."
        ),
    }
