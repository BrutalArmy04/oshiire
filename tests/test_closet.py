"""Tests for the "closet" home/job redesign: door labels, the Home states,
the pure result-sentence function, and the local-font guarantee.

These cover the parts of the redesign that are pure or state-driven; the
component-wiring contract itself is in tests/test_render_contract.py.

    python -m unittest discover -s tests
    python tests/test_closet.py
"""
import re
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from review_sandbox import ReviewSandbox  # noqa: E402

# review binds its manifest/layout/archive dir at import, and resolve exits when
# there's no layout.json: import a private copy inside a throwaway data root,
# and run this module's tests inside it too.
_sandbox = ReviewSandbox()
review = _sandbox.load()
setUpModule = _sandbox.activate
tearDownModule = _sandbox.close


def _field(update, field):
    """Read a field from a gr.update value (dict or object across versions)."""
    if isinstance(update, dict):
        return update.get(field)
    return getattr(update, field, None)


class DoorLabelTest(unittest.TestCase):
    def test_waiting_phrase_zero_one_many(self):
        self.assertEqual(review._waiting_phrase(0), "Nothing waiting")
        self.assertEqual(review._waiting_phrase(1), "1 image waiting")
        self.assertEqual(review._waiting_phrase(14), "14 images waiting")
        # Negatives (shouldn't happen) read as nothing, never a crash.
        self.assertEqual(review._waiting_phrase(-3), "Nothing waiting")

    def test_review_door_label_is_title_then_count(self):
        for n, phrase in [(0, "Nothing waiting"), (1, "1 image waiting"),
                          (9, "9 images waiting")]:
            label = review._review_door_label(n)
            self.assertEqual(label.split("\n"), ["Review", phrase])  # title (::first-line), count

    def test_review_door_description_is_the_buttons_own_after_content(self):
        """The description moved out of the label (the count line and it need
        different sizes) into the button's ::after, so it is still inside the
        one <button>."""
        self.assertIn("Approve, fix or reject", review._REVIEW_DOOR_DESC)
        self.assertIn('.door.main::after { content: "%s"; }' % review._REVIEW_DOOR_DESC,
                      review.CUSTOM_CSS)


class BoardCountTest(unittest.TestCase):
    def test_put_away_count_is_singular_for_one(self):
        self.assertIn("<strong>1</strong> image put away", review._board_connected_html(1))
        self.assertIn("<strong>0</strong> images put away", review._board_connected_html(0))
        self.assertIn("<strong>1,234</strong> images put away",
                      review._board_connected_html(1234))


class HomeLayoutCssTest(unittest.TestCase):
    """The Home layout contract lives in CSS; these pin the rules a refactor
    could silently drop."""
    css = review.CUSTOM_CSS

    def test_header_is_a_two_column_grid_that_stacks_under_700(self):
        self.assertRegex(self.css, r"#home-header \{[^}]*display: grid !important;[^}]*"
                                   r"grid-template-columns: 1fr auto;[^}]*align-items: end;")
        self.assertIn("#home-header > * { min-width: 0 !important;", self.css)
        self.assertRegex(self.css, r"@media \(max-width: 699px\) \{\s*#home-header "
                                   r"\{ grid-template-columns: 1fr; \}")

    def test_home_is_wider_and_fills_tall_windows_only(self):
        self.assertIn("#home-page .inner-wrap { width: min(100% - 48px, 1320px);", self.css)
        self.assertIn("@media (min-height: 760px) and (min-width: 700px)", self.css)
        self.assertIn("#home-page .shelf.coming-in { flex: 6 1 0 !important; }", self.css)
        self.assertIn("#home-page .shelf.putting-away { flex: 4 1 0 !important; }", self.css)

    def test_type_scale(self):
        self.assertIn("#home-title h1 { font-size: 48px;", self.css)
        self.assertIn(".door::first-line { font-size: 24px !important;", self.css)
        self.assertIn(".door.main::first-line { font-size: 44px !important; }", self.css)


class HomeNoThumbnailsTest(unittest.TestCase):
    """The Review door no longer previews waiting thumbnails. Home must build
    and read no images, and must never reach into History's thumbnailer."""

    def setUp(self):
        self._prev_for = review.history._thumbnail_for
        self._prev_archive = review.archive_dir

        def _boom(*a, **k):
            raise AssertionError("Home must not build thumbnails")

        review.history._thumbnail_for = _boom
        review.jobs.finish()  # a free lock -> the connected (non-job) branch

    def tearDown(self):
        review.history._thumbnail_for = self._prev_for
        review.archive_dir = self._prev_archive
        review.jobs.finish()

    def test_home_render_has_no_img_and_no_thumbnailing(self):
        # _menu_availability is what repaints Home; it must not call the
        # thumbnailer (the patch above would raise) and no value it sets may
        # carry an <img> -- the only image source, the strip, is gone.
        u = review._menu_availability()
        for val in u.values():
            text = val if isinstance(val, str) else str(_field(val, "value") or "")
            self.assertNotIn("<img", text)


class JobResultTest(unittest.TestCase):
    def test_ingest_backfill_success_many(self):
        for job in ("ingest", "backfill"):
            res = review.job_result(job, "success", {"new_waiting": 14})
            self.assertTrue(res.seal)
            self.assertEqual(res.sentence, "Done. 14 new images are waiting in Review.")
            self.assertEqual(res.next_label, "Go to Review")

    def test_ingest_success_singular(self):
        res = review.job_result("ingest", "success", {"new_waiting": 1})
        self.assertEqual(res.sentence, "Done. 1 new image is waiting in Review.")
        self.assertEqual(res.next_label, "Go to Review")

    def test_ingest_backfill_success_zero_has_no_next(self):
        # Backfill keeps the plain wording; Ingest points at the full-listing fix
        # (the Free up space button is added beside it by _ingest_result_extra).
        res = review.job_result("backfill", "success", {"new_waiting": 0})
        self.assertTrue(res.seal)
        self.assertEqual(res.sentence, "Done. Nothing new to review.")
        self.assertIsNone(res.next_label)

        res = review.job_result("ingest", "success", {"new_waiting": 0})
        self.assertTrue(res.seal)
        self.assertIn("Nothing new to review.", res.sentence)
        self.assertIn("1,000", res.sentence)
        self.assertIsNone(res.next_label)

    def test_counts_use_thousands_separators(self):
        """Every count sentence renders large numbers with thousands separators
        (export, waiting, filed, ready-to-file/flagged, and the Review door)."""
        self.assertIn(
            "7,252 posts are safe to unsave",
            review.job_result("export_unsave", "success", {"safe": 7252}).sentence)
        self.assertIn(
            "1,234 new images are waiting",
            review.job_result("ingest", "success", {"new_waiting": 1234}).sentence)
        self.assertIn(
            "2,500 images were filed",
            review.job_result("file_images", "success", {"filed": 2500}).sentence)
        res = review.job_result("preview_filing", "success",
                                {"moves": 1200, "flagged": 3400})
        self.assertIn("1,200 images are ready to file", res.sentence)
        self.assertIn("3,400 need", res.sentence)
        self.assertEqual(review._waiting_phrase(5000), "5,000 images waiting")

    def test_preview_filing_without_summary_falls_back(self):
        res = review.job_result("preview_filing", "success", {})
        self.assertTrue(res.seal)
        self.assertEqual(res.sentence, "Done. Nothing was moved.")
        self.assertEqual(res.next_label, "File images")
        self.assertIsNone(res.secondary_label)

    def test_preview_filing_ready_nothing_flagged(self):
        res = review.job_result("preview_filing", "success", {"moves": 12, "flagged": 0})
        self.assertEqual(res.sentence, "Done. 12 images are ready to file. Nothing was moved.")
        self.assertEqual((res.next_label, res.secondary_label), ("File images", None))

    def test_preview_filing_ready_and_flagged(self):
        res = review.job_result("preview_filing", "success", {"moves": 12, "flagged": 3})
        self.assertEqual(res.sentence, "Done. 12 images are ready to file; 3 need Resolve first.")
        self.assertEqual((res.next_label, res.secondary_label), ("Resolve", "File images"))

    def test_preview_filing_only_flagged(self):
        res = review.job_result("preview_filing", "success", {"moves": 0, "flagged": 3})
        self.assertEqual(res.sentence, "Nothing can be filed yet: 3 need Resolve first.")
        self.assertEqual((res.next_label, res.secondary_label), ("Resolve", None))

    def test_preview_filing_nothing_approved(self):
        res = review.job_result("preview_filing", "success", {"moves": 0, "flagged": 0})
        self.assertEqual(res.sentence, "Nothing to file. Approve images in Review first.")
        self.assertEqual((res.next_label, res.secondary_label), ("Go to Review", None))

    def test_preview_filing_singulars(self):
        res = review.job_result("preview_filing", "success", {"moves": 1, "flagged": 1})
        self.assertEqual(res.sentence, "Done. 1 image is ready to file; 1 needs Resolve first.")

    def test_file_images_success_plural_and_singular(self):
        res = review.job_result("file_images", "success", {"filed": 7})
        self.assertTrue(res.seal)
        self.assertEqual(res.sentence, "Done. 7 images were filed.")
        self.assertIsNone(res.next_label)  # only Back to menu
        one = review.job_result("file_images", "success", {"filed": 1})
        self.assertEqual(one.sentence, "Done. 1 image was filed.")

    def test_file_images_success_zero(self):
        res = review.job_result("file_images", "success", {"filed": 0})
        self.assertTrue(res.seal)
        self.assertEqual(res.sentence, "Done. Nothing was ready to file.")
        self.assertIsNone(res.next_label)
        # Also when the key is absent entirely.
        self.assertEqual(review.job_result("file_images", "success", {}).sentence,
                         "Done. Nothing was ready to file.")

    def test_cancelled_has_no_seal_and_no_next(self):
        res = review.job_result("backfill", "cancelled", {})
        self.assertFalse(res.seal)
        self.assertTrue(res.sentence.startswith("Stopped."))
        self.assertIn("nothing after the stop point", res.sentence)
        self.assertIsNone(res.next_label)

    def test_failed_for_every_job_has_no_seal_and_names_the_reason(self):
        for job in ("ingest", "backfill", "preview_filing", "file_images"):
            res = review.job_result(job, "failed", {"reason": "Boom failed (exit 1)"})
            self.assertFalse(res.seal)
            self.assertIn("Didn't finish: Boom failed (exit 1)", res.sentence)
            self.assertIn("under Details", res.sentence)
            self.assertIsNone(res.next_label)

    def test_second_line_passes_through_only_on_success(self):
        res = review.job_result("ingest", "success",
                                {"new_waiting": 2, "second_line": "3 were skipped."})
        self.assertEqual(res.second_line, "3 were skipped.")
        # Not invented when absent.
        self.assertIsNone(review.job_result("ingest", "success", {"new_waiting": 2}).second_line)

    def test_unknown_job_raises(self):
        with self.assertRaises(ValueError):
            review.job_result("nonsense", "success", {})


class HomeStateTest(unittest.TestCase):
    """_menu_availability drives the three Home states. (The full component
    contract is in test_render_contract; here we pin the closet specifics:
    which doors go dashed-disabled and what the board says.)"""

    def setUp(self):
        self._prev_archive = review.archive_dir
        review.jobs.finish()  # start from a free lock

    def tearDown(self):
        review.archive_dir = self._prev_archive
        review.jobs.finish()

    def test_archive_unreachable_disables_putting_away_and_warns(self):
        review.archive_dir = Path(self._prev_archive or ".") / "no-such-archive-xyz"
        u = review._menu_availability()
        for btn in (review.menu_archive_dryrun_btn, review.menu_archive_apply_btn,
                    review.menu_resolve_btn):
            self.assertIs(_field(u[btn], "interactive"), False)
            self.assertIn("Needs the archive folder.", _field(u[btn], "value"))
        # Top shelf + tools stay live.
        for btn in (review.menu_ingest_btn, review.menu_backfill_btn,
                    review.menu_review_btn, review.menu_history_btn, review.menu_manage_btn):
            # ingest/backfill may be greyed for their OWN reasons, but never for
            # the archive; review/history/manage are always live here.
            pass
        self.assertIs(_field(u[review.menu_review_btn], "interactive"), True)
        self.assertIn("isn't reachable", u[review.board_html])
        self.assertIs(_field(u[review.archive_check_again_btn], "visible"), True)
        self.assertIs(_field(u[review.job_progress_btn], "visible"), False)

    def test_job_running_locks_everything_and_shows_progress(self):
        self.assertTrue(review.jobs.try_start("Ingest"))
        u = review._menu_availability()
        for btn in review._MENU_BUTTONS:
            self.assertIs(_field(u[btn], "interactive"), False)
        self.assertIn("Ingest is running", u[review.board_html])
        self.assertIs(_field(u[review.job_progress_btn], "visible"), True)
        self.assertIs(_field(u[review.archive_check_again_btn], "visible"), False)

    def test_renamed_job_shows_display_name_on_the_board(self):
        # The lock name is "Archive apply"; the board must show "File images".
        self.assertTrue(review.jobs.try_start("Archive apply"))
        u = review._menu_availability()
        self.assertIn("File images is running", u[review.board_html])
        self.assertNotIn("Archive apply", u[review.board_html])


class JobDisplayNameTest(unittest.TestCase):
    def test_lock_names_map_to_ui_names(self):
        self.assertEqual(review._job_display("Archive dry run"), "Preview filing")
        self.assertEqual(review._job_display("Archive apply"), "File images")
        # Ingest/Backfill unchanged; None passes through.
        self.assertEqual(review._job_display("Ingest"), "Ingest")
        self.assertEqual(review._job_display("Backfill"), "Backfill")
        self.assertIsNone(review._job_display(None))


class LocalFontTest(unittest.TestCase):
    FONTS = REPO / "assets" / "fonts"
    WOFF2 = ["ZenKakuGothicNew-Regular.woff2", "ZenKakuGothicNew-Medium.woff2",
             "ZenKakuGothicNew-Bold.woff2"]

    def test_font_files_and_licence_exist(self):
        for name in self.WOFF2:
            p = self.FONTS / name
            self.assertTrue(p.exists(), f"missing {name}")
            self.assertGreater(p.stat().st_size, 0)
        self.assertTrue((self.FONTS / "OFL.txt").exists())

    def test_fontface_references_each_woff2(self):
        css = review.CUSTOM_CSS
        self.assertEqual(css.count("@font-face"), 3)
        for name in self.WOFF2:
            self.assertIn(name, css)

    def test_no_remote_font_or_url_in_css(self):
        css = review.CUSTOM_CSS
        self.assertNotIn("googleapis", css)
        self.assertNotIn("gstatic", css)
        # No absolute remote URL anywhere in the stylesheet: the only font src is
        # the local /gradio_api/file= route.
        self.assertEqual(re.findall(r"https?://", css), [])

    def test_source_has_no_google_fonts_link(self):
        src = (REPO / "review.py").read_text(encoding="utf-8")
        self.assertNotIn("fonts.googleapis.com", src)
        self.assertNotIn("fonts.gstatic.com", src)


if __name__ == "__main__":
    unittest.main(verbosity=2)
