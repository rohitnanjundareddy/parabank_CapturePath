"""Artifact store: versioned JSON files on disk, one per id@version.

JSON on disk (rather than a database) keeps artifacts diffable in git and
reviewable in a pull request, which is what the draft -> approved gate wants.
The reference structure across files (capabilities -> fragments) is a graph;
`dependencies()` walks it.
"""

from __future__ import annotations

import os

from .schemas import Artifact, ApprovalStatus, RunSubflowStep
from .textio import read_text, write_text


class ArtifactStore:
    def __init__(self, base_dir: str = "artifacts"):
        self.base_dir = base_dir
        os.makedirs(base_dir, exist_ok=True)

    def _path(self, ref: str) -> str:
        return os.path.join(self.base_dir, f"{ref.replace('@', '__')}.json")

    def save(self, artifact: Artifact) -> str:
        path = self._path(artifact.ref)
        # Through textio, never bare open(): an artifact is a committed file
        # that another machine has to replay, so it is written UTF-8 + LF on
        # every platform rather than in whatever the local locale prefers.
        write_text(path, artifact.model_dump_json(indent=2))
        return path

    def load(self, ref: str) -> Artifact:
        path = self._path(ref)
        if not os.path.exists(path):
            raise FileNotFoundError(f"no artifact '{ref}' in {self.base_dir}")
        return Artifact.model_validate_json(read_text(path))

    def list(self) -> list[Artifact]:
        out = []
        for fn in sorted(os.listdir(self.base_dir)):
            if fn.endswith(".json"):
                out.append(Artifact.model_validate_json(
                    read_text(os.path.join(self.base_dir, fn))))
        return out

    def approve(self, ref: str, force: bool = False) -> Artifact:
        """Promote a reviewed draft. Refuses while the recording is known not
        to match its approved plan, because approving a capability whose
        declared inputs do nothing is how a broken flow reaches production."""
        art = self.load(ref)
        smoke = art.provenance.smoke_replay or ""
        if smoke.startswith(("hard_failure", "error")) and not force:
            raise PermissionError(
                f"{ref} failed its smoke replay ({smoke}). The recording does "
                f"not actually replay; fix it (and clear "
                f"provenance.smoke_replay), or approve with --force.")
        problems = art.provenance.verification_problems
        if problems and not force:
            raise PermissionError(
                "recording does not match the approved plan:\n  - "
                + "\n  - ".join(problems)
                + "\nFix the draft (and clear provenance.verification_problems), "
                  "or approve with --force if you accept these gaps.")
        art.status = ApprovalStatus.APPROVED
        self.save(art)
        return art

    def dependencies(self, ref: str) -> list[str]:
        """Direct fragment refs a capability composes. One edge type of the
        capability graph; at production scale this walk becomes a graph query."""
        art = self.load(ref)
        return [s.ref for s in art.steps if isinstance(s, RunSubflowStep)]

    def print_graph(self, ref: str, indent: int = 0) -> None:
        art = self.load(ref)
        print("  " * indent + f"{art.ref} [{art.kind.value}, {art.status.value}]")
        for dep in self.dependencies(ref):
            self.print_graph(dep, indent + 1)
