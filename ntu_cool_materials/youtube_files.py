"""Place cached YouTube media at its titled course destination."""
from __future__ import annotations

import os
import shutil
from pathlib import Path


def youtube_cache_dir(course_dir: Path) -> Path:
    return course_dir / ".media-cache" / "youtube"


def place_cached_video(source: Path, target: Path) -> bool:
    """Publish via a sibling .part file. Return True when a full copy was needed."""
    target.parent.mkdir(parents=True, exist_ok=True)
    part = target.with_name(target.name + ".part")
    part.unlink(missing_ok=True)
    try:
        os.link(source, part)
        copied = False
    except OSError:
        shutil.copyfile(source, part)
        copied = True
    part.replace(target)
    return copied
