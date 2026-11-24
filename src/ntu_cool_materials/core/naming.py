"""Course directories, module labels, media identifiers and filenames."""
from __future__ import annotations

import re
import unicodedata
from collections.abc import Mapping
from pathlib import Path
from typing import TypedDict


WINDOWS_RESERVED_NAMES = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    "COM1",
    "COM2",
    "COM3",
    "COM4",
    "COM5",
    "COM6",
    "COM7",
    "COM8",
    "COM9",
    "LPT1",
    "LPT2",
    "LPT3",
    "LPT4",
    "LPT5",
    "LPT6",
    "LPT7",
    "LPT8",
    "LPT9",
}


YOUTUBE_ID_RE = re.compile(
    r"(?:youtu\.be/|youtube\.com/(?:watch\?[^ \n\r\t]+?v=|embed/|shorts/))([A-Za-z0-9_-]{11})"
)
YOUTUBE_V_PARAM_RE = re.compile(r"[?&]v=([A-Za-z0-9_-]{11})")
WINDOWS_UNSAFE_CHARS = '<>:"/\\|?*'
_KNOWN_FILE_EXTS = {
    ".pdf", ".doc", ".docx", ".ppt", ".pptx", ".xls", ".xlsx", ".csv",
    ".zip", ".rar", ".7z", ".tar", ".gz",
    ".txt", ".md", ".rtf",
    ".jpg", ".jpeg", ".png", ".gif", ".bmp",
    ".mp3", ".wav", ".m4a",
    ".mp4", ".mov", ".avi", ".mkv",
}


class _CanvasModule(TypedDict, total=False):
    items: list[dict[str, object]]


class _WeekItems(TypedDict, total=False):
    module: _CanvasModule | None


class _CanvasFileItem(TypedDict, total=False):
    id: object
    title: object
    content_details: Mapping[str, object] | None


def course_directory_name(course: Mapping[str, object]) -> str:
    """Canonical per-course directory name used by sync, announcements, etc."""
    course_id = str(course.get("id") or "")
    name = str(course.get("name") or course.get("course_code") or course_id or "course")
    return sanitize_component(f"{name} ({course_id})" if course_id else name, max_length=160)


def sanitize_component(value: str | None, fallback: str = "untitled", max_length: int = 120) -> str:
    raw = unicodedata.normalize("NFKC", value or "").strip()
    cleaned = "".join("_" if _is_unsafe_filename_char(char) else char for char in raw)
    cleaned = " ".join(cleaned.split()).strip(" .")

    if not cleaned:
        cleaned = fallback

    if cleaned.upper() in WINDOWS_RESERVED_NAMES:
        cleaned = f"_{cleaned}"

    if len(cleaned) > max_length:
        cleaned = cleaned[:max_length].rstrip(" .")

    return cleaned or fallback


def _is_unsafe_filename_char(char: str) -> bool:
    return ord(char) < 32 or char in WINDOWS_UNSAFE_CHARS


def extract_youtube_ids(value: str | None) -> list[str]:
    if not value:
        return []

    positioned_ids: list[tuple[int, str]] = []
    for pattern in (YOUTUBE_ID_RE, YOUTUBE_V_PARAM_RE):
        positioned_ids.extend((match.start(1), match.group(1)) for match in pattern.finditer(value))

    ids: list[str] = []
    for _position, video_id in sorted(positioned_ids, key=lambda item: item[0]):
        if video_id not in ids:
            ids.append(video_id)
    return ids


def build_video_title_map(week_items: _WeekItems) -> dict[str, str]:
    # When Canvas reuses a YouTube id under several item titles, keep the first
    # title because it matches the downloaded YouTube metadata more often.
    title_by_video_id: dict[str, str] = {}

    module = week_items.get("module")
    items = module.get("items", []) if isinstance(module, dict) else []
    for item in items:
        if item.get("type") not in {"ExternalUrl", "ExternalTool"}:
            continue
        title = str(item.get("title") or "").strip()
        raw_url = str(item.get("external_url") or item.get("url") or "")
        for video_id in extract_youtube_ids(raw_url):
            title_by_video_id.setdefault(video_id, title)
    return title_by_video_id


def sanitize_teacher_title(value: str, *, max_length: int = 160) -> str:
    normalized = unicodedata.normalize("NFC", value).strip()
    cleaned = "".join("_" if _is_unsafe_filename_char(char) else char for char in normalized)
    cleaned = " ".join(cleaned.split()).strip(" .")
    if cleaned.split(".", 1)[0].upper() in WINDOWS_RESERVED_NAMES:
        cleaned = "_" + cleaned
    if len(cleaned) > max_length:
        cleaned = cleaned[:max_length].rstrip(" .")
    return cleaned or "untitled"


def _module_label(module: Mapping[str, object], index: int) -> str:
    """Pick a directory label like 'week3' from the module name."""
    name = str(module.get("name") or "")
    m = re.search(r"week\s*(\d+)", name, re.IGNORECASE)
    if m:
        return f"week{int(m.group(1))}"
    return f"module{index}"


def _file_item_real_ext(item: _CanvasFileItem) -> str:
    """Best-effort original extension for a Canvas File module item."""
    title = str(item.get("title") or "").strip()
    content_details = item.get("content_details") or {}
    display_name = str(content_details.get("display_name") or content_details.get("filename") or "")
    real_ext = Path(display_name).suffix.lower() if display_name else ""
    if not real_ext:
        real_ext = Path(title).suffix.lower()
    return real_ext


def _file_item_target_name(item: _CanvasFileItem, *, default_extension: str = ".pdf") -> str:
    """Keep the real file extension; use the caller's fallback when Canvas has none."""
    title = str(item.get("title") or "").strip() or f"item-{item.get('id')}"
    real_ext = _file_item_real_ext(item)
    use_ext = real_ext or default_extension
    title_ext = Path(title).suffix.lower()
    stem = title[:-len(title_ext)] if title_ext and (title_ext == real_ext or title_ext in _KNOWN_FILE_EXTS) else title
    safe_title = sanitize_teacher_title(stem)
    return f"{safe_title}{use_ext}"
