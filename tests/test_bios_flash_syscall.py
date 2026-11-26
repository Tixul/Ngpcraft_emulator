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
what the C template's rom/flash.c does. Note the SDK's ngpc.h has
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

# ⚠️ A BIOS looked up outside the checkout made every test in this file skip -- silently,
# and a skip reads as a pass in the summary line. Read the repository's own `bios.bin`.
from rom_paths import BIOS as _BIOS
BIOS_PATH = _BIOS

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


def _rom(size: int = ROM_SIZE, *, filled: bool = False) -> bytes:
    """A cartridge image. `filled` puts DATA all the way to the top.

    ⚠️ AN ALL-0xFF "IMAGE" IS NOT AN IMAGE. 0xFF is what an ERASED cell reads as, so a
    file full of it is indistinguishable from a chip nobody filled to the top -- which
    is exactly how the core now tells a real 2 MiB cartridge from a 512 KiB one that a
    previous version padded out to 2 MiB. A test that means "this really is a full
    16 Mbit cart" has to write bytes up there, or it is asserting about a cart that
    does not exist.
    """
    rom = bytearray(b"\xFF" * size)   # erased flash
    if filled:
        rom[:] = bytes((i * 7 + 1) & 0xFF for i in range(size))
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


@unittest.skipUnless(native.available(), "native core not built")
@unittest.skipUnless(BIOS_PATH.exists(), "the retail BIOS is what we are testing against")
class CartridgeIdentityTests(unittest.TestCase):
    """WHERE THE CONSOLE GETS THE CART'S SIZE FROM -- and where WE get it from.

    A console reads it off the chip: the autoselect probe answers a device ID that names
    the size, and the BIOS writes it down at 0x6C58. A ROM FILE HAS THROWN THAT AWAY --
    the image is whatever was burned on the part, and the part is as big as the publisher
    chose (Delta Warp: 512 KiB of ROM on an 8 Mbit chip). So we have to recover it, and
    the only witness left is the cartridge's own SAVE REQUEST.

    The sharp signal is the BLOCK NUMBER a game hands the BIOS. It comes from the SDK
    table for ITS card, so it names the card -- but only until the BIOS turns it into an
    address, which is why it is read at the `swi 1`.

    ⚠️ EVERY TEST HERE SAVES AT LEAST TWICE. A virgin chip is all 0xFF, so the FIRST
    program needs no erase and any geometry appears to work. The mechanism only engages
    on the second save. That false green has been manufactured here once already.
    """

    @staticmethod
    def _machine(image: int, presented: int, *, filled: bool = False):
        m = native.NativeMachine(_rom(image, filled=filled), bios=BIOS_PATH.read_bytes())
        m.set_flash_size(presented)
        m.reset(bios_handoff=True)
        return m

    def _syscall(self, m, *, wa: int, bc: int = 0, de: int = 0, hl: int = 0) -> int:
        m.write(CODE, bytes([0xF9, 0x05]))
        st = m.cpu(); st.pc = CODE; m.set_cpu(st)
        st = m.cpu()
        bank3 = st.regs if st.rfp == 3 else st.banks[3]
        bank3[XWA], bank3[XBC], bank3[XDE], bank3[XHL] = wa, bc, de, hl
        m.set_cpu(st)
        summary, _ = m.run(2_000_000, record=False)
        self.assertEqual(summary.stop_status, native.STATUS_HALTED)
        st = m.cpu()
        bank3 = st.regs if st.rfp == 3 else st.banks[3]
        return bank3[XWA] & 0xFF

    def _save(self, m, block: int, offset: int, seed: int):
        payload = bytes((i * seed + 3) & 0xFF for i in range(256))
        m.write(SRC, payload)
        self.assertEqual(self._syscall(m, wa=(VECT_FLASHERS << 8), bc=block << 8), SYS_SUCCESS)
        self.assertEqual(
            self._syscall(m, wa=(VECT_FLASHWRITE << 8), bc=1, hl=SRC, de=offset), SYS_SUCCESS
        )
        return m.read(CART + offset, 256) == payload

    def test_the_block_number_names_the_card_BEFORE_the_erase_moves(self) -> None:
        """The whole point: the identity is known before the BIOS computes an address.

        Block 17 is 0xFA000 on an 8 Mbit card and 0x110000 on a 16 Mbit one. Learning it
        one step later -- at the program, which is what the address rule can do -- leaves
        the erase at 0x110000 and the slot never gets cleared.
        """
        with self._machine(0x080000, 0x200000) as m:
            self.assertEqual(m.read(0x006C58, 1)[0], 3, "presented as a 16 Mbit card")
            m.flash_restore(CART + SAVE_OFFSET, b"\x00" * 4)      # a slot with a save in it
            self.assertEqual(self._syscall(m, wa=(VECT_FLASHERS << 8), bc=17 << 8), SYS_SUCCESS)
            self.assertEqual(m.read(0x006C58, 1)[0], 2, "the cart asked for an 8 Mbit block")
            self.assertEqual(m.read(CART + SAVE_OFFSET, 4), b"\xFF" * 4,
                             "the erase went where the 16 Mbit table said, not where the cart lives")

    def test_a_save_slot_that_is_not_the_second_8k_block(self) -> None:
        """0xF8000 / 0xF9F00 / 0xFBF00 are all in the retail corpus (specs/FLASH.md §6).

        The address rule only recognised `capacity - 0x6000`, so these games learnt
        nothing, saved once on the virgin chip, and failed every time after. THIS is the
        case that made the user reach for the manual size setting.
        """
        with self._machine(0x080000, 0x200000) as m:
            for n, seed in enumerate((7, 11, 23), 1):
                self.assertTrue(self._save(m, 16, 0x0F8000, seed), f"save #{n} did not stick")
            self.assertEqual(m.read(0x006C58, 1)[0], 2)

    def test_a_card_BIGGER_than_the_image_is_reachable(self) -> None:
        """StarGunner: a small homebrew on a 16 Mbit part, saving in block 33.

        A block number ABOVE the presented map is a card bigger than the one we show, and
        growing is the safe direction -- the space above the image is erased 0xFF anyway.
        """
        with self._machine(0x080000, 0x080000) as m:
            self.assertEqual(m.read(0x006C58, 1)[0], 1)
            for seed in (7, 11):
                self.assertTrue(self._save(m, 33, 0x1FA000, seed))
            self.assertEqual(m.read(0x006C58, 1)[0], 3)

    def test_a_card_that_would_CUT_INTO_the_rom_is_refused(self) -> None:
        """Shrinking moves the save DOWN, into the game's own code -- and the erase that
        follows would take 8 KiB of it with it. A capacity smaller than the image burned
        on the part is not a cartridge, so it is refused outright."""
        with self._machine(0x200000, 0x200000, filled=True) as m:
            self._syscall(m, wa=(VECT_FLASHERS << 8), bc=17 << 8)
            self.assertEqual(m.read(0x006C58, 1)[0], 3, "a 2 MiB image cannot be an 8 Mbit card")

    def test_an_ordinary_block_teaches_nothing(self) -> None:
        """Only the two 8 KiB save blocks of a card carry the signal. Block 5 is a plain
        64 KiB block on all three, so it says nothing and must change nothing."""
        with self._machine(0x080000, 0x200000) as m:
            self._syscall(m, wa=(VECT_FLASHERS << 8), bc=5 << 8)
            self.assertEqual(m.read(0x006C58, 1)[0], 3)


if __name__ == "__main__":
    unittest.main()


@unittest.skipUnless(native.available(), "native core not built")
@unittest.skipUnless(BIOS_PATH.exists(), "the retail BIOS is what we are testing against")
class CardTypeAndBlockMapAgree(unittest.TestCase):
    """THE CARD-TYPE BYTE AND THE BLOCK MAP ARE TWO HALVES OF ONE ANSWER.

    The BIOS turns a block NUMBER into an ADDRESS using the byte at 0x6C58; the chip
    decides how MUCH to erase from its block map. Let those drift and a game asking
    for its 8 KiB save block gets **64 KiB of its own ROM erased** -- measured, not
    imagined -- and with `save_to_rom` on (the default) that reaches the .ngc.

    Found by an adversarial pass, not by breakage: the one path that refused a resize
    used to return leaving a stale byte behind. Every exit now restates it.
    """

    def _machine(self, image: int, presented: int, *, filled: bool = True):
        # `filled` defaults ON here: every case in this class is about a cartridge
        # whose image really does reach the top. An erased tail is not image.
        m = native.NativeMachine(_rom(image, filled=filled), bios=BIOS_PATH.read_bytes())
        m.set_flash_size(presented)
        m.reset(bios_handoff=True)
        return m

    def test_a_refused_resize_still_leaves_the_byte_describing_the_map(self):
        """A 2 MiB image cannot be an 8 Mbit card, so the resize is refused -- and the
        byte must not be left claiming otherwise."""
        with self._machine(0x200000, 0x200000) as m:
            m.write(0x006C58, b"\x02")                     # a byte that lies
            self._syscall_erase(m, 17)
            self.assertEqual(m.read(0x006C58, 1)[0], 3,
                             "the refused resize left a stale card type behind")

    def test_the_erase_follows_the_corrected_geometry(self):
        """With the byte corrected before the BIOS translates the block number, the
        erase lands on block 17 OF A 16 MBIT CARD (0x110000) -- not on the 8 Mbit
        card's 0xFA000, which on a 2 MiB image is 64 KiB of game code."""
        with self._machine(0x200000, 0x200000) as m:
            for base in (0x0F0000, 0x110000):
                m.flash_restore(CART + base, bytes(0x10000))
            m.write(0x006C58, b"\x02")
            self._syscall_erase(m, 17)
            game_data = m.read(CART + 0x0F0000, 0x10000)
            self.assertNotIn(0xFF, game_data, "64 KiB of the game's own ROM was erased")
            self.assertEqual(m.read(CART + 0x110000, 0x10000), b"\xFF" * 0x10000)

    def _syscall_erase(self, m, block: int) -> None:
        m.write(CODE, bytes([0xF9, 0x05]))
        st = m.cpu(); st.pc = CODE; m.set_cpu(st)
        st = m.cpu()
        bank3 = st.regs if st.rfp == 3 else st.banks[3]
        bank3[XWA], bank3[XBC] = VECT_FLASHERS << 8, block << 8
        m.set_cpu(st)
        m.run(4_000_000, record=False)
