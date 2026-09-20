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
**FLASHWRITE 6, FLASHALLERS 7, FLASHERS 8**, FLASHPROTECT **0x0D**.

⛔ **This line used to say FLASHPROTECT 9. Nine is ALARMSET** (`SYSTEM.INC`, and its
own ABI section documents FLASHPROTECT at `0dh` with a different signature entirely:
`RB3` = first block, `RC3` = card type, `RD3` = how many). The clean-room BIOS image
was wired from the wrong number for one pass: a game setting an RTC alarm would have
irreversibly write-protected a block of its own cartridge instead. Pinned now by
`tests/test_hle_bios_image.py::SyscallVectorNumbers`.

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

### ⛔ And the hand-off's RESET reloads the pristine cart image (2026-09-05)

The console-boot path plays the BIOS for real and then resets into the cartridge's entry
point. That reset is `reset_memory`, which **reloads the ROM image** — a factory reset of
the chip in the middle of a boot. Anything restored into the flash before it is gone. In
"save into the .ngc" mode the reloaded image *is* the file the save was written into, so
it hid; in **separate-file** mode the save was wiped at every launch. See `SAVE_POLICY.md`
and `NativeSession.handoff_reset`.

⚠️ **And you cannot copy the cartridge out of memory at that moment.** The BIOS has just
run its autoselect probe (§4) and the chip is still in `FlashReadId`: it answers its four
ID bytes and **0xFF for every other address**. Measured — a snapshot taken there is
256 KiB of 0xFF, and putting it back destroys exactly what it was meant to save. The save
FILE is the state to restore from; nothing writes the chip between the restore and the
hand-off.

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

* **program** → `mem[addr] &= data` (a NOR cell only goes down), **then the chip is BUSY**
  for the program time.
* **erase** → the block is filled with `0xFF`, **the chip is BUSY** for the erase time,
  and only then does it answer one read with `0xFF` and return to being memory: that is
  the "done" a driver's status poll waits for.
* **busy** → while an operation runs, every read of the window answers **status**, not
  contents: DQ7 = the complement of the bit 7 being written (0 while erasing), DQ6
  toggles on every read, DQ5 = 0. Nothing matches what the driver asked for, so its poll
  loop spins — which is the point. Commands sent during the window are swallowed.
* **a program that cannot succeed** (a 1 asked over a 0, which is a slot programmed twice
  with no erase between) **never comes back**. MEASURED on the cartridge: 262 143 turns of
  the shipped poll loop, **2.45 seconds**, and **no DQ5** — the AMD datasheets describe an
  internal timer and a DQ5 flag, and this part does not do it. The driver leaves on its own
  timeout or not at all, and every one of those seconds is spent with interrupts masked.
  The reset command (`F0`) does get the chip back, which is also measured.
* **durations** (`hw_test_flash_timing`, 16 Mbit cart, two runs): an 8 KB block erase
  **57.5 ms** (353 389 cycles, mean of four measurements spread 53.0-62.5 ms -- the erase
  is NOT a constant), one byte programmed **33.0 µs** (203 cycles), a 64 KB block erase
  441 ms. Scaling by block size is measured at **7.67x** for 8x the size; the code scales
  linearly and is 4 % high.
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

---

## 4b. 🔑 Which cartridge is it? **The cart tells us — in its own units**

A console never has to guess: it probes the chip, the chip answers a device ID that names
its size, done. **A ROM file has thrown that away.** The image is what the publisher
burned; the part is as big as the publisher bought. Delta Warp is 512 KiB of ROM on an
8 Mbit chip, StarGunner is a small homebrew on a 16 Mbit one — **same image size, two
different cartridges**, so no rule over the file can be right for both. That is not a gap
in our model of the hardware; it is information the dump does not contain.

What survives is the cart's own **save request**, and it is expressed in the units of the
card the game was burned on (SDK `FlashMem.txt` / `BLOCK_NO.INC`). The save is the top of
the chip — the two 8 KiB blocks numbered `n-3` and `n-2`:

| card | blocks | addresses |
|---|---|---|
| 4 Mbit | 8, 9 | `0x078000`, `0x07A000` |
| 8 Mbit | 16, 17 | `0x0F8000`, `0x0FA000` |
| 16 Mbit | 32, 33 | `0x1F8000`, `0x1FA000` |

So we listen in two places, and both matter:

* **`flash_adopt_capacity_from_block`** — the **BLOCK NUMBER** a game hands the BIOS
  (`ld rb3, BLOCK_NB` ; `VECT_FLASHERS`), read at the **`swi 1`**. This is the only moment
  the number still exists: the BIOS's next move is to turn it into an address using
  `0x6C58`, and after that the identity is gone. Block 17 means *an 8 Mbit card*,
  whatever we were presenting.
* **`flash_adopt_capacity_from_save`** — the **ADDRESS**, for homebrew that drives the
  chip itself and never calls the BIOS. An offset in the **top 64 KiB** of a standard card
  names that card.

⛔ **Why the address alone was not enough**, measured: with only the address rule, a cart
presented one size too big saves **once** and then fails forever. The first program lands
on a virgin chip — all `0xFF`, nothing to erase, any geometry "works" — and by the time we
learn anything, the erase has already gone to the wrong block, so the slot is never
cleared again. And the rule only recognised `capacity − 0x6000`, i.e. the *second* 8 KiB
block; real games use the whole top (`0xF8000`, `0xF9F00`, `0xFBF00` are all in the corpus
of §6), and those learnt nothing at all. **That is what made the manual size setting
necessary.** `tests/test_bios_flash_syscall.py::CartridgeIdentityTests` pins both cases,
and both fail against the previous core.

⚠️ **AND THE CARD-TYPE BYTE MUST NEVER OUTLIVE THE MAP.** They are two halves of one
answer: the BIOS turns a block NUMBER into an ADDRESS using `0x6C58`, the chip decides
how MUCH to erase from its block map. An adversarial pass found the one path that left
them out of step — the exit that *refuses* a resize returned without restating the byte
— and measured the consequence: a game asking for its 8 KiB save block had **64 KiB of
its own ROM erased**, which with `save_to_rom` on (the default) reaches the `.ngc`.
Every exit from `flash_present_as` now leaves the byte describing the map.
(On silicon this state cannot arise: the BIOS reads the chip's own ID at power-on. If a
game clobbers the byte afterwards, a real console erases the wrong block too — that part
is faithful. What was ours to fix was the core creating the disagreement itself.)

**The guards** — the dangerous direction is *shrinking*, because it moves the save DOWN
into the game's own code and the next erase takes 8 KiB of it:

* a capacity smaller than the **data** on the die is **refused outright** — and *data*
  is the point: a trailing run of `0xFF` is not image, it is what an erased cell reads
  as, indistinguishable from a chip nobody filled to the top. Counting it as image is
  how a file that grew once stayed grown (see §5), so the floor ignores the erased tail
  and an already-padded `.ngc` heals on its next load. Measured once, when the cartridge
  goes in — computing it per call would rescan the padding on every programmed byte;
* a block inside the presented card's **own top four** is that card saving normally — no
  signal, nothing to learn;
* a number **above** the presented map is a *bigger* card (StarGunner asking for block 33)
  and is allowed through: growing is free, the space above the image is erased `0xFF`;
* the number must name a save block on **exactly one** of the three cards.

⚠️ **Still guesswork: the card byte at POWER-ON.** Until the game asks for its first save
we have nothing to go on, and `auto` presents every sub-2 MiB cart as 16 Mbit
(`ngpc_settings.flash_capacity_bytes`). A game that reads `0x6C58` for itself before ever
saving would read the wrong card. Saying so rather than pretending the question is closed.
The manual **Flash size** setting stays as an override; it should no longer be needed.

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
* ⚡ **The image is persisted at the CHIP's size, not at the size we guessed.** `auto`
  presents an under-filled cart as 16 Mbit because it has to guess; the cartridge then
  corrects us (§4b). Writing the `.ngc` back at the guess pads it out to a size the cart
  never had — and on the next load that padding reads as *image*, so the correction is
  refused ("a chip is never smaller than its image") and every later save is programmed
  into a slot nobody erased. Measured on a 512 KiB cart: session 1 saved, the file grew
  to 2 MiB, sessions 2-4 wrote the bitwise AND of two payloads. `_cart_windows()` now
  asks the core what it presents *now* (`ngpc_flash_capacity`), so the file is exactly
  one chip. A cart that really does use the bigger part (StarGunner, saving at
  `0x1FA000`) still grows — that is where its save lives.
  Pinned by `tests/test_save_persistence.py`, which saves DIFFERENT bytes each session:
  the same payload twice cannot fail, because programming a byte over itself is a no-op.
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

* ~~**DQ7/DQ5 status polling / erase timing.**~~ **MODELLED and MEASURED, 2026-09-10** —
  see § 3. `04_MY_PROJECTS/hw_test_flash_timing` is the ROM: it reports the poll turns the
  shipped AMD stubs actually spend, calibrated against 60 frames of that same loop on the
  same silicon. `ngpc_set_flash_timing` is the dial; all three zero restores the
  synchronous chip, for bisecting a corpus change.

  **Both figures our documentation carried were wrong.** `NGPC_FLASH_SAVE_GUIDE.md` gave an
  8 KB erase as ~5-15 ms — and made that the REASON block 33 was chosen; `OPEN_ITEMS.md`
  gave ~1 s. It is 57.5 ms. The 2026-03 hardware trials agree with the measurement from
  both sides (`FLASH_SAVE_RESEARCH.md`): a 64 KB block erase took the console down on a
  ~100 ms watchdog, an 8 KB one did not. **But the margin is a factor of two, not an order
  of magnitude**, which is not what the table claimed.
### 🩺 And now it SAYS SO: `flash-busy-fetch`

A hardware-safety finding, alongside the starved watchdog and the stack in the system page:
**the CPU fetched an instruction out of a chip that was programming or erasing.** That is
the single unambiguous signature of this entire class of bug -- a driver that forgot its
`di`, a stub left in the cartridge instead of copied to RAM, a reset sent to a working chip
-- and until the busy window existed it could not even happen here.

Edge-triggered, carrying the PC that fetched first. Counted, never fatal on its own,
because a console does not stop at that instruction either: it runs the garbage. It shows
up in `ngpc_native.py --json` under `hw_safety`, and `--hw-guard` does NOT stop on it.

```
"hw_safety": {"counts": {"watchdog-starved": 0, "system-stack": 0, "flash-busy-fetch": 0}}
```

* **A busy chip is SLOWER TO READ on silicon, and we do not model it.** The same poll
  loop takes 111.00 lines per 1024 turns against an idle chip -- which our core reproduces
  EXACTLY -- and 120 lines against one that is erasing, where our core still says 112.
  The cartridge has an 8.1 % asymmetry we do not. ⚠️ The busy measurement runs with the
  cart write-enable armed and the idle one does not, so the cause may be that line rather
  than the busy state; a probe that arms /WE without starting an operation separates them.
* **The watchdog period.** `kWatchdogTimeoutCycles` is one CPU second here; the hardware
  trials say ~100 ms. Now that an erase costs real time, that gap is testable for the first
  time — a 64 KB erase should reproduce the 2026-03 crash and will not while the period is
  ten times too long.
* **Write-cycle endurance** (~100 000 per cell, `FlashMem.txt`). Not counted.
* **The Python core** still has its own older, separate flash model (`core/flash.py`),
  which models the *direct* AMD path against the writable overlay. It is not the core
  that runs games. Do not read its passing tests as a statement about the save path —
  that is precisely the mistake §0 records.

See also `SAVE_POLICY.md` (policy) and `specs/BIOS_HLE.md`.
