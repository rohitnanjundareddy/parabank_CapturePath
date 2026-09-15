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

4. Provide a model API key. The variable is named **`ANTHROPIC_API_KEY`** — that exact spelling is what the Anthropic SDK reads, and nothing else is checked.

**`discover`, `chat` and `ui` must be run from a terminal where it is set.** Set it in the same shell you are about to run them in:

```powershell
# PowerShell — this session only
$env:ANTHROPIC_API_KEY = "sk-ant-..."
python -m cua discover ...
```
```bash
# bash / zsh — this session only
export ANTHROPIC_API_KEY=sk-ant-...
python -m cua discover ...
```

It is scoped to that shell. A new terminal tab, a new VS Code window, or a shell opened by another tool does **not** inherit it, and this project deliberately does not read a `.env` file or save the key anywhere. Check before a demo rather than halfway through one:

```powershell
if ($env:ANTHROPIC_API_KEY) { "key is set" } else { "NOT SET" }   # PowerShell
```
```bash
[ -n "$ANTHROPIC_API_KEY" ] && echo "key is set" || echo "NOT SET"   # bash
```

To persist it across new terminals on Windows, `setx ANTHROPIC_API_KEY "sk-ant-..."` — note that `setx` affects only terminals opened *afterwards*, not the one you typed it in.

If it is missing the command says so and prompts, without echo and without saving:

```
No ANTHROPIC_API_KEY in the environment.
Anthropic API key (not echoed, not saved):
```

Answering the prompt is the better path for a demo: `--api-key sk-ant-...` also works but lands the key in shell history.

**Only `discover`, `chat` and `ui` need a key.** `replay`, `capabilities`, `graph`, `approve` and the test suite all run without one — replay has no model in it at all, which is the point of the artifact.

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

Both drive a model, so run them from a shell with `ANTHROPIC_API_KEY` set (see Setup step 4); without it they stop and prompt for the key.

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

### When a human is worth interrupting

The gate stops **automation**. An attended operator may still act on their own authority, so a policy block does hand the browser over — that is what the escalation surface is for. What it must not do is ask forever:

- **Every handover is bounded.** `--max-escalations` (default 2) caps how many times one run may ask, and a blocker reported again after an intervention stops it immediately — the same problem coming back means the handover did not change what the agent can do. Without that cap a stuck agent asked the operator to describe what they did, over and over, while nothing moved.
- **`--no-escalate-policy-blocks`** turns the handover off for policy refusals specifically, for unattended runs or when you would rather the run fail loudly than have someone finish it by hand.
- **The run says why it stopped.** A failed discovery prints the agent's own reason and, where one applies, the rule that refused it.

To see it, ask for something policy blocks. Bill pay is the remaining blocked route:

```
python -m cua discover "Open the Bill Pay page and pay 50 dollars to the payee named Acme from account 13344" --url http://localhost:8080/parabank/index.htm --id billpay_demo --input username=john --input account_id=13344 --secret password=demo
```

Note what happens afterwards: the actions you performed by hand are captured as evidence but do not become replayable steps, so verification reports the planned parameters as unused and **refuses to approve the recording**. That is intentional — see REPORT.md §7.

This is the property to understand before designing a policy, because it decides what the system can ever learn: **only the agent's own actions become replayable steps.** A handover lets a human finish the *task*; it cannot produce a *recording*. So a flow the agent is forbidden to perform is a flow that can never be recorded, no matter how many times an operator completes it by hand — the draft comes back with its parameters frozen and its outputs missing, every time. Blocking a route is therefore a decision about what the catalog may ever contain, not just about what happens today.

Be clear about what the approval refusal does and does not protect, too: the artifact cannot be approved, but anything the operator did in the live app really happened. The approval gate protects the catalog, not the world.

### What the gate judges

Two rules decide every action, and they are not interchangeable:

- **Where it is going.** For a navigation that is the DESTINATION, never the page the agent happens to be standing on. `blocked_url_patterns` and the domain allowlist are the right way to put a route out of bounds, and they are what refuses a forbidden address.
- **What it does there.** Commits are detected structurally — a real submit, or a button inside a non-authentication form — not from a control's label, because a label is the one thing a vendor renames per tenant. The description patterns in `config/policy.yaml` only refine an action already known to commit, or one whose commit-ness was never determined; they are never allowed to turn a link into a state change.

A blocked route therefore refuses the navigation itself. It does not leave the agent stranded on a forbidden page discovering the rule one action at a time.

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

## Replaying every shipped artifact

Eight artifacts ship in `artifacts/`. Every one is replayed with no model in the loop, so none of these need an API key — only ParaBank running locally.

```
docker run -d -p 8080:8080 parasoft/parabank      # or: --start-app on chat/ui
python -m cua capabilities                        # signatures and approval status
```

Commands below are on one line each, so they paste unchanged into bash, zsh and PowerShell — the line-continuation character differs between them (`\` vs `` ` ``) and is the usual reason a copied command fails on the other OS.

Every shipped artifact navigates to `http://localhost:8080/parabank/index.htm`, so the **local** instance is what these replay against; the public `parabank.parasoft.com` host is allowed by policy but is not where these recordings point. Register a throwaway user on the app first and use those credentials — `john` / `demo` below is a placeholder, not an account that exists on your instance.

`password` is sensitive on every artifact, so it is omitted here: the CLI prompts for it without echo and registers it with the redactor before anything is written. Pass `--secret password=...` only for unattended runs, and accept that it lands in shell history.

Add `--headless` to any of these to run without a visible browser.

### Read-only — safe to run repeatedly

**`parabank_login@1.0.0`** — the fragment the others compose. Replayable on its own, which is the quickest check that the app is up and your credentials work.

```
python -m cua replay parabank_login@1.0.0 --input username=john
```

**`account_detail@1.0.0`** — account type and balance for one account.

```
python -m cua replay account_detail@1.0.0 --input username=john --input account_id=13344
```

**`account_activity@1.0.0`** — the full transaction table for one account, as text.

```
python -m cua replay account_activity@1.0.0 --input username=john --input account_id=13344
```

**`get_recent_transactions@1.0.0`** — the first five transactions listed.

```
python -m cua replay get_recent_transactions@1.0.0 --input username=john --input account_id=13344
```

**`demo_find_tx@1.0.0`** — Find Transactions by amount; returns `match_count`.

```
python -m cua replay demo_find_tx@1.0.0 --input username=john --input account_id=13344 --input amount=100
```

**`demo_tx_range@1.0.0`** — Find Transactions by date range; returns `match_count`. Dates are **MM-DD-YYYY**.

```
python -m cua replay demo_tx_range@1.0.0 --input username=john --input account_id=13344 --input start_date=01-01-2026 --input end_date=12-31-2026
```

Year-to-date, without hand-editing the range each run:

```
# bash / zsh
python -m cua replay demo_tx_range@1.0.0 --input username=john --input account_id=13344 --input start_date=01-01-$(date +%Y) --input end_date=$(date +%m-%d-%Y)
```

```
# PowerShell
$s = "01-01-$(Get-Date -f yyyy)"; $e = Get-Date -f MM-dd-yyyy
python -m cua replay demo_tx_range@1.0.0 --input username=john --input account_id=13344 --input start_date=$s --input end_date=$e
```

### Business outcomes — the declared "not there" answers

A nonexistent account is an answer, not a crash. These exit 0 with an outcome code, not 2:

```
python -m cua replay account_detail@1.0.0 --input username=john --input account_id=99999
#   -> ACCOUNT_NOT_FOUND

python -m cua replay parabank_login@1.0.0 --input username=nosuchuser
#   -> INVALID_CREDENTIALS

python -m cua replay demo_tx_range@1.0.0 --input username=john --input account_id=13344 --input start_date=01-01-1990 --input end_date=12-31-1990
#   -> NO_TRANSACTIONS_FOUND
```

### State-changing — these do something to the account

**`open_savings_account@1.0.0`** — opens a real savings account funded from `account_id`. Step `s05_click` is recorded `risky`, so replay shows what is about to be submitted and asks first. Every successful run leaves a new account behind on your instance.

```
python -m cua replay open_savings_account@1.0.0 --input username=john --input account_id=13344
```

The same run unattended, to watch it fail closed rather than submit without a human:

```
python -m cua replay open_savings_account@1.0.0 --input username=john --input account_id=13344 --secret password=demo --non-interactive
```

**`transfer_demo@1.0.0`** — still a `draft`, so the approval gate refuses it and `--allow-draft` is required to run it at all. It moves real money between two of your accounts, and its submit step asks you to confirm first.

```
python -m cua replay transfer_demo@1.0.0 --allow-draft --input username=john --input account_id=13344 --input from_account_id=13344 --input to_account_id=13566 --input transfer_amount=100
```

### Composition and evidence

```
python -m cua graph account_detail@1.0.0     # capability -> pinned fragment versions
python -m cua graph demo_tx_range@1.0.0
```

Each run writes a redacted JSONL trace to `evidence/replay-<timestamp>/events.jsonl`, with a screenshot on failure. Exit code is 0 for a success or a declared business outcome, 2 for a hard failure.

### Observed results, and what to read them as

Run against a local ParaBank on 2026-09-15. Every command below returned `"status": "success"` and exit 0 — which is exactly why the `output_evidence` block matters more than the status line. Each output carries the `source_text` it was read from, and that is what tells you whether the capability answered the question you asked.

| capability | output | source_text actually read | verdict |
|---|---|---|---|
| `parabank_login` | — | — | ✅ reaches Accounts Overview |
| `account_detail` | `account_type`, `balance` | the account's own cells | ✅ real values |
| `account_activity` | `transactions` | the full `#transactionTable` | ✅ real table, 25 rows |
| `get_recent_transactions` | `recent_transactions` | `"Account Activity"` | ❌ the page heading |
| `demo_find_tx` | `match_count` | `"Find Transactions"` | ❌ the page heading |
| `demo_tx_range` | `match_count` | `"12456\n\t12567\n\t..."` | ❌ the account dropdown |
| `open_savings_account` | `new_account_id` | `"CHECKING\n  SAVINGS"` | ❌ the account-type dropdown |
| `demo_transfer` | `transfer_confirmation_message` | `"Transfer Funds"` | ❌ the page heading |

**Five of the seven extracting capabilities read the wrong element and reported success.** This is not a replay bug — the ladders resolve, the steps run, the success checkpoint is satisfied. It is a *recording* defect: the discovery model pointed the extract step at a heading or a dropdown rather than at the result, and nothing downstream re-checks what an extract is pointing at. `python -m cua capabilities` shows their declared signatures; only the target tells you what they really read:

```
demo_tx_range      -> css  #accountId          (the dropdown, not a count)
demo_find_tx       -> text "Find Transactions" (the heading, not a count)
open_savings_account -> css #type              (the dropdown, not the new id)
```

Two things follow, and both are in REPORT.md's known-weaknesses list. `review_notes` flags every single-rung extract (`only one locator candidate — no fallback`), which each of these carries; and a success checkpoint proves *the page arrived*, not *the output is the answer*. Reading `output_evidence.source_text` on a new recording is currently the only check that catches this, which is why it is printed on every run.

These five are left in the repo as recorded rather than hand-edited: an artifact is a record of what discovery actually produced, and a corrected one would misrepresent how often the model gets extraction right on the first pass.

### Human intervention, end to end

The transfer capability is the worked example, because it exercises the handoff for a reason nothing in the system can fix: the destination account does not exist on this instance.

`discover` drives a model, so run it from a shell with `ANTHROPIC_API_KEY` set (Setup step 4). It is the one command here that will stop and ask for a key if the variable is missing — and it asks *after* the browser is already open, which is an awkward moment to discover it.

```
python -m cua discover "Log in with the given username and password, open the Transfer Funds page, transfer the given amount from the given source account to the given destination account, and confirm the transfer completed" --url http://localhost:8080/parabank/index.htm --id demo_transfer --input username=john --input source_account=13344 --input destination_account=13455 --input amount=1 --secret password=demo --max-steps 12
```

**What to expect, in order:**

1. **A plan to approve.** Five steps, with `parabank_login@1.0.0` listed under `REUSES` — the login is replayed deterministically, not rediscovered. Answer `a`.
2. **An intervention request**, because `13455` is not in the To Account dropdown. The agent lists the accounts it can actually see and hands you the live browser. This is the honest case for a handoff: the agent is not stuck on a policy rule or a flaky locator, it is stuck on a caller-supplied value that does not exist.
3. **You act and report back.** Do the transfer by hand, type what you did, press Enter. Control returns and the run continues from the page you left it on.
4. **Hardening**, which adds the business outcomes and detectors the happy path could not observe — `DESTINATION_ACCOUNT_NOT_FOUND`, `INSUFFICIENT_FUNDS`, a session-timeout recovery, and so on.
5. **Verification refuses the recording:**

```
RECORDING DOES NOT MATCH THE APPROVED PLAN:
  - parameter 'destination_account' was planned as an input but no recorded step references it — it is frozen as a constant
  - parameter 'amount' was planned as an input but no recorded step references it — it is frozen as a constant

SUCCESS in 8 steps (1 escalation(s)).
Left as a draft: it did not match the approved plan.
```

That is the system working. The run *succeeded* — the money moved — but **the agent did not do the parameterised parts, you did**, and a human's handoff actions reach evidence without becoming replayable steps ([REPORT.md §7](REPORT.md)). So the artifact declares two inputs that no step consumes. Approving it would publish a capability whose `amount` and `destination_account` arguments are silently ignored.

`--max-escalations` (default 2) bounds how many times one run may ask; the first attempt above hit the limit after two handovers for the same missing account.

**Do not force this one:**

```
python -m cua approve demo_transfer@1.0.0            # refuses, listing both frozen parameters
python -m cua approve demo_transfer@1.0.0 --force    # approves a capability whose inputs do nothing
```

`--force` exists for gaps a reviewer has read and accepted. Frozen parameters are not that — the resulting replay types a hardcoded amount, never touches the destination, and reads the page heading as its confirmation, which is the row at the bottom of the table above.

**The fix is a better recording, not a forced approval.** Use a destination account that exists in the dropdown, and name both dropdowns and the submit button in the goal so the model records a step for each:

```
python -m cua discover "Log in with the given username and password, open the Transfer Funds page, select the source account in the From Account dropdown and the destination account in the To Account dropdown, enter the amount in the amount field, click the Transfer button, and read the transfer confirmation message" --url http://localhost:8080/parabank/index.htm --id demo_transfer --input username=john --input source_account=13344 --input destination_account=12345 --input amount=1 --secret password=demo --max-steps 15
```

Then approve without `--force`. If verification still reports a frozen parameter, the recording is still incomplete.

### Disclaimer

These commands were exercised on **macOS**. They are written to be platform-neutral and the test suite passes on both, but a full end-to-end replay depends on things outside this repo, and another machine can still diverge:

- **Account state — the most common cause of a confusing result.** ParaBank accounts are per-user and mutable. `13344` and `13566` are the account numbers from the machine these were recorded on; yours will differ, and `open_savings_account` changes them on every successful run. `ACCOUNT_NOT_FOUND` or `NO_TRANSACTIONS_FOUND` on a fresh instance usually means the data is not there, not that replay is broken — run `account_detail` against an account you can actually see in Accounts Overview first.
- **Chromium build.** Playwright ships its own browser, so run `playwright install chromium` on each machine rather than assuming the one already there. A different Chromium version can change rendering enough to move a text extraction.
- **Timing.** The recovery ladder absorbs ordinary slowness, but a cold `docker run` or a loaded machine can outlast a wait and surface as a locator failure rather than a timeout.
- **Docker networking.** `localhost:8080` assumes the container publishes straight to the host. Under a VM-backed Docker Desktop, a remote daemon, or WSL2 with its own network namespace, the app may be reachable somewhere else.
- **The public instance is shared.** If you point anything at `parabank.parasoft.com`, other people are using it and its data resets on its own schedule.

Encoding differences are the one class of breakage covered by tests: `tests/test_portability.py` reads every committed artifact with the local decoder. Run it first on a new machine — it fails fast, and needs no browser.

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
| `--max-escalations` | how many handovers a stuck discovery run may ask for (default 2) |
| `--no-escalate-policy-blocks` | do NOT hand over when POLICY is what stopped the agent (handover is on by default) |
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
