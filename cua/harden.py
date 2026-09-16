"""Harden a freshly recorded draft so it replays without hand editing.

A discovery run only ever sees the happy path, so the raw recording is
predictably deficient in three ways, every time:

  1. The success checkpoint often asserts a DATA value the model read off the
     page ("$529.10") instead of page STATE ("Accounts Overview"). The balance
     changes; the artifact breaks with no UI drift and no code change.
  2. No detectors: the run never met an error page, so it recorded none.
  3. No business outcomes: the caller has no declared result for "no such
     account" and gets a hard failure instead of an answer.

Those are review tasks, but nothing says a human must perform them. This
module asks the model to supply them ONCE, at record time, and writes the
result into the draft. Runtime is unaffected: replay still executes frozen
data with no model in the loop. The intelligence moves earlier, not into the
production path.

What is deliberately NOT delegated: the model may only propose detectors and a
checkpoint. It cannot change steps, locators, parameters or risk levels, and
every proposal is validated against the artifact before it is applied.
"""

from __future__ import annotations

from typing import Optional

from .schemas import (
    Artifact,
    BusinessOutcomeDecl,
    ClickStep,
    Detector,
    DetectorMatch,
    ExtractStep,
    NavigateStep,
    OutcomeAction,
    SelectStep,
    TypeStep,
)

HARDEN_SYSTEM = """You are hardening a recorded UI automation so it can run unattended
against a bank's back-office application. The recording succeeded, so it only
contains the happy path. Supply what the happy path could not observe.

1. success_signal — text that proves the flow reached its goal, and that is
   true on EVERY successful run. It must be page state: a heading, a section
   title, a confirmation phrase. It must NOT be a data value read off the page
   (a balance, an account number, a name); those change between runs and would
   break the capability for no reason.

2. detectors — runtime conditions this specific flow will meet in production,
   attached to the step that can actually encounter them. For each, classify:
     business_outcome — a legitimate answer the caller must handle
                        ("no such account", "credentials rejected")
     recover          — transient, retry or dismiss and continue
     hard_failure     — stop and surface a debuggable error
   Only use match text you can justify from the application described. If you
   are not confident of the exact wording an error page shows, omit that
   detector rather than guessing: a detector that never matches is harmless,
   but one that matches the wrong thing turns a real failure into a wrong
   answer.

3. business_outcomes — declare every code your detectors reference."""

HARDEN_TOOL = {
    "name": "submit_hardening",
    "description": "Supply the checkpoint and error handling for this capability",
    "input_schema": {
        "type": "object",
        "properties": {
            "success_signal": {
                "type": "string",
                "description": "page state proving success; never a data value"},
            "success_reason": {
                "type": "string",
                "description": "why this text is state rather than data"},
            "business_outcomes": {"type": "array", "items": {
                "type": "object",
                "properties": {"code": {"type": "string"},
                               "description": {"type": "string"}},
                "required": ["code", "description"]}},
            "detectors": {"type": "array", "items": {
                "type": "object",
                "properties": {
                    "step_id": {"type": "string"},
                    "id": {"type": "string"},
                    "match_text": {"type": "string",
                                   "description": "visible text identifying this state"},
                    "kind": {"type": "string",
                             "enum": ["business_outcome", "recover", "hard_failure"]},
                    "code": {"type": "string",
                             "description": "required for business_outcome"},
                    "message": {"type": "string"},
                },
                "required": ["step_id", "id", "match_text", "kind", "message"]}},
            "extract_not_found": {"type": "array", "items": {
                "type": "object",
                "properties": {
                    "step_id": {"type": "string"},
                    "code": {"type": "string"},
                    "message": {"type": "string"}},
                "required": ["step_id", "code", "message"]},
                "description": "steps whose target is identified by a caller "
                               "parameter, and where NOT FINDING it is a "
                               "business outcome rather than a failure — the "
                               "row or link for the requested id simply is not "
                               "there. Applies to extract steps and to clicks "
                               "on a parameterised element alike."},
        },
        "required": ["success_signal", "success_reason", "business_outcomes",
                     "detectors"],
    },
}


def _flow_summary(art: Artifact, goal: str) -> str:
    lines = [f"GOAL: {goal}", f"APPLICATION: {art.app}", "", "RECORDED STEPS:"]
    for s in art.steps:
        bits = [f"  {s.id} [{getattr(s, 'action', '?')}] {s.description}"]
        if isinstance(s, NavigateStep):
            bits.append(f"url={s.url}")
        if isinstance(s, (TypeStep, SelectStep)):
            bits.append(f"value={s.value}")
        if isinstance(s, ExtractStep):
            bits.append(f"reads into '{s.output}'")
        lines.append(" | ".join(bits))
    lines += ["", "INPUTS: " + ", ".join(i.name for i in art.inputs),
              "OUTPUTS: " + (", ".join(o.name for o in art.outputs) or "(none)"),
              "",
              f"CURRENT CHECKPOINT (may be wrong): "
              f"'{art.success.match.value}'"]
    return "\n".join(lines)


def _live_matches(driver, text: str) -> bool:
    """Evaluate a proposed text_visible condition against the ACTUAL page
    from the run that just succeeded, rather than reasoning about the text
    itself. `driver` is None when no live session is available (e.g.
    re-hardening a stored artifact offline); the check is then skipped."""
    if driver is None or not text:
        return False
    try:
        return driver.matches(DetectorMatch(kind="text_visible", value=text))
    except Exception:
        return False


def _seen_during_success(seen_texts, text: str) -> bool:
    """Was this text on screen at any point during the run that SUCCEEDED?

    Checking only the final page is not enough: a detector attached to an
    early step is evaluated while that step's page is showing, so
    "'Customer Login' means the username field is missing" passes a
    final-page check and then fires on every replay of a healthy login.
    The run succeeded, so nothing it displayed can be proof of failure.
    """
    needle = (text or "").strip().lower()
    if not needle:
        return False
    return any(needle in (page or "").lower() for page in (seen_texts or []))


def harden(client, model: str, art: Artifact, goal: str, driver=None,
           seen_texts: Optional[list[str]] = None,
           final_texts: Optional[list[str]] = None,
           log=lambda *a, **k: None) -> tuple[Artifact, list[str]]:
    """Return the hardened artifact and a list of what was applied.

    `driver`, when given, is the still-open session discovery just used to
    reach the goal — a page we KNOW represents success. Every proposal that
    claims to identify a state (the checkpoint, or a detector's match text)
    is checked against that real page instead of trusted on the strength of
    its wording. This generalizes past any one bad phrasing: a detector is
    rejected because it is empirically indistinguishable from success on
    real data, not because its text happens to match a known-bad string.
    """
    resp = client.messages.create(
        model=model, max_tokens=2000, system=HARDEN_SYSTEM,
        tools=[HARDEN_TOOL],
        tool_choice={"type": "tool", "name": "submit_hardening"},
        messages=[{"role": "user", "content": _flow_summary(art, goal)}])
    block = next(b for b in resp.content if b.type == "tool_use")
    proposal = dict(block.input)
    log("hardening_proposed", proposal=proposal)

    applied: list[str] = []
    rejected: list[str] = []
    by_id = {s.id: s for s in art.steps}

    # 1. Checkpoint. Only replace it when the model's signal is genuinely
    #    state-like (a proposal containing digits is refused, since a
    #    literal balance or account number is the exact defect being
    #    corrected) AND it is actually true on the page discovery just
    #    reached — a signal that isn't even visible right now would fail
    #    every future replay immediately.
    signal = (proposal.get("success_signal") or "").strip()
    if signal and any(ch.isdigit() for ch in signal):
        rejected.append(f"checkpoint '{signal}' rejected: looks like a data "
                        f"value, not page state")
        signal = ""
    elif signal and driver is not None and not _live_matches(driver, signal):
        rejected.append(f"checkpoint '{signal}' rejected: not actually "
                        f"visible on the page the run just succeeded on")
        signal = ""

    # Rejecting the proposal must not leave the recorded checkpoint asserting
    # a balance. The model often describes the state ("the Accounts Overview
    # heading is visible") instead of quoting it, so fall back to text the
    # final page actually shows: state-like, and confirmed on the page.
    if not signal and any(ch.isdigit() for ch in art.success.match.value):
        for cand in (final_texts or []):
            c = (cand or "").strip()
            if (3 <= len(c) <= 60 and not any(ch.isdigit() for ch in c)
                    and _live_matches(driver, c)):
                signal = c
                applied.append(f"checkpoint derived from the finished page: '{c}'")
                break

    if signal:
        if signal != art.success.match.value:
            art.success.match.value = signal
            art.success.description = (
                f"Page state proving success: {proposal.get('success_reason', '')}")
            applied.append(f"checkpoint set to page state '{signal}'")
        # The recorder copies the proof text into the last step's wait; keep
        # them consistent or the step waits for something that never appears.
        for s in art.steps:
            if s.wait_after and s.wait_after.kind == "text_visible" \
                    and any(ch.isdigit() for ch in (s.wait_after.value or "")):
                s.wait_after.value = signal
                applied.append(f"{s.id}: wait condition set to '{signal}'")

    # 2. Declared outcomes must exist before any detector may reference them.
    declared = {b.code for b in art.business_outcomes}
    for b in proposal.get("business_outcomes", []):
        if b["code"] not in declared:
            art.business_outcomes.append(BusinessOutcomeDecl(**b))
            declared.add(b["code"])
            applied.append(f"declared business outcome {b['code']}")

    # 3. Detectors, attached to real steps only, referencing declared codes.
    #    Checked against the live "known success" page before being trusted:
    #    generic to any wording, because it tests what the condition actually
    #    evaluates to on real data rather than comparing text.
    for d in proposal.get("detectors", []):
        step = by_id.get(d["step_id"])
        if step is None:
            continue
        match_text = (d.get("match_text") or "").strip()
        if not match_text:
            rejected.append(f"{step.id}: detector '{d.get('id', '?')}' rejected: "
                            f"empty match text")
            continue
        if _live_matches(driver, match_text):
            rejected.append(
                f"{step.id}: detector '{d.get('id', '?')}' rejected: "
                f"'{match_text}' is visible on the page the run just "
                f"succeeded on, so it can never distinguish that outcome "
                f"from a genuine one")
            continue
        if _seen_during_success(seen_texts, match_text):
            rejected.append(
                f"{step.id}: detector '{d.get('id', '?')}' rejected: "
                f"'{match_text}' was on screen during the run that SUCCEEDED, "
                f"so it cannot be evidence of failure")
            continue
        kind = d.get("kind")
        if kind == "business_outcome":
            code = d.get("code")
            if not code or code not in declared:
                continue
            then = OutcomeAction(code=code, message=d["message"])
        elif kind == "recover":
            from .schemas import RecoverAction
            then = RecoverAction(remedy="retry", max_attempts=2, backoff_ms=1000)
        else:
            from .schemas import FailAction
            then = FailAction(message=d["message"])
        if any(x.id == d["id"] for x in step.detectors):
            continue
        step.detectors.append(Detector(
            id=d["id"],
            when=DetectorMatch(kind="text_visible", value=match_text),
            then=then))
        applied.append(f"{step.id}: detector '{d['id']}' -> {kind}")

    # 4. Not finding the thing the CALLER named is usually an answer, not a
    #    crash — whether the step reads it or clicks it. The guard is that the
    #    target must be identified by a parameter: "the link for account
    #    {{account_id}} is absent" is a fact about the caller's request, while
    #    "some button is missing" is a broken recording, and only the first
    #    may be reported as a business outcome.
    for e in proposal.get("extract_not_found", []):
        step = by_id.get(e["step_id"])
        if step is None:
            continue
        target = getattr(step, "target", None)
        caller_identified = target is not None and any(
            "{{" in c.value for c in target.candidates)
        if not isinstance(step, ExtractStep) and not caller_identified:
            continue
        if e["code"] not in declared:
            art.business_outcomes.append(BusinessOutcomeDecl(
                code=e["code"], description=e["message"]))
            declared.add(e["code"])
        # The proposal is about an EMPTY result, so it belongs on `on_empty`.
        # Writing it to `on_exhausted` conflated two different claims: "the
        # results area is empty" (an answer) and "I could not find the results
        # area at all" (a fault). An extract aimed at the wrong element then
        # failed to resolve and was reported as "no transactions found" while
        # the page showed sixteen.
        if isinstance(step, ExtractStep):
            step.on_empty = OutcomeAction(code=e["code"], message=e["message"])
        else:
            step.on_exhausted = OutcomeAction(code=e["code"], message=e["message"])
        applied.append(f"{step.id}: empty result now reports {e['code']}")

    # Rejections are stored ON the artifact, not just logged, so a reviewer
    # opening the JSON later can see what was proposed and discarded and why
    # — not only what survived.
    if rejected:
        art.provenance.hardening_rejected.extend(rejected)
        for r in rejected:
            log("hardening_rejected", reason=r)
        applied.extend(f"REJECTED: {r}" for r in rejected)

    # Revalidate: hardening must never produce an artifact the schema rejects.
    art = Artifact.model_validate(art.model_dump())
    return art, applied
