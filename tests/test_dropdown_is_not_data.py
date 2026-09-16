"""A dropdown holds choices, not results.

Reading a <select> returns every option concatenated, which looks like tabular
data and is nothing of the sort. An account picker reads back as a long list of
account numbers -- indistinguishable, from a distance, from a transaction
table. Recording that produces a capability whose "read the transactions" step
returns the contents of a picker and reports success, and nothing downstream
catches it: the value is non-empty, it parses, and it even satisfies the
collection heuristic that decides what an empty read means.

Three separate attempts to stop this failed before the driver refused outright:
naming the control better in the digest, telling the model which element to
read in the goal, and printing what was read at the end of a run. The first two
are advice the model can ignore; the third only helps a human who is looking.
"""

import pytest

from cua.driver import NotReadableAsData, _read_ref


class Locator:
    def __init__(self, tag, text="", selected="13344"):
        self.tag = tag
        self.text = text
        self.selected = selected

    def evaluate(self, js):
        if "tagName" in js:
            return self.tag
        if "selectedOptions" in js:
            return self.selected
        raise AssertionError("unexpected evaluate: " + js)

    def inner_text(self):
        return self.text

    def input_value(self):
        return self.text


class Driver:
    """Just enough to exercise the module-level _read_ref."""

    def __init__(self, locator):
        self._locator = locator

    def _ref_locator(self, ref):
        return self._locator


def read(locator):
    return _read_ref(Driver(locator), "e1")


class TestADropdownIsRefused:

    def test_reading_a_select_raises(self):
        options = "\n".join(["12456", "12567", "12678", "13344"])
        with pytest.raises(NotReadableAsData):
            read(Locator("SELECT", text=options))

    def test_the_message_says_what_to_do_instead(self):
        with pytest.raises(NotReadableAsData) as exc:
            read(Locator("SELECT", text="12456\n12567"))
        message = str(exc.value)
        assert "dropdown" in message
        assert "results area" in message

    def test_the_message_names_what_is_selected(self):
        """Useful when the goal really did want the chosen value: it is right
        there in the error rather than requiring another round trip."""
        with pytest.raises(NotReadableAsData) as exc:
            read(Locator("SELECT", text="a\nb", selected="13344"))
        assert "13344" in str(exc.value)


class TestEverythingElseStillReads:

    def test_a_table_reads_normally(self):
        table = "Date\tAmount\n09-04-2026\t$100.00"
        assert read(Locator("TABLE", text=table)) == table

    def test_an_input_reads_its_value(self):
        assert read(Locator("INPUT", text="  100  ")) == "100"

    def test_a_heading_reads_its_text(self):
        assert read(Locator("H1", text=" Transaction Results ")) \
            == "Transaction Results"

    def test_whitespace_is_stripped(self):
        assert read(Locator("TD", text="\n  $1,234.56  \n")) == "$1,234.56"


class TestARecordingDefectIsNeverAnAnswer:
    """The worst outcome available: a capability whose confirmation step reads
    a dropdown refuses to read it, the refusal falls through to the step's
    declared `on_exhausted`, and the caller is told ACCOUNT_OPEN_FAILED -- for
    an account that was just opened. A defect in the artifact is a fact about
    the artifact, never a fact about the world.
    """

    def _engine(self, tmp_path, artifact, driver):
        from cua.evidence import EvidenceLog
        from cua.redaction import Redactor
        from cua.replay import ReplayEngine
        from cua.store import ArtifactStore
        from .test_replay import POLICY
        store = ArtifactStore(str(tmp_path / "artifacts"))
        ev = EvidenceLog(str(tmp_path / "evidence"), "replay", Redactor())
        store.save(artifact)
        return ReplayEngine(driver, POLICY, store, ev)

    def _artifact(self):
        from cua.schemas import Artifact
        return Artifact.model_validate({
            "kind": "capability", "id": "open", "version": "1.0.0",
            "name": "Open", "description": "d", "app": "parabank",
            "status": "approved",
            "outputs": [{"name": "new_id", "type": "string", "description": "d"}],
            "business_outcomes": [{"code": "OPEN_FAILED", "description": "x"}],
            "steps": [
                {"id": "s1", "action": "navigate", "description": "open",
                 "url": "http://localhost/overview"},
                {"id": "s2", "action": "extract", "description": "the new number",
                 "output": "new_id", "parse": "text",
                 "target": {"description": "confirmation",
                            "candidates": [{"strategy": "css", "value": "#type"}]},
                 "on_exhausted": {"type": "business_outcome", "code": "OPEN_FAILED",
                                  "message": "no account number found"}},
            ],
            "success": {"id": "c", "description": "shown",
                        "match": {"kind": "text_visible", "value": "Account Overview"}},
        })

    def test_it_fails_instead_of_claiming_the_action_failed(self, tmp_path):
        from cua.driver import NotReadableAsData
        from cua.schemas import ReplayStatus
        from .test_replay import FakeDriver

        class Driver(FakeDriver):
            def read_text(self, target):
                raise NotReadableAsData("that is a dropdown, not a result")

        d = Driver()
        d.texts_visible.add("Account Overview")
        r = self._engine(tmp_path, self._artifact(), d).run("open@1.0.0", {})
        assert r.outcome_code != "OPEN_FAILED", \
            "the account may well have been opened; the recording is what broke"
        assert r.status == ReplayStatus.HARD_FAILURE
        assert "dropdown" in (r.observed or "")
