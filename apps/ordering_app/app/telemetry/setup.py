"""TracerProvider for ordering-app.

Day 1 gate zero wires only the OTLP pipeline to Jaeger. The BlastRadius
pipeline is added at build step 5.
"""

from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

BATCH_KWARGS = {"max_export_batch_size": 200, "schedule_delay_millis": 2000}


def configure_tracing(service_name: str, otlp_endpoint: str) -> TracerProvider:
    provider = TracerProvider(resource=Resource.create({"service.name": service_name}))
    provider.add_span_processor(
        BatchSpanProcessor(
            OTLPSpanExporter(endpoint=f"{otlp_endpoint.rstrip('/')}/v1/traces"),
            **BATCH_KWARGS,
        )
    )
    trace.set_tracer_provider(provider)
    return provider
