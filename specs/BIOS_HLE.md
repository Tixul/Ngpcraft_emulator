# BIOS HLE Strategy

Spec d'implémentation **High-Level Emulation** du BIOS NGPC dans
l'émulateur, sans jamais distribuer ni dépendre du dump propriétaire
SNK.

Document écrit 2026-05-20. Référence master :
`../../Doc de dev/Final/BIOS_FLASH_SAVES_STRATEGY.md` (point d'entrée
unique cross-projets).

---

## 1. Principe

Le BIOS NGPC est mappé `0xFF0000..0xFFFFFF` (64 KB) dans
`core/bus.py` (région `BIOS_ROM`, kind `bios`). Cette région est
actuellement `unbacked` : lire les bytes retourne le status
`unbacked` et l'executor stoppe honnêtement si une instruction
prétend lire le BIOS.

**Pour l'exécution des BIOS calls**, l'émulateur intercepte les
opcodes `SWI n` (encoding `0xF8 + n`) **avant** que le CPU ne saute
vers le vecteur BIOS. Le handler Python applique le side-effect
attendu, écrit les valeurs de retour dans les registres bank-3
appropriés (RA3, XBC3, …), et avance PC à l'instruction qui suit le
SWI — exactement comme si le BIOS avait exécuté + RETI.

C'est de **l'HLE pur** : aucun byte du BIOS n'est référencé.

L'alternative LLE (charger un dump dans `0xFF0000..0xFFFFFF` et
laisser le décodeur TLCS-900/H exécuter le BIOS lui-même) est
**optionnelle**, gated derrière `--bios <file.bin>`, et n'embarque
jamais le dump dans le repo.

---

## 2. Statut actuel (pass 180, 2026-07-10)

`core/execute.py::_try_execute_swi` **dispatche `swi 1` (BIOS
SYSTEM_CALL)** sur l'index vecteur lu dans **RW3** (le byte W du banc
de registres 3). Les autres `swi n` gardent le no-op stub PC-advance
(3..6 = interruptions logicielles sur silicium réel, non modélisées).

**Ground truth du dispatch** : le `SWI` du TLCS-900/H fait
`push32(pc); pc = loadL(0xFFFE00 + (vecteur << 2))`, et le vecteur est
lu dans le **byte de code `0x31` du banc courant**, c'est-à-dire
**RW3**. Ça **tranche définitivement** l'ambiguïté "RW3 vs RC3 vs RB3"
ouverte au pass 179 : c'est **RW3**. Les side-effects par vecteur
viennent de la **table de vecteurs du BIOS SNK d'origine** (désassemblée
depuis l'image BIOS retail). Cross-check sur ROM réelle : le `swi 1` de
Metal Slug @0x200012 vecteurise vers `0xFF1030`, l'entrée 1 de cette
table = CLOCKGEARSET ; notre ému lit RW3=0x01 et prend le même chemin.

**Collapse note (alignement des traces)** : on exécute le handler BIOS
en **1 pas HLE**, là où une exécution non-HLE en prend 2 (le `swi`,
puis l'instruction marqueur placée au vecteur). L'état NET post-retour
est identique → un diff aligné par PC re-converge ; seul un diff aligné
par INDEX d'instruction (`oracle_tools/trace_diff.py`) dérive d'1 pas
par `swi`. Artefact d'outillage, pas un bug de fidélité. L'alignement
par index = le mode real-BIOS (l'autre moitié de "LES DEUX"), chantier
séparé.

`_extract_banked_core_byte(cpu, 3, r32_index, byte_pos)` lit les
bytes du banc 3 (RA3=`(3,0,0)`, RW3=`(3,0,1)`, RC3=`(3,1,0)`,
RB3=`(3,1,1)`). Écriture RA3 via `_build_banked_core_byte_update`.

Effet observable : la plupart des ROMs cc900 du toolchain
**continuent** sans incident car elles n'observent pas activement les
side-effects BIOS (le bytecode généré utilise les BIOS calls pour
des effets globaux comme "charge la font" ou "sauvegarde", pas pour
des valeurs de retour synchrones).

---

## 3. Vecteur d'invocation

Deux mécanismes d'appel BIOS (per `BIOS_REF.md §1-2`) :

### Méthode 1 — SYSTEM_CALL via vecteur (non-bloquant sur IRQ)

```asm
ldb  rw3, VECT_xxx    ; numéro vecteur dans RW3 (bank 3)
; set params dans bank 3 (XBC3, XDE3, etc.)
call SYSTEM_CALL
```

### Méthode 2 — SWI 1 directe (DI pendant exécution, recommandé pour flash/shutdown)

```asm
ldb  rw3, VECT_xxx    ; numéro vecteur dans RW3 (bank 3)
swi  1
```

Dans les deux cas, **RW3 (= reg W bank 3) porte le numéro de vecteur**
et les paramètres passent par les registres bank-3.

### Côté émulateur

Bank 3 = quand `RFP == 3` (Register File Pointer bit dans SR[8..10]).
L'émulateur a déjà :
- `NgpCraft_emulator.core.cpu.NgpcCpuState.rfp` (2-bit value 0..3)
- `_try_execute_swi` qui lit `decoded.raw_bytes[0]` pour le numéro

Pour HLE complet on doit modéliser :
1. Le swap de bank quand `RFP` change (currently RFP est tracked
   mais le swap visible des registres XWA/XBC/XDE/XHL n'est pas
   wired — c'est SR Phase 3)
2. Lire RW3 depuis le shadow bank-3
3. Dispatcher selon RW3 dans une table d'HLE functions

**Court-circuit pragmatique** : pour les BIOS calls qui ne dépendent
pas du contenu bank-3 (`SYSFONTSET`, `CLOCKGEARSET`, `SHUTDOWN`,
`USRSHUTDOWN`), implémenter en HLE direct sans attendre SR Phase 3.
Les BIOS calls bank-3-dependent (`FLASHWRITE`, `RTCGET`) attendent
SR Phase 3 ou sont bypassés par la lib `ngpc_flash` maison.

---

## 4. Table SWI — statut par BIOS call

Référence : `BIOS_REF.md` §4-5.

⚠️ **Correction du mapping RW3** (pass 180) : l'ancienne table
listait des noms mal alignés (RW3=3 "RTCSET", RW3=4 "ALARMSET",
RW3=9 "ALARMDOWNLOAD"). La table de vecteurs du BIOS SNK d'origine
donne l'index EXACT ci-dessous. `[x]` = implémenté pass 180.

| RW3 | Vecteur (0xFFxxxx) | Nom | Statut | Implémentation HLE |
|----:|---------|-----|--------|---|
| 0  | FF27A2 | `VECT_SHUTDOWN`     | [x] | honest-stop status=`bios-shutdown` (le BIOS ne revient jamais) |
| 1  | FF1030 | `VECT_CLOCKGEARSET` | [x] | noop documenté : on émule à pleine vitesse, le clock gear n'affecte pas la référence |
| 2  | FF1440 | `VECT_RTCGET`       | [x] | horloge host→7 octets BCD→buffer XHL3 (`rCodeL(0x3C)`), garde `>=0xC000`. Horloge derrière hook injectable `_bios_rtc_struct_time` (déterministe en test, non-déterministe live comme tout RTC) |
| 3  | FF12B4 | (unknown)           | [x] | noop (le handler retail fait un RET immédiat) |
| 4  | FF1222 | `VECT_INTLVSET`     | [x] | écrit le niveau (RB3) pour la source (RC3) dans les registres INTxx (0x70/71/73/74/79/7A, nibble bas/haut) ; lit d'abord pour préserver l'autre nibble |
| 5  | FF8D8A | `VECT_SYSFONTSET`   | [x] | lit la VRAIE font du BIOS attaché (`0xFF8DCF`, 0x800 o 1bpp) → expand 2bpp en CHAR RAM `0xA000` (0x1000 o). RA3 = couleurs (bits 0-1 avant-plan / quartet haut arrière-plan). Sans BIOS = honest-stop `bios-font-unavailable`. **Décision FIDÉLITÉ, cf §4.1** |
| 6  | FF6FD8 | `VECT_FLASHWRITE`   | [x] | copie `BC3*256` o de RAM (XHL3) vers la fenêtre flash cart (bank RA3 + XDE3), RA3=0 succès. Écrit dans l'overlay session (shadow ROM = NOR flash). ⚠ le chemin cart-write DIRECT (AMD unlock + /WE, lib maison, §5.4) reste un chantier session-layer séparé |
| 7  | FF7042 | `VECT_FLASHALLERS`  | [x] | RA3=0 (SYS_SUCCESS) |
| 8  | FF7082 | `VECT_FLASHERS`     | [x] | RA3=0 (SYS_SUCCESS). Note bug HW blocs 32-34 → reproductible plus tard |
| 9  | FF149B | `VECT_ALARMSET`     | [x] | RA3=0 (SYS_SUCCESS) |
| 10 | FF1033 | (unknown)           | [x] | noop |
| 11 | FF1487 | `VECT_ALARMDOWNSET` | [x] | RA3=0 (SYS_SUCCESS) |
| 12 | FF731F | (unknown)           | [x] | noop |
| 13 | FF70CA | `VECT_FLASHPROTECT` | [x] | RA3=0 (SYS_SUCCESS) |
| 14 | FF17C4 | `VECT_GEMODESET`    | [x] | noop (sans effet observable sur notre modèle GE) |
| 15 | FF1032 | (unknown)           | [x] | noop |
| 16 (0x10) | FF2BBD | `VECT_COMINIT` | [x] | RA3=0 (COM_BUF_OK) |
| 0x11/0x12 | FF2C0C/44 | `COMSENDSTART`/`COMRECIVESTART` | [x] | no-peer : rien à faire |
| 0x13 | FF2C86 | `COMCREATEDATA` | stub nommé | besoin peer + IRQ comms(11) |
| 0x14 | FF2CB4 | `COMGETDATA` | [x] | no-peer : RA3=1 (COM_BUF_EMPTY) |
| 0x15/0x16 | FF2D27/33 | `COMONRTS`/`COMOFFRTS` | [x] | écrit l'octet RTS 0x00B2 = 0 / 1 |
| 0x17/0x18 | FF2D3A/4E | `COMSENDSTATUS`/`COMRECIVESTATUS` | [x] | no-peer : WA3=0 (compteur buffer 0) |
| 0x19/0x1A | FF2D6C/85 | `COMCREATEBUFDATA`/`COMGETBUFDATA` | stub nommé | besoin peer + IRQ comms |
| SWI 3..6 | — | interruptions logicielles | stub PC-advance | `interrupt(0..3)` sur silicium ; pas le chemin system-call |

### 4.1 DÉCISION font SYSFONTSET — TRANCHÉE : FIDÉLITÉ (2026-07-10)

**Choix = FIDÉLITÉ : on utilise la VRAIE font SNK, jamais un
substitut.** La tension licence-vs-fidélité se dissout : **on
n'embarque RIEN**. La font *vit dans le BIOS* que l'utilisateur
fournit (`--bios`), à l'offset BIOS **`0x8DCF`** (= CPU `0xFF8DCF`),
**`0x800` octets** (256 glyphes × 8 lignes, 1 bit/pixel).

Vérifié directement sur le dump retail : le glyphe `0x41` rend un « A »
8×8 correct, et les `0x800` octets à partir de cet offset forment bien
256 glyphes cohérents d'affilée — c'est là que le BIOS range sa font.

⇒ **Rien de propriétaire n'est distribué avec l'émulateur, ET les
glyphes sont pixel-exacts au vrai hardware.** Sans BIOS attaché :
**honest-stop** (`status="bios-font-unavailable"`) plutôt que
fabriquer une font — cohérent avec la doctrine.

Expansion (ce que fait le handler BIOS retail) : chaque octet 1bpp (8 pixels) devient
un mot 16 bits 2bpp (décalage 2 bits/pixel, l'index couleur est OR'd
→ le pixel 0 = MSB source finit dans les bits hauts). RA3 porte les
couleurs : bits 0-1 = avant-plan, quartet haut = arrière-plan.
Destination = CHAR RAM `0x00A000`..`0x00AFFF` (0x800 → 0x1000 o).

---

## 5. Flash HLE — détail

Voir aussi `SAVE_POLICY.md` pour la politique haut niveau et
`Doc de dev/Final/BIOS_FLASH_SAVES_STRATEGY.md` §5 pour le protocole
HW.

### 5.0 Statut (pass 181, 2026-07-10) — chemin DIRECT modélisé

**Le chemin DIRECT (§5.4, la lib flash MAISON qui ne passe pas par le
BIOS) est modélisé** dans `core/flash.py::FlashController`, câblé dans
la boucle `build_run_steps`/`build_run_until`. Point d'interception
PROPRE : chaque store-executor surface déjà une écriture cart/ROM
hardware-discardée comme MemoryWrite `[DISCARDED]` (adresse+data) →
`_apply_flash_writes` route les écritures cart-window vers le
contrôleur, commit dans l'overlay session (shadow ROM = NOR flash).
Modèle = le protocole de commande de la NOR flash de cartouche
(séquence d'unlock 0x202AAA/0x205555 qui ARME, écriture suivante qui
COMMIT), **plus** un gate /WE (I/O 0x6E=0x14). Ce gate est plus strict
que le modèle « unlock seul » : il colle au HW réel et à la lib maison,
et rejette les écritures cart quand /WE n'est pas ouvert. Non-volatile :
survit `reset()`. Le chemin BIOS-médié (VECT_FLASHWRITE, §4) était déjà
fait pass 180. **Restent TODO** : DQ7/DQ5 status-poll (commit synchrone
→ le poll relit direct la valeur finale), block-erase par secteur
(jamais exercé jusqu'ici : les saves n'appendent que 256 o), persistance
disque `.sram` (les bytes sont dans l'overlay = déjà capturés par le
savestate).

### 5.1 Composants à modéliser

1. **Backing flash 8 KB** : un `bytearray` de 8 192 bytes pour le
   block 33 (`0x3FA000..0x3FBFFF`). Initialisé à `0xFF` (état effacé
   de la flash NGPC). Persisté optionnellement sous `<rom>.sram`.
2. **I/O `0x6E` (FLASH_BUS_CTRL)** : flag `flash_we_enabled` toggled
   par writes à cette adresse :
   - `0x14` → `flash_we_enabled = True`
   - `0xF0` → `flash_we_enabled = False`
3. **I/O `0x6F` (FLASH_WD)** : watchdog. `0xB1` = extended, `0x4E` =
   normal. L'émulateur peut le tracker pour validation HW-faithful
   sans appliquer de comportement watchdog (pas de reset auto).
4. **Cart write interception** : pendant `flash_we_enabled`, les
   writes au cart window `0x200000..0x3FFFFF` sont **interceptées** :
   - Pattern AMD unlock detection (séquences à `0x200000` /
     `0x200555` / `0x2002AA`)
   - Erase block command → zero-out le bytearray flash (ou plus
     précisément, set tous les bytes à `0xFF` pour respecter le
     comportement HW de la NOR flash)
   - Write byte command → commit le byte à l'adresse dans le bytearray
5. **Read** : sur read au range `0x3FA000..0x3FBFFF`, le bus retourne
   le byte depuis le bytearray flash (overlay au-dessus de la ROM
   image qui contient peut-être une version initiale).

### 5.2 Persistance disque

- Au `load_machine_state(rom)` : si `<rom>.sram` existe, charger
  dans le bytearray flash.
- À la fin d'un run (ou via CLI explicite `flash-export`), dumper
  le bytearray vers `<rom>.sram`.
- Format : binaire brut 8 KB, byte-for-byte image du block 33.
- Le savestate v2 capture aussi le bytearray flash (via le
  `writable_overlay`), donc on a deux niveaux de persistance :
  cart flash réelle (`.sram`) + savestate snapshot (`.json`).

### 5.3 Bugs HW reproductibles

L'émulateur peut **simuler** les bugs HW connus (référence
`ngpc_flash.h` docstring) pour valider que les programmes user
les évitent correctement :

| Bug | Reproduction émulateur | Pourquoi utile |
|-----|------------------------|----------------|
| `VECT_FLASHERS` ne peut pas effacer blocs 32-34 | Si une ROM appelle SWI 1 RW3=8 avec param block 32/33/34, retourner status échec | Valider que la ROM utilise bien la lib `ngpc_flash` maison qui contourne |
| `CLR_FLASH_RAM` silent-fail au 2e call | Compter les calls par power-on session, 2e call = no-op silencieux | Valider que la ROM utilise le pattern append-only (1 erase max par session) |
| Writes directs sans `(0x6E)=0x14` | Ignorer les writes au cart window quand `flash_we_enabled == False` | Valider que la ROM toggle correctement `/WE` avant d'écrire |

C'est l'extension naturelle de la doctrine HW-faithful déjà
appliquée aux opcodes silicon-broken (D0+ALU-imm, D8 r+r).

---

## 6. Tests attendus quand on shippe HLE

Référence ROM : `StarGunner_save_lib_test/bin/main.ngc`
(HW-validated).

### Tests unitaires (à ajouter dans `tests/test_bios_hle.py`)

1. `test_swi_noop_stub_advances_pc` (déjà couvert dans `test_execute.py`)
2. `test_flash_we_toggle_via_io_0x6e`
3. `test_amd_unlock_sequence_recognition`
4. `test_erase_block_33_zeros_8kb`
5. `test_write_byte_commits_to_flash_backing`
6. `test_writes_without_we_enabled_are_noop` (bug HW reproduit)
7. `test_savestate_v2_captures_flash_overlay`
8. `test_sysfontset_copies_font_to_char_ram_tiles_32_to_127`
9. `test_user_shutdown_stops_honestly`
10. `test_clockgearset_is_noop_documented`

### Tests end-to-end

1. Lancer StarGunner_save_lib_test, exécuter jusqu'à la routine de
   save (post-init savestate), vérifier que le block 33 contient
   bien un slot avec checksum valide.
2. Reload depuis le savestate, vérifier que la flash persiste.
3. Re-save, vérifier que le slot index a augmenté de 1 (pattern
   append-only).

---

## 7. Option LLE complémentaire (M8+)

Une fois HLE stable, ajouter `--bios <file.bin>` :

```
ngpc_emu.py run-until-exec game.ngc 0xFF0100 --bios ~/dumps/ngp.bios
```

Chargement :
1. `argparse` parse `--bios <path>`
2. `load_machine_state(rom_path, bios_path=...)` lit 64 KB du dump
3. La région `BIOS_ROM` dans `core/bus.py` est backed par les bytes
   du dump au lieu d'être `unbacked`
4. Quand l'executor décode `SWI n`, deux options :
   - Mode HLE pur (défaut) : intercepter en Python
   - Mode LLE (avec `--bios`) : laisser le CPU sauter au vecteur
     dans `0xFF0000..` et exécuter le BIOS comme une ROM normale
5. Une option `--bios-mode hle|lle|mixed` permet de choisir

Le dump n'est **jamais distribué** avec l'émulateur. `.gitignore`
inclut `*.bios`, `*.bios.bin`, `**/ngp.bios*` pour éviter tout
commit accidentel.

---

## 8. Workflow gap-filling

Quand une ROM rencontre un BIOS call non implémenté :

1. **Vérifier l'event log** : status `stopped-on-unsupported-decoded-instruction`
   ou note `Executed SWI N: BIOS call not modeled`.
2. **Identifier le BIOS call** : numéro SWI + valeur de RW3 au
   moment du SWI (via `registers --seed-from <state>` à l'event
   précédant).
3. **Vérifier `BIOS_REF.md` §4-5** : description de ce que ce call
   est censé faire.
4. **Implémenter en HLE** dans `_try_execute_swi` :
   - Lire les paramètres depuis bank-3 (XBC3, XDE3, XHL3, …)
   - Appliquer le side-effect sur le runtime overlay
   - Écrire le return value dans RA3 si applicable
   - Avancer PC normalement
5. **Ajouter un test** dans `tests/test_bios_hle.py`.
6. **Mettre à jour ce document** (§4 table SWI) avec le statut.

Si `BIOS_REF.md` est insuffisant : voir le workflow §7 du master
strategy doc (disasm local du BIOS perso, enrichir `BIOS_REF.md`
en prose).

---

## 9. Références

- Master strategy : `BIOS_FLASH_SAVES_STRATEGY.md`
- BIOS calls doc : `BIOS_REF.md`
- Lib flash maison : `src/core/ngpc_flash.{h,c}` + `ngpc_flash_asm.asm`
- Smoke ROM : `StarGunner_save_lib_test/bin/main.ngc`
- Politique saves : `SAVE_POLICY.md` (dans ce repo)
- Savestate v2 spec : `specs/SAVESTATE.md`
- HW quirks : `core/quirks_db.json` (v `2026-05-20.v4`)
