import pytest
from pydantic import ValidationError

from cua.policy import PolicyEngine, ProposedAction, Verdict
from cua.redaction import Redactor
from cua.schemas import (
    Artifact,
    ArtifactKind,
    BusinessOutcomeDecl,
    Checkpoint,
    DetectorMatch,
    OutputField,
    ParamType,
    ReplayResult,
    ReplayStatus,
    RiskLevel,
)

POLICY = {
    "allowed_domains": ["localhost", "parabank.parasoft.com"],
    "allowed_actions": ["navigate", "click", "type", "extract"],
    "blocked_url_patterns": [r"/admin\.htm"],
    "risky_target_patterns": [r"open.*account"],
    "irreversible_target_patterns": [r"transfer"],
    "risk_handling": {"risky": "confirm", "irreversible": "block"},
}


def minimal_artifact(**overrides) -> dict:
    base = {
        "kind": "capability",
        "id": "lookup_balance",
        "version": "1.0.0",
        "name": "Look up account balance",
        "description": "Reads the balance for one account",
        "app": "parabank",
        "outputs": [
            {"name": "balance", "type": "number", "description": "Current balance"}
        ],
        "steps": [
            {
                "id": "s1",
                "action": "extract",
                "description": "Read balance cell",
                "target": {
                    "description": "Balance cell for the account",
                    "candidates": [{"strategy": "css", "value": "td.balance"}],
                },
                "output": "balance",
                "parse": "currency",
            }
        ],
        "success": {
            "id": "done",
            "description": "Overview shows balances",
            "match": {"kind": "text_visible", "value": "Account Overview"},
        },
    }
    base.update(overrides)
    return base


class TestArtifactSchema:
    def test_valid_artifact_round_trips(self):
        art = Artifact.model_validate(minimal_artifact())
        again = Artifact.model_validate_json(art.model_dump_json())
        assert again.ref == "lookup_balance@1.0.0"
        assert again.kind == ArtifactKind.CAPABILITY

    def test_extract_must_fill_declared_output(self):
        bad = minimal_artifact(outputs=[])
        with pytest.raises(ValidationError, match="undeclared output"):
            Artifact.model_validate(bad)

    def test_detector_outcome_must_be_declared(self):
        bad = minimal_artifact()
        bad["steps"][0]["detectors"] = [
            {
                "id": "d1",
                "when": {"kind": "text_visible", "value": "could not be found"},
                "then": {
                    "type": "business_outcome",
                    "code": "ACCOUNT_NOT_FOUND",
                    "message": "No such account",
                },
            }
        ]
        with pytest.raises(ValidationError, match="undeclared business outcome"):
            Artifact.model_validate(bad)
        bad["business_outcomes"] = [
            {"code": "ACCOUNT_NOT_FOUND", "description": "Account does not exist"}
        ]
        assert Artifact.model_validate(bad)

    def test_subflow_ref_must_pin_version(self):
        bad = minimal_artifact()
        bad["steps"].insert(
            0,
            {
                "id": "s0",
                "action": "run_subflow",
                "description": "Log in first",
                "ref": "login",  # missing @version
            },
        )
        with pytest.raises(ValidationError, match="pin a version"):
            Artifact.model_validate(bad)

    def test_replay_result_contract(self):
        with pytest.raises(ValidationError):
            ReplayResult(
                status=ReplayStatus.BUSINESS_OUTCOME,
                capability="x@1.0.0",
                run_id="r1",
            )
        ok = ReplayResult(
            status=ReplayStatus.HARD_FAILURE,
            capability="x@1.0.0",
            run_id="r1",
            failed_step="s3",
            expected="confirmation page",
            observed="error banner",
        )
        assert ok.status == ReplayStatus.HARD_FAILURE


class TestPolicyGate:
    def setup_method(self):
        self.engine = PolicyEngine(POLICY)

    def test_allows_normal_action(self):
        d = self.engine.check(
            ProposedAction("click", url="https://parabank.parasoft.com/parabank/index.htm",
                           target_description="Accounts Overview link")
        )
        assert d.verdict == Verdict.ALLOW

    def test_blocks_unlisted_domain(self):
        d = self.engine.check(ProposedAction("navigate", url="https://evil.example.com"))
        assert d.verdict == Verdict.BLOCK

    def test_blocks_unlisted_action_type(self):
        d = self.engine.check(ProposedAction("upload_file"))
        assert d.verdict == Verdict.BLOCK

    def test_blocks_admin_route_on_allowed_domain(self):
        d = self.engine.check(
            ProposedAction("navigate",
                           url="https://parabank.parasoft.com/parabank/admin.htm")
        )
        assert d.verdict == Verdict.BLOCK

    def test_risky_pattern_requires_confirmation(self):
        d = self.engine.check(
            ProposedAction("click", target_description="Open New Account button")
        )
        assert d.verdict == Verdict.CONFIRM

    def test_irreversible_pattern_blocked(self):
        d = self.engine.check(
            ProposedAction("click", target_description="Transfer Funds submit")
        )
        assert d.verdict == Verdict.BLOCK

    def test_declared_risk_cannot_be_downgraded(self):
        d = self.engine.check(
            ProposedAction("click", target_description="harmless looking button",
                           risk=RiskLevel.IRREVERSIBLE)
        )
        assert d.verdict == Verdict.BLOCK


class TestRedaction:
    def test_registered_secret_masked(self):
        r = Redactor()
        r.register("hunter2secret")
        assert "hunter2secret" not in r.scrub("typed hunter2secret into field")

    def test_ssn_pattern_masked(self):
        r = Redactor()
        assert "123-45-6789" not in r.scrub("ssn is 123-45-6789 ok")

    def test_password_kv_masked(self):
        r = Redactor()
        out = r.scrub("navigated to /login?username=bob&password=topsecret99")
        assert "topsecret99" not in out


class TestRecorderParameterization:
    """Every caller-supplied value must become a placeholder, whichever action
    consumed it. A select that freezes an account id is a silent reuse bug."""

    def _entry(self, tool, args, target=None, extracted=None):
        from cua.discovery import TranscriptEntry
        return TranscriptEntry(tool, args, description=args.get("description", ""),
                               target=target, extracted=extracted)

    def _target(self, value):
        from cua.schemas import LocatorCandidate, LocatorStrategy, Target
        return Target(description="field",
                      candidates=[LocatorCandidate(
                          strategy=LocatorStrategy.CSS, value=value)])

    def test_select_value_is_parameterized(self):
        from cua.discovery import DiscoveryOutcome
        from cua.recorder import record
        out = DiscoveryOutcome(success=True, proof_text="Account Opened")
        out.transcript = [
            self._entry("type", {"ref": "e1", "text": "john",
                                 "description": "user"}, self._target("#u")),
            self._entry("select", {"ref": "e2", "value": "13344",
                                   "description": "from account"},
                        self._target("#fromAccountId")),
        ]
        art = record(out, artifact_id="open_acct", name="n", description="d",
                     app="parabank", goal="g", entry_url="http://localhost/",
                     params={"username": "john", "account_id": "13344"},
                     secrets={"password": "demo"}, run_id="r", model="m")
        select_step = [s for s in art.steps if s.action == "select"][0]
        assert select_step.value == "{{account_id}}"
        type_step = [s for s in art.steps if s.action == "type"][0]
        assert type_step.value == "{{username}}"


class TestPlanVerification:
    def _plan(self):
        from cua.planner import Plan, PlannedParam, PlannedStep
        return Plan(goal="g", success_signal="Account Opened",
                    steps=[PlannedStep(intent="pick source", action="select",
                                       uses=["account_id"])],
                    parameters=[PlannedParam(name="account_id",
                                             description="source account")],
                    outputs=["balance"])

    def test_frozen_parameter_is_reported(self):
        from cua.planner import verify_against_plan
        art = Artifact.model_validate({
            "kind": "capability", "id": "x", "version": "1.0.0", "name": "x",
            "description": "d", "app": "parabank",
            "outputs": [{"name": "balance", "type": "number", "description": "d"}],
            "steps": [{"id": "s1", "action": "select", "description": "src",
                       "target": {"description": "d", "candidates": [
                           {"strategy": "css", "value": "#fromAccountId"}]},
                       "value": "13344"}],   # frozen, not {{account_id}}
            "success": {"id": "c", "description": "d",
                        "match": {"kind": "text_visible", "value": "Account Opened"}},
        })
        problems = verify_against_plan(art, self._plan())
        assert any("account_id" in p and "frozen" in p for p in problems)

    def test_clean_recording_reports_nothing(self):
        from cua.planner import verify_against_plan
        art = Artifact.model_validate({
            "kind": "capability", "id": "x", "version": "1.0.0", "name": "x",
            "description": "d", "app": "parabank",
            "inputs": [{"name": "account_id", "type": "string", "description": "d"}],
            "outputs": [{"name": "balance", "type": "number", "description": "d"}],
            "steps": [{"id": "s1", "action": "select", "description": "src",
                       "target": {"description": "d", "candidates": [
                           {"strategy": "css", "value": "#fromAccountId"}]},
                       "value": "{{account_id}}"}],
            "success": {"id": "c", "description": "d",
                        "match": {"kind": "text_visible", "value": "Account Opened"}},
        })
        assert verify_against_plan(art, self._plan()) == []


class TestParameterPrompting:
    """Planned parameters not passed on the command line are collected from
    the operator; sensitive ones never travel through argv."""

    def _plan(self):
        from cua.planner import Plan, PlannedParam, PlannedStep
        return Plan(goal="g", success_signal="Account Opened",
                    steps=[PlannedStep(intent="i", action="select")],
                    parameters=[
                        PlannedParam(name="account_id", description="source"),
                        PlannedParam(name="password", description="pw",
                                     sensitive=True)],
                    outputs=["new_account_id"])

    def test_prompts_only_for_missing_values(self, monkeypatch):
        from cua import planner
        asked = []
        monkeypatch.setattr("builtins.input",
                            lambda p: asked.append(p) or "13233")
        monkeypatch.setattr(planner.__dict__.setdefault("getpass", __import__("getpass")),
                            "getpass", lambda p: "secret-value")
        params, secrets = planner.prompt_for_params(
            self._plan(), {"username": "john"}, {})
        assert params["account_id"] == "13233"
        assert params["username"] == "john"       # supplied value untouched
        assert secrets["password"] == "secret-value"
        assert len(asked) == 1                    # only the non-secret prompt

    def test_nothing_to_ask_when_all_supplied(self):
        from cua.planner import prompt_for_params
        params, secrets = prompt_for_params(
            self._plan(), {"account_id": "1"}, {"password": "p"})
        assert params == {"account_id": "1"} and secrets == {"password": "p"}

    def test_unattended_run_fails_closed(self):
        from cua.planner import prompt_for_params
        with pytest.raises(RuntimeError, match="unattended"):
            prompt_for_params(self._plan(), {}, {}, interactive=False)


class TestOperatorSuppliedInputs:
    """A value the agent asked for mid-run becomes a first-class input of the
    capability, so every later replay is prompted for it."""

    def test_asked_value_is_parameterised_and_declared(self):
        from cua.discovery import DiscoveryOutcome, OperatorInput, TranscriptEntry
        from cua.recorder import record
        from cua.schemas import LocatorCandidate, LocatorStrategy, Target

        target = Target(description="routing field", candidates=[
            LocatorCandidate(strategy=LocatorStrategy.CSS, value="#routing")])
        out = DiscoveryOutcome(success=True, proof_text="Transfer Complete")
        out.transcript = [TranscriptEntry(
            "type", {"ref": "e1", "text": "021000021",
                     "description": "routing number field"}, target=target)]
        out.operator_inputs = [OperatorInput(
            name="routing_number",
            question="The ABA routing number for the destination bank")]

        art = record(out, artifact_id="x", name="x", description="d",
                     app="parabank", goal="g", entry_url="http://localhost/",
                     params={"routing_number": "021000021"}, secrets={},
                     run_id="r", model="m")

        step = art.steps[0]
        assert step.value == "{{routing_number}}"        # not frozen
        spec = [i for i in art.inputs if i.name == "routing_number"][0]
        assert "ABA routing number" in spec.description  # the agent's question
        assert "021000021" not in art.model_dump_json()  # value not stored

    def test_sensitive_asked_value_is_not_stored(self):
        from cua.discovery import DiscoveryOutcome, OperatorInput, TranscriptEntry
        from cua.recorder import record
        from cua.schemas import LocatorCandidate, LocatorStrategy, Target

        target = Target(description="pin", candidates=[
            LocatorCandidate(strategy=LocatorStrategy.CSS, value="#pin")])
        out = DiscoveryOutcome(success=True, proof_text="Done")
        out.transcript = [TranscriptEntry(
            "type", {"ref": "e1", "text": "9137", "description": "pin",
                     "sensitive": True}, target=target)]
        out.operator_inputs = [OperatorInput("card_pin", "The card PIN", True)]

        art = record(out, artifact_id="x", name="x", description="d",
                     app="parabank", goal="g", entry_url="http://localhost/",
                     params={}, secrets={"card_pin": "9137"},
                     run_id="r", model="m")
        blob = art.model_dump_json()
        assert "9137" not in blob
        assert [i for i in art.inputs if i.name == "card_pin"][0].sensitive


class TestAmbiguousParameterValues:
    """Two parameters holding the same value cannot be recovered from the
    recording, because the recorder matches on the typed text."""

    def test_recorder_cannot_distinguish_equal_values(self):
        from cua.discovery import DiscoveryOutcome, TranscriptEntry
        from cua.recorder import record
        from cua.schemas import LocatorCandidate, LocatorStrategy, Target

        def target(css):
            return Target(description="f", candidates=[LocatorCandidate(
                strategy=LocatorStrategy.CSS, value=css)])

        out = DiscoveryOutcome(success=True, proof_text="Profile Updated")
        out.transcript = [
            TranscriptEntry("type", {"ref": "e1", "text": "john",
                                     "description": "username"},
                            target=target("#username")),
            TranscriptEntry("type", {"ref": "e2", "text": "john",
                                     "description": "first name"},
                            target=target("#firstName")),
        ]
        art = record(out, artifact_id="x", name="x", description="d",
                     app="parabank", goal="g", entry_url="http://localhost/",
                     params={"username": "john", "first_name": "john"},
                     secrets={}, run_id="r", model="m")
        # Both steps collapse onto whichever name matched first: this is the
        # documented limitation the CLI now warns about before recording.
        values = {s.id: s.value for s in art.steps}
        assert len(set(values.values())) == 1

    def test_distinct_values_record_correctly(self):
        from cua.discovery import DiscoveryOutcome, TranscriptEntry
        from cua.recorder import record
        from cua.schemas import LocatorCandidate, LocatorStrategy, Target

        def target(css):
            return Target(description="f", candidates=[LocatorCandidate(
                strategy=LocatorStrategy.CSS, value=css)])

        out = DiscoveryOutcome(success=True, proof_text="Profile Updated")
        out.transcript = [
            TranscriptEntry("type", {"ref": "e1", "text": "john",
                                     "description": "username"},
                            target=target("#username")),
            TranscriptEntry("type", {"ref": "e2", "text": "Johnathan",
                                     "description": "first name"},
                            target=target("#firstName")),
        ]
        art = record(out, artifact_id="x", name="x", description="d",
                     app="parabank", goal="g", entry_url="http://localhost/",
                     params={"username": "john", "first_name": "Johnathan"},
                     secrets={}, run_id="r", model="m")
        assert art.steps[0].value == "{{username}}"
        assert art.steps[1].value == "{{first_name}}"


class TestInputDescriptionsFromPlan:
    """Replay prompts are only as good as the artifact's input descriptions.
    A generic label gets a wrong value typed into it."""

    def test_plan_description_beats_generic_fallback(self):
        from cua.discovery import DiscoveryOutcome, TranscriptEntry
        from cua.planner import Plan, PlannedParam, PlannedStep
        from cua.recorder import record
        from cua.schemas import LocatorCandidate, LocatorStrategy, Target

        plan = Plan(goal="g", success_signal="Profile Updated",
                    steps=[PlannedStep(intent="fill", action="type")],
                    parameters=[
                        PlannedParam(name="first_name",
                                     description="The customer's first name",
                                     example="John"),
                        PlannedParam(name="password", description="Login password",
                                     sensitive=True)],
                    outputs=[])
        out = DiscoveryOutcome(success=True, proof_text="Profile Updated")
        out.transcript = [TranscriptEntry(
            "type", {"ref": "e1", "text": "Johnathan", "description": "first name"},
            target=Target(description="f", candidates=[LocatorCandidate(
                strategy=LocatorStrategy.CSS, value="#firstName")]))]

        art = record(out, artifact_id="x", name="x", description="d",
                     app="parabank", goal="g", entry_url="http://localhost/",
                     params={"first_name": "Johnathan"},
                     secrets={"password": "demo"},
                     run_id="r", model="m", plan=plan)

        first = [i for i in art.inputs if i.name == "first_name"][0]
        assert first.description == "The customer's first name"
        assert first.example == "John"          # the plan's example, not the value
        assert "Johnathan" not in art.model_dump_json()
        pw = [i for i in art.inputs if i.name == "password"][0]
        assert pw.description == "Login password" and pw.sensitive


class TestParameterizationTolerance:
    """The model re-capitalises and re-spaces what it types. Exact matching
    would freeze those values into the flow as constants."""

    def _record(self, typed, supplied):
        from cua.discovery import DiscoveryOutcome, TranscriptEntry
        from cua.recorder import record
        from cua.schemas import LocatorCandidate, LocatorStrategy, Target
        out = DiscoveryOutcome(success=True, proof_text="Profile Updated")
        out.transcript = [TranscriptEntry(
            "type", {"ref": "e1", "text": typed, "description": "address"},
            target=Target(description="a", candidates=[LocatorCandidate(
                strategy=LocatorStrategy.CSS, value="#street")]))]
        return record(out, artifact_id="x", name="x", description="d",
                      app="parabank", goal="g", entry_url="http://localhost/",
                      params=supplied, secrets={}, run_id="r", model="m")

    def test_recapitalised_value_still_parameterised(self):
        art = self._record("145 West Polk", {"address": "145 west polk"})
        assert art.steps[0].value == "{{address}}"

    def test_respaced_value_still_parameterised(self):
        art = self._record("145  West   Polk", {"address": "145 West Polk"})
        assert art.steps[0].value == "{{address}}"

    def test_different_value_is_not_parameterised(self):
        art = self._record("999 Other Ave", {"address": "145 west polk"})
        assert art.steps[0].value == "999 Other Ave"


class TestSubstringSafety:
    """A short value must never be substituted inside an ordinary word:
    'ss' turned 'Password input field' into 'Pa{{last_name}}word input field'
    and broke the locator."""

    def _record(self, typed_desc, supplied):
        from cua.discovery import DiscoveryOutcome, TranscriptEntry
        from cua.recorder import record
        from cua.schemas import LocatorCandidate, LocatorStrategy, Target
        out = DiscoveryOutcome(success=True, proof_text="Profile Updated")
        out.transcript = [TranscriptEntry(
            "type", {"ref": "e1", "text": "value", "description": typed_desc},
            description=typed_desc,
            target=Target(description=typed_desc, candidates=[LocatorCandidate(
                strategy=LocatorStrategy.CSS, value="#f")]))]
        return record(out, artifact_id="x", name="x", description="d",
                      app="parabank", goal="g", entry_url="http://localhost/",
                      params=supplied, secrets={}, run_id="r", model="m")

    # Target descriptions are parameterised (they feed relative-text
    # locators), so that is where corruption showed up in the field.
    def test_short_value_does_not_corrupt_words(self):
        art = self._record("Password input field", {"last_name": "ss"})
        assert art.steps[0].target.description == "Password input field"

    def test_whole_word_value_still_matches(self):
        art = self._record("Chicago input field", {"city": "chicago"})
        assert "{{city}}" in art.steps[0].target.description

    def test_value_inside_longer_word_is_not_matched(self):
        art = self._record("Springfield input field", {"city": "spring"})
        assert art.steps[0].target.description == "Springfield input field"


class TestApprovalGate:
    """Verification is a gate, not an advisory."""

    def _draft(self, problems):
        from cua.schemas import Artifact
        art = Artifact.model_validate({
            "kind": "capability", "id": "x", "version": "1.0.0", "name": "x",
            "description": "d", "app": "parabank",
            "steps": [{"id": "s1", "action": "navigate", "description": "d",
                       "url": "http://localhost/"}],
            "success": {"id": "c", "description": "d",
                        "match": {"kind": "text_visible", "value": "ok"}}})
        art.provenance.verification_problems = problems
        return art

    def test_approve_refuses_while_problems_stand(self, tmp_path):
        from cua.store import ArtifactStore
        store = ArtifactStore(str(tmp_path))
        store.save(self._draft(["parameter 'x' is frozen as a constant"]))
        with pytest.raises(PermissionError, match="does not match"):
            store.approve("x@1.0.0")
        assert store.load("x@1.0.0").status.value == "draft"

    def test_force_approves_deliberately(self, tmp_path):
        from cua.store import ArtifactStore
        store = ArtifactStore(str(tmp_path))
        store.save(self._draft(["something"]))
        assert store.approve("x@1.0.0", force=True).status.value == "approved"

    def test_clean_draft_approves_normally(self, tmp_path):
        from cua.store import ArtifactStore
        store = ArtifactStore(str(tmp_path))
        store.save(self._draft([]))
        assert store.approve("x@1.0.0").status.value == "approved"
