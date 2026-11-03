"""Exercise startup buffering and later macOS open events in an isolated Qt app."""
import os
import subprocess
import sys
from pathlib import Path


def test_file_open_before_ready_later_and_during_an_open(tmp_path):
    rom = tmp_path / "游戏 with spaces.ngc"
    rom.write_bytes(b"test")
    other = tmp_path / "other.ngp"
    other.write_bytes(b"test")
    code = r'''
import sys
from PyQt6.QtCore import QCoreApplication, QEvent, QUrl
from ngpc_application import NgpCraftApplication
app = NgpCraftApplication([])
first, second = sys.argv[1:]
got = []
class OpenEvent(QEvent):
    # QFileOpenEvent's constructor is protected in PyQt6. Model its public URL
    # contract here; actual Apple Events are tested against the packaged app.
    def __init__(self, url):
        super().__init__(QEvent.Type.FileOpen)
        self._url = url
    def url(self):
        return self._url
def send(path):
    QCoreApplication.sendEvent(app, OpenEvent(QUrl.fromLocalFile(path)))
send(first)  # Arrives before Shell or the socket server exists.
assert not got
def handle(path):
    got.append(path)
    if len(got) == 1:
        send(second)  # A nested native event loop must not reenter ROM loading.
        app.queue_open('')  # Socket foreground request shares the same queue.
        app.processEvents()
        assert got == [first]
app.set_open_handler(handle)
assert not got  # No loading inside Shell initialization.
app.processEvents()
assert got == [first, second, ''], got
send(first)  # The same ROM may be explicitly reopened later.
app.processEvents()
assert got == [first, second, '', first], got
QCoreApplication.sendEvent(app, OpenEvent(QUrl('https://example.com/x.ngc')))
send(first + '.missing.ngc')
send(__file__ if '__file__' in globals() else '/etc/hosts')
app.processEvents()
assert got == [first, second, '', first], got
app.queue_open(second)
app.queue_open(first)
assert app.take_startup_requests() == [second, first]
assert app.take_startup_requests() == []
app.queue_open(second)  # A newer launch's event waits behind the import handoff.
app.prepend_startup_requests([first])
app.processEvents()
assert got[-2:] == [first, second], got
'''
    env = dict(os.environ, QT_QPA_PLATFORM="offscreen")
    result = subprocess.run([sys.executable, "-c", code, str(rom), str(other)],
                   cwd=Path(__file__).resolve().parents[1], env=env,
                   capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stdout + result.stderr


def test_secondary_launch_receives_delayed_and_multiple_startup_requests():
    code = r'''
from PyQt6.QtCore import QTimer
from ngpc_application import NgpCraftApplication
app = NgpCraftApplication([])
got = []
QTimer.singleShot(20, lambda: app.queue_open('late.ngc'))
QTimer.singleShot(60, lambda: app.queue_open('other.ngp'))
app.forward_startup_events(got.append, timeout_ms=120)
assert got == ['late.ngc', 'other.ngp'], got
'''
    result = subprocess.run([sys.executable, '-c', code],
                            cwd=Path(__file__).resolve().parents[1],
                            env=dict(os.environ, QT_QPA_PLATFORM='offscreen'),
                            capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stdout + result.stderr
