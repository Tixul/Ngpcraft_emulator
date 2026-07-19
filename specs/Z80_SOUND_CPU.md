# Le Z80 — le co-processeur son

> **Ce document part de PREUVES, pas de suppositions.** Chaque adresse ci-dessous
> a été observée en exécutant un vrai jeu sur le cœur natif, puis confirmée dans
> la doc constructeur SNK. Rien n'y est inféré.

## 1. Pourquoi ce chantier existe

Le cœur C++ fait tourner les jeux à ×48 le temps réel, et **25 ROMs sur 73
affichent une vraie image**. Les 42 autres restent noires — et ce n'est **ni** un
bug de rendu, **ni** de boot, **ni** de CPU.

Pac-Man boucle éternellement là-dessus :

```
0x20670B   ld XIX, 0x000070DE
0x206710   ld A, (XIX)
0x206712   cp (XIX), A
0x206714   jr NZ, 0x206722      <- il sort quand la valeur CHANGE
```

Il attend qu'**autre chose** écrive à `0x70DE`. Or `0x007000..0x007FFF` est la
`SHARED_Z80_RAM` (`core/bus.py`), et ce « autre chose » est le **CPU son**.

Preuve directe : après 50 000 instructions, Pac-Man a téléversé **2 691 octets**
dans cette fenêtre, et ils commencent par

```
f3        DI
31 c0 00  LD SP, 0x00C0
c3 e6 01  JP 0x01E6
e5        PUSH HL
cd c2 08  CALL 0x08C2
e1        POP HL
c9        RET
```

C'est du **Z80**, sans ambiguïté possible. Le jeu charge son pilote son et attend
qu'il réponde. **Il n'y a aucun Z80 dans cet émulateur.** C'est tout.

## 2. Le protocole, observé puis confirmé

En instrumentant les écritures de Pac-Man dans la page I/O :

| Adresse | Écrit | Doc SNK (`01_SDK/docs/K1SoundSim`) | Rôle |
|---|---|---|---|
| `0x00B8` | `0x55` | `BASE+18B8h` | **Reset du Z80 / du générateur son.** `0x55` le libère. |
| `0x00BA` | 1 accès | `BASE+18BAh` | **Déclenche UNE NMI vers le Z80** (la donnée est sans objet). |
| `0x00BC` | `0xFF` | `BASE+18BCh` | **Registre de communication**, dual-port. |

Les offsets de la doc se projettent **un pour un** sur la page I/O du NGPC. La
séquence est donc :

1. le CPU principal téléverse le pilote dans `0x7000..0x7FFF` ;
2. il écrit `0x55` en `0xB8` → le Z80 sort de reset ;
3. il dépose une commande en `0xBC` ;
4. il écrit en `0xBA` → **NMI** ;
5. le Z80 s'exécute, lit la commande, travaille, et **réécrit dans la RAM
   partagée** ;
6. le CPU principal, qui scrutait `0x70DE`, repart.

## 3. La machine Z80

Côté Z80 (doc SNK, « Related Register List (Z80-CPU) ») :

| Adresse Z80 | Rôle |
|---|---|
| `0x0000..0x0FFF` | sa mémoire = la `SHARED_Z80_RAM` (vue à `0x7000` par le CPU principal) |
| `0x8000` | registre de communication PC↔Z80 (le même octet que `0xBC` côté principal) |
| `0xC000` | registre de contrôle d'interruption |

* **Horloge : 3,072 MHz** (doc SNK) = exactement la moitié des 6,144 MHz du
  TLCS-900. ⇒ le Z80 avance d'**un cycle pour deux** cycles du CPU principal, ce
  qui évite toute dérive de compteur : c'est un rapport entier.
* Le générateur son **T6W28** est piloté par les ports d'E/S du Z80. Le modèle
  existe déjà (`core/apu.py`), **non branché** — c'est la suite, pas ce chantier.

## 4. La doctrine, inchangée

**On construit le tribunal avant l'accusé**, et **rien de silencieusement faux** :

* Un opcode non porté **TRAPPE BRUYAMMENT** (`NGPC_UNIMPLEMENTED`) exactement
  comme dans le cœur TLCS-900. Un Z80 qui NOPe ce qu'il ne connaît pas
  « fonctionnerait » en donnant des résultats faux, et rien ne le dirait.
* **La liste de travail se MESURE, elle ne se devine pas.** On câble le Z80, on
  le laisse trapper, et **ce sont les vrais pilotes son des jeux** qui dictent
  quels opcodes porter — dans l'ordre de leur fréquence réelle. C'est la méthode
  qui a fait passer le cœur TLCS-900 de 57 à 139 millions d'instructions.
* **La porte de preuve est FONCTIONNELLE** : le nombre de ROMs qui composent une
  vraie image. Aujourd'hui **25/73**. Il n'existe pas de second Z80 à qui se
  comparer, donc pas de G2 différentiel ici ; en revanche le jeu lui-même est un
  oracle sans complaisance — il ne repart que si le pilote a réellement tourné.

## 5. Ce qui n'est PAS dans ce chantier

Le **son**. Faire tourner le Z80 débloque l'IMAGE, parce que le jeu attend le
handshake avant de dessiner. Produire des échantillons audio (brancher le T6W28,
mixer, sortir un WAV) est un chantier distinct, et il vient après.
