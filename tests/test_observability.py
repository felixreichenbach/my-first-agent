"""Observability tests: no network, a capturing exporter stands in for Grafana."""
import pytest
from agento11y import Client, ClientConfig
from agento11y.client import NoopGenerationExporter
from agento11y.errors import Agento11yError

import observability
from loop import run_agent
from observability import ActiveObservability, Observability, setup_observability
from test_loop import FakeClient, echo_registry, text, tool_use


class CapturingExporter(NoopGenerationExporter):
    def __init__(self):
        super().__init__()
        self.generations = []
        self.steps = []

    def export_generations(self, request):
        self.generations.extend(request.generations)
        return super().export_generations(request)

    def export_workflow_steps(self, request):
        self.steps.extend(request.workflow_steps)
        return super().export_workflow_steps(request)


@pytest.fixture
def active():
    exporter = CapturingExporter()
    client = Client(ClientConfig(generation_exporter=exporter, agent_name="my-first-agent", agent_version="9.9.9"))
    return ActiveObservability(client, "test", "9.9.9"), client, exporter


def test_disabled_without_endpoint(monkeypatch):
    monkeypatch.delenv("AGENTO11Y_ENDPOINT", raising=False)
    obs = setup_observability("cli")
    assert isinstance(obs, Observability) and not obs.enabled
    run = obs.start_run("conv", "model")
    fake = FakeClient([text("hi")])
    assert run.create_message(fake, {"messages": []}, 1).content[0].text == "hi"
    with run.tool_execution("t", "id") as rec:
        rec.finish({}, "out", False)  # no-op, must not raise
    obs.shutdown()


def test_setup_failure_falls_back_to_disabled(monkeypatch, capsys):
    monkeypatch.setenv("AGENTO11Y_ENDPOINT", "https://example.invalid")
    monkeypatch.setenv("OTEL_SDK_DISABLED", "true")  # no real exporters in a unit test
    monkeypatch.setattr("agento11y.Client", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    obs = setup_observability("cli")
    assert not obs.enabled and "setup failed" in capsys.readouterr().err


def test_full_run_is_recorded(active):
    obs, client, exporter = active
    fake = FakeClient([tool_use("echo", {"text": "hi"}), text("done")])
    result = run_agent(fake, "task", echo_registry(), model="fake-model", system="s",
                       obs=obs, conversation_id="conv-1")
    assert result.answer == "done" and result.finished
    client.flush()
    assert len(exporter.generations) == 2          # one generation per model call
    gen = exporter.generations[0]
    assert gen.conversation_id == "conv-1"
    assert gen.agent_name == "my-first-agent" and gen.agent_version == "9.9.9"
    assert gen.model.name == "fake-model"
    assert gen.usage.input_tokens == 10 and gen.usage.output_tokens == 5


def test_workflow_steps_form_a_linked_graph(active):
    """decide(1) -> tool:echo -> decide(2), each decide linked to its generation."""
    obs, client, exporter = active
    fake = FakeClient([tool_use("echo", {"text": "hi"}), text("done")])
    run_agent(fake, "task", echo_registry(), model="m", system="s", obs=obs, conversation_id="conv-2")
    client.flush()

    by_name = list(exporter.steps)
    assert [s.step_name for s in by_name] == ["decide", "tool:echo", "decide"]
    decide1, tool, decide2 = by_name

    assert decide1.parent_step_ids == []
    assert tool.parent_step_ids == [decide1.id]            # the tool runs because decide(1) asked for it
    assert decide2.parent_step_ids == [tool.id]            # the next decision follows the tool result
    assert all(s.conversation_id == "conv-2" and s.framework == "custom" for s in by_name)

    generation_ids = [g.id for g in exporter.generations]
    assert decide1.linked_generation_ids == [generation_ids[0]]
    assert decide2.linked_generation_ids == [generation_ids[1]]
    assert decide1.output_state["tool_calls"] == ["echo"] and decide1.output_state["stop_reason"] == "tool_use"
    assert decide2.output_state["tool_calls"] == [] and decide2.output_state["stop_reason"] == "end_turn"
    assert tool.output_state == {"is_error": False, "output_chars": len("HI")}


def test_parallel_tools_fan_in_to_the_next_decision(active):
    from anthropic.types import ToolUseBlock
    from test_loop import msg

    obs, client, exporter = active
    two_tools = msg([ToolUseBlock(type="tool_use", id="a", name="echo", input={"text": "1"}),
                     ToolUseBlock(type="tool_use", id="b", name="echo", input={"text": "2"})], "tool_use")
    run_agent(FakeClient([two_tools, text("done")]), "task", echo_registry(), model="m", system="s", obs=obs)
    client.flush()
    decide1, tool_a, tool_b, decide2 = exporter.steps
    assert tool_a.parent_step_ids == tool_b.parent_step_ids == [decide1.id]
    assert sorted(decide2.parent_step_ids) == sorted([tool_a.id, tool_b.id])


def test_tool_error_is_marked_on_the_step(active):
    from tools import ToolError
    obs, client, exporter = active

    def boom(text):
        raise ToolError("secret detail")

    run_agent(FakeClient([tool_use("echo", {"text": "x"}), text("ok")]), "task", echo_registry(boom),
              model="m", system="s", obs=obs)
    client.flush()
    tool = [s for s in exporter.steps if s.step_name == "tool:echo"][0]
    assert tool.error == "tool returned an error"
    assert "secret detail" not in str(tool.output_state) + tool.error   # no tool content in step state


def test_recording_failure_does_not_break_the_run(active, monkeypatch):
    obs, _, _ = active

    def explode(client, request, call, options=None):
        call(request)  # the model answers...
        raise Agento11yError("enqueue failed")  # ...but recording it fails

    monkeypatch.setattr("agento11y_anthropic.messages.create", explode)
    result = run_agent(FakeClient([text("still fine")]), "task", echo_registry(), model="m", system="s", obs=obs)
    assert result.answer == "still fine"


def test_workflow_step_failure_does_not_break_the_run(active, monkeypatch):
    obs, client, _ = active
    monkeypatch.setattr(client, "enqueue_workflow_step", lambda step: (_ for _ in ()).throw(RuntimeError("nope")))
    result = run_agent(FakeClient([tool_use("echo", {"text": "x"}), text("fine")]), "task", echo_registry(),
                       model="m", system="s", obs=obs)
    assert result.answer == "fine"


def test_model_failure_still_propagates_and_is_recorded(active):
    import anthropic, httpx
    obs, client, exporter = active
    err = anthropic.APIConnectionError(request=httpx.Request("POST", "http://x"))
    with pytest.raises(Exception) as info:
        run_agent(FakeClient([err]), "task", echo_registry(), model="m", system="s", obs=obs)
    assert "model API call failed" in str(info.value)
    client.flush()
    assert [s.error for s in exporter.steps] == ["APIConnectionError"]


def test_unconfigured_module_is_importable():
    assert observability.AGENT_NAME == "my-first-agent"


@pytest.mark.parametrize("raw,expected", [
    ("Authorization=Basic MTIzNDo0NTY=", "Authorization=Basic%20MTIzNDo0NTY="),   # as printed by the Grafana UI
    ("'Authorization=Basic MTIzNDo0NTY='", "Authorization=Basic%20MTIzNDo0NTY="),  # quotes left in by a shell
    ("Authorization=Basic%20MTIzNDo0NTY=", "Authorization=Basic%20MTIzNDo0NTY="),  # already encoded: unchanged
    ("a=b c, d=e", "a=b%20c,d=e"),
])
def test_otlp_headers_are_normalized(raw, expected):
    assert observability.normalize_otlp_headers(raw) == expected


def test_normalized_header_is_accepted_by_the_otel_sdk():
    from opentelemetry.util.re import parse_env_headers
    headers = parse_env_headers(observability.normalize_otlp_headers("Authorization=Basic MTIzNDo0NTY="))
    assert headers == {"authorization": "Basic MTIzNDo0NTY="}
