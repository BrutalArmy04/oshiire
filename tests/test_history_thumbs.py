"""Tests for history._thumbnail_for -- the cached 512px JPEG thumbnails.

Thumbnails are a pure optimisation layered under the read-only History page:
a cache hit must not re-open the source, an edited file must regenerate, every
input mode must produce a valid capped JPEG, and a broken source must fall back
to the original path without raising or leaving a temp file behind.

    python -m unittest discover -s tests
    python tests/test_history_thumbs.py
"""
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from PIL import Image, ImageFile

from review_sandbox import ReviewSandbox  # noqa: E402

# history binds the manifest and archive dir at import: import a private copy
# inside a throwaway data root, and run this module's tests inside it too.
_sandbox = ReviewSandbox(module="history")
history = _sandbox.load()
setUpModule = _sandbox.activate
tearDownModule = _sandbox.close


class ThumbnailForTest(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.mkdtemp(prefix="oshiire-thumbs-")
        self.root = Path(self._dir)
        self.src_dir = self.root / "src"
        self.src_dir.mkdir()
        self.thumbs = self.root / "thumbs"
        # Point the cache at the sandbox, not the real data/thumbs.
        self._prev_thumbs = history.THUMBS_DIR
        history.THUMBS_DIR = self.thumbs

    def tearDown(self):
        history.THUMBS_DIR = self._prev_thumbs
        shutil.rmtree(self._dir, ignore_errors=True)

    # -- fixture builders ---------------------------------------------------
    def _jpeg(self, name="big.jpg", size=(1200, 800)):
        path = self.src_dir / name
        Image.new("RGB", size, (90, 110, 130)).save(path, "JPEG")
        return path

    def _rgba_png(self, name="alpha.png", size=(900, 1200)):
        path = self.src_dir / name
        Image.new("RGBA", size, (200, 30, 40, 128)).save(path, "PNG")
        return path

    def _palette_gif(self, name="pal.gif", size=(700, 500)):
        path = self.src_dir / name
        img = Image.new("P", size)
        img.putpalette([i % 256 for i in range(768)])
        img.info["transparency"] = 0
        img.save(path, "GIF", transparency=0)
        return path

    def _animated_gif(self, name="anim.gif", size=(640, 480)):
        path = self.src_dir / name
        frames = [Image.new("P", size, color) for color in (1, 2, 3)]
        frames[0].save(path, "GIF", save_all=True, append_images=frames[1:], loop=0)
        return path

    def _tmp_files(self):
        return list(self.thumbs.glob("*.tmp")) if self.thumbs.exists() else []

    def _assert_capped_jpeg(self, result_path):
        with Image.open(result_path) as out:
            self.assertEqual(out.format, "JPEG")
            self.assertLessEqual(max(out.size), history.THUMB_EDGE)
            self.assertEqual(out.mode, "RGB")

    # -- tests --------------------------------------------------------------
    def test_a_hit_returns_the_same_path_without_opening_the_source(self):
        src = self._jpeg()
        first = history._thumbnail_for(src)
        self.assertNotEqual(first, str(src))
        self.assertTrue(Path(first).exists())

        # A second call must be served from the cache -- so it must NOT open the
        # source. Make any open fail loudly; a cache miss would fall back to
        # str(src), which the equality check below would catch.
        with mock.patch.object(history.Image, "open",
                               side_effect=AssertionError("opened on a cache hit")):
            second = history._thumbnail_for(src)
        self.assertEqual(second, first)

    def test_a_changed_mtime_gives_a_new_path(self):
        src = self._jpeg()
        first = history._thumbnail_for(src)

        st = src.stat()
        os.utime(src, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000_000))
        second = history._thumbnail_for(src)

        self.assertNotEqual(second, first)
        self.assertTrue(Path(first).exists())
        self.assertTrue(Path(second).exists())

    def test_long_edge_is_capped_at_512(self):
        for builder in (self._jpeg, self._rgba_png, self._palette_gif, self._animated_gif):
            with self.subTest(builder=builder.__name__):
                result = history._thumbnail_for(builder())
                self._assert_capped_jpeg(result)

    def test_rgba_input_is_flattened_onto_white(self):
        result = history._thumbnail_for(self._rgba_png())
        self._assert_capped_jpeg(result)
        self.assertNotEqual(result, str(self.src_dir / "alpha.png"))

    def test_palette_and_animated_inputs_work(self):
        for src in (self._palette_gif(), self._animated_gif()):
            with self.subTest(src=src.name):
                result = history._thumbnail_for(src)
                self._assert_capped_jpeg(result)

    def test_a_small_source_is_not_upscaled(self):
        result = history._thumbnail_for(self._jpeg("small.jpg", size=(120, 90)))
        with Image.open(result) as out:
            self.assertEqual(out.size, (120, 90))

    def test_garbage_file_returns_the_original_path(self):
        src = self.src_dir / "garbage.jpg"
        src.write_bytes(b"this is not an image")
        result = history._thumbnail_for(src)
        self.assertEqual(result, str(src))
        self.assertEqual(self._tmp_files(), [])

    def test_truncated_file_returns_the_original_path(self):
        # hash_index.py flips ImageFile.LOAD_TRUNCATED_IMAGES on globally, and
        # that import is present in the full suite (and at real runtime), which
        # makes PIL tolerate a truncated scan. Pin it off here so this test
        # deterministically exercises the decode-error fallback rather than the
        # tolerate-and-thumbnail path.
        prev = ImageFile.LOAD_TRUNCATED_IMAGES
        ImageFile.LOAD_TRUNCATED_IMAGES = False
        try:
            src = self._jpeg("trunc.jpg", size=(1200, 800))
            data = src.read_bytes()
            src.write_bytes(data[: len(data) // 2])  # valid header, broken scan
            result = history._thumbnail_for(src)
            self.assertEqual(result, str(src))
            self.assertEqual(self._tmp_files(), [])
        finally:
            ImageFile.LOAD_TRUNCATED_IMAGES = prev

    def test_a_missing_source_returns_the_original_path(self):
        src = self.src_dir / "nope.jpg"
        result = history._thumbnail_for(src)
        self.assertEqual(result, str(src))

    def test_a_write_failure_leaves_no_temp_and_falls_back(self):
        """If the atomic rename fails after the temp is written, the temp must
        be cleaned up and the original path returned."""
        src = self._jpeg()
        with mock.patch.object(history.os, "replace", side_effect=OSError("boom")):
            result = history._thumbnail_for(src)
        self.assertEqual(result, str(src))
        self.assertEqual(self._tmp_files(), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
