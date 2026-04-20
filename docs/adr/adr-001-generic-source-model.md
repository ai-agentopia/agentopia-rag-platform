---
title: "ADR-001: Generic knowledge source model (external_s3 + managed_upload)"
status: "Accepted"
date: "2026-04-19"
supersedes: "docs/github-ingestion-sync.md (deleted)"
---

# ADR-001 — Generic knowledge source model

## Status

Accepted. Replaces the earlier `docs/github-ingestion-sync.md` contract,
which positioned a GitHub Actions → S3 workflow as a first-class ingress
path. Operator decision on 2026-04-19: GitHub-specific sync is NOT the
product direction. Rag-platform issue #26 is closed as deferred.

## Context

Two classes of customer-facing ingest have emerged from the pilot:

1. **Operators uploading files through the product UI** — today this
   lands in bot-config-api's `POST /api/v1/knowledge/{scope}/ingest`,
   which stages the object in S3 and returns a 202 + `upload_id`
   poll-token (P4.5).
2. **Corpora that already live in object storage** — customer-owned S3
   buckets, pre-existing document dumps, scheduled batch outputs from
   other systems.

The pilot collapsed both into a single pair of scope-level fields
(`s3_bucket`, `s3_prefix`, `s3_region` on `knowledge_bases`) plus one
Pathway deployment per scope. That works for exactly one source per
scope, and it leaks pilot values — repo `ai-agentopia/docs`, prefix
`architecture/`, scope `utop/oddspark` — into the product architecture.
Any second source would collide on the same prefix and the same
`document_id` namespace.

The GitHub Actions sync path was a symptom of that leak: it only made
sense if GitHub was implicitly the source, which it is not.

## Decision

Agentopia's knowledge ingest offers exactly two first-class ingress
options, and they converge on the same backbone:

```
 ingress 1: managed_upload     ─┐
                                ├──▶  object storage  ──▶  Pathway  ──▶  Qdrant
 ingress 2: external_s3        ─┘
```

Both are represented by the same entity — `KnowledgeSource` — and share
the same downstream pipeline. Connectors we may want later (GDrive,
SharePoint, Airbyte-fronted importers, scheduled scrapers) do **not**
invent parallel ingest semantics: they either (a) write into the
Agentopia managed bucket and become a `managed_upload` source under the
hood, or (b) expose their output as an S3-compatible bucket/prefix and
become an `external_s3` source.

**Pathway stays the single ingest engine.** Direct-to-Pathway HTTP
ingest is explicitly rejected.

## Source entity

```
KnowledgeSource
  source_id         UUID              server-assigned, stable
  scope_id          FK → knowledge_bases(client_id, scope_name)
  kind              enum              "managed_upload" | "external_s3"
  display_name      str               operator-facing label
  status            enum              "provisioning"
                                      | "active"
                                      | "paused"
                                      | "error"
                                      | "deprovisioning"
  storage_ref       jsonb             shape depends on `kind` (see below)
  credential_ref    str | null        Vault path when kind = external_s3
  created_at, updated_at, created_by
```

`storage_ref` shape by kind:

```
kind = managed_upload:
  {
    "bucket": "agentopia-knowledge-<env>",
    "prefix": "scopes/{scope_id}/sources/{source_id}/",
    "region": "ap-northeast-1"
  }

kind = external_s3:
  {
    "bucket": "<customer-owned>",
    "prefix": "<customer-chosen>",
    "region": "<customer region>",
    "role_arn": "<optional, for IAM cross-account assume>"
  }
```

### Lifecycle

| Transition | Trigger | Side effects |
|---|---|---|
| `provisioning → active` | Source created, credentials validated, Pathway worker picked up config | Watcher reads empty prefix, no chunks yet |
| `active → paused` | Operator action | Pathway worker keeps its position but stops emitting |
| `active → error` | Credential expiry, bucket access revoked, repeated read failure | Operator alerted; no silent drift |
| `active → deprovisioning → gone` | Operator delete | Retract all chunks where `source_id = …`, free Pathway watcher, revoke/forget credential_ref |

Deprovisioning is explicit and reversible until the retraction commits.

## Scope ↔ source relationship

`1 scope : N sources`. A scope's retrieval is the union of all its
sources' indexed content. This is what lets a client have one
`managed_upload` source for operator edits and an `external_s3` source
for a customer-maintained corpus, under the same retrieval scope.

Reverse direction — `1 source : 1 scope`. A source never straddles
scopes. If customer A and customer B need the same corpus, they declare
two sources pointing at the same external bucket.

## Document identity

```
document_id  =  {source_id}::{relative_key}
```

- `relative_key` = S3 object key with the source's `storage_ref.prefix`
  trimmed. So a managed-upload source `abc123` uploading `doc.md` lands
  at `scopes/<scope_id>/sources/abc123/doc.md` but carries
  `document_id = abc123::doc.md`.
- `::` is chosen as delimiter because it cannot appear in a UUID and is
  unusual inside S3 keys; a key that genuinely contains `::` is still
  unambiguous because `source_id` is a fixed-width UUID.
- Every Qdrant payload additionally carries `source_id` and `scope_id`
  as first-class fields so filters can run server-side without parsing
  the `document_id`.

Why this works:

- **Collision-free across sources.** Two managed_upload sources under
  the same scope never collide on keys even if operators upload
  `readme.md` to both.
- **Stable under external-repo layout churn.** External_s3 sources
  derive `document_id` from the object's relative path; renaming a key
  is a delete + add, cleanly reflected downstream.
- **Cheap source-wide retraction.** Deleting a source = scroll + delete
  by `source_id` filter.

## Storage layout

Managed bucket (Agentopia-owned, one per environment / region):

```
s3://agentopia-knowledge-{env}/
    scopes/
        {scope_id}/
            sources/
                {source_id}/
                    <arbitrary operator-chosen key hierarchy>
```

External S3 buckets stay under whatever layout the customer already
uses. Agentopia never writes into an external_s3 source's bucket.

Delete boundaries are a hard property of this layout:

- A managed_upload source can only issue `PutObject` / `DeleteObject`
  against its own `scopes/{scope_id}/sources/{source_id}/*` prefix
  (enforced in the bot-config-api route and in the IAM policy attached
  to the managed-upload credential).
- An external_s3 source is read-only from Agentopia's perspective.
  Agentopia never calls `DeleteObject` on an external bucket. Retraction
  of an external source = Pathway stops watching + Qdrant cleanup; the
  customer's data is untouched.

## Credential model

| Kind | Credential shape | Storage | Rotation |
|---|---|---|---|
| managed_upload | One central IAM principal per environment, policy scoped to `arn:aws:s3:::agentopia-knowledge-<env>/scopes/*` | K8s Secret `agentopia-managed-upload-s3` | Operator-initiated, pod rollout |
| external_s3 | Either access-key pair OR an IAM role ARN for cross-account assume | Vault `secret/data/agentopia/sources/{source_id}` | Per-source; failure transitions source to `error` state |

The managed_upload credential is shared across all managed sources in a
cluster because its IAM scope is already tight (single bucket, a
wildcard on the safe prefix).

## Operator / API surface

New routes under `bot-config-api` (design, not implementation — shipped
by future issues):

```
POST   /api/v1/knowledge/{scope}/sources              create a source
GET    /api/v1/knowledge/{scope}/sources              list sources for a scope
GET    /api/v1/knowledge/{scope}/sources/{source_id}  inspect
PATCH  /api/v1/knowledge/{scope}/sources/{source_id}  display_name, paused, creds
DELETE /api/v1/knowledge/{scope}/sources/{source_id}  deprovision (async)
```

Existing upload and status routes evolve:

```
POST /api/v1/knowledge/{scope}/ingest
  → becomes a convenience route for a scope's default managed_upload
    source. Can optionally accept ?source_id= to pick among multiple.

GET  /api/v1/knowledge/{scope}/uploads/{upload_id}/status
  → stays; upload_id is already fully-qualified by the source prefix.
```

The existing UI upload flow (P4.5) keeps working unchanged during the
transition (see "Migration" below).

## Pathway implications

Pathway today runs one deployment per scope with `S3_PREFIX` baked in.
Under the generic model, Pathway runs one **watcher per source**:

- Shared pool deployment reads a `knowledge_sources` table and
  materialises one `pw.io.s3.read(...)` per active source.
- Each watcher tags its output rows with the source's `source_id` and
  `scope_id` so downstream the same pipeline can union many sources
  into one Qdrant collection per scope.
- Worker restart re-reads the source table; adding or removing a source
  is a database operation, not a pod rollout.

Direct-to-Pathway HTTP ingest remains rejected: Pathway is the reader,
not the write API.

## Generic-by-design check

The model must generalise to likely future ingresses. Spot-check:

| Potential connector | Fits as | Notes |
|---|---|---|
| GitHub Actions sync (reversed #26 decision) | `managed_upload` source written by a CI job holding managed credentials | Workflow uploads to the Agentopia bucket, same contract as an operator |
| Google Drive | Either `managed_upload` (drive-puller writes to managed bucket) or `external_s3` (if the operator has an S3-compatible mirror) | Never a parallel ingest path |
| Airbyte | Map Airbyte output streams to JSONL in managed bucket → `managed_upload` source | Consistent with the structured-record direction, just without the Pathway-Airbyte direct wiring |
| SharePoint | Same as Drive | — |

No foreseeable connector forces a third kind.

## Migration plan (pilot → generic)

Current pilot state (2026-04-19):

- `knowledge_bases` table has scope-level `s3_bucket`, `s3_prefix`,
  `s3_region` (added in P4.5). One scope = one implicit source.
- Pilot values: bucket `utop-oddspark-document`, prefix `architecture/`,
  region `ap-northeast-1`, scope `utop/oddspark`.
- One Pathway deployment bound to those values.
- Operator upload and retrieval already work.

### Phase 0 — keep the pilot running (this round)

- Nothing live breaks. No schema change. No Pathway change.
- GitHub-specific artifacts removed (`ai-agentopia/docs#18` merged) but
  the existing S3 objects and the existing Qdrant collection remain
  intact — the pilot reads the same state, just no longer pushes from
  GitHub.

### Phase 1 — introduce the sources table (schema-only)

- New migration adds `knowledge_sources` with the columns above.
- Backfill: one row per existing scope, `kind='managed_upload'`,
  `storage_ref` built from the existing scope-level `s3_bucket`/
  `s3_prefix`/`s3_region`, `source_id = uuid()`, `display_name` =
  scope name.
- Add `source_id text` to the Qdrant payload contract (optional, not
  written yet).

### Phase 2 — API shims for managed_upload

- `POST /ingest` writes to the source's `storage_ref.prefix` and tags
  outgoing `upload_id = {source_id}::{relative_key}`.
- Status endpoint already works because `upload_id` is a path
  parameter.
- New optional `?source_id=` on the ingest route — defaulted to the
  scope's sole pre-existing managed_upload source for backward
  compatibility.

### Phase 3 — Pathway source registry

- Pathway reads `knowledge_sources` instead of static env vars.
- One watcher per active source; output tagged with `source_id` +
  `scope_id`.
- Backward compat: legacy chunks (no `source_id` payload field) are
  treated as belonging to the scope's default managed_upload source.

### Phase 4 — external_s3 support

- API + Vault credential path for `kind='external_s3'`.
- Read-only watcher; no write routes exposed.

### Phase 5 — retire legacy columns ✅ Done (2026-04-20)

- `s3_bucket`, `s3_prefix`, `s3_region` dropped from `knowledge_bases`
  via migration `030_drop_kb_s3_columns.sql` (with precondition guard).
- `_load_legacy_kb_s3()` deleted from bot-config-api.
- `_resolve_storage_ref()` and `_resolve_upload_target()` are now
  source-row-only; zero active managed_upload sources → HTTP 404.
- **Option A compatibility strategy chosen**: no global re-ingest.
  Legacy Qdrant chunks (without `source_id` payload) remain readable
  via scope-level collection filter indefinitely.
- `knowledge_sources` is now the **sole source-of-truth** for ingest
  configuration. The legacy `knowledge_bases.s3_*` columns no longer
  exist in the schema.

## What this ADR does NOT decide

- Exact UI operator surface for source CRUD — out of scope for this
  ADR, future issue.
- Pathway deployment topology change (one-per-source vs one-pool) —
  addressed in Phase 3 with preference for one-pool.
- Exactly how Airbyte, Drive, SharePoint get onto the ramp —
  deferred until a real customer requirement materialises. The model
  already accommodates them.
- Whether the managed bucket should be per-region or per-cluster —
  operational detail for Phase 2.

## Non-goals (permanent)

- Direct-to-Pathway HTTP ingest endpoint.
- GitHub-specific connector as a first-class source type.
- Hard-coding any pilot-specific bucket / prefix / scope into the
  product codepath.

## References

- rag-platform issue #26 (closed, deferred): GitHub → S3 sync was the
  symptom-level implementation that forced this architectural
  correction.
- bot-config-api P4.5 work: current managed_upload implementation.
- Pathway pipeline: `src/ingest/pipeline.py` — the ingest backbone all
  sources must funnel through.
