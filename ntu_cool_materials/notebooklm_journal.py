"""Read, validate, lock and atomically persist the per-course NotebookLM journal."""
from __future__ import annotations

import json
import os
import tempfile
from contextlib import contextmanager
from pathlib import Path

from .i18n import t
from .notebooklm_models import ANNOUNCEMENT_PATHS, NotebookLMError, validate_notebook_url
from .storage import hide_file


STATE_NAME = ".notebooklm-import.json"


def read_state(path: Path) -> dict:
    if not path.exists():
        return {"version": 1, "notebooks": {}}
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
        if state.get("version") != 1 or not isinstance(state.get("notebooks"), dict):
            raise ValueError
        for url, record in state["notebooks"].items():
            validate_notebook_url(url)
            if not isinstance(record.get("sources"), dict):
                raise ValueError
            for entry in record["sources"].values():
                if entry.get("status") not in {"pending", "complete"} or not isinstance(entry.get("title"), str):
                    raise ValueError
                if "source_id" in entry and (not isinstance(entry["source_id"], str) or not entry["source_id"]):
                    raise ValueError
                if "announcement_path" in entry and entry["announcement_path"] not in ANNOUNCEMENT_PATHS:
                    raise ValueError
        if state.get("default_url"):
            validate_notebook_url(state["default_url"])
        return state
    except (ValueError, TypeError, AttributeError) as exc:
        raise NotebookLMError(t("匯入紀錄格式損壞；請先檢查 .notebooklm-import.json，避免重複上傳。",
                                "The import record is corrupt; check .notebooklm-import.json first to avoid duplicate uploads.")) from exc


def save_state(path: Path, state: dict) -> None:
    fd, tmp = tempfile.mkstemp(prefix=".notebooklm-state-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(state, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, path)
    finally:
        Path(tmp).unlink(missing_ok=True)
    hide_file(path)


@contextmanager
def import_lock(root: Path):
    """A leftover .notebooklm-import.lock prevents concurrent or interrupted imports from overlapping."""
    path = root / ".notebooklm-import.lock"
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError as exc:
        raise NotebookLMError(t("此課程已有匯入工作或遺留鎖；確認沒有匯入程式後，才移除 .notebooklm-import.lock。",
                                "Another import is running for this course, or a stale lock was left; only remove "
                                ".notebooklm-import.lock once you're sure no import is running.")) from exc
    try:
        os.close(fd)
        yield
    finally:
        path.unlink(missing_ok=True)
