"""Absent data is an answer, not a blocked page.

"No such account" is a legitimate result the caller needs -- the brief calls
conflating it with a crash the most common design mistake in this space. These
tests pin the split: a value the surface does not have is reported as not
found, and the partial path is still recorded as a draft so a reviewer can
replay it and decide whether to finish it."""

import pytest

from cua.discovery import DiscoveryOutcome, TranscriptEntry
from cua.recorder import record
from cua.schemas import ApprovalStatus, LocatorCandidate, LocatorStrategy, Target


def a_transcript():
    return [
        TranscriptEntry("navigate", {"url": "http://localhost/transfer"},
                        description="Open entry page", ok=True),
        TranscriptEntry("select", {"value": "13344"},
                        description="From Account dropdown", ok=True,
                        target=Target(description="From Account", candidates=[
                            LocatorCandidate(strategy=LocatorStrategy.CSS,
                                             value="#fromAccountId")])),
    ]


def a_partial(**over):
    o = DiscoveryOutcome(success=False, transcript=a_transcript(),
                         final_texts=["Transfer Funds"])
    for k, v in over.items():
        setattr(o, k, v)
    return o


def rec(outcome, **kw):
    return record(outcome, artifact_id="p", name="p", description="d",
                  app="parabank", goal="g", entry_url="http://localhost/",
                  params={"source_account": "13344"}, secrets={},
                  run_id="r", model="m", **kw)


class TestAPartialIsRecordedButNeverPassedOff:

    def test_a_failed_run_is_still_refused_without_a_reason(self):
        """The old guard stands. Recording a failure silently is what makes a
        broken draft indistinguishable from a working capability."""
        with pytest.raises(ValueError, match="refusing to record a failed"):
            rec(a_partial())

    def test_a_partial_records_the_path_it_explored(self):
        art = rec(a_partial(), partial_reason="'destination_account' not found")
        assert [s.action for s in art.steps] == ["navigate", "select"]

    def test_a_partial_is_always_a_draft(self):
        art = rec(a_partial(), partial_reason="not found")
        assert art.status == ApprovalStatus.DRAFT

    def test_the_artifact_says_why_it_is_partial(self):
        art = rec(a_partial(), partial_reason="'destination_account' not found")
        assert "destination_account" in art.provenance.partial_reason

    def test_a_successful_run_carries_no_partial_reason(self):
        good = DiscoveryOutcome(success=True, proof_text="Transfer Complete!",
                                transcript=a_transcript())
        assert rec(good).provenance.partial_reason is None


class TestThePartialCheckpointIsHonest:
    """An empty checkpoint is the worst outcome available: "" is visible on
    every page, so the artifact would report success from anywhere."""

    def test_the_checkpoint_asserts_a_state_actually_reached(self):
        art = rec(a_partial(), partial_reason="not found")
        assert art.success.match.value == "Transfer Funds"
        assert art.success.match.value != ""

    def test_a_run_with_nothing_to_assert_is_refused(self):
        bare = a_partial()
        bare.final_texts = []
        with pytest.raises(ValueError, match="no state worth asserting"):
            rec(bare, partial_reason="not found")

    def test_a_digit_only_state_is_not_a_checkpoint(self):
        """Account numbers and balances change between runs, so they prove
        nothing about having arrived."""
        bare = a_partial()
        bare.final_texts = ["13344", "Transfer Funds"]
        assert rec(bare, partial_reason="x").success.match.value == "Transfer Funds"


class TestTheOutcomeCarriesTheAnswer:

    def test_not_found_is_recorded_on_the_outcome(self):
        o = a_partial(not_found={"parameter": "destination_account",
                                 "observed": "12345, 12456, 13344"})
        assert o.not_found["parameter"] == "destination_account"

    def test_a_normal_run_has_no_not_found(self):
        assert DiscoveryOutcome(success=True).not_found is None
