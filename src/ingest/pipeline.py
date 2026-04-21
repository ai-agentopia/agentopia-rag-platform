"""Pathway ingest pipeline — agentopia-rag-platform.

Phase 3 (ADR-001): the runtime is source-aware. At startup we read every
active `managed_upload` row from the control-plane `knowledge_sources`
table and materialise one Pathway watcher per source, each sinking into
its own scope-derived Qdrant collection (`kb-{sha256(scope)[:16]}`).

Backward-compatibility envelope during the Phase 3/4 window:

  * The old single-prefix env contract (`S3_BUCKET_NAME`, `S3_PREFIX`,
    `S3_REGION`, `SCOPE_IDENTITY`, `QDRANT_COLLECTION`) still bootstraps
    a synthetic pilot source when `knowledge_sources` has no rows — see
    `source_registry.synthesize_pilot_source`. New deployments should
    stop relying on it once a row is backfilled, per Phase 1.
  * Qdrant payload still carries the same fields as before (`scope`,
    `document_id`, `section_path`, ...). We additionally write
    `source_id` on every new chunk. Existing chunks without `source_id`
    stay queryable — retrieval filters on `scope`, which continues to
    work for both shapes.
  * `document_id` stays the raw S3 key (e.g. `architecture/foo.md`).
    Changing it now would invalidate every current Qdrant point's
    identity and force a global reindex. A canonical source-aware
    `document_id` is a future-phase concern once the corpus is entirely
    new-shape.

Per-source env vars (still read, now optional):
    QDRANT_URL                — Qdrant endpoint (shared by all watchers)
    EMBEDDING_BASE_URL        — OpenAI-compatible embeddings endpoint
    EMBEDDING_API_KEY         — API key for the embedding endpoint
    EMBEDDING_MODEL           — default text-embedding-3-small
    S3_ACCESS_KEY             — AWS creds, shared across managed sources
    S3_SECRET_ACCESS_KEY
    PATHWAY_POLL_INTERVAL_SECS — S3 poll interval in seconds, default 30
    PATHWAY_STATE_DIR         — path for persistence state PVC

Registry connectivity env (new in Phase 3):
    DATABASE_URL              — Postgres DSN; when unset, the runtime
                                falls back to the synthetic pilot
                                source built from the env vars below
                                (preserves local dev + bootstrap).

Per-source fields live on the `knowledge_sources` row; Phase 3 reads
them directly via `source_registry.resolve_sources()` — the old single-
prefix env vars drive only the synthetic-fallback path.
"""

import hashlib
import json
import logging
import os
import sys
import time as _time

# The container runs `python src/ingest/pipeline.py`, so only the script's
# own directory lands on sys.path. Put `src/` on the path so the sibling
# `ingest.*` package imports resolve. Running `python -m ingest.pipeline`
# would avoid this, but the Dockerfile entrypoint predates the package
# layout and there's no reason to couple this change to a Dockerfile bump.
_SRC_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _SRC_ROOT not in sys.path:
    sys.path.insert(0, _SRC_ROOT)

import pathway as pw  # noqa: E402
from openai import OpenAI  # noqa: E402
from pathway.xpacks.llm.parsers import PypdfParser, UnstructuredParser  # noqa: E402
from qdrant_client import QdrantClient  # noqa: E402
from qdrant_client.models import Distance, PointStruct, VectorParams  # noqa: E402

from dotenv import load_dotenv  # noqa: E402

from ingest.source_registry import SourceConfig, log_source_plan, resolve_sources  # noqa: E402
from ingest.vault_creds import CredentialError, read_s3_credentials  # noqa: E402
from ingest.metrics import (  # noqa: E402
    EVENTS_TOTAL,
    LAST_EVENT_TIMESTAMP,
    PIPELINE_ERRORS_TOTAL,
    SOURCES_ACTIVE,
    start_metrics_server,
)

load_dotenv()

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s [%(levelname)s] %(name)s - %(message)s",
)
logger = logging.getLogger("pipeline")


# ---------------------------------------------------------------------------
# Shared configuration (non-source)
# ---------------------------------------------------------------------------

QDRANT_URL = os.environ["QDRANT_URL"]

S3_ACCESS_KEY = os.environ["S3_ACCESS_KEY"]
S3_SECRET_KEY = os.environ["S3_SECRET_ACCESS_KEY"]

EMBEDDING_BASE_URL = os.environ.get("EMBEDDING_BASE_URL", "https://api.openai.com/v1")
EMBEDDING_API_KEY = os.environ["EMBEDDING_API_KEY"]
EMBEDDING_MODEL = os.environ.get("EMBEDDING_MODEL", "text-embedding-3-small")

POLL_INTERVAL = int(os.environ.get("PATHWAY_POLL_INTERVAL_SECS", "30"))
STATE_DIR = os.environ.get("PATHWAY_STATE_DIR", "/var/pathway/state")

VECTOR_DIM = 1536
_MAX_EMBED_CHARS = 30_000
_CHUNK_MAX_CHARS = 8_000
_CHUNK_OVERLAP_CHARS = 200

_openai_client = OpenAI(api_key=EMBEDDING_API_KEY, base_url=EMBEDDING_BASE_URL)


# ---------------------------------------------------------------------------
# Markdown chunking  (unchanged from Phase 0 — still deterministic per doc)
# ---------------------------------------------------------------------------

def _chunk_markdown(text: str) -> list:
    """Split a markdown document into sub-document chunks.

    Primary: header boundaries (H1/H2/H3 outside fenced code blocks).
    Fallback: overlapping fixed-size windows when a section exceeds the
    per-chunk ceiling.
    """
    import re as _re

    if not isinstance(text, str):
        try:
            text = text.decode("utf-8", errors="replace")
        except AttributeError:
            text = str(text)

    in_fence = False
    fence_marker = ""
    header_positions: list = []
    pos = 0
    for line in text.splitlines(keepends=True):
        stripped = line.lstrip()
        if not in_fence:
            if stripped.startswith("```") or stripped.startswith("~~~"):
                in_fence = True
                fence_marker = stripped[:3]
            else:
                m = _re.match(r'^(#{1,3}) (.+)', line)
                if m:
                    header_positions.append((pos, m.group(2).strip()))
        else:
            if stripped.startswith(fence_marker):
                in_fence = False
        pos += len(line)

    sections: list = []
    if not header_positions:
        sections = [("", text)]
    else:
        if header_positions[0][0] > 0:
            preamble = text[:header_positions[0][0]].strip()
            if preamble:
                sections.append(("", preamble))
        for i, (hpos, title) in enumerate(header_positions):
            end = header_positions[i + 1][0] if i + 1 < len(header_positions) else len(text)
            section_text = text[hpos:end].strip()
            if section_text:
                sections.append((title, section_text))

    chunks: list = []
    for section_title, section_text in sections:
        if len(section_text) <= _CHUNK_MAX_CHARS:
            chunks.append({"section": section_title, "text": section_text})
        else:
            start = 0
            while start < len(section_text):
                window = section_text[start:start + _CHUNK_MAX_CHARS]
                if window.strip():
                    chunks.append({"section": section_title, "text": window})
                next_start = start + _CHUNK_MAX_CHARS - _CHUNK_OVERLAP_CHARS
                if next_start <= start:
                    break
                start = next_start

    if not chunks:
        chunks = [{"section": "", "text": text[:_CHUNK_MAX_CHARS]}]

    total = len(chunks)
    for i, chunk in enumerate(chunks):
        chunk["chunk_index"] = i
        chunk["total_chunks"] = total
    return chunks


# ---------------------------------------------------------------------------
# Official Pathway parsers (Round 1 integration)
# ---------------------------------------------------------------------------
# Replaces the former custom `extract_text()` UDF. Two parsers dispatched
# by file extension:
#   - PypdfParser:        PDF — one tuple per page, pure-Python pypdf
#   - UnstructuredParser: DOCX / HTML / TXT / MD (and anything else) —
#     one tuple per file via chunking_mode="single"; downstream
#     chunk_text() then applies header-aware / fixed-window splitting
#     unchanged from Phase 0.
#
# Both return `list[tuple[str, dict]]` per the v0.30.0 parsers.py contract.
# Module-level instances are safe: both classes are pw.udf wrappers.

_unstructured_parser = UnstructuredParser(chunking_mode="single")
_pdf_parser = PypdfParser()


@pw.udf
def _element_text(element) -> str:
    """Extract the text field from a (text, metadata) parser output tuple.

    Pathway flatten() emits the tuple as-is; both parsers guarantee
    shape `(str, dict)`. Defensive fallback for anything else keeps
    the pipeline from crashing on a malformed row.
    """
    if isinstance(element, (list, tuple)) and len(element) >= 1:
        return str(element[0])
    return str(element)


@pw.udf
def chunk_text(text: str) -> list:
    if not isinstance(text, str):
        try:
            s = str(text)
            d = json.loads(s)
            text = d if isinstance(d, str) else s
        except (json.JSONDecodeError, ValueError):
            text = str(text)
    return [c for c in _chunk_markdown(text) if c.get("text", "").strip()]


# ---------------------------------------------------------------------------
# Embedding UDF (unchanged)
# ---------------------------------------------------------------------------

@pw.udf
def embed_chunk(chunk) -> list:
    if isinstance(chunk, dict):
        text = str(chunk.get("text", ""))
    else:
        try:
            d = json.loads(str(chunk))
            text = str(d.get("text", "")) if isinstance(d, dict) else str(d)
        except (json.JSONDecodeError, ValueError):
            text = str(chunk)

    if len(text) > _MAX_EMBED_CHARS:
        text = text[:_MAX_EMBED_CHARS]

    resp = _openai_client.embeddings.create(input=text, model=EMBEDDING_MODEL)
    return resp.data[0].embedding


# ---------------------------------------------------------------------------
# JSON unwrapping helpers (unchanged)
# ---------------------------------------------------------------------------

def _unwrap_json(val: object) -> str:
    if isinstance(val, dict) and "_value" in val:
        inner = val["_value"]
        try:
            decoded = json.loads(inner) if isinstance(inner, str) else inner
            return str(decoded) if decoded is not None else ""
        except (json.JSONDecodeError, ValueError):
            return str(inner) if inner is not None else ""
    if val is None:
        return ""
    s = str(val)
    try:
        decoded = json.loads(s)
        if isinstance(decoded, str):
            return decoded
    except (json.JSONDecodeError, ValueError):
        pass
    return s


def _unwrap_chunk_dict(val: object) -> dict:
    if isinstance(val, dict):
        return val
    if val is None:
        return {}
    try:
        d = json.loads(str(val))
        return d if isinstance(d, dict) else {}
    except (json.JSONDecodeError, ValueError):
        return {}


# ---------------------------------------------------------------------------
# Qdrant sink — now source-aware
# ---------------------------------------------------------------------------

class QdrantSink(pw.io.python.ConnectorObserver):
    """Per-source Qdrant sink.

    Phase-3 change: the sink is constructed with the source's identity
    (`source_id`, `scope_identity`) and the derived Qdrant collection
    name. Every chunk this sink writes carries `source_id` + `scope` in
    its payload. Legacy chunks without `source_id` remain queryable —
    they just don't participate in source-scoped filters.

    Differential dataflow semantics are preserved: `is_addition=False`
    rows for retracted chunks delete the corresponding Qdrant points.
    """

    def __init__(
        self,
        url: str,
        collection: str,
        scope_identity: str,
        source_id: str | None,
    ) -> None:
        self._client = QdrantClient(url=url)
        self._collection = collection
        self._scope_identity = scope_identity
        self._source_id = source_id
        self._ensure_collection()

    def _ensure_collection(self) -> None:
        existing = [c.name for c in self._client.get_collections().collections]
        if self._collection not in existing:
            self._client.create_collection(
                self._collection,
                vectors_config=VectorParams(size=VECTOR_DIM, distance=Distance.COSINE),
            )

    def _row_id(self, key: pw.Pointer) -> int:
        return int(hashlib.sha256(str(key).encode()).hexdigest(), 16) % (2**63)

    def on_change(
        self,
        key: pw.Pointer,
        row: dict,
        time: int,
        is_addition: bool,
    ) -> None:
        point_id = self._row_id(key)
        try:
            if is_addition:
                chunk = _unwrap_chunk_dict(row.get("chunk"))
                chunk_text = str(chunk.get("text", ""))
                section = str(chunk.get("section", ""))
                chunk_index = int(chunk.get("chunk_index", 0))
                total_chunks = int(chunk.get("total_chunks", 1))

                payload = {
                    "document_id": _unwrap_json(row.get("document_id")),
                    # Canonical scope identity for retrieval filters —
                    # unchanged from Phase 0 so legacy and Phase-3 chunks
                    # co-exist under the same filter.
                    "scope": self._scope_identity,
                    "section_path": _unwrap_json(row.get("section_path")),
                    "section": section,
                    "chunk_index": chunk_index,
                    "total_chunks": total_chunks,
                    "text": chunk_text,
                    "status": "active",
                    "document_hash": hashlib.sha256(chunk_text.encode()).hexdigest(),
                    "ingested_at": _time.time(),
                }
                # Phase-3 additive payload field: stable source identity.
                # Synthetic pilot sources do not have a source_id; the field
                # is omitted rather than faked so downstream code can tell.
                if self._source_id:
                    payload["source_id"] = self._source_id

                self._client.upsert(
                    collection_name=self._collection,
                    points=[PointStruct(id=point_id, vector=row["embedding"], payload=payload)],
                )
            else:
                self._client.delete(
                    collection_name=self._collection,
                    points_selector=[point_id],
                )
        except Exception as exc:
            # Qdrant write failed — count the error but do NOT advance success metrics.
            PIPELINE_ERRORS_TOTAL.labels(error_type="qdrant_error").inc()
            logger.error(
                "pipeline: Qdrant write failed scope=%s operation=%s: %s",
                self._scope_identity, "add" if is_addition else "delete", exc,
            )
            raise

        # Metrics advance only after confirmed Qdrant persistence.
        operation = "add" if is_addition else "delete"
        EVENTS_TOTAL.labels(scope=self._scope_identity, operation=operation).inc()
        LAST_EVENT_TIMESTAMP.labels(scope=self._scope_identity).set(_time.time())

    def on_error(self, error: BaseException) -> None:
        # Called by Pathway for framework-level errors (e.g. deserialization failures),
        # distinct from Qdrant write failures caught in on_change().
        PIPELINE_ERRORS_TOTAL.labels(error_type="qdrant_error").inc()
        logger.error(
            "pipeline: QdrantSink framework error scope=%s: %s", self._scope_identity, error
        )

    def on_end(self) -> None:
        pass


# ---------------------------------------------------------------------------
# Per-source subgraph
# ---------------------------------------------------------------------------

def _resolve_source_s3_credentials(source: SourceConfig) -> tuple[str, str]:
    """Pick the AWS key pair the watcher for this source should use.

    Phase 4 contract:
      * `managed_upload` + synthetic pilot → shared env vars. These buckets
        are Agentopia-owned, so one IAM principal covers every watcher.
      * `external_s3`                     → per-source Vault secret at
        `source.credential_ref`. Each customer source runs with its own
        least-privilege credential; a compromise on one is isolated.

    Failure to resolve is raised as CredentialError so the caller can
    skip this source without taking down unrelated watchers.
    """
    if source.is_external:
        if not source.credential_ref:
            # Defence in depth — source_registry already rejects this,
            # but keep the invariant local to the credential path too.
            raise CredentialError(
                f"external_s3 source {source.source_id} has no credential_ref",
            )
        creds = read_s3_credentials(source.credential_ref)
        return creds.access_key, creds.secret_key
    return S3_ACCESS_KEY, S3_SECRET_KEY


def _build_source_subgraph(source: SourceConfig) -> None:
    """Construct the ingest subgraph for one source.

    Each source contributes an independent `pw.io.s3.read(...)` connector
    plus the chunk/embed/sink chain; `pw.run()` evaluates all of them in
    one process. Writing them as separate subgraphs — instead of one
    unioned table — keeps Qdrant sink routing trivial (one sink per
    source, one collection per scope).

    Phase 4: `external_s3` sources get their own S3 credentials resolved
    from Vault at `credential_ref`; Pathway calls `pw.io.s3.read(...)`
    only — never `put_object` / `delete_object` — so the customer's
    bucket is read-only from Agentopia's perspective.
    """
    logger.info(
        "pipeline: building subgraph  source_id=%s  kind=%s  scope=%s  s3://%s/%s",
        source.source_id or "synthetic-env",
        source.kind,
        source.scope_identity,
        source.bucket,
        source.prefix,
    )

    access_key, secret_key = _resolve_source_s3_credentials(source)

    # Round 1: Pathway-native parser path.
    # Connector reads raw bytes (per S3 docstring, `format="binary"` puts
    # bytes in column `data`). Parsers consume `data`; their output shape
    # is list[tuple[str, dict]] which flatten() splits into one row per
    # element. For UnstructuredParser(chunking_mode="single") that is one
    # element per file; for PypdfParser it is one element per page.
    documents_raw = pw.io.s3.read(
        source.prefix,
        aws_s3_settings=pw.io.s3.AwsS3Settings(
            bucket_name=source.bucket,
            region=source.region,
            access_key=access_key,
            secret_access_key=secret_key,
        ),
        format="binary",
        mode="streaming",
        with_metadata=True,
        autocommit_duration_ms=POLL_INTERVAL * 1000,
    )

    # Dispatch: PDFs through PypdfParser (no model load, pure pypdf);
    # everything else through UnstructuredParser. Extension-based split
    # is cheap and deterministic; the same file never goes through both.
    pdf_docs = documents_raw.filter(pw.this._metadata["path"].str.endswith(".pdf"))
    other_docs = documents_raw.filter(~pw.this._metadata["path"].str.endswith(".pdf"))

    pdf_parsed = pdf_docs.select(
        elements=_pdf_parser(pw.this.data),
        document_id=pw.this._metadata["path"],
        section_path=pw.this._metadata["path"],
    )
    other_parsed = other_docs.select(
        elements=_unstructured_parser(pw.this.data),
        document_id=pw.this._metadata["path"],
        section_path=pw.this._metadata["path"],
    )
    all_parsed = pdf_parsed.concat_reindex(other_parsed)

    # Flatten parser output: one row per (text, metadata) tuple.
    elements_flat = all_parsed.flatten(pw.this.elements).select(
        element=pw.this.elements,
        document_id=pw.this.document_id,
        section_path=pw.this.section_path,
    )
    documents = elements_flat.select(
        text=_element_text(pw.this.element),
        document_id=pw.this.document_id,
        section_path=pw.this.section_path,
    )

    documents_with_chunks = documents.select(
        chunks=chunk_text(pw.this.text),
        document_id=pw.this.document_id,
        section_path=pw.this.section_path,
    )

    chunk_rows = documents_with_chunks.flatten(pw.this.chunks)

    embedded = chunk_rows.select(
        chunk=pw.this.chunks,
        document_id=pw.this.document_id,
        section_path=pw.this.section_path,
        embedding=embed_chunk(pw.this.chunks),
    )

    pw.io.python.write(
        embedded,
        QdrantSink(
            url=QDRANT_URL,
            collection=source.qdrant_collection,
            scope_identity=source.scope_identity,
            source_id=source.source_id,
        ),
    )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def build_pipeline(sources: list[SourceConfig] | None = None) -> int:
    """Construct the Pathway dataflow graph.

    Returns the number of source watchers materialised. The caller (or
    the module main) fails fast when zero — trying to `pw.run()` with
    no watchers is a silent no-op in Pathway.
    """
    if sources is None:
        sources = resolve_sources()
    log_source_plan(sources)
    if not sources:
        return 0
    built = 0
    for source in sources:
        try:
            _build_source_subgraph(source)
            built += 1
        except CredentialError as exc:
            # A Vault miss / Vault outage for ONE external_s3 source
            # must not kill unrelated watchers. Log loudly; the next
            # pod restart will retry.
            PIPELINE_ERRORS_TOTAL.labels(error_type="credential_error").inc()
            logger.error(
                "pipeline: skipping source_id=%s kind=%s scope=%s — %s",
                source.source_id, source.kind, source.scope_identity, exc,
            )
    return built


if __name__ == "__main__":
    count = build_pipeline()
    if count == 0:
        logger.error(
            "pipeline: no active managed_upload sources found and no pilot "
            "env fallback is configured — refusing to start a zero-watcher "
            "Pathway process (would silently do nothing)."
        )
        sys.exit(1)

    SOURCES_ACTIVE.set(count)
    start_metrics_server()

    pw.run(
        persistence_config=pw.persistence.Config(
            backend=pw.persistence.Backend.filesystem(STATE_DIR),
            snapshot_interval_ms=30_000,
        ),
    )
