"""Gamepad reading and turbo (autofire), both feeding the same joypad mask.

Two features that look unrelated but are the same problem: something other than
a key-down/key-up pair decides what the console sees in its 0xB0 register. So
they live together, behind one call the player makes per frame.

GAMEPAD. Qt6 dropped QtGamepad and the project ships only PyQt6 + numpy, so
rather than pull in SDL/pygame (a new dependency, and one more thing for the
PyInstaller build to get wrong) this reads Windows' XInput directly through
ctypes -- the API every Xbox-style pad speaks natively. Off Windows, or with no
DLL present, `XInputPad` reports itself unavailable and the shell simply keeps
running on the keyboard; nothing else has to know.

TURBO. A held button that the console must see as a rapid press/release train.
That only works if 0xB0 is written EVERY emulated frame -- the shell used to
write it once per timer tick (a batch of frames), which would make an autofire
stutter at the batch rate instead of the rate you asked for.
"""

from __future__ import annotations

import ctypes
import sys
import time

# NGPC joypad bits, mirrored from ngpc_settings.JOYPAD_BUTTONS (kept here as
# plain ints so this module stays importable without Qt).
UP, DOWN, LEFT, RIGHT = 0x01, 0x02, 0x04, 0x08
A, B, OPTION = 0x10, 0x20, 0x40


# --------------------------------------------------------------- gamepad
class _XInputState(ctypes.Structure):
    class _Gamepad(ctypes.Structure):
        _fields_ = [
            ("wButtons", ctypes.c_ushort),
            ("bLeftTrigger", ctypes.c_ubyte),
            ("bRightTrigger", ctypes.c_ubyte),
            ("sThumbLX", ctypes.c_short),
            ("sThumbLY", ctypes.c_short),
            ("sThumbRX", ctypes.c_short),
            ("sThumbRY", ctypes.c_short),
        ]

    _fields_ = [("dwPacketNumber", ctypes.c_uint), ("Gamepad", _Gamepad)]


# XInput button bits (xinput.h)
_XI_DPAD_UP, _XI_DPAD_DOWN, _XI_DPAD_LEFT, _XI_DPAD_RIGHT = 0x0001, 0x0002, 0x0004, 0x0008
_XI_START, _XI_BACK = 0x0010, 0x0020
_XI_A, _XI_B, _XI_X, _XI_Y = 0x1000, 0x2000, 0x4000, 0x8000

# The NGPC has two face buttons; a modern pad has four. Map BOTH diagonal pairs so
# either grip works without a settings trip: A/X -> NGPC A, B/Y -> NGPC B.
_FACE_TO_NGPC = (
    (_XI_A, A), (_XI_X, A),
    (_XI_B, B), (_XI_Y, B),
    (_XI_START, OPTION), (_XI_BACK, OPTION),
)
_DPAD_TO_NGPC = (
    (_XI_DPAD_UP, UP), (_XI_DPAD_DOWN, DOWN),
    (_XI_DPAD_LEFT, LEFT), (_XI_DPAD_RIGHT, RIGHT),
)

# Past this much stick deflection a direction counts as pressed. XInput's own
# resting deadzone is 7849; well above it, so a centred stick never drifts into
# a held direction on a worn pad.
_STICK_THRESHOLD = 16000

_DLLS = ("xinput1_4.dll", "xinput1_3.dll", "xinput9_1_0.dll")

# ⚠️ Querying an EMPTY controller slot is expensive -- XInput goes and looks for
# the device, and the call can cost the better part of a millisecond. Polling an
# unplugged pad at frame rate is a well-known way to lose frames for nothing. So
# a slot that answered "not connected" is only re-probed every few seconds; a
# connected one is free to poll as often as the caller likes.
_RETRY_SECONDS = 2.0


class XInputPad:
    """Polls controller 0. Safe to call every frame: see `_RETRY_SECONDS` for how
    the disconnected case is kept cheap."""

    def __init__(self) -> None:
        self._dll = None
        self._connected = False
        self._next_probe = 0.0
        self._mask = 0
        if not sys.platform.startswith("win"):
            return
        for name in _DLLS:
            try:
                self._dll = ctypes.windll.LoadLibrary(name)
                break
            except OSError:
                continue

    @property
    def available(self) -> bool:
        """True when the XInput API itself is usable -- NOT that a pad is plugged
        in. A pad can be connected at any moment, so the poll must keep trying."""
        return self._dll is not None

    @property
    def connected(self) -> bool:
        """Whether the last poll actually saw a controller."""
        return self._connected

    def poll(self) -> int:
        """Current pad state as an NGPC joypad mask (0 if no pad / no XInput)."""
        if self._dll is None:
            return 0
        if not self._connected and time.monotonic() < self._next_probe:
            return 0                    # still unplugged as far as we know -- see above
        state = _XInputState()
        try:
            # ERROR_SUCCESS(0) = a pad is there; ERROR_DEVICE_NOT_CONNECTED(1167)
            # is the normal answer with nothing plugged in, not a failure.
            if self._dll.XInputGetState(0, ctypes.byref(state)) != 0:
                self._connected = False
                self._next_probe = time.monotonic() + _RETRY_SECONDS
                self._mask = 0
                return 0
        except OSError:
            self._connected = False
            self._next_probe = time.monotonic() + _RETRY_SECONDS
            self._mask = 0
            return 0
        self._connected = True
        pad = state.Gamepad
        mask = 0
        for xi_bit, ngpc_bit in _DPAD_TO_NGPC:
            if pad.wButtons & xi_bit:
                mask |= ngpc_bit
        for xi_bit, ngpc_bit in _FACE_TO_NGPC:
            if pad.wButtons & xi_bit:
                mask |= ngpc_bit
        # Left stick doubles as the d-pad: most pads' sticks are more comfortable
        # than their d-pad, and a lot of NGPC games are stick-friendly anyway.
        if pad.sThumbLX <= -_STICK_THRESHOLD:
            mask |= LEFT
        elif pad.sThumbLX >= _STICK_THRESHOLD:
            mask |= RIGHT
        if pad.sThumbLY <= -_STICK_THRESHOLD:
            mask |= DOWN
        elif pad.sThumbLY >= _STICK_THRESHOLD:
            mask |= UP
        # Opposite directions at once is electrically impossible on the real
        # d-pad, and games do not all handle it. Stick + d-pad can produce it.
        if mask & LEFT and mask & RIGHT:
            mask &= ~(LEFT | RIGHT)
        if mask & UP and mask & DOWN:
            mask &= ~(UP | DOWN)
        self._mask = mask
        return mask


# ----------------------------------------------------------------- turbo
def apply_turbo(held: int, turbo_mask: int, frame: int, hz: int) -> int:
    """Chop the turbo-flagged buttons of `held` into an on/off train at `hz`.

    `frame` is a free-running count of EMULATED frames (not host frames), so the
    autofire rate is the same whether the emulator is fast-forwarding or crawling
    -- it is the console's own 60 Hz that the game counts, not the wall clock.

    Buttons without a turbo flag pass through untouched.
    """
    plain = held & ~turbo_mask
    fire = held & turbo_mask
    if not fire:
        return plain
    # A full press+release cycle at `hz`, held for the first half of it. Clamped
    # so 30 Hz (period 2) still alternates instead of collapsing to always-on.
    period = max(2, round(60 / max(1, hz)))
    if (frame % period) < (period // 2):
        return plain | fire
    return plain
