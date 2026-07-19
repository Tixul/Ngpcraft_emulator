# La ROM de calibration — arrêter de régler à l'oreille

> **Statut : À FAIRE. C'est le point de reprise.**
> Idée de l'user, 2026-07-13, après avoir corrigé le tempo à l'oreille :
> *« on prendra un son connu, j'en ai, compilé avec toolchain officiel, et tu ajoutes
> un debug de mesure, et puis on arrête de batailler et on a les bons réglages. »*

## 1. Pourquoi

Deux constantes du cœur sont **réglées à l'oreille**, pas mesurées. Elles sont
consignées comme telles, et elles ne doivent pas rester comme ça :

| Constante | Où | Valeur actuelle | Statut |
|---|---|---|---|
| **Le tap du prescaler** (`φT1`) | `cpp/src/machine.hpp` → `timer_base` | **512 cycles** | 🎧 oreille |
| **L'horloge du T6W28** | `cpp/src/apu.hpp` → `kApuClockHz` | **3 072 000 Hz** | 🎧 déduit, octave indécidable |

Les documents ne peuvent pas les trancher :

* Le **tap** : la datasheet Toshiba et le SDK SNK **nomment les taps différemment**
  et se contredisent d'un facteur 4 à 32. (Ce qui *est* tranché en dur : le
  générateur de baud tourne sur le même prescaler, et `BR0CR = 0x05` + 19 200 bps
  documentés donnent `φT0 = fc/4` avec `fc = 6,144 MHz`. L'**échelle** des taps est
  donc sûre ; seul **quel cran** le champ de mode sélectionne reste inconnu.)
* L'**octave** : `f = horloge / (32n)`. À 3,072 MHz et à 6,144 MHz, les mêmes notes
  tombent sur les mêmes notes, une octave d'écart. **La hauteur ne peut pas arbitrer.**

## 2. ⭐ Le point clé : la ROM peut SE MESURER ELLE-MÊME

On n'a pas besoin d'oreilles ni d'oscilloscope pour le **tap**. La console porte déjà
une horloge de référence **exacte et indiscutable : le VBlank, à 60 Hz** (198 lignes
× 517 cycles, verrouillé par le K2GE).

Donc :

```
    compteur = 0
    dans l'ISR de INTT3 :  compteur++
    dans l'ISR de VBlank :  si (60 VBlanks écoulés) { afficher compteur ; compteur = 0 }
```

**Le nombre affiché EST le tap.** Avec `TREG3 = 98` :

| φT1 réel | INTT3 par seconde | ce que la ROM affichera |
|---|---|---|
| 128 cycles | 490 | **490** |
| 256 cycles | 245 | **245** |
| **512 cycles** | **122** | **122** ← ce que l'oreille prédit |
| 1024 cycles | 61 | **61** |

On flashe, on lit le nombre à l'écran, **et c'est fini.** Pas d'oreille, pas
d'interprétation, pas de « ça sonne mieux ». Un entier.

⚠️ **Et le même chiffre, lu dans notre émulateur, doit être identique.** La ROM est
donc aussi un test de non-régression : `tests/test_hw_calibration.py` la fait tourner
et compare.

## 3. La partie SON (l'octave) — un son CONNU

Pour l'horloge du T6W28, la console ne peut pas s'auto-mesurer : il faut sortir le
signal. Mais c'est simple :

1. La ROM joue **une note unique, tenue, de période connue** (on écrit `n` en dur —
   par exemple `n = 254`, ce qui donne un LA à 3,072 MHz).
2. On enregistre la sortie casque de la vraie console.
3. On mesure la fréquence du fichier (FFT, ou juste un compteur de passages à zéro).

| horloge du chip | fréquence attendue pour `n = 254` |
|---|---|
| **3 072 000 Hz** | **378 Hz** |
| 6 144 000 Hz | 756 Hz |
| 3 579 545 Hz (SN76489) | 440 Hz |

**Un facteur 2 s'entend et se mesure sans ambiguïté.** Une seule prise règle l'octave
pour de bon.

L'user précise qu'il a **des sons connus, compilés avec la toolchain officielle** —
autant s'en servir : jouer un son de référence dont on connaît la partition permet de
vérifier **le tempo ET la hauteur d'un coup**, sur la vraie machine.

## 4. Ce que la ROM doit contenir

Compilée avec la **toolchain officielle** (`cc900` / `asm900`), pas la nôtre — c'est
un instrument de mesure, il ne doit rien devoir à notre chaîne :

1. **Écran 1 — le compteur de timer.** `TREG3 = 98`, timer 3 en `φT1` (exactement ce
   que font Sonic et Metal Slug), un compteur d'INTT3, remis à zéro tous les
   60 VBlanks, affiché en décimal.
   → **donne le tap directement.**
2. **Écran 2 — le balayage des taps.** Le même compteur pour chaque valeur du champ
   de mode (01, 10, 11), affichés côte à côte.
   → **donne toute la table d'horloges d'un coup**, pas juste un cran.
3. **Écran 3 — la note de référence.** Une note tenue de période connue, à écouter et
   à enregistrer. Afficher `n` à l'écran pour qu'aucune confusion ne soit possible.
4. **Écran 4 — le son connu de l'user**, joué par le pilote son officiel : la preuve
   d'ensemble, tempo + hauteur.

## 4-bis. ⭐ Écran 5 — **COMBIEN DE SCANLINES DANS UNE TRAME ? 198 OU 199 ?**

**Question ouverte, et elle ne se tranche PAS par un émulateur tiers.**

`RAS.V` (`0x8009`) est **lisible par le programme**. Il suffit donc de :

1. boucler en lisant `RAS.V` pendant plusieurs trames,
2. retenir sa **valeur MAXIMALE** avant qu'il ne se replie à 0,
3. **l'afficher**.

    la ROM affiche 197  ->  la trame fait 198 lignes (0..197)
    la ROM affiche 198  ->  la trame fait 199 lignes (0..198)

**On flashe, on lit un entier, c'est fini.**

### Pourquoi la doc ne suffit pas
Le K2GE Tech Ref (§6, note sous la figure de timing) dit :
> *« H_INT signal is not generated at line 151 and signal generation for the 0th line occurs at the
> **beginning of line 198** »*

La phrase est **AMBIGUË** : « la ligne 198 » peut désigner la 198ᵉ ligne (indice 197, donc **198 lignes**)
ou l'indice 198 (donc **199 lignes**). Notre cœur a choisi **198** ; un émulateur tiers compte **199**.

⛔ **Cet émulateur n'est PAS une source.** Qu'il compte 199 ne prouve rien — c'est du triage, pas un verdict
(cf. le nombre de fois où il s'est trompé : cycles de `JR`, IRQ de sources désactivées, rendu monochrome par
défaut…). ⚠️ Et l'argument « 199 × 515 donne 59,95 Hz, c'est plus proche de 60 » est une **INFÉRENCE bâtie sur
un 60,00 Hz supposé** — exactement la faute qui avait produit le faux `517`. **On mesure.**

### Ce que ça change
La longueur de trame décide du **budget CPU par trame** : sur les trames où un jeu déborde (chargement de
niveau), 0,5 % de cycles en plus ou en moins déplacent le travail accompli. C'est mesurable, donc c'est à
mesurer.

## 4-ter. ⭐ Écran 6 — **LES QUESTIONS HW OUVERTES DU CPU** (elles se flashent au même endroit)

`specs/TLCS900_MEMORY_FAMILY.md` porte une section **« OPEN HARDWARE QUESTIONS »** : ni l'assembleur ni un
désassembleur ne peuvent y répondre (**un oracle d'encodage ne révèle pas une sémantique d'exécution**). Elles
demandent du **silicium** — donc cette ROM. Autant les poser toutes d'un coup.

### Q1 — l'index registre de `(r32 + r8)` / `(r32 + r16)` est-il **SIGNÉ** ?
```
ld XIX, base ;  ld A, 0xFF ;  ld (XIX+A), B     ->  l'octet atterrit en base+0xFF  ou en base-1 ?
```
**Les 2 cœurs étendent le signe.** Si le HW étend par zéro, **toute table indexée avec un index ≥ 0x80 lit
256 octets à côté**. ⚠️ **Rencontré en vrai** : Densha de Go! 2 exécute `ld XIY,(XIY + WA)` puis `jp (XIY)` —
un saut par table. (Dans SON cas l'index est de toute façon absurde, donc ce n'est pas la cause de son crash ;
mais l'instruction est courante et la question est réelle.)

### Q2 — que contient `dst` après une **division par zéro** ou un **dépassement de quotient** ?
```
ld WA, 0x1234 ;  ld B, 0 ;  div WA, B     ->  afficher WA
```
Le manuel définit le **drapeau** et se tait sur la destination. Le cœur natif garde la moitié basse ; la
référence Python **refuse de deviner**. **Une seule mesure clôt le débat.**

⇒ **Même méthode que le reste de cette ROM : on flashe, on lit un entier à l'écran, c'est fini.**

## 5. Ce que ça règle définitivement

* ✅ le **tap du prescaler** (donc le tempo de TOUS les jeux) — par un entier lu à l'écran ;
* ✅ **toute la table** des horloges de timer, pas juste un cran ;
* ✅ l'**octave** du T6W28 — par une prise son ;
* ✅ **198 ou 199 SCANLINES** — par la valeur max de `RAS.V`, lue et affichée par la ROM elle-même ;
* ✅ les **2 questions HW ouvertes du CPU** (index signé ? destination d'une division par zéro ?) — que
  **ni l'assembleur ni le désassembleur ne peuvent trancher** : seul le silicium le peut ;
* ✅ et ça devient un **test de non-régression** que l'émulateur doit reproduire au chiffre près.

**Après ça, on ne bataille plus : on mesure.**
