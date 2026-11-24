from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from email.message import Message
from pathlib import Path
from typing import Any

from .http_io import NoRedirectHandler, origin, request_json, safe_url
from .canvas_errors import CanvasAPIError, SessionExpiredError
from .canvas_pagination import iter_page_items


# Tri-state result of a cheap pre-flight auth probe. Deliberately NOT a bool:
# a network/timeout error must NOT be mistaken for "expired" (that would pop a
# browser open on a merely-slow connection), so it gets its own "unknown".
AUTH_OK = "ok"
AUTH_EXPIRED = "expired"
AUTH_UNKNOWN = "unknown"


SENSITIVE_HEADER_NAMES = {"cookie", "authorization", "x-csrf-token"}
DROP_REQUEST_HEADER_NAMES = {
    "accept-encoding",
    "connection",
    "content-length",
    "host",
}


DEFAULT_BASE_URL = "https://cool.ntu.edu.tw"
SESSION_ORIGIN_HEADER = "canvas-session-origin"


def read_headers_file(path: Path, *, base_url: str = DEFAULT_BASE_URL) -> dict[str, str]:
    text = path.read_text(encoding="utf-8").strip()
    if not text:
        raise ValueError(f"Headers file is empty: {path}")

    if text.startswith("{"):
        data = json.loads(text)
        if not isinstance(data, dict):
            raise ValueError("JSON headers file must contain an object.")
        headers = {str(key): str(value) for key, value in data.items()}
    else:
        headers = {}
        for raw_line in text.splitlines():
            line = raw_line.strip()
            if not line or line.startswith(":"):
                continue
            name, separator, value = line.partition(":")
            if separator:
                headers[name.strip()] = value.strip()

    bindings = [headers.pop(name) for name in list(headers) if name.lower() == SESSION_ORIGIN_HEADER]
    saved_origin = bindings[0] if bindings else DEFAULT_BASE_URL
    if len(bindings) > 1 or origin(saved_origin) != origin(base_url):
        raise ValueError("Saved Canvas session origin does not match the base URL. Refresh login for this origin.")
    if not headers:
        raise ValueError(f"No headers found in {path}")
    return headers


def redact_headers(headers: dict[str, str]) -> dict[str, str]:
    redacted: dict[str, str] = {}
    for name, value in headers.items():
        if name.lower() in SENSITIVE_HEADER_NAMES:
            redacted[name] = "<redacted>"
        else:
            redacted[name] = value
    return redacted


@dataclass
class CanvasSessionClient:
    base_url: str
    headers: dict[str, str]
    timeout: float = 30.0

    def __post_init__(self) -> None:
        self.base_url = self.base_url.rstrip("/")
        parsed = urllib.parse.urlparse(self.base_url)
        if parsed.scheme != "https":
            raise ValueError("Canvas base URL must use HTTPS.")
        self._base_netloc = parsed.netloc.lower()

    def list_courses(self, enrollment_state: str = "active") -> list[dict[str, Any]]:
        params: list[tuple[str, str]] = [
            ("per_page", "100"),
            ("include[]", "term"),
        ]
        if enrollment_state:
            params.append(("enrollment_state", enrollment_state))

        return list(self.list_paginated("/api/v1/courses", params=params))

    def check_auth(self, *, timeout: float = 6.0) -> str:
        """Probe /api/v1/users/self before listing courses, with redirects disabled.

        A 2xx response means AUTH_OK. A 401 or a 301/302/303/307/308 pointing
        outside the Canvas origin means AUTH_EXPIRED. Other HTTP results and
        connection failures leave the status AUTH_UNKNOWN.
        """
        url = self._build_url("/api/v1/users/self")
        request = urllib.request.Request(url, headers=self._request_headers(url))
        opener = urllib.request.build_opener(NoRedirectHandler)
        try:
            with opener.open(request, timeout=timeout) as response:
                return AUTH_OK if 200 <= response.status < 300 else AUTH_UNKNOWN
        except urllib.error.HTTPError as exc:
            if exc.code == 401:
                return AUTH_EXPIRED
            if exc.code in (301, 302, 303, 307, 308):
                location = exc.headers.get("Location") if exc.headers else None
                if location and not self._is_canvas_origin(self._build_url(location)):
                    return AUTH_EXPIRED  # bounced into SSO → session is gone
                return AUTH_UNKNOWN  # on-origin redirect → don't guess
            return AUTH_UNKNOWN
        except (urllib.error.URLError, OSError, ValueError):
            # timeout / DNS / connection reset / bad URL → can't conclude expiry
            return AUTH_UNKNOWN

    def get_course(self, course_id: str) -> dict[str, Any]:
        quoted_course_id = urllib.parse.quote(str(course_id), safe="")
        data, _headers = self._request_json(
            self._build_url(f"/api/v1/courses/{quoted_course_id}", params=[("include[]", "term")])
        )
        if not isinstance(data, dict):
            raise CanvasAPIError(f"Expected an object response for course {course_id}.")
        return data

    def list_course_files(self, course_id: str) -> list[dict[str, Any]]:
        quoted_course_id = urllib.parse.quote(str(course_id), safe="")
        return list(self.list_paginated(f"/api/v1/courses/{quoted_course_id}/files", params=[("per_page", "100")]))

    def list_course_announcements(self, course_id: str) -> list[dict[str, Any]]:
        quoted_course_id = urllib.parse.quote(str(course_id), safe="")
        return list(
            self.list_paginated(
                f"/api/v1/courses/{quoted_course_id}/discussion_topics",
                params=[
                    ("per_page", "100"),
                    ("only_announcements", "true"),
                    ("order_by", "recent_activity"),
                ],
            )
        )

    def list_announcements(
        self,
        course_id: str,
        *,
        start_date: str | None = None,
        end_date: str | None = None,
        active_only: bool = True,
    ) -> list[dict[str, Any]]:
        params: list[tuple[str, str]] = [
            ("per_page", "100"),
            ("context_codes[]", f"course_{course_id}"),
            ("active_only", "true" if active_only else "false"),
        ]
        if start_date:
            params.append(("start_date", start_date))
        if end_date:
            params.append(("end_date", end_date))

        return list(self.list_paginated("/api/v1/announcements", params=params))

    def get_json(
        self,
        path_or_url: str,
        params: Sequence[tuple[str, str]] | None = None,
    ) -> list[Any] | dict[str, Any]:
        data, _headers = self._request_json(self._build_url(path_or_url, params=params))
        return data

    def list_paginated(
        self,
        path_or_url: str,
        params: Sequence[tuple[str, str]] | None = None,
    ) -> Iterator[dict[str, Any]]:
        yield from iter_page_items(self._build_url(path_or_url, params=params), self._request_json)

    def _request_json(self, url: str) -> tuple[list[Any] | dict[str, Any], Message]:
        try:
            return request_json(url, self._request_headers, timeout=self.timeout, api_origin=self.base_url)
        except urllib.error.HTTPError as exc:
            raise self._api_error(exc, url) from exc
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise CanvasAPIError(f"Canvas returned invalid JSON from {safe_url(url)}") from exc

    def _request_headers(self, url: str) -> dict[str, str]:
        if not self._is_canvas_origin(url):
            return {}

        headers = {
            name: value
            for name, value in self.headers.items()
            if name.lower() not in DROP_REQUEST_HEADER_NAMES
        }
        headers["Accept"] = "application/json, text/plain, */*"
        headers.setdefault("User-Agent", "ntu-cool-materials/0.1")
        return headers

    def _build_url(
        self,
        path_or_url: str,
        params: Sequence[tuple[str, str]] | None = None,
    ) -> str:
        parsed = urllib.parse.urlparse(path_or_url)
        if parsed.scheme:
            url = path_or_url
        else:
            url = f"{self.base_url}/{path_or_url.lstrip('/')}"

        if params:
            separator = "&" if urllib.parse.urlparse(url).query else "?"
            url = f"{url}{separator}{urllib.parse.urlencode(params)}"
        return url

    def _is_canvas_origin(self, url: str) -> bool:
        parsed = urllib.parse.urlparse(url)
        return parsed.scheme == "https" and parsed.netloc.lower() == self._base_netloc

    @staticmethod
    def _api_error(exc: urllib.error.HTTPError, url: str) -> CanvasAPIError:
        message = f"Canvas session request failed with HTTP {exc.code} for {safe_url(url)}"
        exc.close()
        if exc.code == 401:
            return SessionExpiredError(message)
        return CanvasAPIError(message, status_code=exc.code)
