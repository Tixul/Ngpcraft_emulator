"""🔌 CROSSPLAY: the NgpCraft DESKTOP emulator and this libretro core, one cable.

⚡ WHAT THIS PROVES, and why it is the only test that can. Everything else in `tests/`
drives this core against itself, so it would pass just as happily on a private protocol
this port invented. Here the OTHER console is the desktop emulator's own Python stack --
`core.native.NativeMachine` relayed by `core.link.TcpLink`, the exact code a player uses
when they pick "Héberger (adresse directe)". If the wire drifts by one byte, one header,
one flow-control rule, this fails.

And the assertion is the CARTRIDGE's, not the socket's: both sides run `link_probe.ngc`,
which transmits its own controller byte through the BIOS COM routines and records what
came back (`g_last_rx` @ 0x400A, `g_rx_total` @ 0x400C). Each side must end up holding
THE OTHER SIDE'S byte. A relay that moved bytes the CPU never saw cannot pass that.

Run it directly -- it is not a pytest file, because it needs the desktop repo rather than
this one:

    python tests/crossplay_desktop.py --smoke build-check/ngpcraft_libretro_smoke.exe

The desktop repo is found via $NGPCRAFT_EMULATOR_REPO, or the default below.
"""

from __future__ import annotations

import argparse
import os
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

DEFAULT_REPO = Path(r"C:\Users\wilfr\Documents\GitHub\Ngpcraft_emulator")

G_LAST_RX = 0x400A
G_RX_TOTAL = 0x400C

# The two controller bytes. Distinct, non-zero, and each side asserts it received the
# OTHER one -- so a loopback (a relay wired back to its own console, the failure that
# looks exactly like success on a byte counter) fails instead of passing.
PAD_DESKTOP = 0x11
PAD_LIBRETRO = 0x22


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--smoke", required=True, help="path to ngpcraft_libretro_smoke")
    ap.add_argument("--repo", default=os.environ.get("NGPCRAFT_EMULATOR_REPO"),
                    help="the NgpCraft desktop emulator repository")
    ap.add_argument("--frames", type=int, default=600)
    # ⚡ BOTH DIRECTIONS MATTER. Hosting and joining are different code on both sides --
    # accept() against a non-blocking connect() -- and "it works when the desktop hosts"
    # is not the same statement as "the two front ends cross-play".
    ap.add_argument("--role", choices=("join", "host"), default="join",
                    help="what the LIBRETRO side does; the desktop takes the other seat")
    args = ap.parse_args()

    repo = Path(args.repo) if args.repo else DEFAULT_REPO
    rom = repo / "tests" / "roms" / "link_probe.ngc"
    if not repo.is_dir() or not rom.is_file():
        print(f"SKIP: no desktop emulator repo at {repo}")
        return 0
    sys.path.insert(0, str(repo))
    try:
        from core.link import TcpLink
        from core.native_session import NativeSession
    except ImportError as exc:            # the desktop's own deps, not ours
        print(f"SKIP: cannot import the desktop core ({exc})")
        return 0

    bios = next((p for p in (repo / "bios.bin", repo / "ngpc_bios.bin") if p.is_file()),
                None)
    port = free_port()

    # The libretro side JOINS, so the desktop side is listening before it starts. Its
    # peer address goes where the core reads it from: a config file in the system
    # directory, which is also the mechanism a real player uses.
    with tempfile.TemporaryDirectory() as system_dir:
        Path(system_dir, "ngpcraft_link.cfg").write_text(
            f"host = 127.0.0.1\nport = {port}\n", encoding="utf-8")

        command = [args.smoke, str(rom), f"--link={args.role}",
                   f"--link-frames={args.frames}",
                   f"--link-pad={PAD_LIBRETRO:02X}",
                   f"--link-expect={PAD_DESKTOP:02X}",
                   f"--system-dir={system_dir}"]

        if args.role == "join":
            server = socket.socket()
            server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            server.bind(("127.0.0.1", port))
            server.listen(1)
            child = subprocess.Popen(command, stdout=subprocess.PIPE,
                                     stderr=subprocess.STDOUT, text=True)
            server.settimeout(15.0)
            try:
                conn, _ = server.accept()
            except socket.timeout:
                child.kill()
                print("FAIL: the libretro core never connected")
                return 1
            finally:
                server.close()
        else:
            # The core listens on the port from its own config file, so the desktop
            # dials in. Its listener is up by the time the first frame runs, but the
            # process still has to start: retry rather than assume.
            child = subprocess.Popen(command, stdout=subprocess.PIPE,
                                     stderr=subprocess.STDOUT, text=True)
            conn = None
            deadline = time.monotonic() + 15.0
            while conn is None and time.monotonic() < deadline:
                try:
                    conn = socket.create_connection(("127.0.0.1", port), timeout=1.0)
                except OSError:
                    time.sleep(0.05)
            if conn is None:
                child.kill()
                print("FAIL: the libretro core never accepted a connection")
                return 1
            conn.setblocking(True)
        conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)

        session = NativeSession(rom, bios_path=bios, autosave=False)
        link = TcpLink(session.machine, conn)
        # The desktop relays the cable every CABLE_SLICE instructions rather than once a
        # frame, and so does the core under test -- a byte that waits for the end of a
        # frame arrives a frame late, in one direction, always.
        from core.link import CABLE_SLICE
        for _ in range(args.frames):
            session.machine.write(0x00B0, bytes([PAD_DESKTOP]))
            start = session.machine.run(0, record=False)[0].frame_count
            for _ in range(256):
                summ, _ = session.machine.run(CABLE_SLICE, record=False)
                link.pump()
                if summ.executed == 0 or summ.frame_count != start:
                    break
            else:
                session.machine.run_frames(1)
                link.pump()
            if link.lost:
                break

        deadline = time.monotonic() + 30.0
        while child.poll() is None and time.monotonic() < deadline:
            link.pump()
            time.sleep(0.01)
        if child.poll() is None:
            child.kill()
            print("FAIL: the libretro core did not finish")
            return 1
        child_output = child.stdout.read() if child.stdout else ""

        last_rx = session.machine.read(G_LAST_RX, 1)[0]
        total = session.machine.read(G_RX_TOTAL, 2)
        rx_total = total[0] | (total[1] << 8)
        print(f"desktop  last_rx={last_rx:02X} rx_total={rx_total} "
              f"out={link.bytes_out} in={link.bytes_in} lost={link.lost}")
        print("libretro " + child_output.strip())

        ok = True
        if child.returncode != 0:
            print("FAIL: the libretro side rejected the session")
            ok = False
        if rx_total == 0:
            print("FAIL: the desktop cartridge received nothing")
            ok = False
        elif last_rx != PAD_LIBRETRO:
            print(f"FAIL: the desktop game expected {PAD_LIBRETRO:02X}, got {last_rx:02X}")
            ok = False
        print("CROSSPLAY OK" if ok else "CROSSPLAY FAILED")
        return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
