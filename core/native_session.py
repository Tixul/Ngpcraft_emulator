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
from pathlib import Path

from core import flash_file, native
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
CLOCK_HARDWARE = "hardware"
CLOCK_HOST = "host"
CLOCK_PAUSED = "paused"
CLOCK_MODES = (CLOCK_HARDWARE, CLOCK_HOST, CLOCK_PAUSED)

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


def apply_saved_clock(machine, path: Path, mode: str) -> None:
    """Put the console's clock back, per the chosen mode. One place, so the game path,
    the BIOS-only path and any reboot all behave identically."""
    if mode == CLOCK_HOST:
        machine.set_rtc(host_clock_state())
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
        bios_path: str | Path | None = None,
        save_path: str | Path | None = None,
        autosave: bool = True,
        save_to_rom: bool = True,
        sidecar: bool = False,
        flash_size: int = 0,
        real_bios: bool = False,
        clock_mode: str = CLOCK_HARDWARE,
    ):
        if not native.available():
            raise RuntimeError(
                "the native core is not built. `cmake --build cpp/build` first."
            )
        self.rom_path = Path(rom_path)
        self._rom = self.rom_path.read_bytes()
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
        self.clock_mode = clock_mode if clock_mode in CLOCK_MODES else CLOCK_HARDWARE
        if self.real_bios:
            if self.ram_path.exists():
                self.system_ram_baseline = self.ram_path.read_bytes()
                self.machine.set_battery_ram(self.system_ram_baseline)
            # BEFORE the reset, like the RAM: the BIOS reads the chip during its own boot.
            # With a configured cell it leaves what it finds; with a blank one it resets
            # the date to 1998-01-01 itself, which is the real dead-battery behaviour.
            apply_saved_clock(self.machine, self.rtc_path, self.clock_mode)
            self.machine.reset(real_bios=True)
        else:
            self.machine.reset(bios_handoff=True)
            # ⚡ AFTER the reset here, and that order is load-bearing. The hand-off reset
            # BOOTS THE REAL BIOS internally to capture the character RAM it leaves behind,
            # and it does so on a blank coin cell -- so the BIOS takes that boot's
            # dead-battery path and stamps 1998-01-01 over the chip. Restoring before the
            # reset would hand the player's clock straight to that warm-up to be wiped.
            apply_saved_clock(self.machine, self.rtc_path, self.clock_mode)

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
        self._flash_presented = min(len(self._rom), CART_CHIP_SIZE)
        if flash_size and flash_size != self._flash_presented:
            self.machine.set_flash_size(flash_size)
            # The BIOS reads the card type BEFORE it touches the chip, and `reset` wrote it
            # from the pre-resize map -- so it has to be restated, or the byte and the block
            # map disagree about which card this is.
            self.machine.write(BIOS_FLASH_CARD_TYPE, bytes([flash_size_code(flash_size)]))
        if flash_size and flash_size > len(self._rom):
            self._rom = self._orig_rom = bytes(self.machine.read(flash_file.CART_BASE, flash_size))

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
        """The coin cell. Whatever the BIOS learnt -- your language, the date -- lives here.

        Only in real-BIOS mode: in the hand-off the BIOS never ran, so its work RAM holds
        nothing it wrote and saving it would be inventing settings the console never had.

        ⚡ We persist the coin cell AS IT WAS CONFIGURED, not the machine's live work RAM.
        Once a game boots, work RAM is the GAME's -- its variables, not console settings --
        and the game-boot hand-off deliberately hands the cart a clean slate anyway. Writing
        that back would wipe the language/date the player set. A game never reconfigures the
        console, so the right thing to persist is the baseline we loaded. (The console is
        (re)configured through Boot BIOS, which saves system.ram on its own path.)
        """
        if not self.real_bios or self.system_ram_baseline is None:
            return False
        self.ram_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.ram_path.with_suffix(".tmp")
        tmp.write_bytes(self.system_ram_baseline)
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
                blocks += flash_file.diff_blocks(
                    self._orig_rom[offset:offset + size], current[offset:offset + size], base)
                offset += size
            flash_file.write(self.save_path, blocks)
            wrote = True
        if wrote:
            self._rom = current
            self.machine.flash_clear_dirty()
        return wrote

    def _cart_windows(self) -> list[tuple[int, int]]:
        """(base, length) of each flash die, exactly as the core maps them."""
        size = len(self._rom)
        chip0 = min(size, CART_CHIP_SIZE)
        windows = [(flash_file.CART_BASE, chip0)]
        if size > CART_CHIP_SIZE:
            windows.append((CART_CHIP1_BASE, min(size - CART_CHIP_SIZE, CART_CHIP_SIZE)))
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

        for base, data in cartridge:
            self.machine.flash_restore(base, data)

        self._power_pressed = False
        self.executed = 0
        self._frame_count = 0
        self.stop_status = None
        self.stop_pc = 0

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

    def close(self) -> None:
        # The save is committed BEFORE the machine goes away, and a failure to write it
        # is not something to swallow on the way out of a `with` block.
        if self.autosave:
            self.commit_save()
            self.commit_system_ram()
            self.commit_rtc()
        self.machine.close()

    def __enter__(self) -> "NativeSession":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
