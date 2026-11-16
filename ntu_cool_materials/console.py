"""Console / platform helpers: interactive stdin, the Python command name, CA certs."""
from __future__ import annotations

import os
import sys


def stdin_is_interactive() -> bool:
    """True only for a real terminal.

    On Windows `isatty()` is also true for the NUL device, so `command <nul`
    (used by non-interactive commands to mean "never prompt") would still open login
    windows and wait on questions. A console input handle is the reliable test.
    """
    stream = sys.stdin
    try:
        if stream is None or not stream.isatty():
            return False
        if os.name != "nt":
            return True
        import ctypes
        import msvcrt
        handle = msvcrt.get_osfhandle(stream.fileno())
        mode = ctypes.c_uint32()
        return bool(ctypes.windll.kernel32.GetConsoleMode(handle, ctypes.byref(mode)))
    except (AttributeError, OSError, TypeError, ValueError):
        return False


def python_cmd() -> str:
    """The Python command to show in copy-paste hints.

    macOS and most Linux installs only ship `python3`; a bare `python` there is
    either missing or the system's ancient one, so hints must say `python3`.
    """
    return "python" if os.name == "nt" else "python3"


def pip_install_hint(spec: str, *, upgrade: bool = True) -> str:
    flag = " --upgrade" if upgrade else ""
    return f"{python_cmd()} -m pip install{flag} {spec}"


def ensure_ca_bundle() -> None:
    """Make HTTPS work on python.org's macOS Python before "Install Certificates".

    That build links its own OpenSSL with an empty trust store until the user
    runs `Install Certificates.command`, so every urllib call to NTU COOL / PyPI
    fails with CERTIFICATE_VERIFY_FAILED. When that store is empty, point
    OpenSSL at certifi's bundle (installed with `requests`). Child processes
    (yt-dlp, ffmpeg) inherit it too. No-op elsewhere, when the store already has
    certificates, or when the user set SSL_CERT_FILE / SSL_CERT_DIR themselves.
    """
    if sys.platform != "darwin":
        return
    if os.environ.get("SSL_CERT_FILE") or os.environ.get("SSL_CERT_DIR"):
        return
    try:
        import ssl
        if ssl.create_default_context().cert_store_stats().get("x509_ca"):
            return
        import certifi
        os.environ["SSL_CERT_FILE"] = certifi.where()
    except Exception:
        pass
