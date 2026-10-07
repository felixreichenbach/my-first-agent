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

    def export_generations(self, request):
        self.generations.extend(request.generations)
        return super().export_generations(request)


@pytest.fixture
def active():
    exporter = CapturingExporter()
    client = Client(ClientConfig(generation_exporter=exporter, agent_name="my-first-agent", agent_version="9.9.9"))
    return ActiveObservability(client, "test", "9.9.9"), client, exporter


def test_disabled_without_endpoint(monkeypatch):
    monkeypatch.delenv("AGENTO11Y_ENDPOINT", raising=False)
    obs = setup_observability("cli")
    assert isinstance(obs, Observability) and not obs.enabled
    fake = FakeClient([text("hi")])
    assert obs.create_message(fake, {"messages": []}).content[0].text == "hi"
    with obs.tool_execution("t", "id") as rec:
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


def test_recording_failure_does_not_break_the_run(active, monkeypatch):
    obs, _, _ = active

    def explode(client, request, call, options=None):
        call(request)  # the model answers...
        raise Agento11yError("enqueue failed")  # ...but recording it fails

    monkeypatch.setattr("agento11y_anthropic.messages.create", explode)
    fake = FakeClient([text("still fine")])
    result = run_agent(fake, "task", echo_registry(), model="m", system="s", obs=obs)
    assert result.answer == "still fine"


def test_model_failure_still_propagates(active):
    import anthropic, httpx
    obs, _, _ = active
    err = anthropic.APIConnectionError(request=httpx.Request("POST", "http://x"))
    with pytest.raises(Exception) as info:
        run_agent(FakeClient([err]), "task", echo_registry(), model="m", system="s", obs=obs)
    assert "model API call failed" in str(info.value)


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
