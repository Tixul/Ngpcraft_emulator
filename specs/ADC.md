# A/D Converter — the NGPC battery gauge

`core/adc.py` · tests `tests/test_adc.py` · landed 2026-07-10 (DEVLOG pass 185)

Every value in this spec is **quoted from a manufacturer document**. Nothing is
inferred. This subsystem is also the one piece with **no prior art to lean on** —
the A/D converter is routinely left unmodelled entirely — so the Toshiba
datasheet and the SNK SDK documents are the only sources used here.

## 1. Why it exists

The NGPC uses its A/D converter for exactly one thing — measuring the battery:

- SNK SDK (`01_SDK/docs/SysPro.txt`): *"A/D converter — **1 channel (Power
  management.)**"*
- SNK SDK (`01_SDK/docs/SysWork.txt`): *"**Battery_voltage (0x6f80)** : Main power
  voltage … the voltage of the main battery **measured periodically by the
  system**. The value range is **0H~3FFH**."*
- SNK SDK (`01_SDK/docs/SysWork.txt`): *"**Shutdown request from low voltage of
  main power supply (battery)**"*

And it is load-bearing, because the BIOS's power-on check reads that cached value
and **powers the console off** if the battery looks flat:

```asm
0xFF21DC  ld WA, (0x6F80)    ; cached battery reading
0xFF21E0  cp WA, 0x01D3      ; below the low-battery threshold?
0xFF21E4  jr NC, 0xFF21EA    ; healthy -> skip
0xFF21E6  ld RW3, 0          ; RW3 = 0 = VECT_SHUTDOWN
0xFF21E9  swi 1              ; POWER OFF
```

The BIOS clears `0x6F80` during init and refills it **inside the A/D completion
interrupt handler**. So without a working A/D converter the real BIOS boot shuts
itself down. (This was found by our own honest-stop firing on the SWI — see
DEVLOG pass 183. It also independently confirms the SWI-1 vector mapping: the
BIOS literally writes `ld RW3, 0` before `swi 1`.)

## 2. Registers (Toshiba TMP95C061 datasheet)

### ADMOD — A/D control register, `0x00006D` (Figure 3.12 (2))

All bits are `0` after reset.

| bit | name | R/W | meaning |
|----:|------|-----|---------|
| 7 | `EOCF`  | R   | conversion End Flag — `1` = END |
| 6 | `ADBF`  | R   | conversion Busy Flag — `1` = BUSY |
| 5 | `REPET` | R/W | `0` = single mode, `1` = repeat mode |
| 4 | `SCAN`  | R/W | `0` = fixed channel, `1` = channel scan |
| 3 | `ADCS`  | R/W | speed: `0` = high (160 states), `1` = low (320 states) |
| 2 | `ADS`   | R/W | **write `1` = START conversion**. *"Always read as 0."* |
| 1-0 | `ADCH1,0` | R/W | analog input channel (`00` = AN0) |

### ADREG0 — A/D result for AN0, `0x000060` / `0x000061` (Figure 3.12 (3-1))

| addr | name | contents |
|------|------|----------|
| `0x60` | `ADREG04L` | bits 7-6 = **lower 2** bits of the result; bits 5-0 **unused, read as 1** |
| `0x61` | `ADREG04H` | **upper 8** bits of the result |

⇒ the 16-bit little-endian word at `0x60` is **`(result << 6) | 0x3F`**.

That is precisely why the BIOS recovers the 10-bit value with
`ldw WA,(0x60); srl 6`. A healthy full-scale battery (`0x3FF`) reads `0xFFFF`.

## 3. Operation (datasheet § 3.12.1)

1. Writing `ADS = 1` starts a conversion; `ADBF` goes to `1`.
2. On completion (single mode): `EOCF → 1`, `ADBF → 0`, and **INTAD** is raised.
3. Reading a result register clears `EOCF`. Reading the upper byte (`ADREGxH`)
   clears the INTAD request flag.
4. In repeat mode the result registers are updated after every conversion.

**Conversion time.** The datasheet gives *"A/D High Speed Conversion Mode : 160
States = 12.8 µs (at 25 MHz)"*. 160 states spanning 12.8 µs at 25 MHz means one
state is **two clocks**, so a conversion costs **320 CPU clocks** (640 in
low-speed mode) — independent of the clock rate.

## 4. The INTAD interrupt

Toshiba **Table 3.3 (1), TMP95C061 Interrupt Table**:

| source | vector value | address | HDMA start vector |
|--------|--------------|---------|-------------------|
| `INTAD` — *A/D conversion completion* | `0x0070` | `0xFFFF70` | `1CH` |

⇒ **hardware vector index `0x0070 / 4 = 28`**.

That is exactly the slot the SNK BIOS fills with the handler that refills
`0x6F80`. (Before we had the datasheet, our reverse-engineering had labelled
`vec[28] @ 0xFFFF70` as "timer/ADC" — Toshiba confirms it is INTAD.)

Delivery goes through `try_deliver_pending_vector_irq` (see
`specs/FRAME_TIMING.md`): the level is **programmable** and read from `INTE0AD`
(`0x0070`), which the A/D **shares with the INT0 pin** — INT0 takes the low
nibble, the A/D the **high** nibble.

## 5. Model

`AdcController` is owned by `EmulatorSession` so a conversion in flight survives
across step batches, and is ticked in the run loop with the cycles each
instruction consumed:

```python
updates, intad_raised = adc.tick(cycles, memory)
```

`memory` is read (never mutated) to sample ADMOD; the caller merges `updates`
into the writable overlay, so the very next instruction sees the result and the
flags — exactly as on hardware.

`battery_value` defaults to full scale. An emulator has no real cell, so we model
a good one; a flat reading would make the BIOS power the console off.

## 6. Not modelled

- Channels AN1..AN3 and scan mode (the NGPC only wires AN0).
- The analog reference ladder / conversion accuracy (we publish an exact value).
