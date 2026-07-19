# NgpCraft Emulator - Performance And Timing Fidelity Policy

## 1. But

Le projet ne doit pas seulement etre fonctionnellement correct.
Il doit aussi etre honnete sur la cadence reelle de la machine emulee.

Si un jeu:
- tient 60 fps sur hardware, l'emulateur doit tenir 60 fps
- tombe a 30 fps sur hardware, l'emulateur doit tomber a 30 fps
- s'effondre vers 20 fps sur hardware, l'emulateur de reference doit reproduire ce slowdown

La machine hote ne doit pas "embellir" la verite de la machine emulee.

## 2. Regle cardinale

Le mode de reference ne doit pas:
- lisser artificiellement une surcharge CPU
- cacher des frames manquees
- faire croire qu'un jeu est fluide alors qu'il ne l'est pas sur hardware

Le but n'est pas de rendre les jeux plus agreables.
Le but est de reproduire leur comportement reel.

## 3. Trois notions a separer

### 3.1 Temps hote

Temps reel de la machine du joueur.

### 3.2 Temps emule

Temps de la NGPC simulee:
- cycles CPU
- scanlines
- VBlank/HBlank
- timers
- DMA

### 3.3 Cadence visible

Cadence effectivement percue par le joueur dans l'emulation:
- frames produites
- frames manquees
- logique jeu qui avance moins vite

Ces trois notions doivent etre mesurees separement.

## 4. Politique de rendu

Le frontend peut:
- afficher des stats
- offrir des overlays
- proposer des graphes

Le frontend ne peut pas, par defaut:
- compenser silencieusement un budget frame depasse
- interpoler pour faire croire que le jeu est fluide
- decoupler le rendu de facon trompeuse pour masquer une surcharge du coeur

Si des options de confort existent un jour:
- elles doivent etre explicites
- elles doivent etre marquees non-reference
- elles ne doivent jamais servir au debug toolchain

## 5. Metriques obligatoires

Le coeur doit pouvoir exposer:
- cycles consommes par frame emulee
- budget frame theorique
- nombre de frames manquees
- temps passe en IRQ
- temps ou cout associe au DMA si possible
- cadence emulee effective

Le profiler doit pouvoir montrer:
- pourquoi une scene tombe a 20 fps
- quels symboles ou evenements consomment le budget

## 6. Cas d'acceptation

Un cas "slowdown fidelity" est considere bon si:
- la scene lourde reproduit une cadence comparable au hardware
- le budget frame depasse est visible dans les outils
- les differents builds peuvent etre compares proprement

Exemples de cas a couvrir:
- scenes avec beaucoup de sprites
- streaming tilemap
- effets DMA
- IRQ/video lourdes
- regressions de toolchain ou de moteur constatees sur vrai hardware

## 7. Validation

Il faut construire un corpus de scenes de reference:
- scene legere 60 fps
- scene intermediaire
- scene lourde 20-30 fps
- cas limites avec DMA et raster

Pour chaque scene:
- mesure hardware
- mesure emulation
- interpretation documentee

## 8. Ce que le projet ne doit pas faire

Non acceptable:
- annoncer 60 fps quand la logique emulee rate des budgets frame
- utiliser le rendu hote pour cacher un manque de temps machine
- melanger perf de l'emulateur et perf de la machine emulee dans les rapports

## 9. Definition de succes

La politique est respectee quand:
- les chutes de cadence reelles sont reproduites
- elles sont mesurables
- elles sont expliquables
- elles ne sont pas masquees par le frontend

---

## 10. Vitesse du modele de reference Python (mesure 2026-07-10)

⚠️ Ne pas confondre avec les §1-9 : celles-ci parlent de la **cadence de la
machine emulee**. Cette section parle du **debit de notre interpreteur**.

**Etat mesure : ~1 700 instructions/seconde** (CPython, `EmulatorSession`, Crush
Roller). C'est le mur documente depuis la passe 175 : un boot BIOS reel demande
des centaines de milliers d'instructions, une frame commerciale des millions.
**Le prototype Python ne peut pas y arriver par design.**

### 10.1 Optimisations deja faites (behaviour-neutral, DEVLOG passe 186)

| Optimisation | Ce qui n'allait pas |
|---|---|
| **Cache de la fetch view** | `_build_fetch_view` appelait `load_fetch_view` a **chaque batch** — relecture de la ROM **depuis le disque** + reconstruction de la map cold-start (~50 000 entrees), tous les 50 instructions. Or **2 octets seulement** dependent de la frame (RAS.V, BLNK). |
| **Memoisation de `probe()`** | `NgpcAddressSpace.probe()` parcourait la liste des regions et allouait un `AddressProbe` neuf **pour chaque octet lu** : **370 000 appels `contains` pour 4 000 instructions** (~93 comparaisons/instruction). L'espace d'adressage est `frozen` ⇒ `probe()` est **pure** ⇒ memoisable sans changer le comportement. |

**Gain : 1 123 → 1 706 instr/s (x1,5).** Fidelite re-verifiee apres coup contre
le cœur de référence (`oracle_tools/native_diff.py`) : **0 divergence**.

### 10.2 Cahier des charges du coeur natif (C++)

Le cout restant est **structurel**. Le profil (16 000 instructions) le dit
precisement — c'est exactement ce qu'un coeur natif supprime :

1. **`_dispatch_execute_next` est une chaine LINEAIRE de ~100 `_try_execute_*`**,
   essayes un par un jusqu'a ce qu'un matche — **pour chaque instruction**.
   → une **table de saut sur l'octet d'opcode** elimine ca.
2. **Le decodeur lit ~7 octets par instruction, UN PAR UN**, chacun traversant
   trois couches (`_RuntimeOverlayDecodeBus.read_bytes` → `NgpcReadBus.read_bytes`
   → `probe`) : **113 861 appels `read_bytes` pour 16 000 instructions**.
   → un coeur natif lit un mot dans un **tableau plat**.
3. **Churn `dataclasses.replace`** : 32 003 appels / 16 000 instructions sur l'etat
   CPU `frozen`.
   → etat CPU **mutable** dans le coeur chaud.

**Regle qui ne change pas :** le coeur rapide doit rester *reference-exact*. Toute
optimisation se valide contre `oracle_tools/native_diff.py` sur le corpus (voir
`README.md`), et une divergence est un **bug d'optimisation**, pas une licence.
