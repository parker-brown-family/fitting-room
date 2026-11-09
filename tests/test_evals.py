"""The eval runner's own correctness: trials must not contaminate one another."""

import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from h0 import evals
from h0.evals import exclusive, shared_caches

ROOT = Path(__file__).resolve().parent.parent


def _spans(tasks, workers=2, hold=0.15):
    spans = {}
    lock = threading.Lock()

    def trial(i, task):
        with exclusive(task):
            start = time.monotonic()
            time.sleep(hold)
            end = time.monotonic()
        with lock:
            spans[i] = (start, end)

    with ThreadPoolExecutor(max_workers=workers) as ex:
        list(ex.map(lambda it: trial(*it), enumerate(tasks)))
    return [spans[i] for i in range(len(tasks))]


def _overlap(a, b):
    return a[0] < b[1] and b[0] < a[1]


class SharedBuildCaches(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._old = evals.LOCK_DIR
        evals.LOCK_DIR = Path(self._tmp.name)

    def tearDown(self):
        evals.LOCK_DIR = self._old
        self._tmp.cleanup()

    def test_trials_sharing_a_build_cache_never_overlap(self):
        # td-v1, 2026-11-09: two workspaces building into one cargo target ran each
        # other's binaries and decided two of eight trials.
        task = {"sandbox": {"rw_paths": ["~/.cache/h0/cargo-target/test-shared"]}}
        a, b = _spans([task, dict(task)])
        self.assertFalse(_overlap(a, b))

    def test_trials_with_their_own_caches_still_run_in_parallel(self):
        a, b = _spans([{"sandbox": {"rw_paths": ["/tmp/h0-test-cache-a"]}},
                       {"sandbox": {"rw_paths": ["/tmp/h0-test-cache-b"]}}])
        self.assertTrue(_overlap(a, b))

    def test_a_task_with_no_sandbox_takes_no_lock(self):
        self.assertEqual(exclusive({}).paths, [])
        a, b = _spans([{}, {}])
        self.assertTrue(_overlap(a, b))

    def test_the_same_cache_spelled_two_ways_is_one_lock(self):
        import os
        home = os.path.expanduser("~")
        self.assertEqual(shared_caches({"sandbox": {"rw_paths": ["~/x", home + "/x"]}}), [home + "/x"])
        self.assertEqual(exclusive({"sandbox": {"rw_paths": ["~/y"]}}).paths,
                         exclusive({"sandbox": {"rw_paths": [home + "/y"]}}).paths)

    def test_another_h0_process_waits_for_the_cache(self):
        # A second process (an eval beside a re-score) must not build into a held cache.
        task = {"sandbox": {"rw_paths": ["/tmp/h0-test-cache-xproc"]}}
        code = ("import sys, time; from pathlib import Path; from h0 import evals; "
                f"evals.LOCK_DIR = Path({str(evals.LOCK_DIR)!r}); "
                "t = time.monotonic(); "
                "e = evals.exclusive({'sandbox': {'rw_paths': ['/tmp/h0-test-cache-xproc']}}); e.__enter__(); "
                "print(time.monotonic() - t); e.__exit__(None, None, None)")
        with exclusive(task):
            child = subprocess.Popen([sys.executable, "-P", "-c", code], cwd=ROOT, stdout=subprocess.PIPE, text=True,
                                     env=dict(os.environ, PYTHONPATH=str(ROOT)))
            time.sleep(0.6)
        waited = float(child.communicate(timeout=20)[0])
        self.assertGreater(waited, 0.4)


class RetriedTrials(unittest.TestCase):
    def test_a_retry_replaces_the_unscored_row_it_retried(self):
        from h0.cmd_eval import summarize
        base = {"task": "t", "arm": "a", "model": "m", "trial": 1, "wall_s": 1.0}
        rows = [dict(base, passed=None), dict(base, passed=True), dict(base, task="u", passed=None)]
        (row,) = summarize(rows)["table"]
        self.assertEqual((row["scored"], row["unscored"]), (1, 1))  # u never scored; t's retry did


class WorkspacesHoldNoAnswer(unittest.TestCase):
    """2026-11-09: 26 of a mined suite's 168 trials read the task's own solution commit from git history."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        repo = Path(self._tmp.name) / "repo"
        repo.mkdir()
        g = lambda *a: subprocess.run(["git", "-C", str(repo), "-c", "user.email=t@t", "-c", "user.name=t", *a],
                                      check=True, capture_output=True, text=True)
        g("init", "-q")
        (repo / "src.py").write_text("ANSWER = 'old'\n")
        g("add", "-A"); g("commit", "-qm", "before")
        (repo / "src.py").write_text("ANSWER = 'the solution'\n")
        (repo / "added.py").write_text("NEW = 1\n")
        g("add", "-A"); g("commit", "-qm", "the fix")
        self.repo, self.commit = repo, g("rev-parse", "HEAD").stdout.strip()
        self._old = evals.WS_BASE
        evals.WS_BASE = Path(self._tmp.name) / "ws"

    def tearDown(self):
        evals.WS_BASE = self._old
        self._tmp.cleanup()

    def test_the_solution_is_nowhere_in_the_workspace(self):
        ws = evals.make_workspace(self.repo, self.commit, ["src.py", "added.py"], tag="t")
        self.assertEqual((ws / "src.py").read_text(), "ANSWER = 'old'\n")
        self.assertFalse((ws / "added.py").exists())
        log = subprocess.run(["git", "-C", str(ws), "log", "--all", "--oneline"], capture_output=True, text=True).stdout
        self.assertEqual(len(log.splitlines()), 1, log)
        found = subprocess.run(["git", "-C", str(ws), "grep", "-r", "the solution", "$(git rev-list --all)"], capture_output=True, text=True)
        self.assertNotIn("the solution", found.stdout)
        objs = subprocess.run(["git", "-C", str(ws), "count-objects", "-v"], capture_output=True, text=True).stdout
        self.assertNotIn("alternates", objs)
        self.assertFalse((ws / ".git" / "objects" / "info" / "alternates").exists())

    def test_the_oracle_still_restores_the_commit(self):
        task = {"id": "t", "setup": {"type": "git", "repo": str(self.repo), "commit": self.commit, "revert": ["src.py"]},
                "checks": [{"run": "grep -q 'the solution' src.py"}]}
        r = evals.validate_task(task, "git checkout HEAD~1 -- src.py")
        self.assertTrue(r["fails_untouched"], r)
        self.assertTrue(r["passes_with_solution"], r)


class TheJudgeDefault(unittest.TestCase):
    def test_on_unless_switched_off_and_installed(self):
        from h0 import judge
        installed = (judge.JEV_REPO / "jev" / "client.py").exists()
        old = os.environ.pop("H0_JUDGE", None)
        try:
            self.assertEqual(judge.enabled(None), installed)
            self.assertFalse(judge.enabled({"judge": "off"}))
            os.environ["H0_JUDGE"] = "off"
            self.assertFalse(judge.enabled(None))
        finally:
            os.environ.pop("H0_JUDGE", None)
            if old is not None:
                os.environ["H0_JUDGE"] = old


if __name__ == "__main__":
    unittest.main()
