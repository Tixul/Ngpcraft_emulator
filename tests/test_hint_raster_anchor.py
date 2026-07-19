"""The H-INT pulse is anchored to the RASTER, so a timer-driven raster split
lands on the SAME line every frame.

THE DEFECT THIS CONDEMNS (DEVLOG 2026-07-16, found on Metal Slug 1st Mission).
Timer 0 in mode 00 counts the K2GE's H-INT pin (TI0). Those pulses used to be
derived inside timer_tick from a PRIVATE cycle accumulator: same arithmetic as
the raster, WRONG CLOCK. Two measured consequences:

  1. its phase against the raster was whatever history left it at -- in Metal
     Slug it sat exactly ON a line boundary, so the ~50-cycle IRQ-delivery
     quantisation flipped the game's scroll split between two lines;
  2. every IRQ delivery advanced the raster 13 cycles the private accumulator
     never saw, so the phase also DRIFTED a full 515-cycle line every few
     frames -- the split beat up and down and the HUD's top line flickered
     ("la bande en haut du hud qui clignote en continu" -- playtest).

The spec pins the schedule (ngpcspec.txt): "The signal generation begins 1 H
before the Hardware Drawing Period starts. (Please be aware H_INT signal is not
generated at line 151 and signal generation for the 0th line occurs at the
beginning of line 198.)" -- 152 pulses/frame, each a full line ahead of the line
it announces. That full line is the silicon's safety margin, and it only exists
if the pulse rides the raster's own clock.

THE SCENARIO. A hand-assembled cartridge (every encoding verified through the
project disassembler) arms timer 0 on TI0 with TREG0 = 152: one match per frame,
at the SAME pulse each frame, since 152 is exactly the pulse count of a frame.
Its INTT0 handler is `inc 1, (0x8035)` -- S2SO.V grows by one at the match line,
so every frame's raster log carries the split at whatever line the ISR ran. The
real BIOS routes INTT0 through the user hook at 0x6FD4, exactly as games use it.

THE ASSERTION. Across 200 consecutive frames, the split line is ONE constant.
With the pulse on the raster's clock it cannot walk, because every clock in the
machine is the same clock.

⚖️ HONESTY NOTE, measured against the pre-fix DLL: THIS scenario is too quiet to
re-create the old defect (with only two stub deliveries a frame the old private
accumulator happened to hold phase), so this test guards the INVARIANT rather
than re-condemning the old binary. The condemnations live elsewhere: the unit
test "cycles alone carry no TI0 pulse" fails the old TimerController design
outright, the RasterController schedule test pins 198-and-not-151, and the
system-level measurement is on record (Metal Slug split wandering 127..130 over
120 frames before, 127 exactly 120/120 after -- DEVLOG 2026-07-16).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from core import native

BIOS_PATH = Path(__file__).resolve().parents[3] / "jeux officiel" / "bios_v10.bin"

pytestmark = pytest.mark.skipif(
    not BIOS_PATH.exists(), reason="needs the real BIOS image (user-hook routing)"
)

RASTER_LINES = 152
RASTER_REGS = 64


def _test_rom() -> bytes:
    """64-byte header (entry at 0x1C) + the verified program at 0x200040.

    0x200040  F1 D4 6F 02 62 00   ldw (0x6FD4), 0x0062   ; INTT0 user hook ->
    0x200046  F1 D6 6F 02 20 00   ldw (0x6FD6), 0x0020   ; ... 0x00200062
    0x20004C  F1 73 00 00 04      ld  (0x0073), 0x04     ; INTET01: INTT0 level 4
    0x200051  F1 22 00 00 98      ld  (0x0022), 0x98     ; TREG0 = 152
    0x200056  F1 24 00 00 00      ld  (0x0024), 0x00     ; T01MOD: T0 = TI0 pin
    0x20005B  06 00               ei  0
    0x20005D  68 FE               jr  $                  ; park
    0x20005F  00 00 00            (pad)
    0x200062  C1 35 80 61         inc 1, (0x8035)        ; the handler: S2SO.V++
    0x200066  07                  reti

    TRUN is deliberately NOT armed here: the harness arms it MID-FRAME, so the
    single per-frame match (TREG0 = 152 = one whole frame of pulses) lands mid
    frame too. A match at the frame boundary is real but invisible to a
    raster-log read taken exactly there -- the write straddles the wrap and
    every row carries the same value.
    """
    header = bytearray(0x40)
    header[0x1C:0x20] = (0x200040).to_bytes(4, "little")
    code = bytes.fromhex(
        "F1D46F026200"
        "F1D66F022000"
        "F173000004"
        "F122000098"
        "F124000000"
        "0600"
        "68FE"
        "000000"
        "C1358061"
        "07"
    )
    return bytes(header) + code


def _split_line(machine: native.NativeMachine) -> int | None:
    """The first line whose S2SO.V differs from line 0's -- the raster split."""
    log = machine.raster_log()
    base = log[0][0x35]
    for line in range(1, RASTER_LINES):
        if log[line][0x35] != base:
            return line
    return None


def test_timer_driven_raster_split_lands_on_one_line_every_frame() -> None:
    machine = native.NativeMachine(_test_rom(), bios=BIOS_PATH.read_bytes())
    machine.reset(bios_handoff=True)

    machine.run_frames(2)  # hook + timer config are in place; CPU parked on jr $
    # Arm TRUN mid-frame, so the one-match-per-frame lands mid-frame where a
    # frame-aligned raster-log read can actually see it.
    while True:
        summary = machine.run_frames(1, max_instrs=400)
        if 25 <= summary.scanline <= 60:
            break
    machine.write(0x0020, bytes([0x81]))  # TRUN: PRRUN + T0RUN

    machine.run_frames(2)  # settle
    # 200 frames, not 30: the OLD defect drifted the timer's phase ~13 cycles
    # per IRQ delivery, i.e. one 515-cycle line every ~40 quiet frames. A short
    # window can sit between two crossings and read stable by luck; 200 frames
    # spans several crossings, so the walk cannot hide.
    lines = []
    for _ in range(200):
        machine.run_frames(1)
        lines.append(_split_line(machine))

    assert all(x is not None for x in lines), (
        f"the INTT0 handler never ran -- no split seen at all: {lines}"
    )
    assert len(set(lines)) == 1, (
        "the raster split WALKED across lines -- the H-INT pulse is not on the "
        f"raster's clock: {lines}"
    )
