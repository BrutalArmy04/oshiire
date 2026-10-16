"""Tests for jobs.py -- the Phase B child-process runner.

No gradio here: jobs.py is pure Python. Script steps are tiny .py files written
into a temp dir and run with sys.executable, exactly as the app runs the real
pipelines.

    python -m unittest discover -s tests
    python tests/test_jobs.py
"""
import csv
import shutil
import sqlite3
import sys
import tempfile
import textwrap
import threading
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

import jobs
from jobs import Step
sys.path.insert(0, str(Path(__file__).resolve().parent))  # config_sandbox
from config_sandbox import isolated_config


def _app(name):
    """A pipeline script's absolute path in the app folder, as jobs builds it."""
    return str(jobs.REPO_ROOT / name)


def _script(dirpath: Path, name: str, body: str) -> str:
    """Write a dedented script and return its absolute path."""
    path = dirpath / name
    path.write_text(textwrap.dedent(body), encoding="utf-8")
    return str(path)


def _lines(events):
    return [text for kind, text in ((e[0], e[1]) for e in events) if kind == "line"]


class SummaryTest(unittest.TestCase):
    def test_parses_the_summary_line(self):
        self.assertEqual(jobs.parse_summary("OSHIIRE_SUMMARY moves=848 flagged=28"),
                         {"moves": 848, "flagged": 28})

    def test_ignores_everything_else(self):
        for line in ("", "would_move: 848", "OSHIIRE_SUMMARY moves=1",
                     "x OSHIIRE_SUMMARY moves=1 flagged=2",
                     "OSHIIRE_SUMMARY moves=a flagged=2"):
            self.assertIsNone(jobs.parse_summary(line), line)

    def test_archive_summary_line_round_trips(self):
        import archive
        rows = [{"outcome": "move"}, {"outcome": "move"}, {"outcome": "flag"},
                {"outcome": "copy"}, {"outcome": "missing_file"}]
        self.assertEqual(jobs.parse_summary(archive.summary_line(rows)),
                         {"moves": 2, "flagged": 1})
        self.assertEqual(archive.summary_line([]), "OSHIIRE_SUMMARY moves=0 flagged=0")


class UnsaveCountTest(unittest.TestCase):
    """The structured safe-to-unsave count the Free-up-space page reads."""

    def test_parses_the_count_line(self):
        self.assertEqual(jobs.parse_unsave_count("OSHIIRE_UNSAVE total=42"), {"safe": 42})
        self.assertEqual(jobs.parse_unsave_count("OSHIIRE_UNSAVE total=0"), {"safe": 0})

    def test_ignores_everything_else(self):
        for line in ("", "OSHIIRE_UNSAVE total=", "OSHIIRE_UNSAVE total=-1",
                     "OSHIIRE_UNSAVE total=1.5", "x OSHIIRE_UNSAVE total=1",
                     "OSHIIRE_SUMMARY moves=1 flagged=0"):
            self.assertIsNone(jobs.parse_unsave_count(line), line)

    def test_export_count_line_round_trips(self):
        import export_unsave_list
        self.assertEqual(jobs.parse_unsave_count(export_unsave_list.count_line(7)),
                         {"safe": 7})
        self.assertEqual(export_unsave_list.count_line(0), "OSHIIRE_UNSAVE total=0")


class ExportUnsaveStepsTest(unittest.TestCase):
    def test_single_read_only_step(self):
        steps = jobs.export_unsave_steps()
        self.assertEqual([(s.label, s.argv, s.on_fail) for s in steps],
                         [("Make the unsave list", [_app("export_unsave_list.py")], "stop")])


class RedactTest(unittest.TestCase):
    def test_masks_feed_session_token_and_cookie(self):
        self.assertEqual(
            jobs.redact("GET https://www.reddit.com/.rss?feed=ab12CD.ef&limit=25"),
            "GET https://www.reddit.com/.rss?feed=[redacted]&limit=25",
        )
        self.assertEqual(jobs.redact("reddit_session=aaa.bbb.ccc"), "reddit_session=[redacted]")
        self.assertEqual(jobs.redact("token_v2=zzz-www_123"), "token_v2=[redacted]")
        self.assertEqual(
            jobs.redact("Cookie: over18=1; reddit_session=SEKRET; token_v2=NOPE"),
            "Cookie: [redacted]",
        )

    def test_leaves_ordinary_lines_byte_identical(self):
        for line in ("downloaded=5 skipped=2",
                     "",
                     "Reviewing 3 of 40 -- Genshin Impact / Hu Tao",
                     "no secrets here: feedback=great, session started"):
            self.assertEqual(jobs.redact(line), line)


class RelabelForAppTest(unittest.TestCase):
    """relabel_for_app swaps a command-line-only hint for its in-app wording,
    for the job log shown in the app (never data/oshiire.log)."""

    CLI_LINE = "Dry-run only -- nothing was moved. Re-run with --apply to perform these moves."

    def test_dry_run_hint_is_rewritten_for_the_app(self):
        shown = jobs.relabel_for_app(self.CLI_LINE)
        self.assertNotEqual(shown, self.CLI_LINE)
        self.assertNotIn("--apply", shown)
        self.assertIn(
            "Dry run only — nothing was moved. To do these moves, open Archive "
            "apply from the home menu.",
            shown,
        )

    def test_ordinary_lines_pass_through_unchanged(self):
        for line in ("downloaded=5 skipped=2", "", "Moved 12 files."):
            self.assertEqual(jobs.relabel_for_app(line), line)


class RunStepsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="jobs-run-"))
        jobs.finish()  # start from a free lock

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)
        jobs.finish()

    def test_streamed_lines_arrive_in_order_already_redacted(self):
        script = _script(self.tmp, "emit.py", """
            import sys
            for i in range(5):
                print(f"line {i} feed=SECRET{i}")
                sys.stdout.flush()
        """)
        events = list(jobs.run_steps([Step("Emit", [script], "stop")],
                                     threading.Event(), False))
        self.assertEqual(_lines(events),
                         [f"line {i} feed=[redacted]" for i in range(5)])
        self.assertEqual(events[0], ("step", "Emit"))
        self.assertEqual(events[-1], ("done", True, ""))

    def test_non_ascii_child_output_survives_the_pipe(self):
        """A child printing non-ASCII to a pipe must not crash on Windows'
        cp1252 default: every child gets PYTHONIOENCODING=utf-8 and the reader
        decodes utf-8. The script deliberately does NOT reconfigure stdout."""
        script = _script(self.tmp, "kanji.py", """
            print("Oshiire 押入れ")
            print("Re:Zero — Émilia")
        """)
        events = list(jobs.run_steps([Step("Kanji", [script], "stop")],
                                     threading.Event(), False))
        self.assertEqual(_lines(events), ["Oshiire 押入れ", "Re:Zero — Émilia"])
        self.assertEqual(events[-1], ("done", True, ""))

    def test_child_env_forces_utf8_and_unbuffered(self):
        env = jobs._child_env()
        self.assertEqual(env["PYTHONIOENCODING"], "utf-8")
        self.assertEqual(env["PYTHONUNBUFFERED"], "1")

    def test_stop_ends_the_run_and_later_steps_never_start(self):
        failing = _script(self.tmp, "fail.py", "import sys; print('boom'); sys.exit(3)")
        later = _script(self.tmp, "later.py", "print('SHOULD NOT RUN')")
        events = list(jobs.run_steps(
            [Step("Fail", [failing], "stop"), Step("Later", [later], "stop")],
            threading.Event(), False))
        self.assertEqual(events[-1], ("done", False, "Fail failed (exit 3) -- see the log"))
        self.assertNotIn(("step", "Later"), events)
        self.assertNotIn("SHOULD NOT RUN", _lines(events))

    def test_continue_warns_and_goes_on(self):
        failing = _script(self.tmp, "fail.py", "import sys; print('boom'); sys.exit(4)")
        later = _script(self.tmp, "later.py", "print('ran later')")
        events = list(jobs.run_steps(
            [Step("Fail", [failing], "continue"), Step("Later", [later], "stop")],
            threading.Event(), False))
        self.assertIn(("step", "Later"), events)
        self.assertIn("ran later", _lines(events))
        self.assertTrue(any("continuing" in line for line in _lines(events)))
        self.assertEqual(events[-1], ("done", True, ""))

    def test_cancel_terminates_a_cancellable_step(self):
        longs = _script(self.tmp, "long.py", """
            import sys, time
            for i in range(100):
                print(f"tick {i}"); sys.stdout.flush(); time.sleep(0.1)
        """)
        cancel = threading.Event()
        gen = jobs.run_steps([Step("Long", [longs], "stop")], cancel, True)
        self.assertEqual(next(gen), ("step", "Long"))
        next(gen)  # at least one tick
        cancel.set()
        rest = list(gen)
        self.assertEqual(rest[-1], ("done", False, "Cancelled"))
        self.assertIsNone(jobs.running())

    def test_cancel_is_ignored_when_not_cancellable(self):
        short = _script(self.tmp, "short.py", """
            import sys, time
            for i in range(3):
                print(f"tick {i}"); sys.stdout.flush(); time.sleep(0.1)
        """)
        cancel = threading.Event()
        cancel.set()  # already requested -- must be ignored
        events = list(jobs.run_steps([Step("Short", [short], "stop")], cancel, False))
        self.assertEqual(events[-1], ("done", True, ""))
        self.assertIn("tick 2", _lines(events))


class LockTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="jobs-lock-"))
        jobs.finish()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)
        jobs.finish()

    def test_second_try_start_fails_while_one_runs(self):
        self.assertTrue(jobs.try_start("first"))
        self.assertFalse(jobs.try_start("second"))
        self.assertEqual(jobs.running(), "first")
        jobs.finish()
        self.assertIsNone(jobs.running())
        self.assertTrue(jobs.try_start("again"))
        jobs.finish()

    def test_run_steps_does_not_touch_the_lock(self):
        """run_steps neither acquires nor releases the lock: the caller owns it
        and keeps it held across the post-job reload."""
        script = _script(self.tmp, "ok.py", "print('ok')")
        self.assertTrue(jobs.try_start("x"))
        list(jobs.run_steps([Step("Ok", [script], "stop")], threading.Event(), False))
        self.assertEqual(jobs.running(), "x")  # still held -- run_steps left it alone
        jobs.finish()


class SecretsTest(unittest.TestCase):
    """_secrets_from_env + the literal-secret arm of redact, against a sandboxed
    REPO_ROOT/.env."""

    def setUp(self):
        # The sandbox is the DATA ROOT (cwd), so its .env is the data-root .env;
        # isolated_config hides the real app-folder .env and process env.
        self.tmp = Path(tempfile.mkdtemp(prefix="jobs-secrets-"))
        self._sandbox = isolated_config(self.tmp)
        self._sandbox.__enter__()

    def tearDown(self):
        self._sandbox.__exit__(None, None, None)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _write_env(self, feed_url, cookie):
        (self.tmp / ".env").write_text(
            f"REDDIT_SAVED_FEED_URL={feed_url}\nREDDIT_SESSION_COOKIE={cookie}\n",
            encoding="utf-8")

    def test_secrets_from_env(self):
        self._write_env(
            "https://www.reddit.com/saved.rss?feed=FEEDtoken_longvalue123&user=me",
            "reddit_session=SESSIONlongvalue456; loid=abcdefgh12345; ab=sh",
        )
        secrets = jobs._secrets_from_env()
        self.assertIn("FEEDtoken_longvalue123", secrets)   # feed= query token
        self.assertIn("SESSIONlongvalue456", secrets)      # a cookie value
        self.assertIn("abcdefgh12345", secrets)            # another cookie value
        self.assertNotIn("sh", secrets)                    # 'ab=sh' value < 8 chars -> dropped

    def test_redact_masks_literal_secrets(self):
        self._write_env(
            "https://www.reddit.com/saved.rss?feed=FEEDtoken_longvalue123",
            "loid=abcdefgh12345; ab=sh",
        )
        secrets = jobs._secrets_from_env()
        line = ("saved GET using FEEDtoken_longvalue123 with cookie abcdefgh12345 "
                "and short ab=sh preserved")
        out = jobs.redact(line, secrets)
        self.assertNotIn("FEEDtoken_longvalue123", out)   # feed token mid-line masked
        self.assertNotIn("abcdefgh12345", out)            # cookie value masked
        self.assertIn("sh preserved", out)                # short value not a secret
        self.assertEqual(out.count("[redacted]"), 2)


class AvailabilityTest(unittest.TestCase):
    """ingest_available / backfill_available against a sandboxed REPO_ROOT.

    jobs reads .env fresh via dotenv_values(REPO_ROOT/.env) and resolves the CSV,
    index DB and cursor relative to REPO_ROOT, so pointing REPO_ROOT at a temp
    tree exercises every reason branch without touching the real project.
    """
    FEED_SECRET = "FEEDTOKEN_do_not_leak_abc123"
    COOKIE_SECRET = "COOKIE_do_not_leak_xyz789"

    def setUp(self):
        # The sandbox is the DATA ROOT: jobs resolves data/ against the cwd and
        # reads settings through config (data-root .env first).
        self.tmp = Path(tempfile.mkdtemp(prefix="jobs-avail-"))
        (self.tmp / "data" / "backfill").mkdir(parents=True)
        self._sandbox = isolated_config(self.tmp)
        self._sandbox.__enter__()

    def tearDown(self):
        self._sandbox.__exit__(None, None, None)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _write_env(self, **values):
        lines = [f"{k}={v}" for k, v in values.items()]
        (self.tmp / ".env").write_text("\n".join(lines) + "\n", encoding="utf-8")

    def _write_csv(self, n_rows):
        with (self.tmp / "data" / "saved_posts.csv").open("w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["id", "permalink"])
            for i in range(n_rows):
                w.writerow([f"abc{i}", f"https://reddit.com/{i}"])

    def _write_db(self):
        # Any file at the default --db path counts as "index exists".
        con = sqlite3.connect(self.tmp / "data" / "archive_index.db")
        con.close()

    def _write_state(self, next_row):
        import json
        (self.tmp / "data" / "backfill" / "state.json").write_text(
            json.dumps({"next_row": next_row}), encoding="utf-8")

    def _assert_no_secret(self, reason):
        self.assertNotIn(self.FEED_SECRET, reason)
        self.assertNotIn(self.COOKIE_SECRET, reason)

    # -- ingest --------------------------------------------------------------
    def test_ingest_needs_feed_url(self):
        self._write_env(REDDIT_SAVED_FEED_URL="")
        ok, reason = jobs.ingest_available()
        self.assertFalse(ok)
        self.assertTrue(reason)
        self._assert_no_secret(reason)

    def test_ingest_ok_when_feed_set(self):
        self._write_env(REDDIT_SAVED_FEED_URL=self.FEED_SECRET)
        ok, reason = jobs.ingest_available()
        self.assertTrue(ok)
        self.assertEqual(reason, "")

    # -- backfill, each reason branch in order -------------------------------
    def test_backfill_missing_csv(self):
        self._write_env(REDDIT_SESSION_COOKIE=self.COOKIE_SECRET)
        ok, reason = jobs.backfill_available()
        self.assertFalse(ok)
        self.assertIn("saved_posts.csv", reason)
        self._assert_no_secret(reason)

    def test_backfill_missing_index(self):
        self._write_env(REDDIT_SESSION_COOKIE=self.COOKIE_SECRET)
        self._write_csv(10)
        ok, reason = jobs.backfill_available()
        self.assertFalse(ok)
        self.assertIn("index", reason)
        self._assert_no_secret(reason)

    def test_backfill_missing_cookie(self):
        self._write_env(REDDIT_SESSION_COOKIE="")
        self._write_csv(10)
        self._write_db()
        ok, reason = jobs.backfill_available()
        self.assertFalse(ok)
        self.assertIn("login", reason)
        self._assert_no_secret(reason)

    def test_backfill_complete(self):
        self._write_env(REDDIT_SESSION_COOKIE=self.COOKIE_SECRET)
        self._write_csv(10)
        self._write_db()
        self._write_state(10)  # next_row == row count
        ok, reason = jobs.backfill_available()
        self.assertFalse(ok)
        self.assertIn("complete", reason.lower())
        self.assertIn("10", reason)
        self._assert_no_secret(reason)

    def test_backfill_complete_singular(self):
        self._write_env(REDDIT_SESSION_COOKIE=self.COOKIE_SECRET)
        self._write_csv(1)
        self._write_db()
        self._write_state(1)
        ok, reason = jobs.backfill_available()
        self.assertFalse(ok)
        self.assertEqual(reason, "Backfill complete: the 1 saved post was swept.")
        self._write_csv(10)
        self._write_state(10)
        self.assertEqual(jobs.backfill_available()[1],
                         "Backfill complete: all 10 saved posts swept.")

    def test_backfill_ok(self):
        self._write_env(REDDIT_SESSION_COOKIE=self.COOKIE_SECRET)
        self._write_csv(10)
        self._write_db()
        self._write_state(3)  # work remains
        ok, reason = jobs.backfill_available()
        self.assertTrue(ok)
        self.assertEqual(reason, "")


class ArchiveApplyStepsTest(unittest.TestCase):
    """archive_apply_steps must bundle the two maintenance tails apply owns:
    they run at the end of Backfill too, but Backfill greys out once the sweep
    is complete while the unsave list goes stale exactly when apply archives new
    posts. The tails run on `continue` so a failing one never blocks the other
    or masks a successful apply."""

    def test_steps_are_apply_then_refresh_then_unsave_in_order(self):
        steps = jobs.archive_apply_steps()
        self.assertEqual(
            [(s.label, s.argv, s.on_fail) for s in steps],
            [
                ("Archive apply", [_app("archive.py"), "--apply"], "stop"),
                ("Refresh duplicate check", [_app("hash_index.py"), "build"], "continue"),
                ("Update unsave list", [_app("export_unsave_list.py")], "continue"),
            ],
        )


class DataRootJobsTest(unittest.TestCase):
    """Jobs in a split layout: the pipeline scripts live in the app folder, the
    data in the data root (the cwd). Every step must name its script by absolute
    path, each child must run IN the data root, and a secret that exists only in
    data/config.env must still be masked in the streamed output."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="jobs-dataroot-")).resolve()
        self.scripts = Path(tempfile.mkdtemp(prefix="jobs-scripts-"))
        self._sandbox = isolated_config(self.tmp)
        self._sandbox.__enter__()

    def tearDown(self):
        self._sandbox.__exit__(None, None, None)
        shutil.rmtree(self.tmp, ignore_errors=True)
        shutil.rmtree(self.scripts, ignore_errors=True)

    def test_every_pipeline_step_names_an_absolute_app_script(self):
        pipelines = [jobs.ingest_steps(), jobs.backfill_steps(5), jobs.export_unsave_steps(),
                     jobs.archive_dryrun_steps(), jobs.archive_apply_steps()]
        seen = 0
        for steps in pipelines:
            for step in steps:
                if step.argv is None:
                    continue  # the cooldown pause
                script = Path(step.argv[0])
                self.assertTrue(script.is_absolute(), step.argv)
                self.assertEqual(script.parent, jobs.REPO_ROOT, step.argv)
                self.assertTrue(script.is_file(), step.argv)
                seen += 1
        self.assertEqual(seen, 14)

    def test_child_runs_in_the_data_root(self):
        script = _script(self.scripts, "where.py", """
            import os
            print("CWD=" + os.getcwd())
        """)
        events = list(jobs.run_steps([Step("Where", [script], "stop")], threading.Event(), False))
        cwd_lines = [l for l in _lines(events) if l.startswith("CWD=")]
        self.assertEqual(len(cwd_lines), 1)
        self.assertEqual(Path(cwd_lines[0][4:]).resolve(), self.tmp)
        self.assertNotEqual(Path(cwd_lines[0][4:]).resolve(), jobs.REPO_ROOT)

    def test_secret_only_in_config_env_is_redacted_in_stream(self):
        import config
        secret = "CONFIGonlySECRETvalue123"
        config.save_setup({"REDDIT_SESSION_COOKIE": f"reddit_session={secret}"})
        self.assertFalse((self.tmp / ".env").exists())  # config.env is the ONLY source
        script = _script(self.scripts, "leak.py", f"""
            import os
            cookie = os.environ.get("REDDIT_SESSION_COOKIE", "")
            print("child saw " + str(len(cookie)) + " chars")
            print("raw " + cookie.split("=", 1)[-1])
            print("literal {secret} mid-line")
        """)
        events = list(jobs.run_steps([Step("Leak", [script], "stop")], threading.Event(), False))
        lines = _lines(events)
        # The child got the value from config.env through its environment...
        self.assertIn(f"child saw {len('reddit_session=' + secret)} chars", lines)
        # ...and no streamed line carries it.
        self.assertFalse(any(secret in l for l in lines), lines)
        self.assertIn("raw [redacted]", lines)
        self.assertIn("literal [redacted] mid-line", lines)


if __name__ == "__main__":
    unittest.main(verbosity=2)
