"""Tests for savedlisting.py: the listing parser, the classifier and fetch_page.

Everything runs on SYNTHETIC html (listing_fixture.py) with redditclient.get
mocked -- no network, and no real .env: build_headers reads the cookie from the
process environment, which each test sets itself.

    python -m unittest tests.test_savedlisting
"""
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import redditclient
import savedlisting as sl
from listing_fixture import (FakeResponse, comment_thing, forbidden, page, thing)


def _rows(*things):
    return sl.parse_listing(page(list(things)))[0]


class ParseListingTest(unittest.TestCase):
    def test_every_shape_in_one_page_keeps_order(self):
        html = page([
            thing("t3_img", "https://i.redd.it/a.jpeg", "i.redd.it", title="Plain"),
            thing("t3_ext", "https://i.imgur.com/b.jpg", "i.imgur.com"),
            thing("t3_gal", "https://www.reddit.com/gallery/gal", "old.reddit.com",
                  is_gallery=True),
            thing("t3_vid", "https://v.redd.it/vid", "v.redd.it"),
            thing("t3_self", "/r/SomeSub/comments/self/a_title/", "self.SomeSub"),
            thing("t3_xp", "https://i.redd.it/c.png", "i.redd.it", subreddit="Crossposter",
                  crosspost_root="OriginalSub"),
        ])
        rows, nxt = sl.parse_listing(html)
        self.assertEqual([r["post_id"] for r in rows],
                         ["t3_img", "t3_ext", "t3_gal", "t3_vid", "t3_self", "t3_xp"])
        self.assertIsNone(nxt)

    def test_permalink_is_www_plus_the_relative_path(self):
        row = _rows(thing("t3_img", "https://i.redd.it/a.jpeg", "i.redd.it",
                          subreddit="Sub", permalink="/r/Sub/comments/img/a_title/"))[0]
        self.assertEqual(row["permalink"], "https://www.reddit.com/r/Sub/comments/img/a_title/")

    def test_non_ascii_title_and_permalink_survive(self):
        permalink = "/r/HuTao_Mains/comments/1y19i6t/hu_taoby_まつたけぇ/"
        row = _rows(thing("t3_jp", "https://i.redd.it/a.jpeg", "i.redd.it",
                          title="Hu Tao(by: まつたけぇ) ❤️", permalink=permalink))[0]
        self.assertEqual(row["title"], "Hu Tao(by: まつたけぇ) ❤️")
        self.assertEqual(row["permalink"], "https://www.reddit.com" + permalink)

    def test_html_entities_in_attributes_and_title_are_decoded(self):
        row = _rows(thing("t3_amp", "https://i.imgur.com/a.jpg?x=1&y=2", "i.imgur.com",
                          title="Tom & Jerry <3"))[0]
        self.assertEqual(row["url"], "https://i.imgur.com/a.jpg?x=1&y=2")
        self.assertEqual(row["title"], "Tom & Jerry <3")

    def test_saved_comments_and_promoted_posts_are_dropped(self):
        rows = _rows(
            comment_thing("t1_comment"),
            thing("t3_ad", "https://i.redd.it/ad.jpg", "i.redd.it", promoted=True),
            thing("t3_real", "https://i.redd.it/a.jpg", "i.redd.it"),
        )
        self.assertEqual([r["post_id"] for r in rows], ["t3_real"])

    def test_crosspost_root_subreddit_only_when_present(self):
        rows = _rows(
            thing("t3_plain", "https://i.redd.it/a.jpg", "i.redd.it"),
            thing("t3_xp", "https://i.redd.it/b.jpg", "i.redd.it", subreddit="Crossposter",
                  crosspost_root="Wuthering_Waves"),
        )
        self.assertIsNone(rows[0]["crosspost_root_subreddit"])
        self.assertEqual(rows[1]["crosspost_root_subreddit"], "Wuthering_Waves")
        self.assertEqual(rows[1]["subreddit"], "Crossposter")

    def test_num_crossposts_is_not_a_crosspost_marker(self):
        # the fixture gives every thing data-num-crossposts="3"
        row = _rows(thing("t3_plain", "https://i.redd.it/a.jpg", "i.redd.it"))[0]
        self.assertIsNone(row["crosspost_root_subreddit"])

    def test_next_button_present_and_absent(self):
        nxt = "https://old.reddit.com/user/TestUser/saved/?count=25&after=t3_img"
        _, found = sl.parse_listing(page([thing("t3_img", "https://i.redd.it/a.jpg", "i.redd.it")],
                                         next_href=nxt))
        self.assertEqual(found, nxt)
        _, missing = sl.parse_listing(page([thing("t3_img", "https://i.redd.it/a.jpg", "i.redd.it")]))
        self.assertIsNone(missing)

    def test_only_the_first_title_anchor_is_the_title(self):
        row = _rows(thing("t3_img", "https://i.redd.it/a.jpg", "i.redd.it", title="Real title"))[0]
        self.assertEqual(row["title"], "Real title")

    def test_empty_and_garbage_pages_yield_nothing(self):
        self.assertEqual(sl.parse_listing(""), ([], None))
        self.assertEqual(sl.parse_listing("<html><body>nope</body></html>"), ([], None))


class ClassifyRowTest(unittest.TestCase):
    def kind(self, *args, **kwargs):
        return sl.classify_row(_rows(thing("t3_x", *args, **kwargs))[0])

    def test_reddit_image(self):
        self.assertEqual(self.kind("https://i.redd.it/a.jpeg", "i.redd.it"),
                         ("image", None, "https://i.redd.it/a.jpeg"))

    def test_external_image_by_extension(self):
        self.assertEqual(self.kind("https://i.imgur.com/b.PNG", "i.imgur.com"),
                         ("image", None, "https://i.imgur.com/b.PNG"))

    def test_gallery_by_flag_even_though_domain_is_old_reddit(self):
        self.assertEqual(self.kind("https://www.reddit.com/gallery/g", "old.reddit.com",
                                   is_gallery=True), ("gallery", None, None))

    def test_gallery_by_url_alone(self):
        self.assertEqual(self.kind("https://www.reddit.com/gallery/g", "old.reddit.com"),
                         ("gallery", None, None))

    def test_gallery_is_checked_before_everything(self):
        self.assertEqual(self.kind("https://i.redd.it/a.jpg", "i.redd.it", is_gallery=True)[0],
                         "gallery")

    def test_video(self):
        self.assertEqual(self.kind("https://v.redd.it/abc", "v.redd.it"),
                         ("skip", "video_post", None))

    def test_self_post(self):
        self.assertEqual(self.kind("/r/SomeSub/comments/x/a_title/", "self.SomeSub"),
                         ("skip", "text_or_link_post", None))

    def test_external_link_that_is_not_an_image(self):
        self.assertEqual(self.kind("https://example.com/article", "example.com"),
                         ("skip", "unsupported_link_type", None))

    def test_crosspost_classifies_by_its_own_image_url(self):
        self.assertEqual(self.kind("https://i.redd.it/c.png", "i.redd.it", crosspost_root="Other"),
                         ("image", None, "https://i.redd.it/c.png"))


def _get_returning(*responses):
    return mock.patch.object(redditclient, "get", side_effect=list(responses))


class FetchPageTest(unittest.TestCase):
    URL = "https://old.reddit.com/user/TestUser/saved/"

    def setUp(self):
        patcher = mock.patch.dict(os.environ, {"REDDIT_SESSION_COOKIE": "reddit_session=fake-test-value"})
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_good_page_returns_rows_and_the_absolute_next_url(self):
        nxt = "https://old.reddit.com/user/TestUser/saved/?count=25&after=t3_a"
        html = page([thing("t3_a", "https://i.redd.it/a.jpg", "i.redd.it")], next_href=nxt)
        with _get_returning(FakeResponse(html)):
            rows, next_url = sl.fetch_page(self.URL)
        self.assertEqual([r["post_id"] for r in rows], ["t3_a"])
        self.assertEqual(next_url, nxt)

    def test_last_page_has_no_next_url(self):
        html = page([thing("t3_a", "https://i.redd.it/a.jpg", "i.redd.it")])
        with _get_returning(FakeResponse(html)):
            self.assertIsNone(sl.fetch_page(self.URL)[1])

    def test_403_without_the_logout_marker_is_an_expired_cookie(self):
        with _get_returning(FakeResponse(forbidden(logged_in=False), 403)):
            with self.assertRaises(sl.SavedAuthError) as ctx:
                sl.fetch_page(self.URL)
        self.assertFalse(ctx.exception.logged_in)

    def test_403_with_the_logout_marker_is_the_wrong_account(self):
        with _get_returning(FakeResponse(forbidden(logged_in=True, account="OtherUser"), 403)):
            with self.assertRaises(sl.SavedAuthError) as ctx:
                sl.fetch_page(self.URL)
        self.assertTrue(ctx.exception.logged_in)
        self.assertEqual(ctx.exception.account, "OtherUser")

    def test_200_without_the_logout_marker_is_logged_out(self):
        html = page([thing("t3_a", "https://i.redd.it/a.jpg", "i.redd.it")], logged_in=False)
        with _get_returning(FakeResponse(html)):
            with self.assertRaises(sl.SavedAuthError) as ctx:
                sl.fetch_page(self.URL)
        self.assertFalse(ctx.exception.logged_in)

    def test_login_wall_redirect_is_logged_out(self):
        wall = FakeResponse("<html>Log in to use old Reddit</html>",
                            url="https://old.reddit.com/login?dest=x&reason=lor2")
        with _get_returning(wall):
            with self.assertRaises(sl.SavedAuthError):
                sl.fetch_page(self.URL)

    def test_429_is_rate_limited(self):
        with _get_returning(FakeResponse("slow down", 429)):
            with self.assertRaises(sl.SavedRateLimited):
                sl.fetch_page(self.URL)

    def test_a_next_link_off_old_reddit_is_never_followed(self):
        html = page([thing("t3_a", "https://i.redd.it/a.jpg", "i.redd.it")],
                    next_href="https://evil.example.com/steal?after=t3_a")
        with _get_returning(FakeResponse(html)):
            with self.assertRaises(sl.SavedListingError):
                sl.fetch_page(self.URL)

    def test_a_relative_next_link_is_made_absolute_on_old_reddit(self):
        html = page([thing("t3_a", "https://i.redd.it/a.jpg", "i.redd.it")],
                    next_href="/user/TestUser/saved/?count=25&after=t3_a")
        with _get_returning(FakeResponse(html)):
            self.assertEqual(sl.fetch_page(self.URL)[1],
                             "https://old.reddit.com/user/TestUser/saved/?count=25&after=t3_a")

    def test_the_cookie_is_sent_but_never_part_of_the_url(self):
        html = page([thing("t3_a", "https://i.redd.it/a.jpg", "i.redd.it")])
        with _get_returning(FakeResponse(html)) as get:
            sl.fetch_page(self.URL)
        args, kwargs = get.call_args
        self.assertNotIn("fake-test-value", args[0])
        self.assertIn("fake-test-value", kwargs["headers"]["Cookie"])


class HelpersTest(unittest.TestCase):
    def test_listing_url(self):
        self.assertEqual(sl.listing_url("TestUser"),
                         "https://old.reddit.com/user/TestUser/saved/")
        self.assertEqual(sl.listing_url(" /Test User "),
                         "https://old.reddit.com/user/Test%20User/saved/")

    def test_logged_in_account_reads_the_header(self):
        self.assertEqual(sl.logged_in_account(page([], account="TestUser")), "TestUser")
        self.assertEqual(sl.logged_in_account(forbidden(logged_in=False)), "")


if __name__ == "__main__":
    unittest.main()
