"""Tear Qt down BEFORE Python does.

The UI tests hold their `QApplication` in a class attribute, so it outlives the
last test and is destroyed by the interpreter's own finalisation. By then Python
is already shutting down, and any widget destructor that reaches back into Python
finds an interpreter that can no longer run it. PyQt's answer to that is
`qFatal("Unhandled Python exception")` -- which kills the process with
STATUS_STACK_BUFFER_OVERRUN (0xC0000409).

The symptom is nasty precisely because it is NOT a test failure: every test passes,
the summary never prints, and the runner reports a crash. It fires reliably on some
subsets of the suite and about one run in four on the whole of it -- which is how it
came to look, wrongly, like a memory bug in the native core. It is not: a core built
under AddressSanitizer is clean, and an attached debugger shows the fatal exception
raised inside Qt6Core with FAST_FAIL_FATAL_APP_EXIT. See DEVLOG pass 231.

So we destroy the QApplication while the interpreter is still alive and able to run
the Python halves of those destructors.
"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path


def pytest_configure(config):  # noqa: ANN001, ARG001
    """Send QSettings to a throwaway directory BEFORE any test can reach it.

    `cfg.make_settings()` is `QSettings("NgpCraft", "Emulator")` -- the REAL user
    scope, the Windows registry. The UI tests wrap every test in a fixture that
    calls `.clear()` on it, under a comment claiming it is "a throwaway in-memory
    scope". It never was: running the suite silently deleted the user's own BIOS
    path, ROM folder, language and window geometry, once per test. It looked like
    settings being lost "on every new version" because a new version is when you
    run the tests.

    Pointing `NGPCRAFT_SETTINGS` at a temp .ini here -- in `pytest_configure`, so it
    lands before collection imports anything -- means that same `.clear()` now wipes a
    throwaway file. `test_the_suite_never_touches_real_settings` fails if this ever
    stops working.

    Qt's own redirect (`QSettings.setDefaultFormat` + `setPath`) is NOT enough: it is
    documented to steer the (organization, application) constructor and, measured on
    this build, leaves it on the registry regardless. The env var is what actually
    holds, which is why `make_settings()` reads one.
    """
    os.environ[cfg_env()] = str(
        Path(tempfile.mkdtemp(prefix="ngpcraft-tests-")) / "settings.ini")


def cfg_env() -> str:
    """The env var name, read from the settings module so the two cannot drift."""
    import ngpc_settings

    return ngpc_settings.SETTINGS_FILE_ENV


def pytest_sessionfinish(session, exitstatus):  # noqa: ANN001, ARG001
    try:
        from PyQt6 import sip
        from PyQt6.QtWidgets import QApplication
    except ImportError:
        return

    app = QApplication.instance()
    if app is None:
        return

    for widget in list(app.topLevelWidgets()):
        widget.close()
        widget.deleteLater()
    app.processEvents()          # let deleteLater() actually run
    app.quit()
    sip.delete(app)              # destroy it HERE, not during finalisation
