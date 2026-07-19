"""Stateful emulator session — drives execution forward for the UI.

The CLI surface (step-exec, run-steps, etc.) is stateless: each call
re-loads the ROM, seeds from a savestate, runs once, emits an output.
That contract is great for batch / CI / engine-bridge work but
awkward for a GUI that needs to step interactively from a live state.

`EmulatorSession` holds the live CPU + memory overlay + frame_state +
irq_state in memory across calls, exposing simple verbs (`reset`,
`step`, `load_savestate`, `save_savestate`, `render_lcd`,
`snapshot`). It composes the same underlying primitives the CLI uses
(`build_run_steps`, `render_frame`, `build_savestate_payload`) so
the live behavior matches CLI output byte-for-byte at the same seed.

An optional external 64 KB BIOS image can also be attached to the
session. This closes the gap between the live debugger UI and the
CLI's `--bios` support: ROMs that read through `0xFF0000..0xFFFFFF`
can keep stepping toward a visible frame instead of stopping on an
unbacked BIOS read.

Auto VBlank IRQ pending: after each `step`, the session detects
VBlank transitions in the cycle-driven frame_state advance and
folds them into `irq_state` via `fold_vblank_irq_pending`. The next
step then samples the pending bit through the executor's
`try_deliver_pending_irq` and may deliver the IRQ — closing the
real-HW loop in a UI-driven run.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path

from core.breakpoints import (
    Breakpoint,
    breakpoints_path_for_rom,
    load_breakpoints,
    save_breakpoints,
)
from core.cpu import (
    BankedByteRegisters,
    GeneralRegisters32,
    NgpcCpuState,
    StatusFlags,
    create_unknown_control_registers,
)
from core.decode import DecodeResult, decode_instruction_at
from core.fetch import NgpcFetchView, load_fetch_view
from core.adc import AdcController
from core.flash import FlashController
from core.timers import TimerController
from core.frame_timing import (
    CYCLES_PER_SCANLINE,
    SCANLINES_PER_FRAME,
    FrameState,
    IrqState,
    RasterController,
    advance_scanlines,
    detect_vblank_transitions,
    fold_vblank_irq_pending,
    initial_frame_state,
    initial_irq_state,
)
from core.k2ge import (
    K2geControlRegisters,
    K2gePalette,
    K2geSprite,
    K2geTilemapEntry,
    read_all_palettes,
    read_control_registers,
    read_oam_sprites,
    read_tilemap,
)
from core.machine import load_machine_state
from core.renderer import frame_to_ppm_bytes, render_frame
from core.run_steps import RunStepsResult, build_run_steps
from core.seed_presets import (
    BIOS_HANDOFF_INTNEST as SHARED_BIOS_HANDOFF_INTNEST,
    BIOS_HANDOFF_XSP as SHARED_BIOS_HANDOFF_XSP,
)
from core.savestate import (
    build_savestate_payload,
    load_savestate,
    save_savestate,
)
from core.symbols import SymbolTable, load_map
from core.watchpoints import (
    WATCHPOINT_KINDS,
    Watchpoint,
    load_watchpoints,
    save_watchpoints,
    watchpoints_path_for_rom,
)


@dataclass(frozen=True)
class SessionSnapshot:
    """Read-only view of the live session for UI panels.

    Frozen so UI code can hold a reference without worrying about
    the session mutating it mid-render.
    """

    cpu: NgpcCpuState
    memory: dict[int, int]
    frame_state: FrameState
    irq_state: IrqState
    total_cycles_consumed: int
    last_stop_reason: str | None
    last_executed_count: int
    last_irq_deliveries: int


class EmulatorSession:
    """Live emulator state held in memory, mutated by `step` / `reset`.

    Construction loads the ROM and resets to bootstrap. The session
    can also be re-seeded from a savestate via `load_savestate(path)`.

    All execution flows through `build_run_steps` with the live
    `cpu_state`, `memory_bytes`, and `irq_state`. After each batch:
    - `frame_state` advances by `result.total_cycles_consumed`.
    - VBlank transitions across the advance are folded into
      `irq_state` so the next batch can deliver the pending IRQ.
    """

    # ----- BIOS hand-off (UI 0.7) -----
    #
    # The real NGPC BIOS initializes a handful of CPU registers before
    # transferring control to the cart entry-point. Without this, even
    # the first instruction of a real ROM typically blocks because
    # `CALL` / `PUSH` etc. need a known XSP. Most values below are
    # sourced from official docs; `INTNEST = 0` is a narrow hand-off
    # invariant ("not inside an active nested IRQ chain") documented
    # in `RESET_STATE.md`.
    #
    # Sources :
    # - `01_SDK/docs/NGPC_HW_QUICKREF.md §2 (MEMORY MAP)` documents
    #   `0x004000–0x006BFF` as 12 KB user RAM and
    #   `0x006C00–0x006FFF` as system-reserved. The BIOS places the
    #   user stack at the top of the user RAM, growing downward —
    #   so `XSP = 0x00006C00` is the canonical hand-off value.
    # - `01_SDK/docs/ngpcspec.txt §INTERRUPT STATE` : *"The software
    #   starts up with interrupts prohibited (DI)"* → `iff_level = 7`
    #   (the TLCS-900/H "all maskable IRQs blocked" mask).
    # - Default register bank is 0 (`rfp = 0`) — the BIOS uses bank 3
    #   for its own state and hands off in bank 0.
    # - Six ALU flags are typically cleared on cold start ;
    #   `nf=0/zf=0/vf=0/hf=0/cf=0/sf=0` is the safest BIOS-equivalent.
    BIOS_HANDOFF_XSP = SHARED_BIOS_HANDOFF_XSP
    BIOS_HANDOFF_IFF_LEVEL = 7  # DI per ngpcspec.txt INTERRUPT STATE
    BIOS_HANDOFF_RFP = 0        # user bank
    BIOS_HANDOFF_INTNEST = SHARED_BIOS_HANDOFF_INTNEST  # hand-off invariant: no IRQ nesting in flight
    BIOS_HANDOFF_FLAGS = StatusFlags(
        sf=False, zf=False, vf=False, hf=False, cf=False, nf=False,
    )
    JOYPAD_ADDRESS = 0x006F82
    JOYPAD_UP = 0x01
    JOYPAD_DOWN = 0x02
    JOYPAD_LEFT = 0x04
    JOYPAD_RIGHT = 0x08
    JOYPAD_A = 0x10
    JOYPAD_B = 0x20
    JOYPAD_OPTION = 0x40
    JOYPAD_POWER = 0x80

    # `frame_state` is the RASTER's, republished. It used to be a plain attribute
    # that the session advanced after each batch; the video clock now ticks per
    # instruction inside `build_run_steps` (beside the A/D and the timers), so the
    # counter lives in `self.raster` and this is the window onto it. Assignment
    # still works -- seeding a scanline (savestates, tests) pushes into the clock.
    @property
    def frame_state(self) -> FrameState:
        return self.raster.frame_state

    @frame_state.setter
    def frame_state(self, value: FrameState) -> None:
        self.raster.frame_state = value

    def __init__(
        self, rom_path: Path, *,
        apply_bios_handoff: bool = True,
        bios_path: Path | None = None,
        auto_wake_on_halt: bool = True,
    ) -> None:
        self.rom_path = Path(rom_path)
        self.machine = load_machine_state(self.rom_path)
        # Whether reset() reapplies the BIOS hand-off seed. Kept as
        # an instance attribute so a UI command could toggle it later
        # (e.g. "strict bootstrap, no BIOS HLE").
        self._apply_bios_handoff = apply_bios_handoff
        # Whether `step` auto-wakes a `HALT` on the next VBlank instead
        # of stopping dead. This is HARDWARE-FAITHFUL, not a repair: on
        # real HW `HALT` only parks the CPU until the video clock raises
        # VBlank, which then wakes it into the 0x6FCC handler. With this
        # on, the BIOS boot's `ei 5; halt` idle loop self-drives across
        # frames instead of freezing the run loop. A genuinely stuck
        # halt (iff >= 6, even VBlank masked) still stops honestly.
        # A debugger can set this False to stop AT the halt for
        # inspection. Auto-wakes are counted in `last_auto_wakes` so the
        # behavior is observable, never silent (ROADMAP §5.11).
        self.auto_wake_on_halt = auto_wake_on_halt
        # CPU / memory / timing / IRQ — the live mutable state.
        # Bootstrap CPU is augmented with BIOS hand-off state below.
        self.cpu: NgpcCpuState = self._seed_bios_handoff_state(
            self.machine.cpu,
        ) if apply_bios_handoff else self.machine.cpu
        self.memory: dict[int, int] = {}
        # Cached fetch view (see `_build_fetch_view`): rebuilding it per batch
        # re-read the ROM from disk and was the single biggest cost in the run
        # loop. Invalidated whenever the ROM image or BIOS backing changes.
        self._cached_fetch_view: NgpcFetchView | None = None
        # The video clock. It is ticked PER INSTRUCTION inside `build_run_steps`
        # (beside the A/D and the timers), NOT folded in after the batch -- a
        # single `ldir` can span 38 scanlines, and a late VBlank means the wrong
        # interrupt gets taken. `frame_state` below republishes what it computed.
        self.raster = RasterController()
        self.irq_state: IrqState = initial_irq_state()
        # Cartridge flash write model (direct AMD command path). Owned by the
        # session so its armed state survives across step batches. Commits land
        # in `self.memory` (the writable overlay), which shadows the cart ROM
        # exactly like real NOR flash overlays the cartridge.
        self.flash: FlashController = FlashController()
        # TMP95C061 A/D converter = the battery gauge. Session-owned so a
        # conversion in flight survives across step batches.
        self.adc: AdcController = AdcController()
        # The four 8-bit timers. Session-owned: the up-counters must survive
        # across step batches.
        self.timers: TimerController = TimerController()
        # UI 0.4 / 1.0 — debugger-state attached to the session:
        # - `symbol_table` : optional t900ld .map symbols for annotation
        # - `_breakpoints` : live breakpoint rows (same model as the
        #   per-ROM registry) ; the run loop stops as soon as the CPU
        #   lands on any registered address
        self.symbol_table: SymbolTable | None = None
        self._breakpoints: list[Breakpoint] = []
        self._next_breakpoint_id: int = 1
        # UI 0.6 — watchpoints scanned after each step batch ; on
        # hit, `last_stop_reason` flips to "watchpoint-hit" and the
        # Run loop pauses. `last_watch_hit` carries the details (
        # `(watchpoint, access_kind, address, data_bytes)`) for the
        # UI to surface in the status bar.
        self._watchpoints: list[Watchpoint] = []
        self._next_watchpoint_id: int = 1
        self.last_watch_hit: tuple[Watchpoint, str, int, bytes] | None = None
        # Sub-scanline cycle residue. The session accumulates cycles
        # across small batches and converts to scanlines once the
        # residue reaches CYCLES_PER_SCANLINE — otherwise, single-
        # instruction steps (for example a 2-cycle NOP) would round
        # down to zero under integer division and the frame would
        # never advance.
        # Not persisted in savestates: this is intra-session state.
        self._cycle_residue: int = 0
        # Run telemetry — last batch's result, for UI status display.
        self.total_cycles_consumed: int = 0
        self.last_stop_reason: str | None = None
        self.last_executed_count: int = 0
        self.last_irq_deliveries: int = 0
        # Count of VBlank auto-wakes folded into the last `step` call —
        # keeps the halt->wake behavior observable for the UI/tests.
        self.last_auto_wakes: int = 0
        self._bios_path: Path | None = None
        self._bios_bytes: bytes | None = None
        if bios_path is not None:
            self.set_bios_path(Path(bios_path))
        if apply_bios_handoff:
            self._seed_user_vector_table()

    @property
    def bios_path(self) -> Path | None:
        """Return the currently attached external BIOS image path, if any."""
        return self._bios_path

    def set_bios_path(self, path: Path | None) -> None:
        """Attach or clear a 64 KB external BIOS image for live reads.

        The bytes are cached once in-memory so repeated UI single-step
        calls don't re-read the same BIOS file every batch.
        """
        self._invalidate_fetch_view()
        if path is None:
            self._bios_path = None
            self._bios_bytes = None
            return
        bios_path = Path(path)
        bios_bytes = bios_path.read_bytes()
        if len(bios_bytes) != 0x10000:
            raise ValueError(
                f"BIOS image must be exactly 65536 bytes; got {len(bios_bytes)} from {bios_path}"
            )
        self._bios_path = bios_path
        self._bios_bytes = bios_bytes

    def clear_bios_path(self) -> None:
        """Detach the currently attached BIOS image."""
        self.set_bios_path(None)

    # The only parts of the fetch view that depend on `frame_state` are two
    # built-in bytes: RAS.V (the current scanline) and the 2D-status BLNK bit.
    # Everything else -- the ROM image, the address space, the ~50 000-entry
    # cold-start byte map -- is invariant for the life of the session.
    _FRAME_DEPENDENT_RAS_V = 0x008009
    _FRAME_DEPENDENT_2D_STATUS = 0x008010

    def _build_fetch_view(self) -> NgpcFetchView:
        """Return the live fetch view, refreshed for the current frame state.

        PERFORMANCE (2026-07-10): this used to call `load_fetch_view` on every
        call, which re-read the ROM from disk and rebuilt the whole cold-start
        byte map -- and `_step_single_batch` calls it every 50 instructions. That
        alone capped the emulator at ~1.1k instructions/second.

        The view is now built once and cached; each call only refreshes the two
        bytes that actually track the frame. This is behaviour-neutral by
        construction: `load_fetch_view(frame_state=...)` feeds `frame_state` to
        exactly these two entries and nothing else.
        """
        view = self._cached_fetch_view
        if view is None:
            view = load_fetch_view(self.rom_path, frame_state=self.frame_state)
            if self._bios_bytes is not None:
                view = replace(view, bus=replace(view.bus, bios_bytes=self._bios_bytes))
            self._cached_fetch_view = view

        builtin = view.bus.builtin_bytes
        builtin[self._FRAME_DEPENDENT_RAS_V] = self.frame_state.scanline & 0xFF
        builtin[self._FRAME_DEPENDENT_2D_STATUS] = (
            0x40 if self.frame_state.in_vblank else 0x00
        )
        return view

    def _invalidate_fetch_view(self) -> None:
        """Drop the cached view (the ROM image or BIOS backing changed)."""
        self._cached_fetch_view = None

    def _seed_bios_handoff_state(self, cpu: NgpcCpuState) -> NgpcCpuState:
        """Apply the BIOS-equivalent session seed = the cart-entry state.

        Returns a new `NgpcCpuState` that matches what the real NGPC BIOS
        posts to the cart entry point. **Reference-confirmed (2026-07-09):**
        a reference reset dump (`--dump-reset`)
        reports the exact cart-entry state as `wa=bc=de=hl=ix=iy=iz=0`,
        `sp=0x6C00`, `sr=0xF800` (iff_level=7, rfp=0, flags clear), which also
        agrees with the real-hardware findings reverse-engineered from the SNK
        BIOS boot (DEVLOG pass 178). Seeding all general registers to 0 (rather
        than leaving them unknown) is therefore the faithful cart-entry posture,
        and it lets carts run from instruction 1 instead of honest-stopping on a
        `requires-known-full-register` read. The banked register file is seeded
        to 0 to match. See the class-level `BIOS_HANDOFF_*` constants and
        `RESET_STATE.md`.
        """
        control = (
            cpu.control_registers
            if cpu.control_registers is not None
            else create_unknown_control_registers()
        )
        zeroed_regs = GeneralRegisters32(
            xwa=0, xbc=0, xde=0, xhl=0, xix=0, xiy=0, xiz=0,
            xsp=self.BIOS_HANDOFF_XSP,
        )
        zeroed_banks = tuple(
            BankedByteRegisters(slots=(0,) * 16) for _ in range(4)
        )
        return replace(
            cpu,
            regs=zeroed_regs,
            register_bank=0,
            register_banks=zeroed_banks,
            flags=self.BIOS_HANDOFF_FLAGS,
            iff_level=self.BIOS_HANDOFF_IFF_LEVEL,
            iff_enabled=(self.BIOS_HANDOFF_IFF_LEVEL < 7),
            rfp=self.BIOS_HANDOFF_RFP,
            control_registers=replace(
                control,
                intnest=self.BIOS_HANDOFF_INTNEST,
            ),
        )

    def reset(self) -> None:
        """Reset to the documented HW bootstrap state.

        When `apply_bios_handoff` was True at construction (the UI
        default), the reset CPU includes the BIOS-equivalent seed
        (XSP / iff_level / rfp / flags) so the cart entry-point can
        execute its first `CALL` / `PUSH` without blocking.

        Debugger state (`symbol_table`, `_breakpoints`,
        `_watchpoints`) intentionally survives — the user wired them
        in and a Reset shouldn't blow them away. Use the explicit
        `clear_breakpoints` / `clear_watchpoints` to drop them.
        """
        bootstrap = self.machine.cpu
        self.cpu = (
            self._seed_bios_handoff_state(bootstrap)
            if self._apply_bios_handoff
            else bootstrap
        )
        self.memory = {}
        # Flash is non-volatile: a soft reset clears the pending command latch
        # but keeps saved data, which we restore into the fresh overlay so reads
        # still see it (real cart flash survives a power cycle).
        self.flash.clear_pending()
        self.memory.update(self.flash.backing)
        self.adc.reset()
        self.timers.reset()
        self.raster.reset()
        self.irq_state = initial_irq_state()
        self._cycle_residue = 0
        self.total_cycles_consumed = 0
        self.last_stop_reason = None
        self.last_executed_count = 0
        self.last_irq_deliveries = 0
        self.last_auto_wakes = 0
        self.last_watch_hit = None
        if self._apply_bios_handoff:
            self._seed_user_vector_table()

    # The 18-slot USER interrupt vector table lives in RAM at 0x6FB8 (SysPro.txt);
    # the BIOS chains every interrupt through it. Its power-on code fills all 18
    # slots with a default stub BEFORE it starts the cartridge:
    #
    #     FF239D  ld   XIY, 0x00FF23DF     <- the default handler ...
    #     FF23A2  ld   XIX, 0x00006FB8     <- ... the table ...
    #     FF23A7  ld   BC, 0x0012          <- ... 18 entries ...
    #     FF23AA  ld   (XIX+), XIY
    #     FF23AD  djnz BC, 0xFF23AA
    #     FF23DF  reti                     <- and the stub is a bare RETI.
    #
    # THIS IS WHY GAMES SURVIVE AN INTERRUPT THEY NEVER HOOKED. Fatal Fury turns
    # the H-blank interrupt on at boot (INTT0, level 3, via the BIOS INTLVSET
    # call) and only arms the micro-DMA on the screens that scroll a raster; on
    # every other screen the H-int fires 152 times a frame and lands on this
    # RETI. We hand off straight to the cartridge and never run the BIOS's
    # power-on code, so the table stayed all-zero, the CPU vectored to address 0,
    # hit the `swi 7` sitting there, and the BIOS error handler powered the
    # console off. That was the "halting" ROMs (DEVLOG pass 208).
    #
    # The stub is READ OUT OF THE BIOS IMAGE, never memorised: find the fill
    # routine by its `ld XIX, 0x00006FB8` anchor and take the `ld XIY, imm32` in
    # front of it. No BIOS, or no such routine -> the table is left alone rather
    # than seeded with an invented address to jump to.
    USER_VECTOR_TABLE_BASE = 0x006FB8
    USER_VECTOR_TABLE_SLOTS = 18
    _VECTOR_FILL_ANCHOR = bytes((0x44, 0xB8, 0x6F, 0x00, 0x00))  # ld XIX, 0x00006FB8

    def bios_default_user_handler(self) -> int | None:
        """Address the BIOS's power-on code writes into every user vector slot."""
        bios = self._bios_bytes
        if not bios:
            return None
        start = bios.find(self._VECTOR_FILL_ANCHOR)
        while start != -1:
            if start >= 5 and bios[start - 5] == 0x45:  # ld XIY, imm32
                stub = int.from_bytes(bios[start - 4:start], "little")
                if stub >= 0xFF0000:  # must point INTO the BIOS
                    return stub
            start = bios.find(self._VECTOR_FILL_ANCHOR, start + 1)
        return None

    def _seed_user_vector_table(self) -> int | None:
        """Do what the BIOS power-on code does, since the hand-off skips it."""
        stub = self.bios_default_user_handler()
        if stub is None:
            return None
        for slot in range(self.USER_VECTOR_TABLE_SLOTS):
            address = self.USER_VECTOR_TABLE_BASE + slot * 4
            for byte_index in range(4):
                self.memory[address + byte_index] = (stub >> (8 * byte_index)) & 0xFF
        return stub

    # Inner-batch sizes for the breakpoint check loop in `step`.
    # When no breakpoints are set we use the large batch and the
    # `step` body collapses to a single `_step_single_batch(count)`
    # call (no perf overhead). When breakpoints exist we drop to 1
    # so every PC transition can be checked — debugger correctness
    # over throughput. The Run-loop tick at 1000 instr/16ms becomes
    # 1000 inner-batches/tick (~62k inner-batches/sec) which is
    # tolerable under CPython for the typical debug-with-BPs use
    # case ; remove all BPs for full Run-loop speed.
    _BREAKPOINT_CHECK_BATCH_FAST = 50
    _BREAKPOINT_CHECK_BATCH_WITH_BPS = 1

    def step(self, count: int = 1) -> RunStepsResult:
        """Execute up to `count` instructions, then advance timing + IRQs.

        Returns the underlying `RunStepsResult` (the LAST inner-batch's
        result) so callers can inspect per-step records if needed.

        Inner-batches of at most `_BREAKPOINT_CHECK_BATCH` instructions
        are used so the loop can check the breakpoint table after
        every batch. If the CPU lands on a breakpoint, the loop stops
        and reports `last_stop_reason = "breakpoint-hit"`.
        Single-instruction calls (`count=1`) effectively never break
        on entry — they always advance one instruction first, then
        check ; this is the standard debugger "step over a BP"
        semantic.
        """
        if count < 1:
            raise ValueError(f"step count must be >= 1; got {count}")
        # Pick the inner-batch size : both breakpoints and
        # watchpoints need per-batch checks. Either armed → use the
        # tight batch (1 instr) so we don't overshoot. Neither armed →
        # one big batch (zero overhead vs the pre-UI-0.4 behavior).
        if self._breakpoints or self._watchpoints:
            batch_size = self._BREAKPOINT_CHECK_BATCH_WITH_BPS
        else:
            batch_size = self._BREAKPOINT_CHECK_BATCH_FAST
        # Clear "last watch hit" at the start of every public step
        # call — only surface a hit that fired during THIS step.
        self.last_watch_hit = None
        self.last_auto_wakes = 0
        last_result: RunStepsResult | None = None
        executed_total = 0
        irq_deliveries_total = 0
        auto_wakes_total = 0
        just_auto_woke = False
        remaining = count
        while remaining > 0:
            inner_count = min(batch_size, remaining)
            result = self._step_single_batch(inner_count)
            last_result = result
            executed_total += result.executed_count
            irq_deliveries_total += result.irq_deliveries
            remaining -= inner_count
            just_auto_woke = False
            # Inner batch was blocked — propagate and stop.
            if result.stop_reason != "count-reached":
                # HALT is not a dead stop on real hardware: the video
                # clock keeps running until VBlank wakes the CPU into
                # its handler. When enabled, fold that wake and keep
                # going so the boot self-drives across frames. A wake
                # that stays masked (iff >= 6) is a genuinely stuck
                # halt and still stops honestly.
                if (
                    result.stop_reason == "stopped-on-cpu-halted"
                    and self.auto_wake_on_halt
                    and self.wake_halt_via_vblank()
                ):
                    irq_deliveries_total += 1
                    auto_wakes_total += 1
                    self.last_auto_wakes = auto_wakes_total
                    just_auto_woke = True
                    continue
                self.last_stop_reason = result.stop_reason
                self.last_executed_count = executed_total
                self.last_irq_deliveries = irq_deliveries_total
                return result
            # Watchpoint check : did any memory access in this batch
            # match a watchpoint ? (Scanned before BP so the user
            # sees the data-side event first.)
            if self._watchpoints:
                hit = self._scan_watchpoint_hit(result)
                if hit is not None:
                    self.last_watch_hit = hit
                    self.last_stop_reason = "watchpoint-hit"
                    self.last_executed_count = executed_total
                    self.last_irq_deliveries = irq_deliveries_total
                    from dataclasses import replace as _replace
                    return _replace(result, stop_reason="watchpoint-hit")
            # Breakpoint check : did we land on one ?
            if self._breakpoints and self.has_breakpoint(self.cpu.pc):
                self.last_stop_reason = "breakpoint-hit"
                self.last_executed_count = executed_total
                self.last_irq_deliveries = irq_deliveries_total
                from dataclasses import replace as _replace
                return _replace(result, stop_reason="breakpoint-hit")
        # All inner batches completed cleanly. Use the totals across
        # batches for the session-level telemetry.
        assert last_result is not None
        self.last_executed_count = executed_total
        self.last_irq_deliveries = irq_deliveries_total
        self.last_auto_wakes = auto_wakes_total
        # If the instruction budget ran out on the very step that
        # auto-woke, `last_result` is the halted batch — but the CPU is
        # now running inside the woken handler, so report the honest
        # "ran out of budget" reason instead of the stale halt.
        if just_auto_woke and last_result.stop_reason == "stopped-on-cpu-halted":
            self.last_stop_reason = "count-reached"
        else:
            self.last_stop_reason = last_result.stop_reason
        return last_result

    def _scan_watchpoint_hit(
        self, result: RunStepsResult,
    ) -> tuple[Watchpoint, str, int, bytes] | None:
        """Return the first `(watchpoint, kind, address, data)` hit in
        `result.records`'s memory accesses, or None.

        Iteration is one record at a time, writes-then-reads, so the
        earliest temporal hit wins. `kind` is `"write"` or `"read"`
        (the access kind, NOT the watchpoint's `kind`).
        """
        for record in result.records:
            execution = record.execution
            for write in execution.memory_writes:
                for wp in self._watchpoints:
                    if wp.kind not in ("write", "access"):
                        continue
                    if not wp.overlaps_range(write.address, len(write.data)):
                        continue
                    if wp.value is not None:
                        if not write.data or (write.data[0] & 0xFF) != (wp.value & 0xFF):
                            continue
                    return (wp, "write", write.address, write.data)
            for read in execution.memory_reads:
                for wp in self._watchpoints:
                    if wp.kind not in ("read", "access"):
                        continue
                    if not wp.overlaps_range(read.address, len(read.data)):
                        continue
                    if wp.value is not None:
                        if not read.data or (read.data[0] & 0xFF) != (wp.value & 0xFF):
                            continue
                    return (wp, "read", read.address, read.data)
        return None

    # NB (pass 167, 2026-07-06): mul/div r+r (D8..DF sub-op 0x40..0x5F) is
    # HW-CLEARED (hw_test_muldiv: `div WA,BC`=D9 50 executes, XWA 0x3E8/BC 0x0A ->
    # 0x00000064). quirks_db safe_second_ranges now include [64,95]; executor
    # `_try_execute_prefixed_register_muldiv` reuses `_execute_word_memory_muldiv
    # _common`. Only the shift-by-A pocket 0xF8..0xFF and the 0xB8..0xBF gap stay
    # silicon-broken. This clears menu_test_project's div frontier.
    #
    # NB (pass 166, 2026-07-06): the fast inner batch is 50 instructions, so
    # frame-timing (0x8009 scanline / 0x8010 BLNK) already refreshes every ~50
    # instructions across a step() — there is NO per-batch freeze blocking the
    # BIOS vsync-wait. With the correct boot seed (pc=0xFF204A, apply_bios_handoff
    # False, BIOS attached, zeroed regs) the boot runs a stable multi-frame loop
    # (55+ frames) that reaches the VRAM-clear routine 0xFF25EB (fills SCR1/SCR2
    # tilemaps with blank tile 0x0020). It does NOT reach the drawing state
    # machine: it gates in a checksum + peripheral phase (0xFF3318 sums
    # 0x6C25..0x6C2B/0x6F87/0x6F94; 0xFF3350.. pokes I/O 0x20/0x25/0x28 and
    # writes 0x9F/0xBF/0xDF to (XIX=I/O 0xA0/0xA1) with NOP timing = EEPROM/RTC/
    # ADC access). Those peripherals are unmodeled -> the check loops forever.
    def _step_single_batch(self, count: int) -> RunStepsResult:
        """One indivisible step batch — no BP check inside."""
        # Rebuild the fetch view with the live frame_state so the CPU
        # reads of RAS.V (0x8009) and BLNK (0x8010) reflect where we
        # currently are in the frame.
        view = self._build_fetch_view()
        result = build_run_steps(
            view=view,
            count=count,
            cpu_state=self.cpu,
            memory_bytes=self.memory,
            irq_state=self.irq_state,
            flash=self.flash,
            adc=self.adc,
            timers=self.timers,
            raster=self.raster,
        )

        # The raster is ticked PER INSTRUCTION inside `build_run_steps` now, beside
        # the A/D and the timers -- it used to be folded in HERE, after the whole
        # batch, and VBlank therefore became pending one or more instructions late.
        # A single `ldir` can span 38 scanlines (Fatal Fury copies 2798 bytes into
        # the Z80's RAM in one instruction), so "late" meant taking the WRONG
        # interrupt. See RasterController. This just republishes what it computed.
        self._cycle_residue = self.raster.cycle_residue

        self.cpu = result.final_cpu
        self.memory = dict(result.final_memory)
        self.irq_state = (
            result.final_irq_state
            if result.final_irq_state is not None
            else self.irq_state
        )
        self.total_cycles_consumed += result.total_cycles_consumed
        return result

    # ----- Symbols (UI 0.4) -----

    def load_symbol_map(self, path: Path) -> int:
        """Load a t900ld .map file into the session's `symbol_table`.

        Returns the number of symbols loaded. Raises FileNotFoundError
        if the path doesn't exist.
        """
        self.symbol_table = load_map(str(Path(path)))
        return len(self.symbol_table)

    def resolve_symbol(self, address: int) -> str | None:
        """Return `"name+offset"` for the nearest symbol ≤ `address`.

        Returns `None` when no symbol table is loaded or `address`
        precedes the lowest symbol. Exact-address matches drop the
        `+offset` suffix.
        """
        if self.symbol_table is None:
            return None
        sym = self.symbol_table.lookup_address(address)
        if sym is None:
            return None
        offset = address - sym.address
        if offset == 0:
            return sym.name
        return f"{sym.name}+0x{offset:X}"

    # ----- Breakpoints (UI 0.4) -----

    def add_breakpoint(self, address: int, label: str = "") -> Breakpoint:
        """Append one live breakpoint row and return it.

        Duplicate addresses are allowed, matching the ROM-local JSON
        registry contract. The run loop still stops once per landed PC
        regardless of how many rows share that address.
        """
        bp = Breakpoint(
            id=self._next_breakpoint_id,
            address=address & 0xFFFFFF,
            label=label or None,
        )
        self._next_breakpoint_id += 1
        self._breakpoints.append(bp)
        return bp

    def remove_breakpoint(self, address: int) -> bool:
        """Remove the first breakpoint row at `address`.

        Kept for backward compatibility with the pre-registry UI/tests
        which addressed breakpoints by PC instead of by row id.
        """
        address &= 0xFFFFFF
        for index, bp in enumerate(self._breakpoints):
            if bp.address == address:
                del self._breakpoints[index]
                return True
        return False

    def remove_breakpoint_id(self, breakpoint_id: int) -> bool:
        """Remove one breakpoint row by id. Returns True if removed."""
        for index, bp in enumerate(self._breakpoints):
            if bp.id == breakpoint_id:
                del self._breakpoints[index]
                return True
        return False

    def clear_breakpoints(self) -> None:
        """Remove every breakpoint."""
        self._breakpoints.clear()
        self._next_breakpoint_id = 1

    def list_breakpoints(self) -> tuple[Breakpoint, ...]:
        """Return the live breakpoint rows sorted by address then id."""
        return tuple(sorted(self._breakpoints, key=lambda bp: (bp.address, bp.id)))

    def has_breakpoint(self, address: int) -> bool:
        """True if `address` is a breakpoint."""
        address &= 0xFFFFFF
        return any(bp.address == address for bp in self._breakpoints)

    def load_breakpoint_registry(self) -> tuple[Path, int]:
        """Replace live breakpoints from the ROM-local JSON registry."""
        rows = load_breakpoints(self.rom_path)
        self._breakpoints = list(rows)
        self._next_breakpoint_id = max((bp.id for bp in rows), default=0) + 1
        return breakpoints_path_for_rom(self.rom_path), len(rows)

    def save_breakpoint_registry(self) -> Path:
        """Persist live breakpoints to the ROM-local JSON registry."""
        return save_breakpoints(self.rom_path, tuple(self.list_breakpoints()))

    # ----- Watchpoints (UI 0.6) -----

    def add_watchpoint(
        self, start: int, kind: str = "write", *,
        size: int = 1, value: int | None = None,
        label: str | None = None,
    ) -> Watchpoint:
        """Register a watchpoint. Returns the created `Watchpoint`.

        `kind` must be one of `"write"`, `"read"`, `"access"`.
        `size` defaults to 1 byte ; raise on size < 1. `value` is the
        optional byte-value filter (first byte of the accessed range
        must equal `value` for the hit to fire).
        """
        if kind not in WATCHPOINT_KINDS:
            raise ValueError(
                f"watchpoint kind must be one of {WATCHPOINT_KINDS!r}, "
                f"got {kind!r}"
            )
        if size < 1:
            raise ValueError(f"watchpoint size must be >= 1; got {size}")
        wp = Watchpoint(
            id=self._next_watchpoint_id,
            kind=kind,
            start=start & 0xFFFFFF,
            size=size,
            label=label,
            value=value,
        )
        self._next_watchpoint_id += 1
        self._watchpoints.append(wp)
        return wp

    def remove_watchpoint(self, wp_id: int) -> bool:
        """Remove the watchpoint with the given id. Returns True if removed."""
        for i, wp in enumerate(self._watchpoints):
            if wp.id == wp_id:
                del self._watchpoints[i]
                return True
        return False

    def clear_watchpoints(self) -> None:
        """Remove every watchpoint."""
        self._watchpoints.clear()
        self._next_watchpoint_id = 1
        self.last_watch_hit = None

    def list_watchpoints(self) -> tuple[Watchpoint, ...]:
        """Return the watchpoint list as a tuple (in insertion order)."""
        return tuple(self._watchpoints)

    def load_watchpoint_registry(self) -> tuple[Path, int]:
        """Replace live watchpoints from the ROM-local JSON registry."""
        rows = load_watchpoints(self.rom_path)
        self._watchpoints = list(rows)
        self._next_watchpoint_id = max((wp.id for wp in rows), default=0) + 1
        self.last_watch_hit = None
        return watchpoints_path_for_rom(self.rom_path), len(rows)

    def save_watchpoint_registry(self) -> Path:
        """Persist live watchpoints to the ROM-local JSON registry."""
        return save_watchpoints(self.rom_path, tuple(self._watchpoints))

    def step_until_frame_advance(
        self, *, batch: int = 1000, max_steps: int = 50_000,
    ) -> int:
        """Run in batches until `frame_state.frame_count` increases by one.

        Returns the total `executed_count` across the run. Stops early
        on a non-`count-reached` stop reason (e.g. blocked execution)
        or when `max_steps` is reached (whichever comes first).

        Intended for the "Step Frame" UI button — equivalent to "step
        the emulator until the next frame boundary." From scanline 0
        that's ~51,000 NOPs at the current populated `NOP=2` timing ;
        from mid-frame
        proportionally fewer.
        """
        if batch < 1:
            raise ValueError(f"batch must be >= 1; got {batch}")
        if max_steps < 1:
            raise ValueError(f"max_steps must be >= 1; got {max_steps}")
        starting_frame = self.frame_state.frame_count
        total_executed = 0
        remaining = max_steps
        while remaining > 0:
            this_batch = min(batch, remaining)
            result = self.step(this_batch)
            total_executed += result.executed_count
            remaining -= this_batch
            if self.frame_state.frame_count != starting_frame:
                return total_executed
            if result.stop_reason != "count-reached":
                # Execution blocked — don't loop forever.
                return total_executed
        return total_executed

    def wake_halt_via_vblank(self) -> bool:
        """Model a `HALT` waking on the VBlank interrupt.

        On real hardware `HALT` stops the CPU but the video clock keeps
        running until VBlank fires and delivers the interrupt. The bounded
        step model stops with `cpu-halted` instead. This advances the video
        clock a full frame (guaranteeing a VBlank `enter` transition), folds
        the pending VBlank into `irq_state`, then delivers it through the
        interrupt controller (push PC + SR, jump to the 0x6FCC vector /
        installed handler). Raises `iff_level` and clears the pending bit.

        Returns True if the IRQ was delivered (CPU woken into the handler);
        False if it stayed masked / undeliverable (a genuinely stuck halt,
        e.g. `iff_level >= 6` so even VBlank is blocked).
        """
        from core.execute import try_deliver_pending_irq

        transitions = detect_vblank_transitions(self.frame_state, SCANLINES_PER_FRAME)
        self.frame_state = advance_scanlines(self.frame_state, SCANLINES_PER_FRAME)
        self.irq_state = fold_vblank_irq_pending(self.irq_state, transitions)
        view = self._build_fetch_view()
        result = try_deliver_pending_irq(view, self.cpu, self.memory, self.irq_state)
        if result.delivered:
            self.cpu = result.after_cpu
            self.memory = dict(result.after_memory)
            self.irq_state = result.after_irq_state
            self.total_cycles_consumed += result.cycles_consumed
            return True
        return False

    def render_lcd_ppm(self) -> bytes:
        """Render the current frame to a P6 PPM byte string (160×152)."""
        frame = render_frame(self.memory)
        return frame_to_ppm_bytes(frame)

    def joypad_state(self) -> int:
        """Return the current active-high joypad byte (`0x6F82`)."""
        return self.memory.get(self.JOYPAD_ADDRESS, 0) & 0xFF

    def set_joypad_mask(self, mask: int, *, pressed: bool) -> bool:
        """Apply one or more active-high joypad bits."""
        mask &= 0xFF
        if mask == 0:
            return False
        before = self.joypad_state()
        after = (before | mask) if pressed else (before & ~mask)
        after &= 0xFF
        if after == before:
            return False
        if after == 0:
            self.memory.pop(self.JOYPAD_ADDRESS, None)
        else:
            self.memory[self.JOYPAD_ADDRESS] = after
        return True

    # ----- Inspector helpers (UI 0.3 + UI 0.7) -----

    def build_merged_memory_view(self) -> dict[int, int]:
        """Return the live cold-start + overlay memory view for inspectors."""
        view = self._build_fetch_view()
        memory = dict(view.bus.builtin_bytes)
        if self.memory:
            memory.update(self.memory)
        return memory

    def read_memory_range(
        self, address: int, count: int,
    ) -> list[int | None]:
        """Read `count` consecutive bytes from `address` via overlay + bus.

        Each entry is the byte value (0..255) or `None` when the
        address is unbacked / unmapped. The writable overlay shadows
        the read bus the same way the executor sees memory.
        """
        if count < 0:
            raise ValueError(f"count must be >= 0; got {count}")
        view = self._build_fetch_view()
        out: list[int | None] = []
        for i in range(count):
            addr = (address + i) & 0xFFFFFF
            if addr in self.memory:
                out.append(self.memory[addr])
                continue
            result = view.bus.read_bytes(addr, size=1)
            if result.status == "ok" and result.data:
                out.append(result.data[0])
            else:
                out.append(None)
        return out

    def disassemble_around_pc(
        self, *, count: int = 12,
    ) -> list[tuple[int, DecodeResult]]:
        """Return `count` decoded instructions starting at the current PC.

        Walks forward via `decoded.next_sequential_pc` ; stops early
        if decode fails or hits a control-flow instruction without a
        next-sequential-pc. Each entry is `(pc, DecodeResult)`. The
        first entry's PC == `self.cpu.pc`.
        """
        if count < 1:
            raise ValueError(f"count must be >= 1; got {count}")
        return self.disassemble_from(self.cpu.pc, count=count)

    def disassemble_from(
        self, address: int, *, count: int = 12,
    ) -> list[tuple[int, DecodeResult]]:
        """Return `count` decoded instructions starting at `address`."""
        if count < 1:
            raise ValueError(f"count must be >= 1; got {count}")
        view = self._build_fetch_view()
        instructions: list[tuple[int, DecodeResult]] = []
        pc = address & 0xFFFFFF
        for _ in range(count):
            decoded = decode_instruction_at(view.bus, pc)
            instructions.append((pc, decoded))
            if (
                decoded.status != "decoded"
                or decoded.next_sequential_pc is None
            ):
                break
            pc = decoded.next_sequential_pc
        return instructions

    def read_k2ge_control_registers(self) -> K2geControlRegisters:
        """Decode the live K2GE control-register snapshot."""
        return read_control_registers(self.build_merged_memory_view())

    def read_k2ge_palettes(self) -> dict[str, tuple[K2gePalette, ...]]:
        """Decode the live K2GE palette RAM snapshot."""
        return read_all_palettes(self.build_merged_memory_view())

    def read_k2ge_oam_sprites(
        self, *, visible_only: bool = False,
    ) -> tuple[K2geSprite, ...]:
        """Decode the live K2GE sprite list from OAM + CP.C."""
        sprites = read_oam_sprites(self.build_merged_memory_view())
        if visible_only:
            return tuple(sprite for sprite in sprites if not sprite.is_hidden())
        return sprites

    def read_k2ge_tilemap(
        self, plane: str = "scr1", *, non_empty: bool = False,
    ) -> tuple[K2geTilemapEntry, ...]:
        """Decode one live K2GE tilemap plane."""
        entries = read_tilemap(self.build_merged_memory_view(), plane)
        if non_empty:
            return tuple(entry for entry in entries if not entry.is_empty())
        return entries

    def load_savestate(self, path: Path) -> None:
        """Replace the live state from a savestate file."""
        doc = load_savestate(Path(path), expected_rom_path=self.rom_path)
        self.cpu = doc.cpu
        self.memory = dict(doc.writable_overlay)
        self.frame_state = doc.frame_state
        self.irq_state = doc.irq_state
        self.raster.cycle_residue = 0
        # Counters are reset — the savestate is the new "zero point".
        # Cycle residue is not persisted (it's intra-session state) ;
        # treat the loaded frame_state as a clean scanline boundary.
        self._cycle_residue = 0
        self.total_cycles_consumed = 0
        self.last_stop_reason = "loaded-savestate"
        self.last_executed_count = 0
        self.last_irq_deliveries = 0

    def save_savestate(self, path: Path, *, note: str | None = None) -> None:
        """Persist the live state to disk as a v3 savestate."""
        payload = build_savestate_payload(
            rom_path=self.rom_path,
            rom_header=self.machine.header,
            cpu=self.cpu,
            writable_overlay=self.memory,
            note=note,
            frame_state=self.frame_state,
            irq_state=self.irq_state,
        )
        save_savestate(Path(path), payload)

    def snapshot(self) -> SessionSnapshot:
        """Return a frozen read-only view for UI panels."""
        return SessionSnapshot(
            cpu=self.cpu,
            memory=dict(self.memory),
            frame_state=self.frame_state,
            irq_state=self.irq_state,
            total_cycles_consumed=self.total_cycles_consumed,
            last_stop_reason=self.last_stop_reason,
            last_executed_count=self.last_executed_count,
            last_irq_deliveries=self.last_irq_deliveries,
        )
