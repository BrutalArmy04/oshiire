"""Tests for history.build_ui(show_title=...).

History is both a standalone app and a page embedded under review.py's own
header. As a page the duplicate "## History — read-only" heading sat under the
page header; show_title=False drops the heading while keeping the second line.

    python -m unittest discover -s tests
    python tests/test_history_title.py
"""
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))

import gradio as gr

from review_sandbox import ReviewSandbox  # noqa: E402

# history binds the manifest and archive dir at import: import a private copy
# inside a throwaway data root, and run this module's tests inside it too.
_sandbox = ReviewSandbox(module="history")
history = _sandbox.load()
setUpModule = _sandbox.activate
tearDownModule = _sandbox.close

SECOND_LINE = "Recently processed entries, newest first. Nothing here can be edited."


def _markdown_values(show_title):
    """Every Markdown value build_ui creates, in a throwaway Blocks."""
    with gr.Blocks() as demo:
        history.build_ui(show_title=show_title)
    return [b.value for b in demo.blocks.values()
            if isinstance(b, gr.Markdown) and b.value]


class HistoryTitleTest(unittest.TestCase):
    def test_default_keeps_the_heading(self):
        values = _markdown_values(show_title=True)
        self.assertTrue(
            any(v.startswith("## History — read-only") for v in values),
            f"heading missing from {values!r}",
        )
        self.assertTrue(any(SECOND_LINE in v for v in values))

    def test_show_title_false_drops_heading_keeps_second_line(self):
        values = _markdown_values(show_title=False)
        self.assertFalse(
            any("## History — read-only" in v for v in values),
            f"heading should be gone, got {values!r}",
        )
        # The second line survives, and as plain text (no heading markup).
        self.assertIn(SECOND_LINE, values)


if __name__ == "__main__":
    unittest.main(verbosity=2)
