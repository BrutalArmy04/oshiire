"""Regression test: review.py's repaint must cover EVERY registered output.

`_render_current` used to return a 27-element positional tuple, and two callers
patched it by hardcoded index (base[5], base[17], ...). Adding a control to the
UI shifted those indices silently -- it did not raise, it wrote correct values
into the wrong widgets. The repaint is now a {component: value} dict, which
removes the index coupling but introduces two new ways to be wrong that this
test pins down:

  1. A key that is not a component. The dict is keyed by the module-level
     component globals, so a LOCAL variable of the same name inside
     _render_current shadows one and the key silently becomes a plain string.
     Gradio then treats the whole dict as an ordinary return value. Three such
     locals existed (title_md, meta_md, dup_group) and were renamed; nothing
     stops a fourth being introduced.

  2. A missing key. Gradio fills an omitted component with skip(), so a partial
     repaint is silently accepted -- and a partial repaint is exactly what once
     let typed field values revert. Both branches must be complete.

Runs entirely in a temp cwd against a synthetic manifest/layout. The real
manifest.json, layout.json and ARCHIVE_DIR are never read or written.

    python -m unittest discover -s tests
    python tests/test_render_contract.py
"""
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

EXPECTED_OUTPUT_COUNT = 27


def _build_sandbox(tmp: Path) -> None:
    """A minimal but REAL project tree: review.py reads all of this at import."""
    shutil.copy(REPO / "layout.example.json", tmp / "layout.json")
    shutil.copy(REPO / "known_series_names.example.txt", tmp / "known_series_names.txt")
    # tagger.subreddit_is_mapped exits the process if this is absent, and
    # on_accept consults it. "TestSub" is deliberately not in the example map,
    # so the accept path reaches the confirm panel.
    shutil.copy(REPO / "subreddit_map.example.json", tmp / "subreddit_map.json")

    staging = tmp / "staging"
    staging.mkdir()
    image_path = staging / "t3_test01.jpg"
    from PIL import Image
    Image.new("RGB", (1920, 1080), (90, 110, 130)).save(image_path)

    manifest = {
        "t3_test01": {
            "post_id": "t3_test01",
            "title": "Test Character (@artist)",
            "subreddit": "TestSub",
            "permalink": "https://reddit.com/r/TestSub/comments/test01/",
            "image_url": "https://i.redd.it/test01.jpg",
            "local_path": str(image_path).replace("\\", "/"),
            "fetched_at": "2026-01-01T00:00:00+00:00",
            "status": "pending_review",
            "franchise": ["Genshin Impact"],
            "character_guess": ["Test Character"],
            "guess_confidence": "medium",
            "guess_source": "title",
            "crossover": False,
        }
    }
    (tmp / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")


class RenderContractTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.mkdtemp(prefix="oshiire-render-")
        cls._prev_cwd = os.getcwd()
        tmp = Path(cls._tmp)
        _build_sandbox(tmp)
        os.chdir(tmp)
        # ARCHIVE_DIR is read softly by review.py; unset it so the sandbox can
        # never reach the user's real archive.
        cls._prev_archive = os.environ.pop("ARCHIVE_DIR", None)
        # Importing review.py runs its whole module body (manifest load, hash
        # warm-up, gr.Blocks construction). Noisy but side-effect-free here.
        with redirect_stdout(StringIO()):
            import review
        cls.review = review

    @classmethod
    def tearDownClass(cls):
        os.chdir(cls._prev_cwd)
        if cls._prev_archive is not None:
            os.environ["ARCHIVE_DIR"] = cls._prev_archive
        shutil.rmtree(cls._tmp, ignore_errors=True)

    def _render(self, empty_queue: bool):
        review = self.review
        prev = review.current_index
        try:
            # _current_entry() returns None once the index runs past the queue,
            # which is the empty-queue branch.
            review.current_index = len(review.queue) if empty_queue else 0
            with redirect_stdout(StringIO()):
                return review._render_current()
        finally:
            review.current_index = prev

    def test_outputs_list_is_the_expected_size_and_has_no_duplicates(self):
        outputs = self.review.outputs
        self.assertEqual(len(outputs), EXPECTED_OUTPUT_COUNT)
        self.assertEqual(len({id(c) for c in outputs}), EXPECTED_OUTPUT_COUNT,
                         "a component is registered in `outputs` twice")

    def test_both_branches_cover_every_output_component(self):
        outputs = self.review.outputs
        for empty_queue in (False, True):
            branch = "empty-queue" if empty_queue else "normal"
            with self.subTest(branch=branch):
                render = self._render(empty_queue)
                self.assertIsInstance(render, dict, f"{branch}: not a dict")
                self.assertEqual(
                    len(render), EXPECTED_OUTPUT_COUNT,
                    f"{branch}: expected {EXPECTED_OUTPUT_COUNT} keys, got {len(render)}",
                )
                missing = [c for c in outputs if c not in render]
                extra = [k for k in render if k not in outputs]
                self.assertFalse(missing, f"{branch}: components missing from the repaint: {missing}")
                self.assertFalse(extra, f"{branch}: keys not registered in `outputs`: {extra}")
                self.assertEqual(set(map(id, render)), set(map(id, outputs)))

    def test_every_key_is_a_component_not_a_shadowed_local(self):
        """The shadowing trap: a local named after a component turns its key
        into a str, and Gradio silently stops treating the dict as updates."""
        from gradio.blocks import Block
        for empty_queue in (False, True):
            branch = "empty-queue" if empty_queue else "normal"
            with self.subTest(branch=branch):
                render = self._render(empty_queue)
                non_components = [(k, type(k).__name__) for k in render if not isinstance(k, Block)]
                self.assertFalse(non_components, f"{branch}: non-component keys: {non_components}")

    def test_gradio_maps_the_dict_onto_outputs_order(self):
        """The contract this refactor rests on, asserted against the INSTALLED
        gradio rather than assumed: a component-keyed dict is reordered into
        `outputs` order, and nothing is left as skip()."""
        from gradio.blocks import convert_component_dict_to_list
        from gradio.helpers import skip
        outputs = self.review.outputs
        skip_marker = skip()
        for empty_queue in (False, True):
            branch = "empty-queue" if empty_queue else "normal"
            with self.subTest(branch=branch):
                render = self._render(empty_queue)
                as_list = convert_component_dict_to_list([c._id for c in outputs], dict(render))
                self.assertIsInstance(as_list, list)
                self.assertEqual(len(as_list), EXPECTED_OUTPUT_COUNT)
                skipped = [outputs[i] for i, v in enumerate(as_list) if v == skip_marker]
                self.assertFalse(skipped, f"{branch}: left as skip(): {skipped}")
                # order really is outputs order
                for i, component in enumerate(outputs):
                    self.assertEqual(as_list[i], render[component],
                                     f"{branch}: slot {i} ({component}) mismatched")

    def test_character_alias_panel_repaint_is_still_complete(self):
        """The panel repaint overrides by component name; it must not drop or
        invent keys, and the values it sets must be the ones it was given."""
        review = self.review
        with redirect_stdout(StringIO()):
            render = review._character_alias_panel(
                ["Typed Character"], ["Typed Franchise"], True, True, "pc", True, False,
            )
        self.assertEqual(len(render), EXPECTED_OUTPUT_COUNT)
        self.assertEqual(set(map(id, render)), set(map(id, review.outputs)))
        self.assertEqual(render[review.character_box], "Typed Character")
        self.assertEqual(render[review.franchise_box], "Typed Franchise")
        self.assertIs(render[review.crossover_box], True)
        self.assertIs(render[review.same_series_group_box], True)
        self.assertEqual(render[review.wallpaper_box], "pc")
        self.assertIs(render[review.known_series_box], True)
        self.assertIs(render[review.oc_box], False)

    def _restore_state(self):
        """Snapshot/restore review.py's module globals AND the sandbox files, so
        a mutating handler (Reject deletes the staging image) can't leak into
        the next test."""
        review = self.review
        import copy
        state = (
            copy.deepcopy(review.manifest), list(review.queue), review.current_index,
            copy.deepcopy(review.last_action), review.pending_accept,
        )
        image_path = Path(review.manifest["t3_test01"]["local_path"])
        image_bytes = image_path.read_bytes() if image_path.exists() else None

        def restore():
            (review.manifest, review.queue, review.current_index,
             review.last_action, review.pending_accept) = state
            if image_bytes is not None:
                image_path.write_bytes(image_bytes)
        return restore

    def test_every_handler_returns_a_complete_repaint(self):
        """The user-facing checklist as assertions: each button's handler must
        still return a full, correctly-keyed repaint. Runs against the sandbox
        manifest -- Reject really does delete its staging file here."""
        review = self.review
        outputs = review.outputs
        handlers = [
            ("Skip", lambda: review.on_skip()),
            ("Reject", lambda: review.on_reject()),
            ("Undo (after skip)", lambda: (review.on_skip(), review.on_undo())[1]),
            ("Undo (after reject)", lambda: (review.on_reject(), review.on_undo())[1]),
            ("Accept -> map panel", lambda: review.on_accept(
                "C", "F", False, False, "none", False, False)),
            ("map prompt confirm", lambda: (
                review.on_accept("C", "F", False, False, "none", False, False),
                review.on_map_prompt_confirm("once"))[1]),
            ("character alias skip", lambda: (
                review.on_accept("C", "F", False, False, "none", False, False),
                review.on_map_prompt_confirm("once"),
                review.on_character_alias_skip())[2]),
            ("empty queue", lambda: (review.on_skip(), review.on_skip())[1]),
        ]
        for label, call in handlers:
            with self.subTest(handler=label):
                restore = self._restore_state()
                try:
                    with redirect_stdout(StringIO()):
                        render = call()
                    self.assertIsInstance(render, dict, f"{label}: not a dict")
                    self.assertEqual(len(render), EXPECTED_OUTPUT_COUNT,
                                     f"{label}: expected {EXPECTED_OUTPUT_COUNT} keys, got {len(render)}")
                    self.assertEqual(set(map(id, render)), set(map(id, outputs)),
                                     f"{label}: key set does not match `outputs`")
                finally:
                    restore()

    def test_duplicate_banner_failure_still_repaints_completely(self):
        """The banner is advisory and its exception path builds its own values.
        That path must still yield a complete repaint -- it is the one branch
        that used to hand-assemble part of the output tuple."""
        review = self.review
        restore = self._restore_state()
        original = review._duplicate_banner
        try:
            review._duplicate_banner = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom"))
            review.current_index = 0
            with redirect_stdout(StringIO()):
                render = review._render_current()
            self.assertEqual(len(render), EXPECTED_OUTPUT_COUNT)
            self.assertEqual(set(map(id, render)), set(map(id, review.outputs)))
        finally:
            review._duplicate_banner = original
            restore()

    def test_accept_repaint_preserves_typed_values_and_opens_the_panel(self):
        """The other former index-patching site, driven through the real
        on_accept: an unmapped subreddit + single franchise opens the confirm
        panel, and the typed values must survive the repaint."""
        review = self.review
        prev_pending, prev_index = review.pending_accept, review.current_index
        try:
            review.current_index = 0
            with redirect_stdout(StringIO()):
                render = review.on_accept(
                    "Typed Character", "Typed Franchise", False, False, "none", False, False,
                )
            self.assertEqual(len(render), EXPECTED_OUTPUT_COUNT)
            self.assertEqual(set(map(id, render)), set(map(id, review.outputs)))
            self.assertEqual(render[review.character_box], "Typed Character")
            self.assertEqual(render[review.franchise_box], "Typed Franchise")
            self.assertIn("isn't mapped yet", render[review.map_prompt_md])
        finally:
            review.pending_accept, review.current_index = prev_pending, prev_index


if __name__ == "__main__":
    unittest.main(verbosity=2)
