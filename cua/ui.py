"""A chat window over the whole system: discover, approve, replay.

The terminal chat proves the mechanism; this is the same layer with a surface
a person would actually use. It matters for one reason beyond looks: discovery
is interactive — a plan to approve, a commit to confirm, a value to supply, a
browser to take over — and in a terminal those are `input()` calls. Here they
become questions in the conversation, answered by typing back, which is what
makes "discover this for me" a thing you can say rather than a command you run.

The bridge is cua.prompt: a run executes on a worker thread with a prompter
installed that posts the question to the transcript and blocks until the UI
sends an answer. Nothing in the planner, the agent loop or the escalation
controller knows a UI exists.

Deliberately stdlib-only (http.server, one HTML page, polling). A framework
would be more comfortable and would not demonstrate anything further.
"""

from __future__ import annotations

import contextlib
import json
import queue
import sys
import threading
import traceback
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Optional

from . import prompt
from .chat import SYSTEM, CapabilityChat, capability_tools, describe_catalog, invocable
from .store import ArtifactStore

HELD = frozenset({"username", "password"})


def call_command(fn, **overrides):
    """Call a Typer command as an ordinary function.

    A Typer command's unpassed parameters default to `typer.Option(...)`
    OBJECTS rather than values, so calling it directly hands the body an
    OptionInfo where it expects a string — and the failure surfaces far from
    the cause (`TypeError: str expected, not OptionInfo`). Worse, it is silent
    until someone adds a new option, at which point this call site breaks
    without anyone touching it.

    Resolving every parameter from the signature makes the call robust to
    options added later: overrides win, everything else falls back to the
    option's real default.
    """
    import inspect
    kwargs = {}
    for name, param in inspect.signature(fn).parameters.items():
        if name in overrides:
            kwargs[name] = overrides[name]
            continue
        default = param.default
        # typer.Option(...)/Argument(...) carry the true default on `.default`
        kwargs[name] = getattr(default, "default", default)
    return fn(**kwargs)


class _Tee:
    """Everything a run prints, into the chat transcript.

    The plan block, the policy blocks, the intervention banner, the hardening
    summary — all of it is print()/typer.echo() deep inside code that has no
    idea a UI exists, and all of it is what a person needs in order to judge
    what just happened. Rather than rewrite every call site, stdout is
    redirected for the duration of a tool run and each completed line becomes
    a transcript event. The terminal keeps its copy too, so a crash is still
    debuggable where crashes are usually read.
    """

    def __init__(self, session: "Session", original):
        self.session, self.original, self.buf = session, original, ""

    def write(self, text: str) -> int:
        self.original.write(text)
        self.buf += text
        while "\n" in self.buf:
            line, self.buf = self.buf.split("\n", 1)
            if line.strip():
                self.session.emit("output", line.rstrip())
        return len(text)

    def flush(self) -> None:
        if self.buf.strip():
            self.session.emit("output", self.buf.rstrip())
            self.buf = ""
        self.original.flush()

UI_SYSTEM = SYSTEM + """

You can also BUILD new capabilities, not just invoke them:

- discover_capability: teach the system a new flow by driving the live site
  with an LLM once, then recording it. This is slow (a minute or more) and
  will ask the operator to approve a plan first. Use it when the user asks for
  something no existing capability covers and they want it learned.
- approve_capability: promote a recorded draft so it can be replayed. A draft
  exists but is not yet trusted; approving is a deliberate act, so only do it
  when the user asks.
- list_capabilities: what exists right now, including drafts.

When a call fails, report exactly what failed and what the error said. Never
paper over an error or claim something worked when the result says otherwise.
Errors are useful; hiding them is not.

WHEN NOTHING COVERS THE REQUEST — this replaces rule 2 above:
Say plainly that no capability does it yet, then OFFER to learn it, in one
sentence, naming what you would teach the system. For example: "I can't check
transfer history yet — want me to learn it? It takes a minute and you'll be
asked to approve a plan first."

Then stop and wait. Do NOT call discover_capability until they say yes; it
drives the real banking site and costs real time. When they agree:
  - choose a snake_case capability_id,
  - write the goal in plain words, marking anything that should be supplied
    per invocation as {{placeholder}} — an account number is a placeholder,
    never a constant,
  - pass example values for those placeholders in `inputs`, never credentials.
Afterwards, tell them whether it was approved or left a draft, and why."""

BUILD_TOOLS = [
    {
        "name": "list_capabilities",
        "description": "List every capability and fragment in the store, with "
                       "approval status. Use to answer 'what can you do' "
                       "precisely, or to find a draft that needs approving.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "discover_capability",
        "description": "Learn a NEW capability by driving the live site once "
                       "with an LLM and recording what worked. Slow, and asks "
                       "the operator to approve a plan first.",
        "input_schema": {
            "type": "object",
            "properties": {
                "goal": {"type": "string",
                         "description": "what to accomplish, in plain words, "
                                        "naming any per-invocation value as "
                                        "{{placeholder}}"},
                "capability_id": {"type": "string",
                                  "description": "snake_case id to save it under"},
                "inputs": {"type": "object",
                           "description": "example values to record with, e.g. "
                                          "{\"account_id\": \"13344\"}. Do not "
                                          "include credentials."},
            },
            "required": ["goal", "capability_id"],
        },
    },
    {
        "name": "approve_capability",
        "description": "Approve a recorded draft so it can be replayed.",
        "input_schema": {
            "type": "object",
            "properties": {"ref": {"type": "string",
                                   "description": "id@version, e.g. lookup@1.0.0"}},
            "required": ["ref"],
        },
    },
]


class Session:
    """One conversation: a transcript the browser polls, and a question bridge
    so a background run can stop and ask the person something."""

    def __init__(self, url: str, policy: str, model: str, credentials: dict,
                 headless: bool):
        self.url, self.policy, self.model = url, policy, model
        # Defence in depth: whitespace on a credential is invisible and
        # produces a failure that looks like anything but its cause.
        self.credentials = {k: (v.strip() if isinstance(v, str) else v)
                            for k, v in credentials.items()}
        self.headless = headless
        # Playwright's sync API is greenlet-based and bound to the thread that
        # created it. Each message runs on its own worker thread, so a driver
        # built during one turn is unusable in the next — "cannot switch to a
        # different thread", and every call after the first fails regardless
        # of what it was asked to do. One dedicated thread owns the browser
        # for the life of the session; every tool call is marshalled onto it.
        self._browser_thread = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="browser")
        self.events: list[dict] = []
        self.lock = threading.Lock()
        self.busy = False
        self.messages: list[dict] = []
        self._answers: "queue.Queue[str]" = queue.Queue()
        self._pending_question: Optional[str] = None
        self._driver = None
        self._chat: Optional[CapabilityChat] = None

    # -- transcript ---------------------------------------------------------

    def emit(self, kind: str, text: str, **extra) -> None:
        with self.lock:
            self.events.append({"i": len(self.events), "kind": kind,
                                "text": text, **extra})

    def since(self, i: int) -> dict:
        with self.lock:
            return {"events": self.events[i:], "busy": self.busy,
                    "awaiting": self._pending_question}

    # -- the question bridge ------------------------------------------------

    def prompter(self, question: str, sensitive: bool = False) -> str:
        """Installed via cua.prompt for the duration of a run. Posts the
        question into the chat and blocks this worker thread until the person
        answers in the UI."""
        with self.lock:
            self._pending_question = question
        self.emit("question", question, sensitive=bool(sensitive))
        answer = self._answers.get()
        with self.lock:
            self._pending_question = None
        self.emit("answer", "•••" if sensitive else answer)
        return answer

    def answer(self, text: str) -> None:
        self._answers.put(text)

    def awaiting(self) -> Optional[str]:
        with self.lock:
            return self._pending_question

    # -- the browser --------------------------------------------------------

    def chat_session(self) -> CapabilityChat:
        if self._chat is None:
            from .driver import PlaywrightDriver
            self._driver = PlaywrightDriver(headless=self.headless)
            self._chat = CapabilityChat(self._driver, self.policy,
                                        self.credentials)
        # Refresh the catalog: a capability discovered mid-conversation should
        # be callable in the next breath.
        self._chat.caps = {c.id: c for c in invocable(ArtifactStore())}
        return self._chat

    def _release_browser(self) -> None:
        """Give up this session's Playwright instance.

        Only ONE sync Playwright instance may exist per thread, and a
        discovery run builds its own driver. With a session browser already
        open, that second instance is refused outright ("using Playwright Sync
        API inside the asyncio loop") — so discovery succeeds on a fresh
        conversation and fails the moment anything has been replayed first.
        Released here and rebuilt lazily on the next invocation.

        Caller must already be on the browser thread.
        """
        if self._driver is not None:
            try:
                self._driver.close()
            except Exception:
                pass
        self._driver, self._chat = None, None

    def close(self) -> None:
        # The browser must also be torn down on the thread that created it.
        def _shut():
            if self._driver is not None:
                try:
                    self._driver.close()
                except Exception:
                    pass
        try:
            self._browser_thread.submit(_shut).result(timeout=15)
        except Exception:
            pass
        self._browser_thread.shutdown(wait=False)

    # -- tools ---------------------------------------------------------------

    def run_tool(self, name: str, args: dict) -> dict:
        """Marshal every tool call onto the one thread that owns the browser.

        The prompter is installed INSIDE that thread, not around this call:
        cua.prompt keeps the current prompter in thread-local storage, so a
        prompter installed out here would be invisible where the questions
        are actually asked, and a plan approval would go to the terminal
        instead of the chat.
        """
        def _on_browser_thread():
            with prompt.using(self.prompter):
                return self._dispatch(name, args)
        return self._browser_thread.submit(_on_browser_thread).result()

    def _dispatch(self, name: str, args: dict) -> dict:
        if name == "list_capabilities":
            return {"capabilities": [
                {"ref": a.ref, "kind": a.kind.value, "status": a.status.value,
                 "description": a.description,
                 "inputs": [i.name for i in a.inputs if not i.sensitive],
                 "outputs": [o.name for o in a.outputs]}
                for a in ArtifactStore().list()]}

        if name == "approve_capability":
            try:
                art = ArtifactStore().approve(args["ref"])
                return {"approved": art.ref, "status": art.status.value}
            except Exception as exc:
                return {"error": str(exc)}

        if name == "discover_capability":
            return self.discover(args)

        return self.chat_session().invoke(name, args)

    def discover(self, args: dict) -> dict:
        """Run a real discovery from the chat. Reuses this conversation's
        browser so the person can watch it happen in the window already open."""
        from .cli import discover as _discover_cmd  # the same code path as the CLI
        goal = args.get("goal", "")
        cap_id = args.get("capability_id", "")
        inputs = {str(k): str(v) for k, v in (args.get("inputs") or {}).items()}

        # The session's credentials are ALWAYS the ones used, and the model
        # never gets to nominate them. Without this the login fragment cannot
        # be reused for want of `username`, and the agent falls back to
        # guessing that some other parameter is the login — which is exactly
        # how a run ends up hammering a login form with an account number.
        for reserved in ("username", "password", "user", "pass"):
            inputs.pop(reserved, None)
        inputs["username"] = self.credentials.get("username", "")

        # A discovery run owns Playwright for its duration; this session
        # cannot be holding it at the same time.
        self._release_browser()

        self.emit("status",
                  f"Discovering '{cap_id}' as user "
                  f"'{self.credentials.get('username', '?')}' — this drives the "
                  f"real site and will ask you to approve a plan.")
        try:
            # Same code path as the CLI, with this session's credentials.
            # Anything not named here keeps the command's own default.
            call_command(
                _discover_cmd,
                goal=goal, url=self.url, artifact_id=cap_id,
                input=[f"{k}={v}" for k, v in inputs.items()],
                secret=[f"password={self.credentials.get('password','')}"],
                policy=self.policy, model=self.model,
                headless=self.headless,
                api_key="")   # already resolved into the environment by `ui`
            return {"recorded": cap_id, "note": "see the transcript for whether "
                                                "it was approved or left a draft"}
        except SystemExit as exc:           # typer.Exit
            return {"error": f"discovery did not complete (exit {exc.code}). "
                             f"The transcript above says where it stopped."}
        except Exception as exc:
            return {"error": f"{type(exc).__name__}: {exc}"}


REPLAY_ONLY_NOTE = (
    "\n\n[DISCOVERY IS OFF. You may only invoke capabilities that already "
    "exist — you cannot learn anything new this turn. Before invoking, make "
    "sure you have every argument the capability needs; if one is missing, "
    "ASK for it rather than guessing or inventing a value. If nothing covers "
    "the request at all, say so and tell them to switch Discovery on.]")

DISCOVER_NOTE = (
    "\n\n[DISCOVERY IS ON, so the operator has already agreed to learning a "
    "new capability — do not ask whether to proceed. Do make sure the request "
    "is CONCRETE first: you need a clear goal, a snake_case id, and an example "
    "value for each per-invocation placeholder. If any of that is vague or "
    "missing, ask one short follow-up and wait; a discovery run drives the "
    "real bank for a minute, so it is worth one question to aim it properly. "
    "Never ask for a username or password — those are supplied automatically "
    "and must not appear in `inputs`.]")


def _turn(session: Session, said: str, client, model: str,
          discovery: bool = False) -> None:
    """One user message, run to completion on a worker thread.

    `discovery` is the operator's toggle. Replay and discovery differ by
    orders of magnitude in cost and consequence — seconds of frozen data
    versus a minute of an LLM driving a real bank — which is too large a
    difference to infer from phrasing. With the toggle off the tools that
    could learn something are not merely discouraged, they are absent.
    """
    session.busy = True
    try:
        caps = invocable(ArtifactStore())
        tools = capability_tools(caps, HELD)
        tools = tools + BUILD_TOOLS if discovery else tools
        said += DISCOVER_NOTE if discovery else REPLAY_ONLY_NOTE
        session.messages.append({"role": "user", "content": said})
        for _ in range(8):
            resp = client.messages.create(model=model, max_tokens=1500,
                                          system=UI_SYSTEM, tools=tools,
                                          messages=session.messages)
            session.messages.append({"role": "assistant", "content": resp.content})
            for b in resp.content:
                if b.type == "text" and b.text.strip():
                    session.emit("assistant", b.text.strip())
            calls = [b for b in resp.content if b.type == "tool_use"]
            if not calls:
                return
            results = []
            for call in calls:
                session.emit("tool", f"{call.name}({json.dumps(dict(call.input))})")
                tee = _Tee(session, sys.stdout)
                # The prompter is installed on the browser thread by
                # run_tool; here we only need the run's output captured.
                with contextlib.redirect_stdout(tee):
                    try:
                        out = session.run_tool(call.name, dict(call.input))
                    except Exception as exc:
                        out = {"error": f"{type(exc).__name__}: {exc}"}
                        session.emit("error", traceback.format_exc()[-600:])
                    finally:
                        tee.flush()
                status = out.get("status") or ("error" if "error" in out else "ok")
                session.emit("result", f"{call.name} → {status}",
                             detail=json.dumps(out, indent=2)[:1500])
                results.append({"type": "tool_result", "tool_use_id": call.id,
                                "content": json.dumps(out)})
            session.messages.append({"role": "user", "content": results})
    except Exception as exc:
        session.emit("error", f"{type(exc).__name__}: {exc}")
    finally:
        session.busy = False


PAGE = """<!doctype html><html><head><meta charset="utf-8">
<title>Bank Assistant</title><style>
*{box-sizing:border-box}
body{margin:0;font:14px/1.5 system-ui,-apple-system,Segoe UI,sans-serif;
 background:#0f1115;color:#e6e8ee;display:flex;flex-direction:column;height:100vh}
header{padding:12px 18px;border-bottom:1px solid #242833;background:#151823}
header b{color:#7aa2f7}
#cat{color:#8b91a7;font-size:12.5px;white-space:pre-wrap;margin-top:6px}
#who{float:right;color:#e0af68;font-size:12.5px;background:#1b1f2b;padding:3px 10px;border-radius:999px}
#log{flex:1;overflow-y:auto;padding:18px;display:flex;flex-direction:column;gap:10px}
.msg{max-width:80ch;padding:9px 13px;border-radius:10px;white-space:pre-wrap;word-wrap:break-word}
.you{align-self:flex-end;background:#2a3f6b}
.assistant{background:#1b1f2b}
.tool{background:#141824;color:#8b91a7;font-family:ui-monospace,Consolas,monospace;font-size:12.5px}
.result{background:#141824;color:#9ece6a;font-family:ui-monospace,Consolas,monospace;font-size:12.5px;cursor:pointer}
.result.bad{color:#f7768e}
.status{color:#e0af68;font-style:italic;background:none;padding-left:0}
.error{background:#3a1d24;color:#ff9fb0;font-family:ui-monospace,Consolas,monospace;font-size:12.5px}
.question{background:#3a3320;color:#ffd493;border-left:3px solid #e0af68}
.answer{align-self:flex-end;background:#2a3f6b;opacity:.75}
pre{margin:6px 0 0;white-space:pre-wrap;color:#8b91a7;font-size:12px;display:none}
.result.open pre{display:block}
footer{padding:12px 18px;border-top:1px solid #242833;background:#151823;display:flex;gap:10px}
input{flex:1;padding:10px 13px;border-radius:8px;border:1px solid #2c3140;
 background:#0f1115;color:#e6e8ee;font:inherit}
button{padding:10px 18px;border-radius:8px;border:0;background:#7aa2f7;color:#0f1115;
 font:inherit;font-weight:600;cursor:pointer}
#modes{display:flex;gap:8px;align-items:center;padding:8px 18px;background:#151823;
 border-top:1px solid #242833;font-size:12.5px}
#toggle{display:inline-flex;align-items:center;gap:8px;padding:4px 12px 4px 6px;
 border-radius:999px;background:#1b1f2b;color:#8b91a7;cursor:pointer;user-select:none;
 border:1px solid #2c3140}
#toggle i{width:30px;height:17px;border-radius:999px;background:#2c3140;position:relative;
 transition:background .15s}
#toggle i::after{content:"";position:absolute;top:2px;left:2px;width:13px;height:13px;
 border-radius:50%;background:#8b91a7;transition:transform .15s,background .15s}
#toggle.on{background:#4a3a1c;border-color:#e0af68;color:#ffd493}
#toggle.on i{background:#e0af68}
#toggle.on i::after{transform:translateX(13px);background:#1a1206}
#modenote{color:#8b91a7;margin-left:6px}
.output{background:#0c0e14;color:#7f869c;font-family:ui-monospace,Consolas,monospace;
 font-size:12px;padding:3px 13px;border-left:2px solid #242833;border-radius:0;max-width:none}
button:disabled{opacity:.45;cursor:default}
#hint{color:#e0af68;font-size:12.5px;padding:0 18px 8px;background:#151823}
</style></head><body>
<header><b>Bank Assistant</b> — ask for something, or ask me to learn it
<span id="who">signed in as __USER__</span>
<div id="cat">__CATALOG__</div></header>
<div id="log"></div><div id="hint"></div>
<div id="modes">
  <span id="toggle" role="switch" title="Off: only existing capabilities. On: may learn new ones by driving the real site."><i></i>Discovery</span>
  <span id="modenote"></span>
</div>
<footer><input id="in" placeholder="e.g. what's the balance on 13344?" autofocus>
<button id="go">Send</button></footer>
<script>
let seen=0, awaiting=null;
const log=document.getElementById('log'), inp=document.getElementById('in'),
      go=document.getElementById('go'), hint=document.getElementById('hint');
function add(cls,text,detail){
  const d=document.createElement('div'); d.className='msg '+cls; d.textContent=text;
  if(detail){ const p=document.createElement('pre'); p.textContent=detail; d.appendChild(p);
    d.onclick=()=>d.classList.toggle('open'); }
  if(cls==='result'&&/→ (error|hard_failure)/.test(text)) d.classList.add('bad');
  log.appendChild(d); log.scrollTop=log.scrollHeight;
}
let discovery=false;
const tog=document.getElementById('toggle');
const PH={off:"e.g. what's the balance on 13344?",
          on:"describe what it should learn to do, e.g. find transactions over an amount"};
function setDiscovery(on){
  discovery=on; tog.classList.toggle('on',on);
  document.getElementById('modenote').textContent = on
    ? 'ON — may learn new capabilities by driving the real site (slow)'
    : 'OFF — existing capabilities only';
  if(!awaiting) inp.placeholder = on ? PH.on : PH.off;
}
tog.onclick=()=>setDiscovery(!discovery);
async function send(){
  const v=inp.value.trim(); if(!v) return; inp.value='';
  add('you', discovery ? '[discover] '+v : v);
  await fetch('/api/send',{method:'POST',
    body:JSON.stringify({text:v,discovery:discovery})});
  tick();            // show the response immediately; the loop keeps running
}
go.onclick=send; inp.onkeydown=e=>{ if(e.key==='Enter') send(); };
setDiscovery(false);
let delay=800, ticking=false;
async function tick(){
  if(ticking) return;          // one request in flight at a time
  ticking=true;
  try{
    const r=await fetch('/api/events?since='+seen);
    const d=await r.json();
    for(const e of d.events){ seen=e.i+1;
      if(e.kind==='assistant') add('assistant',e.text);
      else if(e.kind==='you') add('you',e.text);
      else if(e.kind==='tool') add('tool','▸ '+e.text);
      else if(e.kind==='result') add('result',e.text,e.detail);
      else if(e.kind==='status') add('status',e.text);
      else if(e.kind==='error') add('error',e.text);
      else if(e.kind==='output') add('output',e.text);
      else if(e.kind==='question') add('question','? '+e.text);
      else if(e.kind==='answer') add('answer',e.text);
    }
    awaiting=d.awaiting;
    hint.textContent = awaiting ? 'Waiting on you: '+awaiting
                     : (d.busy ? 'Working…' : '');
    inp.placeholder = awaiting ? 'Answer above…' : (discovery?PH.on:PH.off);
    // A run in progress prints steadily; idle needs far less.
    delay = (d.busy||awaiting) ? 250 : 900;
  }catch(err){
    // A hiccup must never kill the loop: that is how the transcript ends up
    // frozen until the next message accidentally restarts it.
    delay = 1500;
  }finally{
    ticking=false;
  }
}
async function loop(){ await tick(); setTimeout(loop, delay); }
loop();
</script></body></html>"""


def serve(session: Session, client, model: str, port: int) -> None:
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):        # keep the console for the run itself
            pass

        def _send(self, code: int, body: bytes, ctype: str) -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if self.path.startswith("/api/events"):
                i = 0
                if "since=" in self.path:
                    try:
                        i = int(self.path.split("since=")[1].split("&")[0])
                    except ValueError:
                        i = 0
                self._send(200, json.dumps(session.since(i)).encode(),
                           "application/json")
            else:
                caps = invocable(ArtifactStore())
                page = (PAGE.replace("__CATALOG__", describe_catalog(caps, HELD))
                        .replace("__USER__",
                                 session.credentials.get("username", "?")))
                self._send(200, page.encode("utf-8"), "text/html; charset=utf-8")

        def do_POST(self):
            n = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(n) or b"{}")
            said = body.get("text", "")
            discovery = bool(body.get("discovery", False))
            if session.awaiting() is not None:
                # The person is answering a question a run is blocked on, not
                # starting a new turn.
                session.answer(said)
            elif not session.busy:
                threading.Thread(target=_turn,
                                 args=(session, said, client, model, discovery),
                                 daemon=True).start()
            self._send(200, b'{"ok":true}', "application/json")

    # Threaded: a poll must never wait behind another request. With a
    # single-threaded server the transcript stalls exactly when it matters —
    # while a long run is producing the output worth watching.
    ThreadingHTTPServer(("127.0.0.1", port), Handler).serve_forever()
