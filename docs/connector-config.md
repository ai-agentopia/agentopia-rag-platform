# Pathway Connector Configuration — Pilot Scope

> Phase: P3.2  
> Pilot scope: `joblogic-kb/api-docs`

---

## S3 Connector Config (Pilot)

| Parameter | Value |
|---|---|
| `S3_BUCKET_NAME` | `agentopia-rag-ingest` |
| `S3_PREFIX` | `scopes/joblogic-kb/api-docs/` |
| `S3_REGION` | `ap-southeast-1` |
| `PATHWAY_POLL_INTERVAL_SECS` | `30` |
| `QDRANT_COLLECTION` | `kb-1f28b6baebe4f76d` |
| `EMBEDDING_MODEL` | `text-embedding-3-small` |

QDRANT_COLLECTION is derived as `kb-{sha256("joblogic-kb/api-docs")[:16]}` per the collection naming convention in `migration-plan.md §6`.

## S3 Prefix Convention

```
s3://{S3_BUCKET_NAME}/scopes/{client_id}/{scope_name}/{filename}
```

All documents for a scope are stored under `scopes/{client_id}/{scope_name}/`. Pathway monitors this prefix for additions, updates, and deletions. Documents written to the staging bucket by the upload API or the GitHub Actions sync workflow land here.

**Pilot scope path**: `s3://agentopia-rag-ingest/scopes/joblogic-kb/api-docs/`

## Document Format (Round 1 — Pathway-native parsers)

The pipeline reads S3 objects with `format="binary"` and routes bytes to
official Pathway parsers (`pathway.xpacks.llm.parsers`):

| Extension | Parser | Chunking |
|---|---|---|
| `.pdf` (text-based) | `PypdfParser` | one page per parsed element |
| `.docx`, `.html`, `.txt`, `.md` | `UnstructuredParser(chunking_mode="single")` | one element per file; downstream `chunk_text()` applies header-aware splitting for MD, fixed-window fallback for others |

The former markdown-only / `plaintext_by_object` pilot path is retired.
The former custom `text_extract.py` UDF is retired. Scanned / image PDFs
and unsupported types (`.xlsx`, `.pptx`, images, archives) are NOT
handled by this pipeline in Round 1 — they are rejected at the upload
boundary in bot-config-api (415 Unsupported Media Type).

## Credentials (K8s Secrets)

In production, these env vars are set via K8s Secrets — never hardcoded:

```
S3_ACCESS_KEY            → Secret: agentopia-rag-platform-s3
S3_SECRET_ACCESS_KEY     → Secret: agentopia-rag-platform-s3
EMBEDDING_API_KEY        → Secret: agentopia-rag-platform-embedding
```

See `.env.example` for the full env var list.

## Pathway Persistence

The state PVC must be mounted at `PATHWAY_STATE_DIR` (default `/var/pathway/state`). Without the PVC, Pathway cold-starts on every pod restart and re-indexes all documents from S3. The PVC preserves checkpoint state across restarts, allowing incremental operation.

PVC provisioning is tracked in P3.1 remaining cluster work.

## Single-Publisher Invariant

Once `scope_ingest_mode: pathway` is set for `joblogic-kb/api-docs`:
- `POST /api/v1/upload` on knowledge-ingest returns `409 Conflict` for this scope
- Pathway is the sole writer to `kb-1f28b6baebe4f76d`
- Legacy orchestrator does not run for this scope

The guard is implemented in `agentopia-knowledge-ingest` upload endpoint (P3.2 cross-repo work).
