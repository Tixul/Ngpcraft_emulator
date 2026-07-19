# NgpCraft Emulator - Hardware Compatibility Policy

> ## ✅ RÉSOLU HW (2026-07-03) — le préfixe `D8..DF` est **WORD (16-bit)**, pas long
>
> **Tranché définitivement sur vraie NGPC.** Le doute ouvert le 2026-07-02 sur
> la taille de la famille `D8..DF` est clos par un ROM de test flashé
> (`hw_test_off`, toolchain officielle cc900) sur une vraie console :
>
> | Test flashé | Octets | Résultat HW | Verdict |
> |---|---|---|---|
> | `ld xbc, xwa` | `D8 89` | **AAAA3344** | copie **16-bit** (`ld BC, WA` : BC←WA, high de XBC intact) |
> | `djnz xbc` | `D9 1C` | **0002FFFF** | décrément **16-bit** (`djnz BC` : BC 0x0000→0xFFFF, high intact) |
>
> **Conséquence — erreur systématique corrigée.** Le repo classait `D8..DF`
> comme le préfixe **long (32-bit)**. C'était faux et jamais vérifié HW. La
> vérité (ngdis `masker.h` `getzz(0xD8)=1`=word, confirmée par ces 2 mesures) :
> - `C8..CF` = byte, `D0..D7` = word, **`D8..DF` = word**, **`E8..EF` = long**.
> - Le repo collapsait `D8..DF` **et** `E8..EF` dans « long » ; le **vrai**
>   préfixe long est `E8..EF`.
>
> **Fix appliqué (décodeur + exécuteur).** `_prefixed_register_info` et
> `_prefixed_register_execute_info` mappent `D8..DF → word`, `E8..EF → long`.
> `D8 89` décode `ld BC, WA` (copie word, high préservé) et **s'exécute** ;
> `D9 1C` décode `djnz BC` (word) et **s'exécute**. La copie `ld` **long**
> (`E8..EF`) s'exécute aussi (32-bit). Les sous-cas special (mula/mirr/minc/
> bs1f, largeur figée) sont inchangés. Effet concret : le boot BIOS réel passe
> de 143 → **189 instructions** (le mauvais alignement long est levé).
>
> **`mr_robot`** boote toujours : son `D8 89` est un `ld BC, WA` word valide —
> cohérent avec le jeu qui tourne sur console.
>
> **`D8 8B` (ancien « crash HW » attribué avril/mai) :** sous le décodage
> correct c'est `ld HL, WA` (word), pas `ld XHL, XWA`. La question de taille est
> résolue ; si un crash réel existait il portait sur la destination `HL` en
> word — reste une note-de-côté mineure (jamais reproduite depuis), non
> bloquante. Voir `core/quirks_db.json` `cpu.d8_df_register_to_register`.

> ## ✅ RÉSOLU HW (2026-07-03) — `D0..D7` est une famille MÉMOIRE word, PAS registre-direct
>
> **Tranché sur vraie NGPC** (ROM flashé `hw_test_d0`, v2 sentinelle+canari) :
>
> | Test flashé | Résultat HW | Verdict |
> |---|---|---|
> | `D0 89` (croyait `ld BC,WA` reg-direct) | store résultat **sauté**, canari OK | `D0` **consomme des octets d'opérande** (mis-aligne), **NE plante PAS** |
>
> **Conséquence.** `0xD0..0xD7` n'est **pas** le préfixe registre-direct word
> (ça c'est `0xD8..0xDF`) — c'est une **famille d'adressage MÉMOIRE word**
> (parallèle de `0xC0..0xC7` byte). Croisé ngdis : `getmem(0xD0)`→`decode_zz_mem`
> (mémoire) vs `getmem(0xD8)`→`decode_zz_r` (registre). Ex. `D0 B6 3F 50 00` =
> `cpw (0xB6), 0x0050`, PAS le 2-octets `sbc IZ, WA`.
>
> **Le quirk `cpu.d0_d7_non_immediate` était un MIS-DIAGNOSTIC** (mis-décode, pas
> silicon-broken) — reframé en v9. Le crash 2026-05-20 (`D0 C8`) = le toolchain a
> émis `0xD0` pour du word-reg-immédiat, mais `0xD0` est un mode mémoire → octets
> malformés → crash (mis-encode, pas bug silicium). **Garde conservée** : le
> toolchain ne doit jamais émettre d'op word-registre avec un préfixe `0xD0..0xD7`.
>
> **Fix (pass 155).** Décode+exécute les formes abs8 word (`cpw`, `ldw R16`) ;
> le re-décodage complet de la famille `D0..D7` reste un chantier en cours.

> ## ⚠️ RÉTRACTATION (2026-07-10) — « niveau VBlank = 6 » était FAUX
>
> Un jalon du 2026-07-03 affirmait ici : *« niveau VBlank corrigé 4→6 (le BIOS fait
> `ei 5; halt`, et TLCS-900 n'accepte que `level > iff`) »*. **Les deux moitiés de
> ce raisonnement sont fausses**, et c'est un cas d'école à retenir :
>
> 1. **La règle de masque était off-by-one.** Le manuel CPU Toshiba TLCS-900/L1
>    (SR bits 12-14, IFF2:0) dit : `110` = *« enables interrupts with **level 6 or
>    higher** »*. Donc une IRQ de niveau `L` est acceptée quand **`L >= IFF`**, pas
>    `L > IFF`.
> 2. **Le niveau VBlank est 4.** Le SDK officiel SNK (`01_SDK/docs/SysPro.txt`)
>    l'écrit noir sur blanc : *« It is forbidden to prohibit Vertical Blanking
>    Interrupt (**Interrupt level 4**) »*.
>
> Le « 6 » était une **inférence bâtie sur le bug de gate**. Avec le gate corrigé,
> la prémisse s'effondre : `ei 5` masque bien un VBlank niveau 4, et le `halt`
> d'init du BIOS est réveillé par une source **plus prioritaire** (timer / ADC,
> dont le BIOS programme lui-même le niveau via `VECT_INTLVSET`).
>
> **LEÇON DE DOCTRINE : deux sources documentées battent une inférence — surtout
> une inférence bâtie sur un composant qu'on n'a pas vérifié.** Voir DEVLOG passes
> 183-184 et `specs/FRAME_TIMING.md` § 3.6.

> ## 📚 SOURCES AUTORITATIVES (acquises 2026-07-10) — à consulter AVANT d'inférer
>
> Trois documents constructeur couvrent désormais l'essentiel du matériel. **Ne
> plus deviner ce qu'ils contiennent :**
>
> | Document | Chemin | Couvre |
> |---|---|---|
> | **Manuel CPU Toshiba TLCS-900/L1** | `NgpCraft_toolchain/doc t_900/catalog_en_20010831_ALT00146.txt` | SR/IFF, règles de masque, base des vecteurs (0xFFFF00), reset |
> | **Datasheet Toshiba TMP95C061** | (PDF constructeur) | **Table 3.3(1) = table complète des vecteurs d'interruption**, ADC (ADMOD/ADREG), timers, prescaler |
> | **SDK officiel SNK** | `01_SDK/docs/` | `SysPro`/`SysWork` (vecteurs RAM, batterie), `8Bit` (timers), `K2GETechRef`, `SerialCom`, `MicroDMA` |
>
> ⚠️ **Les TABLES du PDF datasheet sont des IMAGES** → invisibles en conversion
> texte. Méthode : rendre la page en PNG avec `pymupdf` (`fitz`) puis la **lire**.
> Pages clés : **11** = Table des interruptions, **148** = ADMOD, **149/150** = ADREG.

## 1. But

Le projet ne vise pas une machine "idealisee".
Il vise une machine utile, mais fidele au comportement du hardware reel, y compris quand ce comportement est moche.

En clair:
- si le hardware reel boote, l'emulateur doit booter
- si le hardware reel glitch, l'emulateur doit glitcher
- si le hardware reel freeze ou plante sur un cas connu, l'emulateur de reference doit freeze ou planter aussi

La valeur ajoutee de l'emulateur n'est pas de cacher ces problemes.
La valeur ajoutee est de les expliquer.

## 2. Regle cardinale

Le coeur d'emulation n'a pas le droit de "corriger" en douce:
- un opcode casse
- un comportement non documente mais observe
- un bug silicium
- un timing limite qui casse sur machine reelle

Le mode de reference doit rester hardware-faithful.

## 3. Deux couches distinctes

### 3.1 Couche execution

Responsable de:
- reproduire le comportement reel
- y compris les comportements defectueux connus

Cette couche decide:
- ce qui est execute
- comment c'est execute
- quand ca plante

### 3.2 Couche diagnostic

Responsable de:
- observer
- annoter
- expliquer
- capturer

Cette couche peut:
- signaler qu'un opcode casse vient d'etre execute
- signaler qu'un pattern correspond a un bug silicium connu
- produire un rapport de crash enrichi
- suggerer une piste

Cette couche ne peut pas:
- changer l'execution par defaut
- contourner un freeze
- corriger un registre ou un flag
- inventer un chemin "plus stable" que le hardware reel

## 4. Modes autorises

### 4.1 Reference hardware

Mode par defaut pour:
- validation
- regression
- comparaisons hardware
- debug serieux

Proprietes:
- comportement de reference
- quirks actifs
- aucune correction silencieuse

### 4.2 Diagnostic assist

Meme execution que `reference hardware`, avec en plus:
- overlays
- warnings
- etiquettes de quirk
- crash reports plus riches
- exports de timeline/trace

Important:
- les diagnostics n'ont pas le droit de changer le resultat

### 4.3 Non-reference modes

Si un jour un mode plus permissif existe pour le confort utilisateur:
- il doit etre optionnel
- il doit etre clairement etiquete non-reference
- il ne doit jamais servir de base pour valider la toolchain
- il ne doit jamais remplacer le mode de reference dans les tests

## 5. Base de connaissances quirk

Le projet doit maintenir une base versionnee des cas connus:
- opcodes casses
- instructions partiellement documentees
- timings limites
- comportements DMA/IRQ/video atypiques
- bugs silicium confirmes ou fortement suspectes

Pour chaque entree:
- identifiant unique
- categorie
- description courte
- source documentaire ou observation
- niveau de confiance
- ROM ou test de reproduction
- impact visible
- statut implementation

Niveaux de confiance recommandes:
- `documented`
- `observed`
- `suspected`

## 6. Politique de crash

Quand le hardware reel crash sur un cas connu, l'emulateur doit:
- reproduire le crash ou freeze dans le mode de reference
- capturer les dernieres instructions
- capturer l'etat CPU
- capturer les derniers evenements machine importants
- fournir un resume lisible

Le rapport ideal contient:
- PC, SP, flags, registres
- derniere instruction executee
- 32 a 256 dernieres instructions selon le mode
- derniers evenements IRQ/DMA/HBlank/VBlank
- acces memoire/IO recents si actifs
- quirk ou bug connu potentiellement implique
- lien vers la doc ou le test associe si disponible

## 7. Politique d'undefined behavior

Quand un comportement est reellement inconnu:
- ne pas inventer un comportement "gentil"
- marquer le cas comme gap
- conserver une trace exploitable
- permettre d'ajouter rapidement un test de reproduction

Si un comportement probable existe mais n'est pas prouve:
- l'annoter comme `suspected`
- le garder desactive par defaut tant qu'il n'est pas valide
- ne jamais le presenter comme "hardware accurate" sans preuve

## 8. Tests obligatoires

Chaque quirk important doit avoir, si possible:
- un test unitaire ou micro test
- une ROM de reproduction
- un attendu minimal
- un lien vers la source de verite

Familles prioritaires:
- opcodes casses
- prologues/stack edge cases
- DMA et timings video
- IRQ et priorites
- comportements de freeze observes sur hardware reel

## 9. Ce que le projet ne doit pas faire

Non acceptable:
- cacher un plantage avec un fallback silencieux
- continuer l'execution apres un etat impossible en pretendant que tout va bien
- afficher "supporte" pour un quirk non teste
- utiliser un mode permissif comme base du CI

## 10. Definition de succes

La politique est respectee quand:
- les cas de crash hardware connus sont reproduits
- le debugger explique mieux qu'un emulateur standard pourquoi ca casse
- les diagnostics aident, sans changer l'execution
- la base quirk devient un actif central du projet
