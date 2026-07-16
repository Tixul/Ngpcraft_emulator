"""Minimal CPU state container for NgpCraft Emulator."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class GeneralRegisters32:
    """Architectural 32-bit general registers."""

    xwa: int | None
    xbc: int | None
    xde: int | None
    xhl: int | None
    xix: int | None
    xiy: int | None
    xiz: int | None
    xsp: int | None


@dataclass(frozen=True)
class BankedByteRegisters:
    """TLCS-900/H banked byte-register file slice for XWA..XHL.

    Stores the 16 byte slots addressable by the C7 explicit-bank / previous-bank
    register codes:
      A/W/QA/QW, C/B/QC/QB, E/D/QE/QD, L/H/QL/QH

    Each element is one byte (`0..255`) or `None` when that byte is still
    unknown. The order matches the Toshiba `r8_names` table for 0x00..0x0F.
    """

    slots: tuple[int | None, ...]


@dataclass(frozen=True)
class StatusFlags:
    """TLCS-900/H status flag subset modeled by the emulator.

    Layout follows `T900_DENSE_REF.md` §31 (SR register bits):
      - C (bit 0), N (bit 1), V (bit 2), H (bit 4), Z (bit 6), S (bit 7).

    `nf` is the Add/Subtract flag (set to 1 after subtractive ops, 0 after
    additive/logical ops).  It is not consumed by any condition code on
    TLCS-900/H but is required for honest `PUSH SR` / `POP SR` round-trips
    and for BCD-aware future work.
    """

    sf: bool | None
    zf: bool | None
    vf: bool | None
    hf: bool | None
    cf: bool | None
    nf: bool | None = None


@dataclass(frozen=True)
class Tlcs900ControlRegisters:
    """Modeled TLCS-900/H control-register file subset.

    The current emulator only needs the architecturally visible register file
    shape so `LDC` can move values honestly between general registers and the
    CPU control-register namespace. Unknown values remain `None`.
    """

    dmas: tuple[int | None, ...]
    dmad: tuple[int | None, ...]
    dmac: tuple[int | None, ...]
    dmam: tuple[int | None, ...]
    intnest: int | None


@dataclass(frozen=True)
class NgpcCpuState:
    """Current modeled CPU state.

    `iff_level` is the canonical 3-bit interrupt mask (`SR[12:14]`,
    0..7 on TLCS-900/H).  `iff_enabled` is kept as a derived legacy
    convenience: `True` means level < 7 (some interrupts permitted),
    `False` means level == 7 (all maskable interrupts blocked).  When
    both are set on construction, `iff_level` wins and `iff_enabled`
    is overwritten to match.

    `rfp` is the 2-bit Register File Pointer (`SR[8:10]`, bank 0..3).
    """

    pc: int
    sr_raw: int | None
    flags: StatusFlags
    register_bank: int | None
    regs: GeneralRegisters32
    modeled_fields: tuple[str, ...]
    note: str
    iff_enabled: bool | None = None
    iff_level: int | None = None
    rfp: int | None = None
    register_banks: tuple[BankedByteRegisters, ...] | None = None
    alt_flags: StatusFlags | None = None
    control_registers: Tlcs900ControlRegisters | None = None


def create_unknown_control_registers() -> Tlcs900ControlRegisters:
    """Return the current unknown TLCS-900/H control-register file shape."""
    return Tlcs900ControlRegisters(
        dmas=(None, None, None, None),
        dmad=(None, None, None, None),
        dmac=(None, None, None, None),
        dmam=(None, None, None, None),
        intnest=None,
    )


def create_bootstrap_cpu_state(entry_point: int) -> NgpcCpuState:
    """Create the current partial bootstrap CPU state."""
    return NgpcCpuState(
        pc=entry_point,
        sr_raw=None,
        flags=StatusFlags(sf=None, zf=None, vf=None, hf=None, cf=None, nf=None),
        register_bank=None,
        regs=GeneralRegisters32(
            xwa=None,
            xbc=None,
            xde=None,
            xhl=None,
            xix=None,
            xiy=None,
            xiz=None,
            xsp=None,
        ),
        modeled_fields=("PC", "architectural-register-set"),
        note=(
            "Architectural CPU state container exists, but reset values are still partial. "
            "Only PC is currently derived from the ROM header. Other register values, SR, "
            "flags and active register bank remain unknown until verified."
        ),
        register_banks=None,
        alt_flags=StatusFlags(sf=None, zf=None, vf=None, hf=None, cf=None, nf=None),
        control_registers=create_unknown_control_registers(),
    )


# SR (Status Register) layout on TLCS-900/H per T900_DENSE_REF.md §31:
#   bit  0 : C   (carry)
#   bit  1 : N   (add/subtract, BCD)
#   bit  2 : V   (parity / overflow)
#   bit  4 : H   (half carry)
#   bit  6 : Z   (zero)
#   bit  7 : S   (sign)
#   bits 8-10 : RFP (register file pointer, 0..3)
#   bit 11 : MAX (always 1 on NGPC TLCS-900/H)
#   bits 12-14: IFF (interrupt mask level, 0..7)
#   bit 15 : SYSM (always 1 on NGPC, System mode only)

SR_BIT_CF = 0
SR_BIT_NF = 1
SR_BIT_VF = 2
SR_BIT_HF = 4
SR_BIT_ZF = 6
SR_BIT_SF = 7
SR_BIT_RFP_SHIFT = 8
SR_BIT_RFP_MASK = 0b11 << SR_BIT_RFP_SHIFT
SR_BIT_MAX = 11
SR_BIT_IFF_SHIFT = 12
SR_BIT_IFF_MASK = 0b111 << SR_BIT_IFF_SHIFT
SR_BIT_SYSM = 15


def encode_f_from_flags(flags: StatusFlags) -> int | None:
    """Encode the low 8-bit F register from the modeled flag subset."""
    if (
        flags.cf is None
        or flags.nf is None
        or flags.vf is None
        or flags.hf is None
        or flags.zf is None
        or flags.sf is None
    ):
        return None
    value = 0
    if flags.cf:
        value |= 1 << SR_BIT_CF
    if flags.nf:
        value |= 1 << SR_BIT_NF
    if flags.vf:
        value |= 1 << SR_BIT_VF
    if flags.hf:
        value |= 1 << SR_BIT_HF
    if flags.zf:
        value |= 1 << SR_BIT_ZF
    if flags.sf:
        value |= 1 << SR_BIT_SF
    return value & 0xFF


def decode_f_to_flags(f_raw: int) -> StatusFlags:
    """Decode the low 8-bit F register into the modeled flag subset."""
    return StatusFlags(
        sf=bool(f_raw & (1 << SR_BIT_SF)),
        zf=bool(f_raw & (1 << SR_BIT_ZF)),
        vf=bool(f_raw & (1 << SR_BIT_VF)),
        hf=bool(f_raw & (1 << SR_BIT_HF)),
        cf=bool(f_raw & (1 << SR_BIT_CF)),
        nf=bool(f_raw & (1 << SR_BIT_NF)),
    )


def encode_sr_from_state(state: NgpcCpuState) -> int | None:
    """Encode the SR 16-bit raw value from the modeled CPU fields.

    Returns None if any required field is unknown.  All six flags
    (`sf/zf/vf/hf/cf/nf`), `iff_level` and `rfp` must be modeled for
    the encoded value to be meaningful.  `MAX` and `SYSM` are always
    set to 1 on TLCS-900/H NGPC silicon.
    """
    f = state.flags
    if (
        f.cf is None
        or f.nf is None
        or f.vf is None
        or f.hf is None
        or f.zf is None
        or f.sf is None
        or state.iff_level is None
        or state.rfp is None
    ):
        return None
    sr = 0
    if f.cf:
        sr |= 1 << SR_BIT_CF
    if f.nf:
        sr |= 1 << SR_BIT_NF
    if f.vf:
        sr |= 1 << SR_BIT_VF
    if f.hf:
        sr |= 1 << SR_BIT_HF
    if f.zf:
        sr |= 1 << SR_BIT_ZF
    if f.sf:
        sr |= 1 << SR_BIT_SF
    sr |= (state.rfp & 0b11) << SR_BIT_RFP_SHIFT
    sr |= 1 << SR_BIT_MAX
    sr |= (state.iff_level & 0b111) << SR_BIT_IFF_SHIFT
    sr |= 1 << SR_BIT_SYSM
    return sr


def decode_sr_to_fields(sr_raw: int) -> dict[str, int | bool]:
    """Decode a SR 16-bit raw value into individual fields.

    Returns a dict with keys `sf`, `zf`, `vf`, `hf`, `cf`, `nf`,
    `iff_level`, `rfp`.  `MAX` and `SYSM` are not returned: on
    TLCS-900/H NGPC they are read as 1 and not separately modeled.
    """
    return {
        "cf": bool(sr_raw & (1 << SR_BIT_CF)),
        "nf": bool(sr_raw & (1 << SR_BIT_NF)),
        "vf": bool(sr_raw & (1 << SR_BIT_VF)),
        "hf": bool(sr_raw & (1 << SR_BIT_HF)),
        "zf": bool(sr_raw & (1 << SR_BIT_ZF)),
        "sf": bool(sr_raw & (1 << SR_BIT_SF)),
        "rfp": (sr_raw & SR_BIT_RFP_MASK) >> SR_BIT_RFP_SHIFT,
        "iff_level": (sr_raw & SR_BIT_IFF_MASK) >> SR_BIT_IFF_SHIFT,
    }
