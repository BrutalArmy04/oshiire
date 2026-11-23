"""Tests for ingest.py's saved-listing walk: the stop rule, per-page checkpoints,
how each kind of post is filed, and what happens on an expired cookie, a wrong
account or a rate limit.

redditclient / savedlisting.fetch_page, the image download and the gallery fetch
are all mocked; time.sleep is mocked so backoff costs nothing. Each test runs in
a throwaway cwd (the manifest and staging/ are cwd-relative) and never reads
the real .env: main() is entered with load_dotenv patched out and the two
credentials set in a patched os.environ.

    python -m unittest tests.test_ingest_walk
"""
import contextlib
import io
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import ingest
import manifest as manifest_mod
import redditclient
import savedlisting as sl
from listing_fixture import thing

USER = "TestUser"


def row(post_id, url="https://i.redd.it/a.jpg", domain="i.redd.it", **kw):
    from listing_fixture import page
    html = page([thing(post_id, url, domain, **kw)])
    return sl.parse_listing(html)[0][0]


def rows(*post_ids):
    return [row(pid) for pid in post_ids]


class WalkBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        prev = os.getcwd()
        os.chdir(self.tmp.name)
        self.addCleanup(os.chdir, prev)
        self.sleeps = []
        for patcher in (
            mock.patch.object(ingest.time, "sleep", side_effect=self.sleeps.append),
            mock.patch.object(ingest, "download_image", side_effect=self._download),
            mock.patch.dict(os.environ, {"REDDIT_USERNAME": USER,
                                         "REDDIT_SESSION_COOKIE": "reddit_session=fake"}),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        ingest.STAGING_DIR.mkdir(parents=True, exist_ok=True)
        self.downloaded = []

    def _download(self, url, dest):
        self.downloaded.append(url)
        Path(dest).write_bytes(b"img")

    def pages(self, *pages):
        """Serve `pages` (each a list of rows) in order, chained by next URLs."""
        queue = []
        for i, page_rows in enumerate(pages):
            nxt = f"https://old.reddit.com/user/{USER}/saved/?count={25 * (i + 1)}&after=x" \
                if i < len(pages) - 1 else None
            queue.append((page_rows, nxt))
        return mock.patch.object(sl, "fetch_page", side_effect=queue)

    def walk(self, manifest, **counts):
        base = {"downloaded": 0, "skipped": 0, "failed": 0, "pages": 0}
        base.update(counts)
        ingest.walk_saved(manifest, USER, base)
        return base


class StopRuleTest(WalkBase):
    def test_walks_until_a_page_with_no_unknown_post(self):
        known = {"t3_old1": {"post_id": "t3_old1", "status": "pending_review"},
                 "t3_old2": {"post_id": "t3_old2", "status": "rejected"}}
        with self.pages(rows("t3_new1", "t3_new2"), rows("t3_new3", "t3_old1"),
                        rows("t3_old1", "t3_old2"), rows("t3_never")) as fetch:
            counts = self.walk(known)
        self.assertEqual(fetch.call_count, 3)          # stopped on the all-known page
        self.assertEqual(counts["pages"], 3)
        self.assertEqual(counts["downloaded"], 3)
        self.assertNotIn("t3_never", known)

    def test_stops_when_there_is_no_next_page(self):
        with self.pages(rows("t3_a"), rows("t3_b")) as fetch:
            counts = self.walk({})
        self.assertEqual(fetch.call_count, 2)
        self.assertEqual(counts["downloaded"], 2)

    def test_first_page_all_known_is_one_request(self):
        known = {"t3_a": {"post_id": "t3_a", "status": "approved"}}
        with self.pages(rows("t3_a"), rows("t3_b")) as fetch:
            counts = self.walk(known)
        self.assertEqual((fetch.call_count, counts["pages"], counts["downloaded"]), (1, 1, 0))

    def test_a_gallery_posts_children_count_as_known_by_post_id(self):
        known = {"t3_g_1": {"post_id": "t3_g", "image_index": 1, "status": "pending_review"},
                 "t3_g_2": {"post_id": "t3_g", "image_index": 2, "status": "pending_review"}}
        with self.pages(rows("t3_g")) as fetch:
            self.walk(known)
        self.assertEqual(fetch.call_count, 1)
        self.assertEqual(self.downloaded, [])

    def test_page_cap(self):
        with mock.patch.object(ingest, "MAX_PAGES", 2), \
                mock.patch.object(sl, "fetch_page", side_effect=[
                    (rows("t3_a"), "https://old.reddit.com/n1"),
                    (rows("t3_b"), "https://old.reddit.com/n2"),
                    (rows("t3_c"), None)]) as fetch, \
                contextlib.redirect_stderr(io.StringIO()) as err:
            counts = self.walk({})
        self.assertEqual((fetch.call_count, counts["pages"]), (2, 2))
        self.assertIn("2-page limit", err.getvalue())

    def test_sleeps_between_pages_not_before_the_first(self):
        with self.pages(rows("t3_a"), rows("t3_b"), rows("t3_c")):
            self.walk({})
        self.assertEqual(self.sleeps, [ingest.PAGE_DELAY_S] * 2)

    def test_a_repeated_post_within_a_walk_is_filed_once(self):
        with self.pages(rows("t3_a", "t3_a")):
            counts = self.walk({})
        self.assertEqual(counts["downloaded"], 1)
        self.assertEqual(len(self.downloaded), 1)


class CheckpointTest(WalkBase):
    def test_manifest_is_saved_after_every_page(self):
        saved = []
        real = manifest_mod.save_manifest

        def spy(data, *a, **k):
            saved.append(sorted(data))
            real(data, *a, **k)

        with mock.patch.object(ingest, "save_manifest", side_effect=spy), \
                self.pages(rows("t3_a"), rows("t3_b")):
            self.walk({})
        self.assertEqual(saved, [["t3_a"], ["t3_a", "t3_b"]])

    def test_a_failure_on_page_two_keeps_page_one_on_disk(self):
        manifest = {}
        queue = [(rows("t3_a"), "https://old.reddit.com/n1"),
                 sl.SavedAuthError("403")]
        with mock.patch.object(sl, "fetch_page", side_effect=queue):
            with self.assertRaises(sl.SavedAuthError):
                self.walk(manifest)
        on_disk = manifest_mod.load_manifest()
        self.assertEqual(list(on_disk), ["t3_a"])
        self.assertEqual(on_disk["t3_a"]["status"], "pending_review")


class FilingTest(WalkBase):
    def file_one(self, listing_row, **patches):
        manifest = {}
        with mock.patch.object(sl, "fetch_page", return_value=([listing_row], None)):
            self.walk(manifest)
        return manifest

    def test_image_is_downloaded_and_pending(self):
        m = self.file_one(row("t3_a", permalink="/r/S/comments/a/t/", subreddit="S"))
        entry = m["t3_a"]
        self.assertEqual(entry["status"], "pending_review")
        self.assertEqual(entry["permalink"], "https://www.reddit.com/r/S/comments/a/t/")
        self.assertEqual(entry["image_url"], "https://i.redd.it/a.jpg")
        self.assertEqual(Path(entry["local_path"]).name, "t3_a.jpg")
        self.assertNotIn("crosspost_root_subreddit", entry)

    def test_external_image_is_downloaded(self):
        m = self.file_one(row("t3_a", "https://i.imgur.com/b.png", "i.imgur.com"))
        self.assertEqual(m["t3_a"]["status"], "pending_review")
        self.assertEqual(self.downloaded, ["https://i.imgur.com/b.png"])

    def test_video_self_and_other_use_the_existing_skip_reasons(self):
        for pid, url, domain, reason in (
            ("t3_v", "https://v.redd.it/x", "v.redd.it", "video_post"),
            ("t3_s", "/r/S/comments/s/t/", "self.S", "text_or_link_post"),
            ("t3_o", "https://example.com/x", "example.com", "unsupported_link_type"),
        ):
            with self.subTest(reason=reason):
                m = self.file_one(row(pid, url, domain))
                self.assertEqual((m[pid]["status"], m[pid]["skip_reason"]), ("skipped", reason))
        self.assertEqual(self.downloaded, [])

    def test_crosspost_root_subreddit_is_stored_when_present(self):
        m = self.file_one(row("t3_xp", subreddit="Crossposter", crosspost_root="Original"))
        self.assertEqual(m["t3_xp"]["crosspost_root_subreddit"], "Original")
        self.assertEqual(m["t3_xp"]["subreddit"], "Crossposter")

    def test_gallery_goes_through_the_existing_gallery_flow(self):
        gallery = row("t3_g", "https://www.reddit.com/gallery/g", "old.reddit.com",
                      is_gallery=True, crosspost_root="Original")
        with mock.patch.object(ingest, "fetch_gallery_images",
                               return_value=[("m1", ".jpg"), ("m2", ".png")]) as fetch:
            m = self.file_one(gallery)
        fetch.assert_called_once_with("https://www.reddit.com/r/SomeSub/comments/g/a_title/")
        self.assertEqual(sorted(m), ["t3_g_1", "t3_g_2"])
        self.assertEqual({e["post_id"] for e in m.values()}, {"t3_g"})
        self.assertEqual({e["crosspost_root_subreddit"] for e in m.values()}, {"Original"})

    def test_gallery_failures_stay_retryable_skips(self):
        gallery = row("t3_g", "https://www.reddit.com/gallery/g", "old.reddit.com", is_gallery=True)
        for exc, reason in (
            (redditclient.RedditFetchError("boom"), "gallery_fetch_failed"),
            (ingest.RedditAuthWall("wall"), "auth_walled"),
            (ingest.GalleryParseError("drift"), "gallery_parse_error"),
        ):
            with self.subTest(reason=reason), \
                    mock.patch.object(ingest, "fetch_gallery_images", side_effect=exc), \
                    contextlib.redirect_stderr(io.StringIO()):
                m = self.file_one(gallery)
                self.assertEqual(m["t3_g"]["skip_reason"], reason)
                self.assertIn(reason, ingest.RETRYABLE_SKIP_REASONS)

    def test_gallery_with_no_images_settles(self):
        gallery = row("t3_g", "https://www.reddit.com/gallery/g", "old.reddit.com", is_gallery=True)
        with mock.patch.object(ingest, "fetch_gallery_images", return_value=[]):
            m = self.file_one(gallery)
        self.assertEqual(m["t3_g"]["skip_reason"], "gallery_no_images")

    def test_a_failed_download_is_recorded_not_fatal(self):
        import requests
        with mock.patch.object(ingest, "download_image",
                               side_effect=requests.RequestException("nope")):
            m = self.file_one(row("t3_a"))
        self.assertEqual(m["t3_a"]["status"], "download_failed")

    def test_an_unexpected_error_becomes_an_error_entry(self):
        with mock.patch.object(sl, "classify_row", side_effect=ValueError("odd")):
            m = self.file_one(row("t3_a"))
        self.assertEqual((m["t3_a"]["status"], m["t3_a"]["reason"]), ("error", "odd"))

    def test_dedup_is_by_post_id_not_permalink(self):
        known = {"t3_a": {"post_id": "t3_a", "permalink": "https://old.reddit.com/other/",
                          "status": "approved"}}
        with self.pages(rows("t3_a")):
            counts = self.walk(known)
        self.assertEqual(counts["downloaded"], 0)
        self.assertEqual(known["t3_a"]["permalink"], "https://old.reddit.com/other/")


class BackoffTest(WalkBase):
    def test_a_429_is_retried_on_backfills_schedule_then_succeeds(self):
        queue = [sl.SavedRateLimited("429"), redditclient.RedditFetchError("timeout"),
                 (rows("t3_a"), None)]
        with mock.patch.object(sl, "fetch_page", side_effect=queue), \
                contextlib.redirect_stderr(io.StringIO()):
            counts = self.walk({})
        self.assertEqual(self.sleeps, [30, 60])
        self.assertEqual(counts["downloaded"], 1)

    def test_giving_up_after_the_whole_schedule(self):
        with mock.patch.object(sl, "fetch_page", side_effect=sl.SavedRateLimited("429")), \
                contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(sl.SavedRateLimited):
                self.walk({})
        self.assertEqual(self.sleeps, [30, 60, 120, 240])

    def test_an_auth_failure_is_not_retried(self):
        with mock.patch.object(sl, "fetch_page", side_effect=sl.SavedAuthError("403")) as fetch:
            with self.assertRaises(sl.SavedAuthError):
                self.walk({})
        self.assertEqual(fetch.call_count, 1)
        self.assertEqual(self.sleeps, [])


class MainTest(WalkBase):
    def run_main(self, fetch_side_effect=None, env=None):
        err, out = io.StringIO(), io.StringIO()
        env_patch = mock.patch.dict(os.environ, env or {})
        with env_patch, mock.patch.object(ingest, "load_dotenv"), \
                mock.patch.object(ingest, "run_tagging", return_value=[]), \
                mock.patch.object(ingest, "retry_skipped_galleries",
                                  return_value={"retried": 0, "expanded": 0}), \
                mock.patch.object(sys, "argv", ["ingest.py"]), \
                mock.patch.object(sl, "fetch_page", side_effect=fetch_side_effect), \
                contextlib.redirect_stderr(err), contextlib.redirect_stdout(out):
            try:
                ingest.main()
                code = 0
            except SystemExit as exc:
                code = exc.code
        return code, out.getvalue(), err.getvalue()

    def test_missing_credentials_exit_1_without_a_request(self):
        for env in ({"REDDIT_USERNAME": ""}, {"REDDIT_SESSION_COOKIE": ""}):
            with self.subTest(env=env):
                with mock.patch.object(sl, "fetch_page") as fetch:
                    code, _, err = self.run_main(env=env)
                self.assertEqual(code, 1)
                self.assertIn("not set", err)
                fetch.assert_not_called()

    def test_expired_cookie_says_so_saves_progress_and_exits_1(self):
        code, out, err = self.run_main([(rows("t3_a"), "https://old.reddit.com/n"),
                                        sl.SavedAuthError("403", logged_in=False)])
        self.assertEqual(code, 1)
        self.assertIn("cookie has expired", err)
        self.assertIn("Settings -> App", err)
        self.assertNotIn("fake", err + out)             # the cookie is never printed
        self.assertIn("pages=1", out)                   # only pages actually fetched
        self.assertIn("t3_a", manifest_mod.load_manifest())

    def test_wrong_account_names_the_username(self):
        code, _, err = self.run_main([sl.SavedAuthError("403", logged_in=True, account="Other")])
        self.assertEqual(code, 1)
        self.assertIn(f"different account than {USER}", err)

    def test_rate_limit_stops_cleanly_with_what_was_saved(self):
        with mock.patch.object(sl, "fetch_page", side_effect=[
                (rows("t3_a"), "https://old.reddit.com/n")] + [sl.SavedRateLimited("429")] * 5):
            err, out = io.StringIO(), io.StringIO()
            with mock.patch.object(ingest, "load_dotenv"), \
                    mock.patch.object(sys, "argv", ["ingest.py"]), \
                    contextlib.redirect_stderr(err), contextlib.redirect_stdout(out):
                with self.assertRaises(SystemExit) as ctx:
                    ingest.main()
        self.assertEqual(ctx.exception.code, 1)
        self.assertIn("Rate limited, try later", err.getvalue())
        self.assertIn("t3_a", manifest_mod.load_manifest())

    def test_success_prints_pages_in_the_summary(self):
        code, out, _ = self.run_main([(rows("t3_a"), None)])
        self.assertEqual(code, 0)
        self.assertIn("downloaded=1", out)
        self.assertIn("pages=1", out)

    def test_the_old_feed_url_is_not_needed_or_read(self):
        code, _, _ = self.run_main([(rows("t3_a"), None)],
                                   env={"REDDIT_SAVED_FEED_URL": ""})
        self.assertEqual(code, 0)


class AuthMessageTest(unittest.TestCase):
    def test_the_three_messages(self):
        expired = sl.auth_message(sl.SavedAuthError("x"), USER)
        self.assertIn("expired", expired)
        other = sl.auth_message(sl.SavedAuthError("x", True, "Someone"), USER)
        self.assertIn(f"different account than {USER}", other)
        self.assertIn("Someone", other)
        same = sl.auth_message(sl.SavedAuthError("x", True, USER.lower()), USER)
        self.assertNotIn("different account", same)


if __name__ == "__main__":
    unittest.main()
