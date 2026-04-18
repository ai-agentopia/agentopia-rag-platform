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
import json
import os
import re
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
# Safety-net truncation guard — normal operation should not hit this after chunking.
_MAX_EMBED_CHARS = 30_000  # ≈ 7500 tokens — fallback ceiling if a chunk is unexpectedly large

# Chunking parameters.
# Primary split: markdown H1/H2/H3 headers (detected outside fenced code blocks).
# Fallback: bounded windows with overlap for sections exceeding _CHUNK_MAX_CHARS.
_CHUNK_MAX_CHARS = 8_000   # ≈ 2000 tokens — ceiling per chunk, well under embedding limit
_CHUNK_OVERLAP_CHARS = 200  # overlap between window sub-chunks for context continuity

_openai_client = OpenAI(api_key=EMBEDDING_API_KEY, base_url=EMBEDDING_BASE_URL)

# ---------------------------------------------------------------------------
# Markdown chunking
# ---------------------------------------------------------------------------

def _chunk_markdown(text: str) -> list:
    """Split a markdown document into sub-document chunks.

    Primary strategy: split at H1 / H2 / H3 header boundaries so each
    section becomes its own chunk.  This preserves semantic coherence for
    structured markdown documents (the corpus standard for utop/oddspark).

    Headers are detected line-by-line with fenced code block tracking so
    that Python/shell comment lines inside code blocks (e.g. "# variable")
    are never mistaken for markdown headers.

    Fallback: when a section exceeds _CHUNK_MAX_CHARS (e.g. a dense code
    block or very long prose section), the section is subdivided into
    overlapping fixed-size windows using _CHUNK_OVERLAP_CHARS of overlap
    to maintain context continuity across the boundary.

    Each returned dict has:
      text         — chunk text (includes the header line when split at header)
      section      — header title string (stripped of leading '#'), or "" for
                     preamble text before the first header
      chunk_index  — 0-based index of this chunk within the document
      total_chunks — total number of chunks for this document
    """
    if not isinstance(text, str):
        # S3 plaintext_by_object delivers str; guard against unexpected bytes.
        try:
            text = text.decode("utf-8", errors="replace")
        except AttributeError:
            text = str(text)

    # Scan line-by-line, tracking fenced code blocks.
    # Headers (H1-H3) are only matched outside fences so that comment lines
    # inside code blocks (e.g. "# python_var", "# shell comment") are ignored.
    in_fence = False
    fence_marker = ""
    header_positions: list = []  # [(char_offset, title), ...]
    pos = 0

    for line in text.splitlines(keepends=True):
        stripped = line.lstrip()
        if not in_fence:
            if stripped.startswith("```") or stripped.startswith("~~~"):
                in_fence = True
                fence_marker = stripped[:3]
            else:
                m = re.match(r'^(#{1,3}) (.+)', line)
                if m:
                    header_positions.append((pos, m.group(2).strip()))
        else:
            if stripped.startswith(fence_marker):
                in_fence = False
        pos += len(line)

    # Build (section_title, section_text) pairs from detected header positions
    sections: list = []
    if not header_positions:
        # No headers — treat the entire document as a single unnamed section.
        sections = [("", text)]
    else:
        # Preamble before the first header
        if header_positions[0][0] > 0:
            preamble = text[:header_positions[0][0]].strip()
            if preamble:
                sections.append(("", preamble))
        for i, (hpos, title) in enumerate(header_positions):
            end = header_positions[i + 1][0] if i + 1 < len(header_positions) else len(text)
            section_text = text[hpos:end].strip()
            if section_text:
                sections.append((title, section_text))

    # Sub-split any section that exceeds the per-chunk ceiling
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
        # Absolute fallback: emit a single chunk with truncated content.
        chunks = [{"section": "", "text": text[:_CHUNK_MAX_CHARS]}]

    # Annotate with positional metadata
    total = len(chunks)
    for i, chunk in enumerate(chunks):
        chunk["chunk_index"] = i
        chunk["total_chunks"] = total

    return chunks


@pw.udf
def chunk_text(text: str) -> list:
    """Pathway UDF: split one document into a list of chunk dicts."""
    if not isinstance(text, str):
        try:
            s = str(text)
            d = json.loads(s)
            text = d if isinstance(d, str) else s
        except (json.JSONDecodeError, ValueError):
            text = str(text)
    return _chunk_markdown(text)


# ---------------------------------------------------------------------------
# Embedding
# ---------------------------------------------------------------------------

@pw.udf
def embed_chunk(chunk) -> list:
    """Embed a single chunk dict (pw.Json from flatten output).

    Receives one chunk dict per row after flatten().  The dict may arrive as
    a plain Python dict (Pathway deserialises pw.Json for UDFs) or as a
    pw.Json object — both forms are handled defensively.

    After sub-document chunking, chunk sizes are well below _MAX_EMBED_CHARS
    in normal operation.  The truncation guard remains as a safety net for
    unexpectedly large sections or malformed input.
    """
    # Deserialise chunk dict — it arrives as Python dict when Pathway
    # deserialises the pw.Json column, but handle raw pw.Json defensively.
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
# JSON unwrapping helpers
# ---------------------------------------------------------------------------

def _unwrap_json(val: object) -> str:
    """Unwrap a Pathway Json column value from its ConnectorObserver row form.

    Pathway delivers pw.Json-typed columns (e.g. pw.this._metadata["path"]) as
    one of three forms in on_change row dicts:
      Form A — dict wrapper:  {"_value": '"path/to/file.md"'}
      Form B — plain str:     '"path/to/file.md"'  (raw JSON text, Python str type)
      Form C — pw.Json obj:   str(val) == '"path/to/file.md"'  (not a plain str/dict)

    In all cases str(val) produces the JSON-encoded representation of the value,
    so json.loads(str(val)) is the correct canonical decoder.  Form A is handled
    via the dict branch for safety; Forms B and C fall through to json.loads.
    """
    if isinstance(val, dict) and "_value" in val:
        # Form A: unwrap the _value key, then json-decode to strip JSON encoding.
        inner = val["_value"]
        try:
            decoded = json.loads(inner) if isinstance(inner, str) else inner
            return str(decoded) if decoded is not None else ""
        except (json.JSONDecodeError, ValueError):
            return str(inner) if inner is not None else ""
    if val is None:
        return ""
    # Forms B and C: str() of the value is a JSON-encoded string.
    # json.loads strips the surrounding double-quote characters produced by
    # Pathway's JSON serialization of string values.
    s = str(val)
    try:
        decoded = json.loads(s)
        if isinstance(decoded, str):
            return decoded
    except (json.JSONDecodeError, ValueError):
        pass
    return s


def _unwrap_chunk_dict(val: object) -> dict:
    """Extract a Python dict from a pw.Json chunk column in on_change.

    After flatten(), the chunk column (pw.Json) arrives in on_change as
    either a plain Python dict (Pathway deserialises for ConnectorObserver)
    or as a pw.Json object whose str() is a JSON-encoded dict.
    """
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
# Qdrant sink
# ---------------------------------------------------------------------------

class QdrantSink(pw.io.python.ConnectorObserver):
    """Upserts chunk embeddings into a Qdrant collection.

    Each Qdrant point represents one sub-document chunk.  Pathway's
    differential dataflow ensures correct retraction (deletion) semantics:
    when a source document is deleted or updated, all chunk rows derived
    from that document are retracted by Pathway, and on_change is called
    with is_addition=False for each chunk — triggering the corresponding
    Qdrant deletes automatically.

    Point identity is stable per (document, chunk_index): Pathway's flatten
    operation assigns each chunk a compound row key (original_doc_key +
    list_position), which _row_id maps to a stable 63-bit Qdrant point ID.
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
        # Pathway Pointer (compound after flatten: doc_key + chunk_index position)
        # → stable 63-bit int suitable for Qdrant point ID.
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
            # chunk column contains the full chunk dict (pw.Json).
            # _unwrap_chunk_dict handles both Python-dict and pw.Json-object forms.
            chunk = _unwrap_chunk_dict(row.get("chunk"))
            chunk_text = str(chunk.get("text", ""))
            section = str(chunk.get("section", ""))
            chunk_index = int(chunk.get("chunk_index", 0))
            total_chunks = int(chunk.get("total_chunks", 1))

            self._client.upsert(
                collection_name=self._collection,
                points=[
                    PointStruct(
                        id=point_id,
                        vector=row["embedding"],
                        payload={
                            "document_id": _unwrap_json(row.get("document_id")),
                            "scope": SCOPE_IDENTITY,
                            "section_path": _unwrap_json(row.get("section_path")),
                            "section": section,
                            "chunk_index": chunk_index,
                            "total_chunks": total_chunks,
                            "text": chunk_text,
                            "status": "active",
                            # document_hash: SHA-256 of this chunk's text — enables
                            # chunk-level deduplication and change detection.
                            "document_hash": hashlib.sha256(
                                chunk_text.encode()
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
    # pw.this._metadata["path"] produces a pw.Json-typed column. When delivered
    # to ConnectorObserver.on_change it arrives as {"_value": "s3/key.md"}.
    # _unwrap_json() in on_change extracts the plain string from that wrapper.
    documents = documents.select(
        text=pw.this.data,
        document_id=pw.this._metadata["path"],
        section_path=pw.this._metadata["path"],
    )

    # -- Transform: chunk each document into sub-document pieces --------------
    # chunk_text() returns list[dict] (section, text, chunk_index, total_chunks).
    # flatten() expands that list so each chunk becomes its own Pathway row.
    # After flatten, pw.this.chunks is one chunk dict (pw.Json) per row.
    #
    # Row identity design:
    # Pathway assigns compound row keys (original_doc_key + list_position) after
    # flatten().  This means each chunk's Qdrant point ID is stable per
    # (document, chunk_index).  When a source document is deleted or updated,
    # Pathway retracts all derived chunk rows — QdrantSink.on_change receives
    # is_addition=False for each chunk and deletes the corresponding point.
    #
    # Important: Pathway 0.30.0 does not support string-key indexing on pw.Json
    # columns in select() expressions (only integer indexing on arrays).  To
    # avoid this limitation, the full chunk dict is passed as a single column
    # ("chunk") to the sink and embedded via embed_chunk().  Field extraction
    # (section, chunk_index, total_chunks, text) happens inside the UDF and
    # inside on_change(), where plain Python dict access is available.
    documents_with_chunks = documents.select(
        chunks=chunk_text(pw.this.text),
        document_id=pw.this.document_id,
        section_path=pw.this.section_path,
    )

    chunk_rows = documents_with_chunks.flatten(pw.this.chunks)

    # -- Transform: embed each chunk ------------------------------------------
    # embed_chunk() receives the full chunk dict (pw.Json) and extracts the
    # "text" field internally to avoid pw.Json string-key indexing in the graph.
    embedded = chunk_rows.select(
        chunk=pw.this.chunks,
        document_id=pw.this.document_id,
        section_path=pw.this.section_path,
        embedding=embed_chunk(pw.this.chunks),
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
