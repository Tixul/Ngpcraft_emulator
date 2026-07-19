"""The 18-slot USER interrupt vector table the BIOS fills at power-on.

SysPro.txt: every interrupt vectors through the BIOS, which chains to a user
handler pointer in RAM at `0x6FB8 + 4n`. The BIOS's power-on code fills all 18
slots with a default stub before it ever starts the cartridge:

    FF239D  ld   XIY, 0x00FF23DF     <- the default handler ...
    FF23A2  ld   XIX, 0x00006FB8     <- ... the table ...
    FF23A7  ld   BC, 0x0012          <- ... 18 entries ...
    FF23AA  ld   (XIX+), XIY
    FF23AD  djnz BC, 0xFF23AA
    FF23DF  reti                     <- and the stub is a bare RETI.

That RETI is why a game survives an interrupt it never hooked. Fatal Fury turns
the H-blank interrupt on at boot (INTT0, level 3) and only arms the micro-DMA on
the screens that scroll a raster; everywhere else the H-int fires 152 times a
frame and lands on this stub. Both cores hand off straight to the cartridge and
never run the BIOS's power-on code, so the table used to stay all-zero -- the CPU
vectored to address 0, hit the `swi 7` there, and the BIOS error handler powered
the console off. Ten ROMs died that way, and NO TEST COVERED IT (DEVLOG pass 208).

These tests are the cover. They assert the seed exists, that it is READ OUT OF
THE BIOS rather than memorised, and that a BIOS without the fill routine gets no
invented address to jump to.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from core import native
from core.emulator_session import EmulatorSession

BIOS_PATH = Path(__file__).resolve().parents[3] / "jeux officiel" / "bios_v10.bin"
ROM_PATH = (
    Path(__file__).resolve().parents[3]
    / "jeux officiel"
    / "Fatal Fury - First Contact (USA).ngc"
)

USER_VECTOR_TABLE_BASE = 0x006FB8
USER_VECTOR_TABLE_SLOTS = 18
HINT_USER_HOOK = 0x006FD4  # the slot the BIOS's INTT0 handler jumps through

pytestmark = pytest.mark.skipif(
    not (BIOS_PATH.exists() and ROM_PATH.exists()),
    reason="needs the real BIOS image and a commercial ROM",
)


def test_python_core_seeds_every_slot_from_the_bios() -> None:
    session = EmulatorSession(str(ROM_PATH), bios_path=str(BIOS_PATH))

    stub = session.bios_default_user_handler()
    assert stub is not None, "the fill routine must be found in the BIOS image"
    assert stub >= 0xFF0000, "the default handler must point INTO the BIOS"

    for slot in range(USER_VECTOR_TABLE_SLOTS):
        address = USER_VECTOR_TABLE_BASE + slot * 4
        word = int.from_bytes(
            bytes(session.memory[address + i] for i in range(4)), "little"
        )
        assert word == stub, f"slot {slot} at 0x{address:06X} was not seeded"


def test_the_default_handler_is_a_reti() -> None:
    """Not just *an* address -- the stub must actually return, or it is a trap."""
    session = EmulatorSession(str(ROM_PATH), bios_path=str(BIOS_PATH))
    stub = session.bios_default_user_handler()
    assert stub is not None

    bios = BIOS_PATH.read_bytes()
    assert bios[stub - 0xFF0000] == 0x07, "TLCS-900 RETI is opcode 0x07"


def test_native_core_agrees_with_the_python_core() -> None:
    session = EmulatorSession(str(ROM_PATH), bios_path=str(BIOS_PATH))
    stub = session.bios_default_user_handler()

    machine = native.NativeMachine(ROM_PATH.read_bytes(), bios=BIOS_PATH.read_bytes())
    machine.reset(bios_handoff=True)

    table = machine.read(USER_VECTOR_TABLE_BASE, USER_VECTOR_TABLE_SLOTS * 4)
    for slot in range(USER_VECTOR_TABLE_SLOTS):
        word = int.from_bytes(table[slot * 4 : slot * 4 + 4], "little")
        assert word == stub, f"native slot {slot} disagrees with the Python core"


def test_no_bios_means_no_invented_address() -> None:
    """A table seeded with a guess is worse than one left alone: it JUMPS there."""
    session = EmulatorSession(str(ROM_PATH))  # no BIOS attached
    assert session.bios_default_user_handler() is None
    assert HINT_USER_HOOK not in session.memory

    machine = native.NativeMachine(ROM_PATH.read_bytes())  # no BIOS
    machine.reset(bios_handoff=True)
    table = machine.read(USER_VECTOR_TABLE_BASE, USER_VECTOR_TABLE_SLOTS * 4)
    assert table == bytes(USER_VECTOR_TABLE_SLOTS * 4)


def test_the_hint_hook_survives_the_boot_that_used_to_power_the_console_off() -> None:
    """The end-to-end symptom: Fatal Fury enables INTT0 and never hooks it."""
    machine = native.NativeMachine(ROM_PATH.read_bytes(), bios=BIOS_PATH.read_bytes())
    machine.reset(bios_handoff=True)

    for _ in range(120):
        summary = machine.run_frames(1)

    # Sample the PC at several mid-frame points, not once at the frame boundary.
    # Fatal Fury's H-INT fires 152 times a frame and runs the BIOS trampoline
    # (FF22A5..) each time; since the H-INT pulse rides the raster's own clock,
    # its phase is DETERMINISTIC -- and for this ROM the frame boundary lands
    # inside that trampoline every frame. A single boundary sample then reads
    # "in the BIOS" forever while the game runs perfectly. What this test
    # guards is the powered-off console (PC parked in the BIOS for good), so
    # ask the question it means: does CARTRIDGE code get the CPU?
    in_cart = 0
    for _ in range(20):
        machine.run(500, record=False)
        if 0x200000 <= machine.cpu().pc < 0x400000:
            in_cart += 1
    assert in_cart >= 10, (
        f"cartridge code got the CPU in only {in_cart}/20 samples -- "
        "the console looks parked in the BIOS"
    )
    assert summary.executed > 0, "a powered-off console executes nothing"


def test_the_timers_see_the_power_on_io_page_not_a_phantom_zero() -> None:
    """TRUN powers on at 0x80 -- PRRUN already RUNNING -- and the timers must know.

    The 8-bit timers read TRUN/TREG/TxxMOD out of the writable overlay. An
    untouched overlay used to hand them `memory.get(0x20, 0)` = 0, so PRRUN read
    as STOPPED and the horizontal-blank counter stayed frozen until the game
    happened to write TRUN. That left the whole timer chain 440 cycles out of
    phase with the video clock -- enough for the H-blank interrupt to land one
    instruction early and for the CPU to take INTT0 where the native core (which
    seeds the I/O page at reset) correctly took VBlank. Gate G3 caught it.
    """
    from core.memory import io_reset_value
    from core.timers import (
        IRQ_VECTOR_INDEX_INTT0,
        T01MOD_ADDRESS,
        TREG0_ADDRESS,
        TRUN_ADDRESS,
        TRUN_PRESCALER,
        TimerController,
    )

    assert io_reset_value(TRUN_ADDRESS) & TRUN_PRESCALER, "TRUN powers on with PRRUN set"

    # The private H-blank accumulator this used to inspect is GONE: TI0 pulses
    # now come from the raster itself (RasterController.take_hint_pulses), so
    # the video clock cannot be out of phase with the timers by construction.
    # What remains to guard is the power-on plumbing end to end: with TRUN at
    # its documented reset value (+T0RUN), a raster pulse must reach timer 0.
    timers = TimerController()
    memory = {
        TRUN_ADDRESS: io_reset_value(TRUN_ADDRESS) | 0x01,
        TREG0_ADDRESS: 1,
        T01MOD_ADDRESS: 0x00,
    }
    assert timers.tick(0, memory, hint_pulses=1) == [IRQ_VECTOR_INDEX_INTT0], (
        "a raster H-INT pulse must clock timer 0 at power-on TRUN values"
    )
