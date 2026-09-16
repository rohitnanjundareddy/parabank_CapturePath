"""Handing over when a step runs out of options.

A step that exhausts its recovery budget with nothing declared for that case
is, by definition, a failure the artifact never anticipated. On an attended
run that is exactly when a person is worth asking -- so the handoff is the
default, and these tests pin when it does and does not happen."""

import pytest

from cua.evidence import EvidenceLog
from cua.redaction import Redactor
from cua.replay import ReplayEngine
from cua.schemas import Artifact, ReplayStatus
from cua.store import ArtifactStore

from .test_replay import POLICY, FakeDriver, balance_artifact


class Operator:
    """Records handoffs. `interactive` mirrors EscalationController, which the
    engine reads to tell an attended run from an unattended one."""

    def __init__(self, interactive=True, fixes=None):
        self.interactive = interactive
        self.calls = []
        self._fixes = fixes or (lambda driver: None)

    def intervene(self, *, context, goal, step, driver, evidence):
        self.calls.append(step)
        self._fixes(driver)
        return "had a look"

    def confirm(self, question, committing=None):
        return True


def failing_artifact(parameterised=False):
    """The extract's only rung never resolves, and nothing is declared for
    that case -- so it reaches the engine as an unanticipated failure.

    `parameterised` decides WHOSE problem that is. A rung written in terms of
    {{account_id}} most likely found nothing because the caller asked for a
    record that is not there; a fixed rung means the page moved. Only the
    second is worth interrupting a person for.
    """
    art = balance_artifact().model_dump()
    art["steps"][1]["on_exhausted"] = {"type": "hard_failure",
                                       "message": "step exhausted all candidates"}
    if not parameterised:
        art["steps"][1]["target"]["candidates"] = [
            {"strategy": "css", "value": "#balance"}]
        art["steps"][1]["target"]["description"] = "the balance cell"
    return Artifact.model_validate(art)


def engine_for(tmp_path, driver, operator, parameterised=False, **kw):
    store = ArtifactStore(str(tmp_path / "artifacts"))
    ev = EvidenceLog(str(tmp_path / "evidence"), "replay", Redactor())
    store.save(failing_artifact(parameterised))
    return ReplayEngine(driver, POLICY, store, ev, escalation=operator, **kw)


def a_driver():
    d = FakeDriver()
    d.texts_visible.add("Account Overview")
    return d                      # no element_text: the extract cannot resolve


class TestExhaustionHandsOver:

    def test_an_attended_run_asks_before_giving_up(self, tmp_path):
        op = Operator()
        engine = engine_for(tmp_path, a_driver(), op)
        engine.run("lookup@1.0.0", {"account_id": "13344"})
        assert op.calls == ["s2"], "the operator should have been asked once"

    def test_the_handoff_is_recorded_on_the_result(self, tmp_path):
        op = Operator()
        engine = engine_for(tmp_path, a_driver(), op)
        r = engine.run("lookup@1.0.0", {"account_id": "13344"})
        assert r.escalations == 1

    def test_a_fix_made_by_the_operator_lets_the_run_continue(self, tmp_path):
        """If the person clears the blocker, the step is retried and the run
        finishes normally rather than reporting what it saw before they acted."""
        d = a_driver()
        op = Operator(fixes=lambda drv: drv.element_text.__setitem__(
            "#balance", "$1,234.56"))
        engine = engine_for(tmp_path, d, op)
        r = engine.run("lookup@1.0.0", {"account_id": "13344"})
        assert r.status == ReplayStatus.SUCCESS
        assert r.outputs["balance"] == 1234.56

    def test_only_one_handoff_per_step(self, tmp_path):
        """A step that fails again after a handoff is a debuggable failure,
        not a reason to ask the same person the same question again."""
        op = Operator()                       # fixes nothing
        engine = engine_for(tmp_path, a_driver(), op)
        r = engine.run("lookup@1.0.0", {"account_id": "13344"})
        assert len(op.calls) == 1
        assert r.status == ReplayStatus.HARD_FAILURE
        # The second time round the failure is reported as itself, not as
        # "the handoff did not work" -- the underlying error is what a person
        # debugging this needs to see.
        assert "element not found" in (r.observed or "")
        assert r.failed_step == "s2"


class TestWhenItMustNotHandOver:

    def test_an_unattended_run_fails_closed(self, tmp_path):
        """There is nobody there. Handing over would auto-resume into a no-op
        and dress a failure up as a recovery attempt."""
        op = Operator(interactive=False)
        engine = engine_for(tmp_path, a_driver(), op)
        r = engine.run("lookup@1.0.0", {"account_id": "13344"})
        assert op.calls == []
        assert r.status == ReplayStatus.HARD_FAILURE

    def test_opting_out_is_honoured(self, tmp_path):
        op = Operator()
        engine = engine_for(tmp_path, a_driver(), op, escalate_on_failure=False)
        r = engine.run("lookup@1.0.0", {"account_id": "13344"})
        assert op.calls == []
        assert r.status == ReplayStatus.HARD_FAILURE

    def test_no_operator_attached_is_not_a_crash(self, tmp_path):
        """What the smoke replay does: proving a recording must never be
        rescued by a person, or it proves nothing."""
        store = ArtifactStore(str(tmp_path / "artifacts"))
        ev = EvidenceLog(str(tmp_path / "evidence"), "replay", Redactor())
        store.save(failing_artifact())
        engine = ReplayEngine(a_driver(), POLICY, store, ev, escalation=None)
        r = engine.run("lookup@1.0.0", {"account_id": "13344"})
        assert r.status == ReplayStatus.HARD_FAILURE
        assert r.escalations == 0

    def test_a_declared_business_outcome_is_not_a_failure_to_escalate(self, tmp_path):
        """Absence the artifact anticipated is an answer. Nobody is asked."""
        op = Operator()
        store = ArtifactStore(str(tmp_path / "artifacts"))
        ev = EvidenceLog(str(tmp_path / "evidence"), "replay", Redactor())
        store.save(balance_artifact())        # on_exhausted = business_outcome
        engine = ReplayEngine(a_driver(), POLICY, store, ev, escalation=op)
        r = engine.run("lookup@1.0.0", {"account_id": "99999"})
        assert r.status == ReplayStatus.BUSINESS_OUTCOME
        assert op.calls == []


class TestACallersOwnMissingValueIsNotAHandoff:
    """No operator can make account 99999 exist. Asking one to try wastes the
    person and leaves the caller waiting for an answer the run already has."""

    def test_a_parameterised_step_does_not_summon_anyone(self, tmp_path):
        op = Operator()
        engine = engine_for(tmp_path, a_driver(), op, parameterised=True)
        r = engine.run("lookup@1.0.0", {"account_id": "99999"})
        assert op.calls == []
        assert r.status == ReplayStatus.HARD_FAILURE

    def test_a_fixed_rung_still_does(self, tmp_path):
        """The contrast: the same failure on a rung that carries no caller
        value means the page moved, which a person may well be able to clear."""
        op = Operator()
        engine = engine_for(tmp_path, a_driver(), op, parameterised=False)
        engine.run("lookup@1.0.0", {"account_id": "13344"})
        assert op.calls == ["s2"]

    def test_a_declared_escalate_is_still_honoured(self, tmp_path):
        """Opting out of the automatic handoff must not silence a step that
        explicitly asked for one."""
        art = failing_artifact(parameterised=True).model_dump()
        art["steps"][1]["on_exhausted"] = {"type": "escalate",
                                           "reason": "a human must look"}
        store = ArtifactStore(str(tmp_path / "artifacts"))
        ev = EvidenceLog(str(tmp_path / "evidence"), "replay", Redactor())
        store.save(Artifact.model_validate(art))
        op = Operator()
        engine = ReplayEngine(a_driver(), POLICY, store, ev, escalation=op)
        engine.run("lookup@1.0.0", {"account_id": "99999"})
        assert op.calls == ["s2"]
