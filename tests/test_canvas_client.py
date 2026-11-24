from __future__ import annotations

import unittest
from unittest.mock import call, patch

from ntu_cool_materials.canvas_client import CanvasAPIError, CanvasClient, parse_link_header
from ntu_cool_materials.session_client import CanvasSessionClient


class LinkHeaderTests(unittest.TestCase):
    def test_parse_next_link(self) -> None:
        header = (
            '<https://cool.ntu.edu.tw/api/v1/courses?page=1>; rel="current", '
            '<https://cool.ntu.edu.tw/api/v1/courses?page=2>; rel="next", '
            '<https://cool.ntu.edu.tw/api/v1/courses?page=4>; rel="last"'
        )

        links = parse_link_header(header)

        self.assertEqual(links["next"], "https://cool.ntu.edu.tw/api/v1/courses?page=2")
        self.assertEqual(links["last"], "https://cool.ntu.edu.tw/api/v1/courses?page=4")

    def test_empty_link_header(self) -> None:
        self.assertEqual(parse_link_header(None), {})
        self.assertEqual(parse_link_header(""), {})


class PaginationTests(unittest.TestCase):
    def test_both_clients_follow_next_lazily_and_reject_non_list_pages(self) -> None:
        base = "https://canvas.invalid"
        next_url = f"{base}/courses?cursor=abc"
        for client in (CanvasClient(base, "fake-token"), CanvasSessionClient(base, {})):
            with (
                self.subTest(client=type(client).__name__),
                patch.object(client, "_request_json", side_effect=[
                    (["ignored", {"id": 1}], {"Link": f'<{next_url}>; rel="next"'}),
                    ({"unexpected": True}, {}),
                ]) as request,
                patch("ntu_cool_materials.canvas_pagination.time.sleep") as sleep,
            ):
                items = client.list_paginated("/courses", params=[("page", "1")])
                request.assert_not_called()
                self.assertEqual(next(items), {"id": 1})
                sleep.assert_not_called()
                with self.assertRaises(CanvasAPIError):
                    next(items)
                self.assertEqual(request.call_args_list, [call(f"{base}/courses?page=1"), call(next_url)])
                sleep.assert_called_once_with(0.05)


if __name__ == "__main__":
    unittest.main()
