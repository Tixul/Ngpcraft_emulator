"""Reentrant cyclic GC must not free windows inside Qt's global widget walk."""
import os
from pathlib import Path
import subprocess
import sys
import textwrap

import pytest

pytest.importorskip('PyQt6')


@pytest.mark.parametrize('collector_enabled', [False, True])
def test_palette_update_retains_windows_during_layout_gc(tmp_path, collector_enabled):
    # A subprocess turns a native regression into a useful failing assertion with
    # its faulthandler trace, instead of aborting the rest of the test suite.
    script = textwrap.dedent('''
        import gc
        from pathlib import Path
        import sys
        import weakref
        from PyQt6 import sip
        from PyQt6.QtWidgets import QApplication
        import ngpc_settings as cfg
        import ngpc_shell as shell
        import ngpc_theme as theme

        scratch = Path(sys.argv[1])
        enabled = sys.argv[2] == 'True'
        for name, folder in [('DEFAULT_ROM_DIR', 'roms'), ('THUMB_DIR', 'thumbnails'),
                             ('COVER_DIR', 'covers'), ('STATE_DIR', 'states'),
                             ('WATCH_DIR', 'watches'), ('CRASH_DIR', 'crashes')]:
            setattr(shell, name, scratch / folder)
        shell.LIBRARY_DB = scratch / 'library.json'
        settings = cfg.make_settings()
        settings.setValue('general/check_updates', False)
        settings.sync()
        app = QApplication([])
        app.setQuitOnLastWindowClosed(False)
        gc.disable()
        windows = []
        for _ in range(3):
            window = shell.Shell()
            window.cycle = window
            window.resize(640, 400)
            window.show()
            windows.append(weakref.ref(window))
        app.processEvents()
        for ref in windows:
            ref().close()  # Match earlier UI tests: hidden, cyclic, not C++-deleted.
        del window
        # Prevent incidental setup allocations from deciding the trigger point.
        # Explicit collection below still runs with either automatic-GC setting.
        gc.set_threshold(100000000, 100000000, 100000000)
        if enabled:
            gc.enable()
        observed = []
        def trace(frame, event, arg):
            if (not observed and event == 'line' and
                    frame.f_code.co_filename == shell.__file__ and
                    frame.f_code.co_name == '_lay' and 'row_w' in frame.f_locals):
                before = sum(ref() is not None for ref in windows)
                gc.collect()
                after = sum(ref() is not None for ref in windows)
                observed.append((before, after, sip.isdeleted(frame.f_locals['self']), gc.isenabled()))
                print('reentrant collection:', observed[-1], flush=True)
            return trace
        sys.settrace(trace)
        theme.apply_app_palette(app, theme.LIGHT)
        sys.settrace(None)
        assert observed, 'the real Qt height-for-width callback was not exercised'
        assert observed == [(3, 3, False, enabled)], observed
        assert gc.isenabled() == enabled, 'do not change the caller GC policy'
        gc.collect()
        assert all(ref() is None for ref in windows), 'do not retain closed windows after the update'
        print('layout GC completed safely; wrappers released after the native update', flush=True)
    ''')
    env = os.environ.copy()
    env.pop('NGPCRAFT_DATA_DIR', None)
    env.update(QT_QPA_PLATFORM='offscreen', PYTHONFAULTHANDLER='1',
               NGPCRAFT_SETTINGS=str(tmp_path / 'settings.ini'),
               NGPCRAFT_NETLOG=str(tmp_path / 'netplay.log'), NGPCRAFT_NO_UPNP='1')
    result = subprocess.run([sys.executable, '-X', 'faulthandler', '-c', script,
                             str(tmp_path), str(collector_enabled)],
                            cwd=Path(__file__).resolve().parents[1], env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            timeout=60)
    assert result.returncode == 0, result.stdout.decode('utf-8', errors='replace')
