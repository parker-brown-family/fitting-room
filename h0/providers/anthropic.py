"""Anthropic Messages API over urllib, streaming.

Request shape follows platform.claude.com docs read on 2026-11-08: top-level
`cache_control` for automatic prompt caching, `output_config.effort`, and
`thinking: {type: adaptive, display: summarized}`. Assistant content blocks, thinking
blocks and their signatures included, are returned unmodified so they can be sent back.
"""

from __future__ import annotations

import json
import random
import socket
import time
import urllib.error
import urllib.request

from h0.providers import ProviderError, Turn

VERSION = "2023-06-01"
RETRY_STATUS = {408, 409, 429, 500, 502, 503, 504, 529}


class Anthropic:
    name = "anthropic"

    def __init__(self, key: str, base_url: str | None = None):
        self._key = key
        self.url = (base_url or "https://api.anthropic.com").rstrip("/") + "/v1/messages"
        self.thinking_ok: dict[str, bool] = {}

    def _body(self, system, messages, tools, s) -> dict:
        body = {
            "model": s.model, "max_tokens": s.max_tokens, "stream": True,
            "system": [{"type": "text", "text": system}], "messages": messages,
            "cache_control": {"type": "ephemeral"},
        }
        if tools:
            body["tools"] = tools
        if s.effort:
            body["output_config"] = {"effort": s.effort}
        if self.thinking_ok.get(s.model, True):
            body["thinking"] = {"type": "adaptive", "display": "summarized"}
        return body

    def complete(self, system, messages, tools, settings, on_text=None, on_thinking=None) -> Turn:
        attempt = 0
        while True:
            body = self._body(system, messages, tools, settings)
            try:
                turn = self._stream(body, on_text, on_thinking)
                turn.retries = attempt
                return turn
            except ProviderError as e:
                if e.status == 400 and "thinking" in str(e) and self.thinking_ok.get(settings.model, True):
                    self.thinking_ok[settings.model] = False
                    continue
                attempt += 1
                if not e.retryable or attempt > 6:
                    raise
                time.sleep(min(60, (2 ** attempt) + random.random() * 2))

    def _stream(self, body: dict, on_text, on_thinking) -> Turn:
        req = urllib.request.Request(self.url, data=json.dumps(body).encode(), method="POST", headers={
            "x-api-key": self._key, "anthropic-version": VERSION, "content-type": "application/json",
            "accept": "text/event-stream", "user-agent": "harness-0/0.1",
        })
        started = time.monotonic()
        ttft = None
        blocks: list[dict] = []
        partial: dict[int, str] = {}
        usage: dict = {}
        served: str | None = None
        stop = None
        try:
            resp = urllib.request.urlopen(req, timeout=900)
        except urllib.error.HTTPError as e:
            detail = e.read().decode(errors="replace")[:2000]
            retry_after = e.headers.get("retry-after") if e.headers else None
            if retry_after:
                try:
                    time.sleep(min(60, float(retry_after)))
                except ValueError:
                    pass
            raise ProviderError(f"HTTP {e.code}: {detail}", e.code, e.code in RETRY_STATUS) from None
        except (urllib.error.URLError, socket.timeout, ConnectionError) as e:
            raise ProviderError(f"connection: {e}", None, True) from None
        try:
            for raw in resp:
                line = raw.decode("utf-8", errors="replace").rstrip("\r\n")
                if not line.startswith("data:"):
                    continue
                ev = json.loads(line[5:].strip())
                t = ev.get("type")
                if t == "message_start":
                    usage.update({k: v for k, v in ev["message"].get("usage", {}).items() if isinstance(v, int)})
                    served = ev["message"].get("model") or served
                elif t == "content_block_start":
                    cb = dict(ev["content_block"])
                    if cb.get("type") == "tool_use":
                        cb["input"] = {}
                        partial[ev["index"]] = ""
                    blocks.append(cb)
                elif t == "content_block_delta":
                    d = ev["delta"]
                    b = blocks[ev["index"]]
                    dt = d.get("type")
                    if dt == "text_delta":
                        if ttft is None:
                            ttft = int((time.monotonic() - started) * 1000)
                        b["text"] = b.get("text", "") + d["text"]
                        if on_text:
                            on_text(d["text"])
                    elif dt == "thinking_delta":
                        if ttft is None:
                            ttft = int((time.monotonic() - started) * 1000)
                        b["thinking"] = b.get("thinking", "") + d["thinking"]
                        if on_thinking:
                            on_thinking(d["thinking"])
                    elif dt == "signature_delta":
                        b["signature"] = b.get("signature", "") + d["signature"]
                    elif dt == "input_json_delta":
                        if ttft is None:
                            ttft = int((time.monotonic() - started) * 1000)
                        partial[ev["index"]] += d.get("partial_json", "")
                elif t == "content_block_stop":
                    i = ev["index"]
                    if i in partial:
                        raw_json = partial.pop(i)
                        try:
                            blocks[i]["input"] = json.loads(raw_json) if raw_json.strip() else {}
                        except json.JSONDecodeError:
                            blocks[i]["input"] = {"_unparsed": raw_json}
                elif t == "message_delta":
                    stop = ev.get("delta", {}).get("stop_reason", stop)
                    u = ev.get("usage", {})
                    for k, v in u.items():
                        if isinstance(v, int):
                            usage[k] = v
                    details = u.get("output_tokens_details") or {}
                    if "thinking_tokens" in details:
                        usage["thinking_tokens"] = details["thinking_tokens"]
                elif t == "error":
                    err = ev.get("error", {})
                    kind = err.get("type", "")
                    raise ProviderError(f"stream error {kind}: {err.get('message', '')}", None,
                                        kind in {"overloaded_error", "api_error", "rate_limit_error"})
        except (socket.timeout, ConnectionError, urllib.error.URLError) as e:
            raise ProviderError(f"stream interrupted: {e}", None, True) from None
        finally:
            resp.close()
        for b in blocks:  # drop empty text blocks; the API rejects them on the way back
            if b.get("type") == "text" and not b.get("text"):
                b["_drop"] = True
        blocks = [b for b in blocks if not b.get("_drop")]
        return Turn(blocks=blocks, stop_reason=stop, usage=usage, model=body["model"], served_model=served, ttft_ms=ttft,
                    total_ms=int((time.monotonic() - started) * 1000))
