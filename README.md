# agentopia-rag-platform

Knowledge platform for the Agentopia agentic system.

**Status**: Bootstrap — Phase RB (see [docs/rag/implementation-plan.md](https://github.com/ai-agentopia/docs/blob/main/rag/implementation-plan.md))

## Purpose

This repo is the consolidation target for the Agentopia knowledge plane. It will replace the current split between:

- `agentopia-knowledge-ingest` — ingest, upload API, source connectors
- `agentopia-super-rag` — retrieval serving, Qdrant management, eval framework

It will own:
- Pathway ingest pipeline (source watching, differential dataflow, Qdrant upserts)
- Retrieval serving API (Qdrant-backed, scope-filtered)
- Document lifecycle (document_records, status management)
- Eval framework (nDCG@5, freshness, contamination)
- Upload API (async: operator → S3 staging → Pathway)

## Architecture

See [docs/rag/](https://github.com/ai-agentopia/docs/tree/main/rag) for the initiative documentation:
- [README.md](https://github.com/ai-agentopia/docs/blob/main/rag/README.md) — initiative overview
- [architecture.md](https://github.com/ai-agentopia/docs/blob/main/rag/architecture.md) — target architecture
- [implementation-plan.md](https://github.com/ai-agentopia/docs/blob/main/rag/implementation-plan.md) — phase plan
- [migration-plan.md](https://github.com/ai-agentopia/docs/blob/main/rag/migration-plan.md) — two-dimensional migration

## Service Structure (target — see [docs/service-structure.md](docs/service-structure.md))

```
src/
├── ingest/        # Pathway pipeline, source connectors, upload API
├── serving/       # Retrieval API (from agentopia-super-rag)
├── eval/          # Eval framework (from agentopia-super-rag/evaluation/)
└── lifecycle/     # Document lifecycle, document_records, scope management
charts/            # Helm chart
docs/              # Service-local documentation
```

## Implementation Status

See the [knowledge platform milestone](../../milestones) for tracked issues.

## Canonical Docs

See [ai-agentopia/docs](https://github.com/ai-agentopia/docs) — `rag/` directory.
