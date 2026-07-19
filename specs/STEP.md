# Step v0

Purpose:
- define the first stepping-shaped output built on top of the current decode helper
- make one-step inspection possible before real execution exists

Current source references:
- `DECODE.md`
- `TRACE.md`
- `../../NgpCraft_toolchain/T900_DENSE_REF.md`

Current behavior:
- step preview starts from one explicit address, or from the current bootstrap `PC`
- step preview is decode-only and execution-neutral
- step preview currently behaves like a static `step into` preview
- step preview uses control-flow metadata from the current decoder
- a second static stepping mode now exists for `next` / step-over preview
- a third static control-flow helper now exists separately for chained `run-until-preview`; see `RUN_UNTIL.md`

Current preview rules:
- non-control-flow instruction:
  - preview target = sequential next `PC`
- direct `CALL` / `JP` / direct unconditional branch:
  - preview target = decoded direct target
- conditional control-flow:
  - preview target remains unresolved
- `RET` / `RETI` / `RETD` / `SWI` / `HALT`:
  - preview target remains unresolved

Current `next` preview rules:
- non-control-flow instruction:
  - preview target = sequential next `PC`
- direct `CALL` / `CALR`:
  - preview target = sequential return site
  - this assumes the call returns normally
- direct non-call jump:
  - preview target = decoded direct target
- conditional control-flow:
  - preview target remains unresolved
- `RET` / `RETI` / `RETD` / `SWI` / `HALT`:
  - preview target remains unresolved

Current result fields:
- decoded instruction payload
- preview mode
- control-flow kind
- direct target when statically known
- preview target when the current static rules can resolve one
- explicit reason string explaining the choice
- matched quirk metadata inside the nested decode payload when the current
  instruction hits one known local quirk, including database version and
  per-rule `sources` attribution

Current CLI user:
- `python ngpc_emu.py step-preview <rom>`
- `python ngpc_emu.py step-preview <rom> --address 0x200094`
- `python ngpc_emu.py next-preview <rom>`
- `python ngpc_emu.py next-preview <rom> --address 0x200094`

Important:
- this is not real stepping
- this does not mutate CPU state
- this does not evaluate flags or branch conditions
- this is only a static next-target preview based on current decode knowledge

Not implemented yet:
- real instruction execution
- state mutation
- stack effects
- branch-condition evaluation
