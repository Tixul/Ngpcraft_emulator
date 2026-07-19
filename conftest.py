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
