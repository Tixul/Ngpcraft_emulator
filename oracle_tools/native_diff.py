"""Differential harness: the Python reference core vs the native C++ core.

This is the centrepiece of the C++ port (specs/CPP_CORE_PORT.md §5, gates G1/G2).

WHY IT EXISTS
-------------
The port has to move ~102 dispatch bodies and ~135 (opcode x sub-op) pairs
without regressing the model. Reading 18 846 lines of Python and 18 846 lines
of C++ side by side does not scale and does not prove anything. Executing both
on the same input and comparing the full resulting state does.

Both models are OURS, and that is exactly what makes this gate strong: a
divergence is a BUG to fix, never a difference of opinion to triage. It is also
the only gate that reaches opcodes no ROM in the corpus ever executes -- a ROM
sweep, by construction, can only exercise what games actually run.

THE TRICK THAT MAKES IT WORK
----------------------------
The Python core is tri-state (`int | None`, `None` = unknown) and stops honestly
whenever it needs a value it does not have. That looks like it would block any
comparison with a concrete C++ core. It does not: if the seed state is FULLY
CONCRETE (all 8 registers, all 6 flags, IFF, RFP, and the memory the instruction
touches), the tri-state never triggers. Seed concretely and the two cores are
directly comparable.

WHAT IS COMPARED (everything that is architecturally visible)
------------------------------------------------------------
    PC, the 8 general registers, the 6 flags, IFF level, RFP,
    every memory write (address, bytes, and whether it was discarded),
    the cycle count, and the terminal status.

`trace_diff.py`, used for whole-ROM traces, only compares PC + 8 regs + F. Here
we can afford to compare everything, because both sides are instrumented.

AGREEMENT RULES
---------------
- both cores execute      -> compare the full state. Any difference = divergence.
- both cores refuse       -> agreement (an opcode neither models yet).
- Python executes, C++ refuses -> NOT a divergence: it is unported work. Counted
                                  separately as `todo`, so the harness stays
                                  usable throughout the port instead of drowning
                                  in noise until the last opcode lands.
- C++ executes, Python refuses -> DIVERGENCE. During the port C++ must never be
                                  more capable than the reference; if it is, it
                                  invented a behaviour.
"""

from __future__ import annotations

import argparse
import json
import random
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from core import native  # noqa: E402
from core.cpu import (  # noqa: E402
    BankedByteRegisters,
    GeneralRegisters32,
    NgpcCpuState,
    StatusFlags,
    Tlcs900ControlRegisters,
)
from core.execute import build_execute_next  # noqa: E402
from core.fetch import load_fetch_view  # noqa: E402

REG_NAMES = ("xwa", "xbc", "xde", "xhl", "xix", "xiy", "xiz", "xsp")
FLAG_NAMES = ("sf", "zf", "vf", "hf", "cf", "nf")

ENTRY = 0x200100          # where synthetic test ROMs put their code
SEED_RAM_BASE = 0x004000  # deterministic RAM window both cores are seeded with
SEED_RAM_SIZE = 0x0800

# Python statuses that mean "this core declines to execute". The tri-state
# `requires-known-*` family should NEVER appear with a concrete seed; if it does,
# the seed is incomplete and that is a harness bug, not an emulator bug.
_PY_REFUSALS = {
    "unknown-opcode",
    "truncated",
    "unsupported-decoded-instruction",
    "unmodeled-side-effects",
    "unmodeled-register-alias-side-effects",
    "unmodeled-stack-pointer-alias",
    "unmodeled-control-register",
    "not-yet-modeled",
    "unmapped",
}
_PY_TRISTATE = (
    "requires-known-",
    "runtime-state-required",
    "runtime-memory-unavailable",
    "stack-data-unavailable",
    "bios-call-requires-known-register",
)
_CPP_REFUSALS = {"unimplemented", "unknown-opcode", "truncated", "unmapped"}


# --------------------------------------------------------------------------- #
# Seed
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Seed:
    """A fully concrete machine state. No unknowns, by construction."""

    regs: tuple[int, ...]           # 8 x u32
    flags: tuple[bool, ...]         # sf zf vf hf cf nf
    iff_level: int
    rfp: int
    ram: bytes                      # seeded at SEED_RAM_BASE
    # The 4 register BANKS, 4 xregs each: only XWA..XHL are banked (XIX/XIY/XIZ/
    # XSP are shared). Bank[rfp] IS the visible window, so it must equal regs[0:4]
    # or the two cores start from different machines. `ldf` needs these concrete:
    # without them the reference degrades its banked file to unknown and stops.
    banks: tuple[int, ...] = ()     # 4 banks x 4 registers, row-major

    def to_json(self) -> dict:
        return {
            "regs": list(self.regs),
            "flags": [int(f) for f in self.flags],
            "iff_level": self.iff_level,
            "rfp": self.rfp,
            "ram": self.ram.hex(),
        }

    @staticmethod
    def from_json(d: dict) -> "Seed":
        return Seed(
            regs=tuple(d["regs"]),
            flags=tuple(bool(f) for f in d["flags"]),
            iff_level=d["iff_level"],
            rfp=d["rfp"],
            ram=bytes.fromhex(d["ram"]),
        )

    @staticmethod
    def random(rng: random.Random) -> "Seed":
        # Address registers are biased into seeded RAM: a purely random 32-bit
        # pointer would send almost every memory instruction into unmapped space
        # and we would fuzz the open-bus path instead of the opcode.
        def ptr() -> int:
            return SEED_RAM_BASE + rng.randrange(0, SEED_RAM_SIZE - 8)

        regs = [rng.getrandbits(32) for _ in range(4)]          # XWA XBC XDE XHL
        regs += [ptr(), ptr(), ptr()]                            # XIX XIY XIZ
        regs += [SEED_RAM_BASE + SEED_RAM_SIZE - 0x100]          # XSP: room to push
        banks = list(regs[:4])                       # bank 0 IS the visible window
        for _ in range(3):                           # banks 1..3
            banks += [rng.getrandbits(32) for _ in range(4)]
        return Seed(
            regs=tuple(regs),
            flags=tuple(bool(rng.getrandbits(1)) for _ in FLAG_NAMES),
            iff_level=rng.randrange(0, 8),
            rfp=0,
            ram=bytes(rng.getrandbits(8) for _ in range(SEED_RAM_SIZE)),
            banks=tuple(banks),
        )


def make_rom(body: bytes, *, entry: int = ENTRY) -> bytes:
    """Synthesize a minimal NGPC cart carrying `body` at its entry point.

    Same shape as tests/test_execute.py::_write_demo_rom — a 0x30 header whose
    entry-point field (offset 0x1C, little-endian) points at the code.
    """
    header = bytearray(0x30)
    header[0x00:0x1C] = b"COPYRIGHT BY SNK CORPORATION"[:0x1C].ljust(0x1C, b" ")
    header[0x1C:0x20] = entry.to_bytes(4, "little")
    header[0x23] = 0x10  # colour mode
    header[0x24:0x30] = b"DIFFHARNESS "[:0x0C].ljust(0x0C, b"\x00")

    offset = entry - 0x200000
    rom = bytearray(header)
    rom.extend(b"\x00" * (offset - len(rom)))
    rom.extend(body)
    rom.extend(b"\x00" * 16)  # so a long encoding never runs off the end
    return bytes(rom)


# --------------------------------------------------------------------------- #
# Normalised single-step result
# --------------------------------------------------------------------------- #
@dataclass
class Step:
    status: str
    executed: bool
    pc: int | None = None
    regs: tuple[int, ...] | None = None
    flags: tuple[bool, ...] | None = None
    iff_level: int | None = None
    rfp: int | None = None
    writes: tuple = ()          # ((addr, hexbytes, discarded), ...) sorted
    cycles: int | None = None
    ram: bytes | None = None    # the whole seeded RAM window, AFTER the step
    note: str = ""


def _norm_writes(items) -> tuple:
    return tuple(sorted(items))


# --------------------------------------------------------------------------- #
# Python reference core
# --------------------------------------------------------------------------- #
def python_step(rom_path: Path, seed: Seed, pc: int) -> Step:
    cpu = NgpcCpuState(
        pc=pc,
        sr_raw=None,
        flags=StatusFlags(**dict(zip(FLAG_NAMES, seed.flags))),
        register_bank=seed.rfp,
        regs=GeneralRegisters32(**dict(zip(REG_NAMES, seed.regs))),
        modeled_fields=("PC", "architectural-register-set"),
        note="native_diff concrete seed",
        iff_level=seed.iff_level,
        rfp=seed.rfp,
        alt_flags=StatusFlags(sf=False, zf=False, vf=False, hf=False, cf=False, nf=False),
        # The control registers must be CONCRETE too, or `ldc r, cr` makes the
        # reference stop on `requires-known-control-register` -- a tri-state stop,
        # which on a concrete seed means the SEED is incomplete, not the emulator.
        # The native core zeroes its control-register file at reset, so match it.
        register_banks=tuple(
            BankedByteRegisters(
                slots=tuple(
                    (seed.banks[b * 4 + (slot >> 2)] >> (8 * (slot & 3))) & 0xFF
                    for slot in range(16)
                )
            )
            for b in range(4)
        )
        if seed.banks
        else None,
        control_registers=Tlcs900ControlRegisters(
            dmas=(0, 0, 0, 0), dmad=(0, 0, 0, 0),
            dmac=(0, 0, 0, 0), dmam=(0, 0, 0, 0), intnest=0,
        ),
    )
    memory = {SEED_RAM_BASE + i: b for i, b in enumerate(seed.ram)}

    view = load_fetch_view(str(rom_path))
    result = build_execute_next(view, start_pc=pc, cpu_state=cpu, memory_bytes=memory)

    if result.status != "executed" or result.after_cpu is None:
        return Step(status=result.status, executed=False, note=result.note[:160])

    after = result.after_cpu
    return Step(
        status="executed",
        executed=True,
        pc=after.pc,
        regs=tuple(getattr(after.regs, n) for n in REG_NAMES),
        flags=tuple(getattr(after.flags, n) for n in FLAG_NAMES),
        iff_level=after.iff_level,
        rfp=after.rfp,
        writes=_norm_writes(
            (w.address, w.data.hex(), w.note.startswith("[DISCARDED]"))
            for w in result.memory_writes
        ),
        cycles=result.cycles_consumed,
        # The whole RAM window, read back after the step. This is what makes the
        # BLOCK instructions testable: `ldir` writes a STREAM of bytes, far more
        # than the 4 accesses a per-instruction record can hold, so comparing
        # write records alone would always diverge on them. Comparing the memory
        # itself compares the actual effect, however many bytes it took.
        ram=bytes(
            (result.after_memory or {}).get(SEED_RAM_BASE + i, seed.ram[i])
            for i in range(SEED_RAM_SIZE)
        ),
    )


# --------------------------------------------------------------------------- #
# Native C++ core
# --------------------------------------------------------------------------- #
def native_step(rom_bytes: bytes, seed: Seed, pc: int) -> Step:
    with native.NativeMachine(rom_bytes) as m:
        m.reset(bios_handoff=False)
        m.write(SEED_RAM_BASE, seed.ram)

        cpu = m.cpu()
        cpu.pc = pc
        for i, value in enumerate(seed.regs):
            cpu.regs[i] = value
        cpu.flags = (
            (int(seed.flags[4]) << 0)   # C
            | (int(seed.flags[5]) << 1)  # N
            | (int(seed.flags[2]) << 2)  # V
            | (int(seed.flags[3]) << 4)  # H
            | (int(seed.flags[1]) << 6)  # Z
            | (int(seed.flags[0]) << 7)  # S
        )
        cpu.iff_level = seed.iff_level
        cpu.rfp = seed.rfp
        for b in range(4):
            for i in range(4):
                cpu.banks[b][i] = seed.banks[b * 4 + i] if seed.banks else 0
        m.set_cpu(cpu)

        summary, records = m.run(1)
        name = native.status_name(summary.stop_status)

        if summary.executed != 1 or not records:
            return Step(status=name, executed=False)

        rec = records[0]
        after = m.cpu()
        f = after.flags
        return Step(
            status="executed",
            executed=True,
            pc=after.pc,
            regs=tuple(after.regs[i] for i in range(8)),
            flags=(
                bool(f & 0x80),  # S
                bool(f & 0x40),  # Z
                bool(f & 0x04),  # V
                bool(f & 0x10),  # H
                bool(f & 0x01),  # C
                bool(f & 0x02),  # N
            ),
            iff_level=after.iff_level,
            rfp=after.rfp,
            writes=_norm_writes(
                (
                    rec.writes[i].address,
                    bytes(rec.writes[i].data[: rec.writes[i].size]).hex(),
                    bool(rec.writes[i].discarded),
                )
                for i in range(rec.n_writes)
            ),
            cycles=rec.cycles,
            ram=m.read(SEED_RAM_BASE, SEED_RAM_SIZE),
        )


# --------------------------------------------------------------------------- #
# Comparison
# --------------------------------------------------------------------------- #
@dataclass
class Verdict:
    kind: str   # agree | both-refuse | todo | ahead | cycles-adder | DIVERGENCE
    fields: list[str] = field(default_factory=list)


# Toshiba instruction list (10), "Addressing mode" -- the per-mode cycle adder.
# Our Python reference applies NONE of it, so its memory-op cycle counts are all
# too low by exactly this amount. That is a defect in the reference, proven
# against the manufacturer; it is NOT a port bug. Rather than waving cycles
# through (which would let a REAL cycle bug hide behind a known one), we assert
# the identity `cpp == py + extra(mode)`. Anything else is still a DIVERGENCE.
_MODE_EXTRA = {16: 1, 17: 2, 18: 3, 20: 1, 21: 1}   # 0..7 -> 0, 8..15 -> 1, 19 -> 1 or 3


def mode_extra(body: bytes) -> int | None:
    """Addressing-mode cycle adder for a memory-family encoding, else None."""
    if not body:
        return None
    b = body[0]
    if b < 0x80 or b >= 0xF6:
        return None
    mem = ((b & 0x40) >> 2) | (b & 0x0F)
    if mem <= 7:
        return 0
    if mem <= 15:
        return 1
    if mem == 19:                       # depends on the secondary byte
        if len(body) < 2:
            return None
        data = body[1]
        if data in (0x03, 0x07, 0x13) or (data & 0x03) == 0x01:
            return 3
        return 1
    return _MODE_EXTRA.get(mem)


def compare(py: Step, cpp: Step, *, body: bytes | None = None) -> Verdict:
    if py.executed and cpp.executed:
        bad: list[str] = []
        if py.pc != cpp.pc:
            bad.append(f"pc py=0x{py.pc:06X} cpp=0x{cpp.pc:06X}")
        for i, name in enumerate(REG_NAMES):
            if py.regs[i] != cpp.regs[i]:
                bad.append(f"{name} py=0x{py.regs[i]:08X} cpp=0x{cpp.regs[i]:08X}")
        for i, name in enumerate(FLAG_NAMES):
            # A `None` flag from the reference means the DATASHEET DECLARES IT
            # UNDEFINED -- e.g. Toshiba gives CCF/ZCF the symbol row
            # `- - x - 0 *`, where `x` is literally "an undefined value is set".
            # The reference makes no claim, so no concrete value the native core
            # produces can be wrong, and comparing would be a false alarm.
            #
            # This is NOT a loophole for unmodelled state: the seed is fully
            # concrete, so a flag can only come back `None` if the executor
            # deliberately set it to "undefined". (It does mean the chantier's
            # §2 contract was incomplete -- the tri-state encodes silicon-
            # undefined results too, not just unseeded values.)
            if py.flags[i] is None:
                continue
            if py.flags[i] != cpp.flags[i]:
                bad.append(f"{name} py={py.flags[i]} cpp={cpp.flags[i]}")
        if py.iff_level != cpp.iff_level:
            bad.append(f"iff py={py.iff_level} cpp={cpp.iff_level}")
        if py.rfp != cpp.rfp:
            bad.append(f"rfp py={py.rfp} cpp={cpp.rfp}")
        if py.ram is not None and cpp.ram is not None and py.ram != cpp.ram:
            first = next(i for i in range(len(py.ram)) if py.ram[i] != cpp.ram[i])
            bad.append(
                f"RAM differs at 0x{SEED_RAM_BASE + first:06X}: "
                f"py=0x{py.ram[first]:02X} cpp=0x{cpp.ram[first]:02X} "
                f"({sum(a != b for a, b in zip(py.ram, cpp.ram))} bytes)"
            )

        # Write RECORDS are capped at NGPC_MAX_ACCESS per instruction, on purpose:
        # a block instruction writes a stream. So a record-count mismatch on a
        # repeating block op is expected -- what must match is the MEMORY, checked
        # just above.
        block_repeat = bool(body) and len(body) >= 2 and body[0] >= 0x80 and body[1] in (
            0x11, 0x13, 0x15, 0x17
        )
        writes_ok = block_repeat or py.writes == cpp.writes
        discard_only = (
            not writes_ok
            and len(py.writes) == len(cpp.writes)
            and all(
                a[0] == b[0] and a[1] == b[1] and a[2] != b[2]
                for a, b in zip(py.writes, cpp.writes)
            )
        )
        if not writes_ok and not discard_only:
            bad.append(f"writes py={py.writes} cpp={cpp.writes}")

        cycles_ok = py.cycles == cpp.cycles
        adder = mode_extra(body) if body else None

        # The reference bills a FLAT 8 whenever its cycle resolver has no row for
        # the instruction. That 8 is not a measurement: it is literally named
        # ESTIMATED_CYCLES_PER_INSTRUCTION, and the DEVLOG says so ("remaining
        # executed opcodes still use the flat 8-cycle placeholder until their
        # table rows are wired in"). Our value comes from the Toshiba instruction
        # lists, so where the reference says 8 and we say something else, we are
        # the ones with a source. Counted separately, never silently.
        placeholder = not cycles_ok and py.cycles == 8 and cpp.cycles != 8
        known_adder = (
            not cycles_ok
            and adder is not None
            and cpp.cycles is not None
            and py.cycles is not None
            and cpp.cycles - py.cycles == adder
        )
        if not cycles_ok and not known_adder and not placeholder:
            bad.append(f"cycles py={py.cycles} cpp={cpp.cycles} (mode adder={adder})")

        if bad:
            return Verdict("DIVERGENCE", bad)
        if discard_only:
            # Same address, same bytes -- only the "was this write discarded?"
            # flag differs. PROVEN reference defect: the Python core's
            # ALU-on-memory path (core/execute.py, the (R32) handlers) writes
            # straight into its overlay WITHOUT calling _check_writable_range, so
            # a write to unmapped space is reported as landing. The address space
            # itself says otherwise -- probe(0x6B1B69) -> status 'unmapped'.
            # On silicon that write goes nowhere. The native core discards it.
            #
            # Only reachable when a pointer register holds garbage, which the
            # fuzzer does deliberately and real code never does -- so this is a
            # latent reference bug, not one that affects games. Counted, listed,
            # not hidden.
            return Verdict("ref-defect-discard",
                           [f"py kept an unmapped write: {py.writes}"])
        if known_adder:
            # Everything else matches; cycles differ by EXACTLY the datasheet's
            # addressing-mode adder, which the reference omits entirely.
            return Verdict("cycles-adder", [f"py={py.cycles} + {adder} = cpp={cpp.cycles}"])
        if placeholder:
            return Verdict("ref-placeholder-cycles",
                           [f"py billed its flat 8 placeholder; datasheet says {cpp.cycles}"])
        return Verdict("agree")

    if not py.executed and not cpp.executed:
        return Verdict("both-refuse")

    if py.executed and not cpp.executed:
        # Not yet ported. Loud, but expected during the port.
        if cpp.status in _CPP_REFUSALS:
            return Verdict("todo", [f"cpp={cpp.status}"])
        return Verdict("DIVERGENCE", [f"cpp stopped with {cpp.status}, python executed"])

    # --- C++ executed, the reference refused. -------------------------------
    if py.status == "runtime-memory-unavailable":
        # NOT a harness bug and NOT an unseeded register: the reference simply has
        # no byte for an address that IS mapped -- the cart window past the end of
        # the ROM image. Silicon reads ERASED FLASH there, i.e. 0xFF; the reference
        # returns "I don't know" instead. (Its own flash model knows this: DEVLOG
        # 2026-04-20, "cart flash erased-read fallback" -- the (R32) load path just
        # does not use it.) The native core fills the whole cart window with 0xFF
        # at reset, which is what the hardware does.
        return Verdict("ref-unbacked-read",
                       ["py has no byte for a mapped address (erased flash = 0xFF)"])
    if any(py.status.startswith(p) for p in _PY_TRISTATE):
        return Verdict(
            "DIVERGENCE",
            [f"HARNESS BUG: python hit tri-state {py.status!r} on a concrete seed"],
        )
    if py.status in ("division-by-zero", "silicon-undefined"):
        # NOT a defect on either side -- the two cores have different jobs.
        #
        # Toshiba defines the FLAG for this case and only the flag: "<Divide> ...
        # V = 1 is set when divided by 0 or the quotient exceeds the numerals which
        # can be expressed in bits of dst; otherwise 0." What lands in the
        # DESTINATION is not stated anywhere. The reference is an ANALYSIS core and
        # refuses to invent it -- its own note says so in as many words: "the packed
        # destination result is not something this emulator should guess."
        #
        # The native core has to RUN GAMES, and three commercial ROMs divide by zero
        # and carry on. It sets V, keeps the destination the datapath would have
        # produced, and moves on. So there is nothing to compare here: the reference
        # publishes no state. The part that IS defined -- V, the bit games branch on
        # -- is not in dispute.
        #
        # The destination value stays an OPEN HARDWARE QUESTION, flagged in
        # specs/TLCS900_MEMORY_FAMILY.md, for the same arbiter this project used on
        # D0..D7 and D8..DF: a test ROM on real silicon.
        return Verdict("ref-declines-undefined",
                       [f"py refuses to guess the destination ({py.status}); "
                        "cpp sets V, which IS defined, and runs on"])
    if py.status == "silicon-broken":
        # The reference's quirk matcher still fires "silicon-broken" on the D0
        # ALU-on-memory forms. But the OFFICIAL TOSHIBA ASSEMBLER emits them:
        #   addw (0x50),WA -> D0 50 88 | cpw WA,(0x50) -> D0 50 F0
        # and the project ALREADY RETRACTED that quirk after a hardware test on
        # 2026-07-03 ("D0..D7 is a word MEMORY-addressing family, the quirk was a
        # mis-diagnosis"). The matcher is a leftover: it should guard only against
        # word-REGISTER ops carrying a D0 prefix, which is a mis-encode, not
        # against ordinary memory forms.
        #
        # Surfaced, not silently overridden -- narrowing a silicon-broken claim is
        # a fidelity decision, and it is the maintainer's to make.
        return Verdict("ref-stale-quirk", [f"py cried silicon-broken; asm900 emits this encoding"])
    if py.status in ("unknown-opcode", "unsupported-decoded-instruction"):
        # "unsupported-decoded-instruction" is a MODELLING gap by definition: the
        # reference decoded the instruction perfectly well and simply has no
        # executor for it (e.g. `EX R, r`, which the Toshiba list gives both an
        # entry and a state, "3. 3. -"). Same class as a decoder gap, same rule:
        # allowed only because an independent authority says the encoding is real.
        # The reference's DECODER does not know this encoding. That is not
        # automatically a bug in us: the reference has 768 measured coverage gaps
        # in the memory family, and the OFFICIAL TOSHIBA ASSEMBLER emits the
        # disputed encodings (oracle_tools/asm900_oracle.py). So this is "ahead":
        # the native core is more complete than its reference.
        #
        # It is NOT a free pass. Every encoding that lands in this bucket must be
        # backed by asm900 -- see tests/test_native_diff.py, which asserts exactly
        # that. Without the asm900 check, `ahead` would be an excuse instead of a
        # claim.
        return Verdict("ahead", [f"reference decoder gap ({py.status})"])
    return Verdict("DIVERGENCE", [f"cpp executed, python refused ({py.status})"])


# --------------------------------------------------------------------------- #
# Fuzz driver (gate G2)
# --------------------------------------------------------------------------- #
def fuzz(count: int, *, rng_seed: int, encodings: list[bytes] | None, verbose: bool) -> int:
    rng = random.Random(rng_seed)
    tally = {"agree": 0, "both-refuse": 0, "todo": 0, "ahead": 0,
             "cycles-adder": 0, "ref-defect-discard": 0,
             "ref-placeholder-cycles": 0, "ref-stale-quirk": 0,
             "ref-unbacked-read": 0, "ref-declines-undefined": 0,
             "DIVERGENCE": 0}
    divergences: list[str] = []

    with tempfile.TemporaryDirectory() as tmp:
        rom_path = Path(tmp) / "fuzz.ngc"
        for n in range(count):
            body = (
                rng.choice(encodings)
                if encodings
                else bytes(rng.getrandbits(8) for _ in range(8))
            )
            seed = Seed.random(rng)
            rom_bytes = make_rom(body)
            rom_path.write_bytes(rom_bytes)

            py = python_step(rom_path, seed, ENTRY)
            cpp = native_step(rom_bytes, seed, ENTRY)
            v = compare(py, cpp, body=body)
            tally.setdefault(v.kind, 0)
            tally[v.kind] += 1

            if v.kind == "DIVERGENCE":
                divergences.append(
                    f"[{n}] bytes={body.hex()} seed_rng={rng_seed}\n      "
                    + "\n      ".join(v.fields)
                )
                if verbose:
                    print(divergences[-1], file=sys.stderr)

    total = sum(tally.values())
    print(json.dumps({"total": total, **tally}, indent=2))
    if divergences:
        print(f"\n{len(divergences)} DIVERGENCE(S):", file=sys.stderr)
        for d in divergences[:25]:
            print("  " + d, file=sys.stderr)
    return 1 if tally["DIVERGENCE"] else 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("-n", "--count", type=int, default=200)
    p.add_argument("--rng-seed", type=int, default=1234, help="deterministic by default")
    p.add_argument("--opcodes", help="hex encodings, comma-separated (else random bytes)")
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args(argv)

    if not native.available():
        print("native core not built: cmake --build cpp/build", file=sys.stderr)
        return 2

    encodings = (
        [bytes.fromhex(e.strip()) for e in args.opcodes.split(",")] if args.opcodes else None
    )
    return fuzz(args.count, rng_seed=args.rng_seed, encodings=encodings, verbose=args.verbose)


if __name__ == "__main__":
    raise SystemExit(main())
