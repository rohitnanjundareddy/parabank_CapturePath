"""Where a question to the operator goes.

Discovery is not a batch job: it stops to have a plan approved, to confirm a
state-changing action, to ask for a value nobody supplied, and to hand over the
browser. Each of those is a question that needs an answer before the run can
continue. The terminal is one surface for asking; a chat window is another.

Rather than thread a prompter object through the planner, the recorder, the
escalation controller and the agent loop, the current prompter lives here. It
is per-thread, so a UI can install its own for the duration of one run without
any of those modules knowing a UI exists — and without a background run
stealing the terminal's stdin from another.
"""

from __future__ import annotations

import builtins
import getpass
import threading
from contextlib import contextmanager
from typing import Callable, Optional

# Prompter signature: (question, sensitive) -> answer
Prompter = Callable[[str, bool], str]

_local = threading.local()


def ask(question: str, sensitive: bool = False) -> str:
    """Put a question to whoever is operating this run."""
    prompter: Optional[Prompter] = getattr(_local, "prompter", None)
    if prompter is None:
        return getpass.getpass(question) if sensitive else builtins.input(question)
    return prompter(question, sensitive)


@contextmanager
def using(prompter: Prompter):
    """Route questions somewhere else for the duration of a block."""
    previous = getattr(_local, "prompter", None)
    _local.prompter = prompter
    try:
        yield
    finally:
        _local.prompter = previous
