"""The fragment library: small, proven chunks a new capability can reuse.

Why this exists. A fourteen-step discovery gives the model fourteen chances to
choose badly; a five-step one gives it five. Most of those steps are not novel
— logging in, reaching a section — and rediscovering them each time is where
long flows become unreliable.

So: record the common parts ONCE as fragments, prove them by replaying them,
and thereafter REPLAY them deterministically at the start of a discovery run.
The model then explores only the genuinely new part, against a session already
in a known state.

Two properties worth keeping straight:

  * The planner is shown the catalog and may CHOOSE to reuse a fragment. That
    choice is a proposal, reviewed by a human like the rest of the plan.
  * Replay never chooses. The capability records the fragment by pinned
    id@version, and flatten() dereferences it mechanically. Selection happens
    at plan time; execution only dereferences.

That boundary is what keeps composition from smuggling a decision-maker back
into the production path.
"""

from __future__ import annotations

from .schemas import ApprovalStatus, Artifact, ArtifactKind


def approved_fragments(store, app: str | None = None) -> list[Artifact]:
    """Fragments eligible for reuse: approved only, optionally one app.

    A draft fragment is excluded on purpose. Reusing an unreviewed chunk in a
    new capability would let one unverified recording silently become part of
    many capabilities.
    """
    out = []
    for art in store.list():
        if art.kind != ArtifactKind.FRAGMENT:
            continue
        if art.status != ApprovalStatus.APPROVED:
            continue
        if app and art.app != app:
            continue
        out.append(art)
    return out


def render_catalog(fragments: list[Artifact]) -> str:
    """What the planner sees. Signatures only: names, what each chunk does,
    what it needs, and what state it leaves the application in."""
    if not fragments:
        return "(no reusable fragments recorded yet)"
    lines = []
    for f in fragments:
        ins = ", ".join(f"{i.name}:{i.type.value}"
                        f"{'*' if i.sensitive else ''}" for i in f.inputs) or "none"
        outs = ", ".join(o.name for o in f.outputs) or "none"
        lines.append(
            f"  {f.ref}\n"
            f"      does:    {f.description}\n"
            f"      needs:   {ins}\n"
            f"      returns: {outs}\n"
            f"      leaves the app at: {f.success.match.value}")
    return "\n".join(lines)


def resolve_reuse(store, refs: list[str], app: str) -> tuple[list[Artifact], list[str]]:
    """Validate the fragments a plan proposes to reuse.

    Returns (usable, complaints). A ref that does not exist, is not a
    fragment, is not approved, or belongs to another application is dropped
    with an explanation rather than silently ignored: a plan that claims to
    reuse login and then does not is worse than one that never claimed it.
    """
    usable: list[Artifact] = []
    complaints: list[str] = []
    for ref in refs or []:
        if "@" not in ref:
            complaints.append(f"'{ref}' does not pin a version; ignored")
            continue
        try:
            frag = store.load(ref)
        except FileNotFoundError:
            complaints.append(f"'{ref}' is not in the store; ignored")
            continue
        if frag.kind != ArtifactKind.FRAGMENT:
            complaints.append(f"'{ref}' is a capability, not a fragment; ignored")
        elif frag.status != ApprovalStatus.APPROVED:
            complaints.append(f"'{ref}' is still a draft; ignored")
        elif frag.app != app:
            complaints.append(f"'{ref}' belongs to app '{frag.app}'; ignored")
        else:
            usable.append(frag)
    return usable, complaints


def missing_inputs(fragment: Artifact, available: dict) -> list[str]:
    """Inputs a fragment needs that the caller has not supplied."""
    return [i.name for i in fragment.inputs
            if i.required and i.name not in available]
