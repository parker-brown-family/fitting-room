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


@dataclass
class Totals:
    input_tokens: int = 0
    cache_creation_input_tokens: int = 0
    cache_read_input_tokens: int = 0
    output_tokens: int = 0
    thinking_tokens: int = 0
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
        for k in ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens", "output_tokens", "thinking_tokens"):
            setattr(self, k, getattr(self, k) + int(u.get(k, 0) or 0))
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

    def _over_budget(self) -> str | None:
        s = self.settings
        if s.budget_usd is not None and self.totals.usd is not None and self.totals.usd >= s.budget_usd:
            return "budget"
        return None

    def run(self, prompt: str) -> dict:
        started = time.monotonic()
        self.messages.append({"role": "user", "content": prompt})
        self.trace.emit("user", text=prompt)
        final_text, stop, verify_rounds, error = "", "max_turns", 0, None
        empty_nudged = False
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
                stop = "end_turn" if turn.stop_reason in ("end_turn", "stop_sequence", None) else turn.stop_reason
                break
            results = []
            for tu in uses:
                name, args = tu["name"], tu.get("input", {})
                self.ui.tool_start(name, args)
                self.trace.emit("tool_call", id=tu["id"], name=name, args=args)
                t0 = time.monotonic()
                blocked, notes = self.triggers.before(name, args)
                if blocked is not None:
                    from h0.tools import ToolResult
                    res = ToolResult(False, f"[harness rule] {blocked}", {"trigger_blocked": True})
                else:
                    res = self.toolbox.call(name, args)
                    notes += self.triggers.after(name, args)
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
            self.messages.append({"role": "user", "content": results})
        wall = time.monotonic() - started
        summary = {"stop": stop, "final": final_text, "error": error, "wall_s": round(wall, 1),
                   "totals": self.totals.as_dict(), "flags": self.policy.flags.as_dict()}
        return summary
