"""PPM frame diff + named frame golden registry tests (pass 25)."""

from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from core.frame_diff import FrameDiffResult, diff_ppm_bytes, parse_ppm_p6
from core.frame_goldens import (
    FRAME_GOLDEN_FORMAT_VERSION,
    build_frame_golden_manifest,
    delete_frame_golden,
    frame_golden_paths_for_rom,
    list_frame_goldens,
    load_frame_golden,
    save_frame_golden,
)
from ngpc_emu import main


def _write_demo_rom(path: Path, entry_point: int = 0x00200040) -> None:
    data = bytearray(0x40)
    data[0x00:0x1C] = b"LICENSED BY SNK CORPORATION".ljust(0x1C, b"\x00")
    data[0x1C:0x20] = entry_point.to_bytes(4, "little")
    data[0x22] = 0
    data[0x23] = 0x10
    data[0x24:0x30] = b"FRAME GOLD\x00\x00"
    path.write_bytes(bytes(data))


def _make_ppm(width: int, height: int, fill: tuple[int, int, int]) -> bytes:
    """Build a minimal P6 PPM with a uniform fill color (RGB888)."""
    header = f"P6\n{width} {height}\n255\n".encode("ascii")
    body = bytes(fill) * (width * height)
    return header + body


def _flip_pixel(data: bytes, x: int, y: int, width: int) -> bytes:
    """Return a copy of `data` with the pixel at `(x, y)` mutated.

    `data` must be a valid `P6\\n<W> <H>\\n255\\n<body>` blob produced
    by `_make_ppm`. Mutates the R-channel byte of the target pixel.
    """
    body_offset = data.find(b"\n255\n") + len("\n255\n")
    pixel_offset = body_offset + (y * width + x) * 3
    out = bytearray(data)
    out[pixel_offset] = (out[pixel_offset] + 1) & 0xFF
    return bytes(out)


class ParsePpmTests(unittest.TestCase):
    def test_round_trip_dimensions_and_body(self) -> None:
        data = _make_ppm(4, 3, (0xAA, 0xBB, 0xCC))
        width, height, body = parse_ppm_p6(data)
        self.assertEqual((width, height), (4, 3))
        self.assertEqual(len(body), 4 * 3 * 3)
        self.assertEqual(body[:3], b"\xAA\xBB\xCC")

    def test_accepts_comments_in_header(self) -> None:
        data = (
            b"P6\n# captured by NgpCraft_emulator\n2 2\n# maxval next\n255\n"
            + bytes((0xFF, 0x00, 0x00)) * 4
        )
        width, height, body = parse_ppm_p6(data)
        self.assertEqual((width, height), (2, 2))
        self.assertEqual(len(body), 12)

    def test_rejects_missing_magic(self) -> None:
        with self.assertRaises(ValueError):
            parse_ppm_p6(b"P3\n2 2\n255\n\x00" * 4)

    def test_rejects_truncated_body(self) -> None:
        data = b"P6\n2 2\n255\n" + b"\x00\x00"  # missing pixels
        with self.assertRaises(ValueError):
            parse_ppm_p6(data)

    def test_rejects_unsupported_maxval(self) -> None:
        data = b"P6\n2 2\n65535\n" + b"\x00" * 24
        with self.assertRaises(ValueError):
            parse_ppm_p6(data)

    def test_rejects_zero_dimensions(self) -> None:
        with self.assertRaises(ValueError):
            parse_ppm_p6(b"P6\n0 2\n255\n")


class DiffPpmTests(unittest.TestCase):
    def test_identical_frames_report_equal(self) -> None:
        a = _make_ppm(4, 4, (0x12, 0x34, 0x56))
        b = _make_ppm(4, 4, (0x12, 0x34, 0x56))
        result = diff_ppm_bytes(a, b)
        self.assertTrue(result.equal)
        self.assertEqual(result.pixel_count_different, 0)
        self.assertIsNone(result.first_diff_pixel)
        self.assertEqual(result.diff_ratio, 0.0)
        self.assertEqual(result.total_pixels, 16)

    def test_single_pixel_diff_reports_position(self) -> None:
        a = _make_ppm(4, 4, (0x00, 0x00, 0x00))
        # Flip pixel (2, 1).
        b = _flip_pixel(a, 2, 1, 4)
        result = diff_ppm_bytes(a, b)
        self.assertFalse(result.equal)
        self.assertEqual(result.pixel_count_different, 1)
        self.assertEqual(result.first_diff_pixel, (2, 1))
        self.assertAlmostEqual(result.diff_ratio, 1 / 16)

    def test_all_pixels_diff_reports_full_ratio(self) -> None:
        a = _make_ppm(2, 2, (0x00, 0x00, 0x00))
        b = _make_ppm(2, 2, (0xFF, 0xFF, 0xFF))
        result = diff_ppm_bytes(a, b)
        self.assertFalse(result.equal)
        self.assertEqual(result.pixel_count_different, 4)
        self.assertEqual(result.first_diff_pixel, (0, 0))
        self.assertEqual(result.diff_ratio, 1.0)

    def test_dimension_mismatch_raises(self) -> None:
        a = _make_ppm(4, 4, (0, 0, 0))
        b = _make_ppm(2, 2, (0, 0, 0))
        with self.assertRaises(ValueError):
            diff_ppm_bytes(a, b)


class FrameGoldenStorageTests(unittest.TestCase):
    def test_save_load_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            rom_path = tmp / "demo.ngc"
            _write_demo_rom(rom_path)
            ppm = _make_ppm(160, 152, (0x11, 0x22, 0x33))
            manifest = build_frame_golden_manifest(
                rom_path=rom_path,
                name="boot-screen",
                ppm_bytes=ppm,
                width=160,
                height=152,
                label="cold-start backdrop",
            )

            ppm_path, manifest_path = save_frame_golden(
                rom_path, "boot-screen", ppm, manifest,
            )
            self.assertTrue(ppm_path.exists())
            self.assertTrue(manifest_path.exists())

            golden = load_frame_golden(rom_path, "boot-screen")
            self.assertEqual(golden.name, "boot-screen")
            self.assertEqual(golden.ppm_path, ppm_path)
            self.assertEqual(golden.manifest_path, manifest_path)
            self.assertEqual(
                golden.manifest["format_version"], FRAME_GOLDEN_FORMAT_VERSION,
            )
            self.assertEqual(golden.manifest["label"], "cold-start backdrop")
            self.assertEqual(golden.manifest["width"], 160)
            self.assertEqual(golden.manifest["height"], 152)
            self.assertEqual(golden.manifest["ppm_byte_count"], len(ppm))

    def test_load_missing_golden_raises(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            rom_path = tmp / "demo.ngc"
            _write_demo_rom(rom_path)
            with self.assertRaises(FileNotFoundError):
                load_frame_golden(rom_path, "nope")

    def test_list_returns_saved_goldens(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            rom_path = tmp / "demo.ngc"
            _write_demo_rom(rom_path)
            for name in ("a", "b", "c"):
                ppm = _make_ppm(4, 4, (0, 0, 0))
                manifest = build_frame_golden_manifest(
                    rom_path=rom_path, name=name, ppm_bytes=ppm,
                    width=4, height=4,
                )
                save_frame_golden(rom_path, name, ppm, manifest)

            goldens = list_frame_goldens(rom_path)
            names = {g.name for g in goldens}
            self.assertEqual(names, {"a", "b", "c"})

    def test_delete_removes_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            rom_path = tmp / "demo.ngc"
            _write_demo_rom(rom_path)
            ppm = _make_ppm(4, 4, (0, 0, 0))
            manifest = build_frame_golden_manifest(
                rom_path=rom_path, name="tmp", ppm_bytes=ppm,
                width=4, height=4,
            )
            save_frame_golden(rom_path, "tmp", ppm, manifest)
            ppm_path, manifest_path = frame_golden_paths_for_rom(rom_path, "tmp")
            self.assertTrue(ppm_path.exists())
            self.assertTrue(manifest_path.exists())

            delete_frame_golden(rom_path, "tmp")
            self.assertFalse(ppm_path.exists())
            self.assertFalse(manifest_path.exists())

    def test_delete_missing_raises(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            rom_path = tmp / "demo.ngc"
            _write_demo_rom(rom_path)
            with self.assertRaises(FileNotFoundError):
                delete_frame_golden(rom_path, "nope")


class FrameDiffCliTests(unittest.TestCase):
    def test_cli_identical_frames_exit_0(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            ppm_a = tmp / "a.ppm"
            ppm_b = tmp / "b.ppm"
            ppm_a.write_bytes(_make_ppm(4, 4, (0, 0, 0)))
            ppm_b.write_bytes(_make_ppm(4, 4, (0, 0, 0)))

            stdout = io.StringIO()
            with redirect_stdout(stdout):
                exit_code = main(["frame", "diff", str(ppm_a), str(ppm_b)])
            self.assertEqual(exit_code, 0)
            self.assertIn("MATCH", stdout.getvalue())

    def test_cli_different_frames_exit_1_with_first_diff(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            ppm_a = tmp / "a.ppm"
            ppm_b = tmp / "b.ppm"
            base = _make_ppm(4, 4, (0, 0, 0))
            ppm_a.write_bytes(base)
            ppm_b.write_bytes(_flip_pixel(base, 1, 2, 4))

            stdout = io.StringIO()
            with redirect_stdout(stdout):
                exit_code = main(
                    ["frame", "diff", str(ppm_a), str(ppm_b), "--json"],
                )
            self.assertEqual(exit_code, 1)
            payload = json.loads(stdout.getvalue())
            self.assertFalse(payload["equal"])
            self.assertEqual(payload["pixel_count_different"], 1)
            self.assertEqual(payload["first_diff_pixel"], [1, 2])


class FrameGoldenCliTests(unittest.TestCase):
    def test_save_then_check_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            rom_path = tmp / "demo.ngc"
            _write_demo_rom(rom_path)

            with redirect_stdout(io.StringIO()):
                exit_code = main(
                    ["frame", "golden-save", str(rom_path), "boot"],
                )
            self.assertEqual(exit_code, 0)

            stdout = io.StringIO()
            with redirect_stdout(stdout):
                exit_code = main(
                    ["frame", "golden-check", str(rom_path), "boot"],
                )
            self.assertEqual(exit_code, 0)
            self.assertIn("MATCH", stdout.getvalue())

    def test_check_against_missing_golden_exits_1(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            rom_path = tmp / "demo.ngc"
            _write_demo_rom(rom_path)
            with redirect_stdout(io.StringIO()):
                exit_code = main(
                    ["frame", "golden-check", str(rom_path), "ghost"],
                )
            self.assertEqual(exit_code, 1)

    def test_list_shows_saved_goldens(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            rom_path = tmp / "demo.ngc"
            _write_demo_rom(rom_path)
            with redirect_stdout(io.StringIO()):
                main(["frame", "golden-save", str(rom_path), "alpha"])
                main(["frame", "golden-save", str(rom_path), "beta", "--label", "bg"])

            stdout = io.StringIO()
            with redirect_stdout(stdout):
                exit_code = main(
                    ["frame", "golden-list", str(rom_path), "--json"],
                )
            self.assertEqual(exit_code, 0)
            payload = json.loads(stdout.getvalue())
            self.assertEqual(payload["count"], 2)
            names = {g["name"] for g in payload["goldens"]}
            self.assertEqual(names, {"alpha", "beta"})

    def test_delete_then_check_exits_1(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            rom_path = tmp / "demo.ngc"
            _write_demo_rom(rom_path)
            with redirect_stdout(io.StringIO()):
                main(["frame", "golden-save", str(rom_path), "x"])
                main(["frame", "golden-delete", str(rom_path), "x"])
                exit_code = main(
                    ["frame", "golden-check", str(rom_path), "x"],
                )
            self.assertEqual(exit_code, 1)

    def test_save_with_label_persists_in_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            rom_path = tmp / "demo.ngc"
            _write_demo_rom(rom_path)

            stdout = io.StringIO()
            with redirect_stdout(stdout):
                exit_code = main(
                    [
                        "frame", "golden-save", str(rom_path), "labelled",
                        "--label", "VBlank entry frame",
                        "--json",
                    ],
                )
            self.assertEqual(exit_code, 0)
            payload = json.loads(stdout.getvalue())
            self.assertEqual(
                payload["manifest"]["label"], "VBlank entry frame",
            )


class FrameGoldenCheckAllCliTests(unittest.TestCase):
    """Pass 26 — `frame golden-check-all` batch CI workflow."""

    def test_empty_registry_exits_0_with_zero_count(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            rom_path = tmp / "demo.ngc"
            _write_demo_rom(rom_path)

            stdout = io.StringIO()
            with redirect_stdout(stdout):
                exit_code = main(
                    ["frame", "golden-check-all", str(rom_path), "--json"],
                )
            self.assertEqual(exit_code, 0)
            payload = json.loads(stdout.getvalue())
            self.assertEqual(payload["total"], 0)
            self.assertEqual(payload["passed"], 0)
            self.assertEqual(payload["failed"], 0)
            self.assertTrue(payload["all_equal"])
            self.assertEqual(payload["results"], [])

    def test_all_goldens_pass_exits_0(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            rom_path = tmp / "demo.ngc"
            _write_demo_rom(rom_path)
            with redirect_stdout(io.StringIO()):
                main(["frame", "golden-save", str(rom_path), "alpha"])
                main(["frame", "golden-save", str(rom_path), "beta"])

            stdout = io.StringIO()
            with redirect_stdout(stdout):
                exit_code = main(
                    ["frame", "golden-check-all", str(rom_path), "--json"],
                )
            self.assertEqual(exit_code, 0)
            payload = json.loads(stdout.getvalue())
            self.assertEqual(payload["total"], 2)
            self.assertEqual(payload["passed"], 2)
            self.assertEqual(payload["failed"], 0)
            self.assertTrue(payload["all_equal"])
            statuses = {r["name"]: r["status"] for r in payload["results"]}
            self.assertEqual(statuses, {"alpha": "match", "beta": "match"})

    def test_one_corrupted_golden_exits_1_others_still_checked(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            rom_path = tmp / "demo.ngc"
            _write_demo_rom(rom_path)
            with redirect_stdout(io.StringIO()):
                main(["frame", "golden-save", str(rom_path), "a"])
                main(["frame", "golden-save", str(rom_path), "b"])
                main(["frame", "golden-save", str(rom_path), "c"])

            # Corrupt the middle golden by flipping a pixel byte.
            ppm_path, _ = frame_golden_paths_for_rom(rom_path, "b")
            original = ppm_path.read_bytes()
            ppm_path.write_bytes(_flip_pixel(original, 5, 5, 160))

            stdout = io.StringIO()
            with redirect_stdout(stdout):
                exit_code = main(
                    ["frame", "golden-check-all", str(rom_path), "--json"],
                )
            self.assertEqual(exit_code, 1)
            payload = json.loads(stdout.getvalue())
            self.assertEqual(payload["total"], 3)
            self.assertEqual(payload["checked"], 3)
            self.assertEqual(payload["passed"], 2)
            self.assertEqual(payload["failed"], 1)
            self.assertFalse(payload["all_equal"])
            self.assertFalse(payload["stopped_early"])
            statuses = {r["name"]: r["status"] for r in payload["results"]}
            self.assertEqual(statuses, {"a": "match", "b": "diff", "c": "match"})

    def test_stop_on_fail_short_circuits(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            rom_path = tmp / "demo.ngc"
            _write_demo_rom(rom_path)
            with redirect_stdout(io.StringIO()):
                # Sorted alphabetically: 'a' first, 'b' second, 'c' third.
                main(["frame", "golden-save", str(rom_path), "a"])
                main(["frame", "golden-save", str(rom_path), "b"])
                main(["frame", "golden-save", str(rom_path), "c"])

            ppm_path, _ = frame_golden_paths_for_rom(rom_path, "b")
            original = ppm_path.read_bytes()
            ppm_path.write_bytes(_flip_pixel(original, 5, 5, 160))

            stdout = io.StringIO()
            with redirect_stdout(stdout):
                exit_code = main(
                    [
                        "frame", "golden-check-all", str(rom_path),
                        "--stop-on-fail",
                        "--json",
                    ],
                )
            self.assertEqual(exit_code, 1)
            payload = json.loads(stdout.getvalue())
            self.assertEqual(payload["total"], 3)
            # Only 'a' (match) and 'b' (diff) checked — 'c' skipped.
            self.assertEqual(payload["checked"], 2)
            self.assertEqual(payload["passed"], 1)
            self.assertEqual(payload["failed"], 1)
            self.assertTrue(payload["stopped_early"])
            checked_names = [r["name"] for r in payload["results"]]
            self.assertEqual(checked_names, ["a", "b"])

    def test_save_current_dir_writes_rendered_ppm(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            rom_path = tmp / "demo.ngc"
            _write_demo_rom(rom_path)
            with redirect_stdout(io.StringIO()):
                main(["frame", "golden-save", str(rom_path), "alpha"])

            triage_dir = tmp / "triage"
            with redirect_stdout(io.StringIO()):
                exit_code = main(
                    [
                        "frame", "golden-check-all", str(rom_path),
                        "--save-current-dir", str(triage_dir),
                    ],
                )
            self.assertEqual(exit_code, 0)
            current_path = triage_dir / "demo.current.ppm"
            self.assertTrue(current_path.exists())
            self.assertTrue(current_path.read_bytes().startswith(b"P6\n"))

    def test_human_summary_lists_each_golden_status(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            rom_path = tmp / "demo.ngc"
            _write_demo_rom(rom_path)
            with redirect_stdout(io.StringIO()):
                main(["frame", "golden-save", str(rom_path), "alpha"])
                main(["frame", "golden-save", str(rom_path), "beta"])
            # Corrupt beta.
            ppm_path, _ = frame_golden_paths_for_rom(rom_path, "beta")
            ppm_path.write_bytes(_flip_pixel(ppm_path.read_bytes(), 0, 0, 160))

            stdout = io.StringIO()
            with redirect_stdout(stdout):
                exit_code = main(
                    ["frame", "golden-check-all", str(rom_path)],
                )
            self.assertEqual(exit_code, 1)
            text = stdout.getvalue()
            self.assertIn("[OK]   alpha", text)
            self.assertIn("[DIFF] beta", text)
            self.assertIn("1 passed", text)
            self.assertIn("1 failed", text)


if __name__ == "__main__":
    unittest.main()
