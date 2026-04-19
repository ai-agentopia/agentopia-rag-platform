"""Phase 3 tests: source registry and pipeline planning.

These tests do NOT import `pathway` or spin up a real Pathway graph —
they cover the control-plane→runtime boundary: the registry reader, the
row→SourceConfig projection, and the synthetic-pilot fallback. That's
the logic we can exercise without a Postgres container, without AWS
credentials, and without a Pathway worker. Graph construction is left
to live verification on the cluster.
"""

from __future__ import annotations

import hashlib
import os
import sys
from unittest.mock import MagicMock, patch

# Make sure `src/` is importable — there's no pyproject.toml pythonpath.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from ingest.source_registry import (  # noqa: E402
    SourceConfig,
    _qdrant_collection_for_scope,
    _row_to_config,
    load_active_sources,
    resolve_sources,
    synthesize_pilot_source,
)


# ── Collection derivation ──────────────────────────────────────────────────


class TestCollectionDerivation:
    def test_matches_bot_config_api_derivation(self):
        # The same SHA-256-prefix algorithm bot-config-api and knowledge-api
        # use. Lock-step at retrieval time requires byte-identical output.
        scope = "utop/oddspark"
        expected = "kb-" + hashlib.sha256(scope.encode("utf-8")).hexdigest()[:16]
        assert _qdrant_collection_for_scope(scope) == expected
        # Spot-check the pilot's actual collection name.
        assert _qdrant_collection_for_scope("utop/oddspark") == "kb-45294aa854667d3b"

    def test_different_scopes_yield_different_collections(self):
        a = _qdrant_collection_for_scope("acme/docs")
        b = _qdrant_collection_for_scope("acme/api")
        assert a != b
        assert a.startswith("kb-") and b.startswith("kb-")


# ── Row → SourceConfig projection ─────────────────────────────────────────


def _managed_row(
    *,
    source_id: str = "3791ba64-3b3f-493a-a7f8-0f08073bde8d",
    client_id: str = "utop",
    scope_name: str = "oddspark",
    bucket: str = "utop-oddspark-document",
    prefix: str = "architecture/",
    region: str = "ap-northeast-1",
    kind: str = "managed_upload",
    status: str = "active",
) -> dict:
    return {
        "source_id": source_id,
        "client_id": client_id,
        "scope_name": scope_name,
        "kind": kind,
        "display_name": "Default managed upload",
        "status": status,
        "storage_ref": {"bucket": bucket, "prefix": prefix, "region": region},
    }


class TestRowToConfig:
    def test_valid_managed_upload_row_projects_cleanly(self):
        cfg = _row_to_config(_managed_row())
        assert cfg is not None
        assert cfg.source_id == "3791ba64-3b3f-493a-a7f8-0f08073bde8d"
        assert cfg.scope_identity == "utop/oddspark"
        assert cfg.bucket == "utop-oddspark-document"
        assert cfg.prefix == "architecture/"
        assert cfg.region == "ap-northeast-1"
        assert cfg.qdrant_collection == "kb-45294aa854667d3b"
        assert cfg.is_synthetic is False

    def test_external_s3_kind_is_skipped_without_error(self):
        # Phase 4 handles external_s3. Phase 3 must skip it.
        assert _row_to_config(_managed_row(kind="external_s3")) is None

    def test_non_active_status_is_skipped(self):
        for status in ("provisioning", "paused", "error", "deprovisioning"):
            assert _row_to_config(_managed_row(status=status)) is None

    def test_incomplete_storage_ref_skips_with_warning(self, caplog):
        with caplog.at_level("WARNING"):
            row = _managed_row()
            row["storage_ref"]["bucket"] = None
            assert _row_to_config(row) is None
            assert any("skipping source" in r.message for r in caplog.records)

    def test_missing_scope_parts_skip_with_warning(self, caplog):
        row = _managed_row(client_id="", scope_name="oddspark")
        with caplog.at_level("WARNING"):
            assert _row_to_config(row) is None

    def test_non_dict_storage_ref_is_rejected(self):
        row = _managed_row()
        row["storage_ref"] = "not-a-dict"  # type: ignore[assignment]
        assert _row_to_config(row) is None


# ── load_active_sources ────────────────────────────────────────────────────


class _FakeConn:
    def __init__(self, rows):
        self._rows = rows

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def execute(self, *_a, **_kw):
        cur = MagicMock()
        cur.fetchall.return_value = self._rows
        return cur


class TestLoadActiveSources:
    def test_returns_empty_list_when_database_url_unset(self, monkeypatch):
        monkeypatch.delenv("DATABASE_URL", raising=False)
        assert load_active_sources() == []

    def test_projects_rows_from_postgres(self, monkeypatch):
        monkeypatch.setenv("DATABASE_URL", "postgresql://x")
        rows = [_managed_row()]
        with patch("psycopg.connect", return_value=_FakeConn(rows)):
            cfgs = load_active_sources()
        assert len(cfgs) == 1
        assert cfgs[0].source_id == rows[0]["source_id"]

    def test_skips_unsupported_rows(self, monkeypatch):
        monkeypatch.setenv("DATABASE_URL", "postgresql://x")
        rows = [
            _managed_row(),                             # kept
            _managed_row(kind="external_s3",            # skipped — Phase 4
                         source_id="bbbb0000-0000-0000-0000-000000000002"),
            _managed_row(status="paused",               # skipped — not active
                         source_id="cccc0000-0000-0000-0000-000000000003"),
        ]
        with patch("psycopg.connect", return_value=_FakeConn(rows)):
            cfgs = load_active_sources()
        # Only the active managed_upload row survives — _row_to_config
        # already enforces kind + status; test the aggregate filter.
        assert len(cfgs) == 1
        assert cfgs[0].source_id == "3791ba64-3b3f-493a-a7f8-0f08073bde8d"

    def test_db_error_returns_empty_not_raises(self, monkeypatch, caplog):
        monkeypatch.setenv("DATABASE_URL", "postgresql://x")
        with patch("psycopg.connect", side_effect=Exception("no route to host")), \
             caplog.at_level("ERROR"):
            assert load_active_sources() == []
            assert any("DB error" in r.message for r in caplog.records)


# ── synthesize_pilot_source ───────────────────────────────────────────────


class TestSyntheticFallback:
    def test_returns_none_when_env_incomplete(self, monkeypatch):
        for var in ("S3_BUCKET_NAME", "S3_PREFIX", "S3_REGION",
                    "SCOPE_IDENTITY", "QDRANT_COLLECTION"):
            monkeypatch.delenv(var, raising=False)
        assert synthesize_pilot_source() is None

    def test_synthetic_config_has_no_source_id(self, monkeypatch):
        monkeypatch.setenv("S3_BUCKET_NAME", "utop-oddspark-document")
        monkeypatch.setenv("S3_PREFIX", "architecture/")
        monkeypatch.setenv("S3_REGION", "ap-northeast-1")
        monkeypatch.setenv("SCOPE_IDENTITY", "utop/oddspark")
        cfg = synthesize_pilot_source()
        assert cfg is not None
        assert cfg.source_id is None
        assert cfg.is_synthetic is True
        # Collection still derived from the scope identity — so a fallback
        # pilot lands in the same Qdrant bucket the sources-driven path
        # would.
        assert cfg.qdrant_collection == "kb-45294aa854667d3b"

    def test_uses_explicit_qdrant_collection_if_set(self, monkeypatch):
        monkeypatch.setenv("S3_BUCKET_NAME", "b")
        monkeypatch.setenv("S3_PREFIX", "p/")
        monkeypatch.setenv("S3_REGION", "r")
        monkeypatch.setenv("SCOPE_IDENTITY", "x/y")
        monkeypatch.setenv("QDRANT_COLLECTION", "kb-custom-pin")
        cfg = synthesize_pilot_source()
        assert cfg is not None
        assert cfg.qdrant_collection == "kb-custom-pin"


# ── resolve_sources ────────────────────────────────────────────────────────


class TestResolveSources:
    def test_prefers_registry_over_env_fallback(self, monkeypatch):
        monkeypatch.setenv("DATABASE_URL", "postgresql://x")
        monkeypatch.setenv("S3_BUCKET_NAME", "b")
        monkeypatch.setenv("S3_PREFIX", "p/")
        monkeypatch.setenv("S3_REGION", "r")
        monkeypatch.setenv("SCOPE_IDENTITY", "x/y")
        with patch("psycopg.connect", return_value=_FakeConn([_managed_row()])):
            cfgs = resolve_sources()
        assert len(cfgs) == 1
        # Registry row wins — synthetic fallback never materialises.
        assert cfgs[0].source_id == "3791ba64-3b3f-493a-a7f8-0f08073bde8d"
        assert cfgs[0].is_synthetic is False

    def test_falls_back_to_synthetic_when_registry_empty(self, monkeypatch):
        monkeypatch.setenv("DATABASE_URL", "postgresql://x")
        monkeypatch.setenv("S3_BUCKET_NAME", "utop-oddspark-document")
        monkeypatch.setenv("S3_PREFIX", "architecture/")
        monkeypatch.setenv("S3_REGION", "ap-northeast-1")
        monkeypatch.setenv("SCOPE_IDENTITY", "utop/oddspark")
        with patch("psycopg.connect", return_value=_FakeConn([])):
            cfgs = resolve_sources()
        assert len(cfgs) == 1
        assert cfgs[0].is_synthetic is True
        assert cfgs[0].bucket == "utop-oddspark-document"

    def test_empty_when_neither_registry_nor_env_has_data(self, monkeypatch):
        monkeypatch.delenv("DATABASE_URL", raising=False)
        for var in ("S3_BUCKET_NAME", "S3_PREFIX", "S3_REGION",
                    "SCOPE_IDENTITY", "QDRANT_COLLECTION"):
            monkeypatch.delenv(var, raising=False)
        assert resolve_sources() == []


