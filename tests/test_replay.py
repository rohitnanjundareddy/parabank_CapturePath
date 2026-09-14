"""Replay engine tests against a scriptable FakeDriver: proves the three-way
result contract, the recovery ladder, subflow flattening, and parameter
substitution without needing a browser."""

import pytest
from pydantic import ValidationError

from cua.evidence import EvidenceLog
from cua.policy import PolicyEngine
from cua.redaction import Redactor
from cua.replay import ReplayEngine, flatten, substitute
from cua.schemas import (
    Artifact,
    ApprovalStatus,
    LocatorStrategy,
    ReplayStatus,
)
from cua.store import ArtifactStore

POLICY = PolicyEngine({
    "allowed_domains": ["localhost"],
    "allowed_actions": ["navigate", "click", "type", "select", "extract"],
    "risk_handling": {"risky": "confirm", "irreversible": "block"},
})


class FakeDriver:
    """Scriptable surface: element texts, visible texts, and per-target
    failure injection."""

    def __init__(self):
        self.url = "http://localhost/app"
        self.texts_visible: set[str] = set()
        self.element_text: dict[str, str] = {}   # candidate value -> text
        self.fail_targets: dict[str, int] = {}   # candidate value -> times to fail
        self.actions: list[tuple] = []

    def _hit(self, target):
        self.last_missing: list[str] = []
        for c in target.candidates:
            fails = self.fail_targets.get(c.value, 0)
            if fails > 0:
                self.fail_targets[c.value] = fails - 1
                self.last_missing.append(c.strategy.value)
                continue
            if c.value in self.element_text:
                return c
            self.last_missing.append(c.strategy.value)
        raise RuntimeError(f"element not found: {target.description}")

    def last_missing_rungs(self):
        return list(getattr(self, "last_missing", []))

    def navigate(self, url):
        self.actions.append(("navigate", url))
        self.url = url

    def click(self, target):
        c = self._hit(target)
        self.actions.append(("click", c.value))
        return c.strategy

    def type_text(self, target, text):
        c = self._hit(target)
        self.actions.append(("type", c.value, text))
        return c.strategy

    def select(self, target, value):
        c = self._hit(target)
        self.actions.append(("select", c.value, value))
        return c.strategy

    def read_text(self, target):
        c = self._hit(target)
        return self.element_text[c.value], c.strategy

    def wait_for(self, cond):
        return cond.value in self.texts_visible if cond.value else True

    def matches(self, match):
        if match.kind == "text_visible":
            return match.value in self.texts_visible
        if match.kind == "url_matches":
            return match.value in self.url
        return False

    def current_url(self):
        return self.url

    def observe(self, screenshot_to=None):
        return None


def make_engine(tmp_path, driver):
    store = ArtifactStore(str(tmp_path / "artifacts"))
    ev = EvidenceLog(str(tmp_path / "evidence"), "replay", Redactor())
    return ReplayEngine(driver, POLICY, store, ev), store


def balance_artifact(status="approved"):
    return Artifact.model_validate({
        "kind": "capability", "id": "lookup", "version": "1.0.0",
        "name": "Lookup", "description": "d", "app": "parabank",
        "status": status,
        "inputs": [{"name": "account_id", "type": "string", "description": "d"}],
        "outputs": [{"name": "balance", "type": "number", "description": "d"}],
        "business_outcomes": [
            {"code": "ACCOUNT_NOT_FOUND", "description": "no such row"}],
        "steps": [
            {"id": "s1", "action": "navigate", "description": "open overview",
             "url": "http://localhost/overview"},
            {"id": "s2", "action": "extract", "description": "read balance",
             "target": {"description": "balance cell for {{account_id}}",
                        "candidates": [
                            {"strategy": "relative_text",
                             "value": "{{account_id}}||td:nth-child(2)"}]},
             "output": "balance", "parse": "currency",
             "on_exhausted": {"type": "business_outcome",
                              "code": "ACCOUNT_NOT_FOUND",
                              "message": "no row for that account"}},
        ],
        "success": {"id": "c", "description": "overview shown",
                    "match": {"kind": "text_visible", "value": "Account Overview"}},
    })


class TestReplayEngine:
    def test_success_with_typed_outputs(self, tmp_path):
        d = FakeDriver()
        d.texts_visible.add("Account Overview")
        d.element_text["13344||td:nth-child(2)"] = "$1,234.56"
        engine, store = make_engine(tmp_path, d)
        store.save(balance_artifact())

        r = engine.run("lookup@1.0.0", {"account_id": "13344"})
        assert r.status == ReplayStatus.SUCCESS
        assert r.outputs["balance"] == 1234.56
        assert r.step_reports[1].locator_used == LocatorStrategy.RELATIVE_TEXT

    def test_missing_row_is_business_outcome_not_crash(self, tmp_path):
        d = FakeDriver()
        d.texts_visible.add("Account Overview")  # page fine, row absent
        engine, store = make_engine(tmp_path, d)
        store.save(balance_artifact())

        r = engine.run("lookup@1.0.0", {"account_id": "99999"})
        assert r.status == ReplayStatus.BUSINESS_OUTCOME
        assert r.outcome_code == "ACCOUNT_NOT_FOUND"

    def test_unmet_checkpoint_is_hard_failure_with_debug_context(self, tmp_path):
        d = FakeDriver()  # nothing visible: checkpoint cannot pass
        d.element_text["13344||td:nth-child(2)"] = "$10.00"
        engine, store = make_engine(tmp_path, d)
        store.save(balance_artifact())

        r = engine.run("lookup@1.0.0", {"account_id": "13344"})
        assert r.status == ReplayStatus.HARD_FAILURE
        assert r.failed_step == "success_checkpoint"
        assert "Account Overview" in r.expected

    def test_draft_blocked_without_flag(self, tmp_path):
        engine, store = make_engine(tmp_path, FakeDriver())
        store.save(balance_artifact(status="draft"))
        with pytest.raises(PermissionError):
            engine.run("lookup@1.0.0", {"account_id": "1"})

    def test_missing_param_rejected_before_touching_surface(self, tmp_path):
        d = FakeDriver()
        engine, store = make_engine(tmp_path, d)
        store.save(balance_artifact())
        with pytest.raises(KeyError):
            engine.run("lookup@1.0.0", {})
        assert d.actions == []

    def test_recovery_retries_then_succeeds(self, tmp_path):
        art = balance_artifact()
        art.steps[1].detectors = [{
            "id": "flaky", "when": {"kind": "text_visible", "value": "Loading"},
            "then": {"type": "recover", "remedy": "retry",
                     "max_attempts": 2, "backoff_ms": 0},
        }]
        art = Artifact.model_validate(art.model_dump())
        d = FakeDriver()
        d.texts_visible.update({"Account Overview", "Loading"})
        d.element_text["13344||td:nth-child(2)"] = "$5.00"
        d.fail_targets["13344||td:nth-child(2)"] = 1  # fail once, then work
        engine, store = make_engine(tmp_path, d)
        store.save(art)

        r = engine.run("lookup@1.0.0", {"account_id": "13344"})
        assert r.status == ReplayStatus.SUCCESS
        assert r.step_reports[1].attempts == 2
        assert r.step_reports[1].recovered is True


class TestExtractionHonesty:
    """An answer must come from something actually read off the page."""

    def test_empty_extract_is_not_reported_as_success(self, tmp_path):
        d = FakeDriver()
        d.texts_visible.add("Account Overview")
        # The row resolves, but the cell is blank. Returning "" as a success
        # hands the caller an empty answer that looks authoritative.
        d.element_text["13344||td:nth-child(2)"] = "   "
        engine, store = make_engine(tmp_path, d)
        store.save(balance_artifact())

        r = engine.run("lookup@1.0.0", {"account_id": "13344"})
        assert r.status == ReplayStatus.BUSINESS_OUTCOME
        assert r.outcome_code == "ACCOUNT_NOT_FOUND"
        assert "balance" not in r.outputs

    def test_read_value_carries_its_source_evidence(self, tmp_path):
        d = FakeDriver()
        d.texts_visible.add("Account Overview")
        d.element_text["13344||td:nth-child(2)"] = "$1,234.56"
        engine, store = make_engine(tmp_path, d)
        store.save(balance_artifact())

        r = engine.run("lookup@1.0.0", {"account_id": "13344"})
        assert r.outputs["balance"] == 1234.56
        assert r.output_evidence["balance"].source_text == "$1,234.56"


class TestUnreadableIsNotAbsent:
    """`on_exhausted` declares what ABSENCE means. Content that was found but
    could not be parsed is not absence, and must not be reported as one."""

    def _artifact_reading(self, parse_mode):
        art = balance_artifact().model_dump()
        art["steps"][1]["parse"] = parse_mode
        return Artifact.model_validate(art)

    def test_unparseable_content_is_a_failure_not_a_business_outcome(self, tmp_path):
        d = FakeDriver()
        d.texts_visible.add("Account Overview")
        # a whole table, typed as currency by a recorder that saw a "$"
        d.element_text["13344||td:nth-child(2)"] = (
            "Date\tAmount\n12-10-2025\t$300.00\n12-11-2025\t$100.00")
        engine, store = make_engine(tmp_path, d)
        store.save(self._artifact_reading("currency"))

        r = engine.run("lookup@1.0.0", {"account_id": "13344"})
        assert r.status == ReplayStatus.HARD_FAILURE
        assert r.outcome_code != "ACCOUNT_NOT_FOUND"   # never "it is not there"
        assert "could not be parsed" in (r.observed or "")

    def test_genuine_absence_still_reports_the_business_outcome(self, tmp_path):
        d = FakeDriver()
        d.texts_visible.add("Account Overview")       # row simply not present
        engine, store = make_engine(tmp_path, d)
        store.save(self._artifact_reading("currency"))

        r = engine.run("lookup@1.0.0", {"account_id": "99999"})
        assert r.status == ReplayStatus.BUSINESS_OUTCOME
        assert r.outcome_code == "ACCOUNT_NOT_FOUND"


class TestLadderIdentity:
    """Every rung must point at the SAME element. A fallback that drops the
    caller's parameter answers a different question — and returns another
    customer's data as a confident success."""

    def _artifact_with_unconstrained_fallback(self):
        art = balance_artifact().model_dump()
        art["steps"][1]["target"]["candidates"] = [
            {"strategy": "relative_text", "value": "{{account_id}}||td:nth-child(2)"},
            {"strategy": "css", "value": "td.balance"},   # matches ANY account's cell
        ]
        return Artifact.model_validate(art)

    def test_absent_account_does_not_fall_through_to_another_row(self, tmp_path):
        d = FakeDriver()
        d.texts_visible.add("Account Overview")
        d.element_text["td.balance"] = "$999.99"      # some other account's cell
        engine, store = make_engine(tmp_path, d)
        store.save(self._artifact_with_unconstrained_fallback())

        r = engine.run("lookup@1.0.0", {"account_id": "99999"})
        assert r.status == ReplayStatus.BUSINESS_OUTCOME
        assert r.outcome_code == "ACCOUNT_NOT_FOUND"
        assert "balance" not in r.outputs     # never another account's number

    def test_constrained_rung_is_still_used_when_it_matches(self, tmp_path):
        d = FakeDriver()
        d.texts_visible.add("Account Overview")
        d.element_text["13344||td:nth-child(2)"] = "$12.00"
        engine, store = make_engine(tmp_path, d)
        store.save(self._artifact_with_unconstrained_fallback())

        r = engine.run("lookup@1.0.0", {"account_id": "13344"})
        assert r.status == ReplayStatus.SUCCESS
        assert r.outputs["balance"] == 12.00

    def test_unparameterised_ladder_keeps_all_its_rungs(self):
        from cua.schemas import Target
        t = Target.model_validate({"description": "d", "candidates": [
            {"strategy": "relative_text", "value": "Balance:||td:nth-child(2)"},
            {"strategy": "css", "value": "#balance"}]})
        assert len(t.constrained_candidates()) == 2


class TestSubflowCycles:
    def test_self_referencing_capability_is_refused(self, tmp_path):
        store = ArtifactStore(str(tmp_path / "a"))
        art = balance_artifact().model_dump()
        art["steps"].insert(0, {"id": "loop", "action": "run_subflow",
                                "description": "reuse myself",
                                "ref": "lookup@1.0.0"})
        store.save(Artifact.model_validate(art))
        with pytest.raises(ValueError, match="circular subflow"):
            flatten(store.load("lookup@1.0.0"), store, allow_draft=True)


class TestDriftSignal:
    """Sliding down the ladder is the early warning that a capability is
    breaking. It must not fire for a recording that was simply wrong."""

    def _two_rung_artifact(self):
        art = balance_artifact().model_dump()
        art["steps"][1]["target"]["candidates"] = [
            {"strategy": "role_name", "role": "cell", "value": "preferred"},
            {"strategy": "relative_text", "value": "{{account_id}}||td:nth-child(2)"},
        ]
        return Artifact.model_validate(art)

    def test_fallback_rung_flags_drift(self, tmp_path):
        d = FakeDriver()
        d.texts_visible.add("Account Overview")
        d.element_text["13344||td:nth-child(2)"] = "$5.00"   # only the fallback exists
        engine, store = make_engine(tmp_path, d)
        store.save(self._two_rung_artifact())

        r = engine.run("lookup@1.0.0", {"account_id": "13344"})
        assert r.status == ReplayStatus.SUCCESS
        assert "s2" in r.degraded_steps

    def test_preferred_rung_does_not_flag_drift(self, tmp_path):
        d = FakeDriver()
        d.texts_visible.add("Account Overview")
        d.element_text["preferred"] = "$5.00"                # top rung matches
        engine, store = make_engine(tmp_path, d)
        store.save(self._two_rung_artifact())

        r = engine.run("lookup@1.0.0", {"account_id": "13344"})
        assert r.status == ReplayStatus.SUCCESS
        assert r.degraded_steps == []


class TestFlattenAndSubstitute:
    def test_substitute_requires_all_params(self):
        assert substitute("row {{id}}", {"id": "7"}) == "row 7"
        with pytest.raises(KeyError):
            substitute("row {{id}}", {})

    def test_subflow_flattens_with_bound_inputs(self, tmp_path):
        store = ArtifactStore(str(tmp_path / "a"))
        frag = Artifact.model_validate({
            "kind": "fragment", "id": "login", "version": "1.0.0",
            "name": "Login", "description": "d", "app": "parabank",
            "status": "approved",
            "inputs": [{"name": "user", "type": "string", "description": "d"}],
            "steps": [{"id": "u", "action": "type", "description": "username",
                       "target": {"description": "user field", "candidates": [
                           {"strategy": "css", "value": "input[name=username]"}]},
                       "value": "{{user}}"}],
            "success": {"id": "c", "description": "in",
                        "match": {"kind": "text_visible", "value": "Welcome"}},
        })
        store.save(frag)
        cap = balance_artifact()
        cap.steps.insert(0, {"id": "login", "action": "run_subflow",  # type: ignore
                             "description": "auth",
                             "ref": "login@1.0.0",
                             "bind_inputs": {"user": "{{username}}"}})
        cap = Artifact.model_validate(cap.model_dump())
        store.save(cap)

        steps = flatten(cap, store, allow_draft=False)
        assert steps[0].id == "login.u"
        assert steps[0].value == "{{username}}"  # bound to parent param

    def test_draft_fragment_blocked(self, tmp_path):
        store = ArtifactStore(str(tmp_path / "a"))
        frag = Artifact.model_validate({
            "kind": "fragment", "id": "login", "version": "1.0.0",
            "name": "L", "description": "d", "app": "p", "status": "draft",
            "steps": [{"id": "u", "action": "navigate", "description": "d",
                       "url": "http://localhost/"}],
            "success": {"id": "c", "description": "d",
                        "match": {"kind": "text_visible", "value": "x"}}})
        store.save(frag)
        cap = balance_artifact()
        cap.steps.insert(0, {"id": "login", "action": "run_subflow",  # type: ignore
                             "description": "auth", "ref": "login@1.0.0"})
        cap = Artifact.model_validate(cap.model_dump())
        store.save(cap)
        with pytest.raises(PermissionError, match="draft"):
            flatten(cap, store, allow_draft=False)


class TestEscalationDuringReplay:
    """A replay that cannot proceed hands the live session to a human, then
    resumes the REST of the path rather than aborting."""

    def _artifact_with_escalate(self):
        art = balance_artifact().model_dump()
        art["steps"][1]["detectors"] = []
        art["steps"][1]["on_exhausted"] = {
            "type": "escalate",
            "reason": "balance row not reachable; operator input required",
        }
        art["steps"][1]["wait_after"] = {
            "kind": "text_visible", "value": "Account Overview", "timeout_ms": 100}
        return Artifact.model_validate(art)

    def test_human_completes_step_then_run_continues(self, tmp_path):
        d = FakeDriver()
        d.texts_visible.add("Account Overview")

        class Operator:
            def __init__(self): self.called = 0
            def intervene(self, **kw):
                self.called += 1
                # Operator fixes the page in the same live session.
                d.element_text["13344||td:nth-child(2)"] = "$7.50"
                return "located the account manually"
            def confirm(self, q): return True

        op = Operator()
        engine, store = make_engine(tmp_path, d)
        engine.escalation = op
        store.save(self._artifact_with_escalate())

        r = engine.run("lookup@1.0.0", {"account_id": "13344"})
        assert op.called == 1
        assert r.escalations == 1
        assert r.status == ReplayStatus.SUCCESS
        assert "completed by operator" in r.step_reports[1].note

    def test_second_failure_after_handoff_is_hard_failure_not_a_loop(self, tmp_path):
        d = FakeDriver()  # operator does not fix anything

        class Operator:
            def __init__(self): self.called = 0
            def intervene(self, **kw):
                self.called += 1
                return "could not resolve"
            def confirm(self, q): return True

        op = Operator()
        engine, store = make_engine(tmp_path, d)
        engine.escalation = op
        store.save(self._artifact_with_escalate())

        r = engine.run("lookup@1.0.0", {"account_id": "13344"})
        assert op.called == 1          # exactly one handoff, no loop
        assert r.status == ReplayStatus.HARD_FAILURE
        assert r.failed_step == "s2"

    def test_escalation_without_operator_fails_closed(self, tmp_path):
        d = FakeDriver()
        engine, store = make_engine(tmp_path, d)
        engine.escalation = None       # unattended
        store.save(self._artifact_with_escalate())

        r = engine.run("lookup@1.0.0", {"account_id": "13344"})
        assert r.status == ReplayStatus.HARD_FAILURE
        assert "no operator" in r.observed


class TestSchemaGuards:
    def test_two_steps_writing_one_output_is_rejected(self):
        art = balance_artifact().model_dump()
        dup = dict(art["steps"][1])
        dup["id"] = "s3"
        art["steps"].append(dup)
        with pytest.raises(ValidationError, match="same output"):
            Artifact.model_validate(art)
