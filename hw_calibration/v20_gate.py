#!/usr/bin/env python3
"""Depouillement de la ROM v20 -- le tour des mesures manquantes (4 questions).

    python hw_calibration/v20_gate.py                       # predictions du coeur
    python hw_calibration/v20_gate.py --p0 B1 B2 W1 W2 --p1 Q0 Q4 R4 R8 \\
        --p2 D0 D1 D2 --p3 V8B V8W R8B R8W --rasv 198

⛔ RASV DOIT VALOIR 198.

⚖️ CHAQUE PAGE REPOND PAR UNE DIFFERENCE, JAMAIS PAR UN NIVEAU. Le cout propre du montage
-- `push`/`pop` du lot, boucle exterieure, chargement des registres -- est identique entre
les rotations d'une meme page et se simplifie. Une premiere version de ce banc comparait
une difference (Q4-Q0) a un NIVEAU (R4) et rendait un ecart negatif : une charge
« moins chere » apres un transfert bloc, ce qui n'a aucun sens.

  p0  `ldirb` contre `ldirw`. L'annexe B (3) donne le MEME `7n+1` aux deux formes, soit
      14 cy/iteration ; nous livrons 14 et **18**, ce dernier cale sur un oracle MAISON
      (le copieur HiColor de reference), jamais sur silicium.
  p1  un transfert bloc vide-t-il la file ? (`block_drains_queue`, ARME dans le livre,
      mesure sur le meme oracle maison)
      ⚠️ PAGE DE REFUTATION, PAS DE CONFIRMATION. Notre modele predit **+0,01 cy** : le
      drapeau ne change presque rien sur ce motif, parce qu'une charge de 5 octets est
      limitee par le BUS et coute son fetch que la file soit pleine ou vide. La page ne
      peut donc pas confirmer le drapeau -- mais un ecart net au silicium nous
      REFUTERAIT, et c'est deja une reponse.
  p2  la division est-elle a LATENCE VARIABLE ? C'est l'hypothese posee pour expliquer que
      trois autorites donnent trois nombres (annexe B 23 etats / v17 52 cy / corpus 56),
      et elle n'a jamais ete testee.
      ⛔ Les trois immediats ont la MEME longueur : sinon la page mesure l'encodage.
  p3  l'etranglement VRAM se paie-t-il par ACCES ou par OCTET ? `vram_wait = 9` vient
      d'etre epingle (v3) mais sa FORME ne l'a jamais ete -- et la v15 a deja refute une
      forme « par octet » pour les acces ordinaires.
      ⛔ DOUBLE DIFFERENCE contre les memes ecritures en RAM : sinon on mesure la
      difference entre l'instruction octet et l'instruction mot, pas le throttle.
"""

import argparse
import struct
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

ROM_PATH = ROOT / "hw_calibration" / "a_gaps_calib_v20.ngp"
ROM_MD5 = "dc2dc6cad6941573f65021e0e4725c17"
WINDOW = 199 * 515 * 60          # une fenetre de 60 trames, en cycles
BATCH = 16                       # unites par lot (pages 0, 2, 3)
BATCH1 = 64                      # page 1 : 64 blocs COURTS par lot
PAGES = {0: ("B1", "B2", "W1", "W2"), 1: ("Q0", "Q4", "R4", "R8"),
         2: ("D0", "D1", "D2"), 3: ("V8B", "V8W", "R8B", "R8W"), 4: ("RASV",)}
J_RIGHT, J_LEFT = 0x08, 0x04


def read_core():
    from core import native
    m = native.NativeMachine(ROM_PATH.read_bytes(),
                             bios=(ROOT / "bios.bin").read_bytes())
    m.set_timing_silicon()
    m.reset(bios_handoff=True)

    def step(n, b=0):
        for _ in range(n):
            m.write(0x00B0, bytes([b]))
            m.run_frames(1)

    step(400)

    def tile(c, r):
        return struct.unpack("<H", m.read(0x9000 + (r * 32 + c) * 2, 2))[0] & 0x01FF

    key = {tile(1 + i, 17): str(i) for i in range(10)}
    if len(key) != 10:
        raise SystemExit("cle de chiffres illisible")
    out = {}
    for pg, names in PAGES.items():
        # ⛔ APPUI CONTINU, ET ON REGARDE A CHAQUE TRAME. La ROM ne lit la manette QU'UNE
        # FOIS par cycle de mesure (~240 trames). Un appui alterne 20 trames / 20 trames
        # tombe donc toujours a la MEME phase -- et si cette phase est dans la moitie
        # relachee, la page n'avance JAMAIS. C'est un repliement : rallonger la boucle
        # n'y change rien, il faut tenir la touche.
        for _ in range(6000):
            cur = key.get(tile(12, 1))
            if cur == str(pg):
                break
            step(1, J_RIGHT if (cur is None or int(cur) < pg) else J_LEFT)
        else:
            raise SystemExit(f"page {pg} inatteignable")
        step(2, 0)
        step(len(names) * 60 + 300)
        for i, n in enumerate(names):
            s = "".join(key.get(tile(13 + j, 3 + i), " ") for j in range(5)).strip()
            out[n] = int(s) if s.isdigit() else None
    return out


def cy(count):
    """Cycles par lot."""
    return WINDOW / count if count else None


def derive(v):
    """Les quatre grandeurs, chacune tiree d'une DIFFERENCE."""
    d = {}
    if all(v.get(k) for k in ("B1", "B2", "W1", "W2")):
        d["ldirb"] = (cy(v["B2"]) - cy(v["B1"])) / (BATCH * 64)
        d["ldirw"] = (cy(v["W2"]) - cy(v["W1"])) / (BATCH * 64)
    if all(v.get(k) for k in ("Q0", "Q4", "R4", "R8")):
        d["apres_bloc"] = (cy(v["Q4"]) - cy(v["Q0"])) / (BATCH1 * 4)
        d["sans_bloc"] = (cy(v["R8"]) - cy(v["R4"])) / (BATCH1 * 4)
    if all(v.get(k) for k in ("D0", "D1", "D2")):
        d["div"] = [cy(v[k]) / BATCH for k in ("D0", "D1", "D2")]
    if all(v.get(k) for k in ("V8B", "V8W", "R8B", "R8W")):
        d["thr_octet"] = (cy(v["V8B"]) - cy(v["R8B"])) / (BATCH * 8)
        d["thr_mot"] = (cy(v["V8W"]) - cy(v["R8W"])) / (BATCH * 8)
    return d


def report(label, v):
    d = derive(v)
    print(f"\n=== {label} ===")
    if "ldirb" in d:
        print(f"  p0 LDIR     octet {d['ldirb']:6.2f} cy/iteration    mot {d['ldirw']:6.2f}"
              f"     (annexe B : 14,00 pour LES DEUX)")
    if "apres_bloc" in d:
        print(f"  p1 charge   apres un bloc {d['apres_bloc']:6.2f} cy"
              f"    sans bloc {d['sans_bloc']:6.2f}"
              f"     ecart {d['apres_bloc'] - d['sans_bloc']:+6.2f}")
    if "div" in d:
        a, b, c = d["div"]
        print(f"  p2 DIV      {a:7.2f}   {b:7.2f}   {c:7.2f}"
              f"     etendue {max(a, b, c) - min(a, b, c):+6.2f} cy/unite")
    if "thr_octet" in d:
        r = d["thr_mot"] / d["thr_octet"] if d["thr_octet"] else 0.0
        print(f"  p3 VRAM     throttle octet {d['thr_octet']:6.2f} cy/ecriture"
              f"    mot {d['thr_mot']:6.2f}    rapport {r:5.2f}"
              f"     (par ACCES => 1,00 ; par OCTET => 2,00)")
    return d


def verdicts(d):
    print("\n=== VERDICTS ===")
    print(f"  p0 : `ldirb` {d['ldirb']:.2f} cy/iteration, `ldirw` {d['ldirw']:.2f}.")
    if abs(d["ldirw"] - d["ldirb"]) < 1.0:
        print("       => LES DEUX FORMES COUTENT PAREIL : l'annexe B a raison et notre")
        print("          `ldirw_cost = 18` est faux.")
        print("       ! NE PAS L'ARMER SANS VERIFIER LE COPIEUR HICOLOR. 18 vient de son copieur")
        print("         HiColor, ancre a UN cycle et couvert par un test. Deux ancrages")
        print("         en conflit se traitent comme MEM/IRQ : chercher ce qui les")
        print("         separe, pas choisir le plus recent.")
    else:
        print("       => LA FORME MOT COUTE PLUS CHER : l'annexe B est un plancher ici")
        print("          aussi, comme pour MUL/DIV. Reporter la valeur dans `ldirw_cost`.")

    ec = d["apres_bloc"] - d["sans_bloc"]
    print("")
    print(f"  p1 : une charge coute {d['apres_bloc']:.2f} cy apres un bloc,"
          f" {d['sans_bloc']:.2f} sans   (notre modele : +0,01)")
    if abs(ec) < 2.0:
        print("       => CONFORME A NOTRE MODELE. Cette page ne CONFIRME pas")
        print("          `block_drains_queue` -- elle ne le pouvait pas -- elle")
        print("          constate seulement qu'elle ne nous refute pas.")
    else:
        print("       => NOUS SOMMES REFUTES : le silicium voit un effet la ou notre")
        print(f"          modele n'en voit aucun ({ec:+.1f} cy par charge). Le prix")
        print("          d'une instruction apres un transfert bloc est faux chez nous,")
        print("          et l'ancrage HiColor ne suffisait pas a le dire.")

    a, b, c = d["div"]
    span = max(a, b, c) - min(a, b, c)
    print(f"\n  p2 : etendue des trois divisions {span:.2f} cy par unite.")
    if span < 2.0:
        print("       => LATENCE FIXE. Une constante unique EXISTE : l'hypothese")
        print("          « latence variable » est REFUTEE, et les +1,9 % du corpus sur")
        print("          `DIV` viennent d'ailleurs. A rouvrir.")
    else:
        print("       => LATENCE VARIABLE, confirmee. Aucune constante unique ne peut")
        print("          etre juste ; les +1,9 % sont un plafond, pas un defaut.")

    r = d["thr_mot"] / d["thr_octet"] if d["thr_octet"] else 0.0
    print(f"\n  p3 : throttle {d['thr_octet']:.2f} cy par ecriture OCTET,"
          f" {d['thr_mot']:.2f} par MOT (rapport {r:.2f}).")
    if r < 1.5:
        print("       => PAR ACCES. Notre facturation PAR OCTET est fausse -- meme")
        print("          refutation que `data_wait_q16` par la v15. Et la valeur change :")
        print(f"          `vram_wait` devient ~{d['thr_octet']:.0f} cy par ACCES.")
    else:
        print("       => PAR OCTET. La forme livree est bonne ; verifier seulement que la")
        print(f"          valeur colle ({d['thr_octet']:.1f} cy/octet contre 9 livre).")


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    for pg, names in PAGES.items():
        if pg == 4:
            continue
        ap.add_argument(f"--p{pg}", type=int, nargs="*", default=None, metavar="N",
                        help=f"tir silicium page {pg} : " + " ".join(names))
    ap.add_argument("--rasv", type=int, default=None)
    args = ap.parse_args()

    if args.rasv is not None and args.rasv != 198:
        print(f"[STOP] RASV = {args.rasv}, attendu 198 : rien n'est exploitable.")
        return 2

    print(f"ROM  {ROM_PATH.name}  md5 attendu {ROM_MD5}")
    core = read_core()
    print("  compteurs du coeur : "
          + "  ".join(f"{n}={core[n]}" for pg in range(4) for n in PAGES[pg]))
    report("NOTRE MODELE", core)

    shots = {0: args.p0, 1: args.p1, 2: args.p2, 3: args.p3}
    if not all(shots.values()):
        print("\nPas de tir complet. Flashe, note les quinze nombres, puis relance :")
        print("  python hw_calibration/v20_gate.py --p0 B1 B2 W1 W2 --p1 Q0 Q4 R4 R8"
              " --p2 D0 D1 D2 --p3 V8B V8W R8B R8W --rasv 198")
        return 0

    sil = {}
    for pg, vals in shots.items():
        if len(vals) != len(PAGES[pg]):
            raise SystemExit(f"page {pg} : {len(PAGES[pg])} nombres attendus")
        for n, x in zip(PAGES[pg], vals):
            sil[n] = x
    verdicts(report("SILICIUM", sil))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
