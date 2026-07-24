# -*- coding: utf-8 -*-
"""Character-table text tools for fan-translation, working on ANY ROM.

Nothing here is specific to one game: the byte<->character mapping is loaded from a
user `.tbl` file (the romhacking-standard format), and every function takes raw bytes
so it reads the emulator's live memory the same whether that is work RAM or cartridge
ROM. The GUI (`ngpc_debug` "Text" tab) is a thin shell over this; keeping the logic
here means it is pure Python and unit-tested with no Qt and no emulator.

The `.tbl` format understood (a practical subset of the romhacking.net spec):

    ; a comment; blank lines are ignored
    8A=A            one byte  -> a string
    819A=abc        two bytes -> a string (any even number of hex digits works)
    FF00=\n         \n and \\ are honoured in the value, so a line-break byte maps
                    to a real newline
    /FF=<end>       a leading '/' marks a STRING TERMINATOR; the value after '=' is an
                    optional label and is ignored

Decoding is longest-match first, so a multi-byte entry always beats the one-byte
entries that start it. A byte no entry covers is shown as `[XX]`, never dropped.
"""

from __future__ import annotations


class TextTable:
    """A parsed `.tbl`: byte sequences <-> strings, plus the string-terminator bytes.

    Built by `parse_tbl`. `decode` turns bytes into text (optionally stopping at the
    first terminator); `encode` turns text back into bytes; the module-level searches
    need no table at all (relative) or use `encode` (table search)."""

    def __init__(self) -> None:
        self._dec: dict[bytes, str] = {}     # byte-seq -> string
        self._enc: dict[str, bytes] = {}     # string -> byte-seq (first wins)
        self._ends: set[bytes] = set()       # terminator byte-seqs
        self._dec_max = 1                    # longest key, for greedy decode
        self._enc_max = 1                    # longest string, for greedy encode

    # -- construction -------------------------------------------------------
    def _add(self, raw: bytes, text: str, *, end: bool) -> None:
        if end:
            self._ends.add(raw)
            self._dec_max = max(self._dec_max, len(raw))
            return
        # First mapping for a byte-seq / a string wins, so an earlier line is never
        # silently overwritten by a later duplicate.
        self._dec.setdefault(raw, text)
        if text:
            self._enc.setdefault(text, raw)
            self._enc_max = max(self._enc_max, len(text))
        self._dec_max = max(self._dec_max, len(raw))

    # -- decode -------------------------------------------------------------
    def decode(self, data: bytes, *, stop_at_end: bool = True) -> tuple[str, int]:
        """`data` -> (text, bytes_consumed). With `stop_at_end`, stop AT (and consume)
        the first terminator; the terminator itself is not put in the text. Without a
        table loaded every byte is unknown and comes back as `[XX]`."""
        out: list[str] = []
        i, n = 0, len(data)
        while i < n:
            hit = None
            for length in range(min(self._dec_max, n - i), 0, -1):
                seg = data[i:i + length]
                if seg in self._ends:
                    if stop_at_end:
                        return "".join(out), i + length
                    hit = ("", length); break
                if seg in self._dec:
                    hit = (self._dec[seg], length); break
            if hit is None:
                out.append(f"[{data[i]:02X}]")
                i += 1
            else:
                out.append(hit[0])
                i += hit[1]
        return "".join(out), i

    # -- encode -------------------------------------------------------------
    def encode(self, text: str) -> bytes | None:
        """`text` -> bytes, longest string-match first, or None if any part of it has
        no mapping. Used by the table search; the caller decides what to do with None."""
        out = bytearray()
        i, n = 0, len(text)
        while i < n:
            hit = None
            for length in range(min(self._enc_max, n - i), 0, -1):
                seg = text[i:i + length]
                if seg in self._enc:
                    hit = (self._enc[seg], length); break
            if hit is None:
                return None
            out += hit[0]
            i += hit[1]
        return bytes(out)

    # -- introspection (for the UI status line) -----------------------------
    def __len__(self) -> int:
        return len(self._dec)

    @property
    def terminators(self) -> tuple[bytes, ...]:
        return tuple(sorted(self._ends))


def _unescape(value: str) -> str:
    r"""Honour \n and \\ in a table value, so a line-break byte can map to a real
    newline. Any other backslash pair is left as-is rather than guessed at."""
    out: list[str] = []
    i = 0
    while i < len(value):
        c = value[i]
        if c == "\\" and i + 1 < len(value):
            nxt = value[i + 1]
            if nxt == "n":
                out.append("\n"); i += 2; continue
            if nxt == "\\":
                out.append("\\"); i += 2; continue
        out.append(c)
        i += 1
    return "".join(out)


def parse_tbl(text: str) -> TextTable:
    """Parse `.tbl` source into a `TextTable`. Lenient by design: a malformed line is
    skipped, not fatal, so one typo does not sink a whole table -- the count the UI
    shows (`len(table)`) tells you how much actually loaded."""
    table = TextTable()
    for line in text.splitlines():
        s = line.rstrip("\r\n")
        if not s.strip() or s.lstrip().startswith(";"):
            continue
        end = False
        if s.startswith("/"):
            end = True
            s = s[1:]
        # Split on the FIRST '=' only: the value may itself contain '='.
        key, sep, value = s.partition("=")
        key = key.strip()
        if end and not sep:          # "/FF" with no '=' is a bare terminator
            value = ""
        elif not sep:                # a normal line must have '='
            continue
        if len(key) < 2 or len(key) % 2 or any(c not in "0123456789abcdefABCDEF" for c in key):
            continue
        raw = bytes.fromhex(key)
        table._add(raw, _unescape(value), end=end)
    return table


def relative_search(data: bytes, sample: str) -> list[int]:
    """Find where `sample` sits in `data` under an UNKNOWN but linear encoding -- the
    tool that cracks a table from scratch. It matches on the DIFFERENCES between bytes
    rather than their values: if the game orders its letters like the sample's own
    characters (the usual case for an alphabet or a kana row), the gaps line up even
    though the absolute codes are unknown. Repeated letters (equal gaps of 0) are part
    of the fingerprint, which is what makes a distinctive word land a single hit.

    Returns the byte offsets of every window whose deltas match. `sample` needs at
    least two characters; two or three give many false hits, a word gives few."""
    n = len(sample)
    if n < 2 or len(data) < n:
        return []
    base = ord(sample[0])
    deltas = [(ord(sample[k]) - base) & 0xFF for k in range(n)]
    hits: list[int] = []
    end = len(data) - n
    for i in range(end + 1):
        b0 = data[i]
        for k in range(1, n):
            if ((data[i + k] - b0) & 0xFF) != deltas[k]:
                break
        else:
            hits.append(i)
    return hits


def scan_strings(data: bytes, table: TextTable, *, min_len: int = 4,
                 min_known: float = 0.7) -> list[tuple[int, int, str]]:
    """Locate every run of TEXT in `data` -- the 'where is the script' question, once a
    table is loaded. A run is a stretch the table decodes to mostly-known characters;
    it ends at a terminator or at too many unknown bytes in a row. Returns (offset,
    byte_length, decoded_text) for runs of at least `min_len` characters whose share of
    known (non-`[XX]`) characters is at least `min_known`.

    This is the generic counterpart of a game's bespoke extractor: no offset list, no
    pointer table needed -- it reads the bytes and reports what looks like language."""
    results: list[tuple[int, int, str]] = []
    n = len(data)
    i = 0
    run_start = -1
    chars: list[str] = []
    known = 0
    unknown_tail = 0

    def flush(end: int) -> None:
        nonlocal run_start, chars, known, unknown_tail
        if run_start >= 0 and len(chars) >= min_len and known >= min_known * len(chars):
            results.append((run_start, end - run_start, "".join(chars)))
        run_start = -1
        chars = []
        known = 0
        unknown_tail = 0

    while i < n:
        # longest match at i, mirroring decode() but classifying the token
        seg_len = 0
        token = None
        is_end = False
        for length in range(min(table._dec_max, n - i), 0, -1):
            seg = data[i:i + length]
            if seg in table._ends:
                seg_len, is_end = length, True
                break
            if seg in table._dec:
                seg_len, token = length, table._dec[seg]
                break
        if is_end:
            flush(i)
            i += seg_len
            continue
        if token is not None:
            if run_start < 0:
                run_start = i
            chars.append(token)
            known += 1
            unknown_tail = 0
            i += seg_len
        else:
            # an unknown byte: tolerated inside a run, but two in a row ends it
            if run_start >= 0:
                chars.append(f"[{data[i]:02X}]")
                unknown_tail += 1
                if unknown_tail >= 2:
                    # drop the trailing unknowns and close the run before them
                    del chars[-unknown_tail:]
                    flush(i - unknown_tail + 1)
            i += 1
    flush(n)
    return results


def table_search(data: bytes, text: str, table: TextTable) -> list[int]:
    """Find `text` in `data` using a loaded table: encode it, then hunt the byte run.
    Empty list if the text cannot be encoded (a character the table has no byte for)."""
    needle = table.encode(text)
    if not needle:
        return []
    hits: list[int] = []
    start = 0
    while True:
        j = data.find(needle, start)
        if j < 0:
            break
        hits.append(j)
        start = j + 1
    return hits


# --------------------------------------------------------------------------
# Cracking a table (semi-)automatically.
#
# The strong move is NOT to assume a linear alphabet but to read the ACTUAL bytes under
# a word you already know the position of: each character then maps to whatever byte
# sits there, so a non-contiguous encoding cracks just as well. `relative_search` finds
# the position; these turn known (word, position) pairs into a real table.
# --------------------------------------------------------------------------

def derive_from_hit(data: bytes, sample: str, offset: int) -> dict[str, int]:
    """Map each character of `sample` to the byte under it at `offset`. Raises
    ValueError on an internal contradiction (the same character over two different
    bytes) -- that means the offset is wrong, not that the encoding is odd."""
    mapping: dict[str, int] = {}
    if offset < 0 or offset + len(sample) > len(data):
        raise ValueError("sample runs past the data at that offset")
    for k, ch in enumerate(sample):
        b = data[offset + k]
        if ch in mapping and mapping[ch] != b:
            raise ValueError(f"contradiction: {ch!r} is both "
                             f"{mapping[ch]:02X} and {b:02X}")
        mapping[ch] = b
    return mapping


def crack_from_words(data: bytes, entries: "list[str | tuple[str, int]]"
                     ) -> tuple[dict[str, int], list[str]]:
    """Assemble a byte<->character mapping from words you can READ on screen, more or
    less automatically. An entry is either a plain word -- located by relative search,
    used only if the hit is UNIQUE -- or a `(word, offset)` pair that pins it, which is
    how you crack a common word that matches in many places (find it once, then pin it).
    Returns the merged mapping and a human report of what cracked, was skipped (no hit /
    ambiguous), or clashed (a byte two characters claim -- the first wins)."""
    mapping: dict[str, int] = {}
    byte_owner: dict[int, str] = {}
    report: list[str] = []
    for entry in entries:
        if isinstance(entry, tuple):
            word, offset = entry[0].strip(), entry[1]
        else:
            word, offset = entry.strip(), None
        if not word:
            continue
        if offset is None:
            hits = relative_search(data, word)
            if not hits:
                report.append(f"{word!r}: no match — skipped")
                continue
            if len(hits) > 1:
                report.append(f"{word!r}: {len(hits)} matches (ambiguous) — skipped; "
                              f"pin it with '{word} @ <offset>'")
                continue
            offset = hits[0]
        try:
            local = derive_from_hit(data, word, offset)
        except ValueError as exc:
            report.append(f"{word!r} @ 0x{offset:X}: {exc}")
            continue
        for ch, b in local.items():
            if ch in mapping and mapping[ch] != b:
                report.append(f"{ch!r}: {mapping[ch]:02X} vs {b:02X} (from {word!r}) — kept first")
                continue
            if b in byte_owner and byte_owner[b] != ch:
                report.append(f"byte {b:02X}: {byte_owner[b]!r} and {ch!r} — kept first")
                continue
            mapping.setdefault(ch, b)
            byte_owner.setdefault(b, ch)
        report.append(f"{word!r}: cracked at 0x{offset:X}")
    return mapping, report


def build_tbl(mapping: dict[str, int]) -> str:
    """A `.tbl` source for a single-byte char<->byte mapping, sorted by byte, ready to
    save and refine. Newline and space are emitted with the honoured escapes so the
    file round-trips through `parse_tbl`."""
    lines = []
    for ch, b in sorted(mapping.items(), key=lambda kv: kv[1]):
        if ch == "\n":
            val = "\\n"
        elif ch == "\\":
            val = "\\\\"
        else:
            val = ch
        lines.append(f"{b:02X}={val}")
    return "\n".join(lines) + ("\n" if lines else "")
