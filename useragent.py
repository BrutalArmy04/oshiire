"""The one definition of this project's outbound User-Agent header.

Every component that talks to Reddit identifies itself the same way, per
Reddit's API etiquette guidelines: a product token, a short description, and a
contact. Four callers need that string -- `ingest.py` (RSS feed + image
downloads + gallery permalinks), `backfill.py` (the CSV sweep), `calibrate.py`
(the threshold measurement run) and `tombstones.py` (fetching a placeholder
card to sign) -- so it lives here rather than being retyped in each.

STDLIB ONLY, DELIBERATELY. This module exists to be importable from anywhere
without dragging anything along: importing it pulls in `os` and nothing else.
That is what lets `calibrate.py` share the definition while staying independent
of the pipeline modules (manifest/tag), which was the whole reason it had its
own copy before.

The `product` argument is not decoration. `calibrate.py` passes
"oshiire-calibrate" so that a measurement run is distinguishable from ordinary
ingest traffic in any log Reddit keeps -- it hits the same endpoints at the same
cadence but for a different reason, and conflating the two would make a
rate-limit complaint impossible to attribute. Keep it a parameter, not a
constant.
"""
import os

# Bumped together with the project, not per-caller.
VERSION = "v0.1"

DEFAULT_PRODUCT = "oshiire"
DESCRIPTION = "personal saved-feed archiver"

# Used when REDDIT_USERNAME is blank. Deliberately still a route to a human:
# an unattributed UA is what Reddit's etiquette guidelines ask you not to send.
FALLBACK_CONTACT = "contact via GitHub issues"


def build_user_agent(product: str = DEFAULT_PRODUCT) -> str:
    """`<product>:<version> (<description>; <contact>)`.

    Contact is `/u/<name>` when REDDIT_USERNAME is set in the environment,
    else a generic pointer. Read at call time rather than import time so a
    caller that loads .env after importing this still gets the username.
    """
    username = os.environ.get("REDDIT_USERNAME", "").strip()
    contact = f"/u/{username}" if username else FALLBACK_CONTACT
    return f"{product}:{VERSION} ({DESCRIPTION}; {contact})"


class RedditAuthWall(Exception):
    """Raised when a Reddit fetch returns the login wall instead of content."""


def build_headers(product: str = DEFAULT_PRODUCT, *, over18: bool = False) -> dict:
    """Outbound headers for a Reddit request: User-Agent plus any Cookie.

    REDDIT_SESSION_COOKIE holds a logged-in cookie string copied from a
    browser (e.g. "reddit_session=..."). old.reddit.com began requiring login
    for its logged-out HTML/feed endpoints at the end of July 2026, so the
    CSV-sweep and gallery permalink fetches need this to receive real pages
    instead of the login wall. Read at call time rather than import time so a
    caller that loads .env after importing this still picks it up.

    `over18=True` prepends the `over18=1` consent cookie, which is what makes
    an NSFW permalink render instead of the interstitial. The Cookie header is
    omitted entirely when neither piece applies.
    """
    headers = {"User-Agent": build_user_agent(product)}

    cookies = []
    if over18:
        cookies.append("over18=1")
    session = os.environ.get("REDDIT_SESSION_COOKIE", "").strip()
    if session:
        cookies.append(session)
    if cookies:
        headers["Cookie"] = "; ".join(cookies)

    return headers


def is_login_wall(final_url: str, text: str) -> bool:
    """True when a 200 response is old.reddit's login page, not the content.

    The login wall is served as a normal 200, so status codes cannot tell it
    apart from a real page. The redirect target is the reliable signal --
    old.reddit sends you to `/login?dest=...&reason=lor2` -- so the final URL
    is checked first; the body markers are a fallback for the case where the
    caller could not hand back a post-redirect URL.
    """
    url = final_url or ""
    if "/login" in url or "reason=lor2" in url:
        return True

    body = text or ""
    return "reason=lor2" in body or "Log in to use old Reddit" in body
