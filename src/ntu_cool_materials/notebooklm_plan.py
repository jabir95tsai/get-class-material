"""Build local NotebookLM import plans and prepare temporary video parts."""
from __future__ import annotations

import math
import os
import shutil
import subprocess
import tempfile
import weakref
from dataclasses import replace
from pathlib import Path

from .core.naming import sanitize_component
from .i18n import t
from .notebooklm_models import ANNOUNCEMENT_PATHS, ImportPlan, NotebookLMError, Source
from .spinner import spinner
from .storage import sha256_file


DOCUMENT_TYPES = {".pdf", ".txt", ".md", ".docx", ".pptx", ".csv", ".epub"}
MEDIA_TYPES = {".mp3", ".wav", ".m4a", ".mp4", ".aac", ".ogg", ".opus"}
MAX_FILE_BYTES = 200_000_000


def _split_video(path: Path, output: Path, size: int) -> list[Path]:
    duration = float(subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
        check=True, capture_output=True, text=True,
    ).stdout)
    if not math.isfinite(duration) or duration <= 0:
        raise ValueError("Invalid video duration")

    initial_seconds = duration / math.ceil(size / MAX_FILE_BYTES)
    for stream_copy in (True, False):
        seconds = initial_seconds
        for _ in range(6):
            interval = f"{seconds:.6f}"
            codecs = ["-c", "copy"] if stream_copy else [
                "-c:v", "libx264", "-preset", "veryfast", "-c:a", "aac",
                "-force_key_frames", f"expr:gte(t,n_forced*{interval})",
            ]
            try:
                subprocess.run(
                    ["ffmpeg", "-nostdin", "-v", "error", "-xerror", "-i", str(path),
                     "-map", "0:V:0", "-map", "0:a:0?", *codecs,
                     "-f", "segment", "-segment_time", interval, "-reset_timestamps", "1",
                     "-segment_start_number", "1", "part %06d.mp4"],
                    cwd=output, check=True, capture_output=True,
                )
            except subprocess.CalledProcessError:
                if not stream_copy:
                    raise
                for part in output.glob("part *.mp4"):
                    part.unlink()
                break
            parts = sorted(output.glob("part *.mp4"))
            if len(parts) >= 2 and all(0 < part.stat().st_size <= MAX_FILE_BYTES for part in parts):
                return parts
            for part in parts:
                part.unlink()
            seconds /= 2
    raise ValueError("Could not produce video parts within the upload limit")


def build_import_plan(course_dir: Path, *, include_media: bool = True) -> ImportPlan:
    """Scan and hash candidates, preparing temporary parts for oversized MP4s."""
    root = course_dir.expanduser().resolve()
    if not root.is_dir():
        raise NotebookLMError(t("教材資料夾不存在；請指定一門課的資料夾。",
                                "The materials folder doesn't exist; point at one course's folder."))
    print(t(f"NotebookLM：正在掃描並準備教材（{root.name}）…",
            f"NotebookLM: scanning and preparing materials ({root.name})…"), flush=True)
    plan = ImportPlan(root)
    allowed = DOCUMENT_TYPES | (MEDIA_TYPES if include_media else set())
    seen: set[str] = set()
    used_titles: set[str] = set()
    def fail_scan(exc):
        raise NotebookLMError(t("無法讀取教材資料夾，請檢查檔案存取權。",
                                "Can't read the materials folder; check file permissions.")) from exc

    for directory, dirs, files in os.walk(root, followlinks=False, onerror=fail_scan):
        dirs[:] = sorted(d for d in dirs if not d.startswith(".") and d != "metadata"
                         and not (Path(directory) / d).is_symlink()
                         and not (getattr((Path(directory) / d).stat(), "st_file_attributes", 0) & 0x400)
                         and (Path(directory) / d).resolve().is_relative_to(root))
        for name in sorted(files):
            path = Path(directory) / name
            relative = path.relative_to(root).as_posix()
            if name.startswith(".") or relative == "course_overview.md":
                continue
            if path.is_symlink() or not path.resolve().is_relative_to(root):
                plan.skipped.append((relative, t("連結檔案", "link")))
                continue
            if path.suffix.lower() not in allowed:
                reason = (t("已用 --notebooklm-no-media 略過影音檔", "media skipped by --notebooklm-no-media")
                          if path.suffix.lower() in MEDIA_TYPES
                          else t("NotebookLM 不支援的格式", "format NotebookLM doesn't support"))
                plan.skipped.append((relative, reason))
                continue
            size = path.stat().st_size
            split_video = size > MAX_FILE_BYTES and path.suffix.lower() == ".mp4"
            if not size or (size > MAX_FILE_BYTES and not split_video):
                reason = t("空檔案", "empty file") if not size else t("超過 200 MB", "over 200 MB")
                plan.skipped.append((relative, reason))
                continue
            digest = sha256_file(path)
            if digest in seen:
                plan.skipped.append((relative, t("內容相同", "duplicate content")))
                continue
            parts = Path(relative).with_suffix("").parts
            if len(parts) >= 2 and parts[-1].casefold() == parts[-2].casefold():
                parts = parts[:-1]
            label = sanitize_component(" - ".join(parts), max_length=90)
            if relative in ANNOUNCEMENT_PATHS or label == "announcements":
                base_title = "announcements"
                suffix = ""
            else:
                base_title = f"{label}{path.suffix.lower()}"
                suffix = path.suffix.lower()
            uploads = [Source(path, relative, digest, base_title)]
            if split_video:
                try:
                    if plan._video_parts is None:
                        plan._video_parts = Path(tempfile.mkdtemp(prefix="ntucool-video-parts-"))
                        weakref.finalize(plan, shutil.rmtree, plan._video_parts, ignore_errors=True)
                    output = Path(tempfile.mkdtemp(dir=plan._video_parts))
                    with spinner(t(f"  正在分割 {relative}（{size / 1_000_000:.1f} MB）",
                                   f"  Splitting {relative} ({size / 1_000_000:.1f} MB)")):
                        video_parts = _split_video(path, output, size)
                    if sha256_file(path) != digest:
                        raise ValueError("Video changed during splitting")
                    uploads = []
                    for index, part in enumerate(video_parts, 1):
                        content_digest = sha256_file(part)
                        uploads.append(Source(
                            part, f"{relative} (part {index})", f"{digest}:{index}:{content_digest}",
                            f"{label} - part {index}.mp4", content_digest,
                        ))
                except FileNotFoundError:
                    plan.skipped.append((relative, t("影片分割需要 ffmpeg 和 ffprobe，請確認檔案及工具可用",
                                                     "Video splitting requires ffmpeg and ffprobe; check the file and tools")))
                    continue
                except (OSError, ValueError, subprocess.CalledProcessError):
                    plan.skipped.append((relative, t("影片分割失敗，無法產生符合 200 MB 上限的分段",
                                                     "Video splitting failed to produce parts within 200 MB")))
                    continue
            seen.add(digest)
            for source in uploads:
                title = source.title
                title_stem = Path(title).stem if suffix else title
                counter = 2
                while title in used_titles:
                    title = f"{title_stem} ({counter}){suffix}"
                    counter += 1
                used_titles.add(title)
                plan.sources.append(replace(source, title=title))
    return plan
