"""THE SAVE, WITHOUT bios.bin — the clean-room BIOS image's flash driver.

A Neo Geo Pocket has no save RAM: a game saves by asking the BIOS to ERASE a block of
its own cartridge and PROGRAM its slot back in. The HLE image used to leave those four
vectors on the default `ret` stub, which left RA3 holding the CARD NUMBER the caller
passed (0) — read back as SYS_SUCCESS. **Every save reported success and wrote
nothing.** These tests exist so that cannot come back quietly.

They need no Toshiba toolchain: they run the committed hle_bios/bios_hle.bin through
the native core, exactly as the emulator does when no real BIOS is present.

⚠️ EVERY SAVE TEST HERE WRITES OVER SOMETHING. A virgin chip is all 0xFF, so a program
needs no erase and a driver that only half works still appears to save. The slot is
poisoned first, on purpose.
"""

from __future__ import annotations

import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
IMAGE = REPO / "hle_bios" / "bios_hle.bin"

from core import native  # noqa: E402

XWA, XBC, XDE, XHL = 0, 1, 2, 3
CART = 0x200000
CODE, SRC = 0x004000, 0x004100
VECT_FLASHWRITE, VECT_FLASHERS = 6, 8
SYS_SUCCESS = 0
ROM_SIZE = 0x100000                  # 8 Mbit
SAVE_OFFSET, SAVE_BLOCK = 0x0FA000, 17


def _rom(size: int = ROM_SIZE) -> bytes:
    rom = bytearray(b"\xFF" * size)
    rom[0:28] = b" LICENSED BY SNK CORPORATION"
    rom[0x1C:0x20] = (0x200040).to_bytes(4, "little")
    rom[0x23] = 0x10
    rom[0x40] = 0x05                 # halt
    return bytes(rom)


class BlockMapAgreement(unittest.TestCase):
    """The image carries its own copy of the block map, because it must translate a
    block NUMBER into an address without asking the core. Two copies of one truth is
    how they drift apart, so pin them to each other."""

    def test_the_images_block_table_matches_the_cores_map(self):
        import importlib.util

        spec = importlib.util.spec_from_file_location(
            "gen_crt0", REPO / "hle_bios" / "gen_crt0.py"
        )
        gen = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(gen)

        for cap, count in ((0x080000, 11), (0x100000, 19), (0x200000, 35)):
            offs = gen.card_block_offsets(cap)
            self.assertEqual(len(offs), count, f"{cap:#x}: block count")
            # The last 64 KiB is split 32 / 8 / 8 / 16 -- and the two 8 KiB blocks in
            # the middle of that split are where every save goes.
            self.assertEqual(offs[-4:], [cap - 0x10000, cap - 0x8000,
                                         cap - 0x6000, cap - 0x4000])
            self.assertEqual(offs[0], 0)


@unittest.skipUnless(IMAGE.exists(), "hle_bios/bios_hle.bin not built")
@unittest.skipUnless(native.available(), "native core not built")
class HleBiosSave(unittest.TestCase):
    def setUp(self) -> None:
        self.m = native.NativeMachine(_rom(), bios=IMAGE.read_bytes())
        self.m.reset(bios_handoff=True)

    def tearDown(self) -> None:
        self.m.close()

    def _syscall(self, *, wa: int, bc: int = 0, de: int = 0, hl: int = 0) -> int:
        self.m.write(CODE, bytes([0xF9, 0x05]))          # swi 1 ; halt
        st = self.m.cpu(); st.pc = CODE; self.m.set_cpu(st)
        st = self.m.cpu()
        bank3 = st.regs if st.rfp == 3 else st.banks[3]
        bank3[XWA], bank3[XBC], bank3[XDE], bank3[XHL] = wa, bc, de, hl
        self.m.set_cpu(st)
        summary, _ = self.m.run(2_000_000, record=False)
        self.assertEqual(summary.stop_status, native.STATUS_HALTED,
                         "the BIOS routine never came back to the cartridge")
        st = self.m.cpu()
        bank3 = st.regs if st.rfp == 3 else st.banks[3]
        return bank3[XWA] & 0xFF                          # RA3

    def _erase(self, block: int) -> int:
        return self._syscall(wa=(VECT_FLASHERS << 8) | 0, bc=block << 8)

    def _write(self, offset: int, units: int, src: int) -> int:
        return self._syscall(wa=(VECT_FLASHWRITE << 8) | 0, bc=units, hl=src, de=offset)

    def test_a_save_reaches_the_cartridge_with_no_real_bios(self):
        payload = bytes((i * 7 + 3) & 0xFF for i in range(256))
        self.m.write(SRC, payload)
        self.m.flash_restore(CART + SAVE_OFFSET, b"\x00" * 256)   # an old save in the slot

        self.assertEqual(self._erase(SAVE_BLOCK), SYS_SUCCESS)
        self.assertEqual(self.m.read(CART + SAVE_OFFSET, 4), b"\xFF" * 4,
                         "the block was not erased")
        self.assertEqual(self._write(SAVE_OFFSET, 1, SRC), SYS_SUCCESS)
        self.assertEqual(self.m.read(CART + SAVE_OFFSET, 256), payload,
                         "the BIOS reported success and the data is not in the cartridge")
        self.assertTrue(self.m.flash_dirty(),
                        "a save that does not announce itself is never persisted")

    def test_saving_twice_over_the_same_slot(self):
        """The save that is not the first one -- the only one that needs the erase."""
        for seed in (7, 11, 23):
            payload = bytes((i * seed + 3) & 0xFF for i in range(256))
            self.m.write(SRC, payload)
            self.assertEqual(self._erase(SAVE_BLOCK), SYS_SUCCESS)
            self.assertEqual(self._write(SAVE_OFFSET, 1, SRC), SYS_SUCCESS)
            self.assertEqual(self.m.read(CART + SAVE_OFFSET, 256), payload,
                             f"save #{seed} did not stick")

    def test_a_save_bigger_than_one_unit(self):
        """RBC3 counts 256-byte units. Getting the loop wrong truncates saves."""
        payload = bytes((i * 13 + 1) & 0xFF for i in range(1024))
        self.m.write(SRC, payload)
        self.assertEqual(self._erase(SAVE_BLOCK), SYS_SUCCESS)
        self.assertEqual(self._write(SAVE_OFFSET, 4, SRC), SYS_SUCCESS)
        self.assertEqual(self.m.read(CART + SAVE_OFFSET, 1024), payload)

    def test_the_erase_stops_at_the_block_edge(self):
        """F8_B17 is 0xFA000..0xFBFFF. One byte past it belongs to the block the SDK
        reserves for the system -- an erase running on into it eats console data."""
        self.m.flash_restore(CART + 0x0FBFFF, b"\x00")
        self.m.flash_restore(CART + 0x0FC000, b"\x00")
        self.assertEqual(self._erase(SAVE_BLOCK), SYS_SUCCESS)
        self.assertEqual(self.m.read(CART + 0x0FBFFF, 1), b"\xFF", "the erase stopped short")
        self.assertEqual(self.m.read(CART + 0x0FC000, 1), b"\x00", "the erase ran into block 18")

    def test_the_dispatcher_does_not_eat_the_arguments(self):
        """FLASHWRITE takes its source in XHL3 and its offset in XDE3. The `swi 1`
        dispatcher used to do its table lookup in XHL and park its return address in
        XDE -- so the save programmed from the dispatcher's own address and every byte
        went nowhere. This is that bug, pinned: the data must come from OUR buffer."""
        payload = bytes((i * 5 + 1) & 0xFF for i in range(256))
        self.m.write(SRC, payload)
        self.assertEqual(self._erase(SAVE_BLOCK), SYS_SUCCESS)
        self.assertEqual(self._write(SAVE_OFFSET, 1, SRC), SYS_SUCCESS)
        self.assertEqual(self.m.read(CART + SAVE_OFFSET, 256), payload)

    def test_an_empty_slot_is_not_an_invented_cartridge(self):
        """On a 2 MiB cartridge, CS1 (card 1) is the development board's slot and a
        console has nothing in it -- 0x6C59 reads 0. Answering SUCCESS would be
        inventing a cartridge."""
        self.assertEqual(self.m.read(0x006C59, 1)[0], 0, "this cart should have one die")
        self.assertNotEqual(self._erase_card1(), SYS_SUCCESS)

    def _erase_card1(self) -> int:
        return self._syscall(wa=(VECT_FLASHERS << 8) | 1, bc=SAVE_BLOCK << 8)

    # --- the RTC alarm (vector 9) --------------------------------------------
    # It lives next door to the flash vectors, and that proximity was a hazard: one
    # doc numbers FLASHPROTECT 9, and 9 is ALARMSET. Wiring by that number would have
    # made a game's alarm IRREVERSIBLY protect a flash block. Hence these tests here.
    VECT_ALARMSET, VECT_ALARMDOWNSET = 9, 0x0B

    def _alarmset(self, vector: int, day: int, hour: int, minute: int) -> int:
        # QC3 = day (byte 2 of XBC3), RB3 = hour (byte 1), RC3 = minute (byte 0).
        return self._syscall(wa=(vector << 8), bc=(day << 16) | (hour << 8) | minute)

    def test_setting_an_alarm_reaches_the_clock(self):
        for vector in (self.VECT_ALARMSET, self.VECT_ALARMDOWNSET):
            with self.subTest(vector=vector):
                inte0 = self.m.read(0x000070, 1)[0]
                self.assertEqual(self._alarmset(vector, 0x15, 0x07, 0x30), SYS_SUCCESS)
                self.assertEqual(self.m.read(0x000098, 3), bytes([0x15, 0x07, 0x30]))
                self.assertTrue(self.m.read(0x000090, 1)[0] & 0x02, "the alarm is not armed")
                # Retail keeps the request in RAM too (0xFF151B): day, hour, minute, 0xFF.
                self.assertEqual(self.m.read(0x006C42, 4), bytes([0x15, 0x07, 0x30, 0xFF]))
                self.assertEqual(self.m.read(0x006C48, 1)[0], 2)
                self.assertEqual(self.m.read(0x006C46, 1)[0],
                                 0xFF if vector == self.VECT_ALARMDOWNSET else 0x00)
                # The pin it fires on must be open -- and retail does NOT touch INTE0AD to
                # get there: the hand-off already leaves INT0 at level 2. Forcing level 4
                # here was ours alone. Levels 0 and 7 are the two that prohibit.
                self.assertEqual(self.m.read(0x000070, 1)[0], inte0, "INTE0AD rewritten")
                self.assertIn(inte0 & 0x07, range(1, 7),
                              "INT0 masked: the alarm would fire into nothing")

    def test_an_alarm_is_validated_before_anything_is_written(self):
        """Retail validates every field first, in binary after its loose BCD
        conversion. Ours wrote the day to the chip, then refused the hour."""
        before = self.m.read(0x000098, 3)
        for day, hour, minute in ((0x15, 0x24, 0x30),   # hour 24
                                  (0x15, 0x07, 0x5F),   # minute 0x5F = 65 (BCD-loose)
                                  (0x00, 0x07, 0x30),   # day 0 is refused
                                  (0x2F, 0x07, 0x30)):  # day 0x2F = 35
            with self.subTest(day=day, hour=hour, minute=minute):
                self.assertNotEqual(self._alarmset(self.VECT_ALARMSET, day, hour, minute),
                                    SYS_SUCCESS)
                self.assertEqual(self.m.read(0x000098, 3), before, "half-armed alarm")

    def test_the_any_day_wildcard_becomes_the_chips_own(self):
        """The SDK spells 'every day' 0xFF; the chip spells it 0. The retail BIOS
        normalises before the register ever sees it -- an impossible day would
        otherwise mean NEVER, silently."""
        self.assertEqual(self._alarmset(self.VECT_ALARMSET, 0xFF, 0x09, 0x00), SYS_SUCCESS)
        self.assertEqual(self.m.read(0x000098, 1)[0], 0x00)

    def test_an_impossible_time_is_refused(self):
        for day, hour, minute in ((0x00, 0x07, 0x30),      # day 0 is not a day
                                  (0x15, 0x25, 0x30),      # 25 o'clock
                                  (0x15, 0x07, 0x61)):     # 61 minutes
            with self.subTest(day=day, hour=hour, minute=minute):
                self.assertEqual(self._alarmset(self.VECT_ALARMSET, day, hour, minute), 0xFF)


if __name__ == "__main__":
    unittest.main()


@unittest.skipUnless(IMAGE.exists(), "hle_bios/bios_hle.bin not built")
@unittest.skipUnless(native.available(), "native core not built")
class IntlvsetShadows(unittest.TestCase):
    """INTLVSET writes a SHADOW in work RAM, and the shadow is the source of truth.

    Measured off the retail BIOS, ten sources x two levels: with INTE45 (0x71) holding
    0xDC from the hand-off, INTLVSET(source=1, level=3) leaves it at **0xB0** -- not
    0xBC. A read-modify-write of the register would have kept that 0xC; the retail
    BIOS discards it, because it never reads the register at all. It updates its RAM
    copy and writes the whole byte out.

    We used to read the register back, which preserved a nibble silicon would have
    lost -- and left the shadow, which games do read (0x6C27: Delta Warp, Ganbare, Neo
    Turf Masters; 0x6C28: Sonic, Faselei!, KOF Battle de Paradise), permanently zero.

        source -> register / shadow
        0,1 -> 0x70,0x71 / 0x6C24,0x6C25    2,3 -> 0x73 / 0x6C27
        4,5 -> 0x74      / 0x6C28           6,7 -> 0x79 / 0x6C2A    8,9 -> 0x7A / 0x6C2B
    """

    MAP = {0: (0x70, 0x6C24), 1: (0x71, 0x6C25), 2: (0x73, 0x6C27), 3: (0x73, 0x6C27),
           4: (0x74, 0x6C28), 5: (0x74, 0x6C28), 6: (0x79, 0x6C2A), 7: (0x79, 0x6C2A),
           8: (0x7A, 0x6C2B), 9: (0x7A, 0x6C2B)}

    def setUp(self) -> None:
        self.m = native.NativeMachine(_rom(), bios=IMAGE.read_bytes())
        self.m.reset(bios_handoff=True)

    def tearDown(self) -> None:
        self.m.close()

    def _intlvset(self, source: int, level: int) -> None:
        self.m.write(CODE, bytes([0xF9, 0x05]))
        st = self.m.cpu(); st.pc = CODE; self.m.set_cpu(st)
        st = self.m.cpu()
        bank3 = st.regs if st.rfp == 3 else st.banks[3]
        bank3[XWA] = 0x04 << 8                       # RW3 = INTLVSET
        bank3[XBC] = (level << 8) | source           # RB3 = level, RC3 = source
        self.m.set_cpu(st)
        self.m.run(2_000_000, record=False)

    def test_register_and_shadow_agree_for_every_source(self):
        """Checked NIBBLE by nibble, not byte by byte: two sources share one register,
        so a pair accumulates in the shadow (which is the point of the next test)."""
        for source, (reg, shadow) in self.MAP.items():
            with self.subTest(source=source):
                self._intlvset(source, 3)
                nibble = 0x08 | 3
                got_reg, got_shadow = self.m.read(reg, 1)[0], self.m.read(shadow, 1)[0]
                pick = (lambda v: v >> 4) if source & 1 else (lambda v: v & 0x0F)
                self.assertEqual(pick(got_reg), nibble)
                self.assertEqual(pick(got_shadow), nibble,
                                 "the shadow games read was not updated")
                self.assertEqual(got_reg, got_shadow,
                                 "the register and its shadow drifted apart")

    def test_the_shadow_is_the_source_of_truth_not_the_register(self):
        """INTE45 carries VBlank's level in its low nibble and the hand-off leaves it
        at 0xDC. Setting source 1 must yield 0xB0 -- the retail BIOS's answer -- and
        NOT 0xBC, which is what reading the register back would produce."""
        self.assertEqual(self.m.read(0x71, 1)[0], 0xDC, "the hand-off value moved")
        self._intlvset(1, 3)
        self.assertEqual(self.m.read(0x71, 1)[0], 0xB0)

    def test_two_sources_sharing_a_register_accumulate(self):
        """Sources 2 and 3 share 0x73. Faselei! shows the retail sequence 0x0B then
        0xAB: the second call keeps the first's nibble, because both went through the
        same shadow."""
        self._intlvset(2, 3)
        self.assertEqual(self.m.read(0x6C27, 1)[0], 0x0B)
        self._intlvset(3, 2)
        self.assertEqual(self.m.read(0x6C27, 1)[0], 0xAB)
        self.assertEqual(self.m.read(0x73, 1)[0], 0xAB)


@unittest.skipUnless(IMAGE.exists(), "hle_bios/bios_hle.bin not built")
@unittest.skipUnless(native.available(), "native core not built")
class TwoDieCartridges(unittest.TestCase):
    """A 4 MiB CARTRIDGE IS TWO CHIPS, and the second one lives at 0x800000.

    That window is the development slot on a 2 MiB cart -- which is why this driver
    refused card 1 outright. Measured against the corpus, that reasoning is wrong for
    the three 4 MiB carts (Metal Slug 2nd Mission, Densha de Go! 2, SvC Match of the
    Millennium): the retail BIOS writes **0x6C59 = 3** for them, because there really
    is a cartridge there. Refusing card 1 failed a save on exactly the carts with two
    chips to save on.

    So the test is not the card NUMBER, it is whether that slot holds a cartridge.
    """

    def setUp(self) -> None:
        rom = bytearray(_rom(0x400000))          # 4 MiB: two full dies
        self.m = native.NativeMachine(bytes(rom), bios=IMAGE.read_bytes())
        self.m.reset(bios_handoff=True)

    def tearDown(self) -> None:
        self.m.close()

    def _syscall(self, *, wa, bc=0, de=0, hl=0):
        self.m.write(CODE, bytes([0xF9, 0x05]))
        st = self.m.cpu(); st.pc = CODE; self.m.set_cpu(st)
        st = self.m.cpu()
        b3 = st.regs if st.rfp == 3 else st.banks[3]
        b3[XWA], b3[XBC], b3[XDE], b3[XHL] = wa, bc, de, hl
        self.m.set_cpu(st)
        self.m.run(4_000_000, record=False)
        st = self.m.cpu()
        b3 = st.regs if st.rfp == 3 else st.banks[3]
        return b3[XWA] & 0xFF

    def test_both_dies_are_announced(self):
        self.assertEqual(self.m.read(0x006C58, 1)[0], 3)
        self.assertEqual(self.m.read(0x006C59, 1)[0], 3, "the second die is a cartridge too")

    def test_a_save_reaches_either_die(self):
        payload = bytes((i * 11 + 5) & 0xFF for i in range(256))
        self.m.write(SRC, payload)
        for card, base in ((0, 0x200000), (1, 0x800000)):
            with self.subTest(card=card):
                self.m.flash_restore(base + 0x1FA000, bytes(256))
                self.assertEqual(self._syscall(wa=(VECT_FLASHERS << 8) | card,
                                               bc=33 << 8), SYS_SUCCESS)
                self.assertEqual(self._syscall(wa=(VECT_FLASHWRITE << 8) | card, bc=1,
                                               hl=SRC, de=0x1FA000), SYS_SUCCESS)
                self.assertEqual(self.m.read(base + 0x1FA000, 256), payload,
                                 f"card {card} reported success and saved nothing")

    def test_a_third_card_number_is_still_refused(self):
        self.assertNotEqual(self._syscall(wa=(VECT_FLASHERS << 8) | 2, bc=33 << 8),
                            SYS_SUCCESS)
