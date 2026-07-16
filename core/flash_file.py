"""The `.flash` save file — NeoPop's format, because it is everyone's format.

The cartridge IS the save: a game erases a block of its own ROM and programs its
slot back in. So "persisting the save" means persisting the bytes of the cart image
that no longer match the file on disk, and restoring them the next time the
cartridge goes in.

WHY NOT INVENT A FORMAT
-----------------------
NeoPop wrote one in 2002; Mednafen kept it byte-for-byte (same structs, same raw
`memcpy` of the struct, padding included); RACE reads it too. A save is a thing a
player wants to keep and to move between emulators, and there is no version of
"our own cleaner format" that is worth being the one emulator whose saves nobody
else can read.

THE LAYOUT (Core/flash.c)
-------------------------
    FlashFileHeader     u16 valid_flash_id = 0x0053
                        u16 block_count
                        u32 total_file_length      (header + every block)

    FlashFileBlockHeader u32 start_address         (a CPU address: 0x200000 + offset)
                         u16 data_length
                         -- and then TWO BYTES OF C STRUCT PADDING, because they
                            memcpy the struct straight into the file and `u32, u16`
                            aligns to 8. The padding is IN THE FILE. Pack it to 6
                            bytes and every other emulator reads garbage.
    ... followed by data_length bytes.

⚠️ `data_length` is a u16, so a block cannot exceed 65535 bytes -- one byte short of
the 64 KiB the chip's own map uses. We therefore split long runs at 32 KiB. A save
area is 8 or 16 KiB (the small blocks at the top of the chip exist precisely so a
save does not cost 64 KiB), so this is a limit the format meets only when a game has
rewritten a code block, and splitting is what NeoPop's own contiguous-merge would
have produced anyway.

NOT PERSISTED: block protection. The format has nowhere to put it, no other emulator
keeps it, and on silicon it is irreversible (SysCall.txt, VECT_FLASHPROTECT: "there
is no operation which will remove the protection"). A protected block comes back
writable on reload. Say so out loud rather than pretend.
"""

from __future__ import annotations

import struct
from pathlib import Path

FLASH_VALID_ID = 0x0053
CART_BASE = 0x200000

_HEADER = struct.Struct("<HHI")        # id, block_count, total_file_length      -> 8
_BLOCK = struct.Struct("<IH2x")        # start_address, data_length, C padding   -> 8

GRANULE = 0x100                        # the chip programs in 256-byte units
MAX_BLOCK = 0x8000                     # keep data_length inside its u16


class BadFlashFile(ValueError):
    """The file is not a `.flash`, or not one we should be applying to a cartridge."""


def diff_blocks(original: bytes, current: bytes, base: int = CART_BASE) -> list[tuple[int, bytes]]:
    """The bytes of the cartridge that are no longer the bytes of the ROM file.

    An ERASE counts: it turns data into 0xFF, and that is a change that must survive
    a reload, or putting the cartridge back in would hand the game its old data.
    Runs are rounded out to the chip's 256-byte programming granule, merged, and
    split at MAX_BLOCK.
    """
    n = min(len(original), len(current))
    granules = [
        g for g in range(0, n, GRANULE)
        if original[g : g + GRANULE] != current[g : g + GRANULE]
    ]
    if not granules:
        return []

    blocks: list[tuple[int, bytes]] = []
    start = prev = granules[0]
    for g in granules[1:]:
        contiguous = g == prev + GRANULE
        if not contiguous or (g + GRANULE - start) > MAX_BLOCK:
            blocks.append((base + start, current[start : prev + GRANULE]))
            start = g
        prev = g
    blocks.append((base + start, current[start : prev + GRANULE]))
    return blocks


def pack(blocks: list[tuple[int, bytes]]) -> bytes:
    """Serialise. Returns b"" for nothing to save, which is not a file worth writing."""
    if not blocks:
        return b""
    total = _HEADER.size + sum(_BLOCK.size + len(d) for _, d in blocks)
    out = bytearray(_HEADER.pack(FLASH_VALID_ID, len(blocks), total))
    for address, data in blocks:
        if len(data) > 0xFFFF:
            raise ValueError(f"block at {address:#08x} is {len(data)} bytes: too long for the format")
        out += _BLOCK.pack(address, len(data))
        out += data
    return bytes(out)


def unpack(blob: bytes) -> list[tuple[int, bytes]]:
    """Parse, and REFUSE anything that does not look like the real thing.

    A save file is applied straight into the cartridge image, so a malformed one is a
    corrupted game. Truncation is a hard error and not a partial read: half a save is
    worse than none, because it looks like a working one.
    """
    if len(blob) < _HEADER.size:
        raise BadFlashFile("shorter than its own header")
    magic, count, total = _HEADER.unpack_from(blob)
    if magic != FLASH_VALID_ID:
        raise BadFlashFile(f"bad magic {magic:#06x} (want {FLASH_VALID_ID:#06x})")
    if total > len(blob):
        raise BadFlashFile(f"header claims {total} bytes, file has {len(blob)}")

    blocks: list[tuple[int, bytes]] = []
    pos = _HEADER.size
    for _ in range(count):
        if pos + _BLOCK.size > len(blob):
            raise BadFlashFile("truncated in a block header")
        address, length = _BLOCK.unpack_from(blob, pos)
        pos += _BLOCK.size
        if pos + length > len(blob):
            raise BadFlashFile(f"block at {address:#08x} runs off the end of the file")
        blocks.append((address, blob[pos : pos + length]))
        pos += length
    return blocks


def read(path: str | Path) -> list[tuple[int, bytes]]:
    """The save on disk, or nothing. A missing file is not an error -- it is a new game."""
    p = Path(path)
    if not p.exists() or p.stat().st_size == 0:
        return []
    return unpack(p.read_bytes())


def write(path: str | Path, blocks: list[tuple[int, bytes]]) -> bool:
    """Commit. Returns whether a file was written."""
    blob = pack(blocks)
    if not blob:
        return False
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    # Write beside the target and rename: a save interrupted half-written is a save
    # LOST, and this is the one file in the emulator the player cannot regenerate.
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_bytes(blob)
    tmp.replace(p)
    return True
