"""Artifact schema: the contract between discovery (writes) and replay (reads).

Design principles, in priority order:
1. Replay must be able to execute an artifact with zero judgment. Every
   alternative (locator fallback, recovery rule, outcome branch) is declared
   here ahead of time and resolved by fixed rules at runtime.
2. Reviewable: a human reading the JSON should understand what the capability
   does, what it needs, what it returns, and what can go wrong.
3. Composable: capabilities reference reusable fragments (e.g. login) by
   pinned id and version. Composition is frozen at review time; replay only
   dereferences.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from enum import Enum
from typing import Annotated, Literal, Optional, Union

from pydantic import BaseModel, Field, field_validator, model_validator

SCHEMA_VERSION = "1.0"


# ---------------------------------------------------------------------------
# Targeting: how replay finds an element. A ranked ladder, not one selector.
# ---------------------------------------------------------------------------

class LocatorStrategy(str, Enum):
    ROLE_NAME = "role_name"          # accessibility role + accessible name (most stable)
    LABEL = "label"                  # form label text
    TEXT = "text"                    # visible text content
    PLACEHOLDER = "placeholder"      # input placeholder
    RELATIVE_TEXT = "relative_text"  # element near an anchor text (legacy tables)
    CSS = "css"                      # structural fallback, least preferred
    XPATH = "xpath"                  # last resort for hostile markup


class LocatorCandidate(BaseModel):
    """One rung of the targeting ladder. Replay tries candidates in order."""
    strategy: LocatorStrategy
    value: str
    # For role_name: value is the accessible name, role holds the ARIA role.
    role: Optional[str] = None
    # Free-text note from the recorder about why this candidate was chosen.
    rationale: Optional[str] = None


_TARGET_PARAM = re.compile(r"\{\{(\w+)\}\}")


class Target(BaseModel):
    """The element a step acts on."""
    description: str = Field(description="Human meaning, e.g. 'Username field on login form'")
    candidates: list[LocatorCandidate] = Field(min_length=1)

    def constrained_candidates(self) -> list[LocatorCandidate]:
        """Rungs that identify the SAME element as the preferred rung.

        A ladder is only a ladder if every rung points at the same thing. The
        preferred rung defines the identity: if it says "the link for account
        {{account_id}}", a rung that says "any account link" is not a
        fallback for it — it is a different element. Falling through to one
        does not degrade gracefully, it silently answers a question nobody
        asked, and returns another customer's balance as a confident success.

        So a rung is kept only if it carries at least the parameters the
        preferred rung carries. The preferred rung always qualifies, so the
        ladder can never be emptied by this.
        """
        required = set(_TARGET_PARAM.findall(self.candidates[0].value))
        if not required:
            return list(self.candidates)
        return [c for c in self.candidates
                if set(_TARGET_PARAM.findall(c.value)) >= required]


# ---------------------------------------------------------------------------
# Detectors and recovery: declared error handling, the 3.3 requirement.
# ---------------------------------------------------------------------------

class DetectorMatch(BaseModel):
    """A condition that identifies a known page state."""
    kind: Literal["text_visible", "element_visible", "url_matches", "element_absent"]
    value: str
    scope: Optional[Target] = None  # restrict text search to a region if needed


class OutcomeAction(BaseModel):
    """Terminal: report a known business outcome. Not a failure."""
    type: Literal["business_outcome"] = "business_outcome"
    code: str = Field(description="Stable machine code, e.g. ACCOUNT_NOT_FOUND")
    message: str


class RecoverAction(BaseModel):
    """Bounded recovery: perform a fixed remedy, then retry the step."""
    type: Literal["recover"] = "recover"
    remedy: Literal["retry", "dismiss_and_retry", "reload_and_retry"]
    dismiss_target: Optional[Target] = None  # required for dismiss_and_retry
    max_attempts: int = Field(default=2, ge=1, le=3)
    backoff_ms: int = Field(default=1000, ge=0, le=10000)

    @model_validator(mode="after")
    def _dismiss_needs_target(self) -> "RecoverAction":
        if self.remedy == "dismiss_and_retry" and self.dismiss_target is None:
            raise ValueError("dismiss_and_retry requires dismiss_target")
        return self


class EscalateAction(BaseModel):
    """Terminal for automation: hand the live session to a human."""
    type: Literal["escalate"] = "escalate"
    reason: str


class FailAction(BaseModel):
    """Terminal: hard failure with debuggable context."""
    type: Literal["hard_failure"] = "hard_failure"
    message: str


DetectorAction = Annotated[
    Union[OutcomeAction, RecoverAction, EscalateAction, FailAction],
    Field(discriminator="type"),
]


class Detector(BaseModel):
    """If `when` matches after (or while waiting on) a step, do `then`.

    Detectors are how replay separates expected business outcomes and
    recoverable conditions from hard failures, instead of blindly proceeding.
    """
    id: str
    when: DetectorMatch
    then: DetectorAction


# ---------------------------------------------------------------------------
# Steps
# ---------------------------------------------------------------------------

class WaitCondition(BaseModel):
    """What must be true before the step is considered settled."""
    kind: Literal["element_visible", "url_matches", "text_visible", "network_idle"]
    value: Optional[str] = None
    target: Optional[Target] = None
    timeout_ms: int = Field(default=10000, ge=100, le=60000)


class RiskLevel(str, Enum):
    SAFE = "safe"            # reversible: navigation, reads, form typing
    RISKY = "risky"          # state-changing but reviewable: form submits
    IRREVERSIBLE = "irreversible"  # money movement, deletion; blocked unless confirmed


class BaseStep(BaseModel):
    id: str
    description: str
    risk: RiskLevel = RiskLevel.SAFE
    wait_after: Optional[WaitCondition] = None
    detectors: list[Detector] = Field(default_factory=list)
    # What happens if every locator candidate and recovery rule is exhausted.
    on_exhausted: DetectorAction = Field(
        default_factory=lambda: FailAction(message="step exhausted all candidates")
    )


class NavigateStep(BaseStep):
    action: Literal["navigate"] = "navigate"
    url: str = Field(description="May contain {{param}} placeholders")


class ClickStep(BaseStep):
    action: Literal["click"] = "click"
    target: Target


class TypeStep(BaseStep):
    action: Literal["type"] = "type"
    target: Target
    value: str = Field(description="Literal text or {{param}} placeholder")
    sensitive: bool = Field(
        default=False,
        description="If true, the resolved value is never written to logs or evidence",
    )


class SelectStep(BaseStep):
    action: Literal["select"] = "select"
    target: Target
    value: str


class ExtractStep(BaseStep):
    action: Literal["extract"] = "extract"
    target: Target
    output: str = Field(description="Name of the declared output this fills")
    parse: Literal["text", "number", "currency"] = "text"


class RunSubflowStep(BaseStep):
    """Composition: execute a pinned fragment. Frozen at review time."""
    action: Literal["run_subflow"] = "run_subflow"
    ref: str = Field(description="Fragment reference, e.g. 'login@1.0.0'")
    bind_inputs: dict[str, str] = Field(
        default_factory=dict,
        description="Fragment input name -> parent param placeholder or literal",
    )

    @field_validator("ref")
    @classmethod
    def _pinned_version(cls, v: str) -> str:
        if "@" not in v:
            raise ValueError("subflow ref must pin a version, e.g. login@1.0.0")
        return v


Step = Annotated[
    Union[NavigateStep, ClickStep, TypeStep, SelectStep, ExtractStep, RunSubflowStep],
    Field(discriminator="action"),
]


# ---------------------------------------------------------------------------
# Contract: typed inputs and outputs (the capability signature)
# ---------------------------------------------------------------------------

class ParamType(str, Enum):
    STRING = "string"
    NUMBER = "number"
    BOOLEAN = "boolean"


class InputParam(BaseModel):
    name: str
    type: ParamType
    description: str
    required: bool = True
    sensitive: bool = False  # e.g. passwords: injected at runtime, never stored
    example: Optional[str] = None


class OutputField(BaseModel):
    name: str
    type: ParamType
    description: str


class BusinessOutcomeDecl(BaseModel):
    """Declared non-success results the caller must handle. Part of the contract."""
    code: str
    description: str


# ---------------------------------------------------------------------------
# The artifact
# ---------------------------------------------------------------------------

class ArtifactKind(str, Enum):
    CAPABILITY = "capability"  # invocable by an agent
    FRAGMENT = "fragment"      # reusable sub-flow, only invocable via run_subflow


class ApprovalStatus(str, Enum):
    DRAFT = "draft"        # recorded, not yet human-reviewed
    APPROVED = "approved"  # reviewed; eligible for unattended replay


class Checkpoint(BaseModel):
    """Asserted condition proving we reached the expected state."""
    id: str
    description: str
    match: DetectorMatch


class Provenance(BaseModel):
    recorded_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    recorded_by: str = "discovery_agent"
    discovery_run_id: Optional[str] = None
    model: Optional[str] = None  # which LLM discovered the flow
    # The plan a human approved before execution, kept so a reviewer can
    # compare what was intended against what was recorded.
    approved_plan: Optional[dict] = None
    plan_approved_by: Optional[str] = None
    # Problems found comparing the recording against the approved plan. A
    # non-empty list blocks approval: an advisory nobody acts on is not a gate.
    verification_problems: list[str] = Field(default_factory=list)
    # Detectors/checkpoints the hardening pass proposed but rejected, because
    # they were checked against the live page from the run that just
    # succeeded and did not hold up (see cua/harden.py). Kept on the artifact
    # itself so a reviewer can see what was tried and discarded, not just
    # what survived.
    hardening_rejected: list[str] = Field(default_factory=list)
    # Result of replaying this artifact once, immediately after recording,
    # before it was allowed to be approved. "A run succeeded" and "the
    # artifact of that run replays" are different claims; only this one
    # justifies approval.
    smoke_replay: Optional[str] = None
    # Non-blocking things a reviewer should look at. A step with a single
    # locator candidate has no fallback: if that one description of the
    # element stops matching, the step does not degrade, it fails — and an
    # extract that fails can be reported as a business outcome, turning a
    # missed locator into a confident wrong answer.
    review_notes: list[str] = Field(default_factory=list)


class Artifact(BaseModel):
    schema_version: str = SCHEMA_VERSION
    kind: ArtifactKind
    id: str = Field(pattern=r"^[a-z0-9_]+$")
    version: str = Field(pattern=r"^\d+\.\d+\.\d+$")
    name: str
    description: str
    app: str = Field(description="Target application id, e.g. 'parabank'")
    status: ApprovalStatus = ApprovalStatus.DRAFT
    provenance: Provenance = Field(default_factory=Provenance)

    inputs: list[InputParam] = Field(default_factory=list)
    outputs: list[OutputField] = Field(default_factory=list)
    business_outcomes: list[BusinessOutcomeDecl] = Field(default_factory=list)

    steps: list[Step] = Field(min_length=1)
    success: Checkpoint

    @property
    def ref(self) -> str:
        return f"{self.id}@{self.version}"

    @model_validator(mode="after")
    def _validate_consistency(self) -> "Artifact":
        # Every extract step must fill a declared output.
        declared = {o.name for o in self.outputs}
        for s in self.steps:
            if isinstance(s, ExtractStep) and s.output not in declared:
                raise ValueError(f"step {s.id} extracts undeclared output '{s.output}'")
        # Every business_outcome referenced by a detector must be declared.
        declared_codes = {b.code for b in self.business_outcomes}
        for s in self.steps:
            actions = [d.then for d in s.detectors] + [s.on_exhausted]
            for a in actions:
                if isinstance(a, OutcomeAction) and a.code not in declared_codes:
                    raise ValueError(
                        f"step {s.id} references undeclared business outcome '{a.code}'"
                    )
        # Step ids unique.
        ids = [s.id for s in self.steps]
        if len(ids) != len(set(ids)):
            raise ValueError("step ids must be unique")
        # One writer per output. Two extract steps filling the same output is
        # silent last-write-wins at replay time: the caller gets whichever ran
        # last with no error. Reject it at load time instead.
        writers = [s.output for s in self.steps if isinstance(s, ExtractStep)]
        if len(writers) != len(set(writers)):
            raise ValueError("multiple steps write the same output")
        return self


# ---------------------------------------------------------------------------
# Replay result contract: exactly three top-level statuses.
# ---------------------------------------------------------------------------

class ReplayStatus(str, Enum):
    SUCCESS = "success"
    BUSINESS_OUTCOME = "business_outcome"
    HARD_FAILURE = "hard_failure"


class StepReport(BaseModel):
    step_id: str
    started_at: datetime
    ended_at: datetime
    attempts: int = 1
    locator_used: Optional[LocatorStrategy] = None
    recovered: bool = False
    note: Optional[str] = None


class OutputEvidence(BaseModel):
    """What replay actually read off the live page for one output, captured
    at extraction time, before parsing. Lets a caller or reviewer audit a
    returned value (e.g. a balance) without re-running the capability or
    trusting the number in isolation."""
    source_text: str
    locator_used: Optional[LocatorStrategy] = None
    captured_at: datetime


class ReplayResult(BaseModel):
    status: ReplayStatus
    capability: str                      # id@version
    run_id: str
    outputs: dict[str, str | float | bool] = Field(default_factory=dict)
    output_evidence: dict[str, OutputEvidence] = Field(default_factory=dict)
    outcome_code: Optional[str] = None   # set when status == BUSINESS_OUTCOME
    outcome_message: Optional[str] = None
    # Debug context, set when status == HARD_FAILURE:
    failed_step: Optional[str] = None
    expected: Optional[str] = None
    observed: Optional[str] = None
    evidence_dir: Optional[str] = None
    step_reports: list[StepReport] = Field(default_factory=list)
    escalations: int = 0
    # Step ids that succeeded on a fallback locator rather than the
    # preferred (first) candidate. Not a failure — replay still worked — but
    # it is the drift signal: a capability sliding down its ladder degrades
    # before it breaks.
    degraded_steps: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _status_fields(self) -> "ReplayResult":
        if self.status == ReplayStatus.BUSINESS_OUTCOME and not self.outcome_code:
            raise ValueError("business_outcome result requires outcome_code")
        if self.status == ReplayStatus.HARD_FAILURE and not self.failed_step:
            raise ValueError("hard_failure result requires failed_step")
        return self
