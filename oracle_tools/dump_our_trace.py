#!/usr/bin/env python3
"""Dump our Python emulator's execution trace in the trace_diff JSON schema.

Runs `core.trace_exec.load_execution_trace` on a ROM and emits one JSON object
per executed instruction: `{ "i", "pc", "wa", "bc", "de", "hl", "ix", "iy",
"iz", "sp" }`, using the state AFTER each instruction, which is the convention
a reference trace is expected to follow too.

Unknown registers (our emulator models some as None) are emitted as `null`;
`trace_diff.py` skips null slots so an unknown never counts as a mismatch.

Run from the emulator project root so `core` imports resolve:
    python oracle_tools/dump_our_trace.py ROM.ngc -n 5000 > ours.jsonl
    python oracle_tools/trace_diff.py ref.jsonl ours.jsonl
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# allow running as `python oracle_tools/dump_our_trace.py` from the project root
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.trace_exec import load_execution_trace  # noqa: E402
from core.cpu import (  # noqa: E402
    BankedByteRegisters,
    GeneralRegisters32,
    NgpcCpuState,
    create_unknown_control_registers,
    decode_f_to_flags,
    encode_f_from_flags,
)

_REG_FIELDS = (
    ("wa", "xwa"), ("bc", "xbc"), ("de", "xde"), ("hl", "xhl"),
    ("ix", "xix"), ("iy", "xiy"), ("iz", "xiz"), ("sp", "xsp"),
)


def _load_reset_json(path: Path) -> dict:
    """Read a reference `--dump-reset` output (one JSON line) into a dict."""
    for line in path.read_text(errors="replace").splitlines():
        line = line.strip()
        if line.startswith("{"):
            return json.loads(line)
    raise SystemExit(f"no JSON reset line in {path}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("rom")
    ap.add_argument("-n", "--count", type=int, default=2000, help="instructions to trace")
    ap.add_argument("--start-pc", type=lambda s: int(s, 0), default=None)
    ap.add_argument("--seed-xsp", type=lambda s: int(s, 0), default=None,
                    help="seed the reset stack pointer (NGPC BIOS sets XSP, e.g. 0x6C00)")
    ap.add_argument("--seed-reg", action="append", default=[], metavar="NAME=HEX",
                    help="seed a register, e.g. --seed-reg xsp=0x6C00 (repeatable)")
    ap.add_argument("--seed-reset", action="store_true",
                    help="seed the standard reset convention (all GPR=0, xsp=0x6C00)")
    ap.add_argument("--seed-from", metavar="FILE",
                    help="seed exactly from a reference `--dump-reset` JSON line "
                         "(registers + start-pc); overrides --seed-reset")
    args = ap.parse_args()

    start_pc = args.start_pc
    seed_registers: dict[str, int] = {}
    # SR pieces: the hand-off convention is iff_level=7 (DI), rfp=0,
    # flags clear (sr=0xF800). `--seed-from` overrides from the reset JSON.
    sr_raw: int | None = None
    rfp_val = 0
    seed_full_state = False

    if args.seed_reset:
        seed_registers = {r: 0 for r in ("xwa", "xbc", "xde", "xhl", "xix", "xiy", "xiz")}
        seed_registers["xsp"] = 0x6C00
        sr_raw, rfp_val, seed_full_state = 0xF800, 0, True

    if args.seed_from:
        reset = _load_reset_json(Path(args.seed_from))
        for key, attr in _REG_FIELDS:
            if reset.get(key) is not None:
                seed_registers[attr] = reset[key]
        if start_pc is None and reset.get("pc") is not None:
            start_pc = reset["pc"]
        if reset.get("sr") is not None:
            sr_raw = int(reset["sr"]) & 0xFFFF
            rfp_val = int(reset.get("rfp", (sr_raw >> 8) & 0x3))
            seed_full_state = True

    for item in args.seed_reg:  # explicit --seed-reg wins (last)
        name, _, val = item.partition("=")
        seed_registers[name.strip()] = int(val, 0)

    # Build a FULL initial CPU state (regs + iff/rfp/flags from SR) so carts
    # that `PUSH SR` early do not honest-stop on `requires-known-sr`. Without
    # this the trace seeds only register VALUES and SR/iff/rfp stay unknown.
    initial_cpu_state: NgpcCpuState | None = None
    if seed_full_state:
        iff_level = (sr_raw >> 12) & 0x7
        initial_cpu_state = NgpcCpuState(
            pc=start_pc if start_pc is not None else 0,
            sr_raw=sr_raw,
            flags=decode_f_to_flags(sr_raw & 0xFF),
            register_bank=0,
            regs=GeneralRegisters32(
                xwa=seed_registers.get("xwa", 0), xbc=seed_registers.get("xbc", 0),
                xde=seed_registers.get("xde", 0), xhl=seed_registers.get("xhl", 0),
                xix=seed_registers.get("xix", 0), xiy=seed_registers.get("xiy", 0),
                xiz=seed_registers.get("xiz", 0),
                xsp=seed_registers.get("xsp", args.seed_xsp or 0x6C00),
            ),
            modeled_fields=("PC", "architectural-register-set", "SR"),
            note="reset seed (regs + iff/rfp/flags from SR)",
            iff_enabled=(iff_level < 7),
            iff_level=iff_level,
            rfp=rfp_val,
            register_banks=tuple(BankedByteRegisters(slots=(0,) * 16) for _ in range(4)),
            control_registers=create_unknown_control_registers(),
        )

    result = load_execution_trace(
        args.rom,
        start_pc=start_pc,
        count=args.count,
        seed_xsp=args.seed_xsp,
        seed_registers=None if initial_cpu_state is not None else (seed_registers or None),
        initial_cpu_state=initial_cpu_state,
    )

    emitted = 0
    for rec in result.records:
        cpu = rec.execution.after_cpu
        if cpu is None:  # honest stop: nothing executed past here
            break
        regs = cpu.regs
        obj = {"i": rec.index, "pc": (cpu.pc or 0) & 0xFFFFFF}
        for key, attr in _REG_FIELDS:
            v = getattr(regs, attr)
            obj[key] = None if v is None else (v & 0xFFFFFFFF)
        f = encode_f_from_flags(cpu.flags)  # F register (low byte of SR), or None
        obj["f"] = f
        print(json.dumps(obj))
        emitted += 1

    print(
        f"[dump_our_trace] emitted {emitted}/{result.requested_count} "
        f"(executed={result.executed_count}, stop={result.stop_reason})",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
