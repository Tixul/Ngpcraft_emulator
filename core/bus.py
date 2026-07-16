"""Minimal NGPC address-space helpers."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from core.rom import NgpcRomHeader, load_rom_header


@dataclass(frozen=True)
class AddressMapEntry:
    """Named inclusive address range in the current minimal address map."""

    name: str
    start: int
    end: int
    kind: str
    note: str = ""
    backing_file_offset_base: int | None = None

    @property
    def size(self) -> int:
        return self.end - self.start + 1

    def contains(self, address: int) -> bool:
        return self.start <= address <= self.end

    def region_offset(self, address: int) -> int:
        if not self.contains(address):
            raise ValueError(f"address 0x{address:06X} is outside region {self.name}")
        return address - self.start

    def file_offset(self, address: int) -> int | None:
        if self.backing_file_offset_base is None:
            return None
        return self.backing_file_offset_base + self.region_offset(address)


@dataclass(frozen=True)
class AddressProbe:
    """Result of probing one address in the address space."""

    address: int
    status: str
    region: AddressMapEntry | None
    region_offset: int | None
    file_offset: int | None
    note: str


# `probe()` is called for *every byte* the CPU fetches or reads, and it used to
# walk the region list and allocate a fresh `AddressProbe` each time. Profiling
# the run loop (2026-07-10) showed 370k `AddressMapEntry.contains` calls and 30k
# probe allocations for just 4000 executed instructions -- about 93 region
# comparisons per instruction.
#
# The address space is an immutable, frozen description, so `probe(address)` is a
# PURE function of `address`: memoising it is behaviour-neutral by construction.
# The cache is bounded (the working set of a running ROM is a few thousand
# addresses; the cap only guards a pathological scan of the 16 MB space).
_PROBE_CACHE_LIMIT = 1 << 20


@dataclass(frozen=True)
class NgpcAddressSpace:
    """Minimal address-space description derived from one ROM image."""

    rom_path: Path
    rom_size: int
    regions: tuple[AddressMapEntry, ...]
    # Mutable memo on a frozen dataclass: `frozen` forbids rebinding the field,
    # not mutating the dict it points at. Excluded from equality/repr so two
    # address spaces still compare by their actual description.
    _probe_cache: dict[int, AddressProbe] = field(
        default_factory=dict, compare=False, repr=False
    )

    def probe(self, address: int) -> AddressProbe:
        cached = self._probe_cache.get(address)
        if cached is not None:
            return cached
        result = self._probe_uncached(address)
        if len(self._probe_cache) < _PROBE_CACHE_LIMIT:
            self._probe_cache[address] = result
        return result

    def _probe_uncached(self, address: int) -> AddressProbe:
        for region in self.regions:
            if region.contains(address):
                note = region.note
                if region.name == "CART_ROM_UNLOADED":
                    note = (
                        "Address is inside the cartridge ROM window but beyond the loaded "
                        "ROM image size."
                    )
                return AddressProbe(
                    address=address,
                    status="mapped",
                    region=region,
                    region_offset=region.region_offset(address),
                    file_offset=region.file_offset(address),
                    note=note,
                )
        return AddressProbe(
            address=address,
            status="unmapped",
            region=None,
            region_offset=None,
            file_offset=None,
            note="Address is not covered by the current minimal address map.",
        )


def build_address_space(header: NgpcRomHeader) -> NgpcAddressSpace:
    """Build the current minimal NGPC address-space map."""
    cart_window_end = 0x3FFFFF
    cart_loaded_end = min(0x200000 + max(header.file_size - 1, 0), cart_window_end)
    regions = [
        AddressMapEntry("CPU_IO_PAGE", 0x000000, 0x0000FF, "io", "Internal CPU I/O page."),
        AddressMapEntry("WORK_RAM", 0x004000, 0x006BFF, "ram", "User RAM area."),
        AddressMapEntry(
            "SYSTEM_RAM_RESERVED",
            0x006C00,
            0x006FB7,
            "reserved",
            "System-reserved RAM area.",
        ),
        AddressMapEntry(
            "USER_VECTOR_RAM",
            0x006FB8,
            0x006FFC,
            "ram",
            "User interrupt vector area.",
        ),
        AddressMapEntry(
            "SYSTEM_RAM_RESERVED_TAIL",
            0x006FFD,
            0x006FFF,
            "reserved",
            "System-reserved RAM tail.",
        ),
        AddressMapEntry("SHARED_Z80_RAM", 0x007000, 0x007FFF, "ram", "Shared RAM."),
        AddressMapEntry(
            "K2GE_REGS",
            0x008000,
            0x008FFF,
            "io",
            "Video registers and palette RAM.",
        ),
        AddressMapEntry("SCR1_MAP", 0x009000, 0x0097FF, "vram", "Scroll plane 1 map."),
        AddressMapEntry("SCR2_MAP", 0x009800, 0x009FFF, "vram", "Scroll plane 2 map."),
        AddressMapEntry("CHAR_RAM", 0x00A000, 0x00BFFF, "vram", "Character RAM."),
        AddressMapEntry(
            "CART_ROM_LOADED",
            0x200000,
            cart_loaded_end,
            "rom",
            "Loaded ROM image.",
            backing_file_offset_base=0,
        ),
    ]
    if cart_loaded_end < cart_window_end:
        regions.append(
            AddressMapEntry(
                "CART_ROM_UNLOADED",
                cart_loaded_end + 1,
                cart_window_end,
                "rom-gap",
                (
                    "Cartridge flash window not backed by the current file. The current read "
                    "model treats this range as erased flash (0xFF), which matches the 2 MB "
                    "flash-cart layout used by the local save tooling."
                ),
            )
        )
    if header.file_size > 0x200000:
        # A 4 MB cartridge is TWO flash dies; the hardware maps the second at
        # 0x800000. This window used to be unmapped on the claim that the BIOS
        # only touches it for the autoselect handshake -- true for 2 MB carts,
        # false for the three 4 MB ones: SNK vs. Capcom MotM keeps its whole
        # intro (tile data, page descriptors, pointers into the same window)
        # above 0x800000, and an unmapped window fed its decompressor zeros
        # (pass 247). Carts of 2 MB or less keep the window unmapped exactly as
        # before, so the fuzz gate's view of unmapped space is unchanged.
        chip1_size = header.file_size - 0x200000
        chip1_loaded_end = min(0x800000 + chip1_size - 1, 0x9FFFFF)
        regions.append(
            AddressMapEntry(
                "CART_ROM_CHIP1",
                0x800000,
                chip1_loaded_end,
                "rom",
                "Second flash die of a 4 MB cartridge (file bytes 0x200000+).",
                backing_file_offset_base=0x200000,
            )
        )
        if chip1_loaded_end < 0x9FFFFF:
            regions.append(
                AddressMapEntry(
                    "CART_ROM_CHIP1_UNLOADED",
                    chip1_loaded_end + 1,
                    0x9FFFFF,
                    "rom-gap",
                    "Second-die window not backed by the file: erased flash (0xFF).",
                )
            )
    regions.append(
        AddressMapEntry("BIOS_ROM", 0xFF0000, 0xFFFFFF, "bios", "Internal BIOS ROM.")
    )
    return NgpcAddressSpace(
        rom_path=header.path,
        rom_size=header.file_size,
        regions=tuple(regions),
    )


def load_address_space(path: str | Path) -> NgpcAddressSpace:
    """Load a ROM and build the current minimal address-space map."""
    return build_address_space(load_rom_header(path))
