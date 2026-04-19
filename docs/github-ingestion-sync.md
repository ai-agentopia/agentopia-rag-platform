---
title: "GitHub → S3 sync (file content ingestion)"
description: "How markdown architecture docs from the Agentopia docs repository reach Qdrant via Pathway. Covers rag-platform issue #26 only — structured GitHub records (#27) are intentionally out of scope here."
---

# GitHub → S3 sync

This document is the rag-platform side of the GitHub ingestion model. It
answers two operator questions:

1. How does content authored in GitHub become retrievable through the
   knowledge API?
2. What is explicitly **not** handled by this path?

## Two-path model (recap)

The Agentopia RAG platform ingests GitHub content via **two independent
paths**, each owned by its own issue:

| Path | Carries | Transport | Owning issue |
|---|---|---|---|
| File content | `.md`, eventually `.docx`/`.pdf` | GitHub Actions → S3 → Pathway S3 connector → Qdrant | [#26](https://github.com/ai-agentopia/agentopia-rag-platform/issues/26) (this doc) |
| Structured records | issue/PR bodies, commits, review comments | Airbyte GitHub source → `pw.io.airbyte` → Pathway → Qdrant | [#27](https://github.com/ai-agentopia/agentopia-rag-platform/issues/27) (future) |

Path ownership is deliberately separate: `pw.io.github` does not exist in
Pathway 0.30.0, and Airbyte cannot extract file blob content. The sync
workflow and the Airbyte connector never overlap.

## Path covered by #26 (this document)

```
┌───────────────────────┐   push to main   ┌──────────────────────────┐
│ ai-agentopia/docs     │─────────────────▶│ GitHub Actions:          │
│ architecture/*.md     │                  │  aws s3 sync             │
└───────────────────────┘                  │    --delete              │
                                           │    --include '*.md'      │
                                           │    --size-only           │
                                           └──────────┬───────────────┘
                                                      │ PUT / DELETE
                                                      ▼
                                  ┌────────────────────────────────────┐
                                  │ s3://utop-oddspark-document/       │
                                  │     architecture/*.md              │
                                  │     (region ap-northeast-1)        │
                                  └────────────────┬───────────────────┘
                                                   │ 30 s poll
                                                   ▼
                                  ┌────────────────────────────────────┐
                                  │ pathway-pipeline                   │
                                  │   pw.io.s3.read(...)               │
                                  │   format=plaintext_by_object       │
                                  │   autocommit 30 s                  │
                                  └────────────────┬───────────────────┘
                                                   │ chunk → embed → upsert / retract
                                                   ▼
                                  ┌────────────────────────────────────┐
                                  │ Qdrant collection                  │
                                  │   kb-45294aa854667d3b              │
                                  │ Scope identity: utop/oddspark      │
                                  └────────────────────────────────────┘
```

### Contract summary

| Setting | Value | Source of truth |
|---|---|---|
| Source repo | `ai-agentopia/docs` | GitHub |
| Source path | `architecture/**/*.md` | Sync workflow include pattern |
| Trigger | `push` to `main` touching that path; `workflow_dispatch` | Workflow YAML |
| S3 bucket | `utop-oddspark-document` | `agentopia-rag-platform-s3` K8s Secret |
| S3 region | `ap-northeast-1` | Pathway pod env `S3_REGION` |
| S3 prefix | `architecture/` | Pathway pod env `S3_PREFIX` |
| Scope identity | `utop/oddspark` | Pathway pod env `SCOPE_IDENTITY` |
| Qdrant collection | `kb-45294aa854667d3b` | SHA-256 of scope identity |
| Freshness | ≤ 1 Pathway poll cycle (~30 s) + upsert latency | Live-measured ~24 s |

### Update / delete semantics

- **New or modified file in repo**: `aws s3 sync` uploads; Pathway sees a
  new / updated `LastModified`; re-chunks and re-upserts into Qdrant with
  stable point IDs derived from `(document_id, chunk_index)`. Net effect
  is an in-place replacement.
- **File removed from repo**: `--delete` removes the S3 object; Pathway's
  differential-dataflow emits `is_addition=False` rows for every chunk
  that belonged to it; `QdrantSink.on_change` deletes the points.
  Verified live: `incident-response-protocol.md` went from 6 chunks to 0
  chunks one poll after its S3 object was removed.
- **File renamed in repo**: equivalent to delete + add. `document_id` in
  the Qdrant payload changes; old chunks are retracted, new chunks are
  ingested.

### Include / exclude policy

Hard-coded in the workflow:

```
--include '*.md'
--exclude '*'      (applied first so only .md passes through)
```

Plus an early-exit guard: if `architecture/` contains zero markdown
files, the workflow aborts rather than execute a `--delete` that would
wipe every object. This is deliberate protection against accidental
directory removal.

### IAM contract

The workflow authenticates via two GitHub Actions repository secrets:

- `AWS_ACCESS_KEY_ID`
- `AWS_SECRET_ACCESS_KEY`

These belong to an IAM principal (currently the `oddspark-s3` user) with
the following minimal inline policy:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "ListArchitecturePrefix",
      "Effect": "Allow",
      "Action": ["s3:ListBucket"],
      "Resource": "arn:aws:s3:::utop-oddspark-document",
      "Condition": {
        "StringLike": {
          "s3:prefix": ["architecture/*"]
        }
      }
    },
    {
      "Sid": "WriteArchitecturePrefix",
      "Effect": "Allow",
      "Action": ["s3:PutObject", "s3:DeleteObject"],
      "Resource": "arn:aws:s3:::utop-oddspark-document/architecture/*"
    }
  ]
}
```

`ListBucket` is required by `aws s3 sync` for reconciliation; `PutObject`
and `DeleteObject` are the only mutating verbs the sync needs.

## Path NOT covered by #26

Intentionally **out of scope** for this workflow — raise or track under
the noted issues instead:

| Out-of-scope item | Correct home |
|---|---|
| GitHub issue bodies, PR descriptions, commit messages, reviews | rag-platform #27 (Airbyte GitHub connector) |
| Operational state (open/closed status, assignees, milestones) | GitHub API at query time, `operational_state` query family — never RAG-indexed |
| Binary document formats (`.docx`, `.pdf`) | Pathway pipeline must first gain `format="binary"` + UnstructuredParser support — separate pipeline ticket |
| Non-`architecture/` directories in the docs repo | Deliberately ignored until there is a retrieval reason to index them |
| Any other repository than `ai-agentopia/docs` | A new workflow + new S3 prefix + new scope + pipeline config change |

## Live proof

End-to-end verification for this ticket:

1. Merge commit [`eee0b139`](https://github.com/ai-agentopia/docs/commit/eee0b139f8be006cb5727b2485e7ff9776334a0f) — first workflow run on `main`.
2. Sync log shows 16 uploads and 5 deletes (stale test artifacts).
3. Pathway poll at 13:34:30 UTC (24 s after Action completed) ingested
   `architecture/github-s3-sync-proof.md` into 5 Qdrant chunks.
4. Retrieval via `knowledge-api /search?query=zebra-ionosphere-20260419`
   returned the proof document with score 0.5285.
5. Merge commit [`ce2bdf3e`](https://github.com/ai-agentopia/docs/commit/ce2bdf3e7f805470dcbd09b4cdc51f04e6d806c8) removed the proof file.
6. Sync log shows `delete:
   s3://utop-oddspark-document/architecture/github-s3-sync-proof.md`.
7. Qdrant chunk count for that `document_id` dropped from 5 to 0 one
   poll later — retraction confirmed.
8. Merge commit [`5afe29a`](https://github.com/ai-agentopia/docs/commit/5afe29a6492ea11aeb0a78fd2eeacba0165b3241) switched `aws s3 sync` to
   `--size-only`; the next run uploaded and deleted zero objects,
   confirming the no-op-when-unchanged path.

## Known follow-ups (not required for #26)

- Current IAM key is shared between the GitHub Action and the Pathway
  runtime. A dedicated `oddspark-s3-gh-sync` sub-user with only the
  narrower policy above would reduce blast radius for a leaked GH
  Actions secret.
- OIDC federation (no long-lived keys in GitHub secrets) is the
  stronger long-term story.
- `--size-only` is a practical heuristic; a content-hash comparator
  (e.g. `s5cmd sync --checksum`) would be strictly more correct.

None of these block the `#26` deliverable.
