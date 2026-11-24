"""Traverse Canvas list endpoints using the calling client for each request."""
from __future__ import annotations

import time
from collections.abc import Callable, Iterator
from email.message import Message
from typing import Any

from .canvas_errors import CanvasAPIError


def parse_link_header(value: str | None) -> dict[str, str]:
    if not value:
        return {}

    links: dict[str, str] = {}
    for part in value.split(","):
        sections = [section.strip() for section in part.split(";")]
        if not sections or not sections[0].startswith("<") or not sections[0].endswith(">"):
            continue

        url = sections[0][1:-1]
        rel = None
        for section in sections[1:]:
            key, _, raw_value = section.partition("=")
            if key.strip().lower() == "rel":
                rel = raw_value.strip().strip('"')
                break

        if rel:
            links[rel] = url

    return links


def iter_page_items(first_url: str, request_page: Callable[[str], tuple[object, Message]]) -> Iterator[dict[str, Any]]:
    next_url: str | None = first_url
    while next_url:
        data, headers = request_page(next_url)
        if not isinstance(data, list):
            raise CanvasAPIError(f"Expected a list response from {next_url!r}.")

        for item in data:
            if isinstance(item, dict):
                yield item

        next_url = parse_link_header(headers.get("Link")).get("next")
        if next_url:
            time.sleep(0.05)
