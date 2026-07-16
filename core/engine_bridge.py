"""Engine-facing bridge request handling for NgpCraft Emulator."""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from pathlib import Path

from core.event_log import (
    build_event_log_payload,
    diff_event_logs,
    save_event_log,
)
from core.event_log_profile import bucket_event_log_by_symbol
from core.goldens import (
    delete_named_golden,
    golden_path_for_rom,
    list_named_goldens,
    save_named_golden,
)
from core.execute import seed_cpu_state_for_execution
from core.machine import load_machine_state
from core.atlas import render_tile_atlas
from core.frame_diff import diff_ppm_bytes
from core.frame_goldens import (
    build_frame_golden_manifest,
    delete_frame_golden,
    list_frame_goldens,
    save_frame_golden,
)
from core.decode import CONTROL_REGISTER_NAMES
from core.k2ge import (
    K2GE_PALETTE_SCR1_BASE,
    K2GE_PALETTE_SCR2_BASE,
    K2GE_PALETTE_SPRITE_BASE,
    read_plane_palettes,
)
from core.memory import load_read_bus
from core.renderer import (
    frame_to_ppm_bytes,
    pixels_to_ppm_bytes,
    render_frame,
)
from core.run_steps import load_run_until
from core.seed_presets import bios_handoff_minimal_seed_registers
from core.savestate import (
    build_savestate_payload,
    compute_rom_sha256,
    load_savestate,
    save_savestate,
)
from core.symbols import SymbolTable, load_map

ENGINE_BRIDGE_REQUEST_FORMAT = "ngpc-engine-bridge-request"
ENGINE_BRIDGE_RESPONSE_FORMAT = "ngpc-engine-bridge-response"
ENGINE_BRIDGE_VERSION = "ngpc-engine-bridge.v1"
ENGINE_BRIDGE_SEED_PRESETS = {
    "bios-handoff-minimal": bios_handoff_minimal_seed_registers(),
}


class EngineBridgeError(ValueError):
    """Raised when one engine-bridge request is invalid or cannot be fulfilled."""


def load_engine_bridge_request(path: Path) -> dict[str, object]:
    """Load and validate one engine-bridge request JSON file."""
    raw = json.loads(path.read_text(encoding="utf-8-sig"))
    _validate_request(raw, path)
    return raw


def execute_engine_bridge_request(path: Path) -> dict[str, object]:
    """Execute one engine-bridge request and return the JSON-ready response."""
    request = load_engine_bridge_request(path)
    action = _require_str(request, "action", context="request")
    project = _require_dict(request, "project", context="request")
    build = _require_dict(request, "build", context="request")
    runtime = _require_dict(request, "runtime", context="request")
    artifacts = _require_dict(request, "artifacts", context="request")

    rom_path = Path(_require_str(build, "rom_path", context="build"))
    actual_rom_sha256 = compute_rom_sha256(rom_path)
    requested_rom_sha256 = _optional_str(build, "rom_sha256")
    if requested_rom_sha256 is not None and requested_rom_sha256 != actual_rom_sha256:
        raise EngineBridgeError(
            f"ROM hash mismatch: request expected {requested_rom_sha256} but "
            f"{rom_path} is {actual_rom_sha256}"
        )

    # Optional symbol awareness: if the request carries build.map_path, load
    # the t900ld map once and reuse it across all enrichment steps. The bridge
    # is strictly opt-in: when no map_path is provided, every response stays
    # byte-identical to the pre-symbol behavior.
    map_path_str = _optional_str(build, "map_path")
    symbol_table: SymbolTable | None = None
    if map_path_str is not None:
        symbol_table = load_map(map_path_str)

    _prepare_artifact_paths(artifacts)

    initial_cpu_state = None
    initial_memory_bytes: dict[int, int] | None = None
    seed_from_doc = None

    start_mode = _require_str(runtime, "start_mode", context="runtime")
    if start_mode not in {"bootstrap", "savestate"}:
        raise EngineBridgeError(
            f"runtime.start_mode must be 'bootstrap' or 'savestate', got {start_mode!r}"
        )
    if start_mode == "savestate":
        savestate_path = Path(
            _require_str(runtime, "seed_from_savestate", context="runtime")
        )
        seed_from_doc = load_savestate(savestate_path, expected_rom_path=rom_path)
        initial_cpu_state = seed_from_doc.cpu
        initial_memory_bytes = dict(seed_from_doc.writable_overlay)

    seed_xsp = _optional_int(runtime, "seed_xsp")
    seed_presets = _normalize_seed_presets(runtime.get("seed_presets"))
    seed_registers = _resolve_bridge_seed_registers(
        _normalize_seed_registers(runtime.get("seed_registers")),
        seed_presets=seed_presets,
        seed_xsp=seed_xsp,
    )
    start_pc = _optional_int(runtime, "start_pc")
    target_pc = _optional_int(runtime, "target_pc")
    max_steps = _optional_int(runtime, "max_steps") or 8
    note = _optional_str(request, "note")

    artifact_block = {
        "event_log_path": _optional_path_str(artifacts, "event_log_path"),
        "savestate_path": _optional_path_str(artifacts, "savestate_path"),
        "trace_path": _optional_path_str(artifacts, "trace_path"),
        "capture_dir": _optional_path_str(artifacts, "capture_dir"),
        "screenshot_path": _optional_path_str(artifacts, "screenshot_path"),
        "tile_atlas_path": _optional_path_str(artifacts, "tile_atlas_path"),
    }

    if action == "capture-eventlog":
        event_log_path = _require_str(
            artifacts,
            "event_log_path",
            context="artifacts for capture-eventlog",
        )
        payload = _build_eventlog_for_bridge(
            rom_path=rom_path,
            start_pc=start_pc,
            target_pc=target_pc,
            max_steps=max_steps,
            seed_registers=seed_registers,
            seed_xsp=seed_xsp,
            initial_cpu_state=initial_cpu_state,
            initial_memory_bytes=initial_memory_bytes,
            seed_from_doc=seed_from_doc,
            note=note,
        )
        save_event_log(Path(event_log_path), payload)
        summary = _require_dict(payload, "summary", context="event log payload")
        result = _result_with_symbol_enrichment(
            {
                "stop_reason": summary["stop_reason"],
                "executed_count": summary["executed_count"],
                "final_cpu_pc": summary["final_cpu_pc"],
            },
            symbol_table=symbol_table,
            event_log_payload=payload,
        )
        return _response(
            action=action,
            status="ok",
            summary_text=(
                f"Captured event log with {summary['emitted_count']} event(s) "
                f"for project {project.get('project_name') or '<unknown>'}."
            ),
            rom_path=rom_path,
            rom_sha256=actual_rom_sha256,
            artifacts=artifact_block,
            result=result,
            error=None,
        )

    if action == "smoke-run":
        payload = _build_eventlog_for_bridge(
            rom_path=rom_path,
            start_pc=start_pc,
            target_pc=target_pc,
            max_steps=max_steps,
            seed_registers=seed_registers,
            seed_xsp=seed_xsp,
            initial_cpu_state=initial_cpu_state,
            initial_memory_bytes=initial_memory_bytes,
            seed_from_doc=seed_from_doc,
            note=note,
        )
        event_log_path = _optional_str(artifacts, "event_log_path")
        if event_log_path:
            save_event_log(Path(event_log_path), payload)
        summary = _require_dict(payload, "summary", context="event log payload")
        result = _result_with_symbol_enrichment(
            {
                "stop_reason": summary["stop_reason"],
                "executed_count": summary["executed_count"],
                "final_cpu_pc": summary["final_cpu_pc"],
            },
            symbol_table=symbol_table,
            event_log_payload=payload,
        )
        return _response(
            action=action,
            status="ok",
            summary_text=(
                f"Smoke run completed with stop reason {summary['stop_reason']} "
                f"after {summary['executed_count']} executed step(s)."
            ),
            rom_path=rom_path,
            rom_sha256=actual_rom_sha256,
            artifacts=artifact_block,
            result=result,
            error=None,
        )

    if action == "capture-savestate":
        savestate_path = _require_str(
            artifacts,
            "savestate_path",
            context="artifacts for capture-savestate",
        )
        machine = load_machine_state(rom_path)

        final_cpu = machine.cpu if initial_cpu_state is None else initial_cpu_state
        if start_pc is not None:
            final_cpu = replace(final_cpu, pc=start_pc)
        if seed_registers or seed_xsp is not None:
            final_cpu = seed_cpu_state_for_execution(
                final_cpu,
                register_values=seed_registers,
                seed_xsp=seed_xsp,
            )
        final_memory = {} if initial_memory_bytes is None else dict(initial_memory_bytes)
        stop_reason = "state-captured"
        matched_quirk = seed_from_doc.matched_on_last_step if seed_from_doc is not None else None

        if target_pc is not None:
            run_result = load_run_until(
                rom_path,
                target_pc=target_pc,
                start_pc=start_pc,
                seed_registers=seed_registers,
                seed_xsp=seed_xsp,
                max_steps=max_steps,
                initial_cpu_state=initial_cpu_state,
                initial_memory_bytes=initial_memory_bytes,
            )
            final_cpu = run_result.final_cpu
            final_memory = dict(run_result.final_memory)
            stop_reason = run_result.stop_reason
            if run_result.last_record is not None:
                matched_quirk = run_result.last_record.execution.matched_quirk

        payload = build_savestate_payload(
            rom_path=rom_path,
            rom_header=machine.header,
            cpu=final_cpu,
            writable_overlay=final_memory,
            matched_on_last_step=matched_quirk,
            note=note,
        )
        save_savestate(Path(savestate_path), payload)
        result = _result_with_symbol_enrichment(
            {
                "stop_reason": stop_reason,
                "executed_count": None if target_pc is None else run_result.executed_count,
                "final_cpu_pc": final_cpu.pc,
            },
            symbol_table=symbol_table,
            event_log_payload=None,
        )
        return _response(
            action=action,
            status="ok",
            summary_text=(
                f"Captured savestate at PC 0x{final_cpu.pc:08X} "
                f"for project {project.get('project_name') or '<unknown>'}."
            ),
            rom_path=rom_path,
            rom_sha256=actual_rom_sha256,
            artifacts=artifact_block,
            result=result,
            error=None,
        )

    if action == "render-screenshot":
        screenshot_path_str = _require_str(
            artifacts,
            "screenshot_path",
            context="artifacts for render-screenshot",
        )
        # Build the merged memory view the same way the `screenshot` CLI
        # does: cold-start image (`load_read_bus.builtin_bytes`) overlayed
        # with an optional savestate writable overlay when start_mode is
        # 'savestate'.
        bus = load_read_bus(rom_path, frame_state=(seed_from_doc.frame_state if seed_from_doc is not None else None))
        memory: dict[int, int] = dict(bus.builtin_bytes)
        if initial_memory_bytes is not None:
            memory.update(initial_memory_bytes)

        frame = render_frame(memory)
        ppm_bytes = frame_to_ppm_bytes(frame)
        screenshot_path = Path(screenshot_path_str)
        screenshot_path.parent.mkdir(parents=True, exist_ok=True)
        screenshot_path.write_bytes(ppm_bytes)

        artifact_block["screenshot_path"] = str(screenshot_path)

        ctrl = frame.control
        result = {
            "stop_reason": "frame-rendered",
            "executed_count": 0,
            "final_cpu_pc": (
                initial_cpu_state.pc if initial_cpu_state is not None
                else load_machine_state(rom_path).cpu.pc
            ),
            "screenshot": {
                "width": frame.width,
                "height": frame.height,
                "ppm_byte_count": len(ppm_bytes),
                "ppm_sha256": hashlib.sha256(ppm_bytes).hexdigest(),
                "backdrop_color": {
                    "r": frame.backdrop_color.r,
                    "g": frame.backdrop_color.g,
                    "b": frame.backdrop_color.b,
                    "hex_rgb24": frame.backdrop_color.hex_rgb24(),
                },
                "control_snapshot": {
                    "window": {
                        "wba_h": ctrl.wba_h, "wba_v": ctrl.wba_v,
                        "wsi_h": ctrl.wsi_h, "wsi_v": ctrl.wsi_v,
                    },
                    "scroll_prio": {"scr2_in_front": ctrl.scr2_in_front},
                    "scroll_offsets": {
                        "s1so_h": ctrl.s1so_h, "s1so_v": ctrl.s1so_v,
                        "s2so_h": ctrl.s2so_h, "s2so_v": ctrl.s2so_v,
                    },
                    "sprite_offset": {"po_h": ctrl.po_h, "po_v": ctrl.po_v},
                    "twod_control": {"neg": ctrl.neg, "oowc": ctrl.oowc},
                    "backdrop_control": {
                        "bgc_enabled": ctrl.bgc_enabled,
                        "bgc_index": ctrl.bgc_index,
                        "bgc_raw_hex": f"0x{ctrl.bgc_raw:02X}",
                    },
                    "mode": {"k1ge_compat": ctrl.k1ge_compat},
                },
                "renderer_pass": "1.3",
            },
        }
        # No event log produced; final-symbol enrichment still kicks in if
        # the request carried `build.map_path`.
        result = _result_with_symbol_enrichment(
            result, symbol_table=symbol_table, event_log_payload=None,
        )
        return _response(
            action=action,
            status="ok",
            summary_text=(
                f"Rendered {frame.width}×{frame.height} frame "
                f"({len(ppm_bytes)} PPM bytes) for project "
                f"{project.get('project_name') or '<unknown>'}."
            ),
            rom_path=rom_path,
            rom_sha256=actual_rom_sha256,
            artifacts=artifact_block,
            result=result,
            error=None,
        )

    if action == "render-tile-atlas":
        tile_atlas_path_str = _require_str(
            artifacts,
            "tile_atlas_path",
            context="artifacts for render-tile-atlas",
        )

        # Atlas-specific params live under `runtime.atlas` (optional;
        # full CHAR_RAM grayscale 16-col atlas is the default).
        atlas_block_raw = runtime.get("atlas")
        if atlas_block_raw is not None and not isinstance(atlas_block_raw, dict):
            raise EngineBridgeError(
                "runtime.atlas must be a dict when provided"
            )
        atlas_block: dict[str, object] = atlas_block_raw or {}

        tile_range = atlas_block.get("tile_range")
        if tile_range is None:
            tile_ids = list(range(512))
        else:
            if (
                not isinstance(tile_range, (list, tuple))
                or len(tile_range) != 2
                or not all(isinstance(v, int) for v in tile_range)
            ):
                raise EngineBridgeError(
                    "runtime.atlas.tile_range must be [start, end] of ints"
                )
            start, end = tile_range
            if start < 0 or end >= 512 or start > end:
                raise EngineBridgeError(
                    f"runtime.atlas.tile_range out of bounds: [{start}, {end}]"
                )
            tile_ids = list(range(start, end + 1))

        cols_raw = atlas_block.get("cols", 16)
        if not isinstance(cols_raw, int) or cols_raw < 1:
            raise EngineBridgeError(
                "runtime.atlas.cols must be a positive integer"
            )
        cols = cols_raw

        palette_plane = atlas_block.get("palette_plane")
        palette_index = atlas_block.get("palette_index")
        if (palette_plane is None) != (palette_index is None):
            raise EngineBridgeError(
                "runtime.atlas.palette_plane and palette_index must be set "
                "together (both null = grayscale)"
            )

        # Build the same merged memory view as `render-screenshot`.
        bus = load_read_bus(rom_path, frame_state=(seed_from_doc.frame_state if seed_from_doc is not None else None))
        memory: dict[int, int] = dict(bus.builtin_bytes)
        if initial_memory_bytes is not None:
            memory.update(initial_memory_bytes)

        palette = None
        palette_payload = None
        if palette_plane is not None:
            if palette_plane not in {"sprite", "scr1", "scr2"}:
                raise EngineBridgeError(
                    "runtime.atlas.palette_plane must be 'sprite', 'scr1' or 'scr2'"
                )
            if (
                not isinstance(palette_index, int)
                or not 0 <= palette_index < 16
            ):
                raise EngineBridgeError(
                    "runtime.atlas.palette_index must be 0..15"
                )
            plane_base = {
                "sprite": K2GE_PALETTE_SPRITE_BASE,
                "scr1": K2GE_PALETTE_SCR1_BASE,
                "scr2": K2GE_PALETTE_SCR2_BASE,
            }[palette_plane]
            palettes = read_plane_palettes(memory, plane_base, palette_plane)
            palette = palettes[palette_index]
            palette_payload = {
                "plane": palette.plane,
                "index": palette.index,
                "colors": [
                    {"r": c.r, "g": c.g, "b": c.b, "hex_rgb24": c.hex_rgb24()}
                    for c in palette.colors
                ],
            }

        width, height, pixels = render_tile_atlas(
            memory, tile_ids, cols=cols, palette=palette,
        )
        ppm_bytes = pixels_to_ppm_bytes(width, height, pixels)
        atlas_path = Path(tile_atlas_path_str)
        atlas_path.parent.mkdir(parents=True, exist_ok=True)
        atlas_path.write_bytes(ppm_bytes)

        artifact_block["tile_atlas_path"] = str(atlas_path)

        rows = (len(tile_ids) + cols - 1) // cols if tile_ids else 0
        result = {
            "stop_reason": "atlas-rendered",
            "executed_count": 0,
            "final_cpu_pc": (
                initial_cpu_state.pc if initial_cpu_state is not None
                else load_machine_state(rom_path).cpu.pc
            ),
            "tile_atlas": {
                "width": width,
                "height": height,
                "ppm_byte_count": len(ppm_bytes),
                "ppm_sha256": hashlib.sha256(ppm_bytes).hexdigest(),
                "tile_count": len(tile_ids),
                "first_tile": tile_ids[0] if tile_ids else None,
                "last_tile": tile_ids[-1] if tile_ids else None,
                "cols": cols,
                "rows": rows,
                "colorisation": (
                    "palette" if palette is not None else "grayscale"
                ),
                "palette_plane": palette_plane,
                "palette_index": palette_index,
                "palette": palette_payload,
                "renderer_pass": "1.3",
            },
        }
        result = _result_with_symbol_enrichment(
            result, symbol_table=symbol_table, event_log_payload=None,
        )
        return _response(
            action=action,
            status="ok",
            summary_text=(
                f"Rendered {len(tile_ids)}-tile atlas "
                f"({width}×{height} px, {len(ppm_bytes)} PPM bytes) for "
                f"project {project.get('project_name') or '<unknown>'}."
            ),
            rom_path=rom_path,
            rom_sha256=actual_rom_sha256,
            artifacts=artifact_block,
            result=result,
            error=None,
        )

    if action == "check-frame-golden-all":
        # Optional triage artifact path (analog of CLI --save-current-dir
        # but for one file). When set, the rendered current frame is
        # written there so the engine UI can show a side-by-side.
        save_current_str = _optional_path_str(artifacts, "screenshot_path")

        stop_on_fail_raw = runtime.get("stop_on_fail", False)
        if not isinstance(stop_on_fail_raw, bool):
            raise EngineBridgeError(
                "runtime.stop_on_fail must be a bool when provided"
            )
        stop_on_fail = stop_on_fail_raw

        goldens = list_frame_goldens(rom_path)

        bus = load_read_bus(rom_path, frame_state=(seed_from_doc.frame_state if seed_from_doc is not None else None))
        memory: dict[int, int] = dict(bus.builtin_bytes)
        if initial_memory_bytes is not None:
            memory.update(initial_memory_bytes)
        frame = render_frame(memory)
        ppm_bytes = frame_to_ppm_bytes(frame)

        if save_current_str is not None:
            save_current_path = Path(save_current_str)
            save_current_path.parent.mkdir(parents=True, exist_ok=True)
            save_current_path.write_bytes(ppm_bytes)
            artifact_block["screenshot_path"] = str(save_current_path)

        results: list[dict[str, object]] = []
        passed = 0
        failed = 0
        short_circuited = False
        for golden in goldens:
            try:
                stored_ppm = golden.ppm_path.read_bytes()
                diff_result = diff_ppm_bytes(stored_ppm, ppm_bytes)
            except (OSError, ValueError) as exc:
                results.append({
                    "name": golden.name,
                    "status": "error",
                    "error": str(exc),
                })
                failed += 1
                if stop_on_fail:
                    short_circuited = True
                    break
                continue

            entry: dict[str, object] = {
                "name": golden.name,
                "status": "match" if diff_result.equal else "diff",
                "equal": diff_result.equal,
                "pixel_count_different": diff_result.pixel_count_different,
                "diff_ratio": diff_result.diff_ratio,
                "first_diff_pixel": (
                    list(diff_result.first_diff_pixel)
                    if diff_result.first_diff_pixel else None
                ),
            }
            results.append(entry)
            if diff_result.equal:
                passed += 1
            else:
                failed += 1
                if stop_on_fail:
                    short_circuited = True
                    break

        all_equal = failed == 0
        status_str = "ok" if all_equal else "error"
        result = {
            "stop_reason": "frame-goldens-checked",
            "executed_count": 0,
            "final_cpu_pc": (
                initial_cpu_state.pc if initial_cpu_state is not None
                else load_machine_state(rom_path).cpu.pc
            ),
            "frame_goldens_check": {
                "total": len(goldens),
                "checked": len(results),
                "passed": passed,
                "failed": failed,
                "all_equal": all_equal,
                "stopped_early": short_circuited,
                "results": results,
            },
        }
        result = _result_with_symbol_enrichment(
            result, symbol_table=symbol_table, event_log_payload=None,
        )

        if not goldens:
            summary_text = (
                f"No frame goldens registered for {rom_path}."
            )
        elif all_equal:
            summary_text = (
                f"All {len(results)} frame golden(s) matched for "
                f"project {project.get('project_name') or '<unknown>'}."
            )
        else:
            summary_text = (
                f"{failed}/{len(results)} frame golden(s) failed for "
                f"project {project.get('project_name') or '<unknown>'}."
                + (" (stopped early)" if short_circuited else "")
            )
        return _response(
            action=action,
            status=status_str,
            summary_text=summary_text,
            rom_path=rom_path,
            rom_sha256=actual_rom_sha256,
            artifacts=artifact_block,
            result=result,
            error=(
                None if all_equal
                else {
                    "type": "frame-golden-mismatch",
                    "message": summary_text,
                }
            ),
        )

    if action == "check-eventlog-golden-all":
        # Optional triage write target — reuses the existing
        # `event_log_path` artifact slot. When set, the captured current
        # event log is written there so the engine UI can show a
        # side-by-side against any divergent golden.
        save_current_str = _optional_path_str(artifacts, "event_log_path")

        stop_on_fail_raw = runtime.get("stop_on_fail", False)
        if not isinstance(stop_on_fail_raw, bool):
            raise EngineBridgeError(
                "runtime.stop_on_fail must be a bool when provided"
            )
        stop_on_fail = stop_on_fail_raw

        goldens = list_named_goldens(rom_path)

        # Capture ONE current event log via the same builder the
        # capture-eventlog / smoke-run actions use, then diff against
        # every stored golden (render-once-diff-many pattern).
        current_payload = _build_eventlog_for_bridge(
            rom_path=rom_path,
            start_pc=start_pc,
            target_pc=target_pc,
            max_steps=max_steps,
            seed_registers=seed_registers,
            seed_xsp=seed_xsp,
            initial_cpu_state=initial_cpu_state,
            initial_memory_bytes=initial_memory_bytes,
            seed_from_doc=seed_from_doc,
            note=note,
        )
        if save_current_str is not None:
            save_event_log(Path(save_current_str), current_payload)
            artifact_block["event_log_path"] = save_current_str

        results: list[dict[str, object]] = []
        passed = 0
        failed = 0
        short_circuited = False
        for golden in goldens:
            diff_payload = diff_event_logs(golden.payload, current_payload)
            first = diff_payload["first_divergence"]
            status_label = "match" if first is None else "mismatch"
            entry: dict[str, object] = {
                "name": golden.name,
                "golden_path": str(golden.path),
                "status": status_label,
                "first_divergence": first,
            }
            results.append(entry)
            if first is None:
                passed += 1
            else:
                failed += 1
                if stop_on_fail:
                    short_circuited = True
                    break

        all_match = failed == 0
        status_str = "ok" if all_match else "error"

        # Symbol enrichment off the current capture's final PC.
        summary = _require_dict(current_payload, "summary", context="event log payload")
        result = _result_with_symbol_enrichment(
            {
                "stop_reason": "eventlog-goldens-checked",
                "executed_count": summary["executed_count"],
                "final_cpu_pc": summary["final_cpu_pc"],
                "eventlog_goldens_check": {
                    "total": len(goldens),
                    "checked": len(results),
                    "passed": passed,
                    "failed": failed,
                    "all_equal": all_match,
                    "stopped_early": short_circuited,
                    "results": results,
                },
            },
            symbol_table=symbol_table,
            event_log_payload=current_payload,
        )

        if not goldens:
            summary_text = (
                f"No event-log goldens registered for {rom_path}."
            )
        elif all_match:
            summary_text = (
                f"All {len(results)} event-log golden(s) matched for "
                f"project {project.get('project_name') or '<unknown>'}."
            )
        else:
            summary_text = (
                f"{failed}/{len(results)} event-log golden(s) failed for "
                f"project {project.get('project_name') or '<unknown>'}."
                + (" (stopped early)" if short_circuited else "")
            )
        return _response(
            action=action,
            status=status_str,
            summary_text=summary_text,
            rom_path=rom_path,
            rom_sha256=actual_rom_sha256,
            artifacts=artifact_block,
            result=result,
            error=(
                None if all_match
                else {
                    "type": "eventlog-golden-mismatch",
                    "message": summary_text,
                }
            ),
        )

    if action == "save-frame-golden":
        golden_name = _require_str(request, "golden_name", context="request")
        golden_label = _optional_str(request, "golden_label")

        # Render via the same path as `render-screenshot` so a saved
        # golden is bit-identical to what `render-screenshot` would
        # produce for the same memory view.
        bus = load_read_bus(rom_path, frame_state=(seed_from_doc.frame_state if seed_from_doc is not None else None))
        memory: dict[int, int] = dict(bus.builtin_bytes)
        if initial_memory_bytes is not None:
            memory.update(initial_memory_bytes)
        frame = render_frame(memory)
        ppm_bytes = frame_to_ppm_bytes(frame)

        seed_from_label = (
            _optional_str(runtime, "seed_from_savestate")
            if start_mode == "savestate"
            else None
        )

        ctrl = frame.control
        control_snapshot = {
            "window": {
                "wba_h": ctrl.wba_h, "wba_v": ctrl.wba_v,
                "wsi_h": ctrl.wsi_h, "wsi_v": ctrl.wsi_v,
            },
            "scroll_prio": {"scr2_in_front": ctrl.scr2_in_front},
            "scroll_offsets": {
                "s1so_h": ctrl.s1so_h, "s1so_v": ctrl.s1so_v,
                "s2so_h": ctrl.s2so_h, "s2so_v": ctrl.s2so_v,
            },
            "sprite_offset": {"po_h": ctrl.po_h, "po_v": ctrl.po_v},
            "twod_control": {"neg": ctrl.neg, "oowc": ctrl.oowc},
            "backdrop_control": {
                "bgc_enabled": ctrl.bgc_enabled,
                "bgc_index": ctrl.bgc_index,
                "bgc_raw_hex": f"0x{ctrl.bgc_raw:02X}",
            },
            "mode": {"k1ge_compat": ctrl.k1ge_compat},
        }

        manifest = build_frame_golden_manifest(
            rom_path=rom_path,
            name=golden_name,
            ppm_bytes=ppm_bytes,
            width=frame.width,
            height=frame.height,
            label=golden_label,
            seed_from=seed_from_label,
            control_snapshot=control_snapshot,
        )
        ppm_path, manifest_path = save_frame_golden(
            rom_path, golden_name, ppm_bytes, manifest,
        )

        result = {
            "stop_reason": "frame-golden-saved",
            "executed_count": 0,
            "final_cpu_pc": (
                initial_cpu_state.pc if initial_cpu_state is not None
                else load_machine_state(rom_path).cpu.pc
            ),
            "frame_golden_save": {
                "name": golden_name,
                "slug": manifest["slug"],
                "label": golden_label,
                "ppm_path": str(ppm_path),
                "manifest_path": str(manifest_path),
                "ppm_byte_count": len(ppm_bytes),
                "ppm_sha256": manifest["ppm_sha256"],
                "width": frame.width,
                "height": frame.height,
                "captured_at_utc": manifest["captured_at_utc"],
                "renderer_pass": manifest["renderer_pass"],
                "seed_from": seed_from_label,
            },
        }
        result = _result_with_symbol_enrichment(
            result, symbol_table=symbol_table, event_log_payload=None,
        )

        return _response(
            action=action,
            status="ok",
            summary_text=(
                f"Saved frame golden '{golden_name}' "
                f"({frame.width}×{frame.height}, {len(ppm_bytes)} bytes) for "
                f"project {project.get('project_name') or '<unknown>'}."
            ),
            rom_path=rom_path,
            rom_sha256=actual_rom_sha256,
            artifacts=artifact_block,
            result=result,
            error=None,
        )

    if action == "save-eventlog-golden":
        golden_name = _require_str(request, "golden_name", context="request")

        # Capture one event log via the same builder as
        # `capture-eventlog` / `smoke-run` / `check-eventlog-golden-all`.
        payload = _build_eventlog_for_bridge(
            rom_path=rom_path,
            start_pc=start_pc,
            target_pc=target_pc,
            max_steps=max_steps,
            seed_registers=seed_registers,
            seed_xsp=seed_xsp,
            initial_cpu_state=initial_cpu_state,
            initial_memory_bytes=initial_memory_bytes,
            seed_from_doc=seed_from_doc,
            note=note,
        )

        golden_path = golden_path_for_rom(rom_path, golden_name)
        save_named_golden(golden_path, payload)

        summary = _require_dict(payload, "summary", context="event log payload")
        result = _result_with_symbol_enrichment(
            {
                "stop_reason": "eventlog-golden-saved",
                "executed_count": summary["executed_count"],
                "final_cpu_pc": summary["final_cpu_pc"],
                "eventlog_golden_save": {
                    "name": golden_name,
                    "golden_path": str(golden_path),
                    "executed_count": summary["executed_count"],
                    "final_cpu_pc": summary["final_cpu_pc"],
                    "emitted_count": summary["emitted_count"],
                    "stop_reason_capture": summary["stop_reason"],
                    "note": note,
                },
            },
            symbol_table=symbol_table,
            event_log_payload=payload,
        )

        return _response(
            action=action,
            status="ok",
            summary_text=(
                f"Saved event-log golden '{golden_name}' with "
                f"{summary['emitted_count']} event(s) for project "
                f"{project.get('project_name') or '<unknown>'}."
            ),
            rom_path=rom_path,
            rom_sha256=actual_rom_sha256,
            artifacts=artifact_block,
            result=result,
            error=None,
        )

    if action == "delete-frame-golden":
        golden_name = _require_str(request, "golden_name", context="request")
        try:
            ppm_path, manifest_path = delete_frame_golden(rom_path, golden_name)
        except FileNotFoundError as exc:
            raise EngineBridgeError(
                f"frame golden not found for '{golden_name}': {exc}"
            ) from None

        result = _result_with_symbol_enrichment(
            {
                "stop_reason": "frame-golden-deleted",
                "executed_count": 0,
                "final_cpu_pc": load_machine_state(rom_path).cpu.pc,
                "frame_golden_delete": {
                    "name": golden_name,
                    "deleted_ppm_path": str(ppm_path),
                    "deleted_manifest_path": str(manifest_path),
                },
            },
            symbol_table=symbol_table,
            event_log_payload=None,
        )

        return _response(
            action=action,
            status="ok",
            summary_text=(
                f"Deleted frame golden '{golden_name}' for project "
                f"{project.get('project_name') or '<unknown>'}."
            ),
            rom_path=rom_path,
            rom_sha256=actual_rom_sha256,
            artifacts=artifact_block,
            result=result,
            error=None,
        )

    if action == "delete-eventlog-golden":
        golden_name = _require_str(request, "golden_name", context="request")
        try:
            golden_path = delete_named_golden(rom_path, golden_name)
        except FileNotFoundError as exc:
            raise EngineBridgeError(
                f"event-log golden not found for '{golden_name}': {exc}"
            ) from None

        result = _result_with_symbol_enrichment(
            {
                "stop_reason": "eventlog-golden-deleted",
                "executed_count": 0,
                "final_cpu_pc": load_machine_state(rom_path).cpu.pc,
                "eventlog_golden_delete": {
                    "name": golden_name,
                    "deleted_golden_path": str(golden_path),
                },
            },
            symbol_table=symbol_table,
            event_log_payload=None,
        )

        return _response(
            action=action,
            status="ok",
            summary_text=(
                f"Deleted event-log golden '{golden_name}' for project "
                f"{project.get('project_name') or '<unknown>'}."
            ),
            rom_path=rom_path,
            rom_sha256=actual_rom_sha256,
            artifacts=artifact_block,
            result=result,
            error=None,
        )

    if action in {"run", "debug", "profile"}:
        event_log_path = _optional_str(artifacts, "event_log_path")
        result = _result_with_symbol_enrichment(
            {
                "stop_reason": "not-gui-wired-yet",
                "executed_count": 0,
                "final_cpu_pc": load_machine_state(rom_path).cpu.pc,
            },
            symbol_table=symbol_table,
            event_log_payload=None,
        )
        summary_text = (
            f"Action {action!r} is accepted by the bridge but only a headless "
            "prototype fallback exists yet."
        )
        if event_log_path:
            payload = _build_eventlog_for_bridge(
                rom_path=rom_path,
                start_pc=start_pc,
                target_pc=target_pc,
                max_steps=max_steps,
                seed_registers=seed_registers,
                seed_xsp=seed_xsp,
                initial_cpu_state=initial_cpu_state,
                initial_memory_bytes=initial_memory_bytes,
                seed_from_doc=seed_from_doc,
                note=note,
            )
            save_event_log(Path(event_log_path), payload)
            summary = _require_dict(payload, "summary", context="event log payload")
            result = _result_with_symbol_enrichment(
                {
                    "stop_reason": summary["stop_reason"],
                    "executed_count": summary["executed_count"],
                    "final_cpu_pc": summary["final_cpu_pc"],
                },
                symbol_table=symbol_table,
                event_log_payload=payload,
            )
            summary_text = (
                f"Action {action!r} is not GUI-wired yet; emitted a bounded headless "
                f"event-log fallback with {summary['emitted_count']} event(s)."
            )
        return _response(
            action=action,
            status="partial",
            summary_text=summary_text,
            rom_path=rom_path,
            rom_sha256=actual_rom_sha256,
            artifacts=artifact_block,
            result=result,
            error=None,
        )

    raise EngineBridgeError(f"unsupported bridge action: {action!r}")


def build_error_response(
    *,
    action: str | None,
    summary_text: str,
    error_type: str,
) -> dict[str, object]:
    """Build one structured bridge error response."""
    return {
        "format": ENGINE_BRIDGE_RESPONSE_FORMAT,
        "format_version": ENGINE_BRIDGE_VERSION,
        "action": action,
        "status": "error",
        "summary": summary_text,
        "rom": {
            "path": None,
            "sha256": None,
        },
        "artifacts": {
            "event_log_path": None,
            "savestate_path": None,
            "trace_path": None,
            "capture_dir": None,
            "screenshot_path": None,
            "tile_atlas_path": None,
        },
        "result": None,
        "error": {
            "type": error_type,
            "message": summary_text,
        },
    }


_BRIDGE_PROFILE_EXCERPT_TOP_N = 5


def _result_with_symbol_enrichment(
    base_result: dict[str, object],
    *,
    symbol_table: SymbolTable | None,
    event_log_payload: dict[str, object] | None,
) -> dict[str, object]:
    """Enrich an action result block with symbol-aware fields when requested.

    When the bridge request carries `build.map_path`, the table loaded once
    upstream is passed in here. Two enrichments happen:

      1. `final_symbol`: the symbol that owns the result's `final_cpu_pc`
         (same shape as the CLI commands' `final_symbol` block, minus the
         `note` to keep response sizes reasonable).
      2. `event_log_profile_excerpt`: when the action produced an event log,
         a small top-N per-symbol bucketing (5 buckets max) lets the engine
         caller see the dominant functions without loading the full log.

    Both enrichments are strictly additive. With no `symbol_table` (i.e. no
    `map_path` in the request), the result is returned unchanged so the
    response stays byte-identical to the pre-symbol bridge behavior.
    """
    if symbol_table is None:
        return base_result

    enriched = dict(base_result)
    final_pc = base_result.get("final_cpu_pc")
    if isinstance(final_pc, int):
        sym = symbol_table.lookup_address(final_pc)
        if sym is None:
            enriched["final_symbol"] = {
                "map_source": symbol_table.source_path,
                "queried_pc": final_pc,
                "queried_pc_hex": f"0x{final_pc:08X}",
                "found": False,
                "owning_symbol": None,
                "owning_symbol_address_hex": None,
                "offset_from_symbol": None,
                "section": None,
            }
        else:
            enriched["final_symbol"] = {
                "map_source": symbol_table.source_path,
                "queried_pc": final_pc,
                "queried_pc_hex": f"0x{final_pc:08X}",
                "found": True,
                "owning_symbol": sym.name,
                "owning_symbol_address_hex": f"0x{sym.address:08X}",
                "offset_from_symbol": final_pc - sym.address,
                "section": sym.section,
            }

    if event_log_payload is not None:
        profile = bucket_event_log_by_symbol(event_log_payload, symbol_table)
        enriched["event_log_profile_excerpt"] = {
            "map_source": profile["map_source"],
            "total_events": profile["total_events"],
            "resolved_events": profile["resolved_events"],
            "unresolved_events": profile["unresolved_events"],
            "distinct_symbols": profile["distinct_symbols"],
            "halted_status_breakdown": profile["halted_status_breakdown"],
            "top_buckets": profile["buckets"][:_BRIDGE_PROFILE_EXCERPT_TOP_N],
            "top_n": _BRIDGE_PROFILE_EXCERPT_TOP_N,
            "note": (
                "Top-N per-symbol bucketing of the event log captured by "
                "this action. Use `eventlog profile` on the saved log for "
                "the full breakdown."
            ),
        }
    return enriched


def _build_eventlog_for_bridge(
    *,
    rom_path: Path,
    start_pc: int | None,
    target_pc: int | None,
    max_steps: int,
    seed_registers: dict[str, int] | None,
    seed_xsp: int | None,
    initial_cpu_state: object,
    initial_memory_bytes: dict[int, int] | None,
    seed_from_doc: object,
    note: str | None,
) -> dict[str, object]:
    view = load_machine_state(rom_path)
    from core.fetch import load_fetch_view

    # M3 Phase 3.1b: forward seed savestate's frame_state to the bus
    # so CPU reads of RAS.V + BLNK during the captured run match HW.
    initial_frame_state = (
        seed_from_doc.frame_state if seed_from_doc is not None else None
    )
    fetch_view = load_fetch_view(rom_path, frame_state=initial_frame_state)
    seed_from_payload = None
    if seed_from_doc is not None:
        seed_from_payload = {
            "format_version": seed_from_doc.format_version,
            "rom_sha256": seed_from_doc.rom_sha256,
            "cpu_pc": seed_from_doc.cpu.pc,
        }
    return build_event_log_payload(
        rom_path=rom_path,
        rom_header=view.header,
        view=fetch_view,
        start_pc=start_pc,
        target_pc=target_pc,
        max_steps=max_steps,
        cpu_state=initial_cpu_state,
        memory_bytes=initial_memory_bytes,
        seed_registers=seed_registers,
        seed_xsp=seed_xsp,
        seed_from_savestate=seed_from_payload,
        note=note,
    )


def _prepare_artifact_paths(artifacts: dict[str, object]) -> None:
    for key in ("workspace_dir", "capture_dir"):
        path_str = _optional_str(artifacts, key)
        if path_str:
            Path(path_str).mkdir(parents=True, exist_ok=True)
    for key in (
        "event_log_path", "savestate_path", "trace_path",
        "screenshot_path", "tile_atlas_path",
    ):
        path_str = _optional_str(artifacts, key)
        if path_str:
            Path(path_str).parent.mkdir(parents=True, exist_ok=True)


def _normalize_seed_registers(raw: object) -> dict[str, int] | None:
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise EngineBridgeError("runtime.seed_registers must be an object when present")
    allowed_control_registers = {name.upper() for name in CONTROL_REGISTER_NAMES.values()}
    result: dict[str, int] = {}
    for key, value in raw.items():
        if not isinstance(key, str) or key.upper() not in {
            "XWA",
            "XBC",
            "XDE",
            "XHL",
            "XIX",
            "XIY",
            "XIZ",
            "XSP",
            *allowed_control_registers,
        }:
            raise EngineBridgeError(
                "runtime.seed_registers keys must be one of: XWA, XBC, XDE, "
                "XHL, XIX, XIY, XIZ, XSP, DMAS0..3, DMAD0..3, DMAC0..3, DMAM0..3, INTNEST"
            )
        if not isinstance(value, int):
            raise EngineBridgeError(
                f"runtime.seed_registers[{key!r}] must be an integer"
            )
        result[key.upper()] = value & 0xFFFFFFFF
    return result or None


def _normalize_seed_presets(raw: object) -> tuple[str, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise EngineBridgeError("runtime.seed_presets must be a list when present")
    result: list[str] = []
    for entry in raw:
        if not isinstance(entry, str):
            raise EngineBridgeError("runtime.seed_presets entries must be strings")
        if entry not in ENGINE_BRIDGE_SEED_PRESETS:
            raise EngineBridgeError(
                "runtime.seed_presets entries must be one of: "
                + ", ".join(sorted(ENGINE_BRIDGE_SEED_PRESETS))
            )
        result.append(entry)
    return tuple(result)


def _resolve_bridge_seed_registers(
    explicit_seed_registers: dict[str, int] | None,
    *,
    seed_presets: tuple[str, ...],
    seed_xsp: int | None,
) -> dict[str, int] | None:
    if not seed_presets:
        return explicit_seed_registers
    merged: dict[str, int] = {}
    for preset_name in seed_presets:
        merged.update(ENGINE_BRIDGE_SEED_PRESETS[preset_name])
    if seed_xsp is not None:
        merged.pop("XSP", None)
    if explicit_seed_registers:
        merged.update(explicit_seed_registers)
    return merged or None


def _validate_request(raw: object, path: Path) -> None:
    if not isinstance(raw, dict):
        raise EngineBridgeError(f"Engine bridge request at {path} is not a JSON object.")
    if raw.get("format") != ENGINE_BRIDGE_REQUEST_FORMAT:
        raise EngineBridgeError(
            f"Unexpected engine bridge format {raw.get('format')!r} at {path}; "
            f"expected {ENGINE_BRIDGE_REQUEST_FORMAT!r}."
        )
    if raw.get("format_version") != ENGINE_BRIDGE_VERSION:
        raise EngineBridgeError(
            f"Unknown engine bridge format_version {raw.get('format_version')!r} "
            f"at {path}; this build only understands {ENGINE_BRIDGE_VERSION!r}."
        )
    for field_name in ("action", "project", "build", "runtime", "artifacts"):
        if field_name not in raw:
            raise EngineBridgeError(
                f"Engine bridge request at {path} is missing required field {field_name!r}."
            )


def _response(
    *,
    action: str,
    status: str,
    summary_text: str,
    rom_path: Path,
    rom_sha256: str,
    artifacts: dict[str, object],
    result: dict[str, object] | None,
    error: dict[str, object] | None,
) -> dict[str, object]:
    return {
        "format": ENGINE_BRIDGE_RESPONSE_FORMAT,
        "format_version": ENGINE_BRIDGE_VERSION,
        "action": action,
        "status": status,
        "summary": summary_text,
        "rom": {
            "path": str(rom_path),
            "sha256": rom_sha256,
        },
        "artifacts": artifacts,
        "result": result,
        "error": error,
    }


def _require_dict(payload: dict[str, object], field_name: str, *, context: str) -> dict[str, object]:
    value = payload.get(field_name)
    if not isinstance(value, dict):
        raise EngineBridgeError(f"{context} field {field_name!r} must be an object")
    return value


def _require_str(payload: dict[str, object], field_name: str, *, context: str) -> str:
    value = payload.get(field_name)
    if not isinstance(value, str) or not value:
        raise EngineBridgeError(f"{context} field {field_name!r} must be a non-empty string")
    return value


def _optional_str(payload: dict[str, object], field_name: str) -> str | None:
    value = payload.get(field_name)
    if value is None:
        return None
    if not isinstance(value, str) or not value:
        raise EngineBridgeError(f"field {field_name!r} must be a non-empty string when present")
    return value


def _optional_path_str(payload: dict[str, object], field_name: str) -> str | None:
    value = _optional_str(payload, field_name)
    return value


def _optional_int(payload: dict[str, object], field_name: str) -> int | None:
    value = payload.get(field_name)
    if value is None:
        return None
    if not isinstance(value, int):
        raise EngineBridgeError(f"field {field_name!r} must be an integer when present")
    return value
