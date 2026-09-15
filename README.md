# Computer Use Automation System

I use an LLM **once** to work out how to do something in a legacy banking UI, record that run as a typed, versioned artifact, prove the artifact replays, and from then on replay it with no model in the loop. Saved capabilities become a catalog of callable tools an agent can invoke by name with typed arguments.

It includes a policy gate, redacted evidence logs, and human handoff on the live browser session.

Target app: ParaBank, a demo banking site Parasoft publishes for automation practice.

## Setup

1. Python 3.11 or newer.

2. Install dependencies and the browser:

```
pip install -r requirements.txt
playwright install chromium
```

3. Start ParaBank:

```
docker run -d -p 8080:8080 parasoft/parabank
```

It comes up at http://localhost:8080/parabank/index.htm. Register a throwaway user on the site first and use those credentials. Never use real ones.

4. Set your API key. The variable is **`ANTHROPIC_API_KEY`** — that exact name is what the Anthropic SDK reads, and nothing else is checked.

`discover`, `chat` and `ui` need it. Set it in the same shell you run them from:

```powershell
$env:ANTHROPIC_API_KEY = "sk-ant-..."     # PowerShell
```
```bash
export ANTHROPIC_API_KEY=sk-ant-...        # bash / zsh
```

That only covers the current shell. A new tab or a new VS Code window will not have it, and I deliberately do not read a `.env` file or save the key anywhere. Worth checking before a demo rather than during one:

```powershell
if ($env:ANTHROPIC_API_KEY) { "set" } else { "NOT SET" }
```

On Windows, `setx ANTHROPIC_API_KEY "sk-ant-..."` makes it stick for terminals opened afterwards — not the one you typed it in.

If it is missing, the command tells you and asks:

```
No ANTHROPIC_API_KEY in the environment.
Anthropic API key (not echoed, not saved):
```

Answering the prompt is better for a demo than `--api-key`, which puts the key in your shell history.

**`replay`, `capabilities`, `graph`, `approve` and the tests need no key at all.** Replay has no model in it, which is the point.

## Demo path

**1. Record login once, as a reusable fragment.** Small proven chunks make longer flows more reliable — a fourteen-step discovery gives the model fourteen chances to go wrong.

```
python -m cua discover "Log in with the given username and password and reach the Accounts Overview page" --url http://localhost:8080/parabank/index.htm --id parabank_login --kind fragment --input username=john --secret password=demo
```

You approve a plan before anything touches the browser. The run records a draft, the model adds the checkpoint and error handling the happy path could not show, and the artifact is replayed once from a clean browser session before it can be approved.

**2. Record a capability that reuses it.** The planner sees the catalog of proven fragments. If it lists one under `REUSES`, that fragment is replayed deterministically and the model only explores what is new.

```
python -m cua discover "Log in, open Accounts Overview, click into account {{account_id}} to open its detail page, and read the account type into an output named account_type and the balance into an output named balance" --url http://localhost:8080/parabank/index.htm --id account_detail --input username=john --input account_id=13344 --secret password=demo
```

**3. Replay it with new arguments, no model involved:**

```
python -m cua replay account_detail@1.0.0 --input username=john --input account_id=13344
```

You get status, typed outputs, the text each output was read from, and per-step reports. Exit code 0 for success or a business outcome, 2 for a hard failure.

**4. Replay against something that is not there.** A missing account returns the declared business outcome rather than crashing, and never another account's data:

```
python -m cua replay account_detail@1.0.0 --input username=john --input account_id=99999
```

**5. Look at the catalog:**

```
python -m cua graph account_detail@1.0.0      # capability -> pinned fragments
python -m cua capabilities                    # signatures and approval status
python -m cua approve <ref>                   # deliberate human approval
```

## Talking to it

Saved artifacts become callable tools — the typed contract in the artifact *is* the function signature. The model picks a capability and fills in its arguments, then stops. It never drives the browser, never picks a locator, never decides what a page means.

Both of these drive a model, so run them from a shell with `ANTHROPIC_API_KEY` set.

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

Three things separate this from a chatbot that sounds confident:

- **Credentials never reach the model.** Sensitive inputs are stripped from the tool schema and injected by the runtime, so the model has no parameter to put a password in.
- **It cannot answer from memory.** Every factual claim has to come from a tool result, and each result carries the text it was read from.
- **A business outcome is an answer, not an error.** "No such account" comes back as a result.

The browser UI adds a **Discover** toggle. Off, only existing capabilities are callable — the build tools are not offered to the model at all. On, what you type is treated as a goal to learn, and plan approval and confirmations appear as questions you answer by typing back.

## Escalation

During discovery the model can declare itself stuck; during replay a step can declare escalate. Either way the run pauses, prints the goal, step, reason, URL and a screenshot, and hands you the open browser window. You do the manual steps, say what you did, press Enter, and the run carries on in the same session. Your clicks go into the run's evidence. Automation cannot act while you hold the session.

Two things keep a handoff from turning into a loop:

- **`--max-escalations`** (default 2) caps how many times one run may ask. If the agent reports the same blocker after a handover, it stops immediately — the same problem coming back means the handover did not change anything.
- **`--no-escalate-policy-blocks`** turns the handoff off for policy refusals, for unattended runs.

The gate stops **automation**. An attended operator can still act on their own authority, so a policy block does hand over by default.

One property is worth understanding before you write a policy: **only the agent's own actions become replayable steps.** A handoff lets a human finish the *task*, but it cannot produce a *recording*. So a flow the agent is not allowed to perform can never be recorded, however many times someone completes it by hand — the draft comes back with its parameters frozen every time. Blocking a route decides what the catalog can ever contain, not just what happens today.

And the approval refusal protects the catalog, not the world. The artifact cannot be approved, but whatever the operator did in the live app really happened.

### What the gate judges

- **Where an action is going.** For a navigation that means the destination, not the page you happen to be on. `blocked_url_patterns` and the domain allowlist are how you put a route out of bounds.
- **What it does there.** Commits are found structurally — a real submit, or a button inside a non-authentication form — not from a control's label, because a label is the thing a vendor renames per tenant. The patterns in `config/policy.yaml` only sharpen an action already known to commit. They never turn a link into a state change.

## Confirmation before a commit

A control that commits a form is recorded as `risky`, and replaying it shows what is about to be submitted and asks first:

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

Unattended runs (`--non-interactive`) fail closed here rather than proceeding. Capabilities containing a commit are never auto-approved, because proving them would mean doing them again. A form with a password field counts as authentication, not a state change — otherwise every capability that logs in would need a human.

## Replaying the shipped artifacts

Nine artifacts ship in `artifacts/`. None of these need an API key, only ParaBank running.

```
docker run -d -p 8080:8080 parasoft/parabank
python -m cua capabilities
```

Each command is on one line so it pastes into bash, zsh and PowerShell unchanged — the line-continuation character differs between them, which is the usual reason a copied command fails on the other OS.

Every artifact navigates to `http://localhost:8080/parabank/index.htm`, so these replay against the local instance. `john` is a placeholder; use an account that exists on yours. `password` is sensitive, so I leave it off and let the CLI prompt without echo. Add `--headless` to skip the visible browser.

### Read-only

```
python -m cua replay parabank_login@1.0.0 --input username=john
python -m cua replay account_detail@1.0.0 --input username=john --input account_id=13344
python -m cua replay account_activity@1.0.0 --input username=john --input account_id=13344
python -m cua replay get_recent_transactions@1.0.0 --input username=john --input account_id=13344
python -m cua replay demo_find_tx@1.0.0 --input username=john --input account_id=13344 --input amount=100
python -m cua replay demo_tx_range@1.0.0 --input username=john --input account_id=13344 --input start_date=01-01-2026 --input end_date=12-31-2026
```

`parabank_login` is the fragment the others compose, and replaying it alone is the quickest check that the app is up and your credentials work. Dates are **MM-DD-YYYY**.

Year to date, without editing the range each time:

```bash
python -m cua replay demo_tx_range@1.0.0 --input username=john --input account_id=13344 --input start_date=01-01-$(date +%Y) --input end_date=$(date +%m-%d-%Y)
```
```powershell
$s = "01-01-$(Get-Date -f yyyy)"; $e = Get-Date -f MM-dd-yyyy
python -m cua replay demo_tx_range@1.0.0 --input username=john --input account_id=13344 --input start_date=$s --input end_date=$e
```

### Business outcomes

Something that is not there is an answer, not a crash. These exit 0 with an outcome code:

```
python -m cua replay account_detail@1.0.0 --input username=john --input account_id=99999
#   -> ACCOUNT_NOT_FOUND

python -m cua replay parabank_login@1.0.0 --input username=nosuchuser
#   -> INVALID_CREDENTIALS

python -m cua replay demo_tx_range@1.0.0 --input username=john --input account_id=13344 --input start_date=01-01-1990 --input end_date=12-31-1990
#   -> NO_TRANSACTIONS_FOUND
```

### These change the account

`open_savings_account` opens a real savings account funded from `account_id`, and asks you to confirm first. Every successful run leaves a new account behind.

```
python -m cua replay open_savings_account@1.0.0 --input username=john --input account_id=13344
```

The same run unattended, to see it fail closed rather than submit without a human:

```
python -m cua replay open_savings_account@1.0.0 --input username=john --input account_id=13344 --secret password=demo --non-interactive
```

`transfer_demo@1.0.0` is still a draft, so it needs `--allow-draft`. It moves real money and confirms first.

```
python -m cua replay transfer_demo@1.0.0 --allow-draft --input username=john --input account_id=13344 --input from_account_id=13344 --input to_account_id=13566 --input transfer_amount=100
```

### Evidence

```
python -m cua graph account_detail@1.0.0
```

Every run writes a redacted JSONL trace to `evidence/replay-<timestamp>/events.jsonl`, plus a screenshot on failure.

## What I actually got

I ran all of these against a local ParaBank on 2026-09-15. Every one returned `"status": "success"` and exit 0, which is why the `output_evidence` block matters more than the status line. Each output carries the text it was read from, and that is what tells you whether the capability answered the question.

| capability | output | text it actually read | |
|---|---|---|---|
| `parabank_login` | — | — | ok, reaches Accounts Overview |
| `account_detail` | `account_type`, `balance` | the account's own cells | ok |
| `account_activity` | `transactions` | the full `#transactionTable` | ok, 25 rows |
| `get_recent_transactions` | `recent_transactions` | `"Account Activity"` | wrong — the page heading |
| `demo_find_tx` | `match_count` | `"Find Transactions"` | wrong — the page heading |
| `demo_tx_range` | `match_count` | `"12456\n\t12567\n\t..."` | wrong — the account dropdown |
| `open_savings_account` | `new_account_id` | `"CHECKING\n  SAVINGS"` | wrong — the type dropdown |
| `demo_transfer` | `transfer_confirmation_message` | `"Transfer Funds"` | wrong — the page heading |

**Five of the seven extracting capabilities read the wrong element and still reported success.** That is not a replay bug. The ladders resolve, the steps run, the checkpoint passes. It is a recording defect: the discovery model aimed the extract step at a heading or a dropdown instead of the result, and nothing downstream re-checks what an extract points at.

```
demo_tx_range        -> css  #accountId          (the dropdown, not a count)
demo_find_tx         -> text "Find Transactions" (the heading, not a count)
open_savings_account -> css  #type               (the dropdown, not the new id)
```

Two things follow, and both are in REPORT.md's weaknesses list. `review_notes` flags every single-rung extract, and all five carry that flag. And a success checkpoint proves the *page arrived*, not that the *output is the answer*. Reading `output_evidence.source_text` is currently the only check that catches this, which is why it prints on every run.

I left these as recorded rather than hand-editing them. An artifact is a record of what discovery produced, and a corrected one would hide how often the model gets extraction wrong on the first pass.

## Human handoff, end to end

The transfer capability is my worked example, because it needs a handoff for a reason nothing in the system can fix: the destination account does not exist on this instance.

`discover` needs `ANTHROPIC_API_KEY`, and it asks for one *after* the browser is already open, which is an awkward moment to find out.

```
python -m cua discover "Log in with the given username and password, open the Transfer Funds page, transfer the given amount from the given source account to the given destination account, and confirm the transfer completed" --url http://localhost:8080/parabank/index.htm --id demo_transfer --input username=john --input source_account=13344 --input destination_account=13455 --input amount=1 --secret password=demo --max-steps 12
```

What happens, in order:

1. **A plan to approve.** Five steps, with `parabank_login@1.0.0` under `REUSES` — the login is replayed, not rediscovered. Answer `a`.
2. **A handoff request**, because `13455` is not in the To Account dropdown. The agent lists the accounts it can see and gives you the browser. This is the honest case for a handoff: it is not stuck on a policy rule or a flaky locator, it is stuck on a value that does not exist.
3. **You do it and report back.** Type what you did, press Enter. Control returns and the run continues from where you left it.
4. **Hardening** adds the outcomes and detectors the happy path could not show — `DESTINATION_ACCOUNT_NOT_FOUND`, `INSUFFICIENT_FUNDS`, a session-timeout recovery.
5. **Verification refuses the recording:**

```
RECORDING DOES NOT MATCH THE APPROVED PLAN:
  - parameter 'destination_account' was planned as an input but no recorded step references it — it is frozen as a constant
  - parameter 'amount' was planned as an input but no recorded step references it — it is frozen as a constant

SUCCESS in 8 steps (1 escalation(s)).
Left as a draft: it did not match the approved plan.
```

The run succeeded and the money moved, but **you** did the parameterised parts, not the agent, and a human's handoff actions reach evidence without becoming replayable steps (REPORT.md §7). So the artifact declares two inputs no step uses. Approving it would publish a capability whose `amount` and `destination_account` are quietly ignored.

I would not force this one:

```
python -m cua approve demo_transfer@1.0.0            # refuses, listing both frozen parameters
python -m cua approve demo_transfer@1.0.0 --force    # approves a capability whose inputs do nothing
```

`--force` is for gaps a reviewer has read and accepted. Frozen parameters are not that — the replay that follows types a hardcoded amount, never touches the destination, and reads the page heading as its confirmation. That is the last row of the table above.

The fix is a better recording. Use a destination that exists in the dropdown, and name both dropdowns and the submit button in the goal so the model records a step for each:

```
python -m cua discover "Log in with the given username and password, open the Transfer Funds page, select the source account in the From Account dropdown and the destination account in the To Account dropdown, enter the amount in the amount field, click the Transfer button, and read the transfer confirmation message" --url http://localhost:8080/parabank/index.htm --id demo_transfer --input username=john --input source_account=13344 --input destination_account=12345 --input amount=1 --secret password=demo --max-steps 15
```

Then approve without `--force`. If verification still reports a frozen parameter, the recording is still incomplete.

## Tests

```
python -m pytest tests/
```

122 tests covering the artifact schema, the policy gate, redaction, and the replay engine — the three-way result contract, recovery ladder, subflow flattening and cycle refusal, parameter substitution, ladder identity rules, drift, extraction honesty, risk classification, escalation limits, and file encoding. They run against a fake driver, so no browser and no API key.

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
| `--escalate-on-failure` | on an unexpected replay failure, offer a handoff before giving up |
| `--max-escalations` | how many handoffs a stuck discovery run may ask for (default 2) |
| `--no-escalate-policy-blocks` | do not hand over when policy is what stopped the agent |
| `--allow-draft` | replay an unapproved artifact |
| `--start-app` | start ParaBank via docker if it is not up (`chat`, `ui`) |

## Layout

```
cua/schemas.py     artifact schema and result contract (the core data model)
cua/driver.py      surface driver seam (Playwright); owns waiting and session ownership
cua/policy.py      allowlist and risk gate (config/policy.yaml)
cua/planner.py     plan proposal and human approval, before anything executes
cua/library.py     the fragment catalog capabilities can reuse
cua/discovery.py   LLM agent loop — the only module that drives a surface with a model
cua/recorder.py    transcript distillation into an artifact
cua/harden.py      model-proposed checkpoint/detectors, validated against reality
cua/replay.py      deterministic replay engine
cua/escalation.py  human handoff and control transfer
cua/prompt.py      where a question to the operator goes (terminal, or a UI)
cua/chat.py        the catalog as callable tools for an agent
cua/ui.py          browser chat window over discover / approve / replay
cua/evidence.py    redacted JSONL evidence logging
cua/redaction.py   secret masking, applied at write time
cua/store.py       versioned artifact store and dependency graph
cua/textio.py      the only file reader/writer: UTF-8 + LF on every OS
cua/cli.py         command entry points
```

## Disclaimer

I ran these commands on macOS and on Windows. The test suite passes on both. But a full end-to-end replay depends on things outside this repo, and another machine can still behave differently.

**Account state is the most common cause of a confusing result.** ParaBank accounts are per-user and mutable. `13344` and `13566` are the numbers from the machine I recorded on; yours will differ, and `open_savings_account` changes them every successful run. `ACCOUNT_NOT_FOUND` or `NO_TRANSACTIONS_FOUND` on a fresh instance usually means the data is not there, not that replay is broken. Run `account_detail` against an account you can see in Accounts Overview first.

**Extraction quality varies, as the table above shows.** Five of the seven artifacts that extract anything read the wrong element. Check `output_evidence.source_text` before trusting any output, including from a capability you record yourself.

**File encoding was a real cross-platform bug and is now covered by tests.** `recorder.py` writes review notes containing an em dash. Python's `open()` defaults to the locale encoding, so on Windows that became the single byte `0x97`, which is not valid UTF-8, and every macOS replay of that artifact died in the decoder before the engine ran. Everything that touches a file now goes through `cua/textio.py`, which writes UTF-8 with LF endings on every platform and reads permissively (UTF-8, then cp1252, BOM stripped). `.gitattributes` pins the same files to LF in the repo. Artifacts written before this still load; re-saving one normalizes it, or repair a directory with:

```bash
python -c "from cua.textio import normalize_dir; print(normalize_dir('artifacts'))"
```

`tests/test_portability.py` reads every committed artifact with the local decoder, so run the suite first on a new machine — it fails fast and needs no browser.

**Chromium is per-environment.** Playwright ships its own browser, so run `playwright install chromium` on each machine and in each virtualenv rather than assuming the one already there. A different Chromium version can shift a text extraction.

**Timing.** The recovery ladder absorbs ordinary slowness, but a cold `docker run` or a loaded machine can outlast a wait and show up as a locator failure rather than a timeout.

**Docker networking.** `localhost:8080` assumes the container publishes straight to the host. Under a VM-backed Docker Desktop, a remote daemon, or WSL2 with its own network namespace, the app may be somewhere else.

**The public instance is shared.** If you point anything at `parabank.parasoft.com`, other people are using it and its data resets on its own schedule.

See REPORT.md for the design reasoning, trade-offs, and what I cut.
