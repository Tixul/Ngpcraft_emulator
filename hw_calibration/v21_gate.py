#!/usr/bin/env python3
"""Depouillement de la ROM v21 -- un transfert bloc, quatre chemins.

    python hw_calibration/v21_gate.py                       # predictions du coeur
    python hw_calibration/v21_gate.py --p0 RR1 RR2 RV1 RV2 \\
        --p1 OR1 OR2 OV1 OV2 --rasv 198

⛔ RASV DOIT VALOIR 198.

LA QUESTION. La v20 mesure `ldirw` **RAM -> RAM** a **14,16** cy/iteration -- l'annexe B
au centieme. Mais le copieur HiColor de reference, qui copie **ROM -> VRAM**, exige ~18 :
a 14 il tourne 21 % trop vite et l'image se dechire. Deux differences separent les deux
montages, la region SOURCE et la region DESTINATION, et personne ne les a jamais separees.
⛔ L'explication evidente est deja refutee : « 18 = 14 + l'etranglement VRAM » ne tient
pas (le throttle vaut 2,95 cy/acces, il en faudrait 4, et arme sur les blocs il ne change
rien -- il se fait absorber par le recouvrement).

CE QUE LA PAGE REND, et chaque nombre vient d'une DIFFERENCE (64 -> 128 iterations, donc
le chargement des registres se simplifie) :

    RR   RAM -> RAM     le TEMOIN
    RV   RAM -> VRAM    ce que coute la DESTINATION, seule
    OR   ROM -> RAM     ce que coute la SOURCE, seule
    OV   ROM -> VRAM    le chemin du copieur HiColor

⚡ ET LA QUESTION QUI DECIDE DU MODELE : **(OV - RR) vaut-il (RV - RR) + (OR - RR) ?**
Si oui les deux effets sont independants et il suffit de les additionner. Sinon il existe
un troisieme terme, et on saura qu'il existe au lieu de le deviner.

⚠️ Notre modele predit **le meme cout aux quatre chemins** : il ne connait ni la source ni
la destination d'un transfert bloc. La ROM ne peut donc pas nous confirmer -- seulement
nous corriger, et dire de combien.
"""

import argparse
import struct
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

ROM_PATH = ROOT / "hw_calibration" / "a_blocpath_calib_v21.ngp"
ROM_MD5 = "fa103327801b0308e5877e4342cde8c3"
WINDOW = 199 * 515 * 60
BATCH = 16          # unites par lot
STEP = 64           # iterations de plus entre les deux longueurs
PAGES = {0: ("RR1", "RR2", "RV1", "RV2"), 1: ("OR1", "OR2", "OV1", "OV2"), 2: ("RASV",)}
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
        # ⛔ APPUI CONTINU : la ROM ne lit la manette qu'une fois par cycle de mesure,
        # un appui alterne tombe toujours a la meme phase et la page n'avance jamais.
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


def cy(c):
    return WINDOW / c if c else None


def per_iter(v, a, b):
    if not (v.get(a) and v.get(b)):
        return None
    return (cy(v[b]) - cy(v[a])) / (BATCH * STEP)


def report(label, v):
    d = {k: per_iter(v, k + "1", k + "2") for k in ("RR", "RV", "OR", "OV")}
    print(f"\n=== {label} ===")
    if all(d.values()):
        print(f"  RAM>RAM  {d['RR']:6.2f} cy/iteration   (annexe B : 14,00)")
        print(f"  RAM>VRAM {d['RV']:6.2f}    destination seule : {d['RV'] - d['RR']:+6.2f}")
        print(f"  ROM>RAM  {d['OR']:6.2f}    source seule      : {d['OR'] - d['RR']:+6.2f}")
        print(f"  ROM>VRAM {d['OV']:6.2f}    les deux          : {d['OV'] - d['RR']:+6.2f}")
        somme = (d["RV"] - d["RR"]) + (d["OR"] - d["RR"])
        print(f"  somme des deux effets {somme:+6.2f}"
              f"   contre {d['OV'] - d['RR']:+6.2f} mesures"
              f"   ecart {d['OV'] - d['RR'] - somme:+6.2f}")
    return d


def verdict(d):
    print("\n=== VERDICT ===")
    print(f"  Le copieur HiColor (ROM>VRAM) exige ~17,9 cy/iteration ; la v21 mesure"
          f" {d['OV']:.2f}.")
    if abs(d["OV"] - 17.9) < 1.0:
        print("       => LE CHEMIN EXPLIQUE TOUT. `ldirw_cost` doit dependre des REGIONS,")
        print("          pas etre un scalaire : 14 en RAM->RAM, ~18 en ROM->VRAM. Le 18")
        print("          d'aujourd'hui n'etait pas faux, il etait mal ATTRIBUE.")
    else:
        print("       => LE CHEMIN N'EXPLIQUE PAS TOUT. Ce que le copieur HiColor demande ne se")
        print("          retrouve pas dans un `ldirw` ROM->VRAM nu : chercher ce que son")
        print("          copieur fait d'autre (longueur, phase raster, source exacte).")
    somme = (d["RV"] - d["RR"]) + (d["OR"] - d["RR"])
    ec = d["OV"] - d["RR"] - somme
    print(f"\n  Additivite : les deux effets sommes font {somme:+.2f},"
          f" le chemin complet {d['OV'] - d['RR']:+.2f} (ecart {ec:+.2f}).")
    if abs(ec) < 1.0:
        print("       => INDEPENDANTS : le modele n'a qu'a additionner un surcout de")
        print("          SOURCE et un surcout de DESTINATION. Deux nombres, pas quatre.")
    else:
        print("       => PAS INDEPENDANTS : il existe un TROISIEME terme, propre a la")
        print("          combinaison. A ne pas modeliser par une somme.")


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--p0", type=int, nargs="*", default=None, metavar="N",
                    help="tir silicium page 0 : " + " ".join(PAGES[0]))
    ap.add_argument("--p1", type=int, nargs="*", default=None, metavar="N",
                    help="tir silicium page 1 : " + " ".join(PAGES[1]))
    ap.add_argument("--rasv", type=int, default=None)
    args = ap.parse_args()

    if args.rasv is not None and args.rasv != 198:
        print(f"[STOP] RASV = {args.rasv}, attendu 198 : rien n'est exploitable.")
        return 2

    print(f"ROM  {ROM_PATH.name}  md5 attendu {ROM_MD5}")
    core = read_core()
    print("  compteurs du coeur : "
          + "  ".join(f"{n}={core[n]}" for pg in (0, 1) for n in PAGES[pg]))
    report("NOTRE MODELE", core)

    if not (args.p0 and args.p1):
        print("\nPas de tir complet. Flashe, note les huit nombres, puis relance :")
        print("  python hw_calibration/v21_gate.py --p0 RR1 RR2 RV1 RV2"
              " --p1 OR1 OR2 OV1 OV2 --rasv 198")
        return 0

    sil = {}
    for pg, vals in ((0, args.p0), (1, args.p1)):
        if len(vals) != 4:
            raise SystemExit(f"page {pg} : quatre nombres attendus")
        for n, x in zip(PAGES[pg], vals):
            sil[n] = x
    verdict(report("SILICIUM", sil))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
