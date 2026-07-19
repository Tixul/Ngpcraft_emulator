# Execution Trace v0

Purpose:
- define the first real instruction-by-instruction execution trace above `execute-next`
- distinguish honest runtime execution from the static `trace-preview` decode walk

Current source references:
- `EXECUTE.md`
- `RUN_STEPS.md`
- `TRACE.md`

Current behavior:
- `trace-exec` starts from one explicit address, or from the current bootstrap `PC`
- it executes up to `N` instructions using the current real execution subset
- it records one execution payload per attempted instruction
- it carries forward:
  - `after_cpu`
  - `after_memory`
- it stops early when one instruction cannot be executed honestly

Current stop model:
- `count-reached`
- `stopped-on-<status>` when one step returns a non-`executed` status
- confirmed broken opcode families therefore surface naturally as
  `stopped-on-silicon-broken`

Current record fields:
- record index
- decode payload
- matched quirk metadata when the attempted instruction hits one known local quirk,
  including the current quirk-database version and per-rule `sources` attribution
- execution status
- written registers
- memory writes
- after-memory snapshot
- CPU before/after
- flag changes
- execution note

Current CLI use:
- `python ngpc_emu.py trace-exec <rom>`
- `python ngpc_emu.py trace-exec <rom> --count 16`
- `python ngpc_emu.py trace-exec <rom> --address 0x20E1E2 --seed-reg XWA=0x003FBE00`

Important:
- this is a real execution trace, not a static preview
- it shares all current `execute-next` limits
- it is still local to one command invocation
- it is a first trace shape, not yet the final stable interchange format promised by the roadmap

Related stable format:
- `EVENT_LOG.md` defines the locked v1 format for diff / CI /
  regression use; `trace-exec` stays as the evolving ad-hoc shape
  while the locked event-log CLI (`eventlog capture/inspect/diff`)
  is being implemented.

Not implemented yet:
- persistent trace sessions across commands
- timing/cycle information
- trace diff stability guarantees (covered by the event log once
  its CLI lands)
