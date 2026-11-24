"""Read, validate, lock and atomically persist the per-course NotebookLM journal."""
from __future__ import annotations

import errno
import json
import os
import tempfile
from contextlib import contextmanager
from pathlib import Path

from .i18n import t
from .notebooklm_models import ANNOUNCEMENT_PATHS, SYLLABUS_PATHS, NotebookLMError, validate_notebook_url
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
                if "syllabus_path" in entry and entry["syllabus_path"] not in SYLLABUS_PATHS:
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
    """Serialize imports and let the OS release the lock when a process exits."""
    marker = root / ".notebooklm-import.lock"
    # Keep the same file on disk so every process locks the same filesystem object.
    with (root / ".notebooklm-import.guard").open("a+b") as guard:
        try:
            if os.name == "nt":
                import msvcrt

                guard.seek(0)
                msvcrt.locking(guard.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(guard.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            if exc.errno not in {errno.EACCES, errno.EAGAIN}:
                raise
            raise NotebookLMError(t("此課程已有匯入工作正在執行，請等它結束後再試。",
                                    "An import is already running for this course. Wait for it to finish before retrying.")) from exc

        # Older versions create a file here. A directory marks an import protected by the OS lock.
        try:
            marker.mkdir()
        except FileExistsError as exc:
            if marker.is_symlink() or not marker.is_dir():
                raise NotebookLMError(t("此課程有舊版匯入鎖。請確認舊版匯入已結束，再將 .notebooklm-import.lock 改名。",
                                        "This course has a legacy import lock. Confirm the old import has stopped before renaming .notebooklm-import.lock.")) from exc
        try:
            yield
        finally:
            marker.rmdir()
