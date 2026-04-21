# Issue #17 — P5 Retrieval Upgrade Gate Result

**Date:** 2026-04-20  
**Scope:** utop/oddspark  
**Collection:** kb-45294aa854667d3b (post-Pathway corpus, 425 chunks)  
**Evaluation dataset:** src/eval/datasets/p3_pilot_gate.json (10 queries)  
**Gate threshold:** ≥ 3% nDCG@5 improvement over baseline required for authorization

## Verdict: NOT AUTHORIZED

No candidate showed ≥ 3% nDCG@5 improvement. P5 retrieval upgrades are not authorized.
Issue #17 is closed. Issue #18 (P5 implementation) is not activated.

---

## Results Summary

| Candidate | nDCG@5 | Delta | Delta % | Status |
|---|---|---|---|---|
| Baseline (dense-only) | 0.9184 | — | — | (reference) |
| W2 (hybrid BM25+dense) | — | — | — | prerequisite failed |
| W4 (cross-encoder reranking) | 0.9131 | −0.0053 | −0.58% | not authorized |

---

## W2: Hybrid BM25+Dense

**Status: prerequisite failed — not authorized**

- Qdrant v1.16.1 is deployed (server-level sparse vector support ✓, requires ≥ 1.7)
- Collection `kb-45294aa854667d3b` has **no sparse vector index** (`sparse_vectors: None`)
- BM25 hybrid retrieval requires sparse vectors indexed at ingestion time — this is a collection-creation-time decision, not a query-time option
- Re-ingesting the full corpus with a sparse index would be required to evaluate W2
- Re-ingestion is a significant infrastructure change (Pathway pipeline modification + full re-index) not justified without evidence that W2 would cross the +3% threshold

**Conclusion:** W2 cannot be evaluated on the current corpus without infrastructure changes that are themselves unjustified at this gate. Not authorized.

---

## W4: Cross-Encoder Reranking

**Status: not authorized (delta = −0.58%)**

**Method:** Dense retrieval (top-20), rerank with `cross-encoder/ms-marco-MiniLM-L-6-v2`, take top-5.

**Per-query results:**

| Query | Baseline nDCG@5 | W4 nDCG@5 | Delta | Notes |
|---|---|---|---|---|
| p3-01 (A2A protocol comparison) | 1.0000 | 1.0000 | 0 | |
| p3-02 (multi-bot memory arch) | 0.9557 | 1.0000 | +0.0443 | W4 improved rank order |
| p3-03 (retrieval strategy decision) | 1.0000 | 1.0000 | 0 | |
| p3-04 (code review quality gates) | 0.9106 | 0.8772 | −0.0334 | W4 misranked one result |
| p3-05 (dual-lane interaction model) | 1.0000 | 0.9197 | −0.0803 | W4 introduced rank inversion |
| p3-06 (deterministic delivery pipeline) | 0.6934 | 1.0000 | +0.3066 | W4 fixed a rank inversion ✓ |
| p3-07 (A2A implementation phases) | 1.0000 | 1.0000 | 0 | |
| p3-08 (worker pool routing) | 1.0000 | 1.0000 | 0 | |
| p3-09 (coexisting review systems) | 1.0000 | 0.9469 | −0.0531 | W4 introduced rank inversion |
| p3-10 (P1 gate criteria) | 0.8503 | 0.3869 | −0.4634 | W4 major regression |

**Analysis:**

W4 fixed the previously lowest query (p3-06: deterministic delivery pipeline), resolving a rank inversion where the correct document ranked 2nd instead of 1st. However, it introduced a severe regression on p3-10 (P1 gate criteria, −0.4634) and moderate regressions on p3-05 (−0.0803) and p3-09 (−0.0531).

The `ms-marco-MiniLM-L-6-v2` model is trained on MS-MARCO (web search relevance). This corpus consists of Agentopia architecture and design documents with domain-specific terminology. The cross-encoder trades one rank inversion fix for three new ones, producing a net regression.

This pattern is consistent with the prior W4 history: "W4 (reranking): previously evaluated — resulted in nDCG@5 regression (−0.1238)". The post-Pathway corpus confirms the same behavior: generic cross-encoder reranking hurts retrieval on this domain-specific knowledge base.

**Conclusion:** W4 delta = −0.58%, well below the required +3%. Not authorized.

---

## Gate Methodology

- Evaluation run: in-cluster Kubernetes Job `retrieval-upgrade-gate-01` (2026-04-20)
- Image: `ghcr.io/ai-agentopia/agentopia-rag-platform:dev-993d8e4`
- Embedding: `openai/text-embedding-3-small` via OpenRouter
- W4 reranker: `cross-encoder/ms-marco-MiniLM-L-6-v2` (sentence-transformers)
- Dataset: same 10-query set as P3.6 gate (document-level relevance grading)
- Corpus: post-Pathway (Pathway pipeline active since issue #19)
- Machine results file: `src/eval/results/retrieval-upgrade-gate.json`

---

## Consequence

- Issue #17: CLOSED (gate verdict: not authorized)
- Issue #18: NOT ACTIVATED (conditional on #17 authorization — authorization not granted)
- P5 (retrieval upgrades): DEFERRED indefinitely
- Revisit trigger: significant corpus growth or addition of new scope type that changes retrieval characteristics
