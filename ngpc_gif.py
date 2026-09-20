"""Bounded GIF recording in emulated frames; encoding never touches the core."""
from pathlib import Path
import os
import tempfile
import time
import math

import numpy as np
from PIL import Image


class Recording:
    def __init__(self, frames, fps):
        if not 1 <= frames <= 3600 or fps not in (10, 20, 30):
            raise ValueError("Invalid GIF duration or frame rate")
        self.limit, self.fps = frames, fps
        self.elapsed = 0
        self.images = []

    def feed(self, framebuffer):
        if self.elapsed >= self.limit:
            return True
        if self.elapsed % (60 // self.fps) == 0:
            a = np.asarray(framebuffer(), dtype=np.uint16).reshape(152, 160)
            rgb = np.stack(((a & 15) * 17, ((a >> 4) & 15) * 17,
                            ((a >> 8) & 15) * 17), axis=-1).astype(np.uint8)
            self.images.append(Image.fromarray(rgb))
        self.elapsed += 1
        return self.elapsed == self.limit

    def save(self, path):
        if not self.images:
            return False
        # GIF delays have 10 ms precision. Round cumulative boundaries so
        # 30 fps does not silently become 33.3 fps; retain a partial last frame.
        step = 60 // self.fps
        ends = [min((i + 1) * step, self.elapsed) for i in range(len(self.images))]
        boundaries = [0] + [max(1, round(n * 100 / 60)) * 10 for n in ends]
        delays = [b - a for a, b in zip(boundaries, boundaries[1:])]
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(dir=path.parent, suffix=".gif")
        os.close(fd)
        try:
            self.images[0].save(temporary, format="GIF", save_all=True,
                                append_images=self.images[1:], duration=delays,
                                loop=0, disposal=2)
            os.replace(temporary, path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
        return True


def install_button(page, layout, repo):
    """Lazy UI imports keep the recording/encoder independently testable."""
    from PyQt6.QtCore import QThread, QTimer, pyqtSignal, Qt
    from PyQt6.QtWidgets import (QPushButton, QDialog, QVBoxLayout, QComboBox,
                                QSpinBox, QLabel, QDialogButtonBox)
    import ngpc_settings as cfg

    class Encoder(QThread):
        result = pyqtSignal(str)

        def __init__(self, recording, path):
            super().__init__(page)
            self.recording, self.path = recording, path

        def run(self):
            try:
                saved = self.recording.save(self.path)
                self.result.emit(str(self.path) if saved else t("gif_no_frames"))
            except Exception as exc:
                self.result.emit(t("gif_export_failed") + str(exc))
            finally:
                self.recording.images.clear()

    def t(key):
        return cfg.tr(cfg.language(page._settings), key)

    button = QPushButton("GIF")
    button.setObjectName("barBtn")
    button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
    button.setToolTip(t("gif_record_tooltip"))
    layout.addWidget(button)
    page._gif_recording = None
    page._gif_encoder = None
    pending = None
    deadline = 0.0
    countdown = QTimer(page)
    countdown.setInterval(100)

    def tick_countdown():
        nonlocal pending
        if pending is None:
            countdown.stop()
            return
        if page.machine is None:
            finish()
            return
        remaining = math.ceil(deadline - time.monotonic())
        if remaining > 0:
            button.setText(f"GIF {remaining} s")
        else:
            countdown.stop()
            page._gif_recording, pending = pending, None
            button.setText(f"STOP 0/{page._gif_recording.limit}")

    countdown.timeout.connect(tick_countdown)

    def finish(wait=False):
        nonlocal pending
        countdown.stop()
        pending = None
        button.setText("GIF")
        rec, page._gif_recording = page._gif_recording, None
        if rec is not None:
            button.setText("GIF")
            if rec.images:
                worker = Encoder(rec, page._gif_path)
                page._gif_encoder = worker
                button.setEnabled(False)
                worker.result.connect(page._flash)
                worker.finished.connect(lambda: button.setEnabled(True))
                worker.start()
        if wait and page._gif_encoder is not None:
            page._gif_encoder.wait()

    def feed():
        rec = page._gif_recording
        if rec is None:
            return
        try:
            done = rec.feed(page.machine.framebuffer)
            button.setText(f"STOP {rec.elapsed}/{rec.limit}")
            if done:
                finish()
        except Exception as exc:
            page._gif_recording = None
            button.setText("GIF")
            page._flash(str(exc))

    def configure():
        nonlocal pending, deadline
        if pending is not None or page._gif_recording is not None:
            finish()
            return
        if page.machine is None:
            return
        dialog = QDialog(page)
        dialog.setWindowTitle(t("gif_title"))
        box = QVBoxLayout(dialog)
        unit = QComboBox()
        unit.addItems([t("gif_seconds"), t("gif_game_frames")])
        unit.setCurrentIndex(max(0, min(1, int(page._settings.value("gif/unit", 0)))))
        amount = QSpinBox()
        fps = QComboBox()
        fps.addItems(["10", "20", "30"])
        stored_fps = str(page._settings.value("gif/fps", "20"))
        fps.setCurrentText(stored_fps if stored_fps in ("10", "20", "30") else "20")
        info = QLabel()
        delay = QSpinBox()
        delay.setObjectName("gifDelay")
        delay.setRange(0, 10)
        delay.setSuffix(" s")
        delay.setValue(int(page._settings.value("gif/delay", 3)))

        def refresh():
            amount.setRange(1, 60 if unit.currentIndex() == 0 else 3600)
            frames = amount.value() * 60 if unit.currentIndex() == 0 else amount.value()
            info.setText(f"{frames} frames = {frames / 60:g} s\n160 × 152 px · " +
                         t("gif_capture_hint"))

        refresh()
        amount.setValue(int(page._settings.value("gif/amount", 10)))
        unit.currentIndexChanged.connect(refresh)
        amount.valueChanged.connect(refresh)
        refresh()
        box.addWidget(unit)
        box.addWidget(amount)
        box.addWidget(QLabel(t("gif_fps")))
        box.addWidget(fps)
        box.addWidget(QLabel(t("gif_delay")))
        box.addWidget(delay)
        box.addWidget(info)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText(t("gif_start"))
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        box.addWidget(buttons)
        was_paused = page.paused
        page.paused = True
        try:
            accepted = dialog.exec() == QDialog.DialogCode.Accepted
        finally:
            page.paused = was_paused
        if not accepted or page.machine is None:
            return
        for key, value in (("unit", unit.currentIndex()), ("amount", amount.value()),
                           ("fps", fps.currentText()), ("delay", delay.value())):
            page._settings.setValue("gif/" + key, value)
        from datetime import datetime
        folder = Path(cfg.screenshot_dir(page._settings) or repo / "screenshots")
        stem = page._rom_path.stem if page._rom_path else "ngpc"
        page._gif_path = folder / f"{stem}_{datetime.now():%Y%m%d_%H%M%S_%f}.gif"
        frames = amount.value() * 60 if unit.currentIndex() == 0 else amount.value()
        pending = Recording(frames, int(fps.currentText()))
        deadline = time.monotonic() + delay.value()
        tick_countdown()
        if pending is not None:
            countdown.start()
        page.setFocus()

    button.clicked.connect(configure)
    page._gif_finish = finish
    page._gif_feed = feed
