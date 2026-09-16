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

During discovery the model can declare itself stuck; during replay a step can exhaust its retries. Either way the run pauses, prints the goal, step, reason, URL and a screenshot, and hands you the open browser window. You do the manual steps, say what you did, press Enter, and the run carries on in the same session. Automation cannot act while you hold it.

In replay this is on by default on an attended run: a step that runs out of options with nothing declared for that case is, by definition, a failure nobody anticipated, and that is when a person is worth asking. `--no-escalate-on-failure` turns it off, and `--non-interactive` never hands over — there is nobody there, so it fails closed.

### When a human is worth interrupting

Only when a human could actually change the outcome. Three cases fail that test, and each one used to waste an operator's time:

- **The caller's own value is missing.** A step targeted through `{{account_id}}`, or a dropdown that does not list the account you asked for. Nobody can make account 99999 exist, and the run already has the answer.
- **Policy refused it.** The gate stopped automation deliberately. An attended operator may still act on their own authority, so this one does hand over by default, but `--no-escalate-policy-blocks` turns it off for unattended runs.
- **The recording is wrong.** An extract pointing at a dropdown or a blank form field is a defect in the artifact, not a state of the page. No amount of clicking moves the step somewhere else.

Every handover is also bounded. `--max-escalations` (default 2) caps how many times one discovery run may ask, a blocker reported again after an intervention stops it immediately, and replay allows one handoff per step — a step that fails again afterwards is a debuggable failure, not a reason to ask you twice.

### What you do by hand is recorded

The handoff listeners capture the same element properties the agent's own actions capture, so your clicks, typing and dropdown choices become real steps with real locator ladders. A capability you had to rescue replays without you next time, and the values you filled in are matched against the supplied arguments like any other step.

Three things are deliberately not recorded: a password you type (authentication belongs to the login fragment), an element the page could not describe (a step with no way to find its element again is not a step), and what you typed is carried as data for parameterisation, never used to name the element.

To see it, ask for something policy blocks — bill pay is the remaining blocked route:

```
python -m cua discover "Open the Bill Pay page and pay 50 dollars to the payee named Acme from account 13344" --url http://localhost:8080/parabank/index.htm --id billpay_demo --input username=john --input account_id=13344 --secret password=demo
```

A handoff on a page the gate refuses is recorded as steps that replay will refuse in turn — which is the answer I want, rather than a capability that quietly does what policy forbids.

### The three things an artifact declares

These are not interchangeable, and conflating them is the mistake this whole schema exists to prevent:

| declaration | means | is |
|---|---|---|
| `ExtractStep.on_empty` | the results area was found and is empty | an answer |
| `SelectStep.on_value_absent` | the option you asked for is not listed | an answer |
| `on_exhausted` | the element could not be found at all | a fault |

And the rule that keeps it honest: **a fault is never an answer.** A recording that cannot find its element, or points at a dropdown or a blank input, fails loudly. It never becomes "no such account" or "no transactions". Three separate bugs in this build were that exact laundering — including one that reported `ACCOUNT_OPEN_FAILED` for an account that had just been opened.

### What the gate judges

- **Where an action is going.** For a navigation that means the DESTINATION, not the page you happen to be on. `blocked_url_patterns` and the domain allowlist are how you put a route out of bounds.
- **What it does there.** Commits are found structurally — a real submit, or a button inside a non-authentication form — not from a control's label, because a label is the thing a vendor renames per tenant. The patterns in `config/policy.yaml` only sharpen an action already known to commit; they never turn a link into a state change.

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

Seven artifacts ship in `artifacts/`. None need an API key, only ParaBank running.

```
docker run -d -p 8080:8080 parasoft/parabank
python -m cua capabilities
```

Each command is on one line so it pastes into bash, zsh and PowerShell unchanged. `john` is a placeholder — use an account that exists on your instance. `password` is sensitive, so I leave it off and let the CLI prompt without echo. Add `--headless` to skip the visible browser, and `--allow-draft` for anything still a draft.

### Read-only

```
python -m cua replay parabank_login@1.0.0 --input username=john
python -m cua replay account_detail@1.0.0 --input username=john --input account_id=13344
python -m cua replay account_activity@1.0.0 --input username=john --input account_id=13344
python -m cua replay find_tx_by_amount@1.0.0 --allow-draft --input username=john --input account_id=13344 --input amount=100
python -m cua replay tx_by_date_range@1.0.0 --allow-draft --input username=john --input account_id=13344 --input start_date=01-01-2026 --input end_date=12-31-2026
```

`parabank_login` is the fragment the others compose, and replaying it alone is the quickest check that the app is up and your credentials work. Dates are **MM-DD-YYYY**.

Year to date, without editing the range each time:

```bash
python -m cua replay tx_by_date_range@1.0.0 --allow-draft --input username=john --input account_id=13344 --input start_date=01-01-$(date +%Y) --input end_date=$(date +%m-%d-%Y)
```
```powershell
$s = "01-01-$(Get-Date -f yyyy)"; $e = Get-Date -f MM-dd-yyyy
python -m cua replay tx_by_date_range@1.0.0 --allow-draft --input username=john --input account_id=13344 --input start_date=$s --input end_date=$e
```

### The three result classes

The same capability, three ways, all exit 0 except the last:

```
# success, with the real transaction table
python -m cua replay find_tx_by_amount@1.0.0 --allow-draft --input username=john --input account_id=13344 --input amount=100

# an answer: the results area was found and is empty
python -m cua replay find_tx_by_amount@1.0.0 --allow-draft --input username=john --input account_id=13344 --input amount=999999
#   -> NO_TRANSACTIONS

# an answer: the account you asked for is not in the dropdown
python -m cua replay find_tx_by_amount@1.0.0 --allow-draft --input username=john --input account_id=99999 --input amount=100
#   -> NO_SUCH_ACCOUNT_ID

# an answer: no such row on the overview
python -m cua replay account_detail@1.0.0 --input username=john --input account_id=99999
#   -> ACCOUNT_NOT_FOUND

# an answer: bad credentials
python -m cua replay parabank_login@1.0.0 --input username=nosuchuser
#   -> INVALID_CREDENTIALS

# the same three classes on the date-range search
python -m cua replay tx_by_date_range@1.0.0 --allow-draft --input username=john --input account_id=13344 --input start_date=01-01-1990 --input end_date=12-31-1990
#   -> NO_TRANSACTIONS
python -m cua replay tx_by_date_range@1.0.0 --allow-draft --input username=john --input account_id=99999 --input start_date=01-01-2026 --input end_date=12-31-2026
#   -> NO_SUCH_ACCOUNT_ID
```

None of these asks you anything. That is the point of the taxonomy above.

### These change the account

`open_savings_account` opens a real savings account funded from `account_id` and asks you to confirm first. Every successful run leaves a new account behind.

```
python -m cua replay open_savings_account@1.0.0 --allow-draft --input username=john --input account_id=13344
```

The same run unattended, to see it fail closed rather than submit without a human:

```
python -m cua replay open_savings_account@1.0.0 --allow-draft --input username=john --input account_id=13344 --secret password=demo --non-interactive
```

`update_contact` rewrites profile fields and confirms first. It is reversible — set the values back.

```
python -m cua replay update_contact@1.0.0 --allow-draft --input username=john --input phone_number=5559876543 --input city=Boston
```

### Evidence

```
python -m cua graph account_detail@1.0.0
```

Every run writes a redacted JSONL trace to `evidence/replay-<timestamp>/events.jsonl`, plus a screenshot on failure.

## Human handoff, end to end

The clearest worked example is asking for an account the surface does not have. It exercises the whole loop — the run reports what it found, offers you the session, records what you do, and verification judges the result.

`discover` needs `ANTHROPIC_API_KEY`, and it asks for one *after* the browser is already open, which is an awkward moment to find out.

```
python -m cua discover "Log in with the given username and password, open the Find Transactions page, select the given account in the Account dropdown, enter the amount in the Amount field, click the FIND TRANSACTIONS button in the amount section, and read the whole Transaction Results table into an output named transactions" --url http://localhost:8080/parabank/index.htm --id handoff_demo --input username=john --input account_id=99999 --input amount=100 --secret password=demo --max-steps 15
```

What happens, in order:

1. **A plan to approve.** `parabank_login@1.0.0` appears under `REUSES` — the login is replayed deterministically, not rediscovered. Answer `a`.
2. **The agent calls `declare_not_found`**, because 99999 is not among the dropdown's options. This is the distinction that matters: it is not stuck on a page it cannot operate, it has an answer — there is no such account. The run records that as the outcome.
3. **Then it offers you the session anyway.** Discovery is supervised authoring, so you may want to carry on from a state only you can reach. The request says plainly that this is a business outcome, not a blocked page.
4. **What you do is recorded.** Pick a real account, run the search, press Enter. Your clicks and typing become steps with proper locator ladders, and the values you entered are matched against the supplied arguments.
5. **Verification judges the recording.** If you searched a different account than you asked for, `account_id` comes back frozen and the artifact stays a draft:

```
RECORDING DOES NOT MATCH THE APPROVED PLAN:
  - parameter 'account_id' was planned as an input but no recorded step references it — it is frozen as a constant

Left as a draft: it did not match the approved plan.
```

That refusal is the system working. Approving it would publish a capability whose `account_id` argument is silently ignored. `--force` exists for gaps a reviewer has read and accepted; a frozen parameter is not one of those.

If the run cannot reach the goal at all, the path it did explore is still saved as a draft, marked with `provenance.partial_reason`, so you can replay how far it gets and decide whether to finish or discard it. A partial is never smoke-tested and never auto-approved — there is no goal reached, so there is nothing to prove.

## Tests

```
python -m pytest tests/
```

190 tests across 12 files, covering the artifact schema, the policy gate, redaction, and the replay engine — the three-way result contract, recovery ladder, subflow flattening and cycle refusal, parameter substitution, ladder identity rules, drift, extraction honesty, risk classification, escalation limits and what must never escalate, empty versus absent results, stale refs, reading a dropdown as data, and file encoding. They run against a fake driver, so no browser and no API key.

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
| `--no-escalate-on-failure` | do not hand over when a replay step exhausts its retries (on by default, attended runs only) |
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

**Extraction quality is the weak point.** Getting an extract aimed at the right element was the hardest part of this build, and not every shipped artifact is there yet. The system now refuses to read a dropdown or a blank input as data, and refs cannot outlive the observation that minted them, so the common ways of recording the wrong element fail loudly instead of returning something plausible. That is not the same as being correct — check `output_evidence.source_text` before trusting any output, including from a capability you record yourself. The `Read from the page:` block at the end of a discovery run is there for exactly that.

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
