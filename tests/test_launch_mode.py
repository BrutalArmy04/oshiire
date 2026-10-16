"""Tests for the launch-mode UI seams in review.py: the Quit handler, the
Quit button's visibility by mode, and the App settings panel's save discipline.

These import review in-process (its handlers are pure functions over the
manifest/jobs/settings seams) and never start a server or a window.

    python -m unittest discover -s tests
    python tests/test_launch_mode.py
"""
import sys
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

import review


def _field(update, field):
    """Read a field from a gr.update value (dict or object across versions)."""
    if isinstance(update, dict):
        return update.get(field)
    return getattr(update, field, None)


class QuitHandlerTest(unittest.TestCase):
    def tearDown(self):
        review.set_launch_mode("browser")

    def test_quit_refused_while_a_job_runs(self):
        with mock.patch.object(review.jobs, "running", return_value="Ingest"), \
             mock.patch.object(review, "_schedule_server_shutdown") as shutdown:
            updates = review._quit()
        shutdown.assert_not_called()
        notice = _field(updates[review.quit_notice], "value")
        self.assertIn("job", notice.lower())

    def test_quit_shuts_down_when_idle(self):
        with mock.patch.object(review.jobs, "running", return_value=None), \
             mock.patch.object(review, "_schedule_server_shutdown") as shutdown:
            updates = review._quit()
        shutdown.assert_called_once()
        notice = _field(updates[review.quit_notice], "value")
        self.assertIn("stopped", notice.lower())


class QuitButtonVisibilityTest(unittest.TestCase):
    def tearDown(self):
        review.set_launch_mode("browser")

    def test_hidden_in_native_mode(self):
        review.set_launch_mode("native")
        update = review._quit_btn_update(None)
        self.assertIn("page-hidden", _field(update, "elem_classes"))

    def test_shown_and_enabled_in_browser_mode_when_idle(self):
        review.set_launch_mode("browser")
        update = review._quit_btn_update(None)
        self.assertNotIn("page-hidden", _field(update, "elem_classes"))
        self.assertTrue(_field(update, "interactive"))

    def test_disabled_in_browser_mode_while_a_job_runs(self):
        review.set_launch_mode("browser")
        update = review._quit_btn_update("Ingest")
        self.assertNotIn("page-hidden", _field(update, "elem_classes"))
        self.assertFalse(_field(update, "interactive"))


class AppPanelSaveTest(unittest.TestCase):
    def test_save_disabled_when_mode_unchanged(self):
        with mock.patch.object(review.settings, "window_mode", return_value="browser"):
            update = review._app_mode_changed("browser")
        self.assertFalse(_field(update, "interactive"))

    def test_save_enabled_when_mode_changed(self):
        with mock.patch.object(review.settings, "window_mode", return_value="browser"):
            update = review._app_mode_changed("native")
        self.assertTrue(_field(update, "interactive"))

    def test_save_persists_mode_and_disables_save(self):
        with mock.patch.object(review.settings, "save_window_mode") as save:
            save_update, status = review._app_save("native")
        save.assert_called_once_with("native")
        self.assertFalse(_field(save_update, "interactive"))
        self.assertIn("saved", status.lower())

    def test_render_shows_saved_values_with_saves_disabled(self):
        """Opening the panel (tab select / Manage open) re-reads the file, so both
        radios reflect what's on disk and both Saves start disabled."""
        with mock.patch.object(review.settings, "window_mode", return_value="native"), \
             mock.patch.object(review.settings, "theme", return_value="dark"):
            out = review._app_render()
        self.assertEqual(_field(out[review.app_window_radio], "value"), "native")
        self.assertFalse(_field(out[review.app_save_btn], "interactive"))
        self.assertEqual(_field(out[review.app_theme_radio], "value"), "dark")
        self.assertFalse(_field(out[review.app_theme_save_btn], "interactive"))

    def test_theme_save_persists_and_disables(self):
        with mock.patch.object(review.settings, "save_theme") as save:
            upd, status = review._app_theme_save("dark")
        save.assert_called_once_with("dark")
        self.assertFalse(_field(upd, "interactive"))
        self.assertIn("saved", status.lower())

    def test_theme_changed_enables_only_on_change(self):
        with mock.patch.object(review.settings, "theme", return_value="system"):
            self.assertFalse(_field(review._app_theme_changed("system"), "interactive"))
            self.assertTrue(_field(review._app_theme_changed("dark"), "interactive"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
