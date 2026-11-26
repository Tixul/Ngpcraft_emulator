"""Run-ahead (ngpc_runahead): the future it shows must be the real future, the real
console must never feel it, and it must step aside while the game saves.

Real games are needed to say anything about prediction: a frozen test ROM predicts
itself trivially. They come from the folder named by
NGPC_ROMS; without them those tests skip and the save-area ones still run.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

import pytest

from core import native

pytestmark = pytest.mark.skipif(not native.available(), reason="native core not built")

REPO = Path(__file__).resolve().parents[1]
from rom_paths import BIOS as _BIOS, ROMS as _ROMS
CORPUS = _ROMS
BIOS = _BIOS
SONIC = CORPUS / "Sonic the Hedgehog Pocket Adventure (USA).ngc"


class _Page:
    """What RunAhead reads off a PlayPage, and nothing more."""

    def __init__(self, rom: Path, data: bytes, tmp: Path):
        import ngpc_settings as cfg
        from core.native_session import NativeSession
        self._rom_path = rom
        self._settings = cfg.make_settings()
        self.session = NativeSession(rom, rom_bytes=data, from_archive=True,
                                     autosave=False, save_path=tmp / "real.flash",
                                     bios_path=BIOS if BIOS.is_file() else None)
        self.machine = self.session.machine
        self.machine.set_timing_silicon(cfg.CART_FETCH_WAIT, cfg.CART_BIOS_WAIT)
        self.machine.reset(bios_handoff=True)

    def _bios_path(self):
        return BIOS if BIOS.is_file() else None

    def _mono_console(self):
        return False


def _pad(i: int) -> int:          # some play: A taps, right held in stretches, OPTION once
    return ((0x10 if i % 37 < 3 else 0) | (0x08 if (i // 90) % 2 else 0)
            | (0x40 if i in (200, 201) else 0))


def _play(page, ahead, frames, pad=_pad):
    """Run `frames` real frames; return (shown frames, verdict counts, real state hash)."""
    import ngpc_runahead as ra
    r = ra.RunAhead(page, ahead) if ahead else None
    shown = []
    for i in range(frames):
        page.machine.write(0xB0, bytes([pad(i)]))
        page.machine.run_frames(1)
        fb = r.frame(1) if r else None
        shown.append(fb if fb is not None else page.machine.framebuffer())
    state = hashlib.md5(bytes(page.machine.cpu()) + page.machine.read(0, 0xC000)).hexdigest()
    gave_up = r.gave_up if r else False
    if r:
        r.close()
    page.session.close()
    return shown, gave_up, state


needs_sonic = pytest.mark.skipif(not (SONIC.is_file() and BIOS.is_file()),
                                 reason="needs the game corpus (NGPC_ROMS) and bios.bin")


@needs_sonic
@pytest.mark.parametrize("ahead", [1, 2])
def test_the_future_shown_is_the_future_that_happens(tmp_path, ahead):
    """Frame i shown with `ahead` = the real frame i+ahead, whenever the buttons held
    across the gap did not change. Measured 1399/1400 and 1302/1302 when written; the
    one miss at 1 ahead is the very first frame, before any prediction existed."""
    data = SONIC.read_bytes()
    shown, gave_up, _ = _play(_Page(SONIC, data, tmp_path), ahead, 700)
    real, _, _ = _play(_Page(SONIC, data, tmp_path), 0, 700)
    judged = [i for i in range(5, 700 - ahead)
              if all(_pad(j) == _pad(i) for j in range(i, i + ahead + 1))]
    wrong = [i for i in judged if shown[i] != real[i + ahead]]
    assert len(judged) > 400 and not wrong, f"wrong futures at {wrong[:10]}"
    assert not gave_up, "the self-check gave up on a game it predicts perfectly"


@needs_sonic
def test_the_real_console_never_feels_it(tmp_path):
    data = SONIC.read_bytes()
    states = {ahead: _play(_Page(SONIC, data, tmp_path), ahead, 600)[2] for ahead in (0, 2)}
    assert states[0] == states[2]


@needs_sonic
def test_it_takes_the_frames_off_the_reaction(tmp_path):
    """Sonic, in play: press jump at frame 620 and count frames until the SHOWN picture
    changes. Measured 6 / 5 / 4 for 0 / 1 / 2 ahead."""
    data = SONIC.read_bytes()
    t = 620

    def reaction(ahead):
        def pad(i, pressed):
            return _pad(i) if i < 500 else (0x10 if pressed and t <= i < t + 8 else 0)
        a = _play(_Page(SONIC, data, tmp_path), ahead, t + 12, lambda i: pad(i, False))[0]
        b = _play(_Page(SONIC, data, tmp_path), ahead, t + 12, lambda i: pad(i, True))[0]
        return next(i - t for i in range(t, t + 12) if a[i] != b[i])

    base = reaction(0)
    assert [reaction(1), reaction(2)] == [base - 1, base - 2]


# -- the save area: a synthetic cart is enough --------------------------------
def _rom() -> bytes:
    rom = bytearray(b"\xFF" * 0x80000)
    rom[0:28] = b" LICENSED BY SNK CORPORATION"
    rom[0x1C:0x20] = (0x200040).to_bytes(4, "little")
    rom[0x23] = 0x10
    rom[0x40:0x44] = b"\x00\x68\xFE\x00"          # nop ; jr $
    return bytes(rom)


def test_a_save_pauses_it_and_reaches_the_ghost(tmp_path):
    import ngpc_runahead as ra
    page = _Page(tmp_path / "cart.ngc", _rom(), tmp_path)
    r = ra.RunAhead(page, 1)
    try:
        assert r.frame(1) is not None
        cap = page.machine.flash_capacity(0)
        where = 0x200000 + cap - 0x2000                 # inside the top 64 KiB
        page.machine.flash_restore(where, b"\x12\x34")  # the game "saved"
        assert r.frame(1) is None, "run-ahead must step aside while the chip is busy"
        assert r.ghost.machine.flash_peek(where, 2) == b"\x12\x34", "the ghost kept the old save"
        for _ in range(ra.PAUSE_AFTER_SAVE - 1):
            assert r.frame(1) is None
        assert r.frame(1) is not None, "...and comes back once it is over"
    finally:
        r.close()
        page.session.close()


def test_off_by_default():
    import ngpc_settings as cfg
    assert cfg.run_ahead(cfg.make_settings()) == 0


# -- the real player page ------------------------------------------------------
@needs_sonic
def test_the_player_page_runs_it_and_drops_it(tmp_path):
    """Through PlayPage itself: the setting builds the ghost on the first frames, the
    self-check holds through real play, and switching it off frees the ghost."""
    import shutil
    import ngpc_settings as cfg
    import ngpc_shell as shell
    from PyQt6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])   # held: a dropped one dies
    rom = tmp_path / "Sonic.ngc"                    # a copy: the player saves INTO the ROM
    shutil.copy(SONIC, rom)
    s = cfg.make_settings()
    s.clear()
    s.setValue("general/run_ahead", 1)
    w = shell.Shell()
    try:
        w.library._stop_worker()
        page = w.play
        page._frames_due = lambda: 1
        page.start(rom)
        for _ in range(700):
            page._tick()
        assert page._runahead is not None, "run-ahead was never built"
        assert not page._runahead.gave_up and page._runahead_off_for is None
        s.setValue("general/run_ahead", 0)
        page.apply_settings()
        assert page._runahead is None
        page._tick()
        assert page._runahead is None
    finally:
        w.play.stop()
        w.library._stop_worker()
        w.close()
        w.deleteLater()
        app.processEvents()
        s.clear()
