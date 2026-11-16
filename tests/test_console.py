import io
import os
import unittest
from unittest.mock import patch

from ntu_cool_materials import console


class StdinInteractiveTests(unittest.TestCase):
    def test_null_device_is_not_interactive(self):
        # Windows reports isatty() for NUL; `command <nul` must never prompt.
        with open(os.devnull, encoding="utf-8") as null, patch.object(console.sys, "stdin", null):
            self.assertFalse(console.stdin_is_interactive())

    def test_pipe_and_missing_stdin_are_not_interactive(self):
        for stream in (io.StringIO("y\n"), None):
            with self.subTest(stream=stream), patch.object(console.sys, "stdin", stream):
                self.assertFalse(console.stdin_is_interactive())


class PythonHintTests(unittest.TestCase):
    def test_posix_hints_say_python3(self):
        # macOS has no bare `python`; a hint saying `python -m pip` fails there.
        with patch.object(console.os, "name", "posix"):
            self.assertEqual(console.pip_install_hint("yt-dlp"), "python3 -m pip install --upgrade yt-dlp")

    def test_windows_hints_say_python(self):
        with patch.object(console.os, "name", "nt"):
            self.assertEqual(console.pip_install_hint("x", upgrade=False), "python -m pip install x")


class _Context:
    def __init__(self, count):
        self.count = count

    def cert_store_stats(self):
        return {"x509_ca": self.count}


class CABundleTests(unittest.TestCase):
    def _run(self, *, platform="darwin", ca_count=0, env=None):
        import ssl
        environ = dict(env or {})
        with patch.object(console.sys, "platform", platform), \
                patch.dict(console.os.environ, environ, clear=True), \
                patch.object(ssl, "create_default_context", return_value=_Context(ca_count)):
            console.ensure_ca_bundle()
            return dict(console.os.environ)

    def test_empty_macos_store_uses_certifi(self):
        import certifi
        self.assertEqual(self._run().get("SSL_CERT_FILE"), certifi.where())

    def test_working_store_is_left_alone(self):
        self.assertNotIn("SSL_CERT_FILE", self._run(ca_count=150))

    def test_other_platforms_are_left_alone(self):
        self.assertNotIn("SSL_CERT_FILE", self._run(platform="win32"))

    def test_user_setting_wins(self):
        self.assertEqual(self._run(env={"SSL_CERT_FILE": "/mine.pem"})["SSL_CERT_FILE"], "/mine.pem")


if __name__ == "__main__":
    unittest.main()
