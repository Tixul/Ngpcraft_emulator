"""Shared settings backend for the modern shell (`ngpc_shell.py`).

All persistence goes through `QSettings("NgpCraft", "Emulator")` -- the same
scope the project already used -- so nothing here is Anthropic-branded and old
keys (last_dir/*, window/*) keep working. This module owns only the DATA and a
couple of reusable widgets; the modern shell owns the look.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

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

# LANGUAGES / STRINGS are built at the bottom, from lang/*.json.


SETTINGS_FILE_ENV = "NGPCRAFT_SETTINGS"


def make_settings() -> QSettings:
    """The one way to reach stored settings: the user scope (the registry, on Windows),
    or the .ini named by `NGPCRAFT_SETTINGS` when that is set.

    The override exists because the test suite CLEARS this store around every test, and
    there is no other way to keep it off the real one: `QSettings.setDefaultFormat` is
    documented to steer the (organization, application) constructor and, on this Qt
    build, does not -- only the explicit (format, scope, ...) form honours a redirect.
    So it has to happen here. See `pytest_configure` in the root conftest."""
    override = os.environ.get(SETTINGS_FILE_ENV)
    if override:
        return QSettings(override, QSettings.Format.IniFormat)
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


def fs_hide_ui(s: QSettings) -> bool:
    """Hide the sidebar and player toolbar while fullscreen -- the game alone. Default
    on: fullscreen with chrome around it is not what 'fullscreen' usually means."""
    return bool(s.value("gfx/fs_hide_ui", True, type=bool))


def toolbar_autohide(s: QSettings) -> bool:
    """Hide the player toolbar after the mouse goes still, bring it back on any move.
    On by default -- it stays available without sitting over the game the whole time."""
    return bool(s.value("gfx/toolbar_autohide", True, type=bool))


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


# How a MONOCHROME (NGP) cartridge is run. The NGPC tells a cartridge it is a colour
# console (0x6F91) and a colour-aware mono game -- Samurai Shodown -- then paints its own
# palette: green bamboo, characters near their canonical colours. An original NGP has
# neither, so the same cartridge stays in eight greys. Both are real machines.
MONO_K2GE, MONO_K1GE = "k2ge", "k1ge"
MONO_MODES = (MONO_K2GE, MONO_K1GE)


def mono_mode(s: QSettings) -> str:
    """Which console a black-and-white NGP cartridge runs on. Defaults to the NGPC
    (colourised), which is the hardware this emulator is."""
    m = str(s.value("gfx/mono_mode", MONO_K2GE, type=str))
    return m if m in MONO_MODES else MONO_K2GE


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
    """The UI language, validated against what lang/ actually ships. A saved code
    whose file went away falls back to English rather than showing raw keys."""
    lang = s.value("general/language", FALLBACK_LANG, type=str)
    return lang if lang in STRINGS else FALLBACK_LANG


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


# --- localization ---------------------------------------------------------
# One JSON file per language in lang/, discovered at import time: shipping a new
# language is dropping a file in there, no code change. See TRANSLATING.md.
#
# Keys are stable and the table grows over time; a language that misses some of
# them is fine -- `tr` falls back to English key by key, so a half-finished
# translation is still mergeable.
#
# (Why hkn_* and not literal cheat-sheet lines: the old hk_* strings named their
# keys in the text -- "F5 - reset". The Hotkeys panel now BINDS them and shows
# the live key, so those strings would have gone quietly wrong the first time
# anyone rebound anything.)

FALLBACK_LANG = "en"

# Frozen in the .exe the JSONs are extracted next to the code, under _MEIPASS;
# from source they sit beside this file. Both are read-only either way -- a
# translator sends the file and it goes in the repo, so there is deliberately no
# user-editable lang/ folder next to the .exe to keep in sync with this one.
LANG_DIR = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent)) / "lang"


def load_languages(directory: Path | None = None) -> tuple[
        dict[str, dict[str, str]], tuple[tuple[str, str], ...]]:
    """Read lang/<code>.json -> (strings-by-code, (code, menu label) tuples).

    A file that is missing, unreadable or not valid JSON is skipped with a
    warning rather than taken down the app: a broken translation must never
    stop someone from playing. "@..." keys are metadata, not UI strings.
    """
    directory = LANG_DIR if directory is None else Path(directory)
    table: dict[str, dict[str, str]] = {}
    names: dict[str, str] = {}
    for path in sorted(directory.glob("*.json")):
        try:
            doc = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            print(f"[i18n] skipping {path.name}: {exc}", file=sys.stderr)
            continue
        if not isinstance(doc, dict):
            print(f"[i18n] skipping {path.name}: expected an object", file=sys.stderr)
            continue
        code = path.stem.lower()
        names[code] = str(doc.get("@name") or code)
        table[code] = {k: str(v) for k, v in doc.items() if not k.startswith("@")}
    if FALLBACK_LANG not in table:                 # never leave `tr` without a floor
        print(f"[i18n] no {FALLBACK_LANG}.json in {directory}", file=sys.stderr)
        table[FALLBACK_LANG] = {}
        names.setdefault(FALLBACK_LANG, "English")
    # English first (it is the fallback and the source language), rest A->Z on
    # the label the user actually reads.
    order = sorted(table, key=lambda c: (c != FALLBACK_LANG, names[c].lower()))
    return table, tuple((c, names[c]) for c in order)


STRINGS, LANGUAGES = load_languages()


def tr(lang: str, key: str) -> str:
    base = STRINGS.get(FALLBACK_LANG, {})
    return STRINGS.get(lang, base).get(key, base.get(key, key))


def time_units(lang: str) -> dict[str, str]:
    """The min/hour/day abbreviations `ngpc_library`'s card subtitles need. That
    module stays Qt-free, so it cannot reach the table itself."""
    return {u: tr(lang, f"unit_{u}") for u in ("min", "hour", "day")}

