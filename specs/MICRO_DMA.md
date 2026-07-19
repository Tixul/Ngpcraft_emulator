# Le micro-DMA — l'interruption qui ne va PAS au CPU

> **La réponse à une enquête de trois passes.** Dix ROMs commerciales s'éteignaient
> pendant leur boot. J'ai cru successivement à une interruption manquante, puis à
> une erreur d'ordre de nibbles. **Les deux ont été réfutées par le corpus.** La
> vraie cause est ici, et elle est écrite dans la doc SNK depuis toujours.

## 1. Ce qui se passe aujourd'hui

    vec[16] (INTT0, timer 0) -> 0xFF22A5     l'ISR Timer 0 du BIOS
    0xFF22A9   pushw (0x6FD4)                le hook UTILISATEUR du timer 0
    0xFF22AD   ret                           saute à ce qu'il y a lu
    0x000000   nop                           ⚠️ 0x6FD4 vaut ZÉRO
    0x000002   swi 7                         la trappe qui s'y trouve
    0xFF2226   di ; … ; halt                 le gestionnaire d'ERREUR du BIOS
                                             → extinction

Et **personne n'écrit jamais `0x6FD4`** — ni le BIOS, ni le jeu.

## 2. Pourquoi ce n'est pas un bug du jeu

Le jeu programme ceci, à `0x20126C` :

```
TRUN   <- 0x80     arrête les timers, prescaler en marche
T01MOD <- 0x00     timer 0 : horloge externe TI0 = le BLANC HORIZONTAL
TREG0  <- 0x01     match à CHAQUE ligne
TREG1  <- 0x01
TRUN   <- 0x83     lance les timers 0 et 1
```

Un match à chaque ligne, sur 198 lignes, à 60 images/seconde : **11 880 interruptions
par seconde**. Aucun jeu ne fait ça avec une ISR logicielle. Et la doc SNK dit
exactement pourquoi :

> **« Use of timer 0 and micro DMA allows raster scroll effect. »**
> — `01_SDK/docs/MANSysPro.txt`, ligne 155

Et sa table de vecteurs utilisateur a une colonne que j'avais lue sans la voir :

| Adresse | Contenu | **Micro DMA Start Vector** |
|---|---|---|
| `06FCCH` | Vertical Blanking Interrupt | `00BH` |
| `06FD0H` | Interrupt From Z80 | `00CH` |
| **`06FD4H`** | **Timer Interrupt (8 bit timer 0)** | **`010H`** |

## 3. La règle

Le TMP95C061 peut faire servir une interruption par le **micro-DMA** au lieu du CPU.
Quand le canal DMA est armé sur le vecteur d'une source, cette interruption
**déclenche un transfert et ne vectorise PAS le processeur** : l'ISR n'est jamais
appelée, et le hook utilisateur n'a pas à exister.

C'est précisément le mécanisme du **raster scroll** : le timer 0 compte les blancs
horizontaux, et à chaque ligne le micro-DMA recopie la valeur de scroll suivante
depuis une table — sans une seule instruction de CPU.

⇒ **Ce cœur n'a pas de micro-DMA.** Il livre donc INTT0 au CPU, qui tombe sur le
stub du BIOS, qui saute par un vecteur nul, qui arrive à l'adresse 0, qui exécute
le `swi 7` qui s'y trouve — et le gestionnaire de cette trappe **éteint la
console**. Le halt n'a jamais été une attente : c'est le bout d'une chute.

## 4. Ce que le projet savait déjà

* `feedback_hud_raster_split_pattern` : `ngpc_raster_set_scroll_table` — le moteur
  v1 utilise ce mécanisme.
* DEVLOG passe 191 : *« `ldc DMAC0,WA` (`D8 2E 20`) bloquait **19 ROMs** — elles
  programment le micro-DMA au boot. »* On a porté l'instruction qui écrit les
  registres. **On n'a jamais modélisé ce qu'ils font.**

Les registres sont déjà là, dans `cregs[64]` (ABI v2) : `DMAS0..3` (source),
`DMAD0..3` (destination), `DMAC0..3` (compteur), `DMAM0..3` (mode).

## 5. Le chantier

1. **Les registres de vecteur de démarrage** (`DMA0V..DMA3V`) : quel vecteur
   d'interruption arme quel canal. **À localiser dans la datasheet — ne PAS
   deviner** (deux hypothèses ont déjà été réfutées sur ce dossier).
2. **La livraison** : dans `deliver_irq`, si un canal est armé sur le vecteur
   candidat, exécuter le transfert et **ne pas vectoriser le CPU**.
3. **Le transfert** : `DMAM` donne le mode (octet/mot/long, incrément/décrément
   source/destination) ; `DMAC` décompte ; à zéro, l'interruption « fin de
   micro-DMA » (vecteurs utilisateur `0x6FF0..0x6FFC`) est levée.
4. **La porte de preuve reste FONCTIONNELLE** : le nombre de ROMs qui composent une
   image, et les dix qui s'éteignent aujourd'hui. Le corpus a déjà réfuté deux
   intuitions sur ce sujet ; il tranchera celle-ci aussi.

## 6. La leçon, écrite pour qu'elle serve

**Une interruption à 11 880 Hz n'est pas destinée à un CPU.** Le chiffre était là
dès la première trace, et je l'ai regardé trois fois sans le voir — parce que je
cherchais *pourquoi l'interruption n'arrivait pas*, alors que le vrai problème
était qu'elle **arrivait au mauvais destinataire**.


---

# 7. ⭐ LA DOC EXISTE : `01_SDK/docs/MicroDMA.txt`

Un document SDK entier, que personne n'avait ouvert. **Son exemple est, mot pour
mot, ce que fait Fatal Fury** :

```asm
;------------ Routine principale ----------------
    andb  (TRUN),   0b10001110   ; arrête le comptage du timer 0
    ldb   (T01MOD), 0x00         ; timers 0,1 en mode 8 bits
                                 ; horloge du timer 0 = HORLOGE EXTERNE TI0
    ldb   (TREG0),  0x01         ; intervalle = 1  (une ligne)
    orb   (TRUN),   0b00000001   ; démarre le timer 0

    ldl   xwa, 0x8034            ; destination = LE REGISTRE DE SCROLL
    ldc   dmad0, xwa
    ldb   a, 0x08                ; mode : mémoire -> I/O, octet
    ldc   dmam0, a

;---------------- Routine V-int (VBlank) ----------------
    ldl   xwa, data_buffer       ; source = la table de scroll
    ldc   dmas0, xwa
    ldw   wa, 152                ; le nombre de RASTERS
    ldc   dmac0, wa
    ld    (DMA0V), 0x10          ; ⭐ ARME LE CANAL SUR LE VECTEUR DU TIMER 0
```

`0x10` est exactement le « Micro DMA Start Vector » du timer 0 dans la table du
SDK. Le doublement est total : **c'est le raster scroll, et rien d'autre.**

La datasheet TMP95C061 (§3.3.2, « High-Speed DMA ») donne la règle complète :

> *« When the interrupt is generated in the interrupt request source **set by HDMA
> start vector registers**, the interrupt controller sends the HDMA request to the
> CPU **in level 6**, irrelevant to the set interrupt level. […] data is
> automatically transferred from the transfer source address to the transfer
> destination address set in the control register, and the transfer counter is
> decremented. […] if the value in the counter after decrementing is 0, the CPU
> notifies the interrupt controller of the HDMA transfer end interrupt (INTTCn),
> **zero-clears the HDMA start vector register**, disables re-start of the HDMA,
> and ends the HDMA processing. »*

## 8. ⚠️ LE PARADOXE QU'IL FAUDRA RÉSOUDRE — et il est net

Le jeu **arme le canal dans sa routine VBlank**, pas dans son code principal. Mais
il **démarre le timer 0 dans le code principal** (`TRUN <- 0x83` à `0x201279`).

Entre les deux, une ligne s'écoule et le timer 0 déborde. **À cet instant, aucun
canal DMA n'est armé.** Sur ce cœur, l'interruption part donc au CPU, tombe sur le
stub du BIOS, saute par le vecteur nul, et la console s'éteint.

**Sur du vrai matériel, ça ne peut pas arriver.** Donc l'une de ces trois choses
est fausse chez nous, et une trace les tranchera :

1. **`INTT0` est-il seulement DÉMASQUÉ à ce moment ?** Le BIOS écrit
   `INTET01 = 0x0B` (niveau 3) — mais l'ordre des nibbles de ce registre est
   **déjà une hypothèse réfutée** (cf. `machine.hpp`). Il reste à établir *à la
   datasheet*, pas au corpus.
2. **Le timer 0 déborde-t-il vraiment à chaque ligne ?** `TREG0 = 1` avec la source
   TI0. Vérifier ce que compte exactement TI0 sur le NGPC.
3. **Vérification empirique décisive :** le jeu n'écrit JAMAIS `0x10` dans la page
   I/O avant de crasher — mesuré. Donc il n'atteint jamais sa routine VBlank. **Le
   crash précède l'armement.** Il faut trouver ce qui, sur silicium, empêche
   l'interruption de partir pendant cette fenêtre.

**⚠️ NE PAS DEVINER L'ADRESSE DE `DMA0V`.** Elle n'est pas dans le texte du SDK
(la table est une image) ni dans le manuel CPU. L'en-tête `io900h1.h` la place à
`0x100` — **mais pour une AUTRE puce**. Sur le TMP95C061 elle est à trouver dans la
datasheet (PDF `TMP95C061.PDF`, cf. `DOC_SOURCES_INDEX.md` §0 pour son
emplacement ; lire les tables-images avec pymupdf). Trois hypothèses ont déjà été
réfutées par le corpus sur ce dossier.


---

# 9. ✅ CE QUI EST DÉSORMAIS ÉTABLI (datasheet TMP95C061, tables p.178 / 184 / 185)

Les tables sont des **IMAGES** : les lire avec `pymupdf` + rendu PNG (`DOC_SOURCES_INDEX.md` §0).

| Registre | Adresse | Ce qu'il dit |
|---|---|---|
| **`DMA0V..DMA3V`** | **I/O `0x7C..0x7F`** | l'**index de vecteur** armé (0x10 = timer 0) |
| `DMASn / DMADn / DMACn / DMAMn` | **cr** `0x00 / 0x10 / 0x20 / 0x22` **+ 4n** | confirmé par le code du jeu lui-même |
| `DMAM` | `(mode << 2) \| zz` | zz = 0 octet / 1 mot / 2 quatre-octets |
| **`T01MOD`** (`0x24`) | bits 7-6 = mode (00 = 8 bits) · bits 1-0 = **horloge du timer 0 : `00 = TI0 INPUT`** · bits 3-2 = horloge du timer 1 (`00 = TO0TRG`, la cascade) |
| **`TRUN`** (`0x20`) | bit 7 = PRRUN · bits 0..5 = T0RUN..T5RUN |
| **Niveaux d'IRQ** (p.184) | `000` = **Prohibit** · `001..110` = niveaux 1..6 · **`111` = Prohibit** |
| **`INTET01`** (`0x73`) | bits 7-4 = **INTT1** · bits 3-0 = **INTT0** |
| **`INTE0AD`** (`0x70`) | bits 7-4 = **INTAD** · bits 3-0 = **INT0** |
| **`IIMC`** (`0x7B`) | bit 2 = **INT0 input enable** · bit 1 = edge/level · bit 0 = NMIREE |

Et le **K2GE Tech Ref, §4-5-2** :

> *« Hint is different from Vint and is not dependent on the value set in the
> Window Register. It is constant and **152 Hint occur every time**. »*

⇒ TI0 ne pulse que sur les **152 lignes VISIBLES**, pas sur les 198 d'une trame.
Corrigé (ce cœur comptait les 198 : trente pour cent de ticks en trop).

# 10. ⚠️ CE QUI RESTE OUVERT — et pourquoi je ne devine pas

Le micro-DMA est implémenté, correct et sourcé. **Le corpus n'a pas bougé d'un
ROM**, et la raison est mesurée, pas supposée :

**Aucun jeu n'écrit jamais `DMA0V`.** Vérifié en supprimant temporairement la
livraison d'INTT0 : sur 6 millions d'instructions, Fatal Fury et Sonic ne l'arment
**jamais** — et **ils cessent de crasher**.

Donc : le jeu active INTT0 (niveau 3, via le service BIOS `INTLVSET`), démarre le
timer 0 sur TI0 avec `TREG0 = 1`… et n'installe ni handler (`0x6FD4` = 0 pour Fatal
Fury) ni canal DMA. Sur silicium, cette interruption **ne peut pas partir** — sinon
la console s'éteindrait aussi.

**La pièce manquante est là, et elle est étroite.** Trois candidats, à établir *par
lecture*, jamais par intuition (quatre hypothèses ont déjà été réfutées sur ce
dossier) :

1. **Le drapeau de requête d'interruption (`IxxC`)** — ce cœur ne le modélise pas
   du tout. La datasheet en fait un bit réel du registre INTE, positionné par la
   source et effacé à l'acceptation. Est-il *armé* dans cette fenêtre ?
2. **TI0 pulse-t-il seulement ?** Le SDK parle de « l'**interruption** de blanc
   horizontal générée par le contrôleur 2D ». Une *interruption*, donc peut-être
   conditionnée par un bit du K2GE que nous n'avons pas identifié.
3. **`INTLVSET` : quelle source le jeu active-t-il vraiment ?** Le service BIOS
   (`0xFF125E`) écrit dans **DEUX** registres via une table à `0xFF1284`. Décoder
   cette table donnerait la réponse sans la moindre supposition.

---

# 11. ✅ RÉSOLU (passe 208) — et ce n'était PAS le micro-DMA

Le paradoxe de la §10 est tranché, **par une lecture du BIOS**, pas par une intuition.

Le BIOS remplit **les 18 vecteurs UTILISATEUR** (`0x6FB8 + 4n`, SysPro.txt) avec un stub par défaut,
à l'allumage, avant même de démarrer la cartouche :

    FF239D  ld   XIY, 0x00FF23DF     <- le handler par défaut ...
    FF23A2  ld   XIX, 0x00006FB8     <- ... la table ...
    FF23A7  ld   BC, 0x0012          <- ... 18 entrées ...
    FF23AA  ld   (XIX+), XIY
    FF23AD  djnz BC, 0xFF23AA
    FF23DF  reti                     <- **et le stub est un simple RETI.**

**C'est pour ça qu'un jeu survit à une interruption qu'il n'a jamais accrochée.** Fatal Fury active
l'H-blank (INTT0, niveau 3) dès le boot — exactement comme l'exemple du SDK (`8Bit.txt`) — et n'arme
le micro-DMA que sur les écrans qui scrollent un raster (drapeau `0x4DC4`, testé dans SA routine
VBlank, à zéro au boot). Partout ailleurs l'H-int part **152 fois par trame** et atterrit sur ce RETI.

Notre hand-off saute droit à la cartouche sans jouer le code d'allumage du BIOS. La table restait à
**zéro**, le CPU se vectorisait à l'**adresse 0**, y trouvait le `swi 7`, et le gestionnaire d'erreur
du BIOS éteignait la console. **Voilà les dix ROMs « qui dormaient ».**

Le micro-DMA était correct depuis la passe 206. Il n'avait simplement rien à voir avec le crash.

## Et le trou INTTCn est refermé (registres, pas devinette)

La table SFR (datasheet p.23) donne les deux registres manquants, et **la table du BIOS les confirme** :

| Registre | Adresse | Sources |
|---|---|---|
| **`INTETC01`** | **0x79** | INTTC0 (nibble bas) · INTTC1 (haut) |
| **`INTETC23`** | **0x7A** | INTTC2 (nibble bas) · INTTC3 (haut) |

Et `IxxC` (bit 3 / bit 7 de chaque INTE) est le **drapeau de requête** : lire `1` = requête en attente ;
**écrire `0` l'EFFACE**, écrire `1` = *don't care*. C'est pour cela que le BIOS tient un **miroir en RAM**
(`0x6C24..0x6C2B`) : un lire-modifier-écrire sur le vrai registre effacerait la requête de l'autre nibble.
La datasheet l'écrit noir sur blanc : *« Read-modify-write is prohibited »*.
