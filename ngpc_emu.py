"""Minimal headless entry point for NgpCraft Emulator."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from core.bus import AddressProbe, load_address_space
from core.checkpoints import (
    checkpoint_path_for_rom,
    delete_named_checkpoint,
    list_named_checkpoints,
    load_named_checkpoint,
    save_named_checkpoint,
)
from core.cpu import NgpcCpuState, encode_f_from_flags
from core.decode import (
    CONTROL_REGISTER_NAMES,
    DecodeResult,
    control_register_name,
    decode_instruction_at,
    decode_next_instruction,
)
from core.engine_bridge import (
    EngineBridgeError,
    build_error_response,
    execute_engine_bridge_request,
)
from core.event_log import (
    EVENT_LOG_FORMAT_VERSION,
    build_event_log_payload,
    diff_event_logs,
    load_event_log,
    save_event_log,
)
from core.event_log_profile import (
    EVENT_LOG_PROFILE_VERSION,
    bucket_event_log_by_symbol,
)
from core.execute import ExecutionResult, load_execute_next, seed_cpu_state_for_execution
from core.fetch import FetchResult, fetch_next_bytes, load_fetch_view
from core.goldens import (
    delete_named_golden,
    golden_path_for_rom,
    list_named_goldens,
    load_named_golden,
    save_named_golden,
)
from core.machine import NgpcMachineState, load_machine_state
from core.memory import MemoryReadResult, load_read_bus
from core.quirks import match_known_quirk, match_known_silicon_broken
from core.rom import NgpcRomHeader, load_rom_header
from core.run_steps import RunStepsResult, RunUntilResult, load_run_steps, load_run_until
from core.seed_presets import (
    BIOS_HANDOFF_XSP,
    bios_handoff_minimal_seed_registers,
)
from core.k2ge import (
    CHAR_RAM_TILE_COUNT,
    CHAR_RAM_TILE_HEIGHT,
    CHAR_RAM_TILE_WIDTH,
    K2GE_CHAR_RAM_BASE,
    K2GE_OAM_BASE,
    K2GE_OAM_PALETTE_CODES_BASE,
    K2GE_PALETTE_SCR1_BASE,
    K2GE_PALETTE_SCR2_BASE,
    K2GE_PALETTE_SPRITE_BASE,
    K2GE_SCR1_TILEMAP_BASE,
    K2GE_SCR2_TILEMAP_BASE,
    TILEMAP_TILES_PER_COL,
    TILEMAP_TILES_PER_ROW,
    K2geControlRegisters,
    K2gePalette,
    K2geSprite,
    K2geTilemapEntry,
    K2geTilePixels,
    read_all_palettes,
    read_control_registers,
    read_oam_sprites,
    read_plane_palettes,
    read_tile,
    read_tilemap,
)
from core.renderer import (
    NGPC_SCREEN_HEIGHT,
    NGPC_SCREEN_WIDTH,
    RenderedFrame,
    frame_to_ppm_bytes,
    pixels_to_ppm_bytes,
    render_frame,
)
from core.atlas import render_tile_atlas
from core.frame_timing import (
    CYCLES_PER_SCANLINE,
    ESTIMATED_CYCLES_PER_INSTRUCTION,
    FRAMES_PER_SECOND,
    IRQ_LEVEL_VBLANK,
    SCANLINES_PER_FRAME,
    VBLANK_SCANLINES,
    VBLANK_VECTOR_ADDRESS,
    VISIBLE_SCANLINES,
    FrameState,
    IrqState,
    advance_frame_state_by_cycles,
    advance_frames,
    advance_scanlines,
    detect_vblank_transitions,
    fold_vblank_irq_pending,
    initial_frame_state,
    initial_irq_state,
)
from core.frame_diff import diff_ppm_bytes
from core.frame_goldens import (
    build_frame_golden_manifest,
    delete_frame_golden,
    list_frame_goldens,
    load_frame_golden,
    save_frame_golden,
)
from core.savestate import (
    SAVESTATE_FORMAT_VERSION,
    SavestateDocument,
    build_savestate_payload,
    load_savestate,
    save_savestate,
)
from core.symbols import SymbolTable, load_map
from core.sessions import (
    SESSION_FORMAT_VERSION,
    delete_named_session,
    delete_named_session_snapshot,
    list_named_sessions,
    list_named_session_snapshots,
    load_named_session,
    load_named_session_snapshot,
    managed_checkpoint_name_for_session,
    managed_snapshot_checkpoint_name_for_session,
    restore_named_session_snapshot,
    save_named_session,
    save_named_session_snapshot,
    session_checkpoint_path_for_rom,
    session_path_for_rom,
)
from core.trace_exec import ExecutionTraceResult, load_execution_trace
from core.run_until import RunUntilPreview, load_run_until_preview
from core.watchpoints import (
    WATCHPOINT_KINDS,
    WATCHPOINTS_FORMAT_VERSION,
    Watchpoint,
    WatchpointHit,
    add_watchpoint,
    clear_watchpoints,
    load_watchpoints,
    match_event_log_accesses,
    remove_watchpoint,
    watchpoints_path_for_rom,
)
from core.breakpoints import (
    BREAKPOINTS_FORMAT_VERSION,
    Breakpoint,
    BreakpointHit,
    add_breakpoint,
    breakpoints_path_for_rom,
    clear_breakpoints,
    load_breakpoints,
    match_event_log_pc,
    remove_breakpoint,
)
from core.step import StepPreview, load_step_preview
from core.trace import TracePreview, load_trace_preview


def _load_seed_savestate_from_args(
    *,
    rom_path: Path,
    seed_from: str | None,
    seed_checkpoint: str | None,
    seed_session: str | None,
) -> tuple[SavestateDocument | None, dict[str, object] | None]:
    """Resolve one seed savestate source from CLI args."""
    chosen_count = sum(
        1
        for value in (seed_from, seed_checkpoint, seed_session)
        if value is not None
    )
    if chosen_count > 1:
        raise ValueError(
            "use only one of --seed-from, --seed-checkpoint, or --seed-session"
        )
    if seed_from is not None:
        doc = load_savestate(Path(seed_from), expected_rom_path=rom_path)
        return doc, {
            "path": seed_from,
            "format_version": doc.format_version,
            "rom_sha256": doc.rom_sha256,
            "overlay_byte_count": len(doc.writable_overlay),
            "cpu_pc_hex": f"0x{doc.cpu.pc:08X}",
            "kind": "savestate-file",
        }
    if seed_checkpoint is not None:
        checkpoint = load_named_checkpoint(rom_path, seed_checkpoint)
        doc = checkpoint.document
        return doc, {
            "name": seed_checkpoint,
            "path": str(checkpoint.path),
            "format_version": doc.format_version,
            "rom_sha256": doc.rom_sha256,
            "overlay_byte_count": len(doc.writable_overlay),
            "cpu_pc_hex": f"0x{doc.cpu.pc:08X}",
            "kind": "named-checkpoint",
        }
    if seed_session is not None:
        session = load_named_session(rom_path, seed_session)
        doc = session.document
        return doc, {
            "name": session.name,
            "path": str(session.path),
            "checkpoint_name": session.current_checkpoint_name,
            "checkpoint_path": str(session.current_checkpoint_path),
            "format_version": doc.format_version,
            "rom_sha256": doc.rom_sha256,
            "overlay_byte_count": len(doc.writable_overlay),
            "cpu_pc_hex": f"0x{doc.cpu.pc:08X}",
            "kind": "named-session",
        }
    return None, None


def _resolve_save_state_output(
    *,
    rom_path: Path,
    save_state: str | None,
    save_checkpoint: str | None,
    save_session: str | None,
) -> tuple[Path | None, dict[str, object] | None]:
    """Resolve one save target path from CLI args."""
    chosen_count = sum(
        1
        for value in (save_state, save_checkpoint, save_session)
        if value is not None
    )
    if chosen_count > 1:
        raise ValueError(
            "use only one of --save-state, --save-checkpoint, or --save-session"
        )
    if save_state is not None:
        return Path(save_state), {"kind": "savestate-file", "path": save_state}
    if save_checkpoint is not None:
        path = checkpoint_path_for_rom(rom_path, save_checkpoint)
        return path, {
            "kind": "named-checkpoint",
            "name": save_checkpoint,
            "path": str(path),
        }
    if save_session is not None:
        checkpoint_name = managed_checkpoint_name_for_session(save_session)
        checkpoint_path = session_checkpoint_path_for_rom(rom_path, save_session)
        session_path = session_path_for_rom(rom_path, save_session)
        return checkpoint_path, {
            "kind": "named-session",
            "name": save_session,
            "checkpoint_name": checkpoint_name,
            "path": str(checkpoint_path),
            "session_path": str(session_path),
        }
    return None, None


def _seed_source_label(payload: dict[str, object]) -> str:
    """Render one short human label for a seed source payload."""
    kind = payload.get("kind")
    if kind == "savestate-file":
        return str(payload["path"])
    if kind in {"named-checkpoint", "named-session"}:
        return str(payload["name"])
    return "<unknown>"


def _finalize_session_frontier_save(
    *,
    rom_path: Path,
    save_output_payload: dict[str, object],
    last_action: str,
    user_note: str | None,
) -> None:
    """Persist session metadata after its managed checkpoint frontier was updated."""
    if save_output_payload.get("kind") != "named-session":
        return
    save_named_session(
        rom_path,
        str(save_output_payload["name"]),
        current_checkpoint_name=str(save_output_payload["checkpoint_name"]),
        last_action=last_action,
        note=user_note,
    )


def _header_to_dict(header: NgpcRomHeader) -> dict[str, object]:
    return {
        "path": str(header.path),
        "file_size": header.file_size,
        "copyright_text": header.copyright_text,
        "entry_point": header.entry_point,
        "entry_point_hex": f"0x{header.entry_point:08X}",
        "game_id_raw": header.game_id_raw,
        "game_id_bcd": header.game_id_bcd,
        "version": header.version,
        "mode_raw": header.mode_raw,
        "mode_raw_hex": f"0x{header.mode_raw:02X}",
        "mode_name": header.mode_name,
        "title": header.title,
    }


def _cmd_info(args: argparse.Namespace) -> int:
    header = load_rom_header(Path(args.rom))
    payload = _header_to_dict(header)
    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0

    print(f"ROM: {payload['path']}")
    print(f"Size: {payload['file_size']} bytes")
    print(f"Title: {payload['title'] or '<empty>'}")
    print(f"Copyright: {payload['copyright_text'] or '<empty>'}")
    print(f"Entry point: {payload['entry_point_hex']}")
    print(f"Game ID: {payload['game_id_bcd']}")
    print(f"Version: {payload['version']}")
    print(f"Mode: {payload['mode_name']} ({payload['mode_raw_hex']})")
    return 0


def _machine_to_dict(state: NgpcMachineState) -> dict[str, object]:
    return {
        "rom_path": str(state.rom_path),
        "model_status": state.model_status,
        "note": state.note,
        "header": _header_to_dict(state.header),
        "cpu": {
            "pc": state.cpu.pc,
            "pc_hex": f"0x{state.cpu.pc:08X}",
            "sr_raw": state.cpu.sr_raw,
            "sr_raw_hex": None if state.cpu.sr_raw is None else f"0x{state.cpu.sr_raw:04X}",
            "register_bank": state.cpu.register_bank,
            "modeled_fields": list(state.cpu.modeled_fields),
        "flags": {
            "sf": state.cpu.flags.sf,
            "zf": state.cpu.flags.zf,
            "vf": state.cpu.flags.vf,
            "hf": state.cpu.flags.hf,
            "cf": state.cpu.flags.cf,
            "nf": state.cpu.flags.nf,
        },
        "alt_flags": {
            "sf": None if state.cpu.alt_flags is None else state.cpu.alt_flags.sf,
            "zf": None if state.cpu.alt_flags is None else state.cpu.alt_flags.zf,
            "vf": None if state.cpu.alt_flags is None else state.cpu.alt_flags.vf,
            "hf": None if state.cpu.alt_flags is None else state.cpu.alt_flags.hf,
            "cf": None if state.cpu.alt_flags is None else state.cpu.alt_flags.cf,
            "nf": None if state.cpu.alt_flags is None else state.cpu.alt_flags.nf,
        },
            "registers": {
                "xwa": state.cpu.regs.xwa,
                "xbc": state.cpu.regs.xbc,
                "xde": state.cpu.regs.xde,
                "xhl": state.cpu.regs.xhl,
                "xix": state.cpu.regs.xix,
                "xiy": state.cpu.regs.xiy,
                "xiz": state.cpu.regs.xiz,
                "xsp": state.cpu.regs.xsp,
            },
            "note": state.cpu.note,
        },
        "memory_regions": [
            {
                "name": region.name,
                "start": region.start,
                "start_hex": f"0x{region.start:06X}",
                "end": region.end,
                "end_hex": f"0x{region.end:06X}",
                "size": region.size,
                "note": region.note,
            }
            for region in state.memory_regions
        ],
    }


def _cmd_reset_info(args: argparse.Namespace) -> int:
    state = load_machine_state(Path(args.rom))
    payload = _machine_to_dict(state)
    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0

    print(f"ROM: {payload['rom_path']}")
    print(f"Model status: {payload['model_status']}")
    print(f"Initial PC: {payload['cpu']['pc_hex']}")
    print(
        "CPU fields modeled: "
        + ", ".join(payload["cpu"]["modeled_fields"])  # type: ignore[index]
    )
    print(f"CPU note: {payload['cpu']['note']}")
    print("Memory regions:")
    for region in payload["memory_regions"]:  # type: ignore[index]
        print(
            f"  - {region['name']}: {region['start_hex']}..{region['end_hex']} "
            f"({region['size']} bytes)"
        )
    print(f"State note: {payload['note']}")
    return 0


def _parse_address(text: str) -> int:
    value = int(text, 0)
    if value < 0:
        raise ValueError("address must be non-negative")
    return value


def _parse_auto_tick_args(
    args: argparse.Namespace,
) -> tuple[int | None, dict[str, object] | None]:
    raw_address = getattr(args, "auto_tick_addr", None)
    if raw_address is None:
        return None, None
    address = _parse_address(raw_address) & 0xFFFFFF
    period = int(getattr(args, "auto_tick_period"))
    return address, {
        "kind": "auto-tick-byte-counter",
        "address": address,
        "address_hex": f"0x{address:06X}",
        "period": period,
    }


def _format_auto_tick_note_suffix(auto_tick_payload: dict[str, object] | None) -> str | None:
    if auto_tick_payload is None:
        return None
    return (
        "non-reference auto-tick "
        f"addr={auto_tick_payload['address_hex']} "
        f"period={auto_tick_payload['period']}"
    )


def _print_auto_tick_summary(auto_tick_payload: dict[str, object] | None) -> None:
    if auto_tick_payload is None:
        return
    print(
        "Non-reference mode: auto-tick byte counter "
        f"{auto_tick_payload['address_hex']} every "
        f"{auto_tick_payload['period']} executed instruction(s)"
    )


def _probe_to_dict(probe: AddressProbe) -> dict[str, object]:
    payload: dict[str, object] = {
        "address": probe.address,
        "address_hex": f"0x{probe.address:06X}",
        "status": probe.status,
        "note": probe.note,
        "region_offset": probe.region_offset,
        "region_offset_hex": (
            None if probe.region_offset is None else f"0x{probe.region_offset:X}"
        ),
        "file_offset": probe.file_offset,
        "file_offset_hex": None if probe.file_offset is None else f"0x{probe.file_offset:X}",
    }
    if probe.region is None:
        payload["region"] = None
        return payload
    payload["region"] = {
        "name": probe.region.name,
        "kind": probe.region.kind,
        "start": probe.region.start,
        "start_hex": f"0x{probe.region.start:06X}",
        "end": probe.region.end,
        "end_hex": f"0x{probe.region.end:06X}",
        "size": probe.region.size,
        "note": probe.region.note,
    }
    return payload


def _cmd_addr_info(args: argparse.Namespace) -> int:
    address_space = load_address_space(Path(args.rom))
    probe = address_space.probe(_parse_address(args.address))
    payload = _probe_to_dict(probe)
    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0

    print(f"ROM: {address_space.rom_path}")
    print(f"Address: {payload['address_hex']}")
    print(f"Status: {payload['status']}")
    region = payload["region"]
    if region is None:
        print("Region: <unmapped>")
    else:
        print(f"Region: {region['name']} [{region['kind']}]")
        print(f"Range: {region['start_hex']}..{region['end_hex']}")
        print(f"Region offset: {payload['region_offset_hex']}")
        if payload["file_offset_hex"] is not None:
            print(f"ROM file offset: {payload['file_offset_hex']}")
    print(f"Note: {payload['note']}")
    return 0


def _read_to_dict(result: MemoryReadResult) -> dict[str, object]:
    payload: dict[str, object] = {
        "address": result.address,
        "address_hex": f"0x{result.address:06X}",
        "width": result.width,
        "status": result.status,
        "note": result.note,
        "probe": _probe_to_dict(result.probe),
        "data": None if result.data is None else list(result.data),
        "data_hex": None if result.data is None else result.data.hex(" ").upper(),
    }
    return payload


def _cmd_peek(args: argparse.Namespace) -> int:
    bus = load_read_bus(Path(args.rom), bios_path=args.bios)
    result = bus.read_bytes(_parse_address(args.address), size=args.count)
    payload = _read_to_dict(result)
    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0

    print(f"ROM: {bus.rom.path}")
    print(f"Address: {payload['address_hex']}")
    print(f"Width: {payload['width']}")
    print(f"Status: {payload['status']}")
    if payload["data_hex"] is not None:
        print(f"Data: {payload['data_hex']}")
    region = payload["probe"]["region"]  # type: ignore[index]
    if region is None:
        print("Region: <unmapped>")
    else:
        print(f"Region: {region['name']} [{region['kind']}]")
        print(f"Range: {region['start_hex']}..{region['end_hex']}")
    print(f"Note: {payload['note']}")
    return 0


def _ascii_glyph(byte: int) -> str:
    return chr(byte) if 0x20 <= byte <= 0x7E else "."


def _format_hexdump_row(
    base_addr: int,
    row_bytes: list[int | None],
    width: int,
) -> str:
    """Format one hexdump row: '0xAAAAAA  XX XX ...  |ASCII|'.

    `row_bytes` entries that are None mean 'unbacked at this address';
    they print as '??' in the hex column and '.' in the ASCII column.
    The hex column is split into two halves of width/2 for readability
    when width is a multiple of 8.
    """
    half = width // 2 if width >= 8 and width % 8 == 0 else width
    hex_parts: list[str] = []
    ascii_parts: list[str] = []
    for offset, value in enumerate(row_bytes):
        if value is None:
            hex_parts.append("??")
            ascii_parts.append(".")
        else:
            hex_parts.append(f"{value:02X}")
            ascii_parts.append(_ascii_glyph(value))
        if half and (offset + 1) == half and (offset + 1) != width:
            hex_parts.append("")  # insert a gap between halves
    hex_col = " ".join(hex_parts).rstrip()
    ascii_col = "".join(ascii_parts)
    return f"0x{base_addr:06X}  {hex_col:<{(width * 3) + (1 if half else 0)}}  |{ascii_col}|"


def _cmd_memory_dump(args: argparse.Namespace) -> int:
    address = _parse_address(args.address)
    count = args.count
    width = args.width
    if count <= 0:
        print("Error: --count must be >= 1", file=sys.stderr)
        return 1
    if width <= 0:
        print("Error: --width must be >= 1", file=sys.stderr)
        return 1

    overlay: dict[int, int] = {}
    seed_label: str | None = None
    frame_state = None
    if args.seed_from is not None:
        doc = load_savestate(Path(args.seed_from), expected_rom_path=Path(args.rom))
        overlay = dict(doc.writable_overlay)
        seed_label = str(Path(args.seed_from))
        frame_state = doc.frame_state
    # M3 Phase 3.1: forward frame_state to the bus so RAS.V + BLNK
    # reads reflect the live timing seeded from the savestate.
    bus = load_read_bus(Path(args.rom), frame_state=frame_state)

    cells: list[int | None] = []
    for offset in range(count):
        cur_addr = (address + offset) & 0xFFFFFF
        if cur_addr in overlay:
            cells.append(overlay[cur_addr])
            continue
        read = bus.read_bytes(cur_addr, size=1)
        cells.append(read.data[0] if read.status == "ok" and read.data is not None else None)

    if args.json:
        payload = {
            "rom": str(bus.rom.path),
            "address": address,
            "address_hex": f"0x{address:06X}",
            "count": count,
            "width": width,
            "seed_from": seed_label,
            "bytes": [
                {
                    "address": (address + i) & 0xFFFFFF,
                    "address_hex": f"0x{(address + i) & 0xFFFFFF:06X}",
                    "value": cell,
                    "value_hex": None if cell is None else f"0x{cell:02X}",
                }
                for i, cell in enumerate(cells)
            ],
        }
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0

    print(f"ROM: {bus.rom.path}")
    print(f"Base: 0x{address:06X}   Count: {count}   Width: {width}")
    if seed_label is not None:
        print(f"Seed-from: {seed_label}")
    for row_start in range(0, count, width):
        row_addr = (address + row_start) & 0xFFFFFF
        row_cells = cells[row_start : row_start + width]
        # Pad short tail row to keep ASCII column aligned.
        if len(row_cells) < width:
            row_cells = list(row_cells) + [None] * (width - len(row_cells))
            # But trim the actual ASCII column to the real cell count for clarity.
        print(_format_hexdump_row(row_addr, list(row_cells), width))
    return 0


def _build_palette_memory_view(
    rom_path: Path, seed_from: str | None,
) -> tuple[dict[int, int], str | None]:
    """Merge the cold-start bus image with an optional savestate overlay.

    Returns `(merged_memory, seed_label)`. The merged dict is keyed by
    24-bit address. Bytes that the bus cannot resolve (unbacked CPU
    I/O page, unmapped regions) default to 0 in `_read_byte`, which
    matches what a K2GE palette read would see at cold-start for the
    backed-region 0x8200..0x83FF.

    M3 Phase 3.1: when a savestate is seeded, its `frame_state` flows
    into the cold-start image so `RAS.V` (`0x008009`) and the BLNK
    bit of `2D Status` (`0x008010`) reflect the live frame timing.
    Without `--seed-from`, frame_state defaults to the HW reset
    (scanline 0, BLNK=0) and reads stay byte-identical to pre-3.1.
    """
    seed_label: str | None = None
    frame_state = None
    overlay: dict[int, int] = {}
    if seed_from is not None:
        doc = load_savestate(Path(seed_from), expected_rom_path=rom_path)
        overlay = doc.writable_overlay
        frame_state = doc.frame_state
        seed_label = str(Path(seed_from))
    bus = load_read_bus(rom_path, frame_state=frame_state)
    memory: dict[int, int] = dict(bus.builtin_bytes)
    if overlay:
        memory.update(overlay)
    return memory, seed_label


def _palette_to_payload(palette: K2gePalette) -> dict[str, object]:
    return {
        "plane": palette.plane,
        "index": palette.index,
        "base_address": palette.base_address,
        "base_address_hex": f"0x{palette.base_address:06X}",
        "colors": [
            {
                "raw": c.raw,
                "raw_hex": f"0x{c.raw:04X}",
                "r": c.r,
                "g": c.g,
                "b": c.b,
                "hex_rgb12": c.hex_rgb12(),
                "hex_rgb24": c.hex_rgb24(),
            }
            for c in palette.colors
        ],
    }


def _palette_human_row(palette: K2gePalette) -> str:
    addr = f"0x{palette.base_address:06X}"
    color_cells = []
    for c in palette.colors:
        color_cells.append(f"{c.hex_rgb24()} ({c.r:X},{c.g:X},{c.b:X})")
    return f"  {addr}  {palette.plane:<10} #{palette.index:<2}  " + "  ".join(color_cells)


def _cmd_palette_info(args: argparse.Namespace) -> int:
    rom_path = Path(args.rom)
    memory, seed_label = _build_palette_memory_view(rom_path, args.seed_from)
    all_palettes = read_all_palettes(memory)

    kind = args.kind
    selected: list[tuple[str, tuple[K2gePalette, ...]]] = []
    if kind == "all":
        for plane in ("sprite", "scr1", "scr2", "background", "window"):
            selected.append((plane, all_palettes[plane]))
    else:
        selected.append((kind, all_palettes[kind]))

    payload = {
        "rom": str(rom_path),
        "seed_from": seed_label,
        "planes": {
            plane: [_palette_to_payload(p) for p in palettes]
            for plane, palettes in selected
        },
    }
    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0

    print(f"ROM: {rom_path}")
    if seed_label:
        print(f"Seed-from: {seed_label}")
    print(f"Layout: K2GE color mode (0BGR 12-bit, 4-bit components 0..15)")
    print()
    print(f"  Address     Plane       Idx  Colors (4 entries per palette)")
    for plane, palettes in selected:
        print(f"-- {plane} --")
        for p in palettes:
            print(_palette_human_row(p))
    return 0


def _sprite_to_payload(sprite: K2geSprite) -> dict[str, object]:
    return {
        "index": sprite.index,
        "base_address": sprite.base_address,
        "base_address_hex": f"0x{sprite.base_address:06X}",
        "raw_bytes_hex": sprite.raw_bytes.hex(" ").upper(),
        "cp_c_raw": sprite.cp_c_raw,
        "cp_c_raw_hex": f"0x{sprite.cp_c_raw:02X}",
        "tile": sprite.c_c,
        "tile_hex": f"0x{sprite.c_c:03X}",
        "h_flip": sprite.h_flip,
        "v_flip": sprite.v_flip,
        "p_c": sprite.p_c,
        "pr_c": sprite.pr_c,
        "pr_c_label": sprite.pr_c_label,
        "h_chain": sprite.h_chain,
        "v_chain": sprite.v_chain,
        "h_pos": sprite.h_pos,
        "v_pos": sprite.v_pos,
        "cp_c": sprite.cp_c,
        "hidden": sprite.is_hidden(),
    }


def _sprite_human_row(sprite: K2geSprite) -> str:
    flags = "".join(
        [
            "H" if sprite.h_flip else "-",
            "V" if sprite.v_flip else "-",
            "P" if sprite.p_c else "-",
            "h" if sprite.h_chain else "-",
            "v" if sprite.v_chain else "-",
        ]
    )
    return (
        f"  #{sprite.index:<2}  0x{sprite.base_address:06X}  "
        f"tile={sprite.c_c:<3} ({sprite.c_c:#05x})  "
        f"pos=({sprite.h_pos:>3},{sprite.v_pos:>3})  "
        f"pr={sprite.pr_c} ({sprite.pr_c_label:<10})  "
        f"cp={sprite.cp_c:<2}  flags={flags}"
    )


def _cmd_oam_info(args: argparse.Namespace) -> int:
    rom_path = Path(args.rom)
    memory, seed_label = _build_palette_memory_view(rom_path, args.seed_from)
    sprites = read_oam_sprites(memory)
    if args.visible_only:
        selected = tuple(s for s in sprites if not s.is_hidden())
    else:
        selected = sprites

    payload = {
        "rom": str(rom_path),
        "seed_from": seed_label,
        "oam_base_hex": f"0x{K2GE_OAM_BASE:06X}",
        "cp_c_base_hex": f"0x{K2GE_OAM_PALETTE_CODES_BASE:06X}",
        "total_sprites": len(sprites),
        "visible_only": bool(args.visible_only),
        "shown_count": len(selected),
        "sprites": [_sprite_to_payload(s) for s in selected],
    }
    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0

    print(f"ROM: {rom_path}")
    if seed_label:
        print(f"Seed-from: {seed_label}")
    print(
        f"OAM base: {payload['oam_base_hex']}   "
        f"CP.C base: {payload['cp_c_base_hex']}   "
        f"Total: {payload['total_sprites']}   "
        f"Shown: {payload['shown_count']}"
    )
    print("flags: H=h_flip V=v_flip P=p_c h=h_chain v=v_chain")
    print(
        "  Idx  Address     Sprite info"
    )
    for s in selected:
        print(_sprite_human_row(s))
    return 0


def _tilemap_entry_to_payload(entry: K2geTilemapEntry) -> dict[str, object]:
    return {
        "plane": entry.plane,
        "x": entry.x,
        "y": entry.y,
        "base_address": entry.base_address,
        "base_address_hex": f"0x{entry.base_address:06X}",
        "raw_bytes_hex": entry.raw_bytes.hex(" ").upper(),
        "tile": entry.c_c,
        "tile_hex": f"0x{entry.c_c:03X}",
        "h_flip": entry.h_flip,
        "v_flip": entry.v_flip,
        "p_c": entry.p_c,
        "cp_c": entry.cp_c,
        "empty": entry.is_empty(),
    }


def _tilemap_grid_row(entries: tuple[K2geTilemapEntry, ...], y: int) -> str:
    """Render one ASCII row of the 32-wide tilemap grid.

    Each cell becomes a single character:
      - `.` for empty (tile 0)
      - `0..9` for low tile numbers 1..9
      - `a..z` for tile numbers 10..35
      - `A..Z` for 36..61
      - `+` for anything beyond
    The compression is one-way; the goal is a quick visual overview,
    not a faithful round-trip. Use `--list` for the full numbers.
    """
    row_start = y * TILEMAP_TILES_PER_ROW
    cells = []
    for x in range(TILEMAP_TILES_PER_ROW):
        entry = entries[row_start + x]
        if entry.is_empty():
            cells.append(".")
        elif entry.c_c < 10:
            cells.append(str(entry.c_c))
        elif entry.c_c < 36:
            cells.append(chr(ord("a") + entry.c_c - 10))
        elif entry.c_c < 62:
            cells.append(chr(ord("A") + entry.c_c - 36))
        else:
            cells.append("+")
    return "".join(cells)


def _cmd_tilemap_info(args: argparse.Namespace) -> int:
    rom_path = Path(args.rom)
    memory, seed_label = _build_palette_memory_view(rom_path, args.seed_from)
    entries = read_tilemap(memory, args.plane)
    if args.non_empty:
        selected = tuple(e for e in entries if not e.is_empty())
    else:
        selected = entries

    base_address = (
        K2GE_SCR1_TILEMAP_BASE if args.plane == "scr1" else K2GE_SCR2_TILEMAP_BASE
    )

    payload = {
        "rom": str(rom_path),
        "seed_from": seed_label,
        "plane": args.plane,
        "base_address": base_address,
        "base_address_hex": f"0x{base_address:06X}",
        "grid_size": [TILEMAP_TILES_PER_ROW, TILEMAP_TILES_PER_COL],
        "total_entries": len(entries),
        "non_empty_only": bool(args.non_empty),
        "shown_count": len(selected),
        "non_empty_count": sum(1 for e in entries if not e.is_empty()),
        "entries": [_tilemap_entry_to_payload(e) for e in selected],
    }
    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0

    print(f"ROM: {rom_path}")
    if seed_label:
        print(f"Seed-from: {seed_label}")
    print(
        f"Plane: {args.plane}   Base: {payload['base_address_hex']}   "
        f"Grid: {TILEMAP_TILES_PER_ROW}×{TILEMAP_TILES_PER_COL}   "
        f"Non-empty: {payload['non_empty_count']}/{payload['total_entries']}"
    )
    if args.list:
        if args.non_empty and not selected:
            print("(no non-empty tiles)")
        for e in selected:
            flags = "".join(
                ["H" if e.h_flip else "-", "V" if e.v_flip else "-",
                 "P" if e.p_c else "-"]
            )
            print(
                f"  ({e.x:>2},{e.y:>2})  0x{e.base_address:06X}  "
                f"tile={e.c_c:<3} ({e.c_c:#05x})  cp={e.cp_c:<2}  flags={flags}"
            )
    else:
        # Compact 32-wide ASCII grid view. `.` = empty, alpha/digit = tile #.
        print(
            "Grid (`.` empty; `0..9 a..z A..Z +` compress tile #1..62+):"
        )
        for y in range(TILEMAP_TILES_PER_COL):
            print(f"  {y:>2}: {_tilemap_grid_row(entries, y)}")
    return 0


# 4 grayscale ramp glyphs for 2-bit tile pixel values 0..3.
# Value 0 is the conventional transparent / palette-background slot,
# so it renders as a space rather than the lightest shade — the eye
# can spot the tile silhouette at a glance.
_TILE_PIXEL_GLYPHS = (" ", "░", "▒", "█")


def _resolve_palette_for_tile(
    memory: dict[int, int], plane: str | None, palette_index: int | None,
) -> tuple[K2gePalette, ...] | None:
    """Return the resolved 4-color palette when `plane` + `index` are set.

    Returns a 1-tuple wrapping the chosen `K2gePalette`, or `None` when
    the user did not request a palette resolution. Raises on invalid
    plane / index inputs.
    """
    if plane is None and palette_index is None:
        return None
    if plane is None or palette_index is None:
        raise ValueError(
            "--palette and --plane must be provided together for tile colorisation"
        )
    if not (0 <= palette_index < 16):
        raise ValueError("--palette must be in 0..15")
    plane_to_base = {
        "sprite": K2GE_PALETTE_SPRITE_BASE,
        "scr1": K2GE_PALETTE_SCR1_BASE,
        "scr2": K2GE_PALETTE_SCR2_BASE,
    }
    if plane not in plane_to_base:
        raise ValueError("--plane must be one of sprite / scr1 / scr2")
    palettes = read_plane_palettes(memory, plane_to_base[plane], plane)
    return (palettes[palette_index],)


def _tile_pixels_to_payload(
    tile: K2geTilePixels, palette: K2gePalette | None,
) -> dict[str, object]:
    rows: list[dict[str, object]] = []
    for y, row in enumerate(tile.pixels):
        rendered_row: dict[str, object] = {
            "y": y,
            "values": list(row),
            "glyphs": "".join(_TILE_PIXEL_GLYPHS[v] for v in row),
        }
        if palette is not None:
            rendered_row["hex_rgb24"] = [palette.colors[v].hex_rgb24() for v in row]
        rows.append(rendered_row)
    return {
        "tile_id": tile.tile_id,
        "tile_id_hex": f"0x{tile.tile_id:03X}",
        "base_address": tile.base_address,
        "base_address_hex": f"0x{tile.base_address:06X}",
        "raw_bytes_hex": tile.raw_bytes.hex(" ").upper(),
        "blank": tile.is_blank(),
        "rows": rows,
    }


def _cmd_tile_view(args: argparse.Namespace) -> int:
    rom_path = Path(args.rom)
    tile_id = _parse_address_arg(args.tile_id)
    if not (0 <= tile_id < CHAR_RAM_TILE_COUNT):
        print(
            f"Error: tile-id must be in 0..{CHAR_RAM_TILE_COUNT - 1}; got {tile_id}",
            file=sys.stderr,
        )
        return 1

    memory, seed_label = _build_palette_memory_view(rom_path, args.seed_from)
    try:
        palette_pack = _resolve_palette_for_tile(memory, args.plane, args.palette)
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    palette = palette_pack[0] if palette_pack is not None else None

    tile = read_tile(memory, tile_id)
    payload = {
        "rom": str(rom_path),
        "seed_from": seed_label,
        "char_ram_base_hex": f"0x{K2GE_CHAR_RAM_BASE:06X}",
        "tile_width": CHAR_RAM_TILE_WIDTH,
        "tile_height": CHAR_RAM_TILE_HEIGHT,
        "palette_plane": args.plane,
        "palette_index": args.palette,
        "palette": _palette_to_payload(palette) if palette is not None else None,
        "tile": _tile_pixels_to_payload(tile, palette),
    }
    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0

    print(f"ROM: {rom_path}")
    if seed_label:
        print(f"Seed-from: {seed_label}")
    print(
        f"Tile #{tile_id} ({payload['tile']['tile_id_hex']})  "
        f"address={payload['tile']['base_address_hex']}  "
        f"blank={payload['tile']['blank']}"
    )
    if palette is not None:
        print(
            f"Palette: plane={args.plane}  index={args.palette}  "
            f"colors={', '.join(c.hex_rgb24() for c in palette.colors)}"
        )
    print("Pixels (value -> glyph: 0=' ', 1=light, 2=medium, 3=full block):")
    for y, row in enumerate(tile.pixels):
        glyphs = "".join(_TILE_PIXEL_GLYPHS[v] for v in row)
        values = " ".join(str(v) for v in row)
        print(f"  y={y}  |{glyphs}|  values=[{values}]")
    return 0


def _parse_tile_range_arg(spec: str) -> list[int]:
    """Parse '`N..M`' (inclusive) or a single `N` into a list of tile IDs.

    Accepts decimal or `0x`-prefixed hex on either side of `..`.
    Raises `ValueError` on malformed input or reversed range.
    """
    spec = spec.strip()
    if ".." in spec:
        parts = spec.split("..", 1)
        start = _parse_address_arg(parts[0])
        end = _parse_address_arg(parts[1])
        if start > end:
            raise ValueError(
                f"range start {start} must be <= end {end}; got '{spec}'"
            )
        return list(range(start, end + 1))
    return [_parse_address_arg(spec)]


def _cmd_tiles_view(args: argparse.Namespace) -> int:
    rom_path = Path(args.rom)
    memory, seed_label = _build_palette_memory_view(rom_path, args.seed_from)

    try:
        tile_ids = _parse_tile_range_arg(args.range)
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    try:
        palette_pack = _resolve_palette_for_tile(memory, args.plane, args.palette)
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    palette = palette_pack[0] if palette_pack is not None else None

    try:
        width, height, pixels = render_tile_atlas(
            memory, tile_ids, cols=args.cols, palette=palette,
        )
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    output_path = Path(args.output) if args.output else Path("tiles.ppm")
    ppm_bytes = pixels_to_ppm_bytes(width, height, pixels)
    try:
        output_path.write_bytes(ppm_bytes)
    except OSError as exc:
        print(f"Error: cannot write {output_path}: {exc}", file=sys.stderr)
        return 1

    rows = (len(tile_ids) + args.cols - 1) // args.cols if tile_ids else 0

    if args.json:
        payload = {
            "rom": str(rom_path),
            "seed_from": seed_label,
            "tile_count": len(tile_ids),
            "first_tile": tile_ids[0] if tile_ids else None,
            "last_tile": tile_ids[-1] if tile_ids else None,
            "cols": args.cols,
            "rows": rows,
            "width": width,
            "height": height,
            "output_path": str(output_path),
            "ppm_byte_count": len(ppm_bytes),
            "palette_plane": args.plane,
            "palette_index": args.palette,
            "colorisation": "palette" if palette is not None else "grayscale",
            "palette": (
                _palette_to_payload(palette) if palette is not None else None
            ),
        }
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0

    print(f"ROM: {rom_path}")
    if seed_label is not None:
        print(f"Seed-from: {seed_label}")
    print(
        f"Atlas: {len(tile_ids)} tiles, {args.cols} cols × {rows} rows = "
        f"{width}×{height} px"
    )
    print(f"Output: {output_path}  bytes={len(ppm_bytes)}")
    if palette is not None:
        colorisation = f"palette {args.plane} #{args.palette}"
    else:
        colorisation = "grayscale (4-level)"
    print(f"Colorisation: {colorisation}")
    return 0


def _control_registers_to_payload(control: K2geControlRegisters) -> dict[str, object]:
    return {
        "window": {
            "wba_h": control.wba_h, "wba_v": control.wba_v,
            "wsi_h": control.wsi_h, "wsi_v": control.wsi_v,
        },
        "twod_control": {
            "neg": control.neg, "oowc": control.oowc,
        },
        "sprite_offset": {"po_h": control.po_h, "po_v": control.po_v},
        "scroll_prio": {"scr2_in_front": control.scr2_in_front},
        "scroll_offsets": {
            "s1so_h": control.s1so_h, "s1so_v": control.s1so_v,
            "s2so_h": control.s2so_h, "s2so_v": control.s2so_v,
        },
        "backdrop_control": {
            "bgc_raw": control.bgc_raw,
            "bgc_raw_hex": f"0x{control.bgc_raw:02X}",
            "bgc_enabled": control.bgc_enabled,
            "bgc_index": control.bgc_index,
        },
        "mode": {"k1ge_compat": control.k1ge_compat},
    }


def _cmd_screenshot(args: argparse.Namespace) -> int:
    from core.renderer import resolve_oowc_color

    rom_path = Path(args.rom)
    memory, seed_label = _build_palette_memory_view(rom_path, args.seed_from)
    frame = render_frame(memory)
    oowc_color = resolve_oowc_color(memory, frame.control)

    output_path = Path(args.output) if args.output else Path("screenshot.ppm")
    ppm_bytes = frame_to_ppm_bytes(frame)
    try:
        output_path.write_bytes(ppm_bytes)
    except OSError as exc:
        print(f"Error: cannot write {output_path}: {exc}", file=sys.stderr)
        return 1

    if args.json:
        payload = {
            "rom": str(rom_path),
            "seed_from": seed_label,
            "width": frame.width,
            "height": frame.height,
            "output_path": str(output_path),
            "ppm_byte_count": len(ppm_bytes),
            "backdrop_color": {
                "r": frame.backdrop_color.r,
                "g": frame.backdrop_color.g,
                "b": frame.backdrop_color.b,
                "raw": frame.backdrop_color.raw,
                "raw_hex": f"0x{frame.backdrop_color.raw:04X}",
                "hex_rgb12": frame.backdrop_color.hex_rgb12(),
                "hex_rgb24": frame.backdrop_color.hex_rgb24(),
            },
            "oowc_color": {
                "r": oowc_color.r,
                "g": oowc_color.g,
                "b": oowc_color.b,
                "raw_hex": f"0x{oowc_color.raw:04X}",
                "hex_rgb24": oowc_color.hex_rgb24(),
            },
            "control": _control_registers_to_payload(frame.control),
            "renderer_pass": "1.3",
            "renderer_note": (
                "backdrop + SCR1/SCR2 raster + sprites with PR.C 4-level "
                "composition (00 hidden / 01 behind / 10 middle / 11 front), "
                "H.ch/V.ch chain resolution, global PO.H/V sprite offset, "
                "H.F/V.F flip, palette transparency, window clip with OOWC "
                "fill, NEG invert — ROADMAP §8 P0 'screenshots' closed"
            ),
        }
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0

    ctrl = frame.control
    print(f"ROM: {rom_path}")
    if seed_label is not None:
        print(f"Seed-from: {seed_label}")
    print(
        f"Frame: {frame.width}×{frame.height}  output={output_path}  "
        f"bytes={len(ppm_bytes)}"
    )
    print(
        f"Backdrop: {frame.backdrop_color.hex_rgb24()}  "
        f"bgc_enabled={ctrl.bgc_enabled}  bgc_index={ctrl.bgc_index}  "
        f"raw=0x{ctrl.bgc_raw:02X}"
    )
    print(
        f"Scroll prio: scr2_in_front={ctrl.scr2_in_front}  "
        f"s1so=({ctrl.s1so_h},{ctrl.s1so_v})  "
        f"s2so=({ctrl.s2so_h},{ctrl.s2so_v})"
    )
    print(
        f"Sprite offset: PO=({ctrl.po_h},{ctrl.po_v})  "
        f"Window: WBA=({ctrl.wba_h},{ctrl.wba_v})  "
        f"WSI=({ctrl.wsi_h},{ctrl.wsi_v})"
    )
    print(
        f"2D Control: NEG={ctrl.neg}  OOWC={ctrl.oowc} -> {oowc_color.hex_rgb24()}"
        f"   MODE: k1ge_compat={ctrl.k1ge_compat}"
    )
    print(
        "Renderer pass 1.3 — full K2GE color-mode compose: backdrop + "
        "SCR1/SCR2 + sprites (PR.C 4-level) + window clip + NEG invert"
    )
    return 0


def _cmd_frame_diff(args: argparse.Namespace) -> int:
    path_a = Path(args.ppm_a)
    path_b = Path(args.ppm_b)
    try:
        data_a = path_a.read_bytes()
        data_b = path_b.read_bytes()
        result = diff_ppm_bytes(data_a, data_b)
    except (OSError, ValueError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    if args.json:
        payload = {
            "ppm_a": str(path_a),
            "ppm_b": str(path_b),
            "width": result.width,
            "height": result.height,
            "total_pixels": result.total_pixels,
            "pixel_count_different": result.pixel_count_different,
            "diff_ratio": result.diff_ratio,
            "first_diff_pixel": list(result.first_diff_pixel)
                if result.first_diff_pixel else None,
            "equal": result.equal,
        }
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0 if result.equal else 1

    print(f"A: {path_a}")
    print(f"B: {path_b}")
    print(
        f"Dimensions: {result.width}×{result.height}  "
        f"total pixels: {result.total_pixels}"
    )
    if result.equal:
        print("MATCH (all pixels identical)")
    else:
        print(
            f"DIFF: {result.pixel_count_different}/{result.total_pixels} pixels "
            f"({result.diff_ratio * 100:.2f}%); first diff at "
            f"{result.first_diff_pixel}"
        )
    return 0 if result.equal else 1


def _cmd_frame_golden_save(args: argparse.Namespace) -> int:
    rom_path = Path(args.rom)
    memory, seed_label = _build_palette_memory_view(rom_path, args.seed_from)
    frame = render_frame(memory)
    ppm_bytes = frame_to_ppm_bytes(frame)

    manifest = build_frame_golden_manifest(
        rom_path=rom_path,
        name=args.name,
        ppm_bytes=ppm_bytes,
        width=frame.width,
        height=frame.height,
        label=args.label,
        seed_from=seed_label,
        control_snapshot=_control_registers_to_payload(frame.control),
    )

    try:
        ppm_path, manifest_path = save_frame_golden(
            rom_path, args.name, ppm_bytes, manifest,
        )
    except OSError as exc:
        print(f"Error: cannot save golden: {exc}", file=sys.stderr)
        return 1

    if args.json:
        payload = {
            "rom": str(rom_path),
            "name": args.name,
            "ppm_path": str(ppm_path),
            "manifest_path": str(manifest_path),
            "manifest": manifest,
        }
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0

    print(f"Saved frame golden '{args.name}' for {rom_path}")
    print(f"  PPM:      {ppm_path}  ({len(ppm_bytes)} bytes)")
    print(f"  Manifest: {manifest_path}")
    if args.label:
        print(f"  Label: {args.label}")
    if seed_label is not None:
        print(f"  Seed-from: {seed_label}")
    return 0


def _cmd_frame_golden_check(args: argparse.Namespace) -> int:
    rom_path = Path(args.rom)
    try:
        golden = load_frame_golden(rom_path, args.name)
    except FileNotFoundError as exc:
        print(f"Error: golden not found: {exc}", file=sys.stderr)
        return 1

    memory, seed_label = _build_palette_memory_view(rom_path, args.seed_from)
    frame = render_frame(memory)
    ppm_bytes = frame_to_ppm_bytes(frame)

    if args.save_current:
        try:
            Path(args.save_current).write_bytes(ppm_bytes)
        except OSError as exc:
            print(f"Error: cannot save current frame: {exc}", file=sys.stderr)
            return 1

    try:
        result = diff_ppm_bytes(golden.ppm_path.read_bytes(), ppm_bytes)
    except (OSError, ValueError) as exc:
        print(f"Error: cannot compare: {exc}", file=sys.stderr)
        return 1

    if args.json:
        payload = {
            "rom": str(rom_path),
            "name": args.name,
            "golden_ppm_path": str(golden.ppm_path),
            "current_ppm_path": (
                str(args.save_current) if args.save_current else None
            ),
            "seed_from": seed_label,
            "equal": result.equal,
            "width": result.width,
            "height": result.height,
            "total_pixels": result.total_pixels,
            "pixel_count_different": result.pixel_count_different,
            "diff_ratio": result.diff_ratio,
            "first_diff_pixel": list(result.first_diff_pixel)
                if result.first_diff_pixel else None,
        }
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0 if result.equal else 1

    print(f"Golden '{args.name}' for {rom_path}")
    print(f"  Stored PPM: {golden.ppm_path}")
    if result.equal:
        print(f"MATCH (all {result.total_pixels} pixels identical)")
    else:
        print(
            f"DIFF: {result.pixel_count_different}/{result.total_pixels} pixels "
            f"({result.diff_ratio * 100:.2f}%); first diff at "
            f"{result.first_diff_pixel}"
        )
    return 0 if result.equal else 1


def _cmd_frame_golden_check_all(args: argparse.Namespace) -> int:
    rom_path = Path(args.rom)
    goldens = list_frame_goldens(rom_path)

    if not goldens:
        if args.json:
            print(json.dumps(
                {
                    "rom": str(rom_path),
                    "total": 0, "passed": 0, "failed": 0,
                    "all_equal": True,
                    "results": [],
                },
                indent=2, sort_keys=True,
            ))
        else:
            print(f"No frame goldens registered for {rom_path}")
        return 0

    memory, seed_label = _build_palette_memory_view(rom_path, args.seed_from)
    frame = render_frame(memory)
    ppm_bytes = frame_to_ppm_bytes(frame)

    save_dir = Path(args.save_current_dir) if args.save_current_dir else None
    if save_dir is not None:
        try:
            save_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            print(
                f"Error: cannot create --save-current-dir {save_dir}: {exc}",
                file=sys.stderr,
            )
            return 1
        current_path = save_dir / f"{rom_path.stem}.current.ppm"
        try:
            current_path.write_bytes(ppm_bytes)
        except OSError as exc:
            print(
                f"Error: cannot write {current_path}: {exc}", file=sys.stderr,
            )
            return 1

    results: list[dict[str, object]] = []
    passed = 0
    failed = 0
    short_circuited = False
    for golden in goldens:
        try:
            stored_ppm = golden.ppm_path.read_bytes()
            diff = diff_ppm_bytes(stored_ppm, ppm_bytes)
        except (OSError, ValueError) as exc:
            results.append({
                "name": golden.name,
                "status": "error",
                "error": str(exc),
            })
            failed += 1
            if args.stop_on_fail:
                short_circuited = True
                break
            continue

        result_entry: dict[str, object] = {
            "name": golden.name,
            "status": "match" if diff.equal else "diff",
            "equal": diff.equal,
            "pixel_count_different": diff.pixel_count_different,
            "diff_ratio": diff.diff_ratio,
            "first_diff_pixel": (
                list(diff.first_diff_pixel)
                if diff.first_diff_pixel else None
            ),
        }
        results.append(result_entry)
        if diff.equal:
            passed += 1
        else:
            failed += 1
            if args.stop_on_fail:
                short_circuited = True
                break

    all_equal = failed == 0
    exit_code = 0 if all_equal else 1

    if args.json:
        payload = {
            "rom": str(rom_path),
            "seed_from": seed_label,
            "total": len(goldens),
            "checked": len(results),
            "passed": passed,
            "failed": failed,
            "stopped_early": short_circuited,
            "all_equal": all_equal,
            "save_current_dir": (
                str(save_dir) if save_dir is not None else None
            ),
            "results": results,
        }
        print(json.dumps(payload, indent=2, sort_keys=True))
        return exit_code

    print(f"ROM: {rom_path}")
    if seed_label is not None:
        print(f"Seed-from: {seed_label}")
    print(
        f"Frame goldens: {len(results)}/{len(goldens)} checked, "
        f"{passed} passed, {failed} failed"
        + (" (stopped early)" if short_circuited else "")
    )
    for entry in results:
        status = entry["status"]
        name = entry["name"]
        if status == "match":
            print(f"  [OK]   {name}")
        elif status == "diff":
            pct = float(entry["diff_ratio"]) * 100
            print(
                f"  [DIFF] {name}  "
                f"{entry['pixel_count_different']} px ({pct:.2f}%); "
                f"first @ {entry['first_diff_pixel']}"
            )
        else:
            print(f"  [ERR]  {name}  {entry.get('error')}")
    return exit_code


def _cmd_frame_golden_list(args: argparse.Namespace) -> int:
    rom_path = Path(args.rom)
    goldens = list_frame_goldens(rom_path)

    if args.json:
        payload = {
            "rom": str(rom_path),
            "count": len(goldens),
            "goldens": [
                {
                    "name": g.name,
                    "slug": g.slug,
                    "ppm_path": str(g.ppm_path),
                    "manifest_path": str(g.manifest_path),
                    "width": g.manifest.get("width"),
                    "height": g.manifest.get("height"),
                    "ppm_byte_count": g.manifest.get("ppm_byte_count"),
                    "ppm_sha256": g.manifest.get("ppm_sha256"),
                    "captured_at_utc": g.manifest.get("captured_at_utc"),
                    "label": g.manifest.get("label"),
                }
                for g in goldens
            ],
        }
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0

    if not goldens:
        print(f"No frame goldens for {rom_path}")
        return 0
    print(f"Frame goldens for {rom_path} ({len(goldens)}):")
    for g in goldens:
        label = g.manifest.get("label")
        label_str = f" — {label}" if label else ""
        print(
            f"  {g.name:<24} "
            f"{g.manifest.get('width')}×{g.manifest.get('height')}  "
            f"{g.manifest.get('ppm_byte_count')} B  "
            f"{g.manifest.get('captured_at_utc', '')}{label_str}"
        )
    return 0


def _cmd_frame_golden_delete(args: argparse.Namespace) -> int:
    rom_path = Path(args.rom)
    try:
        ppm_path, manifest_path = delete_frame_golden(rom_path, args.name)
    except FileNotFoundError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    if args.json:
        payload = {
            "rom": str(rom_path),
            "name": args.name,
            "deleted_ppm": str(ppm_path),
            "deleted_manifest": str(manifest_path),
        }
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0

    print(f"Deleted frame golden '{args.name}' for {rom_path}")
    print(f"  Removed PPM:      {ppm_path}")
    print(f"  Removed manifest: {manifest_path}")
    return 0


def _fetch_to_dict(result: FetchResult) -> dict[str, object]:
    return {
        "pc": result.pc,
        "pc_hex": f"0x{result.pc:08X}",
        "width": result.width,
        "status": result.status,
        "note": result.note,
        "data": None if result.data is None else list(result.data),
        "data_hex": None if result.data is None else result.data.hex(" ").upper(),
        "next_sequential_pc": result.next_sequential_pc,
        "next_sequential_pc_hex": (
            None
            if result.next_sequential_pc is None
            else f"0x{result.next_sequential_pc:08X}"
        ),
        "read": _read_to_dict(result.read),
    }


def _cmd_fetch_next(args: argparse.Namespace) -> int:
    view = load_fetch_view(Path(args.rom))
    result = fetch_next_bytes(view, size=args.count)
    payload = _fetch_to_dict(result)
    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0

    print(f"ROM: {view.machine.rom_path}")
    print(f"PC: {payload['pc_hex']}")
    print(f"Width: {payload['width']}")
    print(f"Status: {payload['status']}")
    if payload["data_hex"] is not None:
        print(f"Data: {payload['data_hex']}")
    if payload["next_sequential_pc_hex"] is not None:
        print(f"Next sequential PC: {payload['next_sequential_pc_hex']}")
    region = payload["read"]["probe"]["region"]  # type: ignore[index]
    if region is None:
        print("Region: <unmapped>")
    else:
        print(f"Region: {region['name']} [{region['kind']}]")
        print(f"Range: {region['start_hex']}..{region['end_hex']}")
    print(f"Note: {payload['note']}")
    return 0


def _decode_to_dict(result: DecodeResult) -> dict[str, object]:
    return {
        "pc": result.pc,
        "pc_hex": f"0x{result.pc:08X}",
        "status": result.status,
        "raw_bytes": None if result.raw_bytes is None else list(result.raw_bytes),
        "raw_bytes_hex": (
            None if result.raw_bytes is None else result.raw_bytes.hex(" ").upper()
        ),
        "length": result.length,
        "mnemonic": result.mnemonic,
        "operands": result.operands,
        "assembly": result.assembly,
        "next_sequential_pc": result.next_sequential_pc,
        "next_sequential_pc_hex": (
            None
            if result.next_sequential_pc is None
            else f"0x{result.next_sequential_pc:08X}"
        ),
        "control_flow_kind": result.control_flow_kind,
        "direct_target": result.direct_target,
        "direct_target_hex": (
            None if result.direct_target is None else f"0x{result.direct_target:08X}"
        ),
        "falls_through": result.falls_through,
        "warning": result.warning,
        "matched_quirk": _known_quirk_to_dict(match_known_quirk(result)),
        "note": result.note,
    }


def _cmd_decode_next(args: argparse.Namespace) -> int:
    view = load_fetch_view(Path(args.rom), bios_path=args.bios)
    if args.address is None:
        result = decode_next_instruction(view)
    else:
        result = decode_instruction_at(view.bus, _parse_address(args.address))
    payload = _decode_to_dict(result)
    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0

    print(f"ROM: {view.machine.rom_path}")
    print(f"PC: {payload['pc_hex']}")
    print(f"Status: {payload['status']}")
    if payload["raw_bytes_hex"] is not None:
        print(f"Bytes: {payload['raw_bytes_hex']}")
    if payload["assembly"] is not None:
        print(f"Decoded: {payload['assembly']}")
    if payload["length"] is not None:
        print(f"Length: {payload['length']}")
    if payload["next_sequential_pc_hex"] is not None:
        print(f"Next sequential PC: {payload['next_sequential_pc_hex']}")
    if payload["control_flow_kind"] is not None:
        print(f"Control flow: {payload['control_flow_kind']}")
    if payload["direct_target_hex"] is not None:
        print(f"Direct target: {payload['direct_target_hex']}")
    if payload["falls_through"] is not None:
        print(f"Falls through: {payload['falls_through']}")
    if payload["warning"] is not None:
        print(f"Warning: {payload['warning']}")
    if payload["matched_quirk"] is not None:
        quirk = payload["matched_quirk"]  # type: ignore[index]
        print(f"Known quirk: {_known_quirk_label(quirk)}")
        print(f"Quirk summary: {quirk['summary']}")
        _print_known_quirk_sources(quirk)
    print(f"Note: {payload['note']}")
    return 0


def _cpu_named_hex(cpu: NgpcCpuState, name: str) -> str:
    if name == "PC":
        return f"0x{cpu.pc:08X}"

    if name == "F":
        value = encode_f_from_flags(cpu.flags)
        return "<unknown>" if value is None else f"0x{value:02X}"

    if name == "F'":
        if cpu.alt_flags is None:
            return "<unknown>"
        value = encode_f_from_flags(cpu.alt_flags)
        return "<unknown>" if value is None else f"0x{value:02X}"

    if "@bank" in name:
        base_name, _, bank_suffix = name.partition("@bank")
        if (
            base_name in {"XWA", "XBC", "XDE", "XHL"}
            and bank_suffix.isdigit()
            and cpu.register_banks is not None
        ):
            bank_index = int(bank_suffix)
            if 0 <= bank_index < len(cpu.register_banks):
                reg_index = {"XWA": 0, "XBC": 1, "XDE": 2, "XHL": 3}[base_name]
                slots = cpu.register_banks[bank_index].slots[reg_index * 4 : (reg_index * 4) + 4]
                if any(slot is None for slot in slots):
                    return "<unknown>"
                value = (
                    int(slots[0])
                    | (int(slots[1]) << 8)
                    | (int(slots[2]) << 16)
                    | (int(slots[3]) << 24)
                ) & 0xFFFFFFFF
                return f"0x{value:08X}"
        return "<unknown>"

    register_name = name.lower()
    if name.startswith("X") and hasattr(cpu.regs, register_name):
        value = getattr(cpu.regs, register_name)
        return "<unknown>" if value is None else f"0x{value:08X}"

    if name in {"WA", "BC", "DE", "HL", "IX", "IY", "IZ", "SP"}:
        owner_name = {
            "WA": "xwa",
            "BC": "xbc",
            "DE": "xde",
            "HL": "xhl",
            "IX": "xix",
            "IY": "xiy",
            "IZ": "xiz",
            "SP": "xsp",
        }[name]
        owner_value = getattr(cpu.regs, owner_name)
        return "<unknown>" if owner_value is None else f"0x{owner_value & 0xFFFF:04X}"

    if name in {"W", "A", "B", "C", "D", "E", "H", "L"}:
        owner_name = {
            "W": "xwa",
            "A": "xwa",
            "B": "xbc",
            "C": "xbc",
            "D": "xde",
            "E": "xde",
            "H": "xhl",
            "L": "xhl",
        }[name]
        owner_value = getattr(cpu.regs, owner_name)
        if owner_value is None:
            return "<unknown>"
        shift = 8 if name in {"W", "B", "D", "H"} else 0
        return f"0x{(owner_value >> shift) & 0xFF:02X}"

    if name == "IFF":
        if cpu.iff_enabled is None:
            return "<unknown>"
        return "enabled" if cpu.iff_enabled else "disabled"

    if name == "SR":
        return "<unknown>" if cpu.sr_raw is None else f"0x{cpu.sr_raw:04X}"

    if name == "RFP":
        return "<unknown>" if cpu.rfp is None else str(cpu.rfp)

    raise ValueError(f"unknown CPU view name: {name}")


def _memory_overlay_to_rows(
    memory: dict[int, int] | None,
) -> list[dict[str, object]] | None:
    if memory is None:
        return None
    return [
        {
            "address": address,
            "address_hex": f"0x{address:06X}",
            "value": value,
            "value_hex": f"0x{value:02X}",
        }
        for address, value in sorted(memory.items())
    ]


def _seed_registers_to_rows(seed_registers: dict[str, int]) -> list[dict[str, object]]:
    return [
        {
            "name": name,
            "value": value,
            "value_hex": f"0x{value:08X}",
        }
        for name, value in sorted(seed_registers.items())
    ]


def _known_quirk_to_dict(quirk: object | None) -> dict[str, object] | None:
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


def _known_quirk_label(quirk: dict[str, object]) -> str:
    return (
        f"{quirk['quirk_id']} "
        f"[{quirk['confidence']}, {quirk['category']}, db={quirk['database_version']}]"
    )


def _print_known_quirk_sources(quirk: dict[str, object], indent: str = "  ") -> None:
    sources = quirk.get("sources") or []
    if not sources:
        return
    print("Sources:")
    for source in sources:  # type: ignore[union-attr]
        section = source.get("section")
        suffix = f" ({section})" if section else ""
        print(f"{indent}- {source['document']}{suffix}")


_BANK0_REGISTERS_FOR_RESET_ZERO = (
    "XWA",
    "XBC",
    "XDE",
    "XHL",
    "XIX",
    "XIY",
)
_CALLER_SAVED_ZERO_SEEDS = {
    "XWA": 0,
    "XBC": 0,
    "XDE": 0,
    "XHL": 0,
    "XIX": 0,
    "XIZ": 0,
}
_ADECL_ARG_ZERO_SEEDS = {
    "XWA": 0,
    "XBC": 0,
    "XDE": 0,
}
_TOOLCHAIN_LOOP_IZ_ZERO_SEEDS = {
    "XIZ": 0,
}
_BIOS_HANDOFF_XSP_SEED = BIOS_HANDOFF_XSP
_BIOS_HANDOFF_MINIMAL_SEEDS = bios_handoff_minimal_seed_registers()
_BIOS_CALL_CONTEXT_ZERO_SEEDS = {
    "XBC@BANK3": 0,
    "XDE@BANK3": 0,
    "XHL@BANK3": 0,
    "XIY": 0,
    "XIZ": 0,
}
_SEEDABLE_CONTROL_REGISTER_NAMES = {name.upper() for name in CONTROL_REGISTER_NAMES.values()}


def _parse_seed_registers(args: argparse.Namespace) -> dict[str, int]:
    """Resolve every register seed source into a single {NAME: value} dict.

    Resolution order:
      1. --seed-zero-bank0 (low-priority bulk default for the 6 bank-0
         general registers: XWA/XBC/XDE/XHL/XIX/XIY = 0). This is a
         software convention assumption — most cc900/cdecl/adecl
         startups zero the bank-0 set before user code runs. It is NOT
         a hardware-verified power-on reset behavior; the explicit flag
         keeps the assumption opt-in and visible in the command line.
      2. --seed-zero-caller-saved (low-priority ABI/toolchain convention:
         zero the observed cdecl caller-saved set XWA/XBC/XDE/XHL/XIX/XIZ).
      3. --seed-zero-adecl-args (low-priority ABI/toolchain convention:
         zero the observed __adecl argument registers XWA/XBC/XDE).
      4. --seed-zero-toolchain-loop-iz (low-priority toolchain/codegen
         convention: zero XIZ for loop-variable / post-increment paths where
         the current toolchain commonly uses IZ explicitly).
      5. --seed-bios-handoff-xsp (low-priority sourced reset-layer shortcut:
         seed XSP with the documented BIOS hand-off stack top 0x00006C00).
      6. --seed-bios-handoff-minimal (low-priority UI/session-equivalent
         hand-off shortcut: seed `XSP=0x00006C00` and `INTNEST=0`).
      7. --seed-reg NAME=VALUE (individual seeds; override the low-priority
         preset flags above for any general or modeled control register named
         explicitly).
      7b. --seed-zero-bios-call-context (low-priority exploratory default for
         BIOS-call stepping: `XBC@bank3`, `XDE@bank3`, `XHL@bank3`, `XIY`,
         `XIZ` -> `0`).
      8. --seed-xsp (a convenience shortcut equivalent to --seed-reg XSP=...).

    Conflicts between --seed-reg values for the same register raise.
    Mixing --seed-zero-bank0 with --seed-reg for the same register is
    NOT a conflict: the explicit seed silently wins (the bulk default
    is meant to be partial).
    """
    seed_registers: dict[str, int] = {}

    if getattr(args, "seed_zero_bank0", False):
        for name in _BANK0_REGISTERS_FOR_RESET_ZERO:
            seed_registers[name] = 0
    if getattr(args, "seed_zero_caller_saved", False):
        seed_registers.update(_CALLER_SAVED_ZERO_SEEDS)
    if getattr(args, "seed_zero_adecl_args", False):
        seed_registers.update(_ADECL_ARG_ZERO_SEEDS)
    if getattr(args, "seed_zero_toolchain_loop_iz", False):
        seed_registers.update(_TOOLCHAIN_LOOP_IZ_ZERO_SEEDS)
    if getattr(args, "seed_bios_handoff_xsp", False):
        seed_registers["XSP"] = _BIOS_HANDOFF_XSP_SEED
    if getattr(args, "seed_bios_handoff_minimal", False):
        seed_registers.update(_BIOS_HANDOFF_MINIMAL_SEEDS)
    if getattr(args, "seed_zero_bios_call_context", False):
        seed_registers.update(_BIOS_CALL_CONTEXT_ZERO_SEEDS)

    for entry in args.seed_reg:
        if "=" not in entry:
            raise ValueError("seed register must use NAME=VALUE format")
        raw_name, raw_value = entry.split("=", 1)
        register_name = raw_name.strip().upper()
        if "@BANK" in register_name:
            base_name, _, bank_suffix = register_name.partition("@BANK")
            if (
                base_name not in {"XWA", "XBC", "XDE", "XHL"}
                or not bank_suffix.isdigit()
                or not (0 <= int(bank_suffix) <= 3)
            ):
                raise ValueError(
                    "banked seed register name must use XWA@bank0..3, XBC@bank0..3, "
                    "XDE@bank0..3 or XHL@bank0..3"
                )
        elif register_name not in {
            "XWA", "XBC", "XDE", "XHL", "XIX", "XIY", "XIZ", "XSP",
            *_SEEDABLE_CONTROL_REGISTER_NAMES,
        }:
            raise ValueError(
                "seed register name must be one of: XWA, XBC, XDE, XHL, XIX, XIY, XIZ, XSP, "
                "DMAS0..3, DMAD0..3, DMAC0..3, DMAM0..3, INTNEST, or XWA/XBC/XDE/XHL@bank0..3"
            )
        value = _parse_address(raw_value.strip()) & 0xFFFFFFFF
        current_value = seed_registers.get(register_name)
        # Bank-0 default-zero never conflicts with explicit seeds.
        is_bank0_default = (
            getattr(args, "seed_zero_bank0", False)
            and register_name in _BANK0_REGISTERS_FOR_RESET_ZERO
            and current_value == 0
        )
        is_caller_saved_default = (
            getattr(args, "seed_zero_caller_saved", False)
            and register_name in _CALLER_SAVED_ZERO_SEEDS
            and current_value == 0
        )
        is_adecl_arg_default = (
            getattr(args, "seed_zero_adecl_args", False)
            and register_name in _ADECL_ARG_ZERO_SEEDS
            and current_value == 0
        )
        is_toolchain_loop_iz_default = (
            getattr(args, "seed_zero_toolchain_loop_iz", False)
            and register_name in _TOOLCHAIN_LOOP_IZ_ZERO_SEEDS
            and current_value == 0
        )
        is_bios_handoff_xsp_default = (
            getattr(args, "seed_bios_handoff_xsp", False)
            and register_name == "XSP"
            and current_value == _BIOS_HANDOFF_XSP_SEED
        )
        is_bios_handoff_minimal_default = (
            getattr(args, "seed_bios_handoff_minimal", False)
            and register_name in _BIOS_HANDOFF_MINIMAL_SEEDS
            and current_value == _BIOS_HANDOFF_MINIMAL_SEEDS[register_name]
        )
        is_bios_call_default = (
            getattr(args, "seed_zero_bios_call_context", False)
            and register_name in _BIOS_CALL_CONTEXT_ZERO_SEEDS
            and current_value == 0
        )
        if (
            current_value is not None
            and current_value != value
            and not is_bank0_default
            and not is_caller_saved_default
            and not is_adecl_arg_default
            and not is_toolchain_loop_iz_default
            and not is_bios_handoff_xsp_default
            and not is_bios_handoff_minimal_default
            and not is_bios_call_default
        ):
            raise ValueError(f"conflicting seed values were provided for {register_name}")
        seed_registers[register_name] = value

    if args.seed_xsp is not None:
        xsp_value = _parse_address(args.seed_xsp) & 0xFFFFFFFF
        current_value = seed_registers.get("XSP")
        is_bios_handoff_xsp_default = (
            getattr(args, "seed_bios_handoff_xsp", False)
            and current_value == _BIOS_HANDOFF_XSP_SEED
        )
        is_bios_handoff_minimal_default = (
            getattr(args, "seed_bios_handoff_minimal", False)
            and current_value == _BIOS_HANDOFF_XSP_SEED
        )
        if (
            current_value is not None
            and current_value != xsp_value
            and not is_bios_handoff_xsp_default
            and not is_bios_handoff_minimal_default
        ):
            raise ValueError("conflicting seed values were provided for XSP")
        seed_registers["XSP"] = xsp_value

    return seed_registers


def _execute_to_dict(result: ExecutionResult) -> dict[str, object]:
    flag_changes = []
    if result.after_cpu is not None:
        for key, label in (
            ("sf", "S"),
            ("zf", "Z"),
            ("vf", "V"),
            ("hf", "H"),
            ("cf", "C"),
        ):
            before_value = getattr(result.before_cpu.flags, key)
            after_value = getattr(result.after_cpu.flags, key)
            if before_value != after_value:
                flag_changes.append(
                    {
                        "name": label,
                        "before": before_value,
                        "after": after_value,
                    }
                )

    return {
        "status": result.status,
        "matched_quirk": _known_quirk_to_dict(result.matched_quirk),
        "written_registers": list(result.written_registers),
        "memory_writes": [
            {
                "address": write.address,
                "address_hex": f"0x{write.address:06X}",
                "size": len(write.data),
                "data": list(write.data),
                "data_hex": write.data.hex(" ").upper(),
                "note": write.note,
            }
            for write in result.memory_writes
        ],
        "after_memory": _memory_overlay_to_rows(result.after_memory),
        "note": result.note,
        "decode": _decode_to_dict(result.decode),
        "before_cpu": _cpu_to_dict(result.before_cpu),
        "after_cpu": None if result.after_cpu is None else _cpu_to_dict(result.after_cpu),
        "changes": [
            {
                "name": name,
                "before_hex": _cpu_named_hex(result.before_cpu, name),
                "after_hex": (
                    None
                    if result.after_cpu is None
                    else _cpu_named_hex(result.after_cpu, name)
                ),
            }
            for name in result.written_registers
        ],
        "flag_changes": flag_changes,
    }


def _irq_delivery_to_dict(delivery: "IrqDeliveryResult | None") -> dict[str, object] | None:
    if delivery is None:
        return None
    return {
        "delivered": delivery.delivered,
        "blocked_reason": delivery.blocked_reason,
        "note": delivery.note,
        "cycles_consumed": delivery.cycles_consumed,
        "vector_slot_address": delivery.vector_slot_address,
        "vector_slot_address_hex": (
            None
            if delivery.vector_slot_address is None
            else f"0x{delivery.vector_slot_address:08X}"
        ),
        "vector_slot_raw": delivery.vector_slot_raw,
        "vector_slot_raw_hex": (
            None
            if delivery.vector_slot_raw is None
            else f"0x{delivery.vector_slot_raw:08X}"
        ),
        "vector_target": delivery.vector_target,
        "vector_target_hex": (
            None
            if delivery.vector_target is None
            else f"0x{delivery.vector_target:08X}"
        ),
        "used_handler_pointer": delivery.used_handler_pointer,
        "used_slot_fallback": delivery.used_slot_fallback,
    }


def _cmd_execute_next(args: argparse.Namespace) -> int:
    seed_registers = _parse_seed_registers(args)
    result = load_execute_next(
        Path(args.rom),
        start_pc=None if args.address is None else _parse_address(args.address),
        seed_registers=seed_registers,
        bios_path=args.bios,
    )
    payload = _execute_to_dict(result)
    if seed_registers:
        payload["seed_registers"] = _seed_registers_to_rows(seed_registers)
    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0

    decode = payload["decode"]  # type: ignore[index]
    print(f"PC: {decode['pc_hex']}")
    if seed_registers:
        print(
            "Seed registers: "
            + ", ".join(f"{name}=0x{value:08X}" for name, value in sorted(seed_registers.items()))
        )
    print(f"Execution status: {payload['status']}")
    print(f"Decode status: {decode['status']}")
    if decode["raw_bytes_hex"] is not None:
        print(f"Bytes: {decode['raw_bytes_hex']}")
    if decode["assembly"] is not None:
        print(f"Decoded: {decode['assembly']}")
    if payload["written_registers"]:
        print("Written registers: " + ", ".join(payload["written_registers"]))  # type: ignore[arg-type]
    else:
        print("Written registers: <none>")
    if decode["warning"] is not None:
        print(f"Warning: {decode['warning']}")
    if payload["matched_quirk"] is not None:
        quirk = payload["matched_quirk"]  # type: ignore[index]
        print(f"Known quirk: {_known_quirk_label(quirk)}")
        print(f"Quirk summary: {quirk['summary']}")
        _print_known_quirk_sources(quirk)
    if payload["changes"]:
        print("CPU changes:")
        for change in payload["changes"]:  # type: ignore[index]
            if change["after_hex"] is None:
                print(f"  {change['name']}: {change['before_hex']} -> <not executed>")
            else:
                print(f"  {change['name']}: {change['before_hex']} -> {change['after_hex']}")
    else:
        print("CPU changes: <none>")
    if payload["flag_changes"]:
        print("Flag changes:")
        for change in payload["flag_changes"]:  # type: ignore[index]
            print(f"  {change['name']}: {change['before']} -> {change['after']}")
    else:
        print("Flag changes: <none>")
    if payload["memory_writes"]:
        print("Memory writes:")
        for write in payload["memory_writes"]:  # type: ignore[index]
            print(
                f"  {write['address_hex']}: {write['data_hex']} "
                f"({write['size']} bytes)"
            )
    else:
        print("Memory writes: <none>")
    print(f"Note: {payload['note']}")
    return 0


def _step_exec_to_dict(result: RunStepsResult) -> dict[str, object]:
    record = result.records[0]
    return {
        "start_pc": result.start_pc,
        "start_pc_hex": f"0x{result.start_pc:08X}",
        "executed_count": result.executed_count,
        "irq_deliveries": result.irq_deliveries,
        "last_irq_delivery": _irq_delivery_to_dict(result.last_irq_delivery),
        "stop_reason": result.stop_reason,
        "final_cpu": _cpu_to_dict(result.final_cpu),
        "final_memory": _memory_overlay_to_rows(result.final_memory),
        "note": result.note,
        "execution": {
            "index": record.index,
            **_execute_to_dict(record.execution),
        },
    }


def _cmd_step_exec(args: argparse.Namespace) -> int:
    seed_registers = _parse_seed_registers(args)
    rom_path = Path(args.rom)

    initial_cpu_state = None
    initial_memory_bytes: dict[int, int] | None = None
    initial_frame_state = None
    initial_irq_state = None
    seed_from_doc: SavestateDocument | None = None
    seed_from_payload = None
    seed_from_doc, seed_from_payload = _load_seed_savestate_from_args(
        rom_path=rom_path,
        seed_from=args.seed_from,
        seed_checkpoint=args.seed_checkpoint,
        seed_session=args.seed_session,
    )
    if seed_from_doc is not None:
        initial_cpu_state = seed_from_doc.cpu
        initial_memory_bytes = dict(seed_from_doc.writable_overlay)
        initial_frame_state = seed_from_doc.frame_state
        initial_irq_state = seed_from_doc.irq_state

    result = load_run_steps(
        rom_path,
        start_pc=None if args.address is None else _parse_address(args.address),
        count=1,
        seed_xsp=None if args.seed_xsp is None else _parse_address(args.seed_xsp),
        seed_registers=seed_registers,
        initial_cpu_state=initial_cpu_state,
        initial_memory_bytes=initial_memory_bytes,
        initial_frame_state=initial_frame_state,
        initial_irq_state=initial_irq_state,
        bios_path=args.bios,
    )
    payload = _step_exec_to_dict(result)
    if seed_registers:
        payload["seed_registers"] = _seed_registers_to_rows(seed_registers)
    if seed_from_payload is not None:
        payload["seed_from"] = seed_from_payload
    execution = payload["execution"]
    assert isinstance(execution, dict)
    save_output_path, save_output_payload = _resolve_save_state_output(
        rom_path=rom_path,
        save_state=args.save_state,
        save_checkpoint=args.save_checkpoint,
        save_session=args.save_session,
    )
    if save_output_path is not None and save_output_payload is not None:
        state_payload = _save_execution_savestate(
            rom_path=rom_path,
            output_path=save_output_path,
            final_cpu=result.final_cpu,
            final_memory=dict(result.final_memory),
            matched_quirk=execution.get("matched_quirk"),
            source_note_suffix=(
                f"derived from step-exec stop_reason={result.stop_reason} "
                f"executed={result.executed_count}"
            ),
            user_note=args.note,
            final_frame_state=_advance_frame_state_for_run(
                initial_frame_state,
                result.executed_count,
                total_cycles_consumed=result.total_cycles_consumed,
            ),
            final_irq_state=result.final_irq_state,
        )
        _finalize_session_frontier_save(
            rom_path=rom_path,
            save_output_payload=save_output_payload,
            last_action="step-exec",
            user_note=args.note,
        )
        payload["saved_state"] = {
            **save_output_payload,
            "format_version": SAVESTATE_FORMAT_VERSION,
            "rom_sha256": state_payload["rom"]["sha256"],  # type: ignore[index]
            "cpu_pc_hex": f"0x{result.final_cpu.pc:08X}",
            "overlay_byte_count": len(result.final_memory),
        }

    symbol_payload = _resolve_pc_symbol_payload(args.map, result.final_cpu.pc)
    if symbol_payload is not None:
        payload["final_symbol"] = symbol_payload

    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0

    print(f"Start PC: {payload['start_pc_hex']}")
    if seed_from_doc is not None:
        seed_label = _seed_source_label(payload["seed_from"])  # type: ignore[arg-type]
        print(
            f"Seed from state: {seed_label} "
            f"(format {seed_from_doc.format_version}, "
            f"CPU PC 0x{seed_from_doc.cpu.pc:08X}, "
            f"{len(seed_from_doc.writable_overlay)} overlay bytes)"
        )
    if seed_registers:
        print(
            "Seed registers: "
            + ", ".join(f"{name}=0x{value:08X}" for name, value in sorted(seed_registers.items()))
        )
    decode = execution["decode"]
    assert isinstance(decode, dict)
    print(f"Execution status: {execution['status']}")
    print(f"Stop reason: {payload['stop_reason']}")
    print(f"Final PC: {payload['final_cpu']['pc_hex']}")  # type: ignore[index]
    print(f"IRQ deliveries: {payload['irq_deliveries']}")
    last_irq_delivery = payload["last_irq_delivery"]
    assert last_irq_delivery is None or isinstance(last_irq_delivery, dict)
    if last_irq_delivery is not None:
        print(
            "Last IRQ delivery: "
            f"slot={last_irq_delivery['vector_slot_address_hex']} "
            f"target={last_irq_delivery['vector_target_hex']} "
            f"fallback={'yes' if last_irq_delivery['used_slot_fallback'] else 'no'}"
        )
    symbol_line = _format_symbol_line(symbol_payload)
    if symbol_line is not None:
        print(symbol_line)
    if decode["raw_bytes_hex"] is not None:
        print(f"Bytes: {decode['raw_bytes_hex']}")
    if decode["assembly"] is not None:
        print(f"Decoded: {decode['assembly']}")
    if execution["written_registers"]:
        print("Written registers: " + ", ".join(execution["written_registers"]))  # type: ignore[arg-type]
    else:
        print("Written registers: <none>")
    if execution["changes"]:
        print("CPU changes:")
        for change in execution["changes"]:  # type: ignore[index]
            if change["after_hex"] is None:
                print(f"  {change['name']}: {change['before_hex']} -> <not executed>")
            else:
                print(f"  {change['name']}: {change['before_hex']} -> {change['after_hex']}")
    else:
        print("CPU changes: <none>")
    if execution["flag_changes"]:
        print("Flag changes:")
        for change in execution["flag_changes"]:  # type: ignore[index]
            print(f"  {change['name']}: {change['before']} -> {change['after']}")
    else:
        print("Flag changes: <none>")
    if execution["memory_writes"]:
        print("Memory writes:")
        for write in execution["memory_writes"]:  # type: ignore[index]
            print(
                f"  {write['address_hex']}: {write['data_hex']} "
                f"({write['size']} bytes)"
            )
    else:
        print("Memory writes: <none>")
    if execution["matched_quirk"] is not None:
        quirk = execution["matched_quirk"]  # type: ignore[index]
        print(f"Known quirk: {_known_quirk_label(quirk)}")
    if payload.get("saved_state") is not None:
        saved_state = payload["saved_state"]  # type: ignore[index]
        print(f"Saved state: {saved_state['path']}")
        print(f"Saved CPU PC: {saved_state['cpu_pc_hex']}")
        print(f"Saved overlay bytes: {saved_state['overlay_byte_count']}")
    print(f"Note: {payload['note']}")
    return 0


def _run_steps_to_dict(result: RunStepsResult) -> dict[str, object]:
    return {
        "start_pc": result.start_pc,
        "start_pc_hex": f"0x{result.start_pc:08X}",
        "requested_count": result.requested_count,
        "emitted_count": result.emitted_count,
        "executed_count": result.executed_count,
        "irq_deliveries": result.irq_deliveries,
        "last_irq_delivery": _irq_delivery_to_dict(result.last_irq_delivery),
        "stop_reason": result.stop_reason,
        "final_cpu": _cpu_to_dict(result.final_cpu),
        "final_memory": _memory_overlay_to_rows(result.final_memory),
        "note": result.note,
        "records": [
            {
                "index": record.index,
                **_execute_to_dict(record.execution),
            }
            for record in result.records
        ],
    }


def _execution_trace_to_dict(result: ExecutionTraceResult) -> dict[str, object]:
    return {
        "start_pc": result.start_pc,
        "start_pc_hex": f"0x{result.start_pc:08X}",
        "requested_count": result.requested_count,
        "emitted_count": result.emitted_count,
        "executed_count": result.executed_count,
        "irq_deliveries": result.irq_deliveries,
        "last_irq_delivery": _irq_delivery_to_dict(result.last_irq_delivery),
        "stop_reason": result.stop_reason,
        "final_cpu": _cpu_to_dict(result.final_cpu),
        "final_memory": _memory_overlay_to_rows(result.final_memory),
        "note": result.note,
        "records": [
            {
                "index": record.index,
                **_execute_to_dict(record.execution),
            }
            for record in result.records
        ],
    }


def _advance_frame_state_for_run(
    initial_frame_state: "FrameState | None",
    executed_count: int,
    *,
    total_cycles_consumed: int | None = None,
) -> "FrameState | None":
    """M3 Phase 3.2.0/3.2.1/3.2.3a: advance `frame_state` by the run's
    cycle cost.

    Two calling modes:
    - **Cycle total mode** (Phase 3.2.3a, preferred): pass
      `total_cycles_consumed` from `run_result.total_cycles_consumed`.
      Each executed instruction + each IRQ delivery contributes its
      own cycle count to the total. Unpopulated instructions still
      fall back to `ESTIMATED_CYCLES_PER_INSTRUCTION`, selected common
      opcodes now override it with real TLCS-900 timing, and every IRQ
      entry contributes `IRQ_DELIVERY_CYCLES = 13`.
    - **Executed-count fallback** (Phase 3.2.0/3.2.1): when
      `total_cycles_consumed` is None, multiply `executed_count` by
      `ESTIMATED_CYCLES_PER_INSTRUCTION` (the original flat model).
      Kept for callers that don't have a `total_cycles_consumed`
      handy — currently used by the bootstrap-only `_cmd_savestate_save`
      path that doesn't gather a run result.

    When the caller didn't seed a frame_state, we start from the HW
    reset (`initial_frame_state()`) so the emitted output savestate
    still carries advancement from bootstrap. Zero/negative input
    returns the seed (or initial) unchanged.
    """
    start = (
        initial_frame_state if initial_frame_state is not None
        else initial_frame_state_func()
    )
    if total_cycles_consumed is not None:
        elapsed_cycles = max(0, total_cycles_consumed)
    elif executed_count > 0:
        elapsed_cycles = executed_count * ESTIMATED_CYCLES_PER_INSTRUCTION
    else:
        return start
    if elapsed_cycles == 0:
        return start
    return advance_frame_state_by_cycles(start, elapsed_cycles)


# Local alias to avoid the name clash between `initial_frame_state` (the
# variable used throughout the CLI handlers) and the helper function
# that returns the documented HW reset state.
initial_frame_state_func = initial_frame_state


def _save_execution_savestate(
    *,
    rom_path: Path,
    output_path: Path,
    final_cpu: NgpcCpuState,
    final_memory: dict[int, int],
    matched_quirk: object | None,
    source_note_suffix: str,
    user_note: str | None,
    final_frame_state: "FrameState | None" = None,
    final_irq_state: "IrqState | None" = None,
) -> dict[str, object]:
    """Persist one execution result as a savestate v3 payload and return it.

    `final_frame_state` carries the seed's `frame_state` forward into the
    emitted savestate so step-exec / run-steps / trace-exec chained
    commands preserve timing position. Phase 3.2.0/3.2.1 advances it
    per executed instruction via `_advance_frame_state_for_run`.

    `final_irq_state` (Phase 3.2.2b) carries the IRQ pending mask
    forward — the executor's IRQ delivery sampler updates this during
    the run when an IRQ is accepted (bit cleared). None means no
    explicit irq_state was tracked ; the payload defaults to
    `initial_irq_state()`.
    """
    machine = load_machine_state(rom_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    note_pieces = [source_note_suffix]
    if user_note:
        note_pieces.insert(0, user_note)
    final_note: str | None = " | ".join(note_pieces) if note_pieces else None
    payload = build_savestate_payload(
        rom_path=rom_path,
        rom_header=machine.header,
        cpu=final_cpu,
        writable_overlay=final_memory,
        matched_on_last_step=matched_quirk,
        note=final_note,
        frame_state=final_frame_state,
        irq_state=final_irq_state,
    )
    save_savestate(output_path, payload)
    return payload


def _cmd_run_steps(args: argparse.Namespace) -> int:
    seed_registers = _parse_seed_registers(args)
    rom_path = Path(args.rom)

    initial_cpu_state = None
    initial_memory_bytes: dict[int, int] | None = None
    initial_frame_state = None
    initial_irq_state = None
    seed_from_doc: SavestateDocument | None = None
    seed_from_payload = None
    seed_from_doc, seed_from_payload = _load_seed_savestate_from_args(
        rom_path=rom_path,
        seed_from=args.seed_from,
        seed_checkpoint=args.seed_checkpoint,
        seed_session=args.seed_session,
    )
    if seed_from_doc is not None:
        initial_cpu_state = seed_from_doc.cpu
        initial_memory_bytes = dict(seed_from_doc.writable_overlay)
        initial_frame_state = seed_from_doc.frame_state
        initial_irq_state = seed_from_doc.irq_state

    result = load_run_steps(
        rom_path,
        start_pc=None if args.address is None else _parse_address(args.address),
        count=args.count,
        seed_xsp=None if args.seed_xsp is None else _parse_address(args.seed_xsp),
        seed_registers=seed_registers,
        initial_cpu_state=initial_cpu_state,
        initial_memory_bytes=initial_memory_bytes,
        initial_frame_state=initial_frame_state,
        initial_irq_state=initial_irq_state,
        bios_path=args.bios,
    )
    payload = _run_steps_to_dict(result)
    if seed_registers:
        payload["seed_registers"] = _seed_registers_to_rows(seed_registers)
    if seed_from_payload is not None:
        payload["seed_from"] = seed_from_payload
    save_output_path, save_output_payload = _resolve_save_state_output(
        rom_path=rom_path,
        save_state=args.save_state,
        save_checkpoint=args.save_checkpoint,
        save_session=args.save_session,
    )
    if save_output_path is not None and save_output_payload is not None:
        matched_quirk = None
        if result.records:
            matched_quirk = result.records[-1].execution.matched_quirk
        state_payload = _save_execution_savestate(
            rom_path=rom_path,
            output_path=save_output_path,
            final_cpu=result.final_cpu,
            final_memory=dict(result.final_memory),
            matched_quirk=matched_quirk,
            source_note_suffix=(
                f"derived from run-steps count={args.count} "
                f"stop_reason={result.stop_reason} executed={result.executed_count}"
            ),
            user_note=args.note,
            final_frame_state=_advance_frame_state_for_run(
                initial_frame_state,
                result.executed_count,
                total_cycles_consumed=result.total_cycles_consumed,
            ),
            final_irq_state=result.final_irq_state,
        )
        _finalize_session_frontier_save(
            rom_path=rom_path,
            save_output_payload=save_output_payload,
            last_action="run-steps",
            user_note=args.note,
        )
        payload["saved_state"] = {
            **save_output_payload,
            "format_version": SAVESTATE_FORMAT_VERSION,
            "rom_sha256": state_payload["rom"]["sha256"],  # type: ignore[index]
            "cpu_pc_hex": f"0x{result.final_cpu.pc:08X}",
            "overlay_byte_count": len(result.final_memory),
        }

    symbol_payload = _resolve_pc_symbol_payload(args.map, result.final_cpu.pc)
    if symbol_payload is not None:
        payload["final_symbol"] = symbol_payload

    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0

    print(f"Start PC: {payload['start_pc_hex']}")
    if seed_from_doc is not None:
        seed_label = _seed_source_label(payload["seed_from"])  # type: ignore[arg-type]
        print(
            f"Seed from state: {seed_label} "
            f"(format {seed_from_doc.format_version}, "
            f"CPU PC 0x{seed_from_doc.cpu.pc:08X}, "
            f"{len(seed_from_doc.writable_overlay)} overlay bytes)"
        )
    if seed_registers:
        print(
            "Seed registers: "
            + ", ".join(f"{name}=0x{value:08X}" for name, value in sorted(seed_registers.items()))
        )
    print(f"Requested steps: {payload['requested_count']}")
    print(f"Emitted records: {payload['emitted_count']}")
    print(f"Executed records: {payload['executed_count']}")
    print(f"IRQ deliveries: {payload['irq_deliveries']}")
    print(f"Stop reason: {payload['stop_reason']}")
    print(f"Final PC: {payload['final_cpu']['pc_hex']}")
    last_irq_delivery = payload["last_irq_delivery"]
    assert last_irq_delivery is None or isinstance(last_irq_delivery, dict)
    if last_irq_delivery is not None:
        print(
            "Last IRQ delivery: "
            f"slot={last_irq_delivery['vector_slot_address_hex']} "
            f"target={last_irq_delivery['vector_target_hex']} "
            f"fallback={'yes' if last_irq_delivery['used_slot_fallback'] else 'no'}"
        )
    symbol_line = _format_symbol_line(symbol_payload)
    if symbol_line is not None:
        print(symbol_line)
    print("Run:")
    for record in payload["records"]:  # type: ignore[index]
        decode = record["decode"]
        line = (
            f"  [{record['index']}] {decode['pc_hex']} "
            f"{decode['raw_bytes_hex'] or '<none>'}"
        )
        if decode["assembly"] is not None:
            line += f"  {decode['assembly']}"
        else:
            line += f"  <{decode['status']}>"
        line += f"  => {record['status']}"
        print(line)
        if record["written_registers"]:
            print("    writes: " + ", ".join(record["written_registers"]))  # type: ignore[arg-type]
        if record["changes"]:
            for change in record["changes"]:  # type: ignore[index]
                if change["after_hex"] is None:
                    print(
                        f"    {change['name']}: {change['before_hex']} -> <not executed>"
                    )
                else:
                    print(
                        f"    {change['name']}: {change['before_hex']} -> {change['after_hex']}"
                    )
        if record["flag_changes"]:
            for change in record["flag_changes"]:  # type: ignore[index]
                print(f"    flag {change['name']}: {change['before']} -> {change['after']}")
        if record["memory_writes"]:
            for write in record["memory_writes"]:  # type: ignore[index]
                print(f"    mem {write['address_hex']}: {write['data_hex']}")
        if decode["warning"] is not None:
            print(f"    warning: {decode['warning']}")
        if record["matched_quirk"] is not None:
            quirk = record["matched_quirk"]  # type: ignore[index]
            print(f"    quirk: {_known_quirk_label(quirk)}")
    print(f"Note: {payload['note']}")
    if payload.get("saved_state") is not None:
        saved_state = payload["saved_state"]  # type: ignore[index]
        print(f"Saved state: {saved_state['path']}")
        print(f"Saved CPU PC: {saved_state['cpu_pc_hex']}")
        print(f"Saved overlay bytes: {saved_state['overlay_byte_count']}")
    return 0


def _cmd_trace_exec(args: argparse.Namespace) -> int:
    seed_registers = _parse_seed_registers(args)
    rom_path = Path(args.rom)

    initial_cpu_state = None
    initial_memory_bytes: dict[int, int] | None = None
    initial_frame_state = None
    initial_irq_state = None
    seed_from_doc: SavestateDocument | None = None
    seed_from_payload = None
    seed_from_doc, seed_from_payload = _load_seed_savestate_from_args(
        rom_path=rom_path,
        seed_from=args.seed_from,
        seed_checkpoint=args.seed_checkpoint,
        seed_session=args.seed_session,
    )
    if seed_from_doc is not None:
        initial_cpu_state = seed_from_doc.cpu
        initial_memory_bytes = dict(seed_from_doc.writable_overlay)
        initial_frame_state = seed_from_doc.frame_state
        initial_irq_state = seed_from_doc.irq_state

    result = load_execution_trace(
        rom_path,
        start_pc=None if args.address is None else _parse_address(args.address),
        count=args.count,
        seed_xsp=None if args.seed_xsp is None else _parse_address(args.seed_xsp),
        seed_registers=seed_registers,
        initial_cpu_state=initial_cpu_state,
        initial_memory_bytes=initial_memory_bytes,
        initial_frame_state=initial_frame_state,
        initial_irq_state=initial_irq_state,
    )
    payload = _execution_trace_to_dict(result)
    if seed_registers:
        payload["seed_registers"] = _seed_registers_to_rows(seed_registers)
    if seed_from_payload is not None:
        payload["seed_from"] = seed_from_payload
    save_output_path, save_output_payload = _resolve_save_state_output(
        rom_path=rom_path,
        save_state=args.save_state,
        save_checkpoint=args.save_checkpoint,
        save_session=args.save_session,
    )
    if save_output_path is not None and save_output_payload is not None:
        matched_quirk = None
        if result.records:
            matched_quirk = result.records[-1].execution.matched_quirk
        state_payload = _save_execution_savestate(
            rom_path=rom_path,
            output_path=save_output_path,
            final_cpu=result.final_cpu,
            final_memory=dict(result.final_memory),
            matched_quirk=matched_quirk,
            source_note_suffix=(
                f"derived from trace-exec count={args.count} "
                f"stop_reason={result.stop_reason} executed={result.executed_count}"
            ),
            user_note=args.note,
            final_frame_state=_advance_frame_state_for_run(
                initial_frame_state,
                result.executed_count,
                total_cycles_consumed=result.total_cycles_consumed,
            ),
            final_irq_state=result.final_irq_state,
        )
        _finalize_session_frontier_save(
            rom_path=rom_path,
            save_output_payload=save_output_payload,
            last_action="trace-exec",
            user_note=args.note,
        )
        payload["saved_state"] = {
            **save_output_payload,
            "format_version": SAVESTATE_FORMAT_VERSION,
            "rom_sha256": state_payload["rom"]["sha256"],  # type: ignore[index]
            "cpu_pc_hex": f"0x{result.final_cpu.pc:08X}",
            "overlay_byte_count": len(result.final_memory),
        }

    symbol_payload = _resolve_pc_symbol_payload(args.map, result.final_cpu.pc)
    if symbol_payload is not None:
        payload["final_symbol"] = symbol_payload

    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0

    print(f"Start PC: {payload['start_pc_hex']}")
    if seed_from_doc is not None:
        seed_label = _seed_source_label(payload["seed_from"])  # type: ignore[arg-type]
        print(
            f"Seed from state: {seed_label} "
            f"(format {seed_from_doc.format_version}, "
            f"CPU PC 0x{seed_from_doc.cpu.pc:08X}, "
            f"{len(seed_from_doc.writable_overlay)} overlay bytes)"
        )
    if seed_registers:
        print(
            "Seed registers: "
            + ", ".join(f"{name}=0x{value:08X}" for name, value in sorted(seed_registers.items()))
        )
    print(f"Requested records: {payload['requested_count']}")
    print(f"Emitted records: {payload['emitted_count']}")
    print(f"Executed records: {payload['executed_count']}")
    print(f"IRQ deliveries: {payload['irq_deliveries']}")
    print(f"Stop reason: {payload['stop_reason']}")
    print(f"Final PC: {payload['final_cpu']['pc_hex']}")
    last_irq_delivery = payload["last_irq_delivery"]
    assert last_irq_delivery is None or isinstance(last_irq_delivery, dict)
    if last_irq_delivery is not None:
        print(
            "Last IRQ delivery: "
            f"slot={last_irq_delivery['vector_slot_address_hex']} "
            f"target={last_irq_delivery['vector_target_hex']} "
            f"fallback={'yes' if last_irq_delivery['used_slot_fallback'] else 'no'}"
        )
    symbol_line = _format_symbol_line(symbol_payload)
    if symbol_line is not None:
        print(symbol_line)
    print("Execution trace:")
    for record in payload["records"]:  # type: ignore[index]
        decode = record["decode"]
        line = (
            f"  [{record['index']}] {decode['pc_hex']} "
            f"{decode['raw_bytes_hex'] or '<none>'}"
        )
        if decode["assembly"] is not None:
            line += f"  {decode['assembly']}"
        else:
            line += f"  <{decode['status']}>"
        line += f"  => {record['status']}"
        print(line)
        if record["written_registers"]:
            print("    writes: " + ", ".join(record["written_registers"]))  # type: ignore[arg-type]
        if record["changes"]:
            for change in record["changes"]:  # type: ignore[index]
                if change["after_hex"] is None:
                    print(
                        f"    {change['name']}: {change['before_hex']} -> <not executed>"
                    )
                else:
                    print(
                        f"    {change['name']}: {change['before_hex']} -> {change['after_hex']}"
                    )
        if record["flag_changes"]:
            for change in record["flag_changes"]:  # type: ignore[index]
                print(f"    flag {change['name']}: {change['before']} -> {change['after']}")
        if record["memory_writes"]:
            for write in record["memory_writes"]:  # type: ignore[index]
                print(f"    mem {write['address_hex']}: {write['data_hex']}")
        if decode["warning"] is not None:
            print(f"    warning: {decode['warning']}")
        if record["matched_quirk"] is not None:
            quirk = record["matched_quirk"]  # type: ignore[index]
            print(f"    quirk: {_known_quirk_label(quirk)}")
    if payload.get("saved_state") is not None:
        saved_state = payload["saved_state"]  # type: ignore[index]
        print(f"Saved state: {saved_state['path']}")
        print(f"Saved CPU PC: {saved_state['cpu_pc_hex']}")
        print(f"Saved overlay bytes: {saved_state['overlay_byte_count']}")
    print(f"Note: {payload['note']}")
    return 0


def _run_until_exec_to_dict(result: RunUntilResult) -> dict[str, object]:
    last = None
    if result.last_record is not None:
        last = {
            "index": result.last_record.index,
            **_execute_to_dict(result.last_record.execution),
        }
    return {
        "start_pc": result.start_pc,
        "start_pc_hex": f"0x{result.start_pc:08X}",
        "target_pc": result.target_pc,
        "target_pc_hex": f"0x{result.target_pc:08X}",
        "executed_count": result.executed_count,
        "irq_deliveries": result.irq_deliveries,
        "last_irq_delivery": _irq_delivery_to_dict(result.last_irq_delivery),
        "stop_reason": result.stop_reason,
        "final_cpu": _cpu_to_dict(result.final_cpu),
        "final_memory_size": len(result.final_memory),
        "last_record": last,
        "note": result.note,
    }


def _cmd_run_until_exec(args: argparse.Namespace) -> int:
    seed_registers = _parse_seed_registers(args)
    target_pc = _parse_address(args.target)
    rom_path = Path(args.rom)

    initial_cpu_state = None
    initial_memory_bytes: dict[int, int] | None = None
    initial_frame_state = None
    initial_irq_state = None
    seed_from_doc: SavestateDocument | None = None
    seed_from_payload = None
    seed_from_doc, seed_from_payload = _load_seed_savestate_from_args(
        rom_path=rom_path,
        seed_from=args.seed_from,
        seed_checkpoint=args.seed_checkpoint,
        seed_session=args.seed_session,
    )
    if seed_from_doc is not None:
        initial_cpu_state = seed_from_doc.cpu
        initial_memory_bytes = dict(seed_from_doc.writable_overlay)
        initial_frame_state = seed_from_doc.frame_state
        initial_irq_state = seed_from_doc.irq_state

    auto_tick_address, auto_tick_payload = _parse_auto_tick_args(args)
    auto_tick_note_suffix = _format_auto_tick_note_suffix(auto_tick_payload)
    result = load_run_until(
        rom_path,
        target_pc=target_pc,
        start_pc=None if args.address is None else _parse_address(args.address),
        seed_registers=seed_registers,
        max_steps=args.max_steps,
        initial_cpu_state=initial_cpu_state,
        initial_memory_bytes=initial_memory_bytes,
        auto_tick_address=auto_tick_address,
        auto_tick_period=args.auto_tick_period,
        initial_frame_state=initial_frame_state,
        initial_irq_state=initial_irq_state,
        bios_path=getattr(args, "bios", None),
    )
    payload = _run_until_exec_to_dict(result)
    if seed_registers:
        payload["seed_registers"] = _seed_registers_to_rows(seed_registers)
    if seed_from_payload is not None:
        payload["seed_from"] = seed_from_payload
    if auto_tick_payload is not None:
        payload["non_reference"] = auto_tick_payload
    save_output_path, save_output_payload = _resolve_save_state_output(
        rom_path=rom_path,
        save_state=args.save_state,
        save_checkpoint=args.save_checkpoint,
        save_session=args.save_session,
    )
    if save_output_path is not None and save_output_payload is not None:
        matched_quirk = None
        if result.last_record is not None:
            matched_quirk = result.last_record.execution.matched_quirk
        state_payload = _save_execution_savestate(
            rom_path=rom_path,
            output_path=save_output_path,
            final_cpu=result.final_cpu,
            final_memory=dict(result.final_memory),
            matched_quirk=matched_quirk,
            source_note_suffix=(
                f"derived from run-until-exec target=0x{target_pc:08X} "
                f"stop_reason={result.stop_reason} executed={result.executed_count}"
                + ("" if auto_tick_note_suffix is None else f" | {auto_tick_note_suffix}")
            ),
            user_note=args.note,
            final_frame_state=_advance_frame_state_for_run(
                initial_frame_state,
                result.executed_count,
                total_cycles_consumed=result.total_cycles_consumed,
            ),
            final_irq_state=result.final_irq_state,
        )
        _finalize_session_frontier_save(
            rom_path=rom_path,
            save_output_payload=save_output_payload,
            last_action="run-until-exec",
            user_note=args.note,
        )
        payload["saved_state"] = {
            **save_output_payload,
            "format_version": SAVESTATE_FORMAT_VERSION,
            "rom_sha256": state_payload["rom"]["sha256"],  # type: ignore[index]
            "cpu_pc_hex": f"0x{result.final_cpu.pc:08X}",
            "overlay_byte_count": len(result.final_memory),
        }

    symbol_payload = _resolve_pc_symbol_payload(args.map, result.final_cpu.pc)
    if symbol_payload is not None:
        payload["final_symbol"] = symbol_payload

    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0

    print(f"Start PC: {payload['start_pc_hex']}")
    print(f"Target PC: {payload['target_pc_hex']}")
    if seed_from_doc is not None:
        seed_label = _seed_source_label(payload["seed_from"])  # type: ignore[arg-type]
        print(
            f"Seed from state: {seed_label} "
            f"(format {seed_from_doc.format_version}, "
            f"CPU PC 0x{seed_from_doc.cpu.pc:08X}, "
            f"{len(seed_from_doc.writable_overlay)} overlay bytes)"
        )
    if seed_registers:
        print(
            "Seed registers: "
            + ", ".join(f"{name}=0x{value:08X}" for name, value in sorted(seed_registers.items()))
        )
    _print_auto_tick_summary(auto_tick_payload)
    print(f"Executed steps: {payload['executed_count']}")
    print(f"IRQ deliveries: {payload['irq_deliveries']}")
    print(f"Stop reason: {payload['stop_reason']}")
    print(f"Final PC: {payload['final_cpu']['pc_hex']}")  # type: ignore[index]
    last_irq_delivery = payload["last_irq_delivery"]
    assert last_irq_delivery is None or isinstance(last_irq_delivery, dict)
    if last_irq_delivery is not None:
        print(
            "Last IRQ delivery: "
            f"slot={last_irq_delivery['vector_slot_address_hex']} "
            f"target={last_irq_delivery['vector_target_hex']} "
            f"fallback={'yes' if last_irq_delivery['used_slot_fallback'] else 'no'}"
        )
    symbol_line = _format_symbol_line(symbol_payload)
    if symbol_line is not None:
        print(symbol_line)
    if payload["last_record"] is not None:
        rec = payload["last_record"]
        decode = rec["decode"]  # type: ignore[index]
        line = (
            f"Last instruction: {decode['pc_hex']} "
            f"{decode['raw_bytes_hex'] or '<none>'}"
        )
        if decode["assembly"] is not None:
            line += f"  {decode['assembly']}"
        else:
            line += f"  <{decode['status']}>"
        line += f"  => {rec['status']}"
        print(line)
        if rec["written_registers"]:
            print("  writes: " + ", ".join(rec["written_registers"]))  # type: ignore[arg-type]
        if rec["changes"]:
            for change in rec["changes"]:  # type: ignore[index]
                print(f"  {change['name']}: {change['before_hex']} -> {change['after_hex']}")
        if rec["flag_changes"]:
            for change in rec["flag_changes"]:  # type: ignore[index]
                print(f"  flag {change['name']}: {change['before']} -> {change['after']}")
        if rec["matched_quirk"] is not None:
            quirk = rec["matched_quirk"]  # type: ignore[index]
            print(f"  quirk: {_known_quirk_label(quirk)}")
    if payload.get("saved_state") is not None:
        saved_state = payload["saved_state"]  # type: ignore[index]
        print(f"Saved state: {saved_state['path']}")
        print(f"Saved CPU PC: {saved_state['cpu_pc_hex']}")
        print(f"Saved overlay bytes: {saved_state['overlay_byte_count']}")
    print(f"Note: {payload['note']}")
    return 0


def _step_to_dict(preview: StepPreview) -> dict[str, object]:
    return {
        "mode": preview.mode,
        "preview_target": preview.preview_target,
        "preview_target_hex": (
            None if preview.preview_target is None else f"0x{preview.preview_target:08X}"
        ),
        "reason": preview.reason,
        "note": preview.note,
        "decode": _decode_to_dict(preview.decode),
    }


def _cmd_step_preview(args: argparse.Namespace) -> int:
    preview = load_step_preview(
        Path(args.rom),
        start_pc=None if args.address is None else _parse_address(args.address),
        mode="into",
    )
    payload = _step_to_dict(preview)
    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0

    decode = payload["decode"]  # type: ignore[index]
    print(f"PC: {decode['pc_hex']}")
    print(f"Status: {decode['status']}")
    if decode["raw_bytes_hex"] is not None:
        print(f"Bytes: {decode['raw_bytes_hex']}")
    if decode["assembly"] is not None:
        print(f"Decoded: {decode['assembly']}")
    if decode["control_flow_kind"] is not None:
        print(f"Control flow: {decode['control_flow_kind']}")
    if decode["direct_target_hex"] is not None:
        print(f"Direct target: {decode['direct_target_hex']}")
    print(f"Mode: {payload['mode']}")
    if payload["preview_target_hex"] is not None:
        print(f"Step preview target: {payload['preview_target_hex']}")
    else:
        print("Step preview target: <unresolved>")
    print(f"Reason: {payload['reason']}")
    if decode["warning"] is not None:
        print(f"Warning: {decode['warning']}")
    if decode["matched_quirk"] is not None:
        quirk = decode["matched_quirk"]  # type: ignore[index]
        print(f"Known quirk: {_known_quirk_label(quirk)}")
        print(f"Quirk summary: {quirk['summary']}")
        _print_known_quirk_sources(quirk)
    print(f"Note: {payload['note']}")
    return 0


def _cmd_next_preview(args: argparse.Namespace) -> int:
    preview = load_step_preview(
        Path(args.rom),
        start_pc=None if args.address is None else _parse_address(args.address),
        mode="over",
    )
    payload = _step_to_dict(preview)
    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0

    decode = payload["decode"]  # type: ignore[index]
    print(f"PC: {decode['pc_hex']}")
    print(f"Status: {decode['status']}")
    if decode["raw_bytes_hex"] is not None:
        print(f"Bytes: {decode['raw_bytes_hex']}")
    if decode["assembly"] is not None:
        print(f"Decoded: {decode['assembly']}")
    if decode["control_flow_kind"] is not None:
        print(f"Control flow: {decode['control_flow_kind']}")
    if decode["direct_target_hex"] is not None:
        print(f"Direct target: {decode['direct_target_hex']}")
    print(f"Mode: {payload['mode']}")
    if payload["preview_target_hex"] is not None:
        print(f"Next preview target: {payload['preview_target_hex']}")
    else:
        print("Next preview target: <unresolved>")
    print(f"Reason: {payload['reason']}")
    if decode["warning"] is not None:
        print(f"Warning: {decode['warning']}")
    if decode["matched_quirk"] is not None:
        quirk = decode["matched_quirk"]  # type: ignore[index]
        print(f"Known quirk: {_known_quirk_label(quirk)}")
        print(f"Quirk summary: {quirk['summary']}")
        _print_known_quirk_sources(quirk)
    print(f"Note: {payload['note']}")
    return 0


def _run_until_to_dict(preview: RunUntilPreview) -> dict[str, object]:
    return {
        "start_pc": preview.start_pc,
        "start_pc_hex": f"0x{preview.start_pc:08X}",
        "target_pc": preview.target_pc,
        "target_pc_hex": f"0x{preview.target_pc:08X}",
        "mode": preview.mode,
        "max_steps": preview.max_steps,
        "emitted_count": preview.emitted_count,
        "stop_reason": preview.stop_reason,
        "reached_target": preview.reached_target,
        "terminal_pc": preview.terminal_pc,
        "terminal_pc_hex": (
            None if preview.terminal_pc is None else f"0x{preview.terminal_pc:08X}"
        ),
        "note": preview.note,
        "records": [
            {
                "index": record.index,
                **_step_to_dict(record.step),
            }
            for record in preview.records
        ],
    }


def _cmd_run_until_preview(args: argparse.Namespace) -> int:
    preview = load_run_until_preview(
        Path(args.rom),
        target_pc=_parse_address(args.target),
        start_pc=None if args.address is None else _parse_address(args.address),
        max_steps=args.max_steps,
        mode=args.mode,
    )
    payload = _run_until_to_dict(preview)
    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0

    print(f"Start PC: {payload['start_pc_hex']}")
    print(f"Target PC: {payload['target_pc_hex']}")
    print(f"Mode: {payload['mode']}")
    print(f"Max steps: {payload['max_steps']}")
    print(f"Emitted previews: {payload['emitted_count']}")
    print(f"Reached target: {payload['reached_target']}")
    print(f"Stop reason: {payload['stop_reason']}")
    if payload["terminal_pc_hex"] is not None:
        print(f"Terminal PC: {payload['terminal_pc_hex']}")
    print("Run-until path:")
    for record in payload["records"]:  # type: ignore[index]
        decode = record["decode"]
        line = (
            f"  [{record['index']}] {decode['pc_hex']} "
            f"{decode['raw_bytes_hex'] or '<none>'}"
        )
        if decode["assembly"] is not None:
            line += f"  {decode['assembly']}"
        else:
            line += f"  <{decode['status']}>"
        if record["preview_target_hex"] is not None:
            line += f"  -> {record['preview_target_hex']}"
        else:
            line += "  -> <unresolved>"
        print(line)
        print(f"    reason: {record['reason']}")
        if decode["warning"] is not None:
            print(f"    warning: {decode['warning']}")
        if decode["matched_quirk"] is not None:
            quirk = decode["matched_quirk"]  # type: ignore[index]
            print(f"    quirk: {_known_quirk_label(quirk)}")
    print(f"Note: {payload['note']}")
    return 0


def _trace_to_dict(trace: TracePreview) -> dict[str, object]:
    return {
        "start_pc": trace.start_pc,
        "start_pc_hex": f"0x{trace.start_pc:08X}",
        "requested_count": trace.requested_count,
        "emitted_count": trace.emitted_count,
        "stop_reason": trace.stop_reason,
        "note": trace.note,
        "records": [
            {
                "index": record.index,
                **_decode_to_dict(record.decode),
            }
            for record in trace.records
        ],
    }


def _cmd_trace_preview(args: argparse.Namespace) -> int:
    trace = load_trace_preview(
        Path(args.rom),
        count=args.count,
        start_pc=None if args.address is None else _parse_address(args.address),
        stop_on_control_flow=args.stop_on_control_flow,
    )
    payload = _trace_to_dict(trace)
    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0

    print(f"Start PC: {payload['start_pc_hex']}")
    print(f"Requested instructions: {payload['requested_count']}")
    print(f"Emitted instructions: {payload['emitted_count']}")
    print(f"Stop reason: {payload['stop_reason']}")
    print("Trace:")
    for record in payload["records"]:  # type: ignore[index]
        line = (
            f"  [{record['index']}] {record['pc_hex']} "
            f"{record['raw_bytes_hex'] or '<none>'}"
        )
        if record["assembly"] is not None:
            line += f"  {record['assembly']}"
        else:
            line += f"  <{record['status']}>"
        print(line)
        if record["warning"] is not None:
            print(f"    warning: {record['warning']}")
        if record["matched_quirk"] is not None:
            quirk = record["matched_quirk"]  # type: ignore[index]
            print(f"    quirk: {_known_quirk_label(quirk)}")
    print(f"Note: {payload['note']}")
    return 0


def _hex_or_none(value: int | None, width: int = 8) -> str | None:
    if value is None:
        return None
    return f"0x{value:0{width}X}"


def _cpu_control_register_rows(cpu: NgpcCpuState) -> list[dict[str, object]]:
    control = cpu.control_registers
    rows: list[dict[str, object]] = []

    def _append_group(
        base_code: int,
        values: tuple[int | None, ...],
        *,
        size: str,
        width: int,
        step: int,
    ) -> None:
        for index, value in enumerate(values):
            code = base_code + (index * step)
            rows.append(
                {
                    "name": control_register_name(code),
                    "size": size,
                    "value": value,
                    "value_hex": _hex_or_none(value, width),
                }
            )

    _append_group(
        0x00,
        (None, None, None, None) if control is None else control.dmas,
        size="long",
        width=8,
        step=0x04,
    )
    _append_group(
        0x10,
        (None, None, None, None) if control is None else control.dmad,
        size="long",
        width=8,
        step=0x04,
    )
    _append_group(
        0x20,
        (None, None, None, None) if control is None else control.dmac,
        size="word",
        width=4,
        step=0x04,
    )
    _append_group(
        0x22,
        (None, None, None, None) if control is None else control.dmam,
        size="byte",
        width=2,
        step=0x04,
    )
    rows.append(
        {
            "name": control_register_name(0x30),
            "size": "word",
            "value": None if control is None else control.intnest,
            "value_hex": _hex_or_none(None if control is None else control.intnest, 4),
        }
    )
    return rows


def _cpu_to_dict(cpu: NgpcCpuState) -> dict[str, object]:

    return {
        "pc": cpu.pc,
        "pc_hex": f"0x{cpu.pc:08X}",
        "sr_raw": cpu.sr_raw,
        "sr_raw_hex": _hex_or_none(cpu.sr_raw, 4),
        "register_bank": cpu.register_bank,
        "rfp": cpu.rfp,
        "modeled_fields": list(cpu.modeled_fields),
        "flags": {
            "sf": cpu.flags.sf,
            "zf": cpu.flags.zf,
            "vf": cpu.flags.vf,
            "hf": cpu.flags.hf,
            "cf": cpu.flags.cf,
            "nf": cpu.flags.nf,
        },
        "alt_flags": {
            "sf": None if cpu.alt_flags is None else cpu.alt_flags.sf,
            "zf": None if cpu.alt_flags is None else cpu.alt_flags.zf,
            "vf": None if cpu.alt_flags is None else cpu.alt_flags.vf,
            "hf": None if cpu.alt_flags is None else cpu.alt_flags.hf,
            "cf": None if cpu.alt_flags is None else cpu.alt_flags.cf,
            "nf": None if cpu.alt_flags is None else cpu.alt_flags.nf,
        },
        "registers": {
            "xwa": cpu.regs.xwa,
            "xwa_hex": _hex_or_none(cpu.regs.xwa),
            "xbc": cpu.regs.xbc,
            "xbc_hex": _hex_or_none(cpu.regs.xbc),
            "xde": cpu.regs.xde,
            "xde_hex": _hex_or_none(cpu.regs.xde),
            "xhl": cpu.regs.xhl,
            "xhl_hex": _hex_or_none(cpu.regs.xhl),
            "xix": cpu.regs.xix,
            "xix_hex": _hex_or_none(cpu.regs.xix),
            "xiy": cpu.regs.xiy,
            "xiy_hex": _hex_or_none(cpu.regs.xiy),
            "xiz": cpu.regs.xiz,
            "xiz_hex": _hex_or_none(cpu.regs.xiz),
            "xsp": cpu.regs.xsp,
            "xsp_hex": _hex_or_none(cpu.regs.xsp),
        },
        "control_registers": _cpu_control_register_rows(cpu),
        "iff_enabled": cpu.iff_enabled,
        "iff_level": cpu.iff_level,
        "note": cpu.note,
    }


# Mapping from 32-bit register to (high16 name, low16 name, high8 name, low8 name).
# TLCS-900 naming convention: XWA = 32-bit; WA = low 16-bit half; A = low byte,
# W = high byte of WA. Same shape for XBC -> BC -> C/B, etc.
_REG32_DECOMPOSITION = {
    "XWA": ("WA", "W", "A"),
    "XBC": ("BC", "B", "C"),
    "XDE": ("DE", "D", "E"),
    "XHL": ("HL", "H", "L"),
    "XIX": ("IX", None, None),
    "XIY": ("IY", None, None),
    "XIZ": ("IZ", None, None),
    "XSP": ("SP", None, None),
}


def _decompose_register_32(value: int | None) -> dict[str, object]:
    """Return the low-16 / high-8 / low-8 view of a 32-bit register value."""
    if value is None:
        return {
            "long": None,
            "long_hex": None,
            "word_low": None,
            "word_low_hex": None,
            "byte_high": None,
            "byte_high_hex": None,
            "byte_low": None,
            "byte_low_hex": None,
        }
    word_low = value & 0xFFFF
    byte_high = (value >> 8) & 0xFF
    byte_low = value & 0xFF
    return {
        "long": value,
        "long_hex": f"0x{value:08X}",
        "word_low": word_low,
        "word_low_hex": f"0x{word_low:04X}",
        "byte_high": byte_high,
        "byte_high_hex": f"0x{byte_high:02X}",
        "byte_low": byte_low,
        "byte_low_hex": f"0x{byte_low:02X}",
    }


def _registers_view(cpu: NgpcCpuState) -> dict[str, object]:
    """Return a rich register-state view for the `registers` CLI."""
    long_attrs = ("xwa", "xbc", "xde", "xhl", "xix", "xiy", "xiz", "xsp")
    long_names = ("XWA", "XBC", "XDE", "XHL", "XIX", "XIY", "XIZ", "XSP")
    rows: list[dict[str, object]] = []
    for long_name, attr_name in zip(long_names, long_attrs):
        value = getattr(cpu.regs, attr_name)
        decomp = _decompose_register_32(value)
        word_name, byte_high_name, byte_low_name = _REG32_DECOMPOSITION[long_name]
        rows.append(
            {
                "long_name": long_name,
                "long": decomp["long"],
                "long_hex": decomp["long_hex"],
                "word_low_name": word_name,
                "word_low": decomp["word_low"],
                "word_low_hex": decomp["word_low_hex"],
                "byte_high_name": byte_high_name,
                "byte_high": decomp["byte_high"] if byte_high_name else None,
                "byte_high_hex": decomp["byte_high_hex"] if byte_high_name else None,
                "byte_low_name": byte_low_name,
                "byte_low": decomp["byte_low"] if byte_low_name else None,
                "byte_low_hex": decomp["byte_low_hex"] if byte_low_name else None,
            }
        )
    return {
        "pc": cpu.pc,
        "pc_hex": f"0x{cpu.pc:08X}",
        "sr_raw": cpu.sr_raw,
        "sr_raw_hex": None if cpu.sr_raw is None else f"0x{cpu.sr_raw:04X}",
        "iff_level": cpu.iff_level,
        "iff_enabled": cpu.iff_enabled,
        "rfp": cpu.rfp,
        "flags": {
            "S": cpu.flags.sf,
            "Z": cpu.flags.zf,
            "V": cpu.flags.vf,
            "H": cpu.flags.hf,
            "C": cpu.flags.cf,
            "N": cpu.flags.nf,
        },
        "alt_flags": {
            "S": None if cpu.alt_flags is None else cpu.alt_flags.sf,
            "Z": None if cpu.alt_flags is None else cpu.alt_flags.zf,
            "V": None if cpu.alt_flags is None else cpu.alt_flags.vf,
            "H": None if cpu.alt_flags is None else cpu.alt_flags.hf,
            "C": None if cpu.alt_flags is None else cpu.alt_flags.cf,
            "N": None if cpu.alt_flags is None else cpu.alt_flags.nf,
        },
        "registers": rows,
        "control_registers": _cpu_control_register_rows(cpu),
        "modeled_fields": list(cpu.modeled_fields),
    }


def _cmd_registers(args: argparse.Namespace) -> int:
    rom_path = Path(args.rom)
    if args.seed_from is not None:
        doc = load_savestate(Path(args.seed_from), expected_rom_path=rom_path)
        cpu = doc.cpu
        seed_label: str | None = str(Path(args.seed_from))
    else:
        cpu = load_machine_state(rom_path).cpu
        seed_label = None

    view = _registers_view(cpu)
    payload = {
        "rom": str(rom_path),
        "seed_from": seed_label,
        **view,
    }

    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0

    print(f"ROM: {rom_path}")
    if seed_label:
        print(f"Seed-from: {seed_label}")
    print(f"PC : {view['pc_hex']}")
    sr_text = "<unknown>" if view["sr_raw_hex"] is None else view["sr_raw_hex"]
    print(f"SR : {sr_text}")
    iff_text = "<unknown>" if view["iff_level"] is None else f"level={view['iff_level']} ({'enabled' if view['iff_enabled'] else 'masked'})"
    print(f"IFF: {iff_text}")
    rfp_text = "<unknown>" if view["rfp"] is None else f"{view['rfp']}"
    print(f"RFP: {rfp_text}")
    flags = view["flags"]  # type: ignore[index]
    flag_text = " ".join(
        f"{name}={'1' if flags[name] is True else '0' if flags[name] is False else '?'}"  # type: ignore[index]
        for name in ("S", "Z", "V", "H", "C", "N")
    )
    print(f"Flags: {flag_text}")
    alt_flags = view["alt_flags"]  # type: ignore[index]
    alt_flag_text = " ".join(
        f"{name}={'1' if alt_flags[name] is True else '0' if alt_flags[name] is False else '?'}"  # type: ignore[index]
        for name in ("S", "Z", "V", "H", "C", "N")
    )
    print(f"Flags': {alt_flag_text}")
    print()
    print(f"{'Long':<5} {'value':<11}  {'Word':<5} {'value':<7}  {'Hi8':<4} {'value':<5}  {'Lo8':<4} {'value':<5}")
    for row in view["registers"]:  # type: ignore[index]
        long_v = row["long_hex"] or "<unknown>"
        word_v = row["word_low_hex"] or "<unknown>"
        if row["byte_high_name"] is None:
            hi_name, hi_v = "-", "-"
            lo_name, lo_v = "-", "-"
        else:
            hi_name = str(row["byte_high_name"])
            hi_v = str(row["byte_high_hex"] or "<unknown>")
            lo_name = str(row["byte_low_name"])
            lo_v = str(row["byte_low_hex"] or "<unknown>")
        print(
            f"{row['long_name']:<5} {long_v:<11}  "
            f"{row['word_low_name']:<5} {word_v:<7}  "
            f"{hi_name:<4} {hi_v:<5}  "
            f"{lo_name:<4} {lo_v:<5}"
        )
    print()
    print(f"{'CR':<8} {'size':<4} {'value':<11}")
    for row in view["control_registers"]:  # type: ignore[index]
        print(f"{row['name']:<8} {row['size']:<4} {str(row['value_hex'] or '<unknown>'):<11}")
    return 0


def _cmd_cpu_info(args: argparse.Namespace) -> int:
    state = load_machine_state(Path(args.rom))
    payload = _cpu_to_dict(state.cpu)
    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0

    print(f"ROM: {state.rom_path}")
    print(f"PC: {payload['pc_hex']}")
    print(f"SR: {payload['sr_raw_hex'] or '<unknown>'}")
    print(f"Register bank: {payload['register_bank'] if payload['register_bank'] is not None else '<unknown>'}")
    print(
        "CPU fields modeled: "
        + ", ".join(payload["modeled_fields"])  # type: ignore[index]
    )
    print("Registers:")
    regs = payload["registers"]  # type: ignore[index]
    for name in ("xwa", "xbc", "xde", "xhl", "xix", "xiy", "xiz", "xsp"):
        print(f"  - {name.upper()}: {regs[f'{name}_hex'] or '<unknown>'}")
    print("Control registers:")
    for row in payload["control_registers"]:  # type: ignore[index]
        print(f"  - {row['name']}: {row['value_hex'] or '<unknown>'} ({row['size']})")
    print(
        "Flags: "
        f"S={payload['flags']['sf']} "
        f"Z={payload['flags']['zf']} "
        f"V={payload['flags']['vf']} "
        f"H={payload['flags']['hf']} "
        f"C={payload['flags']['cf']} "
        f"N={payload['flags']['nf']}"
    )
    print(
        "Flags': "
        f"S={payload['alt_flags']['sf']} "
        f"Z={payload['alt_flags']['zf']} "
        f"V={payload['alt_flags']['vf']} "
        f"H={payload['alt_flags']['hf']} "
        f"C={payload['alt_flags']['cf']} "
        f"N={payload['alt_flags']['nf']}"
    )
    iff_str = "<unknown>" if payload["iff_enabled"] is None else ("enabled" if payload["iff_enabled"] else "disabled")
    print(f"IFF: {iff_str}")
    print(f"Note: {payload['note']}")
    return 0


def _frame_state_to_payload(state: FrameState) -> dict[str, object]:
    return {
        "scanline": state.scanline,
        "frame_count": state.frame_count,
        "in_vblank": state.in_vblank,
        "in_visible_region": state.in_visible_region,
    }


def _cmd_ui(args: argparse.Namespace) -> int:
    """Launch the PyQt6 debugger UI, optionally with a ROM pre-loaded.

    PyQt6 is the ROADMAP-mandated frontend (§4 Architecture, §Mode 2,
    §298). Imported lazily so headless environments (CI, batch runs)
    can still import `ngpc_emu` without pulling Qt into the import
    graph. If PyQt6 isn't installed, prints a friendly install hint.

    When `args.rom` is None, the UI opens with no active session ;
    the user can pick a ROM via File → Open ROM…
    """
    rom_path: Path | None = None
    bios_path: Path | None = None
    if args.rom is not None:
        rom_path = Path(args.rom)
        if not rom_path.exists():
            print(f"ERROR: ROM not found: {rom_path}", file=sys.stderr)
            return 1
    if getattr(args, "bios", None) is not None:
        bios_path = Path(args.bios)
        if not bios_path.exists():
            print(f"ERROR: BIOS not found: {bios_path}", file=sys.stderr)
            return 1
    try:
        from ngpc_emu_ui_qt import launch_ui
    except ImportError as exc:
        print(
            f"ERROR: PyQt6 not available ({exc}).\n"
            "Install with: pip install PyQt6",
            file=sys.stderr,
        )
        return 1
    return launch_ui(rom_path, bios_path=bios_path)


def _cmd_opcode_coverage(args: argparse.Namespace) -> int:
    """Linear-walk a ROM and report decoder coverage.

    For each address starting at `--start` (default = cart entry
    point), the decoder is invoked. When decoded successfully, the
    walker advances by `length`. When it fails (`unknown-opcode` or
    `unsupported-decoded-instruction`), the leading byte is counted
    and the walker advances by 1. This produces a "miss" histogram
    that prioritises executor expansion work.

    Note : because the walker advances by 1 byte on failure, a
    single unknown opcode generates spurious "misses" at the bytes
    that were actually its operands. The TOP entries in the
    histogram are still reliable signal (real instruction starts
    dominate the noise), but the long tail is mostly fallout.
    """
    from collections import Counter, deque
    from core.decode import decode_instruction_at
    from core.fetch import load_fetch_view

    rom_path = Path(args.rom)
    if not rom_path.exists():
        print(f"ERROR: ROM not found: {rom_path}", file=sys.stderr)
        return 1
    view = load_fetch_view(rom_path)
    start = (
        _parse_address(args.start)
        if args.start is not None
        else view.machine.cpu.pc
    )
    if args.follow_direct_control_flow and (
        args.stop_on_silicon_broken or args.stop_on_non_fallthrough
    ):
        print(
            "ERROR: --follow-direct-control-flow cannot be combined with "
            "--stop-on-silicon-broken or --stop-on-non-fallthrough.",
            file=sys.stderr,
        )
        return 1
    budget = int(args.bytes)
    end = start + budget

    decoded_count = 0
    decoded_bytes = 0
    unknown_op: Counter[int] = Counter()
    unsupported_op: Counter[int] = Counter()
    silicon_broken_fallout_op: Counter[int] = Counter()
    decoded_op: Counter[int] = Counter()
    sample_addresses: dict[int, list[int]] = {}
    stop_reason = "byte-budget-reached"
    silicon_broken_fallout_pcs: set[int] = set()

    if args.follow_direct_control_flow:
        stop_reason = "worklist-exhausted"
        pending = deque([start])
        seen_pcs: set[int] = set()

        while pending:
            pc = pending.popleft()
            if pc < start or pc >= end or pc in seen_pcs:
                continue
            seen_pcs.add(pc)

            d = decode_instruction_at(view.bus, pc)
            if d.status == "decoded":
                decoded_count += 1
                length = max(1, d.length or 1)
                decoded_bytes += length
                if d.raw_bytes:
                    decoded_op[d.raw_bytes[0]] += 1
                if (
                    d.falls_through
                    and d.next_sequential_pc is not None
                    and match_known_silicon_broken(d) is not None
                ):
                    silicon_broken_fallout_pcs.add(d.next_sequential_pc)
                if d.falls_through and d.next_sequential_pc is not None:
                    pending.append(d.next_sequential_pc)
                if d.direct_target is not None:
                    pending.append(d.direct_target)
                continue

            read_result = view.bus.read_bytes(pc, size=1)
            if read_result.status != "ok" or not read_result.data:
                continue
            b = read_result.data[0]
            if pc in silicon_broken_fallout_pcs:
                silicon_broken_fallout_op[b] += 1
            elif d.status == "unknown-opcode":
                unknown_op[b] += 1
            else:
                unsupported_op[b] += 1
            sample_addresses.setdefault(b, []).append(pc)
    else:
        pc = start
        while pc < end:
            d = decode_instruction_at(view.bus, pc)
            if d.status == "decoded":
                decoded_count += 1
                length = max(1, d.length or 1)
                decoded_bytes += length
                if d.raw_bytes:
                    decoded_op[d.raw_bytes[0]] += 1
                is_silicon_broken = match_known_silicon_broken(d) is not None
                if (
                    d.falls_through
                    and d.next_sequential_pc is not None
                    and is_silicon_broken
                ):
                    silicon_broken_fallout_pcs.add(d.next_sequential_pc)
                if args.stop_on_non_fallthrough and d.falls_through is False:
                    stop_reason = "stopped-on-non-fallthrough"
                    pc += length
                    break
                if args.stop_on_silicon_broken and is_silicon_broken:
                    stop_reason = "stopped-on-silicon-broken"
                    pc += length
                    break
                pc += length
                continue
            # Failure : record leading byte and advance by 1.
            read_result = view.bus.read_bytes(pc, size=1)
            if read_result.status != "ok" or not read_result.data:
                pc += 1
                continue
            b = read_result.data[0]
            if pc in silicon_broken_fallout_pcs:
                silicon_broken_fallout_op[b] += 1
            elif d.status == "unknown-opcode":
                unknown_op[b] += 1
            else:
                unsupported_op[b] += 1
            sample_addresses.setdefault(b, []).append(pc)
            pc += 1

    total_misses = (
        sum(unknown_op.values())
        + sum(unsupported_op.values())
        + sum(silicon_broken_fallout_op.values())
    )
    coverage_pct = (
        100.0 * decoded_bytes / max(1, budget)
    )

    if args.json:
        payload = {
            "rom": str(rom_path),
            "start_pc_hex": f"0x{start:08X}",
            "byte_budget": budget,
            "decoded_instruction_count": decoded_count,
            "decoded_bytes": decoded_bytes,
            "coverage_byte_percent": round(coverage_pct, 2),
            "stop_reason": stop_reason,
            "unknown_opcode_total": sum(unknown_op.values()),
            "unsupported_decoded_total": sum(unsupported_op.values()),
            "silicon_broken_fallout_total": sum(silicon_broken_fallout_op.values()),
            "top_unknown_opcodes": [
                {
                    "byte_hex": f"0x{b:02X}",
                    "count": c,
                    "first_addresses_hex": [
                        f"0x{a:08X}" for a in sample_addresses.get(b, [])[:3]
                    ],
                }
                for b, c in unknown_op.most_common(args.top)
            ],
            "top_unsupported_decoded": [
                {"byte_hex": f"0x{b:02X}", "count": c}
                for b, c in unsupported_op.most_common(args.top)
            ],
            "top_silicon_broken_fallout": [
                {
                    "byte_hex": f"0x{b:02X}",
                    "count": c,
                    "first_addresses_hex": [
                        f"0x{a:08X}" for a in sample_addresses.get(b, [])[:3]
                    ],
                }
                for b, c in silicon_broken_fallout_op.most_common(args.top)
            ],
        }
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0

    print(f"ROM: {rom_path}")
    print(f"Start PC: 0x{start:08X}")
    print(f"Walk budget: {budget} bytes")
    print(f"Stop reason: {stop_reason}")
    print(f"Decoded instructions    : {decoded_count}")
    print(f"Decoded bytes           : {decoded_bytes} "
          f"({coverage_pct:.1f}% of budget)")
    print(f"Unknown-opcode misses   : {sum(unknown_op.values())}")
    print(f"Unsupported-decoded     : {sum(unsupported_op.values())}")
    print(f"Silicon-broken fallout  : {sum(silicon_broken_fallout_op.values())}")
    if not total_misses:
        print("All bytes decoded — no coverage gaps in this walk.")
        return 0
    if unknown_op:
        print(f"\nTop {args.top} unknown opcode leading-bytes :")
        print(f"  {'byte':>6}  {'count':>6}  first sample addresses")
        for b, c in unknown_op.most_common(args.top):
            sample = ", ".join(
                f"0x{a:08X}" for a in sample_addresses.get(b, [])[:3]
            )
            print(f"  0x{b:02X}    {c:6d}  {sample}")
    if unsupported_op:
        print(f"\nTop unsupported-decoded leading-bytes :")
        for b, c in unsupported_op.most_common(args.top):
            print(f"  0x{b:02X}    {c:6d}")
    if silicon_broken_fallout_op:
        print(f"\nTop immediate post-silicon-broken fallout bytes :")
        print(f"  {'byte':>6}  {'count':>6}  first sample addresses")
        for b, c in silicon_broken_fallout_op.most_common(args.top):
            sample = ", ".join(
                f"0x{a:08X}" for a in sample_addresses.get(b, [])[:3]
            )
            print(f"  0x{b:02X}    {c:6d}  {sample}")
    return 0


def _cmd_tick_frame(args: argparse.Namespace) -> int:
    """Advance the frame/scanline state model without running CPU instructions.

    M3 Phase 0 command — emits a stateful savestate at the new frame
    timing position so downstream readers (and Phase 3.1+ HW timing
    reads) can consume `frame_state`. No CPU steps are executed; the
    CPU section of the savestate is copied verbatim from the seed
    state (or the bootstrap machine when no `--seed-from` is given).
    """
    rom_path = Path(args.rom)
    machine = load_machine_state(rom_path)

    seed_doc = None
    cpu_state = machine.cpu
    writable_overlay: dict[int, int] = {}
    starting_frame_state = initial_frame_state()
    starting_irq_state = initial_irq_state()
    seed_label: str | None = None

    if args.seed_from is not None:
        seed_doc = load_savestate(Path(args.seed_from), expected_rom_path=rom_path)
        cpu_state = seed_doc.cpu
        writable_overlay = dict(seed_doc.writable_overlay)
        starting_frame_state = seed_doc.frame_state
        starting_irq_state = seed_doc.irq_state
        seed_label = str(Path(args.seed_from))

    if args.scanlines is not None and args.frames is not None:
        print(
            "Error: --scanlines and --frames are mutually exclusive.",
            file=sys.stderr,
        )
        return 1

    if args.scanlines is None and args.frames is None:
        # Default: advance one scanline. Cheapest meaningful tick.
        scanlines_to_advance = 1
        frames_to_advance = 0
    elif args.scanlines is not None:
        scanlines_to_advance = args.scanlines
        frames_to_advance = 0
    else:
        scanlines_to_advance = 0
        frames_to_advance = args.frames

    if scanlines_to_advance < 0 or frames_to_advance < 0:
        print(
            "Error: --scanlines and --frames must be non-negative "
            "(rewind belongs to M5).",
            file=sys.stderr,
        )
        return 1

    transitions: tuple = ()
    if scanlines_to_advance > 0:
        transitions = detect_vblank_transitions(
            starting_frame_state, scanlines_to_advance,
        )
        new_frame_state = advance_scanlines(
            starting_frame_state, scanlines_to_advance,
        )
    else:
        new_frame_state = advance_frames(starting_frame_state, frames_to_advance)

    # M3 Phase 3.2.2a: fold VBlank-enter transitions into pending IRQ
    # state. Advancement by `--frames` snaps scanline → 0 without
    # passing through transition detection, so the IRQ state is
    # carried forward unchanged on that path; Phase 3.2.2b will
    # extend the model when the executor delivers the pending IRQ.
    new_irq_state = fold_vblank_irq_pending(starting_irq_state, transitions)

    saved_path: Path | None = None
    if args.save_state is not None:
        saved_path = Path(args.save_state)
        payload = build_savestate_payload(
            rom_path=rom_path,
            rom_header=machine.header,
            cpu=cpu_state,
            writable_overlay=writable_overlay,
            frame_state=new_frame_state,
            irq_state=new_irq_state,
        )
        save_savestate(saved_path, payload)

    if args.json:
        payload_out = {
            "rom": str(rom_path),
            "seed_from": seed_label,
            "before": _frame_state_to_payload(starting_frame_state),
            "after": _frame_state_to_payload(new_frame_state),
            "irq_before": {
                "pending_mask": starting_irq_state.pending_mask,
                "vblank_pending": starting_irq_state.is_vblank_pending(),
            },
            "irq_after": {
                "pending_mask": new_irq_state.pending_mask,
                "vblank_pending": new_irq_state.is_vblank_pending(),
            },
            "advance": {
                "scanlines": scanlines_to_advance,
                "frames": frames_to_advance,
            },
            "vblank_transitions": [
                {
                    "kind": t.kind,
                    "scanline": t.scanline,
                    "frame_count": t.frame_count,
                }
                for t in transitions
            ],
            "save_state": str(saved_path) if saved_path is not None else None,
            "constants": {
                "scanlines_per_frame": SCANLINES_PER_FRAME,
                "visible_scanlines": VISIBLE_SCANLINES,
                "vblank_scanlines": VBLANK_SCANLINES,
                "frames_per_second": FRAMES_PER_SECOND,
                "vblank_irq_level": IRQ_LEVEL_VBLANK,
                "vblank_vector_address_hex": f"0x{VBLANK_VECTOR_ADDRESS:06X}",
            },
        }
        print(json.dumps(payload_out, indent=2, sort_keys=True))
        return 0

    print(f"ROM: {rom_path}")
    if seed_label is not None:
        print(f"Seed-from: {seed_label}")
    if scanlines_to_advance > 0:
        print(f"Advance: {scanlines_to_advance} scanline(s)")
    else:
        print(f"Advance: {frames_to_advance} frame(s)")
    print(
        f"Before: scanline {starting_frame_state.scanline:>3} / "
        f"frame {starting_frame_state.frame_count}  "
        f"(in_vblank={starting_frame_state.in_vblank})"
    )
    print(
        f"After:  scanline {new_frame_state.scanline:>3} / "
        f"frame {new_frame_state.frame_count}  "
        f"(in_vblank={new_frame_state.in_vblank})"
    )
    if transitions:
        print(f"VBlank transitions: {len(transitions)}")
        for t in transitions:
            print(
                f"  {t.kind:<5}  scanline={t.scanline:>3}  "
                f"frame={t.frame_count}"
            )
    if (
        starting_irq_state.pending_mask != new_irq_state.pending_mask
        or new_irq_state.is_vblank_pending()
    ):
        print(
            f"IRQ pending: 0x{new_irq_state.pending_mask:02X}  "
            f"(VBlank: {'YES' if new_irq_state.is_vblank_pending() else 'no'})"
        )
    if saved_path is not None:
        print(f"Saved-state: {saved_path}")
    return 0


def _cmd_savestate_save(args: argparse.Namespace) -> int:
    rom_path = Path(args.rom)
    output_path = Path(args.output)
    seed_registers = _parse_seed_registers(args)
    auto_tick_address, auto_tick_payload = _parse_auto_tick_args(args)
    auto_tick_note_suffix = _format_auto_tick_note_suffix(auto_tick_payload)

    machine = load_machine_state(rom_path)
    final_cpu = machine.cpu
    final_memory: dict[int, int] = {}
    matched_quirk = None
    source_note_suffix = "derived from bootstrap reset state"
    executed_count = 0
    total_cycles_consumed: int | None = None

    if args.run_until is not None:
        target_pc = _parse_address(args.run_until)
        start_pc = (
            None if args.address is None else _parse_address(args.address)
        )
        run_result = load_run_until(
            rom_path,
            target_pc=target_pc,
            start_pc=start_pc,
            seed_registers=seed_registers,
            max_steps=args.max_steps,
            auto_tick_address=auto_tick_address,
            auto_tick_period=args.auto_tick_period,
        )
        final_cpu = run_result.final_cpu
        final_memory = dict(run_result.final_memory)
        executed_count = run_result.executed_count
        total_cycles_consumed = run_result.total_cycles_consumed
        if run_result.last_record is not None:
            matched_quirk = run_result.last_record.execution.matched_quirk
        source_note_suffix = (
            f"derived from run-until-exec target=0x{target_pc:08X} "
            f"stop_reason={run_result.stop_reason} executed={run_result.executed_count}"
        )
        if auto_tick_note_suffix is not None:
            source_note_suffix += f" | {auto_tick_note_suffix}"

    user_note = args.note
    note_pieces = [source_note_suffix]
    if user_note:
        note_pieces.insert(0, user_note)
    final_note: str | None = " | ".join(note_pieces) if note_pieces else None

    # M3 Phase 3.2.3a: advance frame_state by the run's actual
    # accumulated cycle cost (or the executed_count × 8 fallback for
    # the bootstrap-only path that doesn't gather a run result).
    final_frame_state = _advance_frame_state_for_run(
        None, executed_count, total_cycles_consumed=total_cycles_consumed,
    )
    payload = build_savestate_payload(
        rom_path=rom_path,
        rom_header=machine.header,
        cpu=final_cpu,
        writable_overlay=final_memory,
        matched_on_last_step=matched_quirk,
        note=final_note,
        frame_state=final_frame_state,
    )
    save_savestate(output_path, payload)

    if args.json:
        print(
            json.dumps(
                {
                    "saved_to": str(output_path),
                    "format_version": SAVESTATE_FORMAT_VERSION,
                    "rom_sha256": payload["rom"]["sha256"],  # type: ignore[index]
                    "cpu_pc": final_cpu.pc,
                    "cpu_pc_hex": f"0x{final_cpu.pc:08X}",
                    "overlay_byte_count": len(final_memory),
                    "non_reference": auto_tick_payload,
                    "note": final_note,
                },
                indent=2,
                sort_keys=True,
            )
        )
        return 0

    print(f"Saved savestate: {output_path}")
    print(f"Format version: {SAVESTATE_FORMAT_VERSION}")
    print(f"ROM sha256: {payload['rom']['sha256']}")  # type: ignore[index]
    print(f"CPU PC: 0x{final_cpu.pc:08X}")
    print(f"Overlay bytes captured: {len(final_memory)}")
    _print_auto_tick_summary(auto_tick_payload)
    if final_note:
        print(f"Note: {final_note}")
    return 0


def _cmd_savestate_load(args: argparse.Namespace) -> int:
    input_path = Path(args.input)
    expected_rom = Path(args.rom) if args.rom else None

    doc = load_savestate(input_path, expected_rom_path=expected_rom)

    if args.json:
        print(
            json.dumps(
                {
                    "loaded_from": str(input_path),
                    "format_version": doc.format_version,
                    "created_at_utc": doc.created_at_utc,
                    "rom_sha256": doc.rom_sha256,
                    "rom_file_size": doc.rom_file_size,
                    "rom_header_title": doc.rom_header_title,
                    "rom_header_entry_point": doc.rom_header_entry_point,
                    "rom_header_entry_point_hex": f"0x{doc.rom_header_entry_point:08X}",
                    "rom_header_mode_raw": doc.rom_header_mode_raw,
                    "cpu_pc": doc.cpu.pc,
                    "cpu_pc_hex": f"0x{doc.cpu.pc:08X}",
                    "cpu_registers": {
                        name: getattr(doc.cpu.regs, name)
                        for name in ("xwa", "xbc", "xde", "xhl", "xix", "xiy", "xiz", "xsp")
                    },
                    "cpu_iff_enabled": doc.cpu.iff_enabled,
                    "overlay_byte_count": len(doc.writable_overlay),
                    "quirk_database_version": doc.quirk_database_version,
                    "matched_on_last_step": _known_quirk_to_dict(doc.matched_on_last_step),
                    "note": doc.note,
                },
                indent=2,
                sort_keys=True,
            )
        )
        return 0

    print(f"Loaded savestate: {input_path}")
    print(f"Format version: {doc.format_version}")
    if doc.created_at_utc:
        print(f"Created at: {doc.created_at_utc}")
    print(f"ROM sha256: {doc.rom_sha256}")
    print(f"ROM title: {doc.rom_header_title or '<empty>'}")
    print(f"ROM entry point: 0x{doc.rom_header_entry_point:08X}")
    print(f"ROM file size: {doc.rom_file_size} bytes")
    print(f"CPU PC: 0x{doc.cpu.pc:08X}")
    print("CPU registers:")
    for name in ("xwa", "xbc", "xde", "xhl", "xix", "xiy", "xiz", "xsp"):
        value = getattr(doc.cpu.regs, name)
        hex_value = "<unknown>" if value is None else f"0x{value:08X}"
        print(f"  - {name.upper()}: {hex_value}")
    iff_str = (
        "<unknown>"
        if doc.cpu.iff_enabled is None
        else ("enabled" if doc.cpu.iff_enabled else "disabled")
    )
    print(f"IFF: {iff_str}")
    print(f"Overlay bytes captured: {len(doc.writable_overlay)}")
    print(f"Quirk database version: {doc.quirk_database_version or '<unknown>'}")
    if doc.matched_on_last_step is not None:
        match = doc.matched_on_last_step
        print(f"Last-step quirk: {match.quirk_id} [{match.confidence}, {match.category}]")
    if doc.note:
        print(f"Note: {doc.note}")
    if expected_rom is None:
        print(
            "WARNING: no --rom passed; ROM content hash was NOT verified against a "
            "real ROM file."
        )
    return 0


def _cmd_checkpoint_save(args: argparse.Namespace) -> int:
    rom_path = Path(args.rom)
    checkpoint_path = checkpoint_path_for_rom(rom_path, args.name)
    seed_registers = _parse_seed_registers(args)
    auto_tick_address, auto_tick_payload = _parse_auto_tick_args(args)
    auto_tick_note_suffix = _format_auto_tick_note_suffix(auto_tick_payload)

    machine = load_machine_state(rom_path)
    final_cpu = machine.cpu
    final_memory: dict[int, int] = {}
    matched_quirk = None
    source_note_suffix = f"named checkpoint '{args.name}' derived from bootstrap reset state"

    initial_cpu_state = None
    initial_memory_bytes: dict[int, int] | None = None
    initial_frame_state = None
    initial_irq_state = None
    executed_count = 0
    seed_from_doc, _seed_payload = _load_seed_savestate_from_args(
        rom_path=rom_path,
        seed_from=args.seed_from,
        seed_checkpoint=args.seed_checkpoint,
        seed_session=args.seed_session,
    )
    if seed_from_doc is not None:
        initial_cpu_state = seed_from_doc.cpu
        initial_memory_bytes = dict(seed_from_doc.writable_overlay)
        initial_frame_state = seed_from_doc.frame_state
        initial_irq_state = seed_from_doc.irq_state
        final_cpu = initial_cpu_state
        final_memory = initial_memory_bytes
        source_note_suffix = (
            f"named checkpoint '{args.name}' derived from resumed savestate "
            f"CPU PC 0x{seed_from_doc.cpu.pc:08X}"
        )

    if args.run_until is None and (
        args.seed_xsp is not None or seed_registers
    ):
        final_cpu = seed_cpu_state_for_execution(
            final_cpu,
            register_values=seed_registers,
            seed_xsp=None if args.seed_xsp is None else _parse_address(args.seed_xsp),
        )

    if args.run_until is not None:
        target_pc = _parse_address(args.run_until)
        start_pc = None if args.address is None else _parse_address(args.address)
        run_result = load_run_until(
            rom_path,
            target_pc=target_pc,
            start_pc=start_pc,
            seed_xsp=None if args.seed_xsp is None else _parse_address(args.seed_xsp),
            seed_registers=seed_registers,
            max_steps=args.max_steps,
            initial_cpu_state=initial_cpu_state,
            initial_memory_bytes=initial_memory_bytes,
            auto_tick_address=auto_tick_address,
            auto_tick_period=args.auto_tick_period,
            initial_frame_state=initial_frame_state,
            initial_irq_state=initial_irq_state,
        )
        final_cpu = run_result.final_cpu
        final_memory = dict(run_result.final_memory)
        executed_count = run_result.executed_count
        initial_irq_state = run_result.final_irq_state
        checkpoint_total_cycles = run_result.total_cycles_consumed
        if run_result.last_record is not None:
            matched_quirk = run_result.last_record.execution.matched_quirk
        source_note_suffix = (
            f"named checkpoint '{args.name}' derived from run-until-exec "
            f"target=0x{target_pc:08X} stop_reason={run_result.stop_reason} "
            f"executed={run_result.executed_count}"
        )
        if auto_tick_note_suffix is not None:
            source_note_suffix += f" | {auto_tick_note_suffix}"
    else:
        checkpoint_total_cycles = None

    user_note = args.note
    note_pieces = [source_note_suffix]
    if user_note:
        note_pieces.insert(0, user_note)
    final_note: str | None = " | ".join(note_pieces) if note_pieces else None

    final_frame_state = _advance_frame_state_for_run(
        initial_frame_state,
        executed_count,
        total_cycles_consumed=checkpoint_total_cycles,
    )
    payload = build_savestate_payload(
        rom_path=rom_path,
        rom_header=machine.header,
        cpu=final_cpu,
        writable_overlay=final_memory,
        matched_on_last_step=matched_quirk,
        note=final_note,
        frame_state=final_frame_state,
        irq_state=initial_irq_state,
    )
    save_named_checkpoint(checkpoint_path, payload)

    if args.json:
        print(
            json.dumps(
                {
                    "name": args.name,
                    "path": str(checkpoint_path),
                    "format_version": SAVESTATE_FORMAT_VERSION,
                    "rom_sha256": payload["rom"]["sha256"],  # type: ignore[index]
                    "cpu_pc_hex": f"0x{final_cpu.pc:08X}",
                    "overlay_byte_count": len(final_memory),
                    "non_reference": auto_tick_payload,
                    "note": final_note,
                },
                indent=2,
                sort_keys=True,
            )
        )
        return 0

    print(f"Saved checkpoint: {args.name}")
    print(f"Path: {checkpoint_path}")
    print(f"CPU PC: 0x{final_cpu.pc:08X}")
    print(f"Overlay bytes captured: {len(final_memory)}")
    _print_auto_tick_summary(auto_tick_payload)
    if final_note:
        print(f"Note: {final_note}")
    return 0


def _cmd_checkpoint_load(args: argparse.Namespace) -> int:
    checkpoint = load_named_checkpoint(Path(args.rom), args.name)
    doc = checkpoint.document

    if args.json:
        print(
            json.dumps(
                {
                    "name": checkpoint.name,
                    "path": str(checkpoint.path),
                    "format_version": doc.format_version,
                    "created_at_utc": doc.created_at_utc,
                    "rom_sha256": doc.rom_sha256,
                    "cpu_pc_hex": f"0x{doc.cpu.pc:08X}",
                    "overlay_byte_count": len(doc.writable_overlay),
                    "note": doc.note,
                },
                indent=2,
                sort_keys=True,
            )
        )
        return 0

    print(f"Loaded checkpoint: {checkpoint.name}")
    print(f"Path: {checkpoint.path}")
    print(f"Format version: {doc.format_version}")
    if doc.created_at_utc:
        print(f"Created at: {doc.created_at_utc}")
    print(f"ROM sha256: {doc.rom_sha256}")
    print(f"CPU PC: 0x{doc.cpu.pc:08X}")
    print(f"Overlay bytes captured: {len(doc.writable_overlay)}")
    if doc.note:
        print(f"Note: {doc.note}")
    return 0


def _cmd_checkpoint_list(args: argparse.Namespace) -> int:
    checkpoints = list_named_checkpoints(Path(args.rom))
    if args.json:
        print(
            json.dumps(
                {
                    "rom": str(Path(args.rom)),
                    "count": len(checkpoints),
                    "checkpoints": [
                        {
                            "name": checkpoint.name,
                            "path": str(checkpoint.path),
                            "created_at_utc": checkpoint.document.created_at_utc,
                            "cpu_pc_hex": f"0x{checkpoint.document.cpu.pc:08X}",
                            "overlay_byte_count": len(checkpoint.document.writable_overlay),
                            "note": checkpoint.document.note,
                        }
                        for checkpoint in checkpoints
                    ],
                },
                indent=2,
                sort_keys=True,
            )
        )
        return 0

    print(f"Checkpoint count: {len(checkpoints)}")
    for checkpoint in checkpoints:
        print(
            f"- {checkpoint.name}: PC 0x{checkpoint.document.cpu.pc:08X}, "
            f"{len(checkpoint.document.writable_overlay)} overlay bytes, "
            f"path={checkpoint.path}"
        )
    return 0


def _cmd_checkpoint_delete(args: argparse.Namespace) -> int:
    deleted_path = delete_named_checkpoint(Path(args.rom), args.name)
    if args.json:
        print(
            json.dumps(
                {"name": args.name, "deleted_path": str(deleted_path)},
                indent=2,
                sort_keys=True,
            )
        )
        return 0

    print(f"Deleted checkpoint: {args.name}")
    print(f"Path: {deleted_path}")
    return 0


def _cmd_session_save(args: argparse.Namespace) -> int:
    rom_path = Path(args.rom)
    seed_registers = _parse_seed_registers(args)
    checkpoint_name = managed_checkpoint_name_for_session(args.name)
    checkpoint_path = session_checkpoint_path_for_rom(rom_path, args.name)
    auto_tick_address, auto_tick_payload = _parse_auto_tick_args(args)
    auto_tick_note_suffix = _format_auto_tick_note_suffix(auto_tick_payload)

    machine = load_machine_state(rom_path)
    final_cpu = machine.cpu
    final_memory: dict[int, int] = {}
    matched_quirk = None
    source_note_suffix = (
        f"named session '{args.name}' derived from bootstrap reset state"
    )

    initial_cpu_state = None
    initial_memory_bytes: dict[int, int] | None = None
    initial_frame_state = None
    initial_irq_state = None
    executed_count = 0
    seed_from_doc, _seed_payload = _load_seed_savestate_from_args(
        rom_path=rom_path,
        seed_from=args.seed_from,
        seed_checkpoint=args.seed_checkpoint,
        seed_session=args.seed_session,
    )
    if seed_from_doc is not None:
        initial_cpu_state = seed_from_doc.cpu
        initial_memory_bytes = dict(seed_from_doc.writable_overlay)
        initial_frame_state = seed_from_doc.frame_state
        initial_irq_state = seed_from_doc.irq_state
        final_cpu = initial_cpu_state
        final_memory = initial_memory_bytes
        source_note_suffix = (
            f"named session '{args.name}' derived from resumed state "
            f"CPU PC 0x{seed_from_doc.cpu.pc:08X}"
        )

    if args.run_until is None and (
        args.seed_xsp is not None or seed_registers
    ):
        final_cpu = seed_cpu_state_for_execution(
            final_cpu,
            register_values=seed_registers,
            seed_xsp=None if args.seed_xsp is None else _parse_address(args.seed_xsp),
        )

    if args.run_until is not None:
        target_pc = _parse_address(args.run_until)
        start_pc = None if args.address is None else _parse_address(args.address)
        run_result = load_run_until(
            rom_path,
            target_pc=target_pc,
            start_pc=start_pc,
            seed_xsp=None if args.seed_xsp is None else _parse_address(args.seed_xsp),
            seed_registers=seed_registers,
            max_steps=args.max_steps,
            initial_cpu_state=initial_cpu_state,
            initial_memory_bytes=initial_memory_bytes,
            auto_tick_address=auto_tick_address,
            auto_tick_period=args.auto_tick_period,
            initial_frame_state=initial_frame_state,
            initial_irq_state=initial_irq_state,
        )
        final_cpu = run_result.final_cpu
        final_memory = dict(run_result.final_memory)
        executed_count = run_result.executed_count
        # M3 Phase 3.2.2b: carry IRQ state forward through the run.
        # run_result.final_irq_state is the post-delivery snapshot when
        # the run sampled IRQs (i.e., the caller seeded irq_state).
        initial_irq_state = run_result.final_irq_state
        session_total_cycles: int | None = run_result.total_cycles_consumed
        if run_result.last_record is not None:
            matched_quirk = run_result.last_record.execution.matched_quirk
        source_note_suffix = (
            f"named session '{args.name}' derived from run-until-exec "
            f"target=0x{target_pc:08X} stop_reason={run_result.stop_reason} "
            f"executed={run_result.executed_count}"
        )
        if auto_tick_note_suffix is not None:
            source_note_suffix += f" | {auto_tick_note_suffix}"
    else:
        session_total_cycles = None

    state_payload = _save_execution_savestate(
        rom_path=rom_path,
        output_path=checkpoint_path,
        final_cpu=final_cpu,
        final_memory=final_memory,
        matched_quirk=matched_quirk,
        source_note_suffix=source_note_suffix,
        user_note=args.note,
        final_frame_state=_advance_frame_state_for_run(
            initial_frame_state,
            executed_count,
            total_cycles_consumed=session_total_cycles,
        ),
        final_irq_state=initial_irq_state,
    )
    session = save_named_session(
        rom_path,
        args.name,
        current_checkpoint_name=checkpoint_name,
        last_action="session save",
        note=args.note,
    )

    payload = {
        "name": session.name,
        "path": str(session.path),
        "checkpoint_name": session.current_checkpoint_name,
        "checkpoint_path": str(session.current_checkpoint_path),
        "format_version": SESSION_FORMAT_VERSION,
        "state_format_version": SAVESTATE_FORMAT_VERSION,
        "rom_sha256": state_payload["rom"]["sha256"],  # type: ignore[index]
        "created_at_utc": session.created_at_utc,
        "updated_at_utc": session.updated_at_utc,
        "cpu_pc_hex": f"0x{final_cpu.pc:08X}",
        "overlay_byte_count": len(final_memory),
        "last_action": session.last_action,
        "non_reference": auto_tick_payload,
        "note": session.note,
        "state_note": state_payload.get("note"),
    }
    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0

    print(f"Saved session: {session.name}")
    print(f"Session path: {session.path}")
    print(f"Current checkpoint: {session.current_checkpoint_name}")
    print(f"Checkpoint path: {session.current_checkpoint_path}")
    print(f"CPU PC: 0x{final_cpu.pc:08X}")
    print(f"Overlay bytes captured: {len(final_memory)}")
    _print_auto_tick_summary(auto_tick_payload)
    if session.note:
        print(f"Note: {session.note}")
    return 0


def _cmd_session_load(args: argparse.Namespace) -> int:
    session = load_named_session(Path(args.rom), args.name)
    doc = session.document

    payload = {
        "name": session.name,
        "path": str(session.path),
        "format_version": SESSION_FORMAT_VERSION,
        "created_at_utc": session.created_at_utc,
        "updated_at_utc": session.updated_at_utc,
        "rom_sha256": session.rom_sha256,
        "checkpoint_name": session.current_checkpoint_name,
        "checkpoint_path": str(session.current_checkpoint_path),
        "cpu_pc_hex": f"0x{doc.cpu.pc:08X}",
        "overlay_byte_count": len(doc.writable_overlay),
        "last_action": session.last_action,
        "note": session.note,
        "state_note": doc.note,
    }
    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0

    print(f"Loaded session: {session.name}")
    print(f"Path: {session.path}")
    print(f"Created at: {session.created_at_utc}")
    print(f"Updated at: {session.updated_at_utc}")
    print(f"Current checkpoint: {session.current_checkpoint_name}")
    print(f"Checkpoint path: {session.current_checkpoint_path}")
    print(f"CPU PC: 0x{doc.cpu.pc:08X}")
    print(f"Overlay bytes captured: {len(doc.writable_overlay)}")
    if session.last_action:
        print(f"Last action: {session.last_action}")
    if session.note:
        print(f"Note: {session.note}")
    return 0


def _cmd_session_list(args: argparse.Namespace) -> int:
    sessions = list_named_sessions(Path(args.rom))
    if args.json:
        print(
            json.dumps(
                {
                    "rom": str(Path(args.rom)),
                    "count": len(sessions),
                    "sessions": [
                        {
                            "name": session.name,
                            "path": str(session.path),
                            "created_at_utc": session.created_at_utc,
                            "updated_at_utc": session.updated_at_utc,
                            "checkpoint_name": session.current_checkpoint_name,
                            "checkpoint_path": str(session.current_checkpoint_path),
                            "cpu_pc_hex": f"0x{session.document.cpu.pc:08X}",
                            "overlay_byte_count": len(session.document.writable_overlay),
                            "last_action": session.last_action,
                            "note": session.note,
                        }
                        for session in sessions
                    ],
                },
                indent=2,
                sort_keys=True,
            )
        )
        return 0

    print(f"Session count: {len(sessions)}")
    for session in sessions:
        print(
            f"- {session.name}: PC 0x{session.document.cpu.pc:08X}, "
            f"{len(session.document.writable_overlay)} overlay bytes, "
            f"checkpoint={session.current_checkpoint_name}, path={session.path}"
        )
    return 0


def _cmd_session_delete(args: argparse.Namespace) -> int:
    deleted_path, deleted_checkpoint_path, deleted_snapshot_paths = delete_named_session(
        Path(args.rom), args.name
    )
    payload = {
        "name": args.name,
        "deleted_path": str(deleted_path),
        "deleted_checkpoint_path": (
            None if deleted_checkpoint_path is None else str(deleted_checkpoint_path)
        ),
        "deleted_snapshot_paths": [str(path) for path in deleted_snapshot_paths],
    }
    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0

    print(f"Deleted session: {args.name}")
    print(f"Session path: {deleted_path}")
    if deleted_checkpoint_path is not None:
        print(f"Deleted checkpoint path: {deleted_checkpoint_path}")
    if deleted_snapshot_paths:
        print(f"Deleted snapshot count: {len(deleted_snapshot_paths)}")
    return 0


def _cmd_session_snapshot_save(args: argparse.Namespace) -> int:
    snapshot = save_named_session_snapshot(Path(args.rom), args.name, args.snapshot)
    payload = {
        "session_name": snapshot.session_name,
        "name": snapshot.name,
        "checkpoint_name": snapshot.checkpoint_name,
        "path": str(snapshot.path),
        "format_version": SAVESTATE_FORMAT_VERSION,
        "cpu_pc_hex": f"0x{snapshot.document.cpu.pc:08X}",
        "overlay_byte_count": len(snapshot.document.writable_overlay),
        "note": snapshot.document.note,
    }
    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0

    print(f"Saved session snapshot: {snapshot.name}")
    print(f"Session: {snapshot.session_name}")
    print(f"Path: {snapshot.path}")
    print(f"CPU PC: 0x{snapshot.document.cpu.pc:08X}")
    print(f"Overlay bytes captured: {len(snapshot.document.writable_overlay)}")
    return 0


def _cmd_session_snapshot_list(args: argparse.Namespace) -> int:
    snapshots = list_named_session_snapshots(Path(args.rom), args.name)
    payload = {
        "session_name": args.name,
        "count": len(snapshots),
        "snapshots": [
            {
                "name": snapshot.name,
                "checkpoint_name": snapshot.checkpoint_name,
                "path": str(snapshot.path),
                "created_at_utc": snapshot.document.created_at_utc,
                "cpu_pc_hex": f"0x{snapshot.document.cpu.pc:08X}",
                "overlay_byte_count": len(snapshot.document.writable_overlay),
                "note": snapshot.document.note,
            }
            for snapshot in snapshots
        ],
    }
    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0

    print(f"Session snapshot count: {len(snapshots)}")
    for snapshot in snapshots:
        print(
            f"- {snapshot.name}: PC 0x{snapshot.document.cpu.pc:08X}, "
            f"{len(snapshot.document.writable_overlay)} overlay bytes, "
            f"path={snapshot.path}"
        )
    return 0


def _cmd_session_snapshot_load(args: argparse.Namespace) -> int:
    snapshot = load_named_session_snapshot(Path(args.rom), args.name, args.snapshot)
    payload = {
        "session_name": snapshot.session_name,
        "name": snapshot.name,
        "checkpoint_name": snapshot.checkpoint_name,
        "path": str(snapshot.path),
        "format_version": SAVESTATE_FORMAT_VERSION,
        "created_at_utc": snapshot.document.created_at_utc,
        "cpu_pc_hex": f"0x{snapshot.document.cpu.pc:08X}",
        "overlay_byte_count": len(snapshot.document.writable_overlay),
        "note": snapshot.document.note,
    }
    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0

    print(f"Loaded session snapshot: {snapshot.name}")
    print(f"Session: {snapshot.session_name}")
    print(f"Path: {snapshot.path}")
    print(f"CPU PC: 0x{snapshot.document.cpu.pc:08X}")
    print(f"Overlay bytes captured: {len(snapshot.document.writable_overlay)}")
    return 0


def _cmd_session_snapshot_restore(args: argparse.Namespace) -> int:
    session = restore_named_session_snapshot(Path(args.rom), args.name, args.snapshot)
    payload = {
        "name": session.name,
        "path": str(session.path),
        "checkpoint_name": session.current_checkpoint_name,
        "checkpoint_path": str(session.current_checkpoint_path),
        "snapshot_name": args.snapshot,
        "cpu_pc_hex": f"0x{session.document.cpu.pc:08X}",
        "overlay_byte_count": len(session.document.writable_overlay),
        "last_action": session.last_action,
        "note": session.note,
    }
    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0

    print(f"Restored session: {session.name}")
    print(f"Snapshot: {args.snapshot}")
    print(f"Current checkpoint: {session.current_checkpoint_name}")
    print(f"CPU PC: 0x{session.document.cpu.pc:08X}")
    print(f"Overlay bytes captured: {len(session.document.writable_overlay)}")
    return 0


def _cmd_session_snapshot_delete(args: argparse.Namespace) -> int:
    deleted_path = delete_named_session_snapshot(Path(args.rom), args.name, args.snapshot)
    payload = {
        "session_name": args.name,
        "name": args.snapshot,
        "checkpoint_name": managed_snapshot_checkpoint_name_for_session(
            args.name, args.snapshot
        ),
        "deleted_path": str(deleted_path),
    }
    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0

    print(f"Deleted session snapshot: {args.snapshot}")
    print(f"Session: {args.name}")
    print(f"Path: {deleted_path}")
    return 0


def _event_log_summary_to_dict(payload: dict[str, object], loaded_from: str) -> dict[str, object]:
    rom = payload["rom"]
    run_context = payload["run_context"]
    summary = payload["summary"]
    assert isinstance(rom, dict)
    assert isinstance(run_context, dict)
    assert isinstance(summary, dict)
    return {
        "loaded_from": loaded_from,
        "format_version": payload["format_version"],
        "created_at_utc": payload.get("created_at_utc"),
        "rom_sha256": rom["sha256"],
        "rom_file_size": rom["file_size"],
        "rom_header_title": rom.get("header_title"),
        "rom_header_entry_point": rom["header_entry_point"],
        "rom_header_entry_point_hex": f"0x{rom['header_entry_point']:08X}",
        "run_context": run_context,
        "summary": summary,
        "note": payload.get("note"),
    }


def _build_eventlog_payload_from_args(
    args: argparse.Namespace,
) -> tuple[dict[str, object], SavestateDocument | None, dict[str, object] | None]:
    rom_path = Path(args.rom)
    seed_registers = _parse_seed_registers(args)
    auto_tick_address, auto_tick_payload = _parse_auto_tick_args(args)

    initial_cpu_state = None
    initial_memory_bytes: dict[int, int] | None = None
    initial_frame_state = None
    initial_irq_state = None
    seed_from_doc, seed_from_payload = _load_seed_savestate_from_args(
        rom_path=rom_path,
        seed_from=args.seed_from,
        seed_checkpoint=args.seed_checkpoint,
        seed_session=args.seed_session,
    )
    if seed_from_doc is not None:
        initial_cpu_state = seed_from_doc.cpu
        initial_memory_bytes = dict(seed_from_doc.writable_overlay)
        initial_frame_state = seed_from_doc.frame_state
        initial_irq_state = seed_from_doc.irq_state

    target_pc = None if args.run_until is None else _parse_address(args.run_until)
    max_steps = args.max_steps if target_pc is not None else args.count
    view = load_fetch_view(rom_path, frame_state=initial_frame_state)
    payload = build_event_log_payload(
        rom_path=rom_path,
        rom_header=view.machine.header,
        view=view,
        start_pc=None if args.address is None else _parse_address(args.address),
        target_pc=target_pc,
        max_steps=max_steps,
        cpu_state=initial_cpu_state,
        memory_bytes=initial_memory_bytes,
        seed_registers=seed_registers,
        seed_xsp=None if args.seed_xsp is None else _parse_address(args.seed_xsp),
        seed_from_savestate=seed_from_payload,
        auto_tick_address=auto_tick_address,
        auto_tick_period=args.auto_tick_period,
        note=args.note,
    )
    return payload, seed_from_doc, seed_from_payload


def _eventlog_result_payload_from_capture(
    *,
    payload: dict[str, object],
    output_path: Path | None,
    seed_from_payload: dict[str, object] | None,
    seed_registers: dict[str, int],
    auto_tick_payload: dict[str, object] | None,
    map_path: str | None,
) -> tuple[dict[str, object], dict[str, object] | None]:
    summary = payload["summary"]
    run_context = payload["run_context"]
    rom = payload["rom"]
    assert isinstance(summary, dict)
    assert isinstance(run_context, dict)
    assert isinstance(rom, dict)

    result_payload = {
        "saved_to": None if output_path is None else str(output_path),
        "format_version": EVENT_LOG_FORMAT_VERSION,
        "rom_sha256": rom["sha256"],
        "emitted_count": summary["emitted_count"],
        "executed_count": summary["executed_count"],
        "stop_reason": summary["stop_reason"],
        "final_cpu_pc": summary["final_cpu_pc"],
        "final_cpu_pc_hex": summary["final_cpu_pc_hex"],
        "start_pc_hex": run_context["start_pc_hex"],
        "target_pc_hex": run_context["target_pc_hex"],
        "non_reference": auto_tick_payload,
        "note": payload.get("note"),
    }
    if seed_from_payload is not None:
        result_payload["seed_from"] = dict(seed_from_payload)
    if seed_registers:
        result_payload["seed_registers"] = _seed_registers_to_rows(seed_registers)

    final_pc_for_symbol = summary.get("final_cpu_pc")
    final_pc_int = final_pc_for_symbol if isinstance(final_pc_for_symbol, int) else None
    symbol_payload = _resolve_pc_symbol_payload(map_path, final_pc_int)
    if symbol_payload is not None:
        result_payload["final_symbol"] = symbol_payload
    return result_payload, symbol_payload


def _cmd_eventlog_capture(args: argparse.Namespace) -> int:
    rom_path = Path(args.rom)
    output_path = Path(args.output)
    seed_registers = _parse_seed_registers(args)
    _auto_tick_address, auto_tick_payload = _parse_auto_tick_args(args)
    payload, seed_from_doc, seed_from_payload = _build_eventlog_payload_from_args(args)
    save_event_log(output_path, payload)

    result_payload, symbol_payload = _eventlog_result_payload_from_capture(
        payload=payload,
        output_path=output_path,
        seed_from_payload=seed_from_payload,
        seed_registers=seed_registers,
        auto_tick_payload=auto_tick_payload,
        map_path=args.map,
    )
    summary = payload["summary"]
    run_context = payload["run_context"]
    rom = payload["rom"]
    assert isinstance(summary, dict)
    assert isinstance(run_context, dict)
    assert isinstance(rom, dict)

    if args.json:
        print(json.dumps(result_payload, indent=2, sort_keys=True))
        return 0

    print(f"Saved event log: {output_path}")
    print(f"Format version: {EVENT_LOG_FORMAT_VERSION}")
    print(f"ROM sha256: {rom['sha256']}")
    print(f"Start PC: {run_context['start_pc_hex']}")
    if run_context["target_pc_hex"] is not None:
        print(f"Target PC: {run_context['target_pc_hex']}")
    _print_auto_tick_summary(auto_tick_payload)
    symbol_line = _format_symbol_line(symbol_payload)
    if symbol_line is not None:
        print(symbol_line)
    if seed_from_doc is not None:
        print(
            f"Seed from state: {_seed_source_label(result_payload['seed_from'])} "
            f"(format {seed_from_doc.format_version}, "
            f"CPU PC 0x{seed_from_doc.cpu.pc:08X}, "
            f"{len(seed_from_doc.writable_overlay)} overlay bytes)"
        )
    print(f"Emitted events: {summary['emitted_count']}")
    print(f"Executed steps: {summary['executed_count']}")
    print(f"Stop reason: {summary['stop_reason']}")
    print(f"Final PC: {summary['final_cpu_pc_hex']}")
    if payload.get("note"):
        print(f"Note: {payload['note']}")
    return 0


def _cmd_eventlog_check(args: argparse.Namespace) -> int:
    rom_path = Path(args.rom)
    golden_path = Path(args.golden)
    save_current_path = None if args.save_current is None else Path(args.save_current)
    seed_registers = _parse_seed_registers(args)
    _auto_tick_address, auto_tick_payload = _parse_auto_tick_args(args)

    golden_payload = load_event_log(golden_path, expected_rom_path=rom_path)
    current_payload, seed_from_doc, seed_from_payload = _build_eventlog_payload_from_args(args)
    if save_current_path is not None:
        save_event_log(save_current_path, current_payload)

    capture_payload, symbol_payload = _eventlog_result_payload_from_capture(
        payload=current_payload,
        output_path=save_current_path,
        seed_from_payload=seed_from_payload,
        seed_registers=seed_registers,
        auto_tick_payload=auto_tick_payload,
        map_path=args.map,
    )
    diff_payload = diff_event_logs(golden_payload, current_payload)
    status = "match" if diff_payload["first_divergence"] is None else "mismatch"
    exit_code = 0 if status == "match" else 1
    result_payload = {
        "status": status,
        "golden": str(golden_path),
        "current_capture": capture_payload,
        "diff": diff_payload,
    }
    if save_current_path is not None:
        result_payload["saved_current"] = str(save_current_path)

    if args.json:
        print(json.dumps(result_payload, indent=2, sort_keys=True))
        return exit_code

    print(f"Golden event log: {golden_path}")
    if save_current_path is not None:
        print(f"Saved current log: {save_current_path}")
    print(f"ROM: {rom_path}")
    if seed_from_doc is not None:
        print(
            f"Seed from state: {_seed_source_label(capture_payload['seed_from'])} "
            f"(format {seed_from_doc.format_version}, "
            f"CPU PC 0x{seed_from_doc.cpu.pc:08X}, "
            f"{len(seed_from_doc.writable_overlay)} overlay bytes)"
        )
    _print_auto_tick_summary(auto_tick_payload)
    if symbol_payload is not None:
        symbol_line = _format_symbol_line(symbol_payload)
        if symbol_line is not None:
            print(symbol_line)
    print(f"Status: {status}")
    first = diff_payload["first_divergence"]
    if first is None:
        print("First divergence: <none>")
        print(f"Note: {diff_payload['note']}")
        return exit_code

    assert isinstance(first, dict)
    print(f"First divergence kind: {first['kind']}")
    if first["kind"] == "event":
        print(f"Event index: {first['index']}")
        left_event = first["left"]
        right_event = first["right"]
        assert isinstance(left_event, dict)
        assert isinstance(right_event, dict)
        print(
            f"Golden:  {left_event['pc_hex']} {left_event['raw_bytes_hex'] or '<none>'} "
            f"{left_event['assembly'] or '<unknown>'} => {left_event['status']}"
        )
        print(
            f"Current: {right_event['pc_hex']} {right_event['raw_bytes_hex'] or '<none>'} "
            f"{right_event['assembly'] or '<unknown>'} => {right_event['status']}"
        )
    elif first["kind"] == "length":
        print(
            f"Event counts: golden={first['left_event_count']} "
            f"current={first['right_event_count']}"
        )
    else:
        print("Run context differs between the golden log and the current run.")
    print(f"Note: {diff_payload['note']}")
    return exit_code


def _cmd_eventlog_golden_save(args: argparse.Namespace) -> int:
    rom_path = Path(args.rom)
    golden_path = golden_path_for_rom(rom_path, args.name)
    seed_registers = _parse_seed_registers(args)
    _auto_tick_address, auto_tick_payload = _parse_auto_tick_args(args)
    payload, _seed_from_doc, seed_from_payload = _build_eventlog_payload_from_args(args)
    save_named_golden(golden_path, payload)

    result_payload, symbol_payload = _eventlog_result_payload_from_capture(
        payload=payload,
        output_path=golden_path,
        seed_from_payload=seed_from_payload,
        seed_registers=seed_registers,
        auto_tick_payload=auto_tick_payload,
        map_path=args.map,
    )
    out = {
        "name": args.name,
        "path": str(golden_path),
        "slug": golden_path.name,
        **result_payload,
    }

    if args.json:
        print(json.dumps(out, indent=2, sort_keys=True))
        return 0

    print(f"Saved golden event log: {args.name}")
    print(f"Path: {golden_path}")
    _print_auto_tick_summary(auto_tick_payload)
    if symbol_payload is not None:
        symbol_line = _format_symbol_line(symbol_payload)
        if symbol_line is not None:
            print(symbol_line)
    print(f"Executed steps: {out['executed_count']}")
    print(f"Stop reason: {out['stop_reason']}")
    print(f"Final PC: {out['final_cpu_pc_hex']}")
    return 0


def _cmd_eventlog_golden_load(args: argparse.Namespace) -> int:
    golden = load_named_golden(Path(args.rom), args.name)
    payload = golden.payload
    summary = payload["summary"]
    run_context = payload["run_context"]
    assert isinstance(summary, dict)
    assert isinstance(run_context, dict)

    out = {
        "name": golden.name,
        "path": str(golden.path),
        "format_version": payload["format_version"],
        "created_at_utc": payload.get("created_at_utc"),
        "rom_sha256": payload["rom"]["sha256"],  # type: ignore[index]
        "start_pc_hex": run_context["start_pc_hex"],
        "target_pc_hex": run_context["target_pc_hex"],
        "executed_count": summary["executed_count"],
        "emitted_count": summary["emitted_count"],
        "stop_reason": summary["stop_reason"],
        "final_cpu_pc_hex": summary["final_cpu_pc_hex"],
        "note": payload.get("note"),
    }
    if args.json:
        print(json.dumps(out, indent=2, sort_keys=True))
        return 0

    print(f"Loaded golden event log: {golden.name}")
    print(f"Path: {golden.path}")
    if out["created_at_utc"]:
        print(f"Created at: {out['created_at_utc']}")
    print(f"Start PC: {out['start_pc_hex']}")
    if out["target_pc_hex"] is not None:
        print(f"Target PC: {out['target_pc_hex']}")
    print(f"Executed steps: {out['executed_count']}")
    print(f"Stop reason: {out['stop_reason']}")
    print(f"Final PC: {out['final_cpu_pc_hex']}")
    if out["note"]:
        print(f"Note: {out['note']}")
    return 0


def _cmd_eventlog_golden_list(args: argparse.Namespace) -> int:
    goldens = list_named_goldens(Path(args.rom))
    payload = {
        "rom": str(Path(args.rom)),
        "count": len(goldens),
        "goldens": [],
    }
    for golden in goldens:
        summary = golden.payload["summary"]
        assert isinstance(summary, dict)
        payload["goldens"].append(
            {
                "name": golden.name,
                "path": str(golden.path),
                "created_at_utc": golden.payload.get("created_at_utc"),
                "executed_count": summary["executed_count"],
                "emitted_count": summary["emitted_count"],
                "stop_reason": summary["stop_reason"],
                "final_cpu_pc_hex": summary["final_cpu_pc_hex"],
                "note": golden.payload.get("note"),
            }
        )
    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0

    print(f"Golden count: {len(goldens)}")
    for golden_row in payload["goldens"]:  # type: ignore[index]
        print(
            f"- {golden_row['name']}: PC {golden_row['final_cpu_pc_hex']}, "
            f"stop={golden_row['stop_reason']}, path={golden_row['path']}"
        )
    return 0


def _cmd_eventlog_golden_delete(args: argparse.Namespace) -> int:
    deleted_path = delete_named_golden(Path(args.rom), args.name)
    payload = {"name": args.name, "deleted_path": str(deleted_path)}
    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0

    print(f"Deleted golden event log: {args.name}")
    print(f"Path: {deleted_path}")
    return 0


def _cmd_eventlog_golden_check(args: argparse.Namespace) -> int:
    rom_path = Path(args.rom)
    named = load_named_golden(rom_path, args.name)
    save_current_path = None if args.save_current is None else Path(args.save_current)
    seed_registers = _parse_seed_registers(args)
    _auto_tick_address, auto_tick_payload = _parse_auto_tick_args(args)

    current_payload, seed_from_doc, seed_from_payload = _build_eventlog_payload_from_args(args)
    if save_current_path is not None:
        save_event_log(save_current_path, current_payload)

    capture_payload, symbol_payload = _eventlog_result_payload_from_capture(
        payload=current_payload,
        output_path=save_current_path,
        seed_from_payload=seed_from_payload,
        seed_registers=seed_registers,
        auto_tick_payload=auto_tick_payload,
        map_path=args.map,
    )
    diff_payload = diff_event_logs(named.payload, current_payload)
    status = "match" if diff_payload["first_divergence"] is None else "mismatch"
    exit_code = 0 if status == "match" else 1
    result_payload = {
        "status": status,
        "golden_name": named.name,
        "golden_path": str(named.path),
        "current_capture": capture_payload,
        "diff": diff_payload,
    }
    if save_current_path is not None:
        result_payload["saved_current"] = str(save_current_path)

    if args.json:
        print(json.dumps(result_payload, indent=2, sort_keys=True))
        return exit_code

    print(f"Golden event log: {named.name}")
    print(f"Golden path: {named.path}")
    if save_current_path is not None:
        print(f"Saved current log: {save_current_path}")
    print(f"ROM: {rom_path}")
    if seed_from_doc is not None:
        print(
            f"Seed from state: {_seed_source_label(capture_payload['seed_from'])} "
            f"(format {seed_from_doc.format_version}, "
            f"CPU PC 0x{seed_from_doc.cpu.pc:08X}, "
            f"{len(seed_from_doc.writable_overlay)} overlay bytes)"
        )
    _print_auto_tick_summary(auto_tick_payload)
    if symbol_payload is not None:
        symbol_line = _format_symbol_line(symbol_payload)
        if symbol_line is not None:
            print(symbol_line)
    print(f"Status: {status}")
    first = diff_payload["first_divergence"]
    if first is None:
        print("First divergence: <none>")
        print(f"Note: {diff_payload['note']}")
        return exit_code

    assert isinstance(first, dict)
    print(f"First divergence kind: {first['kind']}")
    if first["kind"] == "event":
        print(f"Event index: {first['index']}")
        left_event = first["left"]
        right_event = first["right"]
        assert isinstance(left_event, dict)
        assert isinstance(right_event, dict)
        print(
            f"Golden:  {left_event['pc_hex']} {left_event['raw_bytes_hex'] or '<none>'} "
            f"{left_event['assembly'] or '<unknown>'} => {left_event['status']}"
        )
        print(
            f"Current: {right_event['pc_hex']} {right_event['raw_bytes_hex'] or '<none>'} "
            f"{right_event['assembly'] or '<unknown>'} => {right_event['status']}"
        )
    elif first["kind"] == "length":
        print(
            f"Event counts: golden={first['left_event_count']} "
            f"current={first['right_event_count']}"
        )
    else:
        print("Run context differs between the golden log and the current run.")
    print(f"Note: {diff_payload['note']}")
    return exit_code


def _cmd_eventlog_golden_check_all(args: argparse.Namespace) -> int:
    rom_path = Path(args.rom)
    goldens = list_named_goldens(rom_path)

    if not goldens:
        if args.json:
            print(json.dumps(
                {
                    "rom": str(rom_path),
                    "total": 0, "passed": 0, "failed": 0,
                    "all_equal": True,
                    "stopped_early": False,
                    "results": [],
                },
                indent=2, sort_keys=True,
            ))
        else:
            print(f"No event-log goldens registered for {rom_path}")
        return 0

    seed_registers = _parse_seed_registers(args)
    _auto_tick_address, auto_tick_payload = _parse_auto_tick_args(args)
    current_payload, seed_from_doc, seed_from_payload = (
        _build_eventlog_payload_from_args(args)
    )

    save_current_path = (
        None if args.save_current is None else Path(args.save_current)
    )
    if save_current_path is not None:
        save_event_log(save_current_path, current_payload)

    capture_payload, symbol_payload = _eventlog_result_payload_from_capture(
        payload=current_payload,
        output_path=save_current_path,
        seed_from_payload=seed_from_payload,
        seed_registers=seed_registers,
        auto_tick_payload=auto_tick_payload,
        map_path=args.map,
    )

    results: list[dict[str, object]] = []
    passed = 0
    failed = 0
    short_circuited = False
    for golden in goldens:
        diff_payload = diff_event_logs(golden.payload, current_payload)
        status = "match" if diff_payload["first_divergence"] is None else "mismatch"
        results.append({
            "name": golden.name,
            "golden_path": str(golden.path),
            "status": status,
            "first_divergence": diff_payload["first_divergence"],
            "note": diff_payload["note"],
        })
        if status == "match":
            passed += 1
        else:
            failed += 1
            if args.stop_on_fail:
                short_circuited = True
                break

    all_match = failed == 0
    exit_code = 0 if all_match else 1

    if args.json:
        payload = {
            "rom": str(rom_path),
            "total": len(goldens),
            "checked": len(results),
            "passed": passed,
            "failed": failed,
            "stopped_early": short_circuited,
            "all_equal": all_match,
            "current_capture": capture_payload,
            "results": results,
        }
        if save_current_path is not None:
            payload["saved_current"] = str(save_current_path)
        print(json.dumps(payload, indent=2, sort_keys=True))
        return exit_code

    print(f"ROM: {rom_path}")
    if seed_from_doc is not None:
        print(
            f"Seed from state: {_seed_source_label(capture_payload['seed_from'])} "
            f"(format {seed_from_doc.format_version}, "
            f"CPU PC 0x{seed_from_doc.cpu.pc:08X}, "
            f"{len(seed_from_doc.writable_overlay)} overlay bytes)"
        )
    _print_auto_tick_summary(auto_tick_payload)
    if symbol_payload is not None:
        symbol_line = _format_symbol_line(symbol_payload)
        if symbol_line is not None:
            print(symbol_line)
    if save_current_path is not None:
        print(f"Saved current log: {save_current_path}")
    print(
        f"Event-log goldens: {len(results)}/{len(goldens)} checked, "
        f"{passed} passed, {failed} failed"
        + (" (stopped early)" if short_circuited else "")
    )
    for entry in results:
        name = entry["name"]
        if entry["status"] == "match":
            print(f"  [OK]       {name}")
        else:
            first = entry["first_divergence"]
            if isinstance(first, dict):
                kind = first.get("kind", "?")
                if kind == "event":
                    idx = first.get("index", "?")
                    print(f"  [MISMATCH] {name}  event[{idx}]")
                elif kind == "length":
                    print(
                        f"  [MISMATCH] {name}  length golden="
                        f"{first.get('left_event_count')} current="
                        f"{first.get('right_event_count')}"
                    )
                else:
                    print(f"  [MISMATCH] {name}  kind={kind}")
            else:
                print(f"  [MISMATCH] {name}")
    return exit_code


def _cmd_eventlog_inspect(args: argparse.Namespace) -> int:
    input_path = Path(args.input)
    expected_rom = Path(args.rom) if args.rom else None
    payload = load_event_log(input_path, expected_rom_path=expected_rom)

    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0

    summary_payload = _event_log_summary_to_dict(payload, str(input_path))
    run_context = summary_payload["run_context"]
    summary = summary_payload["summary"]
    assert isinstance(run_context, dict)
    assert isinstance(summary, dict)

    print(f"Loaded event log: {input_path}")
    print(f"Format version: {summary_payload['format_version']}")
    if summary_payload["created_at_utc"]:
        print(f"Created at: {summary_payload['created_at_utc']}")
    print(f"ROM sha256: {summary_payload['rom_sha256']}")
    print(f"ROM title: {summary_payload['rom_header_title'] or '<empty>'}")
    print(f"ROM entry point: {summary_payload['rom_header_entry_point_hex']}")
    print(f"Start PC: {run_context['start_pc_hex']}")
    if run_context["target_pc_hex"] is not None:
        print(f"Target PC: {run_context['target_pc_hex']}")
    print(f"Max steps: {run_context['max_steps']}")
    print(f"Emitted events: {summary['emitted_count']}")
    print(f"Executed steps: {summary['executed_count']}")
    print(f"Stop reason: {summary['stop_reason']}")
    print(f"Final PC: {summary['final_cpu_pc_hex']}")
    if summary_payload["note"]:
        print(f"Note: {summary_payload['note']}")
    if expected_rom is None:
        print(
            "WARNING: no --rom passed; ROM content hash was NOT verified against a "
            "real ROM file."
        )

    if args.limit > 0:
        events = payload["events"]
        assert isinstance(events, list)
        print("Events:")
        for event in events[: args.limit]:
            assert isinstance(event, dict)
            line = f"  [{event['index']}] {event['pc_hex']} {event['raw_bytes_hex'] or '<none>'}"
            if event["assembly"] is not None:
                line += f"  {event['assembly']}"
            line += f"  => {event['status']}"
            print(line)
    return 0


def _cmd_eventlog_diff(args: argparse.Namespace) -> int:
    left = load_event_log(Path(args.left))
    right = load_event_log(Path(args.right))
    diff_payload = diff_event_logs(left, right)

    if args.json:
        print(json.dumps(diff_payload, indent=2, sort_keys=True))
        return 0

    print(f"ROM sha256: {diff_payload['rom_sha256']}")
    first = diff_payload["first_divergence"]
    if first is None:
        print("First divergence: <none>")
        print(f"Note: {diff_payload['note']}")
        return 0

    assert isinstance(first, dict)
    print(f"First divergence kind: {first['kind']}")
    if first["kind"] == "event":
        print(f"Event index: {first['index']}")
        left_event = first["left"]
        right_event = first["right"]
        assert isinstance(left_event, dict)
        assert isinstance(right_event, dict)
        print(
            f"Left:  {left_event['pc_hex']} {left_event['raw_bytes_hex'] or '<none>'} "
            f"{left_event['assembly'] or '<unknown>'} => {left_event['status']}"
        )
        print(
            f"Right: {right_event['pc_hex']} {right_event['raw_bytes_hex'] or '<none>'} "
            f"{right_event['assembly'] or '<unknown>'} => {right_event['status']}"
        )
    elif first["kind"] == "length":
        print(
            f"Event counts: left={first['left_event_count']} "
            f"right={first['right_event_count']}"
        )
    else:
        print("Run context differs between the two logs.")
    print(f"Note: {diff_payload['note']}")
    return 0


def _parse_address_arg(raw: str) -> int:
    """Parse a CLI address argument (decimal or 0x-prefixed hex)."""
    s = raw.strip()
    base = 16 if s.lower().startswith("0x") else 10
    try:
        return int(s, base)
    except ValueError as exc:
        raise ValueError(f"invalid address: {raw!r}") from exc


def _cmd_eventlog_profile(args: argparse.Namespace) -> int:
    input_path = Path(args.input)
    expected_rom = Path(args.rom) if args.rom else None
    payload = load_event_log(input_path, expected_rom_path=expected_rom)
    symbol_table = load_map(args.map)
    profile = bucket_event_log_by_symbol(payload, symbol_table)

    top = args.top if args.top is not None and args.top > 0 else len(profile["buckets"])

    if args.json:
        out = dict(profile)
        out["buckets"] = profile["buckets"][:top]
        print(json.dumps(out, indent=2, sort_keys=True))
        return 0

    print(f"Loaded event log: {input_path}")
    print(f"Map: {profile['map_source']}")
    if expected_rom is None:
        print(
            "WARNING: no --rom passed; ROM content hash was NOT verified "
            "against a real ROM file."
        )
    print(f"Profile format: {profile['format_version']}")
    if profile["final_cpu_pc_hex"]:
        print(f"Final CPU PC: {profile['final_cpu_pc_hex']}")
    print(f"Total events: {profile['total_events']}")
    print(f"Resolved   : {profile['resolved_events']}")
    print(f"Unresolved : {profile['unresolved_events']}")
    print(f"Distinct symbols hit: {profile['distinct_symbols']}")
    if profile["halted_status_breakdown"]:
        print("Halted statuses:")
        for status, n in profile["halted_status_breakdown"].items():
            print(f"  {status:35} {n}")
    print()
    header = (
        f"{'Symbol':<38} {'total':>6} {'exec':>6} {'halt':>6} "
        f"{'first':<11} {'last':<11} {'section':<18}"
    )
    print(header)
    print("-" * len(header))
    for bucket in profile["buckets"][:top]:
        print(
            f"{bucket['symbol']:<38} "
            f"{bucket['total_events']:>6} "
            f"{bucket['executed_events']:>6} "
            f"{bucket['halted_events']:>6} "
            f"{bucket['first_pc_hex']:<11} "
            f"{bucket['last_pc_hex']:<11} "
            f"{bucket['section']:<18}"
        )
    if top < len(profile["buckets"]):
        rest = len(profile["buckets"]) - top
        print(f"... ({rest} more bucket(s) omitted, use --top 0 for all)")
    return 0


def _watchpoint_to_row(wp: Watchpoint) -> dict[str, object]:
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


def _watchpoint_hit_to_row(hit: WatchpointHit) -> dict[str, object]:
    return {
        "watchpoint": _watchpoint_to_row(hit.watchpoint),
        "event_index": hit.event_index,
        "event_pc": hit.event_pc,
        "event_pc_hex": f"0x{hit.event_pc:08X}",
        "access_kind": hit.access_kind,
        "address": hit.address,
        "address_hex": f"0x{hit.address:06X}",
        "size": hit.size,
        "data_hex": hit.data_hex,
        "assembly": hit.assembly,
    }


def _cmd_watchpoint_add(args: argparse.Namespace) -> int:
    rom_path = Path(args.rom)
    address = _parse_address_arg(args.address)
    value = None if args.value is None else _parse_address_arg(args.value)
    wp = add_watchpoint(
        rom_path,
        kind=args.kind,
        start=address,
        size=args.size,
        label=args.label,
        value=value,
    )
    payload = {
        "rom": str(rom_path),
        "registry_path": str(watchpoints_path_for_rom(rom_path)),
        "watchpoint": _watchpoint_to_row(wp),
    }
    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0
    print(
        f"Added watchpoint #{wp.id}: kind={wp.kind} "
        f"range=[{wp.start:#08x}..{wp.end_inclusive():#08x}] "
        f"size={wp.size} label={wp.label!r}"
    )
    print(f"Registry: {payload['registry_path']}")
    return 0


def _cmd_watchpoint_list(args: argparse.Namespace) -> int:
    rom_path = Path(args.rom)
    watchpoints = load_watchpoints(rom_path)
    payload = {
        "rom": str(rom_path),
        "registry_path": str(watchpoints_path_for_rom(rom_path)),
        "format_version": WATCHPOINTS_FORMAT_VERSION,
        "count": len(watchpoints),
        "watchpoints": [_watchpoint_to_row(wp) for wp in watchpoints],
    }
    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0
    print(f"Watchpoints for {rom_path.name}: {len(watchpoints)}")
    for wp in watchpoints:
        print(
            f"  #{wp.id}: {wp.kind} "
            f"[{wp.start:#08x}..{wp.end_inclusive():#08x}] "
            f"size={wp.size} label={wp.label!r}"
        )
    return 0


def _cmd_watchpoint_remove(args: argparse.Namespace) -> int:
    rom_path = Path(args.rom)
    try:
        removed = remove_watchpoint(rom_path, args.id)
    except KeyError as exc:
        if args.json:
            print(json.dumps({"error": str(exc)}, indent=2, sort_keys=True))
        else:
            print(f"Error: {exc}", file=sys.stderr)
        return 1
    payload = {
        "rom": str(rom_path),
        "removed": _watchpoint_to_row(removed),
    }
    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0
    print(
        f"Removed watchpoint #{removed.id}: {removed.kind} "
        f"[{removed.start:#08x}..{removed.end_inclusive():#08x}]"
    )
    return 0


def _cmd_watchpoint_clear(args: argparse.Namespace) -> int:
    rom_path = Path(args.rom)
    dropped = clear_watchpoints(rom_path)
    payload = {
        "rom": str(rom_path),
        "dropped_count": dropped,
    }
    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0
    print(f"Cleared {dropped} watchpoint(s) for {rom_path.name}.")
    return 0


def _breakpoint_to_row(bp: Breakpoint) -> dict[str, object]:
    return {
        "id": bp.id,
        "address": bp.address,
        "address_hex": f"0x{bp.address:08X}",
        "label": bp.label,
    }


def _breakpoint_hit_to_row(hit: BreakpointHit) -> dict[str, object]:
    return {
        "breakpoint": _breakpoint_to_row(hit.breakpoint),
        "event_index": hit.event_index,
        "event_pc": hit.event_pc,
        "event_pc_hex": f"0x{hit.event_pc:08X}",
        "assembly": hit.assembly,
        "status": hit.status,
    }


def _cmd_breakpoint_add(args: argparse.Namespace) -> int:
    rom_path = Path(args.rom)
    address = _parse_address_arg(args.address)
    bp = add_breakpoint(rom_path, address=address, label=args.label)
    payload = {
        "rom": str(rom_path),
        "registry_path": str(breakpoints_path_for_rom(rom_path)),
        "breakpoint": _breakpoint_to_row(bp),
    }
    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0
    print(
        f"Added breakpoint #{bp.id} at {bp.address:#010x} "
        f"label={bp.label!r}"
    )
    print(f"Registry: {payload['registry_path']}")
    return 0


def _cmd_breakpoint_add_symbol(args: argparse.Namespace) -> int:
    rom_path = Path(args.rom)
    symbol_table = load_map(args.map)
    sym = symbol_table.lookup_name(args.symbol)
    if sym is None:
        msg = (
            f"symbol {args.symbol!r} not found in {args.map}. "
            f"Map carries {len(symbol_table)} symbol(s)."
        )
        if args.json:
            print(json.dumps({"error": msg}, indent=2, sort_keys=True))
        else:
            print(f"Error: {msg}", file=sys.stderr)
        return 1

    # Use the user-supplied label if any; otherwise fall back to the symbol
    # name so the breakpoint stays self-describing in `breakpoint list`.
    label = args.label if args.label is not None else sym.name
    bp = add_breakpoint(rom_path, address=sym.address, label=label)
    payload = {
        "rom": str(rom_path),
        "map": args.map,
        "registry_path": str(breakpoints_path_for_rom(rom_path)),
        "resolved_symbol": {
            "name": sym.name,
            "address": sym.address,
            "address_hex": f"0x{sym.address:08X}",
            "section": sym.section,
        },
        "breakpoint": _breakpoint_to_row(bp),
    }
    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0
    print(
        f"Resolved symbol {sym.name!r} (section {sym.section}) -> "
        f"0x{sym.address:08X}"
    )
    print(
        f"Added breakpoint #{bp.id} at {bp.address:#010x} label={bp.label!r}"
    )
    print(f"Registry: {payload['registry_path']}")
    return 0


def _cmd_breakpoint_list(args: argparse.Namespace) -> int:
    rom_path = Path(args.rom)
    breakpoints = load_breakpoints(rom_path)
    payload = {
        "rom": str(rom_path),
        "registry_path": str(breakpoints_path_for_rom(rom_path)),
        "format_version": BREAKPOINTS_FORMAT_VERSION,
        "count": len(breakpoints),
        "breakpoints": [_breakpoint_to_row(bp) for bp in breakpoints],
    }
    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0
    print(f"Breakpoints for {rom_path.name}: {len(breakpoints)}")
    for bp in breakpoints:
        print(f"  #{bp.id}: {bp.address:#010x} label={bp.label!r}")
    return 0


def _cmd_breakpoint_remove(args: argparse.Namespace) -> int:
    rom_path = Path(args.rom)
    try:
        removed = remove_breakpoint(rom_path, args.id)
    except KeyError as exc:
        if args.json:
            print(json.dumps({"error": str(exc)}, indent=2, sort_keys=True))
        else:
            print(f"Error: {exc}", file=sys.stderr)
        return 1
    payload = {"rom": str(rom_path), "removed": _breakpoint_to_row(removed)}
    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0
    print(f"Removed breakpoint #{removed.id} at {removed.address:#010x}")
    return 0


def _cmd_breakpoint_clear(args: argparse.Namespace) -> int:
    rom_path = Path(args.rom)
    dropped = clear_breakpoints(rom_path)
    payload = {"rom": str(rom_path), "dropped_count": dropped}
    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0
    print(f"Cleared {dropped} breakpoint(s) for {rom_path.name}.")
    return 0


def _cmd_breakpoint_check(args: argparse.Namespace) -> int:
    rom_path = Path(args.rom)
    event_log_path = Path(args.event_log)
    payload_event_log = load_event_log(event_log_path, expected_rom_path=rom_path)
    breakpoints = load_breakpoints(rom_path)
    hits = match_event_log_pc(breakpoints, payload_event_log)
    payload = {
        "rom": str(rom_path),
        "event_log": str(event_log_path),
        "breakpoint_count": len(breakpoints),
        "hit_count": len(hits),
        "hits": [_breakpoint_hit_to_row(hit) for hit in hits],
    }
    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0
    print(
        f"Event log: {event_log_path}\n"
        f"Breakpoints: {len(breakpoints)} | Hits: {len(hits)}"
    )
    for row in payload["hits"]:  # type: ignore[index]
        bp_row = row["breakpoint"]
        print(
            f"  event[{row['event_index']}] pc={row['event_pc_hex']} "
            f"status={row['status']} -> breakpoint #{bp_row['id']} "
            f"label={bp_row['label']!r}: {row['assembly']}"
        )
    return 0


def _cmd_watchpoint_check(args: argparse.Namespace) -> int:
    rom_path = Path(args.rom)
    event_log_path = Path(args.event_log)
    payload_event_log = load_event_log(event_log_path, expected_rom_path=rom_path)
    watchpoints = load_watchpoints(rom_path)
    hits = match_event_log_accesses(watchpoints, payload_event_log)
    payload = {
        "rom": str(rom_path),
        "event_log": str(event_log_path),
        "watchpoint_count": len(watchpoints),
        "hit_count": len(hits),
        "hits": [_watchpoint_hit_to_row(hit) for hit in hits],
    }
    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0
    print(
        f"Event log: {event_log_path}\n"
        f"Watchpoints: {len(watchpoints)} | Hits: {len(hits)}"
    )
    for row in payload["hits"]:  # type: ignore[index]
        wp_row = row["watchpoint"]
        verb = "wrote" if row["access_kind"] == "write" else "read"
        print(
            f"  event[{row['event_index']}] pc={row['event_pc_hex']} "
            f"{verb} {row['size']}B at {row['address_hex']}={row['data_hex']} "
            f"-> watchpoint #{wp_row['id']} ({wp_row['kind']}, "
            f"label={wp_row['label']!r}): {row['assembly']}"
        )
    return 0


def _resolve_pc_symbol_payload(
    map_path: str | None,
    pc: int | None,
) -> dict[str, object] | None:
    """Resolve a PC back to its owning symbol via a t900ld .map file.

    Returns a stable JSON-ready dict, or None when no map was requested.
    On lookup miss, the dict still reports `found=False` so the caller can
    distinguish "no map was requested" (None) from "map was loaded but
    nothing matched" (`found: false`).

    This is the single point where execution commands gain symbol
    awareness. Adding `--map` to a command is strictly additive: the
    existing execution payload is unchanged when the flag is absent.
    """
    if map_path is None:
        return None
    table = load_map(map_path)
    if pc is None:
        return {
            "map_source": table.source_path,
            "queried_pc": None,
            "found": False,
            "owning_symbol": None,
            "owning_symbol_address_hex": None,
            "offset_from_symbol": None,
            "section": None,
            "note": (
                "Reverse lookup not performed: no final PC was available "
                "from the execution result (likely a halt before any "
                "instruction executed)."
            ),
        }
    sym = table.lookup_address(pc)
    return {
        "map_source": table.source_path,
        "queried_pc": pc,
        "queried_pc_hex": f"0x{pc:08X}",
        "found": sym is not None,
        "owning_symbol": sym.name if sym else None,
        "owning_symbol_address_hex": (
            f"0x{sym.address:08X}" if sym else None
        ),
        "offset_from_symbol": (pc - sym.address) if sym else None,
        "section": sym.section if sym else None,
        "note": (
            "Reverse lookup returns the symbol with the highest address "
            "<= the final PC. This is the function or label that owns the "
            "PC at the stop frontier in normal program flow."
        ),
    }


def _format_symbol_line(symbol_payload: dict[str, object] | None) -> str | None:
    """Render a one-line human-readable symbol report for non-JSON output."""
    if symbol_payload is None:
        return None
    if not symbol_payload.get("found"):
        return "Final symbol: <no symbol at or below final PC>"
    name = symbol_payload["owning_symbol"]
    offset = symbol_payload.get("offset_from_symbol") or 0
    base_hex = symbol_payload.get("owning_symbol_address_hex")
    suffix = "" if offset == 0 else f" + 0x{offset:X}"
    return f"Final symbol: {name}{suffix}  (base {base_hex})"


def _cmd_map_info(args: argparse.Namespace) -> int:
    table = load_map(args.map)
    if args.json:
        payload = {
            "source_path": table.source_path,
            "total_symbols": len(table),
            "sections": [
                {"name": sec, "count": n}
                for sec, n in table.section_summary()
            ],
            "note": (
                "Symbol counts per section in the t900ld map file. The map "
                "is the authoritative source; the loader never fabricates "
                "symbols beyond what the file lists."
            ),
        }
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0
    print(f"Map     : {table.source_path}")
    print(f"Symbols : {len(table)}")
    print("Sections:")
    for sec, n in table.section_summary():
        print(f"  {sec:30} {n}")
    return 0


def _cmd_map_lookup_name(args: argparse.Namespace) -> int:
    table = load_map(args.map)
    sym = table.lookup_name(args.name)
    if args.json:
        payload = {
            "name": args.name,
            "found": sym is not None,
            "address": sym.address if sym else None,
            "address_hex": f"0x{sym.address:08X}" if sym else None,
            "section": sym.section if sym else None,
        }
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0
    if sym is None:
        print(f"NOT FOUND: {args.name}")
        return 1
    print(f"{sym.name:30} 0x{sym.address:08X}  ({sym.section})")
    return 0


def _cmd_map_lookup_addr(args: argparse.Namespace) -> int:
    table = load_map(args.map)
    address = _parse_address_arg(args.address)
    sym = table.lookup_address(address)
    if args.json:
        payload = {
            "query_address": address,
            "query_address_hex": f"0x{address:08X}",
            "found": sym is not None,
            "owning_symbol": sym.name if sym else None,
            "owning_symbol_address": sym.address if sym else None,
            "owning_symbol_address_hex": (
                f"0x{sym.address:08X}" if sym else None
            ),
            "offset_from_symbol": (address - sym.address) if sym else None,
            "section": sym.section if sym else None,
            "note": (
                "Reverse lookup returns the symbol with the highest address "
                "<= the requested PC (= the function or label that owns "
                "this PC in normal program execution). Returns null only "
                "when the requested PC is below every known symbol."
            ),
        }
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0
    if sym is None:
        print(f"0x{address:08X}  ->  no symbol at or below this address")
        return 1
    delta = address - sym.address
    suffix = "" if delta == 0 else f" + 0x{delta:X}"
    print(f"0x{address:08X}  ->  {sym.name}{suffix}  ({sym.section})")
    return 0


def _cmd_engine_bridge(args: argparse.Namespace) -> int:
    try:
        payload = execute_engine_bridge_request(Path(args.request))
    except FileNotFoundError as exc:
        payload = build_error_response(
            action=None,
            summary_text=f"file not found: {exc.filename}",
            error_type="file-not-found",
        )
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 1
    except EngineBridgeError as exc:
        payload = build_error_response(
            action=None,
            summary_text=str(exc),
            error_type="engine-bridge-error",
        )
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 1
    except ValueError as exc:
        payload = build_error_response(
            action=None,
            summary_text=str(exc),
            error_type="value-error",
        )
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 1

    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0 if payload.get("status") != "error" else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Minimal headless entry point for NgpCraft Emulator."
    )
    sub = parser.add_subparsers(dest="command", required=True)

    info = sub.add_parser("info", help="Read and print the NGPC ROM header.")
    info.add_argument("rom", help="Path to a .ngp/.ngc ROM file.")
    info.add_argument("--json", action="store_true", help="Emit JSON.")
    info.set_defaults(func=_cmd_info)

    ui = sub.add_parser(
        "ui",
        help=(
            "Launch the PyQt6 debugger UI (LCD canvas + CPU registers "
            "+ disassembly + memory inspector + Step/Run/Reset). "
            "Requires PyQt6 (pip install PyQt6)."
        ),
    )
    ui.add_argument(
        "rom", nargs="?", default=None,
        help=(
            "Optional path to a .ngp/.ngc ROM file. When omitted, the "
            "UI opens with an empty session ; use File → Open ROM…"
        ),
    )
    ui.add_argument(
        "--bios",
        help=(
            "Optional path to a 64 KB NGPC BIOS image. When provided, the "
            "live UI session can satisfy BIOS reads the same way the "
            "executor CLI commands do."
        ),
    )
    ui.set_defaults(func=_cmd_ui)

    opcode_cov = sub.add_parser(
        "opcode-coverage",
        help=(
            "Linear-walk a ROM from its entry point and report which "
            "leading-byte opcodes the decoder doesn't yet handle. "
            "Used to prioritize executor expansion work for HW fidelity."
        ),
    )
    opcode_cov.add_argument("rom", help="Path to a .ngp/.ngc ROM file.")
    opcode_cov.add_argument(
        "--start",
        help=(
            "Start address for the walk (decimal or 0x-hex). Defaults "
            "to the cart header entry point."
        ),
    )
    opcode_cov.add_argument(
        "--bytes", type=int, default=2048,
        help="Walk budget in bytes (default 2048).",
    )
    opcode_cov.add_argument(
        "--top", type=int, default=15,
        help="Number of top unknown-opcode entries to print (default 15).",
    )
    opcode_cov.add_argument(
        "--stop-on-silicon-broken",
        action="store_true",
        help=(
            "Stop the linear walk after the first decoded instruction that matches a "
            "known local silicon-broken quirk, instead of continuing into downstream bytes."
        ),
    )
    opcode_cov.add_argument(
        "--stop-on-non-fallthrough",
        action="store_true",
        help=(
            "Stop the linear walk after the first decoded instruction with "
            "`falls_through = False` (for example RET/RETI/JP/HALT/SWI)."
        ),
    )
    opcode_cov.add_argument(
        "--follow-direct-control-flow",
        action="store_true",
        help=(
            "Use a conservative static worklist instead of pure linear sweep: follow "
            "decoded fallthrough edges and known direct targets (JR/JRL/JP/CALL/CALR/DJNZ)."
        ),
    )
    opcode_cov.add_argument(
        "--json", action="store_true",
        help="Emit JSON instead of human-readable table.",
    )
    opcode_cov.set_defaults(func=_cmd_opcode_coverage)

    reset_info = sub.add_parser(
        "reset-info",
        help="Build the current minimal machine bootstrap state from a ROM.",
    )
    reset_info.add_argument("rom", help="Path to a .ngp/.ngc ROM file.")
    reset_info.add_argument("--json", action="store_true", help="Emit JSON.")
    reset_info.set_defaults(func=_cmd_reset_info)

    addr_info = sub.add_parser(
        "addr-info",
        help="Probe one address in the current minimal NGPC address map.",
    )
    addr_info.add_argument("rom", help="Path to a .ngp/.ngc ROM file.")
    addr_info.add_argument("address", help="Address in decimal or 0x-prefixed hex.")
    addr_info.add_argument("--json", action="store_true", help="Emit JSON.")
    addr_info.set_defaults(func=_cmd_addr_info)

    cpu_info = sub.add_parser(
        "cpu-info",
        help="Show the current minimal CPU state container derived from ROM bootstrap.",
    )
    cpu_info.add_argument("rom", help="Path to a .ngp/.ngc ROM file.")
    cpu_info.add_argument("--json", action="store_true", help="Emit JSON.")
    cpu_info.set_defaults(func=_cmd_cpu_info)

    registers = sub.add_parser(
        "registers",
        help=(
            "Rich CPU register view: 8 R32 with their R16 / R8 decomposition, "
            "PC, SR, IFF level, RFP and the six modeled flags. Use --seed-from "
            "to load a savestate captured during a run."
        ),
    )
    registers.add_argument("rom", help="Path to a .ngp/.ngc ROM file.")
    registers.add_argument(
        "--seed-from",
        default=None,
        help=(
            "Optional savestate JSON path. When set, CPU state is loaded "
            "from the savestate (ROM hash verified) instead of the bootstrap "
            "reset state."
        ),
    )
    registers.add_argument("--json", action="store_true", help="Emit JSON.")
    registers.set_defaults(func=_cmd_registers)

    peek = sub.add_parser(
        "peek",
        help="Read bytes through the current minimal read-only bus model.",
    )
    peek.add_argument("rom", help="Path to a .ngp/.ngc ROM file.")
    peek.add_argument("address", help="Address in decimal or 0x-prefixed hex.")
    peek.add_argument(
        "--bios",
        help="Optional 64 KB BIOS image used to back reads in 0xFF0000..0xFFFFFF.",
    )
    peek.add_argument(
        "--count",
        type=int,
        default=1,
        help="Number of bytes to read (current ROM-backed model only).",
    )
    peek.add_argument("--json", action="store_true", help="Emit JSON.")
    peek.set_defaults(func=_cmd_peek)

    memory_dump = sub.add_parser(
        "memory-dump",
        help=(
            "Hexdump-style memory inspector. Reads through the read bus and "
            "optionally overlays a savestate's writable cells. Human-readable "
            "by default; pass --json for a structured payload."
        ),
    )
    memory_dump.add_argument("rom", help="Path to a .ngp/.ngc ROM file.")
    memory_dump.add_argument(
        "address", help="Base address (decimal or 0x-prefixed hex)."
    )
    memory_dump.add_argument(
        "--count",
        type=int,
        default=64,
        help="Number of bytes to dump (default: 64).",
    )
    memory_dump.add_argument(
        "--width",
        type=int,
        default=16,
        help="Bytes per row (default: 16; 8 or 16 give the nicest grouping).",
    )
    memory_dump.add_argument(
        "--seed-from",
        default=None,
        help=(
            "Optional savestate JSON path. When set, its writable overlay "
            "is layered on top of the read bus, so cells written during the "
            "captured run shadow the cold-start image."
        ),
    )
    memory_dump.add_argument("--json", action="store_true", help="Emit JSON.")
    memory_dump.set_defaults(func=_cmd_memory_dump)

    palette_info = sub.add_parser(
        "palette-info",
        help=(
            "Decode the K2GE palette RAM (0x8200..0x83FF) into a human "
            "view. M2 Phase 0 inspector — does not render anything; only "
            "reads the current overlay + cold-start image and decodes the "
            "0BGR 12-bit entries. Use --kind to filter to one plane."
        ),
    )
    palette_info.add_argument("rom", help="Path to a .ngp/.ngc ROM file.")
    palette_info.add_argument(
        "--kind",
        choices=("all", "sprite", "scr1", "scr2", "background", "window"),
        default="all",
        help="Which palette plane to print (default: all five).",
    )
    palette_info.add_argument(
        "--seed-from",
        default=None,
        help=(
            "Optional savestate JSON path. When set, its writable overlay "
            "is layered on top of the cold-start palette image so cells "
            "written during the captured run are decoded."
        ),
    )
    palette_info.add_argument("--json", action="store_true", help="Emit JSON.")
    palette_info.set_defaults(func=_cmd_palette_info)

    oam_info = sub.add_parser(
        "oam-info",
        help=(
            "Decode the K2GE OAM (0x8800..0x88FF, 64 sprites × 4 bytes) and "
            "the CP.C palette-code strip (0x8C00..0x8C3F). M2 Phase 0 "
            "inspector — no rendering; reads the current overlay + cold-"
            "start image and decodes per-sprite tile, position, flip, "
            "priority code, chain bits and palette index."
        ),
    )
    oam_info.add_argument("rom", help="Path to a .ngp/.ngc ROM file.")
    oam_info.add_argument(
        "--visible-only",
        action="store_true",
        help="Filter out sprites whose priority code (PR.C) is 0 (hidden).",
    )
    oam_info.add_argument(
        "--seed-from",
        default=None,
        help=(
            "Optional savestate JSON path. When set, its writable overlay "
            "is layered on top of the cold-start OAM/CP.C image so cells "
            "written during the captured run are decoded."
        ),
    )
    oam_info.add_argument("--json", action="store_true", help="Emit JSON.")
    oam_info.set_defaults(func=_cmd_oam_info)

    tilemap_info = sub.add_parser(
        "tilemap-info",
        help=(
            "Decode one K2GE scroll-plane tilemap (SCR1 @ 0x9000 or "
            "SCR2 @ 0x9800, 32×32 tiles × 2 bytes). M2 Phase 0 inspector — "
            "default is a compact ASCII 32-wide grid; --list shows the "
            "full per-tile decode; --non-empty drops tile-0 cells."
        ),
    )
    tilemap_info.add_argument("rom", help="Path to a .ngp/.ngc ROM file.")
    tilemap_info.add_argument(
        "--plane",
        choices=("scr1", "scr2"),
        default="scr1",
        help="Which scroll plane to inspect (default: scr1).",
    )
    tilemap_info.add_argument(
        "--non-empty",
        action="store_true",
        help="Filter out tile-0 entries (the NGPC transparent / unused slot).",
    )
    tilemap_info.add_argument(
        "--list",
        action="store_true",
        help="Print one line per tile instead of the compact grid view.",
    )
    tilemap_info.add_argument(
        "--seed-from",
        default=None,
        help=(
            "Optional savestate JSON path. When set, its writable overlay "
            "is layered on top of the cold-start tilemap so cells written "
            "during the captured run are decoded."
        ),
    )
    tilemap_info.add_argument("--json", action="store_true", help="Emit JSON.")
    tilemap_info.set_defaults(func=_cmd_tilemap_info)

    tile_view = sub.add_parser(
        "tile-view",
        help=(
            "Render one 8×8 tile from CHAR_RAM as 4-level grayscale ASCII art. "
            "M2 Phase 0.5 — first visual lens. Optional --plane + --palette "
            "annotates each pixel with the resolved K2GE RGB color."
        ),
    )
    tile_view.add_argument("rom", help="Path to a .ngp/.ngc ROM file.")
    tile_view.add_argument(
        "tile_id",
        help="Tile index in CHAR_RAM (decimal or 0x-prefixed hex, 0..511).",
    )
    tile_view.add_argument(
        "--plane",
        choices=("sprite", "scr1", "scr2"),
        default=None,
        help="Palette plane to colorise pixels with (default: no colorisation).",
    )
    tile_view.add_argument(
        "--palette",
        type=int,
        default=None,
        help="Palette index 0..15 within the selected --plane.",
    )
    tile_view.add_argument(
        "--seed-from",
        default=None,
        help=(
            "Optional savestate JSON path. When set, its writable overlay "
            "is layered on top of the cold-start CHAR_RAM image so tiles "
            "loaded by the captured run are decoded."
        ),
    )
    tile_view.add_argument("--json", action="store_true", help="Emit JSON.")
    tile_view.set_defaults(func=_cmd_tile_view)

    tiles_view = sub.add_parser(
        "tiles-view",
        help=(
            "Render a grid of CHAR_RAM tiles as a binary P6 PPM atlas. "
            "Multi-tile bridge between `tile-view` (single 8×8 tile, "
            "ASCII) and the full framebuffer compose."
        ),
    )
    tiles_view.add_argument("rom", help="Path to a .ngp/.ngc ROM file.")
    tiles_view.add_argument(
        "--range",
        default="0..511",
        help=(
            "Tile range as 'N..M' (inclusive, decimal or 0x-hex) or a "
            "single tile id N. Default: 0..511 (full CHAR_RAM)."
        ),
    )
    tiles_view.add_argument(
        "--cols",
        type=int,
        default=16,
        help="Tiles per row in the atlas grid (default 16).",
    )
    tiles_view.add_argument(
        "--plane",
        choices=("sprite", "scr1", "scr2"),
        default=None,
        help="Palette plane for colorisation (default: 4-level grayscale).",
    )
    tiles_view.add_argument(
        "--palette",
        type=int,
        default=None,
        help="Palette index 0..15 within the selected --plane.",
    )
    tiles_view.add_argument(
        "--seed-from",
        default=None,
        help=(
            "Optional savestate JSON path. When set, its writable overlay "
            "is layered on the cold-start CHAR_RAM image so tiles loaded "
            "by the captured run are decoded."
        ),
    )
    tiles_view.add_argument(
        "--output",
        default=None,
        help="Output PPM path (default: ./tiles.ppm in the working dir).",
    )
    tiles_view.add_argument("--json", action="store_true", help="Emit JSON.")
    tiles_view.set_defaults(func=_cmd_tiles_view)

    screenshot = sub.add_parser(
        "screenshot",
        help=(
            "Render the current K2GE frame and write a binary P6 PPM file. "
            "M2 Phase 1 pass 1.3 (final) — full K2GE color-mode composite: "
            "backdrop + SCR1/SCR2 raster + sprite raster with PR.C 4-level "
            "composition + chain + global PO offset + flip + palette "
            "transparency + window clip with OOWC fill + NEG invert. "
            "Closes ROADMAP §8 P0 'screenshots' bullet."
        ),
    )
    screenshot.add_argument("rom", help="Path to a .ngp/.ngc ROM file.")
    screenshot.add_argument(
        "--seed-from",
        default=None,
        help=(
            "Optional savestate JSON path. When set, its writable overlay "
            "is layered on the cold-start image before rendering so the "
            "captured run's BGC, palette and control-register values feed "
            "the compose."
        ),
    )
    screenshot.add_argument(
        "--output",
        default=None,
        help="Output PPM path (default: ./screenshot.ppm in the working dir).",
    )
    screenshot.add_argument("--json", action="store_true", help="Emit JSON.")
    screenshot.set_defaults(func=_cmd_screenshot)

    frame = sub.add_parser(
        "frame",
        help=(
            "Frame-level operations: byte-diff two PPMs, manage named "
            "frame goldens (save/list/delete/check) under "
            ".ngpc_emu/goldens-frame/."
        ),
    )
    frame_sub = frame.add_subparsers(dest="frame_command", required=True)

    frame_diff = frame_sub.add_parser(
        "diff",
        help=(
            "Byte-compare two P6 PPM files. Exit 0 if identical, 1 if "
            "any pixel differs; reports counts and first-diff position."
        ),
    )
    frame_diff.add_argument("ppm_a", help="First PPM path.")
    frame_diff.add_argument("ppm_b", help="Second PPM path.")
    frame_diff.add_argument("--json", action="store_true", help="Emit JSON.")
    frame_diff.set_defaults(func=_cmd_frame_diff)

    frame_golden_save = frame_sub.add_parser(
        "golden-save",
        help=(
            "Render the current frame and store it as a named golden "
            "under .ngpc_emu/goldens-frame/. Mirrors the eventlog "
            "golden-save workflow but for visual frames."
        ),
    )
    frame_golden_save.add_argument("rom", help="Path to a .ngp/.ngc ROM file.")
    frame_golden_save.add_argument("name", help="Human-readable golden name.")
    frame_golden_save.add_argument(
        "--seed-from",
        default=None,
        help="Optional savestate JSON path to layer over cold-start.",
    )
    frame_golden_save.add_argument(
        "--label",
        default=None,
        help="Optional human-readable label stored in the manifest.",
    )
    frame_golden_save.add_argument(
        "--json", action="store_true", help="Emit JSON.",
    )
    frame_golden_save.set_defaults(func=_cmd_frame_golden_save)

    frame_golden_check = frame_sub.add_parser(
        "golden-check",
        help=(
            "Render the current frame, byte-compare against a stored "
            "golden, and exit 0 (match) or 1 (diff). The diff payload "
            "carries pixel counts + first-diff coordinate for triage."
        ),
    )
    frame_golden_check.add_argument("rom")
    frame_golden_check.add_argument("name")
    frame_golden_check.add_argument(
        "--seed-from",
        default=None,
        help="Optional savestate JSON path to layer over cold-start.",
    )
    frame_golden_check.add_argument(
        "--save-current",
        default=None,
        help=(
            "Optional path to write the current PPM (for manual "
            "inspection of a diff)."
        ),
    )
    frame_golden_check.add_argument(
        "--json", action="store_true", help="Emit JSON.",
    )
    frame_golden_check.set_defaults(func=_cmd_frame_golden_check)

    frame_golden_check_all = frame_sub.add_parser(
        "golden-check-all",
        help=(
            "Render the current frame once and byte-compare it against "
            "every stored golden for the ROM. Exit 0 only when all "
            "match; ideal for CI single-command visual regression."
        ),
    )
    frame_golden_check_all.add_argument("rom")
    frame_golden_check_all.add_argument(
        "--seed-from",
        default=None,
        help="Optional savestate JSON path to layer over cold-start.",
    )
    frame_golden_check_all.add_argument(
        "--stop-on-fail",
        action="store_true",
        help=(
            "Stop iterating goldens at the first diff/error. Useful when "
            "you only need a yes/no signal and want to bound runtime."
        ),
    )
    frame_golden_check_all.add_argument(
        "--save-current-dir",
        default=None,
        help=(
            "Optional directory to write the current rendered PPM as "
            "<rom>.current.ppm — handy for side-by-side triage when one "
            "or more goldens diverge."
        ),
    )
    frame_golden_check_all.add_argument(
        "--json", action="store_true", help="Emit JSON.",
    )
    frame_golden_check_all.set_defaults(func=_cmd_frame_golden_check_all)

    frame_golden_list = frame_sub.add_parser(
        "golden-list",
        help="List stored frame goldens for one ROM.",
    )
    frame_golden_list.add_argument("rom")
    frame_golden_list.add_argument(
        "--json", action="store_true", help="Emit JSON.",
    )
    frame_golden_list.set_defaults(func=_cmd_frame_golden_list)

    frame_golden_delete = frame_sub.add_parser(
        "golden-delete",
        help="Delete a stored frame golden (manifest + PPM).",
    )
    frame_golden_delete.add_argument("rom")
    frame_golden_delete.add_argument("name")
    frame_golden_delete.add_argument(
        "--json", action="store_true", help="Emit JSON.",
    )
    frame_golden_delete.set_defaults(func=_cmd_frame_golden_delete)

    tick_frame = sub.add_parser(
        "tick-frame",
        help=(
            "Advance the K2GE frame/scanline state model (M3 Phase 0). "
            "No CPU instructions executed — emits a savestate at the new "
            "timing position so Phase 3.1+ HW reads (RAS.V, BLNK) can "
            "consume it. Default: advance 1 scanline."
        ),
    )
    tick_frame.add_argument("rom", help="Path to a .ngp/.ngc ROM file.")
    tick_frame.add_argument(
        "--scanlines",
        type=int,
        default=None,
        help=(
            f"Number of scanlines to advance (>=0). Wraps modulo "
            f"{SCANLINES_PER_FRAME} into frame_count. Mutually exclusive "
            f"with --frames."
        ),
    )
    tick_frame.add_argument(
        "--frames",
        type=int,
        default=None,
        help=(
            "Number of full frames to advance (>=0). Snaps scanline to 0 "
            "of the n-th next frame. Mutually exclusive with --scanlines."
        ),
    )
    tick_frame.add_argument(
        "--seed-from",
        default=None,
        help=(
            "Optional savestate JSON path. The starting frame_state is "
            "taken from the loaded savestate; the CPU + overlay are "
            "copied verbatim into the emitted savestate."
        ),
    )
    tick_frame.add_argument(
        "--save-state",
        default=None,
        help=(
            "Optional output path for the new savestate (frame_state at "
            "the advanced position). When omitted, only stdout reports."
        ),
    )
    tick_frame.add_argument(
        "--json", action="store_true", help="Emit JSON.",
    )
    tick_frame.set_defaults(func=_cmd_tick_frame)

    fetch_next = sub.add_parser(
        "fetch-next",
        help="Fetch a raw byte window starting at the current bootstrap PC.",
    )
    fetch_next.add_argument("rom", help="Path to a .ngp/.ngc ROM file.")
    fetch_next.add_argument(
        "--count",
        type=int,
        default=4,
        help="Number of bytes to fetch starting at the current PC.",
    )
    fetch_next.add_argument("--json", action="store_true", help="Emit JSON.")
    fetch_next.set_defaults(func=_cmd_fetch_next)

    decode_next = sub.add_parser(
        "decode-next",
        help="Decode one instruction from the current minimal TLCS-900 subset.",
    )
    decode_next.add_argument("rom", help="Path to a .ngp/.ngc ROM file.")
    decode_next.add_argument(
        "--bios",
        help="Optional 64 KB BIOS image used to back reads in 0xFF0000..0xFFFFFF.",
    )
    decode_next.add_argument(
        "--address",
        help=(
            "Optional explicit address in decimal or 0x-prefixed hex. "
            "Defaults to the current bootstrap PC."
        ),
    )
    decode_next.add_argument("--json", action="store_true", help="Emit JSON.")
    decode_next.set_defaults(func=_cmd_decode_next)

    execute_next = sub.add_parser(
        "execute-next",
        help="Execute one instruction from the current minimal real execution subset.",
    )
    execute_next.add_argument("rom", help="Path to a .ngp/.ngc ROM file.")
    execute_next.add_argument(
        "--bios",
        help="Optional 64 KB BIOS image used to back reads in 0xFF0000..0xFFFFFF.",
    )
    execute_next.add_argument(
        "--address",
        help=(
            "Optional explicit address in decimal or 0x-prefixed hex. "
            "Defaults to the current bootstrap PC."
        ),
    )
    execute_next.add_argument(
        "--seed-xsp",
        help=(
            "Optional decimal or 0x-prefixed hex seed for XSP. This only affects the "
            "current execute-next invocation and is useful while reset-time stack state "
            "is still unknown."
        ),
    )
    execute_next.add_argument(
        "--seed-reg",
        action="append",
        default=[],
        help=(
            "Optional repeatable NAME=VALUE seed for one 32-bit register among "
            "XWA/XBC/XDE/XHL/XIX/XIY/XIZ/XSP."
        ),
    )
    execute_next.add_argument("--json", action="store_true", help="Emit JSON.")
    execute_next.set_defaults(func=_cmd_execute_next)

    step_exec = sub.add_parser(
        "step-exec",
        help=(
            "Execute exactly one real instruction, with optional savestate resume "
            "and direct savestate persistence."
        ),
    )
    step_exec.add_argument("rom", help="Path to a .ngp/.ngc ROM file.")
    step_exec.add_argument(
        "--bios",
        help="Optional 64 KB BIOS image used to back reads in 0xFF0000..0xFFFFFF.",
    )
    step_exec.add_argument(
        "--address",
        help=(
            "Optional explicit address in decimal or 0x-prefixed hex. "
            "Defaults to the current bootstrap PC or the loaded savestate PC."
        ),
    )
    step_exec.add_argument(
        "--seed-xsp",
        help="Optional decimal or 0x-prefixed hex seed for XSP.",
    )
    step_exec.add_argument(
        "--seed-reg",
        action="append",
        default=[],
        help=(
            "Optional repeatable NAME=VALUE seed for one 32-bit register among "
            "XWA/XBC/XDE/XHL/XIX/XIY/XIZ/XSP."
        ),
    )
    step_exec.add_argument(
        "--seed-from",
        help=(
            "Path to a savestate JSON file. Initial CPU state and writable memory "
            "overlay are taken from the savestate. ROM content hash is verified "
            "against the provided ROM. --seed-reg / --seed-xsp may still be used "
            "to override specific registers on top of the loaded state."
        ),
    )
    step_exec.add_argument(
        "--seed-checkpoint",
        help="Optional named checkpoint to resume instead of passing one savestate path.",
    )
    step_exec.add_argument(
        "--seed-session",
        help="Optional named session whose current checkpoint frontier is resumed.",
    )
    step_exec.add_argument(
        "--save-state",
        help=(
            "Optional path where the final CPU state and writable overlay are "
            "saved as a savestate v1 JSON file."
        ),
    )
    step_exec.add_argument(
        "--save-checkpoint",
        help="Optional named checkpoint where the final state is saved.",
    )
    step_exec.add_argument(
        "--save-session",
        help="Optional named session whose current frontier is updated.",
    )
    step_exec.add_argument(
        "--note",
        help="Optional free-form note stored if --save-state is used.",
    )
    step_exec.add_argument(
        "--map",
        help=(
            "Optional path to a t900ld .map file. When set, the result "
            "includes a 'final_symbol' block reporting the symbol that owns "
            "the final PC (nearest symbol with addr <= final PC)."
        ),
    )
    step_exec.add_argument("--json", action="store_true", help="Emit JSON.")
    step_exec.set_defaults(func=_cmd_step_exec)

    run_steps = sub.add_parser(
        "run-steps",
        help="Execute up to N instructions while carrying CPU and stack-overlay state.",
    )
    run_steps.add_argument("rom", help="Path to a .ngp/.ngc ROM file.")
    run_steps.add_argument(
        "--bios",
        help="Optional 64 KB BIOS image used to back reads in 0xFF0000..0xFFFFFF.",
    )
    run_steps.add_argument(
        "--address",
        help=(
            "Optional explicit start address in decimal or 0x-prefixed hex. "
            "Defaults to the current bootstrap PC."
        ),
    )
    run_steps.add_argument(
        "--count",
        type=int,
        default=8,
        help="Maximum number of instructions to execute in this bounded run.",
    )
    run_steps.add_argument(
        "--seed-xsp",
        help=(
            "Optional decimal or 0x-prefixed hex seed for XSP. This only affects the "
            "current run-steps invocation and is useful while reset-time stack state is "
            "still unknown."
        ),
    )
    run_steps.add_argument(
        "--seed-reg",
        action="append",
        default=[],
        help=(
            "Optional repeatable NAME=VALUE seed for one 32-bit register among "
            "XWA/XBC/XDE/XHL/XIX/XIY/XIZ/XSP."
        ),
    )
    run_steps.add_argument(
        "--seed-from",
        help=(
            "Path to a savestate JSON file. Initial CPU state and writable memory "
            "overlay are taken from the savestate. ROM content hash is verified "
            "against the provided ROM. --seed-reg / --seed-xsp may still be used "
            "to override specific registers on top of the loaded state."
        ),
    )
    run_steps.add_argument(
        "--seed-checkpoint",
        help="Optional named checkpoint to resume instead of passing one savestate path.",
    )
    run_steps.add_argument(
        "--seed-session",
        help="Optional named session whose current checkpoint frontier is resumed.",
    )
    run_steps.add_argument(
        "--save-state",
        help=(
            "Optional path where the final CPU state and writable overlay are "
            "saved as a savestate v1 JSON file."
        ),
    )
    run_steps.add_argument(
        "--save-checkpoint",
        help="Optional named checkpoint where the final state is saved.",
    )
    run_steps.add_argument(
        "--save-session",
        help="Optional named session whose current frontier is updated.",
    )
    run_steps.add_argument(
        "--note",
        help="Optional free-form note stored if --save-state is used.",
    )
    run_steps.add_argument(
        "--map",
        help=(
            "Optional path to a t900ld .map file. When set, the result "
            "includes a 'final_symbol' block reporting the symbol that owns "
            "the final PC."
        ),
    )
    run_steps.add_argument("--json", action="store_true", help="Emit JSON.")
    run_steps.set_defaults(func=_cmd_run_steps)

    trace_exec = sub.add_parser(
        "trace-exec",
        help="Show a first real execution trace using the current execute-next subset.",
    )
    trace_exec.add_argument("rom", help="Path to a .ngp/.ngc ROM file.")
    trace_exec.add_argument(
        "--address",
        help=(
            "Optional explicit start address in decimal or 0x-prefixed hex. "
            "Defaults to the current bootstrap PC."
        ),
    )
    trace_exec.add_argument(
        "--count",
        type=int,
        default=8,
        help="Maximum number of execution records to emit.",
    )
    trace_exec.add_argument(
        "--seed-xsp",
        help=(
            "Optional decimal or 0x-prefixed hex seed for XSP. This only affects the "
            "current trace-exec invocation and is useful while reset-time stack state is "
            "still unknown."
        ),
    )
    trace_exec.add_argument(
        "--seed-reg",
        action="append",
        default=[],
        help=(
            "Optional repeatable NAME=VALUE seed for one 32-bit register among "
            "XWA/XBC/XDE/XHL/XIX/XIY/XIZ/XSP."
        ),
    )
    trace_exec.add_argument(
        "--seed-from",
        help=(
            "Path to a savestate JSON file. Initial CPU state and writable memory "
            "overlay are taken from the savestate. ROM content hash is verified "
            "against the provided ROM. --seed-reg / --seed-xsp may still be used "
            "to override specific registers on top of the loaded state."
        ),
    )
    trace_exec.add_argument(
        "--seed-checkpoint",
        help="Optional named checkpoint to resume instead of passing one savestate path.",
    )
    trace_exec.add_argument(
        "--seed-session",
        help="Optional named session whose current checkpoint frontier is resumed.",
    )
    trace_exec.add_argument(
        "--save-state",
        help=(
            "Optional path where the final CPU state and writable overlay are "
            "saved as a savestate v1 JSON file."
        ),
    )
    trace_exec.add_argument(
        "--save-checkpoint",
        help="Optional named checkpoint where the final state is saved.",
    )
    trace_exec.add_argument(
        "--save-session",
        help="Optional named session whose current frontier is updated.",
    )
    trace_exec.add_argument(
        "--note",
        help="Optional free-form note stored if --save-state is used.",
    )
    trace_exec.add_argument(
        "--map",
        help=(
            "Optional path to a t900ld .map file. When set, the result "
            "includes a 'final_symbol' block reporting the symbol that owns "
            "the final PC."
        ),
    )
    trace_exec.add_argument("--json", action="store_true", help="Emit JSON.")
    trace_exec.set_defaults(func=_cmd_trace_exec)

    run_until_exec = sub.add_parser(
        "run-until-exec",
        help=(
            "Execute instructions until a target PC is reached, a blocker is hit, "
            "or a step budget is exhausted. Reports final CPU state and last instruction."
        ),
    )
    run_until_exec.add_argument("rom", help="Path to a .ngp/.ngc ROM file.")
    run_until_exec.add_argument("target", help="Target PC in decimal or 0x-prefixed hex.")
    run_until_exec.add_argument(
        "--bios",
        help=(
            "Optional path to a BIOS image. Required by the SWI1 calls that read "
            "real BIOS data (SYSFONTSET); without it those calls stop honestly."
        ),
    )
    run_until_exec.add_argument(
        "--address",
        help=(
            "Optional explicit start address in decimal or 0x-prefixed hex. "
            "Defaults to the current bootstrap PC."
        ),
    )
    run_until_exec.add_argument(
        "--max-steps",
        type=int,
        default=1_000_000,
        help="Step budget before giving up (default: 1 000 000).",
    )
    run_until_exec.add_argument(
        "--seed-xsp",
        help="Optional decimal or 0x-prefixed hex seed for XSP.",
    )
    run_until_exec.add_argument(
        "--seed-reg",
        action="append",
        default=[],
        help=(
            "Optional repeatable NAME=VALUE seed for one 32-bit register among "
            "XWA/XBC/XDE/XHL/XIX/XIY/XIZ/XSP."
        ),
    )
    run_until_exec.add_argument(
        "--seed-from",
        help=(
            "Path to a savestate JSON file. Initial CPU state and writable memory "
            "overlay are taken from the savestate. ROM content hash is verified "
            "against the provided ROM. --seed-reg / --seed-xsp may still be used "
            "to override specific registers on top of the loaded state."
        ),
    )
    run_until_exec.add_argument(
        "--seed-checkpoint",
        help="Optional named checkpoint to resume instead of passing one savestate path.",
    )
    run_until_exec.add_argument(
        "--seed-session",
        help="Optional named session whose current checkpoint frontier is resumed.",
    )
    run_until_exec.add_argument(
        "--save-state",
        help=(
            "Optional path where the final CPU state and writable overlay are "
            "saved as a savestate v1 JSON file."
        ),
    )
    run_until_exec.add_argument(
        "--save-checkpoint",
        help="Optional named checkpoint where the final state is saved.",
    )
    run_until_exec.add_argument(
        "--save-session",
        help="Optional named session whose current frontier is updated.",
    )
    run_until_exec.add_argument(
        "--note",
        help="Optional free-form note stored if --save-state is used.",
    )
    run_until_exec.add_argument(
        "--map",
        help=(
            "Optional path to a t900ld .map file. When set, the result "
            "includes a 'final_symbol' block reporting the symbol that owns "
            "the final PC (e.g. resolves a silicon-broken stop to its "
            "owning function name)."
        ),
    )
    run_until_exec.add_argument(
        "--auto-tick-addr",
        help=(
            "Address of a byte counter in writable memory (decimal or 0x-hex). "
            "When set, the counter is incremented every --auto-tick-period "
            "executed instructions. Use case: simulate a vblank/timer ISR "
            "counter so that code spinning on it (e.g. `_ngpc_vsync`) "
            "eventually exits without IRQ modeling. NOT hardware-faithful "
            "(non-reference mode per HARDWARE_COMPAT_POLICY.md section 4.3)."
        ),
    )
    run_until_exec.add_argument(
        "--auto-tick-period",
        type=int,
        default=256,
        help=(
            "Number of executed instructions between auto-tick increments "
            "(default: 256). Only meaningful with --auto-tick-addr."
        ),
    )
    run_until_exec.add_argument("--json", action="store_true", help="Emit JSON.")
    run_until_exec.set_defaults(func=_cmd_run_until_exec)

    step_preview = sub.add_parser(
        "step-preview",
        help="Show a first static step-into preview from the current decoder.",
    )
    step_preview.add_argument("rom", help="Path to a .ngp/.ngc ROM file.")
    step_preview.add_argument(
        "--address",
        help=(
            "Optional explicit start address in decimal or 0x-prefixed hex. "
            "Defaults to the current bootstrap PC."
        ),
    )
    step_preview.add_argument("--json", action="store_true", help="Emit JSON.")
    step_preview.set_defaults(func=_cmd_step_preview)

    next_preview = sub.add_parser(
        "next-preview",
        help="Show a first static step-over preview from the current decoder.",
    )
    next_preview.add_argument("rom", help="Path to a .ngp/.ngc ROM file.")
    next_preview.add_argument(
        "--address",
        help=(
            "Optional explicit start address in decimal or 0x-prefixed hex. "
            "Defaults to the current bootstrap PC."
        ),
    )
    next_preview.add_argument("--json", action="store_true", help="Emit JSON.")
    next_preview.set_defaults(func=_cmd_next_preview)

    run_until_preview = sub.add_parser(
        "run-until-preview",
        help="Show a first static run-until preview by chaining step or next previews.",
    )
    run_until_preview.add_argument("rom", help="Path to a .ngp/.ngc ROM file.")
    run_until_preview.add_argument(
        "target",
        help="Target address in decimal or 0x-prefixed hex.",
    )
    run_until_preview.add_argument(
        "--address",
        help=(
            "Optional explicit start address in decimal or 0x-prefixed hex. "
            "Defaults to the current bootstrap PC."
        ),
    )
    run_until_preview.add_argument(
        "--mode",
        choices=("over", "into"),
        default="over",
        help=(
            "Static chaining mode. 'over' assumes direct calls return normally, while "
            "'into' follows direct call targets."
        ),
    )
    run_until_preview.add_argument(
        "--max-steps",
        type=int,
        default=16,
        help="Maximum number of static preview steps to chain.",
    )
    run_until_preview.add_argument("--json", action="store_true", help="Emit JSON.")
    run_until_preview.set_defaults(func=_cmd_run_until_preview)

    trace_preview = sub.add_parser(
        "trace-preview",
        help="Show a first linear decode-only trace preview.",
    )
    trace_preview.add_argument("rom", help="Path to a .ngp/.ngc ROM file.")
    trace_preview.add_argument(
        "--address",
        help=(
            "Optional explicit start address in decimal or 0x-prefixed hex. "
            "Defaults to the current bootstrap PC."
        ),
    )
    trace_preview.add_argument(
        "--count",
        type=int,
        default=8,
        help="Maximum number of sequential records to emit.",
    )
    trace_preview.add_argument(
        "--stop-on-control-flow",
        action="store_true",
        help="Stop the preview after the first decoded control-flow instruction.",
    )
    trace_preview.add_argument("--json", action="store_true", help="Emit JSON.")
    trace_preview.set_defaults(func=_cmd_trace_preview)

    savestate = sub.add_parser(
        "savestate",
        help=(
            "Save or load emulator machine-state snapshots (v1 format, see "
            "specs/SAVESTATE.md)."
        ),
    )
    savestate_sub = savestate.add_subparsers(dest="savestate_command", required=True)

    savestate_save = savestate_sub.add_parser(
        "save",
        help=(
            "Capture the current machine state to disk as a savestate v1 JSON file."
        ),
    )
    savestate_save.add_argument("rom", help="Path to a .ngp/.ngc ROM file.")
    savestate_save.add_argument(
        "output",
        help="Path to the savestate JSON file to create (will be overwritten).",
    )
    savestate_save.add_argument(
        "--run-until",
        help=(
            "Optional target PC. When set, the CLI runs the current execution "
            "subset from the start address or bootstrap PC until this target "
            "or an honest stop, then captures the final state. Without this "
            "flag, the savestate reflects the bootstrap reset state only."
        ),
    )
    savestate_save.add_argument(
        "--address",
        help=(
            "Optional explicit start address in decimal or 0x-prefixed hex when "
            "--run-until is also set. Defaults to the current bootstrap PC."
        ),
    )
    savestate_save.add_argument(
        "--max-steps",
        type=int,
        default=1_000_000,
        help=(
            "Step budget when --run-until is used (default: 1 000 000)."
        ),
    )
    savestate_save.add_argument(
        "--seed-xsp",
        help="Optional decimal or 0x-prefixed hex seed for XSP before running.",
    )
    savestate_save.add_argument(
        "--seed-reg",
        action="append",
        default=[],
        help=(
            "Optional repeatable NAME=VALUE seed for one 32-bit register among "
            "XWA/XBC/XDE/XHL/XIX/XIY/XIZ/XSP."
        ),
    )
    savestate_save.add_argument(
        "--auto-tick-addr",
        help=(
            "Address of a writable byte counter (decimal or 0x-hex) incremented "
            "every --auto-tick-period executed instructions during --run-until. "
            "Diagnostic-only non-reference mode for escaping counter-wait loops "
            "such as `_ngpc_vsync` without IRQ modeling."
        ),
    )
    savestate_save.add_argument(
        "--auto-tick-period",
        type=int,
        default=256,
        help=(
            "Number of executed instructions between auto-tick increments "
            "(default: 256). Only meaningful with --auto-tick-addr and "
            "--run-until."
        ),
    )
    savestate_save.add_argument(
        "--note",
        help="Optional free-form note stored in the savestate.",
    )
    savestate_save.add_argument("--json", action="store_true", help="Emit JSON.")
    savestate_save.set_defaults(func=_cmd_savestate_save)

    savestate_load = savestate_sub.add_parser(
        "load",
        help=(
            "Load and inspect a savestate v1 JSON file. With --rom, ROM "
            "content hash is verified; without --rom, hash is not enforced."
        ),
    )
    savestate_load.add_argument(
        "input",
        help="Path to a savestate JSON file.",
    )
    savestate_load.add_argument(
        "--rom",
        help=(
            "Optional path to the ROM file. When provided, the loader rejects "
            "the savestate if the ROM sha256 does not match."
        ),
    )
    savestate_load.add_argument("--json", action="store_true", help="Emit JSON.")
    savestate_load.set_defaults(func=_cmd_savestate_load)

    checkpoint = sub.add_parser(
        "checkpoint",
        help="Manage named checkpoints built on top of savestate v1 files.",
    )
    checkpoint_sub = checkpoint.add_subparsers(dest="checkpoint_command", required=True)

    checkpoint_save = checkpoint_sub.add_parser(
        "save",
        help="Capture one named checkpoint for a ROM.",
    )
    checkpoint_save.add_argument("rom", help="Path to a .ngp/.ngc ROM file.")
    checkpoint_save.add_argument("name", help="Human checkpoint name.")
    checkpoint_save.add_argument(
        "--run-until",
        help=(
            "Optional target PC. When set, the checkpoint is captured after a real "
            "run-until-exec instead of directly from the current state."
        ),
    )
    checkpoint_save.add_argument(
        "--address",
        help=(
            "Optional explicit start address in decimal or 0x-prefixed hex. "
            "Defaults to the bootstrap PC or the loaded savestate PC."
        ),
    )
    checkpoint_save.add_argument(
        "--max-steps",
        type=int,
        default=1_000_000,
        help="Step budget when --run-until is used (default: 1 000 000).",
    )
    checkpoint_save.add_argument(
        "--seed-xsp",
        help="Optional decimal or 0x-prefixed hex seed for XSP.",
    )
    checkpoint_save.add_argument(
        "--seed-reg",
        action="append",
        default=[],
        help=(
            "Optional repeatable NAME=VALUE seed for one 32-bit register among "
            "XWA/XBC/XDE/XHL/XIX/XIY/XIZ/XSP."
        ),
    )
    checkpoint_save.add_argument(
        "--auto-tick-addr",
        help=(
            "Address of a writable byte counter (decimal or 0x-hex) incremented "
            "every --auto-tick-period executed instructions during --run-until. "
            "Diagnostic-only non-reference mode for escaping counter-wait loops "
            "such as `_ngpc_vsync` without IRQ modeling."
        ),
    )
    checkpoint_save.add_argument(
        "--auto-tick-period",
        type=int,
        default=256,
        help=(
            "Number of executed instructions between auto-tick increments "
            "(default: 256). Only meaningful with --auto-tick-addr and "
            "--run-until."
        ),
    )
    checkpoint_save.add_argument(
        "--seed-from",
        help="Optional savestate file used as the checkpoint source state.",
    )
    checkpoint_save.add_argument(
        "--seed-checkpoint",
        help="Optional existing named checkpoint used as the checkpoint source state.",
    )
    checkpoint_save.add_argument(
        "--seed-session",
        help="Optional named session used as the checkpoint source state.",
    )
    checkpoint_save.add_argument(
        "--note",
        help="Optional free-form note stored in the checkpoint savestate.",
    )
    checkpoint_save.add_argument("--json", action="store_true", help="Emit JSON.")
    checkpoint_save.set_defaults(func=_cmd_checkpoint_save)

    session = sub.add_parser(
        "session",
        help="Manage named session frontiers built on top of managed checkpoints.",
    )
    session_sub = session.add_subparsers(dest="session_command", required=True)

    session_save = session_sub.add_parser(
        "save",
        help="Capture or update one named session frontier for a ROM.",
    )
    session_save.add_argument("rom", help="Path to a .ngp/.ngc ROM file.")
    session_save.add_argument("name", help="Human session name.")
    session_save.add_argument(
        "--run-until",
        help=(
            "Optional target PC. When set, the session frontier is captured after "
            "a real run-until-exec instead of directly from the current state."
        ),
    )
    session_save.add_argument(
        "--address",
        help=(
            "Optional explicit start address in decimal or 0x-prefixed hex. "
            "Defaults to the bootstrap PC or the loaded state PC."
        ),
    )
    session_save.add_argument(
        "--max-steps",
        type=int,
        default=1_000_000,
        help="Step budget when --run-until is used (default: 1 000 000).",
    )
    session_save.add_argument(
        "--seed-xsp",
        help="Optional decimal or 0x-prefixed hex seed for XSP.",
    )
    session_save.add_argument(
        "--seed-reg",
        action="append",
        default=[],
        help=(
            "Optional repeatable NAME=VALUE seed for one 32-bit register among "
            "XWA/XBC/XDE/XHL/XIX/XIY/XIZ/XSP."
        ),
    )
    session_save.add_argument(
        "--auto-tick-addr",
        help=(
            "Address of a writable byte counter (decimal or 0x-hex) incremented "
            "every --auto-tick-period executed instructions during --run-until. "
            "Diagnostic-only non-reference mode for escaping counter-wait loops "
            "such as `_ngpc_vsync` without IRQ modeling."
        ),
    )
    session_save.add_argument(
        "--auto-tick-period",
        type=int,
        default=256,
        help=(
            "Number of executed instructions between auto-tick increments "
            "(default: 256). Only meaningful with --auto-tick-addr and "
            "--run-until."
        ),
    )
    session_save.add_argument(
        "--seed-from",
        help="Optional savestate file used as the session source state.",
    )
    session_save.add_argument(
        "--seed-checkpoint",
        help="Optional existing named checkpoint used as the session source state.",
    )
    session_save.add_argument(
        "--seed-session",
        help="Optional existing named session used as the session source state.",
    )
    session_save.add_argument(
        "--note",
        help="Optional free-form note stored on the session metadata and state.",
    )
    session_save.add_argument("--json", action="store_true", help="Emit JSON.")
    session_save.set_defaults(func=_cmd_session_save)

    session_load = session_sub.add_parser(
        "load",
        help="Load and inspect one named session.",
    )
    session_load.add_argument("rom", help="Path to a .ngp/.ngc ROM file.")
    session_load.add_argument("name", help="Human session name.")
    session_load.add_argument("--json", action="store_true", help="Emit JSON.")
    session_load.set_defaults(func=_cmd_session_load)

    session_list = session_sub.add_parser(
        "list",
        help="List named sessions available for one ROM directory.",
    )
    session_list.add_argument("rom", help="Path to a .ngp/.ngc ROM file.")
    session_list.add_argument("--json", action="store_true", help="Emit JSON.")
    session_list.set_defaults(func=_cmd_session_list)

    session_delete = session_sub.add_parser(
        "delete",
        help="Delete one named session and its managed current checkpoint.",
    )
    session_delete.add_argument("rom", help="Path to a .ngp/.ngc ROM file.")
    session_delete.add_argument("name", help="Human session name.")
    session_delete.add_argument("--json", action="store_true", help="Emit JSON.")
    session_delete.set_defaults(func=_cmd_session_delete)

    session_snapshot = session_sub.add_parser(
        "snapshot",
        help="Manage lightweight named snapshots captured from one session frontier.",
    )
    session_snapshot_sub = session_snapshot.add_subparsers(
        dest="session_snapshot_command",
        required=True,
    )

    session_snapshot_save = session_snapshot_sub.add_parser(
        "save",
        help="Capture one named snapshot from the current session frontier.",
    )
    session_snapshot_save.add_argument("rom", help="Path to a .ngp/.ngc ROM file.")
    session_snapshot_save.add_argument("name", help="Human session name.")
    session_snapshot_save.add_argument("snapshot", help="Human snapshot name.")
    session_snapshot_save.add_argument("--json", action="store_true", help="Emit JSON.")
    session_snapshot_save.set_defaults(func=_cmd_session_snapshot_save)

    session_snapshot_list = session_snapshot_sub.add_parser(
        "list",
        help="List named snapshots currently captured for one session.",
    )
    session_snapshot_list.add_argument("rom", help="Path to a .ngp/.ngc ROM file.")
    session_snapshot_list.add_argument("name", help="Human session name.")
    session_snapshot_list.add_argument("--json", action="store_true", help="Emit JSON.")
    session_snapshot_list.set_defaults(func=_cmd_session_snapshot_list)

    session_snapshot_load = session_snapshot_sub.add_parser(
        "load",
        help="Load and inspect one named session snapshot.",
    )
    session_snapshot_load.add_argument("rom", help="Path to a .ngp/.ngc ROM file.")
    session_snapshot_load.add_argument("name", help="Human session name.")
    session_snapshot_load.add_argument("snapshot", help="Human snapshot name.")
    session_snapshot_load.add_argument("--json", action="store_true", help="Emit JSON.")
    session_snapshot_load.set_defaults(func=_cmd_session_snapshot_load)

    session_snapshot_restore = session_snapshot_sub.add_parser(
        "restore",
        help="Restore one named snapshot into the current session frontier.",
    )
    session_snapshot_restore.add_argument("rom", help="Path to a .ngp/.ngc ROM file.")
    session_snapshot_restore.add_argument("name", help="Human session name.")
    session_snapshot_restore.add_argument("snapshot", help="Human snapshot name.")
    session_snapshot_restore.add_argument("--json", action="store_true", help="Emit JSON.")
    session_snapshot_restore.set_defaults(func=_cmd_session_snapshot_restore)

    session_snapshot_delete = session_snapshot_sub.add_parser(
        "delete",
        help="Delete one named session snapshot.",
    )
    session_snapshot_delete.add_argument("rom", help="Path to a .ngp/.ngc ROM file.")
    session_snapshot_delete.add_argument("name", help="Human session name.")
    session_snapshot_delete.add_argument("snapshot", help="Human snapshot name.")
    session_snapshot_delete.add_argument("--json", action="store_true", help="Emit JSON.")
    session_snapshot_delete.set_defaults(func=_cmd_session_snapshot_delete)

    checkpoint_load = checkpoint_sub.add_parser(
        "load",
        help="Load and inspect one named checkpoint.",
    )
    checkpoint_load.add_argument("rom", help="Path to a .ngp/.ngc ROM file.")
    checkpoint_load.add_argument("name", help="Human checkpoint name.")
    checkpoint_load.add_argument("--json", action="store_true", help="Emit JSON.")
    checkpoint_load.set_defaults(func=_cmd_checkpoint_load)

    checkpoint_list = checkpoint_sub.add_parser(
        "list",
        help="List named checkpoints available for one ROM directory.",
    )
    checkpoint_list.add_argument("rom", help="Path to a .ngp/.ngc ROM file.")
    checkpoint_list.add_argument("--json", action="store_true", help="Emit JSON.")
    checkpoint_list.set_defaults(func=_cmd_checkpoint_list)

    checkpoint_delete = checkpoint_sub.add_parser(
        "delete",
        help="Delete one named checkpoint.",
    )
    checkpoint_delete.add_argument("rom", help="Path to a .ngp/.ngc ROM file.")
    checkpoint_delete.add_argument("name", help="Human checkpoint name.")
    checkpoint_delete.add_argument("--json", action="store_true", help="Emit JSON.")
    checkpoint_delete.set_defaults(func=_cmd_checkpoint_delete)

    eventlog = sub.add_parser(
        "eventlog",
        help=(
            "Capture, inspect, and diff stable event-log v1 JSON files "
            "(see specs/EVENT_LOG.md)."
        ),
    )
    eventlog_sub = eventlog.add_subparsers(dest="eventlog_command", required=True)

    eventlog_capture = eventlog_sub.add_parser(
        "capture",
        help=(
            "Capture one event-log v1 JSON file from the current execution subset."
        ),
    )
    eventlog_capture.add_argument("rom", help="Path to a .ngp/.ngc ROM file.")
    eventlog_capture.add_argument(
        "output",
        help="Path to the event-log JSON file to create (will be overwritten).",
    )
    eventlog_capture.add_argument(
        "--run-until",
        help=(
            "Optional target PC. When set, capture continues until this target, "
            "an honest stop, or --max-steps is reached."
        ),
    )
    eventlog_capture.add_argument(
        "--count",
        type=int,
        default=8,
        help=(
            "Step budget when --run-until is not used (default: 8)."
        ),
    )
    eventlog_capture.add_argument(
        "--max-steps",
        type=int,
        default=1_000_000,
        help=(
            "Step budget when --run-until is used (default: 1 000 000)."
        ),
    )
    eventlog_capture.add_argument(
        "--address",
        help=(
            "Optional explicit start address in decimal or 0x-prefixed hex. "
            "Defaults to the current bootstrap PC."
        ),
    )
    eventlog_capture.add_argument(
        "--seed-xsp",
        help="Optional decimal or 0x-prefixed hex seed for XSP before capture.",
    )
    eventlog_capture.add_argument(
        "--seed-reg",
        action="append",
        default=[],
        help=(
            "Optional repeatable NAME=VALUE seed for one 32-bit register among "
            "XWA/XBC/XDE/XHL/XIX/XIY/XIZ/XSP."
        ),
    )
    eventlog_capture.add_argument(
        "--seed-from",
        help=(
            "Path to a savestate JSON file. Initial CPU state and writable memory "
            "overlay are restored from it before capture; ROM hash is verified "
            "against the provided ROM."
        ),
    )
    eventlog_capture.add_argument(
        "--seed-checkpoint",
        help="Optional named checkpoint to resume instead of passing one savestate path.",
    )
    eventlog_capture.add_argument(
        "--seed-session",
        help="Optional named session whose current checkpoint frontier is resumed.",
    )
    eventlog_capture.add_argument(
        "--auto-tick-addr",
        help=(
            "Address of a writable byte counter (decimal or 0x-hex). When set, "
            "the counter is incremented every --auto-tick-period executed "
            "instructions so event-log capture can escape counter-wait loops "
            "such as `_ngpc_vsync` without IRQ modeling. NOT hardware-faithful "
            "(non-reference mode per HARDWARE_COMPAT_POLICY.md section 4.3)."
        ),
    )
    eventlog_capture.add_argument(
        "--auto-tick-period",
        type=int,
        default=256,
        help=(
            "Number of executed instructions between auto-tick increments "
            "(default: 256). Only meaningful with --auto-tick-addr."
        ),
    )
    eventlog_capture.add_argument(
        "--note",
        help="Optional free-form note stored in the event log.",
    )
    eventlog_capture.add_argument(
        "--map",
        help=(
            "Optional path to a t900ld .map file. When set, the CLI summary "
            "result includes a 'final_symbol' block. The on-disk event-log "
            "JSON file itself is NOT modified — symbol awareness is a CLI "
            "diagnostic on top of the captured log."
        ),
    )
    eventlog_capture.add_argument("--json", action="store_true", help="Emit JSON.")
    eventlog_capture.set_defaults(func=_cmd_eventlog_capture)

    eventlog_inspect = eventlog_sub.add_parser(
        "inspect",
        help=(
            "Load and inspect an event-log v1 JSON file. With --rom, ROM content "
            "hash is verified; without --rom, hash is not enforced."
        ),
    )
    eventlog_inspect.add_argument("input", help="Path to an event-log JSON file.")
    eventlog_inspect.add_argument(
        "--rom",
        help=(
            "Optional path to the ROM file. When provided, the loader rejects "
            "the event log if the ROM sha256 does not match."
        ),
    )
    eventlog_inspect.add_argument(
        "--limit",
        type=int,
        default=0,
        help="Optional number of events to print after the summary (default: 0).",
    )
    eventlog_inspect.add_argument("--json", action="store_true", help="Emit JSON.")
    eventlog_inspect.set_defaults(func=_cmd_eventlog_inspect)

    eventlog_profile = eventlog_sub.add_parser(
        "profile",
        help=(
            "Bucketize one event-log v1 JSON file by owning symbol "
            "(requires --map). Reports per-symbol event totals, "
            "executed-vs-halted counts, and first/last PC observed inside "
            "each symbol. First dynamic-profile primitive."
        ),
    )
    eventlog_profile.add_argument(
        "input",
        help="Path to an event-log v1 JSON file.",
    )
    eventlog_profile.add_argument(
        "--map",
        required=True,
        help="Path to the t900ld .map file matching the build that produced this log.",
    )
    eventlog_profile.add_argument(
        "--rom",
        help=(
            "Optional path to the ROM that produced this log. When set, "
            "the ROM file's sha256 is verified against the value stored in "
            "the event log; mismatch raises an error."
        ),
    )
    eventlog_profile.add_argument(
        "--top",
        type=int,
        default=20,
        help=(
            "How many top buckets to display (default: 20). Pass 0 to "
            "show all distinct symbols hit."
        ),
    )
    eventlog_profile.add_argument(
        "--json",
        action="store_true",
        help="Emit JSON.",
    )
    eventlog_profile.set_defaults(func=_cmd_eventlog_profile)

    eventlog_diff = eventlog_sub.add_parser(
        "diff",
        help="Show the first event-level divergence between two event-log v1 files.",
    )
    eventlog_diff.add_argument("left", help="Path to the left event-log JSON file.")
    eventlog_diff.add_argument("right", help="Path to the right event-log JSON file.")
    eventlog_diff.add_argument("--json", action="store_true", help="Emit JSON.")
    eventlog_diff.set_defaults(func=_cmd_eventlog_diff)

    eventlog_golden_save = eventlog_sub.add_parser(
        "golden-save",
        help=(
            "Capture a fresh event-log run and store it under one named golden "
            "inside the ROM-local registry."
        ),
    )
    eventlog_golden_save.add_argument("rom", help="Path to a .ngp/.ngc ROM file.")
    eventlog_golden_save.add_argument("name", help="Human golden name.")
    eventlog_golden_save.add_argument(
        "--run-until",
        help=(
            "Optional target PC. When set, capture continues until this target, "
            "an honest stop, or --max-steps is reached."
        ),
    )
    eventlog_golden_save.add_argument(
        "--count",
        type=int,
        default=8,
        help="Step budget when --run-until is not used (default: 8).",
    )
    eventlog_golden_save.add_argument(
        "--max-steps",
        type=int,
        default=1_000_000,
        help="Step budget when --run-until is used (default: 1 000 000).",
    )
    eventlog_golden_save.add_argument(
        "--address",
        help=(
            "Optional explicit start address in decimal or 0x-prefixed hex. "
            "Defaults to the current bootstrap PC."
        ),
    )
    eventlog_golden_save.add_argument(
        "--seed-xsp",
        help="Optional decimal or 0x-prefixed hex seed for XSP before capture.",
    )
    eventlog_golden_save.add_argument(
        "--seed-reg",
        action="append",
        default=[],
        help=(
            "Optional repeatable NAME=VALUE seed for one 32-bit register among "
            "XWA/XBC/XDE/XHL/XIX/XIY/XIZ/XSP."
        ),
    )
    eventlog_golden_save.add_argument(
        "--seed-from",
        help=(
            "Path to a savestate JSON file. Initial CPU state and writable memory "
            "overlay are restored from it before capture; ROM hash is verified "
            "against the provided ROM."
        ),
    )
    eventlog_golden_save.add_argument(
        "--seed-checkpoint",
        help="Optional named checkpoint to resume instead of passing one savestate path.",
    )
    eventlog_golden_save.add_argument(
        "--seed-session",
        help="Optional named session whose current checkpoint frontier is resumed.",
    )
    eventlog_golden_save.add_argument(
        "--auto-tick-addr",
        help=(
            "Address of a writable byte counter (decimal or 0x-hex). When set, "
            "the counter is incremented every --auto-tick-period executed "
            "instructions so capture can escape counter-wait loops such as "
            "`_ngpc_vsync` without IRQ modeling. NOT hardware-faithful "
            "(non-reference mode per HARDWARE_COMPAT_POLICY.md section 4.3)."
        ),
    )
    eventlog_golden_save.add_argument(
        "--auto-tick-period",
        type=int,
        default=256,
        help=(
            "Number of executed instructions between auto-tick increments "
            "(default: 256). Only meaningful with --auto-tick-addr."
        ),
    )
    eventlog_golden_save.add_argument(
        "--note",
        help="Optional free-form note stored in the named golden event log.",
    )
    eventlog_golden_save.add_argument(
        "--map",
        help=(
            "Optional path to a t900ld .map file. When set, the save summary "
            "resolves the final PC to a symbol when possible."
        ),
    )
    eventlog_golden_save.add_argument("--json", action="store_true", help="Emit JSON.")
    eventlog_golden_save.set_defaults(func=_cmd_eventlog_golden_save)

    eventlog_golden_load = eventlog_sub.add_parser(
        "golden-load",
        help="Load and inspect one named golden event log.",
    )
    eventlog_golden_load.add_argument("rom", help="Path to a .ngp/.ngc ROM file.")
    eventlog_golden_load.add_argument("name", help="Human golden name.")
    eventlog_golden_load.add_argument("--json", action="store_true", help="Emit JSON.")
    eventlog_golden_load.set_defaults(func=_cmd_eventlog_golden_load)

    eventlog_golden_list = eventlog_sub.add_parser(
        "golden-list",
        help="List named golden event logs available for one ROM directory.",
    )
    eventlog_golden_list.add_argument("rom", help="Path to a .ngp/.ngc ROM file.")
    eventlog_golden_list.add_argument("--json", action="store_true", help="Emit JSON.")
    eventlog_golden_list.set_defaults(func=_cmd_eventlog_golden_list)

    eventlog_golden_delete = eventlog_sub.add_parser(
        "golden-delete",
        help="Delete one named golden event log.",
    )
    eventlog_golden_delete.add_argument("rom", help="Path to a .ngp/.ngc ROM file.")
    eventlog_golden_delete.add_argument("name", help="Human golden name.")
    eventlog_golden_delete.add_argument("--json", action="store_true", help="Emit JSON.")
    eventlog_golden_delete.set_defaults(func=_cmd_eventlog_golden_delete)

    eventlog_golden_check = eventlog_sub.add_parser(
        "golden-check",
        help=(
            "Capture a fresh event-log run and compare it immediately against "
            "one named golden from the ROM-local registry."
        ),
    )
    eventlog_golden_check.add_argument("rom", help="Path to a .ngp/.ngc ROM file.")
    eventlog_golden_check.add_argument("name", help="Human golden name.")
    eventlog_golden_check.add_argument(
        "--save-current",
        help=(
            "Optional path where the freshly captured current event log is "
            "saved before diffing. Useful for mismatch inspection."
        ),
    )
    eventlog_golden_check.add_argument(
        "--run-until",
        help=(
            "Optional target PC. When set, capture continues until this target, "
            "an honest stop, or --max-steps is reached."
        ),
    )
    eventlog_golden_check.add_argument(
        "--count",
        type=int,
        default=8,
        help="Step budget when --run-until is not used (default: 8).",
    )
    eventlog_golden_check.add_argument(
        "--max-steps",
        type=int,
        default=1_000_000,
        help="Step budget when --run-until is used (default: 1 000 000).",
    )
    eventlog_golden_check.add_argument(
        "--address",
        help=(
            "Optional explicit start address in decimal or 0x-prefixed hex. "
            "Defaults to the current bootstrap PC."
        ),
    )
    eventlog_golden_check.add_argument(
        "--seed-xsp",
        help="Optional decimal or 0x-prefixed hex seed for XSP before capture.",
    )
    eventlog_golden_check.add_argument(
        "--seed-reg",
        action="append",
        default=[],
        help=(
            "Optional repeatable NAME=VALUE seed for one 32-bit register among "
            "XWA/XBC/XDE/XHL/XIX/XIY/XIZ/XSP."
        ),
    )
    eventlog_golden_check.add_argument(
        "--seed-from",
        help=(
            "Path to a savestate JSON file. Initial CPU state and writable memory "
            "overlay are restored from it before capture; ROM hash is verified "
            "against the provided ROM."
        ),
    )
    eventlog_golden_check.add_argument(
        "--seed-checkpoint",
        help="Optional named checkpoint to resume instead of passing one savestate path.",
    )
    eventlog_golden_check.add_argument(
        "--seed-session",
        help="Optional named session whose current checkpoint frontier is resumed.",
    )
    eventlog_golden_check.add_argument(
        "--auto-tick-addr",
        help=(
            "Address of a writable byte counter (decimal or 0x-hex). When set, "
            "the counter is incremented every --auto-tick-period executed "
            "instructions so the current run can escape counter-wait loops "
            "such as `_ngpc_vsync` without IRQ modeling. NOT hardware-faithful "
            "(non-reference mode per HARDWARE_COMPAT_POLICY.md section 4.3)."
        ),
    )
    eventlog_golden_check.add_argument(
        "--auto-tick-period",
        type=int,
        default=256,
        help=(
            "Number of executed instructions between auto-tick increments "
            "(default: 256). Only meaningful with --auto-tick-addr."
        ),
    )
    eventlog_golden_check.add_argument(
        "--note",
        help="Optional free-form note stored in the freshly captured current event log.",
    )
    eventlog_golden_check.add_argument(
        "--map",
        help=(
            "Optional path to a t900ld .map file. When set, the current capture "
            "summary resolves the final PC to a symbol when possible."
        ),
    )
    eventlog_golden_check.add_argument("--json", action="store_true", help="Emit JSON.")
    eventlog_golden_check.set_defaults(func=_cmd_eventlog_golden_check)

    eventlog_golden_check_all = eventlog_sub.add_parser(
        "golden-check-all",
        help=(
            "Capture one event-log run and diff it against every stored "
            "golden in the ROM-local registry. Exit 0 only when all match. "
            "Mirrors the `frame golden-check-all` workflow for trace "
            "regressions. Assumes the golden set is homogeneous — every "
            "golden was captured with compatible parameters (same "
            "--count / --run-until / --seed-from)."
        ),
    )
    eventlog_golden_check_all.add_argument(
        "rom", help="Path to a .ngp/.ngc ROM file.",
    )
    eventlog_golden_check_all.add_argument(
        "--save-current",
        help=(
            "Optional path where the freshly captured current event log is "
            "saved before diffing. Useful for mismatch inspection."
        ),
    )
    eventlog_golden_check_all.add_argument(
        "--run-until",
        help=(
            "Optional target PC. When set, capture continues until this "
            "target, an honest stop, or --max-steps is reached."
        ),
    )
    eventlog_golden_check_all.add_argument(
        "--count",
        type=int,
        default=8,
        help="Step budget when --run-until is not used (default: 8).",
    )
    eventlog_golden_check_all.add_argument(
        "--max-steps",
        type=int,
        default=1_000_000,
        help="Step budget when --run-until is used (default: 1 000 000).",
    )
    eventlog_golden_check_all.add_argument(
        "--address",
        help=(
            "Optional explicit start address in decimal or 0x-prefixed hex. "
            "Defaults to the current bootstrap PC."
        ),
    )
    eventlog_golden_check_all.add_argument(
        "--seed-xsp",
        help="Optional decimal or 0x-prefixed hex seed for XSP before capture.",
    )
    eventlog_golden_check_all.add_argument(
        "--seed-reg",
        action="append",
        default=[],
        help=(
            "Optional repeatable NAME=VALUE seed for one 32-bit register among "
            "XWA/XBC/XDE/XHL/XIX/XIY/XIZ/XSP."
        ),
    )
    eventlog_golden_check_all.add_argument(
        "--seed-from",
        help=(
            "Path to a savestate JSON file. Initial CPU state and writable "
            "memory overlay are restored from it before capture."
        ),
    )
    eventlog_golden_check_all.add_argument(
        "--seed-checkpoint",
        help="Optional named checkpoint to resume.",
    )
    eventlog_golden_check_all.add_argument(
        "--seed-session",
        help="Optional named session whose current checkpoint frontier is resumed.",
    )
    eventlog_golden_check_all.add_argument(
        "--auto-tick-addr",
        help=(
            "Address of a writable byte counter (decimal or 0x-hex). When "
            "set, the counter is incremented every --auto-tick-period "
            "executed instructions. NOT hardware-faithful."
        ),
    )
    eventlog_golden_check_all.add_argument(
        "--auto-tick-period",
        type=int,
        default=256,
        help="Auto-tick increment period in executed instructions (default: 256).",
    )
    eventlog_golden_check_all.add_argument(
        "--note",
        help="Optional free-form note stored in the freshly captured current event log.",
    )
    eventlog_golden_check_all.add_argument(
        "--map",
        help=(
            "Optional path to a t900ld .map file for final-PC symbol "
            "resolution in the current-capture summary."
        ),
    )
    eventlog_golden_check_all.add_argument(
        "--stop-on-fail",
        action="store_true",
        help=(
            "Stop iterating goldens at the first mismatch. Useful when "
            "you only need a yes/no signal and want to bound runtime."
        ),
    )
    eventlog_golden_check_all.add_argument(
        "--json", action="store_true", help="Emit JSON.",
    )
    eventlog_golden_check_all.set_defaults(func=_cmd_eventlog_golden_check_all)

    eventlog_check = eventlog_sub.add_parser(
        "check",
        help=(
            "Capture a fresh event-log run and compare it immediately against "
            "one golden event-log JSON file. Exit code 0 = identical, 1 = "
            "divergence (CI-friendly first golden-trace wrapper)."
        ),
    )
    eventlog_check.add_argument("rom", help="Path to a .ngp/.ngc ROM file.")
    eventlog_check.add_argument(
        "golden",
        help="Path to the golden event-log JSON file to compare against.",
    )
    eventlog_check.add_argument(
        "--save-current",
        help=(
            "Optional path where the freshly captured current event log is "
            "saved before diffing. Useful for mismatch inspection."
        ),
    )
    eventlog_check.add_argument(
        "--run-until",
        help=(
            "Optional target PC. When set, capture continues until this target, "
            "an honest stop, or --max-steps is reached."
        ),
    )
    eventlog_check.add_argument(
        "--count",
        type=int,
        default=8,
        help="Step budget when --run-until is not used (default: 8).",
    )
    eventlog_check.add_argument(
        "--max-steps",
        type=int,
        default=1_000_000,
        help="Step budget when --run-until is used (default: 1 000 000).",
    )
    eventlog_check.add_argument(
        "--address",
        help=(
            "Optional explicit start address in decimal or 0x-prefixed hex. "
            "Defaults to the current bootstrap PC."
        ),
    )
    eventlog_check.add_argument(
        "--seed-xsp",
        help="Optional decimal or 0x-prefixed hex seed for XSP before capture.",
    )
    eventlog_check.add_argument(
        "--seed-reg",
        action="append",
        default=[],
        help=(
            "Optional repeatable NAME=VALUE seed for one 32-bit register among "
            "XWA/XBC/XDE/XHL/XIX/XIY/XIZ/XSP."
        ),
    )
    eventlog_check.add_argument(
        "--seed-from",
        help=(
            "Path to a savestate JSON file. Initial CPU state and writable memory "
            "overlay are restored from it before capture; ROM hash is verified "
            "against the provided ROM."
        ),
    )
    eventlog_check.add_argument(
        "--seed-checkpoint",
        help="Optional named checkpoint to resume instead of passing one savestate path.",
    )
    eventlog_check.add_argument(
        "--seed-session",
        help="Optional named session whose current checkpoint frontier is resumed.",
    )
    eventlog_check.add_argument(
        "--auto-tick-addr",
        help=(
            "Address of a writable byte counter (decimal or 0x-hex). When set, "
            "the counter is incremented every --auto-tick-period executed "
            "instructions so the current run can escape counter-wait loops "
            "such as `_ngpc_vsync` without IRQ modeling. NOT hardware-faithful "
            "(non-reference mode per HARDWARE_COMPAT_POLICY.md section 4.3)."
        ),
    )
    eventlog_check.add_argument(
        "--auto-tick-period",
        type=int,
        default=256,
        help=(
            "Number of executed instructions between auto-tick increments "
            "(default: 256). Only meaningful with --auto-tick-addr."
        ),
    )
    eventlog_check.add_argument(
        "--note",
        help="Optional free-form note stored in the freshly captured current event log.",
    )
    eventlog_check.add_argument(
        "--map",
        help=(
            "Optional path to a t900ld .map file. When set, the current capture "
            "summary resolves the final PC to a symbol when possible."
        ),
    )
    eventlog_check.add_argument("--json", action="store_true", help="Emit JSON.")
    eventlog_check.set_defaults(func=_cmd_eventlog_check)

    map_cmd = sub.add_parser(
        "map",
        help=(
            "Inspect a t900ld .map file: list sections, look up symbols by "
            "name, or resolve a PC back to the owning function (debugger "
            "symbol awareness, first layer)."
        ),
    )
    map_sub = map_cmd.add_subparsers(dest="map_command", required=True)

    map_info = map_sub.add_parser(
        "info",
        help="Show total symbol count and per-section breakdown.",
    )
    map_info.add_argument("map", help="Path to a t900ld .map file.")
    map_info.add_argument("--json", action="store_true", help="Emit JSON.")
    map_info.set_defaults(func=_cmd_map_info)

    map_lookup_name = map_sub.add_parser(
        "lookup-name",
        help="Resolve a symbol name to its address (exact match).",
    )
    map_lookup_name.add_argument("map", help="Path to a t900ld .map file.")
    map_lookup_name.add_argument("name", help="Exact symbol name (e.g. _shmup_update).")
    map_lookup_name.add_argument("--json", action="store_true", help="Emit JSON.")
    map_lookup_name.set_defaults(func=_cmd_map_lookup_name)

    map_lookup_addr = map_sub.add_parser(
        "lookup-addr",
        help=(
            "Resolve a PC back to the symbol that owns it (nearest symbol "
            "with address <= the requested PC). Useful for naming an "
            "emulator stop frontier or a trace record."
        ),
    )
    map_lookup_addr.add_argument("map", help="Path to a t900ld .map file.")
    map_lookup_addr.add_argument(
        "address",
        help="PC value in decimal or 0x-prefixed hex.",
    )
    map_lookup_addr.add_argument("--json", action="store_true", help="Emit JSON.")
    map_lookup_addr.set_defaults(func=_cmd_map_lookup_addr)

    engine_bridge = sub.add_parser(
        "engine-bridge",
        help=(
            "Execute one engine-facing bridge request JSON and emit a structured "
            "JSON response on stdout."
        ),
    )
    engine_bridge.add_argument(
        "request",
        help="Path to one engine-bridge request JSON file.",
    )
    engine_bridge.set_defaults(func=_cmd_engine_bridge)

    watchpoint = sub.add_parser(
        "watchpoint",
        help=(
            "Per-ROM memory watchpoint registry and event-log match "
            "(see specs/WATCHPOINTS.md). v1 is write-only."
        ),
    )
    watchpoint_sub = watchpoint.add_subparsers(
        dest="watchpoint_command", required=True
    )

    watchpoint_add = watchpoint_sub.add_parser(
        "add",
        help="Add one watchpoint (write / read / access) to the per-ROM registry.",
    )
    watchpoint_add.add_argument("rom", help="Path to a .ngp/.ngc ROM file.")
    watchpoint_add.add_argument(
        "address",
        help="Address to watch (decimal or 0x-prefixed hex).",
    )
    watchpoint_add.add_argument(
        "--kind",
        choices=WATCHPOINT_KINDS,
        default="write",
        help=(
            "Access kind to watch: 'write' (default) matches memory_writes, "
            "'read' matches memory_reads, 'access' matches both."
        ),
    )
    watchpoint_add.add_argument(
        "--size",
        type=int,
        default=1,
        help="Byte range starting at <address> (default: 1).",
    )
    watchpoint_add.add_argument(
        "--label",
        default=None,
        help="Optional human label for the watchpoint.",
    )
    watchpoint_add.add_argument(
        "--value",
        default=None,
        help=(
            "Optional byte value filter (decimal or 0x-prefixed hex, 0..255). "
            "When set, the watchpoint only fires when the first byte of the "
            "accessed range equals this value."
        ),
    )
    watchpoint_add.add_argument("--json", action="store_true", help="Emit JSON.")
    watchpoint_add.set_defaults(func=_cmd_watchpoint_add)

    watchpoint_list = watchpoint_sub.add_parser(
        "list",
        help="List all watchpoints registered for one ROM.",
    )
    watchpoint_list.add_argument("rom", help="Path to a .ngp/.ngc ROM file.")
    watchpoint_list.add_argument("--json", action="store_true", help="Emit JSON.")
    watchpoint_list.set_defaults(func=_cmd_watchpoint_list)

    watchpoint_remove = watchpoint_sub.add_parser(
        "remove",
        help="Remove one watchpoint by id.",
    )
    watchpoint_remove.add_argument("rom", help="Path to a .ngp/.ngc ROM file.")
    watchpoint_remove.add_argument("id", type=int, help="Watchpoint id to remove.")
    watchpoint_remove.add_argument(
        "--json", action="store_true", help="Emit JSON."
    )
    watchpoint_remove.set_defaults(func=_cmd_watchpoint_remove)

    watchpoint_clear = watchpoint_sub.add_parser(
        "clear",
        help="Remove all watchpoints registered for one ROM.",
    )
    watchpoint_clear.add_argument("rom", help="Path to a .ngp/.ngc ROM file.")
    watchpoint_clear.add_argument(
        "--json", action="store_true", help="Emit JSON."
    )
    watchpoint_clear.set_defaults(func=_cmd_watchpoint_clear)

    watchpoint_check = watchpoint_sub.add_parser(
        "check",
        help=(
            "Match the per-ROM watchpoint registry against the memory writes "
            "captured in one event-log v1 JSON file."
        ),
    )
    watchpoint_check.add_argument("rom", help="Path to a .ngp/.ngc ROM file.")
    watchpoint_check.add_argument(
        "event_log", help="Path to one event-log v1 JSON file."
    )
    watchpoint_check.add_argument(
        "--json", action="store_true", help="Emit JSON."
    )
    watchpoint_check.set_defaults(func=_cmd_watchpoint_check)

    breakpoint_cmd = sub.add_parser(
        "breakpoint",
        help=(
            "Per-ROM PC-address breakpoint registry and event-log match "
            "(see specs/BREAKPOINTS.md). v1 is a post-run filter, not a "
            "live pause; live break-on-hit is M4 debugger territory."
        ),
    )
    breakpoint_sub = breakpoint_cmd.add_subparsers(
        dest="breakpoint_command", required=True
    )

    breakpoint_add = breakpoint_sub.add_parser(
        "add",
        help="Add one PC-address breakpoint to the per-ROM registry.",
    )
    breakpoint_add.add_argument("rom", help="Path to a .ngp/.ngc ROM file.")
    breakpoint_add.add_argument(
        "address",
        help="PC address to break on (decimal or 0x-prefixed hex).",
    )
    breakpoint_add.add_argument(
        "--label",
        default=None,
        help="Optional human label for the breakpoint.",
    )
    breakpoint_add.add_argument("--json", action="store_true", help="Emit JSON.")
    breakpoint_add.set_defaults(func=_cmd_breakpoint_add)

    breakpoint_add_symbol = breakpoint_sub.add_parser(
        "add-symbol",
        help=(
            "Resolve a function/label name via a t900ld .map file and add "
            "a breakpoint at the resolved PC. The breakpoint stores only "
            "the address; rerun `add-symbol` after rebuilding the map if "
            "the symbol moved."
        ),
    )
    breakpoint_add_symbol.add_argument(
        "rom", help="Path to a .ngp/.ngc ROM file."
    )
    breakpoint_add_symbol.add_argument(
        "symbol", help="Exact symbol name to resolve (e.g. _main, _vblank)."
    )
    breakpoint_add_symbol.add_argument(
        "--map",
        required=True,
        help="Path to the t900ld .map file used for symbol resolution.",
    )
    breakpoint_add_symbol.add_argument(
        "--label",
        default=None,
        help=(
            "Optional human label. Defaults to the resolved symbol name "
            "when omitted so `breakpoint list` stays self-describing."
        ),
    )
    breakpoint_add_symbol.add_argument(
        "--json", action="store_true", help="Emit JSON."
    )
    breakpoint_add_symbol.set_defaults(func=_cmd_breakpoint_add_symbol)

    breakpoint_list = breakpoint_sub.add_parser(
        "list",
        help="List all breakpoints registered for one ROM.",
    )
    breakpoint_list.add_argument("rom", help="Path to a .ngp/.ngc ROM file.")
    breakpoint_list.add_argument("--json", action="store_true", help="Emit JSON.")
    breakpoint_list.set_defaults(func=_cmd_breakpoint_list)

    breakpoint_remove = breakpoint_sub.add_parser(
        "remove",
        help="Remove one breakpoint by id.",
    )
    breakpoint_remove.add_argument("rom", help="Path to a .ngp/.ngc ROM file.")
    breakpoint_remove.add_argument("id", type=int, help="Breakpoint id to remove.")
    breakpoint_remove.add_argument(
        "--json", action="store_true", help="Emit JSON."
    )
    breakpoint_remove.set_defaults(func=_cmd_breakpoint_remove)

    breakpoint_clear = breakpoint_sub.add_parser(
        "clear",
        help="Remove all breakpoints registered for one ROM.",
    )
    breakpoint_clear.add_argument("rom", help="Path to a .ngp/.ngc ROM file.")
    breakpoint_clear.add_argument(
        "--json", action="store_true", help="Emit JSON."
    )
    breakpoint_clear.set_defaults(func=_cmd_breakpoint_clear)

    breakpoint_check = breakpoint_sub.add_parser(
        "check",
        help=(
            "Match the per-ROM breakpoint registry against the PC values "
            "captured in one event-log v2 JSON file."
        ),
    )
    breakpoint_check.add_argument("rom", help="Path to a .ngp/.ngc ROM file.")
    breakpoint_check.add_argument(
        "event_log", help="Path to one event-log v2 JSON file."
    )
    breakpoint_check.add_argument(
        "--json", action="store_true", help="Emit JSON."
    )
    breakpoint_check.set_defaults(func=_cmd_breakpoint_check)

    # --seed-zero-bank0 is added to every parser that runs the executor
    # (or feeds runtime state into a savestate/checkpoint/session save).
    # Keeping the wiring centralized here avoids 9 copy-pasted blocks
    # and makes it obvious which commands honor the convention.
    _SEED_ZERO_BANK0_HELP = (
        "Software-convention shortcut: set XWA/XBC/XDE/XHL/XIX/XIY to 0 "
        "before run. Most cc900/cdecl/adecl startups zero this register "
        "set, so this flag avoids repeating six --seed-reg NAME=0 lines. "
        "NOT a hardware-verified power-on reset behavior; documented as "
        "a software-convention assumption (see specs/RESET_STATE.md). "
        "Individual --seed-reg NAME=VALUE always wins over this default."
    )
    _SEED_ZERO_CALLER_SAVED_HELP = (
        "ABI/toolchain shortcut: set the observed cdecl caller-saved set "
        "XWA/XBC/XDE/XHL/XIX/XIZ to 0 before run. Useful when resuming around "
        "ordinary function calls without inventing a callee-preserved XIY value. "
        "NOT a hardware reset behavior; explicit --seed-reg NAME=VALUE still wins."
    )
    _SEED_ZERO_ADECL_ARGS_HELP = (
        "ABI/toolchain shortcut: set the observed __adecl argument registers "
        "XWA/XBC/XDE to 0 before run. Useful when exploring register-argument "
        "call boundaries without inventing non-argument scratch or frame state. "
        "NOT a hardware reset behavior; explicit --seed-reg NAME=VALUE still wins."
    )
    _SEED_ZERO_TOOLCHAIN_LOOP_IZ_HELP = (
        "Toolchain/codegen shortcut: set XIZ to 0 before run. Useful on the "
        "current thc2-style loop/copy paths where IZ often carries the live loop "
        "or post-increment pointer state and is explicitly saved/restored around calls. "
        "NOT a hardware reset behavior; explicit --seed-reg NAME=VALUE still wins."
    )
    _SEED_BIOS_HANDOFF_XSP_HELP = (
        "Sourced reset-layer shortcut: set XSP to the documented BIOS hand-off "
        "stack top 0x00006C00 before run. Useful for the historical StarGunner/"
        "bootstrap smoke context without repeating --seed-xsp 0x6C00. "
        "Explicit --seed-reg XSP=... or --seed-xsp still wins."
    )
    _SEED_BIOS_HANDOFF_MINIMAL_HELP = (
        "UI/session-equivalent hand-off shortcut: set XSP=0x00006C00 and "
        "INTNEST=0 before run. Mirrors the current EmulatorSession BIOS "
        "hand-off layer without inventing DMA control-register state. "
        "Explicit --seed-reg NAME=VALUE or --seed-xsp still wins."
    )
    _SEED_ZERO_BIOS_CALL_CONTEXT_HELP = (
        "Exploratory BIOS-call shortcut: set XBC@bank3/XDE@bank3/XHL@bank3/XIY/XIZ "
        "to 0 before run. Useful when stepping BIOS entry code that saves the bank-3 "
        "callee context. NOT a hardware-verified reset behavior; this is a pragmatic "
        "analysis seed, and any explicit --seed-reg NAME=VALUE still wins."
    )
    for seed_aware_parser in (
        execute_next,
        step_exec,
        run_steps,
        trace_exec,
        run_until_exec,
        savestate_save,
        checkpoint_save,
        session_save,
        eventlog_capture,
    ):
        seed_aware_parser.add_argument(
            "--seed-zero-bank0",
            action="store_true",
            help=_SEED_ZERO_BANK0_HELP,
        )
        seed_aware_parser.add_argument(
            "--seed-zero-caller-saved",
            action="store_true",
            help=_SEED_ZERO_CALLER_SAVED_HELP,
        )
        seed_aware_parser.add_argument(
            "--seed-zero-adecl-args",
            action="store_true",
            help=_SEED_ZERO_ADECL_ARGS_HELP,
        )
        seed_aware_parser.add_argument(
            "--seed-zero-toolchain-loop-iz",
            action="store_true",
            help=_SEED_ZERO_TOOLCHAIN_LOOP_IZ_HELP,
        )
        seed_aware_parser.add_argument(
            "--seed-bios-handoff-xsp",
            action="store_true",
            help=_SEED_BIOS_HANDOFF_XSP_HELP,
        )
        seed_aware_parser.add_argument(
            "--seed-bios-handoff-minimal",
            action="store_true",
            help=_SEED_BIOS_HANDOFF_MINIMAL_HELP,
        )
        seed_aware_parser.add_argument(
            "--seed-zero-bios-call-context",
            action="store_true",
            help=_SEED_ZERO_BIOS_CALL_CONTEXT_HELP,
        )

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except FileNotFoundError as exc:
        print(f"ERROR: file not found: {exc.filename}", file=sys.stderr)
        return 1
    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
