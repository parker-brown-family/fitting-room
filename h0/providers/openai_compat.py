"""OpenAI-compatible chat completions (ollama, llama.cpp server, vLLM, OpenRouter).

Translates the internal Anthropic-style transcript at this edge. When the server
reports no usage, tokens are estimated from characters and the turn says so.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
import uuid

from h0.providers import ProviderError, Turn


def _text(content) -> str:
    if isinstance(content, str):
        return content
    return "\n".join(b.get("text", "") for b in content if b.get("type") == "text")


def to_openai(system: str, messages: list[dict]) -> list[dict]:
    out = [{"role": "system", "content": system}]
    for m in messages:
        c = m["content"]
        if m["role"] == "user":
            if isinstance(c, str):
                out.append({"role": "user", "content": c})
                continue
            for b in c:
                if b.get("type") == "tool_result":
                    body = b.get("content")
                    out.append({"role": "tool", "tool_call_id": b["tool_use_id"],
                                "content": body if isinstance(body, str) else _text(body)})
            text = _text([b for b in c if b.get("type") == "text"])
            if text:
                out.append({"role": "user", "content": text})
        else:
            msg = {"role": "assistant", "content": _text(c if isinstance(c, list) else [{"type": "text", "text": c}]) or None}
            calls = [{"id": b["id"], "type": "function",
                      "function": {"name": b["name"], "arguments": json.dumps(b.get("input", {}))}}
                     for b in (c if isinstance(c, list) else []) if b.get("type") == "tool_use"]
            if calls:
                msg["tool_calls"] = calls
            out.append(msg)
    return out


class OpenAICompat:
    name = "openai"

    def __init__(self, base_url: str, key: str | None = None):
        self.url = base_url.rstrip("/") + "/chat/completions"
        self._key = key

    def complete(self, system, messages, tools, settings, on_text=None, on_thinking=None) -> Turn:
        body = {
            "model": settings.model, "stream": True, "stream_options": {"include_usage": True},
            "messages": to_openai(system, messages), "max_tokens": settings.max_tokens,
        }
        if tools:
            body["tools"] = [{"type": "function", "function": {"name": t["name"], "description": t["description"],
                                                               "parameters": t["input_schema"]}} for t in tools]
        headers = {"content-type": "application/json", "accept": "text/event-stream"}
        if self._key:
            headers["authorization"] = "Bearer " + self._key
        req = urllib.request.Request(self.url, data=json.dumps(body).encode(), headers=headers, method="POST")
        started = time.monotonic()
        ttft = None
        text, thinking = "", ""
        calls: dict[int, dict] = {}
        usage: dict = {}
        stop = None
        try:
            resp = urllib.request.urlopen(req, timeout=1800)
        except urllib.error.HTTPError as e:
            raise ProviderError(f"HTTP {e.code}: {e.read().decode(errors='replace')[:1000]}", e.code, e.code >= 500) from None
        except urllib.error.URLError as e:
            raise ProviderError(f"connection: {e}", None, True) from None
        served: str | None = None
        with resp:
            for raw in resp:
                line = raw.decode("utf-8", errors="replace").strip()
                if not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    break
                ev = json.loads(data)
                served = ev.get("model") or served
                if ev.get("usage"):
                    usage = ev["usage"]
                for ch in ev.get("choices", []):
                    d = ch.get("delta", {})
                    piece = d.get("content")
                    reasoning = d.get("reasoning") or d.get("reasoning_content")
                    if (piece or reasoning or d.get("tool_calls")) and ttft is None:
                        ttft = int((time.monotonic() - started) * 1000)
                    if reasoning:
                        thinking += reasoning
                        if on_thinking:
                            on_thinking(reasoning)
                    if piece:
                        text += piece
                        if on_text:
                            on_text(piece)
                    for tc in d.get("tool_calls") or []:
                        slot = calls.setdefault(tc.get("index", len(calls)), {"id": None, "name": "", "args": ""})
                        slot["id"] = tc.get("id") or slot["id"]
                        fn = tc.get("function") or {}
                        slot["name"] += fn.get("name") or ""
                        slot["args"] += fn.get("arguments") or ""
                    if ch.get("finish_reason"):
                        stop = ch["finish_reason"]
        blocks: list[dict] = []
        if thinking:
            blocks.append({"type": "thinking", "thinking": thinking, "local": True})
        if text.strip():
            blocks.append({"type": "text", "text": text})
        for i in sorted(calls):
            c = calls[i]
            try:
                args = json.loads(c["args"]) if c["args"].strip() else {}
            except json.JSONDecodeError:
                args = {"_unparsed": c["args"]}
            blocks.append({"type": "tool_use", "id": c["id"] or "call_" + uuid.uuid4().hex[:12], "name": c["name"], "input": args})
        estimated = not usage
        if estimated:
            prompt_chars = len(json.dumps(body["messages"]))
            usage = {"prompt_tokens": prompt_chars // 4, "completion_tokens": (len(text) + len(thinking) + sum(len(c["args"]) for c in calls.values())) // 4}
        norm = {"input_tokens": usage.get("prompt_tokens", 0), "output_tokens": usage.get("completion_tokens", 0),
                "cache_read_input_tokens": (usage.get("prompt_tokens_details") or {}).get("cached_tokens", 0)}
        norm["input_tokens"] -= norm["cache_read_input_tokens"]
        stop_reason = {"tool_calls": "tool_use", "stop": "end_turn", "length": "max_tokens"}.get(stop, stop)
        if calls and stop_reason != "tool_use":
            stop_reason = "tool_use"
        return Turn(blocks=blocks, stop_reason=stop_reason, usage=norm, usage_estimated=estimated, model=settings.model,
                    served_model=served, ttft_ms=ttft, total_ms=int((time.monotonic() - started) * 1000))
