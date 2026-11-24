"""Optional Windows-user-bound credentials for the NTU ADFS login form."""
import base64
import getpass
import json
import sys
from pathlib import Path
from urllib.parse import urlsplit

from .storage import atomic_write_text
from . import console
from .i18n import t


def credential_file():
    return Path.home() / ".ntu-cool-gcm" / "credentials" / "ntu-cool.dpapi"


def offer_credentials():
    if sys.platform != "win32" or not console.stdin_is_interactive() or credential_file().is_file():
        return
    try:
        answer = input(t(
            "要在這台電腦加密記住臺大帳密，供 NTU COOL 自動登入嗎？[y/N] ",
            "Save your NTU credentials encrypted on this PC for automatic NTU COOL login? [y/N] ",
        )).strip().lower()
        if answer in {"y", "yes", "是", "好"}:
            if configure_credentials() == 0:
                print(t("已儲存臺大帳密。", "NTU credentials saved."))
    except EOFError:
        return


def configure_credentials(forget=False):
    path = credential_file()
    if forget:
        path.unlink(missing_ok=True)
        return 0
    if sys.platform != "win32" or not sys.stdin.isatty():
        print("請在 Windows 的互動式終端機設定帳密。")
        return 1
    import win32crypt
    username = input("臺大計中帳號：").strip()
    password = getpass.getpass("臺大密碼（輸入時隱藏）：")
    if not username or not password:
        print("帳號與密碼皆為必填。")
        return 1
    data = json.dumps([username, password]).encode()
    encrypted = win32crypt.CryptProtectData(data, "NTU COOL", None, None, None, 1)
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(path, base64.b64encode(encrypted).decode())
    return 0


def try_saved_login(page):
    path = credential_file()
    if sys.platform != "win32" or not path.is_file():
        return False
    from playwright.sync_api import Error
    try:
        url = urlsplit(page.url)
        if (url.scheme, url.netloc, url.path) == ("https", "cool.ntu.edu.tw", "/login/portal"):
            page.get_by_role("button", name="以計中帳號登入", exact=True).click(timeout=5000)
        page.wait_for_url("https://adfs.ntu.edu.tw/adfs/ls/**", timeout=5000)
        page.locator("#ContentPlaceHolder1_PasswordTextBox").wait_for(timeout=5000)
        import win32crypt
        encrypted = base64.b64decode(path.read_bytes(), validate=True)
        _, data = win32crypt.CryptUnprotectData(encrypted, None, None, None, 1)
        # Check the current document and submit destination in the same browser task.
        return page.evaluate("""([account, password]) => {
            if (location.origin !== 'https://adfs.ntu.edu.tw' || location.pathname !== '/adfs/ls/') return false;
            const user = document.querySelector('#ContentPlaceHolder1_UsernameTextBox');
            const pass = document.querySelector('#ContentPlaceHolder1_PasswordTextBox');
            const submit = document.querySelector('#ContentPlaceHolder1_SubmitButton');
            const form = pass?.form;
            if (!form || user?.form !== form || submit?.form !== form ||
                form.method.toLowerCase() !== 'post' || new URL(form.action).origin !== location.origin ||
                submit.hasAttribute('formaction') || submit.hasAttribute('formmethod')) return false;
            user.value = account;
            pass.value = password;
            submit.click();
            return true;
        }""", json.loads(data))
    except (Error, OSError, ValueError, ImportError):
        return False
    except Exception:
        # DPAPI can reject a file copied from another account. Keep manual login available.
        return False
