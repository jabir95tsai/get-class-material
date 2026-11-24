from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .canvas_errors import CanvasAPIError, SessionExpiredError
from .canvas_pagination import iter_page_items, parse_link_header as parse_link_header
from .http_io import NoRedirectHandler, download, request_json, safe_url


JSON = dict[str, Any] | list[Any]


def is_downloadable_course_file(file_info: dict[str, Any]) -> bool:
    return bool(file_info.get("url")) and not any(file_info.get(flag) for flag in (
        "locked", "locked_for_user", "hidden", "hidden_for_user",
    ))


@dataclass
class CanvasClient:
    base_url: str
    token: str
    timeout: float = 30.0
    user_agent: str = "ntu-cool-materials/0.1"

    def __post_init__(self) -> None:
        self.base_url = self.base_url.rstrip("/")
        parsed = urllib.parse.urlparse(self.base_url)
        if parsed.scheme != "https":
            raise ValueError("Canvas base URL must use HTTPS.")
        if not self.token:
            raise ValueError("Canvas access token is required.")

        self._base_netloc = parsed.netloc.lower()
        self._download_opener = urllib.request.build_opener(NoRedirectHandler)

    def list_courses(self, enrollment_state: str = "active") -> list[dict[str, Any]]:
        params: list[tuple[str, str]] = [
            ("per_page", "100"),
            ("include[]", "term"),
        ]
        if enrollment_state:
            params.append(("enrollment_state", enrollment_state))

        return list(self.list_paginated("/api/v1/courses", params=params))

    def get_course(self, course_id: str) -> dict[str, Any]:
        quoted_course_id = urllib.parse.quote(str(course_id), safe="")
        data = self.get_json(f"/api/v1/courses/{quoted_course_id}", params=[("include[]", "term")])
        if not isinstance(data, dict):
            raise CanvasAPIError(f"Expected an object response for course {course_id}.")
        return data

    def list_course_files(self, course_id: str) -> list[dict[str, Any]]:
        quoted_course_id = urllib.parse.quote(str(course_id), safe="")
        return list(
            self.list_paginated(
                f"/api/v1/courses/{quoted_course_id}/files",
                params=[("per_page", "100")],
            )
        )

    def list_modules(self, course_id: str) -> list[dict[str, Any]]:
        quoted_course_id = urllib.parse.quote(str(course_id), safe="")
        return list(
            self.list_paginated(
                f"/api/v1/courses/{quoted_course_id}/modules",
                params=[
                    ("per_page", "100"),
                    ("include[]", "items"),
                    ("include[]", "content_details"),
                ],
            )
        )

    def list_module_items(self, course_id: str, module_id: str) -> list[dict[str, Any]]:
        quoted_course_id = urllib.parse.quote(str(course_id), safe="")
        quoted_module_id = urllib.parse.quote(str(module_id), safe="")
        return list(
            self.list_paginated(
                f"/api/v1/courses/{quoted_course_id}/modules/{quoted_module_id}/items",
                params=[("per_page", "100")],
            )
        )

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

    def list_paginated(
        self,
        path_or_url: str,
        params: Sequence[tuple[str, str]] | None = None,
    ) -> Iterator[dict[str, Any]]:
        yield from iter_page_items(self._build_url(path_or_url, params=params), self._request_json)

    def get_json(
        self,
        path_or_url: str,
        params: Sequence[tuple[str, str]] | None = None,
    ) -> JSON:
        data, _headers = self._request_json(self._build_url(path_or_url, params=params))
        return data

    def download_file_url(self, file_url: str, target_path: Path, *, expected_size=None) -> Path:
        try:
            return download(self._build_url(file_url), target_path, self._download_headers, timeout=self.timeout, expected_size=int(expected_size) if expected_size is not None else None)
        except urllib.error.HTTPError as exc:
            raise self._api_error(exc, file_url) from exc

    def _request_json(self, url: str):
        try:
            return request_json(url, self._json_headers, timeout=self.timeout, api_origin=self.base_url)
        except urllib.error.HTTPError as exc:
            raise self._api_error(exc, url) from exc
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise CanvasAPIError(f"Canvas returned invalid JSON from {safe_url(url)}") from exc

    def _json_headers(self, url: str) -> dict[str, str]:
        headers = {
            "Accept": "application/json+canvas-string-ids",
            "User-Agent": self.user_agent,
        }
        if self._is_canvas_origin(url):
            headers["Authorization"] = f"Bearer {self.token}"
        return headers

    def _download_headers(self, url: str) -> dict[str, str]:
        headers = {"User-Agent": self.user_agent}
        if self._is_canvas_origin(url):
            headers["Authorization"] = f"Bearer {self.token}"
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
        message = f"Canvas request failed with HTTP {exc.code} for {safe_url(url)}"
        exc.close()
        if exc.code == 401:
            return SessionExpiredError(message)
        return CanvasAPIError(message, status_code=exc.code)
