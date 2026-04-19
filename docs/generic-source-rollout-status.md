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
| **1** | `knowledge_sources` schema + backfill + read-only control plane + upload-path shim | ✅ Done (2026-04-19) |
| 2 | API shim with explicit `source_id`, new managed-upload key format | ⏳ Not started |
| 3 | Pathway reads `knowledge_sources`; one watcher per source | ⏳ Not started |
| 4 | `external_s3` end-to-end | ⏳ Not started |
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

## Pointers

- ADR-001: `docs/adr/adr-001-generic-source-model.md`
- Phase 1 PR: `ai-agentopia/agentopia-protocol#446`
- Pilot state before Phase 1: bucket `utop-oddspark-document`,
  prefix `architecture/`, region `ap-northeast-1`, scope
  `utop/oddspark`. Phase 1 copies that triple into a backfilled
  source row; nothing in the live ingest path reads the new row yet.
