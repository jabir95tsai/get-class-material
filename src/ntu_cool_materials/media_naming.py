"""Media naming helpers, available at their original import path."""
from .core.naming import (
    WINDOWS_RESERVED_NAMES, WINDOWS_UNSAFE_CHARS, YOUTUBE_ID_RE, YOUTUBE_V_PARAM_RE,
    build_video_title_map, extract_youtube_ids, sanitize_teacher_title,
)

__all__ = [
    "WINDOWS_RESERVED_NAMES", "WINDOWS_UNSAFE_CHARS", "YOUTUBE_ID_RE", "YOUTUBE_V_PARAM_RE",
    "build_video_title_map", "extract_youtube_ids", "sanitize_teacher_title",
]
