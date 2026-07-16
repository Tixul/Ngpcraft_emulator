"""Named event-log golden helpers for regression workflows."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from core.checkpoints import sanitize_checkpoint_name
from core.event_log import load_event_log, save_event_log


@dataclass(frozen=True)
class NamedEventLogGolden:
    """One named golden event log stored on disk."""

    name: str
    slug: str
    path: Path
    payload: dict[str, object]


def golden_root_for_rom(rom_path: Path) -> Path:
    """Return the default named-golden directory for one ROM."""
    return rom_path.resolve().parent / ".ngpc_emu" / "goldens"


def sanitize_golden_name(name: str) -> str:
    """Convert a human golden name into a stable filesystem slug."""
    return sanitize_checkpoint_name(name)


def golden_path_for_rom(rom_path: Path, name: str) -> Path:
    """Return the default event-log path for one named golden."""
    slug = sanitize_golden_name(name)
    return golden_root_for_rom(rom_path) / f"{rom_path.stem}.{slug}.eventlog.json"


def save_named_golden(path: Path, payload: dict[str, object]) -> None:
    """Persist one named golden payload to disk."""
    path.parent.mkdir(parents=True, exist_ok=True)
    save_event_log(path, payload)


def load_named_golden(rom_path: Path, name: str) -> NamedEventLogGolden:
    """Load one named golden and verify it against the given ROM."""
    path = golden_path_for_rom(rom_path, name)
    if not path.exists():
        raise FileNotFoundError(str(path))
    return NamedEventLogGolden(
        name=name,
        slug=sanitize_golden_name(name),
        path=path,
        payload=load_event_log(path, expected_rom_path=rom_path),
    )


def list_named_goldens(rom_path: Path) -> tuple[NamedEventLogGolden, ...]:
    """List all named goldens currently stored for one ROM directory."""
    root = golden_root_for_rom(rom_path)
    if not root.exists():
        return ()

    goldens: list[NamedEventLogGolden] = []
    prefix = f"{rom_path.stem}."
    suffix = ".eventlog.json"
    for path in sorted(root.glob(f"*{suffix}")):
        if not path.name.startswith(prefix):
            continue
        slug = path.name[len(prefix) : -len(suffix)]
        goldens.append(
            NamedEventLogGolden(
                name=slug,
                slug=slug,
                path=path,
                payload=load_event_log(path, expected_rom_path=rom_path),
            )
        )
    return tuple(goldens)


def delete_named_golden(rom_path: Path, name: str) -> Path:
    """Delete one named golden and return its path."""
    path = golden_path_for_rom(rom_path, name)
    if not path.exists():
        raise FileNotFoundError(str(path))
    path.unlink()
    return path
