"""Surface driver: the seam between "the recorded flow" and "how we
perceive/act on a surface".

Everything above this module (discovery loop, replay engine, escalation)
speaks only observe() / act() / resolve() in terms of the schema types.
Supporting a legacy web app or a desktop app means writing another driver;
artifacts and both engines are untouched. Session ownership also lives here,
which is what makes human handoff work identically for discovery and replay.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional, Protocol

from .schemas import (
    DetectorMatch,
    LocatorCandidate,
    LocatorStrategy,
    Target,
    WaitCondition,
)


_MODIFIER_ALIASES = {"control": "ControlOrMeta", "ctrl": "ControlOrMeta",
                     "meta": "ControlOrMeta", "cmd": "ControlOrMeta",
                     "command": "ControlOrMeta"}


def normalize_key(key: str) -> str:
    """Make a recorded keystroke mean the same thing on Windows and macOS.

    An artifact records what the discovery model pressed, and "Control+A"
    recorded on Windows is "Meta+A" on a mac -- the same intent, a different
    key. Playwright resolves `ControlOrMeta` per platform at press time, so
    rewriting the modifier here keeps one artifact correct on both, rather
    than making the recording OS part of the contract.

    Only the modifier is touched. The key itself ("Enter", "a", "ArrowDown")
    is identical across platforms and passes through untouched.
    """
    parts = key.split("+")
    if len(parts) == 1:
        return key
    *mods, final = parts
    return "+".join([_MODIFIER_ALIASES.get(m.strip().lower(), m) for m in mods]
                    + [final])


class Controller(str, Enum):
    AUTOMATION = "automation"
    HUMAN = "human"


@dataclass
class ObservedElement:
    ref: str                 # driver-scoped handle, stable within one observation
    role: str
    name: str                # accessible name / label / text
    tag: str
    value: Optional[str] = None
    bounds: Optional[tuple[float, float, float, float]] = None


@dataclass
class Observation:
    url: str
    title: str
    elements: list[ObservedElement] = field(default_factory=list)
    screenshot_path: Optional[str] = None


class SurfaceDriver(Protocol):
    """The contract every surface implementation satisfies."""

    def observe(self, screenshot_to: Optional[str] = None) -> Observation: ...
    def navigate(self, url: str) -> None: ...
    def click(self, target: Target) -> LocatorStrategy: ...
    def type_text(self, target: Target, text: str) -> LocatorStrategy: ...
    def select(self, target: Target, value: str) -> LocatorStrategy: ...
    def read_text(self, target: Target) -> tuple[str, LocatorStrategy]: ...
    def press(self, key: str) -> None: ...
    def wait_for(self, cond: WaitCondition) -> bool: ...
    def matches(self, match: DetectorMatch) -> bool: ...
    def current_url(self) -> str: ...
    # Session ownership for human handoff:
    def cede_control(self) -> None: ...
    def resume_control(self) -> None: ...
    def controller(self) -> Controller: ...
    def drain_human_actions(self) -> list[dict]: ...
    def close(self) -> None: ...


class ElementNotFound(Exception):
    def __init__(self, target: Target, tried: list[LocatorCandidate],
                 unusable: Optional[list[str]] = None):
        self.target = target
        self.tried = tried
        self.unusable = unusable or []
        detail = f"(tried {[c.strategy.value for c in tried]}"
        if self.unusable:
            detail += (f"; {self.unusable} matched an element that cannot take "
                       f"this action")
        super().__init__(
            f"no candidate resolved for '{target.description}' " + detail + ")"
        )


class PlaywrightDriver:
    """Web implementation. Headed by default so a human can take over the
    same live session during escalation."""

    def __init__(self, headless: bool = False, default_timeout_ms: int = 5000):
        from playwright.sync_api import sync_playwright

        self._pw = sync_playwright().start()
        self._browser = self._pw.chromium.launch(headless=headless)
        self._context = self._browser.new_context()
        self._page = self._context.new_page()
        self._page.set_default_timeout(default_timeout_ms)
        self._default_timeout_ms = default_timeout_ms
        self._owns_browser = True
        self._controller = Controller.AUTOMATION
        self._human_actions: list[dict] = []
        self._recording_hooks_installed = False

    def fresh_session(self) -> "PlaywrightDriver":
        """A second, cookie-isolated session on the SAME browser.

        Playwright's sync API refuses a second instance in one thread, and a
        smoke replay must not inherit this session's login anyway — the whole
        point is to prove the recording works from a clean start, the way
        production would run it. A new context gives exactly that.
        """
        clone = PlaywrightDriver.__new__(PlaywrightDriver)
        clone._pw = self._pw
        clone._browser = self._browser
        clone._owns_browser = False          # must not tear down the shared browser
        clone._context = self._browser.new_context()
        clone._page = clone._context.new_page()
        clone._page.set_default_timeout(self._default_timeout_ms)
        clone._default_timeout_ms = self._default_timeout_ms
        clone._controller = Controller.AUTOMATION
        clone._human_actions = []
        clone._recording_hooks_installed = False
        return clone

    # -- perception ---------------------------------------------------------

    def observe(self, screenshot_to: Optional[str] = None) -> Observation:
        elements: list[ObservedElement] = []
        # Interactable elements plus headings/cells that carry state. This is
        # a *digest* for the LLM, not a DOM dump: role, name, value, position.
        js = """
        () => {
          // `table` is here so a COLLECTION is addressable, not just its
          // individual cells: without it the only readable units are single
          // <td>s, and a goal like "read the transactions" has no element
          // that represents it — the agent reads one cell, cannot express
          // the list, and retries until it gives up.
          //
          // But a legacy page is BUILT from tables, so including them all
          // buries the page in layout scaffolding and pushes the rows that
          // matter past the digest cutoff. Only data tables qualify: one
          // with header cells, or with several rows of several cells. Rows
          // are left out entirely — the table covers the list case, and a
          // single <tr> has no stable way to be identified anyway.
          const isDataTable = (t) =>
            t.querySelector('th') !== null ||
            t.querySelectorAll(':scope > tbody > tr, :scope > tr').length > 2;
          const sel = 'a, button, input, select, textarea, [role], h1, h2, td, th, table';
          const out = [];
          let i = 0;
          for (const el of document.querySelectorAll(sel)) {
            const r = el.getBoundingClientRect();
            if (r.width === 0 || r.height === 0) continue;
            if (el.tagName === 'TABLE' && !isDataTable(el)) continue;
            const label = el.labels && el.labels[0] ? el.labels[0].innerText : '';
            const name = (el.getAttribute('aria-label') || label ||
                          el.innerText || el.value || el.placeholder || '')
                          .trim().slice(0, 80);
            out.push({
              ref: 'e' + (i++),
              role: el.getAttribute('role') || el.tagName.toLowerCase(),
              name: name,
              tag: el.tagName.toLowerCase(),
              value: el.value !== undefined ? String(el.value).slice(0, 80) : null,
              bounds: [r.x, r.y, r.width, r.height],
            });
            el.setAttribute('data-cua-ref', 'e' + (i - 1));
          }
          return out;
        }
        """
        for raw in self._page.evaluate(js):
            elements.append(ObservedElement(**raw))
        shot = None
        if screenshot_to:
            self._page.screenshot(path=screenshot_to, full_page=False)
            shot = screenshot_to
        return Observation(
            url=self._page.url, title=self._page.title(),
            elements=elements, screenshot_path=shot,
        )

    # -- targeting ladder ---------------------------------------------------

    def _resolve(self, target: Target, requires: Optional[str] = None):
        """Try candidates in recorded order; return (locator, strategy).

        `requires` is what the caller is about to DO with the element. A rung
        that resolves something the action cannot be performed on is not a
        match — it is a near miss — so the ladder keeps descending instead of
        returning it. Without this, a row-anchored rung that lands on the
        <td> wrapping an <input> wins over a perfectly good '#amount' rung
        below it, and replay dies on "Element is not an <input>" with the
        working candidate never tried.
        """
        tried: list[LocatorCandidate] = []
        unusable: list[str] = []
        # Rungs passed over because the element simply was not there. THAT is
        # drift: the page stopped matching how the artifact describes it. A
        # rung skipped as unusable is a defect in the recording, not drift,
        # and must not raise a false alarm.
        self._last_missing: list[str] = []
        self._last_ambiguous: list[str] = []
        ambiguous_fallback = None

        for cand in target.candidates:
            tried.append(cand)
            try:
                loc = self._locator_for(cand)
                count = loc.count()
                if count == 0:
                    self._last_missing.append(cand.strategy.value)
                    continue
                first = loc.first
                if requires and not self._usable(first, requires):
                    unusable.append(cand.strategy.value)
                    continue
                if count == 1:
                    return first, cand.strategy
                # Several matches: this rung does NOT identify an element, it
                # describes a kind of element. Taking .first is a guess, and
                # on a page with four buttons all reading "FIND TRANSACTIONS"
                # the guess submits the wrong form. Keep looking for a rung
                # that resolves uniquely; fall back only if none does, so an
                # artifact that has always relied on .first still runs.
                self._last_ambiguous.append(f"{cand.strategy.value}x{count}")
                if ambiguous_fallback is None:
                    ambiguous_fallback = (first, cand.strategy)
            except Exception:
                self._last_missing.append(cand.strategy.value)
                continue

        if ambiguous_fallback is not None:
            return ambiguous_fallback
        raise ElementNotFound(target, tried, unusable)

    def last_ambiguous_rungs(self) -> list[str]:
        """Rungs the most recent resolution passed over because they matched
        more than one element. A recording that relies on one is guessing."""
        return list(getattr(self, "_last_ambiguous", []))

    def last_missing_rungs(self) -> list[str]:
        """Rungs the most recent resolution passed over because nothing
        matched. Non-empty means the artifact's preferred description of that
        element has stopped working — the drift signal."""
        return list(getattr(self, "_last_missing", []))

    @staticmethod
    def _usable(loc, requires: str) -> bool:
        """Can this element actually take the intended action?"""
        try:
            tag = loc.evaluate("el => el.tagName.toLowerCase()")
        except Exception:
            return False
        if requires == "fillable":
            if tag in ("input", "textarea"):
                return True
            try:
                return bool(loc.evaluate("el => el.isContentEditable"))
            except Exception:
                return False
        if requires == "selectable":
            return tag == "select"
        return True

    def _locator_for(self, cand: LocatorCandidate):
        p = self._page
        s = LocatorStrategy
        if cand.strategy == s.ROLE_NAME:
            return p.get_by_role(cand.role or "button", name=cand.value)
        if cand.strategy == s.LABEL:
            return p.get_by_label(cand.value)
        if cand.strategy == s.TEXT:
            return p.get_by_text(cand.value, exact=False)
        if cand.strategy == s.PLACEHOLDER:
            return p.get_by_placeholder(cand.value)
        
        if cand.strategy == s.RELATIVE_TEXT:
            # 'anchor||css' : element matching css inside the row anchored by
            # the anchor text. No page-wide fallback: if the anchor row does
            # not exist, this candidate must FAIL, never silently match a
            # different row's data.
            anchor, css = cand.value.split("||", 1)
            return p.locator(f"tr:has-text('{anchor}')").locator(css)
        if cand.strategy == s.CSS:
            return p.locator(cand.value)
        if cand.strategy == s.XPATH:
            return p.locator(f"xpath={cand.value}")
        raise ValueError(f"unknown strategy {cand.strategy}")

    # -- actions ------------------------------------------------------------

    def _assert_automation(self) -> None:
        if self._controller is not Controller.AUTOMATION:
            raise RuntimeError("automation acted while a human holds the session")

    def navigate(self, url: str) -> None:
        self._assert_automation()
        before = self.page_signature()
        self._page.goto(url)
        self.after_action(before)

    def click(self, target: Target) -> LocatorStrategy:
        self._assert_automation()
        loc, strat = self._resolve(target)
        before = self.page_signature()
        loc.click()
        self.after_action(before)
        return strat

    def type_text(self, target: Target, text: str) -> LocatorStrategy:
        self._assert_automation()
        loc, strat = self._resolve(target, requires="fillable")
        loc.fill(text)
        self._confirm_landed(loc, text, target)
        return strat

    def _confirm_landed(self, loc, text: str, target: Target,
                        timeout_ms: int = 3000) -> None:
        """An action is not complete because it was issued — it is complete
        when the surface shows its effect. A legacy form can be readonly,
        disabled, or reset the field from JS, and fill() reports success
        either way. Poll for the effect instead of assuming it, so a slow UI
        gets time to apply it and a rejecting one fails here, loudly, rather
        than three steps later as an unrelated-looking error.

        A value that comes back DIFFERENT but non-empty is accepted: fields
        legitimately reformat what you type ("5551234567" -> "(555) 123-4567").
        A value that comes back EMPTY means nothing landed.
        """
        if not text:
            return
        deadline = time.time() + timeout_ms / 1000
        observed = None
        while time.time() < deadline:
            try:
                observed = loc.input_value()
            except Exception:
                return  # not a value-bearing element; nothing to confirm
            if observed:
                return
            time.sleep(0.05)
        raise RuntimeError(
            f"typed into '{target.description}' but the field is still empty "
            f"after {timeout_ms}ms (readonly, disabled, or cleared by the page?)")

    def select(self, target: Target, value: str) -> LocatorStrategy:
        self._assert_automation()
        loc, strat = self._resolve(target, requires="selectable")
        # A legacy <select> commonly posts back from onchange, so this waits
        # too — the gap that made a dropdown look like it had done nothing.
        before = self.page_signature()
        loc.select_option(label=value)
        self.after_action(before)
        return strat

    def read_text(self, target: Target) -> tuple[str, LocatorStrategy]:
        loc, strat = self._resolve(target)
        return (loc.inner_text().strip(), strat)

    def press(self, key: str) -> None:
        self._assert_automation()
        before = self.page_signature()
        self._page.keyboard.press(normalize_key(key))
        self.after_action(before)

    # -- waiting and detection ----------------------------------------------

    def wait_for(self, cond: WaitCondition) -> bool:
        try:
            if cond.kind == "url_matches":
                self._page.wait_for_url(
                    lambda u: bool(cond.value) and cond.value in u,
                    timeout=cond.timeout_ms,
                )
            elif cond.kind == "network_idle":
                self._page.wait_for_load_state(
                    "networkidle", timeout=cond.timeout_ms
                )
            elif cond.kind == "text_visible":
                self._page.get_by_text(cond.value, exact=False).first.wait_for(
                    state="visible", timeout=cond.timeout_ms
                )
            elif cond.kind == "element_visible" and cond.target:
                loc, _ = self._resolve(cond.target)
                loc.wait_for(state="visible", timeout=cond.timeout_ms)
            return True
        except Exception:
            return False

    def matches(self, match: DetectorMatch) -> bool:
        try:
            if match.kind == "url_matches":
                return match.value in self._page.url
            if match.kind == "text_visible":
                return self._page.get_by_text(
                    match.value, exact=False
                ).first.is_visible()
            if match.kind == "element_visible":
                return self._page.locator(match.value).first.is_visible()
            if match.kind == "element_absent":
                return self._page.locator(match.value).count() == 0
        except Exception:
            return False
        return False

    def current_url(self) -> str:
        return self._page.url

    # -- session ownership (human handoff) ----------------------------------

    def cede_control(self) -> None:
        """Hand the live page to a human. Hooks are installed as an init
        script so they survive the human navigating between pages."""
        if not self._recording_hooks_installed:
            self._page.expose_function(
                "_cuaRecord", lambda ev: self._human_actions.append(ev)
            )
            hook_js = """
                () => {
                  document.addEventListener('click', e => {
                    const t = e.target;
                    window._cuaRecord({type:'click', ts: Date.now(),
                      page: location.pathname,
                      desc: (t.innerText || t.value || t.name || t.tagName)
                            .trim().slice(0,60)});
                  }, true);
                  document.addEventListener('change', e => {
                    const t = e.target;
                    window._cuaRecord({type:'change', ts: Date.now(),
                      page: location.pathname,
                      desc: (t.name || t.id || t.tagName).slice(0,60)});
                  }, true);
                }
            """
            self._page.add_init_script(f"({hook_js})()")   # future navigations
            self._page.evaluate(hook_js)                    # current page, now
            self._recording_hooks_installed = True
        self._controller = Controller.HUMAN

    def resume_control(self) -> None:
        self._controller = Controller.AUTOMATION

    def controller(self) -> Controller:
        return self._controller

    def drain_human_actions(self) -> list[dict]:
        actions, self._human_actions = self._human_actions, []
        return actions

    def close(self) -> None:
        if not getattr(self, "_owns_browser", True):
            self._context.close()   # a fresh_session clone: leave the browser up
            return
        self._browser.close()
        self._pw.stop()


# ---------------------------------------------------------------------------
# Discovery-time extensions for PlaywrightDriver: act on elements by the
# observation ref, and derive the locator candidate ladder from a live
# element so the recorder can write robust targets into the artifact.
# ---------------------------------------------------------------------------

def _ref_locator(self, ref: str):
    return self._page.locator(f"[data-cua-ref='{ref}']").first

def _click_ref(self, ref: str) -> None:
    self._assert_automation()
    before = self.page_signature()
    self._ref_locator(ref).click()
    self.after_action(before)

def _type_ref(self, ref: str, text: str) -> None:
    self._assert_automation()
    self._ref_locator(ref).fill(text)

def _select_ref(self, ref: str, value: str) -> None:
    self._assert_automation()
    before = self.page_signature()
    self._ref_locator(ref).select_option(label=value)
    self.after_action(before)

def _read_ref(self, ref: str) -> str:
    loc = self._ref_locator(ref)
    txt = loc.inner_text() if loc.evaluate("el => el.tagName") != "INPUT" else loc.input_value()
    return txt.strip()

def _submits_ref(self, ref: str) -> bool:
    """Does clicking this element COMMIT a form?

    Structural, not lexical: the element really is the control that submits,
    whatever it happens to be labelled. Pattern-matching a button's text
    catches 'Transfer' and misses 'Proceed', and the label is the one thing
    a vendor is free to rename per tenant.
    """
    try:
        return bool(self._ref_locator(ref).evaluate(
            """
            el => {
              const tag = el.tagName;
              const t = (el.getAttribute('type') || '').toLowerCase();
              if (t === 'reset') return false;
              const buttonish =
                (tag === 'INPUT' && ['submit', 'button', 'image'].indexOf(t) !== -1)
                || tag === 'BUTTON';
              if (!buttonish) return false;
              const form = el.closest('form');
              // Authentication establishes a SESSION; it does not commit
              // business state. Treating a login as a state change would
              // make every capability that logs in demand a human, which
              // ends unattended replay for the whole system. The risky
              // things happen after the session exists, and those are still
              // gated. (Limit: a change-password form also contains a
              // password field — see REPORT.md 6.)
              if (form && form.querySelector('input[type="password"]')) return false;
              // A real submit control always commits.
              if (tag === 'INPUT' && (t === 'submit' || t === 'image')) return true;
              // Legacy apps commit from type="button" via a JS handler — see
              // ParaBank's <input type="button" value="Apply Now">. Being a
              // button INSIDE a form is the honest structural signal; the
              // markup gives us nothing better, and guessing from the label
              // is what we are trying to avoid.
              return !!el.closest('form');
            }
            """))
    except Exception:
        return False


def _candidates_for_ref(self, ref: str, description: str):
    """Inspect a live element and build the targeting ladder for the artifact:
    accessible role+name first, then label, then text/placeholder, then a
    structural CSS fallback. Captured at action time, before the page moves on.
    """
    from .schemas import LocatorCandidate, LocatorStrategy, Target

    info = self._ref_locator(ref).evaluate(
        """
        el => {
          const roleMap = {A:'link', BUTTON:'button', SELECT:'combobox',
                           TEXTAREA:'textbox'};
          let role = el.getAttribute('role') || roleMap[el.tagName] || null;
          if (el.tagName === 'INPUT') {
            role = {submit:'button', button:'button', checkbox:'checkbox',
                    radio:'radio'}[el.type] || 'textbox';
          }
          const label = el.labels && el.labels[0] ? el.labels[0].innerText.trim() : null;
          const row = el.closest('tr');
          let rowAnchor = null, cellCss = null, labelAdjacent = null;
          if (row) {
            const firstCell = row.querySelector('td,th');
            if (firstCell && firstCell !== el) {
              rowAnchor = firstCell.innerText.trim().slice(0, 40);
              const cells = Array.from(row.children);
              const myCell = el.closest('td,th');
              if (myCell) {
                const descend = (myCell !== el)
                  ? ' ' + el.tagName.toLowerCase() +
                    (el.name ? '[name="' + el.name + '"]' : '')
                  : '';
                // When the target sits INSIDE the cell (a form control in a
                // legacy layout table), the cell is not the target: descend
                // to the control, or replay tries to type into a <td>.
                cellCss = 'td:nth-child(' + (cells.indexOf(myCell) + 1) + ')' + descend;
                // A label/value pair is the dominant legacy table shape
                // ("Status:" | "Denied"). Anchoring on the label and taking
                // the NEXT cell fails differently from counting columns, so
                // the two together are a real ladder rather than one rung
                // twice: inserting a column breaks the count, not this.
                if (cells.indexOf(myCell) === cells.indexOf(firstCell) + 1) {
                  labelAdjacent = firstCell.innerText.trim().slice(0, 40) + '||' + descend;
                }
              }
            }
          }
          const attrs = [];
          if (el.id) attrs.push('#' + CSS.escape(el.id));
          if (el.name) attrs.push(el.tagName.toLowerCase() + `[name="${el.name}"]`);
          if (el.tagName === 'A' && el.getAttribute('href'))
            attrs.push(`a[href*="${el.getAttribute('href').split('?')[0].split('/').pop()}"]`);
          // innerText of a <select> is every option concatenated, which is
          // never its accessible name — recording it produces a junk rung
          // that no reviewer can make sense of.
          const textName = el.tagName === 'SELECT' ? '' : el.innerText;
          // `value` is the visible LABEL on a button ("Log In") but the
          // user's DATA on a text field. On a pre-filled form, naming a
          // field by its value records a locator that only matches while
          // the field still contains that value, and bakes record-time
          // data into the artifact.
          // `value` is a LABEL only on a button. On a text field it is the
          // user's data, and on a <select> it is whichever option happens to
          // be chosen — recording either produces a locator that finds the
          // control only while it still holds that value.
          const buttonish = el.tagName === 'INPUT' &&
                ['submit', 'button', 'reset'].indexOf(el.type) !== -1;
          const valueName = buttonish ? el.value : '';
          return {
            role: role,
            name: (el.getAttribute('aria-label') || textName ||
                   valueName || '').trim().slice(0, 60),
            label: label,
            placeholder: el.placeholder || null,
            css: attrs,
            rowAnchor: rowAnchor, cellCss: cellCss,
            labelAdjacent: labelAdjacent,
          };
        }
        """
    )
    cands = []
    if info["role"] and info["name"]:
        cands.append(LocatorCandidate(
            strategy=LocatorStrategy.ROLE_NAME, role=info["role"],
            value=info["name"],
            rationale="accessible role and name, most stable across restyling"))
    if info["label"]:
        cands.append(LocatorCandidate(
            strategy=LocatorStrategy.LABEL, value=info["label"],
            rationale="associated form label"))
    if info["placeholder"]:
        cands.append(LocatorCandidate(
            strategy=LocatorStrategy.PLACEHOLDER, value=info["placeholder"],
            rationale="input placeholder"))
    if info["rowAnchor"] and info["cellCss"]:
        cands.append(LocatorCandidate(
            strategy=LocatorStrategy.RELATIVE_TEXT,
            value=f'{info["rowAnchor"]}||{info["cellCss"]}',
            rationale="row anchored by first cell text, for id-less legacy tables"))
    if info.get("labelAdjacent"):
        label, _, descend = info["labelAdjacent"].partition("||")
        if label:
            escaped = label.replace('"', '\\"')
            cands.append(LocatorCandidate(
                strategy=LocatorStrategy.CSS,
                value=f'td:has-text("{escaped}") + td{descend}',
                rationale="the cell immediately after its label cell; unlike the "
                          "row-anchored rung this survives a column being inserted"))
    for css in info["css"]:
        cands.append(LocatorCandidate(
            strategy=LocatorStrategy.CSS, value=css,
            rationale="structural fallback"))
    if not cands and info["name"]:
        cands.append(LocatorCandidate(
            strategy=LocatorStrategy.TEXT, value=info["name"],
            rationale="visible text, last resort"))
    return Target(description=description, candidates=cands)


# How long to wait for an action to START changing the page, how long to
# wait for content to STOP arriving, and how often to look. Deliberately
# short: these are per-action costs paid on every step of every replay.
_CHANGE_GRACE_S = 1.5
_STABLE_WINDOW_S = 2.0
_SAMPLE_S = 0.12


def _page_signature(self):
    """Cheap fingerprint of what is currently on screen.

    URL alone misses a same-URL postback; element count alone misses a swap
    that happens to keep the count; text length catches content changes that
    keep the structure. Together they are enough to tell "the page moved"
    from "nothing happened yet", which is all settle() needs to know.
    """
    try:
        return tuple(self._page.evaluate(
            "() => [location.href, document.querySelectorAll('*').length,"
            " (document.body ? document.body.innerText.length : 0)]"))
    except Exception:
        return None


def _after_action(self, before) -> None:
    """Wait for the page to catch up with an action just performed.

    Lives on the driver rather than in the engines so that no caller has to
    remember it. Every primitive that can move the page routes through here,
    which is what keeps the guarantee uniform: discovery, replay, a future
    desktop driver and anything else get identical behaviour without
    re-deriving it.
    """
    _settle(self, since=before)


def _settle(self, timeout_ms: int = 6000, since=None) -> None:
    """Let a navigation or postback finish before the next observation.

    Load state alone is not enough, and on a legacy app it is actively
    misleading: ParaBank's transaction search reports "settled" 50ms after
    the click — because the OLD page is still loaded and the postback has
    not started — and renders its results table 300ms later. Anything that
    looks in between sees the previous page, concludes there are no results,
    and retries against a page that will never change.

    So when the caller knows what the page looked like beforehand, wait for
    it to actually differ, then for the load state, then for the DOM to stop
    growing. Bounded at each stage: a click that legitimately changes
    nothing costs a fraction of a second, not the full timeout.
    """
    if since is not None:
        deadline = time.time() + _CHANGE_GRACE_S
        while time.time() < deadline:
            if _page_signature(self) != since:
                break
            time.sleep(0.05)
    try:
        self._page.wait_for_load_state("domcontentloaded", timeout=timeout_ms)
        self._page.wait_for_load_state("networkidle", timeout=timeout_ms)
    except Exception:
        pass
    # Content can still be arriving after "networkidle" on a page that
    # rewrites itself; wait for two consecutive identical samples.
    try:
        deadline, previous = time.time() + _STABLE_WINDOW_S, None
        while time.time() < deadline:
            size = self._page.evaluate(
                "() => document.querySelectorAll('*').length")
            if size == previous:
                return
            previous = size
            time.sleep(_SAMPLE_S)
    except Exception:
        pass


PlaywrightDriver.settle = _settle
PlaywrightDriver.page_signature = _page_signature
PlaywrightDriver.after_action = _after_action

PlaywrightDriver.click_ref = _click_ref
PlaywrightDriver._ref_locator = _ref_locator
PlaywrightDriver.type_ref = _type_ref
PlaywrightDriver.select_ref = _select_ref
PlaywrightDriver.read_ref = _read_ref
PlaywrightDriver.candidates_for_ref = _candidates_for_ref
PlaywrightDriver.submits_ref = _submits_ref