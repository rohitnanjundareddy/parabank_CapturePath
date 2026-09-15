"""Policy gate: single enforcement point below both discovery and replay.

Every action, whether proposed by the LLM during discovery or read from an
artifact during replay, passes through PolicyEngine.check() before the driver
executes it. Nothing else in the system touches the browser, so there is
structurally no path around the gate.
"""

from __future__ import annotations

import fnmatch
import re
from dataclasses import dataclass
from enum import Enum
from typing import Optional
from urllib.parse import urlparse

import yaml

from .schemas import RiskLevel
from .textio import read_text


class Verdict(str, Enum):
    ALLOW = "allow"
    BLOCK = "block"
    CONFIRM = "confirm"  # requires human confirmation before executing


@dataclass(frozen=True)
class PolicyDecision:
    verdict: Verdict
    reason: str


@dataclass(frozen=True)
class ProposedAction:
    """Normalized view of an action for policy evaluation."""
    action_type: str                 # click | type | select | navigate | extract | press
    url: Optional[str] = None        # current or destination URL
    target_description: str = ""
    # Did the caller structurally determine whether this action commits state?
    #   True  - it really is the control that submits (driver.submits_ref)
    #   False - it structurally cannot commit: a link, a page load, a read
    #   None  - nobody looked (e.g. a legacy artifact); the label backstop applies
    # Three states, not two, because "known not to commit" and "unknown" must
    # not be conflated: collapsing them either re-opens this false positive or
    # silently drops the backstop that protects older recordings.
    commits: Optional[bool] = None
    risk: RiskLevel = RiskLevel.SAFE


class PolicyEngine:
    def __init__(self, config: dict):
        self.allowed_domains: list[str] = config.get("allowed_domains", [])
        self.allowed_actions: set[str] = set(config.get("allowed_actions", []))
        self.blocked_url_patterns: list[str] = config.get("blocked_url_patterns", [])
        # Regexes matched against target descriptions to classify risk when the
        # artifact or LLM did not already flag the step.
        self.risky_target_patterns: list[str] = config.get("risky_target_patterns", [])
        self.irreversible_target_patterns: list[str] = config.get(
            "irreversible_target_patterns", []
        )
        risk_cfg = config.get("risk_handling", {})
        self.on_risky: Verdict = Verdict(risk_cfg.get("risky", "confirm"))
        self.on_irreversible: Verdict = Verdict(risk_cfg.get("irreversible", "block"))

    @classmethod
    def from_yaml(cls, path: str) -> "PolicyEngine":
        return cls(yaml.safe_load(read_text(path)))

    # -- classification -----------------------------------------------------

    def classify_risk(self, action: ProposedAction) -> RiskLevel:
        """Effective risk = max(declared risk, pattern-inferred risk).

        The description patterns are a BACKSTOP, not the primary signal. They
        read a control's label, and a label is the one thing a vendor renames
        per tenant -- which is why commits are detected structurally instead,
        and why these patterns only ever tighten.

        A label cannot, however, be allowed to invent a commit out of an
        action that structurally cannot perform one. Matching on description
        alone classified "Transfer Funds link in the navigation menu" as
        irreversible and refused the agent the PAGE rather than the payment:
        the same proxy failure that emptied `risky_target_patterns`, where an
        "Open New Account link" turned a page change into a state change.

        So inference is skipped when the caller has established the action
        commits nothing. It still runs when nobody looked (`commits=None`),
        because an artifact recorded before structural detection existed
        carries risk=safe on its submits and has only this backstop.
        """
        if action.commits is False:
            # Declared risk still stands: this narrows what may be INFERRED,
            # and never downgrades what a caller or an artifact asserted.
            return action.risk
        text = action.target_description.lower()
        inferred = RiskLevel.SAFE
        if any(re.search(p, text) for p in self.risky_target_patterns):
            inferred = RiskLevel.RISKY
        if any(re.search(p, text) for p in self.irreversible_target_patterns):
            inferred = RiskLevel.IRREVERSIBLE
        order = [RiskLevel.SAFE, RiskLevel.RISKY, RiskLevel.IRREVERSIBLE]
        return max(action.risk, inferred, key=order.index)

    # -- the gate -----------------------------------------------------------

    def check(self, action: ProposedAction) -> PolicyDecision:
        # 1. Action type allowlist.
        if action.action_type not in self.allowed_actions:
            return PolicyDecision(
                Verdict.BLOCK, f"action type '{action.action_type}' is not allowlisted"
            )

        # 2. Domain allowlist (applies to any action that carries a URL).
        if action.url:
            host = urlparse(action.url).hostname or ""
            if not any(fnmatch.fnmatch(host, pat) for pat in self.allowed_domains):
                return PolicyDecision(
                    Verdict.BLOCK, f"domain '{host}' is outside the allowlist"
                )
            for pat in self.blocked_url_patterns:
                if re.search(pat, action.url):
                    return PolicyDecision(
                        Verdict.BLOCK, f"url matches blocked pattern '{pat}'"
                    )

        # 3. Risk handling.
        risk = self.classify_risk(action)
        if risk == RiskLevel.IRREVERSIBLE:
            return PolicyDecision(
                self.on_irreversible,
                f"irreversible action ('{action.target_description}')",
            )
        if risk == RiskLevel.RISKY:
            return PolicyDecision(
                self.on_risky, f"risky action ('{action.target_description}')"
            )

        return PolicyDecision(Verdict.ALLOW, "within policy")
