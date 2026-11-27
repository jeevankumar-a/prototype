import os
from contextlib import contextmanager
from time import monotonic
from typing import Iterator

from opentelemetry import metrics, propagate, trace
from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from temporalio import activity


def configure(service_name: str) -> None:
    endpoint = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT", "http://localhost:4318").rstrip("/")
    resource = Resource.create({"service.name": service_name})
    provider = TracerProvider(resource=resource)
    provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint=f"{endpoint}/v1/traces")))
    trace.set_tracer_provider(provider)
    reader = PeriodicExportingMetricReader(OTLPMetricExporter(endpoint=f"{endpoint}/v1/metrics"))
    metrics.set_meter_provider(MeterProvider(resource=resource, metric_readers=[reader]))


def set_document_attributes(span: trace.Span, filename: str, digest: str) -> None:
    span.set_attribute("temporal.workflow_id", f"document-{digest}")
    span.set_attribute("langfuse.trace.name", "document-to-repo")
    span.set_attribute("langfuse.session.id", f"document-{digest}")
    span.set_attribute("langfuse.trace.metadata.workflow_id", f"document-{digest}")
    span.set_attribute("langfuse.trace.metadata.filename", filename)


@contextmanager
def activity_span(name: str, traceparent: str, digest: str, filename: str) -> Iterator[None]:
    parent = propagate.extract({"traceparent": traceparent})
    meter = metrics.get_meter("document-workflow")
    histogram = meter.create_histogram("agent_step_duration_seconds", unit="s")
    start = monotonic()
    with trace.get_tracer("document-workflow").start_as_current_span(name, context=parent) as span:
        set_document_attributes(span, filename, digest)
        if activity.in_activity():
            info = activity.info()
            span.set_attribute("langfuse.observation.metadata.attempt", str(info.attempt))
            span.set_attribute("langfuse.observation.metadata.run_id", info.workflow_run_id)
        try:
            yield
        except Exception:
            span.set_attribute("langfuse.observation.metadata.outcome", "failure")
            raise
        else:
            span.set_attribute("langfuse.observation.metadata.outcome", "success")
        finally:
            histogram.record(monotonic() - start, {"step": name})


def trace_headers() -> dict[str, str]:
    headers: dict[str, str] = {}
    propagate.inject(headers)
    current = trace.get_current_span().get_span_context()
    if current.is_valid:
        headers["langfuse_trace_id"] = f"{current.trace_id:032x}"
    return headers
