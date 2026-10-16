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
import ast
import json
import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from unittest import mock
from contextlib import contextmanager, redirect_stdout
from io import StringIO
from pathlib import Path

import gradio as gr

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
        # warm-up, gr.Blocks construction) against THIS sandbox. Another test
        # file may have imported review first (a cached import is a no-op, so the
        # sandbox fixture would never load), so reload it here: that re-runs the
        # body in this cwd and makes the fixture present regardless of test
        # order. Noisy but side-effect-free here.
        import importlib
        with redirect_stdout(StringIO()):
            import review
            importlib.reload(review)
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

    def test_parse_lines_treats_none_as_empty(self):
        """Gradio can pass a Textbox value through as None; _parse_lines must
        return [] rather than raising on None.splitlines()."""
        self.assertEqual(self.review._parse_lines(None), [])

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

    # ---- archive_dir_problem() + the Home-render archive gate -------------- #
    @staticmethod
    def _field(update, field):
        """Read a field from a gr.update value (dict or object across versions)."""
        if isinstance(update, dict):
            return update.get(field)
        return getattr(update, field, None)

    def test_archive_dir_problem_unset(self):
        review = self.review
        prev = review.archive_dir
        try:
            review.archive_dir = None
            kind, sentence = review.archive_dir_problem()
            self.assertEqual(kind, "unset")
            self.assertIn("ARCHIVE_DIR isn't set in .env", sentence)
            self.assertIn("Preview filing, File images and Resolve are off", sentence)
        finally:
            review.archive_dir = prev

    def test_archive_dir_problem_missing(self):
        review = self.review
        prev = review.archive_dir
        try:
            missing = Path(self._tmp) / "no-such-archive"
            review.archive_dir = missing
            kind, sentence = review.archive_dir_problem()
            self.assertEqual(kind, "unreachable")
            self.assertIn("isn't reachable", sentence)
            self.assertIn(str(missing), sentence)
        finally:
            review.archive_dir = prev

    def test_archive_dir_problem_oserror(self):
        """A synced/removable drive can make .exists() raise rather than return
        False; that must read as 'not reachable', not crash."""
        review = self.review
        prev = review.archive_dir

        class _Boom:
            def exists(self):
                raise OSError("drive not ready")

            def __str__(self):
                return "Z:/archive"

        try:
            review.archive_dir = _Boom()
            kind, sentence = review.archive_dir_problem()
            self.assertEqual(kind, "unreachable")
            self.assertIn("isn't reachable", sentence)
            self.assertIn("Z:/archive", sentence)
        finally:
            review.archive_dir = prev

    def test_archive_dir_problem_ok(self):
        review = self.review
        prev = review.archive_dir
        try:
            review.archive_dir = Path(self._tmp)  # exists
            self.assertEqual(review.archive_dir_problem(), (None, None))
        finally:
            review.archive_dir = prev

    def test_home_render_gates_archive_buttons_and_shows_banner(self):
        review = self.review
        prev = review.archive_dir
        # Resolve + both Archive pages gate on the archive. Manage does NOT: it
        # stays clickable and gates its own archive-reading Sync tab internally,
        # alongside Review and History which never needed the archive.
        archive_btns = [review.menu_resolve_btn, review.menu_archive_dryrun_btn,
                        review.menu_archive_apply_btn]
        always = [review.menu_review_btn, review.menu_history_btn,
                  review.menu_manage_btn]
        try:
            # Unreachable: the Putting-away doors are disabled with the
            # "Needs the archive folder." second line, the board shows the
            # warning + the Check again button, Review/History/Manage stay on.
            review.archive_dir = Path(self._tmp) / "no-such-archive"
            updates = review._menu_availability()
            for btn in archive_btns:
                self.assertIs(self._field(updates[btn], "interactive"), False)
                self.assertIn("Needs the archive folder.", self._field(updates[btn], "value"))
            for btn in always:
                self.assertIs(self._field(updates[btn], "interactive"), True)
            self.assertIn("isn't reachable", updates[review.board_html])
            self.assertIs(
                self._field(updates[review.archive_check_again_btn], "visible"), True)

            # Reachable: archive doors back on, board shows the connected state,
            # Check again hidden.
            review.archive_dir = Path(self._tmp)
            updates = review._menu_availability()
            for btn in archive_btns:
                self.assertIs(self._field(updates[btn], "interactive"), True)
            self.assertIn("Archive folder connected", updates[review.board_html])
            self.assertIs(
                self._field(updates[review.archive_check_again_btn], "visible"), False)
        finally:
            review.archive_dir = prev

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

    def test_image_has_no_builtin_corner_buttons(self):
        """buttons=[] strips gr.Image's download/share/fullscreen corner icons;
        the labelled toolbar replaces them."""
        self.assertEqual(self.review.image.buttons, [])

    def test_toolbar_buttons_are_client_side_only(self):
        """Fullscreen acts on the rendered <img> in the browser: it has exactly
        one click wiring, fn is None (no server round-trip), the js targets
        #review-image, and it produces no outputs."""
        review = self.review
        for btn in (review.fullscreen_btn,):
            with self.subTest(button=btn):
                wirings = [
                    f for f in review.demo.fns.values()
                    if (btn._id, "click") in f.targets
                ]
                self.assertEqual(len(wirings), 1)
                fn = wirings[0]
                self.assertIsNone(fn.fn)
                self.assertIsInstance(fn.js, str)
                self.assertTrue(fn.js)
                self.assertIn("#review-image", fn.js)
                self.assertFalse(fn.outputs)

    def test_toolbar_buttons_not_in_outputs(self):
        """The Fullscreen toolbar button is not a repaint target, so the render
        contract is unchanged."""
        review = self.review
        self.assertNotIn(review.fullscreen_btn, review.outputs)
        self.assertEqual(len(review.outputs), EXPECTED_OUTPUT_COUNT)

    def test_undo_button_cancel_state_styling(self):
        """While a pending accept is open the Undo control becomes an amber
        cancel; once it clears, the amber class is explicitly removed."""
        review = self.review
        prev_pending, prev_last = review.pending_accept, review.last_action
        try:
            review.pending_accept = object()  # non-None sentinel
            cancel = review._undo_button()
            self.assertEqual(cancel["value"], "Cancel pending accept")
            self.assertIs(cancel["interactive"], True)
            self.assertEqual(cancel["elem_classes"], ["undo-cancel"])

            review.pending_accept = None
            review.last_action = None
            normal = review._undo_button()
            self.assertEqual(normal["value"], "Undo last action")
            self.assertIs(normal["interactive"], False)
            self.assertEqual(normal["elem_classes"], [])
        finally:
            review.pending_accept, review.last_action = prev_pending, prev_last

    # -- Home menu / page navigation ----------------------------------------
    # These replace the earlier sidebar tests: the shell is a home menu of nine
    # pages (the keys below), Manage holds the Character Folders / Sync /
    # Settings tabs, and every non-home page has a "← Menu" back button. Free up
    # saved space is a tenth navigable page with no home-menu door (reached from
    # Manage and the Ingest result), so it isn't in this door-driven list -- its
    # own tests cover it.
    _PAGE_KEYS = ["home", "review", "resolve", "archive_dryrun", "archive_apply",
                  "ingest", "backfill", "manage", "history"]

    def _pages(self):
        review = self.review
        return {k: getattr(review, f"{k}_page") for k in self._PAGE_KEYS}

    def _menu_buttons(self):
        review = self.review
        return {k: getattr(review, f"menu_{k}_btn")
                for k in self._PAGE_KEYS if k != "home"}

    def test_only_home_visible_at_start(self):
        """The shell opens on the home menu; every other page starts hidden.

        Pages stay mounted (visible stays True) and are hidden with our own
        .page-hidden CSS class, never unmounted — see the note on _nav_to."""
        pages = self._pages()
        for key, page in pages.items():
            self.assertIs(page.visible, True, f"{key} should stay mounted")
        self.assertEqual(pages["home"].elem_classes, ["page"])
        for key, page in pages.items():
            if key != "home":
                self.assertIn("page-hidden", page.elem_classes,
                              f"{key} should start hidden")

    def test_nav_to_shows_exactly_one_page(self):
        """_nav_to(key) repaints exactly the nav_outputs set and shows only the
        chosen page (elem_classes without "page-hidden") with the rest
        CSS-hidden (containing "page-hidden"); no page update carries a visible
        key. Opening Manage also selects its Character Folders tab. A bad key
        raises."""
        review = self.review
        pages = self._pages()
        for key in pages:
            with self.subTest(key=key):
                render = review._nav_to(key)
                self.assertEqual(set(map(id, render)), set(map(id, review.nav_outputs)))
                shown = [k for k, p in pages.items()
                         if "page-hidden" not in render[p]["elem_classes"]]
                self.assertEqual(shown, [key])
                for k, p in pages.items():
                    if k != key:
                        self.assertIn("page-hidden", render[p]["elem_classes"])
                    self.assertNotIn("visible", render[p])
                if key == "manage":
                    self.assertEqual(render[review.manage_tabs].selected, "char")
        with self.assertRaises(ValueError):
            review._nav_to("nope")

    def test_page_hidden_css_present(self):
        """Pages are hidden by our own CSS class, so it must be defined."""
        css = self.review.CUSTOM_CSS
        self.assertIn(".page-hidden", css)
        self.assertIn("display: none", css)

    def test_menu_buttons_route_to_their_page(self):
        """Each of the eight menu buttons has exactly one click ROOT fn
        (trigger_after is None) named _nav_<key>. menu_manage_btn also carries
        the @gr.render block (a second root). The Manage nav root chains the two
        Character-Folders live reads, Review chains its _review_open, Resolve
        and History each chain their embedded page's open function (resolve's
        on_load / history's on_open); every other button chains nothing."""
        review = self.review
        fns = list(review.demo.fns.values())
        for key, btn in self._menu_buttons().items():
            nav_name = f"_nav_{key}"
            with self.subTest(button=nav_name):
                roots = [f for f in fns
                         if (btn._id, "click") in f.targets and f.trigger_after is None]
                nav_roots = [f for f in roots
                             if f.fn is not None and f.fn.__name__ == nav_name]
                self.assertEqual(len(nav_roots), 1)
                # Manage carries the extra @gr.render root; the rest carry one.
                self.assertEqual(len(roots), 2 if key == "manage" else 1)

                chain, current = [], nav_roots[0]._id
                while True:
                    nxt = [f for f in fns if f.trigger_after == current]
                    if not nxt:
                        break
                    self.assertEqual(len(nxt), 1)
                    chain.append(nxt[0].fn.__name__)
                    current = nxt[0]._id
                if key == "manage":
                    # Manage also re-reads the App panel's launch-mode radio on open.
                    expected = ["_character_tab_open", "_series_tab_open", "_app_render"]
                elif key == "review":
                    expected = ["_review_open"]
                elif key == "resolve":
                    expected = ["on_load"]      # resolve.build_ui's open fn
                elif key == "history":
                    expected = ["on_open"]      # history.build_ui's open fn
                elif key in ("ingest", "backfill", "archive_dryrun", "archive_apply"):
                    # Job pages reset their result panel on open.
                    expected = [f"_reset_{key}"]
                else:
                    expected = []
                self.assertEqual(chain, expected)

    def test_every_page_has_a_back_button(self):
        """Nine back buttons (one per non-home page, incl. Free up saved space),
        each with one ROOT click fn that repaints the full nav_outputs set, then
        chains _menu_availability (returning to the menu re-checks
        Ingest/Backfill). The root is _nav_home for every page except Review,
        whose back button is _nav_home_from_review (it abandons any open accept
        confirmation on the way out)."""
        review = self.review
        fns = list(review.demo.fns.values())
        self.assertEqual(len(review.BACK_BUTTONS), 9)
        # Menu-availability target: the 8 door/tool buttons (labels + enabled) +
        # the board HTML + its two context buttons (Check again, Show progress) +
        # the Quit button (shown/hidden and enabled by launch mode).
        self.assertEqual(len(review.menu_avail_outputs), 12)
        want = [c._id for c in review.nav_outputs]
        avail = [c._id for c in review.menu_avail_outputs]
        for btn in review.BACK_BUTTONS:
            roots = [f for f in fns
                     if (btn._id, "click") in f.targets and f.trigger_after is None]
            self.assertEqual(len(roots), 1)
            root = roots[0]
            expected_fn = ("_nav_home_from_review" if btn is review.review_back_btn
                           else "_nav_home")
            self.assertEqual(root.fn.__name__, expected_fn)
            self.assertEqual([c._id for c in root.outputs], want)
            nxt = [f for f in fns if f.trigger_after == root._id]
            self.assertEqual(len(nxt), 1)
            self.assertEqual(nxt[0].fn.__name__, "_menu_availability")
            self.assertEqual([c._id for c in nxt[0].outputs], avail)

    def test_manage_tab_selects_do_their_live_reads(self):
        """Inside Manage the user can switch tabs, so each tab re-reads on
        select: Character Folders fires both open-reads plus its @gr.render
        block, Sync fires _sync_tab_open, Settings fires _settings_render."""
        review = self.review
        fns = list(review.demo.fns.values())

        def selects(tab):
            return [f for f in fns if (tab._id, "select") in f.targets]

        char_selects = selects(review.char_tab)
        self.assertEqual(len(char_selects), 3)  # two open-reads + the @gr.render
        char_names = {f.fn.__name__ for f in char_selects if f.fn is not None}
        self.assertIn("_character_tab_open", char_names)
        self.assertIn("_series_tab_open", char_names)

        sync_names = {f.fn.__name__ for f in selects(review.sync_tab) if f.fn is not None}
        self.assertEqual(sync_names, {"_sync_tab_open"})
        settings_names = {f.fn.__name__ for f in selects(review.settings_tab)
                          if f.fn is not None}
        self.assertEqual(settings_names, {"_settings_render"})

    def test_nav_components_not_in_review_outputs(self):
        """The menu, page and back-button components are never touched by the
        Review repaint, so EXPECTED_OUTPUT_COUNT is unaffected."""
        review = self.review
        nav_components = (list(review.nav_outputs) + list(review.BACK_BUTTONS)
                          + list(self._menu_buttons().values()))
        for component in nav_components:
            self.assertNotIn(component, review.outputs)
        self.assertEqual(len(review.outputs), EXPECTED_OUTPUT_COUNT)

    # -- Phase C: Resolve & History embedded as pages ------------------------
    @staticmethod
    def _descendants(block):
        """Every component under a layout block, inclusive, depth-first."""
        seen = [block]
        for child in getattr(block, "children", None) or []:
            seen.extend(RenderContractTest._descendants(child))
        return seen

    def _has_elem_id(self, root, elem_id):
        return any(getattr(c, "elem_id", None) == elem_id
                   for c in self._descendants(root))

    def test_resolve_page_renders_resolve_ui(self):
        """The Resolve page is resolve.build_ui() built in place, not a stub, so
        resolve's image (elem_id resolve-image) is a descendant of resolve_page."""
        review = self.review
        self.assertTrue(self._has_elem_id(review.resolve_page, "resolve-image"))

    def test_history_page_renders_history_ui(self):
        """The History page is history.build_ui() built in place, so history's
        gallery (elem_id history-gallery) is a descendant of history_page."""
        review = self.review
        self.assertTrue(self._has_elem_id(review.history_page, "history-gallery"))

    def test_resolve_reload_state_in_place(self):
        """resolve.reload_state() re-reads from disk while keeping the SAME
        manifest and layout dict objects (handler closures hold them), picks up a
        change written to disk, and clears last_action and pending_alias."""
        resolve = self.review.resolve
        manifest_obj, layout_obj = resolve.manifest, resolve.layout
        prev_archive = resolve.archive_dir
        prev_last, prev_pending = resolve.last_action, resolve.pending_alias
        disk = Path("manifest.json")
        original = disk.read_text(encoding="utf-8")
        try:
            # Keep plan_moves off the user's real archive (dotenv loads it).
            (Path(self._tmp) / "archive").mkdir(exist_ok=True)
            resolve.archive_dir = Path(self._tmp) / "archive"
            resolve.last_action = {"kind": "layout", "snapshot": {}, "resolved_post_id": "x"}
            resolve.pending_alias = {"variant": "v", "canonical": "c", "code": "C", "post_id": "x"}

            data = json.loads(original)
            data["t3_test01"]["title"] = "Changed On Disk"
            disk.write_text(json.dumps(data, indent=2), encoding="utf-8")

            with redirect_stdout(StringIO()):
                resolve.reload_state()

            self.assertIs(resolve.manifest, manifest_obj)   # same dict object
            self.assertIs(resolve.layout, layout_obj)        # same dict object
            self.assertEqual(resolve.manifest["t3_test01"]["title"], "Changed On Disk")
            self.assertIsNone(resolve.last_action)
            self.assertIsNone(resolve.pending_alias)
        finally:
            disk.write_text(original, encoding="utf-8")
            # Re-read the restored manifest while archive_dir still points at the
            # sandbox, so plan_moves never touches the real drive; then restore.
            with redirect_stdout(StringIO()):
                resolve.reload_state()
            resolve.archive_dir = prev_archive
            resolve.last_action, resolve.pending_alias = prev_last, prev_pending

    def test_visiting_resolve_marks_review_dirty_and_next_review_open_reloads(self):
        """_nav_to('resolve') marks review's copies dirty; the next time a
        manifest/config-writing page (Review) is opened, _nav_to re-reads disk
        in place -- a layout key written to disk meanwhile lands in review.layout
        (the same object) and the dirty flag clears."""
        review = self.review
        layout_obj = review.layout
        prev_dirty = review._external_dirty
        disk = Path("layout.json")
        original = disk.read_text(encoding="utf-8")
        key = "__phase_c_probe__"
        try:
            review._external_dirty = False
            with redirect_stdout(StringIO()):
                review._nav_to("resolve")
            self.assertTrue(review._external_dirty)
            self.assertNotIn(key, review.layout)

            data = json.loads(original)
            data.setdefault("franchise_aliases", {})[key] = "Probe Folder"
            disk.write_text(json.dumps(data, indent=2), encoding="utf-8")

            with redirect_stdout(StringIO()):
                review._nav_to("review")

            self.assertIs(review.layout, layout_obj)  # reloaded in place
            self.assertEqual(review.layout.get("franchise_aliases", {}).get(key),
                             "Probe Folder")
            self.assertFalse(review._external_dirty)
        finally:
            disk.write_text(original, encoding="utf-8")
            review._external_dirty = prev_dirty
            with redirect_stdout(StringIO()):
                review._reload_from_disk()

    def test_back_from_review_cancels_pending_accept(self):
        """Review's back button abandons an open accept confirmation (entry stays
        pending) and navigates home -- the same cancel path as the Undo control."""
        review = self.review
        prev_pending = review.pending_accept
        try:
            review.pending_accept = object()  # a confirmation is open
            with redirect_stdout(StringIO()):
                render = review._nav_home_from_review()
            self.assertIsNone(review.pending_accept)
            # It still performs a home nav: every page update, home shown.
            self.assertEqual(set(map(id, render)), set(map(id, review.nav_outputs)))
            self.assertNotIn("page-hidden", render[review.home_page]["elem_classes"])
        finally:
            review.pending_accept = prev_pending

    def test_history_never_imports_a_writer(self):
        """History's read-only guarantee is structural: parse history.py's
        imports and assert it pulls in no save_manifest / save_layout / save_*."""
        source = (REPO / "history.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                for alias in node.names:
                    imported.add(alias.name)
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    imported.add(alias.name)
        writers = {n for n in imported if n.startswith("save_") or n == "save_manifest"}
        self.assertEqual(writers, set(), f"history.py imports a writer: {writers}")

    def test_resolve_and_history_still_run_standalone(self):
        """Importing resolve/history must not execute their __main__ launch (no
        server starts on import) and must expose a callable build_ui. Checked in
        a SUBPROCESS so it never touches this process's module cache."""
        script = textwrap.dedent(f"""
            import sys
            sys.path.insert(0, {str(REPO)!r})
            import resolve, history
            # __name__ guard held: neither launched a server on import.
            assert callable(resolve.build_ui), "resolve.build_ui missing"
            assert callable(history.build_ui), "history.build_ui missing"
            assert callable(resolve.reload_state), "resolve.reload_state missing"
            print("STANDALONE OK")
        """)
        out = subprocess.run(
            [sys.executable, "-c", script],
            capture_output=True, text=True, timeout=300,
        )
        self.assertIn("STANDALONE OK", out.stdout, out.stderr)

    # -- Phase B: jobs glue --------------------------------------------------
    def test_review_open_skips_when_not_stale(self):
        """Opening Review when nothing invalidated it must repaint nothing: every
        output is gr.skip(), so in-progress edits are never clobbered."""
        review = self.review
        prev = review._review_stale
        try:
            review._review_stale = False
            result = review._review_open()
            self.assertEqual(set(map(id, result)), set(map(id, review.outputs)))
            self.assertTrue(all(v == gr.skip() for v in result.values()))
        finally:
            review._review_stale = prev

    def test_review_open_repaints_and_clears_flag_when_stale(self):
        """After a job marks it stale, the next open is a full repaint over every
        output and the flag is cleared (so the one after it skips again)."""
        review = self.review
        prev = review._review_stale
        try:
            review._review_stale = True
            with redirect_stdout(StringIO()):
                result = review._review_open()
            self.assertEqual(set(map(id, result)), set(map(id, review.outputs)))
            self.assertFalse(review._review_stale)
        finally:
            review._review_stale = prev

    def test_reload_after_job_refreshes_the_same_objects_in_place(self):
        """A job that wrote the manifest calls _reload_after_job; it must refresh
        the SAME manifest dict and queue list objects (handler closures hold
        those references) and reset the cursor."""
        review = self.review
        manifest_obj = review.manifest
        queue_obj = review.queue
        disk = Path("manifest.json")
        original = disk.read_text(encoding="utf-8")
        prev_idx, prev_last, prev_stale = (review.current_index,
                                           review.last_action, review._review_stale)
        try:
            data = json.loads(original)
            data["t3_newjob1"] = {
                "post_id": "t3_newjob1",
                "title": "Freshly ingested",
                "subreddit": "TestSub",
                "permalink": "https://reddit.com/r/TestSub/comments/newjob1/",
                "image_url": "https://i.redd.it/newjob1.jpg",
                "local_path": "staging/t3_newjob1.jpg",
                "fetched_at": "2026-02-02T00:00:00+00:00",
                "status": "pending_review",
                "franchise": [],
                "character_guess": [],
                "guess_confidence": "low",
                "guess_source": "title",
                "crossover": False,
            }
            disk.write_text(json.dumps(data, indent=2), encoding="utf-8")

            review._reload_after_job()

            self.assertIs(review.manifest, manifest_obj)  # same dict object
            self.assertIs(review.queue, queue_obj)        # same list object
            self.assertIn("t3_newjob1", review.manifest)
            self.assertIn("t3_newjob1", review.queue)
            self.assertEqual(review.current_index, 0)
            self.assertIsNone(review.last_action)
            self.assertTrue(review._review_stale)
        finally:
            disk.write_text(original, encoding="utf-8")
            review._reload_after_job()
            review.current_index, review.last_action = prev_idx, prev_last
            review._review_stale = prev_stale

    def test_a_job_refuses_while_a_review_confirmation_is_open(self):
        """pending_accept being set must block a job from even taking the lock:
        the generator yields a status message and stops, lock untouched."""
        review = self.review
        prev = review.pending_accept
        try:
            review.pending_accept = object()  # a confirmation is open in Review
            gen = review.on_ingest_run()
            first = next(gen)
            self.assertIn(review.ingest_status_md, first)
            self.assertIn("Finish or cancel", first[review.ingest_status_md])
            self.assertIsNone(review.jobs.running())
            with self.assertRaises(StopIteration):
                next(gen)
        finally:
            review.pending_accept = prev
            review.jobs.finish()

    def test_archive_apply_click_outputs_include_the_drain_reminder(self):
        """extra_done surfaces archive_apply_next_md on a successful apply, so
        it must be a repaint target of the apply click or the reminder could
        never show."""
        review = self.review
        fns = list(review.demo.fns.values())
        roots = [f for f in fns
                 if (review.archive_apply_run_btn._id, "click") in f.targets
                 and f.trigger_after is None]
        self.assertEqual(len(roots), 1)
        self.assertIn(review.archive_apply_next_md._id,
                      [c._id for c in roots[0].outputs])

    def test_drain_reminder_was_renamed_from_backfill_only(self):
        """The reminder is now shared by Backfill and Archive apply: the old
        _backfill_next_text is gone and _drain_next_text took its place."""
        review = self.review
        self.assertTrue(callable(getattr(review, "_drain_next_text", None)))
        self.assertFalse(hasattr(review, "_backfill_next_text"))

    def test_drain_reminder_points_at_the_page_and_links_the_saved_page(self):
        """The drain reminder points at the Free up saved space page and always
        carries a saved-page link via _saved_page_url (so no username -> the
        /user/me/saved form), with the old "set REDDIT_USERNAME" hint gone."""
        review = self.review
        text = review._drain_next_text()
        self.assertIn("Free up saved space", text)
        self.assertIn(review._saved_page_url(review._reddit_username()), text)
        self.assertNotIn("set REDDIT_USERNAME", text)

    # -- Phase B: reload-after-every-manifest-job + lock ownership ------------
    def _job_script(self, name, body):
        """Write a temp script and return its ABSOLUTE path (jobs runs argv as
        [sys.executable, <path>] with cwd = the data root, so the path must be
        absolute)."""
        path = Path(self._tmp) / name
        path.write_text(textwrap.dedent(body), encoding="utf-8")
        return str(path.resolve())

    @contextmanager
    def _stub_reload(self, on_call=None):
        """Swap review._reload_after_job for a counting stub; save/restore it, the
        lock and _backfill_cancel."""
        review = self.review
        prev_reload = review._reload_after_job
        prev_cancel = review._backfill_cancel.is_set()
        review.jobs.finish()  # start from a free lock
        counter = {"n": 0}

        def stub():
            counter["n"] += 1
            if on_call:
                on_call()
        review._reload_after_job = stub
        try:
            yield counter
        finally:
            review._reload_after_job = prev_reload
            review.jobs.finish()
            (review._backfill_cancel.set if prev_cancel
             else review._backfill_cancel.clear)()

    def _ingest_stream(self, steps, **over):
        """_job_stream wired to the Ingest page's real components (non-cancellable,
        manifest-writing by default); `over` tweaks per test. The result-panel
        components are defaulted to Ingest's too."""
        review = self.review
        kwargs = dict(
            run_btn=review.ingest_run_btn, run_label="Fetch new saves",
            back_btn=review.ingest_back_btn, status_md=review.ingest_status_md,
            log_box=review.ingest_log_box, name="Ingest",
            cancellable=False, writes_manifest=True, job_key="ingest",
            result_group=review.ingest_result_group, seal_html=review.ingest_seal,
            result_text=review.ingest_result_text, primary_btn=review.ingest_primary_btn,
            log_acc=review.ingest_log_acc,
        )
        kwargs.update(over)
        return review._job_stream(steps, **kwargs)

    def test_failed_manifest_job_still_reloads(self):
        review = self.review
        script = self._job_script("ph_fail.py", "import sys; print('x'); sys.exit(1)")
        with self._stub_reload() as counter:
            events = list(self._ingest_stream([review.jobs.Step("Boom", [script], "stop")]))
            # Failure fills the result panel (no seal) and leaves the log open.
            self.assertIn("Didn't finish", events[-1][review.ingest_result_text])
            self.assertEqual(events[-1][review.ingest_seal], "")  # no seal on failure
            self.assertIs(events[-1][review.ingest_log_acc]["open"], True)
            self.assertEqual(counter["n"], 1)
        self.assertIsNone(review.jobs.running())

    def test_cancelled_manifest_job_still_reloads(self):
        review = self.review
        script = self._job_script("ph_cancel.py", """
            import sys, time
            print("started"); sys.stdout.flush()
            time.sleep(5)
        """)
        with self._stub_reload() as counter:
            review._backfill_cancel.clear()
            gen = self._ingest_stream(
                [review.jobs.Step("Long", [script], "stop")],
                run_btn=review.backfill_run_btn, run_label="Run backfill",
                back_btn=review.backfill_back_btn, status_md=review.backfill_status_md,
                log_box=review.backfill_log_box, name="Backfill", cancellable=True,
                job_key="backfill",
                result_group=review.backfill_result_group, seal_html=review.backfill_seal,
                result_text=review.backfill_result_text, primary_btn=review.backfill_primary_btn,
                log_acc=review.backfill_log_acc,
            )
            events = [next(gen)]  # first dict (buttons disabled)
            for ev in gen:
                events.append(ev)
                if review.backfill_log_box in ev:  # first streamed log yield
                    review._backfill_cancel.set()
                    break
            events += list(gen)
            # Cancel: the "Stopped." result, no seal.
            self.assertIn("Stopped.", events[-1][review.backfill_result_text])
            self.assertEqual(events[-1][review.backfill_seal], "")  # no seal on cancel
            self.assertEqual(counter["n"], 1)
        self.assertIsNone(review.jobs.running())

    def test_dryrun_never_reloads(self):
        review = self.review
        script = self._job_script("ph_dry.py", "print('plan: nothing to move')")
        with self._stub_reload() as counter:
            events = list(self._ingest_stream(
                [review.jobs.Step("Dry", [script], "stop")],
                run_btn=review.archive_dryrun_run_btn, run_label="Show the plan",
                back_btn=review.archive_dryrun_back_btn,
                status_md=review.archive_dryrun_status_md,
                log_box=review.archive_dryrun_log_box, name="Archive dry run",
                writes_manifest=False, job_key="preview_filing",
                result_group=review.archive_dryrun_result_group,
                seal_html=review.archive_dryrun_seal,
                result_text=review.archive_dryrun_result_text,
                primary_btn=review.archive_dryrun_primary_btn,
                log_acc=review.archive_dryrun_log_acc,
            ))
            # Preview filing success: the number is dropped (no structured count).
            self.assertIn("Nothing was moved", events[-1][review.archive_dryrun_result_text])
            self.assertEqual(counter["n"], 0)
        self.assertIsNone(review.jobs.running())

    def _dryrun_stream(self, steps):
        review = self.review
        return self._ingest_stream(
            steps,
            run_btn=review.archive_dryrun_run_btn, run_label="Show the plan",
            back_btn=review.archive_dryrun_back_btn,
            status_md=review.archive_dryrun_status_md,
            log_box=review.archive_dryrun_log_box, name="Archive dry run",
            writes_manifest=False, job_key="preview_filing",
            result_group=review.archive_dryrun_result_group,
            seal_html=review.archive_dryrun_seal,
            result_text=review.archive_dryrun_result_text,
            primary_btn=review.archive_dryrun_primary_btn,
            log_acc=review.archive_dryrun_log_acc,
            extra_result=review._dryrun_result_extra,
            plan_table=review.archive_dryrun_plan_table,
            plan_wrap=review.archive_dryrun_plan_wrap,
        )

    def test_dryrun_success_renders_the_table_and_hides_the_summary(self):
        """The dry run's output IS its result: on success the plan renders as a
        table (from the OSHIIRE_PLAN rows), the raw log stays under a CLOSED
        "Details" accordion, and the structured OSHIIRE_SUMMARY/OSHIIRE_PLAN
        lines feed the UI but never reach the log."""
        review = self.review
        script = self._job_script("ph_plan.py", r"""
            print("post_id | action | destination")
            print("t3_a    | MOVE   | Genshin Impact/押入れ")
            print("t3_b    | FLAG   | -")
            print('OSHIIRE_PLAN {"image": "t3_b", "franchise": "Genshin Impact", "character": "Nahida", "destination": "-", "status": "needs Resolve", "needs_resolve": true}')
            print('OSHIIRE_PLAN {"image": "t3_a", "franchise": "Genshin Impact", "character": "Ayaka", "destination": "Genshin Impact/押入れ", "status": "move", "needs_resolve": false}')
            print("OSHIIRE_SUMMARY moves=1 flagged=1")
        """)
        with self._stub_reload():
            last = list(self._dryrun_stream([review.jobs.Step("Dry", [script], "stop")]))[-1]
        # The log accordion is CLOSED now, back under its Details name.
        acc = last[review.archive_dryrun_log_acc]
        self.assertIs(acc["open"], False)
        self.assertEqual(acc["label"], "Details: the full job log")
        # The table is shown and holds BOTH rows, needs-Resolve first. It is an
        # HTML <table> string now (not a Dataframe's list-of-lists).
        self.assertEqual(last[review.archive_dryrun_plan_wrap]["elem_classes"],
                         review._PLAN_SHOWN)
        table_html = last[review.archive_dryrun_plan_table]["value"]
        self.assertLess(table_html.index("t3_b"), table_html.index("t3_a"))  # flag first
        self.assertIn("needs Resolve", table_html)
        self.assertIn("Genshin Impact/押入れ", table_html)
        self.assertIn('class="needs-resolve"', table_html)
        # The raw text table is in the log; the machine lines are not.
        log = last[review.archive_dryrun_log_box]
        self.assertIn("Genshin Impact/押入れ", log)
        self.assertNotIn("OSHIIRE_SUMMARY", log)
        self.assertNotIn("OSHIIRE_PLAN", log)
        self.assertIn("1 image is ready to file; 1 needs Resolve first",
                      last[review.archive_dryrun_result_text])
        # Resolve is the primary; File images sits beside it as the secondary.
        self.assertEqual(last[review.archive_dryrun_resolve_btn]["elem_classes"], [])
        self.assertEqual(last[review.archive_dryrun_primary_btn]["variant"], "secondary")
        self.assertEqual(last[review.archive_dryrun_primary_btn]["elem_classes"], [])
        self.assertEqual(last[review.archive_dryrun_review_btn]["elem_classes"], ["page-hidden"])

    def test_dryrun_table_scales_to_a_thousand_rows(self):
        """Real-scale fixture: the dry run streams ~1000 OSHIIRE_PLAN rows with
        long franchise names and archive paths (the example sandbox only ever had
        ~12). _job_stream must collect all of them into the table value, keep the
        machine lines out of the log, and finish well within a loose budget."""
        review = self.review
        script = self._job_script("ph_bulk.py", r"""
            import json
            LF = "Grand Celestial Chronicle of the Eternal Radiant Dominion XVII"
            LC = "Seraphine Valentina Aldercrest the Third"
            moves = flags = 0
            for i in range(1000):
                flag = (i % 11 == 0)
                flags += flag; moves += (not flag)
                row = {"image": f"t3_bulk{i:05d}", "franchise": LF, "character": LC,
                       "destination": "-" if flag else f"{LF}/{LC}/bulk_{i:05d}_long_source_filename.jpg",
                       "status": "needs Resolve" if flag else "move", "needs_resolve": flag}
                print("OSHIIRE_PLAN " + json.dumps(row, ensure_ascii=False))
            print(f"OSHIIRE_SUMMARY moves={moves} flagged={flags}")
        """)
        start = time.perf_counter()
        with self._stub_reload():
            last = list(self._dryrun_stream([review.jobs.Step("Dry", [script], "stop")]))[-1]
        elapsed = time.perf_counter() - start

        table_html = last[review.archive_dryrun_plan_table]["value"]
        # All 1000 rows are present in the HTML (one id per row).
        self.assertEqual(table_html.count("t3_bulk"), 1000)
        self.assertEqual(last[review.archive_dryrun_plan_wrap]["elem_classes"],
                         review._PLAN_SHOWN)
        # Long strings survive into the table rows.
        self.assertIn("Grand Celestial Chronicle", table_html)
        self.assertIn("bulk_", table_html)
        # None of the 1000 machine lines leak into the log box.
        self.assertNotIn("OSHIIRE_PLAN", last[review.archive_dryrun_log_box])
        # Spawning the child + streaming 1000 rows is subprocess-bound; 30s is a
        # generous ceiling that still fails if row handling went quadratic.
        self.assertLess(elapsed, 30.0, f"1000-row dry run stream took {elapsed:.1f}s")

    def test_plan_table_html_escapes_and_structures(self):
        """_plan_table_html builds a safe HTML <table>: cells are escaped (a
        title with < or & cannot break the markup), header + one <tr> per row,
        needs-Resolve rows are marked, and no rows -> empty string."""
        review = self.review
        rows = [
            {"image": "t3_x", "franchise": "A & B <Co>", "character": "-",
             "destination": "Folder/x.jpg", "status": "needs Resolve", "needs_resolve": True},
            {"image": "t3_y", "franchise": "Plain", "character": "Hu Tao",
             "destination": "Plain/y.jpg", "status": "move", "needs_resolve": False},
        ]
        out = review._plan_table_html(rows)
        self.assertIn("<table", out)
        self.assertIn("A &amp; B &lt;Co&gt;", out)   # escaped
        self.assertNotIn("<Co>", out)                # the raw tag never appears
        self.assertEqual(out.count("<tr"), 3)        # header + 2 body rows
        self.assertIn('class="needs-resolve"', out)
        self.assertEqual(review._plan_table_html([]), "")

    def test_dryrun_failure_keeps_the_details_name(self):
        review = self.review
        script = self._job_script("ph_dryfail.py", "import sys; print('x'); sys.exit(1)")
        with self._stub_reload():
            last = list(self._dryrun_stream([review.jobs.Step("Dry", [script], "stop")]))[-1]
        acc = last[review.archive_dryrun_log_acc]
        self.assertIs(acc["open"], True)
        self.assertEqual(acc["label"], "Details: the full job log")

    def test_dryrun_reset_restores_the_details_name(self):
        review = self.review
        out = review._reset_archive_dryrun()
        self.assertEqual(out[2]["label"], "Details: the full job log")
        self.assertIs(out[2]["open"], False)
        self.assertEqual(len(out), 11)  # matches _dryrun_reset_outputs
        # The last two reset the plan table: cleared value, wrapper hidden.
        self.assertEqual(out[-2]["value"], "")
        self.assertEqual(out[-1]["elem_classes"], review._PLAN_HIDDEN)

    def test_lock_held_through_reload(self):
        review = self.review
        script = self._job_script("ph_ok.py", "print('ok')")
        held = {"while_reloading": None}

        def check():
            held["while_reloading"] = review.jobs.running()
        with self._stub_reload(on_call=check) as counter:
            list(self._ingest_stream([review.jobs.Step("Ok", [script], "stop")]))
            self.assertEqual(counter["n"], 1)
            self.assertIsNotNone(held["while_reloading"])  # lock still held during reload
        self.assertIsNone(review.jobs.running())

    def test_early_close_reloads_and_waits_for_non_cancellable_child(self):
        review = self.review
        marker = Path(self._tmp) / "ph_marker.txt"
        if marker.exists():
            marker.unlink()
        script = self._job_script("ph_early.py", f"""
            import sys, time
            from pathlib import Path
            print("go"); sys.stdout.flush()
            time.sleep(0.4)
            print("mid"); sys.stdout.flush()
            time.sleep(1.0)
            Path(r"{marker}").write_text("done", encoding="utf-8")
        """)
        with self._stub_reload() as counter:
            gen = self._ingest_stream([review.jobs.Step("Slow", [script], "stop")])
            next(gen)  # first dict
            for ev in gen:
                # Wait for a log yield that proves the child is mid-run (it has
                # printed "mid" and is now sleeping before writing the marker).
                box = ev.get(review.ingest_log_box)
                if box and "mid" in box:
                    break
            gen.close()
            # close() must have blocked on the non-cancellable child, which only
            # writes the marker at its very end. CPython closes the inner
            # generator during unwind; this pins that behaviour.
            self.assertTrue(marker.exists())
            self.assertEqual(counter["n"], 1)
        self.assertIsNone(review.jobs.running())

    def test_menu_locked_while_a_job_runs(self):
        """A reload / second tab lands on Home mid-job; the whole menu must be
        locked (nothing can reach a manifest-writing page), the board shows the
        job with a Show-progress button, and it all unlocks once the job ends."""
        review = self.review
        review.jobs.finish()  # start free
        ungated = [review.menu_review_btn, review.menu_resolve_btn,
                   review.menu_archive_dryrun_btn, review.menu_archive_apply_btn,
                   review.menu_manage_btn, review.menu_history_btn]
        # Its own reachable archive folder: the archive doors are only "always
        # available" when ARCHIVE_DIR is set and reachable, and this test must
        # not depend on the real .env providing one.
        prev_archive_dir = review.archive_dir
        review.archive_dir = Path(tempfile.mkdtemp(prefix="render-archive-"))
        self.addCleanup(setattr, review, "archive_dir", prev_archive_dir)
        try:
            self.assertTrue(review.jobs.try_start("Backfill"))
            locked = review._menu_availability()
            for btn in review._MENU_BUTTONS:  # all eight, incl. Ingest/Backfill
                self.assertFalse(locked[btn]["interactive"])
            self.assertIn("Backfill", locked[review.board_html])
            self.assertIs(locked[review.job_progress_btn]["visible"], True)

            review.jobs.finish()
            free = review._menu_availability()
            for btn in ungated:  # the six always-available ones are back on
                self.assertTrue(free[btn]["interactive"])
            self.assertIs(free[review.job_progress_btn]["visible"], False)
        finally:
            review.jobs.finish()


    # -- Free up saved space page -------------------------------------------
    def test_unsave_page_is_navigable_and_built(self):
        """The Free-up-space page is a real navigable page (in nav_outputs, with
        a back button) built with the job-page controls -- run button, result
        frame, log box, and the three step buttons."""
        review = self.review
        self.assertIn(review.unsave_page, review.nav_outputs)
        self.assertIn(review.unsave_back_btn, review.BACK_BUTTONS)
        kids = self._descendants(review.unsave_page)
        for comp in (review.unsave_run_btn, review.unsave_result_group,
                     review.unsave_seal, review.unsave_result_text,
                     review.unsave_openfolder_btn, review.unsave_userscript_btn,
                     review.unsave_oldreddit_btn, review.unsave_saved_btn,
                     review.unsave_log_box):
            self.assertIn(comp, kids)

    def test_nav_to_unsave_shows_only_that_page(self):
        review = self.review
        render = review._nav_to("unsave")
        self.assertEqual(set(map(id, render)), set(map(id, review.nav_outputs)))
        self.assertNotIn("page-hidden", render[review.unsave_page]["elem_classes"])
        # Every other page is hidden.
        for p in review.nav_outputs:
            if p is review.unsave_page or p is review.manage_tabs:
                continue
            self.assertIn("page-hidden", render[p]["elem_classes"])

    def test_ingest_nothing_new_shows_the_freeup_button(self):
        """A nothing-new Ingest success reveals the Free up space button and says
        why; a with-images success hides it (Go to Review is offered instead)."""
        review = self.review
        res_none = review.job_result("ingest", "success", {"new_waiting": 0})
        self.assertIn("Nothing new to review", res_none.sentence)
        self.assertIn("1,000", res_none.sentence)
        self.assertIsNone(res_none.next_label)
        extra = review._ingest_result_extra("success", res_none)
        self.assertEqual(extra[review.ingest_freeup_btn]["elem_classes"], [])

        res_some = review.job_result("ingest", "success", {"new_waiting": 3})
        self.assertEqual(res_some.next_label, "Go to Review")
        extra2 = review._ingest_result_extra("success", res_some)
        self.assertEqual(extra2[review.ingest_freeup_btn]["elem_classes"], ["page-hidden"])

        # Backfill's nothing-new keeps its own wording (it has its own drain
        # reminder), so the shared branch didn't change for it.
        self.assertEqual(
            review.job_result("backfill", "success", {"new_waiting": 0}).sentence,
            "Done. Nothing new to review.")

    def test_export_unsave_result_is_pluralised(self):
        review = self.review
        self.assertEqual(
            review.job_result("export_unsave", "success", {"safe": 1}).sentence,
            "1 post is safe to unsave (already in your archive).")
        self.assertEqual(
            review.job_result("export_unsave", "success", {"safe": 2}).sentence,
            "2 posts are safe to unsave (already in your archive).")
        self.assertEqual(
            review.job_result("export_unsave", "success", {"safe": 0}).sentence,
            "0 posts are safe to unsave (already in your archive).")
        res = review.job_result("export_unsave", "success", {"safe": 5})
        self.assertTrue(res.seal)          # 済 on success
        self.assertIsNone(res.next_label)  # no next-step button

    def test_saved_page_url(self):
        review = self.review
        # No username -> the /user/me/saved form (old.reddit resolves "me").
        self.assertEqual(review._saved_page_url(""),
                         "https://old.reddit.com/user/me/saved")
        self.assertEqual(review._saved_page_url("   "),
                         "https://old.reddit.com/user/me/saved")
        self.assertEqual(review._saved_page_url("alice"),
                         "https://old.reddit.com/user/alice/saved")

    def test_open_saved_page_falls_back_to_me_without_username(self):
        """No REDDIT_USERNAME -> still opens, via the /user/me/saved form."""
        review = self.review
        import webbrowser
        prev_user, prev_open = review._reddit_username, webbrowser.open
        opened = []
        try:
            review._reddit_username = lambda: ""
            webbrowser.open = lambda url, *a, **k: opened.append(url)
            review._open_saved_page()
            self.assertEqual(opened, ["https://old.reddit.com/user/me/saved"])
        finally:
            review._reddit_username, webbrowser.open = prev_user, prev_open

    def test_open_old_reddit_opens_the_front_page(self):
        review = self.review
        import webbrowser
        prev_open = webbrowser.open
        opened = []
        try:
            webbrowser.open = lambda url, *a, **k: opened.append(url)
            msg = review._open_old_reddit()
            self.assertEqual(opened, ["https://old.reddit.com/"])
            self.assertIn("logged in", msg)
        finally:
            webbrowser.open = prev_open

    def test_open_saved_page_opens_when_username_set(self):
        review = self.review
        import webbrowser
        prev_user, prev_open = review._reddit_username, webbrowser.open
        opened = []
        try:
            review._reddit_username = lambda: "bob"
            webbrowser.open = lambda url, *a, **k: opened.append(url)
            msg = review._open_saved_page()
            self.assertEqual(opened, ["https://old.reddit.com/user/bob/saved"])
            self.assertIn("Opening", msg)
        finally:
            review._reddit_username, webbrowser.open = prev_user, prev_open

    def test_change_setup_spawns_at_most_one_child(self):
        """Settings -> App "Change setup...": [child python, APP_DIR/setup_window.py,
        --change] in the data root (cwd), one at a time; a finished child allows
        a new one. Popen is a fake -- no window opens."""
        review = self.review
        prev = review._setup_proc
        self.addCleanup(setattr, review, "_setup_proc", prev)
        review._setup_proc = None
        children = []

        def fake_popen(argv, **kwargs):
            child = mock.Mock()
            child.poll.return_value = None  # still open
            children.append((argv, kwargs, child))
            return child

        first = review._open_change_setup(popen=fake_popen)
        second = review._open_change_setup(popen=fake_popen)
        self.assertEqual(first, review.CHANGE_SETUP_MSG)
        self.assertEqual(second, review.CHANGE_SETUP_MSG)
        self.assertEqual(len(children), 1)  # the second click reused the open one
        argv, kwargs, child = children[0]
        self.assertEqual(argv, [review.jobs._child_python(),
                                str(review.datahome.APP_DIR / "setup_window.py"), "--change"])
        self.assertEqual(Path(kwargs["cwd"]).resolve(), Path(os.getcwd()).resolve())
        # The pre-config environment (config.launch_environ, or os.environ when
        # config was never loaded), so the child sees each setting's true source.
        with mock.patch.object(review.config, "launch_environ", return_value={"ONLY": "pre"}):
            env = review._change_setup_env()
        self.assertEqual(env, {"ONLY": "pre", "PYTHONIOENCODING": "utf-8"})
        child.poll.return_value = 0  # the window was closed
        review._open_change_setup(popen=fake_popen)
        self.assertEqual(len(children), 2)

    def test_open_data_folder_opens_the_cwd(self):
        review = self.review
        opened = []
        if sys.platform == "win32":
            with mock.patch.object(review.os, "startfile", side_effect=opened.append, create=True):
                message = review._open_data_folder()
        else:
            with mock.patch("subprocess.Popen", side_effect=lambda argv: opened.append(argv[-1])):
                message = review._open_data_folder()
        self.assertEqual([Path(p).resolve() for p in opened], [Path(os.getcwd()).resolve()])
        self.assertIn("data folder", message)

    def test_only_the_userscript_is_served_besides_fonts(self):
        """set_static_paths exposes exactly the fonts dir and the one userscript
        file -- never the repo root or any other project file."""
        review = self.review
        paths = review._static_paths()
        self.assertEqual(len(paths), 2)
        self.assertTrue(any(p.endswith("oshiire_unsave.user.js") for p in paths))
        self.assertTrue(any(p.replace("\\", "/").endswith("assets/fonts") for p in paths))
        root = str(Path(review.__file__).resolve().parent)
        self.assertNotIn(root, paths)
        # The served file actually exists (the button hands it to the browser).
        self.assertTrue((Path(root) / "oshiire_unsave.user.js").exists())

    def test_export_unsave_stream_reports_the_pluralised_count(self):
        """End to end through _job_stream: the export job's OSHIIRE_UNSAVE line
        fills the result sentence (pluralised), never reaches the log, and the
        job never reloads the manifest (it only writes unsave_list.json)."""
        review = self.review
        script = self._job_script("ph_unsave.py", r"""
            print("Oshiire unsave whitelist")
            print("  TOTAL safe to unsave ....... 1")
            print("OSHIIRE_UNSAVE total=1")
        """)
        with self._stub_reload() as counter:
            last = list(self._ingest_stream(
                [review.jobs.Step("Export", [script], "stop")],
                run_btn=review.unsave_run_btn, run_label="Make the list",
                back_btn=review.unsave_back_btn, status_md=review.unsave_status_md,
                log_box=review.unsave_log_box, name="Free up space",
                writes_manifest=False, job_key="export_unsave",
                result_group=review.unsave_result_group, seal_html=review.unsave_seal,
                result_text=review.unsave_result_text,
                primary_btn=review.unsave_primary_btn, log_acc=review.unsave_log_acc,
            ))[-1]
            self.assertIn("1 post is safe to unsave", last[review.unsave_result_text])
            self.assertNotIn("OSHIIRE_UNSAVE", last[review.unsave_log_box])
            self.assertEqual(counter["n"], 0)  # export never reloads the manifest
        self.assertIsNone(review.jobs.running())


if __name__ == "__main__":
    unittest.main(verbosity=2)
