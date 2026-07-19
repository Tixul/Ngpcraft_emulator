# Cartridge flash — THE SAVE

`cpp/src/memory.cpp` (the chip) · `core/flash_file.py` (the file) · `core/native_session.py` (the lifecycle)
Tests: `tests/test_flash_saves.py`, `tests/test_bios_flash_syscall.py`, `tests/test_flash_file.py`
Landed 2026-07-14 (pass 240).

---

## 0. ⛔ RETRACTION — what this document used to say

The previous version of this file, and the banner on `SAVE_POLICY.md`, said:

> ✅ STATUT 2026-07-10 — les saves in-game FONCTIONNENT (les deux chemins)

**They did not.** The user lost a save and reported it, and they were right.

The claim was not a typo — it was a **false green**, and it is worth understanding
exactly how it was manufactured, because the ingredients are all still lying around:

1. The save path was implemented and tested **in the Python core**. The Python core
   retires ~1 700 instructions a second; a NGPC needs ~615 000. **It has never been
   able to play a game.** The core people actually play on is the C++ one.
2. In the C++ core, `Machine::flash_command()` handled the AMD unlock and autoselect
   — enough for the BIOS to identify the cartridge — and then, in its own comment's
   words, *"swallowed, not faked"* every erase and every program.
3. So every test passed, every doc said ✅, and **every save went nowhere, silently.**

🔑 **A feature that is green in a core nobody plays on is not a feature.** The tests
were not lying about the code they tested; they were pointed at the wrong machine.

---

## 1. There is no save RAM. The cartridge IS the save.

A game saves by **erasing a block of its own ROM and programming its slot back in**.
The cart window (`0x200000..0x3FFFFF`) is a NOR flash chip; the save area is a few
kilobytes at the top of it.

Two consequences that drive everything below:

* **A NOR cell only goes DOWN.** Programming ANDs the byte into the cell
  (`cell &= data`); only an erase puts the 1-bits back. A model that just stores the
  byte produces data the silicon cannot, and hides the exact bug a homebrew author
  needs to see — a slot programmed twice with no erase between.
* **Persisting the save** means persisting the bytes of the cart image that no longer
  match the ROM file, and putting them back when the cartridge goes back in.

---

## 2. How a game actually reaches the chip: **it doesn't**

A retail game does not drive the flash. It calls the BIOS (SNK `SysCall.txt`; the
vector numbers are SNK's own `SYSTEM.INC`):

```asm
    ld  rw3, VECT_FLASHERS      ; 8   -- erase a block
    ld  ra3, 0                  ;        card 0 (0x200000); 1 = 0x800000
    ld  rb3, BLOCK_NB
    swi 1

    ld  rw3, VECT_FLASHWRITE    ; 6   -- write the data
    ld  ra3, 0
    ld  rbc3, 1                 ;        units of 256 bytes
    ld  xhl3, source
    ld  xde3, offset_in_card    ;        an OFFSET, not an absolute address
    swi 1
```

Return: `RA3` = 0 (`SYS_SUCCESS`) or an error. `VECT_FLASHWRITE` destroys RBC3/XHL3/XDE3.

⚠️ **The SDK's own `ngpc.h` in this RAG has `#define VECT_FLASHWRITE` with NO VALUE**
(the numbers were lost from that copy). Trusting it would have called vector 0 —
`VECT_SHUTDOWN`. The authority is `SYSTEM.INC`: SHUTDOWN 0 … SYSFONTSET 5,
**FLASHWRITE 6, FLASHALLERS 7, FLASHERS 8**, FLASHPROTECT 9.

**Our `swi 1` is not high-level-emulated in the C++ core.** It pushes PC/SR and jumps
through the hardware vector table exactly as the chip does, so with the retail BIOS
attached, a game's save runs **the real SNK flash routine**, which issues the real AMD
command cycles at the chip modelled below. That is the whole path, and it is the one
`tests/test_bios_flash_syscall.py` exercises.

⚠️ **No BIOS image ⇒ no saves.** The vector table reads back zero and `swi 1` jumps to
address 0. This is not a limitation to work around; it is what a console with no BIOS
would do.

---

## 3. 🔑 The byte that made every save fail: `0x6C58`

The BIOS's flash routine reads **its own work RAM at `0x6C58`** before it touches
anything, and returns error `0xFF` if it is zero. That byte records **which cartridge
the BIOS found at power-on**:

| `0x6C58` | cartridge |
|---|---|
| 0 | no card |
| 1 | 4 Mbit |
| 2 | 8 Mbit |
| 3 | 16 Mbit |

`0x6C59` is the same thing for **CS1** (`0x800000`) — the *development board's* slot.
A production console has **nothing plugged into it** (`FlashMem.txt`: "This area CS1 is
only valid during development … cannot be used to run the program in the production
version"), so it is 0. We used to answer its autoselect probe with chip 0's own size,
and the real BIOS duly wrote down that a second cartridge was present — **a cartridge we
had invented.**

We boot games through the **hand-off** (we skip the BIOS's boot code and hand the cart
the state the BIOS would have left). Nobody had ever written this byte. So the chip
below could be flawless and the BIOS would still refuse, having touched nothing.

🔑 **Every layer was correct and the save still went nowhere.** The failure was a byte
nobody had thought to hand over.

**The encoding was not guessed.** Booting the real BIOS with a 4 / 8 / 16 Mbit cartridge
and reading the byte back gives 1 / 2 / 3. That experiment doubles as proof the
autoselect model in §4 is right: **the BIOS could only have learnt the size by asking
our chip.**

---

## 4. The chip (AMD/Fujitsu protocol)

`Machine::flash_command()` — a cart-window write is discarded as memory and handed to
the command latch. Command addresses are masked to 15 bits (`offset & 0x7FFF`).

```
AA @ 5555 · 55 @ 2AAA · then
    90 @ 5555   autoselect  -> reads answer the chip ID
    A0 @ 5555   program     -> the NEXT write IS the data
    F0          reset       -> be memory again
    80 @ 5555   erase prefix -> AA @ 5555 · 55 @ 2AAA · then
                                    10 @ 5555  erase the WHOLE chip
                                    30 @ addr  erase the BLOCK containing addr
    9A @ 5555   protect prefix -> ... 9A @ addr : that block becomes read-only
```

* **program** → `mem[addr] &= data` (a NOR cell only goes down).
* **erase** → the block is filled with `0xFF`, then the chip answers **one** read with
  `0xFF` and returns to being memory: that is the "done" a driver's status poll waits for.
* **autoselect** → `0x98` (Toshiba), then the **device ID, which names the SIZE**:
  `0xAB` (4 Mbit) · `0x2C` (8) · `0x2F` (16), then `0x02`, then `0x80`.
* An **empty slot has no chip** and answers nothing (`flash_present()`).

**The block map** (`FlashMem.txt`, all three sizes): 64 KiB blocks all the way up, with
the **last 64 KiB split 32 / 8 / 8 / 16**. Those small blocks at the top exist precisely
so that rewriting one save slot does not cost 64 KiB — and they are where every game's
save lives. The SDK reserves the **final** block for the system program.

⚠️ The cart's SIZE is decided in **exactly one place** (`flash_device_id`) and read back
by everything else (`flash_size_code` → the `0x6C58` hand-off). Two independent size
ladders is how a 4 Mbit cartridge gets told it is 8 Mbit by one path and 4 by another.

### Cross-checks
* **An independent implementation of the same chip, derived from the datasheet rather
  than from ours, reaches the same model**: same block map, same `input & data`
  (programming can only clear bits), same erase-to-`0xFF`, same device IDs.
* ⛔ **Most emulators do NOT emulate this chip at all.** In that shortcut model a write to
  `0x205555` or `0x202AAA` just sets a flag, and the *next* cart write is punched straight
  into the ROM image with its 256-byte block marked dirty — **the erase command byte `0x30`
  included, stored as data**. It works for games only because they immediately reprogram
  whatever they erased. That approach is a reference for the **file format** (§5), never
  for the protocol.

---

## 5. The file: `saves/<rom>.flash`

**The de-facto community format** — every other NGPC emulator reads and writes it
byte-for-byte. A save is a thing a player wants to keep and to move between emulators, so
we adopt the existing layout rather than inventing a better one.

```
FlashFileHeader      u16 valid_flash_id = 0x0053
                     u16 block_count
                     u32 total_file_length
FlashFileBlockHeader u32 start_address        (a CPU address: 0x200000 + offset)
                     u16 data_length
                     -- + TWO BYTES OF C STRUCT PADDING: they memcpy the struct
                        straight into the file and `u32,u16` aligns to 8. The
                        padding is IN THE FILE. Pack it to 6 and every other
                        emulator reads garbage (and we would never notice).
... then data_length bytes.
```

* We save the granules of the cart image that **differ from the ROM file** — which
  correctly includes an **erase**, since 0xFF-where-there-was-data is a change that must
  survive a reload.
* `data_length` is a u16, so runs are split at 32 KiB.
* Saves live in `saves/`, **not** next to the ROM: the ROM directory is the player's
  collection, not ours to scatter files through. Copy the file next to a ROM and any
  other emulator will read it.
* A **corrupt** save file is refused, not half-applied. Half a save is worse than none,
  because it looks like a working one.
* A **probe is not a player**: `corpus_check` / `triage_vs_oracle` run with
  `autosave=False`.

**Not persisted:** block protection. The format has nowhere to put it and no other
emulator keeps it. On silicon it is irreversible (`SysCall.txt`, VECT_FLASHPROTECT:
"there is no operation which will remove the protection"). A protected block comes back
writable on reload. Said out loud rather than pretended.

---

## 6. Validated — on real game code

A probe over the retail corpus: **six commercial games initialise their save area at boot
with no player input**, and they write exactly where the block map says they should
(offsets `0xF0000`, `0xF8000`, `0xF9F00`, `0xFA000`, `0xFBF00` — the small blocks at the
top of the chip):

> Baseball Stars · Magical Drop Pocket · Memories Off - Pure · Puzzle Link ·
> Puzzle Link 2 · Tsunagete Pon! 2

Every one of those writes used to be silently discarded.

**The power-cycle round trip, on Puzzle Link 2** (`tests/test_flash_file.py`):

| | |
|---|---|
| first boot vs a **fresh** cartridge | byte-identical → the init is deterministic |
| second boot (save restored) vs fresh | **differs** → the game *saw* its save |
| what changed | its own counter at `0x2F8000`, `0` → `1` |

The second boot behaving *differently from a blank cartridge* is the assertion that
cannot be faked by any amount of plumbing on our side. Magical Drop and Memories Off go
further: on the second boot they **do not rewrite at all** — they read the save and
accepted it.

---

## 7. Not modelled (documented, not faked)

* **DQ7/DQ5 status polling / erase timing.** We erase and program synchronously, so a
  driver's poll loop reads the final value immediately. The *outcome* is right; there is
  no timing model. (The erase's one-shot `0xFF` acknowledge is what the BIOS's poll
  actually consumes.)
* **Write-cycle endurance** (~100 000 per cell, `FlashMem.txt`). Not counted.
* **The Python core** still has its own older, separate flash model (`core/flash.py`),
  which models the *direct* AMD path against the writable overlay. It is not the core
  that runs games. Do not read its passing tests as a statement about the save path —
  that is precisely the mistake §0 records.

See also `SAVE_POLICY.md` (policy) and `specs/BIOS_HLE.md`.
