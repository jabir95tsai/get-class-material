"""Course filters, labels, and interactive selection rules."""
from __future__ import annotations

import re
from collections.abc import Mapping


def _course_label(c: Mapping[str, object]) -> str:
    return str(c.get("name") or c.get("course_code") or c.get("id"))


def _parse_multi_indices(raw: str, total: int) -> list[int] | None:
    """Parse space- or comma-separated course numbers into unique zero-based indices."""
    parts = [p for p in re.split(r"[\s,]+", raw) if p]
    if not parts:
        return None
    out: list[int] = []
    seen: set[int] = set()
    for p in parts:
        if not p.isdecimal():
            return None
        try:
            i = int(p) - 1
        except ValueError:  # Python limits the length of decimal integer conversions.
            return None
        if not (0 <= i < total):
            return None
        if i not in seen:
            seen.add(i)
            out.append(i)
    return out


def _split_course_ids(values: list[str]) -> set[str]:
    course_ids: set[str] = set()
    for value in values:
        for item in value.split(","):
            item = item.strip()
            if item:
                course_ids.add(item)
    return course_ids


def _select_courses(
    courses: list[dict[str, object]],
    *,
    all_courses: bool,
    course_ids: set[str],
    name_contains: str | None,
) -> list[dict[str, object]]:
    selected: list[dict[str, object]] = []
    seen_ids: set[str] = set()
    name_filter = name_contains.casefold() if name_contains else None

    for course in courses:
        course_id = str(course.get("id") or "")
        course_name = str(course.get("name") or course.get("course_code") or "")
        matches_id = course_id in course_ids
        matches_name = bool(name_filter and name_filter in course_name.casefold())
        if all_courses or matches_id or matches_name:
            selected.append(course)
            seen_ids.add(course_id)

    for course_id in sorted(course_ids - seen_ids):
        selected.append({"id": course_id, "name": course_id})

    return selected
