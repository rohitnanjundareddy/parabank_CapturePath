# REPORT

## 1. Architecture

A single Python process with strict module boundaries rather than services. At this scale process boundaries would add failure modes without adding capability, and every seam that matters is already an interface in code.

```mermaid
flowchart TB
    goal["Goal in natural language"]

    subgraph learn ["LEARN ONCE — the only place a model drives a surface"]
        direction TB
        plan["<b>planner</b><br/>propose a plan for approval"]
        agent["<b>discovery</b><br/>observe → decide → act"]
        rec["<b>recorder</b><br/>distil transcript into an artifact"]
        hard["<b>harden</b><br/>model proposes checkpoint + detectors<br/>code validates them against the run"]
        smoke["<b>smoke replay</b><br/>clean, cookie-isolated session"]
        plan --> agent --> rec --> hard --> smoke
    end

    goal --> plan
    smoke -->|"proved"| store
    smoke -->|"unproved, or contains a commit"| draft["<b>draft</b><br/>awaits deliberate approval"]
    draft -.->|"human signs off"| store

    store[("<b>artifact store</b><br/>typed · versioned · pinned composition<br/><i>the frozen boundary</i>")]

    subgraph run ["RUN MANY — no model anywhere in this path"]
        direction TB
        replay["<b>replay engine</b><br/>flatten → substitute → execute → verify checkpoint"]
        result["success · business_outcome · hard_failure<br/>each output carries the text it was read from"]
        replay --> result
    end

    tools["<b>chat / ui</b><br/>catalog as callable tools<br/>model picks a capability, fills typed args, stops"]

    store --> replay
    store -.->|"typed signatures"| tools
    tools -->|"invoke by name"| replay

    subgraph foundation ["SHARED FOUNDATION — no route to the surface avoids these"]
        direction TB
        policy["<b>policy gate</b><br/>allowlist · commit detection · risk"]
        driver["<b>driver</b><br/>observe / act / wait / session ownership"]
        policy --> driver
    end

    agent ==>|"every proposed action"| policy
    replay ==>|"every recorded action"| policy
    driver --> surface[("live bank UI")]

    human(["human operator"])
    human -.->|"approves the plan, answers the agent,<br/>takes the session when it is stuck"| agent
    human -.->|"confirms each commit,<br/>takes the session on escalation"| replay
```

**What the diagram is asserting.** Read it top to bottom: a goal enters once, on the left-hand path, and what comes out is not an answer but an *artifact*. Everything below the store runs without a model.

Four properties are structural rather than conventional, and each is visible in the shape:

1. **The store is a one-way boundary.** The model's reasoning goes in; frozen data comes out. Nothing on the `RUN MANY` path can call back into the `LEARN ONCE` path — there is no edge going up. That is what makes replay deterministic: not discipline, but the absence of a route.
2. **Approval is a gate with a proof behind it**, not a status field. The only edge into the store from discovery passes through the smoke replay. A capability that cannot be proved without performing it — one containing a commit — is routed to `draft` and can only reach the store through a human.
3. **Two engines, one chokepoint.** Both heavy arrows converge on the policy gate, and the driver is the sole thing that touches the surface. There is no code path to the browser that avoids either, so a guardrail cannot be bypassed by adding a caller.
4. **The agent-facing layer never touches the surface.** It reads typed signatures out of the store and invokes capabilities by name; its arrow goes to the replay engine, never to the driver. The model that talks to a customer chooses *which* capability to run and with what arguments — and then stops.

The principle underneath all four: **all intelligence happens at discovery and review time and is frozen into data; runtime only executes data.** The model proposes single structured actions through tool calling; my code validates, gates, executes and records them. The model never holds the browser, never chooses a locator at runtime, and never decides what a page means.

The hardest lesson of this build, and the thing that shaped the final architecture, is that **a successful discovery run does not imply a replayable artifact.** The two paths differ in every dimension that matters:

| | Discovery | Replay |
|---|---|---|
| Targeting | the exact element the model pointed at (a stamped handle) | a *reconstructed locator ladder*, never exercised while recording |
| Pacing | settles after every action | full speed |
| Entry navigation | performed by the loop itself | must come from a recorded step |
| Adaptation | the model recovers from its own mistakes | none |

Every serious defect I hit lived in that gap: an entry navigation discovery did for itself and therefore never recorded; waits discovery never needed because it always settled; a locator ladder discovery never used because it addressed elements by handle. Each looked like a browser problem and was really a *recording* problem.

So the pipeline no longer asserts that an artifact works — it proves it:

```
goal → plan (human-approved) → LLM run → record → harden → verify against plan → SMOKE REPLAY → approve
```

The smoke replay executes the just-recorded artifact once in a **cookie-isolated browser context** — a clean session, not logged in, holding nothing the discovery run left behind — and approval is gated on it passing. Nothing reaches `approved` because a discovery run went well; it reaches `approved` because the artifact itself ran. Capabilities containing a state-changing commit are the deliberate exception: verifying them would mean performing them again, so they skip the smoke replay, are never auto-approved, and wait for a human.

**Key trade-off.** Element targeting is captured from the accessibility properties and structure of the live element at action time (roles, labels, row anchors) rather than by screenshot-and-coordinates. Coordinates generalize best to desktop surfaces but are the least reviewable and least stable representation. Semantic locators can be read in a pull request and degrade gracefully. The driver seam (§4) is where a coordinate-based implementation slots in for a surface that offers nothing better.

## 2. Artifact schema

The artifact is a typed capability contract, not a step list: identity and pinned semver, typed inputs (with a sensitive flag — secrets are injected at runtime and never stored), typed outputs, declared business outcomes the caller must handle, ordered steps, a success checkpoint, and provenance.

**Locator candidates, not selectors.** Every target is a ranked ladder — accessible role and name, form label, input placeholder, a row-anchored relative locator for id-less legacy tables, a label-adjacency rung, structural CSS, and visible text as a last resort — each rung carrying a recorded rationale. Robustness lives in the data, where a reviewer can see it.

Two rungs are only a ladder if they fail *differently*. For the dominant legacy shape, a label/value table row (`Status: | Denied`), the artifact records both `Status:||td:nth-child(2)` (count columns inside the anchored row) and `td:has-text("Status:") + td` (take the cell after the label). Inserting a column breaks the first and not the second.

**Three rules govern what may enter a ladder**, and each came from a defect that reached a replay:

1. **Data may never become a locator.** An element's `value` is a label on a button but the user's data on a text field, and on a `<select>` it is whichever option happens to be selected. ParaBank's pre-filled contact form produced `role_name: "smith"` on the Last Name field and `role_name: "12456"` on a dropdown — locators that match only while the field still holds that value.
2. **Every rung must identify the same element.** A lookup for account `{{account_id}}` had `a[href*="activity.htm"]` as its third rung: "any account link". Asked for an account that does not exist, the specific rungs correctly found nothing, the ladder fell through to the generic one, took the first match, and returned **a different customer's balance as a confident success** — with evidence corroborating the wrong number. The preferred rung now defines identity: a rung carrying fewer parameters is dropped at record time and skipped at replay time.
3. **A rung matching many elements identifies none.** Four buttons on ParaBank's search form share the label `FIND TRANSACTIONS`; the role-and-name rung matched all four and taking the first silently clicked the wrong one. Resolution now prefers a rung that resolves *uniquely*, falling back to an ambiguous match only when no rung is unique — and recording that it did so.

**Declared error handling.** Steps carry detectors: a match condition mapped to exactly one of four actions — `business_outcome`, bounded `recover`, `escalate`, or `hard_failure`. The most common design mistake in this space is conflating a legitimate result such as "no such account" with a crash. Here the distinction is encoded in the artifact and enforced by the result type.

**Composition frozen at review time.** Capabilities reference reusable fragments by pinned id and version through `run_subflow` steps. Replay flattens them mechanically and never chooses between alternatives at runtime; selection happens at plan time, where a human reviews it. A self-referential capability — which the planner *will* propose if it is shown its own id — is refused with a clear error rather than recursing until the stack dies.

**Provenance carries the review trail, not just the origin:** the approved plan, `verification_problems` (recording versus plan), `hardening_rejected` (what the model proposed and why it was thrown out), `smoke_replay` (the proof, or why there is none), and `review_notes` (non-blocking observations — currently steps with a single locator rung, which cannot degrade, only fail).

## 3. Determinism & error handling

Determinism comes from four properties: no model calls, pinned composition, explicit waits, and a final checkpoint so success is asserted rather than assumed.

**Waits are recorded from what the action actually did.** Discovery observes where the browser ended up and what appeared, and the recorder turns that into the step's declared `wait_after`: a `url_matches` on the **path only** — never the host, so the artifact ports to another tenant's deployment — when the action navigated, or a `text_visible` on text that appeared when it did not. That second case matters more than it sounds, because legacy apps post back to the same URL constantly and a URL check alone declares no wait at all.

**Waiting for the surface is the driver's job, not the engine's.** An action is not complete because it was issued; it is complete when the surface shows its effect. Waiting on load state cannot express that on a legacy app, because the *old* page is already loaded — ParaBank reported "settled" in 50ms and rendered 300ms later. So every primitive that can move the page — navigate, click, select, press, and the discovery-time handle helpers — captures a page fingerprint (URL, element count, body text length), performs the action, waits for the page to actually *differ*, then waits for content to stop arriving. This lives in the driver precisely so that no caller has to remember it: an earlier version put it in the engines, and replay covered `click` but not `select`, which in legacy apps commonly posts back from an `onchange` handler.

**Errors follow a fixed evaluation order.** When a step fails or its wait times out, declared detectors are consulted first; a matching detector's action wins, and bounded recovery re-runs the step. If the ladder is exhausted, the step's `on_exhausted` applies. Detectors are also checked after *apparently successful* steps, because a legacy app will happily render an error banner inside a page that loaded fine.

```mermaid
flowchart TB
    start(["step begins"]) --> gate{"policy gate"}
    gate -->|"block"| hf["<b>hard_failure</b><br/>step · expected · observed · screenshot"]
    gate -->|"commit needs a human"| confirm{"confirmed?"}
    confirm -->|"no, or unattended"| hf
    confirm -->|"yes"| act
    gate -->|"allow"| act["resolve locator ladder<br/>act · wait for the surface"]

    act --> ok{"acted, and<br/>wait_after met?"}
    ok -->|"yes"| post{"detectors, checked even<br/>on apparent success"}
    post -->|"none match"| done["<b>success</b><br/>typed outputs + evidence"]
    post -->|"matches"| terminal

    ok -->|"no"| det{"do declared<br/>detectors match?"}
    det -->|"yes"| terminal{"declared action"}
    det -->|"no"| exh["on_exhausted"]
    exh --> terminal

    terminal -->|"recover, budget left"| act
    terminal -->|"business_outcome"| bo["<b>business_outcome</b><br/>a legitimate answer,<br/>not an error"]
    terminal -->|"escalate — one handoff per step"| hand["human takes the live session"]
    terminal -->|"hard_failure"| hf
    hand --> act
```

The shape carries the argument. There is **no edge from a failure to the next step** — a step that fails can only recover within its budget, become a declared outcome, reach a human, or stop the run. Silently proceeding is not an unimplemented case; it is an absent edge. Equally, `business_outcome` is a *terminal state of its own*, not a subtype of failure: "no such account" leaves by a different exit than "I could not operate the page", which is the distinction the whole error taxonomy exists to preserve.

**The result contract has exactly three statuses:** `success` with typed outputs, `business_outcome` with a stable code and message, or `hard_failure` naming the failed step, what was expected, what was observed, and a screenshot.

**An answer must be traceable to something read.** Each output carries evidence: the raw source text before parsing, which rung found it, and when. A caller can check `balance: 529.1` against `source_text: "$529.10"` without re-running anything. Correspondingly, an extract that resolves an element but reads *nothing* is not a success — it routes through the step's declared error handling, because returning an empty string hands the caller a blank answer that looks authoritative. Parsing fails loudly rather than guessing, and preserves accounting negatives (`($50.00)` and `-$980.90` both become negative numbers) instead of silently dropping the sign.

**Drift is a distinct signal from failure.** Per-step reports record which rung matched. A step that succeeded on a *fallback* rung appears in `degraded_steps` with a warning: the run passed, but the preferred description of that element has stopped matching. This counts only rungs that stopped *matching* — a rung the driver refused because it resolved the wrong kind of element is a defect in the recording, not a page that moved, and must not raise a false alarm.

## 4. Heterogeneity & multi-tenant

**Surface abstraction.** The seam is the driver protocol: observation returns a normalized element digest, act primitives execute, and detector matching plus session ownership live behind the same interface. Artifacts describe intent ("click the element found by this ladder") rather than mechanism, so a legacy frameset app means a driver that walks frames during observation, and a desktop app means a driver over OS accessibility APIs, where role and name locators carry over directly and coordinate candidates become the fallback rung. Engines and artifacts are untouched in both cases.

Two observation decisions generalize beyond this target. First, the digest is bounded for prompt size, but **bounding must never be the reason the agent cannot see its data** — a long account table runs past any cutoff, so a text search over the *whole* observation returns handles for anything the digest omitted. (The limit was once tuned against a page that turned out to be only partly rendered when it was measured, so the cutoff sat just under the real element count and the rows nearest the bottom of a table vanished. A limit calibrated against an incomplete observation is worse than no limit, because it looks deliberate.) Second, **a collection must be addressable**: with only individual cells observable, a goal like "read the transactions" has no element that represents it, and the agent reads one cell, cannot express the list, and retries until it gives up. Data tables are therefore observable as single units — those with header cells, or with several rows — while layout tables are not, because a legacy page is *built* from tables and including them all buries the rows that matter.

**Multi-tenant reuse.** The store is a graph, and fragments are the built form of it: record login once as a fragment, prove it by replaying it, and thereafter every capability needing a session references it by pinned version instead of rediscovering it. One fix to the fragment reaches every capability that uses it. Tenants would be modelled as overlays that override specific fragments or individual locator ladders rather than whole capabilities — two tenants on the same vendor product share the base capability, and the one with a rebranded login overrides only that fragment. This is also why declared waits assert paths and never hosts. Drift detection falls out of replay telemetry: `degraded_steps` flags a tenant sliding down the ladder before failures start. At production scale these relationships belong in a queryable graph store, to answer blast-radius questions such as which tenants break if this fragment changes; at this scale JSON references and the `graph` command are the honest version of the same structure.

## 5. Escalation & handoff

Stuck is detected three ways: the discovery model explicitly declares it cannot proceed safely (prompted to prefer this over guessing), a replay detector or exhaustion declares escalate, or a state-changing action needs a decision. An intervention request carries the goal, the step, the reason, the current URL and a screenshot.

**Control transfer is enforced at the driver, which owns the session.** Ceding control marks the session human-held and installs listeners that record the human's clicks and changes; any automation action while a human holds the session raises. The human works in the same already-open window, signals completion and describes what they did; the recorded actions and the note enter the run's evidence; control returns; discovery re-observes and continues, or replay re-runs the step with the checkpoint still gating success. One handoff per step, so a step that fails again afterwards becomes a debuggable failure rather than a loop.

**Confirmation is a different mechanism from handoff.** The agent decides whether to *submit*; the bank decides whether to approve. A control that commits a form is `risky` and always asks a human first — on both paths, during discovery as well as replay. The prompt shows what is actually being committed, the values entered into that form with secrets excluded, because a button label alone is not informed consent. Unattended runs fail closed on any such step.

**Where a question goes is a routing decision, not a call-site decision.** Plan approval, risky confirmations, operator questions and handoffs all reach the operator through one per-thread prompter. The terminal is the default; a chat window installs its own for the duration of a run, so the planner, recorder, escalation controller and agent loop never learn that a UI exists — and a background run cannot steal another surface's input. The mechanism underneath (pause, cede, record, resume, verify) is the real deliverable; the operator surface is deliberately thin.

## 6. Safety

A single policy gate sits below both execution paths, so there is structurally no route to the browser around it. It enforces a domain allowlist, an action-type allowlist, and blocked route patterns (the admin console and money-movement routes are blocked by default). Effective risk is the maximum of declared and inferred risk, so a mislabelled step cannot downgrade itself.

**Reading labels to judge risk failed in both directions, so the test is structural.** A loan application sailed through as safe because no regex covered it; later a plain navigation link reading "Open New Account link" matched `open.*account` and turned a page change into a confirmation prompt. Those patterns were a proxy for "this control commits state". A control that commits a form is now detected as such — a real submit, or a button inside a form, which is how legacy apps commit via JavaScript (ParaBank's is literally `<input type="button" value="Apply Now">`) — and that risk is recorded *in the artifact* at record time rather than re-derived from a regex at runtime. A reviewer can lower it deliberately; policy still takes the maximum, so it cannot be downgraded silently. With commits detected directly, the risky-pattern list is empty: a lexical proxy alongside a structural test buys nothing and costs false positives. The irreversible-pattern list stays, because its job is different — refusing to go somewhere at all, including by following a link, which structure cannot tell you.

I tried a cleaner rule first and **rejected it on evidence.** HTTP semantics say GET is safe and POST commits, which would be a principled signal rather than a structural guess. On ParaBank it is exactly backwards: the login form is the only POST, while the loan application, open-account and update-profile forms are all GET, submitting through JavaScript handlers. In a legacy app the markup's own declared semantics are not trustworthy either, which is precisely why the test has to be structural.

One carve-out is deliberate: **a form containing a password field is authentication**, and submitting it establishes a session rather than committing business state. Without it, logging in would be a state change, every capability that logs in would demand a human, and unattended replay would not exist.

**Data handling.** Secrets and sensitive parameters are registered with a redactor that masks them in every evidence line *at write time*; artifacts store placeholders rather than values; defensive patterns scrub SSN- and card-shaped data; a recorded wait can never reference a secret; and output descriptions record the *shape* of what was read rather than the value, because a real balance is regulated data and the artifact is a file that gets committed and reviewed. In the agent-facing layer, sensitive inputs are stripped from the tool schema entirely and injected by the runtime, so the model cannot leak a credential it has no parameter for.

**On trusting a model to harden a recording.** An earlier version of this report listed automatic detector inference as something I had deliberately cut, on the grounds that guessing failure modes from one happy-path run was unsafe. I reversed that, because shipping drafts with no detectors and no declared outcomes was worse, and because the intelligence still happens at record time and freezes into data. But the original concern was correct, and the model demonstrated it repeatedly: it proposed `"Accounts Overview"` (the page's own heading) as proof an account was missing, `"Customer Login"` as proof the login form was broken, and once an empty match string. Each would have turned every successful run into a false failure. So proposals are **validated against reality** before they are applied — a detector whose text was on screen during the run that *succeeded* cannot be evidence of failure — and every rejection is recorded on the artifact. The model proposes; code validates.

**Limits worth stating plainly.** The allowlist is only as good as its configuration. Redaction cannot mask a secret it was never told about. The structural commit test is deliberately conservative, so a read-only search form asks for confirmation until a reviewer lowers it. And the authentication carve-out misclassifies a *change-password* form, which contains a password field but genuinely does change state; pattern rules and the reviewer are the backstop there, since effective risk is the maximum of declared and inferred.

## 7. Cuts

**Cut deliberately, and why:**

- **A full real-time co-browsing operator console.** The chat window and the terminal handoff both exercise the real mechanism — pause, cede, record, resume, verify — and a console would sit on exactly those calls. The seam is real; the surface is thin on purpose.
- **Desktop and legacy frameset drivers.** The driver protocol is the deliverable, and §4 describes what those implementations look like. Building them would have added surface area, not judgment.
- **Turning the human's handoff actions into artifact steps.** When an operator completes a transfer by hand, those actions reach evidence but never become replayable steps, so a human-assisted discovery yields a capability that cannot replay the human's part. Verification catches this and refuses approval — visible on the transfer demo, where three parameters are reported frozen and the planned output missing. Translating recorded DOM events into robust locator ladders is a large piece of work with a poor robustness story, and a draft that honestly refuses approval is better than one that silently replays half a flow.
- **Typed list outputs.** A table is extracted as one block of text, because the schema has no repeated-extract step. Honest and usable, but "typed outputs and their shape" deserves better than a string the caller has to parse.
- **Embedding-based capability matching and multi-run stability scoring.** The catalog lists typed signatures, which is the substrate for the first; `degraded_steps` is a per-run version of the second.

**Known weaknesses I would fix first, in order:**

1. **A missed locator can still become a confident wrong answer.** An extract whose ladder misses can be mapped to a business outcome by `on_exhausted`. The ladder-identity rules make absence genuinely *mean* absence, and `review_notes` surfaces single-rung steps — but the real fix is distinguishing "the row is genuinely absent" from "I could not find the row" before mapping either to an outcome.
2. **A repeated-extract step type** producing a typed list, replacing the text block described above.
3. **Hardening attaches detectors to whatever steps exist**, so a thin recording collects transfer-related detectors on its entry navigation. Harmless, since the text never matches, but noise in an artifact meant to be reviewed.
4. **Cross-tenant overlays are designed, not built.** The fragment layer is the substrate; the next step is demonstrating one artifact against two variants of the same app with a single overridden fragment.
5. **Assisted fallback:** a single-step, policy-checked LLM recovery on replay failure, recorded as evidence and never open-ended.
6. **A golden corpus:** saved page snapshots with human-validated expected extractions, replayed as regression tests, so recording quality is measured rather than discovered in production.
