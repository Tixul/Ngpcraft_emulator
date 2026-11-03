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

With the CPU model now silicon-exact on every class, Cool Boarders STILL ran ~51 fps
(vs 30 on silicon) → the residual was **not the CPU**. **v3** then measured VRAM writes and
found the K2GE throttle is real (VWR 452 < MEM 471) — but *not* Cool Boarders' cause, since
that game writes VRAM in vblank. The residual is its per-frame **LDIR** block copy, which is
what **v6** measures (`ldir_cost`, shipped at 14 pending that ROM). See `../DEVLOG.md` and
memory `project_ngpc_emulator_fps_waitstates`.

**Since (2026-08-25):** the same ROM caught the silicon recalibration. `ldirw_cost` was
still 18 and still right, but the recalibrated model lets the bus interface unit run a
queue ahead (`biu_slack`) — and it was crediting that queue **through** a block copy, which
holds the bus for its whole run and cannot prefetch behind itself. Three block copies per
block = 48 cycles a block given away: **4086 cycles/block against the hardware's 4120
(0.9917×)**, the copier walked back into the beam and one line per band came out corrupt.
With the queue drained: **4134 (1.0034×)**, the closest this project has measured, and the
picture is pixel-clean. ⚖️ **All eleven ROMs here render a bit-identical framebuffer with
and without it** — no silicon figure moves. See `DEVLOG.md` 2026-08-25 and
`tests/test_bomberman_hicolor_phase.py`.

**Since (2026-08-06):** that block cost turned out to be **two** numbers, not one — the byte
form and the word form are different instructions and the loop is billed per iteration. The
word form is now `ldirw_cost`, shipped at **18**, measured on a homebrew raster oracle to a
one-cycle tolerance. See [v7](#v7--the-word-block-copy-ldirw-and-why-v6-cannot-answer-it).

## What the emulator actually ships today

| Knob | Field default (`Machine`) | Shipped by the shell | Status |
|---|---|---|---|
| `cart_wait` (fetch) | `0` — free fetch, the pre-feature behaviour | **3** (`cfg.CART_FETCH_WAIT`) | ✅ silicon (v1) |
| `cart_data_wait` | `0` | **0** (`cfg.CART_DATA_WAIT`) | ✅ silicon (v2): 0 is the answer, not "unset" — **re-confirmed 2026-08-06**, see below |
| `ldir_cost` — **byte** LDIR/LDDR | `7` (datasheet **en ÉTATS**) | **14** (`cfg.CART_LDIR_COST`) | ✅ **silicon (v20)** : 14,12 cy/itération RAM→RAM. ⚡ Et 7 états × 2 = 14 : notre valeur **EST** la datasheet, on lisait des états comme des cycles |
| `ldirw_cost` — **word** LDIRW/LDDRW | `0` = follow `ldir_cost` | **14** | ✅ **silicon (v20 + v21)** : 14,16 cy/itération RAM→RAM, comme l'octet. ⚠️ Le **18** d'avant n'était pas faux, il était **mal attribué** — il portait 14 + le prix de lire la source en cartouche (v21) |
| `block_drains_queue` — the BIU cannot prefetch through a block copy | `false` | **on** (armed by `set_timing_silicon`) | ⚠️ measured on the same homebrew raster oracle (2026-08-25) — see below |
| `vram_wait` | `0` | **10**, **par ACCÈS** (armé par `set_timing_silicon`) | ✅ **silicon (v3 + v20)** : 2,74 et 2,95 cy par écriture ; rapport mot/octet **1,00** ⇒ par accès, pas par octet |
| `block_cart_src_per_byte` | `0` | **2** | ✅ **silicon (v21)** : lire la **source** d'un bloc en cartouche coûte +4,12 cy par itération mot ; la **destination** ne coûte rien (+0,08) |
| `k2ge_access_as_ram` + `vram_read_wait` | `false` / `0` | **on / 4** (armés par `set_timing_silicon`) | ✅ **silicon (v22 + v23, 2026-11-03)** : un accès K2GE (registres) ou SFR coûte **comme la RAM** ; un registre de contrôle (0x8000-0x81FF) n'est **pas** freiné comme la VRAM ; une **lecture** VRAM en affichage actif l'est (+3,95 cy) |

⚠️ **The two defaults differ on purpose, and it catches people.** The field default of `0` is
backward compatibility, not a hardware claim; the shell applies the silicon set on every ROM
load (`cart_wait_states()` → True). **Anything that builds a `Machine` itself — a bench script,
a test, the MCP server — runs free-fetch until it calls the setters**, and will measure a
machine ~2.9× too fast. Worse, any optimisation whose gain is *fewer instruction bytes* then
measures as exactly zero, because fetch is the one cost not being billed. Copy the three calls
from `core/romcheck.py`. Full write-up: README → "Timing — wait states".

## 📌 MISE À JOUR (2026-11-03) — v22 + v23, le coût d'un accès par ZONE

Chronomètre : **RAS.V + RAS.H** (RAS.H vivant depuis la PR 31 ; validé sur console par la
v22 : bornes 0x80→0x00, jamais de remontée, repart à 0x80 quand RAS.V avance, 4 clocks
par cran). Méthode v23 : la même boucle avec l'accès écrit 8 puis 16 fois, coût d'un accès
= (T16 − T8) / 512, mesure démarrée en début de ligne 0, `di`.

| v23 (clocks / accès) | silicium | avant | armé |
|---|---|---|---|
| lecture mot RAM / K2GE 0x8008 / K2GE 0x8020 | 20 / 20 / 20 | 20 / 16 / 16 | 20 / 20 / 20 |
| lecture octet RAM / K2GE 0x8008 | 32 / 32 | 32 / 28 | 32 / 32 |
| écriture mot RAM / K2GE 0x8020 | 28 / 28 | 28 / **34** | 28 / 28 |
| écriture octet RAM | 24 | 24 | 24 |
| lecture mot VRAM 0x9F00 (affichage actif) | 23,93-23,96 | 16 | 24 |
| lecture mot ROM 0x200020 | 28 | 24 | 24 ⚠️ |
| lecture mot SFR 0x0020 | 20 | 12 | 16 ⚠️ |

v22 (changements de ligne en 1024 lectures, A / B) : silicium **175 / 82**, avant 167 / 74
(5 à 10 % trop rapide), armé **175 / 82**. Corpus 26 cases : **0,18 % → 0,12 %**, pire
**0,77 → 0,46 %**. Suite complète verte armée.

⚠️ **Deux écarts restent OUVERTS, volontairement** : ROM et SFR. Leurs instructions n'ont pas
la longueur de celle de la RAM (adresse 24 / 8 bits contre 16), donc la v23 ne sépare pas
coût d'accès et coût de fetch. Et la v2 a mesuré des lectures **octet** cartouche == RAM
(`CRND == RRND`) : **ne pas toucher `cart_data_wait` sur la foi de la v23.** Non mesurés
non plus : palettes (0x8200+) et table des sprites (0x8800+) — l'étranglement en écriture
y est conservé tel quel ; la lecture VRAM en **vblank**.

Banc : `access_gate.py` (`--corpus` rejoue aussi les 26 cases). Test :
`tests/test_access_cost_silicon.py`. Sources : `cpu_calib_v22.c`, `cpu_calib_v23.c` — projets
autonomes (`hw_test_rash`, `hw_test_cout_acces` : `entry.asm` lié en premier, police hex
`hexfont.h`, chaîne officielle cc900, sources en CRLF) ; les ROM font 512 Kio.

## 📌 ÉTAT COURANT (2026-08-30) — lire ceci d'abord

Ce fichier est un **journal** : les sections plus bas racontent des états dépassés, y
compris des chiffres et des conclusions qui ne valent plus. L'état livré, lui, est
celui-ci :

| | valeur | contre |
|---|---|---|
| corpus, 26 cases silicium | **0,18 %** moyen, **0,77 %** pire — **0 case > 1 %** | — |
| chemin d'une interruption | **110,0 cy** | annexe B **110**, silicium **111,5** |
| `ldir` / `ldirw` RAM→RAM | **14,09 / 14,05** cy/itération | silicium **14,12 / 14,16** |
| `ldirw` ROM→VRAM | **18,05** | silicium **18,16** |
| étranglement VRAM | **2,9 cy par ACCÈS** | silicium **2,74** (v3) et **2,95** (v20) |
| contraste v19 (`nop` / charges) | **−1,2** | silicium **+1,5** |
| v18 page 0 (tir indépendant) | 299 / 250 / **236** / 207 | silicium 299 / 250 / **235** / 207 |
| suite | **2115 verts** | — |

**Armé les 29-30/08** : `data_wait_cart_only`, `irq_transparent_queue`, pas de
`branch_taken_extra` sur un `reti` transparent, `flush_on_region_change`,
`vram_wait = 10` **par accès**, `div_word_cycles = 58`, `ldirw_cost = 14` +
`block_cart_src_per_byte = 2`. Récit : `DEVLOG.md` des 29 et 30/08. Reste à faire :
`OPEN_ITEMS.md`.

### ⛔ Résultats à ne PAS rejouer

- la **« ristourne »** (une interruption rendrait le code interrompu moins cher) :
  **réfutée au silicium** (v19, contraste +1,5 quand nos modèles prédisaient +18 et +12) ;
- **`data_access_cycles` uniforme** : il ne se paie qu'en code **cartouche** — deux mesures
  silicium l'exigent d'un côté et le refusent de l'autre, et ce qui les sépare est la
  région du **code**, pas des données ;
- **la destination VRAM sur un transfert bloc** : elle ne coûte **rien** (v21, +0,08).

### ⚠️ Conclusion PÉRIMÉE que ce fichier a portée un jour

Les sections datées du 29/08 affirment que **`mul`/`div` mot n'admet aucune constante
unique** parce que la division serait à **latence variable**. ⛔ **C'est faux, et le tir
v20 l'a réfuté** : trois divisions aux opérandes très différentes coûtent 87,07 / 87,09 /
87,09 — étendue **0,02 cy**. La latence est **fixe**, la constante existe, elle vaut **58**
et pas 56. L'hypothèse avait tenu 24 h et coûtait 1,7 % de corpus.

## Files

### 🔧 Les bancs (à jour 2026-08-30) — ce qu'ils mesurent, et ce qu'ils NE mesurent pas

| banc | question |
|---|---|
| `corpus_gate.py` | les 26 cases silicium des cinq ROM. ⛔ `--with-extra 0` **désarme** (il laissait le défaut avant le 29/08) |
| `v19_gate.py` | le **contraste** `nop` / charges = la ristourne existe-t-elle ? |
| `v20_gate.py` | quatre questions d'un coup : `ldirb`/`ldirw`, vidage de file par un bloc, latence de la division, VRAM par accès ou par octet |
| `v21_gate.py` | un transfert bloc sur **quatre chemins** (RAM/ROM × RAM/VRAM) — sépare le coût de la **source** de celui de la **destination** |
| `access_gate.py` | v22 + v23 : le coût d'**un accès** par zone (RAM, registres K2GE, VRAM, ROM, SFR), désarmé contre armé ; `--corpus` rejoue aussi les 26 cases |
| `irq_sum_gate.py` | coût d'une IRQ par **somme directe** du chemin (ancré sur `next_pc`) |
| `irq_reprise.py` | le flot interrompu est-il plus lent **hors** du chemin d'IRQ ? (comparaison **par PC**, sur la MÊME boucle) |
| `irq_count_gate.py` | combien d'interruptions sont **réellement** livrées (151,88/trame contre 152 supposées) |
| `irq_trace.py` | trace instruction par instruction. ⛔ Sélectionne l'IRQ **utilisateur** (sinon il attrape le VBlank) et totalise chaque modèle **séparément** (deux exécutions ≠ deux colonnes) |
| `irq_keep_gate.py` | balayages `keep` / `bios_wait` / `extra` sur le coût d'IRQ |
| `class_dump.py` | dumpe une itération d'une boucle chaude du corpus, avec/sans `data_access_cycles` |

⚖️ **Trois de ces bancs ont été réparés le 29/08 pour le même défaut : une colonne dont la
provenance n'était pas garantie.** Avant de croire un tableau propre, vérifier d'où vient
chaque colonne.

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

## v7 — the WORD block copy (`LDIRW`), and why v6 cannot answer it

**The gap v6 leaves.** `ldir_cost` is charged **per iteration of the loop**, and a `LDIRW`
iteration moves **two** bytes where a `LDIR` iteration moves one. They were on the same
number, so a word copy was billed at half price per byte. Cool Boarders — the game that
pinned 14 — uses the **byte** form, and LDRR/LDVR in v4/v5/v6 are byte copies too. **Nothing
we have ever measured constrains the word form**, and one field could not have held both
answers anyway.

**What settled it in the meantime — a homebrew ROM, not a calib ROM.** Thor's *BOMBERMAN*
(2004) draws its title screen with a HiColor raster trick that ships **two** implementations
and picks between them at boot by timing a delay loop against RAS.V:

- `hc_showEmu` — polls `RAS.V & 7` between blocks, so it **self-synchronises** and comes out
  right whatever the cycle costs are. This is the reference picture.
- `hc_showHW` — **open loop**: 19 blocks of 224 `ldirw` words, no polling at all, each of
  which must cost exactly one 8-scanline slice (8 × 515 = **4120 cycles**) or the image
  shears. This is the path a real console takes.

This core is accurate enough that the ROM's own detector answers "real hardware"
(`in_emu = 0` at RAM `0x476E`) and takes the open-loop path — so the open-loop path has to
be right. Measured with the event log armed on `0x8280` (one write per block pair, target
pitch **8240** cycles):

| `ldirw_cost` | pitch | vs target | pixels matching the reference |
|---|---|---|---|
| 14 (= `ldir_cost`, the old behaviour) | 6536 | **0.793×** | 30 % |
| 17 | 7880 | 0.956× | 83 % |
| **18** | 8328 | 1.011× | **100 %** |
| 19 | 8418 | 1.022× | 4 % |

**The window is one cycle wide.** That is what makes this a better instrument than any
frame-rate average: Cool Boarders tells you whether a number is roughly right, this tells
you whether it is exactly right.

⛔ **The other explanation was tested and refuted, again by v2.** "The copy's source is slow
cart flash, so charge `cart_data_wait`" also closes the gap — `cart_data_wait = 2` gives the
same 100 % picture. Re-running `a_cpu_calib_v2.ngc` with it: **CRND 252, RRND 255**. Silicon
says those two are equal. So the same ROM that killed `cart_data_wait = 5` in 2026-07 kills
`= 2` as well, and the width split is the explanation left standing. 🔑 **A fix that produces
the right picture is not thereby the right fix** — ask the oracle that already exists.

**What v7 should measure.** v6's harness with a fourth row: **`LDWR`** = one 2000-**byte**
block moved by `LDIRW` (so 1000 iterations), RAM→RAM, alongside the existing `LDRR`. Then
`LDRR` pins the byte form and `LDRR / LDWR` pins the word form, in one flash. Emulator-side
reading scale, so a silicon number can be read off directly:

| what comes back | means |
|---|---|
| `LDRR ≈ 217` | byte form = 14 (what we ship) |
| `LDRR ≈ 169` | byte form = 18 |
| `LDRR ≈ 430` | byte form = 7, the datasheet floor is the truth |

⚠️ v6 **boots in the emulator and crashes on hardware** — unexplained. Build v7 from the v3
source (which flashed fine) rather than from v6, or the same divergence will eat it.

## v8 — the ACCESS COST during active display (bus contention), the top open question

**Where it came from, and it is not our own suspicion.** `Emulator_vs_Hardware_20260807`
(xenon project), measured on device, same-session pairs. Their `SetSpriteEx` writes a sprite
position to a work-RAM shadow and can either keep it in a register or *read it back* as the
source for the OAM store — one extra **work-RAM read** per sprite, 40–60 sprites/frame:

| | with the readback | without | verdict |
|---|---|---|---|
| device, clean pair 2026-08-07 | 117 | 107 | **+9 %** |
| this core, calibrated | 90 | 90 | **0 %** — invisible |

And a third case flips the SIGN: metasprite chaining (fewer OAM writes, more bookkeeping)
measures **−1.8 VBl against** the chain here and **+5 VBl in favour** of it on device. A model
that reverses an optimisation's sign is worse than one that is merely imprecise — it makes the
developer choose backwards.

**What the mechanism is.** Their own source comment names it: *"every access during active
display is penalised by the display controller"*, and *"the emulator reported 1.5 % faster
because it counts instructions and not bus accesses"*. This is **bus contention**, not a cost
of a region.

⚖️ **AND THIS CORE ALREADY HAS THE RIGHT SHAPE — it is simply too narrow** (`execute.cpp`):

```cpp
if (m.vram_wait && a >= 0x8000 && a <= 0xBFFF && !m.in_vblank())
    m.access_wait += m.vram_wait;
```

Per access, and only outside vblank — exactly right, and backed by v3 (`VWR < MEM`). Three
things keep it from covering the case: `vram_wait` **defaults to 0**, it applies to **writes
only**, and it is limited to **0x8000–0xBFFF**. Their readback is a work-RAM READ, so it is
outside all three — turning `vram_wait` on would not touch it. (They also cannot help pin
`vram_wait` itself: their game does all its VRAM writes in vblank.)

🔑 **And v2 left a hole nobody noticed.** v2 proved cart-data reads and RAM reads are **EQUAL
TO EACH OTHER**; it never said what they equal. `cart_data_wait = 0` reads "equal" as
"therefore free", which v2 does not license. Their +9 % is the first evidence that the shared
value is not 0.

**What v8 must measure — and it is NOT "RAM vs cart" (v2 answered that) nor `vram_wait`:**

> **N accesses versus N+1, SAME region, run once during ACTIVE DISPLAY and once during
> VBLANK.**

Four pairs, same harness as v2/v3, each a tight loop of a fixed instruction sequence differing
by exactly one data access:

| row | what it isolates |
|---|---|
| `RD_A` / `RD_V` | one extra **work-RAM READ**, active display vs vblank |
| `WR_A` / `WR_V` | one extra **work-RAM WRITE**, same split |
| `CD_A` / `CD_V` | one extra **cart-data READ**, same split (closes v2's hole) |
| `OR_A` / `OR_V` | one extra **OAM/VRAM READ** (v3 did writes only) |

Reading the result:
- **`X_A == X_V` on every row** → there is no display-phase penalty; the +9 % is something
  else and Case B re-opens. Do NOT ship a number.
- **`X_A < X_V`** → the difference IS the per-access contention cost, per region and per
  direction. That one set of numbers pins `cart_data_wait`, extends the `vram_wait` guard to
  reads, and gives work RAM the term it has never had — which is what all three of their open
  cases are waiting on.

⚠️ Do not calibrate against their ABSOLUTE cycle figures. Their `README.md` states "86 922
cycles per frame at a fixed 30 fps" and, two paragraphs later, that a frame's compute "sits
just above two VBlanks" — 86 922 is **0.85** VBlank here, so the two cannot share a unit.
Their **ratios** (VBl/30F, HW-vs-emu factors) carry no unit and are sound; the absolute
figures are not, until the unit is settled with them.

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

---

## v8 — LE COÛT D'UNE INTERRUPTION (2026-08-23) — ⏳ attend la mesure silicium

`a_irq_calib_v8.ngp` · md5 **`334e5cb56e26fe78194d913cee4029a3`** · source `cpu_calib_v8.c`

**Pourquoi.** Toutes les ROM v1–v7 mesurent du code qui tourne **sans interruptions**. Or
le défaut ouvert est un split rasteur : Cool Boarders coupe `INTT0` (niveau à 0 dans
`0x0073`), fait son travail, le ré-autorise — et la ligne où son split tombe dépend du
débit **dans cette fenêtre, interruptions comprises**. Notre modèle silicium facture
l'entrée en interruption **36 cycles** (18 états × 2) là où l'ancien timing n'en compte que
**18**. Ce nombre n'a **jamais** été mesuré sur silicium.

**Ce que la ROM fait.** Le même lot de travail, trois fois : `WORK0` avec `INTT0` interdit,
`WORK1` avec une interruption **par ligne** (`TREG0`=1, timer 0 en mode broche externe =
le H-blank, exactement le montage de Cool Boarders), `WORK4` une ligne sur quatre.
`LINE` et `RASV` sont des contrôles de longueur de trame.

⚠️ **Aucun gestionnaire n'est installé, et c'est voulu** : deviner le vecteur utilisateur
du BIOS ajouterait une inconnue. On mesure le coût **total d'une interruption prise**,
stub du BIOS compris — ce que le jeu paie aussi.

### Ce que nos deux modèles prédisent

| | WORK0 | WORK1 | WORK4 | coût des IRQ |
|---|---|---|---|---|
| ancien timing (défaut actuel) | **263** | 240 | 258 | −23, soit **8,7 %** du débit |
| modèle silicium (`--timing silicon`) | **214** | 181 | 208 | −33, soit **15,4 %** |

⇒ le modèle facture les interruptions **presque deux fois plus cher en proportion**. Les
deux jeux de chiffres sont assez éloignés pour que le silicium tranche sans ambiguïté.

### Protocole

1. Flasher, lancer, **noter les cinq nombres** (`WORK0`, `WORK1`, `WORK4`, `LINE`, `RASV`)
   et **le md5 ci-dessus** — sans le md5 la mesure n'est rattachable à rien.
2. `RASV` doit valoir **198**. Sinon la console n'est pas dans l'état attendu : rejeter le tir.
3. Laisser tourner quelques secondes : les nombres se réaffichent en boucle et doivent être
   **stables**. Un chiffre qui saute d'un tour à l'autre invalide le tir.

### Comment on lira le résultat

- `WORK0` est le **contrôle** : il ne fait intervenir aucune interruption et doit tomber
  près de la valeur du modèle déjà validé par v1/v2.
- `(WORK0 − WORK1) / (WORK0 − WORK4)` doit valoir **~4** sur silicium. Si non, le coût
  n'est pas linéaire en nombre d'interruptions et notre modèle est faux **dans sa forme**,
  pas seulement dans sa valeur — ce serait le résultat le plus important des trois.
- Le rapport `(WORK0 − WORK1)` silicium / émulateur donne **directement** le facteur
  d'erreur sur le coût d'une interruption, et donc quoi mettre dans `kIrqDeliveryCycles`.

⛔ **Ne pas ajuster la constante sur Cool Boarders.** C'est ce que cette ROM existe pour
éviter : la v2 avait déjà attrapé un `cart_data_wait=5` réglé à l'oreille sur ce même jeu.

---

## v9 — LE COÛT D'UN TRANSFERT MICRO-DMA (2026-08-24) — ⏳ attend la mesure silicium

`a_dma_calib_v9.ngp` · md5 **`541dffc2937504bb66a7abc9594b7b16`** · source `cpu_calib_v9.c`

**Pourquoi.** Notre `micro_dma_service` facture **zéro**. La datasheet donne 8 états par
transfert octet/mot, 12 en 4 octets, 5 en mode compteur — mais ces chiffres n'ont jamais
été vérifiés sur silicium, et les armer à l'aveugle ralentit Fatal Fury de **4,3 %** alors
que des jeux sont déjà signalés trop lents. Le mécanisme existe (`ngpc_set_micro_dma_states`,
en huitièmes du nominal) et reste **désarmé** tant que cette ROM n'a pas parlé.

**Montage.** Timer 0 sur la broche externe TI0 (le H-blank du K2GE), `TREG0` = 1 : une
source d'interruption **par ligne**, ~152 par trame. Le canal 0 du micro-DMA est armé sur
ce vecteur, donc chaque impulsion déclenche **un transfert au lieu de vectoriser le
processeur**. Le seul coût restant est le temps de **bus** — séparé du coût d'entrée en
interruption, déjà mesuré à 111 cycles par la v8.

### Ce que nos réglages prédisent

| coût armé dans l'émulateur | WORK0 | **WORKD** | WORKC |
|---|---|---|---|
| **0** — notre défaut actuel | 260 | **261** | 260 |
| **8** — le nominal datasheet | 260 | **255** | 256 |
| **32** — 4× le nominal | 260 | **236** | 245 |

⇒ la valeur de `WORKD` lue sur console désigne directement la bonne.

### Protocole

1. Flasher, lancer, **noter les cinq nombres** (`WORK0`, `WORKD`, `WORKC`, `DMAC`, `RASV`)
   et **le md5 ci-dessus**.
2. `RASV` doit valoir **198**, sinon rejeter le tir.
3. ⚠️ **`DMAC` EST LE CONTRÔLE QUI VAUT LE DÉPLACEMENT.** Il part de 65535 et doit avoir
   **beaucoup baissé** (~9134 transferts en 60 trames dans l'émulateur, soit ~152 par
   trame). **Si `DMAC` est resté à 65535, le canal ne s'est jamais armé** — et alors un
   `WORKD` égal à `WORK0` ne dit PAS « le transfert est gratuit », il dit « la mesure n'a
   pas eu lieu ». Ce serait d'ailleurs un résultat en soi : notre cœur compare `DMA0V` à
   l'**indice** du vecteur (16), et si le matériel y attend autre chose, ce chiffre le
   révèle.
4. Les nombres se réaffichent en boucle : ils doivent être **stables**.

⛔ **Ne pas armer `micro_dma_states` sans ce tir.** C'est exactement ce que la v2 avait
attrapé sur `cart_data_wait = 5`, réglé à l'oreille sur un jeu et réfuté par la mesure.

---

## v10 — LE PONT ENTRE DEUX CAMPAGNES (2026-08-24) — ⏳ attend la mesure silicium

`a_pont_calib_v10.ngp` · md5 **`ceb06f4d05592faf15b232f8552b65b8`** · source `cpu_calib_v10.c`

**Le problème qu'elle règle.** Les classes d'instructions mesurées par `cpu_calib_v3`
(campagne de **juillet**) donnent aujourd'hui sept classes **du même côté**, +4 à +7 %.
Tentant d'y voir un biais du modèle. ⛔ **Mais le modèle est calé sur le tir v8 d'août** :
autre ROM, autre session. Soustraire deux tirs non comparables est la faute que
`OPEN_ITEMS §4bis` documente — *« une référence silicium n'est pas datable »* — et qui a
déjà coûté une passe entière ici.

**Ce qu'elle fait.** Elle mesure **dans le même tir** la boucle de référence **exacte de la
v8** (`REF`) *et* les classes de la v3. Plus rien à soustraire entre deux dates.

### Ce que notre modèle donne

| | valeur | rapport à `REF` |
|---|---|---|
| `REF` | 262 | 1,000 |
| `BASE` | 715 | **2,729** |
| `SHIFT` | 520 | 1,985 |
| `ADD` | 614 | 2,344 |
| `MUL` | 472 | 1,802 |
| `DIV` | 282 | 1,076 |
| `MEM` | 529 | 2,019 |

### Comment lire le tir — et c'est le RAPPORT qui décide, pas la valeur absolue

- **si les rapports mesurés collent aux nôtres** (à ~1 %), notre tarification *relative*
  des instructions est juste, et l'écart de +5 % vu contre juillet est un **artefact de
  campagne** : il n'y a pas de biais à corriger, et cette question se ferme ;
- **si un rapport diffère**, c'est cette classe-là qui est mal facturée, et l'écart se lit
  directement sans jamais comparer deux dates.

⚠️ Regarder aussi `REF` seul : le tir v8 avait donné **261** pour cette boucle. Un `REF`
proche de 261 confirme que les deux tirs sont dans les mêmes conditions ; un `REF` très
différent dirait que quelque chose a changé côté console, et invaliderait la comparaison
avec juillet — ce qui serait déjà la réponse.

### Protocole

Flasher, lancer, **noter les huit nombres** et **le md5**. `RASV` doit valoir **198**.
Les valeurs se réaffichent en boucle : elles doivent être stables, ou noter la plage.

### ✅ v10 — TIR SILICIUM DU 24/08, et ce qu'il dit

`REF 261 · BASE 281 · SHIFT 538 · ADD 578 · MUL 444 · DIV 266 · MEM 471 · RASV 198`
(± 1 point d'oscillation, note du testeur)

**Le pont tient** : `REF` = **261**, exactement ce que la ROM v8 avait donné pour la même
boucle. Les deux tirs sont donc dans les mêmes conditions, et la comparaison avec la
campagne de juillet redevient licite.

⇒ **le « +5 % » n'était PAS un artefact de campagne.** Il est réel.

| | silicium | nous | rapport |
|---|---|---|---|
| **REF** | 117,8 cy/itér | 117,3 | **×1,00** |
| SHIFT | 57,1 | 59,1 | ×0,97 |
| ADD | 53,2 | 50,1 | ×1,06 |
| MUL | 69,2 | 65,1 | ×1,06 |
| DIV | 115,6 | 109,0 | ×1,06 |
| MEM | 65,3 | 58,1 | ×1,12 |
| **BASE** | **109,4** | **43,0** | **×2,54** |

#### ⚠️ Et `BASE` est une anomalie que rien n'explique encore

Désassemblée (`_m_base` à `0x2016AE`), sa boucle interne est **quatre instructions
registre, dix octets, aucun accès mémoire** :
`ld WA,BC` · `inc 1,DE` · `cp DE,0x00C8` · `jr C` (branche **prise** à chaque tour).

⛔ **L'hypothèse « c'est le vidage de file à la branche » ne tient pas**, et il faut le
dire : `REF` fait **quatre** opérations par tour pour 117,8 cy, `BASE` en fait **une** pour
109,4 — sur silicium les trois opérations supplémentaires coûtent donc ~8 cycles en tout,
alors que chez nous elles en coûtent 74. Une branche identique dans les deux boucles ne
peut pas produire cet écart-là.

⇒ **Prochaine étape : désassembler `_m_ref` (`0x201668`) avant toute théorie.** Le plus
probable est que les deux boucles ne compilent pas pareil — l'une gardant ses variables en
registres et l'autre non — auquel cas `BASE` et `REF` ne mesurent pas la même chose, et
c'est ÇA le résultat.

🚨 **Leçon déjà acquise aujourd'hui, re-appliquée** : ne pas nommer un mécanisme avant
d'avoir regardé le code. Le premier réflexe a été « la branche », et le tableau le réfute
en trois lignes.

#### 💥 `_m_ref` désassemblé — et le modèle a le coût AU MAUVAIS ENDROIT

| | instructions | octets | **mots** | silicium |
|---|---|---|---|---|
| `BASE` (`0x2016C0`) | 4 | 10 | **5** | 109,4 cy/itér |
| `REF` (`0x20167A`) | 13 | 28 | **14** | 117,8 cy/itér |

Neuf instructions et neuf mots de plus ne coûtent que **8,4 cycles**. Résolution linéaire
sur les deux points du **même tir** :

> **coût = 104,7 cycles FIXES par itération + 0,93 cycle par mot lu**

⇒ sur cette console, une itération de boucle coûte ~105 cycles fixes, et la **lecture des
instructions est quasi gratuite** — entièrement masquée par le prefetch.

⛔ **Notre modèle dit l'inverse** : 8,25 cycles par mot, aucun coût fixe. Il tombe juste sur
`REF` **par compensation** (14 × 8,25 ≈ 115 ≈ 118) et s'effondre sur `BASE`, qui n'a que
5 mots à facturer (43 contre 109).

⚠️ **Ce que ça NE dit pas encore** : ce que sont ces 105 cycles. Ils couvrent l'exécution
de `inc` + `cp` + `jr` **et** la pénalité de branche prise, sans qu'on puisse encore les
séparer avec deux points seulement. Une ROM à trois tailles de boucle (par ex. 5, 14 et 30
mots) les séparerait — et confirmerait ou non la linéarité.

🚨 **Et ça recadre TOUT le reste** : `mot = 8,25` a été calé sur la ROM v8, dont la boucle
fait justement ~14 mots. Il compense donc un coût fixe qu'on ne modélise pas. Les classes
à +6 % et `MEM` à +12 % sont probablement le même effet, vu sous des tailles de boucle
différentes. ⛔ **Ne rien réajuster tant que la forme n'est pas corrigée** : ajuster une
constante qui compense une erreur de structure, c'est ce qui a déjà fait dériver `DIV`.

---

## v11 — LA DROITE : séparer le coût fixe du coût par mot (2026-08-24) — ⏳ attend le tir

`a_droite_calib_v11.ngp` · md5 **`78bd48dc869020f6cf52fc4d2dd9054f`** · source `cpu_calib_v11.c`

**Ce que la v10 a établi** : deux boucles du même tir donnent
`coût = 104,7 cycles FIXES par itération + 0,93 cycle par mot lu`. Notre modèle dit
l'inverse (8,25 par mot, aucun coût fixe) : il tombe juste sur une boucle de 14 mots **par
compensation** et s'effondre sur une de 5 mots.

**Ce que deux points ne peuvent pas dire** : si la relation est linéaire, et ce que sont
ces 105 cycles.

**Cette ROM mesure quatre tailles de boucle**, même structure, même compteur, même branche
— seule la quantité de travail registre change. Tailles **relevées au désassemblage**, pas
supposées : `L1` = **5 mots**, `L2` = **14**, `L3` = **35**, `L4` = **53**.

### Les deux prédictions sont incompatibles — c'est ce qui rend le tir décisif

| boucle | notre modèle | si la droite de la v10 tient |
|---|---|---|
| L1 (5 mots) | **720** | **281** |
| L2 (14 mots) | 263 | 261 |
| L3 (35 mots) | **105** | **224** |
| L4 (53 mots) | **70** | **200** |

⇒ les deux ne se croisent qu'à `L2` — la boucle sur laquelle notre constante a été calée.
Partout ailleurs elles divergent d'un facteur 2 à 3.

**Comment lire le tir**

- **valeurs proches de 281/261/224/200** ⇒ la droite tient, le coût est **fixe par
  itération** et la lecture d'instructions quasi gratuite. Notre modèle a le coût au
  mauvais endroit et il faut en changer la **forme**, pas les constantes.
- **valeurs proches de 720/263/105/70** ⇒ notre modèle est bon et c'est la mesure `BASE` de
  la v10 qui était trompeuse.
- **valeurs entre les deux, ou non alignées** ⇒ ni l'un ni l'autre : reporter les quatre
  nombres, la droite se recalcule dessus.

### Protocole

Flasher, noter les **cinq nombres** (`L1`..`L4`, `RASV`) et le **md5**. `RASV` doit valoir
**198**. Noter la plage si ça oscille.

⛔ **Ne réajuster aucune constante avant ce tir.** `mot = 8,25` a été calé sur une boucle
de ~14 mots — exactement le seul point où les deux modèles s'accordent. Ajuster une
constante qui compense une erreur de structure est ce qui a déjà fait dériver `DIV`.

### 🚨 TIR DU 24/08 — LA DROITE EST RÉFUTÉE, ET NOTRE MODÈLE CONFIRMÉ

`L1 678/679 · L2 261 · L3 107 · L4 71 · RASV 198`

| boucle | silicium | notre modèle | écart | « droite v10 » |
|---|---|---|---|---|
| L1 (5 mots) | **678** | 720 | **−5,8 %** | 281 ❌ |
| L2 (14 mots) | **261** | 263 | **−0,8 %** | 261 |
| L3 (35 mots) | **107** | 105 | **+1,9 %** | 224 ❌ |
| L4 (53 mots) | **71** | 70 | **+1,4 %** | 200 ❌ |

⛔ **IL N'Y A PAS DE COÛT FIXE DE 105 CYCLES PAR ITÉRATION.** Le modèle a le coût au bon
endroit — proportionnel aux mots lus — et la lecture d'instructions n'est pas gratuite.
La « droite » tirée de la v10 se trompait d'un facteur 3 sur trois points sur quatre.

**D'où venait l'erreur** : le `BASE = 281` de la v10 est une anomalie **de cette ROM-là**.
`L1` mesure exactement la même chose (`v = w` dans la même macro) et donne **678**. Deux
ROMs, deux résultats incompatibles pour un code identique en apparence — c'est le
désassemblage comparé des deux `m_base`/`m_l1` qui dira pourquoi, et c'est une question
ouverte à part entière.

🚨 **LA LEÇON, ET ELLE EST CHÈRE.** À partir de DEUX points j'ai tiré une droite, nommé un
mécanisme (« coût fixe par itération, fetch gratuit »), et écrit dans trois documents que
la **forme** du modèle était fausse. Quatre points ont suffi à tout démolir.
⇒ **Deux points font toujours une droite.** Ils ne prouvent jamais qu'il y en a une.

### Ce qui reste, et c'est petit

Un écart de **−5,8 % sur la boucle la plus courte**, qui tombe à ±2 % dès 14 mots. Nous
sommes donc légèrement trop rapides sur le code très serré, et justes ailleurs. C'est
probablement la même chose que le « +5 % » vu sur les classes de la v3 — un effet borné,
qui s'atténue quand les boucles s'allongent, **pas** une erreur de structure.

---

## v12 — L'ALIGNEMENT : RÉFUTÉ (2026-08-24)

`a_align_calib_v12.ngp` · md5 `7c03a308e289aa2c13f475a617dd18dc` · source `cpu_calib_v12.c`

La **même boucle de dix octets**, quatre fois, à quatre adresses (restes mod 4 : **3, 1,
0, 0** — deux impairs, deux alignés sur la file de 4 octets).

**Tir du 24/08** : `A1 682 · A2 682 · A3 683 · A4 682 · RASV 198`

⛔ **L'adresse n'a AUCUN effet.** Les quatre sont identiques à ±1.

### Ce que ça ferme, et c'est plus que l'alignement

`682` est **exactement** ce que la campagne de juillet donnait pour `BASE`, et la v11 avait
donné 678 pour la même boucle. Quatre ROM indépendantes s'accordent sur ~680.

⇒ **le `BASE = 281` de la v10 était une anomalie isolée**, et c'est d'elle SEULE que venait
la théorie du « coût fixe de 105 cycles par itération, fetch gratuit » — écrite dans trois
documents avant d'être démolie par la v11.

🚨 **LA LEÇON COMPLÈTE, en trois temps** :
1. j'ai nommé un mécanisme (« la branche ») avant de lire le code — réfuté en trois lignes ;
2. j'ai tiré une droite de **deux** points et conclu que la **forme** du modèle était fausse
   — quatre points l'ont démolie ;
3. le point aberrant qui avait tout déclenché ne s'est jamais reproduit sur aucune des
   trois ROM suivantes.
⇒ **Un point isolé qui contredit le modèle est d'abord suspect, pas révélateur.** Le
reproduire coûte une ROM ; le croire a coûté trois documents à corriger.

### Ce qui reste, chiffré et borné

Sur cette boucle de 5 mots : silicium **682**, notre modèle **~730** ⇒ nous sommes
**~7 % trop rapides**. Sur 14 mots l'écart tombe à −0,8 %, sur 35 à +1,9 %, sur 53 à +1,4 %.

⇒ **le modèle est trop rapide sur le code TRÈS serré, et juste dès que les boucles
s'allongent.** C'est le même phénomène que le « +5 % » des classes de juillet, mesuré
maintenant sur quatre tailles. Effet borné, pas erreur de structure.

---

## v13 — LE VIDAGE DE FILE À LA BRANCHE PRISE (2026-08-27) — ✅ TIRÉE ET TRANCHÉE

`a_flush_calib_v13.ngp` · md5 `c535123425392f9c4ae163e692f2f421` · source `cpu_calib_v13.c`
Dépouillement : `python hw_calibration/flush_gate.py u1 u2 u4 u8 --rasv 198`

### La question, et pourquoi maintenant

Une branche **prise** coûte-t-elle plus que sa ligne de table, parce que la file de
prefetch est jetée ? Nous l'avions essayé le 21/08 et **réfuté par le SIGNE** : un vidage
ne peut que ralentir, or contre le silicium ce cœur est déjà lent. Mais une réfutation par
le signe ne dit pas **où** est la vérité, seulement qu'un des bouts est faux. Le silicium
tranche.

### Le montage

Quatre blocs, **même travail** (640 unités de corps), densité de branches ×8 :

| bloc | `ld BC` | unités/tour | branches prises | corps |
|---|---|---|---|---|
| u1 | 640 | 1 | **640** | 12 o. |
| u2 | 320 | 2 | **320** | 24 o. |
| u4 | 160 | 4 | **160** | 48 o. |
| u8 |  80 | 8 |  **80** | 96 o. |

Coût du bloc = `ordonnée + pente × branches`. ⚡ **C'est la PENTE qui répond, pas les
nombres** : l'ordonnée absorbe tout le coût du travail, donc une erreur sur `mul` ou `ld`
la déplace et **laisse la pente intacte**. La ROM est immunisée contre les coûts qu'elle ne
cherche pas — le contraire des v1-v9, qui comparaient des niveaux absolus.

Vérifié sur la ROM construite : déplacements `djnz` = **−15 / −27 / −51 / −99**, soit des
corps de 12/24/48/96 octets. Encodages relevés au désassemblage : `40`/`43` (ld XRR,#imm32),
`CD 41` (mul WA,E), `D9 1C` (djnz BC — forme registre-direct HW-prouvée, **pas** `D0 1C`).

### ⛔⛔ Le piège qui a tué la première version — À LIRE AVANT DE TOUCHER AU CORPS

Premier montage : corps fait **uniquement de `mul`**, choisi parce qu'une instruction
execute-bound remplit la file et rend le crédit maximal à l'instant de la branche.
**Les deux modèles ont rendu les MÊMES nombres** (224/263/286/299 des deux côtés).

Le raisonnement était à l'envers. **Un vidage ne coûte pas le crédit qu'on jette, il coûte
le crédit qui MANQUE ENSUITE.** Après la branche, une seule `mul` (2 octets, 28 cycles)
reconstitue tout le crédit en une instruction : la perte devient invisible.

⇒ Le corps doit **consommer** du crédit après la branche. D'où sa forme :
`ld XWA,#imm32` + `ld XHL,#imm32` (5 octets pour 5 états = fetch-bound, ils consomment)
puis `mul WA,E` (2 octets pour ~14 états = il reconstruit), **la `mul` en dernier** pour que
la branche tombe crédit au maximum. **Inverser l'ordre rend la ROM aveugle.**

### Les deux prédictions (recalculées en live par `flush_gate.py`, jamais en dur)

| | u1 | u2 | u4 | u8 | pente | ordonnée |
|---|---|---|---|---|---|---|
| drapeau **désarmé** (vidage nul) | 141 | 155 | 163 | 167 | **12,2** cy/br | 35 806 |
| drapeau **armé** (vidage total) | 120 | 141 | 155 | 163 | **24,1** cy/br | 35 821 |

⚖️ **L'ordonnée est la même à 0,04 %** et la pente diffère d'un facteur **1,98**. Les deux
hypothèses ne diffèrent donc que par **un seul paramètre** : la pente est une mesure pure du
coût d'une branche prise. Écarts à la droite : 0,11 % et 0,15 % — les deux ferment.

⚠️ Les deux jeux ne sont pas un facteur d'échelle l'un de l'autre (rapports 1,175 / 1,099 /
1,052 / 1,025) : ils diffèrent de **forme**, pas de niveau.

### Lecture du tir

- quatre points alignés ⇒ montage sain ;
- pente **~12** ⇒ pas de vidage ; pente **~24** ⇒ vidage total ;
- pente **entre les deux** ⇒ vidage **partiel** : lire le crédit conservé ;
- droite qui ne ferme pas, ou RASV ≠ 198 ⇒ **ne rien conclure** (leçon v10).

⛔ **Pas de variable globale dans cette ROM**, volontairement : la v6 bootait chez nous et
**plantait la console**, avec pour seuls suspects des buffers globaux et `ld xhl,_symbol`.
La v13 garde la forme de la v12, qui a tourné sur silicium ; le banc relit les nombres dans
le plan de tuiles via la ligne-clé `0123456789` que la ROM écrit exprès.

### 🎯 TIR SILICIUM 2026-08-27 — les deux réglages extrêmes sont FAUX

RASV **198**. Deux lectures, jitter ±1 sur chaque case :

| | u1 | u2 | u4 | u8 | pente | ordonnée |
|---|---|---|---|---|---|---|
| vidage nul (notre défaut) | 141 | 155 | 163 | 167 | 12,2 | 35 806 |
| **SILICIUM** | **125/124** | **141/140** | **151/152** | **156/157** | **17,5–18,8** | **~37 800** |
| vidage total | 120 | 141 | 155 | 163 | 24,1 | 35 821 |

Droite fermée à 0,15 % et 0,60 % ⇒ le montage est sain, et la conclusion est **stable
sur les deux lectures du jitter**.

⚖️ **La pente tombe à 45-55 % du chemin entre les deux réglages.** Une branche prise
coûte **réellement ~+6 cycles** de plus que sa ligne de table — la réfutation du 21/08
rejetait à raison le vidage **total** et à tort le vidage tout court — mais environ **la
moitié** d'un vidage complet. ⇒ **Le booléen avait la mauvaise FORME, pas la mauvaise
valeur.** D'où `branch_flush_keep` (machine.hpp) : le crédit d'avance qui survit à la
redirection, en cycles.

🚨 **ET LA PORTE AVAIT LE MÊME DÉFAUT QUE LE MODÈLE.** `flush_gate.py` tranchait sur la
médiane des deux extrêmes : le tir tombant pile au milieu, elle **basculait de verdict
selon le côté du jitter** (17,5 → « pas de vidage », 18,8 → « vidage »). La règle « pente
entre les deux ⇒ ne rien conclure » était écrite dans l'en-tête de la ROM et **n'était pas
implémentée**. Corrigé : la porte rend le **crédit conservé**, plus un oui/non.
⚡ Une porte qui ne peut pas répondre « ni l'un ni l'autre » finira par mentir le jour où
c'est la vraie réponse.

### ⛔ Rien n'est armé — et pourquoi

Défaut inchangé (`flush_queue_on_branch = false`, `branch_flush_keep = 0`) ; 2115 tests
verts. Deux raisons de ne pas armer sur ce seul tir :

1. **Le nombre n'est pas dérivé.** `keep = 6` reproduit *cette* ROM. La valeur
   structurellement attendue — un mot de file, ~8 cy — donne 16,1 cy/branche, **hors de
   la bande mesurée**. On ne sait donc pas *pourquoi* c'est la moitié.
2. **Un nombre calé sur une seule boucle est exactement ce que la v2 avait attrapé** sur
   `cart_data_wait = 5`. Avant d'armer : rejouer les ROM v1-v12 contre leurs tirs
   enregistrés et vérifier que ça ne dégrade rien.

⚠️ Noter aussi l'**ordonnée** : silicium ~37 800 contre ~35 800 modélisés, soit **~6 % de
travail non facturé** — c'est le « +5 % commun » déjà connu des classes de juillet, et il
est **indépendant** de la pente (une erreur sur le travail déplace l'ordonnée sans toucher
la pente ; c'est la propriété qui fait la valeur de ce montage).

### ⚖️ LE CORPUS TRANCHE LE MÉCANISME — `corpus_gate.py` (2026-08-27)

Deux modèles reproduisent **également bien** la v13, et c'est tout le problème :

| hypothèse | ce qu'elle dit | pente v13 |
|---|---|---|
| `branch_flush_keep = 6` | la branche perd la **moitié du crédit d'avance** de la file | 18,0 |
| `branch_taken_extra = 6` | la branche coûte **+6 cy, toujours**, crédit ou pas | 18,6 |

Elles ne se séparent que sur du code **fetch-bound**, où le crédit est nul : le vidage y
est invisible, la surcharge non. ⇒ `corpus_gate.py` rejoue les 26 cases silicium de
v2/v10/v11/v12/v8 :

| | écart moyen | pire | les trois ancres exactes (WORK0 · REF · L2) |
|---|---|---|---|
| désarmé (défaut) | 4,78 % | 12,31 % | +0,0 % · +0,8 % · +0,8 % |
| **`keep = 6`** | **4,79 %** | **12,10 %** | +0,0 % · +0,8 % · +0,8 % |
| `extra = 6` | 4,97 % | 12,45 % | **−6,5 % · −5,7 % · −4,2 %** |

⛔ **`branch_taken_extra` EST RÉFUTÉ.** Il bascule tout le corpus de +5 % (trop rapides) à
−5 % (trop lents) et **détruit les trois cases qui étaient exactes** — précisément celles
sur lesquelles `mot = 8,25` et le coût d'IRQ ont été calés.

✅ **Et le mécanisme est donc identifié, pas seulement l'ampleur** : le surcoût d'une
branche prise est **conditionnel au crédit d'avance accumulé**. Une branche dans du code
fetch-bound ne coûte rien de plus — le corpus le prouve. C'est la signature d'un **vidage
de file**, pas d'une surcharge d'instruction.

⚠️ **Ce que le corpus ne fait PAS** : confirmer la valeur 6. Il est **insensible** à
`keep` (±0,2 %) parce qu'aucune de ces boucles n'accumule de crédit à sa branche. C'est un
test de **non-régression**, pas une validation. La v13 reste la seule mesure qui contraigne
ce nombre — elle a été bâtie pour ça.

### 📌 État : rien n'est armé, et voici la décision qui reste

`flush_queue_on_branch = false`, `branch_flush_keep = 0`, `branch_taken_extra = 0`.
2115 tests verts. Le dossier pour armer `flush + keep = 6` :
- **pour** : seul réglage qui reproduit la v13 ; corpus neutre, pire cas légèrement
  meilleur (12,31 → 12,10) ; mécanisme désormais étayé et non plus supposé ;
- **contre** : la valeur 6 tient sur **une** ROM, et n'est toujours pas dérivée (un mot de
  file vaudrait ~8 cy et sort de la bande) ; armer touche le modèle de timing du bureau,
  donc savestates et rejeu.

🔎 **Trouvé en passant, hors sujet mais réel** : sur la v12, le silicium donne
`682/682/683/682` — quatre fois la même boucle à quatre adresses — alors que **nous**
donnons `715/733/732/732`. Notre modèle est **sensible à l'adresse** (le quart de cycle
tiré de l'adresse dans `fetch_wait_q4`) là où le silicium ne l'est pas. Écart 2,4 %,
à instruire séparément.

---

## v14 — CINQ PAGES DE MESURES (2026-08-27) — ✅ TIRÉE

`a_multi_calib_v14.ngp` · md5 `9ee8b192761999dbdc4b3ef91f62e7e1` · source `cpu_calib_v14.c`
Dépouillement : `python hw_calibration/v14_gate.py --p0 ... --rasv 198` (page par page)

**Navigation : GAUCHE / DROITE.** Le numéro de page est affiché en chiffre ligne 1 —
c'est ce qui rend la navigation du banc émulateur déterministe.

### Page 0 — quel modèle de branche ? Trois ROTATIONS du même corps

L'unité fait 12 octets et trois instructions, **toujours les mêmes** : `ld XWA,#imm32`
(5 o./5 états, fetch-bound, **consomme** le crédit), `ld XHL,#imm32` (idem), `mul WA,E`
(2 o./~14 états, execute-bound, **reconstruit** le crédit). Les trois blocs sont les trois
rotations — vérifié à l'octet : composition identique (1 ldw, 1 ldh, 1 mul), seul l'ordre
change.

| bloc | ordre | crédit en main à la branche |
|---|---|---|
| A | ld ld mul | ~16 cy (plein) |
| B | ld mul ld | ~5 cy |
| C | mul ld ld | ~0 cy (vide) |

⚡ **C'est le couple (A1, C1) qui tranche, et il tient en deux nombres :**

| | A1 | B1 | C1 | A8 | C8 |
|---|---|---|---|---|---|
| désarmé | 141 | 132 | **141** | 165 | 165 |
| `keep=4` | 126 | 122 | 140 | 162 | 164 |
| `keep=6` | 129 | 125 | **141** | 162 | 164 |
| `keep=8` | 133 | 128 | **141** | 163 | 164 |
| `extra=6` | 129 | 121 | **130** | 163 | 162 |

- **pas de vidage** ⇒ A1 = C1 ;
- **vidage CONDITIONNEL** ⇒ A1 baisse, **C1 intact** (rien à jeter quand la file est vide) ;
- **surcoût INCONDITIONNEL** ⇒ A1 et C1 baissent **ensemble**.

A8/C8 sont le contrôle d'échelle : l'écart doit se diviser par ~8.

⚠️ **Un écart entre rotations n'est pas à lui seul la preuve d'un vidage** : le modèle
désarmé rend déjà `141/132/141`, parce que la **saturation** du crédit dépend de l'ordre.
C'est le **motif complet** des cinq nombres qu'il faut comparer, jamais un seul écart.

### Pages 1 à 4 — des grandeurs physiques, pas des niveaux

Chaque page fait varier **une** quantité à enveloppe constante : l'enveloppe disparaît dans
la pente, il ne reste que la grandeur cherchée.

| page | ce qu'elle mesure | notre modèle | écart à la droite |
|---|---|---|---|
| 1 | coût d'un **octet lu** (12/16/20/24 charges) | `227 179 141 119` → **4,15 cy/o.** | 1,97 % |
| 2 | coût d'une **lecture RAM 32 bits** (4/8/12/16) | `398 224 155 119` → **12,09 cy** | 0,24 % |
| 3 | coût d'une **division** (charge comprise) | `552 314 168 87` → **42,54 cy** | 0,34 % |
| 4 | coût d'une **multiplication** | `619 358 194 101` → **36,40 cy** | 0,32 % |

**DIV − MUL = 6,14 cy** chez nous. Les deux unités partagent la charge `ld WA,#imm16`, donc
elle **se simplifie dans la différence** : ce nombre-là ne suppose rien.

Page 1 attaque `fetch_wait_q4` (8,25 cy/mot), **jamais mesuré directement** — il avait été
ajusté pour encadrer la v8, donc il porte les erreurs de tout ce qui l'entoure. Page 2
attaque `MEM`, **le pire écart du corpus** (+12,1 % quand tout le reste est à +5 %).

### ⛔ Trois pièges rencontrés en construisant cette ROM — à lire avant d'y toucher

1. **Le crédit de file absorbe les petits k.** Première version des pages 1 et 2 : k = 1..8,
   les quatre points ne fermaient pas une droite (3,2 % et **11,3 %**) dans notre propre
   modèle. Une pente lue sur des points courbes ne vaut rien. Les points commencent
   maintenant hors de ce régime.
2. **`ld A,(XHL)` est pile à l'équilibre** : 2 octets pour 4 états = 8 cy d'exécution contre
   8,25 de bus, donc la boucle oscille autour du seuil de calage. Passée en **lecture
   32 bits** (`A3 20`, 6 états), l'écart à la droite tombe de 11,3 % à **0,24 %**.
3. **Le déplacement du `djnz` est sur 8 bits** : au-delà de ~125 octets de corps, asm900
   refuse (« Out of range for relative reference »). Ça plafonne la page 1 à 24 charges.

Et un piège de **banc**, pas de ROM : le pad n'étant relu qu'entre deux mesures (~60 trames),
un appui maintenu **franchit plusieurs pages**. Deux pages ont été interprétées à l'envers
avant que le numéro de page existe à l'écran. ⇒ La navigation du banc est désormais
**asservie** au chiffre affiché.

### 🎯 TIR SILICIUM v14 — 2026-08-27, RASV 198

Deux lectures par case ; le jitter est de ±1 partout.

#### Page 0 — le vidage est CONFIRMÉ, et notre modèle a la mauvaise FORME

| | A1 | B1 | C1 | A8 | C8 | la plus lente |
|---|---|---|---|---|---|---|
| désarmé | 141 | 132 | 141 | 165 | 165 | **B** |
| `keep=6` | 129 | 125 | 141 | 162 | 164 | **B** |
| `extra=6` | 129 | 121 | 130 | 163 | 162 | **B** |
| **SILICIUM** | **124** | **128** | **128** | **156** | **158** | **A** |

✅ **VIDAGE CONDITIONNEL CONFIRMÉ.** La rotation A — branche prise file **pleine** —
coûte **2,42 cy/branche** de plus que C, file **vide**, à instructions et branches
identiques. Et **B ≈ C** : dès qu'une charge a consommé le crédit, il n'y a déjà plus
rien à jeter. Aucune constante de coût ne peut produire ça.

⛔ **MAIS AUCUN DE NOS RÉGLAGES NE REPRODUIT LA FORME.** Le silicium dit que **A** est la
plus lente ; nous disons **B**, et ce pour *tous* les réglages de branche. Le désaccord
n'est donc pas dans le vidage mais dans la façon dont le crédit **se sature** d'une
instruction à l'autre. ⚡ **Ne pas caler un réglage de branche par-dessus ce désaccord-là.**

⚠️ `A8 − C8` ne porte que 80 branches : deux comptes d'écart y valent ~6 cy/branche, donc
le jitter ±1 le rend **inexploitable**. C'est `A1 − C1` qui porte.

🚨 **Et la porte avait le même défaut qu'en v13, sous une autre forme** : elle notait les
cinq nombres **bruts**. Le tir étant 12 % plus lent que tous nos modèles sur cette page,
l'écart quadratique était dominé par ce décalage **commun** et elle a élu `extra=6` — le
seul modèle qui baissait tout — alors que sa forme est justement celle que le silicium
contredit. Corrigé : la note porte sur les **écarts entre rotations**, insensibles au niveau.

#### Pages 1-4 — quatre grandeurs mesurées, quatre verdicts

| page | grandeur | SILICIUM | nous | écart | droite (Si) |
|---|---|---|---|---|---|
| 1 | **coût d'un octet lu** | **4,03 cy** (8,06 cy/mot) | 4,15 | **+3,0 %** | 0,35 % |
| 2 | **lecture RAM 32 bits** | **16,22 cy** | 12,09 | **−25,5 %** | 0,24 % |
| 3 | division (charge comprise) | 38,45 cy | 42,54 | **+10,7 %** | 0,59 % |
| 4 | multiplication (charge comprise) | 30,43 cy | 36,40 | **+19,6 %** | 0,48 % |
| 3−4 | **DIV − MUL** (la charge se simplifie) | **8,02 cy** | 6,15 | −1,87 cy | — |

⚡ **`fetch_wait_q4` MESURÉ DIRECTEMENT POUR LA PREMIÈRE FOIS : 8,06 cy/mot**, sur une
droite qui ferme à 0,35 %. Notre 8,25 est 3 % trop haut — et **8,06 est ce que la structure
prédit** (2 cycles de bus × 2 états × 2 cycles = 8,00). ⇒ Hypothèse : le +0,25 qu'on avait
ajusté ne mesurait pas le bus, il **compensait le coût de branche qu'on ne facturait pas**.

⛔ **`MUL` et `DIV` sont maintenant trop CHERS** (+19,6 % et +10,7 %), après avoir été
augmentés en juillet sous un fetch à 10 cy/mot — constante corrigée depuis à 8,25 puis
mesurée à 8,06. **Un nombre calé par-dessus un autre nombre faux ne survit pas à la
correction du second**, et c'est la deuxième fois que ces deux-là le démontrent.

⛔ **Une lecture RAM 32 bits nous coûte 25 % trop peu.** C'est `MEM` (+12,1 %, pire écart du
corpus), enfin localisé sur une grandeur unique plutôt que sur une enveloppe de boucle.

#### Recoupement v13 ↔ v14, et ce que ça donne pour la branche

Sur la v14, rotation A à deux densités : silicium **18,2 cy/branche** contre 11,3 chez nous.
La v13 donnait 17,5-18,8 contre 12,2 — **le même excès, sur une ROM différente**. Et la
rotation C (file vide) donne encore 16,3 contre 11,3.
⇒ **Sur ~6 cy/branche d'excès, ~2,4 seulement dépendent du crédit** (le vidage) ; le reste
est un sous-comptage **plat** de la branche prise.

Corpus rejoué avec une surcharge plate plus petite (`corpus_gate.py --extra 2 3 4`) :

| | écart moyen | pire |
|---|---|---|
| désarmé | 4,78 % | 12,31 % |
| `extra=2` | 3,13 % | 8,49 % |
| **`extra=3`** | **2,69 %** | **8,18 %** |
| `extra=4` | 3,08 % | 9,67 % |

⛔ **ET ON N'ARME RIEN.** `extra=3` améliore nettement la moyenne mais **dégrade les trois
cases qui étaient exactes** (WORK0 +0,0 → −3,4 %, REF +0,8 → −2,7 %, WORK1 −0,5 → −3,7 %)
et pousse `SHIFT` de −3,3 à −8,2 %. C'est la signature d'une correction **partiellement**
juste, posée par-dessus des constantes fausses.

### 📌 L'ordre des corrections, et pourquoi cet ordre

1. **`fetch_wait_q4` → 8,06** (mesuré direct, droite à 0,35 %) ;
2. **coût d'une lecture RAM** (mesuré direct, droite à 0,24 %) ;
3. **`MUL` / `DIV`** (mesurés direct, droites à 0,5 %) ;
4. **ensuite seulement** reprendre la branche — dont l'excès absorbe aujourd'hui les
   erreurs des trois premiers ;
5. et **avant tout ça**, comprendre pourquoi nos rotations se classent B < A < C quand le
   silicium dit A < B < C : c'est une erreur de **structure**, et aucune constante ne se
   cale proprement par-dessus.

---

## ✅ CORRECTIONS ARMÉES (2026-08-27) — corpus 4,78 % → **1,30 %**

Trois corrections issues du tir v14, chacune **mesurée directement**, plus la réparation
structurelle qu'elles ont rendue possible. 2115 tests verts.

| | avant | après | d'où vient le nombre |
|---|---|---|---|
| **fetch** | 8,25 cy/**mot** (ajusté) | **4,00 cy/octet** (`fetch_wait_byte_q16=64`) | v14 p.1 : 4,03 cy/o., droite à 0,35 % |
| **branche prise** | non facturée | **+4 cy** (`branch_taken_extra`) | v14 p.0 rotation C : 16,3 vs 11,3 cy/br. ; optimum corpus |
| **`mul` octet** | 15 états | **12 états** | v14 p.4 : 30,43 cy, droite à 0,48 % |
| **`div` octet** | 36 cycles | **32 cycles** | v14 p.3 : 38,45 cy, droite à 0,59 % |

### ⚡ Ce qui rendait le modèle sensible à l'adresse

Le bus cartouche est **8 bits** : il va chercher un octet par cycle de bus. Facturer
**par mot** — une seule fois par adresse paire — faisait dépendre le prix d'une
instruction de sa **parité** : 5 octets payaient 3 charges en partant d'une adresse
paire, 2 en partant d'une impaire. **50 % d'écart pour la même instruction.**

C'était la racine commune de **deux** anomalies : l'écart d'adresse de la v12 (silicium
682/682/683/682, nous 715/733/732/732 ⇒ maintenant **677/683/682/683**) et l'ordre des
rotations de la v14 (silicium A la plus lente, nous B ⇒ maintenant **A**, comme le
silicium, et `B ≈ C` comme lui).

### ⛔ Les deux corrections vont ENSEMBLE ou pas du tout

Le fetch par octet **seul** dégrade le corpus (4,78 % → **7,13 %**). Les 8,25 cy/mot
sur-facturaient le bus pour compenser la branche qu'on ne facturait pas.
⇒ `q64+e4` : **1,30 %** de moyenne, pire cas 12,31 % → **6,58 %**, avec ADD, MUL, L2, L3,
L4, WORK4 et A2/A3/A4 à **±0,1 %**.

⚡ **Et c'est ce qui a débloqué MUL/DIV.** Leurs valeurs avaient été relevées en juillet
sous un fetch à 10 cy/mot, puis ramenées « dans la bande » d'un +5 % commun faute de
mieux. Le biais ayant disparu, viser l'exactitude n'est plus masquer un biais : les deux
tombent maintenant **juste au-dessus du plancher datasheet** (12 états contre 11, 16
contre 15) — cohérent avec « latence variable, un peu plus lent que la table ».

### 🚨 Une sonde de test est devenue aveugle, et c'est instructif

`test_block_copy_drains_the_queue` mesurait le drain avec un run de `nop`. Sous le fetch
par octet, un `nop` coûte **exactement** 4,0 cy de bus pour 4,0 cy d'exécution : il est
pile à l'équilibre, la file ne prend jamais d'avance derrière lui, et vider un crédit
inexistant ne coûte rien. Les deux mesures tombaient sur le même nombre.
⇒ Sonde passée à `ld XWA,#imm32` (20 cy de bus contre 10 d'exécution). **L'assertion n'a
pas bougé** — et l'ancrage silicium réel (`test_bomberman_hicolor_phase`, 4120 cy/bande)
est resté vert tout du long. Même leçon que la ROM v13 : une sonde faite d'instructions à
l'équilibre ne mesure rien.

### 📌 Ce qui reste désarmé, et pourquoi

`data_wait_q16` (coût d'un octet de donnée lu) cale la page 2 de la v14 **au cycle près**
mais ne corrige **pas** le `MEM` du corpus (+6,6 %) et dégrade `WORK1`. La v14 n'a mesuré
**qu'une largeur** (32 bits) : impossible de savoir si le coût est par octet ou par accès.
⇒ **v15.** De même `branch_flush_keep` : la v14 le situe à **13-14** (silicium A1−C1 =
2,42 cy/branche contre 1,83 à keep=14), effet réel mais petit ; à confirmer.

---

## v15 — PROFONDEUR DE FILE, LARGEUR D'ACCÈS, ÉCRITURES (2026-08-27) — ⏳ ATTEND LE TIR

`a_queue_calib_v15.ngp` · md5 `895e629e7c013aece93df826de3b6190` · source `cpu_calib_v15.c`
Dépouillement : `python hw_calibration/v15_gate.py --p0 ... --rasv 198`
**GAUCHE/DROITE** = page, numéro affiché ligne 1.

### Page 0 — de combien la file prend-elle de l'avance ? (`biu_slack` est **déduit**)

Corps = `div WA,E` (2 o. pour ~32 cy : il laisse la file se remplir) + k charges
`ld XWA,#imm32` (5 o. pour 5 états : elles **dépensent** l'avance). k = 1, 2, 4, 8.
Le prix plein d'une charge est **déjà mesuré** (v14 p.1 : 20,15 cy). Ce que les premières
charges coûtent **en moins** est l'avance réelle, en cycles.

| | Q1 | Q2 | Q4 | Q8 | marginal | avance |
|---|---|---|---|---|---|---|
| notre modèle | 516 | 416 | 269 | 157 | 14,3 · 20,2 · 20,4 | **~5,8 cy** |

La marge k4→k8 doit retomber sur ~20,15 : c'est le contrôle du montage.

### Pages 1-2 — par OCTET ou par ACCÈS ? et l'écriture, jamais mesurée

Huit accès par tour, trois largeurs (1, 2, 4 octets) + contrôle de linéarité à seize,
en lecture puis en écriture.

| | 8×1o | 8×2o | 8×4o | 16×4o |
|---|---|---|---|---|
| notre modèle (lecture) | 301 | 301 | 216 | 116 |
| notre modèle (écriture) | 301 | 301 | 216 | 116 |

⛔ **Notre modèle affirme deux égalités** : `RB = RW` (donc coût **par accès**) et
`lecture = écriture`. Si le silicium casse l'une ou l'autre, c'est une erreur de
**forme**, pas de valeur — et il ne faudra pas la corriger en changeant un nombre.

---

## v16 — AVANCE DE LA FILE, ET LE COÛT RÉEL D'UNE INTERRUPTION (2026-08-27) — ✅ TIRÉE

`a_slack_calib_v16.ngp` · md5 `d81c580d6c632137f7ed3e6191b37708` · source `cpu_calib_v16.c`
Dépouillement : `python hw_calibration/v16_gate.py --p0 ... --p1 ... --rasv 198`
**GAUCHE/DROITE** = page, numéro affiché ligne 1.

Les deux dernières lignes d'`OPEN_ITEMS.md`, une page chacune.

### Page 0 — le bus peut-il travailler pendant un calcul long ?

`biu_slack = 16 cy` est **déduit** (4 octets × 4 cy), jamais mesuré. Il porte tout le
recouvrement fetch/exécution — et `branch_taken_extra = 4` a été calé **avec lui en place**.

⛔ **Et la page 0 de la v15 ne pouvait pas y répondre.** Elle lisait des **marges**, alors
que l'avance est dépensée **une fois par tour** : elle se voit dans l'**ordonnée**, pas dans
la pente. Des marges à plein tarif sont compatibles avec « aucune avance » *comme* avec
« une avance constante » — le montage ne séparait pas les deux. Corrigé ici.

Huit charges `ld XWA,#imm32` (160 cy de fetch contre 80 d'exécution ⇒ franchement limitée
par le bus), dans lesquelles on **insère** k divisions (2 octets, ~26 cy d'exécution). La
boucle reste limitée par le bus jusqu'à k=3, donc le **coût marginal d'une division** dit
tout :

| pente | interprétation |
|---|---|
| **~8 cy** | recouvrement **total** — seuls ses 2 octets se payent |
| **~34 cy** | recouvrement **nul** — exécution et fetch se sérialisent |
| entre | recouvrement **borné**, et la borne est l'avance réelle : `34 − pente` |

| | L8 | D1 | D2 | D3 | pente | avance | droite |
|---|---|---|---|---|---|---|---|
| notre modèle | 228 | 209 | 193 | 180 | **16,0 cy/div** | **18,0 cy** | 0,18 % |

### Page 1 — ce que coûte une interruption, à quatre cadences

`WORK1` est la dernière case du corpus au-dessus de 1,5 % (−4,6 %), et l'entrée en
interruption est déjà au **minimum des quatre valeurs documentées** (28/24/22/18 états,
indexées sur la largeur de bus de la zone de pile) : elle ne peut pas descendre.

⚠️ **Le tir v8 ne donnait que deux cadences.** Deux points font une droite qu'on ne peut pas
vérifier — l'erreur exacte qui avait coûté trois documents à la v10. Ici : cinq régimes du
**même lot de travail** (aucune IRQ, puis une toutes les 1, 2, 4, 8 lignes), donc quatre
points pour une droite vérifiable.

| | W0 | W1 | W2 | W4 | W8 | pente | droite |
|---|---|---|---|---|---|---|---|
| notre modèle | 180 | 144 | 161 | 170 | 175 | **134,0 cy/IRQ** | 0,35 % |

⚡ **Et l'écart est déjà visible** : la v8 avait mesuré **111 cy** au silicium, nous en
sommes à **134** — soit +21 %. Cohérent avec `WORK1` à −4,6 %, et assez large pour que
quatre cadences le pincent sans ambiguïté.

⛔ **Ce n'est PAS le coût de l'entrée seule** : aucun gestionnaire n'est installé
(volontaire — installer un vecteur utilisateur est le genre de chose qui fait booter une ROM
chez nous et planter la console). Si la pente matche après nos corrections, l'entrée à
18 états est innocente et le sujet est clos ; sinon, il faudra une ROM qui installe **son**
gestionnaire pour séparer l'entrée de ce que le stub fait — mais on saura alors que ça vaut
le risque.

⚠️ **W0 est le contrôle du tir** : aucune interruption, il doit retomber sur le lot de
travail nu. S'il dérive, le réglage du timer a fui et les quatre autres ne veulent rien dire.

Réglage timer repris **tel quel** de la v8 (timer 0 sur la broche externe TI0, mode 00) —
⛔ on ne « améliore » pas un montage déjà tiré sur console.

### 🎯 TIR SILICIUM v16 — 2026-08-27, RASV 198

| | L8 | D1 | D2 | D3 | pente | avance | droite |
|---|---|---|---|---|---|---|---|
| notre modèle (`biu_slack` = 16) | 228 | 209 | 193 | 180 | 16,0 | 18,0 cy | 0,18 % |
| **SILICIUM** | **228** | **199** | **177** | **158** | **26,5** | **7,5 cy** | 0,36 % |

| | W0 | W1 | W2 | W4 | W8 | pente | droite |
|---|---|---|---|---|---|---|---|
| notre modèle | 179 | 144 | 161 | 170 | 175 | 132,4 cy/IRQ | 0,23 % |
| **SILICIUM** | **180** | **150** | **164** | **173** | **177** | **114,2 cy/IRQ** | 0,54 % |

`W0` (le contrôle) retombe bien sur le lot de travail nu ⇒ le tir est valide. Et **114,2 cy
sur quatre points confirme les 111 de la v8**, qui n'en avait que deux.

### ⛔⛔ CE QUI SORT DE CE TIR N'EST PAS UN NOMBRE, C'EST UNE LIMITE DE FORME

`biu_slack` est **un seul scalaire** qui sert de plafond au crédit d'avance. Trois mesures
indépendantes le tirent maintenant dans **trois directions incompatibles** :

| mesure | ce qu'elle demande |
|---|---|
| v16 page 0 (division insérée dans une chaîne de charges) | **~6** |
| corpus (26 cases, 5 ROM) | **16** — à 6 l'écart moyen passe de 0,67 % à 1,99 % et le pire cas de 4,59 % à **10,81 %** |
| v16 page 1 (coût d'une interruption) | **> 16** — à slack 6 le coût d'IRQ monte de 132 à 147, alors qu'il faut descendre à 114 |

⚡ **Un scalaire ne peut pas satisfaire trois régimes à la fois.** Ce n'est donc pas un
réglage à trouver : c'est le modèle du recouvrement qui a la mauvaise **forme**. Il traite
comme une seule quantité ce qui est au moins deux choses différentes — combien le bus peut
prendre d'avance *pendant un calcul long*, et combien une instruction courte peut en
*dépenser* ensuite.

⛔ **RIEN N'EST CHANGÉ.** `biu_slack` reste à 16, `branch_taken_extra` à 4, corpus à
**0,67 %**, 2115 tests verts. Baisser le premier sans refaire le modèle dégraderait deux
mesures sur trois — et `branch_taken_extra` a été calé **avec** l'avance actuelle en place.

⚡ **Et une hypothèse séduisante a été réfutée en une mesure** : « une IRQ vide la file, donc
son coût contient `biu_slack` ; baisser l'un baisserait l'autre ». Faux — baisser `slack`
fait **monter** le coût d'IRQ (132 → 147). Les deux écarts ne sont pas le même écart.

### Ce que ça laisse pour la suite

1. **Le recouvrement, en tant que structure** — modéliser la file en OCTETS avec un débit de
   remplissage explicite, plutôt qu'en crédit de cycles plafonné. C'est le seul chemin qui
   peut satisfaire les trois régimes, et la v16 page 0 en est déjà le banc.
2. **L'interruption** — 114,2 cy mesurés contre 132,4. L'entrée est au minimum documenté
   (18 états), donc l'écart est dans ce que le stub BIOS fait. Le séparer demande une ROM
   qui installe **son** gestionnaire (un `reti` nu, puis le même précédé de N instructions
   tabulées). ⚠️ On sait maintenant que ça vaut le risque : l'écart est de **16 %** et il est
   confirmé sur quatre cadences.

---

## v17 — `mul` ET `div` EN FORME **MOT** (2026-08-27) — ✅ TIRÉE

`a_word_calib_v17.ngp` · md5 `e71b75bbcd52284fad95ff5233c796d5` · source `cpu_calib_v17.c`
Dépouillement : `python hw_calibration/v17_gate.py --p0 ... --p1 ... --rasv 198`

### Ce qu'elle débloque : le modèle de file en OCTETS

La v16 a montré que `biu_slack` avait la mauvaise **forme**. Le modèle physique a été
écrit — file de **4 octets**, un octet par 4 cycles, **aucun paramètre libre** :

```
manque = n − q     → le CPU cale (manque × coût_octet)
puis l'instruction consomme ses n octets   → q = max(0, q − n)
puis, PENDANT ses e cycles d'exécution     → q = min(4, q + e / coût_octet)
```

⚡ Sur le montage v16 page 0 il rend **26,6 cy/division contre 26,5 mesurés** — exact,
là où le crédit en cycles rendait 16,0. La différence tient à ce que le crédit laissait
**deux** charges profiter de l'avance (16 cy contre 10 de déficit chacune) alors que la
file ne contient jamais que 4 octets, soit les 4/5 d'une seule charge.

⛔ **Mais il n'est PAS armé**, et pas par doute sur le modèle : armé, le corpus passe à
−1 % **uniforme** (bon) sauf trois cases — **MUL −9,0 %, DIV −6,0 %, WORK1 −12,4 %** — et
l'ancrage Bomberman casse. Or ces trois-là sont exactement les constantes jamais
remesurées : les classes MUL/DIV de la v2 sont `v = v * w` sur des `u16`, donc la forme
**MOT** (19 états / 56 cycles), héritée du fetch à 10 cy/mot ; et `WORK1` est le coût
d'une IRQ, qui devra de toute façon être repris **après** que la file soit fixée.

⚡ Armer un nombre juste par-dessus des nombres faux, puis conclure que le nombre juste
est mauvais : c'est l'erreur qui a déjà coûté deux fois. D'où cette ROM, qui ne fait
qu'**une** chose.

### Le montage

Unité = `ld XWA,#imm32` + l'opération en forme mot, k unités par tour (k = 1, 2, 4, 8).

| | D1/P1 | D2/P2 | D4/P4 | D8/P8 | pente | droite |
|---|---|---|---|---|---|---|
| **DIV mot** (`DA 50`) | 1125 | 632 | 335 | 174 | **71,14 cy** | 0,36 % |
| **MUL mot** (`DA 40`) | 1415 | 817 | 442 | 231 | **53,04 cy** | 0,11 % |
| | | | | | **DIV − MUL 18,11** | |

Les deux unités ne diffèrent que par **un octet** (`0x50`/`0x40`) : `DIV − MUL` est le
surcoût de la division sans aucune hypothèse sur le reste.

⛔ `0x00020001 / 3 = 43691` : le quotient **tient** dans 16 bits. Un dividende plus grand
faisait déborder le quotient — chemin dégénéré, que le silicium peut facturer autrement.

### ⛔⛔ Un défaut de HARNAIS trouvé en construisant celle-ci

Première version à 200 tours : `D8` sortait à **397** quand `D4` valait 101 — **plus
rapide avec deux fois plus de travail**. Cause : la boucle extérieure compte les trames
en guettant `RAS_V` qui redescend ; si un bloc dure près d'une trame, deux lectures
consécutives **enjambent une bascule** et la trame n'est pas comptée. À 200 tours de huit
divisions mot, un bloc coûtait ~90 000 cycles contre 102 485 dans une trame.

⚡ **Règle : un bloc doit rester très court devant une trame — sinon l'horloge de
référence du montage compte faux, et elle compte faux SANS LE DIRE.** Ramené à 60 tours
(~27 000 cycles). ⚠️ La v14 pages 3/4 était à ~70 % d'une trame : sous le seuil, mais de
peu.

Et un défaut de **banc** : dès que les blocs sont courts, un appui maintenu franchit
plusieurs pages et **dépasse** la cible. La navigation de `v17_gate.py` est désormais
**bidirectionnelle**.

### 🎯 TIR SILICIUM v17 — RASV 198

| | 1 | 2 | 4 | 8 | pente | droite |
|---|---|---|---|---|---|---|
| DIV mot — nous | 1125 | 632 | 335 | 174 | 71,14 cy | 0,36 % |
| **DIV mot — SILICIUM** | **1220** | **699** | **377** | **196** | **62,70 cy** | **0,11 %** |
| MUL mot — nous | 1415 | 817 | 442 | 231 | 53,04 cy | 0,11 % |
| **MUL mot — SILICIUM** | **1559** | **931** | **515** | **272** | **44,44 cy** | **0,07 %** |

⚡ **Et le nombre robuste tombe juste** : `DIV − MUL` = **18,26 cy** au silicium contre
**18,11** chez nous. Les deux unités ne diffèrent que par un octet, donc la charge s'y
simplifie — **nos coûts RELATIFS étaient déjà bons, c'est le niveau commun qui était faux.**

⇒ **`div` mot = 47 cycles** (−0,7 %), **`mul` mot = 15 états** (+1,4 %), contre 56 et 19.

⚠️ Le quatrième nombre de la page 0 avait été noté `0019` (quatre chiffres). La droite
n'admet que **196** : elle ferme à 0,11 % là, contre 0,38 % pour la voisine. Confirmé
ensuite par le testeur.

### ⛔ ET LE TRIPLET NE PASSE TOUJOURS PAS — ce qui manque n'est pas une constante

Avec `queue_bytes = 4` + `div` mot 47 + `mul` mot 15 :

| | résultat |
|---|---|
| MUL / DIV du corpus | **+2,0 % / +1,9 %** — réparés (ils étaient à −9 et −6) |
| toutes les autres cases | **−0,9 à −2,3 %**, uniformément |
| `WORK1` (coût d'IRQ) | **−12,4 %** |
| corpus | 1,52 % (extra=3) à 1,89 % (extra=4) |
| `test_bomberman_hicolor_phase` | **ROUGE**, et le HUD de Cool Boarders part |

⚡ **Le biais uniforme de −1,5 % est le vrai sujet.** Il ne se compense pas par
`branch_taken_extra` (essayé à 1, 2, 3, 4), et 1,5 % suffit à décaler la phase du copieur
de Bomberman, dont la marge est d'**une ligne**. Il manque donc au modèle en octets une
pièce qu'il ne facture pas — pas une constante à retoucher.

### 📌 État : tout désarmé, et les trois nombres forment un TRIPLET

`queue_bytes = 0`, `div` mot **56**, `mul` mot **19**. Corpus **0,67 %**, 2115 tests verts,
HUD de Cool Boarders vérifié propre sur la savestate du testeur.

⛔ **Les 47 et 15 ne valent QU'AVEC la file armée** : posés sans elle, le corpus passe de
0,67 % à **2,30 %**. C'est le même piège que depuis le début — un nombre juste sous un
modèle est faux sous un autre. Les trois se bougent ensemble ou pas du tout.
`ngpc_set_queue_bytes(4)` et `ngpc_set_muldiv_word(15, 47)` les arment pour la mesure.

### 🔎 Localisation du biais de −1,5 % (2026-08-27)

Les quatre grandeurs directes de la v14 rejouées sous les deux modèles, avec les
constantes mot appropriées à chacun :

| grandeur | modèle courant (crédit) | **modèle en octets** |
|---|---|---|
| coût d'un octet lu | −0,2 % | **+0,6 %** |
| lecture RAM 32 b | −0,3 % | **−0,2 %** |
| div octet (+ charge) | −0,6 % | **+0,5 %** |
| mul octet (+ charge) | −0,1 % | **+0,7 %** |
| v16 p.0 (division insérée) | **+40 %** ❌ | **+0,4 %** ✅ |

⚡ **Le modèle en octets matche TOUTES les mesures directes** — y compris celle que le
crédit en cycles rate de 40 % — et ne décroche que sur le **corpus**, uniformément.

⚠️ Or les cases du corpus ne sont pas des mesures du même genre : ce sont des boucles **en
C** (v2 : `v = v * w` sur des `volatile`), avec pile, `push`/`pop` et branches courtes,
là où toutes les ROM de calibration mesurent de l'**asm écrit à la main**. Le biais est donc
dans quelque chose que les boucles asm n'exercent pas.

🔎 **Et un indice de plus** : sous le modèle en octets, `BASE`, `A1`, `REF` et `WORK0`
tombent à −2,2/−2,3 % pendant que `A2`, `A3`, `A4` restent à −0,9/−1,0 %. Un écart
**bimodal** entre quatre exécutions de la *même* boucle à des adresses différentes — alors
que le modèle en octets ne lit plus l'adresse du tout. Ça, ce n'est pas un biais de
constante : c'est une piste.

### 🔬 Trois pistes fermées, et le vrai blocage identifié (2026-08-27)

Sous le modèle en octets (file 4, `mul`/`div` mot 15/47) :

| piste | test | verdict |
|---|---|---|
| le **coût d'accès mémoire** (pile comprise) | désarmé → 2,08 % contre 1,89 % | ⛔ **écartée**, ça empire |
| le **coût par octet**, remesurable sous ce modèle | balayé 3,81 → 4,00 cy | ⛔ optimum 1,52 %, jamais la parité |
| la **surcharge de branche** | balayée 2, 3, 4 | ⛔ idem |
| l'IRQ ne vidait pas la file (**vrai bug**) | corrigé | ⚠️ **zéro effet** sur les nombres |

⚡ **Et `WORK1` est le pire cas dans TOUTES les configurations** — 11 à 12,4 %, quand aucune
autre case ne dépasse 2,3 %. Hors `WORK1`, le modèle en octets tourne à ~1,1 %.

⇒ **Le blocage est l'interruption, et il est déjà chiffré indépendamment** : la v16 page 1
mesure **114,2 cy** contre **132,4** chez nous (+16 %), sur quatre cadences. Tant que
l'interruption est fausse de 16 %, elle empoisonne le seul corpus qui puisse arbitrer le
modèle de file — et elle l'empoisonne *deux fois plus* sous le modèle en octets.

📌 **Prochaine ROM : l'interruption, avec SON gestionnaire.** Un `reti` nu, puis le même
précédé de N instructions tabulées : la différence isole le coût du stub BIOS et laisse
l'entrée seule. C'est le seul montage qui les sépare, et on sait maintenant que ça vaut le
cycle de flash — c'est lui qui débloque tout le reste.

⛔ Le bug de la file non vidée à l'interruption est **corrigé quand même** : un modèle qui
contredit le mécanisme décrit par son propre commentaire est faux, quel que soit ce que ça
donne sur le corpus.

---

## v18 — DÉCOMPOSER LE COÛT D'UNE INTERRUPTION (2026-08-27) — ✅ TIRÉE

`a_irqdec_calib_v18.ngp` · md5 `f5488eda1c3bd14127100c2bce974fb6` · source `cpu_calib_v18.c`
Dépouillement : `python hw_calibration/v18_gate.py --p0 ... --p1 ... --rasv 198`

### Pourquoi elle débloque tout le reste

Le modèle en octets reproduit **toutes** les mesures directes (v14 à ±0,7 %, v15, et la
v16 p.0 à +0,4 % là où le crédit en cycles se trompe de 40 %). Il ne décroche que sur le
corpus — et dans **toutes** les configurations essayées la pire case est `WORK1`, à
11-12,4 %, quand aucune autre ne dépasse 2,3 %. **Hors `WORK1`, il tourne à ~1,1 %.**

Trois pistes ont été fermées pour rien (coût d'accès, coût par octet, surcharge de
branche). Le blocage est l'interruption, déjà chiffrée par la v16 p.1 : **114,2 cy contre
132,4**, +16 %.

⛔ **Et la v16 ne pouvait pas aller plus loin** : elle mesure le coût *complet* en un seul
nombre. Pour savoir **où** sont les 18 cycles il faut faire varier le gestionnaire — donc
en installer un à soi.

⚠️ **Ce qui rend ça sûr** : le vecteur Timer 0 est en `0x6FD4`, et le template le déclare
déjà (`TI0_INT`, un pointeur de fonction) — `InitNGPC()` y installe `DummyFunction` à
chaque démarrage. On écrit un pointeur là où le SDK en écrit déjà un. Pas de vecteur
inventé, pas de convention supposée.

### Le montage

Le même lot de travail, une impulsion TI0 par ligne, sous des gestionnaires de tailles
croissantes. La droite « coût par IRQ contre taille du gestionnaire » sépare les deux :

- sa **pente** = coût d'une instruction exécutée **dans** un ISR ;
- son **ordonnée** = coût **FIXE** d'une interruption (entrée + aiguillage BIOS + retour).

| page | échelle | ce qu'elle voit |
|---|---|---|
| 0 | `nop` × 0, 8, 24 | le coût **fixe**, proprement (le `nop` est à l'équilibre) |
| 1 | `ld XWA,#imm32` × 0, 2, 4, 8 | l'état de la **file au redémarrage** après l'IRQ |

Gestionnaires vérifiés à l'octet : `h_n0` = un seul `07` (RETI), `h_l0` = `38 58 07`
(push XWA / pop XWA / RETI). Les `push`/`pop` sont dans les **quatre** gestionnaires de la
page 1 — un ISR qui écrase XWA sans le sauver corromprait le code interrompu — donc ils se
simplifient dans les différences.

### Prédictions — et elle discrimine AUSSI les deux modèles de recouvrement

| | W0 | N0 | N8 | N24 | pente | **FIXE** |
|---|---|---|---|---|---|---|
| crédit (courant) | 299 | 239 | 224 | 195 | 4,23 cy/nop | **135,3 cy** |
| file 4 octets | 296 | 217 | 203 | 174 | 3,99 cy/nop | **180,0 cy** |

| | L0 | L2 | L4 | L8 | pente |
|---|---|---|---|---|---|
| crédit (courant) | 226 | 216 | 198 | 162 | 20,29 cy/charge |
| file 4 octets | 206 | 191 | 172 | 136 | 20,88 cy/charge |

⚡ **Un seul tir tranche deux questions.** Si la pente vaut ~4 cy/nop, le code d'un ISR est
facturé juste et **tout** l'écart est dans la constante fixe — une seule chose à corriger,
et elle ne dépend plus du modèle de recouvrement. Si elle ne les vaut pas, c'est la façon
dont on facture le code d'un gestionnaire qu'il faut chercher **avant** de toucher à
l'entrée. Et l'ordonnée sépare au passage les deux modèles : 135 contre 180.

### 🎯 TIR SILICIUM v18 — RASV 198

| | W0 | N0 | N8 | N24 | pente | **FIXE** |
|---|---|---|---|---|---|---|
| **SILICIUM** | **299** | **250** | **235** | **207** | **4,03 cy/nop** | **111,1 cy** |
| nous | 298 | 240 | 226 | 198 | 3,96 | 131,2 |

| | L0 | L2 | L4 | L8 | pente |
|---|---|---|---|---|---|
| **SILICIUM** | **239** | **221** | **203** | **167** | **20,29 cy/charge** |
| nous | 229 | 218 | 200 | 164 | 18,78 |

`W0` (le contrôle) tombe sur notre valeur ⇒ le tir est valide.

**Trois réponses d'un coup :**

1. ✅ **Le code d'un ISR est facturé juste.** Un `nop` y coûte **4,03 cy**, exactement ce
   qu'il coûte ailleurs. Ce n'est donc pas la façon dont on facture un gestionnaire.
2. ✅ **La file redémarre dans le bon état après une interruption.** Une charge dans un ISR
   coûte **20,29 cy** contre 20,15 mesurés hors ISR (v14) — plein tarif, donc la file est
   bien vide à l'entrée. ⇒ **Rien à chercher de ce côté**, et la piste « le modèle en
   octets ne facture pas le redémarrage » est **fermée**. (Au passage, notre crédit en
   cycles donne 18,78 : il laisse à l'ISR un crédit qu'il ne devrait pas avoir.)
3. ✅ **Le coût FIXE d'une interruption est 111,1 cy** — troisième mesure indépendante après
   la v8 (111) et la v16 (114,2). Elles convergent.

### ⛔ Et l'excès n'est PAS dans l'entrée

Balayage de `irq_entry_cycles` : même à **9 états** — la moitié du minimum documenté par
Toshiba (28/24/22/18, indexés sur la largeur de bus de la zone de pile) — on plafonne à
**113,1 cy**, jamais 111,1. La part non-entrée vaut ~95 cy dans notre modèle et devrait en
valoir ~75.

⇒ **L'excès est dans l'exécution du stub d'aiguillage du BIOS**, ~20 cy sur ~95. Et
l'entrée à 18 états, confirmée par la v8, n'est pas le suspect.

### ✅ Un correctif mesuré au passage : la surcharge de branche ne vaut que pour la CARTOUCHE

`branch_taken_extra` modélise le coût de **rechargement de la file sur le bus 8 bits de la
cartouche** — c'est là qu'il a été mesuré (v14 rotation C). Le BIOS et la RAM ne sont pas
sur ce bus et leur fetch est facturé zéro : leur faire payer un rechargement qu'ils ne
subissent pas surfacture tout le code du BIOS, donc **chaque interruption**, qui passe par
son aiguillage.

Conditionné à la cartouche : **corpus 0,67 % → 0,59 %**, pire cas 4,59 % → **3,67 %**,
`WORK1` −4,6 % → **−3,7 %**, 2115 tests verts. Coût d'IRQ 135,6 → 131,2.

### 🔬 L'écart d'IRQ, localisé à UNE instruction (2026-08-27)

Trace complète d'une interruption, instruction par instruction. Le stub d'aiguillage du
BIOS fait **trois** instructions :

```
FF22A5  d1 d6 6f 04   push (0x6FD6)   \  il empile le vecteur utilisateur
FF22A9  d1 d4 6f 04   push (0x6FD4)   /  (Timer 0 = 0x6FD4, quatre octets)
FF22AD  0e            ret             -> et saute dessus
```

Élégant : le BIOS **empile le vecteur et fait `ret` dessus**. Et les deux `push` font
**exactement les mêmes accès** — 1 lecture 2 o. + 1 écriture 2 o. — pour des coûts de
**40 et 24 cycles**. Le second vaut 24 partout ailleurs dans le flot.

⇒ **Le symptôme est net : la PREMIÈRE instruction après une interruption coûte +16 cy chez
nous, systématiquement.** 16 = 4 octets × 4 cy, soit exactement un fetch d'instruction de
plein tarif. Sur ~132 cy de coût fixe et 20 d'écart au silicium, c'est l'essentiel.

⛔ **Et ce n'est PAS le vidage de file.** Un `irq_flush_keep` (crédit qui survit à une IRQ,
par analogie avec les 13-14 cy mesurés pour une branche en v14) a été ajouté et balayé de 0
à 16 : le coût fixe ne bouge que de 131,2 à 129,6. La boucle de travail est limitée par le
bus, donc il n'y a pas de crédit à jeter au moment de l'interruption.

⛔ **Ni l'entrée.** Balayage de `irq_entry_cycles` jusqu'à **9 états** — la moitié du
minimum documenté — : on plafonne à 113,1, jamais 111,1.

📌 **Cible pour la suite, et elle est étroite** : trouver ce qui facture 16 cycles de fetch
à la première instruction d'un ISR alors qu'elle est en **BIOS**, région dont le fetch est
facturé **zéro** (`bios_wait = 0`). La trace est reproductible
(`hw_calibration/irq_trace.py`), le symptôme est à l'instruction près, et il ne dépend
d'aucune constante à régler.

### 🔎 Le +16 : ce qui est éliminé, et ce qui reste (2026-08-27, suite)

L'écart de la première instruction d'un ISR vaut **exactement `2 × bios_wait`** — 0 à
`bios_wait = 0`, 8 à 4, 16 à 8. Il **survit** à :

| candidat | test | verdict |
|---|---|---|
| le crédit d'avance jeté par l'IRQ | `irq_flush_keep` balayé 0→16 | ⛔ le fixe ne bouge que de 131,2 à 129,6 |
| le coût d'accès mémoire | désarmé | ⛔ écart inchangé (16) |
| le fetch par octet | désarmé | ⛔ inchangé |
| la surcharge de branche | désarmée | ⛔ inchangé |
| **tout le modèle de recouvrement** | pipeline désarmé | ⛔ **inchangé** (56 et 40, écart 16) |
| le trafic propre de la livraison d'IRQ | `access_wait` remis à zéro après l'entrée | ⛔ inchangé, corpus neutre |

⚡ **Le pipeline désarmé est la mesure qui tranche** : l'écart n'est pas dans le modèle de
bus, il est dans `access_wait` lui-même — la première instruction d'un ISR se voit facturer
**deux lectures de mot BIOS de plus** que la même instruction ailleurs.

⚠️ Et `bios_wait = 8` n'est pas zéro : contrairement à ce que j'ai supposé deux fois dans
cette campagne, **le fetch BIOS n'est pas gratuit**. C'est lui qui porte l'écart.

📌 **Reste à instruire** : combien d'octets le décodeur lit réellement pour la première
instruction d'un gestionnaire, et pourquoi ce compte diffère de celui de la même
instruction hors interruption. Le banc était `scratchpad/bisect_irq.py` ⛔ **(disparu avec sa session — un banc qui compte va dans le dépôt, pas dans un scratchpad ; ceux d'aujourd'hui sont dans `hw_calibration/`)** — il rejouait les six
lignes du tableau ci-dessus en une commande.

### 🔬 Le +16 est un STALL, pas un fetch (2026-08-27, instrumenté)

Compteur temporaire sur la charge `bios_wait` (`ngpc_dbg_bios_charges`, banc
`scratchpad/count_bios.py`, ⛔ **disparu lui aussi**), instruction par instruction :

```
FF22A5  d1 d6 6f 04   cy=40   charges bios = 2
FF22A9  d1 d4 6f 04   cy=24   charges bios = 2
FF22AD  0e            cy=18   charges bios = 0
```

⚡ **Les deux chargent EXACTEMENT le même nombre de mots BIOS.** `access_wait` vaut donc 16
pour les deux, et le coût de base est identique (8 états × 2 = 16, plus 8 de données).
⇒ Les 16 cycles d'écart sont un **stall**, c'est-à-dire un `biu_debt` qui vaut **+16** en
entrant dans la première instruction du gestionnaire — alors que l'instruction interrompue
ne peut pas laisser de dette positive (elle est remise à zéro à chaque pas).

⛔ Ce que ça élimine en plus : la piste « le décodeur lit plus d'octets pour la première
instruction d'un ISR » est **fermée** — il en lit exactement autant.

📌 **Prochain pas, et il est mécanique** : instrumenter `biu_debt` à l'entrée de chaque
instruction et voir d'où vient ce +16 au moment de la livraison d'interruption. Le compteur
`dbg_bios_charges` est en place et se duplique en trois lignes pour `biu_debt`.

### ✅✅ RÉSOLU — l'entrée en interruption facturait la lecture de son vecteur

**C'était un bug, pas une constante.** `access_wait` est remis à zéro à la **fin** d'un pas
d'instruction ; la livraison d'interruption se fait **après**. Or `deliver_irq` **lit le
vecteur** — quatre octets en BIOS, soit deux mots à `bios_wait` = **16 cycles** — qui
s'accumulaient dans un `access_wait` que plus personne ne remettait à zéro, et **retombaient
sur la première instruction du gestionnaire**.

C'est exactement ce que la trace montrait, et ce que six éliminations successives avaient
cerné sans le nommer :

```
avant :  FF22A5  push (0x6FD6)  cy=40  access_wait=32  stall=16
         FF22A9  push (0x6FD4)  cy=24  access_wait=16  stall=0
après :  FF22A5  push (0x6FD6)  cy=24  access_wait=16  stall=0
         FF22A9  push (0x6FD4)  cy=24  access_wait=16  stall=0
```

⚡ **Ce qui a fini par le trouver : instrumenter, pas déduire.** Un compteur des charges
`bios_wait` a montré que les deux instructions en chargeaient **autant** — donc l'écart
n'était pas dans l'instruction. Un second compteur sur `biu_debt`/`access_wait` a montré
`access_wait = 32` là où l'instruction n'en justifiait que 16. Les 16 orphelins ne pouvaient
venir que d'avant elle.

🚨 **Et il y avait DEUX points de livraison d'interruption** (le pas normal, et le réveil
depuis `HALT`). Le second n'avait **aucune** des gardes. Corrigé aussi : deux chemins qui
livrent la même interruption doivent la livrer pareil.

| | avant | après | silicium |
|---|---|---|---|
| coût FIXE d'une IRQ | 131,2 cy (+18 %) | **113,8 cy (+2 %)** | 111,1 |
| page 0 | 298/240/226/198 | **298/248/233/205** | 299/250/235/207 |
| `WORK1` (corpus) | −3,7 % | **−0,9 %** | — |
| `WORK4` | −0,8 % | **+0,0 %** | — |
| **corpus, écart moyen** | 0,59 % | **0,40 %** | — |
| **corpus, pire cas** | 3,67 % | **1,89 %** | — |

**2115 tests verts.** Toutes les cases du corpus sont désormais sous 2 %.

📌 Les deux compteurs d'instrumentation (`ngpc_dbg_bios_charges`, `ngpc_dbg_biu`) sont
conservés : ils ont trouvé ce bug en deux mesures là où six raisonnements successifs
n'avaient fait que l'encercler.


### 🚌 2026-08-29 — LE MODÈLE EN OCTETS NE RECOUVRAIT QUE LA CARTOUCHE

**Point de départ.** Avec le triplet armé (`queue_bytes = 4`, `mul`/`div` mot 15/47), le
corpus était noté « toutes les cases à −0,9 à −2,3 %, la machine entière ~1,5 % trop
lente ». **Ce n'est plus vrai depuis le correctif d'IRQ du 27/08** — et le relire sans le
remesurer aurait envoyé chercher un biais uniforme qui n'existe plus.

🚨 **Et le banc mentait.** `corpus_gate.py` faisait `if extra:` : `--with-extra 0` ne
désarmait rien, il laissait le défaut à **4**. Deux colonnes du tableau sortaient au
chiffre près identiques sans que rien ne le signale — c'est exactement le défaut déjà
corrigé sur `--dw`, resté sur l'autre option. Corrigé (`None` = défaut, `0` = désarmer) ;
le balayage est monotone depuis.

**Ce que la v18 dit du modèle en octets, une fois le banc honnête :**

| | pente `nop` | pente charge | **FIXE** |
|---|---|---|---|
| **SILICIUM** | **4,03** | **20,29** | **111,1** |
| crédit en cycles (livré) | 4,04 | 18,26 ⛔ | 113,8 |
| file 4 octets | 4,08 ✅ | **20,08** ✅ | 156,1 ⛔ |

⚡ **Les deux PENTES du modèle en octets tombent juste, celles du crédit non** (18,26
contre 20,29 : il laisse à l'ISR une avance qu'il n'a pas). Le décrochage n'est donc pas
diffus — il est tout entier dans l'**ordonnée**, c'est-à-dire dans l'entrée d'interruption.

### 🔬 L'excès suit `bios_wait` — donc il se nomme

| `bios_wait` | 8 | 4 | 0 |
|---|---|---|---|
| FIXE, file armée | 156,1 | 141,4 | 124,1 |

⇒ **Le fetch BIOS s'ajoutait BRUT.** Le modèle en octets ne faisait passer par la file que
la **cartouche** ; le BIOS restait sur `access_wait`, ajouté sans le moindre recouvrement
avec l'exécution, là où le crédit en cycles l'absorbait. Et le chemin d'une interruption
est presque entièrement en BIOS (lecture du vecteur + aiguillage `push`/`push`/`ret`).

✅ **Corrigé — et sans introduire un seul paramètre.** Le BIOS est sur le **même bus**,
donc dans la **même file** ; son prix reste celui de sa région (`bios_wait` par mot, soit
`bios_wait / 2` par octet, porté par `fetch_bc16`). À 8 par mot cela fait 4,00 cy/octet,
exactement le tarif cartouche mesuré par la v14 : la moyenne ne bouge pas. Ce qui change,
c'est que ce temps se **recouvre** et ne dépend plus de la parité de l'adresse.

| file armée (`extra = 4`) | avant | après |
|---|---|---|
| v18, coût FIXE d'une IRQ | 156,1 | **139,1** |
| corpus, écart moyen | 1,61 % | **0,96 %** |
| corpus, pire cas (`WORK1`) | 8,72 % | **5,05 %** |

⚖️ Le modèle **livré** (crédit en cycles) est bit-identique : la correction est sous
`queue_bytes`, désarmé par défaut. Corpus 0,40 % / 1,89 %, 2115 tests verts.

### ⛔ Résultat NÉGATIF — la file ne se recharge pas assez pendant l'acceptation

Reste 28 cy. Hypothèse suivante, et elle a le mérite d'être déjà admise ici pour les
périphériques (« les cycles d'une entrée d'interruption sont des cycles **comme les
autres** ») : pendant les 18 états d'acceptation, le bus tourne, donc la file se recharge.
Balayée par `ngpc_set_irq_queue_keep_q16` (diagnostic, défaut 0), contre **deux** mesures
indépendantes :

| file à l'entrée | 0 o. | 1 o. | 2 o. | 3 o. | **4 o. (plafond)** | silicium |
|---|---|---|---|---|---|---|
| v18, FIXE | 139,1 | 136,1 | 133,2 | 131,4 | **126,6** | **111,1** |
| v8 `WORK1` | −5,0 % | −4,1 % | −3,7 % | −2,8 % | **−2,3 %** | — |

⚡ Les deux mesures vont dans le **même sens**, ce qui rend le mécanisme crédible — mais
**même au plafond physique** (4 octets, la file entière, il n'y a pas de « plus ») il
reste **15 cy** d'écart. ⇒ Le rechargement pendant l'acceptation ne peut pas être la seule
pièce manquante, et **le paramètre reste à 0** : on n'arme pas un mécanisme qui ne ferme
pas ce qu'il prétend fermer.

📌 **Cible pour la suite.** L'écart restant est propre au modèle en octets (le crédit rend
113,8) et vaut ~15 cy par interruption, soit ~4 octets de fetch. À instruire par
**compteur**, pas par raisonnement — c'est ce qui a trouvé le bug du 27/08 : combien
d'octets le modèle facture-t-il entre la dernière instruction interrompue et la fin du
stub d'aiguillage, et combien la machine en lit-elle vraiment ? Bancs :
`hw_calibration/irq_trace.py` (trace côte à côte des deux modèles) et
`hw_calibration/irq_keep_gate.py` (le tableau ci-dessus en une commande).


### 🚨 2026-08-29 (suite) — DEUX BANCS QUI NE CONDAMNAIENT PAS, ET UNE ROM REJETÉE

⛔ **`irq_trace.py` traçait la mauvaise interruption.** Il s'arrêtait à la **première**
livraison venue — en pratique le **VBlank**, dont le gestionnaire BIOS fait des centaines
de cycles et ne rejoint **jamais** la cartouche. Ce n'est pas le chemin que les ROM
v8/v16/v18 mesurent. ⇒ Il sélectionne désormais la livraison qui atteint le gestionnaire
**utilisateur** (elle quitte le BIOS dans les instructions qui suivent).

⛔ **Et il lisait deux exécutions comme une seule.** Les deux modèles étaient tracés
séparément puis affichés côte à côte ligne à ligne — or ils ne tombent ni sur la même
interruption ni au même endroit. 🚨 **Les 5 lignes désalignées étaient EXACTEMENT celles
du chemin d'interruption**, c'est-à-dire les seules dont on tirait des conclusions.
⇒ Chaque modèle est maintenant tracé et totalisé avec **ses propres** PC.

⚖️ *Deux fois dans la même journée, un banc a produit un tableau propre et faux. Le point
commun : il affichait une colonne dont il ne pouvait pas garantir la provenance.*

### 🎯 Le terme dominant, une fois la trace honnête

Chemin TI0 réel (stub BIOS `FF22A5` / `FF22A9` / `FF22AD`, puis `RETI` du gestionnaire
vide en cartouche) :

| | acceptation | stub + `reti` | total |
|---|---|---|---|
| crédit en cycles | 36 | 24 + 24 + 18 + 28 | **130** |
| file en octets | 36 | **40** + 24 + 18 + 28 | **146** |

⚡ **Les 16 cycles d'écart sont UNE ligne** : la première instruction du stub part file
**vide** (`file@entrée = 0.00`, 4 octets à lire) et paie un calage plein. C'est le vidage
à l'acceptation, et rien d'autre — les trois instructions suivantes coûtent le même prix
dans les deux modèles.

⇒ Cohérent avec le balayage : remplir la file au plafond pendant l'acceptation retire
bien ~12,5 cy… et il en reste **15,5**, hors des quatre instructions du chemin. La suite
est donc à chercher dans la **reprise** (le flot interrompu qui retrouve sa phase), pas
dans l'entrée.

### ⛔ ROM v19 — CONSTRUITE, PUIS REJETÉE PAR SON PROPRE CRITÈRE

`cpu_calib_v19.c` / `a_retq_calib_v19.ngp` (md5 `0a9cffdf080d17947cddb814ad313e4e`),
dépouillement `v19_gate.py`. Idée : si une interruption vide la file, son coût doit
dépendre de la **largeur en octets** du code dans lequel elle revient (1 / 2 / 3 / 5).

⛔ **Elle ne discrimine pas.** Notre propre modèle en octets rend **137,6 / 138,8 / 140,3**
pour 2 / 3 / 5 octets — **plat**. Les deux modèles ont la **même forme** et ne diffèrent
que d'un décalage constant (~25 cy) :

| | 1 o. | 2 o. | 3 o. | 5 o. |
|---|---|---|---|---|
| crédit | 133,0 | 115,0 | 114,9 | 115,0 |
| file | 149,2 | 137,6 | 138,8 | 140,3 |

Raison : une instruction plus large cale plus longtemps **mais recharge aussi plus
longtemps** pendant son exécution — les deux se compensent. La prédiction était fausse,
et c'est le banc qui l'a dit avant le silicium.

⚖️ **Elle n'est PAS proposée au tir.** Faire flasher une ROM dont on sait qu'elle ne peut
pas trancher, c'est demander une mesure pour rien. Les fichiers restent au dépôt : un
montage réfuté est une information, et sa page 0 (les quatre boucles **sans** IRQ, à
largeurs d'instruction croissantes) reste un banc de fetch honnête si une question de
largeur se repose.

📌 **Ce qu'il faudrait mesurer maintenant** : le coût d'une interruption contre la
**phase** du flot interrompu, pas contre la largeur de ses instructions.


### ⛔ 2026-08-29 (fin) — LA REPRISE EST RÉFUTÉE, ET LES TERMES NE FONT PAS LE COMPTE

Nouveau banc `hw_calibration/irq_reprise.py`. Il ne suppose rien : il accumule un
histogramme `PC -> (passages, cycles)` sur le code **cartouche**, une fois interruptions
interdites (page 0 de la v19) et une fois autorisées (page 1), **sur la même boucle**, et
compare **par PC**. Une instruction plus chère au même PC serait ralentie par les
interruptions sans être dans leur chemin.

⛔ **Il fallait le verrouiller sur une boucle.** Chaque page enchaîne les quatre largeurs
une seconde chacune : échantillonner « page 0 » puis « page 1 » comparait le flot de
`ld XWA` d'un côté et celui de `ld A` de l'autre. Ici ça s'est vu (aucun PC commun) ; si
les adresses s'étaient trouvées partagées, **la comparaison aurait été silencieusement
fausse** — le même motif que les deux autres bancs de la journée.

| | surcoût par instruction hors chemin | par interruption |
|---|---|---|
| file en octets | **−0,224 cy** | **−1,1 cy** |
| crédit (contrôle) | −0,574 cy | −2,1 cy |

⇒ **Aucun surcoût de reprise. La piste est fermée**, et elle l'est pour les deux modèles.

### 🚨 Et voici le vrai point de sortie : les termes mesurés NE FONT PAS le compte

| terme | mesuré |
|---|---|
| coût par IRQ, crédit (≈ silicium 111,1) | **113,2** |
| coût par IRQ, file en octets | **138,6** |
| écart à expliquer | **+25,4** |
| — vidage de file à l'acceptation (trace, 1 ligne) | **+16** |
| — reprise du flot interrompu (census, par PC) | **≈ 0** |
| — `bios_wait`, `branch_taken_extra` | réfutés (balayages) |
| **reste non localisé** | **~9 cy/IRQ** |

⚠️ **C'est une contradiction, pas une approximation** : trois instruments (trace à
l'instruction, census par PC, balayages) couvrent le chemin et n'y trouvent que 16 des
25,4 cycles que le dépouillement de la v18 mesure. ⇒ **Soit un terme échappe aux trois,
soit l'ordonnée de la v18 est biaisée sous le modèle en octets** (elle est extrapolée de
trois points ; son résidu de fit est pourtant faible, 1,0).

📌 **C'est la question à prendre en premier la prochaine fois, et elle est beaucoup plus
étroite que « le modèle est 1,5 % lent »** : reconstruire le coût d'UNE interruption par
somme directe des cycles (livraison → retour dans le flot), sur des CENTAINES
d'interruptions, et le confronter au 138,6 du dépouillement. Si les deux ne se rejoignent
pas, c'est le dépouillement qu'il faut corriger, pas le modèle.


## ⚡⚡ 2026-08-29 — LA RISTOURNE : une interruption rendait le code interrompu MOINS CHER

Les ~9 cy « que personne ne voit » sont trouvés, et ce n'est pas un terme manquant :
**c'est que les deux méthodes ne mesuraient pas la même grandeur.**

### Les deux mesures, et leur écart

Nouveau banc `hw_calibration/irq_sum_gate.py` : somme **directe**, interruption par
interruption, de tout ce qui s'exécute entre la livraison et le retour à l'adresse de
reprise (acceptation comprise). ⛔ L'ancre est le `next_pc` de l'instruction interrompue —
une première version guettait un *ensemble* de PC identifié sur une autre page, et a
compté **zéro** interruption (les pages n'y tournent pas sur la même largeur au même
moment).

| | crédit | file | écart |
|---|---|---|---|
| **somme directe** (coût du chemin) | **130,0** | **146,0** | **+16,0** |
| **dépouillement v18** (perte de débit) | 113,2 | 138,6 | +25,4 |

σ = 0,0 sur des centaines d'occurrences, 4 instructions par chemin : le chemin est
parfaitement déterministe, ce n'est pas du bruit.

### 🚨 Ce qui les sépare : une ristourne impossible

`irq_reprise.py` l'avait déjà mesurée sans qu'on en voie la portée : **la boucle
interrompue tourne PLUS VITE quand les interruptions sont autorisées** — −0,574
cy/instruction sous le crédit, −0,224 sous la file. Sur ~27 instructions de boucle par
interruption : **~15 et ~6 cy de ristourne**, du bon signe et du bon ordre pour couvrir
exactement les **16,8** et **7,4** qui séparent les deux lignes du tableau.

⛔ **Une interruption ne peut pas rendre le code interrompu moins cher.** C'est un
artefact des deux modèles : les cycles de l'ISR **rechargent la file** (ou soldent la
dette) du flot interrompu, alors que le bus est occupé à chercher **les octets de l'ISR**,
pas les siens.

### ⚖️ Ce que ça change, et ce que ça ne change PAS

⚠️ Le silicium n'est mesurable **qu'en débit** : ses 111,1 cy se comparent à **113,2 et
138,6**, pas à 130 et 146. **Le verdict du dépouillement tient** — le modèle en octets est
bien +25 % sur le coût d'une interruption.

⚡ Ce que la somme directe ajoute est plus dérangeant : **l'accord du crédit avec le
silicium REPOSE sur une ristourne de ~17 cy.** Ôtez l'artefact des deux modèles et c'est
le **crédit** qui devient faux (130 contre 111,1). ⇒ La ristourne n'est pas un défaut de
mesure, c'est **une pièce du modèle** — et la seule qui explique les ~9 cy manquants,
puisque la file en reçoit **deux fois moins** que le crédit.

📌 **La suite est enfin étroite et mécanique.** Le chemin crédit vaut 36 (acceptation,
**documentée** : annexe B (11), 18 états) + 24 + 24 + 18 + 28 = **130** pour **111,1**
mesurés. Deux lectures cohérentes, qu'aucune mesure de débit ne peut séparer :

1. la ristourne est un artefact ⇒ les cinq termes du chemin sont ~19 cy trop chers, et
   c'est une question par instruction, tranchable **sur l'annexe B** ;
2. le silicium a lui aussi un recouvrement au `reti` ⇒ le dépouillement est le bon juge
   et il ne reste que le modèle en octets à corriger.

⇒ Commencer par (1), parce qu'elle se règle **sur la doc**, sans brûler un tir.


## 🎯🎯 2026-08-29 — L'ANNEXE B DONNE 110 cy. LE SILICIUM EN MESURE 111,1.

La doc a tranché ce que deux campagnes de réglages n'avaient pas tranché. Le chemin
complet d'une interruption TI0, **uniquement** avec les tables officielles :

| terme | table | états | cycles |
|---|---|---|---|
| acceptation | **(11) Interrupt** — `PUSH PC`, `PUSH SR`, `IFF`, `INTNEST`, **`JP (FFFF00H+vecteur)`** | 18 | 36 |
| `push (0x6FD6)` | **(1) Load** `PUSH<W> (mem)` = 6 + M, et **(10)** `(#16)` = +2 | 8 | 16 |
| `push (0x6FD4)` | idem | 8 | 16 |
| `ret` (0E) | **(9) Jump/Call/Return** | 9 | 18 |
| `reti` (07) | **(9)**, `POP SR&PC` | 12 | 24 |
| | | | **110** |

⚡ **110 contre 111,1 mesurés sur console.** Un chiffre construit sur la doc seule, à 1 %
d'une mesure silicium indépendante.

### Et notre cœur y tombe EXACTEMENT dès qu'on retire ce qu'il ajoute en trop

Mesuré par somme directe (`irq_sum_gate.py`), des centaines d'interruptions, σ = 0 :

| configuration | chemin |
|---|---|
| crédit, tel quel | 130,0 |
| crédit, sans `data_access_cycles` | 114,0 |
| crédit, **sans `data_access_cycles` ni `branch_taken_extra`** | **110,0** |
| file, sans les deux, file **vide** à l'acceptation | 126,0 |
| file, **sans les deux, file PLEINE** à l'acceptation | **110,0** |

⇒ **130 = 110 + 8 + 8 + 4**, au cycle près : deux `data_access_cycles` sur les `push` et un
`branch_taken_extra` sur le `reti`. Rien d'autre.

### 🚨 DEUX ERREURS QUI SE COMPENSAIENT — voilà pourquoi tous les balayages ont échoué

| | chemin | débit (ce que mesure le dépouillement) | silicium |
|---|---|---|---|
| crédit, tel quel | 130 | **115** | 111,1 |
| crédit, sans les deux | 110 | **94-98** | 111,1 |
| file, tel quel | 146 | **138-140** | 111,1 |
| file, sans les deux | 126 | **116-120** | 111,1 |

⚡ Le crédit « tombait juste » (115 contre 111,1) en additionnant **+20 cy de
sur-facturation** et **−17 cy de ristourne**. Retirez l'une des deux et il s'effondre :
sans la sur-facturation il tombe à 94-98, soit **−13 %**.

⚡⚡ **Et le verdict s'inverse** : une fois la sur-facturation retirée, c'est le **modèle en
octets** qui est le plus proche (116-120 contre 94-98). Les +25 % qu'on lui reprochait
depuis deux campagnes n'étaient pas les siens.

⇒ **C'est pour ça qu'aucun balayage à un bouton n'a jamais convergé** : chaque bouton
touchait une des deux erreurs, jamais les deux, et dégradait donc toujours quelque chose.

### Les trois pièces, et ce qui les justifie

1. **Un coût TABULÉ contient déjà son trafic.** `PUSH<W> (mem)` vaut 6 états + M : sa
   lecture et son écriture sont dedans. Y ajouter `data_access_cycles` les facture deux
   fois. ⚖️ **Le cœur applique DÉJÀ cette règle à l'entrée d'interruption**, avec le
   commentaire qui l'explique (« les 18 états contiennent déjà les empilements ») — elle
   est simplement appliquée de façon **incohérente**.
2. **La ristourne** : les cycles de l'ISR rechargent la file du flot interrompu alors que
   le bus cherche les octets de l'ISR. Une interruption ne peut pas rendre le code
   interrompu moins cher.
3. **La file n'est PAS vide au premier octet du gestionnaire.** Les 18 états contiennent
   le `JP (FFFF00H + vecteur)` : le bus a eu le temps de prefetcher. Armé, le modèle en
   octets tombe sur **110,0** lui aussi.

⛔ **ET ON N'ARME RIEN.** `data_access_cycles` a été **mesuré** (v15) et il a fermé la case
`MEM` du corpus (+12,1 % → −0,4 %) : le retirer globalement la rouvrirait. La règle est
donc **par classe d'instruction** — quelles lignes de notre table portent déjà leurs accès
— et c'est un chantier, pas un bouton. Même chose pour `branch_taken_extra`.

📌 **Le chantier suivant est enfin nommé** : rendre cohérente la règle « un coût tabulé
contient déjà son trafic », puis rejouer le corpus. Les trois pièces vont **ensemble** —
c'est démontré ci-dessus, chacune seule dégrade.


## 🎯 LA ROM v19 REPRENDS DU SERVICE — elle tranche, mais sur un AUTRE axe

Deux lectures sont aujourd'hui **également** compatibles avec tout ce qui a été mesuré, et
aucune moyenne de débit ne peut les séparer :

| | ce qu'elle implique |
|---|---|
| **(A) la ristourne est un artefact** | le chemin vaut les **110 cy** de l'annexe B ⇒ notre sur-facturation est réelle (`data_access_cycles` sur un `PUSH (mem)`, `branch_taken_extra` sur un `reti`) |
| **(B) le silicium a lui aussi une ristourne** | le dépouillement en débit est le bon juge ⇒ le crédit en cycles est déjà proche et c'est le modèle en octets qu'il faut corriger |

⚡ **Le contraste `nop` / charges les sépare.** La ristourne n'existe que si la boucle est
**limitée par le bus** : dans une boucle de `nop` (1 octet) la file est toujours pleine,
il n'y a rien à regagner ; dans une boucle de `ld XWA,#imm32` (5 octets) elle est toujours
vide, la ristourne est maximale. ⇒ **nos deux modèles prédisent qu'une interruption coûte
PLUS CHER dans une boucle de `nop`** — contre-intuitif, et c'est exactement la signature.

| | 1 o. (`nop`) | 2 o. | 3 o. | 5 o. | **contraste** |
|---|---|---|---|---|---|
| crédit (courant) | 133,0 | 115,0 | 114,9 | 115,0 | **+18,0** |
| file 4 octets | 149,2 | 137,6 | 138,8 | 140,3 | **+11,6** |
| **si (A)** | ~111 | ~111 | ~111 | ~111 | **≈ 0** |

⛔ **Cette fois le banc condamne** : un coût plat réfute la ristourne pour **les deux**
modèles ; un `nop` plus cher de 10-20 cy la confirme. Le montage n'a pas bougé d'un octet
— c'est l'axe de lecture qui a changé, pas la ROM.

### 🔬 Et l'hypothèse qui unifierait les deux défauts (à ne pas coder avant le tir)

Les deux erreurs se ressemblent vues de près : **une instruction qui utilise le bus pour
ses données ne peut pas prefetcher en même temps.**

- notre rechargement vaut `execution / coût_octet`, **sans regarder** ce que l'instruction
  fait du bus ⇒ un `reti`, qui dépile SR et PC, remplit quand même la file (ristourne) ;
- et symétriquement, on ajoute `data_access_cycles` **par-dessus** des états tabulés qui
  contiennent déjà ces accès ⇒ double facturation.

⇒ Modéliser l'**occupation du bus** ferait tomber les deux d'un coup, sans constante
ajoutée. ⚠️ C'est une refonte, pas un bouton : **le tir d'abord**, parce qu'en cas (B)
cette unification serait fausse par le haut.

### Comment tirer

ROM `hw_calibration/a_retq_calib_v19.ngp` — md5 **`0a9cffdf080d17947cddb814ad313e4e`**.
GAUCHE/DROITE change de page, le numéro de page est en ligne 1.

- **page 0** (`SANS IRQ`) : noter **W1 W2 W3 W5**
- **page 1** (`AVEC IRQ`) : noter **I1 I2 I3 I5**
- **page 2** : **RASV doit valoir 198**, sinon rien n'est exploitable

```
python hw_calibration/v19_gate.py --p0 W1 W2 W3 W5 --p1 I1 I2 I3 I5 --rasv 198
```


## 🎯🎯🎯 TIR SILICIUM v19 — RASV 198 — **LA RISTOURNE EST RÉFUTÉE**

| | W1 `nop` | W2 `ld A` | W3 `ld WA` | W5 `ld XWA` | I1 | I2 | I3 | I5 |
|---|---|---|---|---|---|---|---|---|
| **SILICIUM** | **1228** | **692** | **481** | **299** | **1023** | **577** | **402** | **250** |

`--p0 1228 692 481 299 --p1 1023 577 402 250 --rasv 198`
| crédit | 1232 | 692 | 481 | 299 | 989 | 574 | 399 | 248 |
| file | 1229 | 691 | 481 | 298 | 957 | 550 | 382 | 236 |

✅ **Le tir est valide, et le contrôle est excellent** : les quatre boucles **sans** IRQ
tombent sur notre modèle à **−0,3 % au pire** (1228/692/481/299 contre 1232/692/481/299).
Ce qui diffère est donc bien l'interruption, et rien d'autre.

### Coût d'une interruption, largeur par largeur

| | 1 o. (`nop`) | 2 o. | 3 o. | 5 o. | contraste |
|---|---|---|---|---|---|
| **SILICIUM** | **112,6** | **112,0** | **110,7** | **110,5** | **+1,5** |
| crédit | 133,0 | 115,0 | 114,9 | 115,0 | +18,0 |
| file 4 octets | 149,2 | 137,6 | 138,8 | 140,3 | +11,6 |

⚡⚡ **PLAT, à 111,5 cy de moyenne — c'est-à-dire les 110 cy de l'annexe B.** Nos deux
modèles prédisaient +18,0 et +11,6 de contraste ; le silicium donne **+1,5**.

⇒ **(A) est confirmé, sans ambiguïté :**

1. ⛔ **La ristourne est un ARTEFACT.** Une interruption ne regagne rien sur le flot
   interrompu — son coût est le même que la boucle soit limitée par le bus ou par
   l'exécution. Les cycles de l'ISR ne doivent PAS recharger la file du code interrompu.
2. ✅ **Notre sur-facturation est RÉELLE.** Le chemin vaut 110 cy, pas 130 : les
   `data_access_cycles` posés sur les deux `PUSH (mem)` et le `branch_taken_extra` posé
   sur le `reti` facturent ce que les états tabulés contiennent déjà.
3. ✅ Le contraste plat vaut aussi pour le **modèle en octets** : sa file ne doit pas être
   vide à l'acceptation — armée pleine, il tombe lui aussi sur **110,0**.

⚖️ **Le crédit en cycles n'a JAMAIS été juste sur l'interruption.** Ses 115 cy en débit
étaient la somme de **+20 de sur-facturation** et **−17 de ristourne** : deux erreurs de
signes opposés. C'est pour ça que deux campagnes de balayage à un bouton n'ont rien pu
conclure — chaque bouton n'en touchait qu'une.

### ⚠️ La tension qui reste, et elle est précise

Si les états tabulés contiennent déjà leurs accès, pourquoi la case `MEM` du corpus
(écriture RAM en boucle) était-elle **+12,1 %** trop rapide avant `data_access_cycles`, et
−0,4 % après ? Deux lectures, à trancher **avant** de généraliser :

- la RAM du NGPC porte des états d'attente que la table (0 wait) ne connaît pas — mais
  alors le chemin d'IRQ, qui écrit lui aussi en RAM (`push`, pile), devrait les payer, et
  le tir dit qu'il ne les paie pas ;
- ou notre table des états est trop basse pour la classe `ld (mem),R`, et
  `data_access_cycles` compensait autre chose.

📌 **Direction du chantier : certaine** (chemin à 110, ristourne supprimée, file pleine à
l'acceptation). **Règle à poser PAR CLASSE**, et cette tension est le premier point à
instruire — `MEM` du corpus contre le chemin d'IRQ, deux mesures silicium qui se
contredisent tant qu'on applique la même règle aux deux.


## ✅ 2026-08-29 (clôture) — VALIDATION INDÉPENDANTE, ET `MUL`/`DIV` EST UN AUTRE PROBLÈME

### La v18 confirme les deux corrections, sans avoir servi à les régler

Le tir v18 n'a pas été utilisé pour dériver `data_wait_cart_only` ni
`irq_transparent_queue` (elles viennent de la v19 et de l'annexe B). Rejoué après :

| page 0 | W0 | N0 | N8 | N24 | pente | **FIXE** |
|---|---|---|---|---|---|---|
| **SILICIUM** | 299 | 250 | 235 | 207 | 4,03 cy/nop | **111,1** |
| modèle livré | **299** | **250** | **236** | **207** | 4,05 | **110,2** |

⚡ **Trois cases sur quatre au compte près**, coût fixe à **−1 %**. Avant les corrections :
113,8 par compensation de deux erreurs ; maintenant 110,2 sans compensation.

⛔ Résidu conservé : la page 1 (charges dans l'ISR) donne **18,68 cy/charge** contre
**20,29** mesurés. Le code chargé d'un gestionnaire reste ~8 % trop rapide.

### 🔬 `MUL` / `DIV` mot : aucune constante unique n'est possible

Redérivées depuis le tir v17 **sous le modèle corrigé**, elles donnent **17 états / 52
cycles** (modèle livré) ou **15 / 48** (file en octets). Armées, elles **dégradent** :
`MUL` passe à **+6,1 %** au corpus. Trois autorités, trois nombres :

| source | `mul` mot | `div` mot |
|---|---|---|
| annexe B, table (4) `MUL/DIV RR,r` | 14 états | 23 états (= 46 cy) |
| ROM v17 (pente marginale, silicium) | 17 | 52 |
| corpus v2/v10 (niveau d'une boucle, silicium) | **19** | **56** |

⚡ **Et ce n'est pas une contradiction, c'est la machine** : la division du 900/L1 est à
**latence variable** — la table donne un **plancher**, ce que le dépôt notait déjà. Les
deux ROM divisent des **opérandes différentes** (`XWA = 0x01010101 ÷ DE` pour la v17,
`w ÷ (v|1)` avec de petites valeurs pour la v2). Aucun scalaire ne peut les satisfaire
toutes les deux. ⇒ **Ne pas rejouer ce cycle de réglage** : les 19/56 restent, calés sur
le corpus, et une amélioration demanderait de modéliser la latence par opérande.

### ⚖️ Et donc : la file en octets n'est plus départageable au corpus

Une fois `MUL`/`DIV` mises de côté — elles ne peuvent arbitrer aucun modèle de
recouvrement — les deux modèles sont **équivalents** :

| (22 cases, hors `MUL`/`DIV`) | moyen | pire |
|---|---|---|
| crédit en cycles (**armé**) | **0,20 %** | 0,77 % (`v10 REF`) |
| file 4 octets, pleine à l'acceptation | 0,27 % | 1,17 % (`v2 BASE`) |

Et la pente des charges de la v18 ne les sépare plus non plus : **18,68** contre **19,13**,
pour **20,29** au silicium — les deux sont courts de 6 à 8 %.

📌 **Le crédit reste armé** (il est très légèrement meilleur et c'est le livré). Mais la
question « quel modèle de recouvrement » n'est plus tranchée par ce corpus : il faudrait
un montage qui sépare les deux **sans** passer par `MUL`/`DIV`, et la pente de charges de
la v18 (8 % courte pour les deux) est le seul écart encore mesuré qui les concerne.


## 🚨 2026-08-30 — LE VIDAGE DE FILE ÉTAIT APPLIQUÉ AU MAUVAIS MOMENT

Seul écart restant après la campagne du 29/08 : une charge dans un gestionnaire coûte
**20,29 cy** au silicium (v18 page 1) et **18,68** chez nous.

### La trace le montre à l'instruction

Chemin TI0, page 1 de la v18 (gestionnaire à charges) :

```
FF22A5  push (0x6FD6)   cy=16   dette=0
FF22A9  push (0x6FD4)   cy=16   dette=0
FF22AD  ret             cy=18   dette=0
2016FE  push XWA        cy=14   dette_in = -16     <- l'avance apparait ici
2016FF  ld XWA,#imm32   cy=10   dette_in = -16     <- 10 au lieu de 20 !
201704  ld XWA,#imm32   cy=14   dette_in =  -6     <- 14 au lieu de 20
201709  pop XWA         cy=12   dette_in =   0
```

⚡ **Le `ret` du stub BIOS construit 16 cycles d'avance, et les deux premières charges du
gestionnaire en cartouche les dépensent.** Or ce `ret` ne fetche pas un octet de la
cartouche : le bus ne connaissait même pas encore l'adresse du gestionnaire. Sur 8 charges
cela fait −2 cy chacune — exactement 20,29 → 18,68.

### 🚨 Et le bouton qui devait corriger ça ne pouvait rien faire

`flush_queue_on_branch` était appliqué **avant** la comptabilité de l'instruction de
branchement : il jetait donc l'avance que la branche **avait en entrant**, jamais celle
qu'elle **crée en s'exécutant**. Or c'est la seconde qui est fausse.

⇒ Corrigé (le vidage se fait après). **Et ça réhabilite une observation qui était fausse** :
le dépôt notait « le corpus est **insensible** à ce réglage (±0,2 %) — il ne peut ni le
valider ni le réfuter ». Cette insensibilité était une **conséquence du bug**. Avec l'ordre
corrigé, le corpus bouge de **0,28 % à 2,85 %** selon `branch_flush_keep` :

| `keep` | corpus | cy/charge (ISR) | FIXE |
|---|---|---|---|
| désarmé (livré) | **0,31 %** | 18,68 | 110,2 |
| 0 | 2,85 % | 19,46 | 109,2 |
| 6 | 0,33 % | **19,46** | **111,1** |
| 14 | **0,28 %** | 18,52 | **111,1** |
| 16 | 0,31 % | 18,68 | 110,2 |

⚖️ **Pas armé, et pas par prudence : par manque d'argument.** `keep = 6` rapproche la pente
(18,68 → 19,46 pour 20,29) et pose le coût fixe exactement sur les 111,1 mesurés, mais
dégrade le corpus (0,31 → 0,33) ; `keep = 14` fait l'inverse. Les écarts sont du même ordre
que la granularité des compteurs de la ROM. ⇒ **Aucune valeur ne domine.**

📌 **Ce qui reste** : la pente des charges plafonne à **19,46** même avec un vidage total,
pour **20,29** mesurés. Les 4 % restants ne sont donc pas dans l'avance héritée du stub —
elle est entièrement jetée dans ce cas — et sont à chercher ailleurs. ⛔ Le correctif
d'ordre, lui, reste : c'est une correction de code, pas un réglage, et il est **latent**
(le bouton est désarmé, le modèle livré est inchangé — 0,31 % / 1,89 %, 2115 verts).


## ✅ 2026-08-30 (suite) — L'AVANCE NE TRAVERSE PAS UN CHANGEMENT DE RÉGION

Mesuré **directement** (moyenne des charges exécutées dans un gestionnaire, 200
interruptions) plutôt qu'à travers une pente ajustée : une charge coûte **18,00 cy** chez
nous, contre **20,29** sur console. ⚖️ Et l'estimateur de pente est bien monotone
(18,00 / 18,25 / 18,50 à `q16` = 64 / 65 / 66) — l'écart de pente que j'avais lu comme
non-monotone était du bruit d'ajustement sur des compteurs entiers, pas un banc cassé.

⛔ **Ce n'est PAS le coût de l'octet.** `q16 = 65` (4,0625 cy/octet, contre 4,03 mesurés
par la v14) dégrade le corpus de **0,31 % à 1,50 %**. Les 64 seizièmes restent — et la
structure les prédit (2 cycles de bus × 2 états × 2 = 8,00 cy/mot).

### La règle : un crédit bâti en lisant une région ne se dépense pas dans une autre

Tout le déficit est le crédit hérité à l'**entrée** du gestionnaire. Vider à **chaque**
branche le corrige mais casse le corpus (0,31 % → 2,85 %) : une branche qui reste dans sa
région garde légitimement une part de son avance (v14, `branch_flush_keep` ~13-14).
Conditionné au **changement de région** :

| | corpus | charge mesurée | pente v18 | FIXE |
|---|---|---|---|---|
| avant | 0,31 % / 1,89 % | 18,00 | 18,68 | 110,2 |
| **`flush_on_region_change`** | **0,31 % / 1,89 %** | **19,25** | **19,62** | 110,2 |
| silicium | — | ~20,2 | **20,29** | 111,1 |

⚡ **Le corpus ne bouge pas d'un centième** — la règle ne se déclenche que sur des branches
que ses boucles ne font pas — pendant que l'écart de charge se referme de plus de moitié.
Aucun paramètre libre : c'est un test de région, pas un nombre.

📌 **Ce qui reste (~1 cy/charge) est probablement légitime** : la première charge du
gestionnaire garde 6 cy d'avance laissés par le `push XWA` qui la précède — un prefetch
**séquentiel**, que le silicium fait aussi. ⇒ ne pas le poursuivre sans une mesure qui le
condamne.

**Armé** dans `ngpc_set_timing_silicon`. 2115 tests verts.


## ✅ 2026-08-30 (fin) — `vram_wait` ENFIN CHIFFRÉ : le tir existait depuis juillet

Le dernier bouton du modèle livré à **0** avec la mention « effet confirmé, coût non
épinglé — on ne livre pas un chiffre inventé ». ⚠️ **Mais le tir silicium existait** : la
ROM v3 donne **VWR 452** contre **MEM 471**, depuis 2026-07-16.

Rejouée aujourd'hui, elle se lit sans effort — et le modèle a assez progressé pour que la
dérivation soit propre :

| `vram_wait` | MEM | VWR |
|---|---|---|
| **0 (livré jusqu'ici)** | 471 ✅ | **503** (+11,3 %) |
| 4 | 471 | 478 (+5,8 %) |
| 8 | 471 | 457 (+1,1 %) |
| **9** | **471** ✅ | **452** ✅ |
| 10 | 471 | 447 (−1,1 %) |

⛔ **Et à 0 c'était pire qu'un manque** : une écriture VRAM coûtait **moins** qu'une
écriture RAM (503 > 471), parce que la VRAM est exclue de `charge_data_access`. Le
silicium dit l'inverse.

### 🚨 La garde qui a coûté un test rouge

Armé tel quel, **`test_bomberman_hicolor_phase` tombe** : le copieur HiColor dérive de sa
tranche de 4120 cycles et corrompt une ligne par bande. Deux ancrages silicium en conflit
— et le dépôt avait déjà la règle :

⇒ **`ldirw_cost = 18` a été mesuré contre CE copieur, sur matériel, avec une fenêtre d'UN
cycle. Il contient donc déjà l'étranglement.** L'ajouter par-dessus le facture deux fois.
`in_block_copy` exclut les transferts bloc, exactement comme `data_wait_before` le fait
déjà pour `data_access_cycles`.

⚠️ **Et le drapeau devait être posé APRÈS le dernier `return false`** du gestionnaire,
sinon il fuit à `true` et désarme `vram_wait` pour le reste de la partie, en silence.

**Bilan.** VWR **452** (silicium 452), MEM **471** (silicium 471), corpus **0,32 %** /
1,89 %, **2115 tests verts**.

⚖️ *La leçon n'est pas technique : un tir silicium dormait depuis six semaines derrière
« on ne livre pas un chiffre inventé ». La prudence était juste — mais il fallait revenir
le dériver quand le modèle a été prêt.*


## 🎯 ROM v20 — LE TOUR DES MESURES MANQUANTES (2026-08-30) — ⏳ ATTEND LE TIR

`a_gaps_calib_v20.ngp` · md5 **`dc2dc6cad6941573f65021e0e4725c17`** · source `cpu_calib_v20.c` ·
dépouillement `v20_gate.py`. **Quinze nombres, quatre questions, un seul tir.**

### ✅ Ce que la datasheet a réglé, et qui n'est donc PAS dans la ROM

| question | réponse | source |
|---|---|---|
| `ldir_cost = 14` est-il un écart à la doc ? | **NON, c'est la doc** — annexe B (3), `LDIR<W>` = **7n + 1 ÉTATS** = 14 cy/itération | table (3) |
| le terme constant | **+1 ÉTAT = +2 cycles** ; nous chargions **+1 cycle**. Corrigé | table (3) + §6 |
| coût d'entrée en interruption | **18 états**, `JP (vecteur)` compris — valeur **unique** | table (11) |
| adder de mode d'adressage | `(R)` = +0, `(#16)` = +2 | table (10) |
| `MUL`/`DIV` | 11/14 et 15/23 états — des **planchers**, la latence est variable | table (4) |

### ⛔ Ce que la datasheet ne peut pas régler — les quatre pages

| page | question | notre modèle |
|---|---|---|
| **0** `B1 B2 W1 W2` | `ldirb` contre `ldirw` : la doc donne **14 aux deux**, nous livrons 14 et **18** (calé sur un oracle maison, jamais sur silicium) | **14,09** / **18,28** |
| **1** `Q0 Q4 R4 R8` | un transfert bloc vide-t-il la file ? | **+0,01** ⚠️ |
| **2** `D0 D1 D2` | la division est-elle à **latence variable** ? | **étendue 0,00** |
| **3** `V8B V8W R8B R8W` | l'étranglement VRAM est-il par **accès** ou par **octet** ? | rapport **3,29** |

### ⚖️ Trois pièges de montage corrigés AVANT le tir

La première version avait **trois pages sur quatre confondues** ; les vérifier en
émulation a coûté une commande et évité un tir pour rien.

1. **p1 comparait une DIFFÉRENCE à un NIVEAU** — `(Q4−Q0)` contre `R4`, lequel contient
   aussi le `push`/`pop` du lot. Le banc rendait un écart **négatif** : une charge « moins
   chère » après un bloc. ⇒ ajout de `R8`, deux différences.
2. **p2 mesurait l'ENCODAGE, pas la division** — `0x00000007` et `0x3FFF0000` ne
   s'assemblent pas sur la même longueur. Notre modèle, dont la division est à latence
   **fixe**, rendait pourtant 75,6 / 81,8 / 83,8. ⇒ immédiats de longueur égale, étendue
   **0,00**.
3. **p3 mesurait l'instruction, pas l'étranglement** — une écriture mot et une écriture
   octet n'ont ni le même encodage ni le même nombre d'états. ⇒ **double différence**
   contre les mêmes écritures en RAM.

⚠️ **Et la page 1 est une page de RÉFUTATION, pas de confirmation.** Notre modèle prédit
**+0,01 cy** : une charge de 5 octets est limitée par le **bus** et paie son fetch que la
file soit pleine ou vide, donc le drapeau n'a presque pas d'empreinte sur ce motif. La page
ne peut pas confirmer `block_drains_queue` — mais un écart net au silicium nous
**réfuterait**, et c'est déjà une réponse. (Blocs raccourcis à 2 itérations : le vidage vaut
au plus un remplissage de file, il se noyait dans 900 cy de copie.)

### 🚨 Le piège de navigation, à retenir pour toutes les ROM

La ROM ne lit la manette **qu'une fois par cycle de mesure** (~240 trames). Un appui
alterné 20 trames / 20 trames tombe donc toujours à la **même phase** — et si cette phase
est dans la moitié relâchée, **la page n'avance jamais**. Ce n'est pas un manque de
patience : rallonger la boucle n'y change rien. ⇒ appui **continu**, et on regarde à chaque
trame.

### Comment tirer

GAUCHE/DROITE change de page, le numéro est en ligne 1.
Pages **0** à **3** : noter les quatre (ou trois) nombres. Page **4** : **RASV = 198**.

```
python hw_calibration/v20_gate.py --p0 B1 B2 W1 W2 --p1 Q0 Q4 R4 R8     --p2 D0 D1 D2 --p3 V8B V8W R8B R8W --rasv 198
```


## 🎯🎯 TIR SILICIUM v20 — RASV 198 — TROIS RÉPONSES NETTES

`--p0 392 204 393 204 --p1 1117 576 1144 583 --p2 4414 4413 4413 --p3 1948 1948 2213 2213`
(md5 `dc2dc6cad6941573f65021e0e4725c17`). ⚠️ Toutes les valeurs bougent de ±1 d'un tir à l'autre, comme d'habitude.

| page | silicium | notre modèle avant | verdict |
|---|---|---|---|
| **0** `ldirb` / `ldirw` | **14,12** / **14,16** | 14,09 / **18,28** | ⛔ les deux formes coûtent **pareil**, et c'est l'annexe B (7n+1 états) |
| **1** charge après un bloc | −0,01 | +0,01 | ✅ aucune réfutation |
| **2** division, 3 opérandes | **87,07 / 87,09 / 87,09** | 83,82 (×3) | ⛔ **latence FIXE** (étendue 0,02) et nous sommes 3,9 % trop rapides |
| **3** VRAM octet / mot | **2,95 / 2,95** — rapport **1,00** | 2,23 / 7,33 — rapport 3,29 | ⛔ **par ACCÈS**, pas par octet |

### ✅ p3 — l'étranglement VRAM est par ACCÈS, et la v3 le confirme

Rapport **1,00** exact : une écriture mot coûte autant qu'une écriture octet. Même
réfutation que `data_wait_q16` par la v15.

⚖️ **La v3 ne pouvait pas trancher** : elle n'écrit que des octets, où les deux formes
coïncident — d'où mon `vram_wait = 9` par octet, qui « collait » à elle. C'est la **double
différence** de la v20 (les mêmes écritures refaites en RAM) qui sépare l'étranglement du
coût propre de l'instruction.

⚡ Et les deux tirs **concordent sur la valeur** : throttle mesuré **2,74** (v3) et
**2,95** (v20). ⇒ `vram_wait = 10`, **par accès**, équilibre les deux (v3 −0,9 %, v20
+1,3 %) là où 9 collait à la v3 mais laissait la v20 à +3,9 %.

### ✅ p2 — la division est à latence FIXE, et `div` mot passe à 58

Trois divisions aux opérandes très différentes coûtent **87,07 / 87,09 / 87,09** :
**étendue 0,02 cy**. ⛔ L'hypothèse « latence variable », posée pour expliquer que trois
autorités donnent trois nombres, est **RÉFUTÉE**. Une constante unique existe — il fallait
la mesurer proprement.

| `div_word_cycles` | corpus | pire cas | `DIV` (v2 / v10) | v20 p2 |
|---|---|---|---|---|
| **56** (livré) | 0,31 % | 1,89 % | +1,9 / +1,5 | +3,9 % |
| **58** ✅ | **0,18 %** | **0,77 %** | **+0,0 / −0,4** | +1,4 % |
| 59 | 0,25 % | 1,13 % | −0,8 / −1,1 | +0,2 % |

⇒ **58 armé.** Pour la première fois **toutes les cases du corpus sont sous 1 %**.
📌 Reste à expliquer pourquoi la v17 (pente marginale) donnait 52.

### ⛔ p0 — `ldirw` : conflit RAM/VRAM RÉEL, et l'explication évidente est réfutée

Un `ldirw` **RAM→RAM** coûte **14,16 cy/itération** sur console, exactement comme un
`ldirb` (14,12) et exactement l'annexe B. Notre `ldirw_cost = 18` est donc faux **de 29 %**
pour ce cas. Mais le copieur HiColor de Bomberman — qui écrit en **VRAM** — exige 18 : à 14
il tourne **21 % trop vite** (6476 cycles pour deux blocs contre 8240 mesurés).

⛔ **Hypothèse testée et RÉFUTÉE : « 18 = 14 + l'étranglement VRAM ».** Armé
(`block_pays_vram`), le throttle ne change **rien** au copieur — 6476 dans les deux cas.
Cause trouvée : il passe par `access_wait`, donc il est **intégralement absorbé** par le
recouvrement, le coût de base d'un bloc étant énorme. Et même non absorbé il vaut 2,9,
pas 4.

⇒ **La différence RAM/VRAM sur un transfert bloc est réelle et pas encore expliquée.** On
garde 18 : il tient un ancrage jouable, et 14 casse une image. C'est la ligne ouverte que
laisse ce tir.


## 🎯 ROM v21 — UN TRANSFERT BLOC, QUATRE CHEMINS (2026-08-30) — ⏳ ATTEND LE TIR

`a_blocpath_calib_v21.ngp` · md5 **`fa103327801b0308e5877e4342cde8c3`** ·
source `cpu_calib_v21.c` · dépouillement `v21_gate.py`. **Huit nombres.**

### La question, et pourquoi elle est ouverte

La v20 mesure `ldirw` **RAM → RAM** à **14,16** cy/itération — l'annexe B au centième. Mais
le copieur HiColor de Bomberman, qui copie **ROM → VRAM**, exige **~17,9** : à 14 il tourne
21 % trop vite et l'image se déchire.

⚡ **Ces deux montages diffèrent par DEUX choses à la fois** — la région **source** et la
région **destination** — et personne ne les a jamais séparées. Tant que ce n'est pas fait,
tout ce qu'on peut poser est un nombre qui arrange un jeu, ce qu'est `ldirw_cost = 18`
aujourd'hui.

⛔ Et l'explication évidente est **déjà réfutée** : « 18 = 14 + l'étranglement VRAM » ne
tient pas — le throttle vaut 2,95 cy/accès (v20 p3), il en faudrait 4, et armé sur les
transferts bloc il ne change **rien** au copieur (il passe par le recouvrement et s'y fait
absorber, le coût de base d'un bloc étant énorme).

### Le montage : le même `ldirw`, seules les régions changent

| rotation | chemin | ce qu'elle isole |
|---|---|---|
| `RR1`/`RR2` | RAM → RAM | le **témoin** (la v20 dit 14,16) |
| `RV1`/`RV2` | RAM → VRAM | la **destination**, seule |
| `OR1`/`OR2` | ROM → RAM | la **source**, seule |
| `OV1`/`OV2` | ROM → VRAM | le chemin de **Bomberman** |

Chaque paire est 64 puis 128 itérations : la différence donne le coût par itération sans
aucune hypothèse sur le reste.

⚡ **Et la question qui décide du modèle : `(OV − RR)` vaut-il `(RV − RR) + (OR − RR)` ?**
Si oui les deux effets sont indépendants et il suffit de les additionner — deux nombres, pas
quatre. Sinon il existe un **troisième terme**, et on le saura au lieu de le deviner.

⚠️ **Notre modèle prédit le même coût aux quatre chemins** (18,2 partout) : il ne connaît
ni la source ni la destination d'un transfert bloc. La ROM ne peut donc pas nous confirmer
— seulement nous corriger, et dire de combien. ✅ Et `RR` sert de **contrôle croisé** : il
doit retomber sur les 14,16 de la v20.

### Comment tirer

GAUCHE/DROITE change de page (tenir la touche — la ROM ne lit la manette qu'une fois par
cycle de mesure). Page **0** : `RR1 RR2 RV1 RV2`. Page **1** : `OR1 OR2 OV1 OV2`.
Page **2** : **RASV = 198**.

```
python hw_calibration/v21_gate.py --p0 RR1 RR2 RV1 RV2 --p1 OR1 OR2 OV1 OV2 --rasv 198
```


## 🎯🎯🎯 TIR SILICIUM v21 — RASV 198 — **CE N'ÉTAIT PAS LA VRAM, C'ÉTAIT LA CARTOUCHE**

`--p0 390 204 392 204 --p1 310 160 310 160 --rasv 198`

| chemin | silicium | écart au témoin | notre modèle avant |
|---|---|---|---|
| RAM → RAM | **14,04** | — (annexe B : **14,00**) | 18,22 ⛔ |
| RAM → VRAM | **14,12** | **+0,08** — la destination ne coûte **RIEN** | 18,05 |
| ROM → RAM | **18,16** | **+4,12** — la **source** coûte tout | 18,22 |
| ROM → VRAM | **18,16** | +4,12 | 17,99 |

⚡ **Additivité parfaite** : les deux effets sommés font **+4,20**, le chemin complet
**+4,12**. Les surcoûts de source et de destination sont **indépendants**.

### Ce que ça règle

⚖️ **`ldirw_cost = 18` n'était pas faux — il était MAL ATTRIBUÉ.** Calé sur le copieur
HiColor de Bomberman, qui copie **ROM → VRAM**, il portait **14** (l'instruction, exactement
l'annexe B) **+ 4** (le prix de lire sa source sur le bus **8 bits** de la cartouche : un
mot y coûte deux accès d'octet). Nous l'appliquions à **tous** les transferts ⇒ **29 % trop
cher** sur toute copie RAM → RAM ou RAM → VRAM, c'est-à-dire la plupart de celles que font
les jeux.

⛔ Et ça enterre l'hypothèse que j'avais poursuivie la veille : la **destination VRAM ne
coûte rien** du tout sur un transfert bloc (+0,08). L'étranglement du K2GE, bien réel sur
une écriture isolée (2,95 cy/accès), ne mord pas ici.

⇒ `ldirw_cost` **18 → 14**, plus `block_cart_src_per_byte = 2` (soit +4 par itération mot).

| | avant | après |
|---|---|---|
| v20 p0 `ldirb` / `ldirw` RAM→RAM | 14,09 / **18,28** | **14,09 / 14,05** (silicium 14,12 / 14,16) |
| v21 les quatre chemins | 18,2 partout | **14,23 / 14,05 / 18,22 / 18,05** |
| Bomberman HiColor | ✅ | ✅ |
| corpus | 0,18 % / 0,77 % | **0,18 % / 0,77 %** |
| suite | 2115 verts | **2115 verts** |

⛔ **Forme OCTET : dérivée, pas mesurée.** Les 2 cy/octet viennent de la mesure du **mot**
(4 ÷ 2 octets). Une rotation `ldirb` à source cartouche le confirmerait. C'est noté au point
d'usage.
