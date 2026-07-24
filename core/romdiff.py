# -*- coding: utf-8 -*-
"""Byte-diff between two ROMs, for fan-translation, working on ANY pair.

An existing patch (a released English translation, say) is an ORACLE: the bytes it
changed are, almost by definition, the text. Diffing the cartridge in the emulator
against a second `.ngc` on disk shows exactly those ranges -- where to look, and, with
a table loaded, what each side says. Nothing game-specific: it is a plain byte compare.
"""

from __future__ import annotations


def diff_ranges(a: bytes, b: bytes, *, merge_gap: int = 16
                ) -> list[tuple[int, bytes, bytes]]:
    """Changed ranges between `a` and `b`, as (offset, a_slice, b_slice).

    Differing bytes closer than `merge_gap` are reported as ONE range, so a changed
    string comes back whole instead of shredded into one entry per altered letter. The
    compare runs over the shorter length; a size difference past that is not a 'change'
    this reports (the caller knows the two files' sizes)."""
    n = min(len(a), len(b))
    ranges: list[tuple[int, bytes, bytes]] = []
    i = 0
    while i < n:
        if a[i] == b[i]:
            i += 1
            continue
        start = i
        last_diff = i
        i += 1
        # extend while the next difference is within merge_gap of the last one
        while i < n and (a[i] != b[i] or i - last_diff <= merge_gap):
            if a[i] != b[i]:
                last_diff = i
            i += 1
        end = last_diff + 1
        ranges.append((start, a[start:end], b[start:end]))
    return ranges
