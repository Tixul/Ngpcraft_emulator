"""The modern shell (`ngpc_shell.py`) — Qt-offscreen structure + settings tests.

Skips cleanly when PyQt6 is absent, like the other UI tests. Runs under the
offscreen QPA platform so it needs no display. It does NOT boot a ROM here (that
is exercised elsewhere / by hand); it checks the shell wiring and the settings
round-trip, which is what the front-end contract is.
"""

from __future__ import annotations

import os
import pathlib
import tempfile

import pytest

pytest.importorskip("PyQt6")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import Qt, QSettings  # noqa: E402
from PyQt6.QtWidgets import QApplication, QLabel, QWidget  # noqa: E402

import ngpc_settings as cfg  # noqa: E402
import ngpc_shell as shell  # noqa: E402
import ngpc_theme as th  # noqa: E402


@pytest.fixture(scope="module")
def app():
    a = QApplication.instance() or QApplication([])
    yield a


@pytest.fixture(autouse=True)
def _clean_settings():
    # This clears the REAL scope -- `make_settings()` has no test mode. What keeps it
    # off the user's registry is `pytest_configure` in the root conftest, which points
    # QSettings at a temp .ini before collection. Without that redirect these two
    # lines delete the user's BIOS path and ROM folder on every test.
    s = cfg.make_settings()

    def wipe():
        s.clear()
        # The backups are part of how settings persist now, so a fixture that
        # leaves them behind is not giving the next test a clean store -- the
        # startup restore would hand it the PREVIOUS test's values.
        for path in (cfg.backup_path(), cfg.backup_path(previous=True)):
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass

    wipe()
    yield
    wipe()


def test_the_suite_never_touches_real_settings():
    """The guard for the conftest redirect. This fixture calls `.clear()` on
    `QSettings("NgpCraft", "Emulator")` around EVERY test in this file; if that ever
    resolves to the user's own scope again, a test run eats their configuration --
    silently, because wiping settings is not something a passing test complains about.
    """
    from PyQt6.QtCore import QSettings

    s = cfg.make_settings()
    assert s.format() == QSettings.Format.IniFormat, \
        "settings must not resolve to the native store (the Windows registry)"
    where = pathlib.Path(s.fileName()).resolve()
    tmp = pathlib.Path(tempfile.gettempdir()).resolve()
    assert where.is_relative_to(tmp), f"tests would write real settings at {where}"


def test_shell_builds_with_three_pages(app):
    w = shell.Shell()
    try:
        assert w._stack.count() == 3
        # rail nav toggles page + checked state
        w._go(1)
        assert w._stack.currentWidget() is w.settings
        assert w._nav_set.isChecked() and not w._nav_lib.isChecked()
    finally:
        w.close()


def test_key_binding_round_trips_through_settings(app):
    s = cfg.make_settings()
    cfg.set_binding(s, "A", int(Qt.Key.Key_J))
    mapping = cfg.key_bindings(s)
    assert mapping.get(int(Qt.Key.Key_J)) == 0x10, "A must map to the joypad A bit"


def test_settings_defaults_are_sane(app):
    s = cfg.make_settings()
    assert cfg.lcd_scale(s) == 3
    assert cfg.audio_enabled(s) is True
    assert cfg.language(s) == "en"
    # every button has a default key
    m = cfg.key_bindings(s)
    assert {0x01, 0x02, 0x04, 0x08, 0x10, 0x20, 0x40} <= set(m.values())


def test_language_switch_retranslates_the_rail(app):
    w = shell.Shell()
    try:
        s = cfg.make_settings()
        s.setValue("general/language", "fr")
        w._retranslate()
        assert "Bibliothèque" in w._nav_lib.text()
        assert "Réglages" in w._nav_set.text()
    finally:
        w.close()


def test_the_rail_fits_every_language_it_ships(app):
    """The rail used to be a fixed 190 px while its labels are TRANSLATED -- so a
    language with longer words (Portuguese "Ferramentas de depuração", but French
    "Bibliothèque" already) had its entries cut off. It now measures itself.

    The contract, per language: every nav entry shows its text IN FULL -- on one line
    if it fits, wrapped onto two if it does not -- or, only when even two lines cannot
    hold it, carries a tooltip. The rail is capped, so it can never eat the window.

    Wrapping rather than shortening is the point: `m_debug` in Portuguese is
    "Ferramentas de depuração", and rewording a contributor's translation to fit a
    sidebar is not the layout's call to make.
    """
    from PyQt6.QtGui import QFont, QFontMetrics

    w = shell.Shell()
    try:
        w.show()
        # Wide on purpose: below RAIL_AUTO_COLLAPSE_W the rail folds itself, and the
        # offscreen QPA's screen size (which decides how big `fit_to_screen` leaves the
        # window) is not the same on every platform. This test is about the MEASURED
        # width, so put it in the state where a width is measured.
        w.resize(shell.RAIL_AUTO_COLLAPSE_W + 200, 700)
        s = cfg.make_settings()
        assert set(cfg.STRINGS) >= {"en", "fr"}, "at least the two original languages"
        for lang in sorted(cfg.STRINGS):
            s.setValue("general/language", lang)
            w._retranslate()
            rail = w._rail.width()
            assert shell.RAIL_MIN_W <= rail <= shell.RAIL_MAX_W
            for b, label in w._nav_text.items():
                b.ensurePolished()
                f = QFont(b.font()); f.setBold(True)
                fm = QFontMetrics(f)
                # whatever the layout, the words shown are the words translated
                assert b.text().split() == label.split(), f"[{lang}] {label!r} altered"
                need = max(fm.horizontalAdvance(ln) for ln in b.text().split("\n"))
                assert need + shell.RAIL_TEXT_PAD <= rail or b.toolTip() == label, (
                    f"[{lang}] {label!r} needs {need + shell.RAIL_TEXT_PAD}px of a "
                    f"{rail}px rail and has no tooltip to fall back on")
        # collapsing still wins over any measured width, and expanding restores it
        w._toggle_rail(False)
        assert w._rail.width() == shell.RAIL_COLLAPSED_W
        w._toggle_rail(True)
        assert w._rail.width() == w._rail_w
    finally:
        w.close()


def test_a_long_nav_label_wraps_instead_of_being_clipped(app):
    """A label no single line can hold is split over two, at the balanced boundary --
    the one that leaves the widest line narrowest, since that is what the rail costs."""
    from PyQt6.QtGui import QFont, QFontMetrics

    w = shell.Shell()
    try:
        w.show()
        b = w._nav_dbg
        b.ensurePolished()
        f = QFont(b.font()); f.setBold(True)
        fm = QFontMetrics(f)
        # Build a label that overflows one rail line in the font Qt WILL PAINT WITH,
        # rather than hard-coding a string and betting it is wide enough. That bet lost
        # on the CI: its offscreen QPA falls back to a narrower font than Windows, so
        # "Ferramentas de depuração" (the original literal) fit one line there, the wrap
        # under test never fired, and this failed on Linux/Mac only.
        #
        # SHORT, uniform words, grown one at a time until the line JUST passes the cap.
        # Two properties the assertions below need, on any font: overflowing by a single
        # short word keeps the balanced split's widest line under the cap (so no tooltip
        # fires), and equal words make that split a clean near-middle break. A long seed
        # word ("Ferramentas"/"depuração") could already blow past twice the cap on a
        # wide font, leaving no two-line fit -- which is the trap this avoids.
        cap = shell.RAIL_MAX_W - shell.RAIL_TEXT_PAD
        word = "log"
        words = [word, word]
        while fm.horizontalAdvance(shell.RAIL_INDENT + " ".join(words)) <= cap:
            words.append(word)
        long_label = " ".join(words)
        w._nav_text = dict(w._nav_text)          # leave the real labels alone
        w._nav_text[b] = long_label
        w._fit_rail()

        assert "\n" in b.text(), "a label this long must not stay on one line"
        assert b.text().split() == long_label.split(), "wrapping must not drop a word"
        assert not b.toolTip(), "it fits on two lines -- no tooltip needed"
        assert b.sizeHint().height() > w._nav_lib.sizeHint().height(), \
            "the two-line entry is taller than a one-line one"
        # The split is the width-minimising one (what `_wrap_nav` promises), not a greedy
        # first-line fill. Check the chosen widest line equals the best any single break
        # can do -- computed the same way the code does, so it holds in any font.
        best = min(
            max(fm.horizontalAdvance(shell.RAIL_INDENT + " ".join(words[:i])),
                fm.horizontalAdvance(shell.RAIL_INDENT + " ".join(words[i:])))
            for i in range(1, len(words)))
        chosen = max(fm.horizontalAdvance(ln) for ln in b.text().split("\n"))
        assert chosen == best, f"wrap must pick the width-minimising split ({chosen} vs {best})"
    finally:
        w.close()


def test_theme_switch_restyles_the_window(app):
    w = shell.Shell()
    try:
        s = cfg.make_settings()
        s.setValue("general/theme", th.THEME_LIGHT)
        w._restyle()
        assert shell.PALETTE is th.LIGHT
        assert th.LIGHT.bg_window in w.styleSheet()
        s.setValue("general/theme", th.THEME_DARK)
        w._restyle()
        assert shell.PALETTE is th.DARK
        assert th.DARK.bg_window in w.styleSheet()
    finally:
        w.close()


def test_no_widget_falls_through_to_the_os_palette(app):
    """The bug this theming exists to kill.

    Every widget class the app instantiates must get a background from OUR
    stylesheet. One that does not gets the OS's colours while `*` still forces
    our text colour onto it -- which is invisible when the user's Windows theme
    runs opposite to the app's, and which looks perfectly fine to a developer
    whose OS theme happens to match. Only a test catches that."""
    for palette in (th.DARK, th.LIGHT):
        css = th.build_style(palette)
        for widget in ("QTableWidget", "QPlainTextEdit", "QTabWidget::pane",
                       "QMenu", "QToolTip", "QDialog",
                       "QComboBox QAbstractItemView"):
            assert widget in css, f"{widget} has no rule: it will use OS colours"


def test_every_palette_colour_actually_parses(app):
    """Qt accepts no 8-digit #RRGGBBAA: it parsed "#4aa3ff22" as #a3ff22 and
    painted the selected rail item lime green for the whole life of the code,
    with no warning. An unparseable colour must fail here, not on screen."""
    from PyQt6.QtGui import QColor

    for palette in (th.DARK, th.LIGHT):
        for field in palette.__dataclass_fields__:
            value = getattr(palette, field)
            if not isinstance(value, str) or not value.startswith("#"):
                continue
            c = QColor()
            c.setNamedColor(value)
            assert c.isValid() and c.name() == value.lower(), (
                f"{field}={value!r} does not round-trip: Qt reads it as {c.name()}")


def test_light_theme_never_reuses_a_dark_colour(app):
    """A light theme built by copy-paste keeps a few dark values by accident, and
    each one is an unreadable patch. Nothing may be shared but the fixed
    console-screen colours, which are deliberately theme-independent."""
    shared = {f.name for f in th.DARK.__dataclass_fields__.values()
              if getattr(th.DARK, f.name) == getattr(th.LIGHT, f.name)}
    assert shared == set(), f"light theme still carries dark values: {shared}"


def test_console_art_loads_and_is_declared_to_pyinstaller(app):
    """The key map is a picture; without it the panel is fields floating in space.

    Two ways that breaks, both silent: the file goes missing, or it exists in the
    repo but is absent from the .spec -- PyInstaller follows imports, not file
    reads, so an asset opened by path is invisible to it and never reaches the
    .exe. The packaged app would show an empty console and no error."""
    import ngpc_bindmap

    assert ngpc_bindmap.ART.is_file(), f"missing console art: {ngpc_bindmap.ART}"
    from PyQt6.QtGui import QPixmap
    assert not QPixmap(str(ngpc_bindmap.ART)).isNull(), "console art will not decode"

    spec = (pathlib.Path(__file__).resolve().parent.parent / "NgpCraftEmulator.spec")
    assert ngpc_bindmap.ART.name in spec.read_text(encoding="utf-8"), (
        f"{ngpc_bindmap.ART.name} is not in the .spec datas: it will be missing "
        "from the built .exe even though the tests pass from source")


def test_bind_map_covers_every_joypad_button(app):
    """Every bindable button needs a field, or a binding becomes unreachable from
    the UI. POWER is deliberately absent: only 7 joypad bits exist (0x80 is POWER
    and the core drives it), so a POWER field would be a dead control."""
    w = shell.Shell()
    try:
        fields = set(w.settings._bindmap.buttons)
        assert fields == {lbl for lbl, _mask in cfg.JOYPAD_BUTTONS}
        assert "Power" not in fields
    finally:
        w.close()


def test_settings_page_writes_graphics_scale(app):
    w = shell.Shell()
    try:
        w.settings._scale.setValue(5)
        assert cfg.lcd_scale(cfg.make_settings()) == 5
    finally:
        w.close()


def test_controls_panel_has_seven_capture_buttons(app):
    w = shell.Shell()
    try:
        assert len(w.settings._keybtns) == 7
    finally:
        w.close()


def test_console_boot_defaults_off(app):
    # Default hand-off: the real-BIOS boot loops on "SUB BATTERY DEAD" (RTC /
    # sub-battery not modelled yet), so games boot via hand-off until that lands.
    assert cfg.real_bios(cfg.make_settings()) is False


# ⚠️ A ROM folder that EXISTS is not a ROM folder that has ROMs in it. A clean
# checkout ships `roms/` with only a README (cartridge images are never
# distributed), so testing `is_dir()` alone let these tests run with nothing to
# load and fail on `assert rom is not None` -- a red suite that means "you have
# no ROMs", not "the emulator is broken". Require an actual cartridge.
_HAVE_ROMS = (
    shell.DEFAULT_BIOS.is_file()
    and shell.DEFAULT_ROM_DIR.is_dir()
    and any(shell.DEFAULT_ROM_DIR.glob("*.ng[cp]"))
)


@pytest.mark.skipif(not _HAVE_ROMS, reason="needs the local ROM folder + BIOS")
def test_handoff_boot_reaches_the_cartridge(app):
    from pathlib import Path
    rom = next(iter(sorted(shell.DEFAULT_ROM_DIR.glob("*.ngc"))), None)
    assert rom is not None
    w = shell.Shell()
    try:
        w.play._frames_due = lambda: 6      # bypass the wall-clock pacer
        w.play.start(Path(rom))
        assert w.play._real_bios is False   # default hand-off
        for _ in range(30):
            w.play._tick()
        pc = w.play.machine.cpu().pc
        assert 0x200000 <= pc < 0x400000, f"should be in cartridge code, got 0x{pc:06X}"
    finally:
        w.play.stop()
        w.close()


@pytest.mark.skipif(not _HAVE_ROMS, reason="needs the local BIOS image")
def test_bios_alone_boots_without_a_cartridge(app):
    w = shell.Shell()
    try:
        w.play._frames_due = lambda: 6
        w.play.start_bios()
        assert w.play.session is None and w.play._raw is not None
        for _ in range(120):
            w.play._tick()
        assert w.play._power_pressed is True
        assert len(set(w.play.machine.framebuffer())) > 3
    finally:
        w.play.stop()
        w.close()


# ---- video filter pipeline (no ROM needed) ----
def test_video_filters_produce_the_right_size_and_darken(app):
    import numpy as np
    import ngpc_video as v
    fb = [((x & 0xF) | ((y & 0xF) << 4) | (((x + y) & 0xF) << 8))
          for y in range(v.SCREEN_H) for x in range(v.SCREEN_W)]
    for filt in v.FILTERS:
        a = v.render_array(fb, 4, filt, v.COLOR_RAW)
        assert a.shape == (v.SCREEN_H * 4, v.SCREEN_W * 4, 3)
    base = v.render_array(fb, 4, v.FILTER_NONE, v.COLOR_RAW).astype(int).sum()
    scan = v.render_array(fb, 4, v.FILTER_SCANLINES, v.COLOR_RAW).astype(int).sum()
    assert scan < base, "scanlines must darken the image"


def test_video_settings_round_trip(app):
    import ngpc_video as v
    s = cfg.make_settings()
    s.setValue("gfx/filter", v.FILTER_CRT)
    s.setValue("gfx/color", v.COLOR_LCD)
    s.setValue("gfx/aspect", v.ASPECT_FIT)
    assert cfg.video_filter(s) == v.FILTER_CRT
    assert cfg.color_profile(s) == v.COLOR_LCD
    assert cfg.aspect_mode(s) == v.ASPECT_FIT
    # bad values fall back to safe defaults
    s.setValue("gfx/filter", "garbage")
    assert cfg.video_filter(s) == v.FILTER_NONE


@pytest.mark.skipif(not _HAVE_ROMS, reason="needs the local ROM folder + BIOS")
def test_escape_opens_the_pause_menu_and_keeps_the_game_alive(app):
    from pathlib import Path
    from PyQt6.QtCore import QEvent
    from PyQt6.QtGui import QKeyEvent
    rom = next(iter(sorted(shell.DEFAULT_ROM_DIR.glob("*.ngc"))), None)
    assert rom is not None
    w = shell.Shell()
    try:
        w.play._frames_due = lambda: 4
        w._launch(str(rom))
        for _ in range(8):
            w.play._tick()
        w.play.keyPressEvent(QKeyEvent(QEvent.Type.KeyPress, int(Qt.Key.Key_Escape),
                                       Qt.KeyboardModifier.NoModifier))
        # THE FIX: Escape pauses into a menu; it does NOT unload the game.
        assert w.play._menu_open and w.play.paused
        assert w.play.machine is not None
        assert w._stack.currentWidget() is w.play
        # in-game options keep the game alive and jump to its settings
        w.play._on_menu_choice("video")
        assert w._stack.currentWidget() is w.settings
        assert w.play.machine is not None and w.play.paused
        assert w.settings._cats.currentRow() == 1
        # resume returns to the live game
        w.settings.resume_requested.emit()
        assert w._stack.currentWidget() is w.play and not w.play.paused
        # quit is the only thing that unloads it
        w.play._on_menu_choice("quit")
        assert w.play.machine is None
        assert w._stack.currentWidget() is w.library
    finally:
        w.play.stop()
        w.close()


@pytest.mark.skipif(not _HAVE_ROMS, reason="needs the local ROM folder + BIOS")
def test_debug_window_reads_every_tab(app):
    from pathlib import Path
    rom = next(iter(sorted(shell.DEFAULT_ROM_DIR.glob("*.ngc"))), None)
    assert rom is not None
    w = shell.Shell()
    try:
        w.play._frames_due = lambda: 4
        w._launch(str(rom))
        for _ in range(40):
            w.play._tick()
        w._open_debug()
        dbg = w._debug_win
        for i in range(dbg._tabs.count()):
            dbg._tabs.setCurrentIndex(i)
            dbg.refresh()      # must not raise on any tab
        assert "PC" in dbg._cpu_text.toPlainText()
        assert len(dbg._dis_text.toPlainText().splitlines()) > 5
        assert len(dbg._mem_text.toPlainText().splitlines()) == 24
        dbg._step()            # single-frame step must not raise
    finally:
        if w._debug_win is not None:
            w._debug_win.close()
        w.play.stop()
        w.close()


@pytest.mark.skipif(not _HAVE_ROMS, reason="needs the local ROM folder + BIOS")
def test_debug_exports_and_trace_to_file(app, tmp_path, monkeypatch):
    from pathlib import Path
    import ngpc_debug
    rom = next(iter(sorted(shell.DEFAULT_ROM_DIR.glob("*.ngc"))), None)
    assert rom is not None

    def fake_save(parent, title, default, filt):
        return (str(tmp_path / Path(default).name), "")
    monkeypatch.setattr(ngpc_debug.QFileDialog, "getSaveFileName", staticmethod(fake_save))

    w = shell.Shell()
    try:
        w.play._frames_due = lambda: 4
        w._launch(str(rom))
        for _ in range(40):
            w.play._tick()
        w._open_debug()
        dbg = w._debug_win
        # trace a run of instructions to a file
        dbg._trace_count.setValue(2000)
        dbg._trace_to_file()
        trace = (tmp_path / "trace.txt").read_text(encoding="utf-8").splitlines()
        assert len(trace) > 1000, "trace file should hold a long run of instructions"
        # text + image exports land on disk
        # ⚠️ BY NAME, never by index. These were hard-coded 3 and 4, which stopped
        # being Palette and Tiles long ago -- the test was refreshing Events and
        # Memory and then saving whatever atlas happened to be left over.
        def _tab(title: str) -> int:
            return [dbg._tabs.tabText(i) for i in range(dbg._tabs.count())].index(title)
        dbg._tabs.setCurrentIndex(_tab("CPU")); dbg.refresh()
        dbg._save_text(dbg._cpu_text.toPlainText(), "cpu_state.txt")
        dbg._tabs.setCurrentIndex(_tab("Palette")); dbg.refresh()
        dbg._save_png(dbg._pal_arr, "palette.png")
        dbg._tabs.setCurrentIndex(_tab("Tiles")); dbg.refresh()
        dbg._save_png(dbg._tiles_arr, "tiles.png")
        assert (tmp_path / "cpu_state.txt").stat().st_size > 0
        assert (tmp_path / "palette.png").stat().st_size > 0
        assert (tmp_path / "tiles.png").stat().st_size > 0
        # freeze stops auto-refresh
        dbg._freeze.setChecked(True)
        assert dbg._frozen
        dbg._on_timer()        # a timer tick while frozen must be a no-op (not raise)
    finally:
        if w._debug_win is not None:
            w._debug_win.close()
        w.play.stop()
        w.close()


def test_tile_hover_reports_address_and_click_copies(app):
    """The tile viewer answers 'which tile is this, where does it live, who uses it,
    what are its bytes' on hover, and a click puts that on the clipboard -- the numbers
    you need to poke or replace a tile. Pure data path, no ROM: the caches hover reads
    are set directly and `_tile_info` is asked for a specific cell."""
    import numpy as np
    import ngpc_debug as dbg_mod
    from PyQt6.QtWidgets import QApplication

    dbg = dbg_mod.DebugWindow(None, cfg.make_settings())
    try:
        n = 300                                    # past 255, so the sprite-only note fires
        char = bytearray(n * dbg_mod.TILE_BYTES)
        char[5 * 16:5 * 16 + 3] = b"\xDE\xAD\xBE"  # a fingerprint in tile 5's bytes
        usage = np.zeros(dbg_mod.CHAR_RAM_TILES, np.uint8)
        usage[5] = dbg_mod.USE_SCR1 | dbg_mod.USE_SPRITE     # shared, to exercise the label
        dbg._tiles_char = bytes(char)
        dbg._tiles_usage = usage
        dbg._tiles_n = n

        # tile 5 -> col 5, row 0. Address is CHAR_RAM + 5*16 = 0x00A050.
        info = dbg._tile_info(5, 0)
        assert "tile 5 (0x005)" in info
        assert "0x00A050" in info and "0x00A05F" in info
        assert "shared" in info and "SCR1" in info and "sprites" in info
        assert "DE AD BE" in info

        # a tile past the last one present has nothing to say
        assert dbg._tile_info(0, n // 16 + 1) is None

        # a high tile carries the 9-bit sprite-addressing note
        assert "sprite ref" in dbg._tile_info(299 % 16, 299 // 16)

        # a click copies the block and the status line confirms it
        dbg._tile_status(info, copy=True)
        assert QApplication.clipboard().text() == info
        assert dbg._tile_status_line.text().startswith("✔ copied")
        # a hover just shows it, without touching the clipboard
        dbg._tile_status(dbg._tile_info(0, 0), copy=False)
        assert not dbg._tile_status_line.text().startswith("✔")

        # the grid's hit size is locked to the sheet geometry, so a click lands on the
        # tile under the cursor and not its neighbour.
        assert dbg._tile_label._cell == dbg_mod.TILE_ATLAS_PITCH * dbg_mod.TILE_ATLAS_SCALE
    finally:
        dbg.close()


def test_text_tab_decodes_and_searches_via_a_loaded_table(app, tmp_path):
    """The Text tab is the fan-translation half of the debugger, and it works on ANY
    ROM through a user .tbl. With a table loaded and a stub machine standing in for the
    core, it decodes a region into strings, finds a phrase by its exact bytes, and --
    with no table -- cracks the encoding by letter spacing. No emulator, no real ROM."""
    import ngpc_debug as dbg_mod
    from core.texttable import parse_tbl

    class _FakeMem:                       # the slice of address space the tab reads
        def __init__(self, blob): self._blob = blob
        def read(self, addr, n): return bytes(self._blob[addr:addr + n])

    class _FakePlay:                      # `_m` is a property off `_play.machine`
        def __init__(self, mem): self.machine = mem

    # 0x10='h' 0x11='i', FF terminates. "hi"<end> planted at offset 0x20.
    blob = bytearray(0x400)
    blob[0x20:0x23] = bytes([0x10, 0x11, 0xFF])
    dbg = dbg_mod.DebugWindow(None, cfg.make_settings())
    try:
        dbg._play = _FakePlay(_FakeMem(blob))
        dbg._txt_table = parse_tbl("10=h\n11=i\n/FF=<end>")

        # decode: the string and its address show up
        dbg._txt_addr.setText("000020"); dbg._txt_len.setValue(16); dbg._txt_decode()
        assert "000020" in dbg._txt_out.toPlainText()
        assert "'hi'" in dbg._txt_out.toPlainText()

        # table search: the exact bytes are found at 0x20
        dbg._txt_from.setText("000000"); dbg._txt_size.setValue(1)
        dbg._txt_find.setText("hi"); dbg._txt_mode.setCurrentText("Table")
        dbg._txt_search()
        hits = dbg._txt_hits.toPlainText()
        assert "1 match" in hits and "000020" in hits

        # relative search: no table needed, and it derives the encoding it found
        dbg._txt_mode.setCurrentText("Relative"); dbg._txt_search()
        rel = dbg._txt_hits.toPlainText()
        assert "000020" in rel
        assert "'h'=10" in rel and "'i'=11" in rel, "relative hit hands back the bytes"
    finally:
        dbg.close()


def test_fullscreen_is_exited_by_escape_and_double_click(app, monkeypatch):
    """Regression for 'stuck in fullscreen': Escape and a double-click both return to
    windowed. The real fullscreen transition crashes under offscreen QPA, so the window
    state is mocked and the heavy apply is stubbed -- what is checked is that both routes
    clear the fullscreen setting, base the flip on the window's real state, and that
    Escape only intercepts WHILE fullscreen."""
    from PyQt6.QtCore import QEvent, QPointF
    from PyQt6.QtGui import QKeyEvent, QMouseEvent

    class _FakeWin:
        def __init__(self, fs): self._fs = fs
        def isFullScreen(self): return self._fs

    w = shell.Shell()
    try:
        p = w.play
        state = {"fs": True}
        monkeypatch.setattr(p, "window", lambda: _FakeWin(state["fs"]))
        monkeypatch.setattr(p, "apply_settings", lambda: None)   # skip the real transition
        monkeypatch.setattr(p, "_reblit_soon", lambda: None)

        esc = QKeyEvent(QEvent.Type.KeyPress, int(Qt.Key.Key_Escape),
                        Qt.KeyboardModifier.NoModifier)

        # Escape while fullscreen -> the setting is cleared
        p._settings.setValue("gfx/fullscreen", True)
        p.keyPressEvent(esc)
        assert not cfg.fullscreen(p._settings), "Escape in fullscreen returns to windowed"

        # double-click on the canvas while fullscreen -> cleared too
        p._settings.setValue("gfx/fullscreen", True)
        dbl = QMouseEvent(QEvent.Type.MouseButtonDblClick, QPointF(5, 5), QPointF(5, 5),
                          Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton,
                          Qt.KeyboardModifier.NoModifier)
        p.lcd.mouseDoubleClickEvent(dbl)
        assert not cfg.fullscreen(p._settings), "double-click in fullscreen returns to windowed"

        # a double-click while WINDOWED goes the other way (into fullscreen)
        state["fs"] = False
        p._settings.setValue("gfx/fullscreen", False)
        p.lcd.mouseDoubleClickEvent(dbl)
        assert cfg.fullscreen(p._settings), "double-click windowed -> fullscreen"
    finally:
        w.close()


def test_toolbar_auto_hides_when_idle_and_returns_on_move(app):
    """Feature: the player toolbar hides after the mouse goes still and comes back on any
    move, staying available without sitting over the game. The idle hide is transient and
    kept apart from the user's saved show/hide preference. State is driven directly (no
    real timer/mouse), and `isHidden()` is checked -- `isVisible()` also needs the window
    shown, which a headless test is not."""
    w = shell.Shell()
    try:
        p = w.play
        p.machine = object()                       # a game is 'running'
        w._settings.setValue("gfx/toolbar", True)
        w._settings.setValue("gfx/toolbar_autohide", True)

        p.refresh_toolbar()
        assert not p.toolbar.isHidden(), "windowed + preference on -> toolbar up"
        assert p._autohide_timer.isActive(), "the idle countdown is armed"

        # the mouse goes still -> the idle timeout hides it (but not the preference)
        p._idle_hide_toolbar()
        assert p._idle_hidden and p.toolbar.isHidden()

        # any move brings it back and re-arms the countdown
        p._on_pointer_activity()
        assert not p._idle_hidden and not p.toolbar.isHidden()
        assert p._autohide_timer.isActive()

        # option off -> a still mouse must NOT hide it
        w._settings.setValue("gfx/toolbar_autohide", False)
        p._idle_hide_toolbar()
        assert not p.toolbar.isHidden()

        # a manual hide is the preference (nub shown), never an idle hide, and stops the timer
        w._settings.setValue("gfx/toolbar_autohide", True)
        p._toggle_toolbar(False)
        assert p.toolbar.isHidden() and not p._bar_show.isHidden()
        assert not p._autohide_timer.isActive(), "nothing up to auto-hide"
    finally:
        p.machine = None
        w.close()


def test_both_windows_can_be_made_small(app):
    """Long, unwrapped help/description labels used to force an enormous minimum window
    (main ~1732 wide, debugger ~3266) -- you could not shrink either. They wrap now, so
    both windows honour a small size. Regression for 'let me make the windows smaller'."""
    import ngpc_debug as dbg_mod
    from PyQt6.QtWidgets import QApplication

    w = shell.Shell()
    try:
        w.show(); QApplication.processEvents()
        w.resize(420, 360); QApplication.processEvents()
        assert w.width() <= 460 and w.height() <= 400, \
            f"main window stuck large: {w.width()}x{w.height()}"

        d = dbg_mod.DebugWindow(w, cfg.make_settings())
        try:
            d.show(); QApplication.processEvents()
            d.resize(400, 340); QApplication.processEvents()
            assert d.width() <= 440 and d.height() <= 380, \
                f"debug window stuck large: {d.width()}x{d.height()}"
        finally:
            d.close()
    finally:
        w.close()


def test_sync_fullscreen_chrome_is_safe_before_the_ui_exists():
    """A WindowStateChange can fire mid-construction (restoreGeometry / first show) before
    the rail and play page exist. `_sync_fullscreen_chrome` must no-op then, not crash --
    regression for the AttributeError on `_rail` at startup. Called on a bare object so it
    exercises the guard without a full window."""
    class _Bare:
        pass
    shell.Shell._sync_fullscreen_chrome(_Bare())   # must not raise


def test_fullscreen_hides_and_restores_sidebar_and_toolbar(app, monkeypatch):
    """Feature: fullscreen can hide the sidebar and the player toolbar so the game gets
    the whole screen, and leaving fullscreen puts them back — the toolbar to the user's
    saved preference, never forced on. Driven by `_sync_fullscreen_chrome`; the window
    state is mocked so no real (and offscreen-crashy) fullscreen transition is needed.
    `isHidden()` is checked rather than `isVisible()` because the test window is not
    shown, which would make everything report not-visible regardless.

    On the GAME page throughout: hiding the nav is for the game only, and doing it on
    the library/settings pages is what left a fullscreen window with no menu and no
    title bar (see test_fullscreen_keeps_the_nav_outside_the_game)."""
    w = shell.Shell()
    try:
        state = {"fs": False}
        monkeypatch.setattr(w, "isFullScreen", lambda: state["fs"])
        w._go(2)                                       # the game page
        w._settings.setValue("gfx/fs_hide_ui", True)
        w._settings.setValue("gfx/toolbar", True)      # user keeps the toolbar normally

        state["fs"] = False; w._sync_fullscreen_chrome()
        assert not w._rail.isHidden() and not w.play.toolbar.isHidden(), "windowed: chrome shown"

        state["fs"] = True; w._sync_fullscreen_chrome()
        assert w._rail.isHidden() and w.play.toolbar.isHidden(), "fullscreen: chrome hidden"
        assert w.play._bar_show.isHidden(), "no 'show toolbar' nub either"

        # ...but the toolbar is only AUTO-hidden: a mouse move brings it back over the game
        # (the sidebar stays gone). This is the fix for 'toolbar never shows in fullscreen'.
        w.play.machine = object()
        w.play._on_pointer_activity()
        assert not w.play.toolbar.isHidden(), "a move reveals the fullscreen toolbar"
        assert w._rail.isHidden(), "...but not the sidebar"
        w.play.machine = None
        w.play._idle_hidden = True     # back to the resting hidden state for the next step

        state["fs"] = False; w._sync_fullscreen_chrome()
        assert not w._rail.isHidden() and not w.play.toolbar.isHidden(), "restored on exit"

        # the option off -> fullscreen keeps the chrome
        w._settings.setValue("gfx/fs_hide_ui", False)
        state["fs"] = True; w._sync_fullscreen_chrome()
        assert not w._rail.isHidden() and not w.play.toolbar.isHidden(), "opt off: chrome kept"

        # option on, but the toolbar was hidden by choice -> exit must not force it back
        w._settings.setValue("gfx/fs_hide_ui", True)
        w._settings.setValue("gfx/toolbar", False)
        state["fs"] = True; w._sync_fullscreen_chrome()
        assert w._rail.isHidden()
        state["fs"] = False; w._sync_fullscreen_chrome()
        assert not w._rail.isHidden() and w.play.toolbar.isHidden(), "toolbar stays as the user left it"
    finally:
        w.close()


def test_fullscreen_keeps_the_nav_outside_the_game(app, monkeypatch):
    """⛔ THE TRAP THIS CLOSES. "je ne vois pas le menu a gauche, l'emulateur se met
    full screen sans moyen de le fermer autrement que alt f4" (2026-08-02).

    A fullscreen window has NO title bar. Hide the nav on top of that and the library
    page has no menu, no window controls and nothing that answers a key -- the only way
    out is killing the app. The nav is hidden for the GAME, which has its own Escape;
    everywhere else it stays, whatever the fullscreen preference says."""
    w = shell.Shell()
    try:
        state = {"fs": True}
        monkeypatch.setattr(w, "isFullScreen", lambda: state["fs"])
        w._settings.setValue("gfx/fs_hide_ui", True)

        w._go(0); assert not w._rail.isHidden(), "library: the nav is the only way out"
        w._go(1); assert not w._rail.isHidden(), "settings: same"
        w._go(2); assert w._rail.isHidden(), "the game still gets the whole screen"
        # ...and coming BACK from the game restores it, without leaving fullscreen.
        w._go(0); assert not w._rail.isHidden()
    finally:
        w.close()


def test_escape_and_f11_leave_fullscreen_from_any_page(app, monkeypatch):
    """The other half of the same trap: a key that gets you out. The game view had one,
    the rest of the shell had none -- so a window restored fullscreen on the library
    page answered nothing. Escape (off the game page, which owns its own Escape) and
    F11 (anywhere) both leave, and the preference follows so it does not come back."""
    from PyQt6.QtGui import QKeyEvent
    from PyQt6.QtCore import QEvent

    w = shell.Shell()
    try:
        state = {"fs": True}
        calls = []
        monkeypatch.setattr(w, "isFullScreen", lambda: state["fs"])
        monkeypatch.setattr(w, "showNormal", lambda: (calls.append("normal"),
                                                      state.update(fs=False)))

        def press(key):
            w.keyPressEvent(QKeyEvent(QEvent.Type.KeyPress, key,
                                      Qt.KeyboardModifier.NoModifier))

        w._go(0)
        w._settings.setValue("gfx/fullscreen", True)
        press(int(Qt.Key.Key_Escape))
        assert calls == ["normal"], "Escape must leave fullscreen on the library page"
        assert not cfg.fullscreen(w._settings), "and the preference must follow"

        state["fs"] = True; calls.clear()
        w._go(2)
        press(int(Qt.Key.Key_Escape))
        assert calls == [], "on the game page Escape belongs to the player, not here"
        press(int(Qt.Key.Key_F11))
        assert calls == ["normal"], "F11 leaves from anywhere"
    finally:
        w.close()


def test_the_window_never_reopens_bigger_than_the_screen(app):
    """`clamp_geometry` is the rule the startup/`fit_to_screen` path applies. Qt's own
    `restoreGeometry` only rescues a window that is ENTIRELY off-screen, so a row saved
    on a 1920x1080 desktop and reopened on a 1366x768 laptop came back oversized, with
    its bottom edge (and on a taller save, its title bar) past the panel."""
    from PyQt6.QtCore import QRect

    laptop = QRect(0, 0, 1366, 728)          # X240, minus the taskbar

    # too big both ways -> shrunk to the work area, not merely moved
    assert shell.clamp_geometry(QRect(100, 80, 1900, 1000), laptop) == QRect(0, 0, 1366, 728)
    # fits, but hangs off the right/bottom -> slid back, size untouched
    assert shell.clamp_geometry(QRect(1200, 700, 400, 300), laptop) == QRect(966, 428, 400, 300)
    # off the TOP-LEFT (a negative save from a second monitor) -> back on screen
    assert shell.clamp_geometry(QRect(-500, -400, 400, 300), laptop) == QRect(0, 0, 400, 300)
    # already inside -> byte-identical, never "helpfully" resized
    fits = QRect(40, 30, 900, 600)
    assert shell.clamp_geometry(fits, laptop) == fits
    # a screen whose origin is not (0,0) -- the second monitor case
    right = QRect(1366, 0, 1920, 1080)
    assert shell.clamp_geometry(QRect(1400, 40, 3000, 900), right) == QRect(1366, 40, 1920, 900)


def test_a_narrow_window_folds_the_rail_without_rewriting_the_preference(app):
    """On a small laptop the 254px nav is a quarter of the page, so it folds itself --
    and unfolds when there is room again. It must not answer FOR the user: the saved
    `win/rail_collapsed` is the user's own toggle, and an automatic fold that wrote to
    it would leave the rail collapsed forever once the window had been narrow once."""
    w = shell.Shell()
    try:
        w.show()
        w.resize(1100, 700)
        assert w._rail.width() > shell.RAIL_COLLAPSED_W, "wide: the nav is spelled out"
        assert not cfg.make_settings().value("win/rail_collapsed", False, type=bool)

        w.resize(shell.RAIL_AUTO_COLLAPSE_W - 60, 600)
        QApplication.processEvents()
        assert w._rail.width() == shell.RAIL_COLLAPSED_W, "narrow: folded to the strip"
        assert not cfg.make_settings().value("win/rail_collapsed", False, type=bool), \
            "the automatic fold is NOT the user's preference"

        w.resize(1100, 700)
        QApplication.processEvents()
        assert w._rail.width() > shell.RAIL_COLLAPSED_W, "room again -> unfolded"

        # ...but an explicit collapse survives a resize round-trip.
        w._toggle_rail(False)
        assert cfg.make_settings().value("win/rail_collapsed", False, type=bool)
        w.resize(shell.RAIL_AUTO_COLLAPSE_W - 60, 600); QApplication.processEvents()
        w.resize(1100, 700); QApplication.processEvents()
        assert w._rail.width() == shell.RAIL_COLLAPSED_W, "the user's choice is kept"
    finally:
        w.close()


def test_the_library_bars_wrap_instead_of_running_off_the_page(app):
    """The header (title + 4 buttons) and the view/search/sort row are ~1300px of
    controls. In a QHBoxLayout that is a hard minimum width: a 1366x768 laptop (less
    the nav, less 125% scaling) pushed "Open ROM", the search box and the sort controls
    off the right edge, with no scrollbar to reach them. Wrapped, they stay reachable --
    the page's minimum width is now its widest single control, not their sum.

    Every number here is DERIVED from the controls' own hints, never written down: a
    literal like "under 500px" is a bet on the font, and the CI runners do not have
    ours (see test_the_settings_panels_scroll)."""
    w = shell.Shell()
    try:
        w.show()
        page = w.library
        bar = [page._bios_btn, page._folder_btn, page._regen_btn, page._open_btn,
               page._search, page._filterbox, page._sortbox, page._revbtn]
        widest = max(b.sizeHint().width() for b in bar)
        in_a_row = sum(b.sizeHint().width() for b in bar)      # the QHBoxLayout floor
        assert page.minimumSizeHint().width() < in_a_row // 2, (
            f"library demands {page.minimumSizeHint().width()}px where its widest single "
            f"control is {widest}px -- the bars are still one unbreakable row")

        # Every control lands inside the page once it has been laid out narrow. Narrow
        # is "a bit more than the widest control", so the layout always has a legal
        # answer -- nothing can wrap its way out of a control wider than the page.
        page.resize(widest + 80, 700)
        QApplication.processEvents()
        for name, wid in (("open", page._open_btn), ("search", page._search),
                          ("sort", page._sortbox), ("reverse", page._revbtn)):
            right = wid.mapTo(page, wid.rect().topRight()).x()
            assert right <= page.width(), f"{name} runs {right - page.width()}px off the page"
    finally:
        w.close()


def test_the_settings_panels_scroll(app):
    """The tallest settings panel wants ~1000px. A 1366x768 laptop has ~614px of usable
    height at 125% scaling, and a page with no scroll area simply has no way to reach
    the rows past the fold.

    ⛔ WHAT THIS TEST MUST NOT DO -- it did it, twice, and broke CI both times. Its
    first version asserted that a 700x420 window scrolls in BOTH directions. True with
    the Windows font; false on the macOS and Linux runners, whose narrower fonts let a
    settings row fit the viewport, so the horizontal bar had nothing to scroll. That is
    a FONT MEASUREMENT dressed up as a feature: red CI, nothing broken.

    ⚖️ The rule this now follows: an assertion may only name what the CODE decides.
    The scroll POLICY is ours (asserted unconditionally). Whether a given axis
    overflows at a given size belongs to the font, so each axis is checked only where
    it is measured to overflow -- and the test refuses to pass vacuously by requiring
    that at least one axis did."""
    from PyQt6.QtWidgets import QScrollArea

    w = shell.Shell()
    try:
        area = w.settings._panel_scroll
        assert isinstance(area, QScrollArea) and area.widgetResizable()
        assert area.widget() is w.settings._stack, "the panels are what scrolls"
        # Ours, not the font's: bars appear when needed, on both axes.
        assert area.verticalScrollBarPolicy() == Qt.ScrollBarPolicy.ScrollBarAsNeeded
        assert area.horizontalScrollBarPolicy() == Qt.ScrollBarPolicy.ScrollBarAsNeeded

        w.show()
        w.resize(w.minimumWidth(), w.minimumHeight())     # 360x320, the shell's floor
        w._go(1)
        w.settings.show_category("controls")
        QApplication.processEvents()

        overflowed = []
        for axis, bar, widget_px, viewport_px in (
                ("vertically", area.verticalScrollBar(),
                 area.widget().height(), area.viewport().height()),
                ("horizontally", area.horizontalScrollBar(),
                 area.widget().width(), area.viewport().width())):
            if widget_px <= viewport_px:
                continue                  # fits on this font -- nothing to reach
            overflowed.append(axis)
            assert bar.maximum() > 0, (
                f"the panel is {widget_px}px {axis} in a {viewport_px}px viewport and "
                f"cannot be scrolled {axis} -- those rows are unreachable")
        assert overflowed, (
            "premise: at the shell's minimum size SOMETHING must overflow, or this test "
            "proves nothing about scrolling")
    finally:
        w.close()


def test_paused_frame_refits_after_a_layout_change(app):
    """Hiding the toolbar / going fullscreen resizes the canvas, but only a RUNNING tick
    re-blits — so a paused game kept an old, mis-scaled (stretched) frame. These paths now
    schedule a deferred re-fit once the layout has settled. Regression for the user report
    'aspect stretches after fullscreen and hiding the sidebar/toolbar'."""
    from PyQt6.QtWidgets import QApplication

    w = shell.Shell()
    try:
        play = w.play
        play.machine = object()                 # non-None so the blit guard passes
        calls = []
        play._blit = lambda: calls.append(1)     # count re-fits; skip the real numpy/Qt path

        # two requests in one turn collapse to a single deferred blit (no drag storm)
        play._reblit_soon(); play._reblit_soon()
        assert calls == [], "the re-fit is deferred, not immediate"
        QApplication.processEvents()
        assert len(calls) == 1, "exactly one deferred re-fit ran"

        # hiding the toolbar fires no resizeEvent of its own, yet must still re-fit
        calls.clear()
        play._toggle_toolbar(False)
        QApplication.processEvents()
        assert calls, "hiding the toolbar must re-fit the paused frame"
    finally:
        play.machine = None
        w.close()


def test_load_tab_gauges_read_vram_and_frame_rate(app):
    """The Load tab reads exact VRAM budgets (sprites, tiles) and shows the frame-rate
    as the honest overload signal, greyed when nothing is moving. Stub machine, no core."""
    import ngpc_debug as dbg_mod

    # A machine whose OAM has 2 active sprites and whose tilemaps reference a few tiles.
    mem = bytearray(0x10000)
    # OAM at 0x8800: sprite 0 active (priority bits set), sprite 1 active (has position)
    mem[0x8800 + 1] = 0x08          # sprite 0: priority != 0 -> active
    mem[0x8804 + 2] = 40            # sprite 1: H position set -> active
    # SCR1 map at 0x9000: make one entry point at tile 5
    mem[0x9000] = 5

    class _FakeMem:
        def read(self, addr, n): return bytes(mem[addr:addr + n])

    class _FakePlay:
        def __init__(self, mem_): self.machine = mem_; self._perf = {}
        def perf(self): return self._perf
    play = _FakePlay(_FakeMem())

    dbg = dbg_mod.DebugWindow(None, cfg.make_settings())
    try:
        dbg._play = play

        play._perf = {"game_fps": 60.0}
        dbg._refresh_load()
        assert "2 / 64" in dbg._g_spr._caption, "two active sprites counted"
        assert not dbg._g_spr._neutral and dbg._g_spr._value == 2 / 64
        assert "/ 512 tiles" in dbg._g_tile._caption
        # keeping up at 60 -> health gauge full, not neutral
        assert dbg._g_cpu._value == 1.0 and not dbg._g_cpu._neutral

        # a still screen (no sprite movement) -> frame-rate gauge goes neutral/grey
        play._perf = {"game_fps": 0.0}
        dbg._refresh_load()
        assert dbg._g_cpu._neutral, "nothing moving -> can't tell the rate -> grey"
    finally:
        dbg.close()


def test_gauge_colour_runs_green_to_red():
    """Low severity is green-ish, high severity is red-ish (independent of Qt state)."""
    import ngpc_debug as dbg_mod
    lo = dbg_mod._Gauge._severity_colour(0.0)
    hi = dbg_mod._Gauge._severity_colour(1.0)
    assert lo.green() > lo.red(), "low = green"
    assert hi.red() > hi.green(), "high = red"


def test_fantrad_tabs_crack_pointers_compare(app, tmp_path):
    """The Crack / Pointers / Compare tabs, end to end against a stub machine: crack a
    table from a readable word, find a pointer to an address, and diff a second ROM.
    All ROM-agnostic; no emulator, no real cartridge."""
    import ngpc_debug as dbg_mod
    from core.texttable import parse_tbl

    letters = {c: 0xA4 + i for i, c in enumerate("abcdefghijklmnopqrstuvwxyz")}
    enc = lambda s: bytes(letters[c] for c in s)

    class _FakeMem:
        def __init__(self, blob): self._blob = blob
        def read(self, addr, n): return bytes(self._blob[addr:addr + n])

    class _FakePlay:
        def __init__(self, mem): self.machine = mem

    # A little cart image: a word to crack, and a 32-bit pointer to address 0x000040.
    blob = bytearray(0x800)
    blob[0x10:0x10 + 5] = enc("magic")
    blob[0x100:0x104] = (0x000040).to_bytes(4, "little")
    dbg = dbg_mod.DebugWindow(None, cfg.make_settings())
    try:
        dbg._play = _FakePlay(_FakeMem(blob))

        # -- Crack: one readable word -> a table with its letters
        dbg._crack_words.setPlainText("magic")
        dbg._crack_from.setText("000000"); dbg._crack_size.setValue(1)
        dbg._crack_run()
        out = dbg._crack_out.toPlainText()
        assert "A8=e" not in out                      # 'e' not in "magic"
        assert f"{letters['m']:02X}=m".upper() in out.upper()
        dbg._crack_use()                              # adopt it in the Text tab
        assert dbg._txt_table is not None and dbg._txt_table.encode("magic") == enc("magic")

        # -- Pointers: find the reference to 0x000040
        dbg._ptr_width.setCurrentIndex(0)             # 32-bit LE
        dbg._ptr_base.setText("000000")
        dbg._ptr_from.setText("000000"); dbg._ptr_size.setValue(1)
        dbg._ptr_target.setText("000040"); dbg._ptr_tol.setValue(0)
        dbg._ptr_find()
        assert "000100" in dbg._ptr_out.toPlainText()

        # -- Compare: a second ROM that differs in one spot. Use a full-alphabet table
        # so BOTH sides decode (the cracked one only knew "magic"'s letters).
        dbg._txt_table = parse_tbl("".join(f"{v:02X}={c}\n" for c, v in letters.items()))
        romb = bytearray(blob)
        romb[0x10:0x15] = enc("power")                # "magic" -> "power"
        romb_path = tmp_path / "romB.ngc"
        romb_path.write_bytes(bytes(romb))
        dbg._cmp_path = str(romb_path)
        dbg._cmp_from.setText("000000"); dbg._cmp_size.setValue(1)
        dbg._cmp_run()
        diff = dbg._cmp_out.toPlainText()
        assert "000010" in diff and "'magic'" in diff and "'power'" in diff
    finally:
        dbg.close()


def test_custom_cover_survives_a_cache_version_bump(app, tmp_path, monkeypatch):
    """The bug a user hit: a title screen they placed by hand came back as the
    default rendered one after every update. The cover cache prunes anything that
    is not the CURRENT render version, and it used to prune by "is a .png" — so an
    update that bumped THUMB_VERSION deleted the user's file too.

    Two guarantees checked here: a chosen cover in `covers/` is what the library
    shows and is never re-rendered over, and the prune only ever removes files the
    thumbnail worker itself wrote.
    """
    from PyQt6.QtGui import QImage

    monkeypatch.setattr(shell, "THUMB_DIR", tmp_path / "thumbnails")
    monkeypatch.setattr(shell, "COVER_DIR", tmp_path / "covers")
    shell.THUMB_DIR.mkdir()
    shell.COVER_DIR.mkdir()

    rom = tmp_path / "roms" / "My Game.ngc"
    rom.parent.mkdir()
    rom.write_bytes(b"\xff" * 64)

    # what the user supplies, and what an older render version left behind
    mine = QImage(4, 4, QImage.Format.Format_RGB32)
    mine.fill(0xFF00FF00)
    assert mine.save(str(shell.COVER_DIR / "My Game.png"), "PNG")
    stale_auto = shell.THUMB_DIR / f"My Game.{shell._path_tag(rom)}.v1.png"
    assert mine.save(str(stale_auto), "PNG")
    hand_placed = shell.THUMB_DIR / "My Game.png"      # the pre-`covers/` workflow
    assert mine.save(str(hand_placed), "PNG")

    assert shell.custom_cover(rom) == shell.COVER_DIR / "My Game.png"

    seen: list[tuple[str, QImage]] = []
    worker = shell.ThumbWorker([rom], None)
    worker.ready.connect(lambda r, i: seen.append((r, i)))
    worker.run()

    # the chosen cover was served -- no ROM was booted to render one over it
    assert [r for r, _ in seen] == [str(rom)]
    assert seen[0][1].size() == mine.size()
    assert not shell._cover_path(rom).exists(), "must not render over a chosen cover"
    # the prune took the worker's own stale file and NOTHING else
    assert not stale_auto.exists()
    assert hand_placed.exists(), "a file the worker did not write is not its to delete"
    assert (shell.COVER_DIR / "My Game.png").exists()


def test_custom_cover_is_scoped_when_two_roms_share_a_name(app, tmp_path, monkeypatch):
    """Every NgpCraft project builds a `main.ngc`. A cover chosen for one of them
    must not become the cover of all of them."""
    monkeypatch.setattr(shell, "COVER_DIR", tmp_path / "covers")
    shell.COVER_DIR.mkdir()
    a = tmp_path / "projA" / "main.ngc"
    b = tmp_path / "projB" / "main.ngc"
    for p in (a, b):
        p.parent.mkdir()
        p.write_bytes(b"\xff" * 64)

    (shell.COVER_DIR / f"main.{shell._path_tag(a)}.png").write_bytes(b"x")
    assert shell.custom_cover(a) is not None
    assert shell.custom_cover(b) is None, "a path-scoped cover must not leak to a twin"

    # the plain name is the drop-in / move-proof form: it answers for both
    (shell.COVER_DIR / "main.png").write_bytes(b"x")
    assert shell.custom_cover(b) == shell.COVER_DIR / "main.png"
    assert shell.custom_cover(a).name.startswith("main."), "the scoped one still wins"
    assert shell.custom_cover(a) != shell.COVER_DIR / "main.png"


def test_choose_and_reset_cover_round_trip(app, tmp_path, monkeypatch):
    """The menu path end to end: choosing an image writes it under `covers/` and
    paints it now; resetting drops it and falls back to the rendered cache."""
    from PyQt6.QtGui import QImage
    from PyQt6.QtWidgets import QFileDialog

    monkeypatch.setattr(shell, "THUMB_DIR", tmp_path / "thumbnails")
    monkeypatch.setattr(shell, "COVER_DIR", tmp_path / "covers")
    shell.THUMB_DIR.mkdir()

    rom = tmp_path / "roms" / "Game.ngc"
    rom.parent.mkdir()
    rom.write_bytes(b"\xff" * 64)

    auto = QImage(4, 4, QImage.Format.Format_RGB32); auto.fill(0xFF0000FF)
    assert auto.save(str(shell._cover_path(rom)), "PNG")     # pretend it was rendered
    picked = tmp_path / "title.png"
    mine = QImage(8, 8, QImage.Format.Format_RGB32); mine.fill(0xFF00FF00)
    assert mine.save(str(picked), "PNG")

    # No ROM folder configured -> the page builds without starting the thumbnail
    # worker (which would boot a core), which is all this test needs.
    page = shell.LibraryPage(cfg.make_settings(), shell.lib.Library(tmp_path / "library.json"))
    try:
        page._all_roms = [rom]
        monkeypatch.setattr(QFileDialog, "getOpenFileName",
                            staticmethod(lambda *a, **k: (str(picked), "")))
        page.set_cover(str(rom))
        assert (shell.COVER_DIR / "Game.png").is_file()
        assert page._images[str(rom)].size() == mine.size(), "the new cover is shown at once"

        page.reset_cover(str(rom))
        assert shell.custom_cover(rom) is None
        assert page._images[str(rom)].size() == auto.size(), "fell back to the rendered one"
    finally:
        page._stop_worker()
        page.deleteLater()


class _FlatMachine:
    """A core whose screen never shows anything -- one solid colour, forever. That
    is literally what a cartridge booted WITHOUT a BIOS puts on screen."""

    def __init__(self, colour: int = 0xFFF) -> None:
        self._fb = [colour] * (shell.SCREEN_W * shell.SCREEN_H)
        self.frames = 0

    def run_frames(self, n: int) -> None:
        self.frames += n

    def framebuffer(self) -> list[int]:
        return self._fb


class _FlatSession:
    def __init__(self, rom, bios_path=None, autosave=False, colour=0xFFF) -> None:
        self.machine = _FlatMachine(colour)

    def close(self) -> None:
        pass


@pytest.mark.parametrize("colour", [0xFFF, 0x000])
def test_a_blank_capture_never_becomes_a_cover(app, tmp_path, monkeypatch, colour):
    """The bug: every cover in the grid was a white box. A ROM that never reaches
    its title screen renders one flat colour, and that frame used to be saved as
    the cover -- CACHED, so it stayed a white box even after the cause was fixed.
    A capture with no picture in it is now no cover at all: the card keeps its
    placeholder and the next launch tries again."""
    monkeypatch.setattr(shell, "THUMB_DIR", tmp_path / "thumbnails")
    monkeypatch.setattr(shell, "COVER_DIR", tmp_path / "covers")
    shell.THUMB_DIR.mkdir()
    monkeypatch.setattr(shell, "NativeSession",
                        lambda *a, **k: _FlatSession(*a, colour=colour, **k))

    rom = tmp_path / "roms" / "Blank.ngc"
    rom.parent.mkdir()
    rom.write_bytes(b"\xff" * 64)

    worker = shell.ThumbWorker([rom], tmp_path / "bios.bin")
    monkeypatch.setattr(worker, "_bios", tmp_path / "bios.bin")   # pretend it exists
    seen: list[str] = []
    worker.ready.connect(lambda r, _i: seen.append(r))
    worker.run()

    assert seen == [], "a blank frame must not be shown as a cover"
    assert not shell._cover_path(rom).exists(), "...and must not be cached to disk"


def test_without_a_bios_no_rom_is_booted_for_a_cover(app, tmp_path, monkeypatch):
    """Covers are rendered by BOOTING the game, and no game boots without a BIOS.
    Rendering anyway spends a full core boot per ROM to produce a blank box, so
    with no BIOS the pass does not run at all -- but a cover the user CHOSE is
    still served, since that one costs no boot."""
    from PyQt6.QtGui import QImage

    monkeypatch.setattr(shell, "THUMB_DIR", tmp_path / "thumbnails")
    monkeypatch.setattr(shell, "COVER_DIR", tmp_path / "covers")
    shell.THUMB_DIR.mkdir()
    shell.COVER_DIR.mkdir()

    def _boom(*a, **k):
        raise AssertionError("booted a ROM with no BIOS to render a cover")
    monkeypatch.setattr(shell, "NativeSession", _boom)

    roms = tmp_path / "roms"
    roms.mkdir()
    plain, chosen = roms / "Plain.ngc", roms / "Chosen.ngc"
    for p in (plain, chosen):
        p.write_bytes(b"\xff" * 64)
    mine = QImage(8, 8, QImage.Format.Format_RGB32); mine.fill(0xFF00FF00)
    assert mine.save(str(shell.COVER_DIR / "Chosen.png"), "PNG")

    seen: list[str] = []
    worker = shell.ThumbWorker([plain, chosen], None)
    worker.ready.connect(lambda r, _i: seen.append(r))
    worker.run()

    assert seen == [str(chosen)]
    assert not shell._cover_path(plain).exists()


def _write_png(path) -> None:
    """A cover the worker will actually accept. A file that is not a readable image is
    re-rendered instead of reused -- which is right, and not what these tests measure."""
    from PyQt6.QtGui import QImage
    img = QImage(4, 4, QImage.Format.Format_RGB32)
    img.fill(0xFF3366CC)
    assert img.save(str(path), "PNG")


class _BootRecorder:
    """A session that records which ROM was booted and shows nothing.

    Blank on purpose: a capture with no picture is never cached (see
    `test_a_blank_capture_never_becomes_a_cover`), so the recorder cannot accidentally
    make the covers it is measuring."""

    booted: list = []

    def __init__(self, rom, bios_path=None, autosave=False, **kw):
        _BootRecorder.booted.append(pathlib.Path(rom))
        self.machine = _FlatMachine()

    def close(self) -> None:
        pass


def _boots_for(monkeypatch, roms, bios):
    """Run the cover worker over `roms` and return the ROMs it actually booted."""
    _BootRecorder.booted = []
    monkeypatch.setattr(shell, "NativeSession", _BootRecorder)
    worker = shell.ThumbWorker(list(roms), bios)
    monkeypatch.setattr(worker, "_bios", bios)       # pretend the image is loadable
    worker.run()
    return list(_BootRecorder.booted)


def test_a_bios_appearing_renders_only_the_missing_covers(app, tmp_path, monkeypatch):
    """Set the BIOS in Settings and come back: the covers that could not be rendered
    without one are rendered now, without a restart — and WITHOUT re-rendering the ones
    that already exist. Re-rendering a real library means booting every game in it."""
    monkeypatch.setattr(shell, "THUMB_DIR", tmp_path / "thumbnails")
    monkeypatch.setattr(shell, "COVER_DIR", tmp_path / "covers")
    monkeypatch.setattr(shell, "DEFAULT_BIOS", tmp_path / "no-such-bios.bin")
    # Also point the clean-room HLE fallback at nothing: otherwise `_resolve_bios`
    # returns hle_bios/bios_hle.bin and this "no BIOS at all" case never happens.
    monkeypatch.setattr(shell, "HLE_BIOS", tmp_path / "no-such-hle.bin")
    shell.THUMB_DIR.mkdir()
    roms = tmp_path / "roms"
    roms.mkdir()
    done, missing = roms / "Done.ngc", roms / "Missing.ngc"
    for rom in (done, missing):
        rom.write_bytes(b"\xff" * 64)
    _write_png(shell._cover_path(done))

    settings = cfg.make_settings()
    settings.setValue("paths/rom_folder", str(roms))
    asked: list[list] = []
    monkeypatch.setattr(shell.LibraryPage, "_start_worker",
                        lambda self, r: asked.append(list(r)))

    page = shell.LibraryPage(settings, shell.lib.Library(tmp_path / "library.json"))
    try:
        assert page._bios_hint.isVisible() or not page.isVisible(), "the why is on screen"

        bios = tmp_path / "bios.bin"
        bios.write_bytes(b"\x00" * 64)
        settings.setValue("paths/bios", str(bios))
        asked.clear()
        page.show()          # back from Settings
        assert asked, "a BIOS appearing must render what could not be rendered before"
        booted = _boots_for(monkeypatch, asked[-1], bios)
        assert booted == [missing], "only the game without a cover is booted"
    finally:
        page._stop_worker()
        page.deleteLater()


def test_a_new_rom_costs_one_boot_not_a_library(app, tmp_path, monkeypatch):
    """Drop a game in the folder and it gets a cover on its own — one boot, not all of
    them. This is the whole reason covers are cached per ROM path."""
    monkeypatch.setattr(shell, "THUMB_DIR", tmp_path / "thumbnails")
    monkeypatch.setattr(shell, "COVER_DIR", tmp_path / "covers")
    shell.THUMB_DIR.mkdir()
    roms = tmp_path / "roms"
    roms.mkdir()
    old = roms / "Old.ngc"
    old.write_bytes(b"\xff" * 64)
    _write_png(shell._cover_path(old))
    bios = tmp_path / "bios.bin"
    bios.write_bytes(b"\x00" * 64)

    settings = cfg.make_settings()
    settings.setValue("paths/rom_folder", str(roms))
    settings.setValue("paths/bios", str(bios))
    asked: list[list] = []
    monkeypatch.setattr(shell.LibraryPage, "_start_worker",
                        lambda self, r: asked.append(list(r)))
    page = shell.LibraryPage(settings, shell.lib.Library(tmp_path / "library.json"))
    try:
        fresh = roms / "New.ngc"
        fresh.write_bytes(b"\xff" * 64)
        page.reload()
        asked.clear()
        page._resume_missing_covers()
        assert asked, "the new game must be picked up"
        assert _boots_for(monkeypatch, asked[-1], bios) == [fresh]
    finally:
        page._stop_worker()
        page.deleteLater()


def test_regenerating_covers_drops_only_what_we_rendered(app, tmp_path, monkeypatch):
    """The button that DOES cost a full re-render. It asks first (booting a whole
    library is minutes), and a cover the user chose is not ours to delete."""
    from PyQt6.QtWidgets import QMessageBox

    monkeypatch.setattr(shell, "THUMB_DIR", tmp_path / "thumbnails")
    monkeypatch.setattr(shell, "COVER_DIR", tmp_path / "covers")
    shell.THUMB_DIR.mkdir(); shell.COVER_DIR.mkdir()
    roms = tmp_path / "roms"
    roms.mkdir()
    rom = roms / "Game.ngc"
    rom.write_bytes(b"\xff" * 64)
    auto = shell._cover_path(rom)
    _write_png(auto)
    chosen = shell.COVER_DIR / "Game.png"
    chosen.write_bytes(b"mine")

    settings = cfg.make_settings()
    settings.setValue("paths/rom_folder", str(roms))
    asked: list[list] = []
    monkeypatch.setattr(shell.LibraryPage, "_start_worker",
                        lambda self, r: asked.append(list(r)))
    page = shell.LibraryPage(settings, shell.lib.Library(tmp_path / "library.json"))
    try:
        monkeypatch.setattr(QMessageBox, "question",
                            staticmethod(lambda *a, **k: QMessageBox.StandardButton.No))
        page.regenerate_covers()
        assert auto.exists(), "answering No must change nothing"

        asked.clear()
        monkeypatch.setattr(QMessageBox, "question",
                            staticmethod(lambda *a, **k: QMessageBox.StandardButton.Yes))
        page.regenerate_covers()
        assert not auto.exists(), "the rendered cover is dropped"
        assert chosen.exists(), "a cover the user chose is not ours to delete"
        assert asked and asked[-1] == [rom], "...and the library is rendered again"
    finally:
        page._stop_worker()
        page.deleteLater()


def test_link_tab_reads_the_cable_and_pokes_it(app):
    """The debugger's Link tab: it must land on its own refresh (the tab list and
    the refresh tuple are POSITIONAL -- a mismatch silently refreshes the wrong
    panel), read the channel out of a real core, and drive its three pokes:
    inject, impair, loopback."""
    import ngpc_debug as dbg_mod
    from core.native import NativeMachine

    class _FakePlay:
        """The slice of PlayPage the Link tab talks to."""

        def __init__(self, machine):
            self.machine = machine
            self.link_monitor = None
            self._net = None
            self.paused = False

        def set_link_monitor(self, mon): self.link_monitor = mon
        def link_mode(self): return "loopback" if self._net is not None else "none"
        def attach_net_link(self, net): self._net = net
        def detach_net_link(self): self._net = None

    machine = NativeMachine(b"\x00" * 0x10000)
    machine.serial_set_enabled(True)
    machine.serial_write_rx(b"\x41\x42")            # two bytes waiting, unread

    dbg = dbg_mod.DebugWindow(None, cfg.make_settings())
    try:
        dbg._play = _FakePlay(machine)
        idx = [dbg._tabs.tabText(i) for i in range(dbg._tabs.count())].index("Link")
        dbg._tabs.setCurrentIndex(idx)
        dbg.refresh()                               # dispatches through the tuple

        text = dbg._link_state.toPlainText()
        assert "cable" in text and "INTRX0" in text
        assert "queued for us       2" in text, "the core's counters are on screen"
        assert dbg._play.link_monitor is not None, "the tab tapped the cable"

        # inject: hex is parsed and queued for the console
        dbg._link_inject.setText("DE AD"); dbg._link_inject_mode.setCurrentText("Hex")
        dbg._link_send()
        assert dbg._link_mon.take_injected() == b"\xDE\xAD"
        dbg._link_inject.setText("zz"); dbg._link_send()
        assert "Not hex" in dbg._link_verdict.text(), "a typo says so, and sends nothing"

        # impair: the dialled-in numbers reach the monitor
        dbg._link_delay.setValue(3); dbg._link_drop.setValue(25)
        dbg._link_cut.setChecked(True)
        assert dbg._link_mon.impair.delay_frames == 3
        assert dbg._link_mon.impair.drop == pytest.approx(0.25)
        assert dbg._link_mon.impair.cut

        # loopback: attaching goes in as a network link, and off again
        dbg._link_loop.setCurrentIndex(1)
        assert type(dbg._play._net).__name__ == "LoopbackLink"
        dbg._link_loop.setCurrentIndex(0)
        assert dbg._play._net is None
    finally:
        dbg.close()


# ---- the BIOS ladder and the clean-room fallback -------------------------------
def _synthetic_rom() -> bytes:
    """A cartridge that boots and halts: enough to exercise the shell's wiring
    without needing a commercial ROM the repo does not ship."""
    rom = bytearray(b"\xFF" * 0x100000)
    rom[0:28] = b" LICENSED BY SNK CORPORATION"
    rom[0x1C:0x20] = (0x200040).to_bytes(4, "little")
    rom[0x23] = 0x10
    rom[0x40] = 0x05                      # halt
    return bytes(rom)


def test_the_bios_ladder_falls_back_to_the_clean_room_image(app, tmp_path, monkeypatch):
    """Configured path wins, then a real bios.bin, then the HLE image -- and the
    emulator must still have A BIOS at the bottom of the ladder, or nothing boots."""
    configured = tmp_path / "mine.bin"
    configured.write_bytes(b"\x00" * 0x10000)
    assert shell._resolve_bios(str(configured)) == configured

    assert shell._resolve_bios("") == shell.DEFAULT_BIOS or not shell.DEFAULT_BIOS.is_file()

    missing = tmp_path / "nope.bin"
    monkeypatch.setattr(shell, "DEFAULT_BIOS", missing)
    assert shell._resolve_bios("") == shell.HLE_BIOS
    assert shell._resolve_bios(str(missing)) == shell.HLE_BIOS, "a bad path must not win"


@pytest.mark.skipif(not shell.HLE_BIOS.is_file(), reason="the HLE image is not built")
def test_the_clean_room_image_never_takes_the_console_boot_path(app, tmp_path, monkeypatch):
    """The HLE image has no boot animation or setup UI -- that is the whole point of
    skipping them. Letting `real_bios` run against it would jump into code that is
    not there, so the shell has to notice which image it got."""
    monkeypatch.setattr(shell, "DEFAULT_BIOS", tmp_path / "absent.bin")
    rom = tmp_path / "synthetic.ngc"
    rom.write_bytes(_synthetic_rom())
    s = cfg.make_settings()
    s.setValue("paths/bios", "")
    s.setValue("general/real_bios", True)          # ...even when the user asked for it
    w = shell.Shell()
    try:
        w.play._frames_due = lambda: 2
        w.play.start(rom)
        assert w.play._bios_path() == shell.HLE_BIOS
        assert w.play._using_hle_bios() is True
        assert w.play._real_bios is False, "console boot needs a real BIOS"
    finally:
        w.play.stop()
        w.close()


@pytest.mark.skipif(not shell.HLE_BIOS.is_file(), reason="the HLE image is not built")
def test_a_savestate_round_trips_through_the_shell(app, tmp_path, monkeypatch):
    """Save, change the machine, load: the bytes must come back. The state is CPU +
    RAM, so this also covers the shell's own capture/apply pair, which nothing else
    exercised."""
    monkeypatch.setattr(shell, "STATE_DIR", tmp_path)
    rom = tmp_path / "synthetic.ngc"
    rom.write_bytes(_synthetic_rom())
    w = shell.Shell()
    try:
        w.play._frames_due = lambda: 2
        w.play.start(rom)
        for _ in range(4):
            w.play._tick()
        w.play.machine.write(0x005000, b"SAVEDSTATE")
        w.play.save_state(0)
        w.play.machine.write(0x005000, b"CLOBBERED!")
        assert w.play.machine.read(0x005000, 10) == b"CLOBBERED!"
        w.play.load_state(0)
        assert w.play.machine.read(0x005000, 10) == b"SAVEDSTATE"
    finally:
        w.play.stop()
        w.close()


def test_two_dumps_of_one_game_do_not_look_like_one_rom(app, tmp_path, monkeypatch):
    """The bug a user reported as "two thumbnails for one ROM": `_pretty` cuts the
    dump tags, so `Biomotor Unitron (USA)` and `Biomotor Unitron (USA, Europe)` --
    two real, different files -- both printed "Biomotor Unitron" under two covers.
    A title that is not unique now keeps the full file name."""
    monkeypatch.setattr(shell, "THUMB_DIR", tmp_path / "thumbnails")
    monkeypatch.setattr(shell, "COVER_DIR", tmp_path / "covers")
    shell.THUMB_DIR.mkdir()

    roms = tmp_path / "roms"
    roms.mkdir()
    twin_a = roms / "Biomotor Unitron (USA).ngc"
    twin_b = roms / "Biomotor Unitron (USA, Europe).ngc"
    alone = roms / "Faselei! (Europe).ngc"
    for p in (twin_a, twin_b, alone):
        p.write_bytes(b"\xff" * 64)

    titles = shell._titles([twin_a, twin_b, alone])
    assert titles[str(twin_a)] == "Biomotor Unitron (USA)"
    assert titles[str(twin_b)] == "Biomotor Unitron (USA, Europe)"
    assert titles[str(alone)] == "Faselei!", "a unique title still loses its tags"
    assert len(set(titles.values())) == 3, "no two cards may read the same"

    # ...and the cards actually carry them, in both views.
    page = shell.LibraryPage(cfg.make_settings(), shell.lib.Library(tmp_path / "library.json"))
    try:
        page._all_roms = sorted([twin_a, twin_b, alone])
        for view in (cfg.VIEW_GRID, cfg.VIEW_LIST):
            page._settings.setValue("library/view", view)
            page._arrange()
            shown = {lbl.text() for item in page._items.values()
                     for lbl in item.findChildren(QLabel)
                     if lbl.objectName() == "cardName"}
            assert shown == set(titles.values()), view
            assert page._items[str(twin_a)].toolTip() == str(twin_a), "hover says which file"
    finally:
        page._stop_worker()
        page.deleteLater()


def test_changing_the_bios_does_not_throw_the_covers_away(app, tmp_path, monkeypatch):
    """A cover is named after the ROM path alone, so switching BIOS leaves the pictures
    the previous one produced. That was briefly "fixed" by purging the cache on every
    BIOS change — and re-rendering a real library is minutes of booting every game, for
    a picture that differs by a shade. The cache stands; `Regenerate covers` is the
    button for the day you actually want them redone.

    The version prune stays: it is what keeps the folder from growing a copy per
    THUMB_VERSION bump, and it still only ever removes files the worker itself wrote."""
    monkeypatch.setattr(shell, "THUMB_DIR", tmp_path / "thumbnails")
    monkeypatch.setattr(shell, "COVER_DIR", tmp_path / "covers")
    shell.THUMB_DIR.mkdir()

    rom = tmp_path / "roms" / "Game.ngc"
    rom.parent.mkdir()
    rom.write_bytes(b"\xff" * 64)
    real, hle = tmp_path / "bios.bin", tmp_path / "bios_hle.bin"
    real.write_bytes(b"\x01" * 64)
    hle.write_bytes(b"\x02" * 64)

    cover = shell._cover_path(rom)
    cover.write_bytes(b"rendered once")
    stale = shell.THUMB_DIR / f"Game.{shell._path_tag(rom)}.v1.png"   # older version
    stale.write_bytes(b"old")
    hand_placed = shell.THUMB_DIR / "Game.png"                        # not ours
    hand_placed.write_bytes(b"x")

    shell.ThumbWorker([rom], hle)._prune()
    assert cover.exists()
    shell.ThumbWorker([rom], real)._prune()
    assert cover.exists(), "a different BIOS must not cost a whole library re-render"
    assert not stale.exists(), "...but an older render version is still cleaned up"
    assert hand_placed.exists(), "a file the worker did not write is not its to delete"


def _make_junction(link: pathlib.Path, target: pathlib.Path) -> bool:
    """A directory junction, the Windows way. Needs no privileges (a symlink does),
    which is exactly why sync clients and installers use them everywhere."""
    import subprocess
    if os.name != "nt":
        try:
            link.symlink_to(target, target_is_directory=True)
            return True
        except OSError:
            return False
    r = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(target)],
                       capture_output=True)
    return r.returncode == 0 and link.exists()


def test_the_scan_finds_roms_recursively_and_ignores_the_rest(tmp_path):
    root = tmp_path / "lib"
    (root / "sub" / "deeper").mkdir(parents=True)
    wanted = [root / "A.ngc", root / "sub" / "B.NGP", root / "sub" / "deeper" / "C.zip"]
    for p in wanted:
        p.write_bytes(b"\xff" * 16)
    for junk in ("notes.txt", "A.ngc.tmp", "cover.png"):
        (root / junk).write_bytes(b"x")

    assert shell.scan_roms(root) == sorted(wanted)


def test_one_rom_behind_a_junction_is_one_card(tmp_path):
    """THE bug: a user with a SINGLE ROM in his folder saw two cards. A junction --
    what every drive-sync client, and the legacy profile folders, are made of --
    makes one file reachable under two paths, and the old `rglob` scan walked
    straight through it. Worse, a junction pointing back up the tree made `rglob`
    recurse until Windows refused the path, and the `except OSError` around it threw
    the WHOLE scan away.

    `entry.is_symlink()` is False for a junction, so `os.walk(followlinks=False)`
    would not have helped either: the reparse tag is what says so.
    """
    root = tmp_path / "lib"
    (root / "inner").mkdir(parents=True)
    (root / "Game.ngc").write_bytes(b"\xff" * 16)
    if not _make_junction(root / "inner" / "link", root):
        pytest.skip("this platform would not create a directory junction")

    found = shell.scan_roms(root)
    assert found == [root / "Game.ngc"], "one file, one card -- by its real path"


def test_a_hardlinked_rom_is_not_a_second_game(tmp_path):
    """The identity check behind the junction fix, on its own: two directory entries
    for ONE file on disk are one game, whatever made them."""
    root = tmp_path / "lib"
    (root / "sub").mkdir(parents=True)
    real = root / "Game.ngc"
    real.write_bytes(b"\xff" * 16)
    try:
        os.link(real, root / "sub" / "Game.ngc")
    except (OSError, NotImplementedError, AttributeError):
        pytest.skip("no hardlink support here")

    assert shell.scan_roms(root) == [real], "the shallowest path is the one kept"


def test_the_scan_survives_a_folder_it_cannot_read(tmp_path, monkeypatch):
    """One unreadable folder must cost that folder, not the library."""
    root = tmp_path / "lib"
    (root / "locked").mkdir(parents=True)
    (root / "Game.ngc").write_bytes(b"\xff" * 16)

    real_scandir = os.scandir

    def picky(path):
        if str(path).endswith("locked"):
            raise PermissionError(str(path))
        return real_scandir(path)

    monkeypatch.setattr(shell.os, "scandir", picky)
    assert shell.scan_roms(root) == [root / "Game.ngc"]


def _fake_bios(kind: str) -> bytes:
    """A 64 KiB image carrying the byte pattern that identifies a BIOS: the
    `ld (0x6F91), #` its boot code runs, plus (colour only) a K2GE register access."""
    img = bytearray(b"\x00" * 0x10000)
    img[0x100:0x105] = b"\xF1\x91\x6F\x00" + (b"\x10" if kind == "colour" else b"\x00")
    if kind == "colour":
        img[0x200:0x203] = b"\xE2\x87\x00"
    return bytes(img)


def test_bios_kind_tells_the_two_consoles_apart(tmp_path):
    """Which machine an image is the BIOS of, read the way a cartridge reads it: the
    console-type byte the BIOS stamps at 0x6F91 (0x10 = NGPC, 0x00 = mono NGP), plus
    whether it ever addresses a K2GE colour register."""
    colour, mono, junk = (tmp_path / n for n in ("c.bin", "m.bin", "j.bin"))
    colour.write_bytes(_fake_bios("colour"))
    mono.write_bytes(_fake_bios("mono"))
    junk.write_bytes(b"\x00" * 0x10000)

    assert shell.bios_kind(colour) == shell.BIOS_COLOUR
    assert shell.bios_kind(mono) == shell.BIOS_MONO
    assert shell.bios_kind(junk) == shell.BIOS_UNKNOWN
    assert shell.bios_kind(tmp_path / "gone.bin") == shell.BIOS_UNKNOWN
    assert shell.bios_kind(None) == shell.BIOS_UNKNOWN


def test_the_bios_selector_picks_which_image_boots(app, tmp_path, monkeypatch):
    """Three slots, one selector: the user's colour dump, the user's mono dump, and
    the built-in HLE image. What the panel says is what boots."""
    monkeypatch.setattr(shell, "DEFAULT_BIOS", tmp_path / "no-bios-here.bin")
    monkeypatch.setattr(shell, "DEFAULT_BIOS_MONO", [tmp_path / "no-mono-here.bin"])
    colour, mono = tmp_path / "ngpc.bin", tmp_path / "ngp.bin"
    colour.write_bytes(_fake_bios("colour"))
    mono.write_bytes(_fake_bios("mono"))

    s = cfg.make_settings()
    s.setValue("paths/bios", str(colour))
    s.setValue("paths/bios_mono", str(mono))

    s.setValue("bios/active", cfg.BIOS_USE_COLOUR)
    assert shell.resolve_selected_bios(s) == (colour, cfg.BIOS_USE_COLOUR)
    s.setValue("bios/active", cfg.BIOS_USE_MONO)
    assert shell.resolve_selected_bios(s) == (mono, cfg.BIOS_USE_MONO)
    s.setValue("bios/active", cfg.BIOS_USE_HLE)
    assert shell.resolve_selected_bios(s) == (shell.HLE_BIOS, cfg.BIOS_USE_HLE)

    # An empty slot boots SOMETHING rather than nothing -- and reports the slot that
    # actually answered, so the panel can say the console asked for is not the one running.
    s.setValue("bios/active", cfg.BIOS_USE_MONO)
    s.setValue("paths/bios_mono", "")
    assert shell.resolve_selected_bios(s) == (colour, cfg.BIOS_USE_COLOUR)

    # ...and with no BIOS at all in reach, the ladder still ends on the HLE image.
    s.setValue("paths/bios", "")
    assert shell.resolve_selected_bios(s) == (shell.HLE_BIOS, cfg.BIOS_USE_HLE)


def test_a_colour_cartridge_on_the_mono_console_is_told_it_is_mono(app, tmp_path):
    """THE bug behind the request. 0x6F91 is how a cartridge knows which machine it is
    in. It used to be seeded from the CARTRIDGE HEADER, which for a colour game says
    0x10 -- so a colour cartridge in our "mono NGP" was still told NGPC and behaved
    exactly as it does on one. The console owns that byte, not the cartridge: the mono
    NGP's own BIOS writes 0x00 there (disassembled: `ld (0x6F91), 0x00`), whatever is
    in the slot."""
    from core.native_session import NativeSession

    rom = tmp_path / "colour.ngc"
    rom.write_bytes(_synthetic_rom())          # header 0x23 = 0x10: a COLOUR cartridge
    for k1ge, expect in ((False, 0x10), (True, 0x00)):
        s = NativeSession(rom, autosave=False, save_to_rom=False, k1ge_console=k1ge)
        try:
            assert s.machine.read(0x006F91, 1)[0] == expect, f"k1ge_console={k1ge}"
        finally:
            s.close()


def test_the_console_follows_the_bios_that_boots(app, tmp_path, monkeypatch):
    """What the user asked for in one line: a cartridge must detect a K1GE console when
    the selected BIOS is a K1GE one. It is the same question twice -- a game reads 0x6F91,
    and 0x6F91 is what the BIOS stamps -- so the machine is derived from the image that
    will actually boot, not from the slot it happens to sit in."""
    monkeypatch.setattr(shell, "DEFAULT_BIOS", tmp_path / "none.bin")
    monkeypatch.setattr(shell, "DEFAULT_BIOS_MONO", [tmp_path / "none-mono.bin"])
    colour, mono = tmp_path / "ngpc.bin", tmp_path / "ngp.bin"
    colour.write_bytes(_fake_bios("colour"))
    mono.write_bytes(_fake_bios("mono"))

    s = cfg.make_settings()
    s.setValue("paths/bios", str(colour))
    s.setValue("paths/bios_mono", str(mono))
    s.setValue("gfx/mono_mode", cfg.MONO_K2GE)      # the Console setting stays on NGPC

    s.setValue("bios/active", cfg.BIOS_USE_COLOUR)
    assert shell.console_is_mono(s) is False
    s.setValue("bios/active", cfg.BIOS_USE_MONO)
    assert shell.console_is_mono(s) is True, "selecting the K1GE BIOS selects the K1GE"

    # The IMAGE decides, not the slot: a mono dump filed under the colour slot still
    # boots a mono console, and a colour dump in the mono slot does not fake one.
    s.setValue("bios/active", cfg.BIOS_USE_COLOUR)
    s.setValue("paths/bios", str(mono))
    assert shell.console_is_mono(s) is True
    s.setValue("bios/active", cfg.BIOS_USE_MONO)
    s.setValue("paths/bios_mono", str(colour))
    assert shell.console_is_mono(s) is False

    # The HLE image is neither console's BIOS, so the explicit setting is what is left.
    s.setValue("bios/active", cfg.BIOS_USE_HLE)
    assert shell.console_is_mono(s) is False
    s.setValue("gfx/mono_mode", cfg.MONO_K1GE)
    assert shell.console_is_mono(s) is True, "the Console setting still forces it"


def test_the_mono_console_renders_in_greys_not_in_two_tones(app, tmp_path):
    """The K1GE render path. On that silicon the LUT value IS the shade -- there is no
    12-bit palette (K1GE Tech Ref §3-7) -- so the picture must not be resolved through
    colour RAM the machine does not have. Reading it there is what left the NGP boot
    logo sitting on a wallpaper of SNK tiles, in two tones."""
    from core.native_session import NativeSession

    rom = tmp_path / "colour.ngc"
    rom.write_bytes(_synthetic_rom())
    ramp = b"\xff\x0f\xdd\x0d\xbb\x0b\x99\x09\x66\x06\x44\x04\x22\x02\x00\x00"
    with NativeSession(rom, autosave=False, save_to_rom=False, k1ge_console=True) as mono:
        assert mono.machine.read(0x008380, 16) == ramp, "eight greys, stamped by the console"
        # ...and it is the console's own, so it is there with no BIOS supplied at all --
        # which is the case that used to render the whole screen in two tones.
        assert mono.machine.read(0x0083C0, 16) == ramp, "same ramp on every plane"


def _plugin_dir(tmp_path):
    """A folder of filter plugins, as a user would have it: one good, three broken."""
    d = tmp_path / "myfilters"
    d.mkdir()
    (d / "double.py").write_text(
        'import numpy as np\n'
        'NAME = "Double"\n'
        'SCALE = 2\n'
        'def apply(rgb):\n'
        '    return np.repeat(np.repeat(rgb, 2, axis=0), 2, axis=1)\n', encoding="utf-8")
    (d / "syntax.py").write_text("def broken(:\n", encoding="utf-8")
    (d / "shape.py").write_text(
        'NAME = "Bad shape"\nSCALE = 2\ndef apply(rgb):\n    return rgb\n', encoding="utf-8")
    (d / "helper.py").write_text("# not a filter at all\nVALUE = 1\n", encoding="utf-8")
    return d


def test_filter_plugins_are_loaded_from_a_folder_the_user_owns(tmp_path):
    """The good scalers (HQx, 2xSaI, xBRZ) are copyleft implementations an MIT emulator
    cannot redistribute, so it loads them instead of shipping them — from a folder the
    user points at, with no directory created on their disk for it."""
    import ngpc_filters

    ok, bad = ngpc_filters.discover(_plugin_dir(tmp_path))
    assert [p.name for p in ok] == ["Double"]
    assert ok[0].scale == 2 and ok[0].ident == "plugin:double"

    # Every rejected file is reported WITH ITS REASON: a plugin that silently fails to
    # appear comes back to us as "your filter list is broken".
    reasons = {b.source.name: b.reason for b in bad}
    assert set(reasons) == {"syntax.py", "shape.py", "helper.py"}
    assert "SyntaxError" in reasons["syntax.py"]
    assert "expected" in reasons["shape.py"]        # smoke-tested, not just declared
    assert "NAME" in reasons["helper.py"]

    assert ngpc_filters.discover("") == ([], []), "no folder set: the feature is simply off"
    assert ngpc_filters.discover(tmp_path / "nope") == ([], [])


def test_a_filter_that_breaks_costs_the_filter_not_the_frame(tmp_path):
    """A plugin verified at load can still fail on a real picture. That must never take
    the game down with it: the frame comes back unfiltered and the plugin is dropped."""
    import ngpc_filters
    import numpy as np

    d = _plugin_dir(tmp_path)
    reg = ngpc_filters.FilterRegistry()
    reg.sync(str(d))
    assert reg.get("plugin:double") is not None

    def explode(rgb):
        raise RuntimeError("boom")
    object.__setattr__(reg.plugins[0], "apply", explode)

    assert reg.run("plugin:double", np.zeros((152, 160, 3), np.uint8)) is None
    assert "boom" in reg.disabled_reason("plugin:double")
    assert reg.get("plugin:double") is None, "...and it is not tried again this session"


def test_a_plugin_never_returns_more_picture_than_was_asked_for(tmp_path):
    """The plugin's magnification counts toward the requested scale, and a factor that
    does not divide it settles for the largest multiple that fits — the window fit
    covers the rest, as it already does for any non-integer window."""
    import ngpc_filters
    import ngpc_video as vid

    reg = ngpc_filters.FilterRegistry()
    reg.sync(str(_plugin_dir(tmp_path)))
    plug = reg.plugins[0]
    scaler = lambda rgb: (reg.run(plug.ident, rgb), plug.scale)  # noqa: E731
    fb = [0] * (vid.SCREEN_W * vid.SCREEN_H)

    for k in (1, 2, 4, 6):
        assert (vid.render_array(fb, k, vid.FILTER_LCD_GRID, vid.COLOR_RAW, scaler).shape
                == vid.render_array(fb, k, vid.FILTER_NONE, vid.COLOR_RAW).shape), k
    at3 = vid.render_array(fb, 3, vid.FILTER_NONE, vid.COLOR_RAW, scaler).shape
    assert at3 == (vid.SCREEN_H * 2, vid.SCREEN_W * 2, 3), "x2 into x3 stays at x2"


# --------------------------------------------------------------------------
# HW Regs tab — the hardware registers decoded field by field.
# --------------------------------------------------------------------------
def _tab_index(dbg, title: str) -> int:
    return [dbg._tabs.tabText(i) for i in range(dbg._tabs.count())].index(title)


class _FakeCpu:
    pc = 0x200000
    sr_raw = 0
    flags = 0
    iff_level = 7
    regs = (0,) * 8


class _FakeRegMem:
    """A flat address space the tab can read. 64 KiB covers every register in the
    map (the highest is 0x87E2).

    `cpu()` is here because ANY refresh can land on the CPU tab -- a checkbox that
    calls `refresh()` does, for one -- and a missing method there raises inside a
    Qt slot, where PyQt calls qFatal and the process dies with no traceback."""

    def __init__(self, values: dict[int, int]):
        self._b = bytearray(0x10000)
        for addr, val in values.items():
            self._b[addr] = val & 0xFF

    def read(self, addr, n=1):
        return bytes(self._b[addr:addr + n])

    def poke(self, addr, val):
        self._b[addr] = val & 0xFF

    def cpu(self):
        return _FakeCpu()


class _FakeBreakSet:
    items: list = []


class _FakeRegPlay:
    """The slice of PlayPage the debug window reads.

    `paused` is read by `refresh()` before it reaches any tab -- without it the
    AttributeError is raised inside a Qt slot, PyQt calls qFatal, and the process
    dies with no traceback at all. `symbols` and `breaks` are what `attach()` hands
    over so a breakpoint guard can name a symbol. Every one of these is here
    because a borrowed method really reads it."""

    paused = False

    def __init__(self, mem):
        self.machine = mem
        self.symbols = None
        self.breaks = _FakeBreakSet()


def test_every_tab_has_its_own_refresher(app):
    """The pairing that replaced a positional tuple. If a tab is ever added without
    a refresher — or the two lists drift — this fails instead of a tab silently
    redrawing a different panel."""
    import ngpc_debug as dbg_mod

    dbg = dbg_mod.DebugWindow(None, cfg.make_settings())
    try:
        assert len(dbg._tab_refresh) == dbg._tabs.count()
        for i in range(dbg._tabs.count()):
            dbg._tabs.setCurrentIndex(i)
            dbg.refresh()          # detached: must not raise on any tab
    finally:
        dbg.close()


def test_hwregs_tab_decodes_the_registers_it_reads(app):
    """0x8118 = 0x87 is 'backdrop on, palette 7'. The Memory tab can show the byte;
    only this tab can say what it means — which is the whole reason it exists."""
    import ngpc_debug as dbg_mod

    dbg = dbg_mod.DebugWindow(None, cfg.make_settings())
    try:
        mem = _FakeRegMem({0x008118: 0x87, 0x000071: 0x32, 0x000020: 0x81,
                           0x00006E: 0x80, 0x008004: 0xFF, 0x008005: 0x98})
        dbg._play = _FakeRegPlay(mem)
        dbg._tabs.setCurrentIndex(_tab_index(dbg, "HW Regs"))
        dbg.refresh()

        rows = {}
        for r, (reg, fld) in enumerate(dbg._hw_rows):
            if reg is None:
                continue
            key = reg.name if fld is None else f"{reg.name}.{fld.name}"
            rows[key] = (dbg._hw_table.item(r, dbg.HW_COL_VALUE).text(),
                         dbg._hw_table.item(r, dbg.HW_COL_MEANING).text())

        assert rows["BGC"][0] == "87"
        assert "ON" in rows["BGC.BGON"][1]
        assert rows["BGC.BGC"][1] == "backdrop palette entry 7"
        # INTE45 = 0x32: VBlank at level 2, the sound CPU's INT5 at 3.
        assert rows["INTE45.INT4"][1] == "level 2"
        assert rows["INTE45.INT5"][1] == "level 3"
    finally:
        dbg.close()


def test_hwregs_tab_lights_a_register_the_frame_it_changes(app):
    """Watching which registers a scene actually touches is the live half of this
    tab. A value that moved is tinted; one that held still is not."""
    import ngpc_debug as dbg_mod

    dbg = dbg_mod.DebugWindow(None, cfg.make_settings())
    try:
        mem = _FakeRegMem({0x008032: 0x00, 0x008118: 0x80})
        dbg._play = _FakeRegPlay(mem)
        dbg._tabs.setCurrentIndex(_tab_index(dbg, "HW Regs"))
        dbg.refresh()

        row_of = {}
        for r, (reg, fld) in enumerate(dbg._hw_rows):
            if reg is not None and fld is None:
                row_of[reg.name] = r

        mem.poke(0x008032, 0x40)        # the game scrolls plane 1
        dbg.refresh()
        scrolled = dbg._hw_table.item(row_of["S1SO.H"], dbg.HW_COL_VALUE)
        still = dbg._hw_table.item(row_of["BGC"], dbg.HW_COL_VALUE)
        assert scrolled.text() == "40"
        assert scrolled.background() != still.background(), \
            "the register that moved must be distinguishable from the one that did not"
    finally:
        dbg.close()


def test_hwregs_checks_report_a_documented_illegal_state(app):
    """ngpcspec.txt: a window whose origin plus size passes the hardware limit
    'disrupts display and Vint/Hint generation'. That is a defect the register
    values state outright, and it is invisible in hex."""
    import ngpc_debug as dbg_mod

    dbg = dbg_mod.DebugWindow(None, cfg.make_settings())
    try:
        dbg._play = _FakeRegPlay(_FakeRegMem({0x008002: 100, 0x008004: 100,
                                              0x000020: 0x81, 0x00006E: 0x80}))
        dbg._tabs.setCurrentIndex(_tab_index(dbg, "HW Regs"))
        dbg.refresh()
        assert dbg._hw_checks.isVisibleTo(dbg)
        assert "overflows horizontally" in dbg._hw_checks.toPlainText()
    finally:
        dbg.close()


def test_hwregs_filter_narrows_the_table(app):
    import ngpc_debug as dbg_mod

    dbg = dbg_mod.DebugWindow(None, cfg.make_settings())
    try:
        dbg._play = _FakeRegPlay(_FakeRegMem({}))
        dbg._tabs.setCurrentIndex(_tab_index(dbg, "HW Regs"))
        dbg.refresh()
        everything = dbg._hw_table.rowCount()

        def shown():
            return {reg.name for reg, _f in dbg._hw_rows if reg is not None}

        # By address — which is how you arrive here from the memory viewer or from
        # a disassembly line, holding a number and no name at all.
        dbg._hw_filter.setText("8118")
        assert 0 < dbg._hw_table.rowCount() < everything
        assert shown() == {"BGC"}

        # By name.
        dbg._hw_filter.setText("wdmod")
        assert shown() == {"WDMOD"}

        # By what it does: matching is deliberately loose over the summary and the
        # group, so a word you remember finds the register you forgot the name of.
        dbg._hw_filter.setText("watchdog")
        assert {"WDMOD", "WDCR"} <= shown()

        dbg._hw_filter.setText("")
        assert dbg._hw_table.rowCount() == everything
    finally:
        dbg.close()


def test_hwregs_hide_untouched_keeps_the_ones_with_no_known_reset(app):
    """'Untouched' is only sayable about a register whose reset value is
    documented. Hiding the rest would read as 'this game does not use it' — a
    claim the table has no basis for."""
    import ngpc_debug as dbg_mod
    from core import hwregs

    dbg = dbg_mod.DebugWindow(None, cfg.make_settings())
    try:
        # Everything at its reset value except BGC, which the game has changed.
        at_reset = {r.addr: r.reset for r in hwregs.all_registers() if r.reset is not None}
        at_reset[0x008118] = 0x83
        dbg._play = _FakeRegPlay(_FakeRegMem(at_reset))
        dbg._tabs.setCurrentIndex(_tab_index(dbg, "HW Regs"))
        dbg.refresh()

        dbg._hw_changed_only.setChecked(True)
        shown = {reg.name for reg, _f in dbg._hw_rows if reg is not None}
        assert "BGC" in shown, "the one register the game moved must survive"
        assert "REF" not in shown, "a register still at its documented reset is hidden"
        assert "TREG0" in shown, "no documented reset -> never hidden"
    finally:
        dbg.close()


# --------------------------------------------------------------------------
# Tilemap tab — the scroll planes as whole maps, with the per-line camera.
# --------------------------------------------------------------------------
class _FakePlaneMem(_FakeRegMem):
    """A machine with VRAM worth drawing and, optionally, a raster log."""

    def __init__(self, *, scroll_of_line=None):
        super().__init__({})
        self._scroll_of_line = scroll_of_line
        from core import tilemap_view as tv
        # One solid tile, one palette entry, and the whole plane paved with it.
        self._b[tv.CHAR_RAM:tv.CHAR_RAM + 16] = b"\x00" * 16          # tile 0: value 0
        self._b[tv.CHAR_RAM + 16:tv.CHAR_RAM + 32] = b"\xFF" * 16     # tile 1: value 3
        base = tv.PALETTE_BASE[tv.SCR1] + 6      # code 0, value 3 -> white
        self._b[base], self._b[base + 1] = 0xFF, 0x0F
        for i in range(32 * 32):
            self._b[tv.MAP_BASE[tv.SCR1] + i * 2] = 1
        self._b[0x008032] = 40                   # end-of-frame scroll, for the fallback

    def raster_log(self):
        from core import tilemap_view as tv
        if self._scroll_of_line is None:
            raise RuntimeError("no raster log on this core")
        rows = []
        for line in range(tv.SCREEN_H):
            blk = bytearray(0x40)
            blk[0x32] = self._scroll_of_line(line) & 0xFF
            rows.append(bytes(blk))
        return tuple(rows)


def test_tilemap_tab_draws_the_plane_and_counts_what_is_on_it(app):
    import ngpc_debug as dbg_mod

    dbg = dbg_mod.DebugWindow(None, cfg.make_settings())
    try:
        dbg._play = _FakeRegPlay(_FakePlaneMem(scroll_of_line=lambda ln: 40))
        dbg._tabs.setCurrentIndex(_tab_index(dbg, "Tilemap"))
        dbg.refresh()
        assert dbg._tm_arr is not None
        assert dbg._tm_arr.shape[0] == 256 * dbg.TILEMAP_SCALE
        assert "1 distinct tiles" in dbg._tm_note.text()
        assert "K2GE colour" in dbg._tm_note.text()
    finally:
        dbg.close()


def test_tilemap_tab_names_line_scroll_instead_of_hiding_it(app):
    """A single end-of-frame scroll number cannot tell you the game is scrolling
    per line — which is exactly how parallax is done here. The tab reads the
    registers each line was actually drawn with."""
    import ngpc_debug as dbg_mod

    dbg = dbg_mod.DebugWindow(None, cfg.make_settings())
    try:
        dbg._play = _FakeRegPlay(_FakePlaneMem(scroll_of_line=lambda ln: 40))
        dbg._tabs.setCurrentIndex(_tab_index(dbg, "Tilemap"))
        dbg.refresh()
        assert "no line-scroll" in dbg._tm_note.text()

        dbg._play = _FakeRegPlay(_FakePlaneMem(scroll_of_line=lambda ln: 40 + ln % 12))
        dbg.refresh()
        assert "line-scroll 11 px" in dbg._tm_note.text()
    finally:
        dbg.close()


def test_tilemap_tab_says_so_when_it_has_no_per_line_registers(app):
    """Falling back to one scroll value for the whole frame is the mistake this
    view exists to expose. It stays reachable — and never silent."""
    import ngpc_debug as dbg_mod

    dbg = dbg_mod.DebugWindow(None, cfg.make_settings())
    try:
        dbg._play = _FakeRegPlay(_FakePlaneMem(scroll_of_line=None))
        dbg._tabs.setCurrentIndex(_tab_index(dbg, "Tilemap"))
        dbg.refresh()
        assert "no raster log" in dbg._tm_note.text()
        assert "scroll X 40" in dbg._tm_note.text(), "it fell back to the live register"
    finally:
        dbg.close()


def test_tilemap_hover_reports_the_entry_you_would_go_and_poke(app):
    import ngpc_debug as dbg_mod
    from PyQt6.QtWidgets import QApplication

    dbg = dbg_mod.DebugWindow(None, cfg.make_settings())
    try:
        mem = _FakePlaneMem(scroll_of_line=lambda ln: 0)
        mem._b[0x009000 + (4 * 32 + 5) * 2] = 0x21        # tile low bits
        mem._b[0x009000 + (4 * 32 + 5) * 2 + 1] = 0x93    # 9th bit + palette 9 + H flip
        dbg._play = _FakeRegPlay(mem)
        dbg._tabs.setCurrentIndex(_tab_index(dbg, "Tilemap"))
        dbg.refresh()

        info = dbg._tm_info(5, 4)
        # entry 133 = ty*32+tx, two bytes each -> 0x9000 + 266
        assert "0x00910A" in info
        assert "tile 289 (0x121)" in info
        assert "0x00B210" in info                 # where those 16 bytes live
        assert "palette 9" in info and "flip H" in info
        assert dbg._tm_info(99, 0) is None

        dbg._tm_status(info, copy=True)
        assert QApplication.clipboard().text() == info
        assert dbg._tm_status_line.text().startswith("✔ copied")
    finally:
        dbg.close()


# --------------------------------------------------------------------------
# Sound CPU tab — the Z80, which used to be one line of text.
# --------------------------------------------------------------------------
class _FakeZ80Mem(_FakeRegMem):
    def __init__(self, aux, shared=b""):
        super().__init__({})
        self._aux = aux
        self._b[0x007000:0x007000 + len(shared)] = shared

    def aux_state(self):
        return self._aux


def _z80_aux(**kw):
    from types import SimpleNamespace
    base = dict(
        z80_a=0, z80_f=0, z80_b=0, z80_c=0, z80_d=0, z80_e=0, z80_h=0, z80_l=0,
        z80_a2=0, z80_f2=0, z80_b2=0, z80_c2=0, z80_d2=0, z80_e2=0, z80_h2=0, z80_l2=0,
        z80_ix=0, z80_iy=0, z80_sp=0x0FF0, z80_pc=0,
        z80_i=0, z80_r=0, z80_im=1, z80_iff1=1, z80_iff2=1,
        z80_halted=0, z80_running=1, z80_nmi_pending=0, z80_int_pending=0,
        z80_trapped=0, z80_trap_prefix=0, z80_trap_opcode=0, z80_trap_pc=0,
        z80_cycle_credit=0, z80_executed=0,
    )
    base.update(kw)
    return SimpleNamespace(**base)


def test_sound_cpu_tab_disassembles_from_the_z80_pc(app):
    import ngpc_debug as dbg_mod

    dbg = dbg_mod.DebugWindow(None, cfg.make_settings())
    try:
        aux = _z80_aux(z80_pc=0x0003, z80_b=0x12, z80_c=0x34, z80_b2=0x99, z80_c2=0x88)
        # Z80 0x0000 is main-bus 0x7000 -- the shared RAM the two CPUs talk through.
        code = bytes([0x21, 0x00, 0x40,     # 0000 ld hl,0x4000
                      0x7E,                 # 0003 ld a,(hl)   <- PC
                      0xC9])                # 0004 ret
        dbg._play = _FakeRegPlay(_FakeZ80Mem(aux, code))
        dbg._tabs.setCurrentIndex(_tab_index(dbg, "Sound CPU"))
        dbg.refresh()

        # The listing starts AT the PC and runs forward. It does not try to show
        # what came before: on a variable-length instruction set, disassembling
        # backwards is guesswork, and a guessed listing is worse than a short one.
        listing = dbg._z80_text.toPlainText()
        assert listing.splitlines()[0].startswith("▶ 0003"), "the PC row is first, and marked"
        assert "ld a,(hl)" in listing and "ret" in listing
        regs = dbg._z80_regs.toPlainText()
        assert "BC 1234" in regs
        assert "BC' 9988" in regs, "the shadow bank is shown -- exx swaps it in one go"
        assert "running" in dbg._z80_why.text()
    finally:
        dbg.close()


def test_sound_cpu_tab_separates_a_halt_from_a_trap(app):
    """A halt is the driver sleeping between timer ticks. A trap is our core
    refusing an opcode — a hole in the emulator with an address on it. Only the
    second one gets the alarm, or the alarm stops meaning anything."""
    import ngpc_debug as dbg_mod

    dbg = dbg_mod.DebugWindow(None, cfg.make_settings())
    try:
        dbg._tabs.setCurrentIndex(_tab_index(dbg, "Sound CPU"))

        dbg._play = _FakeRegPlay(_FakeZ80Mem(_z80_aux(z80_halted=1)))
        dbg.refresh()
        assert "halted" in dbg._z80_why.text()
        assert not dbg._z80_why.styleSheet(), "a halt is normal, not an alarm"

        dbg._play = _FakeRegPlay(_FakeZ80Mem(
            _z80_aux(z80_trapped=1, z80_trap_pc=0x0000,
                     z80_trap_prefix=0xED, z80_trap_opcode=0xB0),
            bytes([0xED, 0xB0])))
        dbg.refresh()
        assert "TRAPPED" in dbg._z80_why.text()
        assert "ldir" in dbg._z80_why.text(), "it names the instruction to implement"
        assert dbg._z80_why.styleSheet(), "this one IS an alarm"
    finally:
        dbg.close()


def test_sound_cpu_tab_can_be_pointed_somewhere_other_than_pc(app):
    import ngpc_debug as dbg_mod

    dbg = dbg_mod.DebugWindow(None, cfg.make_settings())
    try:
        dbg._play = _FakeRegPlay(_FakeZ80Mem(_z80_aux(), bytes([0x00] * 0x20)))
        dbg._tabs.setCurrentIndex(_tab_index(dbg, "Sound CPU"))
        dbg._z80_goto.setText("0010")
        dbg.refresh()
        assert dbg._z80_text.toPlainText().lstrip().startswith("0010")
        assert "work RAM" in dbg._z80_map.text()

        # An address in the sound chip's window: write-only, so it reads 0xFF.
        dbg._z80_goto.setText("4000")
        dbg.refresh()
        assert "T6W28" in dbg._z80_map.text()
        assert "WRITE-ONLY" in dbg._z80_map.text()
    finally:
        dbg.close()


# --------------------------------------------------------------------------
# Coverage tab — what the cartridge actually executed.
# --------------------------------------------------------------------------
class _FakeCovMem(_FakeRegMem):
    def __init__(self, *, armed=False, executed=()):
        super().__init__({})
        from core import coverage_map as cm
        self._armed = armed
        raw = bytearray((cm.COVERAGE_SPAN + 7) // 8)
        for a in executed:
            i = a - cm.COVERAGE_LO
            raw[i >> 3] |= 1 << (i & 7)
        self._raw = bytes(raw)
        self._hits = len(set(executed))
        self.calls: list = []

    def set_coverage(self, on):
        self.calls.append(bool(on))
        self._armed = bool(on)

    def coverage_bitmap(self):
        return self._raw if self._armed else b""

    def coverage_hits(self):
        return self._hits if self._armed else 0


class _FakeCovPlay(_FakeRegPlay):
    def __init__(self, mem, rom_path=None):
        super().__init__(mem)
        self._rom_path = rom_path


def test_coverage_tab_does_not_claim_zero_when_it_is_simply_not_recording(app):
    """An empty bitmap means 'nobody armed it'. Showing that as 0% executed would
    be a measurement the tool never took."""
    import ngpc_debug as dbg_mod

    dbg = dbg_mod.DebugWindow(None, cfg.make_settings())
    try:
        dbg._play = _FakeCovPlay(_FakeCovMem(armed=False))
        dbg._tabs.setCurrentIndex(_tab_index(dbg, "Coverage"))
        dbg.refresh()
        assert "not recording" in dbg._cov_head.text()
        assert "not a claim" in dbg._cov_warn.text()
        assert dbg._cov_gaps.rowCount() == 0
    finally:
        dbg.close()


def test_coverage_tab_reports_reached_addresses_and_the_cold_runs(app, tmp_path):
    import ngpc_debug as dbg_mod
    from core import coverage_map as cm

    rom = tmp_path / "game.ngc"
    rom.write_bytes(b"\x00" * 4096)

    dbg = dbg_mod.DebugWindow(None, cfg.make_settings())
    try:
        hot = list(range(cm.COVERAGE_LO, cm.COVERAGE_LO + 512))
        dbg._play = _FakeCovPlay(_FakeCovMem(armed=True, executed=hot), rom)
        dbg._tabs.setCurrentIndex(_tab_index(dbg, "Coverage"))
        dbg.refresh()

        assert "512 instruction addresses reached" in dbg._cov_head.text()
        assert dbg._cov_arr is not None
        # Everything from 0x200200 to the end of the 4 KiB file never ran.
        assert dbg._cov_gaps.rowCount() >= 1
        biggest = dbg._cov_gap_rows[0]
        assert biggest.addr == cm.COVERAGE_LO + 512
        assert biggest.end < cm.COVERAGE_LO + 4096, \
            "a 4 KiB ROM must not report the rest of the 2 MiB window as dead code"
    finally:
        dbg.close()


def test_coverage_reset_goes_through_the_cores_own_clear(app, tmp_path):
    """Enabling is what allocates and clears in the core. Reset rides that path
    rather than adding a second one that could drift from it."""
    import ngpc_debug as dbg_mod

    rom = tmp_path / "game.ngc"
    rom.write_bytes(b"\x00" * 4096)
    dbg = dbg_mod.DebugWindow(None, cfg.make_settings())
    try:
        mem = _FakeCovMem(armed=True)
        dbg._play = _FakeCovPlay(mem, rom)
        dbg._tabs.setCurrentIndex(_tab_index(dbg, "Coverage"))
        dbg._cov_on.setChecked(True)
        mem.calls.clear()
        dbg._cov_reset()
        assert mem.calls == [False, True]
    finally:
        dbg.close()


def test_coverage_hover_maps_a_pixel_back_to_an_address_range(app, tmp_path):
    import ngpc_debug as dbg_mod
    from core import coverage_map as cm

    rom = tmp_path / "game.ngc"
    rom.write_bytes(b"\x00" * 4096)
    dbg = dbg_mod.DebugWindow(None, cfg.make_settings())
    try:
        dbg._play = _FakeCovPlay(
            _FakeCovMem(armed=True, executed=[cm.COVERAGE_LO]), rom)
        dbg._tabs.setCurrentIndex(_tab_index(dbg, "Coverage"))
        dbg.refresh()
        info = dbg._cov_info(0, 0)
        assert "0x200000" in info and "bytes/pixel" in info
        assert "1 instruction start(s) executed here" in info
        # Past the end of a 4 KiB cartridge the map says so instead of "unexecuted".
        assert "past the end" in dbg._cov_info(dbg.COVERAGE_W - 1, dbg.COVERAGE_H - 1)
    finally:
        dbg.close()


# --------------------------------------------------------------------------
# Profiler tab — where the frame goes, in cycles.
# --------------------------------------------------------------------------
class _FakeProfMem(_FakeRegMem):
    """A machine that retires a fixed script of instructions when asked to run."""

    def __init__(self, script):
        super().__init__({})
        self._script = list(script)
        self.runs = 0

    def run(self, count, record=True):
        from types import SimpleNamespace
        self.runs += 1
        take = self._script[:count]
        del self._script[:count]
        return SimpleNamespace(emitted=len(take)), take


def _rec(pc, cycles=4, reads=0, writes=0):
    from types import SimpleNamespace
    return SimpleNamespace(pc=pc, cycles=cycles, n_reads=reads, n_writes=writes)


class _FakeProfPlay(_FakeRegPlay):
    def __init__(self, mem):
        super().__init__(mem)
        self.paused = False
        self.blits = 0

    def _blit(self):
        self.blits += 1


def test_profiler_says_nothing_until_you_capture(app):
    """Capturing ADVANCES the machine, so it never happens behind your back on a
    refresh. Before that the tab claims nothing."""
    import ngpc_debug as dbg_mod

    dbg = dbg_mod.DebugWindow(None, cfg.make_settings())
    try:
        dbg._play = _FakeProfPlay(_FakeProfMem([]))
        dbg._tabs.setCurrentIndex(_tab_index(dbg, "Profiler"))
        dbg.refresh()
        assert "nothing captured" in dbg._prof_head.text()
        assert dbg._prof_table.rowCount() == 0
    finally:
        dbg.close()


def test_profiler_ranks_by_cycles_and_restores_the_pause_state(app):
    import ngpc_debug as dbg_mod

    dbg = dbg_mod.DebugWindow(None, cfg.make_settings())
    try:
        script = ([_rec(0x200000, cycles=2)] * 100      # many instructions, cheap
                  + [_rec(0x210000, cycles=40)] * 20)   # few instructions, costly
        mem = _FakeProfMem(script)
        play = _FakeProfPlay(mem)
        play.paused = False
        dbg._play = play
        dbg._tabs.setCurrentIndex(_tab_index(dbg, "Profiler"))
        dbg._prof_count.setValue(1000)
        dbg._prof_capture()

        assert dbg._prof_table.rowCount() == 2
        assert dbg._prof_table.item(0, 1).text() == "800", "the costly one leads"
        assert "120 instructions" in dbg._prof_head.text()
        assert not play.paused, "the pause state it found is the one it leaves"
        assert play.blits == 1, "the picture is put back after running blind"
    finally:
        dbg.close()


def test_profiler_names_the_regions_the_cycles_went_to(app):
    """'40% of this frame is inside the BIOS' is something no function list can
    say — and on this console it is often the answer."""
    import ngpc_debug as dbg_mod

    dbg = dbg_mod.DebugWindow(None, cfg.make_settings())
    try:
        script = [_rec(0x200000, cycles=10)] * 6 + [_rec(0xFF2000, cycles=10)] * 4
        dbg._play = _FakeProfPlay(_FakeProfMem(script))
        dbg._tabs.setCurrentIndex(_tab_index(dbg, "Profiler"))
        dbg._prof_count.setValue(1000)
        dbg._prof_capture()
        note = dbg._prof_regions.text()
        assert "cartridge 60.0%" in note and "BIOS 40.0%" in note
        assert "no .map loaded" in note, "and it says the rows are blocks, not functions"
    finally:
        dbg.close()


def test_profiler_capture_stops_when_the_core_stops_emitting(app):
    """A halted or trapped core emits nothing. The capture loop must end, not spin
    asking for the rest of a million instructions that will never arrive."""
    import ngpc_debug as dbg_mod

    dbg = dbg_mod.DebugWindow(None, cfg.make_settings())
    try:
        mem = _FakeProfMem([_rec(0x200000)] * 10)
        dbg._play = _FakeProfPlay(mem)
        dbg._tabs.setCurrentIndex(_tab_index(dbg, "Profiler"))
        dbg._prof_count.setValue(1_000_000)
        dbg._prof_capture()
        assert dbg._prof_report.total_instructions == 10
        assert mem.runs == 2, "one run that emitted, one that returned nothing"
    finally:
        dbg.close()


# --------------------------------------------------------------------------
# Movie — input recording and replay, through the ONE call site that feeds 0x00B0.
# --------------------------------------------------------------------------
class _FakeMoviePlay:
    """The slice of PlayPage the movie feature touches. The real methods are
    exercised through `_movie_byte`, which is the single point the frame loop
    calls -- there is no second clock for a replay to drift against."""

    def __init__(self, held=0):
        from ngpc_shell import PlayPage
        self.machine = object()
        self.held = held
        self.paused = False
        self.movie_rec = None
        self.movie_play = None
        self.movie_ended = None
        self.flashes: list = []
        self.applied: list = []
        self.blits = 0
        self._state = b"STATE-BLOB"
        # Bind the real implementations onto this stand-in.
        for name in ("_movie_byte", "movie_start_recording", "movie_stop_recording",
                     "movie_play_back", "movie_stop"):
            setattr(self, name, getattr(PlayPage, name).__get__(self))

    # what the real page provides to those methods
    def _joypad_byte(self):
        return self.held & 0x7F

    def _capture_state(self):
        return self._state

    def _apply_state(self, body, aux=True):
        self.applied.append(body)

    def _mirror_blocks(self):
        return False

    def _flash(self, msg, ms=1100):
        self.flashes.append(msg)

    def _blit(self):
        self.blits += 1


def test_recording_captures_the_byte_the_console_is_given():
    play = _FakeMoviePlay(held=0x11)
    assert play.movie_start_recording({"rom_name": "game.ngc"})
    assert play._movie_byte() == 0x11
    play.held = 0x02
    assert play._movie_byte() == 0x02
    movie = play.movie_stop_recording()
    assert list(movie.inputs) == [0x11, 0x02]
    assert movie.state == b"STATE-BLOB", "the snapshot is taken when recording starts"


def test_the_starting_state_is_captured_at_the_button_not_the_first_frame():
    """A recording that begins one frame late replays one frame out of step
    forever — and a one-frame drift reads as an emulation bug, not a broken tool."""
    play = _FakeMoviePlay()
    play._state = b"AT-THE-BUTTON"
    play.movie_start_recording({})
    play._state = b"ONE-FRAME-LATER"
    play._movie_byte()
    assert play.movie_stop_recording().state == b"AT-THE-BUTTON"


def test_replay_overrides_the_live_controller():
    from core import movie as mv

    play = _FakeMoviePlay(held=0x7F)               # every button mashed
    movie = mv.Movie({}, b"", bytearray([0x01, 0x00, 0x08]))
    assert play.movie_play_back(movie)
    assert [play._movie_byte() for _ in range(3)] == [0x01, 0x00, 0x08]


def test_replay_applies_the_recorded_state_first():
    from core import movie as mv

    play = _FakeMoviePlay()
    play.movie_play_back(mv.Movie({}, b"SNAP", bytearray([0])))
    assert play.applied == [b"SNAP"]


def test_when_a_replay_ends_the_controller_comes_back():
    """Past the end it must not hold the final byte: a replay that keeps pressing
    what was held on the last frame walks the game into a wall."""
    from core import movie as mv

    play = _FakeMoviePlay(held=0x04)
    play.movie_play_back(mv.Movie({}, b"", bytearray([0x01])))
    assert play._movie_byte() == 0x01
    assert play._movie_byte() == 0x04, "the live pad again"
    assert play.movie_play is None and play.movie_ended is not None


def test_recording_and_replaying_are_mutually_exclusive():
    """Recording a replay would just copy the file back out."""
    from core import movie as mv

    play = _FakeMoviePlay()
    play.movie_start_recording({})
    play.movie_play_back(mv.Movie({}, b"", bytearray([1])))
    assert play.movie_rec is None
    play.movie_start_recording({})
    assert play.movie_play is None


def test_a_mirror_match_refuses_both():
    """The other PC is simulating both consoles from the shared input stream.
    Feeding this one a recorded byte desynchronises the match."""
    from core import movie as mv

    play = _FakeMoviePlay()
    play._mirror_blocks = lambda: True
    assert not play.movie_start_recording({})
    assert not play.movie_play_back(mv.Movie({}, b"", bytearray([1])))
    assert play.movie_rec is None and play.movie_play is None


def test_movie_tab_reports_the_state_it_is_in(app):
    import ngpc_debug as dbg_mod
    from core import movie as mv

    dbg = dbg_mod.DebugWindow(None, cfg.make_settings())
    try:
        play = _FakeMoviePlay()
        dbg._play = play
        dbg._tabs.setCurrentIndex(_tab_index(dbg, "Movie"))
        dbg.refresh()
        assert dbg._mov_state.text() == "idle"

        play.movie_start_recording({})
        play._movie_byte(); play._movie_byte()
        dbg.refresh()
        assert "recording — 2 frames" in dbg._mov_state.text()
        assert not dbg._mov_play.isEnabled(), "no replaying while recording"

        play.movie_stop_recording()
        play.movie_play_back(mv.Movie({}, b"", bytearray([0] * 4)))
        play._movie_byte()
        dbg.refresh()
        assert "frame 1 of 4" in dbg._mov_state.text()
        assert not dbg._mov_rec.isEnabled(), "no recording while replaying"
    finally:
        dbg.close()


# --------------------------------------------------------------------------
# Console tab — a Python prompt with the machine in scope.
# --------------------------------------------------------------------------
class _FakeCartMem:
    """A sparse bus that reaches the cartridge window — `_FakeRegMem` is only
    64 KiB, and the console's helpers default to 0x200000."""

    def __init__(self, values=None):
        self._v = dict(values or {})

    def read(self, addr, n=1):
        return bytes(self._v.get(addr + i, 0) & 0xFF for i in range(n))

    def write(self, addr, data):
        for i, b in enumerate(bytes(data)):
            self._v[addr + i] = b

    def cpu(self):
        return _FakeCpu()
def test_console_tab_runs_a_line_and_shows_the_result(app):
    import ngpc_debug as dbg_mod

    dbg = dbg_mod.DebugWindow(None, cfg.make_settings())
    try:
        dbg._play = _FakeRegPlay(_FakeCartMem({0x200000: 0x42}))
        dbg._tabs.setCurrentIndex(_tab_index(dbg, "Console"))
        dbg.refresh()
        dbg._con_in.setPlainText("hex(u8(0x200000))")
        dbg._con_submit()
        out = dbg._con_out.toPlainText()
        assert ">>> hex(u8(0x200000))" in out
        assert "'0x42'" in out
        assert dbg._con_in.toPlainText() == "", "the input clears after it runs"
    finally:
        dbg.close()


def test_console_tab_shows_an_error_without_taking_the_window_with_it(app):
    """This runs inside a Qt slot: an exception reaching PyQt calls qFatal and the
    process dies with no message. The test surviving IS the assertion."""
    import ngpc_debug as dbg_mod

    dbg = dbg_mod.DebugWindow(None, cfg.make_settings())
    try:
        dbg._play = _FakeRegPlay(_FakeRegMem({}))
        dbg._tabs.setCurrentIndex(_tab_index(dbg, "Console"))
        dbg._con_in.setPlainText("1 / 0")
        dbg._con_submit()
        assert "ZeroDivisionError" in dbg._con_out.toPlainText()
        dbg.refresh()
    finally:
        dbg.close()


def test_console_tab_waits_for_the_rest_of_a_block(app):
    import ngpc_debug as dbg_mod

    dbg = dbg_mod.DebugWindow(None, cfg.make_settings())
    try:
        dbg._play = _FakeRegPlay(_FakeRegMem({}))
        dbg._tabs.setCurrentIndex(_tab_index(dbg, "Console"))
        before = dbg._con_out.toPlainText()
        dbg._con_in.setPlainText("for i in range(2):")
        dbg._con_submit()
        assert dbg._con_out.toPlainText() == before, "nothing ran, nothing echoed"
        assert dbg._con_in.toPlainText() == "for i in range(2):", "the text is kept"

        dbg._con_in.setPlainText("for i in range(2):\n    print('tick', i)\n")
        dbg._con_submit()
        assert "tick 1" in dbg._con_out.toPlainText()
    finally:
        dbg.close()


def test_console_namespace_follows_the_running_game_but_keeps_your_helpers(app):
    import ngpc_debug as dbg_mod

    dbg = dbg_mod.DebugWindow(None, cfg.make_settings())
    try:
        dbg._play = _FakeRegPlay(_FakeCartMem({0x200000: 1}))
        dbg._tabs.setCurrentIndex(_tab_index(dbg, "Console"))
        dbg.refresh()
        dbg._con_in.setPlainText("def mine(): return 7")
        dbg._con_submit()

        dbg._play = _FakeRegPlay(_FakeCartMem({0x200000: 2}))    # a different game
        dbg.refresh()
        dbg._con_in.setPlainText("(mine(), u8(0x200000))")
        dbg._con_submit()
        assert "(7, 2)" in dbg._con_out.toPlainText(), \
            "the helper survived and the machine is the new one"
    finally:
        dbg.close()


def test_console_refresh_never_executes_anything_on_a_timer(app):
    """A console that re-ran something every refresh would be a machine for
    surprises. Only Enter executes."""
    import ngpc_debug as dbg_mod

    dbg = dbg_mod.DebugWindow(None, cfg.make_settings())
    try:
        dbg._play = _FakeRegPlay(_FakeRegMem({}))
        dbg._tabs.setCurrentIndex(_tab_index(dbg, "Console"))
        dbg._con_in.setPlainText("print('should not run')")
        for _ in range(5):
            dbg.refresh()
        assert "should not run" not in dbg._con_out.toPlainText()
    finally:
        dbg.close()


# --------------------------------------------------------------------------
# Two-row tabs — 27 panels do not fit on one row.
# --------------------------------------------------------------------------
def test_the_debug_window_groups_its_panels_on_two_rows(app):
    """A single row stopped working around twenty panels: Qt shrinks the labels
    past reading or hides half of them behind scroll arrows, and a tool whose tabs
    you cannot see is a tool you stop opening."""
    import ngpc_debug as dbg_mod

    dbg = dbg_mod.DebugWindow(None, cfg.make_settings())
    try:
        bar = dbg._tabs._cat_bar
        cats = [bar.tabText(i) for i in range(bar.count())]
        assert cats == ["CPU", "Memory", "Video", "Audio", "Analysis", "ROM", "Link"]
        # Every panel belongs to exactly one category, and none is orphaned.
        assert len(dbg._tabs._categories) == dbg._tabs.count()
        assert set(dbg._tabs._categories) == set(cats)
        # Neither row is longer than it needs to be for a glance.
        for cat in cats:
            assert sum(1 for c in dbg._tabs._categories if c == cat) <= 6
    finally:
        dbg.close()


def test_selecting_a_panel_by_name_pulls_its_category_into_view(app):
    """Nothing outside the tab widget knows panels are grouped — `Ctrl+G` jumps to
    Disassembly from wherever you are, and the double-click handlers in Coverage
    and Profiler do the same. That only works if a flat index still resolves."""
    import ngpc_debug as dbg_mod

    dbg = dbg_mod.DebugWindow(None, cfg.make_settings())
    try:
        names = [dbg._tabs.tabText(i) for i in range(dbg._tabs.count())]
        dbg._tabs.setCurrentIndex(names.index("Link"))
        assert dbg._tabs._cat_bar.tabText(dbg._tabs._cat_bar.currentIndex()) == "Link"

        dbg._tabs.setCurrentIndex(names.index("Disassembly"))
        assert dbg._tabs.currentIndex() == names.index("Disassembly")
        assert dbg._tabs._cat_bar.tabText(dbg._tabs._cat_bar.currentIndex()) == "CPU"
        assert dbg._tabs._stack.currentIndex() == names.index("Disassembly")
    finally:
        dbg.close()


def test_picking_a_category_opens_its_first_panel_and_reports_it(app):
    import ngpc_debug as dbg_mod

    dbg = dbg_mod.DebugWindow(None, cfg.make_settings())
    try:
        seen: list = []
        dbg._tabs.currentChanged.connect(seen.append)
        cats = [dbg._tabs._cat_bar.tabText(i)
                for i in range(dbg._tabs._cat_bar.count())]
        dbg._tabs._cat_bar.setCurrentIndex(cats.index("Video"))
        assert dbg._tabs.tabText(dbg._tabs.currentIndex()) == "Palette"
        assert seen and seen[-1] == dbg._tabs.currentIndex()
    finally:
        dbg.close()


def test_a_category_with_one_panel_does_not_show_a_second_row(app):
    """The Link category would otherwise render a row saying its own name back."""
    import ngpc_debug as dbg_mod

    dbg = dbg_mod.DebugWindow(None, cfg.make_settings())
    try:
        names = [dbg._tabs.tabText(i) for i in range(dbg._tabs.count())]
        dbg._tabs.setCurrentIndex(names.index("Link"))
        assert not dbg._tabs._tab_bar.isVisibleTo(dbg._tabs)
        dbg._tabs.setCurrentIndex(names.index("Tilemap"))
        assert dbg._tabs._tab_bar.isVisibleTo(dbg._tabs)
    finally:
        dbg.close()


def test_every_panel_is_reachable_and_refreshes(app):
    import ngpc_debug as dbg_mod

    dbg = dbg_mod.DebugWindow(None, cfg.make_settings())
    try:
        assert dbg._tabs.count() == len(dbg._tab_refresh) == 28
        for i in range(dbg._tabs.count()):
            dbg._tabs.setCurrentIndex(i)
            assert dbg._tabs.currentIndex() == i, dbg._tabs.tabText(i)
            dbg.refresh()
    finally:
        dbg.close()


def test_no_panel_was_lost_when_the_tabs_were_regrouped(app):
    """The 19 panels the debugger had before the two-row rework, named one by one.

    Regrouping tabs is exactly the kind of change that quietly drops one: the list
    still looks full, and the missing panel is only noticed by the person who
    needed it. Each name is spelled out here so removing one is a decision someone
    has to make on purpose."""
    import ngpc_debug as dbg_mod

    before = ["CPU", "Disassembly", "Call Stack", "Events", "Memory", "Watch",
              "Breakpoints", "RAM Search", "Audio", "Palette", "Tiles", "Sprites",
              "Layers", "Load", "Text", "Crack", "Pointers", "Compare", "Link"]
    added = ["HW Regs", "Tilemap", "Sound CPU", "Coverage", "Profiler", "Movie",
             "Console", "Cheats", "Source"]

    dbg = dbg_mod.DebugWindow(None, cfg.make_settings())
    try:
        names = [dbg._tabs.tabText(i) for i in range(dbg._tabs.count())]
        assert not set(before) - set(names), "a panel that existed before is gone"
        assert set(names) == set(before) | set(added)
        assert len(names) == len(set(names)), "a panel is registered twice"
        # A panel with no widget, or two sharing a refresher, is a panel that shows
        # someone else's contents.
        assert len(set(dbg._tab_refresh)) == len(names)
        assert dbg._tabs._stack.count() == len(names)
        assert all(dbg._tabs._stack.widget(i) is not None for i in range(len(names)))
    finally:
        dbg.close()


# --------------------------------------------------------------------------
# Cheats tab — named groups of addresses held at a value.
# --------------------------------------------------------------------------
class _FakeCheatPlay(_FakeRegPlay):
    def __init__(self, mem):
        super().__init__(mem)
        from core.cheats import CheatSet
        self.cheats = CheatSet()
        self.saved = 0

    def save_cheats(self):
        self.saved += 1


def test_cheats_tab_loads_pasted_codes_and_lists_them(app):
    import ngpc_debug as dbg_mod

    dbg = dbg_mod.DebugWindow(None, cfg.make_settings())
    try:
        play = _FakeCheatPlay(_FakeRegMem({}))
        dbg._play = play
        dbg._tabs.setCurrentIndex(_tab_index(dbg, "Cheats"))
        dbg._ch_text.setPlainText("# Infinite health\n4812:1 = 63\n481A:2 = 03E7\n")
        dbg._ch_apply_text()

        assert dbg._ch_table.rowCount() == 1
        assert dbg._ch_table.item(0, 1).text() == "Infinite health"
        assert "004812:1 = 63" in dbg._ch_table.item(0, 2).text()
        assert play.saved >= 1, "cheats are kept per ROM, like the watches"
    finally:
        dbg.close()


def test_a_bad_line_is_reported_rather_than_silently_dropped(app):
    import ngpc_debug as dbg_mod

    dbg = dbg_mod.DebugWindow(None, cfg.make_settings())
    try:
        dbg._play = _FakeCheatPlay(_FakeRegMem({}))
        dbg._tabs.setCurrentIndex(_tab_index(dbg, "Cheats"))
        dbg._ch_text.setPlainText("# x\n4000=01\ngarbage\n")
        dbg._ch_apply_text()
        assert "line 3" in dbg._ch_warn.text()
        assert dbg._ch_table.rowCount() == 1, "the readable line still loaded"
    finally:
        dbg.close()


def test_re_applying_the_text_does_not_switch_your_cheats_off(app):
    """Re-reading your own list should not silently turn everything off."""
    import ngpc_debug as dbg_mod

    dbg = dbg_mod.DebugWindow(None, cfg.make_settings())
    try:
        play = _FakeCheatPlay(_FakeRegMem({}))
        dbg._play = play
        dbg._tabs.setCurrentIndex(_tab_index(dbg, "Cheats"))
        dbg._ch_text.setPlainText("# hp\n4812:1 = 63\n")
        dbg._ch_apply_text()
        play.cheats.cheats[0].enabled = True
        dbg._ch_apply_text()
        assert play.cheats.cheats[0].enabled
    finally:
        dbg.close()


def test_ticking_a_row_arms_the_cheat_in_the_running_game(app):
    import ngpc_debug as dbg_mod
    from PyQt6.QtCore import Qt as _Qt

    dbg = dbg_mod.DebugWindow(None, cfg.make_settings())
    try:
        play = _FakeCheatPlay(_FakeRegMem({}))
        dbg._play = play
        dbg._tabs.setCurrentIndex(_tab_index(dbg, "Cheats"))
        dbg._ch_text.setPlainText("# hp\n4812:1 = 63\n")
        dbg._ch_apply_text()
        assert not play.cheats.enabled()

        dbg._ch_table.item(0, 0).setCheckState(_Qt.CheckState.Checked)
        assert [c.name for c in play.cheats.enabled()] == ["hp"]
    finally:
        dbg.close()


def test_a_cartridge_address_is_warned_about_when_selected(app):
    """The cart is FLASH: a write there does not change memory, it goes to the
    chip's command latch. Warned, never refused."""
    import ngpc_debug as dbg_mod

    dbg = dbg_mod.DebugWindow(None, cfg.make_settings())
    try:
        dbg._play = _FakeCheatPlay(_FakeRegMem({}))
        dbg._tabs.setCurrentIndex(_tab_index(dbg, "Cheats"))
        dbg._ch_text.setPlainText("# into the cart\n201234:1 = FF\n")
        dbg._ch_apply_text()
        dbg._ch_table.selectRow(0)
        assert "FLASH" in dbg._ch_warn.text()
        assert dbg._ch_table.rowCount() == 1, "still loaded, just flagged"
    finally:
        dbg.close()


def test_the_frame_loop_writes_cheats_where_it_writes_locked_watches():
    """One mechanism, one point in the frame. Two things that both hold a value
    would race, and which won would depend on where in the frame each ran."""
    import inspect

    from ngpc_shell import PlayPage

    src = inspect.getsource(PlayPage._tick)
    lock_at = src.index("w.lock_bytes()")
    cheat_at = src.index("self.cheats.apply(self.machine)")
    assert 0 < cheat_at - lock_at < 400, \
        "the cheat write must sit with the lock write, not somewhere else in the frame"


# --------------------------------------------------------------------------
# Rewind strip — in the main window, while you are rewinding, and nowhere else.
# --------------------------------------------------------------------------
def test_the_rewind_strip_reports_time_left_not_a_frame_count(app):
    """`⏪ 137` is a unit nobody thinks in, printed over the picture you are
    looking at, and it never said the thing that matters while you hold the key:
    how much history is LEFT."""
    import ngpc_shell as shell_mod

    bar = shell_mod._RewindBar()
    try:
        bar.set_range(total=300, pos=299)
        assert bar.frames_back == 0, "at the present"
        bar.set_range(total=300, pos=200)
        assert bar.frames_back == 99
        bar.resize(200, 26)
        bar.grab()                     # it paints without a live theme or a game
    finally:
        bar.deleteLater()


def test_the_strip_clamps_instead_of_reporting_a_position_that_does_not_exist(app):
    import ngpc_shell as shell_mod

    bar = shell_mod._RewindBar()
    try:
        bar.set_range(total=10, pos=999)
        assert bar.frames_back == 0
        bar.set_range(total=10, pos=-5)
        assert bar.frames_back == 9
        bar.set_range(total=0, pos=0)
        assert bar.frames_back == 0, "an empty ring has nothing behind it"
    finally:
        bar.deleteLater()


def test_dragging_the_strip_asks_for_a_position_in_the_ring(app):
    """Dragging is why a strip beats a counter: you go back to A MOMENT instead of
    feeling your way there one key-press at a time."""
    import ngpc_shell as shell_mod

    bar = shell_mod._RewindBar()
    try:
        bar.resize(600, 26)
        bar.set_range(total=101, pos=100)
        # ⚠️ Against the TRACK, not the widget: the track stops before the label,
        # so mapping a click against the full width lands you a few seconds off --
        # and consistently in one direction, which is the kind of wrongness you
        # blame on yourself rather than on the tool.
        track = bar._track_width()
        assert 0 < track < bar.width()
        # The track starts after the plate's left padding -- the strip floats over
        # the picture now, so it draws its own rounded backing and the clickable
        # area is inset from the widget's edge.
        left = bar.INSET // 2
        assert bar._index_at(left) == 0
        assert bar._index_at(left + track // 2) == 50
        assert bar._index_at(left + track) == 100
        assert bar._index_at(bar.width() * 2) == 100, "past the end is the present"
    finally:
        bar.deleteLater()


class _FakeRewindPage(QWidget):
    """The slice of PlayPage the strip touches, with the real methods bound on.

    A QWidget, because that is what it stands in for: the strip is placed against
    the page's own geometry, and a stand-in without a width cannot be placed."""

    def __init__(self, frames=10):
        from collections import deque

        import ngpc_shell as shell_mod

        super().__init__()
        self.resize(640, 480)
        # Class-level settings the borrowed methods read off `self`.
        self.LINGER_MS = shell_mod.PlayPage.LINGER_MS
        self.machine = object()
        self._rewind = deque(bytes([i]) for i in range(frames))
        self._rw_pos = None
        self._rewinding = False
        self.paused = False
        self.applied: list = []
        self.rewind_bar = shell_mod._RewindBar(self)
        self._rw_bar_timer = None
        self._rw_resume_after_drag = False
        self.overlay = QLabel()
        self.lcd = QWidget(self); self.lcd.resize(600, 400)
        for name in ("_show_rewind_bar", "_scrub_to", "_place_rewind_bar",
                     "_hold_rewind_bar", "_linger_rewind_bar", "_hide_rewind_bar",
                     "_begin_scrub", "_end_scrub"):
            setattr(self, name, getattr(shell_mod.PlayPage, name).__get__(self))
        # ...and only THEN wire the strip, the way the page does -- a signal
        # connected to a method that is not bound yet is an AttributeError at
        # construction, not at the click.
        self.rewind_bar.grabbed.connect(self._begin_scrub)
        self.rewind_bar.scrubbed.connect(self._scrub_to)
        self.rewind_bar.dropped.connect(self._end_scrub)

    def _apply_state(self, body, aux=True):
        self.applied.append(body)

    def _drain_audio_silently(self):
        pass

    def _blit(self):
        pass


def test_the_strip_appears_only_when_there_is_history(app):
    page = _FakeRewindPage(frames=1)
    page._show_rewind_bar()
    assert not page.rewind_bar.isVisible(), "one frame is not a timeline"
    page = _FakeRewindPage(frames=10)
    page._show_rewind_bar()
    assert page.rewind_bar.isVisibleTo(page), "with history, it shows"


def test_the_strip_follows_the_held_key_and_the_step_cursor(app):
    """Two different truths: holding rewind POPS the ring so the present is its
    last frame, while stepping leaves the ring intact and moves a cursor. The
    strip has to ask which applies rather than assume one."""
    page = _FakeRewindPage(frames=10)
    page._show_rewind_bar()
    assert page.rewind_bar.frames_back == 0, "held: the tip is the present"
    page._rw_pos = 3
    page._show_rewind_bar()
    assert page.rewind_bar.frames_back == 6, "stepping: the cursor is the present"


def test_scrubbing_applies_that_frame_and_pauses(app):
    page = _FakeRewindPage(frames=10)
    page._rewinding = True
    page._scrub_to(4)
    assert page.applied == [bytes([4])]
    assert page._rw_pos == 4
    assert page.paused, "a deliberate look at the past pauses, like stepping back"
    assert not page._rewinding, "and it is no longer a held rewind"


def test_scrubbing_past_the_ends_lands_inside_the_ring(app):
    page = _FakeRewindPage(frames=10)
    page._scrub_to(-3)
    assert page.applied[-1] == bytes([0])
    page._scrub_to(99)
    assert page.applied[-1] == bytes([9])


def test_scrubbing_an_empty_ring_does_nothing_rather_than_raising(app):
    page = _FakeRewindPage(frames=1)
    page._scrub_to(5)
    assert page.applied == []


# --------------------------------------------------------------------------
# Rewind, second pass: it was slow to go far, and the scrubber was unreachable.
# --------------------------------------------------------------------------
def test_a_held_rewind_speeds_up_the_longer_it_is_held():
    """At one frame per step this ran backwards in REAL TIME: reaching the far end
    of a 30-second buffer took thirty seconds of holding a key, which is not
    rewinding, it is waiting."""
    import ngpc_shell as shell_mod

    assert shell_mod.rewind_speed(0) == 1, "a short tap stays frame-accurate"
    assert shell_mod.rewind_speed(29) == 1
    assert shell_mod.rewind_speed(30) == 2
    assert shell_mod.rewind_speed(60) == 4
    assert shell_mod.rewind_speed(10_000) == shell_mod.REWIND_MAX_SPEED


def test_the_ramp_crosses_a_thirty_second_buffer_in_a_few_seconds():
    """The point of the ramp, stated as the number it has to hit. A step is about
    1/60 s, so this is 'how long must the key be held to reach the far end'."""
    import ngpc_shell as shell_mod

    frames, steps = 30 * 60, 0
    while frames > 0 and steps < 10_000:
        frames -= shell_mod.rewind_speed(steps)
        steps += 1
    assert steps / 60.0 < 5.0, f"{steps / 60.0:.1f} s to cross the buffer"


def test_the_first_half_second_of_holding_is_still_frame_accurate():
    """A tap must land on the frame you meant. An immediate ramp would overshoot
    every short correction, which is what rewind is mostly used for."""
    import ngpc_shell as shell_mod

    assert all(shell_mod.rewind_speed(n) == 1 for n in range(30))


def test_the_strip_says_when_it_is_running_fast(app):
    """A picture that suddenly runs eight times faster with nothing on screen to
    say so reads as a glitch."""
    import ngpc_shell as shell_mod

    bar = shell_mod._RewindBar()
    try:
        bar.set_range(600, 300, speed=1)
        assert "×" not in bar._label()
        bar.set_range(600, 300, speed=8)
        assert "×8" in bar._label()
    finally:
        bar.deleteLater()


def test_the_strip_is_reachable_while_paused_not_only_while_rewinding():
    """⛔ THE BUG IN THE FIRST VERSION. The strip appeared only while rewinding —
    so to see it you had to hold the key, and while holding the key you cannot
    drag it. A scrubber you can never grab is a decoration."""
    import inspect

    from ngpc_shell import PlayPage

    src = inspect.getsource(PlayPage._tick)
    paused = src.index("if self.paused:")
    shown = src.index("self._show_rewind_bar()", paused)
    # The show must be INSIDE the paused branch, before it returns.
    assert 0 < shown - paused < 600
    assert "return" in src[shown:shown + 200]


# --------------------------------------------------------------------------
# Lifecycle: what the debug window must let go of when the game changes.
# --------------------------------------------------------------------------
def test_swapping_games_re_arms_coverage_so_the_tick_box_never_lies(app):
    """A new game is a NEW CORE, and a new core starts with coverage off. The tick
    box survives the swap, so without re-arming it claims to be recording over a
    core that is not."""
    import ngpc_debug as dbg_mod

    dbg = dbg_mod.DebugWindow(None, cfg.make_settings())
    try:
        first = _FakeCovMem(armed=False)
        dbg.attach(_FakeCovPlay(first))
        dbg._cov_on.setChecked(True)
        assert first.calls[-1] is True

        second = _FakeCovMem(armed=False)
        dbg.attach(_FakeCovPlay(second))
        assert second.calls and second.calls[-1] is True, \
            "the new core was never told, so the box was lying"
    finally:
        dbg.close()


def test_the_console_lets_go_of_a_machine_that_is_being_torn_down(app):
    """Its namespace holds `m`. A namespace nobody has looked at in ten minutes was
    keeping a dead core — and the DLL behind it — alive until its tab happened to
    refresh."""
    import ngpc_debug as dbg_mod

    dbg = dbg_mod.DebugWindow(None, cfg.make_settings())
    try:
        mem = _FakeCartMem({0x200000: 1})
        dbg.attach(_FakeRegPlay(mem))
        dbg._tabs.setCurrentIndex(_tab_index(dbg, "Console"))
        dbg.refresh()
        assert dbg._con.namespace["m"] is mem

        dbg.attach(None)
        assert dbg._con.namespace["m"] is None, "released at detach, not eventually"
    finally:
        dbg.close()


def test_closing_the_window_leaves_no_sampler_running_in_the_game_loop(app):
    """A debug tool that keeps sampling after its window is gone is a permanent
    tax on a game nobody is debugging any more."""
    import ngpc_debug as dbg_mod

    dbg = dbg_mod.DebugWindow(None, cfg.make_settings())
    try:
        play = _FakeRegPlay(_FakeCartMem({}))
        play.frame_hooks = []
        play.access_probe = None
        play.apply_debug = lambda: None
        dbg.attach(play)
        dbg._rs_track.setChecked(True)
        dbg.hideEvent(None)
        assert play.frame_hooks == []
    finally:
        dbg.close()


def test_the_strip_does_not_repaint_when_nothing_moved(app):
    """The paused loop asks it to update on every tick — about 250 times a second
    for a strip that has not moved."""
    import ngpc_shell as shell_mod

    bar = shell_mod._RewindBar()
    try:
        painted: list = []
        bar.update = lambda *a: painted.append(1)      # count repaint requests
        bar.set_range(300, 200)
        assert len(painted) == 1
        for _ in range(50):
            bar.set_range(300, 200)
        assert len(painted) == 1, "same numbers, no repaint"
        bar.set_range(300, 199)
        assert len(painted) == 2, "it moved, so it repaints"
    finally:
        bar.deleteLater()


def test_holding_rewind_at_the_end_of_the_buffer_stops_doing_work(app):
    """With one frame left there is nothing to pop, and re-applying that same state
    (plus draining audio, plus repainting) sixty times a second is pure waste.

    This drives the REAL `_tick`, not a copy of its body: a test that re-implements
    the loop it is checking passes forever after the loop changes."""
    from collections import deque

    import ngpc_shell as shell_mod

    class _Page(QWidget):
        def __init__(self, frames):
            super().__init__()
            self.resize(640, 480)
            self.machine = object()
            self._rewind = deque(bytes([i]) for i in range(frames))
            self._rw_pos = None
            self._rewinding = True
            self._rw_accum = 3          # the next tick crosses the 4-tick gate
            self._rw_hold = 0
            self.paused = False
            self.applied: list = []
            self.drains = 0
            self.blits = 0
            self.rewind_bar = shell_mod._RewindBar()
            self._rw_bar_timer = None
            self.lcd = QWidget(self); self.lcd.resize(600, 400)
            for name in ("_tick", "_show_rewind_bar", "_place_rewind_bar"):
                setattr(self, name, getattr(shell_mod.PlayPage, name).__get__(self))

        def _poll_pad(self): pass
        def _apply_state(self, body, aux=True): self.applied.append(body)
        def _drain_audio_silently(self): self.drains += 1
        def _blit(self): self.blits += 1

    # With history, one tick rewinds one frame.
    page = _Page(frames=5)
    page._tick()
    assert page.applied == [bytes([3])] and page.drains == 1

    # At the end of the history, the same tick does nothing at all.
    spent = _Page(frames=1)
    for _ in range(8):
        spent._rw_accum = 3
        spent._tick()
    assert spent.applied == [] and spent.drains == 0


def test_the_strip_floats_over_the_picture_instead_of_shrinking_it(app):
    """⛔ THE SECOND BUG FROM REAL USE. In the layout it TOOK SPACE, so the image
    shrank the moment you touched rewind and grew back when you let go. A picture
    that jumps about is worse than the information is worth."""
    import ngpc_shell as shell_mod

    page = _FakeRewindPage(frames=100)
    try:
        page.show()
        app.processEvents()
        before = (page.lcd.width(), page.lcd.height())
        page._show_rewind_bar()
        app.processEvents()
        assert page.rewind_bar.isVisible()
        assert (page.lcd.width(), page.lcd.height()) == before, \
            "showing the strip must not resize the canvas"
        # ...and it really is on top of it, not beside it.
        assert page.rewind_bar.parent() is page
        assert page.rewind_bar.y() + page.rewind_bar.height() <= page.height()
    finally:
        page.close()


def test_the_strip_stays_up_after_the_key_is_released(app):
    """⛔ THE FIRST BUG FROM REAL USE. It vanished the instant the key came up — so
    the only moment it existed was the moment your hand was busy holding a key, and
    the draggable cursor could never be grabbed. Two ways to use rewind means each
    has to be reachable from the other."""
    import ngpc_shell as shell_mod

    page = _FakeRewindPage(frames=100)
    try:
        page.show()
        app.processEvents()
        page._rewinding = True
        page._show_rewind_bar()
        assert page.rewind_bar.isVisibleTo(page)

        page._linger_rewind_bar()          # what releasing the key does
        page._rewinding = False
        assert page.rewind_bar.isVisibleTo(page), "still grabbable"
        assert page._rw_bar_timer is not None and page._rw_bar_timer.isActive()
        assert page._rw_bar_timer.parent() is page, (
            "a timer that belongs to nobody outlives the page and fires into a "
            "torn-down widget -- which kills the process with no traceback")

        page._hide_rewind_bar()            # what the timer does when it expires
        assert not page.rewind_bar.isVisibleTo(page)
    finally:
        page.close()


def test_grabbing_the_strip_stops_it_disappearing_under_the_cursor(app):
    import ngpc_shell as shell_mod

    page = _FakeRewindPage(frames=100)
    try:
        page.show()
        app.processEvents()
        page._show_rewind_bar()
        page._linger_rewind_bar()
        assert page._rw_bar_timer.isActive()
        page._hold_rewind_bar()
        assert not page._rw_bar_timer.isActive()
    finally:
        page.close()


def test_the_linger_gives_way_to_the_strip_being_in_use(app):
    """The timer can fire while you are already scrubbing again. It must lose."""
    page = _FakeRewindPage(frames=100)
    try:
        page._show_rewind_bar()
        page._rw_pos = 5
        page._hide_rewind_bar()
        assert page.rewind_bar.isVisibleTo(page), "still scrubbing: the timer lost"
        page._rw_pos = None
        page._rewinding = True
        page._hide_rewind_bar()
        assert page.rewind_bar.isVisibleTo(page), "still rewinding: same"
    finally:
        page.close()


def _drag(app, bar, *positions):
    """Press, move, release on the strip -- the gesture, through real Qt events."""
    from PyQt6.QtCore import QEvent, QPointF, Qt as _Qt
    from PyQt6.QtGui import QMouseEvent

    types = ([QEvent.Type.MouseButtonPress]
             + [QEvent.Type.MouseMove] * (len(positions) - 2)
             + [QEvent.Type.MouseButtonRelease])
    for typ, x in zip(types, positions):
        app.sendEvent(bar, QMouseEvent(typ, QPointF(x, bar.height() / 2),
                                       _Qt.MouseButton.LeftButton,
                                       _Qt.MouseButton.LeftButton,
                                       _Qt.KeyboardModifier.NoModifier))
        app.processEvents()


def test_letting_go_of_the_strip_carries_on_playing(app):
    """⛔ THE TRAP THIS CLOSES. Dragging has to pause — otherwise the game runs out
    from under the position you are choosing — but leaving it paused STRANDS you:
    this toolbar has no play button. `⏭` steps one frame (and pauses again) and `⏩`
    does nothing at all while paused, because the loop returns before it."""
    page = _FakeRewindPage(frames=100)
    try:
        page.show()
        app.processEvents()
        page._show_rewind_bar()
        page.paused = False

        bar = page.rewind_bar
        _drag(app, bar, bar.width() * 0.6, bar.width() * 0.4, bar.width() * 0.3)
        assert page._rw_pos is not None, "it scrubbed"
        assert not page.paused, "and it carries on playing when you let go"
    finally:
        page.close()


def test_a_game_that_was_already_paused_stays_paused(app):
    """Then pausing is the thing you asked for, and a drag must not undo it."""
    page = _FakeRewindPage(frames=100)
    try:
        page.show()
        app.processEvents()
        page._show_rewind_bar()
        page.paused = True

        bar = page.rewind_bar
        _drag(app, bar, bar.width() * 0.6, bar.width() * 0.3)
        assert page.paused
    finally:
        page.close()


def test_the_was_it_running_question_is_asked_before_scrubbing_pauses(app):
    """⚠️ The bug inside the fix. Asking it from `_scrub_to` reads the pause that
    scrubbing itself just set, so the answer is always 'it was paused' and the game
    never resumes — the exact trap the change exists to close, reintroduced."""
    page = _FakeRewindPage(frames=100)
    try:
        page.paused = False
        page._begin_scrub()
        assert page._rw_resume_after_drag is True
        page._scrub_to(10)               # this pauses...
        assert page.paused
        assert page._rw_resume_after_drag is True, \
            "...and must not have overwritten the answer"
        page._end_scrub()
        assert not page.paused
    finally:
        page.close()
