"""Themes for the shell and the debugger.

One place for every UI colour. The rest of the app never writes a hex literal:
it asks for a SEMANTIC token (`p.warning`, `p.bg_card`) and gets whatever the
active theme says. That indirection is the whole point -- a colour written in
place is a colour that only one theme gets right, and the theme the author
happens to run is the one that stays correct.

Three choices, `THEME_SYSTEM` by default:

    system   follow Windows (light/dark), and keep following it live
    dark     the original hand-built dark shell
    light    a hand-built light shell (not an inversion of the dark one)

The light palette is designed, not derived. Flipping lightness gives washed-out
accents and unreadable ambers; every foreground here is checked against its own
background for WCAG AA (>=4.5:1 for text, >=3:1 for glyphs and borders).

Two things stay fixed in both themes because they are not chrome: the LCD
backdrop (that is the console's screen, and it is black) and the OSD green over
it (it floats on the game image, not on the window).
"""

from __future__ import annotations

from dataclasses import dataclass

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QBrush, QColor
from PyQt6.QtWidgets import QApplication

THEME_SYSTEM = "system"
THEME_DARK = "dark"
THEME_LIGHT = "light"

# (id, i18n key) -- the label text lives in ngpc_settings.STRINGS like every
# other display string, so a theme name translates with the rest of the UI.
THEMES: tuple[tuple[str, str], ...] = (
    (THEME_SYSTEM, "theme_system"),
    (THEME_DARK, "theme_dark"),
    (THEME_LIGHT, "theme_light"),
)


@dataclass(frozen=True)
class Palette:
    """Every colour the UI is allowed to use, by role rather than by value."""

    # text
    text: str            # body text
    text_dim: str        # hints, secondary lines
    text_strong: str     # titles, hover emphasis
    text_placeholder: str  # the "no cover yet" glyph

    # surfaces
    bg_window: str
    bg_rail: str
    bg_input: str
    bg_ghost: str
    bg_card: str
    bg_card_hover: str
    bg_row: str
    bg_menu: str
    bg_playbar: str
    bg_barbtn: str
    bg_barbtn_hover: str
    bg_hover: str        # rail item hover
    bg_menu_hover: str
    bg_art: str          # behind a cover thumbnail
    bg_scope: str        # the audio scope canvas

    # borders
    border: str
    border_card: str
    border_playbar: str
    border_bar: str

    # accent
    accent: str
    accent_hover: str
    accent_soft: str     # the tint behind a selected rail / list item
    accent_pressed: str  # a latched play-bar button
    on_accent: str       # text ON an accent fill

    # semantic states
    warning: str
    error: str
    fav_on: str
    fav_off: str

    # component-specific foregrounds
    rail_text: str
    rail_toggle: str
    card_name: str
    bar_text: str
    menu_item: str

    # translucent layers (full rgba() strings -- they sit over live content)
    scrim_overlay: str
    scrim_barshow: str

    # debugger tints, as (r, g, b) so they can build QBrush/QColor and also be
    # baked into the numpy tile atlas
    dbg_pc_row: tuple[int, int, int]
    dbg_read: tuple[int, int, int]
    dbg_write: tuple[int, int, int]
    usage_free: tuple[int, int, int]   # a character-RAM tile nobody references

    is_dark: bool


# The console's own screen and the OSD that floats on it. Not themed: the NGPC
# LCD is black when it is off, in any theme, and the OSD reads over the GAME.
LCD_BG = "#000000"
OSD_FG = "#7CFC7C"
OSD_BG = "rgba(0,0,0,0.45)"

DARK = Palette(
    text="#e7e9ee",
    text_dim="#8b93a3",
    text_strong="#ffffff",
    text_placeholder="#2c313c",
    bg_window="#15171c",
    bg_rail="#101216",
    bg_input="#12141a",
    bg_ghost="#21252e",
    bg_card="#1b1e25",
    bg_card_hover="#20242d",
    bg_row="#1b1e25",
    bg_menu="#191d25",
    bg_playbar="#14181f",
    bg_barbtn="#1c222b",
    bg_barbtn_hover="#262d38",
    bg_hover="#1c2028",
    bg_menu_hover="#232833",
    bg_art="#0c0e12",
    bg_scope="#111111",
    border="#2c313c",
    border_card="#262b34",
    border_playbar="#262c36",
    border_bar="#2c333e",
    accent="#4aa3ff",
    accent_hover="#6fb6ff",
    # The accent at ~18% over the rail, pre-blended. It was written "#4aa3ff22"
    # (accent + an alpha byte) for most of this code's life, but Qt accepts no
    # 8-digit hex: it silently parsed that as #a3ff22 and painted every selected
    # rail item and category LIME GREEN. Opaque, so it cannot be misread again.
    accent_soft="#1a2c40",
    accent_pressed="#2f6feb",
    on_accent="#06121f",
    warning="#ffb454",
    error="#e06c75",
    fav_on="#ffcc55",
    fav_off="#5a6270",
    rail_text="#b8c0cf",
    rail_toggle="#7a8494",
    card_name="#cfd5e0",
    bar_text="#cfd6e0",
    menu_item="#cdd3de",
    scrim_overlay="rgba(10, 12, 16, 0.72)",
    scrim_barshow="rgba(20, 24, 31, 0.85)",
    dbg_pc_row=(46, 62, 88),
    dbg_read=(30, 58, 95),
    dbg_write=(96, 40, 44),
    usage_free=(38, 38, 42),
    is_dark=True,
)

# Light. Not an inversion: the accent is DARKENED (a #4aa3ff sky blue reaches
# only ~2:1 on white and fails as text), the warning amber becomes a brown-gold,
# and the surface ladder runs the other way -- cards sit ABOVE the window here
# (white on grey) where in the dark theme they sit below it.
LIGHT = Palette(
    text="#1a1e26",            # 14.8:1 on bg_window
    text_dim="#59606e",        # 5.6:1  on bg_window
    text_strong="#0d1017",
    text_placeholder="#b4bac4",
    bg_window="#f4f5f8",
    bg_rail="#e9ebf0",
    bg_input="#ffffff",
    bg_ghost="#ffffff",
    bg_card="#ffffff",
    bg_card_hover="#f6f7fa",
    bg_row="#ffffff",
    bg_menu="#ffffff",
    bg_playbar="#e9ebf0",
    bg_barbtn="#ffffff",
    bg_barbtn_hover="#edeff3",
    bg_hover="#dde0e7",
    bg_menu_hover="#eef0f4",
    bg_art="#dfe2e8",
    bg_scope="#ffffff",
    border="#d2d6de",
    border_card="#e2e5ea",
    border_playbar="#d8dbe2",
    border_bar="#d2d6de",
    accent="#1668c7",          # 5.5:1 on white
    accent_hover="#0f57ab",
    accent_soft="#dceafb",
    accent_pressed="#1668c7",
    on_accent="#ffffff",
    warning="#8a5a00",         # 5.4:1 on bg_window
    error="#b3261e",           # 5.9:1 on bg_window
    fav_on="#a87400",          # glyph, 4.0:1
    fav_off="#868d9a",         # glyph, 3.1:1
    rail_text="#3b4351",       # 8.8:1 on bg_rail
    rail_toggle="#6b7280",
    card_name="#3b4351",
    bar_text="#3b4351",
    menu_item="#3b4351",
    scrim_overlay="rgba(244, 245, 248, 0.80)",
    scrim_barshow="rgba(233, 235, 240, 0.92)",
    dbg_pc_row=(203, 224, 250),
    dbg_read=(205, 229, 250),
    dbg_write=(250, 212, 210),
    usage_free=(214, 216, 222),
    is_dark=False,
)

PALETTES = {THEME_DARK: DARK, THEME_LIGHT: LIGHT}

# The palette in force right now. Widgets that PAINT (rather than being styled by
# QSS) read it here. It lives in this module, not in the shell, so a custom-drawn
# widget can ask for colours without importing the shell that owns it -- that
# import would be a cycle, since the shell builds the widget.
_CURRENT = DARK


def set_current(p: Palette) -> None:
    global _CURRENT
    _CURRENT = p


def current() -> Palette:
    return _CURRENT


# ------------------------------------------------------------------ system
def system_is_dark() -> bool:
    """What Windows is set to right now.

    Qt 6.5 reports the OS setting through QStyleHints, which is also what emits
    `colorSchemeChanged` when the user flips it -- so this and the live-follow
    signal always agree. `Unknown` means the platform has no preference (some
    Linux desktops); we keep the app's historical dark look there.
    """
    try:
        scheme = QApplication.styleHints().colorScheme()
    except (AttributeError, RuntimeError):
        return True                       # pre-6.5 Qt, or no QApplication yet
    return scheme != Qt.ColorScheme.Light


def resolve(theme_id: str) -> Palette:
    """Theme id -> the palette to actually paint with."""
    if theme_id in PALETTES:
        return PALETTES[theme_id]
    return DARK if system_is_dark() else LIGHT


def brush(rgb: tuple[int, int, int]) -> QBrush:
    return QBrush(QColor(*rgb))


# ------------------------------------------------------------ stylesheets
def build_style(p: Palette) -> str:
    """The main-window stylesheet, for the palette given.

    Rebuilt on every theme change rather than patched, so a token can never go
    stale. QSS is cheap to re-parse: this is a few hundred microseconds.
    """
    return f"""
* {{ color: {p.text}; font-family: 'Segoe UI', system-ui, sans-serif; }}
QMainWindow, QWidget#page {{ background: {p.bg_window}; }}
QWidget#rail {{ background: {p.bg_rail}; }}
QLabel#appTitle {{ font-size: 18px; font-weight: 700; padding: 14px 12px; }}
QLabel#pageTitle {{ font-size: 22px; font-weight: 700; }}
QLabel#hint {{ color: {p.text_dim}; }}
QPushButton#rail {{
    text-align: left; padding: 11px 16px; border: none; border-radius: 8px;
    font-size: 14px; background: transparent; color: {p.rail_text};
}}
QPushButton#rail:hover {{ background: {p.bg_hover}; color: {p.text_strong}; }}
QPushButton#rail:checked {{ background: {p.accent_soft}; color: {p.accent}; font-weight: 600; }}
QPushButton#primary {{
    background: {p.accent}; color: {p.on_accent}; border: none; border-radius: 8px;
    padding: 9px 18px; font-weight: 600;
}}
QPushButton#primary:hover {{ background: {p.accent_hover}; }}
QPushButton#ghost {{
    background: {p.bg_ghost}; border: 1px solid {p.border}; border-radius: 8px;
    padding: 8px 16px;
}}
QPushButton#ghost:hover {{ border-color: {p.accent}; }}
QFrame#card {{ background: {p.bg_card}; border: 1px solid {p.border_card}; border-radius: 12px; }}
QFrame#card:hover {{ border-color: {p.accent}; background: {p.bg_card_hover}; }}
QLabel#cardName {{ font-size: 12px; color: {p.card_name}; padding: 0 6px; }}
QFrame#settingRow {{ background: {p.bg_row}; border-radius: 10px; }}
QListWidget#cats {{ background: transparent; border: none; font-size: 14px; outline: 0; }}
QListWidget#cats::item {{ padding: 10px 14px; border-radius: 8px; margin: 2px 6px; }}
QListWidget#cats::item:selected {{ background: {p.accent_soft}; color: {p.accent}; }}
QComboBox, QLineEdit, QSpinBox {{
    background: {p.bg_input}; border: 1px solid {p.border}; border-radius: 7px; padding: 6px 8px;
}}
QComboBox:hover, QLineEdit:hover {{ border-color: {p.accent}; }}
QSlider::groove:horizontal {{ height: 6px; background: {p.border}; border-radius: 3px; }}
QSlider::handle:horizontal {{ width: 16px; background: {p.accent}; border-radius: 8px; margin: -6px 0; }}
QCheckBox {{ spacing: 8px; }}
QScrollArea {{ border: none; }}
QLabel#lcd {{ background: {LCD_BG}; border-radius: 6px; }}
QLabel#overlay {{ font-size: 20px; font-weight: 700; }}
QLabel#osd {{ color: {OSD_FG}; font-weight: 700; font-family: "Consolas", monospace;
  background: {OSD_BG}; border-radius: 4px; padding: 2px 6px; }}
QFrame#playbar {{ background: {p.bg_playbar}; border-top: 1px solid {p.border_playbar}; }}
QPushButton#barBtn {{ background: {p.bg_barbtn}; color: {p.bar_text}; border: 1px solid {p.border_bar};
  border-radius: 5px; padding: 4px 9px; font-size: 15px; }}
QPushButton#barBtn:hover {{ background: {p.bg_barbtn_hover}; }}
QPushButton#barBtn:checked {{ background: {p.accent_pressed}; color: {p.on_accent};
  border-color: {p.accent_pressed}; }}
QLabel#barSpeed {{ color: {OSD_FG}; font-weight: 700; font-family: "Consolas", monospace; }}
QPushButton#barShow {{ background: {p.scrim_barshow}; color: {p.bar_text};
  border: 1px solid {p.border_bar}; border-top-left-radius: 5px; border-top-right-radius: 5px; }}
QPushButton#railToggle {{ background: transparent; color: {p.rail_toggle}; border: none;
  font-size: 18px; font-weight: 700; }}
QPushButton#railToggle:hover {{ color: {p.text_strong}; }}
QWidget#overlayMenu {{ background: {p.scrim_overlay}; }}
QFrame#menuPanel {{ background: {p.bg_menu}; border: 1px solid {p.border}; border-radius: 14px; }}
QLabel#menuTitle {{ font-size: 18px; font-weight: 700; color: {p.text_strong}; }}
QPushButton#menuItem {{
    text-align: left; padding: 11px 16px; border: none; border-radius: 9px;
    font-size: 15px; background: transparent; color: {p.menu_item};
}}
QPushButton#menuItem:hover {{ background: {p.bg_menu_hover}; color: {p.text_strong}; }}
QPushButton#menuItem[sel="true"] {{ background: {p.accent}; color: {p.on_accent}; font-weight: 600; }}

/* ---------------------------------------------------------------------------
   Everything below had NO rule at all before theming existed. Each of these
   widgets fell through to the operating system's own colours while the `*` rule
   above still forced the app's text colour onto them -- which is invisible on a
   machine whose Windows theme runs opposite to the app's. That mismatch is
   structurally impossible to notice while developing on a matching OS theme,
   which is exactly why it shipped. Anything drawn here must stay covered.
   --------------------------------------------------------------------------- */
QDialog, QMessageBox {{ background: {p.bg_window}; }}
QTableWidget, QTableView {{
    background: {p.bg_input}; alternate-background-color: {p.bg_card};
    gridline-color: {p.border}; border: 1px solid {p.border}; border-radius: 7px;
    selection-background-color: {p.accent}; selection-color: {p.on_accent};
}}
QHeaderView::section {{
    background: {p.bg_ghost}; color: {p.text_dim}; border: none;
    border-right: 1px solid {p.border}; border-bottom: 1px solid {p.border};
    padding: 4px 6px; font-weight: 600;
}}
QTableCornerButton::section {{ background: {p.bg_ghost}; border: none; }}
QPlainTextEdit, QTextEdit {{
    background: {p.bg_input}; color: {p.text};
    border: 1px solid {p.border}; border-radius: 7px; padding: 4px;
    selection-background-color: {p.accent}; selection-color: {p.on_accent};
}}
QTabWidget::pane {{ background: {p.bg_window}; border: 1px solid {p.border}; border-radius: 7px; }}
QTabBar::tab {{
    background: transparent; color: {p.text_dim};
    padding: 7px 14px; margin-right: 2px;
    border-top-left-radius: 7px; border-top-right-radius: 7px;
}}
QTabBar::tab:hover {{ color: {p.text}; background: {p.bg_hover}; }}
QTabBar::tab:selected {{ background: {p.accent_soft}; color: {p.accent}; font-weight: 600; }}
QMenu {{
    background: {p.bg_menu}; color: {p.text};
    border: 1px solid {p.border}; border-radius: 8px; padding: 4px;
}}
QMenu::item {{ padding: 6px 22px; border-radius: 5px; }}
QMenu::item:selected {{ background: {p.accent}; color: {p.on_accent}; }}
QMenu::separator {{ height: 1px; background: {p.border}; margin: 4px 8px; }}
QToolTip {{
    background: {p.bg_menu}; color: {p.text};
    border: 1px solid {p.border}; border-radius: 5px; padding: 4px 7px;
}}
/* The combo POPUP is a separate top-level view; it does not inherit the closed
   combo's rule and is a classic light-on-light offender. */
QComboBox QAbstractItemView {{
    background: {p.bg_menu}; color: {p.text}; border: 1px solid {p.border};
    selection-background-color: {p.accent}; selection-color: {p.on_accent};
    outline: 0;
}}
QScrollBar:vertical {{ background: transparent; width: 11px; margin: 0; }}
QScrollBar:horizontal {{ background: transparent; height: 11px; margin: 0; }}
QScrollBar::handle:vertical, QScrollBar::handle:horizontal {{
    background: {p.border}; border-radius: 5px; min-height: 28px; min-width: 28px;
}}
QScrollBar::handle:hover {{ background: {p.accent}; }}
QScrollBar::add-line, QScrollBar::sub-line {{ height: 0; width: 0; }}
QScrollBar::add-page, QScrollBar::sub-page {{ background: transparent; }}
QGroupBox {{
    border: 1px solid {p.border}; border-radius: 8px; margin-top: 8px; padding-top: 8px;
}}
QGroupBox::title {{ subcontrol-origin: margin; left: 10px; padding: 0 4px; color: {p.text_dim}; }}
QSplitter::handle {{ background: {p.border}; }}

/* -- the visual key map (ngpc_bindmap.py) -- */
QPushButton#bindField {{
    background: {p.bg_input}; color: {p.text}; border: 1px solid {p.accent};
    border-radius: 7px; padding: 4px 8px; font-size: 13px;
}}
QPushButton#bindField:hover {{ background: {p.bg_card_hover}; border-width: 2px; }}
QLabel#bindCaption {{ color: {p.text_dim}; font-size: 11px; font-weight: 700; }}
/* POWER: shown, never bindable -- the core owns it. Reads as inert on purpose. */
QLabel#bindDead {{
    background: transparent; color: {p.text_dim};
    border: 1px dashed {p.border}; border-radius: 7px; font-size: 11px;
}}
"""


# ------------------------------------------------------------ app palette
# Roles Qt paints NATIVELY, where a stylesheet never gets a say: the tick inside
# a checkbox, a text caret, the highlight behind a selection. Setting these on
# the QApplication is what keeps those bits in step with the chosen theme.
_ROLE_MAP = (
    ("Window", "bg_window"), ("WindowText", "text"),
    ("Base", "bg_input"), ("AlternateBase", "bg_card"),
    ("Text", "text"), ("Button", "bg_ghost"), ("ButtonText", "text"),
    ("ToolTipBase", "bg_menu"), ("ToolTipText", "text"),
    ("Highlight", "accent"), ("HighlightedText", "on_accent"),
    ("PlaceholderText", "text_dim"), ("Link", "accent"),
)


def apply_app_palette(app, p: Palette) -> None:
    """Put the whole QApplication on this palette.

    Also pins the Fusion style. Qt's native Windows style paints its indicators
    from the OS theme and ignores both QSS and QPalette -- so on native style,
    choosing "dark" while Windows runs light leaves light checkboxes stranded in
    a dark window. Fusion honours the palette, which is what makes an explicit
    theme choice mean anything. The app already draws all its own chrome, so
    nothing else about the look depends on the platform style.
    """
    from PyQt6.QtGui import QPalette

    app.setStyle("Fusion")
    pal = QPalette()
    for role_name, token in _ROLE_MAP:
        role = getattr(QPalette.ColorRole, role_name, None)
        if role is not None:
            pal.setColor(role, QColor(getattr(p, token)))
    # Disabled text has to be dimmer than normal text in BOTH themes; Fusion's
    # default grey is tuned for light and vanishes on a dark window.
    for group in (QPalette.ColorGroup.Disabled,):
        pal.setColor(group, QPalette.ColorRole.WindowText, QColor(p.text_dim))
        pal.setColor(group, QPalette.ColorRole.Text, QColor(p.text_dim))
        pal.setColor(group, QPalette.ColorRole.ButtonText, QColor(p.text_dim))
    app.setPalette(pal)
