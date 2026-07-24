
# NgpCraft Emulator

A **Neo Geo Pocket / Neo Geo Pocket Color** emulator: a fast native C++ core
(TLCS-900H + Z80 + K2GE video) driven by a PyQt6 desktop shell.

Its timing is **calibrated against real hardware** — instruction-fetch wait-states on
the cartridge flash, silicon-measured MUL/DIV and LDIR costs — so self-timed games
(Cool Boarders Pocket, Densha de Go) run at their true 30 fps instead of the ~2× too
fast that most emulators show.

Instruction coverage is checked the same way: a sweep of a 90-cartridge corpus, each
driven for 400 frames, currently finds **no ROM stopped by a missing opcode**. That sweep
is a feature you can run yourself — see [ROM analysis](#rom-analysis).

<img width="1076" height="761" alt="emulateur01" src="https://github.com/user-attachments/assets/24dd060d-5b35-4ef6-83dd-916a618ba244" />

## Features

- **Library** with cover thumbnails (grid / list / compact), live-reflowing — plus
  **search**, **sort** (name, last played, most played, playtime, recently added, size,
  with a direction toggle), **favourites** ★ and a **never-played** filter. Play count,
  playtime and last-played are tracked per game. Covers are rendered from the game itself,
  or [pick your own](#your-own-cover-art) — one that updates never overwrite.
- **Console boot** — with a real BIOS, *Boot BIOS* powers the console on for real: the
  Neo Geo Pocket intro plays and the game then boots on its own, exactly like hardware.
- **Video**: integer / fit / stretch scaling, scanline / LCD-grid / CRT filters,
  colour profiles, real fullscreen — which **hides the sidebar and toolbar** for the game
  alone (optional); **double-click or `Esc`** returns to windowed. The canvas follows the
  window; size presets `Ctrl+1…5`.
- **Black-and-white cartridges, in colour** — an NGP game on an NGPC is *colourised*, the way
  a Game Boy game is on a Game Boy Color. Both machines are selectable — see
  [Monochrome cartridges](#monochrome-cartridges-on-a-colour-console).
- **Save states** — 8 slots per game (toolbar or `F2` save / `F4` load / `F3` slot).
- **In-game saves** — the game's own flash save, stored in the ROM, a separate file, or both.
- **A console that remembers** — the coin cell keeps its BIOS settings *and* its clock, so
  the date is still right next time. The **RTC alarm** works too, and goes off on time.
- **Speed control** — fast-forward (hold `Tab`) and 0.25×…4× (`[` / `]` or the toolbar).
- **Rewind** — hold `,` (or the ⏪ toolbar button) to run the game backward; release to
  resume. `.` steps one frame forward. Buffer length configurable (Off / 10 / 20 / 30 s).
- **Screenshots** (`F12`, folder configurable), **FPS overlay**, and a **player toolbar**
  that can auto-hide when the mouse goes still and reappear on the next move (optional).
- **Controller support** — an Xbox-style (XInput) pad alongside the keyboard, plus
  **turbo / autofire** on A and B at 5–20 presses per second.
- **Fully remappable** — console buttons *and* every in-game hotkey, with a warning when
  a binding would collide. The console buttons are bound **on a picture of the console**:
  each field sits next to the button it drives, so you pick the D-pad's *left* rather than
  a row labelled "Left".
- **Themes** — follows your desktop's light/dark setting by default, or pick Light or Dark
  explicitly. Switches live, no restart.
- **Debug tools** (`F1`) — a real debugger: symbols, instruction stepping, call stack,
  raster event timeline, read *and* write watchpoints, an editable hex view with access
  highlighting, RAM search, **show/hide any video layer** on the live picture, a tile
  viewer that **names every tile's address on hover** (click to copy), a **Load** tab with
  live green→red gauges for the **sprite (OAM)** and **character-RAM tile** budgets read
  straight from VRAM, and audio analysis with **VGM export**. See [Debugging](#debugging-f1).
- **Fan-translation tools** — everything works on **any ROM**, driven by a character
  table you load (`.tbl`); nothing is game-specific. Four tabs:
  - **Text** — decode a region into strings, **search** for a phrase by its exact bytes,
    **scan** a whole region for every string (a script dump you can export to a file), and
    a **relative search** that finds a word under an *unknown* encoding by its letter
    spacing (no table needed).
  - **Crack** — type words you can read on screen and it **builds the table for you**:
    each word is located by relative search (or pinned with `word @ offset`), and the bytes
    under it become a `.tbl` you can save or use straight away.
  - **Pointers** — **find every pointer to an address** (to repoint a moved string) or
    **locate the pointer tables** themselves. 16/24/32-bit LE, with a base for bank offsets.
  - **Compare** — byte-**diff against a second ROM**: an existing patch's changed ranges
    *are* the text, shown decoded on both sides.

  Every result shows its ROM file offset (`address − 0x200000`).
- **ROM analysis** — right-click a game to boot it, drive it, and report what is wrong
  with it. See [ROM analysis](#rom-analysis).
- **Crash reports** — a ROM fault writes a detailed `crashes/*.txt` (reason, PC, opcode,
  registers, memory & stack dumps).
- **Translated interface** — English and French, switchable live, no restart. One JSON
  file per language in [`lang/`](lang), so adding one is adding a file and needs no
  code: see [TRANSLATING.md](TRANSLATING.md). Contributions welcome.

<img width="1073" height="768" alt="emulateur02" src="https://github.com/user-attachments/assets/051d5d43-dc43-4001-8964-7e8b757b057d" />


## Run

Requires **Python 3.10+**.

```bash
pip install -r requirements.txt
python ngpc_shell.py
```

On Windows a prebuilt core (`cpp/build/ngpc_core.dll`) is included, so it runs as-is.

### Prebuilt download (no Python needed)

Grab the build for your platform from the [Releases](../../releases) page and run it —
nothing to install. ROMs, saves and screenshots live in folders next to the app.

**Windows, Linux and macOS are built from this same source** by GitHub Actions
([`.github/workflows/build.yml`](.github/workflows/build.yml)): each tagged release runs the
test suite on all three and packages one archive per platform. PyInstaller cannot
cross-compile, so every build is made on its own machine — which is the whole reason that
workflow exists.

> The macOS app is **not code-signed**, so Gatekeeper warns the first time: right-click ▸
> **Open** to run it anyway.

To build it yourself on the platform you are on:

```bat
pip install pyinstaller
build_exe.bat                        :: Windows
```

```bash
pyinstaller --noconfirm --clean NgpCraftEmulator.spec    # any platform
```

The spec branches on the host OS (core library name, icon format, macOS `.app` bundle), so
the same file produces the right thing everywhere.

### Building the core (other platforms / from source)

```bash
cd cpp
cmake -B build -DCMAKE_BUILD_TYPE=Release
cmake --build build --config Release
```

This produces `cpp/build/ngpc_core.{dll,so,dylib}`, which the shell loads automatically.

## Games & BIOS

No ROMs or BIOS are included — provide your own.

- Put `.ngc` / `.ngp` files in **`roms/`** (or pick any folder with **Choose ROM folder**,
  in the Library).
- A real Neo Geo Pocket **BIOS** — place it as **`bios.bin`** next to the app (or set the
  path in **Settings ▸ Console (BIOS)**). **It is not optional.** Both start modes below go
  through the BIOS — even the instant hand-off boots it internally to capture the state and
  character RAM it hands the cartridge — so with no BIOS image at all the CPU never reaches
  the cartridge: the game sits on a blank white or black screen, and its Library cover
  cannot be rendered either (a card with no cover is telling you exactly this).

> **A few commercial games need `bios.bin` too.** *Metal Slug — 2nd Mission* checks that the
> console really booted through its BIOS, and quietly disables **fire and jump** when it
> decides it did not — the game still runs and looks perfect, you simply can never shoot or
> jump. Both start modes below satisfy the check; no BIOS at all does not.

### Two ways to start a game

- **Instant hand-off** *(default — leave "Console boot" OFF)*: the cartridge is handed the
  exact state the BIOS boot would have left, so the game starts immediately. The BIOS image
  is still used behind the scenes (saves, system calls); the game just boots straight in.
- **Console boot** *(Settings ▸ Console (BIOS) ▸ "Play the console boot")*: the real BIOS powers on,
  plays its **NEO·GEO POCKET intro**, and then boots the game on its own — exactly like
  turning on the hardware. A brand-new console configures itself the first time (the BIOS
  first-boot setup is auto-completed with defaults and remembered), so you always get
  *intro → game*, every launch, with no setup screen to click through. Needs a real
  `bios.bin`.

The **Boot BIOS** button (Library) boots the BIOS by itself, with no cartridge — the
console's own language/clock screens, one of the NGPC's signature features.

### Your own cover art

Library covers are rendered automatically (the emulator boots each game and keeps its
best-looking frame), and that render is a **cache**: it lives in `thumbnails/` and is
thrown away whenever a new version renders covers differently.

Because a cover is a real boot, it needs **`bios.bin`** — with no BIOS the cards stay
blank (the Library says so) rather than filling with white boxes, and they render by
themselves as soon as you point at a BIOS in Settings. A game that never reaches a real
screen is left uncovered too, and retried next launch, so a blank frame is never cached.

To use your own image instead, right-click a game ▸ **Choose cover image…**. The file is
copied into **`covers/`** — a folder the emulator only ever *reads*. Nothing regenerates
it, no update replaces it, and overwriting the install to upgrade keeps it, because
`covers/` is your data and ships in no archive. Right-click ▸ **Back to the auto cover**
undoes it.

You can also drop files in yourself: `covers/<ROM file name>.png` (also `.jpg`, `.bmp`,
`.gif`, `.webp`) — e.g. `covers/Faselei! (Europe).png`. Any size works; it is scaled to
the card. If two ROMs in your library share a file name (every NgpCraft project builds a
`main.ngc`), use `covers/<name>.<8-hex tag>.png` to pin a cover to one of them — picking
the image through the menu does this for you.

> Placing an image directly in `thumbnails/` used to look like it worked, then lost your
> cover on the next update that changed the render. `thumbnails/` is the cache; `covers/`
> is yours.

## Monochrome cartridges on a colour console

A Neo Geo Pocket game is black and white. Put one in an NGPC and it comes up **in colour** —
the same trick a Game Boy Color plays on a Game Boy cartridge. Both halves of that are
modelled here.

**The game colourises itself.** The console tells a cartridge which machine it is sitting in,
and a colour-aware mono game reads that and takes a different code path. *Samurai Shodown!*
paints its own palette — green bamboo, fighters near their canonical colours — where on an
original NGP it stays in eight greys. Getting that byte wrong is invisible until you compare
against hardware: with it, the game writes some 3 600 palette entries in the first 20 seconds;
without it, sixteen. *(Confirmed against a real NGPC, cartridge flashed.)*

**Everything else gets the console's theme.** A mono game that is *not* colour-aware is tinted
by the BIOS instead, using the colour **you** chose on the console's own setup screens. The
BIOS keeps that palette in coin-cell RAM, so it survives across launches and the emulator
hands it to the cartridge exactly as the hardware does.

> To choose it, use the **Boot BIOS** button and go through the setup. Launching a *game*
> auto-completes that setup with defaults on purpose — nobody wants to fill in a
> questionnaire to start playing — which also means it never asks you for a colour.

**Which machine to be** — *Settings ▸ Graphics ▸ "Black-and-white NGP games"*:

- **NGPC (K2GE) — colourised** *(default)* — what this emulator is.
- **NGP (K1GE) — monochrome** — the original handheld. The cartridge is told it is in a mono
  console, and the 12-bit palette it would colourise through does not exist on that silicon,
  so it stays grey **of its own accord** — the game never runs its colour code. This is not a
  filter laid over the picture afterwards.

Takes effect the next time a game is started.

## Saves

Three different things, kept separate:

- **Save states** — an instant snapshot of the whole machine, 8 slots per game. Toolbar
  buttons, or `F2` save / `F4` load / `F3` change slot. Stored in `savestates/`.
- **The console's own memory** — not a game's at all: the language, colour and date the
  BIOS remembers, kept by the coin cell. See [The console's clock](#the-consoles-clock).
- **In-game saves** — the game's OWN save (RPG progress, high scores, options), written by
  the cartridge's flash. Choose how it is stored in **Settings ▸ General ▸ "In-game save"**:
  - **In the ROM (.ngc)** — written back into the cartridge file itself, exactly like the
    flash chip on a real cartridge. The save travels with the ROM. *(default)*
  - **Separate file** — a `saves/<rom>.flash` beside it; the ROM is never modified.
  - **Both** — into the ROM and a `.flash` backup.

  Commercial games reach the flash through the **BIOS**, so they need a real `bios.bin`.
  Homebrew that drives the flash directly (e.g. save-library test ROMs) saves without one.

  **Cart flash size** (Settings ▸ General) — the emulator presents a flash chip of a given
  capacity to the game. A real cartridge's flash chip is a standard 4 / 8 / 16 Mbit part and
  is often **bigger than the ROM burned on it**, and the game saves in the chip's top block —
  which can sit far above the ROM data. Delta Warp, for example, is a 512 KB ROM yet writes
  its record save at a ~1 MB offset; on a chip sized to the ROM that block is missing and the
  game shows *"SAVE ERROR"*. The capacity is not just a size: a game erases by **block
  number**, and the number→address table is different on each chip (block 17 is `0xFA000` on
  an 8 Mbit card, `0x110000` on a 16 Mbit one), so the wrong capacity sends the erase to the
  wrong place and the save fails on the *second* write — the first one lands on erased flash
  and works, which is what makes it look fine at first.

  Which chip a cart carries cannot be read off the ROM image (Delta Warp is 512 KB on an
  8 Mbit part; StarGunner is smaller still on a 16 Mbit one). So **Auto** lets the cartridge
  answer: on every three-card table in SNK's SDK the save block is the second 8 KB block from
  the top, so `capacity = save address + 0x6000`, and the first time a game programs its save
  the chip re-presents itself at the matching capacity. The `.ngc` grows to the chip size on
  first save (use **Separate file** to leave the ROM untouched). Set it explicitly
  (4 / 8 / 16 Mbit) only to override that.

## The console's clock

A Neo Geo Pocket carries a **calendar chip** and a **coin cell**, and that one battery keeps
*both* the BIOS settings (language, colour) *and* the clock alive. The emulator models it the
same way, so the two are saved together — `saves/system.ram` and `saves/system.rtc`. Pull one
and you would have a console that runs its first-boot setup while still insisting it knows the
date, which is why the reset below clears both.

The clock **runs while you play** and, by default, **keeps running while the emulator is
closed** — shut it for three days and the console comes back three days later, exactly as the
coin cell does on hardware. Choose in **Settings ▸ Console (BIOS) ▸ "Clock while the emulator
is closed"**:

- **Keeps running** *(default)* — what real hardware does.
- **Follow the PC's clock** — set from your computer at every launch. Always right, but it
  overrides whatever date you set on the BIOS screen.
- **Stops, and resumes where it left off** — time freezes with the emulator. Not hardware
  behaviour, but it is **reproducible**, which is what you want when debugging or when a
  game's in-world clock should stay put.

> A **brand-new** console has a flat cell, and the BIOS treats that exactly as hardware does:
> it resets the date to 1998-01-01 on the first boot. That is not a bug — it is the
> dead-battery path. From the second launch on, the BIOS trusts the chip and never touches it.

**The alarm works.** A game that sets one through the BIOS (`VECT_ALARMSET`) gets its
interrupt at the minute it asked for, and an alarm that came due **while the console was
switched off** fires on the next launch. The alarm setting is coin-cell state too, so it
survives a restart.

**Reset the console** (same panel) is pulling the battery out: the console forgets its
language, colour and date, and runs its first-boot setup again like a machine out of the box.
It asks first. **Your games and their saves are not touched** — those live in the ROM and
`.flash` files.

## Controls (default)

| Key | Action |
|-----|--------|
| Arrows / X / C / Enter | D-pad / A / B / Option |
| `Esc` | Exit fullscreen if fullscreen, else pause menu · `P` pause · `F5` reset |
| `F2` / `F4` / `F3` | save / load state · change slot |
| `Tab` (hold) · `[` / `]` | fast-forward · slower / faster |
| `F12` · `F11` · `Ctrl+1…5` | screenshot · fullscreen · window size 1×…5× |
| hold `,` · `.` | rewind while held (release to resume) · step one frame forward |
| `F1` · `H` | debug tools · toggle player toolbar |

**Everything above is rebindable** in **Settings ▸ Controls** (the console buttons) and
**Settings ▸ Hotkeys** (the rest). `Ctrl+1…5` is the one exception — it needs a modifier,
so it can never shadow a console button. Because the hotkeys are matched *before* the
joypad, binding a console button to a hotkey's key would silently kill that button; both
panels detect that and say so instead of letting you wonder.

**Controller** (Settings ▸ Controls) — an XInput pad is read alongside the keyboard:
d-pad *and* left stick move, A/X and B/Y are the two console buttons, Start or Back is
Option. Windows only; elsewhere it does nothing and the keyboard is unaffected.

**Turbo** — A and B can autofire while held, at 5 / 10 / 15 / 20 presses per second. The
rate is counted in *console frames*, so it stays the same under fast-forward.

## Debugging (F1)

Built for people writing NGPC games by hand. Everything below is saved **per ROM**, so
your map of a game survives across sessions.

### Symbols

Drop your linker map beside the ROM (`game.map` next to `game.ngc`) and it is loaded
automatically — the disassembly, the breakpoints, the CPU panel and the analysis reports
all show `player_update+2E` instead of `20A31C`, and you can **type a symbol name
anywhere an address is asked for**. Both map formats are read: the clean-room `t900ld`
form and the **Toshiba `tulink`** map the official cc900 chain emits (bare hex addresses,
long names wrapped onto the next line).

### Stepping and the disassembly

- **Step** `F7` · **Step over** `F8` · **Step out** `Shift+F8` · **Run to cursor** `F4` —
  real instruction-level stepping. Step-over runs a call to completion and uses the stack
  pointer to know it is genuinely back, so recursion does not fool it.
- The listing is **navigable**: `Ctrl+G` or the *Go to* box (address or symbol), page
  up/down, and a *follow PC* toggle. Click the left gutter (or `F9`) to **arm a breakpoint
  on that line**.
- An undecodable byte shows as `??` and the listing resyncs and carries on, instead of
  stopping dead and hiding the rest of the routine.

### Call stack

How execution got here — the chain of callers with their return addresses, and
double-click to jump to any frame. Tracked as a shadow stack while the debug window is
open (about 1 % of emulation speed, nothing while you are just playing): a call is
recognised by the return address landing on the stack and a return by the stack unwinding
past it, so a plain `push` is never mistaken for a call.

### Events — the raster timeline

Every video-register write and every interrupt, plotted at the **scanline and cycle** it
happened on. This is the view for raster work: a mid-frame scroll split, an HBlank HUD or
a palette swap on a given line is correct or broken purely as a function of timing, and no
write log can show that.

### Watch, breakpoints, memory

- **Watch** — name memory addresses and see their live value (1/2/4 bytes, hex or
  signed/unsigned). Each row can also:
  - **break on value** — pause when it hits a condition (`=`, `≠`, `<`, `>`, `change`);
  - **break on write** — pause when *any* code writes it, naming the **PC that did it**;
  - **break on read** — the same for reads, which answers "does anything actually *use*
    this?" (instruction fetches are excluded, so it means the code really loaded the value);
  - **lock** — freeze the address to a value each frame (test "what if HP never drops").
- **Breakpoints** — pause when PC reaches an address (or a symbol name), with an optional
  guard written in C:

  ```
  a == $44 && fz            [$4812] == 0 && pc < $202000            {_score} > 1000
  ```

  Registers (`a wa xhl pc sp`…), flags (`fz fc fs fh fv fn`), memory (`[x]` 1 byte, `{x}`
  2, `[x,4]` 4), symbols, and `&& || ! + - * & | << >>`. A condition that does not compile
  is **named as you type** — it still fires (a guard that silences a breakpoint you asked
  for would be worse), but you are told why instead of wondering. Old
  `ADDR.size OP VALUE` conditions keep their original meaning.
- **Memory** — an **editable** hex grid: click a byte, type two hex digits. Symbol names
  work in the address box. **Highlight accesses** tints bytes the game just read (blue) or
  wrote (red), fading over about a second. The core has one read-log and one write-log
  window, so while highlighting is on it owns them and read/write watchpoints are
  suspended — the panel says so rather than letting them silently clobber each other.
- **RAM Search** — find *where* a value lives: start a search, let the game change it, then
  filter. Absolute (`=` `≠` `>` `<` `≥` `≤`), relative (`changed`, `=prev`, `▲`, `▼`),
  **by amount** (`+N` `−N` `±N` — "a hit always costs 3 HP" is a far sharper filter than
  "it decreased"), and **by change count** — tick *count changes*, hold right for six
  frames, then ask for the addresses that changed exactly six times. **Undo** takes back a
  bad pass, and **unaligned** finds a 16/32-bit value that does not sit on a multiple of
  its size. Double-click a hit to name and watch it.
- **Trace to file** — every instruction with, optionally, the registers it wrote and every
  memory address it read or wrote.
- **Audio** — live per-channel monitor (3 square + noise): period → frequency → **note**,
  L/R volume, plus an oscilloscope of the output and the sound Z80's state. **Mute / solo**
  any channel to isolate it, watch the raw chip-write log, and **record the music** — save it
  as a **`.vgm`** (Furnace / VGM players) or as a **`.ngps` song** for the NGPC sound creator.
- **Layers** — the same idea, applied to the picture: **show or hide** each of the five
  video layers (scroll plane 1, scroll plane 2, and sprites split by priority) **while the
  game runs**, with a *solo* button per layer. On this machine text and artwork are always
  on separate layers — the chip has no other way to put one over the other — so this is how
  you find out which plane a HUD, a dialogue box or a title logo actually lives on, without
  editing a byte of VRAM. **Export PNG** saves what you are looking at at **160×152, one
  file pixel per console pixel**: no scaling, no screen filter, and the 4-bit colour is
  written back losslessly, so a title screen's background comes out as a clean plate you can
  edit and re-import. Hiding a layer changes the *picture* and nothing else — no machine
  state, no timing — so ticking everything back on restores the frame bit for bit.
- Plus the live viewers: CPU, palette, tiles, sprites — each with an Export button. In the
  **Tiles** view, **hover any tile** to read its index, its **VRAM address**, which planes
  reference it, and its 16 raw bytes; **click to copy** that block, or select it from the
  status line — the numbers you need to poke or replace a tile.

### Load — live resource gauges

Green→red bars for what a game actually runs out of on this hardware, updating as it plays:

- **Sprites (OAM)** — active entries of the 64 the hardware has;
- **Char RAM** — distinct tiles referenced, of 512 in character RAM.

Both are read straight from VRAM, so they are exact counts. A third bar, **Frame rate**,
is the honest CPU-headroom signal: it shows whether the game finishes its work in time
(60 = keeping up), inferred from the sprite table changing, and reads grey on a still
screen where nothing updates. A raw CPU-cycle percentage is deliberately *not* shown — on
this machine the slow cartridge bus keeps the CPU busy every frame, so it would read ~100%
always and tell you nothing; whether the game holds 60 is the number that matters.

### Fan-translation — Text · Crack · Pointers · Compare

Tools for translating a game, all working on **any ROM**: they read live memory through a
character table **you** load (`.tbl`, the romhacking-standard format), and every result
shows its **ROM file offset** (`address − 0x200000`) so a hit maps straight back to a byte
in the cartridge file. Nothing here is specific to one game.

- **Text** — load a `.tbl`, then **decode** any region into readable strings, **search**
  for a phrase by the exact bytes it encodes to, or **scan** a whole region for every
  string at once and export it — a script dump to translate offline. A **relative search**
  finds a word under an *unknown* encoding by the spacing between its letters, needing no
  table at all — the first tool when you are cracking a game from scratch.
- **Crack** — type words you can read on screen and it **builds the table for you**: each
  is located by relative search (or pinned with `word @ offset` when a common word matches
  in many places), and the real bytes under it become a `.tbl` you can edit, save, or use
  in the Text tab straight away. It reads the actual bytes, so a non-linear encoding cracks
  as easily as an alphabetical one.
- **Pointers** — **find every pointer to an address** (the entries to patch when a string
  moves) or **locate the pointer tables** themselves. Pointer width 16 / 24 / 32-bit LE,
  with a base added to the stored value for bank-relative offsets, and a tolerance to catch
  a pointer into the middle of a string.
- **Compare** — byte-**diff the running cartridge against a second `.ngc`**. An existing
  translation is an oracle: the ranges it changed *are* the text, and with a table loaded
  they are shown decoded on both sides.

> These tools **find and read**; they do not write the ROM. Injecting the translated text
> and repointing it back into a `.ngc` is a separate build step — this is the discovery and
> verification side of that workflow.

## ROM analysis

Right-click a game in the Library ▸ **Analyze ROM…**. Because the core models the machine
closely enough to *judge* a cartridge rather than merely run it, it can check a build the
way hardware would. Two passes, about a second:

**Static** — the header a real console validates before it will boot a cart (the copyright
string, the 24-bit entry vector, the mode byte), the image size against real 4/8/16 Mbit
flash parts, and how much of the image is erased padding. This is the "it works in my
emulator but the console does nothing" class, and it costs nothing to check.

**Dynamic** — it boots the ROM at its entry vector with cartridge wait-states modelled, and
**plays**: a fixed script presses A, Option, B and directions, held for several frames and
released, so it gets past a title screen into real code. It then reports:

- **globals read before they were written** — work RAM holds whatever the previous game
  left, so such a variable returns the last game's data on hardware while reading zero on
  most emulators. The report gives the **frame of the first write**, which is what makes
  the finding triageable: written by the same instruction that read it is usually a counter
  and harmless; written much later, or never, means real code ran on a value that did not
  exist yet;
- **stack bytes read before written** (locals used before assignment) — reported
  *separately*, because it is much weaker evidence than a global;
- **writes into unmapped space**, which the bus discards without the program ever knowing;
- crashes, whether the cartridge code ever ran, and whether the game fits the frame budget;
- **code reached** — how many distinct instruction addresses executed, so "the analyzer
  looked at this ROM" is a number instead of a claim.

### ⚠️ What the analysis cannot tell you

**This is a dredging tool, not a proof of correctness. Read its output as leads, not
verdicts.**

- **The robot plays blind.** It presses buttons on a fixed schedule with no idea what is on
  screen. It roughly **doubles to triples** the code reached (measured: +119 %, +213 %,
  +100 % on three games) — but a game that needs a real sequence, a menu navigated or a
  password entered, will simply not be reached. A large majority of most cartridges is
  never executed, and **anything never executed is never checked**.
- **"No findings" means "nothing found on the path that ran."** It is not a clean bill of
  health.
- **A finding is a signal, not a diagnosis.** When one report was traced back to its source
  code, of the issues it raised: some were real and harmless (a counter incremented from an
  uninitialised value, used only differentially), one was real and worth fixing (a flag
  polled by the vblank interrupt for seven frames before anything wrote it) — and some were
  **the debugger's own instrumentation contaminating the measurement**, since fixed. Expect
  to have to confirm things yourself; the disassembler and symbols are there for that.
- **Unmapped writes are usually not yours to fix.** Several commercial carts do it from what
  looks like one shared SDK routine, and in every case measured nothing ever reads the
  address back. It only matters if something depends on the value.
- **A stop is not always the ROM's fault.** If the core meets an instruction it does not
  implement, the report says so *and names the emulator*, because reporting it as a
  cartridge defect would send you hunting a bug in your own game.
- **None of this is hardware-validated.** The checks follow the documented behaviour of the
  machine and this core's model of it. A real console is the arbiter.

## Rewind — how it works and its limits

Rewind keeps a ring of recent frame snapshots so you can step **back** (`,`) and **forward**
(`.`) through what just happened. Buffer length is set in **Settings ▸ General ▸ Rewind
buffer**: **Off**, or 10 / 20 / 30 seconds. Each snapshot is ~48 KB, so the cost is roughly
**10 s ≈ 29 MB, 20 s ≈ 58 MB, 30 s ≈ 86 MB** of RAM held while a game runs (Off = no cost).
The default is 10 s.

What it restores is the same thing a **save state** restores: the CPU plus the whole working
image (I/O, RAM, VRAM). That means the **visible frame and game memory come back exactly**,
but a few pieces of hardware timing that live only inside the core — the sound chip's stream,
the timers, the scanline position — are **not** snapshotted and re-sync on the next frame. So
rewind is frame-accurate for *what you saw and what's in memory*, but audio may click at the
seam and cycle-exact timing right after a rewind is approximate. It's a "what did I just see?"
tool, not a deterministic TAS engine.

## Known issues

- **Cool Boarders Pocket freezes on the end-of-race *REWARD* screen** when **Cart flash size**
  is **Auto**. That screen saves, and this is a genuine 8 Mbit cartridge that saves in *its
  own* top block — but Auto presents any under-filled cart as 16 Mbit, which changes the
  block numbering, sends the erase somewhere else, and leaves the save block never cleared. A
  flash cell only goes down, so the write can never take, and the BIOS waits for it forever.
  **Set Cart flash size to 8 Mbit** and it saves and moves on. Auto's rule only recognises a
  save in the *second* 8 KB block from the top; this game uses the first, so it never
  self-corrects. Other genuine 8 Mbit carts that save the same way will behave the same.
- **A save state can carry an old fault back with it.** A state is the whole machine
  including work RAM, so anything a fix corrects *at boot* is restored to its broken value by
  a state captured before the fix. Two known cases, both fixed for a fresh run and both still
  reproducible from an old state — **start a new run to see the fix**:
  - *Metal Slug — 2nd Mission*, fire and jump dead in-game: its copy-protection flag lives in
    work RAM. (The hand-off now leaves character RAM as a real BIOS boot does, which is what
    the game checks.)
  - *Delta Warp*, `"SAVE ERROR!"` on the second save onward: the flash card type the BIOS
    reads lives in work RAM at `0x6C58`. (The chip now takes its capacity from where the game
    actually saves — see **Cart flash size** above.)

If you hit a bug, a ROM fault auto-writes a `crashes/*.txt` report (reason, PC, opcode,
registers, memory & stack) — attach it, ideally with a save state, when reporting.

## Thanks

The emulator speaks more than one language because people sent theirs in.

- **Português (Portugal)** — [@spotanjo3](https://github.com/spotanjo3)
  ([#1](https://github.com/Tixul/Ngpcraft_emulator/issues/1))

Adding yours is adding one JSON file, no Python required, and an unfinished one is
mergeable — see [TRANSLATING.md](TRANSLATING.md). You get credited here, in the file
itself, and as a co-author of the commit.

## Legal

This is a clean-room emulator. It ships **no copyrighted ROMs or BIOS**. Neo Geo Pocket
is a trademark of SNK; this project is not affiliated with SNK.

## License

MIT — see [LICENSE](LICENSE).
