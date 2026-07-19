# Execute v0

Purpose:
- define the first real execution-shaped output above the current decode helper
- mutate CPU state honestly for a narrow subset before a full interpreter exists

Current source references:
- `CPU_STATE.md`
- `DECODE.md`
- `STEP.md`
- `../../NgpCraft_toolchain/T900_DENSE_REF.md`

Current behavior:
- execute-next starts from one explicit address, or from the current bootstrap `PC`
- execute-next performs one real instruction application when the current subset can defend it
- execution is still narrow and deliberately incomplete
- unsupported side effects stop explicitly instead of being guessed
- locally confirmed silicon-broken forms now stop explicitly with status `silicon-broken`
- when a silicon-broken stop is a `ld Xd, Xs` register-to-register copy in the
  `0xD8..0xDF` family, the stop note also carries an actionable toolchain
  remediation hint (`push <src>` + `pop <dst>`); execution still stops and
  invents no post-state (diagnostic layer only, per `HARDWARE_COMPAT_POLICY.md`)
  instead of falling through to the generic unsupported bucket
- silicon-broken matching is now centralized through `core/quirks.py`
- the matched-quirk metadata exposed by execution payloads now includes the
  current quirk-database version loaded from `core/quirks_db.json` and the
  non-empty per-rule `sources` attribution list

Current supported execution subset:
- `NOP`
- direct unconditional jumps when the decoder already exposes one direct target:
  - `JP`
  - unconditional `JR`
  - unconditional `JRL`
- immediate register loads already covered by the decoder:
  - `LD R32, imm32`
  - `LD R16, imm16` only when the owning `R32` is already known
  - `LD R8, imm8` only when the owning `R32` is already known
- the same immediate-load rule currently applies to prefixed immediate register forms when the write is representable
- first prefixed register arithmetic forms on currently safe decoded families:
  - `INC n, R8`
  - `INC n, R32`
  - `DEC n, R8`
  - `DEC n, R32`
  - these currently execute only when the current register view can be read honestly from the CPU model
  - `D0..D7` forms still decoded as word register-direct stop with `silicon-broken`
    via `cpu.d0_d7_non_immediate` — but HW-corrected 2026-07-03: `D0..D7` is a WORD
    MEMORY-addressing family, not a register prefix (that quirk is now a documented
    mis-diagnosis; the abs8 word-memory forms `cpw`/`ldw R16` decode+execute, and
    the full re-decode is in progress). See `specs/DECODE.md` + `HARDWARE_COMPAT_POLICY.md`.
- first writable-stack instructions when `XSP` is known and the target range is writable in the current address map:
  - `PUSH A`
  - `POP A`
  - `PUSH F`
  - `POP F`
  - `PUSHW imm16`
  - `PUSH R16`
  - `PUSH R32`
  - `POP R16`
  - `POP R32`
  - `CALL`
  - `RET`
  - `RETD`
- first non-repeat block-memory subset on the currently decoded word forms:
  - `LDI`
  - `LDD`
  - `CPI`
  - `CPD`
  - `LDI` / `LDD` currently execute only for the documented implicit pointer
    pairs `(XDE+/-, XHL+/-)` and `(XIX+/-, XIY+/-)`
  - `CPI` / `CPD` currently execute as `WA - (R32+/-)`, decrement `BC`,
    preserve `CF`, and still stop honestly on the `XBC` alias case
- first repeat block-memory subset on the same byte/word block family:
  - `LDIR` / `LDDR` copy `BC` items from the implicit source pointer to the
    implicit destination pointer for the documented pairs
    `(XDE+/-, XHL+/-)` and `(XIX+/-, XIY+/-)`, post-adjusting both pointers by
    the operand width each item until `BC == 0`; `H`/`N`/`V` all clear
    (`V` because `BC` reaches 0) and `S`/`Z`/`C` are preserved
  - `CPIR` / `CPDR` compare `A`/`WA` against `(R32+/-)` each iteration,
    post-adjusting the pointer, until a match sets `Z` or `BC` reaches 0; the
    final `S`/`Z`/`H` come from the last compare, `V = (BC != 0)` after the
    last decrement, `N = 1`, and `CF` is preserved; the `XBC` pointer alias
    still stops honestly with `unmodeled-register-alias-side-effects`
  - the whole repeat is applied atomically: every memory access needed to
    reach the honest stopping point must be available up front, otherwise the
    instruction blocks with `runtime-memory-unavailable` without mutating any
    state. Real silicon can interrupt a block repeat mid-flight and resume it
    via `RETI`; the bounded single-step model does not sample interrupts inside
    one instruction, so it either runs the repeat to completion or blocks.
  - a starting `BC` of 0 wraps and runs the full `0x10000` pass, matching the
    decrement-then-test order of the silicon
- `0xF3` ARI secondary-indexed **mode=1** `(r32+d16)` store family (mirrors the
  mode=3 `(r32+r16)` stores that were already modeled; previously mode=1 only
  handled `LDA`):
  - `LD (r32+d16), imm8` / `LDW (r32+d16), imm16`
  - `LD (r32+d16), R8/R16/R32`
  - `EA = r32_base + signed(d16)`; blocks with `requires-known-address-register`
    on an unknown base and `requires-known-full-register` on an unknown source
  - the base register is taken from `(secondary >> 2) & 0x07`, which matches
    authoritative ngdis `r32_names[secondary & 0xFC]` for the current-bank
    encodings real code emits
- `0xC3`/`0xD3`/`0xE3` ARI secondary-indexed **mode=1** `(r32+d16)` read family
  (mirror of the pass-138 mode=1 stores; previously only mode=3 `(r32+r16)`
  loads were modeled):
  - `ld R8/R16/R32, (r32+d16)` (op `0x20..0x27`)
  - `cp (r32+d16), imm8` (`C3` op `0x3F`)
  - `EA = r32_base + signed(d16)`; blocks with `requires-known-address-register`
    on an unknown base and `runtime-memory-unavailable` on an unreadable EA
  - register-vs-memory compare `cp R8/R16/R32, (mem)` (op `0xF0..0xF7`) for
    both mode=1 `(r32+d16)` and mode=3 `(r32+r16)`: reads memory, sets the
    subtract flags from `R - mem`, writes nothing, blocks with
    `requires-known-full-register` on an unknown compared register
  - memory read-modify-write `inc/dec #n, (mem)` (op `0x60..0x6F`, `n=0 -> 8`)
    for both modes: reads, applies `+/- n`, writes back; sets `S/Z/V/H` and `N`
    but preserves `CF`
- long register-indirect `LD R32, (r32)` on the `0xA0..0xA7` family (op
  `0x20..0x27`): reads 4 bytes at `(r32)` into the destination register;
  blocks with `requires-known-address-register` / `runtime-memory-unavailable`.
  (Byte `0x80..0x87` and word `0x90..0x97` register-indirect were already
  modeled; this fills the long size.)
- `BIT #n, (r32+d8)` on the `0xB8..0xBF` displacement family (op `0xC8..0xCF`):
  reads one byte at `EA = r32 + signed(d8)`, sets `Z = NOT bit`, `H = 1`,
  `N = 0`, writes nothing; blocks honestly on an unknown base register
- `BIT #n, (mem)` on the F3 secondary-indexed addressing (op `0xC8..0xCF`):
  mode=1 `(r32+d16)` and mode=3 `(r32+r16)`, same `Z = NOT bit` / `H=1` / `N=0`
  read-only semantics
- register-indirect `CALL [cc,] (r32)` for the whole `0xB0..0xB7` family
  (op `0xE0..0xEF`), generalizing the former `B4 E8` (`call (XIX)`) special
  case:
  - taken calls push the sequential return address to the writable stack model
    and set `PC = r32`; a false conditional call advances `PC` sequentially
  - blocks with `requires-known-full-register` (unknown base) or
    `requires-known-flags` (unknown conditional flags)
- first address-oriented execution slice guided by the official-toolchain disassembly:
  - `LDA R32, (abs24)` as "effective address -> destination register"
  - prefixed register-to-register `CP`
  - indexed memory compare `CP (r32+d8), R32`
  - first abs16 byte compare-immediate: `CP (abs16), imm8`
  - `LD (r32+d8), R32`
  - `LD R32, (r32+d8)`
  - first prefixed register-to-register `LD`
  - first absolute memory stores from the stable official bootstrap:
    - `LD (abs24), R8`
    - `LD (abs24), imm8`
    - `LD (abs16), R32`
    - `LD (abs16), imm8`
    - `RES bit, (abs16)`
    - `SET bit, (abs16)`
- first post-increment byte-memory slice from the stable bootstrap loops:
  - `LD R8, (r32+)`
  - `LD (r32+), R8`
  - `LD (r32+), imm8`
- compact small-immediate register load (catalog `C8+zz+r : A8+#3`):
  - `LD R32, #3` — 2-byte encoding, value 0..7 embedded in the opcode
  - `LD R16, #3` — only when the owning R32 is already known
  - `LD R8, #3` — only when the owning R32 is already known
- first flag-driven control-flow slice:
  - conditional `JR`
  - conditional `JRL`
  - only when the required modeled flags are known
- documented immediate-safe word-prefix forms remain executable even when they use
  the `D0..D7` prefix:
  - `ld r, imm`
  - `multu/muls r, imm`
  - `ld r, #3`
  - ALU-immediate `add/adc/sub/sbc/and/xor/or/cp r, imm`
- first fixed CPU carry-flag control ops:
  - `RCF`
  - `SCF`
  - `CCF`
  - `ZCF`
  - `CCF` / `ZCF` keep the documented undefined `H` result as unknown
    in the CPU model instead of inventing a value
- `HALT` as a terminal post-state:
  - advances `PC` to the next sequential address
  - returns explicit status `cpu-halted`
  - stops bounded runners until interrupt resume is modeled

Current representation rule:
- the current CPU model stores concrete general-register values only at 32-bit owner granularity
- this means:
  - full 32-bit writes are always representable
  - 16-bit and 8-bit writes are only executable when the owning 32-bit register is already known
- example:
  - `ld XSP, 0x00006000` is executable from bootstrap
  - `ld WA, 0x1234` is not executable honestly while `XWA` is still unknown

Current writable stack rule:
- the execution helper can maintain a small in-memory overlay of bytes written by the current instruction
- stack writes still use the address-space map as the authority for "can this target be written at all"
- the current subset explicitly rejects stack targets that land in:
  - unmapped space
  - cartridge ROM
  - cartridge ROM gaps
  - BIOS ROM
- the current CLI can accept manual one-shot register seeding while reset-time values remain unknown:
  - repeatable `--seed-reg XWA=...` .. `--seed-reg XSP=...`
  - convenience hand-off presets such as `--seed-bios-handoff-xsp` and
    `--seed-bios-handoff-minimal`
  - `--seed-xsp` remains as a convenience alias for the most common stack-only case
- stack state is not persisted across separate CLI invocations yet

Current minimal writable-memory rule beyond pure stack ops:
- the same writable runtime overlay now also carries the first representable indexed stores near the current stable bootstrap path
- that overlay also carries the first post-increment byte-copy / zero-fill forms used by the stable official bootstrap loops
- that overlay now also carries the first absolute stores and bit-manipulation writes used by the official bootstrap and tiny init subroutine
- executor-side instruction decode/fetch now also consults that overlay before the read bus, so RAM-resident handlers and vector stubs can execute inside the same run
- this is still a very small subset, not a general RAM or IO write model

Current minimal readable-system rule:
- the read bus now exposes a tiny built-in readable slice for the current stable bootstrap:
  - `0x6F86` defaults to `0x00`
  - `0x6F91` mirrors the ROM header mode byte for the current invocation
- this is intentionally narrow and should not be treated as a general RAM or IO implementation

Current explicit non-goals:
- no general memory writes beyond the current minimal writable overlay for stack, nearby indexed stores and the first post-increment byte loop forms
- no IO writes yet
- no full flags/SR mutation yet
- no full condition evaluation yet
- no persistent multi-step session state from the CLI yet

Current result fields:
- decoded instruction payload
- matched quirk metadata when the current instruction hits one known local quirk
- before-CPU snapshot
- after-CPU snapshot when execution succeeded
- explicit execution status
- list of architectural registers/views written by the instruction
- explicit flag changes when the current subset updates modeled flags
- list of memory-write chunks emitted by the instruction
- current after-memory overlay when the command has one
- per-register before/after changes in CLI/JSON output

Current CLI user:
- `python ngpc_emu.py execute-next <rom>`
- `python ngpc_emu.py execute-next <rom> --address 0x200043`
- `python ngpc_emu.py execute-next <rom> --address 0x20009B`
- `python ngpc_emu.py execute-next <rom> --address 0x2079C6 --seed-xsp 0x4100`
- `python ngpc_emu.py execute-next <rom> --address 0x20D06C --seed-xsp 0x40F4 --seed-reg XIZ=0x12345678`
- `python ngpc_emu.py execute-next <rom> --address 0x20D06D`
- `python ngpc_emu.py execute-next <rom> --address 0x20D098 --seed-reg XIZ=0x00005EBC --seed-reg XBC=0xAABBCC42`

Important:
- this is the first real state-mutation helper, not a full interpreter
- a decoded instruction can still be non-executable if its side effects are not modeled honestly
- writable runtime memory is now partially modeled, but only for a narrow subset and only within the current command
- interrupts, most flags/SR updates, general memory/IO writes and most branch-condition evaluation remain outside the current subset
- `HALT` is now modeled narrowly: the instruction itself completes, advances `PC`, and then stops as `cpu-halted`
- the command does not keep state across invocations; each run starts from the current bootstrap model plus an optional explicit `PC`

Not implemented yet:
- full fetch/decode/execute loop
- full stack and call semantics, including the remaining alias cases around `SP` / `XSP`
- full flags and condition evaluation
- general memory and IO writes
- multi-step run control
- true debugger-grade stepping
