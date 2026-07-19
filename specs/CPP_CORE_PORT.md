# C++ Core — Chantier Plan & Semantic Contract

> Statut : **ouvert 2026-07-11**. Périmètre arrêté :
> **cœur natif headless + harnais de preuve**. Pas de boucle temps réel, pas
> d'UI, pas d'audio dans ce chantier — ce sont des chantiers suivants qui
> s'appuieront dessus.

Ce document est la référence unique du portage. Il fixe **ce qu'on porte**,
**ce qui change de sens**, et **comment on prouve qu'on n'a rien cassé**.

---

## 1. Le constat qui commande tout le reste

| Grandeur | Valeur | Source |
|---|---|---|
| Horloge CPU NGPC | 6 144 000 Hz | `specs/FRAME_TIMING.md` |
| Débit nécessaire (≈10 cycles/instr) | **~615 000 instr/s** | calcul |
| Débit mesuré du cœur Python | **1 706 instr/s** | `PERF_TIMING_POLICY.md` §10 |
| Facteur manquant | **×360** | |
| Débit constaté d'un interpréteur TLCS-900 natif | ~45 000 000 instr/s | mesure 2026-07-11 |
| Débit d'une boucle C++ triviale (mesuré ici, MinGW -O2) | 2 181 M iter/s | probe 2026-07-11 |

**Conclusion : la vitesse n'est pas le risque.** Un interpréteur C++ à dispatch
par table dépasse la cible d'un ordre de grandeur ou deux, sans aucune astuce
(pas de JIT, pas de threads, pas de recompilation dynamique).

**Le risque, c'est la fidélité** : 18 846 lignes d'exécuteur, ~102 corps de
dispatch, ~135 paires (octet de tête × plage de sous-op) — chacun une occasion
de régresser en silence, sur un modèle qui est aujourd'hui à **0 divergence**
contre une trace de référence sur 6 ROMs commerciales.

> ### Doctrine du chantier
> **On construit le tribunal avant l'accusé.**
> Le harnais de preuve (phase 0) est écrit et vérifié *avant* la première ligne
> de CPU. Tout l'effort d'ingénierie va à la preuve d'équivalence, aucun à
> l'optimisation — elle est acquise d'avance.

---

## 2. Contrat sémantique : ce qui change, ce qui ne change pas

Le cœur Python est **tri-state** : chaque registre est `int | None`, où `None`
= « valeur inconnue ». ~215 retours `requires-known-*` existent uniquement pour
servir ce modèle. C'est un héritage de l'époque où l'on exécutait sans BIOS
depuis un état partiel : **une fonctionnalité d'analyse statique, pas
d'émulation**.

**Décision (2026-07-11) : le cœur C++ est CONCRET.** Registres =
`uint32_t` toujours définis, depuis l'état de reset documenté / hand-off BIOS.
Le cœur Python **garde** son tri-state et devient le modèle d'analyse + l'oracle
de référence.

### 2.1 Statuts qui DISPARAISSENT (artefacts du tri-state)

Sans valeur inconnue, ces arrêts n'ont plus d'objet :

`requires-known-full-register` (137 sites) · `runtime-memory-unavailable` (79) ·
`requires-known-address-register` (40) · `runtime-state-required` (30) ·
`requires-known-source-register` (13) · `requires-known-stack-pointer` (9) ·
`requires-known-flags` (8) · `stack-data-unavailable` (7) ·
`bios-call-requires-known-register` (5) · `requires-known-sr` ·
`requires-known-control-register`

`runtime-memory-unavailable` disparaît aussi : la mémoire plate est initialisée
aux **valeurs de power-on documentées** (page I/O TMP95C061, registres K2GE, RAM
froide) — tout octet mappé a une valeur.

### 2.1-bis ⚠️ CORRECTION (2026-07-11) — le tri-state encode AUSSI l'INDÉFINI SILICIUM

Le §2 ci-dessus disait « le tri-state est une feature d'analyse ». **C'est
incomplet, et le harnais l'a prouvé dès la 2ᵉ famille portée.**

`None` a **deux** sens dans le cœur Python :

| Sens | Exemple | Sort en C++ |
|---|---|---|
| « valeur pas encore connue » (analyse, bootstrap sans BIOS) | registre non seedé | **disparaît** (état concret) |
| **« le HARDWARE ne définit pas ce résultat »** | flag H après `CCF`/`ZCF` | **subsiste comme fait** |

Toshiba donne à `CCF` la ligne de symboles `- - × - 0 *`, où `×` signifie
littéralement *« an undefined value is set »*. Le `hf=None` de Python **est la
bonne réponse**.

**Conséquence sur le harnais :** quand la référence rend `None` sur un flag, elle
**ne formule aucune prétention** — aucune valeur concrète produite par le C++ ne
peut être « fausse », et comparer déclencherait une fausse alerte. `compare()`
saute donc ces flags. **Ce n'est pas une échappatoire** : la graine étant
totalement concrète, un flag ne peut revenir `None` que si l'exécuteur l'a
*délibérément* déclaré indéfini.

### 2.2 Statuts qui SURVIVENT — non négociable

Ce sont des vérités hardware ou des trous de couverture. La politique
`HARDWARE_COMPAT_POLICY.md` §9 interdit de les masquer par un fallback
silencieux :

| Statut | Pourquoi il reste |
|---|---|
| `silicon-broken`, `silicon-undefined` | Le hardware casse. L'émulateur de référence casse. (`quirks_db.json` + matcher portés dans le cœur.) |
| `division-by-zero` | Comportement CPU réel. |
| `bios-shutdown` | Le BIOS éteint la console (batterie basse). C'est ce stop honnête qui a diagnostiqué la passe 183. |
| `bios-font-unavailable`, `bios-call-out-of-range` | Vérités du HLE BIOS. |
| `unknown-opcode`, `truncated`, `unmapped` | Trous de décodage / bus. |
| `unsupported-decoded-instruction`, `unmodeled-*`, `not-yet-modeled` | **Trous de couverture → TRAP.** Jamais un NOP silencieux. |
| `cpu-halted`, `executed` | Terminaux normaux. |

> **Règle :** un opcode non implémenté dans le cœur C++ **arrête la machine avec
> son octet et son PC**, exactement comme aujourd'hui. C'est ce qui rend le
> portage progressif sûr : ce qui n'est pas encore porté est *bruyant*, pas faux.

---

## 3. Architecture

```
NgpCraft_emulator/
  cpp/
    CMakeLists.txt
    include/ngpc_core.h        <- ABI C PLATE (le seul contrat exporté)
    src/
      memory.cpp     bus + mémoire plate 16 MB + valeurs power-on
      decode.cpp     24 décodeurs de famille -> opérandes TYPÉS + longueur + CYCLES
      execute.cpp    dispatch par TABLE (256 + sous-tables) -> ~102 corps
      irq.cpp        contrôleur multi-source + table de vecteurs HW
      timers.cpp  adc.cpp  flash.cpp  quirks.cpp  bios_hle.cpp
      k2ge.cpp       registres vidéo + pacing raster (RAS.V / BLNK) DANS le cœur
    tools/
      ngpc_trace.cpp  <- dumper de trace JSONL headless (le témoin au procès)
  core/native.py     <- binding ctypes (zéro dépendance, zéro couplage ABI CPython)
  tests/cpp/         <- harnais de preuve
```

### 3.1 Pourquoi ABI C plate + ctypes, et pas pybind11

**Mesuré, pas supposé (2026-07-11).** Python 3.11.9 ici est compilé **MSVC** ;
le seul compilateur disponible est **GCC 13.1 MinGW**. pybind11 ferait traverser
la frontière à l'ABI C++ *et* à l'API CPython entre deux compilateurs
différents — fragile. Une DLL à **ABI C plate** ne traverse ni l'un ni l'autre.

Vérifié sur cette machine : une DLL C++17 MinGW (avec `std::vector`, `new`/
`delete` internes) se charge et s'exécute sans accroc depuis CPython MSVC via
ctypes.

**Coût d'une traversée FFI : 292 ns.** À 60 appels/s (un par frame) c'est
négligeable ; à 615 000 appels/s (un par instruction) ce serait ~17 % de
surcoût. **⇒ La couture est par batch/frame. Jamais par instruction.**
Corollaire : les breakpoints doivent vivre **dans le cœur**
(`ngpc_set_breakpoints` + stop reason), pas dans une boucle Python à
`batch_size=1` comme aujourd'hui.

### 3.2 Ce qui est porté, ce qui reste en Python

| Porté en C++ | Reste en Python |
|---|---|
| `execute.py` (18 846 L), `decode.py`, `cpu.py`, `bus.py`, `memory.py` | `symbols.py`, `watchpoints.py`, `breakpoints.py` (registre + UI) |
| `run_steps.py` (boucle) + **pacing cycles→scanlines→VBlank** | `savestate.py` (sérialisation JSON, hash ROM) |
| `timers.py`, `adc.py`, `flash.py`, `quirks.py` | `event_log.py`, `frame_goldens.py`, `goldens.py` |
| HLE BIOS `swi 1` (~700 L extraites d'`execute.py` → module à part) | `renderer.py`, `k2ge.py` (lecteurs) — phase 4 décidera |
| Registres K2GE + raster (RAS.V/BLNK) | CLI `ngpc_emu.py`, UI PyQt6, `engine_bridge.py` |

---

## 4. Les 9 pièges de la couture (relevés à l'audit, à traiter explicitement)

1. **`after_memory` / `final_memory` = copie du dict complet à CHAQUE
   instruction** (`execute.py:197`, `run_steps.py:112`, puis `dict(...)` encore
   dans `emulator_session.py:599`). Un cœur natif ne peut pas rendre un dict
   Python par pas. → **deltas seuls** (`memory_writes`) + `ngpc_read_mem`.
2. **Le shell injecte lui-même les registres raster K2GE** dans la mémoire du
   cœur (`emulator_session.py:305-309` poke `0x008009` = scanline, `0x008010` =
   BLNK). → **le cœur natif possède RAS.V/BLNK.**
3. **`dict(bus.builtin_bytes)`** reconstruit toute l'image mémoire pour le rendu
   et chaque inspecteur (6 sites). → bloc-read ou `render_frame` côté cœur.
4. **Le pacing frame/IRQ vit dans le shell** (`emulator_session.py:582-601`). Si
   le cœur avance le raster, le Python double-compte. → **trancher une fois : le
   pacing appartient au cœur.**
5. **Les périphériques sont des objets du shell passés DANS l'appel**
   (`flash=`, `adc=`, `timers=`) et mutés par effet de bord. → ils entrent dans
   la machine ⇒ le schéma de savestate doit grandir (v6) pour les garder.
6. **`status` honest-stop est partout** (CLI, tests, contrôle de flux de
   `EmulatorSession.step`). Le §2 ci-dessus est le contrat qui tranche.
7. **Breakpoints = hack `batch_size=1`** → dans le cœur (cf. §3.1).
8. **`build_event_log_payload` a sa PROPRE boucle d'exécution**
   (`event_log.py:66-98`, appelle `build_execute_next` en direct, sans
   flash/adc/timers). → re-pointer sur l'entrée unique, sinon elle divergera.
9. **Convention de chaîne porteuse** : `MemoryWrite.note` préfixé
   `"[DISCARDED]"` est ce qui **pilote le modèle flash**
   (`run_steps.py:66-92`). Le cœur doit soit reproduire le préfixe, soit
   absorber le flash entièrement (→ l'absorber).

---

## 4-bis. ⚖️ HIÉRARCHIE DES AUTORITÉS (établie empiriquement 2026-07-11)

Arrêtée pour le projet : **le cœur C++ doit être le plus COMPLET possible**, pas
le clone des trous du cœur Python. La règle « jamais plus capable que la
référence » devient donc : **« jamais plus capable sans qu'une autorité
indépendante confirme que l'encodage existe »**.

| Rang | Autorité | Portée | Vérifié |
|---|---|---|---|
| **1** | **`asm900.exe`** — assembleur **officiel Toshiba** (`oracle_tools/asm900_oracle.py`) | **ENCODAGES** : s'il émet les octets, l'encodage existe. Point. | ✅ |
| 2 | Datasheet Toshiba TLCS-900/L1 | sémantique, cycles, flags | ⚠️ tables = images ; **la prose s'extrait mal → croire les LIGNES DE SYMBOLES** (§0.2) |
| 3 | Notre cœur Python | juste là où il agit (HW-vérifié) — mais **768 TROUS** en famille mémoire (dont 616 en long) | ✅ |
| 4 | `ngdis` (NgpCraft_Disasm) | bon décodeur — **mais PÉRIMÉ sur `D0..D7`** | ❌ |
| 5 | Chiffres d'émulation qui circulent (forums, folklore) | ouï-dire ; **cycles bricolés** | ❌ déjà perdu sur `JR` |

### Les deux arbitrages rendus par `asm900`

**(a) Les 768 refus du cœur Python sont de VRAIS TROUS.** asm900 émet, 6/6, les
encodages contestés : `ld XWA,(XWA)` = `A0 20` · `add XWA,(XWA)` = `A0 80` ·
`cp XWA,(0x5000)` = `E1 00 50 F0` · `ex (0x50),W` = `C0 50 30`. ngdis avait
raison, la référence est incomplète. **⇒ le C++ les implémente.**

**(b) Les 72 encodages « Python exécute / ngdis rejette » : c'est NGDIS qui a
tort.** Ils sont *tous* la famille `D0..D7`, que ngdis appelle encore
« BROKEN D0 word-reg ALU prefix — silicon bug » — **un mis-diagnostic que le
projet a RÉTRACTÉ après test sur vraie NGPC le 2026-07-03**. asm900 tranche :
`ldw WA,(0x50)` = `D0 50 20` · `ldw WA,(0x5000)` = `D1 00 50 20` ·
`cpw (0x50),WA` = `D0 50 F8`. C'est une famille d'adressage **mémoire word**
parfaitement ordinaire. **Aucun comportement inventé dans la référence.**
→ Dette à reporter au projet `NgpCraft_Disasm` : **ngdis doit être mis à jour.**

### La carte d'encodage (premier octet ≥ 0x80)

```
zz  = (b & 0x30) >> 4                  0=octet  1=mot  2=long  3=groupe DESTINATION
mem = ((b & 0x40) >> 2) | (b & 0x0F)   0..21 = modes mémoire   >=23 = registre direct
```
puis l'octet suivant (après les octets d'opérande du mode) est la **sous-op**.

| mode | forme | octets |
|---|---|---|
| 0..7 | `(R32)` | 1 |
| 8..15 | `(R32+d8)` | 2 |
| 16 / 17 / 18 | `(abs8)` / `(abs16)` / `(abs24)` | 2 / 3 / 4 |
| 19 | octet secondaire : `(r32)`, `(r32+d16)`, `(r32+R8/R16)` | 2 / 4 / 4 |
| 20 / 21 | `(-R32)` pré-déc / `(R32+)` post-inc | 2 |

⚠️ **`0x30..0x37` = `EX (mem),R`**, PAS `ld (mem),R`. Les **stores** vivent dans
le groupe `zz==3` (`0xB0..0xBF` / `0xF0..0xFF`), avec `ret cc` et les `jp`/`call`
indirects. `0xF8..0xFF` = `CP` inversé (`cpw (0x50),WA` = `D0 50 F8`).

## 4-ter. 🎯 CE QUI BLOQUE LES JEUX — mesuré au (octet, sous-op), 2026-07-12

Après les familles mémoire + registre-direct : **129 576 instructions** exécutées
nativement sur les 66 ROMs commerciales (*Dive Alert* : 43 049). Le blocage n'est
plus l'octet de tête mais la **sous-op** — voici la vraie liste de travail :

| Encodage | Instruction | ROMs bloquées | Ce qu'il faut |
|---|---|---|---|
| **`D8 2E`** | **`ldc CR, WA`** | **19** | **fichier de registres de contrôle** (DMA…) — absent du modèle C++ |
| `C7 31` | forme registre **ÉTENDUE** | 12 | **ni ngdis ni Python ne la décodent** → `asm900_oracle` obligatoire |
| `83 11` | **`ldir`** (instruction bloc) | 10 | recherche FAITE (voir `mem_family.cpp`), reste à écrire |
| `17 00` | `ldf` | 10 | **modèle de fenêtre bancaire** (`banks[4][8]`) — ne pas improviser |
| `F1 86` | `andcf` (ops bit de carry) | 4 | groupe destination `0x28..0x2C` / `0x80..0xA7` |
| `E7 3C`, `D7 FA` | escapes étendus | 2+2 | idem `C7` |

⚠️ **`C7`/`D7`/`E7` : le désassembleur est AVEUGLE sur ces formes** (`db 0xC7…`).
C'est exactement pourquoi `rel_probe`/`asm900_oracle` existe : **l'assembleur
officiel est le seul moyen de lire ces encodages.**

## 5. Le harnais de preuve — 5 portes

Aucune ligne de CPU n'est écrite avant que ces portes existent et tournent.

### G1 — Corpus de conformance (extrait du Python, gelé en données)
Les **355 tests d'exécution** (`test_execute.py`) et **137 de décodage**
(`test_decode.py`) sont déjà en forme *« octets d'opcode → état attendu »*.
On les instrumente pour dumper `(état graine, octets) → (état résultant)` en
JSON. **Ça gèle la spec Python en données rejouables** contre le C++, sans
réécrire 492 tests à la main.

### G2 — Fuzz différentiel d'opcodes  ← la porte qui rend « sans erreur » crédible
État CPU aléatoire × encodages (aléatoires **et** exhaustifs par famille) → on
exécute **une** instruction dans les deux cœurs → on compare l'état résultant
complet (8 registres, 6 flags, IFF/RFP, écritures mémoire, cycles).
C'est ce qui couvre les opcodes qu'aucune ROM du corpus n'exerce jamais.

### G3 — Équivalence de trace Python ↔ C++ sur le corpus ROM
Les deux modèles sont **les nôtres** : toute divergence est un **bug**, sans
l'ambiguïté « triage, pas verdict » qu'impose toute référence extérieure.
Outils : `oracle_tools/native_diff.py` (py↔cpp, pas à pas) et
`oracle_tools/trace_equiv.py` (ROM entière). `trace_diff.py` se réutilise tel
quel si le C++ émet le même JSONL que `dump_our_trace.py` — il suffit d'un
dumper côté C++ homologue de celui-ci.

### G4 — Cliquet de baseline (résultats gelés en données)
Le C++ doit conserver **0 divergence là où le Python en a 0** (Big Bang, Cotton,
Crush Roller, Neo Turf, Pac-Man, Magical Drop / 3000 pas). Les 2 divergences
connues (Metal Slug, Puzzle Bobble — écart HLE `swi 1`) restent tolérées **à
l'identique**, pas une de plus.

### G5 — Non-régression Python
Les **1314 tests** restent verts (baseline 2026-07-11, 14,7 s). Le cœur Python
n'est pas touché par ce chantier ; s'il rougit, c'est qu'on a débordé.

### Trous connus du harnais (à combler, honnêtement)
- `trace_diff.py` ne compare **que PC + 8 registres + F**. Ni cycles, ni
  mémoire, ni IFF/RFP. → étendre pour G3 (entre nos deux cœurs, on peut tout
  comparer).
- Les « micro-ROM goldens » sont des **allers-retours save→check**, pas des
  traces gelées : ils **ne détecteront pas** une régression C++ tant qu'on n'a
  pas figé la sortie Python actuelle en vrais fichiers goldens.
- Une trace de référence extérieure rapporte les registres **de la banque
  courante** (mapping indexé par `rfp & 3`) : divergences fantômes si
  `rfp != 0` et que le C++ banque autrement.
- Les traces de référence gelées dont on dispose ont été prises **en HLE BIOS,
  sans BIOS attaché** : le boot BIOS réel n'est diffable contre aucune d'elles.
  G3 (py↔cpp) est la seule porte qui le couvre.

---

## 6. Phasage

| Phase | Contenu | Porte de sortie |
|---|---|---|
| **0** | Squelette `cpp/` + CMake + ABI C + binding ctypes + **les 5 portes**, contre un cœur vide | Le harnais **détecte** un cœur vide (il doit échouer bruyamment) ; baselines gelées |
| **1** | Mémoire plate + power-on + bus ; décodeur (24 familles) → opérandes typés + longueur + **cycles émis par le décodeur** | Fuzz de décodage exhaustif vs `decode.py` : longueur, mnémonique, opérandes, control-flow |
| **2** | Exécuteur : dispatch par table + ~102 corps, famille par famille dans l'ordre du census | Après **chaque** famille : G2 (fuzz de la famille) + G1 (sous-corpus) verts |
| **3** | IRQ multi-source + table de vecteurs HW, timers, ADC, flash, quirks, HLE BIOS, pacing raster | G3 sur corpus homebrew + G4 (cliquet de baseline) |
| **4** | Vidéo K2GE si le rendu passe dans le cœur (sinon reste Python) | Frame goldens byte-exacts |
| **5** | Vitesse + intégration headless CLI/CI (les 9 pièges §4) | ≥ 615 000 instr/s **avec** G1–G5 verts |

**⚠️ La phase 1 (« décodeur séparé ») est ANNULÉE.** Le cœur Python sépare
décodage et exécution parce qu'il a commencé comme outil d'analyse statique. Un
interpréteur n'en a pas besoin : chaque handler décode ses opérandes en ligne.
Aucune couverture perdue — une longueur d'instruction fausse se manifeste comme
une divergence de PC, que G2 attrape déjà. Le désassemblage reste en Python
(`decode.py` n'est pas dans le chemin chaud). Phases 1 et 2 fusionnées.

**Ordre de portage — MESURÉ, pas supposé (2026-07-11).** Deux recensements :

*Dynamique* (2,4 M instructions réellement exécutées par 8 jeux, tracées via le
co-simulateur) : les **branchements conditionnels `0x6x` pèsent ~34 %** ; 10
octets de tête couvrent 86 % du trafic (`C2` 19 % · `66/65/6E/68` · `D1` 10 % ·
`C1` 6 % · `F2` 6 % · `D9` · `C9` · `F5`).

*Bloquant* (ce qui **arrête** les 66 ROMs commerciales dans le cœur natif) — la
vraie liste de travail :

| Opcode | ROMs bloquées | Famille |
|---|---|---|
| **`0xC1`** | **32 / 66** | mémoire abs16 octet |
| `0x17` | 9 | `ldf` (commute la banque de registres) |
| `0x08` | 6 | store I/O CPU |
| `0xF2` | 4 | famille b0_memory |
| `0xEF` / `0xC9` / `0xC7` | 3 chacun | préfixés registre / registre étendu |

⇒ **Prochaine cible : les familles d'adressage mémoire** (`0xC0..0xC7`,
`0xD0..0xD7`, `0xE0..0xE7`, `0xF0..0xF7`, `0x80..0xBF`), qui sont aussi le gros
des 18 846 lignes. Puis `ldf`/`incf`/`decf`/`pop SR` — qui exigent d'abord le
**modèle de fenêtre bancaire** (`banks[4][8]`) des deux côtés ; ne pas les
improviser.

---

## 7. Ce que ce chantier ne fait PAS

- pas de boucle temps réel, pas de rendu à 60 fps, pas d'input
- pas d'audio (l'APU T6W28 existe en modèle isolé **non branché** ; il n'y a
  **aucun cœur Z80**, le CPU son — c'est un chantier entier à lui seul, et il
  devra rester clean-room comme l'APU)
- pas de réécriture de l'UI PyQt6 ni du CLI (8 512 lignes) — ils continuent de
  tourner sur le cœur Python tant que la phase 5 n'a pas basculé la couture
