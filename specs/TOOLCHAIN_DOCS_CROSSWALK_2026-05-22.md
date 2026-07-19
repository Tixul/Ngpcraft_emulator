# Toolchain docs crosswalk for NgpCraft_emulator

Date: 2026-05-22

Purpose: identify which documents and disassembly assets under
`../NgpCraft_toolchain` and
`../NgpCraft_toolchain/NgpCraft_Toolchain_v2/docs`
are immediately useful for emulator work, and which ones are mostly
toolchain-only.

## Highest-value documents for the emulator

### 1. `06b_MEMFORM_ALU.md`

Most useful near-term CPU document.

Why it matters:
- It gives a concrete catalog of the `80..AF + sub-op` memory-form ALU
  families, including ARI, ARID, abs, pre-dec, post-inc and secondary
  indexed forms.
- It explicitly maps the current decoder gaps to real byte families,
  especially `0x88..0x8F`, `0x98..0x9F`, `0xA8..0xAF`, `0xC0..0xC5`,
  `0xD0..0xD5`, `0xE0..0xE5`, `0xF0..0xF5`.
- It aligns with the emulator's existing priority note in
  `OPCODE_COVERAGE_PRIORITIES.md`: ARID first, then absolute/secondary.

Immediate emulator uses:
- Extend decode/execute coverage for ARID word/byte/long ALU forms.
- Clarify that `0xD0..0xD5` memory-family forms must not be confused with
  the broken `0xD0..0xD7` register-prefix family.
- Use the sub-op tables as a checklist when closing `unknown-opcode`
  clusters after `0xC7`.

### 2. `08_VERIFIED_FINDINGS.md`

Most important quirk-policy update.

Why it matters:
- It states that `add XHL, XWA` / `add HL,WA` is observed as `D8 83`,
  used by CC900, and should not be treated as the previously assumed
  broken form.
- It also states that `ld XHL, XWA` (`D8 8B`) remains inconclusive, so
  the safe conclusion is not "all D8..DF r+r is safe", but also not
  "all D8..DF sub-op >= 0x80 is broken".
- It recommends splitting "HW-confirmed broken" from "avoided by CC900"
  in the quirk database.

Immediate emulator uses:
- Re-audit `core/quirks_db.json` rule `cpu.d8_df_register_to_register`.
- Treat `D8 83` as a targeted contradiction against the current broad
  matcher until hardware or larger corpus validation refines the rule.

### 3. `06_OPCODE_ENCODING.md`

Useful as the encoding doctrine and index.

Why it matters:
- It defines the authoritative encoding sources order:
  our own `ngpc_disasm.py`, then HW-validated tables, then `ngdis`.
- It documents the critical correction from the old guessed
  `D3 88` reading to the observed `D8 83` encoding.
- It points to the exact decoder families mirrored in the emulator:
  `decode_fixed`, `decode_xx`, `decode_zz_r`, `decode_zz_mem`,
  `decode_B0_mem`.

Immediate emulator uses:
- Good reference when a new opcode family needs verification before
  implementation.
- Useful for keeping decoder terminology consistent with the toolchain.

### 4. `04_RUNTIME_ABI.md`

Useful for real-ROM behavior, not for raw opcode decoding.

Why it matters:
- Confirms CC900-style calling convention through `(XSP+N)`.
- Confirms leaf functions commonly avoid `link/unlk`.
- Confirms caller-side stack cleanup patterns (`inc/add XSP`).

Immediate emulator uses:
- Helps explain why official-toolchain ROMs lean so heavily on XSP-
  relative loads, compares, pushes and mem-form ALU.
- Good guidance for building focused ROM probes that resemble compiler
  output.

## Medium-value documents

### `01_DISASM_INDEX.md`

Useful mainly as navigation glue for the big HTML disassemblies.

Best emulator use:
- Find `asmCCALL`, `asmARI`, `asmADDR`, leaf-function handlers and the
  register allocator areas in `thc2.exe_disasm.html`.
- Use it when you need to answer "does CC900 really emit this pattern?"
  rather than "what is the ISA encoding?"

### `05_ASM_SYNTAX.md`

Useful when crafting minimal reference snippets for `asm900`/`thc2`
round-trips, but not a major emulator source by itself.

### `02_TAC_IR_SPEC.md` and `00_STRATEGY.md`

Mostly toolchain-facing. Helpful if the emulator eventually grows a
test ROM generator from TAC or if we want a corpus of compiler-emitted
patterns, but not directly useful for CPU/GPU correctness today.

## Low-value documents for the emulator

### `03_THC2_CODEGEN_ARCH.md`, `03b_THC2_DECISION.md`, `07_PHASES.md`,
`07b_REPRODUCTION_PLAN.md`, `CLEANROOM_POLICY.md`

These are mostly build strategy and backend architecture notes.

They are worth reading only when:
- refining the quirk policy from emitted-code evidence,
- deciding which CC900-emitted patterns deserve dedicated emulator tests,
- or coordinating emulator/toolchain work.

They are not primary sources for instruction semantics.

## Disassembly assets: actual usefulness

Available under `../NgpCraft_toolchain`:
- `thc1.exe_disasm.html`
- `thc2.exe_disasm.html`
- `asm900.exe_disasm.html`
- `tulink.exe_disasm.html`
- `tuconv.exe_disasm.html`
- `cc900.exe_disasm.html`

### Priority for emulator work

1. `asm900.exe_disasm.html`
   Best candidate when ISA encoding is ambiguous and existing docs are
   insufficient. The assembler is closer to instruction encoding truth
   than the C frontend/backend.

2. `thc2.exe_disasm.html`
   Useful to learn which opcode families the official backend actually
   emits, which ABI patterns are common, and which "safe in practice"
   forms should appear in CC900-built ROMs.

3. `cc900.exe_disasm.html`
   Minor value. Mostly helps recover command-line orchestration or
   default flags.

4. `thc1.exe_disasm.html`
   Low value for the emulator. Its TAC output matters to the toolchain,
   but the frontend internals do not help much with CPU/GPU emulation.

5. `tulink.exe_disasm.html` and `tuconv.exe_disasm.html`
   Very low value unless object/link/container behavior becomes the
   blocker.

## Concrete contradictions with the current emulator state

### 1. Broad `D8..DF` broken matcher is now too aggressive

Current emulator quirk entry:
- `core/quirks_db.json` rule `cpu.d8_df_register_to_register`

Problem:
- Current policy effectively stops on `D8 83`.
- `08_VERIFIED_FINDINGS.md` says `D8 83` is observed in CC900 output and
  should be considered safe-in-practice.

Recommended next step:
- Do not blindly delete the quirk.
- Narrow it with evidence: special-case confirmed-safe forms first, then
  revise the family once hardware or larger corpus evidence is gathered.

### 2. ARID and secondary-indexed memory families remain the best ROI

This matches the emulator's own current roadmap:
- after `0xC7`, the practical blockers are still ARID/secondary forms,
  not frontend/compiler internals.

Recommended next step:
- keep targeting `0x88..0x8F`, `0x98..0x9F`, `0xA8..0xAF`, `0xE3`, `0xF3`
  before spending time on `thc1` internals.

## Best practical workflow

For emulator opcode work, the best source order is:

1. Reproduce a failing real ROM byte sequence via
   `python ngpc_emu.py opcode-coverage <rom>`.
2. Confirm the exact family with `NgpCraft_Disasm/ngpc_disasm.py`.
3. Use `06b_MEMFORM_ALU.md` or `06_OPCODE_ENCODING.md` to place it in the
   correct family.
4. Only if still ambiguous, inspect `asm900.exe_disasm.html`.
5. Use `thc2.exe_disasm.html` to answer whether CC900 emits the form and
   how often it is likely to appear in compiler-generated code.

## Bottom line

Most useful immediately:
- `06b_MEMFORM_ALU.md`
- `08_VERIFIED_FINDINGS.md`
- `06_OPCODE_ENCODING.md`
- `04_RUNTIME_ABI.md`

Most useful disassembly asset:
- `asm900.exe_disasm.html` for encoding truth
- `thc2.exe_disasm.html` for emitted-code reality

Least useful for the emulator right now:
- `thc1.exe_disasm.html`

