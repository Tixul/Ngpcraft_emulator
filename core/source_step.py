"""Source-level stepping: run until the C line changes.

Works on anything shaped like the native machine (`cpu().pc`, `cpu().regs[7]`
= XSP, `read(addr, n)`, `run(n, record=False)`, `set_breakpoints(pcs)`), so it
is tested headless and driven by the debug window alike.

Three commands, the ones every debugger has:
  * INTO (F11): stop at the next C line, following a call into a function that
    HAS line info (stopping at its entry). A call into code without any (BIOS,
    hand-written asm, a library) is run to its return in one go -- stepping into
    a BIOS routine you cannot read helps nobody.
  * OVER (F10): the same, but every call runs to its return.
  * OUT (Shift+F11): run until the current function returns.

⛔ THE STACK POINTER DOES NOT TELL WHICH FUNCTION WE ARE IN. A prologue pushes
registers and a call site pushes its arguments: XSP goes down INSIDE the same
function, and a rule "deeper than where we started = inside a callee" skipped
whole lines (first version, 23/11). So nothing here compares XSP levels; each
event that changes the function is recognised exactly, after one instruction:
  * CALL: XSP went down by 4 and the long it points at is an address just past
    the old PC -- the return address.
  * INTERRUPT: XSP went down by 6 (SR at XSP, PC at XSP+2) and that PC is the
    old PC or just past it. Run to its RETI in one go: an ISR is not the code
    being stepped.
  * RETURN: the instruction just executed was RET / RETD / RETI / RET cc and
    control did not fall through.
Calls and interrupts that are run "in one go" come back to the same function,
so between those events we are always in the function we started in.

Stop rules for INTO / OVER:
  * a different line (or no line info: we left the C code) -> stop;
  * back at the FIRST address of the starting line (a one-line loop went round)
    -> stop, so a `while (x) x--;` steps once per turn, as elsewhere;
  * the function returned -> stop in the caller;
  * a user breakpoint -> stop.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Optional

from core.symbols import SourceLines, SourcePos

# Same backstop as the instruction-level steppers: several frames' worth.
STEP_LIMIT = 2_000_000
# A call instruction is at most this long; the return address it pushes is the
# address of the next instruction, so it lies within this distance past the PC.
_MAX_INSN_LEN = 8
# How many instructions one run-to-return crossing takes at most.
_RUN_CHUNK = 4096

# TLCS-900/H returns (toolchain doc, "Program control"): RET = 0E, RETD = 0F d16,
# RETI = 07, RET cc = B0 F0+cc.
_RET, _RETD, _RETI, _RETCC_PREFIX = 0x0E, 0x0F, 0x07, 0xB0


@dataclass
class StepResult:
    ran: int                     # instructions executed
    reason: str                  # "line", "entered", "returned", "breakpoint",
                                 # "left-c", "limit", "stopped"
    pos: Optional[SourcePos]     # where we stopped, if it is C


def _sp(m) -> int:
    return int(m.cpu().regs[7]) & 0xFFFFFFFF


def _pc(m) -> int:
    return int(m.cpu().pc) & 0xFFFFFF


def _read(m, addr: int, n: int) -> bytes:
    try:
        return bytes(m.read(addr & 0xFFFFFF, n))
    except Exception:
        return b""


def _pushed_return(m, old_pc: int, old_sp: int) -> Optional[int]:
    """The last instruction was a call: the address it will return to."""
    sp = _sp(m)
    if sp != (old_sp - 4) & 0xFFFFFFFF:
        return None
    raw = _read(m, sp, 3)
    if len(raw) < 3:
        return None
    ret = int.from_bytes(raw, "little")
    return ret if old_pc < ret <= old_pc + _MAX_INSN_LEN else None


def _interrupt_return(m, old_pc: int, old_sp: int) -> Optional[int]:
    """An interrupt was taken: the address its RETI goes back to."""
    sp = _sp(m)
    if sp != (old_sp - 6) & 0xFFFFFFFF:
        return None
    raw = _read(m, sp + 2, 3)
    if len(raw) < 3:
        return None
    ret = int.from_bytes(raw, "little")
    return ret if old_pc <= ret <= old_pc + _MAX_INSN_LEN else None


def _was_return(m, old_pc: int, op: bytes) -> bool:
    """The instruction at `old_pc` (bytes `op`, read BEFORE running it) was a
    return that was taken."""
    if not op:
        return False
    b0 = op[0]
    is_ret = b0 in (_RET, _RETD, _RETI) or (
        b0 == _RETCC_PREFIX and len(op) > 1 and (op[1] & 0xF0) == 0xF0)
    if not is_ret:
        return False
    pc = _pc(m)
    return not (old_pc < pc <= old_pc + 3)     # RET cc not taken falls through


def _run_to(m, ret: int, call_sp: int, user_bps: list[int],
            limit: int) -> tuple[int, bool, str]:
    """Run until the call/interrupt that pushed `ret` comes back (PC == ret with
    XSP back at the level before it). Returns (ran, back, why)."""
    # ⛔ The core IGNORES a breakpoint on the first instruction of every `run` call
    # (that is how it steps off one). A chunk that happens to end right before `ret`
    # would therefore sail past it: the PC is checked HERE, before each chunk.
    # Recursion (PC == ret but XSP still below) needs nothing special: the next
    # chunk's first instruction steps off our own breakpoint.
    ran = 0
    try:
        m.set_breakpoints(sorted(set(user_bps) | {ret}))
        while ran < limit:
            pc = _pc(m)
            if pc == ret and _sp(m) >= call_sp:
                return ran, True, ""
            if ran > 0 and pc in user_bps:
                return ran, False, "breakpoint"
            summ, _ = m.run(min(_RUN_CHUNK, limit - ran), record=False)
            ran += int(summ.executed)
            if int(summ.executed) == 0:
                return ran, False, "stopped"
    finally:
        try:
            m.set_breakpoints(sorted(set(user_bps)))
        except Exception:
            pass
    return ran, False, "stopped"


def _walk(m, lines: SourceLines, mode: str, bps: list[int], limit: int,
          crashed) -> StepResult:
    start_pc = _pc(m)
    start = lines.at(start_pc)
    ran = 0
    while ran < limit:
        old_pc, old_sp = _pc(m), _sp(m)
        op = _read(m, old_pc, 2)
        summ, _ = m.run(1, record=False)
        n = int(summ.executed)
        ran += n
        if n == 0 or (crashed is not None and crashed(summ)):
            return StepResult(ran, "stopped", lines.at(_pc(m)))
        # --- events that change the function --------------------------------
        back_to = _interrupt_return(m, old_pc, old_sp)
        if back_to is None:
            ret = _pushed_return(m, old_pc, old_sp)
            if ret is not None:
                callee = lines.at(_pc(m))
                if mode == "into" and callee is not None:
                    return StepResult(ran, "entered", callee)
                back_to = ret
        returned_from_call = False
        if back_to is not None:
            r, back, why = _run_to(m, back_to, old_sp, bps, limit - ran)
            ran += r
            if not back:
                return StepResult(ran, why or "stopped", lines.at(_pc(m)))
            # Back where the call was made. The return address can be the FIRST
            # address of the next line: judge it now, not after one more instruction.
            returned_from_call = True
        pc = _pc(m)
        pos = lines.at(pc)
        if not returned_from_call and _was_return(m, old_pc, op):
            if mode == "out" or pos is not None:
                return StepResult(ran, "returned", pos)
            return StepResult(ran, "left-c", None)
        if pc in bps and not returned_from_call:
            return StepResult(ran, "breakpoint", pos)
        if mode == "out":
            continue
        # --- same function: line rules ----------------------------------------
        if pos is None:
            if start is None:
                continue                   # started outside C: walk until we reach some
            return StepResult(ran, "left-c", None)
        if start is None or (pos.file, pos.line) != (start.file, start.line):
            return StepResult(ran, "line", pos)
        if pc == start.start:
            return StepResult(ran, "line", pos)    # one-line loop went round
    return StepResult(ran, "limit", lines.at(_pc(m)))


def step_line(m, lines: SourceLines, *, over: bool,
              user_breakpoints: Iterable[int] = (), limit: int = STEP_LIMIT,
              crashed=None) -> StepResult:
    """INTO (`over=False`) or OVER (`over=True`) one C line.
    `crashed(summary) -> bool` lets the caller stop on its own crash statuses."""
    bps = sorted({int(b) & 0xFFFFFF for b in user_breakpoints})
    return _walk(m, lines, "over" if over else "into", bps, limit, crashed)


def step_out(m, lines: SourceLines, *, user_breakpoints: Iterable[int] = (),
             limit: int = STEP_LIMIT, crashed=None) -> StepResult:
    """Run until the current function returns; calls and interrupts on the way
    run in one go, so leaving a routine that waits a few frames costs chunks,
    not one crossing per instruction."""
    bps = sorted({int(b) & 0xFFFFFF for b in user_breakpoints})
    return _walk(m, lines, "out", bps, limit, crashed)
