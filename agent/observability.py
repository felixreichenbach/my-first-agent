"""Grafana Agent Observability wiring.

Off unless AGENTO11Y_ENDPOINT is set, in which case every method here is a plain
pass-through and the agent behaves exactly as it does without telemetry. When on,
a failure in recording is logged and swallowed: observability must never break a run.

Two independent channels, both configured only through environment variables:
  * generations, tool executions and workflow steps -> AGENTO11Y_* via the agento11y SDK
  * OTel traces and metrics                         -> OTEL_EXPORTER_OTLP_*

A run is a graph of workflow steps, which Grafana draws in the conversation's Workflow tab:

    decide(1) -> tool:calculator -> decide(2)

Each `decide` step is linked to the generation (model call) it wraps; each tool step has the
`decide` step that requested it as its parent, and the next `decide` has the tool steps as parents.
"""
from __future__ import annotations

import contextlib
import logging
import os
import sys
import uuid
from datetime import datetime, timezone
from typing import Any, Iterator

log = logging.getLogger("observability")

AGENT_NAME = "my-first-agent"
FRAMEWORK = "custom"  # a hand-written loop: no framework adapter fills the steps in for us


def _now() -> datetime:
    return datetime.now(timezone.utc)


class _NoopToolRecorder:
    def finish(self, arguments: dict[str, Any], output: str, is_error: bool) -> None:
        pass


class Run:
    """Disabled implementation: calls straight through, records nothing."""

    def create_message(self, client: Any, request: dict[str, Any], iteration: int) -> Any:
        return client.messages.create(**request)

    @contextlib.contextmanager
    def tool_execution(self, name: str, call_id: str) -> Iterator[_NoopToolRecorder]:
        yield _NoopToolRecorder()


class Observability:
    """Disabled implementation. `ActiveObservability` below does the real work."""

    enabled = False

    def start_run(self, conversation_id: str = "", model: str = "") -> Run:
        return Run()

    def shutdown(self) -> None:
        pass


class _RecorderCapturingClient:
    """Wraps the SDK client so we can read the generation id back from the recorder.

    The Anthropic wrapper only needs `start_generation`, and does not return the recorder or the
    generation id. We need the id to link a workflow step to the generation it contains.
    """

    def __init__(self, inner: Any) -> None:
        self._inner = inner
        self.recorder: Any = None

    def start_generation(self, start: Any) -> Any:
        self.recorder = self._inner.start_generation(start)
        return self.recorder

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


class _ToolRecorder:
    def __init__(self, recorder: Any, tool_end: Any) -> None:
        self._recorder, self._tool_end = recorder, tool_end
        self.is_error = False
        self.output_chars = 0

    def finish(self, arguments: dict[str, Any], output: str, is_error: bool) -> None:
        self.is_error, self.output_chars = is_error, len(output)
        try:
            if is_error:
                self._recorder.set_exec_error(RuntimeError(output))
            else:
                self._recorder.set_result(self._tool_end(arguments=arguments, result=output))
        except Exception as exc:  # telemetry must never break the run
            log.warning("observability: recording tool result failed: %s", exc)


class ActiveRun(Run):
    def __init__(self, obs: "ActiveObservability", conversation_id: str, model: str) -> None:
        self._obs, self._conversation_id, self._model = obs, conversation_id, model
        self._decide_step_id = ""            # the decide step whose tools are running now
        self._tool_step_ids: list[str] = []  # tool steps since that decide: parents of the next one

    def _enqueue_step(self, **fields: Any) -> str:
        from agento11y import WorkflowStep

        step_id = f"wfs_{uuid.uuid4().hex[:16]}"
        try:
            self._obs.client.enqueue_workflow_step(WorkflowStep(
                id=step_id, conversation_id=self._conversation_id, framework=FRAMEWORK,
                agent_name=AGENT_NAME, agent_version=self._obs.version,
                tags={"entrypoint": self._obs.entrypoint}, **fields,
            ))
        except Exception as exc:
            log.warning("observability: recording workflow step failed: %s", exc)
        return step_id

    def create_message(self, client: Any, request: dict[str, Any], iteration: int) -> Any:
        from agento11y_anthropic import AnthropicOptions
        from agento11y_anthropic import messages as anthropic_messages

        captured: dict[str, Any] = {}

        def call_model(req: dict[str, Any]) -> Any:
            captured["response"] = client.messages.create(**req)
            return captured["response"]

        proxy = _RecorderCapturingClient(self._obs.client)
        options = AnthropicOptions(
            conversation_id=self._conversation_id, agent_name=AGENT_NAME, agent_version=self._obs.version,
            tags={"entrypoint": self._obs.entrypoint},
        )
        started, error = _now(), ""
        try:
            return anthropic_messages.create(proxy, request, call_model, options)
        except Exception as exc:
            if "response" in captured:
                # The model answered; only recording failed (the SDK re-raises its own errors).
                log.warning("observability: recording generation failed: %s", exc)
                return captured["response"]
            error = type(exc).__name__  # the model call itself failed: let the loop handle it
            raise
        finally:
            self._record_decide_step(proxy, captured.get("response"), iteration, started, error)

    def _record_decide_step(self, proxy: _RecorderCapturingClient, response: Any, iteration: int,
                            started: datetime, error: str) -> None:
        generation_ids: list[str] = []
        try:
            generation = proxy.recorder.last_generation if proxy.recorder is not None else None
            if generation is not None and generation.id:
                generation_ids = [generation.id]
        except Exception as exc:
            log.warning("observability: could not read generation id: %s", exc)

        tool_calls = [b.name for b in getattr(response, "content", []) if getattr(b, "type", "") == "tool_use"]
        # Only metadata in the step state (no prompt or tool content): same default as the generations.
        self._decide_step_id = self._enqueue_step(
            step_name="decide", started_at=started, completed_at=_now(), error=error,
            input_state={"iteration": iteration},
            output_state={"stop_reason": str(getattr(response, "stop_reason", "")), "tool_calls": tool_calls},
            linked_generation_ids=generation_ids, parent_step_ids=list(self._tool_step_ids),
        )
        self._tool_step_ids = []

    @contextlib.contextmanager
    def tool_execution(self, name: str, call_id: str) -> Iterator[Any]:
        from agento11y import ToolExecutionEnd, ToolExecutionStart

        started = _now()
        try:
            recorder = self._obs.client.start_tool_execution(ToolExecutionStart(
                tool_name=name, tool_call_id=call_id, conversation_id=self._conversation_id,
                agent_name=AGENT_NAME, agent_version=self._obs.version,
                request_model=self._model, request_provider="anthropic",
            ))
        except Exception as exc:
            log.warning("observability: starting tool execution failed: %s", exc)
            recorder = None
        tool = _ToolRecorder(recorder, ToolExecutionEnd) if recorder is not None else _NoopToolRecorder()
        try:
            yield tool
        finally:
            if recorder is not None:
                try:
                    recorder.end()
                except Exception as exc:
                    log.warning("observability: ending tool execution failed: %s", exc)
            is_error = getattr(tool, "is_error", False)
            self._tool_step_ids.append(self._enqueue_step(
                step_name=f"tool:{name}", started_at=started, completed_at=_now(),
                error="tool returned an error" if is_error else "",
                input_state={"tool": name},
                output_state={"is_error": is_error, "output_chars": getattr(tool, "output_chars", 0)},
                parent_step_ids=[self._decide_step_id] if self._decide_step_id else [],
            ))


class ActiveObservability(Observability):
    enabled = True

    def __init__(self, client: Any, entrypoint: str, version: str, providers: tuple[Any, ...] = ()) -> None:
        self.client, self.entrypoint, self.version = client, entrypoint, version
        self._providers = providers

    def start_run(self, conversation_id: str = "", model: str = "") -> Run:
        return ActiveRun(self, conversation_id or str(uuid.uuid4()), model)

    def shutdown(self) -> None:
        """Flush queued generations and steps, then the OTel providers, before the process exits."""
        try:
            self.client.shutdown()
        except Exception as exc:
            log.warning("observability: client shutdown failed: %s", exc)
        for provider in self._providers:
            try:
                provider.force_flush()
                provider.shutdown()
            except Exception as exc:
                log.warning("observability: provider shutdown failed: %s", exc)


def normalize_otlp_headers(value: str) -> str:
    """Accept OTEL_EXPORTER_OTLP_HEADERS as Grafana's UI prints it.

    The UI gives `Authorization=Basic <base64>` with a literal space, but the OTel SDK requires
    URL-encoded values and silently drops a header containing a raw space (the OTLP export then
    fails with a 401). Encode spaces so the value can be pasted unchanged.
    """
    pairs = []
    for pair in value.strip().strip("'\"").split(","):
        key, sep, val = pair.partition("=")
        pairs.append(f"{key.strip()}{sep}{val.strip().replace(' ', '%20')}")
    return ",".join(pairs)


def setup_observability(entrypoint: str) -> Observability:
    """Return an active instance if AGENTO11Y_ENDPOINT is set, else a disabled one."""
    if not os.getenv("AGENTO11Y_ENDPOINT"):
        print("observability: disabled (set AGENTO11Y_ENDPOINT to enable)", file=sys.stderr)
        return Observability()

    if os.getenv("OTEL_EXPORTER_OTLP_HEADERS"):
        os.environ["OTEL_EXPORTER_OTLP_HEADERS"] = normalize_otlp_headers(os.environ["OTEL_EXPORTER_OTLP_HEADERS"])

    try:
        from agento11y import Client, ClientConfig
        from opentelemetry import metrics, trace
        from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
        from opentelemetry.sdk.metrics import MeterProvider
        from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor

        # Without these providers the SDK's spans and metrics go to a no-op and are lost silently.
        # Both must exist before the Client is built. Endpoint and auth come from OTEL_EXPORTER_OTLP_*.
        resource = Resource.create({"service.name": os.getenv("OTEL_SERVICE_NAME", AGENT_NAME)})
        tracer_provider = TracerProvider(resource=resource)
        tracer_provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
        trace.set_tracer_provider(tracer_provider)
        meter_provider = MeterProvider(resource=resource,
                                       metric_readers=[PeriodicExportingMetricReader(OTLPMetricExporter())])
        metrics.set_meter_provider(meter_provider)

        version = os.getenv("AGENT_VERSION", "0.1.0")
        client = Client(ClientConfig(agent_name=AGENT_NAME, agent_version=version,
                                     tags={"entrypoint": entrypoint}))
    except Exception as exc:  # a misconfiguration must not stop the agent from running
        print(f"observability: disabled, setup failed: {exc}", file=sys.stderr)
        return Observability()

    print(f"observability: enabled ({entrypoint}, version {version})", file=sys.stderr)
    return ActiveObservability(client, entrypoint, version, (tracer_provider, meter_provider))
