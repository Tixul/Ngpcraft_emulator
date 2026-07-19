# TLCS-900/H — the memory-operand instruction families

> Implementation-ready reference for the C++ core (`cpp/src/execute.cpp`).
> Built 2026-07-11 by cross-checking **four independent authorities**:
> the official Toshiba assembler (`asm900.exe`), the Toshiba TLCS-900/L1
> datasheet, our own Python reference core, and `ngdis`.
>
> **Where they disagree, the ranking in `CPP_CORE_PORT.md` §4-bis applies:
> asm900 for encodings, the datasheet for cycles and flags.**

---

## 1. The encoding map (first opcode byte ≥ 0x80)

```
zz  = (b & 0x30) >> 4                  0=byte  1=word  2=long  3=DESTINATION group
mem = ((b & 0x40) >> 2) | (b & 0x0F)   0..21 = memory modes;  >=23 = register-direct
```
Then the **next byte** (after the addressing mode's operand bytes) is the
**sub-opcode**, which selects the operation.

| First byte | Group |
|---|---|
| `0x80..0x8F` / `0x90..0x9F` / `0xA0..0xAF` | **source** group, size byte / word / long, modes 0..15 |
| `0xC0..0xC5` / `0xD0..0xD5` / `0xE0..0xE5` | **source** group, same sizes, modes 16..21 |
| `0xB0..0xBF` **and `0xF0..0xF5`** | **destination** group (stores, `ret cc`, indirect `jp`/`call`) |
| `0xC7` / `0xD7` / `0xE7` | register-direct escape (`rCode` byte follows) |
| `0xC8..0xCF` / `0xD8..0xDF` / `0xE8..0xEF` | register-direct |
| `0xC6` / `0xD6` / `0xE6` / `0xF6` | **invalid** |
| `0xF7` | **`LDX`** — NOT a destination form |
| `0xF8..0xFF` | **`SWI n`** — NOT a destination form |

> ⚠️ The destination group stops at **`0xF5`**. Assuming `0xF0..0xFF` are all
> stores is wrong and would swallow `LDX` and every `SWI`.

---

## 2. Addressing modes, and the CYCLE ADDER

Cycles are **not** flat per instruction. The real model is:

> ### `cycles = base(instruction) + extra(addressing mode)`

**Extra states — Toshiba instruction list (10) "Addressing mode".** This table is
the authority; the two columns beside it disagree with it and with each other.

| mem | Form | **Toshiba (USE THIS)** | figures in circulation | our Python core |
|---|---|---|---|---|
| 0..7 | `(R)` | **+0** | 0 ✅ | 0 ✅ |
| 8..15 | `(R+d8)` | **+1** | 2 ❌ | 0 ❌ |
| 16 | `(#8)` abs8 | **+1** | 2 ❌ | 0 ❌ |
| 17 | `(#16)` abs16 | **+2** | 2 ✅ | 0 ❌ |
| 18 | `(#24)` abs24 | **+3** | 3 ✅ | 0 ❌ |
| 19 | `(r)` | **+1** | 5 ❌ | — |
| 19 | `(r+d16)` | **+3** | 5 ❌ | — |
| 19 | `(r+r8)` / `(r+r16)` | **+3** | 8 ❌ | — |
| 20 | `(-r)` pre-decrement | **+1** | 3 ❌ | — |
| 21 | `(r+)` post-increment | **+1** | 3 ❌ | — |

**⇒ Our Python core applies NO addressing-mode adder at all**, so every
memory-operand cycle count it reports is too low. The native core implements the
datasheet model and is therefore *more accurate than the reference here*. This is
a known, deliberate, documented divergence — not a port bug.

### Effective-address computation

| mem | Operand bytes | EA |
|---|---|---|
| 0..7 | — | `R32[mem]` |
| 8..15 | `d8` | `R32[mem-8] + (int8)d8` |
| 16 | `n8` | `n8` (zero-extended) |
| 17 | `n16` | `n16` |
| 18 | `n24` | `n24` |
| 19 | secondary byte `data`, then more | see below |
| 20 | `data` | pre-decrement, see below |
| 21 | `data` | post-increment, see below |

**Mode 19 — secondary byte `data`:**
| `data` | Form | EA | Extra bytes |
|---|---|---|---|
| `0x03` | `(r32 + r8)` | `R32[b>>2] + (int8) R8[idx>>2]` | 2 more bytes (r32 code, index code) |
| `0x07` | `(r32 + r16)` | `R32[b>>2] + (int16) R16[idx>>2]` | 2 more |
| `0x13` | **`pc + (int16)d16`** — *undocumented*, this is how `LDAR` works | | 2 more |
| else, `(data & 3) == 1` | `(r32 + d16)` | `R32[data>>2] + (int16)d16` | 2 more |
| else | `(r32)` | `R32[data>>2]` | 0 |

Register selection always uses `data >> 2` (the low 2 bits are the sub-mode).

**Modes 20 / 21 — pre-decrement and post-increment.** ⚠️ **The step comes from the
LOW 2 BITS OF THE OPERAND BYTE (1 / 2 / 4), *not* from the instruction's `zz`
size field.** Register = `data & 0xFC` (i.e. `data >> 2`).

| `data & 3` | step |
|---|---|
| 0 | 1 |
| 1 | 2 |
| 2 | 4 |
| 3 | **undefined** — a naive implementation just leaves `mem` stale (a latent bug; we must trap) |

- **pre-decrement (mode 20):** `R -= step`, then `EA = R` (the *new* value).
- **post-increment (mode 21):** `EA = R` (the *old* value), then `R += step`.

---

## 3. SOURCE group sub-opcodes (memory as source; `zz` = 0/1/2)

Base cycles below are the per-handler base costs in circulation; **add the Toshiba mode extra from §2.**

| Sub-op | Operation | Extra imm | Base cycles (B / W / L) |
|---|---|---|---|
| `0x04` | `PUSH (mem)` | — | 7 — ⚠️ **no long form** (implementations bill 7 and do nothing) |
| `0x06` / `0x07` | `RLD` / `RRD A,(mem)` | — | 12 (byte only) |
| `0x10`/`0x12` | `LDI` / `LDD` | — | 10 |
| `0x11`/`0x13` | `LDIR` / `LDDR` | — | 10 + 14·iterations |
| `0x14`/`0x16` | `CPI` / `CPD` | — | 8 |
| `0x15`/`0x17` | `CPIR` / `CPDR` | — | 10 + 14·iter |
| `0x19` | `LD (nn),(mem)` | +2 | 8 |
| **`0x20..0x27`** | **`LD R,(mem)`** | — | **4 / 4 / 6** |
| **`0x30..0x37`** | **`EX (mem),R`** | — | 6 — ⚠️ **byte/word only, no long** |
| `0x38..0x3F` | `ADD/ADC/SUB/SBC/AND/XOR/OR/CP (mem),#` | +1 / +2 | 7 / 8 (`CP` = 6) |
| `0x40..0x47` / `0x48..0x4F` | `MUL` / `MULS RR,(mem)` | — | 18 / 26 |
| `0x50..0x57` / `0x58..0x5F` | `DIV` / `DIVS RR,(mem)` | — | 22/30 · 24/32 |
| `0x60..0x67` / `0x68..0x6F` | `INC` / `DEC #3,(mem)` — **n=0 means 8** | — | 6 |
| `0x78..0x7F` | `RLC RRC RL RR SLA SRA SLL SRL (mem)` | — | 8 |
| `0x80..0x87` / `0x88..0x8F` | `ADD R,(mem)` / `ADD (mem),R` | — | 4/4/6 · 6/6/10 |
| `0x90..0x9F` | `ADC` (same split) | — | 4/4/6 · 6/6/10 |
| `0xA0..0xAF` | `SUB` | — | 4/4/6 · 6/6/10 |
| `0xB0..0xBF` | `SBC` | — | 4/4/6 · 6/6/10 |
| `0xC0..0xCF` | `AND` | — | 4/4/6 · 6/6/10 |
| `0xD0..0xDF` | `XOR` | — | 4/4/6 · 6/6/10 |
| `0xE0..0xEF` | `OR` | — | 4/4/6 · 6/6/10 |
| **`0xF0..0xF7`** | **`CP R,(mem)`** | — | 4 / 4 / 6 |
| **`0xF8..0xFF`** | **`CP (mem),R`** | — | 6 / 6 / **6** ⚠️ (long is 6, not 10) |

Everything else is invalid.

---

## 4. DESTINATION group sub-opcodes (`zz == 3`: `0xB0..0xBF`, `0xF0..0xF5`)

The sub-opcode carries its own size; the first byte only carries the addressing mode.

| Sub-op | Operation | Extra imm | Base cycles |
|---|---|---|---|
| `0x00` / `0x02` | `LD (mem),#8` / `LD (mem),#16` | +1 / +2 | 5 / 6 |
| `0x04` / `0x06` | `POP (mem)` byte / word | — | 6 |
| `0x14` / `0x16` | `LD (mem),(nn)` byte / word | +2 | 8 |
| `0x20..0x27` | `LDA R,mem` (word reg) | — | 4 |
| `0x28..0x2C` | `ANDCF/ORCF/XORCF/LDCF/STCF A,(mem)` | — | 8 |
| `0x30..0x37` | `LDA R,mem` (long reg) — with mode-19 `data=0x13` this is **`LDAR`** | — | 4 |
| **`0x40..0x47`** | **`LD (mem),R` — BYTE store** | — | **4** |
| **`0x50..0x57`** | **`LD (mem),R` — WORD store** | — | **4** |
| **`0x60..0x67`** | **`LD (mem),R` — LONG store** | — | **6** |
| `0x80..0xA7` | `ANDCF/ORCF/XORCF/LDCF/STCF #3,(mem)` | — | 8 |
| `0xA8..0xAF` | `TSET #3,(mem)` | — | 10 |
| `0xB0..0xC7` | `RES` / `SET` / `CHG #3,(mem)` | — | 8 |
| `0xC8..0xCF` | `BIT #3,(mem)` | — | 8 |
| `0xD0..0xDF` | `JP cc,mem` | — | taken 9 / not 6 |
| `0xE0..0xEF` | `CALL cc,mem` | — | taken 12 / not 6 |
| `0xF0..0xFF` | **`RET cc`** — pops PC, **ignores `mem` entirely** (but still consumes its operand bytes and pays its extra) | — | taken 12 / not 6 |

> **This is where the stores live.** `LD (mem),R` is `0x40/0x50/0x60 + r`, in the
> DESTINATION group — *not* sub-op `0x30` of the source group, which is `EX`.

---

## 5. Flags

Bit layout of `F`: `S=0x80  Z=0x40  H=0x10  V=0x04  N=0x02  C=0x01`.

| Op | S | Z | H | V | N | C |
|---|---|---|---|---|---|---|
| `ADD`/`ADC` byte, word | msb | `==0` | half-carry | signed overflow | 0 | carry |
| `ADD`/`ADC` **long** | msb | `==0` | **untouched** | signed overflow | 0 | carry |
| `SUB`/`SBC`/`CP` byte, word | msb | `==0` | half-borrow | signed overflow | 1 | borrow |
| `SUB`/`SBC`/`CP` **long** | msb | `==0` | **untouched** | signed overflow | 1 | borrow |
| **`AND`** | msb | `==0` | **1** | **PARITY** (byte/word); **untouched for LONG** | 0 | 0 |
| **`OR`** / **`XOR`** | msb | `==0` | **0** | **PARITY** (byte/word); **untouched for LONG** | 0 | 0 |
| `INC #n,(mem)` | msb | `==0` | half | overflow | 0 | **untouched** |
| `DEC #n,(mem)` | msb | `==0` | half | overflow | 1 | **untouched** |
| `RLC/RRC/SLA/SRA/SLL/SRL` | msb | `==0` | 0 | parity | 0 | shifted-out bit |
| `RL` / `RR` | msb | `==0` | **untouched** | parity | **untouched** | shifted-out bit |
| `LDI/LDIR/LDD/LDDR` | — | — | 0 | **`BC != 0`** after `--BC` | 0 | — |
| `CPI/CPD/CPIR/CPDR` | from SUB | from SUB | from SUB | **`BC != 0`** (overrides) | 1 | from SUB |
| `MUL`/`MULS` | — | — | — | — | — | — (no flags) |
| `DIV`/`DIVS` | — | — | — | V=1 on div-by-0 / overflow | — | — |
| `BIT`/`TSET` | — | `!(m & bit)` | 1 | — | 0 | — |
| `RES`/`SET`/`CHG` | — | — | — | — | — | — (no flags) |
| `ANDCF/ORCF/XORCF/LDCF/STCF` | — | — | — | — | — | **C only** |

**V is the PARITY flag for the logical ops** (even population count → V=1) — and
**`AND` SETS H**. Both were corrected in our Python core on 2026-07-09 by triage
against a reference trace, and both are confirmed here. **In LONG size, the logical ops leave
V untouched** (there is no 32-bit parity).

---

## 6. Traps to honour (do not "tidy" these)

1. `PUSH (mem)` and `EX (mem),R` have **no long form**.
2. `CP (mem),R` costs **6 even in long** (every other `(mem),R` ALU op costs 10).
3. `INC/DEC #3,(mem)` with `n == 0` means **8**, not 0.
4. `RET cc` still **consumes the addressing-mode operand bytes** and pays their
   cycle extra, then throws the address away.
5. `LDIR/LDDR/CPIR/CPDR` loop **inside** the instruction — no interrupt can be
   taken mid-block.
6. `CPI/CPD/CPIR/CPDR` take their pointer register from the **FIRST** byte, not
   the second.
7. Pre-dec / post-inc step `data & 3 == 3` is **undefined** — a naive
   implementation silently reuses a stale address. **We trap.**
8. `ANDCF/ORCF/… A,(mem)` are **no-ops when `A & 0xF >= 8`**.

---

## The `RR` code of MUL / MULS / DIV / DIVS is NOT a register index

This one silently corrupted BOTH cores, identically, for as long as they have
existed — which meant the differential gate could never see it. The **official
Toshiba assembler** is what convicted it.

Toshiba states the table three times (`<Divide>` Note 3, and again for
`DIV rr,#`), once for each operand form, and it says the note governs
`DIV RR,r` **and** `DIV RR,(mem)`:

| operation size | destination is a… | codes |
|---|---|---|
| **word** (`D8+r`) | LONG register | `000`=XWA `001`=XBC `010`=XDE `011`=XHL `100`=XIX `101`=XIY `110`=XIZ `111`=XSP |
| **byte** (`C8+r`) | WORD register | `001`=WA `011`=BC `101`=DE `111`=HL — **the even codes name nothing** |

So at byte size the register index is `code >> 1`, and an even code is not a
legal encoding at all. Read as an array index instead:

* `div WA,0x18` (`C9 0A 18`) divided **XBC**,
* `mul DE,(XHL+)` (`C5 EC 45`) multiplied **XIY**,
* `muls BC,7` (`CB 09 07`) wrote **XHL**.

The assembler refuses to emit the even forms, and refuses `mul IY,(XHL+)`
outright — IY is not a reachable destination at that size, which is the tell that
ngdis's name for `C5 EC 45` was invented.

**Lesson.** A differential harness proves two implementations AGREE. It cannot
prove they are RIGHT. Only an oracle outside both of them can, and `asm900.exe`
is that oracle.

## Cycles: the register forms are NOT the memory forms

The memory form costs two states more. Both are in the instruction lists; do not
reuse one for the other.

| | byte | word | | byte | word |
|---|---|---|---|---|---|
| `MUL RR,r`   | 11 | 14 | `MUL RR,(mem)`   | 13 | 16 |
| `MULS RR,r`  |  9 | 12 | `MULS RR,(mem)`  | 11 | 14 |
| `DIV RR,r`   | 15 | 23 | `DIV RR,(mem)`   | 16 | 24 |
| `DIVS RR,r`  | 18 | 26 | `DIVS RR,(mem)`  | 19 | 27 |

Flags: **MUL and MULS write NOTHING** (`- - - - - -`). **DIV and DIVS write V and
nothing else** (`- - - V - -`).

## Divide by zero and quotient overflow are DEFINED

> "V = 1 is set when divided by 0 or the quotient exceeds the numerals which can
> be expressed in bits of dst; otherwise, 0 is set." — `<Divide>`, flag row.

Toshiba defined a flag for exactly these two cases, which means **the program is
meant to run straight through them and test V**. Three commercial ROMs do.
Trapping is wrong.

## Other instructions this file did not cover

| | encoding | note |
|---|---|---|
| `LD<W> (mem),(#16)` | `B0 + mem : 14 + z : #16` | memory-to-memory move. No flags. **8 states + M.** The BIOS interrupt handlers use it. |
| `LD<W> (#16),(mem)` | `80 + zz + mem : 19 : #16` | the mirror form, sub-op `0x19`. 8 states + M. |
| `DAA r` | `C8 + r : 10` | **BYTE ONLY.** 4 states. Flags `* * * P - *` — V is the PARITY. The manual's 13-row correction table is reproduced exactly by the usual two-nibble rule, which also extends it to inputs no BCD add can produce. |
| `MINC1/2/4 #,r` | `D8 + r : 38/39/3A : (# - step)` | **WORD ONLY.** 5 states, no flags. The immediate is **`modulus - step`**, not the modulus. |
| `MDEC1/2/4 #,r` | `D8 + r : 3C/3D/3E : (# - step)` | **WORD ONLY.** 4 states, no flags. |
| `CALL cc,(mem)` | destination group `0xE0..0xEF` | **The effective address IS the target.** `<Call>`: "if cc then PUSH PC: PC ← dst". It is NOT a pointer to dereference — the reference core did, and sent two ROMs to an unmapped address. |

---

# ⚠️ OPEN HARDWARE QUESTIONS

Neither the assembler nor a disassembler can answer these — an encoding oracle
cannot reveal a run-time semantic. A **test ROM on real silicon** is the arbiter,
exactly as it was for `D0..D7` and `D8..DF`.

### 1. Is the register index of `(r32 + r8)` / `(r32 + r16)` SIGNED?

Toshiba calls it "the register specified as the 8- or 16-bit **displacement**"
(CPU manual, "Register Index Addressing Mode"), and every displacement the manual
gives a range for is signed (`(r + d16)`: "−8000H to +7FFFH"). But it gives **no
negative example** for the register form and **states no range** for it.

*Both cores currently sign-extend.* If the hardware zero-extends instead, every
`(XIX + A)` table lookup with A ≥ 0x80 reads 256 bytes away from where we think.

**Test ROM:** `ld XIX,base; ld A,0xFF; ld (XIX+A),B` → does the byte land at
`base+0xFF` or at `base−1`?

### 2. What lands in `dst` on a divide by zero or a quotient overflow?

The manual defines the FLAG (see above) and says nothing about the destination.

*The native core* keeps the low half the datapath would have produced, and on a
divide by zero — where no quotient exists at all — leaves `dst` untouched.
*The Python reference* declines to guess, which is what an analysis core should
do; the differential gate records that as `ref-declines-undefined` rather than a
divergence.

**Test ROM:** `ld WA,0x1234; ld B,0; div WA,B` → dump WA.
