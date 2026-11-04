"""Preview, back up and copy legacy data into an isolated profile; never delete it."""
from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import unicodedata
import uuid

import ngpc_paths as paths

DIRECTORIES = ('roms', 'saves', 'savestates', 'watches', 'covers', 'thumbnails',
               'screenshots', 'crashes', 'logs')
FILES = ('bios.bin', 'ngp_bios.bin', 'ngp_bios.ngp', 'bios_ngp.bin', 'ngpbios.bin',
         'config.ini', 'settings_backup.ini', 'settings_backup.prev.ini', 'library.json')


class MigrationError(RuntimeError):
    pass


def digest(path: Path) -> str:
    if path.is_symlink() or not stat.S_ISREG(path.stat().st_mode):
        raise MigrationError(f'Not a regular file: {path}')
    before = path.stat()
    h = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            h.update(block)
    after = path.stat()
    if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
        raise MigrationError(f'Source changed while reading: {path}')
    return h.hexdigest()


def source_root(source: Path) -> Path:
    root = source.resolve()
    if root.name.lower().endswith('.app'):
        root = root / 'Contents/MacOS'
    if not root.is_dir():
        raise MigrationError(f'Legacy data directory not found: {root}')
    return root


def inventory(root: Path) -> tuple[dict, list[str]]:
    files, directories = {}, []
    def add(path):
        if path.is_symlink():
            raise MigrationError(f'Symlink requires explicit manual handling: {path}')
        files[path.relative_to(root).as_posix()] = {'size': path.stat().st_size,
                                                   'sha256': digest(path)}
    for name in FILES:
        path = root / name
        if path.exists() or path.is_symlink():
            add(path)
    for name in DIRECTORIES:
        folder = root / name
        if not folder.exists() and not folder.is_symlink():
            continue
        if folder.is_symlink() or not folder.is_dir():
            raise MigrationError(f'Unsafe legacy directory: {folder}')
        for current, dirs, names in os.walk(folder, followlinks=False):
            current = Path(current)
            directories.append(current.relative_to(root).as_posix())
            for child in dirs:
                if (current / child).is_symlink():
                    raise MigrationError(f'Symlink requires explicit manual handling: {current / child}')
            for child in names:
                add(current / child)
    return dict(sorted(files.items())), sorted(directories)


def legacy_present(root: Path) -> bool:
    return any((root / name).exists() or (root / name).is_symlink()
               for name in (*FILES, *DIRECTORIES))


@dataclass(frozen=True)
class Plan:
    source: str
    origin: str
    base: str
    profile: str
    files: dict
    directories: list[str]
    bindings: dict
    renames: dict
    path_map: dict
    conflicts: list[str]
    previous: str | None

    def json(self) -> dict:
        return asdict(self)


def preview(source: Path, base: Path, bindings: dict | None = None,
            origin: Path | None = None) -> Plan:
    """Read-only: does not create a destination or inspect external ROM contents."""
    source, base = source_root(source), paths.validate_base(base)
    origin = Path(origin).resolve() if origin is not None else source
    if origin.name.lower().endswith('.app'):
        origin = origin / 'Contents/MacOS'
    if base.is_relative_to(source) or source.is_relative_to(base):
        raise MigrationError('Source and destination must not contain each other.')
    files, directories = inventory(source)
    if not files:
        raise MigrationError('No recognized user files found; no profile was created.')
    if bindings is not None and not isinstance(bindings, dict):
        raise MigrationError('ROM bindings must be a JSON object.')
    bindings = dict(bindings or {})
    if any(not isinstance(k, str) or not isinstance(v, str) or not Path(v).is_absolute()
           for k, v in bindings.items()):
        raise MigrationError('ROM bindings must map relative legacy files to absolute ROM paths.')
    identity = json.dumps([str(source), str(origin), str(base), files, directories, bindings], sort_keys=True)
    profile = 'import-' + hashlib.sha256(identity.encode()).hexdigest()[:24]
    target = base / 'profiles' / profile
    path_map = {str(source / rel): str(target / rel) for rel in (*files, *directories)}
    path_map[str(source)] = str(target)
    path_map.update({str(origin / rel): str(target / rel) for rel in (*files, *directories)})
    path_map[str(origin)] = str(target)
    known = set()
    for rel in files:
        if rel.startswith('roms/') and Path(rel).suffix.lower() in {'.ngp', '.ngc', '.zip', '.7z'}:
            known.add(str(source / rel))
    if 'library.json' in files:
        try:
            library = json.loads((source / 'library.json').read_text('utf-8'))
            games = library['games']
            if not isinstance(games, dict):
                raise ValueError('games must be an object')
            known.update(games)
        except (ValueError, KeyError, TypeError) as exc:
            raise MigrationError('Invalid library.json; refusing silent loss of history.') from exc
    def mapped(name):
        return paths.map_verified_path(name, path_map)
    renames, conflicts = {}, []
    # Do not switch a legacy folder preference to a copied folder while silently
    # leaving referenced personal files behind in an unrecognized bundle location.
    referenced = set(known)
    from PyQt6.QtCore import QSettings
    for name in ('config.ini', 'settings_backup.ini', 'settings_backup.prev.ini'):
        if name in files:
            settings = QSettings(str(source / name), QSettings.Format.IniFormat)
            for key in ('paths/rom_folder', 'paths/bios', 'paths/bios_mono', 'paths/screenshots', 'paths/filters'):
                value = settings.value(key, '', type=str)
                if value:
                    referenced.add(value)
            if settings.status() != QSettings.Status.NoError:
                raise MigrationError(f'Cannot read legacy settings: {name}')
    for value in referenced:
        candidate = str(Path(value))
        internal = any(candidate.startswith(str(root) + os.sep) for root in (source, origin))
        if internal and mapped(value) == value:
            conflicts.append(f'{value}: referenced internal data is outside the import inventory; preserve and review manually')
    for rel in files:
        path = Path(rel)
        stem, suffix = None, None
        if path.parent.as_posix() == 'saves' and path.suffix == '.flash':
            stem = path.stem
            suffix = path.suffix
        elif path.parent.as_posix() == 'savestates' and path.suffix in {f'.s{n}' for n in range(8)}:
            stem = path.stem
            suffix = path.suffix
        elif path.parent.as_posix() == 'watches' and path.suffix == '.json':
            matches = [(name, tail) for name in known for tail in
                       ('.json', '.breaks.json', '.cheats.json')
                       if path.name == Path(name).stem + tail]
            choices_by_target = {mapped(name): (name, tail) for name, tail in matches}
            if rel in bindings:
                name = bindings[rel]
                tails = [tail for tail in ('.json', '.breaks.json', '.cheats.json')
                         if path.name == Path(name).stem + tail]
                suffix = tails[0] if len(tails) == 1 else next(
                    tail for tail in ('.breaks.json', '.cheats.json', '.json') if path.name.endswith(tail))
            elif len(choices_by_target) == 1:
                name, suffix = next(iter(choices_by_target.values()))
            else:
                conflicts.append(f'{rel}: ambiguous or unknown ROM watch binding')
                continue
            renames[rel] = (path.parent / (paths.rom_key(Path(mapped(name)), use_managed=True) + suffix)).as_posix()
        if stem is not None:
            choices = list({mapped(name): name for name in known if Path(name).stem == stem}.values())
            if rel in bindings:
                choices = [bindings[rel]]
            if len(choices) != 1:
                conflicts.append(f'{rel}: requires one explicit ROM binding ({len(choices)} candidates)')
                continue
            rom = Path(mapped(choices[0]))
            renames[rel] = (path.parent / (paths.rom_key(rom, use_managed=True) + suffix)).as_posix()
    # Tagged custom covers use the old path hash; keep the actual chosen artwork.
    for old in known:
        new = mapped(old)
        if old == new:
            continue
        old_tag = hashlib.md5(old.encode('utf-8', 'surrogatepass')).hexdigest()[:8]
        new_tag = hashlib.md5(new.encode('utf-8', 'surrogatepass')).hexdigest()[:8]
        for rel in files:
            p = Path(rel)
            if p.parent.as_posix() == 'covers' and p.stem == f'{Path(old).stem}.{old_tag}':
                renames[rel] = f'covers/{Path(old).stem}.{new_tag}{p.suffix}'
    destinations = [renames.get(rel, rel) for rel in files]
    if len({unicodedata.normalize('NFC', d).casefold() for d in destinations}) != len(destinations):
        conflicts.append('Multiple source files map to the same destination; nothing will be overwritten.')
    previous = paths.active_bytes(base)
    if previous is not None:
        paths.read_active(base)  # Reject a damaged selection before constructing a plan.
    # A renamed backup is not necessarily the path encoded in its library/settings.
    # Do not guess an old install prefix just because a filename happens to match.
    for old in known:
        if mapped(old) == old and '.app/Contents/MacOS/roms/' in old.replace('\\', '/'):
            suffix = old.replace('\\', '/').split('.app/Contents/MacOS/', 1)[1]
            if suffix in files:
                conflicts.append(f'{old}: declare the original installation path before import')
    return Plan(str(source), str(origin), str(base), profile, files, directories, bindings, renames,
                path_map, conflicts, previous.decode('utf-8') if previous else None)


def _copy(source: Path, target: Path, expected: dict) -> None:
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if source.is_symlink():
        raise MigrationError(f'Source became a symlink: {source}')
    with source.open('rb') as inp, target.open('xb') as out:
        shutil.copyfileobj(inp, out, 1024 * 1024)
        out.flush()
        os.fsync(out.fileno())
    if target.stat().st_size != expected['size'] or digest(target) != expected['sha256']:
        raise MigrationError(f'Copy verification failed: {source}')
    shutil.copystat(source, target, follow_symlinks=False)


def _rewrite(stage: Path, plan: Plan) -> None:
    def mapped(value):
        return paths.map_verified_path(value, plan.path_map)
    library = stage / 'library.json'
    if library.exists():
        value = json.loads(library.read_text('utf-8'))
        games = {mapped(name): record for name, record in value['games'].items()}
        if len(games) != len(value['games']):
            raise MigrationError('Library path mapping would merge different records.')
        value['games'] = games
        paths.atomic_json(library, value)
    from PyQt6.QtCore import QSettings
    for name in ('config.ini', 'settings_backup.ini', 'settings_backup.prev.ini'):
        if not (stage / name).exists():
            continue
        settings = QSettings(str(stage / name), QSettings.Format.IniFormat)
        for key in ('paths/rom_folder', 'paths/bios', 'paths/bios_mono', 'paths/screenshots', 'paths/filters'):
            old = settings.value(key)
            new = mapped(old)
            if old != new:
                settings.setValue(key, new)
        settings.sync()
        if settings.status() != QSettings.Status.NoError:
            raise MigrationError(f'INI update failed: {name}')
    for old, new in plan.renames.items():
        if old == new:
            continue
        target = stage / new
        if target.exists():
            raise MigrationError(f'Conflicting destination: {new}')
        (stage / old).rename(target)


def _profile_files(root: Path) -> dict:
    result = {}
    for current, dirs, names in os.walk(root, followlinks=False):
        for name in (*dirs, *names):
            if (Path(current) / name).is_symlink():
                raise MigrationError('Profile contains a symlink; refusing automatic recovery/copy.')
        for name in names:
            p = Path(current) / name
            if p != root / '.migration-manifest.json':
                result[p.relative_to(root).as_posix()] = {'size': p.stat().st_size, 'sha256': digest(p)}
    return result


def _sync_tree(root: Path, *, files: bool = True) -> None:
    for current, _, names in os.walk(root, topdown=False):
        if files:
            for name in names:
                # Windows _commit requires a writable descriptor. Staged payloads
                # have already been made owner-writable; sources are never opened here.
                with (Path(current) / name).open('r+b') as stream:
                    os.fsync(stream.fileno())
        paths.fsync_dir(Path(current))


def execute(plan: Plan, *, confirmed: bool = False, checkpoint=None) -> dict:
    """Caller shows preview and confirms old instances are closed. No source writes."""
    if not confirmed:
        raise MigrationError('Preview and explicit confirmation are required.')
    if plan.conflicts:
        raise MigrationError('\n'.join(plan.conflicts))
    fresh = preview(Path(plan.source), Path(plan.base), plan.bindings, Path(plan.origin))
    if (fresh.files, fresh.directories, fresh.profile, fresh.renames, fresh.path_map) != (
            plan.files, plan.directories, plan.profile, plan.renames, plan.path_map):
        raise MigrationError('Source/plan changed. Preview again; no activation occurred.')
    base, source = Path(plan.base), Path(plan.source)
    with paths.data_lock(base):
        target = base / 'profiles' / plan.profile
        current = paths.read_active(base)
        if target.exists():
            if target.is_symlink() or (target / '.migration-manifest.json').is_symlink():
                raise MigrationError('Destination metadata must not be a symlink.')
            marker = target / '.migration-manifest.json'
            try:
                record = json.loads(marker.read_text(encoding='utf-8'))
                same = record['plan']['files'] == plan.files and _profile_files(target) == record['files']
            except (OSError, ValueError, KeyError):
                same = False
            if same and current and current['profile'] == plan.profile:
                return {'state': 'already-imported', 'profile': plan.profile}
            if same and (paths.active_bytes(base).decode() if paths.active_bytes(base) else None) == plan.previous:
                transaction = record.get('transaction', '')
                if not re_transaction(transaction):
                    raise MigrationError('Unactivated profile lacks a valid recovery transaction.')
                backup = base / 'migrations' / transaction / 'backup'
                for rel, expected in plan.files.items():
                    if digest(backup / rel) != expected['sha256']:
                        raise MigrationError('Recovery backup is incomplete or changed.')
                if inventory(source) != (plan.files, plan.directories):
                    raise MigrationError('Source changed before recovery activation.')
                paths.atomic_json(base / paths.ACTIVE, {'schema': 1, 'profile': plan.profile, 'base': str(base.resolve()),
                                                       'transaction': transaction})
                return {'state': 'resumed', 'profile': plan.profile, 'transaction': transaction}
            raise MigrationError('Destination exists or has changed; it will not be overwritten.')
        old = paths.active_bytes(base)
        if (old.decode('utf-8') if old else None) != plan.previous:
            raise MigrationError('The active profile changed after preview; preview again.')
        if shutil.disk_usage(base).free < 2 * sum(x['size'] for x in plan.files.values()) + 1024 * 1024:
            raise MigrationError('Insufficient space for verified backup and profile copy.')
        transaction = uuid.uuid4().hex
        work = base / 'migrations' / transaction
        work.mkdir(parents=True, mode=0o700)
        backup, stage = work / 'backup', work / 'stage'
        journal = {'schema': 1, 'transaction': transaction, 'plan': plan.json(), 'phase': 'planned'}
        def mark(phase):
            journal['phase'] = phase
            paths.atomic_json(work / 'journal.json', journal)
            if checkpoint:
                checkpoint(phase)
        try:
            mark('planned')
            backup.mkdir(mode=0o700)
            stage.mkdir(mode=0o700)
            for directory in plan.directories:
                (backup / directory).mkdir(parents=True, exist_ok=True)
                (stage / directory).mkdir(parents=True, exist_ok=True)
            for rel, expected in plan.files.items():
                _copy(source / rel, backup / rel, expected)
            _sync_tree(backup, files=False)  # _copy already flushed each writable output handle
            mark('backed-up')
            for rel, expected in plan.files.items():
                _copy(backup / rel, stage / rel, expected)
                # Copies must be writable even when the preserved source was on a
                # read-only installation. This never changes the source or backup.
                (stage / rel).chmod((stage / rel).stat().st_mode | stat.S_IWUSR)
            _rewrite(stage, plan)
            final_files = _profile_files(stage)
            paths.atomic_json(stage / '.migration-manifest.json', {'plan': plan.json(), 'files': final_files,
                                                                 'transaction': transaction})
            _sync_tree(stage)
            # Check the complete source again, not just individual files copied earlier.
            if inventory(source) != (plan.files, plan.directories):
                raise MigrationError('Source changed during migration; no profile was activated.')
            mark('verified')
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.exists():
                raise MigrationError('Destination appeared during import; refusing replacement.')
            stage.rename(target)
            paths.fsync_dir(target.parent)
            mark('ready')
            # Last commit point. Recovery infers committed state from this selection;
            # no later journal write is required to make activation valid.
            paths.atomic_json(base / paths.ACTIVE, {'schema': 1, 'profile': plan.profile, 'base': str(base.resolve()),
                                                   'transaction': transaction})
            return {'state': 'imported', 'profile': plan.profile, 'transaction': transaction,
                    'backup': str(backup)}
        except Exception:
            # Do not delete backups, stages, sources or other profiles. Failed imports
            # remain visible to recovery, and initialization must not create blank data.
            raise


def recover(base: Path) -> list[dict]:
    """Read-only recovery inspection; incomplete copies never become active implicitly."""
    selected = paths.read_active(base)
    results = []
    for file in sorted((base / 'migrations').glob('*/journal.json')):
        if file.is_symlink() or file.parent.is_symlink():
            raise MigrationError('Unsafe transaction journal path.')
        journal = json.loads(file.read_text(encoding='utf-8'))
        transaction = file.parent.name
        state = 'committed' if selected and selected.get('transaction') == transaction else 'unactivated'
        results.append({'transaction': transaction, 'state': state, 'phase': journal['phase'],
                        'backup': str(file.parent / 'backup'), 'source': journal['plan']['source']})
    return results


def rollback(base: Path, transaction: str, *, confirmed: bool = False) -> dict:
    """Keep new progress in a verified snapshot before restoring a previous selection."""
    if not confirmed or not re_transaction(transaction):
        raise MigrationError('A valid transaction and explicit rollback confirmation are required.')
    with paths.data_lock(base):
        journal_path = base / 'migrations' / transaction / 'journal.json'
        if journal_path.is_symlink() or journal_path.parent.is_symlink():
            raise MigrationError('Unsafe transaction journal path.')
        journal = json.loads(journal_path.read_text(encoding='utf-8'))
        selected = paths.read_active(base)
        if not selected or selected.get('transaction') != transaction:
            raise MigrationError('This transaction is not the active profile.')
        previous = journal['plan']['previous']
        if previous is None:
            raise MigrationError('No previous managed profile. Use the preserved old app; refusing an empty fallback.')
        old = paths.decode_active(base, previous.encode())
        old_root = base / 'profiles' / old['profile']
        if not old_root.is_dir() or old_root.is_symlink():
            raise MigrationError('Previous profile is missing; refusing rollback to blank data.')
        root = base / 'profiles' / selected['profile']
        snapshot = base / 'rollbacks' / uuid.uuid4().hex
        snapshot.mkdir(parents=True, mode=0o700)
        entries = _profile_files(root)
        for rel, expected in entries.items():
            _copy(root / rel, snapshot / rel, expected)
        paths.atomic_json(snapshot / 'snapshot-manifest.json', {'files': entries})
        _sync_tree(snapshot, files=False)  # copied payload/manifest handles were flushed on write
        if _profile_files(root) != entries:
            raise MigrationError('Profile changed during rollback; selection was retained.')
        paths.atomic_bytes(base / paths.ACTIVE, previous.encode())
        return {'state': 'rolled-back', 'new_progress_backup': str(snapshot)}


def re_transaction(value: str) -> bool:
    return len(value) == 32 and all(c in '0123456789abcdef' for c in value)


def main(argv=None) -> int:
    import argparse
    import sys
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    p = commands.add_parser('preview')
    p.add_argument('source', type=Path)
    p.add_argument('--data-dir', type=Path, required=True)
    p.add_argument('--origin', type=Path)
    p.add_argument('--bindings', type=Path)
    p = commands.add_parser('apply')
    p.add_argument('plan', type=Path)
    p.add_argument('--confirm', action='store_true')
    p = commands.add_parser('status')
    p.add_argument('--data-dir', type=Path, required=True)
    p = commands.add_parser('rollback')
    p.add_argument('transaction')
    p.add_argument('--data-dir', type=Path, required=True)
    p.add_argument('--confirm', action='store_true')
    args = parser.parse_args(argv)
    try:
        if args.command == 'preview':
            bindings = json.loads(args.bindings.read_text(encoding='utf-8')) if args.bindings else None
            result = preview(args.source, args.data_dir, bindings, args.origin).json()
        elif args.command == 'apply':
            result = execute(Plan(**json.loads(args.plan.read_text(encoding='utf-8'))), confirmed=args.confirm)
        elif args.command == 'rollback':
            result = rollback(paths.validate_base(args.data_dir), args.transaction, confirmed=args.confirm)
        else:
            result = recover(paths.validate_base(args.data_dir))
        print(json.dumps(result, ensure_ascii=True, indent=2))
        return 0
    except (OSError, ValueError, TypeError, MigrationError, paths.DataPathError) as exc:
        print(f'Migration stopped: {exc}', file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
