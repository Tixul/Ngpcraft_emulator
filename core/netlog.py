"""netplay.log: what an online attempt did, step by step -- the file a player can send.

A failed link used to leave nothing behind but a socket error number on one screen and,
often, the real reason on the OTHER screen. This writes one line per step -- connecting,
connected, the hello each side announced, a refusal and its reason, a lost connection
with the raw system text -- next to the emulator (logs/netplay.log), so "it fails every
time" can be answered by reading, not by guessing.

Never raises: a log that can break netplay is worse than no log. Rotates at 1 MiB, one
old file kept. Tests point it elsewhere with NGPCRAFT_NETLOG (the root conftest does).
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

ENV = "NGPCRAFT_NETLOG"
MAX_BYTES = 1024 * 1024


def path() -> Path:
    env = os.environ.get(ENV)
    if env:
        return Path(env)
    from ngpc_paths import data_root
    return data_root() / "logs" / "netplay.log"



def log(event: str, **fields) -> None:
    try:
        p = path()
        p.parent.mkdir(parents=True, exist_ok=True)
        if p.exists() and p.stat().st_size > MAX_BYTES:
            os.replace(p, p.with_suffix(".log.1"))
        stamp = time.strftime("%Y-%m-%d %H:%M:%S") + f".{int(time.time() * 1000) % 1000:03d}"
        parts = " ".join(f"{k}={_clean(v)}" for k, v in fields.items())
        with open(p, "a", encoding="utf-8") as f:
            f.write(f"{stamp} {event} {parts}".rstrip() + "\n")
    except Exception:  # noqa: BLE001 -- see the module note
        pass


def _clean(v) -> str:
    s = str(v).replace("\n", " ").replace("\r", " ")
    return f'"{s}"' if " " in s else s
