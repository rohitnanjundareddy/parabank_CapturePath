"""Text I/O choke point: every file this system reads or writes goes through
here, so a file means the same thing on Windows, macOS, and CI.

Python's `open()` defaults to the *locale* encoding, not UTF-8. On Windows
that is cp1252. Artifacts carry prose written by the recorder and by the
discovery model, and that prose contains em dashes: `recorder.py` writes
"only one locator candidate (css) - no fallback ..." with a real U+2014.
Encoded as cp1252 that is the single byte 0x97, which is not valid UTF-8, so
an artifact recorded on Windows raises UnicodeDecodeError the moment a mac
tries to replay it. The reverse direction fails too: a mac writes the three
UTF-8 bytes, and a Windows reader decodes them as mojibake.

Artifacts are committed, reviewed, and replayed on machines that are not the
one that recorded them. That makes their byte-level encoding part of the
contract, not an implementation detail of whoever hit record.

Two rules, enforced here and nowhere else:

  * Write UTF-8 with LF. Always, on every platform. `newline="\n"` matters as
    much as the encoding -- without it Windows silently rewrites every "\n"
    to "\r\n", so re-saving an artifact on the other OS produces a diff
    touching every line and hides the one line that actually changed.
  * Read permissively. UTF-8 first, then cp1252, because artifacts written by
    earlier Windows runs are already committed and must stay replayable
    without anyone hand-editing them. A BOM is stripped: PowerShell
    redirection adds one, and json.loads rejects it.
"""

from __future__ import annotations

import os
import warnings

#: What we always write, and what we try first when reading.
ENCODING = "utf-8"

#: Tried only if a file is not valid UTF-8. cp1252 decodes every byte, so it
#: never raises -- it is the last rung of the ladder, not one of several.
LEGACY_ENCODING = "cp1252"


def read_text(path: str) -> str:
    """Decode a file written by any of our supported platforms.

    Returns text, or raises the underlying OSError if the file is unreadable.
    A file that is not valid UTF-8 is decoded as cp1252 and flagged: it still
    loads, so nothing breaks mid-run, but the warning names the file so it can
    be rewritten (any `ArtifactStore.save` of it will).
    """
    with open(path, "rb") as f:
        raw = f.read()
    if raw.startswith(b"\xef\xbb\xbf"):        # UTF-8 BOM, e.g. from PowerShell
        raw = raw[3:]
    try:
        return raw.decode(ENCODING)
    except UnicodeDecodeError:
        warnings.warn(
            f"{path} is not valid UTF-8; decoding it as {LEGACY_ENCODING}. It "
            f"was most likely written by an older Windows run -- re-save it to "
            f"normalize.", stacklevel=2)
        return raw.decode(LEGACY_ENCODING)


def write_text(path: str, text: str) -> None:
    """Write UTF-8 with LF endings, whatever the host platform prefers."""
    with open(path, "w", encoding=ENCODING, newline="\n") as f:
        f.write(text)


def append_line(path: str, line: str) -> None:
    """Append one line to a log. Same guarantees as `write_text`; separate
    because evidence is appended a record at a time, never rewritten."""
    with open(path, "a", encoding=ENCODING, newline="\n") as f:
        f.write(line + "\n")


def is_portable(path: str) -> bool:
    """True if `path` is already UTF-8 with no CR. Used by the test that keeps
    committed artifacts honest, and by `normalize_dir`."""
    with open(path, "rb") as f:
        raw = f.read()
    if b"\r" in raw:
        return False
    try:
        raw.decode(ENCODING)
    except UnicodeDecodeError:
        return False
    return True


def normalize_dir(directory: str, suffix: str = ".json") -> list[str]:
    """Rewrite every non-portable file under `directory` as UTF-8 + LF.

    A one-shot repair for files committed before this module existed. Returns
    the paths it changed, so a caller can report them rather than rewriting
    the tree silently.
    """
    changed = []
    for name in sorted(os.listdir(directory)):
        if not name.endswith(suffix):
            continue
        path = os.path.join(directory, name)
        if is_portable(path):
            continue
        write_text(path, read_text(path).replace("\r\n", "\n"))
        changed.append(path)
    return changed
