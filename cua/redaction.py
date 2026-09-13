"""Redaction: sensitive values never reach disk unmasked.

Two mechanisms:
1. Registered secrets: any value marked sensitive (passwords, session tokens,
   values of sensitive input params) is registered at runtime and replaced
   wherever it appears in logged text.
2. Pattern scrubbing: defensive regexes for data shapes that count as PII in
   this domain (SSN, card numbers) in case something slips through.
"""

from __future__ import annotations

import re

MASK = "[REDACTED]"

_PATTERNS = [
    re.compile(r"\b\d{3}-\d{2}-\d{4}\b"),          # SSN
        # card-like: 13-19 contiguous digits, or 4x4 groups with consistent
    # separators. Avoids false positives on timestamp-shaped run ids.
    re.compile(r"\b\d{13,19}\b"),
    re.compile(r"\b\d{4}([ -])\d{4}\1\d{4}\1\d{4}\b"),         # card-like digit runs
    re.compile(r"(password\s*[=:]\s*)\S+", re.I),   # password=... in URLs/logs
]


class Redactor:
    def __init__(self) -> None:
        self._secrets: set[str] = set()

    def register(self, value: str) -> None:
        """Register a runtime secret. Empty or tiny values are ignored to
        avoid masking every occurrence of e.g. 'a'."""
        if value and len(value) >= 4:
            self._secrets.add(value)

    def scrub(self, text: str) -> str:
        for s in self._secrets:
            text = text.replace(s, MASK)
        for p in _PATTERNS:
            text = p.sub(lambda m: (m.group(1) + MASK) if m.lastindex else MASK, text)
        return text
