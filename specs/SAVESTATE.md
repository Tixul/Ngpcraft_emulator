# Savestate v5

Purpose:
- define the on-disk format for an emulator machine-state snapshot
- keep the snapshot clearly distinct from cartridge-persistent saves
- document only the state the emulator actually models
- lock a format that can survive the future C++ core rewrite

Current source references:
- `../SAVE_POLICY.md`
- `../HARDWARE_COMPAT_POLICY.md`
- `CPU_STATE.md`
- `RESET_STATE.md`
- `EXECUTE.md`
- `ADDRESS_SPACE.md`

Current format version:
- `2026-07-01.v5`

Backward-compat versions currently accepted by the loader:
- `2026-05-25.v4`
- `2026-05-20.v3`
- `2026-05-20.v2`

`v5` adds `cpu.control_registers` (the modeled TLCS-900/H control-register
file subset).

## 1. Format envelope

Savestates are UTF-8 JSON with the following root shape:

```json
{
  "format": "ngpc-emu-savestate",
  "format_version": "2026-07-01.v5",
  "created_at_utc": "<ISO-8601 timestamp>",
  "emulator": {
    "project": "NgpCraft_emulator",
    "prototype": "python",
    "commit": null
  },
  "rom": { "...": "..." },
  "cpu": { "...": "..." },
  "memory": { "...": "..." },
  "quirks": { "...": "..." },
  "frame_state": { "...": "..." },
  "irq_state": { "...": "..." },
  "note": "<free-form operator note or null>"
}
```

Mandatory top-level fields:
- `format`
- `format_version`
- `rom`
- `cpu`
- `memory`

## 2. ROM identity

```json
"rom": {
  "path_when_saved": "<absolute path, informational only>",
  "file_size": 123456,
  "sha256": "<64-char hex digest>",
  "header_title": "<decoded cart title>",
  "header_entry_point": 2097216,
  "header_mode_raw": 16
}
```

Rules:
- matching is by `sha256`, never by filename
- loaders must reject a savestate when an explicitly supplied ROM hash does not match
- `path_when_saved` is informational only

## 3. CPU state

```json
"cpu": {
  "pc": 2097216,
  "register_bank": null,
  "sr_raw": null,
  "flags": {
    "sf": null,
    "zf": null,
    "vf": null,
    "hf": null,
    "cf": null,
    "nf": null
  },
  "alt_flags": {
    "sf": null,
    "zf": null,
    "vf": null,
    "hf": null,
    "cf": null,
    "nf": null
  },
  "registers": {
    "xwa": null,
    "xbc": null,
    "xde": null,
    "xhl": null,
    "xix": null,
    "xiy": null,
    "xiz": null,
    "xsp": null
  },
  "control_registers": {
    "dmas": [null, null, null, null],
    "dmad": [null, null, null, null],
    "dmac": [null, null, null, null],
    "dmam": [null, null, null, null],
    "intnest": null
  },
  "iff_enabled": null,
  "iff_level": null,
  "rfp": null,
  "register_banks": null
}
```

Rules:
- same logical shape as `NgpcCpuState` / `StatusFlags` / `GeneralRegisters32`
- unknown fields remain `null`; the loader must not silently replace them with zero
- `iff_level` is canonical; `iff_enabled` is a legacy convenience mirror
- `rfp` is the Register File Pointer
- `flags` is the visible TLCS-900/H flag set `F`
- `alt_flags` is the alternate TLCS-900/H flag set `F'`
- `control_registers` mirrors the currently modeled TLCS-900/H control-register file subset:
  - `dmas[0..3]` / `dmad[0..3]` are 32-bit DMA source/destination registers
  - `dmac[0..3]` are 16-bit DMA count/control registers
  - `dmam[0..3]` are 8-bit DMA mode registers
  - `intnest` is the 16-bit interrupt nesting counter
- when loading older `v3` / `v2` savestates that do not carry `alt_flags`, the loader restores all six shadow flags as unknown
- when loading older `v4` / `v3` / `v2` savestates that do not carry `control_registers`, the loader restores the whole control-register file as unknown
- `register_banks` is optional and, when present, is a 4-entry list of 16 byte slots per bank (`0..255` or `null`)

## 4. Memory

```json
"memory": {
  "writable_overlay": {
    "0x004000": 170,
    "0x004001": 187
  }
}
```

Rules:
- the overlay is the runtime writable overlay produced by the executor
- keys are padded hexadecimal addresses
- values are single bytes `0..255`
- only cells actually written during the session are stored

## 5. Quirk snapshot

```json
"quirks": {
  "database_version": "2026-04-22.v3",
  "matched_on_last_step": null
}
```

Rules:
- records which `core/quirks_db.json` version was active
- optionally stores the last matched quirk payload for diagnostics

## 6. Timing and IRQ state

```json
"frame_state": {
  "scanline": 0,
  "frame_count": 0
},
"irq_state": {
  "pending_mask": 0
}
```

Rules:
- `frame_state` carries the modeled K2GE timing frontier used by the current timing pipeline
- `irq_state.pending_mask` carries the currently pending IRQ bits in the minimal IRQ model
- when loading older `v2` savestates that omit these sections, the loader restores documented reset values

## 7. Versioning rule

Version history:
- `2026-04-22.v1` - initial shipped format
- `2026-05-20.v2` - adds `iff_level`, `rfp`, `flags.nf`
- `2026-05-20.v3` - adds `frame_state`
- `2026-05-25.v4` - adds `cpu.alt_flags`
- `2026-07-01.v5` - adds `cpu.control_registers`

Rules:
- loaders must reject any payload whose `format` is not `ngpc-emu-savestate`
- loaders must reject unknown `format_version` values
- bounded backward compatibility is explicit, not implicit:
  - current implementation accepts `v5`, `v4`, `v3`, and `v2`
  - anything else is rejected
- moving from the Python prototype to a future C++ core does not by itself justify a format break

## 8. CLI status

This format is already used by the implemented CLI:
- `python ngpc_emu.py savestate save <rom> <state.json>`
- `python ngpc_emu.py savestate load <state.json>`
- `python ngpc_emu.py run-until-exec <rom> <target_pc> --seed-from <state.json>`
- `step-exec`, `run-steps`, `trace-exec`, named checkpoints, and named sessions all reuse the same savestate payload shape

## 9. Relation to other policies

- `SAVE_POLICY.md` governs cartridge-persistent saves; a savestate is not a cartridge save
- `HARDWARE_COMPAT_POLICY.md` still applies: a savestate must not paper over behavior the reference emulator would honestly refuse
- runtime traces and event logs are separate formats; a savestate is a point-in-time snapshot, not a history

## 10. Not modeled yet

These remain intentionally out of scope until the corresponding subsystem is first-class in the emulator:
- cycle-exact interrupt latency and scheduler internals beyond `pending_mask`
- DMA channel progress
- full raster/video internals beyond `frame_state`
- audio generator state
- timer reload internals
- BIOS scratchpad state the emulator does not yet model
- any additional data required for a cycle-exact reload
