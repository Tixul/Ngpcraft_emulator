"""NGPC cartridge flash write model (direct AMD command path).

The NGPC save hardware is a NOR flash chip in the cartridge window
(`0x200000..0x3FFFFF`). Games program it either through the BIOS
(`VECT_FLASHWRITE`, handled in `execute.py`) or directly, by running an
AMD/Fujitsu-style command sequence against the cart window with the flash
write-enable (`/WE`) gated on I/O `0x6E`. The project's own hardware-validated
flash lib uses the direct path.

This controller models the direct path the way the reference emulator (NeoPop
`mem.c`) does -- and the reference is exactly what the project's flash stub was
validated against on real hardware:

  * A write to an unlock address (`0x202AAA` / `0x205555`) arms a pending flash
    command (NeoPop's `memory_flash_command`).
  * The next cart-window write, while armed, commits that byte into the flash
    (NeoPop writes it into the ROM image; we write it into the session's
    writable overlay, which shadows the cart ROM exactly like real flash
    overlays the cartridge).

We additionally gate the whole sequence on `/WE` (I/O `0x6E` == `0x14`), which
is stricter than NeoPop but matches real hardware and the project's flash lib
(which always toggles `/WE` around a write). When `/WE` is disabled the cart is
inert -- byte-for-byte the emulator's pre-flash behaviour.

Not modelled here (documented follow-ups): DQ7/DQ5 status-polling (we commit
synchronously, so the stub's poll reads the final value immediately and exits),
per-sector block erase (NeoPop does not special-case it either; the project
saves append full 256-byte slots), and `.sram` disk persistence (the committed
bytes live in the writable overlay, which the savestate already captures).
"""

from __future__ import annotations

from dataclasses import dataclass, field

CART_WINDOW_START = 0x200000
CART_WINDOW_END = 0x3FFFFF
# AMD unlock addresses in the low cart bank (per NeoPop mem.c). A write to
# either arms a pending flash command.
FLASH_UNLOCK_ADDRESSES = (0x202AAA, 0x205555)
# Autoselect / status-read command addresses: acknowledged but never arm a
# write (NeoPop routes these to the EEPROM-status path).
FLASH_STATUS_ADDRESSES = (0x220000, 0x230000)
# I/O register that gates the cartridge write-enable line.
FLASH_WE_IO_ADDRESS = 0x6E
FLASH_WE_ENABLE = 0x14


def in_cart_window(address: int) -> bool:
    return CART_WINDOW_START <= (address & 0xFFFFFF) <= CART_WINDOW_END


@dataclass
class FlashController:
    """Stateful AMD-style flash command model for the direct cart-write path.

    Owned by `EmulatorSession` so its armed state persists across step batches.
    `backing` records every committed flash byte (address -> value) for
    introspection / future `.sram` export; the authoritative copy that
    execution reads back lives in the session's writable overlay.
    """

    _armed: bool = False
    backing: dict[int, int] = field(default_factory=dict)

    def reset(self) -> None:
        """Full reset: clear pending command AND erase all flash (fresh cart)."""
        self._armed = False
        self.backing.clear()

    def clear_pending(self) -> None:
        """Clear only the pending command latch. Flash contents are
        non-volatile and survive a soft reset (power cycle with the cart in)."""
        self._armed = False

    @staticmethod
    def write_enabled(memory: dict[int, int]) -> bool:
        return (memory.get(FLASH_WE_IO_ADDRESS, 0) & 0xFF) == FLASH_WE_ENABLE

    def process_discarded_write(
        self, address: int, data: bytes, memory: dict[int, int]
    ) -> dict[int, int]:
        """Feed one attempted (hardware-discarded) cart-window write through the
        flash command model.

        Returns a mapping of committed `{address: byte}` to merge into the
        writable overlay (empty when the write was a command cycle or `/WE` is
        disabled). `memory` is the current overlay, read only to sample `/WE`.
        """
        if not self.write_enabled(memory):
            # No write-enable: real flash ignores the cycle entirely.
            return {}
        committed: dict[int, int] = {}
        for offset, value in enumerate(data):
            addr = (address + offset) & 0xFFFFFF
            if not in_cart_window(addr):
                continue
            result = self._process_byte(addr, value & 0xFF)
            if result is not None:
                committed[addr] = result
        return committed

    def _process_byte(self, address: int, value: int) -> int | None:
        if address in FLASH_UNLOCK_ADDRESSES:
            # Unlock / command cycle: arm, do not write data.
            self._armed = True
            return None
        if address in FLASH_STATUS_ADDRESSES:
            # Autoselect / status read enable: acknowledged, no data write,
            # and (per NeoPop) does not arm a program.
            return None
        if self._armed:
            # Armed: this cycle programs the byte into flash.
            self._armed = False
            self.backing[address] = value
            return value
        return None
