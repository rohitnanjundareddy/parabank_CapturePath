"""Risk classification: a control's LABEL may refine what is known about an
action, but must never invent a state change out of one that cannot perform
it. Structural detection is the primary signal; these patterns are a backstop
for recordings made before it existed."""

import pytest

from cua.policy import PolicyEngine, ProposedAction, Verdict
from cua.schemas import RiskLevel

POLICY = PolicyEngine({
    "allowed_domains": ["localhost", "parabank.parasoft.com"],
    "allowed_actions": ["navigate", "click", "type", "select", "extract", "press"],
    "blocked_url_patterns": [],
    "irreversible_target_patterns": ["transfer"],
    "risky_target_patterns": ["open.*account"],
    "risk_handling": {"risky": "confirm", "irreversible": "block"},
})

URL = "http://localhost:8080/parabank/index.htm"


class TestLabelsDoNotInventCommits:
    """The reported bug: the agent could not reach the Transfer Funds PAGE,
    because a navigation link whose label contains "transfer" was classified
    irreversible and refused."""

    def test_a_navigation_link_is_not_an_irreversible_action(self):
        d = POLICY.check(ProposedAction(
            "click", url=URL,
            target_description="Transfer Funds link in the navigation menu",
            commits=False))                       # driver: not a submit control
        assert d.verdict == Verdict.ALLOW

    def test_the_control_that_actually_submits_is_still_blocked(self):
        """The whole point: the payment is refused, not the page."""
        d = POLICY.check(ProposedAction(
            "click", url=URL, target_description="Transfer button",
            risk=RiskLevel.RISKY, commits=True))
        assert d.verdict == Verdict.BLOCK

    def test_the_same_holds_for_the_risky_class(self):
        link = POLICY.check(ProposedAction(
            "click", url=URL,
            target_description="Open New Account link", commits=False))
        button = POLICY.check(ProposedAction(
            "click", url=URL,
            target_description="Open New Account button", commits=True))
        assert link.verdict == Verdict.ALLOW
        assert button.verdict == Verdict.CONFIRM

    @pytest.mark.parametrize("action_type", ["navigate", "extract"])
    def test_page_loads_and_reads_commit_nothing(self, action_type):
        d = POLICY.check(ProposedAction(
            action_type, url=URL,
            target_description="Transfer Funds page", commits=False))
        assert d.verdict == Verdict.ALLOW


class TestTheBackstopSurvives:
    """`commits=None` means nobody looked. An artifact recorded before
    structural detection carries risk=safe on its submits and has only the
    label to go on, so the old behaviour has to stay exactly as it was."""

    def test_unknown_still_infers_from_the_label(self):
        d = POLICY.check(ProposedAction(
            "click", url=URL, target_description="Transfer Funds submit"))
        assert d.verdict == Verdict.BLOCK          # unchanged from before

    def test_commits_defaults_to_unknown(self):
        """A caller that says nothing must get the backstop, not a bypass."""
        assert ProposedAction("click").commits is None

    def test_a_declared_risk_is_never_downgraded(self):
        """`commits=False` narrows what may be INFERRED. What an artifact or
        a caller ASSERTED still stands."""
        d = POLICY.check(ProposedAction(
            "click", url=URL, target_description="harmless looking link",
            risk=RiskLevel.IRREVERSIBLE, commits=False))
        assert d.verdict == Verdict.BLOCK


class TestUrlRulesAreUnaffected:
    """Refusing a DESTINATION is the URL allowlist's job, and it still runs
    for a navigate that commits nothing."""

    def test_a_blocked_url_still_blocks_a_harmless_looking_navigate(self):
        engine = PolicyEngine({
            "allowed_domains": ["localhost"],
            "allowed_actions": ["navigate"],
            "blocked_url_patterns": [r"/transfer\.htm"],
            "risk_handling": {"risky": "confirm", "irreversible": "block"},
        })
        d = engine.check(ProposedAction(
            "navigate", url="http://localhost:8080/parabank/transfer.htm",
            target_description="a perfectly innocent page", commits=False))
        assert d.verdict == Verdict.BLOCK

    def test_domain_allowlist_still_applies(self):
        d = POLICY.check(ProposedAction(
            "navigate", url="https://evil.example.com",
            target_description="nothing to see here", commits=False))
        assert d.verdict == Verdict.BLOCK


class TestClassificationIsConsistentEverywhere:
    """The gate and the auto-approve check must agree. Two places classifying
    the same step by different rules is how a capability that commits nothing
    gets held back from approval by the wording of a navigation step."""

    def _steps(self):
        return [("navigate", "Open the Transfer Funds page", RiskLevel.SAFE),
                ("extract", "Read the transfer confirmation", RiskLevel.SAFE),
                ("click", "Transfer button", RiskLevel.RISKY)]

    def test_navigate_and_extract_are_not_state_changing(self):
        for action, desc, risk in self._steps()[:2]:
            r = POLICY.classify_risk(ProposedAction(
                action, target_description=desc, risk=risk, commits=False))
            assert r == RiskLevel.SAFE, f"{action} classified {r}"

    def test_a_declared_risky_submit_still_is(self):
        action, desc, risk = self._steps()[2]
        r = POLICY.classify_risk(ProposedAction(
            action, target_description=desc, risk=risk))
        assert r != RiskLevel.SAFE
