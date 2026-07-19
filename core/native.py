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

ABI_VERSION = 13

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
    20: "unknown-opcode",
    21: "truncated",
    22: "unmapped",
    30: "unimplemented",
    40: "breakpoint",
    41: "count-reached",
}

STATUS_OK = 0
STATUS_HALTED = 1
STATUS_BREAKPOINT = 40
STATUS_COUNT_REACHED = 41

_DLL_NAME = "ngpc_core.dll"
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
    lib.ngpc_get_apu_state.argtypes = [c_void_p, POINTER(ApuState)]
    lib.ngpc_get_apu_state.restype = None
    lib.ngpc_set_apu_channel_mask.argtypes = [c_void_p, c_uint32]
    lib.ngpc_set_apu_channel_mask.restype = None
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
    lib.ngpc_set_cart_wait.argtypes = [c_void_p, c_uint32]
    lib.ngpc_set_cart_wait.restype = None
    lib.ngpc_set_cart_data_wait.argtypes = [c_void_p, c_uint32]
    lib.ngpc_set_cart_data_wait.restype = None
    lib.ngpc_set_vram_wait.argtypes = [c_void_p, c_uint32]
    lib.ngpc_set_vram_wait.restype = None
    lib.ngpc_set_ldir_cost.argtypes = [c_void_p, c_uint32]
    lib.ngpc_set_ldir_cost.restype = None
    lib.ngpc_set_flash_size.argtypes = [c_void_p, c_uint32, c_uint32]
    lib.ngpc_set_flash_size.restype = None
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

    def set_cart_wait(self, cycles_per_byte: int) -> None:
        """Wait-states per byte fetched from cartridge flash (0 = free/old behaviour).

        The cart flash is slow; every instruction is fetched from it, so cart code ran
        ~3.4x too fast with free fetches. Calibrated by hw_calibration/cpu_calib_v1.ngc.
        """
        self._lib.ngpc_set_cart_wait(self._h, int(cycles_per_byte))

    def set_cart_data_wait(self, cycles_per_byte: int) -> None:
        """Wait-states per byte of a RANDOM data read from cart flash (0 = same as fetch).

        Sequential fetch is cheap (flash page-mode); an arbitrary LD from a cart table
        eats the full random-access latency. Calibrated so Cool Boarders' silicon-confirmed
        30fps reproduces on top of the fetch cost. See Machine::cart_data_wait.
        """
        self._lib.ngpc_set_cart_data_wait(self._h, int(cycles_per_byte))

    def set_vram_wait(self, cycles_per_byte: int) -> None:
        """EXPERIMENTAL wait-states per byte written to display RAM (0x8000-0xBFFF).

        Tests whether the K2GE active-display access throttle explains the residual
        speed of self-timed games after the (silicon-confirmed) CPU model is exact.
        Needs a v3 calibration ROM to confirm. See Machine::vram_wait.
        """
        self._lib.ngpc_set_vram_wait(self._h, int(cycles_per_byte))

    def set_ldir_cost(self, cycles_per_byte: int) -> None:
        """Cycles/byte for LDIR/LDDR block copies (default 7 = datasheet). 14 reproduces
        Cool Boarders' silicon 30fps; the datasheet figure is likely a floor (as MUL/DIV
        were). See Machine::ldir_cost."""
        self._lib.ngpc_set_ldir_cost(self._h, int(cycles_per_byte))

    def set_flash_size(self, size_bytes: int, *, chip: int = 0) -> None:
        """Present the cart as a flash chip of this capacity (rebuilds the erasable-block
        map). Lets an under-filled homebrew ROM save in its chip's top block. See
        ngpc_set_flash_size in core.cpp."""
        self._lib.ngpc_set_flash_size(self._h, int(chip), int(size_bytes))

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

    def cpu(self) -> CpuState:
        st = CpuState()
        self._lib.ngpc_get_cpu(self._h, ctypes.byref(st))
        return st

    def set_cpu(self, st: CpuState) -> None:
        self._lib.ngpc_set_cpu(self._h, ctypes.byref(st))

    def read(self, address: int, count: int) -> bytes:
        out = (c_uint8 * count)()
        self._lib.ngpc_read_mem(self._h, address, out, count)
        return bytes(out)

    def write(self, address: int, data: bytes) -> None:
        self._lib.ngpc_write_mem(self._h, address, _buf(data), len(data))

    def set_breakpoints(self, pcs: list[int]) -> None:
        arr = (c_uint32 * len(pcs))(*pcs)
        self._lib.ngpc_set_breakpoints(self._h, arr, len(pcs))

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


def status_name(code: int) -> str:
    return STATUS.get(code, f"unknown-status-{code}")
