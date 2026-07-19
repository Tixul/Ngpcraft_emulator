# NgpCraft Emulator - Doc Sources Index

Purpose:
- keep a single index of the useful docs already present in the workspace
- avoid re-searching the same references every time
- separate "must read first" from "use when needed"

Scope:
- `../NgpCraft_Disasm`
- `../NgpCraft_engine`
- `../NgpCraft_gb2ngp`
- `../NgpCraft_toolchain`
- `../Doc de dev/Final/Doc final uniformise eng`
- `../../01_SDK`

This file is intentionally practical, not exhaustive.
Only the sources that are likely to help the emulator project are listed.

## 0. ⭐ MANUFACTURER DOCUMENTS — read these BEFORE inferring anything

Acquired 2026-07-10. Between them they cover the CPU, the interrupt controller,
the A/D converter, the timers and the whole NGPC system model. **Three of our
"hardware findings" turned out to be wrong once these were read** (see
`HARDWARE_COMPAT_POLICY.md`). Do not guess what is in them.

| Document | Path | Authoritative for |
|---|---|---|
| **Toshiba TLCS-900/L1 CPU manual** | `../NgpCraft_toolchain/doc t_900/catalog_en_20010831_ALT00146.txt` | SR layout, **IFF mask rules** (accept when `L >= IFF`; mask := `L+1`), reset state, **vector base 0xFFFF00** |
| **Toshiba TMP95C061 datasheet** (PDF) | manufacturer document | **Table 3.3 (1) = the complete interrupt vector table**, A/D converter (ADMOD / ADREG), 8-bit timers, prescaler |
| **Official SNK NGPC SDK** | `../../01_SDK/docs/` | see the file-by-file table below |

### 0.1 The SNK SDK, file by file (`01_SDK/docs/`)

| File | Contains |
|---|---|
| `SysPro.txt` / `MANSysPro.txt` | **USER PROGRAM INTERRUPT OPERATION VECTOR** (the RAM vector table at `0x6FB8`, and *"Vertical Blanking Interrupt (**Interrupt level 4**)"*); `Battery_voltage (0x6f80)`; "A/D converter — 1 channel (Power management)" |
| `SysWork.txt` | system work RAM map; battery range `0H~3FFH`; shutdown-on-low-battery |
| `8Bit.txt` | **8-bit timers**: TRUN/TREG/T01MOD/T23MOD, clock sources, prescaler periods, comparator |
| `K2GETechRef.txt` | video: frame rate, raster position, scanline counts |
| `SysCall.txt` | BIOS system-call vector table |
| `SerialCom.txt` | link-cable BIOS vectors |
| `MicroDMA.txt` | micro-DMA start vectors |
| `FlashMem.txt` / `FlashWriter.txt` | cartridge flash |
| `Emulation.txt` | the official dev/emulation unit |

### 0.2 ⚠️ How to read the datasheet PDF

**The tables in the TMP95C061 PDF are IMAGES** — they do not survive text
extraction, and a text-converted copy will silently *omit* them. Render the page
and read it:

```python
import fitz                       # pymupdf (installed)
d = fitz.open("TMP95C061.PDF")
d[11].get_pixmap(dpi=200).save("page.png")   # then read the image
```

Key pages: **11** = Interrupt Table (3.3 (1)), **148** = ADMOD, **149/150** = ADREG0-3.

## 1. Read First

These are the best entry points when starting work on the emulator.

### Emulator core / hardware / reverse

- `../Doc de dev/Final/Doc final uniformise eng/INDEX.md`
  - master navigation for the unified final docs
- `../Doc de dev/Final/Doc final uniformise eng/SOURCES_MAP.md`
  - topic -> document lookup map
- `../../01_SDK/docs/NGPC_SDK_MASTER_ENTRY.md`
  - best SDK "read this first" index
- `../../01_SDK/docs/NGPC_HW_QUICKREF.md`
  - short hardware quick reference
- `../NgpCraft_toolchain/NGPC_REVERSE_REFERENCE.md`
  - reverse-derived hardware, ABI, boot, watchdog, init details
- `../NgpCraft_toolchain/T900_DENSE_REF.md`
  - dense TLCS-900 / cc900 reference
- `../NgpCraft_Disasm/catalog_en_20010831_ALT00146.txt`
  - opcode catalog in text form, easier to grep than the PDF
- `../NgpCraft_Disasm/TMP95C061BFG_datasheet_en_20110126-1134408.pdf`
  - primary CPU datasheet

### Emulator integration / validation

- `../NgpCraft_engine/README_manual.md`
  - headless mode, validation suite, build/run flow
- `../NgpCraft_engine/API_REFERENCE.md`
  - public engine API and headless entry points
- `../NgpCraft_engine/PROJET.md`
  - architecture and long-form project context

## 2. CPU / ISA / ABI / broken opcodes

Use these first when implementing CPU execution, disassembly alignment, calling convention, or silicon quirks.

- `../NgpCraft_Disasm/catalog_en_20010831_ALT00146.txt`
  - opcode tables
- `../NgpCraft_Disasm/TMP95C061BFG_datasheet_en_20110126-1134408.pdf`
  - CPU behavior and registers
- `../NgpCraft_toolchain/T900_DENSE_REF.md`
  - practical cc900/TLCS-900 reference
- `../Doc de dev/Final/Doc final uniformise eng/T900_DENSE_REF.md`
  - same topic from the final doc set, useful cross-check
- `../Doc de dev/Final/Doc final uniformise eng/ASM.md`
  - asm900/TLCS-900 assembly notes and gotchas
- `../Doc de dev/Final/Doc final uniformise eng/REVERSE.md`
  - reverse engineering notes from real games
- `../NgpCraft_toolchain/DISASM_CROSSCHECK.md`
  - very important for broken opcodes vs false positives
- `../NgpCraft_toolchain/J8_RESET_AUDIT.md`
  - targeted toolchain/hardware sanity note
- `../NgpCraft_Disasm/ngpc_disasm.py`
  - tool, not a doc, but useful as reference implementation for decoding/disasm
- `../NgpCraft_Disasm/MANUAL.md`
  - how the disassembler is meant to be used
- `../NgpCraft_Disasm/README.md`
  - project overview

## 3. Hardware map / BIOS / IRQ / timers / DMA

Use these for memory map, interrupts, system variables, timing, DMA and BIOS behavior.

- `../Doc de dev/Final/Doc final uniformise eng/HW_REGISTERS.md`
  - full register map and gotchas
- `../Doc de dev/Final/Doc final uniformise eng/BIOS_REF.md`
  - BIOS calls and conventions
- `../Doc de dev/Final/Doc final uniformise eng/DMA.md`
  - DMA behavior, pitfalls, performance notes
- `../Doc de dev/Final/Doc final uniformise eng/DMA_ASM.md`
  - DMA assembly patterns
- `../Doc de dev/Final/Doc final uniformise eng/GAME_LOOP.md`
  - frame flow, VBlank, watchdog, budgets
- `../../01_SDK/docs/ngpcspec.txt`
  - memory map, cart header, VBlank, interrupts
- `../../01_SDK/docs/SysCall.txt`
  - BIOS system calls
- `../../01_SDK/docs/SysLib.txt`
  - system library functions
- `../../01_SDK/docs/SysPro.txt`
  - system programming details
- `../../01_SDK/docs/SysWork.txt`
  - system variables
- `../../01_SDK/docs/8Bit.txt`
  - timer details
- `../../01_SDK/docs/MicroDMA.txt`
  - micro DMA reference
- `../../01_SDK/docs/Emulation.txt`
  - old SDK emulation/debug notes

## 4. Video / graphics / palettes / sprites / tilemaps

Use these for K1GE/K2GE, VRAM/OAM, palette formats, scroll planes and rendering details.

- `../Doc de dev/Final/Doc final uniformise eng/SPRITES_OAM.md`
  - OAM, sprite palettes, budgets
- `../Doc de dev/Final/Doc final uniformise eng/TILEMAPS_SCROLL.md`
  - SCR1/SCR2, tilemaps, scrolling
- `../Doc de dev/Final/Doc final uniformise eng/GRAPHICS_GUIDE.md`
  - graphics pipeline reference
- `../Doc de dev/Final/Doc final uniformise eng/COLORS_PALETTES.md`
  - color and palette rules
- `../Doc de dev/Final/Doc final uniformise eng/VRAMQ_ASM_LDIRW.md`
  - VRAM queue / copy behavior
- `../../01_SDK/docs/K1GETechRef.txt`
  - K1GE video hardware
- `../../01_SDK/docs/K2GETechRef.txt`
  - K2GE video hardware
- `../../01_SDK/docs/K2GEpal.txt`
  - K2GE palette details
- `../../01_SDK/docs/K2GEres.txt`
  - K2GE resources / quick details
- `../NgpCraft_engine/templates/NgpCraft_base_template/docs/NGPC_GRAPHICS_GUIDE.md`
  - template-side practical graphics usage
- `../NgpCraft_engine/templates/NgpCraft_base_template/docs/NGPC_HW_REGISTERS.md`
  - compact template-side register reference

## 5. Audio

Use these for Z80 side, PSG, timing and future audio emulation.

- `../Doc de dev/Final/Doc final uniformise eng/AUDIO.md`
  - project-level audio integration knowledge
- `../../01_SDK/docs/MANUAL sound.txt`
  - main sound manual
- `../../01_SDK/docs/K1SoundSim.txt`
  - sound simulator docs
- `../../01_SDK/docs/TECHNICAL k1sound.txt`
  - lower-level sound notes
- `../../01_SDK/docs/Z80_SFX_NOTES.md`
  - curated notes
- `../NgpCraft_engine/templates/NgpCraft_base_template/docs/SOUND_DRIVER_REF.md`
  - runtime-side sound behavior
- `../NgpCraft_engine/templates/NgpCraft_base_template/docs/SOUND_INTEGRATION_QUICKSTART.md`
  - practical integration

Audio is not day-one priority for the emulator, but these are the main references when it becomes necessary.

## 6. Saves / flash / storage / RTC

Use these early. Save handling is explicitly a project requirement.

- `../Doc de dev/Final/Doc final uniformise eng/STORAGE.md`
  - flash save and RTC overview
- `../../01_SDK/docs/FlashMem.txt`
  - flash memory behavior
- `../../01_SDK/docs/FlashWriter.txt`
  - flash writing usage
- `../../01_SDK/docs/MANFlashWriter.txt`
  - related flash writer notes
- `../../01_SDK/docs/SysCall.txt`
  - flash BIOS calls
- `../../01_SDK/docs/SysLib.txt`
  - `CLR_FLASH_RAM`, helpers, system lib behavior
- `../NgpCraft_engine/templates/NgpCraft_base_template/docs/NGPC_FLASH_SAVE_GUIDE.md`
  - very important, validated on real hardware
- `../NgpCraft_engine/templates/NgpCraft_base_template/docs/NGPC_BIOS_REF.md`
  - BIOS call conventions in template context
- `../NgpCraft_toolchain/StarGunner_save_lib_test/README.md`
  - concrete 2 MB cart flash save layout used by the preferred smoke ROM
- `../NgpCraft_toolchain/StarGunner_save_lib_test/src/core/ngpc_flash.c`
  - exact save-slot addressing and erased-flash assumptions (`0x3FA000..0x3FBFFF`, `0xFF`)

## 7. Input / gameplay / runtime context

Useful when the debugger UI needs runtime-aware views or when validating engine-generated projects.

- `../Doc de dev/Final/Doc final uniformise eng/INPUT.md`
  - input behavior
- `../Doc de dev/Final/Doc final uniformise eng/GAMEPLAY_MECHANICS.md`
  - gameplay conventions from projects
- `../Doc de dev/Final/Doc final uniformise eng/COLLISION.md`
  - collision models and tile collision assumptions
- `../Doc de dev/Final/Doc final uniformise eng/MATH_FIXED.md`
  - fixed-point and LUT patterns

## 8. Toolchain / real hardware case studies

These are extremely useful for emulator validation because they contain real-world breakage, timing, and comparison material.

- `../NgpCraft_toolchain/DEVLOG.md`
  - long historical log of hardware findings and fixes
- `../NgpCraft_toolchain/DECISIONS.md`
  - design decisions worth reusing
- `../NgpCraft_toolchain/DISASM_CROSSCHECK.md`
  - opcode and hardware cross-checks
- `../NgpCraft_toolchain/test/jalon16/J16_REGRESSION_BISECT_2026-04-02.md`
  - recent hardware regression case study
- `../NgpCraft_toolchain/test/jalon16/J16_STATUS_2026-03-25.md`
  - jalon16 status snapshot
- `../NgpCraft_toolchain/test/jalon16/stargunner_official_toolchain_disasm.asm`
  - official-toolchain disasm reference
- `../NgpCraft_toolchain/test/jalon16/official_main_ngpcdis.asm`
  - official ROM disasm used in recent comparison
- `../NgpCraft_toolchain/test/jalon16/bisect_dma0_bgmfix_ngpcdis.asm`
  - last known good DMA0 disasm reference

Use these when checking:
- crash fidelity
- slowdown fidelity
- official-toolchain vs NgpCraft codegen behavior
- what the emulator should expose in diagnostics

## 9. Engine integration / CI / validation

These are the main references for replacing the external emulator in the current workflow.

### Docs

- `../NgpCraft_engine/README_manual.md`
  - headless mode, validation suite, smoke-run, runtime expectations
- `../NgpCraft_engine/API_REFERENCE.md`
  - importable core API and validation helpers
- `../NgpCraft_engine/PROJET.md`
  - architecture, roadmap, internal concepts
- `../NgpCraft_engine/README.md`
  - shorter project overview
- `../NgpCraft_engine/REFACTOR_EXPORT_PLAN.md`
  - useful for runtime/perf context and validation scenarios
- `../NgpCraft_engine/roadmap_perf_opt.md`
  - performance and validation context from real engine workloads

### Template docs

- `../NgpCraft_engine/templates/NgpCraft_base_template/docs/README.md`
  - template docs index
- `../NgpCraft_engine/templates/NgpCraft_base_template/docs/NGPC_BIOS_REF.md`
  - BIOS usage in runtime code
- `../NgpCraft_engine/templates/NgpCraft_base_template/docs/NGPC_GRAPHICS_GUIDE.md`
  - rendering and map behavior
- `../NgpCraft_engine/templates/NgpCraft_base_template/docs/NGPC_FLASH_SAVE_GUIDE.md`
  - save behavior
- `../NgpCraft_engine/templates/NgpCraft_base_template/docs/SOUND_DRIVER_REF.md`
  - sound runtime reference

### Code entry points worth bookmarking

These are code, not docs, but they matter for integration:

- `../NgpCraft_engine/ngpcraft_engine.py`
  - main GUI/headless entry point
- `../NgpCraft_engine/ui/run_dialog.py`
  - current external emulator launch config
- `../NgpCraft_engine/ui/tabs/project_tab.py`
  - current build/run hooks
- `../NgpCraft_engine/core/headless_export.py`
  - headless export path
- `../NgpCraft_engine/core/validation_runner.py`
  - validation suite runner

## 10. gb2ngp docs

Lower priority, but potentially useful for compatibility experiments and TLCS-900-side boot/runtime expectations.

- `../NgpCraft_gb2ngp/DEVNOTES.md`
  - converter notes
- `../NgpCraft_gb2ngp/ROADMAP.md`
  - project context
- `../NgpCraft_gb2ngp/gb_hal.asm`
  - low-level behavior
- `../NgpCraft_gb2ngp/ngpc_crt0_gb.asm`
  - startup assumptions

## 11. Suggested Reading Order By Task

### If working on CPU execution

1. `../../01_SDK/docs/NGPC_SDK_MASTER_ENTRY.md`
2. `../NgpCraft_Disasm/catalog_en_20010831_ALT00146.txt`
3. `../NgpCraft_Disasm/TMP95C061BFG_datasheet_en_20110126-1134408.pdf`
4. `../NgpCraft_toolchain/T900_DENSE_REF.md`
5. `../NgpCraft_toolchain/DISASM_CROSSCHECK.md`

### If working on memory map / BIOS / IRQ / DMA

1. `../Doc de dev/Final/Doc final uniformise eng/HW_REGISTERS.md`
2. `../Doc de dev/Final/Doc final uniformise eng/BIOS_REF.md`
3. `../Doc de dev/Final/Doc final uniformise eng/DMA.md`
4. `../../01_SDK/docs/ngpcspec.txt`
5. `../../01_SDK/docs/SysCall.txt`
6. `../../01_SDK/docs/MicroDMA.txt`

### If working on video / renderer / VRAM viewers

1. `../Doc de dev/Final/Doc final uniformise eng/SPRITES_OAM.md`
2. `../Doc de dev/Final/Doc final uniformise eng/TILEMAPS_SCROLL.md`
3. `../../01_SDK/docs/K2GETechRef.txt`
4. `../../01_SDK/docs/K1GETechRef.txt`
5. `../NgpCraft_engine/templates/NgpCraft_base_template/docs/NGPC_GRAPHICS_GUIDE.md`

### If working on saves

1. `../NgpCraft_engine/templates/NgpCraft_base_template/docs/NGPC_FLASH_SAVE_GUIDE.md`
2. `../Doc de dev/Final/Doc final uniformise eng/STORAGE.md`
3. `../../01_SDK/docs/FlashMem.txt`
4. `../../01_SDK/docs/SysCall.txt`
5. `../../01_SDK/docs/SysLib.txt`

### If working on engine integration

1. `../NgpCraft_engine/README_manual.md`
2. `../NgpCraft_engine/API_REFERENCE.md`
3. `../NgpCraft_engine/ngpcraft_engine.py`
4. `../NgpCraft_engine/ui/run_dialog.py`
5. `../NgpCraft_engine/ui/tabs/project_tab.py`

### If working on crash fidelity / slowdown fidelity

1. `../NgpCraft_toolchain/test/jalon16/J16_REGRESSION_BISECT_2026-04-02.md`
2. `../NgpCraft_toolchain/DEVLOG.md`
3. `../NgpCraft_toolchain/DISASM_CROSSCHECK.md`
4. `../NgpCraft_toolchain/test/jalon16/stargunner_official_toolchain_disasm.asm`
5. `../Doc de dev/Final/Doc final uniformise eng/GAME_LOOP.md`
6. `../Doc de dev/Final/Doc final uniformise eng/DMA.md`

## 12. Ignore For Now

These are not a priority for the emulator roadmap right now:

- unrelated PDFs in `../NgpCraft_Disasm` that are not NGPC/TLCS-900 references
- large archive files unless needed for recovery
- broad gameplay/design docs unless a debugger view needs them

## 13. Maintenance Rule

Whenever a new important hardware finding appears:
- add the source here
- tag it by topic
- if it changes emulator behavior, also link it from:
  - `HARDWARE_COMPAT_POLICY.md`
  - `PERF_TIMING_POLICY.md`
  - `SAVE_POLICY.md`
