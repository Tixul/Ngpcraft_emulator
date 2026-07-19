# NgpCraft Emulator - Save Policy

> ## ⛔ RETRACTATION 2026-07-14 — le ✅ ci-dessous ETAIT FAUX
>
> Ce fichier a affiche pendant quatre jours :
>
> > ✅ STATUT 2026-07-10 — les saves in-game FONCTIONNENT (les deux chemins)
>
> **Elles ne fonctionnaient pas.** L'utilisateur a perdu une save et l'a signale ;
> il avait raison. Comment ce faux-vert a ete fabrique — les ingredients sont
> toujours dans le projet :
>
> 1. Le chemin save etait implemente et teste **dans le coeur Python**. Le coeur
>    Python retire ~1 700 instructions/s ; un NGPC en demande ~615 000. **Il n'a
>    jamais pu faire tourner un jeu.** Le coeur sur lequel on JOUE est le C++.
> 2. Dans le coeur C++, `Machine::flash_command()` gerait l'unlock AMD et
>    l'autoselect — assez pour que le BIOS identifie la cartouche — puis, selon son
>    propre commentaire, *"swallowed, not faked"* chaque erase et chaque program.
> 3. Donc : tests verts, doc ✅, et **chaque save partait au neant, en silence.**
>
> 🔑 **Une feature verte dans un coeur sur lequel personne ne joue n'est pas une
> feature.** Les tests ne mentaient pas sur le code qu'ils testaient ; ils visaient
> la mauvaise machine. (Meme famille que `fe_match` visant `thc1` sans flag.)
>
> ## ✅ STATUT 2026-07-14 — les saves in-game fonctionnent POUR DE VRAI
>
> **Spec complete : `specs/FLASH.md`.** Chemin reel, valide sur du code de jeu
> commercial : **jeu → `swi 1` → la VRAIE routine flash du BIOS → cycles de commande
> AMD → la puce → `saves/<rom>.flash` → re-injecte quand on remet la cartouche.**
>
> * **Six jeux du commerce** initialisent leur zone de save au boot, sans aucune
>   entree joueur, et ecrivent exactement dans les petits blocs du haut de la puce.
>   Chacune de ces ecritures partait au neant avant aujourd'hui.
> * **Round-trip power-cycle sur Puzzle Link 2** : au 2ᵉ boot, le jeu se comporte
>   DIFFEREMMENT d'une cartouche vierge (son compteur `0x2F8000` passe de 0 a 1) —
>   la seule assertion qu'aucune plomberie de notre cote ne peut simuler.
> * ⚠️ **Sans image BIOS, pas de save** : le jeu passe par `swi 1`, dont le vecteur
>   lit 0. C'est ce que ferait une console sans BIOS.
>
> 🔑 **L'octet qui cassait tout : `0x6C58`** — le BIOS y note quelle cartouche il a
> trouvee au power-on (1 = 4 Mbit, 2 = 8, 3 = 16, 0 = aucune), et sa routine flash
> le lit AVANT tout et renvoie l'erreur 0xFF si il est nul. Notre hand-off saute le
> boot du BIOS : **personne ne l'avait jamais ecrit.** Toutes les couches en dessous
> etaient justes et la save ne partait nulle part quand meme.
>
> Restent `todo`, **documentes et non maquilles** (`specs/FLASH.md` §7) : polling
> DQ7/DQ5 / timing d'erase (on commit de facon synchrone : le *resultat* est correct,
> il n'y a pas de modele de timing) ; endurance des cellules ; protection de bloc non
> persistee (le format n'a pas de place, et aucun autre emulateur ne la garde).

## 1. But

Le projet doit gerer correctement les sauvegardes persistantes des jeux.
Ce point est critique, car beaucoup d'emulateurs NGPC sont faibles ou incoherents ici.

La politique du projet est simple:
- les save states sont une chose
- les saves in-game en sont une autre
- les deux doivent etre geres serieusement, mais separement

## 2. Distinction obligatoire

### 2.1 Save state

Capture instantanee de l'etat complet de la machine emulee.

Usage:
- debug
- rewind
- reprise instantanee

### 2.2 Save in-game persistante

Donnees ecrites par le jeu dans son support de sauvegarde.

Usage:
- progression joueur
- options
- scores
- etat permanent du jeu

Non acceptable:
- melanger les deux dans l'UX
- faire croire qu'un save state remplace la sauvegarde du jeu

## 3. Exigences de support

Le coeur doit:
- identifier le type de sauvegarde attendu par la ROM si possible
- charger les donnees persistantes associees
- les maintenir coherentes en cours de session
- les re-ecrire proprement sur disque

Le frontend doit:
- exposer clairement ou vivent les saves
- permettre backup/import/export plus tard
- ne pas detruire une save par erreur lors d'un rebuild ou d'un changement de ROM

## 4. Comportement attendu

Une sauvegarde correcte doit:
- se recreer a la session suivante
- survivre a un redemarrage normal
- rester dissociee des save states
- etre utilisable autant en standalone qu'en integration `NgpCraft_engine`

## 5. Headless and CI

Le mode headless doit permettre:
- de precharger une save
- d'executer une ROM avec sa save
- de verifier qu'une save a ete creee ou modifiee

Ce point est important pour:
- tests de non-regression
- validation de jeux avec progression
- reproduction de bugs lies a l'etat sauvegarde

## 6. UX minimale

L'utilisateur doit comprendre sans ambiguite:
- ou est la sauvegarde du jeu
- ce qu'est un save state
- ce qui est exportable ou replaceable

## 7. Tests obligatoires

Il faut au minimum:
- un test de creation de save
- un test de rechargement
- un test de round-trip
- un test de non-confusion save state / save persistante

## 8. Definition de succes

La politique est respectee quand:
- une ROM avec sauvegarde fonctionne sur plusieurs sessions
- le comportement est stable
- l'UX est claire
- l'integration `NgpCraft_engine` ne casse pas la persistence

## 9. Reference d'implementation et docs cross-projets

Cette politique est **abstraite**. L'implementation concrete vit
ailleurs et est deja en grande partie ecrite cote toolchain :

- **Master strategy index (point d'entree unique cross-projets)** :
  `../Doc de dev/Final/BIOS_FLASH_SAVES_STRATEGY.md`
  — couvre BIOS HLE, flash, saves pour les 3 projets de l'ecosysteme
  NgpCraft (toolchain + emulateur + live editor).

- **Spec HLE detaillee emulateur** :
  `specs/BIOS_HLE.md` — table SWI complete, statut par BIOS call,
  workflow gap-filling, plan de tests.

- **Lib de reference (propre au projet, HW-validated)** :
  `../Doc de dev/NgpCraft_base_template/NgpCraft_base_template/src/core/ngpc_flash.{h,c}` + `ngpc_flash_asm.asm` — lib developpee
  pour le projet NgpCraft, utilisee par les ROMs cc900 du toolchain
  pour bypass les bugs BIOS flash. **Source de verite** pour le
  protocole AMD flash et le pattern append-only (32 slots × 256
  bytes dans block 33). Reutilisable librement pour l'HLE emulateur,
  aucune contrainte de licence externe (c'est notre code).

- **ROM de smoke HW-validated** :
  `../NgpCraft_toolchain/StarGunner_save_lib_test/bin/main.ngc` —
  exerce toute la stack save. A utiliser pour les tests end-to-end
  d'implementation HLE flash.

- **Documentation fonctionnelle BIOS** :
  `../Doc de dev/Final/BIOS_REF.md` — table SWI, conventions
  d'appel, parametres bank-3. Suffisante pour 95% des HLE.

- **Format savestate** :
  `specs/SAVESTATE.md` v2 — capture le runtime overlay incluant
  l'image flash. Les saves in-game persistantes (separes des
  savestates) seront persistees dans `<rom>.sram` quand le HLE
  flash sera livre.

Quand cette politique evolue, **mettre a jour le master strategy
index** plutot que de dupliquer l'info ici.
