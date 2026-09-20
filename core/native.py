"""ctypes binding to the native C++ core (`cpp/`).

Why ctypes and not pybind11: this machine's CPython is MSVC-built while the
only available compiler is MinGW GCC 13.1. pybind11 would drag both the C++ ABI
and the CPython ABI across a compiler boundary. A flat C ABI crosses neither.
Verified 2026-07-11: a MinGW C++17 DLL loads and runs cleanly under this
CPython via ctypes.

Seam granularity: one FFI crossing costs ~292 ns. Driving the core one
instruction at a time (~615k/s at real speed) would cost ~17%; driving it one
BATCH at a time costs nothing. So `run()` takes an instruction count, and
breakpoints live in the core rather than in a Python loop.

This module is a thin, dumb mirror of `cpp/include/ngpc_core.h`. It must hold
no emulation logic of its own — the whole point of the port is that there is
exactly one implementation of the machine.
"""

from __future__ import annotations

import ctypes
import sys
from ctypes import (
    POINTER,
    Structure,
    c_char_p,
    c_int,
    c_int16,
    c_int32,
    c_size_t,
    c_uint8,
    c_uint16,
    c_uint32,
    c_uint64,
    c_void_p,
)
from pathlib import Path

from core import bios_fingerprint

# 16: + ngpc_get_link_state / ngpc_set_link_state.
# 17: + ngpc_set_serial_break.
# 18: + ngpc_run_linked -- both cabled consoles and the relay, inside the core.
# Must track NGPC_ABI_VERSION in cpp/include/ngpc_core.h -- the loader compares
# them and asks for a rebuild.
ABI_VERSION = 18

# How the machine comes up (NGPC_RESET_* in ngpc_core.h). This was a bool, and a third
# case was hiding inside it: "no hand-off" ALSO started at the cart's entry point, so
# the BIOS's own boot code had never run in either mode.
RESET_RAW = 0          # PC = cart entry, nothing seeded (the differential/fuzz mode)
RESET_HANDOFF = 1      # + the state the BIOS boot leaves behind. THE DEFAULT.
RESET_BIOS_BOOT = 2    # the console POWERING ON: the real BIOS runs

# The console's work RAM -- kept alive by a coin cell, which is why the BIOS remembers
# your language and the date, and why pulling the batteries wipes it.
RAM_START = 0x004000
RAM_SIZE = 0x003000    # 12 KiB

# The picture, as the core drew it -- ONE LINE AT A TIME, as the beam passed.
SCREEN_W = 160
SCREEN_H = 152

NREG = 8
REG_NAMES = ("xwa", "xbc", "xde", "xhl", "xix", "xiy", "xiz", "xsp")
MAX_RAW = 8
MAX_ACCESS = 4

# Mirrors ngpc_status_t. The tri-state `requires-known-*` family of the Python
# core is deliberately absent: the native core is concrete-state. What remains
# is hardware truth and coverage gaps that must trap loudly.
STATUS = {
    0: "executed",
    1: "cpu-halted",
    10: "silicon-broken",
    11: "silicon-undefined",
    12: "division-by-zero",
    13: "bios-shutdown",
    14: "system-stack-violation",
    15: "watchdog-reset",
    20: "unknown-opcode",
    21: "truncated",
    22: "unmapped",
    30: "unimplemented",
    40: "breakpoint",
    41: "count-reached",
    # Only ever seen when the host armed set_serial_break(). Named here all the same:
    # this table is what stops a status printing as "unknown-status-42", and a planned
    # rendezvous that reads like an unknown fault is the worst of both.
    42: "serial-event",
}

STATUS_OK = 0
STATUS_HALTED = 1
STATUS_SYSTEM_STACK_VIOLATION = 14
STATUS_WATCHDOG_RESET = 15
STATUS_BREAKPOINT = 40
STATUS_COUNT_REACHED = 41
# The link cable moved and the host armed set_serial_break(). Never seen unless
# it did: this is a rendezvous, not a fault.
STATUS_SERIAL_EVENT = 42

# The shared library's name follows the platform. CMake strips the `lib` prefix
# (PROPERTIES PREFIX "", see cpp/CMakeLists.txt), so it is `ngpc_core.<ext>` on
# every OS -- only the extension changes. Windows stays byte-for-byte what it was.
if sys.platform == "win32":
    _DLL_NAME = "ngpc_core.dll"
elif sys.platform == "darwin":
    _DLL_NAME = "ngpc_core.dylib"
else:
    _DLL_NAME = "ngpc_core.so"
# Frozen (PyInstaller): the DLL is bundled at cpp/build/ under the extraction root
# (sys._MEIPASS). From source it sits at <repo>/cpp/build/ next to this package.
if getattr(sys, "frozen", False):
    _DEFAULT_DLL = Path(getattr(sys, "_MEIPASS", Path(sys.executable).resolve().parent)) / "cpp" / "build" / _DLL_NAME
else:
    _DEFAULT_DLL = Path(__file__).resolve().parent.parent / "cpp" / "build" / _DLL_NAME


class NativeCoreUnavailable(RuntimeError):
    """The native core is not built. Callers fall back to the Python core."""


class CpuState(Structure):
    _fields_ = [
        ("regs", c_uint32 * NREG),
        ("pc", c_uint32),
        ("sr_raw", c_uint16),
        ("flags", c_uint8),
        ("alt_flags", c_uint8),
        ("iff_level", c_uint8),
        ("rfp", c_uint8),
        ("_pad", c_uint8 * 2),
        ("banks", (c_uint32 * NREG) * 4),
        ("cregs", c_uint32 * 64),
    ]


class Access(Structure):
    _fields_ = [
        ("address", c_uint32),
        ("size", c_uint8),
        ("discarded", c_uint8),
        ("_pad", c_uint8 * 2),
        ("data", c_uint8 * 4),
    ]


class Record(Structure):
    _fields_ = [
        ("pc", c_uint32),
        ("next_pc", c_uint32),
        ("raw", c_uint8 * MAX_RAW),
        ("raw_len", c_uint8),
        ("status", c_uint8),
        ("n_writes", c_uint8),
        ("n_reads", c_uint8),
        ("cycles", c_uint16),
        ("quirk_id", c_uint16),
        ("written_regs", c_uint32),
        ("writes", Access * MAX_ACCESS),
        ("reads", Access * MAX_ACCESS),
    ]


class Z80State(Structure):
    _fields_ = [
        ("running", c_uint8),
        ("halted", c_uint8),
        ("trapped", c_uint8),
        ("trap_prefix", c_uint8),
        ("trap_pc", c_uint16),
        ("trap_opcode", c_uint8),
        ("_pad", c_uint8),
        ("pc", c_uint16),
        ("sp", c_uint16),
        ("executed", c_uint64),
        ("port_writes", c_uint64),
    ]


APU_WRITE_PORT = 0   # the Z80 executed `OUT (n), A`
APU_WRITE_MEM = 1    # the Z80 wrote into 0x4000..0x7FFF


class ApuWrite(Structure):
    """One write aimed at the T6W28 -- RECORDED, not merely counted.

    `kind` says which door it came through, because we do not yet know which one
    the real sound drivers use, and guessing is how you build a chip that plays
    plausible noise. `cycle` is what a mixer needs to place the write in time.
    """

    _fields_ = [
        ("cycle", c_uint64),
        ("address", c_uint16),
        ("value", c_uint8),
        ("kind", c_uint8),
    ]


class ApuState(Structure):
    """The T6W28's register state, so core/apu.py can be held against it."""

    _fields_ = [
        ("square_vol_left", c_int32 * 3),
        ("square_vol_right", c_int32 * 3),
        ("square_period", c_int32 * 3),
        ("noise_vol_left", c_int32),
        ("noise_vol_right", c_int32),
        ("noise_shifter", c_int32),
        ("noise_tap", c_int32),
        ("noise_period_select", c_int32),
        ("noise_period_extra", c_int32),
        ("latch_left", c_uint8),
        ("latch_right", c_uint8),
        ("_pad", c_uint8 * 2),
    ]


AUX_STATE_VERSION = 1


class AuxState(Structure):
    """The machine state a SAVESTATE needs that is not in the memory image.

    The sound CPU's registers, the T6W28's, and the timer up-counters that pace
    them. A snapshot of "main CPU + memory" alone loses all three, which is why
    loading a state used to kill the music until the game changed scene and sent
    its driver a fresh command. Layout mirrors `ngpc_aux_state_t` FIELD FOR FIELD --
    see the contract in cpp/include/ngpc_core.h.
    """

    _fields_ = [
        ("version", c_uint32),
        ("size", c_uint32),
        # the sound CPU
        ("z80_a", c_uint8), ("z80_f", c_uint8), ("z80_b", c_uint8), ("z80_c", c_uint8),
        ("z80_d", c_uint8), ("z80_e", c_uint8), ("z80_h", c_uint8), ("z80_l", c_uint8),
        ("z80_a2", c_uint8), ("z80_f2", c_uint8), ("z80_b2", c_uint8), ("z80_c2", c_uint8),
        ("z80_d2", c_uint8), ("z80_e2", c_uint8), ("z80_h2", c_uint8), ("z80_l2", c_uint8),
        ("z80_ix", c_uint16), ("z80_iy", c_uint16),
        ("z80_sp", c_uint16), ("z80_pc", c_uint16),
        ("z80_i", c_uint8), ("z80_r", c_uint8), ("z80_im", c_uint8), ("z80_iff1", c_uint8),
        ("z80_iff2", c_uint8), ("z80_halted", c_uint8),
        ("z80_running", c_uint8), ("z80_nmi_pending", c_uint8),
        ("z80_int_pending", c_uint8), ("z80_trapped", c_uint8),
        ("z80_trap_prefix", c_uint8), ("z80_trap_opcode", c_uint8),
        ("z80_trap_pc", c_uint16),
        ("z80_int_ack", c_uint8), ("_pad0", c_uint8),
        ("z80_cycle_credit", c_int32),
        ("z80_executed", c_uint64),
        # the T6W28's registers
        ("square_vol_left", c_int32 * 3),
        ("square_vol_right", c_int32 * 3),
        ("square_period", c_int32 * 3),
        ("square_phase", c_int32 * 3),
        ("square_counter", c_int32 * 3),
        ("noise_vol_left", c_int32),
        ("noise_vol_right", c_int32),
        ("noise_shifter", c_int32),
        ("noise_tap", c_int32),
        ("noise_period_select", c_int32),
        ("noise_period_extra", c_int32),
        ("noise_counter", c_int32),
        ("latch_left", c_uint8), ("latch_right", c_uint8),
        ("dac_left", c_uint8), ("dac_right", c_uint8),
        ("apu_main_residue", c_uint32),
        ("apu_step_fp", c_uint32),
        ("_pad1", c_uint32),
        ("apu_chip_residue", c_uint64),
        # the timers that pace the sound CPU, and the pending-interrupt mask
        ("timer_count", c_uint32 * 4),
        ("timer_clock", c_uint32 * 4),
        ("to3_half_periods", c_uint32),
        ("ti0_pending_pulses", c_uint32),
        ("irq_pending", c_uint64),
        ("scanline", c_uint32),
        ("frame_count", c_uint32),
        ("cycle_residue", c_uint32),
        # La dette de l'unite de bus : SAUVEE, pas effacee. Voir ngpc_core.h.
        ("biu_debt", c_uint32),
    ]


class SerialState(Structure):
    """A read-only look at the link cable, for the debugger. Mirrors
    `ngpc_serial_state_t`.

    The bytes that DO cross the cable are already visible to whoever relays them
    (core/link.py). What this adds is the stage a byte is stuck at when it does
    NOT: held by the peer's CTS, queued while our own RTS is high, or presented
    at SC0BUF and never read because nothing is draining it. That is the
    difference between "no cable" and "cable fine, the game is not listening".
    """

    _fields_ = [(name, c_uint32) for name in (
        "enabled", "tx_depth", "rx_depth", "tx_busy", "rx_pending",
        "cts_high", "rts_low", "ctse",
        "tx_count", "wire_count", "rx_queued_count", "rx_read_count",
        "irq_tx_count", "irq_rx_count", "cts_hold_ticks", "rts_hold_ticks",
        "sc0buf", "sc0cr", "sc0mod", "br0cr", "port_b1", "port_b2",
    )]

    def as_dict(self) -> dict[str, int]:
        return {name: int(getattr(self, name)) for name, _ in self._fields_}


LINK_STATE_VERSION = 2   # v2: the second stage of each serial channel
LINK_FIFO_MAX = 1024


class LinkState(Structure):
    """The link cable as SAVEABLE state. Mirrors `ngpc_link_state_t` FIELD FOR FIELD.

    ⛔ WHY THIS EXISTS, MEASURED 2026-08-04. A save state was the CPU struct + AuxState
    + the memory image, and the cable is in none of the three: its FIFOs, its shift
    register and its handshake pins live in the core's `Machine` and were reachable only
    through `SerialState`, which is read-only. Restoring a state taken mid-transfer was
    a NO-OP on the channel, and two re-simulations from the "same" restored state
    diverged by one cable byte inside 60 frames. See LINK_NETPLAY_STUDY.md and
    tests/test_link_savestate_roundtrip.py.

    ⚡ Its own block, NOT extra fields in AuxState: that struct is the sound CPU, the
    T6W28 and the timers, and growing it would have shifted the memory image inside
    every NGPCST02 save state a player already has.

    ⚠️ `overflow` is set when a FIFO was deeper than LINK_FIFO_MAX. The snapshot is
    then INEXACT -- clamped, not truncated in silence -- and a caller must not pretend
    otherwise. `SerialState` is still the door for looking; this one is for keeping.
    """

    _fields_ = [
        ("version", c_uint32),
        ("size", c_uint32),
        ("link_enabled", c_uint8), ("tx_busy", c_uint8),
        ("tx_shifting", c_uint8), ("tx_byte", c_uint8),
        ("cts_high", c_uint8), ("rx_pending", c_uint8),
        ("rx_byte", c_uint8), ("overflow", c_uint8),
        ("tx_cycles", c_int32), ("rx_cycles", c_int32),
        # v2 -- SC0BUF and the shift register are SEPARATE stages on this chip, and the
        # transmitter is modelled that way. ⛔ THIS MIRROR IS NOT DECORATION: the core
        # memsets sizeof(ngpc_link_state_t) into whatever buffer it is handed, so a
        # Python struct one field short is an out-of-bounds WRITE, not a wrong read.
        # Any field added on the C side belongs here in the same commit.
        ("tx_buf_full", c_uint8), ("tx_buf_byte", c_uint8),
        ("rx_shift_full", c_uint8), ("rx_shift_byte", c_uint8),
        # ⛔ `cts_seen` EST PRIS DANS LE BOURRAGE v2, ET C'EST VOULU. C'est lui qui fait
        # répondre 0xB1 bit2 « une console est au bout du fil » ; il ne vivait dans aucun
        # bloc, donc chaque cran de rewind et chaque savestate DÉBRANCHAIT le câble aux
        # yeux de la cartouche. La taille et la version du bloc ne bougent pas : un état
        # écrit avant ce changement se charge encore, son octet de bourrage vaut 0 —
        # « personne n'a parlé pour le pair », exactement ce avec quoi il a été pris.
        ("rx_had_pending", c_uint8), ("cts_seen", c_uint8), ("_pad_v2", c_uint8 * 2),
        ("tx_len", c_uint32), ("rx_len", c_uint32),
        ("tx_count", c_uint32), ("wire_count", c_uint32),
        ("rx_queued_count", c_uint32), ("rx_read_count", c_uint32),
        ("irq_tx_count", c_uint32), ("irq_rx_count", c_uint32),
        ("cts_hold_ticks", c_uint32), ("rts_hold_ticks", c_uint32),
        ("tx_fifo", c_uint8 * LINK_FIFO_MAX),
        ("rx_fifo", c_uint8 * LINK_FIFO_MAX),
    ]


class WriteRec(Structure):
    """One logged memory write: who wrote, where, what. Mirrors `ngpc_write_t`."""

    _fields_ = [
        ("pc", c_uint32),
        ("addr", c_uint32),
        ("value", c_uint8),
        ("_pad", c_uint8 * 3),
    ]


EVENT_WRITE = 0
EVENT_IRQ = 1


class HygieneRec(Structure):
    """One instance of a ROM doing something hardware tolerates but that is a bug.
    Mirrors `ngpc_hygiene_t`."""

    _fields_ = [("pc", c_uint32), ("addr", c_uint32)]


# The two hardware-safety findings, as bits (`ngpc_set_hw_guard`, `Violation.kind`).
HW_WATCHDOG = 0x1
HW_SYSTEM_STACK = 0x2
HW_FLASH_BUSY_FETCH = 0x4
HW_KINDS = {HW_WATCHDOG: "watchdog-starved", HW_SYSTEM_STACK: "system-stack",
            HW_FLASH_BUSY_FETCH: "flash-busy-fetch"}


class Violation(Structure):
    """One hardware-safety finding. Mirrors `ngpc_violation_t`.

    `detail` is the XSP value for a stack crossing, and the watchdog period for a
    starved watchdog. Counted, not fatal: see `set_hw_guard` for the gate.
    """

    _fields_ = [
        ("pc", c_uint32),
        ("detail", c_uint32),
        ("cycle", c_uint64),
        ("kind", c_uint32),
        ("_pad", c_uint32),
    ]

    @property
    def kind_name(self) -> str:
        return HW_KINDS.get(self.kind, f"kind-{self.kind}")


class EventRec(Structure):
    """One logged event WITH its raster position. Mirrors `ngpc_event_t`.

    `scanline` and `cycle` are what make this different from the write log: for a
    raster effect the timing IS the behaviour.
    """

    _fields_ = [
        ("pc", c_uint32),
        ("addr", c_uint32),      # address written, or the vector index for an IRQ
        ("scanline", c_uint16),
        ("cycle", c_uint16),     # cycles into that scanline (0..514)
        ("value", c_uint8),
        ("type", c_uint8),       # EVENT_WRITE / EVENT_IRQ
        ("_pad", c_uint8 * 2),
    ]


class Frame(Structure):
    """One call-stack frame. Mirrors `ngpc_frame_t`.

    Index 0 is the OUTERMOST caller; the routine currently executing is the last.
    """

    _fields_ = [
        ("caller_pc", c_uint32),   # the CALL instruction's own address
        ("entry_pc", c_uint32),    # where it went
        ("return_pc", c_uint32),   # where it will come back to
        ("entry_sp", c_uint32),    # SP before the call pushed anything
    ]


class ReadRec(Structure):
    """One logged memory READ: who read, where, what came back. Mirrors `ngpc_read_t`.

    Instruction fetches are not logged -- see `set_read_log`.
    """

    _fields_ = [
        ("pc", c_uint32),
        ("addr", c_uint32),
        ("value", c_uint8),
        ("_pad", c_uint8 * 3),
    ]


class RtcState(Structure):
    """The calendar IC at I/O 0x90-0x97. Mirrors `ngpc_rtc_t`.

    Packed BCD, exactly as the registers read: `year=0x24` IS 2024. `counter` is the
    sub-second cycle accumulator -- invisible to software, carried so that saving and
    restoring the clock loses nothing.
    """

    _fields_ = [
        ("enable", c_uint8),
        ("year", c_uint8),
        ("month", c_uint8),
        ("day", c_uint8),
        ("hour", c_uint8),
        ("minute", c_uint8),
        ("second", c_uint8),
        ("weekday", c_uint8),
        # The alarm rides the same coin cell, so it belongs to the same save.
        ("alarm_enable", c_uint8),
        ("alarm_day", c_uint8),
        ("alarm_hour", c_uint8),
        ("alarm_minute", c_uint8),
        ("counter", c_uint32),
    ]


class Summary(Structure):
    _fields_ = [
        ("executed", c_uint32),
        ("emitted", c_uint32),
        ("total_cycles", c_uint64),
        ("irq_deliveries", c_uint32),
        ("stop_status", c_uint8),
        ("_pad", c_uint8 * 3),
        ("stop_pc", c_uint32),
        ("stop_opcode", c_uint8),
        ("_pad2", c_uint8 * 3),
        ("scanline", c_uint32),
        ("frame_count", c_uint32),
        ("timer_hblank_cycles", c_uint32),
        ("timer_hblank_line", c_uint32),
    ]


def _bind(path: Path) -> ctypes.CDLL:
    if not path.exists():
        raise NativeCoreUnavailable(
            f"{path} not found. Build it:\n"
            f"  cmake -S cpp -B cpp/build -G 'MinGW Makefiles' && cmake --build cpp/build"
        )
    lib = ctypes.CDLL(str(path))

    lib.ngpc_run_frames.argtypes = [c_void_p, c_uint32, c_uint32, POINTER(Summary)]
    lib.ngpc_run_frames.restype = c_int
    lib.ngpc_run_linked.argtypes = [c_void_p, c_void_p, c_uint32, c_uint32,
                                    POINTER(Summary), POINTER(Summary)]
    lib.ngpc_run_linked.restype = c_int
    lib.ngpc_link_relay_count.argtypes = [c_void_p]
    lib.ngpc_link_relay_count.restype = c_uint32
    lib.ngpc_link_pair_max_gap.argtypes = [c_void_p]
    lib.ngpc_link_pair_max_gap.restype = ctypes.c_uint64
    lib.ngpc_get_z80.argtypes = [c_void_p, POINTER(Z80State)]
    lib.ngpc_get_z80.restype = None
    lib.ngpc_get_apu_writes.argtypes = [c_void_p, POINTER(ApuWrite), c_uint32]
    lib.ngpc_get_apu_writes.restype = c_uint32
    lib.ngpc_apu_write_count.argtypes = [c_void_p]
    lib.ngpc_apu_write_count.restype = c_uint64
    lib.ngpc_set_timer_base.argtypes = [c_void_p, c_uint32]
    lib.ngpc_set_timer_base.restype = None
    lib.ngpc_raise_irq.argtypes = [c_void_p, c_uint32]
    lib.ngpc_raise_irq.restype = None
    # --- link cable (serial channel 0) ---
    lib.ngpc_serial_set_enabled.argtypes = [c_void_p, c_int]
    lib.ngpc_serial_set_enabled.restype = None
    # Guarded, like set_ldirw_cost: a DLL built before ABI 17 is a real situation
    # (dist/, release/, a stale cpp/build), and binding it unconditionally would
    # take the WHOLE core down over one optional feature. set_serial_break() says
    # so out loud instead of silently doing nothing.
    if hasattr(lib, "ngpc_set_serial_break"):
        lib.ngpc_set_serial_break.argtypes = [c_void_p, c_int]
        lib.ngpc_set_serial_break.restype = None
    lib.ngpc_serial_read_tx.argtypes = [c_void_p, POINTER(c_uint8), c_uint32]
    lib.ngpc_serial_read_tx.restype = c_uint32
    lib.ngpc_serial_write_rx.argtypes = [c_void_p, POINTER(c_uint8), c_uint32]
    lib.ngpc_serial_write_rx.restype = None
    lib.ngpc_serial_rts.argtypes = [c_void_p]
    lib.ngpc_serial_rts.restype = c_int
    lib.ngpc_serial_set_cts.argtypes = [c_void_p, c_int]
    lib.ngpc_serial_set_cts.restype = None
    lib.ngpc_serial_state.argtypes = [c_void_p, POINTER(SerialState)]
    lib.ngpc_serial_state.restype = None
    lib.ngpc_get_apu_state.argtypes = [c_void_p, POINTER(ApuState)]
    lib.ngpc_get_apu_state.restype = None
    lib.ngpc_get_aux_state.argtypes = [c_void_p, POINTER(AuxState)]
    lib.ngpc_get_aux_state.restype = None
    lib.ngpc_set_aux_state.argtypes = [c_void_p, POINTER(AuxState)]
    lib.ngpc_set_aux_state.restype = c_int
    lib.ngpc_get_link_state.argtypes = [c_void_p, POINTER(LinkState)]
    lib.ngpc_get_link_state.restype = None
    lib.ngpc_set_link_state.argtypes = [c_void_p, POINTER(LinkState)]
    lib.ngpc_set_link_state.restype = c_int
    lib.ngpc_set_apu_channel_mask.argtypes = [c_void_p, c_uint32]
    lib.ngpc_set_apu_channel_mask.restype = None
    lib.ngpc_set_layer_mask.argtypes = [c_void_p, c_uint32]
    lib.ngpc_set_layer_mask.restype = None
    lib.ngpc_get_layer_mask.argtypes = [c_void_p]
    lib.ngpc_get_layer_mask.restype = c_uint32
    lib.ngpc_get_audio.argtypes = [c_void_p, POINTER(c_int16), c_uint32]
    lib.ngpc_get_audio.restype = c_uint32
    lib.ngpc_audio_dropped.argtypes = [c_void_p]
    lib.ngpc_audio_dropped.restype = c_uint64
    lib.ngpc_get_raster_log.argtypes = [c_void_p, POINTER(c_uint8), c_uint32]
    lib.ngpc_get_raster_log.restype = c_int
    lib.ngpc_set_write_log.argtypes = [c_void_p, c_uint32, c_uint32]
    lib.ngpc_set_write_log.restype = None
    lib.ngpc_write_log_count.argtypes = [c_void_p]
    lib.ngpc_write_log_count.restype = c_uint64
    lib.ngpc_set_read_log.argtypes = [c_void_p, c_uint32, c_uint32]
    lib.ngpc_set_read_log.restype = None
    lib.ngpc_read_log_count.argtypes = [c_void_p]
    lib.ngpc_read_log_count.restype = c_uint64
    lib.ngpc_get_framebuffer.argtypes = [c_void_p, POINTER(c_uint16), c_uint32]
    lib.ngpc_get_framebuffer.restype = c_uint32
    lib.ngpc_set_battery_ram.argtypes = [c_void_p, POINTER(c_uint8), c_uint32]
    lib.ngpc_set_battery_ram.restype = None
    lib.ngpc_get_rtc.argtypes = [c_void_p, POINTER(RtcState)]
    lib.ngpc_get_rtc.restype = None
    lib.ngpc_set_rtc.argtypes = [c_void_p, POINTER(RtcState)]
    lib.ngpc_set_rtc.restype = None
    lib.ngpc_rtc_advance.argtypes = [c_void_p, c_uint32]
    lib.ngpc_rtc_advance.restype = None
    lib.ngpc_set_timing_silicon.argtypes = [c_void_p, c_uint32, c_uint32]
    lib.ngpc_set_timing_silicon.restype = None
    lib.ngpc_set_flash_timing.argtypes = [c_void_p, c_uint32, c_uint32, c_uint32]
    lib.ngpc_set_flash_timing.restype = None
    lib.ngpc_set_byte_extra.argtypes = [c_void_p, c_uint32]
    lib.ngpc_set_byte_extra.restype = None
    lib.ngpc_set_uart_unplugged.argtypes = [c_void_p, c_int]
    lib.ngpc_set_uart_unplugged.restype = None
    lib.ngpc_set_micro_dma_states.argtypes = [c_void_p, c_uint32]
    lib.ngpc_set_micro_dma_states.restype = None
    lib.ngpc_set_fetch_wait_q4.argtypes = [c_void_p, c_uint32]
    lib.ngpc_set_fetch_wait_q4.restype = None
    lib.ngpc_set_bios_data_wait.argtypes = [c_void_p, c_uint32]
    lib.ngpc_set_bios_data_wait.restype = None
    lib.ngpc_set_slack_by_region.argtypes = [c_void_p, c_int]
    lib.ngpc_set_slack_by_region.restype = None
    lib.ngpc_set_branch_flush.argtypes = [c_void_p, c_int]
    lib.ngpc_set_branch_flush.restype = None
    if hasattr(lib, "ngpc_set_branch_flush_keep"):
        lib.ngpc_set_branch_flush_keep.argtypes = [c_void_p, c_uint32]
        lib.ngpc_set_branch_flush_keep.restype = None
    if hasattr(lib, "ngpc_set_branch_taken_extra"):
        lib.ngpc_set_branch_taken_extra.argtypes = [c_void_p, c_uint32]
        lib.ngpc_set_branch_taken_extra.restype = None
    if hasattr(lib, "ngpc_set_fetch_wait_byte_q16"):
        lib.ngpc_set_fetch_wait_byte_q16.argtypes = [c_void_p, c_uint32]
        lib.ngpc_set_fetch_wait_byte_q16.restype = None
    if hasattr(lib, "ngpc_set_data_access_cycles"):
        lib.ngpc_set_data_access_cycles.argtypes = [c_void_p, c_uint32]
        lib.ngpc_set_data_access_cycles.restype = None
    if hasattr(lib, "ngpc_set_flush_on_region_change"):
        lib.ngpc_set_flush_on_region_change.argtypes = [c_void_p, c_int]
        lib.ngpc_set_flush_on_region_change.restype = None
    if hasattr(lib, "ngpc_set_irq_transparent_queue"):
        lib.ngpc_set_irq_transparent_queue.argtypes = [c_void_p, c_int]
        lib.ngpc_set_irq_transparent_queue.restype = None
    if hasattr(lib, "ngpc_set_block_pays_vram"):
        lib.ngpc_set_block_pays_vram.argtypes = [c_void_p, c_int]
        lib.ngpc_set_block_pays_vram.restype = None
    if hasattr(lib, "ngpc_set_data_wait_cart_only"):
        lib.ngpc_set_data_wait_cart_only.argtypes = [c_void_p, c_int]
        lib.ngpc_set_data_wait_cart_only.restype = None
    if hasattr(lib, "ngpc_set_irq_queue_keep_q16"):
        lib.ngpc_set_irq_queue_keep_q16.argtypes = [c_void_p, c_int32]
        lib.ngpc_set_irq_queue_keep_q16.restype = None
    if hasattr(lib, "ngpc_dbg_queue"):
        lib.ngpc_dbg_queue.argtypes = [c_void_p, POINTER(c_int32), POINTER(c_uint32),
                                       POINTER(c_uint32), POINTER(c_uint32)]
        lib.ngpc_dbg_queue.restype = None
    if hasattr(lib, "ngpc_dbg_biu"):
        lib.ngpc_dbg_biu.argtypes = [c_void_p, POINTER(c_int32), POINTER(c_uint32), POINTER(c_uint32)]
        lib.ngpc_dbg_biu.restype = None
    if hasattr(lib, "ngpc_dbg_bios_charges"):
        lib.ngpc_dbg_bios_charges.argtypes = [c_void_p]
        lib.ngpc_dbg_bios_charges.restype = c_uint32
    if hasattr(lib, "ngpc_set_irq_flush_keep"):
        lib.ngpc_set_irq_flush_keep.argtypes = [c_void_p, c_uint32]
        lib.ngpc_set_irq_flush_keep.restype = None
    if hasattr(lib, "ngpc_set_biu_slack"):
        lib.ngpc_set_biu_slack.argtypes = [c_void_p, c_int32]
        lib.ngpc_set_biu_slack.restype = None
    if hasattr(lib, "ngpc_set_queue_bytes"):
        lib.ngpc_set_queue_bytes.argtypes = [c_void_p, c_uint32]
        lib.ngpc_set_queue_bytes.restype = None
    if hasattr(lib, "ngpc_set_muldiv_byte"):
        lib.ngpc_set_muldiv_byte.argtypes = [c_void_p, c_uint32, c_uint32]
        lib.ngpc_set_muldiv_byte.restype = None
    if hasattr(lib, "ngpc_set_muldiv_word"):
        lib.ngpc_set_muldiv_word.argtypes = [c_void_p, c_uint32, c_uint32]
        lib.ngpc_set_muldiv_word.restype = None
    if hasattr(lib, "ngpc_set_block_drains_queue"):
        lib.ngpc_set_block_drains_queue.argtypes = [c_void_p, c_int]
        lib.ngpc_set_block_drains_queue.restype = None
    lib.ngpc_set_rx_double.argtypes = [c_void_p, c_int]
    lib.ngpc_set_rx_double.restype = None
    lib.ngpc_set_tx_irq_early.argtypes = [c_void_p, c_int]
    lib.ngpc_set_tx_irq_early.restype = None
    lib.ngpc_set_fetch_pipelined.argtypes = [c_void_p, c_int, c_int]
    lib.ngpc_set_fetch_pipelined.restype = None
    lib.ngpc_set_half_duplex.argtypes = [c_void_p, c_int]
    lib.ngpc_set_half_duplex.restype = None
    lib.ngpc_set_relay_gate.argtypes = [c_void_p, c_int]
    lib.ngpc_set_relay_gate.restype = None
    lib.ngpc_set_rx_single.argtypes = [c_void_p, c_int]
    lib.ngpc_set_rx_single.restype = None
    lib.ngpc_set_fetch_word.argtypes = [c_void_p, c_int]
    lib.ngpc_set_fetch_word.restype = None
    lib.ngpc_set_base_scale.argtypes = [c_void_p, c_uint32]
    lib.ngpc_set_base_scale.restype = None
    lib.ngpc_set_irq_entry.argtypes = [c_void_p, c_uint32]
    lib.ngpc_set_irq_entry.restype = None
    lib.ngpc_set_bios_wait.argtypes = [c_void_p, c_uint32]
    lib.ngpc_set_bios_wait.restype = None
    lib.ngpc_set_cart_wait.argtypes = [c_void_p, c_uint32]
    lib.ngpc_set_cart_wait.restype = None
    lib.ngpc_set_cart_data_wait.argtypes = [c_void_p, c_uint32]
    lib.ngpc_set_cart_data_wait.restype = None
    lib.ngpc_set_k1ge_console.argtypes = [c_void_p, c_int]
    lib.ngpc_set_k1ge_console.restype = None
    lib.ngpc_set_vram_wait.argtypes = [c_void_p, c_uint32]
    lib.ngpc_set_vram_wait.restype = None
    lib.ngpc_set_ldir_cost.argtypes = [c_void_p, c_uint32]
    lib.ngpc_set_ldir_cost.restype = None
    # Guarded, unlike its neighbours: a DLL built before this symbol existed is a real
    # situation (dist/, release/, a stale cpp/build), and binding it unconditionally would
    # take the WHOLE core down over one optional timing knob. set_ldirw_cost() below says
    # so out loud rather than silently doing nothing.
    if hasattr(lib, "ngpc_set_ldirw_cost"):
        lib.ngpc_set_ldirw_cost.argtypes = [c_void_p, c_uint32]
        lib.ngpc_set_ldirw_cost.restype = None
    lib.ngpc_set_flash_size.argtypes = [c_void_p, c_uint32, c_uint32]
    lib.ngpc_set_flash_size.restype = None
    lib.ngpc_flash_capacity.argtypes = [c_void_p, c_uint32]
    lib.ngpc_flash_capacity.restype = c_uint32
    lib.ngpc_set_language.argtypes = [c_void_p, c_uint32]
    lib.ngpc_set_language.restype = None
    lib.ngpc_bus_write.argtypes = [c_void_p, c_uint32, c_uint8]
    lib.ngpc_bus_write.restype = None
    lib.ngpc_flash_dirty.argtypes = [c_void_p]
    lib.ngpc_flash_dirty.restype = c_int
    lib.ngpc_flash_clear_dirty.argtypes = [c_void_p]
    lib.ngpc_flash_clear_dirty.restype = None
    lib.ngpc_flash_restore.argtypes = [c_void_p, c_uint32, POINTER(c_uint8), c_uint32]
    lib.ngpc_flash_restore.restype = c_int
    lib.ngpc_get_write_log.argtypes = [c_void_p, POINTER(WriteRec), c_uint32]
    lib.ngpc_get_write_log.restype = c_uint32
    lib.ngpc_get_read_log.argtypes = [c_void_p, POINTER(ReadRec), c_uint32]
    lib.ngpc_get_read_log.restype = c_uint32
    lib.ngpc_set_coverage.argtypes = [c_void_p, c_int]
    lib.ngpc_set_coverage.restype = None
    lib.ngpc_coverage_hits.argtypes = [c_void_p]
    lib.ngpc_coverage_hits.restype = c_uint32
    lib.ngpc_get_coverage.argtypes = [c_void_p, POINTER(c_uint8), c_uint32]
    lib.ngpc_get_coverage.restype = c_uint32
    lib.ngpc_set_hygiene.argtypes = [c_void_p, c_int]
    lib.ngpc_set_hygiene.restype = None
    lib.ngpc_uninit_reads.argtypes = [c_void_p]
    lib.ngpc_uninit_reads.restype = c_uint64
    lib.ngpc_lost_writes.argtypes = [c_void_p]
    lib.ngpc_lost_writes.restype = c_uint64
    lib.ngpc_get_uninit_reads.argtypes = [c_void_p, POINTER(HygieneRec), c_uint32]
    lib.ngpc_get_uninit_reads.restype = c_uint32
    lib.ngpc_get_lost_writes.argtypes = [c_void_p, POINTER(HygieneRec), c_uint32]
    lib.ngpc_get_lost_writes.restype = c_uint32
    lib.ngpc_set_hw_guard.argtypes = [c_void_p, c_uint32]
    lib.ngpc_set_hw_guard.restype = None
    lib.ngpc_hw_violations.argtypes = [c_void_p, c_uint32]
    lib.ngpc_hw_violations.restype = c_uint64
    lib.ngpc_get_hw_violations.argtypes = [c_void_p, POINTER(Violation), c_uint32]
    lib.ngpc_get_hw_violations.restype = c_uint32
    lib.ngpc_set_event_log.argtypes = [c_void_p, c_uint32, c_uint32]
    lib.ngpc_set_event_log.restype = None
    lib.ngpc_event_log_count.argtypes = [c_void_p]
    lib.ngpc_event_log_count.restype = c_uint64
    lib.ngpc_get_event_log.argtypes = [c_void_p, POINTER(EventRec), c_uint32]
    lib.ngpc_get_event_log.restype = c_uint32
    lib.ngpc_set_callstack.argtypes = [c_void_p, c_int]
    lib.ngpc_set_callstack.restype = None
    lib.ngpc_callstack_depth.argtypes = [c_void_p]
    lib.ngpc_callstack_depth.restype = c_uint32
    lib.ngpc_callstack_overflow.argtypes = [c_void_p]
    lib.ngpc_callstack_overflow.restype = c_uint64
    lib.ngpc_get_callstack.argtypes = [c_void_p, POINTER(Frame), c_uint32]
    lib.ngpc_get_callstack.restype = c_uint32
    lib.ngpc_abi_version.restype = c_uint32
    lib.ngpc_create.restype = c_void_p
    lib.ngpc_destroy.argtypes = [c_void_p]
    lib.ngpc_load_rom.argtypes = [c_void_p, POINTER(c_uint8), c_size_t]
    lib.ngpc_load_rom.restype = c_int
    lib.ngpc_load_bios.argtypes = [c_void_p, POINTER(c_uint8), c_size_t]
    lib.ngpc_load_bios.restype = c_int
    lib.ngpc_reset.argtypes = [c_void_p, c_int]
    lib.ngpc_run.argtypes = [c_void_p, c_uint32, POINTER(Record), c_uint32, POINTER(Summary)]
    lib.ngpc_run.restype = c_int
    lib.ngpc_get_cpu.argtypes = [c_void_p, POINTER(CpuState)]
    lib.ngpc_set_cpu.argtypes = [c_void_p, POINTER(CpuState)]
    lib.ngpc_read_mem.argtypes = [c_void_p, c_uint32, POINTER(c_uint8), c_uint32]
    lib.ngpc_read_mem.restype = c_int
    lib.ngpc_write_mem.argtypes = [c_void_p, c_uint32, POINTER(c_uint8), c_uint32]
    lib.ngpc_write_mem.restype = c_int
    lib.ngpc_set_breakpoints.argtypes = [c_void_p, POINTER(c_uint32), c_uint32]
    lib.ngpc_set_breakpoints.restype = c_int

    abi = lib.ngpc_abi_version()
    if abi != ABI_VERSION:
        raise NativeCoreUnavailable(
            f"ABI mismatch: {path} reports v{abi}, this binding speaks v{ABI_VERSION}. Rebuild."
        )
    return lib


_LIB: ctypes.CDLL | None = None


def library(path: Path | None = None) -> ctypes.CDLL:
    """Load (once) and return the native core DLL."""
    global _LIB
    if _LIB is None or path is not None:
        _LIB = _bind(path or _DEFAULT_DLL)
    return _LIB


def available() -> bool:
    try:
        library()
        return True
    except (NativeCoreUnavailable, OSError):
        return False


def active_timing_model() -> str:
    """Le modele de temps que ce processus armera : `"silicon"` ou `"legacy"`.

    ⚡ UN SEUL ENDROIT LE DECIDE. `set_timing_silicon` lit la variable d'environnement
    pour choisir, et le netplay doit poser exactement la MEME question -- sinon deux PC
    peuvent s'accorder sur tout ce qu'ils s'annoncent et simuler deux machines de vitesses
    differentes, ce qui ne se voit pas au branchement mais tue le match en derive. Le
    fingerprint du coeur ne les separe pas : il hache la DLL, et le commutateur est en
    Python.
    """
    import os
    return "legacy" if os.environ.get(
        "NGPCRAFT_TIMING", "").strip().lower() == "legacy" else "silicon"


def core_fingerprint(path: Path | None = None) -> str:
    """Identify THIS BUILD of the core, for anything that compares two of them.

    Mirror netplay (core/netplay.py) simulates the same two consoles on two PCs and
    relies on them agreeing bit for bit, so a core built with different timing at the
    other end is a desync waiting to happen -- and one that shows up mid-match as
    drift, not as an error. The ABI number does not move for a timing fix, so the
    file's own bytes are what answers "is that the same core as mine".
    """
    import hashlib

    dll = path or _DEFAULT_DLL
    try:
        digest = hashlib.sha1(Path(dll).read_bytes()).hexdigest()[:16]
    except OSError:
        digest = "unknown"
    return f"{ABI_VERSION}:{digest}"


def _buf(data: bytes) -> "ctypes.Array[c_uint8]":
    return (c_uint8 * len(data)).from_buffer_copy(data)


class NativeMachine:
    """Owns one native machine. Not thread-safe (neither is the core)."""

    def __init__(self, rom: bytes, *, bios: bytes | None = None, dll: Path | None = None):
        self._lib = library(dll)
        self._h = self._lib.ngpc_create()
        if not self._h:
            raise NativeCoreUnavailable("ngpc_create() returned NULL")
        buf = _buf(rom)
        if self._lib.ngpc_load_rom(self._h, buf, len(rom)) != 0:
            raise ValueError("native core rejected the ROM (too small for a header?)")
        self._rom = bytes(rom)          # kept for core.bios_fingerprint (see reset)
        if bios is not None:
            bbuf = _buf(bios)
            if self._lib.ngpc_load_bios(self._h, bbuf, len(bios)) != 0:
                raise ValueError("native core rejected the BIOS (must be exactly 65536 bytes)")

    def z80(self) -> Z80State:
        """The sound CPU's state -- above all, WHERE IT TRAPPED."""
        st = Z80State()
        self._lib.ngpc_get_z80(self._h, ctypes.byref(st))
        return st

    APU_LOG_SIZE = 4096

    def apu_write_count(self) -> int:
        """TOTAL writes aimed at the T6W28, ever. The log only keeps the last 4096."""
        return int(self._lib.ngpc_apu_write_count(self._h))

    def set_timing_legacy(self) -> None:
        """The timing this core shipped BEFORE 2026-08-21, for A/B comparison.

        Not a supported configuration -- it is known to be wrong (a received byte cost
        49 us against 96 measured on silicon). It exists so a suspected regression can
        be attributed in seconds instead of argued about: set NGPCRAFT_TIMING=legacy
        and see whether the symptom follows the model.
        """
        self.set_base_scale(1)
        self.set_fetch_word(False)
        self.set_fetch_pipelined(False, 0)
        self.set_rx_single(False)
        self.set_tx_irq_early(False)
        self.set_uart_unplugged(False)
        self.set_byte_extra(0)
        self.set_cart_wait(3)
        self.set_cart_data_wait(0)
        self.set_ldir_cost(14)
        self.set_ldirw_cost(18)
        # ⛔ ET LE BLOC NE VIDE PAS LA FILE : c'est un morceau du modele silicium, donc
        # « legacy » doit le RETIRER, sinon l'A/B compare l'ancien modele plus une piece
        # du nouveau -- et n'attribue plus rien.
        self.set_block_drains_queue(False)
        self.set_bios_wait(0)
        # ⛔ And the experimental knobs too: "legacy" must DEFINE the machine, exactly
        # like set_timing_silicon does. Leaving one armed would give the old model plus
        # a refuted hypothesis, and no measurement would say so.
        self.set_branch_flush(False)
        self.set_slack_by_region(False)
        self.set_bios_data_wait(0)
        self.set_fetch_wait_q4(0)
        self.set_relay_gate(False)
        self.set_half_duplex(False)
        self.set_rx_double(False)
        self.set_irq_entry(0)
        # ⛔ ET LES PIECES DES CAMPAGNES v19-v21, POUR LA MEME RAISON QUE CI-DESSUS.
        # « legacy » doit DEFINIR la machine : laisser une piece du modele courant armee
        # ferait comparer l'ancien modele PLUS un morceau du nouveau, et l'A/B
        # n'attribuerait plus rien. Chacune est guardee : une DLL plus ancienne que la
        # piece n'a pas a tomber pour un bouton optionnel.
        for name, arg in (("set_data_wait_cart_only", False),
                          ("set_irq_transparent_queue", False),
                          ("set_flush_on_region_change", False),
                          ("set_block_pays_vram", False),
                          ("set_queue_bytes", 0),
                          ("set_irq_queue_keep_q16", 0),
                          ("set_vram_wait", 0)):
            fn = getattr(self, name, None)
            if fn is not None:
                try:
                    fn(arg)
                except Exception:
                    pass
        # `block_cart_src_per_byte` n'a pas de setter : il vit dans le modele silicium et
        # `ldirw_cost = 18` ci-dessus porte deja, en legacy, la somme que ce surcout
        # explique (14 + 4). Les deux ne doivent donc PAS etre armes ensemble.

    def set_timing_silicon(self, word_wait: int = 10, bios_wait: int = 8) -> None:
        """Arm the whole silicon timing model in one call.

        ⚠️ `NGPCRAFT_TIMING=legacy` in the environment makes this apply the PREVIOUS
        model instead -- the A/B switch for attributing a suspected regression.

        Prefer this over setting cart_wait / ldir_cost / ... by hand: those are eight
        settings that must agree, and a bench that arms half of them measures a machine
        no player has. `word_wait` and `bios_wait` are the two calibrated numbers;
        everything else is documented or derived (see ngpc_set_timing_silicon in
        core.cpp, and OPEN_ITEMS.md for what still misses).
        """
        # ⚖️ LE DEFAUT EST LE MODELE SILICIUM, ET TROIS MESURES MATERIELLES LE PORTENT.
        #
        # Il avait ete mis par defaut le 23/08 au matin, puis retire le meme jour sur un
        # glitch de Cool Boarders, puis remis le soir quand les trois raisons du retrait
        # sont tombees l'une apres l'autre. Ce qui a change entre-temps n'est pas un
        # arbitrage, ce sont des MESURES :
        #
        #  - **CPU** : ROM `a_irq_calib_v8.ngp` flashee sur console (RASV=198, chiffres
        #    stables). Silicium 261/218/249 lots, ce modele 262/218/251. Le cout d'une
        #    interruption y est de 111 cycles ; ce modele en compte 113, l'ANCIEN 59 --
        #    il sous-facturait de moitie, ce qui deplacait tous les splits rasteurs.
        #  - **Serie** : campagne AUTO du 21/08 (temoins tous a zero, `ECHOED` = somme des
        #    RT a l'unite). Allers-retours +0,3 / +0,2 / −0,2 %, part du temps passee a
        #    recevoir 18,32 % contre 18,20 %.
        #  - **Le glitch qui avait fait reculer** : Cool Boarders, trames ou le split
        #    derape sur 600 -- ancien timing **122**, ce modele **62**. Le modele est
        #    DEUX FOIS meilleur sur le defaut meme qui l'avait fait retirer.
        #
        # ⛔ Les deux « regressions » qui avaient motive le retrait n'en etaient pas :
        # l'une venait d'une seule scene de jeu generalisee a tort, l'autre d'un banc qui
        # pilotait mal la ROM sonde. Voir OPEN_ITEMS.md.
        #
        # `--timing legacy` garde l'ancien modele pour attribuer une regression en
        # quelques secondes au lieu d'en discuter.
        if active_timing_model() != "silicon":
            self.set_timing_legacy()
            return
        self._lib.ngpc_set_timing_silicon(self._h, int(word_wait), int(bios_wait))

    def set_flash_timing(self, program: int, erase_per_8k: int, fail: int) -> None:
        """How long the cartridge flash is BUSY, in machine cycles.

        `program` per byte, `erase_per_8k` per 8 KB of block, `fail` the time a chip
        spends on a program it cannot do (a 1 asked over a 0) before it raises DQ5.

        ALL THREE ZERO restores the synchronous chip the core was until 2026-09-10 --
        the byte committed inside the command's own bus cycle, a status poll that exits
        on its first turn, no busy window at all. That is the A/B switch for attributing
        a corpus change to this model rather than arguing about it.

        ⚠️ The defaults are DOCUMENTED, NOT MEASURED, and our documentation contradicts
        itself about the erase by a factor of a hundred. hw_test_flash_timing
        (04_MY_PROJECTS) measures all three on the cartridge; its answer belongs here.
        """
        self._lib.ngpc_set_flash_timing(self._h, int(program), int(erase_per_8k), int(fail))

    def set_byte_extra(self, pct: int) -> None:
        """EXPERIMENT: add pct% to the derived serial byte time."""
        self._lib.ngpc_set_byte_extra(self._h, int(pct))

    def set_uart_unplugged(self, on: bool) -> None:
        """EXPERIMENT: the UART transmits with no cable attached."""
        self._lib.ngpc_set_uart_unplugged(self._h, 1 if on else 0)

    def set_micro_dma_states(self, eighths: int) -> None:
        """Le cout d'un transfert micro-DMA, en HUITIEMES du nominal de la datasheet.

        8 = le nominal (8 etats par transfert octet/mot, 12 en 4 octets, 5 en mode
        compteur -- TMP95C061). 0 = l'ancien comportement, qui n'en facturait AUCUN :
        un jeu qui enchaine les transferts tournait alors trop vite. Le reglage existe
        en huitiemes pour pouvoir l'etalonner sans toucher au code.
        """
        self._lib.ngpc_set_micro_dma_states(self._h, int(eighths))

    def set_fetch_wait_q4(self, quarters: int) -> None:
        """Per-word fetch wait in QUARTER cycles; 0 = use the integer cart_wait."""
        self._lib.ngpc_set_fetch_wait_q4(self._h, int(quarters))

    def set_bios_data_wait(self, cycles: int) -> None:
        """EXPERIMENT: charge BIOS-region DATA reads, not just fetches."""
        self._lib.ngpc_set_bios_data_wait(self._h, int(cycles))

    def set_slack_by_region(self, on: bool) -> None:
        """EXPERIMENT: the BIU's run-ahead follows the region being fetched."""
        self._lib.ngpc_set_slack_by_region(self._h, 1 if on else 0)

    def set_block_drains_queue(self, on: bool) -> None:
        """A repeating block transfer leaves the instruction queue EMPTY.

        It owns the bus for its whole run, so the BIU cannot prefetch behind it and
        the following instructions pay their own fetch in full. Guarded by `hasattr`
        like `set_ldirw_cost`: an older DLL must not take the whole core down for an
        optional setting.
        """
        if not hasattr(self._lib, "ngpc_set_block_drains_queue"):
            raise RuntimeError(
                "this core has no ngpc_set_block_drains_queue -- rebuild cpp/build"
            )
        self._lib.ngpc_set_block_drains_queue(self._h, 1 if on else 0)

    def set_flush_on_region_change(self, on: bool) -> None:
        """EXPERIMENT : un transfert de controle qui change de REGION jette l'avance."""
        self._lib.ngpc_set_flush_on_region_change(self._h, 1 if on else 0)

    def set_irq_transparent_queue(self, on: bool) -> None:
        """EXPERIMENT : une IRQ est transparente pour l'etat de bus du flot interrompu."""
        self._lib.ngpc_set_irq_transparent_queue(self._h, 1 if on else 0)

    def set_block_pays_vram(self, on: bool) -> None:
        """ESSAI : un transfert bloc paie l'etranglement VRAM comme toute ecriture."""
        self._lib.ngpc_set_block_pays_vram(self._h, 1 if on else 0)

    def set_data_wait_cart_only(self, on: bool) -> None:
        """EXPERIMENT : le cout d'acces de donnee ne se paie que dans du code CARTOUCHE."""
        self._lib.ngpc_set_data_wait_cart_only(self._h, 1 if on else 0)

    def set_irq_queue_keep_q16(self, q16: int) -> None:
        """DIAGNOSTIC : etat de la file au sortir d'une acceptation d'IRQ, en 1/16 d'octet."""
        self._lib.ngpc_set_irq_queue_keep_q16(self._h, int(q16))

    def set_queue_bytes(self, nbytes: int) -> None:
        """Taille de la file d'instructions, en OCTETS (4 sur ce coeur).

        0 = ancien modele (credit d'avance en cycles plafonne par `biu_slack`).
        Voir `queue_bytes` dans machine.hpp : le modele en octets n'a aucun parametre
        libre, les deux plafonds sont des faits de la machine.
        """
        if not hasattr(self._lib, "ngpc_set_queue_bytes"):
            raise RuntimeError("this core has no ngpc_set_queue_bytes -- rebuild cpp/build")
        self._lib.ngpc_set_queue_bytes(self._h, int(nbytes))

    def set_muldiv_word(self, mul_states: int, div_cycles: int) -> None:
        """Couts MOT de `mul` (etats) et `div` (cycles). 0 = constantes du coeur.

        La v14 n'a mesure que la forme OCTET ; la forme mot date encore du fetch a
        10 cy/mot. ROM v17.
        """
        if not hasattr(self._lib, "ngpc_set_muldiv_word"):
            raise RuntimeError("this core has no ngpc_set_muldiv_word -- rebuild cpp/build")
        self._lib.ngpc_set_muldiv_word(self._h, int(mul_states), int(div_cycles))

    def set_muldiv_byte(self, mul_states: int, div_cycles: int) -> None:
        """Couts OCTET de `mul` (etats) et `div` (cycles). 0 = constantes du coeur.

        ⛔ Couples a `biu_slack` : ces deux instructions sont execute-bound, donc leur
        cout apparent depend de l'avance qu'on autorise au bus. Voir machine.hpp.
        """
        if not hasattr(self._lib, "ngpc_set_muldiv_byte"):
            raise RuntimeError("this core has no ngpc_set_muldiv_byte -- rebuild cpp/build")
        self._lib.ngpc_set_muldiv_byte(self._h, int(mul_states), int(div_cycles))

    def dbg_biu(self) -> tuple[int, int, int]:
        """(biu_debt a l'entree, stall paye, access_wait) de la derniere instruction."""
        d, st, aw = c_int32(), c_uint32(), c_uint32()
        self._lib.ngpc_dbg_biu(self._h, ctypes.byref(d), ctypes.byref(st), ctypes.byref(aw))
        return d.value, st.value, aw.value

    def dbg_queue(self) -> tuple[int, int, int, int]:
        """(file a l'entree en 1/16 d'octet, octets lus, calage paye, access_wait)."""
        q = ctypes.c_int32(); b = ctypes.c_uint32()
        st = ctypes.c_uint32(); aw = ctypes.c_uint32()
        self._lib.ngpc_dbg_queue(self._h, ctypes.byref(q), ctypes.byref(b),
                                 ctypes.byref(st), ctypes.byref(aw))
        return int(q.value), int(b.value), int(st.value), int(aw.value)

    def dbg_bios_charges(self) -> int:
        """Nombre de charges `bios_wait` depuis le dernier appel (instrumentation)."""
        return int(self._lib.ngpc_dbg_bios_charges(self._h))

    def set_irq_flush_keep(self, cycles: int) -> None:
        """Credit d'avance qui SURVIT a une interruption, en cycles (0 = tout jete).

        Voir `irq_flush_keep` dans machine.hpp : la v14 a mesure qu'une branche prise
        n'emporte que ~2,4 cy du credit, pas les 16 d'une file pleine. Une IRQ est aussi
        un transfert de controle.
        """
        if not hasattr(self._lib, "ngpc_set_irq_flush_keep"):
            raise RuntimeError("this core has no ngpc_set_irq_flush_keep -- rebuild cpp/build")
        self._lib.ngpc_set_irq_flush_keep(self._h, int(cycles))

    def set_biu_slack(self, cycles: int) -> None:
        """Avance maximale de la file d'instructions, en cycles.

        Mesuree par la ROM v16 page 0 : ~7,5 cy, soit UN MOT -- pas les deux que la
        taille de la file (4 octets) laissait deduire.
        """
        if not hasattr(self._lib, "ngpc_set_biu_slack"):
            raise RuntimeError("this core has no ngpc_set_biu_slack -- rebuild cpp/build")
        self._lib.ngpc_set_biu_slack(self._h, int(cycles))

    def set_data_access_cycles(self, cycles: int) -> None:
        """Cout FIXE d'un acces memoire de donnee, en cycles (0 = gratuit).

        Voir `data_access_cycles` : v2 avait prouve cart-data == RAM, jamais qu'elles
        etaient gratuites. Mesure : ROM v15 pages 1-2 (~4,05 cy par ACCES, pas par octet).
        """
        if not hasattr(self._lib, "ngpc_set_data_access_cycles"):
            raise RuntimeError("this core has no ngpc_set_data_access_cycles -- rebuild cpp/build")
        self._lib.ngpc_set_data_access_cycles(self._h, int(cycles))

    def set_fetch_wait_byte_q16(self, sixteenths: int) -> None:
        """Cout d'un octet fetche, en seiziemes de cycle (0 = ancien chemin par mot).

        Le bus cartouche est 8 bits : le cout d'un fetch suit les OCTETS. Voir
        `fetch_wait_byte_q16` dans machine.hpp.
        """
        if not hasattr(self._lib, "ngpc_set_fetch_wait_byte_q16"):
            raise RuntimeError(
                "this core has no ngpc_set_fetch_wait_byte_q16 -- rebuild cpp/build")
        self._lib.ngpc_set_fetch_wait_byte_q16(self._h, int(sixteenths))

    def set_branch_taken_extra(self, cycles: int) -> None:
        """Cycles ajoutes a chaque branche PRISE, sans condition (0 = desarme).

        Hypothese concurrente de `set_branch_flush_keep` : voir `branch_taken_extra`
        dans machine.hpp. Ne pas armer les deux ensemble.
        """
        if not hasattr(self._lib, "ngpc_set_branch_taken_extra"):
            raise RuntimeError(
                "this core has no ngpc_set_branch_taken_extra -- rebuild cpp/build")
        self._lib.ngpc_set_branch_taken_extra(self._h, int(cycles))

    def set_branch_flush_keep(self, cycles: int) -> None:
        """Credit d'avance qui SURVIT a une branche prise, en cycles (0 = vidage total).

        N'a d'effet que si `set_branch_flush(True)`. Voir `branch_flush_keep` dans
        machine.hpp : le silicium (ROM v13) tombe entre les deux reglages extremes.
        """
        if not hasattr(self._lib, "ngpc_set_branch_flush_keep"):
            raise RuntimeError(
                "this core has no ngpc_set_branch_flush_keep -- rebuild cpp/build")
        self._lib.ngpc_set_branch_flush_keep(self._h, int(cycles))

    def set_branch_flush(self, on: bool) -> None:
        """EXPERIMENT: a taken branch empties the 4-byte instruction queue."""
        self._lib.ngpc_set_branch_flush(self._h, 1 if on else 0)

    def set_rx_double(self, on: bool) -> None:
        """EXPERIMENT: two-stage receiver (shift register + SC0BUF)."""
        self._lib.ngpc_set_rx_double(self._h, 1 if on else 0)

    def set_tx_irq_early(self, on: bool) -> None:
        """EXPERIMENT: raise INTTX0 when SC0BUF frees, not when the wire frees."""
        self._lib.ngpc_set_tx_irq_early(self._h, 1 if on else 0)

    def set_fetch_pipelined(self, on: bool, slack: int = 0) -> None:
        """EXPERIMENT: overlap instruction fetch with execution (the BIU)."""
        self._lib.ngpc_set_fetch_pipelined(self._h, 1 if on else 0, int(slack))

    def set_half_duplex(self, on: bool) -> None:
        """EXPERIMENT: do not present a byte while our transmitter is busy."""
        self._lib.ngpc_set_half_duplex(self._h, 1 if on else 0)

    def set_relay_gate(self, on: bool) -> None:
        """EXPERIMENT: hold a byte while the receiver's RTS is high."""
        self._lib.ngpc_set_relay_gate(self._h, 1 if on else 0)

    def set_rx_single(self, on: bool) -> None:
        """EXPERIMENT: charge a received byte's time ONCE."""
        self._lib.ngpc_set_rx_single(self._h, 1 if on else 0)

    def set_fetch_word(self, on: bool) -> None:
        """EXPERIMENT: bill the fetch wait per 16-bit word, not per byte."""
        self._lib.ngpc_set_fetch_word(self._h, 1 if on else 0)

    def set_base_scale(self, k: int) -> None:
        """EXPERIMENT: multiply each instruction's own cycles."""
        self._lib.ngpc_set_base_scale(self._h, int(k))

    def set_irq_entry(self, cycles: int) -> None:
        """Cycles charged for accepting an interrupt; 0 = built-in default."""
        self._lib.ngpc_set_irq_entry(self._h, int(cycles))

    def set_bios_wait(self, cycles_per_byte: int) -> None:
        """Cycles per byte of instruction fetch out of the on-chip BIOS ROM.

        Zero (free) is what this core has always assumed. See Machine::bios_wait --
        measured evidence says BIOS-heavy code runs 23-30% too fast while plain
        cartridge code is only 7-9% out.
        """
        self._lib.ngpc_set_bios_wait(self._h, int(cycles_per_byte))

    def set_cart_wait(self, cycles_per_byte: int) -> None:
        """Wait-states per byte of instruction FETCH from cartridge flash. Silicon = 3.

        ⚠️ A FRESH MACHINE STARTS AT 0 -- free fetch, the pre-wait-state behaviour, NOT
        hardware. The desktop shell turns the silicon set on for every ROM it loads
        (ngpc_settings.cart_wait_states() is True); code that builds a Machine itself gets
        the free-fetch machine and must ask for hardware timing explicitly:

            m.set_timing_silicon(10, 8)   # ⚠️ PREFER THIS -- it arms all eight
            m.set_cart_data_wait(0)   # cfg.CART_DATA_WAIT  -- cart data reads are free
            m.set_ldir_cost(14)       # cfg.CART_LDIR_COST  -- block copies, BYTE form
            m.set_ldirw_cost(18)      # cfg.CART_LDIRW_COST -- block copies, WORD form

        Without them cart code runs ~2.9-3.4x too fast, self-timed games (Cool Boarders,
        Densha de Go) show 60fps where hardware shows 30 -- and, the subtler one, any
        optimisation whose gain is FEWER INSTRUCTION BYTES measures as exactly zero,
        because instruction fetch is the thing not being billed. Every byte of encoding
        costs 3 ticks on silicon, so code size is speed.

        Calibrated by hw_calibration/cpu_calib_v1.ngc. See Machine::cart_wait.
        """
        self._lib.ngpc_set_cart_wait(self._h, int(cycles_per_byte))

    def set_cart_data_wait(self, cycles_per_byte: int) -> None:
        """Wait-states per byte of a DATA read from cart flash. Silicon = 0 (free).

        cpu_calib_v2 on real hardware read a random cart byte and a RAM byte at the same
        cost (CRND 252 == RRND 252): only instruction fetch is wait-stated. 0 here means
        free, NOT "unset" -- there is no fallback to the fetch cost. An earlier value of 5,
        curve-fit to Cool Boarders' frame rate, was refuted by that ROM; don't restore it
        without a measurement. See Machine::cart_data_wait.
        """
        self._lib.ngpc_set_cart_data_wait(self._h, int(cycles_per_byte))

    def set_k1ge_console(self, on: bool) -> None:
        """Emulate the ORIGINAL mono NGP instead of the NGPC, for a mono cartridge.

        The NGPC reports itself at 0x6F91 and a colour-aware mono game (Samurai
        Shodown) then runs its colourisation code and paints the 12-bit compat
        palette. An original NGP has neither, so the game stays monochrome and the
        BIOS grey ramp stands. Must be set BEFORE reset. See Machine::k1ge_console.
        """
        self._lib.ngpc_set_k1ge_console(self._h, 1 if on else 0)
        # Shadowed so callers can READ it back: the C core has no getter, and a tool
        # that renders VRAM itself (the tilemap viewer) resolves colours down a
        # different path on a mono console. Asking 0x87E2 instead is not the same
        # question -- the mono NGP does not have that register at all.
        self._k1ge_console = bool(on)

    @property
    def k1ge_console(self) -> bool:
        """Whether this machine is emulating the original mono NGP."""
        return getattr(self, "_k1ge_console", False)

    def set_vram_wait(self, cycles_per_byte: int) -> None:
        """Wait-states per byte written to display RAM (0x8000-0xBFFF). Default 0 = off.

        The K2GE throttle is REAL -- cpu_calib_v3 on silicon returned VWR 452 < MEM 471,
        a VRAM write costing more than a RAM write. What is not pinned is the cost per
        byte, so nothing ships a value and this stays off rather than guessing an integer.
        It is not the cause of Cool Boarders' residual either (that game writes VRAM in
        vblank; the answer was LDIR). If you measure the cost, say so in
        hw_calibration/README.md rather than only here. See Machine::vram_wait.
        """
        self._lib.ngpc_set_vram_wait(self._h, int(cycles_per_byte))

    def set_ldir_cost(self, cycles_per_byte: int) -> None:
        """Cycles/byte for LDIR/LDDR block copies. A fresh Machine starts at 7 (datasheet);
        the shell and romcheck ship 14 (cfg.CART_LDIR_COST), which reproduces Cool Boarders'
        silicon 30fps and leaves Fatal Fury at 60. The datasheet figure is likely a floor,
        as MUL/DIV proved to be. Pass 14 if you want the shipping timing.
        See Machine::ldir_cost."""
        self._lib.ngpc_set_ldir_cost(self._h, int(cycles_per_byte))

    def set_ldirw_cost(self, cycles_per_iteration: int) -> None:
        """Cycles per ITERATION for the WORD block copies, LDIRW/LDDRW. 0 = follow
        set_ldir_cost(), which is the pre-existing behaviour.

        The two widths are different instructions and the loop is billed per iteration,
        so one number cannot price both: an LDIRW iteration moves TWO bytes, and charging
        it the byte figure sells a word copy at half price. Cool Boarders pinned the byte
        form at 14 and never constrained this one. The shell ships 18 (cfg.CART_LDIRW_COST),
        measured on Bomberman's open-loop HiColor copier, where a block must cost exactly
        8 scanlines and the tolerance is one cycle. See Machine::ldirw_cost.
        """
        fn = getattr(self._lib, "ngpc_set_ldirw_cost", None)
        if fn is None:
            raise NativeCoreUnavailable(
                "this ngpc_core has no ngpc_set_ldirw_cost -- it predates the "
                "byte/word split of the block-copy cost; rebuild cpp/")
        fn(self._h, int(cycles_per_iteration))

    def set_flash_size(self, size_bytes: int, *, chip: int = 0) -> None:
        """Present the cart as a flash chip of this capacity (rebuilds the erasable-block
        map). Lets an under-filled homebrew ROM save in its chip's top block. See
        ngpc_set_flash_size in core.cpp."""
        self._lib.ngpc_set_flash_size(self._h, int(chip), int(size_bytes))

    def flash_capacity(self, chip: int = 0) -> int:
        """What the chip presents as NOW -- not what was set. The cartridge corrects us
        mid-session by the block number it asks for, so this is the size to persist a
        save at; see ngpc_flash_capacity in core.cpp for what happens if you use the
        guess instead. 0 = no cartridge in that slot."""
        return int(self._lib.ngpc_flash_capacity(self._h, int(chip)))

    LANGUAGE_JAPANESE, LANGUAGE_ENGLISH = 0, 1

    def set_language(self, code: int) -> None:
        """The console's language setting, handed to the cart at 0x6F87 (SDK SysWork:
        0 = Japanese, 1 = English). A bilingual cartridge reads this byte and nothing
        else -- 24 games of the corpus do. Set BEFORE reset."""
        self._lib.ngpc_set_language(self._h, int(code))

    def set_timer_base(self, cycles_per_phi_t1: int) -> None:
        """phi-T1 in CPU cycles. The docs contradict each other; see ngpc_core.h."""
        self._lib.ngpc_set_timer_base(self._h, cycles_per_phi_t1)

    IRQ_INT0 = 8    # the POWER circuit

    def raise_irq(self, vector_index: int) -> None:
        """Assert an interrupt line from outside the CPU (INT0 = the power button)."""
        self._lib.ngpc_raise_irq(self._h, vector_index)

    def apu_state(self) -> ApuState:
        """The chip's registers -- what the Python oracle gets compared against."""
        st = ApuState()
        self._lib.ngpc_get_apu_state(self._h, ctypes.byref(st))
        return st

    def set_apu_channel_mask(self, mask: int) -> None:
        """Debug mute: bit0..2 squares, bit3 noise, bit4 DAC (0x1F = all on)."""
        self._lib.ngpc_set_apu_channel_mask(self._h, int(mask) & 0x1F)

    # Debug layer mask -- the video twin of the channel mute above. Keep these names
    # in step with `core.renderer.LAYER_*`: one concept, and the two cores must not
    # drift apart on what bit means what.
    LAYER_SCR1, LAYER_SCR2 = 0x01, 0x02
    LAYER_SPR_BACK, LAYER_SPR_MID, LAYER_SPR_FRONT = 0x04, 0x08, 0x10
    LAYER_SPRITES = LAYER_SPR_BACK | LAYER_SPR_MID | LAYER_SPR_FRONT
    LAYER_ALL = 0x1F

    def set_layer_mask(self, mask: int) -> None:
        """Debug show/hide: bit0 SCR1, bit1 SCR2, bit2..4 sprites by PR.C.

        Composition only -- no machine state changes, so a mask can be flipped mid-game
        and flipped back with the picture identical. 0x1F (default) = everything on;
        any fidelity or corpus measurement must run there.
        """
        self._lib.ngpc_set_layer_mask(self._h, int(mask) & self.LAYER_ALL)

    def layer_mask(self) -> int:
        return int(self._lib.ngpc_get_layer_mask(self._h))

    AUDIO_RATE_HZ = 44100

    def audio(self, frames: int = 8192) -> bytes:
        """Drain up to `frames` stereo frames: interleaved L,R, signed 16-bit LE.

        The chip produces 44 100 frames a second whatever speed the emulator runs
        at, so a caller replaying at x48 must drain often or lose audio -- and it
        will KNOW it did, because `audio_dropped()` counts every frame the ring
        had to throw away. Silence that nobody notices is the failure mode here.
        """
        buf = (c_int16 * (frames * 2))()
        got = self._lib.ngpc_get_audio(self._h, buf, frames)
        return bytes(memoryview(buf)[: got * 2])

    def audio_dropped(self) -> int:
        """Stereo frames the host was too slow to collect. Should be zero."""
        return int(self._lib.ngpc_audio_dropped(self._h))

    WRITE_LOG_SIZE = 8192

    def set_write_log(self, lo: int, hi: int) -> None:
        """Log every write landing in `[lo, hi]`, with the PC that made it.

        The native core has breakpoints on PC and nothing on memory, so "which
        routine fills this tilemap, and why does it stop" could only be guessed at.
        Pass `lo > hi` to disarm. Arming also resets the count.
        """
        self._lib.ngpc_set_write_log(self._h, lo, hi)

    def write_log_count(self) -> int:
        """Every write the window saw -- INCLUDING any the ring had to drop."""
        return int(self._lib.ngpc_write_log_count(self._h))

    def write_log(self, limit: int = WRITE_LOG_SIZE) -> list[WriteRec]:
        """The most recent logged writes, oldest first."""
        buf = (WriteRec * limit)()
        got = self._lib.ngpc_get_write_log(self._h, buf, limit)
        return list(buf[:got])

    READ_LOG_SIZE = 8192

    def set_read_log(self, lo: int, hi: int) -> None:
        """Log every DATA read landing in `[lo, hi]`, with the PC that made it.

        The write log's missing half: "which routine writes this?" was answerable,
        "which routine READS this?" was not -- and that is the question you ask about
        a flag nobody seems to act on.

        ⚠️ Instruction FETCHES are deliberately excluded. They go through the same
        read path, so logging them would bury the one data read you are after under
        every instruction of the code doing the reading. Pass `lo > hi` to disarm;
        arming also resets the count.
        """
        self._lib.ngpc_set_read_log(self._h, lo, hi)

    def read_log_count(self) -> int:
        """Every logged read the window saw -- INCLUDING any the ring had to drop."""
        return int(self._lib.ngpc_read_log_count(self._h))

    def read_log(self, limit: int = READ_LOG_SIZE) -> list[ReadRec]:
        """The most recent logged reads, oldest first."""
        buf = (ReadRec * limit)()
        got = self._lib.ngpc_get_read_log(self._h, buf, limit)
        return list(buf[:got])

    COVERAGE_LO, COVERAGE_HI = 0x200000, 0x3FFFFF

    def set_coverage(self, enabled: bool) -> None:
        """Record the address of every instruction executed in the cart window.

        Without this, "the analyzer looked at this ROM" cannot be checked. With it,
        "driving the buttons reached more code" is a number rather than a hope.
        Enabling allocates 256 KiB and resets the count.
        """
        self._lib.ngpc_set_coverage(self._h, 1 if enabled else 0)

    def coverage_hits(self) -> int:
        """Distinct instruction addresses executed since coverage was enabled."""
        return int(self._lib.ngpc_coverage_hits(self._h))

    def coverage_bitmap(self) -> bytes:
        size = int(self._lib.ngpc_get_coverage(self._h, None, 0))
        if not size:
            return b""
        buf = (c_uint8 * size)()
        got = self._lib.ngpc_get_coverage(self._h, buf, size)
        return bytes(buf[:got])

    HYGIENE_SAMPLES = 256

    def set_hygiene(self, enabled: bool) -> None:
        """Watch for work-RAM reads that precede any write, and stores into unmapped
        space. Both are things hardware tolerates silently and that are almost always
        bugs. Enabling resets the counters. Off by default."""
        self._lib.ngpc_set_hygiene(self._h, 1 if enabled else 0)

    def uninit_reads(self) -> int:
        return int(self._lib.ngpc_uninit_reads(self._h))

    def lost_writes(self) -> int:
        return int(self._lib.ngpc_lost_writes(self._h))

    def uninit_read_samples(self, limit: int = HYGIENE_SAMPLES) -> list[HygieneRec]:
        buf = (HygieneRec * limit)()
        got = self._lib.ngpc_get_uninit_reads(self._h, buf, limit)
        return list(buf[:got])

    def lost_write_samples(self, limit: int = HYGIENE_SAMPLES) -> list[HygieneRec]:
        buf = (HygieneRec * limit)()
        got = self._lib.ngpc_get_lost_writes(self._h, buf, limit)
        return list(buf[:got])

    HW_SAMPLES = 64

    def set_hw_guard(self, stop_mask: int) -> None:
        """Which hardware-safety findings should STOP a run: `HW_WATCHDOG`,
        `HW_SYSTEM_STACK`, or both OR-ed together.

        Zero -- the default -- is the diagnostic mode: findings are counted and
        sampled while the ROM keeps running, because neither halts a real console
        at the instruction that commits it. Arm this only for a gate that wants a
        verdict ("this build must be clean"), and read `stop_status`.
        """
        self._lib.ngpc_set_hw_guard(self._h, stop_mask)

    def hw_violations(self, kind: int = HW_WATCHDOG | HW_SYSTEM_STACK
                      | HW_FLASH_BUSY_FETCH) -> int:
        """How many findings of the given kind(s) since the last reset."""
        return int(self._lib.ngpc_hw_violations(self._h, kind))

    def hw_violation_samples(self, limit: int = HW_SAMPLES) -> list[Violation]:
        """The earliest findings, oldest first, so a report can name the code."""
        buf = (Violation * limit)()
        got = self._lib.ngpc_get_hw_violations(self._h, buf, limit)
        return list(buf[:got])

    EVENT_LOG_SIZE = 4096
    # The K2GE video registers: scroll, palette, window, raster control. The default
    # window for the event viewer, because this is where every raster trick lands.
    VIDEO_REGS = (0x008000, 0x0083FF)

    def set_event_log(self, lo: int, hi: int) -> None:
        """Log writes in `[lo, hi]` WITH the scanline and cycle they happened on, plus
        every interrupt delivery.

        The write log answers "who wrote this"; this answers "when in the frame", which
        is the only question that matters for a scroll split or an HBlank HUD. Pass
        `lo > hi` to disarm; arming resets the count.
        """
        self._lib.ngpc_set_event_log(self._h, lo, hi)

    def event_log_count(self) -> int:
        return int(self._lib.ngpc_event_log_count(self._h))

    def event_log(self, limit: int = EVENT_LOG_SIZE) -> list[EventRec]:
        """The most recent events, oldest first."""
        buf = (EventRec * limit)()
        got = self._lib.ngpc_get_event_log(self._h, buf, limit)
        return list(buf[:got])

    CALLSTACK_DEPTH = 64

    def set_callstack(self, enabled: bool) -> None:
        """Track the call stack as a shadow stack, per instruction.

        Answers the one question a breakpoint always raises and that no register
        dump can: how did execution get here. Off by default -- it costs a couple
        of compares per instruction plus a 4-byte read per call, which a player has
        no reason to pay. Disabling also clears the stack.
        """
        self._lib.ngpc_set_callstack(self._h, 1 if enabled else 0)

    def callstack_depth(self) -> int:
        return int(self._lib.ngpc_callstack_depth(self._h))

    def callstack_overflow(self) -> int:
        """Frames dropped because the shadow stack was full. Non-zero means the view
        is TRUNCATED (deep recursion), not that it is wrong."""
        return int(self._lib.ngpc_callstack_overflow(self._h))

    def callstack(self, limit: int = CALLSTACK_DEPTH) -> list[Frame]:
        """Frames outermost-first; the routine executing now is the last one."""
        buf = (Frame * limit)()
        got = self._lib.ngpc_get_callstack(self._h, buf, limit)
        return list(buf[:got])

    RASTER_LINES = 152
    RASTER_REGS = 0x40
    RASTER_BASE = 0x8000

    def raster_log(self) -> tuple[bytes, ...]:
        """The K2GE display registers (0x8000..0x803F) per visible scanline.

        152 rows of 64 bytes: row N is what line N was DRAWN with. A game that
        rewrites its scroll registers mid-frame -- Sonic drives its parallax that
        way, via micro-DMA into S2SO.H on every H-blank -- cannot be rendered from
        a single end-of-frame snapshot, which is what the renderer used to take.
        """
        need = self.RASTER_LINES * self.RASTER_REGS
        buf = (c_uint8 * need)()
        got = self._lib.ngpc_get_raster_log(self._h, buf, need)
        if got != need:
            raise RuntimeError(f"raster log: core returned {got}, expected {need}")
        raw = bytes(buf)
        return tuple(
            raw[i * self.RASTER_REGS : (i + 1) * self.RASTER_REGS]
            for i in range(self.RASTER_LINES)
        )

    def apu_writes(self, limit: int = APU_LOG_SIZE) -> list[ApuWrite]:
        """The most recent writes aimed at the T6W28, oldest first."""
        n = min(limit, self.APU_LOG_SIZE)
        buf = (ApuWrite * n)()
        got = self._lib.ngpc_get_apu_writes(self._h, buf, n)
        return list(buf[:got])

    # ------------------------------------------------------------------ saves
    # The cartridge IS the save medium: a NOR flash the game erases and programs in
    # place. Until now this core knew the AMD unlock sequence well enough for the BIOS
    # to identify the cart, and then SWALLOWED every erase and program -- so a save
    # went nowhere, silently, and you only found out by losing one.

    def bus_write(self, address: int, value: int) -> None:
        """One byte ON THE BUS, exactly as the CPU's store does it.

        A cart-window write is DISCARDED as memory and handed to the flash chip's
        command latch instead. This is the same door a real game uses -- reaching
        around it would prove nothing about the path that matters.
        """
        self._lib.ngpc_bus_write(self._h, address, value & 0xFF)

    def flash_dirty(self) -> bool:
        """True once the game has actually changed a byte of its own cartridge."""
        return bool(self._lib.ngpc_flash_dirty(self._h))

    def flash_clear_dirty(self) -> None:
        self._lib.ngpc_flash_clear_dirty(self._h)

    def flash_restore(self, address: int, data: bytes) -> None:
        """Put bytes back into the cart window -- what re-inserting the cart does."""
        buf = _buf(data)
        if self._lib.ngpc_flash_restore(self._h, address, buf, len(data)) != 0:
            raise ValueError(f"flash_restore: {address:#08x}+{len(data)} is not in the cart window")

    def close(self) -> None:
        if getattr(self, "_h", None):
            self._lib.ngpc_destroy(self._h)
            self._h = None

    def __enter__(self) -> "NativeMachine":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    __del__ = close

    def framebuffer(self) -> list[int]:
        """The 160x152 picture the CORE drew, line by line, as the beam passed.

        Raw 12-bit 0BGR -- exactly what the palette holds. This is not an optimisation
        of `core/renderer.py`: it is the same picture drawn at the RIGHT TIME. A game
        that streams VRAM mid-frame (every scrolling game) cannot be composed correctly
        from the end-of-frame state, however fast you do it.
        """
        n = SCREEN_W * SCREEN_H
        buf = (c_uint16 * n)()
        got = self._lib.ngpc_get_framebuffer(self._h, buf, n)
        return list(buf[:got])

    def set_battery_ram(self, data: bytes | None) -> None:
        """The coin cell: the RAM the console had when it was last switched off.

        Hand it over BEFORE `reset`, which consults the marker inside it to tell a
        first-ever boot from a resume. `None` is a dead cell -- a blank RAM, and a BIOS
        that boots as if brand new (and says "SUB BATTERY DEAD").
        """
        if not data:
            self._lib.ngpc_set_battery_ram(self._h, None, 0)
            return
        buf = (c_uint8 * len(data)).from_buffer_copy(data)
        self._lib.ngpc_set_battery_ram(self._h, buf, len(data))

    def battery_ram(self) -> bytes:
        """The console's 12 KiB of work RAM, as it stands now."""
        return self.read(RAM_START, RAM_SIZE)

    def rtc(self) -> RtcState:
        """The calendar IC (I/O 0x90-0x97), in packed BCD.

        It runs off the SAME coin cell as `battery_ram`, so it belongs to the same save.
        It is machine state rather than memory, though, so `read` cannot reach it -- which
        is precisely how it went unsaved and got re-seeded to a fixed date every launch.
        """
        st = RtcState()
        self._lib.ngpc_get_rtc(self._h, ctypes.byref(st))
        return st

    def rtc_advance(self, seconds: int) -> None:
        """Wind the clock forward, for time the console spent switched off.

        Goes through the CORE's own BCD carry chain -- the same one the running clock
        ticks through -- so month ends and leap years are handled by the code that
        already gets them right, instead of by a second implementation up here.
        """
        if seconds > 0:
            self._lib.ngpc_rtc_advance(self._h, int(seconds))

    def set_rtc(self, st: RtcState) -> None:
        """Put the clock back the way it was left. Hand it over BEFORE `reset` in
        real-BIOS mode: the BIOS reads the chip during its own boot, and (measured) it
        REWRITES it to 1998-01-01 only when the coin cell is blank -- on a configured
        console it never writes it at all, so this is what the console will believe."""
        self._lib.ngpc_set_rtc(self._h, ctypes.byref(st))

    def reset(self, *, bios_handoff: bool = True, real_bios: bool = False) -> None:
        """Power the machine up. See NGPC_RESET_* in ngpc_core.h.

        `real_bios=True` is the console POWERING ON -- the BIOS's own boot code runs.
        It needs a BIOS image; without one the vector table reads zero and PC lands on
        address 0.
        """
        if real_bios:
            mode = RESET_BIOS_BOOT
        else:
            mode = RESET_HANDOFF if bios_handoff else RESET_RAW
        self._lib.ngpc_reset(self._h, mode)
        # A game may check that the console booted from the BIOS by fingerprinting
        # char RAM. Hand-off only: that is the path where char RAM arrives pre-filled
        # (the core warms the loaded BIOS up to capture it), so a real bios.bin leaves
        # the fingerprint there and this is a no-op. Under `real_bios` the BIOS is
        # about to run for itself and char RAM is still blank -- writing there would be
        # us pre-empting a boot that produces the data on its own.
        if mode == RESET_HANDOFF:
            bios_fingerprint.restore(self._rom, self.read, self.write)

    def cpu(self) -> CpuState:
        st = CpuState()
        self._lib.ngpc_get_cpu(self._h, ctypes.byref(st))
        return st

    def set_cpu(self, st: CpuState) -> None:
        self._lib.ngpc_set_cpu(self._h, ctypes.byref(st))

    def aux_state(self) -> AuxState:
        """The rest of the machine: sound CPU, sound chip, timers, pending IRQs.

        Everything a savestate needs that `read()` cannot see, because it is chip
        state rather than memory. See AuxState.
        """
        st = AuxState()
        self._lib.ngpc_get_aux_state(self._h, ctypes.byref(st))
        return st

    def set_aux_state(self, st: AuxState) -> bool:
        """Put it back. False if the blob is from another build (never half-applied).

        ⚠️ Apply this AFTER restoring the memory image, never before: writing the
        image goes through the control registers, and 0x00BA is a door ("fire one
        NMI at the sound CPU"), not storage. This call is what cancels that phantom.
        """
        return self._lib.ngpc_set_aux_state(self._h, ctypes.byref(st)) == 0

    def link_state(self) -> LinkState:
        """The link cable, as something a savestate can keep. See LinkState.

        `serial_state()` is the debugger's read-only view of the same channel; this is
        the one that round-trips. A snapshot taken with a FIFO deeper than
        LINK_FIFO_MAX comes back with `overflow` set and is inexact.
        """
        st = LinkState()
        self._lib.ngpc_get_link_state(self._h, ctypes.byref(st))
        return st

    def set_link_state(self, st: LinkState) -> bool:
        """Put the cable back. False if the blob is from another build.

        ⚠️ Like the aux block, AFTER the memory image: SC0MOD/BR0CR live in the image
        and decide how fast a byte shifts, so restoring the channel first would run it
        against the previous timeline's baud setup for one call.
        """
        return self._lib.ngpc_set_link_state(self._h, ctypes.byref(st)) == 0

    def read(self, address: int, count: int) -> bytes:
        out = (c_uint8 * count)()
        self._lib.ngpc_read_mem(self._h, address, out, count)
        return bytes(out)

    def write(self, address: int, data: bytes) -> None:
        self._lib.ngpc_write_mem(self._h, address, _buf(data), len(data))

    def set_breakpoints(self, pcs: list[int]) -> None:
        arr = (c_uint32 * len(pcs))(*pcs)
        self._lib.ngpc_set_breakpoints(self._h, arr, len(pcs))

    # --- link cable (serial channel 0) -------------------------------------
    # The cable is a byte pipe. A host bridges two machines by draining each
    # one's transmitted bytes (serial_read_tx) and feeding them to the other
    # (serial_write_rx). See core/link.py for the in-process / TCP bridges.
    def serial_set_enabled(self, on: bool) -> None:
        self._lib.ngpc_serial_set_enabled(self._h, 1 if on else 0)

    def set_serial_break(self, on: bool) -> None:
        """Stop `run()` the moment the cable moves, instead of polling for it.

        A host bridging two machines has to relay bytes and had no way to know
        when, so it pumped every N instructions -- N chosen for the worst known
        game (400: The Last Blade breaks past it). That is cable time measured in
        instructions, and the core already counts the real unit: `serial_tick`
        knows the exact cycle a byte finishes shifting, from BR0CR/SC0MOD.

        Armed, `run()` returns with `stop_status == STATUS_SERIAL_EVENT` as soon
        as a byte reaches the transmit FIFO or this machine's RTS changes (it
        drives the peer's CTS, and Card Fighters' Clash's handshake stalls on it).
        The instruction in flight is completed first, so the machine is always
        left on an instruction boundary and the run simply resumes.

        Off by default: every existing caller keeps its exact behaviour.
        """
        fn = getattr(self._lib, "ngpc_set_serial_break", None)
        if fn is None:
            raise NativeCoreUnavailable(
                "this ngpc_core has no ngpc_set_serial_break -- it predates ABI 17; "
                "rebuild cpp/")
        fn(self._h, 1 if on else 0)

    def serial_read_tx(self, max_bytes: int = 64) -> bytes:
        """Drain the bytes this machine has transmitted since the last call."""
        out = (c_uint8 * max_bytes)()
        n = self._lib.ngpc_serial_read_tx(self._h, out, max_bytes)
        return bytes(out[:n])

    def serial_write_rx(self, data: bytes) -> None:
        """Queue bytes for this machine to receive from the peer."""
        if data:
            self._lib.ngpc_serial_write_rx(self._h, _buf(data), len(data))

    def serial_rts(self) -> bool:
        """True when this machine is ready to receive (RTS low)."""
        return self._lib.ngpc_serial_rts(self._h) != 0

    def serial_set_cts(self, high: bool) -> None:
        """Drive this machine's CTS0 handshake input (wired to the PEER's RTS).

        `high` True -> CTS0 high: if the game enabled CTSE (SC0MOD bit6), its
        transmitter halts a queued byte until the peer drops RTS. A bridge passes
        the peer's RTS state here each pump so the hardware handshake is modelled.
        """
        self._lib.ngpc_serial_set_cts(self._h, 1 if high else 0)

    def serial_state(self) -> SerialState:
        """Snapshot the serial channel: FIFO depths, handshake lines, the SC0
        registers and the per-stage byte/interrupt counters. Read-only -- see
        SerialState. Feeds the debugger's Link tab."""
        st = SerialState()
        self._lib.ngpc_serial_state(self._h, ctypes.byref(st))
        return st

    def link_pair_max_gap(self) -> int:
        """Widest cycle gap between the two consoles during the last linked call.

        The number that says whether they were really interleaved -- totals cannot,
        because a whole frame each in sequence costs exactly the same cycles as
        taking turns while leaving one console frozen throughout the other's frame.
        """
        return int(self._lib.ngpc_link_pair_max_gap(self._h))

    def link_relay_count(self) -> int:
        """Relays the CORE has done for this console since the cable came up.

        Zero while a host owns the relay itself. The question it answers is the one
        The Last Blade's handshake cares about: was the cable relayed many times
        inside a frame, or once at the end of it?
        """
        return int(self._lib.ngpc_link_relay_count(self._h))

    def run_frames(self, frames: int = 1, *, max_instrs: int | None = None) -> Summary:
        """Advance whole FRAMES. The core owns the raster, so it owns the boundary.

        `max_instrs` is a runaway backstop, not a target -- a frame is about
        102 000 cycles, so a few tens of thousands of instructions.
        """
        # A frame is ~102 000 cycles, so a few tens of thousands of instructions.
        # 200 000 per frame is a runaway backstop with a wide margin, not a target.
        budget = max_instrs if max_instrs is not None else max(frames, 1) * 200_000
        summary = Summary()
        self._lib.ngpc_run_frames(self._h, frames, budget, ctypes.byref(summary))
        return summary

    def run(self, count: int, *, record: bool = True) -> tuple[Summary, list[Record]]:
        """Run up to `count` instructions in ONE FFI crossing.

        `record=False` is the real-speed path: the core retires instructions
        without building per-instruction records.
        """
        summary = Summary()
        if record:
            recs = (Record * count)()
            self._lib.ngpc_run(self._h, count, recs, count, ctypes.byref(summary))
            return summary, list(recs[: summary.emitted])
        self._lib.ngpc_run(self._h, count, None, 0, ctypes.byref(summary))
        return summary, []


def run_linked(a: "NativeMachine", b: "NativeMachine", frames: int = 1, *,
               max_instrs: int | None = None) -> "tuple[Summary, Summary]":
    """Advance two CABLED consoles together, with the relay done inside the core.

    This replaces the host-side pattern it was written from -- run `a` for a slice of
    instructions, cross the FFI boundary, move the bytes, run `b` for a slice, cross
    back -- and it replaces it for a reason that is not performance.

    ⛔ AN INSTRUCTION COUNT IS NOT CABLE TIME. `CABLE_SLICE = 400` approximated one by
    the other, and the two link faults this project paid most for -- Card Fighters'
    Clash's versus handshake and The Last Blade's threshold -- are the same symptom of
    that approximation rather than two separate bugs. Every emulator that shipped a
    working serial link reached the same conclusion first (LINK_NETPLAY_STUDY.md L3):
    put BOTH consoles and the cable in the core, paced by the hardware's serial clock.

    ⚡ The core now advances whichever console is BEHIND IN CYCLES, in steps bounded by
    a fraction of the cable's own byte time, and relays the moment either console says
    the cable moved. The pair can never be more than one quantum of emulated time apart.

    Both machines must have the serial hardware enabled first: the cable goes in before
    either console boots. Returns one summary per console, in the order given.
    """
    budget = max_instrs if max_instrs is not None else max(frames, 1) * 400_000
    sa, sb = Summary(), Summary()
    a._lib.ngpc_run_linked(a._h, b._h, frames, budget,
                           ctypes.byref(sa), ctypes.byref(sb))
    return sa, sb


def status_name(code: int) -> str:
    return STATUS.get(code, f"unknown-status-{code}")
