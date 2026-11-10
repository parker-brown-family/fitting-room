"""The agent loop.

One turn: send the transcript, stream the reply, run any tool calls through
triggers → policy → tool → redaction, append the results, repeat. The session ends
when the model stops calling tools and the fitting's verify check (if any) passes,
or when a budget runs out.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field

from h0 import config
from h0.fitting import Triggers
from h0.providers import ProviderError

CLEARED = "[cleared by the harness to save context; run the tool again if you need this]"
INTERRUPTED = "[harness] This call never ran: the session was interrupted before its result came back."


def repair(messages: list) -> int:
    """Give every tool_use a tool_result. An interrupt (Ctrl-C in the REPL, a crash, a session
    saved mid-turn and resumed) can leave an assistant turn whose calls have no results, and the
    API refuses the whole transcript from then on. Returns the number of results supplied."""
    added = 0
    i = 0
    while i < len(messages):
        m = messages[i]
        uses = [b["id"] for b in m["content"] if isinstance(b, dict) and b.get("type") == "tool_use"] \
            if m["role"] == "assistant" and isinstance(m["content"], list) else []
        if uses:
            nxt = messages[i + 1] if i + 1 < len(messages) else None
            if nxt is None or nxt["role"] != "user" or not isinstance(nxt["content"], list):
                nxt_content = [] if nxt is None or nxt["role"] != "user" else [{"type": "text", "text": nxt["content"]}]
                if nxt is not None and nxt["role"] == "user":
                    messages[i + 1] = {"role": "user", "content": nxt_content}
                else:
                    messages.insert(i + 1, {"role": "user", "content": nxt_content})
                nxt = messages[i + 1]
            have = {b.get("tool_use_id") for b in nxt["content"] if isinstance(b, dict) and b.get("type") == "tool_result"}
            missing = [{"type": "tool_result", "tool_use_id": u, "content": INTERRUPTED, "is_error": True}
                       for u in uses if u not in have]
            if missing:
                # tool results must come first in the user turn that answers the calls
                nxt["content"] = missing + nxt["content"]
                added += len(missing)
        i += 1
    return added


FINISH_REVIEW = ("[harness] Before you finish, re-read the request. List every behaviour it asks for or says it "
                 "expects, one line each, marked met or not met, with the evidence (a test or command you ran, and "
                 "its result). Anything not met that can be met here, do now. Stop only when every line is met or "
                 "you have said why it cannot be done in this environment.")


@dataclass
class Totals:
    input_tokens: int = 0
    cache_creation_input_tokens: int = 0
    cache_read_input_tokens: int = 0
    output_tokens: int = 0
    thinking_tokens: int | None = None   # None until a server reports it: the API often does not say
    usd: float | None = 0.0
    estimated: bool = False
    turns: int = 0
    sim_local_s: float = 0.0          # prefill of new tokens + decode, at the configured local speeds
    sim_local_nocache_s: float = 0.0  # same, but every prompt token prefilled every turn
    model_ms: int = 0
    tool_ms: int = 0
    retries: int = 0

    def add(self, turn, settings) -> None:
        u = turn.usage
        for k in ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens", "output_tokens"):
            setattr(self, k, getattr(self, k) + int(u.get(k, 0) or 0))
        if isinstance(u.get("thinking_tokens"), int):
            self.thinking_tokens = (self.thinking_tokens or 0) + u["thinking_tokens"]
        cost = config.price(turn.model, u)
        self.usd = None if (cost is None or self.usd is None) else self.usd + cost
        self.estimated = self.estimated or turn.usage_estimated
        self.turns += 1
        self.model_ms += turn.total_ms
        self.retries += turn.retries
        new = u.get("input_tokens", 0) + u.get("cache_creation_input_tokens", 0)
        allp = new + u.get("cache_read_input_tokens", 0)
        out = u.get("output_tokens", 0)
        self.sim_local_s += new / settings.sim_prefill_tps + out / settings.sim_decode_tps
        self.sim_local_nocache_s += allp / settings.sim_prefill_tps + out / settings.sim_decode_tps

    def as_dict(self) -> dict:
        d = dict(self.__dict__)
        d["usd"] = None if self.usd is None else round(self.usd, 6)
        d["sim_local_s"] = round(self.sim_local_s, 1)
        d["sim_local_nocache_s"] = round(self.sim_local_nocache_s, 1)
        return d


@dataclass
class Agent:
    settings: config.Settings
    provider: object
    toolbox: object
    policy: object
    trace: object
    ui: object
    system: str
    fitting: object = None
    messages: list = field(default_factory=list)
    totals: Totals = field(default_factory=Totals)

    def __post_init__(self) -> None:
        triggers = self.fitting.triggers if self.fitting else []
        self.triggers = Triggers(triggers, run_check=self._run_check, emit=self.trace.emit)
        self.repeats: dict[str, int] = {}
        self.pending_notes: list[str] = []   # harness facts the model must hear with the next prompt

    def undo(self) -> list[str]:
        files = self.toolbox.undo()
        if files:
            self.trace.emit("undo", files=files)
            self.pending_notes.append("[harness] The person undid the file changes from your previous request; these files are back "
                                      "as they were before it: " + ", ".join(files) + "." +
                                      ("" if self.toolbox.tree else " Changes made through bash were not undone."))
        return files

    def clear(self) -> None:
        self.messages = []
        self.repeats = {}
        self.pending_notes = []
        self.trace.emit("cleared")

    def _run_check(self, name: str) -> tuple[bool, str]:
        r = self.toolbox.call("check", {"name": name}) if "check" in self.toolbox.by_name else None
        if r is None:
            return True, "(no such check)"
        return r.ok, r.text

    def _clear_old_results(self, keep: int = 6) -> int:
        seen = 0
        cleared = 0
        for m in reversed(self.messages):
            if m["role"] != "user" or not isinstance(m["content"], list):
                continue
            for b in reversed(m["content"]):
                if b.get("type") != "tool_result":
                    continue
                seen += 1
                if seen > keep and b.get("content") != CLEARED:
                    b["content"] = CLEARED
                    cleared += 1
        return cleared

    def _budget_note(self, turn_no: int) -> str | None:
        """Say how many turns are left, twice: at two thirds of the limit and on the last turn with
        tools. A model that cannot see the limit spends it searching; in the local-model runs a
        third of plain trials ended at the limit with the right file found and nothing changed."""
        limit = self.settings.max_turns
        if limit < 9:
            return None
        if turn_no == (2 * limit) // 3:
            return (f"{turn_no} of {limit} turns used, {limit - turn_no} left. If you know which file to change, "
                    "change it now, then check it and finish.")
        if turn_no == limit - 1:
            return ("One turn left: the session ends after your next reply, so make it your answer, saying what you "
                    "changed and what is not done.")
        return None

    def _over_budget(self) -> str | None:
        s = self.settings
        if s.budget_usd is not None and self.totals.usd is not None and self.totals.usd >= s.budget_usd:
            return "budget"
        return None

    def run(self, prompt: str) -> dict:
        started = time.monotonic()
        if (n := repair(self.messages)):
            self.trace.emit("transcript_repaired", results=n)
        if self.pending_notes:
            prompt = "\n".join(self.pending_notes) + "\n\n" + prompt
            self.pending_notes = []
        if hasattr(self.toolbox, "checkpoint"):
            self.toolbox.checkpoint()
        self.messages.append({"role": "user", "content": prompt})
        self.trace.emit("user", text=prompt)
        final_text, stop, verify_rounds, error = "", "max_turns", 0, None
        empty_nudged = reviewed = False
        last_prompt_tokens = 0
        for turn_no in range(1, self.settings.max_turns + 1):
            if last_prompt_tokens > self.settings.context_clear_at:
                n = self._clear_old_results()
                if n:
                    self.trace.emit("context_cleared", results=n, prompt_tokens=last_prompt_tokens)
            try:
                turn = self.provider.complete(self.system, self.messages, self.toolbox.api(), self.settings,
                                              on_text=self.ui.text, on_thinking=self.ui.thinking)
            except ProviderError as e:
                error, stop = str(e), "error"
                self.trace.emit("error", where="provider", message=str(e)[:2000], status=e.status)
                break
            if turn.served_model and not turn.served_model.startswith(self.settings.model):
                # a score served by another model measures nothing: stop loudly, never count it
                error, stop = f"served by {turn.served_model}, asked for {self.settings.model}", "error"
                self.trace.emit("error", where="served-model", message=error, status=None)
                break
            self.totals.add(turn, self.settings)
            u = turn.usage
            last_prompt_tokens = u.get("input_tokens", 0) + u.get("cache_read_input_tokens", 0) + u.get("cache_creation_input_tokens", 0)
            self.trace.emit("assistant", turn=turn_no, stop_reason=turn.stop_reason, usage=u, usage_estimated=turn.usage_estimated,
                            ms=turn.total_ms, ttft_ms=turn.ttft_ms, retries=turn.retries, served_model=turn.served_model,
                            text="".join(b.get("text", "") for b in turn.blocks if b.get("type") == "text"),
                            thinking_chars=sum(len(b.get("thinking", "")) for b in turn.blocks if b.get("type") == "thinking"),
                            tool_calls=[b["name"] for b in turn.blocks if b.get("type") == "tool_use"])
            blocks = [b for b in turn.blocks if not (b.get("type") == "thinking" and b.get("local"))]
            if blocks:
                self.messages.append({"role": "assistant", "content": blocks})
            self.ui.turn_end(self.totals)
            if (why := self._over_budget()):
                stop = why
                break
            uses = [b for b in turn.blocks if b.get("type") == "tool_use"]
            if not uses:
                final_text = "".join(b.get("text", "") for b in turn.blocks if b.get("type") == "text")
                if turn.stop_reason == "max_tokens":
                    self.messages.append({"role": "user", "content": "You ran out of output space. Continue from where you stopped, more briefly."})
                    continue
                if not final_text.strip() and not empty_nudged:
                    # Small local models with thinking on sometimes end a turn with the answer
                    # only in their reasoning; a session never ends on an empty reply.
                    empty_nudged = True
                    self.trace.emit("empty_reply_nudge", turn=turn_no)
                    self.messages.append({"role": "user", "content": "[harness] You ended without a reply. Write your answer for the person now."})
                    continue
                if self.fitting and self.fitting.verify and verify_rounds < 2 and "check" in self.toolbox.by_name:
                    ok, out = self._run_check(self.fitting.verify)
                    self.trace.emit("verify", check=self.fitting.verify, ok=ok, round=verify_rounds + 1)
                    self.ui.note(f"verify `{self.fitting.verify}`: {'passed' if ok else 'failed'}")
                    if not ok:
                        verify_rounds += 1
                        self.messages.append({"role": "user", "content":
                            f"[harness] Before you finish, the project's `{self.fitting.verify}` check must pass. It failed:\n{out}\n"
                            "Fix the cause, or explain why it cannot pass."})
                        continue
                if self.settings.finish_review and not reviewed:
                    # 2026-11-10, the Omarchy issue ladder: an agent named an expectation the issue
                    # states, said it was not met, and stopped. One review at the first finish.
                    reviewed = True
                    self.trace.emit("finish_review", turn=turn_no)
                    self.messages.append({"role": "user", "content": FINISH_REVIEW})
                    continue
                stop = "end_turn" if turn.stop_reason in ("end_turn", "stop_sequence", None) else turn.stop_reason
                break
            results = []
            for tu in uses:
                name, args = tu["name"], tu.get("input", {})
                self.ui.tool_start(name, args)
                self.trace.emit("tool_call", id=tu["id"], name=name, args=args)
                t0 = time.monotonic()
                resolved = None
                if isinstance(args.get("path"), str) and name != "bash":
                    try:
                        resolved = str(self.policy.resolve(args["path"], self.toolbox.cwd))
                    except (OSError, ValueError):
                        resolved = None
                blocked, notes = self.triggers.before(name, args, resolved)
                if blocked is not None:
                    from h0.tools import ToolResult
                    res = ToolResult(False, f"[harness rule] {blocked}", {"trigger_blocked": True})
                else:
                    res = self.toolbox.call(name, args)
                    notes += self.triggers.after(name, args, resolved)
                ms = int((time.monotonic() - t0) * 1000)
                self.totals.tool_ms += ms
                key = name + json.dumps(args, sort_keys=True)
                self.repeats[key] = self.repeats.get(key, 0) + 1
                if self.repeats[key] == 3:
                    notes.append("You have made this exact call three times. Step back: re-read the goal and try a different approach.")
                text = res.text
                if notes:
                    text += "\n\n" + "\n".join(f"[harness note] {n}" for n in notes if n)
                self.trace.emit("tool_result", id=tu["id"], name=name, ok=res.ok, ms=ms, meta=res.meta, text=text[:20000])
                self.ui.tool_end(name, res, ms)
                results.append({"type": "tool_result", "tool_use_id": tu["id"], "content": text or "(no output)", "is_error": not res.ok})
            if (note := self._budget_note(turn_no)):
                results[-1]["content"] += f"\n\n[harness note] {note}"
                self.trace.emit("budget_note", turn=turn_no, note=note)
            self.messages.append({"role": "user", "content": results})
        wall = time.monotonic() - started
        summary = {"stop": stop, "final": final_text, "error": error, "wall_s": round(wall, 1),
                   "totals": self.totals.as_dict(), "flags": self.policy.flags.as_dict()}
        return summary
