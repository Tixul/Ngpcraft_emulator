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

## 9bis. Le levier principal : les wait-states cartouche

C'est ici que se joue l'essentiel des §1-9, donc ca n'est pas optionnel.

**La flash cartouche est lente, et chaque instruction est allee la chercher.** Sur
silicium, un fetch coute des wait-states par octet. Sans ce cout, du code cartouche
tourne **~2,9 a 3,4x trop vite** : les jeux verrouilles au VBlank (Fatal Fury) n'en
montrent rien, mais les jeux **auto-cadences** (Cool Boarders, Densha de Go) font
tenir leur travail dans un seul VBlank et affichent 60 fps la ou la console en donne
30 — exactement le "embellissement" que le §2 interdit.

Valeurs **mesurees** (ROMs dans `hw_calibration/`, jamais reglees a l'oreille) :

| Reglage | Valeur livree | Preuve |
|---|---|---|
| `cart_wait` — cycles par octet de **fetch** | **3** | `cpu_calib_v1` : classes fetch-bound ~3,4x trop rapides, MUL/DIV ~2,5x, raster juste |
| `cart_data_wait` — cycles par octet **lu en donnee** | **0** | `cpu_calib_v2` : lecture cartouche == lecture RAM (252 == 252). Une valeur de 5, calee sur un framerate, a ete **refutee** par cette ROM |
| `ldir_cost` — cycles par **iteration** du `LDIR`/`LDDR` **octet** | **14** | fait tomber Cool Boarders a ses 30 fps sans toucher Fatal Fury. Fortement etaye, ROM de mesure (`v6`) encore ouverte |
| `ldirw_cost` — cycles par **iteration** du `LDIRW`/`LDDRW` **mot** | **18** | le copieur raster en boucle ouverte HiColor de reference (un homebrew de 2004) : chaque bloc doit couter exactement 8 lignes (8 × 515 = 4120 cycles) ou l'image cisaille. A 14 le bloc tombe a 0,793× et l'ecran est illisible ; a 18 la trame est **pixel-identique** au chemin auto-synchronise de la meme ROM ; a 17 → 83 % des pixels, a 19 → 4 %. Fenetre d'**un cycle**, ce qui en fait un meilleur instrument qu'une moyenne de framerate |
| `vram_wait` — ecriture VRAM | **0 (off)** | l'effet est reel (`cpu_calib_v3` : VWR 452 < MEM 471) mais le cout/octet n'est pas fixe : on ne livre pas un entier devine |

⚖️ **`ldir` et `ldirw` sont deux instructions, pas un reglage a deux noms.** Le cout est
paye **par iteration**, et une iteration du `LDIRW` deplace **deux** octets : lui facturer
le chiffre de la forme octet vend la copie mot a moitie prix. Cool Boarders, qui a fixe le
14, utilise la forme **octet** — cette mesure n'a jamais rien contraint sur la forme mot, et
un seul champ ne pouvait pas porter les deux reponses. `ldirw_cost = 0` signifie « suivre
`ldir_cost` », c'est-a-dire le comportement d'avant, pour tout appelant qui ne demande rien.

⛔ L'autre facon de combler le meme ecart — `cart_data_wait=2`, sur l'idee que la source de
la copie est de la flash cartouche lente — est **refutee** par `cpu_calib_v2` sur silicium
(CRND == RRND) : rejouee ici, elle fait tomber CRND a 252 sous RRND 255. Ne pas la ressusciter.

### ⚠️ Deux defauts differents, et c'est voulu

- **`Machine` (le champ C++) demarre a 0** = fetch gratuit. C'est de la
  **retro-compatibilite**, pas une affirmation sur le materiel : quand la
  fonctionnalite est arrivee, le champ a ete laisse eteint pour que le timing
  existant reste identique au bit pres et que le chemin chaud ne coute rien.
- **L'application les allume** a chaque chargement de ROM (`cart_wait_states()`
  dans `ngpc_settings.py`). Le CLI headless `ngpc_native.py` aussi (`--timing
  silicon`, le defaut).

⇒ **Tout code qui construit une `Machine` lui-meme** (banc de mesure, test, serveur
MCP) tourne en fetch gratuit tant qu'il n'appelle pas les setters, et mesure une
machine ~2,9x trop rapide. Copier les trois appels de `core/romcheck.py`.

### La consequence qui trompe le plus

Le fetch gratuit ne fait pas qu'accelerer uniformement : **il rend invisible tout
gain de taille de code**. Une optimisation qui ne fait que raccourcir le code mesure
**exactement zero**, parce que ce qu'elle economise est la seule chose non facturee.
Sur silicium chaque octet d'encodage coute 3 ticks — la taille du code *est* de la
vitesse, et c'est aussi pourquoi aligner une struct sur une puissance de deux peut
couter (les offsets sortent du deplacement 8 bits, l'encodage grossit).

Si une optimisation plausible mesure zero, **suspecter le modele de timing avant de
conclure qu'elle ne sert a rien.**

---

## 9ter. LE MODELE DE TEMPS, RECONSTRUIT ET LIVRE (2026-08-21)

> ⚠️ **HISTORIQUE depuis le 2026-08-27.** Deux des neuf pieces decrites ici
> (l'attente de fetch, et l'absence de cout pour la branche et pour les acces
> de donnee) etaient AJUSTEES et ont ete remplacees par des mesures directes.
> **L'etat courant du modele est le §9quater.** Cette section reste pour la
> genese et pour les mesures serie/rejeu qu'elle documente.

⚠️ **Le §9bis ci-dessus decrit l'ancien modele.** Il reste vrai sur le fond -- sans
wait-states le code cartouche vole -- mais ses NOMBRES sont perimes, et `cart_wait` a
change de sens. Ce qui suit remplace sa partie chiffree.

### Un seul appel

`ngpc_set_timing_silicon(word_wait, bios_wait)`. **Ne jamais rearmer les reglages un par
un** : ce sont huit choses qui doivent s'accorder, et la meme erreur s'est produite TROIS
fois le 21/08 -- `Shell._begin_mirror`, `test_netplay_mirror`, et le helper `silicon()` du
banc de la sonde. Dans les trois cas une moitie du modele etait armee et l'autre non, ce
qui donne une machine que personne n'a. **Appeler ce que le shell appelle.**

### Les neuf pieces

| piece | provenance |
|---|---|
| etats Toshiba **x2** | manuel CPU 900/L1 ; fiche TMP95C061B (un etat = 80 ns a 25 MHz, `tosc` = 40 ns) |
| fetch par **mot 16 bits** | le bus externe est 16 bits |
| fetch **pipeline** (dette du BIU) | fiche 3.3.1 : *l'unite d'execution et l'unite de bus fonctionnent independamment* |
| avance du BIU = **2 x cout REEL du mot** | file de **4 octets**, dite trois fois dans le manuel 900/L1. ⚠️ le cout reel, pas le parametre entier -- l'y avoir laisse a coute 2 lots au banc v8 |
| entree d'interruption **x2** | 18 **etats** (3.3.1 + annexe B table (11)) |
| le **BIOS paie le meme bus** | il est sur le meme bus 16 bits |
| **transmetteur a deux etages** | fiche 3.11 : `SC0BUF` et le registre a decalage sont distincts |
| retrait du **double comptage** a la reception | defaut reel, condition de sortie remplie |
| **la file ne precharge PAS a travers une copie de bloc** | une copie repetee tient le bus (une lecture + une ecriture par iteration) : aucun creneau pour precharger derriere elle. ⚖️ **MESURE** sur le copieur raster en boucle ouverte HiColor de reference, qui doit depenser exactement 8 lignes par bloc : 4086 cy/bloc sans (0,9917x, image cassee), **4134 avec (1,0034x, image propre)**. Les 11 ROM de calibration : framebuffer IDENTIQUE AU BIT avec et sans. Voir DEVLOG 2026-08-25. |
| `word = 8,25` (`fetch_wait_q4 = 33`), `bios = 8` | ⚠️ **les deux seuls chiffres CALIBRES**, et le premier a ete RECALE sur silicium le 23/08 (ROM v8) : il valait 10, l'entier ne peut pas encadrer la console. Voir plus bas. |

### Contre le silicium (tir du 21/08, recoupe en interne)

| | silicium | modele |
|---|---|---|
| aller-retour x3 | 1218/1220/1217 | 1215/1216/1216 (**−0,2 %**) |
| cout CPU d'un octet recu | 96,2 µs | 95,7 (**−0,5 %**) |
| debit sature | 3963 | 4006 (**+1,1 %**) |
| `QUIET` | 6457 | 6912 (**+7 %**, ouvert) |

### ⛔ L'etat de timing EST de l'etat

`biu_debt` et les etages de tampon sont de l'etat machine : ils doivent etre remis a zero
au reset **et** sauvegardes. Les oublier a casse le determinisme du rejeu (barriere
libretro : *non-deterministic state after replay*). `ngpc_link_state_t` est passe en
**version 2** pour les porter ; les savestates anterieurs sont **refuses**, pas mal lus.

**Tout ce qui est ajoute a `Machine` et influence le temps doit suivre la meme regle.**

---

### ⚖️ DEFAUT SUR LES TROIS FACADES, ET RECALE SUR SILICIUM LE 23/08/2026

`--timing legacy` (ou `NGPCRAFT_TIMING=legacy`) garde l'ancien modele, pour attribuer une
regression en secondes au lieu d'en discuter. ⚠️ Le shell **refuse** une valeur inconnue :
le defaut etant le silicium, une faute de frappe le donnerait en silence et on croirait
comparer deux machines alors qu'on en mesure une seule.

#### Les deux nombres calibres ont CHANGE — et c'est un tir sur console qui l'a impose

ROM `hw_calibration/a_irq_calib_v8.ngp` (md5 `334e5cb56e26fe78194d913cee4029a3`), la
premiere a mesurer le **cout d'une interruption** : les sept precedentes ne mesurent que
du code sans interruptions.

| | WORK0 | WORK1 | WORK4 | cout d'une IRQ |
|---|---|---|---|---|
| **SILICIUM** | 261 | 218 | 249 | **111 cycles** |
| ce modele | 260 | 218 | 250 | 113 |
| ancien timing | 263 | 240 | 258 | **59** |

- ✅ **l'entree en interruption a 36 cycles (18 etats x 2) est CONFIRMEE** ; l'ancien
  timing sous-facture de moitie, ce qui deplace tous les splits rasteurs ;
- ⚡ **l'attente de lecture vaut 8,25 cycles par mot**, pas 10. L'entier ne peut pas
  encadrer le silicium (mot=9 -> 238 lots, mot=8 -> 269, console **261**). Arme par
  `fetch_wait_q4 = 33` ;
- ⚡ **`biu_slack` suit le cout REEL du mot** (2 x 8,25), plus le parametre entier. Cette
  seconde correction a amene `WORK1` de 220 a **218 pile**.

⛔ **Un quart de cycle avait deja ete essaye la veille (9,75) puis retire** parce qu'il
aggravait un jeu. C'etait un reglage a l'oreille ; celui-ci vient d'un tir avec RASV=198 et
des chiffres stables. **La difference n'est pas la valeur, c'est d'ou elle vient.**

⚠️ **ET IL A FALLU LE RENDRE SANS ETAT.** Le quart etait obtenu en REPORTANT une retenue
d'une instruction a l'autre -- donc de l'etat, qu'un chemin lisant hors du pas
d'instruction decalait, et le test de rejeu libretro est tombe dessus immediatement. Le
motif vient desormais de l'ADRESSE : sur quatre mots consecutifs, `q4 & 3` coutent un
cycle de plus. Meme moyenne, reproductible, aucune retenue a sauver.

#### Le serie, contre la campagne AUTO du 21/08

Allers-retours **+0,3 / +0,2 / −0,2 %**, part du temps passee a recevoir **18,32 % contre
18,20 %**. ⛔ L'« ecart serie » poursuivi pendant des heures n'existait pas : c'etait
`verify_bench` qui pilotait mal la ROM sonde (roles imposes alors qu'AUTO les elit, et
appui simultane que la ROM refuse). La batterie compare desormais AUTO **directement au
silicium** plutot qu'a des references d'emulateur -- ⚡ **une reference d'emulateur ne
mesure pas la fidelite, elle detecte le mouvement.**

#### Ce qui a bloque l'armement sur les facades

`libretro_smoke_external_bios_priority` : « non-deterministic state after replay ».
Cause -- **`biu_debt` etait EFFACE a la restauration**. Effacer un etat a la restauration
parait prudent et ne l'est pas : la premiere passe continue avec sa valeur vivante, le
rejeu repart de zero, et les deux divergent. ⇒ il voyage maintenant DANS le bloc aux (a la
place de `_pad2`, taille inchangee, aucun savestate invalide).
🚨 **Tout etat qui influence le temps se SERIALISE.**

#### Validation

corpus A/B 83 ROMs / **0 perte soutenue** · suite **2108 passed** · banc du cable
**0 echec** · libretro 5/5 · deux APK · coeurs en phase.

⛔ **Non prouve** : le glitch KOF d'origine n'a jamais ete reproduit sans tete, ni sous
l'ancien timing ni sous le modele. On ne peut donc pas ecrire « repare » -- seulement
« introuvable, et les deux timings rendent 572 trames identiques au pixel avec du
mouvement ».

---

## 9quater. LE MODELE MESURE — campagne v13/v14/v15 (2026-08-27)

> ⚠️ **Etat INTERMEDIAIRE.** Trois pieces decrites ici ont ete corrigees le meme
> jour par la campagne v16/v17/v18 : la surcharge de branche est desormais
> **cartouche uniquement**, l'acces de donnee est facture **par acces** et non par
> octet, et un bug d'entree en interruption a ete trouve. **L'etat courant est le
> §9quinquies.** Cette section reste pour la genese et pour ses deux regles, qui
> valent toujours.

Le §9ter decrivait un modele dont **deux pieces sur neuf** etaient des nombres AJUSTES :
l'attente de fetch (8,25 cy/mot, calee pour encadrer la ROM v8) et l'absence de cout pour
la branche prise et pour les acces de donnee. Trois ROM neuves les ont remplacees par des
mesures directes.

**Ecart moyen aux 26 cases silicium du corpus : 4,78 % -> 0,67 %. Pire cas : 12,31 % -> 4,59 %.**
Banc : `hw_calibration/corpus_gate.py` (v2, v10, v11, v12, v8).

### Les cinq pieces qui ont change

| piece | avant | maintenant | mesure |
|---|---|---|---|
| fetch | 8,25 cy / **mot** | **4,00 cy / OCTET** (`fetch_wait_byte_q16 = 64`) | v14 p.1 : 4,03 cy/o., droite a 0,35 % |
| branche prise | non facturee | **+4 cy** (`branch_taken_extra`) | v14 rotation C : 16,3 vs 11,3 cy/br. |
| acces de donnee | **0** (hypothese) | **4 cy par ACCES** (`data_access_cycles`) | v15 p.1-2 : ~4,05 cy, lecture = ecriture |
| `mul` octet | 15 etats | **12 etats** | v14 p.4 : 30,43 cy, droite a 0,48 % |
| `div` octet | 36 cycles | **32 cycles** | v14 p.3 : 38,45 cy, droite a 0,59 % |

### ⚡ POURQUOI PAR OCTET, ET CE QUE CA A REPARE

Le bus cartouche est **8 bits** (AM8/16 bonde haut) : le processeur va chercher UN OCTET
par cycle de bus. Facturer **par mot** -- une seule charge par adresse paire -- faisait
dependre le prix d'une instruction de sa **PARITE** : 5 octets payaient 3 charges en
partant d'une adresse paire, 2 en partant d'une impaire. **50 % d'ecart pour la meme
instruction.**

C'etait la **racine commune de deux anomalies** qu'on croyait sans rapport :
- la sensibilite a l'adresse (ROM v12 : silicium `682/682/683/682`, nous `715/733/732/732`,
  maintenant `675/681/681/681`) ;
- l'ordre des rotations de la v14 (silicium : A la plus lente ; nous : B).

⇒ **Quand deux anomalies resistent separement, chercher le mecanisme qui produit les deux.**

### ⛔ REGLE 1 — deux corrections qui se compensent ne se corrigent pas separement

Le fetch par octet **seul** DEGRADE le corpus : 4,78 % -> **7,13 %**. Les 8,25 cy/mot
sur-facturaient le bus **pour compenser la branche qu'on ne facturait pas**. Ensemble :
1,30 %, puis 0,67 % avec l'acces de donnee.

⚡ **Un nombre ajuste compense souvent l'absence d'un autre. Corriger l'un sans l'autre
empire le resultat -- et ressemble alors a une refutation de la correction.**

C'est aussi ce qui a debloque `MUL`/`DIV` : releves en juillet sous un fetch a 10 cy/mot,
puis ramenes « dans la bande » d'un +5 % commun faute de mieux (§9ter). Le biais disparu,
viser l'exactitude n'est plus masquer un biais, et les deux tombent **juste au-dessus du
plancher datasheet** (12 etats contre 11, 16 contre 15) -- coherent avec « latence
variable, un peu plus lente que la table ».

### ⛔⛔ REGLE 2 — UN COUT MESURE CONTIENT DEJA SON TRAFIC

**C'est la regle qui a casse le HUD de Cool Boarders, et elle est absolue.**

`data_access_cycles` est facture dans `load_sized` et `store`, c'est-a-dire par ACCES. Or
deux chemins passent par la alors que leur prix est **deja** mesure ou tabule :

- le **transfert bloc** : `ldir_cost` (14) et `ldirw_cost` (18) ne viennent pas d'une table,
  ils ont ete mesures par iteration contre la tranche de 8 lignes du copieur HiColor de reference
  (8 x 515 = 4120 cycles). Les facturer en plus coutait **1792 cycles sur 4120** (+43 %) ;
- l'**entree en interruption** : les quatre valeurs de Toshiba (28/24/22/18 etats) sont
  indexees sur **la largeur de bus de la ZONE DE PILE** -- elles contiennent donc deja les
  ecritures de PC et SR.

⇒ **N'ajouter un cout par acces qu'aux instructions dont le prix vient d'une TABLE.**
Les deux exceptions sont gardees explicitement dans le code (`mem_family.cpp`, sauvegarde
et restauration de `data_wait_cycles` ; `core.cpp`, remise a zero a la livraison d'IRQ).

### ⚠️ Un acces de donnee ne se recouvre PAS avec le prefetch

`access_wait` est du temps de BUS que la file peut recouvrir : le processeur ne cale que si
la file s'est videe. Un acces OPERANDE occupe le bus et retarde **les deux**. Verse dans
`access_wait`, le cout d'une lecture etait tout simplement **avale par le credit d'avance**
-- passer de 0 a 1 cy/octet ne deplacait la page 2 de la v14 que de 12,05 a 12,21 au lieu
des ~16 attendus. D'ou l'accumulateur separe `data_wait_cycles`, ajoute directement aux
cycles de l'instruction.

### ✅ Ce que le silicium a CONFIRME de notre modele

- **lecture et ecriture coutent pareil** (ecart <= 0,5 compte sur les quatre paires v15) ;
- **la largeur d'un acces ne joue que par ses etats** (4/4/6), pas par ses octets ;
- l'entree en interruption a **18 etats** reste la bonne des quatre valeurs documentees.

### ⛔ Ce qui reste ajuste, et doit etre traite comme tel

`branch_taken_extra = 4` est le seul nombre de ce tableau qui vient d'un **optimum de
corpus** autant que d'une mesure (v14 rotation C donne 5,0 cy). Il porte donc encore ce que
les pieces voisines n'expliquent pas -- en particulier `biu_slack` (voir `OPEN_ITEMS.md`).
Ne pas le bouger seul.

## 9septies. ✅ ETAT COURANT (tirs v20 et v21, 2026-08-30) — **LIRE CECI EN PREMIER**

> §9sexies (29/08) et §9quinquies (27/08) restent pour la genese. Leurs chiffres et deux
> de leurs conclusions sont **depasses**.

| | valeur | contre le silicium |
|---|---|---|
| corpus, 26 cases | **0,18 %** moyen, **0,77 %** pire — **0 case > 1 %** | — |
| chemin d'une interruption | **110,0 cy** | annexe B **110**, mesure **111,5** |
| `ldir` / `ldirw` RAM→RAM | 14,09 / 14,05 cy/iteration | **14,12 / 14,16** |
| `ldirw` ROM→VRAM | 18,05 | **18,16** |
| etranglement VRAM | 2,9 cy **par acces** | **2,74** (v3) et **2,95** (v20) |
| suite | **2115 verts** | — |

### Ce que les tirs v20 et v21 ont change

1. ⛔ **La « latence variable » de la division est REFUTEE** (v20 p2). Trois divisions aux
   operandes tres differentes : **87,07 / 87,09 / 87,09**, etendue **0,02 cy**. Une
   constante unique existe -- elle vaut **58**, pas 56. Corpus 0,31 % -> **0,18 %**,
   pire 1,89 % -> **0,77 %**.
   ⚖️ Cette hypothese avait ete posee la veille pour reconcilier trois autorites
   contradictoires (annexe B 23 etats / v17 52 / corpus 56). Elle a tenu 24 h et coutait
   1,7 % de corpus. **Une hypothese posee pour reconcilier des mesures DOIT etre testee.**
2. ⛔ **L'etranglement VRAM se paie PAR ACCES, pas par octet** (v20 p3, rapport mot/octet
   **1,00** exact). ⚖️ La v3 ne pouvait pas trancher : elle n'ecrit que des octets, ou les
   deux formes coincident. Il fallait une **double difference** contre les memes ecritures
   en RAM. Valeur **10**, qui equilibre les deux tirs.
3. ⚖️ **`ldirw_cost = 18` n'etait pas faux, il etait MAL ATTRIBUE** (v21). Le meme `ldirw`
   coute **14,04** cy/iteration en RAM->RAM (l'annexe B au centieme) et **18,16** en
   ROM->RAM : c'est la **SOURCE** en cartouche qui coute +4,12, pas la destination VRAM
   (+0,08). Cale sur le copieur ROM->VRAM HiColor de reference, notre 18 portait 14 + 4 et etait
   applique a TOUS les transferts ⇒ **29 % trop cher** sur toute copie RAM->RAM ou
   RAM->VRAM, la plupart de celles que font les jeux.
   ⇒ `ldirw_cost` **18 -> 14** + `block_cart_src_per_byte = 2`.
4. ✅ **La doc a ferme quatre lignes sans tir** : `ldir_cost = 14` **EST** l'annexe B
   (7 etats x 2, on lisait des ETATS comme des CYCLES) ; le terme constant valait
   `+1 ETAT` = **+2 cycles** (nous chargions +1) ; l'entree d'IRQ est une valeur **unique**
   de 18 etats ; `MUL`/`DIV` a 11/14 et 15/23 sont des **planchers**.

### ⛔ Resultats a ne PAS rejouer

- la **ristourne** (une IRQ rendrait le code interrompu moins cher) : refutee (v19) ;
- **`data_access_cycles` uniforme** : il ne se paie qu'en code **cartouche** ;
- **la destination VRAM sur un transfert bloc** : elle ne coute **rien** (v21).

### ⏳ Ce qui reste, du plus mur au plus flou

> ⚠️ `OPEN_ITEMS.md` et `DEVLOG.md` sont **gitignores** (locaux). Cette liste est leur
> copie dans un fichier SUIVI, pour que le chantier reste lisible sans eux.

1. **Le cluster a -0,8 %** -- `WORK0`, `REF`, `L1`, `BASE`, `A1` bougent ensemble ⇒ une
   cause unique. ⛔ **Ce n'est PAS l'alignement** (les quatre rotations de la v12 coutent
   44 cy/tour au cycle pres en regime stationnaire). La cause est **hors de la boucle
   interne** ⇒ il faut un banc qui regarde le **bloc entier**, pas une iteration.
2. La forme **OCTET** du surcout de source cartouche (2 cy/octet) est **derivee** du mot
   (4 / 2), pas mesuree. Une rotation `ldirb` a source cartouche la confirmerait.
3. Pourquoi la v17 (pente marginale) donnait `div` = 52 quand deux autres mesures disent
   58 -- la latence etant **fixe** (v20), les trois chiffres devraient se reconcilier.
4. Le code **charge** d'un gestionnaire reste ~3 % trop rapide (v18 page 1 : 19,6 contre
   20,29). Dernier ecart mesure qui concerne le choix du modele de recouvrement.
5. **`queue_bytes`** (modele en octets) est a egalite avec le credit en cycles dans le
   bruit ⇒ **non departageable au corpus**. Desarme faute d'argument, pas de resultat.
6. 🎨 **Le vrai point aveugle** : ce corpus ne mesure que le **debit CPU sur des ecrans
   d'intro**. Le rendu, le son, le Z80, les splits raster n'ont **aucune** calibration
   equivalente. A 0,18 % sur le timing, c'est probablement la que le gain marginal est le
   plus eleve.

### Remettre le pied a l'etrier

```
cmake --build cpp/build -j 8                 # le coeur
python -m pytest -q                          # 2124 verts, 48 skips
python hw_calibration/corpus_gate.py         # 0,18 % moyen / 0,77 % pire, 26 cases
```

---

## 9sexies. (historique, 29/08) ROM v19 — la ristourne refutee. **L'etat courant est §9septies**

> §9quinquies reste pour la genese. **Deux corrections l'ont depasse**, et elles vont
> ENSEMBLE : prises separement chacune degrade, ce qui explique deux campagnes de
> balayage steriles.

**Le probleme etait DEUX ERREURS DE SIGNES OPPOSES.** Le coût d'une interruption tombait
« juste » (115 cy en debit contre 111 mesures) en additionnant **+20 cy de
sur-facturation** et **−17 cy de ristourne**. Aucun bouton ne pouvait converger : chacun
n'en touchait qu'une.

### `data_wait_cart_only` — le cout d'acces ne se paie qu'en code CARTOUCHE

Deux mesures silicium se contredisent sous une regle uniforme :

| | silicium | nous sans `data_access_cycles` | nous avec |
|---|---|---|---|
| boucle `MEM` du corpus | 65,3 cy/tour | 62 | **66** |
| chemin d'une interruption | 111,5 cy | **114** | 130 |

Les deux ecrivent en RAM ⇒ ce n'est pas la region des **donnees** qui les separe, c'est
celle du **code** : `MEM` est en cartouche, le stub d'interruption est en BIOS.
⚡ Mecanisme : si ce cout est une **contention** — l'acces vole un cycle de bus au
prefetch — il ne mord que la ou le fetch est cher, le bus 8 bits de la cartouche. Meme
regle et meme raison que `branch_taken_extra`.

### `irq_transparent_queue` — une interruption ne rend pas le code interrompu moins cher

Sans elle, les cycles de l'ISR **rechargent la file** du flot interrompu : −0,574
cy/instruction (credit), −0,224 (file en octets), soit ~17 et ~7 cy de ristourne par
interruption. Impossible : pendant l'ISR le bus cherche les octets **de l'ISR**.

⚡ **La ROM v19 le tranche** : le cout d'une interruption est **PLAT** selon que la boucle
interrompue soit limitee par le bus (`ld XWA`, 5 octets) ou par l'execution (`nop`) —
**112,6 / 112,0 / 110,7 / 110,5**, contraste **+1,5**, quand nos modeles predisaient
**+18,0** et **+11,6**. Arme, notre contraste tombe a **−1,2**.

L'etat de file (ou la dette) est **sauve a la livraison et rendu au `reti`**, pile de 8
niveaux pour les imbrications. Ni gain ni surcout : le silicium ne montre aucun des deux.

### Le controle par l'annexe B

Chemin complet d'une IRQ TI0 sur les tables officielles : **18 etats** (acceptation, table
(11), `JP (vecteur)` compris) + **8** + **8** (`PUSH<W> (mem)` 6 + `(#16)` +2, table (1) et
(10)) + **9** (`ret`) + **12** (`reti`) = **110 cy**. Silicium : **111,5**.
Nous : **130 → 114**.

### Et la surcharge de branche ne se paie pas sur un `reti` transparent

`branch_taken_extra` represente le **rechargement** de la file sur le bus 8 bits de la
cartouche. Si l'interruption rend au flot interrompu l'etat de bus qu'il avait, le `reti`
ne recharge **rien** : la file est rendue, pas refaite. La facturer la contredirait la
transparence qu'on vient d'adopter — et c'etaient exactement les 4 cy qui separaient notre
chemin des 110 de l'annexe B.

### Bilan

| | avant | apres |
|---|---|---|
| corpus, ecart moyen | 0,40 % | **0,31 %** |
| corpus, pire cas | 1,89 % | 1,89 % |
| chemin d'une IRQ | 130 cy | **110,0** (annexe B 110, silicium 111,5) |
| contraste v19 | +18,0 | **−1,2** (silicium +1,5) |
| suite | 2115 verts | **2115 verts** |

✅ **Verifie manette en main** par l'utilisateur sur plusieurs jeux (2026-08-29) : rien de
casse. Les temoins a surveiller restent le copieur raster du titre HiColor de reference et le HUD
en split raster de Cool Boarders.

⚠️ **Ce que le corpus ne voit pas** : il ne regarde que des ecrans d'intro. Les deux
temoins historiques d'un timing d'interruption faux sont **le copieur raster de l'ecran
titre HiColor de reference** (marge : UNE ligne) et **le HUD en split raster de Cool Boarders**.
⇒ a verifier **manette en main** avant de considerer le chantier clos.

---

## 9quinquies. ✅ L'ETAT COURANT DU MODELE (campagne v16/v17/v18, 2026-08-27)

Le §9quater decrivait le modele au sortir de v13/v14/v15. Trois ROM de plus l'ont corrige
sur trois points, dont **un vrai bug**.

**Ecart moyen aux 26 cases silicium : 0,40 %. Pire cas : 1,89 %.** Toutes les cases sont
sous 2 %. (Avant la campagne : 4,78 % et 12,31 %.) Banc `hw_calibration/corpus_gate.py`.

### Ce qui est arme

| piece | valeur | mesure |
|---|---|---|
| fetch | **4,00 cy / OCTET** (`fetch_wait_byte_q16 = 64`) | v14 p.1 : 4,03, droite 0,35 % |
| branche prise | **+4 cy, CARTOUCHE UNIQUEMENT** | v14 rot. C ; conditionnement mesure v18 |
| acces de donnee | **4 cy par ACCES**, lecture = ecriture | v15 p.1-2 : ~4,05, independant de la largeur |
| `mul` / `div` OCTET | **12 etats / 32 cycles** | v14 p.3-4, droites 0,5 % |
| `mul` / `div` MOT | 19 etats / 56 cycles | ⚠️ v17 mesure **15 / 47**, valables SEULEMENT avec la file en octets |
| entree d'interruption | 18 etats (36 cy) | v8, et **innocentee** par v18 |
| recouvrement | credit en cycles, `biu_slack` = 16 | ⚠️ forme fausse, voir plus bas |

### ⛔ REGLE 3 — deux chemins qui livrent la meme interruption doivent la livrer pareil

`access_wait` est remis a zero a la **fin** d'un pas d'instruction ; la livraison
d'interruption se fait **apres**. Or `deliver_irq` **lit le vecteur** -- quatre octets en
BIOS, soit deux mots a `bios_wait` = **16 cycles** -- qui s'accumulaient dans un
`access_wait` que plus personne ne remettait a zero et **retombaient sur la premiere
instruction du gestionnaire**.

```
avant :  FF22A5  push (0x6FD6)  cy=40  access_wait=32  stall=16
         FF22A9  push (0x6FD4)  cy=24  access_wait=16  stall=0
apres :  les deux a 24, identiques -- comme elles doivent l'etre
```

Et il y avait **DEUX** points de livraison (le pas normal, et le reveil depuis `HALT`) : le
second n'avait **aucune** des gardes. Les trois remises a zero (`access_wait`,
`data_wait_cycles`, `biu_debt`) sont desormais aux deux endroits.

⇒ Cout FIXE d'une IRQ : **131,2 -> 113,8 cy** contre **111,1** mesures (v18), soit +18 % ->
**+2 %**. `WORK1` -3,7 % -> **-0,9 %**.

### ⛔ REGLE 4 — une constante mesuree sur un bus ne s'applique qu'a ce bus

`branch_taken_extra` modelise le **rechargement de la file sur le bus 8 bits de la
cartouche** : c'est la qu'il a ete mesure. Le BIOS et la RAM ne sont pas sur ce bus. Leur
faire payer ce rechargement surfacturait tout le code du BIOS, donc **chaque interruption**,
qui passe par son aiguillage. Conditionne a la cartouche : corpus 0,67 % -> 0,59 %.

### ⚠️ LE RECOUVREMENT A ENCORE LA MAUVAISE FORME (et c'est le chantier restant)

`biu_slack` est **un scalaire** qui plafonne le credit d'avance, et trois mesures
independantes le tirent dans trois directions incompatibles :

| mesure | demande |
|---|---|
| v16 p.0 (division inseree dans une chaine de charges) | **~6** |
| corpus (26 cases) | **16** -- a 6, la moyenne double et le pire cas triple |
| v16 p.1 (cout d'une IRQ) | **> 16** |

⇒ Ce n'est pas une valeur a trouver : le modele confond « ce que le bus prend d'avance
pendant un calcul long » et « ce qu'une instruction courte peut en depenser ensuite ».

**Le modele physique est ecrit** (`queue_bytes`, file de 4 OCTETS remplie a un octet par
4 cycles, **aucun parametre libre**) et il rend **26,6 cy/division contre 26,5 mesures** la
ou le credit en cycles rend 16,0 -- une erreur de 40 % sur une mesure directe. Il matche
aussi les quatre grandeurs de la v14 a +-0,7 % et la v15.

⛔ **Il reste desarme** : arme, il laisse le corpus a -1 % uniforme et casse l'ancrage
HiColor (dont la marge est d'UNE ligne). Ce qui manque n'est pas une constante -- les trois
qu'on soupconnait ont ete mesurees (v17) et ne suffisent pas. `ngpc_set_queue_bytes(4)` et
`ngpc_set_muldiv_word(15, 47)` l'arment pour la mesure ; ils vont **ensemble**.

### 🔧 Instrumentation conservee

`ngpc_dbg_bios_charges` (nombre de charges `bios_wait`) et `ngpc_dbg_biu` (`biu_debt` a
l'entree, stall paye, `access_wait`) sont restes dans le coeur. Ils ont trouve le bug
d'interruption **en deux mesures**, la ou **sept** candidats elimines par raisonnement
n'avaient fait que l'encercler.
⚡ **Regle de methode : sur un ecart qui resiste a deux hypotheses, instrumenter plutot que
deduire.**

## 9bis-legacy. ⛔ ET `cart_wait` MASQUE UN DEFAUT PLUS PROFOND (historique, 2026-08-20)

Tout ce qui precede reste vrai : sans wait-states le code cartouche vole. Mais
`cart_wait = 3` n'est **pas** un temps d'attente de bus. Il compense une erreur d'unite.

### Un « state » Toshiba vaut DEUX cycles

`1 state = 2 / f_FPH`, avec `f_FPH = fc` sur cette puce. Confirme deux fois,
independamment :

- le manuel CPU 900/L1 l'ecrit tel quel ;
- la fiche TMP95C061B chiffre un state a **80 ns a 25 MHz** (table des modes micro-DMA)
  alors que son §4.3 donne `tosc` = **40 ns** au meme quartz. 80 = 2 x 40.

Et notre unite est bien `fc` : `kSerialByteCycles = 3200` pour un octet a 19200 bps tient
contre les **551 µs mesures sur silicium**, et la trame fait 102485 de ces cycles a 60 Hz.

### Or chaque gestionnaire facture l'etat comme s'il valait un cycle

`nop` 2, `push #8` 4, `halt` 6, `swi` 19, `LD R,r` 2 — verifies un par un contre l'annexe B
du manuel 900/L1, **tous exacts en states**. Donc **la table d'instructions du coeur est en
demi-states**, et `cart_wait = 3` absorbe la difference depuis le premier jour.

### ⛔ Et doubler ne repare pas — mesure, pas suppose

Bouton `Machine::base_scale` (defaut 1) ajoute pour le tester contre la campagne silicium :

| echelle | `cart_wait` | REG | ROM | RAM | LOOP | debit |
|---|---|---|---|---|---|---|
| 1 | 3 | +9 % | +7 % | +7 % | **+23 %** | −6 % |
| 2 | 1 | +24 % | +17 % | +17 % | −2 % | −12 % |
| 2 | 2 | −2 % | −6 % | −6 % | −14 % | −11 % |
| 2 | 3 | −20 % | −21 % | −21 % | −23 % | −11 % |

Boucles courtes et boucles a appels BIOS veulent **toujours** des `cart_wait` differents.
🔑 **L'erreur a une FORME, pas une echelle** : elle est dans *quelle instruction coute
quoi*. Un multiplicateur global ne peut pas la corriger, et en poser un serait exactement
le facteur d'ajustement que le §8 interdit.

### 📄 La table pour le faire proprement est dans le depot

Annexe B, « 900/L1 Instruction Lists (1/10) » a (10/10) —
`900L1 Core (e_900l1_chap3_cpu) Datasheet.pdf` (Toshiba), pages 159-168. Texte extractible.

⚠️ **Le PDF, pas le `.txt`** : l'extraction texte du catalogue perd les colonnes d'etats de
la Table 5.2 **sans le signaler** — c'est ce qui a fait croire que la table manquait.

⚠️ Le 900/L1 s'applique : Table 1 « CPU Core Different Points » met 900/H et 900/L1 dans la
**meme colonne**. (Le coeur *900* d'origine, lui, a d'autres timings — toujours nommer la
variante.)

⇒ **Chantier ouvert**, voir `OPEN_ITEMS.md`. Il change le temps de tous les jeux : il
demande son propre corpus A/B et sa propre validation, pas un coin de session.

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
