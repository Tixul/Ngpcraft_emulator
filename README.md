
# NgpCraft Emulator

A **Neo Geo Pocket / Neo Geo Pocket Color** emulator: a fast native C++ core
(TLCS-900H + Z80 + K2GE video) driven by a PyQt6 desktop shell.

Its timing is **calibrated against real hardware** — instruction-fetch wait-states on
the cartridge flash, silicon-measured MUL/DIV and LDIR costs — so self-timed games
(Cool Boarders Pocket, Densha de Go) run at their true 30 fps instead of the ~2× too
fast that most emulators show. The measured values, and the one default that trips up
code driving the core as a library, are in
[**Timing — wait states**](#timing--wait-states-and-the-default-that-catches-embedders).

Instruction coverage is checked the same way: a sweep of a 90-cartridge corpus, each
driven for 400 frames, currently finds **no ROM stopped by a missing opcode**. That sweep
is a feature you can run yourself — see [ROM analysis](#rom-analysis).

<img width="1076" height="761" alt="emulateur01" src="https://github.com/user-attachments/assets/24dd060d-5b35-4ef6-83dd-916a618ba244" />

## Available versions

| Version | Installation and details |
| --- | --- |
| Desktop | Python/Qt application described below; standalone builds in [Releases](https://github.com/Tixul/Ngpcraft_emulator/releases). |
| Libretro 0.4 | [RetroArch core](libretro/README.md), with sources and a [Windows x64 prebuilt](libretro/prebuilt/windows-x86_64/). Compiles the same `cpp/` sources and `hle_bios/` firmware as the desktop. |
| Android 1.1 | [Native Android application](android/README.md), Android 7.0+, universal signed APK. Prepared for manual publication in Releases. |

The Android 1.1 and Libretro 0.4 update includes the desktop flash timing model and current clean-room HLE BIOS. Android sources are maintained separately; this repository contains its installation notes, while APKs are distributed as release assets.

## Features

- **Library** with cover thumbnails (grid / list / compact), live-reflowing — plus
  **search**, **sort** (name, last played, most played, playtime, recently added, size,
  with a direction toggle), **favourites** ★ and a **never-played** filter. Play count,
  playtime and last-played are tracked per game. Covers are rendered from the game itself,
  or [pick your own](#your-own-cover-art) — one that updates never overwrite.
- **No BIOS needed** — the built-in clean-room HLE image runs the games, **and drives the
  saves, the link cable and the clock**. A real dump stays optional, for the boot animation,
  the console's setup screens, and certainty — see [Games & BIOS](#games--bios).
- **Console boot** — with a real BIOS, *Boot BIOS* powers the console on for real: the
  Neo Geo Pocket intro plays and the game then boots on its own, exactly like hardware.
- **Graphics filter plugins** — drop your own scalers (Scale2x, HQx, 2xSaI…) in a folder of
  your choosing and they appear in the filter list. None are shipped: those implementations
  are copyleft and this emulator is MIT. See [Filter plugins](#graphics-filter-plugins).
- **Video**: integer / fit / stretch scaling, scanline / LCD-grid / CRT filters,
  colour profiles, real fullscreen — which **hides the sidebar and toolbar** for the game
  alone (optional); **double-click or `Esc`** returns to windowed. The canvas follows the
  window; size presets `Ctrl+1…5`.
- **Black-and-white cartridges, in colour** — an NGP game on an NGPC is *colourised*, the way
  a Game Boy game is on a Game Boy Color. **Both machines are selectable**, and the choice is
  a real console swap: bring your own **monochrome NGP BIOS** and the emulator boots that
  handheld, greys and all — colour games that behave differently on it (*SNK vs. Capcom*) do
  so here. See [Monochrome cartridges](#monochrome-cartridges-on-a-colour-console).
- **Three BIOS slots, one selector** — your colour dump, your mono dump, or the built-in
  clean-room image; tick the one that boots. The emulator identifies each image and tells you
  which console it belongs to. **No BIOS is shipped.**
- **Save states** — 8 slots per game (toolbar or `F2` save / `F4` load / `F3` slot).
- **In-game saves** — the game's own flash save, stored in the ROM, a separate file, or both.
- **A console that remembers** — the coin cell keeps its BIOS settings *and* its clock, so
  the date is still right next time. The **RTC alarm** works too, and goes off on time.
- **Speed control** — fast-forward (hold `Tab`) and 0.25×…4× (`[` / `]` or the toolbar).
- **Rewind** — hold `,` (or the ⏪ toolbar button) to run the game backward; release to
  resume. `.` steps one frame forward. Buffer length configurable (Off / 10 / 20 / 30 s).
- **Screenshots** (`F12`, folder configurable), **FPS overlay**, and a **player toolbar**
  that can auto-hide when the mouse goes still and reappear on the next move (optional).
- **Controller support (beta, not yet hardware-tested)** — cross-platform game pads via
  SDL2 (Xbox, PlayStation, Nintendo and generic pads on Windows / macOS / Linux),
  one per player, alongside the keyboard, plus **turbo / autofire** on A and B at 5–20
  presses per second. ⚠️ *The button mapping has not been validated on a real controller
  yet — see [Known issues](#known-issues).*
- **Two players & link cable** — the NGPC link cable is emulated end to end: play two
  consoles **on one PC** (a second window), over your **LAN**, or **online** (a built-in
  lobby, or share a direct address). Each player has their own controls. And when the ping
  is what hurts, **🪞 mirror mode** keeps the match at full speed by sending only the
  controller bytes. See [Two players & link](#two-players--link-cable).
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
  straight from VRAM, audio analysis with **VGM export**, and a **Link** tab that watches
  the link cable byte by byte, tells you **which end is at fault** when a two-player game
  will not connect, and can inject bytes, loop a console back on itself or break the wire
  on purpose. See [Debugging](#debugging-f1).
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
- **Hardware-safety findings** — two things a real console minds and the running code
  cannot see: a cartridge stack that crossed into BIOS-owned RAM, and a watchdog left to
  starve. The core counts them while the ROM runs — it does not halt, because neither
  halts a real console either — and the analysis names the code that did it. See [the
  hardware-safety contract](specs/HARDWARE_SAFETY.md).
- **Translated interface** — English, French, Japanese (日本語) and Portuguese,
  switchable live, no restart. One JSON file per language in [`lang/`](lang), so adding one
  is adding a file and needs no code: see [TRANSLATING.md](TRANSLATING.md). Contributions
  welcome — including corrections to a language already there.

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

No ROMs are included — provide your own. **A BIOS dump is genuinely optional**: the emulator
ships a small **clean-room HLE BIOS** (`hle_bios/`) and uses it when you have no `bios.bin`,
so games run, save and link out of the box.

- Put `.ngc` / `.ngp` files in **`roms/`** (or pick any folder with **Choose ROM folder**,
  in the Library).

**What each one gives you**, so the choice is yours rather than a recommendation:

| | built-in HLE BIOS | your own `bios.bin` |
|---|---|---|
| Running games | ✅ 71 of the 73-game commercial corpus render | ✅ the reference |
| In-game saves | ✅ real flash driver, both dies of a 4 MiB cart | ✅ |
| 2-player link cable | ✅ real ring driver, same byte rate | ✅ |
| Clock / RTC / alarm | ✅ incl. *Set by hand* | ✅ + the BIOS setup screen |
| Library covers | ✅ | ✅ |
| System font (SYSFONTSET) | ✅ ours, corrected 30/08 — it was unreadable before | ✅ the console's own |
| NEO·GEO POCKET intro, setup screens | ❌ deliberately skipped | ✅ |
| Console settings kept in the coin cell | via the emulator's settings | ✅ its own screens |
| A game that reads BIOS work RAM directly | ⚠️ sees ours, not SNK's | ✅ |

**The reserve, stated plainly:** the official BIOS is the one that gives *certainty*. It is
what the hardware runs, so anything a game does — including things no test of ours predicted
— behaves as it does on the console. The clean-room image reproduces the documented
*contract*, not SNK's internal state, and its coverage is measured rather than guaranteed:
where a game pokes at BIOS internals instead of calling the API, it sees our layout. Two known
consequences are in [Known issues](#known-issues) (*Metal Slug — 2nd Mission* refuses to fire
or jump under the HLE image, and the mono NGP needs its own dump to boot as itself).

So: **start with the HLE image**, and reach for a real dump when you want the boot animation,
the console's own setup screens, or certainty on a game that misbehaves.

> **Played, not just measured.** The corpus numbers above come from automated runs; the two
> features people ask about first have also been confirmed by hand, **on the HLE image** —
> the **link cable** in *SNK vs. Capcom — The Match of the Millennium* (two consoles, in a
> real match) and an **in-game save** in *Cool Boarders Pocket*. That is evidence, not a guarantee of the whole
> library: what is verified is verified, and the rest is still "should".

**Three BIOS slots, one selector** — *Settings ▸ Console (BIOS)*. Each line has a radio button, and
the one you tick is the one that boots:

| Slot | What it is |
|---|---|
| **NGP Color BIOS (NGPC)** | your own `bios.bin` dump — certainty, console boot, setup screens |
| **NGP monochrome BIOS** | your own dump of the **original** Neo Geo Pocket's BIOS — see [Monochrome](#monochrome-cartridges-on-a-colour-console) |
| **Built-in HLE BIOS** | ours, clean-room, ships with the emulator — no file needed |

A line under the three says what is actually loaded, and checks it: the emulator reads the
image and tells the two consoles apart (the NGPC's BIOS stamps the console-type byte
`0x6F91` with `0x10` and drives the K2GE colour registers; the mono NGP's stamps `0x00` and
never touches them). Put a colour dump in the mono slot and it says so, instead of leaving
you to wonder why a game still behaves as if it were on an NGPC. A slot you leave empty
falls back rather than refusing to boot, and the same line tells you it did.

A real `bios.bin` next to the app is picked up automatically, as before; the mono dump is
found the same way under `ngp_bios.bin` / `ngp_bios.ngp` / `bios_ngp.bin`.

> **No BIOS dump ships with this emulator, and none ever will** — both user slots read files
> *you* supply. The only BIOS in the repository is `hle_bios/bios_hle.bin`, which is our own
> clean-room image.

### Your settings are backed up

On Windows the emulator's own preferences live in the registry, which has no undo, no history
and nothing beside it saying what used to be there. A single stray wipe — a botched uninstall,
a "registry cleaner", a script run against the real store — takes the BIOS path, the ROM
folder, every key binding and every preference, silently, and the app comes up looking like a
fresh install.

So it keeps **its own copy beside itself** (`settings_backup.ini`, plus the previous
generation) and **puts it back automatically when the store comes up empty**, saying so once
rather than restoring your preferences behind your back. Two rules make that a safety net
instead of a second way to lose them: an **empty store is never saved over a good copy** —
backing up the damage is the classic way a backup destroys what it was meant to protect — and a
restore takes the **richest** generation, not the newest, because after a wipe the newest copy
is of the damage.

*(Prefer everything in one folder? **Portable mode** keeps settings in a `config.ini` next to
the emulator instead, and the backup follows it there.)*

### Settings the CONSOLE owns

Three things the console tells the cartridge, which the BIOS setup screens decide on real
hardware:

- **Cartridge language** — English or Japanese. Nothing to do with the emulator's own
  language: **bilingual cartridges read this** (system byte `0x6F87`) and pick their script
  from it. Baseball Stars, Puyo Pop and Neo Geo Cup '98 visibly change with it.
- **Clock** — hardware (the console's own battery-backed clock), the host's clock, or paused.
- **Console** — an **NGP Color**, or the original **monochrome NGP**. It applies to every
  cartridge, not only black-and-white ones: a colour game can notice which machine it is in
  and behave differently (*SNK vs. Capcom* shows a different screen). Selecting the mono
  **BIOS** selects the mono **console** — you cannot boot one machine's BIOS on the other's
  silicon.

**Who decides: the BIOS, or these settings.** A real BIOS has a **setup screen**, and that
screen is the console's own control panel — what you set on it goes into the battery RAM and
is **kept**, so it wins, in both start modes. Use the **Boot BIOS** button (Library) to reach
it: it powers the console on with no cartridge, which is the only way to see those screens,
since launching a *game* auto-completes the setup rather than leaving you on a questionnaire.

These settings are what answers when the console cannot: the built-in **HLE BIOS** has no
setup screen, so they apply to it always; and on a **brand-new** console they are the answer
we give the BIOS's first-boot wizard on your behalf — once. From the next launch the console
remembers, and its own screen is what changes it. **Reset the console** (pull the battery)
starts that over.

> Previously the settings overrode the BIOS screen at every launch *and* the screen's own
> choices were never saved — so the language kept reverting, the colour theme never stuck,
> and a first boot re-ran the setup wizard for ever. Both are fixed: the BIOS page of the
> coin cell is persisted as the BIOS left it.

> **One commercial game we know of wants a real `bios.bin`.** *Metal Slug — 2nd Mission*
> checks that the console really booted through its BIOS, and quietly disables **fire and
> jump** when it decides it did not. With the real BIOS both start modes satisfy the check.
> Under the HLE BIOS the game still runs and looks perfect; you simply can never shoot or
> jump. This is the shape of what "certainty" buys you — one game, found by playing it.

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

A cover is a real boot, so it needs a BIOS — but the built-in clean-room HLE BIOS counts,
so covers render **out of the box**, no `bios.bin` required.

**Each cover is rendered once.** Drop a new game in the folder and only *that* game is
booted; everything already on disk is served from the cache. Changing the BIOS does **not**
throw them away — re-rendering a real collection means booting every game in it, minutes of
work for a picture that differs by a shade. When you do want them redone, the Library's
**↻ Covers** button does the whole library, and asks first. A game that never reaches a
real screen is left uncovered and retried next launch, so a blank frame is never cached.

**Zipped collections show every game they hold.** ROMs are usually shipped zipped, and an
archive often holds more than one cartridge. Each game inside gets its own card, its own
save, its own savestates and its own cover — before, a collection quietly booted whichever
member happened to be the largest, and all of them would have shared one save file. `.zip`
works everywhere; `.7z` needs either the `py7zr` package or 7-Zip installed. Opening a
collection by hand asks which game you want. (An archive holding a *single* game is
untouched, so nothing in an existing library is renamed.)

**Two cards that look like the same game are two files.** Card titles drop the usual dump
tags — `Faselei! (Europe)` reads *Faselei!* — so two dumps of one game used to print the same
name under two covers. A title that is not unique now keeps its full file name, and hovering
any card shows the full path. (The library scan itself lists each **physical** file once: it
no longer walks through junctions and symlinks, which could make a single ROM appear several
times — or, with one pointing back up the tree, cut the scan short and leave the library half
empty.)

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

## Graphics filter plugins

Built in: **scanlines**, **LCD grid**, **CRT** — effects drawn *over* the picture. The other
family, the **scalers** (Scale2x, 2xSaI, SuperEagle, HQx, xBRZ…), rebuilds the picture at 2×
or 4× by looking at each pixel's neighbours. Those are not shipped, and not because they are
hard: at 160×152 the whole screen is 24 320 pixels and any of them costs a millisecond or
two. They exist as **copyleft implementations**, and this emulator is MIT — an algorithm
cannot be owned, an implementation can, and theirs are not ours to redistribute.

So it **loads** them instead. *Settings ▸ Graphics ▸ "Filter plugins folder"*: point it at a
folder of your own and every filter in it joins the filter list. **No folder is created for
this** — the setting is empty by default and the feature simply stays off.

A filter is one Python file with three names:

```python
NAME  = "Scale2x"          # what the filter list shows
SCALE = 2                  # how much bigger the output is
def apply(rgb):            # (h, w, 3) uint8  ->  (h*SCALE, w*SCALE, 3) uint8
    ...                    # numpy in, numpy out
```

`apply` gets the picture **after** the colour profile and **before** scanlines / LCD / CRT,
so the two kinds compose: Scale2x *and* an LCD grid on top. Whatever magnification the plugin
did counts toward the window scale, so nothing comes out bigger than you asked for.

Under the folder field, the emulator lists what it found — **and what it refused, with the
reason**:

```
2 filter(s) loaded: Scale2x, HQ2x
⚠ hqx_old.py was ignored — apply() returned (4, 5, 3), expected (8, 10, 3)
```

Every plugin is **run once on a test picture** when it loads: declaring the three names is
not evidence that it works, and a filter that returns the wrong shape would take the display
down with it. One that breaks later, mid-game, costs the *filter* — the game keeps running,
unfiltered, and the panel says why.

> **Licence.** A plugin you download is yours to run, whatever its licence; nothing about it
> is distributed by this project. The example above (Scale2x) is four comparisons per pixel
> written from the published rules — a good starting point for your own.

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

**Which machine to be** — *Settings ▸ Graphics ▸ "Console"*, or simply by selecting the
mono BIOS in *Settings ▸ Console (BIOS)*:

- **NGP Color (K2GE)** *(default)* — what this emulator is.
- **NGP (K1GE) — monochrome** — the original handheld. The cartridge is told it is in a mono
  console, the 12-bit palette it would colourise through does not exist on that silicon, and
  the picture is resolved the way that hardware resolves it: the 3-bit LEVEL look-up **is**
  the shade (K1GE Tech Ref §3-7), eight greys straight out of the panel. The game stays grey
  **of its own accord** — it never runs its colour code. This is not a filter laid over the
  picture afterwards.

**It is the console that answers, not the cartridge.** A game asks which machine it is in by
reading `0x6F91`, and that byte belongs to the console — each BIOS stamps its own machine's
id there while booting. So a **colour** cartridge in the mono NGP is told *mono*, and the
ones that care act on it: *SNK vs. Capcom — The Match of the Millennium* opens on a
completely different, black-and-white screen there, which is exactly what it does on real
mono hardware.

**With the mono BIOS** *(Settings ▸ Console (BIOS) ▸ NGP monochrome BIOS)* you also get that console's
own boot: its NEO·GEO POCKET intro, in greys, then the game. Without a mono dump the colour
BIOS still boots the machine with the K1GE restrictions applied — close, and the BIOS panel
says which of the two you are running.

Takes effect the next time a game is started.

> **What is *not* modelled.** This is a K2GE restricted to K1GE behaviour, not a separate
> K1GE implementation: the console-type byte, the missing colour palette and the grey
> resolution are modelled, but the machine still answers as a K2GE anywhere a game probes
> the video chip itself, and the **NEG** (inverted display) bit is not applied on the mono
> path. Mono-console behaviour has been checked on a handful of games, not the whole corpus
> — reports welcome.

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

  Commercial games reach the flash through the **BIOS** — and the built-in clean-room BIOS
  now drives the chip itself, so **saves work with no `bios.bin`** (both dies of a 4 MiB
  cartridge included). Homebrew that drives the flash directly saves either way.

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
  8 Mbit part; StarGunner is smaller still on a 16 Mbit one) — a real console reads it off the
  chip, and a dump has thrown that away. So **Auto** lets the cartridge answer, because it
  still says which card it is: a game does not hand the BIOS an address, it hands it a **block
  number** taken from the table for *its* card, and block 17 means an 8 Mbit card whatever we
  were presenting. The emulator reads that number as it goes past and re-presents the chip to
  match, before the erase moves. Homebrew that drives the chip directly is caught the same way,
  by the address it writes to.

  The `.ngc` grows to the **chip's** size on first save — not to the size we guessed — so a
  512 KB cart on an 8 Mbit part becomes a 1 MB file and stays right on every later save. (Use
  **Separate file** to leave the ROM untouched.) Setting the size explicitly (4 / 8 / 16 Mbit)
  is only an override and should no longer be needed.

## The console's clock

A Neo Geo Pocket carries a **calendar chip** and a **coin cell**, and that one battery keeps
*both* the BIOS settings (language, colour) *and* the clock alive. The emulator models it the
same way, so the two are saved together — `saves/system.ram` and `saves/system.rtc`. What the
BIOS setup screen writes is what is kept: the settings page of that RAM is persisted **as the
BIOS left it**, so a language or a colour chosen on the console's own screen is still there
next launch. The clock
is handed to the cartridge identically whether you run a real `bios.bin` or the clean-room
image, and games read it through the BIOS the same way (Ganbare Neo Poke-kun's Tamagotchi-style
timers, for instance). Pull one
and you would have a console that runs its first-boot setup while still insisting it knows the
date, which is why the reset below clears both.

**Two-player on one PC is two consoles, and two consoles cannot share one battery.** Player 2
boots from your configured cell — same language, same date, which is what somebody with two
consoles would have — but it is **read-only** for it: player 2 never writes the cell back, so
closing its window cannot stamp its clock over yours. Its crystal also runs on its own
sub-second phase: both consoles used to come up with a bit-identical clock down to the
oscillator's own phase, which is not a machine that ever existed — on hardware two coin cells
never share one. A game seeding on the *second* alone can still see both consoles agree about
half the time; giving player 2 a genuinely separate date would mean a second saved cell, which
is deliberately not done.

⚠️ This was found while chasing a homebrew author's report of a link game electing **two
hosts**, on the theory that an identical clock gives both consoles an identical random stream.
**That theory is not established** — the author reports their RNG does diverge between the two
windows, and their own handshake has a sufficient bug of its own (it compares its current draw
against the peer's previous one, so the two consoles weigh different pairs). What is fixed here
is the shared cell, which is a fidelity and save-safety defect on its own terms and was
measured directly. Do not cite it as the cause of that report.

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
- **Set by hand** — the console's clock starts at a **date and time you choose**, shown in a
  field under the setting, at every launch. On hardware that choice belongs to the BIOS setup
  screen; the built-in **HLE BIOS has no such screen**, so this is how you set the clock when
  running it — and it works with a real BIOS too. To set it once and then let it run, pick
  this mode, start a game, then switch back to *Keeps running*: the date you set is now what
  the coin cell remembers.

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

**Controller (beta)** (Settings ▸ Controls) — a game pad is read alongside the keyboard
via **SDL2**, so it works on Windows, macOS and Linux and with most brands (Xbox,
PlayStation, Nintendo, generic). The d-pad *and* left stick move, A/X and B/Y are the two
console buttons, Start or Back is Option. Player 1 uses controller 1, player 2 controller 2
(the *Controller #* selector). Requires `pygame`; without it, Windows falls back to XInput
and elsewhere the keyboard is unaffected. ⚠️ **Not yet validated on real hardware** — the
button mapping may need adjusting; please report what your controller does.

**Turbo** — A and B can autofire while held, at 5 / 10 / 15 / 20 presses per second. The
rate is counted in *console frames*, so it stays the same under fast-forward.

## Two players & link cable

The NGPC's link cable is emulated as what it really is — a byte pipe between two
independent consoles — so there is no rollback or lockstep to worry about. From the
player toolbar's **🔗** button:

- **Two players — this PC** — opens a second window. Player 2 loads their **own** cartridge
  (its flash save loads normally); each player has their own controls (keyboard or pad),
  routed by player regardless of which window has focus. **Pause, speed and fast-forward
  apply to both consoles**, from either window: they are wired together, and a console left
  running against a partner that has stopped answering makes the *game's* link protocol time
  out, which reads as a link failure rather than as a pause.
- **Online lobby…** — connect to a lobby server, set a nickname, and **create** or **join**
  a game (public, or private with a password). The list shows each game's title, server
  name and creator. When you create one you also pick **which link it is for** — the
  cable, or 🪞 mirror play — and the list marks each room 🔗 or 🪞 so whoever joins starts
  the same mode without being asked. Needs a server — run the tiny one in
  [`server/`](server/README.md) (pure stdlib, deploy free) or your own.
- **Host / Join (direct address)** — the simplest path: one player hosts on a port, the
  other dials `ip:port`. Works on a LAN directly; over the internet it needs the host to
  be reachable (port-forwarding, or a zero-config option like **Tailscale**/**playit.gg**).
  The host dialog auto-detects your public IP, gives a ready-to-share line, and **explains
  the risks of opening a port** honestly.

  Whoever is ready first simply waits: **joining retries** until the host clicks Host, so the
  two of you do not have to press within the same instant. The wait is bounded and can be
  taken back at any time — **✖ Cancel the pending connection**, at the top of the 🔗 menu
  while an attempt is running, or just close the host panel. And when it does not work, the
  message says *which* thing failed — nobody listening, a name that does not resolve, a port
  already taken, a firewall — instead of an error number.

  ⚠️ **If nothing gets through at all, it is almost never the emulator: it is the inbound
  connection.** Behind a router with no port forward — or behind carrier-grade NAT, where no
  forward is even possible — nothing can reach you, whatever you click. Use the **online
  lobby** instead: it relays through the server, so it needs no port forwarding and no public
  address. Think of direct mode as the LAN/Tailscale option.
- **🪞 Mirror play — host / join** — the *other* online mode, for when your ping is the
  problem (also reachable from the lobby above, as a 🪞 room). Instead of sending the cable's bytes, each PC runs **both** consoles and only
  the **controller bytes** cross the network, so the cable is local and the delay is spent
  on slightly late controls rather than on the speed of the game. **Only the host picks the
  input delay** (roughly ping-in-ms / 17, plus one); whoever joins takes the host's, so
  there is no number for two people to get wrong. Hosting shows you your own address, the
  same panel as the direct cable mode. The two consoles swap their
  cartridges when the session opens — a few MiB, once, with progress on screen — so you
  may hold **different games and different saves**, exactly like two people with two
  cartridges. Your own settings travel with your console too (cartridge language, NGP or
  NGPC, flash size), so the other PC mirrors *your* machine rather than rebuilding it from
  theirs. What must match is the **BIOS and the emulator build**, because those
  decide how the code runs; it refuses to start otherwise, since a mismatch does not
  fail loudly, it drifts. Savestates, rewind and reset are refused during a mirror match
  for the same reason.

  ⚠️ **"Same build" means the same .exe on both PCs** — compare the file's date, not a
  version shown anywhere. The check behind it inspects the emulation core, and two builds
  can share a core while differing elsewhere: they will accept each other and fail anyway.

Both players must run a **compatible game** (same title), exactly like real hardware.

### How fast an online game runs

A link game sends a byte and **waits for the answer before it goes on**, so it advances one
exchange per round trip: the delay on the wire is not a detail of the connection, it *is* the
speed of the game. The tell is that the sound stays perfectly normal while the action crawls —
the emulator is not slow, the game is waiting.

So the transport adds as little as it can: the network thread is woken the moment a byte is
handed to it rather than on a timer, and the relay server sends small packets immediately
instead of grouping them. Measured on a local connection, a relayed byte crosses in **0.1 ms**
where it used to take **31 ms**. What is left is your real ping. On a LAN it is
indistinguishable from a cable; across a continent it runs at the pace your ping allows.

That last part is not a bug we can fix in the transport — it is what a blocking byte pipe
costs — so there is a **second mode that changes the deal**. Measured on the same Fatal Fury
link match, counting the game's own logic updates per frame:

| your round trip | cable relayed | 🪞 mirror |
|---|---|---|
| 0 ms | 0.97 | 0.97 |
| 33 ms | 0.78 | **0.97** |
| 67 ms | 0.56 | **0.97** |
| 134 ms | 0.35 | **0.97** |
| 267 ms | 0.21 | **0.97** |

In mirror play the speed stops depending on your ping; what you feel instead is your
controls arriving a few frames late, and an input that has not arrived yet briefly pausing
both sides. Pick the cable mode when your connection is good or your saves differ, and
mirror play when the ping is what is hurting. Details in
[`specs/NETPLAY_MIRROR.md`](specs/NETPLAY_MIRROR.md).

**Some games ask for more than speed.** *SNK vs. Capcom — Card Fighters' Clash* checks
for the next byte of a packet the moment it wants it and gives up if it is not there
yet, so its VS match wants a round trip well under ~120 ms on the cable mode and simply
refuses a slow one ("LINK ERROR. CHECK CONNECTIONS AND SETTINGS."). Play it on a 🪞
mirror room and the question does not arise: that cable is local.

If a game refuses to see the other console, the debugger's [**Link** tab](#link--watch-the-cable-poke-it-break-it)
(`F1`) shows the cable byte by byte and names the end that is at fault — and can drive the
serial path with **one** console, so you can test without a partner.

**Writing a link game?** Two symptoms look like emulator faults and are not. *Both consoles
claiming to be player one* is your own role election, decided above the cable — the console
is a UART with no master, so nothing in the hardware breaks the tie for you. And *the ball
jumping backwards* when a game hands authority over an object between consoles is a protocol
without a step number: each side keeps applying the peer's last known state, which is its own
state from a few frames back. The link is asynchronous by design and the two consoles are
never on the same frame — see the *Link Cable* page of the NgpCraft dev reference for the
role rule and for input lockstep, which removes the handover problem rather than managing it.

## Timing — wait states, and the default that catches embedders

The cartridge flash is **slow**, and on this console that is not a detail: every instruction
is fetched across that bus, so the fetch cost dominates. A cartridge-resident loop runs about
**2.9–3.4× faster** with free fetches than it does on silicon. Games that lock to VBlank
(*Fatal Fury*) never show it; games that **self-time** (*Cool Boarders Pocket*, *Densha de Go*)
show it as double speed — 60 fps where a real console gives 30, and an in-game timer counting
twice too fast.

The values below are measured, not tuned by ear. The measuring ROMs and their raw silicon
numbers are in [`hw_calibration/`](hw_calibration/README.md).

| Knob | Ships as | Where it comes from |
|---|---|---|
| `fetch_wait_byte_q16` — cost of one **instruction-fetch byte**, in sixteenths of a cycle | **64** (= 4.00 cy/byte) | `cpu_calib_v14` page 1 measured it **directly**: 4.03 cy/byte on a line closing to 0.35 %. The cart bus is **8-bit**, so a fetch is priced per byte — charging per *word* made an instruction's price depend on its **parity** (5 bytes paid 3 charges from an even address, 2 from an odd one) and was what made this core address-sensitive where silicon is not (`cpu_calib_v12`: `682/682/683/682` on hardware) |
| `branch_taken_extra` — cycles added to a **taken** control transfer, **cart code only** | **4** | `cpu_calib_v14` page 0, rotation C (branch taken on an *empty* queue, so no flush component): silicon 16.3 cy/branch against 11.3 here. ⛔ **The cart-only condition is measured, not stylistic**: this models queue *refill on the 8-bit cart bus*. BIOS and RAM are not on that bus and their fetch is billed zero here, so charging them a refill they never pay over-billed every interrupt (which goes through the BIOS dispatch). Conditioning it cut the corpus from 0.67 % to 0.59 % |
| `data_access_cycles` — cycles for one **data** memory access, **cart code only** | **4** | `cpu_calib_v15` pages 1-2: ~4.05 cy **per access**, identical for reads and writes, and **independent of width** — a 1-byte read and a 2-byte read cost the same (215 vs 216 counts). An earlier per-**byte** form, fitted to the single width v14 had measured, was **refuted** by this ROM. ⛔ **The cart-only condition is measured** (`data_wait_cart_only`, 2026-08-29): the corpus's `MEM` loop *requires* these 4 cy (silicon 65.3 cy/iteration, 62 without / 66 with) while the interrupt path *refuses* them (silicon 111.5, 114 without / 130 with) — and **both write to RAM**, so what separates them is the region of the **code**, not of the data. Mechanism: this is bus **contention** — the data access steals a bus cycle from the prefetch — so it can only bite where the fetch is expensive, i.e. the 8-bit cart bus. Same rule and same reason as `branch_taken_extra` |
| `cart_data_wait` — *extra* cycles for a cart data byte, on top of the above | **0** | `cpu_calib_v2`: a random cart read and a RAM read cost the same (252 == 252). ⚠️ That proves they are **equal to each other**, never that they are free — and v15 has since priced both at 4 cy/access |
| `mul` / `div`, **byte** form | **12 states / 32 cycles** | `cpu_calib_v14` pages 3-4, lines closing to 0.5 %. Both land just above the datasheet floor (11 and 15 states) — consistent with variable latency |
| `mul` / `div`, **word** form | 19 states / 56 cycles | ⚠️ **No single constant can be right.** Three silicon-backed authorities give three numbers: Appendix B table (4) **14 / 23 states**, `cpu_calib_v17` (marginal slope) **17 / 52**, the corpus (loop level) **19 / 56**. That is not a contradiction — the 900/L1 divide has **variable latency**, the table is a *floor*, and the two ROMs divide **different operands**. The corpus value ships; re-deriving from v17 and arming it puts `MUL` at **+6.1 %** |
| interrupt entry (`kIrqDeliveryCycles`) | 18 states (36 cy) | `cpu_calib_v8`, and **cleared** by v18: a `nop` inside an ISR costs 4.03 cy (same as outside), so the handler's code is billed correctly and the entry was never the suspect. Appendix B table (11) gives **18 states**, `JP (FFFF00H + vector)` included — a single value, not the 28/24/22/18 set quoted elsewhere |
| `irq_transparent_queue` — an interrupt is **transparent** to the interrupted stream's bus state (queue/debt saved on delivery, restored on `reti`) | **on** | `cpu_calib_v19` (silicon, 2026-08-29). Without it the ISR's cycles **refill the interrupted code's queue**, so an interrupt made the interrupted code *cheaper* (−0.574 cy/instruction) — impossible, since during the ISR the bus is fetching the ISR's own bytes. ⚡ v19 measures the per-interrupt cost against how bus-bound the interrupted loop is: **112.6 / 112.0 / 110.7 / 110.5**, contrast **+1.5**, where both overlap models predicted **+18.0** and **+11.6**. Armed, our contrast is **−1.2**. ⛔ And `branch_taken_extra` is **not** charged on a transparent `reti`: it models queue *refill*, and a restored queue is not refilled |
| `ldir_cost` / `ldirw_cost` — per iteration of the byte / word block copy | **14 / 14** | `cpu_calib_v20` and `v21` on silicon: a `ldirb` **and** a `ldirw` both cost **14,0x cy per iteration** RAM→RAM — exactly Appendix B (3), `7n + 1` **states**. ⚠️ Our long-standing **18** was not wrong, it was **mis-attributed**: fitted against Bomberman's HiColor copier, which copies **ROM → VRAM**, it carried 14 (the instruction) **+ 4** (the price of reading its source over the cart's **8-bit** bus). Applying it to every transfer made us **29 % too expensive** on any RAM→RAM or RAM→VRAM copy — most of what games do |
| `block_cart_src_per_byte` — extra cycles per byte a block transfer reads **from the cart** | **2** | `cpu_calib_v21`, four paths of the same `ldirw`: RAM→RAM **14,04**, RAM→VRAM **14,12** (the destination costs **nothing**, +0,08), ROM→RAM **18,16**, ROM→VRAM **18,16** — the **source** carries all of it, +4,12 per word iteration, and the two effects are **additive** (+4,20 predicted). ⛔ The **byte** form's 2 cy is *derived* from the word measurement (4 ÷ 2), not measured directly |
| `vram_wait` — cycles per **data access** to display RAM **during active display**, **block copies excluded** | **10** | `cpu_calib_v3` on silicon: **VWR 452** against **MEM 471**. Shipped at 0 until 2026-08-30, where VWR read **503** (+11.3 %) — and worse, a VRAM write cost *less* than a RAM write, since VRAM is excluded from `charge_data_access`. ⚡ **Per ACCESS, not per byte** — `cpu_calib_v20` page 3, silicon: a word write and a byte write to VRAM both cost **2,95 cy** more than the same write to RAM, ratio **1,00**. v3 could not tell (it only writes bytes, where the two forms coincide). The two shots agree on the value (2,74 and 2,95 cy/write) ⇒ **10**, which balances both (v3 −0,9 %, v20 +1,3 %). ⛔ **Not charged during a block transfer** (`in_block_copy`), and `cpu_calib_v21` says why: on a block copy the **destination costs nothing at all** — RAM→RAM 14,04 against RAM→VRAM 14,12, a difference of +0,08. The K2GE throttle is real on an isolated write and simply does not bite there. (Billing it anyway made Bomberman's HiColor copier drift off its 4120-cycle slice and corrupt one line per band; `test_bomberman_hicolor_phase` catches it.) |

**Where this lands.** Against 26 silicon measurements from five calibration ROMs, the model
is at **0.18 % mean error, 0.77 % worst case** — every one of the 26 cells now under 1 %.
Before this campaign: 4.78 % and 12.31 %. Re-run it with `python hw_calibration/corpus_gate.py`.

⚡ **And the interrupt path now lands on the official tables to the cycle.** Appendix B for
the full TI0 path — 18 states (entry) + 8 + 8 (`PUSH<W> (mem)` 6 + `(#16)` 2) + 9 (`ret`) +
12 (`reti`) = **110 cy**; silicon measures **111.5**; this core spends **110.0** (direct
sum over hundreds of interrupts, σ = 0). It used to spend 130, and *looked* right at 115 in
throughput only because **+20 cy of double-billing and −17 cy of an impossible rebate
cancelled out**. ⚖️ Two errors of opposite sign is why no single-knob sweep ever converged
across two campaigns — each knob touched only one of them.

⛔ **Two of these do not move alone.** Charging the fetch per byte *without* charging the
taken branch makes the model **worse** (corpus 4.78 % → 7.13 %): the old 8.25 cy/word was
over-charging the bus to make up for a branch that cost nothing.

⛔⛔ **And a measured per-instruction cost already contains its own memory traffic.**
`data_access_cycles` is charged in `load_sized`/`store`, so three paths must be excluded by
hand or they are billed twice: **block transfers** (`ldir_cost`/`ldirw_cost` were measured
against Bomberman's 4120-cycle slice — double-charging cost +43 % per block and shredded
Cool Boarders' HUD), **interrupt entry** (Toshiba's 28/24/22/18 states are keyed on the
*stack area's* bus width, so they already include pushing PC and SR), and the interrupt's
**own vector read** — that last one was a real bug, billed to the first instruction of every
handler until it was found by instrumentation. Only instructions whose price comes from a
**table** may take a per-access cost on top.

### ⚠️ The application and the library do not start the same way

- **The application** turns wait states **on** by default, on every ROM it loads
  (`cart_wait_states()` in `ngpc_settings.py`). Playing a game here is silicon-timed. The
  toggle exists so you can compare against the old free-fetch timing on purpose.
- **A bare `Machine`** — anything that imports `core.native` and builds one itself: a bench
  harness, a test, the NgpCraft MCP server — starts at
  `cart_wait = 0`, **free fetch**. That zero is historical: when the feature landed, the field
  was left off so existing timing stayed bit-for-bit identical and the hot path cost nothing
  when disabled. It is *not* a claim about hardware.

So anything measuring performance must ask for hardware timing explicitly:

```python
m.set_cart_wait(cfg.CART_FETCH_WAIT)      # 3
m.set_cart_data_wait(cfg.CART_DATA_WAIT)  # 0
m.set_ldir_cost(cfg.CART_LDIR_COST)       # 14  -- byte block copies
m.set_ldirw_cost(cfg.CART_LDIRW_COST)     # 18  -- word block copies
```

(`core/romcheck.py` does exactly this; copy it rather than re-deriving the numbers.)

### Why this matters more than "everything is ~3× slower"

A uniform slowdown would be easy to live with. This one is **not uniform**, and it changes
which optimisations are real:

- **Instruction count is a cost, not just cycle count.** Every byte of encoding is a byte
  fetched across the slow bus — three extra ticks. Shorter code is *directly* faster.
- **With fetch unbilled, every size-based saving measures as exactly zero.** If a change makes
  the code smaller and the profile does not move at all, suspect the timing model before you
  conclude the optimisation was worthless.
- **Padding a struct to a power of two can backfire.** Grow it and field offsets fall out of
  the 8-bit displacement form; the longer encoding then costs three ticks per extra byte,
  every access.

The [Profiler](#profiler--where-the-frame-goes) reports **cycles**, which is why: on this
machine instruction cost varies by a factor of ten, so ranking by instruction count puts a
tight `djnz` loop above the routine actually eating the frame.

## Debugging (F1)

Built for people writing NGPC games by hand. Everything below is saved **per ROM**, so
your map of a game survives across sessions. The contracts behind these panels — and the
claims they are deliberately *not* allowed to make — are in
[`specs/DEBUG_TOOLS.md`](specs/DEBUG_TOOLS.md).

The 27 panels sit on **two rows** — a category on top (CPU · Memory · Video · Audio ·
Analysis · ROM · Link), its panels underneath. One row stopped working somewhere around
twenty: Qt then either shrinks every label past reading or hides half of them behind scroll
arrows, and a tool whose tabs you cannot see is a tool you stop opening. The grouping is by
*what you are looking at* — chasing a graphics fault puts the palette, the tiles, the map and
the raster timeline side by side — and nothing outside the tab bar knows about it, so
`Ctrl+G` still jumps straight to the disassembly from anywhere and a double-clicked coverage
gap still opens it.

**A debug panel can never kill the emulator.** Refreshing runs from a timer and from tab
changes — both Qt slots — and an exception reaching PyQt calls `qFatal()`: the process dies
with no message, no traceback, and it looks like the *core* crashed. So a panel that throws is
caught, named in the status line (`⚠ Movie: AttributeError: …`), and the rest of the window
keeps working. It is reported rather than swallowed: the failure is also recorded, so a test
asserting "every panel refreshed" cannot pass on a window full of broken ones.

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

### Console — a Python prompt with the machine in scope

Look at what this project does when it needs an answer the debugger does not have:
`analyze_glitch_state.py`, `detect_hdiscontinuity.py`, `ripoam.py`, `triage_vs_oracle.py` —
one-off scripts written outside the tool, run against a dump, then kept forever or lost.
Several of them answered their question in four lines.

This is those four lines, with the machine already loaded. Nothing it exposes is a new
capability — but a debugger you can only use through the buttons someone thought of in
advance stops exactly where your question starts.

```python
>>> [hex(a) for a in find(b"\x21\x00\x70")]      # where is this byte pattern?
>>> hexdump(0x6F80, 32)                          # the BIOS's work RAM
>>> hwregs.format_report(read)                   # every register, decoded
>>> seen = []
>>> cancel = on_frame(lambda: seen.append(u16(0x8032)))   # sample EVERY frame
>>> max(seen) - min(seen)                        # how far did scroll move?
```

`m`, `play`, `read/write/poke`, `u8/u16/u32`, `find`, `hexdump`, `dis`, `z80dis`, `regs`,
`step`, `screen`, `on_frame` — plus `hwregs`, `tilemap_view`, `coverage_map`, `profile`,
`movie`, `z80dasm` and `numpy` already imported. `help()` lists all of it, and a test checks
that everything `help()` names actually exists. **Run a file…** executes a `.py` in the same
session, so the next one-off script never has to leave the debugger.

`on_frame` is the one that replaces those scripts: it runs per **emulated frame**, which is
the granularity they always needed — sampling at a window's refresh rate takes eight readings
out of six hundred thousand instructions and calls it a measurement.

Two safety properties, both tested. Every exception is **captured, never propagated**: this
code runs inside a Qt slot, and an exception reaching PyQt calls `qFatal()` — the process dies
with no message at all, so a REPL that let one through would be a crash generator with a
prompt on it. And the refresh timer **never executes anything**: only Enter runs code. `exit()`
means "close the console", not "tear down a running game".

### Movie — record what you press, replay it exactly

Every playtest report on this project has been a **sentence**: *"the text windows get stuck
halfway", "the HUD flickers"*. Sentences cannot be re-run — the person who saw it has to see
it again, on demand, while someone else watches.

The console takes **one byte of input per frame**, so a session is a snapshot plus a list of
bytes: sixty bytes a second, a few kilobytes a minute. Record, play, save a `.ngpcmov`, and
the bug becomes a file anyone can re-run. The note field travels with it (*"the dialogue box
stops halfway at 0:12"*), along with the cartridge's identity and when it was taken.

The whole feature hangs off **one call site** — the byte written to `0x00B0` once per emulated
frame. Nothing else in the frame loop knows it exists, so there is no second clock for a
replay to drift against. The starting snapshot is taken **when you press Record**, not at the
first frame: a recording that begins one frame late replays one frame out of step forever, and
a one-frame drift reads as an emulation bug rather than as a broken tool.

Three refusals, on purpose:

- a movie recorded against **a different cartridge is refused outright**, not warned about —
  it would produce garbage that looks exactly like an emulation bug, and this feature exists
  to make bugs *believable*. A file merely renamed is only a note.
- a starting state whose size does not match this build is refused: it would be applied
  field-by-field onto a struct of another shape.
- recording and replaying cannot both be on, and **neither runs during a mirror match** — the
  other PC is simulating both consoles from the shared input stream, and feeding this one a
  recorded byte desynchronises the match.

When a replay reaches its end the controller comes straight back to you; it never holds the
last frame's buttons, which would walk the game into a wall and call it a reproduction.

> ⚠️ A movie does **not** carry the cartridge's flash save — a savestate deliberately excludes
> it, because it is a save and not a snapshot. A game that reads its save file mid-session can
> diverge from the recording. That is said out loud rather than papered over: a replay that
> silently drifts is worse than no replay at all.

### Profiler — where the frame goes

**Not a sampler.** The core can retire instructions while recording every one of them —
PC, memory accesses, and what each one *cost* — so this is an exact accounting of a captured
window rather than a statistical guess about it. A sampler running at a debug window's
refresh rate would take eight samples a second out of six hundred thousand instructions and
call the result a profile.

The unit is **cycles, not instruction counts**. On this machine the cartridge bus is slow and
instruction cost varies by a factor of ten, so ranking by instructions puts a tight `djnz`
loop above the routine that is actually eating the frame. Each row gives cycles, share,
**cycles per frame** (the absolute figure — a bucket's *share* of one frame is arithmetically
the same number as its share of the capture, so printing both would be printing one number
twice), instruction count, cycles per instruction, and the read/write counts the slow bus
charges for. Double-click a row to open it in the disassembly.

Rows are named from the toolchain's `.map` when one is loaded beside the ROM, and by address
block when there is none — refusing to answer without symbols would make the tool useless on
every commercial cartridge, which is most of the corpus. Above the table, **where the cycles
went by region**: *"cartridge 61 %, BIOS 39 %"* is something no function list can tell you,
and on this console it is often the answer.

Capturing **advances the machine** — like *Trace to file*, it is a deliberate one-shot on a
button and never something a refresh does behind your back. The pause state you had is the
one you get back.

### Coverage — what the cartridge actually executed

The core has recorded this all along — one bit per byte of the cart window, set when an
instruction *starts* there — and nothing in the emulator ever looked at it. It settles two
questions nothing else can:

- **a routine that never runs.** A breakpoint that does not fire proves nothing on its own:
  it looks exactly like a breakpoint on the wrong address. A cold region says the CPU has
  not been there.
- **whether an input reached new code.** Reset the count, press the button, watch it move.
  "Driving the buttons exercised more of the ROM" becomes a number instead of a hope.

The cartridge is drawn as a map — green ran, grey never did, black is past the end of this
file — with hover giving the address range under the pixel, how many instruction starts fired
in it, and the symbol name if a `.map` is loaded. Beside it, the **never-executed runs**,
largest first; double-click one to open it in the disassembly.

Two honesties are built in. A bit means *an instruction started here*, so the bytes inside a
multi-byte instruction are cold — short gaps are the encoding, not dead code, and are not
reported at all. And the window is `0x200000..0x3FFFFF`: a **4 MiB cartridge has a second
chip at `0x800000` that coverage does not watch**, so the percentage is taken against what is
actually recorded and the tab says why, instead of showing a game that runs perfectly as 40 %
dead. Recording is off until you arm it, and an unarmed bitmap reads as *not recording* — never
as 0 % executed, which would be a measurement the tool never took.

### Events — the raster timeline

Every video-register write and every interrupt, plotted at the **scanline and cycle** it
happened on. This is the view for raster work: a mid-frame scroll split, an HBlank HUD or
a palette swap on a given line is correct or broken purely as a function of timing, and no
write log can show that.

### HW Regs — every register, decoded and checked

The memory viewer can tell you that `0x8118` holds `0x87`. It cannot tell you that this
means *"backdrop enabled, palette 7"* — nor that `0x07`, one bit away, means *"backdrop
off, and the picture goes black"*. Every register on this machine packs unrelated switches
into one byte, so reading them as hex is reading them in a language nobody thinks in.

This tab is the dictionary: every hardware register laid out field by field, with each
field's meaning spelled out (`T0CLK = 0 → external TI0`; `INT4 = 0 → **DISABLED**`;
`DMA0V = 0x10 → INTT0, so that interrupt is serviced by DMA and never reaches the CPU`).
Alongside it:

- **live change tint** — a value cell lights up the refresh after the game writes it and
  fades over about a second, so you can watch *which* registers a scene actually touches;
- **Hide untouched** — drop every register still at its documented reset value; what is
  left is what this game programmed on purpose;
- **filter** by name, by **address** (`8118` — how you arrive here from the memory viewer,
  holding a number and no name), or by what it does (`watchdog`, `timer`);
- **checks** — states the manufacturer documents call illegal, each quoting the sentence
  that makes it a defect rather than an opinion: a window whose origin plus size passes the
  hardware limit (*"display and Vint/Hint generations are disrupted"*), a stopped prescaler
  (*"prohibited"*), Character Over, or a VBlank left at **level 0** — which is not "lowest
  priority", it is off, and a game waiting on it hangs in a way that looks like a CPU fault.

Provenance is part of the table, not a footnote. A register with no manufacturer document
behind it is labelled **REVERSE** (`0x8000`, `0x8400`, the sound-CPU control bytes) — because
presenting a guess next to a sourced fact in the same typography is how a guess becomes a
fact. The decoding itself lives in `core/hwregs.py` — pure Python, no Qt — so a script or a
test can ask the same questions the window does, and `format_report()` dumps the whole map
as text.

### Tilemap — the plane, and where the screen is on it

The **Tiles** tab shows character RAM: 512 tiles in storage order, which is a picture of
nothing. This tab shows the other half — the **32×32 map each scroll plane actually
draws**, as one 256×256 image, in the game's own colours (K2GE palettes, K1GE compat LUT,
or the mono grey ramp on an NGP, whichever the machine is really using).

Over it: **the part the screen is reading**, left bright while everything else is dimmed —
never tinted, because a tint would change the colours you came here to judge. And it is
drawn **one scanline at a time**, which is the point. The plane is cyclical 256×256 and the
scroll registers are re-read *by every line*, so the camera is not a rectangle: games drive
parallax by rewriting scroll from an H-blank handler. A straight edge means one scroll value
for the frame; a **wavy edge means line-scroll**; a torn one means the game meant to and got
the timing wrong. The note line names it outright — `line-scroll 11 px` or `no line-scroll
this frame` — measured **around the circle**, so a scroll wobbling between 254 and 2 reads as
four pixels rather than 252.

If the core cannot supply the per-line registers, the tab falls back to the single
end-of-frame scroll **and says so** — that fallback is the exact mistake this view exists to
expose, so it is never silent.

Hover any cell for its map entry: the **entry's own VRAM address**, the 9-bit character
number, where those 16 bytes live in character RAM, the palette code and the flips — click to
copy. Plus an 8-pixel grid, a **mark-transparent** toggle (pixel value 0 is transparent *per
pixel*; there is no "tile 0 is blank" rule on this hardware), a distinct-tile count and PNG
export. Decoding lives in `core/tilemap_view.py` — pure numpy, no Qt — deliberately in step
with `cpp/src/render.cpp`; if the two ever disagree, this viewer is the one that is wrong.

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
- **Cheats** — named groups of addresses held at a value. The Watch tab already locks *one*
  address (that is how "what if HP never drops" gets answered); a cheat is usually two or
  three that only mean something together — health *and* the death flag, lives *and* the
  continue counter — and typing those back one at a time every session is how a map gets
  lost. Saved per ROM, in the **same plain format you paste into a message**, so what you
  edit and what you share are the same text and there is no second representation to keep in
  step:

  ```
  # Infinite health
  4812:1 = 63
  481A:2 = 03E7
  ```

  Enabled cheats are written **at the same point in the frame as a locked watch** — not a
  second freezing mechanism, because two things that both "hold a value" would eventually
  disagree about which one won, and the answer would depend on where in the frame each ran.
  A line that will not parse is reported **with its number**, never skipped: a pasted code
  that quietly loads three of its four addresses is a cheat that half-works, which is the
  state that wastes the most time. And an address in the cartridge window is flagged —
  the cart is **flash**, so a write there does not change memory, it goes to the chip's
  command latch. Flagged, never refused: a debugger that rejects an address for looking
  wrong is useless the day the address is right.
- **Trace to file** — every instruction with, optionally, the registers it wrote and every
  memory address it read or wrote.
- **Audio** — live per-channel monitor (3 square + noise): period → frequency → **note**,
  L/R volume, plus an oscilloscope of the output and the sound Z80's state. **Mute / solo**
  any channel to isolate it, watch the raw chip-write log, and **record the music** — save it
  as a **`.vgm`** (Furnace / VGM players) or as a **`.ngps` song** for the NGPC sound creator.
- **Sound CPU** — the Z80 as a first-class processor rather than one line of status text.
  A **full Z80 disassembly** that follows its PC (the whole instruction set: CB, ED, DD/FD
  and the `DD CB d op` form, including the undocumented `sll` and the indexed write-back the
  drivers actually use), **both register banks** — `exx` swaps them in one instruction, so
  showing only one is showing half the CPU — the interrupt state, the signed cycle credit,
  and the stack. It runs in its **own address space**, which the tab makes explicit: `0x0000`
  here is the shared RAM the main CPU sees at `0x7000`, `0x4000` is the write-only sound
  chip, `0x8000` the mailbox, `0xC000` the register that raises INT5 on the main CPU.

  Above all it separates **halted** from **trapped**. A halt is the driver sleeping between
  timer ticks — normal, and where it spends most of its life. A trap is *our core refusing
  an opcode*, and the tab names the instruction: `TRAPPED at 0x0100 on opcode ED B0 — that
  instruction is ldir, our core does not implement it. This is a hole in the emulator, not
  in the game.` Only that one gets the alarm colour, or the alarm stops meaning anything.

  Read-only on purpose: the core has no Z80 breakpoints yet, and a Pause button here would
  stop the *main* CPU while claiming to stop this one.
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

### Link — watch the cable, poke it, break it

Everything about a link session that is normally invisible. The tab reads the serial
channel live and, crucially, **says which end is at fault** in plain language instead of
leaving you to deduce it from a byte count:

- **The reading** — cable detect, the RTS/CTS handshake (including how long a byte has
  been *held*, and by which side), the bytes counted at every stage — written to SC0BUF →
  shifted out → queued for us → actually read by the CPU — the two serial interrupts, the
  SC0 registers, and the BIOS's own COM rings.
- **The verdict** — one sentence naming the *earliest* stage that is stuck: no cable ·
  total silence (both consoles have to be on the game's VS screen at the same time) ·
  transmitter held because the peer is not ready · bytes waiting because *this* console
  never lowered RTS · they arrive but the receive interrupt never fires · it fires but
  nothing reads the byte (and, at interrupt mask level 6, *why*).
- **The log** — every byte that crossed, both directions, in hex and ASCII, with the frame
  it crossed on. Exportable as text, or as raw `.bin` per direction.
- **Inject** — hand bytes to the console as if a peer had sent them. They travel the real
  receive path, so a game that reacts proves the whole chain works — **with no second
  console involved**.
- **Loopback** — plug the console into itself (*echo*), or into a wire that never answers
  (*sink*), to exercise or starve the serial path with one machine.
- **Impair** — add latency, throw away a share of the bytes, or cut the wire mid-session.
  The emulated cable is instant and lossless; a real connection is neither, and this is
  how you find out whether a game copes.

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
(`.`) through what just happened.

There are **two ways to use it**, and they are for different things.

**Hold the key** for a quick correction — and it **speeds up the longer you hold**. It used to
run backwards in real time, so reaching the far end of a 30-second buffer meant holding a key
for thirty seconds, which is not rewinding, it is waiting. The first half-second stays at 1×
so a tap still lands on the frame you meant; after that it doubles every half-second up to 8×.
Measured: **a 10 s buffer is crossed in 2.3 s, 20 s in 3.6 s, 30 s in 4.8 s** — and a quarter-
second tap still moves exactly 15 frames. The strip shows `×4` while it ramps, because a
picture that suddenly runs eight times faster with nothing on screen to say so reads as a
glitch.

**Or drag the strip** to go straight to a moment instead of feeling your way there. It floats
**over** the bottom of the picture — never in the layout, because there it took space and the
image shrank the moment you touched rewind and grew back when you let go — and it shows where
you are, how much history is left, and a tick per second so you can *see* it rather than read
a number.

Crucially it **stays up for a few seconds after you let go of the key**, and while the game is
paused. The first version vanished the instant the key came up, so the only moment it existed
was the moment your hand was busy holding a key: a scrubber you can never grab is a
decoration. Grabbing it stops the countdown.

The mouse then mirrors the key exactly: **hold to move through time, let go to carry on.**
Dragging *has* to pause — otherwise the game runs out from under the position you are
choosing — but it does not leave you there, because there is no play button on this toolbar to
get out with: `⏭` steps one frame (and pauses again) and `⏩` does nothing at all while paused,
since the loop returns before it. A game that was already paused before the drag stays paused,
because then pausing is what you asked for. The strip puts itself away when you are done: a
permanent bar under the game is a permanent tax on the game.

(It replaced a `⏪ 137` printed over the middle of the screen: a count of frames is a unit
nobody thinks in, it sat on the thing you were trying to look at, and it never answered the
question you actually have while the key is down — how close you are to the end of the
buffer.)
 Buffer length is set in **Settings ▸ General ▸ Rewind
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

- **Controller support is not yet validated on real hardware.** The cross-platform SDL2
  pad backend (Windows/macOS/Linux, most brands) is wired up and degrades safely when no
  pad is present, but it has **not been tested with a physical controller** — the default
  button mapping (A/X→A, B/Y→B, Start/Back→Option, d-pad + left stick → directions) may
  need adjusting per model. Treat it as **beta / awaiting validation**; reports of what
  your controller actually does are welcome.
- **The monochrome NGP is a restricted K2GE, not a K1GE.** The console-type byte, the absent
  colour palette and the eight-grey resolution are modelled, and games that check the machine
  behave as they do on hardware — but a game that probes the video chip itself still finds a
  K2GE, and the **NEG** (inverted display) bit is not applied on that path. Checked on a
  handful of titles, not the whole corpus.
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
- **Simplified and Traditional Chinese translations** - [Blueteemo](https://github.com/Blueteemo)

Adding yours is adding one JSON file, no Python required, and an unfinished one is
mergeable — see [TRANSLATING.md](TRANSLATING.md). You get credited here, in the file
itself, and as a co-author of the commit.

## Legal

This is a clean-room emulator. It ships **no copyrighted ROMs or BIOS**. Neo Geo Pocket
is a trademark of SNK; this project is not affiliated with SNK.

## License

MIT — see [LICENSE](LICENSE).
