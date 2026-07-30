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
