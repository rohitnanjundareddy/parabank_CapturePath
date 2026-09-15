"""Replay: the production execution path. No LLM anywhere in this module.

Pipeline: load artifact -> flatten pinned subflows -> substitute {{params}} ->
execute steps through the policy gate and the driver's candidate ladder ->
evaluate detectors -> verify the success checkpoint -> return exactly one of
SUCCESS / BUSINESS_OUTCOME / HARD_FAILURE.

Per-step evaluation order when something goes wrong:
1. Do any declared detectors match the current page? Their declared action
   wins (business outcome, bounded recovery, escalate, fail).
2. Otherwise, the step's on_exhausted action applies.
Silently skipping a failed step is not an option by construction.
"""

from __future__ import annotations

import re
import time
from datetime import datetime, timezone

from .evidence import EvidenceLog
from .policy import PolicyEngine, ProposedAction, Verdict
from .schemas import (
    Artifact,
    ApprovalStatus,
    ClickStep,
    Detector,
    EscalateAction,
    ExtractStep,
    FailAction,
    NavigateStep,
    OutcomeAction,
    OutputEvidence,
    RecoverAction,
    ReplayResult,
    ReplayStatus,
    RunSubflowStep,
    SelectStep,
    Step,
    StepReport,
    TypeStep,
)
from .store import ArtifactStore

_PLACEHOLDER = re.compile(r"\{\{(\w+)\}\}")


class _Unreadable(Exception):
    """The element WAS found; its content could not be interpreted.

    Distinct from absence on purpose. A step's `on_exhausted` business
    outcome means "the thing the caller asked for is not there" — so it must
    not swallow a value that was sitting right there and merely failed to
    parse. Reading a whole transaction table and typing it as one currency
    amount produced exactly that: a parse error reported as "this account
    has no transactions", with twelve of them on screen.
    """


class _Terminal(Exception):
    """Internal control flow carrying the final result."""
    def __init__(self, result: ReplayResult):
        self.result = result


def substitute(text: str, params: dict[str, str]) -> str:
    def repl(m: re.Match) -> str:
        name = m.group(1)
        if name not in params:
            raise KeyError(f"missing required parameter '{name}'")
        return params[name]
    return _PLACEHOLDER.sub(repl, text)


def flatten(artifact: Artifact, store: ArtifactStore,
            allow_draft: bool, _seen: frozenset[str] = frozenset()) -> list[Step]:
    """Resolve run_subflow steps by dereferencing pinned fragment versions.
    Purely mechanical: no choice happens here, which is what keeps replay
    reviewable. bind_inputs rewrites the fragment's own placeholders into the
    parent's placeholders or literals before inlining."""
    # A capability that references itself — which the planner will propose if
    # it is shown its own id in the reuse catalog — recurses until the stack
    # dies. Refuse it as a clear error instead.
    if artifact.ref in _seen:
        raise ValueError(
            f"circular subflow reference: {artifact.ref} is reached from itself "
            f"(chain: {' -> '.join(sorted(_seen))})")
    _seen = _seen | {artifact.ref}

    out: list[Step] = []
    for step in artifact.steps:
        if not isinstance(step, RunSubflowStep):
            out.append(step)
            continue
        frag = store.load(step.ref)
        if frag.status != ApprovalStatus.APPROVED and not allow_draft:
            raise PermissionError(
                f"fragment {frag.ref} is draft; approve it or pass allow_draft")
        binding = dict(step.bind_inputs)
        for fs in flatten(frag, store, allow_draft, _seen):
            fs = fs.model_copy(deep=True)
            fs.id = f"{step.id}.{fs.id}"
            _rewrite_placeholders(fs, binding)
            out.append(fs)
    return out


def _rewrite_placeholders(step: Step, binding: dict[str, str]) -> None:
    def rw(text: str) -> str:
        return _PLACEHOLDER.sub(
            lambda m: binding.get(m.group(1), m.group(0)), text)
    if isinstance(step, NavigateStep):
        step.url = rw(step.url)
    if isinstance(step, (ClickStep, TypeStep, SelectStep, ExtractStep)):
        for c in step.target.candidates:
            c.value = rw(c.value)
    if isinstance(step, (TypeStep, SelectStep)):
        step.value = rw(step.value)


def _parse(value: str, mode: str) -> str | float:
    """Turn raw page text into a typed value, failing loudly rather than
    guessing. A parse that silently drops information (an accounting-style
    negative, an unparseable remainder) is exactly the kind of wrong-answer
    risk detectors and checkpoints are meant to catch elsewhere in this
    module — do not let it slip through quietly here instead."""
    if mode not in ("currency", "number"):
        return value
    text = value.strip()
    # Accounting notation for a negative amount, e.g. "($50.00)". The digit
    # strip below would otherwise drop the parens and silently flip the sign.
    negative = text.startswith("(") and text.endswith(")")
    digits = re.sub(r"[^\d.\-]", "", text)
    if not digits or digits in ("-", "."):
        raise ValueError(f"'{value}' does not contain a parseable {mode}")
    try:
        number = float(digits)
    except ValueError as exc:
        raise ValueError(
            f"'{value}' is not a valid {mode} (stripped to '{digits}')") from exc
    if negative and number > 0:
        number = -number
    return number


class ReplayEngine:
    def __init__(self, driver, policy: PolicyEngine, store: ArtifactStore,
                 evidence: EvidenceLog, escalation=None,
                 escalate_on_failure: bool = False):
        self.driver, self.policy = driver, policy
        self.store, self.ev = store, evidence
        self.escalation = escalation
        # Opt-in fallback for a hard failure the artifact did not anticipate
        # (no matching detector, no declared escalate): offer a human one
        # handoff before giving up, instead of ending the run. Declared
        # EscalateAction steps and policy BLOCKs are unaffected by this flag
        # — a block is a deliberate safety decision, not an anticipation gap.
        self.escalate_on_failure = escalate_on_failure

    def run(self, ref: str, params: dict[str, str],
            allow_draft: bool = False) -> ReplayResult:
        artifact = self.store.load(ref)
        if artifact.status != ApprovalStatus.APPROVED and not allow_draft:
            raise PermissionError(
                f"{artifact.ref} is draft; approve it or pass allow_draft")

        missing = [i.name for i in artifact.inputs
                   if i.required and i.name not in params]
        if missing:
            raise KeyError(f"missing required parameters: {missing}")

        self._sensitive = {i.name for i in artifact.inputs if i.sensitive}
        self._entered: dict[str, str] = {}
        steps = flatten(artifact, self.store, allow_draft)
        result = ReplayResult(status=ReplayStatus.SUCCESS, capability=artifact.ref,
                              run_id=self.ev.run_id, evidence_dir=self.ev.dir)
        self.ev.event("replay_start", capability=artifact.ref,
                      params=list(params.keys()), steps=len(steps))
        try:
            for step in steps:
                report = self._run_step(step, params, result)
                result.step_reports.append(report)

            # Success checkpoint: never assume the last click worked.
            check = artifact.success.match.model_copy()
            check.value = substitute(check.value, params)
            if not self.driver.matches(check):
                if self.escalate_on_failure and self.escalation is not None:
                    result.escalations += 1
                    note = self.escalation.intervene(
                        context="success checkpoint not met after all steps completed",
                        goal=result.capability, step="success_checkpoint",
                        driver=self.driver, evidence=self.ev)
                    if not self.driver.matches(check):
                        self._fail(result, step_id="success_checkpoint",
                                   expected=f"{check.kind}: '{check.value}'",
                                   observed=f"still not met after operator handoff: {note}")
                else:
                    self._fail(result, step_id="success_checkpoint",
                               expected=f"{check.kind}: '{check.value}'",
                               observed=f"url={self.driver.current_url()}")
            self.ev.event("replay_success", outputs=list(result.outputs.keys()))
            return result
        except _Terminal as t:
            return t.result

    # ------------------------------------------------------------------ steps

    def _run_step(self, step: Step, params: dict, result: ReplayResult) -> StepReport:
        started = datetime.now(timezone.utc)
        attempts, recovered = 0, False
        recover_budget: dict[str, int] = {}
        locator_used = None
        escalated = False       # at most one handoff per step
        human_note = None

        # Policy gate: identical enforcement to discovery. For navigation the
        # DESTINATION is what policy must judge, not where we currently are.
        policy_url = (substitute(step.url, params)
                      if isinstance(step, NavigateStep)
                      else self.driver.current_url())
        action_type = getattr(step, "action", "click")
        decision = self.policy.check(ProposedAction(
            action_type,
            url=policy_url,
            target_description=step.description, risk=step.risk,
            # A page load and a read commit nothing, however old the
            # recording. A click's structural verdict was frozen into
            # step.risk at record time, but a legacy recording cannot be told
            # apart from a genuinely safe one, so it keeps the backstop.
            commits=False if action_type in ("navigate", "extract") else None))
        if decision.verdict == Verdict.BLOCK:
            self._fail(result, step.id, expected="action within policy",
                       observed=f"blocked: {decision.reason}")
        if decision.verdict == Verdict.CONFIRM:
            # Show the operator what is actually being committed. Secrets are
            # never displayed, and never need to be: nobody approves a
            # transfer on the strength of the password.
            committing = dict(getattr(self, "_entered", {}))
            ok = self.escalation.confirm(
                f"step {step.id} '{step.description}': {decision.reason}",
                committing=committing) if self.escalation else False
            self.ev.event("risk_confirmation", step=step.id, approved=ok)
            if not ok:
                self._fail(result, step.id, expected="human confirmation",
                           observed="declined, or unattended run with a "
                                    "state-changing step")

        while True:
            attempts += 1
            try:
                locator_used = self._act(step, params, result)
                if step.wait_after:
                    cond = step.wait_after.model_copy()
                    if cond.value:
                        cond.value = substitute(cond.value, params)
                    if not self.driver.wait_for(cond):
                        raise TimeoutError(
                            f"wait_after not met: {cond.kind} '{cond.value}'")
                # Even on apparent success, declared detectors get a look:
                # an error banner can render alongside a loaded page.
                self._check_detectors(step, params, result, recover_budget,
                                      post_success=True)
                # Drift is a preferred rung that stopped MATCHING, not one the
                # driver refused as unusable — the latter means the recording
                # described the wrong element, which is a bug to fix, not a
                # page that moved under us.
                missing = getattr(self.driver, "last_missing_rungs", None)
                if (locator_used is not None and getattr(step, "target", None)
                        and missing is not None and missing()):
                    result.degraded_steps.append(step.id)
                ambiguous = getattr(self.driver, "last_ambiguous_rungs", None)
                if ambiguous is not None and ambiguous():
                    # Not a failure — a rung that cannot tell two controls
                    # apart was skipped for one that can. Worth recording,
                    # because it means the artifact describes this element
                    # more loosely than the page requires.
                    self.ev.event("ambiguous_rungs_skipped", step=step.id,
                                  rungs=ambiguous())
                self.ev.event("step_ok", step=step.id, attempts=attempts,
                              locator=locator_used.value if locator_used else None)
                return StepReport(step_id=step.id, started_at=started,
                                  ended_at=datetime.now(timezone.utc),
                                  attempts=attempts, locator_used=locator_used,
                                  recovered=recovered, note=human_note)
            except _Terminal:
                raise
            except Exception as e:
                self.ev.event("step_error", step=step.id, attempts=attempts,
                              error=str(e))
                action = self._check_detectors(step, params, result,
                                               recover_budget) \
                    or step.on_exhausted
                # on_exhausted declares what ABSENCE means. If the element was
                # found and only its content defeated us, reporting "not
                # found" would hand the caller a confident wrong answer — a
                # table of twelve transactions reported as "no transactions".
                if isinstance(e, _Unreadable) and isinstance(action, OutcomeAction):
                    action = FailAction(message=str(e))
                if isinstance(action, RecoverAction):
                    budget_key = f"{step.id}:{action.remedy}"
                    used = recover_budget.get(budget_key, 0)
                    if used < action.max_attempts:
                        recover_budget[budget_key] = used + 1
                        recovered = True
                        self._apply_remedy(action)
                        continue
                    action = step.on_exhausted  # ladder exhausted, next layer

                # An unattended failure the artifact never anticipated: offer
                # one human handoff before giving up, if the operator opted
                # in. A declared EscalateAction is untouched by this — it
                # already gets a handoff below regardless of the flag.
                if (isinstance(action, FailAction) and self.escalate_on_failure
                        and self.escalation is not None and not escalated):
                    action = EscalateAction(reason=action.message)

                if isinstance(action, EscalateAction):
                    # Hand the live session to a human, then RESUME the rest of
                    # the path. One handoff per step: if the same step fails
                    # again afterwards, escalating a second time would loop.
                    if escalated or self.escalation is None:
                        self._fail(result, step.id,
                                   expected=step.description,
                                   observed=f"escalation did not resolve: {e}"
                                   if escalated else
                                   "escalation required but no operator available")
                    escalated = True
                    result.escalations += 1
                    human_note = self.escalation.intervene(
                        context=f"{action.reason} (step failed: {e})",
                        goal=result.capability, step=step.id,
                        driver=self.driver, evidence=self.ev)
                    # Re-verify rather than assume: if the step's own condition
                    # now holds, the human completed it and we move on; if not,
                    # they unblocked the path and we retry the step ourselves.
                    if step.wait_after:
                        cond = step.wait_after.model_copy()
                        if cond.value:
                            cond.value = substitute(cond.value, params)
                        if self.driver.wait_for(cond):
                            self.ev.event("resumed_after_handoff", step=step.id,
                                          disposition="human completed the step")
                            return StepReport(
                                step_id=step.id, started_at=started,
                                ended_at=datetime.now(timezone.utc),
                                attempts=attempts, locator_used=locator_used,
                                recovered=True,
                                note=f"completed by operator: {human_note}")
                    self.ev.event("resumed_after_handoff", step=step.id,
                                  disposition="retrying step after handoff")
                    continue

                self._terminal_action(action, step, result, observed=str(e),
                                      params=params)

    def _act(self, step: Step, params: dict, result: ReplayResult):
        d = self.driver
        if isinstance(step, NavigateStep):
            self._entered = {}          # new page, new form
            # The driver waits for the page to catch up after any action that
            # can move it, so no engine needs to remember to.
            d.navigate(substitute(step.url, params))
            return None
        target = step.target.model_copy(deep=True)
        # Drop rungs that do not carry the preferred rung's parameters, BEFORE
        # substitution makes them indistinguishable. Otherwise a lookup for a
        # nonexistent account falls through to "any account link" and reports
        # someone else's data as a success.
        dropped = len(target.candidates)
        target.candidates = target.constrained_candidates()
        dropped -= len(target.candidates)
        if dropped:
            self.ev.event("unconstrained_rungs_ignored", step=step.id, count=dropped)
        for c in target.candidates:
            c.value = substitute(c.value, params)
        if isinstance(step, ClickStep):
            strat = d.click(target)
            self._entered = {}          # this form has been committed
            return strat
        if isinstance(step, TypeStep):
            value = substitute(step.value, params)
            # Remember what actually went into this form, so the confirmation
            # prompt can show what is being committed rather than every
            # parameter the run happens to carry. Secrets are never recorded.
            if not step.sensitive:
                self._entered[step.description] = value
            return d.type_text(target, value)
        if isinstance(step, SelectStep):
            value = substitute(step.value, params)
            self._entered[step.description] = value
            strat = d.select(target, value)
            return strat
        if isinstance(step, ExtractStep):
            text, strat = d.read_text(target)
            if not text.strip():
                # Finding the element but reading nothing out of it is not a
                # result. Returning "" as a success hands the caller an empty
                # answer that looks authoritative; route it through the step's
                # declared error handling instead, where an empty read is
                # usually a business outcome ("no such row") rather than data.
                raise ValueError(
                    f"'{step.output}' resolved an element but it contained no text")
            try:
                result.outputs[step.output] = _parse(text, step.parse)
            except ValueError as exc:
                raise _Unreadable(
                    f"'{step.output}' read {text[:60]!r} but could not be "
                    f"parsed as {step.parse}: {exc}") from exc
            result.output_evidence[step.output] = OutputEvidence(
                source_text=text, locator_used=strat,
                captured_at=datetime.now(timezone.utc))
            self.ev.event("extracted", step=step.id, output=step.output,
                          source_text=text)
            return strat
        raise ValueError(f"unsupported step type {type(step)}")

    # -------------------------------------------------------------- detectors

    def _check_detectors(self, step: Step, params: dict, result: ReplayResult,
                         budget: dict, post_success: bool = False):
        for det in step.detectors:
            match = det.when.model_copy()
            match.value = substitute(match.value, params)
            if self.driver.matches(match):
                self.ev.event("detector_hit", step=step.id, detector=det.id)
                if isinstance(det.then, RecoverAction) and not post_success:
                    return det.then
                if not isinstance(det.then, RecoverAction):
                    self._terminal_action(det.then, step, result,
                                          observed=f"detector '{det.id}' matched",
                                          params=params)
        return None

    def _apply_remedy(self, action: RecoverAction) -> None:
        time.sleep(action.backoff_ms / 1000)
        if action.remedy == "reload_and_retry":
            self.driver.navigate(self.driver.current_url())
        elif action.remedy == "dismiss_and_retry" and action.dismiss_target:
            try:
                self.driver.click(action.dismiss_target)
            except Exception:
                pass  # interstitial already gone counts as dismissed

    # -------------------------------------------------------------- terminals

    def _terminal_action(self, action, step: Step, result: ReplayResult,
                         observed: str, params: dict | None = None):
        if isinstance(action, OutcomeAction):
            result.status = ReplayStatus.BUSINESS_OUTCOME
            # The message is handed back to the calling agent, so it has to
            # read as an answer about THIS invocation — "account 99999 was
            # not found", not a raw "{{account_id}}" template.
            message = action.message
            if params:
                try:
                    message = substitute(message, params)
                except KeyError:
                    pass  # a placeholder the caller never supplied: leave as authored
            result.outcome_code, result.outcome_message = action.code, message
            self.ev.event("business_outcome", step=step.id, code=action.code)
            raise _Terminal(result)
        self._fail(result, step.id,
                   expected=step.description, observed=observed)

    def _fail(self, result: ReplayResult, step_id: str,
              expected: str, observed: str):
        shot = self.ev.screenshot_path(f"failure_{step_id}")
        try:
            self.driver.observe(screenshot_to=shot)
        except Exception:
            shot = None
        result.status = ReplayStatus.HARD_FAILURE
        result.failed_step = step_id
        result.expected, result.observed = expected, observed
        self.ev.event("hard_failure", step=step_id, expected=expected,
                      observed=observed, screenshot=shot)
        raise _Terminal(result)
