
# NgpCraft Emulator

A **Neo Geo Pocket / Neo Geo Pocket Color** emulator: a fast native C++ core
(TLCS-900H + Z80 + K2GE video) driven by a PyQt6 desktop shell.

Its timing is **calibrated against real hardware** — instruction-fetch wait-states on
the cartridge flash, silicon-measured MUL/DIV and LDIR costs — so self-timed games
(Cool Boarders Pocket, Densha de Go) run at their true 30 fps instead of the ~2× too
fast that most emulators show.

<img width="1076" height="761" alt="emulateur01" src="https://github.com/user-attachments/assets/24dd060d-5b35-4ef6-83dd-916a618ba244" />

## Features

- **Library** with cover thumbnails (grid / list / compact), live-reflowing.
- **Console boot** — with a real BIOS, *Boot BIOS* powers the console on for real: the
  Neo Geo Pocket intro plays and the game then boots on its own, exactly like hardware.
- **Video**: integer / fit / stretch scaling, scanline / LCD-grid / CRT filters,
  colour profiles, real fullscreen. The canvas follows the window; size presets `Ctrl+1…5`.
- **Save states** — 8 slots per game (toolbar or `F2` save / `F4` load / `F3` slot).
- **In-game saves** — the game's own flash save, stored in the ROM, a separate file, or both.
- **Speed control** — fast-forward (hold `Tab`) and 0.25×…4× (`[` / `]` or the toolbar).
- **Rewind** — hold `,` (or the ⏪ toolbar button) to run the game backward; release to
  resume. `.` steps one frame forward. Buffer length configurable (Off / 10 / 20 / 30 s).
- **Screenshots** (`F12`, folder configurable), **FPS overlay**, a hideable **player toolbar**.
- **Debug tools** (`F1`) — CPU, disassembly, memory (with poke), palette, tiles, sprites;
  **named watchpoints** (break-on-value / break-on-write / freeze), **execution breakpoints**
  (with conditions), **RAM search**, and an **audio** panel (per-channel note/volume,
  oscilloscope, mute/solo, **VGM export**); everything exportable and saved per ROM.
- **Crash reports** — a ROM fault writes a detailed `crashes/*.txt` (reason, PC, opcode,
  registers, memory & stack dumps).

<img width="1073" height="768" alt="emulateur02" src="https://github.com/user-attachments/assets/051d5d43-dc43-4001-8964-7e8b757b057d" />


## Run

Requires **Python 3.10+**.

```bash
pip install -r requirements.txt
python ngpc_shell.py
```

On Windows a prebuilt core (`cpp/build/ngpc_core.dll`) is included, so it runs as-is.

### Standalone Windows .exe (no Python needed)

Grab `NgpCraftEmulator.exe` from the [Releases](../../releases) page and double-click
it — nothing to install. ROMs, saves and screenshots live in folders next to the `.exe`.

To build it yourself:

```bat
pip install pyinstaller
build_exe.bat
```

This produces a single self-contained file, `release\NgpCraftEmulator.exe`.

### Building the core (other platforms / from source)

```bash
cd cpp
cmake -B build -DCMAKE_BUILD_TYPE=Release
cmake --build build --config Release
```

This produces `cpp/build/ngpc_core.{dll,so,dylib}`, which the shell loads automatically.

## Games & BIOS

No ROMs or BIOS are included — provide your own.

- Put `.ngc` / `.ngp` files in **`roms/`** (or pick any folder via **Settings ▸ Library**).
- A real Neo Geo Pocket **BIOS** — place it as **`bios.bin`** next to the app (or set the
  path in **Settings ▸ BIOS**). **Most homebrew need it**: they call BIOS routines through
  the console's vector table, so without a BIOS they crash on boot (the emulator will tell
  you when that happens). Commercial games and BIOS-free homebrew run without one.

> **A few commercial games need `bios.bin` too.** *Metal Slug — 2nd Mission* checks that the
> console really booted through its BIOS, and quietly disables **fire and jump** when it
> decides it did not — the game still runs and looks perfect, you simply can never shoot or
> jump. Both start modes below satisfy the check; no BIOS at all does not.

### Two ways to start a game

- **Instant hand-off** *(default — leave "Console boot" OFF)*: the cartridge is handed the
  exact state the BIOS boot would have left, so the game starts immediately. The BIOS image
  is still used behind the scenes (saves, system calls); the game just boots straight in.
- **Console boot** *(Settings ▸ General ▸ "Play the console boot")*: the real BIOS powers on,
  plays its **NEO·GEO POCKET intro**, and then boots the game on its own — exactly like
  turning on the hardware. A brand-new console configures itself the first time (the BIOS
  first-boot setup is auto-completed with defaults and remembered), so you always get
  *intro → game*, every launch, with no setup screen to click through. Needs a real
  `bios.bin`.

The **Boot BIOS** button (Library) boots the BIOS by itself, with no cartridge — the
console's own language/clock screens, one of the NGPC's signature features.

## Saves

Two different things, kept separate:

- **Save states** — an instant snapshot of the whole machine, 8 slots per game. Toolbar
  buttons, or `F2` save / `F4` load / `F3` change slot. Stored in `savestates/`.
- **In-game saves** — the game's OWN save (RPG progress, high scores, options), written by
  the cartridge's flash. Choose how it is stored in **Settings ▸ General ▸ "In-game save"**:
  - **In the ROM (.ngc)** — written back into the cartridge file itself, exactly like the
    flash chip on a real cartridge. The save travels with the ROM. *(default)*
  - **Separate file** — a `saves/<rom>.flash` beside it; the ROM is never modified
    (standard NeoPop / Mednafen / RACE format).
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

## Controls (default)

| Key | Action |
|-----|--------|
| Arrows / Z / X / Enter / Backspace | D-pad / A / B / Option / — (remap in Settings) |
| `Esc` | Pause menu · `P` pause · `F5` reset |
| `F2` / `F4` / `F3` | save / load state · change slot |
| `Tab` (hold) · `[` / `]` | fast-forward · slower / faster |
| `F12` · `F11` · `Ctrl+1…5` | screenshot · fullscreen · window size 1×…5× |
| hold `,` · `.` | rewind while held (release to resume) · step one frame forward |
| `F1` · `H` | debug tools · toggle player toolbar |

## Debugging (F1)

Built for people writing NGPC games by hand. Everything below is saved **per ROM**, so
your map of a game survives across sessions.

- **Watch** — give memory addresses logical names and see their live value (1/2/4 bytes,
  hex or signed/unsigned). Each row can also:
  - **break on value** — pause when it hits a condition (`=`, `≠`, `<`, `>`, `change`);
  - **break on write** — pause when *any* code writes it, naming the **PC that did it**;
  - **lock** — freeze the address to a value each frame (test "what if HP never drops").
- **Breakpoints** — pause when PC reaches an address, with an optional guard condition
  (`4812 = 0`, `4a00.2 > 0x100`): it only fires when the condition holds.
- **RAM Search** — find *where* a value lives: start a search, let the game change it, then
  filter (`=`, `≠`, `>`, `<`, `changed`, `=prev`, `▲`, `▼`) until one address remains.
  Double-click a hit to name and watch it.
- **Audio** — live per-channel monitor (3 square + noise): period → frequency → **note**,
  L/R volume, plus an oscilloscope of the output and the sound Z80's state. **Mute / solo**
  any channel to isolate it, watch the raw chip-write log, and **record the music** — save it
  as a **`.vgm`** (Furnace / VGM players) or as a **`.ngps` song** for the NGPC sound creator.
- Plus the live viewers: CPU, disassembly + trace-to-file, memory (with poke), palette,
  tiles, sprites — each with an Export button.

### Rewind — how it works and its limits

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

## Legal

This is a clean-room emulator. It ships **no copyrighted ROMs or BIOS**. Neo Geo Pocket
is a trademark of SNK; this project is not affiliated with SNK.

## License

MIT — see [LICENSE](LICENSE).
