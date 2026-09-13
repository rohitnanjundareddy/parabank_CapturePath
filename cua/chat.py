"""Agent-facing capability interface: the catalog as callable tools.

This is the layer the whole system exists to serve. The agent-facing product
decides WHAT to do; this system is how it reliably does it. So the model here
picks a capability and fills in its typed arguments — and then stops. It never
drives the browser, never chooses a locator, never decides what a page means.
Execution is the same deterministic replay engine used everywhere else, which
is what makes the answer come from the bank's page rather than from the model.

Three properties worth stating, because they are what separate this from a
chatbot that sounds confident:

1. Credentials are never exposed to the model. Sensitive inputs are stripped
   out of the tool schema entirely and injected by the runtime at call time,
   so the model cannot pass, leak, or hallucinate a password — it has no
   parameter to put one in.
2. The model may not answer from memory. Every factual claim has to come from
   a tool result, and each numeric result carries the source text it was read
   from, so an answer can be checked against the page it came from.
3. A business outcome is an answer, not an error. "No such account" comes back
   as a result the model relays plainly, exactly as the artifact declared it.
"""

from __future__ import annotations

import re
from typing import Optional

from .escalation import EscalationController
from .evidence import EvidenceLog
from .policy import PolicyEngine
from .redaction import Redactor
from .replay import ReplayEngine
from .schemas import ApprovalStatus, Artifact, ArtifactKind, ParamType
from .store import ArtifactStore

SYSTEM = """You are a banking assistant operating on a customer's behalf.

You have no direct knowledge of this customer's accounts. Everything you state
about their money must come from invoking one of your capabilities, which
operate the bank's own web application and return what the page actually said.

Rules:
1. NEVER state a balance, account number, status or any other account fact
   that did not come back from a tool result in this conversation. You have no
   memory of their accounts and no ability to guess one.
2. If no capability covers what was asked, say so plainly and tell them what
   you CAN do. Do not approximate with a capability that answers a different
   question.
3. A business outcome (for example "no such account") is a legitimate answer,
   not a failure. Relay it plainly and without alarm.
4. If a call fails, say what failed. Do not retry the same call repeatedly and
   do not present a failure as a result.
5. When you report a number, report it as the page showed it — the tool result
   carries the exact source text it was read from.
6. Be brief. A sentence or two, unless asked for detail."""

_JSON_TYPE = {ParamType.STRING: "string", ParamType.NUMBER: "number",
              ParamType.BOOLEAN: "boolean"}
_PLACEHOLDER = re.compile(r"\{\{(\w+)\}\}")


def invocable(store: ArtifactStore) -> list[Artifact]:
    """Capabilities an agent may call: approved, and not a fragment.

    A fragment is a building block, not an offer — it is reachable only
    through the capability that pins it. A draft is excluded because nothing
    has proven it replays.
    """
    return [a for a in store.list()
            if a.kind == ArtifactKind.CAPABILITY
            and a.status == ApprovalStatus.APPROVED]


def _readable(text: str) -> str:
    """'account {{account_id}}' -> 'account <account_id>'. The raw goal is
    accurate but placeholder syntax is noise to a person."""
    return _PLACEHOLDER.sub(r"<\1>", text)


def capability_tools(caps: list[Artifact],
                     runtime_supplied: frozenset[str] = frozenset()) -> list[dict]:
    """Turn the catalog into tool definitions the model can call.

    The typed contract in the artifact IS the function signature — that is the
    point of having typed it. Anything the runtime supplies is deliberately
    omitted: sensitive values obviously, but also the identity of the customer
    whose session this is. The model is answering "what does this person want
    done", not "who are they", and it cannot pass a credential it has no
    parameter for.
    """
    tools = []
    for cap in caps:
        props, required = {}, []
        for i in cap.inputs:
            if i.sensitive or i.name in runtime_supplied:
                continue            # supplied by the runtime, never by the model
            props[i.name] = {"type": _JSON_TYPE.get(i.type, "string"),
                             "description": i.description}
            if i.example:
                props[i.name]["description"] += f" (e.g. {i.example})"
            if i.required:
                required.append(i.name)

        returns = ", ".join(f"{o.name} ({o.type.value})" for o in cap.outputs) or "nothing"
        outcomes = "; ".join(f"{b.code}: {b.description}"
                             for b in cap.business_outcomes)
        desc = f"{_readable(cap.description)}\n\nReturns: {returns}."
        if outcomes:
            desc += f"\nMay report these known outcomes instead: {outcomes}"

        tools.append({"name": cap.id, "description": desc,
                      "input_schema": {"type": "object", "properties": props,
                                       "required": required}})
    return tools


def describe_catalog(caps: list[Artifact],
                     runtime_supplied: frozenset[str] = frozenset()) -> str:
    """What to tell a person they can ask for. Derived from the same typed
    contract the model sees, so the two can never drift apart."""
    if not caps:
        return ("No approved capabilities yet. Record one with "
                "`python -m cua discover ...` first.")
    lines = []
    for cap in caps:
        args = ", ".join(i.name for i in cap.inputs
                         if not i.sensitive and i.name not in runtime_supplied)
        needs = f"      (you supply: {args})" if args else "      (nothing to supply)"
        lines.append(f"  - {_readable(cap.description)}\n{needs}")
    return "\n".join(lines)


class CapabilityChat:
    """Holds the browser session and credentials for a conversation.

    One driver for the whole session: each invocation is still an independent
    deterministic replay, but they share a browser so the window stays open and
    the operator can watch what is being done on their behalf.
    """

    def __init__(self, driver, policy_path: str, credentials: dict[str, str],
                 store: Optional[ArtifactStore] = None,
                 interactive: bool = True):
        self.driver = driver
        self.policy = PolicyEngine.from_yaml(policy_path)
        self.store = store or ArtifactStore()
        self.credentials = credentials
        self.redactor = Redactor()
        for value in credentials.values():
            self.redactor.register(value)
        self.escalation = EscalationController(interactive=interactive)
        self.caps = {c.id: c for c in invocable(self.store)}

    def invoke(self, name: str, args: dict) -> dict:
        """Run one capability and return a result compact enough to hand back
        to the model, without the per-step telemetry it has no use for."""
        cap = self.caps.get(name)
        if cap is None:
            return {"error": f"no capability named '{name}'"}

        params = {k: str(v) for k, v in args.items()}
        # Credentials are attached HERE, not by the model. Only what this
        # capability actually declares as an input is passed on.
        declared = {i.name for i in cap.inputs}
        params.update({k: v for k, v in self.credentials.items() if k in declared})

        missing = [i.name for i in cap.inputs
                   if i.required and i.name not in params]
        if missing:
            return {"error": f"missing required input(s): {missing}"}

        ev = EvidenceLog("evidence", "chat", self.redactor)
        # Every invocation runs in a CLEAN session. A capability is
        # self-contained — it navigates to the entry URL and logs itself in —
        # so replaying it into a browser that is already authenticated means
        # the login page never appears and the very first step fails looking
        # for a username field. Independent invocations is also what
        # "deterministic replay" has to mean: the second call cannot depend on
        # what the first one left behind.
        fresh = getattr(self.driver, "fresh_session", None)
        driver = fresh() if fresh else self.driver
        engine = ReplayEngine(driver, self.policy, self.store, ev,
                              escalation=self.escalation)
        try:
            result = engine.run(cap.ref, params)
        except Exception as exc:
            return {"status": "error", "detail": str(exc), "evidence_dir": ev.dir}
        finally:
            if fresh:
                try:
                    driver.close()
                except Exception:
                    pass

        out = {"status": result.status.value,
               "capability": result.capability,
               "evidence_dir": result.evidence_dir}
        if result.outputs:
            out["outputs"] = {k: v for k, v in result.outputs.items()}
            out["read_from_page"] = {k: e.source_text
                                     for k, e in result.output_evidence.items()}
        if result.outcome_code:
            out["outcome"] = result.outcome_code
            out["outcome_message"] = result.outcome_message
        if result.status.value == "hard_failure":
            out["failed_step"] = result.failed_step
            out["expected"], out["observed"] = result.expected, result.observed
        return out
