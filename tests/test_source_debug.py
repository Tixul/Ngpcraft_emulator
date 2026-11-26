"""C source-level debugging: the `.ngdbg` reader and line stepping.

The stepping runs on a tiny scripted machine with the native machine's shape, so
the rules are checked exactly, without a ROM. The program reproduces the trap that
broke the first version: a callee whose PROLOGUE pushes, so XSP goes down inside
the same function -- judging "which function am I in" by XSP skipped its lines.
"""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from core.source_step import step_line, step_out
from core.symbols import load_ngdbg, load_symbols, symbol_files_for_rom

# --- the program -------------------------------------------------------------
#   main.c                                f.c
#   0x100  line 1   nop (2)               0x200 line 10  push (2 bytes, prologue)
#   0x102  line 2   call f (3)            0x201 line 10  nop (1)
#   0x105  line 3   nop (2)               0x202 line 11  nop (2)
#   0x107  line 3   jump back to 0x105    0x204 line 11  pop
#          (one-line loop, `while(x);`)   0x205 line 11  ret
#   0x109  line 4   nop
NOP, CALL, RET, PUSH, POP, JMP = "nop", "call", "ret", "push", "pop", "jmp"
PROGRAM = {
    0x100: (NOP, 2), 0x102: (CALL, 3, 0x200), 0x105: (NOP, 2), 0x107: (JMP, 2, 0x105),
    0x109: (NOP, 2),
    0x200: (PUSH, 1), 0x201: (NOP, 1), 0x202: (NOP, 2), 0x204: (POP, 1), 0x205: (RET, 1),
}
DEBUG = {
    "format": "ngpcraft-debug", "version": 1,
    "fichiers": ["src/main.c", "src/f.c"],
    "lignes": [[0x100, 0, 1], [0x102, 0, 2], [0x105, 0, 3], [0x109, 0, 4],
               [0x200, 1, 10], [0x202, 1, 11]],
    "symboles": [{"nom": "_main", "adresse": 0x100, "fichier": 0, "section": "f_code"},
                 {"nom": "_f", "adresse": 0x200, "fichier": 1, "section": "f_code"},
                 {"nom": "_asm_helper", "adresse": 0x300, "fichier": -1,
                  "section": "f_code"}],
}


class FakeMachine:
    """Native-machine shape: cpu().pc / regs[7], read, run(n), set_breakpoints.
    Like the real core, a breakpoint is IGNORED on the first instruction of a run."""

    def __init__(self, pc: int) -> None:
        self.pc, self.sp = pc, 0x1000
        self.mem = bytearray(0x2000)
        for a, ins in PROGRAM.items():
            self.mem[a] = 0x0E if ins[0] == RET else 0x00
        self.bps: set[int] = set()

    def cpu(self):
        return SimpleNamespace(pc=self.pc, regs=[0] * 7 + [self.sp])

    def read(self, addr: int, n: int) -> bytes:
        return bytes(self.mem[addr:addr + n])

    def set_breakpoints(self, pcs) -> None:
        self.bps = set(pcs)

    def _one(self) -> None:
        ins = PROGRAM[self.pc]
        kind = ins[0]
        if kind == CALL:
            ret = self.pc + ins[1]
            self.sp -= 4
            self.mem[self.sp:self.sp + 4] = ret.to_bytes(4, "little")
            self.pc = ins[2]
        elif kind == RET:
            self.pc = int.from_bytes(self.mem[self.sp:self.sp + 3], "little")
            self.sp += 4
        elif kind == JMP:
            self.pc = ins[2]
        else:
            if kind == PUSH:
                self.sp -= 2
            elif kind == POP:
                self.sp += 2
            self.pc += ins[1]

    def run(self, n: int, record: bool = False):
        done = 0
        while done < n and self.pc in PROGRAM:
            if done and self.pc in self.bps:
                break
            self._one()
            done += 1
        return SimpleNamespace(executed=done), []


@pytest.fixture()
def table(tmp_path):
    p = tmp_path / "game.ngdbg"
    p.write_text(json.dumps(DEBUG), encoding="utf-8")
    return load_ngdbg(str(p))


# --- reader ------------------------------------------------------------------
def test_reader_symbols_and_lines(table):
    assert table.lookup_name("_f").address == 0x200
    pos = table.lines.at(0x203)
    assert (pos.file, pos.line, pos.start, pos.end) == ("src/f.c", 11, 0x202, 0x300)


def test_code_after_the_last_line_is_not_claimed_past_the_next_symbol(table):
    # 0x300 is an asm helper with no lines: it must not read as f.c:11
    assert table.lines.at(0x300) is None
    assert table.lines.at(0x2FF) is not None


def test_a_line_without_code_moves_to_the_next_one(table):
    assert table.lines.addresses_for("src/main.c", 2) == (2, [0x102])
    used, addrs = table.lines.addresses_for("src/f.c", 5)      # before the function
    assert (used, addrs) == (10, [0x200])
    assert table.lines.addresses_for("src/f.c", 99) == (99, [])


def test_source_found_by_walking_up_from_the_rom(tmp_path, table):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "main.c").write_text("int x;\n", encoding="utf-8")
    (tmp_path / "bin").mkdir()
    found = table.lines.resolve_file("src/main.c", tmp_path / "bin")
    assert found == tmp_path / "src" / "main.c"


def test_ngdbg_preferred_to_map_beside_the_rom(tmp_path):
    rom = tmp_path / "game.ngc"
    rom.write_bytes(b"")
    (tmp_path / "game.map").write_text("=== Public symbols ===\n  _main 0x00200000\n")
    (tmp_path / "game.ngdbg").write_text(json.dumps(DEBUG), encoding="utf-8")
    files = symbol_files_for_rom(rom)
    assert [f.suffix for f in files] == [".ngdbg", ".map"]
    assert load_symbols(str(files[0])).lines is not None
    assert load_symbols(str(files[1])).lines is None


def test_not_a_debug_file_is_refused(tmp_path):
    p = tmp_path / "x.ngdbg"
    p.write_text('{"format": "something else"}', encoding="utf-8")
    with pytest.raises(ValueError):
        load_ngdbg(str(p))


# --- stepping ----------------------------------------------------------------
def test_over_runs_the_call(table):
    m = FakeMachine(0x102)
    r = step_line(m, table.lines, over=True)
    assert (r.reason, r.pos.line, m.pc) == ("line", 3, 0x105)


def test_into_enters_a_function_with_source(table):
    m = FakeMachine(0x102)
    r = step_line(m, table.lines, over=False)
    assert (r.reason, r.pos.file, r.pos.line, m.pc) == ("entered", "src/f.c", 10, 0x200)


def test_a_pushing_prologue_does_not_hide_the_lines(table):
    """The bug of the first version: after the prologue's PUSH, XSP is below where
    the step began, and line 11 was skipped as 'inside something deeper'."""
    m = FakeMachine(0x200)
    r = step_line(m, table.lines, over=True)
    assert (r.reason, r.pos.line, m.pc) == ("line", 11, 0x202)


def test_return_stops_in_the_caller(table):
    m = FakeMachine(0x102)
    step_line(m, table.lines, over=False)          # into f
    step_line(m, table.lines, over=True)           # f.c:11
    r = step_line(m, table.lines, over=True)       # pop, ret -> back in main
    assert (r.reason, r.pos.file, r.pos.line, m.pc) == ("returned", "src/main.c", 3, 0x105)


def test_step_out(table):
    m = FakeMachine(0x102)
    step_line(m, table.lines, over=False)          # into f (a real return address on the stack)
    r = step_out(m, table.lines)
    assert (r.reason, r.pos.line, m.pc) == ("returned", 3, 0x105)


def test_one_line_loop_stops_once_per_turn(table):
    m = FakeMachine(0x105)
    r = step_line(m, table.lines, over=True)
    assert (r.reason, r.pos.line, m.pc) == ("line", 3, 0x105)
    assert r.ran == 2


def test_a_user_breakpoint_inside_a_call_stops_step_over(table):
    m = FakeMachine(0x102)
    r = step_line(m, table.lines, over=True, user_breakpoints=[0x202])
    assert (r.reason, m.pc) == ("breakpoint", 0x202)
    assert m.bps == {0x202}            # the temporary return breakpoint is gone
