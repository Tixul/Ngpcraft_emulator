import pytest
from PIL import Image
from ngpc_gif import Recording


@pytest.mark.parametrize('fps', [10, 20, 30])
def test_duration_and_pixels(tmp_path, fps):
    rec = Recording(300, fps)
    calls = []
    def frame():
        calls.append(1)
        return [0x00f if len(calls) % 2 else 0xf00] * (160 * 152)
    for i in range(300):
        assert rec.feed(frame) == (i == 299)
    assert len(calls) == fps * 5
    assert rec.feed(frame)
    assert len(calls) == fps * 5
    path = tmp_path / 'capture.gif'
    assert rec.save(path)
    with Image.open(path) as gif:
        assert gif.size == (160, 152)
        assert gif.info['loop'] == 0
        assert gif.convert('RGB').getpixel((0, 0)) == (255, 0, 0)
        duration = 0
        for i in range(gif.n_frames):
            gif.seek(i)
            duration += gif.info['duration']
        assert duration == 5000


def test_early_stop_and_partial_frame(tmp_path):
    rec = Recording(600, 20)
    for i in range(7):
        rec.feed(lambda: [i] * (160 * 152))
    path = tmp_path / 'short.gif'
    rec.save(path)
    with Image.open(path) as gif:
        delays = []
        for i in range(gif.n_frames):
            gif.seek(i)
            delays.append(gif.info['duration'])
        assert delays == [50, 50, 20]


def test_empty_and_limits(tmp_path):
    assert not Recording(60, 20).save(tmp_path / 'empty.gif')
    assert not (tmp_path / 'empty.gif').exists()
    for frames, fps in [(0, 20), (3601, 20), (60, 60)]:
        with pytest.raises(ValueError):
            Recording(frames, fps)


@pytest.mark.parametrize('delay_seconds,cancel', [(0, False), (3, False), (3, True)])
def test_panel_persistence_and_background_export(tmp_path, monkeypatch, delay_seconds, cancel):
    import os
    os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
    from PyQt6.QtCore import QSettings, QTimer
    from PyQt6.QtWidgets import QApplication, QWidget, QHBoxLayout, QPushButton, QDialog, QSpinBox
    from ngpc_gif import install_button
    import ngpc_gif
    now = [100.0]
    monkeypatch.setattr(ngpc_gif.time, 'monotonic', lambda: now[0])
    app = QApplication.instance() or QApplication([])
    page = QWidget()
    page._settings = QSettings(str(tmp_path / 'config.ini'), QSettings.Format.IniFormat)
    page._settings.setValue('paths/screenshots', str(tmp_path))
    page.paused = False
    page._rom_path = tmp_path / 'game.ngc'
    page.machine = type('Machine', (), {'framebuffer': lambda self: [0xf] * (160 * 152)})()
    messages = []
    page._flash = messages.append
    layout = QHBoxLayout(page)
    install_button(page, layout, tmp_path)
    button = page.findChild(QPushButton)
    def accept():
        dialog = page.findChild(QDialog)
        dialog.findChild(QSpinBox).setValue(1)
        assert dialog.findChild(QSpinBox, 'gifDelay').value() == 3
        dialog.findChild(QSpinBox, 'gifDelay').setValue(delay_seconds)
        dialog.accept()
    QTimer.singleShot(0, accept)
    button.click()
    assert not page.paused
    assert page._settings.value('gif/amount', type=int) == 1
    assert page._settings.value('gif/delay', type=int) == delay_seconds
    if delay_seconds:
        assert page._gif_recording is None
        assert button.text() == 'GIF 3 s'
        page._gif_feed()
        timer = page.findChild(QTimer)
        now[0] += 1.1
        timer.timeout.emit()
        assert button.text() == 'GIF 2 s'
        if cancel:
            button.click()
            assert not timer.isActive()
            now[0] += 10
            timer.timeout.emit()
            assert page._gif_recording is None
            assert not list(tmp_path.glob('*.gif'))
            assert button.text() == 'GIF'
            return
        now[0] += 1.9
        timer.timeout.emit()
        assert not timer.isActive()
        assert page._gif_recording.elapsed == 0
    for _ in range(60):
        page._gif_feed()
    assert page._gif_recording is None
    page._gif_finish(wait=True)
    app.processEvents()
    assert button.isEnabled()
    assert len(list(tmp_path.glob('game_*.gif'))) == 1
    assert messages
