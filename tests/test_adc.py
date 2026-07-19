"""TMP95C061 A/D converter (the NGPC battery gauge).

Every expectation here is quoted from the Toshiba datasheet -- see core/adc.py.
"""

from __future__ import annotations

import unittest

from core.adc import (
    ADC_CYCLES_HIGH_SPEED,
    ADC_CYCLES_LOW_SPEED,
    ADC_FULL_SCALE,
    ADMOD_ADBF,
    ADMOD_ADCS,
    ADMOD_ADDRESS,
    ADMOD_ADS,
    ADMOD_EOCF,
    ADMOD_REPET,
    ADREG0_HIGH_ADDRESS,
    ADREG0_LOW_ADDRESS,
    IRQ_VECTOR_INDEX_INTAD,
    AdcController,
    encode_adreg,
)


class AdregEncodingTests(unittest.TestCase):
    def test_full_scale_sets_all_bits_and_unused_bits_read_one(self) -> None:
        low, high = encode_adreg(ADC_FULL_SCALE)
        self.assertEqual(high, 0xFF)  # upper 8 bits
        self.assertEqual(low, 0xFF)  # (0b11 << 6) | 0x3F
        # The BIOS recovers the 10-bit value with `srl 6`.
        self.assertEqual(((high << 8) | low) >> 6, ADC_FULL_SCALE)

    def test_unused_low_bits_always_read_one(self) -> None:
        low, high = encode_adreg(0x0000)
        self.assertEqual(high, 0x00)
        self.assertEqual(low, 0x3F)  # bits 5-0 read as 1 even for a zero result
        self.assertEqual(((high << 8) | low) >> 6, 0x0000)

    def test_arbitrary_value_round_trips_through_srl_6(self) -> None:
        for value in (0x000, 0x001, 0x1D3, 0x2AB, 0x3FE, 0x3FF):
            low, high = encode_adreg(value)
            self.assertEqual(((high << 8) | low) >> 6, value)


class AdcConversionTests(unittest.TestCase):
    def test_idle_converter_does_nothing(self) -> None:
        adc = AdcController()
        updates, raised = adc.tick(100, {ADMOD_ADDRESS: 0x00})
        self.assertEqual(updates, {})
        self.assertFalse(raised)

    def test_writing_ads_starts_conversion_and_sets_busy(self) -> None:
        # Datasheet: "A/D conversion starts when ADMOD <ADS> is written 1. ...
        # the busy flag ADMOD <ADBF> ... will be set to 1." ADS always reads 0.
        adc = AdcController()
        memory = {ADMOD_ADDRESS: ADMOD_ADS}
        updates, raised = adc.tick(0, memory)
        self.assertFalse(raised)
        admod = updates[ADMOD_ADDRESS]
        self.assertTrue(admod & ADMOD_ADBF)  # BUSY
        self.assertFalse(admod & ADMOD_ADS)  # ADS reads back 0

    def test_conversion_completes_after_160_states_and_raises_intad(self) -> None:
        adc = AdcController()
        memory = {ADMOD_ADDRESS: ADMOD_ADS}
        memory.update(adc.tick(0, memory)[0])

        # Still busy one cycle short of the high-speed conversion time.
        updates, raised = adc.tick(ADC_CYCLES_HIGH_SPEED - 1, memory)
        self.assertFalse(raised)
        self.assertEqual(updates, {})

        updates, raised = adc.tick(1, memory)
        self.assertTrue(raised)  # INTAD
        memory.update(updates)
        # Result published to ADREG0, EOCF set, BUSY cleared.
        self.assertEqual(memory[ADREG0_HIGH_ADDRESS], 0xFF)
        self.assertEqual(memory[ADREG0_LOW_ADDRESS], 0xFF)
        self.assertTrue(memory[ADMOD_ADDRESS] & ADMOD_EOCF)
        self.assertFalse(memory[ADMOD_ADDRESS] & ADMOD_ADBF)
        # And that is exactly the battery value the BIOS caches at 0x6F80.
        word = memory[ADREG0_LOW_ADDRESS] | (memory[ADREG0_HIGH_ADDRESS] << 8)
        self.assertEqual(word >> 6, ADC_FULL_SCALE)

    def test_low_speed_mode_takes_twice_as_long(self) -> None:
        adc = AdcController()
        memory = {ADMOD_ADDRESS: ADMOD_ADS | ADMOD_ADCS}
        memory.update(adc.tick(0, memory)[0])
        _, raised = adc.tick(ADC_CYCLES_HIGH_SPEED, memory)
        self.assertFalse(raised)  # not done yet at the high-speed time
        _, raised = adc.tick(ADC_CYCLES_LOW_SPEED - ADC_CYCLES_HIGH_SPEED, memory)
        self.assertTrue(raised)

    def test_repeat_mode_starts_the_next_conversion_immediately(self) -> None:
        adc = AdcController()
        memory = {ADMOD_ADDRESS: ADMOD_ADS | ADMOD_REPET}
        memory.update(adc.tick(0, memory)[0])
        updates, raised = adc.tick(ADC_CYCLES_HIGH_SPEED, memory)
        self.assertTrue(raised)
        memory.update(updates)
        # Repeat mode: busy again straight away.
        self.assertTrue(memory[ADMOD_ADDRESS] & ADMOD_ADBF)
        _, raised = adc.tick(ADC_CYCLES_HIGH_SPEED, memory)
        self.assertTrue(raised)  # and it completes again

    def test_intad_is_hardware_vector_28(self) -> None:
        # Toshiba Table 3.3 (1): INTAD "A/D conversion completion" has vector
        # value 0x0070 -> table entry 0x0070/4 = 28 at address 0xFFFF70. That is
        # precisely the slot the SNK BIOS fills with its battery handler.
        self.assertEqual(IRQ_VECTOR_INDEX_INTAD, 0x0070 // 4)
        self.assertEqual(IRQ_VECTOR_INDEX_INTAD, 28)


if __name__ == "__main__":
    unittest.main()
