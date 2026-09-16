"""An empty reading is an answer, not a crash -- but only when the thing you
were reading is a collection.

"No transactions in that range" is a fact about the account. A balance cell
that exists and is blank is a reading not to be trusted. Both used to land on
`on_exhausted` together, which is the conflation the brief calls the most
common design mistake in this space."""

import pytest

from cua.discovery import DiscoveryOutcome, TranscriptEntry
from cua.recorder import record, _is_collection
from cua.replay import ReplayEngine
from cua.schemas import (
    Artifact, LocatorCandidate, LocatorStrategy, ReplayStatus, Target)
from cua.evidence import EvidenceLog
from cua.redaction import Redactor
from cua.store import ArtifactStore

from .test_replay import POLICY, FakeDriver


def extract_run(sample):
    return DiscoveryOutcome(
        success=True, proof_text="Account Activity",
        outputs={"transactions": sample},
        transcript=[
            TranscriptEntry("navigate", {"url": "http://localhost/act"},
                            description="Open entry page", ok=True),
            TranscriptEntry("extract", {"output": "transactions"},
                            description="Transaction table", ok=True,
                            extracted=sample,
                            target=Target(description="table", candidates=[
                                LocatorCandidate(strategy=LocatorStrategy.CSS,
                                                 value="#transactionTable")])),
        ])


def rec(sample):
    return record(extract_run(sample), artifact_id="t", name="t",
                  description="d", app="parabank", goal="g",
                  entry_url="http://localhost/", params={}, secrets={},
                  run_id="r", model="m")


A_TABLE = ("Date\tAmount\n"
           "12-10-2026\t$300.00\n"
           "12-11-2026\t$100.00")
A_VALUE = "$1,234.56"


class TestWhatCountsAsACollection:

    @pytest.mark.parametrize("sample", [
        A_TABLE,
        "row one\nrow two",
        "$10.00 $20.00",
    ])
    def test_rows_and_repeated_amounts_are_collections(self, sample):
        assert _is_collection(sample)

    @pytest.mark.parametrize("sample", [A_VALUE, "SAVINGS", "13344", ""])
    def test_a_single_value_is_not(self, sample):
        assert not _is_collection(sample)


class TestRecordingDeclaresTheEmptyCase:

    def test_a_collection_extract_says_what_empty_means(self):
        art = rec(A_TABLE)
        step = [s for s in art.steps if s.action == "extract"][0]
        assert step.on_empty is not None
        assert step.on_empty.code == "NO_TRANSACTIONS"

    def test_the_outcome_is_declared_in_the_contract(self):
        """A caller cannot handle an outcome the capability never mentions."""
        art = rec(A_TABLE)
        assert "NO_TRANSACTIONS" in {o.code for o in art.business_outcomes}

    def test_a_scalar_extract_does_not(self):
        """An empty balance field is a reading not to be trusted, so it stays
        a failure rather than becoming 'the account has no balance'."""
        art = rec(A_VALUE)
        step = [s for s in art.steps if s.action == "extract"][0]
        assert step.on_empty is None


class TestReplayReportsEmptyAsAnOutcome:

    def _engine(self, tmp_path, artifact):
        store = ArtifactStore(str(tmp_path / "artifacts"))
        ev = EvidenceLog(str(tmp_path / "evidence"), "replay", Redactor())
        store.save(artifact)
        d = FakeDriver()
        d.texts_visible.add("Account Activity")
        return ReplayEngine(d, POLICY, store, ev), d

    def test_an_empty_table_is_a_business_outcome(self, tmp_path):
        engine, d = self._engine(tmp_path, rec(A_TABLE))
        d.element_text["#transactionTable"] = "   "      # found, but empty
        r = engine.run("t@1.0.0", {}, allow_draft=True)
        assert r.status == ReplayStatus.BUSINESS_OUTCOME
        assert r.outcome_code == "NO_TRANSACTIONS"

    def test_a_table_with_rows_is_still_a_success(self, tmp_path):
        engine, d = self._engine(tmp_path, rec(A_TABLE))
        d.element_text["#transactionTable"] = A_TABLE
        r = engine.run("t@1.0.0", {}, allow_draft=True)
        assert r.status == ReplayStatus.SUCCESS
        assert r.outputs["transactions"] == A_TABLE

    def test_a_missing_table_is_NOT_reported_as_empty(self, tmp_path):
        """The distinction that matters: the element was never found, so the
        recording no longer matches the page. Calling that 'no transactions'
        is the confident wrong answer."""
        engine, d = self._engine(tmp_path, rec(A_TABLE))
        # no element_text at all -> the ladder resolves nothing
        r = engine.run("t@1.0.0", {}, allow_draft=True)
        assert r.status == ReplayStatus.HARD_FAILURE
        assert r.outcome_code != "NO_TRANSACTIONS"

    def test_an_empty_scalar_is_a_failure_not_an_outcome(self, tmp_path):
        engine, d = self._engine(tmp_path, rec(A_VALUE))
        d.element_text["#transactionTable"] = ""
        r = engine.run("t@1.0.0", {}, allow_draft=True)
        assert r.status == ReplayStatus.HARD_FAILURE
