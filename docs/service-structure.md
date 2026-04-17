# agentopia-rag-platform — Service Structure

> Status: Bootstrap (Phase RB.2)

---

## Repo Role Clarification

| Repo | Current role | Migration role | Target-state role |
|---|---|---|---|
| **`agentopia-rag-platform`** (this repo) | Bootstrap — no production traffic yet | Knowledge-plane implementation anchor from Phase 3 onwards | **Sole owner** of the knowledge-plane service boundary |
| `agentopia-knowledge-ingest` | Production: upload API, connectors, orchestrator, normalizer | Initial migration source; reduced to thin S3 upload proxy after Phase 3 ingest cutover | **Deprecation target** — archived after Phase 7 cutover |
| `agentopia-super-rag` | Production: retrieval serving, Qdrant, eval framework | Transitional serving dependency during Phase 3–4; retrieval serving code migrates here post Phase 4 | **Deprecation target** — archived after serving migration |

**Reading this table**: `agentopia-knowledge-ingest` and `agentopia-super-rag` are migration inputs, not long-term co-owners. They remain operational during the transition window only. Their code and responsibilities move here.

---

## Serving Ownership — Explicit Statement

`agentopia-super-rag` currently owns the retrieval serving path (Qdrant search, scoring, response). This is a **transitional runtime dependency** — not a retained long-term component:

- During Phase 3–4: `agentopia-super-rag` serves all retrieval requests unchanged. It remains the source of truth for the serving path while scopes migrate to Pathway ingest.
- Post Phase 4: the `agentopia-super-rag` serving code (retrieval API, Qdrant client, eval framework) is migrated into `src/serving/` and `src/eval/` in this repo.
- After serving migration: `agentopia-super-rag` is deprecated. All retrieval traffic routes to `agentopia-rag-platform`.

The serving migration is **code migration + traffic cutover** — not permanent co-ownership.

---

## System Boundary — What This Repo Does and Does Not Own

`agentopia-rag-platform` maps to the **knowledge plane** in the 5-plane architecture:

```
Control Plane           → agentopia-protocol (bot-config-api)       ← NOT this repo
Runtime Execution Plane → agentopia-protocol (gateway)              ← NOT this repo
Operational State Plane → governance-bridge + Temporal/K8s/GitHub   ← NOT this repo
Knowledge Plane         → agentopia-rag-platform (this repo)        ← OWNER
Memory Plane            → agentopia-protocol (mem0-api)             ← NOT this repo
```

This repo owns the knowledge-plane service boundary only. It does not own the gateway plugin chain, bot-config-api policy declarations, or memory-plane infrastructure.

---

## Target Directory Layout

```
agentopia-rag-platform/
├── src/
│   ├── ingest/        # Pathway pipeline host, source connectors, upload API
│   │                  # Initial migration source: agentopia-knowledge-ingest/src/
│   │                  # Transitional dependency: agentopia-knowledge-ingest runs in parallel (Phase 3–4)
│   │                  # Target-state owner: this repo (from Phase 3)
│   │
│   ├── serving/       # Retrieval API (Qdrant-backed, scope-filtered)
│   │                  # Initial migration source: agentopia-super-rag/src/
│   │                  # Transitional dependency: agentopia-super-rag serves retrieval (Phase 3–4)
│   │                  # Target-state owner: this repo (post Phase 4)
│   │
│   ├── eval/          # Eval framework (nDCG@5, freshness, contamination)
│   │                  # Initial migration source: agentopia-super-rag/evaluation/
│   │                  # Target-state owner: this repo (post Phase 4)
│   │
│   └── lifecycle/     # Document lifecycle: document_records, scope management, status
│                      # Initial migration source: split from agentopia-knowledge-ingest + agentopia-super-rag
│                      # Target-state owner: this repo (post Phase 4)
│
├── charts/
│   └── agentopia-rag-platform/   # Helm chart (deployed via ArgoCD; tracked in openclaw-infra)
│
├── docs/
│   ├── CANONICAL.md               # Pointer to ai-agentopia/docs rag/ directory
│   ├── service-structure.md       # This file
│   └── migration-sequencing.md    # Two-dimensional migration decision (Phase RB.3)
│
├── .github/
│   └── workflows/
│       └── build-image.yml        # CI: build + push ghcr.io/ai-agentopia/agentopia-rag-platform
│
└── README.md
```

---

## Migration Source Table

| Module | Initial migration source | Transitional dependency period | Target-state ownership |
|---|---|---|---|
| `src/ingest/` | `agentopia-knowledge-ingest/src/` | Phase 3–4 (knowledge-ingest runs as S3 proxy) | This repo, from Phase 3 |
| `src/serving/` | `agentopia-super-rag/src/` | Phase 3–4 (super-rag serves retrieval) | This repo, post Phase 4 |
| `src/eval/` | `agentopia-super-rag/evaluation/` | Phase 3–4 (eval runs in super-rag) | This repo, post Phase 4 |
| `src/lifecycle/` | Split from both | Phase 3–4 (lifecycle split across both source repos) | This repo, post Phase 4 |

---

## Deprecation Timeline

| Repo | Deprecation trigger | Archive eligible |
|---|---|---|
| `agentopia-knowledge-ingest` | All scopes migrated to Pathway + Phase 7 rollback drill passed | Phase 7 exit + 30-day hold |
| `agentopia-super-rag` | Retrieval serving migrated to this repo + 30-day stable window | Post-Phase 4 serving cutover + 30 days |

**Deprecation** = frozen; no new feature work. Bug fixes only, for rollback support.  
**Archive** = decommissioned; repo archived on GitHub. No further changes.

---

## Decision Record

Migration sequencing confirmed in Phase RB.3 ([migration-plan.md §1.5](https://github.com/ai-agentopia/docs/blob/main/rag/migration-plan.md)).

- Ingest migration precedes serving migration (lower concurrent risk)
- `agentopia-knowledge-ingest` is never a long-term peer — it is a transitional dependency that shrinks as scopes migrate to Pathway, then is deprecated
- `agentopia-super-rag` is never a long-term peer — its serving code moves here post Phase 4, then it is deprecated
