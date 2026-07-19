"""Minimal read-only memory tests for NgpCraft Emulator."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from core.memory import load_read_bus


class MemoryReadTests(unittest.TestCase):
    def _write_demo_rom(self, path: Path) -> None:
        data = bytearray(0x40)
        data[0x00:0x1C] = b"LICENSED BY SNK CORPORATION".ljust(0x1C, b"\x00")
        data[0x1C:0x20] = (0x00200040).to_bytes(4, "little")
        data[0x20:0x22] = (0x0000).to_bytes(2, "little")
        data[0x22] = 0
        data[0x23] = 0x10
        data[0x24:0x30] = b"TEST GAME\x00\x00\x00"
        data.extend(b"\xDE\xAD\xBE\xEF")
        path.write_bytes(bytes(data))

    def test_read_loaded_rom_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path)
            bus = load_read_bus(rom_path)

            result = bus.read_bytes(0x200040, size=4)

            self.assertEqual(result.status, "ok")
            self.assertEqual(result.data, b"\xDE\xAD\xBE\xEF")

    def test_read_builtin_system_os_version_byte_from_header_mode(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path)
            bus = load_read_bus(rom_path)

            result = bus.read_bytes(0x006F91, size=1)

            self.assertEqual(result.status, "ok")
            self.assertEqual(result.data, b"\x10")

    def test_read_builtin_system_user_answer_default_byte(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path)
            bus = load_read_bus(rom_path)

            result = bus.read_bytes(0x006F86, size=1)

            self.assertEqual(result.status, "ok")
            self.assertEqual(result.data, b"\x00")

    def test_adc_data_register_reads_healthy_battery(self) -> None:
        """I/O 0x60/0x61 = ADREG0, the A/D result for channel AN0 -- which on
        NGPC is the BATTERY voltage (SDK: "A/D converter, 1 channel (Power
        management)").

        Datasheet (TMP95C061 Fig. 3.12 (3-1)): ADREG0L (0x60) bits 7-6 hold the
        LOWER 2 bits of the 10-bit result and bits 5-0 are unused and READ AS 1;
        ADREG0H (0x61) holds the upper 8. So the word is `(result << 6) | 0x3F`,
        which is why the BIOS does `ldw WA,(0x60); srl 6`.

        Modelling it as 0 made the BIOS boot read a FLAT battery at its power-on
        check (0xFF21DC `ld WA,(0x6F80)` / `cp WA,0x01D3`) and power itself off
        via `ld RW3,0; swi 1` (VECT_SHUTDOWN).
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path)
            bus = load_read_bus(rom_path)

            result = bus.read_bytes(0x000060, size=2)

            self.assertEqual(result.status, "ok")
            assert result.data is not None
            adreg = int.from_bytes(result.data, "little")
            self.assertEqual(adreg, 0xFFFF)  # full scale, unused bits read 1
            # What the BIOS derives from it, and caches at 0x6F80.
            self.assertEqual(adreg >> 6, 0x03FF)
            # Comfortably above the low-battery shutdown threshold.
            self.assertGreater(adreg >> 6, 0x01D3)

    def test_read_unbacked_region(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path)
            bus = load_read_bus(rom_path)

            result = bus.read_bytes(0xFF0000, size=1)

            self.assertEqual(result.status, "unbacked")
            self.assertIsNone(result.data)

    def test_read_bios_region_from_optional_backing_image(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            bios_path = Path(tmpdir) / "bios.bin"
            self._write_demo_rom(rom_path)
            bios = bytearray(0x10000)
            bios[0xFE14:0xFE18] = b"\x78\x56\x34\x12"
            bios_path.write_bytes(bytes(bios))
            bus = load_read_bus(rom_path, bios_path=bios_path)

            result = bus.read_bytes(0xFFFE14, size=4)

            self.assertEqual(result.status, "ok")
            self.assertEqual(result.data, b"\x78\x56\x34\x12")

    def test_read_unloaded_cart_flash_defaults_to_erased_ff(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path)
            bus = load_read_bus(rom_path)

            result = bus.read_bytes(0x003FBE00, size=1)

            self.assertEqual(result.status, "ok")
            self.assertEqual(result.data, b"\xFF")

    def test_read_unmapped_region(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path)
            bus = load_read_bus(rom_path)

            result = bus.read_bytes(0x010000, size=1)

            self.assertEqual(result.status, "unmapped")
            self.assertIsNone(result.data)

    def test_k2ge_registers_read_as_zero_on_power_on(self) -> None:
        """K2GE registers must be readable with value 0x00 (power-on default)."""
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path)
            bus = load_read_bus(rom_path)

            result = bus.read_bytes(0x008030, size=1)

            self.assertEqual(result.status, "ok")
            self.assertEqual(result.data, b"\x00")

    def test_k2ge_control_register_enables_irqs_at_reset(self) -> None:
        # 0x008000 is the K2GE CONTROL register. It powers on at 0xC0 =
        # VBlank (bit 7) + HBlank (bit 6) interrupts ENABLED -- not 0x00.
        # Hardware only raises VBlank while bit 7 is set, so a zero here would
        # (wrongly) mean interrupts are disabled out of reset.
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path)
            bus = load_read_bus(rom_path)

            result = bus.read_bytes(0x008000, size=1)

            self.assertEqual(result.status, "ok")
            self.assertEqual(result.data, b"\xC0")

    def test_k2ge_register_range_end_reads_as_zero(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path)
            bus = load_read_bus(rom_path)

            result = bus.read_bytes(0x008FFF, size=1)

            self.assertEqual(result.status, "ok")
            self.assertEqual(result.data, b"\x00")

    # --- M1d Phase 1: on-chip RAM/VRAM pre-init at cold-start ---

    def test_work_ram_low_address_reads_as_zero_at_cold_start(self) -> None:
        """Work RAM at 0x4000 reads as 0x00 at power-on per MEMORY_READ.md."""
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path)
            bus = load_read_bus(rom_path)

            result = bus.read_bytes(0x004000, size=1)

            self.assertEqual(result.status, "ok")
            self.assertEqual(result.data, b"\x00")

    def test_work_ram_high_address_reads_as_zero_at_cold_start(self) -> None:
        """End of Work RAM (0x6BFF) reads as 0x00 at cold-start."""
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path)
            bus = load_read_bus(rom_path)

            result = bus.read_bytes(0x006BFF, size=1)

            self.assertEqual(result.status, "ok")
            self.assertEqual(result.data, b"\x00")

    def test_shared_z80_ram_reads_as_zero_at_cold_start(self) -> None:
        """Shared Z80 RAM (0x7000..0x7FFF) reads as 0 at cold-start."""
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path)
            bus = load_read_bus(rom_path)

            result = bus.read_bytes(0x007800, size=1)

            self.assertEqual(result.status, "ok")
            self.assertEqual(result.data, b"\x00")

    def test_scr_maps_and_char_ram_read_as_zero_at_cold_start(self) -> None:
        """SCR1/SCR2 maps and CHAR_RAM all read as 0 at cold-start."""
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path)
            bus = load_read_bus(rom_path)

            for addr in (0x009000, 0x009800, 0x00A000, 0x00BFFF):
                result = bus.read_bytes(addr, size=1)
                self.assertEqual(result.status, "ok", msg=f"0x{addr:06X}")
                self.assertEqual(result.data, b"\x00", msg=f"0x{addr:06X}")

    def test_cpu_io_page_is_tracked_register_file_reads_zero_at_reset(self) -> None:
        """CPU I/O page (0x00..0xFF) is now a tracked register file: reads
        return the power-on 0x00 default (writes write through to the overlay),
        so read-modify-write config sequences the BIOS boot uses can run."""
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path)
            bus = load_read_bus(rom_path)

            result = bus.read_bytes(0x000080, size=1)

            self.assertEqual(result.status, "ok")
            self.assertEqual(result.data, b"\x00")


if __name__ == "__main__":
    unittest.main()
