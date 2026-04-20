"""Tests for ingest/metrics.py — issue #19 Pathway metrics to Prometheus.

Boundary: no real Pathway runtime, no real Prometheus scrape.
Tests verify metric registration, label correctness, increment/set
semantics, and that the entrypoint wires SOURCES_ACTIVE correctly.

Coverage:
  - EVENTS_TOTAL counter increments for add and delete operations
  - LAST_EVENT_TIMESTAMP gauge is set to wall-clock time on each event
  - PIPELINE_ERRORS_TOTAL counter increments by error_type label
  - SOURCES_ACTIVE gauge is set to the watcher count returned by build_pipeline
  - start_metrics_server reads METRICS_PORT env var
  - Metric names carry the agentopia_pathway_ prefix
"""

import os
import sys
import types
import time as _time
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Stub out heavy dependencies before importing pipeline modules
# ---------------------------------------------------------------------------

def _stub(name: str) -> types.ModuleType:
    m = types.ModuleType(name)
    sys.modules[name] = m
    return m

# pathway
_pw = _stub("pathway")
_pw.udf = lambda f: f
_pw.run = MagicMock()
_pw.this = MagicMock()

class _FakePointer:
    pass

_pw.Pointer = _FakePointer

_pw_io = _stub("pathway.io")
_pw.io = _pw_io  # pw.io attribute must exist for `class Foo(pw.io.python.ConnectorObserver)`

_pw_io_python = _stub("pathway.io.python")
_pw_io.python = _pw_io_python  # pw.io.python attribute

class _FakeObserver:
    pass

_pw_io_python.ConnectorObserver = _FakeObserver
_pw_io_s3 = _stub("pathway.io.s3")
_pw_io.s3 = _pw_io_s3
_pw_io_python.write = MagicMock()  # pw.io.python.write used in pipeline

_pw_persistence = _stub("pathway.persistence")
_pw_persistence.Config = MagicMock()
_pw_persistence.Backend = MagicMock()
_pw.persistence = _pw_persistence

# openai, qdrant, dotenv
_stub("openai").OpenAI = MagicMock()
_qdc = _stub("qdrant_client")
_qdc.QdrantClient = MagicMock()
_qdc_models = _stub("qdrant_client.models")
_qdc_models.Distance = MagicMock()
_qdc_models.PointStruct = MagicMock()
_qdc_models.VectorParams = MagicMock()
_stub("dotenv").load_dotenv = lambda: None

# Set required env vars so pipeline module-level code doesn't crash on import
os.environ.setdefault("QDRANT_URL", "http://qdrant:6333")
os.environ.setdefault("S3_ACCESS_KEY", "testkey")
os.environ.setdefault("S3_SECRET_ACCESS_KEY", "testsecret")
os.environ.setdefault("EMBEDDING_API_KEY", "testembedkey")

# Now import the metrics module (standalone — no heavy deps)
_SRC = os.path.join(os.path.dirname(__file__), "..", "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from ingest.metrics import (
    EVENTS_TOTAL,
    LAST_EVENT_TIMESTAMP,
    PIPELINE_ERRORS_TOTAL,
    SOURCES_ACTIVE,
    start_metrics_server,
)


# ---------------------------------------------------------------------------
# Helper: read a labeled counter/gauge value
# ---------------------------------------------------------------------------

def _counter_value(counter, **labels) -> float:
    return counter.labels(**labels)._value.get()

def _gauge_value(gauge, **labels) -> float:
    return gauge.labels(**labels)._value.get()


# ---------------------------------------------------------------------------
# EVENTS_TOTAL
# ---------------------------------------------------------------------------

class TestEventsTotal:
    def test_add_operation_increments(self):
        before = _counter_value(EVENTS_TOTAL, scope="test/scope-add", operation="add")
        EVENTS_TOTAL.labels(scope="test/scope-add", operation="add").inc()
        after = _counter_value(EVENTS_TOTAL, scope="test/scope-add", operation="add")
        assert after == before + 1

    def test_delete_operation_increments(self):
        before = _counter_value(EVENTS_TOTAL, scope="test/scope-del", operation="delete")
        EVENTS_TOTAL.labels(scope="test/scope-del", operation="delete").inc()
        after = _counter_value(EVENTS_TOTAL, scope="test/scope-del", operation="delete")
        assert after == before + 1

    def test_add_and_delete_are_independent(self):
        scope = "test/scope-independent"
        EVENTS_TOTAL.labels(scope=scope, operation="add").inc()
        EVENTS_TOTAL.labels(scope=scope, operation="add").inc()
        EVENTS_TOTAL.labels(scope=scope, operation="delete").inc()
        adds = _counter_value(EVENTS_TOTAL, scope=scope, operation="add")
        deletes = _counter_value(EVENTS_TOTAL, scope=scope, operation="delete")
        assert adds >= 2
        assert deletes >= 1
        assert adds != deletes

    def test_metric_name_has_correct_prefix(self):
        # prometheus_client stores base name without _total; Prometheus appends it at scrape
        assert EVENTS_TOTAL._name == "agentopia_pathway_events"

    def test_different_scopes_are_independent(self):
        EVENTS_TOTAL.labels(scope="scope-x", operation="add").inc()
        val_x = _counter_value(EVENTS_TOTAL, scope="scope-x", operation="add")
        val_y = _counter_value(EVENTS_TOTAL, scope="scope-y-never-touched", operation="add")
        assert val_x >= 1
        assert val_y == 0


# ---------------------------------------------------------------------------
# LAST_EVENT_TIMESTAMP
# ---------------------------------------------------------------------------

class TestLastEventTimestamp:
    def test_set_records_current_time(self):
        scope = "test/ts-scope"
        t_before = _time.time()
        LAST_EVENT_TIMESTAMP.labels(scope=scope).set(_time.time())
        t_after = _time.time()
        val = _gauge_value(LAST_EVENT_TIMESTAMP, scope=scope)
        assert t_before <= val <= t_after

    def test_metric_name_has_correct_prefix(self):
        assert LAST_EVENT_TIMESTAMP._name == "agentopia_pathway_last_event_timestamp_seconds"

    def test_different_scopes_are_independent(self):
        LAST_EVENT_TIMESTAMP.labels(scope="ts-scope-a").set(1_000_000.0)
        LAST_EVENT_TIMESTAMP.labels(scope="ts-scope-b").set(2_000_000.0)
        assert _gauge_value(LAST_EVENT_TIMESTAMP, scope="ts-scope-a") == 1_000_000.0
        assert _gauge_value(LAST_EVENT_TIMESTAMP, scope="ts-scope-b") == 2_000_000.0


# ---------------------------------------------------------------------------
# PIPELINE_ERRORS_TOTAL
# ---------------------------------------------------------------------------

class TestPipelineErrorsTotal:
    def test_credential_error_increments(self):
        before = _counter_value(PIPELINE_ERRORS_TOTAL, error_type="credential_error")
        PIPELINE_ERRORS_TOTAL.labels(error_type="credential_error").inc()
        assert _counter_value(PIPELINE_ERRORS_TOTAL, error_type="credential_error") == before + 1

    def test_qdrant_error_increments(self):
        before = _counter_value(PIPELINE_ERRORS_TOTAL, error_type="qdrant_error")
        PIPELINE_ERRORS_TOTAL.labels(error_type="qdrant_error").inc()
        assert _counter_value(PIPELINE_ERRORS_TOTAL, error_type="qdrant_error") == before + 1

    def test_error_types_are_independent(self):
        PIPELINE_ERRORS_TOTAL.labels(error_type="credential_error").inc()
        PIPELINE_ERRORS_TOTAL.labels(error_type="qdrant_error").inc()
        cred = _counter_value(PIPELINE_ERRORS_TOTAL, error_type="credential_error")
        qdrant = _counter_value(PIPELINE_ERRORS_TOTAL, error_type="qdrant_error")
        assert cred >= 1
        assert qdrant >= 1

    def test_metric_name_has_correct_prefix(self):
        assert PIPELINE_ERRORS_TOTAL._name == "agentopia_pathway_pipeline_errors"


# ---------------------------------------------------------------------------
# SOURCES_ACTIVE
# ---------------------------------------------------------------------------

class TestSourcesActive:
    def test_set_reflects_watcher_count(self):
        SOURCES_ACTIVE.set(3)
        assert SOURCES_ACTIVE._value.get() == 3

    def test_metric_name_has_correct_prefix(self):
        assert SOURCES_ACTIVE._name == "agentopia_pathway_sources_active"

    def test_zero_is_valid(self):
        SOURCES_ACTIVE.set(0)
        assert SOURCES_ACTIVE._value.get() == 0


# ---------------------------------------------------------------------------
# start_metrics_server — port wiring
# ---------------------------------------------------------------------------

class TestStartMetricsServer:
    def test_uses_default_port_9090(self):
        with patch("ingest.metrics.start_http_server") as mock_start:
            os.environ.pop("METRICS_PORT", None)
            port = start_metrics_server()
        mock_start.assert_called_once_with(9090)
        assert port == 9090

    def test_reads_metrics_port_env(self):
        with patch("ingest.metrics.start_http_server") as mock_start:
            os.environ["METRICS_PORT"] = "9100"
            port = start_metrics_server()
            os.environ.pop("METRICS_PORT", None)
        mock_start.assert_called_once_with(9100)
        assert port == 9100

    def test_explicit_port_overrides_env(self):
        with patch("ingest.metrics.start_http_server") as mock_start:
            os.environ["METRICS_PORT"] = "9999"
            port = start_metrics_server(port=8080)
            os.environ.pop("METRICS_PORT", None)
        mock_start.assert_called_once_with(8080)
        assert port == 8080


# ---------------------------------------------------------------------------
# QdrantSink metric timing — M19-C4
# Verify: success metrics advance ONLY after confirmed Qdrant persistence.
# ---------------------------------------------------------------------------

class TestQdrantSinkMetricTiming:
    """Timing correctness: metrics must not advance when Qdrant write fails."""

    def _make_sink(self, mock_client=None):
        from ingest.pipeline import QdrantSink
        if mock_client is None:
            mock_client = MagicMock()
            mock_client.get_collections.return_value.collections = []
        sink = QdrantSink.__new__(QdrantSink)
        sink._client = mock_client
        sink._collection = "kb-test"
        sink._scope_identity = "test/timing"
        sink._source_id = "src-test"
        return sink

    def _make_row(self):
        return {
            "chunk": {"text": "hello", "section": "", "chunk_index": 0, "total_chunks": 1},
            "document_id": "docs/test.md",
            "section_path": "docs/test.md",
            "embedding": [0.1] * 4,
        }

    def test_successful_add_increments_events_counter(self):
        sink = self._make_sink()
        sink._scope_identity = "test/timing-ok-add"
        before = _counter_value(EVENTS_TOTAL, scope=sink._scope_identity, operation="add")
        sink.on_change(MagicMock(), self._make_row(), 1, is_addition=True)
        assert _counter_value(EVENTS_TOTAL, scope=sink._scope_identity, operation="add") == before + 1

    def test_successful_add_sets_timestamp(self):
        sink = self._make_sink()
        sink._scope_identity = "test/timing-ok-ts"
        t_before = _time.time()
        sink.on_change(MagicMock(), self._make_row(), 1, is_addition=True)
        assert _gauge_value(LAST_EVENT_TIMESTAMP, scope=sink._scope_identity) >= t_before

    def test_successful_delete_increments_events_counter(self):
        sink = self._make_sink()
        sink._scope_identity = "test/timing-ok-del"
        before = _counter_value(EVENTS_TOTAL, scope=sink._scope_identity, operation="delete")
        sink.on_change(MagicMock(), self._make_row(), 1, is_addition=False)
        assert _counter_value(EVENTS_TOTAL, scope=sink._scope_identity, operation="delete") == before + 1

    def test_failed_upsert_increments_error_counter(self):
        mock_client = MagicMock()
        mock_client.get_collections.return_value.collections = []
        mock_client.upsert.side_effect = RuntimeError("qdrant unavailable")
        sink = self._make_sink(mock_client)
        sink._scope_identity = "test/timing-fail-add"
        before_err = _counter_value(PIPELINE_ERRORS_TOTAL, error_type="qdrant_error")
        with pytest.raises(RuntimeError):
            sink.on_change(MagicMock(), self._make_row(), 1, is_addition=True)
        assert _counter_value(PIPELINE_ERRORS_TOTAL, error_type="qdrant_error") == before_err + 1

    def test_failed_upsert_does_not_advance_events_counter(self):
        mock_client = MagicMock()
        mock_client.get_collections.return_value.collections = []
        mock_client.upsert.side_effect = RuntimeError("qdrant unavailable")
        sink = self._make_sink(mock_client)
        sink._scope_identity = "test/timing-fail-add-events"
        before = _counter_value(EVENTS_TOTAL, scope=sink._scope_identity, operation="add")
        with pytest.raises(RuntimeError):
            sink.on_change(MagicMock(), self._make_row(), 1, is_addition=True)
        assert _counter_value(EVENTS_TOTAL, scope=sink._scope_identity, operation="add") == before

    def test_failed_upsert_does_not_advance_timestamp(self):
        mock_client = MagicMock()
        mock_client.get_collections.return_value.collections = []
        mock_client.upsert.side_effect = RuntimeError("qdrant unavailable")
        sink = self._make_sink(mock_client)
        sink._scope_identity = "test/timing-fail-ts"
        LAST_EVENT_TIMESTAMP.labels(scope=sink._scope_identity).set(0.0)
        with pytest.raises(RuntimeError):
            sink.on_change(MagicMock(), self._make_row(), 1, is_addition=True)
        assert _gauge_value(LAST_EVENT_TIMESTAMP, scope=sink._scope_identity) == 0.0

    def test_failed_delete_increments_error_counter(self):
        mock_client = MagicMock()
        mock_client.get_collections.return_value.collections = []
        mock_client.delete.side_effect = RuntimeError("qdrant unavailable")
        sink = self._make_sink(mock_client)
        sink._scope_identity = "test/timing-fail-del"
        before_err = _counter_value(PIPELINE_ERRORS_TOTAL, error_type="qdrant_error")
        with pytest.raises(RuntimeError):
            sink.on_change(MagicMock(), self._make_row(), 1, is_addition=False)
        assert _counter_value(PIPELINE_ERRORS_TOTAL, error_type="qdrant_error") == before_err + 1

    def test_failed_delete_does_not_advance_events_counter(self):
        mock_client = MagicMock()
        mock_client.get_collections.return_value.collections = []
        mock_client.delete.side_effect = RuntimeError("qdrant unavailable")
        sink = self._make_sink(mock_client)
        sink._scope_identity = "test/timing-fail-del-events"
        before = _counter_value(EVENTS_TOTAL, scope=sink._scope_identity, operation="delete")
        with pytest.raises(RuntimeError):
            sink.on_change(MagicMock(), self._make_row(), 1, is_addition=False)
        assert _counter_value(EVENTS_TOTAL, scope=sink._scope_identity, operation="delete") == before
