"""The online lobby dialog: pseudo, browse open games, create / join.

Sits on top of core.lobby.LobbyClient. When a pairing succeeds it emits `linked`
with the connected client, and the shell wires it to the running console (its
serial bytes then relay through the server). The player must be running a
compatible game -- the list shows each game's title for that reason.
"""

from __future__ import annotations

from PyQt6.QtCore import Qt, QObject, pyqtSignal, pyqtSlot
from PyQt6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QFormLayout, QLabel, QLineEdit,
    QPushButton, QListWidget, QListWidgetItem, QCheckBox, QInputDialog,
    QMessageBox, QApplication, QTextEdit,
)

import ngpc_settings as cfg
from core.lobby import LobbyClient

DEFAULT_PORT = 7788


def _lan_ip() -> str:
    import socket
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


def _other_local_ips(primary: str) -> list[str]:
    """Les AUTRES adresses IPv4 de cette machine, celle de la route par defaut exclue.

    ⛔ POURQUOI CE N'EST PAS DU DETAIL. `_lan_ip()` rend l'adresse de l'interface qui
    sort vers internet -- le 192.168.x de la box. Or la seule facon gratuite et
    permanente de jouer a distance sans toucher a sa box, c'est un reseau prive virtuel
    (Tailscale, ZeroTier): il ajoute une interface avec SA propre adresse, et c'est
    celle-la que le pair doit composer. Elle ne passe pas par la route par defaut, donc
    la fiche ne l'affichait pas -- elle envoyait le joueur sur l'adresse qui ne marche
    justement que chez lui.

    Le tri met les plages de reseau virtuel en premier (100.64/10 chez Tailscale,
    10.147/16 chez ZeroTier tombent dans le prive classique), puis le reste.
    """
    import socket
    found: set[str] = set()
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = info[4][0]
            if ip and not ip.startswith("127."):
                found.add(ip)
    except OSError:
        pass
    found.discard(primary)

    def rank(ip: str) -> tuple[int, str]:
        # 100.64.0.0/10 -- la plage que Tailscale distribue.
        first, second = (ip.split(".") + ["0", "0"])[:2]
        virtual = first == "100" and 64 <= int(second or 0) <= 127
        return (0 if virtual else 1, ip)

    return sorted(found, key=rank)


def _public_ip(timeout: float = 4.0) -> str:
    """Best-effort public IP (what a peer over the internet dials), via a couple
    of tiny plain-text services. Empty string if none answer."""
    import urllib.request
    for url in ("https://api.ipify.org", "https://ifconfig.me/ip",
                "https://icanhazip.com"):
        try:
            with urllib.request.urlopen(url, timeout=timeout) as r:
                ip = r.read().decode("utf-8", "ignore").strip()
                if ip and len(ip) < 64 and ip.count(".") == 3:
                    return ip
        except Exception:
            continue
    return ""


class _PublicIPProbe(QObject):
    """Ou arrive la reponse de `_public_ip()`. UN SEUL pour toute la session.

    ⛔ C'ETAIT UN QThread ENFANT DE LA FICHE, ET IL TUAIT LE PROCESSUS. La recherche
    interroge jusqu'a trois services, 4 s chacun. Si la fiche est detruite avant la
    reponse (fenetre fermee, application qui quitte, test qui demonte son `Shell`), Qt
    detruit avec elle un QThread encore dans `urlopen` -- mort nette, sans trace. Sur le
    runner Windows de la CI, ou ces services repondent lentement, la suite mourait a
    ~65 % dans `test_closing_the_address_card_keeps_the_host_listening`; Linux et macOS
    avaient leur reponse avant le demontage. Reproduit en local en ralentissant
    `_public_ip` a 3 s.

    Le travail reseau part donc dans un fil Python `daemon` qui n'appartient a aucun
    widget (rien a detruire sous lui, et il ne retient pas la sortie). Il ne touche
    qu'a cet objet-ci, qui vit autant que le module: jamais detruit sous un fil qui
    emet. La fiche s'y branche par un vrai slot Qt, que Qt debranche seul quand la
    fiche disparait -- une reponse tardive ne tombe alors sur personne.
    """
    got = pyqtSignal(str)


_probe_hub: _PublicIPProbe | None = None


def _start_public_ip_probe(slot) -> None:
    """Lance la recherche d'IP publique; `slot` recoit la reponse dans le fil Qt."""
    import threading
    global _probe_hub
    if _probe_hub is None:
        _probe_hub = _PublicIPProbe()          # cree dans le fil principal, sans parent
    hub = _probe_hub
    hub.got.connect(slot)
    threading.Thread(target=lambda: hub.got.emit(_public_ip()),
                     name="public-ip", daemon=True).start()


class HostInfoDialog(QDialog):
    """Shown when you host a DIRECT game: your ready-to-paste Discord line (with
    your public IP auto-detected) plus an honest explainer of port-forwarding and
    its risks. Non-modal -- hosting keeps running behind it."""

    def __init__(self, game_name: str, port: int, lang: str = "en", parent=None):
        super().__init__(parent)
        self._game = game_name or "?"
        self._port = int(port)
        # ⚠️ This dialog used to carry its French and its English side by side in the
        # code, picked by a boolean. Nobody but us could translate it, and every other
        # language got the English. It goes through the lang files like everything else.
        self._lang = lang if isinstance(lang, str) else "en"
        self._pub = ""
        self.setWindowTitle(self._t("host_title"))
        self.setMinimumWidth(560)

        v = QVBoxLayout(self)
        lan = _lan_ip()

        # Prominent, unmissable framing: this mode is not for posting to strangers.
        warn = QLabel(self._t("host_warning"))
        warn.setTextFormat(Qt.TextFormat.RichText); warn.setWordWrap(True)
        warn.setStyleSheet("background:#5a1f1f; color:#ffd9d9; padding:8px; border-radius:6px;")
        v.addWidget(warn)

        h = QLabel(self._t("host_port").format(port=port))
        h.setTextFormat(Qt.TextFormat.RichText)
        v.addWidget(h)
        v.addWidget(QLabel(self._t("host_lan").format(addr=f"{lan}:{port}")))
        others = _other_local_ips(lan)
        if others:
            alt = QLabel(self._t("host_other_addrs").format(
                addrs=", ".join(f"{ip}:{port}" for ip in others[:4])))
            alt.setWordWrap(True)
            v.addWidget(alt)
        self._pub_lbl = QLabel(self._t("host_wan_detecting"))
        self._pub_lbl.setTextFormat(Qt.TextFormat.RichText)
        v.addWidget(self._pub_lbl)

        v.addWidget(QLabel(self._t("host_share")))
        self._paste = QLineEdit(); self._paste.setReadOnly(True)
        self._paste.setText(self._line(lan))       # LAN line until public IP arrives
        v.addWidget(self._paste)
        copy = QPushButton(self._t("host_copy"))
        copy.clicked.connect(self._copy)
        v.addWidget(copy)

        risks = QTextEdit(); risks.setReadOnly(True); risks.setMinimumHeight(230)
        risks.setHtml(self._help_html())
        v.addWidget(risks)

        close = QPushButton(self._t("close"))
        close.clicked.connect(self.accept)
        v.addWidget(close)

        _start_public_ip_probe(self._on_public_ip)

    def _t(self, key: str) -> str:
        return cfg.tr(self._lang, key)

    def _line(self, ip: str) -> str:
        return self._t("host_share_line").format(game=self._game,
                                                 addr=f"{ip}:{self._port}")

    @pyqtSlot(str)
    def _on_public_ip(self, ip: str) -> None:
        self._pub = ip
        if ip:
            self._pub_lbl.setText(
                self._t("host_wan").format(addr=f"{ip}:{self._port}"))
            self._paste.setText(self._line(ip))
        else:
            self._pub_lbl.setText(self._t("host_wan_none"))

    def _copy(self) -> None:
        QApplication.clipboard().setText(self._paste.text())

    def _help_html(self) -> str:
        """The port-forwarding explainer and its risks. One text, translated like the
        rest -- it used to exist twice in this file, once per language."""
        return self._t("host_help_html").format(port=self._port, lan=_lan_ip())


class LobbyDialog(QDialog):
    # The client, plus the room's `joined` record -- which carries WHICH LINK the
    # room is for (cable or mirror) and, for a mirror, the host's input delay. The
    # relay is the same pipe either way; only the meaning of the bytes differs.
    linked = pyqtSignal(object, dict)  # the connected LobbyClient, once paired

    def __init__(self, settings, game_name: str, parent=None):
        super().__init__(parent)
        self._lang = cfg.language(settings)
        self.setWindowTitle(self._t("lobby_title"))
        self.setMinimumSize(460, 420)
        self._settings = settings
        self._game_name = game_name or "?"
        self._client: LobbyClient | None = None
        self._hosting = False

        v = QVBoxLayout(self)

        # --- connection row -------------------------------------------------
        form = QFormLayout()
        self._pseudo = QLineEdit(str(settings.value("online/pseudo", "", type=str)))
        self._pseudo.setPlaceholderText(self._t("lobby_nick_hint"))
        self._server = QLineEdit(str(settings.value("online/server", "", type=str)))
        self._server.setPlaceholderText(self._t("lobby_server_hint"))
        form.addRow(self._t("lobby_nick"), self._pseudo)
        form.addRow(self._t("lobby_server"), self._server)
        v.addLayout(form)

        self._connect_btn = QPushButton(self._t("lobby_connect"))
        self._connect_btn.clicked.connect(self._toggle_connect)
        v.addWidget(self._connect_btn)

        self._status = QLabel(self._t("lobby_offline"))
        self._status.setWordWrap(True)
        v.addWidget(self._status)

        v.addWidget(QLabel(self._t("lobby_open_games").format(game=self._game_name)))
        self._list = QListWidget()
        self._list.itemDoubleClicked.connect(lambda _i: self._join())
        v.addWidget(self._list, 1)

        row = QHBoxLayout()
        self._refresh_btn = QPushButton(self._t("lobby_refresh"))
        self._refresh_btn.clicked.connect(lambda: self._client and self._client.refresh())
        self._create_btn = QPushButton(self._t("lobby_create"))
        self._create_btn.clicked.connect(self._create)
        self._join_btn = QPushButton(self._t("lobby_join"))
        self._join_btn.clicked.connect(self._join)
        for b in (self._refresh_btn, self._create_btn, self._join_btn):
            b.setEnabled(False)
            row.addWidget(b)
        v.addLayout(row)

        close = QPushButton(self._t("close"))
        close.clicked.connect(self.reject)
        v.addWidget(close)

    def _t(self, key: str) -> str:
        return cfg.tr(self._lang, key)

    # ---- connection --------------------------------------------------------
    def _toggle_connect(self) -> None:
        if self._client is not None:
            self._teardown_client()
            self._status.setText(self._t("lobby_offline"))
            self._connect_btn.setText(self._t("lobby_connect"))
            self._enable_actions(False)
            return
        pseudo = self._pseudo.text().strip() or self._t("player")
        addr = self._server.text().strip()
        if not addr:
            QMessageBox.warning(self, self._t("lobby_server"),
                                self._t("lobby_server_missing"))
            return
        host, _, p = addr.partition(":")
        port = int(p) if p.strip().isdigit() else DEFAULT_PORT
        self._settings.setValue("online/pseudo", pseudo)
        self._settings.setValue("online/server", addr)

        self._client = LobbyClient(host.strip(), port, pseudo)
        self._client.connected.connect(self._on_connected)
        self._client.disconnected.connect(self._on_disconnected)
        self._client.game_list.connect(self._on_game_list)
        self._client.created.connect(self._on_created)
        self._client.joined.connect(self._on_joined)
        self._client.error.connect(self._on_error)
        self._status.setText(self._t("lobby_connecting"))
        self._connect_btn.setText(self._t("lobby_disconnect"))
        self._client.start()

    def _teardown_client(self) -> None:
        if self._client is not None:
            try:
                self._client.close()
            except Exception:
                pass
            self._client = None
        self._hosting = False

    def _enable_actions(self, on: bool) -> None:
        for b in (self._refresh_btn, self._create_btn, self._join_btn):
            b.setEnabled(on)

    # ---- client signals ----------------------------------------------------
    def _on_connected(self) -> None:
        self._status.setText(self._t("lobby_connected"))
        self._enable_actions(True)
        self._client.refresh()

    def _on_disconnected(self, reason: str) -> None:
        self._status.setText(self._t("lobby_disconnected").format(why=reason))
        self._connect_btn.setText(self._t("lobby_connect"))
        self._enable_actions(False)
        self._client = None

    def _on_game_list(self, games: list) -> None:
        self._list.clear()
        for g in games:
            lock = " 🔒" if g.get("private") else ""
            # A room made by an older client advertises no mode, and that means cable.
            badge = "🪞" if g.get("mode") == "mirror" else "🔗"
            item = QListWidgetItem(
                f"{badge} {g.get('name','?')}{lock}   —   {g.get('game','?')}   "
                f"(by {g.get('creator','?')})")
            item.setData(Qt.ItemDataRole.UserRole, g)
            self._list.addItem(item)
        if not games:
            self._list.addItem(self._t("lobby_no_games"))

    def _on_created(self, room: str) -> None:
        self._hosting = True
        self._status.setText(self._t("lobby_hosting").format(room=room))

    def _on_joined(self, info: dict) -> None:
        # paired: hand the live client to the shell and close
        self.linked.emit(self._client, dict(info))
        self._client = None          # ownership moves to the shell
        self.accept()

    def _on_error(self, msg: str) -> None:
        QMessageBox.warning(self, self._t("lobby_title"), msg)

    # ---- actions -----------------------------------------------------------
    def _create(self) -> None:
        if self._client is None:
            return
        name, ok = QInputDialog.getText(self, self._t("lobby_create"),
                                        self._t("lobby_create_name"))
        if not ok or not name.strip():
            return
        private = QMessageBox.question(
            self, self._t("lobby_private_title"),
            self._t("lobby_private_ask")) == QMessageBox.StandardButton.Yes
        password = ""
        if private:
            password, ok = QInputDialog.getText(
                self, self._t("lobby_password"), self._t("lobby_password_set"),
                QLineEdit.EchoMode.Password)
            if not ok:
                return
        # WHICH LINK this room is for. Kept as a question rather than a setting: it
        # is a property of the game being hosted, not of the player.
        mirror = QMessageBox.question(
            self, self._t("lobby_mode_title"),
            self._t("lobby_mode_ask")) == QMessageBox.StandardButton.Yes
        self._client.create(name.strip(), self._game_name,
                            public=not private, password=password,
                            mode="mirror" if mirror else "cable",
                            delay=cfg.mirror_delay(self._settings))

    def _join(self) -> None:
        if self._client is None:
            return
        item = self._list.currentItem()
        g = item.data(Qt.ItemDataRole.UserRole) if item else None
        if not isinstance(g, dict):
            return
        password = ""
        if g.get("private"):
            password, ok = QInputDialog.getText(
                self, self._t("lobby_password"), self._t("lobby_password_ask"),
                QLineEdit.EchoMode.Password)
            if not ok:
                return
        self._client.join(g["room"], password)

    def reject(self) -> None:
        self._teardown_client()
        super().reject()
