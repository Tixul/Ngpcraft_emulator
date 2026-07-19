# Reset State v1 (BIOS hand-off applied)

Purpose:
- define the current minimal bootstrap machine state
- distinguish "raw HW reset" (machine state at cold start) from
  "BIOS hand-off state" (what user code actually sees when it
  begins executing)

Current source references:
- `../../01_SDK/docs/ngpcspec.txt`
- `../../01_SDK/docs/NGPC_HW_QUICKREF.md`
- `../../01_SDK/docs/NGPC_SDK_MASTER_ENTRY.md`
- `../../01_SDK/docs/SysWork.txt`
- `../NgpCraft_toolchain/NGPC_REVERSE_REFERENCE.md`

---

## 1. Two layers — raw bootstrap vs BIOS hand-off

The real NGPC always runs the BIOS first ; user code at the cart
entry point starts with a set of register values the BIOS has set
up. Modeling user-code execution from a truly-cold CPU is therefore
NOT what games expect — the very first instruction of a typical
cart is `CALL N`, which needs a valid XSP.

The emulator now exposes BOTH layers :

- **Raw bootstrap state** (`core.machine.create_bootstrap_cpu_state`,
  `EmulatorSession(rom_path, apply_bios_handoff=False)`,
  `core.execute.build_run_steps` directly) : PC from cart header ;
  every other register field stays `None`. This is for the CLI
  and engine bridge, where strict honesty about unmodeled state
  is the doctrine.
- **BIOS hand-off state** (`EmulatorSession(rom_path)` — default
  for the UI) : the bootstrap CPU is augmented with the documented
  BIOS-equivalent register values described in §2 below. This lets
  the UI step real ROMs past their first stack-touching instruction.

The hand-off seed is opt-in at the session level (default ON for
the UI ; off for the CLI). The raw bootstrap is preserved at
`EmulatorSession.machine.cpu` for callers that want to compare.

---

## 2. BIOS hand-off values

| Field          | Value         | Source                                            |
|----------------|---------------|---------------------------------------------------|
| `cpu.pc`       | `entry_point` | Cart header byte+0x1C..+0x1F (little-endian)     |
| `regs.xsp`     | `0x00006C00`  | `NGPC_HW_QUICKREF.md §2` — top of 12 KB user RAM `0x004000–0x006BFF` ; `0x006C00..0x006FFF` is system-reserved. Stack grows downward from here. |
| `iff_level`    | `7`           | `ngpcspec.txt §INTERRUPT STATE` : *"The software starts up with interrupts prohibited (DI)"* — IFF = 7 is the TLCS-900/H "all maskable IRQs blocked" mask. User code does `EI 0` later. |
| `iff_enabled`  | `False`       | Derived from `iff_level == 7`.                    |
| `rfp`          | `0`           | The BIOS uses bank 3 for its own state ; hand-off to user code is in bank 0 (= default user bank). |
| `flags.{sf,zf,vf,hf,cf,nf}` | `False` × 6 | Conservative cleared state ; the BIOS doesn't expose well-defined post-hand-off flag values, so `False` is the safest choice that lets value-dependent conditional branches behave deterministically. |
| `control_registers.intnest` | `0` | Hand-off-layer invariant (local inference): the BIOS hands control to user code outside any active interrupt nesting. This is scoped to `EmulatorSession(..., apply_bios_handoff=True)` only ; raw bootstrap still keeps `INTNEST` unknown. |

Other R32 registers (`xwa`, `xbc`, `xde`, `xhl`, `xix`, `xiy`, `xiz`)
remain `None`. The BIOS doesn't guarantee specific values for them
on hand-off ; user code that depends on a specific R32 register at
boot is reading garbage on real HW too.

The hand-off values are defined as class-level constants on
`EmulatorSession` (`BIOS_HANDOFF_XSP`, `BIOS_HANDOFF_IFF_LEVEL`,
`BIOS_HANDOFF_RFP`, `BIOS_HANDOFF_INTNEST`, `BIOS_HANDOFF_FLAGS`) so
they are centralized and inspectable from tests.

---

## 3. What the BIOS hand-off seed unlocks

Empirical measurement (pass 48, smoke on the local ROM corpus) :

| ROM                       | Before pass 48 | After pass 48 |
|---------------------------|----------------|---------------|
| `minimal_template/main.ngc` | 0 instr (`requires-known-stack-pointer`) | 40 instr ; new blocker at `unsupported-decoded-instruction` |
| `HORATIO.ngp`             | 0 instr (`requires-known-address-register`) | 1 instr ; new blocker at `requires-known-full-register` |
| `POCKETRACE.ngp`          | 0 instr | 1 instr ; new blocker at `requires-known-full-register` |
| `MRROBOT.ngp`             | 0 instr | 1 instr ; new blocker at `requires-known-full-register` |

The hand-off doesn't unlock every ROM (most need additional
state that the BIOS would have initialized, plus more opcodes in
the executor's subset), but it removes the single dominant
"first-instruction" blocker for `minimal_template` and any cc900-
compiled ROM that does `CALL`/`PUSH` immediately.

---

## 4. What's still not modeled (post pass 48)

- Other R32 register defaults (`XIX`, `XIY`, `XIZ` typically used
  by cc900 as global / frame / heap pointers — populated by the
  cc900 `crt0`, not by the BIOS)
- BIOS-initialized RAM contents : font tables (the BIOS installs them itself
  via `VECT_SYSFONTSET` — modelled), and the system work area at
  `0x006C00..0x006FFF`

> **Updated 2026-07-10 (passes 180-186).** Several items previously listed here as
> "not modelled" have since landed. Corrections:
>
> - **Power-on register values are NOT `0x00`.** Both the CPU I/O page
>   (`0x000000..0x0000FF`) and the K2GE registers have documented reset values —
>   notably `0x008000 = 0xC0` (VBlank + HBlank interrupts **enabled**),
>   `0x0020 TRUN = 0x80`, `0x0060/0x61 ADREG0 = 0xFFFF` (the battery). Full table
>   in `specs/MEMORY_READ.md` § 2.
> - **BIOS SWI handlers are no longer a silent no-op stub.** `swi 1` dispatches on
>   **RW3** and every deterministic vector is implemented. See `specs/BIOS_HLE.md`.
> - **The interrupt vector table at `0x006FB8`** is the *user* (RAM) table; the
>   *hardware* table lives in BIOS ROM at `0xFFFF00`. `0x006FCC` is simply slot 5
>   (VBlank) of the RAM table. See `specs/FRAME_TIMING.md` § 3.6.
> - **TMP95C061 timers 0..3** and the **A/D converter** are modelled — see
>   `specs/TIMERS.md` and `specs/ADC.md`.

Still not modelled:

- Z80 sub-CPU / PSG audio
- RTC (the `VECT_RTCGET` BIOS call reads the host clock; there is no RTC
  peripheral / alarm model)
- 16-bit timers 4/5, micro-DMA engine

---

## 5. Doctrine

- Raw bootstrap state stays minimal and intentionally honest. The
  CLI default (used by the engine bridge, CI, batch scripts)
  remains "every R32 is None, no fake reset values" so the
  honesty contract for non-UI consumers is preserved.
- The BIOS hand-off seed distinguishes between:
  - sourced fields (`XSP`, `iff_level`, `rfp`)
  - narrow hand-off invariants justified by control-flow context
    (`INTNEST = 0` because user code is not entered from the middle of
    an already-nested IRQ chain)
- Raw bootstrap truth still wins outside that layer: adding a new
  seeded field requires either a citable doc line or a similarly
  narrow hand-off-only invariant that is documented explicitly.
- The hand-off layer is opt-in via the `apply_bios_handoff` kwarg
  so a future "raw bootstrap" UI mode can flip it off without
  touching session callers.
