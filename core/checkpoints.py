"""Named checkpoint helpers built on top of savestate v1 files."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from core.savestate import SavestateDocument, load_savestate


@dataclass(frozen=True)
class NamedCheckpoint:
    """One named checkpoint stored on disk."""

    name: str
    slug: str
    path: Path
    document: SavestateDocument


def checkpoint_root_for_rom(rom_path: Path) -> Path:
    """Return the default checkpoint directory for one ROM."""
    return rom_path.resolve().parent / ".ngpc_emu" / "checkpoints"


def sanitize_checkpoint_name(name: str) -> str:
    """Convert a human checkpoint name into a stable filesystem slug."""
    normalized = re.sub(r"[^A-Za-z0-9._-]+", "-", name.strip()).strip("-._")
    if not normalized:
        raise ValueError("checkpoint name must contain at least one letter or digit")
    return normalized.lower()


def checkpoint_path_for_rom(rom_path: Path, name: str) -> Path:
    """Return the default savestate path for one named checkpoint."""
    slug = sanitize_checkpoint_name(name)
    return checkpoint_root_for_rom(rom_path) / f"{rom_path.stem}.{slug}.json"


def save_named_checkpoint(path: Path, payload: dict[str, object]) -> None:
    """Persist one checkpoint payload to its final path."""
    path.parent.mkdir(parents=True, exist_ok=True)
    from core.savestate import save_savestate

    save_savestate(path, payload)


def load_named_checkpoint(
    rom_path: Path,
    name: str,
) -> NamedCheckpoint:
    """Load one named checkpoint and verify it against the given ROM."""
    path = checkpoint_path_for_rom(rom_path, name)
    if not path.exists():
        raise FileNotFoundError(str(path))
    return NamedCheckpoint(
        name=name,
        slug=sanitize_checkpoint_name(name),
        path=path,
        document=load_savestate(path, expected_rom_path=rom_path),
    )


def list_named_checkpoints(rom_path: Path) -> tuple[NamedCheckpoint, ...]:
    """List all named checkpoints currently stored for one ROM directory."""
    root = checkpoint_root_for_rom(rom_path)
    if not root.exists():
        return ()

    checkpoints: list[NamedCheckpoint] = []
    prefix = f"{rom_path.stem}."
    for path in sorted(root.glob("*.json")):
        if not path.name.startswith(prefix):
            continue
        slug = path.stem[len(prefix) :]
        if slug.startswith("session."):
            continue
        checkpoints.append(
            NamedCheckpoint(
                name=slug,
                slug=slug,
                path=path,
                document=load_savestate(path, expected_rom_path=rom_path),
            )
        )
    return tuple(checkpoints)


def delete_named_checkpoint(rom_path: Path, name: str) -> Path:
    """Delete one named checkpoint and return its path."""
    path = checkpoint_path_for_rom(rom_path, name)
    if not path.exists():
        raise FileNotFoundError(str(path))
    path.unlink()
    return path
