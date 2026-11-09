"""A scripted provider for tests: each call returns the next scripted turn."""

from __future__ import annotations

from h0.providers import Turn


class Fake:
    name = "fake"

    def __init__(self, script, served_model: str | None = None):
        self.script = list(script)
        self.served_model = served_model
        self.calls: list[dict] = []

    def complete(self, system, messages, tools, settings, on_text=None, on_thinking=None) -> Turn:
        self.calls.append({"system": system, "messages": [dict(m) for m in messages], "tools": tools})
        step = self.script.pop(0) if self.script else [{"type": "text", "text": "done"}]
        if callable(step):
            step = step(messages)
        for b in step:
            if b.get("type") == "text" and on_text:
                on_text(b["text"])
        stop = "tool_use" if any(b.get("type") == "tool_use" for b in step) else "end_turn"
        return Turn(blocks=step, stop_reason=stop, usage={"input_tokens": 100, "output_tokens": 20}, model=settings.model,
                    served_model=self.served_model)
