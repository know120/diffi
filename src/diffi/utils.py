"""Shared utilities, constants, and file I/O helpers for Diffi."""
from __future__ import annotations

import json
import tempfile
from pathlib import Path
from typing import Any

METHODS: list[str] = ["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"]
TIMEOUT: int = 30
MAX_HISTORY: int = 50
HISTORY_FILE: Path = Path.home() / ".diffi_history.json"
PROFILES_FILE: Path = Path.home() / ".diffi_profiles.json"
AUTH_TYPES: list[str] = ["None", "Basic Auth", "Bearer Token", "OAuth2"]


def load_json(path: Path) -> Any:
    """Read and parse a JSON file, returning None on any error."""
    if path.exists():
        try:
            return json.loads(path.read_text())
        except (json.JSONDecodeError, OSError):
            return None
    return None


def save_json(path: Path, data: Any) -> None:
    """Atomically write JSON to *path* (write-to-temp then rename)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
        with open(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, default=str)
        Path(tmp).replace(path)
    except BaseException:
        try:
            Path(tmp).unlink(missing_ok=True)  # type: ignore[union-attr]
        except Exception:
            pass
        raise
