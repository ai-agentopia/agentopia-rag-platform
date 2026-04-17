# agentopia-rag-platform — Service Structure

> Status: Bootstrap (Phase RB.2)

## Target Directory Layout

```
agentopia-rag-platform/
├── src/
│   ├── ingest/        # Pathway pipeline host, source connectors, upload API
│   │                  # Migrated from: agentopia-knowledge-ingest/src/
│   ├── serving/       # Retrieval API (Qdrant-backed, scope-filtered)
│   │                  # Migrated from: agentopia-super-rag/src/
│   ├── eval/          # Eval framework (nDCG@5, freshness, contamination)
│   │                  # Migrated from: agentopia-super-rag/evaluation/
│   └── lifecycle/     # Document lifecycle: document_records, scope management
│                      # Split from: agentopia-super-rag + agentopia-knowledge-ingest
├── charts/
│   └── agentopia-rag-platform/   # Helm chart (deployed via ArgoCD from openclaw-infra)
├── docs/
│   ├── CANONICAL.md               # Pointer to ai-agentopia/docs
│   └── service-structure.md       # This file
├── .github/
│   └── workflows/
│       └── build-image.yml        # CI: build + push ghcr.io/ai-agentopia/agentopia-rag-platform
└── README.md
```

## Migration Source Mapping

| Module | Migrated from | Phase |
|---|---|---|
| `src/ingest/` | `agentopia-knowledge-ingest/src/` (connectors, upload API, orchestrator) | Phase 3 (ingest path) |
| `src/serving/` | `agentopia-super-rag/src/` (retrieval API, Qdrant client) | Post Phase 4 |
| `src/eval/` | `agentopia-super-rag/evaluation/` | Post Phase 4 |
| `src/lifecycle/` | Split from both agentopia-knowledge-ingest + agentopia-super-rag | Post Phase 4 |

## Decision Record

Migration sequencing confirmed in Phase RB.3 (see implementation-plan.md).
Ingest migrates first (Phase 3); serving migrates after all scopes are on Pathway (post Phase 4).
