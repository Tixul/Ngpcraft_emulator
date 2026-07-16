# NgpCraft Emulator

A **Neo Geo Pocket / Neo Geo Pocket Color** emulator: a fast native C++ core
(TLCS-900H + Z80 + K2GE video) driven by a PyQt6 desktop shell.

Its timing is **calibrated against real hardware** — instruction-fetch wait-states on
the cartridge flash, silicon-measured MUL/DIV and LDIR costs — so self-timed games
(Cool Boarders Pocket, Densha de Go) run at their true 30 fps instead of the ~2× too
fast that most emulators show.

## Features

- **Library** with cover thumbnails (grid / list / compact), live-reflowing.
- **Video**: integer / fit / stretch scaling, scanline / LCD-grid / CRT filters,
  colour profiles, real fullscreen. The canvas follows the window; size presets `Ctrl+1…5`.
- **Save states** — 8 slots per game (toolbar or `F2` save / `F4` load / `F3` slot).
- **In-game saves** — the game's own flash save, stored in the ROM, a separate file, or both.
- **Speed control** — fast-forward (hold `Tab`) and 0.25×…4× (`[` / `]` or the toolbar).
- **Screenshots** (`F12`, folder configurable), **FPS overlay**, a hideable **player toolbar**.
- **Debug tools** — CPU, disassembly, memory (with poke), palette, tiles, sprites; export.
- **Crash reports** — a ROM fault writes a detailed `crashes/*.txt` (reason, PC, opcode,
  registers, memory & stack dumps).

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
- A real NGPC **BIOS** is optional: place it as **`bios.bin`** next to the app (or set the
  path in Settings) to use *Boot BIOS*. Games launch fine without it.

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
  capacity to the game. Some homebrew save to a high block that only exists on a 2 MB
  (16 Mbit) cart, so **Auto** gives small ROMs a 16 Mbit chip; the `.ngc` grows to that size
  on first save. Set it explicitly (4 / 8 / 16 Mbit) if a game needs a specific capacity.

## Controls (default)

| Key | Action |
|-----|--------|
| Arrows / Z / X / Enter / Backspace | D-pad / A / B / Option / — (remap in Settings) |
| `Esc` | Pause menu · `P` pause · `F5` reset |
| `F2` / `F4` / `F3` | save / load state · change slot |
| `Tab` (hold) · `[` / `]` | fast-forward · slower / faster |
| `F12` · `F11` · `Ctrl+1…5` | screenshot · fullscreen · window size 1×…5× |
| `F1` · `H` | debug tools · toggle player toolbar |

## Legal

This is a clean-room emulator. It ships **no copyrighted ROMs or BIOS**. Neo Geo Pocket
is a trademark of SNK; this project is not affiliated with SNK.

## License

MIT — see [LICENSE](LICENSE).
