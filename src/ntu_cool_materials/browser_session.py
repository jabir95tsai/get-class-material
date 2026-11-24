from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from pathlib import Path

from .console import python_cmd
from .i18n import t
from .http_io import origin
from .session_client import DEFAULT_BASE_URL, SESSION_ORIGIN_HEADER
from .session_paths import secrets_dir, storage_state_path
from .storage import atomic_write_text

# Smart App Control kills Playwright's unsigned Chromium with
# STATUS_SYSTEM_INTEGRITY_POLICY_VIOLATION (0xC0E90002); Google-signed Chrome is allowed.
_BLOCKED_BY_APP_CONTROL = "exitCode=3236495362"


def launch_persistent_context(playwright, profile_dir: Path, **kwargs):
    """Launch Playwright's Chromium, falling back to installed Google Chrome when Windows blocks it."""
    kwargs["chromium_sandbox"] = True
    try:
        return playwright.chromium.launch_persistent_context(user_data_dir=str(profile_dir), **kwargs)
    except Exception as exc:
        if _BLOCKED_BY_APP_CONTROL not in str(exc):
            raise
    print(t("Chromium 被 Windows 應用程式控制原則封鎖,改用 Google Chrome…",
            "Chromium is blocked by Windows Application Control; using Google Chrome instead…"))
    try:
        # Own profile dir: a Playwright-Chromium profile isn't guaranteed to open in branded Chrome.
        return playwright.chromium.launch_persistent_context(
            user_data_dir=str(profile_dir.with_name(profile_dir.name + "_chrome")), channel="chrome", **kwargs)
    except Exception as exc:
        raise RuntimeError(t(
            f"Chromium 被 Windows 應用程式控制原則封鎖,也無法啟動 Google Chrome。請安裝 Chrome: https://www.google.com/chrome/ ({exc})",
            f"Chromium is blocked by Windows Application Control and Google Chrome could not start. Install Chrome: https://www.google.com/chrome/ ({exc})",
        )) from exc



class BrowserSessionError(RuntimeError):
    pass


@dataclass(frozen=True)
class BrowserSessionResult:
    headers_path: Path
    logged_in: bool
    current_url: str


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    if args.remember_login or args.forget_login:
        from .ntu_credentials import configure_credentials
        return configure_credentials(args.forget_login)

    try:
        result = refresh_headers_file(
            course_id=args.course_id,
            base_url=args.base_url,
            profile_dir=Path(args.profile_dir),
            headers_path=Path(args.headers_file),
            headless=args.headless,
            timeout_ms=args.timeout_ms,
        )
    except BrowserSessionError as exc:
        print(f"error: {exc}")
        return 1

    status = "logged in" if result.logged_in else "login required"
    print(f"status: {status}")
    print(f"url: {result.current_url}")
    print(f"headers: {result.headers_path}")
    return 0 if result.logged_in else 2


def refresh_headers_file(
    *,
    course_id: str | None,
    base_url: str = DEFAULT_BASE_URL,
    profile_dir: Path,
    headers_path: Path,
    headless: bool = False,
    timeout_ms: int = 120_000,
) -> BrowserSessionResult:
    try:
        from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise BrowserSessionError(
            f"Playwright is not installed. Run: {python_cmd()} -m pip install playwright && {python_cmd()} -m playwright install chromium"
        ) from exc

    base_url = base_url.rstrip("/")
    if course_id:
        target_url = f"{base_url}/courses/{course_id}/announcements"
        api_url = (
            f"{base_url}/api/v1/courses/{course_id}/discussion_topics"
            "?per_page=100&only_announcements=true&order_by=recent_activity"
        )
    else:
        target_url = f"{base_url}/courses"
        api_url = f"{base_url}/api/v1/courses?per_page=100&include[]=term&enrollment_state=active"

    profile_dir.parent.mkdir(parents=True, exist_ok=True)
    headers_path.parent.mkdir(parents=True, exist_ok=True)

    with sync_playwright() as playwright:
        context = launch_persistent_context(
            playwright,
            profile_dir,
            headless=headless,
            accept_downloads=False,
        )
        try:
            page = context.pages[0] if context.pages else context.new_page()
            page.goto(target_url, wait_until="domcontentloaded", timeout=timeout_ms)

            if _looks_like_login(page.url):
                from .ntu_credentials import try_saved_login
                submitted = base_url == DEFAULT_BASE_URL and try_saved_login(page)
                if headless and not submitted:
                    return BrowserSessionResult(headers_path=headers_path, logged_in=False, current_url=page.url)

                if not submitted:
                    print("請在開啟的瀏覽器視窗完成 NTU COOL 登入。")
                try:
                    page.wait_for_url(lambda url: origin(url) == origin(base_url) and not _looks_like_login(url), timeout=timeout_ms)
                except PlaywrightTimeoutError as exc:
                    raise BrowserSessionError("Timed out waiting for login to finish.") from exc

            response = page.request.get(api_url, headers={"Accept": "application/json, text/plain, */*"})
            if response.status in {401, 403}:
                return BrowserSessionResult(headers_path=headers_path, logged_in=False, current_url=page.url)
            if response.status >= 400:
                raise BrowserSessionError(f"Announcement API returned HTTP {response.status}.")

            cookie_header = _cookie_header(context.cookies(base_url))
            if not cookie_header:
                raise BrowserSessionError("No NTU COOL cookies found in browser context.")

            saved_origin = "://".join(origin(base_url))
            atomic_write_text(headers_path,
                "\n".join(
                    [
                        f"{SESSION_ORIGIN_HEADER}: {saved_origin}",
                        "accept: application/json, text/plain, */*",
                        f"cookie: {cookie_header}",
                        f"referer: {saved_origin}/",
                        "user-agent: ntu-cool-materials/0.1",
                    ]
                )
                + "\n",
            )
            # The full jar lets the cool-video stage resume this login in an
            # invisible browser (Canvas session cookies die with the window).
            atomic_write_text(storage_state_path(headers_path),
                              json.dumps({"origin": saved_origin, "cookies": context.cookies()}))
            return BrowserSessionResult(headers_path=headers_path, logged_in=True, current_url=page.url)
        finally:
            context.close()


def _cookie_header(cookies: list[dict[str, object]]) -> str:
    pairs = []
    for cookie in cookies:
        name = str(cookie.get("name") or "")
        value = str(cookie.get("value") or "")
        if name and value:
            pairs.append(f"{name}={value}")
    return "; ".join(pairs)


def _looks_like_login(url: str) -> bool:
    lowered = url.lower()
    return lowered.startswith("https://adfs.ntu.edu.tw/adfs/ls/") or "/login" in lowered or "oauth2" in lowered or "saml" in lowered


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ntu-cool-session",
        description="Refresh a local NTU COOL browser session and export headers for API calls.",
    )
    credentials = parser.add_mutually_exclusive_group()
    credentials.add_argument("--remember-login", action="store_true", help="Save or update NTU credentials for this Windows user.")
    credentials.add_argument("--forget-login", action="store_true", help="Remove saved NTU credentials.")
    parser.add_argument("--course-id", default=None, help="Canvas course id to open while refreshing session.")
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL, help="NTU COOL base URL.")
    # Same home-anchored location as the main CLI, so `pick` sees this login.
    secrets = secrets_dir()
    parser.add_argument("--profile-dir", default=str(secrets / "ntu_cool_browser_profile"),
                        help="Persistent browser profile directory.")
    parser.add_argument("--headers-file", default=str(secrets / "ntu_cool_headers.txt"), help="Headers output path.")
    parser.add_argument("--headless", action="store_true", help="Do not show the browser window.")
    parser.add_argument("--timeout-ms", type=int, default=120_000, help="Login wait timeout.")
    return parser


if __name__ == "__main__":
    raise SystemExit(main())
