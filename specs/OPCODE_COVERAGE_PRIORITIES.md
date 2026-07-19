# Opcode coverage priorities (pass 50, 2026-05-20)

Empirical census of decoder gaps across the local NGPC ROM corpus,
captured to prioritise executor expansion work for HW fidelity.

Method : `python ngpc_emu.py opcode-coverage <rom> --bytes N`
linearly walks the ROM body, decodes each address, advances by
`length` on success / by 1 on failure (recording the failing
leading byte). The 1-byte advance on failure produces some noise in
the long tail (a single unknown instruction's operand bytes get
counted as "misses"), but the TOP entries are real signal.

## Corpus and methodology

| ROM                       | Walk budget | Decoded instr | Coverage |
|---------------------------|-------------|---------------|----------|
| minimal_template/main.ngc | 2048 bytes  | 753           | 93.0%    |
| HORATIO.ngp               | 2048 bytes  | 780           | ~88%     |
| POCKETRACE.ngp            | 2048 bytes  | 715           | ~90%     |
| MRROBOT.ngp               | 2048 bytes  | 775           | ~90%     |

Coverage % is "bytes decoded / bytes walked". The remaining 7-12%
are the unknown-opcode misses (true blockers + their operand-byte
fallout).

## Aggregate top unknown leading-bytes (across 4 ROMs, 8192 bytes)

**Provisional interpretations — verify against ngdis output before
implementing.** The TLCS-900/H mem-field encoding is non-trivial
(prefix bytes span across multiple addressing modes via the mem
nibble + bit 6 of the leading byte) ; an initial reading suggested
the C0..C7 family was "byte mem op on (R32)" but verification
against `ngdis/tlcs900_zz_r.c` showed `0xC7 0xFB` actually decodes
as **`RR A, r`** (rotate-right register, where `r` is encoded by
the low 3 bits of the prefix byte, with `r=7` requiring an
extension byte after the sub-op).

| Byte  | Count | Provisional family | Status |
|-------|-------|--------------------|--------|
| `0xC7` | 100  | `decode_zz_r` (R direct, byte size, r-extension follows sub-op) | Needs verification |
| `0x99` | 21   | byte mem op on (XBC+d8) (= ARID family C) | Partial existing |
| `0xF0` | 20   | long mem op on (abs8) / B0+mem family | Partial existing |
| `0xE1` | 16   | `decode_zz_r` long variant ? | Needs verification |
| `0xD8` | 16   | long ALU register prefix bank-working | Partial broken-marker |
| `0xE3` | 15   | `decode_zz_r` long variant ? | Needs verification |
| `0xE8` | 15   | long ALU register prefix (other variant) | Partial existing |
| `0xF3` | 15   | mem op on absolute (secondary indexed) | Partial F3 handling exists |
| `0x88` | 15   | byte mem op on (XWA+d8) — ARID byte | Partial existing |
| `0x81` | 15   | byte mem op on (XBC) — ARI byte | Maybe partial |
| `0x91` | 14   | word mem op on (XBC+d8) — ARID word | Partial existing |
| `0x89` | 14   | byte mem op on (XBC+d8) — ARID byte | Partial existing |
| `0x04` | 14   | likely operand-byte fallout from a prior unknown | Noise |

## Family grouping (CORRECTED from initial reading)

The TLCS-900/H first-byte encoding is *not* a clean mapping
"prefix-byte → R32 base". The `mem` field is computed as
`((b & 0x40) >> 2) | (b & 0x0F)` and selects across :

| mem value | meaning              |
|-----------|----------------------|
| 0..7      | `(R32)` ARI register indirect, R32 picked by mem |
| 8..15     | `(R32+d8)` ARID register indirect+disp8 |
| 16        | `(abs8)` ABS_B |
| 17        | `(abs16)` ABS_W |
| 18        | `(abs24)` ABS_L |
| 19        | secondary `(R32+d16)` / `(R32+R8)` / `(R32+R16)` |
| 20        | `(-R32)` ARI_PD pre-decrement |
| 21        | `(R32+)` ARI_PI post-increment |
| 23..31    | R direct (operand is R8/R16/R32 register, no memory) |

The `zz` field `(b & 0x30) >> 4` selects size : 0=byte, 1=word,
2=long, 3=B0+mem special branch (no zz).

So leading bytes don't map 1:1 to families ; instead each leading
byte combines a *size class* with an *addressing mode*. For example:

- `0xC7` : zz=0 (byte), mem=0x17 = **R direct** (not memory at all !) —
  `r-extension follows sub-op` because low 3 bits = `0x07`. The
  instruction is e.g. `RR A, r` (rotate-right shift on register `r`,
  amount in A).
- `0xE3` : zz=2 (long), mem=0x13 = **secondary indexed mode** —
  the byte after is the secondary mode selector with bits[1:0]
  picking `(R32)` / `(R32+d16)` / `(R32+R8/R16)`.
- `0x81` : zz=0 (byte), mem=0x01 = `(XBC)` register indirect — this
  IS a `(R32)` byte memory access.

### Real families per-mem-value (not per-prefix-byte)

- **mem 0..7 (ARI)** : leading bytes `0x80..0x87` (byte),
  `0x90..0x97` (word), `0xA0..0xA7` (long), `0xB0..0xB7` (B0+mem
  zz=3 special). Existing decoder handles `0x80..0x87` and
  `0xB0..0xB7` partially.
- **mem 8..15 (ARID +d8)** : leading bytes `0x88..0x8F` (byte) etc.
  Existing decoder partial.
- **mem 16..21 (absolute / pre-dec / post-inc)** : leading bytes
  `0xC0..0xC5` (byte), `0xD0..0xD5` (word), `0xE0..0xE5` (long),
  `0xF0..0xF5` (B0+mem). Existing decoder handles some `0xF0..0xF7`
  in B0+mem branch.
- **mem 19 (secondary indexed)** : leading bytes `0xC3`, `0xD3`,
  `0xE3`, `0xF3`. `0xF3` partial existing.
- **mem 23..31 (R direct)** : leading bytes `0xC8..0xCF` (byte R8,
  existing), `0xD8..0xDF` (word R16, partial broken-marker),
  `0xE8..0xEF` (long R32, partial existing). And the **r=7 cases
  `0xC7`/`0xD7`/`0xE7`/`0xF7`** use an extension byte after the
  sub-op to select the actual register.

## Recommended chantier ordering (REVISED)

Given the corrected family map, prioritise by **mem mode** not
prefix byte :

1. **R direct family with r-extension** (`0xC7`/`0xD7`/`0xE7`/`0xF7`)
   — covers the dominant `0xC7` count (100). Source :
   `ngdis/tlcs900_zz_r.c` (the extension case: `getr()` returns -1, the
   next byte is the register selector `r`, and `getregs(r<0)` returns the
   256-entry `r8_names` table).
   **DONE for 0xC7 (pass 57).** Correction to the earlier reading: the
   C7 extension byte is *not* "shift amount in A" — it indexes the
   authoritative `r8_names` register-code table directly. Codes
   0xE0..0xFF are current-bank byte slices of XWA..XSP (e.g. QC = XBC
   bits 16..23, QIZH = XIZ bits 24..31) and now decode + execute for
   real (LD / ALU / CP / INC / DEC; shifts and single-r forms decode but
   block on execute). Codes 0x00..0x3F (explicit bank) and 0xD0..0xDF
   (previous bank) block with `requires-register-banks` pending the
   multi-bank register file. NOTE: the Python `ngpc_disasm.py` oracle's
   own C7 case is wrong here (high-nibble-as-bank → bogus `rH14`); trust
   the C source. After pass 57, `0xC7` (and its operand-byte fallout
   `0xE6`) is gone from the top blockers; `0xF3` (secondary-indexed mem)
   is now the leading gap. 0xD7/0xE7/0xF7 (word/long/index r-extension)
   remain.

2. **ARID (+d8) extension** (mem 8..15) — leading bytes
   `0x88..0x8F` byte, `0x98..0x9F` word, `0xA8..0xAF` long. Partial
   support exists ; extend the sub-op coverage. Helps `0x99` (21
   occurrences) and similar.

3. **ABS / secondary mode** (mem 16..21) — leading bytes
   `0xC0..0xC5` (byte), `0xE0..0xE5` (long), etc. Helps `0xE3` and
   `0xF3` clusters.

**Verification step before each implementation** : take an actual
byte sequence from the corpus (e.g. `0xC7 0xFB 0xXX`) and decode
it with ngdis (compile + run on the bytes) to confirm the
mnemonic + operand interpretation before writing the executor.
Without this verification, half the encoding interpretations from
catalog reading turn out wrong (as discovered when initial reading
mis-classified `0xC7` as "byte mem op on (XSP)" when it's actually
"R direct with r-extension").

## Doctrine

- **Source every opcode encoding from ngdis** — it's the in-project
  authoritative reference (per `T900_DENSE_REF.md`) ; figures that
  merely circulate are ruled out for cycle counts, but ngdis is fine
  for encoding tables since it's a direct port of the Toshiba spec.
- **Tests round-trip** : for each new opcode, decode → re-encode
  bytes → assert equality + execute → assert observable state
  change matches expected effect from the encoding spec.
- **No silent stubs** : either fully decode + execute, or return
  `not-modeled-yet` with the leading-byte known. Half-execution
  corrupts state silently.

## How to re-run this analysis

```
python ngpc_emu.py opcode-coverage <rom> [--start ADDR] [--bytes N] [--top N] [--json]
```

After implementing a family, re-run on the same ROMs to confirm
the targeted bytes disappear from the top of the histogram.
