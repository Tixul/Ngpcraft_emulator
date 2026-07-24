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
from PyQt6.QtWidgets import QApplication  # noqa: E402

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
    s.clear()
    yield
    s.clear()


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
        dbg._tabs.setCurrentIndex(0); dbg.refresh()
        dbg._save_text(dbg._cpu_text.toPlainText(), "cpu_state.txt")
        dbg._tabs.setCurrentIndex(3); dbg.refresh(); dbg._save_png(dbg._pal_arr, "palette.png")
        dbg._tabs.setCurrentIndex(4); dbg.refresh(); dbg._save_png(dbg._tiles_arr, "tiles.png")
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
    shown, which would make everything report not-visible regardless."""
    w = shell.Shell()
    try:
        state = {"fs": False}
        monkeypatch.setattr(w, "isFullScreen", lambda: state["fs"])
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


def test_a_bios_added_later_re_renders_the_covers(app, tmp_path, monkeypatch):
    """Set the BIOS in Settings and come back: the covers that could not be
    rendered without one are rendered now, without a restart."""
    monkeypatch.setattr(shell, "THUMB_DIR", tmp_path / "thumbnails")
    monkeypatch.setattr(shell, "COVER_DIR", tmp_path / "covers")
    monkeypatch.setattr(shell, "DEFAULT_BIOS", tmp_path / "no-such-bios.bin")
    roms = tmp_path / "roms"
    roms.mkdir()
    (roms / "Game.ngc").write_bytes(b"\xff" * 64)

    settings = cfg.make_settings()
    settings.setValue("paths/rom_folder", str(roms))
    started: list[object] = []
    monkeypatch.setattr(shell.LibraryPage, "_start_worker",
                        lambda self, r: started.append(self._bios()))
    # The full re-render (as opposed to the cheap "resume what is missing" pass the
    # library already runs whenever it is shown) is what must happen exactly once.
    reloads: list[int] = []
    real_reload = shell.LibraryPage.reload
    monkeypatch.setattr(shell.LibraryPage, "reload",
                        lambda self: (reloads.append(1), real_reload(self))[1])

    page = shell.LibraryPage(settings, shell.lib.Library(tmp_path / "library.json"))
    try:
        assert started == [None], "the first pass ran with no BIOS"
        assert page._bios_hint.isVisible() or not page.isVisible(), "the why is on screen"

        bios = tmp_path / "bios.bin"
        bios.write_bytes(b"\x00" * 64)
        settings.setValue("paths/bios", str(bios))
        page.show()          # back from Settings
        assert started[-1] == bios, "a BIOS appearing re-runs the cover pass"
        assert len(reloads) == 2, "construction, then the BIOS change"
        page.hide()
        page.show()
        assert len(reloads) == 2, "...and not again on every visit"
        assert not page._bios_hint.isVisible()
    finally:
        page._stop_worker()
        page.deleteLater()
