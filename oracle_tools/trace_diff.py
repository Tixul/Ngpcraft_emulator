#!/usr/bin/env python3
"""Diff a reference instruction trace against our emulator's trace.

Consumes a per-instruction register trace (text or ``--json``) and the same
shape emitted by our own ``trace-exec`` run (pc + the eight 32-bit registers
per step). This tool aligns the two by instruction index and reports the FIRST
divergence with context.

TRIAGE, NOT VERDICT -- whenever the reference side is anything other than our
own core. An outside trace generally smooths over broken opcodes and silicon
quirks (open-bus, D0 ALU-imm, the C8..CF family) that no commercial game ever
triggers, so a divergence means "investigate, and if needed flash it on a real
NGPC" -- never "our emulator is wrong". Conversely our emulator models some
registers as *unknown* (None); those slots are skipped, not flagged, so an
unknown never masquerades as a mismatch.

(For a gate that IS a verdict, use ``native_diff.py``: both sides are ours,
so any difference there is a bug by definition.)

Trace schemas accepted
----------------------
* co-sim TEXT : ``NNNN PC=xxxxxx WA=... BC=... ... SP=... SR=.... RFP=n cyc=n``
* co-sim JSON : one ``{"i":..,"pc":..,"wa":..,...}`` object per line
* ours JSON   : one object per line with ``pc`` and any of
                ``wa/xwa, bc/xbc, de/xde, hl/xhl, ix/xix, iy/xiy, iz/xiz, sp/xsp``
                (missing or null register = unknown, skipped in comparison)

Usage
-----
    python trace_diff.py REF.txt OURS.jsonl
    python trace_diff.py --self-test REF.txt      # diff a trace against itself
"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

REGS = ("wa", "bc", "de", "hl", "ix", "iy", "iz", "sp")
_ALIAS = {f"x{r}": r for r in REGS}
_TEXT = re.compile(
    r"PC=([0-9A-Fa-f]+)"
    + "".join(rf".*?\b{r.upper()}=([0-9A-Fa-f]+)" for r in REGS)
    + r".*?\bSR=([0-9A-Fa-f]+)",
)


def _norm_record(pc: int, regs: dict[str, int | None], f: int | None = None) -> dict:
    out = {"pc": pc & 0xFFFFFF}
    for r in REGS:
        v = regs.get(r)
        out[r] = None if v is None else (v & 0xFFFFFFFF)
    out["f"] = None if f is None else (f & 0xFF)
    return out


def parse_trace(path: Path) -> list[dict]:
    """Parse a co-sim (text/json) or ours (json) trace into normalized records."""

    records: list[dict] = []
    for line in path.read_text(errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        if line[0] == "{":  # JSON line (co-sim --json or ours)
            obj = json.loads(line)
            pc = obj.get("pc")
            if pc is None:
                continue
            regs: dict[str, int | None] = {}
            for r in REGS:
                if r in obj:
                    regs[r] = obj[r]
                elif f"x{r}" in obj:
                    regs[r] = obj[f"x{r}"]
            # F register: explicit "f", else derived from co-sim's full "sr"
            if obj.get("f") is not None:
                f = obj["f"]
            elif obj.get("sr") is not None:
                f = obj["sr"] & 0xFF
            else:
                f = None
            records.append(_norm_record(int(pc), regs, f))
        else:  # co-sim text line
            m = _TEXT.search(line)
            if not m:
                continue
            pc = int(m.group(1), 16)
            regs = {r: int(m.group(i + 2), 16) for i, r in enumerate(REGS)}
            f = int(m.group(len(REGS) + 2), 16) & 0xFF
            records.append(_norm_record(pc, regs, f))
    return records


def diff(ref: list[dict], ours: list[dict]) -> dict:
    """Return the first divergence (or {} if none) plus summary counts."""

    n = min(len(ref), len(ours))
    reg_mismatch = 0
    first: dict | None = None
    for i in range(n):
        a, b = ref[i], ours[i]
        fields: list[str] = []
        if a["pc"] != b["pc"]:
            fields.append("pc")
        for r in REGS:
            # only compare where BOTH sides claim to know the value
            if a[r] is not None and b[r] is not None and a[r] != b[r]:
                fields.append(r)
        if a["f"] is not None and b["f"] is not None and a["f"] != b["f"]:
            fields.append("f")
        if fields:
            reg_mismatch += 1
            if first is None:
                first = {"index": i, "fields": fields, "ref": a, "ours": b}
            if "pc" in fields:
                # PC divergence = control flow split; everything after is noise
                break
    return {
        "compared": n,
        "ref_len": len(ref),
        "ours_len": len(ours),
        "diverging_steps": reg_mismatch,
        "first": first,
    }


def _fmt(rec: dict) -> str:
    parts = [f"PC={rec['pc']:06X}"]
    for r in REGS:
        parts.append(f"{r.upper()}={'--------' if rec[r] is None else format(rec[r], '08X')}")
    parts.append(f"F={'--' if rec['f'] is None else format(rec['f'], '02X')}")
    return " ".join(parts)


def _report(res: dict, ref: list[dict], ours: list[dict]) -> int:
    print(f"[trace-diff] compared {res['compared']} steps "
          f"(ref={res['ref_len']}, ours={res['ours_len']}), "
          f"{res['diverging_steps']} diverging")
    first = res["first"]
    if not first:
        print("[trace-diff] OK - no divergence on compared prefix.")
        return 0
    i = first["index"]
    print(f"\n[trace-diff] FIRST divergence at step {i} on: {', '.join(first['fields'])}")
    lo = max(0, i - 2)
    for k in range(lo, min(len(ref), len(ours), i + 3)):
        mark = ">>" if k == i else "  "
        print(f"{mark} #{k}")
        print(f"     ref : {_fmt(ref[k])}")
        print(f"     ours: {_fmt(ours[k])}")
    print("\n[trace-diff] TRIAGE: a divergence is a lead, not a verdict. The tiers")
    print("             lissent les opcodes casses - confirm on real NGPC before")
    print("             concluding our emulator (or the toolchain output) is wrong.")
    return 1


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("ref", help="reference trace (co-sim text or json)")
    ap.add_argument("ours", nargs="?", help="our emulator trace (json lines)")
    ap.add_argument("--self-test", action="store_true", help="diff ref against itself")
    args = ap.parse_args()

    ref = parse_trace(Path(args.ref))
    if args.self_test:
        ours = ref
    elif args.ours:
        ours = parse_trace(Path(args.ours))
    else:
        ap.error("provide OURS trace, or use --self-test")
        return 2

    return _report(diff(ref, ours), ref, ours)


if __name__ == "__main__":
    raise SystemExit(main())
