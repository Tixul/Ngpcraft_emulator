"""Minimal read-only memory access helpers for NgpCraft Emulator."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from core.bus import AddressProbe, NgpcAddressSpace, load_address_space
from core.rom import NgpcRomHeader, load_rom_header

if TYPE_CHECKING:
    # Forward-only import to keep memory.py free of an unconditional
    # dependency on frame_timing (the M3 module). The runtime import
    # happens locally inside `_build_builtin_readable_bytes` below.
    from core.frame_timing import FrameState


@dataclass(frozen=True)
class RomImage:
    """Loaded ROM file image."""

    path: Path
    data: bytes
    header: NgpcRomHeader

    @property
    def size(self) -> int:
        return len(self.data)


@dataclass(frozen=True)
class MemoryReadResult:
    """Result of a minimal read-only memory probe."""

    address: int
    width: int
    status: str
    probe: AddressProbe
    data: bytes | None
    note: str


@dataclass(frozen=True)
class NgpcReadBus:
    """Current minimal read-only bus model."""

    rom: RomImage
    address_space: NgpcAddressSpace
    builtin_bytes: dict[int, int]
    bios_bytes: bytes | None = None

    def read_bytes(self, address: int, size: int = 1) -> MemoryReadResult:
        if size <= 0:
            raise ValueError("size must be >= 1")

        chunks: list[int] = []
        first_probe: AddressProbe | None = None
        for offset in range(size):
            cur_addr = address + offset
            probe = self.address_space.probe(cur_addr)
            if first_probe is None:
                first_probe = probe
            if probe.region is None:
                return MemoryReadResult(
                    address=address,
                    width=size,
                    status="unmapped",
                    probe=probe,
                    data=None,
                    note="Read touches an unmapped address.",
                )
            if cur_addr in self.builtin_bytes:
                chunks.append(self.builtin_bytes[cur_addr])
                continue
            if probe.region.kind == "rom-gap":
                # Erased flash: both cart windows (0x200000 chip 0, 0x800000
                # chip 1) read 0xFF where the file does not reach.
                chunks.append(0xFF)
                continue
            if probe.region.kind == "bios" and self.bios_bytes is not None:
                if probe.region_offset is None or probe.region_offset >= len(self.bios_bytes):
                    return MemoryReadResult(
                        address=address,
                        width=size,
                        status="out-of-file",
                        probe=probe,
                        data=None,
                        note="Computed BIOS file offset is outside the loaded BIOS image.",
                    )
                chunks.append(self.bios_bytes[probe.region_offset])
                continue
            if probe.file_offset is None:
                return MemoryReadResult(
                    address=address,
                    width=size,
                    status="unbacked",
                    probe=probe,
                    data=None,
                    note=(
                        "Address is mapped in the current model but not backed by readable "
                        "data yet."
                    ),
                )
            if probe.file_offset >= self.rom.size:
                return MemoryReadResult(
                    address=address,
                    width=size,
                    status="out-of-file",
                    probe=probe,
                    data=None,
                    note="Computed ROM file offset is outside the loaded file.",
                )
            chunks.append(self.rom.data[probe.file_offset])

        assert first_probe is not None
        return MemoryReadResult(
            address=address,
            width=size,
            status="ok",
            probe=first_probe,
            data=bytes(chunks),
            note=(
                "Read satisfied from the loaded ROM image, erased-cart fallback and/or the "
                "current minimal built-in system-memory backing."
            ),
        )


# TMP95C061 on-chip I/O page (0x000000..0x0000FF) POWER-ON values.
#
# These registers do NOT reset to zero. Transcribed from the reference
# emulator's reset table (NeoPop Core `mem.c` `systemMemory[]`) -- the same
# oracle used for the BIOS HLE / SWI dispatch work. Rows are 16 bytes each.
_IO_PAGE_RESET_VALUES = (
    # 0x00
    0x00, 0x00, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF,
    0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0x08, 0xFF, 0xFF,
    # 0x10
    0x34, 0x3C, 0xFF, 0xFF, 0xFF, 0x3F, 0x00, 0x00,
    0x3F, 0xFF, 0x2D, 0x01, 0xFF, 0xFF, 0x03, 0xB2,
    # 0x20  (0x20 TRUN=0x80, 0x24 T01MOD=0x03)
    0x80, 0x00, 0x01, 0x90, 0x03, 0xB0, 0x90, 0x62,
    0x05, 0x00, 0x00, 0x00, 0x0C, 0x0C, 0x4C, 0x4C,
    # 0x30
    0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
    0x30, 0x00, 0x00, 0x00, 0x20, 0xFF, 0x80, 0x7F,
    # 0x40
    0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
    0x30, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
    # 0x50
    0x00, 0x20, 0x69, 0x15, 0x00, 0x00, 0x00, 0x00,
    0x00, 0x00, 0x00, 0x00, 0xFF, 0xFF, 0xFF, 0xFF,
    # 0x60  (0x60/0x61 = ADC data register -- overridden below, see note)
    0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
    0x17, 0x17, 0x03, 0x03, 0x02, 0x00, 0x00, 0x4E,
    # 0x70  (0x70/0x71 = interrupt-priority INTxx registers)
    0x02, 0x32, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
    0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
    # 0x80
    0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
    0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
    # 0x90
    0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
    0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
    # 0xA0
    0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
    0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
    # 0xB0
    0x00, 0x00, 0x00, 0x00, 0x0A, 0x00, 0x00, 0x00,
    0xAA, 0xAA, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
    # 0xC0
    0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
    0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
    # 0xD0
    0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
    0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
    # 0xE0
    0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
    0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
    # 0xF0
    0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
    0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
)
assert len(_IO_PAGE_RESET_VALUES) == 0x100


def io_reset_value(address: int) -> int:
    """The POWER-ON value of an on-chip I/O register.

    The TMP95C061's I/O page does not reset to zero, and the peripherals must not
    pretend it does. `TRUN` (0x20) powers on at **0x80** -- PRRUN already RUNNING --
    so the prescaler and the external TI0 input are live from the first cycle.

    The CPU has always read these through the fetch view's built-in bytes. The
    timers and the A/D converter did NOT: they read the writable overlay directly
    with `memory.get(addr, 0)`, and an untouched overlay handed them a phantom
    `TRUN = 0`. The 8-bit timers therefore held their horizontal-blank counter at
    zero until the game happened to write TRUN, leaving the whole timer chain 440
    cycles out of phase with the video clock -- and a horizontal-blank interrupt
    that lands one instruction early takes the WRONG interrupt (DEVLOG pass 208).
    """
    if 0 <= address < 0x100:
        return _IO_PAGE_RESET_VALUES[address]
    return 0


def flash_size_code(rom_size: int) -> int:
    """Which cartridge the BIOS decides is in the slot: 1 = 4 Mbit, 2 = 8, 3 = 16.

    The BIOS learns this by running the autoselect probe on the flash chip and decoding
    its device ID (0xAB / 0x2C / 0x2F), so this ladder must be the SAME ladder the chip
    answers with -- `Machine::flash_device_id` in the native core. Two independent size
    ladders is how a 4 Mbit cartridge gets told it is 8 Mbit by one path and 4 by another.
    """
    if rom_size <= 0x080000:
        return 1        # 4 Mbit
    if rom_size <= 0x100000:
        return 2        # 8 Mbit
    return 3            # 16 Mbit


def _build_builtin_readable_bytes(
    header: NgpcRomHeader,
    *,
    frame_state: "FrameState | None" = None,
) -> dict[int, int]:
    """Return the built-in readable system-memory cold-start slice.

    Per `MEMORY_READ.md`, all writable on-chip regions (Work RAM, system
    page, shared Z80 RAM, K2GE registers, scroll maps, character RAM) are
    pre-initialised to `0x00` to match the documented power-on state.
    The `_check_writable_range` guard in `core/execute.py` still routes
    writes through the runtime overlay, which shadows these defaults on
    read (`_read_runtime_bytes` prefers the overlay over this builtin
    map).

    The single non-zero cell is `0x006F91` (HW_SYSTEM_MODE), which the
    BIOS reads from the ROM header mode byte at power-on.

    Cold-start invariants (matching real NGPC silicon at reset):
    - `0x004000..0x006BFF` : Work RAM, read as 0 at power-on
    - `0x006C00..0x006FFF` : system RAM page (including system-reserved
      slices), read as 0 at power-on; `0x006F91` carries the ROM header
      mode byte
    - `0x007000..0x007FFF` : shared Z80 RAM, read as 0 at power-on
    - `0x008000..0x008FFF` : K2GE registers and palette RAM, mostly 0
      at power-on with three documented non-zero K2GE control-register
      reset values (per `NGPC_HW_QUICKREF.md` § 5):
        * `0x008004` WSI.H : 0xFF (window width — full screen)
        * `0x008005` WSI.V : 0xFF (window height — full screen)
        * `0x008006` REF   : 0xC6 (frame rate — DO NOT MODIFY)
    - `0x009000..0x0097FF` : SCR1 map, read as 0
    - `0x009800..0x009FFF` : SCR2 map, read as 0
    - `0x00A000..0x00BFFF` : character RAM, read as 0

    The CPU I/O page (`0x000000..0x0000FF`) is pre-populated to `0x00` and
    tracked as a register file (writes write through to the overlay). This is
    an approximation: individual timer / DMA / interrupt-controller registers
    have subsystem-specific reset values and read side-effects that are not yet
    modeled per-register, but last-write-wins tracking lets the BIOS boot's
    read-modify-write config sequences run.

    When `frame_state` is provided (M3 Phase 3.1+), the K2GE raster
    position register `RAS.V` (`0x008009`) is overridden with the
    current scanline value and the `2D Status` register
    (`0x008010`) gets bit 6 (BLNK) set when `frame_state.in_vblank`
    is True. Other bits of `2D Status` stay 0 (C.OVR sprite overflow
    not modeled yet). With `frame_state=None`, both bytes default to
    `0x00` — equivalent to the documented HW reset (`initial_frame_state()`
    has `scanline=0`, `in_vblank=False`).
    """
    builtin: dict[int, int] = {}
    # CPU on-chip I/O page (0x000000..0x0000FF): modeled as a tracked register
    # file. CPU I/O immediate stores write through to the runtime overlay (see
    # `_try_execute_cpu_io_store`), so read-modify-write config sequences the
    # BIOS boot uses (e.g. `or (0x00B2), imm8`) observe the last written value.
    # Faithful for config registers (last-write-wins); status registers with
    # read side-effects would need per-register modeling later.
    #
    # POWER-ON VALUES (2026-07-10): the TMP95C061 on-chip registers do NOT reset
    # to zero -- they have documented reset values (timer run/mode registers,
    # watchdog, interrupt-priority INTxx registers, ...). We previously cold-
    # filled the whole page with 0x00, which is simply wrong. These values are
    # transcribed from the reference emulator's reset table (NeoPop `mem.c`
    # `systemMemory[]`), the same oracle used for the BIOS HLE and SWI work.
    # Notable entries: 0x20 TRUN=0x80, 0x24 T01MOD=0x03, 0x6F watchdog=0x4E,
    # 0x70/0x71 interrupt-priority = 0x02/0x32 (the INTxx registers VECT_INTLVSET
    # rewrites), 0xB8/0xB9 = 0xAA/0xAA.
    for addr, value in enumerate(_IO_PAGE_RESET_VALUES):
        builtin[addr] = value
    # --- ADC data register (I/O 0x60/0x61) = the BATTERY reading -------------
    #
    # The TMP95C061 A/D converter reads the NGPC's battery voltage. Its 10-bit
    # result sits left-aligned in the 16-bit data register, i.e. `ADREG =
    # result << 6`, so the BIOS recovers it with `ldw WA,(0x60); srl 6` and
    # caches it at 0x6F80 (which is why the observed hand-off value there is
    # 0x03FF = full scale).
    #
    # Modelling this register as 0 told the BIOS the battery was FLAT: its
    # power-on check is
    #     0xFF21E0  cp WA, 0x01D3     ; below the low-battery threshold?
    #     0xFF21E4  jr NC, 0xFF21EA   ; healthy -> skip
    #     0xFF21E6  ld RW3, 0         ; RW3 = 0 = VECT_SHUTDOWN
    #     0xFF21E9  swi 1             ; power off
    # so the boot powered itself off before doing anything (found 2026-07-10 by
    # the SWI SHUTDOWN honest-stop firing at 0xFF21E9 -- which also independently
    # confirms the RW3 vector mapping: the BIOS literally writes `ld RW3,0`).
    #
    # DATASHEET (TMP95C061 Figure 3.12 (3-1), A/D Conversion Result Register):
    #   ADREG04L (0x0060) bits 7-6 = ADR01/ADR00 = the LOWER 2 bits of the AN0
    #                     result; bits 5-0 are unused and READ AS 1.
    #   ADREG04H (0x0061)          = the UPPER 8 bits of the AN0 result.
    # So the 16-bit little-endian word at 0x60 is `(result << 6) | 0x3F`, which is
    # why the BIOS recovers the 10-bit value with `ldw WA,(0x60); srl 6`.
    # The NGPC wires its single A/D channel (power management) to AN0.
    #
    # A healthy battery reads near full scale (0x03FF), giving 0xFF / 0xFF.
    #
    # DELIBERATE DEVIATION FROM THE ORACLE: NeoPop resets 0x60/0x61 to 0x00. That
    # is fine for NeoPop -- it HLE's the BIOS and so never runs the real power-on
    # battery check. We DO run the real BIOS boot, so a zero here means "flat
    # battery" and the console powers itself off. This is the hardware-faithful
    # value, not a workaround.
    builtin[0x000060] = 0xFF  # ADREG0L: (0x3FF & 3) << 6 | 0x3F (unused bits read 1)
    builtin[0x000061] = 0xFF  # ADREG0H: 0x3FF >> 2
    # --- RTC (the calendar IC) and the POWER/BATTERY line ---------------------
    #
    # The NGPC carries a real-time-clock IC the BIOS reads at I/O 0x90-0x97 (BCD:
    # 0x90 enable, 0x91 year, 0x92 month, 0x93 day, 0x94 hour, 0x95 minute, 0x96
    # second, 0x97 weekday+leap). ares (ngp/cpu/rtc.cpp, io.cpp) is the oracle. Like
    # the ADC above, the reference core BAKES the power-on seed -- a running clock is
    # the native core's rtc_step, and no rendered game reads these -- so parity only
    # needs the cold-start value. Seed = 2024-01-01, Monday (matches memory.cpp).
    builtin[0x000090] = 0x01  # RTC enable
    builtin[0x000091] = 0x24  # year  (BCD 2024)
    builtin[0x000092] = 0x01  # month
    builtin[0x000093] = 0x01  # day
    builtin[0x000097] = 0x01  # weekday (hour/minute/second seed 0, already 0 here)
    # Port 0xB1: bit1 = the CR2032 SUB-BATTERY (1 = healthy), bit2 = a must-be-1
    # line. Leaving them 0 is the "SUB BATTERY DEAD" boot loop. bit0 (the POWER
    # level) stays 0: MEASURED against this core, forcing it to ares' "released = 1"
    # parks the BIOS boot blank at 0xFF1127 -- see the read hook in machine.hpp.
    builtin[0x0000B1] = 0x06
    # Work RAM + system page
    for addr in range(0x004000, 0x007000):
        builtin[addr] = 0x00
    builtin[0x006F91] = header.mode_raw & 0xFF
    # INTE45 = 0xDC -- INT4 (VBlank) at level 4, INT5 at level 5.
    #
    # MEASURED off the real BIOS boot (pass 237): this is what the BIOS leaves armed
    # before it jumps to the cart, and it matters because VBlank's level is READ from
    # this register rather than assumed. A cart that never writes INTE45 -- and several
    # do not -- would otherwise inherit level 0, which the chip reads as "interrupt
    # prohibited", and would never see a VBlank at all. The BIOS arms it so the cart
    # does not have to.
    builtin[0x000071] = 0xDC
    # ⚡ THE SAVE. 0x6C58 is where the BIOS writes down which cartridge it found at
    # power-on: 1 = 4 Mbit, 2 = 8 Mbit, 3 = 16 Mbit, 0 = no card. Its flash system
    # calls (`swi 1`, VECT_FLASHWRITE / VECT_FLASHERS) read it FIRST and return the
    # error 0xFF without touching the chip if it is zero -- so with this byte missing,
    # every in-game save silently did nothing. 0x6C59 is the CS1 DEVELOPMENT slot,
    # and a production console has nothing plugged into it.
    #
    # The encoding is not a guess: booting the real BIOS with a 4 / 8 / 16 Mbit
    # cartridge and reading the byte back gives 1 / 2 / 3 (pass 240).
    builtin[0x006C58] = flash_size_code(header.file_size)
    builtin[0x006C59] = 0
    # K1GE compatible mode, for the MONOCHROME cartridges (header byte 0x23 < 0x10).
    # The BIOS sets it from the header (its own code, 0xFF17C4) because a game written
    # for the old machine never asks for it. Without it the mono games draw through
    # K2GE palettes they never wrote: a blank screen.
    if (header.mode_raw & 0xFF) < 0x10:
        builtin[0x0087E2] = 0x80
        builtin[0x0087F0] = 0x55   # re-locked, which is where the BIOS leaves it
        builtin[0x006F95] = 0x00
        # The COMPAT COLOUR PALETTE -- the grey ramp. MEASURED off the real BIOS.
        #
        # A K1GE game writes the 3-bit LEVEL table (0x8100) -- that WAS its palette on
        # the old machine -- and knows nothing about the 12-bit table the K2GE resolves
        # those levels through. That table is the COLOUR THEME the console applies to
        # old cartridges, exactly as a Game Boy Color tints a Game Boy game, and the
        # BIOS installs it.
        #
        # ⛔ It is NOT a table in the BIOS ROM: I searched the image for a grey ramp,
        # found none, and briefly took that as evidence there wasn't one. The BIOS
        # COMPUTES it. Booting the real BIOS with a mono cartridge and reading the
        # palette back is what produced these numbers -- all four planes, the same
        # eight levels, repeated across the sixteen entries.
        #
        # 🔑 "I could not find it in the ROM" is not "it does not exist".
        grey_ramp = (0x0FFF, 0x0DDD, 0x0BBB, 0x0999, 0x0777, 0x0444, 0x0333, 0x0000)
        for base in (0x0083A0, 0x0083C0, 0x0083E0, 0x008380):
            for i in range(16):
                colour = grey_ramp[i & 7]
                builtin[base + i * 2] = colour & 0xFF
                builtin[base + i * 2 + 1] = (colour >> 8) & 0xFF
    # BIOS hand-off system-RAM values the cart sees at entry (the BIOS
    # initialises these before jumping to the cart). Cross-checked 2026-07-09
    # against the native NeoPop reference (`oracle_tools/cosim.exe --dump-mem
    # 0x6F80`) and found UNIVERSAL across carts (Neo Turf / Pac-Man / Big Bang /
    # Metal Slug all identical), and consistent with the SNK BIOS reverse (the
    # BIOS manages 0x6F8x; DEVLOG pass 178). Without these, carts that read them
    # diverge at instruction ~1 (e.g. Neo Turf `ld W,(0x6F84)` expects 0x40 ->
    # WA=0x4000). 0x6F80/0x6F81 = 0x03FF is the ADC/contrast reading at
    # full-scale (the reference default; slightly HW-dependent).
    builtin[0x006F80] = 0xFF  # ADC/contrast reading low (0x03FF full-scale)
    builtin[0x006F81] = 0x03  # ADC/contrast reading high
    builtin[0x006F84] = 0x40  # BIOS system status byte
    builtin[0x006F87] = 0x01  # BIOS system status byte
    # Shared Z80 RAM
    for addr in range(0x007000, 0x008000):
        builtin[addr] = 0x00
    # K2GE register + palette RAM
    for addr in range(0x008000, 0x009000):
        builtin[addr] = 0x00
    # K2GE control-register reset overrides (HW-documented non-zero values).
    # K2GE power-on values. 2026-07-10: several were missing and defaulting to
    # 0x00, which is wrong -- most importantly the INTERRUPT-ENABLE register.
    # Transcribed from the reference emulator's `reset_memory()`.
    builtin[0x008000] = 0xC0  # control: VBlank (bit 7) + HBlank (bit 6) IRQs ENABLED
    builtin[0x008004] = 0xFF  # WSI.H — window width, full screen at reset
    builtin[0x008005] = 0xFF  # WSI.V — window height, full screen at reset
    builtin[0x008006] = 0xC6  # REF   — frame rate (never modified)
    builtin[0x008118] = 0x80  # BGC on
    builtin[0x0083E0] = 0xFF  # default background colour (low)
    builtin[0x0083E1] = 0x0F  # default background colour (high)
    builtin[0x0083F0] = 0xFF  # default window colour (low)
    builtin[0x0083F1] = 0x0F  # default window colour (high)
    builtin[0x008400] = 0xFF  # LED on
    # M3 Phase 3.1: frame_state-derived raster + VBlank bit. Defaults
    # to 0x00 when no frame_state is given (HW reset state).
    if frame_state is not None:
        builtin[0x008009] = frame_state.scanline & 0xFF      # RAS.V
        builtin[0x008010] = 0x40 if frame_state.in_vblank else 0x00  # 2D Status bit 6 BLNK
    # SCR1 / SCR2 / character RAM
    for addr in range(0x009000, 0x00C000):
        builtin[addr] = 0x00
    return builtin


def load_rom_image(path: str | Path) -> RomImage:
    """Load ROM bytes and parse the header once."""
    rom_path = Path(path)
    data = rom_path.read_bytes()
    header = load_rom_header(rom_path)
    return RomImage(path=rom_path, data=data, header=header)


def load_read_bus(
    path: str | Path,
    *,
    frame_state: "FrameState | None" = None,
    bios_path: str | Path | None = None,
) -> NgpcReadBus:
    """Load the current minimal read-only bus model.

    M3 Phase 3.1+: an optional `frame_state` is forwarded to
    `_build_builtin_readable_bytes` so reads of `RAS.V` (`0x8009`) and
    the BLNK bit of `2D Status` (`0x8010`) reflect the live frame
    timing. Callers without a frame_state (most CLI commands at
    bootstrap) get the documented HW reset (scanline 0, BLNK=0) which
    is byte-identical to the pre-Phase 3.1 behavior.
    """
    rom = load_rom_image(path)
    bios_bytes = None
    if bios_path is not None:
        bios_bytes = Path(bios_path).read_bytes()
        if len(bios_bytes) != 0x10000:
            raise ValueError(
                f"BIOS image must be exactly 65536 bytes; got {len(bios_bytes)} from {bios_path}"
            )
    return NgpcReadBus(
        rom=rom,
        address_space=load_address_space(path),
        builtin_bytes=_build_builtin_readable_bytes(rom.header, frame_state=frame_state),
        bios_bytes=bios_bytes,
    )
