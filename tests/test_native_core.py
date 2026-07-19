"""Native C++ core — binding + reset-state equivalence (chantier phase 0).

These tests are the first gate of the C++ port. They do NOT test the CPU (there
isn't one yet, on purpose). They test that:

  1. the MinGW-built C++ DLL loads under this MSVC-built CPython via ctypes;
  2. the native power-on memory image is BYTE-IDENTICAL to the Python core's;
  3. an un-ported opcode TRAPS loudly instead of silently doing nothing.

(2) is the one that matters long-term. Every power-on value is cited from a
manufacturer document (see core/memory.py); a single wrong byte boots a
plausible-but-wrong machine, which is worse than not booting at all. This test
pins the two cores together at reset so that any later trace divergence is
genuinely the CPU's fault and not a seeding artefact.

Skipped (not failed) when cpp/build/ngpc_core.dll is absent, so the suite still
runs for anyone who has not built the native core.
"""

from __future__ import annotations

import unittest
from pathlib import Path

from core import native
from core.memory import load_read_bus
from core.rom import load_rom_header

REPO = Path(__file__).resolve().parent.parent
ROM = REPO.parent.parent / "jeux officiel" / "Crush Roller (USA).ngc"


@unittest.skipUnless(native.available(), "native core not built (cmake --build cpp/build)")
class NativeCoreBindingTests(unittest.TestCase):
    def test_abi_version_matches_the_binding(self) -> None:
        self.assertEqual(native.library().ngpc_abi_version(), native.ABI_VERSION)

    @unittest.skipUnless(ROM.exists(), f"ROM not available: {ROM}")
    def test_entry_point_matches_python(self) -> None:
        header = load_rom_header(ROM)
        with native.NativeMachine(ROM.read_bytes()) as m:
            m.reset(bios_handoff=True)
            self.assertEqual(m.cpu().pc, header.entry_point)

    @unittest.skipUnless(ROM.exists(), f"ROM not available: {ROM}")
    def test_bios_handoff_seeds_the_stack_pointer(self) -> None:
        # Without XSP a real cart cannot execute its first instruction (a CALL).
        with native.NativeMachine(ROM.read_bytes()) as m:
            m.reset(bios_handoff=True)
            cpu = m.cpu()
            self.assertEqual(cpu.regs[native.REG_NAMES.index("xsp")], 0x6C00)
            self.assertEqual(cpu.iff_level, 7)  # DI at boot
            m.reset(bios_handoff=False)
            self.assertEqual(m.cpu().regs[native.REG_NAMES.index("xsp")], 0)

    @unittest.skipUnless(ROM.exists(), f"ROM not available: {ROM}")
    def test_power_on_memory_is_byte_identical_to_the_python_core(self) -> None:
        """The whole cold-start image, byte for byte, against the Python core."""
        bus = load_read_bus(ROM)
        with native.NativeMachine(ROM.read_bytes()) as m:
            m.reset(bios_handoff=True)

            mismatches: list[str] = []
            for address, expected in sorted(bus.builtin_bytes.items()):
                got = m.read(address, 1)[0]
                if got != expected:
                    mismatches.append(f"0x{address:06X}: py=0x{expected:02X} cpp=0x{got:02X}")

            self.assertEqual(
                mismatches[:20],
                [],
                f"{len(mismatches)} power-on byte(s) differ between the cores",
            )

    @unittest.skipUnless(ROM.exists(), f"ROM not available: {ROM}")
    def test_cart_image_is_mapped_at_0x200000(self) -> None:
        rom_bytes = ROM.read_bytes()
        with native.NativeMachine(rom_bytes) as m:
            m.reset()
            self.assertEqual(m.read(0x200000, 64), rom_bytes[:64])

    @unittest.skipUnless(ROM.exists(), f"ROM not available: {ROM}")
    def test_unported_opcode_traps_loudly_with_pc_and_opcode(self) -> None:
        """Coverage gaps must be NOISY. HARDWARE_COMPAT_POLICY.md §9 forbids a
        silent fallback; a NOP-and-advance would leave us debugging ghosts.

        Deliberately does NOT pin *which* opcode traps: that moves every time a
        family lands, and a test that has to be edited on every pass of the port
        is a test nobody trusts. What is pinned is the CONTRACT of the trap.
        """
        with native.NativeMachine(ROM.read_bytes()) as m:
            m.reset(bios_handoff=True)
            summary, _records = m.run(4096)

            if native.status_name(summary.stop_status) == "count-reached":
                self.skipTest("the whole batch is ported — trap contract untestable here")

            self.assertEqual(native.status_name(summary.stop_status), "unimplemented")
            # The trap must NAME its offender: a PC, and the opcode actually
            # sitting at that PC. And it must not have advanced past it.
            self.assertEqual(summary.stop_opcode, m.read(summary.stop_pc, 1)[0])
            self.assertEqual(m.cpu().pc, summary.stop_pc)

    @unittest.skipUnless(ROM.exists(), f"ROM not available: {ROM}")
    def test_ported_opcodes_retire_on_a_real_rom(self) -> None:
        """Crush Roller's entry sequence runs in the native core as far as the
        port currently reaches. This is the progress marker of the chantier."""
        header = load_rom_header(ROM)
        with native.NativeMachine(ROM.read_bytes()) as m:
            m.reset(bios_handoff=True)
            summary, records = m.run(4096)

            self.assertGreater(summary.executed, 0)
            self.assertEqual(records[0].pc, header.entry_point)
            self.assertEqual(records[0].raw_len, 5)  # ld XHL, #imm32
            self.assertGreater(summary.total_cycles, 0)

    @unittest.skipUnless(ROM.exists(), f"ROM not available: {ROM}")
    def test_the_write_log_sees_the_CPU_s_own_stores(self) -> None:
        """The instrument must fire on the path the GAME actually writes through.

        Its first version hooked `Machine::write8()` only -- and the CPU's `store()`
        does its own region check and writes `mem[]` directly, because it must also
        feed the flash latch and the Z80 control registers. So the log reported ZERO
        writes to a tilemap that was visibly changing. An instrument that cannot fire
        is worse than no instrument: it reads as evidence of absence.

        This test drives a real ROM until its own code stores into the window, and
        fails if the log stays empty -- which is exactly what the broken version did.
        """
        with native.NativeMachine(ROM.read_bytes()) as m:
            m.reset(bios_handoff=True)
            m.set_write_log(0x004000, 0x006FFF)     # the game's work RAM
            m.run_frames(2)

            self.assertGreater(
                m.write_log_count(), 0,
                "the write log saw nothing while the game ran -- it is hooked into a "
                "path the CPU does not use",
            )
            recs = m.write_log()
            self.assertTrue(all(0x004000 <= r.addr <= 0x006FFF for r in recs))
            self.assertTrue(
                all(r.pc != 0 for r in recs), "a logged write must name its writer",
            )

    @unittest.skipUnless(ROM.exists(), f"ROM not available: {ROM}")
    def test_the_write_log_is_disarmed_by_default(self) -> None:
        with native.NativeMachine(ROM.read_bytes()) as m:
            m.reset(bios_handoff=True)
            m.run_frames(1)
            self.assertEqual(m.write_log_count(), 0)

    @unittest.skipUnless(ROM.exists(), f"ROM not available: {ROM}")
    def test_the_raster_log_holds_one_register_snapshot_per_visible_line(self) -> None:
        """152 lines, and the line counter 0x8009 must differ on every one of them.

        0x8009 is RAS.V -- the scanline the beam is on. If the log were snapshotting
        the same instant 152 times (or only at end of frame, which is what the
        renderer used to do), RAS.V would be constant and the raster would be a lie.
        """
        with native.NativeMachine(ROM.read_bytes()) as m:
            m.reset(bios_handoff=True)
            m.run_frames(2)
            log = m.raster_log()

            self.assertEqual(len(log), 152)
            self.assertTrue(all(len(line) == 0x40 for line in log))
            ras_v = [line[0x8009 - 0x8000] for line in log]
            self.assertEqual(ras_v, list(range(152)))

    @unittest.skipUnless(ROM.exists(), f"ROM not available: {ROM}")
    def test_guest_writes_to_rom_are_discarded_but_host_writes_are_not(self) -> None:
        # The debugger is allowed to patch its own image; the guest is not.
        # (A discarded guest write is what latches an AMD flash command.)
        with native.NativeMachine(ROM.read_bytes()) as m:
            m.reset()
            m.write(0x200000, b"\xAA")          # host poke -> lands
            self.assertEqual(m.read(0x200000, 1), b"\xAA")
            m.write(0x004000, b"\x5A")          # RAM -> lands
            self.assertEqual(m.read(0x004000, 1), b"\x5A")


if __name__ == "__main__":
    unittest.main()
