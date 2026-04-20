"""Prometheus metrics for the Pathway ingest pipeline — issue #19.

Metrics
-------
agentopia_pathway_events_total
    Counter. Incremented on every Qdrant write or delete.
    Labels: scope (scope_identity string), operation (add|delete).
    Derive event throughput rate in PromQL:
        rate(agentopia_pathway_events_total[5m]) * 60   → events/min

agentopia_pathway_last_event_timestamp_seconds
    Gauge per scope. Updated to wall-clock time on every on_change call.
    Derive indexing lag in PromQL:
        time() - agentopia_pathway_last_event_timestamp_seconds{scope="..."}

    Why Gauge instead of the Histogram named in the issue:
    Pathway's ConnectorObserver.on_change() does not surface the S3 object's
    LastModified timestamp — only Pathway's internal logical clock. Without
    the object's creation time, a true write→index latency histogram cannot
    be populated. The Gauge approach is functionally equivalent: staleness
    is derived by PromQL and the metric is directly actionable for alerting.
    A recording rule in PrometheusRule converts it to stale_scope_count.

agentopia_pathway_sources_active
    Gauge. Set once at pipeline startup to the number of watchers built.

agentopia_pathway_pipeline_errors_total
    Counter. Incremented on errors that skip a source or abort an operation.
    Labels: error_type (credential_error|embedding_error|qdrant_error|source_skip|unknown).

Exposure
--------
HTTP server started on METRICS_PORT (default 9090) before pw.run().
Prometheus Operator ServiceMonitor scrapes /metrics at 30-second intervals.
"""

import logging
import os

from prometheus_client import Counter, Gauge, start_http_server

logger = logging.getLogger(__name__)

EVENTS_TOTAL = Counter(
    "agentopia_pathway_events_total",
    "Total Pathway pipeline events processed (add or delete operations on Qdrant)",
    ["scope", "operation"],
)

LAST_EVENT_TIMESTAMP = Gauge(
    "agentopia_pathway_last_event_timestamp_seconds",
    "Wall-clock Unix timestamp of the last processed pipeline event per scope",
    ["scope"],
)

SOURCES_ACTIVE = Gauge(
    "agentopia_pathway_sources_active",
    "Number of active source watchers built in this pipeline process run",
)

PIPELINE_ERRORS_TOTAL = Counter(
    "agentopia_pathway_pipeline_errors_total",
    "Total pipeline errors that skipped a source or aborted an operation",
    ["error_type"],
)


def start_metrics_server(port: int | None = None) -> int:
    """Start the Prometheus HTTP server on the configured port.

    Must be called before pw.run() — starts a background thread.
    Returns the port the server is listening on.
    """
    if port is None:
        port = int(os.environ.get("METRICS_PORT", "9090"))
    start_http_server(port)
    logger.info("metrics: Prometheus HTTP server started on :%d /metrics", port)
    return port
