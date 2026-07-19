"""NGPC frame / scanline timing model (M3 Phase 0).

Foundational model for the M3 milestone: track the scanline counter
and frame count for one CPU run, and expose the VBlank predicate so
downstream code can decide whether `RAS.V` reads return a visible
or VBlank line and whether bit 6 of `2D Status` (0x8010) is set.

Phase 0 ships the **state** only — Phase 3.1 will wire it through the
read bus so `_read_runtime_bytes(0x008009)` returns the live scanline
and `0x008010` exposes the BLNK bit driven by `in_vblank`. Phase 3.2
adds IRQ delivery at VBlank/HBlank boundaries.

Source for the scanline budget:
- `01_SDK/docs/K2GETechRef.txt` § 4-7 Frame Rate Register + § 4-8
  Raster Position Register
- Hardware quote: "signal generation for the 0th line occurs at the
  beginning of line 198" → scanlines cycle 0..197 (198 total per
  frame)
- "H_INT signal is not generated at line 151" → visible region is
  lines 0..151 (152 scanlines), VBlank is lines 152..197 (46
  scanlines), matching the 152-pixel-tall NGPC LCD

These values are HW-canonical and do not depend on the REF register
at reset (`0x8006 = 0xC6`); REF is locked and not meant to be
modified by software.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace


# ⚖️ 199 — MEASURED ON SILICON, and it used to be 198.
#
# hw_calibration/bin/main.ngc (official Toshiba toolchain, flashed on a real NGPC)
# reads RAS.V (0x8009) and prints its MAXIMUM before the wrap. The console printed
# **00C6 = 198** -> the counter runs 0..198 -> the frame is **199 lines**.
#
# The Tech Ref's sentence -- "signal generation for the 0th line occurs at the
# beginning of line 198" -- is AMBIGUOUS (198th line, or index 198?). We had read it
# as 198 lines. The register is not ambiguous.
#
# ⛔ Do NOT "restore" 198 because the document sounds like it says so.
SCANLINES_PER_FRAME = 199
VISIBLE_SCANLINES = 152
VBLANK_SCANLINES = SCANLINES_PER_FRAME - VISIBLE_SCANLINES  # 46

# Frame rate at the canonical reset gear (CPU 6.144 MHz, REF=0xC6).
FRAMES_PER_SECOND = 60

# Cycles per scanline at canonical reset gear (6.144 MHz, REF=0xC6).
#
# 515, AND IT USED TO BE 517. That was an INFERENCE, and it was wrong.
#
# The only NUMBER the manufacturer gives is 515. K2GETechRef § 4-8:
#   "the 10 bit internal subtraction counter of the horizontal drawing
#    operation time (internally 515 clock) is read in to RAS.H"
# This constant used to be 517, derived by ASSUMING the frame rate is exactly
# 60.00 Hz and solving 6_144_000 / (60 * 198) = 517.17. The old comment even
# said so. But nothing documents 60.00 -- with 515 the refresh is 60.25 Hz, and
# that is an output of the hardware, not an input to it. We inferred a constant
# from a number we had assumed, and then used it as though it were measured.
#
# Two things agree on 515 against that inference:
#   - the manufacturer's text above;
#   - the widely-used TIMER_HINT_RATE of 515, "CPU Ticks between horizontal
#     interrupts" -- i.e. the FULL line, not just the drawing part.
# And a third, independent: with 517 our frame counter ran one frame behind the
# reference emulator's by frame ~1869 of Sonic's demo (they were byte-identical
# in game RAM, just numbered differently). With 515 the two line up exactly.
#
# The project's own rule, learned the hard way in pass 184: two documented
# sources beat an inference. Especially an inference built on an assumption.
CYCLES_PER_SCANLINE = 515

# Estimated CPU cycles per executed instruction (fallback placeholder).
#
# **Non-reference-mode approximation** — see HARDWARE_COMPAT_POLICY.md
# § 4.3. The true TLCS-900 cycle count varies per opcode (typically
# 2..14 cycles for the operations actually executed today). We keep a
# flat 8-cycle fallback for any executor path whose real timing is not
# populated yet. Phase 3.2.3b now overrides selected common opcodes
# directly in `core.execute` (`NOP`, `RETI`, `JP/JR/JRL`, `CALL/RET`,
# `EI/DI`, `LDF`, `LINK/UNLK`, ...), but unpopulated paths still fall
# back to this constant.
#
# Until the remaining TLCS-900 table rows are wired in, any
# reference-mode hardware fidelity claim that depends on cycle count
# is still approximate.
ESTIMATED_CYCLES_PER_INSTRUCTION = 8


def scanlines_elapsed_from_cycles(cycles: int) -> int:
    """Return the integer number of scanlines elapsed for `cycles` CPU clocks.

    Uses the canonical `CYCLES_PER_SCANLINE` (515 at gear 0). Negative
    input is rejected — advancement is monotone in Phase 3.x; rewind
    belongs to M5.
    """
    if cycles < 0:
        raise ValueError(
            f"scanlines_elapsed_from_cycles requires cycles >= 0; got {cycles}"
        )
    return cycles // CYCLES_PER_SCANLINE


def advance_frame_state_by_cycles(
    state: "FrameState", cycles: int,
) -> "FrameState":
    """Advance `state` by the integer scanlines implied by `cycles` CPU clocks.

    Wraps the scanline counter modulo `SCANLINES_PER_FRAME` and carries
    into `frame_count`. See `scanlines_elapsed_from_cycles` for the
    cycles → scanlines conversion. Backward-compatible default for
    callers that don't yet track cycles: pass `cycles=0` for a no-op.
    """
    return advance_scanlines(state, scanlines_elapsed_from_cycles(cycles))


# --- IRQ controller state model (Phase 3.2.2a) ---
#
# NGPC IRQ source → level mapping per `01_SDK/docs/NGPC_HW_QUICKREF.md`
# § 4 "VECTEURS D'INTERRUPTION UTILISATEUR" and § 8 "MICRO DMA".

# VBlank interrupt LEVEL.
#
# CORRECTED 2026-07-10 back to 4, on two authoritative sources that agree:
#   * The official SNK SDK (`01_SDK/docs/SysPro.txt`, "USER PROGRAM INTERRUPT
#     OPERATION VECTOR"): "It is forbidden to prohibit Vertical Blanking
#     Interrupt (**Interrupt level 4**) because the operation has system
#     involvement."
#   * The reference emulator gates VBlank on `statusIFF() <= 4` -- which, under
#     the Toshiba mask rule (an interrupt of level L is accepted when L >= IFF),
#     is exactly "level 4".
#
# It had been raised to 6 on 2026-07-03 by INFERENCE: the BIOS boot runs `ei 5`
# before its init `halt`, and the reasoning was "ei 5 only accepts level > 5, so
# VBlank must be 6 or the BIOS would halt forever". That inference rested on our
# mask gate, which was itself OFF BY ONE (we required L > IFF; the Toshiba CPU
# manual says L >= IFF). With the gate fixed, the premise collapses: `ei 5` does
# mask a level-4 VBlank, and the BIOS's init `halt` is woken by a HIGHER-priority
# source (a timer / power interrupt, whose level the BIOS itself programs through
# VECT_INTLVSET) -- which is precisely the interrupt-controller work still
# outstanding. Two documented sources beat one inference built on a bug.
IRQ_LEVEL_VBLANK = 4

# K2GE control register bit that ENABLES the VBlank interrupt. Real hardware
# only raises VBlank when this is set (reference emulator: `ram[0x8000] & 0x80`).
K2GE_CONTROL_ADDRESS = 0x008000
K2GE_VBLANK_IRQ_ENABLE_BIT = 0x80

# --- USER PROGRAM INTERRUPT VECTOR TABLE (RAM), per the official SNK SDK ------
#
# `01_SDK/docs/SysPro.txt`: the BIOS chains every interrupt to a user handler
# pointer stored in RAM at `0x6FB8 + index * 4`. This is where the long-standing
# magic address 0x006FCC actually comes from: it is simply slot 5 (VBlank).
#
#   idx  addr     source                       idx  addr     source
#   0-3  6FB8..   SWI 3..6                     7    6FD4     Timer 0
#   4    6FC8     RTC alarm                    8    6FD8     Timer 1
#   5    6FCC     Vertical Blanking            9    6FDC     Timer 2
#   6    6FD0     Interrupt from Z80           10   6FE0     Timer 3
#                                              11   6FE4     Serial transmit
#                                              12   6FE8     Serial receive
#                                              14-17 6FF0..  Micro DMA 0..3 end
#
# Cross-checked against the reference emulator, which raises exactly these
# indices (VBlank=5, Timer0=7, Timer1=8, serial=11/12, SWI 3..6 = 0..3).
IRQ_RAM_VECTOR_TABLE_BASE = 0x006FB8

IRQ_INDEX_RTC_ALARM = 4
IRQ_INDEX_VBLANK = 5
IRQ_INDEX_Z80 = 6
IRQ_INDEX_TIMER0 = 7
IRQ_INDEX_TIMER1 = 8
IRQ_INDEX_TIMER2 = 9
IRQ_INDEX_TIMER3 = 10
IRQ_INDEX_SERIAL_TX = 11
IRQ_INDEX_SERIAL_RX = 12


def irq_ram_vector_slot(index: int) -> int:
    """Address of the user (RAM) interrupt-vector slot for `index`."""
    return IRQ_RAM_VECTOR_TABLE_BASE + (index * 4)


# Slot 5 of the RAM vector table = the VBlank user hook. Kept as a name because
# the whole codebase (and the SDK) refers to it as "0x6FCC".
VBLANK_VECTOR_ADDRESS = IRQ_RAM_VECTOR_TABLE_BASE + (IRQ_INDEX_VBLANK * 4)  # 0x006FCC

# Interrupt-priority (INTxx) I/O registers: which register/nibble holds the
# programmable level for each source. This is exactly the mapping VECT_INTLVSET
# writes (see `_SWI1_INTLVSET_TABLE` in execute.py), and the same one the
# reference emulator reads when it gates a timer interrupt
# (`statusIFF() <= (ram[0x73] & 7)` for timer 0). VBlank is absent: its level is
# system-fixed at 4 and the SDK forbids masking it.
#   source index -> (I/O address, uses_high_nibble)
IRQ_PRIORITY_REGISTERS = {
    IRQ_INDEX_RTC_ALARM: (0x0070, False),
    IRQ_INDEX_Z80: (0x0071, True),
    IRQ_INDEX_TIMER0: (0x0073, False),
    IRQ_INDEX_TIMER1: (0x0073, True),
    IRQ_INDEX_TIMER2: (0x0074, False),
    IRQ_INDEX_TIMER3: (0x0074, True),
}

# Priority registers keyed by HARDWARE vector index (Toshiba Table 3.3 (1):
# vector value / 4), for sources delivered through `IrqState.pending_vectors`.
#
# The A/D converter shares register INTE0AD (0x0070) with the INT0 pin: INT0
# takes the low nibble (which is what VECT_INTLVSET's "RTC alarm" case writes)
# and the A/D completion interrupt takes the high nibble.
#   hw vector index -> (I/O address, uses_high_nibble)
IRQ_HW_PRIORITY_REGISTERS = {
    0x0070 // 4: (0x0070, True),  # 28 = INTAD (A/D conversion completion)
    0x0040 // 4: (0x0073, False),  # 16 = INTT0 (8-bit timer 0)
    0x0044 // 4: (0x0073, True),  # 17 = INTT1 (8-bit timer 1)
    0x0048 // 4: (0x0074, False),  # 18 = INTT2 (8-bit timer 2)
    0x004C // 4: (0x0074, True),  # 19 = INTT3 (8-bit timer 3)
}


def irq_level_from_priority_register(
    hw_vector_index: int, memory: dict[int, int]
) -> int | None:
    """Programmed priority level for a hardware vector, or None if unknown.

    A level of 0 means the source is disabled (Toshiba: levels run 1..7).
    """
    entry = IRQ_HW_PRIORITY_REGISTERS.get(hw_vector_index)
    if entry is None:
        return None
    address, high_nibble = entry
    raw = memory.get(address)
    if raw is None:
        return None
    return ((raw >> 4) if high_nibble else raw) & 0x07

# TLCS-900/H HARDWARE interrupt vector table (in BIOS ROM).
#
# On real silicon EVERY interrupt vectors through this table -- the CPU reads a
# 4-byte handler pointer at `IRQ_VECTOR_TABLE_BASE + index * 4` and jumps to it.
# On NGPC those handlers live in the SNK BIOS; the BIOS frame handler does its
# per-frame work and THEN chains to the user hook in RAM (`0x006FCC`).
#
# Dumped from the retail BIOS (2026-07-10), matching the pass-178 reverse:
#   vec[ 0] @0xFFFF00 = 0xFF204A  RESET (the boot entry point)
#   vec[ 8] @0xFFFF20 = 0xFF1898  power / system interrupt
#   vec[11] @0xFFFF2C = 0xFF2163  frame (VBlank) handler
#   vec[28] @0xFFFF70 = 0xFF2DCE  timer / ADC handler
#
# Jumping straight to `0x006FCC` (what we used to do unconditionally) is a
# HOMEBREW-only shortcut: homebrew installs its ISR there and runs without a
# BIOS image attached. It is NOT what hardware does. We now prefer the hardware
# table whenever a BIOS is attached, and keep the direct-0x6FCC path as the
# documented fallback for BIOS-less runs.
IRQ_VECTOR_TABLE_BASE = 0xFFFF00
IRQ_VECTOR_INDEX_VBLANK = 11


def irq_hw_vector_slot(index: int) -> int:
    """Address of hardware vector-table entry `index`."""
    return IRQ_VECTOR_TABLE_BASE + (index * 4)


@dataclass(frozen=True)
class IrqState:
    """Pending-interrupt snapshot.

    `pending_mask` is the original VBlank-only representation (bit
    `IRQ_LEVEL_VBLANK`). It is kept because savestates persist it.

    `pending_vectors` carries every OTHER pending source, keyed by its
    **hardware vector-table index** (Toshiba Table 3.3 (1): the vector value
    divided by 4). That is the chip's own identifier for a source, so it works
    for the A/D converter, the timers and the serial ports without inventing a
    numbering of our own. Example: INTAD has vector value 0x0070, hence index
    28, hence table entry 0xFFFF70.

    Both are **runtime state**, not configuration: software writes to
    `0x008010` etc. don't change them. The executor clears a source as it
    delivers it.
    """

    pending_mask: int = 0
    pending_vectors: frozenset[int] = frozenset()

    def is_vblank_pending(self) -> bool:
        return bool(self.pending_mask & (1 << IRQ_LEVEL_VBLANK))

    def with_vblank_pending(self) -> "IrqState":
        return replace(
            self, pending_mask=self.pending_mask | (1 << IRQ_LEVEL_VBLANK)
        )

    def with_vblank_cleared(self) -> "IrqState":
        return replace(
            self, pending_mask=self.pending_mask & ~(1 << IRQ_LEVEL_VBLANK)
        )

    def is_vector_pending(self, hw_vector_index: int) -> bool:
        return hw_vector_index in self.pending_vectors

    def with_vector_pending(self, hw_vector_index: int) -> "IrqState":
        return replace(
            self, pending_vectors=self.pending_vectors | {hw_vector_index}
        )

    def with_vector_cleared(self, hw_vector_index: int) -> "IrqState":
        return replace(
            self, pending_vectors=self.pending_vectors - {hw_vector_index}
        )


def initial_irq_state() -> IrqState:
    """Return the post-reset IRQ state: nothing pending."""
    return IrqState(pending_mask=0)


def fold_vblank_irq_pending(
    irq_state: IrqState,
    transitions: tuple,
) -> IrqState:
    """Update `irq_state` for any `enter` VBlank transition in `transitions`.

    Pure folder: when a transition with `kind == "enter"` is observed
    in the advancement event list (from `detect_vblank_transitions`),
    set the VBlank pending bit. `leave` transitions don't clear the
    bit — the executor clears it on IRQ delivery (Phase 3.2.2b), or
    via explicit ack at the IRQ controller (Phase 3.2.2c).

    Returns the new (possibly identical) `IrqState`.
    """
    for transition in transitions:
        if transition.kind == "enter":
            irq_state = irq_state.with_vblank_pending()
    return irq_state


@dataclass(frozen=True)
class FrameState:
    """K2GE frame/scanline state at one observable moment.

    `scanline` is the current scanline counter `0..197`. Values
    `0..151` are visible (the 160×152 LCD region). Values `152..197`
    are VBlank.

    `frame_count` is the running count of completed frames since
    reset — wraps modulo 2**32 to keep savestate payloads compact;
    overflow happens after ~2 years of continuous 60 fps emulation.
    """

    scanline: int
    frame_count: int

    @property
    def in_vblank(self) -> bool:
        """True when the current scanline is in the VBlank region."""
        return self.scanline >= VISIBLE_SCANLINES

    @property
    def in_visible_region(self) -> bool:
        """True when the current scanline is in the visible LCD region."""
        return self.scanline < VISIBLE_SCANLINES


def initial_frame_state() -> FrameState:
    """Return the post-reset frame state: scanline 0, frame 0."""
    return FrameState(scanline=0, frame_count=0)


def advance_scanlines(state: FrameState, n: int) -> FrameState:
    """Advance the scanline counter by `n` scanlines.

    Wrapping is automatic: when the counter passes `SCANLINES_PER_FRAME - 1`
    it resets to 0 and `frame_count` is incremented by the appropriate
    amount. Negative `n` is rejected (the timing model is monotonic in
    Phase 0 — rewind belongs to M5).
    """
    if n < 0:
        raise ValueError(f"advance_scanlines requires n >= 0; got {n}")
    if n == 0:
        return state
    total = state.scanline + n
    new_frame_count = (state.frame_count + total // SCANLINES_PER_FRAME) & 0xFFFFFFFF
    new_scanline = total % SCANLINES_PER_FRAME
    return FrameState(scanline=new_scanline, frame_count=new_frame_count)


def advance_frames(state: FrameState, n: int) -> FrameState:
    """Advance `n` full frames, snapping the scanline back to 0.

    Semantically: "skip ahead `n` frame boundaries from the current
    state's frame". The scanline within the current frame is
    discarded; the result always lands at `scanline=0` of the n-th
    next frame. Negative `n` is rejected (see `advance_scanlines`).
    """
    if n < 0:
        raise ValueError(f"advance_frames requires n >= 0; got {n}")
    if n == 0:
        return state
    new_frame_count = (state.frame_count + n) & 0xFFFFFFFF
    return FrameState(scanline=0, frame_count=new_frame_count)


@dataclass(frozen=True)
class VBlankTransition:
    """One enter/leave-VBlank event observed during a scanline advance.

    `kind` is `"enter"` (visible → VBlank, scanline crosses 151→152)
    or `"leave"` (VBlank → visible, scanline crosses 197→0 i.e. frame
    boundary).

    `scanline` is the scanline AT the event — for `"enter"` it's the
    first VBlank scanline (152). For `"leave"` it's 0 (the first
    visible scanline of the new frame).

    `frame_count` is the frame count AT the event — `"leave"` events
    report the new frame's count (post-increment).
    """

    kind: str
    scanline: int
    frame_count: int


def detect_vblank_transitions(
    state: FrameState, n: int,
) -> tuple[VBlankTransition, ...]:
    """Enumerate VBlank enter/leave events that would occur while
    advancing `state` by `n` scanlines.

    The pure-state model doesn't need to fire IRQs (that's Phase 3.2)
    but reporting transitions makes the `tick-frame` CLI useful for
    diagnostics and lets future raster IRQ code consume the same
    sequence.

    Each emitted transition reports the state AT the boundary; the
    final post-advance state is `advance_scanlines(state, n)`.
    """
    if n < 0:
        raise ValueError(f"detect_vblank_transitions requires n >= 0; got {n}")
    transitions: list[VBlankTransition] = []
    current = state
    remaining = n
    while remaining > 0:
        if current.in_visible_region:
            # Next enter-VBlank is at scanline VISIBLE_SCANLINES of the
            # current frame.
            steps_to_enter = VISIBLE_SCANLINES - current.scanline
            if steps_to_enter <= remaining:
                current = advance_scanlines(current, steps_to_enter)
                transitions.append(
                    VBlankTransition(
                        kind="enter",
                        scanline=current.scanline,
                        frame_count=current.frame_count,
                    )
                )
                remaining -= steps_to_enter
            else:
                break
        else:
            # In VBlank — next leave is at frame wrap (scanline 0 of
            # next frame).
            steps_to_leave = SCANLINES_PER_FRAME - current.scanline
            if steps_to_leave <= remaining:
                current = advance_scanlines(current, steps_to_leave)
                transitions.append(
                    VBlankTransition(
                        kind="leave",
                        scanline=current.scanline,
                        frame_count=current.frame_count,
                    )
                )
                remaining -= steps_to_leave
            else:
                break
    return tuple(transitions)


@dataclass
class RasterController:
    """The video clock, ticked PER INSTRUCTION -- like the timers and the A/D.

    It used to be the odd one out. `run_steps` ticks the A/D and the 8-bit timers
    after every instruction, so INTAD and INTT0..3 become pending at the exact
    instruction boundary that raises them. VBlank did NOT: the session advanced
    the scanline counter and folded VBlank's pending bit only once the whole
    batch had run.

    That lag is invisible while instructions are short -- and decides the wrong
    interrupt the moment one is long. Fatal Fury copies its sound driver into the
    Z80's RAM with a single `ldir` of 2798 bytes: ONE instruction, 19 587 cycles,
    thirty-eight scanlines. It starts at scanline 148 and ends at 186, well
    inside VBlank. The native core sees VBlank (level 4) pending at the end of
    it and takes it. This core still had only INTT0 (level 3) pending, took THAT,
    and the two cores ran different handlers for hundreds of instructions --
    which is what Gate G3 reported (DEVLOG pass 208).

    The video clock is a clock. It runs while the instruction runs.
    """

    frame_state: FrameState = field(default_factory=initial_frame_state)
    cycle_residue: int = 0
    # H-INT pulses produced but not yet consumed by timer 0 (TI0, mode 00).
    # ngpcspec.txt: "The signal generation begins 1 H before the Hardware
    # Drawing Period starts. (H_INT signal is not generated at line 151 and
    # signal generation for the 0th line occurs at the beginning of line 198.)"
    # => a pulse at the START of lines 198 and 0..150, 152 per frame, each a
    # full line ahead of the line it announces. Pulsing here, on the raster's
    # own clock, is what keeps a game's scroll split on ONE line -- a private
    # accumulator inside the timers drifted against the raster and Metal Slug's
    # HUD split flickered across a line boundary (DEVLOG 2026-07-16).
    pending_hint_pulses: int = 0

    def reset(self) -> None:
        self.frame_state = initial_frame_state()
        self.cycle_residue = 0
        self.pending_hint_pulses = 0

    def take_hint_pulses(self) -> int:
        """Drain the H-INT pulses raised since the last call (for timer 0)."""
        pulses = self.pending_hint_pulses
        self.pending_hint_pulses = 0
        return pulses

    def tick(self, cycles: int) -> tuple[VBlankTransition, ...]:
        """Advance the raster by `cycles` and report the VBlank edges crossed."""
        if cycles <= 0:
            return ()
        self.cycle_residue += cycles
        scanlines, self.cycle_residue = divmod(self.cycle_residue, CYCLES_PER_SCANLINE)
        if scanlines == 0:
            return ()
        start = self.frame_state.scanline
        for k in range(1, scanlines + 1):
            line = (start + k) % SCANLINES_PER_FRAME
            if line == SCANLINES_PER_FRAME - 1 or line <= VISIBLE_SCANLINES - 2:
                self.pending_hint_pulses += 1
        transitions = detect_vblank_transitions(self.frame_state, scanlines)
        self.frame_state = advance_scanlines(self.frame_state, scanlines)
        return transitions
