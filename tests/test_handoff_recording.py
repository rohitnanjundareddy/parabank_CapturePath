"""Recording a human handoff.

When retries run out and a person rescues the step by hand, what they did has
to become part of the recording -- otherwise the capability cannot replay the
rescued part, and verification refuses it with the hand-filled parameters
reported frozen. These tests pin what is recorded and, as importantly, what is
not."""

import pytest

from cua.discovery import DiscoveryAgent
from cua.driver import _target_from_info
from cua.escalation import EscalationController
from cua.policy import PolicyEngine
from cua.redaction import Redactor
from cua.schemas import LocatorStrategy

POLICY = PolicyEngine({
    "allowed_domains": ["localhost"],
    "allowed_actions": ["navigate", "click", "type", "select", "extract"],
    "risk_handling": {"risky": "confirm", "irreversible": "block"},
})


class Ev:
    def __init__(self): self.events = []
    def event(self, kind, **f): self.events.append((kind, f))
    def screenshot_path(self, label): return "shot.png"


def agent():
    return DiscoveryAgent(object(), POLICY, Ev(), Redactor(), model="t")


def info(**over):
    """What the page reports about an element, in the shape the shared
    inspection JS returns."""
    base = {"role": "combobox", "name": "To Account", "label": "To Account",
            "placeholder": None, "css": ["#toAccountId"], "rowAnchor": None,
            "cellCss": None, "labelAdjacent": None}
    base.update(over)
    return base


class TestHumanActionsBecomeSteps:

    def test_a_select_becomes_a_replayable_step(self):
        [entry] = agent()._transcribe_human_actions([
            {"type": "select", "desc": "To Account dropdown",
             "value": "12345", "info": info()}])
        assert entry.tool == "select"
        assert entry.args["value"] == "12345"
        assert entry.ok
        assert entry.target.candidates, "a step needs a way to find its element"

    def test_typing_carries_what_was_typed(self):
        [entry] = agent()._transcribe_human_actions([
            {"type": "type", "desc": "Amount field", "value": "1",
             "info": info(role="textbox", name="Amount")}])
        assert entry.tool == "type"
        assert entry.args["text"] == "1"

    def test_the_ladder_is_the_same_one_the_agent_would_record(self):
        """Both paths go through `_target_from_info`, so a step a human
        recovered is targeted no differently from one the agent performed."""
        [entry] = agent()._transcribe_human_actions([
            {"type": "click", "desc": "Transfer button", "info": info(
                role="button", name="Transfer", label=None, css=["#transfer"])}])
        strategies = [c.strategy for c in entry.target.candidates]
        assert LocatorStrategy.ROLE_NAME in strategies
        assert entry.target.candidates == _target_from_info(
            info(role="button", name="Transfer", label=None,
                 css=["#transfer"]), "Transfer button").candidates

    def test_order_is_preserved(self):
        entries = agent()._transcribe_human_actions([
            {"type": "type", "desc": "Amount", "value": "1", "info": info()},
            {"type": "select", "desc": "To Account", "value": "12345", "info": info()},
            {"type": "click", "desc": "Transfer", "info": info()},
        ])
        assert [e.tool for e in entries] == ["type", "select", "click"]


class TestWhatIsDeliberatelyNotRecorded:

    def test_a_password_typed_by_hand_is_never_a_step(self):
        """Authentication belongs to the login fragment. A secret has no place
        in a step even as a placeholder."""
        assert agent()._transcribe_human_actions([
            {"type": "type", "desc": "Password", "value": None,
             "sensitive": True, "info": info(role="textbox", name="Password")}]) == []

    def test_an_element_the_page_could_not_describe_is_dropped(self):
        """A step with no way to find its element again is not a step. An
        honest gap beats a replay that fails somewhere less obvious."""
        assert agent()._transcribe_human_actions([
            {"type": "click", "desc": "something", "info": None}]) == []

    def test_an_element_with_no_usable_locator_is_dropped(self):
        blank = info(role=None, name="", label=None, placeholder=None, css=[])
        assert agent()._transcribe_human_actions([
            {"type": "click", "desc": "", "info": blank}]) == []

    def test_unknown_event_types_are_ignored(self):
        assert agent()._transcribe_human_actions([
            {"type": "scroll", "desc": "x", "info": info()},
            {"type": "keydown", "desc": "y", "info": info()}]) == []

    def test_no_actions_is_not_an_error(self):
        assert agent()._transcribe_human_actions([]) == []


class TestTheControllerKeepsWhatHappened:

    def test_actions_are_available_after_a_handoff(self):
        """`intervene` returns the note, so the actions are kept alongside it
        rather than changing a signature every caller already uses."""
        esc = EscalationController(interactive=False)
        assert esc.last_actions == []

        recorded = [{"type": "click", "desc": "Transfer", "info": info()}]

        class D:
            def observe(self, screenshot_to=None): pass
            def current_url(self): return "http://localhost:8080/x"
            def cede_control(self): pass
            def resume_control(self): pass
            def drain_human_actions(self): return recorded

        note = esc.intervene(context="stuck", goal="g", step="s",
                             driver=D(), evidence=Ev())
        assert isinstance(note, str)          # unchanged for existing callers
        assert esc.last_actions == recorded
