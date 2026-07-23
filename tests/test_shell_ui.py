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
        long_label = "Ferramentas de depuração"
        w._nav_text = dict(w._nav_text)          # leave the real labels alone
        w._nav_text[b] = long_label
        w._fit_rail()

        assert "\n" in b.text(), "a label this long must not stay on one line"
        assert b.text().split() == long_label.split(), "wrapping must not drop a word"
        assert not b.toolTip(), "it fits on two lines -- no tooltip needed"
        assert b.sizeHint().height() > w._nav_lib.sizeHint().height(), \
            "the two-line entry is taller than a one-line one"
        # the split is the balanced one, not the greedy fill
        greedy = max(fm.horizontalAdvance(shell.RAIL_INDENT + "Ferramentas de"),
                     fm.horizontalAdvance(shell.RAIL_INDENT + "depuração"))
        chosen = max(fm.horizontalAdvance(ln) for ln in b.text().split("\n"))
        assert chosen < greedy, f"balanced split should beat greedy ({chosen} vs {greedy})"
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
