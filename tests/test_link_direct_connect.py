"""Direct host/join, and keeping two local consoles in step.

Two bug reports from a homebrew author writing a link game, both answered here:

* "sometimes tries to connect using direct host/client mode but it never seems to
  actually get through" -- the host's accept() had no timeout and no reachable
  cancel, so a first attempt nobody could join armed `_start_net`'s one-at-a-time
  guard for the rest of the session and every later click died in silence.
* "if you adjust the speed (or pause) in one session, it would be useful to apply
  the same adjustment to the other side" -- and it is not cosmetic: the frame
  scheduler refuses to drive a paused peer, so a one-sided pause left the other
  console running alone until the game's link protocol timed out.

Nothing here touches the core: these are the Qt/session layers above it. Real
loopback sockets, no display (offscreen QPA), and every timeout shortened on the
instance so the file runs in about a second.
"""

from __future__ import annotations

import os
import socket
import time

import pytest

pytest.importorskip("PyQt6")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import Qt  # noqa: E402
from PyQt6.QtWidgets import QApplication  # noqa: E402

import ngpc_settings as cfg  # noqa: E402
import ngpc_shell as shell  # noqa: E402


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def wait_until(pred, timeout=5.0, step=0.02) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(step)
    return False


class Spy:
    """Collects a signal's payloads. Connected DIRECT, so it works in a test with
    no running event loop -- a queued connection would never be delivered."""

    def __init__(self):
        self.calls = []

    def __call__(self, *a):
        self.calls.append(a)


def attempt(mode, host, port, **limits):
    th = shell._NetConnect(mode, host, port, "en")
    th.POLL_S = 0.02
    for name, value in limits.items():
        setattr(th, name, value)
    ok, bad = Spy(), Spy()
    th.connected.connect(ok, Qt.ConnectionType.DirectConnection)
    th.failed.connect(bad, Qt.ConnectionType.DirectConnection)
    return th, ok, bad


# --- the attempt can always be taken back ----------------------------------
def test_a_pending_host_can_be_cancelled(app):
    """The whole dead end started here: an accept() nobody could interrupt."""
    th, ok, bad = attempt("host", "", free_port(), HOST_TIMEOUT_S=30.0)
    th.start()
    assert wait_until(lambda: th._srv is not None), "never got as far as listening"

    th.cancel()
    assert th.wait(3000), "cancel did not release the accept"
    assert not ok.calls and not bad.calls, "a cancel is not a failure to report"


def test_a_cancelled_host_frees_its_port(app):
    """...and it must really let go, or the next attempt hits EADDRINUSE."""
    port = free_port()
    th, _ok, _bad = attempt("host", "", port, HOST_TIMEOUT_S=30.0)
    th.start()
    assert wait_until(lambda: th._srv is not None)
    th.cancel()
    assert th.wait(3000)

    again = socket.socket()
    again.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        again.bind(("0.0.0.0", port))       # would raise if the listener leaked
    finally:
        again.close()


def test_a_host_nobody_joins_gives_up_and_says_why(app):
    """It used to wait for ever. Now it stops, and the message names the cause."""
    th, ok, bad = attempt("host", "", free_port(), HOST_TIMEOUT_S=0.3)
    th.start()
    assert th.wait(5000)
    assert not ok.calls
    assert len(bad.calls) == 1
    why = bad.calls[0][0]
    assert cfg.tr("en", "net_err_timeout_host") in why
    assert "WinError" not in why and "errno" not in why.lower()


# --- joining ---------------------------------------------------------------
def test_joining_waits_for_the_host_to_click_host(app):
    """⛔ THE FRICTION THIS ENDS: a join used to fail instantly with ECONNREFUSED
    if you were the first of the two to be ready, which is half the time."""
    port = free_port()
    th, ok, bad = attempt("join", "127.0.0.1", port, JOIN_TIMEOUT_S=8.0)
    th.start()
    time.sleep(0.2)                          # nothing is listening yet
    assert not ok.calls and not bad.calls, "gave up before the host was even up"

    srv = socket.socket()
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", port))
    srv.listen(1)
    try:
        assert wait_until(lambda: bool(ok.calls)), "never connected once the host was up"
        assert not bad.calls
        conn, _ = srv.accept()
        conn.close()
    finally:
        srv.close()
        th.cancel()
        th.wait(3000)
    ok.calls[0][0].close()


def test_a_name_that_does_not_resolve_fails_at_once(app):
    """No point retrying a hostname for two minutes: it will not start resolving."""
    th, ok, bad = attempt("join", "no.such.host.invalid", 7788, JOIN_TIMEOUT_S=30.0)
    th.start()
    assert th.wait(10000), "retried a dead name instead of failing"
    assert not ok.calls and len(bad.calls) == 1
    assert cfg.tr("en", "net_err_name") in bad.calls[0][0]


def test_a_connection_that_lands_after_a_cancel_is_dropped(app):
    """⚠️ A cancel and a connection CAN cross: the peer may land in the instant
    between the last check and the emit. Losing that race must mean dropping the
    socket -- not attaching a link the player has just taken back."""
    th, ok, bad = attempt("host", "", free_port())
    landed, other = socket.socketpair()
    th._accept = lambda: landed              # the peer arrived...
    th._cancel.set()                         # ...as the player cancelled
    try:
        th.run()
        assert not ok.calls and not bad.calls
        assert landed.fileno() == -1, "a cancelled attempt left its socket open"
    finally:
        other.close()


def test_a_pending_join_can_be_cancelled(app):
    th, ok, bad = attempt("join", "127.0.0.1", free_port(), JOIN_TIMEOUT_S=30.0)
    th.start()
    time.sleep(0.1)
    th.cancel()
    assert th.wait(3000), "cancel did not release the retry loop"
    assert not ok.calls and not bad.calls


# --- the way out is actually wired to something ----------------------------
def test_the_link_menu_can_cancel_a_pending_attempt(app):
    """End to end through the real Shell: the 🔗 menu entry -> the signal -> the
    attempt released. This is the whole point of the fix -- the old `cancel()` was
    correct code that nothing could reach."""
    w = shell.Shell()
    try:
        w._start_net("host", "", free_port(), "waiting")
        th = w._net_thread
        th.POLL_S = 0.02
        assert th is not None and w.play.net_pending
        assert wait_until(lambda: th._srv is not None)

        w.play.net_cancel_requested.emit()        # what the menu entry does

        assert w._net_thread is None
        assert not w.play.net_pending, "the menu would still offer to cancel nothing"
        assert th.wait(3000), "the worker was never released"
    finally:
        w.close()


def test_a_second_attempt_is_refused_out_loud(app, monkeypatch):
    """⛔ THE SILENT `return` THIS ENDS: the guard used to swallow every later click
    without a word, which is what made the emulator look dead."""
    from PyQt6.QtWidgets import QMessageBox
    w = shell.Shell()
    asked = []
    monkeypatch.setattr(QMessageBox, "question",
                        lambda *a, **k: asked.append(a) or QMessageBox.StandardButton.No)
    try:
        w._start_net("host", "", free_port(), "waiting")
        th = w._net_thread
        th.POLL_S = 0.02
        started = w._start_net("join", "127.0.0.1", free_port(), "connecting")
        assert asked, "the second attempt died in silence again"
        assert started is False, "the caller must be able to see it did not start"
        assert w._net_thread is th, "answering No must leave the first attempt alone"
    finally:
        w._cancel_net_attempt()
        th.wait(3000)
        w.close()


def test_a_cancelled_worker_is_held_until_it_really_stops(app):
    """⛔ THE CRASH THIS AVOIDS. Dropping the last Python reference to a RUNNING
    QThread destroys the C++ object under the OS thread still inside it, and Qt
    answers with "Destroyed while thread is still running" + terminate(). A cancel
    is not instantaneous -- the join path can sit in a blocking connect -- so the
    worker has to be parked somewhere until its own `finished` arrives.

    The second half is the bug that pairs with it: that late `finished` must not
    clear the bookkeeping of the NEW attempt the player started meanwhile.
    """
    w = shell.Shell()
    try:
        w._start_net("host", "", free_port(), "waiting")
        old = w._net_thread
        old.POLL_S = 0.02
        w._cancel_net_attempt()
        assert old in w._net_retiring, "the worker was dropped while still running"

        w._start_net("host", "", free_port(), "waiting")
        new = w._net_thread
        new.POLL_S = 0.02
        assert new is not old

        w._on_net_thread_finished(old)            # the retired one dies, late
        assert w._net_thread is new, "the new attempt lost its own bookkeeping"
        assert w.play.net_pending, "...and with it the only reference keeping it alive"
        assert old not in w._net_retiring
    finally:
        w._shutdown_net()
        w.close()


def test_a_cancelled_worker_that_connects_late_is_not_attached(app):
    """⚠️ AND THE FLAG THAT ALMOST DID IT. A boolean "this attempt was abandoned"
    is reset by the NEXT attempt -- so a cancelled worker whose `connected` was
    already queued got through after all, and the emulator attached the very link
    the player had taken back. Only the SENDER says which worker is talking.
    """
    w = shell.Shell()
    try:
        w._start_net("host", "", free_port(), "waiting")
        cancelled = w._net_thread
        cancelled.POLL_S = 0.02
        w._cancel_net_attempt()
        w._start_net("host", "", free_port(), "waiting")   # ...and a fresh one
        w._net_thread.POLL_S = 0.02

        landed, other = socket.socketpair()
        try:
            cancelled.connected.emit(landed)               # the late arrival
            assert w.play._net_link is None, "a cancelled attempt got attached"
            assert landed.fileno() == -1, "...and its socket was left open"
        finally:
            other.close()
    finally:
        w._shutdown_net()
        w.close()


def test_cancelling_after_it_already_connected_is_a_no_op(app):
    """⚠️ BOTH WAYS IN RUN A NESTED EVENT LOOP -- `QMessageBox.question` in
    `_start_net`, `QMenu.exec` in the 🔗 menu -- and the worker's queued `connected`
    is delivered inside it like any other. So the peer can arrive between the
    question being put and the answer coming back. Cancelling then must do nothing,
    not wipe the banner, the host panel and the mirror mode of a live session."""
    w = shell.Shell()
    try:
        w.play.overlay.setText("🔗 linked")
        w._mirror_pending = "host"
        assert not w._net_attempt_pending()
        w._cancel_net_attempt()
        assert w.play.overlay.text() == "🔗 linked", "wiped a live session's banner"
        assert w._mirror_pending == "host", "disarmed a mode nobody cancelled"
    finally:
        w._mirror_pending = None
        w.close()


def test_a_connection_landing_onto_a_busy_console_is_refused(app):
    """An attempt can be in flight for minutes, and nothing stops the player wiring a
    local cable meanwhile -- `_one_link_at_a_time` only sees links that already
    exist. Attaching on top would put two relays on one serial FIFO."""
    w = shell.Shell()
    landed, other = socket.socketpair()
    try:
        w.play._link_peer = w.play          # a local cable appeared while we searched
        w._on_net_connected(landed)
        assert w.play._net_link is None, "two relays would fight over one FIFO"
        assert landed.fileno() == -1
    finally:
        w.play._link_peer = None
        other.close()
        w.close()


def test_closing_the_window_leaves_no_worker_running(app):
    w = shell.Shell()
    w._start_net("host", "", free_port(), "waiting")
    th = w._net_thread
    th.POLL_S = 0.02
    try:
        w._shutdown_net()
        assert th.isFinished(), "a running QThread outlived the window"
        assert w._net_thread is None and not w._net_retiring
    finally:
        w.close()


def test_a_refused_mirror_attempt_does_not_arm_the_next_connection(app, monkeypatch):
    """⛔ THE SILENT MODE SWITCH THIS ENDS. `_mirror_pending` says what the NEXT
    connection becomes. Arming it before a start that gets refused left it set over
    an earlier CABLE attempt still in flight, which then connected and opened a
    mirror session instead -- a different mode entirely, chosen by nobody."""
    from PyQt6.QtWidgets import QInputDialog, QMessageBox
    w = shell.Shell()
    monkeypatch.setattr(QMessageBox, "question",
                        lambda *a, **k: QMessageBox.StandardButton.No)
    monkeypatch.setattr(QInputDialog, "getInt", lambda *a, **k: (7789, True))
    monkeypatch.setattr(w, "_one_link_at_a_time", lambda want_mirror: False)
    monkeypatch.setattr(w, "_ask_mirror_delay", lambda: True)
    w.play.machine = object()                 # far enough for these two calls
    try:
        w._start_net("host", "", free_port(), "waiting")   # a CABLE attempt, in flight
        th = w._net_thread
        th.POLL_S = 0.02
        w._host_mirror()                                   # refused: an attempt is running
        assert w._mirror_pending is None, "the cable attempt would have become a mirror"
    finally:
        w._cancel_net_attempt()
        th.wait(3000)
        w.play.machine = None
        w.close()


# --- what the player is told ----------------------------------------------
@pytest.mark.parametrize("exc, mode, key", [
    (socket.gaierror(11001, "getaddrinfo failed"), "join", "net_err_name"),
    (ConnectionRefusedError(10061, "refused"), "join", "net_err_refused"),
    (TimeoutError("timed out"), "join", "net_err_timeout_join"),
    (TimeoutError("timed out"), "host", "net_err_timeout_host"),
    (OSError(98, "in use", None, 10048), "host", "net_err_in_use"),
])
def test_the_failure_names_a_cause_not_an_errno(exc, mode, key):
    import errno
    if key == "net_err_in_use":
        exc = OSError(errno.EADDRINUSE, "address already in use")
    text = shell._net_error_text("en", exc, mode)
    assert cfg.tr("en", key) in text


def test_an_unmapped_failure_still_reports_something():
    """The fallback must keep the raw detail rather than swallow it."""
    text = shell._net_error_text("en", RuntimeError("something odd"), "join")
    assert "something odd" in text


# --- two consoles on one PC move together ----------------------------------
@pytest.fixture
def pair(app):
    """Two pages wired as local 2-player. No ROM: the propagation is decided by
    `_link_peer`, and `machine` only has to be non-None for the peer to count."""
    s = cfg.make_settings()
    p1, p2 = shell.PlayPage(s, None), shell.PlayPage(s, None)
    p1.machine = p2.machine = object()
    p1._link_peer, p2._link_peer = p2, p1
    yield p1, p2
    # ⛔ CES DEUX PAGES TUAIENT LA SUITE, ET PAS ICI.
    #
    # Reprendre (`_toggle_pause` deux fois) rearme `self.timer` a 4 ms. Le test rend
    # la main, mais les deux pages RESTENT VIVANTES : `p1._link_peer is p2` et
    # l'inverse forment un CYCLE, que seul le ramasse-miettes defait, a un moment
    # qu'on ne choisit pas. En attendant, elles tiquent des que quelqu'un fait
    # tourner la boucle d'evenements -- et le premier a le faire longuement est
    # `test_lobby`, qui pompe 5 secondes. La `machine` etant un `object()` postiche,
    # `_tick` leve un AttributeError DANS UN SLOT Qt, ce que PyQt traite par
    # `qFatal()` : SIGABRT sur Linux, 0xC0000409 sur Windows.
    #
    # 🔑 LE PROCESSUS MOURAIT DANS UN TEST QUI N'Y ETAIT POUR RIEN, sans resume, sans
    # nom de test, et seulement quand la suite ENTIERE tourne -- le ramasse-miettes
    # nettoyait le cycle avant la bombe sur tout sous-ensemble plus court.
    #
    # Arreter le minuteur suffit a desamorcer ; couper `_link_peer` rend en plus les
    # deux pages liberables tout de suite. Aucune teardown Qt a la main (pas de
    # `deleteLater()` + `processEvents()`) : voir le docstring de `conftest.py`.
    for page in (p1, p2):
        page.timer.stop()
        page._link_peer = None
        page.machine = None


def test_pausing_one_console_pauses_the_other(pair):
    p1, p2 = pair
    p1._toggle_pause()
    assert p1.paused and p2.paused, "the peer kept running against a stopped partner"
    p1._toggle_pause()
    assert not p1.paused and not p2.paused


def test_the_pause_menu_pauses_the_peer_too(pair):
    p1, p2 = pair
    p1.open_menu()
    assert p2.paused
    p1.close_menu()
    assert not p2.paused


def test_leaving_the_pause_menu_sideways_still_wakes_the_peer(pair):
    """⚠️ THE PATH THAT IS EASY TO MISS. Picking Video/Audio/Controls hides the menu
    WITHOUT going through close_menu -- it stays paused on purpose and comes back
    through resume_play. Pausing the peer on the way in and forgetting it on the way
    out left player 2 frozen for the rest of the session.

    resume_play goes on to repaint and re-open audio, which needs a real console;
    this file boots none, so those two steps are stubbed out. The wake-up under test
    happens before them.
    """
    p1, p2 = pair
    p1.open_menu()
    p1._on_menu_choice("video")               # menu gone, both still paused
    assert p2.paused

    p1.apply_settings = lambda: None
    p1._open_audio = lambda: None
    p1.resume_play()
    assert not p1.paused and not p2.paused


def test_suspending_takes_the_peer_with_it(pair):
    p1, p2 = pair
    p1.suspend()
    assert p1.paused and p2.paused


def test_a_speed_change_applies_to_both(pair):
    p1, p2 = pair
    p1.cycle_speed(True)
    assert p1._speed == 2.0 and p2._speed == 2.0
    p2.cycle_speed(False)                    # ...and it works from either side
    assert p1._speed == 1.0 and p2._speed == 1.0


def test_the_mutual_link_does_not_recurse(pair):
    """Both pages point at each other. If the mirroring lived in the `_apply_*`
    helpers instead of in `_link_both`, this would never return."""
    p1, p2 = pair
    for _ in range(3):
        p1.cycle_speed(True)
    assert p1._speed == p2._speed == 4.0


def test_the_toolbar_toggle_moves_both_buttons(pair):
    p1, p2 = pair
    p1._ff_btn.setChecked(True)              # emits toggled -> _set_ff
    assert p1._ff and p2._ff
    assert p2._ff_btn.isChecked(), "the peer's button lied about its own state"
    p1._ff_btn.setChecked(False)
    assert not p1._ff and not p2._ff


def test_the_held_hotkey_does_not_touch_the_buttons(pair):
    """⚠️ Press/release is temporary. Ticking the button on press would make
    fast-forward STICK when the key came back up."""
    p1, p2 = pair
    p1._begin_fast_forward()
    assert p1._ff and p2._ff
    assert not p1._ff_btn.isChecked() and not p2._ff_btn.isChecked()
    p1._end_fast_forward()
    assert not p1._ff and not p2._ff


def test_a_lone_console_is_unaffected(app):
    s = cfg.make_settings()
    solo = shell.PlayPage(s, None)
    solo.machine = object()
    try:
        solo._toggle_pause()
        solo.cycle_speed(True)
        assert solo.paused and solo._speed == 2.0
    finally:
        solo.timer.stop(); solo.machine = None   # voir la fixture `pair`


def test_mirror_play_is_left_alone(app):
    """🔑 Mirror is self-limiting -- `MirrorSession.step()` waits for the peer's
    input for THIS frame, so neither PC can run ahead whatever its speed dial
    says. The propagation follows `_link_peer` and nothing else."""
    s = cfg.make_settings()
    page = shell.PlayPage(s, None)
    page.machine = object()
    page._mirror = object()
    try:
        page.cycle_speed(True)               # must not raise, must not look for a peer
        assert page._speed == 2.0
    finally:
        page.timer.stop()                    # voir la fixture `pair`
        page.machine = page._mirror = None


@pytest.fixture
def bare_window(app):
    """Keep the shell alive until its real network workers have stopped."""
    from PyQt6.QtCore import QCoreApplication, QEvent

    win = shell.Shell()
    try:
        yield win
    finally:
        workers = list(win._net_retiring)
        if win._net_thread is not None:
            workers.append(win._net_thread)
        # Some tests install a sentinel in place of a running machine.
        win.play.machine = None
        win._cancel_net_attempt()
        win.close()
        assert all(not worker.isRunning() for worker in workers), (
            "a network worker outlived its test window")
        win.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)


# --------------------------------------------------------------------------
# ⛔ « EN ATTENTE DE CONNEXION..... » — LE MESSAGE QUI NE DIT PAS S'IL EST VIVANT
#
# Rapporte par un joueur qui essayait a deux, en LAN puis en ligne: « rien ne change,
# en attente de connexion..... ». En regardant les VRAIES sockets de ses deux instances
# (tools/watch_link_sockets.py): AUCUNE ecoute. Le fil avait rendu la main depuis
# longtemps -- HOST_TIMEOUT_S valait 300 s -- et l'ecran affichait toujours le texte
# pose une seule fois par `_start_net`.
#
# Deux joueurs qui montent une partie depassent cinq minutes sans effort: on clique
# Heberger, on va sur l'autre PC, on charge la ROM, on se met d'accord. Passe le delai,
# le pair qui arrive enfin se prend un refus de connexion pendant que l'hote montre
# encore « en attente ». Les deux ecrans mentent, chacun a sa facon.
#
# Deux choses sont donc gelees ici: le delai n'est plus de l'ordre de la minute, et une
# attente VIVANTE se voit -- son texte avance.

def test_the_waiting_message_counts_so_a_dead_attempt_is_visible(app, monkeypatch, bare_window):
    """Le texte d'attente doit CHANGER tant que la tentative est en vie."""
    win = bare_window
    seen = []
    win.play.overlay.setText = lambda t: seen.append(t)      # type: ignore[assignment]
    monkeypatch.setattr(shell._NetConnect, "HOST_TIMEOUT_S", 30.0)
    try:
        assert win._start_net("host", "", free_port(), "⏳ attente")
        assert seen, "aucun texte pose au demarrage"
        first = seen[-1]
        assert "⏳ attente" in first, first
        assert wait_until(lambda: (app.processEvents(), len(seen) > 1)[1], 3.0), \
            "le texte n'a jamais bouge: rien ne distingue une attente morte"
        assert seen[-1] != first
    finally:
        win._cancel_net_attempt()
        app.processEvents()


def test_the_clock_stops_when_the_attempt_does(app, monkeypatch, bare_window):
    """...et il s'arrete quand elle finit, sinon il recouvrirait le verdict."""
    win = bare_window
    monkeypatch.setattr(shell._NetConnect, "HOST_TIMEOUT_S", 30.0)
    assert win._start_net("host", "", free_port(), "⏳ attente")
    assert win._net_clock is not None
    win._on_net_failed("boom")
    assert win._net_clock is None, "le compteur survit a l'echec et efface le message"
    assert "boom" in win.play.overlay.text()


def test_a_host_waits_far_longer_than_two_players_take_to_get_ready():
    """⛔ LA VALEUR ELLE-MEME EST LE CORRECTIF, pas seulement l'affichage.

    Cinq minutes, c'est le temps de charger une ROM et d'ouvrir Discord. Un hote qui
    cesse d'ecouter pendant que son pair s'installe rend une panne que ni l'un ni
    l'autre ne peut diagnostiquer. La sortie appartient au joueur (`_cancel_net_attempt`
    est dans le menu 🔗), pas a un chronometre.
    """
    assert shell._NetConnect.HOST_TIMEOUT_S >= 900.0
    assert shell._NetConnect.JOIN_TIMEOUT_S >= 900.0


# --------------------------------------------------------------------------
# ⛔ FERMER LA FICHE D'ADRESSE NE DOIT PAS ARRETER D'HEBERGER
#
# `HostInfoDialog` est une FICHE: IP locale, IP publique detectee, ligne a coller,
# bouton « Fermer ». Sa propre classe promet « Non-modal -- hosting keeps running
# behind it ». Le shell, lui, annulait la tentative sur `finished`. Mesure sur un
# `Shell` reel, en regardant la vraie socket:
#
#     clic « Heberger en miroir »       -> ecoute sur le port : OUI
#     fermeture de la fiche d'adresse   -> ecoute sur le port : NON
#
# Un joueur lit son adresse, ferme la fiche pour revoir son jeu, et vient d'arreter
# d'heberger sans qu'un mot le lui dise. La sortie explicite existe deja
# (« Annuler la tentative », menu 🔗); une fiche qu'on ferme dit « j'ai lu ».

def _is_listening(win) -> bool:
    """Est-ce que l'hote ecoute ENCORE ? On le demande au fil qui ecoute.

    ⛔ NE PAS SE CONNECTER POUR LE SAVOIR. Un `connect` reussi EST le pair que l'hote
    attendait : `_accept` le rend et referme l'ecoute. La sonde terminait donc
    l'hebergement, puis constatait qu'il n'ecoutait plus -- un faux positif qui a fait
    accuser la fermeture de la fiche d'adresse.

    ⛔ ET PAS `netstat` NON PLUS. La premiere version lisait la table TCP du systeme avec
    `netstat -ano -p TCP`, qui est de la syntaxe Windows : ailleurs la commande rend une
    sortie vide, aucune ligne ne correspond, et le test echoue en annoncant « l'hote n'a
    jamais ecoute » sur un hote parfaitement vivant. Un banc qui depend du systeme pour
    repondre a une question que l'objet teste connait deja se trompe de source.

    `_NetConnect._srv` porte la socket d'ecoute et n'est renseigne QUE pendant l'attente
    (son `finally` la remet a None en sortant, quelle que soit la sortie).
    """
    th = win._net_thread
    return th is not None and getattr(th, "_srv", None) is not None


def test_closing_the_address_card_keeps_the_host_listening(app, monkeypatch, bare_window):
    from PyQt6.QtWidgets import QInputDialog
    port = free_port()
    win = bare_window
    win.play.machine = object()                      # « un jeu tourne »
    monkeypatch.setattr(QInputDialog, "getInt",
                        staticmethod(lambda *a, **k: (port, True)))
    try:
        win._host_mirror()
        assert wait_until(lambda: (app.processEvents(), _is_listening(win))[1], 5.0), \
            "l'hote n'a jamais ecoute"
        assert win._host_info is not None, "la fiche d'adresse n'a pas ete montree"

        win._host_info.accept()                      # le bouton « Fermer » de la fiche
        # ⚠️ ET ON LAISSE LE TEMPS A UNE ANNULATION D'AGIR. `cancel()` ne ferme pas la
        # socket lui-meme : il pose un drapeau que la boucle d'attente relit toutes les
        # POLL_S. Verifier dans la foulee verrait donc l'ecoute encore ouverte meme avec
        # l'ancien comportement -- un test qui ne peut plus echouer.
        end = time.monotonic() + 4 * shell._NetConnect.POLL_S
        while time.monotonic() < end:
            app.processEvents()
            time.sleep(0.02)
        assert _is_listening(win), "fermer la fiche a arrete l'hebergement"
        assert win._net_attempt_pending(), "la tentative a ete annulee en silence"
    finally:
        win._cancel_net_attempt()
        app.processEvents()


def test_cancelling_stops_the_clock(app, monkeypatch, bare_window):
    """⛔ ET LE COMPTEUR MEURT AVEC LA TENTATIVE. Ajoute pour qu'une attente MORTE se
    voie, il en fabriquait une vivante: annulee, la tentative gardait un compteur qui
    avancait -- exactement l'ecran qu'on essayait de rendre impossible."""
    monkeypatch.setattr(shell._NetConnect, "HOST_TIMEOUT_S", 30.0)
    win = bare_window
    assert win._start_net("host", "", free_port(), "⏳ attente")
    assert win._net_clock is not None
    win._cancel_net_attempt()
    assert win._net_clock is None, "le compteur survit a l'annulation"


def test_joining_your_own_pc_says_so(app, monkeypatch, bare_window):
    """« 127.0.0.1 » est legitime (deux fenetres sur un PC) mais se fait prendre pour
    « l'adresse du serveur qu'on m'a donnee ». On ne la refuse pas, on la NOMME."""
    monkeypatch.setattr(shell._NetConnect, "JOIN_TIMEOUT_S", 30.0)
    win = bare_window
    seen = []
    win.play.overlay.setText = lambda t: seen.append(t)   # type: ignore[assignment]
    assert win._start_net("join", "127.0.0.1", free_port(), "⏳ connexion")
    try:
        assert wait_until(lambda: (app.processEvents(),
                                   any("CE PC" in t or "THIS PC" in t
                                       for t in seen))[1], 12.0), \
            "rien ne dit jamais que 127.0.0.1 designe cette machine"
    finally:
        win._cancel_net_attempt()
        app.processEvents()
