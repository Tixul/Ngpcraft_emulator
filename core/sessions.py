"""Named session helpers built on top of managed checkpoint frontiers."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from core.checkpoints import (
    checkpoint_root_for_rom,
    checkpoint_path_for_rom,
    delete_named_checkpoint,
    load_named_checkpoint,
    sanitize_checkpoint_name,
)
from core.savestate import SavestateDocument, compute_rom_sha256

SESSION_FORMAT = "ngpc-emu-session"
SESSION_FORMAT_VERSION = "2026-05-19.v1"


@dataclass(frozen=True)
class NamedSession:
    """One named session stored on disk."""

    name: str
    slug: str
    path: Path
    created_at_utc: str
    updated_at_utc: str
    rom_sha256: str
    current_checkpoint_name: str
    current_checkpoint_path: Path
    last_action: str | None
    note: str | None
    document: SavestateDocument


@dataclass(frozen=True)
class NamedSessionSnapshot:
    """One named snapshot captured from a session frontier."""

    session_name: str
    name: str
    slug: str
    checkpoint_name: str
    path: Path
    document: SavestateDocument


def session_root_for_rom(rom_path: Path) -> Path:
    """Return the default session directory for one ROM."""
    return rom_path.resolve().parent / ".ngpc_emu" / "sessions"


def sanitize_session_name(name: str) -> str:
    """Convert a human session name into a stable filesystem slug."""
    return sanitize_checkpoint_name(name)


def session_path_for_rom(rom_path: Path, name: str) -> Path:
    """Return the metadata path for one named session."""
    slug = sanitize_session_name(name)
    return session_root_for_rom(rom_path) / f"{rom_path.stem}.{slug}.json"


def managed_checkpoint_name_for_session(name: str) -> str:
    """Return the reserved checkpoint name used as the session frontier."""
    slug = sanitize_session_name(name)
    return f"session.{slug}.current"


def session_checkpoint_path_for_rom(rom_path: Path, name: str) -> Path:
    """Return the managed checkpoint path for one named session."""
    return checkpoint_path_for_rom(rom_path, managed_checkpoint_name_for_session(name))


def managed_snapshot_checkpoint_name_for_session(
    session_name: str,
    snapshot_name: str,
) -> str:
    """Return the reserved checkpoint name used for one session snapshot."""
    session_slug = sanitize_session_name(session_name)
    snapshot_slug = sanitize_session_name(snapshot_name)
    return f"session.{session_slug}.snapshot.{snapshot_slug}"


def session_snapshot_checkpoint_path_for_rom(
    rom_path: Path,
    session_name: str,
    snapshot_name: str,
) -> Path:
    """Return the managed checkpoint path for one session snapshot."""
    return checkpoint_path_for_rom(
        rom_path,
        managed_snapshot_checkpoint_name_for_session(session_name, snapshot_name),
    )


def build_session_payload(
    *,
    rom_path: Path,
    name: str,
    current_checkpoint_name: str | None = None,
    created_at_utc: str | None = None,
    updated_at_utc: str | None = None,
    last_action: str | None = None,
    note: str | None = None,
) -> dict[str, object]:
    """Build a v1 JSON-ready session payload."""
    slug = sanitize_session_name(name)
    checkpoint_name = current_checkpoint_name or managed_checkpoint_name_for_session(name)
    checkpoint_path = checkpoint_path_for_rom(rom_path, checkpoint_name)
    created = created_at_utc or datetime.now(timezone.utc).isoformat()
    updated = updated_at_utc or created
    return {
        "format": SESSION_FORMAT,
        "format_version": SESSION_FORMAT_VERSION,
        "created_at_utc": created,
        "updated_at_utc": updated,
        "rom": {
            "path_when_saved": str(rom_path),
            "sha256": compute_rom_sha256(rom_path),
        },
        "session": {
            "name": name,
            "slug": slug,
            "current_checkpoint_name": checkpoint_name,
            "current_checkpoint_path": str(checkpoint_path),
            "last_action": last_action,
        },
        "note": note,
    }


def save_named_session_payload(path: Path, payload: dict[str, object]) -> None:
    """Persist one session metadata payload to disk."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")


def save_named_session(
    rom_path: Path,
    name: str,
    *,
    current_checkpoint_name: str | None = None,
    last_action: str | None = None,
    note: str | None = None,
) -> NamedSession:
    """Create or update one named session metadata file."""
    path = session_path_for_rom(rom_path, name)
    created_at_utc = None
    if path.exists():
        raw = _load_raw_session_payload(path)
        _validate_schema(raw, path)
        created_at_utc = raw.get("created_at_utc")
        if created_at_utc is not None and not isinstance(created_at_utc, str):
            raise ValueError(f"Session at {path} has a non-string created_at_utc.")
    payload = build_session_payload(
        rom_path=rom_path,
        name=name,
        current_checkpoint_name=current_checkpoint_name,
        created_at_utc=created_at_utc,
        last_action=last_action,
        note=note,
    )
    save_named_session_payload(path, payload)
    return load_named_session(rom_path, name)


def load_named_session(rom_path: Path, name: str) -> NamedSession:
    """Load one named session and verify it against the given ROM."""
    path = session_path_for_rom(rom_path, name)
    if not path.exists():
        raise FileNotFoundError(str(path))

    raw = _load_raw_session_payload(path)
    _validate_schema(raw, path)

    actual_hash = compute_rom_sha256(rom_path)
    rom_section = raw["rom"]
    expected_hash = rom_section["sha256"]
    if actual_hash != expected_hash:
        raise ValueError(
            f"ROM hash mismatch: session at {path} was captured against sha256 "
            f"{expected_hash} but {rom_path} is sha256 {actual_hash}"
        )

    session_section = raw["session"]
    checkpoint_name = session_section["current_checkpoint_name"]
    checkpoint = load_named_checkpoint(rom_path, checkpoint_name)
    return NamedSession(
        name=session_section["name"],
        slug=session_section["slug"],
        path=path,
        created_at_utc=raw.get("created_at_utc", ""),
        updated_at_utc=raw.get("updated_at_utc", ""),
        rom_sha256=expected_hash,
        current_checkpoint_name=checkpoint_name,
        current_checkpoint_path=checkpoint.path,
        last_action=session_section.get("last_action"),
        note=raw.get("note"),
        document=checkpoint.document,
    )


def list_named_sessions(rom_path: Path) -> tuple[NamedSession, ...]:
    """List all named sessions currently stored for one ROM directory."""
    root = session_root_for_rom(rom_path)
    if not root.exists():
        return ()

    sessions: list[NamedSession] = []
    prefix = f"{rom_path.stem}."
    for path in sorted(root.glob("*.json")):
        if not path.name.startswith(prefix):
            continue
        slug = path.stem[len(prefix) :]
        sessions.append(load_named_session(rom_path, slug))
    return tuple(sessions)


def delete_named_session(
    rom_path: Path,
    name: str,
) -> tuple[Path, Path | None, tuple[Path, ...]]:
    """Delete one named session, its frontier, and any managed snapshots."""
    session = load_named_session(rom_path, name)
    checkpoint_path: Path | None = None
    snapshot_paths: list[Path] = []
    try:
        checkpoint_path = delete_named_checkpoint(
            rom_path, session.current_checkpoint_name
        )
    except FileNotFoundError:
        checkpoint_path = None
    for snapshot in list_named_session_snapshots(rom_path, name):
        try:
            snapshot_paths.append(
                delete_named_checkpoint(rom_path, snapshot.checkpoint_name)
            )
        except FileNotFoundError:
            continue
    session.path.unlink()
    return session.path, checkpoint_path, tuple(snapshot_paths)


def save_named_session_snapshot(
    rom_path: Path,
    session_name: str,
    snapshot_name: str,
) -> NamedSessionSnapshot:
    """Capture one named snapshot from the current session frontier."""
    session = load_named_session(rom_path, session_name)
    snapshot_path = session_snapshot_checkpoint_path_for_rom(
        rom_path, session_name, snapshot_name
    )
    snapshot_path.parent.mkdir(parents=True, exist_ok=True)
    snapshot_path.write_text(
        session.current_checkpoint_path.read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    return load_named_session_snapshot(rom_path, session_name, snapshot_name)


def load_named_session_snapshot(
    rom_path: Path,
    session_name: str,
    snapshot_name: str,
) -> NamedSessionSnapshot:
    """Load one named session snapshot."""
    checkpoint_name = managed_snapshot_checkpoint_name_for_session(
        session_name, snapshot_name
    )
    checkpoint = load_named_checkpoint(rom_path, checkpoint_name)
    return NamedSessionSnapshot(
        session_name=session_name,
        name=snapshot_name,
        slug=sanitize_session_name(snapshot_name),
        checkpoint_name=checkpoint_name,
        path=checkpoint.path,
        document=checkpoint.document,
    )


def list_named_session_snapshots(
    rom_path: Path,
    session_name: str,
) -> tuple[NamedSessionSnapshot, ...]:
    """List all snapshots currently captured for one named session."""
    root = checkpoint_root_for_rom(rom_path)
    if not root.exists():
        return ()

    snapshots: list[NamedSessionSnapshot] = []
    session_slug = sanitize_session_name(session_name)
    prefix = f"{rom_path.stem}.session.{session_slug}.snapshot."
    for path in sorted(root.glob("*.json")):
        if not path.name.startswith(prefix):
            continue
        snapshot_slug = path.stem[len(prefix) :]
        snapshots.append(
            load_named_session_snapshot(rom_path, session_name, snapshot_slug)
        )
    return tuple(snapshots)


def restore_named_session_snapshot(
    rom_path: Path,
    session_name: str,
    snapshot_name: str,
) -> NamedSession:
    """Restore one session snapshot into the current session frontier."""
    session = load_named_session(rom_path, session_name)
    snapshot = load_named_session_snapshot(rom_path, session_name, snapshot_name)
    session.current_checkpoint_path.write_text(
        snapshot.path.read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    return save_named_session(
        rom_path,
        session_name,
        current_checkpoint_name=session.current_checkpoint_name,
        last_action=f"session restore {snapshot_name}",
        note=session.note,
    )


def delete_named_session_snapshot(
    rom_path: Path,
    session_name: str,
    snapshot_name: str,
) -> Path:
    """Delete one named session snapshot and return its checkpoint path."""
    checkpoint_name = managed_snapshot_checkpoint_name_for_session(
        session_name, snapshot_name
    )
    return delete_named_checkpoint(rom_path, checkpoint_name)


def _load_raw_session_payload(path: Path) -> dict[str, object]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError(f"Session at {path} is not a JSON object.")
    return raw


def _validate_schema(raw: dict[str, object], path: Path) -> None:
    if raw.get("format") != SESSION_FORMAT:
        raise ValueError(
            f"Unexpected session format {raw.get('format')!r} at {path}; "
            f"expected {SESSION_FORMAT!r}."
        )
    if raw.get("format_version") != SESSION_FORMAT_VERSION:
        raise ValueError(
            f"Unknown session format_version {raw.get('format_version')!r} "
            f"at {path}; this build only understands "
            f"{SESSION_FORMAT_VERSION!r}."
        )
    for required in ("rom", "session"):
        if required not in raw:
            raise ValueError(
                f"Session at {path} is missing required field {required!r}."
            )
    rom = raw["rom"]
    session = raw["session"]
    if not isinstance(rom, dict):
        raise ValueError(f"Session at {path} has a non-object rom section.")
    if not isinstance(session, dict):
        raise ValueError(f"Session at {path} has a non-object session section.")
    for required in ("sha256",):
        if required not in rom or not isinstance(rom[required], str):
            raise ValueError(f"Session at {path} is missing rom.{required}.")
    for required in ("name", "slug", "current_checkpoint_name", "current_checkpoint_path"):
        if required not in session or not isinstance(session[required], str):
            raise ValueError(f"Session at {path} is missing session.{required}.")
