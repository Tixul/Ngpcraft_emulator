"""THE IN-GAME SAVE, through the door a real game actually uses.

A Neo Geo Pocket game does not drive the flash chip. It calls the BIOS:

    ld rw3, VECT_FLASHERS    ; 8    -- erase the block
    ld ra3, 0                ;         card 0 (0x200000)
    ld rb3, BLOCK_NB
    swi 1

    ld rw3, VECT_FLASHWRITE  ; 6    -- write the data
    ld ra3, 0
    ld rbc3, 1               ;         1 unit = 256 bytes
    ld xhl3, source
    ld xde3, offset_in_cart
    swi 1

(SNK SysCall.txt; the vector numbers are SNK's own SYSTEM.INC, and this is verbatim
what 02_CODE_PATTERNS/.../rom/flash.c does. Note the SDK's ngpc.h in this RAG has
`#define VECT_FLASHWRITE` with NO VALUE -- trusting it would have called vector 0,
which is SHUTDOWN.)

So `swi 1` runs the REAL BIOS routine, which issues the real AMD command cycles at
the real flash chip. This test asserts the WHOLE chain, and it is the only test here
that can: an emulator can have a flawless flash chip and still lose every save,
which is precisely what this one did.

⚠️ IT FAILED FOR A REASON WORTH REMEMBERING. The BIOS reads a byte of its own work
RAM (0x6C58) that records which cartridge it found at power-on, and returns error
0xFF without touching the chip if it is zero. Our hand-off skips the BIOS boot, so
that byte was never written. Every layer below was correct and the save still went
nowhere -- the failure was in a byte nobody had thought to hand over.
"""

from __future__ import annotations

import unittest
from pathlib import Path

from core import native

BIOS_PATH = Path(__file__).resolve().parents[3] / "jeux officiel" / "bios_v10.bin"

XWA, XBC, XDE, XHL = 0, 1, 2, 3
CART = 0x200000
ROM_SIZE = 0x100000                 # 8 Mbit
SAVE_OFFSET = 0xFA000               # F8_B17: an 8K block (SDK FlashMem.txt)
SAVE_BLOCK = 17
CODE = 0x004000                     # a `swi 1` in work RAM, and somewhere to land after
SRC = 0x004100

VECT_FLASHWRITE = 6
VECT_FLASHERS = 8
SYS_SUCCESS = 0


def _rom(size: int = ROM_SIZE) -> bytes:
    rom = bytearray(b"\xFF" * size)   # erased flash
    rom[0:28] = b" LICENSED BY SNK CORPORATION"
    rom[0x1C:0x20] = (0x200040).to_bytes(4, "little")
    rom[0x23] = 0x10
    rom[0x40] = 0x05                  # halt
    return bytes(rom)


@unittest.skipUnless(native.available(), "native core not built")
@unittest.skipUnless(BIOS_PATH.exists(), "the retail BIOS is what we are testing against")
class BiosFlashSyscallTests(unittest.TestCase):
    def setUp(self) -> None:
        self.m = native.NativeMachine(_rom(), bios=BIOS_PATH.read_bytes())
        self.m.reset(bios_handoff=True)

    def tearDown(self) -> None:
        self.m.close()

    def _syscall(self, *, wa: int, bc: int = 0, de: int = 0, hl: int = 0) -> int:
        """Make the call a game makes, and give back RA3 -- the BIOS's own verdict."""
        self.m.write(CODE, bytes([0xF9, 0x05]))        # swi 1 ; halt
        st = self.m.cpu()
        st.pc = CODE
        self.m.set_cpu(st)

        st = self.m.cpu()
        bank3 = st.regs if st.rfp == 3 else st.banks[3]
        bank3[XWA], bank3[XBC], bank3[XDE], bank3[XHL] = wa, bc, de, hl
        self.m.set_cpu(st)

        summary, _ = self.m.run(2_000_000, record=False)
        self.assertEqual(
            summary.stop_status, native.STATUS_HALTED,
            "the BIOS routine never came back to the cartridge",
        )
        st = self.m.cpu()
        bank3 = st.regs if st.rfp == 3 else st.banks[3]
        return bank3[XWA] & 0xFF                        # RA3

    def _erase(self, block: int) -> int:
        # RW3 = vector, RA3 = card 0  ->  XWA3 = (vector << 8) | card
        # RB3 = block number, and B is the HIGH byte of BC.
        return self._syscall(wa=(VECT_FLASHERS << 8) | 0, bc=block << 8)

    def _write(self, offset: int, units: int, src: int) -> int:
        return self._syscall(wa=(VECT_FLASHWRITE << 8) | 0, bc=units, hl=src, de=offset)

    def test_the_bios_knows_which_cartridge_is_in_the_slot(self) -> None:
        """1 = 4 Mbit, 2 = 8 Mbit, 3 = 16 Mbit, 0 = no card. Measured off the real boot."""
        for size, code in ((0x080000, 1), (0x100000, 2), (0x200000, 3)):
            with self.subTest(size=size):
                with native.NativeMachine(_rom(size), bios=BIOS_PATH.read_bytes()) as m:
                    m.reset(bios_handoff=True)
                    self.assertEqual(m.read(0x006C58, 1)[0], code)

    def test_the_development_slot_is_EMPTY_on_a_production_console(self) -> None:
        """CS1 (0x800000) is the dev board's slot. Nothing is plugged into it.

        We used to answer its autoselect probe with chip 0's own size, and the BIOS
        duly wrote down that a second cartridge was present -- a cartridge we invented.
        """
        self.assertEqual(self.m.read(0x006C59, 1)[0], 0, "we invented a second cartridge")

    def test_a_game_saves_and_the_bytes_are_in_the_cartridge(self) -> None:
        payload = bytes((i * 7 + 3) & 0xFF for i in range(256))
        self.m.write(SRC, payload)

        self.assertEqual(self._erase(SAVE_BLOCK), SYS_SUCCESS, "VECT_FLASHERS refused")
        self.assertEqual(
            self.m.read(CART + SAVE_OFFSET, 4), b"\xFF" * 4, "the block was not erased"
        )

        self.assertEqual(self._write(SAVE_OFFSET, 1, SRC), SYS_SUCCESS, "VECT_FLASHWRITE refused")
        self.assertEqual(
            self.m.read(CART + SAVE_OFFSET, 256), payload,
            "the BIOS reported success and the data is not in the cartridge",
        )
        self.assertTrue(self.m.flash_dirty(), "a save that does not announce itself is never persisted")

    def test_the_erase_is_a_WHOLE_BLOCK_and_stops_at_its_edge(self) -> None:
        """The block map is the manufacturer's, and the BIOS trusts it.

        F8_B17 is 0xFA000..0xFBFFF. One byte past the end belongs to F8_B18, which the
        SDK reserves for the system -- an erase that ran on into it would be eating the
        console's own data.
        """
        self.m.flash_restore(CART + 0xFBFFF, b"\x00")     # last byte of the block
        self.m.flash_restore(CART + 0xFC000, b"\x00")     # first byte of the NEXT one

        self.assertEqual(self._erase(SAVE_BLOCK), SYS_SUCCESS)
        self.assertEqual(self.m.read(CART + 0xFBFFF, 1), b"\xFF", "the erase stopped short")
        self.assertEqual(self.m.read(CART + 0xFC000, 1), b"\x00", "the erase ran into block 18")

    def test_a_save_bigger_than_one_unit(self) -> None:
        """RBC3 counts 256-byte units, so 4 means 1 KiB. Getting this wrong truncates saves."""
        payload = bytes((i * 13 + 1) & 0xFF for i in range(1024))
        self.m.write(SRC, payload)
        self.assertEqual(self._erase(SAVE_BLOCK), SYS_SUCCESS)
        self.assertEqual(self._write(SAVE_OFFSET, 4, SRC), SYS_SUCCESS)
        self.assertEqual(self.m.read(CART + SAVE_OFFSET, 1024), payload)


if __name__ == "__main__":
    unittest.main()
