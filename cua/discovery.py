"""Discovery: the LLM-driven observe -> decide -> act loop against a live surface.

The only module that talks to a model at runtime (replay never does). Each
turn: observe the page, hand the model a digest of what's on it, let it pick
exactly one tool call, gate that call through the same PolicyEngine replay
uses, execute it through the driver, and log everything to evidence. The
loop ends when the model declares success (verified against the live page,
never taken on faith), declares itself stuck (which hands the live session
to a human and resumes on the same session), or the step budget runs out.

The transcript this loop produces is NOT the artifact. recorder.py distills
it into one afterward, dropping dead ends and freezing caller-supplied
values into {{param}} placeholders. This module's only job is to reach the
goal once, safely, with enough detail recorded that distillation is
possible.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Callable, Optional

from .policy import PolicyEngine, ProposedAction, Verdict
from .schemas import DetectorMatch, RiskLevel, Target


# ---------------------------------------------------------------------------
# The transcript: a raw record of one discovery attempt, pre-distillation.
# ---------------------------------------------------------------------------

@dataclass
class TranscriptEntry:
    """One attempted action. Only entries with ok=True survive into the
    artifact; recorder.py is what does the dropping."""
    tool: str
    args: dict
    description: str = ""
    target: Optional[Target] = None
    extracted: Optional[str] = None
    ok: bool = True
    error: Optional[str] = None
    # Where the browser ended up once the action settled, and whether the
    # action moved it there. The recorder turns a move into a declared
    # wait_after, so replay proves it arrived instead of racing ahead and
    # reading a page the browser has not rendered yet.
    result_url: Optional[str] = None
    url_changed: bool = False
    # Text that was NOT on the page before this action and was after it. A
    # legacy postback often submits to the same URL, so a URL check alone
    # declares no wait at all and replay races the response — which is how a
    # loan result got read off the still-showing form.
    appeared_text: Optional[str] = None
    # True when this click COMMITS a form. Recorded so the artifact declares
    # the step risky, instead of leaving it to a regex over the button label.
    submits: bool = False


@dataclass
class OperatorInput:
    """A value the agent asked a human for mid-run, because neither the
    caller nor the plan supplied it. Becomes a first-class declared input of
    the recorded capability, described by the agent's own question."""
    name: str
    question: str
    sensitive: bool = False


@dataclass
class DiscoveryOutcome:
    success: bool
    proof_text: str = ""
    transcript: list[TranscriptEntry] = field(default_factory=list)
    outputs: dict[str, str] = field(default_factory=dict)
    operator_inputs: list[OperatorInput] = field(default_factory=list)
    steps_taken: int = 0
    escalations: int = 0
    failure_reason: Optional[str] = None
    # A value the caller supplied that the surface does not have. This is a
    # business outcome, not a failure to operate the page: no human can make
    # account 13455 exist. Kept separately so the run can report it as such
    # AND hardening can declare it on the artifact, so a later replay answers
    # "not found" instead of failing.
    not_found: Optional[dict] = None
    # Every page state this run passed through. The run SUCCEEDED, so text
    # that was on screen at any point during it cannot be evidence of
    # failure — which is what hardening needs in order to throw out a
    # detector like "'Customer Login' means the username field is missing".
    seen_texts: list[str] = field(default_factory=list)
    # Element texts on the page the run ended on.
    final_texts: list[str] = field(default_factory=list)


def _digest(observation, limit: int = 250) -> str:
    """Render an Observation into text compact enough for the model, keyed
    on the same 'e0', 'e1', ... refs the driver stamped onto the live DOM,
    so a tool call's `ref` always names something that still exists.

    The limit exists to bound prompt size, but it must never be the reason
    the agent cannot see its data: a legacy account table runs to hundreds
    of cells, and a positional cutoff silently hides whichever row the
    caller actually asked about. Anything past the cutoff stays reachable
    through find_elements, which searches the WHOLE observation.

    Set generously on purpose. It was once tuned to a page that turned out
    to be only partly rendered when it was measured — so the cutoff sat just
    under the real element count, and the rows nearest the bottom of a table
    vanished. A limit calibrated against an incomplete observation is worse
    than no limit at all, because it looks deliberate.
    """
    if observation is None:
        return "(no observation available)"
    lines = [f"URL: {observation.url}", f"TITLE: {observation.title}", "ELEMENTS:"]
    elements = observation.elements[:limit]
    for el in elements:
        bits = [f"[{el.ref}]", el.role]
        if el.name:
            bits.append(repr(el.name))
        if el.value:
            bits.append(f"value={el.value!r}")
        lines.append("  " + " ".join(bits))
    hidden = len(observation.elements) - len(elements)
    if hidden > 0:
        lines.append(f"  ... {hidden} more elements are on this page but not listed "
                     f"here. If what you need is not above, call find_elements to "
                     f"search all {len(observation.elements)} of them by text.")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Tool surface offered to the model. One call per turn (tool_choice="any"):
# the model always picks exactly one of these, never free-form action.
# ---------------------------------------------------------------------------

SYSTEM = """You are operating a legacy banking web application on behalf of a caller, \
to accomplish one stated goal. You act by calling exactly one tool per turn; \
you never act outside of a tool call.

Rules:
1. Only ever use an element ref ("e0", "e12", ...) that appears in the MOST \
RECENT observation, or that find_elements just returned to you. Refs are \
re-assigned on every observation; never reuse one from an earlier turn.
1b. A long page (an account table, a transaction list) is listed only in \
part. If the row, cell or control you need is not in the observation, call \
find_elements with the text you are looking for — for example the account \
number — instead of guessing a ref, working from a row that merely looks \
similar, or giving up.
2. Every value the caller supplied (listed below) must be typed or selected \
EXACTLY as given, verbatim. Never invent a value, and never substitute a \
different one for it.
2b. To capture a LIST — several transactions, every row of a table — extract \
the TABLE element itself, not one cell at a time. One extract reads the whole \
table's text into one output, which is what a goal like "read the \
transactions" is asking for. Reading cells individually cannot express a \
list and will not satisfy such a goal.
3. If you need a value nobody supplied, call ask_operator instead of \
guessing or leaving a field blank.
4. Set sensitive: true on a `type` call whenever the value being typed is a \
credential or secret.
5. Call declare_success only once the page genuinely shows the proof text \
you cite. It is checked against the live page, not taken on faith.
6. Call declare_stuck rather than repeating a blocked or failing action, \
guessing at a workaround, or acting outside the stated goal. Getting stuck \
and asking a human for help is always safer than improvising against a \
bank's back office.
7. Do only what the goal asks. Do not take extra steps "while you're here"."""

TOOLS = [
    {
        "name": "navigate",
        "description": "Go to a URL.",
        "input_schema": {
            "type": "object",
            "properties": {
                "url": {"type": "string"},
                "why": {"type": "string"},
            },
            "required": ["url", "why"],
        },
    },
    {
        "name": "click",
        "description": "Click an element from the latest observation.",
        "input_schema": {
            "type": "object",
            "properties": {
                "ref": {"type": "string"},
                "description": {"type": "string",
                                "description": "human meaning, e.g. 'Log In button'"},
                "why": {"type": "string"},
            },
            "required": ["ref", "description", "why"],
        },
    },
    {
        "name": "type",
        "description": "Type text into an input from the latest observation.",
        "input_schema": {
            "type": "object",
            "properties": {
                "ref": {"type": "string"},
                "text": {"type": "string"},
                "description": {"type": "string"},
                "why": {"type": "string"},
                "sensitive": {"type": "boolean",
                             "description": "true if this value is a credential or secret"},
            },
            "required": ["ref", "text", "description", "why"],
        },
    },
    {
        "name": "select",
        "description": "Choose an option in a dropdown from the latest observation.",
        "input_schema": {
            "type": "object",
            "properties": {
                "ref": {"type": "string"},
                "value": {"type": "string",
                         "description": "the visible option label to select"},
                "description": {"type": "string"},
                "why": {"type": "string"},
            },
            "required": ["ref", "value", "description", "why"],
        },
    },
    {
        "name": "extract",
        "description": "Read text off an element from the latest observation into a named output.",
        "input_schema": {
            "type": "object",
            "properties": {
                "ref": {"type": "string"},
                "output": {"type": "string",
                          "description": "name this reading fills, e.g. 'balance'"},
                "description": {"type": "string"},
                "why": {"type": "string"},
            },
            "required": ["ref", "output", "description", "why"],
        },
    },
    {
        "name": "find_elements",
        "description": "Search EVERY element on the current page by text, including "
                       "the ones the observation did not list. Use this to locate a "
                       "row, cell or control in a long table (e.g. by account number) "
                       "and get its ref. Reads only; changes nothing.",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string",
                         "description": "text to look for, e.g. an account number"},
            },
            "required": ["query"],
        },
    },
    {
        "name": "press",
        "description": "Press a single keyboard key, e.g. 'Enter'. Modifier "
                       "combinations may use 'Control+X'; it is normalized so "
                       "the recording replays on macOS too.",
        "input_schema": {
            "type": "object",
            "properties": {"key": {"type": "string"}, "why": {"type": "string"}},
            "required": ["key", "why"],
        },
    },
    {
        "name": "ask_operator",
        "description": "Ask a human for a value nobody supplied. Use instead of guessing.",
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "snake_case parameter name"},
                "question": {"type": "string"},
                "sensitive": {"type": "boolean"},
            },
            "required": ["name", "question"],
        },
    },
    {
        "name": "declare_success",
        "description": "Declare the goal reached. Checked against the live page before being accepted.",
        "input_schema": {
            "type": "object",
            "properties": {
                "proof_text": {"type": "string",
                              "description": "text currently visible on the page proving success"},
            },
            "required": ["proof_text"],
        },
    },
    {
        "name": "declare_not_found",
        "description":
            "A VALUE YOU WERE GIVEN is not present on this surface -- an "
            "account number missing from a dropdown, a record the search "
            "cannot find. Use this instead of declare_stuck whenever the "
            "obstacle is absent DATA rather than a page you cannot operate: "
            "'no such account' is a legitimate answer the caller needs, and "
            "no human can make the missing record exist. Say which parameter "
            "and what you could see instead.",
        "input_schema": {
            "type": "object",
            "properties": {
                "parameter": {"type": "string",
                              "description": "the input whose value is absent"},
                "observed": {"type": "string",
                             "description": "what the surface offered instead"},
            },
            "required": ["parameter", "observed"],
        },
    },
    {
        "name": "declare_stuck",
        "description":
            "You cannot OPERATE this surface -- the path is unclear, a "
            "control will not respond, something unexpected is in the way. "
            "Hands the live session to a human operator. If the obstacle is "
            "a value that simply is not there, use declare_not_found instead.",
        "input_schema": {
            "type": "object",
            "properties": {"reason": {"type": "string"}},
            "required": ["reason"],
        },
    },
]


class DiscoveryAgent:
    """Runs the observe -> decide -> act loop for one discovery attempt.

    The model never touches the driver directly: every proposed browser
    action is normalized into a ProposedAction and passed through the same
    PolicyEngine.check() replay uses, then, if allowed, executed by the
    driver. Nothing about that gate differs for the model versus a
    human-authored artifact at replay time.
    """

    def __init__(self, driver, policy: PolicyEngine, evidence, redactor,
                 *, model: str, max_steps: int = 25, escalation=None,
                 plan=None, max_escalations: int = 2,
                 escalate_policy_blocks: bool = True,
                 operator_prompt: Optional[Callable[[str, str, bool], Optional[str]]] = None):
        self.driver = driver
        self.policy = policy
        self.ev = evidence
        self.redactor = redactor
        self.model = model
        self.max_steps = max_steps
        self.escalation = escalation
        self.plan = plan
        self.max_escalations = max_escalations
        self.escalate_policy_blocks = escalate_policy_blocks
        self.operator_prompt = operator_prompt
        # Policy refusals since the agent last made progress. Non-empty at the
        # moment it declares itself stuck means the gate is what stopped it.
        self._blocked_by_policy: list[str] = []
        # Normalized reasons already handed to an operator, so the same
        # blocker cannot pull a human in twice.
        self._escalated_reasons: list[str] = []

    def run(self, goal: str, url: str, params: dict, secrets: dict) -> DiscoveryOutcome:
        import anthropic
        client = anthropic.Anthropic()

        for v in secrets.values():
            self.redactor.register(v)

        self.driver.navigate(url)
        self.ev.event("action", tool="navigate", url=url, description="Open entry page", ok=True)

        outcome = DiscoveryOutcome(success=False)
        # Recorded as a real step, not just an evidence line: without this,
        # the artifact has no navigate step at all and replay starts on a
        # blank page with nowhere to go.
        outcome.transcript.append(TranscriptEntry(
            "navigate", {"url": url, "why": "entry point", "description": "Open entry page"},
            description="Open entry page", ok=True))
        messages: list[dict] = [
            {"role": "user", "content": self._opening_prompt(goal, params, secrets)}
        ]

        for step in range(1, self.max_steps + 1):
            shot = self.ev.screenshot_path(f"discovery_step_{step:02d}")
            obs = self.driver.observe(screenshot_to=shot)
            self.ev.event("observation", step=step, url=obs.url, title=obs.title)

            outcome.seen_texts.append(_digest(obs))
            # Overwritten every turn, so it ends up holding the state of the
            # page the run finished on — the raw material for a checkpoint
            # when the model cannot name one that is actually on screen.
            outcome.final_texts = [el.name for el in obs.elements if el.name]
            obs_block = {"type": "text", "text": f"OBSERVATION (step {step}):\n{_digest(obs)}"}
            if isinstance(messages[-1]["content"], list):
                messages[-1]["content"].append(obs_block)
            else:
                messages.append({"role": "user", "content": [obs_block]})

            resp = client.messages.create(
                model=self.model, max_tokens=1500, system=SYSTEM, tools=TOOLS,
                tool_choice={"type": "any", "disable_parallel_tool_use": True},
                messages=messages)
            messages.append({"role": "assistant", "content": resp.content})
            tool_uses = [b for b in resp.content if b.type == "tool_use"]
            call = tool_uses[0]
            # Belt and suspenders: disable_parallel_tool_use should prevent
            # more than one, but every tool_use in an assistant turn MUST get
            # a tool_result back or the next request is rejected outright.
            # Only the first call is ever executed; act on one thing at a
            # time so each decision is grounded in a fresh observation.
            extra_results = [
                {"type": "tool_result", "tool_use_id": b.id,
                 "content": "Skipped: only one tool call is processed per turn.",
                 "is_error": True}
                for b in tool_uses[1:]
            ]
            tool, tool_args = call.name, dict(call.input)
            self.ev.event("decision", step=step, tool=tool, args=tool_args)

            if tool == "declare_success":
                proof = tool_args.get("proof_text", "")
                verified = self.driver.matches(DetectorMatch(kind="text_visible", value=proof))
                self.ev.event("declare_success", proof=proof, verified=verified)
                if verified:
                    outcome.success = True
                    outcome.proof_text = proof
                    outcome.steps_taken = step
                    return outcome
                self._reply(messages, call.id,
                           f"'{proof}' is not visible on the current page. "
                           "The goal is not met yet; keep going, or declare_stuck "
                           "if you cannot reach it.", is_error=True, extra=extra_results)
                continue

            if tool == "declare_not_found":
                parameter = tool_args.get("parameter", "")
                observed = tool_args.get("observed", "")
                outcome.not_found = {"parameter": parameter, "observed": observed}
                self.ev.event("not_found", parameter=parameter, observed=observed)
                # Report it as the outcome it is, THEN offer the session. The
                # answer is "not found" either way; the handoff is there
                # because discovery is supervised authoring and the operator
                # may want to carry on from a state only they can reach. What
                # it must not do is masquerade as the agent being stuck.
                refusal = self._why_not_escalate(f"not found: {parameter}")
                if refusal:
                    outcome.steps_taken = step
                    outcome.failure_reason = (
                        f"'{parameter}' was not found on this surface. "
                        f"Observed instead: {observed}\n\n{refusal}")
                    return outcome
                outcome.escalations += 1
                self._escalated_reasons.append(f"not found: {parameter}".lower())
                note = self.escalation.intervene(
                    context=(f"'{parameter}' is not present on this surface. "
                             f"Observed instead: {observed}. This is a business "
                             f"outcome, not a blocked page -- take the session "
                             f"if you want to carry on from a state only you "
                             f"can reach."),
                    goal=goal, step=f"step {step}",
                    driver=self.driver, evidence=self.ev)
                rescued = self._transcribe_human_actions(
                    getattr(self.escalation, "last_actions", None) or [])
                outcome.transcript.extend(rescued)
                self.ev.event("handoff_recorded", step=f"step {step}",
                              steps_recovered=len(rescued))
                self._reply(messages, call.id,
                            f"Recorded that '{parameter}' is not present. A "
                            f"human took control and reported: {note}. If the "
                            f"goal is now reachable, continue; otherwise "
                            f"declare_not_found again to end the run.",
                            extra=extra_results)
                continue

            if tool == "declare_stuck":
                reason = tool_args.get("reason", "")
                self.ev.event("stuck", reason=reason)
                refusal = self._why_not_escalate(reason)
                if refusal:
                    self.ev.event("escalation_refused", reason=reason,
                                  refusal=refusal)
                    outcome.steps_taken = step
                    outcome.failure_reason = f"{reason}\n\n{refusal}"
                    return outcome
                outcome.escalations += 1
                self._escalated_reasons.append(" ".join(reason.lower().split()))
                note = self.escalation.intervene(
                    context=reason, goal=goal, step=f"step {step}",
                    driver=self.driver, evidence=self.ev)
                # What the person did becomes part of the recording. Without
                # this a human-rescued run produces a capability that cannot
                # replay the rescued part, and verification correctly refuses
                # it -- the parameters they filled in by hand come back frozen.
                rescued = self._transcribe_human_actions(
                    getattr(self.escalation, "last_actions", None) or [])
                outcome.transcript.extend(rescued)
                self.ev.event("handoff_recorded", step=f"step {step}",
                              steps_recovered=len(rescued))
                self._reply(messages, call.id,
                           f"A human took control and reported: {note}. "
                           "Continue from the current page state.", extra=extra_results)
                continue

            if tool == "find_elements":
                # Searches the WHOLE observation, not the truncated digest.
                # Pure filtering over data already captured this turn: it
                # touches nothing, so it needs no policy gate, and the refs
                # stay valid because the page cannot have moved.
                query = (tool_args.get("query") or "").strip()
                matches = [e for e in obs.elements
                           if query.lower() in
                           f"{e.name} {e.value or ''} {e.role}".lower()] if query else []
                self.ev.event("action", tool="find_elements", description=query,
                              ok=bool(matches), matches=len(matches))
                if not matches:
                    self._reply(messages, call.id,
                               f"Nothing on this page matches {query!r}.",
                               is_error=True, extra=extra_results)
                    continue
                listing = "\n".join(
                    f"  [{e.ref}] {e.role} {e.name!r}"
                    + (f" value={e.value!r}" if e.value else "")
                    for e in matches[:40])
                self._reply(messages, call.id,
                           f"{len(matches)} element(s) match {query!r} on the current "
                           f"page; these refs are valid now:\n{listing}",
                           extra=extra_results)
                continue

            if tool == "ask_operator":
                name = tool_args.get("name", "")
                question = tool_args.get("question", "")
                sensitive = bool(tool_args.get("sensitive"))
                value = (self.operator_prompt(name, question, sensitive)
                         if self.operator_prompt else None)
                self.ev.event("action", tool="ask_operator", description=question,
                              ok=value is not None)
                if value is None:
                    self._reply(messages, call.id,
                               "No operator available to supply this value. "
                               "Find another way, or declare_stuck.", is_error=True,
                               extra=extra_results)
                    continue
                # Mutates the caller's dicts in place: cli.py passes the same
                # params/secrets objects on to record(), so a value acquired
                # here becomes a declared input of the recorded capability
                # without any extra plumbing.
                (secrets if sensitive else params)[name] = value
                if sensitive:
                    self.redactor.register(value)
                outcome.operator_inputs.append(OperatorInput(name, question, sensitive))
                self._reply(messages, call.id, f"Value supplied for '{name}'. Use it now.",
                           extra=extra_results)
                continue

            # -- browser actions: navigate / click / type / select / extract / press
            result_text, entry = self._act(tool, tool_args, obs)
            if entry is not None:
                outcome.transcript.append(entry)
                if entry.ok and tool == "extract" and entry.extracted is not None:
                    outcome.outputs[tool_args["output"]] = entry.extracted
            self._reply(messages, call.id, result_text, is_error=entry is None or not entry.ok,
                       extra=extra_results)

        outcome.steps_taken = self.max_steps
        outcome.failure_reason = f"max_steps ({self.max_steps}) reached without declare_success"
        self.ev.event("max_steps_reached", max_steps=self.max_steps)
        return outcome

    # ------------------------------------------------------------------ util

    def _opening_prompt(self, goal: str, params: dict, secrets: dict) -> list[dict]:
        lines = [f"GOAL: {goal}", "", "CALLER-SUPPLIED VALUES (use verbatim; never substitute):"]
        for k, v in params.items():
            lines.append(f"  {k} = {v!r}")
        for k, v in secrets.items():
            lines.append(f"  {k} = {v!r}  [sensitive]")
        if not params and not secrets:
            lines.append("  (none)")
        text = "\n".join(lines)
        if self.plan is not None:
            from .planner import plan_prompt_block
            text += "\n\n" + plan_prompt_block(self.plan)
        return [{"type": "text", "text": text}]

    @staticmethod
    def _reply(messages: list[dict], tool_use_id: str, content: str, is_error: bool = False,
              extra: Optional[list[dict]] = None) -> None:
        # `extra` carries tool_result stubs for any additional tool_use blocks
        # the model issued in the same turn (see disable_parallel_tool_use).
        # They must land in this same user message: every tool_use in an
        # assistant turn needs a matching tool_result before the next call.
        messages.append({"role": "user", "content": [
            *(extra or []),
            {"type": "tool_result", "tool_use_id": tool_use_id, "content": content,
             "is_error": is_error},
        ]})

    def _transcribe_human_actions(self, actions: list[dict]) -> list:
        """Turn what a person did during a handoff into recordable steps.

        The handoff listeners capture the same element properties the agent's
        own actions capture, through the one shared description in the driver,
        so these become real locator ladders rather than notes. That is what
        lets a capability a human had to rescue replay WITHOUT them next time.

        Anything the page could not describe is dropped: a step with no way to
        find its element again is not a step, and recording it would trade an
        honest refusal for a replay that fails somewhere less obvious.
        """
        from .driver import _target_from_info

        entries = []
        for action in actions:
            info = action.get("info")
            tool = {"click": "click", "type": "type",
                    "select": "select"}.get(action.get("type"))
            if tool is None or not info:
                continue
            if action.get("sensitive"):
                # A password typed during a handoff is not recorded at all.
                # Authentication belongs to the login fragment, and a secret
                # has no business in a step even as a placeholder.
                continue
            description = (action.get("desc") or "").strip() or                 f"{tool} performed by the operator"
            try:
                target = _target_from_info(info, description)
            except Exception:
                # The schema requires at least one locator candidate, so an
                # element the page could describe no other way raises here.
                # Dropping it is right, but it must not take the run down
                # with it: the rest of the handoff is still worth recording.
                self.ev.event("handoff_action_dropped", description=description)
                continue
            args = {}
            if tool == "type":
                args["text"] = action.get("value") or ""
            elif tool == "select":
                args["value"] = action.get("value") or ""
            entries.append(TranscriptEntry(
                tool, args, description=description, target=target, ok=True))
        return entries

    def _why_not_escalate(self, reason: str) -> Optional[str]:
        """Should a human be pulled into this? Returns why not, or None.

        Handing over the live browser is only worth an operator's time when a
        human can actually clear the blocker. Two cases cannot be cleared that
        way, and before this existed both produced the same symptom: the
        operator asked to describe what they did, again and again, while
        nothing about the agent's situation changed.

        The first is not merely futile, it is a hole. When the gate refuses an
        action and the agent then escalates, the operator is being asked to
        perform by hand the exact action policy just refused -- and whatever
        they do gets recorded as progress. A blocked transfer became a
        human-performed transfer, of the wrong amount between the wrong
        accounts, and the run carried on as though the flow had worked. A
        BLOCK verdict is a deliberate, permanent refusal; routing around it
        through a person is the one thing escalation must never do.
        """
        if self.escalation is None:
            return "No operator is attached to this run."

        if self._blocked_by_policy and not self.escalate_policy_blocks:
            blocked = "; ".join(dict.fromkeys(self._blocked_by_policy))
            return (
                "NOT ESCALATED: the agent is stuck because policy refused its "
                f"actions ({blocked}) -- not because it met something a human "
                "could clear. Handing over the browser here would ask an "
                "operator to perform by hand the action the gate just refused, "
                "and the run would record a flow the policy forbids. If this "
                "goal belongs in scope, widen config/policy.yaml deliberately "
                "and re-run. If it does not, this refusal is the system "
                "working as designed. Pass --escalate-policy-blocks to hand "
                "over anyway: the operator then acts on their own authority, "
                "and the recording still cannot be approved.")

        normalized = " ".join(reason.lower().split())
        if normalized in self._escalated_reasons:
            return (
                "NOT ESCALATED: the agent reported the same blocker it "
                "reported before the last handover, so the intervention did "
                "not change what it is able to do. Asking again would only "
                "repeat the question.")

        if len(self._escalated_reasons) >= self.max_escalations:
            return (
                f"NOT ESCALATED: the escalation budget of "
                f"{self.max_escalations} is spent and the agent is still "
                f"stuck. Raise it with --max-escalations if more handovers "
                f"are genuinely useful here.")

        return None

    #: Actions that cannot commit state whatever their label says: loading a
    #: page and reading text change nothing. A forbidden destination is the
    #: job of `blocked_url_patterns`, which still applies to navigate.
    _CANNOT_COMMIT = ("navigate", "extract")

    def _policy_gate(self, tool: str, description: str,
                     risk: RiskLevel = RiskLevel.SAFE,
                     commits: Optional[bool] = None,
                     url: Optional[str] = None):
        """Returns (allowed, message_if_not). Identical enforcement to replay:
        every proposed action goes through PolicyEngine.check() before the
        driver ever sees it.

        `url` is what policy judges. For a navigation that is the DESTINATION,
        which is the rule replay already applies -- judging the page we happen
        to be standing on instead would let the agent walk onto any blocked
        route it likes and only discover the route is forbidden once it is
        already there, with every subsequent action refused for a reason that
        looks like it is about the action rather than the address. For every
        other action the current page IS the address being acted upon.
        """
        decision = self.policy.check(ProposedAction(
            tool, url=url or self.driver.current_url(),
            target_description=description, risk=risk,
            commits=False if tool in self._CANNOT_COMMIT else commits))
        if decision.verdict == Verdict.BLOCK:
            self.ev.event("policy_block", tool=tool, reason=decision.reason)
            self._blocked_by_policy.append(decision.reason)
            return False, f"blocked by policy: {decision.reason}"
        if decision.verdict == Verdict.CONFIRM:
            approved = (self.escalation.confirm(f"{tool} '{description}': {decision.reason}")
                       if self.escalation else False)
            self.ev.event("policy_confirm", tool=tool, reason=decision.reason, approved=approved)
            if not approved:
                return False, f"declined: {decision.reason}"
        return True, None

    @staticmethod
    def _state_like(name: str) -> bool:
        """Is this text usable as proof a page arrived? Data values change
        between runs, so a balance or an account number is useless as a wait
        condition even though it did just appear."""
        n = (name or "").strip()
        return 3 <= len(n) <= 60 and not any(ch.isdigit() for ch in n)

    def _act(self, tool: str, args: dict,
             before=None) -> tuple[str, Optional[TranscriptEntry]]:
        """Execute one browser action. Returns (message for the model, transcript
        entry). A policy block or confirmation decline never touches the
        driver and produces no transcript entry: it was never attempted."""
        why = args.get("why", "")
        description = args.get("description", why or tool)

        # Whether this click COMMITS a form has to be known BEFORE the gate,
        # not after: the whole point is that the agent stops and asks before
        # submitting a loan application, not that we notice afterwards. The
        # same structural test the recorder uses, applied live.
        ref = args.get("ref")
        # Refs are re-stamped onto the DOM on EVERY observation, so a ref from
        # an earlier turn is not stale-but-harmless: it silently addresses
        # whatever element now happens to carry that number. That is how an
        # extract the model described as "Transaction results table" ended up
        # recorded against the account dropdown, and nothing downstream could
        # tell, because the description came from the model and the locator
        # came from the element.
        if ref and before is not None:
            live = {el.ref for el in before.elements}
            if ref not in live:
                self.ev.event("stale_ref", tool=tool, ref=ref,
                              description=description)
                return (f"'{ref}' is not in the latest observation. Refs are "
                        f"reassigned every time the page is observed, so a ref "
                        f"from an earlier turn now points somewhere else. Look "
                        f"at the current observation and use a ref from it."), None

        submits = bool(
            tool == "click" and ref
            and getattr(self.driver, "submits_ref", lambda r: False)(ref))

        allowed, message = self._policy_gate(
            tool, description,
            risk=RiskLevel.RISKY if submits else RiskLevel.SAFE,
            # For a click the driver gives a definitive structural answer, so
            # pass it. For anything else it has not been asked, and `None`
            # keeps the label backstop switched on.
            commits=submits if (tool == "click" and ref) else None,
            url=args.get("url") if tool == "navigate" else None)
        if not allowed:
            return message, None

        before_url = self.driver.current_url()
        try:
            target = None
            extracted = None
            if tool == "navigate":
                self.driver.navigate(args["url"])
            elif tool == "press":
                self.driver.press(args["key"])
            elif tool == "click":
                target = self.driver.candidates_for_ref(ref, description)
                self.driver.click_ref(ref)
            elif tool == "type":
                target = self.driver.candidates_for_ref(ref, description)
                self.driver.type_ref(ref, args["text"])
            elif tool == "select":
                target = self.driver.candidates_for_ref(ref, description)
                self.driver.select_ref(ref, args["value"])
            elif tool == "extract":
                target = self.driver.candidates_for_ref(ref, description)
                extracted = self.driver.read_ref(ref)
            else:
                raise ValueError(f"unknown tool '{tool}'")
            # No settle here: the driver waits after any action that can
            # move the page, so every surface gets it identically.
        except Exception as exc:
            self.ev.event("action", tool=tool, description=description, ok=False)
            return f"{tool} failed: {exc}", TranscriptEntry(
                tool, args, description=description, ok=False, error=str(exc))

        after_url = self.driver.current_url()
        # The agent got somewhere, so whatever the gate refused earlier is no
        # longer what is standing in its way.
        self._blocked_by_policy.clear()

        # What did this action put on screen that was not there before? For a
        # same-URL postback that is the only signal replay can wait on.
        # Only actions that can move the page get a transition wait. Typing
        # does not: the "new text" it produces is the value just typed, and
        # recording that as a wait means replay waits for the username — or
        # worse, the PASSWORD — to appear as visible text on the page.
        appeared_text = None
        if before is not None and tool in ("click", "press", "navigate"):
            # Poll rather than sample once. settle() can return before a
            # postback has even started, so a single look sees the OLD page,
            # finds nothing new, and records no wait — leaving replay to race
            # the very transition this is supposed to pin down.
            had = {e.name.strip() for e in before.elements if e.name}
            deadline = time.time() + 3.0
            while time.time() < deadline and appeared_text is None:
                try:
                    for el in self.driver.observe().elements:
                        name = (el.name or "").strip()
                        if name and name not in had and self._state_like(name):
                            appeared_text = name
                            break
                except Exception:
                    break
                if appeared_text is None:
                    time.sleep(0.2)

        self.ev.event("action", tool=tool, description=description, ok=True,
                      url=after_url)
        entry = TranscriptEntry(tool, args, description=description,
                                target=target, extracted=extracted, ok=True,
                                result_url=after_url,
                                url_changed=after_url != before_url,
                                appeared_text=appeared_text,
                                submits=submits)
        if tool == "extract":
            # The VALUE, not just the output name. Replay logs what it read and
            # discovery did not, which is why an extract pointed at a page
            # heading was invisible until someone replayed the artifact. The
            # redactor runs first: this is a reading off a bank page.
            self.ev.event("extracted", output=args.get("output"),
                          source_text=self.redactor.scrub(str(extracted))[:500])
            return f"read: {extracted!r}", entry
        return "ok", entry
