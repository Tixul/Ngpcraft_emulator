# ROM Header v0

Minimal ROM header spec used by the first headless prototype.

Source references:
- `../../01_SDK/docs/ngpcspec.txt`
- `../../01_SDK/docs/NGPC_SDK_MASTER_ENTRY.md`

Raw file layout:
- the ROM file starts at cartridge CPU address `0x200000`
- the header is therefore read from file offset `0x000000`

Parsed fields:
- `0x0000..0x001B` : copyright / recognition code, 28 bytes ASCII
- `0x001C..0x001F` : entry point, 32-bit little-endian
- `0x0020..0x0021` : game ID, 16-bit field
- `0x0022` : version
- `0x0023` : mode
  - `0x00` = mono
  - `0x10` = color
- `0x0024..0x002F` : title, 12 bytes ASCII

Current scope:
- parse only
- no validation of entry point target yet
- no checksum logic
- no mapper inference yet

Next likely extensions:
- stronger header validation
- ROM metadata object versioning
- save/media hints if derivable later
