# agentopia-rag-platform — Migration Sequencing Decision

> Status: Phase RB.3 — awaiting CTO approval  
> Reference: [migration-plan.md §1.5](https://github.com/ai-agentopia/docs/blob/main/rag/migration-plan.md)

---

## Decision

The two-dimensional migration proceeds with the **recommended sequencing**:

1. **Ingest migration precedes serving migration** — lower concurrent risk; allows serving to remain stable while ingest cutover is validated scope-by-scope.
2. `agentopia-rag-platform` takes ownership of the ingest path in Phase 3, before the serving path migrates.

---

## Sequencing — Phase by Phase

| Phase | Ingest path owner | Serving path owner | agentopia-rag-platform role |
|---|---|---|---|
| PRB (now) | `agentopia-knowledge-ingest` | `agentopia-super-rag` | Bootstrap only — no production traffic |
| Phase 3 (pilot) | **`agentopia-rag-platform`** (Pathway pipeline, pilot scope) | `agentopia-super-rag` (unchanged) | Owns ingest for pilot scope; serving remains in super-rag |
| Phase 4 (full migration) | **`agentopia-rag-platform`** (Pathway, all scopes) | `agentopia-super-rag` (unchanged) | Owns ingest for all scopes |
| Post Phase 4 | `agentopia-rag-platform` | **`agentopia-rag-platform`** (serving migrated in) | Sole knowledge-plane owner |
| Phase 7 | `agentopia-rag-platform` | `agentopia-rag-platform` | `agentopia-knowledge-ingest` archived |

---

## Code Migration Approach

**Approach: lift-and-refactor, not rewrite.**

- `src/ingest/` is built fresh in this repo (Pathway pipeline code, new connector layer). Existing `agentopia-knowledge-ingest` normalizer and orchestrator are not ported — they are deprecated.
- `src/serving/` and `src/eval/` are lifted from `agentopia-super-rag/src/` and `agentopia-super-rag/evaluation/` respectively. Lifted code is refactored to fit the new module layout and tested before traffic cutover.
- `src/lifecycle/` is new code consolidating document-record management currently split across both source repos.

Rationale: Pathway replaces the orchestrator/normalizer entirely; no value in porting that code. The retrieval-serving and eval code in super-rag is proven in production and worth lifting rather than rewriting.

---

## Deprecation Timeline

### `agentopia-knowledge-ingest`

| Milestone | Trigger | State |
|---|---|---|
| Phase 3 start | `scope_ingest_mode: pathway` set for pilot scope | Reduced to thin S3 upload proxy for pilot scope |
| Phase 4 complete | All scopes on Pathway, P4 exit gate passed | Normalizer + orchestrator frozen (deprecated); upload API still active as S3 proxy |
| Phase 7 rollback drill passed | 30-day hold started at P4.6 (freeze) | Eligible for archive |
| 30 days after Phase 7 | No rollbacks executed | **Archived** — repo set to read-only |

### `agentopia-super-rag`

| Milestone | Trigger | State |
|---|---|---|
| Phase 4 complete | All scopes on Pathway | Retrieval serving still live; no changes |
| Post Phase 4 serving migration | `src/serving/` + `src/eval/` landed in this repo; traffic cut over | **Deprecated** — no new feature work |
| 30 days after serving cutover | No retrieval regressions observed | **Archived** — repo set to read-only |

---

## Definitions

| Term | Definition |
|---|---|
| **Deprecated** | Frozen — no new feature work. Bug fixes and rollback support only. Active as a runtime dependency during cutover window. |
| **Archived** | Decommissioned — repo set to read-only on GitHub. No further changes. All production traffic is on `agentopia-rag-platform`. |

---

## Gate Condition

**This decision gates Phase 3 start.** Phase 3 (`agentopia-rag-platform` takes over ingest path) does not begin until:

1. This document is approved (CTO sign-off recorded below)
2. PRB.2 structure is approved
3. Phase RB.1 bootstrap is confirmed (done — rag-platform#2 closed)

---

## Approval Record

| Item | Status |
|---|---|
| Recommended sequencing confirmed | ⬜ Awaiting CTO sign-off |
| Lift-and-refactor approach confirmed | ⬜ Awaiting CTO sign-off |
| Deprecation timeline confirmed | ⬜ Awaiting CTO sign-off |

_CTO approval: close issue rag-platform#4 once sign-off is given._
