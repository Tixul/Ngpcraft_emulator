"""The lang/*.json translation files and the loader that discovers them.

These run over whatever languages the repo happens to ship: a new lang/pt.json
is picked up here with no edit to this file, which is the whole point of the
layout. They are the gate a translation PR has to pass -- above all the
placeholder check, since "{n}" going missing is a runtime crash, not a typo.
"""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

import pytest

pytest.importorskip("PyQt6")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import ngpc_library as lib  # noqa: E402
import ngpc_settings as cfg  # noqa: E402

LANG_FILES = sorted(cfg.LANG_DIR.glob("*.json"))
OTHERS = [p for p in LANG_FILES if p.stem != cfg.FALLBACK_LANG]
PLACEHOLDER = re.compile(r"\{(\w*)")


def _strings(path: Path) -> dict[str, str]:
    doc = json.loads(path.read_text(encoding="utf-8"))
    return {k: v for k, v in doc.items() if not k.startswith("@")}


# --- the loader -----------------------------------------------------------
def test_english_is_present_and_populated():
    assert cfg.FALLBACK_LANG in cfg.STRINGS, "the fallback language must load"
    assert len(cfg.STRINGS[cfg.FALLBACK_LANG]) > 100


def test_every_file_in_lang_becomes_a_menu_entry():
    assert {p.stem for p in LANG_FILES} == set(cfg.STRINGS)
    assert {c for c, _n in cfg.LANGUAGES} == set(cfg.STRINGS)
    assert cfg.LANGUAGES[0][0] == cfg.FALLBACK_LANG, "English leads the menu"
    for code, label in cfg.LANGUAGES:
        assert label and label != code, f"{code}.json needs a readable @name"


def test_metadata_keys_never_reach_the_ui():
    for code, strings in cfg.STRINGS.items():
        assert not [k for k in strings if k.startswith("@")], f"{code}: @keys leaked"


def test_a_broken_file_is_skipped_not_fatal(tmp_path):
    (tmp_path / "en.json").write_text('{"@name": "English", "library": "Library"}',
                                      encoding="utf-8")
    (tmp_path / "xx.json").write_text("{ not json", encoding="utf-8")
    table, menu = cfg.load_languages(tmp_path)
    assert "xx" not in table and "en" in table
    assert [c for c, _ in menu] == ["en"]


def test_missing_english_still_yields_a_usable_table(tmp_path):
    table, menu = cfg.load_languages(tmp_path)      # empty dir
    assert table == {cfg.FALLBACK_LANG: {}}
    assert menu and menu[0][0] == cfg.FALLBACK_LANG


def test_unknown_saved_language_falls_back(tmp_path):
    # A private .ini, so this never touches the settings the user actually plays with.
    from PyQt6.QtCore import QSettings
    s = QSettings(str(tmp_path / "t.ini"), QSettings.Format.IniFormat)
    s.setValue("general/language", "zz")     # e.g. lang/zz.json was removed
    assert cfg.language(s) == cfg.FALLBACK_LANG
    s.setValue("general/language", "fr")
    assert cfg.language(s) == "fr"


def test_tr_falls_back_key_by_key():
    """A half-translated file must show English on the rest, never a raw key."""
    assert cfg.tr("zz", "library") == cfg.STRINGS["en"]["library"]
    assert cfg.tr("en", "no_such_key_at_all") == "no_such_key_at_all"


# --- the translations themselves ------------------------------------------
@pytest.mark.parametrize("path", OTHERS, ids=[p.stem for p in OTHERS])
def test_translation_has_no_unknown_keys(path):
    extra = sorted(set(_strings(path)) - set(_strings(cfg.LANG_DIR / "en.json")))
    assert not extra, f"{path.name}: keys English does not have (typos?): {extra}"


@pytest.mark.parametrize("path", OTHERS, ids=[p.stem for p in OTHERS])
def test_translation_keeps_every_placeholder(path):
    base = _strings(cfg.LANG_DIR / "en.json")
    bad = []
    for key, value in _strings(path).items():
        if key in base and sorted(PLACEHOLDER.findall(base[key])) != \
                sorted(PLACEHOLDER.findall(value)):
            bad.append(key)
    assert not bad, f"{path.name}: placeholder drift -> runtime crash on {bad}"


@pytest.mark.parametrize("path", LANG_FILES, ids=[p.stem for p in LANG_FILES])
def test_every_shipped_string_formats(path):
    """Every value must survive the .format() the UI may run on it."""
    for key, value in _strings(path).items():
        args = {name: 0 for name in PLACEHOLDER.findall(value) if name}
        value.format(**args)


def test_the_checker_agrees_with_the_repo():
    # Imported here, not at module level: putting tools/ on sys.path at COLLECTION
    # time changes it for the whole session, and the suite is import-order
    # sensitive (see conftest.py).
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
    try:
        import i18n_check
        assert i18n_check.main(["i18n_check.py"]) == 0
    finally:
        sys.path.pop(0)


# --- the strings that used to be hard-coded -------------------------------
def test_time_units_come_from_the_table():
    assert set(cfg.time_units("en")) == set(lib.DEFAULT_UNITS)
    assert cfg.time_units("fr")["day"] == "j", "the French day suffix is a key now"


def test_bios_crash_message_is_translated():
    en = cfg.tr("en", "crash_needs_bios")
    fr = cfg.tr("fr", "crash_needs_bios")
    assert "BIOS" in en and "BIOS" in fr and en != fr
