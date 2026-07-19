"""asm900 — the OFFICIAL Toshiba assembler as an ENCODING ORACLE.

Authority ranking established empirically on 2026-07-11, while porting the
memory-addressing families:

  1. **asm900.exe** (this module) — the official Toshiba assembler. If it emits
     the bytes, the encoding EXISTS. Not an opinion.
  2. Toshiba TLCS-900/L1 datasheet — semantics, cycles, flags. (Careful: its
     tables are images and its prose survives extraction badly; trust the SYMBOL
     rows, see DOC_SOURCES_INDEX.md §0.2.)
  3. Our Python core — hardware-verified where it acts, but it has **768
     coverage gaps** in the memory family (mostly long-size).
  4. `ngdis` (NgpCraft_Disasm) — good, but **STALE on `D0..D7`**: it still calls
     that family "silicon-broken word-reg ALU prefix", a mis-diagnosis the
     project RETRACTED after a hardware test on 2026-07-03. asm900 emits
     `ldw WA,(0x50)` = `D0 50 20`, so the family is a perfectly ordinary
     word-memory addressing mode.
  5. Cycle figures circulating in the wider scene — lowest rank. They are
     hand-tuned and have already lost to the datasheet once (8/4 is quoted
     around for JR; Toshiba says 5/2).

So: an encoding the native core implements but the Python reference refuses is
NOT automatically a bug -- but it must be BACKED BY THIS ORACLE, never by our own
reasoning. That is what makes "the C++ core is more complete than the reference"
an honest statement rather than an excuse.

Wraps `NgpCraft_toolchain/NgpCraft_Toolchain_v2/scripts/rel_probe.py`, which
brackets the instruction between two known anchors in a real `.asm`, assembles it
with asm900, and recovers the bytes from the `.rel` (the disassembler cannot be
used for this: it does not decode the extended forms, so a round-trip would be
blind).
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

TOOLCHAIN = (
    Path(__file__).resolve().parents[2]
    / "NgpCraft_toolchain"
    / "NgpCraft_Toolchain_v2"
)
ASM900 = Path("C:/t900/BIN/asm900.exe")

_DRIVER = r"""
import sys, json
sys.path.insert(0, "scripts")
from rel_probe import probe
instrs = json.loads(sys.argv[1])
out = {}
for k, v in probe(instrs).items():
    out[k] = v.hex() if isinstance(v, (bytes, bytearray)) else None
print(json.dumps(out))
"""


def available() -> bool:
    return ASM900.exists() and (TOOLCHAIN / "scripts" / "rel_probe.py").exists()


def encode(instructions: list[str]) -> dict[str, bytes | None]:
    """Assemble each instruction with the official Toshiba assembler.

    Returns {instruction: bytes} — or `None` for an instruction asm900 rejects,
    which is itself the answer: that encoding does not exist.

    Note: rel_probe returns the instruction bytes wrapped in `.rel` record
    framing (`0xE4 <len>` before each code run). We strip it here so callers get
    exactly the instruction bytes.
    """
    import json

    if not available():
        raise RuntimeError(f"asm900 not found at {ASM900}")

    res = subprocess.run(
        [sys.executable, "-c", _DRIVER, json.dumps(instructions)],
        cwd=str(TOOLCHAIN),
        capture_output=True,
        text=True,
        timeout=120,
    )
    if res.returncode != 0:
        raise RuntimeError(f"asm900 probe failed:\n{res.stderr[-800:]}")

    raw = json.loads(res.stdout.strip().splitlines()[-1])
    return {k: _strip_rel_framing(bytes.fromhex(v)) if v else None for k, v in raw.items()}


def _strip_rel_framing(blob: bytes) -> bytes:
    """`.rel` code runs arrive as `E4 <len> <len bytes> E4 <len> ...`.

    The probe brackets our instruction between two anchors, so the payload we
    want is the FIRST record: E4, its length byte, then that many bytes.
    """
    if len(blob) >= 2 and blob[0] == 0xE4:
        n = blob[1]
        return blob[2 : 2 + n]
    return blob


if __name__ == "__main__":
    tests = sys.argv[1:] or [
        "ld\tXWA,(XWA)",
        "add\tXWA,(XWA)",
        "ldw\tWA,(0x50)",
        "cpw\t(0x50),WA",
        "ex\t(0x50),W",
    ]
    for instruction, encoded in encode(tests).items():
        shown = encoded.hex() if encoded else "REJECTED (encoding does not exist)"
        print(f"{instruction:<24} {shown}")
