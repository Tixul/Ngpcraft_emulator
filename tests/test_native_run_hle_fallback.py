"""`ngpc_native.py run` with no --bios falls back to the clean-room HLE image.

⛔ THE BUG THIS CONDEMNS. The shell has always fallen back to hle_bios/bios_hle.bin;
the headless run (and so every `ngpc_emu_native_run` MCP call) loaded NO image at
all. The system-call table at 0xFFFE00 then read 0, and a cc900 homebrew (DDMR,
2026-10-23) died two frames in: SysSetSystemFont does `ld xix,(0xFFFE14)` /
`call xix` and landed on address 0. The same ROM ran with --bios bios_hle.bin.
"""

from __future__ import annotations

import argparse
from pathlib import Path

import pytest

import ngpc_native
from core import native

ENTRY = 0x200040
BODY = bytes.fromhex("68FE")      # jr $


def _rom(tmp_path: Path) -> Path:
    data = bytearray(0x40)
    data[0x00:0x1C] = b"LICENSED BY SNK CORPORATION".ljust(0x1C, b"\x00")
    data[0x1C:0x20] = ENTRY.to_bytes(4, "little")
    data[0x23] = 0x10
    data[0x24:0x30] = b"HLE FALLBACK"
    data += BODY
    data += b"\xFF" * (0x4000 - len(data))
    path = tmp_path / "hle_fallback.ngc"
    path.write_bytes(bytes(data))
    return path


@pytest.mark.skipif(not native.available(), reason="native core not built")
@pytest.mark.skipif(not ngpc_native.HLE_BIOS.is_file(), reason="bios_hle.bin not built")
def test_run_without_bios_uses_the_hle_system_call_table(tmp_path: Path) -> None:
    out = ngpc_native.cmd_run(argparse.Namespace(
        rom=str(_rom(tmp_path)), bios=None, state=None, frames=2, hold=None, shot=None,
        peek=("0xFFFE14", "4"), timing="silicon", hw_guard=False))
    assert out["bios"] == "hle"
    vector = int.from_bytes(bytes.fromhex(out["peek"]["bytes"]), "little")
    assert vector >= 0xFF0000             # VECT_SYSFONTSET points into the image, not 0
