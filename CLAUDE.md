# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project

Python 3.11+ CLI for downloading NTU COOL (Canvas) materials to a local course library and optionally importing them into NotebookLM. Canvas API calls use `urllib`. The DASH downloader uses `requests`. Playwright and yt-dlp are runtime dependencies. The Chromium browser binary is installed separately.

## Commands

```powershell
# Install editable + tests
pip install -e .
python -m unittest discover -s tests        # all tests
python -m unittest tests.test_storage       # single module
python -m unittest tests.test_storage.StorageTests.test_sanitize_component_replaces_unsafe_chars

# Static checks used by CI
python -m pip install -e ".[lint]"
python -m ruff check src tests
python -m mypy
python -m unittest discover -s tests -p test_core_imports.py

# Install the browser binary for Playwright
python -m playwright install chromium
```

Ruff checks unused imports, undefined names and unused `noqa` directives in the package and tests. Strict mypy checks `src/ntu_cool_materials/core` against Python 3.11. The import-direction test reads the Python syntax tree, including function-local imports. Core may import the standard library and other core modules. The tests use `unittest`.

Console entry points (declared in `pyproject.toml`):
- `ntu-cool-materials` → `ntu_cool_materials.cli:main` (subcommands: `courses`, `sync`, `announcements`, `download-course`, `pick`, `notebooklm`, `doctor`)
- `get-class-material` → `ntu_cool_materials.cli:pick_main` (shortcut alias for `ntu-cool-materials pick`; school-neutral primary name)
- `ntu-cool-gcm`       → same as `get-class-material` (legacy name, kept for existing users)
- `ntu-cool-video`     → `cool_video:main` (DASH `.m4s` segment downloader + ffmpeg mux)
- `ntu-cool-session`   → `browser_session:main` (Playwright session refresh)
- `youtube-cookies`    → `youtube_cookies:main` (export YouTube cookies for yt-dlp)

`download-course` is the bulk per-course pipeline (one command does the whole course):
```powershell
ntu-cool-materials download-course --course-id 60804 --refresh-session
# subsequent runs are idempotent; --refresh-session only needed if Canvas session expired
ntu-cool-materials download-course --course-id 60804
# stages can be skipped:
ntu-cool-materials download-course --course-id 60804 --skip-cool-videos --skip-youtube
```

`get-class-material` and `ntu-cool-gcm` start the interactive `pick` command. When `pick` opens a visible SSO window, it saves the login and closes the window after listing courses. Each selected course reads the saved headers. See the cool-video stage below for browser reuse and login recovery.

On Windows, run with `python -X utf8 -m ntu_cool_materials download-course ...` if the console mojibakes CJK course/file names.

macOS is supported alongside Windows. Mac-specific behaviour: user-facing hints go through `console.python_cmd()` / `console.pip_install_hint()` (macOS has `python3`, not `python`), so never hardcode `python -m pip` in a message; `cli.main` calls `console.ensure_ca_bundle()`, which points `SSL_CERT_FILE` at certifi when python.org's macOS build has an empty trust store (no "Install Certificates" run yet); node/ffmpeg install via Homebrew (doctor hints at https://brew.sh when `brew` is missing); and the yt-dlp cookie retry also tries Vivaldi/Opera/Chromium/Safari on macOS (Safari needs Full Disk Access for the terminal). On this dev Mac, the system Python is 3.9, so use the uv-managed `.venv` (`.venv/bin/python -m unittest discover -s tests`).

`sync` uses a Canvas token (`NTU_COOL_TOKEN` by default). `courses` and `announcements` accept a token or `--headers-file`. `pick` and `download-course` use saved session headers.

## Architecture

The package is built around **two parallel Canvas API clients** that the CLI picks between based on which auth method the user provides. Most other modules consume either client via duck-typed methods (`list_courses`, `list_course_announcements`, `list_paginated`):

- `canvas_client.CanvasClient` — Bearer-token auth. Sends `Authorization: Bearer <token>` only to the configured Canvas origin (see `_is_canvas_origin`); strips it on cross-origin file-download redirects (uses `NoRedirectHandler` + manual redirect loop) so signed CDN URLs don't leak the token.
- `session_client.CanvasSessionClient` — cookie/header auth. Reads a DevTools "Copy request headers" blob via `read_headers_file` (also accepts JSON), drops hop-by-hop headers, and reuses them. The token-equivalent secret is the `cookie` header in `.secrets/ntu_cool_headers.txt`; `redact_headers` exists specifically to keep it out of logs.

Both delegate `list_paginated` to `canvas_pagination.iter_page_items`, which walks RFC 5988 `Link: rel="next"` headers (`parse_link_header`), with a 50ms sleep between pages.

`courses` and `announcements` can refresh their session headers through `browser_session.refresh_headers_file` before fetching course data.

**Idempotent file sync** lives in `sync.py` + `storage.py`:
- `sync.sync_course_materials` writes `materials/<course-name> (<id>)/modules.json` (modules + items, calling `list_module_items` per module if items aren't inlined), then iterates `list_course_files` and downloads each through `client.download_file_url`.
- `storage.ManifestStore` is a SQLite (`materials/.ntu_cool_materials.sqlite3`) keyed by Canvas `file_id`. `needs_download` re-fetches when `updated_at` or `size` differs from the manifest, OR the local file is missing. After a successful download, `upsert_file` records `sha256` of the bytes on disk.
- `core.naming.sanitize_component` applies NFKC normalization and Windows filename rules to course directories and sync file names. Bulk module materials use `core.naming.sanitize_teacher_title` to preserve fullwidth punctuation.
- `core.naming.course_directory_name(course)` is the single source of truth for the per-course directory name: `<course-name> (<id>)` after sanitization, max 160 chars. Used by `sync.sync_course_materials` and `announcements.write_announcements`. If you add a new subcommand that writes per-course, use this — never hardcode `course_<id>`.

**Browser-session bridge.** `browser_session.refresh_headers_file` opens a persistent Playwright context, handles login when needed and probes the configured Canvas API before writing origin-bound headers and a sibling cookie jar. The standalone `ntu-cool-session --headless` returns 2 when the refresh reports that login is required.

**Announcements.** `html_to_markdown` and `canvas_file_links` use `MarkdownExtractor` to render post bodies and discover linked files. `write_announcements` writes `announcements.md` and `.announcements.json` under `<output>/<course_directory_name>/announcements/` and handles the legacy visible JSON filename.

**Video pipeline** is independent from the Canvas sync:
- `cool_video.py` (`ntu-cool-video`) downloads NTU COOL CDN MPEG-DASH segments. Walks `video-{quality}-{n}.m4s` and `audio-{quality}-{n}.m4s` from `--start` until a 404, merges them in order, then `ffmpeg -c copy` mux to MP4. `normalize_base_url` accepts either a directory URL or any numbered segment URL (it strips back to the parent). If the first numbered segment lacks an `ftyp/moov` box (`probe_top_level_mp4_boxes`), the user must supply an explicit init segment via `--video-init-url` / `--audio-init-url` (or `*-init-file`); the ffmpeg error path explicitly hints at this.
- `core/naming.py` associates YouTube IDs with the first matching module title. `sanitize_teacher_title` uses NFC to preserve fullwidth punctuation. `extract_youtube_ids` handles concatenated Canvas URLs.
- `course_pipeline.YT_DLP_BASE_ARGS` sets Node as the JavaScript runtime, selects YouTube player clients and prefers MP4 video with M4A audio. `_youtube_cookie_args` supplies authentication separately.

**Bulk per-course pipeline** (`course_pipeline.download_course`)

1. `plan_course` fetches the course and its modules, fetching missing or incomplete item lists separately. It creates the course directory and reserves stable module paths in the course SQLite manifest. Labels use `week<number>` when the module name contains a week number and `module<index>` otherwise. In each selected module, legacy `files/`, `pages/` and `videos/` files move up one level when their destinations are available. Collisions stay in place. **Preserve `metadata/`** because it may hold the only remaining source-to-file mapping.
2. `save_announcements` refreshes the announcement snapshot and attachments. `download_files` fetches module files through the session client’s origin-scoped download path.
3. `save_pages` — Pages via `/api/v1/courses/:id/pages/:url`, saved as `<safe_title>.md` (HTML → Markdown via `html_to_markdown`); linked Canvas files land in `<week>/attachments/`.
4. After Pages, the pipeline checks the course Files area for additional visible files when the file stage is enabled. `download_youtube` batches missing YouTube IDs across weeks into `<course>/.media-cache/youtube/`, then links or copies each video to its titled destinations. An existing cookies file takes precedence. Otherwise the first attempt uses public access. After a failure, an interactive user can authorize a retry using browser cookies if ffmpeg is usable.
5. The cool-video stage reuses an open course browser when available. Otherwise, if videos are pending, it opens a headless context, restores saved cookies and probes the course. If the probe fails, it closes that context. `--headless` records a failure at that point. In visible mode a fresh window handles SSO. The probe can fail during maintenance or a timeout as well as after expiry. The stage captures each pending item's view response and selects `altSourceUri` or `sourceUri`, including YouTube-backed sources.

When the file or Page stage is enabled, `spreadsheet_convert.convert_spreadsheets` uses the optional `[excel]` dependency (also included in `[notebooklm]`) to write `<workbook-name>.md` beside eligible `.xlsx` and `.xls` files. A current sidecar is reused. Conversion errors for individual workbooks are recorded and processing continues.

A Playwright exception inside the cool-video stage is recorded in the course report. For Canvas session recovery, `_try_recover_session` requires the native NTU origin and visible mode. Opening a new recovery browser also requires interactive stdin.

- `youtube_cookies.py` (`youtube-cookies`) is unrelated to NTU COOL — it's a Playwright helper that exports a Netscape `cookies.txt` from a dedicated browser profile so yt-dlp can hit member-only / age-gated YouTube videos.

`core/selection.py` contains the numeric picker and course filters. `youtube_files.py` places cached videos using a hard link or a copy. For NotebookLM, `notebooklm_plan.py` scans and prepares local sources, `notebooklm_journal.py` owns the import lock and state file, and `notebooklm.py` coordinates resumable uploads.

## Preview and verification modes

- `sync --dry-run` requires a token and queries Canvas for matching courses and files (and modules when enabled). With matching courses it opens the output SQLite manifest, which can create the directory, database and tables. It counts prospective downloads and skips writing course files and module snapshots.
- `notebooklm --course-dir <dir> --dry-run` scans and hashes local candidates and skips Excel conversion, API login, upload and journal updates. With media enabled, a `.mp4` larger than 200,000,000 bytes can invoke ffprobe and ffmpeg and create temporary video parts. Add `--notebooklm-no-media` for a document-only preview.
- `notebooklm --verify-only` prepares the local plan, which can generate Excel sidecars and temporary video parts, then checks remote source IDs when there are candidates. Verification leaves the remote notebook and local import journal unchanged. A missing login can still lead to a login prompt on a terminal.

## Conventions worth preserving

- **Session paths.** `session_paths.secrets_dir()` reuses an existing `./.secrets` directory and otherwise selects `~/.ntu-cool-gcm/.secrets` for default session headers, browser profiles and YouTube cookies. Windows NTU account credentials use a separate user-bound DPAPI file at `~/.ntu-cool-gcm/credentials/ntu-cool.dpapi`. NotebookLM can use explicit SDK profile or storage settings. `~/.ntu-cool-gcm/notebooklm-storage.json` records a storage path.
- **Course output.** For `pick` and `download-course`, `--out` takes precedence. Otherwise `cli._resolve_output_dir` prefers `ntu-cool-gcm_material` under the user's Documents folder (the Windows known folder when available, otherwise `~/Documents`). It reuses `./ntu-cool-gcm_material` only if the Documents target is absent and the local directory already exists. `sync` and `announcements` default to `./materials`.
- **`--version` / `-V`** reads `ntu_cool_materials.__version__`, which is derived from installed package metadata (`importlib.metadata`) — pyproject.toml is the single source of truth, never hardcode a version literal. `pick_main` intercepts `--version` before forwarding to the `pick` subparser.
- **Package updates.** `pick` checks for a newer `get-class-material` in a background thread and waits up to two seconds for the result after setup checks. `doctor` checks when printing its header. `check_for_update` caches successful lookups for 24 hours by default. A failed lookup can reuse an older result and leaves the timestamp untouched. In an interactive `pick`, a released installation defaults to upgrading (`[Y/n]`). A detected editable installation requires explicit consent to switch to PyPI (`[y/N]`). On acceptance the CLI runs pip, checks the installed version, then relaunches the original arguments when the version check succeeds.
- **Dependency updates.** Before `ensure_ready` imports Playwright, `pick` synchronously checks installed yt-dlp and Playwright versions using a separate cache per package. When a newer version is found, it runs pip without prompting. A successful Playwright pip invocation is followed by `playwright install chromium`. If browser installation fails, the Python package may already have changed.
- **Cross-origin token hygiene**: when adding new download paths, follow the `CanvasClient.download_file_url` pattern — only attach `Authorization` when `_is_canvas_origin(url)` is true, since Canvas redirects to signed S3/CDN URLs that already carry their own auth.
- **All Canvas list endpoints paginate**; route new ones through `list_paginated`, not bare `urlopen`.
- Use `urllib.parse.quote(..., safe="")` for path components built from Canvas ids (see `canvas_client.list_course_files`).
- The README is bilingual (Traditional Chinese + English code blocks). Keep that style if extending it.
