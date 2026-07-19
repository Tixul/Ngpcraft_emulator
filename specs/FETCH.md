# Fetch v0

Purpose:
- define the first raw PC-relative fetch helper
- prepare later instruction decode work without pretending decode exists yet

Current source references:
- `CPU_STATE.md`
- `MEMORY_READ.md`
- `../../01_SDK/docs/ngpcspec.txt`
- `../../01_SDK/docs/NGPC_SDK_MASTER_ENTRY.md`

Current behavior:
- fetch starts from the current bootstrap `PC`
- fetch currently reads a raw byte window through the existing read-only bus model
- fetch does not decode the instruction
- fetch does not claim the true instruction length
- fetch only computes a simple sequential `PC + count` helper when bytes were read successfully

Current CLI user:
- `python ngpc_emu.py fetch-next <rom>`
- `python ngpc_emu.py fetch-next <rom> --count N`

Examples:
- fetch the first 4 bytes at the current bootstrap `PC`:
  - `python ngpc_emu.py fetch-next game.ngc`
- fetch an 8-byte raw preview:
  - `python ngpc_emu.py fetch-next game.ngc --count 8`

Not implemented yet:
- opcode decode
- instruction length determination
- operand decoding
- side effects
- cycle accounting
- actual execution
