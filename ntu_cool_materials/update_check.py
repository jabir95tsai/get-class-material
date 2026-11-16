"""Best-effort 'is there a newer version on PyPI?' check.

Design rules (a startup nag must never get in the way):
  - Never raises. Any network/parse/cache error → returns None (no nag).
  - Hits the network at most once per `interval_sec` (default 24h); between
    network checks it compares against the cached latest version, so the user
    still gets reminded every run while they're behind without re-hitting PyPI.
  - Short timeout so a slow/offline network can't stall startup.

`check_for_update` is the entry point and is pure enough to unit-test by
injecting `fetch` and `now`.
"""
from __future__ import annotations

import datetime
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import sysconfig
import time
import urllib.request
from pathlib import Path

from . import console

PYPI_JSON_URL = "https://pypi.org/pypi/{package}/json"
_CHECK_INTERVAL_SEC = 24 * 3600
_UNKNOWN_VERSION = "0.0.0+unknown"


def _parse_version(value: str) -> tuple[int, ...]:
    """Lenient numeric-tuple parse: '0.2.17' -> (0, 2, 17). Non-numeric
    suffixes (e.g. '1rc2', '+unknown') are truncated to their leading digits."""
    parts: list[int] = []
    for chunk in str(value).split("."):
        digits = ""
        for ch in chunk:
            if ch.isdigit():
                digits += ch
            else:
                break
        parts.append(int(digits) if digits else 0)
    return tuple(parts)


def is_newer(latest: str, current: str) -> bool:
    return _parse_version(latest) > _parse_version(current)


def _fetch_latest_version(timeout: float, package: str = "get-class-material") -> str:
    request = urllib.request.Request(PYPI_JSON_URL.format(package=package), headers={"Accept": "application/json"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        data = json.loads(response.read().decode("utf-8"))
    return str(data["info"]["version"])


def check_for_update(
    current_version: str,
    *,
    cache_path: Path,
    timeout: float = 3.0,
    now: float | None = None,
    interval_sec: int = _CHECK_INTERVAL_SEC,
    fetch=None,
) -> str | None:
    """Return the latest version string if PyPI has a newer one, else None.

    Network is contacted at most once per `interval_sec`; results are cached in
    `cache_path`. Never raises.
    """
    if not current_version or current_version == _UNKNOWN_VERSION:
        return None  # running from an uninstalled source tree — don't nag

    now = time.time() if now is None else now
    fetch = fetch or (lambda: _fetch_latest_version(timeout))

    try:
        cache = json.loads(cache_path.read_text(encoding="utf-8"))
        if not isinstance(cache, dict):
            cache = {}
    except Exception:
        cache = {}

    last_check = cache.get("last_check", 0)
    cached_latest = cache.get("latest")
    fresh = (
        isinstance(last_check, (int, float))
        and (now - last_check) < interval_sec
        and bool(cached_latest)
    )

    if fresh:
        latest = str(cached_latest)
    else:
        try:
            latest = fetch()
        except Exception:
            latest = None
        if latest:
            try:
                cache_path.parent.mkdir(parents=True, exist_ok=True)
                cache_path.write_text(
                    json.dumps({"last_check": now, "latest": latest}), encoding="utf-8"
                )
            except OSError:
                pass
        elif cached_latest:
            latest = str(cached_latest)  # network down → fall back to cache

    if latest and is_newer(latest, current_version):
        return latest
    return None


def yt_dlp_version_age_days(version_str: str, now: datetime.date | None = None) -> int | None:
    """Calculate the age in days of a yt-dlp release (version is YYYY.MM.DD[.patch])."""
    if not version_str:
        return None
    m = re.match(r"^(\d{4})\.(\d{1,2})\.(\d{1,2})", version_str.strip())
    if not m:
        return None
    try:
        ver_date = datetime.date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        current_date = now or datetime.date.today()
        return (current_date - ver_date).days
    except (ValueError, OverflowError):
        return None


def resolve_yt_dlp(yt_dlp: str = "yt-dlp") -> str | None:
    """Full path of the yt-dlp executable, or None.

    yt-dlp is one of our pip dependencies, so its launcher sits in this
    Python's Scripts dir — which often isn't on PATH (fresh python.org
    installs, `python -m ntu_cool_materials`, or right after doctor added it
    for future shells only). Look there before declaring it missing."""
    found = shutil.which(yt_dlp)
    if found or os.path.dirname(yt_dlp):
        return found
    scripts = sysconfig.get_path("scripts")
    return shutil.which(yt_dlp, path=scripts) if scripts else None


def yt_dlp_command(yt_dlp: str = "yt-dlp") -> list[str]:
    """argv prefix that runs yt-dlp.

    Prefers `python -m yt_dlp` over the pip-generated yt-dlp.exe launcher, which
    Windows Application Control blocks as unsigned (WinError 4551). A custom
    executable name/path is respected as-is."""
    if Path(yt_dlp).stem.lower() == "yt-dlp" and importlib.util.find_spec("yt_dlp"):
        return [sys.executable, "-m", "yt_dlp"]
    return [yt_dlp]


def get_yt_dlp_version(yt_dlp: str = "yt-dlp") -> str | None:
    """Return the installed yt-dlp version string, or None if unavailable."""
    try:
        import importlib.metadata
        return importlib.metadata.version("yt-dlp")
    except Exception:
        pass
    try:
        res = subprocess.run([yt_dlp, "--version"], capture_output=True, text=True, timeout=5)
        if res.returncode == 0 and res.stdout.strip():
            return res.stdout.strip()
    except Exception:
        pass
    return None


def update_yt_dlp(yt_dlp: str = "yt-dlp") -> tuple[bool, str]:
    """Attempt to update yt-dlp using pip, falling back to yt-dlp -U.

    Returns (success, message).
    """
    from .i18n import t
    # 1. First try python -m pip install --upgrade yt-dlp
    try:
        proc = subprocess.run(
            [sys.executable, "-m", "pip", "install", "--upgrade", "yt-dlp"],
            capture_output=True,
            text=True,
            timeout=120,
        )
        if proc.returncode == 0:
            # Invalidate importlib caches to reflect newly installed package version
            import importlib
            importlib.invalidate_caches()
            new_ver = get_yt_dlp_version(yt_dlp) or t("最新版", "the latest version")
            return True, t(f"已透過 pip 升級至 {new_ver}", f"updated to {new_ver} via pip")
    except Exception:
        pass

    # 2. Fallback: try yt-dlp -U if executable is on PATH
    if shutil.which(yt_dlp):
        try:
            proc = subprocess.run([yt_dlp, "-U"], capture_output=True, text=True, timeout=120)
            if proc.returncode == 0:
                new_ver = get_yt_dlp_version(yt_dlp) or t("最新版", "the latest version")
                return True, t(f"已透過 yt-dlp -U 升級至 {new_ver}", f"updated to {new_ver} via yt-dlp -U")
        except Exception:
            pass

    hint = console.pip_install_hint("yt-dlp")
    return False, t(f"更新失敗，請手動執行 {hint}",
                    f"update failed; run {hint} manually")


def confirm_yt_dlp_update(prompt: str, *, input_fn=None) -> bool:
    """Ask before changing the user's Python environment; never in non-interactive runs."""
    if input_fn is None:
        if not console.stdin_is_interactive():
            return False
        input_fn = input
    try:
        answer = input_fn(prompt).strip().lower()
    except (EOFError, KeyboardInterrupt):
        return False
    return answer in {"", "y", "yes"}


def ensure_yt_dlp_updated(
    yt_dlp: str = "yt-dlp",
    max_age_days: int = 60,
    now: datetime.date | None = None,
    *,
    confirm=confirm_yt_dlp_update,
) -> bool | None:
    """Offer to update yt-dlp when it is older than max_age_days.

    Returns None when no update was needed (or the version is unknown), True
    after a successful update, and False when the user declined or it failed.
    """
    version = get_yt_dlp_version(yt_dlp)
    if not version:
        return None
    age = yt_dlp_version_age_days(version, now=now)
    if age is None or age < max_age_days:
        return None

    from .i18n import t
    if not confirm(t(
        f"  [yt-dlp] 目前版本 ({version}) 已發布 {age} 天，YouTube 下載可能失敗。現在更新嗎？[Y/n]: ",
        f"  [yt-dlp] current version ({version}) is {age} days old; YouTube downloads may fail. Update now? [Y/n]: ",
    )):
        hint = console.pip_install_hint("yt-dlp")
        print(t(f"  [yt-dlp] 略過更新；可稍後執行 {hint}",
                f"  [yt-dlp] update skipped; run {hint} later"))
        return False
    ok, msg = update_yt_dlp(yt_dlp)
    print(f"  [yt-dlp] {msg}")
    return ok


AUTO_UPDATE_PACKAGES = ("yt-dlp", "playwright")


def auto_update_dependencies(secrets_dir: Path, *, fetch=None, run=subprocess.run) -> None:
    """Upgrade yt-dlp / Playwright when PyPI has a newer release.

    YouTube breaks old yt-dlp within weeks, so staying current matters more
    than asking. PyPI is checked at most once a day per package (same cache
    rules as `check_for_update`). Must run before anything imports Playwright
    in this process. Never raises; a failed upgrade just leaves the old one.
    """
    import importlib.metadata
    from .i18n import t
    for package in AUTO_UPDATE_PACKAGES:
        try:
            current = importlib.metadata.version(package)
        except Exception:
            continue  # not installed: ensure_ready installs it
        latest = check_for_update(
            current,
            cache_path=secrets_dir / f"update_check_{package}.json",
            fetch=(lambda p=package: fetch(p)) if fetch else (lambda p=package: _fetch_latest_version(3.0, p)),
        )
        if not latest:
            continue
        print(t(f"  [{package}] 自動更新 {current} → {latest}...",
                f"  [{package}] auto-updating {current} → {latest}..."))
        try:
            ok = run([sys.executable, "-m", "pip", "install", "--upgrade", package],
                     capture_output=True, text=True, timeout=300).returncode == 0
            if ok and package == "playwright":
                # Each Playwright release pins its own Chromium build; fetch it now
                # (output shown: it's a large download).
                ok = run([sys.executable, "-m", "playwright", "install", "chromium"], timeout=900).returncode == 0
        except Exception:
            ok = False
        print(t(f"  [{package}] 已更新" if ok else f"  [{package}] 更新失敗，繼續使用 {current}",
                f"  [{package}] updated" if ok else f"  [{package}] update failed; keeping {current}"))


def _is_editable_install(package: str = "get-class-material") -> bool:
    """A dev's `pip install -e .` must not be replaced by the PyPI build."""
    import importlib.metadata
    try:
        raw = importlib.metadata.distribution(package).read_text("direct_url.json")
        return bool(raw and json.loads(raw).get("dir_info", {}).get("editable"))
    except Exception:
        return False


def offer_self_update(latest: str, relaunch_argv: list[str], *, confirm=None, run=subprocess.run,
                      installed_version=None) -> int | None:
    """Ask [Y/n] to upgrade get-class-material to `latest`, then rerun the same
    command on the new code (modules already imported here are the old ones).

    Returns the rerun's exit code, or None to carry on with the current
    version (declined, non-interactive, editable install, or upgrade failed).
    """
    from .i18n import t
    if _is_editable_install():
        return None
    confirm = confirm or confirm_yt_dlp_update
    if not confirm(t(f"\n💡 有新版本 {latest}，現在自動更新嗎？[Y/n]: ",
                     f"\n💡 Version {latest} is available. Update now? [Y/n]: ")):
        return None
    if installed_version is None:
        import importlib.metadata

        def installed_version() -> str:
            importlib.invalidate_caches()
            return importlib.metadata.version("get-class-material")
    try:
        ok = run([sys.executable, "-m", "pip", "install", "--upgrade", "get-class-material"]).returncode == 0
        # pip exits 0 without upgrading when the new release needs a newer Python.
        ok = ok and not is_newer(latest, installed_version())
    except Exception:
        ok = False
    if not ok:
        print(t("  更新失敗，繼續使用目前版本。可手動執行: python -m pip install --upgrade get-class-material",
                "  Update failed; continuing with the current version. Run: python -m pip install --upgrade get-class-material"))
        return None
    print(t(f"  已更新到 {latest}，重新啟動...\n", f"  Updated to {latest}; restarting...\n"))
    try:
        return run([sys.executable, "-m", "ntu_cool_materials", *relaunch_argv]).returncode
    except KeyboardInterrupt:
        return 130
