"""A 4 MB cartridge is TWO flash dies, and the second one lives at 0x800000.

THE DEFECT THIS CONDEMNS (pass 247, found on SNK vs. Capcom MotM). Both cores
mapped only the first 2 MB of a ROM image, at 0x200000. The second die's window
(0x800000..0x9FFFFF) was deliberately unmapped, on the claim that "the BIOS only
ever touches it to ask what chip it is" -- true for every 2 MB cart, false for
the three 4 MB carts in the corpus. SvC MotM keeps its whole intro above
0x800000: tile data, page descriptors, and pointers into the same window. Every
one of those reads returned ZERO, its decompressor faithfully copied zeros into
character RAM (96.4 % of 142 224 char-RAM writes measured zero), and the intro
played blind on a dash-tile screen while the engine heartbeat ran perfectly.
The C++ core had a second face of the same bug: it spilled a 4 MB image
straight through 0x200000..0x5FFFFF, planting the second die at 0x400000 --
which is not a cartridge window on this bus at all.

Carts of 2 MB or less keep the window unmapped exactly as before (the fuzz gate
reads unmapped space there, and the two cores must keep agreeing about it).
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from core import native
from core.memory import load_read_bus

CHIP0 = 0x200000  # one die
MARKER_OFF = 0x1234  # offset of the probe bytes within the SECOND die
MARKER = bytes([0xA5, 0x5A, 0xC3, 0x3C])


def _four_mb_rom() -> bytes:
    """A minimal two-die image: 2 MB + 8 KB, marker in the second die."""
    rom = bytearray(CHIP0 + 0x2000)
    rom[0x1C:0x20] = (0x200040).to_bytes(4, "little")   # entry point
    rom[0x40] = 0x68                                    # jr $ -- park
    rom[0x41] = 0xFE
    rom[CHIP0 + MARKER_OFF : CHIP0 + MARKER_OFF + 4] = MARKER
    return bytes(rom)


def test_native_core_maps_the_second_die_at_0x800000() -> None:
    machine = native.NativeMachine(_four_mb_rom())
    machine.reset(bios_handoff=True)
    assert machine.read(0x800000 + MARKER_OFF, 4) == MARKER, (
        "the second die's bytes must be readable through the 0x800000 window"
    )
    # Erased flash beyond the image, not zeros.
    assert machine.read(0x800000 + 0x2000, 2) == b"\xff\xff"
    # And the second die's bytes must NOT sit at 0x400000: that address range
    # is not a cartridge window, and the old copy loop spilled into it.
    assert machine.read(0x400000 + MARKER_OFF, 4) != MARKER


def test_python_reference_maps_the_second_die_at_0x800000() -> None:
    with tempfile.TemporaryDirectory() as td:
        rom_path = Path(td) / "twodies.ngc"
        rom_path.write_bytes(_four_mb_rom())
        bus = load_read_bus(rom_path)
        got = bus.read_bytes(0x800000 + MARKER_OFF, 4)
        assert got.status == "ok", got.note
        assert got.data == MARKER
        gap = bus.read_bytes(0x800000 + 0x2000, 2)
        assert gap.status == "ok"
        assert gap.data == b"\xff\xff", "beyond the image the die reads erased (0xFF)"


def test_small_carts_keep_the_window_unmapped() -> None:
    """The fuzz gate reads unmapped space at 0x800000 for 2 MB carts; that view
    must not change underneath it."""
    rom = bytearray(0x1000)
    rom[0x1C:0x20] = (0x200040).to_bytes(4, "little")
    with tempfile.TemporaryDirectory() as td:
        rom_path = Path(td) / "small.ngc"
        rom_path.write_bytes(bytes(rom))
        bus = load_read_bus(rom_path)
        probe = bus.read_bytes(0x800000, 1)
        assert probe.status == "unmapped"
