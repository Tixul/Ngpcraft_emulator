"""NGPC <-> Dreamcast cable through Flycast's BattleCable null modem (core.link.FlycastLink).

The far end here is a plain UDP socket speaking Flycast's framing
(core/network/null_modem.h): b"D" + byte per data byte, b"B" for a break.
Validated for real on 2026-11-03: Match of the Millennium sent its VERSUS points to
Capcom vs. SNK Millennium Fight 2000 running in Flycast 2.7 ("データの受信を完了しました").
The game exchange it carried: NGPC `FF 00 31 31 00 .. FF`, Dreamcast `FF 00 10 00 .. FF`.
"""

from __future__ import annotations

import socket
import time

from core import link


class FakeMachine:
    """Just enough of the serial ABI for the link -- no core needed."""

    def __init__(self) -> None:
        self._tx = bytearray()
        self.rx = bytearray()
        self.enabled = False
        self.cts_high = None

    def serial_set_enabled(self, on: bool) -> None:
        self.enabled = bool(on)

    def serial_read_tx(self, n: int = 64) -> bytes:
        out = bytes(self._tx[:n])
        del self._tx[:n]
        return out

    def serial_write_rx(self, data: bytes) -> None:
        self.rx += data

    def serial_set_cts(self, high: bool) -> None:
        self.cts_high = high


def _flycast_side():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.bind(("127.0.0.1", 0))
    s.settimeout(2.0)
    return s


def _free_udp_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def test_bytes_cross_in_flycast_framing_both_ways():
    fly = _flycast_side()
    m = FakeMachine()
    port = _free_udp_port()
    lk = link.FlycastLink(m, peer=fly.getsockname(), local_port=port, bind_host="127.0.0.1")
    try:
        assert m.enabled and m.cts_high is False      # peer declared present
        # NGPC -> Dreamcast: one 2-byte datagram per byte, in order.
        frame = bytes([0xFF, 0x00, 0x31, 0x31, 0x00, 0xFF])
        m._tx += frame
        lk.pump()
        got = [fly.recvfrom(16)[0] for _ in frame]
        assert got == [b"D" + bytes([b]) for b in frame]
        assert lk.bytes_out == len(frame)
        # Dreamcast -> NGPC: data delivered, a break counted and dropped.
        for b in (0xFF, 0x00, 0x10):
            fly.sendto(b"D" + bytes([b]), ("127.0.0.1", port))
        fly.sendto(b"B", ("127.0.0.1", port))
        deadline = time.time() + 2
        while len(m.rx) < 3 and time.time() < deadline:
            lk.pump()
            time.sleep(0.01)
        assert bytes(m.rx) == bytes([0xFF, 0x00, 0x10])
        assert lk.bytes_in == 3 and lk.breaks_in == 1
        assert lk.heard_from == fly.getsockname()
    finally:
        lk.disconnect()
        fly.close()
    assert not m.enabled


def test_a_flycast_not_started_yet_is_not_a_lost_cable():
    # Windows answers a datagram to a closed port with an ICMP "port unreachable",
    # surfaced as ConnectionResetError on the NEXT socket call. Flycast is often
    # launched after the NGPC: that must not end the link.
    m = FakeMachine()
    lk = link.FlycastLink(m, peer=("127.0.0.1", _free_udp_port()), local_port=_free_udp_port(),
                          bind_host="127.0.0.1")
    try:
        for _ in range(5):
            m._tx += b"\x92"
            lk.pump()
            time.sleep(0.01)
        assert lk.lost is None
        assert lk.bytes_out == 5
    finally:
        lk.disconnect()
