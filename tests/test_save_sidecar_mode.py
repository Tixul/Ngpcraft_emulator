"""LE MODE « FICHIER SEPARE », DE BOUT EN BOUT -- ce que personne ne testait.

⛔ LE TROU. `test_save_persistence.py` couvre la sauvegarde a travers des cycles
d'alimentation, mais TOUTES ses sessions passent `sidecar=False`: elles mesurent le mode
« dans la ROM ». Le reglage « Fichier separe » (`ngpc_settings.SAVE_SIDECAR`) construit
la session autrement -- `save_to_rom=False, sidecar=True` -- et ce chemin-la n'avait
aucune couverture. Signale par un joueur: « quand les sauvegardes in-game sont en
Fichier separe, ni le BIOS ni aucun jeu ne sauvegarde ou ne recharge ses donnees ».

✅ CAUSE TROUVEE (les deux derniers tests). Le mecanisme du mode tient -- les trois
premiers tests le montrent -- et c'est justement pour ca qu'il fallait chercher PLUS
HAUT: la sauvegarde etait bien restauree, puis DETRUITE par la remise a zero qui donne
la main a la cartouche a la fin du demarrage console (`reset_memory` recharge l'image
vierge de la ROM). En mode « dans la .ngc » cette image porte deja la sauvegarde, donc
seul le fichier separe le montrait. La meme remise a zero ecrasait la page de reglages
du BIOS -- l'autre moitie du rapport, « ca reinitialise mon BIOS ». Voir
`NativeSession.handoff_reset`.

⚠️ CHAQUE SESSION ECRIT DES OCTETS DIFFERENTS. Reprogrammer la meme charge utile ne peut
pas echouer -- une cellule NOR fait un ET, donc programmer un octet sur lui-meme le laisse
tel quel et la relecture correspond que l'effacement ait marche ou non. C'est comme ca
que le defaut de juillet a tenu quatre sessions « vertes ».
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
HLE_IMAGE = REPO / "hle_bios" / "bios_hle.bin"
RETAIL_BIOS = REPO / "bios.bin"

from core import flash_file, native  # noqa: E402

XWA, XBC, XDE, XHL = 0, 1, 2, 3
CODE, SRC = 0x004000, 0x004100
VECT_FLASHWRITE, VECT_FLASHERS = 6, 8
CART_BASE = 0x200000


def _rom(size: int) -> bytes:
    rom = bytearray(b"\xFF" * size)
    rom[0:28] = b" LICENSED BY SNK CORPORATION"
    rom[0x1C:0x20] = (0x200040).to_bytes(4, "little")
    rom[0x23] = 0x10
    rom[0x40] = 0x05
    return bytes(rom)


@unittest.skipUnless(HLE_IMAGE.exists(), "hle_bios/bios_hle.bin not built")
@unittest.skipUnless(native.available(), "native core not built")
class SeparateFileMode(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    @staticmethod
    def _call(m, wa, bc=0, de=0, hl=0):
        m.write(CODE, bytes([0xF9, 0x05]))
        st = m.cpu(); st.pc = CODE; m.set_cpu(st)
        st = m.cpu()
        b3 = st.regs if st.rfp == 3 else st.banks[3]
        b3[XWA], b3[XBC], b3[XDE], b3[XHL] = wa, bc, de, hl
        m.set_cpu(st)
        m.run(4_000_000, record=False)

    def _session(self, cart, flash, cap, *, bios=None, real_bios=False, boot=0):
        """A session built the way the shell builds one in 'Separate file' mode."""
        from core.native_session import NativeSession
        s = NativeSession(cart, bios_path=bios or HLE_IMAGE, flash_size=cap,
                          autosave=False, save_to_rom=False, sidecar=True,
                          save_path=flash, real_bios=real_bios)
        for _ in range(boot):
            s.machine.run_frames(1)
        return s

    def _write_save(self, s, seed: int) -> bytes:
        payload = bytes((i * seed + 1) & 0xFF for i in range(256))
        s.machine.write(SRC, payload)
        self._call(s.machine, (VECT_FLASHERS << 8) | 0, bc=0)
        self._call(s.machine, (VECT_FLASHWRITE << 8) | 0, bc=1, hl=SRC, de=0)
        return payload

    def test_the_rom_file_is_never_touched_and_the_save_still_comes_back(self):
        """C'est la PROMESSE du mode: la collection du joueur reste intacte, et la
        sauvegarde vit dans `saves/<jeu>.flash`. Les deux moities comptent -- un mode
        qui laisse la ROM tranquille en perdant la sauvegarde n'en est pas un."""
        cart = self.dir / "game.ngc"
        cart.write_bytes(_rom(0x200000))
        pristine = cart.read_bytes()
        flash = self.dir / "game.flash"

        s = self._session(cart, flash, 0x200000)
        payload = self._write_save(s, 7)
        self.assertEqual(s.machine.read(CART_BASE, 256), payload, "l'ecriture n'a pas pris")
        self.assertTrue(s.commit_save(), "rien n'a ete ecrit")
        s.close()

        self.assertEqual(cart.read_bytes(), pristine, "le .ngc a ete modifie")
        self.assertTrue(flash.exists(), "aucun fichier de sauvegarde separe")

        s2 = self._session(cart, flash, 0x200000)
        try:
            self.assertTrue(s2.save_loaded)
            self.assertEqual(s2.machine.read(CART_BASE, 256), payload,
                             "la sauvegarde n'est pas revenue")
        finally:
            s2.close()

    def test_three_sessions_each_keep_the_one_before(self):
        """Trois charges DIFFERENTES: c'est le seul moyen de voir un effacement rate.
        Et la cartouche est sous-remplie (512 Kio sur une puce de 8 Mbit), la forme qui
        avait deja casse les sauvegardes en juillet -- dans l'autre mode."""
        cart = self.dir / "small.ngc"
        cart.write_bytes(_rom(0x80000))
        pristine = cart.read_bytes()
        flash = self.dir / "small.flash"

        previous = None
        for seed in (7, 13, 29):
            s = self._session(cart, flash, 0x100000)
            try:
                if previous is not None:
                    self.assertEqual(s.machine.read(CART_BASE, 256), previous,
                                     f"session seed={seed}: la sauvegarde precedente a disparu")
                payload = self._write_save(s, seed)
                self.assertEqual(s.machine.read(CART_BASE, 256), payload)
                self.assertTrue(s.commit_save())
            finally:
                s.close()
            previous = payload
        self.assertEqual(cart.read_bytes(), pristine, "le .ngc a ete modifie")

    @unittest.skipUnless(RETAIL_BIOS.exists(), "needs the retail bios.bin (gitignored)")
    def test_it_survives_a_real_console_boot(self):
        """⚡ LA COMBINAISON DU RAPPORT: fichier separe ET demarrage console.

        Le BIOS reel tourne pour de bon avant le jeu, ce qui donne a la console des
        centaines de trames pour toucher la cartouche entre la restauration et la
        premiere lecture du joueur. La sauvegarde doit etre la AVANT et APRES ce boot.
        """
        cart = self.dir / "boot.ngc"
        cart.write_bytes(_rom(0x200000))
        flash = self.dir / "boot.flash"

        s = self._session(cart, flash, 0x200000, bios=RETAIL_BIOS, real_bios=True,
                          boot=420)
        payload = self._write_save(s, 11)
        self.assertTrue(s.commit_save())
        s.close()

        s2 = self._session(cart, flash, 0x200000, bios=RETAIL_BIOS, real_bios=True)
        try:
            self.assertEqual(s2.machine.read(CART_BASE, 256), payload,
                             "perdue avant meme que la console demarre")
            for _ in range(420):
                s2.machine.run_frames(1)
            self.assertEqual(s2.machine.read(CART_BASE, 256), payload,
                             "le boot du BIOS a efface la sauvegarde restauree")
        finally:
            s2.close()

    @unittest.skipUnless(RETAIL_BIOS.exists(), "needs the retail bios.bin (gitignored)")
    def test_the_hand_off_into_the_cartridge_keeps_the_save(self):
        """⛔ LE TROU EXACT DU RAPPORT, et ce que le test au-dessus ne fait pas.

        `test_it_survives_a_real_console_boot` laisse tourner le BIOS -- et le BIOS,
        lui, ne touche pas la sauvegarde. Ce qui la detruisait, c'est l'ETAPE SUIVANTE:
        la remise a zero qui donne la main a la cartouche, parce que `reset_memory`
        RECHARGE L'IMAGE VIERGE de la ROM. En mode « dans la .ngc » cette image contient
        deja la sauvegarde et rien ne se voyait; en fichier separe la .ngc est vierge,
        donc la sauvegarde restauree partait a chaque lancement.

        ⚠️ Et elle ne peut PAS etre recopiee depuis la memoire vive: le BIOS vient
        d'identifier la puce, qui reste en AUTOSELECT et repond 0xFF partout ailleurs
        que sur ses quatre octets d'ID -- la premiere version du correctif a remis ce
        0xFF a la place de la sauvegarde. D'ou l'assertion sur l'autoselect ci-dessous:
        elle fige la raison pour laquelle on relit le FICHIER.
        """
        SAVE_AT = 0x2F8000          # ou un vrai jeu sauvegarde: en haut de la puce
        cart = self.dir / "handoff.ngc"
        cart.write_bytes(_rom(0x200000))
        flash = self.dir / "handoff.flash"
        payload = bytes((i * 23 + 5) & 0xFF for i in range(256))
        flash_file.write(flash, [(SAVE_AT, payload)])

        s = self._session(cart, flash, 0x200000, bios=RETAIL_BIOS, real_bios=True,
                          boot=470)
        try:
            self.assertTrue(s.save_loaded, "le fichier separe n'a meme pas ete lu")
            coin, clock = s.machine.battery_ram(), s.machine.rtc()
            s.handoff_reset(coin, clock)
            self.assertEqual(s.machine.read(SAVE_AT, 256), payload,
                             "le passage BIOS -> cartouche a efface la sauvegarde")
            self.assertFalse(s.machine.flash_dirty(),
                             "remettre la sauvegarde n'est pas une ecriture du jeu")
            s.machine.run_frames(60)
            self.assertEqual(s.machine.read(SAVE_AT, 256), payload,
                             "perdue une fois le jeu parti")
        finally:
            s.close()

    @unittest.skipUnless(RETAIL_BIOS.exists(), "needs the retail bios.bin (gitignored)")
    def test_the_hand_off_keeps_the_console_settings_without_touching_the_game(self):
        """L'AUTRE MOITIE DU MEME RAPPORT: « ca reinitialise mon BIOS a chaque jeu ».

        La remise a zero seme une page de reglages de mise sous tension, et
        `commit_system_ram` persiste la page VIVANTE -- donc la config du joueur etait
        remplacee par celle-la en sortant. Ce qui compte est donc ce qui ATTERRIT DANS LE
        FICHIER, pas ce qu'il y a en memoire.

        ⛔ ET LA MEMOIRE VIVE, ON N'Y TOUCHE PAS. Le premier correctif recopiait la page
        sauvegardee par-dessus la RAM, comme le fait le hand-off instantane -- et il A GELE
        LE JEU: mesure sur Bust-A-Move Pocket, 109 images distinctes tombees a 8, l'ecran
        titre sans sa ligne « push a button » et plus aucune touche active. C'est le
        symptome que le joueur a rapporte ensuite. Les deux chemins ne sont pas
        symetriques: ici la page sauvegardee est la RAM DE TRAVAIL VIVANTE du vrai BIOS,
        et la cartouche a droit a celle que la remise a zero vient de semer pour elle.
        La 2e assertion garde cette porte fermee.
        """
        cart = self.dir / "cell.ngc"
        cart.write_bytes(_rom(0x200000))
        flash = self.dir / "cell.flash"
        s = self._session(cart, flash, 0x200000, bios=RETAIL_BIOS, real_bios=True,
                          boot=470)
        s.ram_path = self.dir / "system.ram"      # jamais la vraie pile bouton
        s.rtc_path = self.dir / "system.rtc"
        marker = bytes([0x11, 0x22, 0x33, 0x44])
        try:
            s.machine.write(0x006DD8, marker)     # un reglage fait dans l'ecran du BIOS
            seeded_before = s.machine.read(0x006C00, 0x400)
            s.handoff_reset(s.machine.battery_ram(), s.machine.rtc())
            self.assertNotEqual(s.machine.read(0x006C00, 0x400), seeded_before,
                                "la page vivante devrait etre celle du reset")
            self.assertNotEqual(s.machine.read(0x006DD8, 4), marker,
                                "on a repose la page du BIOS sur la RAM du jeu: c'est ce "
                                "qui gelait la cartouche")
            self.assertTrue(s.commit_system_ram())
        finally:
            s.close()
        cell = (self.dir / "system.ram").read_bytes()
        off = 0x006DD8 - native.RAM_START
        self.assertEqual(cell[off:off + 4], marker,
                         "le lancement d'un jeu a efface les reglages de la console")


if __name__ == "__main__":
    unittest.main()
