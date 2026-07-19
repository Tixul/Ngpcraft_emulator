"""CPU state container tests for NgpCraft Emulator."""

from __future__ import annotations

import unittest
from dataclasses import replace

from core.cpu import (
    GeneralRegisters32,
    NgpcCpuState,
    StatusFlags,
    create_unknown_control_registers,
    create_bootstrap_cpu_state,
    decode_sr_to_fields,
    encode_sr_from_state,
)


class CpuStateTests(unittest.TestCase):
    def test_bootstrap_cpu_state_keeps_only_pc_known(self) -> None:
        cpu = create_bootstrap_cpu_state(0x00200040)

        self.assertEqual(cpu.pc, 0x00200040)
        self.assertIsNone(cpu.sr_raw)
        self.assertIsNone(cpu.register_bank)
        self.assertEqual(cpu.modeled_fields, ("PC", "architectural-register-set"))
        self.assertIsNone(cpu.regs.xwa)
        self.assertIsNone(cpu.regs.xbc)
        self.assertIsNone(cpu.regs.xde)
        self.assertIsNone(cpu.regs.xhl)
        self.assertIsNone(cpu.regs.xix)
        self.assertIsNone(cpu.regs.xiy)
        self.assertIsNone(cpu.regs.xiz)
        self.assertIsNone(cpu.regs.xsp)
        self.assertIsNone(cpu.flags.sf)
        self.assertIsNone(cpu.flags.zf)
        self.assertIsNone(cpu.flags.vf)
        self.assertIsNone(cpu.flags.hf)
        self.assertIsNone(cpu.flags.cf)
        assert cpu.control_registers is not None
        self.assertEqual(cpu.control_registers, create_unknown_control_registers())

    def test_bootstrap_cpu_state_initializes_nf_iff_level_rfp_unknown(self) -> None:
        cpu = create_bootstrap_cpu_state(0x00200040)

        self.assertIsNone(cpu.flags.nf)
        self.assertIsNotNone(cpu.alt_flags)
        assert cpu.alt_flags is not None
        self.assertIsNone(cpu.alt_flags.sf)
        self.assertIsNone(cpu.alt_flags.nf)
        self.assertIsNone(cpu.iff_enabled)
        self.assertIsNone(cpu.iff_level)
        self.assertIsNone(cpu.rfp)


class StatusRegisterEncodingTests(unittest.TestCase):
    """SR raw 16-bit encoding follows T900_DENSE_REF.md §31 bit layout."""

    def _state_with(
        self,
        *,
        sf: bool = False,
        zf: bool = False,
        vf: bool = False,
        hf: bool = False,
        cf: bool = False,
        nf: bool = False,
        iff_level: int = 0,
        rfp: int = 0,
    ) -> NgpcCpuState:
        return NgpcCpuState(
            pc=0x00200040,
            sr_raw=None,
            flags=StatusFlags(sf=sf, zf=zf, vf=vf, hf=hf, cf=cf, nf=nf),
            register_bank=None,
            regs=GeneralRegisters32(
                xwa=None, xbc=None, xde=None, xhl=None,
                xix=None, xiy=None, xiz=None, xsp=None,
            ),
            modeled_fields=("PC",),
            note="test fixture",
            iff_level=iff_level,
            rfp=rfp,
        )

    def test_encode_sr_individual_flag_bit_positions(self) -> None:
        for flag, expected_bit in (
            ("cf", 0),
            ("nf", 1),
            ("vf", 2),
            ("hf", 4),
            ("zf", 6),
            ("sf", 7),
        ):
            state = self._state_with(**{flag: True})
            sr = encode_sr_from_state(state)
            self.assertIsNotNone(sr, msg=flag)
            assert sr is not None
            # MAX (bit 11) and SYSM (bit 15) are always 1 on TLCS-900/H NGPC.
            flag_only = sr & ~((1 << 11) | (1 << 15))
            self.assertEqual(
                flag_only,
                1 << expected_bit,
                msg=f"{flag} should map to SR bit {expected_bit}, got 0x{flag_only:04X}",
            )

    def test_encode_sr_includes_max_and_sysm_always_set(self) -> None:
        state = self._state_with()
        sr = encode_sr_from_state(state)
        self.assertIsNotNone(sr)
        assert sr is not None
        self.assertTrue(sr & (1 << 11), "MAX bit must be 1 on TLCS-900/H NGPC")
        self.assertTrue(sr & (1 << 15), "SYSM bit must be 1 on TLCS-900/H NGPC")

    def test_encode_sr_iff_level_in_bits_12_to_14(self) -> None:
        state = self._state_with(iff_level=7)
        sr = encode_sr_from_state(state)
        self.assertIsNotNone(sr)
        assert sr is not None
        self.assertEqual((sr >> 12) & 0b111, 7)

    def test_encode_sr_rfp_in_bits_8_to_10(self) -> None:
        state = self._state_with(rfp=2)
        sr = encode_sr_from_state(state)
        self.assertIsNotNone(sr)
        assert sr is not None
        self.assertEqual((sr >> 8) & 0b11, 2)

    def test_encode_sr_returns_none_when_any_field_unknown(self) -> None:
        complete = self._state_with()
        partial = replace(complete, iff_level=None)
        self.assertIsNone(encode_sr_from_state(partial))
        partial = replace(complete, rfp=None)
        self.assertIsNone(encode_sr_from_state(partial))
        partial = replace(complete, flags=replace(complete.flags, nf=None))
        self.assertIsNone(encode_sr_from_state(partial))

    def test_decode_sr_roundtrip_with_all_fields(self) -> None:
        state = self._state_with(
            sf=True, zf=False, vf=True, hf=False, cf=True, nf=True,
            iff_level=5, rfp=3,
        )
        sr = encode_sr_from_state(state)
        self.assertIsNotNone(sr)
        assert sr is not None
        fields = decode_sr_to_fields(sr)
        self.assertEqual(fields["sf"], True)
        self.assertEqual(fields["zf"], False)
        self.assertEqual(fields["vf"], True)
        self.assertEqual(fields["hf"], False)
        self.assertEqual(fields["cf"], True)
        self.assertEqual(fields["nf"], True)
        self.assertEqual(fields["iff_level"], 5)
        self.assertEqual(fields["rfp"], 3)


if __name__ == "__main__":
    unittest.main()
