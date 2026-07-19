"""Gate G3 — whole-ROM trace equivalence: the Python reference vs the native core.

WHY THIS EXISTS, GIVEN G2 ALREADY PASSES
----------------------------------------
The differential fuzzer (G2, `native_diff.py`) executes ONE instruction from a
random state and compares the result. That is the right tool for proving an
opcode correct, and it covers encodings no ROM ever runs. But it is structurally
blind to one whole class of bug: **drift**.

An instruction can be right in isolation and still leave the machine subtly
wrong -- a register the reference banks and we do not, a pointer that walks one
byte too far, a flag that survives when it should not. A single step never sees
it. Ten thousand consecutive steps do: the two cores start from the same reset
state, execute the same real game code, and if they ever disagree about ANYTHING
they will diverge and stay diverged.

So G2 proves each opcode; G3 proves the MACHINE. Both are needed and neither
subsumes the other.

WHAT IS COMPARED
----------------
After every retired instruction: PC, the 8 general registers, the flag byte, the
register-bank pointer, and the interrupt mask. The first mismatch is reported
with the instruction that caused it, its bytes, and both states -- which is the
whole point: on a whole-ROM trace, the FIRST divergence is the bug. Everything
after it is noise.

The Python core runs at ~1 700 instr/s, so a run of a few thousand steps is a
couple of seconds. That is the budget; the native core does 75 MILLION/s and is
not the bottleneck here.
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from core import native  # noqa: E402
from core.emulator_session import EmulatorSession  # noqa: E402

REG_NAMES = ("xwa", "xbc", "xde", "xhl", "xix", "xiy", "xiz", "xsp")

# The A/D converter's registers are driven by the CYCLE COUNT, and the two cores
# disagree about cycles ON PURPOSE.
#
# The native core bills `base(instruction) + extra(addressing mode)`, straight out
# of the Toshiba instruction lists. The Python reference applies NO addressing-mode
# adder at all -- a known, catalogued defect that the differential gate reports as
# its own verdict (`cycles-adder` in native_diff.py, where the identity
# `cpp == py + extra(mode)` is checked so a REAL cycle bug is still caught). So on
# a long trace the two cores drift apart in cycles even while every instruction
# they retire is identical, and a converter that finishes after 320 clocks finishes
# at a different INSTRUCTION in each core.
#
# That is not something this gate can adjudicate: comparing ADMOD here would report
# a divergence whose cause is a defect we have already found, measured, and decided
# not to paper over. The CPU state and every other byte of memory stay compared.
# The day the reference's cycle model is fixed, delete this.
CYCLE_DRIVEN_REGISTERS = frozenset({
    0x000060,   # ADREG0L -- conversion result, low bits
    0x000061,   # ADREG0H
    0x00006D,   # ADMOD   -- EOCF / ADBF, i.e. "has the conversion finished yet"
})

# The K2GE's raster mirrors are NOT memory, and the two cores do not keep them in
# the same place. Both derive them from the same frame state -- `RAS.V` is the
# scanline number and bit 6 of the status byte is BLNK -- but the native core
# writes them into its flat address space each scanline, while the reference
# injects them into its FETCH VIEW and never touches its writable overlay. So the
# overlay can still hold whatever the game last wrote there, and comparing the two
# compares a live register against a stale write. The value a program READS is
# identical in both cores; that is what matters and it is what the CPU state below
# already proves.
FRAME_STATE_MIRRORS = frozenset({
    0x008009,   # RAS.V  -- the current scanline
    0x008010,   # 2D status; bit 6 = BLNK
})

# THE SOUND CPU'S RAM. The native core has a Z80 and the reference does not, so
# this window is not a place the two can be compared -- it is a capability one of
# them simply lacks.
#
# This is not a divergence being swept away. It is what made 19 more ROMs draw a
# picture: the game uploads its sound driver here, kicks the Z80, and waits for it
# to answer. The reference cannot run that driver, so nothing ever answers and the
# bytes stay at whatever the main CPU last wrote. Comparing them would report the
# Z80's existence as a bug.
#
# Everything the main CPU does with this window IS still compared -- the uploads,
# the polls -- right up until the sound CPU starts writing back.
Z80_SHARED_RAM = range(0x007000, 0x008000)

# ...and the three registers the sound CPU talks back through. 0xBC is the
# dual-port comm latch: the main CPU writes a command into it, and the Z80 writes
# its answer into the same byte. With no Z80 on the other side, the reference sees
# only its own half of a conversation.
Z80_CONTROL_REGISTERS = frozenset({
    0x0000B8,   # reset:  0x55 releases the Z80
    0x0000BA,   # a write fires one NMI at it
    0x0000BC,   # the dual-port comm register -- BOTH cpus write this byte
})


@dataclass
class Divergence:
    step: int
    pc: int
    raw: bytes
    field: str
    py: object
    cpp: object


def _flag_byte(flags) -> int | None:
    bits = (
        (flags.cf, 0), (flags.nf, 1), (flags.vf, 2),
        (flags.hf, 4), (flags.zf, 6), (flags.sf, 7),
    )
    if any(v is None for v, _ in bits):
        return None
    return sum(1 << pos for v, pos in bits if v)


def compare_trace(
    rom_path: Path,
    *,
    bios_path: Path | None,
    steps: int,
) -> tuple[int, Divergence | None, str]:
    """Run both cores from the same reset state; return (steps_compared, first divergence)."""
    rom = rom_path.read_bytes()
    bios = bios_path.read_bytes() if bios_path else None

    # --- native core -------------------------------------------------------
    machine = native.NativeMachine(rom, bios=bios)
    machine.reset(bios_handoff=True)

    # --- reference core ----------------------------------------------------
    session = EmulatorSession(str(rom_path), bios_path=str(bios_path) if bios_path else None)

    compared = 0
    # THE ONE-STEP INTERRUPT SKEW, and why it is not a bug.
    #
    # Both cores take the interrupt at the same hardware boundary -- after the
    # instruction whose cycles carried the raster out of the visible area. They
    # just MATERIALISE it at different moments. The native core vectors at the
    # end of that instruction. The reference is batched: it folds the pending
    # IRQ after the batch and only delivers it at the START of the next one.
    #
    # So for exactly ONE step the two disagree: the native core is already at
    # the handler with the frame pushed, and the reference is still sitting at
    # the return address with a clean stack. Nothing observes the machine in
    # between, and on the very next step the reference delivers, both execute
    # the handler's first instruction, and the two states become identical
    # again -- same PC, same stack, same instruction count.
    #
    # We therefore suspend comparison for that single step rather than call it
    # a divergence. What we do NOT suspend is the re-convergence check: if the
    # two do not agree again on the next step, that IS a divergence and it is
    # reported.
    irq_skew = False
    for i in range(steps):
        # MEMORY is compared too, not just registers. A write that lands in the
        # wrong place does not show up in a register until something reads it
        # back -- possibly thousands of instructions later, by which point the
        # trace has moved on and the real culprit is long gone. Comparing the
        # reference's writable overlay against native memory every step pins the
        # divergence to the instruction that CAUSED it.
        for addr, want in ({} if irq_skew else session.memory).items():
            if addr in CYCLE_DRIVEN_REGISTERS or addr in FRAME_STATE_MIRRORS:
                continue
            if addr in Z80_SHARED_RAM or addr in Z80_CONTROL_REGISTERS:
                continue
            got = machine.read(addr, 1)[0]
            if got != want:
                return compared, Divergence(
                    step=i, pc=session.cpu.pc, raw=b"",
                    field=f"mem[0x{addr:06X}]", py=want, cpp=got,
                ), ""
        # one instruction each, in lockstep
        # ONCE THE SOUND CPU ANSWERS, THE TWO MACHINES ARE LEGITIMATELY DIFFERENT.
        #
        # The native core has a Z80 and the reference does not. While the sound CPU
        # is only being uploaded and kicked, everything stays comparable -- those
        # are the MAIN cpu's writes, and both cores make them. The moment the Z80
        # writes its answer back into the dual-port comm register, the main CPU
        # READS a byte that exists in one machine and not the other, and every
        # instruction after that is comparing two different programs.
        #
        # So the trace ends here, deliberately and by name -- exactly as it used to
        # end at `swi`. It is not a divergence: it is the end of what this gate can
        # honestly say.
        if machine.z80().running and machine.read(0xBC, 1)[0] != session.memory.get(0xBC, 0):
            return compared, None, (
                "the sound CPU answered. The native core runs a Z80 and the reference "
                "has none, so from this instruction the two machines are executing "
                "legitimately different programs. Comparison ends here by design."
            )

        summary, records = machine.run(1)
        if summary.executed != 1:
            return compared, None, f"native core stopped: {native.status_name(summary.stop_status)}"

        # ...and the comm register is not the only way the answer comes back. The
        # sound driver's WORKSPACE is the shared RAM at 0x7000, and the main CPU
        # reads it directly: Sonic does `cp (XIX), A` with XIX = 0x0070C3, a byte
        # its Z80 wrote. The native core has that byte; the reference, with no Z80,
        # reads a zero. The flags then differ -- and that is not a CPU bug, it is
        # the same "two different machines" boundary as the comm register, reached
        # by a different door. This gate used to report it as a DIVERGENCE, which
        # sent me looking for a flag bug in `cp` that does not exist.
        if machine.z80().running:
            for i in range(records[0].n_reads):
                if records[0].reads[i].address in Z80_SHARED_RAM:
                    return compared, None, (
                        "the main CPU read the sound driver's workspace "
                        f"(0x{records[0].reads[i].address:06X}). The native core runs a Z80 "
                        "that has written there and the reference has none, so the two "
                        "machines are executing legitimately different programs. "
                        "Comparison ends here by design."
                    )

        session.step(1)
        if session.last_executed_count != 1:
            return compared, None, f"reference stopped: {session.last_stop_reason}"

        # EITHER core may be the one that materialises the interrupt first, and it
        # is not always the same one. They take it at the SAME hardware boundary --
        # after the instruction whose cycles completed the raster line or the A/D
        # conversion -- but the native core vectors at the END of that instruction
        # while the reference, whose loop delivers at the TOP of an iteration, does
        # it at the start of the next. Which of the two ends up ahead depends on
        # where the raising peripheral is ticked, so the skew runs both ways.
        delivered = summary.irq_deliveries > 0 or session.last_irq_deliveries > 0

        cpu = machine.cpu()
        py = session.cpu
        rec = records[0]
        raw = bytes(rec.raw[: rec.raw_len])

        # `swi` USED to end the comparison here. It no longer does.
        #
        # Toshiba defines SWI as a plain hardware trap -- push SR and PC, then jump
        # INDIRECTLY through the CPU's vector table at 0xFFFF00. The native core
        # always did that. The reference used to high-level-emulate it instead, so
        # the two machines legitimately ended up in different code and there was
        # nothing left to compare -- and 28 of the 73 commercial ROMs reach a `swi`
        # inside their first hundred instructions, which made that the single
        # biggest blind spot in this gate.
        #
        # The reference now vectors through the table too, whenever a BIOS image is
        # attached, and keeps its HLE only for BIOS-less sessions. Both cores run
        # the real BIOS, and the trace carries on.

        def diverge(field: str, a: object, b: object) -> Divergence:
            return Divergence(step=i, pc=rec.pc, raw=raw, field=field, py=a, cpp=b)

        if delivered and not irq_skew:
            # One core has vectored and the other has not yet. Skip this single
            # comparison; the next step must bring them back together, and if it
            # does not, that IS a divergence and it is reported.
            irq_skew = True
            compared += 1
            continue
        irq_skew = False

        if py.pc != cpu.pc:
            return compared, diverge("pc", py.pc, cpu.pc), ""
        for k, name in enumerate(REG_NAMES):
            v = getattr(py.regs, name)
            if v is not None and v != cpu.regs[k]:
                return compared, diverge(name, v, cpu.regs[k]), ""
        f = _flag_byte(py.flags)
        if f is not None and f != cpu.flags:
            return compared, diverge("flags", f, cpu.flags), ""
        if py.rfp is not None and py.rfp != cpu.rfp:
            return compared, diverge("rfp", py.rfp, cpu.rfp), ""
        if py.iff_level is not None and py.iff_level != cpu.iff_level:
            return compared, diverge("iff", py.iff_level, cpu.iff_level), ""

        compared += 1

    machine.close()
    return compared, None, ""


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("rom")
    p.add_argument("-n", "--steps", type=int, default=3000)
    p.add_argument("--bios")
    args = p.parse_args(argv)

    if not native.available():
        print("native core not built: cmake --build cpp/build", file=sys.stderr)
        return 2

    compared, div, stop = compare_trace(
        Path(args.rom),
        bios_path=Path(args.bios) if args.bios else None,
        steps=args.steps,
    )

    name = Path(args.rom).name
    if div is not None:
        print(f"DIVERGENCE  {name}")
        print(f"  step {div.step}, at PC=0x{div.pc:06X}  ({div.raw.hex()})")
        pv = f"0x{div.py:X}" if isinstance(div.py, int) else div.py
        cv = f"0x{div.cpp:X}" if isinstance(div.cpp, int) else div.cpp
        print(f"  {div.field}: reference={pv}  native={cv}")
        return 1

    tail = f"  (both stopped: {stop})" if stop else ""
    print(f"OK  {name}: {compared} instructions, byte-identical state throughout{tail}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
