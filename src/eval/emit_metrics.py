"""Emit RAG eval metrics to Prometheus Pushgateway.

Reads the nDCG@5 eval artifact (committed or freshly computed) and the
route_correctness result, then pushes both to a Prometheus Pushgateway.

Metrics emitted:
  agentopia_rag_nDCG_at_5{scope}     — mean nDCG@5 from last eval run
  agentopia_rag_misroute_rate{scope} — 0.0 = all routes correct
  agentopia_rag_eval_timestamp{scope} — Unix ts of the results artifact

Usage (in-cluster CronJob, after p3_pilot_gate.py writes /tmp/eval-results/):
    python src/eval/emit_metrics.py \
        --pushgateway-url prometheus-pushgateway.monitoring.svc.cluster.local:9091 \
        --results-file /tmp/eval-results/nDCG-p3-pilot-gate.json

Note: --pushgateway-url is host:port without http:// prefix.
"""

import argparse
import json
import os
import sys
import time

_SRC_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _SRC_ROOT not in sys.path:
    sys.path.insert(0, _SRC_ROOT)

from eval.route_correctness import check_routes
from prometheus_client import CollectorRegistry, Gauge, push_to_gateway

_DEFAULT_PUSHGATEWAY = "prometheus-pushgateway.monitoring.svc.cluster.local:9091"
_DEFAULT_RESULTS = os.path.join(
    os.path.dirname(__file__), "results", "nDCG-p3-pilot-gate.json"
)


def emit(results_file: str, pushgateway_url: str, job: str) -> None:
    with open(results_file) as f:
        result = json.load(f)

    scope = result.get("scope", "unknown")
    ndcg_mean = result.get("aggregates", {}).get("ndcg", {}).get("mean")
    if ndcg_mean is None:
        raise ValueError(f"results file missing aggregates.ndcg.mean: {results_file}")

    route_result = check_routes()
    misroute_rate = route_result["misroute_rate"]

    registry = CollectorRegistry()

    g_ndcg = Gauge(
        "agentopia_rag_nDCG_at_5",
        "Mean nDCG@5 from the last live retrieval eval run",
        ["scope"],
        registry=registry,
    )
    g_misroute = Gauge(
        "agentopia_rag_misroute_rate",
        "Fraction of known scopes with incorrect collection routing (0.0 = all correct)",
        ["scope"],
        registry=registry,
    )
    g_ts = Gauge(
        "agentopia_rag_eval_timestamp",
        "Unix timestamp of the last eval artifact used for metric emission",
        ["scope"],
        registry=registry,
    )

    g_ndcg.labels(scope=scope).set(ndcg_mean)
    g_misroute.labels(scope=scope).set(misroute_rate)
    g_ts.labels(scope=scope).set(time.time())

    push_to_gateway(pushgateway_url, job=job, registry=registry)

    print(f"Pushed to {pushgateway_url} (job={job}):")
    print(f'  agentopia_rag_nDCG_at_5{{scope="{scope}"}} = {ndcg_mean}')
    print(f'  agentopia_rag_misroute_rate{{scope="{scope}"}} = {misroute_rate}')
    print(f'  agentopia_rag_eval_timestamp{{scope="{scope}"}} = {time.time():.0f}')


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Emit RAG eval metrics to Pushgateway")
    parser.add_argument(
        "--pushgateway-url",
        default=os.environ.get("PUSHGATEWAY_URL", _DEFAULT_PUSHGATEWAY),
        help="Pushgateway host:port (no http:// prefix)",
    )
    parser.add_argument(
        "--results-file",
        default=os.environ.get("EVAL_RESULTS_FILE", _DEFAULT_RESULTS),
    )
    parser.add_argument("--job", default="agentopia_rag_eval")
    args = parser.parse_args()

    emit(args.results_file, args.pushgateway_url, args.job)
