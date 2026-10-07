"""Grafana Agent Observability wiring.

Off unless AGENTO11Y_ENDPOINT is set, in which case every method here is a plain
pass-through and the agent behaves exactly as it does without telemetry. When on,
a failure in recording is logged and swallowed: observability must never break a run.

Two independent channels, both configured only through environment variables:
  * generations (prompt, reply, tokens, model) -> AGENTO11Y_* via the agento11y SDK
  * OTel traces and metrics                    -> OTEL_EXPORTER_OTLP_*
"""
from __future__ import annotations

import contextlib
import logging
import os
import sys
from typing import Any, Iterator

log = logging.getLogger("observability")

AGENT_NAME = "my-first-agent"


class _NoopToolRecorder:
    def finish(self, arguments: dict[str, Any], output: str, is_error: bool) -> None:
        pass


class Observability:
    """Disabled implementation: calls straight through, records nothing."""

    enabled = False

    def create_message(self, client: Any, request: dict[str, Any], *, conversation_id: str = "") -> Any:
        return client.messages.create(**request)

    @contextlib.contextmanager
    def tool_execution(self, name: str, call_id: str, *, conversation_id: str = "",
                       model: str = "") -> Iterator[_NoopToolRecorder]:
        yield _NoopToolRecorder()

    def shutdown(self) -> None:
        pass


class _ToolRecorder:
    def __init__(self, recorder: Any, tool_end: Any) -> None:
        self._recorder, self._tool_end = recorder, tool_end

    def finish(self, arguments: dict[str, Any], output: str, is_error: bool) -> None:
        try:
            if is_error:
                self._recorder.set_exec_error(RuntimeError(output))
            else:
                self._recorder.set_result(self._tool_end(arguments=arguments, result=output))
        except Exception as exc:  # telemetry must never break the run
            log.warning("observability: recording tool result failed: %s", exc)


class ActiveObservability(Observability):
    enabled = True

    def __init__(self, client: Any, entrypoint: str, version: str, providers: tuple[Any, ...] = ()) -> None:
        self._client, self._entrypoint, self._version = client, entrypoint, version
        self._providers = providers

    def create_message(self, client: Any, request: dict[str, Any], *, conversation_id: str = "") -> Any:
        from agento11y_anthropic import AnthropicOptions
        from agento11y_anthropic import messages as anthropic_messages

        captured: dict[str, Any] = {}

        def call_model(req: dict[str, Any]) -> Any:
            captured["response"] = client.messages.create(**req)
            return captured["response"]

        options = AnthropicOptions(
            conversation_id=conversation_id, agent_name=AGENT_NAME, agent_version=self._version,
            tags={"entrypoint": self._entrypoint},
        )
        try:
            return anthropic_messages.create(self._client, request, call_model, options)
        except Exception as exc:
            if "response" in captured:
                # The model answered; only recording failed (the SDK re-raises its own errors).
                log.warning("observability: recording generation failed: %s", exc)
                return captured["response"]
            raise  # the model call itself failed: let the loop handle it as before

    @contextlib.contextmanager
    def tool_execution(self, name: str, call_id: str, *, conversation_id: str = "",
                       model: str = "") -> Iterator[Any]:
        from agento11y import ToolExecutionEnd, ToolExecutionStart

        try:
            recorder = self._client.start_tool_execution(ToolExecutionStart(
                tool_name=name, tool_call_id=call_id, conversation_id=conversation_id,
                agent_name=AGENT_NAME, agent_version=self._version,
                request_model=model, request_provider="anthropic",
            ))
        except Exception as exc:
            log.warning("observability: starting tool execution failed: %s", exc)
            yield _NoopToolRecorder()
            return
        try:
            yield _ToolRecorder(recorder, ToolExecutionEnd)
        finally:
            try:
                recorder.end()
            except Exception as exc:
                log.warning("observability: ending tool execution failed: %s", exc)

    def shutdown(self) -> None:
        """Flush queued generations, then the OTel providers, before the process exits."""
        try:
            self._client.shutdown()
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
