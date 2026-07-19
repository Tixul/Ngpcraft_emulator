"""INC / DEC #n, r -- the flags depend on the SIZE, and only the byte form has any.

Toshiba's instruction list gives this opcode THREE rows, and they disagree:

    INC #3, r   `C8 + r`        size B - -   flags  * * * V 0 -
    INC #3, r   `C8 + zz + r`   size - W L   flags  - - - - - -
    INC<W> #3, (mem)            size B W -   flags  * * * V 0 -
    (DEC is identical, with N = 1 instead of 0.)

Both cores wrote flags at every size. They AGREED WITH EACH OTHER PERFECTLY, so no
differential harness could ever have caught it -- the same trap as the MUL/DIV `RR`
code. Only reading the table breaks that kind of tie.

It cost four ROMs. Faselei's `strlen` finds the NUL with a block compare and then
steps back onto it:

    243B74  cpir (XHL)      ; search -- sets Z when it MATCHES
    243B76  dec 1, XHL      ; back up onto the byte it found
    243B78  ret Z           ; "found" -- reading the CPIR's Z

Clobber Z in that DEC and every found string becomes "not found". `strlen` returns
its 0xFFFF not-found sentinel, the caller hands that to a memcpy, and 65 534 bytes
go over the stack -- return address included. The CPU returned to address 0, hit the
`swi 7` there, and the BIOS error handler powered the console off (DEVLOG pass 210).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from core import native

ROM_DIR = Path(__file__).resolve().parents[3] / "jeux officiel"
BIOS_PATH = ROM_DIR / "bios_v10.bin"
FASELEI = ROM_DIR / "Faselei! (Europe).ngc"

# A 32-bit DEC must leave Z exactly as it found it.
#   E8 = long register prefix, XHL (index 3) -> 0xEB ; 0x69 = DEC #1
DEC1_XHL = bytes((0xEB, 0x69))
#   C8 = byte register prefix, A (index 1) -> 0xC9 ; 0x69 = DEC #1
DEC1_A = bytes((0xC9, 0x69))

CODE_BASE = 0x200000


def _machine_running(code: bytes, *, pc: int) -> native.NativeMachine:
    """A machine whose ROM holds `code` at `pc`, ready to single-step."""
    rom = bytearray(0x200000)
    header = ROM_DIR  # only used for the size check below
    del header
    rom[0:4] = b"\x00\x00\x00\x00"
    rom[pc - CODE_BASE : pc - CODE_BASE + len(code)] = code
    machine = native.NativeMachine(bytes(rom))
    machine.reset(bios_handoff=True)
    cpu = machine.cpu()
    cpu.pc = pc
    machine.set_cpu(cpu)
    return machine


def _flags(machine: native.NativeMachine) -> int:
    return machine.cpu().flags


def test_long_dec_leaves_the_flags_alone() -> None:
    """The whole bug, in one instruction."""
    machine = _machine_running(DEC1_XHL, pc=0x201000)
    cpu = machine.cpu()
    cpu.regs[3] = 0x00004000          # XHL -- decrementing it must not touch Z
    cpu.flags = 0x40                  # Z set, as a CPIR that just MATCHED leaves it
    machine.set_cpu(cpu)

    machine.run(1)

    assert machine.cpu().regs[3] == 0x00003FFF, "the register must still decrement"
    assert machine.cpu().flags == 0x40, (
        "a 32-bit DEC wrote flags: the `ret Z` after a `cpir` will now be wrong"
    )


def test_long_dec_does_not_invent_a_zero_flag_either() -> None:
    """The other direction: it must not SET Z when the result reaches zero."""
    machine = _machine_running(DEC1_XHL, pc=0x201000)
    cpu = machine.cpu()
    cpu.regs[3] = 0x00000001          # -> 0, which a flag-setting DEC would call Z
    cpu.flags = 0x00                  # Z clear
    machine.set_cpu(cpu)

    machine.run(1)

    assert machine.cpu().regs[3] == 0
    assert machine.cpu().flags == 0x00, "a 32-bit DEC set Z on a zero result"


def test_the_byte_form_still_sets_the_flags() -> None:
    """Only the WORD and LONG rows are `- - - - - -`. The byte row is not."""
    machine = _machine_running(DEC1_A, pc=0x201000)
    cpu = machine.cpu()
    cpu.regs[0] = 0x00000100          # XWA: A (the low byte of WA) = 0x00... 
    cpu.regs[0] = 0x00000001          # A = 1 -> DEC gives 0
    cpu.flags = 0x00
    machine.set_cpu(cpu)

    machine.run(1)

    assert machine.cpu().flags & 0x40, "the BYTE form must still set Z"


@pytest.mark.skipif(
    not (FASELEI.exists() and BIOS_PATH.exists()),
    reason="needs the real BIOS image and Faselei",
)
def test_faselei_no_longer_powers_the_console_off() -> None:
    """End to end: the ROM the bug killed."""
    machine = native.NativeMachine(FASELEI.read_bytes(), bios=BIOS_PATH.read_bytes())
    machine.reset(bios_handoff=True)
    for _ in range(600):
        summary = machine.run_frames(1)

    cpu = machine.cpu()
    assert 0x200000 <= cpu.pc < 0x400000, (
        f"Faselei should be running its own code, not parked at 0x{cpu.pc:06X}"
    )
    assert summary.executed > 0, "a powered-off console executes nothing"
