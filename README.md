# Computer Use Automation System

A system that uses an LLM once to discover how to accomplish a goal in a legacy banking UI, records the successful run as a typed, versioned capability artifact, proves that artifact replays, and then replays it deterministically with no model in the loop. Includes policy guardrails, redacted evidence logging, and human escalation with control transfer on the live session.

Target application: ParaBank, a demo banking site built by Parasoft for automation practice. Run it locally with Docker (preferred) or use the public instance.

## Setup

1. Python 3.11 or newer.
2. Install dependencies and the browser:

```
pip install -r requirements.txt
playwright install chromium
```

3. Set your model API key:

```
export ANTHROPIC_API_KEY=sk-ant-...       # PowerShell: $env:ANTHROPIC_API_KEY="sk-ant-..."
```

4. Start the target app locally:

```
docker run -d -p 8080:8080 parasoft/parabank
```

The app is then at http://localhost:8080/parabank/index.htm. To run without local services, substitute the public instance URL https://parabank.parasoft.com/parabank/index.htm (already in `config/policy.yaml`). Register a throwaway user on the site first; never use real credentials.

Only `discover` needs the API key. Everything else — `replay`, `capabilities`, `graph`, `approve`, and the test suite — runs without one.

## Demo path

**Step 1. Record login once, as a reusable fragment.** Small proven chunks make longer flows reliable: a fourteen-step discovery gives the model fourteen chances to choose badly.

```
python -m cua discover "Log in with the given username and password and reach the Accounts Overview page" \
  --url http://localhost:8080/parabank/index.htm \
  --id parabank_login --kind fragment \
  --input username=john --secret password=demo
```

You will be shown a plan to approve before anything touches the browser. The run then records a draft, the model supplies the checkpoint and error handling the happy path could not observe (proposals that fail validation are rejected and the rejection is recorded), and the artifact is **replayed once from a clean browser session** before it is allowed to be approved.

**Step 2. Record a capability that reuses it.** The planner is shown the catalog of proven fragments; if it lists one under `REUSES`, that fragment is replayed deterministically and the model only explores what is genuinely new.

```
python -m cua discover "Open Accounts Overview and read the Total row into an output named total_balance" \
  --url http://localhost:8080/parabank/index.htm \
  --id read_total_balance \
  --input username=john --secret password=demo
```

**Step 3. Deterministic replay with new parameters, no LLM involved:**

```
python -m cua replay read_total_balance@1.0.0 --input username=john --secret password=demo
```

Prints a structured result: status, typed outputs, the evidence each output was read from, and per-step reports. Exit code 0 for success or a business outcome, 2 for a hard failure.

**Step 4. Replay that hits an exceptional state** — a nonexistent account returns the declared business outcome rather than crashing:

```
python -m cua replay lookup_account_balance_v2@1.0.0 \
  --input username=john --input account_id=99999 --secret password=demo
```

**Step 5. Inspect the composition and the catalog:**

```
python -m cua graph read_total_balance@1.0.0     # capability -> pinned fragments
python -m cua capabilities                       # signatures and approval status
python -m cua approve <ref>                      # deliberate human approval
```

## Escalation demo

During discovery the model may declare itself stuck; during replay a step may declare escalate. In both cases the run pauses, an intervention request with context and a screenshot is printed, and the already-open browser window is handed to you. Perform the manual steps, describe what you did, press Enter, and the run resumes on the same session. Everything you clicked is recorded into the run's evidence. Automation is physically prevented from acting while you hold the session.

To trigger it, ask for something policy blocks:

```
python -m cua discover "Open the Transfer Funds page and transfer 100 dollars from account 13344 to account 13566" \
  --url http://localhost:8080/parabank/index.htm --id transfer_demo \
  --input username=john --input account_id=13344 --secret password=demo
```

Note what happens afterwards: the actions you performed by hand are captured as evidence but do not become replayable steps, so verification reports the planned parameters as unused and **refuses to approve the recording**. That is intentional — see REPORT.md §7.

## Confirmation on state-changing actions

A control that commits a form is detected structurally (a real submit, or a button inside a form — legacy apps commit from `<input type="button">` via JavaScript) and recorded as `risky`. Replaying such a step shows what is about to be submitted and asks first:

```
==============================================================
CONFIRM STATE-CHANGING ACTION
  step s06_click 'Apply Now button': risky action
  About to submit:
    loan_amount = 1500
    down_payment = 250
==============================================================
Proceed? [y/N]:
```

Unattended runs (`--non-interactive`) fail closed on these steps rather than proceeding. Capabilities containing one are not auto-approved, because proving them would mean performing them again.

## Tests

```
python -m pytest tests/
```

Covers the artifact schema contract, the policy gate, redaction, and the replay engine (three-way result contract, recovery ladder, subflow flattening, parameter substitution, drift signal, extraction honesty) against a scriptable fake driver — no browser or API key required.

## Useful flags

| Flag | Effect |
|---|---|
| `--kind fragment` | record a reusable chunk instead of a capability |
| `--no-reuse` | hide the fragment catalog from the planner |
| `--no-smoke` | skip the proving replay (approval then has no proof behind it) |
| `--no-harden` | skip model-proposed checkpoint and detectors |
| `--no-auto-approve` | always leave the recording as a draft |
| `--non-interactive` | unattended replay: risky steps fail instead of prompting |
| `--escalate-on-failure` | on an unanticipated replay failure, offer a handoff before giving up |
| `--allow-draft` | replay an unapproved artifact (for iterating) |

## Layout

```
cua/schemas.py     artifact schema and result contract (the core data model)
cua/driver.py      surface driver seam (Playwright implementation)
cua/policy.py      allowlist and risk gate (config/policy.yaml)
cua/planner.py     plan proposal and human approval, before anything executes
cua/library.py     the fragment catalog capabilities can reuse
cua/discovery.py   LLM agent loop (the only module that talks to a model at runtime)
cua/recorder.py    transcript distillation into an artifact
cua/harden.py      model-proposed checkpoint/detectors, validated against reality
cua/replay.py      deterministic replay engine
cua/escalation.py  human intervention and control transfer
cua/evidence.py    redacted JSONL evidence logging
cua/store.py       versioned artifact store and dependency graph
```

See REPORT.md for design reasoning, trade-offs, and cut lines.
