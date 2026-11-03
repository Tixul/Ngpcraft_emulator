"""Synthetic Finder/import handoff data; no user preferences or ROMs are touched."""
import hashlib
import json

import pytest

import ngpc_migration as migration
import ngpc_paths as paths
import ngpc_startup as startup


@pytest.fixture
def imported(tmp_path, monkeypatch):
    base, old = tmp_path / 'data', tmp_path / 'Old.app/Contents/MacOS'
    game = old / 'roms/Game.ngc'
    game.parent.mkdir(parents=True)
    game.write_bytes(b'synthetic embedded progress')
    monkeypatch.setenv(paths.DATA_ENV, str(base))
    plan = migration.preview(old, base)
    migration.execute(plan, confirmed=True)
    return base, game, base / 'profiles' / plan.profile / 'roms/Game.ngc'


def test_confirmed_import_replays_verified_copy_once_and_keeps_source(imported):
    base, old, copied = imported
    original = old.read_bytes()
    startup.retain_after_import(None, ['', str(old), str(old)])
    with paths.runtime_writer(paths.data_root()):
        value = startup.resume(base)
        assert value['requests'] == [{'path': str(copied), 'sha256': hashlib.sha256(original).hexdigest()}]
        # Merely inspecting or quitting before delivery must retain the request.
        assert startup.resume(base) == value
        startup.acknowledge(base, value)
        assert startup.resume(base) is None
    assert old.read_bytes() == copied.read_bytes() == original


def test_cancellation_failed_import_and_fresh_profile_never_queue(tmp_path, monkeypatch):
    base = tmp_path / 'data'
    monkeypatch.setenv(paths.DATA_ENV, str(base))
    startup.retain_after_import(None, ['missing.ngc'])
    assert not base.exists()
    paths.initialize_empty(base)
    startup.retain_after_import(None, ['missing.ngc'])
    assert not (base / startup.PENDING).exists()


def test_cancelled_import_with_existing_selection_does_not_queue(imported):
    base, old, _ = imported
    startup.retain_after_import(paths.active_bytes(base), [str(old)])
    assert not (base / startup.PENDING).exists()


@pytest.mark.parametrize('change', ['missing', 'changed', 'symlink'])
def test_changed_destination_never_opens_and_retains_request(imported, change):
    base, old, copied = imported
    startup.retain_after_import(None, [str(old)])
    before = (base / startup.PENDING).read_bytes()
    if change == 'missing':
        copied.rename(copied.with_suffix('.preserved'))
    elif change == 'changed':
        copied.write_bytes(b'different ROM')
    else:
        copied.unlink()
        copied.symlink_to(old)
    with paths.runtime_writer(paths.data_root()), pytest.raises(paths.DataPathError, match='missing or changed'):
        startup.resume(base)
    assert (base / startup.PENDING).read_bytes() == before
    assert old.read_bytes() == b'synthetic embedded progress'


def test_selection_change_and_existing_handoff_are_not_overwritten(imported):
    base, old, _ = imported
    startup.retain_after_import(None, [str(old)])
    before = (base / startup.PENDING).read_bytes()
    with pytest.raises(paths.DataPathError, match='not be overwritten'):
        startup.retain_after_import(None, [str(old)])
    marker = json.loads(paths.active_bytes(base))
    marker['transaction'] = '0' * 32
    paths.atomic_json(base / paths.ACTIVE, marker)
    with paths.runtime_writer(paths.data_root()), pytest.raises(paths.DataPathError, match='profile changed'):
        startup.resume(base)
    assert (base / startup.PENDING).read_bytes() == before


def test_external_request_is_not_remapped_or_modified(imported, tmp_path):
    base, old, copied = imported
    external = tmp_path / 'external.ngp'
    external.write_bytes(b'external embedded progress')
    startup.retain_after_import(None, [str(old), str(external)])
    with paths.runtime_writer(paths.data_root()):
        value = startup.resume(base)
        assert [e['path'] for e in value['requests']] == [str(copied), str(external)]
        startup.acknowledge(base, value)
        assert [e['path'] for e in startup.resume(base)['requests']] == [str(external)]
        startup.acknowledge(base, value)
        assert startup.resume(base) is None
    assert external.read_bytes() == b'external embedded progress'


def test_unimported_internal_path_fails_instead_of_opening_old_app(imported):
    base, old, _ = imported
    unimported = old.parent.parent / 'not-imported.ngc'
    unimported.write_bytes(b'preserve')
    with pytest.raises(paths.DataPathError, match='not imported'):
        startup.retain_after_import(None, [str(unimported)])
    assert not (base / startup.PENDING).exists()


def test_ack_failure_keeps_request_for_recovery(imported, monkeypatch):
    base, old, _ = imported
    startup.retain_after_import(None, [str(old)])
    with paths.runtime_writer(paths.data_root()):
        value = startup.resume(base)
        def fail(_):
            raise OSError('interrupted acknowledgement')
        monkeypatch.setattr(startup, '_read', fail)
        with pytest.raises(OSError, match='interrupted'):
            startup.acknowledge(base, value)
    assert (base / startup.PENDING).exists()


def test_shell_import_exit_then_restart_delivers_copied_rom_before_new_event(tmp_path, monkeypatch):
    from collections import deque
    from types import SimpleNamespace
    import sys
    import ngpc_application
    import ngpc_shell as shell
    import ngpc_single_instance as single
    import ngpc_storage_ui as ui
    base, old = tmp_path / 'data', tmp_path / 'Old.app/Contents/MacOS'
    game = old / 'roms/Game.ngc'
    game.parent.mkdir(parents=True)
    game.write_bytes(b'synthetic progress')
    new = tmp_path / 'another.ngp'
    new.write_bytes(b'other game')
    monkeypatch.setenv(paths.DATA_ENV, str(base))

    class Application:
        def __init__(self, requests):
            self.requests = deque(requests)
            self.aboutToQuit = SimpleNamespace(connect=lambda _: None)
        def processEvents(self): pass
        def queue_open(self, path): self.requests.append(path)
        def take_startup_requests(self):
            requests = list(self.requests)
            self.requests.clear()
            return requests
        def prepend_startup_requests(self, requests):
            self.requests.extendleft(reversed(requests))
        def set_open_handler(self, handler): self.handler = handler
        def exec(self):
            while self.requests:
                self.handler(self.requests.popleft())
            return 0

    app = Application([str(game)])  # Native event received before storage is ready.
    monkeypatch.setattr(ngpc_application, 'NgpCraftApplication', lambda _: app)
    monkeypatch.setattr(shell, 'APP_ICON', tmp_path / 'no-icon')
    monkeypatch.setattr(shell, '_claim_windows_taskbar_identity', lambda: None)
    monkeypatch.setattr(shell, 'stamp_taskbar_icon', lambda _: None)
    monkeypatch.setattr(sys, 'argv', ['ngpc_shell.py'])
    monkeypatch.setattr(single, 'forward', lambda *args: False)
    monkeypatch.setattr(single, 'InstanceServer', lambda *args: SimpleNamespace(
        requested=SimpleNamespace(connect=lambda _: None)))
    def import_confirmed():
        migration.execute(migration.preview(old, base), confirmed=True)
        return False  # The current process must not use its old cached data root.
    monkeypatch.setattr(ui, 'prepare', import_confirmed)
    monkeypatch.setattr(shell, 'Shell', lambda _: pytest.fail('No gameplay before import restart'))
    assert shell.main() == 0
    assert not (base / '.writer.lock').exists()
    assert (base / startup.PENDING).exists()

    app = Application([str(new)])
    root = paths.data_root()
    monkeypatch.setattr(shell, 'REPO', root)  # Re-imported constants in the new process.
    monkeypatch.setattr(ui, 'prepare', lambda: True)
    got = []
    def window(rom):
        assert rom is None
        return SimpleNamespace(_staged_exe=None, show=lambda: None,
                               open_from_another_launch=got.append)
    monkeypatch.setattr(shell, 'Shell', window)
    assert shell.main() == 0
    assert got == [str(root / 'roms/Game.ngc'), str(new)]
    assert not (base / startup.PENDING).exists()
    assert game.read_bytes() == b'synthetic progress'
