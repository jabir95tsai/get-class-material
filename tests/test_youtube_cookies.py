from __future__ import annotations

import unittest
from pathlib import Path
from unittest import mock

from ntu_cool_materials.youtube_cookies import (
    _has_login_cookie,
    _is_relevant_cookie_domain,
    _netscape_cookie_text,
    export_youtube_cookies,
    YouTubeCookieError,
)


class YouTubeCookieTests(unittest.TestCase):
    def test_export_enables_chromium_sandbox(self) -> None:
        playwright = mock.Mock()
        context = playwright.chromium.launch_persistent_context.return_value
        context.pages = []
        context.cookies.return_value = []
        with mock.patch("playwright.sync_api.sync_playwright") as sync_playwright:
            sync_playwright.return_value.__enter__.return_value = playwright
            with self.assertRaisesRegex(YouTubeCookieError, "No YouTube/Google cookies"):
                export_youtube_cookies(profile_dir=Path("profile"), cookies_path=Path("cookies.txt"), headless=True)
        self.assertTrue(playwright.chromium.launch_persistent_context.call_args.kwargs["chromium_sandbox"])

    def test_relevant_cookie_domains(self) -> None:
        self.assertTrue(_is_relevant_cookie_domain(".youtube.com"))
        self.assertTrue(_is_relevant_cookie_domain("accounts.google.com"))
        self.assertFalse(_is_relevant_cookie_domain("cool.ntu.edu.tw"))

    def test_netscape_cookie_text(self) -> None:
        text = _netscape_cookie_text(
            [
                {
                    "domain": ".youtube.com",
                    "path": "/",
                    "secure": True,
                    "expires": 1893456000,
                    "name": "SID",
                    "value": "abc",
                }
            ]
        )

        self.assertIn(".youtube.com\tTRUE\t/\tTRUE\t1893456000\tSID\tabc", text)

    def test_has_login_cookie(self) -> None:
        self.assertFalse(_has_login_cookie([{"name": "PREF"}]))
        self.assertTrue(_has_login_cookie([{"name": "__Secure-1PSID"}]))


if __name__ == "__main__":
    unittest.main()
