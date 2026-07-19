"""The RUN path: a cartridge booted on the native core, frames pulled out of it.

This is what the whole C++ chantier was for. `EmulatorSession` retires ~1 700
instructions a second and a Neo Geo Pocket Color needs ~615 000 to run in real
time, so the Python session has never been able to *play* a game -- it inspects
one. `NativeSession` hands the job to the C++ core and gets the picture back.

The gate here is deliberately about the SEAM, not the CPU (G2 and G3 own the CPU):
that the frame boundary is the core's and not a guess, that a frame comes out of
the video window the core owns, and that a stop is reported rather than swallowed.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from core import native
from core.native_session import NativeSession


def _write_demo_rom(path: Path, entry: int, body: bytes) -> None:
    header = bytearray(0x30)
    header[0x00:0x1C] = b"COPYRIGHT BY SNK CORPORATION"[:0x1C].ljust(0x1C, b" ")
    header[0x1C:0x20] = entry.to_bytes(4, "little")
    header[0x23] = 0x10
    header[0x24:0x30] = b"NATIVESESS  "[:0x0C].ljust(0x0C, b"\x00")
    rom = bytearray(header)
    offset = entry - 0x200000
    rom.extend(b"\x00" * (offset - len(rom)))
    rom.extend(body)
    rom.extend(b"\x00" * 64)
    path.write_bytes(bytes(rom))


@unittest.skipUnless(native.available(), "native core not built (cmake --build cpp/build)")
class NativeSessionTests(unittest.TestCase):
    def _spin_rom(self, tmpdir: str) -> Path:
        """A ROM that does nothing but burn cycles: `nop; jr -2`."""
        rom = Path(tmpdir) / "spin.ngc"
        _write_demo_rom(rom, 0x00200040, b"\x00\x68\xFD")
        return rom

    def test_frames_advance_and_the_boundary_is_the_cores(self) -> None:
        # The frame boundary belongs to the raster, and the raster lives in the
        # core. `run_frames` must land ON it -- not a burst past it, which is what
        # a shell counting instructions of its own would do (CPP_CORE_PORT.md §4,
        # hazard 4).
        with tempfile.TemporaryDirectory() as tmpdir:
            with NativeSession(self._spin_rom(tmpdir)) as session:
                self.assertEqual(session.frame_count, 0)
                self.assertEqual(session.run_frames(1), 1)
                self.assertEqual(session.frame_count, 1)
                self.assertEqual(session.run_frames(10), 10)
                self.assertEqual(session.frame_count, 11)
                self.assertIsNone(session.stop_status)
                self.assertGreater(session.executed, 0)

    def test_a_frame_costs_a_plausible_number_of_instructions(self) -> None:
        # A frame is 198 scanlines x 517 cycles = 102 366 cycles. A `nop` is 2 and
        # a taken `jr` is 5, so this loop retires roughly 102 366 / 3.5 ~= 29 000
        # instructions per frame. The point is not the exact figure: it is that the
        # core is pacing on its own clock rather than on a fixed instruction count.
        with tempfile.TemporaryDirectory() as tmpdir:
            with NativeSession(self._spin_rom(tmpdir)) as session:
                session.run_frames(1)
                self.assertGreater(session.executed, 10_000)
                self.assertLess(session.executed, 60_000)

    def test_a_trap_is_reported_not_swallowed(self) -> None:
        # `0xFF` with no BIOS attached vectors through an empty table; the ROM here
        # simply runs into an un-ported encoding. Whatever the stop, it must SURFACE
        # -- a run path that silently keeps going would be worse than a slow one.
        with tempfile.TemporaryDirectory() as tmpdir:
            rom = Path(tmpdir) / "trap.ngc"
            _write_demo_rom(rom, 0x00200040, b"\xDE\x01\x00")   # an un-ported form
            with NativeSession(rom) as session:
                session.run_frames(1)
                self.assertIsNotNone(session.stop_status)
                self.assertEqual(session.stop_pc, 0x00200040)

    def test_the_video_window_comes_from_the_core(self) -> None:
        # The renderer reads 0x8000..0xBFFF. The core owns those bytes -- including
        # RAS.V and BLNK, which the Python session pokes into a fetch view instead
        # (hazard 2). One bulk read, not 16 384 crossings.
        with tempfile.TemporaryDirectory() as tmpdir:
            with NativeSession(self._spin_rom(tmpdir)) as session:
                session.run_frames(1)
                window = session.video_memory()
                self.assertEqual(len(window), 0x4000)
                # Power-on values the core sets and the renderer depends on.
                self.assertEqual(window[0x008000], 0xC0)
                self.assertEqual(window[0x008118], 0x80)
                frame = session.render()
                self.assertEqual((frame.width, frame.height), (160, 152))


if __name__ == "__main__":
    unittest.main()
