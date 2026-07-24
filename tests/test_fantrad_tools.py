# -*- coding: utf-8 -*-
"""The ROM-agnostic fan-translation tools beyond decode/search: string scan, table
cracking, pointer discovery, and ROM diff. Pure data -- no Qt, no emulator."""

from core.texttable import (
    parse_tbl, scan_strings, derive_from_hit, crack_from_words, build_tbl,
)
from core.pointers import find_pointers_to, scan_pointer_tables
from core.romdiff import diff_ranges


_LETTERS = "".join(f"{0xA4 + i:02X}={c}\n" for i, c in enumerate("abcdefghijklmnopqrstuvwxyz"))
_TBL = parse_tbl(_LETTERS + "00= \n/FF=<end>\n")


def _enc(s: str) -> bytes:
    return _TBL.encode(s)


# ---- string scan ----------------------------------------------------------
def test_scan_strings_finds_runs_of_text_and_skips_noise():
    data = b"\x00\x01\x02" + _enc("hello") + b"\xff" + b"\x03\x04" + _enc("world") + b"\xff"
    runs = scan_strings(data, _TBL, min_len=4)
    texts = [t for _, _, t in runs]
    assert "hello" in texts and "world" in texts
    # the binary noise (0x01..0x04) is not long enough / not known -> not a run
    assert all("hello" == t or "world" == t for t in texts)


def test_scan_strings_offsets_are_right():
    data = b"\x7f" + _enc("cat") + b"\xff"       # 0x7F is unknown, so the run starts after it
    runs = scan_strings(data, _TBL, min_len=3)
    assert runs and runs[0][0] == 1, "the run starts after the leading unknown byte"


# ---- cracking -------------------------------------------------------------
def test_derive_from_hit_reads_actual_bytes():
    data = b"\x00\x00" + _enc("hi")
    m = derive_from_hit(data, "hi", 2)
    assert m == {"h": _TBL.encode("h")[0], "i": _TBL.encode("i")[0]}


def test_crack_from_words_assembles_a_table_from_readable_words():
    # plant two words; each is unique, so each cracks
    data = b"\x11\x22" + _enc("player") + b"\x33" + _enc("magic") + b"\x44"
    mapping, report = crack_from_words(data, ["player", "magic"])
    # every distinct letter of both words is now known, with the real byte
    for ch in set("playermagic"):
        assert mapping[ch] == _TBL.encode(ch)[0]
    assert any("cracked" in r for r in report)


def test_crack_reports_ambiguous_and_missing_words():
    data = _enc("aa") + b"\x00" + _enc("aa")     # "aa" appears twice -> ambiguous
    mapping, report = crack_from_words(data, ["aa", "zzz"])
    assert any("ambiguous" in r for r in report)
    assert any("no match" in r for r in report)


def test_crack_pins_an_ambiguous_word_by_offset():
    # "cat" twice: ambiguous by search, but pinning the SECOND one cracks it.
    data = _enc("cat") + b"\x00" + _enc("cat")
    mapping, report = crack_from_words(data, [("cat", 4)])   # offset 4 = second "cat"
    assert mapping["c"] == _TBL.encode("c")[0]
    assert any("cracked at 0x4" in r for r in report)


def test_build_tbl_round_trips_through_parse():
    mapping = {"a": 0xA4, "b": 0xA5, " ": 0x00, "\n": 0xF0}
    tbl = build_tbl(mapping)
    back = parse_tbl(tbl)
    assert back.encode("ab") == bytes([0xA4, 0xA5])
    assert back.decode(b"\xf0", stop_at_end=False)[0] == "\n"


# ---- pointers -------------------------------------------------------------
def test_find_pointers_to_locates_every_reference():
    target = 0x205ABC                            # an absolute pointer -> base 0
    ptr = (target).to_bytes(4, "little")
    data = b"\x00\x00\x00\x00" + ptr + b"\xAA\xBB" + ptr    # ptrs at offsets 4 and 10
    hits = find_pointers_to(data, target, base=0, width=4)
    assert hits == [4, 10]


def test_find_pointers_to_honours_base_and_tolerance():
    base = 0x200000
    stored = 0x005000                         # a bank-relative offset
    data = b"\x11" + stored.to_bytes(3, "little")
    # base + stored == 0x205000
    assert find_pointers_to(data, 0x205000, base=base, width=3) == [1]
    # tolerance catches a pointer a few bytes into the target
    assert find_pointers_to(data, 0x205002, base=base, width=3, tolerance=4) == [1]


def test_scan_pointer_tables_finds_a_run():
    cart = 0x200000
    # eight consecutive ABSOLUTE 32-bit pointers into the cart -> base 0, range on cart
    body = b"".join((cart + 0x40 + 4 * k).to_bytes(4, "little") for k in range(8))
    data = b"\x00\x00" + body + b"\x99\x99"
    tables = scan_pointer_tables(data, base=0, width=4,
                                 lo=cart, hi=cart + 0x10000, min_run=8)
    assert tables, "a run of eight pointers is a table"
    offset, count, first = tables[0]
    assert offset == 2 and count >= 8
    assert first == cart + 0x40


# ---- rom diff -------------------------------------------------------------
def test_diff_ranges_merges_nearby_changes():
    a = bytearray(b"\x00" * 48)
    b = bytearray(a)
    b[4] = 0x41; b[5] = 0x42; b[7] = 0x43      # three changes within a few bytes
    b[40] = 0x99                               # a separate change, gap > merge_gap away
    ranges = diff_ranges(bytes(a), bytes(b), merge_gap=16)
    assert len(ranges) == 2
    off0, a0, b0 = ranges[0]
    assert off0 == 4 and b0 == b"\x41\x42\x00\x43"     # 4..7 merged as one
    assert ranges[1][0] == 40


def test_diff_ranges_identical_is_empty():
    a = b"\x01\x02\x03"
    assert diff_ranges(a, a) == []
