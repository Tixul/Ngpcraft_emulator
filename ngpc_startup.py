"""Preserve confirmed-import open requests across the required process restart.

Only request metadata is written. ROMs and migration payloads are never modified.
Callers hold the managed runtime writer lock when reading/acknowledging a handoff.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import ngpc_paths as paths
from ngpc_migration import digest

PENDING = 'pending-open.json'


def _read(base: Path) -> dict | None:
    file = base / PENDING
    if file.is_symlink():
        raise paths.DataPathError(f'Open-request metadata must not be a symlink: {file}')
    if not file.exists():
        return None
    try:
        if file.stat().st_size > 262144:
            raise ValueError('oversized request')
        value = json.loads(file.read_text('utf-8'))
        entries = value['requests']
        if value['schema'] != 1 or not isinstance(entries, list) or not 1 <= len(entries) <= 256:
            raise ValueError('invalid requests')
        if value['selection'] != hashlib.sha256(paths.active_bytes(base) or b'').hexdigest():
            raise ValueError('the selected profile changed')
        for entry in entries:
            if (not isinstance(entry['path'], str) or not Path(entry['path']).is_absolute()
                    or Path(entry['path']).suffix.lower() not in {'.ngp', '.ngc', '.zip', '.7z'}
                    or not isinstance(entry['sha256'], str) or len(entry['sha256']) != 64):
                raise ValueError('invalid ROM reference')
        return value
    except (ValueError, TypeError, KeyError) as exc:
        raise paths.DataPathError(
            f'Cannot resume the imported ROM request ({exc}). No ROM was opened. '
            f'The request is retained at {file}; preserve/move it aside to cancel the handoff.') from exc


def retain_after_import(previous: bytes | None, requests: list[str]) -> None:
    """Cancellation/failure never creates a profile or a deferred open request."""
    base = paths.base_root()
    selected = paths.active_bytes(base)
    active = paths.read_active(base)
    if selected == previous or not active or not active.get('transaction'):
        return
    with paths.runtime_writer(paths.data_root()):
        if paths.active_bytes(base) != selected:
            raise paths.DataPathError('Profile changed before preserving the ROM request; restart and review it.')
        entries = []
        seen = set()
        for request in requests:
            if not request:
                continue  # Foreground-only requests need no replay after a restart.
            file = Path(paths.remap_legacy_path(request)).resolve()
            if file.suffix.lower() not in {'.ngp', '.ngc', '.zip', '.7z'}:
                raise paths.DataPathError(f'Cannot preserve this open request: {request}')
            if str(file) not in seen:
                entries.append({'path': str(file), 'sha256': digest(file)})
                seen.add(str(file))
        if not entries:
            return
        if len(entries) > 256:
            raise paths.DataPathError('Too many startup ROM requests; reopen the desired ROM after restarting.')
        file = base / PENDING
        if file.exists() or file.is_symlink():
            raise paths.DataPathError(f'A previous ROM handoff is retained at {file}; it will not be overwritten.')
        paths.atomic_json(file, {'schema': 1,
                                 'selection': hashlib.sha256(selected).hexdigest(),
                                 'requests': entries})


def resume(base: Path) -> dict | None:
    value = _read(base)
    if value:
        for entry in value['requests']:
            file = Path(entry['path'])
            if not file.is_file() or file.is_symlink() or digest(file) != entry['sha256']:
                raise paths.DataPathError(
                    f'The deferred ROM is missing or changed: {file}. Nothing was opened. '
                    f'The request is retained at {base / PENDING}; preserve/move it aside to cancel.')
    return value


def acknowledge(base: Path, value: dict) -> None:
    """Acknowledge only after the existing GUI open handler returned.

    A crash before acknowledgement retains the request for a later retry. Normal
    restarts cannot replay an already acknowledged request. Saves use the normal
    ROM-switch path, including its confirmation before ending a link session.
    """
    if _read(base) != value:
        raise paths.DataPathError('The deferred open request changed; it was not overwritten.')
    remaining = value['requests'][1:]
    if remaining:
        updated = dict(value, requests=remaining)
        paths.atomic_json(base / PENDING, updated)
        value['requests'] = remaining
    else:
        (base / PENDING).unlink()
        paths.fsync_dir(base)
        value['requests'] = []
