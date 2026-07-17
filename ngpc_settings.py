"""Shared settings backend for the modern shell (`ngpc_shell.py`).

All persistence goes through `QSettings("NgpCraft", "Emulator")` -- the same
scope the project already used -- so nothing here is Anthropic-branded and old
keys (last_dir/*, window/*) keep working. This module owns only the DATA and a
couple of reusable widgets; the modern shell owns the look.
"""

from __future__ import annotations

from PyQt6.QtCore import Qt, QSettings
from PyQt6.QtGui import QKeySequence
from PyQt6.QtWidgets import QPushButton

SETTINGS_ORG = "NgpCraft"
SETTINGS_APP = "Emulator"

# The seven NGPC buttons, in display order. int = joypad MASK bit (matches the
# on-chip 0xB0 register / EmulatorSession constants). Settings are keyed on the
# STABLE label so a mask change never orphans a saved binding.
JOYPAD_BUTTONS: tuple[tuple[str, int], ...] = (
    ("Up", 0x01), ("Down", 0x02), ("Left", 0x04), ("Right", 0x08),
    ("A", 0x10), ("B", 0x20), ("Option", 0x40),
)

DEFAULT_KEYS: dict[str, int] = {
    "Up": int(Qt.Key.Key_Up), "Down": int(Qt.Key.Key_Down),
    "Left": int(Qt.Key.Key_Left), "Right": int(Qt.Key.Key_Right),
    "A": int(Qt.Key.Key_X), "B": int(Qt.Key.Key_C),
    "Option": int(Qt.Key.Key_Return),
}

LANGUAGES: tuple[tuple[str, str], ...] = (("en", "English"), ("fr", "Français"))


def make_settings() -> QSettings:
    return QSettings(SETTINGS_ORG, SETTINGS_APP)


# --- typed accessors ------------------------------------------------------
def rom_folder(s: QSettings) -> str:
    return s.value("paths/rom_folder", "", type=str)


def bios_path(s: QSettings) -> str:
    return s.value("paths/bios", "", type=str)


def lcd_scale(s: QSettings) -> int:
    return int(s.value("gfx/lcd_scale", 3, type=int))


def smoothing(s: QSettings) -> bool:
    return bool(s.value("gfx/smoothing", False, type=bool))


def crt_scanlines(s: QSettings) -> bool:
    return bool(s.value("gfx/scanlines", False, type=bool))


# -- video filter / colour / aspect (see ngpc_video.py for the ids) --
def video_filter(s: QSettings) -> str:
    import ngpc_video as v
    val = s.value("gfx/filter", v.FILTER_NONE, type=str)
    return val if val in v.FILTERS else v.FILTER_NONE


def color_profile(s: QSettings) -> str:
    import ngpc_video as v
    val = s.value("gfx/color", v.COLOR_RAW, type=str)
    return val if val in v.COLOR_PROFILES else v.COLOR_RAW


def aspect_mode(s: QSettings) -> str:
    import ngpc_video as v
    val = s.value("gfx/aspect", v.ASPECT_PIXEL, type=str)
    return val if val in v.ASPECTS else v.ASPECT_PIXEL


def fullscreen(s: QSettings) -> bool:
    return bool(s.value("gfx/fullscreen", False, type=bool))


def audio_enabled(s: QSettings) -> bool:
    return bool(s.value("audio/enabled", True, type=bool))


def audio_volume(s: QSettings) -> int:
    return int(s.value("audio/volume", 80, type=int))


def real_bios(s: QSettings) -> bool:
    # Default OFF (hand-off): the cartridge gets the state the BIOS boot would
    # have left, instantly. The real-BIOS boot IS available and now shows the
    # authentic power-on screens (the "SUB BATTERY DEAD" bug is fixed), but the
    # BIOS does not yet HAND OFF to the cartridge -- it stops at its own setup
    # screens. So hand-off stays the default
    # for launching a game; "Boot BIOS" shows the BIOS by itself.
    return bool(s.value("general/real_bios", False, type=bool))


def show_fps(s: QSettings) -> bool:
    """Show the on-screen FPS / speed readout over the game."""
    return bool(s.value("gfx/show_fps", False, type=bool))


# How in-game saves are persisted.
SAVE_ROM, SAVE_SIDECAR, SAVE_BOTH = "rom", "sidecar", "both"
SAVE_MODES = (SAVE_ROM, SAVE_SIDECAR, SAVE_BOTH)


# Cart flash chip capacity. A real flashcart's chip is a standard 4/8/16 Mbit part,
# often bigger than an under-filled homebrew ROM; a game that saves in the chip's top
# block needs that block to exist (StarGunner -> block 33, needs a 16 Mbit chip).
FLASH_AUTO, FLASH_4M, FLASH_8M, FLASH_16M = "auto", "4m", "8m", "16m"
FLASH_SIZES = (FLASH_AUTO, FLASH_4M, FLASH_8M, FLASH_16M)
_FLASH_BYTES = {FLASH_4M: 0x080000, FLASH_8M: 0x100000, FLASH_16M: 0x200000}


def flash_size_setting(s: QSettings) -> str:
    m = str(s.value("general/flash_size", FLASH_AUTO, type=str))
    return m if m in FLASH_SIZES else FLASH_AUTO


def flash_capacity_bytes(s: QSettings, rom_bytes: int) -> int:
    """Resolve the flash chip capacity for a ROM. Explicit setting wins; 'auto' keeps a
    full-size commercial ROM as-is but presents a tiny homebrew ROM as a 16 Mbit flashcart
    (so it can save in its top block). Never smaller than the ROM."""
    mode = flash_size_setting(s)
    if mode in _FLASH_BYTES:
        return max(rom_bytes, _FLASH_BYTES[mode])
    # auto: a ROM well under a 4 Mbit chip is homebrew on a (typically 16 Mbit) flashcart.
    if rom_bytes < 0x040000:
        return 0x200000
    return rom_bytes


REWIND_CHOICES = (0, 10, 20, 30)          # seconds of history the UI offers


def rewind_seconds(s: QSettings) -> int:
    """Seconds of frame-perfect rewind history to keep (0 = off). Each frame snapshot is
    ~48 KiB, so 10 s ~= 29 MB, 20 s ~= 58 MB, 30 s ~= 86 MB."""
    try:
        v = int(s.value("debug/rewind_seconds", 10))
    except (ValueError, TypeError):
        v = 10
    return v if v in REWIND_CHOICES else 10


def save_mode(s: QSettings) -> str:
    """Where a game's own save goes: 'rom' = into the .ngc (like a real cartridge's flash),
    'sidecar' = a separate saves/<rom>.flash (ROM untouched), 'both' = ROM + a backup file."""
    m = str(s.value("general/save_mode", SAVE_ROM, type=str))
    return m if m in SAVE_MODES else SAVE_ROM


def screenshot_dir(s: QSettings) -> str:
    """Folder screenshots (F12) are written to. Empty -> the shell's default."""
    return str(s.value("paths/screenshots", "", type=str))


def cart_wait_states(s: QSettings) -> bool:
    # Default ON: model the slow cartridge flash. Every instruction is fetched
    # from the cart and data tables are read from it, and that bus is slow. With
    # free cart access the CPU runs cart code ~3.4x too fast, which makes
    # self-timed games (Cool Boarders, Densha de Go) fit their frame work into one
    # VBlank and run at 60fps instead of the 30fps they show on real hardware
    # (the in-game timer then counts ~2x too fast). Calibrated on silicon by
    # hw_calibration/cpu_calib_v1.ngc (fetch=3) plus Cool Boarders' confirmed
    # 30fps (data=5). VBlank-locked games (Fatal Fury) are unaffected. Turn OFF
    # to compare against the old free-fetch timing.
    return bool(s.value("general/cart_wait_states", True, type=bool))


# Silicon-calibrated cart-flash wait-states (cycles per byte). Only instruction
# FETCH is wait-stated: cpu_calib_v2 on real hardware showed a random cart DATA
# read (CRND) costs exactly the same as a RAM read (RRND), so data = 0. (An earlier
# data=5 was a curve-fit to Cool Boarders and the v2 ROM refuted it.) See
# cart_wait_states().
CART_FETCH_WAIT = 3
CART_DATA_WAIT = 0
# LDIR/LDDR block-copy cost per byte. Datasheet 7 leaves self-timed games ~20% too fast;
# 14 puts Cool Boarders at its hardware 30fps and leaves Fatal Fury at 60. Strongly
# evidenced (one fix, both games; datasheet was a floor for MUL/DIV too) but not yet
# confirmed by a clean calibration ROM -- verify the in-game timer against a stopwatch.
CART_LDIR_COST = 14


# -- library view --
VIEW_GRID, VIEW_LIST, VIEW_COMPACT = "grid", "list", "compact"


def library_view(s: QSettings) -> str:
    v = s.value("library/view", VIEW_GRID, type=str)
    return v if v in (VIEW_GRID, VIEW_LIST, VIEW_COMPACT) else VIEW_GRID


def thumb_size(s: QSettings) -> int:
    """Long edge of the cover art in the library, in px (80..240)."""
    return max(80, min(240, int(s.value("library/thumb_size", 160, type=int))))


def language(s: QSettings) -> str:
    lang = s.value("general/language", "en", type=str)
    return lang if lang in dict(LANGUAGES) else "en"


def key_bindings(s: QSettings) -> dict[int, int]:
    """{Qt key code -> joypad mask}, from settings or defaults."""
    out: dict[int, int] = {}
    for label, mask in JOYPAD_BUTTONS:
        code = int(s.value(f"input/{label}", DEFAULT_KEYS.get(label, 0), type=int))
        if code:
            out[code] = mask
    if out.get(int(Qt.Key.Key_Return)) == 0x40:
        out[int(Qt.Key.Key_Enter)] = 0x40   # keep Enter == Option convenience
    return out


def set_binding(s: QSettings, label: str, code: int) -> None:
    s.setValue(f"input/{label}", int(code))


# --- a button that captures the next keypress -----------------------------
class KeyCaptureButton(QPushButton):
    """Click -> shows the prompt -> the next key press becomes the binding.
    Emits nothing; read `.key_code()` (the owner persists it)."""

    def __init__(self, key_code: int, prompt: str = "press a key…") -> None:
        super().__init__()
        self._prompt = prompt
        self._key = int(key_code)
        self._grabbing = False
        self.setCheckable(False)
        self._render()
        self.clicked.connect(self._begin)

    def key_code(self) -> int:
        return self._key

    def set_prompt(self, prompt: str) -> None:
        self._prompt = prompt

    def _render(self) -> None:
        self.setText(QKeySequence(self._key).toString() or f"0x{self._key:X}")

    def _begin(self) -> None:
        self._grabbing = True
        self.setText(self._prompt)
        self.setFocus()

    def keyPressEvent(self, event) -> None:  # type: ignore[override]
        if self._grabbing:
            if event.key() != int(Qt.Key.Key_Escape):
                self._key = int(event.key())
            self._grabbing = False
            self._render()
            return
        super().keyPressEvent(event)


# --- localization table (grows over time; keys are stable) ----------------
STRINGS: dict[str, dict[str, str]] = {
    "en": {
        "library": "Library", "settings": "Settings", "no_roms":
        "No ROMs found. Set your ROM folder in Settings.", "open_rom": "Open ROM…",
        "set_folder": "Choose ROM folder…", "play": "Play", "resume": "Resume",
        "reset": "Reset", "quit_lib": "Quit to Library", "paused": "Paused",
        "cat_general": "General", "cat_graphics": "Graphics", "cat_audio": "Audio",
        "cat_controls": "Controls", "rom_folder": "ROM folder", "bios": "BIOS image",
        "language": "Language", "real_bios": "Boot the real BIOS at power-on",
        "lcd_scale": "Window scale", "smoothing": "Smooth scaling", "scanlines":
        "Scanline overlay", "audio_on": "Enable audio", "volume": "Volume",
        "controls_hint": "Click a button, then press the key to bind it.",
        "browse": "Browse…", "restore": "Restore defaults",
        "view_grid": "Grid", "view_list": "List", "view_compact": "Compact",
        "thumb_size": "Cover size", "boot_bios": "Boot BIOS",
        "console_boot": "Play the console boot (BIOS) before the game — experimental",
        "console_boot_hint": "Runs the Neo Geo Pocket's own power-on screens "
        "(language, clock). ⚠ The BIOS does not yet hand off to the cartridge, so a "
        "game launched this way stops at the setup screens. Leave OFF to hand the "
        "cartridge straight to the game (instant). Use \"Boot BIOS\" to see the "
        "BIOS by itself.",
        "cart_wait": "Cartridge flash timing (real hardware speed)",
        "cart_wait_hint": "Models the slow cartridge flash bus + block-copy timing. Without "
        "it the CPU runs cart code ~3.4x too fast, so self-timed games (Cool Boarders, Densha "
        "de Go) run at 60fps instead of the 30fps a real console shows — their in-game timer "
        "then counts ~2x too fast. Fetch timing is silicon-confirmed; the LDIR block-copy part "
        "is strongly evidenced but not yet ROM-confirmed, so check the in-game timer against a "
        "stopwatch. Leave ON; turn OFF to compare with the old (too-fast) timing.",
        # video
        "filter": "Screen filter", "flt_none": "None", "flt_scanlines": "Scanlines",
        "flt_lcdgrid": "LCD grid", "flt_crt": "CRT",
        "color_profile": "Colour", "col_raw": "Raw", "col_lcd": "LCD", "col_vivid": "Vivid",
        "aspect": "Aspect", "asp_pixel": "Pixel-perfect", "asp_fit": "Fit window",
        "asp_stretch": "Stretch", "fullscreen": "Fullscreen",
        # in-game menu / hotkeys
        "menu_title": "Paused", "m_resume": "Resume", "m_reset": "Reset",
        "m_video": "Video & filters", "m_audio": "Audio", "m_controls": "Controls",
        "m_debug": "Debug tools", "m_quit": "Quit to library", "cat_hotkeys": "Hotkeys",
        "m_savestate": "Save state (slot {n})", "m_loadstate": "Load state (slot {n})",
        "hk_intro": "In game:", "hk_menu": "Esc — pause menu", "hk_pause": "P — pause",
        "hk_reset": "F5 — reset", "hk_fs": "F11 — fullscreen",
        "hk_size": "Ctrl+1…5 — window size 1×…5×", "hk_debug": "F1 — debug tools",
        "hk_state": "F2/F4 — save/load state · F3 — slot",
        "hk_speed": "Tab — fast-forward (hold) · [ ] — slower/faster",
        "hk_shot": "F12 — screenshot",
        "shot_saved": "Saved {name}", "screenshots": "Screenshots folder",
        "show_fps": "Show FPS overlay",
        "save_mode": "In-game save", "save_rom": "In the ROM (.ngc)",
        "save_sidecar": "Separate file", "save_both": "ROM + separate file",
        "flash_size": "Cart flash size", "flash_auto": "Auto",
        "flash_4m": "4 Mbit (512 KB)", "flash_8m": "8 Mbit (1 MB)", "flash_16m": "16 Mbit (2 MB)",
        "rewind": "Rewind buffer", "rewind_off": "Off",
        "rewind_10": "10 s (~29 MB)", "rewind_20": "20 s (~58 MB)", "rewind_30": "30 s (~86 MB)",
        "slot": "Slot {n}", "state_saved": "State {n} saved",
        "state_loaded": "State {n} loaded", "state_empty": "Slot {n} empty",
        "speed": "Speed {x}x",
        "saves_folder": "Saves folder",
    },
    "fr": {
        "library": "Bibliothèque", "settings": "Réglages", "no_roms":
        "Aucune ROM trouvée. Indiquez votre dossier de ROMs dans les Réglages.",
        "open_rom": "Ouvrir une ROM…", "set_folder": "Choisir le dossier de ROMs…",
        "play": "Jouer", "resume": "Reprendre", "reset": "Réinitialiser",
        "quit_lib": "Retour à la bibliothèque", "paused": "En pause",
        "cat_general": "Général", "cat_graphics": "Graphismes", "cat_audio": "Audio",
        "cat_controls": "Commandes", "rom_folder": "Dossier des ROMs",
        "bios": "Image BIOS", "language": "Langue",
        "real_bios": "Démarrer le vrai BIOS à l'allumage", "lcd_scale":
        "Échelle de la fenêtre", "smoothing": "Lissage", "scanlines":
        "Effet lignes de balayage", "audio_on": "Activer l'audio", "volume": "Volume",
        "controls_hint": "Cliquez un bouton, puis appuyez sur la touche à assigner.",
        "browse": "Parcourir…", "restore": "Valeurs par défaut",
        "view_grid": "Grille", "view_list": "Liste", "view_compact": "Compact",
        "thumb_size": "Taille des vignettes", "boot_bios": "Lancer le BIOS",
        "console_boot": "Jouer le démarrage console (BIOS) avant le jeu — expérimental",
        "console_boot_hint": "Joue les écrans d'allumage du Neo Geo Pocket (langue, "
        "horloge). ⚠ Le BIOS ne cède pas encore la main à la cartouche : un jeu lancé "
        "ainsi s'arrête sur les écrans de réglage. Laissez DÉSACTIVÉ pour donner la "
        "cartouche directement au jeu (instantané). « Lancer le BIOS » montre le BIOS "
        "seul.",
        "cart_wait": "Timing du flash cartouche (vitesse console réelle)",
        "cart_wait_hint": "Modélise le bus flash lent + le timing des copies bloc. Sans lui "
        "le CPU exécute le code cartouche ~3,4× trop vite : les jeux auto-cadencés (Cool "
        "Boarders, Densha de Go) tournent à 60fps au lieu des 30fps de la vraie console — "
        "leur chrono compte ~2× trop vite. Le timing fetch est confirmé sur silicium ; la "
        "partie LDIR (copie bloc) est fortement étayée mais pas encore confirmée par ROM — "
        "vérifie le chrono du jeu avec un chronomètre. Laissez ACTIVÉ ; désactivez pour "
        "comparer avec l'ancien timing (trop rapide).",
        # video
        "filter": "Filtre d'écran", "flt_none": "Aucun", "flt_scanlines": "Scanlines",
        "flt_lcdgrid": "Grille LCD", "flt_crt": "CRT",
        "color_profile": "Couleur", "col_raw": "Brut", "col_lcd": "LCD", "col_vivid": "Vif",
        "aspect": "Ratio", "asp_pixel": "Pixel-perfect", "asp_fit": "Ajuster",
        "asp_stretch": "Étirer", "fullscreen": "Plein écran",
        # in-game menu / hotkeys
        "menu_title": "En pause", "m_resume": "Reprendre", "m_reset": "Réinitialiser",
        "m_video": "Vidéo & filtres", "m_audio": "Audio", "m_controls": "Commandes",
        "m_debug": "Outils debug", "m_quit": "Retour à la bibliothèque",
        "m_savestate": "Sauvegarder l'état (empl. {n})", "m_loadstate": "Charger l'état (empl. {n})",
        "cat_hotkeys": "Raccourcis", "hk_intro": "En jeu :", "hk_menu": "Échap — menu pause",
        "hk_pause": "P — pause", "hk_reset": "F5 — réinitialiser",
        "hk_fs": "F11 — plein écran", "hk_size": "Ctrl+1…5 — taille fenêtre 1×…5×",
        "hk_debug": "F1 — outils debug",
        "hk_state": "F2/F4 — sauver/charger l'état · F3 — emplacement",
        "hk_speed": "Tab — avance rapide (maintenir) · [ ] — plus lent/rapide",
        "hk_shot": "F12 — capture d'écran",
        "shot_saved": "Enregistré {name}", "screenshots": "Dossier des captures",
        "show_fps": "Afficher le FPS",
        "save_mode": "Sauvegarde du jeu", "save_rom": "Dans la ROM (.ngc)",
        "save_sidecar": "Fichier séparé", "save_both": "ROM + fichier séparé",
        "flash_size": "Taille flash cart", "flash_auto": "Auto",
        "flash_4m": "4 Mbit (512 Ko)", "flash_8m": "8 Mbit (1 Mo)", "flash_16m": "16 Mbit (2 Mo)",
        "rewind": "Tampon rembobinage", "rewind_off": "Désactivé",
        "rewind_10": "10 s (~29 Mo)", "rewind_20": "20 s (~58 Mo)", "rewind_30": "30 s (~86 Mo)",
        "slot": "Emplacement {n}", "state_saved": "État {n} sauvé",
        "state_loaded": "État {n} chargé", "state_empty": "Emplacement {n} vide",
        "speed": "Vitesse {x}x",
        "saves_folder": "Dossier des sauvegardes",
    },
}


def tr(lang: str, key: str) -> str:
    return STRINGS.get(lang, STRINGS["en"]).get(key, STRINGS["en"].get(key, key))
