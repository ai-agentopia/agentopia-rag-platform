"""P3.6 retrieval eval gate — nDCG@5 on live chunked pilot scope.

Queries the Qdrant collection directly using the same embedding model
as the Pathway pipeline.  Grades results using document-level relevance
judgments from the eval dataset.

Usage (from pod or with port-forwarded Qdrant):
    python src/eval/p3_pilot_gate.py \
        --qdrant-url http://qdrant:6333 \
        --collection kb-45294aa854667d3b \
        --dataset src/eval/datasets/p3_pilot_gate.json

Usage (from local machine via SSH):
    Copy to pod and run, or port-forward Qdrant 6333.
"""

import argparse
import json
import math
import os
import sys
from datetime import datetime, timezone


# ── Metrics (self-contained, matches retrieval_metrics.py in super-rag) ──


def dcg_at_k(relevances: list, k: int) -> float:
    score = 0.0
    for i, rel in enumerate(relevances[:k]):
        score += rel / math.log2(i + 2)
    return score


def ndcg_at_k(relevances: list, k: int) -> float:
    dcg = dcg_at_k(relevances, k)
    ideal = sorted(relevances, reverse=True)
    idcg = dcg_at_k(ideal, k)
    return dcg / idcg if idcg > 0 else 0.0


def mrr(relevances: list, threshold: float = 1.0) -> float:
    for i, rel in enumerate(relevances):
        if rel >= threshold:
            return 1.0 / (i + 1)
    return 0.0


def precision_at_k(relevances: list, k: int, threshold: float = 1.0) -> float:
    top_k = relevances[:k]
    if not top_k:
        return 0.0
    return sum(1 for r in top_k if r >= threshold) / len(top_k)


def recall_at_k(relevances: list, k: int, total_relevant: int, threshold: float = 1.0) -> float:
    if total_relevant == 0:
        return 0.0
    return sum(1 for r in relevances[:k] if r >= threshold) / total_relevant


# ── Retrieval ──


def embed_query(text: str, client, model: str) -> list:
    resp = client.embeddings.create(input=text, model=model)
    return resp.data[0].embedding


def search_qdrant(qc, collection: str, embedding: list, limit: int = 5) -> list:
    results = qc.query_points(
        collection_name=collection,
        query=embedding,
        limit=limit,
        with_payload=True,
    )
    return results.points


# ── Grading ──


def grade_results(points: list, relevant_docs: list, k: int) -> dict:
    """Grade retrieved points against document-level relevance judgments."""
    doc_relevance = {d["document_id"]: d["relevance"] for d in relevant_docs}

    relevances = []
    detail = []
    for rank, pt in enumerate(points[:k]):
        payload = pt.payload or {}
        doc_id = payload.get("document_id", "")
        grade = doc_relevance.get(doc_id, 0)
        relevances.append(float(grade))
        detail.append({
            "rank": rank + 1,
            "document_id": doc_id,
            "section": payload.get("section", ""),
            "chunk_index": payload.get("chunk_index", -1),
            "score": round(pt.score, 4) if hasattr(pt, "score") else 0.0,
            "relevance_grade": grade,
            "text_preview": payload.get("text", "")[:80],
        })

    total_relevant = sum(1 for d in relevant_docs if d["relevance"] >= 1)

    metrics = {
        "ndcg": round(ndcg_at_k(relevances, k), 4),
        "mrr": round(mrr(relevances), 4),
        "precision": round(precision_at_k(relevances, k), 4),
        "recall": round(recall_at_k(relevances, k, total_relevant), 4),
    }

    return {
        "relevances": relevances,
        "metrics": metrics,
        "detail": detail,
        "total_relevant": total_relevant,
    }


# ── Main ──


def run_eval(args):
    from openai import OpenAI
    from qdrant_client import QdrantClient

    with open(args.dataset) as f:
        dataset = json.load(f)

    qc = QdrantClient(url=args.qdrant_url)
    openai_client = OpenAI(
        api_key=args.embedding_api_key or os.environ.get("EMBEDDING_API_KEY", ""),
        base_url=args.embedding_base_url or os.environ.get("EMBEDDING_BASE_URL", "https://api.openai.com/v1"),
    )
    model = args.embedding_model or os.environ.get("EMBEDDING_MODEL", "text-embedding-3-small")
    k = args.k

    per_query = []
    for sample in dataset["samples"]:
        sid = sample["id"]
        query = sample["query"]
        print(f"  [{sid}] {query[:70]}...", end=" ", flush=True)

        embedding = embed_query(query, openai_client, model)
        points = search_qdrant(qc, args.collection, embedding, limit=k)
        grading = grade_results(points, sample["relevant_documents"], k)

        print(f"nDCG@{k}={grading['metrics']['ndcg']:.4f}  MRR={grading['metrics']['mrr']:.4f}")

        per_query.append({
            "id": sid,
            "query": query,
            "scenario": sample.get("scenario", ""),
            **grading,
        })

    # Aggregate
    agg = {}
    for metric in ["ndcg", "mrr", "precision", "recall"]:
        values = [q["metrics"][metric] for q in per_query]
        agg[metric] = {
            "mean": round(sum(values) / len(values), 4) if values else None,
            "min": round(min(values), 4) if values else None,
            "max": round(max(values), 4) if values else None,
            "count": len(values),
        }

    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    result = {
        "phase": "p3",
        "type": "pilot_gate",
        "timestamp": ts,
        "scope": dataset.get("scope", ""),
        "collection": args.collection,
        "dataset": args.dataset,
        "matching_strategy": dataset.get("matching_strategy", ""),
        "k": k,
        "sample_count": len(per_query),
        "aggregates": agg,
        "per_query": per_query,
        "gate": {
            "threshold": 0.90,
            "baseline_reference": 0.925,
            "ndcg_mean": agg["ndcg"]["mean"],
            "passed": agg["ndcg"]["mean"] is not None and agg["ndcg"]["mean"] >= 0.90,
        },
    }

    os.makedirs(args.output_dir, exist_ok=True)
    out_path = os.path.join(args.output_dir, "nDCG-p3-pilot-gate.json")
    with open(out_path, "w") as f:
        json.dump(result, f, indent=2)

    print()
    print(f"=== P3.6 Retrieval Eval Gate ===")
    print(f"Scope:      {dataset.get('scope', '?')}")
    print(f"Collection: {args.collection}")
    print(f"Queries:    {len(per_query)}")
    print(f"K:          {k}")
    print()
    for metric in ["ndcg", "mrr", "precision", "recall"]:
        label = {"ndcg": "nDCG@5", "mrr": "MRR", "precision": "P@5", "recall": "R@5"}[metric]
        a = agg[metric]
        print(f"  {label:10s}  mean={a['mean']:.4f}  min={a['min']:.4f}  max={a['max']:.4f}")
    print()
    print(f"Gate threshold:   nDCG@5 >= 0.90")
    print(f"Baseline ref:     nDCG@5 = 0.925 (P0.1)")
    print(f"Result:           nDCG@5 = {agg['ndcg']['mean']:.4f}")
    print(f"Verdict:          {'PASS' if result['gate']['passed'] else 'FAIL'}")
    print(f"\nArtifact: {out_path}")

    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="P3.6 retrieval eval gate")
    parser.add_argument("--qdrant-url", default="http://qdrant.agentopia-dev.svc.cluster.local:6333")
    parser.add_argument("--collection", default="kb-45294aa854667d3b")
    parser.add_argument("--dataset", default="src/eval/datasets/p3_pilot_gate.json")
    parser.add_argument("--output-dir", default="src/eval/results")
    parser.add_argument("--embedding-base-url", default=None)
    parser.add_argument("--embedding-api-key", default=None)
    parser.add_argument("--embedding-model", default=None)
    parser.add_argument("--k", type=int, default=5)
    run_eval(parser.parse_args())
