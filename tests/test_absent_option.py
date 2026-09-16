"""A value the caller supplied that the surface does not offer.

Picking account 99999 from a dropdown that does not list it is the surface
answering the question: there is no such account. The brief calls treating that
as a crash the most common design mistake, and it must not summon a person
either -- nobody can make the account exist.
"""

import pytest

from cua.discovery import DiscoveryOutcome, TranscriptEntry
from cua.driver import ElementNotFound, OptionNotFound
from cua.evidence import EvidenceLog
from cua.recorder import record
from cua.redaction import Redactor
from cua.replay import ReplayEngine, _asks_for_a_caller_value
from cua.schemas import (
    Artifact,
    LocatorCandidate,
    LocatorStrategy,
    ReplayStatus,
    SelectStep,
    Target,
)
from cua.store import ArtifactStore

from .test_replay import POLICY, FakeDriver


def a_dropdown():
    return Target(description="Account dropdown", candidates=[
        LocatorCandidate(strategy=LocatorStrategy.CSS, value="#accountId")])


def a_select(value="{{account_id}}"):
    return SelectStep(id="s1", description="Account dropdown", value=value,
                      target=a_dropdown())


class TestTheCallersValueIsRecognisedWhereverItLives:
    """The bug behind this: a select finds its dropdown by a fixed selector and
    carries the caller's value in `value`. Looking only at the locator ladder
    made the step look like nobody's request, so a bad account number still
    summoned a person."""

    def test_a_parameterised_select_value_counts(self):
        assert _asks_for_a_caller_value(a_select())

    def test_a_literal_select_value_does_not(self):
        assert not _asks_for_a_caller_value(a_select("SAVINGS"))


class TestRecordingDeclaresTheAbsentOption:

    def _rec(self, picked, params=None):
        out = DiscoveryOutcome(
            success=True, proof_text="Account Overview",
            transcript=[
                TranscriptEntry("navigate", {"url": "http://localhost/a"},
                                description="Open entry page", ok=True),
                TranscriptEntry("select", {"value": picked},
                                description="Account dropdown", ok=True,
                                target=a_dropdown()),
            ])
        return record(out, artifact_id="s", name="s", description="d",
                      app="parabank", goal="g", entry_url="http://localhost/",
                      params=params or {"account_id": picked}, secrets={},
                      run_id="r", model="m")

    def test_a_caller_chosen_option_gets_an_outcome(self):
        art = self._rec("13344")
        step = [s for s in art.steps if s.action == "select"][0]
        assert step.value == "{{account_id}}"
        assert step.on_value_absent is not None
        assert step.on_value_absent.code == "NO_SUCH_ACCOUNT_ID"

    def test_the_outcome_is_in_the_contract(self):
        """A caller cannot handle an outcome the capability never mentions."""
        art = self._rec("13344")
        assert "NO_SUCH_ACCOUNT_ID" in {o.code for o in art.business_outcomes}

    def test_a_fixed_option_gets_none(self):
        """SAVINGS is the capability's own choice, not the caller's -- it is
        not the value of any supplied parameter. If it is missing, the page
        changed, and that is a fault."""
        art = self._rec("SAVINGS", params={"account_id": "13344"})
        step = [s for s in art.steps if s.action == "select"][0]
        assert step.on_value_absent is None


class SelectDriver(FakeDriver):
    """Separates the two failures the way a real dropdown does: the control is
    missing, or the control is there and the option is not."""

    def __init__(self, options, dropdown_present=True):
        super().__init__()
        self.options = options
        self.dropdown_present = dropdown_present
        self.texts_visible.add("Account Overview")

    def select(self, target, value):
        if not self.dropdown_present:
            raise ElementNotFound(target, list(target.candidates))
        if value not in self.options:
            raise OptionNotFound(target, value, self.options)
        return LocatorStrategy.CSS


class Operator:
    interactive = True

    def __init__(self):
        self.calls = []

    def intervene(self, **kw):
        self.calls.append(kw.get("step"))
        return "looked"

    def confirm(self, question, committing=None):
        return True


def select_artifact(declared=False):
    step = {
        "id": "s2", "action": "select", "description": "Account dropdown",
        "value": "{{account_id}}",
        "target": {"description": "Account dropdown",
                   "candidates": [{"strategy": "css", "value": "#accountId"}]},
    }
    if declared:
        step["on_value_absent"] = {"type": "business_outcome",
                                   "code": "NO_SUCH_ACCOUNT_ID",
                                   "message": "no such account"}
    return Artifact.model_validate({
        "kind": "capability", "id": "pick", "version": "1.0.0", "name": "Pick",
        "description": "d", "app": "parabank", "status": "approved",
        "inputs": [{"name": "account_id", "type": "string", "description": "d"}],
        "business_outcomes": ([{"code": "NO_SUCH_ACCOUNT_ID", "description": "x"}]
                              if declared else []),
        "steps": [
            {"id": "s1", "action": "navigate", "description": "open",
             "url": "http://localhost/overview"},
            step,
        ],
        "success": {"id": "c", "description": "shown",
                    "match": {"kind": "text_visible", "value": "Account Overview"}},
    })


def engine(tmp_path, artifact, driver, operator):
    store = ArtifactStore(str(tmp_path / "artifacts"))
    ev = EvidenceLog(str(tmp_path / "evidence"), "replay", Redactor())
    store.save(artifact)
    return ReplayEngine(driver, POLICY, store, ev, escalation=operator)


class TestReplayAnswersInsteadOfAsking:

    def test_an_absent_option_never_summons_a_person(self, tmp_path):
        op = Operator()
        d = SelectDriver(options=["13344", "12345"])
        r = engine(tmp_path, select_artifact(), d, op).run(
            "pick@1.0.0", {"account_id": "99999"})
        assert op.calls == [], "nobody can make account 99999 exist"
        assert r.status == ReplayStatus.HARD_FAILURE     # nothing declared yet

    def test_with_the_outcome_declared_it_answers_not_found(self, tmp_path):
        op = Operator()
        d = SelectDriver(options=["13344", "12345"])
        r = engine(tmp_path, select_artifact(declared=True), d, op).run(
            "pick@1.0.0", {"account_id": "99999"})
        assert op.calls == []
        assert r.status == ReplayStatus.BUSINESS_OUTCOME
        assert r.outcome_code == "NO_SUCH_ACCOUNT_ID"

    def test_a_missing_dropdown_is_still_a_fault(self, tmp_path):
        """The contrast that keeps this honest: the control itself is gone, so
        the recording no longer matches the page. That is not an answer about
        the caller's data."""
        op = Operator()
        d = SelectDriver(options=[], dropdown_present=False)
        r = engine(tmp_path, select_artifact(declared=True), d, op).run(
            "pick@1.0.0", {"account_id": "13344"})
        assert r.outcome_code != "NO_SUCH_ACCOUNT_ID"

    def test_an_option_that_exists_still_works(self, tmp_path):
        op = Operator()
        d = SelectDriver(options=["13344"])
        r = engine(tmp_path, select_artifact(declared=True), d, op).run(
            "pick@1.0.0", {"account_id": "13344"})
        assert r.status == ReplayStatus.SUCCESS
        assert op.calls == []
