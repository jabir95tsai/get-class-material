"""Bulk per-course download pipeline.

Public API used by `cli.download-course`:
    plan_course(client, course_id, output_dir) -> CoursePlan
    download_files(plan, client) -> StageStats
    save_pages(plan, client, course_id) -> StageStats
    download_youtube(plan, *, cookies_path, yt_dlp) -> StageStats
    capture_and_download_cool_videos(plan, *, course_id, profile_dir, headless, headers_path) -> StageStats

Completed artifacts are identified by source ID and verified against a persistent manifest.

The cool-video stage reuses an available course browser. Otherwise it probes
with saved cookies in a headless context and can open a visible SSO window
if that probe fails. Pending videos use the captured view response to select
a signed media URL or a YouTube source.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .browser_session import launch_persistent_context
from .announcements import (
    ANNOUNCEMENTS_JSON, announcements_markdown, canvas_file_links, html_to_markdown, migrate_legacy_announcements_json, write_announcements,
)
from .canvas_client import CanvasAPIError, SessionExpiredError, is_downloadable_course_file
from .i18n import t
from .core.naming import (
    _file_item_target_name, _module_label, build_video_title_map, extract_youtube_ids, sanitize_teacher_title,
)
from .session_client import DEFAULT_BASE_URL, DROP_REQUEST_HEADER_NAMES, SESSION_ORIGIN_HEADER, CanvasSessionClient, read_headers_file
from .session_paths import secrets_dir, storage_state_path
from .spreadsheet_convert import SPREADSHEET_TYPES, convert_spreadsheets, markdown_path_for
from .storage import ManifestStore, atomic_write_text, course_directory_name, hide_dot_entries
from .youtube_files import place_cached_video, youtube_cache_dir
from .update_check import yt_dlp_command
from .http_io import DownloadError, download as download_http, origin
from . import console


CANVAS_NETLOC = "cool.ntu.edu.tw"
COOL_VIDEO_VIEW_RE = re.compile(r"/api/courses/(\d+)/videos/(\d+)/view$")
LOGIN_RE = re.compile(r"/login|oauth2|saml", re.IGNORECASE)

YT_DLP_BASE_ARGS = [
    "--js-runtimes", "node",
    "--extractor-args", "youtube:player_client=default,web,web_safari",
    "--no-playlist",
    "--ignore-errors",
    "--restrict-filenames",
    "--retries", "10",
    "--fragment-retries", "10",
    "--merge-output-format", "mp4",
    "-f", "bv*[ext=mp4]+ba[ext=m4a]/bv*+ba/b",
]


@dataclass
class WeekPlan:
    label: str               # e.g. "week3"
    module: dict[str, Any]   # raw module from list_modules
    week_dir: Path           # <course_dir>/<label>

    @property
    def items(self) -> list[dict[str, Any]]:
        return self.module.get("items") or []


@dataclass
class CoursePlan:
    course: dict[str, Any]
    course_id: str
    course_dir: Path
    weeks: list[WeekPlan] = field(default_factory=list)
    stats: CourseStats | None = None
    verify_files: bool = False
    workers: int = 3
    file_metadata: dict[str, dict[str, Any]] = field(default_factory=dict)
    linked_file_ids: set[str] = field(default_factory=set)
    course_files: WeekPlan | None = None


@dataclass
class StageStats:
    done: int = 0
    skipped: int = 0
    failed: list[str] = field(default_factory=list)  # human labels of failed items
    disabled: bool = False
    # Cool-videos that wrap a YouTube upload and failed: (youtube_id, key, target, version, failure label).
    youtube_retry: list[tuple[str, str, Path, str, str]] = field(default_factory=list)


@dataclass
class CourseStats:
    announcements: StageStats = field(default_factory=StageStats)
    pdfs: StageStats = field(default_factory=StageStats)
    pages: StageStats = field(default_factory=StageStats)
    youtube: StageStats = field(default_factory=StageStats)
    cool_videos: StageStats = field(default_factory=StageStats)

    @property
    def successful(self) -> bool:
        return not any(stage.failed for stage in (self.announcements, self.pdfs, self.pages, self.youtube, self.cool_videos))


def save_announcements(plan: CoursePlan, client: CanvasSessionClient) -> StageStats:
    """Refresh all visible announcements, including edits to previously saved posts."""
    stats = StageStats()
    try:
        announcements = client.list_course_announcements(plan.course_id)
        target_dir = plan.course_dir / "announcements"
        migrate_legacy_announcements_json(target_dir)
        json_path = target_dir / ANNOUNCEMENTS_JSON
        markdown_path = target_dir / "announcements.md"

        existing_posts = {}
        has_existing_files = json_path.is_file() and markdown_path.is_file()
        if has_existing_files:
            try:
                data = json.loads(json_path.read_text(encoding="utf-8"))
                if not isinstance(data, list):
                    raise ValueError("Invalid announcement snapshot")
                for idx, item in enumerate(data):
                    if isinstance(item, dict):
                        key = str(item.get("id")) if item.get("id") is not None else f"idx_{idx}"
                        existing_posts[key] = item
            except (OSError, ValueError):
                has_existing_files = False

        if not has_existing_files:
            write_announcements(plan.course_dir.parent, plan.course, announcements)
            stats.done = len(announcements)
            stats.skipped = 0
        else:
            new_or_updated = 0
            unchanged = 0
            for idx, post in enumerate(announcements):
                key = str(post.get("id")) if post.get("id") is not None else f"idx_{idx}"
                if key not in existing_posts:
                    new_or_updated += 1
                else:
                    prev = existing_posts[key]
                    prev_up = prev.get("updated_at")
                    curr_up = post.get("updated_at")
                    if prev_up and curr_up and prev_up != curr_up:
                        new_or_updated += 1
                    elif (prev.get("title") != post.get("title") or
                          prev.get("message") != post.get("message")):
                        new_or_updated += 1
                    else:
                        unchanged += 1

            if new_or_updated > 0 or len(existing_posts) != len(announcements):
                write_announcements(plan.course_dir.parent, plan.course, announcements)
                stats.done = new_or_updated
                stats.skipped = unchanged
            else:
                expected_markdown = announcements_markdown(announcements)
                try:
                    saved_markdown = markdown_path.read_text(encoding="utf-8")
                except UnicodeDecodeError:
                    saved_markdown = None
                if saved_markdown != expected_markdown:
                    atomic_write_text(markdown_path, expected_markdown)
                stats.done = 0
                stats.skipped = unchanged
        for post in announcements:
            links = canvas_file_links(post.get("message"), getattr(client, "base_url", f"https://{CANVAS_NETLOC}"))
            for attachment in post.get("attachments") or []:
                if isinstance(attachment, dict) and attachment.get("id"):
                    links[str(attachment["id"])] = attachment.get("display_name") or attachment.get("filename") or "attachment"
            linked = _download_linked_files(plan, client, plan.course_dir / "announcements" / "attachments", links)
            stats.failed.extend(linked.failed)
    except SessionExpiredError:
        raise
    except (CanvasAPIError, DownloadError, OSError, ValueError) as exc:
        stats.failed.append(f"announcements: {exc}")
    return stats


# ---- planning ----

def plan_course(client: CanvasSessionClient, course_id: str, output_dir: Path) -> CoursePlan:
    """Fetch modules, reserve on-disk paths and migrate legacy week layouts."""
    course = client.get_course(course_id)
    modules = list(client.list_paginated(
        f"/api/v1/courses/{urllib.parse.quote(str(course_id), safe='')}/modules",
        params=[("per_page", "100"), ("include[]", "items"), ("include[]", "content_details")],
    ))

    course_dir = (output_dir / course_directory_name(course)).resolve()
    course_dir.mkdir(parents=True, exist_ok=True)
    plan = CoursePlan(course=course, course_id=str(course_id), course_dir=course_dir)
    relevant_types = {"File", "Page", "ExternalUrl", "ExternalTool"}
    for index, module in enumerate(modules, start=1):
        items = module.get("items")
        if items is None or len(items) < int(module.get("items_count") or 0):
            mid = urllib.parse.quote(str(module["id"]), safe="")
            cid = urllib.parse.quote(str(course_id), safe="")
            items = list(client.list_paginated(
                f"/api/v1/courses/{cid}/modules/{mid}/items",
                params=[("per_page", "100"), ("include[]", "content_details")],
            ))
            module["items"] = items
        if not any(i.get("type") in relevant_types for i in items):
            continue
        label = _module_label(module, index)
        with _manifest(plan) as store:
            week_dir = store.artifact_path(f"module:{module.get('id', index)}", course_dir, label)
        label = week_dir.name
        week_dir.mkdir(parents=True, exist_ok=True)
        # Flatten the old per-type layout before reserving individual file paths.
        _migrate_legacy_subfolders(week_dir)
        plan.weeks.append(WeekPlan(label=label, module=module, week_dir=week_dir))
    return plan


def _migrate_legacy_subfolders(week_dir: Path) -> None:
    """Move files from legacy files/, pages/ and videos/ directly into week_dir.

    Leave colliding files at their original paths. Preserve metadata/ because
    it may contain the only remaining source-to-file mapping.
    """
    import shutil
    for subname in ("files", "pages", "videos"):
        sub = week_dir / subname
        if not sub.is_dir():
            continue
        for src in list(sub.iterdir()):
            if not src.is_file():
                continue
            dst = week_dir / src.name
            if dst.exists():
                # Don't clobber. Leave the source where it is so user can resolve manually.
                print(f"    [migrate] skipping {src.relative_to(week_dir.parent.parent)}: target {dst.name} already exists")
                continue
            shutil.move(str(src), str(dst))
        try:
            sub.rmdir()
        except OSError:
            pass  # not empty (some files left due to collisions)


# ---- HTTP helpers (session-cookie auth, cross-origin safe) ----

def _session_headers(client: CanvasSessionClient) -> dict[str, str]:
    h = {n: v for n, v in client.headers.items() if n.lower() not in DROP_REQUEST_HEADER_NAMES}
    h["Accept"] = "application/json, text/plain, */*"
    h.setdefault("User-Agent", "ntu-cool-materials/0.1")
    return h


def _progress(label: str):
    last = [0.0]
    def update(received, total):
        now = time.monotonic()
        if sys.stdout.isatty() and (now - last[0] >= 0.5 or received == total):
            last[0] = now
            size = f" / {total / 1048576:.1f} MB" if total is not None else " MB"
            print(f"\r  {label[:60]}: {received / 1048576:.1f}{size}    ", end="", flush=True)
    return update


def _download_canvas_file(file_id: str, target: Path, headers: dict[str, str], *,
                          base_url=f"https://{CANVAS_NETLOC}", expected_size=None) -> None:
    print(f"  [download] {target.name}")
    url = f"{base_url.rstrip('/')}/files/{urllib.parse.quote(file_id, safe='')}/download?download_frd=1"
    def scoped(current):
        return headers if origin(current) == origin(base_url) else {"User-Agent": "ntu-cool-materials/0.1"}
    download_http(url, target, scoped, identity=f"{base_url}/file/{file_id}", expected_size=expected_size)


def _download_signed_url(url: str, target: Path) -> None:
    try:
        download_http(url, target, lambda _: {"User-Agent": "ntu-cool-materials/0.1"},
                      validate=_valid_video, progress=_progress(target.name))
    finally:
        if sys.stdout.isatty():
            print()


def _manifest(plan: CoursePlan) -> ManifestStore:
    return ManifestStore(plan.course_dir / ".ntu_cool_materials.sqlite3")


def _artifact_key(week: WeekPlan, kind: str, source_id) -> str:
    return f"{week.module.get('id', week.label)}:{kind}:{source_id}"


def _canvas_file_version(info: dict[str, Any]) -> str:
    # Older manifests could stamp this version without ever fetching the bytes.
    return json.dumps(["downloaded", info.get("updated_at"), info.get("modified_at"), info.get("size")])


def _adopt_legacy(store: ManifestStore, key: str, target: Path, version, valid) -> bool:
    """Record a valid existing file when its reserved manifest row lacks a size."""
    row = store.artifact(key)
    if row is None or row["size"] is not None or not target.is_file() or not valid(target):
        return False
    store.record_artifact(key, target, version)
    return True


def _download_linked_files(plan, client, directory, links):
    if not links:
        return StageStats()
    plan.linked_file_ids.update(links)
    items = [{"id": fid, "content_id": fid, "type": "File", "title": title, "_attachment": True}
             for fid, title in links.items()]
    week = WeekPlan(str(directory.resolve().relative_to(plan.course_dir.resolve())), {"items": items}, directory)
    linked_plan = CoursePlan(plan.course, plan.course_id, plan.course_dir, [week],
                             verify_files=plan.verify_files, workers=plan.workers, file_metadata=plan.file_metadata)
    return download_files(linked_plan, client)


# ---- per-stage workers ----

def _unlisted_course_files(plan: CoursePlan, client: CanvasSessionClient, stats: StageStats) -> WeekPlan | None:
    seen = plan.linked_file_ids | {
        str(item["content_id"]) for week in plan.weeks for item in week.items
        if item.get("type") == "File" and item.get("content_id")
    }
    try:
        files = client.list_course_files(plan.course_id)
    except SessionExpiredError:
        raise
    except (CanvasAPIError, DownloadError, OSError, ValueError) as exc:
        stats.failed.append(t(f"課程檔案清單：{exc}", f"Course file listing: {exc}"))
        return None

    items = []
    for info in files:
        fid = str(info.get("id") or "")
        if not fid:
            stats.failed.append(t("課程檔案缺少 ID。", "A course file has no ID."))
            continue
        if fid in seen:
            continue
        seen.add(fid)
        if not is_downloadable_course_file(info):
            stats.skipped += 1
            continue
        plan.file_metadata[fid] = info
        title = info.get("display_name") or info.get("filename") or f"file-{fid}"
        items.append({"type": "File", "content_id": fid, "title": title})
    if items:
        module = {"id": "course-files", "name": t("課程檔案", "Course files"), "items": items}
        return WeekPlan("files", module, plan.course_dir / "files")
    return None


def _reuse_files_copy(store, course_dir, fid, key, target, version):
    old_key = f"course-files:file:{fid}"
    row = store.artifact(old_key)
    if row is None:
        return None
    old = Path(row["path"])
    if old.resolve().parent != course_dir / "files" or old == target or old.is_symlink() or not old.is_file():
        return None
    if not target.exists() and store.artifact_current(old_key, old, version, verify_hash=True):
        target.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=target.parent) as stage:
            staged = Path(stage) / target.name
            shutil.copyfile(old, staged)
            staged.replace(target)
        store.record_artifact(key, target, version)
    return old


def _retire_files_copy(store, fid, path):
    """Keep the prior copy recoverable, outside the active material scan."""
    row = store.artifact(f"course-files:file:{fid}")
    if not store.artifact_current(row["key"], path, row["version"], verify_hash=True):
        return
    archive = path.parent.parent / ".previous-course-files"
    archive.mkdir(exist_ok=True)
    saved = Path(tempfile.mkdtemp(dir=archive))
    if path.suffix.lower() in SPREADSHEET_TYPES:
        sidecar = markdown_path_for(path)
        if sidecar.is_file() and not store.has_artifact_path(sidecar):
            sidecar.rename(saved / sidecar.name)
    path.rename(saved / path.name)


def download_files(plan: CoursePlan, client: CanvasSessionClient, *, only_unlisted: bool = False) -> StageStats:
    """Refresh metadata, reserve unique paths, and transfer missing/changed files."""
    headers = _session_headers(client)
    stats = StageStats()
    jobs = []
    previous_files = {}
    base_url = getattr(client, "base_url", f"https://{CANVAS_NETLOC}")
    if not isinstance(base_url, str):
        base_url = f"https://{CANVAS_NETLOC}"
    weeks = plan.weeks
    if only_unlisted:
        plan.course_files = _unlisted_course_files(plan, client, stats)
        weeks = [plan.course_files] if plan.course_files else []
    with _manifest(plan) as store:
        for week in weeks:
            for item in week.items:
                if item.get("type") != "File":
                    continue
                fid = str(item.get("content_id") or "")
                try:
                    if not fid:
                        raise ValueError("File item has no content_id")
                    info = plan.file_metadata.get(fid)
                    if info is None:
                        getter = getattr(client, "get_json", None)
                        info = getter(f"/api/v1/files/{urllib.parse.quote(fid, safe='')}") if getter else (item.get("content_details") or {})
                        if not isinstance(info, dict):
                            raise ValueError("Invalid file metadata")
                        plan.file_metadata[fid] = info
                    item["content_details"] = {**(item.get("content_details") or {}), **info}
                    if item.get("_attachment"):
                        item["title"] = info.get("display_name") or info.get("filename") or item["title"]
                    key = _artifact_key(week, "file", fid)
                    default_extension = "" if week is plan.course_files else ".pdf"
                    target = store.artifact_path(key, week.week_dir, _file_item_target_name(item, default_extension=default_extension))
                    item["_local_path"] = str(target)
                    version = _canvas_file_version(info)
                    size = int(info["size"]) if info.get("size") is not None else None
                    if not only_unlisted and not item.get("_attachment"):
                        old = _reuse_files_copy(store, plan.course_dir, fid, key, target, version)
                        if old:
                            previous_files[key] = (fid, old, target, version)
                    if store.artifact_current(key, target, version, verify_hash=plan.verify_files):
                        stats.skipped += 1
                        continue
                    jobs.append((key, fid, target, version, size))
                except SessionExpiredError:
                    raise
                except (CanvasAPIError, DownloadError, OSError, ValueError) as exc:
                    stats.failed.append(f"{week.label}/{item.get('title')}: {exc}")
        # Reserve names and access SQLite only on the calling thread.
        jobs = list({job[0]: job for job in jobs}.values())
        with ThreadPoolExecutor(max_workers=max(1, min(plan.workers, 4))) as pool:
            pending = {pool.submit(_download_canvas_file, fid, target, headers,
                                   base_url=base_url, expected_size=size): (key, target, version)
                       for key, fid, target, version, size in jobs}
            expired = None
            for future in as_completed(pending):
                key, target, version = pending[future]
                try:
                    future.result()
                    store.record_artifact(key, target, version)
                    stats.done += 1
                    print(f"  [file] {target.name}")
                except SessionExpiredError as exc:
                    expired = exc
                except urllib.error.HTTPError as exc:
                    if exc.code == 401:
                        expired = SessionExpiredError("Canvas file session expired")
                    else:
                        stats.failed.append(f"{target.name}: HTTP {exc.code}")
                except Exception as exc:
                    stats.failed.append(f"{target.name}: {type(exc).__name__}: {exc}")
            if expired:
                expired.stage_stats = stats
                raise expired
        for key, (fid, old, target, version) in previous_files.items():
            if old.is_file() and store.artifact_current(key, target, version, verify_hash=True):
                try:
                    _retire_files_copy(store, fid, old)
                except OSError as exc:
                    stats.failed.append(f"{old.name}: {exc}")
    return stats


def save_pages(plan: CoursePlan, client: CanvasSessionClient, course_id: str) -> StageStats:
    """Save every Page-type module item as <title>.md directly under the week dir."""
    stats = StageStats()
    from .pages import fetch_page
    for week in plan.weeks:
        for item in week.items:
            if item.get("type") != "Page":
                continue
            try:
                slug = item.get("page_url")
                if not slug:
                    raise ValueError("Page item has no page_url")
                page = fetch_page(client, course_id, str(slug))
                title = str(item.get("title") or page.get("title") or slug)
                base_url = getattr(client, "base_url", f"https://{CANVAS_NETLOC}")
                page_url = f"{base_url}/courses/{course_id}/pages/{urllib.parse.quote(str(slug), safe='')}"
                body = html_to_markdown(page.get("body"), base_url=page_url)
                text = f"# {page.get('title') or title}\n\n{body}\n"
                version = hashlib.sha256(text.encode()).hexdigest()
                with _manifest(plan) as store:
                    key = _artifact_key(week, "page", slug)
                    target = store.artifact_path(key, week.week_dir, f"{sanitize_teacher_title(title)}.md")
                    item["_local_path"] = str(target)
                    if store.artifact_current(key, target, version, verify_hash=True):
                        stats.skipped += 1
                    else:
                        atomic_write_text(target, text)
                        store.record_artifact(key, target, version)
                        stats.done += 1
                linked = _download_linked_files(plan, client, week.week_dir / "attachments",
                                                canvas_file_links(page.get("body"), base_url))
                stats.failed.extend(linked.failed)
            except SessionExpiredError:
                raise
            except (CanvasAPIError, DownloadError, OSError, ValueError) as exc:
                stats.failed.append(f"{week.label}/{item.get('title')}: {exc}")
    return stats


def _youtube_cookie_args(cookies_path: Path, cookies_from_browser: str | None) -> list[str]:
    """yt-dlp auth args. A real cookies.txt wins; otherwise read the login
    live from the user's normal browser via --cookies-from-browser, which
    sidesteps Google's 'this browser may not be secure' block on automated
    sign-ins. Empty list = try as a public/unlisted download."""
    if cookies_path.exists():
        return ["--cookies", str(cookies_path)]
    if cookies_from_browser:
        return ["--cookies-from-browser", cookies_from_browser]
    return []


# A profile directory marks a candidate for yt-dlp's browser-cookie retry.
# Candidate order determines retry order.
def _cookie_browser_candidates() -> list[tuple[str, Path]]:
    import platform

    home = Path.home()
    sys_ = platform.system()
    if sys_ == "Windows":
        local = Path(os.environ.get("LOCALAPPDATA") or home / "AppData" / "Local")
        roaming = Path(os.environ.get("APPDATA") or home / "AppData" / "Roaming")
        return [
            ("chrome", local / "Google" / "Chrome" / "User Data"),
            ("edge", local / "Microsoft" / "Edge" / "User Data"),
            ("brave", local / "BraveSoftware" / "Brave-Browser" / "User Data"),
            ("vivaldi", local / "Vivaldi" / "User Data"),
            ("opera", roaming / "Opera Software" / "Opera Stable"),
            ("firefox", roaming / "Mozilla" / "Firefox" / "Profiles"),
        ]
    if sys_ == "Darwin":
        appsup = home / "Library" / "Application Support"
        return [
            ("chrome", appsup / "Google" / "Chrome"),
            ("edge", appsup / "Microsoft Edge"),
            ("brave", appsup / "BraveSoftware" / "Brave-Browser"),
            ("vivaldi", appsup / "Vivaldi"),
            ("opera", appsup / "com.operasoftware.Opera"),
            ("chromium", appsup / "Chromium"),
            ("firefox", appsup / "Firefox" / "Profiles"),
            # Last: the default macOS browser, but yt-dlp can only read its
            # cookie file when the terminal has Full Disk Access.
            ("safari", home / "Library" / "Containers" / "com.apple.Safari"),
        ]
    cfg = home / ".config"
    return [
        ("chrome", cfg / "google-chrome"),
        ("chromium", cfg / "chromium"),
        ("brave", cfg / "BraveSoftware" / "Brave-Browser"),
        ("firefox", home / ".mozilla" / "firefox"),
    ]


def _installed_cookie_browsers() -> list[str]:
    """yt-dlp browser names whose profile dir exists, in try-order."""
    out: list[str] = []
    for name, path in _cookie_browser_candidates():
        try:
            if path.exists():
                out.append(name)
        except OSError:
            pass
    return out


def maybe_retry_youtube_with_login(
    cookies_path: Path, failed_count: int,
    *, interactive: bool = True, input_fn=None,
) -> bool:
    """Ask for consent to retry failed videos using the user's browser cookies.

    An existing cookies path, no failures or interactive=False skips the offer.
    The default input function also requires interactive stdin. The caller uses
    yt-dlp's browser-cookie support after consent.
    """
    if cookies_path.exists():
        return False
    if failed_count <= 0:
        return False
    if not interactive:
        return False
    if input_fn is None:
        if not console.stdin_is_interactive():
            return False
        input_fn = input

    prompt = t(
        f"\n  ❓ 有 {failed_count} 個 YouTube 影片下載失敗。\n"
        f"     如果是私人 / 限齡 / 會員影片,需要你的 YouTube 登入狀態才能抓。\n"
        f"     要用你瀏覽器裡已登入的 YouTube 帳號重試嗎? [y/N]: ",
        f"\n  ❓ {failed_count} YouTube download(s) failed.\n"
        f"     Private / age-restricted / member videos need your YouTube login.\n"
        f"     Retry using the YouTube login from your browser? [y/N]: ",
    )
    try:
        answer = input_fn(prompt).strip().lower()
    except (EOFError, KeyboardInterrupt):
        return False
    return answer in {"y", "yes"}


def _blocked_by_policy(tool: str) -> bool:
    """True when `tool` is on PATH but Windows Application Control (WinError 4551) won't run it."""
    path = shutil.which(tool)
    if not path:
        return False
    try:
        subprocess.run([path, "-version"], capture_output=True, timeout=10)
    except OSError as exc:
        return getattr(exc, "winerror", None) == 4551
    except subprocess.TimeoutExpired:
        pass
    return False


def _tool_usable(tool: str) -> bool:
    return shutil.which(tool) is not None and not _blocked_by_policy(tool)


def download_youtube(
    plan: CoursePlan, *, cookies_path: Path, yt_dlp: str = "yt-dlp",
    cookies_from_browser: str | None = None,
) -> StageStats:
    """Batch missing YouTube IDs in the course cache, then link or copy to week paths.

    An existing cookies file takes precedence over the optional browser login.
    With neither, yt-dlp attempts public access.
    """
    import tempfile
    stats = StageStats()
    cache = youtube_cache_dir(plan.course_dir)
    jobs = {}
    existing = {}
    with _manifest(plan) as store:
        for week in plan.weeks:
            title_map = build_video_title_map({"module": week.module})
            for vid, title in title_map.items():
                key = _artifact_key(week, "youtube", vid)
                target = store.artifact_path(key, week.week_dir, f"{sanitize_teacher_title(title)}.mp4")
                for item in week.items:
                    if vid in extract_youtube_ids(str(item.get("external_url") or item.get("url") or "")):
                        item["_local_path"] = str(target)
                if (store.artifact_current(key, target, vid, verify_hash=plan.verify_files)
                        or _adopt_legacy(store, key, target, vid, _valid_video)):
                    stats.skipped += 1
                    existing[vid] = target
                    continue
                jobs.setdefault(vid, []).append((key, target))
        if not jobs:
            return stats
        if not _tool_usable("ffmpeg"):
            # Without ffmpeg yt-dlp leaves separate video/audio files it can't merge.
            import platform
            hint = {
                "Windows": "winget install Gyan.FFmpeg",
                "Darwin": "brew install ffmpeg",
            }.get(platform.system(), "apt install ffmpeg  (or your distro's equivalent)")
            if _blocked_by_policy("ffmpeg"):
                # Reinstalling won't help; the policy has to allow the file.
                print(t(
                    "  ⚠ ffmpeg 被 Windows「應用程式控制原則」封鎖,無法合併 YouTube 影音。\n"
                    "     請向電腦管理員申請放行 ffmpeg.exe,或改用不受限制的電腦。YouTube 階段先跳過。",
                    "  ⚠ ffmpeg is blocked by Windows Application Control, so YouTube video and audio can't be merged.\n"
                    "     Ask your administrator to allow ffmpeg.exe, or use an unrestricted computer. Skipping YouTube.",
                ))
            else:
                print(t(
                    f"  ⚠ 找不到 ffmpeg,無法把 YouTube 的影片與聲音合併成可播放的 mp4。\n"
                    f"     請先安裝: {hint}\n"
                    f"     或執行 `ntu-cool-materials doctor --fix` 嘗試自動安裝。YouTube 階段先跳過。",
                    f"  ⚠ ffmpeg not found; YouTube video and audio can't be merged into a playable mp4.\n"
                    f"     Install it first: {hint}\n"
                    f"     Or run `ntu-cool-materials doctor --fix` to try auto-install. Skipping YouTube.",
                ))
            stats.failed = [f"YouTube {vid}: ffmpeg is required" for vid in jobs]
            return stats
        if _blocked_by_policy("node"):
            print(t(
                "  ⚠ Node.js 被 Windows「應用程式控制原則」封鎖。yt-dlp 在解 YouTube JS 挑戰時可能會失敗或畫質卡在 360p。\n"
                "     可向管理員申請放行 node.exe。",
                "  ⚠ Node.js is blocked by Windows Application Control. yt-dlp may fail YouTube's JS challenge\n"
                "     or cap quality at 360p. Ask your administrator to allow node.exe.",
            ))
        elif shutil.which("node") is None:
            # yt-dlp needs a JS runtime for YouTube's challenge; without it some
            # videos fail or cap at 360p.
            print(t(
                "  ⚠ 找不到 Node.js。yt-dlp 在解 YouTube JS 挑戰時可能會失敗或畫質卡在 360p。\n"
                "     建議裝 Node.js: winget install OpenJS.NodeJS / brew install node",
                "  ⚠ Node.js not found. yt-dlp may fail YouTube's JS challenge or cap quality\n"
                "     at 360p. Install: winget install OpenJS.NodeJS / brew install node",
            ))
        cache.mkdir(parents=True, exist_ok=True)
        cache_paths = {vid: store.artifact_path(f"youtube-cache:{vid}", cache, f"{vid}.mp4") for vid in jobs}
        missing = [vid for vid in jobs if vid not in existing and not store.artifact_current(
            f"youtube-cache:{vid}", cache_paths[vid], vid, verify_hash=plan.verify_files)]
        if missing:
            with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False, encoding="utf-8") as tf:
                tf.write("\n".join(f"https://youtu.be/{vid}" for vid in missing))
                urls_file = Path(tf.name)
            cmd = [*yt_dlp_command(yt_dlp), *_youtube_cookie_args(cookies_path, cookies_from_browser),
                   *YT_DLP_BASE_ARGS, "--socket-timeout", "30", "--remux-video", "mp4",
                   "-P", str(cache), "-o", "%(id)s.%(ext)s", "-a", str(urls_file)]
            try:
                # A prior invalid final file must not make yt-dlp claim "already downloaded".
                for vid in missing:
                    cache_paths[vid].unlink(missing_ok=True)
                subprocess.run(cmd, timeout=7200)
            except (OSError, subprocess.TimeoutExpired) as exc:
                print(f"  YouTube downloader: {type(exc).__name__}")
            finally:
                urls_file.unlink(missing_ok=True)
        for vid, destinations in jobs.items():
            source = existing.get(vid, cache_paths[vid])
            if not _valid_video(source):
                stats.failed.append(f"YouTube {vid}: no complete playable MP4")
                continue
            if source == cache_paths[vid]:
                store.record_artifact(f"youtube-cache:{vid}", source, vid)
            copied, placed_all = False, True
            for key, target in destinations:
                try:
                    if place_cached_video(source, target):
                        copied = True
                    store.record_artifact(key, target, vid)
                    stats.done += 1
                except OSError as exc:
                    placed_all = False
                    stats.failed.append(f"{target.name}: {exc}")
            if copied and placed_all and source == cache_paths[vid]:
                # No hard links here (e.g. exFAT): don't keep a duplicate in the cache.
                source.unlink(missing_ok=True)
    return stats


def _valid_video(path: Path) -> bool:
    if not path.is_file() or path.stat().st_size < 12:
        return False
    with path.open("rb") as stream:
        if b"ftyp" not in stream.read(64):
            return False
    probe = shutil.which("ffprobe")
    if not probe or _blocked_by_policy("ffprobe"):
        return True
    try:
        result = subprocess.run([probe, "-v", "error", "-show_entries", "stream=codec_type:format=duration",
                                 "-of", "json", str(path)], capture_output=True, text=True, timeout=30)
        data = json.loads(result.stdout)
        return result.returncode == 0 and any(s.get("codec_type") == "video" for s in data.get("streams", []))
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return False


def _course_page(url: str) -> bool:
    path = urllib.parse.urlsplit(url).path
    return origin(url) == origin(DEFAULT_BASE_URL) and (path == "/courses" or path.startswith("/courses/"))


def _maintenance_page(url: str) -> bool:
    parts = urllib.parse.urlsplit(url)
    return parts.hostname == "announcement.dlc.ntu.edu.tw" and parts.path.rstrip("/") == "/maintenance"


def _ensure_logged_in(page, course_id: str | None, sso_timeout_sec: int) -> bool:
    """Navigate to course root (or /courses if no id); if bounced to login, wait for SSO."""
    from playwright.sync_api import TimeoutError as PWTimeout

    target = f"https://{CANVAS_NETLOC}/courses/{course_id}" if course_id else f"https://{CANVAS_NETLOC}/courses"
    print(f"    確認登入狀態 ({target})")
    # `wait_until="commit"` returns as soon as the browser commits the
    # navigation (i.e. starts loading the response) rather than blocking
    # until the DOM is fully built. NTU's SAML chain is multi-redirect and
    # the final SSO login page is JS-heavy enough that `domcontentloaded`
    # would routinely hit the timeout when the saved cookies were stale —
    # which is exactly the moment we need this code path to work, since
    # the whole reason we're here is to refresh those cookies. The
    # subsequent wait_for_url loop is what actually gates on "user has
    # finished logging in", so we don't need goto itself to wait long.
    try:
        page.goto(target, wait_until="commit", timeout=60000)
    except PWTimeout:
        # A redirect can still finish after this timeout. An empty URL or
        # about:blank means there is no committed page to wait on.
        if not page.url or page.url == "about:blank":
            print("    無法連到 NTU COOL — 請檢查網路或稍後再試")
            return False
        print(f"    導向尚未完成,進入登入等待 ({page.url})")

    if not (_course_page(page.url) or _maintenance_page(page.url)):
        from .ntu_credentials import try_saved_login
        if not try_saved_login(page):
            print(f"    需要登入 — 請在開啟的瀏覽器視窗完成 NTU SSO (最多等 {sso_timeout_sec} 秒)")
        try:
            page.wait_for_url(lambda u: _course_page(u) or _maintenance_page(u),
                              timeout=sso_timeout_sec * 1000)
        except PWTimeout:
            print("    SSO 登入逾時")
            return False
    if _maintenance_page(page.url):
        print("    NTU COOL 目前維護中，請稍後再試。")
        return False
    if not _course_page(page.url):
        return False
    print(f"    已登入: {page.url}")
    return True


@dataclass
class BrowserSession:
    """Holds an open Playwright context for cross-stage reuse (avoids repeat SSO)."""
    manager: Any
    pw: Any
    context: Any
    page: Any
    captured: dict[int, dict[str, Any]]
    owns: bool = True

    def close(self) -> None:
        if not self.owns:
            return
        try:
            self.context.close()
        finally:
            self.manager.__exit__(None, None, None)


def open_browser_session(
    *, profile_dir: Path = Path(".secrets/ntu_cool_browser_profile"),
    headless: bool = False,
    course_id: str | None = None,
    sso_timeout_sec: int = 600,
) -> BrowserSession:
    """Open a persistent Chromium context, ensure SSO, return a reusable BrowserSession.

    Caller is responsible for `.close()` (or use `with closing(...)`)."""
    try:
        from playwright.sync_api import sync_playwright as _sp
    except ImportError as exc:
        raise RuntimeError(
            "Playwright is required. From the project root, run:\n"
            f"  {console.python_cmd()} -m pip install -e .\n"
            f"  {console.python_cmd()} -m playwright install chromium"
        ) from exc

    profile_dir.parent.mkdir(parents=True, exist_ok=True)
    manager = _sp()
    pw = manager.__enter__()
    try:
        context = launch_persistent_context(pw, profile_dir, headless=headless, accept_downloads=False)
        page = context.pages[0] if context.pages else context.new_page()
        captured: dict[int, dict[str, Any]] = {}
        page.on("response", make_cool_video_response_handler(captured))
        if not _ensure_logged_in(page, course_id, sso_timeout_sec):
            context.close()
            raise RuntimeError("SSO failed; aborting")
        return BrowserSession(manager=manager, pw=pw, context=context, page=page, captured=captured, owns=True)
    except Exception:
        manager.__exit__(None, None, None)
        raise


def _dump_cookies_to_headers_file(context, headers_path: Path) -> bool:
    """Write Canvas cookies from the live Playwright context to a DevTools-style headers file."""
    cookies = context.cookies(f"https://{CANVAS_NETLOC}")
    pairs = [f"{c['name']}={c['value']}" for c in cookies if c.get("name") and c.get("value")]
    if not pairs:
        return False
    headers_path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(headers_path,
        "\n".join([
            f"{SESSION_ORIGIN_HEADER}: {DEFAULT_BASE_URL}",
            "accept: application/json, text/plain, */*",
            f"cookie: {'; '.join(pairs)}",
            f"referer: https://{CANVAS_NETLOC}/",
            "user-agent: ntu-cool-materials/0.1",
        ]) + "\n",
    )
    # The full cookie jar (with domain/path/secure, plus the NTU SSO cookies)
    # lets a later, invisible Chromium resume this login: Canvas session
    # cookies don't survive the browser exiting, even in a persistent profile.
    try:
        atomic_write_text(storage_state_path(headers_path),
                          json.dumps({"origin": DEFAULT_BASE_URL, "cookies": context.cookies()}))
    except (OSError, TypeError, ValueError):
        pass
    return True


_COOKIE_KEYS = ("name", "value", "domain", "path", "expires", "httpOnly", "secure", "sameSite")


def _saved_login_cookies(headers_path: Path) -> list[dict[str, Any]]:
    """Cookies from the last login, in Playwright `add_cookies` form.

    Prefers the saved cookie jar; falls back to the headers file's `cookie:`
    line (written by older versions and by `ntu-cool-session`)."""
    try:
        headers = read_headers_file(headers_path)
    except (OSError, ValueError):
        return []
    try:
        saved = json.loads(storage_state_path(headers_path).read_text(encoding="utf-8"))
        cookies = [{k: c[k] for k in _COOKIE_KEYS if k in c} for c in saved.get("cookies", [])
                   if isinstance(c, dict) and c.get("name") and c.get("domain")]
        if cookies and origin(saved.get("origin", DEFAULT_BASE_URL)) == origin(DEFAULT_BASE_URL):
            return cookies
    except (OSError, ValueError, AttributeError, TypeError):
        pass
    header = next((v for k, v in headers.items() if k.lower() == "cookie"), "")
    cookies = []
    for part in header.split(";"):
        name, sep, value = part.strip().partition("=")
        if sep and name:
            cookies.append({"name": name, "value": value, "domain": CANVAS_NETLOC, "path": "/", "secure": True})
    return cookies


def _inject_saved_login(context, headers_path: Path | None) -> int:
    """Add the saved login cookies to a fresh context. Returns how many stuck."""
    if headers_path is None:
        return 0
    added = 0
    for cookie in _saved_login_cookies(headers_path):
        try:  # one at a time: a single malformed cookie must not drop the rest
            context.add_cookies([cookie])
            added += 1
        except Exception:
            pass
    return added


def _session_alive(page, course_id: str | None, settle_ms: int = 15000) -> bool:
    """Open the course without waiting on the user; True if Canvas let us in.

    A saved NTU SSO cookie can bounce through the SAML pages on its own, so a
    login URL gets `settle_ms` to resolve before we call the session dead."""
    from playwright.sync_api import TimeoutError as PWTimeout

    target = f"https://{CANVAS_NETLOC}/courses/{course_id}" if course_id else f"https://{CANVAS_NETLOC}/courses"
    try:
        page.goto(target, wait_until="domcontentloaded", timeout=60000)
    except PWTimeout:
        pass
    if _course_page(page.url) or _maintenance_page(page.url):
        return _course_page(page.url)
    try:
        page.wait_for_url(lambda u: _course_page(u) or _maintenance_page(u), timeout=settle_ms)
        return _course_page(page.url)
    except PWTimeout:
        return False


def _capture_cool_video_in_page(page, captured: dict[int, dict[str, Any]],
                                module_item_url: str, video_id: int,
                                sso_timeout_sec: int) -> dict[str, Any] | None:
    """Drive one navigation in an existing page; return the captured view JSON or None."""
    from playwright.sync_api import TimeoutError as PWTimeout

    for attempt in range(4):
        captured.clear()
        try:
            page.goto(module_item_url, wait_until="domcontentloaded", timeout=90000)
        except Exception as exc:
            print(f"      嘗試 {attempt+1} 載入失敗: {exc}")
            continue
        if LOGIN_RE.search(page.url):
            print(f"      嘗試 {attempt+1} 被導回登入頁,等待 SSO")
            try:
                page.wait_for_url(lambda u: not LOGIN_RE.search(u), timeout=sso_timeout_sec * 1000)
            except PWTimeout:
                return None
        deadline = time.monotonic() + 25
        while time.monotonic() < deadline:
            if video_id in captured:
                return captured[video_id]
            page.wait_for_timeout(1500)
        print(f"      嘗試 {attempt+1}: 還沒擷取到 view JSON")
    return None


def _cool_video_targets(plan: CoursePlan) -> list[tuple[WeekPlan, dict[str, Any], int]]:
    targets: list[tuple[WeekPlan, dict[str, Any], int]] = []
    for week in plan.weeks:
        for item in week.items:
            if item.get("type") != "ExternalTool":
                continue
            ext = str(item.get("external_url") or "")
            m = re.search(r"cool-video\.dlc\.ntu\.edu\.tw/.*?/videos/(\d+)", ext)
            if not m:
                continue
            targets.append((week, item, int(m.group(1))))
    return targets


def _pending_cool_videos(plan):
    pending, skipped = [], 0
    with _manifest(plan) as store:
        for week, item, vid in _cool_video_targets(plan):
            key = _artifact_key(week, "cool-video", vid)
            title = str(item.get("title") or f"video-{vid}")
            target = store.artifact_path(key, week.week_dir, f"{sanitize_teacher_title(title)}.mp4")
            item["_local_path"] = str(target)
            version = json.dumps([vid, item.get("updated_at")])
            if (store.artifact_current(key, target, version, verify_hash=plan.verify_files)
                    or _adopt_legacy(store, key, target, version, _valid_video)):
                skipped += 1
            else:
                pending.append((week, item, vid, key, target, version))
    return pending, skipped


def _download_youtube_backed_video(plan: CoursePlan, youtube_id: str, target: Path, *,
                                   yt_dlp: str, yt_cookies: Path | None,
                                   cookies_from_browser: str | None = None) -> None:
    """Some cool-video entries only wrap a YouTube upload (sourceUri is a
    watch page, no altSourceUri); fetching that URL returns YouTube's HTML."""
    if not _tool_usable("ffmpeg"):
        raise DownloadError("ffmpeg is required to merge YouTube video and audio")
    cache = youtube_cache_dir(plan.course_dir)
    cache.mkdir(parents=True, exist_ok=True)
    source = cache / f"{youtube_id}.mp4"
    if not _valid_video(source):
        source.unlink(missing_ok=True)
        if yt_cookies is not None:
            cookie_args = _youtube_cookie_args(yt_cookies, cookies_from_browser)
        else:
            cookie_args = ["--cookies-from-browser", cookies_from_browser] if cookies_from_browser else []
        cmd = [*yt_dlp_command(yt_dlp), *cookie_args, *YT_DLP_BASE_ARGS, "--socket-timeout", "30", "--remux-video", "mp4",
               "-P", str(cache), "-o", "%(id)s.%(ext)s", f"https://youtu.be/{youtube_id}"]
        try:
            subprocess.run(cmd, timeout=7200)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise DownloadError(f"YouTube downloader: {type(exc).__name__}") from None
        if not _valid_video(source):
            raise DownloadError(f"YouTube {youtube_id}: no complete playable MP4")
    place_cached_video(source, target)


def capture_and_download_cool_videos_in_page(plan: CoursePlan, page, captured: dict[int, dict[str, Any]],
                                              *, course_id: str, sso_timeout_sec: int = 600,
                                              yt_dlp: str = "yt-dlp", yt_cookies: Path | None = None) -> StageStats:
    pending, skipped = _pending_cool_videos(plan)
    stats = StageStats(skipped=skipped)
    with _manifest(plan) as store:
        for week, item, vid, key, target, version in pending:
            module_url = f"https://{CANVAS_NETLOC}/courses/{course_id}/modules/items/{item['id']}"
            for attempt in range(2):
                try:
                    view = _capture_cool_video_in_page(page, captured, module_url, vid, sso_timeout_sec)
                    if not view:
                        raise DownloadError("Could not capture video source")
                    url = view.get("altSourceUri") or view.get("sourceUri")
                    if not url:
                        raise DownloadError("Video has no download source")
                    youtube_ids = extract_youtube_ids(url)
                    if youtube_ids:
                        try:
                            _download_youtube_backed_video(plan, youtube_ids[0], target,
                                                           yt_dlp=yt_dlp, yt_cookies=yt_cookies)
                        except DownloadError as exc:
                            message = f"{target.name}: {type(exc).__name__}: {exc}"
                            stats.failed.append(message)
                            # A private / members-only upload may still work with the
                            # browser's YouTube login: see retry_youtube_backed_cool_videos.
                            stats.youtube_retry.append((youtube_ids[0], key, target, version, message))
                            break
                        store.record_artifact(key, target, version)
                        stats.done += 1
                        break
                    if urllib.parse.urlsplit(url).path.lower().endswith((".mpd", ".m3u8")):
                        raise DownloadError("Only a streaming manifest is available; no downloadable MP4")
                    _download_signed_url(url, target)
                    store.record_artifact(key, target, version)
                    stats.done += 1
                    break
                except urllib.error.HTTPError as exc:
                    if exc.code in {401, 403} and attempt == 0:
                        continue  # refresh the signed URL, not the expired URL itself
                    stats.failed.append(f"{target.name}: HTTP {exc.code}")
                    break
                except Exception as exc:
                    stats.failed.append(f"{target.name}: {type(exc).__name__}: {exc}")
                    break
    return stats


def retry_youtube_backed_cool_videos(plan: CoursePlan, stats: StageStats, *, yt_dlp: str,
                                     yt_cookies: Path | None, browsers: list[str]) -> int:
    """Retry cool-videos that wrap a YouTube upload with each browser's YouTube
    login, like the YouTube stage does. No Canvas page is needed: the YouTube
    id was captured on the first pass. Returns how many were recovered."""
    recovered = 0
    with _manifest(plan) as store:
        for browser_name in browsers:
            if not stats.youtube_retry:
                break
            print(t(f"\n  → 用 {browser_name} 裡已登入的 YouTube 帳號重試上課影片...",
                    f"\n  → retrying cool-videos using your {browser_name} YouTube login..."))
            remaining = []
            for youtube_id, key, target, version, message in stats.youtube_retry:
                try:
                    _download_youtube_backed_video(plan, youtube_id, target, yt_dlp=yt_dlp,
                                                   yt_cookies=yt_cookies, cookies_from_browser=browser_name)
                except DownloadError:
                    remaining.append((youtube_id, key, target, version, message))
                    continue
                store.record_artifact(key, target, version)
                stats.done += 1
                stats.failed.remove(message)
                recovered += 1
            stats.youtube_retry = remaining
    return recovered


def make_cool_video_response_handler(captured: dict[int, dict[str, Any]]):
    """Return a Playwright response handler that captures /api/.../videos/:id/view JSON bodies."""
    def _on_response(resp):
        if not COOL_VIDEO_VIEW_RE.search(resp.url.split("?", 1)[0]):
            return
        try:
            ct = resp.headers.get("content-type", "")
            if "json" not in ct:
                return
            v = json.loads(resp.text())
            captured[int(v["videoId"])] = v
        except Exception:
            pass
    return _on_response


def capture_and_download_cool_videos(
    plan: CoursePlan, *,
    course_id: str,
    profile_dir: Path = Path(".secrets/ntu_cool_browser_profile"),
    headless: bool = False,
    sso_timeout_sec: int = 600,
    headers_path: Path | None = None,
    yt_dlp: str = "yt-dlp",
    yt_cookies: Path | None = None,
) -> StageStats:
    """Capture pending videos in a headless context using the saved login.

    If the course probe fails, close that context and retry in a visible SSO
    window unless headless was requested. Save cookies after capture when a
    headers path was supplied.
    """
    stats = StageStats()
    pending, stats.skipped = _pending_cool_videos(plan)
    if not pending:
        return stats
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        stats.failed.extend(f"{job[4].name}: Playwright is required" for job in pending)
        print("    缺少 Playwright。請在專案根目錄依序執行：\n"
              f"      {console.python_cmd()} -m pip install -e .\n"
              f"      {console.python_cmd()} -m playwright install chromium")
        return stats
    profile_dir.parent.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        def launch(visible: bool):
            ctx = launch_persistent_context(p, profile_dir, headless=not visible, accept_downloads=False)
            _inject_saved_login(ctx, headers_path)
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            captured: dict[int, dict[str, Any]] = {}
            page.on("response", make_cool_video_response_handler(captured))
            return ctx, page, captured

        ctx, page, captured = launch(visible=False)
        if not _session_alive(page, course_id):
            ctx.close()
            if headless:
                print(t("    NTU COOL 登入已過期,--headless 模式無法開視窗重新登入",
                        "    NTU COOL login expired; --headless can't open a window to log in again"))
                stats.failed.extend(f"{job[4].name}: SSO login required" for job in pending)
                return stats
            print(t("    NTU COOL 登入已過期,開啟瀏覽器重新登入",
                    "    NTU COOL login expired; opening a browser to log in again"))
            ctx, page, captured = launch(visible=True)
            if not _ensure_logged_in(page, course_id, sso_timeout_sec):
                ctx.close()
                stats.failed.extend(f"{job[4].name}: SSO login failed" for job in pending)
                return stats
        try:
            stats = capture_and_download_cool_videos_in_page(
                plan, page, captured, course_id=course_id, sso_timeout_sec=sso_timeout_sec,
                yt_dlp=yt_dlp, yt_cookies=yt_cookies)
        finally:
            if headers_path is not None:
                _dump_cookies_to_headers_file(ctx, headers_path)
            ctx.close()
    return stats


# ---- top-level orchestrator ----

def _build_session_client_from_file(headers_path: Path, base_url: str) -> CanvasSessionClient:
    return CanvasSessionClient(base_url=base_url, headers=read_headers_file(headers_path, base_url=base_url))


def _write_course_overview(plan: CoursePlan) -> Path:
    """Index course items by module, including local paths and external links."""
    from datetime import datetime, timezone
    course_name = plan.course.get("name") or plan.course.get("course_code") or plan.course_id
    lines: list[str] = [
        f"# {course_name}",
        "",
        f"**課程 ID:** {plan.course_id}",
        f"**產生時間:** {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}",
        f"**路徑:** `{plan.course_dir}`",
        "",
        "課程教材總覽。單元與課程檔案列在下方，並連到本機檔案。",
        "",
    ]
    if (plan.course_dir / "announcements" / "announcements.md").exists():
        lines.extend(["## 公告", "", "- [公告全文](announcements/announcements.md)", ""])
    sections = plan.weeks + ([plan.course_files] if plan.course_files else [])
    for week in sections:
        module_name = week.module.get("name") or week.label
        lines.append(f"## {module_name}")
        lines.append("")
        # Group items by file type for readability, routing by what is on disk.
        per_type: dict[str, list[str]] = {"pdf": [], "md": [], "mp4": [], "other": []}
        icons = {"pdf": "📄", "md": "📝", "mp4": "🎬", "other": "📎"}
        for item in week.items:
            kind = item.get("type")
            title = str(item.get("title") or "").strip()
            # Stages record the reserved path; a skipped stage leaves only the default name.
            local = item.get("_local_path")
            if local:
                path = Path(local)
            elif kind == "File":
                path = week.week_dir / _file_item_target_name(item)
            elif kind == "Page":
                path = week.week_dir / f"{sanitize_teacher_title(title)}.md"
            elif kind in {"ExternalUrl", "ExternalTool"}:
                path = week.week_dir / f"{sanitize_teacher_title(title)}.mp4"
            else:
                continue
            if path.is_file():
                rel = path.resolve().relative_to(plan.course_dir.resolve()).as_posix()
                suffix = path.suffix.lower().lstrip(".")
                bucket = suffix if suffix in per_type else "other"
                per_type[bucket].append(f"- {icons[bucket]} [{title}](<{urllib.parse.quote(rel)}>)")
            elif kind in {"ExternalUrl", "ExternalTool"}:
                raw = str(item.get("external_url") or item.get("url") or "")
                per_type["other"].append(f"- 🔗 [{title}](<{raw}>)")
        for key in ("pdf", "md", "mp4", "other"):
            for line in per_type[key]:
                lines.append(line)
        if not any(per_type.values()):
            lines.append("- _(沒有可下載的內容)_")
        lines.append("")
    overview = plan.course_dir / "course_overview.md"
    atomic_write_text(overview, "\n".join(lines))
    return overview




def download_course(
    *, course_id: str, output_dir: Path,
    base_url: str = f"https://{CANVAS_NETLOC}",
    headers_path: Path = Path(".secrets/ntu_cool_headers.txt"),
    refresh_session: bool = False,
    client: CanvasSessionClient | None = None,
    browser: BrowserSession | None = None,
    yt_cookies: Path | None = None, yt_dlp: str = "yt-dlp",
    profile_dir: Path = Path(".secrets/ntu_cool_browser_profile"),
    headless: bool = False,
    skip_pdfs: bool = False, skip_pages: bool = False,
    skip_announcements: bool = False,
    skip_youtube: bool = False, skip_cool_videos: bool = False,
    sso_timeout_sec: int = 600,
    verify_files: bool = False, workers: int = 3,
) -> CoursePlan:
    """Download course materials using the saved headers or a refreshed session.

    A supplied browser is reused. Refresh or recovery can open a browser owned
    by this run. Without an open browser, the cool-video stage manages its own
    headless probe and optional visible login.
    """
    owns_browser = False
    native_origin = origin(base_url) == origin(DEFAULT_BASE_URL)
    try:
        if browser is None and refresh_session:
            if not native_origin:
                raise RuntimeError("For a custom Canvas origin, refresh login with ntu-cool-session.")
            browser = open_browser_session(
                profile_dir=profile_dir, headless=headless,
                course_id=course_id, sso_timeout_sec=sso_timeout_sec,
            )
            owns_browser = True
            if not _dump_cookies_to_headers_file(browser.context, headers_path):
                raise RuntimeError("No NTU COOL cookies found in browser context")
            print(f"  已寫入登入憑證 → {headers_path}")

        if client is None:
            client = _build_session_client_from_file(headers_path, base_url)

        course_stats = CourseStats()

        def _try_recover_session() -> bool:
            """Recover the native Canvas session in visible mode, opening SSO if needed."""
            nonlocal client, browser, owns_browser
            # A headless browser can't show the SSO page, so "waiting for the
            # user to log in" would just hang for sso_timeout_sec.
            if headless or not native_origin:
                return False
            try:
                if browser is None:
                    if not console.stdin_is_interactive():
                        return False
                    browser = open_browser_session(profile_dir=profile_dir, headless=False,
                                                   course_id=course_id, sso_timeout_sec=sso_timeout_sec)
                    owns_browser = True
                print(t(
                    "  → NTU COOL 登入已過期,在同一個瀏覽器重新登入...",
                    "  → NTU COOL session expired, re-authenticating in the open browser...",
                ))
                if not _ensure_logged_in(browser.page, course_id, sso_timeout_sec):
                    return False
            except Exception as exc:  # Playwright missing/crashed, or SSO aborted
                print(t(f"  ✗ 無法重新登入: {exc}", f"  ✗ could not re-authenticate: {exc}"))
                return False
            if not _dump_cookies_to_headers_file(browser.context, headers_path):
                return False
            client = _build_session_client_from_file(headers_path, base_url)
            print(t("  ✓ 重新登入完成,重試此階段", "  ✓ session refreshed, retrying"))
            return True

        def _api_call_with_session_retry(fn, label: str):
            """Run fn(client). If it raises SessionExpiredError, try to refresh
            the login once and retry. Used for any API call that touches
            cool.ntu.edu.tw — list_modules, get_course, files, pages, etc."""
            try:
                return fn(client)
            except SessionExpiredError as exc:
                print(t(f"  ⚠ {label}: {exc}", f"  ⚠ {label}: {exc}"))
                if not _try_recover_session():
                    raise
                result = fn(client)
                previous = getattr(exc, "stage_stats", None)
                if previous is not None and isinstance(result, StageStats):
                    result.done += previous.done
                    result.skipped = max(0, result.skipped - previous.done)
                return result

        # plan_course hits the Canvas API (get_course + list_modules); if the
        # session expired between list_courses (in cli.py) and now, this is
        # where we'd see the 401. Wrap it in the same retry helper as the
        # download stages.
        plan = _api_call_with_session_retry(
            lambda c: plan_course(c, course_id, output_dir),
            t("plan", "plan"),
        )
        plan.verify_files = verify_files
        plan.workers = max(1, min(workers, 4))
        plan.stats = course_stats
        print(t(f"課程: {plan.course.get('name')!r}", f"Course: {plan.course.get('name')!r}"))
        print(t(f"存放位置: {plan.course_dir}", f"Output: {plan.course_dir}"))
        print(t(
            f"有教材的週次: {[w.label for w in plan.weeks]}",
            f"Weeks with content: {[w.label for w in plan.weeks]}",
        ))

        def _run_with_session_retry(stage_fn, label: str) -> StageStats:
            return _api_call_with_session_retry(stage_fn, label)

        for stage, disabled in ((course_stats.announcements, skip_announcements), (course_stats.pdfs, skip_pdfs),
                                (course_stats.pages, skip_pages), (course_stats.youtube, skip_youtube),
                                (course_stats.cool_videos, skip_cool_videos)):
            stage.disabled = disabled

        if not skip_announcements:
            print(t("\n[1/5] 公告內容", "\n[1/5] Announcements"))
            course_stats.announcements = _run_with_session_retry(
                lambda c: save_announcements(plan, c), t("公告", "announcements")
            )
            print(t(
                f"  儲存 {course_stats.announcements.done}、跳過 {course_stats.announcements.skipped}、失敗 {len(course_stats.announcements.failed)}",
                f"  saved {course_stats.announcements.done}, skipped {course_stats.announcements.skipped}, failed {len(course_stats.announcements.failed)}",
            ))

        if not skip_pdfs:
            print(t("\n[2/5] 單元教材檔案", "\n[2/5] Module files"))
            course_stats.pdfs = _run_with_session_retry(
                lambda c: download_files(plan, c), t("檔案", "files")
            )
            print(t(
                f"  下載 {course_stats.pdfs.done}、跳過 {course_stats.pdfs.skipped}、失敗 {len(course_stats.pdfs.failed)}",
                f"  downloaded {course_stats.pdfs.done}, skipped {course_stats.pdfs.skipped}, failed {len(course_stats.pdfs.failed)}",
            ))
        if not skip_pages:
            print(t("\n[3/5] Page 內容", "\n[3/5] Pages"))
            course_stats.pages = _run_with_session_retry(
                lambda c: save_pages(plan, c, course_id), t("Page", "pages")
            )
            print(t(
                f"  儲存 {course_stats.pages.done}、跳過 {course_stats.pages.skipped}、失敗 {len(course_stats.pages.failed)}",
                f"  saved {course_stats.pages.done}, skipped {course_stats.pages.skipped}, failed {len(course_stats.pages.failed)}",
            ))
        if not skip_pdfs:
            print(t("\n補抓課程「檔案」區", "\nChecking the course Files area"))
            extra_files = _run_with_session_retry(
                lambda c: download_files(plan, c, only_unlisted=True), t("課程檔案清單", "course file listing")
            )
            course_stats.pdfs.done += extra_files.done
            course_stats.pdfs.skipped += extra_files.skipped
            course_stats.pdfs.failed.extend(extra_files.failed)
        if not (skip_pdfs and skip_pages):
            # Files and Page attachments can both bring in Excel workbooks.
            excel = convert_spreadsheets(plan.course_dir)
            if excel.done or excel.failed:
                print(t(f"  Excel → Markdown: 轉換 {excel.done}、失敗 {len(excel.failed)}",
                        f"  Excel → Markdown: {excel.done} converted, {len(excel.failed)} failed"))
        if yt_cookies is None:
            yt_cookies = secrets_dir() / "youtube_cookies.txt"
        from .update_check import resolve_yt_dlp
        yt_dlp = resolve_yt_dlp(yt_dlp) or yt_dlp
        # Set once the user agrees to the browser-login retry, so the cool-video
        # stage reuses that answer instead of asking twice.
        youtube_login_consent = False

        if not skip_youtube:
            print(t("\n[4/5] YouTube 影片", "\n[4/5] YouTube videos"))
            yt_cookies_path = yt_cookies
            has_youtube_jobs = any(
                extract_youtube_ids(str(item.get("external_url") or item.get("url") or ""))
                for week in plan.weeks
                for item in week.items
            )
            # None leaves the post-failure update offer available. A bool records a prior outcome.
            yt_dlp_update = None
            if has_youtube_jobs:
                from .update_check import ensure_yt_dlp_updated
                try:
                    yt_dlp_update = ensure_yt_dlp_updated(yt_dlp=yt_dlp, max_age_days=60)
                except Exception:
                    pass

            # Try the available credentials first so a browser-login offer can
            # report the number of videos that actually failed.
            course_stats.youtube = download_youtube(
                plan, cookies_path=yt_cookies_path, yt_dlp=yt_dlp
            )
            failed_count = len(course_stats.youtube.failed)
            # A post-failure update or login retry also needs a usable ffmpeg.
            retryable = _tool_usable("ffmpeg")
            if failed_count > 0 and retryable and yt_dlp_update is None:
                from .update_check import confirm_yt_dlp_update, update_yt_dlp
                ok = False
                if confirm_yt_dlp_update(t(
                    f"\n  有 {failed_count} 個 YouTube 影片下載失敗；更新 yt-dlp 後重試嗎？[Y/n]: ",
                    f"\n  {failed_count} YouTube download(s) failed; update yt-dlp and retry? [Y/n]: ",
                )):
                    ok, msg = update_yt_dlp(yt_dlp=yt_dlp)
                    print(f"  [yt-dlp] {msg}")
                if ok:
                    retry_stats = download_youtube(
                        plan, cookies_path=yt_cookies_path, yt_dlp=yt_dlp
                    )
                    course_stats.youtube.done += retry_stats.done
                    course_stats.youtube.failed = retry_stats.failed
                    failed_count = len(course_stats.youtube.failed)
            youtube_login_consent = retryable and maybe_retry_youtube_with_login(yt_cookies_path, failed_count)
            if youtube_login_consent:
                # Retry the still-missing videos using the YouTube login from
                # the user's normal browser(s). We try each installed browser
                # in turn and stop as soon as nothing's left failing — the
                # right browser is whichever one the user is signed into.
                # Already-downloaded videos are skipped on each pass, so the
                # retries only re-attempt what's still missing (no double work,
                # and `done` can't double-count: each pass derives `done` from
                # files that weren't on disk when that pass started).
                browsers = _installed_cookie_browsers()
                if not browsers:
                    print(t(
                        "  ⚠ 找不到可讀取的瀏覽器(Chrome / Edge / Firefox …),無法用瀏覽器登入重試。",
                        "  ⚠ No readable browser (Chrome / Edge / Firefox …) found; can't retry with browser cookies.",
                    ))
                for br in browsers:
                    print(t(
                        f"\n  → 用 {br} 裡已登入的 YouTube 帳號重試失敗的下載...",
                        f"\n  → retrying failed downloads using your {br} YouTube login...",
                    ))
                    retry_stats = download_youtube(
                        plan, cookies_path=yt_cookies_path, yt_dlp=yt_dlp,
                        cookies_from_browser=br,
                    )
                    course_stats.youtube.done += retry_stats.done
                    course_stats.youtube.failed = retry_stats.failed
                    if not retry_stats.failed:
                        break
                recovered = failed_count - len(course_stats.youtube.failed)
                if recovered > 0:
                    print(t(
                        f"  ✓ 重試救回 {recovered} 個影片",
                        f"  ✓ retry recovered {recovered} video(s)",
                    ))
                if browsers and course_stats.youtube.failed:
                    print(t(
                        "  ⚠ 還是有影片抓不到。若你用 Chrome / Edge,請先完全關閉瀏覽器再重跑一次"
                        "(瀏覽器執行中時 cookie 檔會被鎖住)。",
                        "  ⚠ Some videos still failed. If you use Chrome / Edge, fully close it and re-run "
                        "— its cookie database is locked while the browser is open.",
                    ))

        if not skip_cool_videos:
            print(t("\n[5/5] NTU 上課影片 (cool-video)", "\n[5/5] NTU CDN videos (cool-video)"))
            try:
                if browser is not None:
                    course_stats.cool_videos = capture_and_download_cool_videos_in_page(
                        plan, browser.page, browser.captured,
                        course_id=course_id, sso_timeout_sec=sso_timeout_sec,
                        yt_dlp=yt_dlp, yt_cookies=yt_cookies,
                    )
                else:
                    course_stats.cool_videos = capture_and_download_cool_videos(
                        plan, course_id=course_id, profile_dir=profile_dir, headless=headless,
                        sso_timeout_sec=sso_timeout_sec, headers_path=headers_path if native_origin else None,
                        yt_dlp=yt_dlp, yt_cookies=yt_cookies,
                    )
            except Exception as exc:
                # Playwright raises its own Error type (Chromium missing, profile
                # locked by another window, network down mid-launch). Record it
                # as a stage failure so the rest of the course still finishes.
                course_stats.cool_videos.failed.append(f"cool-video: {type(exc).__name__}: {exc}")
                print(t(f"  ✗ 上課影片階段失敗: {exc}", f"  ✗ cool-video stage failed: {exc}"))
            # Cool-videos that are really private / members-only YouTube uploads
            # get the same browser-login retry as the YouTube stage.
            youtube_backed = course_stats.cool_videos.youtube_retry
            if youtube_backed and _tool_usable("ffmpeg") and (
                    youtube_login_consent or maybe_retry_youtube_with_login(yt_cookies, len(youtube_backed))):
                recovered = retry_youtube_backed_cool_videos(
                    plan, course_stats.cool_videos, yt_dlp=yt_dlp, yt_cookies=yt_cookies,
                    browsers=_installed_cookie_browsers())
                if recovered:
                    print(t(f"  ✓ 重試救回 {recovered} 個上課影片", f"  ✓ retry recovered {recovered} cool-video(s)"))

        # Per-course overview at the course root.
        try:
            overview_path = _write_course_overview(plan)
            print(t(f"\n  目錄: {overview_path.name}", f"\n  overview: {overview_path.name}"))
        except Exception as exc:
            print(t(f"\n  (無法產生目錄: {exc})", f"\n  (could not write overview: {exc})"))

        # Summary
        print("\n" + "=" * 60)
        print(t("完成。", "Done.") if course_stats.successful else t("部分下載失敗。", "Completed with failures."))
        def _row(label_zh: str, label_en: str, s: StageStats) -> None:
            if s.disabled:
                print(t(f"  {label_zh} 使用者略過", f"  {label_en} disabled by user"))
                return
            line_zh = f"  {label_zh}  新增 {s.done}、跳過 {s.skipped}、失敗 {len(s.failed)}"
            line_en = f"  {label_en}  {s.done} new, {s.skipped} skipped, {len(s.failed)} failed"
            print(t(line_zh, line_en))
        _row("公告:      ", "Announcements:", course_stats.announcements)
        _row("檔案:      ", "Files:       ", course_stats.pdfs)
        _row("Page:      ", "Pages:       ", course_stats.pages)
        _row("YouTube:   ", "YouTube:     ", course_stats.youtube)
        _row("上課影片:  ", "Cool-video:  ", course_stats.cool_videos)
        all_failures = (course_stats.announcements.failed + course_stats.pdfs.failed + course_stats.pages.failed
                        + course_stats.youtube.failed + course_stats.cool_videos.failed)
        if all_failures:
            print(t(f"\n失敗清單 ({len(all_failures)} 筆):", f"\nFailures ({len(all_failures)}):"))
            for f in all_failures:
                print(f"  ✗ {f}")
        print(t(
            f"\n檔案存放位置:\n  {plan.course_dir.resolve()}",
            f"\nFiles saved to:\n  {plan.course_dir.resolve()}",
        ))
        atomic_write_text(plan.course_dir / ".download_report.json", json.dumps(
            {"successful": course_stats.successful, "stages": {
                name: {"done": stage.done, "skipped": stage.skipped, "failed": stage.failed, "disabled": stage.disabled}
                for name, stage in vars(course_stats).items()}}, ensure_ascii=False, indent=2))
        # Older versions wrote a visible report; the hidden one above replaces it.
        (plan.course_dir / "download_report.json").unlink(missing_ok=True)
        hide_dot_entries(plan.course_dir)
        return plan
    finally:
        if owns_browser and browser is not None:
            browser.close()
