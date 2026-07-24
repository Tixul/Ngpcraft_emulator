# -*- coding: utf-8 -*-
"""Pointer discovery for fan-translation, working on ANY ROM.

Repointing is the other half of translating: once a string moves, whatever pointed at
it has to be found and updated. Nothing here knows a specific game -- the pointer width
(16/24/32-bit little-endian) and the base added to a stored value are parameters, so it
fits a cart of absolute 32-bit pointers as well as one of bank-relative 16-bit offsets.

Two questions it answers:
  * `find_pointers_to` -- who points AT this address? (the entries to patch)
  * `scan_pointer_tables` -- where are the pointer TABLES? (runs of plausible pointers)

Everything takes raw bytes and returns file offsets into them, so the caller adds its
own base (the cartridge maps at 0x200000) to show a CPU address.
"""

from __future__ import annotations

import numpy as np


def _le_values(data: bytes, width: int) -> np.ndarray:
    """Little-endian value at EVERY byte offset, as uint64. Result length is
    len(data) - width + 1; entry i is the `width`-byte LE integer starting at i."""
    if width < 1 or width > 4:
        raise ValueError("width must be 1..4 bytes")
    a = np.frombuffer(data, np.uint8).astype(np.uint64)
    m = len(a) - width + 1
    if m <= 0:
        return np.empty(0, np.uint64)
    vals = np.zeros(m, np.uint64)
    for k in range(width):
        vals |= a[k:k + m] << np.uint64(8 * k)
    return vals


def find_pointers_to(data: bytes, target: int, *, base: int = 0,
                     width: int = 4, tolerance: int = 0) -> list[int]:
    """Byte offsets in `data` holding a `width`-byte LE pointer that resolves to
    `target` (i.e. base + stored == target), within +/- `tolerance`. Use tolerance to
    catch a pointer into the MIDDLE of a string (a common re-use) as well as its head."""
    vals = _le_values(data, width)
    if vals.size == 0:
        return []
    resolved = vals.astype(np.int64) + np.int64(base)
    lo, hi = target - tolerance, target + tolerance
    return [int(i) for i in np.nonzero((resolved >= lo) & (resolved <= hi))[0]]


def scan_pointer_tables(data: bytes, *, base: int = 0, width: int = 4,
                        lo: int | None = None, hi: int | None = None,
                        min_run: int = 8) -> list[tuple[int, int, int]]:
    """Find runs of consecutive plausible pointers -- i.e. pointer TABLES.

    A pointer is 'plausible' when base + its stored value lands in [lo, hi), which
    defaults to the whole of `data` (base .. base+len). A run is `min_run` or more of
    them back to back at the pointer width's stride. Returns (offset, count, first
    resolved target) per table, so the caller can jump to the table and to what its
    first entry points at.

    The stride is the width, but a table can START at any byte, so every phase is
    scanned -- still one linear pass, not width passes over the data."""
    vals = _le_values(data, width)
    if vals.size == 0:
        return []
    resolved = vals.astype(np.int64) + np.int64(base)
    if lo is None:
        lo = base
    if hi is None:
        hi = base + len(data)
    good = (resolved >= lo) & (resolved < hi)

    tables: list[tuple[int, int, int]] = []
    for phase in range(width):
        g = good[phase::width]
        if not g.any():
            continue
        # Run-length encode the boolean lane: a True run of length >= min_run is a table.
        edges = np.diff(g.astype(np.int8))
        starts = list(np.nonzero(edges == 1)[0] + 1)
        ends = list(np.nonzero(edges == -1)[0] + 1)
        if g[0]:
            starts.insert(0, 0)
        if g[-1]:
            ends.append(len(g))
        for s, e in zip(starts, ends):
            count = e - s
            if count >= min_run:
                offset = phase + s * width
                first_target = int(resolved[offset])
                tables.append((offset, count, first_target))
    tables.sort()
    return tables
