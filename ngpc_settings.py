"""Shared settings backend for the modern shell (`ngpc_shell.py`).

All persistence goes through `QSettings("NgpCraft", "Emulator")` -- the same
scope the project already used -- so nothing here is Anthropic-branded and old
keys (last_dir/*, window/*) keep working. This module owns only the DATA and a
couple of reusable widgets; the modern shell owns the look.
"""

from __future__ import annotations

from PyQt6.QtCore import Qt, QSettings, pyqtSignal
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
    # have left, instantly. Turn it ON for the authentic power-on: the real BIOS
    # plays its intro and boots the game on its own, exactly like hardware. (An
    # unconfigured console stops at the first-boot setup once; after that it is
    # remembered.) Hand-off stays the default because it is instant.
    return bool(s.value("general/real_bios", False, type=bool))


def clock_mode(s: QSettings) -> str:
    """What the console's calendar clock does while the emulator is CLOSED.

    The mode ids live in `core.native_session` (which is what acts on them); this is
    only the stored preference. Defaults to `hardware` -- what a real coin cell does.
    """
    from core.native_session import CLOCK_HARDWARE, CLOCK_MODES
    m = str(s.value("bios/clock_mode", CLOCK_HARDWARE, type=str))
    return m if m in CLOCK_MODES else CLOCK_HARDWARE


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
    """Resolve the flash chip capacity for a ROM. Explicit setting wins; 'auto' presents
    any under-filled cart as a full 16 Mbit chip so a game can save in its chip's top block.
    Never smaller than the ROM.

    A cart's flash chip is a standard 4/8/16 Mbit part and is very often BIGGER than the ROM
    image burned onto it -- the game saves in the chip's top block, which can sit far above
    the ROM. Delta Warp is 512 KB (4 Mbit) of ROM yet programs its record save at ~1 MB
    offset (an 8 Mbit chip's block); StarGunner reaches block 33 (a 16 Mbit chip). Presenting
    such a cart at only its ROM size leaves that block missing, the program is a no-op, the
    game's read-back verify fails, and it shows "SAVE ERROR". So in auto mode ANY cart below
    16 Mbit is presented as a 16 Mbit chip (top filled with erased 0xFF, exactly like an
    under-filled flashcart); a full 2 MB ROM is kept as-is."""
    mode = flash_size_setting(s)
    if mode in _FLASH_BYTES:
        return max(rom_bytes, _FLASH_BYTES[mode])
    # auto: an under-filled cart is a small ROM on a (typically 16 Mbit) flash chip.
    if rom_bytes < 0x200000:
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


def library_sort(s: QSettings) -> str:
    import ngpc_library as lib
    v = str(s.value("library/sort", lib.SORT_NAME, type=str))
    return v if v in lib.SORT_KEYS else lib.SORT_NAME


def library_reverse(s: QSettings) -> bool:
    """Flip the sort. Each key already sorts the useful way round (A->Z, most
    played first), so this is where "least played" and "Z->A" come from."""
    return bool(s.value("library/sort_reverse", False, type=bool))


def library_filter(s: QSettings) -> str:
    import ngpc_library as lib
    v = str(s.value("library/filter", lib.FILTER_ALL, type=str))
    return v if v in lib.FILTERS else lib.FILTER_ALL


def language(s: QSettings) -> str:
    lang = s.value("general/language", "en", type=str)
    return lang if lang in dict(LANGUAGES) else "en"


def theme(s: QSettings) -> str:
    """Which UI theme to paint. Defaults to following the OS, so a fresh install
    matches the desktop the user already chose rather than imposing one."""
    import ngpc_theme as th
    val = s.value("general/theme", th.THEME_SYSTEM, type=str)
    return val if val in dict(th.THEMES) else th.THEME_SYSTEM


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


# --- turbo (autofire) -----------------------------------------------------
# Only the four action buttons can sensibly autofire; a turbo D-pad is a way to
# make a game unplayable, not a feature, so it is not offered.
TURBO_BUTTONS: tuple[str, ...] = ("A", "B")
TURBO_RATES: tuple[int, ...] = (5, 10, 15, 20)   # presses per second


def turbo_hz(s: QSettings) -> int:
    v = int(s.value("input/turbo_hz", 10, type=int))
    return v if v in TURBO_RATES else 10


def turbo_on(s: QSettings, label: str) -> bool:
    return bool(s.value(f"input/turbo_{label}", False, type=bool))


def set_turbo(s: QSettings, label: str, on: bool) -> None:
    s.setValue(f"input/turbo_{label}", bool(on))


def turbo_mask(s: QSettings) -> int:
    """The joypad bits that should autofire while held."""
    masks = dict(JOYPAD_BUTTONS)
    out = 0
    for label in TURBO_BUTTONS:
        if turbo_on(s, label):
            out |= masks.get(label, 0)
    return out


# --- gamepad --------------------------------------------------------------
def gamepad_enabled(s: QSettings) -> bool:
    """Read an XInput controller alongside the keyboard. On by default: a pad
    that is not plugged in costs one cheap poll and changes nothing."""
    return bool(s.value("input/gamepad", True, type=bool))


# --- hotkeys --------------------------------------------------------------
# The in-game hotkeys. Each is (action id, default key, name string). The action
# id is the settings key AND what `PlayPage` dispatches on, so adding one here is
# half the work of adding a hotkey -- the other half is a handler in the player.
#
# Hotkeys are matched BEFORE the joypad bindings, so a console button bound to a
# hotkey's key is silently dead: the hotkey eats the press and the game never
# sees it. `conflicts()` below exists to say so out loud.
#
# Ctrl+1..5 (window size) is deliberately absent: it needs a modifier, so it can
# never shadow a plain joypad key, and there is nothing to protect it from.
HK_MENU, HK_DEBUG, HK_PAUSE, HK_RESET = "menu", "debug", "pause", "reset"
HK_SAVE, HK_LOAD, HK_SLOT = "save", "load", "slot"
HK_FS, HK_SHOT, HK_TOOLBAR = "fs", "shot", "toolbar"
HK_FF, HK_FASTER, HK_SLOWER = "ff", "faster", "slower"
HK_REWIND, HK_STEP = "rewind", "step"

HOTKEYS: tuple[tuple[str, int, str], ...] = (
    (HK_MENU, int(Qt.Key.Key_Escape), "hkn_menu"),
    (HK_PAUSE, int(Qt.Key.Key_P), "hkn_pause"),
    (HK_RESET, int(Qt.Key.Key_F5), "hkn_reset"),
    (HK_SAVE, int(Qt.Key.Key_F2), "hkn_save"),
    (HK_LOAD, int(Qt.Key.Key_F4), "hkn_load"),
    (HK_SLOT, int(Qt.Key.Key_F3), "hkn_slot"),
    (HK_FF, int(Qt.Key.Key_Tab), "hkn_ff"),
    (HK_FASTER, int(Qt.Key.Key_BracketRight), "hkn_faster"),
    (HK_SLOWER, int(Qt.Key.Key_BracketLeft), "hkn_slower"),
    (HK_REWIND, int(Qt.Key.Key_Comma), "hkn_rewind"),
    (HK_STEP, int(Qt.Key.Key_Period), "hkn_step"),
    (HK_FS, int(Qt.Key.Key_F11), "hkn_fs"),
    (HK_SHOT, int(Qt.Key.Key_F12), "hkn_shot"),
    (HK_TOOLBAR, int(Qt.Key.Key_H), "hkn_toolbar"),
    (HK_DEBUG, int(Qt.Key.Key_F1), "hkn_debug"),
)

# Hotkeys that act on HOLD rather than on press: they need the key-release too.
HOLD_HOTKEYS = frozenset({HK_FF, HK_REWIND})

DEFAULT_HOTKEYS: dict[str, int] = {a: k for a, k, _n in HOTKEYS}
HOTKEY_NAMES: dict[str, str] = {a: n for a, _k, n in HOTKEYS}


def hotkey_code(s: QSettings, action: str) -> int:
    return int(s.value(f"hotkey/{action}", DEFAULT_HOTKEYS.get(action, 0), type=int))


def set_hotkey(s: QSettings, action: str, code: int) -> None:
    s.setValue(f"hotkey/{action}", int(code))


def hotkey_bindings(s: QSettings) -> dict[int, str]:
    """{Qt key code -> action id}. A key bound to two actions keeps the FIRST in
    HOTKEYS order, which is also the order the conflict report lists them in --
    so what the warning says matches what the player actually does."""
    out: dict[int, str] = {}
    for action, _default, _name in HOTKEYS:
        code = hotkey_code(s, action)
        if code and code not in out:
            out[code] = action
    return out


def hotkey_label(s: QSettings, action: str, lang: str) -> str:
    """'F5 (reset)' for the conflict messages."""
    key = QKeySequence(hotkey_code(s, action)).toString() or "?"
    return f"{key} ({tr(lang, HOTKEY_NAMES.get(action, action))})"


def conflicts(s: QSettings, lang: str) -> tuple[list[str], list[str]]:
    """Every ambiguous binding, as (joypad-vs-hotkey, hotkey-vs-hotkey) text.

    Both matter and they fail differently: a joypad button that collides with a
    hotkey never reaches the game, and two hotkeys on one key means one of them
    is unreachable.
    """
    hk = hotkey_bindings(s)
    pad_clashes = []
    for label, _mask in JOYPAD_BUTTONS:
        code = int(s.value(f"input/{label}", DEFAULT_KEYS.get(label, 0), type=int))
        action = hk.get(code)
        if action:
            pad_clashes.append(f"{label} → {hotkey_label(s, action, lang)}")

    seen: dict[int, str] = {}
    dupes = []
    for action, _default, _name in HOTKEYS:
        code = hotkey_code(s, action)
        if not code:
            continue
        if code in seen:
            dupes.append(f"{tr(lang, HOTKEY_NAMES[action])} → {hotkey_label(s, seen[code], lang)}")
        else:
            seen[code] = action
    return pad_clashes, dupes


# --- a button that captures the next keypress -----------------------------
class KeyCaptureButton(QPushButton):
    """Click -> shows the prompt -> the next key press becomes the binding.
    Emits `captured(new_code)` AFTER `_key` is updated so the owner persists the
    NEW value (never read `.key_code()` from an event filter -- that races the
    keyPressEvent below and saves the previous key)."""

    captured = pyqtSignal(int)   # emitted with the new key code once a capture completes

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
            changed = event.key() != int(Qt.Key.Key_Escape)
            if changed:
                self._key = int(event.key())
            self._grabbing = False
            self._render()
            if changed:
                self.captured.emit(self._key)   # persist the NEW code (see class docstring)
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
        "cat_bios": "Console (BIOS)",
        "cat_controls": "Controls", "rom_folder": "ROM folder", "bios": "BIOS image",
        # -- the console's own clock and coin cell
        "clock_mode": "Clock while the emulator is closed",
        "clk_hardware": "Keeps running (like real hardware)",
        "clk_host": "Follow the PC's clock",
        "clk_paused": "Stops, and resumes where it left off",
        "clock_mode_hint": "A real console's clock runs off its coin cell whether or not "
        "you are playing, so shutting the emulator for three days should bring it back "
        "three days later — that is the default. 'Follow the PC's clock' sets it from your "
        "computer at every launch: always right, but it overrides whatever date you set on "
        "the BIOS screen. 'Stops' freezes time with the emulator — not what hardware does, "
        "but it is reproducible, which is what you want when debugging.",
        "coin_cell": "Coin cell (console memory)",
        "coin_cell_hint": "One battery keeps BOTH the BIOS settings (language, colour) and "
        "the clock alive. Resetting it is pulling that battery out: the console forgets its "
        "language and date and runs its first-boot setup again, exactly like a brand-new "
        "machine. Your games and their saves are NOT touched.",
        "coin_cell_reset": "Reset the console",
        "coin_cell_confirm_title": "Reset the console?",
        "coin_cell_confirm": "This clears the BIOS settings (language, colour) and the "
        "clock, and the console will run its first-boot setup again.\n\nYour games and "
        "their saves are not affected.\n\nReset it?",
        "coin_cell_done": "Console reset — it will boot as new.",
        "coin_cell_empty": "Nothing to reset: this console has no saved settings yet.",
        "coin_cell_busy": "Quit the game first — the console is running.",
        "language": "Language", "real_bios": "Boot the real BIOS at power-on",
        "theme": "Theme", "theme_system": "Follow Windows",
        "theme_dark": "Dark", "theme_light": "Light",
        "btn_power": "POWER", "btn_up": "UP", "btn_down": "DOWN",
        "btn_left": "LEFT", "btn_right": "RIGHT",
        "btn_option": "OPTION", "btn_a": "A", "btn_b": "B",
        "press_key": "press a key…",
        "power_managed": "handled by the console",
        "lcd_scale": "Window scale", "smoothing": "Smooth scaling", "scanlines":
        "Scanline overlay", "audio_on": "Enable audio", "volume": "Volume",
        "controls_hint": "Click a button, then press the key to bind it.",
        "browse": "Browse…", "restore": "Restore defaults",
        "view_grid": "Grid", "view_list": "List", "view_compact": "Compact",
        "thumb_size": "Cover size", "boot_bios": "Boot BIOS",
        "console_boot": "Play the console boot (BIOS) before the game",
        "console_boot_hint": "Powers the console on for real: the Neo Geo Pocket BIOS "
        "plays its intro, then boots the game on its own — just like hardware. A brand-new "
        "console configures itself the first time (first-boot setup auto-completed with "
        "defaults and remembered), so you always get intro → game with no setup to click "
        "through. Leave OFF to hand the cartridge straight to the game (instant).",
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
        # (the old hk_* cheat-sheet lines named their keys literally -- "F5 — reset".
        # The Hotkeys panel now BINDS them and shows the live key, so those strings
        # would have become quietly wrong the first time anyone rebound anything.)
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
        # -- library: search / sort / filter
        "search": "Search…", "sort": "Sort",
        "sort_name": "Name", "sort_last": "Last played", "sort_plays": "Most played",
        "sort_time": "Playtime", "sort_added": "Recently added", "sort_size": "Size",
        "sort_reverse": "Reverse the order",
        "filter_all": "All", "filter_fav": "Favourites", "filter_never": "Never played",
        "no_match": "No game matches this search.",
        "fav_add": "Add to favourites", "fav_remove": "Remove from favourites",
        "never_played": "Never played", "plays_n": "{n}×",
        # -- turbo / gamepad
        "turbo": "Turbo (autofire) on {btn}",
        "turbo_rate": "Turbo rate",
        "turbo_hz": "{n} per second",
        "turbo_hint": "Holding a turbo button fires it repeatedly. The rate is counted "
        "in console frames, so it stays the same under fast-forward.",
        "gamepad": "Use a controller",
        "gamepad_hint": "Reads an Xbox-style (XInput) controller alongside the keyboard. "
        "D-pad and left stick move; A/X and B/Y are the two console buttons; Start or "
        "Back is Option. Windows only — elsewhere this does nothing.",
        "pad_on": "Controller detected", "pad_off": "No controller detected",
        "pad_none": "Controller support unavailable on this system",
        "key_conflict": "⚠ This key is already {hk}. In game the hotkey wins and this "
        "button will not respond.",
        # short hotkey names, for the conflict warning above
        "hkn_menu": "menu", "hkn_debug": "debug tools", "hkn_save": "save state",
        "hkn_slot": "slot", "hkn_load": "load state", "hkn_reset": "reset",
        "hkn_fs": "fullscreen", "hkn_shot": "screenshot", "hkn_pause": "pause",
        "hkn_toolbar": "toolbar", "hkn_ff": "fast-forward", "hkn_slower": "slower",
        "hkn_faster": "faster", "hkn_rewind": "rewind", "hkn_step": "frame step",
        "hotkeys_hint": "Click a hotkey, then press the key to bind it. "
        "Ctrl+1…5 always sets the window size and cannot be rebound.",
        "hk_dupe": "⚠ Two hotkeys share a key: {hk}. Only the first one will fire.",
    },
    "fr": {
        "library": "Bibliothèque", "settings": "Réglages", "no_roms":
        "Aucune ROM trouvée. Indiquez votre dossier de ROMs dans les Réglages.",
        "open_rom": "Ouvrir une ROM…", "set_folder": "Choisir le dossier de ROMs…",
        "play": "Jouer", "resume": "Reprendre", "reset": "Réinitialiser",
        "quit_lib": "Retour à la bibliothèque", "paused": "En pause",
        "cat_general": "Général", "cat_graphics": "Graphismes", "cat_audio": "Audio",
        "cat_bios": "Console (BIOS)",
        "cat_controls": "Commandes", "rom_folder": "Dossier des ROMs",
        "bios": "Image BIOS", "language": "Langue",
        "theme": "Thème", "theme_system": "Suivre Windows",
        "theme_dark": "Sombre", "theme_light": "Clair",
        "btn_power": "POWER", "btn_up": "HAUT", "btn_down": "BAS",
        "btn_left": "GAUCHE", "btn_right": "DROITE",
        "btn_option": "OPTION", "btn_a": "A", "btn_b": "B",
        "press_key": "appuyez sur une touche…",
        "power_managed": "géré par la console",
        # -- l'horloge et la pile bouton de la console
        "clock_mode": "Horloge quand l'émulateur est fermé",
        "clk_hardware": "Continue de tourner (comme le vrai matériel)",
        "clk_host": "Suivre l'horloge du PC",
        "clk_paused": "S'arrête, et reprend où elle en était",
        "clock_mode_hint": "Sur une vraie console, la pile bouton fait tourner l'horloge "
        "que vous jouiez ou non : fermer l'émulateur trois jours devrait donc la retrouver "
        "trois jours plus tard — c'est le réglage par défaut. « Suivre l'horloge du PC » la "
        "règle sur votre ordinateur à chaque lancement : toujours juste, mais ça écrase la "
        "date que vous aviez mise dans le BIOS. « S'arrête » fige le temps avec l'émulateur "
        "— ce n'est pas le comportement du matériel, mais c'est reproductible, ce qu'on veut "
        "pour déboguer.",
        "coin_cell": "Pile bouton (mémoire de la console)",
        "coin_cell_hint": "Une seule pile garde EN VIE les réglages du BIOS (langue, "
        "couleur) ET l'horloge. La réinitialiser, c'est retirer cette pile : la console "
        "oublie sa langue et sa date et refait sa configuration de premier démarrage, comme "
        "une machine neuve. Vos jeux et leurs sauvegardes ne sont PAS touchés.",
        "coin_cell_reset": "Réinitialiser la console",
        "coin_cell_confirm_title": "Réinitialiser la console ?",
        "coin_cell_confirm": "Ceci efface les réglages du BIOS (langue, couleur) et "
        "l'horloge, et la console refera sa configuration de premier démarrage.\n\nVos jeux "
        "et leurs sauvegardes ne sont pas affectés.\n\nRéinitialiser ?",
        "coin_cell_done": "Console réinitialisée — elle démarrera comme neuve.",
        "coin_cell_empty": "Rien à réinitialiser : cette console n'a pas encore de réglages.",
        "coin_cell_busy": "Quittez le jeu d'abord — la console tourne.",
        "real_bios": "Démarrer le vrai BIOS à l'allumage", "lcd_scale":
        "Échelle de la fenêtre", "smoothing": "Lissage", "scanlines":
        "Effet lignes de balayage", "audio_on": "Activer l'audio", "volume": "Volume",
        "controls_hint": "Cliquez un bouton, puis appuyez sur la touche à assigner.",
        "browse": "Parcourir…", "restore": "Valeurs par défaut",
        "view_grid": "Grille", "view_list": "Liste", "view_compact": "Compact",
        "thumb_size": "Taille des vignettes", "boot_bios": "Lancer le BIOS",
        "console_boot": "Jouer le démarrage console (BIOS) avant le jeu",
        "console_boot_hint": "Allume vraiment la console : le BIOS du Neo Geo Pocket joue "
        "son intro, puis lance le jeu tout seul — comme sur le hardware. Une console neuve "
        "se configure toute seule au premier lancement (premier démarrage auto-complété avec "
        "les réglages par défaut et mémorisé) : tu as toujours intro → jeu, sans écran de "
        "réglage à valider. Laissez DÉSACTIVÉ pour donner la cartouche directement au jeu.",
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
        "cat_hotkeys": "Raccourcis",
        # (voir la note côté anglais : les anciennes lignes hk_* citaient les touches
        # en dur et seraient devenues fausses dès le premier remap.)
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
        # -- bibliothèque : recherche / tri / filtre
        "search": "Rechercher…", "sort": "Trier",
        "sort_name": "Nom", "sort_last": "Dernier joué", "sort_plays": "Plus joué",
        "sort_time": "Temps de jeu", "sort_added": "Ajouté récemment", "sort_size": "Taille",
        "sort_reverse": "Inverser l'ordre",
        "filter_all": "Tous", "filter_fav": "Favoris", "filter_never": "Jamais joué",
        "no_match": "Aucun jeu ne correspond à cette recherche.",
        "fav_add": "Ajouter aux favoris", "fav_remove": "Retirer des favoris",
        "never_played": "Jamais joué", "plays_n": "{n}×",
        # -- turbo / manette
        "turbo": "Turbo (tir auto) sur {btn}",
        "turbo_rate": "Cadence du turbo",
        "turbo_hz": "{n} par seconde",
        "turbo_hint": "Maintenir un bouton turbo l'enchaîne automatiquement. La cadence "
        "est comptée en images console : elle reste la même en avance rapide.",
        "gamepad": "Utiliser une manette",
        "gamepad_hint": "Lit une manette de type Xbox (XInput) en plus du clavier. Croix "
        "directionnelle et stick gauche pour se déplacer ; A/X et B/Y sont les deux boutons "
        "de la console ; Start ou Back fait Option. Windows uniquement — ailleurs, sans effet.",
        "pad_on": "Manette détectée", "pad_off": "Aucune manette détectée",
        "pad_none": "Manette non prise en charge sur ce système",
        "key_conflict": "⚠ Cette touche est déjà {hk}. En jeu le raccourci gagne et ce "
        "bouton ne répondra pas.",
        # noms courts des raccourcis, pour l'avertissement ci-dessus
        "hkn_menu": "menu", "hkn_debug": "outils debug", "hkn_save": "sauver l'état",
        "hkn_slot": "emplacement", "hkn_load": "charger l'état", "hkn_reset": "réinitialiser",
        "hkn_fs": "plein écran", "hkn_shot": "capture", "hkn_pause": "pause",
        "hkn_toolbar": "barre d'outils", "hkn_ff": "avance rapide", "hkn_slower": "ralentir",
        "hkn_faster": "accélérer", "hkn_rewind": "rembobiner", "hkn_step": "image par image",
        "hotkeys_hint": "Cliquez un raccourci, puis appuyez sur la touche à assigner. "
        "Ctrl+1…5 règle toujours la taille de la fenêtre et n'est pas remappable.",
        "hk_dupe": "⚠ Deux raccourcis partagent une touche : {hk}. Seul le premier agira.",
    },
}


def tr(lang: str, key: str) -> str:
    return STRINGS.get(lang, STRINGS["en"]).get(key, STRINGS["en"].get(key, key))
