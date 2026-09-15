"""CLI entry points.

  python -m cua discover "goal text" --url http://localhost:8080/... \\
      --id lookup_balance --input account_id=13344 --secret password=demo
  python -m cua replay lookup_balance@1.0.0 --input account_id=13344 ...
  python -m cua capabilities
  python -m cua approve lookup_balance@1.0.0
  python -m cua graph lookup_balance@1.0.0
"""

from __future__ import annotations

import builtins
import json
import sys

import typer

from .discovery import DiscoveryAgent
from .driver import PlaywrightDriver
from .library import approved_fragments, missing_inputs, render_catalog, resolve_reuse
from .planner import (propose_plan, prompt_for_params, review_plan,
                      review_plan_interactive, verify_against_plan)
from .escalation import EscalationController
from .evidence import EvidenceLog
from .policy import PolicyEngine, ProposedAction
from .recorder import record
from .redaction import Redactor
from .replay import ReplayEngine
from .schemas import ArtifactKind, RiskLevel
from .store import ArtifactStore
from .harden import harden

app = typer.Typer(add_completion=False, help="Computer use capability system")


def _resolve_api_key(api_key: str) -> None:
    """Settle the Anthropic key once, for this process only.

    The SDK reads ANTHROPIC_API_KEY from the environment and every client in
    this codebase is constructed with no arguments, so resolving it here —
    flag, then environment, then ask — means every downstream client picks it
    up without the key being threaded through, stored, or written anywhere.
    """
    import os
    if api_key:
        os.environ["ANTHROPIC_API_KEY"] = api_key
        return
    if os.environ.get("ANTHROPIC_API_KEY"):
        return
    import getpass
    typer.echo("No ANTHROPIC_API_KEY in the environment.")
    key = getpass.getpass("Anthropic API key (not echoed, not saved): ").strip()
    if not key:
        typer.echo("This command needs a model, and a model needs a key.")
        raise typer.Exit(2)
    os.environ["ANTHROPIC_API_KEY"] = key


def _clean_credential(label: str, value: str) -> str:
    """Strip stray whitespace off a credential, and say so.

    A tab or trailing space pasted (or fat-fingered) into a hidden prompt is
    invisible by definition, and produces a failure that looks like anything
    but its cause: the login is rejected, the login fragment reports
    INVALID_CREDENTIALS, the agent concludes the credentials are wrong and
    escalates to a human who can see nothing amiss. Almost no password is
    meant to carry leading or trailing whitespace, so remove it — loudly.
    """
    cleaned = value.strip()
    if cleaned != value:
        typer.echo(f"  note: stripped whitespace from {label} "
                   f"(it had a leading/trailing tab or space)")
    return cleaned


def _kv(pairs: list[str]) -> dict[str, str]:
    out = {}
    for p in pairs:
        k, _, v = p.partition("=")
        out[k] = v
    return out


@app.command()
def discover(
    goal: str,
    url: str = typer.Option(..., help="Entry URL of the target app"),
    artifact_id: str = typer.Option(..., "--id"),
    name: str = typer.Option(""),
    input: list[str] = typer.Option([], help="param=value, becomes a typed input"),
    secret: list[str] = typer.Option([], help="param=value, sensitive, never stored"),
    app_name: str = typer.Option("parabank", "--app"),
    policy: str = typer.Option("config/policy.yaml"),
    model: str = typer.Option("claude-sonnet-4-6"),
    max_steps: int = typer.Option(25),
    max_escalations: int = typer.Option(
        2, help="How many times a stuck agent may hand the browser to a "
                "human before the run gives up."),
    escalate_policy_blocks: bool = typer.Option(
        True, "--escalate-policy-blocks/--no-escalate-policy-blocks",
        help="Hand the browser to a human when the agent is stuck because "
             "POLICY refused it. On by default: the gate stops AUTOMATION, "
             "and an attended operator may still act on their own authority. "
             "Bounded by --max-escalations either way. Use "
             "--no-escalate-policy-blocks for unattended or hands-off runs."),
    headless: bool = typer.Option(False),
    plan: bool = typer.Option(True, help="Propose a plan for human approval "
                                         "before touching the browser"),
    harden_draft: bool = typer.Option(
        True, "--harden/--no-harden",
        help="Have the model supply the checkpoint and error handling the "
             "happy-path recording could not observe"),
    auto_approve: bool = typer.Option(
        True, "--auto-approve/--no-auto-approve",
        help="Approve the draft automatically when verification is clean"),
    kind: str = typer.Option("capability", "--kind",
                             help="capability | fragment"),
    reuse: bool = typer.Option(True, "--reuse/--no-reuse",
                               help="Let the plan replay proven fragments "
                                    "instead of rediscovering them"),
    smoke: bool = typer.Option(True, "--smoke/--no-smoke",
                               help="Prove the recorded artifact actually "
                                    "replays before approving it"),
    api_key: str = typer.Option(
        "", "--api-key", help="Anthropic key. Omit to use "
             "ANTHROPIC_API_KEY, or to be prompted without echo "
             "(a key typed as a flag lands in shell history)."),
):
    """Plan, get human approval, run the LLM agent, record a replayable artifact."""
    _resolve_api_key(api_key)
    params, secrets = _kv(input), _kv(secret)
    redactor = Redactor()
    for v in secrets.values():
        redactor.register(v)
    ev = EvidenceLog("evidence", "discovery", redactor)
    driver = PlaywrightDriver(headless=headless)
    esc = EscalationController(interactive=True)

    approved_plan = None
    try:
        if plan:
            import anthropic
            client = anthropic.Anthropic()
            driver.navigate(url)
            from .discovery import _digest
            digest = _digest(driver.observe(), limit=60)
            store = ArtifactStore()
            # Never offer this capability its own id: re-recording a fragment
            # would otherwise be planned as "reuse yourself", and the recorded
            # artifact references itself forever.
            catalog = (render_catalog([f for f in approved_fragments(store, app_name)
                                       if f.id != artifact_id])
                       if reuse else "(reuse disabled)")
            proposal = propose_plan(client, model, goal, url, digest,
                                    {**params, **secrets}, list(secrets),
                                    catalog=catalog)
            ev.event("plan_proposed", plan=proposal.model_dump())

            def _record_revision(n, feedback, revised):
                ev.event("plan_revised", revision=n, feedback=feedback,
                         plan=revised.model_dump())

            approved_plan = review_plan_interactive(
                client, model, proposal, digest, on_revision=_record_revision)
            if approved_plan is None:
                ev.event("plan_rejected")
                typer.echo("Plan rejected. Nothing was executed.")
                raise typer.Exit(1)
            ev.event("plan_approved", plan=approved_plan.model_dump())

            # Values the plan declares but the command line did not supply are
            # collected here, so a capability's inputs come from its contract
            # rather than from whichever flags happened to be typed.
            params, secrets = prompt_for_params(approved_plan, params, secrets)
            for v in secrets.values():
                redactor.register(v)

            # The recorder turns values back into placeholders by matching the
            # text that was typed. Two parameters holding the SAME value are
            # indistinguishable to that match, so the wrong one gets recorded
            # (a first name that replays as the username). Catch it here, while
            # it costs nothing, rather than in a replay weeks later.
            collisions: dict[str, list[str]] = {}
            for k, v in {**params, **secrets}.items():
                if v:
                    collisions.setdefault(v, []).append(k)
            clashing = [names for names in collisions.values() if len(names) > 1]
            if clashing:
                typer.echo("\nAMBIGUOUS PARAMETER VALUES:")
                for names in clashing:
                    typer.echo(f"  - {', '.join(names)} all have the same value")
                typer.echo("  The recording cannot tell them apart. Use a "
                           "distinct value for each while recording; the "
                           "capability still accepts any value on replay.")
                ev.event("ambiguous_parameters", groups=clashing)
                if not builtins.input("Record anyway? [y/N]: ").strip().lower().startswith("y"):
                    raise typer.Exit(1)

        def _ask_operator(name: str, question: str, sensitive: bool):
            """The agent needs one value it was not given. Cheaper and more
            auditable than a full session handoff for a single field."""
            from . import prompt as _prompt
            typer.echo("\n" + "-" * 62)
            typer.echo(f"THE AGENT NEEDS A VALUE: {name}")
            typer.echo(f"  {question}")
            typer.echo("  (press Enter with no value to decline)")
            typer.echo("-" * 62)
            value = _prompt.ask(f"  {name}: ", sensitive).strip()
            return value or None

        # Replay the proven chunks first, on the SAME browser session the
        # agent is about to use. Discovery then starts from a known state and
        # only has to work out what is genuinely new.
        prefix_subflows = []
        if approved_plan is not None and approved_plan.reuse:
            frags, complaints = resolve_reuse(
                ArtifactStore(), approved_plan.reuse, app_name)
            for c in complaints:
                typer.echo(f"  reuse ignored: {c}")
            available = {**params, **secrets}
            for frag in frags:
                gaps = missing_inputs(frag, available)
                if gaps:
                    typer.echo(f"  cannot reuse {frag.ref}: missing {gaps}")
                    continue
                typer.echo(f"\nReplaying proven fragment {frag.ref}...")
                sub = ReplayEngine(driver, PolicyEngine.from_yaml(policy),
                                   ArtifactStore(), ev, escalation=esc)
                res = sub.run(frag.ref, available)
                ev.event("fragment_replayed", ref=frag.ref, status=res.status.value)
                if res.status.value != "success":
                    # Name the actual outcome. "business_outcome" alone sends
                    # the agent off to rediscover a login that was in fact
                    # rejected for a reason worth reading.
                    why = (f"{res.outcome_code}: {res.outcome_message}"
                           if res.outcome_code
                           else f"{res.failed_step}: {res.observed}")
                    typer.echo(f"  fragment {frag.ref} did not succeed "
                               f"({res.status.value}) — {why}")
                    typer.echo(f"  the agent will have to reach that state itself")
                    continue
                available.update({k: str(v) for k, v in res.outputs.items()})
                prefix_subflows.append(
                    (frag.ref, {i.name: "{{" + i.name + "}}" for i in frag.inputs}))
                typer.echo(f"  {frag.ref} replayed; agent continues from there")

        agent = DiscoveryAgent(driver, PolicyEngine.from_yaml(policy), ev,
                               redactor, model=model, max_steps=max_steps,
                               escalation=esc, plan=approved_plan,
                               max_escalations=max_escalations,
                               escalate_policy_blocks=escalate_policy_blocks,
                               operator_prompt=_ask_operator)
        outcome = agent.run(goal, url, params, secrets)
        if not outcome.success:
            # Print WHY. A bare "did not succeed" sends the operator digging
            # through evidence for something the run already knows -- and when
            # the cause is a deliberate policy refusal, that is the one line
            # they need in order to stop retrying it.
            typer.echo("\nDiscovery did not succeed.")
            if outcome.failure_reason:
                typer.echo(f"\n{outcome.failure_reason}")
            typer.echo(f"\nEvidence: {ev.dir}")
            raise typer.Exit(1)

        try:
            artifact = record(outcome, artifact_id=artifact_id,
                              name=name or artifact_id, description=goal,
                              app=app_name, goal=goal, entry_url=url,
                              params=params, secrets=secrets,
                              run_id=ev.run_id, model=model,
                              plan=approved_plan,
                              kind=(ArtifactKind.FRAGMENT
                                    if kind == "fragment"
                                    else ArtifactKind.CAPABILITY),
                              prefix_subflows=prefix_subflows)
        except Exception as exc:
            # The run reached the goal but the recording violates the schema.
            # Nothing is saved: an invalid artifact must never enter the store.
            ev.event("recording_rejected", error=str(exc))
            typer.echo(f"\nRun succeeded but the RECORDING IS INVALID:\n  {exc}")
            typer.echo(f"Nothing was saved. Transcript and screenshots: {ev.dir}")
            raise typer.Exit(3)

        # Hardening runs BEFORE verification. The recording is deficient in
        # ways known in advance — a checkpoint asserting a balance that will
        # have changed by tomorrow, no detectors, no declared outcomes —
        # because the happy path could not observe them. Fix those first, or
        # verification reports problems the system was always going to correct.
        if harden_draft:
            import anthropic
            typer.echo("\nHardening the draft...")
            try:
                artifact, applied = harden(
                    anthropic.Anthropic(), model, artifact, goal, driver=driver,
                    seen_texts=outcome.seen_texts,
                    final_texts=outcome.final_texts,
                    log=lambda kind, **kw: ev.event(kind, **kw))
                for line in applied:
                    typer.echo(f"  + {line}")
                if not applied:
                    typer.echo("  (nothing to add)")
                ev.event("hardening_applied", changes=applied)
            except Exception as exc:
                # Hardening is an improvement, not a precondition: a failure
                # here leaves a plain draft rather than losing the whole run.
                typer.echo(f"  hardening failed ({exc}); draft left as recorded")
                ev.event("hardening_failed", error=str(exc))

        if approved_plan is not None:
            artifact.provenance.approved_plan = approved_plan.model_dump()
            artifact.provenance.plan_approved_by = "operator"
            problems = verify_against_plan(artifact, approved_plan)
            artifact.provenance.verification_problems = problems
            if problems:
                typer.echo("\nRECORDING DOES NOT MATCH THE APPROVED PLAN:")
                for pr in problems:
                    typer.echo(f"  - {pr}")
                ev.event("plan_verification_failed", problems=problems)
            else:
                ev.event("plan_verification_passed")

        # Save as a draft FIRST so the artifact can be replayed by ref, then
        # let that replay decide whether it earns approval.
        path = ArtifactStore().save(artifact)

        # A successful discovery run and a replayable artifact are different
        # claims. Discovery targets elements directly and settles after every
        # action; replay resolves a recorded locator ladder at full speed.
        # Approving on the strength of the run alone is how "Verification
        # clean: ready to replay" got printed for artifacts that could not
        # replay at all. So: prove it, once, here.
        smoke_ok = None
        if smoke and not artifact.provenance.verification_problems:
            pol = PolicyEngine.from_yaml(policy)
            # The step's DECLARED risk has to go in, or a submit the recorder
            # correctly marked risky reads as safe here and the "verification"
            # goes and files the loan application it was meant to avoid.
            # Same classification rule the gate applies, or the two disagree:
            # a navigate step merely DESCRIBED as "the Transfer Funds page"
            # would read as state-changing here and block auto-approval of a
            # capability that commits nothing.
            changes_state = [
                s.id for s in artifact.steps
                if pol.classify_risk(ProposedAction(
                    getattr(s, "action", "click"),
                    target_description=s.description,
                    risk=s.risk,
                    commits=(False if getattr(s, "action", "click")
                             in ("navigate", "extract") else None),
                )) != RiskLevel.SAFE]
            if changes_state:
                # Replaying would repeat a state-changing action (a transfer,
                # an application). Verifying must not itself be an act with
                # consequences, so this one is left for a human to run.
                smoke_ok = None
                typer.echo(f"\nSkipping smoke replay: {changes_state} would "
                           f"repeat a state-changing action. This capability "
                           f"cannot be auto-approved — review it and approve "
                           f"deliberately.")
                artifact.provenance.smoke_replay = "skipped: state-changing steps"
            else:
                typer.echo("\nProving the artifact replays...")
                # A clean session on the same browser: no cookies, not logged
                # in, nothing this run left behind. Replaying inside the
                # discovery session would let an existing login paper over a
                # recording that cannot actually log in by itself.
                smoke_driver = driver.fresh_session()
                smoke_ev = EvidenceLog("evidence", "smoke", redactor)
                try:
                    smoke_engine = ReplayEngine(
                        smoke_driver, PolicyEngine.from_yaml(policy),
                        ArtifactStore(), smoke_ev, escalation=None)
                    res = smoke_engine.run(artifact.ref, {**params, **secrets},
                                           allow_draft=True)
                    smoke_ok = res.status.value != "hard_failure"
                    artifact.provenance.smoke_replay = (
                        f"{res.status.value} ({smoke_ev.run_id})")
                    typer.echo(f"  {res.status.value}"
                               + (f" — failed at {res.failed_step}: {res.observed}"
                                  if not smoke_ok else ""))
                except Exception as exc:
                    smoke_ok = False
                    artifact.provenance.smoke_replay = f"error: {exc}"
                    typer.echo(f"  smoke replay errored: {exc}")
                finally:
                    smoke_driver.close()
                ev.event("smoke_replay", result=artifact.provenance.smoke_replay)

        # Auto-approval requires PROOF, not merely the absence of a failure.
        # A capability whose commit step could not be verified without
        # actually performing it is exactly the kind a human should sign off
        # on deliberately.
        approved_now = False
        if (auto_approve and not artifact.provenance.verification_problems
                and smoke_ok is True):
            from .schemas import ApprovalStatus
            artifact.status = ApprovalStatus.APPROVED
            approved_now = True
            ev.event("auto_approved")

        path = ArtifactStore().save(artifact)
        typer.echo(f"\nSUCCESS in {outcome.steps_taken} steps "
                   f"({outcome.escalations} escalation(s)).")
        typer.echo(f"Artifact: {path}")
        typer.echo(f"Evidence: {ev.dir}")
        if artifact.provenance.review_notes:
            typer.echo("\nWorth a reviewer's eye:")
            for note in artifact.provenance.review_notes:
                typer.echo(f"  ! {note}")
        if approved_now:
            typer.echo("\nApproved — proven by a replay. Risky steps still "
                       "require confirmation on every replay.")
            typer.echo("  python -m cua replay " + artifact.ref
                       + "".join(f" --input {k}=..." for k in params)
                       + "".join(f" --secret {k}=..." for k in secrets))
        else:
            if artifact.provenance.verification_problems:
                why = "it did not match the approved plan"
            elif smoke_ok is False:
                why = "it failed its smoke replay"
            else:
                why = ("it performs a state-changing action, so it could not "
                       "be proven without performing it again")
            typer.echo(f"\nLeft as a draft: {why}.")
            typer.echo(f"Approve deliberately with: python -m cua approve "
                       f"{artifact.ref}")
    finally:
        driver.close()


@app.command()
def replay(
    ref: str,
    input: list[str] = typer.Option([], help="param=value"),
    secret: list[str] = typer.Option([], help="param=value, redacted in logs"),
    policy: str = typer.Option("config/policy.yaml"),
    allow_draft: bool = typer.Option(False),
    headless: bool = typer.Option(False),
    non_interactive: bool = typer.Option(False,
        help="No human available: risky steps fail instead of prompting"),
    escalate_on_failure: bool = typer.Option(
        False, help="Ask the operator before giving up on an unanticipated "
                    "runtime failure, instead of ending the run"),
):
    """Deterministically replay an artifact. No LLM in the loop."""
    params = {**_kv(input), **_kv(secret)}
    redactor = Redactor()
    for v in _kv(secret).values():
        redactor.register(v)

    # The artifact's declared inputs are the contract. Anything required and
    # not supplied is asked for now — attended runs stay usable without
    # putting account numbers and passwords in shell history, and unattended
    # runs still fail closed rather than prompting into the void.
    import getpass
    store_ = ArtifactStore()
    try:
        contract = store_.load(ref)
    except FileNotFoundError:
        typer.echo(f"No artifact '{ref}' in the store.")
        raise typer.Exit(2)
    for spec in contract.inputs:
        if spec.required and spec.name not in params:
            if non_interactive:
                typer.echo(f"Missing required input '{spec.name}' "
                           f"(unattended run).")
                raise typer.Exit(2)
            prompt = f"{spec.name} — {spec.description}"
            eg = f" (e.g. {spec.example})" if spec.example else ""
            # A required input silently accepting an empty string is how a
            # blank password reaches the login form and the run fails three
            # steps later with an unrelated-looking error. Keep asking.
            for attempt in range(3):
                if spec.sensitive:
                    value = getpass.getpass(f"{prompt}: ")
                else:
                    value = builtins.input(f"{prompt}{eg}: ").strip()
                if value:
                    break
                typer.echo(f"  '{spec.name}' is required.")
            else:
                typer.echo("No value supplied; aborting.")
                raise typer.Exit(2)
            if spec.sensitive:
                redactor.register(value)
            params[spec.name] = value
    ev = EvidenceLog("evidence", "replay", redactor)
    driver = PlaywrightDriver(headless=headless)
    esc = EscalationController(interactive=not non_interactive)
    engine = ReplayEngine(driver, PolicyEngine.from_yaml(policy),
                          ArtifactStore(), ev, escalation=esc,
                          escalate_on_failure=escalate_on_failure
                          and not non_interactive)
    try:
        result = engine.run(ref, params, allow_draft=allow_draft)
        typer.echo(json.dumps(result.model_dump(mode="json"), indent=2))
        if getattr(result, "degraded_steps", None):
            typer.echo(
                f"\nDRIFT WARNING: {len(result.degraded_steps)} step(s) "
                f"succeeded on a fallback locator "
                f"({', '.join(result.degraded_steps)}). The run passed, but "
                f"the preferred locators no longer match — review this "
                f"capability before the fallbacks run out.")
        raise typer.Exit(0 if result.status.value != "hard_failure" else 2)
    finally:
        driver.close()


@app.command()
def capabilities():
    """List the capability catalog: name, signature, status."""
    for a in ArtifactStore().list():
        ins = ", ".join(f"{i.name}:{i.type.value}" for i in a.inputs)
        outs = ", ".join(f"{o.name}:{o.type.value}" for o in a.outputs)
        typer.echo(f"{a.ref:40s} [{a.kind.value:10s}] [{a.status.value:8s}] "
                   f"({ins}) -> ({outs})")
        typer.echo(f"    {a.description}")


@app.command()
def approve(ref: str,
            force: bool = typer.Option(
                False, help="Approve despite unresolved verification problems")):
    """Mark a reviewed artifact as approved for unattended replay."""
    try:
        a = ArtifactStore().approve(ref, force=force)
    except PermissionError as exc:
        typer.echo(str(exc))
        raise typer.Exit(2)
    typer.echo(f"{a.ref} approved."
               + (" (forced past verification problems)" if force else ""))


def _ensure_app_running(url: str, start: bool) -> bool:
    """Is the target app up? Optionally start it. Starting a container is a
    side effect on the operator's machine, so it happens only when asked."""
    import urllib.request
    def up() -> bool:
        try:
            with urllib.request.urlopen(url, timeout=3) as r:
                return r.status == 200
        except Exception:
            return False

    if up():
        return True
    if not start:
        typer.echo(f"{url} is not responding. Start it with:\n"
                   f"  docker run -d -p 8080:8080 parasoft/parabank\n"
                   f"or re-run with --start-app.")
        return False

    import subprocess, time
    typer.echo("Starting ParaBank (docker)...")
    try:
        subprocess.run(["docker", "run", "-d", "-p", "8080:8080",
                        "parasoft/parabank"], check=True,
                       capture_output=True, text=True)
    except FileNotFoundError:
        typer.echo("docker is not on PATH.")
        return False
    except subprocess.CalledProcessError as exc:
        # Most often: a container is already bound to 8080.
        typer.echo(f"docker run failed: {exc.stderr.strip()[:200]}")
    for _ in range(60):
        if up():
            typer.echo("ParaBank is up.")
            return True
        time.sleep(2)
    typer.echo("ParaBank did not come up in time.")
    return False


@app.command()
def chat(
    url: str = typer.Option("http://localhost:8080/parabank/index.htm",
                            help="Entry URL of the target app"),
    username: str = typer.Option("john", "--user"),
    password: str = typer.Option("", "--password",
                                 help="Omitted: prompted for without echo"),
    policy: str = typer.Option("config/policy.yaml"),
    model: str = typer.Option("claude-sonnet-4-6"),
    headless: bool = typer.Option(False),
    start_app: bool = typer.Option(False, "--start-app",
                                   help="Start ParaBank via docker if it is down"),
    api_key: str = typer.Option(
        "", "--api-key", help="Anthropic key. Omit to use "
             "ANTHROPIC_API_KEY, or to be prompted without echo "
             "(a key typed as a flag lands in shell history)."),
):
    """Chat to the bank. The model picks a capability and fills its typed
    arguments; the deterministic replay engine does the work."""
    _resolve_api_key(api_key)
    import anthropic
    from .chat import (SYSTEM, CapabilityChat, capability_tools,
                       describe_catalog, invocable)

    if not _ensure_app_running(url, start_app):
        raise typer.Exit(2)

    caps = invocable(ArtifactStore())
    if not caps:
        typer.echo("No approved capabilities to offer. Record one first.")
        raise typer.Exit(1)

    if not password:
        import getpass
        password = getpass.getpass(f"Password for '{username}' (never shown to the model): ")
    username = _clean_credential("username", username)
    password = _clean_credential("password", password)

    typer.echo("\n" + "=" * 62)
    typer.echo("BANK ASSISTANT")
    typer.echo("=" * 62)
    held = frozenset({"username", "password"})
    typer.echo("I can do these things, by operating the bank's own site:")
    typer.echo(describe_catalog(caps, held))
    typer.echo("\nYour credentials are held here and injected at call time — "
               "the model never sees them.")
    typer.echo("Type a request, or 'quit'.\n")

    driver = PlaywrightDriver(headless=headless)
    session = CapabilityChat(driver, policy, {"username": username,
                                              "password": password})
    tools = capability_tools(caps, held)
    client = anthropic.Anthropic()
    messages: list[dict] = []

    try:
        while True:
            try:
                said = builtins.input("you> ").strip()
            except EOFError:
                break
            if not said:
                continue
            if said.lower() in {"quit", "exit"}:
                break

            messages.append({"role": "user", "content": said})
            # Let the model call as many capabilities as the request needs,
            # bounded so a confused model cannot loop on the bank forever.
            for _ in range(6):
                resp = client.messages.create(
                    model=model, max_tokens=1200, system=SYSTEM,
                    tools=tools, messages=messages)
                messages.append({"role": "assistant", "content": resp.content})
                calls = [b for b in resp.content if b.type == "tool_use"]
                for block in (b for b in resp.content if b.type == "text"):
                    if block.text.strip():
                        typer.echo(f"\n{block.text.strip()}\n")
                if not calls:
                    break
                results = []
                for call in calls:
                    typer.echo(f"  [invoking {call.name} {dict(call.input)}]")
                    out = session.invoke(call.name, dict(call.input))
                    typer.echo(f"  [{out.get('status', 'error')}]")
                    results.append({"type": "tool_result",
                                    "tool_use_id": call.id,
                                    "content": json.dumps(out)})
                messages.append({"role": "user", "content": results})
    finally:
        driver.close()


@app.command()
def ui(
    url: str = typer.Option("http://localhost:8080/parabank/index.htm",
                            help="Entry URL of the target app"),
    username: str = typer.Option("john", "--user"),
    password: str = typer.Option("", "--password"),
    policy: str = typer.Option("config/policy.yaml"),
    model: str = typer.Option("claude-sonnet-4-6"),
    port: int = typer.Option(8765),
    headless: bool = typer.Option(False, help="Headless hides the browser the "
                                              "agent drives; leave off to watch it"),
    start_app: bool = typer.Option(False, "--start-app",
                                   help="Start ParaBank via docker if it is down"),
    api_key: str = typer.Option(
        "", "--api-key", help="Anthropic key. Omit to use "
             "ANTHROPIC_API_KEY, or to be prompted without echo "
             "(a key typed as a flag lands in shell history)."),
):
    """Open a chat window that can invoke, discover and approve capabilities."""
    _resolve_api_key(api_key)
    import anthropic
    import webbrowser
    from .ui import Session, serve

    if not _ensure_app_running(url, start_app):
        raise typer.Exit(2)
    if not password:
        import getpass
        password = getpass.getpass(f"Password for '{username}' "
                                   f"(held here, never sent to the model): ")
    username = _clean_credential("username", username)
    password = _clean_credential("password", password)

    session = Session(url, policy, model,
                      {"username": username, "password": password}, headless)
    typer.echo(f"\nChat UI on http://127.0.0.1:{port}  (ctrl-c to stop)")
    typer.echo("Questions from a discovery run — plan approval, confirmations —"
               " appear in the chat.\n")
    try:
        webbrowser.open(f"http://127.0.0.1:{port}")
    except Exception:
        pass
    try:
        serve(session, anthropic.Anthropic(), model, port)
    except KeyboardInterrupt:
        typer.echo("\nStopping.")
    finally:
        session.close()


@app.command()
def graph(ref: str):
    """Print the dependency graph (capability -> fragments)."""
    ArtifactStore().print_graph(ref)


if __name__ == "__main__":
    app()