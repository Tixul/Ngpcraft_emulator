"""A session that runs on the NATIVE core — the point of the whole chantier.

`EmulatorSession` retires about 1 700 instructions a second. A Neo Geo Pocket
Color needs roughly 615 000 to run in real time, so the Python session has never
been able to *play* a game: it inspects one. This session hands the same job to
the C++ core, which retires around 40 million a second, and gets the frame back.

WHAT IT DOES NOT DO
-------------------
It is not a drop-in replacement for `EmulatorSession`, and it does not pretend to
be. The Python session carries the honest-stop machinery, the tri-state analysis,
the event log and the whole debugger surface; those stay where they are. This is
the RUN path: boot a cartridge, advance whole frames, hand the K2GE's memory to
the renderer. That is what "an emulator that reads games at real speed" means, and
it is the one thing the Python session structurally could not do.

THE SEAM
--------
`specs/CPP_CORE_PORT.md` §4 lists nine hazards in the seam between shell and core.
The ones that bite here are settled as follows, and they are settled the same way
in both directions:

  * **Frame pacing belongs to the core** (hazard 4). The native core owns the
    scanline counter, the VBlank edge, the interrupt controller, the A/D converter
    and the four timers. The shell does not advance the raster, does not fold
    pending interrupts and does not tick a peripheral. If it did, everything would
    be counted twice.
  * **The core owns RAS.V and BLNK** (hazard 2). The Python session pokes 0x8009
    and 0x8010 into its fetch view every batch; the native core writes them into
    its address space each scanline, which is what the hardware does.
  * **No per-instruction memory dict** (hazard 1). Nothing here copies memory per
    step. A frame is one FFI crossing (~292 ns), and the renderer gets a bulk read
    of the video window afterwards.
"""

from __future__ import annotations

import ctypes
import sys
import time
from datetime import datetime
from pathlib import Path

from core import flash_file, native, rom_loader
from core.frame_timing import CYCLES_PER_SCANLINE, SCANLINES_PER_FRAME
from core.renderer import RenderedFrame, render_frame

# What the renderer actually reads: the K2GE register file, the palettes, the
# tilemaps, the tiles and the sprite table all live in 0x8000..0xBFFF, and the
# backdrop/control bytes it resolves are in there too. Reading the block in one
# crossing costs one call; reading it byte by byte would cost 16 384.
VIDEO_WINDOW_START = 0x008000
VIDEO_WINDOW_END = 0x00C000

# A frame is a fixed number of scanlines, and a scanline a fixed number of cycles.
# The core is driven in INSTRUCTIONS, so we cannot ask it for "one frame" directly
# -- we run until its own frame counter moves. That keeps the frame boundary where
# the hardware puts it (the raster) rather than where a batch size happens to fall.
CYCLES_PER_FRAME = CYCLES_PER_SCANLINE * SCANLINES_PER_FRAME

# Saves live HERE, not next to the ROM. The ROM directory is the player's collection
# and it is not ours to scatter files through; `saves/x.flash` is the same standard
# format either way, and copying it next to a ROM is all another emulator needs.
# Frozen into a single .exe, this must sit BESIDE the .exe (writable, persistent) --
# never under sys._MEIPASS, whose extraction dir is wiped when the process exits.
if getattr(sys, "frozen", False):
    SAVE_DIR = Path(sys.executable).resolve().parent / "saves"
else:
    SAVE_DIR = Path(__file__).resolve().parent.parent / "saves"


def default_save_path(rom_path: Path) -> Path:
    return SAVE_DIR / f"{rom_path.stem}.flash"


# The CONSOLE's memory, not a cartridge's: one file for the machine, whatever is in the
# slot. This is the coin-cell-backed RAM the BIOS keeps its settings in.
SYSTEM_RAM_PATH = SAVE_DIR / "system.ram"

# ⚡ AND THE OTHER HALF OF THE SAME COIN CELL: THE CLOCK.
#
# One CR2032 keeps the RAM above alive AND runs the calendar IC. On hardware they are a
# single battery domain -- a console that still knows your language necessarily still
# knows the date. We were persisting only the RAM half, so every launch re-seeded the
# clock to the core's hardcoded 2024-01-01 while the language survived: half a coin cell.
#
# It went unnoticed because the clock is machine state, NOT memory, so it never rode
# along in the RAM dump the way settings do (it is unreachable through `read`).
#
# MEASURED against the retail BIOS: on a CONFIGURED console the BIOS does not write the
# chip even once -- it trusts it and will never correct it -- so a wrong clock stays
# wrong forever. On a BLANK cell it rewrites 1998-01-01 itself, which is the authentic
# dead-battery behaviour and is left alone.
#
# A separate file rather than bytes appended to system.ram: that file is a raw 12 KiB RAM
# image every other tool reads positionally, and growing it would break that contract.
SYSTEM_RTC_PATH = SAVE_DIR / "system.rtc"
_RTC_BLOB_SIZE = ctypes.sizeof(native.RtcState)

# The chip's sub-second `counter` is in CPU CYCLES and wraps at one second --
# `kRtcCyclesPerSecond` in cpp/src/memory.cpp. Keep the two equal.
RTC_CYCLES_PER_SECOND = 6_144_000
# Half a second: the furthest two crystals can be out of phase with each other, and a
# fixed number rather than a random one so a run stays reproducible. See
# `NativeSession.second_console` for why a second console needs a phase of its own.
SECOND_CONSOLE_PHASE = RTC_CYCLES_PER_SECOND // 2


def offset_crystal_phase(machine, cycles: int = SECOND_CONSOLE_PHASE) -> None:
    """Move a machine's RTC crystal out of phase, without moving its clock.

    Only `counter` changes -- the free-running cycle count inside the current second.
    Every displayed field (year..second) is left exactly as it was, so this invents no
    time: it says the second console's oscillator is not the first one's, which is the
    one thing two coin cells are guaranteed not to share.
    """
    st = machine.rtc()
    st.counter = (int(st.counter) + int(cycles)) % RTC_CYCLES_PER_SECOND
    machine.set_rtc(st)

# ---------------------------------------------------------------- clock modes
# What the console's clock should do while the emulator is CLOSED. There is no single
# right answer, which is why it is a setting rather than a decision baked in here.
#
# HARDWARE  what a real console does: the coin cell keeps the calendar running, so shut
#           it for three days and it comes back three days later. The default.
# HOST      the clock is set from the PC's own clock at every launch. Always right, never
#           drifts, and ignores whatever the player set on the BIOS date screen.
# PAUSED    time stops with the emulator and resumes exactly where it left off. Not what
#           hardware does, but it is REPRODUCIBLE -- the one to pick for debugging, or to
#           keep a game's in-world clock where you left it.
# MANUAL    the clock is set to a date and time YOU chose, at every launch. On a real
#           console that is what the BIOS setup screen is for -- and the clean-room HLE
#           image has no setup screen, so without this there was no way to set the clock
#           at all short of changing the PC's. Deterministic on purpose: a game whose
#           events depend on the date can be put on the date you want, repeatably.
CLOCK_HARDWARE = "hardware"
CLOCK_HOST = "host"
CLOCK_PAUSED = "paused"
CLOCK_MANUAL = "manual"
CLOCK_MODES = (CLOCK_HARDWARE, CLOCK_HOST, CLOCK_PAUSED, CLOCK_MANUAL)

# A guard on the catch-up, not a policy: if the saved stamp is nonsense (a PC clock that
# jumped, a file copied from another machine) we would otherwise wind the chip forward one
# second at a time for an unbounded number of steps. Ten years is far past any real gap.
_MAX_CATCHUP_SECONDS = 10 * 365 * 24 * 3600


def _to_bcd(value: int) -> int:
    return (((value // 10) & 0x0F) << 4) | (value % 10)


def host_clock_state() -> "native.RtcState":
    """The PC's wall clock, in the packed BCD the chip's registers use."""
    t = time.localtime()
    st = native.RtcState()
    st.enable = 1
    st.year = _to_bcd((t.tm_year - 2000) % 100)
    st.month = _to_bcd(t.tm_mon)          # tm_mon is already 1-12
    st.day = _to_bcd(t.tm_mday)
    st.hour = _to_bcd(t.tm_hour)
    st.minute = _to_bcd(t.tm_min)
    st.second = _to_bcd(min(t.tm_sec, 59))   # a leap second would not be valid BCD
    st.weekday = (t.tm_wday + 1) % 7      # Python Mon=0..Sun=6 -> the chip's Sun=0..Sat=6
    st.counter = 0
    return st


def manual_clock_state(when: "str | None") -> "native.RtcState | None":
    """The chosen date/time, in the packed BCD the chip's registers use.

    `when` is an ISO-8601 string ("1999-01-01T12:00:00") -- what the settings store.
    Returns None when it cannot be read, and the caller then leaves the clock alone
    rather than inventing a date nobody chose."""
    if not when:
        return None
    try:
        t = datetime.fromisoformat(str(when))
    except (TypeError, ValueError):
        return None
    st = native.RtcState()
    st.enable = 1
    st.year = _to_bcd((t.year - 2000) % 100)
    st.month = _to_bcd(t.month)
    st.day = _to_bcd(t.day)
    st.hour = _to_bcd(t.hour)
    st.minute = _to_bcd(t.minute)
    st.second = _to_bcd(min(t.second, 59))
    st.weekday = (t.weekday() + 1) % 7        # Python Mon=0..Sun=6 -> the chip's Sun=0
    st.counter = 0
    return st


def read_rtc_file(path: Path) -> "tuple[native.RtcState, int | None] | None":
    """The clock as the console was last switched off, plus the PC timestamp of that
    moment (None for a file written before stamps existed, or if it is unusable).

    Returns None when there is nothing saved -- a brand-new console -- in which case the
    core's own seed stands, exactly as a fresh coin cell would.
    """
    try:
        blob = path.read_bytes()
    except OSError:
        return None
    # A file is the struct, optionally followed by the 8-byte stamp. Older files were
    # written before the struct grew its alarm fields and before stamps existed; they are
    # short, and the fields they are missing are exactly the ones that default to zero
    # (no alarm armed, no known stamp), so a short read is safe to accept.
    if len(blob) in (_RTC_BLOB_SIZE, _RTC_BLOB_SIZE + 8) or len(blob) < _RTC_BLOB_SIZE:
        padded = blob[:_RTC_BLOB_SIZE].ljust(_RTC_BLOB_SIZE, b"\x00")
        state = native.RtcState.from_buffer_copy(padded)
        stamp = (int.from_bytes(blob[_RTC_BLOB_SIZE:], "little", signed=True)
                 if len(blob) == _RTC_BLOB_SIZE + 8 else None)
        return state, stamp
    return None


def write_rtc_file(path: Path, state: "native.RtcState") -> None:
    """Save the clock, stamped with the PC's time -- the stamp is what lets the next
    launch work out how long the console was switched off."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_bytes(bytes(state) + int(time.time()).to_bytes(8, "little", signed=True))
    tmp.replace(path)


def apply_saved_clock(machine, path: Path, mode: str, manual: "str | None" = None) -> None:
    """Put the console's clock back, per the chosen mode. One place, so the game path,
    the BIOS-only path and any reboot all behave identically."""
    if mode == CLOCK_HOST:
        machine.set_rtc(host_clock_state())
        return
    if mode == CLOCK_MANUAL:
        state = manual_clock_state(manual)
        if state is not None:
            machine.set_rtc(state)
        return

    saved = read_rtc_file(path)
    if saved is None:
        # Nothing saved yet. In hardware mode start from the PC's clock, so a console
        # being used for the first time is simply right rather than starting in 2024.
        if mode == CLOCK_HARDWARE:
            machine.set_rtc(host_clock_state())
        return

    state, saved_at = saved
    machine.set_rtc(state)
    if mode != CLOCK_HARDWARE or saved_at is None:
        return
    elapsed = int(time.time()) - saved_at
    if 0 < elapsed <= _MAX_CATCHUP_SECONDS:
        machine.rtc_advance(elapsed)          # the coin cell kept running while it was off


# INT0 is the POWER BUTTON (pass 235). The BIOS boots, arms it, and sleeps.
INT0_POWER = 8

# One flash die is 2 MiB. A 4 MiB cart is two of them, and the hardware maps the second
# at 0x800000 -- NOT at 0x400000, which is not a cartridge window (pass 247, memory.cpp).
CART_CHIP_SIZE = 0x200000
CART_CHIP1_BASE = 0x800000

# What the BIOS reads to learn which flash card is in the slot, and the codes it expects
# (memory.cpp::flash_size_code). It decides the block-number -> address table from this.
BIOS_FLASH_CARD_TYPE = 0x006C58


def flash_size_code(capacity: int) -> int:
    """1 = 4 Mbit, 2 = 8 Mbit, 3 = 16 Mbit -- the same ladder the core uses."""
    if capacity <= 0x080000:
        return 1
    if capacity <= 0x100000:
        return 2
    return 3


class NativeSession:
    """Boot a cartridge on the native core and pull frames out of it."""

    def __init__(
        self,
        rom_path: str | Path,
        *,
        rom_bytes: bytes | None = None,
        from_archive: bool = False,
        bios_path: str | Path | None = None,
        save_path: str | Path | None = None,
        autosave: bool = True,
        save_to_rom: bool = True,
        sidecar: bool = False,
        flash_size: int = 0,
        real_bios: bool = False,
        clock_mode: str = CLOCK_HARDWARE,
        clock_manual: str | None = None,
        k1ge_console: bool = False,
        language: int = 1,          # 0 = Japanese, 1 = English (SDK SysWork 0x6F87)
        hle_bios: bool = False,     # our clean-room image: no setup screen, see below
        second_console: bool = False,   # the OTHER console of a local 2-player cable
    ):
        if not native.available():
            raise RuntimeError(
                "the native core is not built. `cmake --build cpp/build` first."
            )
        self.rom_path = Path(rom_path)
        # The ROM can arrive as a bare .ngc/.ngp, or packed in a .zip/.7z. The caller
        # may pass the already-unpacked bytes (the shell does, so it can size the flash
        # chip first); otherwise we unpack here, so every entry point -- thumbnails,
        # CLI, tests -- gets archive support for free.
        if rom_bytes is None:
            loaded = rom_loader.load(self.rom_path)
            self._rom = loaded.data
            from_archive = loaded.from_archive
        else:
            self._rom = bytes(rom_bytes)
        # An archive is read-only: a game's flash save can never be written back into
        # the .zip/.7z, so it goes to the standard sidecar .flash instead.
        self.from_archive = from_archive
        if from_archive:
            save_to_rom = False
        self._orig_rom = self._rom          # pristine baseline for a full sidecar diff
        bios = Path(bios_path).read_bytes() if bios_path else None
        self.machine = native.NativeMachine(self._rom, bios=bios)

        # ⚡ THE CONSOLE POWERING ON, versus BEING HANDED A GAME.
        #
        # `real_bios` runs the BIOS's own boot code from the hardware reset vector. The
        # default hands the cartridge the state that boot would have left, which is what
        # a game actually sees and is 700x faster to reach.
        #
        # The console's 12 KiB of RAM is kept alive by a coin cell -- that is where the
        # BIOS remembers your language and the date -- so it is handed over BEFORE the
        # reset, which consults the marker inside it to tell a first boot from a resume.
        # Which machine a monochrome cartridge thinks it is in. Set BEFORE either reset
        # path below, because the reset is what stamps 0x6F91.
        self.k1ge_console = k1ge_console
        # ⚡ WHO OWNS THE CONSOLE'S SETTINGS: the BIOS, or the emulator's UI.
        #
        # A REAL BIOS has a setup screen. That screen is the console's own control panel,
        # what the player sets on it goes into battery RAM, and `commit_system_ram` now
        # keeps it -- so it must WIN, in both boot modes. Overriding it from the UI made
        # the BIOS screen a decoration: you set the language on it and the emulator undid
        # the choice at the next launch.
        #
        # Our clean-room HLE image has no such screen (yet), so a console running it has
        # no control panel other than the UI. There, and on a console that has never been
        # configured at all, the setting is the only thing that can answer.
        self.hle_bios = hle_bios
        self.machine.set_k1ge_console(k1ge_console)
        # ...and which language the console is set to (0x6F87). Same deal: the reset is
        # what stamps it, and a bilingual cartridge reads that byte and nothing else.
        self.machine.set_language(language)
        self.real_bios = real_bios and bios is not None
        self.ram_path = SYSTEM_RAM_PATH
        self._power_pressed = False
        # The console's configured coin cell (language/date), as loaded. It is the
        # baseline a game must NOT overwrite: a game fills work RAM with its own state,
        # and saving that back as the coin cell would wipe the config. Kept here so the
        # BIOS->cart hand-off can boot the game from a clean slate yet still persist the
        # real config. `None` = a blank (first-boot) console.
        self.system_ram_baseline: bytes | None = None
        # The clock rides the same coin cell (see SYSTEM_RTC_PATH). Unlike the RAM
        # baseline it is restored in BOTH modes: work RAM in hand-off mode belongs to the
        # game and must not be written back as console settings, but the clock is never
        # the game's scratch -- it is the console's, and it should keep running across
        # launches the way the hardware's does.
        self.rtc_path = SYSTEM_RTC_PATH
        # ⛔ TWO CONSOLES CANNOT SHARE ONE COIN CELL, and local 2-player gave them one.
        #
        # `SYSTEM_RAM_PATH` / `SYSTEM_RTC_PATH` are the cell of THE console, and the shell
        # builds player 2's page through this same constructor -- so both machines booted
        # from the same file. MEASURED: two sessions created 400 ms apart came up with a
        # bit-identical clock down to `counter`, the sub-second CYCLE phase of the crystal
        # (1321875 on both). That is not a machine that ever existed: the counter is the
        # oscillator's own phase, and two coin cells do not share one.
        #
        # It is not only a fidelity point, it is REACHABLE. A game that seeds its RNG from
        # the clock gets the same stream on both consoles -- a two-console homebrew does
        # exactly that (`RandomNumberCounter = Second << 5`) and its host/client election
        # then has nothing left to break a tie. Attaching the cable power-cycles both
        # consoles at once (`_power_cycle_for_link`, deliberate and correct), so they also
        # boot in step. The emulator was manufacturing a role collision the hardware makes
        # far rarer.
        #
        # And the write-back collided: both sessions committed to the same file, so
        # whichever console was switched off LAST stamped its clock over the other's
        # (measured: P1 21h on disk, then P2 closes and it reads 02h).
        #
        # ⚖️ SO THE SECOND CONSOLE'S CELL IS READ-ONLY AND ITS CRYSTAL IS OUT OF PHASE.
        # It still boots from the player's configured cell -- same language, same date,
        # which is what somebody with two consoles would have -- but it never writes back,
        # and its sub-second phase is its own. Nothing is invented: no displayed field is
        # altered, only the crystal phase that no screen shows.
        #
        # ⚠️ WHAT THIS DOES *NOT* CLOSE, and it should be said plainly: a phase offset
        # moves the moment the second ticks over, so the two consoles read the same
        # wall-clock SECOND roughly half the time. A game seeding on `Second` alone still
        # collides on those. Closing that needs the second console to keep its own cell
        # with its own date -- a real second save file, deliberately not done here.
        self.second_console = bool(second_console)
        self.clock_mode = clock_mode if clock_mode in CLOCK_MODES else CLOCK_HARDWARE
        self.clock_manual = clock_manual
        # The console's own settings -- language, date, colour theme -- live in the coin
        # cell, and the player configured them once through Boot BIOS. Load that cell here
        # for BOTH boot modes: the fast hand-off is meant to skip the BIOS INTRO, not the
        # player's settings. (Only the WRITE-BACK differs -- see commit_system_ram: a game's
        # work RAM must never be persisted back as console settings.)
        if self.ram_path.exists():
            self.system_ram_baseline = self.ram_path.read_bytes()
        # Was this console configured BEFORE this launch? The answer decides who owns its
        # settings, and it must be taken now: the console-boot path fills the cell in as it
        # goes (the shell auto-completes the BIOS's first-boot wizard so a player who only
        # wanted to start a game is not left on a setup screen), so by hand-off time an
        # unconfigured console looks configured. The UI setting is the answer we give that
        # wizard on the player's behalf -- once. From the next launch the console remembers,
        # and its own setup screen is the only thing that changes it.
        self.started_unconfigured = self.system_ram_baseline is None
        # ⚡ Set by `handoff_reset` once the BIOS has handed the console to the cartridge:
        # from that moment the settings page in live RAM is the one the RESET seeded for
        # the game, not the console's own answer, and `commit_system_ram` must persist the
        # cell captured at the hand-off instead. See handoff_reset for what it cost to
        # learn that the two hand-off paths are not symmetrical here.
        self.cell_captured = False

        if self.real_bios:
            if self.system_ram_baseline is not None:
                self.machine.set_battery_ram(
                    self._cell_with_machine_type(self.system_ram_baseline))
            # BEFORE the reset, like the RAM: the BIOS reads the chip during its own boot.
            # With a configured cell it leaves what it finds; with a blank one it resets
            # the date to 1998-01-01 itself, which is the real dead-battery behaviour.
            apply_saved_clock(self.machine, self.rtc_path, self.clock_mode, self.clock_manual)
            if self.second_console:
                offset_crystal_phase(self.machine)   # its own crystal, see above
            self.machine.reset(real_bios=True)
        else:
            self.machine.reset(bios_handoff=True)
            # ⚡ AFTER the reset here, and that order is load-bearing. The hand-off reset
            # BOOTS THE REAL BIOS internally to capture the character RAM it leaves behind,
            # and it does so on a blank coin cell -- so the BIOS takes that boot's
            # dead-battery path and stamps 1998-01-01 over the chip. Restoring before the
            # reset would hand the player's clock straight to that warm-up to be wiped.
            apply_saved_clock(self.machine, self.rtc_path, self.clock_mode, self.clock_manual)
            if self.second_console:
                offset_crystal_phase(self.machine)   # its own crystal, see above
            # The player's language/date, laid back over the hand-off for the same reason as
            # the clock above: the fast boot skips the intro, not the settings. The warm-up
            # ran on a blank cell (its captured char RAM stays deterministic); here we put
            # the configured BIOS system page back so a dual-language cart (Match of the
            # Millennium) reads the language the console was set to, not the power-on default.
            self._restore_bios_settings_page()
            self._apply_bios_colour_theme()

        # Present the cart as a bigger flash chip than the (under-filled) ROM, so a homebrew
        # that saves in the chip's top block has that block. The working image becomes the
        # full chip (ROM + 0xFF), so the in-ROM save covers the save block too -- the .ngc
        # grows to the chip size on first save, exactly like padding it for the flashcart.
        #
        # ⚡ THE CAPACITY IS THE BLOCK NUMBERING, NOT JUST THE SIZE. A game erases by BLOCK
        # NUMBER (SDK FlashMem.txt, BLOCK_NO.INC) and the number->address table is different
        # for each chip: block 17 is 0xFA000 on an 8 Mbit card and 0x110000 on a 16 Mbit one.
        # Delta Warp saves in block 17 of an 8 Mbit card; presented as 16 Mbit its erase lands
        # two blocks away, the save area is never cleared, the read-back verify fails and the
        # game says "SAVE ERROR!" -- measured: 9 erases at 0x310000 while it programmed
        # 0x2FA000. So an explicit capacity has to be obeyed EVEN WHEN IT IS SMALLER than the
        # image: `> len(rom)` silently ignored every downward choice, which made the setting
        # look broken for exactly the cart that needs it. Only GROWING rewrites the image.
        # What the chip currently presents as: `ngpc_load_rom` built the map from the image,
        # which for a cart already padded to its chip size is the padded length -- so the
        # identity is read off the FILE, and a file grown by an earlier save keeps claiming
        # the bigger card forever. That is why an explicit setting must be able to shrink it.
        #
        # ⚡ AND THE CAPACITY IS ONE DIE'S, NOT THE WHOLE CARTRIDGE'S. The caller sizes the
        # chip from the ROM FILE, and a 4 MiB file is TWO 2 MiB dies -- so handing that
        # number to chip 0 tells the core a single die is 4 MiB long. Every consumer of
        # `flash_capacity(0)` then believes it: `_cart_windows` returns a 4 MiB window at
        # 0x200000, which runs 2 MiB past the end of the cart window. MEASURED on SvC The
        # Match of the Millennium (and it is the same for Metal Slug 2nd Mission and Densha
        # de Go! 2): `reboot()` raised `flash_restore: 0x200000+4194304 is not in the cart
        # window` -- and a reboot is what ATTACHING PLAYER 2 does, so two-player play on a
        # 4 MiB cart died on the spot; in the GUI that exception lands in a Qt slot, which
        # PyQt answers with qFatal, i.e. the whole emulator vanishes with no message.
        # `_read_cart_image` was reading 6 MiB for a 4 MiB cart too (chip 0's 2 MiB of
        # nothing included), so it never matched the file and an in-game save would have
        # written that 6 MiB back over the .ngc.
        die = min(flash_size, CART_CHIP_SIZE) if flash_size else 0
        self._flash_presented = min(len(self._rom), CART_CHIP_SIZE)
        if die and die != self._flash_presented:
            self.machine.set_flash_size(die)
            # The BIOS reads the card type BEFORE it touches the chip, and `reset` wrote it
            # from the pre-resize map -- so it has to be restated, or the byte and the block
            # map disagree about which card this is.
            self.machine.write(BIOS_FLASH_CARD_TYPE, bytes([flash_size_code(die)]))
        if die and die > len(self._rom):
            self._rom = self._orig_rom = bytes(self.machine.read(flash_file.CART_BASE, die))

        # THE SAVE. The cartridge is the save -- a game erases a block of its own ROM
        # and programs its slot back in -- so restoring one means putting those bytes
        # back into the cart image, which is what taking the cartridge out and putting
        # it back in does. `.flash` is the format the scene already shares.
        #
        # ⚠️ Saving needs the BIOS: a game reaches the flash through `swi 1`, and with
        # no BIOS image that vector reads back zero. No BIOS, no saves -- exactly as
        # a console with no BIOS would manage.
        self.autosave = autosave
        self.save_to_rom = save_to_rom
        self.sidecar = sidecar or not save_to_rom     # sidecar-only mode still needs a file
        self.save_path = Path(save_path) if save_path else default_save_path(self.rom_path)
        self.save_loaded = self._restore_save()

        self.executed = 0
        self._frame_count = 0
        self.stop_status: int | None = None
        self.stop_pc = 0

    # -- the save -----------------------------------------------------------

    def _restore_save(self) -> bool:
        """Put the cartridge back in, with whatever the player last wrote on it."""
        try:
            blocks = flash_file.read(self.save_path)
        except flash_file.BadFlashFile as exc:
            # Do NOT quietly start a new game on top of a save we failed to read: that
            # is how a player loses one and never finds out why.
            raise RuntimeError(f"{self.save_path} is not a usable save: {exc}") from exc
        for address, data in blocks:
            self.machine.flash_restore(address, data)
        self.machine.flash_clear_dirty()
        return bool(blocks)

    def commit_system_ram(self) -> bool:
        """The coin cell. Whatever the BIOS learnt -- your language, your colour theme --
        lives here, and on a console boot the BIOS SETUP SCREEN is what wrote it.

        Only in real-BIOS mode: in the hand-off the BIOS never ran, so nothing here is
        its work and saving it would invent settings the console never had.

        ⚡ WHAT IS PERSISTED, AND WHY IT IS NOT THE WHOLE OF RAM.
        Two regions, two owners:

          * 0x6C00-0x6FFF is the BIOS's own settings page. It is written by the setup
            screen, and it is saved LIVE -- what you set on that screen is what the
            console remembers, exactly as a coin cell behaves. This used to write the
            baseline back instead, so every choice made in the BIOS screen was thrown
            away on exit: the language asked again at every launch, the colour theme
            never sticking. (And with a blank cell it saved nothing at all, so a first
            boot re-ran the setup wizard for ever.)
          * everything below it is the GAME's work RAM. That one keeps the baseline: a
            game fills it with its own variables, and writing those back as console
            settings would wipe the config with a save file's leftovers.

        The clock is the other half of the same coin cell and rides its own file, live
        from the chip -- see `commit_rtc`. """
        if not self.real_bios:
            return False
        base = bytearray(self.system_ram_baseline
                         if self.system_ram_baseline is not None
                         else bytes(native.RAM_SIZE))
        # ⛔ ...EXCEPT ONCE THE CARTRIDGE HAS THE CONSOLE. `handoff_reset` captured the
        # cell the BIOS left -- setup screen included -- and then reset, which seeds a
        # power-on settings page for the game. Re-reading live RAM after that persists
        # THAT page, which is how a launch came to wipe the player's console config.
        # The captured cell is already the baseline, so there is simply nothing to
        # overlay: what the BIOS knew is what the console keeps.
        if not self.cell_captured:
            page_start, page_end = self._SETTINGS_PAGE
            live = self.machine.read(page_start, page_end - page_start)
            lo = page_start - native.RAM_START
            if lo + len(live) <= len(base):
                base[lo:lo + len(live)] = live
        self.ram_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.ram_path.with_suffix(".tmp")
        tmp.write_bytes(bytes(base))
        tmp.replace(self.ram_path)
        return True

    def commit_rtc(self) -> bool:
        """The clock, as the console is switched off. Its other half -- the settings --
        goes out through `commit_system_ram`.

        Saved in BOTH modes, unlike the RAM baseline: that one is skipped in hand-off mode
        because work RAM there is the GAME's and writing it back would invent settings.
        The clock is never the game's -- no cartridge writes the calendar chip -- so what
        is in it is always the console's own time, and it is always the right thing to keep.
        """
        try:
            write_rtc_file(self.rtc_path, self.machine.rtc())
        except OSError:
            return False
        return True

    def commit_save(self) -> bool:
        """Persist the cartridge's changed bytes. The save lives IN the ROM: the current
        cart image is written back into the `.ngc` file in place, exactly like the flash
        chip on a real cartridge holds the save. When `sidecar` is on, a standard `.flash`
        block file is ALSO written beside it (backup / portable copy)."""
        if not self.machine.flash_dirty():
            return False
        current = self._read_cart_image()
        if current == self._rom:
            self.machine.flash_clear_dirty()
            return False
        wrote = False
        # 1) into the ROM file itself (atomic replace) -- the cartridge holds its own save
        if self.save_to_rom:
            tmp = self.rom_path.with_suffix(self.rom_path.suffix + ".tmp")
            tmp.write_bytes(current)
            tmp.replace(self.rom_path)
            wrote = True
        # 2) a separate .flash file beside it (standard block format; the full diff vs the
        #    pristine cart, so it stays a usable standalone save even in ROM mode)
        if self.sidecar:
            self.save_path.parent.mkdir(parents=True, exist_ok=True)
            # Per die: `diff_blocks` adds a flat offset to one base, and the second die's
            # bytes do NOT continue from the first one's address -- they start again at
            # 0x800000. Diffing the whole image against CART_BASE would stamp every block
            # above 2 MiB with an address that is not a cartridge at all, and the reload
            # would refuse them.
            blocks: list[tuple[int, bytes]] = []
            offset = 0
            for base, size in self._cart_windows():
                # The pristine image can be SHORTER than the chip (an under-filled cart
                # whose save lives above its ROM) -- pad it with the erased 0xFF a blank
                # chip reads as, or `diff_blocks` walks off the end and reports nothing
                # for exactly the region the save is in.
                pristine = self._orig_rom[offset:offset + size]
                pristine += b"\xFF" * (size - len(pristine))
                blocks += flash_file.diff_blocks(pristine, current[offset:offset + size], base)
                offset += size
            flash_file.write(self.save_path, blocks)
            wrote = True
        if wrote:
            self._rom = current
            self.machine.flash_clear_dirty()
        return wrote

    def _cart_windows(self) -> list[tuple[int, int]]:
        """(base, length) of each flash die, exactly as the core maps them.

        ⚡ THE LENGTH IS THE CHIP'S, NOT THE FILE'S. `auto` presents an under-filled cart
        as 16 Mbit because it has to guess -- but the cartridge then CORRECTS us, by the
        block number it asks the BIOS for. Persisting at the guess writes the .ngc back
        padded out to a size the cart never had, and on the next load that padding reads
        as image: the correction is then refused (a chip is never smaller than its image)
        and every later save is ANDed into a slot nobody erased.

        Measured, three sessions on a 512 KiB cart: session 1 saved, the file grew to
        2 MiB, sessions 2 and 3 wrote garbage. Asking the core what it presents NOW --
        after the cart has spoken -- makes the file exactly one chip, and it stays right.
        """
        size = len(self._rom)
        chip0 = self.machine.flash_capacity(0) or min(size, CART_CHIP_SIZE)
        windows = [(flash_file.CART_BASE, chip0)]
        if size > CART_CHIP_SIZE:
            chip1 = self.machine.flash_capacity(1) or min(size - CART_CHIP_SIZE, CART_CHIP_SIZE)
            windows.append((CART_CHIP1_BASE, chip1))
        return windows

    def _read_cart_image(self) -> bytes:
        """The whole cartridge, in the order the ROM FILE lays it out.

        ⚡ NOT one flat read from 0x200000. A 4 MiB cart is two dies and the second is
        wired to 0x800000, so reading `len(rom)` bytes straight through chip 0 runs off
        its window into space that is not a cartridge and reads back ZEROS. `commit_save`
        did exactly that, compared the result against the ROM file, found it "changed",
        and -- saving into the .ngc, which is the default -- WROTE THE SECOND HALF OF THE
        CARTRIDGE BACK AS ZEROS. Any 4 MiB game that saves would have destroyed its own
        ROM file the first time it did (measured: bytes 2 MiB..4 MiB all zero).
        """
        return b"".join(self.machine.read(base, size) for base, size in self._cart_windows())

    def reboot(self) -> None:
        """POWER OFF, POWER ON. The cartridge never left the slot.

        ⚡ A POWER CYCLE IS NOT A FACTORY RESET, and the two are easy to confuse in an
        emulator because `reset()` reloads the pristine ROM image from disk. On the
        console, NOTHING about the cartridge changes when you switch it off: the flash is
        non-volatile, which is the whole reason a save exists at all. And the console's
        own work RAM is held by a coin cell, which is why the BIOS still knows your
        language afterwards.

        So both are snapshotted across the reset and handed straight back. A reboot that
        quietly wiped the save the player made two minutes ago would be a cruel bug, and
        it is exactly the bug the naive implementation has.
        """
        # ⚡ A 4 MiB CART IS TWO DIES, AND THEY ARE NOT ADJACENT ON THE BUS. Chip 0 sits
        # at 0x200000 and holds at most 2 MiB; chip 1 is wired to 0x800000 (pass 247).
        # This used to snapshot `len(self._rom)` bytes straight through 0x200000, which
        # for the three 4 MiB carts runs off the end of chip 0's window into space that
        # is not a cartridge at all -- and `flash_restore` rightly refused it, so
        # rebooting Metal Slug 2nd Mission, SvC MotM or Densha de Go! 2 raised instead
        # of rebooting. Snapshot each die from where its pins actually are.
        cartridge = [(base, self.machine.read(base, size))
                     for base, size in self._cart_windows()]
        coin_cell = self.machine.battery_ram() if self.real_bios else None
        # The clock is coin-cell state too, and rebooting the console does not reset the
        # date any more than it forgets your language. It has to be carried across by
        # hand: a hand-off reset boots the BIOS internally on a blank cell and that boot
        # stamps 1998-01-01 over the chip (see __init__).
        clock = self.machine.rtc()

        if self.real_bios:
            self.machine.set_battery_ram(coin_cell)   # consulted BY the reset, so first
            self.machine.set_rtc(clock)
            self.machine.reset(real_bios=True)
        else:
            self.machine.reset(bios_handoff=True)
            self.machine.set_rtc(clock)               # after: the warm-up would wipe it
            # ⛔ AND THE CONSOLE'S OWN SETTINGS PAGE, exactly as __init__ does after the
            # same reset. Without it a power cycle quietly forgets the language, and a
            # dual-language SNK cartridge comes back up in JAPANESE.
            #
            # It stayed unnoticed while the only way here was the reset button. Then the
            # link button started power-cycling player 1 (so a game that probes the cable
            # at boot can find its peer) -- and two-player play began showing one window
            # in the chosen language and the other in Japanese, because player 2 was a
            # FRESH session, which does restore the page, and player 1 was a rebooted one,
            # which did not. Reported by a player, diffed byte by byte: 25 bytes of the
            # BIOS page differed between the two consoles, 0x6DC8.. still at 0xFF on the
            # rebooted side.
            self._restore_bios_settings_page()
            self._apply_bios_colour_theme()

        for base, data in cartridge:
            self.machine.flash_restore(base, data)

        self._power_pressed = False
        self.executed = 0
        self._frame_count = 0
        self.stop_status = None
        self.stop_pc = 0

    def handoff_reset(self, coin_cell: bytes, clock: object) -> None:
        """The BIOS -> CARTRIDGE hand-off, with everything non-volatile carried across.

        The console-boot path runs the real BIOS first, then resets into the cartridge's
        entry point to hand it the same clean slate the instant boot gives it. That reset
        is `reset_memory`, and `reset_memory` RELOADS THE PRISTINE CART IMAGE -- so it is
        a factory reset of the flash chip in the middle of a boot. Two things that must
        survive a power-on were being destroyed by it:

          * ⚡ THE GAME'S SAVE. The save is put into the chip in `__init__`, and this
            reset laid the pristine ROM back over it. In "save into the .ngc" mode the
            pristine image IS the file the save was written into, so nothing showed; in
            SEPARATE-FILE mode the .ngc is untouched and the save was wiped at every
            launch -- the .flash sat in `saves/`, was rewritten correctly on exit, and
            never came back. Reported by a player (console boot + separate file),
            reproduced bare: `flash_restore` then `reset(bios_handoff=True)` reads 0xFF.
          * ⚡ THE COIN CELL. The reset seeds a power-on settings page, and handing the
            cell back to the BUFFER afterwards does not touch live RAM -- so
            `commit_system_ram`, which persists the LIVE page, stamped that power-on page
            over the player's console settings on the way out. Measured: a cell holding
            0x11223344 at 0x6DD8 read back all zeros after the hand-off. That is the
            "it resets my BIOS whenever I start a game" half of the same report.

        ⛔ AND THE CELL IS FIXED ON THE *WRITE* SIDE, NOT BY PUTTING IT BACK IN RAM.
        Laying the saved page over live RAM here -- `_restore_bios_settings_page`, which
        is exactly what the INSTANT hand-off does -- FROZE THE GAME. Measured on Bust-A-
        Move Pocket: 109 distinct frames became 8, the title came up without its "push a
        button" line and no input did anything, which is precisely what the player then
        reported. The two paths are not symmetrical: in the instant hand-off that page
        holds a cell SAVED by an earlier session, while here it is the real BIOS's LIVE
        work RAM, scratch and all, and the hand-off reset has just seeded the page the
        cartridge is entitled to. So live RAM keeps what the reset seeded, and
        `commit_system_ram` is told to persist the CAPTURED CELL instead of re-reading
        a page that is no longer the console's answer. See `cell_captured`.

        ⛔ AND THE SAVE IS NOT SNAPSHOTTED FROM LIVE MEMORY, the way `reboot` does it.
        The BIOS has just probed the cartridge, which leaves the chip in AUTOSELECT: it
        answers its ID, and `flash_id_read` gives 0xFF for every address that is not one
        of the four ID bytes. Measured on Bust-A-Move Pocket with the player's own save
        file: the snapshot came back all 0xFF and put THAT back, so the first version of
        this fix wiped the save just as thoroughly as the bug it replaced. Nothing has
        written the chip between `__init__` and here -- only the BIOS ran, and it does
        not save -- so the save FILE is the exact pre-reset state, and re-reading it is
        both simpler and immune to whatever mode the chip is left in.
        """
        was_dirty = self.machine.flash_dirty()
        self.machine.set_battery_ram(b"")   # the game boots on clean work RAM
        self.machine.reset(bios_handoff=True)
        self.machine.set_battery_ram(coin_cell)   # the cell back in the buffer...
        self.system_ram_baseline = coin_cell
        self.machine.set_rtc(clock)
        # ...and it is THIS that gets persisted, rather than the page the reset seeded.
        self.cell_captured = True
        self._restore_save()                      # the cartridge, as the player left it
        if not was_dirty:
            # Putting the save back is not the game writing one: leaving the chip marked
            # dirty would make every launch rewrite the save file it never changed.
            self.machine.flash_clear_dirty()

    # -- running ------------------------------------------------------------

    def run_frames(self, count: int = 1) -> int:
        """Advance `count` whole frames. Returns the number actually completed.

        The frame boundary is the RASTER's, and the raster lives in the core --
        which is why this is one FFI call and not a Python loop guessing at
        instruction counts. Guessing is how a shell ends up re-implementing the
        video clock (CPP_CORE_PORT.md §4, hazard 4).
        """
        before = self._frame_count
        summary = self.machine.run_frames(count)

        # ⚡ THE HALT IS NOT A HANG: IT IS THE CONSOLE SWITCHED OFF.
        #
        # The BIOS boots, arms INT0, and sleeps. INT0 is the POWER BUTTON, and until it
        # is pressed the machine is behaving perfectly -- it is off. We press it once, on
        # the player's behalf, because they already asked for the console to come on by
        # launching the emulator.
        if (self.real_bios and not self._power_pressed
                and summary.stop_status == native.STATUS_HALTED):
            self.machine.raise_irq(INT0_POWER)
            self._power_pressed = True
            summary = self.machine.run_frames(count)

        self.executed += summary.executed
        self._frame_count = summary.frame_count
        if summary.stop_status != native.STATUS_COUNT_REACHED:
            self.stop_status = summary.stop_status
            self.stop_pc = summary.stop_pc
        return self._frame_count - before

    # -- reading ------------------------------------------------------------

    @property
    def frame_count(self) -> int:
        """The core's own frame counter."""
        return self._frame_count

    def video_memory(self) -> dict[int, int]:
        """The window the renderer reads, pulled in ONE crossing."""
        blob = self.machine.read(VIDEO_WINDOW_START, VIDEO_WINDOW_END - VIDEO_WINDOW_START)
        return {VIDEO_WINDOW_START + i: b for i, b in enumerate(blob)}

    def render(self) -> RenderedFrame:
        """Compose the current frame from the core's own video memory.

        The scroll offsets come from the core's RASTER LOG, not from the registers'
        end-of-frame values: the hardware latches them per line, and games rewrite
        them while the beam runs to split the screen or fake parallax.
        """
        return render_frame(self.video_memory(), self.machine.raster_log())

    # The compat palette lives at 0x8380..0x83FF and, for a MONOCHROME cartridge, holds
    # the COLOUR THEME the player picked in the BIOS's own setup screens -- the NGPC's
    # Game Boy Color trick. The BIOS keeps its master copy in battery-backed RAM at
    # 0x6DD8 and blits it across when a dirty flag is set (its routine at 0xFF4FDF:
    # `lda XIY,(0x6DD8) / lda XIX,(0x8380) / ld BC,0x40 / ldirw`).
    #
    # In hand-off mode we never run that code AND never restore the coin cell -- the
    # game deliberately boots on a zeroed slate so the picture is deterministic. So the
    # theme has to be fetched from the saved cell by hand, and ONLY the theme: restoring
    # the whole 12 KiB would put the BIOS's work RAM back under the game and undo that
    # determinism. Without this the core's built-in grey ramp stands and the player's
    # choice -- a green ramp, say -- silently does nothing.
    _THEME_ADDR, _THEME_LEN = 0x006DD8, 0x80

    def _apply_bios_colour_theme(self) -> None:
        # ⚡ NOT ON THE MONO NGP. The theme is a K2GE feature -- that silicon has no
        # 12-bit palette to theme -- and 0x8380 is where `reset_memory` stamps the
        # panel's grey ramp for a mono console instead. Writing an NGPC theme over it
        # put the mono picture back into two tones, and only ONCE THE COIN CELL HAD
        # BEEN CONFIGURED: a fresh install looked right, and every launch after the
        # player had been through Boot BIOS once did not. Same rule as the machine-type
        # bytes above: the console answers for itself.
        if self.k1ge_console:
            return
        try:
            cell = self.ram_path.read_bytes()
        except OSError:
            return
        off = self._THEME_ADDR - native.RAM_START
        theme = cell[off:off + self._THEME_LEN]
        # A console that never completed the BIOS setup leaves this all zero, which as a
        # palette is an all-black screen -- keep the core's grey ramp for that case.
        if len(theme) == self._THEME_LEN and any(theme):
            self.machine.write(0x008380, theme)

    # The BIOS SYSTEM PAGE (0x6C00-0x6FFF) holds the console's own settings: the LANGUAGE
    # and date the player set, plus BIOS scratch. Sibling to _apply_bios_colour_theme, which
    # cherry-picks the theme the same way -- except a game reads the language byte straight
    # from this page, so we restore the whole page rather than blit one field. In hand-off
    # mode nothing else puts the coin cell back, so without this a game reads whatever the
    # power-on default left, and a dual-language SNK cart boots Japanese. Laid back on top of
    # the hand-off EXCEPT the bytes the hand-off itself owns for the inserted cartridge:
    #   0x6C58/0x6C59  the flash card-type the BIOS learnt (0 = "no cart" -> the save fails)
    #   0x6F87         the LANGUAGE, which is now a SETTING and not the cell's to keep
    #   0x6F91/0x6F92  the colour machine-type reset stamps per the console's own mode
    #   0x6FB8-0x6FFF  the user interrupt vector table the hand-off seeds
    # Game work RAM (0x4000-0x6BFF) is untouched, so the boot stays as deterministic as it
    # was: only the OS settings page is restored.
    _SETTINGS_PAGE = (0x006C00, 0x007000)
    # ⚠️ 0x6F87 is in this list because a user reported the language setting doing
    # nothing: the page restore below put the SAVED cell's language back over the value
    # the reset had just stamped from the setting, so whichever language the console was
    # first configured in stayed forever. The cell is still the authority for everything
    # else on this page -- the date, the theme, the BIOS's own scratch.
    _SETTINGS_SKIP = ((0x006C58, 0x006C5A), (0x006F87, 0x006F88),
                      (0x006F91, 0x006F93), (0x006FB8, 0x007000))

    def _cell_with_machine_type(self, cell: bytes) -> bytes:
        """The coin cell as handed to a console BOOT, with the machine-type bytes set
        to THIS console's answer rather than to the one that happened to be saved.

        0x6F91/0x6F92 say which machine the cartridge is in. They are not battery-backed
        settings -- but our console boot reaches the cartridge without the BIOS having
        re-stamped them, so whatever the cell carries is what the game reads. Handed over
        untouched that is the LAST session's console: boot the mono NGP after any NGPC
        session and a colour cartridge (SNK vs. Capcom) still read 0x10 and went on
        believing it was in an NGPC. Blanking them instead was worse -- measured, the
        COLOUR console then answered 0x00 and its games came up monochrome.

        So we stamp the pair `reset_memory` stamps, from the same setting. The hand-off
        path solves this by SKIPPING these bytes when it restores the page
        (`_SETTINGS_SKIP`); this is the console-boot half of the same rule."""
        lo, hi = 0x006F91 - native.RAM_START, 0x006F93 - native.RAM_START
        if len(cell) < hi:
            return cell
        stamp = b"\x00\x00" if self.k1ge_console else b"\x10\x03"
        return cell[:lo] + stamp + cell[hi:]

    def _ui_owns_language(self) -> bool:
        """True when the emulator's language setting is the only control panel there is:
        the HLE image (no setup screen) or a console that was never configured."""
        return self.hle_bios or self.started_unconfigured

    def _settings_skip(self) -> tuple:
        """Which bytes of the settings page the coin cell does NOT get to restore.

        The language is in that list only when the UI owns it. With a real BIOS on a
        configured console the cell is the authority -- that is where its setup screen
        wrote the player's choice, and taking it back would be the emulator overruling
        the console's own control panel."""
        skip = list(self._SETTINGS_SKIP)
        if not self._ui_owns_language():
            skip = [r for r in skip if r != (0x006F87, 0x006F88)]
        return tuple(skip)

    def _restore_bios_settings_page(self) -> None:
        base = self.system_ram_baseline
        if base is None:
            return
        page_start, page_end = self._SETTINGS_PAGE
        addr = page_start
        for skip_start, skip_end in self._settings_skip():
            if addr < skip_start:
                self._write_from_baseline(base, addr, skip_start)
            addr = max(addr, skip_end)
        if addr < page_end:
            self._write_from_baseline(base, addr, page_end)

    def _write_from_baseline(self, base: bytes, start: int, end: int) -> None:
        """Copy [start, end) of the saved coin cell into live RAM (addresses, not offsets)."""
        chunk = base[start - native.RAM_START:end - native.RAM_START]
        if chunk:
            self.machine.write(start, chunk)

    def close(self) -> None:
        # The save is committed BEFORE the machine goes away, and a failure to write it
        # is not something to swallow on the way out of a `with` block.
        if self.autosave:
            # The CARTRIDGE save is this console's own business and is always written --
            # player 2 chose its own cartridge and played it.
            self.commit_save()
            # ...but the COIN CELL is not. A second console borrows the first one's cell
            # to boot with the player's settings; writing it back would mean whichever
            # window was closed last stamped its clock over the other's. Measured before
            # this guard: P1 left 21h on disk, then P2 closed and the file read 02h.
            if not self.second_console:
                self.commit_system_ram()
                self.commit_rtc()
        self.machine.close()

    def __enter__(self) -> "NativeSession":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
