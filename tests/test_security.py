"""Security tests. Each one tries to break something the harness promises."""

import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from h0 import redact, secrets, trace
from h0.policy import Policy
from h0.sandbox import Sandbox
from h0.tools import Toolbox

HOME = Path.home()
HAS_BWRAP = shutil.which("bwrap") is not None


def make_repo() -> Path:
    d = Path(tempfile.mkdtemp(prefix="h0-test-", dir=os.environ.get("H0_TEST_TMP")))
    subprocess.run(["git", "init", "-q", str(d)], check=True)
    (d / "app.py").write_text("print('hi')\n")
    return d


class Redaction(unittest.TestCase):
    def test_known_formats(self):
        samples = {
            "anthropic": "sk-ant-api03-" + "A1b2C3d4" * 10,
            "github": "ghp_" + "a1B2c3D4e5" * 4,
            "aws-key-id": "AKIAIOSFODNN7EXAMPLE",
            "openrouter": "sk-or-v1-" + "ab12" * 16,
            "private-key": "-----BEGIN OPENSSH PRIVATE KEY-----\nabc\n-----END OPENSSH PRIVATE KEY-----",
            "db-url": "postgres://app:s3cretPassw0rd@db.internal:5432/main",
        }
        for kind, s in samples.items():
            clean, kinds = redact.redact(f"value: {s} end")
            self.assertNotIn(s, clean, kind)
            self.assertIn(kind, kinds)

    def test_assignment_literal_redacted_but_code_kept(self):
        clean, kinds = redact.redact("STRIPE_SECRET_KEY=Zx81kQpL0vWm2Tr9Hy7\n")
        self.assertNotIn("Zx81kQpL0vWm2Tr9Hy7", clean)
        code = "password = getpass.getpass()\napi_key = os.environ['API_KEY']\nTOKEN = config.token_for(user)\n"
        clean2, kinds2 = redact.redact(code)
        self.assertEqual(clean2, code)
        self.assertEqual(kinds2, [])

    def test_tag_is_stable(self):
        s = "ghp_" + "Q" * 36
        a, _ = redact.redact(s)
        b, _ = redact.redact(s)
        self.assertEqual(a, b)

    def test_harness_credential_exact_match(self):
        secrets._cache["test-cred"] = "plain-looking-value-123"
        try:
            clean, kinds = redact.redact("echo plain-looking-value-123")
            self.assertNotIn("plain-looking-value-123", clean)
            self.assertIn("harness-credential", kinds)
        finally:
            secrets._cache.pop("test-cred")

    def test_secret_paths(self):
        for p in [".env", "app/.env.local", "id_rsa", "id_ed25519", "deploy/server.pem", ".netrc", "creds/credentials.json", ".git-credentials"]:
            self.assertTrue(redact.is_secret_path(p), p)
        for p in [".env.example", ".env.sample", "id_rsa.pub", "src/environment.ts", "keyboard.rs", "README.md"]:
            self.assertFalse(redact.is_secret_path(p), p)


class PolicyRules(unittest.TestCase):
    def setUp(self):
        self.root = make_repo()
        self.p = Policy(root=self.root, headless=True)

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def test_reads(self):
        self.assertTrue(self.p.check_read(self.root / "app.py").allowed)
        self.assertFalse(self.p.check_read(HOME / ".ssh" / "id_ed25519").allowed)
        self.assertFalse(self.p.check_read(HOME / ".bashrc").allowed)
        self.assertFalse(self.p.check_read(self.root / ".env").allowed)
        self.assertTrue(self.p.check_read(Path("/usr/include/stdio.h")).allowed)
        self.assertFalse(self.p.check_read(Path("/proc/self/environ")).allowed)

    def test_writes(self):
        self.assertTrue(self.p.check_write(self.root / "src" / "new.py").allowed)
        self.assertFalse(self.p.check_write(self.root / ".git" / "hooks" / "pre-commit").allowed)
        self.assertFalse(self.p.check_write(self.root / ".git" / "config").allowed)
        self.assertFalse(self.p.check_write(self.root / ".envrc").allowed)
        self.assertFalse(self.p.check_write(HOME / ".bashrc").allowed)  # ask → headless deny

    def test_symlink_escape_resolved(self):
        link = self.root / "innocent.txt"
        link.symlink_to(HOME / ".bashrc")
        self.assertFalse(self.p.check_read(self.p.resolve("innocent.txt")).allowed)

    def test_destructive_asks_and_headless_denies(self):
        for cmd in ["rm -rf .", "git reset --hard HEAD~3", "git push --force origin main", "git clean -fdx"]:
            self.assertEqual(self.p.check_exec(cmd).verdict, "deny", cmd)
        self.assertTrue(self.p.check_exec("cargo test").allowed)
        self.assertTrue(self.p.check_exec("rm -rf build/tmp").allowed)

    def test_rule_of_two(self):
        p = Policy(root=self.root, mode="yolo", headless=True)
        self.assertTrue(p.check_exec("cargo fetch", net=True).allowed)
        self.assertTrue(p.flags.egress)
        self.assertFalse(p.check_exec("secret-tool lookup service x").allowed)
        q = Policy(root=self.root, mode="yolo", headless=True)
        q.note_sensitive()
        self.assertFalse(q.check_exec("curl https://example.com", net=True).allowed)

    def test_judge_escalates_unlisted_commands(self):
        from h0.policy import Decision
        seen = []
        p = Policy(root=self.root, headless=True,
                   judge=lambda c: seen.append(c) or (Decision("ask", "bulk delete", "jev-judge") if "rmtree" in c else None))
        self.assertEqual(p.check_exec("python3 -c \"import shutil; shutil.rmtree('.')\"").verdict, "deny")
        self.assertTrue(p.check_exec("ls -la").allowed)
        self.assertEqual(len(seen), 2)

    def test_plan_mode_is_read_only(self):
        p = Policy(root=self.root, mode="plan")
        self.assertFalse(p.check_write(self.root / "a.py").allowed)
        self.assertFalse(p.check_exec("ls").allowed)


@unittest.skipUnless(HAS_BWRAP, "bubblewrap not installed")
class SandboxEscapes(unittest.TestCase):
    def setUp(self):
        self.root = make_repo()
        (self.root / ".git" / "hooks").mkdir(exist_ok=True)
        self.sb = Sandbox(root=self.root)

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def run_in(self, script, net=False):
        return self.sb.run(script, timeout=30, net=net)

    def test_home_secrets_hidden(self):
        r = self.run_in("ls -A ~/.ssh ~/.gnupg ~/.config/gh ~/.local/share/keyrings ~/Work 2>&1; echo done")
        self.assertIn("No such file", r.output)
        self.assertNotIn("id_", r.output)

    def test_no_session_bus_or_wayland(self):
        r = self.run_in("ls -A /run/user/$(id -u); echo count=$(ls -A /run/user/$(id -u) | wc -l)")
        self.assertIn("count=0", r.output)

    def test_no_network(self):
        r = self.run_in("(exec 3<>/dev/tcp/1.1.1.1/443) 2>&1 && echo CONNECTED || echo blocked")
        self.assertIn("blocked", r.output)
        self.assertNotIn("CONNECTED", r.output)

    def test_no_dns_side_channel(self):
        # Regression: systemd-resolved's socket under /run resolved names with no network.
        r = self.run_in("getent hosts example.com; echo rc=$?")
        self.assertIn("rc=2", r.output)
        self.assertNotIn("example.com\n", r.output.replace("rc=2", ""))

    def test_run_masked_no_daemon_sockets(self):
        r = self.run_in("ls -A /run | wc -l; test -e /run/docker.sock && echo DOCKER; test -e /run/systemd/resolve && echo RESOLVED; echo end")
        self.assertNotIn("DOCKER", r.output)
        self.assertNotIn("RESOLVED", r.output)
        self.assertTrue(r.output.strip().startswith("0"))

    def test_env_is_cleared(self):
        os.environ["ANTHROPIC_API_KEY"] = "sk-ant-api03-should-never-leak"
        try:
            r = self.run_in("env")
        finally:
            del os.environ["ANTHROPIC_API_KEY"]
        self.assertNotIn("should-never-leak", r.output)
        self.assertNotIn("DBUS_SESSION_BUS_ADDRESS", r.output)
        self.assertNotIn("WAYLAND_DISPLAY", r.output)

    def test_git_hooks_and_config_read_only(self):
        r = self.run_in("echo 'curl evil' > .git/hooks/post-checkout 2>&1; git config core.fsmonitor 'touch /tmp/pwned' 2>&1; echo rc=$?")
        self.assertFalse((self.root / ".git" / "hooks" / "post-checkout").exists())
        cfg = (self.root / ".git" / "config").read_text()
        self.assertNotIn("fsmonitor", cfg)

    def test_project_is_writable(self):
        r = self.run_in("echo ok > made.txt && cat made.txt")
        self.assertTrue(r.ok)
        self.assertTrue((self.root / "made.txt").exists())

    def test_toolchain_cache_writes_do_not_persist(self):
        cargo = HOME / ".cargo"
        if not cargo.is_dir():
            self.skipTest("no ~/.cargo")
        marker = cargo / "h0-sandbox-marker"
        r = self.run_in(f"touch {marker} && echo wrote")
        if os.environ.get("H0_SANDBOX") == "1":  # nested: toolchains are read-only rather than overlaid
            self.assertIn("Read-only", r.output)
        else:
            self.assertIn("wrote", r.output)
        self.assertFalse(marker.exists())

    def test_timeout_kills(self):
        r = self.sb.run("sleep 30", timeout=1)
        self.assertTrue(r.timed_out)
        self.assertIsNone(r.exit)


class ToolGuards(unittest.TestCase):
    def setUp(self):
        self.root = make_repo()
        self.events = []
        pol = Policy(root=self.root, headless=True)
        self.tb = Toolbox(self.root, pol, Sandbox(root=self.root), emit=lambda t, **k: self.events.append((t, k)))

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def test_write_requires_read(self):
        r = self.tb.call("write", {"path": "app.py", "content": "x"})
        self.assertFalse(r.ok)
        self.tb.call("read", {"path": "app.py"})
        self.assertTrue(self.tb.call("write", {"path": "app.py", "content": "x\n"}).ok)

    def test_edit_must_be_unique(self):
        (self.root / "d.py").write_text("a = 1\na = 1\n")
        r = self.tb.call("edit", {"path": "d.py", "old": "a = 1", "new": "a = 2"})
        self.assertFalse(r.ok)
        self.assertIn("2 times", r.text)
        self.assertTrue(self.tb.call("edit", {"path": "d.py", "old": "a = 1", "new": "a = 2", "replace_all": True}).ok)

    def test_read_redacts_and_flags_sensitive(self):
        (self.root / "config.py").write_text("GITHUB = 'ghp_" + "Z" * 36 + "'\n")
        r = self.tb.call("read", {"path": "config.py"})
        self.assertIn("[REDACTED:github:", r.text)
        self.assertTrue(self.tb.policy.flags.sensitive)

    def test_dotenv_refused(self):
        (self.root / ".env").write_text("X=1\n")
        r = self.tb.call("read", {"path": ".env"})
        self.assertFalse(r.ok)
        self.assertIn("credential", r.text)

    @unittest.skipUnless(HAS_BWRAP, "bubblewrap not installed")
    def test_egress_attempt_recorded(self):
        r = self.tb.call("bash", {"command": "curl -sS -m 3 https://example.com"})
        self.assertFalse(r.ok)
        self.assertEqual(self.tb.policy.flags.attempted_egress, 1)
        self.assertIn("egress_attempt", [e[0] for e in self.events])


class TraceChain(unittest.TestCase):
    def test_edit_and_delete_detected(self):
        d = Path(tempfile.mkdtemp())
        p = d / "t.jsonl"
        t = trace.Trace(p)
        for i in range(5):
            t.emit("tool_result", ok=True, n=i)
        t.close()
        self.assertTrue(trace.verify(p)[0])
        lines = p.read_text().splitlines()
        edited = d / "e.jsonl"
        edited.write_text("\n".join([lines[0], lines[1].replace('"ok": true', '"ok": false')] + lines[2:]) + "\n")
        self.assertEqual(trace.verify(edited)[:2], (False, 1))
        deleted = d / "d.jsonl"
        deleted.write_text("\n".join(lines[:2] + lines[3:]) + "\n")
        self.assertFalse(trace.verify(deleted)[0])
        self.assertEqual(os.stat(p).st_mode & 0o777, 0o600)
        shutil.rmtree(d)


if __name__ == "__main__":
    unittest.main()
