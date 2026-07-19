"""Cartridge flash write model tests (direct AMD command path).

Covers the standalone `FlashController` command logic and its end-to-end
wiring through `build_run_steps`, where hardware-discarded cart-window writes
drive the flash and commits land in the writable overlay.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from core.flash import FlashController
from core.fetch import load_fetch_view
from core.run_steps import build_run_steps


class FlashControllerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fc = FlashController()
        self.we_on = {0x6E: 0x14}
        self.we_off = {0x6E: 0xF0}

    def test_program_sequence_commits_byte(self) -> None:
        # Unlock cycle (write to an unlock address) arms; next write commits.
        self.assertEqual(
            self.fc.process_discarded_write(0x205555, b"\xAA", self.we_on), {}
        )
        committed = self.fc.process_discarded_write(0x203000, b"\x42", self.we_on)
        self.assertEqual(committed, {0x203000: 0x42})
        self.assertEqual(self.fc.backing[0x203000], 0x42)

    def test_full_amd_program_cycle(self) -> None:
        # 0xAA->0x5555, 0x55->0x2AAA, 0xA0->0x5555, data->dest.
        self.fc.process_discarded_write(0x205555, b"\xAA", self.we_on)
        self.fc.process_discarded_write(0x202AAA, b"\x55", self.we_on)
        self.fc.process_discarded_write(0x205555, b"\xA0", self.we_on)
        committed = self.fc.process_discarded_write(0x2A0010, b"\x7E", self.we_on)
        self.assertEqual(committed, {0x2A0010: 0x7E})

    def test_write_without_we_is_inert(self) -> None:
        self.fc.process_discarded_write(0x205555, b"\xAA", self.we_off)
        committed = self.fc.process_discarded_write(0x203000, b"\x42", self.we_off)
        self.assertEqual(committed, {})
        self.assertNotIn(0x203000, self.fc.backing)

    def test_unarmed_cart_write_does_not_commit(self) -> None:
        # A cart write with no preceding unlock is ignored (open bus).
        committed = self.fc.process_discarded_write(0x203000, b"\x42", self.we_on)
        self.assertEqual(committed, {})

    def test_status_address_does_not_arm(self) -> None:
        self.fc.process_discarded_write(0x220000, b"\x90", self.we_on)
        committed = self.fc.process_discarded_write(0x203000, b"\x42", self.we_on)
        self.assertEqual(committed, {})

    def test_clear_pending_keeps_backing(self) -> None:
        self.fc.process_discarded_write(0x205555, b"\xAA", self.we_on)
        self.fc.process_discarded_write(0x203000, b"\x42", self.we_on)
        self.fc.clear_pending()
        self.assertEqual(self.fc.backing[0x203000], 0x42)
        # Pending latch cleared -> a lone cart write no longer commits.
        self.assertEqual(
            self.fc.process_discarded_write(0x203004, b"\x99", self.we_on), {}
        )


class FlashRunLoopTests(unittest.TestCase):
    """End-to-end: real store instructions drive the flash through the run loop."""

    def _write_rom(self, path: Path, body: bytes) -> None:
        data = bytearray(0x40)
        data[0x00:0x1C] = b"LICENSED BY SNK CORPORATION".ljust(0x1C, b"\x00")
        data[0x1C:0x20] = (0x00200040).to_bytes(4, "little")
        data[0x23] = 0x10
        data[0x24:0x30] = b"TEST GAME\x00\x00\x00"
        data[0x40 : 0x40 + len(body)] = body
        path.write_bytes(bytes(data))

    def test_amd_program_through_run_loop_commits_to_overlay(self) -> None:
        # 08 6E 14              ldb (0x6E), 0x14        ; enable /WE
        # F2 55 55 20 00 AA     ld (0x205555), 0xAA     ; unlock (arm)
        # F2 00 30 20 00 42     ld (0x203000), 0x42     ; program byte -> commit
        body = (
            b"\x08\x6E\x14"
            b"\xF2\x55\x55\x20\x00\xAA"
            b"\xF2\x00\x30\x20\x00\x42"
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "flash.ngc"
            self._write_rom(rom_path, body)
            view = load_fetch_view(rom_path)
            flash = FlashController()
            result = build_run_steps(view=view, count=3, flash=flash)

        # All three stores executed; the programmed byte is in the overlay.
        self.assertEqual(result.executed_count, 3)
        self.assertEqual(result.final_memory.get(0x203000), 0x42)
        self.assertEqual(flash.backing.get(0x203000), 0x42)
        # The unlock write itself did not land as data.
        self.assertNotEqual(result.final_memory.get(0x205555), 0xAA)

    def test_no_flash_controller_keeps_open_bus(self) -> None:
        # Same sequence, no flash controller -> writes stay discarded (open bus).
        body = (
            b"\x08\x6E\x14"
            b"\xF2\x55\x55\x20\x00\xAA"
            b"\xF2\x00\x30\x20\x00\x42"
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "flash.ngc"
            self._write_rom(rom_path, body)
            view = load_fetch_view(rom_path)
            result = build_run_steps(view=view, count=3)  # flash=None

        self.assertEqual(result.executed_count, 3)
        self.assertNotIn(0x203000, result.final_memory)

    def test_program_without_we_does_not_commit_through_run_loop(self) -> None:
        # Skip the /WE enable -> the AMD sequence is inert.
        body = (
            b"\xF2\x55\x55\x20\x00\xAA"
            b"\xF2\x00\x30\x20\x00\x42"
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "flash.ngc"
            self._write_rom(rom_path, body)
            view = load_fetch_view(rom_path)
            flash = FlashController()
            result = build_run_steps(view=view, count=2, flash=flash)

        self.assertEqual(result.executed_count, 2)
        self.assertNotIn(0x203000, result.final_memory)


if __name__ == "__main__":
    unittest.main()
