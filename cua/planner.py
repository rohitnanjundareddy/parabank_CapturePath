"""Plan first, then act.

Before the agent touches the browser it proposes a PLAN: the ordered steps it
intends to take, and — the load-bearing part — which values must be typed
parameters supplied per invocation rather than constants baked into the flow.
A human approves or rejects the plan; only an approved plan gets executed.

Why this exists (and why it is not ceremony):
1. Parameter declaration moves up front. Without it, whether an account number
   becomes a reusable input depends on the recorder happening to string-match
   the value the caller passed. Declared parameters make it a contract, and
   `verify_against_plan` fails the recording when the contract is not met.
2. Review gets cheaper. Correcting an intended flow costs one conversation;
   correcting a recorded artifact costs a re-run. The human still reviews the
   recorded artifact afterwards — this is an additional gate, not a substitute.
3. The approved plan is stored in the artifact's provenance, so a reviewer can
   compare what was intended against what was recorded.

The plan constrains discovery but never replays: at replay time the artifact is
the only thing executed, and nothing here is consulted.
"""

from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, Field

from . import prompt

PLANNER_SYSTEM = """You plan automation of a legacy banking web application before executing it.

Given a goal, the entry page, and the values the caller supplied, produce a plan:
the ordered steps you intend to take, the typed parameters the capability needs,
the outputs it returns, and the page state that will prove success.

Rules:
1. EVERY value the caller supplied is a parameter, never a constant. Account
   numbers, usernames, amounts, identifiers: all parameters. Only values
   intrinsic to the capability itself (for example the account TYPE in an
   "open a savings account" capability) may be constants, and only when the
   capability name makes the constant explicit.
2. Mark credentials and secrets sensitive.
3. success_signal must be page STATE that is true on every successful run
   (a heading, a confirmation phrase), never a data value read from the page
   (a balance, an account number). Data changes between runs; state does not.
4. Keep steps at the level of intent ("log in with the supplied credentials"),
   not pixel detail. You will adapt to the real page when you execute.
5. Plan only what the goal asks for. Do not add steps that change state
   beyond the goal.
6. You may be shown a catalog of REUSABLE FRAGMENTS: proven chunks that
   already exist. If one accomplishes part of this goal, list its exact
   pinned ref in `reuse` and do NOT restate its steps in your plan. Reuse is
   how a long flow stays reliable: replayed chunks cannot go wrong the way a
   rediscovered one can. Only reuse a fragment whose stated end state is
   actually the state your next step needs."""


class PlannedParam(BaseModel):
    name: str = Field(description="snake_case parameter name")
    description: str
    example: str = ""
    sensitive: bool = False


class PlannedStep(BaseModel):
    intent: str = Field(description="what this step accomplishes, in plain words")
    action: str = Field(description="navigate | click | type | select | extract")
    uses: list[str] = Field(default_factory=list,
                            description="parameter names this step consumes")


class Plan(BaseModel):
    goal: str
    steps: list[PlannedStep]
    parameters: list[PlannedParam] = Field(default_factory=list)
    outputs: list[str] = Field(default_factory=list)
    success_signal: str
    reuse: list[str] = Field(
        default_factory=list,
        description="pinned fragment refs this plan will replay instead of "
                    "rediscovering, e.g. ['parabank_login@1.0.0']")

    def render(self) -> str:
        lines = [f"GOAL: {self.goal}", "", "PARAMETERS (supplied per invocation):"]
        for p in self.parameters:
            tag = " [sensitive]" if p.sensitive else ""
            eg = f" e.g. {p.example}" if p.example else ""
            lines.append(f"  - {p.name}: {p.description}{eg}{tag}")
        if self.reuse:
            lines += ["", "REUSES (replayed deterministically, not rediscovered):"]
            lines += [f"  - {r}" for r in self.reuse]
        lines += ["", "STEPS:"]
        for i, s in enumerate(self.steps, 1):
            uses = f"  <- {', '.join(s.uses)}" if s.uses else ""
            lines.append(f"  {i}. [{s.action}] {s.intent}{uses}")
        lines += ["", f"OUTPUTS: {', '.join(self.outputs) or '(none)'}",
                  f"SUCCESS SIGNAL: {self.success_signal}"]
        return "\n".join(lines)


PLAN_TOOL = {
    "name": "submit_plan",
    "description": "Submit the plan for human review",
    "input_schema": {
        "type": "object",
        "properties": {
            "steps": {"type": "array", "items": {
                "type": "object",
                "properties": {
                    "intent": {"type": "string"},
                    "action": {"type": "string"},
                    "uses": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["intent", "action"]}},
            "parameters": {"type": "array", "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "description": {"type": "string"},
                    "example": {"type": "string"},
                    "sensitive": {"type": "boolean"},
                },
                "required": ["name", "description"]}},
            "outputs": {"type": "array", "items": {"type": "string"}},
            "success_signal": {"type": "string"},
            "reuse": {"type": "array", "items": {"type": "string"},
                     "description": "pinned refs of existing fragments to "
                                    "replay instead of rediscovering"},
        },
        "required": ["steps", "parameters", "outputs", "success_signal"],
    },
}


def propose_plan(client, model: str, goal: str, entry_url: str,
                 digest: str, supplied: dict[str, str],
                 secrets: list[str], catalog: str = "") -> Plan:
    supplied_note = "\n".join(
        f"  {k} = {'(secret)' if k in secrets else v}" for k, v in supplied.items())
    resp = client.messages.create(
        model=model, max_tokens=1500, system=PLANNER_SYSTEM,
        tools=[PLAN_TOOL], tool_choice={"type": "tool", "name": "submit_plan"},
        messages=[{"role": "user", "content":
                   f"GOAL: {goal}\n\nENTRY: {entry_url}\n\n"
                   f"CALLER SUPPLIED VALUES (each of these is a parameter):\n"
                   f"{supplied_note}\n\nREUSABLE FRAGMENTS:\n{catalog}\n\n"
                   f"ENTRY PAGE STATE:\n{digest}"}])
    block = next(b for b in resp.content if b.type == "tool_use")
    return Plan(goal=goal, **dict(block.input))


REFINE_SYSTEM = PLANNER_SYSTEM + """

You are revising a plan a human has asked you to change. Apply their feedback
and resubmit the WHOLE plan, keeping everything they did not object to. If the
feedback would break a rule above (for example freezing a supplied value as a
constant), keep the rule and say so in the step intents."""


def refine_plan(client, model: str, plan: Plan, feedback: str,
                digest: str) -> Plan:
    """One turn of plan revision. The conversation happens here, before any
    action; execution never negotiates."""
    resp = client.messages.create(
        model=model, max_tokens=1500, system=REFINE_SYSTEM,
        tools=[PLAN_TOOL], tool_choice={"type": "tool", "name": "submit_plan"},
        messages=[{"role": "user", "content":
                   f"CURRENT PLAN:\n{plan.render()}\n\n"
                   f"PAGE STATE:\n{digest}\n\n"
                   f"HUMAN FEEDBACK:\n{feedback}\n\n"
                   f"Submit the revised plan."}])
    block = next(b for b in resp.content if b.type == "tool_use")
    return Plan(goal=plan.goal, **dict(block.input))


def prompt_for_params(plan: Plan, supplied: dict[str, str],
                      secrets: dict[str, str],
                      interactive: bool = True) -> tuple[dict, dict]:
    """Collect values for every parameter the approved plan declares.

    Values passed on the command line are used as-is; anything the plan
    declares but the caller did not supply is asked for here. Sensitive values
    are read without echo and never appear in argv, shell history, or logs.
    """
    import getpass
    params, secs = dict(supplied), dict(secrets)
    missing = [p for p in plan.parameters
               if p.name not in params and p.name not in secs]
    if not missing:
        return params, secs
    if not interactive:
        raise RuntimeError(
            f"missing values for planned parameters: "
            f"{[p.name for p in missing]} (unattended run)")
    import builtins
    print("\nThe plan needs values for these parameters:")
    for p in missing:
        hint = f" (e.g. {p.example})" if p.example else ""
        for attempt in range(3):
            if p.sensitive:
                value = prompt.ask(f"  {p.name} — {p.description}: ", True)
            else:
                value = prompt.ask(
                    f"  {p.name} — {p.description}{hint}: ").strip()
            if value:
                break
            print(f"    '{p.name}' is required.")
        else:
            raise RuntimeError(f"no value supplied for '{p.name}'")
        if len(value.strip()) < 3 and not p.sensitive:
            print(f"    note: '{value}' is very short and will be recorded "
                  f"literally rather than as a parameter. Prefer a longer, "
                  f"distinctive value while recording.")
        (secs if p.sensitive else params)[p.name] = value
    return params, secs


def review_plan(plan: Plan, interactive: bool = True) -> bool:
    """Human gate. Approving a plan is not approving the artifact: the recorded
    artifact still goes through its own review before unattended replay."""
    print("\n" + "=" * 62)
    print("PLAN PROPOSED — REVIEW BEFORE EXECUTION")
    print("=" * 62)
    print(plan.render())
    print("=" * 62)
    if not interactive:
        print("Non-interactive: plan auto-rejected (no human available).")
        return False
    return prompt.ask("Approve this plan and execute it? [y/N]: ").strip().lower() == "y"


def review_plan_interactive(client, model: str, plan: Plan, digest: str,
                            on_revision=None, interactive: bool = True):
    """Approve / edit / reject loop. Returns the approved Plan, or None.

    Editing is a conversation with the planner: describe the change, get a
    revised plan, review again. Bounded to keep an unproductive loop from
    burning tokens. Every revision is handed to `on_revision` so the whole
    negotiation lands in evidence, not just the plan that won.
    """
    if not interactive:
        print("Non-interactive: plan auto-rejected (no human available).")
        return None
    for revision in range(6):
        print("\n" + "=" * 62)
        print(f"PLAN{'' if revision == 0 else f' (revision {revision})'}"
              " — REVIEW BEFORE EXECUTION")
        print("=" * 62)
        print(plan.render())
        print("=" * 62)
        choice = prompt.ask("[a]pprove and execute, [e]dit the plan, "
                            "[r]eject: ").strip().lower()
        if choice.startswith("a"):
            return plan
        if choice.startswith("r"):
            return None
        if not choice.startswith("e"):
            print("Please answer a, e or r.")
            continue
        print("Describe the change you want (one message, blank line to send):")
        lines = []
        while True:
            line = prompt.ask("  ")
            if not line.strip():
                break
            lines.append(line)
        feedback = "\n".join(lines).strip()
        if not feedback:
            continue
        print("Revising...")
        plan = refine_plan(client, model, plan, feedback, digest)
        if on_revision:
            on_revision(revision + 1, feedback, plan)
    print("Revision limit reached without approval; nothing was executed.")
    return None


def plan_prompt_block(plan: Plan) -> str:
    """Injected into the agent loop so execution follows what was approved."""
    return (
        "A human has APPROVED the following plan. Follow it.\n\n"
        f"{plan.render()}\n\n"
        "Adapt to what the page actually shows, but stay within the plan's "
        "intent. Every value listed under PARAMETERS must be typed or selected "
        "exactly as supplied — never substitute a different value. If reality "
        "forces a material deviation from the plan, call declare_stuck instead "
        "of improvising."
    )


def verify_against_plan(artifact, plan: Plan) -> list[str]:
    """Compare the recorded artifact against the approved plan.

    This is the check that catches a parameter which was planned as an input
    but got frozen into the flow as a constant — a defect that otherwise
    surfaces only when a caller passes a new value and silently gets the old
    one's behaviour.
    """
    problems: list[str] = []
    blob = artifact.model_dump_json()
    for p in plan.parameters:
        if "{{" + p.name + "}}" not in blob:
            problems.append(
                f"parameter '{p.name}' was planned as an input but no recorded "
                f"step references it — it is frozen as a constant")
    declared = {o.name for o in artifact.outputs}
    for o in plan.outputs:
        if o not in declared:
            problems.append(f"planned output '{o}' is not produced by the recording")
    check = artifact.success.match.value
    if any(ch.isdigit() for ch in check) and plan.success_signal not in check:
        problems.append(
            f"success checkpoint '{check}' looks like a data value rather than "
            f"page state (planned signal: '{plan.success_signal}')")
    return problems
