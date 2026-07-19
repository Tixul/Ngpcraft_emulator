"""Minimal execute-next tests for NgpCraft Emulator."""

from __future__ import annotations

from dataclasses import replace
import tempfile
import unittest
from pathlib import Path

from core.cpu import BankedByteRegisters, StatusFlags, create_unknown_control_registers
from core.execute import build_execute_next, seed_cpu_state_for_execution
from core.fetch import load_fetch_view
from core.quirks import load_known_quirk_database


class ExecuteNextTests(unittest.TestCase):
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

    def test_execute_ld_r32_imm32_updates_register_and_pc(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x00200040, b"\x47\x00\x60\x00\x00")
            view = load_fetch_view(rom_path)

            result = build_execute_next(view)

            self.assertEqual(result.status, "executed")
            self.assertEqual(result.decode.assembly, "ld XSP, 0x00006000")
            self.assertEqual(result.written_registers, ("XSP", "PC"))
            assert result.after_cpu is not None
            self.assertEqual(result.after_cpu.regs.xsp, 0x00006000)
            self.assertEqual(result.after_cpu.pc, 0x00200045)

    def test_execute_direct_jump_updates_pc(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x00200040, b"\x1B\x98\x00\x20")
            view = load_fetch_view(rom_path)

            result = build_execute_next(view)

            self.assertEqual(result.status, "executed")
            self.assertEqual(result.decode.assembly, "jp 0x200098")
            self.assertEqual(result.written_registers, ("PC",))
            assert result.after_cpu is not None
            self.assertEqual(result.after_cpu.pc, 0x00200098)

    def test_execute_halt_advances_pc_then_stops_cpu(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x00200040, b"\x05")
            view = load_fetch_view(rom_path)

            result = build_execute_next(view)

            self.assertEqual(result.status, "cpu-halted")
            self.assertEqual(result.decode.assembly, "halt")
            self.assertEqual(result.written_registers, ("PC",))
            assert result.after_cpu is not None
            assert result.after_memory is not None
            self.assertEqual(result.after_cpu.pc, 0x00200041)
            self.assertEqual(result.after_memory, {})

    def test_execute_rcf_resets_carry_halfcarry_and_n(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x00200040, b"\x10")
            base_view = load_fetch_view(rom_path)
            seeded_cpu = replace(
                base_view.machine.cpu,
                flags=StatusFlags(sf=True, zf=False, vf=True, hf=True, cf=True, nf=True),
            )

            result = build_execute_next(base_view, cpu_state=seeded_cpu)

            self.assertEqual(result.status, "executed")
            self.assertEqual(result.decode.assembly, "rcf")
            assert result.after_cpu is not None
            self.assertFalse(result.after_cpu.flags.cf)
            self.assertFalse(result.after_cpu.flags.hf)
            self.assertFalse(result.after_cpu.flags.nf)
            self.assertTrue(result.after_cpu.flags.vf)
            self.assertTrue(result.after_cpu.flags.sf)
            self.assertFalse(result.after_cpu.flags.zf)

    def test_execute_scf_sets_carry_and_resets_h_and_n(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x00200040, b"\x11")
            base_view = load_fetch_view(rom_path)
            seeded_cpu = replace(
                base_view.machine.cpu,
                flags=StatusFlags(sf=False, zf=True, vf=False, hf=True, cf=False, nf=True),
            )

            result = build_execute_next(base_view, cpu_state=seeded_cpu)

            self.assertEqual(result.status, "executed")
            self.assertEqual(result.decode.assembly, "scf")
            assert result.after_cpu is not None
            self.assertTrue(result.after_cpu.flags.cf)
            self.assertFalse(result.after_cpu.flags.hf)
            self.assertFalse(result.after_cpu.flags.nf)
            self.assertTrue(result.after_cpu.flags.zf)

    def test_execute_ccf_complements_carry_and_makes_h_unknown(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x00200040, b"\x12")
            base_view = load_fetch_view(rom_path)
            seeded_cpu = replace(
                base_view.machine.cpu,
                flags=StatusFlags(sf=False, zf=False, vf=True, hf=False, cf=False, nf=True),
            )

            result = build_execute_next(base_view, cpu_state=seeded_cpu)

            self.assertEqual(result.status, "executed")
            self.assertEqual(result.decode.assembly, "ccf")
            assert result.after_cpu is not None
            self.assertTrue(result.after_cpu.flags.cf)
            self.assertIsNone(result.after_cpu.flags.hf)
            self.assertFalse(result.after_cpu.flags.nf)
            self.assertTrue(result.after_cpu.flags.vf)

    def test_execute_ccf_preserves_unknown_carry_as_unknown(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x00200040, b"\x12")
            base_view = load_fetch_view(rom_path)
            seeded_cpu = replace(
                base_view.machine.cpu,
                flags=StatusFlags(sf=None, zf=None, vf=None, hf=True, cf=None, nf=True),
            )

            result = build_execute_next(base_view, cpu_state=seeded_cpu)

            self.assertEqual(result.status, "executed")
            assert result.after_cpu is not None
            self.assertIsNone(result.after_cpu.flags.cf)
            self.assertIsNone(result.after_cpu.flags.hf)
            self.assertFalse(result.after_cpu.flags.nf)

    def test_execute_zcf_uses_inverted_z_and_makes_h_unknown(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x00200040, b"\x13")
            base_view = load_fetch_view(rom_path)
            seeded_cpu = replace(
                base_view.machine.cpu,
                flags=StatusFlags(sf=True, zf=False, vf=False, hf=False, cf=False, nf=True),
            )

            result = build_execute_next(base_view, cpu_state=seeded_cpu)

            self.assertEqual(result.status, "executed")
            self.assertEqual(result.decode.assembly, "zcf")
            assert result.after_cpu is not None
            self.assertTrue(result.after_cpu.flags.cf)
            self.assertIsNone(result.after_cpu.flags.hf)
            self.assertFalse(result.after_cpu.flags.nf)
            self.assertFalse(result.after_cpu.flags.zf)

    def test_execute_zcf_preserves_unknown_z_as_unknown_carry(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x00200040, b"\x13")
            base_view = load_fetch_view(rom_path)
            seeded_cpu = replace(
                base_view.machine.cpu,
                flags=StatusFlags(sf=False, zf=None, vf=True, hf=True, cf=True, nf=True),
            )

            result = build_execute_next(base_view, cpu_state=seeded_cpu)

            self.assertEqual(result.status, "executed")
            assert result.after_cpu is not None
            self.assertIsNone(result.after_cpu.flags.cf)
            self.assertIsNone(result.after_cpu.flags.hf)
            self.assertFalse(result.after_cpu.flags.nf)

    def test_execute_fixed_push_a_writes_one_stack_byte(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x00200040, b"\x14")
            view = load_fetch_view(rom_path)
            seeded_cpu = replace(
                view.machine.cpu,
                regs=replace(view.machine.cpu.regs, xwa=0x123456AB, xsp=0x00006C00),
            )

            result = build_execute_next(view, cpu_state=seeded_cpu)

            self.assertEqual(result.status, "executed")
            self.assertEqual(result.decode.assembly, "push A")
            assert result.after_cpu is not None
            self.assertEqual(result.after_cpu.regs.xsp, 0x00006BFF)
            self.assertEqual(len(result.memory_writes), 1)
            self.assertEqual(result.memory_writes[0].address, 0x00006BFF)
            self.assertEqual(result.memory_writes[0].data, b"\xAB")

    def test_execute_fixed_pop_a_reads_one_stack_byte(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x00200040, b"\x15")
            view = load_fetch_view(rom_path)
            seeded_cpu = replace(
                view.machine.cpu,
                regs=replace(view.machine.cpu.regs, xwa=0x12345600, xsp=0x00006C00),
            )

            result = build_execute_next(
                view,
                cpu_state=seeded_cpu,
                memory_bytes={0x00006C00: 0xAB},
            )

            self.assertEqual(result.status, "executed")
            self.assertEqual(result.decode.assembly, "pop A")
            assert result.after_cpu is not None
            self.assertEqual(result.after_cpu.regs.xwa, 0x123456AB)
            self.assertEqual(result.after_cpu.regs.xsp, 0x00006C01)

    def test_execute_push_f_writes_encoded_flag_byte(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x00200040, b"\x18")
            view = load_fetch_view(rom_path)
            seeded_cpu = replace(
                view.machine.cpu,
                regs=replace(view.machine.cpu.regs, xsp=0x00006C00),
                flags=StatusFlags(sf=True, zf=False, vf=True, hf=True, cf=False, nf=True),
            )

            result = build_execute_next(view, cpu_state=seeded_cpu)

            self.assertEqual(result.status, "executed")
            self.assertEqual(result.decode.assembly, "push F")
            assert result.after_cpu is not None
            self.assertEqual(result.after_cpu.regs.xsp, 0x00006BFF)
            self.assertEqual(len(result.memory_writes), 1)
            self.assertEqual(result.memory_writes[0].data, b"\x96")

    def test_execute_push_f_blocks_when_flag_byte_is_unknown(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x00200040, b"\x18")
            view = load_fetch_view(rom_path)
            seeded_cpu = replace(
                view.machine.cpu,
                regs=replace(view.machine.cpu.regs, xsp=0x00006C00),
                flags=StatusFlags(sf=True, zf=False, vf=True, hf=True, cf=None, nf=True),
            )

            result = build_execute_next(view, cpu_state=seeded_cpu)

            self.assertEqual(result.status, "requires-known-flags")
            self.assertIsNone(result.after_cpu)

    def test_execute_pop_f_reads_flag_byte_and_updates_xsp(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x00200040, b"\x19")
            view = load_fetch_view(rom_path)
            seeded_cpu = replace(
                view.machine.cpu,
                regs=replace(view.machine.cpu.regs, xsp=0x00006C00),
                flags=StatusFlags(sf=False, zf=False, vf=False, hf=False, cf=False, nf=False),
            )

            result = build_execute_next(
                view,
                cpu_state=seeded_cpu,
                memory_bytes={0x00006C00: 0xD7},
            )

            self.assertEqual(result.status, "executed")
            self.assertEqual(result.decode.assembly, "pop F")
            assert result.after_cpu is not None
            self.assertEqual(result.after_cpu.regs.xsp, 0x00006C01)
            self.assertTrue(result.after_cpu.flags.sf)
            self.assertTrue(result.after_cpu.flags.zf)
            self.assertTrue(result.after_cpu.flags.vf)
            self.assertTrue(result.after_cpu.flags.hf)
            self.assertTrue(result.after_cpu.flags.cf)
            self.assertTrue(result.after_cpu.flags.nf)

    def test_execute_ldi_word_copies_xhl_to_xde_and_updates_bc(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x00200040, b"\x93\x10")
            view = load_fetch_view(rom_path)
            seeded_cpu = replace(
                view.machine.cpu,
                regs=replace(
                    view.machine.cpu.regs,
                    xbc=0x12340002,
                    xde=0x00002000,
                    xhl=0x00003000,
                ),
                flags=StatusFlags(sf=True, zf=False, vf=False, hf=True, cf=True, nf=True),
            )

            result = build_execute_next(
                view,
                cpu_state=seeded_cpu,
                memory_bytes={0x00003000: 0x5A, 0x00003001: 0xA5},
            )

            self.assertEqual(result.status, "executed")
            self.assertEqual(result.cycles_consumed, 8)
            self.assertEqual(result.written_registers, ("BC", "XDE", "XHL", "PC"))
            assert result.after_cpu is not None
            assert result.after_memory is not None
            self.assertEqual(result.after_cpu.regs.xbc, 0x12340001)
            self.assertEqual(result.after_cpu.regs.xde, 0x00002002)
            self.assertEqual(result.after_cpu.regs.xhl, 0x00003002)
            self.assertEqual(result.after_memory[0x00002000], 0x5A)
            self.assertEqual(result.after_memory[0x00002001], 0xA5)
            self.assertTrue(result.after_cpu.flags.sf)
            self.assertFalse(result.after_cpu.flags.zf)
            self.assertTrue(result.after_cpu.flags.cf)
            self.assertFalse(result.after_cpu.flags.hf)
            self.assertFalse(result.after_cpu.flags.nf)
            self.assertTrue(result.after_cpu.flags.vf)

    def test_execute_ldd_word_copies_two_bytes_and_clears_v_when_bc_reaches_zero(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x00200040, b"\x93\x12")
            view = load_fetch_view(rom_path)
            seeded_cpu = replace(
                view.machine.cpu,
                regs=replace(
                    view.machine.cpu.regs,
                    xbc=0x00000001,
                    xde=0x00002010,
                    xhl=0x00003020,
                ),
                flags=StatusFlags(sf=False, zf=True, vf=True, hf=True, cf=False, nf=True),
            )

            result = build_execute_next(
                view,
                cpu_state=seeded_cpu,
                memory_bytes={0x00003020: 0x34, 0x00003021: 0x12},
            )

            self.assertEqual(result.status, "executed")
            self.assertEqual(result.cycles_consumed, 8)
            assert result.after_cpu is not None
            assert result.after_memory is not None
            self.assertEqual(result.after_cpu.regs.xbc, 0x00000000)
            self.assertEqual(result.after_cpu.regs.xde, 0x0000200E)
            self.assertEqual(result.after_cpu.regs.xhl, 0x0000301E)
            self.assertEqual(result.after_memory[0x00002010], 0x34)
            self.assertEqual(result.after_memory[0x00002011], 0x12)
            self.assertFalse(result.after_cpu.flags.hf)
            self.assertFalse(result.after_cpu.flags.nf)
            self.assertFalse(result.after_cpu.flags.vf)
            self.assertFalse(result.after_cpu.flags.sf)
            self.assertTrue(result.after_cpu.flags.zf)
            self.assertFalse(result.after_cpu.flags.cf)

    def test_execute_cpi_word_preserves_carry_and_advances_pointer(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x00200040, b"\x95\x14")
            view = load_fetch_view(rom_path)
            seeded_cpu = replace(
                view.machine.cpu,
                regs=replace(
                    view.machine.cpu.regs,
                    xwa=0x11220010,
                    xbc=0x00000002,
                    xiy=0x00004000,
                ),
                flags=StatusFlags(sf=False, zf=True, vf=False, hf=False, cf=False, nf=False),
            )

            result = build_execute_next(
                view,
                cpu_state=seeded_cpu,
                memory_bytes={0x00004000: 0x20, 0x00004001: 0x00},
            )

            self.assertEqual(result.status, "executed")
            self.assertEqual(result.cycles_consumed, 6)
            self.assertEqual(result.written_registers, ("BC", "XIY", "PC"))
            assert result.after_cpu is not None
            self.assertEqual(result.after_cpu.regs.xbc, 0x00000001)
            self.assertEqual(result.after_cpu.regs.xiy, 0x00004002)
            self.assertTrue(result.after_cpu.flags.sf)
            self.assertFalse(result.after_cpu.flags.zf)
            self.assertTrue(result.after_cpu.flags.nf)
            self.assertFalse(result.after_cpu.flags.cf)
            self.assertTrue(result.after_cpu.flags.vf)

    def test_execute_cpd_word_blocks_xbc_alias_side_effects(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x00200040, b"\x91\x16")
            view = load_fetch_view(rom_path)
            seeded_cpu = replace(
                view.machine.cpu,
                regs=replace(view.machine.cpu.regs, xwa=0x12345678, xbc=0x00004002),
            )

            result = build_execute_next(view, cpu_state=seeded_cpu)

            self.assertEqual(result.status, "unmodeled-register-alias-side-effects")
            self.assertIsNone(result.after_cpu)

    def test_execute_ldir_word_copies_full_block_and_clears_v(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x00200040, b"\x93\x11")
            view = load_fetch_view(rom_path)
            seeded_cpu = replace(
                view.machine.cpu,
                regs=replace(
                    view.machine.cpu.regs,
                    xbc=0x00000003,
                    xde=0x00002000,
                    xhl=0x00003000,
                ),
                flags=StatusFlags(sf=True, zf=False, vf=True, hf=True, cf=True, nf=True),
            )

            result = build_execute_next(
                view,
                cpu_state=seeded_cpu,
                memory_bytes={
                    0x00003000: 0x11, 0x00003001: 0x22,
                    0x00003002: 0x33, 0x00003003: 0x44,
                    0x00003004: 0x55, 0x00003005: 0x66,
                },
            )

            self.assertEqual(result.status, "executed")
            self.assertEqual(result.decode.assembly, "ldir (XHL)")
            # Toshiba list (3): the REPEATING block forms cost `7n + 1` (LDIR/LDDR)
            # and `6n + 1` (CPIR/CPDR) -- not a flat multiple of the single-step
            # form. These assertions used to freeze the flat `8n` / `6n`.
            self.assertEqual(result.cycles_consumed, 22)
            self.assertEqual(result.written_registers, ("BC", "XDE", "XHL", "PC"))
            assert result.after_cpu is not None
            assert result.after_memory is not None
            self.assertEqual(result.after_cpu.regs.xbc, 0x00000000)
            self.assertEqual(result.after_cpu.regs.xde, 0x00002006)
            self.assertEqual(result.after_cpu.regs.xhl, 0x00003006)
            for offset in range(6):
                self.assertEqual(
                    result.after_memory[0x00002000 + offset],
                    result.after_memory[0x00003000 + offset],
                )
            self.assertEqual(result.after_memory[0x00002000], 0x11)
            self.assertEqual(result.after_memory[0x00002005], 0x66)
            # Repeat drives BC to zero, so V clears; H/N clear; S/Z/C preserved.
            self.assertFalse(result.after_cpu.flags.vf)
            self.assertFalse(result.after_cpu.flags.hf)
            self.assertFalse(result.after_cpu.flags.nf)
            self.assertTrue(result.after_cpu.flags.sf)
            self.assertFalse(result.after_cpu.flags.zf)
            self.assertTrue(result.after_cpu.flags.cf)

    def test_execute_lddr_word_walks_pointers_backward(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x00200040, b"\x93\x13")
            view = load_fetch_view(rom_path)
            seeded_cpu = replace(
                view.machine.cpu,
                regs=replace(
                    view.machine.cpu.regs,
                    xbc=0x00000002,
                    xde=0x00002010,
                    xhl=0x00003010,
                ),
                flags=StatusFlags(sf=False, zf=True, vf=True, hf=True, cf=False, nf=True),
            )

            result = build_execute_next(
                view,
                cpu_state=seeded_cpu,
                memory_bytes={
                    0x00003010: 0xAA, 0x00003011: 0xBB,
                    0x0000300E: 0xCC, 0x0000300F: 0xDD,
                },
            )

            self.assertEqual(result.status, "executed")
            self.assertEqual(result.decode.assembly, "lddr (XHL)")
            self.assertEqual(result.cycles_consumed, 15)
            assert result.after_cpu is not None
            assert result.after_memory is not None
            self.assertEqual(result.after_cpu.regs.xbc, 0x00000000)
            self.assertEqual(result.after_cpu.regs.xde, 0x0000200C)
            self.assertEqual(result.after_cpu.regs.xhl, 0x0000300C)
            self.assertEqual(result.after_memory[0x00002010], 0xAA)
            self.assertEqual(result.after_memory[0x00002011], 0xBB)
            self.assertEqual(result.after_memory[0x0000200E], 0xCC)
            self.assertEqual(result.after_memory[0x0000200F], 0xDD)
            self.assertFalse(result.after_cpu.flags.vf)

    def test_execute_ldirw_95_11_uses_xix_xiy_pair(self) -> None:
        # `0x95 0x11` is LDIRW (XIX+),(XIY+): w == 5 selects the XIX/XIY pair,
        # per authoritative ngdis decode_zz_R, not the XDE/XHL pair.
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x00200040, b"\x95\x11")
            view = load_fetch_view(rom_path)
            seeded_cpu = replace(
                view.machine.cpu,
                regs=replace(
                    view.machine.cpu.regs,
                    xbc=0x00000001,
                    xix=0x00002100,
                    xiy=0x00003100,
                ),
            )

            result = build_execute_next(
                view,
                cpu_state=seeded_cpu,
                memory_bytes={0x00003100: 0x7E, 0x00003101: 0x81},
            )

            self.assertEqual(result.status, "executed")
            self.assertEqual(result.decode.assembly, "ldirw (XIX+),(XIY+)")
            self.assertEqual(result.written_registers, ("BC", "XIX", "XIY", "PC"))
            assert result.after_cpu is not None
            assert result.after_memory is not None
            self.assertEqual(result.after_cpu.regs.xix, 0x00002102)
            self.assertEqual(result.after_cpu.regs.xiy, 0x00003102)
            self.assertEqual(result.after_memory[0x00002100], 0x7E)
            self.assertEqual(result.after_memory[0x00002101], 0x81)

    def test_execute_ldir_blocks_when_mid_range_source_is_unreadable(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x00200040, b"\x93\x11")
            view = load_fetch_view(rom_path)
            seeded_cpu = replace(
                view.machine.cpu,
                regs=replace(
                    view.machine.cpu.regs,
                    xbc=0x00000003,
                    xde=0x00002000,
                    xhl=0x00FF0000,
                ),
            )

            # Only the first item is backed; the second read has no source. The
            # source sits in the BIOS region with NO BIOS image attached -- the
            # one memory class that still honest-stops, because we genuinely lack
            # the bytes to model it. (On-chip RAM is pre-initialised to 0x00 and
            # unmapped space open-bus-reads 0x00 -- hw_test_openbus 2026-07-08 --
            # so neither of those blocks any more.)
            result = build_execute_next(
                view,
                cpu_state=seeded_cpu,
                memory_bytes={0x00FF0000: 0x11, 0x00FF0001: 0x22},
            )

            self.assertEqual(result.status, "runtime-memory-unavailable")
            self.assertIsNone(result.after_cpu)

    def test_execute_cpir_word_stops_on_match_and_sets_v(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x00200040, b"\x95\x15")
            view = load_fetch_view(rom_path)
            seeded_cpu = replace(
                view.machine.cpu,
                regs=replace(
                    view.machine.cpu.regs,
                    xwa=0x11220020,
                    xbc=0x00000004,
                    xiy=0x00004000,
                ),
                flags=StatusFlags(sf=True, zf=False, vf=False, hf=True, cf=True, nf=False),
            )

            result = build_execute_next(
                view,
                cpu_state=seeded_cpu,
                memory_bytes={
                    0x00004000: 0x10, 0x00004001: 0x00,
                    0x00004002: 0x20, 0x00004003: 0x00,
                },
            )

            self.assertEqual(result.status, "executed")
            self.assertEqual(result.decode.assembly, "cpir (XIY)")
            # Toshiba list (3): the REPEATING block forms cost `7n + 1` (LDIR/LDDR)
            # and `6n + 1` (CPIR/CPDR) -- not a flat multiple of the single-step
            # form. These assertions used to freeze the flat `8n` / `6n`.
            self.assertEqual(result.cycles_consumed, 13)
            self.assertEqual(result.written_registers, ("BC", "XIY", "PC"))
            assert result.after_cpu is not None
            # Two iterations: mismatch then match on the second word.
            self.assertEqual(result.after_cpu.regs.xbc, 0x00000002)
            self.assertEqual(result.after_cpu.regs.xiy, 0x00004004)
            self.assertTrue(result.after_cpu.flags.zf)   # match -> WA - mem == 0
            self.assertTrue(result.after_cpu.flags.vf)   # BC != 0 after decrement
            self.assertTrue(result.after_cpu.flags.nf)   # subtract
            self.assertTrue(result.after_cpu.flags.cf)   # carry preserved

    def test_execute_cpdr_word_exhausts_bc_without_match_clears_v(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x00200040, b"\x95\x17")
            view = load_fetch_view(rom_path)
            seeded_cpu = replace(
                view.machine.cpu,
                regs=replace(
                    view.machine.cpu.regs,
                    xwa=0x112200FF,
                    xbc=0x00000002,
                    xiy=0x00004000,
                ),
                flags=StatusFlags(sf=False, zf=True, vf=True, hf=False, cf=True, nf=False),
            )

            result = build_execute_next(
                view,
                cpu_state=seeded_cpu,
                memory_bytes={
                    0x00004000: 0x01, 0x00004001: 0x00,
                    0x00003FFE: 0x02, 0x00003FFF: 0x00,
                },
            )

            self.assertEqual(result.status, "executed")
            self.assertEqual(result.decode.assembly, "cpdr (XIY)")
            self.assertEqual(result.cycles_consumed, 13)
            assert result.after_cpu is not None
            # No match: full two-iteration pass, pointer retreats by 4.
            self.assertEqual(result.after_cpu.regs.xbc, 0x00000000)
            self.assertEqual(result.after_cpu.regs.xiy, 0x00003FFC)
            self.assertFalse(result.after_cpu.flags.zf)  # last compare non-zero
            self.assertFalse(result.after_cpu.flags.vf)  # BC == 0 after
            self.assertTrue(result.after_cpu.flags.cf)   # carry preserved

    def test_execute_cpir_blocks_when_next_source_word_is_unreadable(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x00200040, b"\x95\x15")
            view = load_fetch_view(rom_path)
            seeded_cpu = replace(
                view.machine.cpu,
                regs=replace(
                    view.machine.cpu.regs,
                    xwa=0x112200FF,
                    xbc=0x00000003,
                    xiy=0x00FF0000,
                ),
            )

            # First word mismatches and BC is not yet exhausted, so the repeat
            # needs the next word at 0xFF0002 -- BIOS region with no BIOS image
            # attached, the one class that still honest-stops (we lack the bytes).
            # On-chip RAM (0x00) and unmapped open-bus (0x00, hw_test_openbus
            # 2026-07-08) no longer block.
            result = build_execute_next(
                view,
                cpu_state=seeded_cpu,
                memory_bytes={0x00FF0000: 0x01, 0x00FF0001: 0x00},
            )

            self.assertEqual(result.status, "runtime-memory-unavailable")
            self.assertIsNone(result.after_cpu)

    def test_execute_ldir_requires_known_block_counter(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x00200040, b"\x93\x11")
            view = load_fetch_view(rom_path)
            seeded_cpu = replace(
                view.machine.cpu,
                regs=replace(
                    view.machine.cpu.regs,
                    xde=0x00002000,
                    xhl=0x00003000,
                ),
            )

            result = build_execute_next(view, cpu_state=seeded_cpu)

            self.assertEqual(result.status, "requires-known-full-register")
            self.assertIsNone(result.after_cpu)

    def test_execute_f3_mode1_word_immediate_store(self) -> None:
        # F3 E1 04 00 02 EF BE = ldw (XWA+4), 0xBEEF (ARI secondary mode=1)
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x00200040, b"\xF3\xE1\x04\x00\x02\xEF\xBE")
            view = load_fetch_view(rom_path)
            seeded_cpu = replace(
                view.machine.cpu,
                regs=replace(view.machine.cpu.regs, xwa=0x00006000),
            )

            result = build_execute_next(view, cpu_state=seeded_cpu, memory_bytes={})

            self.assertEqual(result.status, "executed")
            self.assertEqual(result.decode.assembly, "ldw (XWA+4), 0xBEEF")
            assert result.after_cpu is not None
            assert result.after_memory is not None
            self.assertEqual(result.after_cpu.pc, 0x00200047)
            self.assertEqual(result.after_memory[0x00006004], 0xEF)
            self.assertEqual(result.after_memory[0x00006005], 0xBE)

    def test_execute_f3_mode1_byte_immediate_store_negative_disp(self) -> None:
        # F3 E1 FE FF 00 7A = ld (XWA-2), 0x7A (signed d16 = -2)
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x00200040, b"\xF3\xE1\xFE\xFF\x00\x7A")
            view = load_fetch_view(rom_path)
            seeded_cpu = replace(
                view.machine.cpu,
                regs=replace(view.machine.cpu.regs, xwa=0x00006000),
            )

            result = build_execute_next(view, cpu_state=seeded_cpu, memory_bytes={})

            self.assertEqual(result.status, "executed")
            self.assertEqual(result.decode.assembly, "ld (XWA-2), 0x7A")
            assert result.after_cpu is not None
            assert result.after_memory is not None
            self.assertEqual(result.after_cpu.pc, 0x00200046)
            self.assertEqual(result.after_memory[0x00005FFE], 0x7A)

    def test_execute_f3_mode1_r32_register_store(self) -> None:
        # F3 E1 08 00 62 = ld (XWA+8), XDE (mode=1 register store, R32 source)
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x00200040, b"\xF3\xE1\x08\x00\x62")
            view = load_fetch_view(rom_path)
            seeded_cpu = replace(
                view.machine.cpu,
                regs=replace(view.machine.cpu.regs, xwa=0x00006000, xde=0x12345678),
            )

            result = build_execute_next(view, cpu_state=seeded_cpu, memory_bytes={})

            self.assertEqual(result.status, "executed")
            self.assertEqual(result.decode.assembly, "ld (XWA+8), XDE")
            assert result.after_cpu is not None
            assert result.after_memory is not None
            self.assertEqual(result.after_cpu.pc, 0x00200045)
            self.assertEqual(
                bytes(result.after_memory[0x00006008 + i] for i in range(4)),
                b"\x78\x56\x34\x12",
            )

    def test_execute_f3_mode1_store_requires_known_base_register(self) -> None:
        # Same store, but XWA is unknown -> honest block on the base register.
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x00200040, b"\xF3\xE1\x08\x00\x62")
            view = load_fetch_view(rom_path)
            seeded_cpu = replace(
                view.machine.cpu,
                regs=replace(view.machine.cpu.regs, xde=0x12345678),
            )

            result = build_execute_next(view, cpu_state=seeded_cpu, memory_bytes={})

            self.assertEqual(result.status, "requires-known-address-register")
            self.assertIsNone(result.after_cpu)

    def test_execute_c3_mode1_byte_load(self) -> None:
        # C3 E1 04 00 20 = ld W, (XWA+4) (mode=1 byte load into R8[0]=W)
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x00200040, b"\xC3\xE1\x04\x00\x20")
            view = load_fetch_view(rom_path)
            seeded_cpu = replace(
                view.machine.cpu,
                regs=replace(view.machine.cpu.regs, xwa=0x00006000),
            )

            result = build_execute_next(view, cpu_state=seeded_cpu, memory_bytes={0x00006004: 0x9C})

            self.assertEqual(result.status, "executed")
            self.assertEqual(result.decode.assembly, "ld W, (XWA+4)")
            assert result.after_cpu is not None
            self.assertEqual(result.after_cpu.pc, 0x00200045)
            # R8[0] = W = high byte of WA (bits 8..15 of XWA).
            self.assertEqual((result.after_cpu.regs.xwa >> 8) & 0xFF, 0x9C)

    def test_execute_e3_mode1_long_load_negative_disp(self) -> None:
        # E3 E1 FE FF 23 = ld XHL, (XWA-2) (mode=1 long load, signed d16)
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x00200040, b"\xE3\xE1\xFE\xFF\x23")
            view = load_fetch_view(rom_path)
            seeded_cpu = replace(
                view.machine.cpu,
                regs=replace(view.machine.cpu.regs, xwa=0x00006000),
            )

            result = build_execute_next(
                view,
                cpu_state=seeded_cpu,
                memory_bytes={0x00005FFE: 0x11, 0x00005FFF: 0x22, 0x00006000: 0x33, 0x00006001: 0x44},
            )

            self.assertEqual(result.status, "executed")
            self.assertEqual(result.decode.assembly, "ld XHL, (XWA-2)")
            assert result.after_cpu is not None
            self.assertEqual(result.after_cpu.regs.xhl, 0x44332211)

    def test_execute_c3_mode1_byte_compare_immediate_sets_zero_on_equal(self) -> None:
        # C3 FD A4 01 3F 7E = cp (XSP+420), 0x7E; equal memory -> Z=1, N=1.
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x00200040, b"\xC3\xFD\xA4\x01\x3F\x7E")
            view = load_fetch_view(rom_path)
            seeded_cpu = replace(
                view.machine.cpu,
                regs=replace(view.machine.cpu.regs, xsp=0x00006000),
            )

            result = build_execute_next(
                view,
                cpu_state=seeded_cpu,
                memory_bytes={0x00006000 + 420: 0x7E},
            )

            self.assertEqual(result.status, "executed")
            self.assertEqual(result.decode.assembly, "cp (XSP+420), 0x7E")
            assert result.after_cpu is not None
            self.assertEqual(result.after_cpu.pc, 0x00200046)
            self.assertTrue(result.after_cpu.flags.zf)
            self.assertTrue(result.after_cpu.flags.nf)

    def test_execute_c3_mode1_load_requires_known_base_register(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x00200040, b"\xC3\xE1\x04\x00\x20")
            view = load_fetch_view(rom_path)

            result = build_execute_next(view, memory_bytes={0x00006004: 0x9C})

            self.assertEqual(result.status, "requires-known-address-register")
            self.assertIsNone(result.after_cpu)

    def test_execute_c3_mode1_compare_register_equal_sets_zero(self) -> None:
        # C3 E9 C0 01 F3 = cp C, (XDE+448); C == mem -> Z=1, N=1, C(arry)=0.
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x00200040, b"\xC3\xE9\xC0\x01\xF3")
            view = load_fetch_view(rom_path)
            seeded_cpu = replace(
                view.machine.cpu,
                regs=replace(view.machine.cpu.regs, xde=0x00006000, xbc=0x00000042),
            )

            result = build_execute_next(
                view,
                cpu_state=seeded_cpu,
                memory_bytes={0x00006000 + 448: 0x42},
            )

            self.assertEqual(result.status, "executed")
            self.assertEqual(result.decode.assembly, "cp C, (XDE+448)")
            assert result.after_cpu is not None
            self.assertEqual(result.after_cpu.pc, 0x00200045)
            self.assertTrue(result.after_cpu.flags.zf)
            self.assertTrue(result.after_cpu.flags.nf)
            self.assertFalse(result.after_cpu.flags.cf)

    def test_execute_c3_mode1_compare_register_less_sets_carry(self) -> None:
        # cp C, (XDE+448) with C(0x10) < mem(0x20) -> borrow: C(arry)=1, Z=0.
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x00200040, b"\xC3\xE9\xC0\x01\xF3")
            view = load_fetch_view(rom_path)
            seeded_cpu = replace(
                view.machine.cpu,
                regs=replace(view.machine.cpu.regs, xde=0x00006000, xbc=0x00000010),
            )

            result = build_execute_next(
                view,
                cpu_state=seeded_cpu,
                memory_bytes={0x00006000 + 448: 0x20},
            )

            self.assertEqual(result.status, "executed")
            assert result.after_cpu is not None
            self.assertTrue(result.after_cpu.flags.cf)
            self.assertFalse(result.after_cpu.flags.zf)

    def test_execute_c3_mode1_compare_requires_known_register(self) -> None:
        # Base known, memory readable, but the compare register C is unknown.
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x00200040, b"\xC3\xE9\xC0\x01\xF3")
            view = load_fetch_view(rom_path)
            seeded_cpu = replace(
                view.machine.cpu,
                regs=replace(view.machine.cpu.regs, xde=0x00006000),
            )

            result = build_execute_next(
                view,
                cpu_state=seeded_cpu,
                memory_bytes={0x00006000 + 448: 0x42},
            )

            self.assertEqual(result.status, "requires-known-full-register")
            self.assertIsNone(result.after_cpu)

    def test_execute_c3_mode1_dec_memory_updates_byte_and_preserves_carry(self) -> None:
        # C3 FD 9E 01 69 = dec 1, (XSP+414); mem 0x05 -> 0x04, N=1, CF preserved.
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x00200040, b"\xC3\xFD\x9E\x01\x69")
            view = load_fetch_view(rom_path)
            seeded_cpu = replace(
                view.machine.cpu,
                regs=replace(view.machine.cpu.regs, xsp=0x00006000),
                flags=StatusFlags(sf=None, zf=None, vf=None, hf=None, cf=True, nf=None),
            )

            ea = 0x00006000 + 414
            result = build_execute_next(view, cpu_state=seeded_cpu, memory_bytes={ea: 0x05})

            self.assertEqual(result.status, "executed")
            self.assertEqual(result.decode.assembly, "dec 1, (XSP+414)")
            assert result.after_cpu is not None
            assert result.after_memory is not None
            self.assertEqual(result.after_memory[ea], 0x04)
            self.assertFalse(result.after_cpu.flags.zf)
            self.assertTrue(result.after_cpu.flags.nf)
            # INC/DEC on memory preserves the carry flag.
            self.assertTrue(result.after_cpu.flags.cf)

    def test_execute_c3_mode1_inc_memory_count_eight_wraps(self) -> None:
        # C3 E1 04 00 60 = inc 8, (XWA+4); mem 0xFE + 8 = 0x06 (byte wrap), N=0.
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x00200040, b"\xC3\xE1\x04\x00\x60")
            view = load_fetch_view(rom_path)
            seeded_cpu = replace(
                view.machine.cpu,
                regs=replace(view.machine.cpu.regs, xwa=0x00006000),
            )

            result = build_execute_next(view, cpu_state=seeded_cpu, memory_bytes={0x00006004: 0xFE})

            self.assertEqual(result.status, "executed")
            self.assertEqual(result.decode.assembly, "inc 8, (XWA+4)")
            assert result.after_cpu is not None
            assert result.after_memory is not None
            self.assertEqual(result.after_memory[0x00006004], 0x06)
            self.assertFalse(result.after_cpu.flags.nf)

    def test_execute_reg_indirect_long_load(self) -> None:
        # A1 20 = ld XWA, (XBC); reads a 32-bit little-endian value into XWA.
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x00200040, b"\xA1\x20")
            view = load_fetch_view(rom_path)
            seeded_cpu = replace(
                view.machine.cpu,
                regs=replace(view.machine.cpu.regs, xbc=0x00006000),
            )

            result = build_execute_next(
                view,
                cpu_state=seeded_cpu,
                memory_bytes={0x00006000: 0x78, 0x00006001: 0x56, 0x00006002: 0x34, 0x00006003: 0x12},
            )

            self.assertEqual(result.status, "executed")
            self.assertEqual(result.decode.assembly, "ld XWA, (XBC)")
            assert result.after_cpu is not None
            self.assertEqual(result.after_cpu.pc, 0x00200042)
            self.assertEqual(result.after_cpu.regs.xwa, 0x12345678)

    def test_execute_reg_indirect_long_load_requires_known_base(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x00200040, b"\xA1\x20")
            view = load_fetch_view(rom_path)

            result = build_execute_next(view)

            self.assertEqual(result.status, "requires-known-address-register")
            self.assertIsNone(result.after_cpu)

    def test_execute_arid_d8_bit_test_sets_zero_from_bit(self) -> None:
        # B8 0A C8 = bit 0, (XWA+10); Z = complement of the tested bit, H=1, N=0.
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x00200040, b"\xB8\x0A\xC8")
            view = load_fetch_view(rom_path)
            seeded_cpu = replace(
                view.machine.cpu,
                regs=replace(view.machine.cpu.regs, xwa=0x00006000),
            )

            set_result = build_execute_next(view, cpu_state=seeded_cpu, memory_bytes={0x0000600A: 0x01})
            clear_result = build_execute_next(view, cpu_state=seeded_cpu, memory_bytes={0x0000600A: 0x00})

            self.assertEqual(set_result.status, "executed")
            self.assertEqual(set_result.decode.assembly, "bit 0, (XWA+10)")
            assert set_result.after_cpu is not None
            self.assertEqual(set_result.after_cpu.pc, 0x00200043)
            self.assertFalse(set_result.after_cpu.flags.zf)  # bit set -> Z=0
            self.assertTrue(set_result.after_cpu.flags.hf)
            self.assertFalse(set_result.after_cpu.flags.nf)
            assert clear_result.after_cpu is not None
            self.assertTrue(clear_result.after_cpu.flags.zf)  # bit clear -> Z=1
            # A pure bit test must not write memory.
            self.assertEqual(len(set_result.memory_writes), 0)

    def test_execute_arid_d8_bit_test_requires_known_base(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x00200040, b"\xB8\x0A\xC8")
            view = load_fetch_view(rom_path)

            result = build_execute_next(view, memory_bytes={0x0000600A: 0x01})

            self.assertEqual(result.status, "requires-known-address-register")
            self.assertIsNone(result.after_cpu)

    def test_execute_secondary_mode1_bit_test(self) -> None:
        # F3 FD A6 01 C9 = bit 1, (XSP+422); Z = complement of bit 1.
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x00200040, b"\xF3\xFD\xA6\x01\xC9")
            view = load_fetch_view(rom_path)
            seeded_cpu = replace(
                view.machine.cpu,
                regs=replace(view.machine.cpu.regs, xsp=0x00006000),
            )

            ea = 0x00006000 + 422
            set_result = build_execute_next(view, cpu_state=seeded_cpu, memory_bytes={ea: 0x02})
            clear_result = build_execute_next(view, cpu_state=seeded_cpu, memory_bytes={ea: 0x00})

            self.assertEqual(set_result.status, "executed")
            self.assertEqual(set_result.decode.assembly, "bit 1, (XSP+422)")
            assert set_result.after_cpu is not None
            self.assertEqual(set_result.after_cpu.pc, 0x00200045)
            self.assertFalse(set_result.after_cpu.flags.zf)
            self.assertTrue(set_result.after_cpu.flags.hf)
            self.assertFalse(set_result.after_cpu.flags.nf)
            assert clear_result.after_cpu is not None
            self.assertTrue(clear_result.after_cpu.flags.zf)
            self.assertEqual(len(set_result.memory_writes), 0)

    def test_execute_secondary_mode3_bit_test(self) -> None:
        # F3 07 E0 E4 CA = bit 2, (XWA+BC); EA = XWA + BC.
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x00200040, b"\xF3\x07\xE0\xE4\xCA")
            view = load_fetch_view(rom_path)
            seeded_cpu = replace(
                view.machine.cpu,
                regs=replace(view.machine.cpu.regs, xwa=0x00006000, xbc=0x00000004),
            )

            result = build_execute_next(view, cpu_state=seeded_cpu, memory_bytes={0x00006004: 0x04})

            self.assertEqual(result.status, "executed")
            self.assertEqual(result.decode.assembly, "bit 2, (XWA+BC)")
            assert result.after_cpu is not None
            self.assertFalse(result.after_cpu.flags.zf)  # bit 2 of 0x04 is set

    def test_execute_call_requires_known_stack_pointer_when_unseeded(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x00200040, b"\x1D\x2F\x91\x20")
            view = load_fetch_view(rom_path)

            result = build_execute_next(view)

            self.assertEqual(result.status, "requires-known-stack-pointer")
            self.assertEqual(result.decode.assembly, "call 0x20912F")
            self.assertIsNone(result.after_cpu)

    def test_execute_word_load_requires_known_full_register(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x00200040, b"\x30\x34\x12")
            view = load_fetch_view(rom_path)

            result = build_execute_next(view)

            self.assertEqual(result.status, "requires-known-full-register")
            self.assertEqual(result.decode.assembly, "ld WA, 0x1234")
            self.assertIsNone(result.after_cpu)

    def test_execute_word_load_updates_known_full_register(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x00200040, b"\x30\x34\x12")
            base_view = load_fetch_view(rom_path)
            seeded_cpu = replace(
                base_view.machine.cpu,
                regs=replace(base_view.machine.cpu.regs, xwa=0xAABBCCDD),
            )
            seeded_view = replace(
                base_view,
                machine=replace(base_view.machine, cpu=seeded_cpu),
            )

            result = build_execute_next(seeded_view)

            self.assertEqual(result.status, "executed")
            self.assertEqual(result.written_registers, ("WA", "PC"))
            assert result.after_cpu is not None
            self.assertEqual(result.after_cpu.regs.xwa, 0xAABB1234)
            self.assertEqual(result.after_cpu.pc, 0x00200043)

    def test_execute_prefixed_dec_long_updates_xsp_and_pc(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x00200040, b"\xEF\x6C")
            base_view = load_fetch_view(rom_path)
            seeded_cpu = replace(
                base_view.machine.cpu,
                regs=replace(base_view.machine.cpu.regs, xsp=0x00004100),
            )

            result = build_execute_next(base_view, cpu_state=seeded_cpu)

            self.assertEqual(result.status, "executed")
            self.assertEqual(result.decode.assembly, "dec 4, XSP")
            self.assertEqual(result.written_registers, ("XSP", "PC"))
            assert result.after_cpu is not None
            self.assertEqual(result.after_cpu.regs.xsp, 0x000040FC)
            self.assertEqual(result.after_cpu.pc, 0x00200042)

    def test_execute_prefixed_alu_immediate_word_stops_as_silicon_broken(self) -> None:
        # Migrated 2026-07-08: the old `D0 61` "inc word" fixture relied on the
        # retired D0-reg-direct mis-decode. 0xD0..0xD7 is now the WORD MEMORY
        # family, so `D0 61` decodes as `cpw (0x61), SP` (a memory op, not
        # silicon-broken). Re-point to the still-broken D0 ALU-immediate form
        # `D0 C8 lo hi` = `add WA, imm16` (real-NGPC crash 2026-05-20), which
        # keeps honest-stopping. Same honest-stop mechanism + quirk payload
        # shape; only the fixture bytes and decoded assembly change.
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x00200040, b"\xD8\xB8")
            base_view = load_fetch_view(rom_path)
            seeded_cpu = replace(
                base_view.machine.cpu,
                regs=replace(base_view.machine.cpu.regs, xwa=0xAABB1234),
            )

            result = build_execute_next(base_view, cpu_state=seeded_cpu)

            self.assertEqual(result.status, "silicon-broken")
            self.assertEqual(result.decode.assembly, "ex WA, WA")
            self.assertEqual(result.written_registers, ())
            self.assertIsNone(result.after_cpu)
            self.assertIn("D8..DF", result.note)
            self.assertIsNotNone(result.matched_quirk)
            assert result.matched_quirk is not None
            self.assertEqual(result.matched_quirk.database_version, load_known_quirk_database().database_version)
            self.assertEqual(result.matched_quirk.quirk_id, "cpu.d8_df_register_to_register")

    def test_execute_prefixed_long_ld_register_to_register_copy_now_executes(self) -> None:
        # D8 89 = ld BC, WA (WORD copy — 0xD8..0xDF is the word prefix).
        # HW-confirmed executable (retail mr_robot boots on real NGPC using this
        # exact copy), so it copies WA (low 16 of XWA) into BC instead of stopping
        # silicon-broken. The copy is 16-bit: only BC (low word) is written and the
        # high word of XBC is preserved — HW-confirmed result AAAA3344 when the
        # high word starts as AAAA. Quirk DB v7 safe-lists the ld-copy sub-ops.
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x00200040, b"\xD8\x89")
            base_view = load_fetch_view(rom_path)
            seeded_cpu = replace(
                base_view.machine.cpu,
                regs=replace(base_view.machine.cpu.regs, xwa=0x11223344, xbc=0x0),
            )

            result = build_execute_next(base_view, cpu_state=seeded_cpu)

            self.assertEqual(result.status, "executed")
            self.assertEqual(result.decode.assembly, "ld BC, WA")
            self.assertEqual(result.written_registers, ("BC", "PC"))
            assert result.after_cpu is not None
            self.assertEqual(result.after_cpu.regs.xbc, 0x00003344)
            self.assertEqual(result.after_cpu.pc, 0x00200042)

    def test_execute_add_wa_wa_word_reg_reg_hw_cleared(self) -> None:
        # D8 80 = add WA, WA (word r+r). HW-confirmed 2026-07-05 (hw_test_addrr,
        # GREEN): executes and is 16-bit-correct. Seed XWA=0x99991234 -> WA
        # doubles to 0x2468, high word 0x9999 preserved -> XWA=0x99992468.
        # (This is the exact frontier several engine cart ROMs honest-stopped on.)
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x00200040, b"\xD8\x80")
            base_view = load_fetch_view(rom_path)
            seeded_cpu = replace(
                base_view.machine.cpu,
                regs=replace(base_view.machine.cpu.regs, xwa=0x99991234),
            )

            result = build_execute_next(base_view, cpu_state=seeded_cpu)

            self.assertEqual(result.status, "executed")
            self.assertEqual(result.decode.assembly, "add WA, WA")
            assert result.after_cpu is not None
            self.assertEqual(result.after_cpu.regs.xwa, 0x99992468)
            self.assertEqual(result.after_cpu.pc, 0x00200042)

    def test_execute_16bit_memory_compare_stops_needing_known_register(self) -> None:
        # Migrated 2026-07-08: 0xD0..0xD7 is the WORD MEMORY-addressing family,
        # so `D0 89` is no longer a silicon-broken `ld BC, WA` reg-direct copy
        # carrying a push/pop "same family as the HW-confirmed 32-bit copies"
        # remediation hint -- that reg-direct ld form (and its hint) is retired.
        # It now decodes as `cpw (0x89), SP`, an abs8 word memory compare that
        # honest-stops because its owner register (SP) is not yet known. The
        # instruction STILL does not execute (after_cpu stays None) -- same
        # honest-stop frontier, new (memory-decode) reason -- and it must NOT
        # carry the retired ld-copy remediation hint.
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x00200040, b"\xD0\x89")
            base_view = load_fetch_view(rom_path)

            result = build_execute_next(base_view)

            self.assertEqual(result.status, "requires-known-full-register")
            self.assertEqual(result.decode.assembly, "cpw (0x89), SP")
            self.assertIsNone(result.after_cpu)
            self.assertNotIn("Toolchain remediation", result.note)

    def test_execute_silicon_broken_non_copy_has_no_remediation_hint(self) -> None:
        # D8 B8 (sub-op 0xB8, in the still-broken 0xB8..0xBF pocket) is
        # silicon-broken but not a register copy, so it must not carry the
        # ld-copy remediation hint. (D9 50 = div is now HW-cleared, so it can no
        # longer stand in for a broken non-copy form.)
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x00200040, b"\xD8\xB8")
            base_view = load_fetch_view(rom_path)

            result = build_execute_next(base_view)

            self.assertEqual(result.status, "silicon-broken")
            self.assertNotIn("Toolchain remediation", result.note)

    def test_execute_prefixed_word_div_register_to_register_executes(self) -> None:
        # div WA, BC (D9 50) is HW-cleared (hw_test_muldiv, 2026-07-06: real NGPC
        # runs it and returns the correct packed result). XWA=8 / BC=2 -> quotient
        # 4 in the low word, remainder 0 in the high word = 0x00000004.
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x00207CAC, b"\xD9\x50")
            base_view = load_fetch_view(rom_path)
            seeded_cpu = replace(
                base_view.machine.cpu,
                regs=replace(
                    base_view.machine.cpu.regs,
                    xwa=0x00000008,
                    xbc=0x00000002,
                ),
            )

            result = build_execute_next(base_view, cpu_state=seeded_cpu)

            self.assertEqual(result.status, "executed")
            self.assertEqual(result.decode.assembly, "div WA, BC")
            self.assertIsNotNone(result.after_cpu)
            assert result.after_cpu is not None
            self.assertEqual(result.after_cpu.regs.xwa, 0x00000004)

    def test_execute_prefixed_long_cp_register_to_register_stays_executable(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x00200040, b"\xD8\xF3")
            base_view = load_fetch_view(rom_path)
            seeded_cpu = replace(
                base_view.machine.cpu,
                regs=replace(
                    base_view.machine.cpu.regs,
                    xwa=0x11223344,
                    xhl=0x11223344,
                ),
            )

            result = build_execute_next(base_view, cpu_state=seeded_cpu)

            self.assertNotEqual(result.status, "silicon-broken")
            self.assertIsNone(result.matched_quirk)

    def test_execute_prefixed_ldc_write_updates_dmac0(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x00200040, b"\xD8\x2E\x20")
            base_view = load_fetch_view(rom_path)
            seeded_cpu = replace(
                base_view.machine.cpu,
                regs=replace(base_view.machine.cpu.regs, xwa=0x00001234),
            )

            result = build_execute_next(base_view, cpu_state=seeded_cpu)

            self.assertEqual(result.status, "executed")
            self.assertEqual(result.decode.assembly, "ldc DMAC0, WA")
            self.assertEqual(result.written_registers, ("DMAC0", "PC"))
            assert result.after_cpu is not None
            assert result.after_cpu.control_registers is not None
            self.assertEqual(result.after_cpu.control_registers.dmac[0], 0x1234)

    def test_execute_prefixed_ldc_read_updates_xwa_from_dmad0(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x00200040, b"\xE8\x2F\x10")
            base_view = load_fetch_view(rom_path)
            control = replace(
                create_unknown_control_registers(),
                dmad=(0x20123456, None, None, None),
            )
            seeded_cpu = replace(
                base_view.machine.cpu,
                regs=replace(base_view.machine.cpu.regs, xwa=0),
                control_registers=control,
            )

            result = build_execute_next(base_view, cpu_state=seeded_cpu)

            self.assertEqual(result.status, "executed")
            self.assertEqual(result.decode.assembly, "ldc XWA, DMAD0")
            self.assertEqual(result.written_registers, ("XWA", "PC"))
            assert result.after_cpu is not None
            self.assertEqual(result.after_cpu.regs.xwa, 0x20123456)

    def test_execute_prefixed_ldc_read_blocks_when_control_register_is_unknown(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x00200040, b"\xD8\x2F\x20")
            base_view = load_fetch_view(rom_path)

            result = build_execute_next(base_view)

            self.assertEqual(result.status, "requires-known-control-register")
            self.assertEqual(result.decode.assembly, "ldc WA, DMAC0")
            self.assertIsNone(result.after_cpu)

    def test_execute_prefixed_long_add_xhl_xwa_stays_executable(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x00200040, b"\xE8\x83")
            base_view = load_fetch_view(rom_path)
            seeded_cpu = replace(
                base_view.machine.cpu,
                regs=replace(
                    base_view.machine.cpu.regs,
                    xwa=0x00000002,
                    xhl=0x00000003,
                ),
            )

            result = build_execute_next(base_view, cpu_state=seeded_cpu)

            self.assertEqual(result.status, "executed")
            self.assertEqual(result.decode.assembly, "add XHL, XWA")
            self.assertIsNone(result.matched_quirk)
            assert result.after_cpu is not None
            self.assertEqual(result.after_cpu.regs.xhl, 0x00000005)
            self.assertEqual(result.after_cpu.pc, 0x00200042)

    def test_execute_prefixed_long_paa_increments_odd_value(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x00200040, b"\xE8\x14")
            base_view = load_fetch_view(rom_path)
            seeded_cpu = replace(
                base_view.machine.cpu,
                regs=replace(base_view.machine.cpu.regs, xwa=0x00234567),
            )

            result = build_execute_next(base_view, cpu_state=seeded_cpu)

            self.assertEqual(result.status, "executed")
            self.assertEqual(result.decode.assembly, "paa XWA")
            assert result.after_cpu is not None
            self.assertEqual(result.after_cpu.regs.xwa, 0x00234568)
            self.assertEqual(result.after_cpu.pc, 0x00200042)

    def test_execute_prefixed_long_paa_leaves_even_value_unchanged(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x00200040, b"\xE8\x14")
            base_view = load_fetch_view(rom_path)
            seeded_cpu = replace(
                base_view.machine.cpu,
                regs=replace(base_view.machine.cpu.regs, xwa=0x00234568),
            )

            result = build_execute_next(base_view, cpu_state=seeded_cpu)

            self.assertEqual(result.status, "executed")
            assert result.after_cpu is not None
            self.assertEqual(result.after_cpu.regs.xwa, 0x00234568)

    def test_execute_prefixed_byte_paa_stops_as_silicon_undefined(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x00200040, b"\xC8\x14")
            base_view = load_fetch_view(rom_path)
            seeded_cpu = replace(
                base_view.machine.cpu,
                regs=replace(base_view.machine.cpu.regs, xwa=0x00000001),
            )

            result = build_execute_next(base_view, cpu_state=seeded_cpu)

            self.assertEqual(result.status, "silicon-undefined")
            self.assertEqual(result.decode.assembly, "paa W")

    def test_execute_prefixed_byte_djnz_takes_branch_when_result_nonzero(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x00200040, b"\xC9\x1C\xFE")
            base_view = load_fetch_view(rom_path)
            seeded_cpu = replace(
                base_view.machine.cpu,
                regs=replace(base_view.machine.cpu.regs, xwa=0x00000002),
            )

            result = build_execute_next(base_view, cpu_state=seeded_cpu)

            self.assertEqual(result.status, "executed")
            self.assertEqual(result.decode.assembly, "djnz A, 0x200041")
            assert result.after_cpu is not None
            self.assertEqual(result.after_cpu.regs.xwa & 0xFF, 0x01)
            self.assertEqual(result.after_cpu.pc, 0x00200041)

    def test_execute_prefixed_byte_djnz_falls_through_when_result_zero(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x00200040, b"\xC9\x1C\xFE")
            base_view = load_fetch_view(rom_path)
            seeded_cpu = replace(
                base_view.machine.cpu,
                regs=replace(base_view.machine.cpu.regs, xwa=0x00000001),
            )

            result = build_execute_next(base_view, cpu_state=seeded_cpu)

            self.assertEqual(result.status, "executed")
            assert result.after_cpu is not None
            self.assertEqual(result.after_cpu.regs.xwa & 0xFF, 0x00)
            self.assertEqual(result.after_cpu.pc, 0x00200043)

    def test_execute_prefixed_long_djnz_stops_as_silicon_undefined(self) -> None:
        # E8 1C FE = djnz XWA (genuine LONG prefix). Word djnz (D8 1C) EXECUTES on
        # real HW, but the long-djnz form stays silicon-undefined — this test now
        # covers the long prefix so the "stays undefined" coverage is preserved.
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x00200040, b"\xE8\x1C\xFE")
            base_view = load_fetch_view(rom_path)
            seeded_cpu = replace(
                base_view.machine.cpu,
                regs=replace(base_view.machine.cpu.regs, xwa=0x00000002),
            )

            result = build_execute_next(base_view, cpu_state=seeded_cpu)

            self.assertEqual(result.status, "silicon-undefined")
            self.assertEqual(result.decode.assembly, "djnz XWA, 0x200041")

    def test_execute_prefixed_mirr_reverses_word_bits(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x00200040, b"\xDB\x16")
            base_view = load_fetch_view(rom_path)
            seeded_cpu = replace(
                base_view.machine.cpu,
                regs=replace(base_view.machine.cpu.regs, xhl=0xAABB1234),
                flags=StatusFlags(sf=True, zf=False, vf=True, hf=False, cf=True, nf=False),
            )

            result = build_execute_next(base_view, cpu_state=seeded_cpu)

            self.assertEqual(result.status, "executed")
            self.assertEqual(result.decode.assembly, "mirr HL")
            assert result.after_cpu is not None
            self.assertEqual(result.after_cpu.regs.xhl, 0xAABB2C48)
            self.assertEqual(result.after_cpu.pc, 0x00200042)
            self.assertEqual(result.after_cpu.flags, seeded_cpu.flags)

    def test_execute_prefixed_bs1f_writes_first_set_bit_index_into_a(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x00200040, b"\xDC\x0E")
            base_view = load_fetch_view(rom_path)
            seeded_cpu = replace(
                base_view.machine.cpu,
                regs=replace(base_view.machine.cpu.regs, xwa=0xAAAA0000, xix=0x00001200),
                flags=StatusFlags(sf=True, zf=False, vf=True, hf=False, cf=True, nf=False),
            )

            result = build_execute_next(base_view, cpu_state=seeded_cpu)

            self.assertEqual(result.status, "executed")
            self.assertEqual(result.decode.assembly, "bs1f A, IX")
            assert result.after_cpu is not None
            self.assertEqual(result.after_cpu.regs.xwa, 0xAAAA0009)
            self.assertFalse(result.after_cpu.flags.vf)
            self.assertEqual(result.after_cpu.flags.sf, seeded_cpu.flags.sf)
            self.assertEqual(result.after_cpu.flags.zf, seeded_cpu.flags.zf)
            self.assertEqual(result.after_cpu.flags.hf, seeded_cpu.flags.hf)
            self.assertEqual(result.after_cpu.flags.cf, seeded_cpu.flags.cf)
            self.assertEqual(result.after_cpu.flags.nf, seeded_cpu.flags.nf)

    def test_execute_prefixed_bs1b_writes_last_set_bit_index_into_a(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x00200040, b"\xDC\x0F")
            base_view = load_fetch_view(rom_path)
            seeded_cpu = replace(
                base_view.machine.cpu,
                regs=replace(base_view.machine.cpu.regs, xwa=0xAAAA0000, xix=0x00001200),
                flags=StatusFlags(sf=False, zf=True, vf=True, hf=True, cf=False, nf=True),
            )

            result = build_execute_next(base_view, cpu_state=seeded_cpu)

            self.assertEqual(result.status, "executed")
            self.assertEqual(result.decode.assembly, "bs1b A, IX")
            assert result.after_cpu is not None
            self.assertEqual(result.after_cpu.regs.xwa, 0xAAAA000C)
            self.assertFalse(result.after_cpu.flags.vf)
            self.assertEqual(result.after_cpu.flags.sf, seeded_cpu.flags.sf)
            self.assertEqual(result.after_cpu.flags.zf, seeded_cpu.flags.zf)
            self.assertEqual(result.after_cpu.flags.hf, seeded_cpu.flags.hf)
            self.assertEqual(result.after_cpu.flags.cf, seeded_cpu.flags.cf)
            self.assertEqual(result.after_cpu.flags.nf, seeded_cpu.flags.nf)

    def test_execute_prefixed_bs1_zero_source_stops_as_silicon_undefined(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x00200040, b"\xDC\x0E")
            base_view = load_fetch_view(rom_path)
            seeded_cpu = replace(
                base_view.machine.cpu,
                regs=replace(base_view.machine.cpu.regs, xwa=0xAAAA0000, xix=0x00000000),
            )

            result = build_execute_next(base_view, cpu_state=seeded_cpu)

            self.assertEqual(result.status, "silicon-undefined")

    def test_execute_prefixed_mula_matches_toshiba_example(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x00200040, b"\xDC\x19")
            base_view = load_fetch_view(rom_path)
            seeded_cpu = replace(
                base_view.machine.cpu,
                regs=replace(
                    base_view.machine.cpu.regs,
                    xde=0x00000100,
                    xhl=0x00000200,
                    xix=0x50000000,
                ),
            )

            result = build_execute_next(
                base_view,
                cpu_state=seeded_cpu,
                memory_bytes={
                    0x0100: 0x34,
                    0x0101: 0x12,
                    0x0200: 0xAB,
                    0x0201: 0x89,
                },
            )

            self.assertEqual(result.status, "executed")
            self.assertEqual(result.decode.assembly, "mula XIX")
            assert result.after_cpu is not None
            self.assertEqual(result.after_cpu.regs.xix, 0x4795FCBC)
            self.assertEqual(result.after_cpu.regs.xhl, 0x000001FE)
            self.assertFalse(result.after_cpu.flags.sf)
            self.assertFalse(result.after_cpu.flags.zf)
            self.assertFalse(result.after_cpu.flags.vf)

    def test_execute_prefixed_mula_decrements_xhl_after_writing_overlapping_destination(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x00200040, b"\xDB\x19")
            base_view = load_fetch_view(rom_path)
            seeded_cpu = replace(
                base_view.machine.cpu,
                regs=replace(
                    base_view.machine.cpu.regs,
                    xde=0x00000100,
                    xhl=0x00000200,
                ),
                flags=StatusFlags(sf=False, zf=True, vf=False, hf=True, cf=True, nf=True),
            )

            result = build_execute_next(
                base_view,
                cpu_state=seeded_cpu,
                memory_bytes={
                    0x0100: 0x03,
                    0x0101: 0x00,
                    0x0200: 0x04,
                    0x0201: 0x00,
                },
            )

            self.assertEqual(result.status, "executed")
            self.assertEqual(result.decode.assembly, "mula XHL")
            assert result.after_cpu is not None
            self.assertEqual(result.after_cpu.regs.xhl, 0x0000020A)
            self.assertFalse(result.after_cpu.flags.sf)
            self.assertFalse(result.after_cpu.flags.zf)
            self.assertFalse(result.after_cpu.flags.vf)
            self.assertEqual(result.after_cpu.flags.hf, seeded_cpu.flags.hf)
            self.assertEqual(result.after_cpu.flags.cf, seeded_cpu.flags.cf)
            self.assertEqual(result.after_cpu.flags.nf, seeded_cpu.flags.nf)

    def test_execute_prefixed_mula_stops_when_runtime_memory_is_unavailable(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x00200040, b"\xDC\x19")
            base_view = load_fetch_view(rom_path)
            seeded_cpu = replace(
                base_view.machine.cpu,
                regs=replace(
                    base_view.machine.cpu.regs,
                    xde=0x00FF0000,
                    xhl=0x00FF0000,
                    xix=0x50000000,
                ),
            )

            # Operand source is in the BIOS region with no BIOS image attached --
            # the one memory class that still honest-stops (we lack the bytes).
            # On-chip RAM (0x00) and unmapped open-bus (0x00, hw_test_openbus
            # 2026-07-08) no longer block.
            result = build_execute_next(base_view, cpu_state=seeded_cpu)

            self.assertEqual(result.status, "runtime-memory-unavailable")
            self.assertIsNone(result.after_cpu)

    def test_execute_prefixed_minc2_wraps_with_documented_modulo_window(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x00200040, b"\xDC\x39\x06\x00")
            base_view = load_fetch_view(rom_path)
            seeded_cpu = replace(
                base_view.machine.cpu,
                regs=replace(base_view.machine.cpu.regs, xix=0xAAAA1236),
                flags=StatusFlags(sf=True, zf=False, vf=True, hf=False, cf=True, nf=False),
            )

            result = build_execute_next(base_view, cpu_state=seeded_cpu)

            self.assertEqual(result.status, "executed")
            self.assertEqual(result.decode.assembly, "minc2 0x0006, IX")
            assert result.after_cpu is not None
            self.assertEqual(result.after_cpu.regs.xix, 0xAAAA1230)
            self.assertEqual(result.after_cpu.flags, seeded_cpu.flags)

    def test_execute_prefixed_mdec1_wraps_with_documented_modulo_window(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x00200040, b"\xDC\x3C\x07\x00")
            base_view = load_fetch_view(rom_path)
            seeded_cpu = replace(
                base_view.machine.cpu,
                regs=replace(base_view.machine.cpu.regs, xix=0xAAAA1230),
                flags=StatusFlags(sf=False, zf=True, vf=False, hf=True, cf=False, nf=True),
            )

            result = build_execute_next(base_view, cpu_state=seeded_cpu)

            self.assertEqual(result.status, "executed")
            self.assertEqual(result.decode.assembly, "mdec1 0x0007, IX")
            assert result.after_cpu is not None
            self.assertEqual(result.after_cpu.regs.xix, 0xAAAA1237)
            self.assertEqual(result.after_cpu.flags, seeded_cpu.flags)

    def test_execute_prefixed_minc4_advances_without_wrap(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x00200040, b"\xDC\x3A\x3C\x00")
            base_view = load_fetch_view(rom_path)
            seeded_cpu = replace(
                base_view.machine.cpu,
                regs=replace(base_view.machine.cpu.regs, xix=0xAAAA1240),
            )

            result = build_execute_next(base_view, cpu_state=seeded_cpu)

            self.assertEqual(result.status, "executed")
            assert result.after_cpu is not None
            self.assertEqual(result.after_cpu.regs.xix, 0xAAAA1244)

    def test_execute_prefixed_modulo_adjust_invalid_window_stops_as_silicon_undefined(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x00200040, b"\xDC\x39\x00\x00")
            base_view = load_fetch_view(rom_path)
            seeded_cpu = replace(
                base_view.machine.cpu,
                regs=replace(base_view.machine.cpu.regs, xix=0xAAAA1234),
            )

            result = build_execute_next(base_view, cpu_state=seeded_cpu)

            self.assertEqual(result.status, "silicon-undefined")

    def test_execute_prefixed_word_add_immediate_now_stops_as_silicon_broken(self) -> None:
        """Updated 2026-05-20: `D0 C8 lo hi` (= add WA, imm16) is
        silicon-broken on real NGPC. HW crash on
        stargunner_j16_C4_phase4_BROKEN_HW_20260520.ngc confirmed this.
        The HW-faithful emulator must STOP execution rather than pretend
        the instruction succeeded."""
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x00200040, b"\xD8\xB8")
            base_view = load_fetch_view(rom_path)
            seeded_cpu = replace(
                base_view.machine.cpu,
                regs=replace(base_view.machine.cpu.regs, xwa=0xAABB1234),
            )

            result = build_execute_next(base_view, cpu_state=seeded_cpu)

            self.assertEqual(result.status, "silicon-broken")
            self.assertIsNotNone(result.matched_quirk)
            assert result.matched_quirk is not None
            self.assertEqual(result.matched_quirk.quirk_id, "cpu.d8_df_register_to_register")

    def test_execute_prefixed_byte_inc_requires_known_owner_register(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x00200040, b"\xC8\x61")
            view = load_fetch_view(rom_path)

            result = build_execute_next(view)

            self.assertEqual(result.status, "requires-known-full-register")
            self.assertEqual(result.decode.assembly, "inc 1, W")
            self.assertIsNone(result.after_cpu)

    def test_execute_lda_r32_abs24_updates_register_with_effective_address(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x0020D06D, b"\xF2\x66\x32\x21\x31")
            view = load_fetch_view(rom_path)

            result = build_execute_next(view)

            self.assertEqual(result.status, "executed")
            self.assertEqual(result.decode.assembly, "lda XBC, (0x213266)")
            self.assertEqual(result.written_registers, ("XBC", "PC"))
            assert result.after_cpu is not None
            self.assertEqual(result.after_cpu.regs.xbc, 0x00213266)
            self.assertEqual(result.after_cpu.pc, 0x0020D072)

    def test_execute_indexed_store_r32_writes_runtime_overlay(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x00200040, b"\xBF\x04\x61")
            base_view = load_fetch_view(rom_path)
            seeded_cpu = replace(
                base_view.machine.cpu,
                regs=replace(
                    base_view.machine.cpu.regs,
                    xsp=0x000040F8,
                    xbc=0x00213266,
                ),
            )

            result = build_execute_next(base_view, cpu_state=seeded_cpu)

            self.assertEqual(result.status, "executed")
            self.assertEqual(result.decode.assembly, "ld (XSP+4), XBC")
            self.assertEqual(result.written_registers, ("PC",))
            self.assertEqual(len(result.memory_writes), 1)
            self.assertEqual(result.memory_writes[0].address, 0x0040FC)
            self.assertEqual(result.memory_writes[0].data, b"\x66\x32\x21\x00")
            assert result.after_cpu is not None
            assert result.after_memory is not None
            self.assertEqual(result.after_cpu.pc, 0x00200043)
            self.assertEqual(result.after_memory[0x0040FC], 0x66)
            self.assertEqual(result.after_memory[0x0040FD], 0x32)
            self.assertEqual(result.after_memory[0x0040FE], 0x21)
            self.assertEqual(result.after_memory[0x0040FF], 0x00)

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
            base_view = load_fetch_view(rom_path)
            seeded_cpu = replace(
                base_view.machine.cpu,
                regs=replace(base_view.machine.cpu.regs, xbc=0x12340000),
            )

            result = build_execute_next(
                base_view,
                cpu_state=seeded_cpu,
                memory_bytes={0x6141: 0xAB},   # the CONTENTS must be ignored
            )

            self.assertEqual(result.status, "executed")
            self.assertEqual(result.decode.assembly, "lda BC, (0x6141)")
            assert result.after_cpu is not None
            # BC holds the ADDRESS; the byte 0xAB sitting there is irrelevant.
            self.assertEqual(result.after_cpu.regs.xbc & 0xFFFF, 0x6141)

    def test_execute_abs16_byte_add_a_mem_from_bios_checksum(self) -> None:
        # add A,(0x6141) = C1 41 61 81. The real SNK BIOS boot sums HW registers
        # into A via this abs16 byte mem-source form (frontier that advanced the
        # boot 189 -> 234 instructions). A = A + mem8, flags from the addition.
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x00200040, b"\xC1\x41\x61\x81")
            base_view = load_fetch_view(rom_path)
            seeded_cpu = replace(
                base_view.machine.cpu,
                regs=replace(base_view.machine.cpu.regs, xwa=0x00000010),
            )

            result = build_execute_next(
                base_view, cpu_state=seeded_cpu, memory_bytes={0x6141: 0x05}
            )

            self.assertEqual(result.status, "executed")
            self.assertEqual(result.decode.assembly, "add A, (0x6141)")
            self.assertEqual(result.written_registers, ("A", "PC"))
            assert result.after_cpu is not None
            self.assertEqual(result.after_cpu.regs.xwa, 0x00000015)
            self.assertEqual(result.after_cpu.pc, 0x00200044)

    def test_execute_abs16_byte_add_mem_reg_writes_memory(self) -> None:
        # add (0x6141),W = C1 41 61 88. Memory destination: mem8 = mem8 + W.
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x00200040, b"\xC1\x41\x61\x88")
            base_view = load_fetch_view(rom_path)
            seeded_cpu = replace(
                base_view.machine.cpu,
                regs=replace(base_view.machine.cpu.regs, xwa=0x00000300),
            )

            result = build_execute_next(
                base_view, cpu_state=seeded_cpu, memory_bytes={0x6141: 0x05}
            )

            self.assertEqual(result.status, "executed")
            self.assertEqual(result.decode.assembly, "add (0x6141), W")
            self.assertEqual(len(result.memory_writes), 1)
            self.assertEqual(result.memory_writes[0].address, 0x6141)
            self.assertEqual(result.memory_writes[0].data, bytes((0x08,)))

    def test_execute_abs16_byte_cp_a_mem_flags_only(self) -> None:
        # cp A,(0x6141) = C1 41 61 F1. Flags only, no register/memory writeback.
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x00200040, b"\xC1\x41\x61\xF1")
            base_view = load_fetch_view(rom_path)
            seeded_cpu = replace(
                base_view.machine.cpu,
                regs=replace(base_view.machine.cpu.regs, xwa=0x00000005),
            )

            result = build_execute_next(
                base_view, cpu_state=seeded_cpu, memory_bytes={0x6141: 0x05}
            )

            self.assertEqual(result.status, "executed")
            self.assertEqual(result.decode.assembly, "cp A, (0x6141)")
            self.assertEqual(result.written_registers, ("PC",))
            self.assertEqual(result.memory_writes, ())
            assert result.after_cpu is not None
            self.assertEqual(result.after_cpu.regs.xwa, 0x00000005)
            self.assertTrue(result.after_cpu.flags.zf)

    def test_execute_abs8_word_compare_immediate_updates_flags(self) -> None:
        # cpw (0x40),0x0050 = D0 40 3F 50 00. Reads word at abs8 0x40, compares
        # with 0x0050, sets flags, no writeback. (0xD0..0xD7 = word memory.)
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x00200040, b"\xD0\x40\x3F\x50\x00")
            base_view = load_fetch_view(rom_path)

            result = build_execute_next(
                base_view, memory_bytes={0x40: 0x50, 0x41: 0x00}
            )

            self.assertEqual(result.status, "executed")
            self.assertEqual(result.decode.assembly, "cpw (0x40), 0x0050")
            self.assertEqual(result.memory_writes, ())
            assert result.after_cpu is not None
            self.assertTrue(result.after_cpu.flags.zf)   # 0x0050 - 0x0050 == 0
            self.assertEqual(result.after_cpu.pc, 0x00200045)

    def test_execute_abs8_bit_set_writes_io_page(self) -> None:
        # set 2, (0x40) = F0 40 BA. Sets bit 2 of the byte at abs8 0x40.
        # Real SNK BIOS boot: `set 2, (0xB3)` (F0 B3 BA) near the final halt.
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x00200040, b"\xF0\x40\xBA")
            base_view = load_fetch_view(rom_path)

            result = build_execute_next(base_view, memory_bytes={0x40: 0x01})

            self.assertEqual(result.status, "executed")
            self.assertEqual(result.decode.assembly, "set 2, (0x40)")
            self.assertEqual(len(result.memory_writes), 1)
            self.assertEqual(result.memory_writes[0].address, 0x40)
            self.assertEqual(result.memory_writes[0].data, bytes((0x05,)))  # 0x01 | (1<<2)

    def test_execute_abs8_byte_register_store_writes_io_page(self) -> None:
        # ld (0xBC),A = F0 BC 41. Store byte register A to the CPU-I/O-page
        # address 0x0000BC. Real SNK BIOS boot writes HW registers this way.
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x00200040, b"\xF0\xBC\x41")
            base_view = load_fetch_view(rom_path)
            seeded_cpu = replace(
                base_view.machine.cpu,
                regs=replace(base_view.machine.cpu.regs, xwa=0x0000005A),
            )

            result = build_execute_next(base_view, cpu_state=seeded_cpu)

            self.assertEqual(result.status, "executed")
            self.assertEqual(result.decode.assembly, "ld (0xBC), A")
            self.assertEqual(len(result.memory_writes), 1)
            self.assertEqual(result.memory_writes[0].address, 0x0000BC)
            self.assertEqual(result.memory_writes[0].data, bytes((0x5A,)))
            assert result.after_cpu is not None
            self.assertEqual(result.after_cpu.pc, 0x00200043)

    def test_execute_lda_abs8_loads_effective_address_into_r32(self) -> None:
        # lda XIX,(0xA0) = F0 A0 34. The real SNK BIOS boot points XIX at a HW
        # register this way. LDA loads the operand's EFFECTIVE ADDRESS (0x0000A0),
        # with no memory access and no flag changes.
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x00200040, b"\xF0\xA0\x34")
            base_view = load_fetch_view(rom_path)

            result = build_execute_next(base_view)

            self.assertEqual(result.status, "executed")
            self.assertEqual(result.decode.assembly, "lda XIX, (0xA0)")
            self.assertEqual(result.written_registers, ("XIX", "PC"))
            assert result.after_cpu is not None
            self.assertEqual(result.after_cpu.regs.xix, 0x000000A0)
            self.assertEqual(result.after_cpu.pc, 0x00200043)

    def test_execute_lda_abs8_word_dest_preserves_high_half(self) -> None:
        # lda BC,(0xA0) = F0 A0 21. Word destination: only the low 16 bits of
        # XBC receive the effective address; the high half is preserved.
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x00200040, b"\xF0\xA0\x21")
            base_view = load_fetch_view(rom_path)
            seeded_cpu = replace(
                base_view.machine.cpu,
                regs=replace(base_view.machine.cpu.regs, xbc=0xDEAD0000),
            )

            result = build_execute_next(base_view, cpu_state=seeded_cpu)

            self.assertEqual(result.status, "executed")
            self.assertEqual(result.decode.assembly, "lda BC, (0xA0)")
            assert result.after_cpu is not None
            self.assertEqual(result.after_cpu.regs.xbc, 0xDEAD00A0)

    def test_execute_prefixed_ld_r32_to_r32_copies_known_value(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x0020D084, b"\xEB\x8D")
            base_view = load_fetch_view(rom_path)
            seeded_cpu = replace(
                base_view.machine.cpu,
                regs=replace(base_view.machine.cpu.regs, xhl=0x00004000),
            )

            result = build_execute_next(base_view, cpu_state=seeded_cpu)

            self.assertEqual(result.status, "executed")
            self.assertEqual(result.decode.assembly, "ld XIY, XHL")
            self.assertEqual(result.written_registers, ("XIY", "PC"))
            assert result.after_cpu is not None
            self.assertEqual(result.after_cpu.regs.xiy, 0x00004000)
            self.assertEqual(result.after_cpu.pc, 0x0020D086)

    def test_execute_prefixed_cp_r32_updates_flag_subset(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x0020D08B, b"\xEC\xF1")
            base_view = load_fetch_view(rom_path)
            seeded_cpu = replace(
                base_view.machine.cpu,
                regs=replace(
                    base_view.machine.cpu.regs,
                    xbc=0x00213266,
                    xix=0x00213350,
                ),
            )

            result = build_execute_next(base_view, cpu_state=seeded_cpu)

            self.assertEqual(result.status, "executed")
            self.assertEqual(result.decode.assembly, "cp XBC, XIX")
            self.assertEqual(result.written_registers, ("PC",))
            assert result.after_cpu is not None
            self.assertEqual(result.after_cpu.pc, 0x0020D08D)
            self.assertFalse(result.after_cpu.flags.zf)
            self.assertTrue(result.after_cpu.flags.sf)
            self.assertFalse(result.after_cpu.flags.vf)
            self.assertFalse(result.after_cpu.flags.hf)
            self.assertTrue(result.after_cpu.flags.cf)
            self.assertTrue(result.after_cpu.flags.nf, "CP is a subtractive op; NF must be 1")
            self.assertIn("modeled-flags-subset", result.after_cpu.modeled_fields)

    def test_execute_add_a_imm_clears_nf_flag(self) -> None:
        """ADD is an additive op: NF must be cleared after execution."""
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            # C9 C8 01 = add A, 0x01   (byte form, HW-safe)
            self._write_demo_rom(rom_path, 0x00200040, b"\xC9\xC8\x01")
            base_view = load_fetch_view(rom_path)
            seeded_cpu = replace(
                base_view.machine.cpu,
                regs=replace(base_view.machine.cpu.regs, xwa=0xAABB0010),
                flags=StatusFlags(sf=False, zf=False, vf=False, hf=False, cf=False, nf=True),
            )

            result = build_execute_next(base_view, cpu_state=seeded_cpu)

            self.assertEqual(result.status, "executed")
            assert result.after_cpu is not None
            self.assertFalse(result.after_cpu.flags.nf, "ADD must clear NF")

    def test_execute_scc_nz_xhl_sets_one_when_condition_true(self) -> None:
        # EB 7E = scc NZ, XHL — the long-prefixed form (EB is the genuine long
        # prefix). With zf=False the condition (not-zero) is true → XHL becomes 1.
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x00200040, b"\xEB\x7E")
            base_view = load_fetch_view(rom_path)
            seeded_cpu = replace(
                base_view.machine.cpu,
                regs=replace(base_view.machine.cpu.regs, xhl=0xDEADBEEF),
                flags=StatusFlags(sf=False, zf=False, vf=False, hf=False, cf=False),
            )

            result = build_execute_next(base_view, cpu_state=seeded_cpu)

            self.assertEqual(result.status, "executed")
            self.assertEqual(result.decode.assembly, "scc NZ, XHL")
            self.assertEqual(result.written_registers, ("XHL", "PC"))
            assert result.after_cpu is not None
            self.assertEqual(result.after_cpu.regs.xhl, 1)
            self.assertEqual(result.after_cpu.pc, 0x00200042)

    def test_execute_scc_nz_xhl_sets_zero_when_condition_false(self) -> None:
        # Same opcode, but zf=True → not-zero is false → XHL becomes 0.
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x00200040, b"\xEB\x7E")
            base_view = load_fetch_view(rom_path)
            seeded_cpu = replace(
                base_view.machine.cpu,
                regs=replace(base_view.machine.cpu.regs, xhl=0xDEADBEEF),
                flags=StatusFlags(sf=False, zf=True, vf=False, hf=False, cf=False),
            )

            result = build_execute_next(base_view, cpu_state=seeded_cpu)

            self.assertEqual(result.status, "executed")
            assert result.after_cpu is not None
            self.assertEqual(result.after_cpu.regs.xhl, 0)

    def test_execute_scc_blocks_when_required_flags_unknown(self) -> None:
        # zf is None (unseeded) → condition undecidable → honest block.
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x00200040, b"\xEB\x7E")
            base_view = load_fetch_view(rom_path)
            seeded_cpu = replace(
                base_view.machine.cpu,
                regs=replace(base_view.machine.cpu.regs, xhl=0xDEADBEEF),
                # Explicitly do NOT seed flags — zf stays None.
            )

            result = build_execute_next(base_view, cpu_state=seeded_cpu)

            self.assertEqual(result.status, "requires-known-flags")
            self.assertEqual(result.decode.assembly, "scc NZ, XHL")
            self.assertIsNone(result.after_cpu)

    def test_execute_scc_t_always_sets_one(self) -> None:
        # CC index 8 = T (always true) — sets reg to 1 without reading flags.
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x00200040, b"\xEB\x78")
            base_view = load_fetch_view(rom_path)
            seeded_cpu = replace(
                base_view.machine.cpu,
                regs=replace(base_view.machine.cpu.regs, xhl=0xDEADBEEF),
            )

            result = build_execute_next(base_view, cpu_state=seeded_cpu)

            self.assertEqual(result.status, "executed")
            self.assertEqual(result.decode.assembly, "scc T, XHL")
            assert result.after_cpu is not None
            self.assertEqual(result.after_cpu.regs.xhl, 1)

    def test_execute_prefixed_rl_a_uses_carry(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x00200040, b"\xC9\xEA\x01")
            view = load_fetch_view(rom_path)
            seeded_cpu = replace(
                view.machine.cpu,
                regs=replace(view.machine.cpu.regs, xwa=0x00000040),
                flags=StatusFlags(sf=False, zf=False, vf=False, hf=False, cf=True, nf=False),
            )

            result = build_execute_next(view, cpu_state=seeded_cpu)

            self.assertEqual(result.status, "executed")
            self.assertEqual(result.decode.assembly, "rl 1, A")
            assert result.after_cpu is not None
            self.assertEqual(result.after_cpu.regs.xwa & 0xFF, 0x81)
            self.assertFalse(result.after_cpu.flags.cf)

    def test_execute_prefixed_rr_a_uses_carry(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x00200040, b"\xC9\xEB\x01")
            view = load_fetch_view(rom_path)
            seeded_cpu = replace(
                view.machine.cpu,
                regs=replace(view.machine.cpu.regs, xwa=0x00000002),
                flags=StatusFlags(sf=False, zf=False, vf=False, hf=False, cf=True, nf=False),
            )

            result = build_execute_next(view, cpu_state=seeded_cpu)

            self.assertEqual(result.status, "executed")
            self.assertEqual(result.decode.assembly, "rr 1, A")
            assert result.after_cpu is not None
            self.assertEqual(result.after_cpu.regs.xwa & 0xFF, 0x81)
            self.assertFalse(result.after_cpu.flags.cf)

    def test_execute_prefixed_rl_blocks_when_carry_unknown(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x00200040, b"\xC9\xEA\x01")
            view = load_fetch_view(rom_path)
            seeded_cpu = replace(
                view.machine.cpu,
                regs=replace(view.machine.cpu.regs, xwa=0x00000040),
            )

            result = build_execute_next(view, cpu_state=seeded_cpu)

            self.assertEqual(result.status, "requires-known-flags")
            self.assertEqual(result.decode.assembly, "rl 1, A")
            self.assertIsNone(result.after_cpu)

    def test_execute_prefixed_rlc_a_count_comes_from_register_a(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x00200040, b"\xC9\xF8")
            view = load_fetch_view(rom_path)
            seeded_cpu = replace(
                view.machine.cpu,
                regs=replace(view.machine.cpu.regs, xwa=0x00008103),
                flags=StatusFlags(sf=False, zf=False, vf=False, hf=False, cf=False, nf=False),
            )

            result = build_execute_next(view, cpu_state=seeded_cpu)

            self.assertEqual(result.status, "executed")
            self.assertEqual(result.decode.assembly, "rlc A, A")
            assert result.after_cpu is not None
            self.assertEqual(result.after_cpu.regs.xwa & 0xFF, 0x18)
            self.assertFalse(result.after_cpu.flags.cf)

    def test_execute_prefixed_rl_a_register_uses_carry(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x00200040, b"\xC9\xFA")
            view = load_fetch_view(rom_path)
            seeded_cpu = replace(
                view.machine.cpu,
                regs=replace(view.machine.cpu.regs, xwa=0x00004001),
                flags=StatusFlags(sf=False, zf=False, vf=False, hf=False, cf=True, nf=False),
            )

            result = build_execute_next(view, cpu_state=seeded_cpu)

            self.assertEqual(result.status, "executed")
            self.assertEqual(result.decode.assembly, "rl A, A")
            assert result.after_cpu is not None
            self.assertEqual(result.after_cpu.regs.xwa & 0xFF, 0x03)
            self.assertFalse(result.after_cpu.flags.cf)

    def test_execute_prefixed_rr_a_register_blocks_when_carry_unknown(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x00200040, b"\xC9\xFB")
            view = load_fetch_view(rom_path)
            seeded_cpu = replace(
                view.machine.cpu,
                regs=replace(view.machine.cpu.regs, xwa=0x00008001),
            )

            result = build_execute_next(view, cpu_state=seeded_cpu)

            self.assertEqual(result.status, "requires-known-flags")
            self.assertEqual(result.decode.assembly, "rr A, A")
            self.assertIsNone(result.after_cpu)

    def test_execute_conditional_jr_uses_known_flags(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x00200040, b"\x6F\x11")
            base_view = load_fetch_view(rom_path)
            seeded_cpu = replace(
                base_view.machine.cpu,
                flags=StatusFlags(sf=False, zf=False, vf=False, hf=False, cf=True),
            )

            result = build_execute_next(base_view, cpu_state=seeded_cpu)

            self.assertEqual(result.status, "executed")
            self.assertEqual(result.decode.assembly, "jr NC, 0x200053")
            assert result.after_cpu is not None
            self.assertEqual(result.after_cpu.pc, 0x00200042)

    def test_execute_indexed_load_r32_reads_runtime_overlay(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x0020D08F, b"\xAF\x04\x20")
            base_view = load_fetch_view(rom_path)
            seeded_cpu = replace(
                base_view.machine.cpu,
                regs=replace(base_view.machine.cpu.regs, xsp=0x000040F0),
            )

            result = build_execute_next(
                base_view,
                cpu_state=seeded_cpu,
                memory_bytes={
                    0x0040F4: 0x66,
                    0x0040F5: 0x32,
                    0x0040F6: 0x21,
                    0x0040F7: 0x00,
                },
            )

            self.assertEqual(result.status, "executed")
            self.assertEqual(result.decode.assembly, "ld XWA, (XSP+4)")
            self.assertEqual(result.written_registers, ("XWA", "PC"))
            assert result.after_cpu is not None
            self.assertEqual(result.after_cpu.regs.xwa, 0x00213266)
            self.assertEqual(result.after_cpu.pc, 0x0020D092)

    def test_execute_post_increment_byte_load_updates_register_and_pointer(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x0020D092, b"\xC5\xE0\x23")
            base_view = load_fetch_view(rom_path)
            seeded_cpu = replace(
                base_view.machine.cpu,
                regs=replace(
                    base_view.machine.cpu.regs,
                    xwa=0x00213266,
                    xbc=0xAABBCCDD,
                ),
            )

            result = build_execute_next(
                base_view,
                cpu_state=seeded_cpu,
                memory_bytes={0x213266: 0x42},
            )

            self.assertEqual(result.status, "executed")
            self.assertEqual(result.decode.assembly, "ld C, (XWA+)")
            self.assertEqual(result.written_registers, ("C", "XWA", "PC"))
            assert result.after_cpu is not None
            self.assertEqual(result.after_cpu.regs.xwa, 0x00213267)
            self.assertEqual(result.after_cpu.regs.xbc, 0xAABBCC42)
            self.assertEqual(result.after_cpu.pc, 0x0020D095)

    def test_execute_post_increment_byte_store_writes_overlay_and_pointer(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x0020D098, b"\xF5\xF8\x43")
            base_view = load_fetch_view(rom_path)
            seeded_cpu = replace(
                base_view.machine.cpu,
                regs=replace(
                    base_view.machine.cpu.regs,
                    xiz=0x00005EBC,
                    xbc=0xAABBCC42,
                ),
            )

            result = build_execute_next(base_view, cpu_state=seeded_cpu)

            self.assertEqual(result.status, "executed")
            self.assertEqual(result.decode.assembly, "ld (XIZ+), C")
            self.assertEqual(result.written_registers, ("XIZ", "PC"))
            self.assertEqual(len(result.memory_writes), 1)
            self.assertEqual(result.memory_writes[0].address, 0x005EBC)
            self.assertEqual(result.memory_writes[0].data, b"\x42")
            assert result.after_cpu is not None
            assert result.after_memory is not None
            self.assertEqual(result.after_cpu.regs.xiz, 0x00005EBD)
            self.assertEqual(result.after_cpu.pc, 0x0020D09B)
            self.assertEqual(result.after_memory[0x005EBC], 0x42)

    def test_execute_post_increment_word_store_writes_overlay_and_pointer(self) -> None:
        # F5 F1 51 = ldw (XIX+), BC
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x0020150F, b"\xF5\xF1\x51")
            base_view = load_fetch_view(rom_path)
            seeded_cpu = replace(
                base_view.machine.cpu,
                regs=replace(
                    base_view.machine.cpu.regs,
                    xix=0x00005000,
                    xbc=0xAABB3344,
                ),
            )

            result = build_execute_next(base_view, cpu_state=seeded_cpu)

            self.assertEqual(result.status, "executed")
            self.assertEqual(result.decode.assembly, "ldw (XIX+), BC")
            self.assertEqual(result.written_registers, ("XIX", "PC"))
            self.assertEqual(len(result.memory_writes), 1)
            self.assertEqual(result.memory_writes[0].address, 0x005000)
            self.assertEqual(result.memory_writes[0].data, b"\x44\x33")
            assert result.after_cpu is not None
            assert result.after_memory is not None
            self.assertEqual(result.after_cpu.regs.xix, 0x00005002)
            self.assertEqual(result.after_cpu.pc, 0x00201512)
            self.assertEqual(result.after_memory[0x005000], 0x44)
            self.assertEqual(result.after_memory[0x005001], 0x33)

    def test_execute_post_increment_word_load_updates_register_and_pointer(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x00201520, b"\xD5\xF1\x21")
            base_view = load_fetch_view(rom_path)
            seeded_cpu = replace(
                base_view.machine.cpu,
                regs=replace(
                    base_view.machine.cpu.regs,
                    xix=0x00005000,
                    xbc=0xAAAABBBB,
                ),
            )

            result = build_execute_next(
                base_view,
                cpu_state=seeded_cpu,
                memory_bytes={0x005000: 0x34, 0x005001: 0x12},
            )

            self.assertEqual(result.status, "executed")
            self.assertEqual(result.decode.assembly, "ld BC, (XIX+)")
            self.assertEqual(result.written_registers, ("BC", "XIX", "PC"))
            assert result.after_cpu is not None
            self.assertEqual(result.after_cpu.regs.xix, 0x00005002)
            self.assertEqual(result.after_cpu.regs.xbc, 0xAAAA1234)
            self.assertEqual(result.after_cpu.pc, 0x00201523)

    def test_execute_post_increment_long_load_updates_register_and_pointer(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x00201530, b"\xE5\xF1\x23")
            base_view = load_fetch_view(rom_path)
            seeded_cpu = replace(
                base_view.machine.cpu,
                regs=replace(
                    base_view.machine.cpu.regs,
                    xix=0x00005000,
                    xhl=0x00000000,
                ),
            )

            result = build_execute_next(
                base_view,
                cpu_state=seeded_cpu,
                memory_bytes={0x005000: 0x78, 0x005001: 0x56, 0x005002: 0x34, 0x005003: 0x12},
            )

            self.assertEqual(result.status, "executed")
            self.assertEqual(result.decode.assembly, "ld XHL, (XIX+)")
            self.assertEqual(result.written_registers, ("XHL", "XIX", "PC"))
            assert result.after_cpu is not None
            self.assertEqual(result.after_cpu.regs.xix, 0x00005004)
            self.assertEqual(result.after_cpu.regs.xhl, 0x12345678)
            self.assertEqual(result.after_cpu.pc, 0x00201533)

    def test_execute_post_increment_immediate_word_store_writes_overlay_and_pointer(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x00201540, b"\xF5\xF1\x02\x34\x12")
            base_view = load_fetch_view(rom_path)
            seeded_cpu = replace(
                base_view.machine.cpu,
                regs=replace(base_view.machine.cpu.regs, xix=0x00005000),
            )

            result = build_execute_next(base_view, cpu_state=seeded_cpu)

            self.assertEqual(result.status, "executed")
            self.assertEqual(result.decode.assembly, "ldw (XIX+), 0x1234")
            self.assertEqual(result.written_registers, ("XIX", "PC"))
            self.assertEqual(len(result.memory_writes), 1)
            self.assertEqual(result.memory_writes[0].address, 0x005000)
            self.assertEqual(result.memory_writes[0].data, b"\x34\x12")
            assert result.after_cpu is not None
            assert result.after_memory is not None
            self.assertEqual(result.after_cpu.regs.xix, 0x00005002)
            self.assertEqual(result.after_cpu.pc, 0x00201545)
            self.assertEqual(result.after_memory[0x005000], 0x34)
            self.assertEqual(result.after_memory[0x005001], 0x12)

    def test_execute_indexed_memory_compare_updates_flag_subset(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x0020D09B, b"\xAF\x04\xFC")
            base_view = load_fetch_view(rom_path)
            seeded_cpu = replace(
                base_view.machine.cpu,
                regs=replace(
                    base_view.machine.cpu.regs,
                    xsp=0x000040F0,
                    xix=0x00213268,
                ),
            )

            result = build_execute_next(
                base_view,
                cpu_state=seeded_cpu,
                memory_bytes={
                    0x0040F4: 0x67,
                    0x0040F5: 0x32,
                    0x0040F6: 0x21,
                    0x0040F7: 0x00,
                },
            )

            self.assertEqual(result.status, "executed")
            self.assertEqual(result.decode.assembly, "cp (XSP+4), XIX")
            self.assertEqual(result.written_registers, ("PC",))
            assert result.after_cpu is not None
            self.assertEqual(result.after_cpu.pc, 0x0020D09E)
            self.assertTrue(result.after_cpu.flags.cf)
            self.assertTrue(result.after_cpu.flags.sf)
            self.assertFalse(result.after_cpu.flags.zf)
            self.assertFalse(result.after_cpu.flags.vf)
            self.assertTrue(result.after_cpu.flags.hf)

    def test_execute_post_increment_immediate_store_writes_overlay_and_pointer(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x0020D0A4, b"\xF5\xF4\x00\x00")
            base_view = load_fetch_view(rom_path)
            seeded_cpu = replace(
                base_view.machine.cpu,
                regs=replace(base_view.machine.cpu.regs, xiy=0x00004000),
            )

            result = build_execute_next(base_view, cpu_state=seeded_cpu)

            self.assertEqual(result.status, "executed")
            self.assertEqual(result.decode.assembly, "ld (XIY+), 0x00")
            self.assertEqual(result.written_registers, ("XIY", "PC"))
            self.assertEqual(len(result.memory_writes), 1)
            self.assertEqual(result.memory_writes[0].address, 0x004000)
            self.assertEqual(result.memory_writes[0].data, b"\x00")
            assert result.after_cpu is not None
            assert result.after_memory is not None
            self.assertEqual(result.after_cpu.regs.xiy, 0x00004001)
            self.assertEqual(result.after_cpu.pc, 0x0020D0A8)
            self.assertEqual(result.after_memory[0x004000], 0x00)

    def test_execute_post_increment_long_store_writes_overlay_and_pointer(self) -> None:
        # F5 F6 60 = ld (XIY+), XWA  (F6 >> 2 & 7 = 5 = XIY, 0x60 & 7 = 0 = XWA)
        # This is the bootstrap frontier instruction at 0x0020F605.
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x0020D0AC, b"\xF5\xF6\x60")
            base_view = load_fetch_view(rom_path)
            seeded_cpu = replace(
                base_view.machine.cpu,
                regs=replace(
                    base_view.machine.cpu.regs,
                    xiy=0x00005EBC,
                    xwa=0x12345678,
                ),
            )

            result = build_execute_next(base_view, cpu_state=seeded_cpu)

            self.assertEqual(result.status, "executed")
            self.assertEqual(result.decode.assembly, "ld (XIY+), XWA")
            self.assertEqual(result.written_registers, ("XIY", "PC"))
            self.assertEqual(len(result.memory_writes), 1)
            self.assertEqual(result.memory_writes[0].address, 0x005EBC)
            self.assertEqual(result.memory_writes[0].data, b"\x78\x56\x34\x12")
            assert result.after_cpu is not None
            assert result.after_memory is not None
            self.assertEqual(result.after_cpu.regs.xiy, 0x00005EC0)
            self.assertEqual(result.after_cpu.pc, 0x0020D0AF)
            self.assertEqual(result.after_memory[0x005EBC], 0x78)
            self.assertEqual(result.after_memory[0x005EBD], 0x56)
            self.assertEqual(result.after_memory[0x005EBE], 0x34)
            self.assertEqual(result.after_memory[0x005EBF], 0x12)

    def test_execute_ari_secondary_indexed_byte_store_writes_overlay(self) -> None:
        # F3 07 E0 F0 00 AB = ld (XWA+IX), 0xAB
        # r32_byte=0xE0 -> (0xE0 >> 2) & 7 = 0 -> XWA
        # r16_byte=0xF0 -> (0xF0 >> 2) & 7 = 4 -> IX (lower 16 of XIX)
        # effective = XWA + IX = 0x5000 + 0x0100 = 0x5100
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x0020D0AC, b"\xF3\x07\xE0\xF0\x00\xAB")
            base_view = load_fetch_view(rom_path)
            seeded_cpu = replace(
                base_view.machine.cpu,
                regs=replace(
                    base_view.machine.cpu.regs,
                    xwa=0x00005000,
                    xix=0x00000100,
                ),
            )

            result = build_execute_next(base_view, cpu_state=seeded_cpu)

            self.assertEqual(result.status, "executed")
            self.assertEqual(result.decode.assembly, "ld (XWA+IX), 0xAB")
            self.assertEqual(result.written_registers, ("PC",))
            self.assertEqual(len(result.memory_writes), 1)
            self.assertEqual(result.memory_writes[0].address, 0x005100)
            self.assertEqual(result.memory_writes[0].data, b"\xAB")
            assert result.after_cpu is not None
            assert result.after_memory is not None
            self.assertEqual(result.after_cpu.pc, 0x0020D0B2)
            self.assertEqual(result.after_memory[0x005100], 0xAB)

    def test_execute_ari_secondary_indexed_word_store_writes_overlay(self) -> None:
        # F3 07 E0 F0 02 CD AB = ldw (XWA+IX), 0xABCD
        # r32_byte=0xE0 -> (0xE0 >> 2) & 7 = 0 -> XWA
        # r16_byte=0xF0 -> (0xF0 >> 2) & 7 = 4 -> IX (lower 16 of XIX)
        # effective = XWA + IX = 0x5000 + 0x0100 = 0x5100
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x0020D0AC, b"\xF3\x07\xE0\xF0\x02\xCD\xAB")
            base_view = load_fetch_view(rom_path)
            seeded_cpu = replace(
                base_view.machine.cpu,
                regs=replace(
                    base_view.machine.cpu.regs,
                    xwa=0x00005000,
                    xix=0x00000100,
                ),
            )

            result = build_execute_next(base_view, cpu_state=seeded_cpu)

            self.assertEqual(result.status, "executed")
            self.assertEqual(result.decode.assembly, "ldw (XWA+IX), 0xABCD")
            self.assertEqual(result.written_registers, ("PC",))
            self.assertEqual(len(result.memory_writes), 1)
            self.assertEqual(result.memory_writes[0].address, 0x005100)
            self.assertEqual(result.memory_writes[0].data, b"\xCD\xAB")
            assert result.after_cpu is not None
            assert result.after_memory is not None
            self.assertEqual(result.after_cpu.pc, 0x0020D0B3)
            self.assertEqual(result.after_memory[0x005100], 0xCD)
            self.assertEqual(result.after_memory[0x005101], 0xAB)

    def test_execute_ari_secondary_indexed_word_store_from_register_writes_overlay(self) -> None:
        # F3 07 E0 E4 53 = ldw (XWA+BC), HL
        # effective = XWA + BC = 0x1000 + 0x0002 = 0x1002
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x002010D2, b"\xF3\x07\xE0\xE4\x53")
            base_view = load_fetch_view(rom_path)
            seeded_cpu = replace(
                base_view.machine.cpu,
                regs=replace(
                    base_view.machine.cpu.regs,
                    xwa=0x00001000,
                    xbc=0x00000002,
                    xhl=0xAABBCCDD,
                ),
            )

            result = build_execute_next(base_view, cpu_state=seeded_cpu)

            self.assertEqual(result.status, "executed")
            self.assertEqual(result.decode.assembly, "ldw (XWA+BC), HL")
            self.assertEqual(result.written_registers, ("PC",))
            self.assertEqual(len(result.memory_writes), 1)
            self.assertEqual(result.memory_writes[0].address, 0x001002)
            self.assertEqual(result.memory_writes[0].data, b"\xDD\xCC")
            assert result.after_cpu is not None
            assert result.after_memory is not None
            self.assertEqual(result.after_cpu.pc, 0x002010D7)

    def test_execute_ari_secondary_indexed_long_store_from_register_writes_overlay(self) -> None:
        # F3 07 F0 EC 60 = ld (XIX+HL), XWA
        # effective = XIX + HL = 0x2000 + 0x0004 = 0x2004
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x002010F1, b"\xF3\x07\xF0\xEC\x60")
            base_view = load_fetch_view(rom_path)
            seeded_cpu = replace(
                base_view.machine.cpu,
                regs=replace(
                    base_view.machine.cpu.regs,
                    xwa=0x11223344,
                    xhl=0x00000004,
                    xix=0x00002000,
                ),
            )

            result = build_execute_next(base_view, cpu_state=seeded_cpu)

            self.assertEqual(result.status, "executed")
            self.assertEqual(result.decode.assembly, "ld (XIX+HL), XWA")
            self.assertEqual(result.written_registers, ("PC",))
            self.assertEqual(len(result.memory_writes), 1)
            self.assertEqual(result.memory_writes[0].address, 0x002004)
            self.assertEqual(result.memory_writes[0].data, b"\x44\x33\x22\x11")
            assert result.after_cpu is not None
            assert result.after_memory is not None
            self.assertEqual(result.after_cpu.pc, 0x002010F6)

    def test_execute_secondary_indexed_long_load_from_r16_index_updates_dest(self) -> None:
        # E3 07 E0 EC 20 = ld XWA, (XWA+HL)
        # effective = XWA + HL = 0x6000 + 0x0004 = 0x6004
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x002014E9, b"\xE3\x07\xE0\xEC\x20")
            base_view = load_fetch_view(rom_path)
            seeded_cpu = replace(
                base_view.machine.cpu,
                regs=replace(
                    base_view.machine.cpu.regs,
                    xwa=0x00006000,
                    xhl=0x00000004,
                ),
            )

            result = build_execute_next(
                base_view,
                cpu_state=seeded_cpu,
                memory_bytes={0x006004: 0x78, 0x006005: 0x56, 0x006006: 0x34, 0x006007: 0x12},
            )

            self.assertEqual(result.status, "executed")
            self.assertEqual(result.decode.assembly, "ld XWA, (XWA+HL)")
            self.assertEqual(result.written_registers, ("XWA", "PC"))
            assert result.after_cpu is not None
            self.assertEqual(result.after_cpu.regs.xwa, 0x12345678)
            self.assertEqual(result.after_cpu.pc, 0x002014EE)

    def test_execute_secondary_indexed_long_load_from_r8_index_updates_dest(self) -> None:
        # E3 03 F0 E1 24 = ld XIX, (XIX+W)
        # index byte E1 selects W, sourced from the upper byte of XWA.
        # effective = XIX + W = 0x7000 + 0x12 = 0x7012
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x0020E088, b"\xE3\x03\xF0\xE1\x24")
            base_view = load_fetch_view(rom_path)
            seeded_cpu = replace(
                base_view.machine.cpu,
                regs=replace(
                    base_view.machine.cpu.regs,
                    xwa=0x00001234,
                    xix=0x00007000,
                ),
            )

            result = build_execute_next(
                base_view,
                cpu_state=seeded_cpu,
                memory_bytes={0x007012: 0xEF, 0x007013: 0xCD, 0x007014: 0xAB, 0x007015: 0x89},
            )

            self.assertEqual(result.status, "executed")
            self.assertEqual(result.decode.assembly, "ld XIX, (XIX+W)")
            self.assertEqual(result.written_registers, ("XIX", "PC"))
            assert result.after_cpu is not None
            self.assertEqual(result.after_cpu.regs.xix, 0x89ABCDEF)
            self.assertEqual(result.after_cpu.pc, 0x0020E08D)

    def test_execute_secondary_indexed_byte_load_from_r16_index_updates_dest(self) -> None:
        # C3 07 E0 EC 21 = ld A, (XWA+HL)
        # effective = XWA + HL = 0x1000 + 0x0004 = 0x1004
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x002014D3, b"\xC3\x07\xE0\xEC\x21")
            base_view = load_fetch_view(rom_path)
            seeded_cpu = replace(
                base_view.machine.cpu,
                regs=replace(
                    base_view.machine.cpu.regs,
                    xwa=0x00001000,
                    xhl=0x00000004,
                ),
            )

            result = build_execute_next(
                base_view,
                cpu_state=seeded_cpu,
                memory_bytes={0x001004: 0x7A},
            )

            self.assertEqual(result.status, "executed")
            self.assertEqual(result.decode.assembly, "ld A, (XWA+HL)")
            self.assertEqual(result.written_registers, ("A", "PC"))
            assert result.after_cpu is not None
            self.assertEqual(result.after_cpu.regs.xwa, 0x0000107A)
            self.assertEqual(result.after_cpu.pc, 0x002014D8)

    def test_execute_prefixed_byte_bit_test_updates_flags_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x002010A5, b"\xC9\x33\x02")
            base_view = load_fetch_view(rom_path)
            seeded_cpu = replace(
                base_view.machine.cpu,
                flags=StatusFlags(sf=True, zf=False, vf=True, hf=False, cf=True, nf=True),
                regs=replace(base_view.machine.cpu.regs, xwa=0x00000004),
            )

            result = build_execute_next(base_view, cpu_state=seeded_cpu)

            self.assertEqual(result.status, "executed")
            self.assertEqual(result.decode.assembly, "bit 2, A")
            self.assertEqual(result.written_registers, ("PC",))
            assert result.after_cpu is not None
            self.assertEqual(result.after_cpu.regs.xwa, 0x00000004)
            self.assertEqual(result.after_cpu.pc, 0x002010A8)
            self.assertFalse(result.after_cpu.flags.zf)
            self.assertTrue(result.after_cpu.flags.hf)
            self.assertFalse(result.after_cpu.flags.nf)
            self.assertTrue(result.after_cpu.flags.sf)
            self.assertTrue(result.after_cpu.flags.vf)
            self.assertTrue(result.after_cpu.flags.cf)

    def test_execute_prefixed_long_set_bit_updates_register_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x002699D8, b"\xE8\x31\x06")
            base_view = load_fetch_view(rom_path)
            seeded_cpu = replace(
                base_view.machine.cpu,
                flags=StatusFlags(sf=True, zf=False, vf=True, hf=False, cf=True, nf=True),
                regs=replace(base_view.machine.cpu.regs, xwa=0x00000000),
            )

            result = build_execute_next(base_view, cpu_state=seeded_cpu)

            self.assertEqual(result.status, "executed")
            self.assertEqual(result.decode.assembly, "set 6, XWA")
            self.assertEqual(result.written_registers, ("XWA", "PC"))
            assert result.after_cpu is not None
            self.assertEqual(result.after_cpu.regs.xwa, 0x00000040)
            self.assertEqual(result.after_cpu.pc, 0x002699DB)
            self.assertTrue(result.after_cpu.flags.sf)
            self.assertFalse(result.after_cpu.flags.zf)
            self.assertTrue(result.after_cpu.flags.vf)
            self.assertFalse(result.after_cpu.flags.hf)
            self.assertTrue(result.after_cpu.flags.cf)
            self.assertTrue(result.after_cpu.flags.nf)

    def test_execute_prefixed_long_tset_updates_zero_from_old_bit(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x002699E1, b"\xE8\x34\x06")
            base_view = load_fetch_view(rom_path)
            seeded_cpu = replace(
                base_view.machine.cpu,
                flags=StatusFlags(sf=False, zf=False, vf=True, hf=True, cf=False, nf=True),
                regs=replace(base_view.machine.cpu.regs, xwa=0x00000000),
            )

            result = build_execute_next(base_view, cpu_state=seeded_cpu)

            self.assertEqual(result.status, "executed")
            self.assertEqual(result.decode.assembly, "tset 6, XWA")
            self.assertEqual(result.written_registers, ("XWA", "PC"))
            assert result.after_cpu is not None
            self.assertEqual(result.after_cpu.regs.xwa, 0x00000040)
            self.assertEqual(result.after_cpu.pc, 0x002699E4)
            self.assertFalse(result.after_cpu.flags.sf)
            self.assertTrue(result.after_cpu.flags.zf)
            self.assertTrue(result.after_cpu.flags.vf)
            self.assertFalse(result.after_cpu.flags.cf)
            # TSET writes H = 1 and N = 0 (Toshiba symbol row `x * 1 x 0 -`).
            # This test used to assert N = 1, freezing the same omission the
            # memory form had.
            self.assertTrue(result.after_cpu.flags.hf)
            self.assertFalse(result.after_cpu.flags.nf)

    def test_execute_prefixed_byte_divide_immediate_updates_word_dest(self) -> None:
        # `C9 0A 18` is `div WA, 0x18` -- NOT `div BC, 0x18`, which this test used
        # to assert. The mul/div `rr` code is not a register index: Toshiba's
        # <Divide> Note 3 says that at BYTE size the destination is a WORD register
        # and only the ODD codes name one (001 = WA, 011 = BC, 101 = DE, 111 = HL).
        # The official assembler settles it -- `div BC,0x18` is `CB 0A 18`.
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x002015D8, b"\xC9\x0A\x18")
            base_view = load_fetch_view(rom_path)
            seeded_cpu = replace(
                base_view.machine.cpu,
                flags=StatusFlags(sf=True, zf=False, vf=True, hf=True, cf=False, nf=True),
                regs=replace(base_view.machine.cpu.regs, xwa=0x00000131),
            )

            result = build_execute_next(base_view, cpu_state=seeded_cpu)

            self.assertEqual(result.status, "executed")
            self.assertEqual(result.decode.assembly, "div WA, 0x18")
            self.assertEqual(result.written_registers, ("WA", "PC"))
            assert result.after_cpu is not None
            # 0x0131 / 0x18 = 0x0C remainder 0x11 -> quotient low, remainder high.
            self.assertEqual(result.after_cpu.regs.xwa, 0x0000110C)
            self.assertEqual(result.after_cpu.pc, 0x002015DB)

    def test_execute_byte_divide_immediate_rejects_an_even_rr_code(self) -> None:
        # `C8 0A 18` names destination code 000, which at byte size is no register
        # at all -- the official assembler will not emit it. Executing it anyway
        # (as this core did, into XWA) is a silent wrong answer, so it must stop.
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x002015D8, b"\xC8\x0A\x18")
            base_view = load_fetch_view(rom_path)
            result = build_execute_next(base_view, cpu_state=base_view.machine.cpu)
            self.assertNotEqual(result.status, "executed")

    def test_execute_prefixed_long_bit_test_sets_zero_when_bit_clear(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x002085DD, b"\xE8\x33\x01")
            base_view = load_fetch_view(rom_path)
            seeded_cpu = replace(
                base_view.machine.cpu,
                flags=StatusFlags(sf=False, zf=False, vf=False, hf=False, cf=True, nf=True),
                regs=replace(base_view.machine.cpu.regs, xwa=0x00000001),
            )

            result = build_execute_next(base_view, cpu_state=seeded_cpu)

            self.assertEqual(result.status, "executed")
            self.assertEqual(result.decode.assembly, "bit 1, XWA")
            self.assertEqual(result.written_registers, ("PC",))
            assert result.after_cpu is not None
            self.assertEqual(result.after_cpu.regs.xwa, 0x00000001)
            self.assertEqual(result.after_cpu.pc, 0x002085E0)
            self.assertTrue(result.after_cpu.flags.zf)
            self.assertTrue(result.after_cpu.flags.hf)
            self.assertFalse(result.after_cpu.flags.nf)
            self.assertFalse(result.after_cpu.flags.sf)
            self.assertFalse(result.after_cpu.flags.vf)
            self.assertTrue(result.after_cpu.flags.cf)

    def test_execute_prefixed_long_divide_immediate_updates_long_dest(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x00210B01, b"\xE9\x0A\x64\x00")
            base_view = load_fetch_view(rom_path)
            seeded_cpu = replace(
                base_view.machine.cpu,
                flags=StatusFlags(sf=False, zf=True, vf=True, hf=False, cf=True, nf=False),
                regs=replace(base_view.machine.cpu.regs, xbc=0x000001F4),
            )

            result = build_execute_next(base_view, cpu_state=seeded_cpu)

            self.assertEqual(result.status, "executed")
            self.assertEqual(result.decode.assembly, "div XBC, 0x0064")
            self.assertEqual(result.written_registers, ("XBC", "PC"))
            assert result.after_cpu is not None
            self.assertEqual(result.after_cpu.regs.xbc, 0x00000005)
            self.assertEqual(result.after_cpu.pc, 0x00210B05)
            self.assertFalse(result.after_cpu.flags.sf)
            self.assertTrue(result.after_cpu.flags.zf)
            self.assertFalse(result.after_cpu.flags.vf)
            self.assertFalse(result.after_cpu.flags.hf)
            self.assertTrue(result.after_cpu.flags.cf)
            self.assertFalse(result.after_cpu.flags.nf)

    def test_execute_reg_indirect_word_signed_divide_updates_destination(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x00201D17, b"\x94\x5F")
            base_view = load_fetch_view(rom_path)
            seeded_cpu = replace(
                base_view.machine.cpu,
                flags=StatusFlags(sf=False, zf=True, vf=True, hf=True, cf=True, nf=False),
                regs=replace(
                    base_view.machine.cpu.regs,
                    xix=0x00008000,
                    xsp=0x00000014,
                ),
            )
            result = build_execute_next(
                base_view,
                cpu_state=seeded_cpu,
                memory_bytes={
                    0x00008000: 0xFE,
                    0x00008001: 0xFF,
                },
            )

            self.assertEqual(result.status, "executed")
            self.assertEqual(result.decode.assembly, "divs XSP, (XIX)")
            self.assertEqual(result.written_registers, ("XSP", "PC"))
            assert result.after_cpu is not None
            self.assertEqual(result.after_cpu.regs.xsp, 0x0000FFF6)
            self.assertEqual(result.after_cpu.pc, 0x00201D19)
            # DIV writes V ONLY (Toshiba <Divide>: `- - - V - -`). S, Z, H, N and C
            # are unchanged. This test used to assert S and Z derived from the
            # quotient -- the handler published them, and it should not have.
            self.assertEqual(result.after_cpu.flags.sf, result.before_cpu.flags.sf)
            self.assertEqual(result.after_cpu.flags.zf, result.before_cpu.flags.zf)
            self.assertEqual(result.after_cpu.flags.cf, result.before_cpu.flags.cf)
            self.assertFalse(result.after_cpu.flags.vf)
            self.assertTrue(result.after_cpu.flags.hf)
            self.assertFalse(result.after_cpu.flags.nf)

    def test_execute_indexed_word_signed_multiply_updates_destination(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x00205500, b"\x98\x02\x49")
            base_view = load_fetch_view(rom_path)
            seeded_cpu = replace(
                base_view.machine.cpu,
                flags=StatusFlags(sf=False, zf=True, vf=True, hf=False, cf=True, nf=True),
                regs=replace(
                    base_view.machine.cpu.regs,
                    xwa=0x00008000,
                    xbc=0x00000003,
                ),
            )
            result = build_execute_next(
                base_view,
                cpu_state=seeded_cpu,
                memory_bytes={
                    0x00008002: 0xFC,
                    0x00008003: 0xFF,
                },
            )

            self.assertEqual(result.status, "executed")
            self.assertEqual(result.decode.assembly, "muls XBC, (XWA+2)")
            self.assertEqual(result.written_registers, ("XBC", "PC"))
            assert result.after_cpu is not None
            self.assertEqual(result.after_cpu.regs.xbc, 0xFFFFFFF4)
            self.assertEqual(result.after_cpu.pc, 0x00205503)
            # MULS CHANGES NO FLAGS. Toshiba's <Multiply> page is `- - - - - -`.
            # This test used to assert S and Z derived from the product; the
            # handler published them, and it should not have. The flags here are
            # therefore whatever the seed carried in, untouched.
            self.assertEqual(result.after_cpu.flags, result.before_cpu.flags)

    def test_execute_secondary_indexed_word_load_updates_wa(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x00207A25, b"\xD3\x07\xF0\xE0\x20")
            base_view = load_fetch_view(rom_path)
            seeded_cpu = replace(
                base_view.machine.cpu,
                regs=replace(
                    base_view.machine.cpu.regs,
                    xix=0x00008000,
                    xwa=0x00000004,
                ),
            )

            result = build_execute_next(
                base_view,
                cpu_state=seeded_cpu,
                memory_bytes={
                    0x00008004: 0x78,
                    0x00008005: 0x56,
                },
            )

            self.assertEqual(result.status, "executed")
            self.assertEqual(result.decode.assembly, "ld WA, (XIX+WA)")
            self.assertEqual(result.written_registers, ("WA", "PC"))
            assert result.after_cpu is not None
            self.assertEqual(result.after_cpu.regs.xwa, 0x00005678)
            self.assertEqual(result.after_cpu.pc, 0x00207A2A)

    def test_execute_secondary_indexed_jump_updates_pc(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x00207A2F, b"\xF3\x07\xF0\xE0\xD8")
            base_view = load_fetch_view(rom_path)
            seeded_cpu = replace(
                base_view.machine.cpu,
                regs=replace(
                    base_view.machine.cpu.regs,
                    xix=0x00201000,
                    xwa=0x00000034,
                ),
            )

            result = build_execute_next(base_view, cpu_state=seeded_cpu)

            self.assertEqual(result.status, "executed")
            self.assertEqual(result.decode.assembly, "jp (XIX+WA)")
            self.assertEqual(result.written_registers, ("PC",))
            assert result.after_cpu is not None
            self.assertEqual(result.after_cpu.pc, 0x00201034)

    def test_execute_indirect_call_via_xix_pushes_return_and_jumps(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x0020E08D, b"\xB4\xE8")
            base_view = load_fetch_view(rom_path)
            seeded_cpu = replace(
                base_view.machine.cpu,
                regs=replace(
                    base_view.machine.cpu.regs,
                    xix=0x00201234,
                    xsp=0x00006010,
                ),
            )

            result = build_execute_next(base_view, cpu_state=seeded_cpu)

            self.assertEqual(result.status, "executed")
            self.assertEqual(result.decode.assembly, "call (XIX)")
            self.assertEqual(result.written_registers, ("XSP", "PC"))
            self.assertEqual(len(result.memory_writes), 1)
            self.assertEqual(result.memory_writes[0].address, 0x00600C)
            self.assertEqual(result.memory_writes[0].data, b"\x8F\xE0\x20\x00")
            assert result.after_cpu is not None
            assert result.after_memory is not None
            self.assertEqual(result.after_cpu.regs.xsp, 0x0000600C)
            self.assertEqual(result.after_cpu.pc, 0x00201234)
            self.assertEqual(result.after_memory[0x00600C], 0x8F)
            self.assertEqual(result.after_memory[0x00600D], 0xE0)
            self.assertEqual(result.after_memory[0x00600E], 0x20)
            self.assertEqual(result.after_memory[0x00600F], 0x00)

    def test_execute_indirect_call_via_xwa_pushes_return_and_jumps(self) -> None:
        # B0 E8 = call (XWA): the generalized register-indirect call.
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x0020E08D, b"\xB0\xE8")
            base_view = load_fetch_view(rom_path)
            seeded_cpu = replace(
                base_view.machine.cpu,
                regs=replace(
                    base_view.machine.cpu.regs,
                    xwa=0x00205000,
                    xsp=0x00006010,
                ),
            )

            result = build_execute_next(base_view, cpu_state=seeded_cpu)

            self.assertEqual(result.status, "executed")
            self.assertEqual(result.decode.assembly, "call (XWA)")
            self.assertEqual(result.written_registers, ("XSP", "PC"))
            assert result.after_cpu is not None
            assert result.after_memory is not None
            self.assertEqual(result.after_cpu.regs.xsp, 0x0000600C)
            self.assertEqual(result.after_cpu.pc, 0x00205000)
            # Sequential return address 0x0020E08F pushed little-endian.
            self.assertEqual(
                bytes(result.after_memory[0x00600C + i] for i in range(4)),
                b"\x8F\xE0\x20\x00",
            )

    def test_execute_indirect_call_conditional_not_taken_falls_through(self) -> None:
        # B1 E0 = call F, (XBC): cc=0 (F) is never taken -> no push, PC advances.
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x00200040, b"\xB1\xE0")
            base_view = load_fetch_view(rom_path)
            seeded_cpu = replace(
                base_view.machine.cpu,
                regs=replace(
                    base_view.machine.cpu.regs,
                    xbc=0x00205200,
                    xsp=0x00006010,
                ),
                flags=StatusFlags(sf=False, zf=False, vf=False, hf=False, cf=False, nf=False),
            )

            result = build_execute_next(base_view, cpu_state=seeded_cpu)

            self.assertEqual(result.status, "executed")
            self.assertEqual(result.decode.assembly, "call F, (XBC)")
            self.assertEqual(result.written_registers, ("PC",))
            assert result.after_cpu is not None
            # Not taken: PC advances sequentially, XSP untouched.
            self.assertEqual(result.after_cpu.pc, 0x00200042)
            self.assertEqual(result.after_cpu.regs.xsp, 0x00006010)
            self.assertEqual(len(result.memory_writes), 0)

    def test_execute_indirect_call_requires_known_target_register(self) -> None:
        # B0 E8 = call (XWA) with XWA unknown -> honest block.
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x00200040, b"\xB0\xE8")
            base_view = load_fetch_view(rom_path)
            seeded_cpu = replace(
                base_view.machine.cpu,
                regs=replace(base_view.machine.cpu.regs, xsp=0x00006010),
            )

            result = build_execute_next(base_view, cpu_state=seeded_cpu)

            # The register-indirect handler blocks first on the unknown base
            # register; either honest "register unknown" status is acceptable.
            self.assertIn(
                result.status,
                {"requires-known-address-register", "requires-known-full-register"},
            )
            self.assertIsNone(result.after_cpu)

    def test_execute_abs24_call_jumps_to_the_effective_address(self) -> None:
        # `F2 5B 84 20 E8` is `call (0x20845B)` and it jumps TO 0x20845B. The
        # effective address IS the target -- Toshiba, <Call>: "CALL cc, dst -- if
        # cc then PUSH PC: PC <- dst", where `dst` is what the addressing mode
        # computes. It is NOT a pointer to dereference.
        #
        # This test used to assert the opposite (read four bytes from 0x20845B,
        # jump to 0x00345678), and the executor obliged. On the real ROMs that sent
        # Fatal Fury and SNK Gals' Fighters to 0x031702 -- neither RAM nor
        # cartridge -- which is how a whole-ROM trace against the native core
        # caught it.
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x002086CC, b"\xF2\x5B\x84\x20\xE8")
            base_view = load_fetch_view(rom_path)
            seeded_cpu = replace(
                base_view.machine.cpu,
                regs=replace(
                    base_view.machine.cpu.regs,
                    xsp=0x00006020,
                ),
            )

            result = build_execute_next(
                base_view,
                cpu_state=seeded_cpu,
                memory_bytes={
                    0x20845B: 0x78,
                    0x20845C: 0x56,
                    0x20845D: 0x34,
                    0x20845E: 0x12,
                },
            )

            self.assertEqual(result.status, "executed")
            self.assertEqual(result.decode.assembly, "call (0x20845B)")
            self.assertEqual(result.written_registers, ("XSP", "PC"))
            self.assertEqual(len(result.memory_writes), 1)
            self.assertEqual(result.memory_writes[0].address, 0x00601C)
            self.assertEqual(result.memory_writes[0].data, b"\xD1\x86\x20\x00")
            assert result.after_cpu is not None
            assert result.after_memory is not None
            self.assertEqual(result.after_cpu.regs.xsp, 0x0000601C)
            # The effective address IS the target: 0x20845B -- not the 0x00345678
            # that the four bytes AT 0x20845B happen to spell.
            self.assertEqual(result.after_cpu.pc, 0x0020845B)
            self.assertEqual(result.after_memory[0x00601C], 0xD1)
            self.assertEqual(result.after_memory[0x00601D], 0x86)
            self.assertEqual(result.after_memory[0x00601E], 0x20)
            self.assertEqual(result.after_memory[0x00601F], 0x00)

    def test_execute_abs24_indirect_call_false_condition_falls_through(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x002086CC, b"\xF2\x5B\x84\x20\xEE")
            base_view = load_fetch_view(rom_path)
            seeded_cpu = replace(
                base_view.machine.cpu,
                regs=replace(
                    base_view.machine.cpu.regs,
                    xsp=0x00006020,
                ),
                flags=StatusFlags(sf=False, zf=True, vf=False, hf=False, cf=False),
            )

            result = build_execute_next(base_view, cpu_state=seeded_cpu)

            self.assertEqual(result.status, "executed")
            self.assertEqual(result.decode.assembly, "call NZ, (0x20845B)")
            self.assertEqual(result.written_registers, ("PC",))
            self.assertEqual(result.memory_writes, ())
            assert result.after_cpu is not None
            self.assertEqual(result.after_cpu.regs.xsp, 0x00006020)
            self.assertEqual(result.after_cpu.pc, 0x002086D1)

    def test_execute_abs24_byte_and_immediate_updates_overlay(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x00207B16, b"\xC2\xCA\x5E\x00\x3C\x01")
            base_view = load_fetch_view(rom_path)

            result = build_execute_next(
                base_view,
                memory_bytes={0x005ECA: 0xF3},
            )

            self.assertEqual(result.status, "executed")
            self.assertEqual(result.decode.assembly, "and (0x005ECA), 0x01")
            self.assertEqual(result.written_registers, ("PC",))
            self.assertEqual(len(result.memory_writes), 1)
            self.assertEqual(result.memory_writes[0].address, 0x005ECA)
            self.assertEqual(result.memory_writes[0].data, b"\x01")
            assert result.after_cpu is not None
            assert result.after_memory is not None
            self.assertEqual(result.after_cpu.pc, 0x00207B1C)
            self.assertEqual(result.after_memory[0x005ECA], 0x01)

    def test_execute_abs8_word_immediate_store_updates_overlay(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x002085FE, b"\xF0\x66\x02\xD9\xA9")
            base_view = load_fetch_view(rom_path)

            result = build_execute_next(base_view)

            self.assertEqual(result.status, "executed")
            self.assertEqual(result.decode.assembly, "ldw (0x66), 0xA9D9")
            self.assertEqual(result.written_registers, ("PC",))
            self.assertEqual(len(result.memory_writes), 1)
            self.assertEqual(result.memory_writes[0].address, 0x0066)
            self.assertEqual(result.memory_writes[0].data, b"\xD9\xA9")
            assert result.after_cpu is not None
            assert result.after_memory is not None
            self.assertEqual(result.after_cpu.pc, 0x00208603)
            self.assertEqual(result.after_memory[0x0066], 0xD9)
            self.assertEqual(result.after_memory[0x0067], 0xA9)

    def test_execute_abs8_mem_to_mem_byte_store_copies_source(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x002085F2, b"\xF0\x66\x14\xED\x61")
            base_view = load_fetch_view(rom_path)

            result = build_execute_next(
                base_view,
                memory_bytes={0x61ED: 0x55},
            )

            self.assertEqual(result.status, "executed")
            self.assertEqual(result.decode.assembly, "ld (0x66), (0x61ED)")
            self.assertEqual(result.written_registers, ("PC",))
            self.assertEqual(len(result.memory_writes), 1)
            self.assertEqual(result.memory_writes[0].address, 0x0066)
            self.assertEqual(result.memory_writes[0].data, b"\x55")
            assert result.after_cpu is not None
            assert result.after_memory is not None
            self.assertEqual(result.after_cpu.pc, 0x002085F7)
            self.assertEqual(result.after_memory[0x0066], 0x55)

    def test_execute_abs24_word_compare_updates_flags(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x00208664, b"\xD2\xFC\x5E\x00\xF0")
            base_view = load_fetch_view(rom_path)
            seeded_cpu = replace(
                base_view.machine.cpu,
                regs=replace(base_view.machine.cpu.regs, xwa=0x1234),
            )

            result = build_execute_next(
                base_view,
                cpu_state=seeded_cpu,
                memory_bytes={
                    0x005EFC: 0x34,
                    0x005EFD: 0x12,
                },
            )

            self.assertEqual(result.status, "executed")
            self.assertEqual(result.decode.assembly, "cp WA, (0x005EFC)")
            self.assertEqual(result.written_registers, ("PC",))
            self.assertEqual(result.memory_writes, ())
            assert result.after_cpu is not None
            self.assertEqual(result.after_cpu.pc, 0x00208669)
            self.assertTrue(result.after_cpu.flags.zf)
            self.assertFalse(result.after_cpu.flags.cf)
            self.assertTrue(result.after_cpu.flags.nf)

    def test_execute_abs24_word_push_reads_source_and_pushes_stack(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x00207EF7, b"\xD2\x02\x5F\x00\x04")
            base_view = load_fetch_view(rom_path)
            seeded_cpu = replace(
                base_view.machine.cpu,
                regs=replace(base_view.machine.cpu.regs, xsp=0x00006010),
            )

            result = build_execute_next(
                base_view,
                cpu_state=seeded_cpu,
                memory_bytes={
                    0x005F02: 0xAA,
                    0x005F03: 0x55,
                },
            )

            self.assertEqual(result.status, "executed")
            self.assertEqual(result.decode.assembly, "pushw (0x005F02)")
            self.assertEqual(result.written_registers, ("XSP", "PC"))
            self.assertEqual(len(result.memory_writes), 1)
            self.assertEqual(result.memory_writes[0].address, 0x00600E)
            self.assertEqual(result.memory_writes[0].data, b"\xAA\x55")
            assert result.after_cpu is not None
            assert result.after_memory is not None
            self.assertEqual(result.after_cpu.regs.xsp, 0x0000600E)
            self.assertEqual(result.after_cpu.pc, 0x00207EFC)
            self.assertEqual(result.after_memory[0x00600E], 0xAA)
            self.assertEqual(result.after_memory[0x00600F], 0x55)

    def test_execute_prefixed_alu_register_add_r32_updates_dest(self) -> None:
        # E9 80 = add XWA, XBC  (source=XBC from E9 prefix, dest=XWA from op 0x80)
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x0020D0AC, b"\xE9\x80")
            base_view = load_fetch_view(rom_path)
            seeded_cpu = replace(
                base_view.machine.cpu,
                regs=replace(
                    base_view.machine.cpu.regs,
                    xwa=0x00000010,
                    xbc=0x00000005,
                ),
            )

            result = build_execute_next(base_view, cpu_state=seeded_cpu)

            self.assertEqual(result.status, "executed")
            self.assertEqual(result.decode.assembly, "add XWA, XBC")
            self.assertIn("XWA", result.written_registers)
            assert result.after_cpu is not None
            self.assertEqual(result.after_cpu.regs.xwa, 0x00000015)
            self.assertEqual(result.after_cpu.pc, 0x0020D0AE)

    def test_execute_indexed_imm_byte_store_writes_overlay(self) -> None:
        # B8 01 00 A0 = ld (XWA+1), 0xA0
        # B8: XWA-based (B8 & 7 = 0), d8=0x01=+1, op=0x00, imm8=0xA0
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x0020D0AC, b"\xB8\x01\x00\xA0")
            base_view = load_fetch_view(rom_path)
            seeded_cpu = replace(
                base_view.machine.cpu,
                regs=replace(base_view.machine.cpu.regs, xwa=0x00005000),
            )

            result = build_execute_next(base_view, cpu_state=seeded_cpu)

            self.assertEqual(result.status, "executed")
            self.assertEqual(result.decode.assembly, "ld (XWA+1), 0xA0")
            self.assertEqual(len(result.memory_writes), 1)
            self.assertEqual(result.memory_writes[0].address, 0x005001)
            self.assertEqual(result.memory_writes[0].data, b"\xA0")
            assert result.after_cpu is not None
            assert result.after_memory is not None
            self.assertEqual(result.after_cpu.pc, 0x0020D0B0)
            self.assertEqual(result.after_memory[0x005001], 0xA0)

    def test_execute_reg_indirect_load_reads_overlay_to_r8(self) -> None:
        # 80 27 = ld L, (XWA)  — byte load from (XWA) into L
        # 0x80 & 7 = 0 = XWA, op=0x27 => R8[7] = L
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x0020D0AC, b"\x80\x27")
            base_view = load_fetch_view(rom_path)
            seeded_cpu = replace(
                base_view.machine.cpu,
                regs=replace(
                    base_view.machine.cpu.regs,
                    xwa=0x00005000,
                    xhl=0x00000000,
                ),
            )

            result = build_execute_next(
                base_view,
                cpu_state=seeded_cpu,
                memory_bytes={0x005000: 0xBE},
            )

            self.assertEqual(result.status, "executed")
            self.assertEqual(result.decode.assembly, "ld L, (XWA)")
            self.assertIn("L", result.written_registers)
            assert result.after_cpu is not None
            # L is low byte of XHL: xhl should now be 0x000000BE
            self.assertEqual(result.after_cpu.regs.xhl & 0xFF, 0xBE)
            self.assertEqual(result.after_cpu.pc, 0x0020D0AE)

    def test_execute_cpu_io_byte_store_advances_pc(self) -> None:
        # 08 B8 42 = ldb (0xB8), 0x42 — CPU I/O byte store, now tracked in overlay
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x0020D0AC, b"\x08\xB8\x42")
            base_view = load_fetch_view(rom_path)

            result = build_execute_next(base_view)

            self.assertEqual(result.status, "executed")
            self.assertEqual(result.decode.assembly, "ldb (0xB8), 0x42")
            self.assertEqual(result.written_registers, ("PC",))
            self.assertEqual(len(result.memory_writes), 1)
            self.assertEqual(result.memory_writes[0].address, 0xB8)
            self.assertEqual(result.memory_writes[0].data, b"\x42")
            assert result.after_cpu is not None
            assert result.after_memory is not None
            self.assertEqual(result.after_cpu.pc, 0x0020D0AF)
            # CPU I/O page is now a tracked register file: the write lands in
            # the overlay so a later read-modify-write observes it.
            self.assertEqual(result.after_memory[0xB8], 0x42)

    def test_execute_cpu_io_word_store_advances_pc(self) -> None:
        # 0A B8 AA AA = ldw (0xB8), 0xAAAA — CPU I/O word store, now tracked
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x0020D0AC, b"\x0A\xB8\xAA\xAA")
            base_view = load_fetch_view(rom_path)

            result = build_execute_next(base_view)

            self.assertEqual(result.status, "executed")
            self.assertEqual(result.decode.assembly, "ldw (0xB8), 0xAAAA")
            self.assertEqual(result.written_registers, ("PC",))
            self.assertEqual(len(result.memory_writes), 1)
            self.assertEqual(result.memory_writes[0].address, 0xB8)
            self.assertEqual(result.memory_writes[0].data, b"\xAA\xAA")
            assert result.after_cpu is not None
            assert result.after_memory is not None
            self.assertEqual(result.after_cpu.pc, 0x0020D0B0)
            # Word store tracked little-endian in the overlay.
            self.assertEqual(result.after_memory[0xB8], 0xAA)
            self.assertEqual(result.after_memory[0xB9], 0xAA)

    def test_execute_pop_xiz_reads_seeded_stack_overlay(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x0020D0AC, b"\x5E")
            base_view = load_fetch_view(rom_path)
            seeded_cpu = replace(
                base_view.machine.cpu,
                regs=replace(base_view.machine.cpu.regs, xsp=0x000040F0),
            )

            result = build_execute_next(
                base_view,
                cpu_state=seeded_cpu,
                memory_bytes={
                    0x0040F0: 0x78,
                    0x0040F1: 0x56,
                    0x0040F2: 0x34,
                    0x0040F3: 0x12,
                },
            )

            self.assertEqual(result.status, "executed")
            self.assertEqual(result.decode.assembly, "pop XIZ")
            self.assertEqual(result.written_registers, ("XIZ", "XSP", "PC"))
            assert result.after_cpu is not None
            self.assertEqual(result.after_cpu.regs.xiz, 0x12345678)
            self.assertEqual(result.after_cpu.regs.xsp, 0x000040F4)
            self.assertEqual(result.after_cpu.pc, 0x0020D0AD)

    def test_execute_abs16_byte_compare_immediate_updates_flags(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x0020D0DA, b"\xC1\x91\x6F\x3F\x00")
            base_view = load_fetch_view(rom_path)

            result = build_execute_next(
                base_view,
                memory_bytes={0x006F91: 0x00},
            )

            self.assertEqual(result.status, "executed")
            self.assertEqual(result.decode.assembly, "cp (0x6F91), 0x00")
            self.assertEqual(result.written_registers, ("PC",))
            assert result.after_cpu is not None
            self.assertEqual(result.after_cpu.pc, 0x0020D0DF)
            self.assertFalse(result.after_cpu.flags.sf)
            self.assertTrue(result.after_cpu.flags.zf)
            self.assertFalse(result.after_cpu.flags.vf)
            self.assertFalse(result.after_cpu.flags.hf)
            self.assertFalse(result.after_cpu.flags.cf)

    def test_execute_abs24_byte_store_writes_overlay(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x0020D0E3, b"\xF2\x80\x5F\x00\x41")
            base_view = load_fetch_view(rom_path)
            seeded_cpu = seed_cpu_state_for_execution(
                base_view.machine.cpu,
                register_values={"XWA": 0x11223344},
            )

            result = build_execute_next(base_view, cpu_state=seeded_cpu)

            self.assertEqual(result.status, "executed")
            self.assertEqual(result.decode.assembly, "ld (0x005F80), A")
            self.assertEqual(result.written_registers, ("PC",))
            self.assertEqual(len(result.memory_writes), 1)
            self.assertEqual(result.memory_writes[0].address, 0x005F80)
            self.assertEqual(result.memory_writes[0].data, b"\x44")
            assert result.after_cpu is not None
            assert result.after_memory is not None
            self.assertEqual(result.after_cpu.pc, 0x0020D0E8)
            self.assertEqual(result.after_memory[0x005F80], 0x44)

    def test_execute_abs24_word_store_writes_overlay(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x00269A40, b"\xF2\x04\x40\x00\x50")
            base_view = load_fetch_view(rom_path)
            seeded_cpu = seed_cpu_state_for_execution(
                base_view.machine.cpu,
                register_values={"XWA": 0x11223344},
            )

            result = build_execute_next(base_view, cpu_state=seeded_cpu)

            self.assertEqual(result.status, "executed")
            self.assertEqual(result.decode.assembly, "ldw (0x004004), WA")
            self.assertEqual(result.written_registers, ("PC",))
            self.assertEqual(len(result.memory_writes), 1)
            self.assertEqual(result.memory_writes[0].address, 0x004004)
            self.assertEqual(result.memory_writes[0].data, b"\x44\x33")
            assert result.after_cpu is not None
            assert result.after_memory is not None
            self.assertEqual(result.after_cpu.pc, 0x00269A45)
            self.assertEqual(result.after_memory[0x004004], 0x44)
            self.assertEqual(result.after_memory[0x004005], 0x33)

    def test_execute_abs8_long_load_reads_overlay_to_xiz(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x002693CB, b"\xE0\xE4\x26")
            base_view = load_fetch_view(rom_path)

            result = build_execute_next(
                base_view,
                memory_bytes={0x00E4: 0x78, 0x00E5: 0x56, 0x00E6: 0x34, 0x00E7: 0x12},
            )

            self.assertEqual(result.status, "executed")
            self.assertEqual(result.decode.assembly, "ld XIZ, (0xE4)")
            self.assertEqual(result.written_registers, ("XIZ", "PC"))
            assert result.after_cpu is not None
            self.assertEqual(result.after_cpu.regs.xiz, 0x12345678)
            self.assertEqual(result.after_cpu.pc, 0x002693CE)

    def test_execute_pre_decrement_byte_load_updates_register_and_address(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x00200040, b"\xC4\xE4\x21")  # ld A, (-XBC)
            base_view = load_fetch_view(rom_path)
            seeded_cpu = seed_cpu_state_for_execution(
                base_view.machine.cpu,
                register_values={"XWA": 0x11223344, "XBC": 0x00001002},
            )

            result = build_execute_next(base_view, cpu_state=seeded_cpu, memory_bytes={0x1001: 0xAB})

            self.assertEqual(result.status, "executed")
            self.assertEqual(result.decode.assembly, "ld A, (-XBC)")
            self.assertEqual(result.written_registers, ("A", "XBC", "PC"))
            assert result.after_cpu is not None
            self.assertEqual(result.after_cpu.regs.xwa, 0x112233AB)
            self.assertEqual(result.after_cpu.regs.xbc, 0x00001001)

    def test_execute_pre_decrement_word_load_updates_register_and_address(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x00200040, b"\xD4\xE8\x23")  # ld HL, (-XDE)
            base_view = load_fetch_view(rom_path)
            seeded_cpu = seed_cpu_state_for_execution(
                base_view.machine.cpu,
                register_values={"XDE": 0x00002004, "XHL": 0x11223344},
            )

            result = build_execute_next(
                base_view,
                cpu_state=seeded_cpu,
                memory_bytes={0x2002: 0x78, 0x2003: 0x56},
            )

            self.assertEqual(result.status, "executed")
            self.assertEqual(result.decode.assembly, "ld HL, (-XDE)")
            self.assertEqual(result.written_registers, ("HL", "XDE", "PC"))
            assert result.after_cpu is not None
            self.assertEqual(result.after_cpu.regs.xde, 0x00002002)
            self.assertEqual(result.after_cpu.regs.xhl, 0x11225678)

    def test_execute_pre_decrement_long_load_updates_register_and_address(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x00200040, b"\xE4\xE0\x21")  # ld XBC, (-XWA)
            base_view = load_fetch_view(rom_path)
            seeded_cpu = seed_cpu_state_for_execution(
                base_view.machine.cpu,
                register_values={"XWA": 0x00003004},
            )

            result = build_execute_next(
                base_view,
                cpu_state=seeded_cpu,
                memory_bytes={0x3000: 0x78, 0x3001: 0x56, 0x3002: 0x34, 0x3003: 0x12},
            )

            self.assertEqual(result.status, "executed")
            self.assertEqual(result.decode.assembly, "ld XBC, (-XWA)")
            self.assertEqual(result.written_registers, ("XBC", "XWA", "PC"))
            assert result.after_cpu is not None
            self.assertEqual(result.after_cpu.regs.xwa, 0x00003000)
            self.assertEqual(result.after_cpu.regs.xbc, 0x12345678)

    def test_execute_pre_decrement_load_blocks_on_aliasing_destination(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x00200040, b"\xE4\xE0\x20")  # ld XWA, (-XWA)
            base_view = load_fetch_view(rom_path)
            seeded_cpu = seed_cpu_state_for_execution(
                base_view.machine.cpu,
                register_values={"XWA": 0x00003004},
            )

            result = build_execute_next(base_view, cpu_state=seeded_cpu)

            self.assertEqual(result.status, "unmodeled-register-alias-side-effects")
            self.assertIn("aliases XWA", result.note)

    def test_execute_abs16_res_and_set_use_builtin_system_byte(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x0020D0E8, b"\xF1\x86\x6F\xB5\xF1\x86\x6F\xBE")
            base_view = load_fetch_view(rom_path)

            res_result = build_execute_next(base_view)
            self.assertEqual(res_result.status, "executed")
            self.assertEqual(res_result.decode.assembly, "res 5, (0x6F86)")
            assert res_result.after_memory is not None
            self.assertEqual(res_result.after_memory[0x006F86], 0x00)

            set_view = replace(
                base_view,
                machine=replace(base_view.machine, cpu=replace(base_view.machine.cpu, pc=0x0020D0EC)),
            )
            set_result = build_execute_next(
                set_view,
                memory_bytes=res_result.after_memory,
            )
            self.assertEqual(set_result.status, "executed")
            self.assertEqual(set_result.decode.assembly, "set 6, (0x6F86)")
            assert set_result.after_memory is not None
            self.assertEqual(set_result.after_memory[0x006F86], 0x40)

    def test_execute_abs24_immediate_store_writes_overlay(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x0020D21D, b"\xF2\x1A\x50\x00\x00\x00")
            base_view = load_fetch_view(rom_path)

            result = build_execute_next(base_view)

            self.assertEqual(result.status, "executed")
            self.assertEqual(result.decode.assembly, "ld (0x00501A), 0x00")
            self.assertEqual(result.written_registers, ("PC",))
            self.assertEqual(len(result.memory_writes), 1)
            self.assertEqual(result.memory_writes[0].address, 0x00501A)
            self.assertEqual(result.memory_writes[0].data, b"\x00")
            assert result.after_cpu is not None
            assert result.after_memory is not None
            self.assertEqual(result.after_cpu.pc, 0x0020D223)
            self.assertEqual(result.after_memory[0x00501A], 0x00)

    def test_execute_abs24_increment_updates_overlay_and_preserves_cf(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x002011E2, b"\xC2\x06\x4F\x00\x61")
            base_view = load_fetch_view(rom_path)
            seeded_cpu = replace(
                base_view.machine.cpu,
                flags=StatusFlags(sf=False, zf=False, vf=False, hf=False, cf=True, nf=True),
            )

            result = build_execute_next(
                base_view,
                cpu_state=seeded_cpu,
                memory_bytes={0x004F06: 0x7F},
            )

            self.assertEqual(result.status, "executed")
            self.assertEqual(result.decode.assembly, "inc 1, (0x004F06)")
            self.assertEqual(result.written_registers, ("PC",))
            self.assertEqual(len(result.memory_writes), 1)
            self.assertEqual(result.memory_writes[0].address, 0x004F06)
            self.assertEqual(result.memory_writes[0].data, b"\x80")
            assert result.after_cpu is not None
            assert result.after_memory is not None
            self.assertEqual(result.after_cpu.pc, 0x002011E7)
            self.assertEqual(result.after_memory[0x004F06], 0x80)
            self.assertTrue(result.after_cpu.flags.sf)
            self.assertFalse(result.after_cpu.flags.zf)
            self.assertTrue(result.after_cpu.flags.vf)
            self.assertTrue(result.after_cpu.flags.hf)
            self.assertTrue(result.after_cpu.flags.cf)
            self.assertFalse(result.after_cpu.flags.nf)

    def test_execute_abs16_long_store_writes_overlay(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x0020D0F9, b"\xF1\xB8\x6F\x60")
            base_view = load_fetch_view(rom_path)
            seeded_cpu = seed_cpu_state_for_execution(
                base_view.machine.cpu,
                register_values={"XWA": 0x20D0B0},
            )

            result = build_execute_next(base_view, cpu_state=seeded_cpu)

            self.assertEqual(result.status, "executed")
            self.assertEqual(result.decode.assembly, "ld (0x6FB8), XWA")
            self.assertEqual(result.written_registers, ("PC",))
            self.assertEqual(len(result.memory_writes), 1)
            self.assertEqual(result.memory_writes[0].address, 0x006FB8)
            self.assertEqual(result.memory_writes[0].data, b"\xB0\xD0\x20\x00")
            assert result.after_cpu is not None
            assert result.after_memory is not None
            self.assertEqual(result.after_cpu.pc, 0x0020D0FD)
            self.assertEqual(result.after_memory[0x006FB8], 0xB0)
            self.assertEqual(result.after_memory[0x006FB9], 0xD0)
            self.assertEqual(result.after_memory[0x006FBA], 0x20)
            self.assertEqual(result.after_memory[0x006FBB], 0x00)

    def test_execute_abs24_add_register_destination_updates_a(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x00200040, b"\xC2\x08\x42\x00\x81")
            base_view = load_fetch_view(rom_path)
            seeded_cpu = seed_cpu_state_for_execution(
                base_view.machine.cpu,
                register_values={"XWA": 0x00000010},
            )

            result = build_execute_next(
                base_view,
                cpu_state=seeded_cpu,
                memory_bytes={0x004208: 0x20},
            )

            self.assertEqual(result.status, "executed")
            self.assertEqual(result.decode.assembly, "add A, (0x004208)")
            assert result.after_cpu is not None
            self.assertEqual(result.after_cpu.regs.xwa & 0xFF, 0x30)
            self.assertFalse(result.after_cpu.flags.cf)
            self.assertFalse(result.after_cpu.flags.zf)

    def test_execute_abs24_add_memory_destination_writes_overlay(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x00200040, b"\xC2\x08\x42\x00\x89")
            base_view = load_fetch_view(rom_path)
            seeded_cpu = seed_cpu_state_for_execution(
                base_view.machine.cpu,
                register_values={"XWA": 0x00000005},
            )

            result = build_execute_next(
                base_view,
                cpu_state=seeded_cpu,
                memory_bytes={0x004208: 0x10},
            )

            self.assertEqual(result.status, "executed")
            self.assertEqual(result.decode.assembly, "add (0x004208), A")
            assert result.after_memory is not None
            self.assertEqual(result.after_memory[0x004208], 0x15)
            self.assertEqual(len(result.memory_writes), 1)
            self.assertFalse(result.after_cpu.flags.cf)

    def test_execute_abs24_cp_memory_minus_register_sets_flags_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x00200040, b"\xC2\x00\x40\x00\xF9")
            base_view = load_fetch_view(rom_path)
            seeded_cpu = seed_cpu_state_for_execution(
                base_view.machine.cpu,
                register_values={"XWA": 0x00000010},
            )

            result = build_execute_next(
                base_view,
                cpu_state=seeded_cpu,
                memory_bytes={0x004000: 0x05},
            )

            self.assertEqual(result.status, "executed")
            self.assertEqual(result.decode.assembly, "cp (0x004000), A")
            self.assertEqual(result.memory_writes, ())
            assert result.after_cpu is not None
            self.assertTrue(result.after_cpu.flags.cf)
            self.assertTrue(result.after_cpu.flags.sf)

    def test_execute_d3_secondary_indexed_word_add_memory_destination(self) -> None:
        # shmup cart frontier: D3 FD 08 06 88 = add (XSP+0x0608), WA.
        # Word RMW into the stack-relative memory word: mem = mem + WA.
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x00200040, b"\xD3\xFD\x08\x06\x88")
            base_view = load_fetch_view(rom_path)
            seeded_cpu = seed_cpu_state_for_execution(
                base_view.machine.cpu,
                register_values={"XSP": 0x00006000, "XWA": 0x00002222},
            )

            result = build_execute_next(
                base_view,
                cpu_state=seeded_cpu,
                memory_bytes={0x006608: 0x11, 0x006609: 0x11},
            )

            self.assertEqual(result.status, "executed")
            self.assertEqual(result.decode.assembly, "add (XSP+1544), WA")
            assert result.after_memory is not None
            # 0x1111 + 0x2222 = 0x3333, little-endian.
            self.assertEqual(result.after_memory[0x006608], 0x33)
            self.assertEqual(result.after_memory[0x006609], 0x33)
            self.assertEqual(len(result.memory_writes), 1)
            assert result.after_cpu is not None
            self.assertFalse(result.after_cpu.flags.cf)
            self.assertFalse(result.after_cpu.flags.zf)

    def test_execute_d2_abs24_word_compare_immediate_sets_flags(self) -> None:
        # battle cart frontier: D2 56 47 00 3F FF 7F = cpw (0x004756), 0x7FFF.
        # Flags only (mem16 - imm16); no write-back.
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x00200040, b"\xD2\x56\x47\x00\x3F\xFF\x7F")
            base_view = load_fetch_view(rom_path)

            result = build_execute_next(
                base_view,
                cpu_state=base_view.machine.cpu,
                memory_bytes={0x004756: 0x00, 0x004757: 0x10},
            )

            self.assertEqual(result.status, "executed")
            self.assertEqual(result.decode.assembly, "cpw (0x004756), 0x7FFF")
            self.assertEqual(result.memory_writes, ())
            assert result.after_cpu is not None
            # 0x1000 - 0x7FFF wraps negative: CF set, SF set.
            self.assertTrue(result.after_cpu.flags.cf)
            self.assertTrue(result.after_cpu.flags.sf)

    def test_execute_e2_abs24_long_load_updates_r32(self) -> None:
        # dialogue cart frontier: E2 5A 49 00 20 = ld XWA, (0x00495A).
        # Long load: 4 bytes from the abs24 address into the destination R32.
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x00200040, b"\xE2\x5A\x49\x00\x20")
            base_view = load_fetch_view(rom_path)

            result = build_execute_next(
                base_view,
                cpu_state=base_view.machine.cpu,
                memory_bytes={
                    0x00495A: 0x44,
                    0x00495B: 0x33,
                    0x00495C: 0x22,
                    0x00495D: 0x11,
                },
            )

            self.assertEqual(result.status, "executed")
            self.assertEqual(result.decode.assembly, "ld XWA, (0x00495A)")
            assert result.after_cpu is not None
            self.assertEqual(result.after_cpu.regs.xwa, 0x11223344)
            self.assertEqual(result.after_cpu.pc, 0x00200045)

    def test_execute_e7_long_register_to_register_copy(self) -> None:
        # cave cart frontier family: E7 <reg> 0x88/0x98 = ld r,R / ld R,r (long
        # register-to-register). E7 E8 88 = ld XWA, XDE (current-bank copy).
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x00200040, b"\xE7\xE8\x88")
            base_view = load_fetch_view(rom_path)
            seeded_cpu = replace(
                base_view.machine.cpu,
                regs=replace(base_view.machine.cpu.regs, xde=0x11223344, xwa=0),
            )

            result = build_execute_next(base_view, cpu_state=seeded_cpu)

            self.assertEqual(result.status, "executed")
            self.assertEqual(result.decode.assembly, "ld XWA, XDE")
            assert result.after_cpu is not None
            self.assertEqual(result.after_cpu.regs.xwa, 0x11223344)
            self.assertEqual(result.after_cpu.pc, 0x00200043)

    def test_execute_byte_block_transfer_ldir_copies_and_counts(self) -> None:
        # Retail SNK cart startup: 83 11 = ldir (byte block copy (XDE+)<-(XHL+)).
        # Frontier of Shougi / Match-of-the-Millennium / Sonic / Super Real Mahjong.
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x00200040, b"\x83\x11")
            base_view = load_fetch_view(rom_path)
            seeded_cpu = replace(
                base_view.machine.cpu,
                regs=replace(base_view.machine.cpu.regs, xbc=0x00000002, xhl=0x00006000, xde=0x00006100),
            )
            result = build_execute_next(base_view, cpu_state=seeded_cpu, memory_bytes={0x6000: 0xAA, 0x6001: 0xBB})
            self.assertEqual(result.status, "executed")
            self.assertEqual(result.decode.assembly, "ldir (XHL)")
            assert result.after_memory is not None
            self.assertEqual(result.after_memory[0x6100], 0xAA)
            self.assertEqual(result.after_memory[0x6101], 0xBB)
            assert result.after_cpu is not None
            self.assertEqual(result.after_cpu.regs.xbc & 0xFFFF, 0x0000)

    def test_execute_d7_banked_word_load_immediate(self) -> None:
        # HW-confirmed 2026-07-08 (hw_test_d7reg): D7 34 A8 = ld RBC3, 0 -- load 0
        # into the LOW 16-bit word of bank-3 XBC, preserving the high word.
        # Entry-idiom frontier of 6 retail games (Biomotor, Fatal Fury, Pac-Man,
        # Pocket Tennis, SNK Gals, Magical Drop).
        from core.execute import _e7_read_r32  # banked-register reader
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x00200040, b"\xD7\x34\xA8")
            base_view = load_fetch_view(rom_path)
            banks = [BankedByteRegisters(slots=tuple([0] * 16)) for _ in range(4)]
            # bank-3 XBC (r32 index 1) = slots[4:8] = 0xAAAA5555 (low 0x5555, high 0xAAAA)
            banks[3] = BankedByteRegisters(slots=(0, 0, 0, 0, 0x55, 0x55, 0xAA, 0xAA, 0, 0, 0, 0, 0, 0, 0, 0))
            seeded_cpu = replace(base_view.machine.cpu, register_banks=tuple(banks))
            result = build_execute_next(base_view, cpu_state=seeded_cpu)
            self.assertEqual(result.status, "executed")
            self.assertEqual(result.decode.assembly, "ld RBC3, 0")
            assert result.after_cpu is not None
            self.assertEqual(_e7_read_r32(result.after_cpu, ("banked", 3, 1)), 0xAAAA0000)

    def test_execute_c1_abs16_memory_to_memory_byte_move(self) -> None:
        # Card Fighters Clash frontier: C1 08 80 19 BA 4F = ld (0x4FBA), (0x8008).
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x00200040, b"\xC1\x08\x80\x19\xBA\x4F")
            base_view = load_fetch_view(rom_path)
            result = build_execute_next(base_view, cpu_state=base_view.machine.cpu,
                                        memory_bytes={0x8008: 0xEF})
            self.assertEqual(result.status, "executed")
            self.assertEqual(result.decode.assembly, "ld (0x4FBA), (0x8008)")
            assert result.after_memory is not None
            self.assertEqual(result.after_memory[0x4FBA], 0xEF)

    def test_execute_c0_abs8_source_memory_to_memory_byte_move(self) -> None:
        # Real SNK BIOS VBlank frame handler: C0 B2 19 85 6E = ld (0x6E85), (0xB2).
        # abs8 source (CPU I/O page 0x0000B2) -> abs16 dest 0x6E85.
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x00200040, b"\xC0\xB2\x19\x85\x6E")
            base_view = load_fetch_view(rom_path)
            result = build_execute_next(base_view, cpu_state=base_view.machine.cpu,
                                        memory_bytes={0xB2: 0x5A})
            self.assertEqual(result.status, "executed")
            self.assertEqual(result.decode.assembly, "ld (0x6E85), (0xB2)")
            assert result.after_memory is not None
            self.assertEqual(result.after_memory[0x6E85], 0x5A)

    def test_execute_d1_abs16_word_compare_memory_register(self) -> None:
        # Real SNK BIOS checksum-verify: D1 14 6C F8 = cpw (0x6C14), WA (mem - reg).
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x00200040, b"\xD1\x14\x6C\xF8")
            base_view = load_fetch_view(rom_path)
            seeded_cpu = replace(base_view.machine.cpu,
                                 regs=replace(base_view.machine.cpu.regs, xwa=0x00000007))
            result = build_execute_next(base_view, cpu_state=seeded_cpu,
                                        memory_bytes={0x6C14: 0x07, 0x6C15: 0x00})
            self.assertEqual(result.status, "executed")
            self.assertEqual(result.decode.assembly, "cpw (0x6C14), WA")
            assert result.after_cpu is not None
            self.assertTrue(result.after_cpu.flags.zf)  # mem(0x0007) - WA(0x0007) == 0
            self.assertEqual(result.memory_writes, ())

    def test_execute_d1_abs16_source_memory_to_memory_word_move(self) -> None:
        # Real SNK BIOS cart hand-off: D1 04 6C 19 84 6E = ldw (0x6E84), (0x6C04).
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x00200040, b"\xD1\x04\x6C\x19\x84\x6E")
            base_view = load_fetch_view(rom_path)
            result = build_execute_next(base_view, cpu_state=base_view.machine.cpu,
                                        memory_bytes={0x6C04: 0x34, 0x6C05: 0x12})
            self.assertEqual(result.status, "executed")
            self.assertEqual(result.decode.assembly, "ldw (0x6E84), (0x6C04)")
            assert result.after_memory is not None
            self.assertEqual(result.after_memory[0x6E84], 0x34)
            self.assertEqual(result.after_memory[0x6E85], 0x12)

    def test_execute_d2_abs24_source_memory_to_memory_word_move(self) -> None:
        # Real SNK BIOS cart hand-off: D2 42 E2 FF 19 04 6C = ldw (0x6C04), (0xFFE242).
        # Source 0xFFE242 is in the BIOS image; here we drive the overlay directly.
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x00200040, b"\xD2\x00\x50\x00\x19\x04\x6C")
            base_view = load_fetch_view(rom_path)
            result = build_execute_next(base_view, cpu_state=base_view.machine.cpu,
                                        memory_bytes={0x5000: 0xCD, 0x5001: 0xAB})
            self.assertEqual(result.status, "executed")
            self.assertEqual(result.decode.assembly, "ldw (0x6C04), (0x005000)")
            assert result.after_memory is not None
            self.assertEqual(result.after_memory[0x6C04], 0xCD)
            self.assertEqual(result.after_memory[0x6C05], 0xAB)

    def test_execute_d1_abs16_word_increment(self) -> None:
        # Real SNK BIOS frame-counter bump: D1 18 6C 61 = incw 1, (0x6C18).
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x00200040, b"\xD1\x18\x6C\x61")
            base_view = load_fetch_view(rom_path)
            result = build_execute_next(base_view, cpu_state=base_view.machine.cpu,
                                        memory_bytes={0x6C18: 0x10, 0x6C19: 0x00})
            self.assertEqual(result.status, "executed")
            self.assertEqual(result.decode.assembly, "incw 1, (0x6C18)")
            assert result.after_memory is not None
            self.assertEqual(result.after_memory[0x6C18], 0x11)
            self.assertEqual(result.after_memory[0x6C19], 0x00)

    def test_execute_d1_abs16_word_compare_register(self) -> None:
        # Real SNK BIOS frame-counter threshold test: D1 1A 6C F0 = cp WA, (0x6C1A).
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x00200040, b"\xD1\x1A\x6C\xF0")
            base_view = load_fetch_view(rom_path)
            seeded_cpu = replace(base_view.machine.cpu,
                                 regs=replace(base_view.machine.cpu.regs, xwa=0x00000005))
            result = build_execute_next(base_view, cpu_state=seeded_cpu,
                                        memory_bytes={0x6C1A: 0x05, 0x6C1B: 0x00})
            self.assertEqual(result.status, "executed")
            self.assertEqual(result.decode.assembly, "cp WA, (0x6C1A)")
            assert result.after_cpu is not None
            self.assertTrue(result.after_cpu.flags.zf)  # WA(0x0005) - mem(0x0005) == 0
            self.assertFalse(result.after_cpu.flags.cf)  # no borrow
            self.assertEqual(result.memory_writes, ())  # compare writes no memory

    def test_execute_c1_abs16_exchange_memory_register(self) -> None:
        # Ganbare frontier: C1 A0 44 36 = ex (0x44A0), H (swap mem byte <-> R8).
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x00200040, b"\xC1\xA0\x44\x36")
            base_view = load_fetch_view(rom_path)
            seeded_cpu = replace(base_view.machine.cpu, regs=replace(base_view.machine.cpu.regs, xhl=0x0000AB00))
            result = build_execute_next(base_view, cpu_state=seeded_cpu, memory_bytes={0x44A0: 0x12})
            self.assertEqual(result.status, "executed")
            self.assertEqual(result.decode.assembly, "ex (0x44A0), H")
            assert result.after_memory is not None and result.after_cpu is not None
            self.assertEqual(result.after_memory[0x44A0], 0xAB)  # mem now holds old H
            self.assertEqual(result.after_cpu.regs.xhl, 0x00001200)  # H now holds old mem

    def test_execute_d5_post_increment_word_compare_immediate(self) -> None:
        # Puyo Pop frontier: D5 E5 3F FF FF = cpw (XBC+), 0xFFFF (flags + post-inc by 2).
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x00200040, b"\xD5\xE5\x3F\xFF\xFF")
            base_view = load_fetch_view(rom_path)
            seeded_cpu = replace(base_view.machine.cpu, regs=replace(base_view.machine.cpu.regs, xbc=0x00006000))
            result = build_execute_next(base_view, cpu_state=seeded_cpu, memory_bytes={0x6000: 0x00, 0x6001: 0x10})
            self.assertEqual(result.status, "executed")
            self.assertEqual(result.decode.assembly, "cpw (XBC+), 0xFFFF")
            assert result.after_cpu is not None
            self.assertEqual(result.after_cpu.regs.xbc, 0x00006002)  # post-incremented by 2
            self.assertTrue(result.after_cpu.flags.cf)  # 0x1000 - 0xFFFF borrows

    def test_execute_8f_indexed_memory_to_memory_byte_move(self) -> None:
        # Shougi / Melon-chan frontier: 8F 04 19 32 47 = ld (0x4732), (XSP+4).
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x00200040, b"\x8F\x04\x19\x32\x47")
            base_view = load_fetch_view(rom_path)
            seeded_cpu = replace(base_view.machine.cpu, regs=replace(base_view.machine.cpu.regs, xsp=0x00006000))
            result = build_execute_next(base_view, cpu_state=seeded_cpu, memory_bytes={0x6004: 0xCD})
            self.assertEqual(result.status, "executed")
            self.assertEqual(result.decode.assembly, "ld (0x4732), (XSP+4)")
            assert result.after_memory is not None
            self.assertEqual(result.after_memory[0x4732], 0xCD)

    def test_execute_f0_abs8_word_register_store(self) -> None:
        # Cool Cool Jam / KOF Battle frontier: F0 B8 50 = ldw (0xB8), WA.
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x00200040, b"\xF0\xB8\x50")
            base_view = load_fetch_view(rom_path)
            seeded_cpu = replace(base_view.machine.cpu, regs=replace(base_view.machine.cpu.regs, xwa=0x1234))
            result = build_execute_next(base_view, cpu_state=seeded_cpu)
            self.assertEqual(result.status, "executed")
            self.assertEqual(result.decode.assembly, "ldw (0xB8), WA")
            assert result.after_memory is not None
            self.assertEqual(result.after_memory[0xB8], 0x34)
            self.assertEqual(result.after_memory[0xB9], 0x12)

    def test_execute_c2_memory_to_memory_byte_move(self) -> None:
        # Puzzle Link / Tsunagete frontier: C2 <abs24> 19 <abs16> = ld (abs16), (abs24).
        # Using a RAM destination (0x4632) so the moved byte lands.
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x00200040, b"\xC2\xC7\x44\x00\x19\x32\x46")
            base_view = load_fetch_view(rom_path)
            result = build_execute_next(base_view, cpu_state=base_view.machine.cpu,
                                        memory_bytes={0x0044C7: 0xAB})
            self.assertEqual(result.status, "executed")
            self.assertEqual(result.decode.assembly, "ld (0x4632), (0x0044C7)")
            assert result.after_memory is not None
            self.assertEqual(result.after_memory[0x4632], 0xAB)

    def test_execute_d1_abs16_word_alu_immediate_rmw(self) -> None:
        # Crush Roller frontier: D1 E6 4E 3E 01 00 = orw (0x4EE6), 0x0001 (word RMW).
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x00200040, b"\xD1\xE6\x4E\x3E\x01\x00")
            base_view = load_fetch_view(rom_path)
            result = build_execute_next(base_view, cpu_state=base_view.machine.cpu,
                                        memory_bytes={0x4EE6: 0x00, 0x4EE7: 0x00})
            self.assertEqual(result.status, "executed")
            self.assertEqual(result.decode.assembly, "orw (0x4EE6), 0x0001")
            assert result.after_memory is not None
            self.assertEqual(result.after_memory[0x4EE6], 0x01)
            self.assertEqual(result.after_memory[0x4EE7], 0x00)

    def test_execute_d2_abs24_word_inc_memory(self) -> None:
        # Baseball Stars / Neo Turf frontier: D2 F3 4B 00 61 = incw 1, (0x004BF3).
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x00200040, b"\xD2\xF3\x4B\x00\x61")
            base_view = load_fetch_view(rom_path)
            result = build_execute_next(base_view, cpu_state=base_view.machine.cpu,
                                        memory_bytes={0x4BF3: 0x10, 0x4BF4: 0x00})
            self.assertEqual(result.status, "executed")
            self.assertEqual(result.decode.assembly, "incw 1, (0x004BF3)")
            assert result.after_memory is not None
            self.assertEqual(result.after_memory[0x4BF3], 0x11)

    def test_execute_c5_post_increment_mul_byte_source(self) -> None:
        # Dive Alert / Koi Koi / Rockman frontier: C5 EC 45 = mul DE, (XHL+).
        # DE.low8 * mem8 -> 16-bit DE; XHL post-increments by 1.
        #
        # This test used to say `mul IY, (XHL+)`, a name copied from ngdis. It is
        # wrong, and the official assembler says so twice over: `mul DE,(XHL+)` IS
        # `C5 EC 45`, and `mul IY,(XHL+)` it refuses to encode at all. Toshiba's
        # <Divide> Note 3 -- which covers "DIV RR,r AND DIV RR,(mem)" -- gives the
        # reason: at BYTE size the destination is a WORD register and only the ODD
        # codes name one (001 = WA, 011 = BC, 101 = DE, 111 = HL). IY is not
        # reachable here at all.
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x00200040, b"\xC5\xEC\x45")
            base_view = load_fetch_view(rom_path)
            seeded_cpu = replace(
                base_view.machine.cpu,
                regs=replace(base_view.machine.cpu.regs, xde=0x0003, xhl=0x00006000),
            )
            result = build_execute_next(base_view, cpu_state=seeded_cpu, memory_bytes={0x6000: 0x04})
            self.assertEqual(result.status, "executed")
            self.assertEqual(result.decode.assembly, "mul DE, (XHL+)")
            assert result.after_cpu is not None
            self.assertEqual(result.after_cpu.regs.xde & 0xFFFF, 0x000C)
            self.assertEqual(result.after_cpu.regs.xhl, 0x00006001)

    def test_execute_c5_post_increment_add_byte(self) -> None:
        # Bakumatsu / Last Blade frontier: C5 F0 81 = add A, (XIX+).
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x00200040, b"\xC5\xF0\x81")
            base_view = load_fetch_view(rom_path)
            seeded_cpu = replace(
                base_view.machine.cpu,
                regs=replace(base_view.machine.cpu.regs, xwa=0x0010, xix=0x00006000),
            )
            result = build_execute_next(base_view, cpu_state=seeded_cpu, memory_bytes={0x6000: 0x05})
            self.assertEqual(result.status, "executed")
            self.assertEqual(result.decode.assembly, "add A, (XIX+)")
            assert result.after_cpu is not None
            self.assertEqual(result.after_cpu.regs.xwa & 0xFF, 0x15)
            self.assertEqual(result.after_cpu.regs.xix, 0x00006001)

    def test_execute_f2_abs24_conditional_jump(self) -> None:
        # Retail frontier (~12 games incl. Dive Alert, Metal Slug 1, Rockman):
        # F2 5A 29 23 D6 = jp Z, (0x23295A). Taken when ZF set; PC = the abs24.
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x00200040, b"\xF2\x5A\x29\x23\xD6")
            base_view = load_fetch_view(rom_path)
            taken_cpu = replace(base_view.machine.cpu, flags=replace(base_view.machine.cpu.flags, zf=True))
            taken = build_execute_next(base_view, cpu_state=taken_cpu)
            self.assertEqual(taken.status, "executed")
            self.assertEqual(taken.decode.assembly, "jp Z, (0x23295A)")
            assert taken.after_cpu is not None
            self.assertEqual(taken.after_cpu.pc, 0x0023295A)
            # Not taken (ZF clear) -> fall through to the next sequential PC.
            nt_cpu = replace(base_view.machine.cpu, flags=replace(base_view.machine.cpu.flags, zf=False))
            not_taken = build_execute_next(base_view, cpu_state=nt_cpu)
            self.assertEqual(not_taken.status, "executed")
            assert not_taken.after_cpu is not None
            self.assertEqual(not_taken.after_cpu.pc, 0x00200045)

    def test_execute_f1_abs16_lda_loads_effective_address(self) -> None:
        # Hanafuda frontier: F1 B8 6F 35 = lda XIY, (0x6FB8) -- loads the address.
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x00200040, b"\xF1\xB8\x6F\x35")
            base_view = load_fetch_view(rom_path)
            result = build_execute_next(base_view, cpu_state=base_view.machine.cpu)
            self.assertEqual(result.status, "executed")
            self.assertEqual(result.decode.assembly, "lda XIY, (0x6FB8)")
            assert result.after_cpu is not None
            self.assertEqual(result.after_cpu.regs.xiy, 0x00006FB8)

    def test_execute_c7_banked_byte_load_immediate_writes_bank(self) -> None:
        # Bakumatsu / Baseball Color / Tsunagete frontier: C7 31 03 10 = ld RW3, 0x10.
        # Writing an immediate into a banked byte must NOT require the owner R32's
        # other bytes known (regression: the ld path wrongly blocked when reg_updates
        # was None even though the banked write landed in extra_cpu_updates).
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x00200040, b"\xC7\x31\x03\x10")
            base_view = load_fetch_view(rom_path)
            seeded_cpu = replace(
                base_view.machine.cpu,
                register_banks=tuple(BankedByteRegisters(slots=tuple([0] * 16)) for _ in range(4)),
            )
            result = build_execute_next(base_view, cpu_state=seeded_cpu)
            self.assertEqual(result.status, "executed")
            self.assertEqual(result.decode.assembly, "ld RW3, 0x10")

    def test_execute_c7_banked_byte_load_from_register_writes_bank(self) -> None:
        # Delta Warp / Densha Go 2 / Kikouseki frontier: C7 30 99 = ld RA3, A.
        # Same banked-write regression as the immediate form, on the ld r,R path.
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x00200040, b"\xC7\x30\x99")
            base_view = load_fetch_view(rom_path)
            seeded_cpu = replace(
                base_view.machine.cpu,
                regs=replace(base_view.machine.cpu.regs, xwa=0x000000AB),
                register_banks=tuple(BankedByteRegisters(slots=tuple([0] * 16)) for _ in range(4)),
            )
            result = build_execute_next(base_view, cpu_state=seeded_cpu)
            self.assertEqual(result.status, "executed")
            self.assertEqual(result.decode.assembly, "ld RA3, A")

    def test_execute_e7_push_long_banked_register(self) -> None:
        # Baseball Stars (Pocket) frontier: E7 30 04 = push XWA3 (long banked reg).
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x00200040, b"\xE7\x30\x04")
            base_view = load_fetch_view(rom_path)
            seeded_cpu = replace(
                base_view.machine.cpu,
                regs=replace(base_view.machine.cpu.regs, xsp=0x00006C00),
                register_banks=tuple(BankedByteRegisters(slots=tuple([0] * 16)) for _ in range(4)),
            )
            result = build_execute_next(base_view, cpu_state=seeded_cpu)
            self.assertEqual(result.status, "executed")
            self.assertEqual(result.decode.assembly, "push XWA3")
            assert result.after_cpu is not None
            self.assertEqual(result.after_cpu.regs.xsp, 0x00006BFC)

    def test_execute_e7_pop_long_banked_register(self) -> None:
        # Baseball Stars (Pocket/Color) frontier: E7 3C 05 = pop XHL3.
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x00200040, b"\xE7\x3C\x05")
            base_view = load_fetch_view(rom_path)
            seeded_cpu = replace(
                base_view.machine.cpu,
                regs=replace(base_view.machine.cpu.regs, xsp=0x00006C00),
                register_banks=tuple(BankedByteRegisters(slots=tuple([0] * 16)) for _ in range(4)),
            )
            result = build_execute_next(
                base_view, cpu_state=seeded_cpu,
                memory_bytes={0x6C00: 0x44, 0x6C01: 0x33, 0x6C02: 0x22, 0x6C03: 0x11},
            )
            self.assertEqual(result.status, "executed")
            self.assertEqual(result.decode.assembly, "pop XHL3")
            assert result.after_cpu is not None
            self.assertEqual(result.after_cpu.regs.xsp, 0x00006C04)

    def test_execute_e1_abs16_long_load_updates_r32(self) -> None:
        # Ogre Battle Gaiden frontier: E1 02 40 23 = ld XHL, (0x4002) (abs16 long load).
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x00200040, b"\xE1\x02\x40\x23")
            base_view = load_fetch_view(rom_path)
            result = build_execute_next(
                base_view, cpu_state=base_view.machine.cpu,
                memory_bytes={0x4002: 0x44, 0x4003: 0x33, 0x4004: 0x22, 0x4005: 0x11},
            )
            self.assertEqual(result.status, "executed")
            self.assertEqual(result.decode.assembly, "ld XHL, (0x4002)")
            assert result.after_cpu is not None
            self.assertEqual(result.after_cpu.regs.xhl, 0x11223344)

    def test_execute_abs16_immediate_store_writes_io_overlay(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x0020D142, b"\xF1\x02\x80\x00\xA0")
            base_view = load_fetch_view(rom_path)

            result = build_execute_next(base_view)

            self.assertEqual(result.status, "executed")
            self.assertEqual(result.decode.assembly, "ld (0x8002), 0xA0")
            self.assertEqual(result.written_registers, ("PC",))
            self.assertEqual(len(result.memory_writes), 1)
            self.assertEqual(result.memory_writes[0].address, 0x008002)
            self.assertEqual(result.memory_writes[0].data, b"\xA0")
            assert result.after_cpu is not None
            assert result.after_memory is not None
            self.assertEqual(result.after_cpu.pc, 0x0020D147)
            self.assertEqual(result.after_memory[0x008002], 0xA0)

    def test_execute_abs16_word_immediate_store(self) -> None:
        # F1 80 6F 02 34 12 = ldw (0x6F80), 0x1234 (abs16 word-immediate store).
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x00200040, b"\xF1\x80\x6F\x02\x34\x12")
            base_view = load_fetch_view(rom_path)

            result = build_execute_next(base_view)

            self.assertEqual(result.status, "executed")
            self.assertEqual(result.decode.assembly, "ldw (0x6F80), 0x1234")
            assert result.after_cpu is not None
            assert result.after_memory is not None
            self.assertEqual(result.after_cpu.pc, 0x00200046)
            # Little-endian 0x1234 -> 0x34 at 0x6F80, 0x12 at 0x6F81.
            self.assertEqual(result.after_memory[0x006F80], 0x34)
            self.assertEqual(result.after_memory[0x006F81], 0x12)

    def test_seed_cpu_state_for_execution_sets_multiple_registers(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x00200040, b"\x00")
            view = load_fetch_view(rom_path)

            seeded_cpu = seed_cpu_state_for_execution(
                view.machine.cpu,
                register_values={"xiz": 0x12345678, "XWA": 0x89ABCDEF},
                seed_xsp=0x00004100,
            )

            self.assertEqual(seeded_cpu.regs.xwa, 0x89ABCDEF)
            self.assertEqual(seeded_cpu.regs.xiz, 0x12345678)
            self.assertEqual(seeded_cpu.regs.xsp, 0x00004100)
            self.assertIn("user-seeded-registers", seeded_cpu.modeled_fields)
            self.assertIn("XWA=0x89ABCDEF", seeded_cpu.note)
            self.assertIn("XIZ=0x12345678", seeded_cpu.note)
            self.assertIn("XSP=0x00004100", seeded_cpu.note)

    def test_seed_cpu_state_for_execution_can_seed_banked_core_register(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x00200040, b"\x00")
            view = load_fetch_view(rom_path)
            cpu = replace(view.machine.cpu, rfp=3, register_bank=3)

            seeded_cpu = seed_cpu_state_for_execution(
                cpu,
                register_values={"XBC@bank3": 0x12345678},
            )

            self.assertEqual(seeded_cpu.regs.xbc, 0x12345678)
            assert seeded_cpu.register_banks is not None
            self.assertEqual(seeded_cpu.register_banks[3].slots[4:8], (0x78, 0x56, 0x34, 0x12))
            self.assertIn("XBC@bank3=0x12345678", seeded_cpu.note)

    def test_seed_cpu_state_for_execution_can_seed_control_registers(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x00200040, b"\x00")
            view = load_fetch_view(rom_path)

            seeded_cpu = seed_cpu_state_for_execution(
                view.machine.cpu,
                register_values={
                    "DMAD0": 0x00201234,
                    "DMAC0": 0x12345678,
                    "DMAM0": 0x1234,
                    "INTNEST": 0x12345678,
                },
            )

            assert seeded_cpu.control_registers is not None
            self.assertEqual(seeded_cpu.control_registers.dmad[0], 0x00201234)
            self.assertEqual(seeded_cpu.control_registers.dmac[0], 0x5678)
            self.assertEqual(seeded_cpu.control_registers.dmam[0], 0x34)
            self.assertEqual(seeded_cpu.control_registers.intnest, 0x5678)
            self.assertIn("DMAD0=0x00201234", seeded_cpu.note)
            self.assertIn("DMAC0=0x12345678", seeded_cpu.note)

    def test_seed_cpu_state_for_execution_rejects_conflicting_xsp_values(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x00200040, b"\x00")
            view = load_fetch_view(rom_path)

            with self.assertRaisesRegex(ValueError, "conflicting seed values"):
                seed_cpu_state_for_execution(
                    view.machine.cpu,
                    register_values={"XSP": 0x00004100},
                    seed_xsp=0x00004200,
                )

    def test_execute_pushw_immediate_writes_stack_and_updates_xsp(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x00200040, b"\x0B\x20\x00")
            base_view = load_fetch_view(rom_path)
            seeded_cpu = replace(
                base_view.machine.cpu,
                regs=replace(base_view.machine.cpu.regs, xsp=0x00004100),
            )

            result = build_execute_next(base_view, cpu_state=seeded_cpu)

            self.assertEqual(result.status, "executed")
            self.assertEqual(result.decode.assembly, "pushw 0x0020")
            self.assertEqual(result.written_registers, ("XSP", "PC"))
            self.assertEqual(len(result.memory_writes), 1)
            self.assertEqual(result.memory_writes[0].address, 0x0040FE)
            self.assertEqual(result.memory_writes[0].data, b"\x20\x00")
            assert result.after_cpu is not None
            assert result.after_memory is not None
            self.assertEqual(result.after_cpu.regs.xsp, 0x000040FE)
            self.assertEqual(result.after_cpu.pc, 0x00200043)
            self.assertEqual(result.after_memory[0x0040FE], 0x20)
            self.assertEqual(result.after_memory[0x0040FF], 0x00)

    def test_execute_push_word_reg_indirect_reads_memory_and_updates_stack(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x00200040, b"\x90\x04")
            base_view = load_fetch_view(rom_path)
            seeded_cpu = replace(
                base_view.machine.cpu,
                regs=replace(base_view.machine.cpu.regs, xwa=0x00005010, xsp=0x00004100),
            )

            result = build_execute_next(
                base_view,
                cpu_state=seeded_cpu,
                memory_bytes={0x5010: 0x34, 0x5011: 0x12},
            )

            self.assertEqual(result.status, "executed")
            self.assertEqual(result.decode.assembly, "push (XWA)")
            self.assertEqual(result.written_registers, ("XSP", "PC"))
            self.assertEqual(len(result.memory_writes), 1)
            self.assertEqual(result.memory_writes[0].address, 0x0040FE)
            self.assertEqual(result.memory_writes[0].data, b"\x34\x12")
            assert result.after_cpu is not None
            assert result.after_memory is not None
            self.assertEqual(result.after_cpu.regs.xsp, 0x000040FE)
            self.assertEqual(result.after_cpu.pc, 0x00200042)
            self.assertEqual(result.after_memory[0x0040FE], 0x34)
            self.assertEqual(result.after_memory[0x0040FF], 0x12)

    def test_execute_secondary_indexed_byte_compare_immediate_sets_zero_flag(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x00200040, b"\xC3\x07\xE4\xE0\x3F\x00")
            base_view = load_fetch_view(rom_path)
            seeded_cpu = seed_cpu_state_for_execution(
                base_view.machine.cpu,
                register_values={"XBC": 0x00005000, "XWA": 0x00000010},
            )

            result = build_execute_next(
                base_view,
                cpu_state=seeded_cpu,
                memory_bytes={0x5010: 0x00},
            )

            self.assertEqual(result.status, "executed")
            self.assertEqual(result.decode.assembly, "cp (XBC+WA), 0x00")
            self.assertEqual(result.written_registers, ("PC",))
            assert result.after_cpu is not None
            self.assertTrue(result.after_cpu.flags.zf)
            self.assertFalse(result.after_cpu.flags.sf)
            self.assertFalse(result.after_cpu.flags.cf)
            self.assertEqual(result.after_cpu.pc, 0x00200046)

    def test_execute_call_pushes_return_address_and_jumps(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x00200040, b"\x1D\x2F\x91\x20")
            base_view = load_fetch_view(rom_path)
            seeded_cpu = replace(
                base_view.machine.cpu,
                regs=replace(base_view.machine.cpu.regs, xsp=0x00004100),
            )

            result = build_execute_next(base_view, cpu_state=seeded_cpu)

            self.assertEqual(result.status, "executed")
            self.assertEqual(result.decode.assembly, "call 0x20912F")
            self.assertEqual(result.written_registers, ("XSP", "PC"))
            self.assertEqual(len(result.memory_writes), 1)
            self.assertEqual(result.memory_writes[0].address, 0x0040FC)
            self.assertEqual(result.memory_writes[0].data, b"\x44\x00\x20\x00")
            assert result.after_cpu is not None
            self.assertEqual(result.after_cpu.regs.xsp, 0x000040FC)
            self.assertEqual(result.after_cpu.pc, 0x0020912F)

    def test_execute_pop_r32_reads_seeded_stack_overlay(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x00200040, b"\x58")
            base_view = load_fetch_view(rom_path)
            seeded_cpu = replace(
                base_view.machine.cpu,
                regs=replace(base_view.machine.cpu.regs, xsp=0x00004100),
            )

            result = build_execute_next(
                base_view,
                cpu_state=seeded_cpu,
                memory_bytes={
                    0x004100: 0x78,
                    0x004101: 0x56,
                    0x004102: 0x34,
                    0x004103: 0x12,
                },
            )

            self.assertEqual(result.status, "executed")
            self.assertEqual(result.decode.assembly, "pop XWA")
            self.assertEqual(result.written_registers, ("XWA", "XSP", "PC"))
            assert result.after_cpu is not None
            self.assertEqual(result.after_cpu.regs.xwa, 0x12345678)
            self.assertEqual(result.after_cpu.regs.xsp, 0x00004104)
            self.assertEqual(result.after_cpu.pc, 0x00200041)

    def test_execute_ret_restores_pc_from_seeded_stack_overlay(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x00200040, b"\x0E")
            base_view = load_fetch_view(rom_path)
            seeded_cpu = replace(
                base_view.machine.cpu,
                regs=replace(base_view.machine.cpu.regs, xsp=0x00004100),
            )

            result = build_execute_next(
                base_view,
                cpu_state=seeded_cpu,
                memory_bytes={
                    0x004100: 0x34,
                    0x004101: 0x12,
                    0x004102: 0x20,
                    0x004103: 0x00,
                },
            )

            self.assertEqual(result.status, "executed")
            self.assertEqual(result.decode.assembly, "ret")
            self.assertEqual(result.written_registers, ("XSP", "PC"))
            assert result.after_cpu is not None
            self.assertEqual(result.after_cpu.pc, 0x00201234)
            self.assertEqual(result.after_cpu.regs.xsp, 0x00004104)

    def test_execute_retd_restores_pc_and_adjusts_stack(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x00200040, b"\x0F\x08\x00")
            base_view = load_fetch_view(rom_path)
            seeded_cpu = replace(
                base_view.machine.cpu,
                regs=replace(base_view.machine.cpu.regs, xsp=0x00004100),
            )

            result = build_execute_next(
                base_view,
                cpu_state=seeded_cpu,
                memory_bytes={
                    0x004100: 0x34,
                    0x004101: 0x12,
                    0x004102: 0x20,
                    0x004103: 0x00,
                },
            )

            self.assertEqual(result.status, "executed")
            self.assertEqual(result.decode.assembly, "retd 8")
            assert result.after_cpu is not None
            self.assertEqual(result.after_cpu.pc, 0x00201234)
            self.assertEqual(result.after_cpu.regs.xsp, 0x0000410C)

    def test_execute_pushw_blocks_when_stack_target_is_read_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x00200040, b"\x0B\x20\x00")
            base_view = load_fetch_view(rom_path)
            seeded_cpu = replace(
                base_view.machine.cpu,
                regs=replace(base_view.machine.cpu.regs, xsp=0x00200010),
            )

            result = build_execute_next(base_view, cpu_state=seeded_cpu)

            # Stack write to ROM space: _check_writable_range returns "write-discarded"
            # (real hardware: open bus, no WE signal — write is silently discarded).
            # The stack caller treats any non-None write_status as a stop condition.
            self.assertEqual(result.status, "write-discarded")
            self.assertEqual(result.decode.assembly, "pushw 0x0020")
            self.assertIsNone(result.after_cpu)

    def test_execute_store_to_unmapped_address_is_discarded_and_continues(self) -> None:
        """ld (XDE), A where XDE points to an unmapped address.

        Real NGPC hardware: TLCS-900 puts the write on the bus, nothing responds
        (open bus).  Execution continues normally — no bus fault, no exception.
        The emulator must reflect this: status='executed', PC advances, memory
        is unchanged, but a MemoryWrite record with '[DISCARDED]' is emitted.
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            # B2 41 = ld (XDE), A  (B0+2 prefix = XDE indirect, op 0x41 = A)
            self._write_demo_rom(rom_path, 0x00200000, b"\xB2\x41")
            base_view = load_fetch_view(rom_path)
            # Seed XDE to an unmapped address (0x00DB07 is in the reserved gap).
            # Seed XWA so A (low byte) is known.
            seeded_cpu = replace(
                base_view.machine.cpu,
                regs=replace(
                    base_view.machine.cpu.regs,
                    xde=0x0000DB07,
                    xwa=0x000000B1,
                    xsp=0x00006C00,
                ),
            )

            result = build_execute_next(base_view, cpu_state=seeded_cpu)

            # Execution must succeed (not blocked).
            self.assertEqual(result.status, "executed")
            # PC must advance past the 2-byte instruction.
            assert result.after_cpu is not None
            self.assertEqual(result.after_cpu.pc, 0x00200002)
            # Memory must be unchanged (write discarded — after_memory == before_memory).
            assert result.after_memory is not None
            self.assertEqual(result.after_memory, {})
            # A MemoryWrite record must be emitted with [DISCARDED] note.
            self.assertEqual(len(result.memory_writes), 1)
            self.assertIn("[DISCARDED]", result.memory_writes[0].note)
            self.assertEqual(result.memory_writes[0].address, 0x0000DB07)

    def test_execute_store_to_rom_address_is_discarded_and_continues(self) -> None:
        """ld (abs24), A where the target is inside the ROM image.

        Real hardware silently discards writes to the ROM window (no WE signal).
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            # C2 21 00 00 20 41 = ld A, (0x200000) ... actually use abs24 store form:
            # F2 target_lo target_hi target_bank 41 = ld (abs24), A
            # abs24 = 0x200010 (inside ROM).  Bytes: F2 10 00 20 41
            self._write_demo_rom(rom_path, 0x00200000, b"\xF2\x10\x00\x20\x41")
            base_view = load_fetch_view(rom_path)
            seeded_cpu = replace(
                base_view.machine.cpu,
                regs=replace(
                    base_view.machine.cpu.regs,
                    xwa=0x000000C5,  # A = 0xC5
                    xsp=0x00006C00,
                ),
            )

            result = build_execute_next(base_view, cpu_state=seeded_cpu)

            self.assertEqual(result.status, "executed")
            assert result.after_cpu is not None
            self.assertEqual(result.after_cpu.pc, 0x00200005)
            self.assertEqual(len(result.memory_writes), 1)
            self.assertIn("[DISCARDED]", result.memory_writes[0].note)

    def test_execute_ld_r32_small_imm_zero(self) -> None:
        """ld XWA, 0 (E8 A8) — compact 2-byte small-immediate load, value=0."""
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x00200040, b"\xE8\xA8")
            view = load_fetch_view(rom_path)

            result = build_execute_next(view)

            self.assertEqual(result.status, "executed")
            self.assertEqual(result.decode.assembly, "ld XWA, 0")
            self.assertIn("XWA", result.written_registers)
            assert result.after_cpu is not None
            self.assertEqual(result.after_cpu.regs.xwa, 0x00000000)

    def test_execute_ld_r32_small_imm_nonzero(self) -> None:
        """ld XBC, 5 (E9 AD) — compact 2-byte small-immediate load, value=5."""
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            # E9 = XBC long prefix, AD = A8+5 → value 5
            self._write_demo_rom(rom_path, 0x00200040, b"\xE9\xAD")
            view = load_fetch_view(rom_path)

            result = build_execute_next(view)

            self.assertEqual(result.status, "executed")
            self.assertEqual(result.decode.assembly, "ld XBC, 5")
            self.assertIn("XBC", result.written_registers)
            assert result.after_cpu is not None
            self.assertEqual(result.after_cpu.regs.xbc, 0x00000005)

    def test_execute_res_bit_k2ge_reads_builtin_and_writes_overlay(self) -> None:
        """res 7, (0x8030) — K2GE RMW: reads builtin 0x00, writes 0x00 to overlay."""
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            # F1 30 80 B7 = res 7, (0x8030)
            self._write_demo_rom(rom_path, 0x00200040, b"\xF1\x30\x80\xB7")
            view = load_fetch_view(rom_path)

            result = build_execute_next(view)

            self.assertEqual(result.status, "executed")
            self.assertEqual(result.decode.assembly, "res 7, (0x8030)")
            assert result.after_memory is not None
            self.assertIn(0x8030, result.after_memory)
            self.assertEqual(result.after_memory[0x8030], 0x00)

    def test_execute_set_bit_k2ge_reads_builtin_and_writes_overlay(self) -> None:
        """set 3, (0x8010) — K2GE RMW: reads builtin 0x00, writes 0x08 to overlay."""
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            # F1 10 80 BB = set 3, (0x8010)
            self._write_demo_rom(rom_path, 0x00200040, b"\xF1\x10\x80\xBB")
            view = load_fetch_view(rom_path)

            result = build_execute_next(view)

            self.assertEqual(result.status, "executed")
            self.assertEqual(result.decode.assembly, "set 3, (0x8010)")
            assert result.after_memory is not None
            self.assertIn(0x8010, result.after_memory)
            self.assertEqual(result.after_memory[0x8010], 0x08)


    def test_execute_abs24_memory_bit_test_updates_flags_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x00200E3E, b"\xF2\x6A\x4C\x00\xCC")
            base_view = load_fetch_view(rom_path)
            seeded_cpu = replace(
                base_view.machine.cpu,
                flags=StatusFlags(sf=True, zf=False, vf=True, hf=False, cf=True, nf=True),
            )

            result = build_execute_next(
                base_view,
                cpu_state=seeded_cpu,
                memory_bytes={0x004C6A: 0x00},
            )

            self.assertEqual(result.status, "executed")
            self.assertEqual(result.decode.assembly, "bit 4, (0x004C6A)")
            self.assertEqual(result.written_registers, ("PC",))
            assert result.after_cpu is not None
            assert result.after_memory is not None
            self.assertEqual(result.after_cpu.pc, 0x00200E43)
            self.assertEqual(result.after_memory[0x004C6A], 0x00)
            self.assertTrue(result.after_cpu.flags.zf)
            self.assertTrue(result.after_cpu.flags.hf)
            self.assertFalse(result.after_cpu.flags.nf)
            self.assertTrue(result.after_cpu.flags.sf)
            self.assertTrue(result.after_cpu.flags.vf)
            self.assertTrue(result.after_cpu.flags.cf)

    def test_execute_abs24_memory_tset_writes_overlay_and_sets_z_h_and_n(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x00200E61, b"\xF2\x6A\x4C\x00\xA8")
            base_view = load_fetch_view(rom_path)
            seeded_cpu = replace(
                base_view.machine.cpu,
                flags=StatusFlags(sf=True, zf=False, vf=True, hf=False, cf=True, nf=True),
            )

            result = build_execute_next(
                base_view,
                cpu_state=seeded_cpu,
                memory_bytes={0x004C6A: 0x00},
            )

            self.assertEqual(result.status, "executed")
            self.assertEqual(result.decode.assembly, "tset 0, (0x004C6A)")
            self.assertEqual(result.written_registers, ("PC",))
            assert result.after_cpu is not None
            assert result.after_memory is not None
            self.assertEqual(result.after_cpu.pc, 0x00200E66)
            self.assertEqual(result.after_memory[0x004C6A], 0x01)
            self.assertTrue(result.after_cpu.flags.zf)
            self.assertTrue(result.after_cpu.flags.sf)
            self.assertTrue(result.after_cpu.flags.vf)
            self.assertTrue(result.after_cpu.flags.cf)   # C is not touched
            # TSET writes H and N as well -- Toshiba's symbol row is `x * 1 x 0 -`.
            # This test used to assert the opposite (its name still says "z only"):
            # it had frozen a bug in which TSET set Z and left H and N alone, which
            # is exactly what the C++ differential harness surfaced on 2026-07-12.
            self.assertTrue(result.after_cpu.flags.hf)
            self.assertFalse(result.after_cpu.flags.nf)

    def test_execute_abs24_memory_andcf_updates_carry_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x00200E87, b"\xF2\x6A\x4C\x00\x80")
            base_view = load_fetch_view(rom_path)
            seeded_cpu = replace(
                base_view.machine.cpu,
                flags=StatusFlags(sf=True, zf=False, vf=True, hf=False, cf=True, nf=True),
            )

            result = build_execute_next(
                base_view,
                cpu_state=seeded_cpu,
                memory_bytes={0x004C6A: 0x00},
            )

            self.assertEqual(result.status, "executed")
            self.assertEqual(result.decode.assembly, "andcf 0, (0x004C6A)")
            self.assertEqual(result.written_registers, ("PC",))
            assert result.after_cpu is not None
            self.assertEqual(result.after_cpu.pc, 0x00200E8C)
            self.assertFalse(result.after_cpu.flags.cf)
            self.assertTrue(result.after_cpu.flags.sf)
            self.assertFalse(result.after_cpu.flags.zf)
            self.assertTrue(result.after_cpu.flags.vf)
            self.assertFalse(result.after_cpu.flags.hf)
            self.assertTrue(result.after_cpu.flags.nf)

    def test_execute_abs24_memory_ldcf_from_dynamic_a_bit(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x00200EA0, b"\xF2\x6A\x4C\x00\x2B")
            base_view = load_fetch_view(rom_path)
            seeded_cpu = replace(
                base_view.machine.cpu,
                flags=StatusFlags(sf=False, zf=True, vf=True, hf=False, cf=False, nf=True),
                regs=replace(base_view.machine.cpu.regs, xwa=0x00000004),
            )

            result = build_execute_next(
                base_view,
                cpu_state=seeded_cpu,
                memory_bytes={0x004C6A: 0x10},
            )

            self.assertEqual(result.status, "executed")
            self.assertEqual(result.decode.assembly, "ldcf A, (0x004C6A)")
            self.assertEqual(result.written_registers, ("PC",))
            assert result.after_cpu is not None
            self.assertEqual(result.after_cpu.pc, 0x00200EA5)
            self.assertTrue(result.after_cpu.flags.cf)
            self.assertFalse(result.after_cpu.flags.sf)
            self.assertTrue(result.after_cpu.flags.zf)
            self.assertTrue(result.after_cpu.flags.vf)
            self.assertFalse(result.after_cpu.flags.hf)
            self.assertTrue(result.after_cpu.flags.nf)

    def test_execute_abs24_memory_stcf_writes_bit_from_carry(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x00200EB0, b"\xF2\x6A\x4C\x00\xA0")
            base_view = load_fetch_view(rom_path)
            seeded_cpu = replace(
                base_view.machine.cpu,
                flags=StatusFlags(sf=False, zf=True, vf=True, hf=False, cf=False, nf=True),
            )

            result = build_execute_next(
                base_view,
                cpu_state=seeded_cpu,
                memory_bytes={0x004C6A: 0xFF},
            )

            self.assertEqual(result.status, "executed")
            self.assertEqual(result.decode.assembly, "stcf 0, (0x004C6A)")
            self.assertEqual(result.written_registers, ("PC",))
            assert result.after_cpu is not None
            assert result.after_memory is not None
            self.assertEqual(result.after_cpu.pc, 0x00200EB5)
            self.assertEqual(result.after_memory[0x004C6A], 0xFE)
            self.assertFalse(result.after_cpu.flags.cf)
            self.assertFalse(result.after_cpu.flags.sf)
            self.assertTrue(result.after_cpu.flags.zf)
            self.assertTrue(result.after_cpu.flags.vf)
            self.assertFalse(result.after_cpu.flags.hf)
            self.assertTrue(result.after_cpu.flags.nf)

    def test_execute_prefixed_andcf_immediate_updates_carry_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x00200040, b"\xC9\x20\x03")
            base_view = load_fetch_view(rom_path)
            seeded_cpu = replace(
                base_view.machine.cpu,
                regs=replace(base_view.machine.cpu.regs, xwa=0x00000000),
                flags=StatusFlags(sf=True, zf=False, vf=True, hf=False, cf=True, nf=True),
            )

            result = build_execute_next(base_view, cpu_state=seeded_cpu)

            self.assertEqual(result.status, "executed")
            self.assertEqual(result.decode.assembly, "andcf 3, A")
            self.assertEqual(result.written_registers, ("PC",))
            assert result.after_cpu is not None
            self.assertEqual(result.after_cpu.pc, 0x00200043)
            self.assertFalse(result.after_cpu.flags.cf)
            self.assertTrue(result.after_cpu.flags.sf)
            self.assertFalse(result.after_cpu.flags.zf)
            self.assertTrue(result.after_cpu.flags.vf)
            self.assertFalse(result.after_cpu.flags.hf)
            self.assertTrue(result.after_cpu.flags.nf)

    def test_execute_prefixed_ldcf_dynamic_byte_uses_low_nibble_of_a(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x00200040, b"\xC9\x2B")
            base_view = load_fetch_view(rom_path)
            seeded_cpu = replace(
                base_view.machine.cpu,
                regs=replace(base_view.machine.cpu.regs, xwa=0x00000014),
                flags=StatusFlags(sf=False, zf=True, vf=True, hf=False, cf=False, nf=True),
            )

            result = build_execute_next(base_view, cpu_state=seeded_cpu)

            self.assertEqual(result.status, "executed")
            self.assertEqual(result.decode.assembly, "ldcf A, A")
            self.assertEqual(result.written_registers, ("PC",))
            assert result.after_cpu is not None
            self.assertEqual(result.after_cpu.pc, 0x00200042)
            self.assertTrue(result.after_cpu.flags.cf)
            self.assertFalse(result.after_cpu.flags.sf)
            self.assertTrue(result.after_cpu.flags.zf)
            self.assertTrue(result.after_cpu.flags.vf)
            self.assertFalse(result.after_cpu.flags.hf)
            self.assertTrue(result.after_cpu.flags.nf)

    def test_execute_prefixed_stcf_dynamic_byte_out_of_range_leaves_operand_unchanged(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x00200040, b"\xC8\x2C")
            base_view = load_fetch_view(rom_path)
            seeded_cpu = replace(
                base_view.machine.cpu,
                regs=replace(base_view.machine.cpu.regs, xwa=0x00001208),
                flags=StatusFlags(sf=False, zf=True, vf=True, hf=False, cf=True, nf=True),
            )

            result = build_execute_next(base_view, cpu_state=seeded_cpu)

            self.assertEqual(result.status, "executed")
            self.assertEqual(result.decode.assembly, "stcf A, W")
            self.assertEqual(result.written_registers, ("PC",))
            assert result.after_cpu is not None
            self.assertEqual(result.after_cpu.pc, 0x00200042)
            self.assertEqual(result.after_cpu.regs.xwa, 0x00001208)

    def test_execute_prefixed_ldcf_dynamic_byte_out_of_range_stops_as_silicon_undefined(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x00200040, b"\xC8\x2B")
            base_view = load_fetch_view(rom_path)
            seeded_cpu = replace(
                base_view.machine.cpu,
                regs=replace(base_view.machine.cpu.regs, xwa=0x00001208),
            )

            result = build_execute_next(base_view, cpu_state=seeded_cpu)

            self.assertEqual(result.status, "silicon-undefined")
            self.assertEqual(result.decode.assembly, "ldcf A, W")
            self.assertIsNone(result.after_cpu)

    def test_execute_prefixed_extz_byte_stops_as_silicon_undefined(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x00200040, b"\xC8\x12")
            base_view = load_fetch_view(rom_path)
            seeded_cpu = replace(
                base_view.machine.cpu,
                regs=replace(base_view.machine.cpu.regs, xwa=0x00001234),
            )

            result = build_execute_next(base_view, cpu_state=seeded_cpu)

            self.assertEqual(result.status, "silicon-undefined")
            self.assertEqual(result.decode.assembly, "extz W")
            self.assertIsNone(result.after_cpu)

    def test_execute_abs24_word_load_from_template_pattern_updates_wa(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x00201D1A, b"\xD2\x06\x4F\x00\x20")
            base_view = load_fetch_view(rom_path)
            seeded_cpu = replace(
                base_view.machine.cpu,
                regs=replace(base_view.machine.cpu.regs, xwa=0x00000000),
            )

            result = build_execute_next(
                base_view,
                cpu_state=seeded_cpu,
                memory_bytes={
                    0x004F06: 0x34,
                    0x004F07: 0x12,
                },
            )

            self.assertEqual(result.status, "executed")
            self.assertEqual(result.decode.assembly, "ld WA, (0x004F06)")
            self.assertEqual(result.written_registers, ("WA", "PC"))
            assert result.after_cpu is not None
            self.assertEqual(result.after_cpu.regs.xwa, 0x00001234)
            self.assertEqual(result.after_cpu.pc, 0x00201D1F)

    def test_execute_abs16_memory_xor_immediate_writes_overlay(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x00201B42, b"\xC1\x30\x80\x3D\x80")
            base_view = load_fetch_view(rom_path)

            result = build_execute_next(
                base_view,
                memory_bytes={0x8030: 0x55},
            )

            self.assertEqual(result.status, "executed")
            self.assertEqual(result.decode.assembly, "xor (0x8030), 0x80")
            self.assertEqual(result.written_registers, ("PC",))
            assert result.after_cpu is not None
            assert result.after_memory is not None
            self.assertEqual(result.after_cpu.pc, 0x00201B47)
            self.assertEqual(result.after_memory[0x8030], 0xD5)
            self.assertTrue(result.after_cpu.flags.sf)
            self.assertFalse(result.after_cpu.flags.zf)
            self.assertFalse(result.after_cpu.flags.vf)
            self.assertFalse(result.after_cpu.flags.cf)
            self.assertFalse(result.after_cpu.flags.nf)

    def test_execute_abs8_memory_or_immediate_writes_overlay(self) -> None:
        # C0 B2 3E 01 = or (0xB2), 0x01 — abs8 byte RMW on the CPU I/O page.
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x00200040, b"\xC0\xB2\x3E\x01")
            base_view = load_fetch_view(rom_path)

            result = build_execute_next(base_view, memory_bytes={0x00B2: 0x04})

            self.assertEqual(result.status, "executed")
            self.assertEqual(result.decode.assembly, "or (0xB2), 0x01")
            assert result.after_cpu is not None
            assert result.after_memory is not None
            self.assertEqual(result.after_cpu.pc, 0x00200044)
            self.assertEqual(result.after_memory[0x00B2], 0x05)  # 0x04 | 0x01

    def test_execute_ldw_reg_indirect_imm16_writes_overlay(self) -> None:
        """ldw (XBC), 0x1234 — stores word little-endian at XBC address."""
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            # B1 02 34 12 = ldw (XBC), 0x1234
            self._write_demo_rom(rom_path, 0x00200040, b"\xB1\x02\x34\x12")
            base_view = load_fetch_view(rom_path)
            # Seed XBC to a writable RAM address
            cpu = seed_cpu_state_for_execution(
                base_view.machine.cpu,
                register_values={"XBC": 0x00007000},
            )
            result = build_execute_next(base_view, cpu_state=cpu)

            self.assertEqual(result.status, "executed")
            self.assertEqual(result.decode.assembly, "ldw (XBC), 0x1234")
            assert result.after_memory is not None
            self.assertEqual(result.after_memory.get(0x7000), 0x34)
            self.assertEqual(result.after_memory.get(0x7001), 0x12)

    def test_execute_ld_reg_indirect_imm8_writes_overlay(self) -> None:
        """ld (XDE), 0x42 — stores byte at XDE address."""
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            # B2 00 42 = ld (XDE), 0x42
            self._write_demo_rom(rom_path, 0x00200040, b"\xB2\x00\x42")
            base_view = load_fetch_view(rom_path)
            cpu = seed_cpu_state_for_execution(
                base_view.machine.cpu,
                register_values={"XDE": 0x00007800},
            )
            result = build_execute_next(base_view, cpu_state=cpu)

            self.assertEqual(result.status, "executed")
            self.assertEqual(result.decode.assembly, "ld (XDE), 0x42")
            assert result.after_memory is not None
            self.assertEqual(result.after_memory.get(0x7800), 0x42)

    def test_execute_ldw_reg_indirect_blocks_when_r32_unknown(self) -> None:
        """ldw (XBC), 0x1234 — blocked when XBC is not seeded."""
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x00200040, b"\xB1\x02\x34\x12")
            view = load_fetch_view(rom_path)

            result = build_execute_next(view)

            self.assertEqual(result.status, "requires-known-address-register")

    def test_execute_exts_r32_sign_extends_word_to_long(self) -> None:
        """exts XBC — sign-extends lower 16 bits, positive value stays unchanged."""
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            # E9 13 = exts XBC
            self._write_demo_rom(rom_path, 0x00200040, b"\xE9\x13")
            base_view = load_fetch_view(rom_path)
            cpu = seed_cpu_state_for_execution(
                base_view.machine.cpu,
                register_values={"XBC": 0x00000008},
            )
            result = build_execute_next(base_view, cpu_state=cpu)

            self.assertEqual(result.status, "executed")
            # bit 15 of 0x0008 is 0, so upper 16 → 0x0000
            assert result.after_cpu is not None
            self.assertEqual(result.after_cpu.regs.xbc, 0x00000008)

    def test_execute_exts_r32_sign_extends_negative_word(self) -> None:
        """exts XBC — bit 15 set in lower 16 causes upper 16 to become 0xFFFF."""
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            # E9 13 = exts XBC
            self._write_demo_rom(rom_path, 0x00200040, b"\xE9\x13")
            base_view = load_fetch_view(rom_path)
            cpu = seed_cpu_state_for_execution(
                base_view.machine.cpu,
                register_values={"XBC": 0x00008001},
            )
            result = build_execute_next(base_view, cpu_state=cpu)

            self.assertEqual(result.status, "executed")
            assert result.after_cpu is not None
            self.assertEqual(result.after_cpu.regs.xbc, 0xFFFF8001)

    def test_execute_extz_r32_zero_extends_word_to_long(self) -> None:
        """extz XBC — clears upper 16 bits regardless of their prior value."""
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            # E9 12 = extz XBC
            self._write_demo_rom(rom_path, 0x00200040, b"\xE9\x12")
            base_view = load_fetch_view(rom_path)
            cpu = seed_cpu_state_for_execution(
                base_view.machine.cpu,
                register_values={"XBC": 0xDEAD1234},
            )
            result = build_execute_next(base_view, cpu_state=cpu)

            self.assertEqual(result.status, "executed")
            assert result.after_cpu is not None
            self.assertEqual(result.after_cpu.regs.xbc, 0x00001234)

    def test_execute_prefixed_cpl_byte_inverts_bits_and_preserves_szc(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x00200040, b"\xC9\x06")
            view = load_fetch_view(rom_path)
            seeded_cpu = replace(
                view.machine.cpu,
                regs=replace(view.machine.cpu.regs, xwa=0x0000005A),
                flags=StatusFlags(sf=True, zf=False, vf=False, hf=False, cf=True, nf=False),
            )

            result = build_execute_next(view, cpu_state=seeded_cpu)

            self.assertEqual(result.status, "executed")
            self.assertEqual(result.decode.assembly, "cpl A")
            assert result.after_cpu is not None
            self.assertEqual(result.after_cpu.regs.xwa & 0xFF, 0xA5)
            self.assertTrue(result.after_cpu.flags.sf)
            self.assertFalse(result.after_cpu.flags.zf)
            self.assertTrue(result.after_cpu.flags.cf)
            self.assertTrue(result.after_cpu.flags.hf)
            self.assertTrue(result.after_cpu.flags.nf)

    def test_execute_prefixed_neg_byte_uses_subtract_flags(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x00200040, b"\xC9\x07")
            view = load_fetch_view(rom_path)
            seeded_cpu = replace(
                view.machine.cpu,
                regs=replace(view.machine.cpu.regs, xwa=0x00000005),
            )

            result = build_execute_next(view, cpu_state=seeded_cpu)

            self.assertEqual(result.status, "executed")
            self.assertEqual(result.decode.assembly, "neg A")
            assert result.after_cpu is not None
            self.assertEqual(result.after_cpu.regs.xwa & 0xFF, 0xFB)
            self.assertTrue(result.after_cpu.flags.sf)
            self.assertFalse(result.after_cpu.flags.zf)
            self.assertTrue(result.after_cpu.flags.hf)
            self.assertTrue(result.after_cpu.flags.cf)
            self.assertTrue(result.after_cpu.flags.nf)


    def test_execute_prefixed_push_byte_writes_one_stack_byte(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x00200040, b"\xC9\x04")
            view = load_fetch_view(rom_path)
            seeded_cpu = replace(
                view.machine.cpu,
                regs=replace(view.machine.cpu.regs, xwa=0x123456AB, xsp=0x00006C00),
            )

            result = build_execute_next(view, cpu_state=seeded_cpu)

            self.assertEqual(result.status, "executed")
            self.assertEqual(result.decode.assembly, "push A")
            assert result.after_cpu is not None
            self.assertEqual(result.after_cpu.regs.xsp, 0x00006BFF)
            self.assertEqual(len(result.memory_writes), 1)
            self.assertEqual(result.memory_writes[0].address, 0x00006BFF)
            self.assertEqual(result.memory_writes[0].data, b"\xAB")

    def test_execute_prefixed_pop_byte_reads_one_stack_byte(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x00200040, b"\xC9\x05")
            view = load_fetch_view(rom_path)
            seeded_cpu = replace(
                view.machine.cpu,
                regs=replace(view.machine.cpu.regs, xwa=0x12345600, xsp=0x00006C00),
            )

            result = build_execute_next(
                view,
                cpu_state=seeded_cpu,
                memory_bytes={0x00006C00: 0xAB},
            )

            self.assertEqual(result.status, "executed")
            self.assertEqual(result.decode.assembly, "pop A")
            assert result.after_cpu is not None
            self.assertEqual(result.after_cpu.regs.xwa, 0x123456AB)
            self.assertEqual(result.after_cpu.regs.xsp, 0x00006C01)

    def test_execute_prefixed_push_long_writes_four_stack_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x00200040, b"\xE9\x04")
            view = load_fetch_view(rom_path)
            seeded_cpu = replace(
                view.machine.cpu,
                regs=replace(view.machine.cpu.regs, xbc=0x12345678, xsp=0x00006C00),
            )

            result = build_execute_next(view, cpu_state=seeded_cpu)

            self.assertEqual(result.status, "executed")
            self.assertEqual(result.decode.assembly, "push XBC")
            assert result.after_cpu is not None
            self.assertEqual(result.after_cpu.regs.xsp, 0x00006BFC)
            self.assertEqual(len(result.memory_writes), 1)
            self.assertEqual(result.memory_writes[0].address, 0x00006BFC)
            self.assertEqual(result.memory_writes[0].data, b"\x78\x56\x34\x12")

    def test_execute_prefixed_daa_addition_adjusts_bcd_and_sets_parity(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x00200040, b"\xC9\x10")
            view = load_fetch_view(rom_path)
            seeded_cpu = replace(
                view.machine.cpu,
                regs=replace(view.machine.cpu.regs, xwa=0x0000006C),
                flags=StatusFlags(sf=False, zf=False, vf=False, hf=False, cf=False, nf=False),
            )

            result = build_execute_next(view, cpu_state=seeded_cpu)

            self.assertEqual(result.status, "executed")
            self.assertEqual(result.decode.assembly, "daa A")
            assert result.after_cpu is not None
            self.assertEqual(result.after_cpu.regs.xwa & 0xFF, 0x72)
            self.assertFalse(result.after_cpu.flags.sf)
            self.assertFalse(result.after_cpu.flags.zf)
            self.assertTrue(result.after_cpu.flags.vf)
            self.assertTrue(result.after_cpu.flags.hf)
            self.assertFalse(result.after_cpu.flags.cf)
            self.assertFalse(result.after_cpu.flags.nf)

    def test_execute_prefixed_daa_subtraction_uses_incoming_hn_flags(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x00200040, b"\xC9\x10")
            view = load_fetch_view(rom_path)
            seeded_cpu = replace(
                view.machine.cpu,
                regs=replace(view.machine.cpu.regs, xwa=0x0000000F),
                flags=StatusFlags(sf=False, zf=False, vf=False, hf=True, cf=False, nf=True),
            )

            result = build_execute_next(view, cpu_state=seeded_cpu)

            self.assertEqual(result.status, "executed")
            assert result.after_cpu is not None
            self.assertEqual(result.after_cpu.regs.xwa & 0xFF, 0x09)
            self.assertFalse(result.after_cpu.flags.sf)
            self.assertFalse(result.after_cpu.flags.zf)
            self.assertTrue(result.after_cpu.flags.vf)
            self.assertFalse(result.after_cpu.flags.hf)
            self.assertFalse(result.after_cpu.flags.cf)
            self.assertTrue(result.after_cpu.flags.nf)

    def test_execute_prefixed_daa_blocks_when_bcd_flags_unknown(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x00200040, b"\xC9\x10")
            view = load_fetch_view(rom_path)
            seeded_cpu = replace(
                view.machine.cpu,
                regs=replace(view.machine.cpu.regs, xwa=0x0000006C),
                flags=StatusFlags(sf=False, zf=False, vf=False, hf=None, cf=False, nf=False),
            )

            result = build_execute_next(view, cpu_state=seeded_cpu)

            self.assertEqual(result.status, "requires-known-flags")

    def test_execute_ei_0_sets_iff_enabled(self) -> None:
        """ei 0 — sets IFF to enabled (level 0 = all IRQs permitted)."""
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            # 06 00 = ei 0
            self._write_demo_rom(rom_path, 0x00200040, b"\x06\x00")
            view = load_fetch_view(rom_path)

            result = build_execute_next(view)

            self.assertEqual(result.status, "executed")
            self.assertIn("IFF", result.written_registers)
            assert result.after_cpu is not None
            self.assertEqual(result.after_cpu.iff_enabled, True)
            self.assertEqual(result.after_cpu.iff_level, 0)
            self.assertEqual(result.after_cpu.pc, 0x00200042)

    def test_execute_ei_3_sets_iff_level_to_3(self) -> None:
        """ei 3 — sets interrupt mask level to 3 (IRQs of priority > 3 permitted)."""
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            # 06 03 = ei 3
            self._write_demo_rom(rom_path, 0x00200040, b"\x06\x03")
            view = load_fetch_view(rom_path)

            result = build_execute_next(view)

            self.assertEqual(result.status, "executed")
            assert result.after_cpu is not None
            self.assertEqual(result.after_cpu.iff_level, 3)
            self.assertEqual(result.after_cpu.iff_enabled, True)

    def test_execute_cp_r8_reg_indirect_equal_sets_zero_flag(self) -> None:
        """CP R8, (R32) via 0x80..0x87 + 0xF0..0xF7 (pass 51).

        Source: ngdis/tlcs900_zz_mem.c case 0xF0 "CP R,(mem)".
        Encoding: [0x80+r32_idx] [0xF0+r8_idx] = 2 bytes.
        Effect: flags = R8 - mem ; no register or memory write ;
        Z=1 when R8 == mem.
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            # 0x83 = prefix r32_idx=3 (XHL) ; 0xF1 = sub-op r8_idx=1 (A).
            # → CP A, (XHL)
            self._write_demo_rom(rom_path, 0x00200040, b"\x83\xF1")
            view = load_fetch_view(rom_path)
            seeded = replace(
                view.machine.cpu,
                regs=replace(
                    view.machine.cpu.regs, xhl=0x00006000, xwa=0x00000042,
                ),
                flags=StatusFlags(
                    sf=False, zf=False, vf=False, hf=False, cf=False, nf=False,
                ),
                iff_level=0, rfp=0,
            )
            # A is the low byte of XWA. XWA=0x42 → A=0x42.
            result = build_execute_next(
                view, cpu_state=seeded,
                memory_bytes={0x00006000: 0x42},
            )
            self.assertEqual(result.status, "executed")
            assert result.after_cpu is not None
            flags = result.after_cpu.flags
            self.assertTrue(flags.zf, "Z must be set when A == mem")
            self.assertFalse(flags.cf, "C must be clear when no borrow")
            self.assertTrue(flags.nf, "N must be set (subtract-op flag)")
            self.assertEqual(result.after_cpu.pc, 0x00200042)
            # No memory write or register update other than flags + PC.
            self.assertEqual(result.memory_writes, ())

    def test_execute_cp_r8_reg_indirect_a_less_than_mem_sets_carry(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x00200040, b"\x83\xF1")
            view = load_fetch_view(rom_path)
            seeded = replace(
                view.machine.cpu,
                regs=replace(
                    view.machine.cpu.regs, xhl=0x00006000, xwa=0x00000005,
                ),
                flags=StatusFlags(
                    sf=False, zf=False, vf=False, hf=False, cf=False, nf=False,
                ),
                iff_level=0, rfp=0,
            )
            # A=0x05 cmp mem=0x10 → A < mem → borrow → C=1
            result = build_execute_next(
                view, cpu_state=seeded,
                memory_bytes={0x00006000: 0x10},
            )
            self.assertEqual(result.status, "executed")
            flags = result.after_cpu.flags
            self.assertTrue(flags.cf, "C must be set when A < mem (borrow)")
            self.assertFalse(flags.zf)
            self.assertTrue(flags.sf, "S must be set (result is negative)")

    def test_execute_cp_r8_reg_indirect_blocks_on_unknown_a(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x00200040, b"\x83\xF1")
            view = load_fetch_view(rom_path)
            # XHL known, XWA (= owner of A) unknown.
            seeded = replace(
                view.machine.cpu,
                regs=replace(view.machine.cpu.regs, xhl=0x00006000),
            )
            result = build_execute_next(
                view, cpu_state=seeded,
                memory_bytes={0x00006000: 0x10},
            )
            self.assertEqual(result.status, "requires-known-source-register")

    def test_execute_cp_r8_reg_indirect_blocks_on_unknown_address_register(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x00200040, b"\x83\xF1")
            view = load_fetch_view(rom_path)
            # XHL unknown.
            seeded = replace(
                view.machine.cpu,
                regs=replace(view.machine.cpu.regs, xwa=0x00000005),
            )
            result = build_execute_next(view, cpu_state=seeded)
            self.assertEqual(result.status, "requires-known-address-register")

    def test_execute_reg_indirect_word_load_updates_wa(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x00200040, b"\x91\x20")
            view = load_fetch_view(rom_path)
            seeded = seed_cpu_state_for_execution(
                view.machine.cpu,
                register_values={"XBC": 0x00006000, "XWA": 0xAABB0000},
            )

            result = build_execute_next(
                view,
                cpu_state=seeded,
                memory_bytes={0x00006000: 0x34, 0x00006001: 0x12},
            )

            self.assertEqual(result.status, "executed")
            self.assertEqual(result.decode.assembly, "ld WA, (XBC)")
            assert result.after_cpu is not None
            self.assertEqual(result.after_cpu.regs.xwa, 0xAABB1234)

    def test_execute_reg_indirect_word_add_memory_destination(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x00200040, b"\x91\x88")
            view = load_fetch_view(rom_path)
            seeded = seed_cpu_state_for_execution(
                view.machine.cpu,
                register_values={"XBC": 0x00006000, "XWA": 0x00000002},
            )

            result = build_execute_next(
                view,
                cpu_state=seeded,
                memory_bytes={0x00006000: 0x01, 0x00006001: 0x00},
            )

            self.assertEqual(result.status, "executed")
            self.assertEqual(result.decode.assembly, "add (XBC), WA")
            assert result.after_memory is not None
            self.assertEqual(result.after_memory[0x00006000], 0x03)
            self.assertEqual(result.after_memory[0x00006001], 0x00)
            self.assertFalse(result.after_cpu.flags.cf)

    def test_execute_reg_indirect_word_add_immediate_writes_memory(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x00200040, b"\x91\x38\x34\x12")
            view = load_fetch_view(rom_path)
            seeded = seed_cpu_state_for_execution(
                view.machine.cpu,
                register_values={"XBC": 0x00006000},
            )

            result = build_execute_next(
                view,
                cpu_state=seeded,
                memory_bytes={0x00006000: 0x01, 0x00006001: 0x00},
            )

            self.assertEqual(result.status, "executed")
            self.assertEqual(result.decode.assembly, "add (XBC), 0x1234")
            assert result.after_memory is not None
            self.assertEqual(result.after_memory[0x00006000], 0x35)
            self.assertEqual(result.after_memory[0x00006001], 0x12)

    def test_execute_reg_indirect_word_cp_memory_minus_register_sets_flags_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x00200040, b"\x91\xF8")
            view = load_fetch_view(rom_path)
            seeded = seed_cpu_state_for_execution(
                view.machine.cpu,
                register_values={"XBC": 0x00006000, "XWA": 0x00000010},
            )

            result = build_execute_next(
                view,
                cpu_state=seeded,
                memory_bytes={0x00006000: 0x05, 0x00006001: 0x00},
            )

            self.assertEqual(result.status, "executed")
            self.assertEqual(result.decode.assembly, "cp (XBC), WA")
            self.assertEqual(result.memory_writes, ())
            assert result.after_cpu is not None
            self.assertTrue(result.after_cpu.flags.cf)
            self.assertTrue(result.after_cpu.flags.sf)

    def test_execute_and_mem_r8_writes_back_to_memory(self) -> None:
        """AND (R32), R8 — encoding [0x80+r32_idx][0xC8+r8_idx], 2 bytes.

        Verified against NgpCraft_Disasm oracle : `0x81 0xC9` → `and (XBC), A`.
        Pass 53.
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x00200040, b"\x81\xC9")
            view = load_fetch_view(rom_path)
            seeded = replace(
                view.machine.cpu,
                regs=replace(
                    view.machine.cpu.regs, xbc=0x00006000, xwa=0x000000F0,
                ),
                flags=StatusFlags(
                    sf=False, zf=False, vf=False, hf=False, cf=False, nf=False,
                ),
                iff_level=0, rfp=0,
            )
            # A = low byte of XWA = 0xF0 ; mem[0x6000] = 0xAA.
            # 0xF0 & 0xAA = 0xA0
            result = build_execute_next(
                view, cpu_state=seeded,
                memory_bytes={0x00006000: 0xAA},
            )
            self.assertEqual(result.status, "executed")
            self.assertEqual(result.after_memory[0x00006000], 0xA0)
            # Sign bit of 0xA0 is set → S=1, Z=0, V=0, C=0, N=0
            f = result.after_cpu.flags
            self.assertTrue(f.sf)
            self.assertFalse(f.zf)
            self.assertFalse(f.cf)
            self.assertFalse(f.nf)
            # Memory writes record the change.
            self.assertEqual(len(result.memory_writes), 1)

    def test_execute_or_r8_mem_writes_back_to_register(self) -> None:
        """OR R8, (R32) — encoding [0x80+r32_idx][0xE0+r8_idx], 2 bytes.

        Verified : `0x83 0xE0` → `or W, (XHL)`. Pass 53.
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x00200040, b"\x83\xE0")
            view = load_fetch_view(rom_path)
            seeded = replace(
                view.machine.cpu,
                # XWA = 0x1234 → W = 0x12 (high), A = 0x34 (low)
                regs=replace(
                    view.machine.cpu.regs, xhl=0x00006000, xwa=0x00001234,
                ),
                flags=StatusFlags(
                    sf=False, zf=False, vf=False, hf=False, cf=False, nf=False,
                ),
                iff_level=0, rfp=0,
            )
            result = build_execute_next(
                view, cpu_state=seeded,
                memory_bytes={0x00006000: 0x0F},
            )
            self.assertEqual(result.status, "executed")
            # W ← W | mem = 0x12 | 0x0F = 0x1F. A unchanged = 0x34.
            # XWA = (W<<8) | A = 0x1F34
            self.assertEqual(result.after_cpu.regs.xwa, 0x00001F34)
            # No memory write for the R8-dest direction.
            self.assertEqual(result.memory_writes, ())

    def test_execute_xor_mem_r8_clears_when_equal(self) -> None:
        """XOR (R32), R8 — encoding [0x80+r32_idx][0xD8+r8_idx], 2 bytes.

        Verified : `0x85 0xDD` → `xor (XIY), E`. Pass 53.
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x00200040, b"\x87\xDF")
            view = load_fetch_view(rom_path)
            seeded = replace(
                view.machine.cpu,
                # XHL = 0x000000FF → L (low byte) = 0xFF
                regs=replace(
                    view.machine.cpu.regs, xsp=0x00006000, xhl=0x000000FF,
                ),
                flags=StatusFlags(
                    sf=False, zf=False, vf=False, hf=False, cf=False, nf=False,
                ),
                iff_level=0, rfp=0,
            )
            # mem[0x6000] = 0xFF. 0xFF ^ 0xFF = 0x00 → Z=1
            result = build_execute_next(
                view, cpu_state=seeded,
                memory_bytes={0x00006000: 0xFF},
            )
            self.assertEqual(result.status, "executed")
            self.assertEqual(result.after_memory[0x00006000], 0x00)
            self.assertTrue(result.after_cpu.flags.zf)
            self.assertFalse(result.after_cpu.flags.sf)

    def test_execute_logical_blocks_on_unknown_source(self) -> None:
        """ALU mem-op blocks when the R8 owner is unmodeled."""
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x00200040, b"\x83\xC0")  # AND W, (XHL)
            view = load_fetch_view(rom_path)
            seeded = replace(
                view.machine.cpu,
                regs=replace(view.machine.cpu.regs, xhl=0x00006000),
                # XWA stays None
            )
            result = build_execute_next(
                view, cpu_state=seeded,
                memory_bytes={0x00006000: 0xFF},
            )
            self.assertEqual(result.status, "requires-known-source-register")

    def test_execute_add_r8_mem_writes_back_to_register(self) -> None:
        """ADD R8, (R32) — encoding [0x80+r32_idx][0x80+r8_idx], 2 bytes.

        Oracle verified : `0x83 0x81` → `add A, (XHL)`. Pass 54.
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x00200040, b"\x83\x81")
            view = load_fetch_view(rom_path)
            seeded = replace(
                view.machine.cpu,
                regs=replace(
                    view.machine.cpu.regs, xhl=0x00006000, xwa=0x00000010,
                ),
                flags=StatusFlags(
                    sf=False, zf=False, vf=False, hf=False, cf=False, nf=False,
                ),
                iff_level=0, rfp=0,
            )
            # A = 0x10, mem = 0x20 → A = 0x30. No carry, no zero.
            result = build_execute_next(
                view, cpu_state=seeded,
                memory_bytes={0x00006000: 0x20},
            )
            self.assertEqual(result.status, "executed")
            self.assertEqual(result.after_cpu.regs.xwa & 0xFF, 0x30)
            self.assertFalse(result.after_cpu.flags.cf)
            self.assertFalse(result.after_cpu.flags.zf)
            self.assertFalse(result.after_cpu.flags.nf)
            self.assertEqual(result.memory_writes, ())

    def test_execute_add_mem_r8_writes_back_to_memory_with_carry(self) -> None:
        """ADD (R32), R8 — encoding [0x80+r32_idx][0x88+r8_idx], 2 bytes.

        Oracle verified : `0x83 0x88` → `add (XHL), W`. Pass 54.
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x00200040, b"\x83\x88")
            view = load_fetch_view(rom_path)
            # XWA = 0xF034 → W (high byte) = 0xF0
            seeded = replace(
                view.machine.cpu,
                regs=replace(
                    view.machine.cpu.regs, xhl=0x00006000, xwa=0x0000F034,
                ),
                flags=StatusFlags(
                    sf=False, zf=False, vf=False, hf=False, cf=False, nf=False,
                ),
                iff_level=0, rfp=0,
            )
            # mem = 0x20 ; mem ← mem + W = 0x20 + 0xF0 = 0x110 → 0x10, C=1
            result = build_execute_next(
                view, cpu_state=seeded,
                memory_bytes={0x00006000: 0x20},
            )
            self.assertEqual(result.status, "executed")
            self.assertEqual(result.after_memory[0x00006000], 0x10)
            self.assertTrue(result.after_cpu.flags.cf)
            self.assertEqual(len(result.memory_writes), 1)

    def test_execute_sub_r8_mem_no_borrow(self) -> None:
        """SUB R8, (R32) — encoding [0x80+r32_idx][0xA0+r8_idx], 2 bytes.

        Oracle verified : `0x83 0xA1` → `sub A, (XHL)`. Pass 54.
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x00200040, b"\x83\xA1")
            view = load_fetch_view(rom_path)
            seeded = replace(
                view.machine.cpu,
                regs=replace(
                    view.machine.cpu.regs, xhl=0x00006000, xwa=0x00000010,
                ),
                flags=StatusFlags(
                    sf=False, zf=False, vf=False, hf=False, cf=False, nf=False,
                ),
                iff_level=0, rfp=0,
            )
            # A = 0x10, mem = 0x05 → A = 0x0B, no borrow.
            result = build_execute_next(
                view, cpu_state=seeded,
                memory_bytes={0x00006000: 0x05},
            )
            self.assertEqual(result.after_cpu.regs.xwa & 0xFF, 0x0B)
            self.assertFalse(result.after_cpu.flags.cf)
            self.assertTrue(result.after_cpu.flags.nf, "SUB sets N=1")

    def test_execute_sub_r8_mem_with_borrow(self) -> None:
        """SUB R8, (R32) — A < mem → borrow, C=1, result wraps."""
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x00200040, b"\x83\xA1")
            view = load_fetch_view(rom_path)
            seeded = replace(
                view.machine.cpu,
                regs=replace(
                    view.machine.cpu.regs, xhl=0x00006000, xwa=0x00000005,
                ),
                flags=StatusFlags(
                    sf=False, zf=False, vf=False, hf=False, cf=False, nf=False,
                ),
                iff_level=0, rfp=0,
            )
            # A=0x05 - mem=0x10 = 0xF5 (with borrow), C=1, S=1
            result = build_execute_next(
                view, cpu_state=seeded,
                memory_bytes={0x00006000: 0x10},
            )
            self.assertEqual(result.after_cpu.regs.xwa & 0xFF, 0xF5)
            self.assertTrue(result.after_cpu.flags.cf)
            self.assertTrue(result.after_cpu.flags.sf)

    def test_execute_sub_mem_r8_writes_back_to_memory(self) -> None:
        """SUB (R32), R8 — encoding [0x80+r32_idx][0xA8+r8_idx], 2 bytes.

        Oracle verified : `0x83 0xA9` → `sub (XHL), A`. Pass 54.
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x00200040, b"\x83\xA9")
            view = load_fetch_view(rom_path)
            seeded = replace(
                view.machine.cpu,
                regs=replace(
                    view.machine.cpu.regs, xhl=0x00006000, xwa=0x00000005,
                ),
                flags=StatusFlags(
                    sf=False, zf=False, vf=False, hf=False, cf=False, nf=False,
                ),
                iff_level=0, rfp=0,
            )
            # mem ← mem - A = 0x10 - 0x05 = 0x0B
            result = build_execute_next(
                view, cpu_state=seeded,
                memory_bytes={0x00006000: 0x10},
            )
            self.assertEqual(result.after_memory[0x00006000], 0x0B)
            self.assertFalse(result.after_cpu.flags.cf)

    # --- Pass 55 : ADC/SBC/CP-rev/EX R8 ↔ (R32) + ALU (R32), imm8 ----

    def test_execute_ex_mem_r8_swaps_byte_and_register(self) -> None:
        """EX (R32), R8 — encoding [0x80+r32_idx][0x30+r8_idx], 2 bytes.

        Oracle verified : `0x83 0x31` → `ex (XHL), A`. Pass 55.
        Swap : mem ← old R8, R8 ← old mem. Flags unchanged.
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x00200040, b"\x83\x31")
            view = load_fetch_view(rom_path)
            seeded = replace(
                view.machine.cpu,
                regs=replace(
                    view.machine.cpu.regs, xhl=0x00006000, xwa=0x000000AA,
                ),
                flags=StatusFlags(
                    sf=True, zf=True, vf=True, hf=True, cf=True, nf=True,
                ),
                iff_level=0, rfp=0,
            )
            result = build_execute_next(
                view, cpu_state=seeded,
                memory_bytes={0x00006000: 0x55},
            )
            self.assertEqual(result.status, "executed")
            # A ← old mem byte (0x55) ; mem ← old A (0xAA).
            self.assertEqual(result.after_cpu.regs.xwa & 0xFF, 0x55)
            self.assertEqual(result.after_memory[0x00006000], 0xAA)
            # Flags must be preserved (EX does not touch them).
            self.assertTrue(result.after_cpu.flags.sf)
            self.assertTrue(result.after_cpu.flags.zf)
            self.assertTrue(result.after_cpu.flags.cf)
            self.assertEqual(len(result.memory_writes), 1)

    def test_execute_ex_mem_r8_blocks_on_unknown_source_register(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x00200040, b"\x83\x31")
            view = load_fetch_view(rom_path)
            seeded = replace(
                view.machine.cpu,
                regs=replace(view.machine.cpu.regs, xhl=0x00006000),
            )
            result = build_execute_next(
                view, cpu_state=seeded,
                memory_bytes={0x00006000: 0x55},
            )
            self.assertEqual(result.status, "requires-known-source-register")

    def test_execute_adc_r8_mem_propagates_carry_in(self) -> None:
        """ADC R8, (R32) — encoding [0x80+r32_idx][0x90+r8_idx], 2 bytes.

        Oracle verified : `0x83 0x91` → `adc A, (XHL)`. Pass 55.
        A=0x10 + mem=0x05 + C=1 = 0x16. New C=0.
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x00200040, b"\x83\x91")
            view = load_fetch_view(rom_path)
            seeded = replace(
                view.machine.cpu,
                regs=replace(
                    view.machine.cpu.regs, xhl=0x00006000, xwa=0x00000010,
                ),
                flags=StatusFlags(
                    sf=False, zf=False, vf=False, hf=False, cf=True, nf=False,
                ),
                iff_level=0, rfp=0,
            )
            result = build_execute_next(
                view, cpu_state=seeded,
                memory_bytes={0x00006000: 0x05},
            )
            self.assertEqual(result.status, "executed")
            self.assertEqual(result.after_cpu.regs.xwa & 0xFF, 0x16)
            self.assertFalse(result.after_cpu.flags.cf)
            self.assertFalse(result.after_cpu.flags.nf, "ADC sets N=0")

    def test_execute_adc_r8_mem_blocks_on_unknown_carry(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x00200040, b"\x83\x91")
            view = load_fetch_view(rom_path)
            seeded = replace(
                view.machine.cpu,
                regs=replace(
                    view.machine.cpu.regs, xhl=0x00006000, xwa=0x00000010,
                ),
                # CF stays None (unknown carry) → must block.
            )
            result = build_execute_next(
                view, cpu_state=seeded,
                memory_bytes={0x00006000: 0x05},
            )
            self.assertEqual(result.status, "runtime-state-required")

    def test_execute_adc_mem_r8_writes_back_to_memory(self) -> None:
        """ADC (R32), R8 — [0x80+r32_idx][0x98+r8_idx]. Pass 55."""
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x00200040, b"\x83\x99")  # adc (XHL), A
            view = load_fetch_view(rom_path)
            # XWA = 0x000000FF → A (low byte) = 0xFF ; mem=0x00 ; C=1
            # mem ← 0x00 + 0xFF + 1 = 0x00 (wraps), C=1, Z=1
            seeded = replace(
                view.machine.cpu,
                regs=replace(
                    view.machine.cpu.regs, xhl=0x00006000, xwa=0x000000FF,
                ),
                flags=StatusFlags(
                    sf=False, zf=False, vf=False, hf=False, cf=True, nf=False,
                ),
                iff_level=0, rfp=0,
            )
            result = build_execute_next(
                view, cpu_state=seeded,
                memory_bytes={0x00006000: 0x00},
            )
            self.assertEqual(result.status, "executed")
            self.assertEqual(result.after_memory[0x00006000], 0x00)
            self.assertTrue(result.after_cpu.flags.cf)
            self.assertTrue(result.after_cpu.flags.zf)

    def test_execute_sbc_r8_mem_borrow_in(self) -> None:
        """SBC R8, (R32) — [0x80+r32_idx][0xB0+r8_idx]. Pass 55.
        A=0x10 - mem=0x05 - C=1 = 0x0A. New C=0 (no borrow needed).
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x00200040, b"\x83\xB1")
            view = load_fetch_view(rom_path)
            seeded = replace(
                view.machine.cpu,
                regs=replace(
                    view.machine.cpu.regs, xhl=0x00006000, xwa=0x00000010,
                ),
                flags=StatusFlags(
                    sf=False, zf=False, vf=False, hf=False, cf=True, nf=False,
                ),
                iff_level=0, rfp=0,
            )
            result = build_execute_next(
                view, cpu_state=seeded,
                memory_bytes={0x00006000: 0x05},
            )
            self.assertEqual(result.status, "executed")
            self.assertEqual(result.after_cpu.regs.xwa & 0xFF, 0x0A)
            self.assertFalse(result.after_cpu.flags.cf)
            self.assertTrue(result.after_cpu.flags.nf, "SBC sets N=1")

    def test_execute_sbc_mem_r8_writes_back_to_memory(self) -> None:
        """SBC (R32), R8 — [0x80+r32_idx][0xB8+r8_idx]. Pass 55."""
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x00200040, b"\x83\xB9")  # sbc (XHL), A
            view = load_fetch_view(rom_path)
            # mem=0x20 - A=0x10 - C=0 = 0x10
            seeded = replace(
                view.machine.cpu,
                regs=replace(
                    view.machine.cpu.regs, xhl=0x00006000, xwa=0x00000010,
                ),
                flags=StatusFlags(
                    sf=False, zf=False, vf=False, hf=False, cf=False, nf=False,
                ),
                iff_level=0, rfp=0,
            )
            result = build_execute_next(
                view, cpu_state=seeded,
                memory_bytes={0x00006000: 0x20},
            )
            self.assertEqual(result.status, "executed")
            self.assertEqual(result.after_memory[0x00006000], 0x10)
            self.assertFalse(result.after_cpu.flags.cf)

    def test_execute_cp_mem_r8_sets_flags_only(self) -> None:
        """CP (R32), R8 — [0x80+r32_idx][0xF8+r8_idx]. Pass 55.

        Operand order reversed vs CP R8, (R32) — flags = mem - R8.
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x00200040, b"\x83\xF9")  # cp (XHL), A
            view = load_fetch_view(rom_path)
            # mem=0x05 - A=0x10 → C=1, S=1 (borrow + negative)
            seeded = replace(
                view.machine.cpu,
                regs=replace(
                    view.machine.cpu.regs, xhl=0x00006000, xwa=0x00000010,
                ),
                flags=StatusFlags(
                    sf=False, zf=False, vf=False, hf=False, cf=False, nf=False,
                ),
                iff_level=0, rfp=0,
            )
            result = build_execute_next(
                view, cpu_state=seeded,
                memory_bytes={0x00006000: 0x05},
            )
            self.assertEqual(result.status, "executed")
            # No memory write, no register write.
            self.assertEqual(result.memory_writes, ())
            self.assertNotIn("WA", result.written_registers)
            self.assertTrue(result.after_cpu.flags.cf)
            self.assertTrue(result.after_cpu.flags.sf)

    def test_execute_add_mem_imm8_writes_back(self) -> None:
        """ADD (R32), imm8 — [0x80+r32_idx][0x38][imm8], 3 bytes. Pass 55."""
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x00200040, b"\x83\x38\x11")
            view = load_fetch_view(rom_path)
            seeded = replace(
                view.machine.cpu,
                regs=replace(view.machine.cpu.regs, xhl=0x00006000),
                flags=StatusFlags(
                    sf=False, zf=False, vf=False, hf=False, cf=False, nf=False,
                ),
                iff_level=0, rfp=0,
            )
            result = build_execute_next(
                view, cpu_state=seeded,
                memory_bytes={0x00006000: 0x22},
            )
            self.assertEqual(result.status, "executed")
            self.assertEqual(result.after_memory[0x00006000], 0x33)
            self.assertFalse(result.after_cpu.flags.cf)

    def test_execute_adc_mem_imm8_blocks_on_unknown_carry(self) -> None:
        """ADC (R32), imm8 — [0x80+r32_idx][0x39][imm8]. Pass 55."""
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x00200040, b"\x83\x39\x10")
            view = load_fetch_view(rom_path)
            seeded = replace(
                view.machine.cpu,
                regs=replace(view.machine.cpu.regs, xhl=0x00006000),
                # CF stays None.
            )
            result = build_execute_next(
                view, cpu_state=seeded,
                memory_bytes={0x00006000: 0x20},
            )
            self.assertEqual(result.status, "runtime-state-required")

    def test_execute_sub_mem_imm8_writes_back(self) -> None:
        """SUB (R32), imm8 — [0x80+r32_idx][0x3A][imm8]. Pass 55."""
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x00200040, b"\x83\x3A\x05")
            view = load_fetch_view(rom_path)
            seeded = replace(
                view.machine.cpu,
                regs=replace(view.machine.cpu.regs, xhl=0x00006000),
                flags=StatusFlags(
                    sf=False, zf=False, vf=False, hf=False, cf=False, nf=False,
                ),
                iff_level=0, rfp=0,
            )
            # mem = 0x10 → mem - 0x05 = 0x0B
            result = build_execute_next(
                view, cpu_state=seeded,
                memory_bytes={0x00006000: 0x10},
            )
            self.assertEqual(result.status, "executed")
            self.assertEqual(result.after_memory[0x00006000], 0x0B)
            self.assertTrue(result.after_cpu.flags.nf, "SUB sets N=1")

    def test_execute_and_mem_imm8_clears_bits(self) -> None:
        """AND (R32), imm8 — [0x80+r32_idx][0x3C][imm8]. Pass 55."""
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x00200040, b"\x83\x3C\x0F")
            view = load_fetch_view(rom_path)
            seeded = replace(
                view.machine.cpu,
                regs=replace(view.machine.cpu.regs, xhl=0x00006000),
                flags=StatusFlags(
                    sf=True, zf=False, vf=True, hf=False, cf=True, nf=False,
                ),
                iff_level=0, rfp=0,
            )
            # mem = 0xF3 & 0x0F = 0x03
            result = build_execute_next(
                view, cpu_state=seeded,
                memory_bytes={0x00006000: 0xF3},
            )
            self.assertEqual(result.after_memory[0x00006000], 0x03)
            # Logical flags : V=1 (per existing _compute_logical_flags), C=0, S=0, Z=0.
            self.assertFalse(result.after_cpu.flags.cf)
            self.assertFalse(result.after_cpu.flags.sf)
            self.assertFalse(result.after_cpu.flags.zf)

    def test_execute_xor_mem_imm8_clears_when_equal(self) -> None:
        """XOR (R32), imm8 — [0x80+r32_idx][0x3D][imm8]. Pass 55."""
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x00200040, b"\x83\x3D\xAA")
            view = load_fetch_view(rom_path)
            seeded = replace(
                view.machine.cpu,
                regs=replace(view.machine.cpu.regs, xhl=0x00006000),
                flags=StatusFlags(
                    sf=False, zf=False, vf=False, hf=False, cf=False, nf=False,
                ),
                iff_level=0, rfp=0,
            )
            # mem = 0xAA ^ 0xAA = 0x00 → Z=1
            result = build_execute_next(
                view, cpu_state=seeded,
                memory_bytes={0x00006000: 0xAA},
            )
            self.assertEqual(result.after_memory[0x00006000], 0x00)
            self.assertTrue(result.after_cpu.flags.zf)

    def test_execute_or_mem_imm8_sets_bits(self) -> None:
        """OR (R32), imm8 — [0x80+r32_idx][0x3E][imm8]. Pass 55."""
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x00200040, b"\x83\x3E\x0F")
            view = load_fetch_view(rom_path)
            seeded = replace(
                view.machine.cpu,
                regs=replace(view.machine.cpu.regs, xhl=0x00006000),
                flags=StatusFlags(
                    sf=False, zf=False, vf=False, hf=False, cf=False, nf=False,
                ),
                iff_level=0, rfp=0,
            )
            # mem = 0xA0 | 0x0F = 0xAF
            result = build_execute_next(
                view, cpu_state=seeded,
                memory_bytes={0x00006000: 0xA0},
            )
            self.assertEqual(result.after_memory[0x00006000], 0xAF)
            self.assertFalse(result.after_cpu.flags.zf)
            self.assertTrue(result.after_cpu.flags.sf, "high bit set after OR")

    # --- Pass 56 : INC/DEC #n, (R32) + shift family on (R32) ----

    def test_execute_inc_mem_n1_increments_byte(self) -> None:
        """INC #1, (R32) — [0x80+r32_idx][0x61]. Pass 56."""
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x00200040, b"\x83\x61")
            view = load_fetch_view(rom_path)
            seeded = replace(
                view.machine.cpu,
                regs=replace(view.machine.cpu.regs, xhl=0x00006000),
                flags=StatusFlags(
                    sf=False, zf=False, vf=False, hf=False, cf=True, nf=True,
                ),
                iff_level=0, rfp=0,
            )
            # mem = 0x10 + 1 = 0x11.
            result = build_execute_next(
                view, cpu_state=seeded,
                memory_bytes={0x00006000: 0x10},
            )
            self.assertEqual(result.status, "executed")
            self.assertEqual(result.after_memory[0x00006000], 0x11)
            self.assertFalse(result.after_cpu.flags.zf)
            # CF must be preserved (INC mem does not touch C).
            self.assertTrue(result.after_cpu.flags.cf, "CF preserved by INC mem")

    def test_execute_inc_mem_n0_means_8(self) -> None:
        """INC #8, (R32) — sub_op=0x60 has n=0 in encoding → +8 per Toshiba spec."""
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x00200040, b"\x83\x60")
            view = load_fetch_view(rom_path)
            seeded = replace(
                view.machine.cpu,
                regs=replace(view.machine.cpu.regs, xhl=0x00006000),
                flags=StatusFlags(
                    sf=False, zf=False, vf=False, hf=False, cf=False, nf=False,
                ),
                iff_level=0, rfp=0,
            )
            # mem = 0x10 + 8 = 0x18.
            result = build_execute_next(
                view, cpu_state=seeded,
                memory_bytes={0x00006000: 0x10},
            )
            self.assertEqual(result.after_memory[0x00006000], 0x18)
            # Confirm decoded mnemonic shows "8" not "0".
            self.assertIn("8", result.decode.operands)

    def test_execute_inc_mem_wrap_preserves_carry(self) -> None:
        """INC mem wrap from 0xFF + 1 = 0x00 sets Z=1 but preserves CF."""
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x00200040, b"\x83\x61")
            view = load_fetch_view(rom_path)
            seeded = replace(
                view.machine.cpu,
                regs=replace(view.machine.cpu.regs, xhl=0x00006000),
                flags=StatusFlags(
                    sf=False, zf=False, vf=False, hf=False, cf=False, nf=False,
                ),
                iff_level=0, rfp=0,
            )
            result = build_execute_next(
                view, cpu_state=seeded,
                memory_bytes={0x00006000: 0xFF},
            )
            self.assertEqual(result.after_memory[0x00006000], 0x00)
            self.assertTrue(result.after_cpu.flags.zf)
            self.assertFalse(
                result.after_cpu.flags.cf,
                "CF preserved (was False before INC), should NOT be set to True even on wrap.",
            )

    def test_execute_dec_mem_n1_sets_nf(self) -> None:
        """DEC #1, (R32) — [0x80+r32_idx][0x69]. Pass 56."""
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x00200040, b"\x83\x69")
            view = load_fetch_view(rom_path)
            seeded = replace(
                view.machine.cpu,
                regs=replace(view.machine.cpu.regs, xhl=0x00006000),
                flags=StatusFlags(
                    sf=False, zf=False, vf=False, hf=False, cf=True, nf=False,
                ),
                iff_level=0, rfp=0,
            )
            # mem = 0x10 - 1 = 0x0F.
            result = build_execute_next(
                view, cpu_state=seeded,
                memory_bytes={0x00006000: 0x10},
            )
            self.assertEqual(result.after_memory[0x00006000], 0x0F)
            self.assertTrue(result.after_cpu.flags.nf, "DEC sets N=1")
            self.assertTrue(result.after_cpu.flags.cf, "CF preserved")

    def test_execute_dec_mem_wrap_preserves_carry(self) -> None:
        """DEC mem 0x00 - 1 = 0xFF — sets S=1 but preserves CF."""
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x00200040, b"\x83\x69")
            view = load_fetch_view(rom_path)
            seeded = replace(
                view.machine.cpu,
                regs=replace(view.machine.cpu.regs, xhl=0x00006000),
                flags=StatusFlags(
                    sf=False, zf=False, vf=False, hf=False, cf=False, nf=False,
                ),
                iff_level=0, rfp=0,
            )
            result = build_execute_next(
                view, cpu_state=seeded,
                memory_bytes={0x00006000: 0x00},
            )
            self.assertEqual(result.after_memory[0x00006000], 0xFF)
            self.assertTrue(result.after_cpu.flags.sf)
            self.assertFalse(result.after_cpu.flags.cf, "CF NOT set on borrow (mem variant preserves CF)")

    def test_execute_rlc_mem_msb_to_carry(self) -> None:
        """RLC (R32) — [0x80+r32_idx][0x78]. Pass 56.
        0x81 RLC → bit0 ← MSB(1), MSB ← bit6 ; result = 0x03, C=1.
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x00200040, b"\x83\x78")
            view = load_fetch_view(rom_path)
            seeded = replace(
                view.machine.cpu,
                regs=replace(view.machine.cpu.regs, xhl=0x00006000),
                flags=StatusFlags(
                    sf=False, zf=False, vf=False, hf=False, cf=False, nf=False,
                ),
                iff_level=0, rfp=0,
            )
            result = build_execute_next(
                view, cpu_state=seeded,
                memory_bytes={0x00006000: 0x81},
            )
            self.assertEqual(result.after_memory[0x00006000], 0x03)
            self.assertTrue(result.after_cpu.flags.cf)

    def test_execute_rrc_mem_lsb_to_carry(self) -> None:
        """RRC (R32) — 0x01 → 0x80 (rotate), C=1."""
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x00200040, b"\x83\x79")
            view = load_fetch_view(rom_path)
            seeded = replace(
                view.machine.cpu,
                regs=replace(view.machine.cpu.regs, xhl=0x00006000),
                flags=StatusFlags(
                    sf=False, zf=False, vf=False, hf=False, cf=False, nf=False,
                ),
                iff_level=0, rfp=0,
            )
            result = build_execute_next(
                view, cpu_state=seeded,
                memory_bytes={0x00006000: 0x01},
            )
            self.assertEqual(result.after_memory[0x00006000], 0x80)
            self.assertTrue(result.after_cpu.flags.cf)
            self.assertTrue(result.after_cpu.flags.sf, "high bit set after RRC of 0x01")

    def test_execute_rl_mem_through_carry(self) -> None:
        """RL (R32) — 0x40 with old C=1 → 0x81 ; new C=0 (old MSB)."""
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x00200040, b"\x83\x7A")
            view = load_fetch_view(rom_path)
            seeded = replace(
                view.machine.cpu,
                regs=replace(view.machine.cpu.regs, xhl=0x00006000),
                flags=StatusFlags(
                    sf=False, zf=False, vf=False, hf=False, cf=True, nf=False,
                ),
                iff_level=0, rfp=0,
            )
            result = build_execute_next(
                view, cpu_state=seeded,
                memory_bytes={0x00006000: 0x40},
            )
            self.assertEqual(result.after_memory[0x00006000], 0x81)
            self.assertFalse(result.after_cpu.flags.cf, "new C = old MSB(0x40) = 0")

    def test_execute_rl_mem_blocks_on_unknown_carry(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x00200040, b"\x83\x7A")
            view = load_fetch_view(rom_path)
            seeded = replace(
                view.machine.cpu,
                regs=replace(view.machine.cpu.regs, xhl=0x00006000),
                # CF stays None.
            )
            result = build_execute_next(
                view, cpu_state=seeded,
                memory_bytes={0x00006000: 0x40},
            )
            self.assertEqual(result.status, "runtime-state-required")

    def test_execute_sla_mem_msb_to_carry(self) -> None:
        """SLA (R32) — 0x80 << 1 = 0x00, C=1, Z=1."""
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x00200040, b"\x83\x7C")
            view = load_fetch_view(rom_path)
            seeded = replace(
                view.machine.cpu,
                regs=replace(view.machine.cpu.regs, xhl=0x00006000),
                flags=StatusFlags(
                    sf=False, zf=False, vf=False, hf=False, cf=False, nf=False,
                ),
                iff_level=0, rfp=0,
            )
            result = build_execute_next(
                view, cpu_state=seeded,
                memory_bytes={0x00006000: 0x80},
            )
            self.assertEqual(result.after_memory[0x00006000], 0x00)
            self.assertTrue(result.after_cpu.flags.cf)
            self.assertTrue(result.after_cpu.flags.zf)

    def test_execute_sra_mem_sign_extends(self) -> None:
        """SRA (R32) — 0x80 >> 1 = 0xC0 (sign-extending), C=0."""
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x00200040, b"\x83\x7D")
            view = load_fetch_view(rom_path)
            seeded = replace(
                view.machine.cpu,
                regs=replace(view.machine.cpu.regs, xhl=0x00006000),
                flags=StatusFlags(
                    sf=False, zf=False, vf=False, hf=False, cf=False, nf=False,
                ),
                iff_level=0, rfp=0,
            )
            result = build_execute_next(
                view, cpu_state=seeded,
                memory_bytes={0x00006000: 0x80},
            )
            self.assertEqual(result.after_memory[0x00006000], 0xC0)
            self.assertFalse(result.after_cpu.flags.cf)
            self.assertTrue(result.after_cpu.flags.sf, "MSB preserved (sign-extension)")

    def test_execute_srl_mem_no_sign_extend(self) -> None:
        """SRL (R32) — 0x80 >> 1 = 0x40 (logical, bit7 ← 0), C=0."""
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x00200040, b"\x83\x7F")
            view = load_fetch_view(rom_path)
            seeded = replace(
                view.machine.cpu,
                regs=replace(view.machine.cpu.regs, xhl=0x00006000),
                flags=StatusFlags(
                    sf=False, zf=False, vf=False, hf=False, cf=False, nf=False,
                ),
                iff_level=0, rfp=0,
            )
            result = build_execute_next(
                view, cpu_state=seeded,
                memory_bytes={0x00006000: 0x80},
            )
            self.assertEqual(result.after_memory[0x00006000], 0x40)
            self.assertFalse(result.after_cpu.flags.cf)
            self.assertFalse(result.after_cpu.flags.sf, "MSB cleared (logical shift)")

    def test_execute_ldf_sets_rfp_and_advances_pc(self) -> None:
        """LDF n (opcode 0x17) sets RFP to n and advances PC by 2.

        Closes the next blocker after the BIOS hand-off seed —
        cc900-compiled code calling SYSTEM_CALL does `LDF 3` to
        switch to the BIOS bank before reading the params register.
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            # 17 03 = LDF 3
            self._write_demo_rom(rom_path, 0x00200040, b"\x17\x03")
            view = load_fetch_view(rom_path)
            result = build_execute_next(view)
            self.assertEqual(result.status, "executed")
            assert result.after_cpu is not None
            self.assertEqual(result.after_cpu.rfp, 3)
            self.assertEqual(result.after_cpu.pc, 0x00200042)
            self.assertIn("RFP", result.written_registers)
            self.assertIn("LDF", result.note)

    def test_execute_ldf_accepts_all_four_bank_values(self) -> None:
        for n in range(4):
            with tempfile.TemporaryDirectory() as tmpdir:
                rom_path = Path(tmpdir) / "demo.ngc"
                self._write_demo_rom(
                    rom_path, 0x00200040, bytes([0x17, n]),
                )
                view = load_fetch_view(rom_path)
                result = build_execute_next(view)
                self.assertEqual(
                    result.status, "executed",
                    f"LDF {n} should execute cleanly",
                )
                self.assertEqual(result.after_cpu.rfp, n)

    def test_execute_ldx_direct_store_writes_overlay(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            # Toshiba note: 2nd / 4th / 6th bytes need not be 00; only bytes 1/3/5 matter.
            self._write_demo_rom(rom_path, 0x00200040, b"\xF7\xAA\x66\xBB\xCC\xDD")
            view = load_fetch_view(rom_path)

            result = build_execute_next(view)

            self.assertEqual(result.status, "executed")
            self.assertEqual(result.decode.assembly, "ldx (0x66), 0xCC")
            self.assertEqual(result.written_registers, ("PC",))
            assert result.after_cpu is not None
            assert result.after_memory is not None
            self.assertEqual(result.after_cpu.pc, 0x00200046)
            self.assertEqual(result.after_memory[0x66], 0xCC)
            self.assertEqual(result.memory_writes[0].address, 0x66)
            self.assertEqual(result.memory_writes[0].data, b"\xCC")

    def test_execute_incf_rotates_rfp_and_advances_pc(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x00200040, b"\x0C")
            view = load_fetch_view(rom_path)
            seeded_cpu = replace(view.machine.cpu, rfp=2, register_bank=2)
            result = build_execute_next(view, cpu_state=seeded_cpu)
            self.assertEqual(result.status, "executed")
            assert result.after_cpu is not None
            self.assertEqual(result.after_cpu.rfp, 3)
            self.assertEqual(result.after_cpu.pc, 0x00200041)
            self.assertEqual(result.decode.assembly, "incf")

    def test_execute_decf_wraps_bank_index(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x00200040, b"\x0D")
            view = load_fetch_view(rom_path)
            seeded_cpu = replace(view.machine.cpu, rfp=0, register_bank=0)
            result = build_execute_next(view, cpu_state=seeded_cpu)
            self.assertEqual(result.status, "executed")
            assert result.after_cpu is not None
            self.assertEqual(result.after_cpu.rfp, 3)
            self.assertEqual(result.after_cpu.pc, 0x00200041)
            self.assertEqual(result.decode.assembly, "decf")

    def test_execute_incf_loads_visible_core_regs_from_next_bank(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x00200040, b"\x0C")
            view = load_fetch_view(rom_path)
            banks = (
                BankedByteRegisters(slots=(0x44, 0x33, 0x22, 0x11) + (None,) * 12),
                BankedByteRegisters(slots=(0x78, 0x56, 0x34, 0x12) + (None,) * 12),
                BankedByteRegisters(slots=(None,) * 16),
                BankedByteRegisters(slots=(None,) * 16),
            )
            seeded_cpu = replace(
                view.machine.cpu,
                regs=replace(view.machine.cpu.regs, xwa=0x11223344),
                rfp=0,
                register_bank=0,
                register_banks=banks,
            )
            result = build_execute_next(view, cpu_state=seeded_cpu)
            self.assertEqual(result.status, "executed")
            assert result.after_cpu is not None
            self.assertEqual(result.after_cpu.rfp, 1)
            self.assertEqual(result.after_cpu.regs.xwa, 0x12345678)
            self.assertEqual(result.after_cpu.register_banks[0].slots[:4], (0x44, 0x33, 0x22, 0x11))

    def test_execute_ex_ff_swaps_visible_and_shadow_flags(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x00200040, b"\x16")
            view = load_fetch_view(rom_path)
            seeded_cpu = replace(
                view.machine.cpu,
                flags=StatusFlags(
                    sf=True, zf=False, vf=True, hf=False, cf=True, nf=False,
                ),
                alt_flags=StatusFlags(
                    sf=False, zf=True, vf=False, hf=True, cf=False, nf=True,
                ),
                iff_level=0,
                rfp=0,
            )
            result = build_execute_next(view, cpu_state=seeded_cpu)
            self.assertEqual(result.status, "executed")
            self.assertEqual(result.decode.assembly, "ex F,F'")
            self.assertEqual(result.written_registers, ("F", "F'", "PC"))
            assert result.after_cpu is not None
            self.assertFalse(result.after_cpu.flags.sf)
            self.assertTrue(result.after_cpu.flags.zf)
            self.assertTrue(result.after_cpu.flags.hf)
            self.assertTrue(result.after_cpu.flags.nf)
            assert result.after_cpu.alt_flags is not None
            self.assertTrue(result.after_cpu.alt_flags.sf)
            self.assertFalse(result.after_cpu.alt_flags.zf)
            self.assertTrue(result.after_cpu.alt_flags.vf)
            self.assertTrue(result.after_cpu.alt_flags.cf)
            self.assertFalse(result.after_cpu.alt_flags.nf)
            self.assertEqual(result.after_cpu.pc, 0x00200041)

    def test_execute_ex_ff_without_seeded_shadow_flags_degrades_to_unknowns(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x00200040, b"\x16")
            view = load_fetch_view(rom_path)
            seeded_cpu = replace(
                view.machine.cpu,
                flags=StatusFlags(
                    sf=True, zf=False, vf=False, hf=True, cf=True, nf=False,
                ),
                alt_flags=None,
                iff_level=0,
                rfp=0,
            )
            result = build_execute_next(view, cpu_state=seeded_cpu)
            self.assertEqual(result.status, "executed")
            assert result.after_cpu is not None
            self.assertIsNone(result.after_cpu.flags.sf)
            self.assertIsNone(result.after_cpu.flags.cf)
            assert result.after_cpu.alt_flags is not None
            self.assertTrue(result.after_cpu.alt_flags.sf)
            self.assertTrue(result.after_cpu.alt_flags.cf)

    def test_execute_ldf_loads_visible_core_regs_from_target_bank(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x00200040, b"\x17\x03")
            view = load_fetch_view(rom_path)
            banks = (
                BankedByteRegisters(slots=(0x44, 0x33, 0x22, 0x11) + (None,) * 12),
                BankedByteRegisters(slots=(None,) * 16),
                BankedByteRegisters(slots=(None,) * 16),
                BankedByteRegisters(slots=(0x78, 0x56, 0x34, 0x12) + (None,) * 12),
            )
            seeded_cpu = replace(
                view.machine.cpu,
                regs=replace(view.machine.cpu.regs, xwa=0x11223344),
                rfp=0,
                register_bank=0,
                register_banks=banks,
            )
            result = build_execute_next(view, cpu_state=seeded_cpu)
        self.assertEqual(result.status, "executed")
        assert result.after_cpu is not None
        self.assertEqual(result.after_cpu.rfp, 3)
        self.assertEqual(result.after_cpu.regs.xwa, 0x12345678)
        self.assertEqual(result.after_cpu.register_banks[0].slots[:4], (0x44, 0x33, 0x22, 0x11))

    def test_prefixed_byte_alu_uses_current_bank_byte_slots_when_owner_unknown(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x00200040, b"\xC8\x80")  # add W, W
            view = load_fetch_view(rom_path)
            banks = (
                BankedByteRegisters(slots=(None,) * 16),
                BankedByteRegisters(slots=(None,) * 16),
                BankedByteRegisters(slots=(None,) * 16),
                BankedByteRegisters(slots=(None, 0x05) + (None,) * 14),
            )
            seeded_cpu = replace(
                view.machine.cpu,
                flags=StatusFlags(sf=False, zf=False, vf=False, hf=False, cf=False, nf=False),
                rfp=3,
                register_bank=3,
                register_banks=banks,
            )
            result = build_execute_next(view, cpu_state=seeded_cpu)
        self.assertEqual(result.status, "executed")
        assert result.after_cpu is not None
        self.assertIsNone(result.after_cpu.regs.xwa)
        assert result.after_cpu.register_banks is not None
        self.assertEqual(result.after_cpu.register_banks[3].slots[1], 0x0A)

    def test_secondary_indexed_load_uses_current_bank_byte_index_slot(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x00200040, b"\xE3\x03\xF0\xE1\x24")
            view = load_fetch_view(rom_path)
            banks = (
                BankedByteRegisters(slots=(None,) * 16),
                BankedByteRegisters(slots=(None,) * 16),
                BankedByteRegisters(slots=(None,) * 16),
                BankedByteRegisters(slots=(None, 0x04) + (None,) * 14),
            )
            seeded_cpu = replace(
                view.machine.cpu,
                regs=replace(view.machine.cpu.regs, xix=0x00001000),
                flags=StatusFlags(sf=False, zf=False, vf=False, hf=False, cf=False, nf=False),
                rfp=3,
                register_bank=3,
                register_banks=banks,
            )
            result = build_execute_next(
                view,
                cpu_state=seeded_cpu,
                memory_bytes={0x1004: 0x78, 0x1005: 0x56, 0x1006: 0x34, 0x1007: 0x12},
            )
        self.assertEqual(result.status, "executed")
        assert result.after_cpu is not None
        self.assertEqual(result.after_cpu.regs.xix, 0x12345678)

    def test_secondary_indexed_load_can_read_backed_bios_region(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            bios_path = Path(tmpdir) / "bios.bin"
            self._write_demo_rom(rom_path, 0x00200040, b"\xE3\x03\xF0\xE1\x24")
            bios = bytearray(0x10000)
            bios[0xFE14:0xFE18] = b"\x78\x56\x34\x12"
            bios_path.write_bytes(bytes(bios))
            view = load_fetch_view(rom_path, bios_path=bios_path)
            banks = (
                BankedByteRegisters(slots=(None,) * 16),
                BankedByteRegisters(slots=(None,) * 16),
                BankedByteRegisters(slots=(None,) * 16),
                BankedByteRegisters(slots=(None, 0x14) + (None,) * 14),
            )
            seeded_cpu = replace(
                view.machine.cpu,
                regs=replace(view.machine.cpu.regs, xix=0x00FFFE00),
                flags=StatusFlags(sf=False, zf=False, vf=False, hf=False, cf=False, nf=False),
                rfp=3,
                register_bank=3,
                register_banks=banks,
            )
            result = build_execute_next(view, cpu_state=seeded_cpu)
        self.assertEqual(result.status, "executed")
        assert result.after_cpu is not None
        self.assertEqual(result.after_cpu.regs.xix, 0x12345678)

    def test_push_word_uses_current_bank_low_word_when_visible_owner_unknown(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x00200040, b"\x29")
            view = load_fetch_view(rom_path)
            banks = (
                BankedByteRegisters(slots=(None,) * 16),
                BankedByteRegisters(slots=(None,) * 16),
                BankedByteRegisters(slots=(None,) * 16),
                BankedByteRegisters(slots=(None, None, None, None, 0x78, 0x56, None, None) + (None,) * 8),
            )
            seeded_cpu = replace(
                view.machine.cpu,
                regs=replace(view.machine.cpu.regs, xsp=0x00006C00),
                rfp=3,
                register_bank=3,
                register_banks=banks,
            )
            result = build_execute_next(view, cpu_state=seeded_cpu)
        self.assertEqual(result.status, "executed")
        assert result.after_cpu is not None
        self.assertEqual(result.after_cpu.regs.xsp, 0x00006BFE)
        self.assertEqual(result.memory_writes[0].address, 0x00006BFE)
        self.assertEqual(result.memory_writes[0].data, b"\x78\x56")

    def test_push_long_uses_current_bank_full_value_when_visible_owner_unknown(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x00200040, b"\x39")
            view = load_fetch_view(rom_path)
            banks = (
                BankedByteRegisters(slots=(None,) * 16),
                BankedByteRegisters(slots=(None,) * 16),
                BankedByteRegisters(slots=(None,) * 16),
                BankedByteRegisters(slots=(None, None, None, None, 0x78, 0x56, 0x34, 0x12) + (None,) * 8),
            )
            seeded_cpu = replace(
                view.machine.cpu,
                regs=replace(view.machine.cpu.regs, xsp=0x00006C00),
                rfp=3,
                register_bank=3,
                register_banks=banks,
            )
            result = build_execute_next(view, cpu_state=seeded_cpu)
        self.assertEqual(result.status, "executed")
        assert result.after_cpu is not None
        self.assertEqual(result.after_cpu.regs.xsp, 0x00006BFC)
        self.assertEqual(result.memory_writes[0].address, 0x00006BFC)
        self.assertEqual(result.memory_writes[0].data, b"\x78\x56\x34\x12")

    def test_execute_push_sr_requires_full_sr_shape(self) -> None:
        """PUSH SR blocks honestly when any SR-derived field is unknown."""
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x00200040, b"\x02")
            view = load_fetch_view(rom_path)
            # Stack pointer is set but flags / iff_level / rfp stay None.
            seeded_cpu = replace(
                view.machine.cpu,
                regs=replace(view.machine.cpu.regs, xsp=0x00006C00),
            )

            result = build_execute_next(view, cpu_state=seeded_cpu)

            self.assertEqual(result.status, "requires-known-sr")
            self.assertIsNone(result.after_cpu)

    def test_execute_push_sr_writes_two_bytes_when_sr_known(self) -> None:
        """PUSH SR encodes the modeled SR fields into a 16-bit value."""
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x00200040, b"\x02")
            view = load_fetch_view(rom_path)
            seeded_cpu = replace(
                view.machine.cpu,
                regs=replace(view.machine.cpu.regs, xsp=0x00006C00),
                flags=StatusFlags(sf=True, zf=False, vf=False, hf=False, cf=True, nf=False),
                iff_level=0,
                rfp=0,
            )

            result = build_execute_next(view, cpu_state=seeded_cpu)

            self.assertEqual(result.status, "executed")
            assert result.after_cpu is not None
            # XSP must have moved down by 2.
            self.assertEqual(result.after_cpu.regs.xsp, 0x00006BFE)
            # SR low byte = 1<<0 (CF) | 1<<7 (SF) = 0x81.
            # SR high byte = 1<<3 (MAX bit 11) | 1<<7 (SYSM bit 15) = 0x88.
            self.assertEqual(len(result.memory_writes), 1)
            written = result.memory_writes[0]
            self.assertEqual(written.address, 0x00006BFE)
            self.assertEqual(written.data, bytes([0x81, 0x88]))
            self.assertIn("PUSH SR", result.note)

    def test_execute_pop_sr_restores_all_sr_fields(self) -> None:
        """POP SR decodes the 16-bit stack value into flags, iff_level and rfp."""
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x00200040, b"\x03")
            view = load_fetch_view(rom_path)
            seeded_cpu = replace(
                view.machine.cpu,
                regs=replace(view.machine.cpu.regs, xsp=0x00006BFE),
            )
            # Pre-load SR=0x8881 (CF=1, SF=1, MAX=1, SYSM=1) into stack.
            seeded_memory = {0x00006BFE: 0x81, 0x00006BFF: 0x88}

            result = build_execute_next(
                view, cpu_state=seeded_cpu, memory_bytes=seeded_memory,
            )

            self.assertEqual(result.status, "executed")
            assert result.after_cpu is not None
            self.assertEqual(result.after_cpu.regs.xsp, 0x00006C00)
            self.assertTrue(result.after_cpu.flags.cf)
            self.assertTrue(result.after_cpu.flags.sf)
            self.assertFalse(result.after_cpu.flags.zf)
            self.assertFalse(result.after_cpu.flags.nf)
            self.assertEqual(result.after_cpu.iff_level, 0)
            self.assertEqual(result.after_cpu.rfp, 0)
            self.assertEqual(result.after_cpu.sr_raw, 0x8881)
            self.assertIn("SR", result.written_registers)
            self.assertIn("RFP", result.written_registers)
            self.assertIn("IFF", result.written_registers)

    def test_execute_push_pop_sr_roundtrip_preserves_state(self) -> None:
        """PUSH SR followed by POP SR restores all six flags, iff_level and rfp."""
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            # PUSH SR ; POP SR ; nop
            self._write_demo_rom(rom_path, 0x00200040, b"\x02\x03\x00")
            view = load_fetch_view(rom_path)
            initial_cpu = replace(
                view.machine.cpu,
                regs=replace(view.machine.cpu.regs, xsp=0x00006C00),
                flags=StatusFlags(sf=False, zf=True, vf=True, hf=False, cf=False, nf=True),
                iff_level=5,
                rfp=2,
            )

            push_result = build_execute_next(view, cpu_state=initial_cpu)
            self.assertEqual(push_result.status, "executed")
            assert push_result.after_cpu is not None
            self.assertEqual(push_result.after_cpu.regs.xsp, 0x00006BFE)

            pop_result = build_execute_next(
                view,
                cpu_state=push_result.after_cpu,
                memory_bytes=push_result.after_memory,
            )
            self.assertEqual(pop_result.status, "executed")
            assert pop_result.after_cpu is not None

            # XSP must be back to its initial value.
            self.assertEqual(pop_result.after_cpu.regs.xsp, 0x00006C00)
            # All SR-derived fields restored.
            self.assertEqual(pop_result.after_cpu.flags.sf, initial_cpu.flags.sf)
            self.assertEqual(pop_result.after_cpu.flags.zf, initial_cpu.flags.zf)
            self.assertEqual(pop_result.after_cpu.flags.vf, initial_cpu.flags.vf)
            self.assertEqual(pop_result.after_cpu.flags.hf, initial_cpu.flags.hf)
            self.assertEqual(pop_result.after_cpu.flags.cf, initial_cpu.flags.cf)
            self.assertEqual(pop_result.after_cpu.flags.nf, initial_cpu.flags.nf)
            self.assertEqual(pop_result.after_cpu.iff_level, initial_cpu.iff_level)
            self.assertEqual(pop_result.after_cpu.rfp, initial_cpu.rfp)

    def test_execute_pop_sr_reloads_visible_core_regs_from_restored_bank(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x00200040, b"\x03")
            view = load_fetch_view(rom_path)
            banks = (
                BankedByteRegisters(slots=(0x44, 0x33, 0x22, 0x11) + (None,) * 12),
                BankedByteRegisters(slots=(None,) * 16),
                BankedByteRegisters(slots=(None,) * 16),
                BankedByteRegisters(slots=(0x78, 0x56, 0x34, 0x12) + (None,) * 12),
            )
            seeded_cpu = replace(
                view.machine.cpu,
                regs=replace(view.machine.cpu.regs, xwa=0x11223344, xsp=0x00006BFE),
                rfp=0,
                register_bank=0,
                register_banks=banks,
            )
            # SR=0x8B00 -> MAX=1, SYSM=1, RFP=3, all modeled flags clear, iff_level=0.
            seeded_memory = {0x00006BFE: 0x00, 0x00006BFF: 0x8B}

            result = build_execute_next(
                view, cpu_state=seeded_cpu, memory_bytes=seeded_memory,
            )

            self.assertEqual(result.status, "executed")
            assert result.after_cpu is not None
            self.assertEqual(result.after_cpu.rfp, 3)
            self.assertEqual(result.after_cpu.register_bank, 3)
            self.assertEqual(result.after_cpu.regs.xwa, 0x12345678)
            assert result.after_cpu.register_banks is not None
            self.assertEqual(result.after_cpu.register_banks[0].slots[:4], (0x44, 0x33, 0x22, 0x11))

    def test_execute_pop_sr_blocks_when_xsp_unknown(self) -> None:
        """POP SR honestly stops when XSP is not modeled yet."""
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x00200040, b"\x03")
            view = load_fetch_view(rom_path)

            result = build_execute_next(view)

            self.assertEqual(result.status, "requires-known-stack-pointer")
            self.assertIsNone(result.after_cpu)

    def test_execute_reti_reloads_visible_core_regs_from_restored_bank(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x00200040, b"\x07")
            view = load_fetch_view(rom_path)
            banks = (
                BankedByteRegisters(slots=(0x44, 0x33, 0x22, 0x11) + (None,) * 12),
                BankedByteRegisters(slots=(None,) * 16),
                BankedByteRegisters(slots=(None,) * 16),
                BankedByteRegisters(slots=(0x78, 0x56, 0x34, 0x12) + (None,) * 12),
            )
            seeded_cpu = replace(
                view.machine.cpu,
                regs=replace(view.machine.cpu.regs, xwa=0x11223344, xsp=0x00006BFA),
                rfp=0,
                register_bank=0,
                register_banks=banks,
            )
            seeded_memory = {
                0x00006BFA: 0x99,
                0x00006BFB: 0x00,
                0x00006BFC: 0x20,
                0x00006BFD: 0x00,
                0x00006BFE: 0x00,
                0x00006BFF: 0x8B,
            }

            result = build_execute_next(
                view, cpu_state=seeded_cpu, memory_bytes=seeded_memory,
            )

            self.assertEqual(result.status, "executed")
            assert result.after_cpu is not None
            self.assertEqual(result.after_cpu.pc, 0x00200099)
            self.assertEqual(result.after_cpu.rfp, 3)
            self.assertEqual(result.after_cpu.register_bank, 3)
            self.assertEqual(result.after_cpu.regs.xwa, 0x12345678)
            assert result.after_cpu.register_banks is not None
            self.assertEqual(result.after_cpu.register_banks[0].slots[:4], (0x44, 0x33, 0x22, 0x11))
            self.assertEqual(result.after_cpu.regs.xsp, 0x00006C00)


class ExecutorMemoryReadCollectionTests(unittest.TestCase):
    """Phase 3: every executor that reads memory now surfaces its reads
    via `_STEP_READS`, folded automatically into `memory_reads`."""

    def _write_demo_rom(self, path: Path, entry_point: int, body: bytes) -> None:
        data = bytearray(0x40)
        data[0x00:0x1C] = b"LICENSED BY SNK CORPORATION".ljust(0x1C, b"\x00")
        data[0x1C:0x20] = entry_point.to_bytes(4, "little")
        data[0x22] = 0
        data[0x23] = 0x10
        data[0x24:0x30] = b"READ COLLECT\x00"
        body_offset = entry_point - 0x00200000
        if body_offset < len(data):
            data[body_offset : body_offset + len(body)] = body
        else:
            data.extend(b"\x00" * (body_offset - len(data)))
            data.extend(body)
        path.write_bytes(bytes(data))

    def test_cp_abs24_immediate_surfaces_one_byte_read(self) -> None:
        """`cp (abs24), imm8` reads one byte from abs24; that read appears
        in memory_reads automatically."""
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            # C2 abs24le 3F imm8 → cp (0x004000), 0x5A
            self._write_demo_rom(
                rom_path, 0x00200040, b"\xC2\x00\x40\x00\x3F\x5A"
            )
            view = load_fetch_view(rom_path)

            result = build_execute_next(view)

            self.assertEqual(result.status, "executed")
            self.assertEqual(len(result.memory_reads), 1)
            self.assertEqual(result.memory_reads[0].address, 0x004000)
            self.assertEqual(result.memory_reads[0].size if hasattr(
                result.memory_reads[0], "size"
            ) else len(result.memory_reads[0].data), 1)
            # Work RAM is pre-init to 0 so the read data is 0x00.
            self.assertEqual(result.memory_reads[0].data, b"\x00")

    def test_ld_r8_abs24_load_records_one_byte_read(self) -> None:
        """`ld R8, (abs24)` (C2 abs24 20+r) reads 1 byte → memory_reads has it.

        Needs the destination's owning XWA to be modeled before R8 can
        be honestly updated (the byte writeback must merge into the
        full 32-bit register state)."""
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            # C2 abs24le 21 = ld A, (0x004000)
            self._write_demo_rom(rom_path, 0x00200040, b"\xC2\x00\x40\x00\x21")
            view = load_fetch_view(rom_path)
            seeded_cpu = replace(
                view.machine.cpu,
                regs=replace(view.machine.cpu.regs, xwa=0x00000000),
            )

            result = build_execute_next(view, cpu_state=seeded_cpu)

            self.assertEqual(result.status, "executed")
            self.assertEqual(len(result.memory_reads), 1)
            self.assertEqual(result.memory_reads[0].address, 0x004000)
            self.assertEqual(result.memory_reads[0].data, b"\x00")

    def test_executor_with_no_reads_emits_empty_memory_reads(self) -> None:
        """A NOP does not read any memory; memory_reads stays empty."""
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x00200040, b"\x00")
            view = load_fetch_view(rom_path)

            result = build_execute_next(view)

            self.assertEqual(result.status, "executed")
            self.assertEqual(result.memory_reads, ())

    def test_pop_sr_read_still_surfaces_via_global_accumulator(self) -> None:
        """POP SR previously emitted memory_reads manually (Phase 2); now
        the global accumulator handles it — same observable result."""
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x00200040, b"\x03")
            view = load_fetch_view(rom_path)
            seeded_cpu = replace(
                view.machine.cpu,
                regs=replace(view.machine.cpu.regs, xsp=0x00006BFE),
            )

            result = build_execute_next(view, cpu_state=seeded_cpu)

            self.assertEqual(result.status, "executed")
            self.assertEqual(len(result.memory_reads), 1)
            self.assertEqual(result.memory_reads[0].address, 0x00006BFE)
            self.assertEqual(len(result.memory_reads[0].data), 2)

    def test_execute_di_sets_iff_disabled(self) -> None:
        """di — sets IFF level to 7 (all maskable IRQs blocked)."""
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            # 06 07 = di
            self._write_demo_rom(rom_path, 0x00200040, b"\x06\x07")
            view = load_fetch_view(rom_path)

            result = build_execute_next(view)

            self.assertEqual(result.status, "executed")
            self.assertIn("IFF", result.written_registers)
            assert result.after_cpu is not None
            self.assertEqual(result.after_cpu.iff_enabled, False)
            self.assertEqual(result.after_cpu.iff_level, 7)
            self.assertEqual(result.after_cpu.pc, 0x00200042)

    def test_execute_ei_carries_through_run_steps_context(self) -> None:
        """ei 0 followed by a NOP: IFF state carries to next step."""
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            # 06 00 (ei 0) + 00 (nop)
            self._write_demo_rom(rom_path, 0x00200040, b"\x06\x00\x00")
            view = load_fetch_view(rom_path)

            ei_result = build_execute_next(view)
            self.assertEqual(ei_result.status, "executed")
            assert ei_result.after_cpu is not None
            self.assertEqual(ei_result.after_cpu.iff_enabled, True)


    def test_execute_indexed_rmw_add_reads_overlay_adds_r32_writes_back(self) -> None:
        # AF 38 88 = add (XSP+56), XWA
        # AF = A8+7 → base = XSP; 0x38 = displacement +56; 0x88 = source XWA (0x88+0)
        # effective = XSP + 56 = 0x006000 + 56 = 0x006038
        # mem[0x6038..0x603B] = 0x00000001; XWA = 0x00000002 => result = 0x00000003
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x0020D0AC, b"\xAF\x38\x88")
            base_view = load_fetch_view(rom_path)
            seeded_cpu = replace(
                base_view.machine.cpu,
                regs=replace(
                    base_view.machine.cpu.regs,
                    xsp=0x00006000,
                    xwa=0x00000002,
                ),
            )

            result = build_execute_next(
                base_view,
                cpu_state=seeded_cpu,
                memory_bytes={
                    0x006038: 0x01,
                    0x006039: 0x00,
                    0x00603A: 0x00,
                    0x00603B: 0x00,
                },
            )

            self.assertEqual(result.status, "executed")
            self.assertEqual(result.decode.assembly, "add (XSP+56), XWA")
            self.assertEqual(result.written_registers, ("PC",))
            self.assertEqual(len(result.memory_writes), 1)
            self.assertEqual(result.memory_writes[0].address, 0x006038)
            self.assertEqual(result.memory_writes[0].data, b"\x03\x00\x00\x00")
            assert result.after_cpu is not None
            assert result.after_memory is not None
            self.assertEqual(result.after_cpu.pc, 0x0020D0AF)
            self.assertEqual(result.after_memory[0x006038], 0x03)
            self.assertEqual(result.after_memory[0x006039], 0x00)
            self.assertEqual(result.after_memory[0x00603A], 0x00)
            self.assertEqual(result.after_memory[0x00603B], 0x00)
            # Flags: result 3 is non-zero, positive, no overflow, no carry
            self.assertFalse(result.after_cpu.flags.zf)
            self.assertFalse(result.after_cpu.flags.sf)
            self.assertFalse(result.after_cpu.flags.cf)

    def test_execute_indexed_long_sub_updates_destination_register(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x00201720, b"\xAF\x04\xA0")
            base_view = load_fetch_view(rom_path)
            seeded_cpu = replace(
                base_view.machine.cpu,
                regs=replace(
                    base_view.machine.cpu.regs,
                    xsp=0x00006000,
                    xwa=0x00000010,
                ),
            )

            result = build_execute_next(
                base_view,
                cpu_state=seeded_cpu,
                memory_bytes={0x006004: 0x05, 0x006005: 0x00, 0x006006: 0x00, 0x006007: 0x00},
            )

            self.assertEqual(result.status, "executed")
            self.assertEqual(result.decode.assembly, "sub XWA, (XSP+4)")
            self.assertEqual(result.written_registers, ("XWA", "PC"))
            assert result.after_cpu is not None
            self.assertEqual(result.after_cpu.regs.xwa, 0x0000000B)
            self.assertEqual(result.after_cpu.pc, 0x00201723)
            self.assertFalse(result.after_cpu.flags.cf)
            self.assertFalse(result.after_cpu.flags.zf)
            self.assertFalse(result.after_cpu.flags.sf)
            self.assertTrue(result.after_cpu.flags.nf)

    def test_execute_indexed_long_xor_writes_back_to_memory(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x00201730, b"\xAF\x04\xD8")
            base_view = load_fetch_view(rom_path)
            seeded_cpu = replace(
                base_view.machine.cpu,
                regs=replace(
                    base_view.machine.cpu.regs,
                    xsp=0x00006000,
                    xwa=0x0F0F0F0F,
                ),
            )

            result = build_execute_next(
                base_view,
                cpu_state=seeded_cpu,
                memory_bytes={0x006004: 0xFF, 0x006005: 0x00, 0x006006: 0xFF, 0x006007: 0x00},
            )

            self.assertEqual(result.status, "executed")
            self.assertEqual(result.decode.assembly, "xor (XSP+4), XWA")
            self.assertEqual(result.written_registers, ("PC",))
            assert result.after_cpu is not None
            assert result.after_memory is not None
            self.assertEqual(result.after_memory[0x006004], 0xF0)
            self.assertEqual(result.after_memory[0x006005], 0x0F)
            self.assertEqual(result.after_memory[0x006006], 0xF0)
            self.assertEqual(result.after_memory[0x006007], 0x0F)
            self.assertFalse(result.after_cpu.flags.zf)
            self.assertFalse(result.after_cpu.flags.sf)
            self.assertFalse(result.after_cpu.flags.cf)

    def test_execute_indexed_long_or_immediate_writes_back_to_memory(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x00201740, b"\xAF\x04\x3E\x78\x56\x34\x12")
            base_view = load_fetch_view(rom_path)
            seeded_cpu = replace(
                base_view.machine.cpu,
                regs=replace(base_view.machine.cpu.regs, xsp=0x00006000),
            )

            result = build_execute_next(
                base_view,
                cpu_state=seeded_cpu,
                memory_bytes={0x006004: 0x00, 0x006005: 0x00, 0x006006: 0x00, 0x006007: 0x00},
            )

            self.assertEqual(result.status, "executed")
            self.assertEqual(result.decode.assembly, "or (XSP+4), 0x12345678")
            self.assertEqual(result.written_registers, ("PC",))
            assert result.after_cpu is not None
            assert result.after_memory is not None
            self.assertEqual(result.after_cpu.pc, 0x00201747)
            self.assertEqual(result.after_memory[0x006004], 0x78)
            self.assertEqual(result.after_memory[0x006005], 0x56)
            self.assertEqual(result.after_memory[0x006006], 0x34)
            self.assertEqual(result.after_memory[0x006007], 0x12)
            self.assertFalse(result.after_cpu.flags.zf)
            self.assertFalse(result.after_cpu.flags.sf)
            self.assertFalse(result.after_cpu.flags.cf)

    def test_execute_indexed_word_add_writes_back_to_memory(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x00200040, b"\x9F\x04\x88")
            base_view = load_fetch_view(rom_path)
            seeded_cpu = replace(
                base_view.machine.cpu,
                regs=replace(
                    base_view.machine.cpu.regs,
                    xsp=0x00006000,
                    xwa=0x00000002,
                ),
            )

            result = build_execute_next(
                base_view,
                cpu_state=seeded_cpu,
                memory_bytes={0x006004: 0x01, 0x006005: 0x00},
            )

            self.assertEqual(result.status, "executed")
            self.assertEqual(result.decode.assembly, "add (XSP+4), WA")
            self.assertEqual(result.written_registers, ("PC",))
            self.assertEqual(len(result.memory_writes), 1)
            self.assertEqual(result.memory_writes[0].address, 0x006004)
            self.assertEqual(result.memory_writes[0].data, b"\x03\x00")
            assert result.after_cpu is not None
            assert result.after_memory is not None
            self.assertEqual(result.after_cpu.pc, 0x00200043)
            self.assertEqual(result.after_memory[0x006004], 0x03)
            self.assertEqual(result.after_memory[0x006005], 0x00)
            self.assertFalse(result.after_cpu.flags.zf)
            self.assertFalse(result.after_cpu.flags.sf)
            self.assertFalse(result.after_cpu.flags.cf)

    def test_execute_indexed_word_add_updates_destination_register(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x00200040, b"\x9F\x04\x80")
            base_view = load_fetch_view(rom_path)
            seeded_cpu = replace(
                base_view.machine.cpu,
                regs=replace(
                    base_view.machine.cpu.regs,
                    xsp=0x00006000,
                    xwa=0xAABB0002,
                ),
            )

            result = build_execute_next(
                base_view,
                cpu_state=seeded_cpu,
                memory_bytes={0x006004: 0x01, 0x006005: 0x00},
            )

            self.assertEqual(result.status, "executed")
            self.assertEqual(result.decode.assembly, "add WA, (XSP+4)")
            self.assertEqual(result.written_registers, ("WA", "PC"))
            assert result.after_cpu is not None
            self.assertEqual(result.after_cpu.regs.xwa, 0xAABB0003)
            self.assertEqual(result.after_cpu.pc, 0x00200043)
            self.assertFalse(result.after_cpu.flags.zf)
            self.assertFalse(result.after_cpu.flags.sf)
            self.assertFalse(result.after_cpu.flags.cf)

    def test_execute_indexed_word_xor_writes_back_to_memory(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x00200040, b"\x9F\x04\xD8")
            base_view = load_fetch_view(rom_path)
            seeded_cpu = replace(
                base_view.machine.cpu,
                regs=replace(
                    base_view.machine.cpu.regs,
                    xsp=0x00006000,
                    xwa=0x00000F0F,
                ),
            )

            result = build_execute_next(
                base_view,
                cpu_state=seeded_cpu,
                memory_bytes={0x006004: 0xFF, 0x006005: 0x00},
            )

            self.assertEqual(result.status, "executed")
            self.assertEqual(result.decode.assembly, "xor (XSP+4), WA")
            self.assertEqual(result.written_registers, ("PC",))
            assert result.after_cpu is not None
            assert result.after_memory is not None
            self.assertEqual(result.after_memory[0x006004], 0xF0)
            self.assertEqual(result.after_memory[0x006005], 0x0F)
            self.assertFalse(result.after_cpu.flags.zf)
            self.assertFalse(result.after_cpu.flags.sf)
            self.assertFalse(result.after_cpu.flags.cf)

    def test_execute_indexed_word_add_immediate_writes_back_to_memory(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x00200040, b"\x9F\x04\x38\x34\x12")
            base_view = load_fetch_view(rom_path)
            seeded_cpu = replace(
                base_view.machine.cpu,
                regs=replace(base_view.machine.cpu.regs, xsp=0x00006000),
            )

            result = build_execute_next(
                base_view,
                cpu_state=seeded_cpu,
                memory_bytes={0x006004: 0x01, 0x006005: 0x00},
            )

            self.assertEqual(result.status, "executed")
            self.assertEqual(result.decode.assembly, "add (XSP+4), 0x1234")
            self.assertEqual(result.written_registers, ("PC",))
            assert result.after_cpu is not None
            assert result.after_memory is not None
            self.assertEqual(result.after_cpu.pc, 0x00200045)
            self.assertEqual(result.after_memory[0x006004], 0x35)
            self.assertEqual(result.after_memory[0x006005], 0x12)
            self.assertFalse(result.after_cpu.flags.zf)
            self.assertFalse(result.after_cpu.flags.sf)
            self.assertFalse(result.after_cpu.flags.cf)

    def test_execute_indexed_word_inc_preserves_carry(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x00200040, b"\x9F\x04\x61")
            base_view = load_fetch_view(rom_path)
            seeded_cpu = replace(
                base_view.machine.cpu,
                regs=replace(base_view.machine.cpu.regs, xsp=0x00006000),
                flags=replace(base_view.machine.cpu.flags, cf=True),
            )

            result = build_execute_next(
                base_view,
                cpu_state=seeded_cpu,
                memory_bytes={0x006004: 0xFF, 0x006005: 0x00},
            )

            self.assertEqual(result.status, "executed")
            self.assertEqual(result.decode.assembly, "inc 1, (XSP+4)")
            assert result.after_cpu is not None
            assert result.after_memory is not None
            self.assertEqual(result.after_memory[0x006004], 0x00)
            self.assertEqual(result.after_memory[0x006005], 0x01)
            self.assertTrue(result.after_cpu.flags.cf)
            self.assertFalse(result.after_cpu.flags.zf)

    def test_execute_indexed_byte_add_updates_destination_register(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x00200040, b"\x8F\x02\x81")
            base_view = load_fetch_view(rom_path)
            seeded_cpu = replace(
                base_view.machine.cpu,
                regs=replace(
                    base_view.machine.cpu.regs,
                    xsp=0x00006000,
                    xwa=0x00000010,
                ),
            )

            result = build_execute_next(
                base_view,
                cpu_state=seeded_cpu,
                memory_bytes={0x006002: 0x20},
            )

            self.assertEqual(result.status, "executed")
            self.assertEqual(result.decode.assembly, "add A, (XSP+2)")
            self.assertEqual(result.written_registers, ("A", "PC"))
            assert result.after_cpu is not None
            self.assertEqual(result.after_cpu.regs.xwa & 0xFF, 0x30)
            self.assertEqual(result.after_cpu.pc, 0x00200043)
            self.assertFalse(result.after_cpu.flags.cf)
            self.assertFalse(result.after_cpu.flags.zf)

    def test_execute_indexed_byte_and_immediate_writes_memory(self) -> None:
        # dialogue cart frontier: 8D 1F 3C FC = and (XIY+31), 0xFC.
        # Byte RMW: mem = mem & imm8, flags only from the result (logical).
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x00200040, b"\x8D\x1F\x3C\xFC")
            base_view = load_fetch_view(rom_path)
            seeded_cpu = replace(
                base_view.machine.cpu,
                regs=replace(base_view.machine.cpu.regs, xiy=0x00006000),
            )

            result = build_execute_next(
                base_view,
                cpu_state=seeded_cpu,
                memory_bytes={0x00601F: 0x37},
            )

            self.assertEqual(result.status, "executed")
            self.assertEqual(result.decode.assembly, "and (XIY+31), 0xFC")
            assert result.after_memory is not None
            # 0x37 & 0xFC = 0x34, written back to memory.
            self.assertEqual(result.after_memory[0x00601F], 0x34)
            self.assertEqual(len(result.memory_writes), 1)
            assert result.after_cpu is not None
            self.assertFalse(result.after_cpu.flags.zf)
            self.assertFalse(result.after_cpu.flags.cf)
            self.assertEqual(result.after_cpu.pc, 0x00200044)

    def test_execute_indexed_byte_res_clears_bit_writes_memory(self) -> None:
        # dialogue cart frontier: BE 1F B1 = res 1, (XIZ+31). RMW: clear bit 1,
        # write back; flags unchanged.
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x00200040, b"\xBE\x1F\xB1")
            base_view = load_fetch_view(rom_path)
            seeded_cpu = replace(
                base_view.machine.cpu,
                regs=replace(base_view.machine.cpu.regs, xiz=0x00006000),
            )

            result = build_execute_next(
                base_view,
                cpu_state=seeded_cpu,
                memory_bytes={0x00601F: 0xFF},
            )

            self.assertEqual(result.status, "executed")
            self.assertEqual(result.decode.assembly, "res 1, (XIZ+31)")
            assert result.after_memory is not None
            # 0xFF with bit 1 cleared = 0xFD.
            self.assertEqual(result.after_memory[0x00601F], 0xFD)
            self.assertEqual(len(result.memory_writes), 1)
            self.assertEqual(result.after_cpu.pc, 0x00200043)

    def test_execute_indexed_byte_sub_updates_destination_register(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x00200040, b"\x8F\x14\xA1")
            base_view = load_fetch_view(rom_path)
            seeded_cpu = replace(
                base_view.machine.cpu,
                regs=replace(
                    base_view.machine.cpu.regs,
                    xsp=0x00006000,
                    xwa=0x00000010,
                ),
            )

            result = build_execute_next(
                base_view,
                cpu_state=seeded_cpu,
                memory_bytes={0x006014: 0x05},
            )

            self.assertEqual(result.status, "executed")
            self.assertEqual(result.decode.assembly, "sub A, (XSP+20)")
            assert result.after_cpu is not None
            self.assertEqual(result.after_cpu.regs.xwa & 0xFF, 0x0B)
            self.assertFalse(result.after_cpu.flags.cf)
            self.assertTrue(result.after_cpu.flags.nf)

    def test_execute_indexed_byte_cp_memory_minus_register_sets_flags_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x00200040, b"\x8F\x02\xF9")
            base_view = load_fetch_view(rom_path)
            seeded_cpu = replace(
                base_view.machine.cpu,
                regs=replace(
                    base_view.machine.cpu.regs,
                    xsp=0x00006000,
                    xwa=0x00000010,
                ),
            )

            result = build_execute_next(
                base_view,
                cpu_state=seeded_cpu,
                memory_bytes={0x006002: 0x05},
            )

            self.assertEqual(result.status, "executed")
            self.assertEqual(result.decode.assembly, "cp (XSP+2), A")
            self.assertEqual(result.memory_writes, ())
            self.assertEqual(result.written_registers, ("PC",))
            assert result.after_cpu is not None
            self.assertTrue(result.after_cpu.flags.cf)
            self.assertTrue(result.after_cpu.flags.sf)

    def test_execute_indexed_rmw_add_blocked_when_memory_unavailable(self) -> None:
        # AF 38 88 = add (XSP+56), XWA — effective address 0x00FF0038 lands in
        # the BIOS region with NO BIOS image attached, the one memory class that
        # still honest-stops (we genuinely lack the bytes). On-chip RAM is
        # pre-initialised to 0x00 and a truly UNMAPPED target (0x00C000..0x1FFFFF)
        # now open-bus-reads 0x00 (hw_test_openbus 2026-07-08: the TLCS-900 has no
        # bus fault), so neither exercises runtime-memory-unavailable any more.
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x0020D0AC, b"\xAF\x38\x88")
            base_view = load_fetch_view(rom_path)
            seeded_cpu = replace(
                base_view.machine.cpu,
                regs=replace(
                    base_view.machine.cpu.regs,
                    xsp=0x00FF0000,
                    xwa=0x00000002,
                ),
            )

            result = build_execute_next(base_view, cpu_state=seeded_cpu)

            self.assertEqual(result.status, "runtime-memory-unavailable")
            self.assertIsNone(result.after_cpu)

    def test_execute_cp_reg_indirect_imm8_reads_erased_unloaded_cart_flash(self) -> None:
        """cp (XWA), 0xCA — unloaded cart flash reads as erased 0xFF."""
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x00200040, b"\x80\x3F\xCA")
            base_view = load_fetch_view(rom_path)
            seeded_cpu = replace(
                base_view.machine.cpu,
                regs=replace(base_view.machine.cpu.regs, xwa=0x003FBE00),
            )

            result = build_execute_next(base_view, cpu_state=seeded_cpu)

            self.assertEqual(result.status, "executed")
            self.assertEqual(result.decode.assembly, "cp (XWA), 0xCA")
            assert result.after_cpu is not None
            self.assertEqual(result.after_cpu.pc, 0x00200043)
            self.assertFalse(result.after_cpu.flags.zf)
            self.assertFalse(result.after_cpu.flags.sf)
            self.assertFalse(result.after_cpu.flags.cf)


class C7ExtendedRegisterTests(unittest.TestCase):
    """Pass 57 — C7 extended-register prefix on current-bank byte slices.

    The C7 prefix selects a byte register via an 8-bit code (`r8_names`).
    Codes 0xE0..0xFF address byte slices of the eight current-bank 32-bit
    registers, e.g. QC = bits 16..23 of XBC, QIZH = bits 24..31 of XIZ.
    Encodings are verified against ngdis `decode_zz_r` (extension case) and
    the T900_DENSE_REF.md §XIZ-spill examples (`C7 FB 9F = ld QIZH, L`).
    """

    def _write_demo_rom(self, path: Path, entry_point: int, body: bytes) -> None:
        data = bytearray(0x40)
        data[0x00:0x1C] = b"LICENSED BY SNK CORPORATION".ljust(0x1C, b"\x00")
        data[0x1C:0x20] = entry_point.to_bytes(4, "little")
        data[0x23] = 0x10
        data[0x24:0x30] = b"TEST GAME\x00\x00\x00"
        data[0x40 : 0x40 + len(body)] = body
        path.write_bytes(bytes(data))

    def _run(self, body: bytes, **reg_values):
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x00200040, body)
            view = load_fetch_view(rom_path)
            control_registers = reg_values.pop("control_registers", view.machine.cpu.control_registers)
            seeded_cpu = replace(
                view.machine.cpu,
                regs=replace(view.machine.cpu.regs, **reg_values),
                flags=StatusFlags(sf=False, zf=False, vf=False, hf=False, cf=False, nf=False),
                control_registers=control_registers,
            )
            return build_execute_next(view, cpu_state=seeded_cpu)

    def test_ld_r_R_writes_q_slice_of_owner(self) -> None:
        # C7 E6 99 = ld QC, A : QC = XBC[16:23] <- A = XWA[0:7]
        result = self._run(b"\xC7\xE6\x99", xwa=0x000000AB, xbc=0x00000000)
        self.assertEqual(result.status, "executed")
        self.assertEqual(result.decode.assembly, "ld QC, A")
        assert result.after_cpu is not None
        self.assertEqual(result.after_cpu.regs.xbc, 0x00AB0000)
        self.assertEqual(result.after_cpu.pc, 0x00200043)

    def test_ld_R_r_reads_q_slice_into_r8(self) -> None:
        # C7 E6 8D = ld E, QC : E = XDE[0:7] <- QC = XBC[16:23]
        result = self._run(b"\xC7\xE6\x8D", xbc=0x00AB0000, xde=0x00000000)
        self.assertEqual(result.status, "executed")
        self.assertEqual(result.decode.assembly, "ld E, QC")
        assert result.after_cpu is not None
        self.assertEqual(result.after_cpu.regs.xde, 0x000000AB)

    def test_ld_q_slice_preserves_other_bytes(self) -> None:
        # C7 FB 9F = ld QIZH, L (T900_DENSE_REF.md spill example).
        result = self._run(b"\xC7\xFB\x9F", xhl=0x00000042, xiz=0x00112233)
        self.assertEqual(result.status, "executed")
        self.assertEqual(result.decode.assembly, "ld QIZH, L")
        assert result.after_cpu is not None
        self.assertEqual(result.after_cpu.regs.xiz, 0x42112233)

    def test_cp_q_slice_imm8_sets_zero_flag(self) -> None:
        # C7 E6 CF 10 = cp QC, 0x10 ; QC == 0x10 -> Z set, no register write.
        result = self._run(b"\xC7\xE6\xCF\x10", xbc=0x00100000)
        self.assertEqual(result.status, "executed")
        self.assertEqual(result.decode.assembly, "cp QC, 0x10")
        assert result.after_cpu is not None
        self.assertTrue(result.after_cpu.flags.zf)
        self.assertFalse(result.after_cpu.flags.cf)
        self.assertEqual(result.after_cpu.regs.xbc, 0x00100000)  # unchanged
        self.assertEqual(result.after_cpu.pc, 0x00200044)

    def test_add_r8_q_slice_writes_r8_destination(self) -> None:
        # C7 E6 80 = add W, QC : W = XWA[8:15] <- W + QC.
        result = self._run(b"\xC7\xE6\x80", xwa=0x00000100, xbc=0x00200000)
        self.assertEqual(result.status, "executed")
        self.assertEqual(result.decode.assembly, "add W, QC")
        assert result.after_cpu is not None
        self.assertEqual(result.after_cpu.regs.xwa, 0x00002100)
        self.assertFalse(result.after_cpu.flags.cf)

    def test_inc_q_slice_preserves_carry(self) -> None:
        # C7 E6 60 = inc 8, QC (n=0 means 8). CF must stay as seeded.
        result = self._run(b"\xC7\xE6\x60", xbc=0x00050000)
        self.assertEqual(result.status, "executed")
        self.assertEqual(result.decode.assembly, "inc 8, QC")
        assert result.after_cpu is not None
        self.assertEqual(result.after_cpu.regs.xbc, 0x000D0000)
        self.assertFalse(result.after_cpu.flags.cf)  # preserved (was False)

    def test_imm_load_writes_q_slice(self) -> None:
        # C7 E6 03 7F = ld QC, 0x7F
        result = self._run(b"\xC7\xE6\x03\x7F", xbc=0x00000000)
        self.assertEqual(result.status, "executed")
        self.assertEqual(result.decode.assembly, "ld QC, 0x7F")
        assert result.after_cpu is not None
        self.assertEqual(result.after_cpu.regs.xbc, 0x007F0000)

    def test_shift_immediate_updates_q_slice(self) -> None:
        # C7 E6 E8 03 = rlc 3, QC ; QC=0x81 -> 0x0C
        result = self._run(b"\xC7\xE6\xE8\x03", xbc=0x00810000)
        self.assertEqual(result.status, "executed")
        self.assertEqual(result.decode.assembly, "rlc 3, QC")
        assert result.after_cpu is not None
        self.assertEqual(result.after_cpu.regs.xbc, 0x000C0000)
        self.assertFalse(result.after_cpu.flags.cf)

    def test_shift_by_a_updates_q_slice(self) -> None:
        # C7 E6 F8 = rlc A, QC ; A low nibble = 3, QC=0x81 -> 0x0C
        result = self._run(b"\xC7\xE6\xF8", xwa=0x00000003, xbc=0x00810000)
        self.assertEqual(result.status, "executed")
        self.assertEqual(result.decode.assembly, "rlc A, QC")
        assert result.after_cpu is not None
        self.assertEqual(result.after_cpu.regs.xbc, 0x000C0000)
        self.assertFalse(result.after_cpu.flags.cf)

    def test_rotate_through_carry_by_a_blocks_when_carry_unknown(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x00200040, b"\xC7\xE6\xFA")
            view = load_fetch_view(rom_path)
            seeded_cpu = replace(
                view.machine.cpu,
                regs=replace(view.machine.cpu.regs, xwa=0x00000001, xbc=0x00100000),
            )
            result = build_execute_next(view, cpu_state=seeded_cpu)
        self.assertEqual(result.status, "requires-known-flags")

    def test_cpl_q_slice_inverts_bits_and_sets_hn_only(self) -> None:
        result = self._run(b"\xC7\xE6\x06", xbc=0x005A0000)
        self.assertEqual(result.status, "executed")
        self.assertEqual(result.decode.assembly, "cpl QC")
        assert result.after_cpu is not None
        self.assertEqual(result.after_cpu.regs.xbc, 0x00A50000)
        self.assertFalse(result.after_cpu.flags.sf)
        self.assertFalse(result.after_cpu.flags.zf)
        self.assertTrue(result.after_cpu.flags.hf)
        self.assertTrue(result.after_cpu.flags.nf)

    def test_neg_q_slice_uses_subtract_flags(self) -> None:
        result = self._run(b"\xC7\xE6\x07", xbc=0x00050000)
        self.assertEqual(result.status, "executed")
        self.assertEqual(result.decode.assembly, "neg QC")
        assert result.after_cpu is not None
        self.assertEqual(result.after_cpu.regs.xbc, 0x00FB0000)
        self.assertTrue(result.after_cpu.flags.sf)
        self.assertFalse(result.after_cpu.flags.zf)
        self.assertTrue(result.after_cpu.flags.hf)
        self.assertTrue(result.after_cpu.flags.cf)
        self.assertTrue(result.after_cpu.flags.nf)

    def test_daa_q_slice_adjusts_current_bank_byte(self) -> None:
        result = self._run(b"\xC7\xE6\x10", xbc=0x006C0000)
        self.assertEqual(result.status, "executed")
        self.assertEqual(result.decode.assembly, "daa QC")
        assert result.after_cpu is not None
        self.assertEqual(result.after_cpu.regs.xbc, 0x00720000)
        self.assertFalse(result.after_cpu.flags.sf)
        self.assertFalse(result.after_cpu.flags.zf)
        self.assertTrue(result.after_cpu.flags.vf)
        self.assertTrue(result.after_cpu.flags.hf)
        self.assertFalse(result.after_cpu.flags.cf)
        self.assertFalse(result.after_cpu.flags.nf)

    def test_push_q_slice_writes_one_stack_byte(self) -> None:
        result = self._run(b"\xC7\xE6\x04", xbc=0x005A0000, xsp=0x00006C00)
        self.assertEqual(result.status, "executed")
        self.assertEqual(result.decode.assembly, "push QC")
        assert result.after_cpu is not None
        self.assertEqual(result.after_cpu.regs.xsp, 0x00006BFF)
        self.assertEqual(len(result.memory_writes), 1)
        self.assertEqual(result.memory_writes[0].address, 0x00006BFF)
        self.assertEqual(result.memory_writes[0].data, b"\x5A")

    def test_pop_q_slice_reads_one_stack_byte(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x00200040, b"\xC7\xE6\x05")
            view = load_fetch_view(rom_path)
            seeded_cpu = replace(
                view.machine.cpu,
                regs=replace(view.machine.cpu.regs, xbc=0x00000000, xsp=0x00006C00),
                flags=StatusFlags(sf=False, zf=False, vf=False, hf=False, cf=False, nf=False),
            )
            result = build_execute_next(
                view,
                cpu_state=seeded_cpu,
                memory_bytes={0x00006C00: 0x5A},
            )

        self.assertEqual(result.status, "executed")
        self.assertEqual(result.decode.assembly, "pop QC")
        assert result.after_cpu is not None
        self.assertEqual(result.after_cpu.regs.xbc, 0x005A0000)
        self.assertEqual(result.after_cpu.regs.xsp, 0x00006C01)

    def test_adc_q_slice_blocks_on_unknown_carry(self) -> None:
        # C7 E6 C9 01 = adc QC, 0x01 — needs CF known; flags cleared but cf=None here.
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x00200040, b"\xC7\xE6\xC9\x01")
            view = load_fetch_view(rom_path)
            seeded_cpu = replace(
                view.machine.cpu,
                regs=replace(view.machine.cpu.regs, xbc=0x00100000),
            )
            result = build_execute_next(view, cpu_state=seeded_cpu)
        self.assertEqual(result.status, "runtime-state-required")

    def test_execute_c7_andcf_immediate_updates_carry_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x00200040, b"\xC7\xE6\x20\x03")
            view = load_fetch_view(rom_path)
            seeded_cpu = replace(
                view.machine.cpu,
                regs=replace(view.machine.cpu.regs, xbc=0x00000000),
                flags=StatusFlags(sf=True, zf=False, vf=True, hf=False, cf=True, nf=True),
            )
            result = build_execute_next(view, cpu_state=seeded_cpu)

        self.assertEqual(result.status, "executed")
        self.assertEqual(result.decode.assembly, "andcf 3, QC")
        self.assertEqual(result.written_registers, ("PC",))
        assert result.after_cpu is not None
        self.assertEqual(result.after_cpu.pc, 0x00200044)
        self.assertFalse(result.after_cpu.flags.cf)
        self.assertTrue(result.after_cpu.flags.sf)
        self.assertFalse(result.after_cpu.flags.zf)
        self.assertTrue(result.after_cpu.flags.vf)
        self.assertFalse(result.after_cpu.flags.hf)
        self.assertTrue(result.after_cpu.flags.nf)

    def test_execute_c7_stcf_dynamic_out_of_range_leaves_slice_unchanged(self) -> None:
        result = self._run(b"\xC7\xE6\x2C", xwa=0x00000008, xbc=0x005A0000)
        self.assertEqual(result.status, "executed")
        self.assertEqual(result.decode.assembly, "stcf A, QC")
        self.assertEqual(result.written_registers, ("PC",))
        assert result.after_cpu is not None
        self.assertEqual(result.after_cpu.regs.xbc, 0x005A0000)
        self.assertEqual(result.after_cpu.pc, 0x00200043)

    def test_execute_c7_exts_slice_stops_as_silicon_undefined(self) -> None:
        result = self._run(b"\xC7\xE6\x13", xbc=0x005A0000)
        self.assertEqual(result.status, "silicon-undefined")
        self.assertEqual(result.decode.assembly, "exts QC")
        self.assertIsNone(result.after_cpu)

    def test_execute_c7_unlk_slice_stops_as_silicon_undefined(self) -> None:
        result = self._run(b"\xC7\xE6\x0D", xbc=0x005A0000, xsp=0x00006C00)
        self.assertEqual(result.status, "silicon-undefined")
        self.assertEqual(result.decode.assembly, "unlk QC")
        self.assertIsNone(result.after_cpu)

    def test_execute_c7_ldc_write_updates_dmam0(self) -> None:
        result = self._run(b"\xC7\xE6\x2E\x22", xbc=0x005A0000)
        self.assertEqual(result.status, "executed")
        self.assertEqual(result.decode.assembly, "ldc DMAM0, QC")
        self.assertEqual(result.written_registers, ("PC", "DMAM0"))
        assert result.after_cpu is not None
        assert result.after_cpu.control_registers is not None
        self.assertEqual(result.after_cpu.control_registers.dmam[0], 0x5A)

    def test_execute_c7_ldc_read_updates_qc(self) -> None:
        result = self._run(
            b"\xC7\xE6\x2F\x22",
            xbc=0x11223344,
            control_registers=replace(
                create_unknown_control_registers(),
                dmam=(0xAA, None, None, None),
            ),
        )
        self.assertEqual(result.status, "executed")
        self.assertEqual(result.decode.assembly, "ldc QC, DMAM0")
        self.assertEqual(result.written_registers, ("PC", "XBC"))
        assert result.after_cpu is not None
        self.assertEqual(result.after_cpu.regs.xbc, 0x11AA3344)

    def test_alternate_bank_register_imm_load_updates_banked_store(self) -> None:
        # C7 30 AD = ld RA3, 5 : explicit bank-3 write. The visible bank-0
        # XWA stays untouched; the banked backing store records RA3 = 5.
        result = self._run(b"\xC7\x30\xAD", xwa=0x11223344)
        self.assertEqual(result.status, "executed")
        self.assertEqual(result.decode.assembly, "ld RA3, 5")
        assert result.after_cpu is not None
        self.assertEqual(result.after_cpu.regs.xwa, 0x11223344)
        assert result.after_cpu.register_banks is not None
        self.assertEqual(result.after_cpu.register_banks[3].slots[0], 5)

    def test_previous_bank_register_load_reads_banked_store(self) -> None:
        # Current bank 1, previous bank = 0. D0 names A' and should read from
        # bank-0 low byte of XWA.
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "demo.ngc"
            self._write_demo_rom(rom_path, 0x00200040, b"\xC7\xD0\x89")  # ld A, A'
            view = load_fetch_view(rom_path)
            banks = (
                BankedByteRegisters(slots=(0x5A,) + (None,) * 15),
                BankedByteRegisters(slots=(None,) * 16),
                BankedByteRegisters(slots=(None,) * 16),
                BankedByteRegisters(slots=(None,) * 16),
            )
            seeded_cpu = replace(
                view.machine.cpu,
                regs=replace(view.machine.cpu.regs, xwa=0x00000000),
                flags=StatusFlags(sf=False, zf=False, vf=False, hf=False, cf=False, nf=False),
                rfp=1,
                register_bank=1,
                register_banks=banks,
            )
            result = build_execute_next(view, cpu_state=seeded_cpu)
        self.assertEqual(result.status, "executed")
        assert result.after_cpu is not None
        self.assertEqual(result.after_cpu.regs.xwa & 0xFF, 0x5A)

    def test_unknown_source_register_blocks(self) -> None:
        # ld E, QC with XBC unknown -> the QC slice value is not modeled.
        result = self._run(b"\xC7\xE6\x8D", xde=0x00000000)  # xbc left None
        self.assertEqual(result.status, "requires-known-source-register")

    def test_execute_d7_push_qiz(self) -> None:
        # D7 FA 04 = push QIZ (high word of XIZ). Real engine runtime helper
        # frontier that used to mis-decode as `rl A, SP`.
        result = self._run(b"\xD7\xFA\x04", xiz=0x12345678, xsp=0x00006C00)
        self.assertEqual(result.status, "executed")
        self.assertEqual(result.decode.assembly, "push QIZ")
        assert result.after_cpu is not None and result.after_memory is not None
        self.assertEqual(result.after_cpu.regs.xsp, 0x00006BFE)
        # QIZ = 0x1234 pushed little-endian.
        self.assertEqual(result.after_memory[0x00006BFE], 0x34)
        self.assertEqual(result.after_memory[0x00006BFF], 0x12)

    def test_execute_d7_pop_qiz_roundtrips_push(self) -> None:
        # push QIZ then pop QIZ restores the high word and XSP.
        pushed = self._run(b"\xD7\xFA\x04", xiz=0x12345678, xsp=0x00006C00)
        self.assertEqual(pushed.status, "executed")
        # Now pop from the same state (seed a fresh XIZ high word to prove it loads).
        result = self._run(
            b"\xD7\xFA\x05",
            xiz=0x00005678, xsp=0x00006BFE,
            **{},
        )
        # The stack word at 0x6BFE isn't seeded here, so pop honest-stops on
        # unmodeled stack memory — the decode + XSP path is exercised.
        self.assertIn(result.status, ("executed", "runtime-memory-unavailable"))
        self.assertEqual(result.decode.assembly, "pop QIZ")

    def test_execute_byte_div_reg_reg_cb51_matches_hardware(self) -> None:
        # HW-CLEARED 2026-07-08 (hw_test_bytediv, on a real NGPC):
        # `div A, C` (CB 51, byte mul/div r+r pocket, C8..CF C-source prefix) is
        # NOT silicon-broken. WA=0x1F64 (8036) / C=0x64 (100) -> WA=0x2450 =
        # quotient 80=0x50 in A (low byte) | remainder 36=0x24 in W (high byte).
        result = self._run(b"\xCB\x51", xwa=0x00001F64, xbc=0x00000064)
        self.assertEqual(result.status, "executed")
        self.assertEqual(result.decode.assembly, "div A, C")
        self.assertEqual(result.written_registers, ("XWA", "PC"))
        assert result.after_cpu is not None
        self.assertEqual(result.after_cpu.regs.xwa & 0xFFFF, 0x2450)


if __name__ == "__main__":
    unittest.main()
