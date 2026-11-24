"""Canvas errors shared by the API clients and HTTP transport."""


class CanvasAPIError(RuntimeError):
    """Raised when Canvas returns an error response.

    `status_code` is the HTTP response code when the error originated from
    an HTTPError (None for parse / decode failures). Lets callers distinguish
    between "your session expired" (401) and other failures so they can
    auto-recover with a fresh login instead of just printing and exiting.
    """

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class SessionExpiredError(CanvasAPIError):
    """Raised when Canvas returns 401 — the saved login is no longer valid.

    Callers should catch this specifically to trigger a re-authentication
    flow rather than just bubbling the error up to the user. Subclasses
    CanvasAPIError so existing `except CanvasAPIError` blocks continue to
    catch it; `status_code` is fixed at 401.
    """

    def __init__(self, message: str) -> None:
        super().__init__(message, status_code=401)
