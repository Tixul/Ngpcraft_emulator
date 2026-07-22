# Translating NgpCraft Emulator

Every string the player sees lives in `lang/<code>.json` — one file per language,
plain JSON, no code. Adding a language is adding a file: the emulator globs that
folder at start-up, so a new file shows up in **Settings ▸ Language** on its own.

You do not need to know Python, and you do not need to finish: anything you leave
out falls back to English, key by key. A half-done translation is mergeable.

## Adding a language

1. Copy `lang/en.json` to `lang/<code>.json`, where `<code>` is the ISO 639-1 code
   of the language (`pt` for Portuguese, `de` for German, `pt-br` if you really
   need to split a regional variant).
2. Set `"@name"` to the language's name **in that language** — `"Português"`, not
   `"Portuguese"`. It is what the language menu shows.
3. Translate the values on the right of the colons. Never touch the keys on the
   left, and never translate them.
4. Check your file:

   ```
   python tools/i18n_check.py pt
   ```

5. Open a pull request with just that file — or simply send it in, it goes in the
   repo as-is. Languages ship with the app, so the next build has yours in the menu.

## The rules that actually matter

**Keep every `{placeholder}` exactly as it is.** `"Slot {n}"` → `"Espaço {n}"`.
The app substitutes real values into those braces at runtime, so renaming `{n}`
to `{numero}` or dropping it does not produce an odd sentence — it *crashes the
app* the moment the string is shown. This is the one mistake the checker treats
as an error, and it is the only real trap in the file. You may move a placeholder
inside the sentence, and you should when the word order asks for it.

**Keys starting with `@` are not UI text.** `"@name"` is the menu label (translate
it). `"@section:*"` entries are just headings that group the file so you can see
what a string is used for; they stay English and are never displayed. Leave them
where they are.

**`"@credit"` is yours.** Put your name or handle there and it travels with the
file — through forks, copies, and rewrites of the README. You are credited in the
README too, and as a co-author of the commit that brings your file in, so the
contribution counts on your GitHub profile. Say so if you would rather not be
named: anonymous is a fine answer and the translation is just as welcome.

**Keep `\n` where it is.** It is a line break in a message box or an overlay; the
surrounding lines were sized around it.

**Length is a real constraint.** Most of these strings sit in buttons, tab labels
and a 160-pixel-wide card subtitle. If your language needs twice the characters,
prefer a shorter wording over a correct-but-clipped one — `library`, `settings`,
the `view_*`, `sort_*` and `hkn_*` families are the tight ones.

**Keys ending in `_hint` are paragraphs**, shown under a setting to explain what
it does. Translate the meaning, not the words; these are the only long ones.

**Untranslated is fine, wrong is not.** Deleting a key you are unsure about is
safe: English shows instead. Inventing a key that does not exist in `en.json` is
not — the real key then stays untranslated forever and nobody notices, so the
checker rejects it.

## What the checker tells you

```
python tools/i18n_check.py          # all languages
python tools/i18n_check.py pt       # just yours
```

- `ERROR placeholders […] in en, […] here` — fix this, it is a runtime crash.
- `ERROR not a key of en.json` — a typo in a key name; copy it from `en.json`.
- `note N untranslated` — informational, shows how complete you are. Not a
  failure; you can merge and finish later.

The same checks run in the test suite (`tests/test_i18n.py`) against whatever
languages the repo ships, so a PR that adds `lang/pt.json` is automatically
covered without touching any test.

## Scope: what is *not* translated

The **debug tools** (`ngpc_debug.py` — disassembler, memory viewer, VRAM and
audio panels) are English only, on purpose. They are developer instruments whose
vocabulary is the hardware's own (`PC`, `opcode`, `VRAM`, `wait-state`), the terms
appear that way in the NGPC documentation, and translating them would make the
panels harder to match against the specs, not easier. The player-facing shell —
library, settings, in-game menu, on-screen messages — is fully translated.

## For maintainers: adding a new string

Add the key to `lang/en.json`, in the section it belongs to, and use it through
`cfg.tr(lang, "your_key")`. Do not add it to the other languages: leaving it out
is what makes the checker report the language as incomplete, and English shows in
the meantime. Never build a user-visible sentence by concatenation or by branching
on the language code — that is exactly what this layout replaced.
