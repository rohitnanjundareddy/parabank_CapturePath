"""Evidence: structured JSONL log of what the system did and why, plus
screenshots on failure. Every write passes through the redactor, so
sensitive values are masked before touching disk, not after.
"""

from __future__ import annotations

import json
import os
import uuid
from datetime import datetime, timezone

from .redaction import Redactor


class EvidenceLog:
    def __init__(self, base_dir: str, run_kind: str, redactor: Redactor):
        self.run_id = f"{run_kind}-{datetime.now(timezone.utc):%Y%m%d-%H%M%S}-{uuid.uuid4().hex[:6]}"
        self.dir = os.path.join(base_dir, self.run_id)
        os.makedirs(self.dir, exist_ok=True)
        self._redactor = redactor
        self._path = os.path.join(self.dir, "events.jsonl")

    def event(self, kind: str, **fields) -> None:
        record = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "kind": kind,
            **fields,
        }
        line = self._redactor.scrub(json.dumps(record, default=str))
        with open(self._path, "a") as f:
            f.write(line + "\n")

    def screenshot_path(self, label: str) -> str:
        safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in label)
        return os.path.join(self.dir, f"{safe}.png")
