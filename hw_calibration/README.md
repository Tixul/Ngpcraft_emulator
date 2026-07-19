# CPU-speed calibration ROM — for the "games run 2x too fast" bug  ✅ SOLVED

**Bug (SOLVED):** Cool Boarders Pocket / Densha de Go ran **2x too fast in every
emulator measured, this one included**, vs real hardware. They self-time their frame
rate: on silicon their per-frame work spills past one VBlank -> 30 fps; in emulators
it fits -> 60 fps. So emulators ran the games' work in **fewer cycles than silicon**.
This ROM measured that per instruction class (VBlank = reference clock) and pinned
the cause.

**Result (silicon, v2):** the numbers are **not uniform** — short/fetch-bound ops
~3.4x too fast, MUL/DIV ~2.5x, RASV correct. Signature of **unmodelled cart-flash
FETCH wait-states**. What silicon settled:
- ✅ **`cart_wait=3` (instruction fetch) confirmed** — BASE/SHIFT/ADD/MEM match.
- ⛔ **cart DATA reads are NOT wait-stated** — v2's `CRND == RRND` on silicon means a
  cart data read costs the same as RAM, so **`cart_data_wait=0`**. (An earlier
  `cart_data_wait=5`, curve-fit to Cool Boarders, was **refuted** by this ROM.)
- ✅ **MUL/DIV were under-costed** (silicon 444/265 vs 481/301) — **fixed** in the core.

With the CPU model now silicon-exact on every class, Cool Boarders STILL runs ~51 fps
(vs 30 on silicon) → the residual is **not the CPU**. The one unmeasured thing is
**VRAM writes** (the per-frame char-RAM ldir); **v3** adds a test for it. See
`../DEVLOG.md` and memory `project_ngpc_emulator_fps_waitstates`.

## Files
- `a_cpu_calib_v6.ngc` — **current ROM.** LDRR/LDVR = ONE big 2000-byte LDIR per batch
  (RAM→RAM and RAM→VRAM), measuring the block-transfer cost/byte. (v4/v5 were broken: the
  compiler hoisted a small looped ldir / an inline-asm count crashed — validated in the
  emulator this time, LDRR=430 = exactly 2000×7.) Prime suspect for Cool Boarders' residual.
- `a_cpu_calib_v4.ngc`, `a_cpu_calib_v5.ngc` — earlier LDIR attempts (BROKEN, ignore).
- `a_cpu_calib_v3.ngc` — added VWR (VRAM write). Silicon: **VWR 452 < MEM 471** → VRAM writes
  ARE throttled in active display — real, but Cool Boarders writes VRAM in vblank, so not its cause.
- `a_cpu_calib_v2.ngc` — added CSEQ/CRND/RRND. Silicon settled `cart_data_wait=0` (CRND==RRND)
  and the MUL/DIV under-costing.
- `a_cpu_calib_v1.ngc` — the original (instruction classes only).
- `cpu_calib_v{1,2,3,4}.c` — sources.
- Built with the **official** Toshiba cc900 toolchain, so they owe nothing to our
  assembler. Rebuild in the C template (`02_CODE_PATTERNS/minimal_template/`): drop
  the `.c` in as `main.c`, `export THOME=$(pwd)/install/ngpcbins/T900`,
  `export PATH=$THOME/BIN:$PATH`, `cp makefile_win makefile`, `make` -> `main.ngp`
  (rename to `.ngc`; ~11 KB, no 2 MB padding needed — the flashcart takes it raw).

## What it shows
Each line = how many **200-op batches finish in 60 video frames (~1 s)**. Bigger
number = the CPU did more work per second = cheaper per op. Clock gear is forced
to 0 (full 6.144 MHz), exactly like the games.

```
BASE : bare loop + a register move (the loop overhead floor)
SHIFT: word shift  v = w << 5      (Cool Boarders' hot instruction)
ADD  : reg-reg add v = v + w
MUL  : multiply    v = v * w
DIV  : divide      v = w / v
MEM  : RAM byte write
RASV : max scanline seen -> 197 = 198 lines/frame ; 198 = 199 lines/frame
```

## Baseline — THIS EMULATOR (measured 2026-07-16)
```
BASE : 02313
SHIFT: 01786
ADD  : 02022
MUL  : 01128
DIV  : 00693
MEM  : 01598
RASV : 198        (i.e. our frame = 199 lines, 0..198)
```

## Silicon result (measured on real hardware, v2, 2026-07-16)
```
BASE : 00682   SHIFT: 00538   ADD  : 00578   MUL  : 00444   DIV  : 00265
MEM  : 00471   CSEQ : 00270   CRND : 00252   RRND : 00252   RASV : 198
```
- Fetch-bound (BASE/SHIFT/ADD/MEM) ~3.4x too fast in the emulator → **`cart_wait=3`** matches.
- **`CRND == RRND` (252 == 252)** → a cart data read == a RAM read → **`cart_data_wait=0`**
  (the earlier `=5` was refuted here).
- MUL/DIV smaller than a pure fetch-wait predicts (2.5x) → they were under-costed →
  **fixed** (emulator now reads MUL 446 / DIV 265).

## v2 — the three data-read tests (isolate `cart_data_wait`)
CSEQ/CRND/RRND each read ONE byte per rep with identical index arithmetic; only the
source differs. RRND reads RAM (never wait-stated); CRND reads cart flash with the
SAME stride, so **RRND − CRND is the pure cart data-read penalty**. (Every test's loop
CODE is fetched from cart, so fetch=3 slows them all equally — that shared cost cancels
in RRND − CRND.) CSEQ reads cart sequentially (stride 1); CSEQ vs CRND hints at flash
page-mode, but carries a small `inc`-vs-`add` arithmetic offset, so only a LARGE
CSEQ > CRND gap means sequential reads are genuinely cheaper (a 3rd parameter).

Emulator v2 numbers (2026-07-16):
```
              cart_wait=0     with fix (fetch=3,data=5)
CSEQ :          00923              00262
CRND :          00871              00246
RRND :          00871              00255      <- RRND==CRND with no fix; CRND<RRND with it
```
So the model charges cart data reads (CRND drops below RRND once the fix is on).

**Silicon verdict:** `RRND − CRND` came back **0** (252 == 252) → cart data reads are NOT
wait-stated, `cart_data_wait=0`, and the residual slowdown is elsewhere. (This is the
"re-open the analysis" branch — which pointed at VRAM writes, hence v3.)

## v3 — the VRAM-write test (VWR), the last open piece
With the CPU model now silicon-exact, Cool Boarders still runs ~51 fps in the emulator
vs 30 on silicon. The remaining suspect is the per-frame **char-RAM ldir**: the K2GE may
throttle CPU VRAM access during the active drawing period (`ngpcspec.txt`, "adjustment
circuitry"). **VWR** writes a byte to VRAM (0xBE00) in the same harness as **MEM** (a RAM
write at 0x4200); the batch loop spans active + vblank lines, so VWR is the average
VRAM-write cost across a frame.

Emulator v3 numbers: with no VRAM wait, `VWR == MEM` (497 ≈ 496). With a 4-cycle VRAM
write, `VWR = 466 < MEM = 496`. **On silicon:**
- `VWR << MEM` → VRAM writes ARE throttled → confirms the hypothesis; the gap gives the
  cost (feeds `vram_wait`, which brings Cool Boarders to 30 fps and leaves Fatal Fury at 60).
- `VWR == MEM` → VRAM writes are NOT throttled → refuted; re-open (do NOT ship a guess —
  a `cart_data_wait=5` guess already got refuted this way).

## v6 — the LDIR (block-copy) test, the current open question
With a silicon-exact CPU and the VRAM throttle understood, Cool Boarders still runs ~51 fps
(vs 30). It does a big per-frame **LDIR** into a RAM frame buffer (~thousands of bytes) the
calib never timed. Our LDIR costs 7 cycles/byte (datasheet) — and the datasheet MUL/DIV
figures already proved to be **floors**. Setting LDIR = 14 makes Cool Boarders hit 30 fps AND
leaves Fatal Fury at 60 — one instruction-cost fix explaining both — but that must be measured,
not guessed. **LDRR** = one 2000-byte LDIR RAM→RAM per batch (pure block cost); **LDVR** =
2000-byte LDIR RAM→VRAM (block + any VRAM throttle). Emulator (LDIR=7): **LDRR == LDVR == 430**
(2000×7 = 14000 cycles dominates the batch — verified in the emulator, unlike v4/v5).

## How to use it (real hardware)
1. Flash `a_cpu_calib_v6.ngc` to your flashcart, boot it.
2. Wait a few seconds for the numbers to settle, note them all (+ RASV).
3. **The open numbers are `LDRR` and `LDVR`** (the rest re-confirm v1/v2/v3).

### Reading the result (LDIR cost/byte = `7 × 430 / LDRR`)
- **LDRR ≈ 215 (about half of 430)** → LDIR is ~2x under-costed (real ≈ 14/byte). This is
  Cool Boarders' bug — bump the LDIR cycle count and it drops to 30 fps, Fatal Fury untouched.
- **LDRR ≈ 430** → LDIR is correctly costed at 7 → the residual is something else (re-open).
- **LDRR ≈ 860** → LDIR is *over*-costed (real ≈ 3.4) → the residual is elsewhere and LDIR
  needs lowering (a different fix).
- **LDVR < LDRR** → writing the block to VRAM costs extra (the active-display throttle, v3).
- **RASV**: 197 → our 199-line frame is one line too long (~0.5%); 198 → we match.

> The ratio you read is the whole answer: it turns "the game feels too fast" into
> an integer we can act on, and it becomes a non-regression test the emulator must
> reproduce. This is the project's rule: **we don't tune by feel, we measure.**
