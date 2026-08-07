"""Regression test: an explicit reviewer DECISION silences the alias prompt.

The character-alias prompt exists to answer ONE question -- "which character
subfolder does this name mean?" -- so it must not fire when the reviewer has
already answered it on this screen. Four controls do that, each an explicit
choice made before pressing Accept:

    Crossover              -> Crossover/                    (precedence 1)
    Original character     -> Others/Artist's Original/      (precedence 7)
    File as Known Series   -> Others/Known Series/           (precedence 6)
    Same-series group      -> <Franchise>/Others_Group/      (precedence 4)

The first three were excluded when the panel was built. Same-series group was
not, so every group shot with an unmatched name -- and an unmatched name on a
group shot is the NORMAL case, not a problem -- cost a click to answer a
question its own checkbox had already settled.

The line is the DECISION, not the destination, and the two are easy to confuse
because they coincide: a multi-name entry reaches the very same group folder
via `len(identities) >= 2` in archive.route_entry, and is deliberately NOT
excluded. Nobody decided that -- it is inferred from the tags, and the tags are
what this prompt corrects. Suppressing there would go quiet exactly when a name
is most likely wrong. That boundary is asserted here (see
test_an_inferred_group_shot_still_prompts) precisely because the destinations
are identical, so nothing else in the tree would notice it being crossed.

Driven through the real `on_accept` / `on_map_prompt_confirm` rather than
`_character_alias_candidate` directly, because the argument being dropped at a
CALL SITE is the whole bug class here: the helper grew the parameter it needed
while one of its two callers kept passing the old set. A test on the helper
alone would have gone on passing.

Runs in a temp cwd against a synthetic manifest/layout, same sandbox pattern as
tests/test_render_contract.py. The real manifest.json, layout.json and
ARCHIVE_DIR are never read or written.

    python -m unittest discover -s tests
    python tests/test_alias_prompt_suppression.py
"""
import copy
import json
import os
import shutil
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

# Safe at module scope, unlike review: archive.py binds nothing to the cwd at
# import (its load_dotenv call is inside main), so it needs no sandbox and no
# sys.modules juggling.
import archive  # noqa: E402

FOLDER = "Starfall Chronicle"
# Not in the roster below, and not an alias -- so it resolves to nothing and the
# prompt WOULD fire, which is what makes the suppressions observable.
UNMATCHED = "Nobody In The Roster"

LAYOUT = {
    "group_subfolder": "Others_Group",
    "special_folders": {
        "crossover": "Crossover", "wallpaper_root": "Wallpaper",
        "wallpaper_pc": "PC", "wallpaper_phone": "Phone",
        "others_oc": "Others/Artist's Original",
        "others_unknown_source": "Others/Unknown Sauce",
        "others_known_series": "Others/Known Series",
    },
    "shortname_file": "known_series_names.txt",
    "franchise_aliases": {},
    "character_aliases": {},
    "franchises": {FOLDER: {"style": "nested", "characters": ["Sera", "Kestrel"]}},
}


def _build_sandbox(tmp: Path) -> None:
    shutil.copy(REPO / "known_series_names.example.txt", tmp / "known_series_names.txt")
    # tagger.subreddit_is_mapped exits the process if this is absent. "TestSub"
    # is deliberately NOT in the example map, so every accept goes through the
    # subreddit panel first -- which is the chained path the second call site
    # lives on.
    shutil.copy(REPO / "subreddit_map.example.json", tmp / "subreddit_map.json")
    (tmp / "layout.json").write_text(json.dumps(LAYOUT, indent=2), encoding="utf-8")

    staging = tmp / "staging"
    staging.mkdir()
    from PIL import Image

    entries = {}
    for n in range(1, 21):
        path = staging / f"t3_s{n:02d}.jpg"
        Image.new("RGB", (900, 600), (30, 90, 130)).save(path)
        entries[f"t3_s{n:02d}"] = {
            "post_id": f"t3_s{n:02d}",
            "title": f"Group shot {n}",
            "subreddit": "TestSub",
            "permalink": f"https://reddit.com/r/TestSub/comments/s{n:02d}/",
            "image_url": f"https://i.redd.it/s{n:02d}.jpg",
            "local_path": str(path).replace("\\", "/"),
            "fetched_at": f"2026-01-01T00:00:{n:02d}+00:00",
            "status": "pending_review",
            "franchise": [FOLDER],
            "character_guess": [UNMATCHED],
            "guess_confidence": "medium",
            "guess_source": "title",
            "crossover": False,
        }
    (tmp / "manifest.json").write_text(json.dumps(entries, indent=2), encoding="utf-8")


class AliasPromptSuppressionTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.mkdtemp(prefix="oshiire-suppress-")
        cls._prev_cwd = os.getcwd()
        _build_sandbox(Path(cls._tmp))
        os.chdir(cls._tmp)
        cls._prev_archive = os.environ.pop("ARCHIVE_DIR", None)
        # review.py binds its manifest, layout and queue at IMPORT time, from
        # whatever cwd it was first imported in -- so the module is really a
        # handle on one sandbox. tests/test_render_contract.py imports it too,
        # and discovery runs this file first; leaving our copy in sys.modules
        # hands that suite THIS sandbox's manifest and every one of its entries
        # goes missing. Take the slot, then give it back.
        cls._prev_module = sys.modules.pop("review", None)
        with redirect_stdout(StringIO()):
            import review
        cls.review = review

    @classmethod
    def tearDownClass(cls):
        os.chdir(cls._prev_cwd)
        if cls._prev_archive is not None:
            os.environ["ARCHIVE_DIR"] = cls._prev_archive
        if cls._prev_module is not None:
            sys.modules["review"] = cls._prev_module
        else:
            sys.modules.pop("review", None)
        shutil.rmtree(cls._tmp, ignore_errors=True)

    def setUp(self):
        """Rewind to a clean queue position, and snapshot layout.json -- an
        accept that reaches the panel could otherwise write an answer that
        suppresses the NEXT test's prompt for the same name."""
        review = self.review
        review.current_index = 0
        review.pending_accept = None
        review.last_action = None
        self._layout_before = copy.deepcopy(review.layout)
        for entry in review.manifest.values():
            entry["status"] = "pending_review"
            entry.pop("archive_override", None)
        review.queue = sorted(review.manifest)

    def tearDown(self):
        review = self.review
        review.pending_accept = None
        review.layout.clear()
        review.layout.update(self._layout_before)

    def _accept(self, characters=UNMATCHED, **flags):
        """Accept the current entry with `flags` ticked, answering the subreddit
        panel with "don't remember". Returns (panel_opened, final_status)."""
        review = self.review
        post_id = review.queue[review.current_index]
        with redirect_stdout(StringIO()):
            review.on_accept(
                characters, FOLDER,
                flags.get("crossover", False),
                flags.get("same_series_group", False),
                "none",
                flags.get("known_series", False),
                flags.get("is_oc", False),
            )
            if (review.pending_accept or {}).get("stage") == "subreddit":
                review.on_map_prompt_confirm("once")
            opened = (review.pending_accept or {}).get("stage") == "character"
            if opened:
                # Clear it so the queue advances and the next case starts clean.
                review.on_character_alias_skip()
        return opened, review.manifest[post_id]["status"]

    def test_an_unmatched_name_with_nothing_ticked_still_prompts(self):
        """The control. Every suppression below is only meaningful because this
        case DOES open the panel -- without it the test could pass by the
        prompt being broken outright."""
        opened, status = self._accept()
        self.assertTrue(opened, "the alias prompt no longer fires at all")
        self.assertEqual(status, "approved")

    def test_an_inferred_group_shot_still_prompts(self):
        """THE BOUNDARY. Two tagged names route to exactly the same place the
        same-series-group checkbox does -- and must still be asked about.

        The exclusions are about an explicit reviewer DECISION, not about where
        the image lands. Nobody decided this one: the group routing is inferred
        from the tags, and the tags are what the prompt exists to correct. A
        multi-name entry is where a name is most likely to be wrong, so it is
        the last place to go quiet.

        Asserted alongside the destination it shares, because the tempting
        wrong move is to exclude it "for consistency" with same_series_group --
        and the destinations really are identical, so nothing else in the tree
        would object."""
        opened, status = self._accept(f"{UNMATCHED}\nAlso Not In The Roster")
        self.assertTrue(opened, "an inferred group shot stopped prompting")
        self.assertEqual(status, "approved")

        # ...and the shared destination that makes this look excludable.
        two_names = {
            "franchise": [FOLDER], "crossover": False,
            "character_guess": [UNMATCHED, "Also Not In The Roster"],
        }
        ticked = {
            "franchise": [FOLDER], "crossover": False,
            "character_guess": [UNMATCHED], "same_series_group": True,
        }
        inferred = archive.route_entry(two_names, self.review.layout, [], None)
        decided = archive.route_entry(ticked, self.review.layout, [], None)
        self.assertEqual(inferred.dest_dir, f"{FOLDER}/Others_Group")
        self.assertEqual(inferred.dest_dir, decided.dest_dir,
                         "premise broken: these no longer share a destination")

    def test_the_helper_does_not_exclude_a_multi_name_entry(self):
        """The same boundary one level down, where the guard actually lives."""
        candidate = self.review._character_alias_candidate
        self.assertIsNotNone(
            candidate([FOLDER], [UNMATCHED, "Also Not In The Roster"]),
            "a multi-name entry is being excluded like a ticked checkbox",
        )
        # A name count is not a decision; the checkbox is.
        self.assertIsNone(
            candidate([FOLDER], [UNMATCHED, "Also Not In The Roster"],
                      same_series_group=True),
        )

    def test_each_routing_decision_suppresses_the_prompt(self):
        for flag in ("same_series_group", "crossover", "is_oc", "known_series"):
            with self.subTest(ticked=flag):
                self.setUp()
                opened, status = self._accept(**{flag: True})
                self.assertFalse(opened, f"{flag} ticked but the alias panel still opened")
                self.assertEqual(status, "approved",
                                 f"{flag} ticked: accept did not go straight through")

    def test_same_series_group_suppresses_it_on_both_call_sites(self):
        """The helper takes the flag; both callers must pass it. The chained
        path (subreddit panel answered first) and the direct path (no subreddit
        question) are separate call sites and have drifted before -- a
        multi-franchise entry takes the direct one."""
        review = self.review

        # Chained: TestSub is unmapped and there is one franchise, so on_accept
        # opens the subreddit panel and on_map_prompt_confirm asks the question.
        with redirect_stdout(StringIO()):
            review.on_accept(UNMATCHED, FOLDER, False, True, "none", False, False)
        self.assertEqual((review.pending_accept or {}).get("stage"), "subreddit",
                         "expected the chained path; the sandbox subreddit is mapped?")
        with redirect_stdout(StringIO()):
            review.on_map_prompt_confirm("once")
        self.assertIsNone(review.pending_accept, "chained path: panel opened anyway")

        # Direct: two franchises fails `eligible`, so on_accept asks directly.
        self.setUp()
        with redirect_stdout(StringIO()):
            review.on_accept(UNMATCHED, f"{FOLDER}\nNeon Ward", False, True, "none", False, False)
        self.assertIsNone(review.pending_accept, "direct path: panel opened anyway")

    def test_the_helper_itself_rejects_each_flag(self):
        """The same matrix one level down, so a failure says WHICH layer broke."""
        candidate = self.review._character_alias_candidate
        self.assertIsNotNone(candidate([FOLDER], [UNMATCHED]), "control: no candidate")
        for flag in ("same_series_group", "crossover", "is_oc", "known_series"):
            with self.subTest(flag=flag):
                self.assertIsNone(candidate([FOLDER], [UNMATCHED], **{flag: True}))

    def test_a_matching_character_is_unaffected_by_any_of_them(self):
        """Suppression is about not ASKING; it must not change what is stored.
        A name that already resolves never prompted anyway."""
        review = self.review
        for flag in (None, "same_series_group", "crossover"):
            with self.subTest(ticked=flag):
                self.setUp()
                post_id = review.queue[review.current_index]
                flags = {flag: True} if flag else {}
                with redirect_stdout(StringIO()):
                    review.on_accept(
                        "Sera", FOLDER, flags.get("crossover", False),
                        flags.get("same_series_group", False), "none", False, False,
                    )
                    if (review.pending_accept or {}).get("stage") == "subreddit":
                        review.on_map_prompt_confirm("once")
                self.assertIsNone(review.pending_accept)
                entry = review.manifest[post_id]
                self.assertEqual(entry["status"], "approved")
                self.assertEqual(entry["character_guess"], ["Sera"])
                self.assertEqual(entry["same_series_group"], flag == "same_series_group")
                self.assertEqual(entry["crossover"], flag == "crossover")

    def test_suppression_writes_nothing_to_layout_json(self):
        """A silenced prompt is not a persistent answer -- it must leave no
        record, or a later untick would find the name already decided."""
        review = self.review
        before = Path("layout.json").read_bytes()
        for flag in ("same_series_group", "crossover", "is_oc", "known_series"):
            with self.subTest(ticked=flag):
                self.setUp()
                self._accept(**{flag: True})
                self.assertEqual(Path("layout.json").read_bytes(), before)
                self.assertNotIn("character_group_route", review.layout)
                self.assertNotIn("character_alias_dismissed", review.layout)


if __name__ == "__main__":
    unittest.main(verbosity=2)
