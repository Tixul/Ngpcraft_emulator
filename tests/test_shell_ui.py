"""The modern shell (`ngpc_shell.py`) — Qt-offscreen structure + settings tests.

Skips cleanly when PyQt6 is absent, like the other UI tests. Runs under the
offscreen QPA platform so it needs no display. It does NOT boot a ROM here (that
is exercised elsewhere / by hand); it checks the shell wiring and the settings
round-trip, which is what the front-end contract is.
"""

from __future__ import annotations

import os

import pytest

pytest.importorskip("PyQt6")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import Qt, QSettings  # noqa: E402
from PyQt6.QtWidgets import QApplication  # noqa: E402

import ngpc_settings as cfg  # noqa: E402
import ngpc_shell as shell  # noqa: E402


@pytest.fixture(scope="module")
def app():
    a = QApplication.instance() or QApplication([])
    yield a


@pytest.fixture(autouse=True)
def _clean_settings():
    # Never touch the user's real settings: use a throwaway in-memory scope.
    s = cfg.make_settings()
    s.clear()
    yield
    s.clear()


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
