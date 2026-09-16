"""Escalation: bring a human into the loop on the SAME live session.

State machine: RUNNING -> PAUSED_FOR_HUMAN -> HUMAN_IN_CONTROL -> RESUMING.
Ownership is enforced at the driver (automation raises if it acts while a
human holds the session), the human works in the already-open headed browser,
their actions are recorded into evidence, and control returns only when they
signal done. The operator surface here is a deliberate mock (the terminal);
the handoff mechanism underneath it is real, and a web console would sit on
the same four calls: intervene, act, done, confirm.
"""

from __future__ import annotations

from typing import Optional

from . import prompt


class EscalationController:
    def __init__(self, interactive: bool = True):
        self.interactive = interactive
        # What the human actually did during the last handoff. The note says
        # what they meant; this is what the page saw. Kept here rather than
        # returned so the signature stays a string for existing callers.
        self.last_actions: list[dict] = []

    def intervene(self, *, context: str, goal: str, step: str,
                  driver, evidence) -> str:
        shot = evidence.screenshot_path("intervention")
        driver.observe(screenshot_to=shot)
        evidence.event("intervention_raised", context=context, goal=goal,
                       step=step, url=driver.current_url(), screenshot=shot)

        print("\n" + "=" * 62)
        print("HUMAN INTERVENTION REQUESTED")
        print(f"  Goal:    {goal}")
        print(f"  Step:    {step}")
        print(f"  Why:     {context}")
        print(f"  Where:   {driver.current_url()}")
        print(f"  Evidence screenshot: {shot}")
        print("  The live browser window is now yours. Perform the needed")
        print("  steps manually, then return here.")
        print("=" * 62)

        driver.cede_control()
        evidence.event("control_transferred", to="human")

        if self.interactive:
            note = prompt.ask("When finished, describe what you did and press "
                              "Enter to hand control back: ").strip() or "resolved"
        else:
            note = "non-interactive mode: auto-resumed"

        human_actions = driver.drain_human_actions()
        self.last_actions = human_actions
        driver.resume_control()
        evidence.event("control_transferred", to="automation",
                       human_note=note, human_actions=human_actions)
        print("Control returned to automation.\n")
        return note

    def confirm(self, question: str, committing: Optional[dict] = None) -> bool:
        """Ask before a state-changing action. `committing` is what the step
        is about to submit — a button label alone is not informed consent,
        and the operator approving a loan needs to see the amount and the
        account, not the word 'Apply'."""
        if not self.interactive:
            return False  # conservative default when unattended
        print("\n" + "=" * 62)
        print("CONFIRM STATE-CHANGING ACTION")
        print(f"  {question}")
        if committing:
            print("  About to submit:")
            for k, v in committing.items():
                print(f"    {k} = {v}")
        print("=" * 62)
        return prompt.ask("Proceed? [y/N]: ").strip().lower() == "y"
