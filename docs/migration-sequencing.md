# agentopia-rag-platform — Migration Sequencing Decision

> Status: Approved — CTO sign-off 2026-04-17 (rag-platform#4)  
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

## P4.6 Legacy Freeze Declaration

> **Date:** 2026-04-21  
> **Issue:** [rag-platform#16](https://github.com/ai-agentopia/agentopia-rag-platform/issues/16) — CLOSED  
> **Authority:** agentopia-rag-platform team (formal declaration; CTO sign-off on sequencing 2026-04-17)

### Declaration

Pathway is the sole active ingest path for all registered scopes.  
No competing legacy ingest process is running or expected to restart.  
`agentopia-knowledge-ingest` normalizer and orchestrator are declared inactive as of this date.

### Verification Evidence

**Pod audit (2026-04-21, `agentopia-dev`):**

| Pod | Image | Role |
|---|---|---|
| `agentopia-rag-platform-67d5db6cb8-5gjl8` | `ghcr.io/ai-agentopia/agentopia-rag-platform:dev-993d8e4` | Pathway ingest — sole ingest runtime |

No legacy ingest pod (`agentopia-knowledge-ingest` or equivalent) found in `agentopia-dev` or any other namespace.

**nDCG@5 confirmation:**

| Scope | nDCG@5 | Bar | Status |
|---|---|---|---|
| utop/oddspark | 0.9184 | ≥ 0.90 | ✓ |

Source: live weekly eval CronJob (`agentopia-rag-eval`), run 2026-04-20. Committed artifact: `src/eval/results/nDCG-p3-pilot-gate.json` (0.941, 2026-04-18).

### What this declaration does and does not do

| In scope | Out of scope |
|---|---|
| Formal recording that Pathway is sole ingest path | Code deletion of `agentopia-knowledge-ingest` |
| Starting the 30-day hold clock | Serving-path migration (not yet started) |
| Unblocking issue #22 (rollback drill) | Executing the rollback drill itself |
| Confirming nDCG ≥ 0.90 at freeze point | Promising nDCG improvement |

Code removal (archiving `agentopia-knowledge-ingest`) requires the rollback drill in issue #22 to pass first.

### 30-Day Hold

| Date | Event |
|---|---|
| **2026-04-21** | P4.6 freeze declared — hold-start date |
| **2026-05-21** | Hold complete — issue #22 (rollback drill) becomes schedulable |

Issue #22 must not execute before 2026-05-21.

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
| Recommended sequencing confirmed | ✅ Approved 2026-04-17 |
| Lift-and-refactor approach confirmed | ✅ Approved 2026-04-17 |
| Deprecation timeline confirmed | ✅ Approved 2026-04-17 |

_CTO approval recorded 2026-04-17. Issue rag-platform#4 closed._
