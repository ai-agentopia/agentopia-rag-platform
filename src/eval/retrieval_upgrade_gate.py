"""Issue #17 — P5 evidence gate: retrieval upgrade candidates on post-Pathway corpus.

Evaluates two candidates against the dense-only baseline:
  W2 (hybrid BM25+dense): prerequisite check only — sparse vectors required at ingestion time.
  W4 (cross-encoder reranking): retrieve top-N dense, rerank with cross-encoder, take top-K.

Gate threshold: ≥ 3% nDCG@5 improvement over baseline required for authorization.
If no candidate clears 3% delta: verdict = not authorized, #17 closed, P5 deferred.

Usage (in-cluster):
    python src/eval/retrieval_upgrade_gate.py \
        --qdrant-url http://qdrant:6333 \
        --collection kb-45294aa854667d3b \
        --dataset src/eval/datasets/p3_pilot_gate.json \
        --output-dir /tmp/eval-results \
        --embedding-base-url https://openrouter.ai/api/v1 \
        --embedding-model openai/text-embedding-3-small
"""

import argparse
import json
import math
import os
import sys
from datetime import datetime, timezone


def dcg_at_k(relevances, k):
    score = 0.0
    for i, rel in enumerate(relevances[:k]):
        score += rel / math.log2(i + 2)
    return score


def ndcg_at_k(relevances, k):
    dcg = dcg_at_k(relevances, k)
    ideal = sorted(relevances, reverse=True)
    idcg = dcg_at_k(ideal, k)
    return dcg / idcg if idcg > 0 else 0.0


def embed_query(text, client, model):
    resp = client.embeddings.create(input=text, model=model)
    return resp.data[0].embedding


def search_dense(qc, collection, embedding, limit):
    results = qc.query_points(
        collection_name=collection,
        query=embedding,
        limit=limit,
        with_payload=True,
    )
    return results.points


def grade(points, relevant_docs, k):
    doc_rel = {d["document_id"]: d["relevance"] for d in relevant_docs}
    relevances = []
    for pt in points[:k]:
        payload = pt.payload or {}
        doc_id = payload.get("document_id", "")
        relevances.append(float(doc_rel.get(doc_id, 0)))
    total_relevant = sum(1 for d in relevant_docs if d["relevance"] >= 1)
    return {
        "ndcg": round(ndcg_at_k(relevances, k), 4),
        "relevances": relevances,
        "total_relevant": total_relevant,
    }


def check_w2_prerequisite(qc, collection):
    info = qc.get_collection(collection)
    sparse_config = info.config.params.sparse_vectors
    if sparse_config is None:
        return {
            "candidate": "W2",
            "status": "prerequisite_failed",
            "reason": (
                "Collection has no sparse vector index (sparse_vectors=None). "
                "BM25 hybrid retrieval requires sparse vectors configured at ingestion time. "
                "Qdrant v1.16.1 supports sparse vectors at server level, "
                "but the collection was not created with a sparse index. "
                "Re-indexing the full corpus would be required to enable W2."
            ),
            "qdrant_version_ok": True,
            "sparse_index_present": False,
            "verdict": "not_authorized",
        }
    return {
        "candidate": "W2",
        "status": "prerequisite_met",
        "sparse_index_present": True,
        "verdict": "needs_eval",
    }


def run_w4_reranking(samples, qc, collection, openai_client, model, k, retrieve_n=20):
    from sentence_transformers import CrossEncoder
    print(f"[W4] Loading cross-encoder model cross-encoder/ms-marco-MiniLM-L-6-v2 ...")
    reranker = CrossEncoder("cross-encoder/ms-marco-MiniLM-L-6-v2")
    print(f"[W4] Model loaded. Running {len(samples)} queries (retrieve top-{retrieve_n}, rerank to top-{k})...")

    per_query = []
    for sample in samples:
        sid = sample["id"]
        query = sample["query"]
        print(f"  [{sid}] {query[:70]}...", end=" ", flush=True)

        embedding = embed_query(query, openai_client, model)
        candidates = search_dense(qc, collection, embedding, limit=retrieve_n)

        # Build (query, passage) pairs for reranker
        pairs = []
        for pt in candidates:
            payload = pt.payload or {}
            text = payload.get("text", "")
            if not text:
                text = payload.get("section", "") + " " + payload.get("text_preview", "")
            pairs.append([query, text])

        if pairs:
            scores = reranker.predict(pairs).tolist()
            # Sort candidates by reranker score
            ranked = sorted(zip(scores, candidates), key=lambda x: x[0], reverse=True)
            reranked_points = [pt for _, pt in ranked]
        else:
            reranked_points = candidates

        grading = grade(reranked_points, sample["relevant_documents"], k)
        print(f"nDCG@{k}={grading['ndcg']:.4f}")

        per_query.append({
            "id": sid,
            "ndcg": grading["ndcg"],
        })

    mean_ndcg = round(sum(q["ndcg"] for q in per_query) / len(per_query), 4) if per_query else 0.0
    return {"mean_ndcg": mean_ndcg, "per_query": per_query}


def run_baseline(samples, qc, collection, openai_client, model, k):
    per_query = []
    for sample in samples:
        embedding = embed_query(sample["query"], openai_client, model)
        points = search_dense(qc, collection, embedding, limit=k)
        grading = grade(points, sample["relevant_documents"], k)
        per_query.append({"id": sample["id"], "ndcg": grading["ndcg"]})
    mean_ndcg = round(sum(q["ndcg"] for q in per_query) / len(per_query), 4) if per_query else 0.0
    return {"mean_ndcg": mean_ndcg, "per_query": per_query}


def main():
    parser = argparse.ArgumentParser(description="Issue #17 retrieval upgrade gate")
    parser.add_argument("--qdrant-url", default="http://qdrant.agentopia-dev.svc.cluster.local:6333")
    parser.add_argument("--collection", default="kb-45294aa854667d3b")
    parser.add_argument("--dataset", default="src/eval/datasets/p3_pilot_gate.json")
    parser.add_argument("--output-dir", default="src/eval/results")
    parser.add_argument("--embedding-base-url", default=None)
    parser.add_argument("--embedding-api-key", default=None)
    parser.add_argument("--embedding-model", default=None)
    parser.add_argument("--k", type=int, default=5)
    parser.add_argument("--retrieve-n", type=int, default=20,
                        help="Candidates retrieved for W4 reranking before taking top-k")
    args = parser.parse_args()

    from openai import OpenAI
    from qdrant_client import QdrantClient

    with open(args.dataset) as f:
        dataset = json.load(f)
    samples = dataset["samples"]

    qc = QdrantClient(url=args.qdrant_url)
    openai_client = OpenAI(
        api_key=args.embedding_api_key or os.environ.get("EMBEDDING_API_KEY", ""),
        base_url=args.embedding_base_url or os.environ.get("EMBEDDING_BASE_URL", "https://api.openai.com/v1"),
    )
    model = args.embedding_model or os.environ.get("EMBEDDING_MODEL", "text-embedding-3-small")
    k = args.k

    print("=== Issue #17 — P5 Retrieval Upgrade Gate ===")
    print(f"Collection: {args.collection}  |  Queries: {len(samples)}  |  K: {k}")
    print()

    # Baseline (dense-only)
    print("--- Baseline: dense-only ---")
    baseline = run_baseline(samples, qc, args.collection, openai_client, model, k)
    print(f"Baseline nDCG@{k} = {baseline['mean_ndcg']:.4f}")
    print()

    # W2 prerequisite check
    print("--- W2: hybrid BM25+dense (prerequisite check) ---")
    w2_result = check_w2_prerequisite(qc, args.collection)
    print(f"Status:  {w2_result['status']}")
    print(f"Reason:  {w2_result['reason']}")
    print(f"Verdict: {w2_result['verdict']}")
    print()

    # W4: cross-encoder reranking
    print("--- W4: cross-encoder reranking ---")
    w4_result = run_w4_reranking(
        samples, qc, args.collection, openai_client, model, k, args.retrieve_n
    )
    w4_delta = round(w4_result["mean_ndcg"] - baseline["mean_ndcg"], 4)
    w4_delta_pct = round(w4_delta / baseline["mean_ndcg"] * 100, 2) if baseline["mean_ndcg"] else 0.0
    w4_authorized = w4_delta_pct >= 3.0
    print(f"W4 nDCG@{k} = {w4_result['mean_ndcg']:.4f}  delta={w4_delta:+.4f} ({w4_delta_pct:+.2f}%)")
    print(f"Verdict:  {'authorized (≥3% delta)' if w4_authorized else 'not authorized (<3% delta)'}")
    print()

    # Final gate verdict
    any_authorized = w4_authorized
    gate_verdict = "authorized" if any_authorized else "not_authorized"

    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    result = {
        "gate": "retrieval_upgrade",
        "timestamp": ts,
        "scope": dataset.get("scope", ""),
        "collection": args.collection,
        "dataset": args.dataset,
        "k": k,
        "sample_count": len(samples),
        "delta_threshold_pct": 3.0,
        "baseline": {
            "strategy": "dense-only",
            "mean_ndcg": baseline["mean_ndcg"],
            "per_query": baseline["per_query"],
        },
        "candidates": {
            "W2": w2_result,
            "W4": {
                "candidate": "W4",
                "strategy": "cross-encoder reranking (ms-marco-MiniLM-L-6-v2)",
                "retrieve_n": args.retrieve_n,
                "k": k,
                "mean_ndcg": w4_result["mean_ndcg"],
                "delta": w4_delta,
                "delta_pct": w4_delta_pct,
                "authorized": w4_authorized,
                "per_query": w4_result["per_query"],
            },
        },
        "verdict": gate_verdict,
    }

    os.makedirs(args.output_dir, exist_ok=True)
    out_path = os.path.join(args.output_dir, "retrieval-upgrade-gate.json")
    with open(out_path, "w") as f:
        json.dump(result, f, indent=2)

    print("=== GATE VERDICT ===")
    print(f"Baseline:  nDCG@5 = {baseline['mean_ndcg']:.4f}")
    print(f"W2:        {w2_result['status']} — {w2_result['verdict']}")
    print(f"W4:        nDCG@5 = {w4_result['mean_ndcg']:.4f}  delta={w4_delta_pct:+.2f}% — {'authorized' if w4_authorized else 'not_authorized'}")
    print(f"Outcome:   P5 retrieval upgrades are {gate_verdict.replace('_', ' ').upper()}")
    print(f"\nArtifact: {out_path}")

    sys.exit(0 if any_authorized else 2)


if __name__ == "__main__":
    main()
