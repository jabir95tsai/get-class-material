"""NotebookLM source, plan and adapter types and their validation rules."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Protocol
from urllib.parse import urlsplit

from .i18n import t


ANNOUNCEMENT_PATHS = {"announcements/announcements.md", "announcements.md"}


class NotebookLMError(RuntimeError):
    pass


@dataclass(frozen=True)
class Source:
    path: Path
    relative_path: str
    digest: str
    title: str
    content_digest: str | None = None


@dataclass
class ImportPlan:
    root: Path
    sources: list[Source] = field(default_factory=list)
    skipped: list[tuple[str, str]] = field(default_factory=list)
    _video_parts: Path | None = field(default=None, repr=False)

    def print_skipped(self, *, show_details: bool = False) -> None:
        if not self.skipped:
            return
        print(t(f"略過的檔案（共 {len(self.skipped)} 個）", f"Skipped files ({len(self.skipped)} total)"))
        if not show_details:
            print(t("  加上 --show-skipped 可查看清單。", "  Use --show-skipped to see the list."))
            return
        by_reason: dict[str, list[str]] = {}
        for relative, reason in self.skipped:
            by_reason.setdefault(reason, []).append(relative)
        for reason, paths in by_reason.items():
            print(t(f"  {reason}（{len(paths)} 個）", f"  {reason} ({len(paths)})"))
            for relative in paths:
                print(f"    - {relative}")


@dataclass
class ImportResult:
    uploaded: int = 0
    unchanged: int = 0
    notebook_url: str = ""
    unverified: int = 0


class NotebookAdapter(Protocol):
    def open_notebook(self, url: str | None, title: str) -> str: ...
    def ready_sources(self) -> dict[str, str]: ...
    def matches_original(self, source_id: str, source: Source) -> bool: ...
    def source_count(self) -> int: ...
    def upload(self, path: Path, title: str, *, on_created: Callable[[str], None]) -> None: ...
    def delete_sources(self, source_ids: set[str], *, keep_source_id: str) -> None: ...


def validate_notebook_url(value: str) -> str:
    try:
        parsed = urlsplit(value.strip())
        port = parsed.port
    except ValueError as exc:
        raise NotebookLMError(t("NotebookLM 網址格式不正確。", "Malformed NotebookLM URL.")) from exc
    if (parsed.scheme != "https" or parsed.hostname not in
            {"notebooklm.google.com", "notebook.google.com"}
            or parsed.username or parsed.password or port
            or not re.fullmatch(r"/notebook/[A-Za-z0-9_-]+/?", parsed.path)):
        raise NotebookLMError(t("請提供 NotebookLM 筆記本的 https 網址（/notebook/…）。",
                                "Give the https URL of a NotebookLM notebook (/notebook/…)."))
    # Strip tracking/account query strings; never persist authentication URLs.
    return f"https://{parsed.hostname}{parsed.path.rstrip('/')}"


def strip_legacy_hash(title: str) -> str:
    """Strip legacy 8-64 hex char hash suffix like ' [11b37d6cafc0]' before the file extension."""
    return re.sub(r" \[[0-9a-fA-F]{8,64}\](?=\.[^.]+$|$)", "", title)


def is_announcement_title(title: str) -> bool:
    """Match only announcement summaries, including names used by older imports."""
    clean = strip_legacy_hash(title)
    return bool(re.fullmatch(
        r"(?:announcements|announcements - announcements)(?: \(\d+\))?(?:\.md)?", clean))
