# Run Steps v0

Purpose:
- define the first bounded stateful execution loop above `execute-next`
- carry `CPU` state and the current writable stack overlay across multiple instructions in one invocation

Current source references:
- `EXECUTE.md`
- `STEP.md`
- `TRACE.md`

Current behavior:
- `run-steps` starts from one explicit address, or from the current bootstrap `PC`
- it executes up to `N` instructions using the current real execution subset
- it carries forward:
  - `after_cpu`
  - `after_memory`
- it stops early when one step cannot be executed honestly

Current supported execution basis:
- everything already covered by `execute-next`
- this currently includes:
  - `NOP`
  - direct unconditional jumps with known direct target
  - representable immediate register loads
  - first absolute-address `LDA R32, (abs24)` forms
  - first prefixed register-to-register `LD`
  - first prefixed register-to-register `CP`
  - first representable prefixed register `inc` / `dec`
  - first conditional `JR` / `JRL` execution when the required modeled flags are known
  - first representable indexed load: `LD R32, (r32+d8)`
  - first representable indexed writable store: `LD (r32+d8), R32`
  - first indexed compare: `CP (r32+d8), R32`
  - first abs16 byte compare-immediate: `CP (abs16), imm8`
  - first post-increment byte forms:
    - `LD R8, (r32+)`
    - `LD (r32+), R8`
    - `LD (r32+), imm8`
  - first writable-stack subset:
    - `pushw`
    - `push`
    - `pop`
    - `call`
    - `ret`
    - `retd`

Current stop model:
- `count-reached`
- `stopped-on-<status>` when one step returns a non-`executed` status

Current result fields:
- start `PC`
- requested step count
- emitted record count
- executed record count
- explicit stop reason
- final CPU state after the last successfully executed step
- final writable-memory overlay
- per-record execution payload

Current CLI use:
- `python ngpc_emu.py run-steps <rom>`
- `python ngpc_emu.py run-steps <rom> --count 4`
- `python ngpc_emu.py run-steps <rom> --address 0x2079C6 --seed-xsp 0x4100 --count 3`
- `python ngpc_emu.py run-steps <rom> --address 0x2079C6 --seed-xsp 0x4100 --seed-reg XIZ=0x12345678 --count 5`
- `python ngpc_emu.py run-steps <rom> --address 0x2079C6 --seed-xsp 0x4100 --seed-reg XIZ=0x12345678 --count 12`
- `python ngpc_emu.py run-steps <rom> --address 0x2079C6 --seed-xsp 0x4100 --seed-reg XIZ=0x12345678 --count 16`
- `python ngpc_emu.py run-steps <rom> --address 0x2079C6 --seed-xsp 0x4100 --seed-reg XIZ=0x12345678 --count 24`

Important:
- this is the first bounded stateful execution slice, not a full run loop
- it only chains the currently implemented execution subset
- one blocked instruction stops the run immediately instead of being guessed past
- manual register seeding is available for honest smoke/bisect use while reset-time register values are still unknown
- the state is still local to one command invocation
- on the current stable smoke ROM, this is now enough to enter and iterate the first byte-copy loop honestly:
  - `ld XWA, (XSP+4)`
  - `ld C, (XWA+)`
  - `ld (XSP+4), XWA`
  - `ld (XIZ+), C`
  - `cp (XSP+4), XIX`
  - `jr C, 0x20D08F`
- the sibling zero-fill loop is also executable with direct seeded entry:
  - `ld (XIY+), 0x00`
  - `cp XIY, XDE`
  - `jr C, 0x20D0A4`
- after the seeded zero-fill exit, the next stable-ROM step now decodes as:
  - `0x0020D0AC pop XIZ`
- on the stable official-toolchain ROM, `run-steps` can now also carry:
  - the `0x20D0DA` color/mono branch via built-in readable system bytes
  - `ld (0x005F80), A`
  - `res 5, (0x6F86)` / `set 6, (0x6F86)`
  - the small init subroutine at `0x20D21D`
  - vector initialization through `0x6FFC`
  - the first `ld (abs16), imm8` K2GE writes through `0x8035`
- the next honest runtime blocker after that is:
  - `0x0020D16A res 7, (0x8030)`
  - it needs readable K2GE state for a real read-modify-write

Not implemented yet:
- persistent debugger session state across commands
- `step over`
- `run until`
- full branch-condition evaluation
- full flags and `SR` mutation
- general memory/IO writes
- readable K2GE defaults / register semantics beyond the tiny stable-bootstrap slice
- final execution-trace format
