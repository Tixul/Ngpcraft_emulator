#!/usr/bin/env python3
"""Cout d'un acces memoire par ZONE : v22 (RAS.H) + v23 (acces), contre leurs tirs silicium.

    python hw_calibration/access_gate.py                 # desarme vs arme (lecture VRAM 0 et 4)
    python hw_calibration/access_gate.py --vrw 0 2 4 6   # balaye la lecture VRAM
    python hw_calibration/access_gate.py --corpus        # rejoue AUSSI les 26 cases du corpus

POURQUOI CE BANC EXISTE. La v22 (lecture de RAS.H en rafale) a montre que nous executions
ses boucles 5 a 10 % trop vite. La v23 a isole la cause : sur console, un acces au K2GE
(registres) ou a la page SFR coute EXACTEMENT comme un acces RAM, et une lecture VRAM en
affichage actif est freinee. Le coeur, lui, exclut le K2GE et les SFR de
`data_access_cycles` et freine les ecritures de REGISTRES comme de la VRAM. Le reglage
`set_k2ge_access_model` corrige ca ; il reste DESARME tant que ce banc et le corpus
(`corpus_gate.py`, 26 cases) ne disent pas tous les deux qu'il ne degrade rien.

LECTURE DES ROM. Les deux ecrivent leurs resultats en RAM (pas a l'ecran) :
  v22 : 0x5400 = test A, 0x5420 = test B, mots R_* de cpu_calib_v22.c
        (R_DOWN = paires « meme ligne, RAS.H a baisse » ; 1023 - R_DOWN = changements de
        ligne en 1024 lectures, R_MODE = pas le plus frequent en unites de 4 clocks).
  v23 : 0x5400 + 2*id = T8, 0x5420 + 2*id = T16 ; cout d'un acces = (T16 - T8) / 512.
"""

import argparse
import struct
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

HW = ROOT / "hw_calibration"
FRAMES = 400

# Tir silicium du 03/11/2026 : v22, 7 passes ; R_DOWN 0x350 ou 0x351 selon la passe.
V22_SILICON = {"A changements": 1023 - 0x350, "A pas": 0x16,
               "B changements": 1023 - 0x3AD, "B pas": 0x09}
# Tir silicium du 03/11/2026 : v23, 8 passes ; 8 et A varient d'une passe a l'autre
# (8 : 23,93..23,96 ; A : 19,99..20,00), la moyenne est retenue.
V23_SILICON = {1: 20.00, 2: 32.00, 3: 20.00, 4: 20.00, 5: 32.00, 6: 28.00,
               7: 24.00, 8: 23.95, 9: 28.00, 10: 20.00, 11: 28.00}
V23_LABELS = {1: "lect mot RAS", 2: "lect oct RAS", 3: "lect mot 8020", 4: "lect mot RAM",
              5: "lect oct RAM", 6: "ecr mot RAM", 7: "ecr oct RAM", 8: "lect mot VRAM",
              9: "lect mot ROM", 10: "lect mot SFR", 11: "ecr mot 8020"}


def make_runner(as_ram, vram_read_wait):
    from core import native

    bios = (ROOT / "bios.bin").read_bytes()

    def run(rom_path, frames):
        # Meme machine que la colonne « modele » de corpus_gate.py, plus le reglage.
        m = native.NativeMachine(rom_path.read_bytes(), bios=bios)
        m.set_timing_silicon()
        m.set_branch_flush(False)
        m.set_k2ge_access_model(as_ram, vram_read_wait)
        m.reset(bios_handoff=True)
        for _ in range(frames):
            m.write(0x00B0, bytes([0]))
            m.run_frames(1)
        return m

    return run


def words(m, addr, n):
    return struct.unpack(f"<{n}H", m.read(addr, 2 * n))


def read_v22(run):
    m = run(HW / "a_rash_calib_v22.ngc", FRAMES)
    a, b = words(m, 0x5400, 13), words(m, 0x5420, 13)
    return {"A changements": 1023 - a[3], "A pas": a[5],
            "B changements": 1023 - b[3], "B pas": b[5]}


def read_v23(run):
    m = run(HW / "a_acces_calib_v23.ngc", FRAMES)
    t8, t16 = words(m, 0x5400, 12), words(m, 0x5420, 12)
    return {i: (t16[i] - t8[i]) / 512.0 for i in range(1, 12)}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--vrw", type=int, nargs="*", default=[0, 4],
                    help="vram_read_wait a evaluer, reglage arme (defaut 0 4)")
    ap.add_argument("--corpus", action="store_true",
                    help="rejouer aussi les 26 cases de corpus_gate.py")
    args = ap.parse_args()

    settings = [("desarme", False, 0)] + [(f"arme vrw={v}", True, v) for v in args.vrw]
    runners = {name: make_runner(r, v) for name, r, v in settings}

    print("\n=== v22  RAS.H en rafale (a_rash_calib_v22.ngc) ===")
    res = {name: read_v22(runners[name]) for name, _, _ in settings}
    print(f"  {'':<16}{'silicium':>9}" + "".join(f"{n:>16}" for n, _, _ in settings))
    for label, si in V22_SILICON.items():
        print(f"  {label:<16}{si:>9}" + "".join(f"{res[n][label]:>16}" for n, _, _ in settings))

    print("\n=== v23  cout d'un acces, clocks (a_acces_calib_v23.ngc) ===")
    res = {name: read_v23(runners[name]) for name, _, _ in settings}
    print(f"  {'':<16}{'silicium':>9}" + "".join(f"{n:>16}" for n, _, _ in settings))
    totals = {n: [] for n, _, _ in settings}
    for i, si in V23_SILICON.items():
        line = f"  {i:>2} {V23_LABELS[i]:<13}{si:>9.2f}"
        for n, _, _ in settings:
            got = res[n][i]
            totals[n].append(abs(got - si))
            line += f"{got:>10.2f}{got - si:>+6.2f}"
        print(line)
    print("  ecart absolu moyen :" + "".join(
        f"{sum(v) / len(v):>16.2f}" for v in totals.values()))

    if args.corpus:
        import corpus_gate as cg
        key = cg.digit_map(runners["desarme"])
        print("\n=== corpus_gate : 26 cases silicium (ecart de VITESSE, + = trop rapide) ===")
        ctot = {n: [] for n, _, _ in settings}
        for entry in cg.CORPUS:
            got = {n: cg.read_rom(runners[n], key, entry) for n, _, _ in settings}
            for label, si in entry["silicon"].items():
                line = f"  {entry['name'][:4]} {label:<11}{si:>6}"
                for n, _, _ in settings:
                    g = got[n][label]
                    if not g:
                        line += f"{'?':>16}"
                        continue
                    dev = (g / si - 1.0) * 100.0
                    ctot[n].append(abs(dev))
                    line += f"{g:>9}{dev:>+6.1f}%"
                print(line)
        for n, vals in ctot.items():
            print(f"  {n:<14} moyen {sum(vals) / len(vals):5.2f} %   pire {max(vals):5.2f} %"
                  f"   ({len(vals)} cases)")


if __name__ == "__main__":
    main()
