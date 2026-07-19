# Le mode « K1GE upper palette compatible » — les jeux MONOCHROMES

> **Statut (passe 236) : IMPLÉMENTÉ ET PROUVÉ.** Le chemin K1GE existe
> (`core/renderer.py` : `k1ge_compat_enabled()` + `_k1ge_plane_colors()`), le **trou n°1
> est CLOS par la mesure** (§ 3-bis), et **l'écran du BIOS s'affiche** — texte, cadre et
> papier peint nets : la vérité terrain qu'on ne peut pas truquer.
>
> ✅ **LE TROU N°2 EST CLOS AUSSI (passe 238) — LES 3 CARTOUCHES MONO DESSINENT.**
> La palette 12 bits est bien le **THÈME DE COULEUR** que le BIOS applique aux vieux jeux
> (comme une Game Boy Color teinte un jeu Game Boy), et **c'est la RAMPE DE GRIS** :
>
> ```
> FFF  DDD  BBB  999  777  444  333  000     (répétée sur les 16 entrées, les 4 plans)
> ```
>
> ⛔ **ET J'AVAIS ÉCRIT ICI MÊME : « il n'y a aucune rampe de gris dans la ROM du BIOS
> (cherchée, absente) ».** C'était faux **deux fois** : j'avais cherché une **TABLE**
> d'octets, et **le BIOS la CALCULE**. Il a suffi de **le faire démarrer** avec une
> cartouche mono et de **relire la palette dans la machine**.
> 🔑 **« Je ne l'ai pas trouvée dans la ROM » n'est pas « elle n'existe pas ».** Je
> cherchais la bonne chose **sous la mauvaise FORME** — et j'en avais tiré, en une ligne,
> une conclusion que rien ne portait.

## 1. Le fait qui a lancé tout ça

L'en-tête de cartouche porte un drapeau couleur à l'**octet `0x23`** :

| Valeur | Machine |
|---|---|
| `0x00` | Neo Geo Pocket **monochrome** (K1GE) |
| `0x10` | Neo Geo Pocket **Color** (K2GE) |

Mesuré sur les 73 ROMs du corpus :

```
MONO    (0x23 = 0x00) :   4 ROMs  ->  4 NOIRES   (100 %)
COULEUR (0x23 = 0x10) :  68 ROMs  ->  7 noires   (10 %)
```

Baseball Stars, King of Fighters R-1, Melon-chan no Seichou Nikki, Samurai Shodown! —
**ce sont des jeux NGP monochromes**, écrits avant que la NGPC existe. Notre moteur de
rendu n'a **aucun chemin K1GE** : il lit toujours la palette couleur K2GE.

## 2. Ce qui est établi (sourcé)

### Le registre de MODE — `0x87E2`, bit 7
*K2GETechRef §4-9, Table 10.*

| Bit 7 | Mode |
|---|---|
| `0` | **K2GE color mode** — la valeur après reset |
| `1` | **K1GE upper palette compatible mode** |

Il est **verrouillé** : `0x87F0 ← 0xAA` déverrouille, `0x87F0 ← 0x55` reverrouille.
(« This register is locked and only a priority user may be allowed to change the values. »)

### Qui le pose : le BIOS, depuis l'en-tête — routine `0xFF17C4`

```
FF17C4  cp A, 0x10
FF17C7  jr NC, 0xFF17DE       ; A >= 0x10  ->  COULEUR
FF17C9  ld (0x87F0), 0xAA     ; déverrouille
FF17CE  ld (0x87E2), 0x80     ; MODE = 1  ->  K1GE COMPAT
FF17D3  ld (0x6F95), 0x00     ; le drapeau système "mode couleur"
FF17D8  ld (0x87F0), 0x55     ; reverrouille
FF17DD  ret
                              ; (0xFF17DE = la même chose avec 0x00 / 0x10)
```

**Un jeu COULEUR appelle ce service lui-même** (`VECT_GEMODESET`) — Fatal Fury lui passe
`0x10`. **Un jeu MONO ne l'appelle jamais** : il a été écrit avant. C'est donc le **code
d'allumage du BIOS** qui doit le faire, et **notre hand-off saute ce code**. Mesuré :
`0x87E2` et `0x87F0` restent à zéro sur toute la durée d'un jeu mono.

### Le LUT de palette 3 bits — `0x8100..0x8117`
*K2GETechRef §4-12 / 4-13 / 4-14. Valide UNIQUEMENT en mode compat.*

```
0x8101..0x8103   SPPLT.01..03    sprites,  code de palette 0, couleurs 1..3
0x8105..0x8107   SPPLT.11..13    sprites,  code de palette 1
0x8109..0x810B   SC1PLT.0n       scroll 1, code de palette 0
0x810D..0x810F   SC1PLT.1n       scroll 1, code de palette 1
0x8111..0x8113   SC2PLT.0n       scroll 2, code de palette 0
0x8115..0x8117   SC2PLT.1n       scroll 2, code de palette 1
```

Chaque octet porte un **niveau sur 3 bits** (D2 = MSB, D0 = LSB). **La couleur 0 du
caractère est TRANSPARENTE** (« clear code ») et n'a donc pas d'entrée.

### Les couleurs 12 bits — Table 19

```
0x8380..0x839F   palette couleur du mode compat — SPRITES
0x83A0..0x83BF   palette couleur du mode compat — SCROLL 1
0x83C0..0x83DF   palette couleur du mode compat — SCROLL 2
0x83E0..0x83EF   fond      (les DEUX modes)
0x83F0..0x83FF   fenêtre   (les DEUX modes)
```

Format : 16 bits, `D11-D8 = bleu · D7-D4 = vert · D3-D0 = rouge`. **Accès 16 bits obligatoire.**

## 3-bis. ✅ **LE TROU N°1 EST CLOS — PAR LA DONNÉE** (passe 235)

Faire démarrer le vrai BIOS **le fait dessiner en mode compat K1GE**, et il remplit **le LUT ET la
palette 12 bits**. On a donc, pour la première fois, un jeu de valeurs COHÉRENT à confronter aux
deux lectures possibles.

**Relevé sur le BIOS en cours d'exécution** (`0x87E2 = 0x80`) :

| plan | LUT (`0x8100`) | entrées **NON NULLES** de la palette 12 bits |
|---|---|---|
| SPRITES | pal0 : 7,7,7 · pal1 : **1,6,5** | **7 · 9 · 13 · 14** |
| SCROLL1 | pal0 : 2,3,6 · pal1 : **0,2,6** | **2 · 3 · 6 · 8 · 10 · 14** |
| SCROLL2 | pal0 : 1,2,3 · pal1 : 7,7,7 | 1 · 2 · 3 |

### ⇒ **`index = code_de_palette × 8 + niveau_du_LUT`**

- SPRITES : pal0 → 7 ; pal1 → 8+1=**9**, 8+6=**14**, 8+5=**13**. ⇒ {7, 9, 13, 14} — **exactement les
  4 entrées remplies.**
- SCROLL1 : pal0 → 2, 3, 6 ; pal1 → 8+0=**8**, 8+2=**10**, 8+6=**14**. ⇒ **les 6, toutes.**

**L'autre lecture (index = niveau seul) rendrait les entrées 8..15 INATTEIGNABLES — et le BIOS les
remplit.** Elle est donc réfutée. 🔑 **Ce n'est pas un raisonnement sur ce qui « semble logique » :
c'est la seule lecture sous laquelle toute entrée écrite est atteignable, et aucune ne l'est en vain.**

## 3. ⛔ LES DEUX TROUS — NE PAS LES COMBLER À L'INSTINCT
> *(section d'origine, conservée. **Le trou n°1 est désormais CLOS — voir 3-bis.** Le trou n°2 le
> sera quand le boot BIOS ira jusqu'au lancement de la cartouche : c'est LUI qui pose la palette.)*

1. **Le calcul d'adresse exact (§5-3) est une FIGURE, pas du texte.** Il est absent du
   `.txt` du SDK *et* de `ngpcspec.txt`. Le LUT sort un code sur **3 bits (8 valeurs)**,
   mais la Table 19 alloue **16 entrées par plan**. On ne sait donc pas si l'index final
   est `code`, ou `(code_de_palette, code)`. **Deux lectures possibles, une seule juste.**

2. **Personne ne remplit la palette 12 bits du mode compat.** Un jeu K1GE ne la connaît
   pas — elle n'existait pas sur sa machine. Le BIOS a bien une API pour la poser
   (`0xFF5043`) et une **ombre en RAM à `0x6DD8`** qu'il blitte vers `0x8380` (`0xFF4FE8`),
   mais son initialisation par défaut **la met à ZÉRO** (`0xFF5001`). La rampe de gris par
   défaut doit venir de son **code d'allumage**, que nous ne jouons pas.

**Un moteur de rendu mono écrit sur ces deux inconnues afficherait des pixels parfaitement
confiants et parfaitement faux** — la même faute que `OUT (0xFF)` en passe 209, mais en
pixels. On ne la commet pas.

## 4. ⭐ La vraie leçon : c'est le TROISIÈME mur identique

```
passe 208 : la table des vecteurs UTILISATEUR  -> remplie par le code d'allumage du BIOS  -> on le saute
passe 211 : le registre de MODE 0x87E2         -> posé   par le code d'allumage du BIOS   -> on le saute
passe 211 : la palette du mode compat          -> posée  par le code d'allumage du BIOS   -> on le saute
```

**On synthétise la sortie du BIOS pièce par pièce au lieu de le FAIRE DÉMARRER.** Chaque
fois, ça coûte des ROMs ; chaque fois, la pièce suivante est derrière la même porte.

⇒ **Le prochain chantier est de faire démarrer le vrai BIOS.** Le diagnostic est déjà fait
(passe 208) : lancé depuis son vecteur de reset (`0xFF204A`), il **tourne**, et il **remplit
bien la table des vecteurs** (vérifié : `0x6FD4 = 0xFF23DF`). Puis il se synchronise sur le
VBlank et exécute `ei 5 ; halt` — il attend une interruption de **niveau ≥ 5**, que notre
cœur ne lève jamais. `INTE0AD = 0x0D` programme **INT0 au niveau 5**. C'est là, et c'est
**une seule porte**.

---

## 5. ✅ LA PORTE EST OUVERTE (passe 235, 2026-07-14) — **`INT0` EST LE BOUTON POWER**

### ⛔ D'abord, une CORRECTION : `bios_handoff=False` NE FAISAIT PAS DÉMARRER LE BIOS
`ngpc_reset()` posait **`PC = point d'entrée de la CARTOUCHE`** dans les deux cas. Le drapeau
ne faisait que sauter l'amorçage RAM/registres — **le code de démarrage du BIOS n'a JAMAIS
tourné.** Toutes les mesures « avec le vrai BIOS » de ce projet partaient donc de la
cartouche. Le vrai vecteur de reset est **`0xFFFF00` → `0xFF204A`**.

### 🔑 LE `halt` N'EST PAS UN BUG : C'EST LA CONSOLE ÉTEINTE
Depuis `0xFF204A`, le BIOS s'initialise **correctement** et **choisit** de dormir. C'est son
état **OFF** :

```
0xFF215A  ldb (0x6E), 0x14      ; WDMOD bit 4 = 1  -> le MARQUEUR "je me suis endormi proprement"
0xFF2160  jrl 0xFF1074          ; -> range le matériel
0xFF1112  ei 5                  ; n'accepte plus que le niveau >= 5
0xFF1114  set 2, (0xB3)         ; ARME INT0
0xFF1123  ldw (0xB4), 0x00A0
0xFF1127  halt                  ; et la console est "éteinte"
```

Et le handler d'`INT0` (`0xFF1898`) **acquitte ce même bit** et teste le marqueur :

```
0xFF1898  res 2, (0xB3)         ; acquitte
0xFF189B  bit 4, (0x6E)         ; WDMOD bit 4 -- posé par l'extinction
0xFF189E  jrl Z,  0xFF204A      ; PAS de marqueur -> init froide (qui ré-éteint)
          ...                   ; marqueur présent -> chemin de REPRISE = LE BOOT
```

⇒ **`INT0` = LE BOUTON POWER.** La séquence réelle est : *piles insérées → init → veille* ;
puis **appui sur POWER → INT0 → reprise → démarrage.** Notre BIOS s'endormait **correctement** ;
on ne lui appuyait simplement jamais dessus.

### 🎉 CE QUE ÇA DÉBLOQUE, VÉRIFIÉ
En levant `INT0` (`ngpc_raise_irq(8)`) sur le BIOS endormi :
- il **repart** et exécute son démarrage ;
- il active `INT1`/`INT2` (`INTE12 = 0xDC`) ; **son ISR VBlank tourne** ;
- **il pose `0x87E2 = 0x80`** — *le registre de MODE que ce fichier attendait.*

### 📌 CE QUI RESTE — LA CARTE COMPLÈTE (pour ne rien re-chercher)

**Le moteur du BIOS MARCHE.** Ce n'est pas lui qu'il faut débugger.

```
0xFF35E7  cp (0x64E6), 0x00     ; attend le drapeau de trame (posé par l'ISR VBlank)
0xFF35EE  lda XIZ, (0x6440)     ; TÊTE d'une LISTE CHAÎNÉE de tâches
0xFF35F5  call (XIX)            ; exécute la tâche
0xFF3600  ld IX, (XIZ+6)        ; noeud suivant
0xFF3610  cp (0x64E5), 0xFF     ; la boucle SORT quand ce drapeau vaut 0xFF
```

**La chaîne réelle, relevée en cours d'exécution :**

| nœud | handler | rôle |
|---|---|---|
| `0x6440` | `0xFF36F3` | manette (détection de front sur `0x6F82`) |
| `0x6000` | `0xFF37E1` | ? |
| `0x6040` | `0xFF3984` | ? |
| `0x6080` | `0xFF39C2` | ? |
| `0x60C0` | `0xFF404E` | **machine à états de SCÈNE** (table de sauts `0xFF41C5`, index = octet `XIZ+27`) |
| `0x6480` | `0xFF35D6` | fin (marqueur `FFFF`) |

✅ **LES 6 TÂCHES S'EXÉCUTENT, CHAQUE TRAME** (mesuré : 23 appels chacune sur 23 trames).
La scène courante appelle **`0xFF4130`**, qui calcule une position :

```
E = (XHL+0) + (XIZ+29) - (0x5C02)      ; 0xFF4171
cp E, 0xA0 ; jr NC -> C = 0xFF          ; la scène se TERMINE quand E >= 0xA0
```

### ✅ ET L'ANIMATION TOURNE — ce n'est PAS elle qui bloque
Vérifié : **`(0x5C02)` DESCEND** (`0x44 → 0x40 → 0x3C → 0x38`, écrit par **`0xFF3E6A`**), donc `E`
MONTE et la scène **progresse** vers sa condition de fin. *(J'avais d'abord écrit qu'elle était
figée — c'était faux, et la mesure l'a corrigé avant que ça ne devienne une fausse piste.)*

### ⛔ RÉTRACTATION IMMÉDIATE — « le BIOS n'écrit rien en VRAM » ÉTAIT FAUX
C'était **MON instrument**, pas le BIOS : j'avais armé le journal **sur les mauvaises plages** (et
seulement APRÈS le réveil). En le posant sur **toute** la VRAM (`0x8000..0xBFFF`) **avant** le
réveil : **335 083 écritures**, dont l'immense majorité en **`0x8800` — L'OAM.**

🔑 **LE BIOS DESSINE AVEC DES SPRITES, PAS AVEC DES TILEMAPS.** Je regardais là où il n'écrit pas.
*(Troisième fois que le journal d'écritures « prouve une absence » qui n'existe pas — après
`store()` et `z80_write()`. Les deux premières fois il ne POUVAIT pas se déclencher ; cette fois
c'est moi qui l'ai mal visé. **Même conclusion : valider l'instrument sur une région qui DOIT
changer, avant de conclure quoi que ce soit.**)*

### 🔴 LE FAIT QUI RESTE, ET C'EST LE SEUL
Le BIOS dessine, mais **notre écran reste uni** — parce qu'il dessine **en mode compat K1GE**, que
notre renderer **n'implémente pas**. La boucle se referme : *pour voir le BIOS, il faut le chemin
K1GE ; pour connaître le chemin K1GE, il fallait faire tourner le BIOS.* **La deuxième moitié est
faite** (voir 3-bis : le trou n°1 est clos par la donnée).

⭐ **REPRENDRE :**
1. **Implémenter le chemin K1GE** (l'index est connu : `palette × 8 + niveau`) et vérifier qu'on
   voit enfin l'écran du BIOS — **c'est le test de bout en bout**, avec une vérité terrain (le logo).
2. Puis finir le boot : le BIOS ne lance toujours pas la cartouche (`0xFF31EA` lit `(0x20001C)` ;
   appelé depuis `0xFF3188` / `0xFF3192`, derrière les drapeaux `0x6C55`/`0x6C58`/`0x6C59`/`0x6C5B`).
⚠️ **Le trou n°2 (la palette compat par défaut d'un jeu MONO) N'EST PAS clos** : les jeux K1GE
écrivent bien le LUT 3 bits (mesuré : KOF R-1 pose `00 03 05 07`) mais **ignorent la palette 12
bits** — c'est le BIOS qui doit la poser au lancement. **Ne pas inventer une rampe de gris.**
