"""What the real BIOS leaves in the registers at cart entry. MEASURED ON SILICON.

We used to hand the cartridge eight zeros. That is not a neutral default -- it is a
wrong one, and it broke a game.

Puyo Pop's init loop clears both tilemaps at once:

    ld  XIZ, 0x9000
    loop:  ldw (XIZ+), 0        ; SCR1
           ldw (XIX+), 0        ; ... and NOTHING IN THE CARTRIDGE SETS XIX
           djnz BC, loop        ; 1024 times

On hardware XIX points INTO THE BIOS ROM, which is read-only, so those 1024 writes are
DISCARDED and the loop is harmless. With our zero they swept the I/O PAGE and wiped the
timer registers: timer 3 stopped, the Z80 took no interrupt, the sound driver never
answered the handshake at 0x70DE, and the main CPU spun forever on a blank screen.

⛔ THE WORKING HYPOTHESIS WAS WRONG, AND IT "WORKED".  We guessed XIX = 0x9800 (SCR2's
base -- exactly the right size for a 1024-word clear). Forcing it made the game boot,
which felt like proof. It was not: 0x9800 and 0xFF23C3 have exactly one thing in
common, and it is that NEITHER IS THE I/O PAGE. `hw_entry_regs` was
flashed to settle it, and the console refuted the guess.

⚠️ TWO FLASHES, AND THEY DISAGREED -- which is itself the finding. Six registers came
back identical both times. XDE and XHL did not (0x00006BFF then 0x002040FF; 0x50).
They are BIOS scratch, no cartridge can depend on them, and they are NOT seeded:
freezing one sample of a value that does not reproduce is a coin toss wearing a fact's
clothes.
"""

from __future__ import annotations

import unittest

from core import native

XWA, XBC, XDE, XHL, XIX, XIY, XIZ, XSP = range(8)

# Stable across both flashes.
MEASURED = {
    XIX: 0x00FF23C3,
    XIY: 0x00FF23DF,
    XIZ: 0x00006480,
    XWA: 0x000000DD,
    XBC: 0x00200018,
    XSP: 0x00006C00,   # the ROM prints 0x6BFC, its own prologue having pushed 4 bytes
}
NAMES = {XWA: "XWA", XBC: "XBC", XDE: "XDE", XHL: "XHL",
         XIX: "XIX", XIY: "XIY", XIZ: "XIZ", XSP: "XSP"}


def _rom() -> bytes:
    rom = bytearray(0x400)
    rom[0x1C:0x20] = (0x200020).to_bytes(4, "little")
    rom[0x20] = 0x00                                    # nop
    return bytes(rom)


@unittest.skipUnless(native.available(), "native core not built")
class BiosHandoffRegisterTests(unittest.TestCase):
    def test_the_cart_sees_the_registers_the_console_reported(self) -> None:
        with native.NativeMachine(_rom()) as m:
            m.reset(bios_handoff=True)
            cpu = m.cpu()
            for idx, want in MEASURED.items():
                self.assertEqual(
                    cpu.regs[idx], want,
                    f"{NAMES[idx]} at cart entry is {cpu.regs[idx]:#010x}, "
                    f"but the console says {want:#010x}",
                )

    def test_xix_points_into_the_bios_where_a_stray_write_is_HARMLESS(self) -> None:
        """The whole point. A cart that clears memory through XIX must do no damage.

        This is the assertion that actually protects Puyo Pop: not the exact value,
        but the fact that it lands in read-only space. A future 'tidy-up' that zeroed
        XIX again would sail past an equality check on a constant it also changed --
        it cannot sail past this one.
        """
        with native.NativeMachine(_rom()) as m:
            m.reset(bios_handoff=True)
            xix = m.cpu().regs[XIX]
            self.assertGreaterEqual(
                xix, 0xFF0000,
                "XIX must land in the BIOS, where a cart's stray writes are discarded. "
                "Zero puts it on the I/O page, and Puyo Pop wipes its own timers.",
            )

    def test_the_registers_that_VARY_on_hardware_are_left_alone(self) -> None:
        """XDE and XHL differed between two power-ons. We do not invent them."""
        with native.NativeMachine(_rom()) as m:
            m.reset(bios_handoff=True)
            cpu = m.cpu()
            for idx in (XDE, XHL):
                self.assertEqual(
                    cpu.regs[idx], 0,
                    f"{NAMES[idx]} was seeded from a single sample of a value the "
                    "console does not reproduce",
                )

    def test_a_cart_run_WITHOUT_the_handoff_still_gets_nothing(self) -> None:
        """`bios_handoff=False` means "run the real BIOS" -- it must seed nothing."""
        with native.NativeMachine(_rom()) as m:
            m.reset(bios_handoff=False)
            cpu = m.cpu()
            for idx in range(8):
                self.assertEqual(cpu.regs[idx], 0, f"{NAMES[idx]} was seeded anyway")


if __name__ == "__main__":
    unittest.main()
