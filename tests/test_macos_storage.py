"""Synthetic import transactions only: never read a user's ROMs or preferences."""
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

pytest.importorskip('PyQt6.QtCore')
import ngpc_migration as migration
import ngpc_paths as paths


def write(root, name, data=b'fixture'):
    file = root / name
    file.parent.mkdir(parents=True, exist_ok=True)
    file.write_bytes(data)
    return file


@pytest.fixture
def legacy(tmp_path):
    root = tmp_path / 'Old.app/Contents/MacOS'
    game = write(root, 'roms/日本語.ngc', b'embedded-flash-do-not-change')
    write(root, 'saves/日本語.flash', b'sidecar')
    write(root, 'savestates/日本語.s0', b'state')
    write(root, 'watches/日本語.breaks.json', b'[]')
    write(root, 'saves/system.ram', b'coin cell')
    write(root, 'saves/system.rtc', b'clock')
    write(root, 'screenshots/capture.gif', b'GIF')
    write(root, 'bios.bin', b'fake BIOS fixture')
    write(root, 'library.json', json.dumps({'games': {str(game): {'plays': 5}}}).encode())
    write(root, 'config.ini', f'[paths]\nrom_folder={root / "roms"}\nbios={root / "bios.bin"}\n'.encode())
    tag = hashlib.md5(str(game).encode()).hexdigest()[:8]
    write(root, f'covers/日本語.{tag}.png', b'art')
    return root


def test_preview_is_read_only_and_apply_preserves_original_and_backup(legacy, tmp_path):
    base = tmp_path / 'data'
    before = migration.inventory(legacy)
    plan = migration.preview(legacy, base)
    assert not base.exists()
    assert not plan.conflicts
    with pytest.raises(migration.MigrationError, match='confirmation'):
        migration.execute(plan)
    result = migration.execute(plan, confirmed=True)
    target = base / 'profiles' / plan.profile
    assert migration.inventory(legacy) == before
    assert migration.inventory(Path(result['backup'])) == before
    assert (target / 'roms/日本語.ngc').read_bytes() == b'embedded-flash-do-not-change'
    key = paths.rom_key(target / 'roms/日本語.ngc', use_managed=True)
    assert (target / f'saves/{key}.flash').read_bytes() == b'sidecar'
    assert (target / f'savestates/{key}.s0').read_bytes() == b'state'
    assert (target / f'watches/{key}.breaks.json').exists()
    assert str(target / 'roms/日本語.ngc') in json.loads((target / 'library.json').read_text())['games']
    from PyQt6.QtCore import QSettings
    ini = QSettings(str(target / 'config.ini'), QSettings.Format.IniFormat)
    assert ini.value('paths/bios') == str(target / 'bios.bin')
    assert migration.execute(migration.preview(legacy, base), confirmed=True)['state'] == 'already-imported'
    assert migration.recover(base)[0]['state'] == 'committed'


@pytest.mark.parametrize('phase', ['planned', 'backed-up', 'verified', 'ready'])
def test_interruption_retains_selection_and_can_retry(legacy, tmp_path, phase):
    base = tmp_path / 'data'
    old = paths.initialize_empty(base)
    write(old, 'saves/previous.flash', b'previous')
    marker = paths.active_bytes(base)
    before = migration.inventory(legacy)
    plan = migration.preview(legacy, base)
    def interrupt(actual):
        if actual == phase:
            raise OSError('simulated process interruption')
    with pytest.raises(OSError, match='interruption'):
        migration.execute(plan, confirmed=True, checkpoint=interrupt)
    assert paths.active_bytes(base) == marker
    assert migration.inventory(legacy) == before
    assert migration.recover(base)[0]['state'] == 'unactivated'
    result = migration.execute(plan, confirmed=True)
    assert result['state'] == ('resumed' if phase == 'ready' else 'imported')
    assert (old / 'saves/previous.flash').read_bytes() == b'previous'


def test_no_blank_fallback_after_failed_first_import(legacy, tmp_path):
    base = tmp_path / 'data'
    def fail(_):
        raise OSError('failure')
    with pytest.raises(OSError):
        migration.execute(migration.preview(legacy, base), confirmed=True, checkpoint=fail)
    assert paths.read_active(base) is None
    with pytest.raises(paths.DataPathError, match='recovery'):
        paths.initialize_empty(base)


def test_modified_source_and_changed_selection_require_new_preview(legacy, tmp_path):
    base = tmp_path / 'data'
    plan = migration.preview(legacy, base)
    write(legacy, 'bios.bin', b'changed')
    with pytest.raises(migration.MigrationError, match='changed'):
        migration.execute(plan, confirmed=True)
    assert not base.exists()
    plan = migration.preview(legacy, base)
    paths.initialize_empty(base)
    with pytest.raises(migration.MigrationError, match='active profile changed'):
        migration.execute(plan, confirmed=True)


def test_disk_full_and_copy_error_do_not_activate(legacy, tmp_path, monkeypatch):
    base = tmp_path / 'data'
    plan = migration.preview(legacy, base)
    monkeypatch.setattr(migration.shutil, 'disk_usage', lambda _: SimpleNamespace(free=0))
    with pytest.raises(migration.MigrationError, match='space'):
        migration.execute(plan, confirmed=True)
    assert paths.read_active(base) is None
    monkeypatch.undo()
    def fail(*_):
        raise OSError('ENOSPC during copy')
    monkeypatch.setattr(migration, '_copy', fail)
    with pytest.raises(OSError, match='ENOSPC'):
        migration.execute(plan, confirmed=True)
    assert paths.read_active(base) is None
    assert (legacy / 'saves/日本語.flash').read_bytes() == b'sidecar'


def test_ambiguous_names_need_explicit_bindings(tmp_path):
    root, base = tmp_path / 'old', tmp_path / 'data'
    a = write(root, 'roms/a/Game.ngc')
    b = write(root, 'roms/b/Game.ngc')
    write(root, 'saves/Game.flash', b'valuable')
    plan = migration.preview(root, base)
    assert plan.conflicts
    with pytest.raises(migration.MigrationError):
        migration.execute(plan, confirmed=True)
    plan = migration.preview(root, base, {'saves/Game.flash': str(a)})
    assert not plan.conflicts
    migration.execute(plan, confirmed=True)
    target = base / 'profiles' / plan.profile
    assert paths.rom_key(target / a.relative_to(root), use_managed=True) != paths.rom_key(target / b.relative_to(root), use_managed=True)
    assert len(list((target / 'saves').glob('*.flash'))) == 1


def test_external_rom_and_bios_are_never_read_or_changed(tmp_path):
    root, base = tmp_path / 'old', tmp_path / 'data'
    game, bios = write(tmp_path, 'external/Game.ngc', b'embedded'), write(tmp_path, 'external/bios.bin', b'BIOS')
    write(root, 'library.json', json.dumps({'games': {str(game): {}}}).encode())
    write(root, 'config.ini', f'[paths]\nbios={bios}\n'.encode())
    write(root, 'saves/Game.flash', b'backup')
    plan = migration.preview(root, base)
    result = migration.execute(plan, confirmed=True)
    assert game.read_bytes() == b'embedded' and bios.read_bytes() == b'BIOS'
    assert not list(Path(result['backup']).glob('roms/*'))
    assert str(game) not in plan.path_map


def test_moved_backup_requires_declared_origin(legacy, tmp_path):
    moved = tmp_path / 'Backup.app/Contents/MacOS'
    moved.parent.mkdir(parents=True)
    legacy.rename(moved)
    base = tmp_path / 'data'
    assert migration.preview(moved, base).conflicts
    plan = migration.preview(moved, base, origin=legacy)
    assert not plan.conflicts
    migration.execute(plan, confirmed=True)
    games = json.loads((base / 'profiles' / plan.profile / 'library.json').read_text())['games']
    assert list(games) == [str(base / 'profiles' / plan.profile / 'roms/日本語.ngc')]


def test_corrupt_destination_is_never_overwritten(legacy, tmp_path):
    base = tmp_path / 'data'
    plan = migration.preview(legacy, base)
    migration.execute(plan, confirmed=True)
    target = base / 'profiles' / plan.profile
    write(target, 'roms/日本語.ngc', b'new-progress')
    with pytest.raises(migration.MigrationError, match='not be overwritten'):
        migration.execute(migration.preview(legacy, base), confirmed=True)
    assert (target / 'roms/日本語.ngc').read_bytes() == b'new-progress'


def test_rollback_snapshots_new_progress_and_keeps_every_profile(legacy, tmp_path):
    base = tmp_path / 'data'
    previous = paths.initialize_empty(base)
    write(previous, 'saves/old.flash', b'previous')
    marker = paths.active_bytes(base)
    result = migration.execute(migration.preview(legacy, base), confirmed=True)
    target = base / 'profiles' / result['profile']
    write(target, 'saves/new.flash', b'new progress')
    result = migration.rollback(base, result['transaction'], confirmed=True)
    assert paths.active_bytes(base) == marker
    assert (Path(result['new_progress_backup']) / 'saves/new.flash').read_bytes() == b'new progress'
    assert (target / 'saves/new.flash').exists()
    assert (previous / 'saves/old.flash').exists()


def test_first_import_rollback_cannot_select_blank_data(legacy, tmp_path):
    base = tmp_path / 'data'
    result = migration.execute(migration.preview(legacy, base), confirmed=True)
    with pytest.raises(migration.MigrationError, match='No previous'):
        migration.rollback(base, result['transaction'], confirmed=True)
    assert paths.read_active(base)['profile'] == result['profile']


def test_invalid_markers_and_symlinks_fail_closed(tmp_path, legacy):
    base = tmp_path / 'data'
    base.mkdir()
    write(base, paths.ACTIVE, b'{broken')
    with pytest.raises(paths.DataPathError):
        paths.read_active(base)
    (base / paths.ACTIVE).unlink()
    with pytest.raises(paths.DataPathError, match='outside'):
        paths.validate_base(tmp_path / 'App.app/data')
    with pytest.raises(paths.DataPathError, match='absolute'):
        paths.validate_base(Path('relative'))


def test_shared_writer_lock_prevents_migration(legacy, tmp_path):
    base = tmp_path / 'data'
    plan = migration.preview(legacy, base)
    with paths.data_lock(base):
        with pytest.raises(paths.DataPathError, match='in use'):
            migration.execute(plan, confirmed=True)
    assert paths.read_active(base) is None


def test_managed_settings_default_and_path_compatibility(legacy, tmp_path, monkeypatch):
    import ngpc_settings as cfg
    base = tmp_path / 'data'
    monkeypatch.setenv(paths.DATA_ENV, str(base))
    monkeypatch.delenv(cfg.SETTINGS_FILE_ENV)
    assert cfg.portable_mode()
    plan = migration.preview(legacy, base)
    migration.execute(plan, confirmed=True)
    settings = cfg.make_settings()
    assert cfg.bios_path(settings) == str(paths.data_root() / 'bios.bin')
    assert cfg.save_mode(settings) == cfg.SAVE_SIDECAR
    settings.setValue('general/save_mode', cfg.SAVE_ROM)
    assert cfg.save_mode(settings) == cfg.SAVE_ROM
    # Native settings can retain their existing path strings: access-time mapping only.
    assert paths.remap_legacy_path(str(legacy / 'roms')) == str(paths.data_root() / 'roms')
    assert paths.remap_legacy_path('/external/unchanged') == '/external/unchanged'
    cfg.set_portable(False)
    assert Path(cfg.config_path()).is_file()


def test_readonly_source_is_preserved_but_new_copy_is_writable(legacy, tmp_path):
    import os
    import stat
    for file in legacy.rglob('*'):
        if file.is_file():
            file.chmod(stat.S_IRUSR)
    before = migration.inventory(legacy)
    plan = migration.preview(legacy, tmp_path / 'data')
    result = migration.execute(plan, confirmed=True)
    target = Path(plan.base) / 'profiles' / result['profile']
    assert migration.inventory(legacy) == before
    assert (target / 'roms/日本語.ngc').stat().st_mode & stat.S_IWUSR
    if os.name != 'nt':
        assert not (legacy / 'roms/日本語.ngc').stat().st_mode & stat.S_IWUSR
        assert not (Path(result['backup']) / 'roms/日本語.ngc').stat().st_mode & stat.S_IWUSR


def test_relocating_profile_fails_visibly_instead_of_using_wrong_save_keys(tmp_path):
    base = tmp_path / 'data'
    paths.initialize_empty(base)
    moved = tmp_path / 'moved'
    base.rename(moved)
    with pytest.raises(paths.DataPathError, match='refusing'):
        paths.read_active(moved)


def test_managed_save_paths_are_separate_for_same_named_external_roms(tmp_path, monkeypatch):
    import core.native_session as ns
    monkeypatch.setenv(paths.DATA_ENV, str(tmp_path / 'data'))
    monkeypatch.setattr(ns, 'SAVE_DIR', tmp_path / 'saves')
    a, b = tmp_path / 'a/Game.ngc', tmp_path / 'b/Game.ngc'
    assert ns.default_save_path(a) != ns.default_save_path(b)
    write(tmp_path, 'saves/Game.flash', b'unassigned')
    with pytest.raises(RuntimeError, match='unassigned'):
        ns.default_save_path(a)


def test_symlinked_user_data_is_not_followed(tmp_path, legacy):
    try:
        (legacy / 'saves/link').symlink_to(tmp_path / 'elsewhere')
    except OSError:
        pytest.skip('This account cannot create symlinks')
    with pytest.raises(migration.MigrationError, match='Symlink'):
        migration.preview(legacy, tmp_path / 'data')


def test_source_changed_during_copy_never_activates(legacy, tmp_path):
    base = tmp_path / 'data'
    plan = migration.preview(legacy, base)
    def change(phase):
        if phase == 'backed-up':
            write(legacy, 'saves/system.ram', b'new save from old app')
    with pytest.raises(migration.MigrationError, match='during migration'):
        migration.execute(plan, confirmed=True, checkpoint=change)
    assert paths.read_active(base) is None
    assert migration.recover(base)[0]['state'] == 'unactivated'


def test_corrupted_backup_prevents_resume(legacy, tmp_path):
    base = tmp_path / 'data'
    plan = migration.preview(legacy, base)
    def stop(phase):
        if phase == 'ready':
            raise OSError('interruption')
    with pytest.raises(OSError):
        migration.execute(plan, confirmed=True, checkpoint=stop)
    info = migration.recover(base)[0]
    write(Path(info['backup']), 'saves/system.ram', b'corruption')
    with pytest.raises(migration.MigrationError, match='backup'):
        migration.execute(plan, confirmed=True)
    assert paths.read_active(base) is None


def test_atomic_activation_failure_keeps_previous_profile(legacy, tmp_path, monkeypatch):
    base = tmp_path / 'data'
    paths.initialize_empty(base)
    marker = paths.active_bytes(base)
    plan = migration.preview(legacy, base)
    atomic = paths.atomic_json
    def fail(file, value):
        if file == base / paths.ACTIVE:
            raise OSError('activation failed')
        atomic(file, value)
    monkeypatch.setattr(paths, 'atomic_json', fail)
    with pytest.raises(OSError, match='activation'):
        migration.execute(plan, confirmed=True)
    assert paths.active_bytes(base) == marker


def test_failed_rollback_copy_keeps_current_profile(legacy, tmp_path, monkeypatch):
    base = tmp_path / 'data'
    paths.initialize_empty(base)
    result = migration.execute(migration.preview(legacy, base), confirmed=True)
    marker = paths.active_bytes(base)
    def fail(*args):
        raise OSError('snapshot failed')
    monkeypatch.setattr(migration, '_copy', fail)
    with pytest.raises(OSError, match='snapshot'):
        migration.rollback(base, result['transaction'], confirmed=True)
    assert paths.active_bytes(base) == marker


def test_startup_cancel_never_creates_blank_data(tmp_path, monkeypatch):
    import ngpc_storage_ui as ui
    base = tmp_path / 'data'
    monkeypatch.setenv(paths.DATA_ENV, str(base))
    monkeypatch.setattr(paths, 'install_root', lambda: tmp_path / 'clean-app')
    class CancelBox:
        ButtonRole = ui.QMessageBox.ButtonRole
        StandardButton = ui.QMessageBox.StandardButton
        def setWindowTitle(self, value): pass
        def setText(self, value): pass
        def setDefaultButton(self, value): pass
        def addButton(self, *args): return object()
        def exec(self): return 0
        def clickedButton(self): return None
    monkeypatch.setattr(ui, 'QMessageBox', CancelBox)
    assert ui.prepare() is False
    assert not base.exists()


def test_imported_save_round_trip_uses_native_persistence(tmp_path, monkeypatch):
    from core import native, flash_file
    import core.native_session as ns
    if not native.available():
        pytest.skip('Native core not built')
    root, base = tmp_path / 'old', tmp_path / 'data'
    rom = bytearray(b'\xff' * 0x100000)
    rom[:28] = b' LICENSED BY SNK CORPORATION'
    rom[0x1c:0x20] = (0x4000).to_bytes(4, 'little')
    rom[0x23] = 0x10
    write(root, 'roms/Game.ngc', rom)
    (root / 'saves').mkdir()
    address = 0x2f0000
    flash_file.write(root / 'saves/Game.flash', [(address, b'\xaa')])
    plan = migration.preview(root, base)
    migration.execute(plan, confirmed=True)
    monkeypatch.setenv(paths.DATA_ENV, str(base))
    profile = paths.data_root()
    monkeypatch.setattr(ns, 'SAVE_DIR', profile / 'saves')
    game = profile / 'roms/Game.ngc'
    original = game.read_bytes()
    with ns.NativeSession(game, save_to_rom=False, sidecar=True, flash_size=len(rom)) as session:
        assert session.save_loaded
        assert session.machine.flash_peek(address, 1) == b'\xaa'
        session.machine.write(0x4000, bytes([0x68, 0xfe]))  # legal synthetic RAM loop
        state = session.machine.cpu(); state.pc = 0x4000; session.machine.set_cpu(state)
        for location, byte in [(0x205555, 0xaa), (0x202aaa, 0x55), (0x205555, 0xa0), (address, 0x88)]:
            session.machine.bus_write(location, byte)
        session.machine.run_frames(5)
        assert session.commit_save()
    assert game.read_bytes() == original
    assert (root / 'roms/Game.ngc').read_bytes() == original
    with ns.NativeSession(game, save_to_rom=False, sidecar=True, flash_size=len(rom)) as restarted:
        assert restarted.save_loaded
        assert restarted.machine.flash_peek(address, 1) == b'\x88'
