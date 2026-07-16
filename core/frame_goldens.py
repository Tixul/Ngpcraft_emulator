"""Named frame goldens for screenshot regression workflows (pass 25).

Parallels `core/goldens.py` (event-log goldens). One named frame golden
is a pair of files under `.ngpc_emu/goldens-frame/`:

- `<rom>.<slug>.golden.ppm` — the raw P6 PPM bytes captured at save time
- `<rom>.<slug>.golden.json` — manifest with rom path, hashes, dimensions,
  optional human label, captured timestamp, renderer pass + control-register
  snapshot

The manifest is the source of truth for golden identity; the PPM is the
artifact. `golden-check` re-renders the current frame and compares it
to the stored PPM byte-by-byte via `core/frame_diff.py`.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from core.checkpoints import sanitize_checkpoint_name


FRAME_GOLDEN_FORMAT_VERSION = 1


@dataclass(frozen=True)
class NamedFrameGolden:
    """One named frame golden stored on disk."""

    name: str
    slug: str
    ppm_path: Path
    manifest_path: Path
    manifest: dict[str, object]


def frame_golden_root_for_rom(rom_path: Path) -> Path:
    """Return the default named-frame-golden directory for one ROM."""
    return rom_path.resolve().parent / ".ngpc_emu" / "goldens-frame"


def sanitize_frame_golden_name(name: str) -> str:
    """Convert a human golden name into a stable filesystem slug."""
    return sanitize_checkpoint_name(name)


def frame_golden_paths_for_rom(
    rom_path: Path, name: str,
) -> tuple[Path, Path]:
    """Return `(ppm_path, manifest_path)` for one named frame golden."""
    slug = sanitize_frame_golden_name(name)
    root = frame_golden_root_for_rom(rom_path)
    base = root / f"{rom_path.stem}.{slug}.golden"
    return Path(str(base) + ".ppm"), Path(str(base) + ".json")


def build_frame_golden_manifest(
    *,
    rom_path: Path,
    name: str,
    ppm_bytes: bytes,
    width: int,
    height: int,
    label: str | None = None,
    seed_from: str | None = None,
    renderer_pass: str = "1.3",
    control_snapshot: dict | None = None,
) -> dict[str, object]:
    """Build a manifest dict suitable for `save_frame_golden`.

    The manifest is content-addressed via `ppm_sha256` so a downstream
    integrity check can flag "manifest says version X but the PPM on disk
    hashes to Y" — useful in shared repos or after a manual edit.
    """
    rom_sha = hashlib.sha256(rom_path.read_bytes()).hexdigest()
    ppm_sha = hashlib.sha256(ppm_bytes).hexdigest()
    return {
        "format_version": FRAME_GOLDEN_FORMAT_VERSION,
        "name": name,
        "slug": sanitize_frame_golden_name(name),
        "label": label,
        "rom_path": str(rom_path),
        "rom_sha256": rom_sha,
        "ppm_path": None,                     # filled at save time
        "ppm_byte_count": len(ppm_bytes),
        "ppm_sha256": ppm_sha,
        "width": width,
        "height": height,
        "captured_at_utc": datetime.now(timezone.utc).isoformat(),
        "seed_from": seed_from,
        "renderer_pass": renderer_pass,
        "control_snapshot": control_snapshot,
    }


def save_frame_golden(
    rom_path: Path,
    name: str,
    ppm_bytes: bytes,
    manifest: dict[str, object],
) -> tuple[Path, Path]:
    """Persist one named frame golden to disk: PPM + manifest JSON."""
    ppm_path, manifest_path = frame_golden_paths_for_rom(rom_path, name)
    ppm_path.parent.mkdir(parents=True, exist_ok=True)
    ppm_path.write_bytes(ppm_bytes)
    manifest = dict(manifest)
    manifest["ppm_path"] = str(ppm_path)
    manifest_path.write_text(
        json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8"
    )
    return ppm_path, manifest_path


def load_frame_golden(rom_path: Path, name: str) -> NamedFrameGolden:
    """Load one named frame golden by name, validating both files exist."""
    ppm_path, manifest_path = frame_golden_paths_for_rom(rom_path, name)
    if not manifest_path.exists():
        raise FileNotFoundError(str(manifest_path))
    if not ppm_path.exists():
        raise FileNotFoundError(str(ppm_path))
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    return NamedFrameGolden(
        name=manifest.get("name", name),
        slug=sanitize_frame_golden_name(name),
        ppm_path=ppm_path,
        manifest_path=manifest_path,
        manifest=manifest,
    )


def list_frame_goldens(rom_path: Path) -> tuple[NamedFrameGolden, ...]:
    """List every named frame golden currently stored for one ROM."""
    root = frame_golden_root_for_rom(rom_path)
    if not root.exists():
        return ()
    prefix = f"{rom_path.stem}."
    suffix = ".golden.json"
    result: list[NamedFrameGolden] = []
    for manifest_path in sorted(root.glob(f"*{suffix}")):
        if not manifest_path.name.startswith(prefix):
            continue
        slug = manifest_path.name[len(prefix):-len(suffix)]
        ppm_path = manifest_path.parent / (
            manifest_path.name[:-len(".json")] + ".ppm"
        )
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        result.append(
            NamedFrameGolden(
                name=manifest.get("name", slug),
                slug=slug,
                ppm_path=ppm_path,
                manifest_path=manifest_path,
                manifest=manifest,
            )
        )
    return tuple(result)


def delete_frame_golden(rom_path: Path, name: str) -> tuple[Path, Path]:
    """Delete a named frame golden's manifest + PPM, return both paths."""
    ppm_path, manifest_path = frame_golden_paths_for_rom(rom_path, name)
    if not manifest_path.exists():
        raise FileNotFoundError(str(manifest_path))
    manifest_path.unlink()
    if ppm_path.exists():
        ppm_path.unlink()
    return ppm_path, manifest_path
