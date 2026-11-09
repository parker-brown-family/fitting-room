"""Loop behaviour with the scripted provider: tool dispatch, triggers, verify-on-finish."""

import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from h0 import config
from h0.fitting import Fitting
from h0.loop import Agent
from h0.policy import Policy
from h0.providers.fake import Fake
from h0.sandbox import Sandbox
from h0.tools import Toolbox
from h0.trace import Trace


class QuietUI:
    def text(self, s): pass
    def thinking(self, s): pass
    def tool_start(self, n, a): pass
    def tool_end(self, n, r, ms): pass
    def note(self, s): pass
    def turn_end(self, t): pass
    def ask(self, w, d): return False


def tool(name, **inp):
    return {"type": "tool_use", "id": f"t_{name}_{len(inp)}_{abs(hash(str(inp))) % 10000}", "name": name, "input": inp}


class LoopTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="h0-loop-"))
        subprocess.run(["git", "init", "-q", str(self.root)], check=True)
        (self.root / "a.txt").write_text("hello\n")

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def agent(self, script, fitting=None):
        s = config.Settings(model="fake", headless=True)
        pol = Policy(root=self.root, headless=True)
        tb = Toolbox(self.root, pol, Sandbox(root=self.root), checks=(fitting.checks if fitting else {}))
        tr = Trace(None)
        return Agent(settings=s, provider=Fake(script), toolbox=tb, policy=pol, trace=tr, ui=QuietUI(), system="sys", fitting=fitting)

    def test_a_turn_served_by_another_model_stops_the_run(self):
        # a score served by another model measures nothing, so it must never be counted
        a = self.agent([[{"type": "text", "text": "hi"}]])
        a.provider.served_model = "claude-other-1"
        out = a.run("hello")
        self.assertEqual(out["stop"], "error")
        a2 = self.agent([[{"type": "text", "text": "hi"}]])
        a2.provider.served_model = a2.settings.model
        self.assertEqual(a2.run("hello")["stop"], "end_turn")

    def test_tool_round_trip(self):
        a = self.agent([[tool("read", path="a.txt")], [{"type": "text", "text": "it says hello"}]])
        out = a.run("what does a.txt say?")
        self.assertEqual(out["stop"], "end_turn")
        self.assertEqual(out["final"], "it says hello")
        results = a.messages[2]["content"]
        self.assertEqual(results[0]["type"], "tool_result")
        self.assertIn("hello", results[0]["content"])

    def test_trigger_block_and_inject(self):
        fit = Fitting(name="t", root=self.root, triggers=[
            {"on": "before:bash", "match": r"git\s+push", "action": "block", "note": "never push from h0"},
            {"on": "after:edit", "match": r"\.txt$", "action": "inject", "note": "text files are user-facing"},
        ])
        a = self.agent([[tool("bash", command="git push origin main")],
                        [tool("edit", path="a.txt", old="hello", new="hi")],
                        [{"type": "text", "text": "ok"}]], fit)
        a.run("go")
        push_result = a.messages[2]["content"][0]
        self.assertTrue(push_result["is_error"])
        self.assertIn("never push", push_result["content"])
        edit_result = a.messages[4]["content"][0]
        self.assertIn("text files are user-facing", edit_result["content"])

    def test_verify_on_finish_feeds_failure_back(self):
        fit = Fitting(name="t", root=self.root, checks={"ok": {"run": "grep -q fixed a.txt"}}, verify="ok")
        a = self.agent([[{"type": "text", "text": "done"}],
                        [tool("edit", path="a.txt", old="hello", new="fixed")],
                        [{"type": "text", "text": "really done"}]], fit)
        out = a.run("fix it")
        self.assertEqual(out["stop"], "end_turn")
        self.assertIn("check must pass", a.messages[2]["content"])
        self.assertEqual(out["final"], "really done")

    def test_empty_reply_is_nudged_once(self):
        a = self.agent([[{"type": "thinking", "thinking": "answer is 4", "local": True}], [{"type": "text", "text": "4"}]])
        out = a.run("2+2?")
        self.assertEqual(out["final"], "4")
        self.assertIn("ended without a reply", a.messages[-2]["content"])

    def test_repeat_warning(self):
        same = tool("bash", command="echo x")
        a = self.agent([[same], [same], [same], [{"type": "text", "text": "stop"}]])
        a.run("loop")
        third = a.messages[6]["content"][0]["content"]
        self.assertIn("three times", third)


if __name__ == "__main__":
    unittest.main()
