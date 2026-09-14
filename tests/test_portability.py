"""Cross-platform portability: artifacts recorded on one OS must replay on
another. These are byte-level tests on purpose -- the failure they guard
against is not a logic bug, it is a file that decodes differently depending on
which machine opened it."""

import json
import os

import pytest

from cua.schemas import Artifact
from cua.store import ArtifactStore
from cua.textio import ENCODING, is_portable, read_text, write_text

from .test_core import minimal_artifact

ARTIFACT_DIRS = [d for d in ("artifacts", "artifacts_backup") if os.path.isdir(d)]


def _committed_artifacts():
    return [os.path.join(d, n)
            for d in ARTIFACT_DIRS
            for n in sorted(os.listdir(d)) if n.endswith(".json")]


@pytest.mark.skipif(not ARTIFACT_DIRS, reason="run from the repo root")
@pytest.mark.parametrize("path", _committed_artifacts())
def test_committed_artifacts_are_utf8_and_lf(path):
    """The regression that started this: a Windows run wrote the em dash in
    `recorder.py`'s review note as cp1252 byte 0x97, and every macOS replay of
    that artifact died in the decoder before the engine ever ran."""
    assert is_portable(path), (
        f"{path} is not UTF-8+LF. Re-save it: "
        f"python -c \"from cua.textio import normalize_dir; normalize_dir('"
        f"{os.path.dirname(path)}')\"")


class TestArtifactBytesAreStable:
    """`ArtifactStore` is the only writer of artifacts, so the guarantee lives
    there and is tested there."""

    def test_non_ascii_prose_survives_a_round_trip(self, tmp_path):
        art = minimal_artifact()
        # exactly what recorder.py puts in a review note, plus the other
        # characters the pages we drive actually contain
        art["provenance"] = {"review_notes": [
            "s1: only one locator candidate (css) — no fallback",
            "balance shown as £1,234.56 / €987.00",
        ]}
        store = ArtifactStore(str(tmp_path))
        path = store.save(Artifact.model_validate(art))

        assert is_portable(path)
        assert "—" in store.load("lookup_balance@1.0.0").provenance.review_notes[0]

    def test_written_bytes_use_lf_on_every_platform(self, tmp_path):
        store = ArtifactStore(str(tmp_path))
        path = store.save(Artifact.model_validate(minimal_artifact()))
        raw = open(path, "rb").read()
        # Windows' default text mode would turn every "\n" into "\r\n" here,
        # making an artifact re-saved on the other OS a whole-file diff.
        assert b"\r" not in raw
        assert json.loads(raw.decode(ENCODING))["id"] == "lookup_balance"


class TestLegacyFilesStillLoad:
    """Artifacts written before textio existed are already committed. They have
    to keep replaying without anyone hand-editing bytes out of them."""

    def test_a_cp1252_artifact_loads_and_warns(self, tmp_path):
        art = minimal_artifact()
        art["provenance"] = {"review_notes": ["one candidate — no fallback"]}
        path = tmp_path / "lookup_balance__1.0.0.json"
        # how a pre-fix Windows run wrote it: pydantic emits the character
        # itself, not a \u escape, and the locale encoder turns it into 0x97
        path.write_bytes(
            json.dumps(art, ensure_ascii=False).encode("cp1252"))
        assert b"\x97" in path.read_bytes()

        store = ArtifactStore(str(tmp_path))
        with pytest.warns(UserWarning, match="not valid UTF-8"):
            loaded = store.load("lookup_balance@1.0.0")
        assert loaded.provenance.review_notes[0].endswith("— no fallback")

    def test_re_saving_a_legacy_artifact_normalizes_it(self, tmp_path):
        art = minimal_artifact()
        art["provenance"] = {"review_notes": ["one candidate — no fallback"]}
        path = tmp_path / "lookup_balance__1.0.0.json"
        path.write_bytes(json.dumps(art, ensure_ascii=False).encode("cp1252"))

        store = ArtifactStore(str(tmp_path))
        with pytest.warns(UserWarning):
            store.save(store.load("lookup_balance@1.0.0"))
        assert is_portable(str(path))

    def test_a_bom_does_not_break_parsing(self, tmp_path):
        """PowerShell redirection prepends one, and json.loads rejects it."""
        p = tmp_path / "policy.yaml"
        p.write_bytes(b"\xef\xbb\xbfallowed_domains: [localhost]\n")
        assert read_text(str(p)).startswith("allowed_domains")


def test_evidence_log_lines_are_utf8(tmp_path):
    from cua.evidence import EvidenceLog
    from cua.redaction import Redactor

    log = EvidenceLog(str(tmp_path), "test", Redactor())
    log.event("note", detail="locator — no fallback, £1,234.56")
    raw = open(os.path.join(log.dir, "events.jsonl"), "rb").read()
    assert b"\r" not in raw
    assert json.loads(raw.decode(ENCODING))["kind"] == "note"
