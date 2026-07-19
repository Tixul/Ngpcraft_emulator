"""The cartridge flash. THE SAVE.

The NGPC has no save RAM: the cartridge IS a NOR flash chip, and a game saves by
erasing a block of itself and programming its slot back in. This core knew the AMD
unlock sequence well enough for the BIOS to identify the cartridge, and then, in its
own comment's words, "swallowed, not faked" every erase and every program.

So every save this emulator ever took went NOWHERE. Silently. You found out by losing
one -- which is exactly the failure mode the project's own policy forbids.

The protocol is AMD/Fujitsu; the block map is the manufacturer's (SDK FlashMem.txt):
64 KiB blocks all the way up, with the LAST 64 KiB split 32 / 8 / 8 / 16. Those small
blocks at the top are where a save lives -- the chip is divided that way precisely so
rewriting one slot does not cost you 64 KiB.

⚠️ AND A NOR CELL ONLY GOES DOWN. Programming ANDs the byte in; only an erase puts the
1 bits back. A model that just stores the byte would produce data the silicon cannot,
and would hide the exact bug a homebrew author needs to see -- a slot programmed twice
with no erase between.
"""

from __future__ import annotations

import unittest

from core import native

CART = 0x200000
ROM_SIZE = 0x100000          # 8 Mbit
# The top 64 KiB, split 32 / 8 / 8 / 16 (SDK FlashMem.txt).
TOP = ROM_SIZE - 0x10000
BLOCK_32K = CART + TOP
BLOCK_8K_A = CART + TOP + 0x8000
BLOCK_8K_B = CART + TOP + 0xA000
BLOCK_16K = CART + TOP + 0xC000


def _rom() -> bytes:
    rom = bytearray(b"\x00" * ROM_SIZE)
    rom[0:28] = b" LICENSED BY SNK CORPORATION"
    rom[0x1C:0x20] = (0x200020).to_bytes(4, "little")
    rom[0x23] = 0x10
    return bytes(rom)


@unittest.skipUnless(native.available(), "native core not built")
class FlashTests(unittest.TestCase):
    def setUp(self) -> None:
        self.m = native.NativeMachine(_rom())
        self.m.reset(bios_handoff=True)

    def tearDown(self) -> None:
        self.m.close()

    # The command cycles go through the CPU's own store path, because that is the only
    # way a game can reach the flash: a cart-window write is DISCARDED as memory, and
    # the discarded write is what latches the command.
    def _cmd(self, address: int, value: int) -> None:
        self.m.bus_write(address, value)

    def _unlock(self) -> None:
        self._cmd(CART + 0x5555, 0xAA)
        self._cmd(CART + 0x2AAA, 0x55)

    def _program(self, address: int, value: int) -> None:
        self._unlock()
        self._cmd(CART + 0x5555, 0xA0)
        self._cmd(address, value)

    def _erase_block(self, address: int) -> None:
        self._unlock()
        self._cmd(CART + 0x5555, 0x80)
        self._unlock()
        self._cmd(address, 0x30)

    def test_nothing_is_dirty_on_a_fresh_cartridge(self) -> None:
        self.assertFalse(self.m.flash_dirty())

    def test_a_program_writes_the_byte(self) -> None:
        self._erase_block(BLOCK_8K_A)          # erased flash reads 0xFF
        self._program(BLOCK_8K_A + 4, 0x5A)
        self.assertEqual(self.m.read(BLOCK_8K_A + 4, 1), b"\x5A")
        self.assertTrue(self.m.flash_dirty(), "a real save must announce itself")

    def test_a_NOR_CELL_ONLY_GOES_DOWN(self) -> None:
        """Program twice with no erase between, and the bits AND together.

        This is the assertion that makes the model a flash chip rather than a byte
        array. A homebrew that rewrites a slot without erasing first gets corruption on
        real hardware; it must get the same corruption here, or the emulator is lying
        to its author in the most expensive way possible.
        """
        self._erase_block(BLOCK_8K_A)
        self._program(BLOCK_8K_A, 0xF0)
        self._program(BLOCK_8K_A, 0x3C)        # 0xF0 & 0x3C = 0x30
        self.assertEqual(
            self.m.read(BLOCK_8K_A, 1), b"\x30",
            "programming must AND into the cell -- a NOR bit cannot be raised, only erased",
        )

    def test_an_erase_puts_the_ones_back_and_only_in_its_own_block(self) -> None:
        self._erase_block(BLOCK_8K_A)
        self._program(BLOCK_8K_A, 0x00)
        self._program(BLOCK_8K_B, 0x00)        # the NEXT block along

        self._erase_block(BLOCK_8K_A)
        self.assertEqual(self.m.read(BLOCK_8K_A, 1), b"\xFF", "the erase did not fire")
        self.assertEqual(
            self.m.read(BLOCK_8K_B, 1), b"\x00",
            "the erase spilled into the neighbouring block -- the map is wrong",
        )

    def test_the_save_survives_a_reset_the_way_a_cartridge_does(self) -> None:
        """Flash is NON-VOLATILE. Powering the console off does not wipe your save."""
        self._erase_block(BLOCK_8K_A)
        self._program(BLOCK_8K_A, 0x42)
        saved = self.m.read(BLOCK_8K_A, 16)

        self.m.reset(bios_handoff=True)        # reload the cart image = a fresh cart
        self.m.flash_restore(BLOCK_8K_A, saved)   # ... and put the cartridge back in
        self.assertEqual(self.m.read(BLOCK_8K_A, 1), b"\x42")

    def test_a_stray_write_with_no_command_sequence_changes_NOTHING(self) -> None:
        """The cart window is READ-ONLY memory until the AMD sequence says otherwise.

        A game that walks off the end of an array into the cart window must not be able
        to corrupt its own save by accident -- and neither must a bug in our core.
        """
        before = self.m.read(BLOCK_8K_A, 4)
        self._cmd(BLOCK_8K_A, 0x00)
        self._cmd(BLOCK_8K_A + 1, 0x00)
        self.assertEqual(self.m.read(BLOCK_8K_A, 4), before)
        self.assertFalse(self.m.flash_dirty())

    def test_autoselect_answers_the_chip_id_then_gives_the_memory_back(self) -> None:
        """The BIOS asks the cartridge what it is. The device ID names its SIZE."""
        self._unlock()
        self._cmd(CART + 0x5555, 0x90)
        self.assertEqual(self.m.read(CART + 0, 1), b"\x98", "manufacturer: Toshiba")
        self.assertEqual(
            self.m.read(CART + 1, 1), b"\x2C",
            "an 8 Mbit part must say 0x2C -- the ID is how the BIOS learns the size",
        )
        self._cmd(CART + 0x5555, 0xF0)         # reset: be memory again
        self.assertEqual(self.m.read(CART, 1), b" "[:1], "the cart image must be back")


if __name__ == "__main__":
    unittest.main()
