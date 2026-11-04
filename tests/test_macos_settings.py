"""Managed INI preferences must survive a real Qt/process restart."""
import os
from pathlib import Path
import subprocess
import sys

from PyQt6.QtCore import QSettings

import ngpc_paths as paths
import ngpc_settings as cfg


def test_legacy_general_settings_and_explicit_save_mode_survive_restart(tmp_path):
    base = tmp_path / 'managed'
    root = paths.initialize_empty(base)
    (root / 'config.ini').write_text(
        '[%General]\nsave_mode=both\ntheme=light\ncheck_updates=false\n'
        '[paths]\nrom_folder=/synthetic/games\n', encoding='utf-8')
    env = dict(os.environ, NGPCRAFT_DATA_DIR=str(base), QT_QPA_PLATFORM='offscreen')
    env.pop(cfg.SETTINGS_FILE_ENV, None)
    scripts = [r'''
import ngpc_settings as cfg
s = cfg.make_settings()
assert cfg.save_mode(s) == cfg.SAVE_BOTH
assert cfg.theme(s) == 'light'
assert s.value('general/check_updates', True, type=bool) is False
assert 'general/save_mode' in s.allKeys()
assert 'General/save_mode' not in s.allKeys()
s.setValue('general/theme', 'dark')
s.setValue('general/save_mode', cfg.SAVE_ROM)
s.sync()
''', r'''
import ngpc_settings as cfg
s = cfg.make_settings()
assert cfg.save_mode(s) == cfg.SAVE_ROM
assert cfg.theme(s) == 'dark'
assert s.value('general/check_updates', True, type=bool) is False
assert s.value('paths/rom_folder') == '/synthetic/games'
s.beginGroup('general')
assert s.value('theme') == 'dark'
s.setValue('theme', 'light')
s.endGroup()
s.remove('general/check_updates')
assert not s.contains('general/check_updates')
s.sync()
''', r'''
import ngpc_settings as cfg
s = cfg.make_settings()
assert cfg.save_mode(s) == cfg.SAVE_ROM
assert cfg.theme(s) == 'light'
assert not s.contains('general/check_updates')
''']
    for code in scripts:
        result = subprocess.run([sys.executable, '-c', code], env=env,
                                cwd=Path(__file__).resolve().parents[1],
                                capture_output=True, text=True, timeout=20)
        assert result.returncode == 0, result.stdout + result.stderr


def test_managed_ini_handles_same_process_export_cache_and_public_keys(tmp_path, monkeypatch):
    file = tmp_path / 'portable.ini'
    old = QSettings(str(file), QSettings.Format.IniFormat)
    old.setValue('general/theme', 'light')
    old.setValue('general/save_mode', cfg.SAVE_BOTH)
    old.sync()
    monkeypatch.setenv(cfg.SETTINGS_FILE_ENV, str(file))
    monkeypatch.setattr(paths, 'managed', lambda: True)
    s = cfg.make_settings()
    assert cfg.theme(s) == 'light' and cfg.save_mode(s) == cfg.SAVE_BOTH
    s.setValue('general/theme', 'dark')
    assert cfg.theme(s) == 'dark'
    assert s.allKeys() == ['general/save_mode', 'general/theme']
    # The exporter/native store sees the original lowercase public key names.
    exported = QSettings(str(tmp_path / 'export.ini'), QSettings.Format.IniFormat)
    for key in s.allKeys():
        exported.setValue(key, s.value(key))
    assert exported.value('general/theme') == 'dark'
    assert exported.value('general/save_mode') == cfg.SAVE_BOTH


def test_unmanaged_portable_settings_keep_original_qsettings(tmp_path, monkeypatch):
    monkeypatch.setenv(cfg.SETTINGS_FILE_ENV, str(tmp_path / 'unmanaged.ini'))
    monkeypatch.setattr(paths, 'managed', lambda: False)
    assert type(cfg.make_settings()) is QSettings
