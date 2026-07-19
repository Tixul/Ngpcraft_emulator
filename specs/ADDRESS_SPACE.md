# Address Space v0

Purpose:
- define the current minimal address-space object used by the bootstrap tooling
- support ROM loading, reset bootstrap views and address probing

Current source references:
- `../../01_SDK/docs/ngpcspec.txt`
- `../../01_SDK/docs/NGPC_SDK_MASTER_ENTRY.md`
- `../NgpCraft_toolchain/StarGunner_save_lib_test/README.md`
- `../NgpCraft_toolchain/StarGunner_save_lib_test/src/core/ngpc_flash.c`

Current mapped regions:
- `0x000000..0x0000FF` : internal CPU I/O page — **does not power on at `0x00`**;
  the TMP95C061 on-chip registers have documented reset values
  (see `specs/MEMORY_READ.md` § 2.2). Notably `0x60`/`0x61` = the A/D result =
  the **battery** (`specs/ADC.md`), `0x20`/`0x22`-`0x28` = the timers
  (`specs/TIMERS.md`), `0x6E` = the cartridge flash `/WE` (`specs/FLASH.md`),
  `0x70`-`0x7A` = the interrupt priority (INTxx) registers.
- `0x004000..0x006BFF` : user RAM
- `0x006C00..0x006FB7` : system-reserved RAM
- `0x006FB8..0x006FFC` : **user interrupt vector table** — `0x6FB8 + index * 4`,
  per the SNK SDK. `0x006FCC` (the VBlank hook) is simply slot 5. The BIOS handler
  runs first and *chains* to the pointer stored here. See `specs/FRAME_TIMING.md`.
- `0x006FFD..0x006FFF` : system-reserved RAM tail
- `0x007000..0x007FFF` : shared Z80 RAM
- `0x008000..0x008FFF` : K2GE/video registers and palette RAM
- `0x009000..0x0097FF` : SCR1 map
- `0x009800..0x009FFF` : SCR2 map
- `0x00A000..0x00BFFF` : character RAM
- `0x200000..(0x200000 + rom_size - 1)` : loaded cart ROM image
- remaining `0x200000..0x3FFFFF` addresses beyond the file size are tagged as unloaded cart flash
- `0xFF0000..0xFFFFFF` : BIOS ROM

Current scope:
- region lookup
- region kind tagging
- ROM file offset translation for loaded cart ROM
- 2 MB cart-flash window visibility for save-related addresses such as `0x3FA000..0x3FBFFF`
- unmapped-address reporting

Cold-start values modeled (per `MEMORY_READ.md` §2):
- on-chip RAM/VRAM (Work RAM, system page, shared Z80 RAM, K2GE,
  SCR1/SCR2 maps, character RAM) read as `0x00` at reset
- `0x006F91` carries the ROM header `mode_raw` byte
- CPU I/O page (`0x000000..0x0000FF`) is mapped but not yet backed

Not modeled yet:
- mirroring
- bus timing
- read/write side effects
- BIOS backing image loading
- dynamic banking behavior if required later
- CPU I/O page reset values (timers, DMA channels, IRQ controller)

CLI user:
- `python ngpc_emu.py addr-info <rom> <address>`
