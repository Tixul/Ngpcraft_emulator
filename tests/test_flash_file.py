"""The save survives the emulator being CLOSED. That is the whole point.

A flash chip that only lives in RAM loses the save on exit exactly as completely as
the old code that swallowed the writes -- the player cannot tell the two failures
apart, and both cost them their game.

The container is the de-facto `.flash` format the NGP scene already shares
(`core/flash_file.py`), so these tests pin the on-disk bytes as well as the round
trip: getting the struct padding wrong would produce a file we read back perfectly
and nothing else can read at all.
"""

from __future__ import annotations

import struct
import tempfile
import unittest
from pathlib import Path

from core import flash_file, native
from core.native_session import NativeSession

BIOS_PATH = Path(__file__).resolve().parents[3] / "jeux officiel" / "bios_v10.bin"

CART = flash_file.CART_BASE
ROM_SIZE = 0x100000
SAVE_OFFSET = 0xFA000
SAVE_BLOCK = 17
CODE, SRC = 0x004000, 0x004100
VECT_FLASHWRITE, VECT_FLASHERS = 6, 8
XWA, XBC, XDE, XHL = 0, 1, 2, 3


def _rom(size: int = ROM_SIZE) -> bytes:
    rom = bytearray(b"\xFF" * size)
    rom[0:28] = b" LICENSED BY SNK CORPORATION"
    rom[0x1C:0x20] = (0x200040).to_bytes(4, "little")
    rom[0x23] = 0x10
    rom[0x40] = 0x05
    return bytes(rom)


class FlashFileFormatTests(unittest.TestCase):
    """The bytes on disk. Other emulators read this file; the layout is not ours to drift."""

    def test_the_block_header_carries_its_C_STRUCT_PADDING(self) -> None:
        """u32 + u16 aligns to 8, and the original writer memcpy'd the struct straight in.

        Pack it to 6 bytes and the file is unreadable by every other emulator -- and,
        worse, still perfectly readable by us, so nothing would ever fail here.
        """
        blob = flash_file.pack([(CART + 0x1000, b"\xAB" * 4)])
        self.assertEqual(len(blob), 8 + 8 + 4)
        magic, count, total = struct.unpack_from("<HHI", blob)
        self.assertEqual((magic, count, total), (0x0053, 1, 20))
        address, length = struct.unpack_from("<IH", blob, 8)
        self.assertEqual((address, length), (CART + 0x1000, 4))
        self.assertEqual(blob[16:], b"\xAB" * 4, "the data must start at offset 16, not 14")

    def test_round_trip(self) -> None:
        blocks = [(CART + 0x1000, b"\x01\x02\x03\x04"), (CART + 0xFA000, bytes(range(64)))]
        self.assertEqual(flash_file.unpack(flash_file.pack(blocks)), blocks)

    def test_a_corrupt_file_is_REFUSED_not_half_applied(self) -> None:
        """Half a save is worse than none: it looks like a working one."""
        good = flash_file.pack([(CART, b"\xAA" * 256)])
        with self.assertRaises(flash_file.BadFlashFile):
            flash_file.unpack(b"not a flash file at all")
        with self.assertRaises(flash_file.BadFlashFile):
            flash_file.unpack(good[:-10])                    # truncated in the data
        with self.assertRaises(flash_file.BadFlashFile):
            flash_file.unpack(good[:12])                     # truncated in a block header

    def test_the_diff_records_an_ERASE_as_well_as_a_write(self) -> None:
        """An erase turns data into 0xFF. If that is not saved, reloading the cartridge
        hands the game back the data it deliberately destroyed."""
        original = bytes(256) + b"\xDE\xAD" + bytes(254)
        current = bytes(256) + b"\xFF\xFF" + bytes(254)
        blocks = flash_file.diff_blocks(original, current)
        self.assertEqual(len(blocks), 1)
        self.assertEqual(blocks[0][0], CART + 0x100, "the 256-byte granule was not found")

    def test_nothing_changed_means_no_file(self) -> None:
        rom = _rom()
        self.assertEqual(flash_file.diff_blocks(rom, rom), [])
        self.assertEqual(flash_file.pack([]), b"")

    def test_a_long_run_is_split_to_fit_the_u16_length(self) -> None:
        original = b"\x00" * 0x20000
        current = b"\xFF" * 0x20000
        for _, data in flash_file.diff_blocks(original, current):
            self.assertLessEqual(len(data), 0xFFFF, "a block cannot state its own length")


@unittest.skipUnless(native.available(), "native core not built")
@unittest.skipUnless(BIOS_PATH.exists(), "an in-game save needs the BIOS: it goes through swi 1")
class SaveSurvivesTheEmulatorTests(unittest.TestCase):
    """End to end: a game saves, the emulator EXITS, and the save is still there."""

    def _save_in_game(self, session: NativeSession, payload: bytes) -> None:
        m = session.machine
        m.write(SRC, payload)
        for wa, bc, de, hl in (
            ((VECT_FLASHERS << 8), SAVE_BLOCK << 8, 0, 0),
            ((VECT_FLASHWRITE << 8), len(payload) // 256, SAVE_OFFSET, SRC),
        ):
            m.write(CODE, bytes([0xF9, 0x05]))          # swi 1 ; halt
            st = m.cpu()
            st.pc = CODE
            m.set_cpu(st)
            st = m.cpu()
            b3 = st.regs if st.rfp == 3 else st.banks[3]
            b3[XWA], b3[XBC], b3[XDE], b3[XHL] = wa, bc, de, hl
            m.set_cpu(st)
            summary, _ = m.run(2_000_000, record=False)
            self.assertEqual(summary.stop_status, native.STATUS_HALTED)
            st = m.cpu()
            b3 = st.regs if st.rfp == 3 else st.banks[3]
            self.assertEqual(b3[XWA] & 0xFF, 0, "the BIOS refused the save")

    def test_save_close_reopen(self) -> None:
        # The save lives IN the .ngc: a cartridge IS its file. Writing it back in place is
        # what the flash chip on a real cartridge does, so a copy of the ROM is one cartridge.
        payload = bytes((i * 11 + 5) & 0xFF for i in range(256))
        with tempfile.TemporaryDirectory() as tmp:
            rom_path = Path(tmp) / "game.ngc"
            rom_path.write_bytes(_rom())
            pristine = rom_path.read_bytes()

            with NativeSession(rom_path, bios_path=BIOS_PATH) as s:
                self._save_in_game(s, payload)
            # ...and on the way out it went INTO the cartridge file.
            self.assertNotEqual(rom_path.read_bytes(), pristine,
                                "the save was not written into the ROM")

            # Put the same cartridge back in: the save is already on it.
            with NativeSession(rom_path, bios_path=BIOS_PATH) as s:
                self.assertEqual(
                    s.machine.read(CART + SAVE_OFFSET, 256), payload,
                    "the save was written into the ROM and did not come back",
                )

    def test_save_also_written_beside_the_rom_when_sidecar_on(self) -> None:
        payload = bytes((i * 7 + 3) & 0xFF for i in range(256))
        with tempfile.TemporaryDirectory() as tmp:
            rom_path = Path(tmp) / "game.ngc"
            rom_path.write_bytes(_rom())
            save_path = Path(tmp) / "game.flash"
            with NativeSession(rom_path, bios_path=BIOS_PATH,
                               save_path=save_path, sidecar=True) as s:
                self._save_in_game(s, payload)
            self.assertTrue(save_path.exists(), "the optional sidecar was not written")

    def test_a_game_that_never_saved_leaves_NO_file_behind(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            rom_path = Path(tmp) / "game.ngc"
            rom_path.write_bytes(_rom())
            save_path = Path(tmp) / "game.flash"
            with NativeSession(rom_path, bios_path=BIOS_PATH, save_path=save_path):
                pass
            self.assertFalse(save_path.exists(), "an empty save file is a lie about the cartridge")

    def test_a_REBOOT_does_not_eat_the_save(self) -> None:
        """The POWER switch is not a factory reset.

        `reset()` reloads the pristine ROM image from disk, so the naive reboot wipes the
        save the player made two minutes ago -- silently, and with the cartridge still
        sitting in the slot. On the console the flash is NON-VOLATILE; that is the entire
        reason a save exists.
        """
        with tempfile.TemporaryDirectory() as tmp:
            rom_path = Path(tmp) / "game.ngc"
            rom_path.write_bytes(_rom())
            payload = bytes((i * 3 + 9) & 0xFF for i in range(256))

            with NativeSession(rom_path, bios_path=BIOS_PATH,
                               save_path=Path(tmp) / "g.flash", autosave=False) as s:
                self._save_in_game(s, payload)
                self.assertEqual(s.machine.read(CART + SAVE_OFFSET, 256), payload)

                s.reboot()

                self.assertEqual(
                    s.machine.read(CART + SAVE_OFFSET, 256), payload,
                    "the reboot wiped the cartridge -- a power cycle is not a factory reset",
                )
                self.assertTrue(s.machine.flash_dirty(),
                                "the save must still be worth writing after a reboot")

    def test_a_probe_run_does_not_write_saves(self) -> None:
        """corpus_check / triage boot dozens of ROMs. They are observers, not players."""
        with tempfile.TemporaryDirectory() as tmp:
            rom_path = Path(tmp) / "game.ngc"
            rom_path.write_bytes(_rom())
            save_path = Path(tmp) / "game.flash"
            with NativeSession(
                rom_path, bios_path=BIOS_PATH, save_path=save_path, autosave=False
            ) as s:
                self._save_in_game(s, b"\x5A" * 256)
            self.assertFalse(save_path.exists(), "a probe wrote a save file")


RETAIL = BIOS_PATH.parent / "Puzzle Link 2 (USA, Europe).ngc"


@unittest.skipUnless(native.available(), "native core not built")
@unittest.skipUnless(BIOS_PATH.exists() and RETAIL.exists(), "needs the retail BIOS + cartridge")
class ARealGameSavesTests(unittest.TestCase):
    """A COMMERCIAL cartridge, its own code, no synthetic anything.

    Puzzle Link 2 initialises its save area on first boot with no player input, so the
    whole path -- game -> `swi 1` -> BIOS flash routine -> AMD command cycles -> chip ->
    `.flash` on disk -> back into the cartridge -> the game reads it -- runs unattended.

    The assertion is the one that cannot be faked: on the second boot the game must
    behave DIFFERENTLY than it does on a blank cartridge. It bumps a counter at
    0x2F8000 from 0 to 1. It can only do that if it saw the save.
    """

    FRAMES = 900
    COUNTER = 0x2F8000

    def _boot(self, cart: Path, autosave: bool) -> bytes:
        with NativeSession(cart, bios_path=BIOS_PATH, autosave=autosave) as s:
            s.run_frames(self.FRAMES)
            return s.machine.read(CART, cart.stat().st_size)

    def test_the_game_reads_back_the_save_we_persisted(self) -> None:
        # This test needs a PRISTINE Puzzle Link 2 (no save on it). In ROM save-mode the
        # cartridge file carries its own save, so a dump that has already been played shows
        # a save from the first boot -- re-copy a clean dump to run this.
        if b"_EXIST2" in RETAIL.read_bytes()[self.COUNTER - CART:0x100000]:
            self.skipTest("the retail Puzzle Link 2 dump already carries a save")
        with tempfile.TemporaryDirectory() as tmp:
            cart = Path(tmp) / "pl2.ngc"; cart.write_bytes(RETAIL.read_bytes())
            new_cart = Path(tmp) / "pl2_fresh.ngc"; new_cart.write_bytes(RETAIL.read_bytes())
            pristine = cart.read_bytes()

            first = self._boot(cart, autosave=True)            # PL2 saves INTO cart.ngc
            self.assertNotEqual(cart.read_bytes(), pristine,
                                "the game saved and the cartridge (ROM) kept nothing")

            second = self._boot(cart, autosave=False)          # same cartridge back in
            fresh = self._boot(new_cart, autosave=False)       # a NEW, blank cartridge

            self.assertEqual(first, fresh, "the first boot is not a deterministic init")
            self.assertNotEqual(
                second, fresh,
                "the second boot behaved exactly like a blank cartridge: it never saw the save",
            )
            i = self.COUNTER - CART
            self.assertEqual((first[i], second[i]), (0, 1), "the game's own counter did not advance")


if __name__ == "__main__":
    unittest.main()
