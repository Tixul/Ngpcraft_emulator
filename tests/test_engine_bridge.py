"""Tests for the engine-facing bridge request handler."""

from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from core.engine_bridge import (
    ENGINE_BRIDGE_REQUEST_FORMAT,
    ENGINE_BRIDGE_VERSION,
    EngineBridgeError,
    execute_engine_bridge_request,
)
from ngpc_emu import main


class EngineBridgeTests(unittest.TestCase):
    def _write_demo_rom(self, path: Path, entry_point: int, body: bytes) -> None:
        data = bytearray(0x40)
        data[0x00:0x1C] = b"LICENSED BY SNK CORPORATION".ljust(0x1C, b"\x00")
        data[0x1C:0x20] = entry_point.to_bytes(4, "little")
        data[0x20:0x22] = (0x0000).to_bytes(2, "little")
        data[0x22] = 0
        data[0x23] = 0x10
        data[0x24:0x30] = b"TEST GAME\x00\x00\x00"
        body_offset = entry_point - 0x00200000
        if body_offset < len(data):
            data[body_offset : body_offset + len(body)] = body
        else:
            data.extend(b"\x00" * (body_offset - len(data)))
            data.extend(body)
        path.write_bytes(bytes(data))

    def _write_request(self, path: Path, payload: dict[str, object]) -> None:
        path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    def _base_request(self, rom_path: Path, action: str) -> dict[str, object]:
        return {
            "format": ENGINE_BRIDGE_REQUEST_FORMAT,
            "format_version": ENGINE_BRIDGE_VERSION,
            "action": action,
            "project": {
                "project_root": str(rom_path.parent),
                "project_name": "demo",
                "invoker": "NgpCraft_engine",
                "invoker_version": "test",
            },
            "build": {
                "rom_path": str(rom_path),
                "rom_sha256": None,
                "map_path": None,
                "symbols_available": False,
            },
            "runtime": {
                "start_mode": "bootstrap",
                "seed_from_savestate": None,
                "seed_registers": {"XIZ": 0},
                "seed_xsp": 0x6C00,
                "target_pc": None,
                "max_steps": 4,
            },
            "artifacts": {
                "workspace_dir": str(rom_path.parent / ".ngpc_emu"),
                "event_log_path": str(rom_path.parent / ".ngpc_emu" / "last.eventlog.json"),
                "savestate_path": str(rom_path.parent / ".ngpc_emu" / "last.state.json"),
                "trace_path": None,
                "capture_dir": str(rom_path.parent / ".ngpc_emu" / "captures"),
            },
            "ui": {
                "focus_symbol": None,
                "focus_scene": None,
                "focus_asset_kind": None,
                "focus_asset_id": None,
            },
            "note": "test",
        }

    def test_execute_capture_eventlog_writes_requested_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            req_path = Path(tmpdir) / "bridge_request.json"
            self._write_demo_rom(rom_path, 0x00200040, b"\x00\x00")
            request = self._base_request(rom_path, "capture-eventlog")
            request["runtime"]["max_steps"] = 2  # type: ignore[index]
            self._write_request(req_path, request)

            response = execute_engine_bridge_request(req_path)

            self.assertEqual(response["status"], "ok")
            event_log_path = Path(request["artifacts"]["event_log_path"])  # type: ignore[index]
            self.assertTrue(event_log_path.exists())
            payload = json.loads(event_log_path.read_text(encoding="utf-8"))
            self.assertEqual(payload["format"], "ngpc-emu-event-log")
            self.assertEqual(payload["summary"]["executed_count"], 2)

    def test_execute_capture_savestate_writes_requested_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            req_path = Path(tmpdir) / "bridge_request.json"
            self._write_demo_rom(rom_path, 0x00200040, b"\x00")
            request = self._base_request(rom_path, "capture-savestate")
            self._write_request(req_path, request)

            response = execute_engine_bridge_request(req_path)

            self.assertEqual(response["status"], "ok")
            savestate_path = Path(request["artifacts"]["savestate_path"])  # type: ignore[index]
            self.assertTrue(savestate_path.exists())
            payload = json.loads(savestate_path.read_text(encoding="utf-8"))
            self.assertEqual(payload["format"], "ngpc-emu-savestate")

    def test_execute_capture_savestate_accepts_control_register_seeds(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            req_path = Path(tmpdir) / "bridge_request.json"
            self._write_demo_rom(rom_path, 0x00200040, b"\x00")
            request = self._base_request(rom_path, "capture-savestate")
            request["runtime"]["seed_registers"] = {"DMAC0": 0x12345678}  # type: ignore[index]
            self._write_request(req_path, request)

            response = execute_engine_bridge_request(req_path)

            self.assertEqual(response["status"], "ok")
            savestate_path = Path(request["artifacts"]["savestate_path"])  # type: ignore[index]
            payload = json.loads(savestate_path.read_text(encoding="utf-8"))
            self.assertEqual(payload["cpu"]["control_registers"]["dmac"][0], 0x5678)

    def test_execute_capture_savestate_accepts_seed_presets(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            req_path = Path(tmpdir) / "bridge_request.json"
            self._write_demo_rom(rom_path, 0x00200040, b"\x00")
            request = self._base_request(rom_path, "capture-savestate")
            request["runtime"]["seed_registers"] = None  # type: ignore[index]
            request["runtime"]["seed_xsp"] = None  # type: ignore[index]
            request["runtime"]["seed_presets"] = ["bios-handoff-minimal"]  # type: ignore[index]
            self._write_request(req_path, request)

            response = execute_engine_bridge_request(req_path)

            self.assertEqual(response["status"], "ok")
            savestate_path = Path(request["artifacts"]["savestate_path"])  # type: ignore[index]
            payload = json.loads(savestate_path.read_text(encoding="utf-8"))
            self.assertEqual(payload["cpu"]["registers"]["xsp"], 0x00006C00)
            self.assertEqual(payload["cpu"]["control_registers"]["intnest"], 0)

    def test_execute_capture_eventlog_seed_preset_can_unblock_intnest_read(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            req_path = Path(tmpdir) / "bridge_request.json"
            self._write_demo_rom(rom_path, 0x00200040, b"\xD8\x2F\x30")
            request = self._base_request(rom_path, "capture-eventlog")
            request["runtime"]["seed_registers"] = {"XWA": 0}  # type: ignore[index]
            request["runtime"]["seed_xsp"] = None  # type: ignore[index]
            request["runtime"]["seed_presets"] = ["bios-handoff-minimal"]  # type: ignore[index]
            request["runtime"]["max_steps"] = 1  # type: ignore[index]
            self._write_request(req_path, request)

            response = execute_engine_bridge_request(req_path)

            self.assertEqual(response["status"], "ok")
            event_log_path = Path(request["artifacts"]["event_log_path"])  # type: ignore[index]
            payload = json.loads(event_log_path.read_text(encoding="utf-8"))
            self.assertEqual(payload["summary"]["executed_count"], 1)
            self.assertEqual(payload["events"][0]["assembly"], "ldc WA, INTNEST")
            self.assertEqual(payload["run_context"]["seed_registers"]["INTNEST"], 0)
            self.assertEqual(payload["run_context"]["seed_registers"]["XWA"], 0)

    def test_execute_capture_savestate_seed_xsp_overrides_seed_preset_xsp(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            req_path = Path(tmpdir) / "bridge_request.json"
            self._write_demo_rom(rom_path, 0x00200040, b"\x00")
            request = self._base_request(rom_path, "capture-savestate")
            request["runtime"]["seed_registers"] = None  # type: ignore[index]
            request["runtime"]["seed_xsp"] = 0x00004100  # type: ignore[index]
            request["runtime"]["seed_presets"] = ["bios-handoff-minimal"]  # type: ignore[index]
            self._write_request(req_path, request)

            response = execute_engine_bridge_request(req_path)

            self.assertEqual(response["status"], "ok")
            savestate_path = Path(request["artifacts"]["savestate_path"])  # type: ignore[index]
            payload = json.loads(savestate_path.read_text(encoding="utf-8"))
            self.assertEqual(payload["cpu"]["registers"]["xsp"], 0x00004100)
            self.assertEqual(payload["cpu"]["control_registers"]["intnest"], 0)

    def test_execute_capture_eventlog_seed_register_override_beats_seed_preset(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            req_path = Path(tmpdir) / "bridge_request.json"
            self._write_demo_rom(rom_path, 0x00200040, b"\xD8\x2F\x30")
            request = self._base_request(rom_path, "capture-eventlog")
            request["runtime"]["seed_registers"] = {"XWA": 0, "INTNEST": 0x1234}  # type: ignore[index]
            request["runtime"]["seed_xsp"] = None  # type: ignore[index]
            request["runtime"]["seed_presets"] = ["bios-handoff-minimal"]  # type: ignore[index]
            request["runtime"]["max_steps"] = 1  # type: ignore[index]
            self._write_request(req_path, request)

            response = execute_engine_bridge_request(req_path)

            self.assertEqual(response["status"], "ok")
            event_log_path = Path(request["artifacts"]["event_log_path"])  # type: ignore[index]
            payload = json.loads(event_log_path.read_text(encoding="utf-8"))
            self.assertEqual(payload["summary"]["executed_count"], 1)
            self.assertEqual(payload["events"][0]["assembly"], "ldc WA, INTNEST")
            self.assertEqual(payload["run_context"]["seed_registers"]["INTNEST"], 0x1234)
            self.assertEqual(payload["run_context"]["seed_registers"]["XWA"], 0)

    def test_execute_capture_eventlog_seed_xsp_override_is_recorded_as_seed_xsp(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            req_path = Path(tmpdir) / "bridge_request.json"
            self._write_demo_rom(rom_path, 0x00200040, b"\x00")
            request = self._base_request(rom_path, "capture-eventlog")
            request["runtime"]["seed_registers"] = None  # type: ignore[index]
            request["runtime"]["seed_xsp"] = 0x00004100  # type: ignore[index]
            request["runtime"]["seed_presets"] = ["bios-handoff-minimal"]  # type: ignore[index]
            request["runtime"]["max_steps"] = 1  # type: ignore[index]
            self._write_request(req_path, request)

            response = execute_engine_bridge_request(req_path)

            self.assertEqual(response["status"], "ok")
            event_log_path = Path(request["artifacts"]["event_log_path"])  # type: ignore[index]
            payload = json.loads(event_log_path.read_text(encoding="utf-8"))
            self.assertEqual(payload["summary"]["executed_count"], 1)
            self.assertEqual(payload["run_context"]["seed_xsp"], 0x00004100)
            self.assertEqual(payload["run_context"]["seed_registers"]["INTNEST"], 0)
            self.assertNotIn("XSP", payload["run_context"]["seed_registers"])

    def test_execute_request_rejects_unknown_seed_preset(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            req_path = Path(tmpdir) / "bridge_request.json"
            self._write_demo_rom(rom_path, 0x00200040, b"\x00")
            request = self._base_request(rom_path, "smoke-run")
            request["runtime"]["seed_presets"] = ["unknown-preset"]  # type: ignore[index]
            self._write_request(req_path, request)

            with self.assertRaisesRegex(EngineBridgeError, "runtime.seed_presets"):
                execute_engine_bridge_request(req_path)

    def test_execute_run_returns_partial_and_emits_headless_fallback_log(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            req_path = Path(tmpdir) / "bridge_request.json"
            self._write_demo_rom(rom_path, 0x00200040, b"\x00")
            request = self._base_request(rom_path, "run")
            request["runtime"]["max_steps"] = 1  # type: ignore[index]
            self._write_request(req_path, request)

            response = execute_engine_bridge_request(req_path)

            self.assertEqual(response["status"], "partial")
            event_log_path = Path(request["artifacts"]["event_log_path"])  # type: ignore[index]
            self.assertTrue(event_log_path.exists())

    def test_execute_request_rejects_rom_hash_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            req_path = Path(tmpdir) / "bridge_request.json"
            self._write_demo_rom(rom_path, 0x00200040, b"\x00")
            request = self._base_request(rom_path, "smoke-run")
            request["build"]["rom_sha256"] = "00" * 32  # type: ignore[index]
            self._write_request(req_path, request)

            with self.assertRaisesRegex(EngineBridgeError, "ROM hash mismatch"):
                execute_engine_bridge_request(req_path)

    def test_cli_engine_bridge_prints_structured_json_response(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            req_path = Path(tmpdir) / "bridge_request.json"
            self._write_demo_rom(rom_path, 0x00200040, b"\x00")
            request = self._base_request(rom_path, "smoke-run")
            request["runtime"]["max_steps"] = 1  # type: ignore[index]
            self._write_request(req_path, request)

            stdout = io.StringIO()
            with redirect_stdout(stdout):
                exit_code = main(["engine-bridge", str(req_path)])

            payload = json.loads(stdout.getvalue())
            self.assertEqual(exit_code, 0)
            self.assertEqual(payload["status"], "ok")
            self.assertEqual(payload["action"], "smoke-run")

    def test_execute_request_accepts_utf8_bom_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            req_path = Path(tmpdir) / "bridge_request.json"
            self._write_demo_rom(rom_path, 0x00200040, b"\x00")
            request = self._base_request(rom_path, "smoke-run")
            req_path.write_text(json.dumps(request, indent=2), encoding="utf-8-sig")

            response = execute_engine_bridge_request(req_path)

            self.assertEqual(response["status"], "ok")

    # --- map_path enrichment ---------------------------------------------

    _MAP_BODY = (
        "# t900ld synthetic map\n"
        "\n"
        "=== Public symbols ===\n"
        "  __startup                0x00200040\n"
        "  _main                    0x00200200\n"
    )

    def _write_map(self, path: Path) -> None:
        path.write_text(self._MAP_BODY, encoding="utf-8")

    def test_capture_eventlog_without_map_path_omits_symbol_block(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            req_path = Path(tmpdir) / "bridge_request.json"
            self._write_demo_rom(rom_path, 0x00200040, b"\x00")
            req = self._base_request(rom_path, "capture-eventlog")
            self._write_request(req_path, req)

            response = execute_engine_bridge_request(req_path)

            self.assertEqual(response["status"], "ok")
            result = response["result"]
            assert isinstance(result, dict)
            self.assertNotIn("final_symbol", result)
            self.assertNotIn("event_log_profile_excerpt", result)

    def test_capture_eventlog_with_map_path_enriches_with_symbol_and_profile(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            map_path = Path(tmpdir) / "demo.map"
            req_path = Path(tmpdir) / "bridge_request.json"
            self._write_demo_rom(rom_path, 0x00200040, b"\x00")
            self._write_map(map_path)
            req = self._base_request(rom_path, "capture-eventlog")
            req["build"]["map_path"] = str(map_path)
            req["build"]["symbols_available"] = True
            self._write_request(req_path, req)

            response = execute_engine_bridge_request(req_path)

            self.assertEqual(response["status"], "ok")
            result = response["result"]
            assert isinstance(result, dict)
            self.assertIn("final_symbol", result)
            fs = result["final_symbol"]
            assert isinstance(fs, dict)
            self.assertTrue(fs["found"])
            self.assertEqual(fs["owning_symbol"], "__startup")

            self.assertIn("event_log_profile_excerpt", result)
            profile = result["event_log_profile_excerpt"]
            assert isinstance(profile, dict)
            self.assertEqual(profile["distinct_symbols"], 1)
            self.assertEqual(profile["unresolved_events"], 0)
            self.assertEqual(profile["top_n"], 5)
            top = profile["top_buckets"]
            assert isinstance(top, list)
            self.assertEqual(len(top), 1)
            self.assertEqual(top[0]["symbol"], "__startup")

    def test_capture_savestate_with_map_path_enriches_with_symbol_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            map_path = Path(tmpdir) / "demo.map"
            req_path = Path(tmpdir) / "bridge_request.json"
            self._write_demo_rom(rom_path, 0x00200040, b"\x00")
            self._write_map(map_path)
            req = self._base_request(rom_path, "capture-savestate")
            req["build"]["map_path"] = str(map_path)
            req["build"]["symbols_available"] = True
            self._write_request(req_path, req)

            response = execute_engine_bridge_request(req_path)

            self.assertEqual(response["status"], "ok")
            result = response["result"]
            assert isinstance(result, dict)
            self.assertIn("final_symbol", result)
            self.assertEqual(result["final_symbol"]["owning_symbol"], "__startup")
            # capture-savestate does not produce an event log; no profile excerpt
            self.assertNotIn("event_log_profile_excerpt", result)

    def test_smoke_run_with_map_path_enriches_both_blocks(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            map_path = Path(tmpdir) / "demo.map"
            req_path = Path(tmpdir) / "bridge_request.json"
            self._write_demo_rom(rom_path, 0x00200040, b"\x00")
            self._write_map(map_path)
            req = self._base_request(rom_path, "smoke-run")
            req["build"]["map_path"] = str(map_path)
            self._write_request(req_path, req)

            response = execute_engine_bridge_request(req_path)

            self.assertEqual(response["status"], "ok")
            result = response["result"]
            assert isinstance(result, dict)
            self.assertIn("final_symbol", result)
            self.assertIn("event_log_profile_excerpt", result)

    def test_invalid_map_path_raises_engine_bridge_error_on_load(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            req_path = Path(tmpdir) / "bridge_request.json"
            self._write_demo_rom(rom_path, 0x00200040, b"\x00")
            req = self._base_request(rom_path, "smoke-run")
            req["build"]["map_path"] = "/no/such/file.map"
            self._write_request(req_path, req)

            # load_map raises FileNotFoundError; the CLI wraps it but
            # execute_engine_bridge_request lets it surface directly.
            with self.assertRaises(FileNotFoundError):
                execute_engine_bridge_request(req_path)


class EngineBridgeRenderScreenshotTests(unittest.TestCase):
    """Pass 28 — `render-screenshot` action wires the renderer into the bridge.

    Standalone class (no inheritance from `EngineBridgeTests`) so the
    11 base tests aren't duplicated by unittest discovery — only the
    4 render-specific tests in this class count toward the project total.
    """

    # Helpers duplicated from EngineBridgeTests intentionally — see class
    # docstring. Extracting a TestCase-less mixin would also work; for 3
    # short methods the inline copy stays cheaper.
    _write_demo_rom = EngineBridgeTests._write_demo_rom
    _write_request = EngineBridgeTests._write_request
    _base_request = EngineBridgeTests._base_request

    def _render_request(
        self, rom_path: Path, *, screenshot_path: Path,
        seed_from: Path | None = None,
    ) -> dict[str, object]:
        req = self._base_request(rom_path, "render-screenshot")
        req["artifacts"]["screenshot_path"] = str(screenshot_path)
        # Render doesn't need event-log / savestate artifacts.
        req["artifacts"]["event_log_path"] = None
        req["artifacts"]["savestate_path"] = None
        if seed_from is not None:
            req["runtime"]["start_mode"] = "savestate"
            req["runtime"]["seed_from_savestate"] = str(seed_from)
        return req

    def test_bootstrap_render_produces_ppm_and_response_shape(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            req_path = Path(tmpdir) / "bridge_request.json"
            screenshot_path = Path(tmpdir) / "out.ppm"
            self._write_demo_rom(rom_path, 0x00200040, b"\x00\x00")

            req = self._render_request(rom_path, screenshot_path=screenshot_path)
            self._write_request(req_path, req)

            response = execute_engine_bridge_request(req_path)
            self.assertEqual(response["status"], "ok")
            self.assertEqual(response["action"], "render-screenshot")
            self.assertEqual(
                response["artifacts"]["screenshot_path"], str(screenshot_path),
            )
            # PPM file produced.
            self.assertTrue(screenshot_path.exists())
            data = screenshot_path.read_bytes()
            self.assertTrue(data.startswith(b"P6\n160 152\n255\n"))
            # Result block carries the screenshot diagnostics.
            result = response["result"]
            self.assertEqual(result["stop_reason"], "frame-rendered")
            self.assertEqual(result["executed_count"], 0)
            shot = result["screenshot"]
            self.assertEqual(shot["width"], 160)
            self.assertEqual(shot["height"], 152)
            self.assertEqual(shot["ppm_byte_count"], len(data))
            self.assertEqual(len(shot["ppm_sha256"]), 64)
            self.assertEqual(shot["renderer_pass"], "1.3")
            # Control snapshot exposes all 6 sub-blocks.
            ctrl = shot["control_snapshot"]
            self.assertIn("window", ctrl)
            self.assertIn("scroll_prio", ctrl)
            self.assertIn("scroll_offsets", ctrl)
            self.assertIn("sprite_offset", ctrl)
            self.assertIn("twod_control", ctrl)
            self.assertIn("backdrop_control", ctrl)
            self.assertIn("mode", ctrl)

    def test_savestate_render_layers_overlay(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            rom_path = tmp / "demo.ngc"
            req_path = tmp / "bridge_request.json"
            state_path = tmp / "seed.state.json"
            screenshot_path = tmp / "out.ppm"
            self._write_demo_rom(rom_path, 0x00200040, b"\x00\x00")

            # Build a savestate that flips BGC + backdrop slot 0 to red.
            from core.machine import load_machine_state
            from core.savestate import build_savestate_payload, save_savestate
            machine = load_machine_state(rom_path)
            save_savestate(
                state_path,
                build_savestate_payload(
                    rom_path=rom_path,
                    rom_header=machine.header,
                    cpu=machine.cpu,
                    writable_overlay={
                        0x008118: 0x80,         # BGC bit 7 = enabled
                        0x0083E0: 0x0F,         # backdrop slot 0 low: r=15
                        0x0083E1: 0x00,
                    },
                ),
            )

            req = self._render_request(
                rom_path,
                screenshot_path=screenshot_path,
                seed_from=state_path,
            )
            self._write_request(req_path, req)
            response = execute_engine_bridge_request(req_path)
            self.assertEqual(response["status"], "ok")
            shot = response["result"]["screenshot"]
            self.assertEqual(shot["backdrop_color"]["hex_rgb24"], "#FF0000")
            self.assertTrue(
                shot["control_snapshot"]["backdrop_control"]["bgc_enabled"]
            )

    def test_render_screenshot_missing_path_returns_bridge_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            req_path = Path(tmpdir) / "bridge_request.json"
            self._write_demo_rom(rom_path, 0x00200040, b"\x00\x00")

            req = self._base_request(rom_path, "render-screenshot")
            # Deliberately omit screenshot_path.
            self._write_request(req_path, req)
            with self.assertRaises(EngineBridgeError):
                execute_engine_bridge_request(req_path)

    def test_render_via_cli_engine_bridge_command(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            rom_path = tmp / "demo.ngc"
            req_path = tmp / "bridge_request.json"
            screenshot_path = tmp / "out.ppm"
            self._write_demo_rom(rom_path, 0x00200040, b"\x00\x00")
            req = self._render_request(rom_path, screenshot_path=screenshot_path)
            self._write_request(req_path, req)

            stdout = io.StringIO()
            with redirect_stdout(stdout):
                exit_code = main(["engine-bridge", str(req_path)])
            self.assertEqual(exit_code, 0)
            payload = json.loads(stdout.getvalue())
            self.assertEqual(payload["status"], "ok")
            self.assertEqual(payload["action"], "render-screenshot")
            self.assertTrue(screenshot_path.exists())


class EngineBridgeRenderTileAtlasTests(unittest.TestCase):
    """Pass 29 — `render-tile-atlas` action wires the atlas inspector into the bridge.

    Standalone TestCase (no inheritance) so unittest discovery doesn't
    re-run the 11 base `EngineBridgeTests` cases.
    """

    _write_demo_rom = EngineBridgeTests._write_demo_rom
    _write_request = EngineBridgeTests._write_request
    _base_request = EngineBridgeTests._base_request

    def _atlas_request(
        self,
        rom_path: Path,
        *,
        tile_atlas_path: Path,
        atlas_params: dict | None = None,
        seed_from: Path | None = None,
    ) -> dict[str, object]:
        req = self._base_request(rom_path, "render-tile-atlas")
        req["artifacts"]["tile_atlas_path"] = str(tile_atlas_path)
        req["artifacts"]["event_log_path"] = None
        req["artifacts"]["savestate_path"] = None
        if atlas_params is not None:
            req["runtime"]["atlas"] = atlas_params
        if seed_from is not None:
            req["runtime"]["start_mode"] = "savestate"
            req["runtime"]["seed_from_savestate"] = str(seed_from)
        return req

    def test_default_full_atlas_produces_128x256_ppm(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            rom_path = tmp / "demo.ngc"
            req_path = tmp / "bridge_request.json"
            atlas_path = tmp / "atlas.ppm"
            self._write_demo_rom(rom_path, 0x00200040, b"\x00\x00")

            req = self._atlas_request(rom_path, tile_atlas_path=atlas_path)
            self._write_request(req_path, req)
            response = execute_engine_bridge_request(req_path)

            self.assertEqual(response["status"], "ok")
            self.assertEqual(response["action"], "render-tile-atlas")
            self.assertEqual(
                response["artifacts"]["tile_atlas_path"], str(atlas_path),
            )
            self.assertTrue(atlas_path.exists())
            data = atlas_path.read_bytes()
            self.assertTrue(data.startswith(b"P6\n128 256\n255\n"))
            atlas = response["result"]["tile_atlas"]
            self.assertEqual(atlas["width"], 128)
            self.assertEqual(atlas["height"], 256)
            self.assertEqual(atlas["tile_count"], 512)
            self.assertEqual(atlas["cols"], 16)
            self.assertEqual(atlas["rows"], 32)
            self.assertEqual(atlas["colorisation"], "grayscale")
            self.assertIsNone(atlas["palette"])
            self.assertEqual(len(atlas["ppm_sha256"]), 64)

    def test_custom_range_and_cols(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            rom_path = tmp / "demo.ngc"
            req_path = tmp / "bridge_request.json"
            atlas_path = tmp / "atlas.ppm"
            self._write_demo_rom(rom_path, 0x00200040, b"\x00\x00")

            req = self._atlas_request(
                rom_path,
                tile_atlas_path=atlas_path,
                atlas_params={"tile_range": [0, 15], "cols": 4},
            )
            self._write_request(req_path, req)
            response = execute_engine_bridge_request(req_path)

            self.assertEqual(response["status"], "ok")
            atlas = response["result"]["tile_atlas"]
            self.assertEqual(atlas["tile_count"], 16)
            self.assertEqual(atlas["cols"], 4)
            self.assertEqual(atlas["rows"], 4)
            self.assertEqual(atlas["width"], 32)
            self.assertEqual(atlas["height"], 32)
            self.assertEqual(atlas["first_tile"], 0)
            self.assertEqual(atlas["last_tile"], 15)

    def test_palette_colorisation_returns_palette_payload(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            rom_path = tmp / "demo.ngc"
            req_path = tmp / "bridge_request.json"
            state_path = tmp / "seed.state.json"
            atlas_path = tmp / "atlas.ppm"
            self._write_demo_rom(rom_path, 0x00200040, b"\x00\x00")

            from core.machine import load_machine_state
            from core.savestate import build_savestate_payload, save_savestate
            machine = load_machine_state(rom_path)
            save_savestate(
                state_path,
                build_savestate_payload(
                    rom_path=rom_path,
                    rom_header=machine.header,
                    cpu=machine.cpu,
                    writable_overlay={
                        # SCR1 palette 0 color 1 = pure red (raw 0x000F).
                        0x008282: 0x0F, 0x008283: 0x00,
                    },
                ),
            )

            req = self._atlas_request(
                rom_path,
                tile_atlas_path=atlas_path,
                atlas_params={
                    "tile_range": [0, 3],
                    "cols": 2,
                    "palette_plane": "scr1",
                    "palette_index": 0,
                },
                seed_from=state_path,
            )
            self._write_request(req_path, req)
            response = execute_engine_bridge_request(req_path)

            self.assertEqual(response["status"], "ok")
            atlas = response["result"]["tile_atlas"]
            self.assertEqual(atlas["colorisation"], "palette")
            self.assertEqual(atlas["palette_plane"], "scr1")
            self.assertEqual(atlas["palette_index"], 0)
            self.assertEqual(atlas["palette"]["plane"], "scr1")
            self.assertEqual(
                atlas["palette"]["colors"][1]["hex_rgb24"], "#FF0000",
            )

    def test_missing_tile_atlas_path_raises(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            rom_path = tmp / "demo.ngc"
            req_path = tmp / "bridge_request.json"
            self._write_demo_rom(rom_path, 0x00200040, b"\x00\x00")

            req = self._base_request(rom_path, "render-tile-atlas")
            self._write_request(req_path, req)
            with self.assertRaises(EngineBridgeError):
                execute_engine_bridge_request(req_path)

    def test_invalid_tile_range_raises(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            rom_path = tmp / "demo.ngc"
            req_path = tmp / "bridge_request.json"
            atlas_path = tmp / "atlas.ppm"
            self._write_demo_rom(rom_path, 0x00200040, b"\x00\x00")

            # Reversed range.
            req = self._atlas_request(
                rom_path,
                tile_atlas_path=atlas_path,
                atlas_params={"tile_range": [10, 3]},
            )
            self._write_request(req_path, req)
            with self.assertRaises(EngineBridgeError):
                execute_engine_bridge_request(req_path)

    def test_palette_plane_without_index_raises(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            rom_path = tmp / "demo.ngc"
            req_path = tmp / "bridge_request.json"
            atlas_path = tmp / "atlas.ppm"
            self._write_demo_rom(rom_path, 0x00200040, b"\x00\x00")

            req = self._atlas_request(
                rom_path,
                tile_atlas_path=atlas_path,
                atlas_params={"palette_plane": "scr1"},
            )
            self._write_request(req_path, req)
            with self.assertRaises(EngineBridgeError):
                execute_engine_bridge_request(req_path)


class EngineBridgeCheckFrameGoldenAllTests(unittest.TestCase):
    """Pass 30 — `check-frame-golden-all` action runs the visual regression
    batch from the engine bridge for CI integration.

    Standalone TestCase (no inheritance) so unittest discovery doesn't
    re-run the 11 base `EngineBridgeTests` cases.
    """

    _write_demo_rom = EngineBridgeTests._write_demo_rom
    _write_request = EngineBridgeTests._write_request
    _base_request = EngineBridgeTests._base_request

    def _check_request(
        self,
        rom_path: Path,
        *,
        stop_on_fail: bool = False,
        save_current: Path | None = None,
    ) -> dict[str, object]:
        req = self._base_request(rom_path, "check-frame-golden-all")
        req["artifacts"]["event_log_path"] = None
        req["artifacts"]["savestate_path"] = None
        if save_current is not None:
            req["artifacts"]["screenshot_path"] = str(save_current)
        if stop_on_fail:
            req["runtime"]["stop_on_fail"] = True
        return req

    def _save_frame_golden(self, rom_path: Path, name: str) -> None:
        """Use the CLI to create a frame golden under the ROM-local registry."""
        with redirect_stdout(io.StringIO()):
            exit_code = main(["frame", "golden-save", str(rom_path), name])
        self.assertEqual(exit_code, 0)

    def test_empty_registry_returns_ok_with_zero_total(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            req_path = Path(tmpdir) / "bridge_request.json"
            self._write_demo_rom(rom_path, 0x00200040, b"\x00\x00")

            req = self._check_request(rom_path)
            self._write_request(req_path, req)
            response = execute_engine_bridge_request(req_path)

            self.assertEqual(response["status"], "ok")
            self.assertEqual(response["action"], "check-frame-golden-all")
            self.assertIsNone(response["error"])
            check = response["result"]["frame_goldens_check"]
            self.assertEqual(check["total"], 0)
            self.assertEqual(check["passed"], 0)
            self.assertEqual(check["failed"], 0)
            self.assertTrue(check["all_equal"])
            self.assertEqual(check["results"], [])

    def test_all_matching_goldens_return_ok(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            req_path = Path(tmpdir) / "bridge_request.json"
            self._write_demo_rom(rom_path, 0x00200040, b"\x00\x00")

            self._save_frame_golden(rom_path, "alpha")
            self._save_frame_golden(rom_path, "beta")

            req = self._check_request(rom_path)
            self._write_request(req_path, req)
            response = execute_engine_bridge_request(req_path)

            self.assertEqual(response["status"], "ok")
            self.assertIsNone(response["error"])
            check = response["result"]["frame_goldens_check"]
            self.assertEqual(check["total"], 2)
            self.assertEqual(check["passed"], 2)
            self.assertEqual(check["failed"], 0)
            self.assertTrue(check["all_equal"])
            statuses = {r["name"]: r["status"] for r in check["results"]}
            self.assertEqual(statuses, {"alpha": "match", "beta": "match"})

    def test_corrupted_golden_returns_error_status(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            req_path = Path(tmpdir) / "bridge_request.json"
            self._write_demo_rom(rom_path, 0x00200040, b"\x00\x00")

            self._save_frame_golden(rom_path, "a")
            self._save_frame_golden(rom_path, "b")
            self._save_frame_golden(rom_path, "c")

            from core.frame_goldens import frame_golden_paths_for_rom
            ppm_path, _ = frame_golden_paths_for_rom(rom_path, "b")
            data = bytearray(ppm_path.read_bytes())
            # Flip a single body byte to force a pixel diff.
            data[20] = (data[20] + 1) & 0xFF
            ppm_path.write_bytes(bytes(data))

            req = self._check_request(rom_path)
            self._write_request(req_path, req)
            response = execute_engine_bridge_request(req_path)

            self.assertEqual(response["status"], "error")
            self.assertIsNotNone(response["error"])
            self.assertEqual(
                response["error"]["type"], "frame-golden-mismatch",
            )
            check = response["result"]["frame_goldens_check"]
            self.assertEqual(check["total"], 3)
            self.assertEqual(check["checked"], 3)
            self.assertEqual(check["passed"], 2)
            self.assertEqual(check["failed"], 1)
            self.assertFalse(check["all_equal"])
            self.assertFalse(check["stopped_early"])
            statuses = {r["name"]: r["status"] for r in check["results"]}
            self.assertEqual(statuses, {"a": "match", "b": "diff", "c": "match"})

    def test_stop_on_fail_short_circuits(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            req_path = Path(tmpdir) / "bridge_request.json"
            self._write_demo_rom(rom_path, 0x00200040, b"\x00\x00")

            self._save_frame_golden(rom_path, "a")
            self._save_frame_golden(rom_path, "b")
            self._save_frame_golden(rom_path, "c")

            from core.frame_goldens import frame_golden_paths_for_rom
            ppm_path, _ = frame_golden_paths_for_rom(rom_path, "b")
            data = bytearray(ppm_path.read_bytes())
            data[20] = (data[20] + 1) & 0xFF
            ppm_path.write_bytes(bytes(data))

            req = self._check_request(rom_path, stop_on_fail=True)
            self._write_request(req_path, req)
            response = execute_engine_bridge_request(req_path)

            self.assertEqual(response["status"], "error")
            check = response["result"]["frame_goldens_check"]
            self.assertEqual(check["total"], 3)
            self.assertEqual(check["checked"], 2)
            self.assertEqual(check["passed"], 1)
            self.assertEqual(check["failed"], 1)
            self.assertTrue(check["stopped_early"])
            checked_names = [r["name"] for r in check["results"]]
            self.assertEqual(checked_names, ["a", "b"])

    def test_screenshot_path_writes_triage_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            rom_path = tmp / "demo.ngc"
            req_path = tmp / "bridge_request.json"
            triage_path = tmp / "current.ppm"
            self._write_demo_rom(rom_path, 0x00200040, b"\x00\x00")

            self._save_frame_golden(rom_path, "alpha")

            req = self._check_request(rom_path, save_current=triage_path)
            self._write_request(req_path, req)
            response = execute_engine_bridge_request(req_path)

            self.assertEqual(response["status"], "ok")
            self.assertEqual(
                response["artifacts"]["screenshot_path"], str(triage_path),
            )
            self.assertTrue(triage_path.exists())
            self.assertTrue(triage_path.read_bytes().startswith(b"P6\n"))

    def test_invalid_stop_on_fail_type_raises(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            req_path = Path(tmpdir) / "bridge_request.json"
            self._write_demo_rom(rom_path, 0x00200040, b"\x00\x00")

            req = self._check_request(rom_path)
            req["runtime"]["stop_on_fail"] = "yes"  # not a bool
            self._write_request(req_path, req)
            with self.assertRaises(EngineBridgeError):
                execute_engine_bridge_request(req_path)


class EngineBridgeCheckEventlogGoldenAllTests(unittest.TestCase):
    """Pass 31 — `check-eventlog-golden-all` action runs the trace regression
    batch through the bridge — symmetric of pass 30 for Niveau B.

    Standalone TestCase (no inheritance from EngineBridgeTests) so unittest
    discovery doesn't re-run the 11 base cases as part of this subclass.
    """

    _write_demo_rom = EngineBridgeTests._write_demo_rom
    _write_request = EngineBridgeTests._write_request
    _base_request = EngineBridgeTests._base_request

    def _check_request(
        self,
        rom_path: Path,
        *,
        max_steps: int = 4,
        stop_on_fail: bool = False,
        save_current_path: Path | None = None,
    ) -> dict[str, object]:
        req = self._base_request(rom_path, "check-eventlog-golden-all")
        req["runtime"]["max_steps"] = max_steps
        # Clear the default seeds from `_base_request` (XIZ=0 / XSP=0x6C00):
        # the CLI `eventlog golden-save` invoked by `_save_eventlog_golden`
        # below does NOT seed registers, so bridge requests with seeds set
        # would diverge from the golden's capture config (`run_context`).
        req["runtime"]["seed_registers"] = None
        req["runtime"]["seed_xsp"] = None
        if stop_on_fail:
            req["runtime"]["stop_on_fail"] = True
        if save_current_path is not None:
            req["artifacts"]["event_log_path"] = str(save_current_path)
        else:
            req["artifacts"]["event_log_path"] = None
        req["artifacts"]["savestate_path"] = None
        return req

    def _save_eventlog_golden(
        self, rom_path: Path, name: str, count: int = 4,
    ) -> None:
        with redirect_stdout(io.StringIO()):
            exit_code = main(
                [
                    "eventlog", "golden-save", str(rom_path), name,
                    "--count", str(count),
                ],
            )
        self.assertEqual(exit_code, 0)

    def test_empty_registry_returns_ok_with_zero_total(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            req_path = Path(tmpdir) / "bridge_request.json"
            self._write_demo_rom(rom_path, 0x00200040, b"\x00\x00\x00")

            req = self._check_request(rom_path, max_steps=2)
            self._write_request(req_path, req)
            response = execute_engine_bridge_request(req_path)

            self.assertEqual(response["status"], "ok")
            self.assertEqual(response["action"], "check-eventlog-golden-all")
            self.assertIsNone(response["error"])
            check = response["result"]["eventlog_goldens_check"]
            self.assertEqual(check["total"], 0)
            self.assertEqual(check["passed"], 0)
            self.assertEqual(check["failed"], 0)
            self.assertTrue(check["all_equal"])
            self.assertEqual(check["results"], [])

    def test_two_matching_goldens_return_ok(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            req_path = Path(tmpdir) / "bridge_request.json"
            self._write_demo_rom(rom_path, 0x00200040, b"\x00\x00\x00")

            self._save_eventlog_golden(rom_path, "first", count=2)
            self._save_eventlog_golden(rom_path, "second", count=2)

            req = self._check_request(rom_path, max_steps=2)
            self._write_request(req_path, req)
            response = execute_engine_bridge_request(req_path)

            self.assertEqual(response["status"], "ok")
            self.assertIsNone(response["error"])
            check = response["result"]["eventlog_goldens_check"]
            self.assertEqual(check["total"], 2)
            self.assertEqual(check["passed"], 2)
            self.assertEqual(check["failed"], 0)
            self.assertTrue(check["all_equal"])
            statuses = {r["name"]: r["status"] for r in check["results"]}
            self.assertEqual(statuses, {"first": "match", "second": "match"})

    def test_capture_config_mismatch_reports_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            req_path = Path(tmpdir) / "bridge_request.json"
            self._write_demo_rom(rom_path, 0x00200040, b"\x00\x00\x00\x00")

            # Golden saved at count=2; check-all runs with max_steps=4.
            self._save_eventlog_golden(rom_path, "two-step", count=2)

            req = self._check_request(rom_path, max_steps=4)
            self._write_request(req_path, req)
            response = execute_engine_bridge_request(req_path)

            self.assertEqual(response["status"], "error")
            self.assertIsNotNone(response["error"])
            self.assertEqual(
                response["error"]["type"], "eventlog-golden-mismatch",
            )
            check = response["result"]["eventlog_goldens_check"]
            self.assertEqual(check["passed"], 0)
            self.assertEqual(check["failed"], 1)
            self.assertFalse(check["all_equal"])
            divergence = check["results"][0]["first_divergence"]
            # Different max_steps changes either run_context or length.
            self.assertIn(divergence["kind"], ("run_context", "length"))

    def test_stop_on_fail_short_circuits_after_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            req_path = Path(tmpdir) / "bridge_request.json"
            self._write_demo_rom(rom_path, 0x00200040, b"\x00\x00\x00\x00")

            # 'a' saved at count=4 (will match), 'b' at count=2 (mismatch
            # on length / run_context), 'c' at count=4 (would match but
            # should be skipped by stop_on_fail after 'b' fails).
            self._save_eventlog_golden(rom_path, "a", count=4)
            self._save_eventlog_golden(rom_path, "b", count=2)
            self._save_eventlog_golden(rom_path, "c", count=4)

            req = self._check_request(rom_path, max_steps=4, stop_on_fail=True)
            self._write_request(req_path, req)
            response = execute_engine_bridge_request(req_path)

            self.assertEqual(response["status"], "error")
            check = response["result"]["eventlog_goldens_check"]
            self.assertEqual(check["total"], 3)
            self.assertEqual(check["checked"], 2)
            self.assertEqual(check["passed"], 1)
            self.assertEqual(check["failed"], 1)
            self.assertTrue(check["stopped_early"])
            checked_names = [r["name"] for r in check["results"]]
            self.assertEqual(checked_names, ["a", "b"])

    def test_save_current_writes_captured_log(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            rom_path = tmp / "demo.ngc"
            req_path = tmp / "bridge_request.json"
            current_path = tmp / "current.eventlog.json"
            self._write_demo_rom(rom_path, 0x00200040, b"\x00\x00")

            self._save_eventlog_golden(rom_path, "anchor", count=2)

            req = self._check_request(
                rom_path, max_steps=2, save_current_path=current_path,
            )
            self._write_request(req_path, req)
            response = execute_engine_bridge_request(req_path)

            self.assertEqual(response["status"], "ok")
            self.assertEqual(
                response["artifacts"]["event_log_path"], str(current_path),
            )
            self.assertTrue(current_path.exists())
            current_data = json.loads(current_path.read_text(encoding="utf-8"))
            self.assertIn("format_version", current_data)

    def test_invalid_stop_on_fail_type_raises(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            req_path = Path(tmpdir) / "bridge_request.json"
            self._write_demo_rom(rom_path, 0x00200040, b"\x00\x00")

            req = self._check_request(rom_path, max_steps=2)
            req["runtime"]["stop_on_fail"] = 1  # not a bool
            self._write_request(req_path, req)
            with self.assertRaises(EngineBridgeError):
                execute_engine_bridge_request(req_path)


class EngineBridgeSaveFrameGoldenTests(unittest.TestCase):
    """Pass 32 — `save-frame-golden` action creates a registry entry
    through the bridge, closing the golden lifecycle (save + check) for
    NgpCraft_engine integration.

    Standalone TestCase to avoid unittest discovery re-running the 11
    base `EngineBridgeTests` cases.
    """

    _write_demo_rom = EngineBridgeTests._write_demo_rom
    _write_request = EngineBridgeTests._write_request
    _base_request = EngineBridgeTests._base_request

    def _save_request(
        self,
        rom_path: Path,
        *,
        name: str | None = "bridge-golden",
        label: str | None = None,
        seed_from: Path | None = None,
    ) -> dict[str, object]:
        req = self._base_request(rom_path, "save-frame-golden")
        if name is not None:
            req["golden_name"] = name
        if label is not None:
            req["golden_label"] = label
        req["artifacts"]["event_log_path"] = None
        req["artifacts"]["savestate_path"] = None
        if seed_from is not None:
            req["runtime"]["start_mode"] = "savestate"
            req["runtime"]["seed_from_savestate"] = str(seed_from)
        return req

    def test_bootstrap_save_writes_ppm_and_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            rom_path = tmp / "demo.ngc"
            req_path = tmp / "bridge_request.json"
            self._write_demo_rom(rom_path, 0x00200040, b"\x00\x00")

            req = self._save_request(rom_path, name="bootstart")
            self._write_request(req_path, req)
            response = execute_engine_bridge_request(req_path)

            self.assertEqual(response["status"], "ok")
            self.assertEqual(response["action"], "save-frame-golden")
            save = response["result"]["frame_golden_save"]
            self.assertEqual(save["name"], "bootstart")
            self.assertEqual(save["width"], 160)
            self.assertEqual(save["height"], 152)
            self.assertEqual(len(save["ppm_sha256"]), 64)
            self.assertEqual(save["renderer_pass"], "1.3")
            self.assertIsNone(save["seed_from"])
            self.assertTrue(Path(save["ppm_path"]).exists())
            self.assertTrue(Path(save["manifest_path"]).exists())
            # PPM is valid binary P6.
            self.assertTrue(
                Path(save["ppm_path"]).read_bytes().startswith(b"P6\n160 152\n255\n")
            )

    def test_label_persists_in_result_block(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            rom_path = tmp / "demo.ngc"
            req_path = tmp / "bridge_request.json"
            self._write_demo_rom(rom_path, 0x00200040, b"\x00\x00")

            req = self._save_request(
                rom_path, name="labelled", label="VBlank capture",
            )
            self._write_request(req_path, req)
            response = execute_engine_bridge_request(req_path)

            self.assertEqual(response["status"], "ok")
            save = response["result"]["frame_golden_save"]
            self.assertEqual(save["label"], "VBlank capture")
            # Manifest on disk also carries the label.
            manifest = json.loads(
                Path(save["manifest_path"]).read_text(encoding="utf-8"),
            )
            self.assertEqual(manifest["label"], "VBlank capture")

    def test_savestate_overlay_save_records_seed_from(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            rom_path = tmp / "demo.ngc"
            req_path = tmp / "bridge_request.json"
            state_path = tmp / "seed.state.json"
            self._write_demo_rom(rom_path, 0x00200040, b"\x00\x00")

            from core.machine import load_machine_state
            from core.savestate import build_savestate_payload, save_savestate
            machine = load_machine_state(rom_path)
            save_savestate(
                state_path,
                build_savestate_payload(
                    rom_path=rom_path,
                    rom_header=machine.header,
                    cpu=machine.cpu,
                    writable_overlay={
                        0x008118: 0x80,         # enable BGC
                        0x0083E0: 0x0F,         # backdrop slot 0 = red
                        0x0083E1: 0x00,
                    },
                ),
            )

            req = self._save_request(
                rom_path,
                name="red-backdrop",
                seed_from=state_path,
            )
            self._write_request(req_path, req)
            response = execute_engine_bridge_request(req_path)

            self.assertEqual(response["status"], "ok")
            save = response["result"]["frame_golden_save"]
            self.assertEqual(save["seed_from"], str(state_path))
            # Manifest persists seed_from too.
            manifest = json.loads(
                Path(save["manifest_path"]).read_text(encoding="utf-8"),
            )
            self.assertEqual(manifest["seed_from"], str(state_path))
            # Control snapshot in manifest captures the BGC-enabled state.
            self.assertTrue(
                manifest["control_snapshot"]["backdrop_control"]["bgc_enabled"]
            )

    def test_missing_golden_name_raises(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            rom_path = tmp / "demo.ngc"
            req_path = tmp / "bridge_request.json"
            self._write_demo_rom(rom_path, 0x00200040, b"\x00\x00")

            req = self._save_request(rom_path, name=None)
            self._write_request(req_path, req)
            with self.assertRaises(EngineBridgeError):
                execute_engine_bridge_request(req_path)

    def test_save_then_check_all_round_trip(self) -> None:
        """Lifecycle test — save a golden via bridge, then check-all
        confirms it matches a fresh render (proving save and check are
        symmetric)."""
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            rom_path = tmp / "demo.ngc"
            save_req_path = tmp / "save.json"
            check_req_path = tmp / "check.json"
            self._write_demo_rom(rom_path, 0x00200040, b"\x00\x00")

            # 1) save-frame-golden via bridge.
            save_req = self._save_request(rom_path, name="lifecycle")
            self._write_request(save_req_path, save_req)
            save_resp = execute_engine_bridge_request(save_req_path)
            self.assertEqual(save_resp["status"], "ok")

            # 2) check-frame-golden-all via bridge — same start mode,
            # same memory view → should match.
            check_req = self._base_request(rom_path, "check-frame-golden-all")
            check_req["artifacts"]["event_log_path"] = None
            check_req["artifacts"]["savestate_path"] = None
            self._write_request(check_req_path, check_req)
            check_resp = execute_engine_bridge_request(check_req_path)

            self.assertEqual(check_resp["status"], "ok")
            check = check_resp["result"]["frame_goldens_check"]
            self.assertEqual(check["total"], 1)
            self.assertEqual(check["passed"], 1)
            self.assertTrue(check["all_equal"])
            self.assertEqual(check["results"][0]["name"], "lifecycle")


class EngineBridgeSaveEventlogGoldenTests(unittest.TestCase):
    """Pass 33 — `save-eventlog-golden` action — symmetric of pass 32
    `save-frame-golden` for trace goldens. Closes the Niveau B
    lifecycle through the bridge."""

    _write_demo_rom = EngineBridgeTests._write_demo_rom
    _write_request = EngineBridgeTests._write_request
    _base_request = EngineBridgeTests._base_request

    def _save_request(
        self,
        rom_path: Path,
        *,
        name: str | None = "bridge-trace",
        max_steps: int = 2,
        note: str | None = None,
    ) -> dict[str, object]:
        req = self._base_request(rom_path, "save-eventlog-golden")
        if name is not None:
            req["golden_name"] = name
        if note is not None:
            req["note"] = note
        # CLI eventlog golden-save doesn't seed; clear so `save → check-all`
        # round-trip via bridge can match without `run_context` divergence.
        req["runtime"]["seed_registers"] = None
        req["runtime"]["seed_xsp"] = None
        req["runtime"]["max_steps"] = max_steps
        req["artifacts"]["event_log_path"] = None
        req["artifacts"]["savestate_path"] = None
        return req

    def test_save_writes_golden_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            rom_path = tmp / "demo.ngc"
            req_path = tmp / "bridge_request.json"
            self._write_demo_rom(rom_path, 0x00200040, b"\x00\x00")

            req = self._save_request(rom_path, name="boot-trace")
            self._write_request(req_path, req)
            response = execute_engine_bridge_request(req_path)

            self.assertEqual(response["status"], "ok")
            self.assertEqual(response["action"], "save-eventlog-golden")
            save = response["result"]["eventlog_golden_save"]
            self.assertEqual(save["name"], "boot-trace")
            self.assertTrue(Path(save["golden_path"]).exists())
            self.assertGreaterEqual(save["executed_count"], 0)

    def test_note_persists_in_saved_payload(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            rom_path = tmp / "demo.ngc"
            req_path = tmp / "bridge_request.json"
            self._write_demo_rom(rom_path, 0x00200040, b"\x00\x00")

            req = self._save_request(
                rom_path, name="annotated", note="captured VBlank ISR",
            )
            self._write_request(req_path, req)
            response = execute_engine_bridge_request(req_path)

            self.assertEqual(response["status"], "ok")
            save = response["result"]["eventlog_golden_save"]
            self.assertEqual(save["note"], "captured VBlank ISR")
            # Saved golden JSON also carries the note.
            golden_data = json.loads(
                Path(save["golden_path"]).read_text(encoding="utf-8"),
            )
            self.assertEqual(golden_data.get("note"), "captured VBlank ISR")

    def test_missing_golden_name_raises(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            rom_path = tmp / "demo.ngc"
            req_path = tmp / "bridge_request.json"
            self._write_demo_rom(rom_path, 0x00200040, b"\x00\x00")

            req = self._save_request(rom_path, name=None)
            self._write_request(req_path, req)
            with self.assertRaises(EngineBridgeError):
                execute_engine_bridge_request(req_path)

    def test_save_then_check_all_round_trip(self) -> None:
        """Lifecycle lock — save a trace golden via bridge, then
        check-all confirms it matches a fresh capture with the same
        params."""
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            rom_path = tmp / "demo.ngc"
            save_req_path = tmp / "save.json"
            check_req_path = tmp / "check.json"
            self._write_demo_rom(rom_path, 0x00200040, b"\x00\x00")

            save_req = self._save_request(rom_path, name="lifecycle",
                                          max_steps=2)
            self._write_request(save_req_path, save_req)
            save_resp = execute_engine_bridge_request(save_req_path)
            self.assertEqual(save_resp["status"], "ok")

            check_req = self._base_request(rom_path, "check-eventlog-golden-all")
            check_req["runtime"]["max_steps"] = 2
            check_req["runtime"]["seed_registers"] = None
            check_req["runtime"]["seed_xsp"] = None
            check_req["artifacts"]["event_log_path"] = None
            check_req["artifacts"]["savestate_path"] = None
            self._write_request(check_req_path, check_req)
            check_resp = execute_engine_bridge_request(check_req_path)

            self.assertEqual(check_resp["status"], "ok")
            check = check_resp["result"]["eventlog_goldens_check"]
            self.assertEqual(check["total"], 1)
            self.assertEqual(check["passed"], 1)
            self.assertTrue(check["all_equal"])
            self.assertEqual(check["results"][0]["name"], "lifecycle")


class EngineBridgeDeleteFrameGoldenTests(unittest.TestCase):
    """Pass 33 — `delete-frame-golden` action prunes a frame golden
    through the bridge."""

    _write_demo_rom = EngineBridgeTests._write_demo_rom
    _write_request = EngineBridgeTests._write_request
    _base_request = EngineBridgeTests._base_request

    def _save_frame_golden(self, rom_path: Path, name: str) -> None:
        with redirect_stdout(io.StringIO()):
            exit_code = main(["frame", "golden-save", str(rom_path), name])
        self.assertEqual(exit_code, 0)

    def _delete_request(self, rom_path: Path, name: str | None) -> dict[str, object]:
        req = self._base_request(rom_path, "delete-frame-golden")
        if name is not None:
            req["golden_name"] = name
        req["artifacts"]["event_log_path"] = None
        req["artifacts"]["savestate_path"] = None
        return req

    def test_delete_existing_golden_removes_both_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            rom_path = tmp / "demo.ngc"
            req_path = tmp / "bridge_request.json"
            self._write_demo_rom(rom_path, 0x00200040, b"\x00\x00")

            self._save_frame_golden(rom_path, "to-prune")
            from core.frame_goldens import frame_golden_paths_for_rom
            ppm_path, manifest_path = frame_golden_paths_for_rom(rom_path, "to-prune")
            self.assertTrue(ppm_path.exists())
            self.assertTrue(manifest_path.exists())

            req = self._delete_request(rom_path, "to-prune")
            self._write_request(req_path, req)
            response = execute_engine_bridge_request(req_path)

            self.assertEqual(response["status"], "ok")
            self.assertEqual(response["action"], "delete-frame-golden")
            delete = response["result"]["frame_golden_delete"]
            self.assertEqual(delete["name"], "to-prune")
            self.assertEqual(delete["deleted_ppm_path"], str(ppm_path))
            self.assertEqual(delete["deleted_manifest_path"], str(manifest_path))
            # Files removed from disk.
            self.assertFalse(ppm_path.exists())
            self.assertFalse(manifest_path.exists())

    def test_delete_missing_golden_raises(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            rom_path = tmp / "demo.ngc"
            req_path = tmp / "bridge_request.json"
            self._write_demo_rom(rom_path, 0x00200040, b"\x00\x00")

            req = self._delete_request(rom_path, "nope")
            self._write_request(req_path, req)
            with self.assertRaises(EngineBridgeError):
                execute_engine_bridge_request(req_path)

    def test_missing_golden_name_raises(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            rom_path = tmp / "demo.ngc"
            req_path = tmp / "bridge_request.json"
            self._write_demo_rom(rom_path, 0x00200040, b"\x00\x00")

            req = self._delete_request(rom_path, None)
            self._write_request(req_path, req)
            with self.assertRaises(EngineBridgeError):
                execute_engine_bridge_request(req_path)


class EngineBridgeDeleteEventlogGoldenTests(unittest.TestCase):
    """Pass 33 — `delete-eventlog-golden` action prunes a trace golden
    through the bridge."""

    _write_demo_rom = EngineBridgeTests._write_demo_rom
    _write_request = EngineBridgeTests._write_request
    _base_request = EngineBridgeTests._base_request

    def _save_eventlog_golden(self, rom_path: Path, name: str) -> None:
        with redirect_stdout(io.StringIO()):
            exit_code = main(
                ["eventlog", "golden-save", str(rom_path), name, "--count", "2"],
            )
        self.assertEqual(exit_code, 0)

    def _delete_request(self, rom_path: Path, name: str | None) -> dict[str, object]:
        req = self._base_request(rom_path, "delete-eventlog-golden")
        if name is not None:
            req["golden_name"] = name
        req["artifacts"]["event_log_path"] = None
        req["artifacts"]["savestate_path"] = None
        return req

    def test_delete_existing_golden_removes_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            rom_path = tmp / "demo.ngc"
            req_path = tmp / "bridge_request.json"
            self._write_demo_rom(rom_path, 0x00200040, b"\x00\x00")

            self._save_eventlog_golden(rom_path, "to-prune")
            from core.goldens import golden_path_for_rom
            golden_path = golden_path_for_rom(rom_path, "to-prune")
            self.assertTrue(golden_path.exists())

            req = self._delete_request(rom_path, "to-prune")
            self._write_request(req_path, req)
            response = execute_engine_bridge_request(req_path)

            self.assertEqual(response["status"], "ok")
            self.assertEqual(response["action"], "delete-eventlog-golden")
            delete = response["result"]["eventlog_golden_delete"]
            self.assertEqual(delete["name"], "to-prune")
            self.assertEqual(delete["deleted_golden_path"], str(golden_path))
            self.assertFalse(golden_path.exists())

    def test_delete_missing_golden_raises(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            rom_path = tmp / "demo.ngc"
            req_path = tmp / "bridge_request.json"
            self._write_demo_rom(rom_path, 0x00200040, b"\x00\x00")

            req = self._delete_request(rom_path, "nope")
            self._write_request(req_path, req)
            with self.assertRaises(EngineBridgeError):
                execute_engine_bridge_request(req_path)

    def test_missing_golden_name_raises(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            rom_path = tmp / "demo.ngc"
            req_path = tmp / "bridge_request.json"
            self._write_demo_rom(rom_path, 0x00200040, b"\x00\x00")

            req = self._delete_request(rom_path, None)
            self._write_request(req_path, req)
            with self.assertRaises(EngineBridgeError):
                execute_engine_bridge_request(req_path)


if __name__ == "__main__":
    unittest.main()
