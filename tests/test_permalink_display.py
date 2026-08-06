"""manifest.display_permalink: reddit hosts normalize, everything else doesn't.

The manifest holds both spellings of the same URL -- ingest.py writes the RSS
feed's `old.reddit.com` links, backfill.py writes `www.reddit.com` ones from the
CSV export -- and the review/resolve screens render whichever they find. This
pins the display-time fix: the host is rewritten, the rest of the URL is byte-
for-byte preserved, and non-reddit hosts (notably the redd.it image domains) are
never touched.

Pure string work; reads and writes nothing.

    python -m unittest discover -s tests
    python tests/test_permalink_display.py
"""
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from manifest import display_permalink  # noqa: E402

POST_PATH = "/r/Genshin_Impact/comments/abc123/hu_tao_by_artist/"


class DisplayPermalinkTest(unittest.TestCase):
    def test_old_reddit_becomes_www(self):
        self.assertEqual(
            display_permalink("https://old.reddit.com" + POST_PATH),
            "https://www.reddit.com" + POST_PATH,
        )

    def test_www_reddit_unchanged(self):
        url = "https://www.reddit.com" + POST_PATH
        self.assertEqual(display_permalink(url), url)

    def test_bare_reddit_becomes_www(self):
        self.assertEqual(
            display_permalink("https://reddit.com" + POST_PATH),
            "https://www.reddit.com" + POST_PATH,
        )

    def test_other_reddit_subdomains_become_www(self):
        for host in ("new.reddit.com", "np.reddit.com", "sh.reddit.com"):
            with self.subTest(host=host):
                self.assertEqual(
                    display_permalink(f"https://{host}{POST_PATH}"),
                    "https://www.reddit.com" + POST_PATH,
                )

    def test_path_preserved_verbatim(self):
        self.assertEqual(
            display_permalink("https://old.reddit.com/r/Sub/comments/xy12z/a_title"),
            "https://www.reddit.com/r/Sub/comments/xy12z/a_title",
        )

    def test_trailing_slash_preserved(self):
        with_slash = display_permalink("https://old.reddit.com/r/Sub/comments/xy12z/t/")
        without = display_permalink("https://old.reddit.com/r/Sub/comments/xy12z/t")
        self.assertTrue(with_slash.endswith("/t/"))
        self.assertTrue(without.endswith("/t"))

    def test_query_string_preserved(self):
        self.assertEqual(
            display_permalink("https://old.reddit.com" + POST_PATH + "?utm_source=share&context=3"),
            "https://www.reddit.com" + POST_PATH + "?utm_source=share&context=3",
        )

    def test_redd_it_image_hosts_unchanged(self):
        # A different domain entirely -- rewriting these would break image URLs.
        for url in (
            "https://i.redd.it/abc123.jpg",
            "https://preview.redd.it/abc123.jpg?width=640&crop=smart",
            "https://redd.it/abc123",
        ):
            with self.subTest(url=url):
                self.assertEqual(display_permalink(url), url)

    def test_non_reddit_hosts_unchanged(self):
        for url in (
            "https://www.pixiv.net/artworks/18567314",
            "https://notreddit.com/r/Sub/comments/xy12z/",
            "https://reddit.com.evil.example/r/Sub/",
        ):
            with self.subTest(url=url):
                self.assertEqual(display_permalink(url), url)

    def test_empty_string(self):
        self.assertEqual(display_permalink(""), "")

    def test_unparseable_input_unchanged(self):
        for url in ("not a url at all", "://", "https://[oops"):
            with self.subTest(url=url):
                self.assertEqual(display_permalink(url), url)


if __name__ == "__main__":
    unittest.main()
