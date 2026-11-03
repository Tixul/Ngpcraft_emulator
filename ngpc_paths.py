"""Writable profiles outside macOS application bundles; no implicit migration."""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import tempfile

DATA_ENV = 'NGPCRAFT_DATA_DIR'
ACTIVE = 'active-profile.json'
PROFILE_RE = re.compile(r'[A-Za-z0-9_-]{1,80}\Z')


class DataPathError(RuntimeError):
    pass


def install_root() -> Path:
    if getattr(sys, 'frozen', False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def managed() -> bool:
    return bool(os.environ.get(DATA_ENV)) or (sys.platform == 'darwin' and
                                             bool(getattr(sys, 'frozen', False)))


def validate_base(path: Path) -> Path:
    if not path.is_absolute():
        raise DataPathError('The portable data directory must be an absolute path.')
    result = path.resolve()
    if any(p.name.lower().endswith('.app') for p in (result, *result.parents)):
        raise DataPathError('User data must be outside every .app bundle.')
    return result


def standard_base() -> Path:
    from PyQt6.QtCore import QStandardPaths
    parent = QStandardPaths.writableLocation(QStandardPaths.StandardLocation.GenericDataLocation)
    if not parent:
        raise DataPathError('Cannot locate Application Support; refusing a fallback data directory.')
    return validate_base(Path(parent) / 'NgpCraft')


def base_root() -> Path:
    override = os.environ.get(DATA_ENV)
    return validate_base(Path(override)) if override else standard_base()


def active_bytes(base: Path) -> bytes | None:
    for name in ('profiles', 'migrations', 'rollbacks'):
        if (base / name).is_symlink():
            raise DataPathError(f'Managed directory must not be a symlink: {name}')
    marker = base / ACTIVE
    if marker.is_symlink():
        raise DataPathError('The active profile marker must not be a symlink.')
    if not marker.exists():
        return None
    if marker.stat().st_size > 65536:
        raise DataPathError('Invalid active profile marker; no empty-profile fallback was used.')
    return marker.read_bytes()


def read_active(base: Path) -> dict | None:
    raw = active_bytes(base)
    if raw is None:
        return None
    return decode_active(base, raw)


def decode_active(base: Path, raw: bytes) -> dict:
    try:
        value = json.loads(raw)
        if value['schema'] != 1 or not PROFILE_RE.fullmatch(value['profile']):
            raise ValueError('invalid profile')
        if value['base'] != str(base.resolve()):
            raise ValueError('data directory moved; explicit relocation is required')
        root = base / 'profiles' / value['profile']
        if root.is_symlink() or not root.is_dir() or not root.resolve().is_relative_to(base):
            raise ValueError('missing or external profile')
    except (ValueError, TypeError, KeyError) as exc:
        raise DataPathError('Invalid or missing selected profile; refusing to start with blank data.') from exc
    return value


def remap_legacy_path(value: str) -> str:
    if not managed() or not value:
        return value
    manifest = data_root() / '.migration-manifest.json'
    if manifest.is_symlink():
        raise DataPathError('Migration metadata must not be a symlink.')
    if not manifest.exists():
        return value
    try:
        mapping = json.loads(manifest.read_text())['plan']['path_map']
        if not isinstance(mapping, dict) or any(not isinstance(k, str) or not isinstance(v, str)
                                                for k, v in mapping.items()):
            raise ValueError('invalid path map')
        if value in mapping:
            return mapping[value]
        for old in sorted(mapping, key=len, reverse=True):
            if Path(old).suffix.lower() in {'.zip', '.7z'} and value.startswith(old + os.sep):
                return mapping[old] + value[len(old):]
        return value
    except (ValueError, KeyError, TypeError) as exc:
        raise DataPathError('Invalid migration path map; no empty-path fallback was used.') from exc


def data_root() -> Path:
    if not managed():
        return install_root()
    base = base_root()
    active = read_active(base)
    return base / 'profiles' / (active['profile'] if active else 'default')


def rom_key(path: Path, *, use_managed: bool | None = None) -> str:
    if use_managed is None:
        use_managed = managed()
    if not use_managed:
        return path.stem
    tag = hashlib.sha256(str(path.resolve()).encode('utf-8', 'surrogatepass')).hexdigest()[:16]
    return f'{path.stem}.{tag}'


def fsync_dir(path: Path) -> None:
    if os.name == 'posix':
        fd = os.open(path, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


def atomic_bytes(path: Path, content: bytes) -> None:
    """Caller holds the shared data lock. Only replace explicitly owned metadata."""
    fd, name = tempfile.mkstemp(prefix='.ngpc-', dir=path.parent)
    temp = Path(name)
    try:
        with os.fdopen(fd, 'wb') as output:
            output.write(content)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temp, path)
        fsync_dir(path.parent)
    finally:
        temp.unlink(missing_ok=True)


def atomic_json(path: Path, value: dict) -> None:
    atomic_bytes(path, json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2).encode())


@contextmanager
def data_lock(base: Path):
    from PyQt6.QtCore import QLockFile
    base = validate_base(base)
    base.mkdir(parents=True, exist_ok=True, mode=0o700)
    active_bytes(base)  # Validate managed directory boundaries before creating a lock.
    lock = QLockFile(str(base / '.writer.lock'))
    lock.setStaleLockTime(0)
    if not lock.tryLock(0):
        raise DataPathError('Data is in use or not writable. Close the other instance; no fallback was used.')
    try:
        yield lock
    finally:
        lock.unlock()


def initialize_empty(base: Path) -> Path:
    """Explicit fresh-profile operation, never used as migration failure recovery."""
    with data_lock(base):
        current = read_active(base)
        if current:
            return base / 'profiles' / current['profile']
        if any((base / 'migrations').glob('*/journal.json')):
            raise DataPathError('An import needs recovery/review; refusing to initialize blank data.')
        root = base / 'profiles/default'
        # Refuse unknown pre-existing data rather than adopting it silently.
        if root.exists() and any(root.iterdir()):
            raise DataPathError('Unselected data already exists; import/inspect it before activation.')
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        probe_writable(root)
        atomic_json(base / ACTIVE, {'schema': 1, 'profile': 'default', 'base': str(base.resolve())})
        return root


def probe_writable(root: Path) -> None:
    fd, name = tempfile.mkstemp(prefix='.write-check-', dir=root)
    path = Path(name)
    try:
        with os.fdopen(fd, 'wb') as out:
            out.write(b'write/rename check')
            out.flush()
            os.fsync(out.fileno())
        moved = path.with_name(path.name + '-renamed')
        os.replace(path, moved)
        moved.unlink()
    finally:
        path.unlink(missing_ok=True)
