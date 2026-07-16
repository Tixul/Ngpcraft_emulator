"""ROM parsing helpers for NgpCraft Emulator."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


ROM_HEADER_SIZE = 0x30


def _decode_ascii_field(raw: bytes) -> str:
    """Decode a fixed-size ASCII field, trimming trailing NUL/space padding."""
    text = raw.decode("ascii", errors="replace")
    return text.rstrip("\x00 ").strip()


def _format_bcd_u16(value: int) -> str:
    """Format a 16-bit BCD-ish field as four hex nibbles."""
    return f"{value:04X}"


@dataclass(frozen=True)
class NgpcRomHeader:
    """Minimal NGPC cartridge header information."""

    path: Path
    file_size: int
    copyright_text: str
    entry_point: int
    game_id_raw: int
    game_id_bcd: str
    version: int
    mode_raw: int
    title: str

    @property
    def is_color(self) -> bool:
        return self.mode_raw == 0x10

    @property
    def mode_name(self) -> str:
        if self.mode_raw == 0x10:
            return "color"
        if self.mode_raw == 0x00:
            return "mono"
        return f"unknown(0x{self.mode_raw:02X})"


def parse_rom_header_bytes(data: bytes, path: Path) -> NgpcRomHeader:
    """Parse the NGPC cartridge header from raw ROM bytes."""
    if len(data) < ROM_HEADER_SIZE:
        raise ValueError(
            f"ROM too small for NGPC header: got {len(data)} bytes, "
            f"need at least {ROM_HEADER_SIZE}"
        )

    copyright_text = _decode_ascii_field(data[0x00:0x1C])
    entry_point = int.from_bytes(data[0x1C:0x20], "little")
    game_id_raw = int.from_bytes(data[0x20:0x22], "little")
    version = data[0x22]
    mode_raw = data[0x23]
    title = _decode_ascii_field(data[0x24:0x30])

    return NgpcRomHeader(
        path=path,
        file_size=len(data),
        copyright_text=copyright_text,
        entry_point=entry_point,
        game_id_raw=game_id_raw,
        game_id_bcd=_format_bcd_u16(game_id_raw),
        version=version,
        mode_raw=mode_raw,
        title=title,
    )


def load_rom_header(path: str | Path) -> NgpcRomHeader:
    """Load and parse a ROM header from disk."""
    rom_path = Path(path)
    data = rom_path.read_bytes()
    return parse_rom_header_bytes(data, rom_path)
