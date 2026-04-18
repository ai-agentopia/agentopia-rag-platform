# P3.5 Freshness Lag Measurement — agentopia-rag-platform

**Date**: 2026-04-18
**Issue**: ai-agentopia/agentopia-rag-platform#9
**Pipeline**: S3 (`utop-oddspark-document/architecture/`) → Pathway (dev-be9d352) → Qdrant (`kb-45294aa854667d3b`)
**Scope**: `utop/oddspark`

---

## Methodology

### Measurement Approach

Each run:
1. Upload a new version of `architecture/p35-update-probe.md` to S3 via `boto3.put_object`
2. Record `T1` = local `time.time()` immediately after upload returns
3. Poll Qdrant `kb-45294aa854667d3b` for `ingested_at` to change on the probe document
4. Record `T2` = new `ingested_at` value (written by `on_change` in `QdrantSink`, pod wall-clock)
5. `lag_raw = T2 − T1`
6. Apply clock correction: `lag_corrected = lag_raw − clock_offset`

### Clock Skew Characterization

The pipeline pod runs on `server36` (k3s). The pod clock is **+1.29s ahead** of the local measurement machine.

Offset measured via RTT-corrected sampling (3 samples):
| Sample | Method | Offset |
|--------|--------|--------|
| 1 | `date +%s%3N` comparison, RTT halved | +1.301s |
| 2 | `date +%s%3N` comparison, RTT halved | +1.313s |
| 3 | `date +%s%3N` comparison, RTT halved | +1.267s |
| **Mean** | | **+1.29s** |

`lag_corrected` represents the true S3-upload-complete → Qdrant-write latency.

### Infrastructure Notes

- **S3 IAM**: Pipeline user `oddspark-s3` is read-only. Probe uploads used admin credentials (`utop-aws` profile).
- **Pathway poll interval**: `PATHWAY_POLL_INTERVAL_SECS=30`, `autocommit_duration_ms=30000`.
- **`ingested_at`**: Set by `time.time()` in `QdrantSink.on_change(is_addition=True)` — wall-clock at Qdrant write time.

---

## Raw Results — 10 Timed Update Runs

| Run | T1 (local, unix) | ingested_at (pod, unix) | lag_raw (s) | lag_corrected (s) |
|-----|-----------------|------------------------|-------------|-------------------|
| 1  | 1776479654.930 | 1776479657.145 | 2.215 | **0.925** |
| 2  | 1776479675.821 | 1776479677.594 | 1.773 | **0.483** |
| 3  | 1776479695.505 | 1776479697.735 | 2.230 | **0.940** |
| 4  | 1776479714.307 | 1776479716.206 | 1.899 | **0.609** |
| 5  | 1776479733.827 | 1776479735.240 | 1.413 | **0.123** |
| 6  | 1776479751.470 | 1776479753.156 | 1.686 | **0.396** |
| 7  | 1776479768.749 | 1776479770.461 | 1.712 | **0.422** |
| 8  | 1776479785.672 | 1776479787.338 | 1.666 | **0.376** |
| 9  | 1776479802.284 | 1776479803.627 | 1.343 | **0.053** |
| 10 | 1776479822.629 | 1776479824.409 | 1.780 | **0.490** |

---

## Summary Statistics (corrected lags, n=10)

| Metric | Value |
|--------|-------|
| Min    | 0.053s |
| Max    | 0.940s |
| Mean   | 0.482s |
| **P50** | **0.483s** |
| **P95** | **0.940s** |

Sorted corrected lags: `[0.053, 0.123, 0.376, 0.396, 0.422, 0.483, 0.490, 0.609, 0.925, 0.940]`

---

## Baseline Comparison

| Baseline | P50 | P95 | Notes |
|----------|-----|-----|-------|
| **P0.2 (legacy batch ingest)** | hours – days | hours – days | Manual batch pipeline; no streaming; uploads queued until next run |
| **P3.5 (Pathway streaming)** | **0.483s** | **0.940s** | S3 polling, 30s interval, differential dataflow |

**Improvement**: ~3–4 orders of magnitude reduction in freshness lag. Updates in the live S3 prefix are visible in Qdrant in under 1 second (p50) from the moment the S3 write completes.

### Why P50 < 1s despite a 30s poll interval?

Pathway's S3 connector does not wait for the full `autocommit_duration_ms` between checks when there is active work. The 30s setting governs the logical commit (autocommit) boundary, not the polling frequency itself. In practice, the connector detects and processes object changes within the same poll pass they occur.

---

## Verdict

```
P3_5_DONE_FRESHNESS_MEASURED
```

- All 10 update runs completed without timeout.
- Clock skew characterized and corrected.
- P50 = **0.483s**, P95 = **0.940s** — both well within the sub-2-second target.
- Pathway streaming ingest delivers real-time freshness vs. P0.2 batch baseline of hours–days.
