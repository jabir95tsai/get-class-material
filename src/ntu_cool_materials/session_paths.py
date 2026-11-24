"""File locations shared by the CLI and session helpers."""
from __future__ import annotations

from pathlib import Path


STORAGE_STATE_NAME = "ntu_cool_storage_state.json"


def secrets_dir() -> Path:
    """Reuse an existing ./.secrets directory, otherwise use the user's home."""
    legacy = Path(".secrets")
    try:
        if legacy.is_dir():
            return legacy
    except OSError:
        pass
    return Path.home() / ".ntu-cool-gcm" / ".secrets"


def storage_state_path(headers_path: Path) -> Path:
    if headers_path.name == "ntu_cool_headers.txt":
        return headers_path.with_name(STORAGE_STATE_NAME)
    return headers_path.with_name(f"{headers_path.name}.storage_state.json")
