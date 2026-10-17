"""Settings picker behaviour (A1 forgiving new-entry + A2 label settling).

The five panels (subreddit / franchise / character / series / shortnames) share
one picker shape. A typed value that is NOT an existing entry starts a new entry
with that name; a typed value that matches an existing one under the panel's
identity rule selects that entry instead.
"""
import sys
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))

from review_sandbox import ReviewSandbox  # noqa: E402

# review binds its manifest/layout/archive dir at import, and resolve exits when
# there's no layout.json: import a private copy inside a throwaway data root,
# and run this module's tests inside it too.
_sandbox = ReviewSandbox()
review = _sandbox.load()
setUpModule = _sandbox.activate
tearDownModule = _sandbox.close


def _field(update, field):
    if isinstance(update, dict):
        return update.get(field)
    return getattr(update, field, None)


class ResolvePickTest(unittest.TestCase):
    """The shared resolver, exercised with each panel's identity rule."""

    def test_empty_is_empty(self):
        self.assertEqual(review._resolve_pick("", ["zzz"], review._sub_same),
                         ("empty", None))

    def test_subreddit_case_variant_matches(self):
        self.assertEqual(review._resolve_pick("ZZZ", ["zzz"], review._sub_same),
                         ("match", "zzz"))

    def test_series_name_variant_matches(self):
        from shortname import same_series_name
        self.assertEqual(
            review._resolve_pick("RE:ZERO", ["Re:Zero"], same_series_name),
            ("match", "Re:Zero"))

    def test_character_name_variant_matches(self):
        self.assertEqual(review._resolve_pick("hu-tao", ["Hu Tao"], review._name_same),
                         ("match", "Hu Tao"))

    def test_brand_new_value_is_new(self):
        self.assertEqual(review._resolve_pick("Newbie", ["zzz"], review._sub_same),
                         ("new", "Newbie"))


class SubredditPickerTest(unittest.TestCase):
    def test_new_entry(self):
        with mock.patch.object(review, "_settings_raw", return_value={"zzz": {"franchise": "Z"}}):
            out = review.on_settings_select("Newbie")
        self.assertIn("isn’t in the list yet", _field(out[review.settings_status_md], "value") or out[review.settings_status_md])
        self.assertEqual(out[review.settings_key_box], "Newbie")
        self.assertEqual(_field(out[review.settings_save_btn], "value"), "Save new subreddit entry")
        self.assertFalse(_field(out[review.settings_delete_btn], "interactive"))

    def test_matches_existing_case_variant(self):
        with mock.patch.object(review, "_settings_raw", return_value={"zzz": {"franchise": "Z"}}):
            out = review.on_settings_select("ZZZ")
        self.assertEqual(_field(out[review.settings_dropdown], "value"), "zzz")


class FranchiseAliasPickerTest(unittest.TestCase):
    def test_new_entry(self):
        with mock.patch.object(review, "_fa_table", return_value={"Zed": "Zed Folder"}):
            out = review.on_fa_select("Newbie")
        self.assertIn("isn’t in the list yet", out[review.fa_status_md])
        self.assertEqual(out[review.fa_key_box], "Newbie")
        self.assertEqual(_field(out[review.fa_save_btn], "value"), "Save new franchise alias")

    def test_matches_existing_case_variant(self):
        with mock.patch.object(review, "_fa_table", return_value={"Zed": "Zed Folder"}):
            out = review.on_fa_select("zed")
        self.assertEqual(_field(out[review.fa_dropdown], "value"), "Zed")


class SeriesAliasPickerTest(unittest.TestCase):
    def test_new_entry(self):
        with mock.patch.object(review, "load_series_aliases", return_value={"Zed": "Zed Full"}):
            out = review.on_sa_select("Newbie")
        self.assertIn("isn’t in the list yet", out[review.sa_status_md])
        self.assertEqual(_field(out[review.sa_save_btn], "value"), "Save new series alias")

    def test_matches_existing_case_variant(self):
        with mock.patch.object(review, "load_series_aliases", return_value={"Zed": "Zed Full"}):
            out = review.on_sa_select("zed")
        self.assertEqual(_field(out[review.sa_dropdown], "value"), "Zed")


class CharacterAliasPickerTest(unittest.TestCase):
    def test_new_entry(self):
        with mock.patch.object(review, "_nested_franchises", return_value=["ZedSeries"]), \
             mock.patch.object(review, "_ca_aliases", return_value={"zed": "Zed Folder"}), \
             mock.patch.object(review, "_ca_impact", return_value=""):
            out = review.on_ca_variant_select("ZedSeries", "Newbie")
        self.assertIn("isn’t in the list yet", out[review.ca_status_md])
        self.assertEqual(out[review.ca_key_box], "Newbie")
        self.assertEqual(_field(out[review.ca_save_btn], "value"), "Save new character alias")

    def test_matches_existing_variant(self):
        with mock.patch.object(review, "_nested_franchises", return_value=["ZedSeries"]), \
             mock.patch.object(review, "_ca_aliases", return_value={"zed": "Zed Folder"}), \
             mock.patch.object(review, "_ca_impact", return_value=""):
            out = review.on_ca_variant_select("ZedSeries", "ZED")
        self.assertEqual(_field(out[review.ca_variant_dropdown], "value"), "zed")


class ShortnamePickerTest(unittest.TestCase):
    def test_new_entry(self):
        with mock.patch.object(review, "_sn_entries", return_value=[("ZD", "Zed Series")]):
            out = review.on_sn_select("Newbie")
        self.assertIn("isn’t in the list yet", out[review.sn_status_md])
        self.assertEqual(out[review.sn_fullname_box], "Newbie")
        self.assertEqual(_field(out[review.sn_save_btn], "value"), "Save new shortname")

    def test_matches_existing_variant(self):
        with mock.patch.object(review, "_sn_entries", return_value=[("ZD", "Zed Series")]):
            out = review.on_sn_select("ZED SERIES")
        self.assertEqual(_field(out[review.sn_dropdown], "value"), "Zed Series")


class LabelSettlesOnFinalTextTest(unittest.TestCase):
    """A2: the relabel handler, given the FINAL box text, must produce the label
    the button ends with -- no lag behind the last keystroke."""

    def test_subreddit_rename_label_is_final(self):
        with mock.patch.object(review, "_settings_raw", return_value={"zzz": {"franchise": "Z"}}):
            save, _delete = review.on_settings_key_input("zz test", "zzz")
        self.assertEqual(_field(save, "value"), "Rename “zzz” → “zz test”")

    def test_franchise_rename_label_is_final(self):
        with mock.patch.object(review, "_fa_table", return_value={"Zed": "Zed Folder"}):
            save, _delete = review.on_fa_key_input("Zed Two", "Zed")
        self.assertEqual(_field(save, "value"), "Rename “Zed” → “Zed Two”")


if __name__ == "__main__":
    unittest.main(verbosity=2)
