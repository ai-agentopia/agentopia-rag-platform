"""Pathway ingest pipeline — agentopia-rag-platform Phase 3.

Entry point for the differential dataflow ingest pipeline.
Reads documents from S3, chunks + embeds them, and upserts into Qdrant.

All configuration is via environment variables (set from K8s Secrets):
  QDRANT_URL           — e.g. http://qdrant:6333
  QDRANT_COLLECTION    — target collection name, e.g. kb-<scope_hash>
  SCOPE_IDENTITY       — human-readable scope, e.g. utop/oddspark (default: QDRANT_COLLECTION)
  S3_BUCKET_NAME       — source S3 bucket
  S3_PREFIX            — object prefix to poll, e.g. scopes/{scope}/
  S3_REGION            — AWS region
  S3_ACCESS_KEY        — AWS access key ID (from K8s Secret)
  S3_SECRET_ACCESS_KEY — AWS secret access key (from K8s Secret)
  EMBEDDING_BASE_URL   — OpenAI-compatible base URL (e.g. https://api.openai.com/v1)
  EMBEDDING_API_KEY    — API key for the embedding endpoint
  EMBEDDING_MODEL      — model name, default text-embedding-3-small
  PATHWAY_POLL_INTERVAL_SECS — S3 poll interval in seconds, default 30
  PATHWAY_STATE_DIR    — path for persistence state PVC, default /var/pathway/state
"""

import hashlib
import os
import time as _time

import pathway as pw
from openai import OpenAI
from qdrant_client import QdrantClient
from qdrant_client.models import Distance, PointStruct, VectorParams

from dotenv import load_dotenv

load_dotenv()

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

QDRANT_URL = os.environ["QDRANT_URL"]
QDRANT_COLLECTION = os.environ["QDRANT_COLLECTION"]

S3_BUCKET = os.environ["S3_BUCKET_NAME"]
S3_PREFIX = os.environ.get("S3_PREFIX", "")
S3_REGION = os.environ.get("S3_REGION", "us-east-1")
S3_ACCESS_KEY = os.environ["S3_ACCESS_KEY"]
S3_SECRET_KEY = os.environ["S3_SECRET_ACCESS_KEY"]

EMBEDDING_BASE_URL = os.environ.get("EMBEDDING_BASE_URL", "https://api.openai.com/v1")
EMBEDDING_API_KEY = os.environ["EMBEDDING_API_KEY"]
EMBEDDING_MODEL = os.environ.get("EMBEDDING_MODEL", "text-embedding-3-small")

POLL_INTERVAL = int(os.environ.get("PATHWAY_POLL_INTERVAL_SECS", "30"))
STATE_DIR = os.environ.get("PATHWAY_STATE_DIR", "/var/pathway/state")
# Human-readable scope identity stored in Qdrant payload (e.g. "utop/oddspark").
# Defaults to QDRANT_COLLECTION (collection hash) if not explicitly set.
# Set SCOPE_IDENTITY to the source scope for correct gateway retrieval filtering.
SCOPE_IDENTITY = os.environ.get("SCOPE_IDENTITY", QDRANT_COLLECTION)

VECTOR_DIM = 1536  # text-embedding-3-small output dimension
# text-embedding-3-small max input: 8191 tokens (~4 chars/token).
# Truncate to stay under limit; for production use a chunking pass.
_MAX_EMBED_CHARS = 30_000  # ≈ 7500 tokens — safe margin for the 8191-token limit

_openai_client = OpenAI(api_key=EMBEDDING_API_KEY, base_url=EMBEDDING_BASE_URL)


@pw.udf
def embed_text(text: str) -> list:
    """Embed a single text chunk using the configured OpenAI-compatible endpoint.

    Truncates input to _MAX_EMBED_CHARS to stay within text-embedding-3-small's
    8191-token limit. OpenRouter returns data:[] (not an HTTP error) for over-limit
    inputs, which the openai client surfaces as ValueError('No embedding data received').
    """
    if len(text) > _MAX_EMBED_CHARS:
        text = text[:_MAX_EMBED_CHARS]
    resp = _openai_client.embeddings.create(input=text, model=EMBEDDING_MODEL)
    return resp.data[0].embedding


@pw.udf
def extract_path(meta: dict) -> str:
    """Extract the S3 object key from Pathway _metadata as a plain string.

    pw.this._metadata["path"] returns a Pathway ColumnReference that serialises
    as {"_value": "..."} when passed directly through select(). This UDF unwraps
    the value so document_id and section_path are stored as strings in Qdrant.
    """
    return meta.get("path", "")


# ---------------------------------------------------------------------------
# Qdrant sink
# ---------------------------------------------------------------------------

class QdrantSink(pw.io.python.ConnectorObserver):
    """Upserts embeddings into a Qdrant collection.

    Handles addition and retraction (deletion) events from Pathway's
    differential dataflow, preserving the single-publisher invariant.
    """

    def __init__(self, url: str, collection: str) -> None:
        self._client = QdrantClient(url=url)
        self._collection = collection
        self._ensure_collection()

    def _ensure_collection(self) -> None:
        existing = [c.name for c in self._client.get_collections().collections]
        if self._collection not in existing:
            self._client.create_collection(
                self._collection,
                vectors_config=VectorParams(size=VECTOR_DIM, distance=Distance.COSINE),
            )

    def _row_id(self, key: pw.Pointer) -> int:
        # Pathway Pointer → stable 63-bit int suitable for Qdrant point ID
        return int(hashlib.sha256(str(key).encode()).hexdigest(), 16) % (2**63)

    def on_change(
        self,
        key: pw.Pointer,
        row: dict,
        time: int,
        is_addition: bool,
    ) -> None:
        point_id = self._row_id(key)
        if is_addition:
            self._client.upsert(
                collection_name=self._collection,
                points=[
                    PointStruct(
                        id=point_id,
                        vector=row["embedding"],
                        payload={
                            "document_id": row.get("document_id", ""),
                            "scope": SCOPE_IDENTITY,
                            "section_path": row.get("section_path", ""),
                            "text": row.get("text", ""),
                            "status": "active",
                            # document_hash: SHA-256 of stored text — enables deduplication
                            # and change detection without re-fetching from S3.
                            "document_hash": hashlib.sha256(
                                row.get("text", "").encode()
                            ).hexdigest(),
                            # ingested_at: wall-clock Unix timestamp (seconds) when this
                            # chunk was written to Qdrant. Pathway's `time` parameter is a
                            # logical batch epoch in ms (autocommit_duration_ms cycles) —
                            # NOT wall-clock time. _time.time() is the correct source for
                            # freshness lag measurement (P3.5).
                            "ingested_at": _time.time(),
                        },
                    )
                ],
            )
        else:
            self._client.delete(
                collection_name=self._collection,
                points_selector=[point_id],
            )

    def on_end(self) -> None:
        pass


# ---------------------------------------------------------------------------
# Pipeline
# ---------------------------------------------------------------------------

def build_pipeline() -> None:
    # -- Source: S3 polling connector ----------------------------------------
    documents = pw.io.s3.read(
        S3_PREFIX,
        aws_s3_settings=pw.io.s3.AwsS3Settings(
            bucket_name=S3_BUCKET,
            region=S3_REGION,
            access_key=S3_ACCESS_KEY,
            secret_access_key=S3_SECRET_KEY,
        ),
        format="plaintext_by_object",
        mode="streaming",
        with_metadata=True,
        autocommit_duration_ms=POLL_INTERVAL * 1000,
    )

    # -- Transform: derive document_id and section_path from S3 object key ----
    # extract_path UDF unwraps pw.this._metadata to a plain string.
    # Direct pw.this._metadata["path"] serialises as {"_value": "..."} (dict bug).
    documents = documents.select(
        text=pw.this.data,
        document_id=extract_path(pw.this._metadata),
        section_path=extract_path(pw.this._metadata),
    )

    # -- Transform: embed each document chunk ---------------------------------
    embedded = documents.select(
        **documents,
        embedding=embed_text(pw.this.text),
    )

    # -- Sink: Qdrant upsert --------------------------------------------------
    pw.io.python.write(embedded, QdrantSink(url=QDRANT_URL, collection=QDRANT_COLLECTION))


if __name__ == "__main__":
    build_pipeline()

    pw.run(
        persistence_config=pw.persistence.Config(
            backend=pw.persistence.Backend.filesystem(STATE_DIR),
            snapshot_interval_ms=30_000,
        ),
    )
