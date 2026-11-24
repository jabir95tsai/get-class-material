import base64
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from importlib.resources import files
from pathlib import Path
from unittest.mock import Mock, patch

from ntu_cool_materials import ntu_credentials as credentials


class WindowsCredentialTests(unittest.TestCase):
    def setUp(self):
        self.home = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.enterContext(patch.object(Path, "home", return_value=self.home))
        self.enterContext(patch.object(credentials, "sys", Mock(platform="win32")))
        credentials.sys.stdin.isatty.return_value = True
        self.dpapi = Mock()
        self.dpapi.CryptProtectData.return_value = b"opaque fixture"
        self.dpapi.CryptUnprotectData.return_value = ("NTU COOL", b'["fixture-user", "fixture-pass"]')
        self.enterContext(patch.dict(sys.modules, {"win32crypt": self.dpapi}))
        self.output = self.enterContext(redirect_stdout(io.StringIO()))
        self.path = self.home / ".ntu-cool-gcm" / "credentials" / "ntu-cool.dpapi"

    def test_save_and_forget_use_only_the_encrypted_file(self):
        with patch("builtins.input", return_value=" fixture-user "), patch.object(
            credentials.getpass, "getpass", return_value="fixture-pass"
        ):
            self.assertEqual(credentials.configure_credentials(), 0)
        payload, _, _, _, _, flags = self.dpapi.CryptProtectData.call_args.args
        self.assertEqual(json.loads(payload), ["fixture-user", "fixture-pass"])
        self.assertEqual(flags, 1)
        self.assertEqual(base64.b64decode(self.path.read_bytes(), validate=True), b"opaque fixture")
        self.assertNotIn("fixture-pass", self.output.getvalue())
        self.assertEqual(credentials.configure_credentials(forget=True), 0)
        self.assertFalse(self.path.exists())
        self.assertEqual(credentials.configure_credentials(forget=True), 0)

    def test_noninteractive_configuration_does_not_prompt_or_save(self):
        credentials.sys.stdin.isatty.return_value = False
        with patch("builtins.input") as prompt, patch.object(credentials.getpass, "getpass") as password:
            self.assertEqual(credentials.configure_credentials(), 1)
        prompt.assert_not_called()
        password.assert_not_called()
        self.dpapi.CryptProtectData.assert_not_called()
        self.assertFalse(self.path.exists())

    def seed_saved_login(self):
        self.path.parent.mkdir(parents=True)
        self.path.write_bytes(base64.b64encode(b"opaque fixture"))

    def test_declining_the_offer_leaves_no_credentials_file(self):
        with (
            patch.object(credentials.console, "stdin_is_interactive", return_value=True),
            patch("builtins.input", return_value="") as prompt,
            patch.object(credentials.getpass, "getpass") as password,
        ):
            credentials.offer_credentials()
        prompt.assert_called_once()
        password.assert_not_called()
        self.dpapi.CryptProtectData.assert_not_called()
        self.assertFalse(self.path.exists())

    def test_empty_replacement_input_preserves_the_saved_file(self):
        self.seed_saved_login()
        original = self.path.read_bytes()
        for user, password in ((" ", "fixture-pass"), ("fixture-user", "")):
            with (
                self.subTest(user=user),
                patch("builtins.input", return_value=user),
                patch.object(credentials.getpass, "getpass", return_value=password),
            ):
                self.assertEqual(credentials.configure_credentials(), 1)
                self.assertEqual(self.path.read_bytes(), original)
                self.dpapi.CryptProtectData.assert_not_called()

    def test_unreadable_credentials_leave_the_form_untouched(self):
        self.seed_saved_login()
        self.dpapi.CryptUnprotectData.side_effect = OSError("different fixture user")
        page = Mock(url="https://adfs.ntu.edu.tw/adfs/ls/")
        self.assertFalse(credentials.try_saved_login(page))
        self.dpapi.CryptUnprotectData.assert_called_once()
        page.evaluate.assert_not_called()

    def test_autofill_checks_the_live_document_and_form_before_submitting(self):
        self.seed_saved_login()
        # Playwright ships a Node runtime on every supported platform.
        node = files("playwright").joinpath("driver", "node.exe" if os.name == "nt" else "node")
        fixture = Path(__file__).parent / "fixtures" / "ntu_login_form.cjs"
        adfs_url = "https://adfs.ntu.edu.tw/adfs/ls/"
        cases = [
            ("valid form", {}, True),
            ("different document origin", {"pageUrl": "https://other.invalid/adfs/ls/"}, False),
            ("different document path", {"pageUrl": adfs_url + "other"}, False),
            ("different submit origin", {"action": "https://other.invalid/login"}, False),
            ("GET form", {"method": "get"}, False),
            ("detached password", {"detached": "#ContentPlaceHolder1_PasswordTextBox"}, False),
            ("detached username", {"detached": "#ContentPlaceHolder1_UsernameTextBox"}, False),
            ("detached submit", {"detached": "#ContentPlaceHolder1_SubmitButton"}, False),
            ("submit action override", {"override": "formaction"}, False),
            ("submit method override", {"override": "formmethod"}, False),
        ]
        for label, changes, allowed in cases:
            with self.subTest(label=label):
                page = Mock(url="https://cool.ntu.edu.tw/login/portal")
                observed = {}

                def evaluate(script, values):
                    request = dict(script=script, credentials=values, pageUrl=adfs_url,
                                   action="/adfs/ls/", method="post")
                    result = subprocess.run(
                        [str(node), str(fixture)], input=json.dumps(request | changes),
                        capture_output=True, text=True, check=True, timeout=10,
                    )
                    observed.update(json.loads(result.stdout))
                    return observed["result"]

                page.evaluate.side_effect = evaluate
                self.assertEqual(credentials.try_saved_login(page), allowed)
                page.get_by_role.assert_called_once_with("button", name="以計中帳號登入", exact=True)
                page.get_by_role.return_value.click.assert_called_once()
                page.wait_for_url.assert_called_once_with("https://adfs.ntu.edu.tw/adfs/ls/**", timeout=5000)
                self.assertEqual(observed, {
                    "result": allowed, "user": "fixture-user" if allowed else "",
                    "pass": "fixture-pass" if allowed else "", "clicks": int(allowed),
                })
