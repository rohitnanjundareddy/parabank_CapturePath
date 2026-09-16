"""Recorder: transcript -> draft artifact.

Distillation, not transcription:
1. Failed attempts and dead ends are dropped; only actions that worked ship.
2. Caller supplied parameter values are replaced with {{param}} placeholders
   wherever they appear (typed text, URLs, relative locator anchors), which is
   what makes the artifact reusable with new inputs.
3. Secrets are marked sensitive and their literal values never enter the file.
4. The model's declared proof text becomes the success checkpoint.

The output is a DRAFT. A human reviews it (and typically adds detectors and
business outcomes for known error states) before approval. Encoding "what can
go wrong" is a review responsibility by design: the discovery run only saw
the happy path, so it cannot know the failure modes.
"""

from __future__ import annotations

import re
from urllib.parse import urlparse

from .discovery import DiscoveryOutcome
from .schemas import (
    ApprovalStatus,
    Artifact,
    ArtifactKind,
    BusinessOutcomeDecl,
    Checkpoint,
    ClickStep,
    DetectorMatch,
    ExtractStep,
    InputParam,
    NavigateStep,
    OutcomeAction,
    OutputField,
    ParamType,
    Provenance,
    RiskLevel,
    RunSubflowStep,
    SelectStep,
    Step,
    TypeStep,
    WaitCondition,
)


MIN_PARAM_LEN = 3   # shorter values match inside ordinary words


def _url_wait(entry, params: dict[str, str], secrets: dict[str, str]):
    """A step that moved the browser must declare where it has to land.

    Without this, only the last step carried a wait and replay raced the
    browser: it clicked Log In and read the account table in the same
    millisecond, found no row, and reported the account missing. Discovery
    never saw it because discovery settles after every action.

    Only the PATH is asserted, never the host: the same capability has to
    replay against another tenant's deployment of the same product, where
    the host differs and the route does not.
    """
    if getattr(entry, "url_changed", False) and entry.result_url:
        path = urlparse(entry.result_url).path
        if path and path != "/":
            return WaitCondition(kind="url_matches",
                                 value=_parameterize(path, params, secrets),
                                 timeout_ms=15000)
    # Same URL, different page: a legacy postback. Wait on the text the
    # action actually produced, or the next step reads the previous page.
    appeared = getattr(entry, "appeared_text", None)
    if appeared:
        value = _parameterize(appeared, params, secrets)
        # Never wait on a secret: that would make replay hunt for the
        # password as visible text on the page.
        if any("{{" + name + "}}" in value for name in secrets):
            return None
        return WaitCondition(kind="text_visible", value=value, timeout_ms=15000)
    return None


def _is_collection(sample: str) -> bool:
    """Does this reading look like a list of things rather than one value?

    Rows and cells are the tell: newlines or tabs, or more than one amount.
    Used twice, and the second use is why it is worth naming. Typing decides
    whether a reading is a number; `on_empty` decides what NOTHING means, and
    the answer differs for the same reason. An empty collection is a real
    answer -- the account has no transactions in that range. An empty scalar
    is a broken page: a balance cell that exists and is blank is not a bank
    telling you something, it is a reading that should not be trusted.
    """
    s = (sample or "").strip()
    return ("\n" in s or "\t" in s
            or len(re.findall(r"[$€£]\s?[\d,]", s)) > 1)


def _infer_output(sample: str) -> tuple[ParamType, str]:
    """Type an output from what was actually read off the page.

    Only currency is inferred, and only when a currency symbol is present.
    An account number reads as perfectly numeric but is an IDENTIFIER:
    typing it as a number would hand back 13344.0, drop any leading zero,
    and invite arithmetic on something that is not a quantity. Anything
    that is not unambiguously money stays a string.

    A COLLECTION is never a scalar, however much money it contains. Reading
    a whole transaction table yields text full of currency symbols, and
    typing that as one number makes replay try to parse the entire table as
    a single amount — which fails, and the failure then gets mapped to
    "this account has no transactions" while the page plainly shows twelve.
    Rows and cells are the tell: a value carrying newlines or tabs, or more
    than one amount, is a table and stays text.
    """
    s = (sample or "").strip()
    looks_like_a_collection = (_is_collection(s)
                               or len(re.findall(r"[$€£]\s?[\d,]", s)) > 1)
    if (not looks_like_a_collection
            and re.search(r"[$€£]", s) and re.search(r"\d", s)):
        return ParamType.NUMBER, "currency"
    return ParamType.STRING, "text"


def _parameterize(text: str, params: dict[str, str], secrets: dict[str, str]) -> str:
    """Turn supplied values back into placeholders.

    Matching is case- and whitespace-insensitive on purpose: the model
    routinely normalises what it types ("145 west polk" entered as "145 West
    Polk"), and an exact match would miss, silently freezing a caller input
    into the flow as a constant. Longest values first so 'account_12' is
    replaced before 'account_1'.
    """
    for name, value in sorted({**params, **secrets}.items(),
                              key=lambda kv: -len(kv[1])):
        if not value or len(value.strip()) < MIN_PARAM_LEN:
            # A one- or two-character value matches inside ordinary words
            # ("ss" inside "Password") and would corrupt descriptions and
            # locators. Such values stay literal; the CLI warns at collection
            # time so the operator can supply something distinctive instead.
            continue
        # \s+ between tokens tolerates re-spacing; IGNORECASE tolerates
        # re-capitalisation; \b anchors keep the match to whole words so a
        # short value cannot land in the middle of another one.
        core = r"\s+".join(re.escape(tok) for tok in value.split())
        lead = r"\b" if value.strip()[0].isalnum() else ""
        trail = r"\b" if value.strip()[-1].isalnum() else ""
        text = re.sub(lead + core + trail, "{{" + name + "}}", text,
                      flags=re.IGNORECASE)
    return text


def record(outcome: DiscoveryOutcome, *, artifact_id: str, name: str,
           description: str, app: str, goal: str, entry_url: str,
           params: dict[str, str], secrets: dict[str, str],
           run_id: str, model: str, version: str = "1.0.0",
           plan=None, kind=None, prefix_subflows=None,
           partial_reason: str = "") -> Artifact:
    """`partial_reason` records a run that did NOT reach its goal, keeping the
    path it did explore so a reviewer can replay it and decide whether to
    finish it. It is always a draft, and the caller must say why -- a partial
    recorded silently is indistinguishable from a working capability, which is
    the confusion this whole pipeline exists to prevent."""
    if not outcome.success and not partial_reason:
        raise ValueError("refusing to record a failed discovery run")

    # Chunks that were REPLAYED rather than discovered (see cli.py's reuse
    # flow) are recorded as pinned references, not as copies of their steps.
    # One fix to a fragment then reaches every capability that uses it.
    prefix_steps: list[Step] = []
    for n0, (ref, binding) in enumerate(prefix_subflows or [], start=1):
        prefix_steps.append(RunSubflowStep(
            id=f"s00_{n0}_subflow",
            description=f"replay proven fragment {ref}",
            ref=ref, bind_inputs=binding or {}))

    # Distillation, part one: when several extracts fill the same output, only
    # the last one's value survives a linear replay. The earlier ones are dead
    # writes — the same class of thing as a failed attempt — so they do not
    # ship. (They are usually the model retrying after reading the wrong
    # element, which is exactly what review needs to look at in the survivor.)
    last_writer: dict[str, int] = {}
    for i, e in enumerate(outcome.transcript):
        if e.ok and e.tool == "extract":
            last_writer[e.args["output"]] = i
    superseded = {i for i, e in enumerate(outcome.transcript)
                  if e.ok and e.tool == "extract"
                  and last_writer[e.args["output"]] != i}

    steps: list[Step] = list(prefix_steps)
    # Outcomes implied by the steps themselves. A capability that can come
    # back empty has to SAY so in its contract, or the caller has no reason
    # to handle it.
    collection_outcomes: list[BusinessOutcomeDecl] = []
    n = 0
    for idx, e in enumerate(outcome.transcript):
        if not e.ok or idx in superseded:
            continue  # distillation: dead ends and dead writes do not ship
        n += 1
        sid = f"s{n:02d}_{e.tool}"
        # The step description is parameterised for the same reason the
        # target description is: "Balance of account 13344" is a lie the
        # moment the capability is replayed for a different account, and it
        # is the text that shows up in results and policy decisions.
        common = dict(id=sid,
                      description=_parameterize(e.description or e.tool,
                                                params, secrets),
                      wait_after=_url_wait(e, params, secrets))

        if e.tool == "navigate":
            steps.append(NavigateStep(
                **common,
                url=_parameterize(e.args["url"], params, secrets)))
        elif e.tool == "click" and e.target:
            # Filling fields is reversible; committing the form is the moment
            # state changes. A submit is therefore declared RISKY in the
            # artifact itself, so the gate does not depend on a regex
            # recognising whatever this tenant named the button. A reviewer
            # can lower it deliberately; policy still takes the max of
            # declared and inferred, so it cannot be downgraded silently.
            steps.append(ClickStep(
                **common, target=_param_target(e.target, params, secrets),
                risk=RiskLevel.RISKY if getattr(e, "submits", False)
                else RiskLevel.SAFE))
        elif e.tool == "type" and e.target:
            sensitive = bool(e.args.get("sensitive"))
            raw = e.args["text"]
            value = _parameterize(raw, params, secrets)
            if sensitive and value == raw:
                # A secret must never be stored literally. If it was not one of
                # the supplied secret params, drop the value entirely.
                value = "{{" + "UNBOUND_SECRET" + "}}"
            steps.append(TypeStep(**common, target=_param_target(e.target, params, secrets),
                                  value=value, sensitive=sensitive))
        elif e.tool == "select" and e.target:
            picked = _parameterize(e.args["value"], params, secrets)
            # If the caller chooses this option, then an option that is not
            # there is an answer about their data, not a broken page. Say so
            # in the contract, or replay has nowhere to put "no such account"
            # and falls back to reporting a fault.
            absent = None
            m = re.fullmatch(r"\{\{(\w+)\}\}", picked.strip())
            if m:
                pname = m.group(1)
                code = "NO_SUCH_" + re.sub(r"[^A-Z0-9]+", "_",
                                           pname.upper()).strip("_")
                absent = OutcomeAction(
                    code=code,
                    message=(f"The {{{{{pname}}}}} supplied is not among the "
                             f"options offered here, so there is no such "
                             f"record to act on."))
                if code not in {o.code for o in collection_outcomes}:
                    collection_outcomes.append(BusinessOutcomeDecl(
                        code=code,
                        description=f"The supplied {pname} does not exist on "
                                    f"this surface."))
            steps.append(SelectStep(**common, target=_param_target(e.target, params, secrets),
                                    value=picked, on_value_absent=absent))
        elif e.tool == "extract" and e.target:
            out_name = e.args["output"]
            sample = outcome.outputs.get(out_name, "")
            _, parse_mode = _infer_output(sample)
            # What does reading NOTHING out of this element mean? For a
            # collection it means the collection is empty, which is an answer
            # the caller asked for: no transactions in that range. For a
            # single value it means a field that should hold something does
            # not, which is a reading not to be trusted -- so that case is
            # left to on_exhausted, and stays a failure.
            on_empty = None
            if _is_collection(sample):
                code = "NO_" + re.sub(r"[^A-Z0-9]+", "_", out_name.upper()).strip("_")
                on_empty = OutcomeAction(
                    code=code,
                    message=(f"The page showed the {out_name} area with nothing "
                             f"in it, so there are none to report for these "
                             f"inputs. This is an answer, not a failure."))
                if code not in {o.code for o in collection_outcomes}:
                    collection_outcomes.append(BusinessOutcomeDecl(
                        code=code,
                        description=f"No {out_name} were present for the "
                                    f"supplied inputs."))
            steps.append(ExtractStep(**common, target=_param_target(e.target, params, secrets),
                                     output=out_name, parse=parse_mode,
                                     on_empty=on_empty))

    # The final step waits for the proof text: replay then verifies the same
    # condition the model proved success with.
    if steps:
        steps[-1].wait_after = steps[-1].wait_after or WaitCondition(
            kind="text_visible", value=outcome.proof_text, timeout_ms=10000)

    # Input descriptions come from the best source available, in order:
    #   1. the agent's own question, for values it asked for mid-run
    #   2. the approved plan's parameter description
    #   3. a generic fallback
    # This matters more than it looks: these strings are what the next caller
    # is prompted with at replay time, and a vague prompt gets a wrong value.
    asked = {oi.name: oi for oi in outcome.operator_inputs}
    planned = {p.name: p for p in (plan.parameters if plan else [])}

    def _describe(k: str, fallback: str) -> str:
        if k in asked:
            return asked[k].question
        if k in planned and planned[k].description:
            return planned[k].description
        return fallback

    def _example(k: str, observed: str) -> str:
        # Prefer the plan's illustrative example over the value actually used:
        # echoing the recorded value invites the next caller to retype it, and
        # for personal data it should not be sitting in the artifact at all.
        if k in planned and planned[k].example:
            return planned[k].example
        return "" if k in asked else observed

    inputs = [InputParam(name=k, type=ParamType.STRING,
                         description=_describe(k, "Parameter supplied per invocation"),
                         example=_example(k, v))
              for k, v in params.items()]
    inputs += [InputParam(name=k, type=ParamType.STRING,
                          description=_describe(
                              k, "Secret. Injected at runtime, never stored."),
                          sensitive=True)
               for k in secrets]
    # The SHAPE of what was read, never the value itself: a real balance is
    # regulated customer data and this file gets committed and reviewed.
    outputs = []
    for k, v in outcome.outputs.items():
        otype, mode = _infer_output(v)
        shape = ("a currency amount, returned as a number" if mode == "currency"
                 else "returned as text")
        outputs.append(OutputField(
            name=k, type=otype,
            description=f"Read from the page during discovery; {shape}"))

    # A step with one rung cannot degrade gracefully — it can only fail. Worth
    # a reviewer's eye, especially on an extract, where on_exhausted may turn
    # a missed locator into a business outcome the caller will believe.
    notes = []
    for s in steps:
        tgt = getattr(s, "target", None)
        if tgt is not None and len(tgt.candidates) == 1:
            notes.append(
                f"{s.id}: only one locator candidate "
                f"({tgt.candidates[0].strategy.value}) — no fallback if it "
                f"stops matching")

    return Artifact(
        kind=kind or ArtifactKind.CAPABILITY,
        id=artifact_id, version=version, name=name,
        description=description or goal, app=app,
        status=ApprovalStatus.DRAFT,
        provenance=Provenance(discovery_run_id=run_id, model=model,
                              review_notes=notes,
                              partial_reason=partial_reason or None),
        inputs=inputs, outputs=outputs,
        business_outcomes=collection_outcomes,
        steps=steps,
        success=Checkpoint(
            id="goal_proof",
            description="Text the discovery model proved success with",
            match=DetectorMatch(kind="text_visible",
                                value=_parameterize(_goal_proof(outcome), params, secrets))),
    )


def _goal_proof(outcome: DiscoveryOutcome) -> str:
    """What the success checkpoint asserts.

    A successful run proved something and that text is the checkpoint. A
    PARTIAL never reached the goal, so there is no proof -- and an empty
    checkpoint is the worst possible answer, because "" is visible on every
    page and the artifact would report success from anywhere. Assert the last
    state it genuinely reached instead, so replaying a partial tells you how
    far the path actually gets.
    """
    if outcome.proof_text:
        return outcome.proof_text
    for text in outcome.final_texts:
        candidate = (text or "").strip()
        if len(candidate) >= 4 and not candidate.isdigit():
            return candidate
    raise ValueError(
        "cannot record: the run reached no state worth asserting, so the "
        "artifact would have no checkpoint and would 'succeed' anywhere")


def _param_target(target, params, secrets):
    t = target.model_copy(deep=True)
    t.description = _parameterize(t.description, params, secrets)
    for c in t.candidates:
        c.value = _parameterize(c.value, params, secrets)
    # Once values become placeholders it is visible which rungs identify the
    # element by the caller's parameter and which merely match its kind. Keep
    # only the ones that preserve the identity; a rung that finds "any account
    # link" is not a fallback for "account {{account_id}}'s link".
    t.candidates = t.constrained_candidates()
    return t
