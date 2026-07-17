"""Export the sound chip's register writes to a VGM file — the chiptune standard.

The NGPC's T6W28 is a STEREO SN76489: two write doors, left (mem 0x4001) and right
(mem 0x4000), each an independent SN76489 stream with its own volumes. A real NGPC
VGM encodes them as command 0x50 (left / first chip) and 0x30 (right / second chip),
with the SN76489 clock field carrying the T6W28 marker. We replicate exactly that
(clock 0xC02EE000, feedback 3, shift width 15) so VGM players and Furnace read it.

Usage: begin() at record start, feed() the apu-write log each frame, build() the file.
"""
from __future__ import annotations

import struct

MAIN_CLOCK_HZ = 6_144_000     # the APU clock (3.072 MHz) is main/2; `cycle` is main-CPU
VGM_RATE = 44_100
DATA_OFFSET = 0x100           # a clean 0x100-byte header; data follows
_MEM = 1                      # NGPC_APU_WRITE_MEM (see core/native.py)


class VgmRecorder:
    """Accumulates PSG writes (with cycle timestamps) into a VGM command stream."""

    def __init__(self) -> None:
        self.events: list[tuple[int, bool, int]] = []   # (cycle, is_left, value)
        self.start_cycle: int | None = None
        self._last_count = 0

    def begin(self, write_count: int) -> None:
        self.events = []
        self.start_cycle = None
        self._last_count = write_count

    def feed(self, count: int, writes) -> None:
        """`count` = machine.apu_write_count(); `writes` = machine.apu_writes()
        (oldest-first ring). Takes only the entries new since the last feed."""
        new = count - self._last_count
        if new <= 0:
            return
        new = min(new, len(writes))
        for w in writes[len(writes) - new:]:
            # PSG only: the sound drivers touch mem 0x4000 (right) / 0x4001 (left).
            # The PORT 'writes' are the Z80 interrupt-ack, not sound -- skip them.
            if w.kind == _MEM and (w.address & ~1) == 0x4000:
                if self.start_cycle is None:
                    self.start_cycle = w.cycle
                self.events.append((w.cycle, bool(w.address & 1), w.value))
        self._last_count = count

    def empty(self) -> bool:
        return not self.events

    def build(self) -> bytes:
        data = bytearray()
        total = 0
        prev = self.start_cycle or 0
        for cycle, is_left, value in self.events:
            samples = int(round(max(0, cycle - prev) * VGM_RATE / MAIN_CLOCK_HZ))
            prev = cycle
            total += samples
            while samples > 0:                          # 0x61 nnnn = wait n samples
                n = min(samples, 0xFFFF)
                data += bytes((0x61, n & 0xFF, (n >> 8) & 0xFF))
                samples -= n
            data += bytes((0x50 if is_left else 0x30, value & 0xFF))
        data += bytes((0x66,))                          # end of sound data

        h = bytearray(DATA_OFFSET)
        h[0:4] = b"Vgm "
        struct.pack_into("<I", h, 0x04, DATA_OFFSET + len(data) - 4)   # EOF offset
        struct.pack_into("<I", h, 0x08, 0x00000171)                   # version 1.71
        struct.pack_into("<I", h, 0x0C, 0xC02EE000)                   # SN76489 clock (T6W28)
        struct.pack_into("<I", h, 0x18, total)                        # total samples
        struct.pack_into("<H", h, 0x28, 0x0003)                       # noise feedback
        h[0x2A] = 0x0F                                                # shift register width
        struct.pack_into("<I", h, 0x34, DATA_OFFSET - 0x34)           # data offset
        return bytes(h) + bytes(data)
