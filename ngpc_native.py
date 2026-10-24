"""Headless CLI for the NATIVE core — the emulator an agent can actually drive.

`ngpc_emu.py` inspects: it opens a ROM or a save state and reports what is in it.
This runs the machine. Load the state a player captured one frame before a bug, hold
a button, advance frames, and look at what came out -- the same loop a person does by
hand, available to a tool.

    python ngpc_native.py run GAME.ngc --state bug.s0 --hold A --frames 30 --shot out.png

Everything is one command so one tool call answers "what happens if I press A here?".
Output is JSON on stdout with --json, so a caller never has to parse prose.

Two things it reports that are easy to get wrong elsewhere. It runs with the
SILICON-CALIBRATED cart wait-states on (`--timing free` opts out and says so in the
output), because the raw core defaults to free instruction fetch and a machine measured
that way is ~2.9x too fast. And every run carries an `hw_safety` block -- a starved
watchdog or a stack that crossed into the BIOS page, counted while the ROM runs on.
"""

from __future__ import annotations

import argparse
import ctypes
import json
import sys
from pathlib import Path

from core import native, rom_loader

# Same fallback as the shell (ngpc_shell._resolve_bios). With NO image the vector table
# and the system-call table at 0xFFFE00 read 0: a homebrew's first BIOS call (cc900
# templates call VECT_SYSFONTSET before anything else) jumps to address 0 two frames in.
HLE_BIOS = Path(__file__).resolve().parent / "hle_bios" / "bios_hle.bin"

# The player's save state (ngpc_shell.py, F2): magic, the CpuState struct, the
# sound/timer block (v2+), the link-cable block (v3+), then the working image from
# 0x0000. Same contract as core.savestate.load_shell_savestate.
#
# ⛔ THIS FILE IS THE SECOND READER OF A FORMAT IT DOES NOT WRITE, and that is exactly
# how it broke: v3 shipped in the shell and here nothing changed, so `--state` REFUSED
# every state a player produces -- "is not a NGPCST02 save state" -- while the shell had
# already stopped writing v2. A generation added at one end and not the other is not a
# degraded read, it is a door that closes. Whatever the shell's STATE_MAGIC becomes, it
# must appear below on the same day.
SHELL_MAGIC = b"NGPCST04"       # v4: + l'horloge calendaire (etat machine, pas memoire)
SHELL_MAGIC_V3 = b"NGPCST03"    # v3: + the link cable (FIFOs, shifter, CTS/RTS pins)
SHELL_MAGIC_V2 = b"NGPCST02"    # v2: carries the sound CPU, the T6W28 and the timers
SHELL_MAGIC_V1 = b"NGPCST01"    # v1: CPU + memory only -- loads, but the sound will not
SHELL_MAGICS = (SHELL_MAGIC, SHELL_MAGIC_V3, SHELL_MAGIC_V2, SHELL_MAGIC_V1)
SHELL_MEM_LEN = 0x00C000

# SILICON TIMING. The core's fields default to free cart fetch (back-compat, see
# Machine::cart_wait) and the desktop shell switches these on for every ROM it loads.
# A headless run has no QSettings to read, so the same calibrated set is spelled out
# here -- keep it equal to CART_FETCH_WAIT / CART_DATA_WAIT / CART_LDIR_COST /
# CART_LDIRW_COST in ngpc_settings.py. Without them cart code runs ~2.9x too fast and any
# optimisation that only shortens the code measures as exactly zero.
SILICON_TIMING = {"word_wait": 10, "bios_wait": 8}

# The controller, as the hardware reports it at 0x00B0.
BUTTONS = {
    "UP": 0x01, "DOWN": 0x02, "LEFT": 0x04, "RIGHT": 0x08,
    "A": 0x10, "B": 0x20, "OPTION": 0x40,
}


def parse_buttons(spec: str | None) -> int:
    if not spec:
        return 0
    held = 0
    for name in spec.replace(",", "+").split("+"):
        key = name.strip().upper()
        if not key:
            continue
        if key not in BUTTONS:
            raise SystemExit(f"unknown button {name!r}; known: {', '.join(BUTTONS)}")
        held |= BUTTONS[key]
    return held


def load_state(machine: native.NativeMachine, path: Path) -> None:
    """Restore a player save state into a running machine."""
    blob = path.read_bytes()
    magic = blob[:len(SHELL_MAGIC)]
    if magic not in SHELL_MAGICS:
        raise SystemExit(
            f"{path} carries no known save-state magic: {magic!r} "
            f"(known: {', '.join(m.decode() for m in SHELL_MAGICS)})")
    body = blob[len(magic):]
    cpu_t = type(machine.cpu())
    cpu_len = ctypes.sizeof(cpu_t)
    aux_len = (ctypes.sizeof(native.AuxState)
               if magic in (SHELL_MAGIC, SHELL_MAGIC_V3, SHELL_MAGIC_V2) else 0)
    link_len = (ctypes.sizeof(native.LinkState)
                if magic in (SHELL_MAGIC, SHELL_MAGIC_V3) else 0)
    # ⚡ v4 : l'horloge, en TETE. Sans elle le rejeu diverge d'un octet a 0x000096.
    rtc_len = ctypes.sizeof(native.RtcState) if magic == SHELL_MAGIC else 0
    head = rtc_len + cpu_len + aux_len + link_len
    if len(body) != head + SHELL_MEM_LEN:
        raise SystemExit(
            f"{path}: expected {head + SHELL_MEM_LEN} bytes after the magic "
            f"({cpu_len} of CPU + {aux_len} of sound/timer + {link_len} of link cable "
            f"+ {SHELL_MEM_LEN} of memory), got {len(body)}"
        )
    machine.write(0, body[head:])
    if rtc_len:
        machine.set_rtc(native.RtcState.from_buffer_copy(body[:rtc_len]))
    body = body[rtc_len:]
    machine.set_cpu(cpu_t.from_buffer_copy(body[:cpu_len]))
    # AFTER the image: writing it goes through the control registers, and 0x00BA is a
    # door ("fire one NMI at the sound CPU"), not storage. This is what cancels that.
    if aux_len:
        machine.set_aux_state(
            native.AuxState.from_buffer_copy(body[cpu_len:cpu_len + aux_len]))
    # ...and the cable after the image for the same shape of reason: SC0MOD/BR0CR live
    # in the image and decide how fast a byte shifts.
    if link_len:
        machine.set_link_state(native.LinkState.from_buffer_copy(
            body[cpu_len + aux_len:cpu_len + aux_len + link_len]))


def write_png(machine: native.NativeMachine, path: Path) -> None:
    """The frame as the core drew it, line by line, as the beam passed."""
    fb = machine.framebuffer()
    rows = bytearray()
    for px in fb:                                   # 12-bit 0BGR -> 8-bit RGB
        rows += bytes((((px >> 0) & 0xF) * 17, ((px >> 4) & 0xF) * 17, ((px >> 8) & 0xF) * 17))
    ppm = b"P6\n%d %d\n255\n" % (native.SCREEN_W, native.SCREEN_H) + bytes(rows)
    if path.suffix.lower() == ".ppm":
        path.write_bytes(ppm)
        return
    try:
        from PIL import Image                       # optional: PPM always works
    except ImportError:
        alt = path.with_suffix(".ppm")
        alt.write_bytes(ppm)
        raise SystemExit(f"Pillow not installed; wrote {alt} instead of {path}")
    import io
    Image.open(io.BytesIO(ppm)).save(path)


def apply_timing(machine: native.NativeMachine, mode: str) -> None:
    """Cart-flash wait-states: `silicon` (calibrated) or `free` (the raw core default).

    `free` exists to reproduce a measurement taken before wait-states, and for nothing
    else -- it is not a claim about hardware.
    """
    if mode != "silicon":
        return
    machine.set_timing_silicon(SILICON_TIMING["word_wait"],
                               SILICON_TIMING["bios_wait"])


def hw_report(machine: native.NativeMachine, limit: int = 8) -> dict:
    """The two hardware-safety findings: a starved watchdog, and a stack that crossed
    into the BIOS's own page. Counted, never fatal -- a real console does not stop at
    the instruction that commits them, so neither does this."""
    counts = {name: machine.hw_violations(bit)
              for bit, name in native.HW_KINDS.items()}
    out: dict = {"counts": counts, "clean": not any(counts.values())}
    if not out["clean"]:
        out["first"] = [{"kind": v.kind_name, "pc": v.pc, "detail": v.detail,
                         "cycle": v.cycle}
                        for v in machine.hw_violation_samples(limit)]
    return out


def hygiene_report(machine: native.NativeMachine, *, armed: bool, rom: Path | None = None,
                   limit: int = 8) -> dict:
    """User-RAM bytes (0x4000..0x6BFF) the ROM READ before ever writing them.

    Each is a value that is 0 in this core and garbage on a console: the classic
    "works in the emulator, frozen on hardware". Distinct addresses with the first
    reader's PC; `core/romcheck.py` gives the full triage (who writes it, and when).
    """
    if not armed:
        return {"armed": False, "note": "not armed when starting from a save state"}
    samples = machine.uninit_read_samples()
    first: dict[int, int] = {}
    for s in samples:
        first.setdefault(s.addr, s.pc)
    out: dict = {"armed": True, "uninit_reads": machine.uninit_reads(),
                 "uninit_addresses": len(first), "clean": not first}
    if first:
        # Named from the ROM's .map when one sits beside it -- same lookup as romcheck.
        symbols = None
        if rom is not None:
            from core import romcheck
            symbols = romcheck._load_symbols_for(rom)
            name_ram, name_pc = romcheck._ram_name, romcheck._sym
        out["first"] = [
            {"addr": f"0x{a:06X}", "pc": f"0x{pc:06X}",
             **({"variable": name_ram(symbols, a), "reader": name_pc(symbols, pc)}
                if symbols is not None else {})}
            for a, pc in list(first.items())[:limit]]
        out["note"] = ("These read 0 here and noise on a console. Zero the statics at "
                       "boot, or run core/romcheck.py for who writes them and when.")
    return out


def cmd_run(args: argparse.Namespace) -> dict:
    rom = Path(args.rom)
    bios_path = Path(args.bios) if args.bios else (HLE_BIOS if HLE_BIOS.is_file() else None)
    bios = bios_path.read_bytes() if bios_path else None
    # Through the archive loader, so a .zip/.7z works here exactly as it does in the
    # app -- including "Pack.zip/Game.ngc" to name one title inside a multi-game
    # archive. A bare .ngc falls straight through to a plain read.
    machine = native.NativeMachine(rom_loader.read_rom_bytes(args.rom), bios=bios)
    apply_timing(machine, args.timing)          # BEFORE reset: it prices the boot too
    if args.hw_guard:
        machine.set_hw_guard(native.HW_WATCHDOG | native.HW_SYSTEM_STACK)
    machine.reset(bios_handoff=True)

    if args.state:
        load_state(machine, Path(args.state))
    else:
        # READ BEFORE WRITE, armed from power-on. This core hands the cartridge a
        # ZEROED work RAM; a console hands it whatever was there. A global the ROM
        # never initialises therefore reads 0 here and noise there -- KERO CROSS
        # (2026-10-16) ran perfectly in every run of this command and froze on
        # hardware, because `paused` started non-zero. The counter existed (romcheck
        # arms it) but no everyday run did. Armed AFTER the reset, as in romcheck:
        # the reset writes RAM itself. Not armed from a save state, whose RAM is
        # already the program's.
        machine.set_hygiene(True)

    held = parse_buttons(args.hold)
    summary = None
    for _ in range(max(0, args.frames)):
        machine.write(0x00B0, bytes([held & 0x7F]))
        summary = machine.run_frames(1)
        # Only when the caller ARMED the guard: without it these two are diagnostics
        # and the ROM is meant to run on, exactly as a console does.
        if args.hw_guard and summary.stop_status in (
                native.STATUS_SYSTEM_STACK_VIOLATION, native.STATUS_WATCHDOG_RESET):
            break

    cpu = machine.cpu()
    out = {
        "rom": str(rom),
        "bios": str(args.bios) if args.bios else ("hle" if bios_path else None),
        "state": str(args.state) if args.state else None,
        "held": args.hold or "",
        "frames": args.frames,
        "pc": cpu.pc,
        "registers": {n: cpu.regs[i] for i, n in enumerate(native.REG_NAMES)},
        "stop_status": native.status_name(summary.stop_status) if summary else "no-frames-run",
        "frame_count": summary.frame_count if summary else 0,
        # Say which timing model produced these numbers, so a cycle figure is never
        # quoted without knowing whether the cart bus was billed.
        #
        # ⛔ CE QUI A TOURNE, PAS CE QU'ON A DEMANDE. `--timing silicon` passe par
        # `set_timing_silicon`, que `NGPCRAFT_TIMING=legacy` detourne vers l'ancien
        # modele : imprimer le drapeau ferait dire au rapport « silicon » sous des
        # chiffres produits par l'autre machine. Un rapport qui se trompe de machine est
        # pire que pas de rapport -- c'est celui qu'on cite six mois plus tard.
        "timing": (native.active_timing_model() if args.timing == "silicon"
                   else args.timing),
        "hw_safety": hw_report(machine),
        "hygiene": hygiene_report(machine, armed=not args.state, rom=rom),
    }
    if args.peek:
        addr, count = int(args.peek[0], 0), int(args.peek[1], 0)
        out["peek"] = {"address": addr, "count": count,
                       "bytes": machine.read(addr, count).hex()}
    if args.shot:
        write_png(machine, Path(args.shot))
        out["screenshot"] = str(args.shot)
    machine.close()
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="run the native core and report what came out")
    run.add_argument("rom", help="path to a .ngc / .ngp, or an archive; "
                                 "'Pack.zip/Game.ngc' picks one title inside it")
    run.add_argument("--bios", help="a real bios.bin; some games need it (see README). "
                                    "Without it the clean-room hle_bios/bios_hle.bin is used")
    run.add_argument("--state", help="a player save state (.s0) to start from")
    run.add_argument("--frames", type=int, default=1, help="frames to advance (default 1)")
    run.add_argument("--hold", help="buttons held for every frame, e.g. 'A' or 'LEFT+B'")
    run.add_argument("--shot", help="write the resulting frame here (.png or .ppm)")
    run.add_argument("--peek", nargs=2, metavar=("ADDR", "COUNT"),
                     help="read COUNT bytes at ADDR after the run")
    run.add_argument("--timing", choices=("silicon", "free"), default="silicon",
                     help="cart-flash wait-states: 'silicon' (calibrated, the default) "
                          "or 'free' (no fetch cost -- the raw core default, ~2.9x too "
                          "fast; only for reproducing a pre-wait-state measurement)")
    run.add_argument("--hw-guard", action="store_true",
                     help="STOP the run on a hardware-safety finding (starved watchdog, "
                          "stack in the BIOS page) instead of only counting it")
    run.add_argument("--json", action="store_true", help="machine-readable output")
    run.set_defaults(func=cmd_run)

    args = parser.parse_args(argv)
    if not native.available():
        raise SystemExit(
            "the native core is not built. Build it with:\n"
            "  cmake -S cpp -B cpp/build -G 'MinGW Makefiles' && cmake --build cpp/build"
        )
    result = args.func(args)
    if getattr(args, "json", False):
        print(json.dumps(result, indent=2))
    else:
        for key, value in result.items():
            if key == "registers":
                print("registers:", " ".join(f"{n}={v:#010x}" for n, v in value.items()))
            elif key == "peek":
                print(f"peek {value['address']:#08x}+{value['count']}: {value['bytes']}")
            elif key == "hw_safety":
                if value["clean"]:
                    print("hw_safety: clean")
                else:
                    print("hw_safety: " + ", ".join(f"{k}={v}" for k, v in
                                                    value["counts"].items() if v))
                    for s in value["first"]:
                        print(f"  {s['kind']} at pc={s['pc']:#08x} "
                              f"detail={s['detail']:#x} cycle={s['cycle']}")
            elif value is not None:
                print(f"{key}: {value}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
