"""A ref only means something in the observation that minted it.

Refs are re-stamped onto the DOM on every observation, so a ref carried over
from an earlier turn is not stale-but-harmless -- it silently addresses
whatever element now holds that number. That is how an extract the model
described as "Transaction results table" was recorded against the account
dropdown: the description came from the model, the locator came from the
element, and nothing downstream could tell they disagreed.
"""

import pytest

from cua.discovery import DiscoveryAgent
from cua.policy import PolicyEngine
from cua.redaction import Redactor

POLICY = PolicyEngine({
    "allowed_domains": ["localhost"],
    "allowed_actions": ["navigate", "click", "type", "select", "extract"],
    "risk_handling": {"risky": "confirm", "irreversible": "block"},
})


class Ev:
    def __init__(self):
        self.events = []

    def event(self, kind, **fields):
        self.events.append((kind, fields))

    def screenshot_path(self, label):
        return "shot.png"


class Element:
    def __init__(self, ref):
        self.ref = ref
        self.name = ref


class Observation:
    def __init__(self, refs):
        self.elements = [Element(r) for r in refs]


class Driver:
    """Records anything that reaches it. Nothing should, for a stale ref."""

    def __init__(self):
        self.touched = []

    def current_url(self):
        return "http://localhost:8080/parabank/findtrans.htm"

    def submits_ref(self, ref):
        return False

    def candidates_for_ref(self, ref, description):
        self.touched.append(("candidates", ref))
        raise AssertionError("should not resolve a stale ref")

    def read_ref(self, ref):
        self.touched.append(("read", ref))
        raise AssertionError("should not read a stale ref")

    def click_ref(self, ref):
        self.touched.append(("click", ref))
        raise AssertionError("should not click a stale ref")


def agent(ev=None):
    return DiscoveryAgent(Driver(), POLICY, ev or Ev(), Redactor(), model="t")


CURRENT = Observation(["e0", "e1", "e2"])


class TestAStaleRefIsRefused:

    @pytest.mark.parametrize("tool", ["click", "extract", "type", "select"])
    def test_the_driver_is_never_touched(self, tool):
        a = agent()
        args = {"ref": "e18", "description": "Transaction results table",
                "output": "transactions", "text": "x", "value": "x"}
        message, entry = a._act(tool, args, CURRENT)
        assert a.driver.touched == []
        assert entry is None, "nothing happened, so nothing is recorded"
        assert "e18" in message

    def test_the_model_is_told_why_and_what_to_do(self):
        message, _ = agent()._act(
            "extract", {"ref": "e18", "output": "transactions"}, CURRENT)
        assert "latest observation" in message
        assert "reassigned" in message

    def test_it_is_recorded_as_evidence(self):
        ev = Ev()
        a = agent(ev)
        a._act("extract", {"ref": "e18", "output": "transactions"}, CURRENT)
        assert any(k == "stale_ref" for k, _ in ev.events)

    def test_a_live_ref_is_not_refused(self):
        """The guard must not swallow a legitimate ref. A ref in the current
        observation is passed through to the driver, which is the whole point
        -- refusing everything would be a safe-looking way to break the agent."""
        a = agent()
        a._act("extract", {"ref": "e1", "output": "transactions"}, CURRENT)
        assert a.driver.touched == [("candidates", "e1")]

    def test_a_refless_action_is_unaffected(self):
        """navigate carries no ref, so there is nothing to validate."""
        a = agent()
        message, _ = a._act("navigate", {"url": "http://localhost/x"}, CURRENT)
        assert "latest observation" not in message
