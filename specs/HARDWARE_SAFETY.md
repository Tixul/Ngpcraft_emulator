# Hardware safety findings

Two things a real Neo Geo Pocket Color minds, that the running program cannot
see, and that every emulator which does not model them lets pass in silence. A
ROM running here is not evidence of hardware compatibility unless these counters
are read.

They are **counted, not enforced**. Neither fault stops a real console at the
instruction that commits it — the stack overwrite corrupts something for later,
the watchdog raises INTWD — so neither stops this one. An emulator that halted
there would be reporting a crash the hardware does not have, and the first thing
it would cost you is an afternoon debugging the emulator instead of the ROM.

`ngpc_set_hw_guard` is the opt-in for the callers that want a verdict rather than
a report: pass a mask of kinds, and a run that commits one ends with the matching
status at the offending PC.

## System-stack boundary

SNK's `SysPro.txt` assigns user work RAM to `0x4000..0x6BFF`. `0x6C00..0x6FFF`
is system-managed RAM — the BIOS's own variables.

The TLCS-900/H stack descends before writing, therefore:

- `XSP = 0x6C00` is the valid exclusive top of the user stack;
- a 16-bit push first writes at `0x6BFE`, a 32-bit push at `0x6BFC`;
- a cartridge instruction leaving XSP in `0x6C01..0x6FFF` is a finding.

Restricted to instructions executing from either cartridge window
(`0x200000..0x3FFFFF`, `0x800000..0x9FFFFF`): the BIOS legitimately owns its own
page. **Edge-triggered** — a cart that parks its stack up there is one finding,
recorded at the PC that moved it, not one per instruction from then on.

## Watchdog

SNK's system documentation requires writing the clear code `0x4E` to WDCR at
I/O `0x006F` periodically, and `ngpcspec.txt` recommends an interval no longer
than about 100 ms.

- a valid `0x4E` write restarts the deadline;
- WDMOD at `0x006E` bit 7 arms the counter; arming restarts it;
- WDCR code `0xB1` switches the watchdog off — what the Toshiba startup emits
  (`WDMOD=0`, `WDCR=0xB1`) and what the BIOS uses while it boots;
- instruction, interrupt-entry and HALT-idle cycles all advance it;
- on expiry the counter **re-arms**, as in ares, so a ROM that never refreshes
  reports once per period instead of once per session.

### Who owns it at hand-off

Measured on the retail BIOS, by logging every write to `0x006E`/`0x006F` through
a full boot:

| PC | write | effect |
|---|---|---|
| `FF204F` / `FF215D` | WDMOD = `0x04` / `0x14` | disarmed |
| `FF2052` / `FF2160` / `FF19B9` | WDCR = `0xB1` | off |
| `FF1EE2`, `FF1BBD` | WDCR = `0x4E` | refresh |
| `FF1BC0` | WDMOD = `0xF0` | **armed**, last write before hand-off |

So a power-on starts with the watchdog off and lets the BIOS decide, and a
cartridge hand-off starts where the BIOS left it: armed. Refreshing it is the
cartridge's duty from its first instruction.

### ⚠️ The period is an assumption

One CPU second at the measured 6.144 MHz, i.e. 6,144,000 cycles — the value ares
uses. WDMOD's prescaler bits (WDTP, bits 6-5) are **not** decoded, and the SDK's
100 ms is the refresh rate asked of the program, not the counter's period. No
hardware measurement backs the number yet.

Being wrong here moves *when* a starved watchdog is reported, never whether the
ROM runs — which is precisely why this is a counter and not a stop. Treat the
count as the finding; do not build anything on its exact timing.

## Public contract

ABI v14 adds, alongside the hygiene counters:

```c
#define NGPC_HW_WATCHDOG     0x1u
#define NGPC_HW_SYSTEM_STACK 0x2u

NGPC_API void     ngpc_set_hw_guard(ngpc_t*, uint32_t stop_mask);   /* 0 = report only */
NGPC_API uint64_t ngpc_hw_violations(ngpc_t*, uint32_t kind);
NGPC_API uint32_t ngpc_get_hw_violations(ngpc_t*, ngpc_violation_t* out, uint32_t n);
```

and two statuses, produced **only** when the matching kind is armed:

| Code | Name | Meaning |
|---:|---|---|
| 14 | `system-stack-violation` | cartridge XSP entered BIOS-owned RAM |
| 15 | `watchdog-reset` | no valid refresh before the deadline |

The ROM analyzer (`core/romcheck.py`) reads the counters on every run and reports
both as errors, naming the code through the symbol map when one is loaded.

## Regression proof

`tests/test_hardware_safety.py`:

1. a starved watchdog is counted and the ROM **keeps running**;
2. it re-arms — three periods of starving report three times;
3. a loop refreshing `0x006F` reports nothing;
4. `WDMOD=0x14`, `WDCR=0xB1` (the BIOS/Toshiba sequence) reports nothing;
5. the gate turns the starve into `watchdog-reset` at ~6.14 M cycles;
6. `XSP=0x6EFF` is reported once, at the instruction that set it, with the value;
7. the crossing is reported once, not per instruction;
8. `XSP=0x6C00` is not a finding;
9. the gate turns the crossing into `system-stack-violation`;
10. one core, two builds, opposite verdicts.

Measured on content known to run — 16 ROMs (commercial, homebrew, project
builds) × hand-off and real-BIOS boot × 300 frames: **zero findings of either
kind**. A diagnostic that fires on everything teaches you to ignore it.

A Game Boy port built with GB2NGP is
the differential: clean over 600 frames in both boot modes, while the same ROM
with the old `XSP=0x6EFF` forced back reports the stack crossing at its first
cartridge instruction.

Passing these checks is necessary but not sufficient for a hardware release. The
last gate remains a cold boot and a gameplay test on a physical NGPC.
