import json
import threading

import pytest

from sol_control_hud.chains.llm import ChatResult, LLMError
from sol_control_hud.chains.runner import Runner, run_checks, StepFailure
from sol_control_hud.chains.store import Store
from sol_control_hud.chains.workflow import WorkflowError, parse, render

SUMMARY_WF = {
    "name": "summarize",
    "inputs": ["transcript"],
    "steps": [{
        "id": "summary", "model": "worker", "escalate_to": "specialist", "retries": 1,
        "prompt": "Summarize: {{ inputs.transcript }}",
        "schema": {"type": "object", "required": ["bullets", "action"],
                   "properties": {"bullets": {"type": "array", "items": {"type": "string"}}, "action": {"type": "string"}}},
        "checks": [{"path": "bullets", "min_items": 2}, {"path": "action", "contains": "fuel"}],
    }, {
        "id": "title", "model": "worker", "prompt": "Title for: {{ steps.summary.output.action }}",
    }],
}


class FakeClient:
    """Returns scripted answers per model, records calls."""

    def __init__(self, script: dict[str, list]):
        self.script = {k: list(v) for k, v in script.items()}
        self.calls: list[tuple[str, list]] = []

    def chat(self, model, messages, **kw):
        self.calls.append((model, messages))
        item = self.script[model].pop(0)
        if isinstance(item, Exception):
            raise item
        if isinstance(item, ChatResult):
            return item
        return ChatResult(content=item if isinstance(item, str) else json.dumps(item), finish_reason="stop", completion_tokens=10)


@pytest.fixture
def store(tmp_path):
    return Store(tmp_path / "runs.sqlite")


GOOD = {"bullets": ["a", "b"], "action": "renegotiate the fuel contract"}


def test_success_passes_outputs_between_steps(store):
    client = FakeClient({"worker": [GOOD, "Fuel contract plan"]})
    run_id = Runner(client, store).run(parse(SUMMARY_WF), {"transcript": "..."})
    run = store.run(run_id)
    assert run["status"] == "succeeded"
    assert json.loads(run["outputs"])["title"] == "Fuel contract plan"
    assert "renegotiate the fuel contract" in client.calls[1][1][0]["content"]  # step 2 saw step 1's output


def test_bad_json_is_retried_with_feedback(store):
    client = FakeClient({"worker": ["not json", GOOD, "t"]})
    run_id = Runner(client, store).run(parse(SUMMARY_WF), {"transcript": "..."})
    assert store.run(run_id)["status"] == "succeeded"
    retry_messages = client.calls[1][1]
    assert "rejected" in retry_messages[-1]["content"] and "JSON" in retry_messages[-1]["content"]


def test_failed_check_escalates_then_succeeds(store):
    wrong = {"bullets": ["a", "b"], "action": "book a room"}
    client = FakeClient({"worker": [wrong, wrong, "t"], "specialist": [GOOD]})
    run_id = Runner(client, store).run(parse(SUMMARY_WF), {"transcript": "..."})
    assert store.run(run_id)["status"] == "succeeded"
    kinds = [e["kind"] for e in store.events(run_id)]
    assert "escalated" in kinds


def test_still_failing_after_escalation_stops_and_asks_user(store):
    wrong = {"bullets": ["a"], "action": "fuel"}
    client = FakeClient({"worker": [wrong, wrong], "specialist": [wrong]})
    run_id = Runner(client, store).run(parse(SUMMARY_WF), {"transcript": "..."})
    run = store.run(run_id)
    assert run["status"] == "needs_user" and "at least 2 items" in run["error"]
    assert len(client.calls) == 3  # bounded: no endless loop


def test_ran_out_of_tokens_is_reported_as_such(store):
    cut = ChatResult(content='{"bul', finish_reason="length", completion_tokens=8192)
    client = FakeClient({"worker": [cut, cut], "specialist": [cut]})
    run_id = Runner(client, store).run(parse(SUMMARY_WF), {"transcript": "..."})
    assert "ran out of tokens" in store.run(run_id)["error"]


def test_engine_down_marks_run_failed_not_stuck(store):
    client = FakeClient({"worker": [LLMError("ConnectError: engine down")]})
    run_id = Runner(client, store).run(parse(SUMMARY_WF), {"transcript": "..."})
    run = store.run(run_id)
    assert run["status"] == "failed" and "engine down" in run["error"] and run["finished_at"]


def test_cancel_stops_before_next_call(store):
    runner = Runner(None, store)

    class CancellingClient(FakeClient):
        def chat(self, model, messages, **kw):
            runner.cancel()
            return ChatResult(content="not json", finish_reason="stop")

    runner.client = CancellingClient({})
    run_id = runner.run(parse(SUMMARY_WF), {"transcript": "..."})
    assert store.run(run_id)["status"] == "cancelled"


def test_restart_marks_active_runs_interrupted_and_never_resumes(tmp_path):
    s1 = Store(tmp_path / "r.sqlite")
    rid = s1.create_run("summarize", {})
    s1.set_status(rid, "running")
    s2 = Store(tmp_path / "r.sqlite")  # simulated process restart
    assert s2.recover_after_restart() == 1
    assert s2.run(rid)["status"] == "interrupted"


def test_missing_input_fails_immediately(store):
    client = FakeClient({"worker": []})
    run_id = Runner(client, store).run(parse(SUMMARY_WF), {})
    assert store.run(run_id)["status"] == "failed" and not client.calls


@pytest.mark.parametrize("bad, msg", [
    ({"name": "x", "steps": []}, "at least one step"),
    ({"name": "x", "steps": [{"id": "a", "model": "m"}]}, "missing 'prompt'"),
    ({"name": "x", "inputs": [], "steps": [{"id": "a", "model": "m", "prompt": "{{ inputs.nope }}"}]}, "undeclared input"),
    ({"name": "x", "steps": [{"id": "a", "model": "m", "prompt": "{{ steps.b.output }}"}]}, "before it runs"),
    ({"name": "x", "steps": [{"id": "a", "model": "m", "prompt": "p", "schema": {"type": "nonsense"}}]}, "invalid schema"),
    ({"name": "x", "steps": [{"id": "a", "model": "m", "prompt": "p", "checks": [{"min_items": 1}]}]}, "invalid"),
])
def test_workflow_validation_rejects_bad_files(bad, msg):
    with pytest.raises(WorkflowError, match=msg):
        parse(bad)


def test_empty_path_checks_whole_text_output():
    run_checks("# Note\nbody", [{"path": "", "min_length": 1}, {"path": "", "matches": "^# "}])
    with pytest.raises(StepFailure):
        run_checks("", [{"path": "", "min_length": 1}])


def test_example_workflow_file_is_valid():
    from pathlib import Path
    from sol_control_hud.chains.workflow import load
    wf = load(Path(__file__).parent.parent / "workflows" / "transcript-note.yaml")
    assert [s.id for s in wf.steps] == ["transcribe", "summary", "note"]


def test_render_and_checks():
    assert render("Hi {{ inputs.name }}", {"inputs": {"name": "Sol"}}) == "Hi Sol"
    with pytest.raises(StepFailure):
        run_checks({"a": "x"}, [{"path": "a", "matches": r"^\d+$"}])
