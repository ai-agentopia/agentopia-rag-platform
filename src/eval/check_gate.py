"""CI eval gate — validates committed artifact and route correctness.

Reads the committed nDCG@5 result artifact and checks the gate verdict.
Runs the pure-Python route correctness check (no live Qdrant needed).
Exits 0 only when both pass. Designed for eval-gate.yml on push/PR.

Contamination check: explicitly deferred to #17 (no automated signal).

Usage:
    python src/eval/check_gate.py
    python src/eval/check_gate.py --results-file path/to/artifact.json
"""

import argparse
import json
import os
import sys

_SRC_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _SRC_ROOT not in sys.path:
    sys.path.insert(0, _SRC_ROOT)

from eval.route_correctness import check_routes

_DEFAULT_RESULTS = os.path.join(
    os.path.dirname(__file__), "results", "nDCG-p3-pilot-gate.json"
)


def check_artifact(results_file: str) -> dict:
    if not os.path.exists(results_file):
        return {"passed": False, "reason": f"artifact missing: {results_file}"}

    with open(results_file) as f:
        result = json.load(f)

    gate = result.get("gate", {})
    passed = bool(gate.get("passed", False))
    ndcg_mean = gate.get("ndcg_mean")
    threshold = gate.get("threshold", 0.90)
    return {
        "passed": passed,
        "ndcg_mean": ndcg_mean,
        "threshold": threshold,
        "scope": result.get("scope", "?"),
        "timestamp": result.get("timestamp", "?"),
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="CI eval gate check")
    parser.add_argument("--results-file", default=_DEFAULT_RESULTS)
    args = parser.parse_args()

    print("=== CI Eval Gate ===")
    print()

    # 1. nDCG@5 artifact check
    print("1. nDCG@5 artifact check")
    ndcg_check = check_artifact(args.results_file)
    ndcg_ok = ndcg_check["passed"]
    if ndcg_ok:
        print(f"   PASS  nDCG@5={ndcg_check['ndcg_mean']:.4f} >= {ndcg_check['threshold']}")
        print(f"   Scope: {ndcg_check['scope']}  Run: {ndcg_check['timestamp']}")
    else:
        reason = ndcg_check.get("reason") or (
            f"nDCG@5={ndcg_check.get('ndcg_mean')} < {ndcg_check.get('threshold', 0.90)}"
        )
        print(f"   FAIL  {reason}")
    print()

    # 2. Route correctness check
    print("2. Route correctness check")
    route_result = check_routes()
    route_ok = route_result["passed"]
    if route_ok:
        print(f"   PASS  misroute_rate=0.0  ({route_result['total']} routes verified)")
    else:
        print(f"   FAIL  misroute_rate={route_result['misroute_rate']}")
        for r in route_result["routes"]:
            if not r["correct"]:
                print(f"   MISROUTED  {r['scope']} → {r['actual']} (expected {r['expected']})")
    print()

    # 3. Contamination check
    print("3. Contamination check — deferred to #17 (no automated signal available)")
    print()

    all_passed = ndcg_ok and route_ok
    print(f"=== Verdict: {'PASS' if all_passed else 'FAIL'} ===")
    sys.exit(0 if all_passed else 1)
