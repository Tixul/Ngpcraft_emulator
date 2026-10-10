"""run_frames never loses a breakpoint between its slices.

run_frames cuts a frame into ngpc_run slices, down to ONE instruction near the
frame's end. A run never stops on its own first instruction (that is how a
caller steps off the breakpoint it sits on), so every later slice used to skip
its first instruction too: a breakpoint landing on a slice start passed
unseen. NgpCraft Studio's debugger breaks on every executed VM instruction
and missed some of them.

Proof: two identical consoles. One counts how often the PC reaches X by
recording every instruction; the other stops on X with run_frames and resumes.
The counts must be equal over the same instructions.
"""
from __future__ import annotations

from collections import Counter
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
BIOS = REPO / "bios.bin"
ROM = REPO / "tests" / "roms" / "link_probe.ngc"

requires_rom = pytest.mark.skipif(
    not (BIOS.exists() and ROM.exists()),
    reason="needs the retail bios.bin (gitignored) and the probe ROM",
)


def _console():
    from core.native_session import NativeSession

    session = NativeSession(ROM, bios_path=BIOS, autosave=False)
    session.machine.run_frames(20)
    return session.machine


@requires_rom
def test_every_breakpoint_hit_is_reported_across_slices():
    from core import native

    tracer, stopper = _console(), _console()
    # The hottest PC of one frame, not the one the consoles stand on now.
    _, records = tracer.run(30_000, record=True)
    here = stopper.cpu().pc
    target = next(pc for pc, _ in Counter(r.pc for r in records).most_common() if pc != here)
    tracer, stopper = _console(), _console()

    stopper.set_breakpoints([target])
    hits, executed, frames = 0, 0, 0
    while frames < 6:
        summary = stopper.run_frames(1)
        executed += summary.executed
        if summary.stop_status == native.STATUS_BREAKPOINT:
            assert stopper.cpu().pc == target
            hits += 1
        else:
            frames += 1
    seen = 0
    left = executed
    while left:
        _, records = tracer.run(min(left, 50_000), record=True)
        seen += sum(r.pc == target for r in records)
        left -= len(records)
    assert hits > 50, "the chosen PC should be hot"
    assert hits == seen, f"{seen - hits} breakpoint hits lost between run_frames slices"
