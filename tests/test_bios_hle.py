"""BIOS HLE tests: the `swi 1` (SYSTEM_CALL) vector dispatch.

Ground truth for every vector's side effect is the retail SNK BIOS itself.
`swi 1` reads the vector index from RW3 (the W byte of the bank-3 register
file), then dispatches through the table at 0xFFFE00:
`pc = loadL(0xFFFE00 + (rCodeB(0x31) << 2))`.
"""

from __future__ import annotations

from dataclasses import replace
import tempfile
import unittest
from pathlib import Path

import time

import core.execute as execute
from core.cpu import BankedByteRegisters
from core.execute import build_execute_next
from core.fetch import load_fetch_view


def _write_swi_rom(path: Path, entry_point: int, body: bytes) -> None:
    data = bytearray(0x40)
    data[0x00:0x1C] = b"LICENSED BY SNK CORPORATION".ljust(0x1C, b"\x00")
    data[0x1C:0x20] = entry_point.to_bytes(4, "little")
    data[0x23] = 0x10
    data[0x24:0x30] = b"TEST GAME\x00\x00\x00"
    body_offset = entry_point - 0x00200000
    data[body_offset : body_offset + len(body)] = body
    path.write_bytes(bytes(data))


def _banks(bank3_slots: dict[int, int]) -> tuple[BankedByteRegisters, ...]:
    """Build 4 register banks, all zero except explicit bank-3 slot overrides.

    Bank-3 slot layout (Toshiba r8 order): 0=A 1=W 2=QA 3=QW | 4=C 5=B ...
    So RA3=slot0, RW3=slot1, RC3=slot4, RB3=slot5.
    """
    zero = tuple(0 for _ in range(16))
    b3 = list(zero)
    for slot, value in bank3_slots.items():
        b3[slot] = value
    return (
        BankedByteRegisters(slots=zero),
        BankedByteRegisters(slots=zero),
        BankedByteRegisters(slots=zero),
        BankedByteRegisters(slots=tuple(b3)),
    )


class Swi1DispatchTests(unittest.TestCase):
    def _run_swi1(self, bank3_slots: dict[int, int]):
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "swi.ngc"
            _write_swi_rom(rom_path, 0x00200040, b"\xF9")  # swi 1
            view = load_fetch_view(rom_path)
            seeded = replace(
                view.machine.cpu,
                pc=0x00200040,
                rfp=0,
                register_bank=0,
                register_banks=_banks(bank3_slots),
            )
            return build_execute_next(view, cpu_state=seeded)

    def test_swi1_clockgearset_is_noop_advances_pc(self) -> None:
        # RW3 = 1 (VECT_CLOCKGEARSET). RET, no side effect.
        result = self._run_swi1({1: 0x01})
        self.assertEqual(result.decode.assembly, "swi 1")
        self.assertEqual(result.status, "executed")
        self.assertEqual(result.written_registers, ("PC",))
        self.assertEqual(result.memory_writes, ())
        assert result.after_cpu is not None
        self.assertEqual(result.after_cpu.pc, 0x00200041)
        self.assertIn("CLOCKGEARSET", result.note)

    def test_swi1_shutdown_stops_honestly(self) -> None:
        # RW3 = 0 (VECT_SHUTDOWN). The BIOS never returns; we stop honestly.
        result = self._run_swi1({1: 0x00})
        self.assertEqual(result.status, "bios-shutdown")
        self.assertIsNone(result.after_cpu)
        self.assertIn("SHUTDOWN", result.note)

    def test_swi1_flashers_returns_ra3_success(self) -> None:
        # RW3 = 8 (VECT_FLASHERS). Sets RA3 = 0 (SYS_SUCCESS).
        # Seed RA3 (bank-3 slot 0) nonzero to observe it being cleared.
        result = self._run_swi1({0: 0xFF, 1: 0x08})
        self.assertEqual(result.status, "executed")
        self.assertIn("RA3", result.written_registers)
        assert result.after_cpu is not None
        self.assertEqual(result.after_cpu.pc, 0x00200041)
        assert result.after_cpu.register_banks is not None
        self.assertEqual(result.after_cpu.register_banks[3].slots[0], 0x00)
        self.assertIn("SYS_SUCCESS", result.note)

    def test_swi1_intlvset_writes_timer0_low_nibble(self) -> None:
        # RW3=4 (INTLVSET), RC3(source)=2 (timer0 -> IO 0x73 low nibble),
        # RB3(level)=5. Cold IO 0x73 = 0x00 -> becomes 0x05.
        result = self._run_swi1({1: 0x04, 4: 0x02, 5: 0x05})
        self.assertEqual(result.status, "executed")
        self.assertEqual(len(result.memory_writes), 1)
        write = result.memory_writes[0]
        self.assertEqual(write.address, 0x0073)
        self.assertEqual(write.data, bytes([0x05]))
        assert result.after_memory is not None
        self.assertEqual(result.after_memory[0x0073], 0x05)
        assert result.after_cpu is not None
        self.assertEqual(result.after_cpu.pc, 0x00200041)

    def test_swi1_intlvset_high_nibble_preserves_low(self) -> None:
        # RC3(source)=1 (Z80 -> IO 0x71 HIGH nibble), RB3(level)=3.
        # Seed IO 0x71 low nibble via a prior overlay value to prove it survives.
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "swi.ngc"
            _write_swi_rom(rom_path, 0x00200040, b"\xF9")
            view = load_fetch_view(rom_path)
            seeded = replace(
                view.machine.cpu,
                pc=0x00200040,
                rfp=0,
                register_bank=0,
                register_banks=_banks({1: 0x04, 4: 0x01, 5: 0x03}),
            )
            result = build_execute_next(
                view, cpu_state=seeded, memory_bytes={0x0071: 0x0A}
            )
        self.assertEqual(result.status, "executed")
        write = result.memory_writes[0]
        self.assertEqual(write.address, 0x0071)
        # low nibble 0x0A preserved, high nibble = level 3 -> 0x3A
        self.assertEqual(write.data, bytes([0x3A]))

    def test_swi1_unknown_rw3_requires_known_register(self) -> None:
        # RW3 unknown (None) -> honest stop, do not guess the vector.
        result = self._run_swi1({})  # slots all 0 first...
        # override slot 1 (RW3) to None explicitly:
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "swi.ngc"
            _write_swi_rom(rom_path, 0x00200040, b"\xF9")
            view = load_fetch_view(rom_path)
            slots = list(0 for _ in range(16))
            slots[1] = None  # RW3 unknown
            banks = (
                BankedByteRegisters(slots=tuple(0 for _ in range(16))),
                BankedByteRegisters(slots=tuple(0 for _ in range(16))),
                BankedByteRegisters(slots=tuple(0 for _ in range(16))),
                BankedByteRegisters(slots=tuple(slots)),
            )
            seeded = replace(
                view.machine.cpu,
                pc=0x00200040,
                rfp=0,
                register_bank=0,
                register_banks=banks,
            )
            result = build_execute_next(view, cpu_state=seeded)
        self.assertEqual(result.status, "bios-call-requires-known-register")
        self.assertIsNone(result.after_cpu)

    def test_swi_other_than_1_keeps_pc_advance_stub(self) -> None:
        # swi 3 (0xFB) is a software interrupt, not the system-call path.
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "swi3.ngc"
            _write_swi_rom(rom_path, 0x00200040, b"\xFB")  # swi 3
            view = load_fetch_view(rom_path)
            result = build_execute_next(view)
        self.assertEqual(result.decode.assembly, "swi 3")
        self.assertEqual(result.status, "executed")
        assert result.after_cpu is not None
        self.assertEqual(result.after_cpu.pc, 0x00200041)

    def test_swi1_comgetdata_no_peer_returns_buf_empty(self) -> None:
        # RW3=0x14 (COMGETDATA), no serial peer -> RA3 = 1 (COM_BUF_EMPTY).
        result = self._run_swi1({0: 0xFF, 1: 0x14})
        self.assertEqual(result.status, "executed")
        assert result.after_cpu is not None
        assert result.after_cpu.register_banks is not None
        self.assertEqual(result.after_cpu.register_banks[3].slots[0], 0x01)
        self.assertIn("COM_BUF_EMPTY", result.note)

    def test_swi1_comsendstatus_no_peer_returns_zero_count(self) -> None:
        # RW3=0x17 (COMSENDSTATUS) -> WA3 = 0 (RA3 and RW3 both cleared).
        result = self._run_swi1({0: 0xFF, 1: 0x17})
        self.assertEqual(result.status, "executed")
        assert result.after_cpu is not None
        assert result.after_cpu.register_banks is not None
        self.assertEqual(result.after_cpu.register_banks[3].slots[0], 0x00)
        self.assertEqual(result.after_cpu.register_banks[3].slots[1], 0x00)

    def test_swi1_comoffrts_sets_rts_byte(self) -> None:
        # RW3=0x16 (COMOFFRTS) -> RTS handshake byte 0x00B2 = 1.
        result = self._run_swi1({1: 0x16})
        self.assertEqual(result.status, "executed")
        self.assertEqual(len(result.memory_writes), 1)
        self.assertEqual(result.memory_writes[0].address, 0x00B2)
        self.assertEqual(result.memory_writes[0].data, bytes([0x01]))

    def test_swi1_comonrts_clears_rts_byte(self) -> None:
        # RW3=0x15 (COMONRTS) -> RTS handshake byte 0x00B2 = 0.
        result = self._run_swi1({1: 0x15})
        self.assertEqual(result.status, "executed")
        self.assertEqual(result.memory_writes[0].address, 0x00B2)
        self.assertEqual(result.memory_writes[0].data, bytes([0x00]))


class Swi1RtcGetTests(unittest.TestCase):
    # ⚡ VECT_RTCGET reads the machine's OWN clock chip at I/O 0x91-0x97, never the
    # host wall clock. So this fixture SEEDS THOSE REGISTERS instead of patching
    # time.localtime(): a console has exactly one clock, and a test that faked a
    # second one would not be exercising what the BIOS actually does. (It would
    # also make the reference core non-deterministic, which for the half of a
    # differential pair whose job is reproducibility is a defect on its own.)
    #
    # 2026-07-10 14:30:45, a Friday. 0x97 packs the leap phase in its top nibble
    # and the weekday in the low one, exactly as the chip presents it.
    _RTC_REGS = {
        0x000091: 0x26,  # year, since 2000
        0x000092: 0x07,  # month
        0x000093: 0x10,  # day
        0x000094: 0x14,  # hour
        0x000095: 0x30,  # minute
        0x000096: 0x45,  # second
        0x000097: 0x25,  # (year & 3) << 4 | weekday
    }

    def _run_rtcget(self, xhl3: int):
        # XHL3 = bank-3 XHL (r32_index 3, slots 12..15), little-endian bytes.
        slots = [0 for _ in range(16)]
        slots[1] = 0x02  # RW3 = 2 (VECT_RTCGET)
        for pos in range(4):
            slots[12 + pos] = (xhl3 >> (8 * pos)) & 0xFF
        banks = (
            BankedByteRegisters(slots=tuple(0 for _ in range(16))),
            BankedByteRegisters(slots=tuple(0 for _ in range(16))),
            BankedByteRegisters(slots=tuple(0 for _ in range(16))),
            BankedByteRegisters(slots=tuple(slots)),
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "swi.ngc"
            _write_swi_rom(rom_path, 0x00200040, b"\xF9")  # swi 1
            view = load_fetch_view(rom_path)
            seeded = replace(
                view.machine.cpu,
                pc=0x00200040,
                rfp=0,
                register_bank=0,
                register_banks=banks,
            )
            return build_execute_next(
                view, cpu_state=seeded, memory_bytes=dict(self._RTC_REGS)
            )

    def test_rtcget_writes_seven_bcd_bytes_to_buffer(self) -> None:
        result = self._run_rtcget(0x4000)
        self.assertEqual(result.status, "executed")
        self.assertEqual(len(result.memory_writes), 1)
        write = result.memory_writes[0]
        self.assertEqual(write.address, 0x4000)
        # The seven chip registers come straight across, 0x91..0x97 in order.
        self.assertEqual(
            write.data, bytes([0x26, 0x07, 0x10, 0x14, 0x30, 0x45, 0x25])
        )
        assert result.after_memory is not None
        self.assertEqual(result.after_memory[0x4000], 0x26)
        self.assertEqual(result.after_memory[0x4006], 0x25)
        assert result.after_cpu is not None
        self.assertEqual(result.after_cpu.pc, 0x00200041)

    def test_rtcget_rejects_buffer_at_or_above_0xC000(self) -> None:
        result = self._run_rtcget(0xC000)
        self.assertEqual(result.status, "executed")
        self.assertEqual(result.memory_writes, ())
        self.assertIn(">= 0xC000", result.note)
        assert result.after_cpu is not None
        self.assertEqual(result.after_cpu.pc, 0x00200041)


class Swi1FlashWriteTests(unittest.TestCase):
    def _run_flashwrite(
        self, *, bank3_slots: dict[int, int], memory_bytes: dict[int, int]
    ):
        slots = [0 for _ in range(16)]
        slots[1] = 0x06  # RW3 = 6 (VECT_FLASHWRITE)
        for slot, value in bank3_slots.items():
            slots[slot] = value
        banks = (
            BankedByteRegisters(slots=tuple(0 for _ in range(16))),
            BankedByteRegisters(slots=tuple(0 for _ in range(16))),
            BankedByteRegisters(slots=tuple(0 for _ in range(16))),
            BankedByteRegisters(slots=tuple(slots)),
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            rom_path = Path(tmpdir) / "swi.ngc"
            _write_swi_rom(rom_path, 0x00200040, b"\xF9")  # swi 1
            view = load_fetch_view(rom_path)
            seeded = replace(
                view.machine.cpu,
                pc=0x00200040,
                rfp=0,
                register_bank=0,
                register_banks=banks,
            )
            return build_execute_next(
                view, cpu_state=seeded, memory_bytes=memory_bytes
            )

    def test_flashwrite_copies_block_into_cart_window(self) -> None:
        # RA3=0 (bank 0x200000), XDE3=0xA000, XHL3=0x4000, BC3=1 (256 bytes).
        src = {0x4000 + i: (i & 0xFF) for i in range(256)}
        result = self._run_flashwrite(
            bank3_slots={
                0: 0x00,  # RA3 bank lo
                4: 0x01,  # BC3 low  = 1 unit
                5: 0x00,  # BC3 high
                8: 0x00,
                9: 0xA0,  # XDE3 = 0x0000A000
                12: 0x00,
                13: 0x40,  # XHL3 = 0x00004000
            },
            memory_bytes=src,
        )
        self.assertEqual(result.status, "executed")
        self.assertEqual(len(result.memory_writes), 1)
        write = result.memory_writes[0]
        self.assertEqual(write.address, 0x20A000)  # 0x200000 + 0xA000
        self.assertEqual(len(write.data), 256)
        assert result.after_memory is not None
        self.assertEqual(result.after_memory[0x20A000], 0x00)
        self.assertEqual(result.after_memory[0x20A001], 0x01)
        self.assertEqual(result.after_memory[0x20A0FF], 0xFF)
        # RA3 = SYS_SUCCESS
        assert result.after_cpu is not None
        assert result.after_cpu.register_banks is not None
        self.assertEqual(result.after_cpu.register_banks[3].slots[0], 0x00)
        self.assertEqual(result.after_cpu.pc, 0x00200041)

    def test_flashwrite_hi_bank_targets_0x800000(self) -> None:
        src = {0x4000 + i: 0xAB for i in range(256)}
        result = self._run_flashwrite(
            bank3_slots={0: 0x01, 4: 0x01, 8: 0x00, 9: 0x00, 12: 0x00, 13: 0x40},
            memory_bytes=src,
        )
        self.assertEqual(result.status, "executed")
        self.assertEqual(result.memory_writes[0].address, 0x800000)

    def test_flashwrite_zero_count_is_success_noop(self) -> None:
        result = self._run_flashwrite(
            bank3_slots={0: 0x00, 4: 0x00, 5: 0x00}, memory_bytes={}
        )
        self.assertEqual(result.status, "executed")
        self.assertEqual(result.memory_writes, ())
        self.assertIn("SYS_SUCCESS", result.note)

    def test_flashwrite_unread_source_stops_honestly(self) -> None:
        # Source XHL3 points into an unbacked in-region address (BIOS ROM with no
        # BIOS attached) -> honest stop rather than fabricating flash content.
        result = self._run_flashwrite(
            bank3_slots={0: 0x00, 4: 0x01, 12: 0x00, 13: 0x00, 14: 0xFF},  # XHL3=0xFF0000
            memory_bytes={},
        )
        self.assertEqual(result.status, "runtime-memory-unavailable")
        self.assertIsNone(result.after_cpu)


class Swi1SysFontSetTests(unittest.TestCase):
    """SYSFONTSET reads the REAL font out of the attached BIOS (fidelity choice:
    never substitute a different font). Nothing proprietary is embedded here --
    these tests build a synthetic BIOS image with a known glyph."""

    FONT_OFFSET = 0x8DCF
    GLYPH_A = bytes([0x10, 0x28, 0x28, 0x44, 0x7C, 0x82, 0x82, 0x00])

    def _make_bios(self, tmpdir: Path) -> Path:
        bios = bytearray(0x10000)
        bios[self.FONT_OFFSET + 0x41 * 8 : self.FONT_OFFSET + 0x41 * 8 + 8] = self.GLYPH_A
        path = tmpdir / "fake.bios"
        path.write_bytes(bytes(bios))
        return path

    def _run(self, *, with_bios: bool, ra3: int = 0x03):
        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = Path(tmp)
            rom_path = tmpdir / "swi.ngc"
            _write_swi_rom(rom_path, 0x00200040, b"\xF9")  # swi 1
            bios_path = self._make_bios(tmpdir) if with_bios else None
            view = load_fetch_view(rom_path, bios_path=bios_path)
            seeded = replace(
                view.machine.cpu,
                pc=0x00200040,
                rfp=0,
                register_bank=0,
                register_banks=_banks({0: ra3, 1: 0x05}),  # RA3=colours, RW3=5
            )
            return build_execute_next(view, cpu_state=seeded)

    def test_expand_row_packs_two_bits_per_pixel(self) -> None:
        from core.execute import _sysfont_expand_row

        # 0x10 = 0b00010000 -> only pixel 3 set. fg=3, bg=0 -> 0x0300.
        self.assertEqual(_sysfont_expand_row(0x10, fg=3, bg=0), 0x0300)
        # All pixels set with fg=1 -> every 2-bit field = 01.
        self.assertEqual(_sysfont_expand_row(0xFF, fg=1, bg=0), 0x5555)
        # No pixels set with bg=2 -> every 2-bit field = 10.
        self.assertEqual(_sysfont_expand_row(0x00, fg=3, bg=2), 0xAAAA)

    def test_sysfontset_expands_bios_font_into_char_ram(self) -> None:
        result = self._run(with_bios=True, ra3=0x03)  # fg=3, bg=0
        self.assertEqual(result.status, "executed")
        self.assertEqual(len(result.memory_writes), 1)
        write = result.memory_writes[0]
        self.assertEqual(write.address, 0x00A000)
        self.assertEqual(len(write.data), 0x1000)  # 0x800 rows x 2 bytes
        assert result.after_memory is not None
        # Glyph 0x41 row 0 (source 0x10) -> word 0x0300, little-endian, at
        # CHAR RAM offset (0x41*8 + 0) * 2 = 0x410.
        self.assertEqual(result.after_memory[0x00A410], 0x00)
        self.assertEqual(result.after_memory[0x00A411], 0x03)
        assert result.after_cpu is not None
        self.assertEqual(result.after_cpu.pc, 0x00200041)

    def test_sysfontset_without_bios_stops_honestly(self) -> None:
        # Fidelity: the glyphs live in the BIOS. With none attached we refuse to
        # fabricate a font.
        result = self._run(with_bios=False)
        self.assertEqual(result.status, "bios-font-unavailable")
        self.assertIsNone(result.after_cpu)
        self.assertIn("BIOS", result.note)

    def test_sysfontset_background_colour_from_high_nibble(self) -> None:
        result = self._run(with_bios=True, ra3=0x20)  # fg = 0, bg = 2
        self.assertEqual(result.status, "executed")
        assert result.after_memory is not None
        # Glyph 0x41 row 0 = 0x10: pixel 3 -> fg(0), all others bg(2) -> 0xAA8A.
        expected = 0
        for bit_index in range(8):
            expected = (expected << 2) & 0xFFFF
            expected |= 0 if ((0x10 >> (7 - bit_index)) & 1) else 2
        self.assertEqual(
            result.after_memory[0x00A410] | (result.after_memory[0x00A411] << 8),
            expected,
        )


if __name__ == "__main__":
    unittest.main()
