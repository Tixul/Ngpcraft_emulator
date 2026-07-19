# Memory Read v1

Purpose:
- define the read-only memory access path used by the bootstrap tooling
  and the executor
- support byte peeks from the loaded ROM image, the on-chip RAM/VRAM
  cold-start image, and the runtime writable overlay

Current source references:
- `../../01_SDK/docs/ngpcspec.txt`
- `../../01_SDK/docs/NGPC_SDK_MASTER_ENTRY.md`
- `ADDRESS_SPACE.md`
- `../NgpCraft_toolchain/StarGunner_save_lib_test/README.md`
- `../NgpCraft_toolchain/StarGunner_save_lib_test/src/core/ngpc_flash.c`

## 1. Read path

A read at address `A` of size `N` is resolved in order:

1. **Runtime writable overlay** (passed by the executor):
   per-byte if the address has been written during this session.
2. **Read bus** (`core/memory.py::NgpcReadBus.read_bytes`):
   - cartridge ROM image (loaded `.ngc` file) → byte from file offset
   - unloaded cartridge flash window (`0x200000..0x3FFFFF` beyond the
     loaded file size) → erased `0xFF`
   - built-in readable cold-start image (see §2)
   - otherwise `unbacked` / `unmapped` / `out-of-file`

The overlay never invalidates the bus: writing then reading the same
address returns the overlay byte, and unmodified neighbouring addresses
in the same region keep returning their cold-start value.

## 2. Built-in readable cold-start image

`_build_builtin_readable_bytes()` builds the power-on byte map.

### 2.1 RAM / VRAM — zero at power-on

- `0x004000..0x006BFF` — Work RAM (10 752 bytes)
- `0x006C00..0x006FFF` — system RAM page (including system-reserved
  slices and user vector area)
- `0x007000..0x007FFF` — shared Z80 RAM
- `0x009000..0x0097FF` — SCR1 map
- `0x009800..0x009FFF` — SCR2 map
- `0x00A000..0x00BFFF` — character RAM

### 2.2 CPU on-chip I/O page (`0x000000..0x0000FF`) — **NOT zero**

> **Corrected 2026-07-10.** We used to cold-fill all 256 registers with `0x00`.
> That is simply wrong: the TMP95C061 on-chip registers have **documented reset
> values**. The full table now lives in `core/memory.py::_IO_PAGE_RESET_VALUES`,
> transcribed from the reference emulator's reset table (the same oracle used for
> the BIOS HLE work). Load-bearing entries:

| Addr | Register | Reset | Why it matters |
|------|----------|-------|----------------|
| `0x20` | TRUN     | `0x80` | prescaler running |
| `0x24` | T01MOD   | `0x03` | timer 0/1 clock sources |
| `0x60`/`0x61` | **ADREG0** | `0xFF`/`0xFF` | the **battery** reading — see below |
| `0x6D` | ADMOD    | `0x00` | A/D control (all flags clear) |
| `0x6F` | watchdog | `0x4E` | |
| `0x70`/`0x71` | INTxx | `0x02`/`0x32` | interrupt priority levels |
| `0x02..0x0C` | ports | `0xFF` | |
| `0xB8`/`0xB9` | | `0xAA`/`0xAA` | |

**The A/D result register (`0x60`/`0x61`) is the battery gauge.** Per the SNK SDK
(*"A/D converter — 1 channel (Power management.)"*, and *"Battery_voltage
(0x6f80) : Main power voltage … value range 0H~3FFH"*), the NGPC wires its single
channel to AN0. Per the Toshiba datasheet (Fig. 3.12 (3-1)), `0x60` bits 7-6 hold
the **lower 2** result bits and bits 5-0 are **unused and read as 1**, while `0x61`
holds the upper 8 — so the 16-bit word is `(result << 6) | 0x3F`, which is exactly
why the BIOS does `ldw WA,(0x60); srl 6`. We model a **healthy battery** (full
scale, `0xFFFF`).

> *Deliberate deviation from the oracle:* the reference emulator resets these to
> `0x00`. That is fine for it — it HLE's the BIOS and never reads the A/D. We run
> the **real** BIOS boot, where a zero reads as a **flat battery** and the BIOS
> powers the console off (`ld RW3,0; swi 1` = VECT_SHUTDOWN at `0xFF21E9`). See
> `specs/ADC.md`.

### 2.3 K2GE registers (`0x008000..0x008FFF`) — **not all zero either**

Mostly zero, with these documented power-on values (also corrected 2026-07-10):

| Addr | Reset | Meaning |
|------|-------|---------|
| `0x008000` | `0xC0` | **control: VBlank (bit 7) + HBlank (bit 6) interrupts ENABLED** |
| `0x008004` / `0x008005` | `0xFF` / `0xFF` | WSI.H / WSI.V — window = full screen |
| `0x008006` | `0xC6` | REF — frame rate (never modify) |
| `0x008118` | `0x80` | BGC on |
| `0x0083E0` / `0x0083E1` | `0xFF` / `0x0F` | default backdrop colour |
| `0x0083F0` / `0x0083F1` | `0xFF` / `0x0F` | default window colour |
| `0x008400` | `0xFF` | LED on |

A freshly-reset console therefore does **not** power on with interrupts disabled,
BGC off and a black backdrop — which is what our old all-zero map implied.

### 2.4 Other overrides

- `0x006F91` — HW_SYSTEM_MODE, set from the ROM header `mode_raw` byte
  (OS version / color-mono selector). The BIOS reads this at power-on
  and games branch on it.
- `0x006F80`/`0x006F81`, `0x006F84`, `0x006F87` — BIOS hand-off system-RAM values
  a cart sees at entry (cross-checked against the reference emulator and found
  universal across carts).
- When a `frame_state` is supplied, `0x008009` (RAS.V) and `0x008010`
  (2D status BLNK bit) track it — see `specs/FRAME_TIMING.md` § 3.5.

## 3. Write side

Writes do not go through this read path. They are handled by the
executor's runtime overlay (`memory_writes` field of `ExecutionResult`),
with `_check_writable_range` filtering out ROM, BIOS and unmapped
targets per `EXECUTE.md`. Writes to writable regions accumulate in the
overlay, which then shadows the cold-start image on subsequent reads.

## 4. Result statuses

- `ok` — bytes were resolved
- `mapped` — used at the probe layer (`AddressProbe.status`), not at
  the read layer
- `unmapped` — no region contains the address
- `unbacked` — region exists but is not yet backed (e.g. CPU I/O page,
  BIOS ROM with no image loaded)
- `out-of-file` — region is `CART_ROM_LOADED` but the computed file
  offset is past EOF (defensive check; should not happen in practice)

### 4b. Open bus — how the executor treats a non-`ok` read

HW-measured on real NGPC hardware (`hw_test_openbus` ROM, 2026-07-08):
the TLCS-900/H has **no bus-fault trap**. A read of an address that is outside every
mapped region returns **`0x0000`** (0x00BC0000 / 0x00100000 / 0x00C00000 all read
`0000`), a write to such an address is **silently discarded**, and neither **hangs**.

The read bus itself still returns `unmapped` for those addresses (§4). The executor
(`_read_runtime_bytes` / `_read_runtime_bytes_silent` in `core/execute.py`) resolves
the two non-`ok` classes differently, which is the HW-faithful policy:

- `unmapped` (`probe.region is None`) → **open bus**: return `0x00` and continue. This
  is the measured hardware behavior, not an invented value.
- `unbacked` (region exists but has no backing — e.g. the BIOS region `0xFF0000..`
  with no BIOS image attached) → **honest-stop** (`runtime-memory-unavailable`),
  because that is modelable state we simply do not have the bytes for. On-chip RAM is
  pre-initialised to `0x00` (M1d Phase 1) and unloaded cart flash reads as `0xFF`, so
  the only class that still honest-stops in practice is BIOS-without-a-BIOS-image.

Writes mirror this: `_check_writable_range` reports `write-discarded` for unmapped and
read-only targets (open bus / no WE signal) and execution continues (§3 / `EXECUTE.md`).

This is what lets a cartridge whose runtime dereferences a pointer into unmapped space
(e.g. `menu_test_project`'s `cp (XWA),IX` with XWA→0x00BC0002) keep running to its
render, exactly as it does on real hardware, instead of honest-stopping.

## 5. CLI

- `python ngpc_emu.py peek <rom> <address>`
- `python ngpc_emu.py peek <rom> <address> --count N`
- `python ngpc_emu.py memory-dump <rom> <address> [--count N] [--width W] [--seed-from state.json] [--json]`
  — hexdump-style multi-row inspector. Reads through the same read bus
  as `peek` and optionally overlays a savestate's writable cells.

Examples:
- ROM entry point fetch candidate:
  - `python ngpc_emu.py peek game.ngc 0x200040 --count 8`
- title bytes:
  - `python ngpc_emu.py peek game.ngc 0x200024 --count 12`
- cold-start Work RAM (returns 12 × 0x00 since M1d Phase 1):
  - `python ngpc_emu.py peek game.ngc 0x004000 --count 12`
- browse a captured run's writes:
  - `python ngpc_emu.py memory-dump game.ngc 0x004000 --seed-from sg_at_assert_fail.state.json`

## 6. Not implemented yet

- CPU I/O page reset values (timers, DMA, IRQ controller registers)
- BIOS image loading (`0xFF0000..0xFFFFFF` is mapped but unbacked)
- writable cart flash persistence (save block `0x3FA000..0x3FBFFF`
  shadows over the read bus through the overlay but is not persisted
  on disk yet)
- K2GE side-effect reads (the current backing is power-on-default
  zeros only)
- bus timing
