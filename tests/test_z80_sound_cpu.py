"""The sound CPU — the co-processor that was missing, and why 19 more games draw.

The gate here is FUNCTIONAL, and it has to be: there is no second Z80 in this
project to run a differential fuzz against. But the games are an unforgiving
oracle of their own. A game uploads its sound driver into the shared window, kicks
the Z80, and then *waits*. It does not draw a single tile until the driver
answers. So "does the picture appear" is not a soft check -- it is the driver
actually having executed.

What is asserted here is the SEAM (the CPU itself is proved by the corpus):

  * the Z80 is held in reset until the main CPU releases it, and a game that never
    asks for it never gets one;
  * `0x55` to `0xB9` (the high half of the 16-bit register) releases it;
  * its memory IS the shared window -- both CPUs see the same bytes;
  * an un-ported opcode TRAPS, loudly, with its address. It does not NOP.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from core import native

# ⚖️ The reset register is 16 BITS and commands TWO chips (SNK, K1SoundSim.txt
# § 3.4.3.1): 5555h = both RUN · 55AAh = Z80 RUN, chip reset · AA55h = Z80 RESET,
# chip RUN · AAAAh = both reset (the power-on value). Cross-read those four rows and
# the byte roles fall out: the HIGH byte (0xB9) commands the Z80.
#
# This suite used to write the byte 0x55 to 0xB8 and call that a release. It is not:
# on silicon that leaves 0xB9 at its power-on 0xAA -- the word AA55h -- and the Z80
# STAYS IN RESET. A calibration ROM doing exactly that never started the sound CPU on
# a real NGPC, and its liveness stamp is what caught the emulator being lenient.
Z80_RESET_REG = 0x0000B9        # the HIGH byte of the 16-bit register
Z80_NMI_REG = 0x0000BA
Z80_COMM_REG = 0x0000BC
Z80_SHARED_RAM = 0x007000
Z80_RELEASE = 0x55


def _write_demo_rom(path: Path, entry: int, body: bytes) -> None:
    header = bytearray(0x30)
    header[0x00:0x1C] = b"COPYRIGHT BY SNK CORPORATION"[:0x1C].ljust(0x1C, b" ")
    header[0x1C:0x20] = entry.to_bytes(4, "little")
    header[0x23] = 0x10
    header[0x24:0x30] = b"Z80TEST     "[:0x0C].ljust(0x0C, b"\x00")
    rom = bytearray(header)
    rom.extend(b"\x00" * (entry - 0x200000 - len(rom)))
    rom.extend(body)
    rom.extend(b"\x00" * 64)
    path.write_bytes(bytes(rom))


@unittest.skipUnless(native.available(), "native core not built (cmake --build cpp/build)")
class Z80SoundCpuTests(unittest.TestCase):
    def _machine(self, tmpdir: str, body: bytes = b"\x00\x68\xFD"):
        """A main-CPU program that just burns cycles (`nop; jr -2`) so the Z80 runs."""
        rom = Path(tmpdir) / "z80.ngc"
        _write_demo_rom(rom, 0x00200040, body)
        m = native.NativeMachine(rom.read_bytes())
        m.reset(bios_handoff=True)
        return m

    def test_it_stays_in_reset_until_the_main_cpu_asks(self) -> None:
        # A cartridge that never touches the register must never get a sound CPU. Starting
        # one anyway would let it execute whatever bytes happen to be in the shared
        # window at power-on.
        with tempfile.TemporaryDirectory() as tmpdir:
            m = self._machine(tmpdir)
            m.run(50_000, record=False)
            z = m.z80()
            self.assertFalse(z.running)
            self.assertEqual(z.executed, 0)
            m.close()

    def test_writing_0x55_to_0xb9_releases_it_and_it_runs(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            m = self._machine(tmpdir)
            # A Z80 program that spins: `jr -2` at its address 0x0000.
            m.write(Z80_SHARED_RAM, bytes([0x18, 0xFE]))
            m.write(Z80_RESET_REG, bytes([Z80_RELEASE]))
            m.run(50_000, record=False)
            z = m.z80()
            self.assertTrue(z.running)
            self.assertFalse(z.trapped)
            self.assertGreater(z.executed, 0)
            m.close()

    def test_its_memory_is_the_shared_window(self) -> None:
        # `ld a,0x5A ; ld (0x0123),a ; halt` -- the byte must land where the MAIN cpu
        # sees it, at 0x7123. That is the whole point of the window, and it is how a
        # driver answers the game that is polling it.
        with tempfile.TemporaryDirectory() as tmpdir:
            m = self._machine(tmpdir)
            m.write(Z80_SHARED_RAM, bytes([0x3E, 0x5A, 0x32, 0x23, 0x01, 0x76]))
            m.write(Z80_RESET_REG, bytes([Z80_RELEASE]))
            m.run(50_000, record=False)
            self.assertEqual(m.read(Z80_SHARED_RAM + 0x123, 1)[0], 0x5A)
            self.assertTrue(m.z80().halted)
            m.close()

    def test_the_nmi_register_wakes_a_halted_driver(self) -> None:
        # The driver parks on HALT and the main CPU hands it work by poking 0xBA.
        # An NMI vectors to 0x0066; we put a store there so the wake is observable.
        with tempfile.TemporaryDirectory() as tmpdir:
            m = self._machine(tmpdir)
            m.write(Z80_SHARED_RAM, bytes([0x76]))                       # 0x0000: halt
            m.write(Z80_SHARED_RAM + 0x66,
                    bytes([0x3E, 0xA5, 0x32, 0x00, 0x02, 0x76]))          # 0x0066: ld a,0xA5; ld (0x0200),a; halt
            m.write(Z80_RESET_REG, bytes([Z80_RELEASE]))
            m.run(20_000, record=False)
            self.assertTrue(m.z80().halted)
            self.assertEqual(m.read(Z80_SHARED_RAM + 0x200, 1)[0], 0x00)  # not yet

            m.write(Z80_NMI_REG, bytes([0x01]))                           # fire the NMI
            m.run(20_000, record=False)
            self.assertEqual(m.read(Z80_SHARED_RAM + 0x200, 1)[0], 0xA5)
            m.close()

    def test_an_unported_opcode_traps_loudly(self) -> None:
        # A sound CPU that NOPed what it did not recognise would still "run", and
        # would hand the game a wrong answer with nothing to say so. `ED 77` is a
        # documented no-op on real silicon but is NOT in our table, so it must trap
        # rather than be waved through.
        with tempfile.TemporaryDirectory() as tmpdir:
            m = self._machine(tmpdir)
            m.write(Z80_SHARED_RAM, bytes([0x00, 0xED, 0x77]))
            m.write(Z80_RESET_REG, bytes([Z80_RELEASE]))
            m.run(50_000, record=False)
            z = m.z80()
            self.assertTrue(z.trapped)
            self.assertEqual(z.trap_prefix, 0xED)
            self.assertEqual(z.trap_opcode, 0x77)
            self.assertEqual(z.trap_pc, 0x0001)
            m.close()


if __name__ == "__main__":
    unittest.main()
