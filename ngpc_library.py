"""Per-ROM library stats: what you played, when, and how long.

The shell knew a ROM only as a path on disk, so it could offer exactly one
ordering (alphabetical) and no filtering. "Most played" / "last played" are not
sort modes you can bolt onto a file listing -- they are DATA nothing was
recording. This module is that data.

One JSON file (`library.json`, beside `thumbnails/` and `saves/`), keyed on the
ROM's full path -- the same choice `_cover_path` makes in the shell, and for the
same reason: two projects can each hold a `main.ngc`, and a stem-only key would
silently merge their play counts.

Nothing here imports Qt: the store is plain data so the thumbnail thread and the
player can both touch it, and so it can be unit-tested without a UI.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

# -- sort keys. The UI shows these in this order; the ids are persisted, so
# renaming one orphans the saved preference (it falls back to SORT_NAME).
SORT_NAME = "name"       # A->Z on the displayed title
SORT_LAST = "last"       # last played
SORT_PLAYS = "plays"     # number of launches
SORT_TIME = "time"       # total playtime
SORT_ADDED = "added"     # file mtime -- "recently added to the collection"
SORT_SIZE = "size"       # ROM size on disk
SORT_KEYS = (SORT_NAME, SORT_LAST, SORT_PLAYS, SORT_TIME, SORT_ADDED, SORT_SIZE)

# -- filters
FILTER_ALL = "all"
FILTER_FAV = "fav"
FILTER_NEVER = "never"   # never launched -- "what have I not tried yet?"
FILTERS = (FILTER_ALL, FILTER_FAV, FILTER_NEVER)

_EMPTY: dict = {"plays": 0, "last": 0.0, "time": 0.0, "fav": False}


class Library:
    """The play-history store. Load once, mutate, `save()` when it matters.

    Every write is committed immediately (these are a handful of counters, and a
    crash mid-session must not lose the fact that you played something), but the
    write is atomic -- a torn `library.json` would lose the whole history.
    """

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._data: dict[str, dict] = {}
        self.load()

    # ---- persistence ----------------------------------------------------
    def load(self) -> None:
        try:
            raw = json.loads(self.path.read_text("utf-8"))
        except (OSError, ValueError):
            return
        if isinstance(raw, dict) and isinstance(raw.get("games"), dict):
            # Keep only well-formed entries: a hand-edited or half-written file
            # must not take the library down with it.
            for key, ent in raw["games"].items():
                if isinstance(ent, dict):
                    self._data[str(key)] = {**_EMPTY, **ent}

    def save(self) -> None:
        payload = {"version": 1, "games": self._data}
        tmp = self.path.with_suffix(".json.tmp")
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_text(json.dumps(payload, indent=1), "utf-8")
            os.replace(tmp, self.path)      # atomic: never a truncated library
        except OSError:
            pass

    # ---- reads ----------------------------------------------------------
    @staticmethod
    def _key(rom: Path) -> str:
        return str(Path(rom))

    def entry(self, rom: Path) -> dict:
        return self._data.get(self._key(rom), _EMPTY)

    def plays(self, rom: Path) -> int:
        return int(self.entry(rom).get("plays", 0))

    def playtime(self, rom: Path) -> float:
        return float(self.entry(rom).get("time", 0.0))

    def last_played(self, rom: Path) -> float:
        return float(self.entry(rom).get("last", 0.0))

    def is_favorite(self, rom: Path) -> bool:
        return bool(self.entry(rom).get("fav", False))

    # ---- writes ---------------------------------------------------------
    def _mut(self, rom: Path) -> dict:
        key = self._key(rom)
        ent = self._data.get(key)
        if ent is None:
            ent = dict(_EMPTY)
            self._data[key] = ent
        return ent

    def note_launch(self, rom: Path) -> None:
        """A game just started: one more play, and it becomes the most recent."""
        ent = self._mut(rom)
        ent["plays"] = int(ent.get("plays", 0)) + 1
        ent["last"] = time.time()
        self.save()

    def add_playtime(self, rom: Path, seconds: float) -> None:
        """Accumulate real seconds spent in-game. Sub-second slivers are dropped:
        a launch that fails instantly should not register as a play session."""
        if seconds < 1.0:
            return
        ent = self._mut(rom)
        ent["time"] = float(ent.get("time", 0.0)) + float(seconds)
        self.save()

    def toggle_favorite(self, rom: Path) -> bool:
        ent = self._mut(rom)
        ent["fav"] = not bool(ent.get("fav", False))
        self.save()
        return bool(ent["fav"])

    def forget(self, rom: Path) -> None:
        self._data.pop(self._key(rom), None)
        self.save()

    # ---- the actual ordering --------------------------------------------
    def arrange(self, roms: list[Path], key: str, reverse: bool, filt: str,
                query: str, title_of) -> list[Path]:
        """Filter, search and sort in one pass.

        Each key already sorts the way you actually want it: A->Z for the name,
        and biggest-first for every statistic (most played, most recent, longest
        session). `reverse` flips that -- which is where "least played" lives.
        One key plus a direction, rather than a menu entry per combination.

        `title_of` is the shell's display-name function (it trims the "(USA)"
        dump tags), so the A->Z order and the search match what is actually
        printed on the cards rather than the raw filename.
        """
        out = list(roms)

        if filt == FILTER_FAV:
            out = [r for r in out if self.is_favorite(r)]
        elif filt == FILTER_NEVER:
            out = [r for r in out if self.plays(r) == 0]

        needle = (query or "").strip().casefold()
        if needle:
            # Match the pretty title AND the filename: a search for "usa" should
            # still find a ROM whose tag the title strips off.
            out = [r for r in out
                   if needle in title_of(r.stem).casefold() or needle in r.name.casefold()]

        def sort_key(rom: Path):
            if key == SORT_LAST:
                return self.last_played(rom)
            if key == SORT_PLAYS:
                return self.plays(rom)
            if key == SORT_TIME:
                return self.playtime(rom)
            if key == SORT_SIZE:
                return _stat(rom, "st_size")
            if key == SORT_ADDED:
                return _stat(rom, "st_mtime")
            return 0

        # Alphabetical is the tiebreak for every mode, so ROMs that share a value
        # (all the never-played ones, all with 0 plays) stay in a stable, sane
        # order instead of shuffling with the filesystem's listing order.
        out.sort(key=lambda r: (title_of(r.stem).casefold(), str(r)))
        if key != SORT_NAME:
            out.sort(key=sort_key, reverse=True)
        if reverse:
            out.reverse()
        return out


def _stat(rom: Path, field: str) -> float:
    try:
        return float(getattr(rom.stat(), field))
    except OSError:
        return 0.0


def format_playtime(seconds: float) -> str:
    """Compact playtime for a card subtitle: '—', '12 min', '3 h 04'."""
    s = int(seconds)
    if s < 60:
        return "—"
    if s < 3600:
        return f"{s // 60} min"
    return f"{s // 3600} h {(s % 3600) // 60:02d}"


def format_last(when: float, lang: str = "en") -> str:
    """Relative 'last played', short enough for a card. Deliberately terse and
    unit-only ("5 min", "3 h", "12 j") so it needs no sentence to translate --
    only the day letter differs between the two languages we ship."""
    if not when:
        return ""
    delta = time.time() - when
    if delta < 300:
        return "· · ·"                       # minutes ago; no number worth printing
    if delta < 3600:
        return f"{int(delta // 60)} min"
    if delta < 86400:
        return f"{int(delta // 3600)} h"
    days = int(delta // 86400)
    if days < 30:
        return f"{days} {'j' if lang == 'fr' else 'd'}"
    return time.strftime("%Y-%m-%d", time.localtime(when))
