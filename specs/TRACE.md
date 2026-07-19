# Trace v0

Purpose:
- define the first trace-shaped output built on top of the current decode helper
- make the first bootstrap sequence readable without pretending execution already exists

Current source references:
- `DECODE.md`
- `FETCH.md`
- `../../NgpCraft_toolchain/T900_DENSE_REF.md`
- `../../NgpCraft_Disasm/ngpc_disasm.py`

Current behavior:
- trace preview starts from one explicit address, or from the current bootstrap `PC`
- trace preview walks linearly using `next sequential PC` only
- trace preview is decode-only and execution-neutral
- trace preview does not evaluate branch conditions
- trace preview does not follow taken control flow
- trace preview can optionally stop as soon as one control-flow instruction is decoded
- trace preview stops when:
  - the requested instruction count is reached
  - decode hits a non-`decoded` status
  - optional control-flow stop is enabled and one control-flow instruction is decoded
  - a sequential next `PC` is missing

Current stop reasons:
- `count-reached`
- `stopped-on-unknown-opcode`
- `stopped-on-truncated`
- `stopped-on-unmapped`
- `stopped-on-unbacked`
- `stopped-on-out-of-file`
- `stopped-on-control-flow`
- `stopped-on-missing-next-pc`

Current record fields:
- record index
- current `PC`
- decode status
- raw bytes
- decoded mnemonic / operands / assembly when available
- sequential next `PC`
- control-flow metadata when available from the current decoder
- warning text when a known silicon-risk pattern is recognized
- matched quirk metadata when a decoded instruction hits one known local quirk
  including the local quirk-database version and per-rule `sources` attribution

Current CLI user:
- `python ngpc_emu.py trace-preview <rom>`
- `python ngpc_emu.py trace-preview <rom> --count 16`
- `python ngpc_emu.py trace-preview <rom> --address 0x200050`
- `python ngpc_emu.py trace-preview <rom> --stop-on-control-flow`

Related real-execution trace:
- `TRACE_EXEC.md`
- `python ngpc_emu.py trace-exec <rom>`

Important:
- this is not an execution trace
- this is not yet the stable `trace v1` interchange format
- this is a linear preview to inspect the current bootstrap decode path honestly
- when `--stop-on-control-flow` is enabled, the preview behaves more like a static basic-block preview than a plain sequential walk

Not implemented yet:
- runtime execution events
- taken-branch control-flow tracking
- cycle timing
- memory side effects
- trace diff stability guarantees
