"""The cost of one memory access, region by region, against the console (03/11/2026).

hw_calibration/a_acces_calib_v23.ngc times 512 copies of one access with RAS.V/RAS.H
as the clock; hw_calibration/a_rash_calib_v22.ngc reads RAS.H in a burst. On silicon, a
K2GE register or SFR access costs exactly what a RAM access costs, a control-register
write is not throttled like VRAM, and a VRAM read during active display is. Before the
fix the core ran the v22 loops 5-10 % too fast. See hw_calibration/access_gate.py.
"""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "hw_calibration"))

from core import native  # noqa: E402

BIOS = ROOT / "bios.bin"
pytestmark = [
    pytest.mark.skipif(not native.available(), reason="native core not built"),
    pytest.mark.skipif(not BIOS.is_file(), reason="needs the retail bios.bin (gitignored)"),
]


@pytest.fixture(scope="module")
def gate():
    import access_gate
    return access_gate


@pytest.fixture(scope="module")
def silicon_run(gate):
    # The default silicon model, exactly as the app runs it (no extra setter).
    from core import native as nat
    bios = BIOS.read_bytes()

    def run(rom_path, frames):
        m = nat.NativeMachine(rom_path.read_bytes(), bios=bios)
        m.set_timing_silicon()
        m.reset(bios_handoff=True)
        for _ in range(frames):
            m.write(0x00B0, bytes([0]))
            m.run_frames(1)
        return m
    return run


def test_v22_raster_burst_matches_the_console(gate, silicon_run):
    assert gate.read_v22(silicon_run) == gate.V22_SILICON


@pytest.mark.parametrize("test_id", [1, 2, 3, 4, 5, 6, 7, 8, 11])
def test_v23_access_cost_matches_the_console(gate, silicon_run, test_id):
    got = gate.read_v23(silicon_run)[test_id]
    assert got == pytest.approx(gate.V23_SILICON[test_id], abs=0.1), gate.V23_LABELS[test_id]


@pytest.mark.parametrize("test_id", [9, 10])
def test_v23_known_open_gaps_stay_where_they_are(gate, silicon_run, test_id):
    """ROM word read and SFR read are still 4 cy cheap: their instruction is one byte
    longer / shorter than the RAM one, so v23 cannot tell an access cost from a fetch
    cost (and v2 measured cart BYTE reads == RAM). Pinned so a change shows up here."""
    got = gate.read_v23(silicon_run)[test_id]
    assert got == pytest.approx(gate.V23_SILICON[test_id] - 4.0, abs=0.1), gate.V23_LABELS[test_id]


def test_disarmed_model_still_shows_the_gap_the_console_found(gate):
    """The bench must still bite: with the model off, v22 runs 175 -> 167 line changes."""
    run = gate.make_runner(False, 0)
    assert gate.read_v22(run)["A changements"] == 167
