#!/usr/bin/env python3
"""Check the translation files in lang/ against English.

    python tools/i18n_check.py            # every language
    python tools/i18n_check.py pt         # just that one

English is the source language: every other file is compared to it. Three
things are checked, in order of how much they hurt:

  ERROR   placeholder mismatch -- "{n}" in English but not in the translation
          (or an invented one). These are `.format()`ed at runtime, so a wrong
          placeholder is a KeyError/IndexError in the user's face, not an ugly
          string. This is the one real trap of translating this app.
  ERROR   unknown key -- a key English does not have. Almost always a typo,
          and a typo means the real key stays untranslated forever, silently.
  note    missing key -- untranslated, falls back to English at runtime. Fine
          to merge; the count is there to show how complete the language is.

Exit code is non-zero only on ERRORs, so a partial translation still passes CI.
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

LANG_DIR = Path(__file__).resolve().parent.parent / "lang"
SOURCE = "en"
PLACEHOLDER = re.compile(r"\{(\w*)")          # {n}, {name}, {} -- not "{" alone


def load(path: Path) -> dict[str, str]:
    doc = json.loads(path.read_text(encoding="utf-8"))
    return {k: v for k, v in doc.items() if not k.startswith("@")}


def check(code: str, strings: dict[str, str], base: dict[str, str]) -> int:
    errors = 0
    unknown = sorted(set(strings) - set(base))
    missing = sorted(set(base) - set(strings))

    for key in unknown:
        print(f"  ERROR  {key}: not a key of {SOURCE}.json (typo?)")
        errors += 1

    for key in sorted(set(strings) & set(base)):
        want = sorted(PLACEHOLDER.findall(base[key]))
        got = sorted(PLACEHOLDER.findall(strings[key]))
        if want != got:
            print(f"  ERROR  {key}: placeholders {want or '[]'} in {SOURCE}, "
                  f"{got or '[]'} here -- this crashes at runtime")
            errors += 1

    if missing:
        shown = ", ".join(missing[:8]) + (" …" if len(missing) > 8 else "")
        print(f"  note   {len(missing)} untranslated (English will show): {shown}")

    done = len(base) - len(missing)
    print(f"  {done}/{len(base)} strings, {errors} error(s)")
    return errors


def main(argv: list[str]) -> int:
    base_path = LANG_DIR / f"{SOURCE}.json"
    if not base_path.exists():
        print(f"no {base_path}", file=sys.stderr)
        return 2
    base = load(base_path)

    wanted = [a.lower().removesuffix(".json") for a in argv[1:]]
    paths = [p for p in sorted(LANG_DIR.glob("*.json"))
             if p.stem != SOURCE and (not wanted or p.stem.lower() in wanted)]
    if wanted and not paths:
        print(f"no such language in {LANG_DIR}: {', '.join(wanted)}", file=sys.stderr)
        return 2

    total = 0
    for path in paths:
        doc = json.loads(path.read_text(encoding="utf-8"))
        print(f"{path.name}  ({doc.get('@name', '?')})")
        total += check(path.stem, load(path), base)
        print()

    if not paths:
        print(f"only {SOURCE}.json is present -- nothing to compare.")
    print("OK" if total == 0 else f"{total} error(s)")
    return 1 if total else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
