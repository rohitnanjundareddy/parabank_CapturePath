"""Escalation gating: when a human is worth interrupting, and when handing
over the browser would either waste their time or defeat the policy gate."""

import pytest

from cua.discovery import DiscoveryAgent
from cua.policy import PolicyEngine, Verdict
from cua.redaction import Redactor


class FakeEvidence:
    def __init__(self):
        self.events = []

    def event(self, kind, **fields):
        self.events.append((kind, fields))

    def screenshot_path(self, label):
        return f"/tmp/{label}.png"


class FakeEscalation:
    """Counts handovers. Present but never expected to be called in the
    cases that matter here."""

    def __init__(self):
        self.calls = []

    def intervene(self, **kw):
        self.calls.append(kw)
        return "did the thing"

    def confirm(self, question, committing=None):
        return False


class FakeDriver:
    def current_url(self):
        return "http://localhost:8080/parabank/transfer.htm"


POLICY = PolicyEngine({
    "allowed_domains": ["localhost"],
    "allowed_actions": ["navigate", "click", "type", "extract"],
    "blocked_url_patterns": [r"/transfer\.htm"],
    "irreversible_target_patterns": ["transfer"],
    "risk_handling": {"risky": "confirm", "irreversible": "block"},
})


def make_agent(escalation=None, max_escalations=2,
               escalate_policy_blocks=True):
    return DiscoveryAgent(FakeDriver(), POLICY, FakeEvidence(), Redactor(),
                          model="test", escalation=escalation,
                          max_escalations=max_escalations,
                          escalate_policy_blocks=escalate_policy_blocks)


class TestPolicyBlocksAreNeverEscalated:
    """The hole this closes: the gate refuses to transfer funds, the agent
    declares itself stuck, and the operator is handed the live browser and
    asked to perform the transfer by hand. The refusal has to end the run."""

    def test_a_policy_block_refuses_the_handover(self):
        esc = FakeEscalation()
        agent = make_agent(esc, escalate_policy_blocks=False)
        agent._blocked_by_policy.append("irreversible action")

        why = agent._why_not_escalate("cannot reach the Transfer Funds page")
        assert why is not None
        assert "NOT ESCALATED" in why
        assert "config/policy.yaml" in why      # names the deliberate fix
        assert esc.calls == []                  # nobody was interrupted

    def test_the_gate_records_the_block_for_that_decision(self):
        agent = make_agent(FakeEscalation(), escalate_policy_blocks=False)
        allowed, message = agent._policy_gate("navigate", "Transfer Funds link")
        assert not allowed
        assert agent._blocked_by_policy                     # remembered
        assert agent._why_not_escalate("stuck") is not None  # and acted on

    def test_progress_clears_the_block_memory(self):
        """A block that the agent then routed around is not why it is stuck
        later, so it must not veto a legitimate handover forever."""
        agent = make_agent(FakeEscalation(), escalate_policy_blocks=False)
        agent._blocked_by_policy.append("irreversible action")
        agent._blocked_by_policy.clear()        # what a successful action does
        assert agent._why_not_escalate("a genuinely new problem") is None


class TestPolicyBlockOverride:
    """`--escalate-policy-blocks` restores the documented escalation demo:
    the operator is handed the browser and acts on their own authority. The
    budget still applies, so it cannot become the old unbounded loop."""

    def test_the_override_allows_the_handover(self):
        agent = make_agent(FakeEscalation(), escalate_policy_blocks=True)
        agent._blocked_by_policy.append("irreversible action")
        assert agent._why_not_escalate("blocked by policy") is None

    def test_the_override_is_still_bounded_by_the_budget(self):
        agent = make_agent(FakeEscalation(), max_escalations=1,
                           escalate_policy_blocks=True)
        agent._blocked_by_policy.append("irreversible action")
        agent._escalated_reasons.append("first handover")
        why = agent._why_not_escalate("blocked by policy, again")
        assert why is not None and "budget" in why


class TestEscalationBudget:
    """Without a cap, `declare_stuck` -> intervene -> 'continue from the
    current page state' loops for as long as the operator keeps typing."""

    def test_the_first_handover_is_allowed(self):
        agent = make_agent(FakeEscalation())
        assert agent._why_not_escalate("something a human can fix") is None

    def test_the_budget_stops_the_loop(self):
        agent = make_agent(FakeEscalation(), max_escalations=2)
        agent._escalated_reasons += ["first problem", "second problem"]
        why = agent._why_not_escalate("third problem")
        assert why is not None and "budget" in why
        assert "--max-escalations" in why       # says how to override

    def test_the_same_blocker_is_not_escalated_twice(self):
        agent = make_agent(FakeEscalation(), max_escalations=5)
        reason = "The Transfer Funds page is blocked"
        agent._escalated_reasons.append(" ".join(reason.lower().split()))
        why = agent._why_not_escalate(reason + "   ")   # whitespace-insensitive
        assert why is not None and "same blocker" in why

    def test_no_operator_attached_is_stated_plainly(self):
        agent = make_agent(escalation=None)
        assert "No operator" in agent._why_not_escalate("stuck")


class TestNavigationIsJudgedByDestination:
    """Discovery must judge WHERE A NAVIGATION IS GOING, exactly as replay
    does. Judging the current page instead lets the agent walk onto a blocked
    route and only discover it is forbidden once standing on it -- every
    later action refused for a reason that reads as though it were about the
    action rather than the address.

    This was covered by accident until the label backstop stopped classifying
    navigations: the description happened to contain a forbidden word. An
    accident is not enforcement.
    """

    def _agent(self, here):
        class Driver:
            def current_url(self):
                return here
        agent = make_agent(FakeEscalation())
        agent.driver = Driver()
        return agent

    def test_navigating_to_a_blocked_route_is_refused(self):
        agent = self._agent("http://localhost:8080/parabank/index.htm")
        allowed, message = agent._policy_gate(
            "navigate", "a perfectly innocent page",
            url="http://localhost:8080/parabank/transfer.htm")
        assert not allowed
        assert "blocked pattern" in message

    def test_a_benign_destination_from_a_blocked_page_is_allowed(self):
        """The corollary: standing on a blocked page must not freeze the
        agent in place. Navigating AWAY is judged on where it is going."""
        agent = self._agent("http://localhost:8080/parabank/transfer.htm")
        allowed, _ = agent._policy_gate(
            "navigate", "back to the overview",
            url="http://localhost:8080/parabank/overview.htm")
        assert allowed

    def test_non_navigation_actions_are_judged_where_they_act(self):
        """For everything else the current page IS the address being acted
        upon, so a blocked page still refuses what happens on it."""
        agent = self._agent("http://localhost:8080/parabank/transfer.htm")
        allowed, message = agent._policy_gate("type", "amount field")
        assert not allowed
        assert "blocked pattern" in message


class TestPolicyBlocksEscalateByDefault:
    """The gate stops AUTOMATION. An attended operator may still act on their
    own authority, which is what the escalation surface is for -- bounded, so
    it cannot become the unbounded handover loop again."""

    def test_default_hands_over(self):
        agent = make_agent(FakeEscalation())
        agent._blocked_by_policy.append("irreversible action")
        assert agent._why_not_escalate("blocked by policy") is None

    def test_still_bounded_by_the_budget(self):
        agent = make_agent(FakeEscalation(), max_escalations=1)
        agent._blocked_by_policy.append("irreversible action")
        agent._escalated_reasons.append("the first handover")
        why = agent._why_not_escalate("blocked by policy, again")
        assert why is not None and "budget" in why

    def test_opting_out_restores_the_refusal(self):
        agent = make_agent(FakeEscalation(), escalate_policy_blocks=False)
        agent._blocked_by_policy.append("irreversible action")
        why = agent._why_not_escalate("blocked by policy")
        assert why is not None and "NOT ESCALATED" in why
