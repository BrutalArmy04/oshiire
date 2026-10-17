"""The one outbound transport for old.reddit.com HTML pages.

At the end of July 2026 old.reddit.com began serving its login page to
logged-out requests. A valid `REDDIT_SESSION_COOKIE` is necessary but NOT
sufficient: plain `requests` (and PowerShell) hit the wall carrying the exact
same cookie jar and the exact same browser headers. What gets through is the
TLS/HTTP2 fingerprint -- `curl_cffi` with `impersonate="chrome"` presents a
real browser's JA3 and HTTP2 settings, and the same cookie then authenticates.
So the deciding factor is the transport, not the headers.

Three callers fetch old.reddit HTML and all three go through here:
`ingest.fetch_gallery_images` (gallery permalinks), `backfill.fetch_post_html`
(the CSV sweep) and `calibrate.fetch_post_html` (the threshold measurement
run). The `impersonate="chrome"` string lives ONLY in this module -- it is the
one knob that decides whether any of them see real pages, and it should be
changed in one place.

This module is NOT the place for anything that isn't walled. The www RSS feed
and the i.redd.it image downloads work fine on plain `requests`; they stay
there. Impersonation costs a native dependency and gives them nothing.

Deliberately separate from `useragent.py`, which is STDLIB-ONLY by contract so
that anything can import the shared UA definition without dragging a
dependency along. This module is the opposite: it owns the one heavy network
dependency. Callers keep importing `build_headers`/`is_login_wall` from
`useragent` and pass the headers in here -- the descriptive User-Agent that
Reddit's etiquette guidelines ask for survives impersonation intact (verified
on the wire), so impersonating the fingerprint does NOT mean impersonating the
identity.
"""
from curl_cffi import requests as _cffi_requests
from curl_cffi.requests.exceptions import CurlError as _CurlError

# The browser whose TLS/HTTP2 fingerprint we present. Intentionally the only
# occurrence in the project.
IMPERSONATE = "chrome"

DEFAULT_TIMEOUT = 15


class RedditFetchError(Exception):
    """The one error type callers catch -- every transport failure, normalized.

    `curl_cffi` raises out of its own hierarchy, which callers previously
    handled as `requests.RequestException`. Rather than have each caller learn
    a second exception vocabulary, everything the transport can raise is
    re-raised as this. `CurlError` is the base that actually covers the whole
    surface: every member of `curl_cffi.requests.exceptions` derives from
    `RequestException`, which in turn derives from `CurlError` -- but a bare
    `CurlError` is NOT a `RequestException`, so catching the narrower one would
    let raw libcurl failures escape.

    `kind` keeps the original exception's class name ("ReadTimeout",
    "ConnectionError", ...). Normalizing to one type would otherwise collapse
    backfill's per-cause failure tally into a single bucket; `kind` is what
    lets a log still say which failure it was.
    """

    def __init__(self, message: str, kind: str = ""):
        super().__init__(message)
        self.kind = kind or type(self).__name__


def get(url, *, headers=None, timeout=DEFAULT_TIMEOUT):
    """GET `url` over an impersonated connection, following redirects.

    Redirects are followed because the login wall IS a redirect: the reliable
    way to detect it is comparing the FINAL url against `/login`, which
    requires having followed it (see `useragent.is_login_wall`). Callers are
    expected to run that check on the result -- the wall is served as a normal
    200, so this function cannot tell it apart from content and does not try.

    Returns the `curl_cffi` response, which exposes the `.url` (already a
    `str`), `.text`, `.status_code` and `.headers` the callers use.

    Raises:
        RedditFetchError -- any network/DNS/TLS/timeout failure.
    """
    try:
        return _cffi_requests.get(
            url,
            headers=headers,
            impersonate=IMPERSONATE,
            allow_redirects=True,
            timeout=timeout,
        )
    except _CurlError as exc:
        raise RedditFetchError(str(exc), kind=type(exc).__name__) from exc


def raise_for_status(resp):
    """`resp.raise_for_status()`, with its error normalized like `get`'s.

    Separate from `get` because two of the three callers need to inspect
    `status_code` themselves rather than raise on it -- backfill distinguishes
    a retryable 429 from a permanently dead 403/404, and calibrate treats any
    non-200 as dead. Only the gallery fetch wants raise-on-error, and it would
    otherwise get a bare `curl_cffi` `HTTPError` straight past the handler that
    is catching `RedditFetchError`.
    """
    try:
        resp.raise_for_status()
    except _CurlError as exc:
        raise RedditFetchError(str(exc), kind=type(exc).__name__) from exc
