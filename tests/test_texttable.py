# -*- coding: utf-8 -*-
"""The ROM-agnostic text tools behind the debugger's Text tab: table parsing,
decode/encode, and the two searches. Pure data -- no Qt, no emulator."""

from core.texttable import parse_tbl, relative_search, table_search


# A tiny table exercising every feature: one-byte letters, a multi-byte entry, a
# newline escape, and a terminator.
_TBL = """
; a comment, ignored
00=
8A=a
8B=b
8C=c
819A=cat
FF00=\\n
/FF=<end>
"""


def test_parse_counts_entries_and_terminator():
    t = parse_tbl(_TBL)
    assert len(t) >= 5                       # 00,8A,8B,8C,819A,FF00
    assert b"\xff" in t.terminators


def test_decode_prefers_the_longest_match():
    t = parse_tbl(_TBL)
    # 81 9A must decode as "cat", not as two unknown/short pieces.
    text, used = t.decode(b"\x81\x9a", stop_at_end=False)
    assert text == "cat" and used == 2


def test_decode_stops_at_the_terminator():
    t = parse_tbl(_TBL)
    text, used = t.decode(b"\x8a\x8b\x8c\xff\x8a")   # "abc" <end> then more
    assert text == "abc"
    assert used == 4, "the terminator is consumed but not shown"


def test_decode_shows_unknown_bytes_rather_than_dropping_them():
    t = parse_tbl(_TBL)
    text, _ = t.decode(b"\x8a\x7f\x8b", stop_at_end=False)
    assert text == "a[7F]b"


def test_newline_escape_maps_to_a_real_newline():
    t = parse_tbl(_TBL)
    text, _ = t.decode(b"\x8a\xff\x00\x8b", stop_at_end=False)
    assert text == "a\nb"


def test_encode_round_trips_and_reports_the_unencodable():
    t = parse_tbl(_TBL)
    assert t.encode("abc") == b"\x8a\x8b\x8c"
    # the multi-char entry wins over three single letters
    assert t.encode("cat") == b"\x81\x9a"
    assert t.encode("az") is None, "no byte for 'z' -> not encodable"


def test_table_search_finds_every_occurrence():
    t = parse_tbl(_TBL)
    data = b"\x00\x8a\x8b\x8c\x00\x8a\x8b\x8c\x00"     # "abc" twice
    assert table_search(data, "abc", t) == [1, 5]
    assert table_search(data, "z", t) == []             # unencodable -> no hits


def test_relative_search_cracks_an_unknown_linear_encoding():
    # "hello" planted at a made-up base (letters kept in ASCII order, shifted by 0x40),
    # plus a decoy that shares the first delta but not the rest.
    base = 0x40
    word = bytes((base + (ord(c) - ord("h"))) & 0xFF for c in "hello")
    data = b"\x00\x11\x22" + word + b"\x99" + bytes([base, base + 1, base + 9])
    hits = relative_search(data, "hello")
    assert hits == [3], "the repeated 'l' fingerprint pins a single hit"


def test_relative_search_needs_at_least_two_chars():
    assert relative_search(b"\x01\x02\x03", "x") == []


def test_empty_table_decodes_everything_as_unknown():
    t = parse_tbl("")
    text, used = t.decode(b"\x8a\x8b", stop_at_end=False)
    assert text == "[8A][8B]" and used == 2
    assert t.encode("a") is None
