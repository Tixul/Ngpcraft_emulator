"""Headless CLI for the NATIVE core — the emulator an agent can actually drive.

`ngpc_emu.py` inspects: it opens a ROM or a save state and reports what is in it.
This runs the machine. Load the state a player captured one frame before a bug, hold
a button, advance frames, and look at what came out -- the same loop a person does by
hand, available to a tool.

    python ngpc_native.py run GAME.ngc --state bug.s0 --hold A --frames 30 --shot out.png

Everything is one command so one tool call answers "what happens if I press A here?".
Output is JSON on stdout with --json, so a caller never has to parse prose.
"""

from __future__ import annotations

import argparse
import ctypes
import json
import sys
from pathlib import Path

from core import native

# The player's save state (ngpc_shell.py, F2): magic, the CpuState struct, then the
# working image from 0x0000. Same contract as core.savestate.load_shell_savestate.
SHELL_MAGIC = b"NGPCST01"
SHELL_MEM_LEN = 0x00C000

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
    if not blob.startswith(SHELL_MAGIC):
        raise SystemExit(f"{path} is not a {SHELL_MAGIC.decode()} save state")
    body = blob[len(SHELL_MAGIC):]
    cpu_t = type(machine.cpu())
    cpu_len = ctypes.sizeof(cpu_t)
    if len(body) != cpu_len + SHELL_MEM_LEN:
        raise SystemExit(
            f"{path}: expected {cpu_len + SHELL_MEM_LEN} bytes after the magic, got {len(body)}"
        )
    machine.write(0, body[cpu_len:])
    machine.set_cpu(cpu_t.from_buffer_copy(body[:cpu_len]))


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


def cmd_run(args: argparse.Namespace) -> dict:
    rom = Path(args.rom)
    bios = Path(args.bios).read_bytes() if args.bios else None
    machine = native.NativeMachine(rom.read_bytes(), bios=bios)
    machine.reset(bios_handoff=True)

    if args.state:
        load_state(machine, Path(args.state))

    held = parse_buttons(args.hold)
    summary = None
    for _ in range(max(0, args.frames)):
        machine.write(0x00B0, bytes([held & 0x7F]))
        summary = machine.run_frames(1)

    cpu = machine.cpu()
    out = {
        "rom": str(rom),
        "bios": str(args.bios) if args.bios else None,
        "state": str(args.state) if args.state else None,
        "held": args.hold or "",
        "frames": args.frames,
        "pc": cpu.pc,
        "registers": {n: cpu.regs[i] for i, n in enumerate(native.REG_NAMES)},
        "stop_status": native.status_name(summary.stop_status) if summary else "no-frames-run",
        "frame_count": summary.frame_count if summary else 0,
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
    run.add_argument("rom", help="path to a .ngc / .ngp")
    run.add_argument("--bios", help="a real bios.bin; some games need it (see README)")
    run.add_argument("--state", help="a player save state (.s0) to start from")
    run.add_argument("--frames", type=int, default=1, help="frames to advance (default 1)")
    run.add_argument("--hold", help="buttons held for every frame, e.g. 'A' or 'LEFT+B'")
    run.add_argument("--shot", help="write the resulting frame here (.png or .ppm)")
    run.add_argument("--peek", nargs=2, metavar=("ADDR", "COUNT"),
                     help="read COUNT bytes at ADDR after the run")
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
            elif value is not None:
                print(f"{key}: {value}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
