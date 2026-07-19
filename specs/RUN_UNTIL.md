# Run Until v0

Purpose:
- define the first run-until-shaped output built on top of the current static stepping helpers
- make bounded address-target inspection possible before real execution exists

Current source references:
- `STEP.md`
- `TRACE.md`
- `../../NgpCraft_toolchain/T900_DENSE_REF.md`

Current behavior:
- run-until preview starts from one explicit address, or from the current bootstrap `PC`
- run-until preview requires one explicit target `PC`
- run-until preview is decode-only and execution-neutral
- run-until preview chains the current static stepping rules in `over` or `into` mode
- default CLI mode is `over` because it stays more useful in the current no-execution model

Current chaining rules:
- `over` mode:
  - reuses the same rules as `next-preview`
  - direct calls preview to the sequential return site
  - this assumes the call returns normally
- `into` mode:
  - reuses the same rules as `step-preview`
  - direct calls preview to the decoded callee entry
- both modes:
  - stop immediately when a step result becomes unresolved
  - stop on decode failure
  - stop on a repeated `PC` cycle
  - stop when the step budget is exhausted
  - stop when the target `PC` is reached

Current result fields:
- start `PC`
- target `PC`
- terminal `PC`
- chained preview mode
- maximum step budget
- explicit `reached_target` flag
- explicit `stop_reason`
- per-step decoded instruction payload
- per-step preview target and reason
- per-step matched quirk metadata through the nested decode payload when relevant,
  including database version and per-rule `sources` attribution

Current CLI user:
- `python ngpc_emu.py run-until-preview <rom> <target>`
- `python ngpc_emu.py run-until-preview <rom> <target> --address 0x200094`
- `python ngpc_emu.py run-until-preview <rom> <target> --mode into`
- `python ngpc_emu.py run-until-preview <rom> <target> --max-steps 32`

Important:
- this is not real run control
- this does not execute instructions
- this does not mutate CPU state
- this does not evaluate flags or branch conditions
- `over` mode assumes direct calls return normally
- `into` mode can stop at `RET` / `RETI` / `RETD` / `SWI` / `HALT` because those remain runtime-dependent in the current model

Not implemented yet:
- real debugger `run until`
- breakpoint tables
- call stack and return prediction
- state mutation
- branch-condition evaluation
