from __future__ import annotations

import unittest
from pathlib import Path

from ntu_cool_materials.browser_session import _cookie_header, _looks_like_login
from ntu_cool_materials.session_client import CanvasSessionClient


class BrowserSessionTests(unittest.TestCase):
    def test_cookie_header(self) -> None:
        self.assertEqual(
            _cookie_header(
                [
                    {"name": "a", "value": "1"},
                    {"name": "b", "value": "2"},
                    {"name": "", "value": "ignored"},
                ]
            ),
            "a=1; b=2",
        )

    def test_looks_like_login(self) -> None:
        self.assertTrue(_looks_like_login("https://cool.ntu.edu.tw/login"))
        self.assertTrue(_looks_like_login("https://cool.ntu.edu.tw/oauth2/login"))
        self.assertFalse(_looks_like_login("https://cool.ntu.edu.tw/courses/57544/announcements"))

    def test_session_client_builds_course_url(self) -> None:
        client = CanvasSessionClient(base_url="https://cool.ntu.edu.tw", headers={"cookie": "a=b"})

        url = client._build_url(
            "/api/v1/courses",
            params=[("per_page", "100"), ("include[]", "term"), ("enrollment_state", "active")],
        )

        self.assertEqual(
            url,
            "https://cool.ntu.edu.tw/api/v1/courses?per_page=100&include%5B%5D=term&enrollment_state=active",
        )


if __name__ == "__main__":
    unittest.main()


class ChromeFallbackTests(unittest.TestCase):
    def test_blocked_chromium_falls_back_to_chrome_with_own_profile(self) -> None:
        from unittest import mock
        from ntu_cool_materials.browser_session import launch_persistent_context

        pw = mock.Mock()
        pw.chromium.launch_persistent_context.side_effect = [
            Exception("<process did exit: exitCode=3236495362, signal=null>"), "ctx"]
        with mock.patch("builtins.print"):
            ctx = launch_persistent_context(pw, Path("p/profile"), headless=False)
        self.assertEqual(ctx, "ctx")
        retry = pw.chromium.launch_persistent_context.call_args_list[1].kwargs
        self.assertEqual(retry["channel"], "chrome")
        self.assertEqual(Path(retry["user_data_dir"]), Path("p/profile_chrome"))

    def test_other_launch_errors_are_not_retried(self) -> None:
        from unittest import mock
        from ntu_cool_materials.browser_session import launch_persistent_context

        pw = mock.Mock()
        pw.chromium.launch_persistent_context.side_effect = Exception("boom")
        with self.assertRaisesRegex(Exception, "boom"):
            launch_persistent_context(pw, Path("p/profile"))
        self.assertEqual(pw.chromium.launch_persistent_context.call_count, 1)
