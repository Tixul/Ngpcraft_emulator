"""TMP95C061 A/D converter (the NGPC battery gauge).

Everything here is transcribed from the Toshiba TMP95C061 datasheet -- nothing
is inferred. The NGPC uses the A/D converter for exactly one thing: measuring
the main battery voltage. The official SNK SDK says so outright
(`01_SDK/docs/SysPro.txt`: "A/D converter -- 1 channel (Power management.)"),
and `SysWork.txt` names the result: "Battery_voltage (0x6f80) : Main power
voltage ... measured periodically by the system. The value range is 0H~3FFH."

Why this matters: the BIOS's power-on check reads that cached value and powers
the console off if the battery looks flat --

    0xFF21DC  ld WA, (0x6F80)    ; cached battery reading
    0xFF21E0  cp WA, 0x01D3      ; below the low-battery threshold?
    0xFF21E4  jr NC, 0xFF21EA    ; healthy -> skip
    0xFF21E6  ld RW3, 0          ; RW3 = 0 = VECT_SHUTDOWN
    0xFF21E9  swi 1              ; power off

-- and it is the A/D COMPLETION INTERRUPT handler that refills 0x6F80 from the
converter. Without a working ADC the BIOS boot shuts itself down.

Datasheet facts used below
--------------------------
ADMOD (0x006D), all bits 0 after reset -- Figure 3.12 (2):
    bit 7  EOCF   conversion End Flag   (R)   1 = END
    bit 6  ADBF   conversion Busy Flag  (R)   1 = BUSY
    bit 5  REPET  0 = single, 1 = repeat      (R/W)
    bit 4  SCAN   0 = fixed channel, 1 = scan (R/W)
    bit 3  ADCS   speed: 0 = high (160 states), 1 = low (320 states)  (R/W)
    bit 2  ADS    write 1 = START conversion. "Always read as 0."     (R/W)
    bits 1-0 ADCH analog input channel (00 = AN0)                     (R/W)

ADREG0 (0x0060 / 0x0061) -- Figure 3.12 (3-1):
    0x0060 ADREG04L: bits 7-6 = lower 2 bits of the AN0 result;
                     bits 5-0 unused, READ AS 1.
    0x0061 ADREG04H: upper 8 bits of the AN0 result.
    => the 16-bit word at 0x60 is `(result << 6) | 0x3F`, which is why the BIOS
       does `ldw WA,(0x60); srl 6`.

Operation (datasheet 3.12.1):
  * Writing ADS = 1 starts a conversion; ADBF goes to 1.
  * On completion (single mode): EOCF -> 1, ADBF -> 0, and the INTAD interrupt
    is raised. INTAD is vector 0x0070 (Table 3.3 (1)), i.e. hardware vector
    table entry 28 at 0xFFFF70 -- which is exactly the handler the SNK BIOS
    installs there.
  * Reading a result register clears EOCF.
  * In repeat mode the registers are updated after every conversion.

Conversion time: high-speed mode is "160 States = 12.8 us (at 25 MHz)". 160
states spanning 12.8 us at 25 MHz means one state is two clocks, so a conversion
takes 320 CPU clocks (640 in low-speed mode) -- independent of the clock rate.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from core.memory import io_reset_value

ADMOD_ADDRESS = 0x00006D
ADREG0_LOW_ADDRESS = 0x000060
ADREG0_HIGH_ADDRESS = 0x000061

ADMOD_EOCF = 0x80
ADMOD_ADBF = 0x40
ADMOD_REPET = 0x20
ADMOD_SCAN = 0x10
ADMOD_ADCS = 0x08
ADMOD_ADS = 0x04

# 160 states high-speed / 320 states low-speed, at 2 clocks per state.
ADC_CYCLES_HIGH_SPEED = 160 * 2
ADC_CYCLES_LOW_SPEED = 320 * 2

# The A/D completion interrupt: vector value 0x0070 (Table 3.3 (1)) => entry
# 0x70 / 4 = 28 in the hardware vector table based at 0xFFFF00.
IRQ_VECTOR_INDEX_INTAD = 28

# 10-bit full scale. A healthy NGPC battery reads at/near the top of the range;
# an emulator has no real cell, so we model a good one. (A flat reading would
# make the BIOS power the console off -- see the module docstring.)
ADC_FULL_SCALE = 0x03FF


def encode_adreg(result: int) -> tuple[int, int]:
    """Split a 10-bit result into (ADREG0L, ADREG0H) exactly as the chip does."""
    result &= ADC_FULL_SCALE
    low = ((result & 0x03) << 6) | 0x3F  # unused bits 5-0 read as 1
    high = (result >> 2) & 0xFF
    return low, high


@dataclass
class AdcController:
    """A/D converter state. Owned by `EmulatorSession` so a conversion survives
    across step batches; ticked with the cycles each instruction consumed."""

    battery_value: int = ADC_FULL_SCALE
    _cycles_remaining: int = 0
    _busy: bool = False

    def reset(self) -> None:
        self._cycles_remaining = 0
        self._busy = False

    def tick(
        self, cycles: int, memory: dict[int, int]
    ) -> tuple[dict[int, int], bool]:
        """Advance the converter by `cycles` CPU clocks.

        Returns `(memory_updates, intad_raised)`. `memory` is read (never
        mutated) to sample ADMOD; the caller merges the returned updates into the
        writable overlay.
        """
        admod = memory.get(ADMOD_ADDRESS, io_reset_value(ADMOD_ADDRESS)) & 0xFF
        updates: dict[int, int] = {}

        if not self._busy:
            if not (admod & ADMOD_ADS):
                return updates, False
            # Software wrote ADS=1: start a conversion. ADS always reads back 0,
            # and the busy flag goes up.
            self._busy = True
            self._cycles_remaining = (
                ADC_CYCLES_LOW_SPEED if (admod & ADMOD_ADCS) else ADC_CYCLES_HIGH_SPEED
            )
            admod = (admod & ~ADMOD_ADS) | ADMOD_ADBF
            updates[ADMOD_ADDRESS] = admod & 0xFF
            return updates, False

        self._cycles_remaining -= max(cycles, 0)
        if self._cycles_remaining > 0:
            return updates, False

        # Conversion complete: publish the result, flip the flags, raise INTAD.
        self._busy = False
        low, high = encode_adreg(self.battery_value)
        updates[ADREG0_LOW_ADDRESS] = low
        updates[ADREG0_HIGH_ADDRESS] = high
        admod = (admod & ~ADMOD_ADBF) | ADMOD_EOCF
        if admod & ADMOD_REPET:
            # Repeat mode: immediately begin the next conversion.
            self._busy = True
            self._cycles_remaining = (
                ADC_CYCLES_LOW_SPEED if (admod & ADMOD_ADCS) else ADC_CYCLES_HIGH_SPEED
            )
            admod |= ADMOD_ADBF
        updates[ADMOD_ADDRESS] = admod & 0xFF
        return updates, True
