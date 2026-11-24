"""Run the notebooklm-py login CLI with Chromium sandboxing enabled."""
import sys
from functools import wraps


def main():
    from notebooklm.notebooklm_cli import main as notebooklm_main
    from playwright.sync_api import BrowserType

    # notebooklm-py 0.8.6 has no CLI switch for Playwright's sandbox option.
    launch = BrowserType.launch_persistent_context

    @wraps(launch)
    def sandboxed_launch(self, **kwargs):
        kwargs["chromium_sandbox"] = True
        return launch(self, **kwargs)

    BrowserType.launch_persistent_context = sandboxed_launch
    try:
        sys.argv.insert(1, "login")
        notebooklm_main()
    finally:
        BrowserType.launch_persistent_context = launch
        del sys.argv[1]


if __name__ == "__main__":
    main()
