"""Test helper: synthetic old.reddit saved-listing HTML.

Hand-built from the shapes measured on a real account (see savedlisting.py's
docstring) -- no real post, user or token in it. Not a test module (the name
doesn't match test*.py).
"""
from html import escape

LOGOUT = '<form class="logout hover" action="/logout" method="post"></form>'


def thing(fullname, url, domain, *, title="A title", subreddit="SomeSub", permalink=None,
          is_gallery=False, promoted=False, crosspost_root=None, extra=""):
    slug = fullname[3:]
    if permalink is None:
        permalink = f"/r/{subreddit}/comments/{slug}/a_title/"
    attrs = {
        "data-fullname": fullname,
        "data-type": "link",
        "data-is-gallery": "true" if is_gallery else "false",
        "data-subreddit": subreddit,
        "data-url": url,
        "data-permalink": permalink,
        "data-domain": domain,
        "data-score": "123",
        "data-timestamp": "1795290161000",
        "data-promoted": "true" if promoted else "false",
        "data-num-crossposts": "3",
    }
    if crosspost_root:
        attrs["data-crosspost-root-subreddit"] = crosspost_root
        attrs["data-target-root-fullname"] = "t3_rootpost"
    attr_text = " ".join(f'{k}="{escape(v, quote=True)}"' for k, v in attrs.items())
    return (
        f'<div class=" thing id-{fullname} saved link " id="thing_{fullname}" {attr_text} {extra}>'
        f'<div class="entry"><p class="title">'
        f'<a class="title may-blank outbound" href="{escape(url)}">{escape(title)}</a>'
        f'<span class="domain">(<a href="/domain/x/">{escape(domain)}</a>)</span></p>'
        f'<ul class="flat-list buttons"><li><a class="bylink comments" href="x">3 comments</a></li></ul>'
        f'</div></div>'
    )


def comment_thing(fullname):
    return (f'<div class=" thing id-{fullname} comment " data-fullname="{fullname}" '
            f'data-type="comment" data-subreddit="SomeSub"><div class="entry">'
            f'<a class="title" href="x">a saved comment</a></div></div>')


def page(things, *, next_href=None, logged_in=True, account="TestUser"):
    header = (f'<div id="header-bottom-right"><span class="user">'
              f'<a href="https://old.reddit.com/user/{account}/">{account}</a></span>'
              f'{LOGOUT if logged_in else ""}</div>')
    nxt = (f'<div class="nav-buttons"><span class="nextprev"><span class="next-button">'
           f'<a href="{escape(next_href)}" rel="nofollow next">next &rsaquo;</a></span></span></div>'
           if next_href else "")
    return (f'<html><head><title>saved</title></head><body>{header}'
            f'<div id="siteTable" class="sitetable linklisting">{"".join(things)}{nxt}</div>'
            f'</body></html>')


def forbidden(logged_in=False, account="OtherUser"):
    header = (f'<div id="header-bottom-right"><span class="user">'
              f'<a href="https://old.reddit.com/user/{account}/">{account}</a></span>{LOGOUT}</div>'
              if logged_in else "")
    return (f'<html><head><title>reddit.com: forbidden (reddit.com)</title></head><body>'
            f'{header}<div class="content">you are not allowed to do that</div></body></html>')


class FakeResponse:
    def __init__(self, text, status_code=200, url="https://old.reddit.com/user/TestUser/saved/"):
        self.text = text
        self.status_code = status_code
        self.url = url
        self.headers = {}

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")
