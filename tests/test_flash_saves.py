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


# THE TEST PROGRAM RUNS FROM RAM, and it has to.
#
# A chip that is programming or erasing answers STATUS to every read of its window --
# an instruction fetch included. A test ROM whose code sat in the cart window would be
# fetching status bits as opcodes for the whole busy period, die on the first one that
# decodes to nothing, and stop the machine clock -- so the chip would never finish and
# the test would deadlock on its own premise.
#
# That is not an artefact of the model: it is the exact hazard the model exists to show,
# and it is why every real NGPC flash driver copies its stub into RAM first. So this ROM
# does what a real driver does -- its entry point is in RAM, and setUp puts an infinite
# loop there for the clock to run on.
RAM_ENTRY = 0x004000
RAM_LOOP = bytes([0x68, 0xFE])       # jr T,-2 -- spin, and let time pass


def _rom() -> bytes:
    rom = bytearray(b"\x00" * ROM_SIZE)
    rom[0:28] = b" LICENSED BY SNK CORPORATION"
    rom[0x1C:0x20] = RAM_ENTRY.to_bytes(4, "little")
    rom[0x23] = 0x10
    return bytes(rom)


@unittest.skipUnless(native.available(), "native core not built")
class FlashTests(unittest.TestCase):
    def setUp(self) -> None:
        self.m = native.NativeMachine(_rom())
        self.m.reset(bios_handoff=True)
        self.m.write(RAM_ENTRY, RAM_LOOP)

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

    # ⏱️ AND THEN YOU WAIT. A chip that is programming or erasing answers STATUS, not
    # contents, until it is done -- so a read taken right after the command comes back
    # 0x40 (the DQ6 toggle), not the byte. That is not a detail of the model, it is the
    # whole reason a flash stub runs from RAM with interrupts masked. Every test below
    # therefore lets the machine run, which is what a driver's poll loop is.
    #
    # FIVE frames, and the number comes from the cartridge. One frame is ~102 000 cycles;
    # the 8 KB erase was MEASURED at ~329 600 (53.65 ms, hw_test_flash_timing ROW 4), so a
    # single frame leaves the chip still working and every assertion below reads status
    # instead of contents. `test_the_chip_is_BUSY_...` is the test that deliberately does
    # not wait.
    def _settle(self) -> None:
        self.m.run_frames(5)

    # The AMD reset. A driver ends every sequence with it, and it is the ONLY thing a
    # chip that has raised DQ5 will listen to.
    def _reset_chip(self) -> None:
        self._cmd(CART + 0x5555, 0xF0)

    def _program(self, address: int, value: int) -> None:
        self._unlock()
        self._cmd(CART + 0x5555, 0xA0)
        self._cmd(address, value)
        self._settle()

    def _erase_block(self, address: int) -> None:
        self._unlock()
        self._cmd(CART + 0x5555, 0x80)
        self._unlock()
        self._cmd(address, 0x30)
        self._settle()

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

        # ⛔ AND THE CHIP SAYS NOTHING. 0x3C asks for four bits the cell has already lost,
        # so its own verify can never match -- and it does NOT report that. Measured on the
        # cartridge: 2.45 seconds of polling, no DQ5, the chip still answering status.
        # The corruption is silent and the driver is the only thing that can end it, which
        # is why the project's research log could record "FVFY:0002 = NOR AND of old and
        # new data" from a routine that thought it had succeeded.
        self.assertEqual(self.m.read(BLOCK_8K_A, 1)[0] & 0x20, 0x00,
                         "this part does not raise DQ5 -- the corruption is silent")
        self._reset_chip()                     # the driver's own timeout is the only exit
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


    # ------------------------------------------------------------------ busy window
    def test_the_chip_is_BUSY_while_it_programs_and_says_so(self) -> None:
        """A program takes time, and during it the window answers status.

        Committing the byte inside the command's own bus cycle -- what this core did
        until 2026-09-10 -- deletes the only window in which a NGPC save can go wrong:
        the one the driver spends with interrupts masked, reading a cartridge that has
        stopped being memory. hw_test_flash_timing measured it as ZERO turns of the
        shipped AMD poll loop, against 107 000 turns per second of that same loop.
        """
        self._erase_block(BLOCK_8K_A)
        self._unlock()
        self._cmd(CART + 0x5555, 0xA0)
        self._cmd(BLOCK_8K_A, 0x5A)
        self.assertNotEqual(self.m.read(BLOCK_8K_A, 1), bytes([0x5A]),
                            "the byte appeared in the same cycle as the command")
        self.assertEqual(self.m.read(BLOCK_8K_A, 1)[0] & 0x80, 0x80,
                         "DQ7 answers the COMPLEMENT of the bit 7 being programmed, and "
                         "0x5A has bit 7 clear, so the chip shows it set")
        self._settle()
        self.assertEqual(self.m.read(BLOCK_8K_A, 1), bytes([0x5A]), "and then it lands")

    def test_the_chip_is_BUSY_while_it_erases(self) -> None:
        self._program(BLOCK_8K_A, 0x00)
        self._unlock()
        self._cmd(CART + 0x5555, 0x80)
        self._unlock()
        self._cmd(BLOCK_8K_A, 0x30)
        self.assertNotEqual(self.m.read(BLOCK_8K_A, 1), bytes([0xFF]),
                            "the block came back blank inside the erase command itself")
        self._settle()
        self.assertEqual(self.m.read(BLOCK_8K_A, 1), bytes([0xFF]))

    def test_a_1_asked_over_a_0_NEVER_COMES_BACK(self) -> None:
        """The failure a slot programmed twice with no erase produces on the console.

        ⛔ AND THE CHIP NEVER ADMITS IT. The AMD datasheets say the part runs an internal
        timer and raises DQ5, and this test was written that way. THE CARTRIDGE SAYS NO:
        hw_test_flash_timing ROW 9, asked to put 0xFF back over a 0x5A, spun 262 143 turns
        of the shipped poll loop -- 2.45 SECONDS -- with no DQ5 at all. The stub left on
        its own iteration ceiling, and the byte was untouched.

        That is the mechanism, not a curiosity: two and a half seconds INTERRUPTS MASKED,
        no V-blank, no watchdog refresh from the game's handler, a micro-DMA still reading
        a cartridge that stopped being memory. A save that passes in an emulator and takes
        the console down.

        So the chip stays busy for as long as it is asked, and the only way out is the
        driver's own timeout followed by a reset. `flash_fail_cycles` gives the datasheet
        DQ5 behaviour to a part that has it -- this one does not.
        """
        self._erase_block(BLOCK_8K_A)
        self._program(BLOCK_8K_A, 0x5A)
        self._unlock()
        self._cmd(CART + 0x5555, 0xA0)
        self._cmd(BLOCK_8K_A, 0xFF)            # asking 0x5A back up to 0xFF
        for _ in range(4):                     # far longer than any real operation
            self._settle()
            status = self.m.read(BLOCK_8K_A, 1)[0]
            self.assertEqual(status & 0x20, 0x00, "this part does not raise DQ5")
            self.assertNotEqual(status, 0x5A, "and it never returns to being memory")
        self._reset_chip()                     # the driver gives up: only this gets out
        self.assertEqual(self.m.read(BLOCK_8K_A, 1), bytes([0x5A]),
                         "and the byte is untouched")

    def test_running_FROM_a_busy_chip_is_reported(self) -> None:
        """⛔ The finding that names the whole class of bug.

        A chip that is erasing answers STATUS to every read of its window, an instruction
        fetch included. A ROM that keeps executing from the cartridge across its own save
        is therefore running status bits, and that is what takes a console down. It is
        also why every NGPC flash driver copies its stub into RAM and masks interrupts --
        and why a ROM that forgets either looked perfect here until the busy window
        existed.

        Counted, never fatal on its own: a console does not stop at that instruction
        either, it runs the garbage.
        """
        # A second machine, this one executing from the CART rather than from RAM.
        rom = bytearray(_rom())
        rom[0x1C:0x20] = (0x200020).to_bytes(4, "little")
        rom[0x20:0x22] = bytes([0x68, 0xFE])           # jr T,-2 -- spin, in the cartridge
        m = native.NativeMachine(bytes(rom))
        try:
            m.reset(bios_handoff=True)
            m.run_frames(1)
            self.assertEqual(m.hw_violations(native.HW_FLASH_BUSY_FETCH), 0,
                             "nothing is wrong while the chip is just memory")

            for addr, value in ((CART + 0x5555, 0xAA), (CART + 0x2AAA, 0x55),
                                (CART + 0x5555, 0x80), (CART + 0x5555, 0xAA),
                                (CART + 0x2AAA, 0x55), (BLOCK_8K_A, 0x30)):
                m.bus_write(addr, value)
            m.run_frames(1)

            self.assertGreaterEqual(m.hw_violations(native.HW_FLASH_BUSY_FETCH), 1,
                                    "the CPU fetched out of a chip that was erasing and "
                                    "nothing said so")
            first = m.hw_violation_samples(4)[0]
            self.assertEqual(first.kind_name, "flash-busy-fetch")
            self.assertTrue(0x200000 <= first.pc <= 0x3FFFFF,
                            f"the finding must carry the fetching PC, got {first.pc:#x}")
        finally:
            m.close()

    def test_the_synchronous_chip_is_still_one_call_away(self) -> None:
        """The A/B switch. Zero everywhere = the model that shipped before the window,
        so a corpus change can be attributed instead of argued about."""
        self.m.set_flash_timing(0, 0, 0)
        self._unlock()
        self._cmd(CART + 0x5555, 0x80)
        self._unlock()
        self._cmd(BLOCK_8K_A, 0x30)
        self._unlock()
        self._cmd(CART + 0x5555, 0xA0)
        self._cmd(BLOCK_8K_A, 0x5A)
        self.assertEqual(self.m.read(BLOCK_8K_A, 1), bytes([0x5A]),
                         "with no busy window the byte is there in the same cycle")


if __name__ == "__main__":
    unittest.main()
