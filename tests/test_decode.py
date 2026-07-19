"""Minimal instruction decode tests for NgpCraft Emulator."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from core.decode import decode_instruction_at
from core.memory import load_read_bus


class DecodeTests(unittest.TestCase):
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

    def test_decode_bootstrap_watchdog_kick(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x00200040, b"\x08\x6F\x4E")
            bus = load_read_bus(rom_path)

            result = decode_instruction_at(bus, 0x00200040)

            self.assertEqual(result.status, "decoded")
            self.assertEqual(result.raw_bytes, b"\x08\x6F\x4E")
            self.assertEqual(result.length, 3)
            self.assertEqual(result.mnemonic, "ldb")
            self.assertEqual(result.operands, "(HW_WATCHDOG), 0x4E")
            self.assertEqual(result.assembly, "ldb (HW_WATCHDOG), 0x4E")
            self.assertEqual(result.next_sequential_pc, 0x00200043)

    def test_decode_ld_r32_imm32(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x00200040, b"\x47\x00\x60\x00\x00")
            bus = load_read_bus(rom_path)

            result = decode_instruction_at(bus, 0x00200040)

            self.assertEqual(result.status, "decoded")
            self.assertEqual(result.length, 5)
            self.assertEqual(result.mnemonic, "ld")
            self.assertEqual(result.operands, "XSP, 0x00006000")
            self.assertEqual(result.next_sequential_pc, 0x00200045)

    def test_decode_jr_relative_target(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x00200052, b"\x66\x16")
            bus = load_read_bus(rom_path)

            result = decode_instruction_at(bus, 0x00200052)

            self.assertEqual(result.status, "decoded")
            self.assertEqual(result.mnemonic, "jr")
            self.assertEqual(result.operands, "Z, 0x20006A")
            self.assertEqual(result.next_sequential_pc, 0x00200054)

    def test_decode_prefixed_register_alu_from_bootstrap(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x00200050, b"\xC8\xE1")
            bus = load_read_bus(rom_path)

            result = decode_instruction_at(bus, 0x00200050)

            self.assertEqual(result.status, "decoded")
            self.assertEqual(result.raw_bytes, b"\xC8\xE1")
            self.assertEqual(result.length, 2)
            self.assertEqual(result.assembly, "or A, W")
            self.assertIsNone(result.warning)

    def test_decode_prefixed_ldc_uses_symbolic_control_register_name(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x00200040, b"\xD8\x2E\x20")
            bus = load_read_bus(rom_path)

            result = decode_instruction_at(bus, 0x00200040)

            self.assertEqual(result.status, "decoded")
            self.assertEqual(result.length, 3)
            self.assertEqual(result.assembly, "ldc DMAC0, WA")

    def test_decode_prefixed_ldc_read_uses_symbolic_control_register_name(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x00200040, b"\xE8\x2F\x10")
            bus = load_read_bus(rom_path)

            result = decode_instruction_at(bus, 0x00200040)

            self.assertEqual(result.status, "decoded")
            self.assertEqual(result.length, 3)
            self.assertEqual(result.assembly, "ldc XWA, DMAD0")

    def test_decode_e8_ef_family_as_long_register_alu(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x00209133, b"\xEF\xC8\xFC\xFF\xFF\xFF")
            bus = load_read_bus(rom_path)

            result = decode_instruction_at(bus, 0x00209133)

            self.assertEqual(result.status, "decoded")
            self.assertEqual(result.length, 6)
            self.assertEqual(result.assembly, "add XSP, 0xFFFFFFFC")
            self.assertEqual(result.next_sequential_pc, 0x00209139)

    def test_decode_call_abs24(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x00200094, b"\x1D\x2F\x91\x20")
            bus = load_read_bus(rom_path)

            result = decode_instruction_at(bus, 0x00200094)

            self.assertEqual(result.status, "decoded")
            self.assertEqual(result.length, 4)
            self.assertEqual(result.assembly, "call 0x20912F")
            self.assertEqual(result.next_sequential_pc, 0x00200098)

    def test_decode_lda_r32_abs24_from_official_pattern(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x0020D06D, b"\xF2\x66\x32\x21\x31")
            bus = load_read_bus(rom_path)

            result = decode_instruction_at(bus, 0x0020D06D)

            self.assertEqual(result.status, "decoded")
            self.assertEqual(result.length, 5)
            self.assertEqual(result.assembly, "lda XBC, (0x213266)")
            self.assertEqual(result.next_sequential_pc, 0x0020D072)

    def test_decode_post_increment_byte_load_from_official_loop(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x0020D092, b"\xC5\xE0\x23")
            bus = load_read_bus(rom_path)

            result = decode_instruction_at(bus, 0x0020D092)

            self.assertEqual(result.status, "decoded")
            self.assertEqual(result.length, 3)
            self.assertEqual(result.assembly, "ld C, (XWA+)")
            self.assertEqual(result.next_sequential_pc, 0x0020D095)

    def test_decode_post_increment_byte_store_from_official_loop(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x0020D098, b"\xF5\xF8\x43")
            bus = load_read_bus(rom_path)

            result = decode_instruction_at(bus, 0x0020D098)

            self.assertEqual(result.status, "decoded")
            self.assertEqual(result.length, 3)
            self.assertEqual(result.assembly, "ld (XIZ+), C")
            self.assertEqual(result.next_sequential_pc, 0x0020D09B)

    def test_decode_post_increment_word_store_from_template_loop(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x0020150F, b"\xF5\xF1\x51")
            bus = load_read_bus(rom_path)

            result = decode_instruction_at(bus, 0x0020150F)

            self.assertEqual(result.status, "decoded")
            self.assertEqual(result.length, 3)
            self.assertEqual(result.assembly, "ldw (XIX+), BC")
            self.assertEqual(result.next_sequential_pc, 0x00201512)

    def test_decode_post_increment_word_load(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x00201520, b"\xD5\xF1\x21")
            bus = load_read_bus(rom_path)

            result = decode_instruction_at(bus, 0x00201520)

            self.assertEqual(result.status, "decoded")
            self.assertEqual(result.length, 3)
            self.assertEqual(result.assembly, "ld BC, (XIX+)")
            self.assertEqual(result.next_sequential_pc, 0x00201523)

    def test_decode_post_increment_long_load(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x00201530, b"\xE5\xF1\x23")
            bus = load_read_bus(rom_path)

            result = decode_instruction_at(bus, 0x00201530)

            self.assertEqual(result.status, "decoded")
            self.assertEqual(result.length, 3)
            self.assertEqual(result.assembly, "ld XHL, (XIX+)")
            self.assertEqual(result.next_sequential_pc, 0x00201533)

    def test_decode_post_increment_immediate_word_store(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x00201540, b"\xF5\xF1\x02\x34\x12")
            bus = load_read_bus(rom_path)

            result = decode_instruction_at(bus, 0x00201540)

            self.assertEqual(result.status, "decoded")
            self.assertEqual(result.length, 5)
            self.assertEqual(result.assembly, "ldw (XIX+), 0x1234")
            self.assertEqual(result.next_sequential_pc, 0x00201545)

    def test_decode_indexed_memory_compare_from_official_loop(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x0020D09B, b"\xAF\x04\xFC")
            bus = load_read_bus(rom_path)

            result = decode_instruction_at(bus, 0x0020D09B)

            self.assertEqual(result.status, "decoded")
            self.assertEqual(result.length, 3)
            self.assertEqual(result.assembly, "cp (XSP+4), XIX")
            self.assertEqual(result.next_sequential_pc, 0x0020D09E)

    def test_decode_reg_indirect_word_load_from_mrrobot_pattern(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x002696A9, b"\x91\x20")
            bus = load_read_bus(rom_path)

            result = decode_instruction_at(bus, 0x002696A9)

            self.assertEqual(result.status, "decoded")
            self.assertEqual(result.length, 2)
            self.assertEqual(result.assembly, "ld WA, (XBC)")
            self.assertEqual(result.next_sequential_pc, 0x002696AB)

    def test_decode_reg_indirect_long_load_from_xbc(self) -> None:
        # A1 20 = ld XWA, (XBC) (long register-indirect, op 0x20 = LD R32,(mem))
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x00200040, b"\xA1\x20")
            bus = load_read_bus(rom_path)

            result = decode_instruction_at(bus, 0x00200040)

            self.assertEqual(result.status, "decoded")
            self.assertEqual(result.length, 2)
            self.assertEqual(result.assembly, "ld XWA, (XBC)")
            self.assertEqual(result.next_sequential_pc, 0x00200042)

    def test_decode_reg_indirect_long_load_into_xix_from_xde(self) -> None:
        # A2 24 = ld XIX, (XDE) (op 0x24 -> destination R32[4] = XIX)
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x00200040, b"\xA2\x24")
            bus = load_read_bus(rom_path)

            result = decode_instruction_at(bus, 0x00200040)

            self.assertEqual(result.status, "decoded")
            self.assertEqual(result.length, 2)
            self.assertEqual(result.assembly, "ld XIX, (XDE)")

    def test_decode_arid_d8_bit_test(self) -> None:
        # B8 0A C8 = bit 0, (XWA+10) — bit test on (r32+d8) memory (op 0xC8, n=0)
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x00200040, b"\xB8\x0A\xC8")
            bus = load_read_bus(rom_path)

            result = decode_instruction_at(bus, 0x00200040)

            self.assertEqual(result.status, "decoded")
            self.assertEqual(result.length, 3)
            self.assertEqual(result.assembly, "bit 0, (XWA+10)")
            self.assertEqual(result.next_sequential_pc, 0x00200043)

    def test_decode_arid_d8_bit_test_high_bit_from_xhl(self) -> None:
        # BB 0A CB = bit 3, (XHL+10) (op 0xCB -> bit index 3)
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x00200040, b"\xBB\x0A\xCB")
            bus = load_read_bus(rom_path)

            result = decode_instruction_at(bus, 0x00200040)

            self.assertEqual(result.status, "decoded")
            self.assertEqual(result.length, 3)
            self.assertEqual(result.assembly, "bit 3, (XHL+10)")

    def test_decode_reg_indirect_word_add_immediate(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x00200040, b"\x91\x38\x34\x12")
            bus = load_read_bus(rom_path)

            result = decode_instruction_at(bus, 0x00200040)

            self.assertEqual(result.status, "decoded")
            self.assertEqual(result.length, 4)
            self.assertEqual(result.assembly, "add (XBC), 0x1234")
            self.assertEqual(result.next_sequential_pc, 0x00200044)

    def test_decode_reg_indirect_word_add_memory_destination(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x00200040, b"\x91\x88")
            bus = load_read_bus(rom_path)

            result = decode_instruction_at(bus, 0x00200040)

            self.assertEqual(result.status, "decoded")
            self.assertEqual(result.length, 2)
            self.assertEqual(result.assembly, "add (XBC), WA")
            self.assertEqual(result.next_sequential_pc, 0x00200042)

    def test_decode_reg_indirect_word_ldirw_special_from_mrrobot_pattern(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x00269401, b"\x95\x11")
            bus = load_read_bus(rom_path)

            result = decode_instruction_at(bus, 0x00269401)

            self.assertEqual(result.status, "decoded")
            self.assertEqual(result.length, 2)
            # `0x95` selects w == 5 -> (XIX+),(XIY+) per authoritative ngdis
            # decode_zz_R (tlcs900_zz_rr.c); the earlier XDE/XHL string was wrong.
            self.assertEqual(result.assembly, "ldirw (XIX+),(XIY+)")
            self.assertEqual(result.next_sequential_pc, 0x00269403)

    def test_decode_indexed_word_add_register_destination(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x00200040, b"\x9F\x04\x80")
            bus = load_read_bus(rom_path)

            result = decode_instruction_at(bus, 0x00200040)

            self.assertEqual(result.status, "decoded")
            self.assertEqual(result.length, 3)
            self.assertEqual(result.assembly, "add WA, (XSP+4)")
            self.assertEqual(result.next_sequential_pc, 0x00200043)

    def test_decode_indexed_word_add_memory_destination(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x00200040, b"\x9F\x04\x88")
            bus = load_read_bus(rom_path)

            result = decode_instruction_at(bus, 0x00200040)

            self.assertEqual(result.status, "decoded")
            self.assertEqual(result.length, 3)
            self.assertEqual(result.assembly, "add (XSP+4), WA")
            self.assertEqual(result.next_sequential_pc, 0x00200043)

    def test_decode_indexed_word_xor_memory_destination(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x00200040, b"\x9F\x04\xD8")
            bus = load_read_bus(rom_path)

            result = decode_instruction_at(bus, 0x00200040)

            self.assertEqual(result.status, "decoded")
            self.assertEqual(result.length, 3)
            self.assertEqual(result.assembly, "xor (XSP+4), WA")
            self.assertEqual(result.next_sequential_pc, 0x00200043)

    def test_decode_indexed_word_add_immediate(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x00200040, b"\x9F\x04\x38\x34\x12")
            bus = load_read_bus(rom_path)

            result = decode_instruction_at(bus, 0x00200040)

            self.assertEqual(result.status, "decoded")
            self.assertEqual(result.length, 5)
            self.assertEqual(result.assembly, "add (XSP+4), 0x1234")
            self.assertEqual(result.next_sequential_pc, 0x00200045)

    def test_decode_indexed_word_inc_count(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x00200040, b"\x9F\x04\x61")
            bus = load_read_bus(rom_path)

            result = decode_instruction_at(bus, 0x00200040)

            self.assertEqual(result.status, "decoded")
            self.assertEqual(result.length, 3)
            self.assertEqual(result.assembly, "inc 1, (XSP+4)")
            self.assertEqual(result.next_sequential_pc, 0x00200043)

    def test_decode_indexed_byte_sub_register_destination(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x002016B9, b"\x8F\x14\xA1")
            bus = load_read_bus(rom_path)

            result = decode_instruction_at(bus, 0x002016B9)

            self.assertEqual(result.status, "decoded")
            self.assertEqual(result.length, 3)
            self.assertEqual(result.assembly, "sub A, (XSP+20)")
            self.assertEqual(result.next_sequential_pc, 0x002016BC)

    def test_decode_indexed_byte_add_register_destination(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x002016FE, b"\x8F\x02\x81")
            bus = load_read_bus(rom_path)

            result = decode_instruction_at(bus, 0x002016FE)

            self.assertEqual(result.status, "decoded")
            self.assertEqual(result.length, 3)
            self.assertEqual(result.assembly, "add A, (XSP+2)")
            self.assertEqual(result.next_sequential_pc, 0x00201701)

    def test_decode_indexed_long_sub_register_destination(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x00201720, b"\xAF\x04\xA0")
            bus = load_read_bus(rom_path)

            result = decode_instruction_at(bus, 0x00201720)

            self.assertEqual(result.status, "decoded")
            self.assertEqual(result.length, 3)
            self.assertEqual(result.assembly, "sub XWA, (XSP+4)")
            self.assertEqual(result.next_sequential_pc, 0x00201723)

    def test_decode_indexed_long_xor_memory_destination(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x00201730, b"\xAF\x04\xD8")
            bus = load_read_bus(rom_path)

            result = decode_instruction_at(bus, 0x00201730)

            self.assertEqual(result.status, "decoded")
            self.assertEqual(result.length, 3)
            self.assertEqual(result.assembly, "xor (XSP+4), XWA")
            self.assertEqual(result.next_sequential_pc, 0x00201733)

    def test_decode_indexed_long_or_immediate(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x00201740, b"\xAF\x04\x3E\x78\x56\x34\x12")
            bus = load_read_bus(rom_path)

            result = decode_instruction_at(bus, 0x00201740)

            self.assertEqual(result.status, "decoded")
            self.assertEqual(result.length, 7)
            self.assertEqual(result.assembly, "or (XSP+4), 0x12345678")
            self.assertEqual(result.next_sequential_pc, 0x00201747)

    def test_decode_indexed_word_block_transfer_from_mrrobot_pattern(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x00269934, b"\x99\xDE\x13")
            bus = load_read_bus(rom_path)

            result = decode_instruction_at(bus, 0x00269934)

            self.assertEqual(result.status, "decoded")
            self.assertEqual(result.length, 3)
            self.assertEqual(result.assembly, "lddr (XBC-34)")
            self.assertEqual(result.next_sequential_pc, 0x00269937)

    def test_decode_indexed_byte_block_transfer_from_mrrobot_pattern(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x0026993B, b"\x88\xE8\x12")
            bus = load_read_bus(rom_path)

            result = decode_instruction_at(bus, 0x0026993B)

            self.assertEqual(result.status, "decoded")
            self.assertEqual(result.length, 3)
            self.assertEqual(result.assembly, "ldd (XWA-24)")
            self.assertEqual(result.next_sequential_pc, 0x0026993E)

    def test_decode_ari_secondary_indexed_word_store_from_register(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x002010D2, b"\xF3\x07\xE0\xE4\x53")
            bus = load_read_bus(rom_path)

            result = decode_instruction_at(bus, 0x002010D2)

            self.assertEqual(result.status, "decoded")
            self.assertEqual(result.length, 5)
            self.assertEqual(result.assembly, "ldw (XWA+BC), HL")
            self.assertEqual(result.next_sequential_pc, 0x002010D7)

    def test_decode_ari_secondary_indexed_long_store_from_register(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x002010F1, b"\xF3\x07\xF0\xEC\x60")
            bus = load_read_bus(rom_path)

            result = decode_instruction_at(bus, 0x002010F1)

            self.assertEqual(result.status, "decoded")
            self.assertEqual(result.length, 5)
            self.assertEqual(result.assembly, "ld (XIX+HL), XWA")
            self.assertEqual(result.next_sequential_pc, 0x002010F6)

    def test_decode_ari_secondary_mode1_word_immediate_store(self) -> None:
        # F3 FD 34 03 02 00 00 = ldw (XSP+820), 0x0000 (mode=1 r32+d16, op 0x02)
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x00200040, b"\xF3\xFD\x34\x03\x02\x00\x00")
            bus = load_read_bus(rom_path)

            result = decode_instruction_at(bus, 0x00200040)

            self.assertEqual(result.status, "decoded")
            self.assertEqual(result.length, 7)
            self.assertEqual(result.assembly, "ldw (XSP+820), 0x0000")
            self.assertEqual(result.next_sequential_pc, 0x00200047)

    def test_decode_ari_secondary_mode1_r32_register_store(self) -> None:
        # F3 E5 80 00 60 = ld (XBC+128), XWA (mode=1 r32+d16, op 0x60)
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x00200040, b"\xF3\xE5\x80\x00\x60")
            bus = load_read_bus(rom_path)

            result = decode_instruction_at(bus, 0x00200040)

            self.assertEqual(result.status, "decoded")
            self.assertEqual(result.length, 5)
            self.assertEqual(result.assembly, "ld (XBC+128), XWA")
            self.assertEqual(result.next_sequential_pc, 0x00200045)

    def test_decode_secondary_mode1_bit_test(self) -> None:
        # F3 FD A6 01 C9 = bit 1, (XSP+422) (mode=1, op 0xC9 = BIT #1,(mem))
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x00200040, b"\xF3\xFD\xA6\x01\xC9")
            bus = load_read_bus(rom_path)

            result = decode_instruction_at(bus, 0x00200040)

            self.assertEqual(result.status, "decoded")
            self.assertEqual(result.length, 5)
            self.assertEqual(result.assembly, "bit 1, (XSP+422)")
            self.assertEqual(result.next_sequential_pc, 0x00200045)

    def test_decode_secondary_mode3_bit_test(self) -> None:
        # F3 07 E0 E4 CA = bit 2, (XWA+BC) (mode=3, op 0xCA = BIT #2,(mem))
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x00200040, b"\xF3\x07\xE0\xE4\xCA")
            bus = load_read_bus(rom_path)

            result = decode_instruction_at(bus, 0x00200040)

            self.assertEqual(result.status, "decoded")
            self.assertEqual(result.length, 5)
            self.assertEqual(result.assembly, "bit 2, (XWA+BC)")

    def test_decode_secondary_mode1_byte_load(self) -> None:
        # C3 FD A8 01 21 = ld A, (XSP+424) (mode=1 r32+d16 byte load, op 0x21)
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x00200040, b"\xC3\xFD\xA8\x01\x21")
            bus = load_read_bus(rom_path)

            result = decode_instruction_at(bus, 0x00200040)

            self.assertEqual(result.status, "decoded")
            self.assertEqual(result.length, 5)
            self.assertEqual(result.assembly, "ld A, (XSP+424)")
            self.assertEqual(result.next_sequential_pc, 0x00200045)

    def test_decode_secondary_mode1_long_load_negative_disp(self) -> None:
        # E3 E1 FE FF 23 = ld XHL, (XWA-2) (mode=1 r32+d16 long load, signed d16)
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x00200040, b"\xE3\xE1\xFE\xFF\x23")
            bus = load_read_bus(rom_path)

            result = decode_instruction_at(bus, 0x00200040)

            self.assertEqual(result.status, "decoded")
            self.assertEqual(result.length, 5)
            self.assertEqual(result.assembly, "ld XHL, (XWA-2)")
            self.assertEqual(result.next_sequential_pc, 0x00200045)

    def test_decode_secondary_mode1_byte_compare_immediate(self) -> None:
        # C3 FD A4 01 3F 7E = cp (XSP+420), 0x7E (mode=1 r32+d16, op 0x3F)
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x00200040, b"\xC3\xFD\xA4\x01\x3F\x7E")
            bus = load_read_bus(rom_path)

            result = decode_instruction_at(bus, 0x00200040)

            self.assertEqual(result.status, "decoded")
            self.assertEqual(result.length, 6)
            self.assertEqual(result.assembly, "cp (XSP+420), 0x7E")
            self.assertEqual(result.next_sequential_pc, 0x00200046)

    def test_decode_secondary_mode1_compare_register_vs_memory(self) -> None:
        # C3 E9 C0 01 F3 = cp C, (XDE+448) (mode=1, op 0xF3 = CP R8,(mem), R=3)
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x00200040, b"\xC3\xE9\xC0\x01\xF3")
            bus = load_read_bus(rom_path)

            result = decode_instruction_at(bus, 0x00200040)

            self.assertEqual(result.status, "decoded")
            self.assertEqual(result.length, 5)
            self.assertEqual(result.assembly, "cp C, (XDE+448)")
            self.assertEqual(result.next_sequential_pc, 0x00200045)

    def test_decode_secondary_mode3_compare_register_vs_memory(self) -> None:
        # C3 07 E0 E4 F3 = cp C, (XWA+BC) (mode=3, op 0xF3 = CP R8,(mem), R=3)
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x00200040, b"\xC3\x07\xE0\xE4\xF3")
            bus = load_read_bus(rom_path)

            result = decode_instruction_at(bus, 0x00200040)

            self.assertEqual(result.status, "decoded")
            self.assertEqual(result.length, 5)
            self.assertEqual(result.assembly, "cp C, (XWA+BC)")
            self.assertEqual(result.next_sequential_pc, 0x00200045)

    def test_decode_secondary_mode1_dec_memory(self) -> None:
        # C3 FD 9E 01 69 = dec 1, (XSP+414) (mode=1, op 0x69 = DEC #1, byte mem)
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x00200040, b"\xC3\xFD\x9E\x01\x69")
            bus = load_read_bus(rom_path)

            result = decode_instruction_at(bus, 0x00200040)

            self.assertEqual(result.status, "decoded")
            self.assertEqual(result.length, 5)
            self.assertEqual(result.assembly, "dec 1, (XSP+414)")
            self.assertEqual(result.next_sequential_pc, 0x00200045)

    def test_decode_secondary_mode1_inc_memory_count_eight(self) -> None:
        # C3 E1 04 00 60 = inc 8, (XWA+4) (mode=1, op 0x60, n=0 -> 8)
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x00200040, b"\xC3\xE1\x04\x00\x60")
            bus = load_read_bus(rom_path)

            result = decode_instruction_at(bus, 0x00200040)

            self.assertEqual(result.status, "decoded")
            self.assertEqual(result.length, 5)
            self.assertEqual(result.assembly, "inc 8, (XWA+4)")
            self.assertEqual(result.next_sequential_pc, 0x00200045)

    def test_decode_secondary_indexed_long_load_from_r16_index(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x002014E9, b"\xE3\x07\xE0\xEC\x20")
            bus = load_read_bus(rom_path)

            result = decode_instruction_at(bus, 0x002014E9)

            self.assertEqual(result.status, "decoded")
            self.assertEqual(result.length, 5)
            self.assertEqual(result.assembly, "ld XWA, (XWA+HL)")
            self.assertEqual(result.next_sequential_pc, 0x002014EE)

    def test_decode_secondary_indexed_long_load_from_r8_index(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x0020E088, b"\xE3\x03\xF0\xE1\x24")
            bus = load_read_bus(rom_path)

            result = decode_instruction_at(bus, 0x0020E088)

            self.assertEqual(result.status, "decoded")
            self.assertEqual(result.length, 5)
            self.assertEqual(result.assembly, "ld XIX, (XIX+W)")
            self.assertEqual(result.next_sequential_pc, 0x0020E08D)

    def test_decode_secondary_indexed_byte_load_from_r16_index(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x002014D3, b"\xC3\x07\xE0\xEC\x21")
            bus = load_read_bus(rom_path)

            result = decode_instruction_at(bus, 0x002014D3)

            self.assertEqual(result.status, "decoded")
            self.assertEqual(result.length, 5)
            self.assertEqual(result.assembly, "ld A, (XWA+HL)")
            self.assertEqual(result.next_sequential_pc, 0x002014D8)

    def test_decode_prefixed_byte_bit_test(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x002010A5, b"\xC9\x33\x02")
            bus = load_read_bus(rom_path)

            result = decode_instruction_at(bus, 0x002010A5)

            self.assertEqual(result.status, "decoded")
            self.assertEqual(result.length, 3)
            self.assertEqual(result.assembly, "bit 2, A")
            self.assertEqual(result.next_sequential_pc, 0x002010A8)

    def test_decode_prefixed_long_set_bit(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x002699D8, b"\xE8\x31\x06")
            bus = load_read_bus(rom_path)

            result = decode_instruction_at(bus, 0x002699D8)

            self.assertEqual(result.status, "decoded")
            self.assertEqual(result.length, 3)
            self.assertEqual(result.assembly, "set 6, XWA")
            self.assertEqual(result.next_sequential_pc, 0x002699DB)

    def test_decode_prefixed_byte_divide_immediate(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x002015D8, b"\xC9\x0A\x18")
            bus = load_read_bus(rom_path)

            result = decode_instruction_at(bus, 0x002015D8)

            self.assertEqual(result.status, "decoded")
            self.assertEqual(result.length, 3)
            # `C9 0A 18` is `div WA, 0x18`. The mul/div `rr` code is not a register
            # index: at BYTE size only the odd codes name a word register (001 = WA,
            # 011 = BC, 101 = DE, 111 = HL), and the official assembler encodes
            # `div BC,0x18` as `CB 0A 18`. See core/decode.py.
            self.assertEqual(result.assembly, "div WA, 0x18")
            self.assertEqual(result.next_sequential_pc, 0x002015DB)

    def test_decode_prefixed_long_bit_test(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x002085DD, b"\xE8\x33\x01")
            bus = load_read_bus(rom_path)

            result = decode_instruction_at(bus, 0x002085DD)

            self.assertEqual(result.status, "decoded")
            self.assertEqual(result.length, 3)
            self.assertEqual(result.assembly, "bit 1, XWA")
            self.assertEqual(result.next_sequential_pc, 0x002085E0)

    def test_decode_abs24_memory_bit_test(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x00200E3E, b"\xF2\x6A\x4C\x00\xCC")
            bus = load_read_bus(rom_path)

            result = decode_instruction_at(bus, 0x00200E3E)

            self.assertEqual(result.status, "decoded")
            self.assertEqual(result.length, 5)
            self.assertEqual(result.assembly, "bit 4, (0x004C6A)")
            self.assertEqual(result.next_sequential_pc, 0x00200E43)

    def test_decode_abs24_memory_andcf_bit(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x00200E61, b"\xF2\x6A\x4C\x00\x80")
            bus = load_read_bus(rom_path)

            result = decode_instruction_at(bus, 0x00200E61)

            self.assertEqual(result.status, "decoded")
            self.assertEqual(result.length, 5)
            self.assertEqual(result.assembly, "andcf 0, (0x004C6A)")
            self.assertEqual(result.next_sequential_pc, 0x00200E66)

    def test_decode_abs24_memory_ldcf_dynamic_bit(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x00200E87, b"\xF2\x6A\x4C\x00\x2B")
            bus = load_read_bus(rom_path)

            result = decode_instruction_at(bus, 0x00200E87)

            self.assertEqual(result.status, "decoded")
            self.assertEqual(result.length, 5)
            self.assertEqual(result.assembly, "ldcf A, (0x004C6A)")
            self.assertEqual(result.next_sequential_pc, 0x00200E8C)

    def test_decode_abs24_indirect_call_unconditional(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x002086CC, b"\xF2\x5B\x84\x20\xE8")
            bus = load_read_bus(rom_path)

            result = decode_instruction_at(bus, 0x002086CC)

            self.assertEqual(result.status, "decoded")
            self.assertEqual(result.length, 5)
            self.assertEqual(result.assembly, "call (0x20845B)")
            self.assertEqual(result.control_flow_kind, "call")
            self.assertEqual(result.next_sequential_pc, 0x002086D1)

    def test_decode_abs24_indirect_call_conditional(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x002086CC, b"\xF2\x5B\x84\x20\xEE")
            bus = load_read_bus(rom_path)

            result = decode_instruction_at(bus, 0x002086CC)

            self.assertEqual(result.status, "decoded")
            self.assertEqual(result.length, 5)
            self.assertEqual(result.assembly, "call NZ, (0x20845B)")
            self.assertEqual(result.control_flow_kind, "call")
            self.assertEqual(result.next_sequential_pc, 0x002086D1)

    def test_decode_abs24_byte_and_immediate(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x00207B16, b"\xC2\xCA\x5E\x00\x3C\x01")
            bus = load_read_bus(rom_path)

            result = decode_instruction_at(bus, 0x00207B16)

            self.assertEqual(result.status, "decoded")
            self.assertEqual(result.length, 6)
            self.assertEqual(result.assembly, "and (0x005ECA), 0x01")
            self.assertEqual(result.next_sequential_pc, 0x00207B1C)

    def test_decode_abs8_word_immediate_store_from_stargunner_flash_pattern(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x002085FE, b"\xF0\x66\x02\xD9\xA9")
            bus = load_read_bus(rom_path)

            result = decode_instruction_at(bus, 0x002085FE)

            self.assertEqual(result.status, "decoded")
            self.assertEqual(result.length, 5)
            self.assertEqual(result.assembly, "ldw (0x66), 0xA9D9")
            self.assertEqual(result.next_sequential_pc, 0x00208603)

    def test_decode_abs8_mem_to_mem_byte_store_from_flash_pattern(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x002085F2, b"\xF0\x66\x14\xED\x61")
            bus = load_read_bus(rom_path)

            result = decode_instruction_at(bus, 0x002085F2)

            self.assertEqual(result.status, "decoded")
            self.assertEqual(result.length, 5)
            self.assertEqual(result.assembly, "ld (0x66), (0x61ED)")
            self.assertEqual(result.next_sequential_pc, 0x002085F7)

    def test_decode_abs24_word_load_from_template_pattern(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x00201D1A, b"\xD2\x06\x4F\x00\x20")
            bus = load_read_bus(rom_path)

            result = decode_instruction_at(bus, 0x00201D1A)

            self.assertEqual(result.status, "decoded")
            self.assertEqual(result.length, 5)
            self.assertEqual(result.assembly, "ld WA, (0x004F06)")
            self.assertEqual(result.next_sequential_pc, 0x00201D1F)

    def test_decode_abs24_word_compare_from_stargunner_pattern(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x00207D03, b"\xD2\xCC\x2D\x20\xF6")
            bus = load_read_bus(rom_path)

            result = decode_instruction_at(bus, 0x00207D03)

            self.assertEqual(result.status, "decoded")
            self.assertEqual(result.length, 5)
            self.assertEqual(result.assembly, "cp IZ, (0x202DCC)")
            self.assertEqual(result.next_sequential_pc, 0x00207D08)

    def test_decode_abs24_word_push_from_stargunner_pattern(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x00207EF7, b"\xD2\x02\x5F\x00\x04")
            bus = load_read_bus(rom_path)

            result = decode_instruction_at(bus, 0x00207EF7)

            self.assertEqual(result.status, "decoded")
            self.assertEqual(result.length, 5)
            self.assertEqual(result.assembly, "pushw (0x005F02)")
            self.assertEqual(result.next_sequential_pc, 0x00207EFC)

    def test_decode_abs16_memory_change_bit(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x00200040, b"\xF1\x30\x80\xC7")
            bus = load_read_bus(rom_path)

            result = decode_instruction_at(bus, 0x00200040)

            self.assertEqual(result.status, "decoded")
            self.assertEqual(result.length, 4)
            self.assertEqual(result.assembly, "chg 7, (0x8030)")
            self.assertEqual(result.next_sequential_pc, 0x00200044)

    def test_decode_abs16_memory_xor_immediate(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x00201B42, b"\xC1\x30\x80\x3D\x80")
            bus = load_read_bus(rom_path)

            result = decode_instruction_at(bus, 0x00201B42)

            self.assertEqual(result.status, "decoded")
            self.assertEqual(result.length, 5)
            self.assertEqual(result.assembly, "xor (0x8030), 0x80")
            self.assertEqual(result.next_sequential_pc, 0x00201B47)

    def test_decode_abs8_memory_or_immediate_from_bios_boot(self) -> None:
        # C0 B2 3E 01 = or (0xB2), 0x01 — abs8 byte-memory (CPU I/O page). The
        # real NGPC BIOS boot uses this family to read-modify I/O registers.
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x00200040, b"\xC0\xB2\x3E\x01")
            bus = load_read_bus(rom_path)

            result = decode_instruction_at(bus, 0x00200040)

            self.assertEqual(result.status, "decoded")
            self.assertEqual(result.length, 4)
            self.assertEqual(result.assembly, "or (0xB2), 0x01")
            self.assertEqual(result.next_sequential_pc, 0x00200044)

    def test_decode_abs8_memory_load_register(self) -> None:
        # C0 90 21 = ld A, (0x90) — abs8 byte load (op 0x21 -> R8[1] = A)
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x00200040, b"\xC0\x90\x21")
            bus = load_read_bus(rom_path)

            result = decode_instruction_at(bus, 0x00200040)

            self.assertEqual(result.status, "decoded")
            self.assertEqual(result.length, 3)
            self.assertEqual(result.assembly, "ld A, (0x90)")

    def test_decode_prefixed_long_divide_immediate(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x00210B01, b"\xE9\x0A\x64\x00")
            bus = load_read_bus(rom_path)

            result = decode_instruction_at(bus, 0x00210B01)

            self.assertEqual(result.status, "decoded")
            self.assertEqual(result.length, 4)
            self.assertEqual(result.assembly, "div XBC, 0x0064")
            self.assertEqual(result.next_sequential_pc, 0x00210B05)

    def test_decode_prefixed_long_divide_register_from_stargunner_pattern(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x00207CAC, b"\xD9\x50")
            bus = load_read_bus(rom_path)

            result = decode_instruction_at(bus, 0x00207CAC)

            self.assertEqual(result.status, "decoded")
            self.assertEqual(result.length, 2)
            self.assertEqual(result.assembly, "div WA, BC")
            self.assertEqual(result.next_sequential_pc, 0x00207CAE)

    def test_decode_reg_indirect_word_signed_divide(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x00201D17, b"\x94\x5F")
            bus = load_read_bus(rom_path)

            result = decode_instruction_at(bus, 0x00201D17)

            self.assertEqual(result.status, "decoded")
            self.assertEqual(result.length, 2)
            self.assertEqual(result.assembly, "divs XSP, (XIX)")
            self.assertEqual(result.next_sequential_pc, 0x00201D19)

    def test_decode_indexed_word_signed_multiply(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x00205500, b"\x98\x02\x49")
            bus = load_read_bus(rom_path)

            result = decode_instruction_at(bus, 0x00205500)

            self.assertEqual(result.status, "decoded")
            self.assertEqual(result.length, 3)
            self.assertEqual(result.assembly, "muls XBC, (XWA+2)")
            self.assertEqual(result.next_sequential_pc, 0x00205503)

    def test_decode_secondary_indexed_word_load_from_stargunner_pattern(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x00207A25, b"\xD3\x07\xF0\xE0\x20")
            bus = load_read_bus(rom_path)

            result = decode_instruction_at(bus, 0x00207A25)

            self.assertEqual(result.status, "decoded")
            self.assertEqual(result.length, 5)
            self.assertEqual(result.assembly, "ld WA, (XIX+WA)")
            self.assertEqual(result.next_sequential_pc, 0x00207A2A)

    def test_decode_secondary_indexed_jump_from_stargunner_pattern(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x00207A2F, b"\xF3\x07\xF0\xE0\xD8")
            bus = load_read_bus(rom_path)

            result = decode_instruction_at(bus, 0x00207A2F)

            self.assertEqual(result.status, "decoded")
            self.assertEqual(result.length, 5)
            self.assertEqual(result.assembly, "jp (XIX+WA)")
            self.assertEqual(result.control_flow_kind, "jump")
            self.assertFalse(result.falls_through)

    def test_decode_indirect_call_via_xix(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x0020E08D, b"\xB4\xE8")
            bus = load_read_bus(rom_path)

            result = decode_instruction_at(bus, 0x0020E08D)

            self.assertEqual(result.status, "decoded")
            self.assertEqual(result.length, 2)
            self.assertEqual(result.assembly, "call (XIX)")
            self.assertEqual(result.next_sequential_pc, 0x0020E08F)

    def test_decode_indirect_call_via_xwa_unconditional(self) -> None:
        # B0 E8 = call (XWA): op 0xE8 (cc=8) is the unconditional indirect call.
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x00200040, b"\xB0\xE8")
            bus = load_read_bus(rom_path)

            result = decode_instruction_at(bus, 0x00200040)

            self.assertEqual(result.status, "decoded")
            self.assertEqual(result.length, 2)
            self.assertEqual(result.assembly, "call (XWA)")
            self.assertEqual(result.control_flow_kind, "call")
            self.assertEqual(result.next_sequential_pc, 0x00200042)

    def test_decode_indirect_call_conditional(self) -> None:
        # B1 E6 = call Z, (XBC): op 0xE6 (cc=6 = Z) is a conditional indirect call.
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x00200040, b"\xB1\xE6")
            bus = load_read_bus(rom_path)

            result = decode_instruction_at(bus, 0x00200040)

            self.assertEqual(result.status, "decoded")
            self.assertEqual(result.length, 2)
            self.assertEqual(result.assembly, "call Z, (XBC)")
            self.assertEqual(result.control_flow_kind, "conditional-call")
            self.assertEqual(result.next_sequential_pc, 0x00200042)

    def test_decode_post_increment_immediate_store_from_official_loop(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x0020D0A4, b"\xF5\xF4\x00\x00")
            bus = load_read_bus(rom_path)

            result = decode_instruction_at(bus, 0x0020D0A4)

            self.assertEqual(result.status, "decoded")
            self.assertEqual(result.length, 4)
            self.assertEqual(result.assembly, "ld (XIY+), 0x00")
            self.assertEqual(result.next_sequential_pc, 0x0020D0A8)

    def test_decode_pop_r32_from_official_epilogue(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x0020D0AC, b"\x5E")
            bus = load_read_bus(rom_path)

            result = decode_instruction_at(bus, 0x0020D0AC)

            self.assertEqual(result.status, "decoded")
            self.assertEqual(result.length, 1)
            self.assertEqual(result.assembly, "pop XIZ")
            self.assertEqual(result.next_sequential_pc, 0x0020D0AD)

    def test_decode_abs16_byte_compare_immediate_from_official_epilogue(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x0020D0DA, b"\xC1\x91\x6F\x3F\x00")
            bus = load_read_bus(rom_path)

            result = decode_instruction_at(bus, 0x0020D0DA)

            self.assertEqual(result.status, "decoded")
            self.assertEqual(result.length, 5)
            self.assertEqual(result.assembly, "cp (0x6F91), 0x00")
            self.assertEqual(result.next_sequential_pc, 0x0020D0DF)

    def test_decode_abs24_byte_store_from_official_bootstrap(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x0020D0E3, b"\xF2\x80\x5F\x00\x41")
            bus = load_read_bus(rom_path)

            result = decode_instruction_at(bus, 0x0020D0E3)

            self.assertEqual(result.status, "decoded")
            self.assertEqual(result.length, 5)
            self.assertEqual(result.assembly, "ld (0x005F80), A")
            self.assertEqual(result.next_sequential_pc, 0x0020D0E8)

    def test_decode_abs24_word_store_from_mrrobot_pattern(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x00269A40, b"\xF2\x04\x40\x00\x50")
            bus = load_read_bus(rom_path)

            result = decode_instruction_at(bus, 0x00269A40)

            self.assertEqual(result.status, "decoded")
            self.assertEqual(result.length, 5)
            self.assertEqual(result.assembly, "ldw (0x004004), WA")
            self.assertEqual(result.next_sequential_pc, 0x00269A45)

    def test_decode_abs8_long_load_from_mrrobot_pattern(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x002693CB, b"\xE0\xE4\x26")
            bus = load_read_bus(rom_path)

            result = decode_instruction_at(bus, 0x002693CB)

            self.assertEqual(result.status, "decoded")
            self.assertEqual(result.length, 3)
            self.assertEqual(result.assembly, "ld XIZ, (0xE4)")
            self.assertEqual(result.next_sequential_pc, 0x002693CE)

    def test_decode_pre_decrement_load_families(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(
                rom_path,
                0x00200040,
                b"\xC4\xE4\x21"  # ld A, (-XBC)
                b"\xD4\xE4\x22"  # ld DE, (-XBC)
                b"\xE4\xE0\x21",  # ld XBC, (-XWA)
            )
            bus = load_read_bus(rom_path)

            byte_result = decode_instruction_at(bus, 0x00200040)
            word_result = decode_instruction_at(bus, 0x00200043)
            long_result = decode_instruction_at(bus, 0x00200046)

            self.assertEqual(byte_result.status, "decoded")
            self.assertEqual(byte_result.assembly, "ld A, (-XBC)")
            self.assertEqual(byte_result.length, 3)
            self.assertEqual(word_result.status, "decoded")
            self.assertEqual(word_result.assembly, "ld DE, (-XBC)")
            self.assertEqual(word_result.length, 3)
            self.assertEqual(long_result.status, "decoded")
            self.assertEqual(long_result.assembly, "ld XBC, (-XWA)")
            self.assertEqual(long_result.length, 3)

    def test_decode_word_reg_indirect_push(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x00200040, b"\x90\x04\x93\x04")
            bus = load_read_bus(rom_path)

            push_xwa = decode_instruction_at(bus, 0x00200040)
            push_xhl = decode_instruction_at(bus, 0x00200042)

            self.assertEqual(push_xwa.status, "decoded")
            self.assertEqual(push_xwa.assembly, "push (XWA)")
            self.assertEqual(push_xwa.length, 2)
            self.assertEqual(push_xhl.status, "decoded")
            self.assertEqual(push_xhl.assembly, "push (XHL)")
            self.assertEqual(push_xhl.length, 2)

    def test_decode_secondary_indexed_byte_compare_immediate(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x00200040, b"\xC3\x07\xE4\xE0\x3F\x00")
            bus = load_read_bus(rom_path)

            result = decode_instruction_at(bus, 0x00200040)

            self.assertEqual(result.status, "decoded")
            self.assertEqual(result.assembly, "cp (XBC+WA), 0x00")
            self.assertEqual(result.length, 6)
            self.assertEqual(result.next_sequential_pc, 0x00200046)

    def test_decode_abs16_bit_ops_from_official_bootstrap(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x0020D0E8, b"\xF1\x86\x6F\xB5\xF1\x86\x6F\xBE")
            bus = load_read_bus(rom_path)

            res_result = decode_instruction_at(bus, 0x0020D0E8)
            set_result = decode_instruction_at(bus, 0x0020D0EC)

            self.assertEqual(res_result.status, "decoded")
            self.assertEqual(res_result.length, 4)
            self.assertEqual(res_result.assembly, "res 5, (0x6F86)")
            self.assertEqual(set_result.status, "decoded")
            self.assertEqual(set_result.length, 4)
            self.assertEqual(set_result.assembly, "set 6, (0x6F86)")

    def test_decode_abs24_immediate_store_from_official_subroutine(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x0020D21D, b"\xF2\x1A\x50\x00\x00\x00")
            bus = load_read_bus(rom_path)

            result = decode_instruction_at(bus, 0x0020D21D)

            self.assertEqual(result.status, "decoded")
            self.assertEqual(result.length, 6)
            self.assertEqual(result.assembly, "ld (0x00501A), 0x00")
            self.assertEqual(result.next_sequential_pc, 0x0020D223)

    def test_decode_abs24_increment_from_template_pattern(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x002011E2, b"\xC2\x06\x4F\x00\x61")
            bus = load_read_bus(rom_path)

            result = decode_instruction_at(bus, 0x002011E2)

            self.assertEqual(result.status, "decoded")
            self.assertEqual(result.length, 5)
            self.assertEqual(result.assembly, "inc 1, (0x004F06)")
            self.assertEqual(result.next_sequential_pc, 0x002011E7)

    def test_decode_abs24_add_register_destination_from_template_pattern(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x002016D8, b"\xC2\x08\x42\x00\x81")
            bus = load_read_bus(rom_path)

            result = decode_instruction_at(bus, 0x002016D8)

            self.assertEqual(result.status, "decoded")
            self.assertEqual(result.length, 5)
            self.assertEqual(result.assembly, "add A, (0x004208)")
            self.assertEqual(result.next_sequential_pc, 0x002016DD)

    def test_decode_abs24_cp_memory_minus_register_from_mrrobot_pattern(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x002696DA, b"\xC2\x00\x40\x00\xF9")
            bus = load_read_bus(rom_path)

            result = decode_instruction_at(bus, 0x002696DA)

            self.assertEqual(result.status, "decoded")
            self.assertEqual(result.length, 5)
            self.assertEqual(result.assembly, "cp (0x004000), A")
            self.assertEqual(result.next_sequential_pc, 0x002696DF)

    def test_decode_abs16_long_store_from_official_vector_init(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x0020D0F9, b"\xF1\xB8\x6F\x60")
            bus = load_read_bus(rom_path)

            result = decode_instruction_at(bus, 0x0020D0F9)

            self.assertEqual(result.status, "decoded")
            self.assertEqual(result.length, 4)
            self.assertEqual(result.assembly, "ld (0x6FB8), XWA")
            self.assertEqual(result.next_sequential_pc, 0x0020D0FD)

    def test_decode_reg_indirect_jump_xix(self) -> None:
        # B4 D8 = JP (XIX). Register-indirect jump (mem-byte 0xB0+r, op 0xD0+cc;
        # cc=8 unconditional). Used by the gb2t900 HAL register-return.
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x00200040, b"\xB4\xD8")
            bus = load_read_bus(rom_path)
            result = decode_instruction_at(bus, 0x00200040)
            self.assertEqual(result.status, "decoded")
            self.assertEqual(result.length, 2)
            self.assertEqual(result.assembly, "jp (XIX)")
            self.assertEqual(result.control_flow_kind, "jump")

    def test_f1_sub20_is_lda_not_a_byte_load(self) -> None:
        """`F1 lo hi 0x20+r` is `lda R16, imm16` -- the ADDRESS, not the contents.

        This test used to assert the opposite, and named its own source: gb2t900.py,
        our Game Boy -> TLCS-900 translator, hand-emits these bytes for `LD A,(nn)`
        because (its own comment says) "t900as LD unsupported for abs16". The
        assembler supports it fine; the author simply picked the wrong family.
        Asked directly:

            asm900:  ld  A,(0x6141)   ->  C1 41 61 21     <- byte LOAD lives in C1
            asm900:  ld  WA,(0x6141)  ->  D1 41 61 20     <- word LOAD lives in D1
            asm900:  ld  (0x6141),A   ->  F1 41 61 41     <- F1 is the STORE family
            asm900:  lda WA,0x6141    ->  F1 41 61 20     <- and THIS is F1 sub-0x20

        So `F1 41 61 21` is `lda BC, 0x6141`: the constant 0x6141 goes INTO BC.
        Puyo Pop is what the wrong decode cost -- `F1 FF 0F 20` at 0x2015B2 left WA
        holding the byte AT 0x0FFF (zero) instead of 0x0FFF itself, and gate G3
        caught the native core and the reference disagreeing on that register.
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x00200040, b"\xF1\x41\x61\x21")
            bus = load_read_bus(rom_path)

            result = decode_instruction_at(bus, 0x00200040)

            self.assertEqual(result.status, "decoded")
            self.assertEqual(result.length, 4)
            self.assertEqual(result.mnemonic, "lda")
            self.assertEqual(result.assembly, "lda BC, (0x6141)")
            self.assertEqual(result.next_sequential_pc, 0x00200044)

    def test_decode_abs16_byte_alu_mem_source_from_bios_checksum(self) -> None:
        # add A,(0x6F87) = C1 87 6F 81. The real SNK BIOS boot at 0xFF331B sums
        # HW registers into A via this abs16 byte ALU mem-source family
        # (0x80..0xFF: op hi nibble = operation, bit3 = (mem),R8 direction,
        # low 3 = R8 index). Verified against our NGPC disassembler.
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x00200040, b"\xC1\x87\x6F\x81")
            bus = load_read_bus(rom_path)

            result = decode_instruction_at(bus, 0x00200040)

            self.assertEqual(result.status, "decoded")
            self.assertEqual(result.length, 4)
            self.assertEqual(result.mnemonic, "add")
            self.assertEqual(result.assembly, "add A, (0x6F87)")
            self.assertEqual(result.next_sequential_pc, 0x00200044)

    def test_decode_abs16_byte_alu_mem_dest_direction(self) -> None:
        # add (0x1234),W = C1 34 12 88 — the (mem),R8 direction (bit3 set).
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x00200040, b"\xC1\x34\x12\x88")
            bus = load_read_bus(rom_path)

            result = decode_instruction_at(bus, 0x00200040)

            self.assertEqual(result.status, "decoded")
            self.assertEqual(result.assembly, "add (0x1234), W")

    def test_decode_abs8_word_compare_immediate_from_bios_boot(self) -> None:
        # cpw (0xB6),0x0050 = D0 B6 3F 50 00. HW-confirmed 2026-07-03: 0xD0..0xD7
        # is a WORD MEMORY family (not word register-direct — that is 0xD8..0xDF).
        # The real SNK BIOS boot at 0xFF115C runs this; the repo previously
        # mis-decoded it as the 2-byte reg-direct `sbc IZ, WA`.
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x00200040, b"\xD0\xB6\x3F\x50\x00")
            bus = load_read_bus(rom_path)

            result = decode_instruction_at(bus, 0x00200040)

            self.assertEqual(result.status, "decoded")
            self.assertEqual(result.length, 5)
            self.assertEqual(result.mnemonic, "cpw")
            self.assertEqual(result.assembly, "cpw (0xB6), 0x0050")
            self.assertEqual(result.next_sequential_pc, 0x00200045)

    def test_decode_abs8_word_load_r16(self) -> None:
        # ld BC,(0x89) = D0 89 21 — abs8 word load into R16 (op 0x20..0x27).
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x00200040, b"\xD0\x89\x21")
            bus = load_read_bus(rom_path)

            result = decode_instruction_at(bus, 0x00200040)

            self.assertEqual(result.status, "decoded")
            self.assertEqual(result.length, 3)
            self.assertEqual(result.assembly, "ld BC, (0x89)")

    def test_decode_abs8_bit_set_from_bios_boot(self) -> None:
        # set 2, (0xB3) = F0 B3 BA. abs8 memory bit-manipulation family
        # (op 0xA8=tset/0xB0=res/0xB8=set/0xC0=chg/0xC8=bit, low3=bit index).
        # Real SNK BIOS boot at 0xFF1114, just before the init halt.
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x00200040, b"\xF0\xB3\xBA")
            bus = load_read_bus(rom_path)

            result = decode_instruction_at(bus, 0x00200040)

            self.assertEqual(result.status, "decoded")
            self.assertEqual(result.length, 3)
            self.assertEqual(result.mnemonic, "set")
            self.assertEqual(result.assembly, "set 2, (0xB3)")

    def test_decode_abs8_byte_register_store_from_bios_boot(self) -> None:
        # ld (0xBC),A = F0 BC 41. Store direction of the abs8 byte family
        # (op 0x40..0x47 = ld (abs8), R8). Real SNK BIOS boot at 0xFF33AC.
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x00200040, b"\xF0\xBC\x41")
            bus = load_read_bus(rom_path)

            result = decode_instruction_at(bus, 0x00200040)

            self.assertEqual(result.status, "decoded")
            self.assertEqual(result.length, 3)
            self.assertEqual(result.mnemonic, "ld")
            self.assertEqual(result.assembly, "ld (0xBC), A")
            self.assertEqual(result.next_sequential_pc, 0x00200043)

    def test_decode_lda_abs8_from_bios_hw_register_pointer(self) -> None:
        # lda XIX,(0xA0) = F0 A0 34. Real SNK BIOS boot at 0xFF3396 loads the
        # effective abs8 address into a register (pointer to a HW register).
        # 0xF0 op 0x20..0x27 = lda R16, 0x30..0x37 = lda R32 (R = op & 7).
        # Verified against our NGPC disassembler; falls through to the existing
        # 0xF0 abs8-store family for non-lda sub-ops.
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x00200040, b"\xF0\xA0\x34")
            bus = load_read_bus(rom_path)

            result = decode_instruction_at(bus, 0x00200040)

            self.assertEqual(result.status, "decoded")
            self.assertEqual(result.length, 3)
            self.assertEqual(result.mnemonic, "lda")
            self.assertEqual(result.assembly, "lda XIX, (0xA0)")
            self.assertEqual(result.next_sequential_pc, 0x00200043)

    def test_decode_f0_abs8_store_still_decodes_after_lda_slice(self) -> None:
        # Regression guard: the 0xF0 lda slice must NOT shadow the pre-existing
        # 0xF0 abs8 store family. F0 66 02 D9 A9 = ldw (0x66), 0xA9D9.
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x00200040, b"\xF0\x66\x02\xD9\xA9")
            bus = load_read_bus(rom_path)

            result = decode_instruction_at(bus, 0x00200040)

            self.assertEqual(result.status, "decoded")
            self.assertEqual(result.assembly, "ldw (0x66), 0xA9D9")

    def test_decode_abs16_immediate_store_from_official_video_init(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x0020D142, b"\xF1\x02\x80\x00\x00")
            bus = load_read_bus(rom_path)

            result = decode_instruction_at(bus, 0x0020D142)

            self.assertEqual(result.status, "decoded")
            self.assertEqual(result.length, 5)
            self.assertEqual(result.assembly, "ld (0x8002), 0x00")
            self.assertEqual(result.next_sequential_pc, 0x0020D147)

    def test_decode_abs16_word_immediate_store_from_bios_boot(self) -> None:
        # F1 80 6F 02 34 12 = ldw (0x6F80), 0x1234. The real NGPC BIOS boot uses
        # this abs16 word-immediate store (op 0x02) to init work RAM words.
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x00200040, b"\xF1\x80\x6F\x02\x34\x12")
            bus = load_read_bus(rom_path)

            result = decode_instruction_at(bus, 0x00200040)

            self.assertEqual(result.status, "decoded")
            self.assertEqual(result.length, 6)
            self.assertEqual(result.assembly, "ldw (0x6F80), 0x1234")
            self.assertEqual(result.next_sequential_pc, 0x00200046)

    def test_decode_calr_relative_target(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x00200040, b"\x1E\x10\x00")
            bus = load_read_bus(rom_path)

            result = decode_instruction_at(bus, 0x00200040)

            self.assertEqual(result.status, "decoded")
            self.assertEqual(result.length, 3)
            self.assertEqual(result.assembly, "calr 0x200053")

    def test_decode_fixed_sr_and_flag_ops(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x00200040, b"\x02\x10")
            bus = load_read_bus(rom_path)

            push_sr = decode_instruction_at(bus, 0x00200040)
            rcf = decode_instruction_at(bus, 0x00200041)

            self.assertEqual(push_sr.status, "decoded")
            self.assertEqual(push_sr.assembly, "push SR")
            self.assertEqual(rcf.status, "decoded")
            self.assertEqual(rcf.assembly, "rcf")

    def test_decode_indexed_store_from_bootstrap(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x00200057, b"\xBD\x00\x41")
            bus = load_read_bus(rom_path)

            result = decode_instruction_at(bus, 0x00200057)

            self.assertEqual(result.status, "decoded")
            self.assertEqual(result.length, 3)
            self.assertEqual(result.assembly, "ld (XIY+0), A")

    def test_decode_prefixed_inc_no_longer_misclassified_as_jr(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x0020005A, b"\xED\x61")
            bus = load_read_bus(rom_path)

            result = decode_instruction_at(bus, 0x0020005A)

            self.assertEqual(result.status, "decoded")
            self.assertEqual(result.length, 2)
            self.assertEqual(result.assembly, "inc 1, XIY")

    def test_decode_broken_d0_prefix_with_warning(self) -> None:
        # Migrated 2026-07-08: the old `D0 61` fixture asserted the retired
        # "D0 reg-direct is silicon-broken" semantic. D0..D7 is now the WORD
        # MEMORY-addressing family (HW-confirmed), so `D0 61` decodes cleanly as
        # a memory op (`cpw (0x61), SP`) and is no longer flagged. The genuinely
        # still-broken D0 form is the ALU-IMMEDIATE encoding `D0 C8 01 00` =
        # `add WA, 0x0001` (real-NGPC crash 2026-05-20). Re-point to it so this
        # test keeps exercising the same "!BROKEN D0..D7 decoded-with-warning"
        # mechanism.
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x00200040, b"\xD0\xC8\x01\x00")
            bus = load_read_bus(rom_path)

            result = decode_instruction_at(bus, 0x00200040)

            self.assertEqual(result.status, "decoded")
            self.assertEqual(result.assembly, "add WA, 0x0001")
            self.assertIsNotNone(result.warning)
            assert result.warning is not None
            self.assertIn("!BROKEN D0..D7", result.warning)

    def test_decode_prefixed_byte_ext_warning(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x00200040, b"\xC8\x12")
            bus = load_read_bus(rom_path)

            result = decode_instruction_at(bus, 0x00200040)

            self.assertEqual(result.status, "decoded")
            self.assertEqual(result.assembly, "extz W")
            self.assertIsNotNone(result.warning)
            assert result.warning is not None
            self.assertIn("!UNDEFINED EXTZ", result.warning)

    def test_decode_adc_w_b_risk_warning(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x00200040, b"\xCA\x90")
            bus = load_read_bus(rom_path)

            result = decode_instruction_at(bus, 0x00200040)

            self.assertEqual(result.status, "decoded")
            self.assertEqual(result.assembly, "adc W, B")
            self.assertIsNotNone(result.warning)
            assert result.warning is not None
            self.assertIn("adc W, B", result.warning)

    def test_decode_scc_nz_xhl_from_stargunner_frontier(self) -> None:
        # DB 7E = scc NZ, HL — the honest frontier on the StarGunner trace
        # (stop-on-unknown at 0x0020E27F prior to this decoder extension).
        # DB is the WORD prefix (0xD8..0xDF), so the register is word HL, not XHL.
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x00200040, b"\xDB\x7E")
            bus = load_read_bus(rom_path)

            result = decode_instruction_at(bus, 0x00200040)

            self.assertEqual(result.status, "decoded")
            self.assertEqual(result.length, 2)
            self.assertEqual(result.assembly, "scc NZ, HL")

    def test_decode_scc_family_variants(self) -> None:
        # Prefix selects size + register; sub-op low-nibble selects CC index.
        # Only a couple of representative cases — full 8 prefixes × 16 CCs is
        # exhaustively covered by the generic decoder logic.
        cases = [
            (b"\xEB\x78", "scc T, XHL"),    # prefix EB → R32[3]=XHL, 78=T
            (b"\xEB\x70", "scc F, XHL"),    # 70 = F (always-false)
            (b"\xE8\x76", "scc Z, XWA"),    # prefix E8 → R32[0]=XWA; E8..EF is the genuine long prefix
            (b"\xC8\x7E", "scc NZ, W"),     # prefix C8 → R8[0]=W (byte-size)
        ]
        with tempfile.TemporaryDirectory() as tmpdir:
            for i, (body, expected) in enumerate(cases):
                rom_path = Path(tmpdir) / f"demo_{i}.ngc"
                self._write_demo_rom(rom_path, 0x00200040, body)
                bus = load_read_bus(rom_path)
                result = decode_instruction_at(bus, 0x00200040)
                self.assertEqual(result.status, "decoded", msg=f"case {body.hex()}")
                self.assertEqual(result.assembly, expected, msg=f"case {body.hex()}")
                self.assertEqual(result.length, 2, msg=f"case {body.hex()}")

    def test_decode_prefixed_long_paa(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x00200040, b"\xE8\x14")
            bus = load_read_bus(rom_path)

            result = decode_instruction_at(bus, 0x00200040)

            self.assertEqual(result.status, "decoded")
            self.assertEqual(result.length, 2)
            self.assertEqual(result.assembly, "paa XWA")

    def test_decode_prefixed_byte_djnz(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x00200040, b"\xC9\x1C\xFE")
            bus = load_read_bus(rom_path)

            result = decode_instruction_at(bus, 0x00200040)

            self.assertEqual(result.status, "decoded")
            self.assertEqual(result.length, 3)
            self.assertEqual(result.assembly, "djnz A, 0x200041")

    def test_decode_prefixed_mirr_word_special_case(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x00200040, b"\xDB\x16")
            bus = load_read_bus(rom_path)

            result = decode_instruction_at(bus, 0x00200040)

            self.assertEqual(result.status, "decoded")
            self.assertEqual(result.length, 2)
            self.assertEqual(result.assembly, "mirr HL")

    def test_decode_prefixed_bs1_special_cases(self) -> None:
        cases = [
            (b"\xDC\x0E", "bs1f A, IX"),
            (b"\xDC\x0F", "bs1b A, IX"),
        ]
        with tempfile.TemporaryDirectory() as tmpdir:
            for i, (body, expected) in enumerate(cases):
                rom_path = Path(tmpdir) / f"demo_{i}.ngc"
                self._write_demo_rom(rom_path, 0x00200040, body)
                bus = load_read_bus(rom_path)
                result = decode_instruction_at(bus, 0x00200040)
                self.assertEqual(result.status, "decoded")
                self.assertEqual(result.length, 2)
                self.assertEqual(result.assembly, expected)

    def test_decode_prefixed_mula_long_special_case(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x00200040, b"\xDC\x19")
            bus = load_read_bus(rom_path)

            result = decode_instruction_at(bus, 0x00200040)

            self.assertEqual(result.status, "decoded")
            self.assertEqual(result.length, 2)
            self.assertEqual(result.assembly, "mula XIX")

    def test_decode_prefixed_modulo_adjust_special_cases(self) -> None:
        cases = [
            (b"\xDC\x38\x07\x00", "minc1 0x0007, IX"),
            (b"\xDC\x39\x06\x00", "minc2 0x0006, IX"),
            (b"\xDC\x3A\x3C\x00", "minc4 0x003C, IX"),
            (b"\xDC\x3C\x07\x00", "mdec1 0x0007, IX"),
            (b"\xDC\x3D\x06\x00", "mdec2 0x0006, IX"),
            (b"\xDC\x3E\xFC\x00", "mdec4 0x00FC, IX"),
        ]
        with tempfile.TemporaryDirectory() as tmpdir:
            for i, (body, expected) in enumerate(cases):
                rom_path = Path(tmpdir) / f"demo_{i}.ngc"
                self._write_demo_rom(rom_path, 0x00200040, body)
                bus = load_read_bus(rom_path)
                result = decode_instruction_at(bus, 0x00200040)
                self.assertEqual(result.status, "decoded")
                self.assertEqual(result.length, 4)
                self.assertEqual(result.assembly, expected)

    def test_decode_prefixed_carry_flag_register_forms(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x00200040, b"\xC9\x20\x03")
            bus = load_read_bus(rom_path)
            result = decode_instruction_at(bus, 0x00200040)
            self.assertEqual(result.status, "decoded")
            self.assertEqual(result.length, 3)
            self.assertEqual(result.assembly, "andcf 3, A")

            # `ldcf A, HL` is a WORD register-direct carry-flag op. The word
            # reg-direct prefix is D8..DF (HL = R16[3] -> D8+3 = DB), NOT D0..D7.
            # D0..D7 is the WORD MEMORY-addressing family (HW-confirmed 2026-07-03
            # via hw_test_d0; retail shmup/battle carts execute the D3 ARI mem-form
            # end-to-end), so `D3 2B ...` now decodes as an ARI memory op, not
            # `ldcf A, HL`. This case previously used D3 as a (mis-decoded) word
            # register prefix.
            rom_path = Path(tmpdir) / "demo_word.ngc"
            self._write_demo_rom(rom_path, 0x00200040, b"\xDB\x2B")
            bus = load_read_bus(rom_path)
            result = decode_instruction_at(bus, 0x00200040)
            self.assertEqual(result.status, "decoded")
            self.assertEqual(result.length, 2)
            self.assertEqual(result.assembly, "ldcf A, HL")

    def test_decode_d2_d3_word_memory_frontier_forms(self) -> None:
        """D0..D7 word-memory ALU/compare forms reached by retail carts.

        `D2 56 47 00 3F FF 7F` = cpw (abs24), imm16 (battle cart frontier).
        `D3 FD 08 06 88`       = add (r32+d16), R (shmup cart frontier).
        Both previously fell through to the mis-decoded word register-direct
        path and were flagged silicon-broken; they are ordinary word-memory
        instructions the shipped carts execute.
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "battle.ngc"
            self._write_demo_rom(rom_path, 0x00200040, b"\xD2\x56\x47\x00\x3F\xFF\x7F")
            bus = load_read_bus(rom_path)
            result = decode_instruction_at(bus, 0x00200040)
            self.assertEqual(result.status, "decoded")
            self.assertEqual(result.length, 7)
            self.assertEqual(result.assembly, "cpw (0x004756), 0x7FFF")

            rom_path = Path(tmpdir) / "shmup.ngc"
            self._write_demo_rom(rom_path, 0x00200040, b"\xD3\xFD\x08\x06\x88")
            bus = load_read_bus(rom_path)
            result = decode_instruction_at(bus, 0x00200040)
            self.assertEqual(result.status, "decoded")
            self.assertEqual(result.length, 5)
            self.assertEqual(result.assembly, "add (XSP+1544), WA")

    def test_unknown_opcode_stays_honest(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x00200040, b"\x1F")
            bus = load_read_bus(rom_path)

            result = decode_instruction_at(bus, 0x00200040)

            self.assertEqual(result.status, "unknown-opcode")
            self.assertEqual(result.raw_bytes, b"\x1F")
            self.assertIsNone(result.length)
            self.assertIsNone(result.assembly)

    def test_supported_family_reports_truncation(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            data = bytearray(0x200000)
            data[0x00:0x1C] = b"LICENSED BY SNK CORPORATION".ljust(0x1C, b"\x00")
            data[0x1C:0x20] = (0x00200040).to_bytes(4, "little")
            data[0x20:0x22] = (0x0000).to_bytes(2, "little")
            data[0x22] = 0
            data[0x23] = 0x10
            data[0x24:0x30] = b"TEST GAME\x00\x00\x00"
            data[-2:] = b"\x47\x00"
            rom_path.write_bytes(bytes(data))
            bus = load_read_bus(rom_path)

            result = decode_instruction_at(bus, 0x003FFFFE)

            self.assertEqual(result.status, "truncated")
            self.assertEqual(result.raw_bytes, b"\x47\x00")
            self.assertIsNone(result.length)
            self.assertIsNone(result.assembly)


    def test_decode_ldw_reg_indirect_imm16(self) -> None:
        """B1 02 34 12 = ldw (XBC), 0x1234"""
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x00200040, b"\xB1\x02\x34\x12")
            bus = load_read_bus(rom_path)

            result = decode_instruction_at(bus, 0x00200040)

            self.assertEqual(result.status, "decoded")
            self.assertEqual(result.length, 4)
            self.assertEqual(result.assembly, "ldw (XBC), 0x1234")
            self.assertEqual(result.next_sequential_pc, 0x00200044)

    def test_decode_ld_reg_indirect_imm8(self) -> None:
        """B2 00 42 = ld (XDE), 0x42"""
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x00200040, b"\xB2\x00\x42")
            bus = load_read_bus(rom_path)

            result = decode_instruction_at(bus, 0x00200040)

            self.assertEqual(result.status, "decoded")
            self.assertEqual(result.length, 3)
            self.assertEqual(result.assembly, "ld (XDE), 0x42")
            self.assertEqual(result.next_sequential_pc, 0x00200043)

    def _decode_body(self, body: bytes):
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x00200040, body)
            bus = load_read_bus(rom_path)
            return decode_instruction_at(bus, 0x00200040)

    def test_decode_c7_dense_ref_xiz_spill(self) -> None:
        """C7 FB 9F = ld QIZH, L (T900_DENSE_REF.md XIZ spill example)."""
        result = self._decode_body(b"\xC7\xFB\x9F")
        self.assertEqual(result.status, "decoded")
        self.assertEqual(result.length, 3)
        self.assertEqual(result.assembly, "ld QIZH, L")
        self.assertEqual(result.next_sequential_pc, 0x00200043)

    def test_decode_c7_current_bank_q_name(self) -> None:
        """C7 E6 99 = ld QC, A — code 0xE6 is QC (XBC byte 2), not the old rH14."""
        result = self._decode_body(b"\xC7\xE6\x99")
        self.assertEqual(result.status, "decoded")
        self.assertEqual(result.assembly, "ld QC, A")

    def test_decode_c7_immediate_form_is_four_bytes(self) -> None:
        """C7 E6 CF 10 = cp QC, 0x10 — immediate ALU form carries a 4th byte."""
        result = self._decode_body(b"\xC7\xE6\xCF\x10")
        self.assertEqual(result.status, "decoded")
        self.assertEqual(result.length, 4)
        self.assertEqual(result.assembly, "cp QC, 0x10")
        self.assertEqual(result.next_sequential_pc, 0x00200044)

    def test_decode_c7_invalid_register_code_is_not_decoded(self) -> None:
        """C7 C7 12 — register code 0xC7 is invalid (?), so this is not an instruction."""
        result = self._decode_body(b"\xC7\xC7\x12")
        self.assertNotEqual(result.status, "decoded")


    def test_decode_c7_carry_flag_register_forms(self) -> None:
        result = self._decode_body(b"\xC7\xE6\x20\x03")
        self.assertEqual(result.status, "decoded")
        self.assertEqual(result.length, 4)
        self.assertEqual(result.assembly, "andcf 3, QC")

        result = self._decode_body(b"\xC7\xE6\x2B")
        self.assertEqual(result.status, "decoded")
        self.assertEqual(result.length, 3)
        self.assertEqual(result.assembly, "ldcf A, QC")

    def test_decode_c7_ldc_uses_symbolic_control_register_name(self) -> None:
        result = self._decode_body(b"\xC7\xE6\x2E\x22")
        self.assertEqual(result.status, "decoded")
        self.assertEqual(result.length, 4)
        self.assertEqual(result.assembly, "ldc DMAM0, QC")

        result = self._decode_body(b"\xC7\xE6\x2F\x22")
        self.assertEqual(result.status, "decoded")
        self.assertEqual(result.length, 4)
        self.assertEqual(result.assembly, "ldc QC, DMAM0")

    def test_decode_c7_exts_warning(self) -> None:
        result = self._decode_body(b"\xC7\xE6\x13")
        self.assertEqual(result.status, "decoded")
        self.assertEqual(result.assembly, "exts QC")
        self.assertIsNotNone(result.warning)
        assert result.warning is not None
        self.assertIn("!UNDEFINED C7 EXTS", result.warning)

    def test_decode_d7_push_qiz(self) -> None:
        # D7 = WORD extended-register prefix `D7 <reg> <op>`. Code 0xFA -> QIZ
        # (high word of XIZ), op 0x04 -> push. This is a 3-byte instruction the
        # pre-D7 decoder mis-read as the 2-byte `rl A, SP`. Real engine runtime
        # helper frontier.
        result = self._decode_body(b"\xD7\xFA\x04")
        self.assertEqual(result.status, "decoded")
        self.assertEqual(result.length, 3)
        self.assertEqual(result.mnemonic, "push")
        self.assertEqual(result.assembly, "push QIZ")

    def test_decode_d7_ld_word_imm(self) -> None:
        # D7 EC 03 34 12 = ld HL, 0x1234 (word extended-register, imm16, 5 bytes).
        result = self._decode_body(b"\xD7\xEC\x03\x34\x12")
        self.assertEqual(result.status, "decoded")
        self.assertEqual(result.length, 5)
        self.assertEqual(result.assembly, "ld HL, 0x1234")

if __name__ == "__main__":
    unittest.main()
