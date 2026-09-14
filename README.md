# Computer Use Automation System

A system that uses an LLM **once** to discover how to accomplish a goal in a legacy banking UI, records the successful run as a typed, versioned capability artifact, **proves that artifact replays**, and then replays it deterministically with no model in the loop. Saved capabilities are exposed as a catalog of callable tools, so an agent can invoke them by name with typed arguments. Includes policy guardrails, redacted evidence logging, and human escalation with control transfer on the live session.

Target application: ParaBank, a demo banking site built by Parasoft for automation practice. Run it locally with Docker (preferred) or use the public instance.

## Setup

1. Python 3.11 or newer.
2. Install dependencies and the browser:

```
pip install -r requirements.txt
playwright install chromium
```

3. Start the target app locally:

```
docker run -d -p 8080:8080 parasoft/parabank
```

The app is then at http://localhost:8080/parabank/index.htm. To run without local services, substitute the public instance URL https://parabank.parasoft.com/parabank/index.htm (already in `config/policy.yaml`). Register a throwaway user on the site first; never use real credentials.

4. Provide a model API key. Any of these work:

```
python -m cua discover ... --api-key sk-ant-...     # explicit
$env:ANTHROPIC_API_KEY = "sk-ant-..."               # PowerShell
export ANTHROPIC_API_KEY=sk-ant-...                 # bash
```

If none is set you are prompted without echo. A key passed as a flag lands in shell history, so the prompt is the better path for a demo.

**Only `discover`, `chat` and `ui` need a key.** `replay`, `capabilities`, `graph`, `approve` and the test suite all run without one — replay has no model in it at all.

## Demo path

**Step 1. Record login once, as a reusable fragment.** Small proven chunks make longer flows reliable: a fourteen-step discovery gives the model fourteen chances to choose badly.

```
python -m cua discover "Log in with the given username and password and reach the Accounts Overview page" \
  --url http://localhost:8080/parabank/index.htm \
  --id parabank_login --kind fragment \
  --input username=john --secret password=demo
```

You are shown a plan to approve before anything touches the browser. The run then records a draft, the model supplies the checkpoint and error handling the happy path could not observe (proposals that fail validation are rejected, and the rejection is recorded on the artifact), and the artifact is **replayed once from a clean browser session** before it is allowed to be approved.

**Step 2. Record a capability that reuses it.** The planner is shown the catalog of proven fragments; if it lists one under `REUSES`, that fragment is replayed deterministically and the model only explores what is genuinely new.

```
python -m cua discover "Log in, open Accounts Overview, click into account {{account_id}} to open its detail page, and read the account type into an output named account_type and the balance into an output named balance" \
  --url http://localhost:8080/parabank/index.htm \
  --id account_detail \
  --input username=john --input account_id=13344 --secret password=demo
```

**Step 3. Deterministic replay with new parameters, no LLM involved:**

```
python -m cua replay account_detail@1.0.0 \
  --input username=john --input account_id=13344 --secret password=demo
```

Prints a structured result: status, typed outputs, the evidence each output was read from, and per-step reports. Exit code 0 for success or a business outcome, 2 for a hard failure.

**Step 4. Replay that hits an exceptional state** — a nonexistent account returns the declared business outcome rather than crashing, and never another account's data:

```
python -m cua replay account_detail@1.0.0 \
  --input username=john --input account_id=99999 --secret password=demo
```

**Step 5. Inspect the catalog and the composition:**

```
python -m cua graph account_detail@1.0.0      # capability -> pinned fragments
python -m cua capabilities                    # signatures and approval status
python -m cua approve <ref>                   # deliberate human approval
```

## Talking to it

Saved artifacts are exposed as a catalog of callable tools — the typed contract in the artifact *is* the function signature. The model picks a capability and fills in its arguments, then stops: it never drives the browser, never chooses a locator, and never decides what a page means. Execution is the same deterministic replay engine used everywhere else.

```
python -m cua chat --start-app        # terminal
python -m cua ui   --start-app        # browser chat window
```

```
you> what's the balance on 13344?
  [invoking account_detail {'account_id': '13344'}]
That account is a SAVINGS account with a balance of -$980.90.

you> and 99999?
  [invoking account_detail {'account_id': '99999'}]
There's no account 99999 on this customer's profile.
```

Three properties are what separate this from a chatbot that sounds confident:

- **Credentials never reach the model.** Sensitive inputs are stripped from the tool schema entirely and injected by the runtime at call time, so the model has no parameter to put a password in.
- **It cannot answer from memory.** Every factual claim has to come from a tool result, and each result carries the source text it was read from.
- **A business outcome is an answer, not an error.** "No such account" comes back as a result and is relayed plainly.

The browser UI adds a **Discover** toggle. With it off, only existing capabilities are callable — the build tools are not offered to the model at all, so it cannot learn anything even if it decides it wants to. With it on, what you type is treated as a goal to learn, and the discovery run happens inside the chat: plan approval, risky confirmations and operator questions appear as questions you answer by typing back.

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

A control that commits a form is detected structurally — a real submit, or a button inside a form, which is how legacy apps commit via JavaScript — and recorded as `risky`. Replaying such a step shows what is about to be submitted and asks first:

```
==============================================================
CONFIRM STATE-CHANGING ACTION
  step s06_click 'Apply Now button': risky action
  About to submit:
    Loan Amount field = 1500
    Down Payment field = 250
==============================================================
Proceed? [y/N]:
```

Unattended runs (`--non-interactive`) fail closed on these steps rather than proceeding. Capabilities containing one are never auto-approved, because proving them would mean performing them again. A form containing a password field is treated as authentication rather than a state change — otherwise every capability that logs in would demand a human.

## Tests

```
python -m pytest tests/
```

Covers the artifact schema contract, the policy gate, redaction, and the replay engine — the three-way result contract, recovery ladder, subflow flattening and cycle refusal, parameter substitution, locator-ladder identity rules, drift signal, and extraction honesty — against a scriptable fake driver. No browser or API key required.

## Useful flags

| Flag | Effect |
|---|---|
| `--api-key` | model key; otherwise the environment, otherwise prompted |
| `--kind fragment` | record a reusable chunk instead of a capability |
| `--no-reuse` | hide the fragment catalog from the planner |
| `--no-smoke` | skip the proving replay (approval then has no proof behind it) |
| `--no-harden` | skip model-proposed checkpoint and detectors |
| `--no-auto-approve` | always leave the recording as a draft |
| `--non-interactive` | unattended replay: risky steps fail instead of prompting |
| `--escalate-on-failure` | on an unanticipated replay failure, offer a handoff before giving up |
| `--allow-draft` | replay an unapproved artifact (for iterating) |
| `--start-app` | start ParaBank via docker if it is not already up (`chat`, `ui`) |

## Layout

```
cua/schemas.py     artifact schema and result contract (the core data model)
cua/driver.py      surface driver seam (Playwright implementation); owns waiting
                   and session ownership
cua/policy.py      allowlist and risk gate (config/policy.yaml)
cua/planner.py     plan proposal and human approval, before anything executes
cua/library.py     the fragment catalog capabilities can reuse
cua/discovery.py   LLM agent loop — the only module that drives a surface with a model
cua/recorder.py    transcript distillation into an artifact
cua/harden.py      model-proposed checkpoint/detectors, validated against reality
cua/replay.py      deterministic replay engine
cua/escalation.py  human intervention and control transfer
cua/prompt.py      where a question to the operator goes (terminal, or a UI)
cua/chat.py        the catalog as callable tools for an agent
cua/ui.py          browser chat window over discover / approve / replay
cua/evidence.py    redacted JSONL evidence logging
cua/redaction.py   secret masking, applied at write time
cua/store.py       versioned artifact store and dependency graph
cua/textio.py      the only file reader/writer: UTF-8 + LF on every OS
cua/cli.py         command entry points
```

## Artifacts across Windows and macOS

Artifacts are committed files replayed on machines other than the one that
recorded them, so their encoding is part of the contract. Everything that
touches a file goes through `cua/textio.py`, which **writes UTF-8 with LF
endings on every platform** and reads permissively (UTF-8, then cp1252, BOM
stripped).

This is not cosmetic. `recorder.py` writes review notes containing an em dash;
Python's `open()` defaults to the *locale* encoding, so on Windows that became
the single byte `0x97`, which is not valid UTF-8 — and every macOS replay of
that artifact died in the decoder before the engine ran. `.gitattributes` pins
the same files to LF in the repository so a re-save on the other OS diffs only
where the flow changed.

Artifacts written before this are still loadable; re-saving one normalizes it.
To repair a directory in place:

```bash
python -c "from cua.textio import normalize_dir; print(normalize_dir('artifacts'))"
```

`tests/test_portability.py` asserts every committed artifact is UTF-8 + LF, so
a regression fails in CI rather than on a reviewer's laptop.

See REPORT.md for design reasoning, trade-offs, and cut lines.
