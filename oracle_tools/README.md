# oracle_tools — les gates de validation du cœur

Outils qui répondent à une seule question : **nos deux cœurs, et les octets
qu'ils produisent, sont-ils d'accord avec les sources primaires ?**

Tout ici est à nous. Aucun outil ne lit, ne compile ni ne lie du code d'un autre
émulateur.

---

## Politique de sources

Par ordre d'autorité décroissante :

1. **Le silicium.** Une ROM de `hw_calibration/` flashée sur une vraie NGPC.
   Tranche tout le reste.
2. **La documentation Toshiba** (`doc t_900`) — listes d'instructions, lignes de
   symboles pour les flags, tables de cycles.
3. **L'assembleur officiel** (`asm900`) — vérité terrain sur les *encodages*.
4. **`NGPC_HW_QUICKREF.md`** et la doc SDK — carte des registres, valeurs de reset.
5. **Les chiffres qui circulent dans la scène** — ouï-dire. Ils ont déjà perdu
   contre le datasheet (sur `JR` : 8/4 annoncé, Toshiba dit 5/2). Jamais une
   autorité ; au mieux le signal qu'il faut aller mesurer.

⚠️ **Aucun code d'émulateur tiers n'entre dans ce dépôt** — ni source, ni
binaire, ni table extraite. Les extracteurs de cycles/noms et le co-simulateur
natif qui vivaient ici ont été retirés le 2026-07-19 : ils lisaient ou liaient
du code sous copyleft, et chaque fait qu'ils fournissaient est déjà disponible
dans les sources 1 à 4 ci-dessus.

---

## Outils

### `native_diff.py` — ⭐ LE gate du portage C++

Exécute **le cœur Python et le cœur C++ sur la même entrée** et compare tout
l'état architecturalement visible : PC, les 8 registres, les 6 flags, IFF, RFP,
chaque écriture mémoire (adresse, octets, rejetée ou non), le compte de cycles,
le statut terminal.

Les deux modèles étant à nous, **une divergence est un bug**, jamais un point de
triage. C'est aussi le **seul** gate qui atteint les opcodes qu'aucune ROM du
corpus n'exécute : un balayage de ROMs ne peut, par construction, exercer que ce
que les jeux font tourner.

### `trace_equiv.py`

Équivalence de traces : deux exécutions du même programme doivent produire la
même suite d'états. Sert à prouver qu'un changement (refactor, optimisation) n'a
rien modifié d'observable.

### `trace_diff.py`

Aligne deux traces par index d'instruction et signale la PREMIÈRE divergence
avec son contexte.

⚠️ **Triage, pas verdict** dès que le côté référence n'est pas un de nos cœurs :
une trace extérieure lisse en général les opcodes cassés et les bizarreries
silicium (open-bus, `D0` ALU-imm, famille `C8..CF`) qu'aucun jeu commercial ne
déclenche. Une divergence veut dire « investiguer, et si besoin flasher sur une
vraie NGPC » — jamais « on a tort ». Pour un gate qui *est* un verdict, prendre
`native_diff.py`.

### `dump_our_trace.py`

Émet la trace du cœur Python au schéma de `trace_diff.py`, un objet JSON par
instruction, état APRÈS chaque instruction.

Notre cœur est un oracle honnête : il refuse d'exécuter sur des entrées
inconnues plutôt que d'inventer. Les registres non modélisés sortent en `null`,
et `trace_diff.py` saute ces créneaux — un inconnu ne peut donc jamais se faire
passer pour une divergence.

```bash
python oracle_tools/dump_our_trace.py ROM.ngc -n 5000 > ours.jsonl
python oracle_tools/trace_diff.py ref.jsonl ours.jsonl
```

### `asm900_oracle.py`

Confronte nos encodages à ceux de l'assembleur officiel — la seule autorité qui
tranche « cette suite d'octets signifie-t-elle bien cette instruction ».

---

## Dépendances

Python seul, aucun compilateur requis. Se lance depuis la racine du dépôt pour
que les imports `core` se résolvent.
