"""Where the tests find the files this repository does not ship: the BIOS and cartridges.

Neither is distributed. The BIOS is read from the repository's own `bios.bin`, as the
emulator does, or from `NGPC_BIOS`. Cartridges are read from `NGPC_ROMS` (a folder), else
from `roms/`. A test whose file is missing skips -- it never fails for lack of a ROM.
"""

from __future__ import annotations

import os
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
ROMS = Path(os.environ.get("NGPC_ROMS") or REPO / "roms")
BIOS = Path(os.environ.get("NGPC_BIOS") or REPO / "bios.bin")
