# APU T6W28 v1 (spec exécutable — audio core)

Purpose:
- décrire, **en clean-room**, le chip son NGPC **T6W28** (3 voies carré +
  1 voie bruit, sortie stéréo) au niveau algorithmique, pour :
  - préparer le remplacement du core audio hérité encore utilisé par
    l'outil son (cf. `README.md` § "sous-systeme audio ... modulaire")
  - donner à `ngpc_emu_core` (Python oracle puis C++) une référence
    opcode-registre par registre du son, au même grain de fidélité que
    le reste de l'émulateur
- **remplacer** l'approximation "SN76489 mono" par le vrai comportement
  T6W28 : latches **gauche/droite séparés** = panoramique stéréo réel,
  qui est LA différence du chip NGPC vs le SN76489 classique.

Statut : spec de référence (M-audio Phase 0). Pas encore d'implémentation
runtime dans `core/`. Ce document EST le contrat d'implémentation.

## 0. Provenance & frontière licence (à lire avant d'implémenter)

Cette spec décrit le T6W28 au niveau **algorithmique** uniquement :
mapping des ports, table de volume, polynôme du LFSR, latches stéréo.
Ce sont des **faits matériels** (non copyrightables), recoupés avec la
documentation publique de la famille SN76489, le mapping I/O du
`NGPC_HW_QUICKREF.md` et le comportement observé des jeux. **Aucun code
n'est recopié ici.**

⚠ Règle clean-room, non négociable : l'implémentation `core/` doit être
écrite **from scratch** à partir de ce seul document — exactement la
méthode clean-room `thc1` de la toolchain — sans lire d'implémentation
sous licence copyleft.

La reconstruction **band-limited** du signal (synthèse à offsets) n'est
volontairement **pas** spécifiée : c'est un détail de sortie côté DAC,
remplaçable par notre propre resampler.

## 1. Vue d'ensemble du chip

Le T6W28 (Toshiba) = variante **stéréo** du SN76489 :

- **4 oscillateurs** : `osc[0..2]` = ondes carrées (tone), `osc[3]` = bruit.
- Chaque oscillateur a **deux volumes indépendants** : `volume_left` et
  `volume_right` (0..64). C'est la spécificité T6W28 : là où le SN76489
  a un seul registre de volume par voie, le T6W28 en a un par canal
  stéréo, adressé par **deux ports séparés**.
- Amplitude interne bipolaire : la sortie d'une voie alterne entre
  `+volume` et `-volume` selon la phase / le LFSR.

### Table de volume (16 entrées, atténuation logarithmique)

Fait matériel (courbe ~1.26 dB/pas, index 0 = fort, 15 = muet) :

```
volumes = [64, 50, 39, 31, 24, 19, 15, 12, 9, 7, 5, 4, 3, 2, 1, 0]
# volumes[i] = round(64 * 1.26**(15-i) / 1.26**15)
```

Le nibble bas d'une écriture de volume (`data & 0x0F`) indexe cette table.
`0x0` = plein volume (64), `0xF` = silence (0).

## 2. Interface registres (mapping ports NGPC)

Ports d'I/O internes TLCS-900/H (zone `0x00..0xFF`) :

| Port  | Rôle                         | Fonction chip        |
|-------|------------------------------|----------------------|
| `0xA0`| Sound chip **RIGHT**         | `write_data_right`   |
| `0xA1`| Sound chip **LEFT**          | `write_data_left`    |
| `0xA2`| DAC gauche (PCM 8-bit)       | canal DAC L          |
| `0xA3`| DAC droite (PCM 8-bit)       | canal DAC R          |
| `0xB8`| `0x55`=enable / `0xAA`=disable son | master enable  |

⚠ Garde importante (fidélité) : les écritures `0xA0/0xA1` ne vont au
T6W28 **que si le Z80 audio n'est pas actif** (`if (!Z80_IsEnabled())`).
Quand le Z80 est en marche, c'est lui qui pilote le son ; le CPU principal
n'y touche pas. À modéliser dans le bus, pas dans l'APU.

### 2.1 Format d'un octet de commande (identique L/R)

Deux types d'écriture, discriminés par le **bit 7** :

- **`data & 0x80` = LATCH** (octet 1) :
  - `index = (data >> 5) & 3`  → voie visée (0..2 = carré, 3 = bruit)
  - `data & 0x10` (bit 4) : `1` = commande **volume**, `0` = commande
    **période/data**
  - le latch est mémorisé (`latch_left` / `latch_right`) pour l'octet
    suivant.
- **`data & 0x80 == 0` = DATA** (octet 2) : complète la dernière commande
  latchée sur ce canal (réutilise `index` du latch mémorisé).

`index` et le bit `0x10` proviennent **toujours** du dernier latch du même
canal (L ou R) — d'où deux latches séparés.

### 2.2 Écriture de VOLUME (`latch & 0x10`)

Port LEFT  → `osc[index].volume_left  = volumes[data & 15]`
Port RIGHT → `osc[index].volume_right = volumes[data & 15]`

(Une même voie peut donc avoir un volume gauche ≠ droit = pan.)

### 2.3 Écriture de PÉRIODE — voies carré (port LEFT, index 0..2)

La période (14 bits ici, `0..0x3FFF`) se construit en deux octets :

```
si data & 0x80 (latch)  : period = (period & 0x3F00) | ((data << 4) & 0x00FF)   # nibble bas
sinon (data)            : period = (period & 0x00FF) | ((data << 8) & 0x3F00)   # 6 bits hauts
```

Les périodes de tonalité ne s'écrivent **que par le port LEFT** (`0xA1`).

### 2.4 Écriture côté RIGHT — contrôle du bruit / période étendue

Le port RIGHT (`0xA0`), hors commande volume, sert au bruit :

- `index == 2` (data) : écrit `noise.period_extra` (même schéma 2 octets
  que §2.3). C'est la période "voie carré 3" réutilisable par le bruit.
- `index == 3` (latch bruit) : `select = data & 3`
  - `select < 3` : période bruit = table fixe `noise_periods[select]`
    avec `noise_periods = [0x100, 0x200, 0x400]`
  - `select == 3` : période bruit = `noise.period_extra` (suit la voie 3)
  - `tap = 13 if (data & 0x04) else 16`  (bit 2 : `1`=bruit blanc,
    `0`=bruit périodique — `tap=16` désactive le tap)
  - `shifter = 0x4000` (reset du LFSR à chaque reconfig bruit)

## 3. Génération du signal (état + run)

Chaque oscillateur maintient : `delay` (fraction de période reportée entre
deux `run`), `last_amp_left/right` (dernière amplitude émise, pour n'émettre
que des **deltas**), et `volume_left/right`.

### 3.1 Voie carré

État propre : `period` (14 bits), `phase` (0/1).

Règle de silence / anti-alias : si `(volume_left==0 && volume_right==0)`
**ou** `period <= 128`, la voie est muette (fréquences ≥ ~16 kHz ignorées) ;
on ramène `last_amp` à 0 et on avance seulement la phase :

```
phase = (phase + ceil((end - time) / period)) & 1
```

Sinon, sur l'intervalle `[time, end)` :

```
amp_left  = +volume_left  if phase else -volume_left
amp_right = +volume_right if phase else -volume_right
# émettre les deltas amp - last_amp aux instants de transition
boucle time += period : phase ^= 1 ; amp_* = -amp_*   # onde carrée 50%
```

`delay = time - end` reporte le reliquat sur le prochain `run`.

### 3.2 Voie bruit — LFSR 15 bits

État propre : `shifter` (15 bits, init `0x4000`), `tap` (13 ou 16),
`period` (pointeur vers période active), `period_extra`.

Sortie = `±volume` selon `shifter & 1`. Avancement (fait matériel, polynôme
XOR à deux prises) :

```
period_eff = 2 * active_period   (si 0 → 16)
à chaque pas de period_eff :
    changed = (shifter + 1) & 2               # bit va-t-il changer ?
    shifter = (((shifter << 14) ^ (shifter << tap)) & 0x4000) | (shifter >> 1)
    si changed : inverser l'amplitude émise (delta)
```

`tap = 16` (bit désactivé) ⇒ le terme `shifter << 16` sort du masque
`0x4000` ⇒ pas de XOR ⇒ LFSR **périodique** (buzz) au lieu de blanc.

## 4. Mixage & volume global

- `run_until(t)` fait tourner les 4 oscillateurs de `last_time` à `t`.
- Volume maître : `vol *= 0.85 / (4 * 64 * 2)` avant synthèse — headroom
  pour 4 voies × 64 pas × 2 canaux, avec 15 % de marge anti-clip.
  À conserver comme constante de normalisation pour matcher le niveau.
- Sortie stéréo : les voies écrivent sur `output_left` / `output_right`
  séparément (canal "center" mono = somme, optionnel).
- **DAC** (ports `0xA2/0xA3`) : PCM 8-bit direct, mixé par-dessus les 4
  voies. À spécifier en v2 (samples voix des jeux) ; hors scope v1.

## 5. Savestate (champs à sérialiser)

Par oscillateur (×4) : `delay`, `volume_left`, `volume_right`.
Par carré (×3) : `period` (masquer `& 0x3FFF` au load), `phase`.
Bruit : `shifter`, `tap`, `period_extra` (masquer `& 0x3FFF`), et l'index
de période (`0..2` = fixe, `3` = extra — restaurer le pointeur au load).
Global : `latch_left`, `latch_right`.

Ces champs s'intègrent au `core/savestate.py` existant (même politique de
sérialisation immuable que le CPU).

## 6. Plan d'implémentation (oracle Python → core C++)

1. **`core/apu.py`** : dataclass immuable `ApuState` (4 oscs + latches),
   fonctions pures `write_left(state, data) -> state`,
   `write_right(state, data) -> state`, `run_until(state, cycles) -> (state, samples)`.
   Même style frozen-dataclass que `core/cpu.py`.
2. **Tests** (`tests/test_apu_t6w28.py`) : vecteurs registres → attendus.
   Golden minimal = table de volume, construction de période 2 octets,
   séquence LFSR blanc vs périodique, pan L≠R.
3. **Sortie** : d'abord un resampler naïf (somme d'amplitudes par sample
   au taux NGPC), pas de band-limited requis pour l'oracle. La qualité
   band-limited viendra dans le core C++ temps réel.
4. **Cross-check** : capturer un log d'écritures son d'une ROM de test
   (via `psg-trace` MCP déjà présent) et comparer l'état APU dérivé.

## 7. Points de fidélité à ne PAS lisser

- latches L/R **séparés** (le pan est réel, pas cosmétique).
- garde Z80 : pas d'écriture APU quand le Z80 audio tient le son.
- `period <= 128` muet (voies carré) — comportement anti-alias du chip.
- `tap` 13 vs 16 = blanc vs périodique (les jeux s'en servent pour les
  bruits de moteur / percussions).
- table de volume logarithmique exacte (un volume linéaire sonnerait faux).
```
