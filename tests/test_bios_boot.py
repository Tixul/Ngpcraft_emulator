"""THE CONSOLE POWERING ON — and the coin cell that makes the second boot different.

⛔ WHAT THIS FIXES. `bios_handoff=False` was documented as "run the real BIOS". It did
not: `ngpc_reset` set `PC = the cartridge's entry point` in BOTH branches, so the BIOS's
own boot code had never executed in this emulator, in either mode. We diagnosed exactly
that in pass 237 and never fixed it. Overloading one bool had hidden a third case:

    RAW       PC = cart entry, nothing seeded   (the differential / fuzz mode)
    HANDOFF   PC = cart entry + what the BIOS boot leaves behind   (THE DEFAULT)
    BIOS BOOT PC = the hardware RESET VECTOR, and the real BIOS runs

⚡ AND THE CONSOLE'S RAM IS NOT VOLATILE. A coin cell holds the 12 KiB alive, which is
why the BIOS remembers your language and the date -- and why PULLING THE BATTERIES WIPES
IT (the user hit exactly this on real hardware). `0x6C7A` is the marker the BIOS writes
when it enters or leaves a halt, so it is non-zero once the console has booted once, and
the hardware consults it on power-on:

    RAM blank -> the RESET vector (0xFF204A): a first-ever boot.
    RAM kept  -> VECT_SHUTDOWN (0xFF27A2): resume, so the BIOS can run the cleanup it
                 would normally do when you swap cartridges.

(The rule was derived from SNK's own code; the RAM marker at 0x2c7a is what the
BIOS itself tests.)
"""

from __future__ import annotations

import unittest
from pathlib import Path

from core import native

BIOS_PATH = Path(__file__).resolve().parents[3] / "jeux officiel" / "bios_v10.bin"

RESET_VECTOR_TARGET = 0xFF204A     # what the retail BIOS's table at 0xFFFF00 points to
SHUTDOWN_VECTOR_TARGET = 0xFF27A2  # ... and its VECT_SHUTDOWN at 0xFFFE00
RAM_MARKER = 0x006C7A
INT0_POWER = 8


def _rom() -> bytes:
    rom = bytearray(b"\xFF" * 0x100000)
    rom[0:28] = b" LICENSED BY SNK CORPORATION"
    rom[0x1C:0x20] = (0x200040).to_bytes(4, "little")
    rom[0x23] = 0x10
    rom[0x40] = 0x05                    # halt
    return bytes(rom)


@unittest.skipUnless(native.available(), "native core not built")
@unittest.skipUnless(BIOS_PATH.exists(), "there is no BIOS to boot")
class BiosBootTests(unittest.TestCase):
    def _machine(self, battery: bytes | None = None) -> native.NativeMachine:
        m = native.NativeMachine(_rom(), bios=BIOS_PATH.read_bytes())
        m.set_battery_ram(battery)
        m.reset(real_bios=True)
        return m

    def test_a_cold_console_starts_at_the_HARDWARE_RESET_VECTOR(self) -> None:
        """Not at the cartridge. This is the bug: the BIOS had never run."""
        with self._machine() as m:
            self.assertEqual(m.cpu().pc, RESET_VECTOR_TARGET)

    def test_the_handoff_still_starts_at_the_CARTRIDGE(self) -> None:
        """The default path is unchanged -- a game is handed a console, not a boot."""
        with native.NativeMachine(_rom(), bios=BIOS_PATH.read_bytes()) as m:
            m.reset(bios_handoff=True)
            self.assertEqual(m.cpu().pc, 0x200040)
            m.reset(bios_handoff=False)     # RAW: still the cart, nothing seeded
            self.assertEqual(m.cpu().pc, 0x200040)
            self.assertEqual(m.cpu().regs[7], 0, "RAW seeded a register")

    def test_the_bios_actually_RUNS_and_its_interrupts_arrive(self) -> None:
        """It boots, sleeps, and wakes on POWER -- and then its ISR runs every frame.

        The `halt` is not a hang: it IS the console switched off. INT0 is the POWER
        button, and until we press it the BIOS is doing exactly what it should.
        """
        with self._machine() as m:
            halted_at = None
            for _ in range(40):
                s = m.run_frames(1)
                if s.stop_status == native.STATUS_HALTED and halted_at is None:
                    halted_at = s.stop_pc
                    m.raise_irq(INT0_POWER)          # the POWER button
            self.assertIsNotNone(halted_at, "the BIOS never went to sleep")

            # It is alive: the K2GE is on and the BIOS has armed its interrupts.
            self.assertEqual(m.read(0x000071, 1), b"\xDC", "INTE45 not armed by the BIOS")
            deliveries = sum(m.run_frames(1).irq_deliveries for _ in range(10))
            self.assertGreaterEqual(deliveries, 8, "the BIOS's interrupts are not arriving")

    def test_the_coin_cell_keeps_the_ram_across_a_power_cycle(self) -> None:
        with self._machine() as m:
            for _ in range(40):
                s = m.run_frames(1)
                if s.stop_status == native.STATUS_HALTED:
                    m.raise_irq(INT0_POWER)
            kept = m.battery_ram()
        self.assertNotEqual(kept[RAM_MARKER - native.RAM_START], 0,
                            "the BIOS never wrote its own boot marker")

        with self._machine(kept) as m:
            self.assertEqual(
                m.read(RAM_MARKER, 1), bytes([kept[RAM_MARKER - native.RAM_START]]),
                "reset() wiped the battery-backed RAM -- that is a factory reset, not a power cycle",
            )

    def test_a_console_that_has_booted_before_RESUMES_instead(self) -> None:
        """The marker in the kept RAM sends power-on to VECT_SHUTDOWN, not the reset vector."""
        ram = bytearray(native.RAM_SIZE)
        ram[RAM_MARKER - native.RAM_START] = 0xA5        # "I have been switched on before"
        with self._machine(bytes(ram)) as m:
            self.assertEqual(m.cpu().pc, SHUTDOWN_VECTOR_TARGET)
            self.assertEqual(m.cpu().regs[7], 0x006C00, "a system call needs a stack")

    def test_a_DEAD_cell_is_a_first_ever_boot(self) -> None:
        with self._machine(None) as m:
            self.assertEqual(m.cpu().pc, RESET_VECTOR_TARGET)
        with self._machine(bytes(native.RAM_SIZE)) as m:   # present but blank
            self.assertEqual(m.cpu().pc, RESET_VECTOR_TARGET)


if __name__ == "__main__":
    unittest.main()
