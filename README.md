# agentopia-rag-platform

**Target-state owner of the Agentopia knowledge plane.**

**Status**: Bootstrap — Phase RB (see [implementation-plan.md](https://github.com/ai-agentopia/docs/blob/main/rag/implementation-plan.md))

---

## Repo Role

| Repo | Role | Target state |
|---|---|---|
| **`agentopia-rag-platform`** (this repo) | **Target-state owner** — knowledge plane service | Long-term owner; receives all knowledge-plane work from Phase 3 onwards |
| `agentopia-knowledge-ingest` | **Initial migration source** — ingest, connectors, upload API | Transitional dependency during Phase 3–4; **deprecation target** after Phase 7 cutover |
| `agentopia-super-rag` | **Initial migration source** — retrieval serving, Qdrant, eval framework | Transitional serving dependency during Phase 3–4; **deprecation target** after serving migration (post Phase 4) |

`agentopia-knowledge-ingest` and `agentopia-super-rag` are **not** long-term co-owners of the knowledge plane. They are migration sources that remain operational during the cutover window, then are deprecated. All new knowledge-plane work is anchored here.

---

## Scope — What This Repo Owns

`agentopia-rag-platform` owns the complete knowledge-plane service boundary:

- **Pathway ingest pipeline** — source watching, differential dataflow, insert/update/delete propagation to Qdrant
- **Retrieval serving API** — Qdrant-backed, scope-filtered, multi-tenant
- **Upload API** — async operator upload path (operator → S3 staging → Pathway → Qdrant)
- **Document lifecycle** — `document_records`, `status` management, scope ownership
- **Eval framework** — nDCG@5, freshness lag, contamination, route correctness

This repo does **not** own:
- Control plane / harness (bot-config-api in `agentopia-protocol`)
- Runtime execution / gateway plugin chain (`agentopia-protocol`)
- Memory plane (mem0-api in `agentopia-protocol`)
- Cluster infrastructure (`openclaw-infra`)

---

## Migration Path

The transition from the old split to this repo is two-dimensional:

**Dimension 1 — Ingest path**: batch operator-triggered → Pathway streaming  
**Dimension 2 — Service consolidation**: `agentopia-knowledge-ingest` + `agentopia-super-rag` → this repo

Sequencing (see [migration-plan.md](https://github.com/ai-agentopia/docs/blob/main/rag/migration-plan.md) §1.5):

1. **Phase 3**: `agentopia-rag-platform` takes over the ingest path. Pathway pipeline runs here. `agentopia-knowledge-ingest` is reduced to a thin S3 upload proxy.
2. **Phase 4**: All scopes migrated to Pathway inside this repo.
3. **Post Phase 4**: `agentopia-super-rag` retrieval serving code migrates into this repo. `agentopia-super-rag` is deprecated.
4. **Phase 7**: `agentopia-knowledge-ingest` deprecated and archived. This repo is the sole knowledge-plane owner.

During the transition, `agentopia-knowledge-ingest` and `agentopia-super-rag` are **transitional runtime dependencies** — they serve live traffic while cutover proceeds scope-by-scope. They are not retained beyond their migration window.

---

## Service Structure (target — see [docs/service-structure.md](docs/service-structure.md))

```
src/
├── ingest/     # Pathway pipeline, source connectors, upload API
├── serving/    # Retrieval API (Qdrant-backed, scope-filtered)
├── eval/       # Eval framework (nDCG@5, freshness, contamination)
└── lifecycle/  # Document lifecycle, document_records, scope management
charts/         # Helm chart (agentopia-rag-platform — deployed via ArgoCD)
docs/           # Service-local documentation
```

---

## Architecture References

All architecture documentation is in [ai-agentopia/docs](https://github.com/ai-agentopia/docs) — `rag/` directory:

- [README.md](https://github.com/ai-agentopia/docs/blob/main/rag/README.md) — initiative overview, candidate evaluation
- [architecture.md](https://github.com/ai-agentopia/docs/blob/main/rag/architecture.md) — 5-plane target architecture; Section 9 = repo ownership table
- [implementation-plan.md](https://github.com/ai-agentopia/docs/blob/main/rag/implementation-plan.md) — phase map, Phase RB through Phase 7
- [migration-plan.md](https://github.com/ai-agentopia/docs/blob/main/rag/migration-plan.md) — two-dimensional migration, consolidation sequencing

---

## Implementation Status

See the [knowledge platform milestone](https://github.com/ai-agentopia/agentopia-rag-platform/milestone/1) for tracked issues (Phase RB, P3–P7).

Control-plane routing work (P0–P2.5) is tracked in [agentopia-protocol milestone #40](https://github.com/ai-agentopia/agentopia-protocol/milestone/40).
