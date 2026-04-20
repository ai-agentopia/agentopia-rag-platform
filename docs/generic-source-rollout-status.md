---
title: "Generic knowledge source rollout status"
description: "Live state of the ADR-001 phased rollout. Updated each time a phase lands."
---

# Generic knowledge source rollout — status

Tracks the ADR-001 migration as it lands, so anyone reading the repo
can tell what is wired in production versus what is still design. The
five-phase shape is defined in
[`docs/adr/adr-001-generic-source-model.md`](adr/adr-001-generic-source-model.md).

| Phase | Summary | Status |
|---|---|---|
| 0 | GitHub-specific artifacts removed; ADR-001 accepted | ✅ Done (2026-04-19) |
| 1 | `knowledge_sources` schema + backfill + read-only control plane + upload-path shim | ✅ Done (2026-04-19) |
| 2 | Source-aware ingest contract: explicit `?source_id=`, canonical `upload_id` (`source:{id}:{rel_key}`), `legacy_upload_id` compat, ambiguity 409, source `writable`/`is_default` annotations | ✅ Done (2026-04-19) |
| **3** | Pathway runtime reads `knowledge_sources` at startup, one watcher per active `managed_upload` source, `source_id` in Qdrant payload, legacy chunks readable in-place | ✅ Done (2026-04-20) |
| **4** | `external_s3` end-to-end (per-source Vault creds, read-only watchers, automatic watcher teardown on deprovision) | ✅ Done (2026-04-20) |
| 5 | Retire scope-level `knowledge_bases.s3_*` columns | ⏳ Not started |

## Phase 1 — what actually shipped

Repo: `agentopia-protocol`, merged as PR
[#446](https://github.com/ai-agentopia/agentopia-protocol/pull/446) on
branch `dev`. Deployed via the existing ArgoCD Image Updater flow —
bot-config-api picks up `dev-39deb12` automatically.

### Schema

Migration `bot-config-api/db/029_knowledge_sources.sql` adds:

- `knowledge_source_kind` enum (`managed_upload`, `external_s3`).
- `knowledge_source_status` enum (`provisioning`, `active`, `paused`,
  `error`, `deprovisioning`).
- `knowledge_sources` table keyed by a generated UUID `source_id`,
  with a foreign key `(client_id, scope_name) → knowledge_bases` and
  `ON DELETE CASCADE`.
- Two indexes: `idx_knowledge_sources_scope (client_id, scope_name)`
  for the upload-path default lookup, and
  `idx_knowledge_sources_kind_status (kind, status)` for the future
  Phase-3 Pathway registry reads.
- Idempotent backfill: every scope that already has a populated
  `(s3_bucket, s3_prefix, s3_region)` triple gets one
  `managed_upload` row tagged `legacy_backfill: true`, created_by
  `system:adr-001-phase1`. Re-running the migration is a no-op.

The legacy `knowledge_bases.s3_bucket / s3_prefix / s3_region`
columns **remain in place** as a compatibility shim until Phase 5.

### Control-plane read model

Two new read-only routes on bot-config-api, same dual-auth as
`/documents`:

- `GET /api/v1/knowledge/{scope}/sources` — list rows for a scope
  (empty if not yet backfilled).
- `GET /api/v1/knowledge/{scope}/sources/{source_id}` — single row;
  404 on scope mismatch.

`credential_ref` is scrubbed from every response.

### Upload path — additive shim

`POST /api/v1/knowledge/{scope}/ingest` and
`GET /api/v1/knowledge/{scope}/uploads/{upload_id}/status` now call
the same resolver:

1. Prefer the oldest `status='active'` `managed_upload` row in
   `knowledge_sources`.
2. Fall back to the legacy `knowledge_bases.s3_*` columns.
3. Raise the same 404 / 409 as before when neither path yields a
   complete `(bucket, prefix, region)`.

The upload response gains an optional `source_id` field when the
resolver used a sources-table row. Existing clients tolerate both its
presence and absence — the UI already ignores unknown fields.

### What Phase 1 deliberately does NOT change

- Pathway pipeline still reads a single S3 prefix via the same
  hard-coded env block. Per-source watchers arrive in Phase 3.
- `upload_id` is still the raw S3 key. The `{source_id}::{relative_key}`
  identity format lands in Phase 2.
- No write CRUD on `knowledge_sources` from the API — sources are
  produced by the migration only in Phase 1.
- No UI change.

### Evidence

- 18 new pytest cases (`test_knowledge_sources_phase1.py`) pass.
- 115 existing knowledge / upload tests still pass (zero regression).
- Migration 029 is applied on bot-config-api startup via the
  existing `db_migrate.run_migrations()` mechanism — first boot
  after rollout backfills the pilot scope `utop/oddspark` into a
  source row, subsequent boots are no-ops.

## Phase 2 — what actually shipped

Repo: `agentopia-protocol`, merged as PR
[#447](https://github.com/ai-agentopia/agentopia-protocol/pull/447) on
branch `dev`. Deployed via the existing ArgoCD Image Updater flow —
bot-config-api picks up `dev-5c974fc` automatically.

### Ingest contract

`POST /api/v1/knowledge/{scope}/ingest`
- Optional `?source_id=<uuid>` query parameter.
- Explicit source validation: 404 unknown, 403 cross-scope,
  422 on `external_s3`, 409 on non-active or incomplete storage_ref.
- Implicit resolution — deterministic, no guessing:
  * one active `managed_upload` source → auto-select
  * zero → legacy `knowledge_bases.s3_*` fallback
  * two or more → 409 with a message that names the candidate
    source_ids and tells the caller to pass `?source_id=`.

### Canonical upload identity

`upload_id = source:{source_id}:{relative_key}` when a source row
was resolved. Operators see one shape that is stable per source.

The 202 response carries both fields during the compatibility window:

```
{
  "status": "uploaded_to_s3",
  "upload_id":        "source:3791ba64-…:doc.md",      # canonical
  "legacy_upload_id": "architecture/doc.md",            # raw S3 key
  "source_id":        "3791ba64-…",
  "bucket":           "utop-oddspark-document",
  ...
}
```

On the legacy-fallback path (no source row), `upload_id` and
`legacy_upload_id` are equal and `source_id` is absent.

### Status route

`GET /api/v1/knowledge/{scope}/uploads/{upload_id}/status` dispatches on
the identity's shape:

- Canonical id → resolves the source directly, computes the S3 key as
  `storage_ref.prefix + relative_key`, HEADs S3, and queries Qdrant on
  that raw key (Pathway still writes raw keys in Phase 2; the
  source-aware Qdrant `document_id` lands in Phase 3+).
- Legacy id → single-default resolver. If the scope now has multiple
  active managed_upload sources, returns 409 "ambiguous" instead of
  guessing a bucket.
- Cross-scope canonical ids return 404.

### Source visibility

`GET /{scope}/sources` and `GET /{scope}/sources/{source_id}` now
annotate each row with:

- `writable` — `kind == 'managed_upload' AND status == 'active'`.
- `is_default` — true only when this row is the sole active
  managed_upload source for the scope.

The list response also exposes `default_source_id` (null when the
scope is multi-source). Operator UIs can drive a source picker
straight off these flags without re-implementing the selection rules.

### What Phase 2 deliberately does NOT change

- Pathway deployment unchanged — still one watcher per pod, still
  writes `document_id = raw S3 key` to Qdrant.
- `knowledge_bases.s3_bucket / s3_prefix / s3_region` still honoured
  as the legacy-fallback route. Removal is Phase 5.
- UI requires no change: the new response fields (`source_id`,
  `legacy_upload_id`) are ignored by existing clients; the canonical
  `upload_id` round-trips through the status route because the server
  accepts both formats.

### Evidence

- 22 new Phase-2 pytest cases
  (`src/tests/test_knowledge_sources_phase2.py`): identity helpers,
  explicit source_id validation across four error paths, implicit
  selection (single / zero-fallback / ambiguous), canonical + legacy
  status dispatch, source annotations.
- 153 tests pass across all knowledge-touching suites
  (Phase 1, async_upload, knowledge_bases, knowledge_scope_auth,
  knowledge_ingest_mode, knowledge_proxy_wiring,
  knowledge_proxy_auth_semantics, knowledge_settings, and the new
  Phase-2 file).
- No migration in this phase — the schema from Phase 1 is sufficient.

## Phase 3 — what actually shipped

Repos: `agentopia-rag-platform` (runtime) and `agentopia-infra` (chart).
Three PRs landed on `main`:

- [rag-platform#50](https://github.com/ai-agentopia/agentopia-rag-platform/pull/50)
  — source registry + per-source watchers (`dev-0bca45b`).
- [rag-platform#51](https://github.com/ai-agentopia/agentopia-rag-platform/pull/51)
  — put `src/` on `sys.path` so the `ingest.source_registry` package
  import resolves under the existing Dockerfile entrypoint.
- [rag-platform#52](https://github.com/ai-agentopia/agentopia-rag-platform/pull/52)
  — add `psycopg[binary]>=3.2` to the pipeline image; remove a
  duplicate import.
- [infra#149](https://github.com/ai-agentopia/agentopia-infra/pull/149)
  — wire `DATABASE_URL` (optional secretKeyRef) into the pathway-
  pipeline Deployment so the runtime can reach `knowledge_sources`.

Initial rollout image on the dev cluster was
`ghcr.io/ai-agentopia/agentopia-rag-platform:dev-08f603e`.
Subsequent ArgoCD Image Updater bumps may change the currently-running
tag; rely on cluster state for the live image, not this document.

### Runtime model

- **Registry read: startup-only.** `pipeline.py::build_pipeline()` calls
  `source_registry.resolve_sources()` once at process start and
  materialises one `pw.io.s3.read(...)` subgraph per active
  `managed_upload` row. Hot reload is not attempted; Pathway's DAG is
  constructed before `pw.run()` anyway, so a new source requires a pod
  restart. This keeps lifecycle predictable and avoids mid-flight
  watcher churn.
- **Fallback.** When `knowledge_sources` is empty and `DATABASE_URL` is
  unreachable but the Phase-0 env contract is still set, the runtime
  synthesises a single pilot source (`source_id=None`) from
  `S3_BUCKET_NAME / S3_PREFIX / S3_REGION / SCOPE_IDENTITY`. Preserves
  local-dev and fresh-cluster boot.
- **One watcher per source.** Each source owns its own
  `pw.io.s3.read → chunk → embed → QdrantSink` subgraph; `pw.run()`
  evaluates all of them in one process. The sink for each source is
  constructed with that source's `scope_identity`, `source_id`, and
  derived collection name (`kb-{sha256(scope)[:16]}`).

### Payload shape

Every new chunk written by Phase-3 Pathway carries:

```
  document_id       raw S3 key (unchanged — e.g. architecture/foo.md)
  scope             human-readable scope identity (unchanged)
  source_id         UUID of the knowledge_sources row (NEW)
  section, section_path, chunk_index, total_chunks, text,
  document_hash, ingested_at, status   (unchanged)
```

`document_id` stays the raw S3 key deliberately: changing it would
invalidate every existing Qdrant point identity and force a full
reindex. A source-aware `document_id` is a future-phase concern once
the corpus is entirely new-shape.

### Compatibility rules during rollout

- **Legacy chunks stay readable.** Existing chunks written before
  Phase 3 have no `source_id` field. They are unaffected — scope-
  filtered retrieval by `scope = "utop/oddspark"` returns both shapes,
  the bot's search works identically, and no scope-identity cutover is
  required.
- **No reindex required.** Verified live: the pilot's
  `architecture/overview.md` chunks remain in Qdrant without
  `source_id` after the Phase-3 rollout; only newly-uploaded documents
  get the field. A future backfill that paints `source_id` onto legacy
  chunks is possible but not necessary until a source-scoped filter
  becomes part of a query path.
- **Env compat.** `S3_BUCKET_NAME`, `S3_PREFIX`, `S3_REGION`,
  `SCOPE_IDENTITY`, and `QDRANT_COLLECTION` remain honoured as the
  synthetic-fallback inputs. Once every scope has a populated
  `knowledge_sources` row (already true for the pilot), these are only
  exercised on fresh-cluster bootstrap.
- **Persistence state preserved.** Pathway's filesystem snapshot
  (`/var/pathway/state`) restored cleanly across the restart —
  `persistence.input_snapshot` recovered 4,530 entries and resumed
  from the last known S3 frontier; no re-embedding of existing
  documents.

### Evidence

- 18 new pytest cases in `tests/test_source_registry.py`: collection
  derivation byte-identical to bot-config-api, row→SourceConfig
  projection across every skip path, `load_active_sources` DB-error
  tolerance, synthetic-pilot fallback precedence rules.
- Live startup log from the rolled pod:
  ```
  ingest.source_registry - source_registry: loaded 1 managed_upload
      source(s) from knowledge_sources
  pipeline - pipeline: building subgraph  source_id=3791ba64-…
      scope=utop/oddspark  s3://utop-oddspark-document/architecture/
  ```
- Live upload (marker `quokka-vestibule-1776640689`) indexed by
  Pathway; Qdrant payload inspection confirms `source_id` field
  present on the new chunk and absent on the legacy `architecture/
  overview.md` chunks — retrieval via knowledge-api `/search` returns
  both shapes under the same scope filter (HTTP 200, count ≥ 1 for
  each query).
- Pilot sanity: `/search?query=super-rag&scopes=utop/oddspark` → HTTP
  200 after rollout.

### What Phase 3 deliberately does NOT change

- `external_s3` kind — deferred at Phase-3 ship time. Phase 4 adds the
  runtime-side `external_s3` watcher model; see the Phase 4 section
  below for the live state and remaining gap.
- `document_id` format — stays the raw S3 key so the corpus doesn't
  need a reindex.
- `knowledge_bases.s3_bucket / s3_prefix / s3_region` columns —
  retained as the synthetic-fallback input. Removal is Phase 5.
- UI — no change; the source-aware upload contract already landed in
  Phase 2 and clients ignore additive fields.

## Phase 4 — shipped (done)

Repos: `agentopia-protocol` (bot-config-api control plane,
`agentopia-protocol#449`), `agentopia-rag-platform` (runtime,
`agentopia-rag-platform#54` + `#55`), and `agentopia-infra` (Vault
env wiring). As of 2026-04-20, Phase 4 is **fully done**. The
control-plane create/deprovision flow, Vault credential contract,
runtime watcher support for `external_s3`, live external-bucket E2E
proof, and automatic watcher teardown on deprovision are all in place.

### What has shipped

- **Control-plane create path.**
  `POST /api/v1/knowledge/{scope}/sources` accepts only
  `kind='external_s3'`, requires:
  - `display_name`
  - `storage_ref.bucket`
  - `storage_ref.prefix`
  - `storage_ref.region`
  - `credential_ref`
- **Validation and lifecycle.** Create inserts the row as
  `status='provisioning'`, reads the AWS keypair from Vault, runs
  `ListObjectsV2` against the target bucket/prefix/region, then flips
  the row to:
  - `active` on success
  - `error` on failure, with a short operator-visible
    `storage_ref.validation_error`
- **Credential contract.** `credential_ref` must be a string under:
  - `secret/data/agentopia/sources/`
  - `agentopia/sources/`
  The Vault payload must contain `access_key` and `secret_key`.
  `credential_ref` is scrubbed from operator responses.
- **Runtime watcher support.** Pathway now projects active
  `external_s3` rows from `knowledge_sources`, resolves per-source S3
  credentials from Vault, and builds a read-only watcher per source.
  New chunks still carry `scope`, `document_id`, and now also
  `source_id`.
- **Real external-bucket proof.** On 2026-04-20 the dev cluster watched
  a dedicated external bucket (`agentopia-ext-s3-test-phase4`) using a
  read-only per-source credential from Vault, ingested an externally
  placed object under `phase4/`, and retrieved the marker content
  through the live vector-search path. This proved the `external_s3`
  read path end-to-end against a bucket separate from the managed pilot
  path.
- **Deprovision semantics.**
  `DELETE /api/v1/knowledge/{scope}/sources/{source_id}` sets the row to
  `deprovisioning`, retracts Qdrant points by `source_id`, then deletes
  the row. The external bucket is never mutated. Current limitation: the
  runtime graph is built at startup, so the watcher for a deprovisioned
  source is not removed until the next Pathway restart.

### Lifecycle boundary for Phase 4

Phase 4's lifecycle surface is intentionally:

- `provisioning`
- `active`
- `error`
- `deprovisioning`

`pause` / `resume` are **not** part of the shipped Phase-4 surface.
They remain a follow-up lifecycle enhancement, not a prerequisite for
the external source model itself. The acceptance boundary for this phase
is therefore:

- create + validate
- watch + ingest
- error visibility
- deprovision without mutating the external bucket

This is still insufficient for `DONE` while watcher teardown remains a
restart-bound side effect instead of an explicit runtime contract.

### Read-only safety guarantees

- Agentopia never calls `PutObject` or `DeleteObject` on an
  `external_s3` bucket.
- Source validation uses `ListObjectsV2` only.
- Runtime credential resolution is per-source; one broken external
  source should be skippable without taking down unrelated sources.
- Vault credential values do not live in Postgres and are not returned
  by the API.

### Watcher teardown — product contract

After a source is deprovisioned, bot-config-api automatically triggers
a K8s rollout restart of `agentopia-rag-platform`. The new pod calls
`resolve_sources()` at startup, which queries only `status='active'`
rows — a deprovisioned (deleted) source is structurally absent. The
restart is non-blocking; the deprovision API call returns
`reconcile_triggered: true` once K8s accepts the patch, without waiting
for the pod to become ready.

This is the explicit, documented product contract for watcher teardown:
Pathway's dataflow graph is constructed once at process start
(`pw.run()` is then blocking), so adding or removing a watcher requires
a pod restart. The controlled restart is the correct mechanism, not a
hot-reload workaround.

**D4-4 live proof (2026-04-20):**

1. External source `82742fe3` created → `status: active`, Vault-backed
   `agentopia-ext-s3-reader` credentials validated.
2. Rag-platform restarted → logs: `loaded 2 active source(s) (1 external_s3, 1 managed_upload)`.
3. Test file `phase4/d4-4-proof.md` placed out-of-band (customer action,
   admin account, not Agentopia).
4. Pathway downloaded file at 04:35:52 UTC; Qdrant confirmed 1 point
   for `source_id=82742fe3`.
5. `DELETE /api/v1/knowledge/utop--oddspark/sources/82742fe3` →
   `{"status": "deprovisioned", "reconcile_triggered": true}`.
6. Deployment annotation confirmed: `agentopia/reconcile-reason: source-deprovisioned`.
7. New pod logs: `loaded 1 active source(s) (1 managed_upload)` — external source gone.
8. Post-deprov file `phase4/post-deprov-test.md` placed in bucket →
   no Pathway download event, no Qdrant upsert after 70s.
9. Final Qdrant count for `source_id=82742fe3`: **0**.
10. Bucket contents intact (both files present, Agentopia made 0 PutObject/DeleteObject calls).
11. Managed pilot `3791ba64` (managed_upload) unaffected throughout.

Phase 4 is **done**.

## Pointers

- ADR-001: `docs/adr/adr-001-generic-source-model.md`
- Phase 1 PR: `ai-agentopia/agentopia-protocol#446`
- Phase 2 PR: `ai-agentopia/agentopia-protocol#447`
- Phase 3 PRs:
  - `ai-agentopia/agentopia-rag-platform#50` — runtime + registry.
  - `ai-agentopia/agentopia-rag-platform#51` — sys.path bootstrap.
  - `ai-agentopia/agentopia-rag-platform#52` — psycopg dependency.
  - `ai-agentopia/agentopia-infra#149` — DATABASE_URL env wiring.
- Pilot state before Phase 1: bucket `utop-oddspark-document`,
  prefix `architecture/`, region `ap-northeast-1`, scope
  `utop/oddspark`. Phase 1 copied that triple into a backfilled
  source row; Phase 2 made the upload/status contract source-aware;
  Phase 3 drives the runtime from that row. The pilot's behaviour is
  unchanged — it just stopped being a hard-coded special case.
