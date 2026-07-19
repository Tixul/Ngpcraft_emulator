# CPU State v1

Purpose:
- define the modeled CPU state container for TLCS-900/H on NGPC
- separate the architectural register model from reset-value knowledge
- carry the full SR shape so SR-touching opcodes round-trip honestly

Current source references:
- `../NgpCraft_toolchain/T900_DENSE_REF.md` section 31 (SR bit layout)
- `../NgpCraft_toolchain/NGPC_REVERSE_REFERENCE.md`
- `../../01_SDK/docs/ngpcspec.txt`

## 1. Architectural state

Modeled architecture:
- 32-bit registers:
  - `XWA`, `XBC`, `XDE`, `XHL`, `XIX`, `XIY`, `XIZ`, `XSP`
- `PC` (24-bit address bus, stored as int)
- `SR` raw 16-bit (optional cache; canonical state lives in the fields below)
- visible flag set `F` with all six TLCS-900/H ALU flags:
  - `SF` (sign)
  - `ZF` (zero)
  - `VF` (parity / overflow)
  - `HF` (half-carry)
  - `CF` (carry)
  - `NF` (add/subtract)
- alternate flag set `F'` with the same six-flag shape as `F`
- `iff_level` - interrupt mask level 0..7 (`SR[12:14]`)
- `iff_enabled` - derived legacy convenience: `True` iff `iff_level < 7`
- `rfp` - Register File Pointer 0..3 (`SR[8:10]`)
- TLCS-900/H control-register file subset:
  - `DMAS0..3` (32-bit DMA source registers)
  - `DMAD0..3` (32-bit DMA destination registers)
  - `DMAC0..3` (16-bit DMA count/control registers)
  - `DMAM0..3` (8-bit DMA mode registers)
  - `INTNEST` (16-bit interrupt nesting counter)

## 2. SR bit layout

TLCS-900/H NGPC silicon, per `T900_DENSE_REF.md` section 31:

```text
Bit  Name   Description
 0   C      Carry flag
 1   N      Add/Subtract (BCD)
 2   V      Parity / Overflow
 4   H      Half Carry
 6   Z      Zero
 7   S      Sign
 8-10 RFP   Register File Pointer (bank 0..3)
11   MAX    Maximum mode (always 1 on NGPC TLCS-900/H)
12-14 IFF   Interrupt mask level (0..7)
15   SYSM   System Mode (always 1 on NGPC TLCS-900/H)
```

Helpers in `core/cpu.py`:
- `encode_sr_from_state(state)` - encode the canonical fields into a 16-bit raw value, or `None` if a required field is unknown
- `decode_sr_to_fields(sr_raw)` - decode raw SR into `sf/zf/vf/hf/cf/nf/iff_level/rfp`

## 3. NGPC-specific simplifications vs. the wider TLCS-900 family

NGPC uses TLCS-900/H (TMP95C061). Per `T900_DENSE_REF.md` section 32:
- `MAX` mode is permanent
- system privilege only
- single IFF mask level
- vector-based interrupt method

The emulator therefore does not need:
- a supervisor/user mode bit
- a `min_mode` bit
- a second Z80-style `iff2` interrupt shadow field

## 4. Bootstrap truth level

- `PC` is derived from the ROM header entry point
- all other register values remain unknown until verified or explicitly seeded
- visible `flags`, shadow `alt_flags`, `iff_level`, and `rfp` are intentionally unknown at bootstrap
- TLCS-900/H control-register values are also intentionally unknown at bootstrap
- `--seed-zero-bank0` populates `XWA/XBC/XDE/XHL/XIX/XIY = 0` for the documented crt0 convenience path, but does not invent SR bits

## 5. Why the model is acceptable as v1

- the project has a stable CPU state shape that matches the documented SR layout
- SR-touching opcodes now round-trip without revisiting the data model
- reset-value accuracy can improve later without changing the container shape
- unknown fields remain `None` instead of being forged

## 6. What is modeled today

- `PUSH SR` (0x02) / `POP SR` (0x03):
  - push/pop the six visible flags plus `iff_level` and `rfp`
  - use `encode_sr_from_state` / `decode_sr_to_fields`
  - block honestly when SR shape or XSP is not known enough
- `LDF imm` (0x17):
  - writes `rfp` honestly
  - advances PC normally
- `EX F,F'` (0x16):
  - swaps the visible `flags` set with shadow `alt_flags`
  - if no incoming shadow set exists, degrades to an all-unknown `F'` instead of inventing bits
- `POP SR` / `RETI`:
  - restore `rfp` from the popped SR value
  - reload the visible `XWA/XBC/XDE/XHL` window from the restored bank
  - flush the outgoing visible core bank into the bank backing store first
- `LDC cr, r` / `LDC r, cr`:
  - real transfer support for the modeled TLCS-900/H control-register subset
  - unknown control-register reads stop honestly as `requires-known-control-register`
- IRQ delivery / `RETI`:
  - increment / decrement `INTNEST` when its current value is already known
  - keep `INTNEST` unknown instead of inventing a nesting depth when the incoming state did not model it
  - `EmulatorSession(..., apply_bios_handoff=True)` may seed `INTNEST = 0`
    at the BIOS hand-off layer; raw bootstrap state still leaves it unknown

## 7. What is still partial or not modeled

- automatic reset values for the TLCS-900/H control-register file
- DMA engine side effects behind `DMAS/DMAD/DMAC/DMAM`
- IRQ paths that would initialize `INTNEST` from an unknown prior state

## 8. CLI surfaces

- `python ngpc_emu.py cpu-info <rom>` - bootstrap CPU summary, now also exposing the modeled TLCS-900/H control-register file subset
- `python ngpc_emu.py registers <rom>` - rich CPU view, including visible `Flags`, shadow `Flags'`, and the TLCS-900/H control-register file subset
