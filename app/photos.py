from __future__ import annotations

from pathlib import Path
from typing import Any

ALLOWED_EXTENSIONS = {".jpg", ".jpeg", ".png"}


def list_customer_photos(customer: dict[str, Any]) -> list[dict[str, str]]:
    root = Path(customer["photo_dir"]).resolve()
    root.mkdir(parents=True, exist_ok=True)
    photos: list[dict[str, str]] = []
    for path in sorted(root.iterdir()):
        if path.is_symlink() or not path.is_file() or path.suffix.lower() not in ALLOWED_EXTENSIONS:
            continue
        # Sidecar convention: team_event.jpg.txt describes team_event.jpg.
        sidecar = path.with_name(path.name + ".txt")
        description = sidecar.read_text("utf-8").strip() if sidecar.exists() else ""
        photos.append({
            "filename": path.name,
            "description": description or "No description provided; assess only the filename and do not infer unsupported details.",
        })
    return photos


def resolve_customer_photo(customer: dict[str, Any], filename: str) -> Path:
    if Path(filename).name != filename:
        raise ValueError("Invalid photo filename")
    path = (Path(customer["photo_dir"]).resolve() / filename).resolve()
    root = Path(customer["photo_dir"]).resolve()
    if path.parent != root or not path.is_file() or path.suffix.lower() not in ALLOWED_EXTENSIONS:
        raise ValueError("The selected photo is not in this customer's photo pool")
    return path
