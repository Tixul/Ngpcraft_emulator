"""Keep the test suite out of the user's console.

⛔ THE DAMAGE THIS PREVENTS, and it is not hypothetical -- it happened while writing
the tests next to this file. `saves/system.ram` is the COIN CELL: the language, the
colour theme and the clock the player set on the BIOS's own setup screen. Any test that
boots the real BIOS and then stops it goes through `commit_system_ram`, which writes
that file for real. A test only ticks a handful of frames, so what it commits is a
half-initialised BIOS page -- and the next hand-off boot restores that page as its
baseline. MEASURED: with the file a test had just written, `test_link_play` came up with
an entirely black framebuffer; move the file aside and the same tests pass.

So the suite writes a throwaway coin cell instead. Both names are patched because they
are two references to the same path: `core.native_session` owns it, and `ngpc_shell`
imported it under its own name at module load, so patching one leaves the other pointing
at the player's file.

Autouse and suite-wide on purpose. This is not a property of the tests that happen to
boot a BIOS today -- it is a property of booting one at all, and the next test to do it
should not have to know.
"""

from __future__ import annotations

import sys

import pytest

# Qt-free on purpose: this file is imported for EVERY test, including the ones that run
# with no PyQt6 installed (the UI tests skip themselves with `importorskip`). Importing
# the shell here would turn a missing PyQt6 from "some tests skip" into "collection
# fails", on every platform -- and the Linux CI runner is the one where Qt's shared
# libraries are least likely to all be present. `core.native_session` has no Qt in it.
import core.native_session as ns


@pytest.fixture(autouse=True)
def _sandbox_the_coin_cell(tmp_path, monkeypatch):
    cell = tmp_path / "system.ram"
    clock = tmp_path / "system.rtc"
    monkeypatch.setattr(ns, "SYSTEM_RAM_PATH", cell, raising=False)
    monkeypatch.setattr(ns, "SYSTEM_RTC_PATH", clock, raising=False)
    # The shell took its OWN references at import time (`SYSTEM_RAM_PATH as
    # _SYSTEM_RAM`), and `start_bios` / `stop` use THOSE -- patching only the module
    # above would leave the console-boot path writing the player's file. Looked up in
    # `sys.modules` rather than imported: a test that uses the shell has already
    # imported it (module level, during collection), and one that has not needs nothing
    # from it.
    shell = sys.modules.get("ngpc_shell")
    if shell is not None:
        monkeypatch.setattr(shell, "_SYSTEM_RAM", cell, raising=False)
        monkeypatch.setattr(shell, "_SYSTEM_RTC", clock, raising=False)
    yield


@pytest.fixture(autouse=True)
def _no_internet_for_the_host_card(monkeypatch):
    """La fiche d'adresse de l'hote ne sort PAS sur internet pendant les tests.

    ⛔ `HostInfoDialog` demande l'IP publique a api.ipify.org & co (3 services, 4 s
    chacun). En local ca repond en un clin d'oeil; sur le runner Windows de la CI, le
    `connect` reste bloque -- et c'est le seul fil qui differe entre les deux runs ou la
    suite est morte a ~65 % (« Windows fatal exception: access violation » pendant
    `test_closing_the_address_card_keeps_the_host_listening`, un fil dans
    `socket.create_connection`). Un test n'a rien a attendre d'un service tiers: sa
    reponse ne se verifie pas, et sa lenteur deplace tout le reste du banc.

    Sans Qt, comme ci-dessus: on n'importe `ngpc_lobby` que si Qt est deja la (le shell
    charge, ou le module lui-meme). ⚠️ `sys.modules` seul ne suffit PAS: le shell
    importe la fiche a la demande, dans `_show_host_info` -- un fichier de test lance
    seul n'aurait encore jamais charge `ngpc_lobby`, et la fiche serait sortie sur le
    reseau quand meme.
    """
    lobby = sys.modules.get("ngpc_lobby")
    if lobby is None and "ngpc_shell" in sys.modules:
        import ngpc_lobby as lobby
    if lobby is not None:
        monkeypatch.setattr(lobby, "_public_ip", lambda timeout=4.0: "", raising=False)
    yield
