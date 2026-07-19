"""Debugger expression language -- conditions worth writing.

A breakpoint guard used to be ONE comparison against ONE memory address:
`4812 = 0`. No registers, no logic, no arithmetic. So "stop in this routine only
when the player is dead AND we are past scanline 100" was not expressible, and
the interesting bugs are exactly the ones that need that.

This is the evaluator. The syntax follows the debugger convention every
retro developer has already met, and it is basically C:

    a == 3 && [$4812] < 10
    pc >= $201000 && pc < $202000
    {$4a00} > 0x100 || fz
    [_player_hp] == 0            # symbol names, when a .map is loaded

  numbers      42   $2A   0x2A   %101010
  registers    xwa xbc xde xhl xix xiy xiz xsp   (32-bit)
               wa bc de hl ix iy iz sp           (low 16)
               w a b c d e h l                   (bytes)
               pc  sp  flags
  flags        fs fz fh fv fn fc   -- 0 or 1
  raster       scanline  frame
  memory       [addr] 1 byte   {addr} 2 bytes   [addr,n] n bytes (1..4)
  operators    + - * / %  & | ^ ~ << >>  == != < > <= >=  && || !  ( )

Safety: the text is translated to Python and parsed with `ast`, then every node
is checked against a whitelist before it is allowed to compile. No attribute
access, no calls other than the memory readers, no names other than the ones
below. A debugger condition is typed by the user, but it still must not be a way
to execute arbitrary code.
"""

from __future__ import annotations

import ast
import re

# Register name -> (index into the 8 general registers, byte width, byte offset).
# The TLCS-900 general registers are 32-bit XWA..XSP; the 16- and 8-bit names are
# windows onto the same storage, which is why they are all one table.
_XREGS = ("xwa", "xbc", "xde", "xhl", "xix", "xiy", "xiz", "xsp")
_WREGS = ("wa", "bc", "de", "hl", "ix", "iy", "iz", "sp")
# The byte halves, in the order the architecture names them: XWA holds W:A, so
# `a` is the LOW byte and `w` the high one. Same shape for BC (b:c) and DE (d:e).
_BREGS = {"a": (0, 0), "w": (0, 1), "c": (1, 0), "b": (1, 1),
          "e": (2, 0), "d": (2, 1), "l": (3, 0), "h": (3, 1)}

_FLAGS = {"fs": 7, "fz": 6, "fh": 4, "fv": 2, "fn": 1, "fc": 0}

# Identifiers, but NOT the letters inside a numeric literal. Without the lookbehind
# this matches the "x2A" of "0x2A" and reports it as an unknown symbol.
_IDENT_RE = re.compile(r"(?<![0-9A-Za-z_.])[A-Za-z_][A-Za-z_0-9.]*")


class ExprError(ValueError):
    """The expression is not valid. The message is meant to be shown to the user."""


# ---------------------------------------------------------------- translation
def _translate(src: str) -> str:
    """Rewrite the debugger syntax into something `ast` can parse.

    Order matters: `!=` must survive the `!` -> `not` rewrite, and `$`/`%`
    literals must be converted before identifiers are scanned.
    """
    out = []
    i = 0
    n = len(src)
    while i < n:
        ch = src[i]
        # $2A / %1010 numeric literals
        if ch == "$":
            j = i + 1
            while j < n and src[j] in "0123456789abcdefABCDEF":
                j += 1
            if j == i + 1:
                raise ExprError("'$' must be followed by hex digits")
            out.append("0x" + src[i + 1:j]); i = j; continue
        if ch == "%" and i + 1 < n and src[i + 1] in "01":
            j = i + 1
            while j < n and src[j] in "01":
                j += 1
            out.append("0b" + src[i + 1:j]); i = j; continue
        # && || ! (but not !=)
        if src.startswith("&&", i):
            out.append(" and "); i += 2; continue
        if src.startswith("||", i):
            out.append(" or "); i += 2; continue
        if ch == "!" and not src.startswith("!=", i):
            out.append(" not "); i += 1; continue
        # = that is not == / != / <= / >= : accept a single = as equality, because
        # every existing saved condition uses it and retyping them all would be rude.
        if ch == "=" and not src.startswith("==", i) and (i == 0 or src[i - 1] not in "!<>="):
            if i + 1 < n and src[i + 1] == "=":
                out.append("=="); i += 2; continue
            out.append("=="); i += 1; continue
        # memory: [addr] -> _r(addr,1)   [addr,n] -> _r(addr,n)   {addr} -> _r(addr,2)
        if ch == "[":
            body, i = _take_bracket(src, i, "[", "]")
            if "," in body:
                addr, _, size = body.rpartition(",")
                out.append(f"_r({_translate(addr)},{_translate(size)})")
            else:
                out.append(f"_r({_translate(body)},1)")
            continue
        if ch == "{":
            body, i = _take_bracket(src, i, "{", "}")
            out.append(f"_r({_translate(body)},2)")
            continue
        out.append(ch); i += 1
    # `ast.parse(mode="eval")` rejects leading whitespace as an indent, and the `!`
    # rewrite emits " not " -- so "!fc" would become " not fc" and fail to parse.
    return "".join(out).strip()


def _take_bracket(src: str, i: int, open_c: str, close_c: str) -> tuple[str, int]:
    depth = 0
    j = i
    while j < len(src):
        if src[j] == open_c:
            depth += 1
        elif src[j] == close_c:
            depth -= 1
            if depth == 0:
                return src[i + 1:j], j + 1
        j += 1
    raise ExprError(f"unbalanced '{open_c}'")


# ------------------------------------------------------------------ whitelist
_OK_NODES = (
    ast.Expression, ast.BoolOp, ast.BinOp, ast.UnaryOp, ast.Compare, ast.Call,
    ast.Name, ast.Load, ast.Constant, ast.And, ast.Or, ast.Not, ast.USub, ast.UAdd,
    ast.Invert, ast.Add, ast.Sub, ast.Mult, ast.Div, ast.FloorDiv, ast.Mod,
    ast.BitAnd, ast.BitOr, ast.BitXor, ast.LShift, ast.RShift,
    ast.Eq, ast.NotEq, ast.Lt, ast.LtE, ast.Gt, ast.GtE, ast.IfExp,
)


def _check(tree: ast.AST, allowed_names: set[str]) -> None:
    for node in ast.walk(tree):
        if not isinstance(node, _OK_NODES):
            raise ExprError(f"not allowed here: {type(node).__name__}")
        if isinstance(node, ast.Call):
            # The ONLY callable is the memory reader the translator emits.
            if not (isinstance(node.func, ast.Name) and node.func.id == "_r"):
                raise ExprError("function calls are not allowed")
        if isinstance(node, ast.Name) and node.id not in allowed_names:
            raise ExprError(f"unknown name '{node.id}'")


class Expression:
    """A compiled debugger expression. Build once, evaluate every hit."""

    def __init__(self, source: str, symbols=None) -> None:
        self.source = source
        self._symbols = symbols
        self._extra: dict[str, int] = {}     # symbol names referenced by this expression

        text = _translate(source)
        # Resolve identifiers that are neither registers nor keywords: they must be
        # symbols from the .map, and they are frozen into the expression at compile
        # time (a map does not change while the game runs).
        names = set(_IDENT_RE.findall(text))
        builtin = set(_XREGS) | set(_WREGS) | set(_BREGS) | set(_FLAGS) | {
            "pc", "sp", "flags", "scanline", "frame", "_r", "and", "or", "not",
            "True", "False"}
        for name in names:
            if name in builtin:
                continue
            addr = self._resolve_symbol(name)
            if addr is None:
                raise ExprError(f"unknown name '{name}'")
            self._extra[name] = addr

        try:
            tree = ast.parse(text, mode="eval")
        except SyntaxError as exc:
            raise ExprError(f"syntax error: {exc.msg}") from None
        _check(tree, builtin | set(self._extra))
        self._code = compile(tree, "<debug-expression>", "eval")

    def _resolve_symbol(self, name: str) -> int | None:
        if self._symbols is None:
            return None
        sym = self._symbols.lookup_name(name)
        if sym is None and not name.startswith("_"):
            sym = self._symbols.lookup_name("_" + name)   # t900ld prefixes with _
        return sym.address if sym is not None else None

    def evaluate(self, m) -> int:
        """Evaluate against a live machine. Raises ExprError if the machine cannot
        answer (a bad address, mostly) so the caller can surface it rather than
        silently deciding the condition is true."""
        cpu = m.cpu()
        regs = list(cpu.regs)

        def _r(addr, size=1):
            size = max(1, min(4, int(size)))
            return int.from_bytes(m.read(int(addr) & 0xFFFFFF, size), "little")

        env: dict[str, object] = {"_r": _r, "__builtins__": {}}
        for i, name in enumerate(_XREGS):
            env[name] = regs[i] & 0xFFFFFFFF
        for i, name in enumerate(_WREGS):
            env[name] = regs[i] & 0xFFFF
        for name, (idx, off) in _BREGS.items():
            env[name] = (regs[idx] >> (8 * off)) & 0xFF
        for name, bit in _FLAGS.items():
            env[name] = (cpu.flags >> bit) & 1
        env["pc"] = cpu.pc & 0xFFFFFF
        env["sp"] = regs[7] & 0xFFFF
        env["flags"] = cpu.flags & 0xFF
        # Raster position, so a condition can say "only during the HUD split".
        env["scanline"] = getattr(m, "scanline", lambda: 0)() if callable(
            getattr(m, "scanline", None)) else 0
        env["frame"] = 0
        env.update(self._extra)
        try:
            return eval(self._code, env)      # noqa: S307 - whitelisted AST, no builtins
        except ExprError:
            raise
        except Exception as exc:
            raise ExprError(f"{type(exc).__name__}: {exc}") from None

    def is_true(self, m) -> bool:
        return bool(self.evaluate(m))


def compile_expr(source: str, symbols=None) -> Expression:
    return Expression(source, symbols)


def check(source: str, symbols=None) -> str:
    """Validate without a machine. Returns '' when fine, else the error message.
    The UI uses this to say WHY a condition was rejected instead of accepting it
    and quietly firing on every hit."""
    try:
        compile_expr(source, symbols)
        return ""
    except ExprError as exc:
        return str(exc)
