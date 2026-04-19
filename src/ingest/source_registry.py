"""Source registry — the ingest runtime's read-only view onto bot-config-api's
`knowledge_sources` table (ADR-001 Phase 3).

Pathway needs to know which buckets/prefixes to watch, what scope each one
feeds, and what Qdrant collection the chunks land in. Phase 1 introduced
the `knowledge_sources` table; Phase 2 made the upload/status contract
source-aware; Phase 3 brings the runtime in line by:

    * Reading active `managed_upload` sources at pipeline startup.
    * Materialising one Pathway watcher per source (see pipeline.py).
    * Preserving the pilot's single-scope env-based bootstrap as a
      fallback so local development and cluster rebuilds that run
      before Postgres is reachable still work.

Design choices:

    * **Startup-only read.** Pathway constructs its dataflow graph at
      process start; adding or removing a `pw.io.s3.read(...)` watcher
      requires a process restart anyway. This keeps the runtime
      predictable — no concurrent watcher lifecycle management, no
      race with mid-flight S3 polls.
    * **Skip-with-error, don't ingest wrong data.** A row with an
      incomplete `storage_ref` is logged loudly and skipped. The
      runtime never silently falls back to "some other bucket" for a
      broken source row.
    * **No DB writes.** This module is read-only. Source CRUD lives on
      the bot-config-api control plane.

Not yet in scope for Phase 3:

    * `external_s3` kind (Phase 4).
    * Per-source Vault credential fetch (Phase 4).
    * Hot reload of the registry (Phase 3+).
"""

from __future__ import annotations

import hashlib
import logging
import os
from dataclasses import dataclass
from typing import Iterable

try:
    import psycopg
    from psycopg.rows import dict_row as _dict_row
except ImportError:  # pragma: no cover — container image has psycopg
    psycopg = None  # type: ignore[assignment]
    _dict_row = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class SourceConfig:
    """Everything the Pathway runtime needs to stand up a single watcher."""

    source_id: str | None  # None for synthetic pilot-env fallbacks
    scope_identity: str    # e.g. "utop/oddspark" — human-readable
    bucket: str
    prefix: str
    region: str
    qdrant_collection: str  # kb-{sha256(scope)[:16]}

    @property
    def is_synthetic(self) -> bool:
        """True when this config came from env vars, not a DB row."""
        return self.source_id is None


def _qdrant_collection_for_scope(scope: str) -> str:
    """Match the hashing bot-config-api / knowledge-api use (ADR-011).

    Same algorithm used in:
      - bot-config-api `routers/knowledge.py` (upload-status path)
      - knowledge-api `services/knowledge.py` (`QdrantBackend._qdrant_collection_name`)
      - agentopia-ui `KnowledgePage` does not compute this — it reads collection
        name from the API responses.
    Keeping the derivation in lock-step prevents split-brain at retrieval
    time: the same canonical scope identity always maps to the same
    collection, irrespective of which service produced the key.
    """
    return "kb-" + hashlib.sha256(scope.encode("utf-8")).hexdigest()[:16]


def _db_url() -> str:
    return os.getenv("DATABASE_URL", "")


def _row_to_config(row: dict) -> SourceConfig | None:
    """Project a `knowledge_sources` row onto a SourceConfig.

    Returns None (and logs loudly) when the row is not a safe runtime
    target — incomplete `storage_ref` fields, unsupported kind, or
    non-active status. This is the "skip with error, don't ingest wrong
    data" contract from the module docstring.
    """
    kind = str(row.get("kind") or "")
    status = str(row.get("status") or "")
    source_id = str(row.get("source_id") or "")
    client_id = row.get("client_id") or ""
    scope_name = row.get("scope_name") or ""
    scope_identity = f"{client_id}/{scope_name}" if client_id and scope_name else ""

    if kind != "managed_upload":
        # external_s3 and future kinds are handled by later phases.
        return None
    if status != "active":
        logger.info(
            "source_registry: skipping source %s (status=%s, scope=%s)",
            source_id, status, scope_identity,
        )
        return None
    if not scope_identity:
        logger.warning(
            "source_registry: skipping source %s — missing client_id/scope_name",
            source_id,
        )
        return None

    storage = row.get("storage_ref") or {}
    if not isinstance(storage, dict):
        logger.warning(
            "source_registry: skipping source %s — storage_ref is not a JSON object",
            source_id,
        )
        return None
    bucket = storage.get("bucket")
    prefix = storage.get("prefix")
    region = storage.get("region")
    if not bucket or not prefix or not region:
        logger.warning(
            "source_registry: skipping source %s scope=%s — storage_ref missing "
            "one of (bucket=%r, prefix=%r, region=%r)",
            source_id, scope_identity, bucket, prefix, region,
        )
        return None

    return SourceConfig(
        source_id=source_id,
        scope_identity=scope_identity,
        bucket=str(bucket),
        prefix=str(prefix),
        region=str(region),
        qdrant_collection=_qdrant_collection_for_scope(scope_identity),
    )


def load_active_sources() -> list[SourceConfig]:
    """Return every active `managed_upload` source safe to ingest from.

    An empty list is a valid result — callers are expected to check
    before constructing the Pathway graph and should apply the
    env-based synthetic-pilot fallback (see `synthesize_pilot_source`)
    when appropriate.
    """
    if psycopg is None or not _db_url():
        logger.info(
            "source_registry: DATABASE_URL or psycopg unavailable — "
            "skipping registry read (env-fallback path may still apply)",
        )
        return []

    try:
        with psycopg.connect(_db_url(), row_factory=_dict_row) as conn:
            rows = conn.execute(
                """
                SELECT source_id, client_id, scope_name, kind,
                       display_name, status, storage_ref
                  FROM knowledge_sources
                 WHERE kind = 'managed_upload'
                   AND status = 'active'
              ORDER BY client_id ASC, scope_name ASC, created_at ASC
                """,
            ).fetchall()
    except Exception as exc:
        # A DB outage must not take the runtime down silently. Log and
        # return empty so the env-fallback path can keep the pilot
        # alive during a control-plane blip.
        logger.error("source_registry: DB error — %s", exc)
        return []

    configs: list[SourceConfig] = []
    for row in rows:
        cfg = _row_to_config(row)
        if cfg is not None:
            configs.append(cfg)
    logger.info(
        "source_registry: loaded %d managed_upload source(s) from knowledge_sources",
        len(configs),
    )
    return configs


def synthesize_pilot_source() -> SourceConfig | None:
    """Build a synthetic `SourceConfig` from the legacy pipeline env vars.

    Used only as a rescue path when the registry returns zero rows but
    the deployment is still configured with the Phase-0 env contract
    (`S3_BUCKET_NAME` + `S3_PREFIX` + `S3_REGION` + `SCOPE_IDENTITY`).
    The synthetic source has `source_id=None` so downstream code can
    tell it apart — no payload pretends to carry a fake UUID.
    """
    bucket = os.getenv("S3_BUCKET_NAME", "")
    prefix = os.getenv("S3_PREFIX", "")
    region = os.getenv("S3_REGION", "")
    # Optional — SCOPE_IDENTITY falls back to QDRANT_COLLECTION only for
    # pilot compatibility; new deployments must always set a human-readable
    # scope identity.
    scope_identity = os.getenv("SCOPE_IDENTITY") or os.getenv("QDRANT_COLLECTION", "")
    if not (bucket and prefix and region and scope_identity):
        return None
    qdrant_collection = (
        os.getenv("QDRANT_COLLECTION")
        or _qdrant_collection_for_scope(scope_identity)
    )
    logger.warning(
        "source_registry: falling back to synthetic pilot source from env "
        "(bucket=%s, prefix=%s, scope=%s). Populate knowledge_sources to "
        "transition off the env-fallback path.",
        bucket, prefix, scope_identity,
    )
    return SourceConfig(
        source_id=None,
        scope_identity=scope_identity,
        bucket=bucket,
        prefix=prefix,
        region=region,
        qdrant_collection=qdrant_collection,
    )


def resolve_sources() -> list[SourceConfig]:
    """Public entry point used by `pipeline.py::build_pipeline`.

    Order of preference:
      1. Active rows from `knowledge_sources` (the product model).
      2. A single synthetic pilot source from env (legacy compat).
      3. Empty list — the caller should fail fast, because trying to
         `pw.run()` with no watchers is a silent no-op in Pathway and
         would otherwise look like a working-but-idle deployment.
    """
    configs = load_active_sources()
    if configs:
        return configs
    synthetic = synthesize_pilot_source()
    return [synthetic] if synthetic else []


def log_source_plan(sources: Iterable[SourceConfig]) -> None:
    """Emit the per-source plan once at startup.

    Kept as a single INFO log so operators see the entire watcher
    roster together in the pod log — easier to reason about than one
    line per source interleaved with Pathway's own startup noise.
    """
    lines = []
    for s in sources:
        tag = "synthetic-env" if s.is_synthetic else f"source_id={s.source_id}"
        lines.append(
            f"  - {tag}  scope={s.scope_identity}  "
            f"s3://{s.bucket}/{s.prefix}  region={s.region}  "
            f"→ collection {s.qdrant_collection}"
        )
    logger.info(
        "pipeline: %d watcher(s) planned:\n%s",
        len(lines), "\n".join(lines) if lines else "  (none)",
    )
