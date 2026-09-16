# Computer Use Automation System

I use an LLM **once** to work out how to do something in a legacy banking UI, record that run as a typed, versioned artifact, prove the artifact replays, and from then on replay it with no model in the loop. Saved capabilities become a catalog of callable tools an agent can invoke by name with typed arguments.

It includes a policy gate, redacted evidence logs, and human handoff on the live browser session.

Target app: ParaBank, a demo banking site Parasoft publishes for automation practice. It is server-rendered, table-laid-out and has no test IDs. It does expose a REST API; I drove the UI instead, because the UI is the case this project is about.

See [REPORT.md](REPORT.md) for the design reasoning, trade-offs, and what I cut.

---

## 1. Setup

**1. Python 3.11 or newer.**

**2. Install dependencies and the browser:**

```
pip install -r requirements.txt
playwright install chromium
```

**3. Start ParaBank:**

```
docker run -d -p 8080:8080 parasoft/parabank
```

It comes up at http://localhost:8080/parabank/index.htm. Register a throwaway user on the site and use those credentials. Never use real ones.

**4. Set your API key.** The variable is **`ANTHROPIC_API_KEY`** — that exact name is what the Anthropic SDK reads.

```powershell
$env:ANTHROPIC_API_KEY = "sk-ant-..."     # PowerShell
```
```bash
export ANTHROPIC_API_KEY=sk-ant-...        # bash / zsh
```

It only covers the current shell. A new terminal tab will not have it, and I deliberately do not read a `.env` file or save the key. If it is missing, the command says so and prompts without echo — which is better for a demo than `--api-key`, since that lands in shell history.

**Only `discover`, `chat` and `ui` need a key.** `replay`, `capabilities`, `graph`, `approve` and the tests need none — replay has no model in it.

### Running without live services

```
python -m pytest tests/
python -m cua capabilities
```

The test suite runs against a scriptable fake driver, so it needs no browser, no ParaBank and no API key. `capabilities` reads the artifact store and needs nothing running either.

---

## 2. Demo path

The through-line the brief asks for: a goal, an LLM run that completes it, a saved artifact, and a deterministic replay with inputs, outputs and error handling.

**Step 1 — Record login once, as a reusable fragment.** Small proven pieces make longer flows more reliable.

```
python -m cua discover "Log in with the given username and password and reach the Accounts Overview page" --url http://localhost:8080/parabank/index.htm --id parabank_login --kind fragment --input username=john --secret password=demo
```

You approve a plan before anything touches the browser. The run records a draft, the model adds the checkpoint and error handling the happy path could not show, and the artifact is replayed once from a clean browser session before it can be approved.

**Step 2 — Record a capability that reuses it.** The planner sees the fragment catalog; if it lists `parabank_login@1.0.0` under `REUSES`, login is replayed rather than rediscovered.

```
python -m cua discover "Log in, open Accounts Overview, click into account {{account_id}} to open its detail page, and read the account type into an output named account_type and the balance into an output named balance" --url http://localhost:8080/parabank/index.htm --id account_detail --input username=john --input account_id=13344 --secret password=demo
```

The run ends by printing what it actually read:

```
Read from the page:
  account_type = 'SAVINGS'
  balance      = '-$1984.90'
```

That block is the check that matters. If it shows a heading or a list of account numbers, the recording aimed at the wrong element.

**Step 3 — Replay it with new arguments, no model involved:**

```
python -m cua replay account_detail@1.0.0 --input username=john --input account_id=13344
```

You get the status, typed outputs, the source text each output was read from, and per-step reports. Exit code 0 for success or a business outcome, 2 for a hard failure.

**Step 4 — Replay against something that is not there.** A missing account is an answer, not a crash — and never another account's data:

```
python -m cua replay account_detail@1.0.0 --input username=john --input account_id=99999
#   -> business_outcome: ACCOUNT_NOT_FOUND
```

**Step 5 — Inspect the result:**

```
python -m cua capabilities                   # every artifact's typed signature and status
python -m cua graph account_detail@1.0.0     # capability -> pinned fragment versions
```

---

## 3. Capability catalog

Eight artifacts ship in `artifacts/`, all approved. Replaying any of them needs only ParaBank running.

| capability | does | inputs | returns | declared outcomes | changes state |
|---|---|---|---|---|---|
| `parabank_login` | logs in (fragment) | `username`, `password` | — | `INVALID_CREDENTIALS` | no |
| `account_detail` | reads one account | `account_id` | `account_type`, `balance` | `ACCOUNT_NOT_FOUND`, `NO_ACCOUNTS` | no |
| `account_activity` | reads the activity table | `account_id` | `transactions` | `ACCOUNT_NOT_FOUND`, `NO_TRANSACTIONS` | no |
| `find_tx_by_amount` | searches transactions by amount | `account_id`, `amount` | `transactions` | `NO_SUCH_ACCOUNT_ID`, `NO_TRANSACTIONS` | no |
| `tx_by_date_range` | searches transactions by date | `account_id`, `start_date`, `end_date` | `transactions` | `NO_SUCH_ACCOUNT_ID`, `NO_TRANSACTIONS` | no |
| `request_loan` | applies for a loan | `loan_amount`, `down_payment`, `account_id` | `loan_status` | `LOAN_APPROVED`, `LOAN_DENIED`, `NO_SUCH_ACCOUNT_ID` | **yes** |
| `open_savings_account` | opens a savings account | `account_id` | — | `NO_SUCH_ACCOUNT_ID`, `INSUFFICIENT_FUNDS` | **yes** |
| `update_contact` | updates phone and city | `phone_number`, `city` | `confirmation` | `CONTACT_INFO_PAGE_NOT_ACCESSIBLE` | **yes** |

Every capability except `parabank_login` also takes `username` and `password`, and declares `CREDENTIALS_REJECTED`.

`open_savings_account` opens the account but does not yet return the new account number — its recording has no extract step. It is listed as it stands.

---

## 4. Replaying the catalog

Each command is on one line so it pastes into bash, zsh and PowerShell unchanged. `john` and `13344` are placeholders — use an account that exists on your instance. `password` is left off so the CLI prompts for it without echo. Add `--headless` to skip the visible browser.

### Success

```
python -m cua replay parabank_login@1.0.0 --input username=john
python -m cua replay account_detail@1.0.0 --input username=john --input account_id=13344
python -m cua replay account_activity@1.0.0 --input username=john --input account_id=13344
python -m cua replay find_tx_by_amount@1.0.0 --input username=john --input account_id=13344 --input amount=100
python -m cua replay tx_by_date_range@1.0.0 --input username=john --input account_id=13344 --input start_date=01-01-2026 --input end_date=12-31-2026
```

Dates are **MM-DD-YYYY**. Year to date, without editing the range:

```bash
python -m cua replay tx_by_date_range@1.0.0 --input username=john --input account_id=13344 --input start_date=01-01-$(date +%Y) --input end_date=$(date +%m-%d-%Y)
```
```powershell
$s = "01-01-$(Get-Date -f yyyy)"; $e = Get-Date -f MM-dd-yyyy
python -m cua replay tx_by_date_range@1.0.0 --input username=john --input account_id=13344 --input start_date=$s --input end_date=$e
```

### Business outcomes

Each of these exits 0 with an outcome code, and none asks you anything.

```
python -m cua replay account_detail@1.0.0 --input username=john --input account_id=99999
#   -> ACCOUNT_NOT_FOUND          no such row on the overview

python -m cua replay find_tx_by_amount@1.0.0 --input username=john --input account_id=99999 --input amount=100
#   -> NO_SUCH_ACCOUNT_ID         the account is not in the dropdown

python -m cua replay find_tx_by_amount@1.0.0 --input username=john --input account_id=13344 --input amount=999999
#   -> NO_TRANSACTIONS            the results table was found and is empty

python -m cua replay tx_by_date_range@1.0.0 --input username=john --input account_id=13344 --input start_date=01-01-1990 --input end_date=12-31-1990
#   -> NO_TRANSACTIONS

python -m cua replay parabank_login@1.0.0 --input username=nosuchuser
#   -> INVALID_CREDENTIALS
```

### State-changing

These change the account, so each one stops and shows what it is about to submit before the commit:

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

```
python -m cua replay request_loan@1.0.0 --input username=john --input loan_amount=1500 --input down_payment=250 --input account_id=13344
python -m cua replay request_loan@1.0.0 --input username=john --input loan_amount=100000 --input down_payment=1 --input account_id=13344
python -m cua replay open_savings_account@1.0.0 --input username=john --input account_id=13344
python -m cua replay update_contact@1.0.0 --input username=john --input phone_number=5559876543 --input city=Boston
```

The two `request_loan` calls ask for a loan the bank should accept and one it should deny; the decision comes back as the outcome either way. `request_loan` and `open_savings_account` leave real records behind on every successful run. `update_contact` is reversible — set the values back.

To see a commit fail closed rather than submit without a person, run any of them unattended:

```
python -m cua replay open_savings_account@1.0.0 --input username=john --input account_id=13344 --secret password=demo --non-interactive
```

---

## 5. Human handoff

During discovery the agent can declare itself stuck; during replay a step can run out of retries. Either way the run pauses, prints the goal, the step, the reason, the URL and a screenshot, and hands you the **same live browser window**. You do the steps by hand, say what you did, press Enter, and the run resumes in that session. Automation cannot act while you hold it.

What you do by hand is recorded as real steps with locator ladders, so a capability you had to rescue replays without you next time. Passwords you type are never recorded.

A handoff is only raised when a person could actually change the outcome. It is **not** raised for a value the caller supplied that the surface does not have, or for a recording that points at the wrong element — those return an answer or a failure straight away. Every handoff is bounded, and `--non-interactive` never hands over.

To see one, ask for an account that does not exist during discovery:

```
python -m cua discover "Log in with the given username and password, open the Find Transactions page, select the given account in the Account dropdown, enter the amount in the Amount field, click the FIND TRANSACTIONS button in the amount section, and read the whole Transaction Results table into an output named transactions" --url http://localhost:8080/parabank/index.htm --id handoff_demo --input username=john --input account_id=99999 --input amount=100 --secret password=demo --max-steps 15
```

The agent reports that 99999 is not among the options, then offers you the session. If you finish the search on a different account, verification notices that `account_id` no longer reaches any step and keeps the recording a draft rather than publishing a capability that ignores its argument.

REPORT.md §5 covers how stuck is detected, the control-transfer model, and why these cases are excluded.

---

## 6. Talking to it

Saved artifacts become callable tools — the typed contract in the artifact *is* the function signature. The model picks a capability and fills in its arguments, then stops. It never drives the browser.

```
python -m cua chat --start-app        # terminal
python -m cua ui   --start-app        # browser chat window
```

```
you> what's the balance on 13344?
  [invoking account_detail {'account_id': '13344'}]
That account is a SAVINGS account with a balance of -$1984.90.

you> and 99999?
  [invoking account_detail {'account_id': '99999'}]
There's no account 99999 on this customer's profile.
```

Credentials never reach the model: sensitive inputs are stripped from the tool schema and injected at call time. Every factual claim has to come from a tool result, and the model is told today's date so "this year" resolves correctly.

---

## 7. Evidence

Every run writes a redacted JSONL trace, plus a screenshot on failure:

```
evidence/discovery-<timestamp>/events.jsonl    what the agent observed, decided and did
evidence/replay-<timestamp>/events.jsonl       each step, the locator used, and the outcome
evidence/*/intervention.png                    the page at the moment of a handoff
```

Secrets and sensitive parameters are masked at write time, before anything reaches disk.

---

## 8. Reference

### Tests

```
python -m pytest tests/
```

194 tests covering the artifact schema, policy gate, redaction and replay engine — the three-way result contract, recovery ladder, subflow flattening, ladder identity rules, drift, empty versus absent results, stale element handles, what must never escalate, and file encoding.

### Useful flags

| Flag | Effect |
|---|---|
| `--api-key` | model key; otherwise the environment, otherwise prompted |
| `--kind fragment` | record a reusable chunk instead of a capability |
| `--no-reuse` | hide the fragment catalog from the planner |
| `--no-smoke` | skip the proving replay (approval then has no proof behind it) |
| `--no-harden` | skip model-proposed checkpoint and detectors |
| `--no-auto-approve` | always leave the recording as a draft |
| `--non-interactive` | unattended replay: commits fail closed, nothing hands over |
| `--no-escalate-on-failure` | do not hand over when a replay step runs out of retries |
| `--max-escalations` | how many handoffs one discovery run may ask for (default 2) |
| `--no-escalate-policy-blocks` | do not hand over when policy is what stopped the agent |
| `--allow-draft` | replay an artifact that is not yet approved |
| `--start-app` | start ParaBank via docker if it is not up (`chat`, `ui`) |

### Layout

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
config/policy.yaml allowed domains, allowed actions, blocked routes, risk handling
artifacts/         the saved capabilities
tests/             the suite
```

---

## Disclaimer

I ran this on macOS and Windows, and the test suite passes on both. A full end-to-end replay depends on things outside this repo, so another machine can still behave differently.

**Account state is the most common cause of a confusing result.** ParaBank accounts are per-user and change over time. `13344` is the number from the machine I recorded on; yours will differ, and the state-changing capabilities add records every run. `ACCOUNT_NOT_FOUND` or `NO_TRANSACTIONS` on a fresh instance usually means the data is not there, not that replay is broken — run `account_detail` against an account you can see in Accounts Overview first.

**Extraction is the weak point.** Getting an extract aimed at the right element was the hardest part of this build. The system refuses to read a dropdown or a blank input as data, and element handles cannot outlive the page they came from, so the common ways of recording the wrong element fail loudly. That is not the same as being correct — check `output_evidence.source_text` before trusting an output, including from a capability you record yourself.

**Chromium is per-environment.** Run `playwright install chromium` on each machine and in each virtualenv. A different Chromium version can shift a text extraction.

**Timing and networking.** A cold `docker run` or a loaded machine can outlast a wait and show up as a locator failure. `localhost:8080` assumes the container publishes straight to the host; under a VM-backed Docker Desktop, a remote daemon or WSL2, the app may be elsewhere.

**File encoding.** Everything that touches a file goes through `cua/textio.py`, which writes UTF-8 with LF endings on every platform. This fixed a real bug where an artifact written on Windows could not be read on macOS. `tests/test_portability.py` checks every committed artifact, so run the suite first on a new machine.
