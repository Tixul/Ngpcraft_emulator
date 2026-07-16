"""Binary P6 PPM frame diff primitive (pass 25).

Compares two PPM frames pixel-by-pixel and reports an equality flag
plus a small set of diagnostic counters. Used directly by the
`frame diff` CLI and by `frame golden-check` under the hood.

No PIL dependency: PPM parser is hand-rolled to keep the renderer
stack zero-deps (consistent with `core/renderer.py::frame_to_ppm_bytes`).
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class FrameDiffResult:
    """Outcome of comparing two PPM frames.

    `equal` is True iff every byte of the body matches. `pixel_count_different`
    counts pixels (RGB triplets) that differ, not bytes; `first_diff_pixel`
    is the `(x, y)` of the leftmost-topmost difference (row-major scan) or
    `None` when the frames match.
    """

    equal: bool
    width: int
    height: int
    total_pixels: int
    pixel_count_different: int
    diff_ratio: float
    first_diff_pixel: tuple[int, int] | None


def parse_ppm_p6(data: bytes) -> tuple[int, int, bytes]:
    """Parse a binary P6 PPM blob into `(width, height, body_bytes)`.

    Accepts comments (`#` to EOL) and any of the conventional whitespace
    separators between header tokens. Maxval must be `255` (matches the
    canonical 8-bit-per-channel encoding produced by
    `pixels_to_ppm_bytes`). Raises `ValueError` on malformed or
    unsupported PPM.
    """
    if not data.startswith(b"P6"):
        raise ValueError("not a P6 PPM (missing 'P6' magic)")
    pos = 2
    if pos >= len(data) or data[pos:pos + 1] not in (b"\n", b" ", b"\t", b"\r"):
        raise ValueError("expected whitespace after 'P6' magic")
    pos += 1

    def read_token(p: int) -> tuple[str, int]:
        while p < len(data):
            ch = data[p:p + 1]
            if ch == b"#":
                while p < len(data) and data[p:p + 1] != b"\n":
                    p += 1
                continue
            if ch in (b" ", b"\t", b"\n", b"\r"):
                p += 1
                continue
            break
        start = p
        while p < len(data):
            ch = data[p:p + 1]
            if ch in (b" ", b"\t", b"\n", b"\r", b"#"):
                break
            p += 1
        if start == p:
            raise ValueError("unexpected end of PPM header")
        return data[start:p].decode("ascii"), p

    width_str, pos = read_token(pos)
    height_str, pos = read_token(pos)
    maxval_str, pos = read_token(pos)
    try:
        width = int(width_str)
        height = int(height_str)
        maxval = int(maxval_str)
    except ValueError as exc:
        raise ValueError(f"non-integer in PPM header: {exc}") from None
    if width <= 0 or height <= 0:
        raise ValueError(f"non-positive dimensions: {width}×{height}")
    if maxval != 255:
        raise ValueError(
            f"unsupported PPM maxval {maxval} (only 255 supported)"
        )
    if pos >= len(data) or data[pos:pos + 1] not in (b"\n", b" ", b"\t", b"\r"):
        raise ValueError("expected whitespace before pixel data")
    pos += 1

    body_len = width * height * 3
    body = data[pos:pos + body_len]
    if len(body) != body_len:
        raise ValueError(
            f"truncated PPM body: expected {body_len} bytes, got {len(body)}"
        )
    return width, height, body


def diff_ppm_bytes(ppm_a: bytes, ppm_b: bytes) -> FrameDiffResult:
    """Diff two binary P6 PPMs and return a structured result.

    Raises `ValueError` when the two frames have different dimensions
    (a mis-sized comparison is almost always a usage error rather than
    a "different frame" — the caller would be hiding a real bug if we
    pretended otherwise).
    """
    width_a, height_a, body_a = parse_ppm_p6(ppm_a)
    width_b, height_b, body_b = parse_ppm_p6(ppm_b)
    if (width_a, height_a) != (width_b, height_b):
        raise ValueError(
            f"dimension mismatch: {width_a}×{height_a} vs {width_b}×{height_b}"
        )

    total_pixels = width_a * height_a
    diff_count = 0
    first_diff: tuple[int, int] | None = None
    for i in range(0, len(body_a), 3):
        if body_a[i] != body_b[i] or body_a[i + 1] != body_b[i + 1] \
                or body_a[i + 2] != body_b[i + 2]:
            diff_count += 1
            if first_diff is None:
                pixel_index = i // 3
                first_diff = (pixel_index % width_a, pixel_index // width_a)

    return FrameDiffResult(
        equal=(diff_count == 0),
        width=width_a,
        height=height_a,
        total_pixels=total_pixels,
        pixel_count_different=diff_count,
        diff_ratio=(diff_count / total_pixels) if total_pixels else 0.0,
        first_diff_pixel=first_diff,
    )
