"""Hash-chained session trace.

Each line is one event. Its hash covers the previous line's hash plus its own canonical
body, so an edit or deletion anywhere breaks every later link. That makes the trace
tamper-evident, not tamper-proof: someone who can rewrite the file can rewrite the
chain. The final hash of each session is also appended to anchors.log, a second place
an edit would have to reach.

The first event is host_metadata and the last is host_summary, matching the host
contract in tcballard/build-omarchy-themes (evals/harness/README.md).
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path

GENESIS = "0" * 64


def _canonical(event: dict) -> str:
    return json.dumps(event, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class Trace:
    def __init__(self, path: Path | None, echo=None):
        self.path = path
        self.seq = 0
        self.prev = GENESIS
        self.echo = echo  # optional callable(record) for streaming JSONL to stdout
        self._f = None
        if path is not None:
            path.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            self._f = os.fdopen(fd, "a", buffering=1, encoding="utf-8")

    def emit(self, type_: str, **data) -> dict:
        body = {"seq": self.seq, "ts": now_iso(), "type": type_, **data}
        digest = hashlib.sha256((self.prev + _canonical(body)).encode()).hexdigest()
        record = {**body, "prev": self.prev, "hash": digest}
        line = json.dumps(record, ensure_ascii=False)
        if self._f:
            self._f.write(line + "\n")
        if self.echo:
            self.echo(record)
        self.prev = digest
        self.seq += 1
        return record

    def close(self) -> None:
        if self._f:
            self._f.close()
            anchors = self.path.parent / "anchors.log"
            with open(anchors, "a", encoding="utf-8") as a:
                a.write(f"{int(time.time())} {self.path.name} {self.seq} {self.prev}\n")
            self._f = None


def verify(path: Path) -> tuple[bool, int | None, int]:
    """Return (ok, first_bad_seq, events). A truncated final line counts as bad."""
    prev = GENESIS
    n = 0
    with open(path, encoding="utf-8") as f:
        for line in f:
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                return False, n, n
            claimed, claimed_prev = rec.pop("hash", None), rec.pop("prev", None)
            if claimed_prev != prev:
                return False, rec.get("seq", n), n
            if hashlib.sha256((prev + _canonical(rec)).encode()).hexdigest() != claimed:
                return False, rec.get("seq", n), n
            prev = claimed
            n += 1
    return True, None, n
