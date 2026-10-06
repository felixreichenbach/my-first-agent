"""OpenTelemetry setup, driven entirely by standard OTEL_* environment variables.

No endpoint configured -> telemetry is a silent no-op. A broken endpoint only
produces log warnings from the exporter; it never fails the agent run.
"""
from __future__ import annotations

import os
import sys

from opentelemetry import trace


def capture_content() -> bool:
    return os.getenv("OTEL_CAPTURE_CONTENT", "false").lower() == "true"


def setup_telemetry() -> "object | None":
    """Install a tracer provider if configured. Returns it (for flushing) or None."""
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor, ConsoleSpanExporter

    exporter = None
    if os.getenv("OTEL_TRACES_EXPORTER", "").lower() == "console":
        exporter = ConsoleSpanExporter(out=sys.stderr)
    elif os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT") or os.getenv("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT"):
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
        # Endpoint, headers and timeout are read from OTEL_EXPORTER_OTLP_* by the exporter.
        exporter = OTLPSpanExporter()

    if exporter is None:
        print("telemetry: disabled (set OTEL_EXPORTER_OTLP_ENDPOINT to enable)", file=sys.stderr)
        return None

    # Resource.create() picks up OTEL_SERVICE_NAME and OTEL_RESOURCE_ATTRIBUTES.
    provider = TracerProvider(resource=Resource.create())
    provider.add_span_processor(BatchSpanProcessor(exporter))
    trace.set_tracer_provider(provider)
    return provider


def shutdown_telemetry(provider: "object | None") -> None:
    """Flush pending spans before the one-shot container exits."""
    if provider is not None:
        try:
            provider.shutdown()
        except Exception as exc:  # telemetry must never break the run
            print(f"telemetry: flush failed: {exc}", file=sys.stderr)
