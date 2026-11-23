"""The logged-in old.reddit saved listing: fetch a page, parse it, classify a post.

Reddit retired the saved-posts RSS feed on 2026-11-13. What replaces it is the
server-rendered listing at https://old.reddit.com/user/<name>/saved/, read with
the same cookie and the same Chrome-impersonated transport the gallery fetch
already uses (`redditclient.get`). Three callers share this module so none of
them re-learns the markup: `ingest.py` (the walk), `setup_window.py` (the Test
button: ONE page) and the tests.

What the listing looks like (measured against a real account, not guessed):
  * 25 posts a page, each a `div.thing` carrying its facts as `data-*`
    attributes, newest SAVE first. Post ids are `t3_...`; a `t1_...` thing is a
    saved comment and is skipped.
  * The next page is the `.next-button a` href, absolute, with
    `?count=<n>&after=<last fullname>`. The last page has no next button.
  * `data-permalink` is RELATIVE and may hold raw non-ASCII. Everything is a
    string -- `data-is-gallery` is 'true'/'false', never a bool.
  * An expired or wrong cookie does NOT show the login wall here: the listing
    answers HTTP 403 "you are not allowed to do that". A logged-in page carries
    a logout form/link; that marker is how a good session is told from a bad
    one, and how a 403 for the wrong ACCOUNT is told from an expired cookie.
  * Not used, on purpose: `data-num-crossposts` (a count, not a flag),
    `data-score` (drifts between fetches) and `data-timestamp` (the post's
    creation time, not when it was saved).

STDLIB parsing (`html.parser`), no new dependency. The one network call is
`fetch_page`; everything else is pure and takes text.
"""
import re
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import quote, urlparse, urlsplit

import redditclient
from useragent import build_headers, is_login_wall

LISTING_HOST = "old.reddit.com"
WWW_ORIGIN = "https://www.reddit.com"
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif", ".webp"}

_LOGOUT_RE = re.compile(
    r'<form[^>]*logout|href="[^"]*logout|class="[^"]*\blogout\b',
    re.IGNORECASE,
)
_HEADER_RE = re.compile(r'id="header-bottom-right"')
_HEADER_SPAN = 2500
_HEADER_USER_RE = re.compile(r'href="(?:https?://[^"/]*)?/user/([^/"?#]+)', re.IGNORECASE)


class SavedAuthError(Exception):
    """The listing did not come back logged in.

    `logged_in` is True when the page carried the logout marker anyway -- a 403
    for an account that is signed in but is not the one whose saves were asked
    for. `account` is that signed-in name when the header showed one, else ''.
    """

    def __init__(self, message, logged_in=False, account=""):
        super().__init__(message)
        self.logged_in = logged_in
        self.account = account


class SavedRateLimited(Exception):
    """Reddit answered HTTP 429."""


class SavedListingError(Exception):
    """The listing could not be followed (e.g. a next link off old.reddit.com)."""


def listing_url(username):
    """The first page of `username`'s saved listing."""
    return f"https://{LISTING_HOST}/user/{quote(username.strip().lstrip('/'), safe='')}/saved/"


def has_logout_marker(text):
    """True when the page has the logout form/link only a logged-in page shows."""
    return bool(_LOGOUT_RE.search(text or ""))


def logged_in_account(text):
    """The signed-in account name from the page header, or '' if not shown."""
    match = _HEADER_RE.search(text or "")
    if not match:
        return ""
    user = _HEADER_USER_RE.search(text[match.start():match.start() + _HEADER_SPAN])
    return user.group(1) if user else ""


class _ListingParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.things = []
        self.next_href = None
        self._depth = 0
        self._cur = None
        self._cur_depth = None
        self._in_title = False
        self._in_next = False

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        classes = (attrs.get("class") or "").split()
        if tag == "div":
            self._depth += 1
            if "thing" in classes and attrs.get("data-fullname"):
                self._cur = {"attrs": attrs, "title": None}
                self._cur_depth = self._depth
                self.things.append(self._cur)
        elif tag == "span" and "next-button" in classes:
            self._in_next = True
        elif tag == "a":
            if self._in_next and self.next_href is None:
                self.next_href = attrs.get("href")
            if self._cur is not None and self._cur["title"] is None and "title" in classes:
                self._in_title = True
                self._cur["title"] = ""

    def handle_endtag(self, tag):
        if tag == "div":
            if self._cur is not None and self._depth == self._cur_depth:
                self._cur = None
            self._depth -= 1
        elif tag == "a":
            self._in_title = False
        elif tag == "span":
            self._in_next = False

    def handle_data(self, data):
        if self._in_title and self._cur is not None:
            self._cur["title"] += data


def _row(attrs, title):
    permalink = attrs.get("data-permalink") or ""
    if permalink.startswith("/"):
        permalink = WWW_ORIGIN + permalink
    return {
        "post_id": attrs["data-fullname"],
        "title": (title or "").strip(),
        "subreddit": attrs.get("data-subreddit") or "unknown",
        "permalink": permalink,
        "url": attrs.get("data-url") or "",
        "domain": attrs.get("data-domain") or "",
        "is_gallery": attrs.get("data-is-gallery") == "true",
        "crosspost_root_subreddit": attrs.get("data-crosspost-root-subreddit") or None,
    }


def parse_listing(html):
    """(rows, next_href) for one listing page's HTML.

    Only posts (`t3_`) come back, and promoted ones are dropped. `next_href` is
    the raw `.next-button a` href (None on the last page); `fetch_page` is what
    vets it before anything follows it.
    """
    parser = _ListingParser()
    parser.feed(html or "")
    rows = []
    for thing in parser.things:
        attrs = thing["attrs"]
        if not attrs["data-fullname"].startswith("t3_"):
            continue
        if attrs.get("data-promoted") == "true":
            continue
        rows.append(_row(attrs, thing["title"]))
    return rows, parser.next_href


def classify_row(row):
    """(kind, skip_reason, image_url) -- kind is "image", "gallery" or "skip".

    Galleries first, and never by domain: a gallery's `data-domain` is
    "old.reddit.com". "gallery" carries no reason or image_url -- the caller must
    fetch the permalink to learn what is in it (ingest.fetch_gallery_images).
    The skip reasons are the ones already in the manifest.
    """
    url = row["url"]
    if row["is_gallery"] or "/gallery/" in url:
        return "gallery", None, None

    parsed = urlparse(url)
    host = (parsed.netloc or "").lower()
    domain = row["domain"].lower()
    if domain.startswith("self.") or url.startswith("/") or not url:
        return "skip", "text_or_link_post", None
    if domain == "v.redd.it" or host == "v.redd.it":
        return "skip", "video_post", None
    if (domain == "i.redd.it" or host == "i.redd.it"
            or Path(parsed.path).suffix.lower() in IMAGE_EXTENSIONS):
        return "image", None, url
    return "skip", "unsupported_link_type", None


def _follow_url(href):
    """The absolute next-page URL, or raise if it would leave old.reddit.com.

    The session cookie rides on every request, so a next link pointing anywhere
    else must never be followed with it."""
    absolute = f"https://{LISTING_HOST}{href}" if href.startswith("/") else href
    parts = urlsplit(absolute)
    if parts.scheme != "https" or (parts.hostname or "").lower() != LISTING_HOST:
        raise SavedListingError("the next-page link leaves old.reddit.com; not following it")
    return absolute


def auth_message(exc, username):
    """The one message for a saved listing that did not come back logged in.

    Shared by ingest and the setup window's Test button, so both say the same
    thing. A 403 that still carries the logout marker is a good session for the
    wrong account; without the marker the cookie is expired, missing or wrong."""
    if exc.logged_in:
        who = exc.account
        if who and who.lower() == username.lower():
            return (f"Reddit refused your saved listing (HTTP 403) although this login "
                    f"is signed in as {who}.")
        suffix = f" (it is signed in as {who})" if who else ""
        return f"This login is for a different account than {username}{suffix}."
    return "Your Reddit login cookie has expired or is wrong."


def fetch_page(url, cookie=None):
    """GET one listing page. Returns (rows, next_url) -- next_url None on the last.

    `cookie` is for the setup window's Test button (a value not saved yet);
    everything else lets build_headers read REDDIT_SESSION_COOKIE.

    Raises:
        SavedAuthError -- HTTP 403, the login wall, or a 200 without the logout
            marker. A 403 that still has the marker is the wrong ACCOUNT.
        SavedRateLimited -- HTTP 429.
        redditclient.RedditFetchError -- network failure or another bad status.
        SavedListingError -- a next link that must not be followed.
    """
    resp = redditclient.get(url, headers=build_headers(over18=True, cookie=cookie))
    text = resp.text or ""
    logged_in = has_logout_marker(text)
    if resp.status_code == 403:
        raise SavedAuthError("HTTP 403 from the saved listing", logged_in=logged_in,
                             account=logged_in_account(text) if logged_in else "")
    if resp.status_code == 429:
        raise SavedRateLimited("HTTP 429 from the saved listing")
    redditclient.raise_for_status(resp)
    if is_login_wall(resp.url, text) or not logged_in:
        raise SavedAuthError("the saved listing came back logged out")
    rows, next_href = parse_listing(text)
    return rows, (_follow_url(next_href) if next_href else None)
