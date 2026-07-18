"""Emulator machine-state snapshot serialization (savestate v1).

This module implements `specs/SAVESTATE.md` — the first on-disk format for
an emulator machine-state snapshot.  It is deliberately narrow:

- CPU state mirrors the current `NgpcCpuState` container; unknown fields stay
  `None` instead of being forged.
- Memory is limited to the writable runtime overlay produced by the executor.
- ROM identity is carried as a SHA-256 content hash.  Loaders refuse any
  mismatching ROM regardless of filename.
- Subsystems that the emulator does not yet model (video / audio / DMA / IRQ
  / timers) are NOT captured.
"""

from __future__ import annotations

import ctypes
import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from core.cpu import (
    BankedByteRegisters,
    GeneralRegisters32,
    NgpcCpuState,
    StatusFlags,
    Tlcs900ControlRegisters,
    create_unknown_control_registers,
)
from core.quirks import KnownQuirkMatch, QuirkSource, load_known_quirk_database
from core.frame_timing import (
    FrameState,
    IrqState,
    initial_frame_state,
    initial_irq_state,
)
from core.rom import NgpcRomHeader

SAVESTATE_FORMAT = "ngpc-emu-savestate"
SAVESTATE_FORMAT_VERSION = "2026-07-01.v5"
# Older versions still loadable so existing fixtures + saved sessions
# don't break when a new build ships. Missing fields default to the
# documented post-reset value (e.g. `frame_state → initial_frame_state()`).
SAVESTATE_BACKWARD_COMPAT_VERSIONS = (
    "2026-05-25.v4",
    "2026-05-20.v3",
    "2026-05-20.v2",
)


# ⚡ THE SAVE STATE THE PLAYER ACTUALLY HAS.
#
# `ngpc_shell.py` (F2 / F4) writes this: a magic, the native core's CpuState struct
# verbatim, then the whole working image from 0x0000 -- the I/O page, work RAM, and all
# of video memory. It is not this module's JSON, and nothing could read it outside the
# app, which meant the one artefact a player can produce at the exact moment a bug
# happens was useless to every analysis tool. Now it is the same door.
SHELL_SAVESTATE_MAGIC = b"NGPCST01"
SHELL_SAVESTATE_MEM_LEN = 0x00C000


@dataclass(frozen=True)
class SavestateDocument:
    """In-memory view of a parsed savestate file."""

    format_version: str
    created_at_utc: str
    rom_sha256: str
    rom_file_size: int
    rom_header_title: str
    rom_header_entry_point: int
    rom_header_mode_raw: int
    cpu: NgpcCpuState
    writable_overlay: dict[int, int]
    quirk_database_version: str
    matched_on_last_step: KnownQuirkMatch | None
    note: str | None
    frame_state: FrameState
    irq_state: IrqState


def compute_rom_sha256(rom_path: Path) -> str:
    return hashlib.sha256(rom_path.read_bytes()).hexdigest()


def build_savestate_payload(
    *,
    rom_path: Path,
    rom_header: NgpcRomHeader,
    cpu: NgpcCpuState,
    writable_overlay: dict[int, int],
    matched_on_last_step: KnownQuirkMatch | None = None,
    note: str | None = None,
    created_at_utc: str | None = None,
    frame_state: FrameState | None = None,
    irq_state: IrqState | None = None,
) -> dict[str, object]:
    """Build a JSON-ready savestate payload at the current schema version.

    `frame_state` defaults to `initial_frame_state()` (post-reset
    scanline 0, frame_count 0) so every existing caller transparently
    upgrades to v3 — they emit a v3 payload with the documented HW
    reset frame state. Callers that have advanced the timing model
    (e.g. the `tick-frame` CLI) pass an explicit `FrameState`.
    """
    quirk_db = load_known_quirk_database()
    created = created_at_utc or datetime.now(timezone.utc).isoformat()
    effective_frame_state = frame_state or initial_frame_state()
    effective_irq_state = irq_state if irq_state is not None else initial_irq_state()
    return {
        "format": SAVESTATE_FORMAT,
        "format_version": SAVESTATE_FORMAT_VERSION,
        "created_at_utc": created,
        "emulator": {
            "project": "NgpCraft_emulator",
            "prototype": "python",
            "commit": None,
        },
        "rom": {
            "path_when_saved": str(rom_path),
            "file_size": rom_header.file_size,
            "sha256": compute_rom_sha256(rom_path),
            "header_title": rom_header.title,
            "header_entry_point": rom_header.entry_point,
            "header_mode_raw": rom_header.mode_raw,
        },
        "cpu": _cpu_to_savestate_payload(cpu),
        "memory": {
            "writable_overlay": {
                f"0x{addr:06X}": byte
                for addr, byte in sorted(writable_overlay.items())
            },
        },
        "quirks": {
            "database_version": quirk_db.database_version,
            "matched_on_last_step": _match_to_savestate_payload(matched_on_last_step),
        },
        "frame_state": {
            "scanline": effective_frame_state.scanline,
            "frame_count": effective_frame_state.frame_count,
        },
        "irq_state": {
            "pending_mask": effective_irq_state.pending_mask,
        },
        "note": note,
    }


def save_savestate(path: Path, payload: dict[str, object]) -> None:
    """Write a savestate payload to disk as UTF-8 JSON."""
    path.write_text(
        json.dumps(payload, indent=2, sort_keys=True),
        encoding="utf-8",
    )


def load_savestate(
    path: Path,
    *,
    expected_rom_path: Path | None = None,
) -> SavestateDocument:
    """Load and validate a savestate file.

    If `expected_rom_path` is provided, the loader computes its SHA-256 and
    rejects the savestate if the hash does not match `rom.sha256`.  Path is
    informational only; matching is always by content hash.

    ⚡ TWO FORMATS, ONE DOOR. The JSON below is this module's own; `NGPCST01` is
    what the PLAYER's emulator writes with F2, and that is the one a person
    actually has when they hit a bug. Every `--seed-from` consumer -- 28 CLI
    subcommands, and through them the inspector tools -- reaches the machine
    through this single function, so teaching it the player's format is what
    lets somebody hand over a save state taken one frame before a glitch and
    have the whole toolset open it. See `load_shell_savestate`.
    """
    if path.read_bytes()[: len(SHELL_SAVESTATE_MAGIC)] == SHELL_SAVESTATE_MAGIC:
        return load_shell_savestate(path, expected_rom_path=expected_rom_path)

    raw = json.loads(path.read_text(encoding="utf-8"))
    _validate_schema(raw, path)

    rom_section = raw["rom"]
    if expected_rom_path is not None:
        actual_hash = compute_rom_sha256(expected_rom_path)
        expected_hash = rom_section["sha256"]
        if actual_hash != expected_hash:
            raise ValueError(
                f"ROM hash mismatch: savestate at {path} was captured against "
                f"sha256 {expected_hash} but {expected_rom_path} is sha256 "
                f"{actual_hash}"
            )

    cpu_section = raw["cpu"]
    mem_section = raw["memory"]
    overlay = _parse_overlay(mem_section.get("writable_overlay") or {}, path)

    quirks_section = raw.get("quirks") or {}
    matched_raw = quirks_section.get("matched_on_last_step")
    matched_obj: KnownQuirkMatch | None = None
    if matched_raw:
        matched_obj = _savestate_payload_to_match(matched_raw)

    frame_state = _parse_frame_state(raw.get("frame_state"), path)
    irq_state = _parse_irq_state(raw.get("irq_state"), path)

    return SavestateDocument(
        format_version=raw["format_version"],
        created_at_utc=raw.get("created_at_utc", ""),
        rom_sha256=rom_section["sha256"],
        rom_file_size=rom_section["file_size"],
        rom_header_title=rom_section.get("header_title", ""),
        rom_header_entry_point=rom_section["header_entry_point"],
        rom_header_mode_raw=rom_section["header_mode_raw"],
        cpu=_savestate_payload_to_cpu(cpu_section),
        writable_overlay=overlay,
        quirk_database_version=quirks_section.get("database_version", ""),
        matched_on_last_step=matched_obj,
        note=raw.get("note"),
        frame_state=frame_state,
        irq_state=irq_state,
    )


def _parse_frame_state(raw: object, path: Path) -> FrameState:
    """Parse the `frame_state` section of a savestate payload.

    Backward compat: v2 savestates omit the field entirely → return
    the documented post-reset `initial_frame_state()`. v3 saves carry
    `{"scanline": int, "frame_count": int}`; any malformed shape
    raises with a clear path reference.
    """
    if raw is None:
        return initial_frame_state()
    if not isinstance(raw, dict):
        raise ValueError(
            f"Savestate at {path} has a non-object frame_state section."
        )
    scanline = raw.get("scanline", 0)
    frame_count = raw.get("frame_count", 0)
    if not isinstance(scanline, int) or not isinstance(frame_count, int):
        raise ValueError(
            f"Savestate at {path} frame_state.scanline/frame_count must be ints."
        )
    return FrameState(scanline=scanline, frame_count=frame_count)


def _parse_irq_state(raw: object, path: Path) -> IrqState:
    """Parse the `irq_state` section of a savestate payload.

    Backward compat: v2 saves and pre-3.2.2a v3 saves omit the field
    → return the documented post-reset `initial_irq_state()` (no
    pending IRQs). v3 saves carry `{"pending_mask": int}`.
    """
    if raw is None:
        return initial_irq_state()
    if not isinstance(raw, dict):
        raise ValueError(
            f"Savestate at {path} has a non-object irq_state section."
        )
    pending_mask = raw.get("pending_mask", 0)
    if not isinstance(pending_mask, int) or pending_mask < 0:
        raise ValueError(
            f"Savestate at {path} irq_state.pending_mask must be a "
            "non-negative int."
        )
    return IrqState(pending_mask=pending_mask)


def _validate_schema(raw: object, path: Path) -> None:
    if not isinstance(raw, dict):
        raise ValueError(f"Savestate at {path} is not a JSON object.")
    if raw.get("format") != SAVESTATE_FORMAT:
        raise ValueError(
            f"Unexpected savestate format {raw.get('format')!r} at {path}; "
            f"expected {SAVESTATE_FORMAT!r}."
        )
    seen_version = raw.get("format_version")
    if seen_version != SAVESTATE_FORMAT_VERSION and seen_version not in SAVESTATE_BACKWARD_COMPAT_VERSIONS:
        accepted = (SAVESTATE_FORMAT_VERSION, *SAVESTATE_BACKWARD_COMPAT_VERSIONS)
        raise ValueError(
            f"Unknown savestate format_version {seen_version!r} at {path}; "
            f"this build understands {accepted}."
        )
    for required in ("rom", "cpu", "memory"):
        if required not in raw:
            raise ValueError(
                f"Savestate at {path} is missing required field {required!r}."
            )


def _parse_overlay(raw_overlay: object, path: Path) -> dict[int, int]:
    if not isinstance(raw_overlay, dict):
        raise ValueError(
            f"Savestate at {path} has a non-object memory.writable_overlay."
        )
    overlay: dict[int, int] = {}
    for key, value in raw_overlay.items():
        if not isinstance(key, str) or not key.startswith("0x"):
            raise ValueError(
                f"Savestate at {path} overlay key {key!r} is not a 0x-prefixed "
                "hex address."
            )
        if not isinstance(value, int) or not 0 <= value <= 0xFF:
            raise ValueError(
                f"Savestate at {path} overlay value for {key!r} must be an "
                "integer byte 0..255."
            )
        overlay[int(key, 16)] = value
    return overlay


def _cpu_to_savestate_payload(cpu: NgpcCpuState) -> dict[str, object]:
    return {
        "pc": cpu.pc,
        "register_bank": cpu.register_bank,
        "register_banks": (
            [list(bank.slots) for bank in cpu.register_banks]
            if cpu.register_banks is not None
            else None
        ),
        "sr_raw": cpu.sr_raw,
        "iff_enabled": cpu.iff_enabled,
        "iff_level": cpu.iff_level,
        "rfp": cpu.rfp,
        "flags": {
            "sf": cpu.flags.sf,
            "zf": cpu.flags.zf,
            "vf": cpu.flags.vf,
            "hf": cpu.flags.hf,
            "cf": cpu.flags.cf,
            "nf": cpu.flags.nf,
        },
        "alt_flags": {
            "sf": None if cpu.alt_flags is None else cpu.alt_flags.sf,
            "zf": None if cpu.alt_flags is None else cpu.alt_flags.zf,
            "vf": None if cpu.alt_flags is None else cpu.alt_flags.vf,
            "hf": None if cpu.alt_flags is None else cpu.alt_flags.hf,
            "cf": None if cpu.alt_flags is None else cpu.alt_flags.cf,
            "nf": None if cpu.alt_flags is None else cpu.alt_flags.nf,
        },
        "registers": {
            "xwa": cpu.regs.xwa,
            "xbc": cpu.regs.xbc,
            "xde": cpu.regs.xde,
            "xhl": cpu.regs.xhl,
            "xix": cpu.regs.xix,
            "xiy": cpu.regs.xiy,
            "xiz": cpu.regs.xiz,
            "xsp": cpu.regs.xsp,
        },
        "control_registers": _control_registers_to_payload(cpu.control_registers),
    }


def load_shell_savestate(
    path: Path,
    *,
    expected_rom_path: Path | None = None,
) -> SavestateDocument:
    """Read a save state written by the player's emulator (`NGPCST01`).

    Layout: magic, the native `CpuState` struct, then `SHELL_SAVESTATE_MEM_LEN` bytes
    of the working image starting at address 0. The struct is read through
    `core.native`'s ctypes mirror, which is a pure declaration -- **importing it does
    not need the compiled core**, so this works in a Python-only checkout.

    ⚠️ NO ROM HASH. The player's format does not record one, so `expected_rom_path`
    cannot be enforced here the way it is for the JSON format. Loading a state against
    the wrong cartridge yields nonsense rather than an error, so the ROM identity is
    reported as empty rather than invented -- a caller that needs certainty must ask
    the person which game it came from.
    """
    from core.native import CpuState        # ctypes declaration only; no DLL needed

    blob = path.read_bytes()
    cpu_size = ctypes.sizeof(CpuState)
    header = len(SHELL_SAVESTATE_MAGIC)
    expected = header + cpu_size + SHELL_SAVESTATE_MEM_LEN
    if len(blob) != expected:
        raise ValueError(
            f"{path} is not a usable {SHELL_SAVESTATE_MAGIC.decode()} save state: "
            f"expected {expected} bytes ({cpu_size}-byte CPU struct + "
            f"{SHELL_SAVESTATE_MEM_LEN} of memory), got {len(blob)}"
        )

    raw_cpu = CpuState.from_buffer_copy(blob[header : header + cpu_size])
    memory = blob[header + cpu_size :]

    # Only the cells that carry something are handed on: the overlay is a sparse dict,
    # and a fully zeroed 48 KiB image would otherwise become 48 000 useless entries.
    overlay = {addr: byte for addr, byte in enumerate(memory) if byte}

    flags = raw_cpu.flags
    return SavestateDocument(
        format_version=SAVESTATE_FORMAT_VERSION,
        created_at_utc="",
        rom_sha256="",                     # not recorded by the player's format
        rom_file_size=0,
        rom_header_title="",
        rom_header_entry_point=0,
        rom_header_mode_raw=0,
        cpu=NgpcCpuState(
            pc=int(raw_cpu.pc),
            sr_raw=int(raw_cpu.sr_raw),
            flags=_flags_from_byte(flags),
            register_bank=int(raw_cpu.rfp),
            regs=GeneralRegisters32(
                xwa=int(raw_cpu.regs[0]), xbc=int(raw_cpu.regs[1]),
                xde=int(raw_cpu.regs[2]), xhl=int(raw_cpu.regs[3]),
                xix=int(raw_cpu.regs[4]), xiy=int(raw_cpu.regs[5]),
                xiz=int(raw_cpu.regs[6]), xsp=int(raw_cpu.regs[7]),
            ),
            modeled_fields=("PC", "architectural-register-set"),
            note=f"CPU state restored from a player save state ({path.name}).",
            iff_enabled=int(raw_cpu.iff_level) < 7,
            iff_level=int(raw_cpu.iff_level),
            rfp=int(raw_cpu.rfp),
            register_banks=None,
            alt_flags=_flags_from_byte(raw_cpu.alt_flags),
            control_registers=create_unknown_control_registers(),
        ),
        writable_overlay=overlay,
        quirk_database_version=load_known_quirk_database().database_version,
        matched_on_last_step=None,
        note="Loaded from a player save state; ROM identity is not carried by that format.",
        frame_state=initial_frame_state(),
        irq_state=initial_irq_state(),
    )


def _flags_from_byte(raw: int) -> StatusFlags:
    """The native core packs the status flags one per bit, SZ-H-V-NC (TLCS-900/H SR)."""
    return StatusFlags(
        sf=bool(raw & 0x80),
        zf=bool(raw & 0x40),
        hf=bool(raw & 0x10),
        vf=bool(raw & 0x04),
        nf=bool(raw & 0x02),
        cf=bool(raw & 0x01),
    )


def _savestate_payload_to_cpu(payload: dict[str, object]) -> NgpcCpuState:
    flags_section = payload.get("flags") or {}
    alt_flags_section = payload.get("alt_flags") or {}
    regs_section = payload.get("registers") or {}
    banks_section = payload.get("register_banks")
    control_registers = _savestate_payload_to_control_registers(
        payload.get("control_registers")
    )
    assert isinstance(flags_section, dict)
    assert isinstance(alt_flags_section, dict)
    assert isinstance(regs_section, dict)
    register_banks = None
    if banks_section is not None:
        if not isinstance(banks_section, list) or len(banks_section) != 4:
            raise ValueError("savestate cpu.register_banks must be null or a 4-bank list")
        parsed_banks: list[BankedByteRegisters] = []
        for bank in banks_section:
            if not isinstance(bank, list) or len(bank) != 16:
                raise ValueError("each savestate cpu.register_banks entry must be a 16-byte list")
            slots: list[int | None] = []
            for value in bank:
                if value is not None and (not isinstance(value, int) or not 0 <= value <= 0xFF):
                    raise ValueError("savestate cpu.register_banks bytes must be 0..255 or null")
                slots.append(value)
            parsed_banks.append(BankedByteRegisters(slots=tuple(slots)))
        register_banks = tuple(parsed_banks)
    return NgpcCpuState(
        pc=int(payload["pc"]),
        sr_raw=payload.get("sr_raw"),
        flags=StatusFlags(
            sf=flags_section.get("sf"),
            zf=flags_section.get("zf"),
            vf=flags_section.get("vf"),
            hf=flags_section.get("hf"),
            cf=flags_section.get("cf"),
            nf=flags_section.get("nf"),
        ),
        register_bank=payload.get("register_bank"),
        regs=GeneralRegisters32(
            xwa=regs_section.get("xwa"),
            xbc=regs_section.get("xbc"),
            xde=regs_section.get("xde"),
            xhl=regs_section.get("xhl"),
            xix=regs_section.get("xix"),
            xiy=regs_section.get("xiy"),
            xiz=regs_section.get("xiz"),
            xsp=regs_section.get("xsp"),
        ),
        modeled_fields=("PC", "architectural-register-set"),
        note="CPU state restored from savestate.",
        iff_enabled=payload.get("iff_enabled"),
        iff_level=payload.get("iff_level"),
        rfp=payload.get("rfp"),
        register_banks=register_banks,
        alt_flags=StatusFlags(
            sf=alt_flags_section.get("sf"),
            zf=alt_flags_section.get("zf"),
            vf=alt_flags_section.get("vf"),
            hf=alt_flags_section.get("hf"),
            cf=alt_flags_section.get("cf"),
            nf=alt_flags_section.get("nf"),
        ),
        control_registers=control_registers,
    )


def _control_registers_to_payload(
    control: Tlcs900ControlRegisters | None,
) -> dict[str, object] | None:
    if control is None:
        return None
    return {
        "dmas": list(control.dmas),
        "dmad": list(control.dmad),
        "dmac": list(control.dmac),
        "dmam": list(control.dmam),
        "intnest": control.intnest,
    }


def _savestate_payload_to_control_registers(
    payload: object,
) -> Tlcs900ControlRegisters:
    if payload is None:
        return create_unknown_control_registers()
    if not isinstance(payload, dict):
        raise ValueError("savestate cpu.control_registers must be null or an object")

    def _parse_quad(name: str) -> tuple[int | None, ...]:
        raw = payload.get(name)
        if not isinstance(raw, list) or len(raw) != 4:
            raise ValueError(f"savestate cpu.control_registers.{name} must be a 4-entry list")
        parsed: list[int | None] = []
        for value in raw:
            if value is not None and (not isinstance(value, int) or not 0 <= value <= 0xFFFFFFFF):
                raise ValueError(
                    f"savestate cpu.control_registers.{name} entries must be ints or null"
                )
            parsed.append(value)
        return tuple(parsed)

    intnest = payload.get("intnest")
    if intnest is not None and (not isinstance(intnest, int) or not 0 <= intnest <= 0xFFFF):
        raise ValueError("savestate cpu.control_registers.intnest must be a 16-bit int or null")

    return Tlcs900ControlRegisters(
        dmas=_parse_quad("dmas"),
        dmad=_parse_quad("dmad"),
        dmac=_parse_quad("dmac"),
        dmam=_parse_quad("dmam"),
        intnest=intnest,
    )


def _match_to_savestate_payload(
    match: KnownQuirkMatch | None,
) -> dict[str, object] | None:
    if match is None:
        return None
    return {
        "database_version": match.database_version,
        "quirk_id": match.quirk_id,
        "category": match.category,
        "confidence": match.confidence,
        "summary": match.summary,
        "note": match.note,
        "sources": [
            {"document": s.document, "section": s.section, "quote": s.quote}
            for s in match.sources
        ],
    }


def _savestate_payload_to_match(payload: dict[str, object]) -> KnownQuirkMatch:
    sources_raw = payload.get("sources") or []
    assert isinstance(sources_raw, list)
    sources = tuple(
        QuirkSource(
            document=s["document"],
            section=s.get("section"),
            quote=s.get("quote"),
        )
        for s in sources_raw
        if isinstance(s, dict)
    )
    return KnownQuirkMatch(
        database_version=str(payload["database_version"]),
        quirk_id=str(payload["quirk_id"]),
        category=str(payload["category"]),
        confidence=str(payload["confidence"]),
        summary=str(payload["summary"]),
        note=str(payload["note"]),
        sources=sources,
    )
