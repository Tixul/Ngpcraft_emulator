"""ROM analysis -- boot a cartridge and report what is wrong with it.

The premise: this core models the machine closely enough to JUDGE a cartridge, not
merely to run it. It knows what the BIOS validates before it will boot a cart, what
the flash chip's block map looks like, which addresses are unmapped, and what a
frame's cycle budget is. All of that is exactly the knowledge a developer needs
applied to their own build, and none of it was reachable without reading the source.

Two passes:

  STATIC   the header the BIOS checks, entry vector, mode byte, image size against
           real flash-chip capacities, how much of the image is erased padding.
           Costs nothing and catches the "it never boots on hardware" class.

  DYNAMIC  boot it in the emulator with the hygiene counters armed and watch. Does
           it leave the BIOS, does it crash, does it read work RAM it never wrote,
           does it write into unmapped space, does it keep up with the frame clock.

Findings are graded. `error` means it is broken or will not boot; `warning` means it
is very likely a bug; `note` is context worth knowing. Nothing here guesses: every
finding names the evidence it is based on.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from core import native
from core.rom import parse_rom_header_bytes

# What the BIOS looks for at the top of the cartridge. A cart whose header does not
# carry one of these is refused by a real console -- this is the single most common
# reason a homebrew ROM runs in an emulator and does nothing on hardware.
VALID_COPYRIGHT = ("COPYRIGHT BY SNK CORPORATION", "LICENSED BY SNK CORPORATION")

CART_BASE = 0x200000
CART_END = 0x3FFFFF
# Real cartridge flash parts. An image that is not one of these sizes is not fatal
# (a flashcart pads it), but it tells you the build is not chip-shaped.
FLASH_SIZES = {0x080000: "4 Mbit", 0x100000: "8 Mbit", 0x200000: "16 Mbit"}

ERROR, WARNING, NOTE = "error", "warning", "note"

# Stop reasons that are OUR shortcoming rather than the cartridge's. Reporting these
# as ROM errors sends a developer hunting a bug that is in the emulator.
_EMULATOR_GAPS = (20, 30)     # unknown-opcode, unimplemented

# -- the robot player ------------------------------------------------------
# Booting a ROM and watching it idle only ever analyses the title screen: the
# gameplay code, the menus and the save path never run, and the report is then
# confidently about a sliver of the cartridge. So the analysis PLAYS a little.
#
# The pattern is deliberately dumb, because a clever one would be tuned to one
# game and lie about the others. It presses A, then Option (start), then B, with
# gaps, and stirs in directions -- enough to get through a title screen, a mode
# select and into a level on most things. Coverage is measured, so whether it
# actually helped is a number rather than a hope.
JOY_UP, JOY_DOWN, JOY_LEFT, JOY_RIGHT = 0x01, 0x02, 0x04, 0x08
JOY_A, JOY_B, JOY_OPTION = 0x10, 0x20, 0x40

# (mask, frames-held). A button must be HELD for several frames and released for
# several more: games poll once per frame and act on the EDGE, so a single-frame
# tap can fall between two polls and a permanently-held button is read as one press.
ROBOT_SCRIPT: tuple[tuple[int, int], ...] = (
    (0, 24),                 # let it boot and show something
    (JOY_A, 6), (0, 10),
    (JOY_OPTION, 6), (0, 10),
    (JOY_A, 6), (0, 20),
    (JOY_DOWN, 4), (0, 4), (JOY_A, 6), (0, 14),
    (JOY_B, 6), (0, 10),
    (JOY_RIGHT, 8), (JOY_A, 6), (0, 8),
    (JOY_LEFT, 8), (JOY_A, 6), (0, 8),
    (JOY_UP, 4), (0, 4), (JOY_A, 6), (0, 20),
    (JOY_OPTION, 6), (0, 30),
)
JOY_PORT = 0x00B0


def _robot_frame(frame: int) -> int:
    """The joypad byte for this frame: the script above, looped."""
    total = sum(hold for _mask, hold in ROBOT_SCRIPT)
    if total <= 0:
        return 0
    t = frame % total
    for mask, hold in ROBOT_SCRIPT:
        if t < hold:
            return mask
        t -= hold
    return 0


@dataclass
class Finding:
    level: str
    title: str
    detail: str = ""
    where: str = ""          # an address or PC, when there is one

    def __str__(self) -> str:
        mark = {ERROR: "ERROR  ", WARNING: "WARNING", NOTE: "note   "}[self.level]
        line = f"{mark}  {self.title}"
        if self.where:
            line += f"  [{self.where}]"
        if self.detail:
            line += f"\n         {self.detail}"
        return line


@dataclass
class Report:
    path: Path
    findings: list[Finding] = field(default_factory=list)
    facts: dict = field(default_factory=dict)

    def add(self, level: str, title: str, detail: str = "", where: str = "") -> None:
        self.findings.append(Finding(level, title, detail, where))

    @property
    def errors(self) -> list[Finding]:
        return [f for f in self.findings if f.level == ERROR]

    @property
    def warnings(self) -> list[Finding]:
        return [f for f in self.findings if f.level == WARNING]

    def text(self) -> str:
        out = [f"ROM analysis — {self.path.name}", "=" * 60, ""]
        for key, value in self.facts.items():
            out.append(f"  {key:<22} {value}")
        out.append("")
        if not self.findings:
            out.append("No problems found.")
        else:
            n_e, n_w = len(self.errors), len(self.warnings)
            out.append(f"{n_e} error(s), {n_w} warning(s), "
                       f"{len(self.findings) - n_e - n_w} note(s)")
            out.append("")
            for level in (ERROR, WARNING, NOTE):
                for f in self.findings:
                    if f.level == level:
                        out.append(str(f))
        return "\n".join(out)


def _sym(symbols, pc: int) -> str:
    """'player_init+2E (2694D7)' when a map is loaded, else the bare address.

    Naming the code is most of the value of a finding: without it every report ends
    with the reader disassembling addresses by hand.
    """
    if symbols is not None:
        try:
            hit = symbols.lookup_address(pc)
        except Exception:
            hit = None
        if hit is not None:
            delta = pc - hit.address
            name = hit.name if delta == 0 else f"{hit.name}+{delta:X}"
            return f"{name} ({pc:06X})"
    return f"PC {pc:06X}"


def _first_writes(machine, addrs: set[int], frames: int = 90) -> dict[int, tuple[int, int]]:
    """Re-run from a fresh power-on and record, for each address, the frame and PC of
    the FIRST write to it.

    A second run rather than instrumenting the first: the write log is one window and
    the first pass needs it free for the other checks, and a fresh boot is exactly the
    same deterministic sequence -- verified across runs.
    """
    if not addrs:
        return {}
    lo, hi = min(addrs), max(addrs)
    out: dict[int, tuple[int, int]] = {}
    try:
        machine.reset(bios_handoff=True, real_bios=False)
        machine.set_cart_wait(3); machine.set_cart_data_wait(0); machine.set_ldir_cost(14)
        machine.set_write_log(lo, hi)
        for frame in range(frames):
            machine.write(JOY_PORT, bytes([_robot_frame(frame) & 0x7F]))
            machine.run_frames(1)
            if machine.write_log_count():
                for rec in machine.write_log(2048):
                    if rec.addr in addrs and rec.addr not in out:
                        out[rec.addr] = (frame, rec.pc)
                machine.set_write_log(lo, hi)      # consume, keep only firsts
            if len(out) == len(addrs):
                break
        machine.set_write_log(1, 0)
    except Exception:
        return out
    return out


# ------------------------------------------------------------------ static
def analyse_static(data: bytes, path: Path, report: Report) -> None:
    size = len(data)
    report.facts["file size"] = f"{size} bytes ({size / 1024:.0f} KiB)"

    if size < 0x40:
        report.add(ERROR, "Image is too small to hold a cartridge header",
                   f"{size} bytes; the header alone is 0x40.")
        return

    try:
        head = parse_rom_header_bytes(data, path)
    except ValueError as exc:
        report.add(ERROR, "Header could not be parsed", str(exc))
        return

    report.facts["title"] = head.title or "(blank)"
    report.facts["game id"] = head.game_id_bcd
    report.facts["version"] = head.version
    report.facts["mode"] = head.mode_name
    report.facts["entry point"] = f"{head.entry_point:06X}"
    report.facts["copyright"] = head.copyright_text or "(blank)"

    # -- the check that decides whether hardware will boot it at all
    if head.copyright_text.strip() not in VALID_COPYRIGHT:
        report.add(ERROR, "Header copyright string is not one the BIOS accepts",
                   "A real console checks this before it will run a cartridge. Expected "
                   f"one of {VALID_COPYRIGHT!r}, found {head.copyright_text.strip()!r}. "
                   "The ROM may still run in an emulator that skips the check.",
                   where=f"{CART_BASE:06X}")

    # -- the entry vector
    entry = head.entry_point
    if entry == 0:
        report.add(ERROR, "Entry vector is zero",
                   "The BIOS jumps to the 24-bit vector at 0x20001C after validating the "
                   "cart. Zero means it jumps into the I/O page.", where="20001C")
    elif not (CART_BASE <= entry <= CART_END):
        report.add(ERROR, "Entry vector points outside the cartridge window",
                   f"{entry:06X} is not in {CART_BASE:06X}..{CART_END:06X}.",
                   where="20001C")
    elif entry - CART_BASE >= size:
        report.add(ERROR, "Entry vector points past the end of the image",
                   f"{entry:06X} is {entry - CART_BASE - size} bytes beyond the last byte "
                   "of the file, so it lands in erased flash.", where="20001C")
    else:
        # An entry that lands on erased flash is a linker/section mistake, and it is
        # the exact shape of the "const at the entry address" trap: the vector points
        # at data, the CPU executes 0xFF, and the console dies on boot.
        at_entry = data[entry - CART_BASE: entry - CART_BASE + 4]
        if at_entry and all(b == 0xFF for b in at_entry):
            report.add(ERROR, "Entry vector points at erased flash (0xFF)",
                       "The first bytes of the entry point are all 0xFF, which is what an "
                       "unprogrammed chip reads. Usually a section that did not get placed "
                       "where the vector says it is.", where=f"{entry:06X}")
        elif at_entry and all(b == 0x00 for b in at_entry):
            report.add(WARNING, "Entry vector points at zeroed bytes",
                       "Four zero bytes at the entry point. Executable code is possible "
                       "but unlikely here.", where=f"{entry:06X}")

    if head.mode_raw not in (0x00, 0x10):
        report.add(WARNING, "Cartridge mode byte is neither mono (0x00) nor colour (0x10)",
                   f"Found 0x{head.mode_raw:02X}.", where="200023")

    if not head.title.strip():
        report.add(NOTE, "Cartridge title field is blank",
                   "Cosmetic, but it is what the BIOS and front-ends display.",
                   where="200024")

    # -- image size against real chips
    if size > 0x400000:
        report.add(ERROR, "Image is larger than the biggest addressable cartridge",
                   f"{size} bytes; a two-die 32 Mbit cart tops out at 0x400000.")
    elif size not in FLASH_SIZES and size < 0x200000:
        nearest = min((s for s in FLASH_SIZES if s >= size), default=0x200000)
        report.add(NOTE, "Image size is not a flash-chip size",
                   f"{size} bytes. A cart carries a {FLASH_SIZES.get(nearest, '16 Mbit')} "
                   "part, and the rest reads as erased 0xFF -- which is fine, but any "
                   "save must be programmed into a block that actually exists.")

    # -- how much is real content
    tail = len(data)
    while tail > 0 and data[tail - 1] in (0xFF, 0x00):
        tail -= 1
    report.facts["last non-blank byte"] = f"{CART_BASE + tail - 1:06X}" if tail else "(none)"
    if tail == 0:
        report.add(ERROR, "Image is entirely blank", "Every byte is 0x00 or 0xFF.")


# ----------------------------------------------------------------- dynamic
def analyse_dynamic(rom: Path, report: Report, bios: Path | None = None,
                    frames: int = 600, play: bool = True,
                    symbols=None) -> None:
    """Boot the ROM with the hygiene counters armed and watch what it does.

    `play=True` drives the joypad from ROBOT_SCRIPT so the analysis gets past the
    title screen into real code; coverage is reported either way so the difference
    is visible.
    """
    try:
        machine = native.NativeMachine(rom.read_bytes(),
                                       bios=bios.read_bytes() if bios else None)
    except Exception as exc:
        report.add(ERROR, "The core could not load this image", f"{type(exc).__name__}: {exc}")
        return

    try:
        # ⚠️ HAND-OFF, never the real BIOS boot. Booting the BIOS for real would spend
        # the whole analysis window inside SNK's code: the counters then measure the
        # BIOS's own RAM habits, every ROM scores identically, and the report is
        # confidently about the wrong program. (That is exactly what the first version
        # of this did -- two completely different carts produced the same numbers.)
        # Hand-off gives the cartridge a running console at its entry vector, which is
        # the state we want to judge. The BIOS image is still loaded so `swi` calls work.
        machine.reset(bios_handoff=True, real_bios=False)
        # Armed AFTER the reset: reset writes RAM itself, and those are not the ROM's
        # reads or writes.
        machine.set_hygiene(True)
        machine.set_callstack(True)
        machine.set_coverage(True)

        # Model the slow cartridge flash, or the frame-budget verdict is meaningless:
        # without wait-states the CPU runs cart code roughly 3.4x too fast and every
        # ROM looks comfortably inside its budget. Silicon-calibrated values, the same
        # ones the player uses (see ngpc_settings.CART_FETCH_WAIT and friends).
        try:
            machine.set_cart_wait(3)
            machine.set_cart_data_wait(0)
            machine.set_ldir_cost(14)
        except Exception:
            pass

        total_instr = 0
        ran_frames = 0
        crashed = None
        pcs = []
        # The lowest stack pointer the run ever reached. Everything at or above it is
        # STACK; everything below is globals. The two produce the same raw signal --
        # "read before written" -- but they are not the same finding: an uninitialised
        # global is wrong for the life of the program, while a stack byte is only ever
        # a local that a function read before assigning, in one call, and is much
        # weaker evidence. Reporting them in one undifferentiated list would bury the
        # strong findings under the weak ones.
        stack_floor = 0x006C00
        for frame in range(frames):
            if play:
                # Written EVERY frame, before the frame runs: the joypad register is
                # polled by the game once per frame, so a press that is not standing
                # there when it looks is a press that never happened.
                machine.write(JOY_PORT, bytes([_robot_frame(frame) & 0x7F]))
            summ = machine.run_frames(1)
            total_instr += int(summ.executed)
            ran_frames += 1
            # ⚠️ Sample the LIVE PC. `stop_pc` is only meaningful when the run actually
            # stopped on something (a breakpoint or a fault); on a normal frame it is
            # stale, and reading it here reported "never in cart space" for a ROM whose
            # PC was plainly in cart space -- a confident, wrong ERROR.
            cpu = machine.cpu()
            pcs.append(cpu.pc)
            sp = cpu.regs[7] & 0xFFFFFF
            if 0x004000 <= sp <= 0x006C00:
                stack_floor = min(stack_floor, sp)
            if summ.stop_status in (native.STATUS_OK, native.STATUS_HALTED,
                                    native.STATUS_COUNT_REACHED):
                continue
            if summ.stop_status == native.STATUS_BREAKPOINT:
                continue
            crashed = (summ.stop_status, int(summ.stop_pc), int(summ.stop_opcode))
            break

        pc = machine.cpu().pc
        report.facts["frames run"] = ran_frames
        report.facts["input driven"] = "yes (robot player)" if play else "no (idle boot)"
        report.facts["instructions"] = total_instr
        report.facts["final PC"] = f"{pc:06X}"
        covered = machine.coverage_hits()
        report.facts["code reached"] = f"{covered} distinct instruction addresses"

        if crashed:
            status, cpc, opcode = crashed
            # ⚠️ NOT EVERY STOP IS THE ROM'S FAULT. "unknown-opcode" and "unimplemented"
            # mean THIS EMULATOR ran out of instruction set, which says nothing about the
            # cartridge -- and reporting it as a ROM error is a lie that sends the reader
            # hunting a bug in their game. (Card Fighters Clash 2 Expand Edition stops
            # this way, identically, at the same address, in two independent dumps: that
            # is our gap, not theirs.) Named as what it is, and pointed at us.
            if status in _EMULATOR_GAPS:
                report.add(WARNING,
                           f"This emulator cannot run all of this ROM: "
                           f"{native.status_name(status)}",
                           f"Opcode 0x{opcode:02X} at {cpc:06X} is not implemented in our "
                           "core, so execution stopped there. This is a gap in the "
                           "EMULATOR, not a defect in the cartridge — but everything past "
                           "this point is unanalysed.",
                           where=f"{cpc:06X}")
            else:
                report.add(ERROR, f"The CPU stopped: {native.status_name(status)}",
                           f"Opcode 0x{opcode:02X}. Everything after this point is "
                           "unreachable.", where=f"{cpc:06X}")

        # -- is it running its OWN code? Started at the entry vector, a game should be
        # in cart space most of the time; sitting in BIOS or RAM means it jumped away
        # and did not come back.
        in_cart = sum(1 for p in pcs if CART_BASE <= p <= CART_END)
        report.facts["PC in cart"] = f"{in_cart}/{len(pcs)} frames"
        # ⚠️ The frame-end PC alone cannot decide this. A game that waits for VBlank
        # inside a BIOS routine is sampled in BIOS on EVERY frame while running cart
        # code the whole time in between -- NEOTYPE executed 1140 distinct cartridge
        # addresses and still scored 0/240 here, and was called dead. Coverage is the
        # honest test of "did the cartridge run"; the sample only says where it idles.
        if covered == 0:
            report.add(ERROR, "No cartridge code ever executed",
                       "Not one instruction ran in the cart window. The program never "
                       "reached the game, so nothing below has been exercised.")
        elif in_cart == 0:
            report.add(NOTE, "The game idles outside cart code",
                       f"{covered} cartridge addresses executed, but every frame ENDED "
                       "with the PC in BIOS or RAM — typically a BIOS vblank wait. Normal; "
                       "noted because it makes the frame-end sample uninformative.")
        elif in_cart < len(pcs) // 2:
            report.add(NOTE, "Much of the time is spent outside cart code",
                       f"Only {in_cart} of {len(pcs)} frame-end samples were in the "
                       "cartridge; the rest were BIOS calls or code copied to RAM.")

        # -- did it do anything at all?
        if total_instr < ran_frames * 10:
            report.add(ERROR, "The CPU is barely executing",
                       f"{total_instr} instructions over {ran_frames} frames. A running "
                       "game is tens of thousands per frame; this is a hang.")

        # -- reading RAM it never wrote
        uninit = machine.uninit_reads()
        if uninit:
            samples = machine.uninit_read_samples(64)
            # WHEN each address is finally written is the thing that makes a finding
            # triageable. "Read once, written by the same instruction" is a counter
            # being incremented from nothing -- usually harmless. "Read 7 times, not
            # written until frame 7" is a flag being polled before it exists, which is
            # a real window of wrong behaviour. Without this the two look identical.
            addrs = {s.addr for s in samples}
            first_write = _first_writes(machine, addrs, frames=90)

            def describe(addr: int) -> str:
                readers = sorted({s.pc for s in samples if s.addr == addr})
                n = sum(1 for s in samples if s.addr == addr)
                fw = first_write.get(addr)
                when = (f"first written on frame {fw[0]} by {_sym(symbols, fw[1])}"
                        if fw else "never written during the run")
                return (f"{addr:06X} read {n}x by "
                        + ", ".join(_sym(symbols, p) for p in readers[:3])
                        + f" — {when}")

            globals_ = sorted(a for a in addrs if a < stack_floor)
            stacked = sorted(a for a in addrs if a >= stack_floor)
            report.facts["stack low-water mark"] = f"{stack_floor:06X}"

            if globals_:
                report.add(WARNING,
                           f"{len(globals_)} global variable(s) read before being written",
                           "Work RAM holds whatever the previous game left, so these return "
                           "the last game's data on hardware while reading zero on most "
                           "emulators. The frame of the first write is the thing to look at: "
                           "written by the same instruction that read it is usually a "
                           "counter and harmless; written much later, or never, means real "
                           "code ran on a value that did not exist yet.\n         "
                           + "\n         ".join(describe(a) for a in globals_[:10]))
            if stacked:
                report.add(NOTE,
                           f"{len(stacked)} stack byte(s) read before being written",
                           "At or above the stack low-water mark, so these are local "
                           "variables read before assignment rather than globals. Weaker "
                           "evidence — one function, one call — but still undefined "
                           "behaviour.\n         "
                           + "\n         ".join(describe(a) for a in stacked[:6]))
        report.facts["uninitialised reads"] = uninit

        # -- writes into nowhere
        lost = machine.lost_writes()
        if lost:
            samples = machine.lost_write_samples(8)
            where = ", ".join(f"{s.addr:06X} by {_sym(symbols, s.pc)}" for s in samples[:4])
            report.add(WARNING, f"{lost} writes into unmapped space",
                       "The bus discards these and the program never finds out, so the "
                       "store simply does not happen. Worth checking in your own code; "
                       "note that several commercial carts do it too, from what looks "
                       "like one shared SDK routine (Super Real Mahjong and KOF Battle de "
                       "Paradise write from byte-identical code), and in every case "
                       "measured nothing ever reads the address back — so it is only a "
                       "real defect if something depends on the value.\n         "
                       f"First: {where}")
        report.facts["lost writes"] = lost

        # -- frame budget. The console has a fixed number of cycles per frame; a game
        # that needs more is not "slow on this emulator", it is slow on hardware too.
        if ran_frames:
            per_frame = total_instr / ran_frames
            report.facts["instructions/frame"] = f"{per_frame:.0f}"
            # With the cart wait-states above in force, a frame of 102 485 cycles buys
            # roughly 7 500 fetch-bound instructions. Well past that and the game cannot
            # be finishing its work inside one frame ON HARDWARE, whatever the host does.
            if per_frame > 12000:
                report.add(WARNING, "The frame budget is being exceeded",
                           f"{per_frame:.0f} instructions per frame, against roughly "
                           "7 500 affordable with real cartridge timing. On hardware this "
                           "game is not completing its work in one frame.")
            elif per_frame > 7500:
                report.add(NOTE, "Close to the frame budget",
                           f"{per_frame:.0f} instructions per frame; about 7 500 fit in "
                           "a frame with real cartridge timing.")

        # -- did it set the screen up?
        try:
            regs = machine.read(0x008000, 0x40)
            if all(b == 0 for b in regs):
                report.add(WARNING, "The video registers were never programmed",
                           "All of 0x8000..0x803F is still zero, so nothing was told to "
                           "display. A game that draws sets these.")
        except Exception:
            pass
    finally:
        try:
            machine.set_hygiene(False)
            machine.set_callstack(False)
            machine.close()
        except Exception:
            pass


def _load_symbols_for(rom: Path):
    """The toolchain map beside the ROM, if there is one. Findings that name a
    function instead of an address are worth several times as much."""
    from core.symbols import load_map
    for cand in (rom.with_suffix(".map"), Path(str(rom) + ".map")):
        if cand.is_file():
            try:
                return load_map(str(cand))
            except (OSError, ValueError):
                return None
    return None


def analyse(rom: str | Path, bios: str | Path | None = None,
            frames: int = 600, run: bool = True, play: bool = True,
            symbols=None) -> Report:
    """Full analysis. `run=False` does the static half only (instant, no core).

    `play=True` drives the joypad so the analysis reaches past the title screen.
    """
    path = Path(rom)
    report = Report(path=path)
    try:
        data = path.read_bytes()
    except OSError as exc:
        report.add(ERROR, "Could not read the file", str(exc))
        return report

    analyse_static(data, path, report)
    if run:
        bios_path = Path(bios) if bios else None
        if bios_path is not None and not bios_path.is_file():
            bios_path = None
        if symbols is None:
            symbols = _load_symbols_for(path)
        report.facts["symbols"] = f"{len(symbols)} loaded" if symbols else "none"
        analyse_dynamic(path, report, bios_path, frames, play=play, symbols=symbols)
    return report
